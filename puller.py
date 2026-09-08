#!/usr/bin/env python3
"""
Waseela Milk Exchange — data puller.
Connects to the Frappe Cloud MariaDB (read-only), computes the numbers the
dashboard needs, and writes data.json next to index.html.

Credentials come from environment variables (never hard-code them):
  MILK_DB_HOST, MILK_DB_PORT, MILK_DB_USER, MILK_DB_PASS, MILK_DB_NAME
Run:  MILK_DB_HOST=... MILK_DB_USER=... MILK_DB_PASS=... MILK_DB_NAME=... python3 puller.py
Schedule it (cron) and commit the refreshed data.json to publish updates.
"""
import os, json, datetime, pymysql

# Load credentials from a local .env (KEY=VALUE) if present — keep it git-ignored, never commit it.
def _load_env():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
_load_env()

CFG = dict(
    host=os.environ["MILK_DB_HOST"], port=int(os.environ.get("MILK_DB_PORT", 3306)),
    user=os.environ["MILK_DB_USER"], password=os.environ["MILK_DB_PASS"],
    database=os.environ["MILK_DB_NAME"], ssl={"ssl": {}},
    connect_timeout=20, read_timeout=90, cursorclass=pymysql.cursors.DictCursor,
)
TYPES = ["Buffalo", "Cow", "Mix"]
OUT = os.path.join(os.path.dirname(__file__), "data.json")


def norm_type(t):
    t = (t or "").strip().title()
    return t if t in TYPES else "Mix"


def main():
    c = pymysql.connect(**CFG)
    cur = c.cursor()

    # --- location master (names + coords) ---
    cur.execute("SELECT name, location_name, latitude, longitude FROM `tabLocation`")
    loc = {r["name"]: r for r in cur.fetchall()}

    # --- BUYING: rate from Milk Pricing (TS-based base), volume from Milk Dispatch ---
    cur.execute("""SELECT collection_center, milk_type, AVG(base_price) bp
                   FROM `tabMilk Pricing` WHERE collection_center IS NOT NULL AND collection_center<>''
                   GROUP BY collection_center, milk_type""")
    price = {}
    for r in cur.fetchall():
        price.setdefault(r["collection_center"], {})[norm_type(r["milk_type"])] = float(r["bp"])

    cur.execute("""SELECT collection_centre code, milk_type, SUM(gross_volume) vol
                   FROM `tabMilk Dispatch`
                   WHERE docstatus<2 AND collection_centre IS NOT NULL AND collection_centre<>''
                     AND creation>=DATE_SUB(NOW(), INTERVAL 90 DAY)
                   GROUP BY collection_centre, milk_type""")
    vol = {}
    for r in cur.fetchall():
        vol.setdefault(r["code"], {})[norm_type(r["milk_type"])] = float(r["vol"] or 0) / 90.0  # daily litres

    # average total-solids % per centre x type (clean 'ts' column; used to TS-adjust the paid rate)
    cur.execute("""SELECT collection_centre cc, milk_type mt, AVG(NULLIF(weighted_average_of_ts,0)) ts
                   FROM `tabMilk Dispatch` WHERE docstatus<2 AND collection_centre LIKE 'LOC-%%'
                     AND creation>=DATE_SUB(NOW(), INTERVAL 120 DAY)
                   GROUP BY collection_centre, milk_type""")
    tsmap = {}
    for r in cur.fetchall():
        if r["ts"]:
            tsmap.setdefault(r["cc"], {})[norm_type(r["mt"])] = float(r["ts"])

    def eff(base, code, t):
        """Effective paid rate = base (rate at TS13) x actual TS / 13. Falls back to base if no TS."""
        ts = tsmap.get(code, {}).get(t)
        return (round(base * ts / 13.0, 1), round(base, 1), round(ts, 2)) if ts else (round(base, 1), round(base, 1), None)

    centres = []
    for code, ptypes in price.items():
        if code not in loc:
            continue
        types = {}
        for t in TYPES:
            p = ptypes.get(t)
            v = vol.get(code, {}).get(t, 0)
            if p and v > 0:
                pr, base, ts = eff(p, code, t)
                types[t] = {"price": pr, "base": base, "ts": ts, "vol": round(v)}
        if not types:  # priced but no recent tagged volume — still show at nominal small volume
            for t, p in ptypes.items():
                pr, base, ts = eff(p, code, t)
                types[t] = {"price": pr, "base": base, "ts": ts, "vol": 200}
        centres.append({
            "code": code, "name": loc[code]["location_name"] or code,
            "lat": float(loc[code]["latitude"] or 0) or None,
            "lng": float(loc[code]["longitude"] or 0) or None,
            "types": types,
        })

    # --- OPEX per centre: expense GL (EXCLUDING Cost of Goods Sold) on the MCC cost centre, / litres ---
    KW = {"LOC-000784": "Bhawana", "LOC-000532": "Mahabali", "LOC-000503": "Ubhaan",
          "LOC-000526": "Tahli", "LOC-000356": "Kachain", "LOC-000816": "157"}
    for ce in centres:
        kwv = KW.get(ce["code"])
        if not kwv:
            continue
        cur.execute("SELECT name FROM `tabCost Center` WHERE name LIKE %s AND name LIKE %s", ("%" + kwv + "%", "MCC %"))
        ccs = [r["name"] for r in cur.fetchall()]
        if not ccs:
            continue
        ph = ",".join(["%s"] * len(ccs))
        cur.execute("SELECT g.account acct, SUM(g.debit-g.credit) amt FROM `tabGL Entry` g JOIN `tabAccount` a ON a.name=g.account "
                    "WHERE g.is_cancelled=0 AND a.root_type='Expense' AND a.name NOT LIKE %s "
                    "AND g.cost_center IN (" + ph + ") AND g.posting_date>=DATE_SUB(CURDATE(),INTERVAL 90 DAY) GROUP BY g.account",
                    ["%Cost of Goods Sold%"] + ccs)
        FIXED_KW = ("payroll", "rent", "finance", "depreciation", "office general")
        fixed = var = 0.0
        for r in cur.fetchall():
            amt = float(r["amt"] or 0)
            if any(k in (r["acct"] or "").lower() for k in FIXED_KW):
                fixed += amt
            else:
                var += amt
        cur.execute("SELECT SUM(gross_volume) v FROM `tabMilk Dispatch` WHERE docstatus<2 AND collection_centre=%s "
                    "AND creation>=DATE_SUB(NOW(),INTERVAL 90 DAY)", (ce["code"],))
        lit = float(cur.fetchone()["v"] or 0)
        if lit > 0 and (fixed + var) != 0:
            ce["opexVarPerL"] = round(var / lit, 2)
            ce["opexFixedPerL"] = round(fixed / lit, 2)

    # --- SELLING: Milk Sale (real price/L, litres, location, type) ---
    def sell_rows(days):
        cur.execute("""SELECT location, milk_type,
                              SUM(price_per_liter*milk_volume_liters)/SUM(milk_volume_liters) p,
                              SUM(milk_volume_liters) v
                       FROM `tabMilk Sale`
                       WHERE docstatus<2 AND creation>=DATE_SUB(NOW(), INTERVAL %s DAY)
                       GROUP BY location, milk_type""", (days,))
        return cur.fetchall()

    markets = {}
    for r in sell_rows(90):
        code = r["location"]
        if not code or code not in loc:
            continue
        t = norm_type(r["milk_type"])
        m = markets.setdefault(code, {"code": code, "name": loc[code]["location_name"] or code,
                                      "lat": float(loc[code]["latitude"] or 0) or None,
                                      "lng": float(loc[code]["longitude"] or 0) or None, "types": {}})
        m["types"][t] = {"price": round(float(r["p"]), 1), "vol": round(float(r["v"]) / 90.0)}
    markets = list(markets.values())

    # --- company headline rolling (volume-weighted) for reference ---
    def company(days, table_price_vol):
        num = den = 0
        for e in table_price_vol:
            for t, d in e["types"].items():
                num += d["price"] * d["vol"]; den += d["vol"]
        return round(num / den, 2) if den else None

    cur.execute("""SELECT SUM(price_per_liter*milk_volume_liters)/SUM(milk_volume_liters) p
                   FROM `tabMilk Sale` WHERE docstatus<2 AND creation>=DATE_SUB(NOW(),INTERVAL 30 DAY)""")
    sell30 = cur.fetchone()["p"]

    # --- WEEKLY TIME SERIES (real movement) ---
    def wk_label(yw):
        y, w = int(str(yw)[:4]), int(str(yw)[4:])
        return datetime.date.fromisocalendar(y, w, 1).strftime("%d %b")

    # BUY: weekly volume-weighted TS per centre -> effective price = centre base x TS/13
    base_c = {ce["code"]: (sum(t["base"] for t in ce["types"].values()) / len(ce["types"]) if ce["types"] else 150.0) for ce in centres}
    cur.execute("""SELECT collection_centre cc, YEARWEEK(creation,3) wk,
                     SUM(weighted_average_of_ts*gross_volume)/SUM(gross_volume) ts, SUM(gross_volume) vol
                   FROM `tabMilk Dispatch` WHERE docstatus<2 AND collection_centre LIKE 'LOC-%%'
                     AND weighted_average_of_ts BETWEEN 8 AND 18 AND creation>=DATE_SUB(NOW(),INTERVAL 210 DAY)
                   GROUP BY cc, wk""")
    brows = cur.fetchall()
    bweeks = sorted({r["wk"] for r in brows})[-26:]
    bC, bN, bD = {}, {w: 0.0 for w in bweeks}, {w: 0.0 for w in bweeks}
    for r in brows:
        if r["wk"] not in bweeks or not r["ts"]:
            continue
        pr = base_c.get(r["cc"], 150.0) * float(r["ts"]) / 13.0
        v = float(r["vol"] or 0)
        bC.setdefault(r["cc"], {})[r["wk"]] = round(pr, 1)
        bN[r["wk"]] += pr * v; bD[r["wk"]] += v
    buy_series = {"weeks": [wk_label(w) for w in bweeks],
                  "company": [round(bN[w] / bD[w], 1) if bD[w] else None for w in bweeks],
                  "byCentre": {cc: [bC.get(cc, {}).get(w) for w in bweeks] for cc in bC}}

    # SELL: weekly volume-weighted price per market from Milk Sale
    cur.execute("""SELECT location loc, YEARWEEK(creation,3) wk,
                     SUM(price_per_liter*milk_volume_liters)/SUM(milk_volume_liters) p, SUM(milk_volume_liters) v
                   FROM `tabMilk Sale` WHERE docstatus<2 AND creation>=DATE_SUB(NOW(),INTERVAL 210 DAY)
                   GROUP BY loc, wk""")
    srows = cur.fetchall()
    sweeks = sorted({r["wk"] for r in srows})[-26:]
    sC, sN, sD = {}, {w: 0.0 for w in sweeks}, {w: 0.0 for w in sweeks}
    for r in srows:
        if r["wk"] not in sweeks:
            continue
        sC.setdefault(r["loc"], {})[r["wk"]] = round(float(r["p"]), 1)
        sN[r["wk"]] += float(r["p"]) * float(r["v"]); sD[r["wk"]] += float(r["v"])
    sell_series = {"weeks": [wk_label(w) for w in sweeks],
                   "company": [round(sN[w] / sD[w], 1) if sD[w] else None for w in sweeks],
                   "byMarket": {loc: [sC.get(loc, {}).get(w) for w in sweeks] for loc in sC}}

    data = {
        "asOf": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "source": "Frappe Cloud replica — Milk Pricing/Dispatch (buy) + Milk Sale (sell)",
        "buy": {"centres": centres, "company30": company(30, centres), "series": buy_series},
        "sell": {"markets": markets, "company30": round(float(sell30), 2) if sell30 else None, "series": sell_series},
        "notes": {
            "buying": "Effective rate = Milk Pricing base_price x (avg TS / 13), per centre x type, using clean TS from Milk Dispatch (last 120d); volume-weighted by dispatch litres. Varies by quality (Buffalo>Cow). Still no day-by-day history, so the rolling line reflects current quality/mix, not a negotiated-price time series.",
            "market_competitor": "Not in ERP — agent-posted; left to the app.",
            "opex": "opexPerL = expense GL on the MCC cost centre EXCLUDING Cost of Goods Sold, over last 90d, / dispatched litres. Total operating opex (fixed+variable together) until accounts are tagged fixed/variable.",
        },
    }
    with open(OUT, "w") as f:
        json.dump(data, f, indent=1)
    c.close()
    print("wrote", OUT)
    print("company 30d BUY  ~", data["buy"]["company30"])
    print("company 30d SELL ~", data["sell"]["company30"])
    print("centres:", [f'{x["name"]}({len(x["types"])})' for x in centres])
    print("markets:", [x["name"] for x in markets])


if __name__ == "__main__":
    main()
