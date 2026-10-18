import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class HissabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = str(Path(cls.tempdir.name) / "test.db")
        os.environ["APP_PASSWORD"] = "test-password"
        os.environ["APP_SECRET_KEY"] = "test-secret"
        import app as hissab
        cls.hissab = hissab
        cls.client = hissab.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        with self.hissab.connect_db() as db:
            db.execute("DELETE FROM ledger_movements")
            db.execute("DELETE FROM transactions")
            db.execute("DELETE FROM products")
            db.execute("UPDATE accounts SET opening_balance_paise = 0")
        with self.client.session_transaction() as session:
            session.clear()

    def sign_in(self):
        return self.client.post("/api/login", json={"password": "test-password"})

    def test_password_protects_bookkeeping_endpoints(self):
        self.assertEqual(self.client.get("/api/dashboard").status_code, 401)
        self.assertEqual(self.sign_in().status_code, 200)
        self.assertEqual(self.client.get("/api/dashboard").status_code, 200)

    def test_homepage_and_javascript_asset_are_served(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        response = self.client.get("/static/app.js")
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_production_refuses_to_start_without_postgres_url(self):
        environment = os.environ.copy()
        environment.update(APP_ENV="production", APP_PASSWORD="test", APP_SECRET_KEY="test")
        environment.pop("DATABASE_URL", None)
        result = subprocess.run(
            [sys.executable, "-c", "import app"],
            cwd=Path(__file__).parent,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set DATABASE_URL", result.stderr)

    def test_dashboard_shows_empty_books_after_sign_in(self):
        self.sign_in()
        response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["metrics"]["revenue_paise"], 0)
        self.assertEqual(response.json["metrics"]["month_expenses_paise"], 0)

    def test_postgres_connection_adapter_translates_placeholders(self):
        class FakeConnection:
            def execute(self, statement, parameters):
                self.statement = statement
                self.parameters = parameters
                return self

        raw = FakeConnection()
        db = self.hissab.DatabaseConnection(raw, postgres=True)
        db.execute("SELECT * FROM products WHERE id = ? AND sku = ?", (7, "ALM-1"))
        self.assertEqual(raw.statement, "SELECT * FROM products WHERE id = %s AND sku = %s")
        self.assertEqual(raw.parameters, (7, "ALM-1"))

    def test_purchase_sale_return_and_expense_update_stock_gst_and_cash(self):
        self.sign_in()
        response = self.client.post(
            "/api/products",
            json={"sku": "ALM-1", "name": "Almonds", "opening_stock": 10, "unit_cost": 100, "sell_price": 150, "gst_rate": 5},
        )
        self.assertEqual(response.status_code, 201)
        product_id = self.client.get("/api/products").json[0]["id"]
        purchase = self.client.post(
            "/api/transactions",
            json={"kind": "purchase", "description": "Supplier order", "product_id": product_id, "quantity": 10,
                  "unit_price": 100, "payment_account": "bank", "gst_credit_eligible": True},
        )
        self.assertEqual(purchase.status_code, 201, purchase.json)
        sale = self.client.post(
            "/api/transactions",
            json={"kind": "sale", "description": "Online order", "product_id": product_id, "quantity": 2,
                  "unit_price": 150, "payment_account": "marketplace", "channel": "Marketplace"},
        )
        self.assertEqual(sale.status_code, 201, sale.json)
        returned = self.client.post(
            "/api/transactions",
            json={"kind": "sales_return", "description": "Customer return", "product_id": product_id, "quantity": 1,
                  "amount": 150, "payment_account": "bank", "restocked": True},
        )
        self.assertEqual(returned.status_code, 201, returned.json)
        expense = self.client.post(
            "/api/transactions",
            json={"kind": "expense", "description": "Return shipping", "amount": 20, "gst_rate": 5,
                  "category": "Return shipping", "payment_account": "bank", "gst_credit_eligible": True},
        )
        self.assertEqual(expense.status_code, 201, expense.json)
        data = self.client.get("/api/dashboard").json
        self.assertEqual(data["metrics"]["revenue_paise"], 15000)
        self.assertEqual(data["metrics"]["inventory_units"], 19)
        self.assertEqual(data["metrics"]["gst_estimate_paise"], -4350)
        self.assertEqual(data["metrics"]["expenses_paise"], 2000)
        accounts = {account["account_key"]: account["balance_paise"] for account in data["accounts"]}
        self.assertEqual(accounts["bank"], -122850)
        self.assertEqual(accounts["marketplace"], 31500)

    def test_transfer_moves_cash_without_changing_profit(self):
        self.sign_in()
        self.client.post("/api/accounts/opening-balance", json={"account_key": "bank", "amount": 1000})
        result = self.client.post(
            "/api/transactions",
            json={"kind": "transfer", "date": "2026-10-18", "amount": 250, "from_account": "bank", "to_account": "savings"},
        )
        self.assertEqual(result.status_code, 201, result.json)
        data = self.client.get("/api/dashboard").json
        self.assertEqual(data["metrics"]["operating_profit_paise"], 0)
        balances = {account["account_key"]: account["balance_paise"] for account in data["accounts"]}
        self.assertEqual(balances["bank"], 75000)
        self.assertEqual(balances["savings"], 25000)

    def test_sale_rejects_insufficient_stock(self):
        self.sign_in()
        self.client.post(
            "/api/products",
            json={"sku": "CAS-1", "name": "Cashews", "opening_stock": 0, "unit_cost": 100, "sell_price": 150, "gst_rate": 0},
        )
        product_id = self.client.get("/api/products").json[0]["id"]
        response = self.client.post(
            "/api/transactions",
            json={"kind": "sale", "description": "Local sale", "product_id": product_id, "quantity": 1, "unit_price": 150},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Not enough stock", response.json["error"])

    def test_invalid_payment_account_does_not_partially_record_sale(self):
        self.sign_in()
        created = self.client.post(
            "/api/products",
            json={"sku": "ATOMIC-1", "name": "Atomic test product", "opening_stock": 5,
                  "unit_cost": 100, "sell_price": 150, "gst_rate": 0},
        )
        self.assertEqual(created.status_code, 201, created.json)
        product_id = self.client.get("/api/products").json[0]["id"]

        response = self.client.post(
            "/api/transactions",
            json={"kind": "sale", "description": "Invalid account sale", "product_id": product_id,
                  "quantity": 2, "unit_price": 150, "payment_account": "missing"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/products").json[0]["stock_qty"], 5)
        self.assertEqual(self.client.get("/api/transactions").json, [])

    def test_partner_contribution_and_withdrawal_net_to_zero(self):
        self.sign_in()
        for kind in ("partner_contribution", "partner_withdrawal"):
            response = self.client.post(
                "/api/transactions",
                json={"kind": kind, "description": kind, "partner": "A partner",
                      "amount": 100, "payment_account": "bank"},
            )
            self.assertEqual(response.status_code, 201, response.json)

        dashboard = self.client.get("/api/dashboard").json
        self.assertEqual(dashboard["partners"][0]["balance_paise"], 0)
        bank = next(account for account in dashboard["accounts"] if account["account_key"] == "bank")
        self.assertEqual(bank["balance_paise"], 0)

    def test_invalid_transaction_date_returns_a_client_error(self):
        self.sign_in()
        response = self.client.post(
            "/api/transactions",
            json={"kind": "expense", "date": "not-a-date", "description": "Packaging", "amount": 10},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("YYYY-MM-DD", response.json["error"])

    def test_csv_export_escapes_spreadsheet_formula_cells(self):
        self.sign_in()
        response = self.client.post(
            "/api/transactions",
            json={"kind": "expense", "description": "=HYPERLINK(\"https://example.invalid\")", "amount": 10},
        )
        self.assertEqual(response.status_code, 201, response.json)
        exported = self.client.get("/api/export")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("'=HYPERLINK", exported.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
