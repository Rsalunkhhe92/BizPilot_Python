"""
Food Seller - dashboard, profit & loss, stock movement, reports and notifications.
All figures are computed from the stock ledger and posted transactions on request;
nothing here stores a running total that could drift.
"""

import base64
import io
from datetime import datetime, timedelta

from flask import g, request

from foodseller_common import ApiError, IST, body, camel, date_range, foodseller_bp, int_id, load_settings, ok, parse_date, today_ist, tx
from foodseller_core import ZERO, compute_pnl, dec, split_usage_cost, stock_state, usage_target


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------
def usage_rows(cur, start, end):
    """One row per (day, stock item): manual preparation qty, recipe qty, and what left stock (qty, cost)."""
    cur.execute(
        """WITH led AS (
               SELECT transaction_date AS d, product_id AS p, -SUM(quantity) AS posted_qty, -SUM(value) AS cost
               FROM food_inventory_transactions
               WHERE business_id = %(b)s AND transaction_date BETWEEN %(s)s AND %(e)s AND txn_type IN ('CONSUMPTION', 'SALE_USAGE')
               GROUP BY 1, 2),
           man AS (
               SELECT i.transaction_date AS d, i.product_id AS p, SUM(i.stock_qty) AS manual
               FROM food_consumption_items i JOIN food_consumption_transactions h ON h.id = i.consumption_id
               WHERE i.business_id = %(b)s AND i.transaction_date BETWEEN %(s)s AND %(e)s AND i.purpose = 'PREPARATION' AND h.status = 'Posted'
               GROUP BY 1, 2),
           rec AS (
               SELECT u.transaction_date AS d, u.product_id AS p, SUM(u.required_qty) AS recipe
               FROM food_sales_item_usage u JOIN food_sales_transactions h ON h.id = u.sale_id
               WHERE u.business_id = %(b)s AND u.transaction_date BETWEEN %(s)s AND %(e)s AND h.status = 'Posted'
               GROUP BY 1, 2)
           SELECT d, p, COALESCE(posted_qty, 0) AS posted_qty, COALESCE(cost, 0) AS cost, COALESCE(manual, 0) AS manual, COALESCE(recipe, 0) AS recipe
           FROM led FULL JOIN man USING (d, p) FULL JOIN rec USING (d, p)""",
        {"b": g.business_id, "s": start, "e": end},
    )
    return cur.fetchall()


_PNL_FIELDS = ("revenue", "purchases", "recipe_cost", "unmatched_cost", "wastage", "spoilage", "personal",
               "adjustment_net", "expenses", "expenses_excluded", "discount", "sales_count")


def pnl_by_day(cur, start, end):
    days = {}

    def bucket(day):
        return days.setdefault(day, {f: ZERO for f in _PNL_FIELDS})

    params = {"b": g.business_id, "s": start, "e": end}
    cur.execute(
        """SELECT transaction_date AS d, SUM(total_amount) AS revenue, SUM(discount) AS discount, COUNT(*) AS n FROM food_sales_transactions
           WHERE business_id = %(b)s AND transaction_date BETWEEN %(s)s AND %(e)s AND status = 'Posted' GROUP BY 1""", params)
    for r in cur.fetchall():
        day = bucket(r["d"])
        day["revenue"], day["discount"], day["sales_count"] = r["revenue"], r["discount"], dec(r["n"])
    cur.execute(
        """SELECT transaction_date AS d, SUM(total_amount) AS t FROM food_purchase_transactions
           WHERE business_id = %(b)s AND transaction_date BETWEEN %(s)s AND %(e)s AND status = 'Posted' GROUP BY 1""", params)
    for r in cur.fetchall():
        bucket(r["d"])["purchases"] = r["t"]
    cur.execute(
        """SELECT transaction_date AS d, txn_type, SUM(value) AS v FROM food_inventory_transactions
           WHERE business_id = %(b)s AND transaction_date BETWEEN %(s)s AND %(e)s
             AND txn_type IN ('WASTAGE', 'SPOILAGE', 'PERSONAL', 'ADJUST_IN', 'ADJUST_OUT') GROUP BY 1, 2""", params)
    for r in cur.fetchall():
        day = bucket(r["d"])
        if r["txn_type"].startswith("ADJUST"):
            day["adjustment_net"] += r["v"]
        else:
            day[r["txn_type"].lower()] = -r["v"]
    cur.execute(
        """SELECT transaction_date AS d, COALESCE(SUM(amount) FILTER (WHERE counts_in_pnl), 0) AS counted,
                  COALESCE(SUM(amount) FILTER (WHERE NOT counts_in_pnl), 0) AS excluded
           FROM food_expense_transactions
           WHERE business_id = %(b)s AND transaction_date BETWEEN %(s)s AND %(e)s AND status = 'Posted' GROUP BY 1""", params)
    for r in cur.fetchall():
        day = bucket(r["d"])
        day["expenses"], day["expenses_excluded"] = r["counted"], r["excluded"]
    for r in usage_rows(cur, start, end):
        recipe_cost, unmatched = split_usage_cost(r["cost"], r["manual"], r["recipe"])
        day = bucket(r["d"])
        day["recipe_cost"] += recipe_cost
        day["unmatched_cost"] += unmatched
    return days


def finish_pnl(parts):
    out = dict(parts)
    out.update(compute_pnl(parts))
    out["consumption_cost"] = dec(parts["recipe_cost"]) + dec(parts["unmatched_cost"])
    return out


def pnl_total(cur, start, end):
    total = {f: ZERO for f in _PNL_FIELDS}
    for day in pnl_by_day(cur, start, end).values():
        for field in _PNL_FIELDS:
            total[field] += day[field]
    return finish_pnl(total)


def sold_lines(cur, start, end):
    """Posted sale lines with their real cost: each line's frozen recipe quantity at
    that day's actual usage rate, so line costs add up to the recipe cost in P&L."""
    rates = {}
    for r in usage_rows(cur, start, end):
        if r["posted_qty"] > 0:
            rates[(r["d"], r["p"])] = r["cost"] / usage_target(r["posted_qty"], r["recipe"])
    cur.execute(
        """SELECT i.id, i.sale_id, i.transaction_date, i.food_product_id, i.food_name, i.quantity, i.price, i.total, i.discount_share,
                  i.has_cost_source, f.category_id, s.customer_name, s.cash_amount, s.upi_amount, s.credit_amount
           FROM food_sales_items i JOIN food_sales_transactions s ON s.id = i.sale_id LEFT JOIN food_products f ON f.id = i.food_product_id
           WHERE i.business_id = %(b)s AND i.transaction_date BETWEEN %(s)s AND %(e)s AND s.status = 'Posted' ORDER BY i.transaction_date, i.sale_id, i.id""",
        {"b": g.business_id, "s": start, "e": end},
    )
    lines = cur.fetchall()
    cur.execute(
        """SELECT u.sale_item_id, u.product_id, u.required_qty, u.transaction_date FROM food_sales_item_usage u
           JOIN food_sales_transactions s ON s.id = u.sale_id
           WHERE u.business_id = %(b)s AND u.transaction_date BETWEEN %(s)s AND %(e)s AND s.status = 'Posted'""",
        {"b": g.business_id, "s": start, "e": end},
    )
    costs = {}
    for u in cur.fetchall():
        costs[u["sale_item_id"]] = costs.get(u["sale_item_id"], ZERO) + u["required_qty"] * rates.get((u["transaction_date"], u["product_id"]), ZERO)
    for line in lines:
        line["revenue"] = line["total"] - line["discount_share"]
        line["cost"] = costs.get(line["id"], ZERO)
        line["profit"] = line["revenue"] - line["cost"]
    return lines


def food_profitability(cur, start, end):
    grouped = {}
    for line in sold_lines(cur, start, end):
        row = grouped.setdefault(line["food_name"], {
            "food": line["food_name"], "food_product_id": line["food_product_id"], "category_id": line["category_id"],
            "quantity": ZERO, "revenue": ZERO, "cost": ZERO, "profit": ZERO, "cost_missing": False})
        for field in ("quantity", "revenue", "cost", "profit"):
            row[field] += line[field]
        row["cost_missing"] = row["cost_missing"] or not line["has_cost_source"]
    rows = sorted(grouped.values(), key=lambda r: r["profit"], reverse=True)
    for row in rows:
        row["margin"] = (row["profit"] * 100 / row["revenue"]) if row["revenue"] > 0 else ZERO
    return rows


def stock_rows(cur, start, end):
    """Closing = Opening + Purchases + Adjust In - Consumption - Sales usage - Adjust Out - Wastage etc.
    Every column comes from the ledger. Usage is split so the recipe share is 'sales usage'
    and only what goes beyond recipes is 'consumption', whichever was entered first."""
    cur.execute(
        """SELECT p.id, p.name, p.category_id, c.name AS category_name, u.name AS unit_name, p.status, p.min_stock_alert, p.purchase_price,
                  COALESCE(SUM(l.quantity) FILTER (WHERE l.transaction_date < %(s)s OR (l.txn_type = 'OPENING' AND l.transaction_date <= %(e)s)), 0) AS opening,
                  COALESCE(SUM(l.value) FILTER (WHERE l.transaction_date < %(s)s OR (l.txn_type = 'OPENING' AND l.transaction_date <= %(e)s)), 0) AS opening_value,
                  COALESCE(SUM(l.quantity) FILTER (WHERE l.txn_type = 'PURCHASE' AND l.transaction_date BETWEEN %(s)s AND %(e)s), 0) AS purchased,
                  COALESCE(SUM(l.quantity) FILTER (WHERE l.txn_type = 'ADJUST_IN' AND l.transaction_date BETWEEN %(s)s AND %(e)s), 0) AS adjust_in,
                  COALESCE(-SUM(l.quantity) FILTER (WHERE l.txn_type = 'ADJUST_OUT' AND l.transaction_date BETWEEN %(s)s AND %(e)s), 0) AS adjust_out,
                  COALESCE(-SUM(l.quantity) FILTER (WHERE l.txn_type IN ('CONSUMPTION', 'SALE_USAGE') AND l.transaction_date BETWEEN %(s)s AND %(e)s), 0) AS used,
                  COALESCE(-SUM(l.value) FILTER (WHERE l.txn_type IN ('CONSUMPTION', 'SALE_USAGE') AND l.transaction_date BETWEEN %(s)s AND %(e)s), 0) AS used_cost,
                  COALESCE(-SUM(l.quantity) FILTER (WHERE l.txn_type IN ('WASTAGE', 'SPOILAGE', 'PERSONAL') AND l.transaction_date BETWEEN %(s)s AND %(e)s), 0) AS lost,
                  COALESCE(-SUM(l.value) FILTER (WHERE l.txn_type IN ('WASTAGE', 'SPOILAGE', 'PERSONAL') AND l.transaction_date BETWEEN %(s)s AND %(e)s), 0) AS lost_cost,
                  COALESCE(SUM(l.quantity) FILTER (WHERE l.transaction_date <= %(e)s), 0) AS closing,
                  COALESCE(SUM(l.value) FILTER (WHERE l.transaction_date <= %(e)s), 0) AS closing_value,
                  COUNT(l.id) AS movements
           FROM food_inventory_products p
           JOIN food_units u ON u.id = p.unit_id
           LEFT JOIN food_product_categories c ON c.id = p.category_id
           LEFT JOIN food_inventory_transactions l ON l.product_id = p.id AND l.business_id = p.business_id
           WHERE p.business_id = %(b)s AND p.deleted_at IS NULL
           GROUP BY p.id, c.name, u.name ORDER BY p.name""",
        {"b": g.business_id, "s": start, "e": end},
    )
    rows = cur.fetchall()
    usage = {}
    for r in usage_rows(cur, start, end):
        entry = usage.setdefault(r["p"], {"manual": ZERO, "recipe": ZERO, "unmatched": ZERO})
        entry["manual"] += r["manual"]
        entry["recipe"] += r["recipe"]
        entry["unmatched"] += max(r["posted_qty"] - r["recipe"], ZERO)
    for row in rows:
        entry = usage.get(row["id"], {"manual": ZERO, "recipe": ZERO, "unmatched": ZERO})
        row["manual"], row["recipe"] = entry["manual"], entry["recipe"]
        row["consumption"] = entry["unmatched"]
        row["sales_usage"] = row["used"] - entry["unmatched"]
        row["available"] = row["opening"] + row["purchased"] + row["adjust_in"]
        row["state"] = stock_state(row["closing"], row["min_stock_alert"])
        row["needs_attention"] = row["state"] != "OK" and row["status"] == "Active" and row["movements"] > 0
    return rows


def cash_rows(cur, start, end):
    params = {"b": g.business_id, "s": start, "e": end}
    days = {}

    def bucket(day):
        return days.setdefault(day, {k: ZERO for k in ("sales_cash", "sales_upi", "sales_credit", "collected_cash", "collected_upi",
                                                       "paid_out_cash", "paid_out_upi")})

    cur.execute(
        """SELECT transaction_date AS d, SUM(cash_amount) AS c, SUM(upi_amount) AS u, SUM(credit_amount) AS cr FROM food_sales_transactions
           WHERE business_id = %(b)s AND transaction_date BETWEEN %(s)s AND %(e)s AND status = 'Posted' GROUP BY 1""", params)
    for r in cur.fetchall():
        day = bucket(r["d"])
        day["sales_cash"], day["sales_upi"], day["sales_credit"] = r["c"], r["u"], r["cr"]
    cur.execute(
        """SELECT transaction_date AS d, direction, payment_method AS m, SUM(amount) AS a FROM food_payments
           WHERE business_id = %(b)s AND transaction_date BETWEEN %(s)s AND %(e)s AND status = 'Posted' GROUP BY 1, 2, 3""", params)
    for r in cur.fetchall():
        prefix = "collected" if r["direction"] == "RECEIVED" else "paid_out"
        bucket(r["d"])[f"{prefix}_{r['m'].lower()}"] += r["a"]
    cur.execute(
        """SELECT transaction_date AS d, payment_method AS m, SUM(amount_paid) AS a FROM food_purchase_transactions
           WHERE business_id = %(b)s AND transaction_date BETWEEN %(s)s AND %(e)s AND status = 'Posted' AND payment_method IN ('Cash', 'UPI') GROUP BY 1, 2""", params)
    for r in cur.fetchall():
        bucket(r["d"])[f"paid_out_{r['m'].lower()}"] += r["a"]
    cur.execute(
        """SELECT transaction_date AS d, payment_method AS m, SUM(amount) AS a FROM food_expense_transactions
           WHERE business_id = %(b)s AND transaction_date BETWEEN %(s)s AND %(e)s AND status = 'Posted' AND payment_method IN ('Cash', 'UPI') GROUP BY 1, 2""", params)
    for r in cur.fetchall():
        bucket(r["d"])[f"paid_out_{r['m'].lower()}"] += r["a"]
    return days


def pending_totals(cur):
    cur.execute(
        """SELECT COALESCE((SELECT SUM(credit_amount) FROM food_sales_transactions WHERE business_id = %(b)s AND status = 'Posted'), 0)
                - COALESCE((SELECT SUM(amount) FROM food_payments WHERE business_id = %(b)s AND status = 'Posted' AND direction = 'RECEIVED'), 0) AS receivable,
                  COALESCE((SELECT SUM(total_amount - amount_paid) FROM food_purchase_transactions WHERE business_id = %(b)s AND status = 'Posted'), 0)
                - COALESCE((SELECT SUM(amount) FROM food_payments WHERE business_id = %(b)s AND status = 'Posted' AND direction = 'PAID'), 0) AS payable""",
        {"b": g.business_id},
    )
    return cur.fetchone()


# ---------------------------------------------------------------------------
# Dashboard and P&L
# ---------------------------------------------------------------------------
@foodseller_bp.get("/dashboard")
def dashboard():
    on_date = parse_date(request.args.get("date"), default=today_ist())
    with tx() as cur:
        day = pnl_total(cur, on_date, on_date)
        month = pnl_total(cur, on_date.replace(day=1), on_date)
        stock = stock_rows(cur, on_date, on_date)
        cash = cash_rows(cur, on_date, on_date).get(on_date, {})
        pending = pending_totals(cur)
        foods = food_profitability(cur, on_date, on_date)
        series_start = on_date - timedelta(days=6)
        series = pnl_by_day(cur, series_start, on_date)
        refresh_notifications(cur)
        cur.execute("SELECT COUNT(*) AS n FROM food_notifications WHERE business_id = %s AND NOT is_read", (g.business_id,))
        unread = cur.fetchone()["n"]
    trend = []
    for offset in range(7):
        current = series_start + timedelta(days=offset)
        parts = series.get(current)
        figures = finish_pnl(parts) if parts else None
        trend.append({"date": current, "revenue": figures["revenue"] if figures else 0, "netProfit": figures["net_profit"] if figures else 0})
    attention = [r for r in stock if r["needs_attention"]]
    return ok({
        "date": on_date,
        "today": camel(day),
        "monthNetProfit": month["net_profit"],
        "monthRevenue": month["revenue"],
        "openingStockValue": sum((r["opening_value"] for r in stock), ZERO),
        "remainingStockValue": sum((r["closing_value"] for r in stock), ZERO),
        "cashReceived": dec(cash.get("sales_cash", 0)) + dec(cash.get("collected_cash", 0)),
        "upiReceived": dec(cash.get("sales_upi", 0)) + dec(cash.get("collected_upi", 0)),
        "pendingReceivable": pending["receivable"],
        "pendingPayable": pending["payable"],
        "productCount": len(stock),
        "attention": [{"id": r["id"], "name": r["name"], "state": r["state"], "stockQty": r["closing"], "unit": r["unit_name"], "minStockAlert": r["min_stock_alert"]}
                      for r in attention[:12]],
        "attentionCount": len(attention),
        "topFoods": [camel(r) for r in foods[:5]],
        "trend": trend,
        "unreadNotifications": unread,
    })


@foodseller_bp.get("/pnl")
def pnl():
    start, end = date_range()
    with tx() as cur:
        total = pnl_total(cur, start, end)
        foods = food_profitability(cur, start, end)
        lines = sold_lines(cur, start, end)
    sales = {}
    for line in lines:
        row = sales.setdefault(line["sale_id"], {"sale_id": line["sale_id"], "date": line["transaction_date"], "customer": line["customer_name"],
                                                 "items": [], "revenue": ZERO, "cost": ZERO, "profit": ZERO})
        row["items"].append(f"{line['food_name']} x {dec(line['quantity']).normalize():f}")
        for field in ("revenue", "cost", "profit"):
            row[field] += line[field]
    return ok({"from": start, "to": end, "pnl": camel(total), "foods": [camel(r) for r in foods],
               "sales": [camel(r) for r in sorted(sales.values(), key=lambda s: s["sale_id"], reverse=True)][:100]})


@foodseller_bp.get("/consumption/summary")
def consumption_summary():
    """Opening/Purchased | Used | Remaining for one day, live from the ledger."""
    on_date = parse_date(request.args.get("date"), default=today_ist())
    with tx() as cur:
        rows = stock_rows(cur, on_date, on_date)
    moved = [r for r in rows if r["status"] == "Active" and (r["available"] != 0 or r["used"] != 0 or r["lost"] != 0 or r["closing"] != 0)]
    return ok({"date": on_date, "rows": [camel(r) for r in moved],
               "totalCost": sum((r["used_cost"] for r in rows), ZERO), "totalLostCost": sum((r["lost_cost"] for r in rows), ZERO)})


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def _col(key, label, kind="text"):
    return {"key": key, "label": label, "type": kind}


def _rep_daily_purchase(cur, start, end):
    cur.execute(
        """SELECT h.transaction_date AS date, COALESCE(s.name, '-') AS supplier, h.id AS bill, p.name AS product, c.name AS category,
                  i.entered_qty AS quantity, u.name AS unit, i.rate, i.total, i.product_id AS _product, p.category_id AS _category
           FROM food_purchase_items i JOIN food_purchase_transactions h ON h.id = i.purchase_id
           JOIN food_inventory_products p ON p.id = i.product_id JOIN food_units u ON u.id = i.unit_id
           LEFT JOIN food_product_categories c ON c.id = p.category_id LEFT JOIN food_suppliers s ON s.id = h.supplier_id
           WHERE i.business_id = %s AND i.transaction_date BETWEEN %s AND %s AND h.status = 'Posted'
           ORDER BY h.transaction_date DESC, h.id DESC, i.id""", (g.business_id, start, end))
    return [_col("date", "Date", "date"), _col("product", "Product"), _col("category", "Category"), _col("supplier", "Supplier"),
            _col("quantity", "Quantity", "number"), _col("unit", "Unit"), _col("rate", "Rate", "money"), _col("total", "Total", "money")], cur.fetchall(), ["total"]


def _rep_daily_sales(cur, start, end):
    rows = [{"date": l["transaction_date"], "bill": l["sale_id"], "food": l["food_name"], "quantity": l["quantity"], "price": l["price"],
             "gross": l["total"], "discount": l["discount_share"], "net": l["revenue"], "cost": l["cost"], "profit": l["profit"],
             "_food": l["food_product_id"], "_category": l["category_id"]} for l in sold_lines(cur, start, end)]
    rows.reverse()
    return [_col("date", "Date", "date"), _col("food", "Food product"), _col("bill", "Bill no", "number"), _col("quantity", "Qty sold", "number"), _col("price", "Price", "money"),
            _col("gross", "Gross", "money"), _col("discount", "Discount", "money"), _col("net", "Net sale", "money"), _col("cost", "Cost", "money"),
            _col("profit", "Profit", "money")], rows, ["quantity", "gross", "discount", "net", "cost", "profit"]


def _product_lookup(cur):
    cur.execute(
        """SELECT p.id, p.name, p.category_id, c.name AS category, u.name AS unit FROM food_inventory_products p
           JOIN food_units u ON u.id = p.unit_id LEFT JOIN food_product_categories c ON c.id = p.category_id WHERE p.business_id = %s""",
        (g.business_id,),
    )
    return {r["id"]: r for r in cur.fetchall()}


def _rep_daily_consumption(cur, start, end):
    products = _product_lookup(cur)
    rows = []
    for r in usage_rows(cur, start, end):
        product = products[r["p"]]
        rows.append({"date": r["d"], "product": product["name"], "category": product["category"], "unit": product["unit"], "manual": r["manual"],
                     "recipe": r["recipe"], "not_double": min(r["manual"], r["recipe"]), "deducted": r["posted_qty"], "cost": r["cost"],
                     "_product": r["p"], "_category": product["category_id"]})
    rows.sort(key=lambda x: (x["date"], x["product"]), reverse=True)
    return [_col("date", "Date", "date"), _col("product", "Product"), _col("category", "Category"), _col("unit", "Unit"), _col("manual", "Manual usage", "number"),
            _col("recipe", "Recipe usage (sales)", "number"), _col("not_double", "Overlap not deducted twice", "number"), _col("deducted", "Deducted from stock", "number"),
            _col("cost", "Consumption cost", "money")], rows, ["cost"]


def _stock_columns():
    return [_col("name", "Product"), _col("category_name", "Category"), _col("unit_name", "Unit"), _col("opening", "Opening", "number"), _col("purchased", "Purchased", "number"),
            _col("adjust_in", "Adjust in", "number"), _col("consumption", "Consumption", "number"), _col("sales_usage", "Sales usage", "number"), _col("adjust_out", "Adjust out", "number"),
            _col("lost", "Wastage / spoilage / personal", "number"), _col("closing", "Closing", "number"), _col("closing_value", "Closing value", "money")]


def _rep_stock(cur, start, end):
    rows = stock_rows(cur, start, end)
    for r in rows:
        r["_product"], r["_category"] = r["id"], r["category_id"]
    return _stock_columns(), rows, ["closing_value"]


def _rep_low_stock(cur, start, end):
    rows = [r for r in stock_rows(cur, end, end) if r["needs_attention"]]
    for r in rows:
        r["_product"], r["_category"] = r["id"], r["category_id"]
        r["state_label"] = "Out of stock" if r["state"] == "OUT" else "Low stock"
    return [_col("name", "Product"), _col("category_name", "Category"), _col("state_label", "State"), _col("closing", "In stock", "number"), _col("unit_name", "Unit"),
            _col("min_stock_alert", "Alert level", "number")], rows, []


def _rep_wastage(cur, start, end):
    cur.execute(
        """SELECT l.transaction_date AS date, p.name AS product, c.name AS category, l.txn_type AS kind, -SUM(l.quantity) AS quantity, u.name AS unit, -SUM(l.value) AS cost,
                  l.product_id AS _product, p.category_id AS _category
           FROM food_inventory_transactions l JOIN food_inventory_products p ON p.id = l.product_id JOIN food_units u ON u.id = p.unit_id
           LEFT JOIN food_product_categories c ON c.id = p.category_id
           WHERE l.business_id = %s AND l.transaction_date BETWEEN %s AND %s AND l.txn_type IN ('WASTAGE', 'SPOILAGE', 'PERSONAL')
           GROUP BY l.transaction_date, p.name, c.name, l.txn_type, u.name, l.product_id, p.category_id
           HAVING SUM(l.quantity) <> 0 ORDER BY 1 DESC, 2""", (g.business_id, start, end))
    return [_col("date", "Date", "date"), _col("product", "Product"), _col("category", "Category"), _col("kind", "Type"), _col("quantity", "Quantity", "number"), _col("unit", "Unit"),
            _col("cost", "Cost", "money")], cur.fetchall(), ["cost"]


def _rep_supplier(cur, start, end):
    cur.execute(
        """SELECT COALESCE(s.name, 'No supplier') AS supplier, COUNT(*) AS bills, SUM(h.total_amount) AS total, SUM(h.amount_paid) + COALESCE(SUM(pay.paid), 0) AS paid,
                  SUM(h.total_amount) - SUM(h.amount_paid) - COALESCE(SUM(pay.paid), 0) AS due, MAX(h.transaction_date) AS last_date
           FROM food_purchase_transactions h LEFT JOIN food_suppliers s ON s.id = h.supplier_id
           LEFT JOIN (SELECT purchase_id, SUM(amount) AS paid FROM food_payments WHERE status = 'Posted' AND direction = 'PAID' GROUP BY purchase_id) pay ON pay.purchase_id = h.id
           WHERE h.business_id = %s AND h.transaction_date BETWEEN %s AND %s AND h.status = 'Posted'
           GROUP BY s.name ORDER BY total DESC""", (g.business_id, start, end))
    return [_col("supplier", "Supplier"), _col("bills", "Bills", "number"), _col("total", "Purchased", "money"), _col("paid", "Paid", "money"), _col("due", "Due", "money"),
            _col("last_date", "Last purchase", "date")], cur.fetchall(), ["total", "paid", "due"]


_PNL_COLS = [_col("revenue", "Revenue", "money"), _col("purchases", "Purchases (not in profit)", "money"), _col("cogs", "Cost of goods sold", "money"), _col("gross_profit", "Gross profit", "money"),
             _col("expenses", "Other expenses", "money"), _col("wastage", "Wastage", "money"), _col("spoilage", "Spoilage", "money"), _col("adjustment_net", "Stock adjustments", "money"),
             _col("net_profit", "Net profit", "money"), _col("personal", "Personal use (not in profit)", "money")]
_PNL_TOTALS = [c["key"] for c in _PNL_COLS]


def _rep_daily_pnl(cur, start, end):
    days = pnl_by_day(cur, start, end)
    rows = [{"date": d, **finish_pnl(days[d])} for d in sorted(days, reverse=True)]
    return [_col("date", "Date", "date"), *_PNL_COLS], rows, _PNL_TOTALS


def _rep_monthly_pnl(cur, start, end):
    months = {}
    for day, parts in pnl_by_day(cur, start, end).items():
        bucket = months.setdefault(day.strftime("%Y-%m"), {f: ZERO for f in _PNL_FIELDS})
        for field in _PNL_FIELDS:
            bucket[field] += parts[field]
    rows = [{"month": m, **finish_pnl(months[m])} for m in sorted(months, reverse=True)]
    return [_col("month", "Month"), *_PNL_COLS], rows, _PNL_TOTALS


def _rep_food_profit(cur, start, end):
    rows = food_profitability(cur, start, end)
    for r in rows:
        r["_food"], r["_category"] = r["food_product_id"], r["category_id"]
        r["note"] = "No recipe: cost unknown" if r["cost_missing"] else ""
    return [_col("food", "Food product"), _col("quantity", "Qty sold", "number"), _col("revenue", "Revenue", "money"), _col("cost", "Cost", "money"), _col("profit", "Profit", "money"),
            _col("margin", "Margin %", "number"), _col("note", "Note")], rows, ["quantity", "revenue", "cost", "profit"]


def _rep_sale_profit(cur, start, end):
    sales = {}
    for line in sold_lines(cur, start, end):
        row = sales.setdefault(line["sale_id"], {"bill": line["sale_id"], "date": line["transaction_date"], "customer": line["customer_name"] or "", "items": [],
                                                 "revenue": ZERO, "cost": ZERO, "profit": ZERO})
        row["items"].append(f"{line['food_name']} x {dec(line['quantity']).normalize():f}")
        for field in ("revenue", "cost", "profit"):
            row[field] += line[field]
    rows = sorted(sales.values(), key=lambda s: s["bill"], reverse=True)
    for r in rows:
        r["items"] = ", ".join(r["items"])
    return [_col("date", "Date", "date"), _col("bill", "Bill no", "number"), _col("items", "Items"), _col("customer", "Customer"), _col("revenue", "Revenue", "money"),
            _col("cost", "Cost", "money"), _col("profit", "Profit", "money")], rows, ["revenue", "cost", "profit"]


def _rep_cash_upi(cur, start, end):
    days = cash_rows(cur, start, end)
    rows = []
    for d in sorted(days, reverse=True):
        v = days[d]
        rows.append({"date": d, **v, "cash_in": v["sales_cash"] + v["collected_cash"], "upi_in": v["sales_upi"] + v["collected_upi"]})
    return [_col("date", "Date", "date"), _col("sales_cash", "Cash sales", "money"), _col("sales_upi", "UPI sales", "money"), _col("sales_credit", "Credit sales", "money"),
            _col("collected_cash", "Credit collected (cash)", "money"), _col("collected_upi", "Credit collected (UPI)", "money"), _col("cash_in", "Total cash in", "money"),
            _col("upi_in", "Total UPI in", "money"), _col("paid_out_cash", "Cash paid out", "money"), _col("paid_out_upi", "UPI paid out", "money")], rows, \
        ["sales_cash", "sales_upi", "sales_credit", "collected_cash", "collected_upi", "cash_in", "upi_in", "paid_out_cash", "paid_out_upi"]


REPORTS = {
    "daily_purchase": ("Daily Purchase Report", "Purchases", _rep_daily_purchase, "product"),
    "supplier_purchase": ("Supplier-wise Purchase Report", "Purchases", _rep_supplier, None),
    "daily_sales": ("Daily Sales Report", "Sales", _rep_daily_sales, "food"),
    "food_profit": ("Food Product Profitability Report", "Sales", _rep_food_profit, "food"),
    "sale_profit": ("Sales-wise Profit Report", "Sales", _rep_sale_profit, None),
    "cash_upi": ("Cash vs UPI Report", "Sales", _rep_cash_upi, None),
    "daily_consumption": ("Daily Consumption Report", "Stock", _rep_daily_consumption, "product"),
    "stock": ("Product-wise Stock Report", "Stock", _rep_stock, "product"),
    "low_stock": ("Low Stock Report", "Stock", _rep_low_stock, "product"),
    "wastage": ("Wastage Report", "Stock", _rep_wastage, "product"),
    "daily_pnl": ("Daily Profit & Loss Report", "Profit", _rep_daily_pnl, None),
    "monthly_pnl": ("Monthly Profit & Loss Report", "Profit", _rep_monthly_pnl, None),
}


@foodseller_bp.get("/reports")
def report_catalog():
    return ok({"reports": [{"key": k, "title": v[0], "group": v[1], "filter": v[3]} for k, v in REPORTS.items()]})


def _xlsx(title, meta, columns, rows, totals):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        raise ApiError(503, "Excel export is not available on this server (openpyxl is not installed); use PDF")

    def safe(value):
        # A text cell starting with one of these would run as a formula when opened in Excel.
        if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
            return "'" + value
        return float(value) if hasattr(value, "as_tuple") else value

    wb = Workbook()
    ws = wb.active
    ws.title = "".join(ch for ch in title if ch not in "[]:*?/\\")[:31]
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([meta])
    ws.append([])
    ws.append([c["label"] for c in columns])
    for cell in ws[4]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append([safe(row.get(c["key"])) for c in columns])
    if totals:
        ws.append(["Total" if i == 0 else safe(totals.get(c["key"])) for i, c in enumerate(columns)])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
    for i, column in enumerate(columns, start=1):
        ws.column_dimensions[ws.cell(row=4, column=i).column_letter].width = max(12, min(len(column["label"]) + 4, 32))
    buffer = io.BytesIO()
    wb.save(buffer)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@foodseller_bp.get("/reports/<key>")
def run_report(key):
    spec = REPORTS.get(key)
    if not spec:
        raise ApiError(404, "Report not found")
    start, end = date_range()
    product_id = int_id(request.args.get("productId"), "Product", required=False)
    food_id = int_id(request.args.get("foodId"), "Food product", required=False)
    category_id = int_id(request.args.get("categoryId"), "Category", required=False)
    with tx() as cur:
        columns, rows, total_keys = spec[2](cur, start, end)
    rows = [dict(r) for r in rows]
    if product_id:
        rows = [r for r in rows if r.get("_product") == product_id]
    if food_id:
        rows = [r for r in rows if r.get("_food") == food_id]
    if category_id:
        rows = [r for r in rows if r.get("_category") == category_id]
    rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    totals = {k: sum((dec(r.get(k) or 0) for r in rows), ZERO) for k in total_keys} if rows else {}
    meta = f"{start.isoformat()} to {end.isoformat()}"
    if request.args.get("format") == "xlsx":
        return ok({"fileName": f"{key}_{start.isoformat()}_{end.isoformat()}.xlsx", "fileBase64": _xlsx(spec[0], meta, columns, rows, totals)})
    return ok({"key": key, "title": spec[0], "meta": meta, "columns": columns, "rows": rows, "totals": totals})


# ---------------------------------------------------------------------------
# Notifications: derived from the data, stored once per key so read state sticks.
# ---------------------------------------------------------------------------
def _notify(cur, key, kind, severity, message, params):
    import json
    cur.execute(
        """INSERT INTO food_notifications (business_id, user_id, transaction_date, dedupe_key, type, severity, message, params)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb) ON CONFLICT (business_id, dedupe_key) DO NOTHING""",
        (g.business_id, g.user_id, today_ist(), key[:120], kind, severity, message[:400], json.dumps(params, default=str)),
    )


def refresh_notifications(cur):
    today = today_ist()
    settings = load_settings(cur) or {"excess_ratio": dec("1.5"), "unusual_expense_ratio": dec(2)}

    # Stock state: an alert that no longer applies (restocked) is removed unless already read.
    rows = stock_rows(cur, today, today)
    live = []
    for r in rows:
        if not r["needs_attention"]:
            continue
        kind = "out_of_stock" if r["state"] == "OUT" else "low_stock"
        key = f"{kind}:{r['id']}:{today.isoformat()}"
        live.append(key)
        qty = f"{dec(r['closing']).normalize():f}"
        message = f"{r['name']} is out of stock" if r["state"] == "OUT" else f"{r['name']} is low: {qty} {r['unit_name']} left"
        _notify(cur, key, kind, "critical" if r["state"] == "OUT" else "warning", message, {"name": r["name"], "qty": qty, "unit": r["unit_name"]})
    cur.execute(
        "DELETE FROM food_notifications WHERE business_id = %s AND type IN ('low_stock', 'out_of_stock') AND NOT is_read AND NOT (dedupe_key = ANY(%s))",
        (g.business_id, live),
    )

    # Excessive consumption: today's manual usage well above the usual day.
    cur.execute(
        """SELECT i.product_id, i.transaction_date = %(t)s AS is_today, SUM(i.stock_qty) AS qty
           FROM food_consumption_items i JOIN food_consumption_transactions h ON h.id = i.consumption_id
           WHERE i.business_id = %(b)s AND i.transaction_date BETWEEN %(from)s AND %(t)s AND i.purpose = 'PREPARATION' AND h.status = 'Posted'
           GROUP BY i.product_id, i.transaction_date""",
        {"b": g.business_id, "t": today, "from": today - timedelta(days=14)},
    )
    history, today_usage = {}, {}
    for r in cur.fetchall():
        if r["is_today"]:
            today_usage[r["product_id"]] = r["qty"]
        else:
            history.setdefault(r["product_id"], []).append(r["qty"])
    names = {r["id"]: r for r in rows}
    for product_id, qty in today_usage.items():
        past = history.get(product_id, [])
        if len(past) >= 3:
            usual = sum(past, ZERO) / len(past)
            if qty > usual * settings["excess_ratio"] and product_id in names:
                product = names[product_id]
                _notify(cur, f"excess:{product_id}:{today.isoformat()}", "excess_consumption", "warning",
                        f"{product['name']}: used {qty.normalize():f} {product['unit_name']} today, usually about {usual:.2f}",
                        {"name": product["name"], "qty": f"{qty.normalize():f}", "unit": product["unit_name"], "usual": f"{usual:.2f}"})

    # Unusual expense: well above this category's usual entry.
    cur.execute(
        """SELECT e.id, e.amount, c.name, stats.avg_amount
           FROM food_expense_transactions e JOIN food_expense_categories c ON c.id = e.category_id
           JOIN LATERAL (SELECT AVG(p.amount) AS avg_amount, COUNT(*) AS n FROM food_expense_transactions p
                         WHERE p.business_id = e.business_id AND p.category_id = e.category_id AND p.status = 'Posted'
                           AND p.transaction_date BETWEEN %(t)s::date - 60 AND %(t)s::date - 1) stats ON TRUE
           WHERE e.business_id = %(b)s AND e.transaction_date = %(t)s AND e.status = 'Posted' AND stats.n >= 3 AND e.amount > stats.avg_amount * %(r)s""",
        {"b": g.business_id, "t": today, "r": settings["unusual_expense_ratio"]},
    )
    for r in cur.fetchall():
        _notify(cur, f"expense:{r['id']}", "unusual_expense", "warning", f"{r['name']} expense of ₹{r['amount']:.0f} is unusually high (usually about ₹{r['avg_amount']:.0f})",
                {"name": r["name"], "amount": f"{r['amount']:.0f}", "usual": f"{r['avg_amount']:.0f}"})

    # Pending payments, once a day.
    pending = pending_totals(cur)
    if pending["receivable"] > 0 or pending["payable"] > 0:
        _notify(cur, f"pending:{today.isoformat()}", "pending_payments", "info",
                f"Pending: ₹{pending['receivable']:.0f} to collect, ₹{pending['payable']:.0f} to pay suppliers",
                {"receivable": f"{pending['receivable']:.0f}", "payable": f"{pending['payable']:.0f}"})

    # Sales and profit summaries: yesterday's, and today's once the evening is here.
    summary_days = [today - timedelta(days=1)]
    if datetime.now(IST).hour >= 20:
        summary_days.append(today)
    for day in summary_days:
        figures = pnl_total(cur, day, day)
        if figures["revenue"] > 0:
            _notify(cur, f"sales_summary:{day.isoformat()}", "sales_summary", "info", f"{day.isoformat()}: sales ₹{figures['revenue']:.0f} from {figures['sales_count']:.0f} bills",
                    {"date": day.isoformat(), "revenue": f"{figures['revenue']:.0f}", "bills": f"{figures['sales_count']:.0f}"})
            _notify(cur, f"profit_summary:{day.isoformat()}", "profit_summary", "info", f"{day.isoformat()}: net profit ₹{figures['net_profit']:.0f}",
                    {"date": day.isoformat(), "net": f"{figures['net_profit']:.0f}"})


@foodseller_bp.get("/notifications")
def list_notifications():
    with tx() as cur:
        refresh_notifications(cur)
        cur.execute(
            "SELECT id, type, severity, message, params, is_read, created_at FROM food_notifications WHERE business_id = %s ORDER BY is_read, created_at DESC, id DESC LIMIT 100",
            (g.business_id,),
        )
        return ok({"notifications": [camel(r) for r in cur.fetchall()]})


@foodseller_bp.post("/notifications/read")
def read_notifications():
    payload = body()
    with tx() as cur:
        if payload.get("id"):
            cur.execute("UPDATE food_notifications SET is_read = TRUE WHERE business_id = %s AND id = %s", (g.business_id, int_id(payload["id"])))
        else:
            cur.execute("UPDATE food_notifications SET is_read = TRUE WHERE business_id = %s AND NOT is_read", (g.business_id,))
    return ok({"read": True})
