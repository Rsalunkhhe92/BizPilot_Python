"""
Unit tests for Collection API endpoints
"""

import unittest
from datetime import date
from decimal import Decimal
import json

from app import app


class CollectionApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.collector_id = 36
        self.target_date = "2026-09-07"

    def test_01_dashboard_returns_50_customers(self):
        response = self.client.get(f"/api/collection/dashboard?collector_id={self.collector_id}&date={self.target_date}")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["totalAssigned"], 50)
        self.assertEqual(data["collector"]["fullName"], "Riya N")

    def test_02_today_schedule_returns_customers(self):
        response = self.client.get(f"/api/collection/today?collector_id={self.collector_id}&date={self.target_date}")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["total"], 50)
        first_cust = data["customers"][0]
        self.assertIn("customerName", first_cust)
        self.assertIn("expectedAmount", first_cust)
        self.assertIn("totalDue", first_cust)

    def test_03_payment_validation(self):
        # 1. Zero amount should fail
        res = self.client.post("/api/collection/collect", json={
            "scheduleId": 1,
            "collectorId": self.collector_id,
            "amount": 0,
            "paymentMethod": "CASH"
        })
        self.assertEqual(res.status_code, 400)

        # 2. UPI without transactionRef should fail
        res = self.client.post("/api/collection/collect", json={
            "scheduleId": 1,
            "collectorId": self.collector_id,
            "amount": 500,
            "paymentMethod": "UPI",
            "transactionRef": ""
        })
        self.assertEqual(res.status_code, 400)

    def test_04_collect_cash_payment_generates_receipt(self):
        # Fetch first schedule item
        sched_res = self.client.get(f"/api/collection/today?collector_id={self.collector_id}&date={self.target_date}")
        schedules = sched_res.get_json()["customers"]
        target = schedules[0]
        s_id = target["scheduleId"]

        # Collect full expected payment
        res = self.client.post("/api/collection/collect", json={
            "scheduleId": s_id,
            "collectorId": self.collector_id,
            "amount": target["expectedAmount"],
            "paymentMethod": "CASH",
            "notes": "Test cash collection"
        })
        self.assertIn(res.status_code, (201, 409))
        if res.status_code == 201:
            data = res.get_json()
            self.assertIn("receipt", data)
            self.assertTrue(data["receipt"]["receiptNumber"].startswith("REC-20260907-"))
            self.assertEqual(data["schedule"]["status"], "COLLECTED")
            self.assertGreater(data["summary"]["cashAmount"], 0)

    def test_05_update_status_not_available(self):
        sched_res = self.client.get(f"/api/collection/today?collector_id={self.collector_id}&date={self.target_date}")
        schedules = sched_res.get_json()["customers"]
        target = schedules[2]  # 3rd customer
        s_id = target["scheduleId"]

        res = self.client.post("/api/collection/status", json={
            "scheduleId": s_id,
            "status": "NOT_AVAILABLE",
            "reason": "Shop closed / Customer out of town",
            "notes": "Visited at 11:30 AM"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "NOT_AVAILABLE")

    def test_06_reschedule_collection(self):
        sched_res = self.client.get(f"/api/collection/today?collector_id={self.collector_id}&date={self.target_date}")
        schedules = sched_res.get_json()["customers"]
        target = schedules[3]  # 4th customer
        s_id = target["scheduleId"]

        res = self.client.post("/api/collection/reschedule", json={
            "scheduleId": s_id,
            "reason": "Requested visit tomorrow afternoon",
            "nextFollowupDate": "2026-09-08",
            "notes": "Confirmed on phone"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "RESCHEDULED")
        self.assertEqual(data["nextFollowupDate"], "2026-09-08")

    def test_07_missed_collections_endpoint(self):
        res = self.client.get(f"/api/collection/missed?collector_id={self.collector_id}&date={self.target_date}")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertGreater(data["totalMissed"], 0)


if __name__ == "__main__":
    unittest.main()
