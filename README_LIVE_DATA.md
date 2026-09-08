# Milk Exchange — live data wiring

## How it works (static site + puller)
GitHub Pages can't run a database, so:
1. **`puller.py`** connects to the Frappe Cloud DB (read-only), runs SQL, and writes **`data.json`**.
2. **`index.html`** fetches `data.json` on load and renders the desks from it. If `data.json` is missing/unreachable it silently falls back to the demo seed. The top bar shows **"● Live · <timestamp>"** when live data loaded, else "Demo data (seed)".
3. To refresh the live site: re-run the puller, then commit **both** `index.html` and the updated `data.json` to the repo.

## Run the puller
Credentials come from environment variables (never hard-coded):
```
MILK_DB_HOST=n2-singapore.frappe.cloud MILK_DB_PORT=3306 \
MILK_DB_USER=<user> MILK_DB_PASS=<pass> MILK_DB_NAME=<db> \
python3 puller.py
```
Needs `pip install pymysql`. Schedule it (cron / GitHub Action) to keep `data.json` fresh, e.g. hourly.

## What's live vs. pending
- **Selling** — real, from `tabMilk Sale` (price/L × litres, by location & type).
- **Buying** — rate from `tabMilk Pricing` (TS-based `base_price`), volume-weighted by `tabMilk Dispatch` litres. *No historical price series yet*, so the buying trend is flat until price history is captured. (Confirm with tech lead how supplier pay-rate is derived — TS/13-TS formula — to make it a true time series.)
- **Market/competitor price** — not in ERP (agent-posted); stays app-side.
- **OpEx** — not pulled yet (`GL Entry` by Cost Center). Landed Cost uses a placeholder; wire GL next.
- **Coordinates** — several MCCs have placeholder coords in `tabLocation`; the app substitutes known lat/lng for the four main centres.

## Current live snapshot
Company all-milk 30-day: **buy ≈ Rs 152.9/L**, **sell ≈ Rs 167/L** → margin ≈ Rs 14/L.
Centres: Mahabali, Bhawana, Ubhaan, Tahli Rang Shah, Kachain (Kala Bali), Chak 157NB.

## Auto-refresh with GitHub Actions
`.github/workflows/refresh-data.yml` runs the puller hourly (and on-demand via "Run workflow"), then commits the refreshed `data.json`. It uses **GitHub Secrets** (never the `.env`).

Set up once, in the `milktrading` repo → **Settings → Secrets and variables → Actions → New repository secret**, add:
- `MILK_DB_HOST` = n2-singapore.frappe.cloud
- `MILK_DB_PORT` = 3306
- `MILK_DB_USER` = <user>
- `MILK_DB_PASS` = <password>
- `MILK_DB_NAME` = <db>

Then commit `puller.py` + the workflow. Actions → "Refresh milk data.json" → Run workflow to test.

**Caveat — DB IP allowlist:** GitHub's runners use rotating IPs. If Frappe Cloud restricts DB access by IP, the Action can't connect. Options: (a) allow GitHub Actions IP ranges, (b) run the puller from a fixed server/cron instead (same script, env vars), or (c) whitelist a self-hosted runner. Change the `cron:` line to adjust frequency.

## OpEx — now live AND split fixed vs variable
Per centre, from GL on the **MCC cost centre**, **excluding "Cost of Goods Sold"** (the milk purchase), last 90 days ÷ dispatched litres:
- `opexVarPerL` — variable: FESCO (electricity), generator fuel, PSO cards, commission, repairs, freight, stock adj. **Loads into landed cost.**
- `opexFixedPerL` — fixed: payroll, rent, finance, depreciation, office general. **Shown but excluded from landed cost.**

Classification is by account name keyword in `puller.py` (`FIXED_KW`). Adjust that list, or better, tag accounts in ERP, to refine. Centres with no dispatch litres fall back to a Rs 10/L placeholder. Note: where a centre's tagged dispatch volume is low, its fixed/L looks high (fixed overhead ÷ few litres) — fixes itself once volume tagging is complete (see below).

## Buying price TREND — why it's flat, and the one fix needed
The buy number is **real but flat**, for two data reasons (not a tool issue):
1. **Administered pricing.** Prices are set values (`Milk Pricing` base ≈ Rs 152–155; every recent sale is exactly Rs 167). They don't fluctuate per transaction, so a rolling line is flat until someone changes the set price.
2. **COGS and volume don't overlap in time.** Daily `Cost of Goods Sold` is booked recently, but `Milk Dispatch` volumes stopped being centre-tagged around late July — so `COGS ÷ litres` (the true paid price/L) can't be computed as a continuous series.

**Fix for a real, moving buy trend:** capture the **effective paid rate per collection, per day, with the collection centre and 13-TS on the row** (either keep `Milk Dispatch.collection_centre` populated with `gross_volume`, or add rate+qty to `Milk Collections`). Then buy price/L = COGS ÷ litres varies daily with fat/TS and negotiation, and the puller can emit a genuine time series. Until then the desks show the correct current level with a flat history.

## SECURITY
The DB credentials were shared in chat — **rotate the password now** and keep the new one only in the puller's environment (or a secrets manager), never in the committed files. `data.json` contains only aggregated prices/volumes, no credentials.
