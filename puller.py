#!/usr/bin/env python3
"""
Waseela Milk Exchange — data puller.
Connects to the Frappe Cloud MariaDB (read-only), computes the numbers the
dashboard needs, and writes data.json next to index.html.

Credentials come from environment variables (never hard-code them):
  MILK_DB_HOST, MILK_DB_PORT, MILK_DB_USER, MILK_DB_PASS, MILK_DB_NAME
Run:  python3 puller.py   (reads .env if present)
Schedule it (cron / GitHub Action) and commit the refreshed data.json to publish.

BUYING source = tabMilk Supplier Deposit (child of Milk Collections): the live
per-supplier intake with the ACTUAL paid amount. Milk Dispatch/Receive were
retired ~20 Jul 2026, so we use Supplier Deposit which is current to today.
  buy price/L = SUM(total_payable_amount) / SUM(milk_volume)
SELLING source = tabMilk Sale (price_per_liter, litres, location, type).
"""
import os, json, datetime, pymysql

def _load_env():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
_load_env()

CFG = dict(
    host=os.environ["MILK_DB_HOST"], port=int(os.environ.get("MILK_DB_PORT", 3306)),
    user=os.environ["MILK_DB_USER"], password=os.environ["MILK_DB_PASS"],
    database=os.environ["MILK_DB_NAME"], ssl={"ssl": {}},
    connect_timeout=20, read_timeout=120, cursorclass=pymysql.cursors.DictCursor,
)
TYPES = ["Buffalo", "Cow", "Mix"]
OUT = os.path.join(os.path.dirname(__file__), "data.json")
MSD = ("`tabMilk Supplier Deposit` d JOIN `tabMilk Collections` p ON p.name=d.parent")


def norm_type(t):
    t = (t or "").strip().title()
    return t if t in TYPES else "Mix"


def wk_label(yw):
    y, w = int(str(yw)[:4]), int(str(yw)[4:])
    return datetime.date.fromisocalendar(y, w, 1).strftime("%d %b")


def main():
    c = pymysql.connect(**CFG)
    cur = c.cursor()

    cur.execute("SELECT name, location_name, latitude, longitude FROM `tabLocation`")
    loc = {r["name"]: r for r in cur.fetchall()}

    # ---------- BUYING: actual paid price from Milk Supplier Deposit (last 30d) ----------
    cur.execute("SELECT p.collection_center cc, d.milk_type mt, "
                "SUM(d.total_payable_amount) pay, SUM(d.milk_volume) vol, "
                "SUM(d.total_solids*d.milk_volume)/NULLIF(SUM(d.milk_volume),0) ts "
                "FROM " + MSD + " WHERE d.docstatus<2 AND d.milk_volume>0 "
                "AND d.creation>=DATE_SUB(NOW(),INTERVAL 30 DAY) GROUP BY p.collection_center, d.milk_type")
    cur30 = {}
    for r in cur.fetchall():
        cc = r["cc"]
        v = float(r["vol"] or 0)
        if not cc or v <= 0:
            continue
        pr = round(float(r["pay"] or 0) / v, 1)
        cur30.setdefault(cc, {})[norm_type(r["mt"])] = {
            "price": pr, "base": pr, "ts": round(float(r["ts"]), 2) if r["ts"] else None, "vol": round(v / 30.0)}
    centres = []
    for cc, types in cur30.items():
        if cc not in loc or not (loc[cc]["location_name"] or "").startswith("MCC"):
            continue
        centres.append({"code": cc, "name": loc[cc]["location_name"] or cc,
                        "lat": float(loc[cc]["latitude"] or 0) or None,
                        "lng": float(loc[cc]["longitude"] or 0) or None, "types": types})

    # ---------- OPEX per centre: GL ex-COGS on the MCC cost centre, split fixed/variable, / live litres ----------
    KW = {"LOC-000784": "Bhawana", "LOC-000532": "Mahabali", "LOC-000503": "Ubhaan",
          "LOC-000526": "Tahli", "LOC-000356": "Kachain", "LOC-000816": "157"}
    FIXED_KW = ("payroll", "rent", "finance", "depreciation", "office general")
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
                    "WHERE g.is_cancelled=0 AND a.root_type='Expense' AND a.name NOT LIKE %s AND g.cost_center IN (" + ph + ") "
                    "AND g.posting_date>=DATE_SUB(CURDATE(),INTERVAL 90 DAY) GROUP BY g.account", ["%Cost of Goods Sold%"] + ccs)
        fixed = var = 0.0
        for r in cur.fetchall():
            amt = float(r["amt"] or 0)
            if any(k in (r["acct"] or "").lower() for k in FIXED_KW):
                fixed += amt
            else:
                var += amt
        cur.execute("SELECT SUM(d.milk_volume) v FROM " + MSD + " WHERE d.docstatus<2 AND d.milk_volume>0 "
                    "AND p.collection_center=%s AND d.creation>=DATE_SUB(NOW(),INTERVAL 90 DAY)", (ce["code"],))
        lit = float(cur.fetchone()["v"] or 0)
        if lit > 0 and (fixed + var) != 0:
            ce["opexVarPerL"] = round(var / lit, 2)
            ce["opexFixedPerL"] = round(fixed / lit, 2)

    # ---------- BUY weekly series (true paid price, through today) ----------
    cur.execute("SELECT p.collection_center cc, YEARWEEK(d.creation,3) wk, "
                "SUM(d.total_payable_amount) pay, SUM(d.milk_volume) vol "
                "FROM " + MSD + " WHERE d.docstatus<2 AND d.milk_volume>0 "
                "AND d.creation>=DATE_SUB(NOW(),INTERVAL 210 DAY) GROUP BY p.collection_center, wk")
    brows = cur.fetchall()
    bweeks = sorted({r["wk"] for r in brows})[-26:]
    bC, bV, bN, bD = {}, {}, {w: 0.0 for w in bweeks}, {w: 0.0 for w in bweeks}
    for r in brows:
        if r["wk"] not in bweeks or not r["cc"] or not r["vol"]:
            continue
        v = float(r["vol"]); pay = float(r["pay"] or 0)
        bC.setdefault(r["cc"], {})[r["wk"]] = round(pay / v, 1)
        bV.setdefault(r["cc"], {})[r["wk"]] = round(v)
        bN[r["wk"]] += pay; bD[r["wk"]] += v
    buy_series = {"weeks": [wk_label(w) for w in bweeks],
                  "company": [round(bN[w] / bD[w], 1) if bD[w] else None for w in bweeks],
                  "vol": [round(bD[w]) for w in bweeks],
                  "byCentre": {cc: [bC.get(cc, {}).get(w) for w in bweeks] for cc in bC},
                  "byCentreVol": {cc: [bV.get(cc, {}).get(w) for w in bweeks] for cc in bV}}

    # ---------- SELLING: Milk Sale (last 30d levels) ----------
    cur.execute("""SELECT location, milk_type, SUM(price_per_liter*milk_volume_liters)/SUM(milk_volume_liters) p,
                          SUM(milk_volume_liters) v
                   FROM `tabMilk Sale` WHERE docstatus<2 AND creation>=DATE_SUB(NOW(),INTERVAL 30 DAY)
                   GROUP BY location, milk_type""")
    markets = {}
    for r in cur.fetchall():
        code = r["location"]
        if not code or code not in loc:
            continue
        m = markets.setdefault(code, {"code": code, "name": loc[code]["location_name"] or code,
                                      "lat": float(loc[code]["latitude"] or 0) or None,
                                      "lng": float(loc[code]["longitude"] or 0) or None, "types": {}})
        m["types"][norm_type(r["milk_type"])] = {"price": round(float(r["p"]), 1), "vol": round(float(r["v"]) / 30.0)}
    markets = list(markets.values())

    cur.execute("""SELECT location loc, YEARWEEK(creation,3) wk,
                     SUM(price_per_liter*milk_volume_liters)/SUM(milk_volume_liters) p, SUM(milk_volume_liters) v
                   FROM `tabMilk Sale` WHERE docstatus<2 AND creation>=DATE_SUB(NOW(),INTERVAL 210 DAY) GROUP BY loc, wk""")
    srows = cur.fetchall()
    sweeks = sorted({r["wk"] for r in srows})[-26:]
    sC, sV, sN, sD = {}, {}, {w: 0.0 for w in sweeks}, {w: 0.0 for w in sweeks}
    for r in srows:
        if r["wk"] not in sweeks:
            continue
        sC.setdefault(r["loc"], {})[r["wk"]] = round(float(r["p"]), 1)
        sV.setdefault(r["loc"], {})[r["wk"]] = round(float(r["v"]))
        sN[r["wk"]] += float(r["p"]) * float(r["v"]); sD[r["wk"]] += float(r["v"])
    sell_series = {"weeks": [wk_label(w) for w in sweeks],
                   "company": [round(sN[w] / sD[w], 1) if sD[w] else None for w in sweeks],
                   "vol": [round(sD[w]) for w in sweeks],
                   "byMarket": {loc_: [sC.get(loc_, {}).get(w) for w in sweeks] for loc_ in sC},
                   "byMarketVol": {loc_: [sV.get(loc_, {}).get(w) for w in sweeks] for loc_ in sV}}

    def company30(entities):
        num = den = 0.0
        for e in entities:
            for d in e["types"].values():
                num += d["price"] * d["vol"]; den += d["vol"]
        return round(num / den, 2) if den else None

    data = {
        "asOf": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "source": "Frappe Cloud replica — Milk Supplier Deposit (buy, actual paid) + Milk Sale (sell)",
        "buy": {"centres": centres, "company30": company30(centres), "series": buy_series},
        "sell": {"markets": markets, "company30": company30(markets), "series": sell_series},
        "notes": {
            "buying": "Actual paid price/L = SUM(total_payable_amount)/SUM(milk_volume) from Milk Supplier Deposit (child of Milk Collections) — the live intake table, current to today. Dispatch/Receive were retired ~20 Jul 2026. Weekly series is the true paid price.",
            "market_competitor": "Not in ERP — agent-posted; left to the app.",
            "opex": "Expense GL on the MCC cost centre EXCLUDING Cost of Goods Sold, last 90d, split fixed/variable by account name, / live intake litres.",
        },
    }
    with open(OUT, "w") as f:
        json.dump(data, f, indent=1)
    c.close()
    print("wrote", OUT)
    print("company 30d BUY  ~", data["buy"]["company30"])
    print("company 30d SELL ~", data["sell"]["company30"])
    print("buy weeks:", buy_series["weeks"][-1] if buy_series["weeks"] else None, "->", len(buy_series["weeks"]), "weeks")
    print("centres:", [f'{x["name"]}({len(x["types"])})' for x in centres])


if __name__ == "__main__":
    main()
