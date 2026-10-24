"""
End-to-end tests for the Mahila Bachat Gat API against a real PostgreSQL database.

These tests CREATE TABLES and WRITE DATA, so they only run against a database you
name explicitly - never the DATABASE_URL in .env:

    $env:BACHATGAT_TEST_DATABASE_URL = "postgresql://postgres:<pw>@localhost:5432/autoledger_test"
    .venv\\Scripts\\python.exe -m unittest test_bachatgat_api -v

Use an empty, throwaway database. The suite does not import app.py (which runs
the whole platform's schema provisioning on import); it mounts only the SHG
blueprint on a bare Flask app.
"""

import os
import threading
import unittest
import uuid
from datetime import date, timedelta

TEST_DB = os.getenv("BACHATGAT_TEST_DATABASE_URL")


def _configured_db():
    from dotenv import dotenv_values
    return (dotenv_values(os.path.join(os.path.dirname(__file__), ".env")) or {}).get("DATABASE_URL")


@unittest.skipUnless(TEST_DB, "set BACHATGAT_TEST_DATABASE_URL to an empty test database to run")
class BachatGatApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if TEST_DB == _configured_db() or TEST_DB == os.getenv("DATABASE_URL"):
            raise RuntimeError("Refusing to run: BACHATGAT_TEST_DATABASE_URL must not be the application database")
        os.environ["BACHATGAT_DATABASE_URL"] = TEST_DB
        os.environ.setdefault("AUTH_SECRET", "bachatgat-test-secret")
        import psycopg
        from flask import Flask
        from auth_token import issue_token
        from bachatgat_api import bachatgat_bp
        from bachatgat_service import ensure_bachatgat_schema

        with psycopg.connect(TEST_DB) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS userdetails (
                        id BIGSERIAL PRIMARY KEY, full_name VARCHAR(120) NOT NULL, email VARCHAR(255) NOT NULL UNIQUE,
                        dob DATE NOT NULL, business_type VARCHAR(100) NOT NULL DEFAULT 'Other local businesses',
                        active_plan VARCHAR(100) NOT NULL DEFAULT 'monthly', user_type VARCHAR(20) NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'active', enabled_features JSONB DEFAULT '[]'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
                    CREATE TABLE IF NOT EXISTS business_types (
                        id BIGSERIAL PRIMARY KEY, name VARCHAR(120) NOT NULL UNIQUE, description VARCHAR(500) NOT NULL DEFAULT '',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
                """)
                ensure_bachatgat_schema(cur)
                ensure_bachatgat_schema(cur)  # idempotent re-run
                run = uuid.uuid4().hex[:8]
                ids = []
                for name, btype, feats in (("Tenant A", "Mahila Bachat Gat", "[]"), ("Tenant B", "Mahila Bachat Gat", "[]"),
                                           ("Kirana", "Kirana stores", '["products"]'),
                                           ("No reports", "Mahila Bachat Gat", '["shg_groups", "shg_members"]')):
                    cur.execute(
                        "INSERT INTO userdetails (full_name, email, dob, business_type, user_type, enabled_features) "
                        "VALUES (%s, %s, '1990-01-01', %s, 'customer', %s::jsonb) RETURNING id",
                        (name, f"{name.replace(' ', '').lower()}-{run}@test.local", btype, feats),
                    )
                    ids.append(cur.fetchone()[0])
            conn.commit()
        cls.user_a, cls.user_b, cls.user_c, cls.user_d = ids
        cls.tokens = {uid: issue_token(uid) for uid in ids}
        app = Flask("bachatgat-test")
        app.register_blueprint(bachatgat_bp)
        cls.client = app.test_client()
        cls.state = {}
        cls.mobile_seed = int(run, 16) % 10_000_000

    # helpers --------------------------------------------------------------
    def call(self, method, path, user=None, json=None, key=None, expect=None):
        headers = {"Authorization": f"Bearer {self.tokens[user or self.user_a]}"}
        if key:
            headers["Idempotency-Key"] = key
        res = getattr(self.client, method)(f"/api/bachatgat{path}", json=json, headers=headers)
        data = res.get_json()
        if expect is not None:
            self.assertEqual(res.status_code, expect, f"{method.upper()} {path}: {data}")
        return data

    def key(self):
        return uuid.uuid4().hex

    def mobile(self, n):
        return f"9{(self.mobile_seed + n) % 1_000_000_000:09d}"

    # tests (run in name order) ----------------------------------------------
    def test_01_access_control(self):
        self.assertEqual(self.client.get("/api/bachatgat/dashboard").status_code, 401)
        self.call("get", "/dashboard", user=self.user_c, expect=403)  # other business type
        self.call("get", "/groups", user=self.user_d, expect=200)  # enabled feature
        self.call("get", "/reports", user=self.user_d, expect=403)  # feature switched off by admin
        self.call("get", "/dashboard", expect=200)

    def test_02_group_lifecycle(self):
        today = date.today()
        g = self.call("post", "/groups", json={
            "nameEn": "Savitri Mahila Bachat Gat", "nameMr": "सावित्री महिला बचत गट", "formationDate": (today - timedelta(days=400)).isoformat(),
            "village": "Shirur", "taluka": "Shirur", "district": "Pune", "state": "Maharashtra",
            "bankName": "Bank of Maharashtra", "accountNumber": "60123456789", "ifsc": "MAHB0000123",
            "policy": {"minMembers": 3, "savingsAmount": 100, "minMembershipMonths": 0, "savingsMultiple": 4,
                       "expenseApprovalLimit": 1000, "penaltyType": "FLAT", "penaltyValue": 10,
                       "allowSavingsWithdrawal": True, "loanApprovalLimit": 5000},
        }, expect=201)["group"]
        gid = self.state["group"] = g["id"]
        self.assertEqual(g["status"], "Draft")
        self.call("get", f"/groups/{gid}", user=self.user_b, expect=404)  # tenant isolation

        members = []
        for i, name in enumerate(["Sunita Pawar", "Asha Jadhav", "Rekha More", "Meena Shinde"]):
            m = self.call("post", "/members", json={"groupId": gid, "fullName": name, "mobile": self.mobile(i),
                                                    "joiningDate": (today - timedelta(days=300)).isoformat(),
                                                    "idProofType": "Aadhaar", "idProofNumber": "123412341234"}, expect=201)["member"]
            self.assertEqual(m["idProofLast4"], "1234")
            self.assertNotIn("idProofNumber", m)
            members.append(m["id"])
        self.state["members"] = members
        self.call("post", "/members", json={"groupId": gid, "fullName": "Dup", "mobile": self.mobile(0),
                                            "joiningDate": today.isoformat()}, expect=409)
        self.call("post", f"/members/{members[0]}/workflow", json={"action": "approve"}, expect=422)  # not verified yet
        for mid in members:
            self.call("post", f"/members/{mid}/workflow", json={"action": "verify"}, expect=200)
            self.call("post", f"/members/{mid}/workflow", json={"action": "approve"}, expect=200)
        self.call("post", f"/groups/{gid}/workflow", json={"action": "submit"}, expect=422)  # no office-bearers
        for mid, role in zip(members[1:], ["President", "Secretary", "Treasurer"]):
            self.call("post", f"/members/{mid}/role", json={"role": role}, expect=200)
        self.call("post", f"/groups/{gid}/workflow", json={"action": "submit"}, expect=200)
        officer = self.call("post", f"/groups/{gid}/officers", json={"fullName": "CRP Kale", "designation": "Field Officer"}, expect=201)["officer"]
        self.call("post", f"/groups/{gid}/workflow", json={"action": "approve", "officerId": officer["id"]}, expect=422)  # no document
        import base64
        self.call("post", "/documents", json={"entityType": "GROUP", "entityId": gid, "documentType": "Formation resolution",
                                               "mimeType": "application/pdf", "fileBase64": base64.b64encode(b"%PDF-1.4 test").decode()}, expect=201)
        self.call("post", "/documents", json={"entityType": "GROUP", "entityId": gid, "documentType": "Fake",
                                               "mimeType": "application/pdf", "fileBase64": base64.b64encode(b"MZ binary").decode()}, expect=422)
        g = self.call("post", f"/groups/{gid}/workflow", json={"action": "approve", "officerId": officer["id"]}, expect=200)["group"]
        self.assertEqual(g["status"], "Active")

    def test_03_savings_and_idempotency(self):
        gid, m = self.state["group"], self.state["members"]
        # A wrong entry that was reversed must not block the opening balance.
        wrong = self.call("post", "/savings/collect", key=self.key(), json={"memberId": m[0], "amount": 100}, expect=201)
        self.call("post", f"/transactions/{wrong['transaction']['id']}/reverse", key=self.key(), json={"reason": "Wrong entry"}, expect=201)
        self.call("post", "/transactions", key=self.key(), json={"groupId": gid, "txnType": "OPENING_BALANCE", "amount": 5000}, expect=201)
        gen = self.call("post", "/savings/dues/generate", json={"groupId": gid}, expect=200)
        self.assertGreater(gen["created"], 0)
        k = self.key()
        first = self.call("post", "/savings/collect", key=k, json={"memberId": m[0], "amount": 300}, expect=201)
        self.assertTrue(first["transaction"]["receiptNumber"])
        again = self.call("post", "/savings/collect", key=k, json={"memberId": m[0], "amount": 300}, expect=201)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["transaction"]["id"], first["transaction"]["id"])
        bal = self.call("get", f"/savings/member-due?memberId={m[0]}", expect=200)["savingsBalance"]
        self.assertEqual(bal, 300.0)
        self.call("post", "/savings/collect", key=self.key(), json={"memberId": m[0], "amount": 50}, user=self.user_b, expect=404)
        self.call("post", "/savings/collect", json={"memberId": m[0], "amount": 50}, expect=400)  # no key
        for mid in m[1:]:
            self.call("post", "/savings/collect", key=self.key(), json={"memberId": mid, "amount": 400, "paymentMethod": "UPI"}, expect=201)

    def test_04_concurrent_duplicate_submission(self):
        mid = self.state["members"][3]
        k = self.key()
        results = []

        def go():
            results.append(self.call("post", "/savings/collect", key=k, json={"memberId": mid, "amount": 25}))

        threads = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        ids = {r["transaction"]["id"] for r in results if r and "transaction" in r}
        self.assertEqual(len(ids), 1, results)

    def test_05_loan_approval_workflow(self):
        gid, m = self.state["group"], self.state["members"]
        loan = self.call("post", "/loans", json={"memberId": m[0], "requestedAmount": 1000, "purpose": "Tailoring machine",
                                                 "interestMethod": "FLAT", "interestRate": 12, "installments": 10}, expect=201)["loan"]
        lid = self.state["loan"] = loan["id"]
        self.call("post", f"/loans/{lid}/workflow", json={"action": "submit"}, expect=200)
        self.call("post", f"/loans/{lid}/workflow", json={"action": "review", "remarks": "Documents checked"}, expect=200)
        self.call("post", f"/loans/{lid}/workflow", json={"action": "approve"}, expect=422)  # no resolution
        self.call("post", f"/loans/{lid}/disburse", key=self.key(), json={"authorizedByMemberId": m[1]}, expect=409)  # not approved
        mt = self.call("post", "/meetings", json={"groupId": gid, "meetingDate": date.today().isoformat(), "agenda": "Loan of Sunita"}, expect=201)["meeting"]
        self.state["meeting"] = mt["id"]
        res_payload = {"resolutionType": "LOAN_APPROVAL", "subject": "Approve loan", "loanId": lid, "amount": 1000,
                       "votesFor": 3, "votesAgainst": 0}
        self.call("post", f"/meetings/{mt['id']}/resolutions", json=res_payload, expect=409)  # not held yet
        self.call("post", f"/meetings/{mt['id']}/attendance", json={"entries": [{"memberId": x, "present": True} for x in m]}, expect=200)
        self.call("post", f"/meetings/{mt['id']}/resolutions", json={**res_payload, "votesFor": 4}, expect=422)  # borrower voted
        res = self.call("post", f"/meetings/{mt['id']}/resolutions", json=res_payload, expect=201)["resolution"]
        self.assertEqual(res["result"], "Passed")
        loan = self.call("post", f"/loans/{lid}/workflow", json={"action": "approve", "resolutionId": res["id"]}, expect=200)["loan"]
        self.assertEqual(loan["status"], "Approved")
        # The same resolution cannot approve a second loan
        other = self.call("post", "/loans", json={"memberId": m[2], "requestedAmount": 500, "purpose": "Goats", "installments": 5}, expect=201)["loan"]
        self.call("post", f"/loans/{other['id']}/workflow", json={"action": "submit"}, expect=200)
        self.call("post", f"/loans/{other['id']}/workflow", json={"action": "review", "remarks": "ok"}, expect=200)
        self.call("post", f"/loans/{other['id']}/workflow", json={"action": "approve", "resolutionId": res["id"]}, expect=409)
        self.call("post", f"/loans/{other['id']}/workflow", json={"action": "reject", "remarks": "Re-apply next month"}, expect=200)

    def test_06_disburse_repay_reverse(self):
        m, lid, gid = self.state["members"], self.state["loan"], self.state["group"]
        self.call("post", f"/loans/{lid}/disburse", key=self.key(), json={"authorizedByMemberId": m[0]}, expect=422)  # borrower
        self.call("post", f"/loans/{lid}/disburse", key=self.key(), json={"authorizedByMemberId": m[2]}, expect=422)  # secretary
        cash_before = self.call("get", f"/accounts/summary?groupId={gid}", expect=200)["cash"]
        d = self.call("post", f"/loans/{lid}/disburse", key=self.key(), json={"authorizedByMemberId": m[1]}, expect=201)
        self.assertEqual(len(d["schedule"]), 10)
        self.assertEqual(sum(r["principal"] for r in d["schedule"]), 1000)
        self.assertEqual(sum(r["interest"] for r in d["schedule"]), 100)  # 1000 x 12% x 10/12
        self.assertEqual(self.call("get", f"/accounts/summary?groupId={gid}")["cash"], cash_before - 1000)
        self.call("post", f"/loans/{lid}/repay", key=self.key(), json={"amount": 5000}, expect=422)  # more than owed
        rep = self.call("post", f"/loans/{lid}/repay", key=self.key(), json={"amount": 110}, expect=201)
        self.assertEqual(rep["transaction"]["principalAmount"], 100)
        self.assertEqual(rep["transaction"]["interestAmount"], 10)
        rev = self.call("post", f"/transactions/{rep['transaction']['id']}/reverse", key=self.key(), json={"reason": "Wrong member"}, expect=201)
        self.assertEqual(rev["original"]["postingStatus"], "Reversed")
        self.call("post", f"/transactions/{rep['transaction']['id']}/reverse", key=self.key(), json={"reason": "again"}, expect=409)
        detail = self.call("get", f"/loans/{lid}", expect=200)
        self.assertEqual(detail["schedule"][0]["principalPaid"], 0)
        self.assertEqual(detail["loan"]["principalOutstanding"], 1000)

    def test_07_books_balance(self):
        gid = self.state["group"]
        s = self.call("get", f"/accounts/summary?groupId={gid}", expect=200)
        tb = s["trialBalance"]
        self.assertAlmostEqual(sum(r["debit"] for r in tb), sum(r["credit"] for r in tb), places=2)
        self.assertTrue(s["financialPosition"]["balanced"], s["financialPosition"])
        cb = self.call("get", f"/accounts/cashbook?groupId={gid}", expect=200)["cashbook"]
        self.assertEqual(cb["closing"], s["cash"])
        dash = self.call("get", "/dashboard", expect=200)
        self.assertEqual(dash["loanOutstanding"], 1000)
        self.assertEqual(dash["savingsLiability"], 300 + 400 * 3 + 25)

    def test_08_expense_rules_and_period_close(self):
        gid = self.state["group"]
        self.call("post", "/transactions", key=self.key(), json={"groupId": gid, "txnType": "OPERATING_EXPENSE", "amount": 1500,
                                                                 "narration": "Stationery"}, expect=422)
        self.call("post", "/transactions", key=self.key(), json={"groupId": gid, "txnType": "OPERATING_EXPENSE", "amount": 200,
                                                                 "narration": "Stationery"}, expect=201)
        self.call("post", "/transactions", key=self.key(), json={"groupId": gid, "txnType": "OPERATING_EXPENSE", "amount": 999999,
                                                                 "narration": "Too much", "resolutionId": None}, expect=422)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.call("post", "/accounts/period-close", json={"groupId": gid, "periodEnd": yesterday}, expect=201)
        self.call("post", "/savings/collect", key=self.key(), json={"memberId": self.state["members"][0], "amount": 10,
                                                                     "txnDate": yesterday}, expect=409)

    def test_09_ledger_is_immutable(self):
        import psycopg
        with psycopg.connect(TEST_DB) as conn:
            with self.assertRaises(psycopg.Error):
                conn.execute("UPDATE shg_ledger_lines SET debit = debit + 1 WHERE id = (SELECT MIN(id) FROM shg_ledger_lines)")

    def test_10_foreclose_and_reports(self):
        lid = self.state["loan"]
        q = self.call("get", f"/loans/{lid}/quote", expect=200)["quote"]
        self.call("post", f"/loans/{lid}/repay", key=self.key(), json={"amount": q["total"], "foreclose": True}, expect=201)
        self.assertEqual(self.call("get", f"/loans/{lid}")["loan"]["status"], "Closed")
        catalog = self.call("get", "/reports", expect=200)["reports"]
        self.assertGreaterEqual(len(catalog), 23)
        gid, mid = self.state["group"], self.state["members"][0]
        for r in catalog:
            data = self.call("get", f"/reports/{r['key']}?groupId={gid}&memberId={mid}", expect=200)
            self.assertIn("rows", data)
        x = self.call("get", f"/reports/cashbook?groupId={gid}&format=xlsx", expect=200)
        self.assertTrue(x["fileBase64"])

    def test_11_member_exit_settlement(self):
        mid = self.state["members"][3]
        quote = self.call("get", f"/members/{mid}/settlement", expect=200)["settlement"]
        self.assertEqual(quote["savingsBalance"], 425)
        out = self.call("post", f"/members/{mid}/exit", key=self.key(), json={"reason": "Moved village"}, expect=200)
        self.assertEqual(out["refund"], 425)
        self.assertEqual(self.call("get", f"/members/{mid}")["member"]["status"], "Exited")
        # Her mobile number is free again for a new membership; exited members cannot transact.
        self.call("post", "/savings/collect", key=self.key(), json={"memberId": mid, "amount": 10}, expect=409)

    def _resolution(self, rtype, subject, amount=None, loan_id=None):
        res = self.call("post", f"/meetings/{self.state['meeting']}/resolutions", json={
            "resolutionType": rtype, "subject": subject, "amount": amount, "loanId": loan_id, "votesFor": 2, "votesAgainst": 0,
        }, expect=201)["resolution"]
        return res["id"]

    def test_12_reschedule_waive_writeoff_and_disbursement_reversal(self):
        m = self.state["members"]
        # New loan for member 2 (Asha, President), approved by a resolution, then disbursal reversed and redone.
        loan = self.call("post", "/loans", json={"memberId": m[1], "requestedAmount": 1200, "purpose": "Dairy cow",
                                                 "interestMethod": "REDUCING", "interestRate": 18, "installments": 6}, expect=201)["loan"]
        lid = loan["id"]
        self.call("post", f"/loans/{lid}/workflow", json={"action": "submit"}, expect=200)
        self.call("post", f"/loans/{lid}/workflow", json={"action": "review", "remarks": "ok"}, expect=200)
        rid = self._resolution("LOAN_APPROVAL", "Approve Asha", 1200, lid)
        self.call("post", f"/loans/{lid}/workflow", json={"action": "approve", "resolutionId": rid}, expect=200)
        d1 = self.call("post", f"/loans/{lid}/disburse", key=self.key(), json={"authorizedByMemberId": m[2]}, expect=422)  # secretary
        d1 = self.call("post", f"/loans/{lid}/disburse", key=self.key(), json={"authorizedByMemberId": m[0]}, expect=422)  # ordinary member
        # Treasurer exited in test_11, so make Meena's replacement: assign member 0 as Treasurer
        self.call("post", f"/members/{m[0]}/role", json={"role": "Treasurer"}, expect=200)
        d1 = self.call("post", f"/loans/{lid}/disburse", key=self.key(), json={"authorizedByMemberId": m[0]}, expect=201)
        self.call("post", f"/transactions/{d1['transaction']['id']}/reverse", key=self.key(), json={"reason": "Wrong amount"}, expect=201)
        self.assertEqual(self.call("get", f"/loans/{lid}")["loan"]["status"], "Approved")
        d2 = self.call("post", f"/loans/{lid}/disburse", key=self.key(), json={"authorizedByMemberId": m[0]}, expect=201)
        self.assertEqual(len(d2["schedule"]), 6)
        detail = self.call("get", f"/loans/{lid}", expect=200)
        self.assertEqual(len(detail["schedule"]), 6)  # superseded schedule not shown as current
        # Reschedule needs its own resolution
        self.call("post", f"/loans/{lid}/reschedule", json={"installments": 10, "firstDueDate": (date.today() + timedelta(days=40)).isoformat()}, expect=422)
        rr = self._resolution("LOAN_RESCHEDULE", "Extend Asha", None, lid)
        r = self.call("post", f"/loans/{lid}/reschedule", json={"resolutionId": rr, "installments": 10,
                                                               "firstDueDate": (date.today() + timedelta(days=40)).isoformat()}, expect=200)
        self.assertEqual(round(sum(x["principal"] for x in r["schedule"]), 2), 1200)
        self.call("post", f"/loans/{lid}/reschedule", json={"resolutionId": rr, "installments": 8,
                                                           "firstDueDate": (date.today() + timedelta(days=40)).isoformat()}, expect=409)  # used
        # Nothing is due yet, so there is nothing to waive
        rw = self._resolution("LOAN_WAIVER", "Waive Asha", 100, lid)
        self.call("post", f"/loans/{lid}/waive", json={"resolutionId": rw, "heads": ["penalty"]}, expect=409)
        # Write-off needs a resolution covering the principal
        small = self._resolution("LOAN_WRITE_OFF", "Write off (too small)", 100, lid)
        self.call("post", f"/loans/{lid}/write-off", key=self.key(), json={"resolutionId": small, "reason": "Death of member"}, expect=409)
        full = self._resolution("LOAN_WRITE_OFF", "Write off Asha", 1200, lid)
        wo = self.call("post", f"/loans/{lid}/write-off", key=self.key(), json={"resolutionId": full, "reason": "Member deceased"}, expect=201)
        self.assertEqual(wo["loan"]["status"], "Written Off")
        self.assertEqual(wo["loan"]["principalOutstanding"], 0)
        self.call("post", f"/transactions/{wo['transaction']['id']}/reverse", key=self.key(), json={"reason": "x"}, expect=409)

    def test_13_savings_withdrawal_needs_resolution_and_respects_lien(self):
        m = self.state["members"]
        self.call("post", "/savings/withdraw", key=self.key(), json={"memberId": m[2], "amount": 100}, expect=422)  # no resolution
        rid = self._resolution("SAVINGS_WITHDRAWAL", "Rekha medical", 100)
        self.call("post", "/savings/withdraw", key=self.key(), json={"memberId": m[2], "amount": 500, "resolutionId": rid}, expect=422)  # > balance
        out = self.call("post", "/savings/withdraw", key=self.key(), json={"memberId": m[2], "amount": 100, "resolutionId": rid}, expect=201)
        self.assertEqual(out["savingsBalance"], 300)

    def test_14_minutes_approval_locks_meeting(self):
        mid = self.state["meeting"]
        self.call("post", f"/meetings/{mid}/confirm", json={"by": "President"}, expect=422)  # no minutes yet
        self.call("put", f"/meetings/{mid}", json={"minutes": "Savings collected; loans approved.", "decisions": "Start papad unit"}, expect=200)
        self.call("post", f"/meetings/{mid}/confirm", json={"by": "President"}, expect=200)
        out = self.call("post", f"/meetings/{mid}/confirm", json={"by": "Secretary"}, expect=200)["meeting"]
        self.assertEqual(out["status"], "Minutes Approved")
        self.call("put", f"/meetings/{mid}", json={"minutes": "changed"}, expect=409)
        self.call("post", f"/meetings/{mid}/resolutions", json={"resolutionType": "GENERAL", "subject": "late", "votesFor": 1}, expect=409)

    def test_15_business_cycle(self):
        gid, m = self.state["group"], self.state["members"]
        act = self.call("post", "/business/activities", json={"groupId": gid, "name": "Papad unit", "activityType": "Food Processing"}, expect=201)["activity"]
        aid = act["id"]
        papad = self.call("post", f"/business/activities/{aid}/products", json={"name": "Papad pack", "unit": "pack", "salePrice": 50, "purchasePrice": 30}, expect=201)["product"]
        flour = self.call("post", f"/business/activities/{aid}/products", json={"name": "Urad flour", "kind": "Raw Material", "unit": "kg", "purchasePrice": 120}, expect=201)["product"]
        sup = self.call("post", "/business/parties", json={"groupId": gid, "partyType": "Supplier", "name": "Mill"}, expect=201)["party"]
        cust = self.call("post", "/business/parties", json={"groupId": gid, "partyType": "Customer", "name": "Shop"}, expect=201)["party"]
        self.call("post", f"/business/activities/{aid}/orders", key=self.key(), json={"orderType": "SALE", "partyId": cust["id"],
                  "lines": [{"productId": papad["id"], "quantity": 5}]}, expect=422)  # no stock yet
        po = self.call("post", f"/business/activities/{aid}/orders", key=self.key(), json={"orderType": "PURCHASE", "partyId": sup["id"],
                       "lines": [{"productId": flour["id"], "quantity": 10}], "paidAmount": 600}, expect=201)
        self.assertEqual(po["order"]["totalAmount"], 1200)
        self.assertEqual(po["order"]["status"], "Partial")
        self.call("post", f"/business/activities/{aid}/production", json={"productId": papad["id"], "quantity": 40,
                  "consumption": [{"productId": flour["id"], "quantity": 4}]}, expect=201)
        so = self.call("post", f"/business/activities/{aid}/orders", key=self.key(), json={"orderType": "SALE", "partyId": cust["id"],
                       "lines": [{"productId": papad["id"], "quantity": 30}], "paidAmount": 1500}, expect=201)
        self.assertEqual(so["order"]["status"], "Paid")
        self.call("post", f"/business/orders/{po['order']['id']}/payments", key=self.key(), json={"amount": 700}, expect=422)  # > balance
        work = self.call("post", f"/business/activities/{aid}/work", json={"memberId": m[0], "description": "Rolling papad", "earnings": 200}, expect=201)["work"]
        self.call("post", "/business/work/pay", key=self.key(), json={"workIds": [work["id"]]}, expect=201)
        self.call("post", "/business/work/pay", key=self.key(), json={"workIds": [work["id"]]}, expect=409)
        detail = self.call("get", f"/business/activities/{aid}", expect=200)
        pl = detail["profitLoss"]
        self.assertEqual(pl["cashBasis"]["income"], 1500)
        self.assertEqual(pl["cashBasis"]["expenses"], 800)  # 600 supplier + 200 earnings
        self.assertEqual(pl["payables"], 600)
        stock = {p["name"]: p["stockQty"] for p in detail["products"]}
        self.assertEqual(stock, {"Papad pack": 10, "Urad flour": 6})
        s = self.call("get", f"/accounts/summary?groupId={gid}", expect=200)
        self.assertEqual(s["incomeExpenditure"]["business"]["surplus"], 700)
        self.assertTrue(s["financialPosition"]["balanced"])

    def test_16_schemes_and_reconciliation(self):
        gid = self.state["group"]
        sch = self.call("post", "/schemes", json={"groupId": gid, "applicationKind": "GOVT_SCHEME", "schemeName": "UMED Revolving Fund",
                                                 "amountRequested": 15000}, expect=201)["scheme"]
        sid = sch["id"]
        self.call("post", f"/schemes/{sid}/disbursements", key=self.key(), json={"amount": 15000}, expect=409)  # not approved
        for st in ("Submitted", "Under Verification"):
            self.call("post", f"/schemes/{sid}/status", json={"status": st}, expect=200)
        self.call("post", f"/schemes/{sid}/status", json={"status": "Approved", "amountSanctioned": 15000}, expect=200)
        self.call("post", f"/schemes/{sid}/disbursements", key=self.key(), json={"amount": 16000}, expect=422)  # > sanctioned
        self.call("post", f"/schemes/{sid}/disbursements", key=self.key(), json={"amount": 15000, "referenceNumber": "NEFT1"}, expect=201)
        self.call("post", f"/schemes/{sid}/utilizations", json={"amount": 20000, "purpose": "too much"}, expect=422)
        self.call("post", f"/schemes/{sid}/utilizations", json={"amount": 5000, "purpose": "Raw material"}, expect=201)
        fp = self.call("get", f"/accounts/summary?groupId={gid}")["financialPosition"]
        grant = next(x for x in fp["funds"] if x["account"] == "GRANT_FUND")
        self.assertEqual(grant["amount"], 15000)
        rec = self.call("get", f"/accounts/reconciliation?groupId={gid}", expect=200)
        ids = [u["txnId"] for u in rec["uncleared"]]
        self.assertTrue(ids)
        self.call("post", "/accounts/reconciliation/clear", json={"txnIds": ids}, expect=200)
        rec2 = self.call("get", f"/accounts/reconciliation?groupId={gid}", expect=200)
        self.assertEqual(rec2["netUncleared"], 0)
        saved = self.call("post", "/accounts/reconciliation", json={"groupId": gid, "bankAccountId": rec2["bankAccountId"],
                          "statementDate": date.today().isoformat(), "statementBalance": rec2["bookBalance"]}, expect=201)
        self.assertEqual(saved["reconciliation"]["difference"], 0)

    def test_18_online_savings_via_razorpay(self):
        """Member QR / payment link payments are posted as savings once, with the fee as a bank charge."""
        import hashlib, hmac, json
        from cryptography.fernet import Fernet
        import bachatgat_gateway as gw
        os.environ["DRIVER_KEYS_ENCRYPTION_KEY"] = os.environ.get("DRIVER_KEYS_ENCRYPTION_KEY") or Fernet.generate_key().decode()
        gid, m = self.state["group"], self.state["members"]
        now = int(__import__("time").time())
        pay = lambda pid, paise, fee=0: {"id": pid, "amount": paise, "fee": fee, "status": "captured", "vpa": "member@upi",
                                         "acquirer_data": {"rrn": "123456789012"}, "created_at": now}
        qr_payments = {"items": []}
        link_state = {"status": "created", "payments": []}

        def fake(method, path, payload=None, creds=None):
            if path.startswith("/payments?count"):
                return {"items": []}
            if path == "/payments/qr_codes" and method == "POST":
                return {"id": "qr_test_1", "image_url": "https://rzp.io/qr.png"}
            if path.startswith("/payments/qr_codes/qr_test_1/payments"):
                return qr_payments
            if path == "/payment_links" and method == "POST":
                return {"id": "plink_1", "short_url": "https://rzp.io/l/abc"}
            if path == "/payment_links/plink_1":
                return link_state
            if path == "/payments/pay_link1":
                return pay("pay_link1", 5000)
            raise AssertionError(f"unexpected Razorpay call {method} {path}")
        original = gw._razorpay_request
        gw._razorpay_request = fake
        try:
            self.call("post", f"/members/{m[0]}/payment-qr", expect=409)  # not connected yet
            self.call("put", f"/groups/{gid}/gateway", json={"keyId": "bad", "keySecret": "x"}, expect=422)
            st = self.call("put", f"/groups/{gid}/gateway", json={"keyId": "rzp_test_ABCDEFGH1234", "keySecret": "secretsecret",
                                                                 "webhookSecret": "whsec"}, expect=200)["gateway"]
            self.assertTrue(st["connected"])
            self.assertEqual(st["mode"], "test")
            self.assertTrue(st["webhookUrl"].endswith(f"/api/bachatgat/gateway-webhook/{gid}"))
            qr = self.call("post", f"/members/{m[0]}/payment-qr", expect=201)
            self.assertEqual(qr["qrImageUrl"], "https://rzp.io/qr.png")
            self.assertFalse(self.call("post", f"/members/{m[0]}/payment-qr", expect=200)["created"])
            link = self.call("post", f"/members/{m[0]}/payment-link", json={"amount": 50}, expect=201)["link"]
            self.assertEqual(link["shortUrl"], "https://rzp.io/l/abc")

            before = self.call("get", f"/savings/member-due?memberId={m[0]}")["savingsBalance"]
            qr_payments["items"] = [pay("pay_qr1", 10000, fee=236)]
            out = self.call("post", f"/groups/{gid}/gateway/sync?force=1", expect=200)
            self.assertEqual(out["posted"], 1)
            self.assertEqual(self.call("post", f"/groups/{gid}/gateway/sync?force=1")["posted"], 0)  # never twice
            self.assertEqual(self.call("get", f"/savings/member-due?memberId={m[0]}")["savingsBalance"], before + 100)
            txns = self.call("get", f"/transactions?groupId={gid}&type=BANK_CHARGES")["transactions"]
            self.assertTrue(any(t["amount"] == 2.36 for t in txns))

            # Webhook: forged signature refused, valid one posts a new payment
            event = {"event": "qr_code.credited", "payload": {"qr_code": {"entity": {"id": "qr_test_1"}},
                                                               "payment": {"entity": pay("pay_qr2", 2500)}}}
            raw = json.dumps(event).encode()
            bad = self.client.post(f"/api/bachatgat/gateway-webhook/{gid}", data=raw, headers={"X-Razorpay-Signature": "x"})
            self.assertEqual(bad.status_code, 400)
            sig = hmac.new(b"whsec", raw, hashlib.sha256).hexdigest()
            okr = self.client.post(f"/api/bachatgat/gateway-webhook/{gid}", data=raw, headers={"X-Razorpay-Signature": sig})
            self.assertEqual(okr.status_code, 200, okr.get_json())
            self.assertEqual(self.call("get", f"/savings/member-due?memberId={m[0]}")["savingsBalance"], before + 125)
            again = self.client.post(f"/api/bachatgat/gateway-webhook/{gid}", data=raw, headers={"X-Razorpay-Signature": sig})
            self.assertEqual(again.status_code, 200)
            self.assertEqual(self.call("get", f"/savings/member-due?memberId={m[0]}")["savingsBalance"], before + 125)

            # Payment link paid -> posted, link marked paid
            link_state.update({"status": "paid", "payments": [{"payment_id": "pay_link1", "status": "captured"}]})
            self.assertEqual(self.call("post", f"/groups/{gid}/gateway/sync?force=1")["posted"], 1)
            self.assertEqual(self.call("get", f"/savings/member-due?memberId={m[0]}")["savingsBalance"], before + 175)

            # Exited member's QR payment is kept as Unposted, not lost and not posted
            import psycopg
            with psycopg.connect(TEST_DB) as conn:
                conn.execute("INSERT INTO shg_member_gateway (member_id, group_id, qr_id, mode) VALUES (%s, %s, 'qr_exited', 'test')",
                             (m[3], gid))
            event2 = {"event": "qr_code.credited", "payload": {"qr_code": {"entity": {"id": "qr_exited"}},
                                                                "payment": {"entity": pay("pay_qr3", 1000)}}}
            raw2 = json.dumps(event2).encode()
            self.client.post(f"/api/bachatgat/gateway-webhook/{gid}", data=raw2,
                             headers={"X-Razorpay-Signature": hmac.new(b"whsec", raw2, hashlib.sha256).hexdigest()})
            unposted = self.call("get", f"/groups/{gid}/gateway/payments?status=Unposted")["payments"]
            self.assertEqual([p["gatewayPaymentId"] for p in unposted], ["pay_qr3"])
            self.assertIn("not active", unposted[0]["note"])
            self.call("post", f"/gateway-payments/{unposted[0]['id']}/retry", expect=409)
            self.assertTrue(self.call("get", f"/accounts/summary?groupId={gid}")["financialPosition"]["balanced"])
            # Another tenant cannot see or use the group's gateway
            self.call("get", f"/groups/{gid}/gateway", user=self.user_b, expect=404)
        finally:
            gw._razorpay_request = original

    def test_17_cross_tenant_documents_and_reports_match_ledger(self):
        gid = self.state["group"]
        docs = self.call("get", f"/documents?groupId={gid}", expect=200)["documents"]
        self.call("get", f"/documents/{docs[0]['id']}/content", user=self.user_b, expect=404)
        self.call("get", f"/documents?groupId={gid}", user=self.user_b, expect=404)
        self.call("get", f"/reports/cashbook?groupId={gid}", user=self.user_b, expect=404)
        # Cashbook report closing equals the ledger cash balance
        cb = self.call("get", f"/reports/cashbook?groupId={gid}", expect=200)
        cash = self.call("get", f"/accounts/summary?groupId={gid}")["cash"]
        self.assertEqual(cb["rows"][-1]["balance"], cash)
        tb = self.call("get", f"/reports/trial_balance?groupId={gid}", expect=200)
        self.assertAlmostEqual(tb["totals"]["debit"], tb["totals"]["credit"], places=2)


if __name__ == "__main__":
    unittest.main()
