"""
Mahila Bachat Gat - Autopilot (AI agent) that runs the group's day-to-day work by itself.

What one run does for a group (each step is idempotent, so running twice changes nothing):
  1. Members   - approves pending members whose ID proof is recorded.
  2. Dues      - raises every active member's savings dues up to today.
  3. Online    - pulls Razorpay payments (member QRs / links) and posts them as savings or repayments.
  4. Loan health - assesses late penalties, marks loans Overdue / Closed.
  5. Loans     - submits and reviews applications (eligibility from the group policy), approves those
                 within the Autopilot limit (or covered by a meeting resolution), and disburses approved
                 loans when the cash or bank balance allows, authorized by the Treasurer/President.
  6. Links     - texts members a Razorpay payment link for overdue savings and installments due
                 (cancels links that are no longer needed).
  7. Meetings  - keeps the next meeting scheduled; asks for the register of meetings not yet recorded.
  8. Reminders - refreshes in-app reminders.
  9. Books     - closes last month's books from the 7th of the next month.
The register photo (POST /agent/scan) is read by Gemini and posted the same way (attendance,
savings, repayments, loan requests, new members) before the run.

Safety
- Every posting goes through the same engine as the screens (balanced ledger, period locks,
  no negative cash/bank), so the Autopilot cannot do anything a treasurer could not.
- Everything it does is listed in shg_agent_actions; money actions can be undone (a reversal,
  never a delete). An undone loan or member is left alone by later runs.
- What it cannot decide (loan above the limit, missing ID, not enough cash, unreadable line)
  becomes one "Needs Attention" item, which resolves itself once the cause is gone.
- One run per group at a time (advisory lock); runs come from a background thread, an external
  cron (POST /agent/cron with SHG_AGENT_CRON_SECRET) or the app.
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

import psycopg
from flask import g, request

from bachatgat_accounts_api import close_books, refresh_reminders, reverse_txn
from bachatgat_api import generate_dues_for, record_savings_deposit
from bachatgat_common import (
    MOBILE_RE,
    ApiError,
    account_balance,
    bachatgat_bp,
    body,
    camel,
    choice,
    formatted_number,
    jsonable,
    load_bank_account,
    load_group,
    load_loan,
    load_member,
    load_policy,
    next_number,
    notify,
    ok,
    page_args,
    parse_int,
    parse_money,
    record_event,
    text,
    tx,
    use_resolution,
)
from bachatgat_finance import ZERO, FinanceError, add_period, money
from bachatgat_gateway import _gateway_call, _group_creds, create_payment_link, sync_group
from bachatgat_loans_api import (
    OPEN_LOAN_STATUSES,
    _eligibility,
    apply_loan_payment,
    assess_penalties,
    disburse,
    quote_for_loan,
    sync_loan_status,
)

LOCK_NAMESPACE = 7391
MAX_LINKS_PER_RUN = 25
CLOSE_BOOKS_FROM_DAY = 7
LINK_DUE_SOON_DAYS = 3
MAX_SCAN_BYTES = 3 * 1024 * 1024
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SETTING_FLAGS = ("enabled", "auto_members", "auto_dues", "auto_payment_links", "auto_loans",
                 "auto_disburse", "auto_meetings", "auto_close_books")
# Done actions a person can undo, and what undo means for each (see undo_action).
UNDOABLE = ("SAVINGS_RECORDED", "REPAYMENT_RECORDED", "ONLINE_PAYMENT_POSTED", "LOAN_DISBURSED", "LOAN_APPROVED",
            "LOAN_REQUESTED", "MEMBER_APPROVED", "MEMBER_ADDED", "MEETING_SCHEDULED")
STEP_LABELS = {
    "members": "Member approvals", "dues": "Savings dues", "gateway": "Online payments", "health": "Loan status",
    "loans": "Loan processing", "links": "Payment links", "meetings": "Meetings", "reminders": "Reminders",
    "close": "Closing the books",
}


def rs(value):
    v = money(value)
    return "₹" + (f"{v:,.0f}" if v == v.to_integral_value() else f"{v:,.2f}")


def _error_text(err):
    if isinstance(err, ApiError):
        return err.message
    if isinstance(err, FinanceError):
        return str(err)
    print(f"Autopilot error: {err!r}")
    return "the database rejected the change"


# ---------------------------------------------------------------------------
# Settings and the run log
# ---------------------------------------------------------------------------
def load_settings(cur, group_id):
    cur.execute("INSERT INTO shg_agent_settings (group_id) VALUES (%s) ON CONFLICT DO NOTHING", (group_id,))
    cur.execute("SELECT * FROM shg_agent_settings WHERE group_id = %s", (group_id,))
    return cur.fetchone()


def effective_loan_limit(settings, policy):
    if settings["auto_loan_limit"] is not None:
        return money(settings["auto_loan_limit"])
    return money(policy["loan_approval_limit"])


class Run:
    def __init__(self, cur, group, trigger):
        self.cur, self.group, self.gid = cur, group, group["id"]
        cur.execute("INSERT INTO shg_agent_runs (group_id, trigger) VALUES (%s, %s) RETURNING id", (self.gid, trigger))
        self.id = cur.fetchone()["id"]
        self.seen = set()
        self.keep_prefixes = {"scan"}  # register lines stay until a person dismisses them
        self.counts = {"Done": 0, "Needs Attention": 0, "Failed": 0}

    def _insert(self, status, action_type, message, entity_type, entity_id, txn_id, amount, details, key):
        self.cur.execute(
            """INSERT INTO shg_agent_actions (group_id, run_id, action_type, status, message, entity_type, entity_id,
                   txn_id, amount, details, dedupe_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (group_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO UPDATE SET
                   status = EXCLUDED.status, message = EXCLUDED.message, run_id = EXCLUDED.run_id,
                   action_type = EXCLUDED.action_type, amount = EXCLUDED.amount, details = EXCLUDED.details,
                   updated_at = NOW()
               RETURNING id""",
            (self.gid, self.id, action_type, status, message[:500], entity_type, entity_id, txn_id,
             money(amount) if amount is not None else None, json.dumps(jsonable(details or {})), key),
        )
        self.counts[status] = self.counts.get(status, 0) + 1

    def done(self, action_type, message, entity_type=None, entity_id=None, txn_id=None, amount=None, details=None):
        self._insert("Done", action_type, message, entity_type, entity_id, txn_id, amount, details, None)

    def attention(self, key, action_type, message, entity_type=None, entity_id=None, amount=None, details=None):
        self.seen.add(key)
        self._insert("Needs Attention", action_type, message, entity_type, entity_id, None, amount, details, key)

    def guard(self, key, label, fn, *args):
        """Runs one item in a savepoint; a failure becomes a Needs Attention item instead of
        stopping the run. Returns fn's result, or None if it failed."""
        try:
            with self.cur.connection.transaction():
                return fn(*args)
        except (ApiError, FinanceError, psycopg.Error) as e:
            self.attention(key, "AGENT_ERROR", f"{label}: {_error_text(e)}")
            return None

    def step(self, name, fn, *args):
        try:
            with self.cur.connection.transaction():
                fn(self, *args)
        except (ApiError, FinanceError, psycopg.Error) as e:
            self.keep_prefixes.add(name)  # its open items were not re-checked: keep them
            self.attention(f"{name}:failed", "STEP_FAILED", f"{STEP_LABELS[name]}: {_error_text(e)}")

    def finish(self):
        self.cur.execute(
            """UPDATE shg_agent_actions SET status = 'Resolved', updated_at = NOW()
               WHERE group_id = %s AND status = 'Needs Attention' AND dedupe_key IS NOT NULL
                 AND NOT (dedupe_key = ANY(%s::text[])) AND NOT (split_part(dedupe_key, ':', 1) = ANY(%s::text[]))""",
            (self.gid, list(self.seen), list(self.keep_prefixes)),
        )
        self.cur.execute(
            """UPDATE shg_agent_runs SET finished_at = NOW(), done_count = %s, attention_count = %s, failed_count = %s
               WHERE id = %s""",
            (self.counts["Done"], self.counts["Needs Attention"], self.counts["Failed"], self.id),
        )
        self.cur.execute("UPDATE shg_agent_settings SET last_run_at = NOW() WHERE group_id = %s", (self.gid,))

    def summary(self):
        self.cur.execute("SELECT * FROM shg_agent_runs WHERE id = %s", (self.id,))
        run = camel(self.cur.fetchone())
        self.cur.execute(
            "SELECT * FROM shg_agent_actions WHERE run_id = %s AND status IN ('Done', 'Needs Attention') ORDER BY id",
            (self.id,),
        )
        run["actions"] = [_action_out(r) for r in self.cur.fetchall()]
        return run


def _action_out(row):
    out = camel(row)
    out["canUndo"] = row["status"] == "Done" and row["action_type"] in UNDOABLE
    out["canApprove"] = row["status"] == "Needs Attention" and row["action_type"] == "LOAN_NEEDS_APPROVAL"
    return out


def _held(cur, group_id, entity_type):
    """Records whose latest Autopilot action was undone: a person took them over."""
    cur.execute(
        """SELECT entity_id FROM (
               SELECT DISTINCT ON (entity_id) entity_id, status FROM shg_agent_actions
               WHERE group_id = %s AND entity_type = %s AND status IN ('Done', 'Undone')
               ORDER BY entity_id, id DESC) x
           WHERE status = 'Undone'""",
        (group_id, entity_type),
    )
    return {r["entity_id"] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def step_members(run, settings):
    if not settings["auto_members"]:
        return
    cur = run.cur
    held = _held(cur, run.gid, "MEMBER")
    cur.execute("SELECT * FROM shg_members WHERE group_id = %s AND status = 'Pending' ORDER BY member_number FOR UPDATE",
                (run.gid,))
    for m in cur.fetchall():
        if m["id"] in held:
            continue
        who = f"{m['full_name']} (#{m['member_number']})"
        verification = m["id_verification_status"]
        if verification == "Rejected":
            run.attention(f"members:rejected:{m['id']}", "MEMBER_ID_REJECTED",
                          f"{who}: her ID proof was rejected - correct it and verify her by hand.", "MEMBER", m["id"])
            continue
        if verification not in ("Verified", "Not Required"):
            if not (m["id_proof_type"] and m["id_proof_last4"]):
                run.attention(f"members:id:{m['id']}", "MEMBER_NEEDS_ID",
                              f"{who}: add her ID proof type and number; the Autopilot then approves her.", "MEMBER", m["id"])
                continue
            cur.execute("UPDATE shg_members SET id_verification_status = 'Verified', version = version + 1 WHERE id = %s", (m["id"],))
            record_event(cur, run.gid, "MEMBER", m["id"], "VERIFY", verification, "Verified",
                         {"remarks": "Autopilot: ID proof details recorded", "by": "Autopilot"})
        cur.execute("UPDATE shg_members SET status = 'Active', version = version + 1, updated_at = NOW() WHERE id = %s", (m["id"],))
        record_event(cur, run.gid, "MEMBER", m["id"], "APPROVE", "Pending", "Active", {"by": "Autopilot"})
        run.done("MEMBER_APPROVED", f"Approved {who} as a member", "MEMBER", m["id"])


def step_dues(run, settings, policy):
    if not settings["auto_dues"]:
        return
    if money(policy["savings_amount"]) <= 0:
        run.attention("dues:policy", "POLICY_MISSING",
                      "Set the savings amount in Settings > Group policy so the Autopilot can raise savings dues.")
        return
    created = generate_dues_for(run.cur, run.gid, date.today())
    if created:
        run.done("DUES_GENERATED", f"Raised {created} savings due(s) for the current period", "GROUP", run.gid)


def step_gateway(run):
    cur = run.cur
    if not _group_creds(cur, run.gid):
        return
    result = sync_group(cur, run.group, force=True)
    for err in result.get("errors") or []:
        run.attention("gateway:sync", "GATEWAY_ERROR", f"Razorpay: {err}")
    # Rows inserted in this database transaction carry its start time.
    cur.execute(
        """SELECT gp.*, m.full_name, t.receipt_number FROM shg_gateway_payments gp
           JOIN shg_members m ON m.id = gp.member_id LEFT JOIN shg_transactions t ON t.id = gp.txn_id
           WHERE gp.group_id = %s AND gp.created_at = NOW() AND gp.status = 'Posted' ORDER BY gp.id""",
        (run.gid,),
    )
    for p in cur.fetchall():
        what = "loan repayment" if p["loan_id"] else "savings"
        run.done("ONLINE_PAYMENT_POSTED", f"{rs(p['amount'])} {what} received online from {p['full_name']} (receipt {p['receipt_number']})",
                 "GATEWAY_PAYMENT", p["id"], p["txn_id"], p["amount"])
    cur.execute(
        """SELECT gp.*, m.full_name FROM shg_gateway_payments gp JOIN shg_members m ON m.id = gp.member_id
           WHERE gp.group_id = %s AND gp.status = 'Unposted' ORDER BY gp.id""",
        (run.gid,),
    )
    for p in cur.fetchall():
        run.attention(f"gateway:unposted:{p['id']}", "PAYMENT_NOT_POSTED",
                      f"{rs(p['amount'])} paid online by {p['full_name']} could not be posted: {p['note']}",
                      "GATEWAY_PAYMENT", p["id"], p["amount"])


def step_loan_health(run):
    cur = run.cur
    today = date.today()
    cur.execute("SELECT * FROM shg_loans WHERE group_id = %s AND status IN ('Active', 'Overdue', 'Disbursed') ORDER BY id",
                (run.gid,))
    for loan in cur.fetchall():
        assess_penalties(cur, loan, today)
        status = sync_loan_status(cur, loan["id"], today)
        if status != loan["status"]:
            run.done(f"LOAN_{status.upper()}", f"Loan {loan['application_number']} is now {status}", "LOAN", loan["id"])


def _set_loan_status(cur, loan, to_status, action, extra=None):
    sets = {"status": to_status, **(extra or {})}
    cur.execute(
        f"UPDATE shg_loans SET {', '.join(f'{k} = %s' for k in sets)}, version = version + 1, updated_at = NOW() WHERE id = %s",
        list(sets.values()) + [loan["id"]],
    )
    record_event(cur, loan["group_id"], "LOAN", loan["id"], action, loan["status"], to_status, {"by": "Autopilot", **(extra or {})})
    loan = {**loan, **sets}
    return loan


def approve_loan(cur, loan, approved, resolution_id, remarks, by):
    loan = _set_loan_status(cur, loan, "Approved", "APPROVE", {
        "approved_amount": approved, "resolution_id": resolution_id,
        "review_remarks": loan.get("review_remarks") or remarks,
    })
    record_event(cur, loan["group_id"], "LOAN", loan["id"], "APPROVED_BY", details={"by": by, "remarks": remarks})
    notify(cur, loan["group_id"], "LOAN_STATUS", "Loan approved",
           f"Loan application {loan['application_number']} is now Approved.", f"loan-{loan['id']}-Approved", "LOAN", loan["id"])
    return loan


def _process_loan(run, loan_id, settings, policy, limit):
    cur = run.cur
    loan = load_loan(cur, loan_id, lock=True)
    member = load_member(cur, loan["member_id"])
    number, who = loan["application_number"], member["full_name"]
    if loan["status"] in ("Draft", "Submitted", "Under Review"):
        if member["status"] != "Active":
            run.attention(f"loans:member:{loan_id}", "LOAN_MEMBER_INACTIVE",
                          f"Loan {number}: {who} is {member['status']}; reject or withdraw the application.", "LOAN", loan_id)
            return
        requested = money(loan["requested_amount"])
        problems = _eligibility(cur, loan, member, policy, requested, loan["installments"])
        if problems:
            run.attention(f"loans:ineligible:{loan_id}", "LOAN_NOT_ELIGIBLE",
                          f"Loan {number} for {who} ({rs(requested)}) is not eligible: {'; '.join(problems)}", "LOAN", loan_id, requested)
            return
        if loan["status"] == "Draft":
            loan = _set_loan_status(cur, loan, "Submitted", "SUBMIT")
        if loan["status"] == "Submitted":
            savings = account_balance(cur, run.gid, "MEMBER_SAVINGS", member_id=member["id"])
            loan = _set_loan_status(cur, loan, "Under Review", "REVIEW", {
                "review_remarks": f"Autopilot: eligibility checked against the group policy (savings {rs(abs(savings))})."})
        cur.execute(
            """SELECT r.id FROM shg_resolutions r JOIN shg_meetings mt ON mt.id = r.meeting_id
               WHERE r.loan_id = %s AND r.resolution_type = 'LOAN_APPROVAL' AND r.result = 'Passed'
                 AND r.consumed_by_type IS NULL AND mt.status IN ('Held', 'Minutes Approved') AND r.amount >= %s
               ORDER BY r.id LIMIT 1""",
            (loan_id, requested),
        )
        resolution = cur.fetchone()
        policy_limit = money(policy["loan_approval_limit"])
        if resolution:
            use_resolution(cur, run.gid, resolution["id"], "LOAN_APPROVAL", "LOAN", loan_id, loan_id=loan_id,
                           amount=requested, supermajority=policy_limit > 0 and requested > policy_limit)
            loan = approve_loan(cur, loan, requested, resolution["id"], "Approved under the meeting's resolution", "Autopilot")
        elif limit > 0 and requested <= limit:
            loan = approve_loan(cur, loan, requested, None,
                                f"Auto-approved by the Autopilot: within the {rs(limit)} limit and all eligibility checks passed",
                                "Autopilot")
        else:
            reason = f"above the Autopilot limit of {rs(limit)}" if limit > 0 else "the Autopilot loan limit is not set"
            run.attention(f"loans:approval:{loan_id}", "LOAN_NEEDS_APPROVAL",
                          f"Loan {number}: {who} asks for {rs(requested)}; {reason}. Tap Approve, or pass a resolution in the meeting.",
                          "LOAN", loan_id, requested)
            return
        run.done("LOAN_APPROVED", f"Approved loan {number} of {rs(requested)} for {who}", "LOAN", loan_id, amount=requested)
    if loan["status"] == "Approved":
        _disburse_loan(run, settings, policy, loan, member)


def _disburse_loan(run, settings, policy, loan, member):
    cur = run.cur
    number, amount = loan["application_number"], money(loan["approved_amount"])
    if not settings["auto_disburse"]:
        run.attention(f"loans:disburse:{loan['id']}", "LOAN_AWAITING_DISBURSAL",
                      f"Loan {number} ({rs(amount)}) is approved; disburse it from the loan screen or turn on auto-disbursal.",
                      "LOAN", loan["id"], amount)
        return
    if member["status"] != "Active":
        run.attention(f"loans:member:{loan['id']}", "LOAN_MEMBER_INACTIVE",
                      f"Loan {number}: {member['full_name']} is {member['status']}; it was not disbursed.", "LOAN", loan["id"])
        return
    cur.execute(
        """SELECT id FROM shg_members WHERE group_id = %s AND status = 'Active' AND role IN ('Treasurer', 'President') AND id <> %s
           ORDER BY CASE role WHEN 'Treasurer' THEN 0 ELSE 1 END LIMIT 1""",
        (run.gid, member["id"]),
    )
    auth = cur.fetchone()
    if not auth:
        run.attention(f"loans:authorizer:{loan['id']}", "LOAN_NO_AUTHORIZER",
                      f"Loan {number}: the group needs an active Treasurer or President (other than the borrower) to authorize disbursements.",
                      "LOAN", loan["id"])
        return
    method = settings["disburse_method"]
    bank = None
    if method == "Cash":
        available, where = account_balance(cur, run.gid, "CASH"), "cash in hand"
    else:
        bank = load_bank_account(cur, run.gid, None)
        available, where = account_balance(cur, run.gid, "BANK", bank_account_id=bank["id"]), "in the bank"
    if available < amount:
        run.attention(f"loans:funds:{loan['id']}", "LOAN_WAITING_FOR_FUNDS",
                      f"Loan {number} for {member['full_name']} needs {rs(amount)} but there is {rs(available)} {where}. "
                      "It is paid out automatically once the money is there.", "LOAN", loan["id"], amount)
        return
    today = date.today()
    first_due = add_period(today, loan["frequency"], 1)
    txn, rows = disburse(cur, run.group, policy, loan, auth["id"], method, today, first_due,
                         bank["id"] if bank else None, None, by="Autopilot")
    run.done("LOAN_DISBURSED",
             f"Disbursed {rs(amount)} to {member['full_name']} ({method}) - loan {number}, {len(rows)} installments, "
             f"first due {first_due.strftime('%d %b %Y')}", "LOAN", loan["id"], txn["id"], amount)


def step_loans(run, settings, policy):
    if not settings["auto_loans"]:
        return
    cur = run.cur
    limit = effective_loan_limit(settings, policy)
    held = _held(cur, run.gid, "LOAN")
    cur.execute(
        """SELECT id, application_number FROM shg_loans WHERE group_id = %s
           AND status IN ('Draft', 'Submitted', 'Under Review', 'Approved') ORDER BY id""",
        (run.gid,),
    )
    for row in cur.fetchall():
        if row["id"] in held:
            continue
        run.guard(f"loans:error:{row['id']}", f"Loan {row['application_number']}", _process_loan, run, row["id"], settings, policy, limit)


def _wanted_links(run):
    """What each member should be asked to pay online now: overdue savings, and loan
    installments that are due (or fall due within a few days)."""
    cur = run.cur
    today = date.today()
    wanted = {}
    cur.execute(
        """SELECT d.member_id, SUM(d.due_amount - d.paid_amount) AS due FROM shg_savings_dues d
           JOIN shg_members m ON m.id = d.member_id
           WHERE d.group_id = %s AND m.status = 'Active' AND d.status IN ('Pending', 'Partial') AND d.due_date <= %s
           GROUP BY d.member_id""",
        (run.gid, today),
    )
    for r in cur.fetchall():
        if money(r["due"]) > 0:
            wanted[("S", r["member_id"])] = money(r["due"])
    cur.execute(
        """SELECT l.id, l.member_id FROM shg_loans l JOIN shg_members m ON m.id = l.member_id
           WHERE l.group_id = %s AND m.status = 'Active' AND l.status IN ('Active', 'Overdue')""",
        (run.gid,),
    )
    for loan in cur.fetchall():
        q = quote_for_loan(cur, loan["id"], today)
        amount = money(q["dueNow"])
        nxt = q["nextInstallment"]
        if amount <= 0 and nxt and nxt["dueDate"] <= today + timedelta(days=LINK_DUE_SOON_DAYS):
            amount = money(nxt["amount"])
        if amount > 0:
            wanted[("L", loan["id"])] = amount
    return wanted


def step_links(run, settings):
    if not settings["auto_payment_links"]:
        return
    cur = run.cur
    creds = _group_creds(cur, run.gid)
    if not creds:
        return
    wanted = _wanted_links(run)
    cur.execute("SELECT * FROM shg_payment_links WHERE group_id = %s AND status = 'Open' AND mode = %s", (run.gid, creds[2]))
    for link in cur.fetchall():
        key = ("L", link["loan_id"]) if link["loan_id"] else ("S", link["member_id"])
        if key in wanted and money(wanted[key]) == money(link["amount"]):
            del wanted[key]  # the member already has the right link
            continue

        def cancel(link=link):
            _gateway_call("POST", f"/payment_links/{link['link_id']}/cancel", {}, creds=creds)
            cur.execute("UPDATE shg_payment_links SET status = 'Cancelled' WHERE id = %s", (link["id"],))

        run.guard("links:cancel", "Cancelling an old payment link", cancel)
    budget = MAX_LINKS_PER_RUN
    for (kind, ref), amount in wanted.items():
        if budget <= 0:
            break
        budget -= 1
        if kind == "L":
            loan = load_loan(cur, ref)
            member = load_member(cur, loan["member_id"])
        else:
            loan, member = None, load_member(cur, ref)

        def send(member=member, loan=loan, amount=amount):
            row = create_payment_link(cur, run.group, member, amount, creds, loan=loan, sms=True)
            what = f"loan {loan['application_number']} installment" if loan else "savings due"
            run.done("PAYMENT_LINK_SENT", f"Sent {member['full_name']} a payment link (SMS) for {rs(amount)} - {what}",
                     "MEMBER", member["id"], amount=amount, details={"linkId": row["id"], "url": row["short_url"]})

        run.guard("links:send", f"Payment link for {member['full_name']}", send)


def step_meetings(run, settings):
    if not settings["auto_meetings"]:
        return
    cur, group = run.cur, run.group
    today = date.today()
    cur.execute("SELECT 1 FROM shg_meetings WHERE group_id = %s AND status = 'Scheduled' AND meeting_date >= %s LIMIT 1",
                (run.gid, today))
    if not cur.fetchone():
        cur.execute("SELECT MAX(meeting_date) AS last FROM shg_meetings WHERE group_id = %s AND status <> 'Cancelled'", (run.gid,))
        last = cur.fetchone()["last"]
        freq = group["meeting_frequency"]
        nxt = add_period(last, freq, 1) if last else today + timedelta(days=7)
        while nxt <= today:
            nxt = add_period(nxt, freq, 1)
        cur.execute("SELECT COUNT(*) AS n FROM shg_loans WHERE group_id = %s AND status IN ('Submitted', 'Under Review')", (run.gid,))
        pending = cur.fetchone()["n"]
        agenda = "1. Attendance\n2. Savings collection\n3. Loan repayments\n" + \
                 (f"4. Loan applications ({pending})\n5. Any other matter" if pending else "4. Any other matter")
        number = next_number(cur, run.gid, "MEETING")
        cur.execute(
            "INSERT INTO shg_meetings (group_id, meeting_number, meeting_date, agenda) VALUES (%s, %s, %s, %s) RETURNING id",
            (run.gid, number, nxt, agenda),
        )
        meeting_id = cur.fetchone()["id"]
        record_event(cur, run.gid, "MEETING", meeting_id, "SCHEDULED", None, "Scheduled", {"date": nxt, "by": "Autopilot"})
        notify(cur, run.gid, "MEETING_SCHEDULED", "Meeting scheduled",
               f"Meeting #{number} of {group['name_en']} is on {nxt.strftime('%d %b %Y')}.",
               f"meeting-{meeting_id}-scheduled", "MEETING", meeting_id)
        run.done("MEETING_SCHEDULED", f"Scheduled meeting #{number} on {nxt.strftime('%d %b %Y')}", "MEETING", meeting_id)
    cur.execute(
        "SELECT id, meeting_number, meeting_date FROM shg_meetings WHERE group_id = %s AND status = 'Scheduled' AND meeting_date < %s",
        (run.gid, today),
    )
    for m in cur.fetchall():
        run.attention(f"meetings:register:{m['id']}", "MEETING_NEEDS_REGISTER",
                      f"Meeting #{m['meeting_number']} of {m['meeting_date'].strftime('%d %b %Y')}: send the register photo "
                      "so the Autopilot records attendance and collections (or cancel the meeting).", "MEETING", m["id"])


def step_reminders(run):
    refresh_reminders(run.cur)


def step_close(run, settings):
    if not settings["auto_close_books"]:
        return
    cur, group = run.cur, run.group
    today = date.today()
    if today.day < CLOSE_BOOKS_FROM_DAY:
        return
    period_end = today.replace(day=1) - timedelta(days=1)
    if period_end < group["formation_date"]:
        return
    cur.execute("SELECT MAX(period_end) AS last FROM shg_period_closes WHERE group_id = %s", (run.gid,))
    last = cur.fetchone()["last"]
    if last and last >= period_end:
        return
    cur.execute(
        "SELECT COUNT(*) AS n FROM shg_gateway_payments WHERE group_id = %s AND status = 'Unposted' AND paid_at < %s",
        (run.gid, today.replace(day=1)),
    )
    if cur.fetchone()["n"]:
        run.attention("close:unposted", "BOOKS_NOT_CLOSED",
                      f"{period_end.strftime('%B %Y')} was not closed: some online payments of that month are not posted yet.")
        return
    close_books(cur, group, period_end, "Closed automatically by the Autopilot")
    run.done("BOOKS_CLOSED", f"Closed the books for {period_end.strftime('%B %Y')}", "GROUP", run.gid)


def run_group(group_id, trigger, stale_hours=None, prelude=None):
    """One Autopilot run for a group (g.owner_id must be the group's owner). Returns the run
    summary, or None when another run holds the group or (with stale_hours) it ran recently."""
    with tx() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s, %s) AS ok", (LOCK_NAMESPACE, int(group_id)))
        if not cur.fetchone()["ok"]:
            if trigger in ("SCHEDULE",):
                return None
            raise ApiError(409, "The Autopilot is already working on this group; try again in a minute")
        group = load_group(cur, group_id)
        settings = load_settings(cur, group_id)
        if stale_hours is not None and settings["last_run_at"] and \
                settings["last_run_at"] > datetime.now(timezone.utc) - timedelta(hours=stale_hours):
            return None
        run = Run(cur, group, trigger)
        if prelude:
            prelude(run)
        if not settings["enabled"]:
            pass  # a register photo is still posted; the autonomous steps stay off
        elif group["status"] != "Active":
            run.attention("group:inactive", "GROUP_INACTIVE",
                          f"The group is {group['status']}; the Autopilot works only for active groups.", "GROUP", group_id)
        else:
            policy = load_policy(cur, group_id)
            run.step("members", step_members, settings)
            run.step("dues", step_dues, settings, policy)
            run.step("gateway", step_gateway)
            run.step("health", step_loan_health)
            run.step("loans", step_loans, settings, policy)
            run.step("links", step_links, settings)
            run.step("meetings", step_meetings, settings)
            run.step("reminders", step_reminders)
            run.step("close", step_close, settings)
        run.finish()
        return run.summary()


def run_due_groups():
    """Runs every enabled active group that has not run for SHG_AUTOPILOT_EVERY_HOURS."""
    hours = float(os.getenv("SHG_AUTOPILOT_EVERY_HOURS", "6"))
    with tx() as cur:
        cur.execute(
            """SELECT gr.id, gr.owner_id FROM shg_groups gr JOIN userdetails u ON u.id = gr.owner_id
               LEFT JOIN shg_agent_settings s ON s.group_id = gr.id
               WHERE gr.status = 'Active' AND u.status = 'active' AND COALESCE(s.enabled, TRUE)
                 AND (s.last_run_at IS NULL OR s.last_run_at < NOW() - make_interval(secs => %s))
               ORDER BY gr.id LIMIT 200""",
            (hours * 3600,),
        )
        groups = cur.fetchall()
    ran = 0
    for row in groups:
        g.owner_id = row["owner_id"]
        try:
            if run_group(row["id"], "SCHEDULE", stale_hours=hours):
                ran += 1
        except Exception as e:  # one group's problem must not stop the others
            print(f"Autopilot: group {row['id']} failed: {e!r}")
    return {"due": len(groups), "ran": ran}


def start_autopilot(app):
    """Background loop (one per worker; the per-group lock and staleness check stop double runs).
    A sleeping free-tier server does not run it - point an external cron at /agent/cron for that."""
    if os.getenv("SHG_AUTOPILOT", "on").strip().lower() in ("off", "0", "false", "no"):
        return
    interval = max(5, int(os.getenv("SHG_AUTOPILOT_INTERVAL_MIN", "30"))) * 60

    def loop():
        time.sleep(90)
        while True:
            try:
                with app.test_request_context("/api/bachatgat/agent/background"):
                    run_due_groups()
            except Exception as e:
                print(f"Autopilot loop error: {e!r}")
            time.sleep(interval)

    threading.Thread(target=loop, name="shg-autopilot", daemon=True).start()


# ---------------------------------------------------------------------------
# Meeting register photo
# ---------------------------------------------------------------------------
SCAN_PROMPT = """You read photos of the meeting register of a Mahila Bachat Gat (an Indian women's self-help group).
Registers are handwritten or printed, in Marathi, Hindi or English, usually one row per member with columns such as
attendance/signature, savings (bachat/बचत), loan repayment (karj parat/कर्ज परतफेड/हप्ता), interest and new loan requests.

Reply with ONLY this JSON:
{"meetingDate": "YYYY-MM-DD" or null,
 "rows": [{"memberNumber": number or null, "nameAsWritten": "text", "present": true | false | null,
           "savings": number or null, "loanRepayment": number or null,
           "loanRequestAmount": number or null, "loanPurpose": "text" or null, "loanInstallments": number or null}],
 "newMembers": [{"name": "text", "mobile": "text" or null}],
 "unreadable": ["short description of each line or cell you could not read"]}

Rules:
- The group's members are listed below as "number. name". Set memberNumber only when the row clearly is that member
  (spellings differ between scripts and short forms are common). If unsure, use null.
- Amounts are rupees as plain numbers. Loan repayment = principal plus interest paid in that meeting.
  A blank or dash cell is null. Never guess a number you cannot read: leave it null and mention it in "unreadable".
- present: true for a tick, "P", signature or thumb impression; false for "A" or absent; null if there is no attendance.
- newMembers: only people the page explicitly records as joining the group.
- Dates are written day/month/year.
"""


def _read_register(image_b64, mime, roster, api_key):
    url = GEMINI_API_URL.format(model=GEMINI_MODEL) + f"?key={api_key}"
    payload = {
        "system_instruction": {"parts": [{"text": SCAN_PROMPT}]},
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": mime, "data": image_b64}},
            {"text": "Group members:\n" + (roster or "(none yet)") + f"\n\nToday is {date.today().isoformat()}. Read this register page."},
        ]}],
        "generationConfig": {"maxOutputTokens": 8192, "temperature": 0.1, "responseMimeType": "application/json",
                             "thinkingConfig": {"thinkingBudget": 1024}},
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    parts = ((result.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    raw = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):]
    data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def _num(value):
    try:
        v = money(value)
    except (FinanceError, ValueError, TypeError, ArithmeticError):
        return None
    return v if v > 0 else None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scan_date(raw, group, cur):
    today = date.today()
    try:
        d = date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        d = None
    on = d if d and group["formation_date"] <= d <= today else today
    cur.execute("SELECT MAX(period_end) AS closed FROM shg_period_closes WHERE group_id = %s", (group["id"],))
    closed = cur.fetchone()["closed"]
    if closed and on <= closed:
        on = today  # that month's books are closed: post it today
    return on


def _record_attendance(run, on, marks):
    cur = run.cur
    cur.execute(
        """SELECT * FROM shg_meetings WHERE group_id = %s AND status IN ('Scheduled', 'Held')
             AND meeting_date BETWEEN %s AND %s
           ORDER BY (meeting_date = %s) DESC, ABS(meeting_date - %s), id LIMIT 1 FOR UPDATE""",
        (run.gid, on - timedelta(days=7), date.today(), on, on),
    )
    meeting = cur.fetchone()
    if meeting and meeting["status"] == "Scheduled" and meeting["meeting_date"] != on:
        cur.execute("UPDATE shg_meetings SET meeting_date = %s WHERE id = %s", (on, meeting["id"]))
    if not meeting:
        number = next_number(cur, run.gid, "MEETING")
        cur.execute(
            "INSERT INTO shg_meetings (group_id, meeting_number, meeting_date, agenda) VALUES (%s, %s, %s, %s) RETURNING *",
            (run.gid, number, on, "Recorded from the meeting register photo"),
        )
        meeting = cur.fetchone()
        record_event(cur, run.gid, "MEETING", meeting["id"], "SCHEDULED", None, "Scheduled", {"date": on, "by": "Autopilot"})
    for member_id, present in marks:
        cur.execute(
            """INSERT INTO shg_meeting_attendance (meeting_id, member_id, present) VALUES (%s, %s, %s)
               ON CONFLICT (meeting_id, member_id) DO UPDATE SET present = EXCLUDED.present""",
            (meeting["id"], member_id, present),
        )
    if meeting["status"] == "Scheduled":
        cur.execute("UPDATE shg_meetings SET status = 'Held', updated_at = NOW() WHERE id = %s", (meeting["id"],))
        record_event(cur, run.gid, "MEETING", meeting["id"], "HELD", "Scheduled", "Held", {"by": "Autopilot"})
    record_event(cur, run.gid, "MEETING", meeting["id"], "ATTENDANCE_MARKED", details={"entries": len(marks), "by": "Autopilot"})
    present = sum(1 for _m, p in marks if p)
    run.done("ATTENDANCE_RECORDED",
             f"Meeting #{meeting['meeting_number']} ({on.strftime('%d %b %Y')}): {present} present, {len(marks) - present} absent",
             "MEETING", meeting["id"])
    return meeting["id"]


def _scan_savings(run, scan_id, member, amount, on):
    if member["status"] != "Active":
        raise ApiError(409, f"{member['full_name']} is {member['status']}, so her savings were not recorded")
    txn, _alloc, _vol = record_savings_deposit(run.cur, run.group, member, amount, on, "Cash",
                                               narration="Savings from the meeting register (photo)",
                                               reference_number=f"SCAN-{scan_id}")
    run.done("SAVINGS_RECORDED", f"Savings {rs(amount)} from {member['full_name']} (receipt {txn['receipt_number']})",
             "MEMBER", member["id"], txn["id"], amount)


def _scan_repayment(run, scan_id, member, amount, on):
    cur = run.cur
    cur.execute(
        """SELECT id, application_number FROM shg_loans WHERE member_id = %s AND status IN ('Active', 'Overdue', 'Disbursed')
           ORDER BY (status = 'Overdue') DESC, id LIMIT 1""",
        (member["id"],),
    )
    loan = cur.fetchone()
    if not loan:
        raise ApiError(409, f"{member['full_name']} has no running loan for the {rs(amount)} repayment")
    txn = apply_loan_payment(cur, run.group, loan["id"], amount, on, "Cash", None, f"SCAN-{scan_id}",
                             "Repayment from the meeting register (photo)")
    run.done("REPAYMENT_RECORDED", f"Repayment {rs(amount)} from {member['full_name']} on loan {loan['application_number']} "
             f"(receipt {txn['receipt_number']})", "LOAN", loan["id"], txn["id"], amount)


def _scan_loan_request(run, member, amount, purpose, installments, on):
    cur = run.cur
    if member["status"] != "Active":
        raise ApiError(409, f"{member['full_name']} is {member['status']}; only active members can apply for loans")
    policy = load_policy(cur, run.gid)
    count = max(1, min(_int(installments) or min(12, policy["max_installments"]), policy["max_installments"]))
    number = formatted_number(cur, run.group, "LOAN", "LN", on)
    cur.execute(
        """INSERT INTO shg_loans (group_id, member_id, application_number, application_date, requested_amount,
               purpose, interest_method, interest_rate, installments, frequency, remarks)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (run.gid, member["id"], number, on, amount, (purpose or "As written in the meeting register")[:200],
         policy["default_interest_method"], policy["default_interest_rate"], count, policy["default_frequency"],
         "Created by the Autopilot from the meeting register photo"),
    )
    loan_id = cur.fetchone()["id"]
    record_event(cur, run.gid, "LOAN", loan_id, "CREATED", None, "Draft", {"amount": amount, "by": "Autopilot"})
    run.done("LOAN_REQUESTED", f"Loan application {number}: {member['full_name']} asks for {rs(amount)} ({count} installments)",
             "LOAN", loan_id, amount=amount)


def _scan_new_member(run, name, mobile_number, on):
    cur = run.cur
    cur.execute("SELECT COALESCE(MAX(member_number), 0) + 1 AS n FROM shg_members WHERE group_id = %s", (run.gid,))
    number = cur.fetchone()["n"]
    cur.execute(
        """INSERT INTO shg_members (owner_id, group_id, member_number, full_name, mobile, joining_date)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (g.owner_id, run.gid, number, name[:150], mobile_number, on),
    )
    member_id = cur.fetchone()["id"]
    record_event(cur, run.gid, "MEMBER", member_id, "REGISTERED", None, "Pending", {"memberNumber": number, "by": "Autopilot"})
    run.done("MEMBER_ADDED", f"Registered new member {name} (#{number}) from the register", "MEMBER", member_id)


def _post_scan(run, scan_id, data):
    cur, group = run.cur, run.group
    on = _scan_date(data.get("meetingDate"), group, cur)
    cur.execute("SELECT * FROM shg_members WHERE group_id = %s", (run.gid,))
    by_number = {m["member_number"]: m for m in cur.fetchall()}
    marks, meeting_id = [], None
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    for i, row in enumerate(rows[:200]):
        if not isinstance(row, dict):
            continue
        label = str(row.get("nameAsWritten") or "?")[:60]
        member = by_number.get(_int(row.get("memberNumber")))
        savings, repayment, request_amt = _num(row.get("savings")), _num(row.get("loanRepayment")), _num(row.get("loanRequestAmount"))
        if not member:
            if savings or repayment or request_amt or row.get("present") is not None:
                run.attention(f"scan:{scan_id}:{i}", "SCAN_UNMATCHED",
                              f"Register line \"{label}\" does not match any member - record it by hand.", details=row)
            continue
        if isinstance(row.get("present"), bool) and member["status"] == "Active":
            marks.append((member["id"], row["present"]))
        if savings:
            run.guard(f"scan:{scan_id}:{i}:sav", f"Savings of {label}", _scan_savings, run, scan_id, member, savings, on)
        if repayment:
            run.guard(f"scan:{scan_id}:{i}:rep", f"Repayment of {label}", _scan_repayment, run, scan_id, member, repayment, on)
        if request_amt:
            run.guard(f"scan:{scan_id}:{i}:loan", f"Loan request of {label}", _scan_loan_request, run, member, request_amt,
                      str(row.get("loanPurpose") or "").strip(), row.get("loanInstallments"), on)
    if marks:
        meeting_id = run.guard(f"scan:{scan_id}:attendance", "Attendance", _record_attendance, run, on, marks)
    new_members = data.get("newMembers") if isinstance(data.get("newMembers"), list) else []
    for j, nm in enumerate(new_members[:20]):
        if not isinstance(nm, dict) or not str(nm.get("name") or "").strip():
            continue
        name = str(nm["name"]).strip()
        digits = "".join(ch for ch in str(nm.get("mobile") or "") if ch.isdigit())[-10:]
        if not MOBILE_RE.match(digits):
            run.attention(f"scan:{scan_id}:new{j}", "SCAN_NEW_MEMBER",
                          f"New member \"{name[:60]}\" in the register has no readable mobile number - add her by hand.")
            continue
        run.guard(f"scan:{scan_id}:new{j}", f"New member {name[:60]}", _scan_new_member, run, name, digits, on)
    for k, line in enumerate((data.get("unreadable") or [])[:20] if isinstance(data.get("unreadable"), list) else []):
        run.attention(f"scan:{scan_id}:unreadable{k}", "SCAN_UNREADABLE", f"Could not read: {str(line)[:300]}")
    return on, meeting_id


@bachatgat_bp.post("/agent/scan")
def agent_scan():
    payload = body()
    group_id = parse_int(payload, "groupId", required=True, label="Group")
    mime = choice(payload, "mimeType", ("image/jpeg", "image/png"), default="image/jpeg", label="Photo type")
    raw = str(payload.get("imageBase64") or "")
    if raw.startswith("data:") and "," in raw[:100]:
        raw = raw.split(",", 1)[1]
    if len(raw) > MAX_SCAN_BYTES * 4 // 3 + 8:
        raise ApiError(413, "Photo is larger than 3 MB; take it again at lower quality")
    try:
        content = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(422, "Photo could not be read")
    if not content or not content.startswith(b"\xff\xd8" if mime == "image/jpeg" else b"\x89PNG"):
        raise ApiError(422, "Photo could not be read")
    sha = hashlib.sha256(content).hexdigest()
    with tx() as cur:
        group = load_group(cur, group_id)
        cur.execute("SELECT created_at FROM shg_agent_scans WHERE group_id = %s AND image_sha256 = %s", (group_id, sha))
        seen = cur.fetchone()
        if seen:
            raise ApiError(409, f"This photo was already posted on {seen['created_at'].strftime('%d %b %Y')}")
        cur.execute(
            "SELECT member_number, full_name FROM shg_members WHERE group_id = %s AND status IN ('Active', 'Pending') ORDER BY member_number",
            (group_id,),
        )
        roster = "\n".join(f"{m['member_number']}. {m['full_name']}" for m in cur.fetchall())
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ApiError(503, "Reading register photos needs GEMINI_API_KEY in the backend configuration")
    try:
        data = _read_register(raw, mime, roster, api_key)
    except urllib.error.HTTPError as e:
        raise ApiError(502, f"The AI service could not read the photo right now (error {e.code}); try again")
    except (urllib.error.URLError, TimeoutError):
        raise ApiError(502, "The AI service did not answer; check the connection and try again")
    except (ValueError, KeyError, IndexError):
        raise ApiError(422, "The register could not be understood; take a clearer, well-lit photo of one page")

    result = {}

    def prelude(run):
        cur = run.cur
        cur.execute(
            """INSERT INTO shg_agent_scans (group_id, image_sha256, run_id, extracted) VALUES (%s, %s, %s, %s)
               ON CONFLICT (group_id, image_sha256) DO NOTHING RETURNING id""",
            (group_id, sha, run.id, json.dumps(jsonable(data))),
        )
        row = cur.fetchone()
        if not row:
            raise ApiError(409, "This photo was already posted")
        on, meeting_id = _post_scan(run, row["id"], data)
        cur.execute(
            """INSERT INTO shg_documents (owner_id, group_id, entity_type, entity_id, document_type, file_name,
                   mime_type, size_bytes, content, uploaded_by)
               VALUES (%s, %s, %s, %s, 'Meeting register photo', %s, %s, %s, %s, %s) RETURNING id""",
            (g.owner_id, group_id, "MEETING" if meeting_id else "GROUP", meeting_id or group_id,
             f"register-{on.isoformat()}.{'jpg' if mime == 'image/jpeg' else 'png'}", mime, len(content), content, g.owner_id),
        )
        cur.execute("UPDATE shg_agent_scans SET document_id = %s WHERE id = %s", (cur.fetchone()["id"], row["id"]))
        result.update(scanId=row["id"], postedFor=on, meetingId=meeting_id, rowsRead=len(data.get("rows") or []))

    summary = run_group(group["id"], "SCAN", prelude=prelude)
    return ok({"run": summary, "scan": result}, 201)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@bachatgat_bp.get("/agent/overview")
def agent_overview():
    group_id = request.args.get("groupId", type=int)
    if not group_id:
        raise ApiError(422, "groupId is required")
    return ok(_overview(group_id))


def _overview(group_id):
    with tx() as cur:
        load_group(cur, group_id)
        settings = load_settings(cur, group_id)
        policy = load_policy(cur, group_id)
        cur.execute("SELECT * FROM shg_agent_actions WHERE group_id = %s AND status = 'Needs Attention' ORDER BY updated_at DESC LIMIT 100",
                    (group_id,))
        attention = [_action_out(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT * FROM shg_agent_actions WHERE group_id = %s AND status IN ('Done', 'Undone') ORDER BY id DESC LIMIT 40",
            (group_id,),
        )
        recent = [_action_out(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM shg_agent_runs WHERE group_id = %s ORDER BY id DESC LIMIT 1", (group_id,))
        last_run = cur.fetchone()
        cur.execute(
            """SELECT COUNT(*) FILTER (WHERE status IN ('Done', 'Undone')) AS done,
                      COALESCE(SUM(amount) FILTER (WHERE status = 'Done' AND action_type IN
                          ('SAVINGS_RECORDED', 'REPAYMENT_RECORDED', 'ONLINE_PAYMENT_POSTED')), 0) AS collected
               FROM shg_agent_actions WHERE group_id = %s AND created_at >= date_trunc('month', NOW())""",
            (group_id,),
        )
        month = camel(cur.fetchone())
        gateway = bool(_group_creds(cur, group_id))
    out = camel(settings)
    out["effectiveLoanLimit"] = effective_loan_limit(settings, policy)
    out["policyLoanLimit"] = money(policy["loan_approval_limit"])
    return {
        "settings": out, "attention": attention, "recent": recent, "lastRun": camel(last_run) if last_run else None,
        "month": month, "gatewayConnected": gateway, "aiReady": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "everyHours": float(os.getenv("SHG_AUTOPILOT_EVERY_HOURS", "6")),
    }


@bachatgat_bp.get("/agent/actions")
def agent_actions():
    size, offset, page = page_args(30, 100)
    group_id = request.args.get("groupId", type=int)
    if not group_id:
        raise ApiError(422, "groupId is required")
    with tx() as cur:
        load_group(cur, group_id)
        cur.execute(
            "SELECT * FROM shg_agent_actions WHERE group_id = %s AND status <> 'Resolved' ORDER BY id DESC LIMIT %s OFFSET %s",
            (group_id, size + 1, offset),
        )
        rows = cur.fetchall()
    return ok({"actions": [_action_out(r) for r in rows[:size]], "hasMore": len(rows) > size, "page": page})


@bachatgat_bp.put("/agent/settings/<int:group_id>")
def agent_settings(group_id):
    payload = body()
    with tx() as cur:
        load_group(cur, group_id, lock=True)
        current = load_settings(cur, group_id)
        sets = {}
        for col in SETTING_FLAGS:
            key = "".join(w.capitalize() if i else w for i, w in enumerate(col.split("_")))
            if key in payload:
                sets[col] = bool(payload[key])
        if "autoLoanLimit" in payload:
            raw = payload.get("autoLoanLimit")
            sets["auto_loan_limit"] = None if raw in (None, "") else parse_money(payload, "autoLoanLimit", label="Auto-approval limit")
        if "disburseMethod" in payload:
            sets["disburse_method"] = choice(payload, "disburseMethod", ("Cash", "Bank Transfer", "UPI"), label="Disbursement method")
        if sets:
            cur.execute(
                f"UPDATE shg_agent_settings SET {', '.join(f'{k} = %s' for k in sets)}, updated_at = NOW() WHERE group_id = %s",
                list(sets.values()) + [group_id],
            )
            record_event(cur, group_id, "GROUP", group_id, "AUTOPILOT_SETTINGS", details={
                k: (sets[k], current[k]) for k in sets})
    return ok(_overview(group_id))


@bachatgat_bp.post("/agent/run")
def agent_run():
    payload = body()
    group_id = parse_int(payload, "groupId", required=True, label="Group")
    with tx() as cur:
        load_group(cur, group_id)
        if not load_settings(cur, group_id)["enabled"]:
            raise ApiError(409, "The Autopilot is off for this group; turn it on first")
    stale = 1.0 if payload.get("ifStale") else None
    summary = run_group(group_id, "MANUAL", stale_hours=stale)
    return ok({"run": summary, "skipped": summary is None})


def _load_action(cur, action_id):
    cur.execute(
        """SELECT a.* FROM shg_agent_actions a JOIN shg_groups gr ON gr.id = a.group_id
           WHERE a.id = %s AND gr.owner_id = %s FOR UPDATE OF a""",
        (action_id, g.owner_id),
    )
    action = cur.fetchone()
    if not action:
        raise ApiError(404, "Autopilot action not found")
    return action


@bachatgat_bp.post("/agent/actions/<int:action_id>/undo")
def undo_action(action_id):
    payload = body()
    reason = text(payload, "reason", 300) or "Undone from the Autopilot screen"
    with tx() as cur:
        a = _load_action(cur, action_id)
        if a["status"] != "Done" or a["action_type"] not in UNDOABLE:
            raise ApiError(409, "This Autopilot action cannot be undone")
        kind, gid = a["action_type"], a["group_id"]
        if a["txn_id"]:
            cur.execute("SELECT posting_status FROM shg_transactions WHERE id = %s", (a["txn_id"],))
            if cur.fetchone()["posting_status"] != "Reversed":
                reverse_txn(cur, a["txn_id"], f"Autopilot undo: {reason}")
            if kind == "ONLINE_PAYMENT_POSTED":
                cur.execute("SELECT * FROM shg_gateway_payments WHERE id = %s FOR UPDATE", (a["entity_id"],))
                gp = cur.fetchone()
                if gp and gp["fee_txn_id"]:
                    cur.execute("SELECT posting_status FROM shg_transactions WHERE id = %s", (gp["fee_txn_id"],))
                    if cur.fetchone()["posting_status"] != "Reversed":
                        reverse_txn(cur, gp["fee_txn_id"], f"Autopilot undo: {reason}")
                cur.execute("UPDATE shg_gateway_payments SET status = 'Unposted', note = %s WHERE id = %s",
                            (f"Undone: {reason}"[:500], a["entity_id"]))
        elif kind == "LOAN_APPROVED":
            loan = load_loan(cur, a["entity_id"], lock=True)
            if loan["status"] != "Approved":
                raise ApiError(409, f"Loan is {loan['status']}; undo its disbursement first")
            if loan["resolution_id"]:
                cur.execute("UPDATE shg_resolutions SET consumed_by_type = NULL, consumed_by_id = NULL WHERE id = %s",
                            (loan["resolution_id"],))
            cur.execute("""UPDATE shg_loans SET status = 'Under Review', approved_amount = NULL, resolution_id = NULL,
                               version = version + 1, updated_at = NOW() WHERE id = %s""", (loan["id"],))
            record_event(cur, gid, "LOAN", loan["id"], "APPROVAL_UNDONE", "Approved", "Under Review", {"reason": reason})
        elif kind == "LOAN_REQUESTED":
            loan = load_loan(cur, a["entity_id"], lock=True)
            if loan["status"] not in ("Draft", "Submitted", "Under Review"):
                raise ApiError(409, f"Loan is already {loan['status']}; undo the later steps first")
            cur.execute("UPDATE shg_loans SET status = 'Rejected', rejection_reason = %s, version = version + 1 WHERE id = %s",
                        (f"Withdrawn: {reason}"[:500], loan["id"]))
            record_event(cur, gid, "LOAN", loan["id"], "WITHDRAW", loan["status"], "Rejected", {"reason": reason})
        elif kind in ("MEMBER_APPROVED", "MEMBER_ADDED"):
            member = load_member(cur, a["entity_id"], lock=True)
            cur.execute(
                """SELECT (SELECT COUNT(*) FROM shg_transactions WHERE member_id = %s AND posting_status = 'Posted') +
                          (SELECT COUNT(*) FROM shg_loans WHERE member_id = %s AND status <> 'Rejected') AS n""",
                (member["id"], member["id"]),
            )
            if cur.fetchone()["n"]:
                raise ApiError(409, "She already has transactions or loans; suspend her from the member screen instead")
            to_status = "Pending" if kind == "MEMBER_APPROVED" else "Rejected"
            cur.execute("DELETE FROM shg_savings_dues WHERE member_id = %s AND paid_amount = 0", (member["id"],))
            cur.execute("UPDATE shg_members SET status = %s, version = version + 1, updated_at = NOW() WHERE id = %s",
                        (to_status, member["id"]))
            record_event(cur, gid, "MEMBER", member["id"], "AUTOPILOT_UNDO", member["status"], to_status, {"reason": reason})
        elif kind == "MEETING_SCHEDULED":
            cur.execute("SELECT status FROM shg_meetings WHERE id = %s FOR UPDATE", (a["entity_id"],))
            mt = cur.fetchone()
            if not mt or mt["status"] != "Scheduled":
                raise ApiError(409, "The meeting is no longer just scheduled")
            cur.execute("UPDATE shg_meetings SET status = 'Cancelled', updated_at = NOW() WHERE id = %s", (a["entity_id"],))
            record_event(cur, gid, "MEETING", a["entity_id"], "CANCELLED", "Scheduled", "Cancelled", {"reason": reason})
        cur.execute(
            "UPDATE shg_agent_actions SET status = 'Undone', details = details || %s, updated_at = NOW() WHERE id = %s",
            (json.dumps({"undoReason": reason}), action_id),
        )
        cur.execute("SELECT * FROM shg_agent_actions WHERE id = %s", (action_id,))
        out = _action_out(cur.fetchone())
    return ok({"action": out})


@bachatgat_bp.post("/agent/actions/<int:action_id>/dismiss")
def dismiss_action(action_id):
    with tx() as cur:
        a = _load_action(cur, action_id)
        if a["status"] != "Needs Attention":
            raise ApiError(409, "Only open items can be dismissed")
        cur.execute("UPDATE shg_agent_actions SET status = 'Resolved', updated_at = NOW() WHERE id = %s", (action_id,))
    return ok({"dismissed": True})


@bachatgat_bp.post("/agent/loans/<int:loan_id>/approve")
def agent_approve_loan(loan_id):
    """The owner's one-tap approval of a loan above the Autopilot limit. The next run
    (started right away) disburses it like any other approved loan."""
    payload = body()
    with tx() as cur:
        loan = load_loan(cur, loan_id, lock=True)
        if loan["status"] not in ("Draft", "Submitted", "Under Review"):
            raise ApiError(409, f"Loan is {loan['status']}")
        member = load_member(cur, loan["member_id"])
        if member["status"] != "Active":
            raise ApiError(409, "Borrower is not an active member")
        policy = load_policy(cur, loan["group_id"])
        approved = parse_money(payload, "approvedAmount", positive=True, default=str(loan["requested_amount"]), label="Approved amount")
        if approved > money(loan["requested_amount"]):
            raise ApiError(422, "Approved amount cannot exceed the requested amount")
        problems = _eligibility(cur, loan, member, policy, approved, loan["installments"])
        if problems:
            raise ApiError(422, "Member is not eligible for this loan", problems)
        approve_loan(cur, loan, approved, None, "Approved by the owner on the Autopilot screen", "Owner")
        cur.execute(
            """INSERT INTO shg_agent_actions (group_id, action_type, status, message, entity_type, entity_id, amount)
               VALUES (%s, 'LOAN_APPROVED', 'Done', %s, 'LOAN', %s, %s)""",
            (loan["group_id"], f"You approved loan {loan['application_number']} of {rs(approved)} for {member['full_name']}",
             loan_id, approved),
        )
        cur.execute(
            "UPDATE shg_agent_actions SET status = 'Resolved', updated_at = NOW() WHERE group_id = %s AND dedupe_key = %s",
            (loan["group_id"], f"loans:approval:{loan_id}"),
        )
        group_id = loan["group_id"]
    summary = None
    try:
        summary = run_group(group_id, "APPROVAL")
    except ApiError:
        pass  # a run already in progress picks the loan up
    return ok({"approved": True, "run": summary})


@bachatgat_bp.post("/agent/cron")
def agent_cron():
    secret = os.getenv("SHG_AGENT_CRON_SECRET", "")
    given = request.headers.get("X-Cron-Secret", "")
    if not secret or not hmac.compare_digest(secret, given):
        return ok({"error": "Forbidden"}, 403)
    return ok(run_due_groups())
