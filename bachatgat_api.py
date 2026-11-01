"""
Mahila Bachat Gat REST API (Flask Blueprint) - mounted at /api/bachatgat

This module: groups, officers, bank accounts, policies, members, documents and
savings. Loans/meetings live in bachatgat_loans_api.py; accounting, business,
schemes, dashboard, notifications and reports in bachatgat_accounts_api.py.
All three register routes on the same blueprint (see bachatgat_common.py for
the auth / transaction / idempotency rules every route follows).
"""

import base64
import binascii
from datetime import date, timedelta

from flask import g, request

from bachatgat_common import (
    DOCUMENT_MIME_TYPES,
    FREQUENCY_VALUES,
    IFSC_RE,
    MAX_DOCUMENT_BYTES,
    PAYMENT_METHODS,
    ApiError,
    active_member_count,
    bachatgat_bp,
    body,
    camel,
    check_version,
    choice,
    fetch_txn,
    formatted_number,
    idempotent,
    load_group,
    load_member,
    load_policy,
    mask_account,
    member_savings_balance,
    mobile,
    next_number,
    notify,
    ok,
    page_args,
    parse_date,
    parse_int,
    parse_money,
    post_transaction,
    record_event,
    require_active_group,
    text,
    tx,
    use_resolution,
    account_balance,
)
from bachatgat_finance import add_period, allocate_savings, money, ZERO

GROUP_STATUSES = ("Draft", "Pending Approval", "Active", "Suspended", "Closed")
GROUP_CATEGORIES = ("General", "SC", "ST", "OBC", "Minority", "Mixed", "Other")
MEMBER_ROLES = ("President", "Secretary", "Treasurer", "Member")
OFFICE_BEARERS = ("President", "Secretary", "Treasurer")


@bachatgat_bp.get("/meta")
def shg_meta():
    """Enabled sections and option lists for the app's forms."""
    return ok({
        "features": g.shg_features,
        "groupStatuses": GROUP_STATUSES,
        "groupCategories": GROUP_CATEGORIES,
        "memberRoles": MEMBER_ROLES,
        "paymentMethods": PAYMENT_METHODS,
        "frequencies": FREQUENCY_VALUES,
        "interestMethods": ["FLAT", "REDUCING"],
        "today": date.today(),
    })


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------
GROUP_LIST_SQL = """
    SELECT gr.id, gr.group_code, gr.name_en, gr.name_mr, gr.village, gr.taluka, gr.district,
           gr.status, gr.verification_status, gr.formation_date, gr.livelihood_activity, gr.version,
           (SELECT COUNT(*) FROM shg_members m WHERE m.group_id = gr.id AND m.status = 'Active') AS active_members,
           (SELECT COUNT(*) FROM shg_members m WHERE m.group_id = gr.id AND m.status = 'Pending') AS pending_members,
           (SELECT COALESCE(SUM(credit - debit), 0) FROM shg_ledger_lines x
              WHERE x.group_id = gr.id AND x.account_code = 'MEMBER_SAVINGS') AS savings_balance,
           (SELECT COALESCE(SUM(debit - credit), 0) FROM shg_ledger_lines x
              WHERE x.group_id = gr.id AND x.account_code = 'LOAN_RECEIVABLE') AS loan_outstanding
    FROM shg_groups gr
"""


def _group_fields(payload, partial=False):
    fields = {
        "name_en": text(payload, "nameEn", 150, required=not partial, label="Group name (English)"),
        "name_mr": text(payload, "nameMr", 150),
        "formation_date": parse_date(payload, "formationDate", required=not partial, label="Formation date"),
        "registration_number": text(payload, "registrationNumber", 80),
        "registration_authority": text(payload, "registrationAuthority", 150),
        "state": text(payload, "state", 80),
        "district": text(payload, "district", 80),
        "taluka": text(payload, "taluka", 80),
        "village": text(payload, "village", 120),
        "gram_panchayat": text(payload, "gramPanchayat", 120),
        "address": text(payload, "address", 500),
        "category": choice(payload, "category", GROUP_CATEGORIES, default="General"),
        "livelihood_activity": text(payload, "livelihoodActivity", 150),
        "meeting_frequency": choice(payload, "meetingFrequency", FREQUENCY_VALUES, default="MONTHLY"),
    }
    if fields["formation_date"] and fields["formation_date"] > date.today():
        raise ApiError(422, "Formation date cannot be in the future")
    if partial:
        fields = {k: v for k, v in fields.items() if _camel_key(k) in payload}
    return fields


def _camel_key(snake):
    parts = snake.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _bank_fields(payload):
    bank_name = text(payload, "bankName", 120)
    number = text(payload, "accountNumber", 30)
    if not bank_name and not number:
        return None
    if not bank_name or not number:
        raise ApiError(422, "Bank name and account number are both required")
    if not number.isdigit() or not 6 <= len(number) <= 20:
        raise ApiError(422, "Account number must be 6-20 digits")
    ifsc = (text(payload, "ifsc", 11) or "").upper() or None
    if ifsc and not IFSC_RE.match(ifsc):
        raise ApiError(422, "IFSC must look like ABCD0123456")
    return {
        "bank_name": bank_name, "branch": text(payload, "branch", 120), "account_number": number,
        "ifsc": ifsc, "account_type": text(payload, "accountType", 30) or "Savings",
    }


def _insert_bank(cur, group_id, fields, primary):
    cur.execute(
        """INSERT INTO shg_bank_accounts (group_id, bank_name, branch, account_number, ifsc, account_type, is_primary)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (group_id, fields["bank_name"], fields["branch"], fields["account_number"], fields["ifsc"],
         fields["account_type"], primary),
    )
    return cur.fetchone()


def serialize_bank(row, full=False):
    out = camel(row)
    out["accountNumberMasked"] = mask_account(row["account_number"])
    if not full:
        out.pop("accountNumber", None)
    return out


def _policy_out(row):
    out = camel(row)
    return out


@bachatgat_bp.get("/groups")
def list_groups():
    size, offset, page = page_args()
    where, params = ["gr.owner_id = %s"], [g.owner_id]
    status = request.args.get("status")
    if status:
        where.append("gr.status = %s")
        params.append(status)
    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(gr.name_en ILIKE %s OR gr.name_mr ILIKE %s OR gr.group_code ILIKE %s OR gr.village ILIKE %s)")
        params += [f"%{q}%"] * 4
    with tx() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM shg_groups gr WHERE {' AND '.join(where)}", params)
        total = cur.fetchone()["n"]
        cur.execute(
            f"{GROUP_LIST_SQL} WHERE {' AND '.join(where)} ORDER BY gr.status = 'Active' DESC, gr.name_en LIMIT %s OFFSET %s",
            params + [size, offset],
        )
        groups = [camel(r) for r in cur.fetchall()]
    return ok({"groups": groups, "total": total, "page": page, "pageSize": size})


@bachatgat_bp.post("/groups")
def create_group():
    payload = body()
    fields = _group_fields(payload)
    code = (text(payload, "groupCode", 30) or "").upper()
    bank = _bank_fields(payload)
    with tx() as cur:
        if not code:
            cur.execute("SELECT COUNT(*) + 1 AS n FROM shg_groups WHERE owner_id = %s", (g.owner_id,))
            code = f"SHG{cur.fetchone()['n']:03d}"
        if not code.replace("-", "").isalnum():
            raise ApiError(422, "Group code may contain only letters, digits and '-'")
        cols = ["owner_id", "group_code"] + list(fields)
        cur.execute(
            f"INSERT INTO shg_groups ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
            [g.owner_id, code] + list(fields.values()),
        )
        group = cur.fetchone()
        policy_payload = payload.get("policy") or {}
        load_policy(cur, group["id"])
        if policy_payload:
            _update_policy(cur, group["id"], policy_payload)
        if bank:
            _insert_bank(cur, group["id"], bank, True)
        record_event(cur, group["id"], "GROUP", group["id"], "CREATED", None, "Draft", {"code": code})
    return ok({"group": camel(group)}, 201)


@bachatgat_bp.get("/groups/<int:group_id>")
def get_group(group_id):
    with tx() as cur:
        group = load_group(cur, group_id)
        policy = load_policy(cur, group_id)
        cur.execute("SELECT * FROM shg_group_officers WHERE group_id = %s ORDER BY active DESC, full_name", (group_id,))
        officers = [camel(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM shg_bank_accounts WHERE group_id = %s ORDER BY is_primary DESC, id", (group_id,))
        banks = [serialize_bank(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT status, COUNT(*) AS n FROM shg_members WHERE group_id = %s GROUP BY status", (group_id,)
        )
        member_counts = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute(
            "SELECT role, full_name, id FROM shg_members WHERE group_id = %s AND status = 'Active' AND role <> 'Member'",
            (group_id,),
        )
        office_bearers = {r["role"]: {"id": r["id"], "name": r["full_name"]} for r in cur.fetchall()}
        summary = {
            "savingsBalance": -account_balance(cur, group_id, "MEMBER_SAVINGS"),
            "loanOutstanding": account_balance(cur, group_id, "LOAN_RECEIVABLE"),
            "cashBalance": account_balance(cur, group_id, "CASH"),
            "bankBalance": account_balance(cur, group_id, "BANK"),
            "members": member_counts,
        }
    out = camel(group)
    return ok({
        "group": out, "policy": _policy_out(policy), "officers": officers, "bankAccounts": banks,
        "officeBearers": office_bearers, "summary": summary,
    })


@bachatgat_bp.put("/groups/<int:group_id>")
def update_group(group_id):
    payload = body()
    with tx() as cur:
        group = load_group(cur, group_id, lock=True)
        check_version(group, payload)
        if group["status"] == "Closed":
            raise ApiError(409, "A closed group cannot be edited")
        fields = _group_fields(payload, partial=True)
        if "groupCode" in payload and group["status"] == "Draft":
            code = (text(payload, "groupCode", 30, required=True) or "").upper()
            fields["group_code"] = code
        if not fields:
            raise ApiError(422, "Nothing to update")
        sets = ", ".join(f"{k} = %s" for k in fields)
        cur.execute(
            f"UPDATE shg_groups SET {sets}, version = version + 1, updated_at = NOW() WHERE id = %s RETURNING *",
            list(fields.values()) + [group_id],
        )
        updated = cur.fetchone()
        record_event(cur, group_id, "GROUP", group_id, "UPDATED", details={"fields": list(fields)})
    return ok({"group": camel(updated)})


GROUP_TRANSITIONS = {
    "submit": (("Draft",), "Pending Approval"),
    "approve": (("Pending Approval",), "Active"),
    "reject": (("Pending Approval",), "Draft"),
    "suspend": (("Active",), "Suspended"),
    "reactivate": (("Suspended",), "Active"),
    "close": (("Active", "Suspended", "Draft"), "Closed"),
}


@bachatgat_bp.post("/groups/<int:group_id>/workflow")
def group_workflow(group_id):
    payload = body()
    action = choice(payload, "action", tuple(GROUP_TRANSITIONS), label="Action")
    remarks = text(payload, "remarks", 500)
    allowed_from, to_status = GROUP_TRANSITIONS[action]
    with tx() as cur:
        group = load_group(cur, group_id, lock=True)
        if group["status"] not in allowed_from:
            raise ApiError(409, f"Cannot {action} a group that is {group['status']}")
        extra = {}
        if action == "submit":
            policy = load_policy(cur, group_id)
            members = active_member_count(cur, group_id)
            if members < policy["min_members"]:
                raise ApiError(422, f"At least {policy['min_members']} approved members are needed before submitting (now {members})")
            cur.execute(
                "SELECT role FROM shg_members WHERE group_id = %s AND status = 'Active' AND role <> 'Member'", (group_id,)
            )
            roles = {r["role"] for r in cur.fetchall()}
            missing = [r for r in OFFICE_BEARERS if r not in roles]
            if missing:
                raise ApiError(422, f"Assign the {', '.join(missing)} before submitting")
        if action in ("approve", "reject"):
            officer_id = parse_int(payload, "officerId", required=True, label="Reviewing officer")
            cur.execute(
                "SELECT * FROM shg_group_officers WHERE id = %s AND group_id = %s AND active AND designation IN ('Group Manager', 'Field Officer')",
                (officer_id, group_id),
            )
            if not cur.fetchone():
                raise ApiError(422, "Choose an active Group Manager or Field Officer of this group as reviewer")
            if action == "reject" and not remarks:
                raise ApiError(422, "Give a reason for rejection")
            extra = {"reviewed_by_officer_id": officer_id, "review_remarks": remarks,
                     "verification_status": "Verified" if action == "approve" else "Rejected"}
            if action == "approve":
                cur.execute("SELECT COUNT(*) AS n FROM shg_documents WHERE entity_type = 'GROUP' AND entity_id = %s", (group_id,))
                if cur.fetchone()["n"] == 0:
                    raise ApiError(422, "Upload at least one group document (e.g. resolution of formation) before approval")
        if action in ("suspend", "close") and not remarks:
            raise ApiError(422, f"Give a reason to {action} the group")
        if action == "close":
            for acc, label in (("MEMBER_SAVINGS", "member savings"), ("LOAN_RECEIVABLE", "loans outstanding"),
                               ("CASH", "cash in hand"), ("BANK", "bank balance")):
                if account_balance(cur, group_id, acc) != 0:
                    raise ApiError(409, f"Settle {label} before closing the group")
        sets = {"status": to_status, **extra}
        if to_status == "Active" and not group["activated_at"]:
            sets["activated_at"] = date.today()
        if to_status == "Closed":
            sets["closed_at"] = date.today()
        cur.execute(
            f"UPDATE shg_groups SET {', '.join(f'{k} = %s' for k in sets)}, version = version + 1, updated_at = NOW() WHERE id = %s RETURNING *",
            list(sets.values()) + [group_id],
        )
        updated = cur.fetchone()
        record_event(cur, group_id, "GROUP", group_id, action.upper(), group["status"], to_status, {"remarks": remarks})
        if action == "submit":
            notify(cur, group_id, "APPROVAL_PENDING", "Group awaiting approval",
                   f"{group['name_en']} was submitted for review.", f"group-submit-{group_id}-{updated['version']}", "GROUP", group_id)
    return ok({"group": camel(updated)})


@bachatgat_bp.get("/groups/<int:group_id>/history")
def group_history(group_id):
    size, offset, page = page_args(50, 200)
    with tx() as cur:
        load_group(cur, group_id)
        cur.execute(
            """SELECT id, entity_type, entity_id, action, from_status, to_status, details, created_at
               FROM shg_events WHERE group_id = %s AND owner_id = %s ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s""",
            (group_id, g.owner_id, size, offset),
        )
        events = [camel(r) for r in cur.fetchall()]
    return ok({"events": events, "page": page})


# Officers -------------------------------------------------------------------
@bachatgat_bp.post("/groups/<int:group_id>/officers")
def add_officer(group_id):
    payload = body()
    with tx() as cur:
        group = load_group(cur, group_id)
        if group["status"] == "Closed":
            raise ApiError(409, "Group is closed")
        cur.execute(
            "INSERT INTO shg_group_officers (group_id, full_name, designation, mobile) VALUES (%s, %s, %s, %s) RETURNING *",
            (group_id, text(payload, "fullName", 150, required=True, label="Name"),
             choice(payload, "designation", ("Group Manager", "Field Officer", "Auditor"), label="Designation"),
             mobile(payload, "mobile")),
        )
        officer = cur.fetchone()
        record_event(cur, group_id, "OFFICER", officer["id"], "ASSIGNED", details={"designation": officer["designation"]})
    return ok({"officer": camel(officer)}, 201)


@bachatgat_bp.put("/officers/<int:officer_id>")
def update_officer(officer_id):
    payload = body()
    with tx() as cur:
        cur.execute(
            """SELECT o.* FROM shg_group_officers o JOIN shg_groups gr ON gr.id = o.group_id
               WHERE o.id = %s AND gr.owner_id = %s""",
            (officer_id, g.owner_id),
        )
        officer = cur.fetchone()
        if not officer:
            raise ApiError(404, "Officer not found")
        cur.execute(
            "UPDATE shg_group_officers SET full_name = %s, mobile = %s, active = %s WHERE id = %s RETURNING *",
            (text(payload, "fullName", 150) or officer["full_name"],
             mobile(payload, "mobile") if "mobile" in payload else officer["mobile"],
             bool(payload.get("active", officer["active"])), officer_id),
        )
        updated = cur.fetchone()
        record_event(cur, officer["group_id"], "OFFICER", officer_id, "UPDATED", details={"active": updated["active"]})
    return ok({"officer": camel(updated)})


# Bank accounts ----------------------------------------------------------------
@bachatgat_bp.get("/groups/<int:group_id>/bank-accounts")
def list_bank_accounts(group_id):
    with tx() as cur:
        load_group(cur, group_id)
        cur.execute("SELECT * FROM shg_bank_accounts WHERE group_id = %s ORDER BY is_primary DESC, id", (group_id,))
        rows = cur.fetchall()
        out = []
        for r in rows:
            item = serialize_bank(r, full=request.args.get("full") == "1")
            item["balance"] = account_balance(cur, group_id, "BANK", bank_account_id=r["id"])
            out.append(item)
    return ok({"bankAccounts": out})


@bachatgat_bp.post("/groups/<int:group_id>/bank-accounts")
def add_bank_account(group_id):
    payload = body()
    fields = _bank_fields(payload)
    if not fields:
        raise ApiError(422, "Bank name and account number are required")
    with tx() as cur:
        group = load_group(cur, group_id)
        if group["status"] == "Closed":
            raise ApiError(409, "Group is closed")
        cur.execute("SELECT COUNT(*) AS n FROM shg_bank_accounts WHERE group_id = %s", (group_id,))
        primary = cur.fetchone()["n"] == 0
        acct = _insert_bank(cur, group_id, fields, primary)
        record_event(cur, group_id, "BANK_ACCOUNT", acct["id"], "ADDED", details={"bank": acct["bank_name"], "last4": acct["account_number"][-4:]})
    return ok({"bankAccount": serialize_bank(acct)}, 201)


@bachatgat_bp.put("/bank-accounts/<int:account_id>")
def update_bank_account(account_id):
    payload = body()
    with tx() as cur:
        cur.execute(
            """SELECT b.* FROM shg_bank_accounts b JOIN shg_groups gr ON gr.id = b.group_id
               WHERE b.id = %s AND gr.owner_id = %s FOR UPDATE OF b""",
            (account_id, g.owner_id),
        )
        acct = cur.fetchone()
        if not acct:
            raise ApiError(404, "Bank account not found")
        active = bool(payload.get("active", acct["active"]))
        if not active and account_balance(cur, acct["group_id"], "BANK", bank_account_id=account_id) != 0:
            raise ApiError(409, "Move the balance out before deactivating this account")
        if payload.get("isPrimary"):
            cur.execute("UPDATE shg_bank_accounts SET is_primary = FALSE WHERE group_id = %s", (acct["group_id"],))
        cur.execute(
            "UPDATE shg_bank_accounts SET branch = %s, active = %s, is_primary = %s WHERE id = %s RETURNING *",
            (text(payload, "branch", 120) or acct["branch"], active,
             bool(payload.get("isPrimary", acct["is_primary"])), account_id),
        )
        updated = cur.fetchone()
        record_event(cur, acct["group_id"], "BANK_ACCOUNT", account_id, "UPDATED", details={"active": active})
    return ok({"bankAccount": serialize_bank(updated)})


# Policies ---------------------------------------------------------------------
POLICY_FIELDS = {
    # json key: (column, parser)
    "savingsFrequency": ("savings_frequency", lambda p, k: choice(p, k, FREQUENCY_VALUES)),
    "savingsAmount": ("savings_amount", lambda p, k: parse_money(p, k, label="Savings amount")),
    "savingsDueDay": ("savings_due_day", lambda p, k: parse_int(p, k, minimum=1, maximum=28, label="Due day")),
    "allowSavingsWithdrawal": ("allow_savings_withdrawal", lambda p, k: bool(p.get(k))),
    "minMembers": ("min_members", lambda p, k: parse_int(p, k, minimum=1, maximum=100, label="Minimum members")),
    "quorumPercent": ("quorum_percent", lambda p, k: parse_int(p, k, minimum=1, maximum=100, label="Quorum")),
    "defaultInterestMethod": ("default_interest_method", lambda p, k: choice(p, k, ("FLAT", "REDUCING"))),
    "defaultInterestRate": ("default_interest_rate", lambda p, k: parse_money(p, k, label="Interest rate")),
    "defaultFrequency": ("default_frequency", lambda p, k: choice(p, k, FREQUENCY_VALUES)),
    "maxInstallments": ("max_installments", lambda p, k: parse_int(p, k, minimum=1, maximum=520, label="Maximum installments")),
    "maxLoanAmount": ("max_loan_amount", lambda p, k: parse_money(p, k, label="Maximum loan")),
    "savingsMultiple": ("savings_multiple", lambda p, k: parse_money(p, k, label="Savings multiple")),
    "minMembershipMonths": ("min_membership_months", lambda p, k: parse_int(p, k, minimum=0, maximum=120, label="Minimum membership months")),
    "allowMultipleLoans": ("allow_multiple_loans", lambda p, k: bool(p.get(k))),
    "loanApprovalLimit": ("loan_approval_limit", lambda p, k: parse_money(p, k, label="Loan approval limit")),
    "expenseApprovalLimit": ("expense_approval_limit", lambda p, k: parse_money(p, k, label="Expense approval limit")),
    "penaltyType": ("penalty_type", lambda p, k: choice(p, k, ("NONE", "FLAT", "PERCENT_PER_MONTH"))),
    "penaltyValue": ("penalty_value", lambda p, k: parse_money(p, k, label="Penalty")),
    "graceDays": ("grace_days", lambda p, k: parse_int(p, k, minimum=0, maximum=90, label="Grace days")),
    "roundToRupee": ("round_to_rupee", lambda p, k: bool(p.get(k))),
}


def _update_policy(cur, group_id, payload):
    updates = {}
    for key, (col, parser) in POLICY_FIELDS.items():
        if key in payload:
            updates[col] = parser(payload, key)
    if "defaultInterestRate" in payload and updates["default_interest_rate"] > 60:
        raise ApiError(422, "Interest rate must be at most 60% per year")
    if not updates:
        return load_policy(cur, group_id)
    cur.execute(
        f"UPDATE shg_group_policies SET {', '.join(f'{c} = %s' for c in updates)}, updated_at = NOW() WHERE group_id = %s RETURNING *",
        list(updates.values()) + [group_id],
    )
    return cur.fetchone()


@bachatgat_bp.get("/policies/<int:group_id>")
def get_policy(group_id):
    with tx() as cur:
        load_group(cur, group_id)
        return ok({"policy": _policy_out(load_policy(cur, group_id))})


@bachatgat_bp.put("/policies/<int:group_id>")
def put_policy(group_id):
    payload = body()
    with tx() as cur:
        group = load_group(cur, group_id, lock=True)
        if group["status"] == "Closed":
            raise ApiError(409, "Group is closed")
        load_policy(cur, group_id)
        policy = _update_policy(cur, group_id, payload)
        record_event(cur, group_id, "POLICY", group_id, "UPDATED",
                     details={k: payload[k] for k in payload if k in POLICY_FIELDS})
    return ok({"policy": _policy_out(policy)})


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
MEMBER_SELECT = """
    SELECT m.id, m.group_id, m.member_number, m.full_name, m.mobile, m.address, m.date_of_birth,
           m.joining_date, m.occupation, m.livelihood_activity, m.nominee_name, m.nominee_relation,
           m.nominee_mobile, m.id_proof_type, m.id_proof_last4, m.id_verification_status, m.role,
           m.status, m.photo_document_id, m.exit_date, m.exit_reason, m.version, m.created_at,
           gr.name_en AS group_name, gr.group_code
    FROM shg_members m JOIN shg_groups gr ON gr.id = m.group_id
"""


def _member_fields(payload, partial=False):
    fields = {
        "full_name": text(payload, "fullName", 150, required=not partial, label="Full name"),
        "mobile": mobile(payload, "mobile", required=not partial),
        "address": text(payload, "address", 500),
        "date_of_birth": parse_date(payload, "dateOfBirth"),
        "joining_date": parse_date(payload, "joiningDate", required=not partial, label="Joining date"),
        "occupation": text(payload, "occupation", 120),
        "livelihood_activity": text(payload, "livelihoodActivity", 150),
        "nominee_name": text(payload, "nomineeName", 150),
        "nominee_relation": text(payload, "nomineeRelation", 60),
        "nominee_mobile": mobile(payload, "nomineeMobile", label="Nominee mobile"),
        "id_proof_type": text(payload, "idProofType", 40),
    }
    # Only the last 4 digits of an ID number are ever stored.
    raw_id = "".join(ch for ch in str(payload.get("idProofNumber") or "") if ch.isalnum())
    if raw_id:
        fields["id_proof_last4"] = raw_id[-4:]
    if fields["date_of_birth"]:
        age_years = (date.today() - fields["date_of_birth"]).days / 365.25
        if age_years < 18:
            raise ApiError(422, "Members must be at least 18 years old")
    if fields["joining_date"] and fields["joining_date"] > date.today():
        raise ApiError(422, "Joining date cannot be in the future")
    if partial:
        keep = {k for k in fields if _camel_key(k) in payload}
        if "idProofNumber" in payload:
            keep.add("id_proof_last4")
        fields = {k: v for k, v in fields.items() if k in keep}
    return fields


@bachatgat_bp.get("/members")
def list_members():
    size, offset, page = page_args()
    where, params = ["m.owner_id = %s"], [g.owner_id]
    for arg, col in (("groupId", "m.group_id"), ("status", "m.status"), ("role", "m.role")):
        if request.args.get(arg):
            where.append(f"{col} = %s")
            params.append(request.args[arg])
    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(m.full_name ILIKE %s OR m.mobile LIKE %s OR CAST(m.member_number AS TEXT) = %s)")
        params += [f"%{q}%", f"%{q}%", q]
    with tx() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM shg_members m WHERE {' AND '.join(where)}", params)
        total = cur.fetchone()["n"]
        cur.execute(
            f"""SELECT x.*,
                   (SELECT COALESCE(SUM(credit - debit), 0) FROM shg_ledger_lines l
                     WHERE l.member_id = x.id AND l.account_code = 'MEMBER_SAVINGS') AS savings_balance,
                   (SELECT COALESCE(SUM(due_amount - paid_amount), 0) FROM shg_savings_dues d
                     WHERE d.member_id = x.id AND d.status IN ('Pending', 'Partial') AND d.due_date <= CURRENT_DATE) AS pending_dues
                FROM ({MEMBER_SELECT} WHERE {' AND '.join(where)}
                      ORDER BY gr.name_en, m.member_number LIMIT %s OFFSET %s) x""",
            params + [size, offset],
        )
        members = [camel(r) for r in cur.fetchall()]
    return ok({"members": members, "total": total, "page": page, "pageSize": size})


@bachatgat_bp.post("/members")
def create_member():
    payload = body()
    group_id = parse_int(payload, "groupId", required=True, label="Group")
    fields = _member_fields(payload)
    with tx() as cur:
        group = load_group(cur, group_id, lock=True)
        if group["status"] in ("Closed", "Suspended"):
            raise ApiError(409, f"Cannot add members to a {group['status'].lower()} group")
        if fields["joining_date"] < group["formation_date"]:
            raise ApiError(422, "Joining date cannot be before the group's formation date")
        cur.execute("SELECT COALESCE(MAX(member_number), 0) + 1 AS n FROM shg_members WHERE group_id = %s", (group_id,))
        number = cur.fetchone()["n"]
        cols = ["owner_id", "group_id", "member_number"] + list(fields)
        cur.execute(
            f"INSERT INTO shg_members ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
            [g.owner_id, group_id, number] + list(fields.values()),
        )
        member_id = cur.fetchone()["id"]
        record_event(cur, group_id, "MEMBER", member_id, "REGISTERED", None, "Pending", {"memberNumber": number})
        notify(cur, group_id, "APPROVAL_PENDING", "Member awaiting approval",
               f"{fields['full_name']} was registered in {group['name_en']}.", f"member-new-{member_id}", "MEMBER", member_id)
        cur.execute(MEMBER_SELECT + " WHERE m.id = %s", (member_id,))
        member = camel(cur.fetchone())
    return ok({"member": member}, 201)


def _member_summary(cur, member):
    mid, gid = member["id"], member["group_id"]
    cur.execute(
        """SELECT COALESCE(SUM(due_amount - paid_amount), 0) AS pending,
                  COUNT(*) FILTER (WHERE status IN ('Pending', 'Partial') AND due_date < CURRENT_DATE) AS missed
           FROM shg_savings_dues WHERE member_id = %s AND status IN ('Pending', 'Partial') AND due_date <= CURRENT_DATE""",
        (mid,),
    )
    dues = cur.fetchone()
    cur.execute(
        """SELECT COUNT(*) FILTER (WHERE status IN ('Active', 'Overdue', 'Disbursed')) AS active_loans,
                  COUNT(*) FILTER (WHERE status = 'Overdue') AS overdue_loans
           FROM shg_loans WHERE member_id = %s""",
        (mid,),
    )
    loans = cur.fetchone()
    cur.execute(
        """SELECT COUNT(*) FILTER (WHERE a.present) AS present, COUNT(*) AS total
           FROM shg_meeting_attendance a JOIN shg_meetings mt ON mt.id = a.meeting_id
           WHERE a.member_id = %s AND mt.status <> 'Cancelled'""",
        (mid,),
    )
    att = cur.fetchone()
    cur.execute(
        "SELECT COALESCE(SUM(debit - credit), 0) AS bal FROM shg_ledger_lines l JOIN shg_loans ln ON ln.id = l.loan_id "
        "WHERE ln.member_id = %s AND l.account_code = 'LOAN_RECEIVABLE'",
        (mid,),
    )
    outstanding = money(cur.fetchone()["bal"])
    return {
        "savingsBalance": member_savings_balance(cur, gid, mid),
        "pendingDues": money(dues["pending"]),
        "missedContributions": dues["missed"],
        "activeLoans": loans["active_loans"],
        "overdueLoans": loans["overdue_loans"],
        "loanOutstanding": outstanding,
        "meetingsPresent": att["present"],
        "meetingsTotal": att["total"],
    }


@bachatgat_bp.get("/members/<int:member_id>")
def get_member(member_id):
    with tx() as cur:
        load_member(cur, member_id)
        cur.execute(MEMBER_SELECT + " WHERE m.id = %s", (member_id,))
        row = cur.fetchone()
        member = camel(row)
        summary = _member_summary(cur, row)
        cur.execute(
            "SELECT id, action, from_status, to_status, details, created_at FROM shg_events "
            "WHERE entity_type = 'MEMBER' AND entity_id = %s AND owner_id = %s ORDER BY id DESC LIMIT 50",
            (member_id, g.owner_id),
        )
        history = [camel(r) for r in cur.fetchall()]
    return ok({"member": member, "summary": summary, "history": history})


@bachatgat_bp.put("/members/<int:member_id>")
def update_member(member_id):
    payload = body()
    with tx() as cur:
        member = load_member(cur, member_id, lock=True)
        check_version(member, payload)
        if member["status"] in ("Exited", "Rejected"):
            raise ApiError(409, "This membership is closed and cannot be edited")
        fields = _member_fields(payload, partial=True)
        if "joining_date" in fields:
            cur.execute("SELECT COUNT(*) AS n FROM shg_transactions WHERE member_id = %s", (member_id,))
            if cur.fetchone()["n"]:
                raise ApiError(409, "Joining date cannot change after transactions are recorded")
        if not fields:
            raise ApiError(422, "Nothing to update")
        cur.execute(
            f"UPDATE shg_members SET {', '.join(f'{k} = %s' for k in fields)}, version = version + 1, updated_at = NOW() WHERE id = %s",
            list(fields.values()) + [member_id],
        )
        record_event(cur, member["group_id"], "MEMBER", member_id, "UPDATED", details={"fields": list(fields)})
        cur.execute(MEMBER_SELECT + " WHERE m.id = %s", (member_id,))
        updated = camel(cur.fetchone())
    return ok({"member": updated})


MEMBER_TRANSITIONS = {
    "approve": (("Pending",), "Active"),
    "reject": (("Pending",), "Rejected"),
    "suspend": (("Active",), "Suspended"),
    "reactivate": (("Suspended",), "Active"),
}


@bachatgat_bp.post("/members/<int:member_id>/workflow")
def member_workflow(member_id):
    payload = body()
    action = choice(payload, "action", tuple(MEMBER_TRANSITIONS) + ("verify", "reject_verification", "waive_verification"), label="Action")
    remarks = text(payload, "remarks", 500)
    with tx() as cur:
        member = load_member(cur, member_id, lock=True)
        group = load_group(cur, member["group_id"])
        if action in ("verify", "reject_verification", "waive_verification"):
            new_status = {"verify": "Verified", "reject_verification": "Rejected", "waive_verification": "Not Required"}[action]
            if action == "verify" and not member["id_proof_type"]:
                raise ApiError(422, "Record the ID proof type before verifying")
            if action != "verify" and not remarks:
                raise ApiError(422, "Give a reason")
            cur.execute("UPDATE shg_members SET id_verification_status = %s, version = version + 1 WHERE id = %s",
                        (new_status, member_id))
            record_event(cur, group["id"], "MEMBER", member_id, action.upper(), member["id_verification_status"], new_status, {"remarks": remarks})
        else:
            allowed, to_status = MEMBER_TRANSITIONS[action]
            if member["status"] not in allowed:
                raise ApiError(409, f"Cannot {action} a member who is {member['status']}")
            if action == "approve" and member["id_verification_status"] not in ("Verified", "Not Required"):
                raise ApiError(422, "Verify the member's details (or mark verification not required) before approval")
            if action in ("reject", "suspend") and not remarks:
                raise ApiError(422, "Give a reason")
            if action == "suspend" and member["role"] != "Member":
                cur.execute("UPDATE shg_members SET role = 'Member' WHERE id = %s", (member_id,))
            cur.execute("UPDATE shg_members SET status = %s, version = version + 1, updated_at = NOW() WHERE id = %s",
                        (to_status, member_id))
            record_event(cur, group["id"], "MEMBER", member_id, action.upper(), member["status"], to_status, {"remarks": remarks})
        cur.execute(MEMBER_SELECT + " WHERE m.id = %s", (member_id,))
        updated = camel(cur.fetchone())
    return ok({"member": updated})


@bachatgat_bp.post("/members/<int:member_id>/role")
def assign_role(member_id):
    payload = body()
    role = choice(payload, "role", MEMBER_ROLES, label="Role")
    with tx() as cur:
        member = load_member(cur, member_id, lock=True)
        if member["status"] != "Active":
            raise ApiError(409, "Only active members can hold a role")
        if role != "Member":
            # Hand the post over: the current holder becomes an ordinary member.
            cur.execute(
                "SELECT id FROM shg_members WHERE group_id = %s AND role = %s AND status = 'Active' AND id <> %s FOR UPDATE",
                (member["group_id"], role, member_id),
            )
            previous = cur.fetchone()
            if previous:
                cur.execute("UPDATE shg_members SET role = 'Member', version = version + 1 WHERE id = %s", (previous["id"],))
                record_event(cur, member["group_id"], "MEMBER", previous["id"], "ROLE_CHANGED", role, "Member")
        cur.execute("UPDATE shg_members SET role = %s, version = version + 1 WHERE id = %s", (role, member_id))
        record_event(cur, member["group_id"], "MEMBER", member_id, "ROLE_CHANGED", member["role"], role)
        cur.execute(MEMBER_SELECT + " WHERE m.id = %s", (member_id,))
        updated = camel(cur.fetchone())
    return ok({"member": updated})


@bachatgat_bp.get("/members/<int:member_id>/transactions")
def member_transactions(member_id):
    size, offset, page = page_args(50, 200)
    kind = request.args.get("kind")  # savings | repayments | all
    types = {"savings": ("SAVINGS_DEPOSIT", "SAVINGS_WITHDRAWAL", "SAVINGS_LOAN_OFFSET"),
             "repayments": ("LOAN_REPAYMENT", "SAVINGS_LOAN_OFFSET")}.get(kind)
    with tx() as cur:
        load_member(cur, member_id)
        sql = "SELECT * FROM shg_transactions WHERE member_id = %s AND owner_id = %s"
        params = [member_id, g.owner_id]
        if types:
            sql += " AND txn_type = ANY(%s)"
            params.append(list(types))
        cur.execute(sql + " ORDER BY txn_date DESC, id DESC LIMIT %s OFFSET %s", params + [size, offset])
        rows = []
        for r in cur.fetchall():
            item = camel(r)
            item.pop("idempotencyKey", None)
            rows.append(item)
    return ok({"transactions": rows, "page": page})


@bachatgat_bp.get("/members/<int:member_id>/attendance")
def member_attendance(member_id):
    with tx() as cur:
        load_member(cur, member_id)
        cur.execute(
            """SELECT mt.id AS meeting_id, mt.meeting_number, mt.meeting_date, mt.status, a.present, a.remarks
               FROM shg_meeting_attendance a JOIN shg_meetings mt ON mt.id = a.meeting_id
               WHERE a.member_id = %s ORDER BY mt.meeting_date DESC LIMIT 200""",
            (member_id,),
        )
        rows = [camel(r) for r in cur.fetchall()]
    return ok({"attendance": rows})


def _settlement_quote(cur, member, as_of=None):
    as_of = as_of or date.today()
    savings = member_savings_balance(cur, member["group_id"], member["id"])
    cur.execute(
        "SELECT id, application_number, status FROM shg_loans WHERE member_id = %s AND status IN ('Active', 'Overdue', 'Disbursed', 'Approved', 'Submitted', 'Under Review', 'Draft')",
        (member["id"],),
    )
    open_loans = cur.fetchall()
    from bachatgat_loans_api import assess_penalties, quote_for_loan  # circular-safe late import
    loan_quotes = []
    owed = ZERO
    for loan in open_loans:
        if loan["status"] in ("Active", "Overdue", "Disbursed"):
            # Assess late charges first so the quote equals what the settlement posting will collect.
            cur.execute("SELECT * FROM shg_loans WHERE id = %s", (loan["id"],))
            assess_penalties(cur, cur.fetchone(), as_of)
            q = quote_for_loan(cur, loan["id"], as_of)
            loan_quotes.append({"loanId": loan["id"], "applicationNumber": loan["application_number"], **q})
            owed += q["total"]
        else:
            loan_quotes.append({"loanId": loan["id"], "applicationNumber": loan["application_number"], "pendingApplication": True, "status": loan["status"]})
    return {
        "savingsBalance": savings,
        "loanDues": owed,
        "loans": loan_quotes,
        "netPayable": savings - owed,
        "canSettleFromSavings": savings >= owed,
    }


@bachatgat_bp.get("/members/<int:member_id>/settlement")
def member_settlement_quote(member_id):
    with tx() as cur:
        member = load_member(cur, member_id)
        return ok({"settlement": _settlement_quote(cur, member)})


@bachatgat_bp.post("/members/<int:member_id>/exit")
@idempotent
def member_exit(cur, member_id):
    """Exit & settlement: open loan dues are recovered from the member's savings
    (SAVINGS_LOAN_OFFSET), the remaining savings are refunded, then the
    membership is closed. Pending loan applications must be withdrawn first."""
    payload = body()
    reason = text(payload, "reason", 500, required=True, label="Exit reason")
    exit_date = parse_date(payload, "exitDate", default=date.today())
    method = choice(payload, "paymentMethod", PAYMENT_METHODS, default="Cash")
    member = load_member(cur, member_id, lock=True)
    if member["status"] not in ("Active", "Suspended"):
        raise ApiError(409, f"Member is {member['status']}")
    group = load_group(cur, member["group_id"])
    if exit_date > date.today() or exit_date < member["joining_date"]:
        raise ApiError(422, "Exit date must be between the joining date and today")
    quote = _settlement_quote(cur, member, exit_date)
    if any(l.get("pendingApplication") for l in quote["loans"]):
        raise ApiError(409, "Reject or withdraw the member's pending loan applications first")
    if not quote["canSettleFromSavings"]:
        raise ApiError(409, f"Savings ({quote['savingsBalance']}) do not cover loan dues ({quote['loanDues']}); collect the balance as a repayment first")
    from bachatgat_loans_api import apply_loan_payment
    txns = []
    for lq in quote["loans"]:
        if lq["total"] > 0:
            t = apply_loan_payment(cur, group, lq["loanId"], lq["total"], exit_date, "Adjustment", None,
                                   None, "Settled from savings at exit", offset=True, foreclose=True)
            txns.append(t["id"])
    refund = member_savings_balance(cur, group["id"], member_id)
    if refund > 0:
        t = post_transaction(cur, group, "SAVINGS_WITHDRAWAL", refund, exit_date, method,
                             payload.get("bankAccountId"), member_id=member_id,
                             narration="Savings refund on exit", receipt_prefix="SAV",
                             reference_number=text(payload, "referenceNumber", 80))
        txns.append(t["id"])
    cur.execute(
        "UPDATE shg_savings_dues SET status = 'Waived' WHERE member_id = %s AND status IN ('Pending', 'Partial') AND period_start > %s",
        (member_id, exit_date),
    )
    cur.execute(
        "UPDATE shg_members SET status = 'Exited', role = 'Member', exit_date = %s, exit_reason = %s, version = version + 1 WHERE id = %s",
        (exit_date, reason, member_id),
    )
    record_event(cur, group["id"], "MEMBER", member_id, "EXITED", member["status"], "Exited",
                 {"reason": reason, "refund": refund, "transactions": txns})
    return {"settlement": quote, "refund": refund, "transactions": [fetch_txn(cur, t) for t in txns]}, 200


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
def _entity_group(cur, entity_type, entity_id):
    queries = {
        "GROUP": "SELECT id AS group_id FROM shg_groups WHERE id = %s AND owner_id = %s",
        "MEMBER": "SELECT group_id FROM shg_members WHERE id = %s AND owner_id = %s",
        "LOAN": "SELECT l.group_id FROM shg_loans l JOIN shg_groups gr ON gr.id = l.group_id WHERE l.id = %s AND gr.owner_id = %s",
        "SCHEME": "SELECT s.group_id FROM shg_scheme_applications s JOIN shg_groups gr ON gr.id = s.group_id WHERE s.id = %s AND gr.owner_id = %s",
        "MEETING": "SELECT mt.group_id FROM shg_meetings mt JOIN shg_groups gr ON gr.id = mt.group_id WHERE mt.id = %s AND gr.owner_id = %s",
        "TRANSACTION": "SELECT group_id FROM shg_transactions WHERE id = %s AND owner_id = %s",
    }
    if entity_type not in queries:
        raise ApiError(422, "Unknown document entity")
    cur.execute(queries[entity_type], (entity_id, g.owner_id))
    row = cur.fetchone()
    if not row:
        raise ApiError(404, "Record not found")
    return row["group_id"]


DOC_META = """SELECT id, group_id, entity_type, entity_id, document_type, file_name, mime_type, size_bytes,
                     verification_status, verification_remarks, created_at FROM shg_documents"""


@bachatgat_bp.post("/documents")
def upload_document():
    payload = body()
    entity_type = choice(payload, "entityType", ("GROUP", "MEMBER", "LOAN", "SCHEME", "MEETING", "TRANSACTION"))
    entity_id = parse_int(payload, "entityId", required=True)
    mime = choice(payload, "mimeType", DOCUMENT_MIME_TYPES, label="File type")
    raw = str(payload.get("fileBase64") or "")
    if "," in raw[:100] and raw.startswith("data:"):
        raw = raw.split(",", 1)[1]
    if len(raw) > MAX_DOCUMENT_BYTES * 4 // 3 + 8:
        raise ApiError(413, "File is larger than 3 MB")
    try:
        content = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(422, "File could not be read")
    if not content:
        raise ApiError(422, "File is empty")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ApiError(413, "File is larger than 3 MB")
    signatures = {"image/jpeg": b"\xff\xd8", "image/png": b"\x89PNG", "application/pdf": b"%PDF"}
    if not content.startswith(signatures[mime]):
        raise ApiError(422, "File content does not match its type")
    with tx() as cur:
        group_id = _entity_group(cur, entity_type, entity_id)
        cur.execute(
            """INSERT INTO shg_documents (owner_id, group_id, entity_type, entity_id, document_type, file_name,
                   mime_type, size_bytes, content, uploaded_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (g.owner_id, group_id, entity_type, entity_id,
             text(payload, "documentType", 60, required=True, label="Document type"),
             (text(payload, "fileName", 200) or f"document.{mime.split('/')[1]}").replace("/", "_"),
             mime, len(content), content, g.owner_id),
        )
        doc_id = cur.fetchone()["id"]
        if entity_type == "MEMBER" and payload.get("isPhoto"):
            cur.execute("UPDATE shg_members SET photo_document_id = %s WHERE id = %s", (doc_id, entity_id))
        record_event(cur, group_id, "DOCUMENT", doc_id, "UPLOADED", details={"entity": entity_type, "entityId": entity_id})
        notify(cur, group_id, "DOCUMENT_VERIFICATION", "Document awaiting verification",
               "A new document was uploaded and needs verification.", f"doc-{doc_id}", entity_type, entity_id)
        cur.execute(DOC_META + " WHERE id = %s", (doc_id,))
        doc = camel(cur.fetchone())
    return ok({"document": doc}, 201)


@bachatgat_bp.get("/documents")
def list_documents():
    entity_type = request.args.get("entityType")
    entity_id = request.args.get("entityId", type=int)
    with tx() as cur:
        if entity_type and entity_id:
            _entity_group(cur, entity_type, entity_id)
            cur.execute(DOC_META + " WHERE owner_id = %s AND entity_type = %s AND entity_id = %s ORDER BY id DESC",
                        (g.owner_id, entity_type, entity_id))
        else:
            group_id = request.args.get("groupId", type=int)
            if not group_id:
                raise ApiError(422, "groupId or entityType+entityId is required")
            load_group(cur, group_id)
            cur.execute(DOC_META + " WHERE owner_id = %s AND group_id = %s ORDER BY id DESC LIMIT 200", (g.owner_id, group_id))
        docs = [camel(r) for r in cur.fetchall()]
    return ok({"documents": docs})


@bachatgat_bp.get("/documents/<int:doc_id>/content")
def document_content(doc_id):
    with tx() as cur:
        cur.execute("SELECT file_name, mime_type, content, group_id FROM shg_documents WHERE id = %s AND owner_id = %s",
                    (doc_id, g.owner_id))
        doc = cur.fetchone()
        if not doc:
            raise ApiError(404, "Document not found")
    return ok({"fileName": doc["file_name"], "mimeType": doc["mime_type"],
               "fileBase64": base64.b64encode(bytes(doc["content"])).decode("ascii")})


@bachatgat_bp.post("/documents/<int:doc_id>/verify")
def verify_document(doc_id):
    payload = body()
    status = choice(payload, "status", ("Verified", "Rejected"))
    remarks = text(payload, "remarks", 300)
    if status == "Rejected" and not remarks:
        raise ApiError(422, "Give a reason for rejection")
    with tx() as cur:
        cur.execute(
            "UPDATE shg_documents SET verification_status = %s, verification_remarks = %s WHERE id = %s AND owner_id = %s RETURNING group_id",
            (status, remarks, doc_id, g.owner_id),
        )
        row = cur.fetchone()
        if not row:
            raise ApiError(404, "Document not found")
        record_event(cur, row["group_id"], "DOCUMENT", doc_id, f"DOCUMENT_{status.upper()}", details={"remarks": remarks})
        cur.execute(DOC_META + " WHERE id = %s", (doc_id,))
        doc = camel(cur.fetchone())
    return ok({"document": doc})


@bachatgat_bp.delete("/documents/<int:doc_id>")
def delete_document(doc_id):
    with tx() as cur:
        cur.execute("SELECT group_id, verification_status, file_name FROM shg_documents WHERE id = %s AND owner_id = %s",
                    (doc_id, g.owner_id))
        doc = cur.fetchone()
        if not doc:
            raise ApiError(404, "Document not found")
        if doc["verification_status"] == "Verified":
            raise ApiError(409, "Verified documents are kept for the audit trail")
        cur.execute("UPDATE shg_members SET photo_document_id = NULL WHERE photo_document_id = %s", (doc_id,))
        cur.execute("DELETE FROM shg_documents WHERE id = %s", (doc_id,))
        record_event(cur, doc["group_id"], "DOCUMENT", doc_id, "DELETED", details={"fileName": doc["file_name"]})
    return ok({"deleted": True})


# ---------------------------------------------------------------------------
# Savings
# ---------------------------------------------------------------------------
def period_bounds(frequency, anchor, day):
    """The savings period containing `day`: monthly periods start on the 1st,
    weekly/fortnightly periods are counted from the group's formation date."""
    if frequency == "MONTHLY":
        return date(day.year, day.month, 1)
    length = 7 if frequency == "WEEKLY" else 14
    offset = (day - anchor).days // length
    return anchor + timedelta(days=offset * length)


def due_date_for(frequency, period_start, due_day):
    if frequency == "MONTHLY":
        return period_start.replace(day=due_day)
    return period_start + timedelta(days=6 if frequency == "WEEKLY" else 13)


def generate_dues_for(cur, group_id, up_to):
    """Creates the savings dues of every active member for each period up to
    `up_to`. Safe to run repeatedly: existing dues are kept. Returns the count created.
    Used by the Generate button and by the Autopilot."""
    group = load_group(cur, group_id, lock=True)
    require_active_group(group)
    policy = load_policy(cur, group_id)
    if money(policy["savings_amount"]) <= 0:
        raise ApiError(422, "Set the savings amount in the group policy first")
    freq, anchor = policy["savings_frequency"], group["formation_date"]
    cur.execute("SELECT id, joining_date FROM shg_members WHERE group_id = %s AND status = 'Active'", (group_id,))
    members = cur.fetchall()
    created = 0
    last_period = period_bounds(freq, anchor, up_to)
    for m in members:
        joined = max(m["joining_date"], anchor)
        start = period_bounds(freq, anchor, joined)
        if freq == "MONTHLY" and due_date_for(freq, start, policy["savings_due_day"]) < joined:
            start = add_period(start, "MONTHLY", 1)
        k = 0
        while True:
            if freq == "MONTHLY":
                p = add_period(start, "MONTHLY", k)
            else:
                p = start + timedelta(days=k * (7 if freq == "WEEKLY" else 14))
            if p > last_period:
                break
            cur.execute(
                """INSERT INTO shg_savings_dues (group_id, member_id, period_start, due_date, due_amount)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (member_id, period_start) DO NOTHING RETURNING id""",
                (group_id, m["id"], p, due_date_for(freq, p, policy["savings_due_day"]), policy["savings_amount"]),
            )
            created += 1 if cur.fetchone() else 0
            k += 1
            if k > 600:
                break
    if created:
        record_event(cur, group_id, "SAVINGS", group_id, "DUES_GENERATED", details={"upTo": up_to, "created": created})
    return created


@bachatgat_bp.post("/savings/dues/generate")
def generate_dues():
    payload = body()
    group_id = parse_int(payload, "groupId", required=True, label="Group")
    up_to = parse_date(payload, "upTo", default=date.today())
    if up_to > date.today() + timedelta(days=62):
        raise ApiError(422, "Dues can be generated at most two months ahead")
    with tx() as cur:
        created = generate_dues_for(cur, group_id, up_to)
    return ok({"created": created, "upTo": up_to})


@bachatgat_bp.get("/savings/dues")
def list_dues():
    size, offset, page = page_args(50, 200)
    group_id = request.args.get("groupId", type=int)
    if not group_id:
        raise ApiError(422, "groupId is required")
    where, params = ["d.group_id = %s"], [group_id]
    if request.args.get("memberId"):
        where.append("d.member_id = %s")
        params.append(request.args.get("memberId", type=int))
    status = request.args.get("status")
    if status == "open":
        where.append("d.status IN ('Pending', 'Partial')")
    elif status == "overdue":
        where.append("d.status IN ('Pending', 'Partial') AND d.due_date < CURRENT_DATE")
    elif status:
        where.append("d.status = %s")
        params.append(status)
    if request.args.get("period"):
        where.append("d.period_start = %s")
        params.append(request.args["period"])
    with tx() as cur:
        load_group(cur, group_id)
        cur.execute(
            f"""SELECT d.*, m.full_name AS member_name, m.member_number, (d.due_amount - d.paid_amount) AS balance
                FROM shg_savings_dues d JOIN shg_members m ON m.id = d.member_id
                WHERE {' AND '.join(where)} ORDER BY d.period_start DESC, m.member_number LIMIT %s OFFSET %s""",
            params + [size, offset],
        )
        dues = [camel(r) for r in cur.fetchall()]
        cur.execute(
            f"""SELECT COALESCE(SUM(d.due_amount), 0) AS due, COALESCE(SUM(d.paid_amount), 0) AS paid,
                       COUNT(*) FILTER (WHERE d.status IN ('Pending', 'Partial')) AS open_count
                FROM shg_savings_dues d WHERE {' AND '.join(where)}""",
            params,
        )
        totals = camel(cur.fetchone())
    return ok({"dues": dues, "totals": totals, "page": page})


@bachatgat_bp.get("/savings/member-due")
def member_due():
    member_id = request.args.get("memberId", type=int)
    if not member_id:
        raise ApiError(422, "memberId is required")
    with tx() as cur:
        member = load_member(cur, member_id)
        policy = load_policy(cur, member["group_id"])
        cur.execute(
            """SELECT id, period_start, due_date, due_amount, paid_amount, status, (due_amount - paid_amount) AS balance
               FROM shg_savings_dues WHERE member_id = %s AND status IN ('Pending', 'Partial')
               ORDER BY period_start""",
            (member_id,),
        )
        open_dues = [camel(r) for r in cur.fetchall()]
        due_now = sum((money(d["balance"]) for d in open_dues if d["dueDate"] <= date.today().isoformat()), ZERO)
        return ok({
            "member": {"id": member["id"], "fullName": member["full_name"], "memberNumber": member["member_number"],
                       "status": member["status"], "groupId": member["group_id"]},
            "savingsBalance": member_savings_balance(cur, member["group_id"], member_id),
            "dueNow": due_now,
            "openDues": open_dues,
            "standardAmount": policy["savings_amount"],
        })


def record_savings_deposit(cur, group, member, amount, txn_date, method, bank_account_id=None,
                           narration=None, reference_number=None):
    """Posts a member's savings payment and applies it to her open dues, oldest first
    (anything extra stays as voluntary savings). Used by counter collection and by
    online (Razorpay) payments, so both produce the same receipt, ledger and dues."""
    cur.execute(
        "SELECT id, period_start, due_amount, paid_amount FROM shg_savings_dues WHERE member_id = %s AND status IN ('Pending', 'Partial') FOR UPDATE",
        (member["id"],),
    )
    dues = [{"id": r["id"], "periodStart": r["period_start"], "dueAmount": r["due_amount"], "paidAmount": r["paid_amount"]}
            for r in cur.fetchall()]
    allocations, voluntary = allocate_savings(amount, dues)
    txn = post_transaction(cur, group, "SAVINGS_DEPOSIT", amount, txn_date, method, bank_account_id,
                           member_id=member["id"], narration=narration or "Savings contribution",
                           reference_number=reference_number, receipt_prefix="SAV")
    for a in allocations:
        cur.execute(
            """UPDATE shg_savings_dues SET paid_amount = paid_amount + %s,
                   status = CASE WHEN paid_amount + %s >= due_amount THEN 'Paid' ELSE 'Partial' END
               WHERE id = %s""",
            (a["amount"], a["amount"], a["dueId"]),
        )
        cur.execute("INSERT INTO shg_allocations (txn_id, savings_due_id, amount) VALUES (%s, %s, %s)",
                    (txn["id"], a["dueId"], a["amount"]))
    notify(cur, group["id"], "TRANSACTION_CONFIRMATION", "Savings recorded",
           f"Savings receipt {txn['receipt_number']} issued to member #{member['member_number']}.",
           f"txn-{txn['id']}", "TRANSACTION", txn["id"])
    return txn, allocations, voluntary


@bachatgat_bp.post("/savings/collect")
@idempotent
def collect_savings(cur):
    payload = body()
    member_id = parse_int(payload, "memberId", required=True, label="Member")
    amount = parse_money(payload, "amount", required=True, positive=True, label="Amount")
    method = choice(payload, "paymentMethod", PAYMENT_METHODS, default="Cash", label="Payment method")
    txn_date = parse_date(payload, "txnDate", default=date.today())
    member = load_member(cur, member_id, lock=True)
    if member["status"] != "Active":
        raise ApiError(409, "Savings can be collected only from active members")
    group = load_group(cur, member["group_id"])
    txn, allocations, voluntary = record_savings_deposit(
        cur, group, member, amount, txn_date, method, payload.get("bankAccountId"),
        narration=text(payload, "notes", 300), reference_number=text(payload, "referenceNumber", 80),
    )
    return {
        "transaction": fetch_txn(cur, txn["id"]),
        "allocations": allocations,
        "voluntary": voluntary,
        "savingsBalance": member_savings_balance(cur, group["id"], member_id),
    }, 201


@bachatgat_bp.post("/savings/withdraw")
@idempotent
def withdraw_savings(cur):
    payload = body()
    member_id = parse_int(payload, "memberId", required=True, label="Member")
    amount = parse_money(payload, "amount", required=True, positive=True, label="Amount")
    method = choice(payload, "paymentMethod", PAYMENT_METHODS, default="Cash")
    txn_date = parse_date(payload, "txnDate", default=date.today())
    member = load_member(cur, member_id, lock=True)
    if member["status"] != "Active":
        raise ApiError(409, "Only active members can withdraw savings (use Exit & settlement for leaving members)")
    group = load_group(cur, member["group_id"])
    policy = load_policy(cur, group["id"])
    if not policy["allow_savings_withdrawal"]:
        raise ApiError(403, "The group policy does not allow savings withdrawals")
    balance = member_savings_balance(cur, group["id"], member_id)
    cur.execute(
        "SELECT COALESCE(SUM(l.debit - l.credit), 0) AS owed FROM shg_ledger_lines l JOIN shg_loans ln ON ln.id = l.loan_id "
        "WHERE ln.member_id = %s AND l.account_code = 'LOAN_RECEIVABLE'",
        (member_id,),
    )
    lien = money(cur.fetchone()["owed"])
    if amount > balance - lien:
        raise ApiError(422, f"Withdrawable savings are {max(balance - lien, ZERO)} (balance {balance}, loan outstanding {lien})")
    res_id = parse_int(payload, "resolutionId", label="Resolution")
    txn = post_transaction(cur, group, "SAVINGS_WITHDRAWAL", amount, txn_date, method, payload.get("bankAccountId"),
                           member_id=member_id, resolution_id=res_id, receipt_prefix="SAV",
                           narration=text(payload, "notes", 300) or "Savings withdrawal",
                           reference_number=text(payload, "referenceNumber", 80))
    use_resolution(cur, group["id"], res_id, "SAVINGS_WITHDRAWAL", "TRANSACTION", txn["id"], amount=amount)
    return {"transaction": fetch_txn(cur, txn["id"]), "savingsBalance": member_savings_balance(cur, group["id"], member_id)}, 201


@bachatgat_bp.get("/savings/ledger")
def savings_ledger():
    group_id = request.args.get("groupId", type=int)
    member_id = request.args.get("memberId", type=int)
    with tx() as cur:
        if member_id:
            member = load_member(cur, member_id)
            group_id = member["group_id"]
        elif group_id:
            load_group(cur, group_id)
        else:
            raise ApiError(422, "groupId or memberId is required")
        sql = """SELECT l.txn_date, t.txn_number, t.receipt_number, t.txn_type, t.payment_method, t.posting_status,
                        m.full_name AS member_name, m.member_number, l.credit AS deposit, l.debit AS withdrawal,
                        SUM(l.credit - l.debit) OVER (ORDER BY l.txn_date, l.id) AS running_balance
                 FROM shg_ledger_lines l JOIN shg_transactions t ON t.id = l.txn_id
                 LEFT JOIN shg_members m ON m.id = l.member_id
                 WHERE l.group_id = %s AND l.account_code = 'MEMBER_SAVINGS'"""
        params = [group_id]
        if member_id:
            sql += " AND l.member_id = %s"
            params.append(member_id)
        cur.execute(f"SELECT * FROM ({sql}) x ORDER BY txn_date DESC, txn_number DESC LIMIT 500", params)
        rows = [camel(r) for r in cur.fetchall()]
    return ok({"ledger": rows})


# Load the remaining route modules (they register on the same blueprint).
import bachatgat_loans_api  # noqa: E402,F401
import bachatgat_accounts_api  # noqa: E402,F401
import bachatgat_gateway  # noqa: E402,F401
import bachatgat_agent  # noqa: E402,F401
