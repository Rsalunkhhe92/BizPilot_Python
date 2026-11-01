"""
Food Seller API - shared plumbing: blueprint, tenant check, transactions, idempotency
and the stock ledger engine.

The bearer token decides the business; a request can never name another one, and
every query filters on business_id. tx() is one database transaction per request,
so a sale, its recipe usage and its stock deductions commit together or not at all.
"""

import json
import os
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps

import psycopg
from flask import Blueprint, g, jsonify, request
from psycopg.rows import dict_row

from auth_token import verify_token
from foodseller_core import ZERO, CoreError, average_cost, convert_qty, dec, money2, qty4, usage_target
from foodseller_service import BUSINESS_TYPE_NAME, FOOD_SELLER_FEATURES

foodseller_bp = Blueprint("foodseller", __name__, url_prefix="/api/foodseller")

IST = timezone(timedelta(hours=5, minutes=30))
MAX_QTY = Decimal("999999999")
MAX_MONEY = Decimal("9999999999")
MAX_LINES = 200


class ApiError(Exception):
    def __init__(self, status, message, details=None, extra=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details
        self.extra = extra or {}


@foodseller_bp.errorhandler(ApiError)
def _api_error(err):
    payload = {"error": err.message, **err.extra}
    if err.details:
        payload["details"] = err.details
    return jsonify(jsonable(payload)), err.status


@foodseller_bp.errorhandler(CoreError)
def _core_error(err):
    return jsonify({"error": str(err)}), 422


@foodseller_bp.errorhandler(psycopg.errors.UniqueViolation)
def _unique_error(err):
    constraint = getattr(getattr(err, "diag", None), "constraint_name", "") or ""
    messages = {
        "uq_food_inv_product_name": "A stock product with this name already exists",
        "uq_food_product_name": "A food product with this name already exists",
        "uq_food_unit_name": "A unit with this name already exists",
        "uq_food_category_name": "A category with this name already exists",
        "uq_food_supplier_name": "A supplier with this name already exists",
        "uq_food_expense_category_name": "An expense category with this name already exists",
        "food_recipe_ingredients_recipe_id_product_id_key": "An ingredient is listed twice in this recipe",
    }
    return jsonify({"error": messages.get(constraint, "This record already exists")}), 409


@foodseller_bp.errorhandler(psycopg.errors.ForeignKeyViolation)
def _fk_error(err):
    return jsonify({"error": "This record is in use, or refers to something that does not exist"}), 409


@foodseller_bp.errorhandler(psycopg.errors.CheckViolation)
def _check_error(err):
    return jsonify({"error": "The values entered are not allowed for this record"}), 422


@foodseller_bp.errorhandler(psycopg.Error)
def _db_error(err):
    # Never echo SQL or data back to the client.
    print(f"Food Seller database error: {type(err).__name__}")
    return jsonify({"error": "Database is unavailable or rejected the request. Please try again."}), 503


def database_url():
    url = os.getenv("FOODSELLER_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise ApiError(503, "Database is not configured")
    return url


@contextmanager
def tx():
    conn = psycopg.connect(database_url(), options="-c timezone=Asia/Kolkata", row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


SECTION_FEATURES = {
    "products": "food_inventory", "units": "food_inventory", "categories": "food_inventory",
    "suppliers": "food_inventory", "adjustments": "food_inventory",
    "purchases": "food_purchases", "consumption": "food_consumption", "food-products": "food_menu",
    "sales": "food_sales", "payments": "food_sales", "expenses": "food_expenses",
    "expense-categories": "food_expenses", "reports": "food_reports", "pnl": "food_reports", "ai": "food_ai",
}


def is_food_seller(business_type, features):
    return (business_type or "").strip().lower() == BUSINESS_TYPE_NAME.lower() or any(f in FOOD_SELLER_FEATURES for f in features)


@foodseller_bp.before_request
def require_food_tenant():
    if request.method == "OPTIONS":
        return None
    header = request.headers.get("Authorization", "")
    user_id = verify_token(header[7:].strip()) if header.startswith("Bearer ") else None
    if user_id is None:
        return jsonify({"error": "Session expired. Please log in again."}), 401
    with tx() as cur:
        cur.execute("SELECT id, user_type, status, business_type, enabled_features FROM userdetails WHERE id = %s", (user_id,))
        user = cur.fetchone()
    if not user or user["user_type"] != "customer":
        return jsonify({"error": "Account not found"}), 403
    if user["status"] != "active":
        return jsonify({"error": "Your account is not active"}), 403
    features = user["enabled_features"] if isinstance(user["enabled_features"], list) else []
    if not is_food_seller(user["business_type"], features):
        return jsonify({"error": "Food Seller is not enabled for this account"}), 403
    # An explicit feature list set by the admin wins; none means the business type's defaults.
    explicit = [f for f in features if f in FOOD_SELLER_FEATURES]
    enabled = set(explicit) if explicit else set(FOOD_SELLER_FEATURES)
    segment = request.path[len("/api/foodseller/"):].split("/", 1)[0]
    needed = SECTION_FEATURES.get(segment)
    if needed and needed not in enabled:
        return jsonify({"error": "This module is not enabled for your account"}), 403
    g.business_id = user_id
    g.user_id = user_id
    g.food_features = sorted(enabled)
    return None


# ---------------------------------------------------------------------------
# Input and output helpers
# ---------------------------------------------------------------------------
def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def today_ist():
    return datetime.now(IST).date()


def text(payload, key, max_len=200, required=False, label=None):
    value = str(payload.get(key) if payload.get(key) is not None else "").strip()
    if required and not value:
        raise ApiError(422, f"{label or key} is required")
    if len(value) > max_len:
        raise ApiError(422, f"{label or key} is too long (max {max_len} characters)")
    return value or None


def choice(payload, key, allowed, default=None, label=None):
    value = payload.get(key)
    if value in (None, ""):
        if default is None:
            raise ApiError(422, f"{label or key} is required")
        return default
    if value not in allowed:
        raise ApiError(422, f"{label or key} must be one of: {', '.join(allowed)}")
    return value


def parse_date(value, default=None, label="Date", allow_future=False):
    if value in (None, ""):
        return default
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ApiError(422, f"{label} must be a date (YYYY-MM-DD)")
    if not allow_future and parsed > today_ist():
        raise ApiError(422, f"{label} cannot be in the future")
    return parsed


def txn_date(payload):
    return parse_date(payload.get("date"), default=today_ist())


def number(payload, key, required=False, positive=False, default="0", label=None, limit=MAX_QTY):
    value = payload.get(key)
    if value in (None, ""):
        if required:
            raise ApiError(422, f"{label or key} is required")
        return dec(default)
    try:
        parsed = dec(value, label or key)
    except CoreError:
        raise ApiError(422, f"{label or key} must be a number")
    if parsed < 0 or (positive and parsed <= 0):
        raise ApiError(422, f"{label or key} must be greater than zero")
    if parsed > limit:
        raise ApiError(422, f"{label or key} is too large")
    return parsed


def int_id(value, label="id", required=True):
    if value in (None, ""):
        if required:
            raise ApiError(422, f"{label} is required")
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(422, f"{label} must be a valid id")


def lines_of(payload, key="items", label="items"):
    lines = payload.get(key)
    if not isinstance(lines, list) or not lines:
        raise ApiError(422, f"Add at least one row to {label}")
    if len(lines) > MAX_LINES:
        raise ApiError(422, f"Too many rows (max {MAX_LINES})")
    if not all(isinstance(line, dict) for line in lines):
        raise ApiError(422, f"Each row in {label} must be an object")
    return lines


def page_args(default_size=30, max_size=100):
    try:
        size = min(max(int(request.args.get("pageSize") or default_size), 1), max_size)
        page = max(int(request.args.get("page") or 1), 1)
    except ValueError:
        raise ApiError(422, "page and pageSize must be whole numbers")
    return size, (page - 1) * size, page


def date_range(default_from=None, default_to=None):
    """from/to, or month=YYYY-MM. Defaults to today."""
    args = request.args
    month = args.get("month")
    if month:
        if not re.match(r"^\d{4}-\d{2}$", month):
            raise ApiError(422, "month must be YYYY-MM")
        try:
            start = date(int(month[:4]), int(month[5:7]), 1)
        except ValueError:
            raise ApiError(422, "month must be YYYY-MM")
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start, next_month - timedelta(days=1)
    today = today_ist()
    end = parse_date(args.get("to"), default=default_to or today, label="To date", allow_future=True)
    start = parse_date(args.get("from"), default=default_from or end, label="From date", allow_future=True)
    if start > end:
        raise ApiError(422, "From date is after To date")
    if (end - start).days > 1830:
        raise ApiError(422, "Date range is too long (max 5 years)")
    return start, end


def jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return value


_CAMEL = re.compile(r"_([a-z0-9])")


def camel(row):
    if row is None:
        return None
    return {_CAMEL.sub(lambda m: m.group(1).upper(), k): jsonable(v) for k, v in row.items()}


def ok(payload, status=200):
    return jsonify(jsonable(payload)), status


def idempotent(fn):
    """Money and stock writes need an Idempotency-Key. The key row commits with the
    posting: a retry after a dropped connection returns the original response, and a
    failed attempt leaves nothing behind so it can run again."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        key = (request.headers.get("Idempotency-Key") or "").strip()
        if not re.match(r"^[A-Za-z0-9_-]{8,64}$", key):
            raise ApiError(400, "Idempotency-Key header (8-64 letters/digits) is required for this action")
        with tx() as cur:
            cur.execute(
                """INSERT INTO food_request_keys (business_id, user_id, transaction_date, idempotency_key, endpoint)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
                (g.business_id, g.user_id, today_ist(), key, request.path[:120]),
            )
            if cur.fetchone() is None:
                cur.execute(
                    "SELECT endpoint, status_code, response FROM food_request_keys WHERE business_id = %s AND idempotency_key = %s",
                    (g.business_id, key),
                )
                prior = cur.fetchone()
                if prior["endpoint"] != request.path[:120]:
                    raise ApiError(409, "This Idempotency-Key was already used for a different action")
                return jsonify({**(prior["response"] or {}), "replayed": True}), prior["status_code"] or 200
            result, status = fn(cur, *args, **kwargs)
            result = jsonable(result)
            cur.execute(
                "UPDATE food_request_keys SET status_code = %s, response = %s WHERE business_id = %s AND idempotency_key = %s",
                (status, json.dumps(result), g.business_id, key),
            )
        return jsonify(result), status

    return wrapper


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------
def load_unit(cur, unit_id):
    cur.execute("SELECT * FROM food_units WHERE id = %s AND business_id = %s", (int_id(unit_id, "Unit"), g.business_id))
    unit = cur.fetchone()
    if not unit:
        raise ApiError(404, "Unit not found")
    return unit


def load_settings(cur):
    cur.execute("SELECT * FROM food_settings WHERE business_id = %s", (g.business_id,))
    return cur.fetchone()


def owned(cur, table, row_id, label):
    """table is always a literal from this package, never user input."""
    if row_id is None:
        return None
    cur.execute(f"SELECT id FROM {table} WHERE id = %s AND business_id = %s", (row_id, g.business_id))
    if not cur.fetchone():
        raise ApiError(404, f"{label} not found")
    return row_id


# ---------------------------------------------------------------------------
# Stock ledger engine
# ---------------------------------------------------------------------------
def lock_products(cur, product_ids):
    """Locks stock items in id order (so two postings cannot deadlock) and returns them
    keyed by id with their stock unit. Holding this lock serialises balance checks."""
    ids = sorted({int_id(p, "Product") for p in product_ids})
    cur.execute(
        """SELECT p.*, u.name AS unit_name, u.dimension AS unit_dimension, u.to_base AS unit_to_base
           FROM food_inventory_products p JOIN food_units u ON u.id = p.unit_id
           WHERE p.business_id = %s AND p.id = ANY(%s) AND p.deleted_at IS NULL
           ORDER BY p.id FOR UPDATE OF p""",
        (g.business_id, ids),
    )
    products = {r["id"]: r for r in cur.fetchall()}
    missing = [i for i in ids if i not in products]
    if missing:
        raise ApiError(404, "A selected stock product was not found")
    return products


def stock_unit(product):
    return {"id": product["unit_id"], "name": product["unit_name"], "dimension": product["unit_dimension"],
            "to_base": product["unit_to_base"]}


def to_stock_qty(cur, product, quantity, unit_id):
    unit = load_unit(cur, unit_id) if unit_id else stock_unit(product)
    try:
        converted = convert_qty(quantity, unit, stock_unit(product))
    except CoreError as err:
        raise ApiError(422, f"{product['name']}: {err}")
    if converted <= 0:
        raise ApiError(422, f"{product['name']}: quantity is too small")
    return converted, unit


def balance(cur, product_id):
    cur.execute(
        """SELECT COALESCE(SUM(quantity), 0) AS qty, COALESCE(SUM(value), 0) AS val
           FROM food_inventory_transactions WHERE business_id = %s AND product_id = %s""",
        (g.business_id, product_id),
    )
    row = cur.fetchone()
    return row["qty"], row["val"]


def post_ledger(cur, product_id, txn_type, quantity, value, on_date, source_type=None, source_id=None, note=None):
    cur.execute(
        """INSERT INTO food_inventory_transactions
           (business_id, user_id, transaction_date, product_id, txn_type, quantity, value, source_type, source_id, note)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (g.business_id, g.user_id, on_date, product_id, txn_type, quantity, value, source_type, source_id, note),
    )


class StockGuard:
    """Collects every shortage in one posting, so the owner sees them all at once.
    Checks run on final balances, after all rows of the posting are written."""

    def __init__(self, cur, allow_override=False):
        self.cur = cur
        self.allowed = bool(allow_override) or bool((load_settings(cur) or {}).get("allow_negative_stock"))
        self.reduced = {}

    def note_reduction(self, product):
        self.reduced[product["id"]] = product

    def enforce(self):
        if self.allowed:
            return
        shortages = []
        for product_id, product in self.reduced.items():
            qty, _ = balance(self.cur, product_id)
            if qty < 0:
                shortages.append({"productId": product_id, "name": product["name"], "unit": product["unit_name"], "shortBy": -qty})
        if shortages:
            names = ", ".join(f"{s['name']} (short by {s['shortBy'].normalize():f} {s['unit']})" for s in shortages)
            raise ApiError(409, f"Not enough stock: {names}", extra={"code": "INSUFFICIENT_STOCK", "shortages": shortages})


def post_stock_out(cur, product, txn_type, quantity, on_date, source_type, source_id, guard, note=None):
    qty, val = balance(cur, product["id"])
    rate = average_cost(qty, val, product["purchase_price"])
    post_ledger(cur, product["id"], txn_type, -quantity, -(quantity * rate), on_date, source_type, source_id, note)
    guard.note_reduction(product)


def day_usage(cur, product_id, on_date):
    """(manual preparation qty, recipe-required qty, qty already taken from stock) for one item on one day."""
    cur.execute(
        """SELECT COALESCE(SUM(i.stock_qty), 0) AS q FROM food_consumption_items i
           JOIN food_consumption_transactions h ON h.id = i.consumption_id
           WHERE i.business_id = %s AND i.product_id = %s AND i.transaction_date = %s
             AND i.purpose = 'PREPARATION' AND h.status = 'Posted'""",
        (g.business_id, product_id, on_date),
    )
    manual = cur.fetchone()["q"]
    cur.execute(
        """SELECT COALESCE(SUM(u.required_qty), 0) AS q FROM food_sales_item_usage u
           JOIN food_sales_transactions h ON h.id = u.sale_id
           WHERE u.business_id = %s AND u.product_id = %s AND u.transaction_date = %s AND h.status = 'Posted'""",
        (g.business_id, product_id, on_date),
    )
    recipe = cur.fetchone()["q"]
    cur.execute(
        """SELECT COALESCE(-SUM(quantity), 0) AS q, COALESCE(-SUM(value), 0) AS v FROM food_inventory_transactions
           WHERE business_id = %s AND product_id = %s AND transaction_date = %s AND txn_type IN ('CONSUMPTION', 'SALE_USAGE')""",
        (g.business_id, product_id, on_date),
    )
    posted = cur.fetchone()
    return manual, recipe, posted["q"], posted["v"]


def reconcile_usage(cur, product, on_date, txn_type, source_type, source_id, guard):
    """Brings the ledger to max(manual, recipe) for this item and day by posting only
    the difference. This is the single place preparation and sales usage touch stock."""
    manual, recipe, posted_qty, posted_value = day_usage(cur, product["id"], on_date)
    delta = usage_target(manual, recipe) - posted_qty
    if delta > 0:
        post_stock_out(cur, product, txn_type, delta, on_date, source_type, source_id, guard)
    elif delta < 0:
        # A void gave usage back: return it at the rate it left stock.
        rate = posted_value / posted_qty if posted_qty > 0 else ZERO
        post_ledger(cur, product["id"], txn_type, -delta, -delta * rate, on_date, source_type, source_id, "Usage reversed")


__all__ = [
    "ApiError", "IST", "MAX_MONEY", "StockGuard", "balance", "body", "camel", "choice", "date_range", "day_usage", "foodseller_bp",
    "idempotent", "int_id", "jsonable", "lines_of", "load_settings", "load_unit", "lock_products", "money2", "number", "ok",
    "owned", "page_args", "parse_date", "post_ledger", "post_stock_out", "qty4", "reconcile_usage", "stock_unit", "text",
    "to_stock_qty", "today_ist", "tx", "txn_date",
]
