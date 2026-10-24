"""
Mahila Bachat Gat (SHG) - pure financial calculations.

Nothing in this module touches the database, so every rule here is covered by
plain unit tests (test_bachatgat_finance.py). The API layer calls these
functions and persists the results.

Conventions
- All money is Decimal, quantized to paise with ROUND_HALF_UP.
- When a group's policy rounds installments to whole rupees, each installment is
  rounded and the LAST installment absorbs the residual, so the schedule always
  sums to exactly the principal (and, for flat loans, exactly the total interest).
- Interest is recognised on receipt (cash basis), which is how SHG books are kept.
"""

import calendar
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

PAISE = Decimal("0.01")
RUPEE = Decimal("1")
ZERO = Decimal("0")

INTEREST_METHODS = ("FLAT", "REDUCING")
FREQUENCIES = {"MONTHLY": 12, "FORTNIGHTLY": 26, "WEEKLY": 52}


class FinanceError(ValueError):
    """A business-rule violation the caller should show to the user."""


def money(value, default=ZERO):
    if value is None or value == "":
        return Decimal(default).quantize(PAISE, rounding=ROUND_HALF_UP)
    try:
        return Decimal(str(value)).quantize(PAISE, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise FinanceError(f"Invalid amount: {value}")


def _round(value, unit):
    return Decimal(value).quantize(unit, rounding=ROUND_HALF_UP)


def add_period(start, frequency, count):
    """The date `count` periods after `start`. Monthly dates keep the day of
    month where possible and clamp to the month's last day (31 Jan -> 28 Feb)."""
    if frequency == "WEEKLY":
        return start + timedelta(days=7 * count)
    if frequency == "FORTNIGHTLY":
        return start + timedelta(days=14 * count)
    if frequency != "MONTHLY":
        raise FinanceError(f"Unsupported installment frequency: {frequency}")
    month_index = start.month - 1 + count
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def build_schedule(principal, annual_rate, installments, method, frequency, first_due_date, round_to_rupee=True):
    """Installment schedule as a list of dicts:
    {number, dueDate, principal, interest, total, openingBalance, closingBalance}.

    FLAT: interest = P x rate x term-in-years, spread evenly.
    REDUCING: equal installments (EMI); each period's interest is charged on the
    opening balance, the rest of the EMI reduces principal.
    """
    principal = money(principal)
    rate = Decimal(str(annual_rate or 0))
    n = int(installments or 0)
    if principal <= 0:
        raise FinanceError("Loan amount must be greater than zero")
    if n <= 0 or n > 520:
        raise FinanceError("Number of installments must be between 1 and 520")
    if rate < 0 or rate > 60:
        raise FinanceError("Interest rate must be between 0 and 60 percent per year")
    if method not in INTEREST_METHODS:
        raise FinanceError("Interest method must be FLAT or REDUCING")
    if frequency not in FREQUENCIES:
        raise FinanceError("Installment frequency must be MONTHLY, FORTNIGHTLY or WEEKLY")
    if not isinstance(first_due_date, date):
        raise FinanceError("First due date is required")

    unit = RUPEE if round_to_rupee else PAISE
    periods_per_year = FREQUENCIES[frequency]
    periodic_rate = rate / Decimal(100) / Decimal(periods_per_year)
    rows = []

    if method == "FLAT":
        total_interest = money(principal * rate / Decimal(100) * Decimal(n) / Decimal(periods_per_year))
        p_each = _round(principal / n, unit)
        i_each = _round(total_interest / n, unit)
        balance = principal
        for k in range(1, n + 1):
            last = k == n
            p = principal - p_each * (n - 1) if last else p_each
            i = total_interest - i_each * (n - 1) if last else i_each
            if p < 0 or i < 0:
                raise FinanceError("Loan amount is too small for this many installments")
            rows.append(_row(k, add_period(first_due_date, frequency, k - 1), balance, p, i))
            balance -= p
        return rows

    # Reducing balance (EMI)
    if periodic_rate == 0:
        emi = principal / n
    else:
        factor = (1 + periodic_rate) ** n
        emi = principal * periodic_rate * factor / (factor - 1)
    emi = _round(emi, unit)
    balance = principal
    for k in range(1, n + 1):
        interest = money(balance * periodic_rate)
        if k == n:
            p = balance
        else:
            p = emi - interest
            if p < 0:
                raise FinanceError("Installment does not cover interest; increase the term or amount")
            if p > balance:
                p = balance
        rows.append(_row(k, add_period(first_due_date, frequency, k - 1), balance, p, interest))
        balance -= p
    return rows


def _row(number, due, opening, p, i):
    p = money(p)
    i = money(i)
    return {
        "number": number,
        "dueDate": due,
        "principal": p,
        "interest": i,
        "total": p + i,
        "openingBalance": money(opening),
        "closingBalance": money(opening - p),
    }


def schedule_totals(rows):
    return {
        "principal": sum((r["principal"] for r in rows), ZERO),
        "interest": sum((r["interest"] for r in rows), ZERO),
        "total": sum((r["total"] for r in rows), ZERO),
    }


def penalty_for(installment, as_of, policy):
    """Late-payment charge an installment carries on `as_of`, per the group's
    policy. Never less than what was already assessed (penalties are not undone
    by time passing); an installment with no unpaid principal/interest accrues
    nothing new.

    policy keys: penaltyType NONE | FLAT | PERCENT_PER_MONTH, penaltyValue, graceDays
    installment keys: dueDate, principalDue, interestDue, principalPaid,
                      interestPaid, interestWaived, penaltyDue
    """
    assessed = money(installment.get("penaltyDue"))
    ptype = (policy.get("penaltyType") or "NONE").upper()
    value = money(policy.get("penaltyValue"))
    grace = int(policy.get("graceDays") or 0)
    unpaid = (
        money(installment["principalDue"]) - money(installment.get("principalPaid"))
        + money(installment["interestDue"]) - money(installment.get("interestPaid"))
        - money(installment.get("interestWaived"))
    )
    if ptype == "NONE" or value <= 0 or unpaid <= 0:
        return assessed
    late_from = installment["dueDate"] + timedelta(days=grace)
    if as_of <= late_from:
        return assessed
    if ptype == "FLAT":
        computed = value
    elif ptype == "PERCENT_PER_MONTH":
        days_late = (as_of - installment["dueDate"]).days
        months = (Decimal(days_late) / Decimal(30)).to_integral_value(rounding=ROUND_CEILING)
        computed = money(unpaid * value / Decimal(100) * months)
    else:
        raise FinanceError(f"Unknown penalty type {ptype}")
    return max(assessed, computed)


def installment_outstanding(inst):
    """Unpaid amounts of one installment: dict(principal, interest, penalty)."""
    return {
        "principal": max(ZERO, money(inst["principalDue"]) - money(inst.get("principalPaid"))),
        "interest": max(ZERO, money(inst["interestDue"]) - money(inst.get("interestPaid")) - money(inst.get("interestWaived"))),
        "penalty": max(ZERO, money(inst.get("penaltyDue")) - money(inst.get("penaltyPaid")) - money(inst.get("penaltyWaived"))),
    }


def allocate_payment(amount, installments):
    """Splits a repayment across installments, oldest first; within each
    installment the order is penalty -> interest -> principal (the usual SHG
    and RBI-guideline order). Paying ahead covers future installments in turn.

    Returns (allocations, totals). Raises FinanceError when the payment exceeds
    everything owed - the app has no advance/credit balance for loans.
    """
    amount = money(amount)
    if amount <= 0:
        raise FinanceError("Payment amount must be greater than zero")
    ordered = sorted(installments, key=lambda r: (r["dueDate"], r["number"]))
    owed = ZERO
    for inst in ordered:
        o = installment_outstanding(inst)
        owed += o["principal"] + o["interest"] + o["penalty"]
    if amount > owed:
        raise FinanceError(f"Payment exceeds the outstanding amount of {owed}")

    remaining = amount
    allocations = []
    totals = {"principal": ZERO, "interest": ZERO, "penalty": ZERO}
    for inst in ordered:
        if remaining <= 0:
            break
        o = installment_outstanding(inst)
        part = {"installmentId": inst.get("id"), "number": inst["number"], "principal": ZERO, "interest": ZERO, "penalty": ZERO}
        for head in ("penalty", "interest", "principal"):
            take = min(remaining, o[head])
            part[head] = take
            remaining -= take
        if part["principal"] or part["interest"] or part["penalty"]:
            allocations.append(part)
            for head in totals:
                totals[head] += part[head]
    return allocations, totals


def foreclosure_quote(installments, as_of, method):
    """Amount needed to close a loan today.

    Installments due on or before `as_of` are owed in full (with penalty).
    For future installments only principal is owed on REDUCING loans (interest
    has not accrued yet); FLAT loans owe the contracted interest too.
    Returns dict(principal, interest, penalty, total, waivedInterest).
    """
    q = {"principal": ZERO, "interest": ZERO, "penalty": ZERO, "waivedInterest": ZERO}
    for inst in installments:
        o = installment_outstanding(inst)
        q["principal"] += o["principal"]
        q["penalty"] += o["penalty"]
        if inst["dueDate"] <= as_of or method == "FLAT":
            q["interest"] += o["interest"]
        else:
            q["waivedInterest"] += o["interest"]
    q["total"] = q["principal"] + q["interest"] + q["penalty"]
    return q


def check_loan_eligibility(policy, member, request):
    """Eligibility against the group's configured loan rules. Returns a list of
    human-readable problems (empty list = eligible).

    policy: maxLoanAmount, savingsMultiple, minMembershipMonths, maxInstallments,
            allowMultipleLoans
    member: status, joiningDate, savingsBalance, activeLoans, overdueLoans
    request: amount, installments, asOf
    """
    problems = []
    amount = money(request.get("amount"))
    as_of = request.get("asOf") or date.today()
    if member.get("status") != "Active":
        problems.append("Member must be active")
    if amount <= 0:
        problems.append("Loan amount must be greater than zero")
    max_amount = money(policy.get("maxLoanAmount"))
    if max_amount > 0 and amount > max_amount:
        problems.append(f"Amount exceeds the group limit of {max_amount}")
    multiple = Decimal(str(policy.get("savingsMultiple") or 0))
    if multiple > 0:
        limit = money(money(member.get("savingsBalance")) * multiple)
        if amount > limit:
            problems.append(f"Amount exceeds {multiple} x member savings ({limit})")
    min_months = int(policy.get("minMembershipMonths") or 0)
    joined = member.get("joiningDate")
    if min_months > 0 and joined:
        if add_period(joined, "MONTHLY", min_months) > as_of:
            problems.append(f"Member needs at least {min_months} months of membership")
    max_inst = int(policy.get("maxInstallments") or 0)
    if max_inst > 0 and int(request.get("installments") or 0) > max_inst:
        problems.append(f"Term exceeds the group maximum of {max_inst} installments")
    if int(member.get("overdueLoans") or 0) > 0:
        problems.append("Member has an overdue loan")
    if not policy.get("allowMultipleLoans") and int(member.get("activeLoans") or 0) > 0:
        problems.append("Member already has an active loan")
    return problems


def period_starts(frequency, start, end):
    """Savings period start dates from `start` to `end` inclusive."""
    if start > end:
        return []
    out = []
    k = 0
    while True:
        d = add_period(start, frequency, k)
        if d > end:
            return out
        out.append(d)
        k += 1
        if k > 1000:
            raise FinanceError("Date range is too long")


def allocate_savings(amount, dues):
    """Applies a savings payment to a member's open dues, oldest first. Anything
    beyond the dues is voluntary savings (still the member's money).
    Returns (allocations [{dueId, amount}], voluntary)."""
    remaining = money(amount)
    if remaining <= 0:
        raise FinanceError("Payment amount must be greater than zero")
    out = []
    for due in sorted(dues, key=lambda d: d["periodStart"]):
        if remaining <= 0:
            break
        open_amount = money(due["dueAmount"]) - money(due.get("paidAmount"))
        if open_amount <= 0:
            continue
        take = min(open_amount, remaining)
        out.append({"dueId": due.get("id"), "amount": take})
        remaining -= take
    return out, remaining


# ---------------------------------------------------------------------------
# Double-entry postings
# ---------------------------------------------------------------------------
# Account codes and their classification. "Money" accounts are CASH and BANK
# (BANK lines also carry the bank account id).
ACCOUNTS = {
    "CASH": "ASSET",
    "BANK": "ASSET",
    "LOAN_RECEIVABLE": "ASSET",
    "MEMBER_SAVINGS": "LIABILITY",
    "BANK_LOAN": "LIABILITY",
    "GRANT_FUND": "FUND",
    "OPENING_EQUITY": "EQUITY",
    "INTEREST_INCOME": "INCOME",
    "PENALTY_INCOME": "INCOME",
    "OTHER_INCOME": "INCOME",
    "BANK_INTEREST": "INCOME",
    "BUSINESS_INCOME": "BUSINESS_INCOME",
    "OPERATING_EXPENSE": "EXPENSE",
    "BANK_CHARGES": "EXPENSE",
    "BANK_LOAN_INTEREST": "EXPENSE",
    "LOAN_WRITE_OFF": "EXPENSE",
    "BUSINESS_EXPENSE": "BUSINESS_EXPENSE",
}

# txn type -> (direction of money: IN / OUT / NONE / TRANSFER, the non-money account)
TXN_TYPES = {
    "SAVINGS_DEPOSIT": ("IN", "MEMBER_SAVINGS"),
    "SAVINGS_WITHDRAWAL": ("OUT", "MEMBER_SAVINGS"),
    "LOAN_DISBURSEMENT": ("OUT", "LOAN_RECEIVABLE"),
    "LOAN_REPAYMENT": ("IN", None),  # split into principal / interest / penalty
    "SAVINGS_LOAN_OFFSET": ("NONE", None),  # savings used to settle a loan at exit
    "LOAN_WRITE_OFF": ("NONE", None),
    "OPERATING_EXPENSE": ("OUT", "OPERATING_EXPENSE"),
    "OTHER_INCOME": ("IN", "OTHER_INCOME"),
    "BUSINESS_INCOME": ("IN", "BUSINESS_INCOME"),
    "BUSINESS_EXPENSE": ("OUT", "BUSINESS_EXPENSE"),
    "BANK_CHARGES": ("OUT", "BANK_CHARGES"),
    "BANK_INTEREST": ("IN", "BANK_INTEREST"),
    "GRANT_RECEIVED": ("IN", "GRANT_FUND"),
    "BANK_LOAN_RECEIVED": ("IN", "BANK_LOAN"),
    "BANK_LOAN_REPAYMENT": ("OUT", None),  # principal -> BANK_LOAN, interest -> BANK_LOAN_INTEREST
    "BANK_DEPOSIT": ("TRANSFER", None),  # cash -> bank
    "BANK_WITHDRAWAL": ("TRANSFER", None),  # bank -> cash
    "OPENING_BALANCE": ("IN", "OPENING_EQUITY"),
    "REVERSAL": ("NONE", None),
}

# Classification shown on every transaction (requirement: separate savings
# liabilities, loan receivables, interest income, business income, expenses, funds).
TXN_CLASSIFICATION = {
    "SAVINGS_DEPOSIT": "SAVINGS_LIABILITY",
    "SAVINGS_WITHDRAWAL": "SAVINGS_LIABILITY",
    "SAVINGS_LOAN_OFFSET": "SAVINGS_LIABILITY",
    "LOAN_DISBURSEMENT": "LOAN_RECEIVABLE",
    "LOAN_REPAYMENT": "LOAN_RECEIVABLE",
    "LOAN_WRITE_OFF": "LOAN_RECEIVABLE",
    "OPERATING_EXPENSE": "OPERATING_EXPENSE",
    "OTHER_INCOME": "OTHER_INCOME",
    "BUSINESS_INCOME": "BUSINESS_INCOME",
    "BUSINESS_EXPENSE": "BUSINESS_EXPENSE",
    "BANK_CHARGES": "OPERATING_EXPENSE",
    "BANK_INTEREST": "OTHER_INCOME",
    "GRANT_RECEIVED": "EXTERNAL_FUNDS",
    "BANK_LOAN_RECEIVED": "EXTERNAL_FUNDS",
    "BANK_LOAN_REPAYMENT": "EXTERNAL_FUNDS",
    "BANK_DEPOSIT": "TRANSFER",
    "BANK_WITHDRAWAL": "TRANSFER",
    "OPENING_BALANCE": "OPENING",
    "REVERSAL": "REVERSAL",
}


def money_account(payment_method):
    """Cash goes to the cashbook; bank transfer / UPI / cheque to the bankbook."""
    return "CASH" if (payment_method or "Cash") == "Cash" else "BANK"


def journal_lines(txn_type, amount, payment_method="Cash", split=None):
    """Balanced ledger lines [(account, debit, credit, tag)] for a transaction.
    `tag` is 'money' on the CASH/BANK line so the caller can attach a bank
    account id. `split` carries principal/interest/penalty for repayments."""
    amount = money(amount)
    if amount <= 0:
        raise FinanceError("Amount must be greater than zero")
    if txn_type not in TXN_TYPES or txn_type == "REVERSAL":
        raise FinanceError(f"Unsupported transaction type: {txn_type}")
    m = money_account(payment_method)
    direction, other = TXN_TYPES[txn_type]

    if txn_type in ("LOAN_REPAYMENT", "SAVINGS_LOAN_OFFSET"):
        split = split or {}
        p, i, pen = money(split.get("principal")), money(split.get("interest")), money(split.get("penalty"))
        if p + i + pen != amount:
            raise FinanceError("Repayment split does not add up to the amount")
        debit_acc = m if txn_type == "LOAN_REPAYMENT" else "MEMBER_SAVINGS"
        lines = [(debit_acc, amount, ZERO, "money" if txn_type == "LOAN_REPAYMENT" else None)]
        if p:
            lines.append(("LOAN_RECEIVABLE", ZERO, p, None))
        if i:
            lines.append(("INTEREST_INCOME", ZERO, i, None))
        if pen:
            lines.append(("PENALTY_INCOME", ZERO, pen, None))
        return lines
    if txn_type == "BANK_LOAN_REPAYMENT":
        split = split or {}
        p, i = money(split.get("principal", amount)), money(split.get("interest"))
        if p + i != amount:
            raise FinanceError("Bank loan repayment split does not add up to the amount")
        lines = [(m, ZERO, amount, "money")]
        if p:
            lines.append(("BANK_LOAN", p, ZERO, None))
        if i:
            lines.append(("BANK_LOAN_INTEREST", i, ZERO, None))
        return lines
    if txn_type == "LOAN_WRITE_OFF":
        return [("LOAN_WRITE_OFF", amount, ZERO, None), ("LOAN_RECEIVABLE", ZERO, amount, None)]
    if txn_type == "BANK_DEPOSIT":
        return [("BANK", amount, ZERO, "money"), ("CASH", ZERO, amount, None)]
    if txn_type == "BANK_WITHDRAWAL":
        return [("CASH", amount, ZERO, None), ("BANK", ZERO, amount, "money")]
    if txn_type in ("BANK_CHARGES", "BANK_INTEREST") and m != "BANK":
        raise FinanceError("Bank charges and bank interest must use a bank account")
    if direction == "IN":
        return [(m, amount, ZERO, "money"), (other, ZERO, amount, None)]
    return [(other, amount, ZERO, None), (m, ZERO, amount, "money")]


def is_balanced(lines):
    return sum((l[1] for l in lines), ZERO) == sum((l[2] for l in lines), ZERO)
