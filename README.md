# SDM — Operational ERP for Small Food Retail

*[Versão em português](https://github.com/maykonlincolnusa/SDM)*

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

**Inventory.** Purchases are recorded as purchases, not as positive adjustments: they carry supplier, invoice, and the packaging as bought ("2 boxes × 5 kg at €120 a box"), convert to the stock unit, and update the weighted-average cost. Physical counts ask for what's on the shelf and compute the difference themselves. Waste is a separate, explained movement. Staff withdrawals — the morning batch, a tasting, restocking the counter — are recorded as one multi-item movement with the name of whoever took them, deducted all-or-nothing and undone the same way; they feed consumption and the production plan, and are never counted as waste. A withdrawal short on stock is refused by default — that is where a mistyped 50 for 0.5 gets caught — but can be confirmed through, since the ingredient did leave the shelf and a negative balance says more than a movement that was never recorded.

**Costing.** A per-product dossier separates ingredients from packaging, shows what survives each channel's commission, allocates monthly fixed cost per unit, and gives the price needed for any target margin plus the break-even volume. Two unit costs sit side by side: the recipe's, and the recipe's plus what that product's ingredients lose to waste and withdrawals — per ingredient, so the fruit's shrinkage never inflates the cup. Target price and break-even use the second, because pricing off the recipe alone is pricing off a cost the shop never had. A simulator runs the same maths for a product that doesn't exist yet — nothing is saved.

**Channels and delivery.** Each sales channel carries its real commission, so per-channel contribution is measured, not assumed. Couriers can be paid by delivery, by commission, by day rate, by salary, or any combination — because that is how neighbourhood shops actually pay. Shift settlements reconcile against the orders of the day.

**The street, recorded from the street.** Couriers get their own account and their own page — `/entregador`, one screen, one API call, targets sized for a gloved thumb. They check in on arrival, take an order off the day's queue, and mark arrival at the customer's door. Two taps per delivery, because the delivery is born from the order already in the ERP: products, quantity and freight come from it, and the courier confirms only the zone — which the system guesses from where that same customer ordered last time. The shift settlement is no longer typed at all; check-out computes it from what was recorded in the street and writes it into the same collection the payroll maths already reads. Nobody enters the same fact twice, and the second entry is always the one that diverges.

The courier is not limited to what the counter typed. A delivery can be registered for any of the last seven days — the shift that was forgotten does not have to be reopened, because making forgetting the cheaper option is how a record gets lost for good — and a delivery with no order in the system carries what was collected and how. That becomes a **sale pending review**: it counts immediately for the courier's settlement and for the territory reading, and it is not revenue until someone at the shop confirms it, which is the moment the bill of materials deducts stock. Whoever takes the money does not close their own revenue. The courier also sees their own history: every shift with its check-in and check-out times, and the deliveries that fell outside a shift, because checking your own settlement is the reason for keeping the record at all.

Their account reaches nothing else. Every ERP route already required an admin claim, so the restriction needs no blocklist to maintain: P&L, costing and pricing are simply unreachable from a phone that spends the day on a motorbike.

**Where the orders come from.** The system held no address anywhere — not in the order, not in the delivery aggregator's report, not in what the iFood integration kept. "Which neighbourhoods order most" was a question with no data behind it. Zones are now registered rather than typed, because *Centro*, *centro* and *Ctr* are one street and three rows in any report; the free-text landmark stays on the delivery, where it belongs. From that come demand per zone, delivery time against the time the shop promised for that zone, and the reading a single menu hides: the product that is average shop-wide and the champion of one neighbourhood.

**iFood, connected rather than imported.** The marketplace is the one channel that outlives the current delivery aggregator, so it gets a real integration instead of another spreadsheet importer. Activation follows iFood's distributed-application flow: the ERP asks for a user code, the shop types that code into the Partner Portal under Services and solutions → Integrations → Activate application by code, and pastes the authorisation code back here — no secret is ever typed into a third-party screen, and neither key nor token is ever returned to the browser. From then on the event queue is polled, and an order is written when the shop *confirms* it, not when it merely arrives: deducting ingredients for an order that can still be refused would be inventing consumption. The bill of materials deducts stock and computes the cost, while the revenue recorded is what iFood charged rather than what this menu would have charged — the gap between the two is stored, not smoothed. An item whose iFood menu entry carries no external code cannot be costed, so that order is recorded with the right revenue and the cost declared unknown; inventing a recipe for it would poison every other margin. Events are acknowledged only after the order is persisted, and each iFood order maps to exactly one ERP order, so a repeated event cannot invent a second sale. The credentials do not live with the data: application secret and refresh token go to Secret Manager, while the business database keeps only the connection state — one active version at a time, because the token rotates every six hours and stale versions bill monthly.

**Fixed cost.** Rent, payroll, utilities and subscriptions are recurring series: the system posts each month as it falls due, and projects the months ahead from the contract rather than from an average of the past.

**Forecasting.** Daily revenue is decomposed into a baseline, a weekday factor and a trend, each visible and arguable. The forecast converts into a purchase plan through the recipes, and into a projected P&L combining forecast volume, current costs, channel fees, delivery and fixed expenses.

**Production and replenishment planning.** Tomorrow's batch size comes from a newsvendor model over empirical quantiles — not a fitted distribution — because a shop with a few months of history has too few observations per weekday for anything with parameters. Weekday factors are shrunk toward neutral by sample size. Lead-time demand comes from observed rolling windows. ABC/XYZ classification ranks what deserves attention.

**Growth suggestions.** Basket pairs that already sell together, empty weekdays, channel economics, ingredients sitting idle in the freezer — each with the arithmetic that produced it and the assumption written next to the estimate. Channel advice compares the shop's own ordering link against the marketplace on revenue and commission actually paid, and the counter is excluded from that comparison: it takes no commission either, but it is not somewhere you can publish an offer.

**Pricing.** Price has three anchors, and they answer different questions: cost says what you *need* to charge, target margin says what you *want* to charge, and the market says what you *can* charge. Only the third is not inside the shop, so it is entered rather than inferred — dated observations of what competitors charge, which age and are flagged when they do. Alongside it sits the market the shop *can* measure: what its own customers actually pay, per channel, and which price band the volume sits in. Without a reference the verdict says so instead of dressing cost up as market reading.

**Weekly promotions.** A discount is margin given away, and it only pays back in volume that would not have come otherwise. Every suggestion therefore carries its **break-even**: how many extra units that specific weekday must sell for the promotion not to be a loss. The month is planned as four weeks, and the week splits in two halves that ask for opposite mechanics: the empty half needs *traffic*, which price buys, and the full half needs *ticket*, which a size upgrade or an add-on buys without touching the headline price — discounting a full day buys sales that were already coming. Four weeks never repeat the same anchor: a discount that returns every week stops being a promotion and becomes the new price. Each offer also carries the pricing verdict for the product it discounts, because a promotion does not fix a price that was wrong to begin with. Three rules bound it — never on a strong day (that buys sales you already had), never below the floor (cost with shrinkage, net of the channel's commission), and the source always declared: measured sales, menu registration, or an anchor the owner entered. Weekday averages count only the days the shop actually opened, so a day with few openings is reported as closed rather than weak — no discount fixes a closed door. For the sports line there is no data on training habits anywhere in the system, so the shop registers its anchors — the gym, the running group, the local team, and the days they train — and suggestions built on them say so instead of guessing.

---

## Why there is no machine learning here

Deliberately. A shop doing single-digit daily orders with a few months of history has, per weekday, a handful of observations. Fitting a distribution to that produces numbers with four decimal places and no support: the model memorises the sample's noise and misses badly on the first unusual day.

Three choices avoid that, and each is visible in the code:

1. **Empirical quantiles, not fitted curves.** The percentile comes from the shop's own ordered observations. If the history has never seen a day above X, the plan does not invent one.
2. **Shrinkage on weekday effects.** Each factor is pulled back toward 1 by `n / (n + k)`. A Tuesday seen three times barely moves the plan; seen twenty times, it moves most of it.
3. **Empirical convolution for lead time.** Three-day demand comes from observed three-day rolling sums, with no normality assumption over a short horizon where it is weakest.

The same rule governs the newest reading, demand by zone. A zone's forecast is the product of three numbers that all appear on screen — the average per day the shop actually opened, that weekday's weight, and the trend of the last fortnight — and the trend is clamped to [0.5, 1.5] so one unusual week cannot project double the month. "This neighbourhood orders more X" is lift, the same measure the basket analysis already uses: how many times more that product leaves for that zone than it would if the zone changed nothing. A zone below the sample threshold still appears, flagged as thin rather than hidden — a new zone must not look like a nonexistent one.

Everything runs in pure Python. No NumPy, no scikit-learn. The container stays small, and every step can be shown to the shop owner in their own language — which is the point, because a forecast the owner cannot argue with is a forecast they will not act on.

---

## Architecture

```
FastAPI  ·  Pydantic v2  ·  Python 3.12
Storage  ·  Firestore in production, JSON file locally — one interface, chosen at boot
Secrets  ·  Secret Manager in production, local file in dev — same pattern, never beside the data
Frontend ·  Vanilla JS PWA, no build step, installable, offline shell
Auth     ·  Firebase Authentication, two custom claims: erp_admin (the shop) and courier (the street)
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
                     secret vault: Secret Manager in production, local file in dev
  services/
    erp.py           orders, inventory, expenses, channels, audit
    costing.py       cost dossier, simulator, brand-line performance
    forecast.py      revenue decomposition, purchase plan, projected P&L
    planning.py      newsvendor production plan, reorder points, ABC/XYZ
    delivery.py      couriers, shift settlement, reconciliation
    growth.py        opportunity detection with the arithmetic attached
    ifood.py         iFood activation by code, event polling, order ingestion
    catalog.py       editable option lists
    media.py         document and receipt library
  static/            the PWA
etl/                 spreadsheet ingestion: bronze → silver → gold
scripts/             importers and operational tooling
tests/               580 tests, no mocks of the storage layer
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
pytest                             # 580 tests, about six minutes
```

Tests exercise the services directly against a temporary JSON store. There are no mocks for storage, so concurrency, transactions and reversal logic are tested for real.

### Deploying

```bash
gcloud run deploy sdm --source . --region <region>
```

Set `DATA_MODE=firebase`, `AUTH_REQUIRED=true`, and the Firebase project variables. The container refuses to boot in production with authentication disabled — the settings validator enforces it rather than trusting a checklist.

Access is granted per role, and never by editing a list in the code:

```bash
python -m scripts.grant_erp_admin --email owner@shop.com     # the shop
python -m scripts.grant_courier   --email rider@shop.com     # the street
```

The courier script refuses an account with no matching courier record, because granting access to someone the system cannot resolve produces an authenticated person who cannot open a shift — and the error would surface on the pavement.

---

## Status

Running in production at one shop. 580 tests passing.

Known limits, stated plainly:

- **Delivery-platform imports carry no line items.** The Brazilian platform this shop uses exports order totals but not what was sold, so imported orders have unknown cost until the items arrive from somewhere. The system reports this as missing coverage rather than papering over it, and a second importer is ready for an item-level export: it matches products by name, attaches the lines to the orders already imported, prices them through the recipes, and leaves stock untouched — those ingredients left the shelf months ago, and deducting now would gouge today's balance. An order whose lines don't all match a registered product stays marked as unknown cost, because half a cost presented as a whole one is worse than no cost at all. The same export also has no timestamp and no customer, so hour-of-day and retention analysis cannot come from this source at all.
- **The forecast horizon is capped at 30 days.** Quarterly, half-year and annual projections of operating profit are not implemented yet. The historical P&L already scales across those windows.
- **Localisation.** The interface is Portuguese only. The domain model is currency- and language-agnostic; the strings are not yet extracted.

---

## Licence

[MIT](LICENSE). Use it, change it, ship it — commercially included. Keep the copyright notice, and understand it comes with no warranty.

If you run a shop and want help putting it to work, or you are building something adjacent and want to compare notes, get in touch.
