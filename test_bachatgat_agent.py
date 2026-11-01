"""
End-to-end test of the Mahila Bachat Gat Autopilot against a real, throwaway PostgreSQL database
(same rules as test_bachatgat_api.py: set BACHATGAT_TEST_DATABASE_URL; never the app database).
Gemini is replaced by a fake register reader, so no AI call is made.
"""

import base64
import os
import unittest
import uuid
from datetime import date, timedelta

TEST_DB = os.getenv("BACHATGAT_TEST_DATABASE_URL")


def _configured_db():
    from dotenv import dotenv_values
    return (dotenv_values(os.path.join(os.path.dirname(__file__), ".env")) or {}).get("DATABASE_URL")


@unittest.skipUnless(TEST_DB, "set BACHATGAT_TEST_DATABASE_URL to an empty test database to run")
class AutopilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if TEST_DB == _configured_db() or TEST_DB == os.getenv("DATABASE_URL"):
            raise RuntimeError("Refusing to run: BACHATGAT_TEST_DATABASE_URL must not be the application database")
        os.environ["BACHATGAT_DATABASE_URL"] = TEST_DB
        os.environ.setdefault("AUTH_SECRET", "bachatgat-test-secret")
        os.environ["GEMINI_API_KEY"] = "test-key"
        os.environ.pop("SHG_AGENT_CRON_SECRET", None)
        import psycopg
        from flask import Flask
        from auth_token import issue_token
        from bachatgat_api import bachatgat_bp
        from bachatgat_service import ensure_bachatgat_schema

        run = uuid.uuid4().hex[:8]
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
                ids = []
                for name in ("Agent A", "Agent B"):
                    cur.execute(
                        "INSERT INTO userdetails (full_name, email, dob, business_type, user_type) "
                        "VALUES (%s, %s, '1990-01-01', 'Mahila Bachat Gat', 'customer') RETURNING id",
                        (name, f"{name.replace(' ', '').lower()}-{run}@test.local"),
                    )
                    ids.append(cur.fetchone()[0])
            conn.commit()
        cls.user_a, cls.user_b = ids
        cls.tokens = {uid: issue_token(uid) for uid in ids}
        app = Flask("bachatgat-agent-test")
        app.register_blueprint(bachatgat_bp)
        cls.client = app.test_client()
        cls.seed = int(run, 16) % 10_000_000
        cls.state = {}

    def call(self, method, path, user=None, json=None, expect=None):
        headers = {"Authorization": f"Bearer {self.tokens[user or self.user_a]}"}
        res = getattr(self.client, method)(f"/api/bachatgat{path}", json=json, headers=headers)
        data = res.get_json()
        if expect is not None:
            self.assertEqual(res.status_code, expect, f"{method.upper()} {path}: {data}")
        return data

    def mobile(self, n):
        return f"8{(self.seed + n) % 1_000_000_000:09d}"

    def types(self, run, status="Done"):
        return [a["actionType"] for a in run["actions"] if a["status"] == status]

    def test_1_setup_and_first_run(self):
        today = date.today()
        g = self.call("post", "/groups", json={
            "nameEn": "Jijau Mahila Bachat Gat", "formationDate": (today - timedelta(days=400)).isoformat(),
            "village": "Baramati", "district": "Pune", "state": "Maharashtra",
            "bankName": "Bank of Maharashtra", "accountNumber": f"7{self.seed:010d}", "ifsc": "MAHB0000123",
            "policy": {"minMembers": 3, "savingsAmount": 100, "minMembershipMonths": 0, "savingsMultiple": 4,
                       "loanApprovalLimit": 0, "penaltyType": "NONE"},
        }, expect=201)["group"]
        gid = self.state["group"] = g["id"]
        members = []
        for i, name in enumerate(["Sunita Pawar", "Asha Jadhav", "Rekha More", "Meena Shinde"]):
            m = self.call("post", "/members", json={"groupId": gid, "fullName": name, "mobile": self.mobile(i),
                                                    "joiningDate": (today - timedelta(days=300)).isoformat(),
                                                    "idProofType": "Aadhaar", "idProofNumber": "123412341234"}, expect=201)["member"]
            self.call("post", f"/members/{m['id']}/workflow", json={"action": "verify"}, expect=200)
            self.call("post", f"/members/{m['id']}/workflow", json={"action": "approve"}, expect=200)
            members.append(m["id"])
        for mid, role in zip(members[1:], ["President", "Secretary", "Treasurer"]):
            self.call("post", f"/members/{mid}/role", json={"role": role}, expect=200)
        self.call("post", f"/groups/{gid}/workflow", json={"action": "submit"}, expect=200)
        officer = self.call("post", f"/groups/{gid}/officers", json={"fullName": "CRP Kale", "designation": "Field Officer"}, expect=201)["officer"]
        self.call("post", "/documents", json={"entityType": "GROUP", "entityId": gid, "documentType": "Formation resolution",
                                               "mimeType": "application/pdf", "fileBase64": base64.b64encode(b"%PDF-1.4 t").decode()}, expect=201)
        self.call("post", f"/groups/{gid}/workflow", json={"action": "approve", "officerId": officer["id"]}, expect=200)
        # One new member with ID details (Autopilot approves her), one without (needs attention)
        with_id = self.call("post", "/members", json={"groupId": gid, "fullName": "Kavita Gaikwad", "mobile": self.mobile(10),
                                                      "joiningDate": today.isoformat(), "idProofType": "Aadhaar",
                                                      "idProofNumber": "999988887777"}, expect=201)["member"]
        no_id = self.call("post", "/members", json={"groupId": gid, "fullName": "Lata Kamble", "mobile": self.mobile(11),
                                                    "joiningDate": today.isoformat()}, expect=201)["member"]
        self.state.update(members=members, with_id=with_id["id"], no_id=no_id["id"])

        ov = self.call("put", f"/agent/settings/{gid}", json={"autoLoanLimit": 300, "disburseMethod": "Cash"}, expect=200)
        self.assertEqual(ov["settings"]["effectiveLoanLimit"], 300)
        self.call("put", f"/agent/settings/{gid}", user=self.user_b, json={"enabled": False}, expect=404)  # tenant isolation

        run = self.call("post", "/agent/run", json={"groupId": gid}, expect=200)["run"]
        done = self.types(run)
        self.assertIn("MEMBER_APPROVED", done)
        self.assertIn("DUES_GENERATED", done)
        self.assertIn("MEETING_SCHEDULED", done)
        self.assertIn("MEMBER_NEEDS_ID", self.types(run, "Needs Attention"))
        member = self.call("get", f"/members/{with_id['id']}", expect=200)["member"]
        self.assertEqual(member["status"], "Active")
        # A second run changes nothing (idempotent) and the attention item is not duplicated
        run2 = self.call("post", "/agent/run", json={"groupId": gid}, expect=200)["run"]
        self.assertEqual(self.types(run2), [])
        ov = self.call("get", f"/agent/overview?groupId={gid}", expect=200)
        self.assertEqual(sum(1 for a in ov["attention"] if a["actionType"] == "MEMBER_NEEDS_ID"), 1)

    def test_2_register_photo(self):
        import bachatgat_agent as agent
        gid, m = self.state["group"], self.state["members"]
        today = date.today().isoformat()
        extracted = {
            "meetingDate": today,
            "rows": [
                {"memberNumber": 1, "nameAsWritten": "सुनीता पवार", "present": True, "savings": 500},
                {"memberNumber": 2, "nameAsWritten": "आशा जाधव", "present": True, "savings": 500, "loanRequestAmount": 300,
                 "loanPurpose": "Sewing machine", "loanInstallments": 6},
                {"memberNumber": 3, "nameAsWritten": "रेखा मोरे", "present": True, "savings": 500, "loanRequestAmount": 1500},
                {"memberNumber": 4, "nameAsWritten": "मीना शिंदे", "present": True, "savings": 500},
                {"memberNumber": None, "nameAsWritten": "Shobha ?", "present": True, "savings": 100},
            ],
            "newMembers": [{"name": "Pooja Salve", "mobile": self.mobile(20)}],
            "unreadable": [],
        }
        calls = []

        def fake_reader(image_b64, mime, roster, api_key):
            calls.append(roster)
            return extracted

        original = agent._read_register
        agent._read_register = fake_reader
        try:
            photo = base64.b64encode(b"\xff\xd8\xff\xe0 register page " + uuid.uuid4().bytes).decode()
            out = self.call("post", "/agent/scan", json={"groupId": gid, "imageBase64": photo, "mimeType": "image/jpeg"}, expect=201)
            self.assertIn("1. Sunita Pawar", calls[0])
            run = out["run"]
            done = self.types(run)
            self.assertEqual(done.count("SAVINGS_RECORDED"), 4)
            self.assertIn("ATTENDANCE_RECORDED", done)
            self.assertEqual(done.count("LOAN_REQUESTED"), 2)
            self.assertIn("MEMBER_ADDED", done)
            # 300 is within the limit: approved and paid out from the cash collected in the meeting
            self.assertIn("LOAN_APPROVED", done)
            self.assertIn("LOAN_DISBURSED", done)
            attention = self.types(run, "Needs Attention")
            self.assertIn("LOAN_NEEDS_APPROVAL", attention)  # 1500 is above the limit
            self.assertIn("SCAN_UNMATCHED", attention)
            # Same photo again is refused
            self.call("post", "/agent/scan", json={"groupId": gid, "imageBase64": photo, "mimeType": "image/jpeg"}, expect=409)
        finally:
            agent._read_register = original
        loans = self.call("get", f"/loans?groupId={gid}", expect=200)["loans"]
        small = next(l for l in loans if l["requestedAmount"] == 300)
        big = next(l for l in loans if l["requestedAmount"] == 1500)
        self.assertEqual(small["status"], "Active")
        self.assertEqual(big["status"], "Under Review")
        self.state.update(small=small["id"], big=big["id"])
        self.assertEqual(self.call("get", f"/savings/member-due?memberId={m[0]}")["savingsBalance"], 500)

    def test_3_owner_approval_then_undo(self):
        gid, m = self.state["group"], self.state["members"]
        self.call("post", f"/agent/loans/{self.state['big']}/approve", user=self.user_b, expect=404)
        out = self.call("post", f"/agent/loans/{self.state['big']}/approve", json={}, expect=200)
        self.assertIn("LOAN_DISBURSED", self.types(out["run"]))
        self.assertEqual(self.call("get", f"/loans/{self.state['big']}")["loan"]["status"], "Active")

        ov = self.call("get", f"/agent/overview?groupId={gid}", expect=200)
        self.assertFalse(any(a["actionType"] == "LOAN_NEEDS_APPROVAL" for a in ov["attention"]))
        disb = next(a for a in ov["recent"] if a["actionType"] == "LOAN_DISBURSED" and a["entityId"] == self.state["small"])
        self.assertTrue(disb["canUndo"])
        self.call("post", f"/agent/actions/{disb['id']}/undo", json={"reason": "Wrong member"}, expect=200)
        self.call("post", f"/agent/actions/{disb['id']}/undo", expect=409)  # once only
        self.assertEqual(self.call("get", f"/loans/{self.state['small']}")["loan"]["status"], "Approved")
        # The Autopilot leaves an undone loan to the people
        run = self.call("post", "/agent/run", json={"groupId": gid}, expect=200)["run"]
        self.assertNotIn("LOAN_DISBURSED", self.types(run))
        self.assertEqual(self.call("get", f"/loans/{self.state['small']}")["loan"]["status"], "Approved")

        sav = next(a for a in ov["recent"] if a["actionType"] == "SAVINGS_RECORDED" and a["entityId"] == m[0])
        self.call("post", f"/agent/actions/{sav['id']}/undo", json={}, expect=200)
        self.assertEqual(self.call("get", f"/savings/member-due?memberId={m[0]}")["savingsBalance"], 0)
        self.assertTrue(self.call("get", f"/accounts/summary?groupId={gid}")["financialPosition"]["balanced"])

    def test_5_payment_links_and_loan_link_posting(self):
        import time
        from cryptography.fernet import Fernet
        import bachatgat_gateway as gw
        os.environ["DRIVER_KEYS_ENCRYPTION_KEY"] = os.environ.get("DRIVER_KEYS_ENCRYPTION_KEY") or Fernet.generate_key().decode()
        gid, m = self.state["group"], self.state["members"]
        created, paid = [], {}

        def fake(method, path, payload=None, creds=None):
            if path == "/payment_links" and method == "POST":
                created.append(payload)
                return {"id": f"plink_a{len(created)}", "short_url": f"https://rzp.io/l/{len(created)}"}
            if path.startswith("/payment_links/") and path.endswith("/cancel"):
                return {"status": "cancelled"}
            if path.startswith("/payment_links/"):
                link_id = path.rsplit("/", 1)[1]
                if link_id in paid:
                    return {"status": "paid", "payments": [{"payment_id": paid[link_id], "status": "captured"}]}
                return {"status": "created"}
            if path.startswith("/payments/pay_"):
                pid = path.rsplit("/", 1)[1]
                return {"id": pid, "amount": 10000, "fee": 0, "status": "captured", "vpa": "m@upi", "created_at": int(time.time())}
            if path.startswith("/payments/qr_codes") or path.startswith("/payments?"):
                return {"items": []}
            raise AssertionError(f"unexpected Razorpay call {method} {path}")

        original = gw._razorpay_request
        gw._razorpay_request = fake
        try:
            self.call("put", f"/groups/{gid}/gateway", json={"keyId": "rzp_test_AGENT1234567", "keySecret": "secretsecret"}, expect=200)
            run = self.call("post", "/agent/run", json={"groupId": gid}, expect=200)["run"]
            sent = [a for a in run["actions"] if a["actionType"] == "PAYMENT_LINK_SENT"]
            self.assertTrue(sent)
            self.assertTrue(all(p["notify"]["sms"] for p in created))
            loan_links = [p for p in created if p["notes"]["kind"] == "shg_loan"]
            # Members owe overdue savings; loan installments are not due yet (first due next month)
            self.assertEqual(loan_links, [])
            # Running again sends nothing new while the links still match what is owed
            n = len(created)
            self.call("post", "/agent/run", json={"groupId": gid}, expect=200)
            self.assertEqual(len(created), n)

            # A paid loan link is posted as that loan's repayment, not as savings
            import psycopg
            with psycopg.connect(TEST_DB) as conn:
                conn.execute(
                    """INSERT INTO shg_payment_links (group_id, member_id, link_id, short_url, amount, mode, loan_id)
                       SELECT group_id, member_id, 'plink_loan', 'https://rzp.io/l/x', 100, 'test', id FROM shg_loans WHERE id = %s""",
                    (self.state["big"],))
            paid["plink_loan"] = "pay_loan1"
            before = self.call("get", f"/loans/{self.state['big']}")["loan"]["principalOutstanding"]
            run = self.call("post", "/agent/run", json={"groupId": gid}, expect=200)["run"]
            posted = [a for a in run["actions"] if a["actionType"] == "ONLINE_PAYMENT_POSTED"]
            self.assertEqual(len(posted), 1)
            self.assertIn("loan repayment", posted[0]["message"])
            after = self.call("get", f"/loans/{self.state['big']}")["loan"]["principalOutstanding"]
            self.assertLess(after, before)
            self.assertTrue(self.call("get", f"/accounts/summary?groupId={gid}")["financialPosition"]["balanced"])
        finally:
            gw._razorpay_request = original

    def test_4_cron_needs_secret(self):
        self.assertEqual(self.client.post("/api/bachatgat/agent/cron").status_code, 403)
        os.environ["SHG_AGENT_CRON_SECRET"] = "cron-secret"
        try:
            self.assertEqual(self.client.post("/api/bachatgat/agent/cron", headers={"X-Cron-Secret": "nope"}).status_code, 403)
            res = self.client.post("/api/bachatgat/agent/cron", headers={"X-Cron-Secret": "cron-secret"})
            self.assertEqual(res.status_code, 200, res.get_json())
        finally:
            os.environ.pop("SHG_AGENT_CRON_SECRET", None)


if __name__ == "__main__":
    unittest.main()
