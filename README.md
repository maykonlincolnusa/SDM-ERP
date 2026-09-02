# SDM — Operational ERP for Small Food Retail

*[Versão em português](README.pt-BR.md)*

SDM answers one question that most point-of-sale systems leave open: **what did this shop actually earn this month, after everything?**

Not revenue. Not gross margin on the menu price. Operating profit — after ingredients, packaging, marketplace commissions, delivery payouts, waste, rent and payroll.

It was built for and runs in a real açaí shop in Brazil, but the model it implements — recipes, add-ons, per-channel economics, perishable stock, recurring fixed cost — fits any small food retailer whose margin lives or dies in details nobody has time to track by hand.

---

## The principle: it refuses to guess

Most small-business dashboards will happily show you a margin. They compute it by assuming the costs they don't know are zero, which quietly turns every gap in the data into profit.

SDM does the opposite, everywhere:

- An order imported from a delivery platform without line items records **cost as unknown**, never as zero. It counts toward revenue and is excluded from margin.
- Every profit figure ships with a **coverage percentage** — the share of revenue whose cost is actually known. Below 100%, the screen says so, and the profit is labelled an upper bound rather than a number.
- Every forecast reports **its own error**, measured against the shop's own history. A weekday seen twice gets no weight of its own.
- When a calculation can't be made honestly, the API returns `null` and a plain-language explanation of what is missing, instead of a plausible-looking figure.

This is the difference between a system a shop owner trusts and one they stop opening after a month.

---

## What it does

**Sales and recipes.** Every product carries a bill of materials. A sale deducts each ingredient, the cup, and the lid from stock at the moment it is recorded. Add-ons — a banana, a shot of syrup, extra granola — are chosen per line at checkout and deduct their own portion, so a 500 ml cup with two toppings costs what it actually cost. Orders can be edited or cancelled; stock is reversed against exactly what left, not against a recipe that may have changed since.

**Inventory.** Purchases are recorded as purchases, not as positive adjustments: they carry supplier, invoice, and the packaging as bought ("2 boxes × 5 kg at €120 a box"), convert to the stock unit, and update the weighted-average cost. Physical counts ask for what's on the shelf and compute the difference themselves. Waste is a separate, explained movement.

**Costing.** A per-product dossier separates ingredients from packaging, shows what survives each channel's commission, allocates monthly fixed cost per unit, and gives the price needed for any target margin plus the break-even volume. A simulator runs the same maths for a product that doesn't exist yet — nothing is saved.

**Channels and delivery.** Each sales channel carries its real commission, so per-channel contribution is measured, not assumed. Couriers can be paid by delivery, by commission, by day rate, by salary, or any combination — because that is how neighbourhood shops actually pay. Shift settlements reconcile against the orders of the day.

**Fixed cost.** Rent, payroll, utilities and subscriptions are recurring series: the system posts each month as it falls due, and projects the months ahead from the contract rather than from an average of the past.

**Forecasting.** Daily revenue is decomposed into a baseline, a weekday factor and a trend, each visible and arguable. The forecast converts into a purchase plan through the recipes, and into a projected P&L combining forecast volume, current costs, channel fees, delivery and fixed expenses.

**Production and replenishment planning.** Tomorrow's batch size comes from a newsvendor model over empirical quantiles — not a fitted distribution — because a shop with a few months of history has too few observations per weekday for anything with parameters. Weekday factors are shrunk toward neutral by sample size. Lead-time demand comes from observed rolling windows. ABC/XYZ classification ranks what deserves attention.

**Growth suggestions.** Basket pairs that already sell together, empty weekdays, channel economics, ingredients sitting idle in the freezer — each with the arithmetic that produced it and the assumption written next to the estimate.

---

## Why there is no machine learning here

Deliberately. A shop doing single-digit daily orders with a few months of history has, per weekday, a handful of observations. Fitting a distribution to that produces numbers with four decimal places and no support: the model memorises the sample's noise and misses badly on the first unusual day.

Three choices avoid that, and each is visible in the code:

1. **Empirical quantiles, not fitted curves.** The percentile comes from the shop's own ordered observations. If the history has never seen a day above X, the plan does not invent one.
2. **Shrinkage on weekday effects.** Each factor is pulled back toward 1 by `n / (n + k)`. A Tuesday seen three times barely moves the plan; seen twenty times, it moves most of it.
3. **Empirical convolution for lead time.** Three-day demand comes from observed three-day rolling sums, with no normality assumption over a short horizon where it is weakest.

Everything runs in pure Python. No NumPy, no scikit-learn. The container stays small, and every step can be shown to the shop owner in their own language — which is the point, because a forecast the owner cannot argue with is a forecast they will not act on.

---

## Architecture

```
FastAPI  ·  Pydantic v2  ·  Python 3.12
Storage  ·  Firestore in production, JSON file locally — one interface, chosen at boot
Frontend ·  Vanilla JS PWA, no build step, installable, offline shell
Auth     ·  Firebase Authentication with a custom admin claim
Deploy   ·  Docker → Google Cloud Run, scale to zero
```

The storage layer is a narrow protocol — `list`, `get`, `put`, `delete`, `run_transaction` — with two implementations. The same service code runs against a local JSON file for development and Firestore in production, and the transaction boundary is honoured by both. Tests run entirely against the file store, in-process, with no emulator.

The frontend ships as static files with no bundler, no framework and no npm install. It is roughly 3,000 lines of readable JavaScript split into a read layer and an edit layer, plus a service worker that caches the application shell — never the data, because a device must never display stale sales figures.

```
app/
  main.py            FastAPI routes
  config.py          settings, validated at boot
  schemas/           Pydantic models — the contract, with the reasoning in comments
  repositories/      storage protocol + Firestore and JSON implementations
  services/
    erp.py           orders, inventory, expenses, channels, audit
    costing.py       cost dossier, simulator, brand-line performance
    forecast.py      revenue decomposition, purchase plan, projected P&L
    planning.py      newsvendor production plan, reorder points, ABC/XYZ
    delivery.py      couriers, shift settlement, reconciliation
    growth.py        opportunity detection with the arithmetic attached
    catalog.py       editable option lists
    media.py         document and receipt library
  static/            the PWA
etl/                 spreadsheet ingestion: bronze → silver → gold
scripts/             importers and operational tooling
tests/               178 tests, no mocks of the storage layer
```

---

## Running it

Requires Python 3.11 or newer. No database, no services, no configuration.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Open http://localhost:8000. The first run seeds a small demo shop — products with recipes, ingredients, add-ons, a week of sales — so every screen has something to show.

```bash
pytest                             # 178 tests, about two minutes
```

Tests exercise the services directly against a temporary JSON store. There are no mocks for storage, so concurrency, transactions and reversal logic are tested for real.

### Deploying

```bash
gcloud run deploy sdm --source . --region <region>
```

Set `DATA_MODE=firebase`, `AUTH_REQUIRED=true`, and the Firebase project variables. The container refuses to boot in production with authentication disabled — the settings validator enforces it rather than trusting a checklist.

---

## Status

Running in production at one shop. 178 tests passing.

Known limits, stated plainly:

- **Delivery-platform imports carry no line items.** The Brazilian platform this shop uses exports order totals but not what was sold, so imported orders have unknown cost until an API integration or manual entry supplies the items. The system reports this as missing coverage rather than papering over it.
- **The forecast horizon is capped at 30 days.** Quarterly, half-year and annual projections of operating profit are not implemented yet. The historical P&L already scales across those windows.
- **Localisation.** The interface is Portuguese only. The domain model is currency- and language-agnostic; the strings are not yet extracted.

---

## Licence

No licence file yet — all rights reserved by default. If you want to use, extend or deploy this, get in touch first.
