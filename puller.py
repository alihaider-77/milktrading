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

    # ---------- BUY daily series (raw daily price + volume, per centre) ----------
    cur.execute("SELECT p.collection_center cc, DATE(d.creation) dt, "
                "SUM(d.total_payable_amount) amt, SUM(d.milk_volume) vol "
                "FROM " + MSD + " WHERE d.docstatus<2 AND d.milk_volume>0 "
                "AND d.creation>=DATE_SUB(NOW(),INTERVAL 180 DAY) GROUP BY p.collection_center, dt")
    braw = cur.fetchall()
    ccset = {ce["code"] for ce in centres}
    bdates = sorted({str(r["dt"]) for r in braw if r["cc"] in ccset})
    bi = {d: i for i, d in enumerate(bdates)}
    bAmt = {cc: [0.0]*len(bdates) for cc in ccset}; bVol = {cc: [0.0]*len(bdates) for cc in ccset}
    for r in braw:
        if r["cc"] not in ccset:
            continue
        i = bi[str(r["dt"])]; bAmt[r["cc"]][i] += float(r["amt"] or 0); bVol[r["cc"]][i] += float(r["vol"] or 0)
    buy_daily = {"dates": [__import__("datetime").date.fromisoformat(d).strftime("%d %b") for d in bdates],
                 "byCentre": {cc: {"amt": [round(x) for x in bAmt[cc]], "vol": [round(x) for x in bVol[cc]]} for cc in ccset}}

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

    # ---------- SELL daily series ----------
    cur.execute("""SELECT location loc, DATE(creation) dt,
                     SUM(price_per_liter*milk_volume_liters) amt, SUM(milk_volume_liters) vol
                   FROM `tabMilk Sale` WHERE docstatus<2 AND creation>=DATE_SUB(NOW(),INTERVAL 180 DAY) GROUP BY loc, dt""")
    sraw = cur.fetchall()
    mset = {m["code"] for m in markets}
    sdates = sorted({str(r["dt"]) for r in sraw if r["loc"] in mset})
    si = {d: i for i, d in enumerate(sdates)}
    sAmt = {c: [0.0]*len(sdates) for c in mset}; sVol = {c: [0.0]*len(sdates) for c in mset}
    for r in sraw:
        if r["loc"] not in mset:
            continue
        i = si[str(r["dt"])]; sAmt[r["loc"]][i] += float(r["amt"] or 0); sVol[r["loc"]][i] += float(r["vol"] or 0)
    sell_daily = {"dates": [__import__("datetime").date.fromisoformat(d).strftime("%d %b") for d in sdates],
                  "byMarket": {c: {"amt": [round(x) for x in sAmt[c]], "vol": [round(x) for x in sVol[c]]} for c in mset}}

    def company30(entities):
        num = den = 0.0
        for e in entities:
            for d in e["types"].values():
                num += d["price"] * d["vol"]; den += d["vol"]
        return round(num / den, 2) if den else None

    data = {
        "asOf": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "source": "Frappe Cloud replica — Milk Supplier Deposit (buy, actual paid) + Milk Sale (sell)",
        "buy": {"centres": centres, "company30": company30(centres), "daily": buy_daily},
        "sell": {"markets": markets, "company30": company30(markets), "daily": sell_daily},
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
