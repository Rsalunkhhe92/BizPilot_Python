"""
Mahila Bachat Gat - internal loans, repayments, meetings and resolutions.

Loan lifecycle
  Draft -> Submitted (eligibility checked) -> Under Review (reviewer remarks)
  -> Approved (needs a Passed LOAN_APPROVAL resolution of a held meeting) | Rejected
  -> Active (disbursed: authorized by President/Treasurer who is not the borrower,
     schedule generated, LOAN_DISBURSEMENT posted) -> Overdue <-> Active
  -> Closed (everything paid) | Written Off (needs a LOAN_WRITE_OFF resolution)
A loan can never be disbursed unless it is Approved.
"""

from datetime import date, timedelta

from flask import g, request

from bachatgat_common import (
    FREQUENCY_VALUES,
    PAYMENT_METHODS,
    ApiError,
    TXN_SELECT,
    account_balance,
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
    load_loan,
    load_member,
    load_policy,
    member_savings_balance,
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
    serialize_txn,
    text,
    tx,
    use_resolution,
)
from bachatgat_finance import (
    ZERO,
    FinanceError,
    add_period,
    allocate_payment,
    build_schedule,
    check_loan_eligibility,
    foreclosure_quote,
    installment_outstanding,
    money,
    penalty_for,
    schedule_totals,
)

OPEN_LOAN_STATUSES = ("Active", "Overdue", "Disbursed")

LOAN_SELECT = """
    SELECT l.*, m.full_name AS member_name, m.member_number, m.mobile AS member_mobile,
           gr.name_en AS group_name, gr.group_code,
           (SELECT COALESCE(SUM(debit - credit), 0) FROM shg_ledger_lines x
             WHERE x.loan_id = l.id AND x.account_code = 'LOAN_RECEIVABLE') AS principal_outstanding
    FROM shg_loans l JOIN shg_members m ON m.id = l.member_id JOIN shg_groups gr ON gr.id = l.group_id
"""


def _policy_dict(policy):
    return {
        "maxLoanAmount": policy["max_loan_amount"], "savingsMultiple": policy["savings_multiple"],
        "minMembershipMonths": policy["min_membership_months"], "maxInstallments": policy["max_installments"],
        "allowMultipleLoans": policy["allow_multiple_loans"], "penaltyType": policy["penalty_type"],
        "penaltyValue": policy["penalty_value"], "graceDays": policy["grace_days"],
    }


def loan_installments(cur, loan_id, lock=False):
    """Current schedule (latest version, superseded rows excluded) in finance-module shape."""
    cur.execute(
        """SELECT * FROM shg_loan_installments WHERE loan_id = %s
             AND schedule_version = (SELECT schedule_version FROM shg_loans WHERE id = %s)
             AND status <> 'Rescheduled'
           ORDER BY installment_number""" + (" FOR UPDATE" if lock else ""),
        (loan_id, loan_id),
    )
    return [_inst(r) for r in cur.fetchall()]


def _inst(r):
    return {
        "id": r["id"], "number": r["installment_number"], "dueDate": r["due_date"],
        "principalDue": r["principal_due"], "interestDue": r["interest_due"],
        "principalPaid": r["principal_paid"], "interestPaid": r["interest_paid"],
        "interestWaived": r["interest_waived"], "penaltyDue": r["penalty_due"],
        "penaltyPaid": r["penalty_paid"], "penaltyWaived": r["penalty_waived"],
        "principalWrittenOff": r["principal_written_off"], "status": r["status"],
    }


def _inst_out(i, as_of):
    o = installment_outstanding({**i, "principalPaid": money(i["principalPaid"]) + money(i["principalWrittenOff"])})
    return {
        **i, "outstandingPrincipal": o["principal"], "outstandingInterest": o["interest"],
        "outstandingPenalty": o["penalty"], "outstandingTotal": o["principal"] + o["interest"] + o["penalty"],
        "isOverdue": i["status"] not in ("Paid", "Written Off") and i["dueDate"] < as_of and (o["principal"] + o["interest"]) > 0,
    }


def assess_penalties(cur, loan, as_of):
    policy = _policy_dict(load_policy(cur, loan["group_id"]))
    for inst in loan_installments(cur, loan["id"], lock=True):
        if inst["status"] in ("Paid", "Written Off"):
            continue
        new_penalty = penalty_for(inst, as_of, policy)
        if new_penalty > money(inst["penaltyDue"]):
            cur.execute("UPDATE shg_loan_installments SET penalty_due = %s WHERE id = %s", (new_penalty, inst["id"]))


def refresh_installment_status(cur, inst_id):
    cur.execute(
        """UPDATE shg_loan_installments SET status = CASE
              WHEN principal_paid + principal_written_off >= principal_due
                   AND interest_paid + interest_waived >= interest_due
                   AND penalty_paid + penalty_waived >= penalty_due
                THEN CASE WHEN principal_written_off > 0 THEN 'Written Off' ELSE 'Paid' END
              WHEN principal_paid + interest_paid + penalty_paid > 0 THEN 'Partial'
              ELSE 'Pending' END
           WHERE id = %s""",
        (inst_id,),
    )


def sync_loan_status(cur, loan_id, as_of=None):
    """Active <-> Overdue <-> Closed from the schedule; returns the new status."""
    as_of = as_of or date.today()
    cur.execute("SELECT * FROM shg_loans WHERE id = %s FOR UPDATE", (loan_id,))
    loan = cur.fetchone()
    if loan["status"] not in OPEN_LOAN_STATUSES:
        return loan["status"]
    policy = load_policy(cur, loan["group_id"])
    insts = loan_installments(cur, loan_id)
    remaining = ZERO
    overdue = False
    for i in insts:
        o = installment_outstanding({**i, "principalPaid": money(i["principalPaid"]) + money(i["principalWrittenOff"])})
        owed = o["principal"] + o["interest"] + o["penalty"]
        remaining += owed
        if owed > 0 and i["dueDate"] + timedelta(days=policy["grace_days"]) < as_of:
            overdue = True
    if remaining == 0 and insts:
        new_status = "Closed"
    else:
        new_status = "Overdue" if overdue else "Active"
    if new_status != loan["status"]:
        cur.execute(
            "UPDATE shg_loans SET status = %s, closed_on = %s, version = version + 1, updated_at = NOW() WHERE id = %s",
            (new_status, as_of if new_status == "Closed" else None, loan_id),
        )
        record_event(cur, loan["group_id"], "LOAN", loan_id, "STATUS_" + new_status.upper(), loan["status"], new_status)
        if new_status == "Overdue":
            notify(cur, loan["group_id"], "OVERDUE_REPAYMENT", "Loan overdue",
                   f"Loan {loan['application_number']} has an overdue installment.",
                   f"loan-overdue-{loan_id}-{as_of.isoformat()[:7]}", "LOAN", loan_id)
        if new_status == "Closed":
            notify(cur, loan["group_id"], "LOAN_STATUS", "Loan closed",
                   f"Loan {loan['application_number']} is fully repaid.", f"loan-closed-{loan_id}", "LOAN", loan_id)
    return new_status


def quote_for_loan(cur, loan_id, as_of):
    cur.execute("SELECT interest_method FROM shg_loans WHERE id = %s", (loan_id,))
    method = cur.fetchone()["interest_method"]
    insts = loan_installments(cur, loan_id)
    adj = [{**i, "principalPaid": money(i["principalPaid"]) + money(i["principalWrittenOff"])} for i in insts]
    q = foreclosure_quote(adj, as_of, method)
    due_now = ZERO
    for i in adj:
        if i["dueDate"] <= as_of:
            o = installment_outstanding(i)
            due_now += o["principal"] + o["interest"] + o["penalty"]
    q["dueNow"] = due_now
    nxt = next((i for i in adj if i["dueDate"] > as_of and sum(installment_outstanding(i).values()) > 0), None)
    q["nextInstallment"] = {"number": nxt["number"], "dueDate": nxt["dueDate"],
                            "amount": sum(installment_outstanding(nxt).values())} if nxt else None
    return q


def apply_loan_payment(cur, group, loan_id, amount, txn_date, method, bank_account_id, reference, narration,
                       offset=False, foreclose=False):
    """Allocates and posts one repayment. With foreclose=True and an amount equal
    to the foreclosure quote, not-yet-due interest of REDUCING loans is waived
    (it has not accrued). `offset` settles from the member's savings (exit)."""
    loan = load_loan(cur, loan_id, lock=True)
    if loan["status"] not in OPEN_LOAN_STATUSES:
        raise ApiError(409, f"Loan is {loan['status']}; repayments need an active loan")
    if txn_date < loan["disbursed_on"]:
        raise ApiError(422, "Repayment date is before the disbursement date")
    assess_penalties(cur, loan, txn_date)
    amount = money(amount)
    insts = loan_installments(cur, loan_id, lock=True)
    if foreclose:
        quote = quote_for_loan(cur, loan_id, txn_date)
        if amount != quote["total"]:
            raise ApiError(422, f"Foreclosure amount must be exactly {quote['total']}")
        if quote["waivedInterest"] > 0:
            for i in insts:
                if i["dueDate"] > txn_date:
                    o = installment_outstanding(i)
                    if o["interest"] > 0:
                        cur.execute("UPDATE shg_loan_installments SET interest_waived = interest_waived + %s WHERE id = %s",
                                    (o["interest"], i["id"]))
            record_event(cur, group["id"], "LOAN", loan_id, "FORECLOSURE_INTEREST_WAIVED",
                         details={"waivedInterest": quote["waivedInterest"]})
            insts = loan_installments(cur, loan_id, lock=True)
    adj = [{**i, "principalPaid": money(i["principalPaid"]) + money(i["principalWrittenOff"])} for i in insts]
    try:
        allocations, totals = allocate_payment(amount, adj)
    except FinanceError as e:
        raise ApiError(422, str(e))
    txn = post_transaction(
        cur, group, "SAVINGS_LOAN_OFFSET" if offset else "LOAN_REPAYMENT", amount, txn_date,
        "Adjustment" if offset else method, bank_account_id, member_id=loan["member_id"], loan_id=loan_id,
        split=totals, narration=narration or f"Repayment of loan {loan['application_number']}",
        reference_number=reference, receipt_prefix="LRP",
    )
    for a in allocations:
        cur.execute(
            """UPDATE shg_loan_installments SET principal_paid = principal_paid + %s, interest_paid = interest_paid + %s,
                   penalty_paid = penalty_paid + %s WHERE id = %s""",
            (a["principal"], a["interest"], a["penalty"], a["installmentId"]),
        )
        refresh_installment_status(cur, a["installmentId"])
        cur.execute(
            "INSERT INTO shg_allocations (txn_id, installment_id, principal, interest, penalty, amount) VALUES (%s,%s,%s,%s,%s,%s)",
            (txn["id"], a["installmentId"], a["principal"], a["interest"], a["penalty"],
             a["principal"] + a["interest"] + a["penalty"]),
        )
    sync_loan_status(cur, loan_id, max(txn_date, date.today()))
    notify(cur, group["id"], "TRANSACTION_CONFIRMATION", "Repayment recorded",
           f"Repayment receipt {txn['receipt_number']} recorded for loan {loan['application_number']}.",
           f"txn-{txn['id']}", "TRANSACTION", txn["id"])
    return txn


def _loan_out(cur, loan_id):
    cur.execute(LOAN_SELECT + " WHERE l.id = %s", (loan_id,))
    return camel(cur.fetchone())


def _eligibility(cur, loan_row_or_payload, member, policy, amount, installments):
    cur.execute(
        """SELECT COUNT(*) FILTER (WHERE status IN ('Active', 'Overdue', 'Disbursed', 'Approved')) AS active_loans,
                  COUNT(*) FILTER (WHERE status = 'Overdue') AS overdue_loans
           FROM shg_loans WHERE member_id = %s AND id <> %s""",
        (member["id"], loan_row_or_payload.get("id") or 0),
    )
    counts = cur.fetchone()
    return check_loan_eligibility(_policy_dict(policy), {
        "status": member["status"], "joiningDate": member["joining_date"],
        "savingsBalance": member_savings_balance(cur, member["group_id"], member["id"]),
        "activeLoans": counts["active_loans"], "overdueLoans": counts["overdue_loans"],
    }, {"amount": amount, "installments": installments, "asOf": date.today()})


# ---------------------------------------------------------------------------
# Loan endpoints
# ---------------------------------------------------------------------------
@bachatgat_bp.get("/loans")
def list_loans():
    size, offset, page = page_args()
    where, params = ["gr.owner_id = %s"], [g.owner_id]
    for arg, col in (("groupId", "l.group_id"), ("memberId", "l.member_id")):
        if request.args.get(arg):
            where.append(f"{col} = %s")
            params.append(request.args.get(arg, type=int))
    status = request.args.get("status")
    if status == "pending":
        where.append("l.status IN ('Submitted', 'Under Review')")
    elif status == "open":
        where.append("l.status IN ('Active', 'Overdue', 'Disbursed')")
    elif status:
        where.append("l.status = %s")
        params.append(status)
    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(m.full_name ILIKE %s OR l.application_number ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    with tx() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM shg_loans l JOIN shg_members m ON m.id = l.member_id JOIN shg_groups gr ON gr.id = l.group_id WHERE {' AND '.join(where)}",
            params,
        )
        total = cur.fetchone()["n"]
        cur.execute(f"{LOAN_SELECT} WHERE {' AND '.join(where)} ORDER BY l.application_date DESC, l.id DESC LIMIT %s OFFSET %s",
                    params + [size, offset])
        loans = [camel(r) for r in cur.fetchall()]
    return ok({"loans": loans, "total": total, "page": page, "pageSize": size})


def _loan_terms(payload, policy):
    return {
        "interest_method": choice(payload, "interestMethod", ("FLAT", "REDUCING"), default=policy["default_interest_method"]),
        "interest_rate": parse_money(payload, "interestRate", default=str(policy["default_interest_rate"]), label="Interest rate"),
        "installments": parse_int(payload, "installments", required=True, minimum=1, maximum=520, label="Number of installments"),
        "frequency": choice(payload, "frequency", FREQUENCY_VALUES, default=policy["default_frequency"]),
    }


@bachatgat_bp.post("/loans/preview-schedule")
def preview_schedule():
    payload = body()
    group_id = parse_int(payload, "groupId", required=True)
    with tx() as cur:
        load_group(cur, group_id)
        policy = load_policy(cur, group_id)
    terms = _loan_terms(payload, policy)
    amount = parse_money(payload, "amount", required=True, positive=True, label="Amount")
    first_due = parse_date(payload, "firstDueDate", default=add_period(date.today(), terms["frequency"], 1))
    rows = build_schedule(amount, terms["interest_rate"], terms["installments"], terms["interest_method"],
                          terms["frequency"], first_due, policy["round_to_rupee"])
    return ok({"schedule": rows, "totals": schedule_totals(rows), "terms": camel(terms)})


@bachatgat_bp.post("/loans")
def create_loan():
    payload = body()
    member_id = parse_int(payload, "memberId", required=True, label="Member")
    amount = parse_money(payload, "requestedAmount", required=True, positive=True, label="Requested amount")
    purpose = text(payload, "purpose", 200, required=True, label="Purpose")
    with tx() as cur:
        member = load_member(cur, member_id)
        group = load_group(cur, member["group_id"])
        require_active_group(group)
        if member["status"] != "Active":
            raise ApiError(409, "Only active members can apply for loans")
        policy = load_policy(cur, group["id"])
        terms = _loan_terms(payload, policy)
        if terms["interest_rate"] > 60:
            raise ApiError(422, "Interest rate must be at most 60% per year")
        app_date = parse_date(payload, "applicationDate", default=date.today())
        if app_date > date.today():
            raise ApiError(422, "Application date cannot be in the future")
        number = formatted_number(cur, group, "LOAN", "LN", app_date)
        cur.execute(
            """INSERT INTO shg_loans (group_id, member_id, application_number, application_date, requested_amount,
                   purpose, interest_method, interest_rate, installments, frequency, remarks)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (group["id"], member_id, number, app_date, amount, purpose, terms["interest_method"],
             terms["interest_rate"], terms["installments"], terms["frequency"], text(payload, "remarks", 500)),
        )
        loan_id = cur.fetchone()["id"]
        record_event(cur, group["id"], "LOAN", loan_id, "CREATED", None, "Draft", {"amount": amount})
        out = _loan_out(cur, loan_id)
    return ok({"loan": out}, 201)


@bachatgat_bp.put("/loans/<int:loan_id>")
def update_loan(loan_id):
    payload = body()
    with tx() as cur:
        loan = load_loan(cur, loan_id, lock=True)
        check_version(loan, payload)
        if loan["status"] != "Draft":
            raise ApiError(409, "Only draft applications can be edited")
        policy = load_policy(cur, loan["group_id"])
        merged = {
            "interestMethod": loan["interest_method"], "interestRate": loan["interest_rate"],
            "installments": loan["installments"], "frequency": loan["frequency"], **payload,
        }
        terms = _loan_terms(merged, policy)
        amount = parse_money(payload, "requestedAmount", positive=True, default=str(loan["requested_amount"]))
        cur.execute(
            """UPDATE shg_loans SET requested_amount = %s, purpose = %s, interest_method = %s, interest_rate = %s,
                   installments = %s, frequency = %s, remarks = %s, version = version + 1, updated_at = NOW() WHERE id = %s""",
            (amount, text(payload, "purpose", 200) or loan["purpose"], terms["interest_method"], terms["interest_rate"],
             terms["installments"], terms["frequency"], text(payload, "remarks", 500) or loan["remarks"], loan_id),
        )
        record_event(cur, loan["group_id"], "LOAN", loan_id, "UPDATED")
        out = _loan_out(cur, loan_id)
    return ok({"loan": out})


LOAN_TRANSITIONS = {
    "submit": ("Draft",),
    "review": ("Submitted",),
    "approve": ("Under Review",),
    "reject": ("Submitted", "Under Review"),
    "withdraw": ("Draft", "Submitted", "Under Review", "Approved"),
}


@bachatgat_bp.post("/loans/<int:loan_id>/workflow")
def loan_workflow(loan_id):
    payload = body()
    action = choice(payload, "action", tuple(LOAN_TRANSITIONS), label="Action")
    remarks = text(payload, "remarks", 500)
    with tx() as cur:
        loan = load_loan(cur, loan_id, lock=True)
        check_version(loan, payload)
        if loan["status"] not in LOAN_TRANSITIONS[action]:
            raise ApiError(409, f"Cannot {action} a loan that is {loan['status']}")
        group = load_group(cur, loan["group_id"])
        member = load_member(cur, loan["member_id"])
        policy = load_policy(cur, group["id"])
        sets = {}
        if action == "submit":
            require_active_group(group)
            problems = _eligibility(cur, loan, member, policy, loan["requested_amount"], loan["installments"])
            if problems:
                raise ApiError(422, "Member is not eligible for this loan", problems)
            sets["status"] = "Submitted"
        elif action == "review":
            if not remarks:
                raise ApiError(422, "Enter review remarks")
            sets.update(status="Under Review", review_remarks=remarks)
        elif action == "approve":
            require_active_group(group)
            approved = parse_money(payload, "approvedAmount", positive=True, default=str(loan["requested_amount"]), label="Approved amount")
            if approved > money(loan["requested_amount"]):
                raise ApiError(422, "Approved amount cannot exceed the requested amount")
            problems = _eligibility(cur, loan, member, policy, approved, loan["installments"])
            if problems:
                raise ApiError(422, "Member is not eligible for this loan", problems)
            limit = money(policy["loan_approval_limit"])
            res_id = parse_int(payload, "resolutionId", required=True, label="Meeting resolution")
            use_resolution(cur, group["id"], res_id, "LOAN_APPROVAL", "LOAN", loan_id, loan_id=loan_id,
                           amount=approved, supermajority=limit > 0 and approved > limit)
            sets.update(status="Approved", approved_amount=approved, resolution_id=res_id)
        elif action in ("reject", "withdraw"):
            if not remarks:
                raise ApiError(422, "Give a reason")
            res_id = parse_int(payload, "resolutionId")
            if res_id:
                use_resolution(cur, group["id"], res_id, "LOAN_REJECTION", "LOAN", loan_id, loan_id=loan_id)
                sets["resolution_id"] = res_id
            sets.update(status="Rejected", rejection_reason=("Withdrawn: " if action == "withdraw" else "") + remarks)
        cur.execute(
            f"UPDATE shg_loans SET {', '.join(f'{k} = %s' for k in sets)}, version = version + 1, updated_at = NOW() WHERE id = %s",
            list(sets.values()) + [loan_id],
        )
        record_event(cur, group["id"], "LOAN", loan_id, action.upper(), loan["status"], sets["status"],
                     {"remarks": remarks, "resolutionId": sets.get("resolution_id"), "approvedAmount": sets.get("approved_amount")})
        notify(cur, group["id"], "LOAN_STATUS", f"Loan {sets['status'].lower()}",
               f"Loan application {loan['application_number']} is now {sets['status']}.",
               f"loan-{loan_id}-{sets['status']}", "LOAN", loan_id)
        out = _loan_out(cur, loan_id)
    return ok({"loan": out})


@bachatgat_bp.post("/loans/<int:loan_id>/disburse")
@idempotent
def disburse_loan(cur, loan_id):
    payload = body()
    loan = load_loan(cur, loan_id, lock=True)
    if loan["status"] != "Approved":
        raise ApiError(409, "Only approved loans can be disbursed")
    group = load_group(cur, loan["group_id"])
    policy = load_policy(cur, group["id"])
    auth_id = parse_int(payload, "authorizedByMemberId", required=True, label="Authorizing office-bearer")
    if auth_id == loan["member_id"]:
        raise ApiError(422, "The borrower cannot authorize her own disbursement")
    auth = load_member(cur, auth_id)
    if auth["group_id"] != group["id"] or auth["status"] != "Active" or auth["role"] not in ("President", "Treasurer"):
        raise ApiError(422, "Disbursement must be authorized by the group's active President or Treasurer")
    borrower = load_member(cur, loan["member_id"])
    if borrower["status"] != "Active":
        raise ApiError(409, "Borrower is no longer an active member")
    method = choice(payload, "paymentMethod", PAYMENT_METHODS, default="Cash")
    on = parse_date(payload, "disbursedOn", default=date.today())
    if on < loan["application_date"]:
        raise ApiError(422, "Disbursement date is before the application date")
    first_due = parse_date(payload, "firstDueDate", default=add_period(on, loan["frequency"], 1))
    if first_due <= on:
        raise ApiError(422, "First installment must fall after the disbursement date")
    amount = money(loan["approved_amount"])
    rows = build_schedule(amount, loan["interest_rate"], loan["installments"], loan["interest_method"],
                          loan["frequency"], first_due, policy["round_to_rupee"])
    txn = post_transaction(cur, group, "LOAN_DISBURSEMENT", amount, on, method, payload.get("bankAccountId"),
                           member_id=loan["member_id"], loan_id=loan_id, resolution_id=loan["resolution_id"],
                           narration=f"Disbursement of loan {loan['application_number']}",
                           reference_number=text(payload, "referenceNumber", 80), receipt_prefix="LDS")
    # A reversed earlier disbursement leaves its (superseded) schedule behind; start a new version.
    schedule_version = loan["schedule_version"] + 1
    for r in rows:
        cur.execute(
            """INSERT INTO shg_loan_installments (loan_id, schedule_version, installment_number, due_date, principal_due, interest_due)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (loan_id, schedule_version, r["number"], r["dueDate"], r["principal"], r["interest"]),
        )
    cur.execute(
        """UPDATE shg_loans SET status = 'Active', disbursed_on = %s, first_due_date = %s, schedule_version = %s,
               disbursement_authorized_by = %s, version = version + 1, updated_at = NOW() WHERE id = %s""",
        (on, first_due, schedule_version, auth_id, loan_id),
    )
    record_event(cur, group["id"], "LOAN", loan_id, "DISBURSED", "Approved", "Active",
                 {"amount": amount, "txnId": txn["id"], "authorizedBy": auth_id})
    notify(cur, group["id"], "LOAN_STATUS", "Loan disbursed",
           f"Loan {loan['application_number']} was disbursed.", f"loan-{loan_id}-disbursed", "LOAN", loan_id)
    return {"loan": _loan_out(cur, loan_id), "transaction": fetch_txn(cur, txn["id"]), "schedule": rows}, 201


@bachatgat_bp.get("/loans/<int:loan_id>")
def get_loan(loan_id):
    as_of = date.today()
    with tx() as cur:
        loan = load_loan(cur, loan_id)
        if loan["status"] in OPEN_LOAN_STATUSES:
            assess_penalties(cur, loan, as_of)
            sync_loan_status(cur, loan_id, as_of)
        out = _loan_out(cur, loan_id)
        schedule = [_inst_out(i, as_of) for i in loan_installments(cur, loan_id)]
        cur.execute(
            "SELECT * FROM shg_loan_installments WHERE loan_id = %s AND status = 'Rescheduled' ORDER BY schedule_version, installment_number",
            (loan_id,),
        )
        superseded = [camel(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT id, action, from_status, to_status, details, created_at FROM shg_events WHERE entity_type = 'LOAN' AND entity_id = %s AND owner_id = %s ORDER BY id",
            (loan_id, g.owner_id),
        )
        events = [camel(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT r.*, mt.meeting_number, mt.meeting_date FROM shg_resolutions r JOIN shg_meetings mt ON mt.id = r.meeting_id
               WHERE r.loan_id = %s ORDER BY r.id""",
            (loan_id,),
        )
        resolutions = [camel(r) for r in cur.fetchall()]
        cur.execute(TXN_SELECT + " WHERE t.loan_id = %s AND gr.owner_id = %s ORDER BY t.txn_date, t.id", (loan_id, g.owner_id))
        txns = [serialize_txn(r) for r in cur.fetchall()]
        quote = quote_for_loan(cur, loan_id, as_of) if out["status"] in OPEN_LOAN_STATUSES else None
        eligibility = None
        if out["status"] in ("Draft", "Submitted", "Under Review"):
            member = load_member(cur, loan["member_id"])
            eligibility = _eligibility(cur, loan, member, load_policy(cur, loan["group_id"]),
                                       loan["requested_amount"], loan["installments"])
        preview = None
        if out["status"] in ("Draft", "Submitted", "Under Review", "Approved"):
            policy = load_policy(cur, loan["group_id"])
            amt = loan["approved_amount"] or loan["requested_amount"]
            preview_rows = build_schedule(amt, loan["interest_rate"], loan["installments"], loan["interest_method"],
                                          loan["frequency"], add_period(as_of, loan["frequency"], 1), policy["round_to_rupee"])
            preview = {"schedule": preview_rows, "totals": schedule_totals(preview_rows)}
    return ok({"loan": out, "schedule": schedule, "supersededInstallments": superseded, "events": events,
               "resolutions": resolutions, "transactions": txns, "quote": quote,
               "eligibilityProblems": eligibility, "proposedSchedule": preview})


@bachatgat_bp.get("/loans/<int:loan_id>/quote")
def loan_quote(loan_id):
    as_of = parse_date(request.args, "date", default=date.today())
    with tx() as cur:
        loan = load_loan(cur, loan_id)
        if loan["status"] not in OPEN_LOAN_STATUSES:
            raise ApiError(409, f"Loan is {loan['status']}")
        assess_penalties(cur, loan, as_of)
        quote = quote_for_loan(cur, loan_id, as_of)
    return ok({"quote": quote})


@bachatgat_bp.post("/loans/<int:loan_id>/repay")
@idempotent
def repay_loan(cur, loan_id):
    payload = body()
    amount = parse_money(payload, "amount", required=True, positive=True, label="Amount")
    method = choice(payload, "paymentMethod", PAYMENT_METHODS, default="Cash")
    txn_date = parse_date(payload, "txnDate", default=date.today())
    loan = load_loan(cur, loan_id)
    group = load_group(cur, loan["group_id"])
    txn = apply_loan_payment(cur, group, loan_id, amount, txn_date, method, payload.get("bankAccountId"),
                             text(payload, "referenceNumber", 80), text(payload, "notes", 300),
                             foreclose=bool(payload.get("foreclose")))
    cur.execute("SELECT * FROM shg_allocations WHERE txn_id = %s", (txn["id"],))
    allocations = [camel(r) for r in cur.fetchall()]
    return {"transaction": fetch_txn(cur, txn["id"]), "allocations": allocations, "loan": _loan_out(cur, loan_id),
            "quote": quote_for_loan(cur, loan_id, txn_date) if _loan_out(cur, loan_id)["status"] in OPEN_LOAN_STATUSES else None}, 201


def _require_no_past_due_interest(cur, loan_id, as_of):
    for i in loan_installments(cur, loan_id):
        if i["dueDate"] <= as_of:
            o = installment_outstanding(i)
            if o["interest"] > 0 or o["penalty"] > 0:
                raise ApiError(409, "Collect or waive the overdue interest and penalty before rescheduling")


@bachatgat_bp.post("/loans/<int:loan_id>/reschedule")
def reschedule_loan(loan_id):
    payload = body()
    as_of = date.today()
    with tx() as cur:
        loan = load_loan(cur, loan_id, lock=True)
        if loan["status"] not in ("Active", "Overdue"):
            raise ApiError(409, "Only active or overdue loans can be rescheduled")
        group = load_group(cur, loan["group_id"])
        require_active_group(group)
        policy = load_policy(cur, group["id"])
        assess_penalties(cur, loan, as_of)
        _require_no_past_due_interest(cur, loan_id, as_of)
        res_id = parse_int(payload, "resolutionId", required=True, label="Meeting resolution")
        use_resolution(cur, group["id"], res_id, "LOAN_RESCHEDULE", "LOAN_RESCHEDULE", loan_id, loan_id=loan_id)
        terms = _loan_terms({"interestMethod": loan["interest_method"], "interestRate": loan["interest_rate"],
                             "frequency": loan["frequency"], **payload}, policy)
        if terms["installments"] > policy["max_installments"]:
            raise ApiError(422, f"Term exceeds the group maximum of {policy['max_installments']} installments")
        first_due = parse_date(payload, "firstDueDate", required=True, label="First due date")
        if first_due <= as_of:
            raise ApiError(422, "First due date must be in the future")
        remaining = ZERO
        for i in loan_installments(cur, loan_id, lock=True):
            o = installment_outstanding({**i, "principalPaid": money(i["principalPaid"]) + money(i["principalWrittenOff"])})
            if o["principal"] > 0 or o["interest"] > 0:
                remaining += o["principal"]
                cur.execute("UPDATE shg_loan_installments SET status = 'Rescheduled' WHERE id = %s", (i["id"],))
        if remaining <= 0:
            raise ApiError(409, "Nothing left to reschedule")
        new_version = loan["schedule_version"] + 1
        rows = build_schedule(remaining, terms["interest_rate"], terms["installments"], terms["interest_method"],
                              terms["frequency"], first_due, policy["round_to_rupee"])
        for r in rows:
            cur.execute(
                """INSERT INTO shg_loan_installments (loan_id, schedule_version, installment_number, due_date, principal_due, interest_due)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (loan_id, new_version, r["number"], r["dueDate"], r["principal"], r["interest"]),
            )
        cur.execute(
            """UPDATE shg_loans SET schedule_version = %s, interest_rate = %s, installments = %s, frequency = %s,
                   interest_method = %s, version = version + 1, updated_at = NOW() WHERE id = %s""",
            (new_version, terms["interest_rate"], terms["installments"], terms["frequency"], terms["interest_method"], loan_id),
        )
        record_event(cur, group["id"], "LOAN", loan_id, "RESCHEDULED", details={
            "resolutionId": res_id, "principal": remaining, "installments": terms["installments"], "scheduleVersion": new_version})
        sync_loan_status(cur, loan_id, as_of)
        out = _loan_out(cur, loan_id)
    return ok({"loan": out, "schedule": rows})


@bachatgat_bp.post("/loans/<int:loan_id>/waive")
def waive_loan_charges(loan_id):
    """Waives outstanding interest and/or penalty of installments due up to today
    (or the selected installments), authorized by a LOAN_WAIVER resolution."""
    payload = body()
    heads = payload.get("heads") or ["penalty"]
    if not set(heads) <= {"interest", "penalty"} or not heads:
        raise ApiError(422, "heads must contain interest and/or penalty")
    selected = set(int(x) for x in (payload.get("installmentIds") or []))
    as_of = date.today()
    with tx() as cur:
        loan = load_loan(cur, loan_id, lock=True)
        if loan["status"] not in ("Active", "Overdue"):
            raise ApiError(409, "Only active or overdue loans can have charges waived")
        group = load_group(cur, loan["group_id"])
        assess_penalties(cur, loan, as_of)
        plan = []
        total = ZERO
        for i in loan_installments(cur, loan_id, lock=True):
            if selected and i["id"] not in selected:
                continue
            if not selected and i["dueDate"] > as_of:
                continue
            o = installment_outstanding(i)
            w_int = o["interest"] if "interest" in heads else ZERO
            w_pen = o["penalty"] if "penalty" in heads else ZERO
            if w_int or w_pen:
                plan.append((i["id"], w_int, w_pen))
                total += w_int + w_pen
        if total <= 0:
            raise ApiError(409, "Nothing to waive")
        res_id = parse_int(payload, "resolutionId", required=True, label="Meeting resolution")
        use_resolution(cur, group["id"], res_id, "LOAN_WAIVER", "LOAN_WAIVER", loan_id, loan_id=loan_id, amount=total)
        for inst_id, w_int, w_pen in plan:
            cur.execute(
                "UPDATE shg_loan_installments SET interest_waived = interest_waived + %s, penalty_waived = penalty_waived + %s WHERE id = %s",
                (w_int, w_pen, inst_id),
            )
            refresh_installment_status(cur, inst_id)
        record_event(cur, group["id"], "LOAN", loan_id, "CHARGES_WAIVED", details={"resolutionId": res_id, "total": total, "heads": heads})
        sync_loan_status(cur, loan_id, as_of)
        out = _loan_out(cur, loan_id)
    return ok({"loan": out, "waived": total})


@bachatgat_bp.post("/loans/<int:loan_id>/write-off")
@idempotent
def write_off_loan(cur, loan_id):
    payload = body()
    as_of = parse_date(payload, "txnDate", default=date.today())
    loan = load_loan(cur, loan_id, lock=True)
    if loan["status"] not in ("Active", "Overdue"):
        raise ApiError(409, "Only active or overdue loans can be written off")
    group = load_group(cur, loan["group_id"])
    principal = account_balance(cur, group["id"], "LOAN_RECEIVABLE", loan_id=loan_id)
    if principal <= 0:
        raise ApiError(409, "No principal outstanding")
    res_id = parse_int(payload, "resolutionId", required=True, label="Meeting resolution")
    txn = post_transaction(cur, group, "LOAN_WRITE_OFF", principal, as_of, "Adjustment", None,
                           member_id=loan["member_id"], loan_id=loan_id, resolution_id=res_id,
                           narration=text(payload, "reason", 300, required=True, label="Reason"))
    use_resolution(cur, group["id"], res_id, "LOAN_WRITE_OFF", "TRANSACTION", txn["id"], loan_id=loan_id, amount=principal)
    for i in loan_installments(cur, loan_id, lock=True):
        o = installment_outstanding({**i, "principalPaid": money(i["principalPaid"]) + money(i["principalWrittenOff"])})
        if any(o.values()):
            cur.execute(
                """UPDATE shg_loan_installments SET principal_written_off = principal_written_off + %s,
                       interest_waived = interest_waived + %s, penalty_waived = penalty_waived + %s WHERE id = %s""",
                (o["principal"], o["interest"], o["penalty"], i["id"]),
            )
            refresh_installment_status(cur, i["id"])
    cur.execute("UPDATE shg_loans SET status = 'Written Off', closed_on = %s, version = version + 1 WHERE id = %s", (as_of, loan_id))
    record_event(cur, group["id"], "LOAN", loan_id, "WRITTEN_OFF", loan["status"], "Written Off",
                 {"principal": principal, "txnId": txn["id"], "resolutionId": res_id})
    return {"loan": _loan_out(cur, loan_id), "transaction": fetch_txn(cur, txn["id"])}, 201


@bachatgat_bp.get("/loans/<int:loan_id>/statement")
def loan_statement(loan_id):
    """Outstanding / closure statement: terms, disbursement, every repayment with
    its split, current schedule and totals - all from posted records."""
    with tx() as cur:
        load_loan(cur, loan_id)
        loan = _loan_out(cur, loan_id)
        schedule = [_inst_out(i, date.today()) for i in loan_installments(cur, loan_id)]
        cur.execute(TXN_SELECT + " WHERE t.loan_id = %s AND gr.owner_id = %s ORDER BY t.txn_date, t.id", (loan_id, g.owner_id))
        txns = [serialize_txn(r) for r in cur.fetchall()]
        posted = [t for t in txns if t["postingStatus"] == "Posted" and t["txnType"] != "REVERSAL"]
        totals = {
            "disbursed": sum(money(t["amount"]) for t in posted if t["txnType"] == "LOAN_DISBURSEMENT"),
            "principalPaid": sum(money(t["principalAmount"]) for t in posted if t["txnType"] in ("LOAN_REPAYMENT", "SAVINGS_LOAN_OFFSET")),
            "interestPaid": sum(money(t["interestAmount"]) for t in posted if t["txnType"] in ("LOAN_REPAYMENT", "SAVINGS_LOAN_OFFSET")),
            "penaltyPaid": sum(money(t["penaltyAmount"]) for t in posted if t["txnType"] in ("LOAN_REPAYMENT", "SAVINGS_LOAN_OFFSET")),
            "writtenOff": sum(money(t["amount"]) for t in posted if t["txnType"] == "LOAN_WRITE_OFF"),
            "interestWaived": sum(money(i["interestWaived"]) for i in schedule),
            "penaltyWaived": sum(money(i["penaltyWaived"]) for i in schedule),
            "principalOutstanding": money(loan["principalOutstanding"]),
        }
    return ok({"loan": loan, "schedule": schedule, "transactions": txns, "totals": totals, "generatedOn": date.today()})


@bachatgat_bp.get("/installments/upcoming")
def upcoming_installments():
    group_id = request.args.get("groupId", type=int)
    days = min(request.args.get("days", default=30, type=int), 180)
    with tx() as cur:
        where, params = ["gr.owner_id = %s", "l.status IN ('Active', 'Overdue')", "i.status IN ('Pending', 'Partial')",
                         "i.schedule_version = l.schedule_version", "i.due_date <= CURRENT_DATE + %s"], [g.owner_id, days]
        if group_id:
            where.append("l.group_id = %s")
            params.append(group_id)
        if request.args.get("overdue") == "1":
            where.append("i.due_date < CURRENT_DATE")
        cur.execute(
            f"""SELECT i.*, l.application_number, l.group_id, m.full_name AS member_name, m.member_number, gr.name_en AS group_name,
                       (i.principal_due - i.principal_paid - i.principal_written_off) AS principal_outstanding,
                       (i.interest_due - i.interest_paid - i.interest_waived) AS interest_outstanding,
                       (i.penalty_due - i.penalty_paid - i.penalty_waived) AS penalty_outstanding,
                       GREATEST(CURRENT_DATE - i.due_date, 0) AS days_overdue
                FROM shg_loan_installments i JOIN shg_loans l ON l.id = i.loan_id
                JOIN shg_members m ON m.id = l.member_id JOIN shg_groups gr ON gr.id = l.group_id
                WHERE {' AND '.join(where)} ORDER BY i.due_date, m.member_number LIMIT 300""",
            params,
        )
        rows = [camel(r) for r in cur.fetchall()]
    return ok({"installments": rows})


# ---------------------------------------------------------------------------
# Meetings, attendance, resolutions
# ---------------------------------------------------------------------------
MEETING_SELECT = """
    SELECT mt.*, gr.name_en AS group_name, gr.group_code,
           (SELECT COUNT(*) FROM shg_meeting_attendance a WHERE a.meeting_id = mt.id AND a.present) AS present_count,
           (SELECT COUNT(*) FROM shg_meeting_attendance a WHERE a.meeting_id = mt.id) AS marked_count
    FROM shg_meetings mt JOIN shg_groups gr ON gr.id = mt.group_id
"""


def _load_meeting(cur, meeting_id, lock=False):
    cur.execute(
        "SELECT mt.* FROM shg_meetings mt JOIN shg_groups gr ON gr.id = mt.group_id WHERE mt.id = %s AND gr.owner_id = %s"
        + (" FOR UPDATE OF mt" if lock else ""),
        (meeting_id, g.owner_id),
    )
    mt = cur.fetchone()
    if not mt:
        raise ApiError(404, "Meeting not found")
    return mt


def _quorum(cur, meeting):
    policy = load_policy(cur, meeting["group_id"])
    members = active_member_count(cur, meeting["group_id"])
    cur.execute("SELECT COUNT(*) AS n FROM shg_meeting_attendance WHERE meeting_id = %s AND present", (meeting["id"],))
    present = cur.fetchone()["n"]
    needed = -(-members * policy["quorum_percent"] // 100)  # ceiling
    return {"activeMembers": members, "present": present, "required": needed, "met": present >= needed and present > 0,
            "quorumPercent": policy["quorum_percent"]}


@bachatgat_bp.get("/meetings")
def list_meetings():
    size, offset, page = page_args()
    where, params = ["gr.owner_id = %s"], [g.owner_id]
    if request.args.get("groupId"):
        where.append("mt.group_id = %s")
        params.append(request.args.get("groupId", type=int))
    if request.args.get("status"):
        where.append("mt.status = %s")
        params.append(request.args["status"])
    if request.args.get("upcoming") == "1":
        where.append("mt.meeting_date >= CURRENT_DATE AND mt.status = 'Scheduled'")
    for arg, op in (("from", ">="), ("to", "<=")):
        if request.args.get(arg):
            where.append(f"mt.meeting_date {op} %s")
            params.append(request.args[arg])
    order = "mt.meeting_date ASC" if request.args.get("upcoming") == "1" else "mt.meeting_date DESC"
    with tx() as cur:
        cur.execute(f"{MEETING_SELECT} WHERE {' AND '.join(where)} ORDER BY {order}, mt.id DESC LIMIT %s OFFSET %s",
                    params + [size, offset])
        rows = [camel(r) for r in cur.fetchall()]
    return ok({"meetings": rows, "page": page})


@bachatgat_bp.post("/meetings")
def create_meeting():
    payload = body()
    group_id = parse_int(payload, "groupId", required=True, label="Group")
    meeting_date = parse_date(payload, "meetingDate", required=True, label="Meeting date")
    with tx() as cur:
        group = load_group(cur, group_id, lock=True)
        if group["status"] in ("Closed",):
            raise ApiError(409, "Group is closed")
        if meeting_date < group["formation_date"]:
            raise ApiError(422, "Meeting date is before the group's formation")
        number = next_number(cur, group_id, "MEETING")
        cur.execute(
            """INSERT INTO shg_meetings (group_id, meeting_number, meeting_date, meeting_time, location, agenda)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (group_id, number, meeting_date, text(payload, "meetingTime", 10), text(payload, "location", 200),
             text(payload, "agenda", 4000)),
        )
        meeting_id = cur.fetchone()["id"]
        record_event(cur, group_id, "MEETING", meeting_id, "SCHEDULED", None, "Scheduled", {"date": meeting_date})
        notify(cur, group_id, "MEETING_SCHEDULED", "Meeting scheduled",
               f"Meeting #{number} of {group['name_en']} is on {meeting_date.strftime('%d %b %Y')}.",
               f"meeting-{meeting_id}-scheduled", "MEETING", meeting_id)
        cur.execute(MEETING_SELECT + " WHERE mt.id = %s", (meeting_id,))
        out = camel(cur.fetchone())
    return ok({"meeting": out}, 201)


@bachatgat_bp.get("/meetings/<int:meeting_id>")
def get_meeting(meeting_id):
    with tx() as cur:
        mt = _load_meeting(cur, meeting_id)
        cur.execute(MEETING_SELECT + " WHERE mt.id = %s", (meeting_id,))
        meeting = camel(cur.fetchone())
        cur.execute(
            """SELECT m.id AS member_id, m.full_name, m.member_number, m.role, a.present, a.remarks
               FROM shg_members m LEFT JOIN shg_meeting_attendance a ON a.member_id = m.id AND a.meeting_id = %s
               WHERE m.group_id = %s AND (m.status = 'Active' OR a.member_id IS NOT NULL)
               ORDER BY m.member_number""",
            (meeting_id, mt["group_id"]),
        )
        attendance = [camel(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT r.*, l.application_number, lm.full_name AS loan_member_name FROM shg_resolutions r
               LEFT JOIN shg_loans l ON l.id = r.loan_id LEFT JOIN shg_members lm ON lm.id = l.member_id
               WHERE r.meeting_id = %s ORDER BY r.resolution_number""",
            (meeting_id,),
        )
        resolutions = [camel(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT txn_type, COUNT(*) AS count, COALESCE(SUM(amount), 0) AS amount FROM shg_transactions
               WHERE group_id = %s AND txn_date = %s AND posting_status = 'Posted'
                 AND txn_type IN ('SAVINGS_DEPOSIT', 'LOAN_REPAYMENT', 'LOAN_DISBURSEMENT', 'OPERATING_EXPENSE')
               GROUP BY txn_type""",
            (mt["group_id"], mt["meeting_date"]),
        )
        money_summary = [camel(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT l.id, l.application_number, l.requested_amount, l.status, m.full_name AS member_name
               FROM shg_loans l JOIN shg_members m ON m.id = l.member_id
               WHERE l.group_id = %s AND l.status IN ('Submitted', 'Under Review') ORDER BY l.id""",
            (mt["group_id"],),
        )
        pending_loans = [camel(r) for r in cur.fetchall()]
        quorum = _quorum(cur, mt)
    return ok({"meeting": meeting, "attendance": attendance, "resolutions": resolutions,
               "collectionSummary": money_summary, "pendingLoans": pending_loans, "quorum": quorum})


@bachatgat_bp.put("/meetings/<int:meeting_id>")
def update_meeting(meeting_id):
    payload = body()
    with tx() as cur:
        mt = _load_meeting(cur, meeting_id, lock=True)
        if mt["status"] in ("Minutes Approved", "Cancelled"):
            raise ApiError(409, f"Meeting is {mt['status']} and locked")
        fields = {}
        for key, col, size in (("meetingTime", "meeting_time", 10), ("location", "location", 200), ("agenda", "agenda", 4000),
                               ("discussions", "discussions", 8000), ("minutes", "minutes", 8000), ("decisions", "decisions", 4000)):
            if key in payload:
                fields[col] = text(payload, key, size)
        if "meetingDate" in payload:
            if mt["status"] != "Scheduled":
                raise ApiError(409, "Date can change only before the meeting is held")
            fields["meeting_date"] = parse_date(payload, "meetingDate", required=True)
        if "nextMeetingDate" in payload:
            nxt = parse_date(payload, "nextMeetingDate")
            if nxt and nxt <= mt["meeting_date"]:
                raise ApiError(422, "Next meeting must be after this meeting")
            fields["next_meeting_date"] = nxt
        if not fields:
            raise ApiError(422, "Nothing to update")
        cur.execute(f"UPDATE shg_meetings SET {', '.join(f'{k} = %s' for k in fields)}, updated_at = NOW() WHERE id = %s",
                    list(fields.values()) + [meeting_id])
        record_event(cur, mt["group_id"], "MEETING", meeting_id, "UPDATED", details={"fields": list(fields)})
        cur.execute(MEETING_SELECT + " WHERE mt.id = %s", (meeting_id,))
        out = camel(cur.fetchone())
    return ok({"meeting": out})


@bachatgat_bp.post("/meetings/<int:meeting_id>/attendance")
def mark_attendance(meeting_id):
    payload = body()
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ApiError(422, "entries are required")
    with tx() as cur:
        mt = _load_meeting(cur, meeting_id, lock=True)
        if mt["status"] in ("Minutes Approved", "Cancelled"):
            raise ApiError(409, f"Meeting is {mt['status']}")
        if mt["meeting_date"] > date.today():
            raise ApiError(409, "Attendance can be marked on or after the meeting date")
        cur.execute("SELECT id FROM shg_members WHERE group_id = %s AND status = 'Active'", (mt["group_id"],))
        allowed = {r["id"] for r in cur.fetchall()}
        for e in entries:
            mid = int(e.get("memberId") or 0)
            if mid not in allowed:
                raise ApiError(422, "Attendance can be marked only for active members of this group")
            cur.execute(
                """INSERT INTO shg_meeting_attendance (meeting_id, member_id, present, remarks) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (meeting_id, member_id) DO UPDATE SET present = EXCLUDED.present, remarks = EXCLUDED.remarks""",
                (meeting_id, mid, bool(e.get("present")), str(e.get("remarks") or "")[:200] or None),
            )
        if mt["status"] == "Scheduled":
            cur.execute("UPDATE shg_meetings SET status = 'Held', updated_at = NOW() WHERE id = %s", (meeting_id,))
            record_event(cur, mt["group_id"], "MEETING", meeting_id, "HELD", "Scheduled", "Held")
        record_event(cur, mt["group_id"], "MEETING", meeting_id, "ATTENDANCE_MARKED", details={"entries": len(entries)})
        quorum = _quorum(cur, mt)
    return ok({"saved": len(entries), "quorum": quorum})


RESOLUTION_TYPES = ("LOAN_APPROVAL", "LOAN_REJECTION", "LOAN_RESCHEDULE", "LOAN_WAIVER", "LOAN_WRITE_OFF",
                    "EXPENSE_APPROVAL", "SAVINGS_WITHDRAWAL", "BUSINESS_DECISION", "GENERAL")
AMOUNT_TYPES = ("LOAN_APPROVAL", "LOAN_WAIVER", "LOAN_WRITE_OFF", "EXPENSE_APPROVAL", "SAVINGS_WITHDRAWAL")
LOAN_TYPES = ("LOAN_APPROVAL", "LOAN_REJECTION", "LOAN_RESCHEDULE", "LOAN_WAIVER", "LOAN_WRITE_OFF")


@bachatgat_bp.post("/meetings/<int:meeting_id>/resolutions")
def add_resolution(meeting_id):
    payload = body()
    rtype = choice(payload, "resolutionType", RESOLUTION_TYPES, label="Resolution type")
    with tx() as cur:
        mt = _load_meeting(cur, meeting_id, lock=True)
        if mt["status"] != "Held":
            raise ApiError(409, "Resolutions are recorded in a held meeting before its minutes are approved")
        quorum = _quorum(cur, mt)
        if not quorum["met"]:
            raise ApiError(409, f"Quorum not met: {quorum['present']} present, {quorum['required']} required")
        votes = {k: parse_int(payload, k, default=0, minimum=0, maximum=1000) for k in ("votesFor", "votesAgainst", "votesAbstain")}
        if sum(votes.values()) > quorum["present"]:
            raise ApiError(422, f"Votes exceed the {quorum['present']} members present")
        if sum(votes.values()) == 0:
            raise ApiError(422, "Record the votes")
        loan_id = parse_int(payload, "loanId")
        if rtype in LOAN_TYPES:
            if not loan_id:
                raise ApiError(422, "Choose the loan this resolution is about")
            loan = load_loan(cur, loan_id)
            if loan["group_id"] != mt["group_id"]:
                raise ApiError(422, "Loan belongs to another group")
            # The borrower may not vote on her own loan: votes must fit among the other members present.
            cur.execute("SELECT present FROM shg_meeting_attendance WHERE meeting_id = %s AND member_id = %s",
                        (meeting_id, loan["member_id"]))
            borrower = cur.fetchone()
            if borrower and borrower["present"] and sum(votes.values()) > quorum["present"] - 1:
                raise ApiError(422, "The borrower may not vote on her own loan; votes exceed the other members present")
        amount = parse_money(payload, "amount", required=rtype in AMOUNT_TYPES, positive=rtype in AMOUNT_TYPES, label="Amount")
        result = "Passed" if votes["votesFor"] > votes["votesAgainst"] else "Failed"
        number = next_number(cur, mt["group_id"], f"RES:{meeting_id}")
        cur.execute(
            """INSERT INTO shg_resolutions (meeting_id, resolution_number, resolution_type, subject, resolution_text,
                   loan_id, amount, votes_for, votes_against, votes_abstain, result)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (meeting_id, number, rtype, text(payload, "subject", 200, required=True, label="Subject"),
             text(payload, "resolutionText", 4000), loan_id if rtype in LOAN_TYPES else None,
             amount if amount > 0 else None, votes["votesFor"], votes["votesAgainst"], votes["votesAbstain"], result),
        )
        res = cur.fetchone()
        record_event(cur, mt["group_id"], "RESOLUTION", res["id"], "RECORDED", details={
            "meetingId": meeting_id, "type": rtype, "result": result, "loanId": loan_id})
        if loan_id:
            record_event(cur, mt["group_id"], "LOAN", loan_id, "DISCUSSED_IN_MEETING", details={
                "meetingId": meeting_id, "resolutionId": res["id"], "type": rtype, "result": result})
    return ok({"resolution": camel(res)}, 201)


@bachatgat_bp.get("/resolutions")
def list_resolutions():
    group_id = request.args.get("groupId", type=int)
    if not group_id:
        raise ApiError(422, "groupId is required")
    where, params = ["mt.group_id = %s"], [group_id]
    for arg, col in (("type", "r.resolution_type"), ("loanId", "r.loan_id")):
        if request.args.get(arg):
            where.append(f"{col} = %s")
            params.append(request.args[arg])
    if request.args.get("usable") == "1":
        where.append("r.result = 'Passed' AND r.consumed_by_type IS NULL AND mt.status IN ('Held', 'Minutes Approved')")
    with tx() as cur:
        load_group(cur, group_id)
        cur.execute(
            f"""SELECT r.*, mt.meeting_number, mt.meeting_date, l.application_number FROM shg_resolutions r
                JOIN shg_meetings mt ON mt.id = r.meeting_id LEFT JOIN shg_loans l ON l.id = r.loan_id
                WHERE {' AND '.join(where)} ORDER BY mt.meeting_date DESC, r.resolution_number LIMIT 300""",
            params,
        )
        rows = [camel(r) for r in cur.fetchall()]
    return ok({"resolutions": rows})


@bachatgat_bp.post("/meetings/<int:meeting_id>/confirm")
def confirm_minutes(meeting_id):
    """President and Secretary each confirm the minutes; with both confirmations
    (and minutes written) the meeting becomes Minutes Approved and is locked."""
    payload = body()
    who = choice(payload, "by", ("President", "Secretary"), label="Confirming office-bearer")
    with tx() as cur:
        mt = _load_meeting(cur, meeting_id, lock=True)
        if mt["status"] != "Held":
            raise ApiError(409, "Only minutes of a held meeting can be confirmed")
        if not (mt["minutes"] or "").strip():
            raise ApiError(422, "Write the minutes before confirming")
        cur.execute("SELECT id FROM shg_members WHERE group_id = %s AND role = %s AND status = 'Active'", (mt["group_id"], who))
        holder = cur.fetchone()
        if not holder:
            raise ApiError(409, f"The group has no active {who}")
        cur.execute("SELECT present FROM shg_meeting_attendance WHERE meeting_id = %s AND member_id = %s", (meeting_id, holder["id"]))
        att = cur.fetchone()
        if not att or not att["present"]:
            raise ApiError(409, f"The {who} must have attended the meeting to confirm its minutes")
        col = "president_confirmed_at" if who == "President" else "secretary_confirmed_at"
        cur.execute(f"UPDATE shg_meetings SET {col} = NOW(), updated_at = NOW() WHERE id = %s RETURNING *", (meeting_id,))
        updated = cur.fetchone()
        record_event(cur, mt["group_id"], "MEETING", meeting_id, f"CONFIRMED_BY_{who.upper()}", details={"memberId": holder["id"]})
        if updated["president_confirmed_at"] and updated["secretary_confirmed_at"]:
            cur.execute("UPDATE shg_meetings SET status = 'Minutes Approved' WHERE id = %s", (meeting_id,))
            record_event(cur, mt["group_id"], "MEETING", meeting_id, "MINUTES_APPROVED", "Held", "Minutes Approved")
        cur.execute(MEETING_SELECT + " WHERE mt.id = %s", (meeting_id,))
        out = camel(cur.fetchone())
    return ok({"meeting": out})


@bachatgat_bp.post("/meetings/<int:meeting_id>/cancel")
def cancel_meeting(meeting_id):
    payload = body()
    reason = text(payload, "reason", 300, required=True, label="Reason")
    with tx() as cur:
        mt = _load_meeting(cur, meeting_id, lock=True)
        if mt["status"] != "Scheduled":
            raise ApiError(409, "Only scheduled meetings can be cancelled")
        cur.execute("UPDATE shg_meetings SET status = 'Cancelled', updated_at = NOW() WHERE id = %s", (meeting_id,))
        record_event(cur, mt["group_id"], "MEETING", meeting_id, "CANCELLED", "Scheduled", "Cancelled", {"reason": reason})
    return ok({"cancelled": True})
