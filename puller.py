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

    centres = []
    for code, ptypes in price.items():
        if code not in loc:
            continue
        types = {}
        for t in TYPES:
            p = ptypes.get(t)
            v = vol.get(code, {}).get(t, 0)
            if p and v > 0:
                types[t] = {"price": round(p, 1), "vol": round(v)}
        if not types:  # priced but no recent tagged volume — still show at nominal small volume
            for t, p in ptypes.items():
                types[t] = {"price": round(p, 1), "vol": 200}
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

    data = {
        "asOf": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "source": "Frappe Cloud replica — Milk Pricing/Dispatch (buy) + Milk Sale (sell)",
        "buy": {"centres": centres, "company30": company(30, centres)},
        "sell": {"markets": markets, "company30": round(float(sell30), 2) if sell30 else None},
        "notes": {
            "buying": "Rate = Milk Pricing base_price (TS-based master), volume-weighted by Milk Dispatch litres (last 90d). No historical price series yet, so buying trend is flat until price history is captured.",
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
