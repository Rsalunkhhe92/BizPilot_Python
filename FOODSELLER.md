# Food Seller module

Business type for vada pav and tea sellers, snack shops, fast-food stalls, street food vendors and
small restaurants: stock, purchases, daily consumption, recipes, sales, expenses, profit & loss,
reports, alerts and an AI assistant.

## Where things are

| Part | Files |
|---|---|
| Schema and starter data | `foodseller_service.py`, `migrations/2026_10_31_foodseller.sql` |
| Calculations (no database) | `foodseller_core.py` |
| Auth, transactions, idempotency, stock ledger engine | `foodseller_common.py` |
| Setup, masters, purchases, consumption, recipes, sales, expenses, payments | `foodseller_api.py` |
| Dashboard, P&L, reports, notifications | `foodseller_reports.py` |
| AI assistant and proposals | `foodseller_ai.py` |
| Mobile and web module | `../src/screens/foodseller/` (mounted from `App.tsx`) |
| Admin feature catalogue | `../admin-dashboard/src/App.tsx` (`food_*` keys) |

All endpoints are under `/api/foodseller` and need the bearer token from `/api/login`. The token decides
the business. Features the admin switched off are hidden in the app and refused by the API.

## Migration

`ensure_foodseller_tables()` in `app.py` provisions the schema at API startup, in its own transaction. It
creates `food_*` tables, two trigger functions and the `Food Seller` row in `business_types`. It does not alter
any existing table. **Starting the API applies it to whatever `DATABASE_URL` points to.** To apply by hand:

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/2026_10_31_foodseller.sql
```

Rollback (deletes all Food Seller data, back up first):

```powershell
pg_dump "$env:DATABASE_URL" -t 'food_*' -f food_backup.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/2026_10_31_foodseller_down.sql
```

The tables are `food_`-prefixed because `inventory_products` and `inventory_transactions` already belong to
the Fruit Seller module. There is no separate business table on this platform: `business_id` is the customer's
`userdetails.id`, and `user_id` is the login that recorded the row (the same today, since each customer has one login).

## Tests

```powershell
.venv\Scripts\python.exe -m unittest test_foodseller_core -v

# Creates tables and writes data, so it only runs against a database you name, never the one in .env:
$env:FOODSELLER_TEST_DATABASE_URL = "postgresql://user@127.0.0.1:5432/empty_test_db"
.venv\Scripts\python.exe -m unittest test_foodseller_api -v
```

## How the numbers work

**Stock.** `food_inventory_transactions` is the only source of truth. Stock is `SUM(quantity)` and stock value is
`SUM(value)`. Rows are never updated (a trigger enforces it); a void posts a correcting row. Nothing stores a running balance.

    Closing = Opening + Purchases + Adjust In - Consumption - Sales usage - Adjust Out - Wastage/Spoilage/Personal

**Valuation.** Moving average: value / quantity at the moment stock leaves. When stock is zero or negative it falls
back to the product's latest purchase price.

**No double deduction.** For one stock item on one day, what leaves stock is `max(manual preparation usage, recipe usage from sales)`.
They are two views of the same usage, so "5 KG used this morning" and "50 vada pav sold, needing 5 KG" deduct 5 KG, in either
order. `reconcile_usage` is the one place either touches stock, and it posts only the difference. A void re-runs it. Wastage,
spoilage and personal use are always deducted in addition.

This matching is per calendar day. A batch prepared today and sold tomorrow should be entered on the day it is sold (or left to
the recipe); otherwise tomorrow's sale deducts its recipe again.

**Profit.**

    Revenue       = posted sales after discount
    COGS          = cost of recipe/packaged items sold + manual preparation usage beyond recipes
    Gross profit  = Revenue - COGS
    Net profit    = Gross profit - counted expenses - wastage - spoilage + stock adjustments (net)

Purchases are never in profit; unused stock stays in closing stock. Personal use is the owner's drawing: shown, not a cost.
A line's cost is its frozen recipe quantity at that day's actual usage rate, so line costs add up to the P&L figure. A later
recipe edit never rewrites past sales.

**Gas and packaging.** An expense category has `counts_in_pnl`. Gas Refill and Packaging default to off, since their cost reaches
profit when used from stock. An owner who does not stock them turns it on for the category or the entry.

**Negative stock.** Refused with a list of shortages, unless the owner chooses "Save anyway" for that entry or turns on the setting.
A posting is one database transaction, so a refused sale leaves nothing behind. Stock rows are locked in id order per posting.

**Alerts** are derived from the data when alerts or the dashboard load, stored once per key so read state sticks. They are in-app;
the platform has no push service. A product nobody has stocked yet raises no alert.

## AI assistant

Uses Gemini (`GEMINI_API_KEY`), like the rest of the project. Each question sends a snapshot of this business's own figures. The model
can only propose: its output is validated against the business's own ids and stored in `food_ai_proposals` as Pending, and only
`POST /ai/proposals/<id>/confirm`, from the owner's tap, runs the ordinary posting code. Proposals expire after 3 days. Without a key, or if
Gemini fails, common questions are answered from the same snapshot with a notice.

## Web

`npx expo start --web` runs the whole app in a browser (`react-native-web` and `react-dom` were added for this). The module uses 2 / 3 / 4
stat columns by width and a centred column up to 1180 px. Other business types' screens were not reviewed for desktop layout.
