"""
Unit tests for the Mahila Bachat Gat financial engine (no database needed).

Run:  .venv\\Scripts\\python.exe -m unittest test_bachatgat_finance -v
"""

import unittest
from datetime import date
from decimal import Decimal as D

from bachatgat_finance import (
    FinanceError,
    add_period,
    allocate_payment,
    allocate_savings,
    build_schedule,
    check_loan_eligibility,
    foreclosure_quote,
    is_balanced,
    journal_lines,
    penalty_for,
    period_starts,
    schedule_totals,
    TXN_TYPES,
)


def inst(number, due, p, i, **paid):
    return {"id": number, "number": number, "dueDate": due, "principalDue": D(p), "interestDue": D(i), **paid}


class DateTests(unittest.TestCase):
    def test_monthly_clamps_to_month_end(self):
        self.assertEqual(add_period(date(2026, 1, 31), "MONTHLY", 1), date(2026, 2, 28))
        self.assertEqual(add_period(date(2028, 1, 31), "MONTHLY", 1), date(2028, 2, 29))
        self.assertEqual(add_period(date(2026, 11, 15), "MONTHLY", 3), date(2027, 2, 15))

    def test_weekly_and_fortnightly(self):
        self.assertEqual(add_period(date(2026, 10, 1), "WEEKLY", 2), date(2026, 10, 15))
        self.assertEqual(add_period(date(2026, 10, 1), "FORTNIGHTLY", 1), date(2026, 10, 15))

    def test_period_starts(self):
        self.assertEqual(
            period_starts("MONTHLY", date(2026, 1, 1), date(2026, 3, 31)),
            [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)],
        )
        self.assertEqual(period_starts("MONTHLY", date(2026, 5, 1), date(2026, 4, 1)), [])


class FlatScheduleTests(unittest.TestCase):
    def test_flat_12_months_24_percent(self):
        rows = build_schedule(10000, 24, 12, "FLAT", "MONTHLY", date(2026, 11, 1))
        totals = schedule_totals(rows)
        self.assertEqual(totals["principal"], D("10000.00"))
        self.assertEqual(totals["interest"], D("2400.00"))  # 10000 x 24% x 1 year
        self.assertEqual(rows[0]["principal"], D("833.00"))
        self.assertEqual(rows[-1]["principal"], D("837.00"))  # residual on last
        self.assertEqual(rows[0]["interest"], D("200.00"))
        self.assertEqual(rows[-1]["closingBalance"], D("0.00"))
        self.assertEqual(rows[1]["dueDate"], date(2026, 12, 1))

    def test_flat_weekly_term_in_years(self):
        rows = build_schedule(5200, 12, 52, "FLAT", "WEEKLY", date(2026, 11, 2))
        self.assertEqual(schedule_totals(rows)["interest"], D("624.00"))  # one year of interest

    def test_paise_rounding_when_configured(self):
        rows = build_schedule(1000, 10, 3, "FLAT", "MONTHLY", date(2026, 11, 1), round_to_rupee=False)
        self.assertEqual(rows[0]["principal"], D("333.33"))
        self.assertEqual(rows[-1]["principal"], D("333.34"))
        self.assertEqual(schedule_totals(rows)["principal"], D("1000.00"))


class ReducingScheduleTests(unittest.TestCase):
    def test_emi_12_months_12_percent(self):
        rows = build_schedule(10000, 12, 12, "REDUCING", "MONTHLY", date(2026, 11, 1))
        # Standard EMI for 10,000 @ 1%/month x 12 = 888.49 -> 888 rounded to rupee
        self.assertEqual(rows[0]["total"], D("888.00"))
        self.assertEqual(rows[0]["interest"], D("100.00"))  # 1% of 10,000
        self.assertEqual(rows[0]["principal"], D("788.00"))
        self.assertEqual(rows[1]["interest"], D("92.12"))  # 1% of 9,212
        totals = schedule_totals(rows)
        self.assertEqual(totals["principal"], D("10000.00"))
        self.assertEqual(rows[-1]["closingBalance"], D("0.00"))
        # Total interest close to the textbook 661.85 (differences come from rupee rounding)
        self.assertTrue(D("650") < totals["interest"] < D("670"), totals["interest"])

    def test_zero_rate(self):
        rows = build_schedule(1200, 0, 12, "REDUCING", "MONTHLY", date(2026, 11, 1))
        self.assertTrue(all(r["interest"] == 0 for r in rows))
        self.assertEqual(schedule_totals(rows)["principal"], D("1200.00"))

    def test_validation(self):
        with self.assertRaises(FinanceError):
            build_schedule(0, 12, 12, "FLAT", "MONTHLY", date(2026, 1, 1))
        with self.assertRaises(FinanceError):
            build_schedule(1000, 12, 0, "FLAT", "MONTHLY", date(2026, 1, 1))
        with self.assertRaises(FinanceError):
            build_schedule(1000, 12, 12, "SIMPLE", "MONTHLY", date(2026, 1, 1))
        with self.assertRaises(FinanceError):
            build_schedule(1000, 99, 12, "FLAT", "MONTHLY", date(2026, 1, 1))


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.insts = [
            inst(1, date(2026, 1, 1), "100", "10", penaltyDue=D("5")),
            inst(2, date(2026, 2, 1), "100", "10"),
        ]

    def test_order_penalty_interest_principal_oldest_first(self):
        allocs, totals = allocate_payment(50, self.insts)
        self.assertEqual(len(allocs), 1)
        self.assertEqual((allocs[0]["penalty"], allocs[0]["interest"], allocs[0]["principal"]), (D("5"), D("10"), D("35")))
        self.assertEqual(totals["principal"], D("35"))

    def test_prepayment_covers_next_installment(self):
        allocs, totals = allocate_payment(150, self.insts)
        self.assertEqual([a["number"] for a in allocs], [1, 2])
        self.assertEqual(allocs[1]["interest"], D("10"))
        self.assertEqual(allocs[1]["principal"], D("25"))
        self.assertEqual(totals["principal"] + totals["interest"] + totals["penalty"], D("150"))

    def test_overpayment_rejected(self):
        with self.assertRaises(FinanceError):
            allocate_payment("225.01", self.insts)
        allocs, totals = allocate_payment("225", self.insts)
        self.assertEqual(totals["principal"], D("200"))

    def test_respects_already_paid_and_waived(self):
        insts = [inst(1, date(2026, 1, 1), "100", "10", principalPaid=D("100"), interestPaid=D("4"), interestWaived=D("6"))]
        with self.assertRaises(FinanceError):
            allocate_payment(1, insts)


class PenaltyTests(unittest.TestCase):
    def test_flat_after_grace(self):
        i = inst(1, date(2026, 1, 1), "100", "10")
        policy = {"penaltyType": "FLAT", "penaltyValue": 20, "graceDays": 5}
        self.assertEqual(penalty_for(i, date(2026, 1, 6), policy), D("0"))
        self.assertEqual(penalty_for(i, date(2026, 1, 7), policy), D("20"))

    def test_percent_per_month(self):
        i = inst(1, date(2026, 1, 1), "1000", "0")
        policy = {"penaltyType": "PERCENT_PER_MONTH", "penaltyValue": 2, "graceDays": 0}
        self.assertEqual(penalty_for(i, date(2026, 1, 15), policy), D("20.00"))  # part month counts as 1
        self.assertEqual(penalty_for(i, date(2026, 3, 2), policy), D("40.00"))  # 60 days = 2 months

    def test_never_decreases_and_none_when_paid(self):
        paid = inst(1, date(2026, 1, 1), "100", "10", principalPaid=D("100"), interestPaid=D("10"), penaltyDue=D("7"))
        policy = {"penaltyType": "FLAT", "penaltyValue": 20, "graceDays": 0}
        self.assertEqual(penalty_for(paid, date(2026, 6, 1), policy), D("7"))
        self.assertEqual(penalty_for(inst(1, date(2026, 1, 1), "1", "0"), date(2026, 6, 1), {"penaltyType": "NONE"}), D("0"))


class ForeclosureTests(unittest.TestCase):
    def test_reducing_waives_future_interest(self):
        insts = [inst(1, date(2026, 1, 1), "100", "10"), inst(2, date(2026, 3, 1), "100", "8")]
        q = foreclosure_quote(insts, date(2026, 2, 1), "REDUCING")
        self.assertEqual(q["total"], D("210"))
        self.assertEqual(q["waivedInterest"], D("8"))
        q = foreclosure_quote(insts, date(2026, 2, 1), "FLAT")
        self.assertEqual(q["total"], D("218"))


class EligibilityTests(unittest.TestCase):
    policy = {"maxLoanAmount": 50000, "savingsMultiple": 4, "minMembershipMonths": 6, "maxInstallments": 24, "allowMultipleLoans": False}

    def member(self, **kw):
        base = {"status": "Active", "joiningDate": date(2026, 1, 1), "savingsBalance": D("2000"), "activeLoans": 0, "overdueLoans": 0}
        base.update(kw)
        return base

    def test_eligible(self):
        self.assertEqual(check_loan_eligibility(self.policy, self.member(), {"amount": 8000, "installments": 12, "asOf": date(2026, 10, 1)}), [])

    def test_each_rule(self):
        req = {"amount": 9000, "installments": 30, "asOf": date(2026, 3, 1)}
        problems = check_loan_eligibility(self.policy, self.member(activeLoans=1, overdueLoans=1, status="Pending"), req)
        self.assertEqual(len(problems), 6, problems)


class SavingsAllocationTests(unittest.TestCase):
    def test_oldest_first_with_voluntary_excess(self):
        dues = [
            {"id": 2, "periodStart": date(2026, 2, 1), "dueAmount": D("100"), "paidAmount": D("0")},
            {"id": 1, "periodStart": date(2026, 1, 1), "dueAmount": D("100"), "paidAmount": D("60")},
        ]
        allocs, extra = allocate_savings(200, dues)
        self.assertEqual(allocs, [{"dueId": 1, "amount": D("40")}, {"dueId": 2, "amount": D("100")}])
        self.assertEqual(extra, D("60"))


class JournalTests(unittest.TestCase):
    def test_every_type_balances(self):
        for txn_type in TXN_TYPES:
            if txn_type == "REVERSAL":
                continue
            method = "Bank Transfer" if txn_type.startswith("BANK") else "Cash"
            split = {"principal": D("60"), "interest": D("30"), "penalty": D("10")}
            if txn_type == "BANK_LOAN_REPAYMENT":
                split = {"principal": D("80"), "interest": D("20")}
            lines = journal_lines(txn_type, 100, method, split)
            self.assertTrue(is_balanced(lines), txn_type)

    def test_savings_deposit_is_liability_not_income(self):
        lines = journal_lines("SAVINGS_DEPOSIT", 500, "Cash")
        self.assertEqual(lines[0][:2], ("CASH", D("500.00")))
        self.assertEqual(lines[1][0], "MEMBER_SAVINGS")

    def test_upi_goes_to_bank(self):
        self.assertEqual(journal_lines("SAVINGS_DEPOSIT", 1, "UPI")[0][0], "BANK")

    def test_repayment_split_must_match(self):
        with self.assertRaises(FinanceError):
            journal_lines("LOAN_REPAYMENT", 100, "Cash", {"principal": 50, "interest": 10})

    def test_bank_charges_need_bank(self):
        with self.assertRaises(FinanceError):
            journal_lines("BANK_CHARGES", 10, "Cash")


if __name__ == "__main__":
    unittest.main()
