import tempfile
import unittest
from pathlib import Path

from minutebooks import create_app


class MinuteBooksAPITestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self.tmpdir.name) / "test.sqlite3")
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test", "DATABASE": db_path})
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_register_seeds_defaults_and_authenticates(self):
        response = self.client.post(
            "/register",
            json={"username": "alex", "password": "strongpass", "currency": "USD"},
        )
        self.assertEqual(response.status_code, 201)

        categories = self.client.get("/categories")
        self.assertEqual(categories.status_code, 200)
        names = {item["name"] for item in categories.get_json()}
        self.assertTrue({"groceries", "entertainment", "transport", "dining", "utilities"}.issubset(names))

    def test_expense_pending_for_future_date(self):
        self.client.post(
            "/register",
            json={"username": "jamie", "password": "strongpass", "currency": "USD"},
        )
        categories = self.client.get("/categories").get_json()
        category_id = categories[0]["id"]

        response = self.client.post(
            "/expenses",
            json={"amount": 12.5, "category_id": category_id, "date_occurred": "2099-01-01"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.get_json()["is_pending"])

    def test_analytics_endpoint(self):
        self.client.post(
            "/register",
            json={"username": "sam", "password": "strongpass", "currency": "USD"},
        )

        income = self.client.post("/income", json={"amount": 150, "date": "2026-01-01"})
        self.assertEqual(income.status_code, 201)

        categories = self.client.get("/categories").get_json()
        category_id = categories[0]["id"]
        expense = self.client.post(
            "/expenses",
            json={"amount": 50, "category_id": category_id, "date_occurred": "2026-01-01"},
        )
        self.assertEqual(expense.status_code, 201)

        analytics = self.client.get("/analytics")
        self.assertEqual(analytics.status_code, 200)
        payload = analytics.get_json()
        self.assertEqual(payload["totals"]["total_income"], 150.0)
        self.assertEqual(payload["totals"]["total_expenses"], 50.0)
        self.assertEqual(payload["balance"], 100.0)


if __name__ == "__main__":
    unittest.main()
