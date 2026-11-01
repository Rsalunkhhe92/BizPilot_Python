"""
Food Seller REST API (mounted at /api/foodseller): setup, masters and every
stock-moving transaction. Reports and dashboard are in foodseller_reports.py, the AI
assistant in foodseller_ai.py; both register on the same blueprint at the bottom.
Design notes are in FOODSELLER.md.
"""

from flask import g, request

from foodseller_common import (
    MAX_MONEY, ApiError, StockGuard, balance, body, camel, choice, date_range, foodseller_bp, idempotent, int_id, lines_of,
    load_settings, load_unit, lock_products, number, ok, owned, page_args, post_ledger, post_stock_out, reconcile_usage,
    stock_unit, text, to_stock_qty, today_ist, tx, txn_date,
)
from foodseller_core import (
    ZERO, CoreError, allocate_discount, average_cost, convert_qty, dec, money2, qty4, stock_state,
)
from foodseller_service import (
    DEFAULT_CATEGORIES, DEFAULT_EXPENSE_CATEGORIES, DEFAULT_UNITS, FOOD_SELLER_FEATURES, STARTER_MENU, STARTER_PRODUCTS,
)


def _who(on_date=None):
    return g.business_id, g.user_id, on_date or today_ist()


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
def ensure_defaults(cur):
    """Runs on first use per business. Afterwards units, categories and expense
    categories are the owner's own rows to edit or delete."""
    cur.execute(
        "INSERT INTO food_settings (business_id, user_id, transaction_date) VALUES (%s, %s, %s) ON CONFLICT (business_id) DO NOTHING RETURNING id",
        _who(),
    )
    if cur.fetchone() is None:
        return
    for name, dimension, to_base in DEFAULT_UNITS:
        cur.execute(
            "INSERT INTO food_units (business_id, user_id, transaction_date, name, dimension, to_base) VALUES (%s, %s, %s, %s, %s, %s)",
            (*_who(), name, dimension, to_base),
        )
    for name in DEFAULT_CATEGORIES:
        cur.execute("INSERT INTO food_product_categories (business_id, user_id, transaction_date, name) VALUES (%s, %s, %s, %s)", (*_who(), name))
    for name, counts in DEFAULT_EXPENSE_CATEGORIES:
        cur.execute(
            "INSERT INTO food_expense_categories (business_id, user_id, transaction_date, name, counts_in_pnl) VALUES (%s, %s, %s, %s, %s)",
            (*_who(), name, counts),
        )


def _name_map(cur, table):
    cur.execute(f"SELECT id, name FROM {table} WHERE business_id = %s", (g.business_id,))
    return {r["name"].lower(): r["id"] for r in cur.fetchall()}


@foodseller_bp.get("/bootstrap")
def bootstrap():
    with tx() as cur:
        ensure_defaults(cur)
        settings = load_settings(cur)
        cur.execute("SELECT * FROM food_units WHERE business_id = %s ORDER BY dimension, to_base DESC, name", (g.business_id,))
        units = [camel(r) for r in cur.fetchall()]
        cur.execute("SELECT id, name FROM food_product_categories WHERE business_id = %s ORDER BY name", (g.business_id,))
        categories = [camel(r) for r in cur.fetchall()]
        cur.execute("SELECT id, name, counts_in_pnl FROM food_expense_categories WHERE business_id = %s ORDER BY name", (g.business_id,))
        expense_categories = [camel(r) for r in cur.fetchall()]
        cur.execute("SELECT id, name, mobile, address, status FROM food_suppliers WHERE business_id = %s ORDER BY name", (g.business_id,))
        suppliers = [camel(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS n FROM food_inventory_products WHERE business_id = %s AND deleted_at IS NULL", (g.business_id,))
        product_count = cur.fetchone()["n"]
    return ok({
        "settings": camel(settings), "units": units, "categories": categories, "expenseCategories": expense_categories,
        "suppliers": suppliers, "features": g.food_features or FOOD_SELLER_FEATURES, "today": today_ist(),
        "needsSetup": not settings["starter_choice"] and product_count == 0,
    })


@foodseller_bp.post("/setup/starter")
def setup_starter():
    """Adds the starter list as ordinary rows, or records that the owner starts empty."""
    use_starter = bool(body().get("useStarter"))
    with tx() as cur:
        ensure_defaults(cur)
        cur.execute("SELECT starter_choice FROM food_settings WHERE business_id = %s FOR UPDATE", (g.business_id,))
        if cur.fetchone()["starter_choice"]:
            raise ApiError(409, "Starter setup was already done")
        cur.execute("UPDATE food_settings SET starter_choice = %s WHERE business_id = %s", ("starter" if use_starter else "empty", g.business_id))
        added = {"products": 0, "foods": 0}
        if use_starter:
            added = _load_starter(cur)
    return ok(added, 201)


def _load_starter(cur):
    categories, units = _name_map(cur, "food_product_categories"), _name_map(cur, "food_units")
    product_ids = {}
    for category, products in STARTER_PRODUCTS:
        for name, unit in products:
            cur.execute(
                """INSERT INTO food_inventory_products (business_id, user_id, transaction_date, name, category_id, unit_id)
                   VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
                (*_who(), name, categories.get(category.lower()), units[unit.lower()]),
            )
            row = cur.fetchone()
            if row:
                product_ids[name] = row["id"]
    foods = 0
    for item in STARTER_MENU:
        packaged = product_ids.get(item.get("packaged"))
        cur.execute(
            """INSERT INTO food_products (business_id, user_id, transaction_date, name, category_id, product_type, selling_price, packaged_product_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
            (*_who(), item["name"], categories.get(item["category"].lower()), "PACKAGED" if packaged else "RECIPE", item["price"], packaged),
        )
        row = cur.fetchone()
        if not row:
            continue
        foods += 1
        if item.get("ingredients"):
            cur.execute(
                "INSERT INTO food_recipes (business_id, user_id, transaction_date, food_product_id, yield_qty) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (*_who(), row["id"], item["yield"]),
            )
            recipe_id = cur.fetchone()["id"]
            for name, quantity, unit in item["ingredients"]:
                cur.execute(
                    """INSERT INTO food_recipe_ingredients (business_id, user_id, transaction_date, recipe_id, product_id, quantity, unit_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (*_who(), recipe_id, product_ids[name], quantity, units[unit.lower()]),
                )
    return {"products": len(product_ids), "foods": foods}


@foodseller_bp.get("/settings")
def get_settings():
    with tx() as cur:
        ensure_defaults(cur)
        return ok({"settings": camel(load_settings(cur))})


@foodseller_bp.put("/settings")
def put_settings():
    payload = body()
    with tx() as cur:
        ensure_defaults(cur)
        current = load_settings(cur)
        cur.execute(
            """UPDATE food_settings SET allow_negative_stock = %s, cover_days = %s, excess_ratio = %s, unusual_expense_ratio = %s
               WHERE business_id = %s RETURNING *""",
            (bool(payload.get("allowNegativeStock", current["allow_negative_stock"])),
             int(number(payload, "coverDays", default=current["cover_days"], limit=30)) or 1,
             max(number(payload, "excessRatio", default=current["excess_ratio"], limit=100), dec(1)),
             max(number(payload, "unusualExpenseRatio", default=current["unusual_expense_ratio"], limit=100), dec(1)),
             g.business_id),
        )
        return ok({"settings": camel(cur.fetchone())})


# ---------------------------------------------------------------------------
# Simple masters: units, categories, expense categories, suppliers
# ---------------------------------------------------------------------------
def _insert_returning(cur, table, columns, values):
    placeholders = ", ".join(["%s"] * (3 + len(columns)))
    cur.execute(
        f"INSERT INTO {table} (business_id, user_id, transaction_date, {', '.join(columns)}) VALUES ({placeholders}) RETURNING *",
        (*_who(), *values),
    )
    return cur.fetchone()


def _unit_values(payload):
    dimension = choice(payload, "dimension", ("mass", "volume", "count", "custom"), default="custom", label="Unit type")
    to_base = number(payload, "toBase", positive=True, default="1", label="Conversion") if dimension != "custom" else dec(1)
    return text(payload, "name", 40, required=True, label="Unit name"), dimension, to_base


@foodseller_bp.post("/units")
def create_unit():
    values = _unit_values(body())
    with tx() as cur:
        return ok({"unit": camel(_insert_returning(cur, "food_units", ("name", "dimension", "to_base"), values))}, 201)


@foodseller_bp.put("/units/<int:unit_id>")
def update_unit(unit_id):
    name, dimension, to_base = _unit_values(body())
    with tx() as cur:
        current = load_unit(cur, unit_id)
        if (dimension != current["dimension"] or dec(to_base) != current["to_base"]) and _unit_in_use(cur, unit_id):
            raise ApiError(409, "This unit is already used in records, so only its name can change")
        cur.execute("UPDATE food_units SET name = %s, dimension = %s, to_base = %s WHERE id = %s AND business_id = %s RETURNING *",
                    (name, dimension, to_base, unit_id, g.business_id))
        return ok({"unit": camel(cur.fetchone())})


def _unit_in_use(cur, unit_id):
    cur.execute(
        """SELECT EXISTS (SELECT 1 FROM food_inventory_products WHERE business_id = %(b)s AND unit_id = %(u)s)
               OR EXISTS (SELECT 1 FROM food_purchase_items WHERE business_id = %(b)s AND unit_id = %(u)s)
               OR EXISTS (SELECT 1 FROM food_consumption_items WHERE business_id = %(b)s AND unit_id = %(u)s)
               OR EXISTS (SELECT 1 FROM food_stock_adjustments WHERE business_id = %(b)s AND unit_id = %(u)s)
               OR EXISTS (SELECT 1 FROM food_recipe_ingredients WHERE business_id = %(b)s AND unit_id = %(u)s) AS used""",
        {"b": g.business_id, "u": unit_id},
    )
    return cur.fetchone()["used"]


@foodseller_bp.delete("/units/<int:unit_id>")
def delete_unit(unit_id):
    with tx() as cur:
        load_unit(cur, unit_id)
        if _unit_in_use(cur, unit_id):
            raise ApiError(409, "This unit is used in records and cannot be deleted")
        cur.execute("DELETE FROM food_units WHERE id = %s AND business_id = %s", (unit_id, g.business_id))
    return ok({"deleted": True})


@foodseller_bp.post("/categories")
def create_category():
    name = text(body(), "name", 80, required=True, label="Category name")
    with tx() as cur:
        return ok({"category": camel(_insert_returning(cur, "food_product_categories", ("name",), (name,)))}, 201)


@foodseller_bp.put("/categories/<int:category_id>")
def update_category(category_id):
    name = text(body(), "name", 80, required=True, label="Category name")
    with tx() as cur:
        cur.execute("UPDATE food_product_categories SET name = %s WHERE id = %s AND business_id = %s RETURNING *", (name, category_id, g.business_id))
        row = cur.fetchone()
        if not row:
            raise ApiError(404, "Category not found")
        return ok({"category": camel(row)})


@foodseller_bp.delete("/categories/<int:category_id>")
def delete_category(category_id):
    with tx() as cur:
        owned(cur, "food_product_categories", category_id, "Category")
        # Soft-deleted rows still hold the reference, so history stays intact and this is refused too.
        cur.execute("DELETE FROM food_product_categories WHERE id = %s AND business_id = %s", (category_id, g.business_id))
    return ok({"deleted": True})


@foodseller_bp.post("/expense-categories")
def create_expense_category():
    payload = body()
    name = text(payload, "name", 80, required=True, label="Category name")
    with tx() as cur:
        row = _insert_returning(cur, "food_expense_categories", ("name", "counts_in_pnl"), (name, bool(payload.get("countsInPnl", True))))
        return ok({"category": camel(row)}, 201)


@foodseller_bp.put("/expense-categories/<int:category_id>")
def update_expense_category(category_id):
    payload = body()
    name = text(payload, "name", 80, required=True, label="Category name")
    with tx() as cur:
        cur.execute(
            "UPDATE food_expense_categories SET name = %s, counts_in_pnl = %s WHERE id = %s AND business_id = %s RETURNING *",
            (name, bool(payload.get("countsInPnl", True)), category_id, g.business_id),
        )
        row = cur.fetchone()
        if not row:
            raise ApiError(404, "Category not found")
        return ok({"category": camel(row)})


@foodseller_bp.delete("/expense-categories/<int:category_id>")
def delete_expense_category(category_id):
    with tx() as cur:
        owned(cur, "food_expense_categories", category_id, "Category")
        cur.execute("DELETE FROM food_expense_categories WHERE id = %s AND business_id = %s", (category_id, g.business_id))
    return ok({"deleted": True})


def _supplier_values(payload):
    return (text(payload, "name", 120, required=True, label="Supplier name"), text(payload, "mobile", 15),
            text(payload, "address", 300), choice(payload, "status", ("Active", "Inactive"), default="Active"))


@foodseller_bp.get("/suppliers")
def list_suppliers():
    with tx() as cur:
        cur.execute(
            """SELECT s.id, s.name, s.mobile, s.address, s.status,
                      COALESCE(p.bills, 0) AS bills, COALESCE(p.total, 0) AS total_purchases, COALESCE(p.last_date, NULL) AS last_purchase,
                      COALESCE(p.total, 0) - COALESCE(p.paid, 0) - COALESCE(pay.paid, 0) AS due
               FROM food_suppliers s
               LEFT JOIN (SELECT supplier_id, COUNT(*) AS bills, SUM(total_amount) AS total, SUM(amount_paid) AS paid, MAX(transaction_date) AS last_date
                          FROM food_purchase_transactions WHERE business_id = %(b)s AND status = 'Posted' GROUP BY supplier_id) p ON p.supplier_id = s.id
               LEFT JOIN (SELECT h.supplier_id, SUM(x.amount) AS paid FROM food_payments x
                          JOIN food_purchase_transactions h ON h.id = x.purchase_id
                          WHERE x.business_id = %(b)s AND x.status = 'Posted' AND h.status = 'Posted' GROUP BY h.supplier_id) pay ON pay.supplier_id = s.id
               WHERE s.business_id = %(b)s ORDER BY s.name""",
            {"b": g.business_id},
        )
        return ok({"suppliers": [camel(r) for r in cur.fetchall()]})


@foodseller_bp.post("/suppliers")
def create_supplier():
    values = _supplier_values(body())
    with tx() as cur:
        return ok({"supplier": camel(_insert_returning(cur, "food_suppliers", ("name", "mobile", "address", "status"), values))}, 201)


@foodseller_bp.put("/suppliers/<int:supplier_id>")
def update_supplier(supplier_id):
    values = _supplier_values(body())
    with tx() as cur:
        cur.execute("UPDATE food_suppliers SET name = %s, mobile = %s, address = %s, status = %s WHERE id = %s AND business_id = %s RETURNING *",
                    (*values, supplier_id, g.business_id))
        row = cur.fetchone()
        if not row:
            raise ApiError(404, "Supplier not found")
        return ok({"supplier": camel(row)})


@foodseller_bp.delete("/suppliers/<int:supplier_id>")
def delete_supplier(supplier_id):
    with tx() as cur:
        owned(cur, "food_suppliers", supplier_id, "Supplier")
        cur.execute("DELETE FROM food_suppliers WHERE id = %s AND business_id = %s", (supplier_id, g.business_id))
    return ok({"deleted": True})


# ---------------------------------------------------------------------------
# Stock products
# ---------------------------------------------------------------------------
PRODUCT_SELECT = """
    SELECT p.*, c.name AS category_name, u.name AS unit_name, s.name AS supplier_name,
           COALESCE(l.qty, 0) AS stock_qty, COALESCE(l.val, 0) AS stock_value, COALESCE(l.n, 0) AS movements
    FROM food_inventory_products p
    JOIN food_units u ON u.id = p.unit_id
    LEFT JOIN food_product_categories c ON c.id = p.category_id
    LEFT JOIN food_suppliers s ON s.id = p.supplier_id
    LEFT JOIN (SELECT product_id, SUM(quantity) AS qty, SUM(value) AS val, COUNT(*) AS n FROM food_inventory_transactions
               WHERE business_id = %(b)s GROUP BY product_id) l ON l.product_id = p.id
    WHERE p.business_id = %(b)s AND p.deleted_at IS NULL
"""


def serialize_product(row):
    out = camel(row)
    out["avgCost"] = average_cost(row["stock_qty"], row["stock_value"], row["purchase_price"])
    out["stockState"] = stock_state(row["stock_qty"], row["min_stock_alert"])
    return out


@foodseller_bp.get("/products")
def list_products():
    args = request.args
    sql, params = PRODUCT_SELECT, {"b": g.business_id}
    if args.get("q"):
        sql += " AND p.name ILIKE %(q)s"
        params["q"] = "%" + args["q"].replace("%", r"\%").replace("_", r"\_") + "%"
    if args.get("categoryId"):
        sql += " AND p.category_id = %(c)s"
        params["c"] = int_id(args["categoryId"], "Category")
    if args.get("status"):
        sql += " AND p.status = %(s)s"
        params["s"] = args["status"]
    with tx() as cur:
        cur.execute(sql + " ORDER BY p.name", params)
        products = [serialize_product(r) for r in cur.fetchall()]
    state = args.get("state")
    if state == "ATTENTION":
        products = [p for p in products if needs_attention(p)]
    elif state:
        products = [p for p in products if p["stockState"] == state]
    return ok({"products": products})


def _product_values(cur, payload):
    unit_id = int_id(payload.get("unitId"), "Unit")
    load_unit(cur, unit_id)
    selling = payload.get("sellingPrice")
    return {
        "name": text(payload, "name", 120, required=True, label="Product name"),
        "category_id": owned(cur, "food_product_categories", int_id(payload.get("categoryId"), required=False), "Category"),
        "unit_id": unit_id,
        "purchase_price": number(payload, "purchasePrice", label="Purchase price", limit=MAX_MONEY),
        "selling_price": number(payload, "sellingPrice", label="Selling price", limit=MAX_MONEY) if selling not in (None, "") else None,
        "min_stock_alert": number(payload, "minStockAlert", label="Minimum stock alert"),
        "supplier_id": owned(cur, "food_suppliers", int_id(payload.get("supplierId"), required=False), "Supplier"),
        "status": choice(payload, "status", ("Active", "Inactive"), default="Active"),
    }


def _load_product_row(cur, product_id):
    cur.execute(PRODUCT_SELECT + " AND p.id = %(p)s", {"b": g.business_id, "p": product_id})
    row = cur.fetchone()
    if not row:
        raise ApiError(404, "Product not found")
    return row


@foodseller_bp.post("/products")
def create_product():
    payload = body()
    opening = qty4(number(payload, "openingStock", label="Opening stock"))
    on_date = txn_date(payload)
    with tx() as cur:
        values = _product_values(cur, payload)
        row = _insert_returning(cur, "food_inventory_products", (*values.keys(), "opening_stock"), (*values.values(), opening))
        if opening > 0:
            post_ledger(cur, row["id"], "OPENING", opening, opening * values["purchase_price"], on_date, "product", row["id"], "Opening stock")
        return ok({"product": serialize_product(_load_product_row(cur, row["id"]))}, 201)


@foodseller_bp.get("/products/<int:product_id>")
def get_product(product_id):
    size, offset, page = page_args(50)
    with tx() as cur:
        product = serialize_product(_load_product_row(cur, product_id))
        cur.execute(
            """SELECT id, transaction_date, txn_type, quantity, value, source_type, source_id, note, created_at
               FROM food_inventory_transactions WHERE business_id = %s AND product_id = %s
               ORDER BY transaction_date DESC, id DESC LIMIT %s OFFSET %s""",
            (g.business_id, product_id, size + 1, offset),
        )
        rows = cur.fetchall()
    return ok({"product": product, "ledger": [camel(r) for r in rows[:size]], "hasMore": len(rows) > size, "page": page})


@foodseller_bp.put("/products/<int:product_id>")
def update_product(product_id):
    payload = body()
    with tx() as cur:
        lock_products(cur, [product_id])
        values = _product_values(cur, payload)
        cur.execute("SELECT unit_id, opening_stock, transaction_date FROM food_inventory_products WHERE id = %s", (product_id,))
        current = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS n FROM food_inventory_transactions WHERE business_id = %s AND product_id = %s AND txn_type <> 'OPENING'",
                    (g.business_id, product_id))
        has_movement = cur.fetchone()["n"] > 0
        if values["unit_id"] != current["unit_id"] and (has_movement or _used_in_menu(cur, product_id)):
            raise ApiError(409, "The stock unit cannot change once this product has been used")
        new_opening = current["opening_stock"]
        if payload.get("openingStock") not in (None, ""):
            new_opening = qty4(number(payload, "openingStock", label="Opening stock"))
        if new_opening != current["opening_stock"]:
            if has_movement:
                raise ApiError(409, "Opening stock cannot change after stock has moved. Use a stock adjustment instead.")
            delta = new_opening - current["opening_stock"]
            post_ledger(cur, product_id, "OPENING", delta, delta * values["purchase_price"], current["transaction_date"],
                        "product", product_id, "Opening stock corrected")
        assignments = ", ".join(f"{k} = %s" for k in values)
        cur.execute(f"UPDATE food_inventory_products SET {assignments}, opening_stock = %s WHERE id = %s AND business_id = %s",
                    (*values.values(), new_opening, product_id, g.business_id))
        return ok({"product": serialize_product(_load_product_row(cur, product_id))})


def _used_in_menu(cur, product_id):
    cur.execute(
        """SELECT f.name FROM food_recipe_ingredients i JOIN food_recipes r ON r.id = i.recipe_id
           JOIN food_products f ON f.id = r.food_product_id
           WHERE i.business_id = %(b)s AND i.product_id = %(p)s AND f.deleted_at IS NULL
           UNION
           SELECT name FROM food_products WHERE business_id = %(b)s AND packaged_product_id = %(p)s AND deleted_at IS NULL""",
        {"b": g.business_id, "p": product_id},
    )
    return [r["name"] for r in cur.fetchall()]


@foodseller_bp.delete("/products/<int:product_id>")
def delete_product(product_id):
    with tx() as cur:
        lock_products(cur, [product_id])
        foods = _used_in_menu(cur, product_id)
        if foods:
            raise ApiError(409, f"Used in the recipe of: {', '.join(foods)}. Remove it from there first.")
        cur.execute("SELECT COUNT(*) AS n FROM food_inventory_transactions WHERE business_id = %s AND product_id = %s AND txn_type <> 'OPENING'",
                    (g.business_id, product_id))
        if cur.fetchone()["n"] == 0:
            cur.execute("DELETE FROM food_inventory_transactions WHERE business_id = %s AND product_id = %s", (g.business_id, product_id))
            cur.execute("DELETE FROM food_inventory_products WHERE id = %s AND business_id = %s", (product_id, g.business_id))
            return ok({"deleted": True, "archived": False})
        qty, _ = balance(cur, product_id)
        if qty != 0:
            raise ApiError(409, "This product still has stock. Adjust it to zero first, or mark it Inactive.")
        cur.execute("UPDATE food_inventory_products SET deleted_at = NOW(), status = 'Inactive' WHERE id = %s AND business_id = %s", (product_id, g.business_id))
    return ok({"deleted": True, "archived": True})


def needs_attention(product):
    """A product nobody has stocked yet is not an alert, only one that ran low or ran out."""
    return product["stockState"] != "OK" and product["status"] == "Active" and product["movements"] > 0


def stock_alerts(cur, product_ids):
    """Low / out of stock state of just-moved products, returned so the app can warn straight away."""
    if not product_ids:
        return []
    cur.execute(PRODUCT_SELECT + " AND p.id = ANY(%(ids)s)", {"b": g.business_id, "ids": list(product_ids)})
    alerts = []
    for row in cur.fetchall():
        state = stock_state(row["stock_qty"], row["min_stock_alert"])
        if state != "OK":
            alerts.append({"productId": row["id"], "name": row["name"], "state": state, "stockQty": row["stock_qty"], "unit": row["unit_name"]})
    return alerts


# ---------------------------------------------------------------------------
# Purchases
# ---------------------------------------------------------------------------
def create_purchase(cur, payload):
    on_date = txn_date(payload)
    lines = lines_of(payload, label="the purchase")
    method = choice(payload, "paymentMethod", ("Cash", "UPI", "Credit"), default="Cash", label="Payment method")
    supplier_id = owned(cur, "food_suppliers", int_id(payload.get("supplierId"), required=False), "Supplier")
    products = lock_products(cur, [line.get("productId") for line in lines])
    prepared, total = [], ZERO
    for line in lines:
        product = products[int_id(line.get("productId"), "Product")]
        entered = number(line, "quantity", required=True, positive=True, label=f"{product['name']} quantity")
        rate = number(line, "rate", required=True, label=f"{product['name']} rate", limit=MAX_MONEY)
        stock_qty, unit = to_stock_qty(cur, product, entered, line.get("unitId"))
        line_total = money2(entered * rate)
        prepared.append((product, entered, unit, rate, line_total, stock_qty))
        total += line_total
    paid = number(payload, "amountPaid", label="Amount paid", default=ZERO if method == "Credit" else total, limit=MAX_MONEY)
    paid = money2(paid)
    if paid > total:
        raise ApiError(422, "Amount paid is more than the purchase total")
    cur.execute(
        """INSERT INTO food_purchase_transactions (business_id, user_id, transaction_date, supplier_id, invoice_no, total_amount, amount_paid, payment_method, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (*_who(on_date), supplier_id, text(payload, "invoiceNo", 60), total, paid, method, text(payload, "notes", 300)),
    )
    purchase = cur.fetchone()
    for product, entered, unit, rate, line_total, stock_qty in prepared:
        cur.execute(
            """INSERT INTO food_purchase_items (business_id, user_id, transaction_date, purchase_id, product_id, entered_qty, unit_id, rate, total, stock_qty)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (*_who(on_date), purchase["id"], product["id"], entered, unit["id"], rate, line_total, stock_qty),
        )
        item_id = cur.fetchone()["id"]
        post_ledger(cur, product["id"], "PURCHASE", stock_qty, line_total, on_date, "purchase_item", item_id)
        # Latest rate per stock unit: the fallback cost while stock is empty.
        cur.execute(
            "UPDATE food_inventory_products SET purchase_price = %s, supplier_id = COALESCE(%s, supplier_id) WHERE id = %s AND business_id = %s",
            (qty4(line_total / stock_qty), supplier_id, product["id"], g.business_id),
        )
    return {"purchase": camel(purchase), "alerts": stock_alerts(cur, products.keys())}


@foodseller_bp.post("/purchases")
@idempotent
def post_purchase(cur):
    return create_purchase(cur, body()), 201


def _attach_purchase_items(cur, purchases):
    ids = [p["id"] for p in purchases]
    cur.execute(
        """SELECT i.purchase_id, i.id, i.product_id, p.name AS product_name, i.entered_qty, u.name AS unit_name, i.rate, i.total, i.stock_qty
           FROM food_purchase_items i JOIN food_inventory_products p ON p.id = i.product_id JOIN food_units u ON u.id = i.unit_id
           WHERE i.business_id = %s AND i.purchase_id = ANY(%s) ORDER BY i.id""",
        (g.business_id, ids),
    )
    by_header = {}
    for r in cur.fetchall():
        by_header.setdefault(r["purchase_id"], []).append(camel(r))
    for p in purchases:
        p["items"] = by_header.get(p["id"], [])
    return purchases


PURCHASE_SELECT = """
    SELECT h.*, s.name AS supplier_name,
           h.total_amount - h.amount_paid - COALESCE((SELECT SUM(x.amount) FROM food_payments x
               WHERE x.purchase_id = h.id AND x.status = 'Posted'), 0) AS due
    FROM food_purchase_transactions h LEFT JOIN food_suppliers s ON s.id = h.supplier_id
    WHERE h.business_id = %(b)s
"""


@foodseller_bp.get("/purchases")
def list_purchases():
    start, end = date_range(default_from=today_ist().replace(day=1))
    size, offset, page = page_args()
    sql, params = PURCHASE_SELECT + " AND h.transaction_date BETWEEN %(s)s AND %(e)s", {"b": g.business_id, "s": start, "e": end}
    if request.args.get("supplierId"):
        sql += " AND h.supplier_id = %(sup)s"
        params["sup"] = int_id(request.args["supplierId"], "Supplier")
    with tx() as cur:
        cur.execute(sql + " ORDER BY h.transaction_date DESC, h.id DESC LIMIT %(l)s OFFSET %(o)s", {**params, "l": size + 1, "o": offset})
        rows = [camel(r) for r in cur.fetchall()]
        items = _attach_purchase_items(cur, rows[:size])
    return ok({"purchases": items, "hasMore": len(rows) > size, "page": page})


@foodseller_bp.get("/purchases/<int:purchase_id>")
def get_purchase(purchase_id):
    with tx() as cur:
        cur.execute(PURCHASE_SELECT + " AND h.id = %(p)s", {"b": g.business_id, "p": purchase_id})
        row = cur.fetchone()
        if not row:
            raise ApiError(404, "Purchase not found")
        return ok({"purchase": _attach_purchase_items(cur, [camel(row)])[0]})


def _load_for_void(cur, table, row_id, label):
    cur.execute(f"SELECT * FROM {table} WHERE id = %s AND business_id = %s FOR UPDATE", (row_id, g.business_id))
    row = cur.fetchone()
    if not row:
        raise ApiError(404, f"{label} not found")
    if row["status"] != "Posted":
        raise ApiError(409, f"This {label.lower()} is already voided")
    return row


def _assert_no_payments(cur, column, row_id):
    cur.execute(f"SELECT COUNT(*) AS n FROM food_payments WHERE {column} = %s AND business_id = %s AND status = 'Posted'", (row_id, g.business_id))
    if cur.fetchone()["n"]:
        raise ApiError(409, "Payments are recorded against this entry. Void those payments first.")


@foodseller_bp.post("/purchases/<int:purchase_id>/void")
@idempotent
def void_purchase(cur, purchase_id):
    payload = body()
    purchase = _load_for_void(cur, "food_purchase_transactions", purchase_id, "Purchase")
    _assert_no_payments(cur, "purchase_id", purchase_id)
    cur.execute("SELECT id, product_id, stock_qty, total FROM food_purchase_items WHERE purchase_id = %s AND business_id = %s", (purchase_id, g.business_id))
    items = cur.fetchall()
    products = lock_products(cur, [i["product_id"] for i in items])
    guard = StockGuard(cur, payload.get("allowNegative"))
    for item in items:
        post_ledger(cur, item["product_id"], "PURCHASE", -item["stock_qty"], -item["total"], purchase["transaction_date"],
                    "purchase_item", item["id"], "Purchase voided")
        guard.note_reduction(products[item["product_id"]])
    guard.enforce()
    cur.execute("UPDATE food_purchase_transactions SET status = 'Voided' WHERE id = %s", (purchase_id,))
    return {"voided": True}, 200


# ---------------------------------------------------------------------------
# Manual consumption
# ---------------------------------------------------------------------------
@foodseller_bp.post("/consumption")
@idempotent
def post_consumption(cur):
    payload = body()
    on_date = txn_date(payload)
    lines = lines_of(payload, label="the consumption entry")
    products = lock_products(cur, [line.get("productId") for line in lines])
    guard = StockGuard(cur, payload.get("allowNegative"))
    before = {pid: balance(cur, pid)[0] for pid in products}
    cur.execute(
        "INSERT INTO food_consumption_transactions (business_id, user_id, transaction_date, notes) VALUES (%s, %s, %s, %s) RETURNING *",
        (*_who(on_date), text(payload, "notes", 300)),
    )
    header = cur.fetchone()
    preparation = {}
    entered_by_product = {}
    for line in lines:
        product = products[int_id(line.get("productId"), "Product")]
        entered = number(line, "quantity", required=True, positive=True, label=f"{product['name']} quantity")
        purpose = choice(line, "purpose", ("PREPARATION", "WASTAGE", "SPOILAGE", "PERSONAL"), default="PREPARATION", label="Purpose")
        stock_qty, unit = to_stock_qty(cur, product, entered, line.get("unitId"))
        cur.execute(
            """INSERT INTO food_consumption_items (business_id, user_id, transaction_date, consumption_id, product_id, entered_qty, unit_id, stock_qty, purpose)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (*_who(on_date), header["id"], product["id"], entered, unit["id"], stock_qty, purpose),
        )
        item_id = cur.fetchone()["id"]
        entered_by_product[product["id"]] = entered_by_product.get(product["id"], ZERO) + stock_qty
        if purpose == "PREPARATION":
            preparation[product["id"]] = product
        else:
            post_stock_out(cur, product, purpose, stock_qty, on_date, "consumption_item", item_id, guard)
    for product in preparation.values():
        reconcile_usage(cur, product, on_date, "CONSUMPTION", "consumption", header["id"], guard)
    guard.enforce()
    deductions = []
    for pid, product in products.items():
        after = balance(cur, pid)[0]
        deductions.append({"productId": pid, "name": product["name"], "unit": product["unit_name"], "entered": entered_by_product[pid],
                           "deducted": before[pid] - after, "coveredBySales": entered_by_product[pid] - (before[pid] - after), "remaining": after})
    return {"consumption": camel(header), "deductions": deductions, "alerts": stock_alerts(cur, products.keys())}, 201


@foodseller_bp.get("/consumption")
def list_consumption():
    start, end = date_range(default_from=today_ist().replace(day=1))
    size, offset, page = page_args()
    with tx() as cur:
        cur.execute(
            """SELECT * FROM food_consumption_transactions WHERE business_id = %s AND transaction_date BETWEEN %s AND %s
               ORDER BY transaction_date DESC, id DESC LIMIT %s OFFSET %s""",
            (g.business_id, start, end, size + 1, offset),
        )
        rows = [camel(r) for r in cur.fetchall()]
        page_rows = rows[:size]
        cur.execute(
            """SELECT i.consumption_id, i.id, i.product_id, p.name AS product_name, i.entered_qty, u.name AS unit_name, i.stock_qty, i.purpose
               FROM food_consumption_items i JOIN food_inventory_products p ON p.id = i.product_id JOIN food_units u ON u.id = i.unit_id
               WHERE i.business_id = %s AND i.consumption_id = ANY(%s) ORDER BY i.id""",
            (g.business_id, [r["id"] for r in page_rows]),
        )
        by_header = {}
        for r in cur.fetchall():
            by_header.setdefault(r["consumption_id"], []).append(camel(r))
    for r in page_rows:
        r["items"] = by_header.get(r["id"], [])
    return ok({"consumption": page_rows, "hasMore": len(rows) > size, "page": page})


@foodseller_bp.post("/consumption/<int:consumption_id>/void")
@idempotent
def void_consumption(cur, consumption_id):
    header = _load_for_void(cur, "food_consumption_transactions", consumption_id, "Consumption entry")
    cur.execute("SELECT id, product_id, purpose FROM food_consumption_items WHERE consumption_id = %s AND business_id = %s", (consumption_id, g.business_id))
    items = cur.fetchall()
    products = lock_products(cur, [i["product_id"] for i in items])
    guard = StockGuard(cur, True)
    cur.execute("UPDATE food_consumption_transactions SET status = 'Voided' WHERE id = %s", (consumption_id,))
    preparation = set()
    for item in items:
        if item["purpose"] == "PREPARATION":
            preparation.add(item["product_id"])
            continue
        cur.execute(
            """SELECT COALESCE(-SUM(quantity), 0) AS q, COALESCE(-SUM(value), 0) AS v FROM food_inventory_transactions
               WHERE business_id = %s AND source_type = 'consumption_item' AND source_id = %s""",
            (g.business_id, item["id"]),
        )
        taken = cur.fetchone()
        post_ledger(cur, item["product_id"], item["purpose"], taken["q"], taken["v"], header["transaction_date"],
                    "consumption_item", item["id"], "Entry voided")
    for product_id in preparation:
        reconcile_usage(cur, products[product_id], header["transaction_date"], "CONSUMPTION", "consumption", consumption_id, guard)
    return {"voided": True}, 200


# ---------------------------------------------------------------------------
# Stock adjustments
# ---------------------------------------------------------------------------
def create_adjustment(cur, payload, allow_negative=None):
    on_date = txn_date(payload)
    product = lock_products(cur, [payload.get("productId")])[int_id(payload.get("productId"), "Product")]
    direction = choice(payload, "direction", ("IN", "OUT"), label="Direction")
    reason = text(payload, "reason", 300, required=True, label="Reason")
    entered = number(payload, "quantity", required=True, positive=True, label="Quantity")
    stock_qty, unit = to_stock_qty(cur, product, entered, payload.get("unitId"))
    guard = StockGuard(cur, payload.get("allowNegative") if allow_negative is None else allow_negative)
    cur.execute(
        """INSERT INTO food_stock_adjustments (business_id, user_id, transaction_date, product_id, direction, entered_qty, unit_id, stock_qty, reason)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (*_who(on_date), product["id"], direction, entered, unit["id"], stock_qty, reason),
    )
    adjustment = cur.fetchone()
    if direction == "IN":
        qty, val = balance(cur, product["id"])
        rate = average_cost(qty, val, product["purchase_price"])
        post_ledger(cur, product["id"], "ADJUST_IN", stock_qty, stock_qty * rate, on_date, "adjustment", adjustment["id"], reason)
    else:
        post_stock_out(cur, product, "ADJUST_OUT", stock_qty, on_date, "adjustment", adjustment["id"], guard, reason)
    guard.enforce()
    return {"adjustment": camel(adjustment), "alerts": stock_alerts(cur, [product["id"]])}


@foodseller_bp.post("/adjustments")
@idempotent
def post_adjustment(cur):
    return create_adjustment(cur, body()), 201


@foodseller_bp.get("/adjustments")
def list_adjustments():
    start, end = date_range(default_from=today_ist().replace(day=1))
    with tx() as cur:
        cur.execute(
            """SELECT a.*, p.name AS product_name, u.name AS unit_name FROM food_stock_adjustments a
               JOIN food_inventory_products p ON p.id = a.product_id JOIN food_units u ON u.id = a.unit_id
               WHERE a.business_id = %s AND a.transaction_date BETWEEN %s AND %s ORDER BY a.transaction_date DESC, a.id DESC LIMIT 200""",
            (g.business_id, start, end),
        )
        return ok({"adjustments": [camel(r) for r in cur.fetchall()]})


@foodseller_bp.post("/adjustments/<int:adjustment_id>/void")
@idempotent
def void_adjustment(cur, adjustment_id):
    payload = body()
    row = _load_for_void(cur, "food_stock_adjustments", adjustment_id, "Adjustment")
    products = lock_products(cur, [row["product_id"]])
    guard = StockGuard(cur, payload.get("allowNegative"))
    cur.execute(
        """SELECT txn_type, quantity, value FROM food_inventory_transactions
           WHERE business_id = %s AND source_type = 'adjustment' AND source_id = %s""",
        (g.business_id, adjustment_id),
    )
    original = cur.fetchone()
    post_ledger(cur, row["product_id"], original["txn_type"], -original["quantity"], -original["value"], row["transaction_date"],
                "adjustment", adjustment_id, "Adjustment voided")
    if original["quantity"] > 0:
        guard.note_reduction(products[row["product_id"]])
    guard.enforce()
    cur.execute("UPDATE food_stock_adjustments SET status = 'Voided' WHERE id = %s", (adjustment_id,))
    return {"voided": True}, 200


# ---------------------------------------------------------------------------
# Food products and recipes
# ---------------------------------------------------------------------------
def _food_details(cur, foods):
    """Adds preparation cost, profit per unit and how many can be made from current stock."""
    ids = [f["id"] for f in foods]
    cur.execute(
        """SELECT r.food_product_id, r.yield_qty, r.notes AS recipe_notes, i.id AS ingredient_id, i.product_id, i.quantity, i.unit_id,
                  iu.name AS unit_name, iu.dimension, iu.to_base,
                  p.name AS product_name, p.purchase_price, p.unit_id AS stock_unit_id, su.name AS stock_unit_name, su.dimension AS stock_dimension,
                  su.to_base AS stock_to_base, COALESCE(l.qty, 0) AS stock_qty, COALESCE(l.val, 0) AS stock_value
           FROM food_recipes r
           LEFT JOIN food_recipe_ingredients i ON i.recipe_id = r.id
           LEFT JOIN food_units iu ON iu.id = i.unit_id
           LEFT JOIN food_inventory_products p ON p.id = i.product_id
           LEFT JOIN food_units su ON su.id = p.unit_id
           LEFT JOIN (SELECT product_id, SUM(quantity) AS qty, SUM(value) AS val FROM food_inventory_transactions
                      WHERE business_id = %(b)s GROUP BY product_id) l ON l.product_id = i.product_id
           WHERE r.business_id = %(b)s AND r.food_product_id = ANY(%(ids)s) ORDER BY i.id""",
        {"b": g.business_id, "ids": ids},
    )
    recipes = {}
    for r in cur.fetchall():
        recipes.setdefault(r["food_product_id"], []).append(r)
    for food in foods:
        selling = dec(food.get("sellingPrice") or 0)
        rows = recipes.get(food["id"], [])
        ingredients, unit_cost, can_make = [], ZERO, None
        if food["productType"] == "PACKAGED":
            if food.get("packagedProductId"):
                unit_cost = dec(food.get("packagedQty") or 1) * average_cost(food.get("packagedStockQty") or 0, food.get("packagedStockValue") or 0, food.get("packagedPurchasePrice") or 0)
                can_make = int(dec(food.get("packagedStockQty") or 0) // dec(food.get("packagedQty") or 1))
                can_make = max(can_make, 0)
        else:
            for r in rows:
                if r["ingredient_id"] is None:
                    continue
                per_unit = convert_qty(r["quantity"], {"id": r["unit_id"], "name": r["unit_name"], "dimension": r["dimension"], "to_base": r["to_base"]},
                                       {"id": r["stock_unit_id"], "name": r["stock_unit_name"], "dimension": r["stock_dimension"], "to_base": r["stock_to_base"]}) / r["yield_qty"]
                rate = average_cost(r["stock_qty"], r["stock_value"], r["purchase_price"])
                cost = per_unit * rate
                unit_cost += cost
                if per_unit > 0:
                    possible = max(int(dec(r["stock_qty"]) // per_unit), 0)
                    can_make = possible if can_make is None else min(can_make, possible)
                ingredients.append({"id": r["ingredient_id"], "productId": r["product_id"], "productName": r["product_name"], "quantity": r["quantity"],
                                    "unitId": r["unit_id"], "unitName": r["unit_name"], "stockUnitName": r["stock_unit_name"], "costPerFoodUnit": cost})
        food["recipe"] = {"yieldQty": rows[0]["yield_qty"], "notes": rows[0]["recipe_notes"], "ingredients": ingredients} if rows else None
        food["preparationCost"] = unit_cost
        food["profitPerUnit"] = selling - unit_cost
        food["availableQty"] = can_make
        food["hasCostSource"] = bool(ingredients) or bool(food.get("packagedProductId"))
    return foods


FOOD_SELECT = """
    SELECT f.*, c.name AS category_name, pk.name AS packaged_product_name, pk.purchase_price AS packaged_purchase_price,
           COALESCE(l.qty, 0) AS packaged_stock_qty, COALESCE(l.val, 0) AS packaged_stock_value
    FROM food_products f
    LEFT JOIN food_product_categories c ON c.id = f.category_id
    LEFT JOIN food_inventory_products pk ON pk.id = f.packaged_product_id
    LEFT JOIN (SELECT product_id, SUM(quantity) AS qty, SUM(value) AS val FROM food_inventory_transactions
               WHERE business_id = %(b)s GROUP BY product_id) l ON l.product_id = f.packaged_product_id
    WHERE f.business_id = %(b)s AND f.deleted_at IS NULL
"""


def load_foods(cur, status=None):
    sql = FOOD_SELECT + (" AND f.status = %(s)s" if status else "") + " ORDER BY f.name"
    cur.execute(sql, {"b": g.business_id, "s": status})
    return _food_details(cur, [camel(r) for r in cur.fetchall()])


@foodseller_bp.get("/food-products")
def list_foods():
    with tx() as cur:
        foods = load_foods(cur, request.args.get("status"))
    query = (request.args.get("q") or "").strip().lower()
    if query:
        foods = [f for f in foods if query in f["name"].lower()]
    return ok({"foods": foods})


def _load_food(cur, food_id):
    cur.execute(FOOD_SELECT + " AND f.id = %(f)s", {"b": g.business_id, "f": food_id})
    row = cur.fetchone()
    if not row:
        raise ApiError(404, "Food product not found")
    return _food_details(cur, [camel(row)])[0]


def _food_values(cur, payload):
    product_type = choice(payload, "productType", ("RECIPE", "PACKAGED"), default="RECIPE", label="Type")
    packaged_id = None
    if product_type == "PACKAGED":
        packaged_id = owned(cur, "food_inventory_products", int_id(payload.get("packagedProductId"), "Stock product to deduct"), "Stock product")
    return {
        "name": text(payload, "name", 120, required=True, label="Food product name"),
        "category_id": owned(cur, "food_product_categories", int_id(payload.get("categoryId"), required=False), "Category"),
        "product_type": product_type,
        "selling_price": number(payload, "sellingPrice", required=True, label="Selling price", limit=MAX_MONEY),
        "packaged_product_id": packaged_id,
        "packaged_qty": number(payload, "packagedQty", positive=True, default="1", label="Quantity per sale"),
        "status": choice(payload, "status", ("Active", "Inactive"), default="Active"),
    }


def _save_recipe(cur, food_id, recipe):
    """Replaces the recipe. Past sales keep the usage frozen when they were recorded."""
    yield_qty = number(recipe, "yieldQty", required=True, positive=True, label="Recipe makes (quantity)")
    ingredients = recipe.get("ingredients") or []
    if not isinstance(ingredients, list) or not all(isinstance(i, dict) for i in ingredients):
        raise ApiError(422, "Ingredients must be a list")
    seen, prepared = set(), []
    products = lock_products(cur, [i.get("productId") for i in ingredients]) if ingredients else {}
    for ingredient in ingredients:
        product = products[int_id(ingredient.get("productId"), "Ingredient")]
        if product["id"] in seen:
            raise ApiError(422, f"{product['name']} is listed twice in this recipe")
        seen.add(product["id"])
        quantity = number(ingredient, "quantity", required=True, positive=True, label=f"{product['name']} quantity")
        _, unit = to_stock_qty(cur, product, quantity, ingredient.get("unitId"))
        prepared.append((product["id"], quantity, unit["id"]))
    cur.execute(
        """INSERT INTO food_recipes (business_id, user_id, transaction_date, food_product_id, yield_qty, notes)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (food_product_id) DO UPDATE SET yield_qty = EXCLUDED.yield_qty, notes = EXCLUDED.notes RETURNING id""",
        (*_who(), food_id, yield_qty, text(recipe, "notes", 300)),
    )
    recipe_id = cur.fetchone()["id"]
    cur.execute("DELETE FROM food_recipe_ingredients WHERE recipe_id = %s AND business_id = %s", (recipe_id, g.business_id))
    for product_id, quantity, unit_id in prepared:
        cur.execute(
            """INSERT INTO food_recipe_ingredients (business_id, user_id, transaction_date, recipe_id, product_id, quantity, unit_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (*_who(), recipe_id, product_id, quantity, unit_id),
        )


@foodseller_bp.post("/food-products")
def create_food():
    payload = body()
    with tx() as cur:
        values = _food_values(cur, payload)
        row = _insert_returning(cur, "food_products", tuple(values.keys()), tuple(values.values()))
        if isinstance(payload.get("recipe"), dict) and values["product_type"] == "RECIPE":
            _save_recipe(cur, row["id"], payload["recipe"])
        return ok({"food": _load_food(cur, row["id"])}, 201)


@foodseller_bp.get("/food-products/<int:food_id>")
def get_food(food_id):
    with tx() as cur:
        return ok({"food": _load_food(cur, food_id)})


@foodseller_bp.put("/food-products/<int:food_id>")
def update_food(food_id):
    payload = body()
    with tx() as cur:
        owned(cur, "food_products", food_id, "Food product")
        values = _food_values(cur, payload)
        assignments = ", ".join(f"{k} = %s" for k in values)
        cur.execute(f"UPDATE food_products SET {assignments} WHERE id = %s AND business_id = %s", (*values.values(), food_id, g.business_id))
        if isinstance(payload.get("recipe"), dict) and values["product_type"] == "RECIPE":
            _save_recipe(cur, food_id, payload["recipe"])
        return ok({"food": _load_food(cur, food_id)})


@foodseller_bp.put("/food-products/<int:food_id>/recipe")
def put_recipe(food_id):
    with tx() as cur:
        owned(cur, "food_products", food_id, "Food product")
        _save_recipe(cur, food_id, body())
        return ok({"food": _load_food(cur, food_id)})


@foodseller_bp.delete("/food-products/<int:food_id>")
def delete_food(food_id):
    with tx() as cur:
        owned(cur, "food_products", food_id, "Food product")
        # Sold food keeps its row so bills still resolve; it just leaves the menu.
        cur.execute("UPDATE food_products SET deleted_at = NOW(), status = 'Inactive' WHERE id = %s AND business_id = %s", (food_id, g.business_id))
    return ok({"deleted": True})


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------
def _food_usage_per_unit(cur, food):
    """{stock product id: stock quantity needed for one unit sold}."""
    if food["product_type"] == "PACKAGED":
        return ({food["packaged_product_id"]: food["packaged_qty"]}) if food["packaged_product_id"] else {}
    cur.execute(
        """SELECT i.product_id, i.quantity, i.unit_id, r.yield_qty FROM food_recipes r JOIN food_recipe_ingredients i ON i.recipe_id = r.id
           WHERE r.business_id = %s AND r.food_product_id = %s""",
        (g.business_id, food["id"]),
    )
    return [dict(r) for r in cur.fetchall()]


@foodseller_bp.post("/sales")
@idempotent
def post_sale(cur):
    payload = body()
    on_date = txn_date(payload)
    lines = lines_of(payload, label="the sale")
    food_ids = sorted({int_id(line.get("foodProductId"), "Food product") for line in lines})
    cur.execute("SELECT * FROM food_products WHERE business_id = %s AND id = ANY(%s) AND deleted_at IS NULL", (g.business_id, food_ids))
    foods = {r["id"]: r for r in cur.fetchall()}
    if len(foods) != len(food_ids):
        raise ApiError(404, "A selected food product was not found")

    prepared, subtotal = [], ZERO
    for line in lines:
        food = foods[int_id(line.get("foodProductId"))]
        if food["status"] != "Active":
            raise ApiError(409, f"{food['name']} is inactive")
        quantity = number(line, "quantity", required=True, positive=True, label=f"{food['name']} quantity")
        price = number(line, "price", default=food["selling_price"], label=f"{food['name']} price", limit=MAX_MONEY)
        line_total = money2(quantity * price)
        prepared.append((food, quantity, price, line_total))
        subtotal += line_total

    discount = money2(number(payload, "discount", label="Discount", limit=MAX_MONEY))
    if discount > subtotal:
        raise ApiError(422, "Discount is more than the bill total")
    total = subtotal - discount
    mode = payload.get("paymentMode")
    if mode:
        choice(payload, "paymentMode", ("Cash", "UPI", "Credit"), label="Payment mode")
        cash, upi, credit = (total if mode == "Cash" else ZERO), (total if mode == "UPI" else ZERO), (total if mode == "Credit" else ZERO)
    else:
        cash = money2(number(payload, "cashAmount", label="Cash amount", limit=MAX_MONEY))
        upi = money2(number(payload, "upiAmount", label="UPI amount", limit=MAX_MONEY))
        credit = money2(number(payload, "creditAmount", label="Credit amount", limit=MAX_MONEY))
    if cash + upi + credit != total:
        raise ApiError(422, f"Cash + UPI + Credit must equal the bill total of {total}")
    customer = text(payload, "customerName", 120)
    if credit > 0 and not customer:
        raise ApiError(422, "Enter the customer name for a credit sale, so it can be collected later")

    # Everything this bill needs from stock, locked before anything is read or written.
    usage_by_line, needed_ids = [], set()
    for food, quantity, _, _ in prepared:
        per_unit = _food_usage_per_unit(cur, food)
        usage_by_line.append(per_unit)
        needed_ids.update(per_unit.keys() if isinstance(per_unit, dict) else [r["product_id"] for r in per_unit])
    products = lock_products(cur, needed_ids) if needed_ids else {}
    guard = StockGuard(cur, payload.get("allowNegative"))

    cur.execute(
        """INSERT INTO food_sales_transactions (business_id, user_id, transaction_date, customer_name, subtotal, discount, total_amount, cash_amount, upi_amount, credit_amount, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (*_who(on_date), customer, subtotal, discount, total, cash, upi, credit, text(payload, "notes", 300)),
    )
    sale = cur.fetchone()
    shares = allocate_discount([p[3] for p in prepared], discount)
    no_cost_source = []
    for (food, quantity, price, line_total), share, per_unit in zip(prepared, shares, usage_by_line):
        cur.execute(
            """INSERT INTO food_sales_items (business_id, user_id, transaction_date, sale_id, food_product_id, food_name, quantity, price, total, discount_share, has_cost_source)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (*_who(on_date), sale["id"], food["id"], food["name"], quantity, price, line_total, share, bool(per_unit)),
        )
        item_id = cur.fetchone()["id"]
        if not per_unit:
            no_cost_source.append(food["name"])
        required = {}
        if isinstance(per_unit, dict):
            required = {pid: qty4(q * quantity) for pid, q in per_unit.items()}
        else:
            for r in per_unit:
                product = products[r["product_id"]]
                stock_qty, _ = to_stock_qty(cur, product, r["quantity"], r["unit_id"])
                required[r["product_id"]] = qty4(stock_qty * quantity / r["yield_qty"])
        for product_id, required_qty in required.items():
            if required_qty > 0:
                cur.execute(
                    """INSERT INTO food_sales_item_usage (business_id, user_id, transaction_date, sale_id, sale_item_id, product_id, required_qty)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (*_who(on_date), sale["id"], item_id, product_id, required_qty),
                )
    for product in products.values():
        reconcile_usage(cur, product, on_date, "SALE_USAGE", "sale", sale["id"], guard)
    guard.enforce()
    return {"sale": camel(sale), "noRecipe": no_cost_source, "alerts": stock_alerts(cur, products.keys())}, 201


SALE_SELECT = """
    SELECT h.*, h.credit_amount - COALESCE((SELECT SUM(x.amount) FROM food_payments x
        WHERE x.sale_id = h.id AND x.status = 'Posted'), 0) AS pending
    FROM food_sales_transactions h WHERE h.business_id = %(b)s
"""


def _attach_sale_items(cur, sales):
    cur.execute(
        """SELECT sale_id, id, food_product_id, food_name, quantity, price, total, discount_share, has_cost_source
           FROM food_sales_items WHERE business_id = %s AND sale_id = ANY(%s) ORDER BY id""",
        (g.business_id, [s["id"] for s in sales]),
    )
    by_header = {}
    for r in cur.fetchall():
        by_header.setdefault(r["sale_id"], []).append(camel(r))
    for sale in sales:
        sale["items"] = by_header.get(sale["id"], [])
    return sales


@foodseller_bp.get("/sales")
def list_sales():
    start, end = date_range(default_from=today_ist().replace(day=1))
    size, offset, page = page_args()
    with tx() as cur:
        cur.execute(
            SALE_SELECT + " AND h.transaction_date BETWEEN %(s)s AND %(e)s ORDER BY h.transaction_date DESC, h.id DESC LIMIT %(l)s OFFSET %(o)s",
            {"b": g.business_id, "s": start, "e": end, "l": size + 1, "o": offset},
        )
        rows = [camel(r) for r in cur.fetchall()]
        sales = _attach_sale_items(cur, rows[:size])
    return ok({"sales": sales, "hasMore": len(rows) > size, "page": page})


@foodseller_bp.get("/sales/<int:sale_id>")
def get_sale(sale_id):
    with tx() as cur:
        cur.execute(SALE_SELECT + " AND h.id = %(i)s", {"b": g.business_id, "i": sale_id})
        row = cur.fetchone()
        if not row:
            raise ApiError(404, "Sale not found")
        return ok({"sale": _attach_sale_items(cur, [camel(row)])[0]})


@foodseller_bp.post("/sales/<int:sale_id>/void")
@idempotent
def void_sale(cur, sale_id):
    sale = _load_for_void(cur, "food_sales_transactions", sale_id, "Sale")
    _assert_no_payments(cur, "sale_id", sale_id)
    cur.execute("SELECT DISTINCT product_id FROM food_sales_item_usage WHERE sale_id = %s AND business_id = %s", (sale_id, g.business_id))
    product_ids = [r["product_id"] for r in cur.fetchall()]
    products = lock_products(cur, product_ids) if product_ids else {}
    cur.execute("UPDATE food_sales_transactions SET status = 'Voided' WHERE id = %s", (sale_id,))
    guard = StockGuard(cur, True)
    for product in products.values():
        reconcile_usage(cur, product, sale["transaction_date"], "SALE_USAGE", "sale", sale_id, guard)
    return {"voided": True}, 200


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------
def create_expense(cur, payload):
    on_date = txn_date(payload)
    category_id = int_id(payload.get("categoryId"), "Expense category")
    cur.execute("SELECT * FROM food_expense_categories WHERE id = %s AND business_id = %s", (category_id, g.business_id))
    category = cur.fetchone()
    if not category:
        raise ApiError(404, "Expense category not found")
    counts = payload.get("countsInPnl")
    cur.execute(
        """INSERT INTO food_expense_transactions (business_id, user_id, transaction_date, category_id, amount, payment_method, counts_in_pnl, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (*_who(on_date), category_id, money2(number(payload, "amount", required=True, positive=True, label="Amount", limit=MAX_MONEY)),
         choice(payload, "paymentMethod", ("Cash", "UPI", "Credit"), default="Cash"),
         category["counts_in_pnl"] if counts is None else bool(counts), text(payload, "notes", 300)),
    )
    return {"expense": camel(cur.fetchone())}


@foodseller_bp.post("/expenses")
@idempotent
def post_expense(cur):
    return create_expense(cur, body()), 201


@foodseller_bp.get("/expenses")
def list_expenses():
    start, end = date_range(default_from=today_ist().replace(day=1))
    size, offset, page = page_args()
    with tx() as cur:
        cur.execute(
            """SELECT e.*, c.name AS category_name FROM food_expense_transactions e JOIN food_expense_categories c ON c.id = e.category_id
               WHERE e.business_id = %s AND e.transaction_date BETWEEN %s AND %s AND e.status = 'Posted'
               ORDER BY e.transaction_date DESC, e.id DESC LIMIT %s OFFSET %s""",
            (g.business_id, start, end, size + 1, offset),
        )
        rows = [camel(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT COALESCE(SUM(amount) FILTER (WHERE transaction_date = %(d)s), 0) AS day_total,
                      COALESCE(SUM(amount) FILTER (WHERE date_trunc('month', transaction_date) = date_trunc('month', %(d)s::date)), 0) AS month_total,
                      COALESCE(SUM(amount) FILTER (WHERE transaction_date = %(d)s AND NOT counts_in_pnl), 0) AS day_excluded
               FROM food_expense_transactions WHERE business_id = %(b)s AND status = 'Posted'""",
            {"b": g.business_id, "d": end},
        )
        totals = camel(cur.fetchone())
    return ok({"expenses": rows[:size], "totals": totals, "hasMore": len(rows) > size, "page": page})


@foodseller_bp.put("/expenses/<int:expense_id>")
def update_expense(expense_id):
    payload = body()
    with tx() as cur:
        current = _load_for_void(cur, "food_expense_transactions", expense_id, "Expense")
        category_id = owned(cur, "food_expense_categories", int_id(payload.get("categoryId"), "Expense category"), "Expense category")
        counts = payload.get("countsInPnl")
        cur.execute(
            """UPDATE food_expense_transactions SET category_id = %s, amount = %s, payment_method = %s, counts_in_pnl = %s, notes = %s, transaction_date = %s
               WHERE id = %s AND business_id = %s RETURNING *""",
            (category_id, money2(number(payload, "amount", required=True, positive=True, label="Amount", limit=MAX_MONEY)),
             choice(payload, "paymentMethod", ("Cash", "UPI", "Credit"), default="Cash"),
             current["counts_in_pnl"] if counts is None else bool(counts), text(payload, "notes", 300), txn_date(payload), expense_id, g.business_id),
        )
        return ok({"expense": camel(cur.fetchone())})


@foodseller_bp.delete("/expenses/<int:expense_id>")
def delete_expense(expense_id):
    with tx() as cur:
        _load_for_void(cur, "food_expense_transactions", expense_id, "Expense")
        cur.execute("UPDATE food_expense_transactions SET status = 'Voided' WHERE id = %s", (expense_id,))
    return ok({"deleted": True})


# ---------------------------------------------------------------------------
# Pending payments: credit sales to collect, supplier dues to pay
# ---------------------------------------------------------------------------
@foodseller_bp.get("/payments/pending")
def pending_payments():
    with tx() as cur:
        cur.execute(SALE_SELECT + " AND h.status = 'Posted' AND h.credit_amount > 0 ORDER BY h.transaction_date", {"b": g.business_id})
        sales = [camel(r) for r in cur.fetchall() if r["pending"] > 0]
        cur.execute(PURCHASE_SELECT + " AND h.status = 'Posted' AND h.total_amount > h.amount_paid ORDER BY h.transaction_date", {"b": g.business_id})
        purchases = [camel(r) for r in cur.fetchall() if r["due"] > 0]
    return ok({"receivable": sales, "payable": purchases,
               "receivableTotal": sum(dec(s["pending"]) for s in sales), "payableTotal": sum(dec(p["due"]) for p in purchases)})


@foodseller_bp.post("/payments")
@idempotent
def post_payment(cur):
    payload = body()
    direction = choice(payload, "direction", ("RECEIVED", "PAID"), label="Direction")
    amount = money2(number(payload, "amount", required=True, positive=True, label="Amount", limit=MAX_MONEY))
    method = choice(payload, "paymentMethod", ("Cash", "UPI"), default="Cash", label="Payment method")
    on_date = txn_date(payload)
    if direction == "RECEIVED":
        sale_id, purchase_id = int_id(payload.get("saleId"), "Sale"), None
        cur.execute("SELECT id FROM food_sales_transactions WHERE id = %s AND business_id = %s AND status = 'Posted' FOR UPDATE", (sale_id, g.business_id))
        if not cur.fetchone():
            raise ApiError(404, "Sale not found")
        cur.execute(SALE_SELECT + " AND h.id = %(i)s", {"b": g.business_id, "i": sale_id})
        outstanding = cur.fetchone()["pending"]
    else:
        sale_id, purchase_id = None, int_id(payload.get("purchaseId"), "Purchase")
        cur.execute("SELECT id FROM food_purchase_transactions WHERE id = %s AND business_id = %s AND status = 'Posted' FOR UPDATE", (purchase_id, g.business_id))
        if not cur.fetchone():
            raise ApiError(404, "Purchase not found")
        cur.execute(PURCHASE_SELECT + " AND h.id = %(p)s", {"b": g.business_id, "p": purchase_id})
        outstanding = cur.fetchone()["due"]
    if amount > outstanding:
        raise ApiError(422, f"Only {outstanding} is pending on this entry")
    cur.execute(
        """INSERT INTO food_payments (business_id, user_id, transaction_date, direction, sale_id, purchase_id, amount, payment_method, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (*_who(on_date), direction, sale_id, purchase_id, amount, method, text(payload, "notes", 300)),
    )
    return {"payment": camel(cur.fetchone())}, 201


@foodseller_bp.get("/payments")
def list_payments():
    start, end = date_range(default_from=today_ist().replace(day=1))
    with tx() as cur:
        cur.execute(
            "SELECT * FROM food_payments WHERE business_id = %s AND transaction_date BETWEEN %s AND %s ORDER BY id DESC LIMIT 200",
            (g.business_id, start, end),
        )
        return ok({"payments": [camel(r) for r in cur.fetchall()]})


@foodseller_bp.post("/payments/<int:payment_id>/void")
@idempotent
def void_payment(cur, payment_id):
    _load_for_void(cur, "food_payments", payment_id, "Payment")
    cur.execute("UPDATE food_payments SET status = 'Voided' WHERE id = %s", (payment_id,))
    return {"voided": True}, 200


# Register the remaining routes on the same blueprint.
import foodseller_reports  # noqa: E402,F401
import foodseller_ai  # noqa: E402,F401

__all__ = ["foodseller_bp", "stock_unit", "CoreError"]
