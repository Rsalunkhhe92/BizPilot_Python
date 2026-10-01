"""
Inventory Business REST API Endpoints (Flask Blueprint)
Mounted at /api/inventory
For Fruit Sellers / other product-based business types: a product catalog
plus daily per-product transactions with daily/weekly/monthly filtering.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import os

from flask import Blueprint, jsonify, request
import psycopg

inventory_bp = Blueprint("inventory", __name__, url_prefix="/api/inventory")


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(database_url, options="-c timezone=Asia/Kolkata")


def get_owner_id(payload=None):
    """Resolve the logged-in business owner's userdetails.id from query/body/header."""
    candidates = [
        request.args.get("owner_id"),
        request.args.get("ownerId"),
        (payload or {}).get("ownerId") if payload else None,
        (payload or {}).get("owner_id") if payload else None,
        request.headers.get("X-User-Id"),
    ]
    for c in candidates:
        if c not in (None, ""):
            try:
                return int(c)
            except (TypeError, ValueError):
                continue
    return None


def to_decimal(value, default="0"):
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal(default)


def parse_date(date_str):
    if not date_str:
        return date.today()
    try:
        return date.fromisoformat(str(date_str).strip())
    except ValueError:
        return date.today()


def serialize_product(row):
    (pid, name, category, unit, stock_qty, selling_price, cost_price,
     low_stock_threshold, status, created_at, updated_at) = row
    return {
        "id": str(pid),
        "name": name,
        "category": category,
        "unit": unit,
        "stockQty": float(stock_qty or 0),
        "sellingPrice": float(selling_price or 0),
        "costPrice": float(cost_price or 0),
        "lowStockThreshold": float(low_stock_threshold or 0),
        "status": status,
    }


def serialize_transaction(row):
    (tid, product_id, product_name, quantity, unit, amount, transaction_type, payment_method,
     transaction_date, note, created_at) = row
    return {
        "id": str(tid),
        "productId": str(product_id) if product_id is not None else None,
        "productName": product_name,
        "quantity": float(quantity or 0),
        "unit": unit,
        "amount": float(amount or 0),
        "type": transaction_type or "SALE",
        "paymentMethod": payment_method or "CASH",
        "date": transaction_date.isoformat() if transaction_date else None,
        "note": note or "",
        "createdAt": created_at.isoformat() if created_at else None,
    }


@inventory_bp.get("/products")
def list_products():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, category, unit, stock_qty, selling_price, cost_price,
                           low_stock_threshold, status, created_at, updated_at
                    FROM inventory_products
                    WHERE owner_id = %s AND status = 'active'
                    ORDER BY name ASC
                    """,
                    (owner_id,),
                )
                products = [serialize_product(row) for row in cur.fetchall()]
        return jsonify({"products": products})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.post("/products")
def create_product():
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    name = str(payload.get("name", "")).strip()
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not name:
        return jsonify({"error": "Product name is required"}), 400

    category = str(payload.get("category", "General")).strip() or "General"
    unit = str(payload.get("unit", "kg")).strip() or "kg"
    stock_qty = to_decimal(payload.get("stockQty"))
    selling_price = to_decimal(payload.get("sellingPrice"))
    cost_price = to_decimal(payload.get("costPrice"))
    low_stock_threshold = to_decimal(payload.get("lowStockThreshold"))

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO inventory_products
                        (owner_id, name, category, unit, stock_qty, selling_price, cost_price, low_stock_threshold)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, name, category, unit, stock_qty, selling_price, cost_price,
                              low_stock_threshold, status, created_at, updated_at
                    """,
                    (owner_id, name, category, unit, stock_qty, selling_price, cost_price, low_stock_threshold),
                )
                product = serialize_product(cur.fetchone())
                conn.commit()
        return jsonify({"product": product}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.put("/products/<int:product_id>")
def update_product(product_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    fields = []
    values = []
    if "name" in payload:
        fields.append("name = %s")
        values.append(str(payload.get("name", "")).strip())
    if "category" in payload:
        fields.append("category = %s")
        values.append(str(payload.get("category", "")).strip() or "General")
    if "unit" in payload:
        fields.append("unit = %s")
        values.append(str(payload.get("unit", "")).strip() or "kg")
    if "stockQty" in payload:
        fields.append("stock_qty = %s")
        values.append(to_decimal(payload.get("stockQty")))
    if "sellingPrice" in payload:
        fields.append("selling_price = %s")
        values.append(to_decimal(payload.get("sellingPrice")))
    if "costPrice" in payload:
        fields.append("cost_price = %s")
        values.append(to_decimal(payload.get("costPrice")))
    if "lowStockThreshold" in payload:
        fields.append("low_stock_threshold = %s")
        values.append(to_decimal(payload.get("lowStockThreshold")))

    if not fields:
        return jsonify({"error": "No fields to update"}), 400

    fields.append("updated_at = NOW()")
    values.extend([product_id, owner_id])

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE inventory_products SET {', '.join(fields)}
                    WHERE id = %s AND owner_id = %s
                    RETURNING id, name, category, unit, stock_qty, selling_price, cost_price,
                              low_stock_threshold, status, created_at, updated_at
                    """,
                    values,
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Product not found"}), 404
                product = serialize_product(row)
                conn.commit()
        return jsonify({"product": product})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.delete("/products/<int:product_id>")
def delete_product(product_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE inventory_products SET status = 'inactive', updated_at = NOW() WHERE id = %s AND owner_id = %s RETURNING id",
                    (product_id, owner_id),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Product not found"}), 404
                conn.commit()
        return jsonify({"message": "Product removed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.post("/transactions")
def create_transaction():
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    product_id = payload.get("productId") or payload.get("product_id")
    amount = to_decimal(payload.get("amount"))
    quantity = to_decimal(payload.get("quantity"))
    tx_type = str(payload.get("type", "SALE")).strip().upper()
    if tx_type not in ("SALE", "WASTAGE", "PURCHASE"):
        tx_type = "SALE"
    payment_method = str(payload.get("paymentMethod", "CASH")).strip().upper()
    if payment_method not in ("CASH", "UPI", "CREDIT"):
        payment_method = "CASH"

    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not product_id:
        return jsonify({"error": "productId is required"}), 400
    if tx_type == "WASTAGE":
        if quantity <= 0:
            return jsonify({"error": "quantity is required for wastage entries"}), 400
    elif tx_type == "PURCHASE":
        if quantity <= 0:
            return jsonify({"error": "quantity is required for purchase entries"}), 400
        if amount <= 0:
            return jsonify({"error": "amount paid is required for purchase entries"}), 400
    elif amount <= 0:
        return jsonify({"error": "amount must be greater than 0"}), 400

    tx_date = parse_date(payload.get("date"))
    note = str(payload.get("note", "")).strip()

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, unit, stock_qty, cost_price FROM inventory_products WHERE id = %s AND owner_id = %s",
                    (product_id, owner_id),
                )
                product_row = cur.fetchone()
                if not product_row:
                    return jsonify({"error": "Product not found"}), 404
                _, product_name, product_unit, stock_qty, cost_price = product_row

                if tx_type == "WASTAGE" and amount <= 0:
                    amount = (quantity * cost_price) if cost_price else Decimal("0")

                cur.execute(
                    """
                    INSERT INTO inventory_transactions
                        (owner_id, product_id, product_name, quantity, unit, amount, transaction_type, payment_method, transaction_date, note)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, product_id, product_name, quantity, unit, amount, transaction_type, payment_method, transaction_date, note, created_at
                    """,
                    (owner_id, product_id, product_name, quantity, product_unit, amount, tx_type, payment_method, tx_date, note),
                )
                transaction = serialize_transaction(cur.fetchone())

                if quantity > 0:
                    if tx_type == "PURCHASE":
                        cur.execute(
                            "UPDATE inventory_products SET stock_qty = stock_qty + %s, updated_at = NOW() WHERE id = %s",
                            (quantity, product_id),
                        )
                    else:
                        cur.execute(
                            "UPDATE inventory_products SET stock_qty = GREATEST(stock_qty - %s, 0), updated_at = NOW() WHERE id = %s",
                            (quantity, product_id),
                        )
                conn.commit()
        return jsonify({"transaction": transaction}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.delete("/transactions/<int:transaction_id>")
def delete_transaction(transaction_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM inventory_transactions WHERE id = %s AND owner_id = %s RETURNING id",
                    (transaction_id, owner_id),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Transaction not found"}), 404
                conn.commit()
        return jsonify({"message": "Transaction deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _period_bounds(period, anchor):
    if period == "weekly":
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=6)
    elif period == "monthly":
        start = anchor.replace(day=1)
        if anchor.month == 12:
            end = anchor.replace(day=31)
        else:
            end = anchor.replace(month=anchor.month + 1, day=1) - timedelta(days=1)
    else:
        start = anchor
        end = anchor
    return start, end


@inventory_bp.get("/transactions")
def list_transactions():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400

    period = str(request.args.get("period", "daily")).strip().lower()
    if period not in ("daily", "weekly", "monthly"):
        period = "daily"
    anchor = parse_date(request.args.get("date"))
    start_date, end_date = _period_bounds(period, anchor)

    type_filter = str(request.args.get("type", "all")).strip().upper()
    if type_filter not in ("ALL", "SALE", "WASTAGE", "PURCHASE"):
        type_filter = "ALL"

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                type_clause = ""
                params = [owner_id, start_date, end_date]
                if type_filter != "ALL":
                    type_clause = "AND transaction_type = %s"
                    params.append(type_filter)

                cur.execute(
                    f"""
                    SELECT id, product_id, product_name, quantity, unit, amount, transaction_type, payment_method, transaction_date, note, created_at
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date BETWEEN %s AND %s {type_clause}
                    ORDER BY transaction_date DESC, created_at DESC
                    """,
                    params,
                )
                rows = cur.fetchall()
                transactions = [serialize_transaction(row) for row in rows]

                cur.execute(
                    """
                    SELECT product_name, COALESCE(SUM(quantity), 0), COALESCE(SUM(amount), 0), COUNT(*)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date BETWEEN %s AND %s AND transaction_type = 'SALE'
                    GROUP BY product_name
                    ORDER BY SUM(amount) DESC
                    """,
                    (owner_id, start_date, end_date),
                )
                by_product = [
                    {
                        "productName": r[0],
                        "quantity": float(r[1] or 0),
                        "amount": float(r[2] or 0),
                        "count": r[3],
                    }
                    for r in cur.fetchall()
                ]

                cur.execute(
                    """
                    SELECT transaction_type, COALESCE(SUM(amount), 0), COALESCE(SUM(quantity), 0), COUNT(*)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date BETWEEN %s AND %s
                    GROUP BY transaction_type
                    """,
                    (owner_id, start_date, end_date),
                )
                type_totals = {
                    r[0]: {"amount": float(r[1] or 0), "quantity": float(r[2] or 0), "count": r[3]}
                    for r in cur.fetchall()
                }

                cur.execute(
                    """
                    SELECT payment_method, COALESCE(SUM(amount), 0)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date BETWEEN %s AND %s AND transaction_type = 'SALE'
                    GROUP BY payment_method
                    """,
                    (owner_id, start_date, end_date),
                )
                payment_totals = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

        sale_totals = type_totals.get("SALE", {"amount": 0, "quantity": 0, "count": 0})
        wastage_totals = type_totals.get("WASTAGE", {"amount": 0, "quantity": 0, "count": 0})
        purchase_totals = type_totals.get("PURCHASE", {"amount": 0, "quantity": 0, "count": 0})

        return jsonify({
            "transactions": transactions,
            "period": period,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "summary": {
                "totalAmount": sale_totals["amount"],
                "totalQuantity": sale_totals["quantity"],
                "totalCount": len(transactions),
                "wastageValue": wastage_totals["amount"],
                "wastageQuantity": wastage_totals["quantity"],
                "wastageCount": wastage_totals["count"],
                "purchaseValue": purchase_totals["amount"],
                "purchaseQuantity": purchase_totals["quantity"],
                "purchaseCount": purchase_totals["count"],
                "paymentMethodSplit": {
                    "cash": payment_totals.get("CASH", 0),
                    "upi": payment_totals.get("UPI", 0),
                    "credit": payment_totals.get("CREDIT", 0),
                },
                "byProduct": by_product,
            },
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.get("/insights")
def get_insights():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Top selling products (last 30 days, by amount)
                cur.execute(
                    """
                    SELECT product_name, COALESCE(SUM(amount), 0), COALESCE(SUM(quantity), 0)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date >= CURRENT_DATE - INTERVAL '30 days'
                        AND transaction_type = 'SALE'
                    GROUP BY product_name
                    ORDER BY SUM(amount) DESC
                    LIMIT 8
                    """,
                    (owner_id,),
                )
                top_products = [
                    {"productName": r[0], "amount": float(r[1] or 0), "quantity": float(r[2] or 0)}
                    for r in cur.fetchall()
                ]

                # Most wasted products (last 30 days, by lost value)
                cur.execute(
                    """
                    SELECT product_name, COALESCE(SUM(amount), 0), COALESCE(SUM(quantity), 0)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date >= CURRENT_DATE - INTERVAL '30 days'
                        AND transaction_type = 'WASTAGE'
                    GROUP BY product_name
                    ORDER BY SUM(amount) DESC
                    LIMIT 8
                    """,
                    (owner_id,),
                )
                top_wasted_products = [
                    {"productName": r[0], "amount": float(r[1] or 0), "quantity": float(r[2] or 0)}
                    for r in cur.fetchall()
                ]

                # Day-wise trend: last 7 days, zero-filled
                cur.execute(
                    """
                    SELECT d::date, COALESCE(SUM(t.amount), 0)
                    FROM generate_series(CURRENT_DATE - INTERVAL '6 days', CURRENT_DATE, INTERVAL '1 day') AS d
                    LEFT JOIN inventory_transactions t
                        ON t.transaction_date = d::date AND t.owner_id = %s AND t.transaction_type = 'SALE'
                    GROUP BY d
                    ORDER BY d
                    """,
                    (owner_id,),
                )
                daily_trend = [
                    {"label": r[0].strftime("%d %b"), "date": r[0].isoformat(), "amount": float(r[1] or 0)}
                    for r in cur.fetchall()
                ]

                # Week-wise trend: last 8 weeks (Mon-start), zero-filled
                cur.execute(
                    """
                    SELECT gs::date, COALESCE(SUM(t.amount), 0)
                    FROM generate_series(
                        date_trunc('week', CURRENT_DATE) - INTERVAL '7 weeks',
                        date_trunc('week', CURRENT_DATE),
                        INTERVAL '1 week'
                    ) AS gs
                    LEFT JOIN inventory_transactions t
                        ON date_trunc('week', t.transaction_date) = gs AND t.owner_id = %s AND t.transaction_type = 'SALE'
                    GROUP BY gs
                    ORDER BY gs
                    """,
                    (owner_id,),
                )
                weekly_trend = [
                    {"label": r[0].strftime("%d %b"), "weekStart": r[0].isoformat(), "amount": float(r[1] or 0)}
                    for r in cur.fetchall()
                ]

                # Month-wise trend: last 6 months, zero-filled
                cur.execute(
                    """
                    SELECT gs::date, COALESCE(SUM(t.amount), 0)
                    FROM generate_series(
                        date_trunc('month', CURRENT_DATE) - INTERVAL '5 months',
                        date_trunc('month', CURRENT_DATE),
                        INTERVAL '1 month'
                    ) AS gs
                    LEFT JOIN inventory_transactions t
                        ON date_trunc('month', t.transaction_date) = gs AND t.owner_id = %s AND t.transaction_type = 'SALE'
                    GROUP BY gs
                    ORDER BY gs
                    """,
                    (owner_id,),
                )
                monthly_trend = [
                    {"label": r[0].strftime("%b %Y"), "monthStart": r[0].isoformat(), "amount": float(r[1] or 0)}
                    for r in cur.fetchall()
                ]

                # Purchase vs Sales (last 30 days)
                cur.execute(
                    """
                    SELECT transaction_type, COALESCE(SUM(amount), 0)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date >= CURRENT_DATE - INTERVAL '30 days'
                        AND transaction_type IN ('SALE', 'PURCHASE')
                    GROUP BY transaction_type
                    """,
                    (owner_id,),
                )
                type_amounts = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
                purchase_vs_sales = {
                    "purchase": type_amounts.get("PURCHASE", 0),
                    "sales": type_amounts.get("SALE", 0),
                }

                # Payment method split (last 30 days, sales only)
                cur.execute(
                    """
                    SELECT payment_method, COALESCE(SUM(amount), 0)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date >= CURRENT_DATE - INTERVAL '30 days'
                        AND transaction_type = 'SALE'
                    GROUP BY payment_method
                    """,
                    (owner_id,),
                )
                payment_amounts = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
                payment_method_split = {
                    "cash": payment_amounts.get("CASH", 0),
                    "upi": payment_amounts.get("UPI", 0),
                    "credit": payment_amounts.get("CREDIT", 0),
                }

        return jsonify({
            "topProducts": top_products,
            "topWastedProducts": top_wasted_products,
            "dailyTrend": daily_trend,
            "weeklyTrend": weekly_trend,
            "monthlyTrend": monthly_trend,
            "purchaseVsSales": purchase_vs_sales,
            "paymentMethodSplit": payment_method_split,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _compute_restock_suggestions(cur, owner_id):
    """
    Smart restock suggestions: for each product, estimate daily sales velocity and
    daily wastage rate from the last 14 days of activity, project how many days of
    stock remain, and suggest a restock quantity sized to a short buffer window that
    shrinks when wastage is a large share of depletion (so high-spoilage items get
    bought little-and-often instead of in bulk).
    """
    window_days = 14
    window_start = date.today() - timedelta(days=window_days - 1)

    cur.execute(
        """
        SELECT id, name, unit, stock_qty, low_stock_threshold
        FROM inventory_products
        WHERE owner_id = %s AND status = 'active'
        """,
        (owner_id,),
    )
    products = cur.fetchall()

    suggestions = []
    for pid, name, unit, stock_qty, low_stock_threshold in products:
        cur.execute(
            """
            SELECT transaction_type, COALESCE(SUM(quantity), 0), MIN(transaction_date)
            FROM inventory_transactions
            WHERE product_id = %s AND transaction_type IN ('SALE', 'WASTAGE')
                AND transaction_date >= %s
            GROUP BY transaction_type
            """,
            (pid, window_start),
        )
        rows = {r[0]: {"qty": float(r[1] or 0), "minDate": r[2]} for r in cur.fetchall()}
        sale_qty = rows.get("SALE", {}).get("qty", 0)
        wastage_qty = rows.get("WASTAGE", {}).get("qty", 0)

        if sale_qty <= 0 and wastage_qty <= 0:
            continue

        min_dates = [v["minDate"] for v in rows.values() if v.get("minDate")]
        earliest = min(min_dates) if min_dates else window_start
        days_active = max((date.today() - earliest).days, 1)
        days_active = min(days_active, window_days)

        avg_daily_sales = sale_qty / days_active
        avg_daily_wastage = wastage_qty / days_active
        net_daily_depletion = avg_daily_sales + avg_daily_wastage

        if net_daily_depletion <= 0:
            continue

        stock_qty_f = float(stock_qty or 0)
        days_of_stock_left = stock_qty_f / net_daily_depletion
        wastage_ratio = avg_daily_wastage / net_daily_depletion

        # High wastage share -> shorter buffer (buy little and often); low wastage -> normal buffer.
        if wastage_ratio >= 0.35:
            buffer_days = 1
        elif wastage_ratio >= 0.15:
            buffer_days = 2
        else:
            buffer_days = 3

        target_stock = buffer_days * net_daily_depletion
        suggested_qty = max(0.0, target_stock - stock_qty_f)

        is_low_stock = stock_qty_f <= float(low_stock_threshold or 0)
        needs_restock = days_of_stock_left <= 2 or is_low_stock

        if not needs_restock or suggested_qty <= 0:
            continue

        suggestions.append({
            "productId": str(pid),
            "productName": name,
            "unit": unit,
            "currentStock": round(stock_qty_f, 2),
            "avgDailySales": round(avg_daily_sales, 2),
            "avgDailyWastage": round(avg_daily_wastage, 2),
            "wastageRatio": round(wastage_ratio, 2),
            "daysOfStockLeft": round(days_of_stock_left, 1),
            "suggestedQty": round(suggested_qty, 1),
            "urgency": "critical" if days_of_stock_left <= 1 else "soon",
        })

    suggestions.sort(key=lambda s: s["daysOfStockLeft"])
    return suggestions


@inventory_bp.get("/restock-suggestions")
def restock_suggestions():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                suggestions = _compute_restock_suggestions(cur, owner_id)
        return jsonify({"suggestions": suggestions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
