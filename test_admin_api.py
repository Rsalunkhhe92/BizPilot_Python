import unittest
from unittest.mock import MagicMock, patch

import app


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    @patch("app.psycopg.connect")
    def test_admin_dashboard_returns_counts(self, mock_connect):
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_connection
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(1,), (12, 9, 3)]

        response = self.client.get(
            "/api/admin/dashboard",
            headers={"X-User-Type": "admin", "X-User-Email": "admin@example.com"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["totalUsers"], 12)
        self.assertEqual(response.get_json()["activeUsers"], 9)
        self.assertEqual(response.get_json()["inactiveUsers"], 3)

    @patch("app.psycopg.connect")
    def test_admin_customer_list_returns_data(self, mock_connect):
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_connection
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [
            (1, "Jane Customer", "jane@example.com", "1990-08-15", "active", "monthly", "active")
        ]

        response = self.client.get(
            "/api/admin/drivers",
            headers={"X-User-Type": "admin", "X-User-Email": "admin@example.com"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["drivers"][0]["fullName"], "Jane Customer")
        self.assertEqual(payload["drivers"][0]["subscriptionPlan"], "monthly")

    @patch("app.psycopg.connect")
    def test_login_by_email_and_dob(self, mock_connect):
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_connection
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (1, "AutoLedger Admin", "admin@example.com", "admin", "active")

        response = self.client.post(
            "/api/login",
            json={"email": "admin@example.com", "dob": "1992-08-01"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["user"]["id"], 1)
        self.assertEqual(response.get_json()["user"]["userType"], "admin")


if __name__ == "__main__":
    unittest.main()
