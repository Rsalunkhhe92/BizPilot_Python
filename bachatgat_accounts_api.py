"""
Mahila Bachat Gat - accounting, livelihood business, bank/scheme linkage,
dashboard, notifications and reports.

Every figure here is computed from posted shg_ledger_lines / shg_transactions;
nothing is a stored running total that could drift from the books.
"""

import base64
import io
from datetime import date, timedelta

from flask import g, request

from bachatgat_common import (
    PAYMENT_METHODS,
    TXN_SELECT,
    ApiError,
    account_balance,
    assert_period_open,
    bachatgat_bp,
    body,
    camel,
    choice,
    fetch_txn,
    formatted_number,
    idempotent,
    load_bank_account,
    load_group,
    load_member,
    load_policy,
    mask_account,
    member_savings_balance,
    notify,
    ok,
    page_args,
    parse_date,
    parse_int,
    parse_money,
    post_transaction,
    record_event,
    require_active_group,
    serialize_txn,
    text,
    tx,
    use_resolution,
)
from bachatgat_finance import ACCOUNTS, ZERO, money
from bachatgat_loans_api import refresh_installment_status, sync_loan_status


def _fy_start(today=None):
    today = today or date.today()
    return date(today.year if today.month >= 4 else today.year - 1, 4, 1)


def _range(args=None):
    args = args if args is not None else request.args
    start = parse_date(args, "from", default=_fy_start())
    end = parse_date(args, "to", default=date.today())
    if start > end:
        raise ApiError(422, "From date is after To date")
    return start, end


def _group_arg(cur, required=True):
    group_id = request.args.get("groupId", type=int)
    if not group_id:
        if required:
            raise ApiError(422, "groupId is required")
        return None
    return load_group(cur, group_id)


# ---------------------------------------------------------------------------
# Transactions: list, record, reverse, receipt
# ---------------------------------------------------------------------------
@bachatgat_bp.get("/transactions")
def list_transactions():
    size, offset, page = page_args(30, 200)
    where, params = ["gr.owner_id = %s"], [g.owner_id]
    for arg, col in (("groupId", "t.group_id"), ("memberId", "t.member_id"), ("loanId", "t.loan_id"),
                     ("activityId", "t.activity_id"), ("schemeId", "t.scheme_id")):
        if request.args.get(arg):
            where.append(f"{col} = %s")
            params.append(request.args.get(arg, type=int))
    for arg, col in (("type", "t.txn_type"), ("classification", "t.classification"),
                     ("paymentMethod", "t.payment_method"), ("postingStatus", "t.posting_status")):
        if request.args.get(arg):
            where.append(f"{col} = %s")
            params.append(request.args[arg])
    if request.args.get("from"):
        where.append("t.txn_date >= %s")
        params.append(request.args["from"])
    if request.args.get("to"):
        where.append("t.txn_date <= %s")
        params.append(request.args["to"])
    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(t.txn_number ILIKE %s OR t.receipt_number ILIKE %s OR m.full_name ILIKE %s OR t.reference_number ILIKE %s)")
        params += [f"%{q}%"] * 4
    with tx() as cur:
        cur.execute(f"{TXN_SELECT} WHERE {' AND '.join(where)} ORDER BY t.txn_date DESC, t.id DESC LIMIT %s OFFSET %s",
                    params + [size, offset])
        rows = [serialize_txn(r) for r in cur.fetchall()]
    return ok({"transactions": rows, "page": page, "pageSize": size})


MANUAL_TYPES = ("OPERATING_EXPENSE", "OTHER_INCOME", "BANK_CHARGES", "BANK_INTEREST", "BANK_DEPOSIT",
                "BANK_WITHDRAWAL", "OPENING_BALANCE", "BANK_LOAN_REPAYMENT", "BUSINESS_EXPENSE", "BUSINESS_INCOME")


@bachatgat_bp.post("/transactions")
@idempotent
def record_transaction(cur):
    """Cashbook entries that are not member savings / loans / scheme receipts:
    expenses, other income, bank charges & interest, cash<->bank transfers,
    opening balances and bank-loan repayments."""
    payload = body()
    group = load_group(cur, parse_int(payload, "groupId", required=True, label="Group"))
    txn_type = choice(payload, "txnType", MANUAL_TYPES, label="Transaction type")
    amount = parse_money(payload, "amount", required=True, positive=True, label="Amount")
    txn_date = parse_date(payload, "txnDate", default=date.today())
    method = choice(payload, "paymentMethod", PAYMENT_METHODS, default="Cash")
    if txn_type in ("BANK_CHARGES", "BANK_INTEREST", "BANK_DEPOSIT", "BANK_WITHDRAWAL") and method == "Cash":
        method = "Bank Transfer"
    narration = text(payload, "narration", 300, required=txn_type in ("OPERATING_EXPENSE", "OTHER_INCOME", "BUSINESS_EXPENSE"),
                     label="Description")
    policy = load_policy(cur, group["id"])
    res_id = parse_int(payload, "resolutionId")
    split = None
    if txn_type == "OPENING_BALANCE":
        # Entries that were reversed (and their reversals) net to nothing, so they don't count.
        cur.execute(
            """SELECT COUNT(*) AS n FROM shg_transactions WHERE group_id = %s AND posting_status = 'Posted'
               AND txn_type NOT IN ('OPENING_BALANCE', 'REVERSAL')""",
            (group["id"],),
        )
        if cur.fetchone()["n"]:
            raise ApiError(409, "Opening balances can be entered only before other transactions")
    if txn_type == "BANK_LOAN_REPAYMENT":
        scheme_id = parse_int(payload, "schemeId", required=True, label="Bank loan")
        interest = parse_money(payload, "interestAmount", label="Interest part")
        split = {"principal": amount - interest, "interest": interest}
        if split["principal"] < 0:
            raise ApiError(422, "Interest part is more than the amount")
    else:
        scheme_id = parse_int(payload, "schemeId")
    activity_id = parse_int(payload, "activityId")
    if txn_type.startswith("BUSINESS_"):
        if not activity_id:
            raise ApiError(422, "Choose the business activity")
        _load_activity(cur, activity_id, group["id"])
    if scheme_id:
        _load_scheme(cur, scheme_id, group["id"])
    needs_resolution = txn_type in ("OPERATING_EXPENSE", "BUSINESS_EXPENSE") and money(policy["expense_approval_limit"]) > 0 \
        and amount > money(policy["expense_approval_limit"])
    if needs_resolution and not res_id:
        raise ApiError(422, f"Expenses above {policy['expense_approval_limit']} need an EXPENSE_APPROVAL resolution")
    txn = post_transaction(cur, group, txn_type, amount, txn_date, method, payload.get("bankAccountId"),
                           activity_id=activity_id, scheme_id=scheme_id, resolution_id=res_id, split=split,
                           narration=narration, reference_number=text(payload, "referenceNumber", 80),
                           receipt_prefix="VCH" if txn_type in ("OPERATING_EXPENSE", "BUSINESS_EXPENSE", "BANK_LOAN_REPAYMENT") else "RCT")
    if res_id:
        use_resolution(cur, group["id"], res_id, "EXPENSE_APPROVAL", "TRANSACTION", txn["id"],
                       amount=amount if needs_resolution else None)
    return {"transaction": fetch_txn(cur, txn["id"])}, 201


@bachatgat_bp.get("/transactions/<int:txn_id>")
def get_transaction(txn_id):
    with tx() as cur:
        txn = fetch_txn(cur, txn_id)
        if not txn:
            raise ApiError(404, "Transaction not found")
        cur.execute(
            "SELECT account_code, bank_account_id, debit, credit FROM shg_ledger_lines WHERE txn_id = %s ORDER BY id", (txn_id,)
        )
        lines = [camel(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM shg_allocations WHERE txn_id = %s", (txn_id,))
        allocations = [camel(r) for r in cur.fetchall()]
    return ok({"transaction": txn, "lines": lines, "allocations": allocations})


@bachatgat_bp.get("/transactions/<int:txn_id>/receipt")
def transaction_receipt(txn_id):
    with tx() as cur:
        txn = fetch_txn(cur, txn_id)
        if not txn:
            raise ApiError(404, "Transaction not found")
        group = load_group(cur, txn["groupId"])
        extra = {}
        if txn["memberId"]:
            extra["savingsBalance"] = member_savings_balance(cur, group["id"], txn["memberId"])
        if txn["loanId"]:
            extra["loanPrincipalOutstanding"] = account_balance(cur, group["id"], "LOAN_RECEIVABLE", loan_id=txn["loanId"])
        cur.execute("SELECT full_name, role FROM shg_members WHERE group_id = %s AND status = 'Active' AND role IN ('President', 'Treasurer')",
                    (group["id"],))
        signatories = {r["role"]: r["full_name"] for r in cur.fetchall()}
    return ok({
        "receipt": {
            **txn, **extra,
            "groupNameEn": group["name_en"], "groupNameMr": group["name_mr"], "groupCode": group["group_code"],
            "village": group["village"], "signatories": signatories,
        }
    })


@bachatgat_bp.post("/transactions/<int:txn_id>/reverse")
@idempotent
def reverse_transaction(cur, txn_id):
    """Authorized correction: posts the mirror entry dated today, marks the
    original Reversed and undoes what it paid (dues, installments, orders).
    Posted transactions are never edited or deleted."""
    payload = body()
    reason = text(payload, "reason", 300, required=True, label="Reason")
    cur.execute("SELECT * FROM shg_transactions WHERE id = %s AND owner_id = %s FOR UPDATE", (txn_id, g.owner_id))
    orig = cur.fetchone()
    if not orig:
        raise ApiError(404, "Transaction not found")
    if orig["posting_status"] == "Reversed":
        raise ApiError(409, "Already reversed")
    if orig["txn_type"] == "REVERSAL":
        raise ApiError(409, "A reversal cannot itself be reversed; post a fresh entry instead")
    if orig["txn_type"] == "LOAN_WRITE_OFF":
        raise ApiError(409, "Write-offs are final; record any later recovery as other income")
    if orig["txn_type"] == "OPENING_BALANCE":
        cur.execute("SELECT COUNT(*) AS n FROM shg_transactions WHERE group_id = %s AND posting_status = 'Posted' "
                    "AND txn_type NOT IN ('OPENING_BALANCE', 'REVERSAL') AND id > %s",
                    (orig["group_id"], txn_id))
        if cur.fetchone()["n"]:
            raise ApiError(409, "Opening balances cannot be reversed after other transactions exist")
    group = load_group(cur, orig["group_id"])

    if orig["txn_type"] in ("LOAN_REPAYMENT", "SAVINGS_LOAN_OFFSET"):
        cur.execute(
            """SELECT COUNT(*) AS n FROM shg_transactions WHERE loan_id = %s AND id > %s AND posting_status = 'Posted'
               AND txn_type IN ('LOAN_REPAYMENT', 'SAVINGS_LOAN_OFFSET', 'LOAN_WRITE_OFF')""",
            (orig["loan_id"], txn_id),
        )
        if cur.fetchone()["n"]:
            raise ApiError(409, "Reverse the later repayments of this loan first")
    if orig["txn_type"] == "LOAN_DISBURSEMENT":
        cur.execute(
            "SELECT COUNT(*) AS n FROM shg_transactions WHERE loan_id = %s AND id <> %s AND posting_status = 'Posted' AND txn_type <> 'REVERSAL'",
            (orig["loan_id"], txn_id),
        )
        if cur.fetchone()["n"]:
            raise ApiError(409, "Reverse the repayments of this loan before reversing its disbursement")

    cur.execute("SELECT * FROM shg_ledger_lines WHERE txn_id = %s ORDER BY id", (txn_id,))
    lines = [(l["account_code"], l["credit"], l["debit"], None) for l in cur.fetchall()]
    reversal = post_transaction(
        cur, group, "REVERSAL", orig["amount"], date.today(), orig["payment_method"], orig["bank_account_id"],
        member_id=orig["member_id"], loan_id=orig["loan_id"], activity_id=orig["activity_id"], order_id=orig["order_id"],
        scheme_id=orig["scheme_id"], narration=f"Reversal of {orig['txn_number']}: {reason}", lines=lines,
        split={"principal": orig["principal_amount"], "interest": orig["interest_amount"], "penalty": orig["penalty_amount"]},
    )
    cur.execute("UPDATE shg_transactions SET reversal_of_id = %s, reversal_reason = %s WHERE id = %s",
                (txn_id, reason, reversal["id"]))
    cur.execute("UPDATE shg_transactions SET posting_status = 'Reversed', reversed_by_id = %s, reversal_reason = %s WHERE id = %s",
                (reversal["id"], reason, txn_id))

    # Undo the operational effects.
    cur.execute("SELECT * FROM shg_allocations WHERE txn_id = %s", (txn_id,))
    for a in cur.fetchall():
        if a["savings_due_id"]:
            cur.execute(
                """UPDATE shg_savings_dues SET paid_amount = paid_amount - %s,
                       status = CASE WHEN paid_amount - %s <= 0 THEN 'Pending' ELSE 'Partial' END WHERE id = %s""",
                (a["amount"], a["amount"], a["savings_due_id"]),
            )
        if a["installment_id"]:
            cur.execute(
                """UPDATE shg_loan_installments SET principal_paid = principal_paid - %s, interest_paid = interest_paid - %s,
                       penalty_paid = penalty_paid - %s WHERE id = %s""",
                (a["principal"], a["interest"], a["penalty"], a["installment_id"]),
            )
            refresh_installment_status(cur, a["installment_id"])
    if orig["txn_type"] in ("LOAN_REPAYMENT", "SAVINGS_LOAN_OFFSET"):
        cur.execute("UPDATE shg_loans SET status = 'Active', closed_on = NULL WHERE id = %s AND status = 'Closed'", (orig["loan_id"],))
        sync_loan_status(cur, orig["loan_id"])
    if orig["txn_type"] == "LOAN_DISBURSEMENT":
        # Superseded, not deleted: reversed repayments' allocations still point at these rows.
        cur.execute("UPDATE shg_loan_installments SET status = 'Rescheduled' WHERE loan_id = %s", (orig["loan_id"],))
        cur.execute(
            """UPDATE shg_loans SET status = 'Approved', disbursed_on = NULL, first_due_date = NULL,
                   disbursement_authorized_by = NULL, version = version + 1 WHERE id = %s""",
            (orig["loan_id"],),
        )
        record_event(cur, group["id"], "LOAN", orig["loan_id"], "DISBURSEMENT_REVERSED", "Active", "Approved", {"reason": reason})
    if orig["txn_type"] == "SAVINGS_DEPOSIT" and member_savings_balance(cur, group["id"], orig["member_id"]) < 0:
        raise ApiError(409, "The member has already withdrawn these savings; reversal would make her balance negative")
    if orig["order_id"]:
        cur.execute(
            """UPDATE shg_business_orders SET paid_amount = paid_amount - %s,
                   status = CASE WHEN paid_amount - %s <= 0 THEN 'Open' ELSE 'Partial' END WHERE id = %s""",
            (orig["amount"], orig["amount"], orig["order_id"]),
        )
    cur.execute("UPDATE shg_business_work SET paid_txn_id = NULL WHERE paid_txn_id = %s", (txn_id,))
    cur.execute("DELETE FROM shg_scheme_utilizations WHERE txn_id = %s", (txn_id,))
    record_event(cur, group["id"], "TRANSACTION", txn_id, "REVERSED", "Posted", "Reversed",
                 {"reason": reason, "reversalId": reversal["id"]})
    return {"original": fetch_txn(cur, txn_id), "reversal": fetch_txn(cur, reversal["id"])}, 201


# ---------------------------------------------------------------------------
# Books: cashbook, bankbook, trial balance, I&E, financial position
# ---------------------------------------------------------------------------
def book(cur, group_id, account, start, end, bank_account_id=None):
    extra, params = "", []
    if bank_account_id:
        extra = " AND l.bank_account_id = %s"
        params = [bank_account_id]
    cur.execute(
        f"SELECT COALESCE(SUM(debit - credit), 0) AS bal FROM shg_ledger_lines l WHERE l.group_id = %s AND l.account_code = %s AND l.txn_date < %s{extra}",
        [group_id, account, start] + params,
    )
    opening = money(cur.fetchone()["bal"])
    cur.execute(
        f"""SELECT l.txn_date, t.id AS txn_id, t.txn_number, t.receipt_number, t.txn_type, t.classification, t.narration,
                   t.payment_method, t.reference_number, t.posting_status, t.bank_cleared_on, m.full_name AS member_name,
                   l.debit AS receipt, l.credit AS payment
            FROM shg_ledger_lines l JOIN shg_transactions t ON t.id = l.txn_id LEFT JOIN shg_members m ON m.id = t.member_id
            WHERE l.group_id = %s AND l.account_code = %s AND l.txn_date BETWEEN %s AND %s{extra}
            ORDER BY l.txn_date, l.id""",
        [group_id, account, start, end] + params,
    )
    rows, running = [], opening
    receipts = payments = ZERO
    for r in cur.fetchall():
        running += money(r["receipt"]) - money(r["payment"])
        receipts += money(r["receipt"])
        payments += money(r["payment"])
        item = camel(r)
        item["balance"] = running
        rows.append(item)
    return {"opening": opening, "rows": rows, "totalReceipts": receipts, "totalPayments": payments, "closing": running}


@bachatgat_bp.get("/accounts/cashbook")
def cashbook():
    start, end = _range()
    with tx() as cur:
        group = _group_arg(cur)
        return ok({"cashbook": book(cur, group["id"], "CASH", start, end), "from": start, "to": end})


@bachatgat_bp.get("/accounts/bankbook")
def bankbook():
    start, end = _range()
    with tx() as cur:
        group = _group_arg(cur)
        bank = load_bank_account(cur, group["id"], request.args.get("bankAccountId", type=int))
        data = book(cur, group["id"], "BANK", start, end, bank["id"])
        return ok({"bankbook": data, "bankAccount": {"id": bank["id"], "bankName": bank["bank_name"],
                   "accountNumberMasked": mask_account(bank["account_number"])}, "from": start, "to": end})


def trial_balance(cur, group_id, as_of):
    cur.execute(
        """SELECT account_code, COALESCE(SUM(debit), 0) AS debit, COALESCE(SUM(credit), 0) AS credit
           FROM shg_ledger_lines WHERE group_id = %s AND txn_date <= %s GROUP BY account_code ORDER BY account_code""",
        (group_id, as_of),
    )
    rows = []
    for r in cur.fetchall():
        bal = money(r["debit"]) - money(r["credit"])
        rows.append({"account": r["account_code"], "classification": ACCOUNTS.get(r["account_code"]),
                     "debit": bal if bal > 0 else ZERO, "credit": -bal if bal < 0 else ZERO})
    return rows


def income_expenditure(cur, group_id, start, end):
    cur.execute(
        """SELECT account_code, COALESCE(SUM(credit - debit), 0) AS net FROM shg_ledger_lines
           WHERE group_id = %s AND txn_date BETWEEN %s AND %s GROUP BY account_code""",
        (group_id, start, end),
    )
    nets = {r["account_code"]: money(r["net"]) for r in cur.fetchall()}
    income = [{"account": a, "amount": nets.get(a, ZERO)} for a, c in ACCOUNTS.items() if c == "INCOME"]
    expense = [{"account": a, "amount": -nets.get(a, ZERO)} for a, c in ACCOUNTS.items() if c == "EXPENSE"]
    biz_income = nets.get("BUSINESS_INCOME", ZERO)
    biz_expense = -nets.get("BUSINESS_EXPENSE", ZERO)
    total_income = sum((i["amount"] for i in income), ZERO)
    total_expense = sum((e["amount"] for e in expense), ZERO)
    return {
        "income": income, "expenditure": expense, "totalIncome": total_income, "totalExpenditure": total_expense,
        "groupSurplus": total_income - total_expense,
        "business": {"income": biz_income, "expenditure": biz_expense, "surplus": biz_income - biz_expense},
        "consolidatedSurplus": total_income - total_expense + biz_income - biz_expense,
    }


def financial_position(cur, group_id, as_of):
    bal = {r["account"]: r["debit"] - r["credit"] for r in trial_balance(cur, group_id, as_of)}
    get = lambda a: bal.get(a, ZERO)  # noqa: E731
    assets = [{"account": a, "amount": get(a)} for a in ("CASH", "BANK", "LOAN_RECEIVABLE")]
    liabilities = [{"account": a, "amount": -get(a)} for a in ("MEMBER_SAVINGS", "BANK_LOAN")]
    funds = [{"account": "GRANT_FUND", "amount": -get("GRANT_FUND")}, {"account": "OPENING_EQUITY", "amount": -get("OPENING_EQUITY")}]
    surplus = -sum((v for a, v in bal.items() if ACCOUNTS.get(a) in ("INCOME", "EXPENSE", "BUSINESS_INCOME", "BUSINESS_EXPENSE")), ZERO)
    funds.append({"account": "ACCUMULATED_SURPLUS", "amount": surplus})
    total_assets = sum((x["amount"] for x in assets), ZERO)
    total_claims = sum((x["amount"] for x in liabilities + funds), ZERO)
    return {"asOf": as_of, "assets": assets, "liabilities": liabilities, "funds": funds,
            "totalAssets": total_assets, "totalLiabilitiesAndFunds": total_claims,
            "balanced": total_assets == total_claims}


@bachatgat_bp.get("/accounts/summary")
def accounts_summary():
    start, end = _range()
    with tx() as cur:
        group = _group_arg(cur)
        gid = group["id"]
        cur.execute("SELECT id, bank_name, account_number, is_primary FROM shg_bank_accounts WHERE group_id = %s AND active ORDER BY is_primary DESC, id", (gid,))
        banks = [{"id": b["id"], "bankName": b["bank_name"], "accountNumberMasked": mask_account(b["account_number"]),
                  "balance": account_balance(cur, gid, "BANK", bank_account_id=b["id"])} for b in cur.fetchall()]
        return ok({
            "cash": account_balance(cur, gid, "CASH"),
            "banks": banks,
            "trialBalance": trial_balance(cur, gid, end),
            "incomeExpenditure": income_expenditure(cur, gid, start, end),
            "financialPosition": financial_position(cur, gid, end),
            "from": start, "to": end,
        })


@bachatgat_bp.get("/accounts/reconciliation")
def reconciliation_view():
    with tx() as cur:
        group = _group_arg(cur)
        bank = load_bank_account(cur, group["id"], request.args.get("bankAccountId", type=int))
        as_of = parse_date(request.args, "statementDate", default=date.today())
        book_balance = account_balance(cur, group["id"], "BANK", as_of=as_of, bank_account_id=bank["id"])
        cur.execute(
            """SELECT t.id AS txn_id, t.txn_number, t.txn_date, t.txn_type, t.narration, t.reference_number,
                      l.debit AS deposit, l.credit AS withdrawal, t.bank_cleared_on
               FROM shg_ledger_lines l JOIN shg_transactions t ON t.id = l.txn_id
               WHERE l.group_id = %s AND l.account_code = 'BANK' AND l.bank_account_id = %s AND l.txn_date <= %s
                 AND (t.bank_cleared_on IS NULL OR t.bank_cleared_on > %s)
               ORDER BY l.txn_date, l.id""",
            (group["id"], bank["id"], as_of, as_of),
        )
        uncleared = [camel(r) for r in cur.fetchall()]
        net_uncleared = sum((money(u["deposit"]) - money(u["withdrawal"]) for u in uncleared), ZERO)
        cur.execute("SELECT * FROM shg_bank_reconciliations WHERE bank_account_id = %s ORDER BY statement_date DESC LIMIT 24", (bank["id"],))
        history = [camel(r) for r in cur.fetchall()]
    return ok({"bankAccountId": bank["id"], "statementDate": as_of, "bookBalance": book_balance,
               "uncleared": uncleared, "netUncleared": net_uncleared,
               "expectedStatementBalance": book_balance - net_uncleared, "history": history})


@bachatgat_bp.post("/accounts/reconciliation/clear")
def reconciliation_clear():
    payload = body()
    ids = [int(x) for x in (payload.get("txnIds") or [])]
    cleared_on = parse_date(payload, "clearedOn", default=date.today())
    if not ids:
        raise ApiError(422, "Choose transactions")
    with tx() as cur:
        cur.execute(
            """UPDATE shg_transactions SET bank_cleared_on = %s WHERE id = ANY(%s) AND owner_id = %s
                 AND bank_account_id IS NOT NULL AND txn_date <= %s RETURNING id, group_id""",
            (None if payload.get("unclear") else cleared_on, ids, g.owner_id, cleared_on),
        )
        rows = cur.fetchall()
        for r in rows:
            record_event(cur, r["group_id"], "TRANSACTION", r["id"], "BANK_UNCLEARED" if payload.get("unclear") else "BANK_CLEARED",
                         details={"clearedOn": cleared_on})
    return ok({"updated": len(rows)})


@bachatgat_bp.post("/accounts/reconciliation")
def reconciliation_save():
    payload = body()
    with tx() as cur:
        group = load_group(cur, parse_int(payload, "groupId", required=True))
        bank = load_bank_account(cur, group["id"], parse_int(payload, "bankAccountId", required=True))
        as_of = parse_date(payload, "statementDate", required=True, label="Statement date")
        statement = parse_money(payload, "statementBalance", required=True, label="Statement balance")
        book_balance = account_balance(cur, group["id"], "BANK", as_of=as_of, bank_account_id=bank["id"])
        cur.execute(
            """SELECT COALESCE(SUM(l.debit - l.credit), 0) AS n FROM shg_ledger_lines l JOIN shg_transactions t ON t.id = l.txn_id
               WHERE l.account_code = 'BANK' AND l.bank_account_id = %s AND l.txn_date <= %s
                 AND (t.bank_cleared_on IS NULL OR t.bank_cleared_on > %s)""",
            (bank["id"], as_of, as_of),
        )
        uncleared = money(cur.fetchone()["n"])
        difference = statement - (book_balance - uncleared)
        cur.execute(
            """INSERT INTO shg_bank_reconciliations (bank_account_id, statement_date, statement_balance, book_balance,
                   uncleared_amount, difference, notes, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (bank["id"], as_of, statement, book_balance, uncleared, difference, text(payload, "notes", 500), g.owner_id),
        )
        rec = cur.fetchone()
        record_event(cur, group["id"], "BANK_ACCOUNT", bank["id"], "RECONCILED", details={"statementDate": as_of, "difference": difference})
    return ok({"reconciliation": camel(rec)}, 201)


@bachatgat_bp.get("/accounts/period-closes")
def list_period_closes():
    with tx() as cur:
        group = _group_arg(cur)
        cur.execute("SELECT * FROM shg_period_closes WHERE group_id = %s ORDER BY period_end DESC", (group["id"],))
        return ok({"closes": [camel(r) for r in cur.fetchall()]})


@bachatgat_bp.post("/accounts/period-close")
def close_period():
    payload = body()
    with tx() as cur:
        group = load_group(cur, parse_int(payload, "groupId", required=True), lock=True)
        require_active_group(group)
        period_end = parse_date(payload, "periodEnd", required=True, label="Period end")
        if period_end >= date.today():
            raise ApiError(422, "Only past periods can be closed")
        cur.execute("SELECT MAX(period_end) AS last FROM shg_period_closes WHERE group_id = %s", (group["id"],))
        last = cur.fetchone()["last"]
        if last and period_end <= last:
            raise ApiError(409, f"Books are already closed up to {last.isoformat()}")
        tb = trial_balance(cur, group["id"], period_end)
        if sum((r["debit"] for r in tb), ZERO) != sum((r["credit"] for r in tb), ZERO):
            raise ApiError(500, "Trial balance does not agree; contact support before closing")
        cash = account_balance(cur, group["id"], "CASH", as_of=period_end)
        bank = account_balance(cur, group["id"], "BANK", as_of=period_end)
        cur.execute(
            """INSERT INTO shg_period_closes (group_id, period_end, cash_balance, bank_balance, notes, closed_by)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (group["id"], period_end, cash, bank, text(payload, "notes", 500), g.owner_id),
        )
        row = cur.fetchone()
        record_event(cur, group["id"], "GROUP", group["id"], "PERIOD_CLOSED", details={"periodEnd": period_end, "cash": cash, "bank": bank})
    return ok({"close": camel(row)}, 201)


# ---------------------------------------------------------------------------
# Business / livelihood activities
# ---------------------------------------------------------------------------
ACTIVITY_TYPES = ("Tailoring & Garments", "Food Processing", "Dairy & Animal Husbandry", "Agriculture & Nursery",
                  "Handicrafts", "Grocery & Retail", "Catering & Tiffin", "Other")


def _load_activity(cur, activity_id, group_id=None):
    cur.execute(
        """SELECT a.* FROM shg_business_activities a JOIN shg_groups gr ON gr.id = a.group_id
           WHERE a.id = %s AND gr.owner_id = %s""",
        (activity_id, g.owner_id),
    )
    act = cur.fetchone()
    if not act or (group_id and act["group_id"] != group_id):
        raise ApiError(404, "Business activity not found")
    return act


def _load_product(cur, product_id, lock=False):
    cur.execute(
        """SELECT p.*, a.group_id FROM shg_business_products p JOIN shg_business_activities a ON a.id = p.activity_id
           JOIN shg_groups gr ON gr.id = a.group_id WHERE p.id = %s AND gr.owner_id = %s""" + (" FOR UPDATE OF p" if lock else ""),
        (product_id, g.owner_id),
    )
    p = cur.fetchone()
    if not p:
        raise ApiError(404, "Product not found")
    return p


def _move_stock(cur, product, qty, mtype, on, order_id=None, notes=None):
    if product["kind"] != "Service":
        cur.execute("UPDATE shg_business_products SET stock_qty = stock_qty + %s WHERE id = %s RETURNING stock_qty", (qty, product["id"]))
        if cur.fetchone()["stock_qty"] < 0:
            raise ApiError(422, f"Not enough stock of {product['name']}")
    cur.execute(
        "INSERT INTO shg_stock_movements (product_id, movement_date, movement_type, quantity, order_id, notes) VALUES (%s,%s,%s,%s,%s,%s)",
        (product["id"], on, mtype, qty, order_id, notes),
    )


def business_pl(cur, activity_id, start, end):
    cur.execute(
        """SELECT account_code, COALESCE(SUM(credit - debit), 0) AS net FROM shg_ledger_lines
           WHERE activity_id = %s AND txn_date BETWEEN %s AND %s GROUP BY account_code""",
        (activity_id, start, end),
    )
    nets = {r["account_code"]: money(r["net"]) for r in cur.fetchall()}
    cur.execute(
        """SELECT order_type, COALESCE(SUM(total_amount), 0) AS total, COALESCE(SUM(total_amount - paid_amount), 0) AS balance
           FROM shg_business_orders WHERE activity_id = %s AND status <> 'Cancelled' AND order_date BETWEEN %s AND %s GROUP BY order_type""",
        (activity_id, start, end),
    )
    orders = {r["order_type"]: r for r in cur.fetchall()}
    cur.execute(
        """SELECT COALESCE(SUM(stock_qty * purchase_price), 0) AS cost_value, COALESCE(SUM(stock_qty * sale_price), 0) AS sale_value
           FROM shg_business_products WHERE activity_id = %s AND kind <> 'Service'""",
        (activity_id,),
    )
    stock = cur.fetchone()
    sales = money((orders.get("SALE") or {}).get("total"))
    purchases = money((orders.get("PURCHASE") or {}).get("total"))
    cash_income = nets.get("BUSINESS_INCOME", ZERO)
    cash_expense = -nets.get("BUSINESS_EXPENSE", ZERO)
    return {
        "cashBasis": {"income": cash_income, "expenses": cash_expense, "profit": cash_income - cash_expense},
        "salesBooked": sales, "purchasesBooked": purchases,
        "receivables": money((orders.get("SALE") or {}).get("balance")),
        "payables": money((orders.get("PURCHASE") or {}).get("balance")),
        "stockAtCost": money(stock["cost_value"]), "stockAtSalePrice": money(stock["sale_value"]),
    }


@bachatgat_bp.get("/business/activities")
def list_activities():
    with tx() as cur:
        group = _group_arg(cur)
        cur.execute(
            """SELECT a.*, (SELECT COUNT(*) FROM shg_business_products p WHERE p.activity_id = a.id AND p.active) AS product_count,
                      (SELECT COALESCE(SUM(credit - debit), 0) FROM shg_ledger_lines l WHERE l.activity_id = a.id) AS net_cash
               FROM shg_business_activities a WHERE a.group_id = %s ORDER BY a.status, a.name""",
            (group["id"],),
        )
        return ok({"activities": [camel(r) for r in cur.fetchall()], "activityTypes": ACTIVITY_TYPES})


@bachatgat_bp.post("/business/activities")
def create_activity():
    payload = body()
    with tx() as cur:
        group = load_group(cur, parse_int(payload, "groupId", required=True))
        require_active_group(group)
        cur.execute(
            """INSERT INTO shg_business_activities (group_id, name, activity_type, description, start_date)
               VALUES (%s, %s, %s, %s, %s) RETURNING *""",
            (group["id"], text(payload, "name", 150, required=True, label="Activity name"),
             choice(payload, "activityType", ACTIVITY_TYPES, label="Activity type"),
             text(payload, "description", 1000), parse_date(payload, "startDate", default=date.today())),
        )
        act = cur.fetchone()
        record_event(cur, group["id"], "BUSINESS", act["id"], "CREATED")
    return ok({"activity": camel(act)}, 201)


@bachatgat_bp.put("/business/activities/<int:activity_id>")
def update_activity(activity_id):
    payload = body()
    with tx() as cur:
        act = _load_activity(cur, activity_id)
        cur.execute(
            "UPDATE shg_business_activities SET name = %s, description = %s, status = %s WHERE id = %s RETURNING *",
            (text(payload, "name", 150) or act["name"], text(payload, "description", 1000) or act["description"],
             choice(payload, "status", ("Active", "Paused", "Closed"), default=act["status"]), activity_id),
        )
        updated = cur.fetchone()
        record_event(cur, act["group_id"], "BUSINESS", activity_id, "UPDATED", act["status"], updated["status"])
    return ok({"activity": camel(updated)})


@bachatgat_bp.get("/business/activities/<int:activity_id>")
def get_activity(activity_id):
    start, end = _range()
    with tx() as cur:
        act = _load_activity(cur, activity_id)
        cur.execute("SELECT * FROM shg_business_products WHERE activity_id = %s ORDER BY active DESC, kind, name", (activity_id,))
        products = [camel(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT o.*, p.name AS party_name FROM shg_business_orders o LEFT JOIN shg_business_parties p ON p.id = o.party_id
               WHERE o.activity_id = %s ORDER BY o.order_date DESC, o.id DESC LIMIT 50""",
            (activity_id,),
        )
        orders = [camel(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT w.*, m.full_name AS member_name FROM shg_business_work w JOIN shg_members m ON m.id = w.member_id
               WHERE w.activity_id = %s ORDER BY w.work_date DESC, w.id DESC LIMIT 100""",
            (activity_id,),
        )
        work = [camel(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM shg_business_assets WHERE activity_id = %s ORDER BY name", (activity_id,))
        assets = [camel(r) for r in cur.fetchall()]
        pl = business_pl(cur, activity_id, start, end)
    return ok({"activity": camel(act), "products": products, "orders": orders, "work": work, "assets": assets,
               "profitLoss": pl, "from": start, "to": end})


@bachatgat_bp.post("/business/activities/<int:activity_id>/products")
def add_product(activity_id):
    payload = body()
    with tx() as cur:
        act = _load_activity(cur, activity_id)
        cur.execute(
            """INSERT INTO shg_business_products (activity_id, name, kind, unit, sale_price, purchase_price, reorder_level)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (activity_id, text(payload, "name", 150, required=True, label="Name"),
             choice(payload, "kind", ("Product", "Raw Material", "Service"), default="Product"),
             text(payload, "unit", 20) or "pcs", parse_money(payload, "salePrice"), parse_money(payload, "purchasePrice"),
             parse_money(payload, "reorderLevel")),
        )
        p = cur.fetchone()
        record_event(cur, act["group_id"], "BUSINESS_PRODUCT", p["id"], "CREATED")
    return ok({"product": camel(p)}, 201)


@bachatgat_bp.put("/business/products/<int:product_id>")
def update_product(product_id):
    payload = body()
    with tx() as cur:
        p = _load_product(cur, product_id, lock=True)
        cur.execute(
            """UPDATE shg_business_products SET name = %s, unit = %s, sale_price = %s, purchase_price = %s,
                   reorder_level = %s, active = %s WHERE id = %s RETURNING *""",
            (text(payload, "name", 150) or p["name"], text(payload, "unit", 20) or p["unit"],
             parse_money(payload, "salePrice", default=str(p["sale_price"])),
             parse_money(payload, "purchasePrice", default=str(p["purchase_price"])),
             parse_money(payload, "reorderLevel", default=str(p["reorder_level"])),
             bool(payload.get("active", p["active"])), product_id),
        )
        out = cur.fetchone()
    return ok({"product": camel(out)})


@bachatgat_bp.post("/business/products/<int:product_id>/adjust")
def adjust_stock(product_id):
    payload = body()
    try:
        qty = money(payload.get("quantity"))
    except Exception:
        raise ApiError(422, "Quantity must be a number")
    if qty == 0:
        raise ApiError(422, "Quantity cannot be zero")
    reason = text(payload, "reason", 200, required=True, label="Reason")
    with tx() as cur:
        p = _load_product(cur, product_id, lock=True)
        _move_stock(cur, p, qty, "ADJUSTMENT", parse_date(payload, "date", default=date.today()), notes=reason)
        record_event(cur, p["group_id"], "BUSINESS_PRODUCT", product_id, "STOCK_ADJUSTED", details={"quantity": qty, "reason": reason})
        out = _load_product(cur, product_id)
    return ok({"product": camel(out)})


@bachatgat_bp.get("/business/products/<int:product_id>/movements")
def stock_movements(product_id):
    with tx() as cur:
        _load_product(cur, product_id)
        cur.execute(
            """SELECT s.*, o.order_number FROM shg_stock_movements s LEFT JOIN shg_business_orders o ON o.id = s.order_id
               WHERE s.product_id = %s ORDER BY s.movement_date DESC, s.id DESC LIMIT 300""",
            (product_id,),
        )
        return ok({"movements": [camel(r) for r in cur.fetchall()]})


@bachatgat_bp.post("/business/activities/<int:activity_id>/production")
def record_production(activity_id):
    """Finished goods produced (stock in) and raw materials consumed (stock out)."""
    payload = body()
    on = parse_date(payload, "date", default=date.today())
    with tx() as cur:
        act = _load_activity(cur, activity_id)
        product = _load_product(cur, parse_int(payload, "productId", required=True, label="Product"), lock=True)
        if product["activity_id"] != activity_id or product["kind"] != "Product":
            raise ApiError(422, "Choose a product of this activity")
        qty = money(payload.get("quantity"))
        if qty <= 0:
            raise ApiError(422, "Quantity must be greater than zero")
        notes = text(payload, "notes", 200)
        _move_stock(cur, product, qty, "PRODUCTION", on, notes=notes)
        for c in payload.get("consumption") or []:
            raw = _load_product(cur, int(c.get("productId") or 0), lock=True)
            if raw["activity_id"] != activity_id:
                raise ApiError(422, "Raw material belongs to another activity")
            used = money(c.get("quantity"))
            if used <= 0:
                raise ApiError(422, "Consumed quantity must be greater than zero")
            _move_stock(cur, raw, -used, "CONSUMPTION", on, notes=f"Used for {product['name']}")
        record_event(cur, act["group_id"], "BUSINESS", activity_id, "PRODUCTION", details={"productId": product["id"], "quantity": qty})
    return ok({"recorded": True}, 201)


@bachatgat_bp.get("/business/parties")
def list_parties():
    with tx() as cur:
        group = _group_arg(cur)
        params = [group["id"]]
        sql = "SELECT * FROM shg_business_parties WHERE group_id = %s"
        if request.args.get("type"):
            sql += " AND party_type = %s"
            params.append(request.args["type"])
        cur.execute(sql + " ORDER BY name", params)
        return ok({"parties": [camel(r) for r in cur.fetchall()]})


@bachatgat_bp.post("/business/parties")
def add_party():
    payload = body()
    from bachatgat_common import mobile as parse_mobile
    with tx() as cur:
        group = load_group(cur, parse_int(payload, "groupId", required=True))
        cur.execute(
            "INSERT INTO shg_business_parties (group_id, party_type, name, mobile, address) VALUES (%s,%s,%s,%s,%s) RETURNING *",
            (group["id"], choice(payload, "partyType", ("Customer", "Supplier"), label="Type"),
             text(payload, "name", 150, required=True, label="Name"), parse_mobile(payload, "mobile"), text(payload, "address", 500)),
        )
        return ok({"party": camel(cur.fetchone())}, 201)


def _pay_order(cur, group, order, amount, on, method, bank_id, reference):
    balance = money(order["total_amount"]) - money(order["paid_amount"])
    if amount > balance:
        raise ApiError(422, f"Payment exceeds the order balance of {balance}")
    txn_type = "BUSINESS_INCOME" if order["order_type"] == "SALE" else "BUSINESS_EXPENSE"
    txn = post_transaction(cur, group, txn_type, amount, on, method, bank_id, activity_id=order["activity_id"],
                           order_id=order["id"], reference_number=reference,
                           narration=f"{'Sale receipt' if order['order_type'] == 'SALE' else 'Supplier payment'} {order['order_number']}",
                           receipt_prefix="BRC" if order["order_type"] == "SALE" else "BPV")
    cur.execute(
        """UPDATE shg_business_orders SET paid_amount = paid_amount + %s,
               status = CASE WHEN paid_amount + %s >= total_amount THEN 'Paid' ELSE 'Partial' END WHERE id = %s""",
        (amount, amount, order["id"]),
    )
    return txn


@bachatgat_bp.post("/business/activities/<int:activity_id>/orders")
@idempotent
def create_order(cur, activity_id):
    payload = body()
    act = _load_activity(cur, activity_id)
    if act["status"] != "Active":
        raise ApiError(409, "Activity is not active")
    group = load_group(cur, act["group_id"])
    require_active_group(group)
    otype = choice(payload, "orderType", ("SALE", "PURCHASE"), label="Order type")
    on = parse_date(payload, "orderDate", default=date.today())
    assert_period_open(cur, group["id"], on)
    party_id = parse_int(payload, "partyId")
    if party_id:
        cur.execute("SELECT party_type FROM shg_business_parties WHERE id = %s AND group_id = %s", (party_id, group["id"]))
        party = cur.fetchone()
        if not party:
            raise ApiError(404, "Customer/supplier not found")
        if party["party_type"] != ("Customer" if otype == "SALE" else "Supplier"):
            raise ApiError(422, "Choose a customer for sales and a supplier for purchases")
    lines = payload.get("lines") or []
    if not lines:
        raise ApiError(422, "Add at least one item")
    number = formatted_number(cur, group, f"ORD-{otype}", "SO" if otype == "SALE" else "PO", on)
    cur.execute(
        """INSERT INTO shg_business_orders (activity_id, order_type, order_number, party_id, order_date, total_amount, notes)
           VALUES (%s, %s, %s, %s, %s, 0, %s) RETURNING *""",
        (activity_id, otype, number, party_id, on, text(payload, "notes", 500)),
    )
    order = cur.fetchone()
    total = ZERO
    for ln in lines:
        product = _load_product(cur, int(ln.get("productId") or 0), lock=True)
        if product["activity_id"] != activity_id:
            raise ApiError(422, "Item belongs to another activity")
        qty = money(ln.get("quantity"))
        rate = money(ln.get("rate") if ln.get("rate") not in (None, "") else (product["sale_price"] if otype == "SALE" else product["purchase_price"]))
        if qty <= 0 or rate < 0:
            raise ApiError(422, "Quantity must be positive and rate cannot be negative")
        amount = money(qty * rate)
        total += amount
        cur.execute("INSERT INTO shg_business_order_lines (order_id, product_id, quantity, rate, amount) VALUES (%s,%s,%s,%s,%s)",
                    (order["id"], product["id"], qty, rate, amount))
        _move_stock(cur, product, qty if otype == "PURCHASE" else -qty, otype, on, order["id"])
    if total <= 0:
        raise ApiError(422, "Order total must be greater than zero")
    cur.execute("UPDATE shg_business_orders SET total_amount = %s WHERE id = %s RETURNING *", (total, order["id"]))
    order = cur.fetchone()
    paid = parse_money(payload, "paidAmount")
    txn = None
    if paid > 0:
        txn = _pay_order(cur, group, order, paid, on, choice(payload, "paymentMethod", PAYMENT_METHODS, default="Cash"),
                         payload.get("bankAccountId"), text(payload, "referenceNumber", 80))
    record_event(cur, group["id"], "BUSINESS_ORDER", order["id"], f"{otype}_CREATED", details={"total": total})
    cur.execute("SELECT * FROM shg_business_orders WHERE id = %s", (order["id"],))
    return {"order": camel(cur.fetchone()), "transaction": fetch_txn(cur, txn["id"]) if txn else None}, 201


@bachatgat_bp.get("/business/orders/<int:order_id>")
def get_order(order_id):
    with tx() as cur:
        cur.execute(
            """SELECT o.*, p.name AS party_name, a.group_id FROM shg_business_orders o
               JOIN shg_business_activities a ON a.id = o.activity_id JOIN shg_groups gr ON gr.id = a.group_id
               LEFT JOIN shg_business_parties p ON p.id = o.party_id WHERE o.id = %s AND gr.owner_id = %s""",
            (order_id, g.owner_id),
        )
        order = cur.fetchone()
        if not order:
            raise ApiError(404, "Order not found")
        cur.execute(
            "SELECT l.*, p.name AS product_name, p.unit FROM shg_business_order_lines l JOIN shg_business_products p ON p.id = l.product_id WHERE l.order_id = %s",
            (order_id,),
        )
        lines = [camel(r) for r in cur.fetchall()]
        cur.execute(TXN_SELECT + " WHERE t.order_id = %s AND gr.owner_id = %s ORDER BY t.id", (order_id, g.owner_id))
        txns = [serialize_txn(r) for r in cur.fetchall()]
    return ok({"order": camel(order), "lines": lines, "transactions": txns})


@bachatgat_bp.post("/business/orders/<int:order_id>/payments")
@idempotent
def pay_order(cur, order_id):
    payload = body()
    cur.execute(
        """SELECT o.*, a.group_id FROM shg_business_orders o JOIN shg_business_activities a ON a.id = o.activity_id
           JOIN shg_groups gr ON gr.id = a.group_id WHERE o.id = %s AND gr.owner_id = %s FOR UPDATE OF o""",
        (order_id, g.owner_id),
    )
    order = cur.fetchone()
    if not order:
        raise ApiError(404, "Order not found")
    if order["status"] == "Cancelled":
        raise ApiError(409, "Order is cancelled")
    group = load_group(cur, order["group_id"])
    txn = _pay_order(cur, group, order, parse_money(payload, "amount", required=True, positive=True),
                     parse_date(payload, "txnDate", default=date.today()),
                     choice(payload, "paymentMethod", PAYMENT_METHODS, default="Cash"),
                     payload.get("bankAccountId"), text(payload, "referenceNumber", 80))
    return {"transaction": fetch_txn(cur, txn["id"])}, 201


@bachatgat_bp.post("/business/orders/<int:order_id>/cancel")
def cancel_order(order_id):
    payload = body()
    reason = text(payload, "reason", 200, required=True, label="Reason")
    with tx() as cur:
        cur.execute(
            """SELECT o.*, a.group_id FROM shg_business_orders o JOIN shg_business_activities a ON a.id = o.activity_id
               JOIN shg_groups gr ON gr.id = a.group_id WHERE o.id = %s AND gr.owner_id = %s FOR UPDATE OF o""",
            (order_id, g.owner_id),
        )
        order = cur.fetchone()
        if not order:
            raise ApiError(404, "Order not found")
        if order["status"] == "Cancelled":
            raise ApiError(409, "Already cancelled")
        if money(order["paid_amount"]) > 0:
            raise ApiError(409, "Reverse the payments of this order first")
        cur.execute("SELECT product_id, quantity FROM shg_business_order_lines WHERE order_id = %s", (order_id,))
        for ln in cur.fetchall():
            product = _load_product(cur, ln["product_id"], lock=True)
            qty = money(ln["quantity"])
            _move_stock(cur, product, -qty if order["order_type"] == "PURCHASE" else qty, "CANCEL", date.today(), order_id, reason)
        cur.execute("UPDATE shg_business_orders SET status = 'Cancelled' WHERE id = %s", (order_id,))
        record_event(cur, order["group_id"], "BUSINESS_ORDER", order_id, "CANCELLED", order["status"], "Cancelled", {"reason": reason})
    return ok({"cancelled": True})


@bachatgat_bp.post("/business/activities/<int:activity_id>/expenses")
@idempotent
def business_expense(cur, activity_id):
    payload = body()
    act = _load_activity(cur, activity_id)
    group = load_group(cur, act["group_id"])
    policy = load_policy(cur, group["id"])
    amount = parse_money(payload, "amount", required=True, positive=True)
    res_id = parse_int(payload, "resolutionId")
    limit = money(policy["expense_approval_limit"])
    needs = limit > 0 and amount > limit
    if needs and not res_id:
        raise ApiError(422, f"Expenses above {limit} need an EXPENSE_APPROVAL resolution")
    txn = post_transaction(cur, group, "BUSINESS_EXPENSE", amount, parse_date(payload, "txnDate", default=date.today()),
                           choice(payload, "paymentMethod", PAYMENT_METHODS, default="Cash"), payload.get("bankAccountId"),
                           activity_id=activity_id, resolution_id=res_id,
                           narration=text(payload, "narration", 300, required=True, label="Description"),
                           reference_number=text(payload, "referenceNumber", 80), receipt_prefix="BPV")
    if res_id:
        use_resolution(cur, group["id"], res_id, "EXPENSE_APPROVAL", "TRANSACTION", txn["id"], amount=amount if needs else None)
    return {"transaction": fetch_txn(cur, txn["id"])}, 201


@bachatgat_bp.post("/business/activities/<int:activity_id>/work")
def add_work(activity_id):
    payload = body()
    with tx() as cur:
        act = _load_activity(cur, activity_id)
        member = load_member(cur, parse_int(payload, "memberId", required=True, label="Member"))
        if member["group_id"] != act["group_id"] or member["status"] != "Active":
            raise ApiError(422, "Choose an active member of this group")
        cur.execute(
            """INSERT INTO shg_business_work (activity_id, member_id, work_date, description, quantity, earnings)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (activity_id, member["id"], parse_date(payload, "workDate", default=date.today()),
             text(payload, "description", 200, required=True, label="Work"),
             money(payload.get("quantity")) if payload.get("quantity") not in (None, "") else None,
             parse_money(payload, "earnings")),
        )
        return ok({"work": camel(cur.fetchone())}, 201)


@bachatgat_bp.post("/business/work/pay")
@idempotent
def pay_work(cur):
    """Pays members' unpaid earnings (one payment voucher per member)."""
    payload = body()
    ids = [int(x) for x in (payload.get("workIds") or [])]
    if not ids:
        raise ApiError(422, "Choose work entries to pay")
    cur.execute(
        """SELECT w.*, a.group_id FROM shg_business_work w JOIN shg_business_activities a ON a.id = w.activity_id
           JOIN shg_groups gr ON gr.id = a.group_id WHERE w.id = ANY(%s) AND gr.owner_id = %s FOR UPDATE OF w""",
        (ids, g.owner_id),
    )
    rows = cur.fetchall()
    if len(rows) != len(ids) or any(r["paid_txn_id"] for r in rows):
        raise ApiError(409, "Some entries are missing or already paid")
    if len({(r["activity_id"]) for r in rows}) != 1:
        raise ApiError(422, "Pay one activity at a time")
    group = load_group(cur, rows[0]["group_id"])
    by_member = {}
    for r in rows:
        by_member.setdefault(r["member_id"], []).append(r)
    txns = []
    for member_id, items in by_member.items():
        total = sum((money(i["earnings"]) for i in items), ZERO)
        if total <= 0:
            continue
        txn = post_transaction(cur, group, "BUSINESS_EXPENSE", total, parse_date(payload, "txnDate", default=date.today()),
                               choice(payload, "paymentMethod", PAYMENT_METHODS, default="Cash"), payload.get("bankAccountId"),
                               member_id=member_id, activity_id=rows[0]["activity_id"], narration="Member earnings",
                               receipt_prefix="BPV")
        cur.execute("UPDATE shg_business_work SET paid_txn_id = %s WHERE id = ANY(%s)", (txn["id"], [i["id"] for i in items]))
        txns.append(fetch_txn(cur, txn["id"]))
    return {"transactions": txns}, 201


@bachatgat_bp.post("/business/activities/<int:activity_id>/assets")
def add_asset(activity_id):
    payload = body()
    with tx() as cur:
        act = _load_activity(cur, activity_id)
        cur.execute(
            """INSERT INTO shg_business_assets (activity_id, name, purchase_date, cost, condition, location, notes)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (activity_id, text(payload, "name", 150, required=True, label="Asset"), parse_date(payload, "purchaseDate"),
             parse_money(payload, "cost"), choice(payload, "condition", ("Good", "Needs Repair", "Disposed"), default="Good"),
             text(payload, "location", 150), text(payload, "notes", 500)),
        )
        asset = cur.fetchone()
        record_event(cur, act["group_id"], "BUSINESS_ASSET", asset["id"], "ADDED")
    return ok({"asset": camel(asset)}, 201)


@bachatgat_bp.put("/business/assets/<int:asset_id>")
def update_asset(asset_id):
    payload = body()
    with tx() as cur:
        cur.execute(
            """SELECT s.*, a.group_id FROM shg_business_assets s JOIN shg_business_activities a ON a.id = s.activity_id
               JOIN shg_groups gr ON gr.id = a.group_id WHERE s.id = %s AND gr.owner_id = %s""",
            (asset_id, g.owner_id),
        )
        asset = cur.fetchone()
        if not asset:
            raise ApiError(404, "Asset not found")
        cur.execute(
            "UPDATE shg_business_assets SET condition = %s, location = %s, notes = %s WHERE id = %s RETURNING *",
            (choice(payload, "condition", ("Good", "Needs Repair", "Disposed"), default=asset["condition"]),
             text(payload, "location", 150) or asset["location"], text(payload, "notes", 500) or asset["notes"], asset_id),
        )
        out = cur.fetchone()
        record_event(cur, asset["group_id"], "BUSINESS_ASSET", asset_id, "UPDATED", asset["condition"], out["condition"])
    return ok({"asset": camel(out)})


# ---------------------------------------------------------------------------
# Bank linkage & government schemes
# ---------------------------------------------------------------------------
SCHEME_STATUSES = ("Draft", "Submitted", "Under Verification", "Approved", "Rejected", "Disbursed", "Closed")
SCHEME_FLOW = {
    "Draft": ("Submitted",), "Submitted": ("Under Verification", "Rejected"),
    "Under Verification": ("Approved", "Rejected"), "Approved": ("Closed",), "Disbursed": ("Closed",),
    "Rejected": (), "Closed": (),
}


def _load_scheme(cur, scheme_id, group_id=None, lock=False):
    cur.execute(
        """SELECT s.* FROM shg_scheme_applications s JOIN shg_groups gr ON gr.id = s.group_id
           WHERE s.id = %s AND gr.owner_id = %s""" + (" FOR UPDATE OF s" if lock else ""),
        (scheme_id, g.owner_id),
    )
    s = cur.fetchone()
    if not s or (group_id and s["group_id"] != group_id):
        raise ApiError(404, "Application not found")
    return s


SCHEME_SELECT = """
    SELECT s.*, gr.name_en AS group_name,
           (SELECT COALESCE(SUM(t.amount), 0) FROM shg_transactions t WHERE t.scheme_id = s.id AND t.posting_status = 'Posted'
              AND t.txn_type IN ('GRANT_RECEIVED', 'BANK_LOAN_RECEIVED')) AS amount_received,
           (SELECT COALESCE(SUM(u.amount), 0) FROM shg_scheme_utilizations u WHERE u.scheme_id = s.id) AS amount_utilized
    FROM shg_scheme_applications s JOIN shg_groups gr ON gr.id = s.group_id
"""


@bachatgat_bp.get("/schemes")
def list_schemes():
    where, params = ["gr.owner_id = %s"], [g.owner_id]
    for arg, col in (("groupId", "s.group_id"), ("kind", "s.application_kind"), ("status", "s.status")):
        if request.args.get(arg):
            where.append(f"{col} = %s")
            params.append(request.args[arg])
    with tx() as cur:
        cur.execute(f"{SCHEME_SELECT} WHERE {' AND '.join(where)} ORDER BY s.updated_at DESC LIMIT 200", params)
        return ok({"schemes": [camel(r) for r in cur.fetchall()], "statuses": SCHEME_STATUSES})


def _scheme_fields(payload):
    return {
        "scheme_name": text(payload, "schemeName", 200, required=True, label="Scheme / bank name"),
        "authority": text(payload, "authority", 200),
        "application_number": text(payload, "applicationNumber", 80),
        "reference_number": text(payload, "referenceNumber", 80),
        "applied_on": parse_date(payload, "appliedOn"),
        "amount_requested": parse_money(payload, "amountRequested") or None,
        "required_documents": text(payload, "requiredDocuments", 2000),
        "reporting_requirements": text(payload, "reportingRequirements", 2000),
        "remarks": text(payload, "remarks", 1000),
    }


@bachatgat_bp.post("/schemes")
def create_scheme():
    payload = body()
    with tx() as cur:
        group = load_group(cur, parse_int(payload, "groupId", required=True))
        kind = choice(payload, "applicationKind", ("BANK_LINKAGE", "BANK_LOAN", "GOVT_SCHEME"), label="Type")
        bank_id = parse_int(payload, "bankAccountId")
        if bank_id:
            load_bank_account(cur, group["id"], bank_id)
        fields = _scheme_fields(payload)
        cols = ["group_id", "application_kind", "bank_account_id"] + list(fields)
        cur.execute(f"INSERT INTO shg_scheme_applications ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                    [group["id"], kind, bank_id] + list(fields.values()))
        sid = cur.fetchone()["id"]
        record_event(cur, group["id"], "SCHEME", sid, "CREATED", None, "Draft")
        cur.execute(SCHEME_SELECT + " WHERE s.id = %s", (sid,))
        return ok({"scheme": camel(cur.fetchone())}, 201)


@bachatgat_bp.put("/schemes/<int:scheme_id>")
def update_scheme(scheme_id):
    payload = body()
    with tx() as cur:
        s = _load_scheme(cur, scheme_id, lock=True)
        if s["status"] in ("Closed", "Rejected"):
            raise ApiError(409, f"Application is {s['status']}")
        fields = _scheme_fields({**{"schemeName": s["scheme_name"]}, **payload})
        fields = {k: v for k, v in fields.items() if k == "scheme_name" or any(
            pk for pk in payload if pk.lower() == k.replace("_", ""))}
        cur.execute(f"UPDATE shg_scheme_applications SET {', '.join(f'{k} = %s' for k in fields)}, updated_at = NOW() WHERE id = %s",
                    list(fields.values()) + [scheme_id])
        record_event(cur, s["group_id"], "SCHEME", scheme_id, "UPDATED")
        cur.execute(SCHEME_SELECT + " WHERE s.id = %s", (scheme_id,))
        return ok({"scheme": camel(cur.fetchone())})


@bachatgat_bp.post("/schemes/<int:scheme_id>/status")
def scheme_status(scheme_id):
    payload = body()
    new_status = choice(payload, "status", SCHEME_STATUSES, label="Status")
    remarks = text(payload, "remarks", 1000)
    with tx() as cur:
        s = _load_scheme(cur, scheme_id, lock=True)
        if new_status not in SCHEME_FLOW[s["status"]]:
            raise ApiError(409, f"Cannot move from {s['status']} to {new_status}")
        sets = {"status": new_status}
        if new_status == "Approved":
            sanctioned = parse_money(payload, "amountSanctioned", label="Sanctioned amount")
            sets["amount_sanctioned"] = sanctioned if sanctioned > 0 else None
            sets["verification_status"] = "Verified"
        if new_status == "Rejected":
            if not remarks:
                raise ApiError(422, "Give the reason for rejection")
            sets["verification_status"] = "Rejected"
        if remarks:
            sets["remarks"] = remarks
        if payload.get("referenceNumber"):
            sets["reference_number"] = text(payload, "referenceNumber", 80)
        cur.execute(f"UPDATE shg_scheme_applications SET {', '.join(f'{k} = %s' for k in sets)}, updated_at = NOW() WHERE id = %s",
                    list(sets.values()) + [scheme_id])
        record_event(cur, s["group_id"], "SCHEME", scheme_id, "STATUS_CHANGED", s["status"], new_status, {"remarks": remarks})
        notify(cur, s["group_id"], "SCHEME_STATUS", "Application status changed",
               f"{s['scheme_name']} is now {new_status}.", f"scheme-{scheme_id}-{new_status}", "SCHEME", scheme_id)
        cur.execute(SCHEME_SELECT + " WHERE s.id = %s", (scheme_id,))
        return ok({"scheme": camel(cur.fetchone())})


@bachatgat_bp.get("/schemes/<int:scheme_id>")
def get_scheme(scheme_id):
    with tx() as cur:
        _load_scheme(cur, scheme_id)
        cur.execute(SCHEME_SELECT + " WHERE s.id = %s", (scheme_id,))
        scheme = camel(cur.fetchone())
        cur.execute("SELECT * FROM shg_scheme_utilizations WHERE scheme_id = %s ORDER BY utilization_date DESC", (scheme_id,))
        utilizations = [camel(r) for r in cur.fetchall()]
        cur.execute(TXN_SELECT + " WHERE t.scheme_id = %s AND gr.owner_id = %s ORDER BY t.txn_date, t.id", (scheme_id, g.owner_id))
        txns = [serialize_txn(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT action, from_status, to_status, details, created_at FROM shg_events WHERE entity_type = 'SCHEME' AND entity_id = %s AND owner_id = %s ORDER BY id",
            (scheme_id, g.owner_id),
        )
        history = [camel(r) for r in cur.fetchall()]
    return ok({"scheme": scheme, "utilizations": utilizations, "transactions": txns, "history": history})


@bachatgat_bp.post("/schemes/<int:scheme_id>/disbursements")
@idempotent
def scheme_disbursement(cur, scheme_id):
    payload = body()
    s = _load_scheme(cur, scheme_id, lock=True)
    if s["status"] not in ("Approved", "Disbursed"):
        raise ApiError(409, "Funds can be recorded only after approval")
    group = load_group(cur, s["group_id"])
    amount = parse_money(payload, "amount", required=True, positive=True)
    cur.execute(
        "SELECT COALESCE(SUM(amount), 0) AS got FROM shg_transactions WHERE scheme_id = %s AND posting_status = 'Posted' AND txn_type IN ('GRANT_RECEIVED', 'BANK_LOAN_RECEIVED')",
        (scheme_id,),
    )
    received = money(cur.fetchone()["got"])
    if s["amount_sanctioned"] and received + amount > money(s["amount_sanctioned"]):
        raise ApiError(422, f"Total received would exceed the sanctioned {s['amount_sanctioned']}")
    txn_type = "GRANT_RECEIVED" if s["application_kind"] == "GOVT_SCHEME" else "BANK_LOAN_RECEIVED"
    txn = post_transaction(cur, group, txn_type, amount, parse_date(payload, "txnDate", default=date.today()),
                           choice(payload, "paymentMethod", PAYMENT_METHODS, default="Bank Transfer"),
                           payload.get("bankAccountId") or s["bank_account_id"], scheme_id=scheme_id,
                           narration=f"{s['scheme_name']} funds received", reference_number=text(payload, "referenceNumber", 80),
                           receipt_prefix="RCT")
    if s["status"] != "Disbursed":
        cur.execute("UPDATE shg_scheme_applications SET status = 'Disbursed', updated_at = NOW() WHERE id = %s", (scheme_id,))
        record_event(cur, group["id"], "SCHEME", scheme_id, "DISBURSED", s["status"], "Disbursed", {"amount": amount})
    return {"transaction": fetch_txn(cur, txn["id"])}, 201


@bachatgat_bp.post("/schemes/<int:scheme_id>/utilizations")
def scheme_utilization(scheme_id):
    payload = body()
    with tx() as cur:
        s = _load_scheme(cur, scheme_id, lock=True)
        amount = parse_money(payload, "amount", required=True, positive=True)
        cur.execute(SCHEME_SELECT + " WHERE s.id = %s", (scheme_id,))
        row = cur.fetchone()
        if money(row["amount_utilized"]) + amount > money(row["amount_received"]):
            raise ApiError(422, "Utilization cannot exceed the funds received")
        txn_id = parse_int(payload, "txnId")
        if txn_id:
            cur.execute("SELECT 1 FROM shg_transactions WHERE id = %s AND group_id = %s AND txn_type IN ('OPERATING_EXPENSE', 'BUSINESS_EXPENSE', 'LOAN_DISBURSEMENT')",
                        (txn_id, s["group_id"]))
            if not cur.fetchone():
                raise ApiError(422, "Linked transaction must be an expense or loan disbursement of this group")
        cur.execute(
            "INSERT INTO shg_scheme_utilizations (scheme_id, utilization_date, amount, purpose, txn_id) VALUES (%s,%s,%s,%s,%s) RETURNING *",
            (scheme_id, parse_date(payload, "utilizationDate", default=date.today()), amount,
             text(payload, "purpose", 200, required=True, label="Purpose"), txn_id),
        )
        u = cur.fetchone()
        record_event(cur, s["group_id"], "SCHEME", scheme_id, "UTILIZATION_RECORDED", details={"amount": amount})
    return ok({"utilization": camel(u)}, 201)


# ---------------------------------------------------------------------------
# Notifications & reminders
# ---------------------------------------------------------------------------
def refresh_reminders(cur):
    """Derives reminders from the books (idempotent via dedupe keys). Texts
    carry no amounts or account details."""
    today = date.today()
    soon = today + timedelta(days=3)
    month_key = today.strftime("%Y-%m")
    cur.execute(
        """SELECT d.group_id, gr.name_en, COUNT(*) FILTER (WHERE d.due_date < %s) AS missed,
                  COUNT(*) FILTER (WHERE d.due_date BETWEEN %s AND %s) AS upcoming
           FROM shg_savings_dues d JOIN shg_groups gr ON gr.id = d.group_id
           WHERE gr.owner_id = %s AND gr.status = 'Active' AND d.status IN ('Pending', 'Partial')
           GROUP BY d.group_id, gr.name_en""",
        (today, today, soon, g.owner_id),
    )
    for r in cur.fetchall():
        if r["missed"]:
            notify(cur, r["group_id"], "MISSED_SAVINGS", "Missed savings",
                   f"{r['missed']} savings contributions in {r['name_en']} are past due.",
                   f"missed-savings-{r['group_id']}-{today.isoformat()}", "GROUP", r["group_id"])
        if r["upcoming"]:
            notify(cur, r["group_id"], "SAVINGS_DUE", "Savings due soon",
                   f"{r['upcoming']} savings contributions in {r['name_en']} fall due in the next 3 days.",
                   f"savings-due-{r['group_id']}-{month_key}-{soon.isoformat()}", "GROUP", r["group_id"])
    cur.execute(
        """SELECT l.group_id, gr.name_en, COUNT(*) FILTER (WHERE i.due_date < %s) AS overdue,
                  COUNT(*) FILTER (WHERE i.due_date BETWEEN %s AND %s) AS upcoming
           FROM shg_loan_installments i JOIN shg_loans l ON l.id = i.loan_id JOIN shg_groups gr ON gr.id = l.group_id
           WHERE gr.owner_id = %s AND l.status IN ('Active', 'Overdue') AND i.status IN ('Pending', 'Partial')
             AND i.schedule_version = l.schedule_version
           GROUP BY l.group_id, gr.name_en""",
        (today, today, soon, g.owner_id),
    )
    for r in cur.fetchall():
        if r["overdue"]:
            notify(cur, r["group_id"], "OVERDUE_REPAYMENT", "Overdue installments",
                   f"{r['overdue']} loan installments in {r['name_en']} are overdue.",
                   f"overdue-inst-{r['group_id']}-{today.isoformat()}", "GROUP", r["group_id"])
        if r["upcoming"]:
            notify(cur, r["group_id"], "INSTALLMENT_DUE", "Installments due soon",
                   f"{r['upcoming']} loan installments in {r['name_en']} fall due in the next 3 days.",
                   f"inst-due-{r['group_id']}-{soon.isoformat()}", "GROUP", r["group_id"])
    cur.execute(
        """SELECT mt.id, mt.group_id, mt.meeting_number, mt.meeting_date, gr.name_en FROM shg_meetings mt
           JOIN shg_groups gr ON gr.id = mt.group_id
           WHERE gr.owner_id = %s AND mt.status = 'Scheduled' AND mt.meeting_date BETWEEN %s AND %s""",
        (g.owner_id, today, today + timedelta(days=1)),
    )
    for r in cur.fetchall():
        when = "today" if r["meeting_date"] == today else "tomorrow"
        notify(cur, r["group_id"], "MEETING_REMINDER", "Meeting reminder",
               f"Meeting #{r['meeting_number']} of {r['name_en']} is {when}.",
               f"meeting-reminder-{r['id']}-{when}", "MEETING", r["id"])
    cur.execute(
        """SELECT l.id, l.group_id, l.application_number FROM shg_loans l JOIN shg_groups gr ON gr.id = l.group_id
           WHERE gr.owner_id = %s AND l.status IN ('Submitted', 'Under Review')""",
        (g.owner_id,),
    )
    for r in cur.fetchall():
        notify(cur, r["group_id"], "APPROVAL_PENDING", "Loan awaiting decision",
               f"Loan application {r['application_number']} needs review.", f"loan-pending-{r['id']}", "LOAN", r["id"])


@bachatgat_bp.get("/notifications")
def list_notifications():
    size, offset, page = page_args(30, 100)
    with tx() as cur:
        refresh_reminders(cur)
        params = [g.owner_id]
        sql = "SELECT n.*, gr.name_en AS group_name FROM shg_notifications n LEFT JOIN shg_groups gr ON gr.id = n.group_id WHERE n.owner_id = %s"
        if request.args.get("unread") == "1":
            sql += " AND NOT n.is_read"
        cur.execute(sql + " ORDER BY n.created_at DESC, n.id DESC LIMIT %s OFFSET %s", params + [size, offset])
        rows = [camel(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS n FROM shg_notifications WHERE owner_id = %s AND NOT is_read", (g.owner_id,))
        unread = cur.fetchone()["n"]
    return ok({"notifications": rows, "unread": unread, "page": page})


@bachatgat_bp.post("/notifications/<int:notif_id>/read")
def read_notification(notif_id):
    with tx() as cur:
        cur.execute("UPDATE shg_notifications SET is_read = TRUE WHERE id = %s AND owner_id = %s", (notif_id, g.owner_id))
    return ok({"ok": True})


@bachatgat_bp.post("/notifications/read-all")
def read_all_notifications():
    with tx() as cur:
        cur.execute("UPDATE shg_notifications SET is_read = TRUE WHERE owner_id = %s AND NOT is_read", (g.owner_id,))
    return ok({"ok": True})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@bachatgat_bp.get("/dashboard")
def dashboard():
    start = parse_date(request.args, "from", default=date.today().replace(day=1))
    end = parse_date(request.args, "to", default=date.today())
    group_id = request.args.get("groupId", type=int)
    with tx() as cur:
        refresh_reminders(cur)
        scope, params = "gr.owner_id = %s", [g.owner_id]
        if group_id:
            load_group(cur, group_id)
            scope += " AND gr.id = %s"
            params.append(group_id)
        cur.execute(f"SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE status = 'Active') AS active, "
                    f"COUNT(*) FILTER (WHERE status = 'Pending Approval') AS pending FROM shg_groups gr WHERE {scope}", params)
        groups = cur.fetchone()
        cur.execute(f"""SELECT COUNT(*) FILTER (WHERE m.status = 'Active') AS active,
                               COUNT(*) FILTER (WHERE m.status = 'Pending') AS pending
                        FROM shg_members m JOIN shg_groups gr ON gr.id = m.group_id WHERE {scope}""", params)
        members = cur.fetchone()
        cur.execute(
            f"""SELECT
                  COALESCE(SUM(CASE WHEN l.account_code = 'MEMBER_SAVINGS' THEN l.credit - l.debit END), 0) AS savings_liability,
                  COALESCE(SUM(CASE WHEN l.account_code = 'LOAN_RECEIVABLE' THEN l.debit - l.credit END), 0) AS loan_outstanding,
                  COALESCE(SUM(CASE WHEN l.account_code = 'CASH' THEN l.debit - l.credit END), 0) AS cash,
                  COALESCE(SUM(CASE WHEN l.account_code = 'BANK' THEN l.debit - l.credit END), 0) AS bank
                FROM shg_ledger_lines l JOIN shg_groups gr ON gr.id = l.group_id WHERE {scope}""",
            params,
        )
        balances = cur.fetchone()
        cur.execute(
            f"""SELECT
                  COALESCE(SUM(CASE WHEN t.txn_type = 'SAVINGS_DEPOSIT' THEN t.amount END), 0) AS savings_collected,
                  COALESCE(SUM(CASE WHEN t.txn_type = 'LOAN_REPAYMENT' THEN t.amount END), 0) AS repayments,
                  COALESCE(SUM(CASE WHEN t.txn_type = 'LOAN_DISBURSEMENT' THEN t.amount END), 0) AS disbursed,
                  COALESCE(SUM(CASE WHEN t.txn_type = 'LOAN_REPAYMENT' THEN t.interest_amount END), 0) AS interest_received
                FROM shg_transactions t JOIN shg_groups gr ON gr.id = t.group_id
                WHERE {scope} AND t.posting_status = 'Posted' AND t.txn_date BETWEEN %s AND %s""",
            params + [start, end],
        )
        flows = cur.fetchone()
        cur.execute(
            f"""SELECT COALESCE(SUM(CASE WHEN l.account_code = 'BUSINESS_INCOME' THEN l.credit - l.debit END), 0) AS income,
                       COALESCE(SUM(CASE WHEN l.account_code = 'BUSINESS_EXPENSE' THEN l.debit - l.credit END), 0) AS expense,
                       COALESCE(SUM(CASE WHEN l.account_code IN ('OPERATING_EXPENSE', 'BANK_CHARGES') THEN l.debit - l.credit END), 0) AS op_expense
                FROM shg_ledger_lines l JOIN shg_groups gr ON gr.id = l.group_id
                WHERE {scope} AND l.txn_date BETWEEN %s AND %s""",
            params + [start, end],
        )
        biz = cur.fetchone()
        cur.execute(
            f"""SELECT COUNT(*) AS n,
                       COALESCE(SUM(i.principal_due - i.principal_paid - i.principal_written_off
                                    + i.interest_due - i.interest_paid - i.interest_waived
                                    + i.penalty_due - i.penalty_paid - i.penalty_waived), 0) AS amount
                FROM shg_loan_installments i JOIN shg_loans ln ON ln.id = i.loan_id JOIN shg_groups gr ON gr.id = ln.group_id
                WHERE {scope} AND ln.status IN ('Active', 'Overdue') AND i.status IN ('Pending', 'Partial')
                  AND i.schedule_version = ln.schedule_version AND i.due_date < CURRENT_DATE""",
            params,
        )
        overdue = cur.fetchone()
        cur.execute(
            f"""SELECT COUNT(*) FILTER (WHERE ln.status IN ('Submitted', 'Under Review')) AS loans_pending,
                       COUNT(*) FILTER (WHERE ln.status = 'Approved') AS loans_to_disburse
                FROM shg_loans ln JOIN shg_groups gr ON gr.id = ln.group_id WHERE {scope}""",
            params,
        )
        loans = cur.fetchone()
        cur.execute(f"SELECT COUNT(*) AS n FROM shg_documents d JOIN shg_groups gr ON gr.id = d.group_id WHERE {scope} AND d.verification_status = 'Pending'", params)
        docs_pending = cur.fetchone()["n"]
        cur.execute(
            f"""SELECT mt.id, mt.meeting_number, mt.meeting_date, mt.meeting_time, mt.location, gr.name_en AS group_name, gr.id AS group_id
                FROM shg_meetings mt JOIN shg_groups gr ON gr.id = mt.group_id
                WHERE {scope} AND mt.status = 'Scheduled' AND mt.meeting_date >= CURRENT_DATE ORDER BY mt.meeting_date LIMIT 5""",
            params,
        )
        meetings = [camel(r) for r in cur.fetchall()]
        cur.execute(f"{TXN_SELECT} WHERE {scope} ORDER BY t.created_at DESC, t.id DESC LIMIT 8", params)
        recent = [serialize_txn(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS n FROM shg_notifications WHERE owner_id = %s AND NOT is_read", (g.owner_id,))
        unread = cur.fetchone()["n"]
    return ok({
        "from": start, "to": end, "features": g.shg_features,
        "groups": {"total": groups["total"], "active": groups["active"], "pendingApproval": groups["pending"]},
        "members": {"active": members["active"], "pending": members["pending"]},
        "savingsCollected": money(flows["savings_collected"]),
        "savingsLiability": money(balances["savings_liability"]),
        "loanOutstanding": money(balances["loan_outstanding"]),
        "repaymentsCollected": money(flows["repayments"]),
        "loansDisbursed": money(flows["disbursed"]),
        "interestReceived": money(flows["interest_received"]),
        "overdue": {"installments": overdue["n"], "amount": money(overdue["amount"])},
        "cashBalance": money(balances["cash"]),
        "bankBalance": money(balances["bank"]),
        "business": {"income": money(biz["income"]), "expense": money(biz["expense"])},
        "operatingExpenses": money(biz["op_expense"]),
        "pendingApprovals": {
            "groups": groups["pending"], "members": members["pending"], "loans": loans["loans_pending"],
            "disbursements": loans["loans_to_disburse"], "documents": docs_pending,
            "total": groups["pending"] + members["pending"] + loans["loans_pending"] + loans["loans_to_disburse"] + docs_pending,
        },
        "upcomingMeetings": meetings,
        "recentTransactions": recent,
        "unreadNotifications": unread,
    })


# ---------------------------------------------------------------------------
# Reports (+ Excel export). PDF is rendered on the device from the same JSON.
# ---------------------------------------------------------------------------
REPORTS = {}


def report(key, title, category, needs_group=True, needs_member=False):
    def deco(fn):
        REPORTS[key] = {"fn": fn, "title": title, "category": category, "needsGroup": needs_group, "needsMember": needs_member}
        return fn
    return deco


def C(key, label, kind="text"):
    return {"key": key, "label": label, "type": kind}


def _rows(cur, sql, params):
    cur.execute(sql, params)
    return [camel(r) for r in cur.fetchall()]


@report("groups", "Group registration report", "Member reports", needs_group=False)
def r_groups(cur, ctx):
    rows = _rows(cur, GROUP_LIST_REPORT_SQL, [g.owner_id])
    return [C("groupCode", "Code"), C("nameEn", "Group"), C("nameMr", "नाव"), C("village", "Village"), C("taluka", "Taluka"),
            C("formationDate", "Formed", "date"), C("registrationNumber", "Reg. No."), C("status", "Status"),
            C("activeMembers", "Members", "number"), C("savingsBalance", "Savings", "money")], rows, ["savingsBalance", "activeMembers"]


GROUP_LIST_REPORT_SQL = """
    SELECT gr.group_code, gr.name_en, gr.name_mr, gr.village, gr.taluka, gr.formation_date, gr.registration_number, gr.status,
           (SELECT COUNT(*) FROM shg_members m WHERE m.group_id = gr.id AND m.status = 'Active') AS active_members,
           (SELECT COALESCE(SUM(credit - debit), 0) FROM shg_ledger_lines x WHERE x.group_id = gr.id AND x.account_code = 'MEMBER_SAVINGS') AS savings_balance
    FROM shg_groups gr WHERE gr.owner_id = %s ORDER BY gr.name_en
"""


@report("members", "Member directory", "Member reports", needs_group=False)
def r_members(cur, ctx):
    where, params = ["m.owner_id = %s"], [g.owner_id]
    if ctx["group"]:
        where.append("m.group_id = %s")
        params.append(ctx["group"]["id"])
    if ctx["status"]:
        where.append("m.status = %s")
        params.append(ctx["status"])
    rows = _rows(cur, f"""SELECT gr.group_code, m.member_number, m.full_name, m.mobile, m.role, m.status, m.joining_date,
                                 m.occupation, m.id_verification_status,
                                 (SELECT COALESCE(SUM(credit - debit), 0) FROM shg_ledger_lines l WHERE l.member_id = m.id AND l.account_code = 'MEMBER_SAVINGS') AS savings_balance
                          FROM shg_members m JOIN shg_groups gr ON gr.id = m.group_id WHERE {' AND '.join(where)}
                          ORDER BY gr.group_code, m.member_number""", params)
    return [C("groupCode", "Group"), C("memberNumber", "No.", "number"), C("fullName", "Name"), C("mobile", "Mobile"),
            C("role", "Role"), C("status", "Status"), C("joiningDate", "Joined", "date"), C("idVerificationStatus", "KYC"),
            C("savingsBalance", "Savings", "money")], rows, ["savingsBalance"]


@report("member_savings", "Member savings statement", "Member reports", needs_member=True)
def r_member_savings(cur, ctx):
    rows = _rows(cur, """SELECT l.txn_date, t.receipt_number, t.txn_type, t.payment_method, t.posting_status, l.credit AS deposit, l.debit AS withdrawal,
                                SUM(l.credit - l.debit) OVER (ORDER BY l.txn_date, l.id) AS balance
                         FROM shg_ledger_lines l JOIN shg_transactions t ON t.id = l.txn_id
                         WHERE l.member_id = %s AND l.account_code = 'MEMBER_SAVINGS' AND l.txn_date <= %s ORDER BY l.txn_date, l.id""",
                 [ctx["member"]["id"], ctx["to"]])
    rows = [r for r in rows if r["txnDate"] >= ctx["from"].isoformat()] if rows else rows
    return [C("txnDate", "Date", "date"), C("receiptNumber", "Receipt"), C("txnType", "Type"), C("paymentMethod", "Mode"),
            C("deposit", "Deposit", "money"), C("withdrawal", "Withdrawal", "money"), C("balance", "Balance", "money"),
            C("postingStatus", "Status")], rows, ["deposit", "withdrawal"]


@report("monthly_savings", "Monthly savings collection", "Financial reports")
def r_monthly_savings(cur, ctx):
    rows = _rows(cur, """SELECT TO_CHAR(t.txn_date, 'YYYY-MM') AS month, m.member_number, m.full_name, COUNT(*) AS receipts,
                                SUM(t.amount) AS amount
                         FROM shg_transactions t JOIN shg_members m ON m.id = t.member_id
                         WHERE t.group_id = %s AND t.txn_type = 'SAVINGS_DEPOSIT' AND t.posting_status = 'Posted'
                           AND t.txn_date BETWEEN %s AND %s
                         GROUP BY 1, m.member_number, m.full_name ORDER BY 1, m.member_number""",
                 [ctx["group"]["id"], ctx["from"], ctx["to"]])
    return [C("month", "Month"), C("memberNumber", "No.", "number"), C("fullName", "Member"), C("receipts", "Receipts", "number"),
            C("amount", "Collected", "money")], rows, ["amount"]


@report("pending_contributions", "Pending contributions", "Member reports")
def r_pending(cur, ctx):
    rows = _rows(cur, """SELECT m.member_number, m.full_name, m.mobile, d.period_start, d.due_date, d.due_amount, d.paid_amount,
                                (d.due_amount - d.paid_amount) AS balance, (CURRENT_DATE - d.due_date) AS days_late
                         FROM shg_savings_dues d JOIN shg_members m ON m.id = d.member_id
                         WHERE d.group_id = %s AND d.status IN ('Pending', 'Partial') AND d.due_date <= %s
                         ORDER BY m.member_number, d.period_start""", [ctx["group"]["id"], ctx["to"]])
    return [C("memberNumber", "No.", "number"), C("fullName", "Member"), C("mobile", "Mobile"), C("periodStart", "Period", "date"),
            C("dueDate", "Due", "date"), C("dueAmount", "Due amt", "money"), C("paidAmount", "Paid", "money"),
            C("balance", "Pending", "money"), C("daysLate", "Days late", "number")], rows, ["balance"]


LOAN_REPORT_COLS = [C("applicationNumber", "Application"), C("applicationDate", "Applied", "date"), C("memberName", "Member"),
                    C("requestedAmount", "Requested", "money"), C("approvedAmount", "Approved", "money"),
                    C("interestMethod", "Method"), C("interestRate", "Rate %", "number"), C("installments", "Inst.", "number"),
                    C("purpose", "Purpose"), C("status", "Status")]


@report("loan_applications", "Loan applications", "Loan reports")
def r_loan_apps(cur, ctx):
    params = [ctx["group"]["id"], ctx["from"], ctx["to"]]
    extra = ""
    if ctx["status"]:
        extra = " AND l.status = %s"
        params.append(ctx["status"])
    rows = _rows(cur, f"""SELECT l.*, m.full_name AS member_name FROM shg_loans l JOIN shg_members m ON m.id = l.member_id
                          WHERE l.group_id = %s AND l.application_date BETWEEN %s AND %s{extra} ORDER BY l.application_date""", params)
    return LOAN_REPORT_COLS, rows, ["requestedAmount", "approvedAmount"]


@report("loan_decisions", "Loan approvals and rejections", "Loan reports")
def r_loan_decisions(cur, ctx):
    rows = _rows(cur, """SELECT l.application_number, m.full_name AS member_name, e.to_status AS decision, e.created_at::date AS decided_on,
                                l.requested_amount, l.approved_amount, mt.meeting_number, r.resolution_number, r.votes_for, r.votes_against,
                                COALESCE(l.rejection_reason, l.review_remarks) AS remarks
                         FROM shg_events e JOIN shg_loans l ON l.id = e.entity_id AND e.entity_type = 'LOAN'
                         JOIN shg_members m ON m.id = l.member_id
                         LEFT JOIN shg_resolutions r ON r.id = l.resolution_id LEFT JOIN shg_meetings mt ON mt.id = r.meeting_id
                         WHERE l.group_id = %s AND e.action IN ('APPROVE', 'REJECT', 'WITHDRAW') AND e.created_at::date BETWEEN %s AND %s
                         ORDER BY e.created_at""", [ctx["group"]["id"], ctx["from"], ctx["to"]])
    return [C("applicationNumber", "Application"), C("memberName", "Member"), C("decision", "Decision"), C("decidedOn", "Date", "date"),
            C("requestedAmount", "Requested", "money"), C("approvedAmount", "Approved", "money"), C("meetingNumber", "Meeting #", "number"),
            C("resolutionNumber", "Res. #", "number"), C("votesFor", "For", "number"), C("votesAgainst", "Against", "number"),
            C("remarks", "Remarks")], rows, ["approvedAmount"]


def _txn_report(cur, ctx, types):
    return _rows(cur, f"""{TXN_SELECT} WHERE t.group_id = %s AND gr.owner_id = %s AND t.txn_type = ANY(%s)
                          AND t.txn_date BETWEEN %s AND %s ORDER BY t.txn_date, t.id""",
                 [ctx["group"]["id"], g.owner_id, list(types), ctx["from"], ctx["to"]])


@report("disbursements", "Loan disbursement register", "Loan reports")
def r_disbursements(cur, ctx):
    rows = _txn_report(cur, ctx, ("LOAN_DISBURSEMENT",))
    return [C("txnDate", "Date", "date"), C("receiptNumber", "Voucher"), C("applicationNumber", "Loan"), C("memberName", "Member"),
            C("amount", "Amount", "money"), C("paymentMethod", "Mode"), C("postingStatus", "Status")], rows, ["amount"]


@report("repayments", "Repayment history", "Loan reports")
def r_repayments(cur, ctx):
    rows = _txn_report(cur, ctx, ("LOAN_REPAYMENT", "SAVINGS_LOAN_OFFSET"))
    if ctx["member"]:
        rows = [r for r in rows if r["memberId"] == ctx["member"]["id"]]
    return [C("txnDate", "Date", "date"), C("receiptNumber", "Receipt"), C("applicationNumber", "Loan"), C("memberName", "Member"),
            C("principalAmount", "Principal", "money"), C("interestAmount", "Interest", "money"), C("penaltyAmount", "Penalty", "money"),
            C("amount", "Total", "money"), C("paymentMethod", "Mode"), C("postingStatus", "Status")], rows, \
        ["principalAmount", "interestAmount", "penaltyAmount", "amount"]


@report("overdue", "Overdue installment report", "Loan reports")
def r_overdue(cur, ctx):
    rows = _rows(cur, """SELECT l.application_number, m.full_name AS member_name, m.mobile, i.installment_number, i.due_date,
                                (CURRENT_DATE - i.due_date) AS days_overdue,
                                (i.principal_due - i.principal_paid - i.principal_written_off) AS principal,
                                (i.interest_due - i.interest_paid - i.interest_waived) AS interest,
                                (i.penalty_due - i.penalty_paid - i.penalty_waived) AS penalty
                         FROM shg_loan_installments i JOIN shg_loans l ON l.id = i.loan_id JOIN shg_members m ON m.id = l.member_id
                         WHERE l.group_id = %s AND l.status IN ('Active', 'Overdue') AND i.status IN ('Pending', 'Partial')
                           AND i.schedule_version = l.schedule_version AND i.due_date < CURRENT_DATE
                         ORDER BY i.due_date""", [ctx["group"]["id"]])
    for r in rows:
        r["total"] = round((r["principal"] or 0) + (r["interest"] or 0) + (r["penalty"] or 0), 2)
    return [C("applicationNumber", "Loan"), C("memberName", "Member"), C("mobile", "Mobile"), C("installmentNumber", "Inst.", "number"),
            C("dueDate", "Due", "date"), C("daysOverdue", "Days", "number"), C("principal", "Principal", "money"),
            C("interest", "Interest", "money"), C("penalty", "Penalty", "money"), C("total", "Total", "money")], rows, \
        ["principal", "interest", "penalty", "total"]


@report("outstanding", "Outstanding loan report", "Loan reports")
def r_outstanding(cur, ctx):
    rows = _rows(cur, """SELECT l.application_number, m.full_name AS member_name, l.disbursed_on, l.approved_amount, l.status,
                                (SELECT COALESCE(SUM(debit - credit), 0) FROM shg_ledger_lines x WHERE x.loan_id = l.id AND x.account_code = 'LOAN_RECEIVABLE' AND x.txn_date <= %s) AS principal_outstanding,
                                (SELECT MIN(i.due_date) FROM shg_loan_installments i WHERE i.loan_id = l.id AND i.schedule_version = l.schedule_version AND i.status IN ('Pending', 'Partial')) AS next_due
                         FROM shg_loans l JOIN shg_members m ON m.id = l.member_id
                         WHERE l.group_id = %s AND l.status IN ('Active', 'Overdue') ORDER BY m.member_number""",
                 [ctx["to"], ctx["group"]["id"]])
    return [C("applicationNumber", "Loan"), C("memberName", "Member"), C("disbursedOn", "Disbursed", "date"),
            C("approvedAmount", "Amount", "money"), C("principalOutstanding", "Outstanding", "money"), C("nextDue", "Next due", "date"),
            C("status", "Status")], rows, ["approvedAmount", "principalOutstanding"]


@report("attendance", "Meeting attendance", "Member reports")
def r_attendance(cur, ctx):
    rows = _rows(cur, """SELECT mt.meeting_number, mt.meeting_date, mt.status,
                                COUNT(a.member_id) FILTER (WHERE a.present) AS present,
                                COUNT(a.member_id) FILTER (WHERE NOT a.present) AS absent
                         FROM shg_meetings mt LEFT JOIN shg_meeting_attendance a ON a.meeting_id = mt.id
                         WHERE mt.group_id = %s AND mt.meeting_date BETWEEN %s AND %s AND mt.status <> 'Cancelled'
                         GROUP BY mt.id ORDER BY mt.meeting_date""", [ctx["group"]["id"], ctx["from"], ctx["to"]])
    return [C("meetingNumber", "Meeting #", "number"), C("meetingDate", "Date", "date"), C("status", "Status"),
            C("present", "Present", "number"), C("absent", "Absent", "number")], rows, []


@report("resolutions", "Meeting resolutions", "Member reports")
def r_resolutions(cur, ctx):
    rows = _rows(cur, """SELECT mt.meeting_number, mt.meeting_date, r.resolution_number, r.resolution_type, r.subject, r.amount,
                                r.votes_for, r.votes_against, r.votes_abstain, r.result, l.application_number
                         FROM shg_resolutions r JOIN shg_meetings mt ON mt.id = r.meeting_id LEFT JOIN shg_loans l ON l.id = r.loan_id
                         WHERE mt.group_id = %s AND mt.meeting_date BETWEEN %s AND %s ORDER BY mt.meeting_date, r.resolution_number""",
                 [ctx["group"]["id"], ctx["from"], ctx["to"]])
    return [C("meetingNumber", "Meeting #", "number"), C("meetingDate", "Date", "date"), C("resolutionNumber", "Res. #", "number"),
            C("resolutionType", "Type"), C("subject", "Subject"), C("applicationNumber", "Loan"), C("amount", "Amount", "money"),
            C("votesFor", "For", "number"), C("votesAgainst", "Against", "number"), C("result", "Result")], rows, []


BOOK_COLS = [C("txnDate", "Date", "date"), C("txnNumber", "Txn"), C("receiptNumber", "Receipt"), C("txnType", "Type"),
             C("memberName", "Member"), C("narration", "Particulars"), C("receipt", "Receipt", "money"),
             C("payment", "Payment", "money"), C("balance", "Balance", "money")]


def _book_rows(data):
    opening = {"txnDate": None, "narration": "Opening balance", "balance": data["opening"]}
    closing = {"txnDate": None, "narration": "Closing balance", "balance": data["closing"]}
    return [opening] + data["rows"] + [closing]


@report("cashbook", "Cashbook", "Financial reports")
def r_cashbook(cur, ctx):
    return BOOK_COLS, _book_rows(book(cur, ctx["group"]["id"], "CASH", ctx["from"], ctx["to"])), ["receipt", "payment"]


@report("bankbook", "Bankbook", "Financial reports")
def r_bankbook(cur, ctx):
    bank = load_bank_account(cur, ctx["group"]["id"], request.args.get("bankAccountId", type=int))
    return BOOK_COLS, _book_rows(book(cur, ctx["group"]["id"], "BANK", ctx["from"], ctx["to"], bank["id"])), ["receipt", "payment"]


@report("income_expenditure", "Income and expenditure", "Financial reports")
def r_ie(cur, ctx):
    ie = income_expenditure(cur, ctx["group"]["id"], ctx["from"], ctx["to"])
    rows = [{"section": "Income", "account": i["account"], "amount": i["amount"]} for i in ie["income"]]
    rows += [{"section": "Expenditure", "account": e["account"], "amount": e["amount"]} for e in ie["expenditure"]]
    rows += [{"section": "Result", "account": "Group surplus / (deficit)", "amount": ie["groupSurplus"]},
             {"section": "Business", "account": "Business income", "amount": ie["business"]["income"]},
             {"section": "Business", "account": "Business expenditure", "amount": ie["business"]["expenditure"]},
             {"section": "Result", "account": "Consolidated surplus / (deficit)", "amount": ie["consolidatedSurplus"]}]
    return [C("section", "Section"), C("account", "Head"), C("amount", "Amount", "money")], rows, []


@report("financial_position", "Financial position", "Financial reports")
def r_fp(cur, ctx):
    fp = financial_position(cur, ctx["group"]["id"], ctx["to"])
    rows = [{"section": "Assets", **a} for a in fp["assets"]]
    rows += [{"section": "Liabilities", **a} for a in fp["liabilities"]]
    rows += [{"section": "Funds & surplus", **a} for a in fp["funds"]]
    rows += [{"section": "Total", "account": "Total assets", "amount": fp["totalAssets"]},
             {"section": "Total", "account": "Total liabilities & funds", "amount": fp["totalLiabilitiesAndFunds"]}]
    return [C("section", "Section"), C("account", "Account"), C("amount", "Amount", "money")], rows, []


@report("trial_balance", "Trial balance", "Financial reports")
def r_tb(cur, ctx):
    rows = trial_balance(cur, ctx["group"]["id"], ctx["to"])
    return [C("account", "Account"), C("classification", "Class"), C("debit", "Debit", "money"), C("credit", "Credit", "money")], rows, ["debit", "credit"]


@report("bank_reconciliation", "Bank reconciliation", "Financial reports")
def r_recon(cur, ctx):
    rows = _rows(cur, """SELECT b.bank_name, RIGHT(b.account_number, 4) AS last4, r.statement_date, r.statement_balance, r.book_balance,
                                r.uncleared_amount, r.difference, r.notes
                         FROM shg_bank_reconciliations r JOIN shg_bank_accounts b ON b.id = r.bank_account_id
                         WHERE b.group_id = %s AND r.statement_date BETWEEN %s AND %s ORDER BY r.statement_date""",
                 [ctx["group"]["id"], ctx["from"], ctx["to"]])
    return [C("bankName", "Bank"), C("last4", "A/c (last 4)"), C("statementDate", "Statement date", "date"),
            C("statementBalance", "Statement", "money"), C("bookBalance", "Books", "money"), C("unclearedAmount", "Uncleared", "money"),
            C("difference", "Difference", "money"), C("notes", "Notes")], rows, []


@report("business_orders", "Business sales and purchases", "Business reports")
def r_orders(cur, ctx):
    rows = _rows(cur, """SELECT a.name AS activity, o.order_type, o.order_number, o.order_date, p.name AS party, o.total_amount,
                                o.paid_amount, (o.total_amount - o.paid_amount) AS balance, o.status
                         FROM shg_business_orders o JOIN shg_business_activities a ON a.id = o.activity_id
                         LEFT JOIN shg_business_parties p ON p.id = o.party_id
                         WHERE a.group_id = %s AND o.order_date BETWEEN %s AND %s ORDER BY o.order_date""",
                 [ctx["group"]["id"], ctx["from"], ctx["to"]])
    return [C("activity", "Activity"), C("orderType", "Type"), C("orderNumber", "Number"), C("orderDate", "Date", "date"),
            C("party", "Party"), C("totalAmount", "Total", "money"), C("paidAmount", "Paid", "money"), C("balance", "Balance", "money"),
            C("status", "Status")], rows, ["totalAmount", "paidAmount", "balance"]


@report("inventory", "Inventory and stock", "Business reports")
def r_inventory(cur, ctx):
    rows = _rows(cur, """SELECT a.name AS activity, p.name, p.kind, p.unit, p.stock_qty, p.reorder_level, p.purchase_price, p.sale_price,
                                (p.stock_qty * p.purchase_price) AS stock_value
                         FROM shg_business_products p JOIN shg_business_activities a ON a.id = p.activity_id
                         WHERE a.group_id = %s AND p.kind <> 'Service' ORDER BY a.name, p.name""", [ctx["group"]["id"]])
    return [C("activity", "Activity"), C("name", "Item"), C("kind", "Kind"), C("unit", "Unit"), C("stockQty", "Stock", "number"),
            C("reorderLevel", "Reorder at", "number"), C("purchasePrice", "Cost", "money"), C("salePrice", "Price", "money"),
            C("stockValue", "Value at cost", "money")], rows, ["stockValue"]


@report("business_pl", "Business profit and loss", "Business reports")
def r_business_pl(cur, ctx):
    cur.execute("SELECT id, name FROM shg_business_activities WHERE group_id = %s ORDER BY name", (ctx["group"]["id"],))
    rows = []
    for a in cur.fetchall():
        pl = business_pl(cur, a["id"], ctx["from"], ctx["to"])
        rows.append({"activity": a["name"], "income": pl["cashBasis"]["income"], "expenses": pl["cashBasis"]["expenses"],
                     "profit": pl["cashBasis"]["profit"], "receivables": pl["receivables"], "payables": pl["payables"],
                     "stockAtCost": pl["stockAtCost"]})
    return [C("activity", "Activity"), C("income", "Income received", "money"), C("expenses", "Expenses paid", "money"),
            C("profit", "Profit (cash)", "money"), C("receivables", "To receive", "money"), C("payables", "To pay", "money"),
            C("stockAtCost", "Stock at cost", "money")], rows, ["income", "expenses", "profit"]


@report("schemes", "Scheme application status", "Business reports")
def r_schemes(cur, ctx):
    rows = _rows(cur, SCHEME_SELECT + " WHERE s.group_id = %s ORDER BY s.applied_on NULLS LAST", [ctx["group"]["id"]])
    return [C("applicationKind", "Type"), C("schemeName", "Scheme / bank"), C("applicationNumber", "Application"),
            C("appliedOn", "Applied", "date"), C("amountRequested", "Requested", "money"), C("amountSanctioned", "Sanctioned", "money"),
            C("amountReceived", "Received", "money"), C("amountUtilized", "Utilized", "money"), C("status", "Status")], rows, \
        ["amountReceived", "amountUtilized"]


@report("audit_trail", "Audit trail", "Audit reports")
def r_audit(cur, ctx):
    rows = _rows(cur, """SELECT e.created_at, e.entity_type, e.entity_id, e.action, e.from_status, e.to_status, e.details::text AS details
                         FROM shg_events e WHERE e.owner_id = %s AND e.group_id = %s AND e.created_at::date BETWEEN %s AND %s
                         ORDER BY e.created_at DESC, e.id DESC LIMIT 2000""",
                 [g.owner_id, ctx["group"]["id"], ctx["from"], ctx["to"]])
    return [C("createdAt", "When", "date"), C("entityType", "Record"), C("entityId", "ID", "number"), C("action", "Action"),
            C("fromStatus", "From"), C("toStatus", "To"), C("details", "Details")], rows, []


@bachatgat_bp.get("/reports")
def report_catalog():
    return ok({"reports": [{"key": k, "title": v["title"], "category": v["category"], "needsGroup": v["needsGroup"],
                            "needsMember": v["needsMember"]} for k, v in REPORTS.items()]})


def _xlsx(title, columns, rows, totals, meta):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        raise ApiError(503, "Excel export is not available on this server (openpyxl is not installed); use PDF")

    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([meta])
    ws.append([])
    ws.append([c["label"] for c in columns])
    for cell in ws[4]:
        cell.font = Font(bold=True)
    for r in rows:
        ws.append([r.get(c["key"]) for c in columns])
    if totals:
        ws.append([("Total" if i == 0 else totals.get(c["key"], "")) for i, c in enumerate(columns)])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
    for i, c in enumerate(columns, start=1):
        ws.column_dimensions[ws.cell(row=4, column=i).column_letter].width = 14 if c["type"] != "text" else 22
    buf = io.BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue()).decode("ascii")


@bachatgat_bp.get("/reports/<key>")
def run_report(key):
    spec = REPORTS.get(key)
    if not spec:
        raise ApiError(404, "Unknown report")
    start, end = _range()
    with tx() as cur:
        group = _group_arg(cur, required=spec["needsGroup"])
        member = None
        if request.args.get("memberId"):
            member = load_member(cur, request.args.get("memberId", type=int))
            if group and member["group_id"] != group["id"]:
                raise ApiError(422, "Member is not in this group")
        if spec["needsMember"] and not member:
            raise ApiError(422, "Choose a member")
        ctx = {"group": group, "member": member, "from": start, "to": end, "status": request.args.get("status")}
        columns, rows, total_keys = spec["fn"](cur, ctx)
    totals = {}
    for k in total_keys:
        totals[k] = round(sum(float(r.get(k) or 0) for r in rows), 2)
    meta = " | ".join(x for x in [
        f"{group['name_en']} ({group['group_code']})" if group else "All groups",
        f"{member['full_name']} #{member['member_number']}" if member else "",
        f"{start.isoformat()} to {end.isoformat()}",
        f"Generated {date.today().isoformat()}",
    ] if x)
    payload = {"key": key, "title": spec["title"], "columns": columns, "rows": rows, "totals": totals, "meta": meta,
               "from": start, "to": end}
    if request.args.get("format") == "xlsx":
        from bachatgat_common import jsonable
        safe_rows = jsonable(rows)
        return ok({"fileName": f"{key}-{date.today().isoformat()}.xlsx",
                   "fileBase64": _xlsx(spec["title"], columns, safe_rows, totals, meta)})
    return ok(payload)
