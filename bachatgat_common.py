"""
Mahila Bachat Gat API - shared plumbing (blueprint, auth, DB, posting engine).

Security model
- Every request must carry the bearer token issued by /api/login (same scheme
  as the inventory API). The token decides the tenant (owner_id); a request can
  never name a different owner.
- The tenant must be an active customer whose business type is Mahila Bachat
  Gat or whose enabled features include the shg_* key for the section being
  called - so the admin's feature switches are enforced here, not only in the app.
- Every group-scoped query joins back to shg_groups.owner_id = the caller.

Write model
- `tx()` opens one database transaction per request: commit on success,
  rollback on any error (including ApiError raised mid-way), so a failed
  posting never leaves half-written rows.
- Financial endpoints are wrapped in `idempotent`, keyed by the client's
  Idempotency-Key header: a retried request gets the original response.
- Rows that are updated by postings are locked with SELECT ... FOR UPDATE.
"""

import json
import os
import re
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from functools import wraps

import psycopg
from flask import Blueprint, g, jsonify, request
from psycopg.rows import dict_row

from auth_token import verify_token
from bachatgat_finance import (
    ACCOUNTS,
    TXN_CLASSIFICATION,
    FinanceError,
    is_balanced,
    journal_lines,
    money,
    money_account,
)
from bachatgat_service import BACHAT_GAT_FEATURES, BUSINESS_TYPE_NAME

bachatgat_bp = Blueprint("bachatgat", __name__, url_prefix="/api/bachatgat")

PAYMENT_METHODS = ("Cash", "Bank Transfer", "UPI", "Cheque")
FREQUENCY_VALUES = ("WEEKLY", "FORTNIGHTLY", "MONTHLY")
MAX_DOCUMENT_BYTES = 3 * 1024 * 1024
DOCUMENT_MIME_TYPES = ("image/jpeg", "image/png", "application/pdf")


class ApiError(Exception):
    def __init__(self, status, message, details=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


@bachatgat_bp.errorhandler(ApiError)
def _api_error(err):
    body = {"error": err.message}
    if err.details:
        body["details"] = err.details
    return jsonify(body), err.status


@bachatgat_bp.errorhandler(FinanceError)
def _finance_error(err):
    return jsonify({"error": str(err)}), 422


@bachatgat_bp.errorhandler(psycopg.errors.UniqueViolation)
def _unique_error(err):
    constraint = getattr(getattr(err, "diag", None), "constraint_name", "") or ""
    messages = {
        "uq_shg_group_code": "A group with this code already exists",
        "uq_shg_member_mobile_active": "This mobile number already belongs to an active member of one of your groups",
        "uq_shg_office_bearer": "This group already has an active member in that role",
        "uq_shg_bank_account": "This bank account is already added to the group",
        "uq_shg_product_name": "A product with this name already exists in this activity",
        "uq_shg_period_close": "This period is already closed",
        "uq_shg_txn_reversal": "This transaction has already been reversed",
    }
    return jsonify({"error": messages.get(constraint, "This record already exists")}), 409


@bachatgat_bp.errorhandler(psycopg.errors.CheckViolation)
def _check_error(err):
    print(f"SHG check violation: {getattr(getattr(err, 'diag', None), 'constraint_name', '')}")
    return jsonify({"error": "The values entered are not allowed for this record"}), 422


@bachatgat_bp.errorhandler(psycopg.Error)
def _db_error(err):
    # Never echo SQL or data back to the client; log the class only.
    print(f"SHG database error: {type(err).__name__}")
    return jsonify({"error": "Database is unavailable or rejected the request. Please try again."}), 503


def database_url():
    url = os.getenv("BACHATGAT_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise ApiError(503, "Database is not configured")
    return url


@contextmanager
def tx():
    """One database transaction yielding a dict-row cursor."""
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


# Section -> feature key; the first path segment after /api/bachatgat decides it.
SECTION_FEATURES = {
    "groups": "shg_groups", "officers": "shg_groups", "bank-accounts": "shg_accounts",
    "members": "shg_members", "savings": "shg_savings", "loans": "shg_loans",
    "installments": "shg_loans", "meetings": "shg_meetings", "resolutions": "shg_meetings",
    "accounts": "shg_accounts", "transactions": "shg_accounts", "business": "shg_business",
    "schemes": "shg_schemes", "reports": "shg_reports", "policies": "shg_settings",
    "gateway-payments": "shg_savings",
}


@bachatgat_bp.before_request
def require_shg_tenant():
    if request.method == "OPTIONS":
        return None
    # Razorpay's webhook has no login; it is authenticated by the group's webhook secret instead.
    if request.path.startswith("/api/bachatgat/gateway-webhook/"):
        return None
    # The Autopilot's cron trigger is authenticated by SHG_AGENT_CRON_SECRET instead.
    if request.path == "/api/bachatgat/agent/cron":
        return None
    header = request.headers.get("Authorization", "")
    user_id = verify_token(header[7:].strip()) if header.startswith("Bearer ") else None
    if user_id is None:
        return jsonify({"error": "Session expired. Please log in again."}), 401
    with tx() as cur:
        cur.execute(
            "SELECT id, user_type, status, business_type, enabled_features FROM userdetails WHERE id = %s",
            (user_id,),
        )
        user = cur.fetchone()
    if not user or user["user_type"] != "customer":
        return jsonify({"error": "Account not found"}), 403
    if user["status"] != "active":
        return jsonify({"error": "Your account is not active"}), 403
    features = user["enabled_features"] if isinstance(user["enabled_features"], list) else []
    is_shg_type = (user["business_type"] or "").strip().lower() == BUSINESS_TYPE_NAME.lower()
    has_shg = any(f in BACHAT_GAT_FEATURES for f in features)
    if not is_shg_type and not has_shg:
        return jsonify({"error": "Mahila Bachat Gat is not enabled for this account"}), 403
    # Explicit feature list (set by the admin) wins; an empty list means "type defaults".
    enabled = set(f for f in features if f in BACHAT_GAT_FEATURES) if has_shg else set(BACHAT_GAT_FEATURES)
    segment = request.path[len("/api/bachatgat/"):].split("/", 1)[0]
    needed = SECTION_FEATURES.get(segment)
    if needed and needed not in enabled:
        return jsonify({"error": "This module is not enabled for your account"}), 403
    g.owner_id = user_id
    g.shg_features = sorted(enabled)
    return None


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------
def body():
    return request.get_json(silent=True) or {}


def text(payload, key, max_len=200, required=False, label=None):
    value = str(payload.get(key) or "").strip()
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
    value = str(value).strip()
    if value not in allowed:
        raise ApiError(422, f"{label or key} must be one of: {', '.join(allowed)}")
    return value


def parse_date(payload, key, required=False, default=None, label=None):
    value = payload.get(key)
    if value in (None, ""):
        if required:
            raise ApiError(422, f"{label or key} is required")
        return default
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ApiError(422, f"{label or key} must be a date (YYYY-MM-DD)")


def parse_money(payload, key, required=False, positive=False, label=None, default="0"):
    value = payload.get(key)
    if value in (None, ""):
        if required:
            raise ApiError(422, f"{label or key} is required")
        return money(default)
    try:
        amount = money(value)
    except FinanceError:
        raise ApiError(422, f"{label or key} must be a number")
    if amount < 0 or (positive and amount <= 0):
        raise ApiError(422, f"{label or key} must be greater than zero")
    if amount > Decimal("9999999999"):
        raise ApiError(422, f"{label or key} is too large")
    return amount


def parse_int(payload, key, required=False, minimum=None, maximum=None, default=None, label=None):
    value = payload.get(key)
    if value in (None, ""):
        if required:
            raise ApiError(422, f"{label or key} is required")
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError(422, f"{label or key} must be a whole number")
    if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        raise ApiError(422, f"{label or key} must be between {minimum} and {maximum}")
    return number


MOBILE_RE = re.compile(r"^[6-9]\d{9}$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def mobile(payload, key, required=False, label="Mobile number"):
    value = re.sub(r"[\s-]", "", str(payload.get(key) or ""))
    if value.startswith("+91"):
        value = value[3:]
    if not value:
        if required:
            raise ApiError(422, f"{label} is required")
        return None
    if not MOBILE_RE.match(value):
        raise ApiError(422, f"{label} must be a valid 10-digit Indian mobile number")
    return value


def page_args(default_size=30, max_size=100):
    size = min(max(int(request.args.get("pageSize") or default_size), 1), max_size)
    page = max(int(request.args.get("page") or 1), 1)
    return size, (page - 1) * size, page


def jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, memoryview):
        return None
    return value


_CAMEL = re.compile(r"_([a-z0-9])")


def camel(row):
    """dict row -> camelCase JSON-safe dict."""
    if row is None:
        return None
    return {_CAMEL.sub(lambda m: m.group(1).upper(), k): jsonable(v) for k, v in row.items()}


def ok(payload, status=200):
    return jsonify(jsonable(payload)), status


def mask_account(number):
    number = str(number or "")
    return ("X" * max(0, len(number) - 4)) + number[-4:] if number else ""


# ---------------------------------------------------------------------------
# Access checks (tenant + group isolation)
# ---------------------------------------------------------------------------
def load_group(cur, group_id, lock=False):
    cur.execute(
        "SELECT * FROM shg_groups WHERE id = %s AND owner_id = %s" + (" FOR UPDATE" if lock else ""),
        (group_id, g.owner_id),
    )
    group = cur.fetchone()
    if not group:
        raise ApiError(404, "Group not found")
    return group


def require_active_group(group):
    if group["status"] != "Active":
        raise ApiError(409, f"Group is {group['status']}; financial transactions need an active group")


def load_member(cur, member_id, lock=False):
    cur.execute(
        "SELECT m.* FROM shg_members m WHERE m.id = %s AND m.owner_id = %s" + (" FOR UPDATE" if lock else ""),
        (member_id, g.owner_id),
    )
    member = cur.fetchone()
    if not member:
        raise ApiError(404, "Member not found")
    return member


def load_loan(cur, loan_id, lock=False):
    cur.execute(
        """SELECT l.* FROM shg_loans l JOIN shg_groups gr ON gr.id = l.group_id
           WHERE l.id = %s AND gr.owner_id = %s""" + (" FOR UPDATE OF l" if lock else ""),
        (loan_id, g.owner_id),
    )
    loan = cur.fetchone()
    if not loan:
        raise ApiError(404, "Loan not found")
    return loan


def load_policy(cur, group_id):
    cur.execute("SELECT * FROM shg_group_policies WHERE group_id = %s", (group_id,))
    policy = cur.fetchone()
    if not policy:
        cur.execute("INSERT INTO shg_group_policies (group_id) VALUES (%s) RETURNING *", (group_id,))
        policy = cur.fetchone()
    return policy


def load_bank_account(cur, group_id, bank_account_id):
    if not bank_account_id:
        cur.execute(
            "SELECT * FROM shg_bank_accounts WHERE group_id = %s AND active ORDER BY is_primary DESC, id LIMIT 1",
            (group_id,),
        )
        acct = cur.fetchone()
        if not acct:
            raise ApiError(422, "Add a bank account to this group before recording bank/UPI payments")
        return acct
    cur.execute("SELECT * FROM shg_bank_accounts WHERE id = %s AND group_id = %s AND active", (bank_account_id, group_id))
    acct = cur.fetchone()
    if not acct:
        raise ApiError(404, "Bank account not found for this group")
    return acct


def check_version(row, payload):
    """Optimistic concurrency for edits: the client sends the version it loaded."""
    sent = payload.get("version")
    if sent not in (None, "") and int(sent) != int(row["version"]):
        raise ApiError(409, "This record was changed by someone else. Reload and try again.")


# ---------------------------------------------------------------------------
# Numbering, history, notifications
# ---------------------------------------------------------------------------
def next_number(cur, group_id, key):
    cur.execute(
        """INSERT INTO shg_counters (group_id, counter_key, value) VALUES (%s, %s, 1)
           ON CONFLICT (group_id, counter_key) DO UPDATE SET value = shg_counters.value + 1
           RETURNING value""",
        (group_id, key),
    )
    return cur.fetchone()["value"]


def formatted_number(cur, group, key, prefix, when=None):
    seq = next_number(cur, group["id"], f"{key}:{(when or date.today()).year}")
    return f"{group['group_code']}-{prefix}-{(when or date.today()).year}-{seq:05d}"


def record_event(cur, group_id, entity_type, entity_id, action, from_status=None, to_status=None, details=None):
    cur.execute(
        """INSERT INTO shg_events (owner_id, group_id, entity_type, entity_id, action, from_status, to_status, details, performed_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (g.owner_id, group_id, entity_type, entity_id, action, from_status, to_status,
         json.dumps(jsonable(details or {})), g.owner_id),
    )


def notify(cur, group_id, kind, title, body_text, dedupe_key, entity_type=None, entity_id=None):
    """Adds an in-app notification once per dedupe_key. Text must not carry
    amounts, account numbers or ID numbers (previews show on lock screens)."""
    cur.execute(
        """INSERT INTO shg_notifications (owner_id, group_id, kind, title, body, entity_type, entity_id, dedupe_key)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (owner_id, dedupe_key) DO NOTHING""",
        (g.owner_id, group_id, kind, title[:200], body_text[:400], entity_type, entity_id, dedupe_key[:160]),
    )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def idempotent(fn):
    """Requires an Idempotency-Key header on financial writes. The key row is
    written inside the same DB transaction as the posting, so:
    - success: the response is stored; a retry returns it with replayed=true
    - failure: everything (including the key) rolls back; a retry runs again
    - two concurrent requests with one key: the second waits on the row lock,
      then sees the stored response."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        key = (request.headers.get("Idempotency-Key") or "").strip()
        if not re.match(r"^[A-Za-z0-9_-]{8,64}$", key):
            raise ApiError(400, "Idempotency-Key header (8-64 letters/digits) is required for this action")
        with tx() as cur:
            cur.execute(
                """INSERT INTO shg_request_keys (owner_id, idempotency_key, endpoint) VALUES (%s, %s, %s)
                   ON CONFLICT DO NOTHING RETURNING owner_id""",
                (g.owner_id, key, request.path[:120]),
            )
            if cur.fetchone() is None:
                cur.execute(
                    "SELECT endpoint, status_code, response FROM shg_request_keys WHERE owner_id = %s AND idempotency_key = %s",
                    (g.owner_id, key),
                )
                prior = cur.fetchone()
                if prior["endpoint"] != request.path[:120]:
                    raise ApiError(409, "This Idempotency-Key was already used for a different action")
                payload = dict(prior["response"] or {})
                payload["replayed"] = True
                return jsonify(payload), prior["status_code"] or 200
            g.cur = cur
            result, status = fn(cur, *args, **kwargs)
            result = jsonable(result)
            cur.execute(
                "UPDATE shg_request_keys SET status_code = %s, response = %s WHERE owner_id = %s AND idempotency_key = %s",
                (status, json.dumps(result), g.owner_id, key),
            )
        return jsonify(result), status

    return wrapper


# ---------------------------------------------------------------------------
# Posting engine
# ---------------------------------------------------------------------------
def assert_period_open(cur, group_id, txn_date):
    cur.execute("SELECT MAX(period_end) AS closed FROM shg_period_closes WHERE group_id = %s", (group_id,))
    closed = cur.fetchone()["closed"]
    if closed and txn_date <= closed:
        raise ApiError(409, f"Books are closed up to {closed.isoformat()}; use a date after that")
    if txn_date > date.today():
        raise ApiError(422, "Transaction date cannot be in the future")


def account_balance(cur, group_id, account_code, as_of=None, bank_account_id=None, member_id=None, loan_id=None):
    """Debit-minus-credit balance of an account from posted lines."""
    sql = ["SELECT COALESCE(SUM(debit - credit), 0) AS bal FROM shg_ledger_lines WHERE group_id = %s AND account_code = %s"]
    params = [group_id, account_code]
    for col, val in (("bank_account_id", bank_account_id), ("member_id", member_id), ("loan_id", loan_id)):
        if val is not None:
            sql.append(f"AND {col} = %s")
            params.append(val)
    if as_of is not None:
        sql.append("AND txn_date <= %s")
        params.append(as_of)
    cur.execute(" ".join(sql), params)
    return money(cur.fetchone()["bal"])


def member_savings_balance(cur, group_id, member_id):
    # Liability: credit balance
    return -account_balance(cur, group_id, "MEMBER_SAVINGS", member_id=member_id)


def post_transaction(cur, group, txn_type, amount, txn_date, payment_method="Cash", bank_account_id=None,
                     member_id=None, loan_id=None, activity_id=None, order_id=None, scheme_id=None,
                     resolution_id=None, split=None, narration=None, reference_number=None,
                     receipt_prefix=None, lines=None):
    """Writes one balanced transaction (header + ledger lines) and returns the header row.
    Caller is responsible for business validation and for locking rows it updates."""
    require_active_group(group)
    # Serialize all postings of one group: balance checks below and number
    # sequences must not race with a concurrent posting.
    cur.execute("SELECT status FROM shg_groups WHERE id = %s FOR UPDATE", (group["id"],))
    if cur.fetchone()["status"] != "Active":
        raise ApiError(409, "Group is no longer active")
    assert_period_open(cur, group["id"], txn_date)
    amount = money(amount)
    if payment_method not in PAYMENT_METHODS + ("Adjustment",):
        raise ApiError(422, "Unsupported payment method")
    if lines is None:
        lines = journal_lines(txn_type, amount, payment_method, split)
    if not is_balanced(lines):
        raise ApiError(500, "Posting is not balanced")
    needs_bank = any(acc == "BANK" for acc, _d, _c, _t in lines)
    bank = load_bank_account(cur, group["id"], bank_account_id) if needs_bank else None
    if needs_bank and payment_method == "Cash" and txn_type not in ("BANK_DEPOSIT", "BANK_WITHDRAWAL"):
        payment_method = "Bank Transfer"

    # Never let cash or a bank account go negative.
    for acc, _d, credit, _t in lines:
        if acc in ("CASH", "BANK") and credit > 0:
            available = account_balance(cur, group["id"], acc, bank_account_id=bank["id"] if acc == "BANK" else None)
            if credit > available:
                where = "cash in hand" if acc == "CASH" else f"bank account {mask_account(bank['account_number'])}"
                raise ApiError(422, f"Not enough {where}: available {available}, needed {credit}")

    txn_number = formatted_number(cur, group, "TXN", "TXN", txn_date)
    receipt_number = formatted_number(cur, group, f"RCP-{receipt_prefix}", receipt_prefix, txn_date) if receipt_prefix else None
    split = split or {}
    cur.execute(
        """INSERT INTO shg_transactions
             (owner_id, group_id, txn_number, receipt_number, txn_date, txn_type, classification, amount,
              principal_amount, interest_amount, penalty_amount, payment_method, bank_account_id,
              reference_number, member_id, loan_id, activity_id, order_id, scheme_id, resolution_id,
              narration, approval_status, idempotency_key, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING *""",
        (g.owner_id, group["id"], txn_number, receipt_number, txn_date, txn_type,
         TXN_CLASSIFICATION.get(txn_type, "ADJUSTMENT"), amount,
         money(split.get("principal")), money(split.get("interest")), money(split.get("penalty")),
         payment_method, bank["id"] if bank else None, reference_number, member_id, loan_id, activity_id,
         order_id, scheme_id, resolution_id, (narration or "")[:300] or None,
         "Approved" if resolution_id else "Not Required",
         request.headers.get("Idempotency-Key"), g.owner_id),
    )
    txn = cur.fetchone()
    for acc, debit, credit, _tag in lines:
        if acc not in ACCOUNTS:
            raise ApiError(500, f"Unknown account {acc}")
        cur.execute(
            """INSERT INTO shg_ledger_lines (txn_id, group_id, txn_date, account_code, bank_account_id,
                   member_id, loan_id, activity_id, debit, credit)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (txn["id"], group["id"], txn_date, acc, bank["id"] if acc == "BANK" else None,
             member_id if acc == "MEMBER_SAVINGS" else None,
             loan_id if acc in ("LOAN_RECEIVABLE", "INTEREST_INCOME", "PENALTY_INCOME", "LOAN_WRITE_OFF") else None,
             activity_id if acc in ("BUSINESS_INCOME", "BUSINESS_EXPENSE") else None,
             debit, credit),
        )
    record_event(cur, group["id"], "TRANSACTION", txn["id"], f"POSTED_{txn_type}", details={
        "txnNumber": txn_number, "amount": amount, "memberId": member_id, "loanId": loan_id,
    })
    return txn


def serialize_txn(row):
    out = camel(row)
    out.pop("idempotencyKey", None)
    return out


TXN_SELECT = """
    SELECT t.*, m.full_name AS member_name, m.member_number, l.application_number,
           b.bank_name, RIGHT(b.account_number, 4) AS bank_last4, gr.name_en AS group_name, gr.group_code
    FROM shg_transactions t
    JOIN shg_groups gr ON gr.id = t.group_id
    LEFT JOIN shg_members m ON m.id = t.member_id
    LEFT JOIN shg_loans l ON l.id = t.loan_id
    LEFT JOIN shg_bank_accounts b ON b.id = t.bank_account_id
"""


def fetch_txn(cur, txn_id):
    cur.execute(TXN_SELECT + " WHERE t.id = %s AND gr.owner_id = %s", (txn_id, g.owner_id))
    return serialize_txn(cur.fetchone())


# ---------------------------------------------------------------------------
# Meeting resolutions as authorization
# ---------------------------------------------------------------------------
def active_member_count(cur, group_id):
    cur.execute("SELECT COUNT(*) AS n FROM shg_members WHERE group_id = %s AND status = 'Active'", (group_id,))
    return cur.fetchone()["n"]


def use_resolution(cur, group_id, resolution_id, resolution_type, consumer_type, consumer_id,
                   loan_id=None, amount=None, supermajority=False):
    """Validates that a Passed resolution of the right type, from a held meeting
    of this group, authorizes this decision - then marks it used so one
    resolution cannot authorize two decisions. Returns the resolution row."""
    if not resolution_id:
        raise ApiError(422, "A meeting resolution is required to authorize this action")
    cur.execute(
        """SELECT r.*, mt.group_id, mt.status AS meeting_status,
                  (SELECT COUNT(*) FROM shg_meeting_attendance a WHERE a.meeting_id = mt.id AND a.present) AS present_count
           FROM shg_resolutions r JOIN shg_meetings mt ON mt.id = r.meeting_id
           WHERE r.id = %s FOR UPDATE OF r""",
        (resolution_id,),
    )
    res = cur.fetchone()
    if not res or res["group_id"] != group_id:
        raise ApiError(404, "Resolution not found for this group")
    if res["meeting_status"] not in ("Held", "Minutes Approved"):
        raise ApiError(409, "The meeting for this resolution has not been held")
    if res["resolution_type"] != resolution_type:
        raise ApiError(409, f"Resolution type must be {resolution_type}")
    if res["result"] != "Passed":
        raise ApiError(409, "This resolution did not pass")
    if loan_id is not None and res["loan_id"] != loan_id:
        raise ApiError(409, "This resolution is for a different loan")
    if amount is not None and (res["amount"] is None or money(res["amount"]) < money(amount)):
        raise ApiError(409, "The amount exceeds what the resolution authorized")
    # One resolution authorizes exactly one decision - even a second reschedule of the
    # same loan needs a fresh resolution. (Retries are covered by idempotency keys.)
    if res["consumed_by_type"]:
        raise ApiError(409, "This resolution has already been used for another decision")
    if supermajority and res["votes_for"] * 3 < res["present_count"] * 2:
        raise ApiError(409, "Amounts above the group's approval limit need two-thirds of members present to vote in favour")
    cur.execute(
        "UPDATE shg_resolutions SET consumed_by_type = %s, consumed_by_id = %s WHERE id = %s",
        (consumer_type, consumer_id, resolution_id),
    )
    return res
