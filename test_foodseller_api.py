"""
End-to-end tests for the Food Seller API against a real PostgreSQL database.

They CREATE TABLES and WRITE DATA, so they only run against a database you name
explicitly, never the DATABASE_URL in .env:

    $env:FOODSELLER_TEST_DATABASE_URL = "postgresql://user@127.0.0.1:5432/empty_test_db"
    .venv\\Scripts\\python.exe -m unittest test_foodseller_api -v

The suite does not import app.py; it mounts only the Food Seller blueprint.
"""

import base64
import json
import os
import threading
import unittest
import uuid
from datetime import timedelta
from unittest import mock

TEST_DB = os.getenv("FOODSELLER_TEST_DATABASE_URL")


def _configured_db():
    from dotenv import dotenv_values
    return (dotenv_values(os.path.join(os.path.dirname(__file__), ".env")) or {}).get("DATABASE_URL")


@unittest.skipUnless(TEST_DB, "set FOODSELLER_TEST_DATABASE_URL to an empty test database to run")
class FoodSellerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if TEST_DB == _configured_db() or TEST_DB == os.getenv("DATABASE_URL"):
            raise RuntimeError("Refusing to run: FOODSELLER_TEST_DATABASE_URL must not be the application database")
        os.environ["FOODSELLER_DATABASE_URL"] = TEST_DB
        os.environ.setdefault("AUTH_SECRET", "foodseller-test-secret")
        os.environ.pop("GEMINI_API_KEY", None)
        import psycopg
        from flask import Flask
        from auth_token import issue_token
        from foodseller_api import foodseller_bp
        from foodseller_common import today_ist
        from foodseller_service import ensure_foodseller_schema

        cls.today = today_ist()
        cls.psycopg = psycopg
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
                ensure_foodseller_schema(cur)
                ensure_foodseller_schema(cur)  # idempotent re-run
                run = uuid.uuid4().hex[:8]
                ids = []
                for name, btype, feats in (("Food A", "Food Seller", "[]"), ("Food B", "Food Seller", "[]"),
                                           ("Fruit", "Fruit sellers", '["products"]'),
                                           ("No reports", "Food Seller", '["food_inventory"]')):
                    cur.execute(
                        "INSERT INTO userdetails (full_name, email, dob, business_type, user_type, enabled_features) "
                        "VALUES (%s, %s, '1990-01-01', %s, 'customer', %s::jsonb) RETURNING id",
                        (name, f"{name.replace(' ', '').lower()}-{run}@test.local", btype, feats),
                    )
                    ids.append(cur.fetchone()[0])
            conn.commit()
        cls.a, cls.b, cls.fruit, cls.limited = ids
        cls.tokens = {uid: issue_token(uid) for uid in ids}
        app = Flask("foodseller-test")
        app.register_blueprint(foodseller_bp)
        cls.app = app
        cls.client = app.test_client()

    # helpers --------------------------------------------------------------
    def call(self, method, path, user=None, json=None, expect=None, idem=False, key=None):
        headers = {"Authorization": f"Bearer {self.tokens[user or self.a]}"}
        if idem or key:
            headers["Idempotency-Key"] = key or uuid.uuid4().hex
        res = getattr(self.client, method)(f"/api/foodseller{path}", json=json, headers=headers)
        data = res.get_json()
        if expect is not None:
            self.assertEqual(res.status_code, expect, f"{method.upper()} {path}: {data}")
        return data

    def units(self, user=None):
        return {u["name"]: u["id"] for u in self.call("get", "/bootstrap", user=user, expect=200)["units"]}

    def product(self, name, unit="KG", price=0, opening=0, minimum=0, user=None):
        return self.call("post", "/products", user=user, expect=201, json={
            "name": name, "unitId": self.units(user)[unit], "purchasePrice": price, "openingStock": opening, "minStockAlert": minimum,
        })["product"]

    def stock(self, product_id, user=None):
        return self.call("get", f"/products/{product_id}", user=user, expect=200)["product"]["stockQty"]

    def buy(self, items, user=None, **extra):
        return self.call("post", "/purchases", user=user, idem=True, expect=201, json={"items": items, **extra})

    def food(self, name, price, yield_qty=None, ingredients=None, user=None, **extra):
        payload = {"name": name, "sellingPrice": price, **extra}
        if yield_qty:
            payload["recipe"] = {"yieldQty": yield_qty, "ingredients": ingredients}
        return self.call("post", "/food-products", user=user, expect=201, json=payload)["food"]

    def sell(self, food_id, qty, user=None, expect=201, **extra):
        return self.call("post", "/sales", user=user, idem=True, expect=expect,
                         json={"items": [{"foodProductId": food_id, "quantity": qty}], "paymentMode": "Cash", **extra})

    def use(self, product_id, qty, unit_id=None, purpose="PREPARATION", user=None, expect=201, **extra):
        item = {"productId": product_id, "quantity": qty, "purpose": purpose}
        if unit_id:
            item["unitId"] = unit_id
        return self.call("post", "/consumption", user=user, idem=True, expect=expect, json={"items": [item], **extra})

    def dash(self, user=None):
        return self.call("get", "/dashboard", user=user, expect=200)

    def assertClose(self, a, b, places=2):
        self.assertAlmostEqual(float(a), float(b), places=places)

    # access ---------------------------------------------------------------
    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/foodseller/bootstrap").status_code, 401)

    def test_other_business_types_are_refused(self):
        self.call("get", "/bootstrap", user=self.fruit, expect=403)

    def test_admin_feature_switch_is_enforced(self):
        self.call("get", "/products", user=self.limited, expect=200)
        self.call("get", "/reports", user=self.limited, expect=403)
        self.call("get", "/sales", user=self.limited, expect=403)

    def test_business_isolation(self):
        own = self.product(f"Iso {uuid.uuid4().hex[:6]}", opening=5)
        self.units(self.b)
        self.call("get", f"/products/{own['id']}", user=self.b, expect=404)
        self.call("delete", f"/products/{own['id']}", user=self.b, expect=404)
        self.call("post", "/purchases", user=self.b, idem=True, expect=404, json={"items": [{"productId": own["id"], "quantity": 1, "rate": 1}]})
        self.call("post", "/consumption", user=self.b, idem=True, expect=404, json={"items": [{"productId": own["id"], "quantity": 1}]})
        listed = self.call("get", "/products", user=self.b, expect=200)["products"]
        self.assertNotIn(own["id"], [p["id"] for p in listed])
        # The database itself refuses a cross-business reference.
        with self.psycopg.connect(TEST_DB) as conn:
            with self.assertRaises(self.psycopg.errors.ForeignKeyViolation), conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO food_inventory_transactions (business_id, user_id, product_id, txn_type, quantity, value) VALUES (%s, %s, %s, 'PURCHASE', 1, 1)",
                    (self.b, self.b, own["id"]),
                )

    # setup ----------------------------------------------------------------
    def test_starter_data_is_ordinary_editable_rows(self):
        fresh_id = self._new_tenant()
        self.assertTrue(self.call("get", "/bootstrap", user=fresh_id, expect=200)["needsSetup"])
        added = self.call("post", "/setup/starter", user=fresh_id, json={"useStarter": True}, expect=201)
        self.assertEqual(added["products"], 21)
        self.assertEqual(added["foods"], 4)
        self.call("post", "/setup/starter", user=fresh_id, json={"useStarter": True}, expect=409)
        self.assertFalse(self.call("get", "/bootstrap", user=fresh_id, expect=200)["needsSetup"])
        products = self.call("get", "/products?q=Potato", user=fresh_id, expect=200)["products"]
        self.call("delete", f"/products/{products[0]['id']}", user=fresh_id, expect=409)  # in the Vada Pav recipe
        foods = {f["name"]: f for f in self.call("get", "/food-products", user=fresh_id, expect=200)["foods"]}
        self.assertEqual(len(foods["Vada Pav"]["recipe"]["ingredients"]), 7)
        self.assertEqual(foods["Cold Drink"]["productType"], "PACKAGED")
        # Products nobody has stocked yet are not alerts.
        self.assertEqual(self.call("get", "/notifications", user=fresh_id, expect=200)["notifications"], [])
        self.assertEqual(self.dash(fresh_id)["attentionCount"], 0)

    def _new_tenant(self):
        from auth_token import issue_token
        with self.psycopg.connect(TEST_DB) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO userdetails (full_name, email, dob, business_type, user_type) VALUES ('Fresh', %s, '1990-01-01', 'Food Seller', 'customer') RETURNING id",
                        (f"fresh-{uuid.uuid4().hex[:8]}@test.local",))
            uid = cur.fetchone()[0]
        self.tokens[uid] = issue_token(uid)
        return uid

    def test_categories_units_and_suppliers_are_dynamic(self):
        user = self._new_tenant()
        self.units(user)
        unit = self.call("post", "/units", user=user, expect=201, json={"name": "Tray", "dimension": "custom"})["unit"]
        self.call("post", "/units", user=user, expect=409, json={"name": "tray"})
        category = self.call("post", "/categories", user=user, expect=201, json={"name": "Dairy"})["category"]
        supplier = self.call("post", "/suppliers", user=user, expect=201, json={"name": "Patil Traders", "mobile": "9876543210"})["supplier"]
        product = self.call("post", "/products", user=user, expect=201, json={
            "name": "Paneer", "unitId": unit["id"], "categoryId": category["id"], "supplierId": supplier["id"], "purchasePrice": 200, "minStockAlert": 2, "sellingPrice": 300,
        })["product"]
        self.assertEqual(product["categoryName"], "Dairy")
        self.assertEqual(product["supplierName"], "Patil Traders")
        self.call("delete", f"/units/{unit['id']}", user=user, expect=409)
        self.call("delete", f"/categories/{category['id']}", user=user, expect=409)
        self.call("delete", f"/products/{product['id']}", user=user, expect=200)
        self.call("delete", f"/units/{unit['id']}", user=user, expect=200)

    # purchases ------------------------------------------------------------
    def test_purchase_example_from_the_brief(self):
        user = self._new_tenant()
        units = self.units(user)
        potato = self.product("Potato", "KG", user=user)
        oil = self.product("Oil", "Liter", user=user)
        pav = self.product("Pav", "Pieces", user=user)
        mirchi = self.product("Mirchi", "KG", user=user)
        gas = self.product("Gas", "Cylinder", user=user)
        supplier = self.call("post", "/suppliers", user=user, expect=201, json={"name": "Mandi"})["supplier"]
        result = self.buy([
            {"productId": potato["id"], "quantity": 30, "rate": 25}, {"productId": oil["id"], "quantity": 5, "rate": 150},
            {"productId": pav["id"], "quantity": 100, "rate": 4}, {"productId": mirchi["id"], "quantity": 2, "rate": 60},
            {"productId": gas["id"], "quantity": 1, "rate": 950},
        ], user=user, supplierId=supplier["id"], paymentMethod="Credit", amountPaid=970)
        self.assertEqual(result["purchase"]["totalAmount"], 2970)
        self.assertEqual(self.stock(potato["id"], user), 30)
        self.assertEqual(self.stock(pav["id"], user), 100)
        bought_in_grams = self.buy([{"productId": mirchi["id"], "quantity": 500, "unitId": units["Gram"], "rate": 0.1}], user=user)
        self.assertEqual(bought_in_grams["purchase"]["totalAmount"], 50)
        self.assertEqual(self.stock(mirchi["id"], user), 2.5)
        history = self.call("get", f"/purchases?from={self.today}&to={self.today}&supplierId={supplier['id']}", user=user, expect=200)["purchases"]
        self.assertEqual(len(history), 1)
        self.assertEqual(len(history[0]["items"]), 5)
        self.assertEqual(history[0]["due"], 2000)
        dash = self.dash(user)
        self.assertEqual(dash["today"]["purchases"], 3020)
        self.assertEqual(dash["today"]["netProfit"], 0)  # stock bought is not a cost
        self.assertEqual(dash["pendingPayable"], 2000)
        self.assertClose(dash["remainingStockValue"], 3020)

    def test_purchase_can_be_voided_and_payments_block_it(self):
        user = self._new_tenant()
        self.units(user)
        product = self.product("Rice", user=user)
        purchase = self.buy([{"productId": product["id"], "quantity": 10, "rate": 40}], user=user, paymentMethod="Credit")["purchase"]
        payment = self.call("post", "/payments", user=user, idem=True, expect=201,
                            json={"direction": "PAID", "purchaseId": purchase["id"], "amount": 100, "paymentMethod": "UPI"})["payment"]
        self.call("post", f"/purchases/{purchase['id']}/void", user=user, idem=True, expect=409)
        self.call("post", f"/payments/{payment['id']}/void", user=user, idem=True, expect=200)
        self.use(product["id"], 8, user=user)
        self.call("post", f"/purchases/{purchase['id']}/void", user=user, idem=True, expect=409)  # 8 used, 10 cannot be returned
        self.call("post", f"/purchases/{purchase['id']}/void", user=user, idem=True, expect=200, json={"allowNegative": True})
        self.assertEqual(self.stock(product["id"], user), -8)

    def test_purchase_rejects_bad_input(self):
        user = self._new_tenant()
        self.units(user)
        product = self.product("Salt", user=user)
        for bad in ([], [{"productId": product["id"], "quantity": -1, "rate": 1}], [{"productId": product["id"], "quantity": "abc", "rate": 1}],
                    [{"productId": product["id"], "quantity": "NaN", "rate": 1}], [{"productId": product["id"], "quantity": 1}]):
            self.call("post", "/purchases", user=user, idem=True, expect=422, json={"items": bad})
        tomorrow = (self.today + timedelta(days=1)).isoformat()
        self.call("post", "/purchases", user=user, idem=True, expect=422, json={"date": tomorrow, "items": [{"productId": product["id"], "quantity": 1, "rate": 1}]})
        self.call("post", "/purchases", user=user, expect=400, json={"items": [{"productId": product["id"], "quantity": 1, "rate": 1}]})
        wrong_unit = self.units(user)["Liter"]
        self.call("post", "/purchases", user=user, idem=True, expect=422, json={"items": [{"productId": product["id"], "quantity": 1, "unitId": wrong_unit, "rate": 1}]})
        self.assertEqual(self.stock(product["id"], user), 0)

    # consumption ----------------------------------------------------------
    def test_consumption_example_from_the_brief(self):
        user = self._new_tenant()
        units = self.units(user)
        potato = self.product("Potato", "KG", user=user)
        oil = self.product("Oil", "Liter", user=user)
        pav = self.product("Pav", "Pieces", user=user)
        mirchi = self.product("Mirchi", "KG", user=user)
        self.buy([{"productId": potato["id"], "quantity": 30, "rate": 25}, {"productId": oil["id"], "quantity": 5, "rate": 150},
                  {"productId": pav["id"], "quantity": 100, "rate": 4}, {"productId": mirchi["id"], "quantity": 2, "rate": 60}], user=user)
        self.use(potato["id"], 5, user=user)
        self.use(oil["id"], 1, user=user)
        self.use(pav["id"], 40, user=user)
        self.use(mirchi["id"], 500, unit_id=units["Gram"], user=user)
        rows = {r["name"]: r for r in self.call("get", "/consumption/summary", user=user, expect=200)["rows"]}
        self.assertEqual((rows["Potato"]["available"], rows["Potato"]["used"], rows["Potato"]["closing"]), (30, 5, 25))
        self.assertEqual((rows["Oil"]["available"], rows["Oil"]["used"], rows["Oil"]["closing"]), (5, 1, 4))
        self.assertEqual((rows["Pav"]["available"], rows["Pav"]["used"], rows["Pav"]["closing"]), (100, 40, 60))
        self.assertEqual((rows["Mirchi"]["available"], rows["Mirchi"]["used"], rows["Mirchi"]["closing"]), (2, 0.5, 1.5))
        # Cost is at the inventory valuation, and repeated entries in a day add up.
        self.use(potato["id"], 1, user=user)
        dash = self.dash(user)
        self.assertClose(dash["today"]["consumptionCost"], 6 * 25 + 150 + 40 * 4 + 0.5 * 60)
        self.assertEqual(self.stock(potato["id"], user), 24)

    def test_opening_stock_formula_from_the_brief(self):
        user = self._new_tenant()
        self.units(user)
        potato = self.product("Potato", price=20, opening=10, user=user)
        self.buy([{"productId": potato["id"], "quantity": 30, "rate": 25}], user=user)
        self.use(potato["id"], 5, user=user)
        row = self.call("get", "/consumption/summary", user=user, expect=200)["rows"][0]
        self.assertEqual((row["opening"], row["purchased"], row["available"], row["used"], row["closing"]), (10, 30, 40, 5, 35))

    def test_negative_stock_is_blocked_unless_the_owner_allows_it(self):
        user = self._new_tenant()
        self.units(user)
        product = self.product("Besan", opening=2, price=80, user=user)
        data = self.use(product["id"], 3, user=user, expect=409)
        self.assertEqual(data["code"], "INSUFFICIENT_STOCK")
        self.assertEqual(data["shortages"][0]["shortBy"], 1)
        self.assertEqual(self.stock(product["id"], user), 2)
        self.use(product["id"], 3, user=user, allowNegative=True)
        self.assertEqual(self.stock(product["id"], user), -1)
        self.call("put", "/settings", user=user, expect=200, json={"allowNegativeStock": True})
        self.use(product["id"], 1, user=user)
        self.assertEqual(self.stock(product["id"], user), -2)

    def test_low_and_out_of_stock_alerts(self):
        user = self._new_tenant()
        self.units(user)
        product = self.product("Sugar", opening=10, price=40, minimum=3, user=user)
        self.assertEqual(self.use(product["id"], 2, user=user)["alerts"], [])
        low = self.use(product["id"], 6, user=user)["alerts"]
        self.assertEqual(low[0]["state"], "LOW")
        kinds = {n["type"] for n in self.call("get", "/notifications", user=user, expect=200)["notifications"]}
        self.assertIn("low_stock", kinds)
        self.assertEqual(self.use(product["id"], 2, user=user)["alerts"][0]["state"], "OUT")
        kinds = {n["type"] for n in self.call("get", "/notifications", user=user, expect=200)["notifications"]}
        self.assertIn("out_of_stock", kinds)
        self.assertNotIn("low_stock", kinds)
        self.buy([{"productId": product["id"], "quantity": 20, "rate": 40}], user=user)
        kinds = {n["type"] for n in self.call("get", "/notifications", user=user, expect=200)["notifications"]}
        self.assertNotIn("out_of_stock", kinds)
        self.assertEqual(self.call("get", "/products?state=ATTENTION", user=user, expect=200)["products"], [])

    # recipes and sales ----------------------------------------------------
    def _vada_pav_shop(self, user):
        units = self.units(user)
        ids = {}
        for name, unit, price, opening in (("Potato", "KG", 25, 30), ("Oil", "Liter", 150, 5), ("Pav", "Pieces", 4, 100), ("Mirchi", "KG", 60, 2),
                                           ("Besan", "KG", 80, 5), ("Masala", "KG", 300, 1), ("Bag", "Pieces", 1, 100)):
            ids[name] = self.product(name, unit, price=price, opening=opening, user=user)["id"]
        food = self.food("Vada Pav", 15, 10, [
            {"productId": ids["Potato"], "quantity": 1, "unitId": units["KG"]}, {"productId": ids["Oil"], "quantity": 200, "unitId": units["ML"]},
            {"productId": ids["Pav"], "quantity": 10, "unitId": units["Pieces"]}, {"productId": ids["Mirchi"], "quantity": 100, "unitId": units["Gram"]},
            {"productId": ids["Besan"], "quantity": 250, "unitId": units["Gram"]}, {"productId": ids["Masala"], "quantity": 50, "unitId": units["Gram"]},
            {"productId": ids["Bag"], "quantity": 10, "unitId": units["Pieces"]},
        ], user=user)
        return ids, food

    def test_recipe_sale_example_from_the_brief(self):
        user = self._new_tenant()
        ids, food = self._vada_pav_shop(user)
        # Cost of 10 = 25 + 30 + 40 + 6 + 20 + 15 + 10 = 146.
        self.assertClose(food["preparationCost"], 14.6)
        self.assertClose(food["profitPerUnit"], 0.4)
        self.assertEqual(food["availableQty"], 100)  # 100 pav at 1 each runs out first
        self.sell(food["id"], 50, user=user)
        self.assertEqual(self.stock(ids["Potato"], user), 25)
        self.assertEqual(self.stock(ids["Oil"], user), 4)
        self.assertEqual(self.stock(ids["Pav"], user), 50)
        self.assertEqual(self.stock(ids["Mirchi"], user), 1.5)
        self.assertEqual(self.stock(ids["Besan"], user), 3.75)
        self.assertEqual(self.stock(ids["Masala"], user), 0.75)
        self.assertEqual(self.stock(ids["Bag"], user), 50)
        dash = self.dash(user)
        self.assertEqual(dash["today"]["revenue"], 750)
        self.assertClose(dash["today"]["cogs"], 730)
        self.assertClose(dash["today"]["grossProfit"], 20)
        self.assertEqual(dash["cashReceived"], 750)

    def test_manual_then_sale_does_not_deduct_twice(self):
        user = self._new_tenant()
        ids, food = self._vada_pav_shop(user)
        self.use(ids["Potato"], 5, user=user)  # morning: 5 KG taken for today's batch
        self.assertEqual(self.stock(ids["Potato"], user), 25)
        self.sell(food["id"], 50, user=user)  # evening: 50 sold needs exactly 5 KG
        self.assertEqual(self.stock(ids["Potato"], user), 25)
        self.assertEqual(self.stock(ids["Pav"], user), 50)  # no manual entry, deducted by recipe

    def test_sale_then_manual_does_not_deduct_twice(self):
        user = self._new_tenant()
        ids, food = self._vada_pav_shop(user)
        self.sell(food["id"], 50, user=user)
        result = self.use(ids["Potato"], 5, user=user)
        self.assertEqual(self.stock(ids["Potato"], user), 25)
        self.assertEqual(result["deductions"][0]["deducted"], 0)
        self.assertEqual(result["deductions"][0]["coveredBySales"], 5)

    def test_only_the_difference_is_deducted(self):
        user = self._new_tenant()
        ids, food = self._vada_pav_shop(user)
        self.use(ids["Potato"], 7, user=user)
        self.sell(food["id"], 50, user=user)  # needs 5, 7 already taken
        self.assertEqual(self.stock(ids["Potato"], user), 23)
        self.sell(food["id"], 30, user=user)  # total need 8, 1 more than the 7 taken
        self.assertEqual(self.stock(ids["Potato"], user), 22)
        self.use(ids["Potato"], 3, user=user)  # manual now 10, recipe 8: 2 more
        self.assertEqual(self.stock(ids["Potato"], user), 20)
        rep = self.call("get", "/reports/daily_consumption", user=user, expect=200)
        row = next(r for r in rep["rows"] if r["product"] == "Potato")
        self.assertEqual((row["manual"], row["recipe"], row["not_double"], row["deducted"]), (10, 8, 8, 10))

    def test_day_totals_do_not_depend_on_entry_order(self):
        users = [self._new_tenant(), self._new_tenant()]
        results = []
        for n, user in enumerate(users):
            ids, food = self._vada_pav_shop(user)
            if n == 0:
                self.use(ids["Potato"], 7, user=user)
                self.sell(food["id"], 50, user=user)
            else:
                self.sell(food["id"], 50, user=user)
                self.use(ids["Potato"], 7, user=user)
            results.append((self.stock(ids["Potato"], user), self.dash(user)["today"]["netProfit"]))
        self.assertEqual(results[0][0], results[1][0])
        self.assertClose(results[0][1], results[1][1])

    def test_void_gives_back_exactly_what_was_taken(self):
        user = self._new_tenant()
        ids, food = self._vada_pav_shop(user)
        value_before = self.dash(user)["remainingStockValue"]
        sale = self.sell(food["id"], 50, user=user)["sale"]
        manual = self.use(ids["Potato"], 7, user=user)["consumption"]
        self.assertEqual(self.stock(ids["Potato"], user), 23)
        self.call("post", f"/sales/{sale['id']}/void", user=user, idem=True, expect=200)
        self.assertEqual(self.stock(ids["Potato"], user), 23)  # manual 7 still stands
        self.assertEqual(self.stock(ids["Pav"], user), 100)
        self.call("post", f"/consumption/{manual['id']}/void", user=user, idem=True, expect=200)
        self.assertEqual(self.stock(ids["Potato"], user), 30)
        dash = self.dash(user)
        self.assertClose(dash["remainingStockValue"], value_before)
        self.assertEqual(dash["today"]["revenue"], 0)
        self.assertClose(dash["today"]["netProfit"], 0)
        self.call("post", f"/sales/{sale['id']}/void", user=user, idem=True, expect=409)

    def test_sale_rolls_back_entirely_when_any_ingredient_is_short(self):
        user = self._new_tenant()
        ids, food = self._vada_pav_shop(user)
        data = self.sell(food["id"], 2000, user=user, expect=409)
        names = {s["name"] for s in data["shortages"]}
        self.assertTrue({"Potato", "Pav", "Masala"} <= names)
        self.assertEqual(self.stock(ids["Potato"], user), 30)
        self.assertEqual(self.dash(user)["today"]["revenue"], 0)
        with self.psycopg.connect(TEST_DB) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM food_sales_transactions WHERE business_id = %s", (user,))
            self.assertEqual(cur.fetchone()[0], 0)

    def test_recipe_edit_only_changes_future_sales(self):
        user = self._new_tenant()
        ids, food = self._vada_pav_shop(user)
        units = self.units(user)
        self.sell(food["id"], 10, user=user)
        self.assertEqual(self.stock(ids["Potato"], user), 29)
        self.call("put", f"/food-products/{food['id']}/recipe", user=user, expect=200, json={
            "yieldQty": 10, "ingredients": [{"productId": ids["Potato"], "quantity": 2, "unitId": units["KG"]}]})
        self.sell(food["id"], 10, user=user)
        self.assertEqual(self.stock(ids["Potato"], user), 27)
        self.assertEqual(self.stock(ids["Pav"], user), 90)  # no longer in the recipe; first sale's 10 stays
        self.call("put", f"/food-products/{food['id']}/recipe", user=user, expect=422, json={
            "yieldQty": 10, "ingredients": [{"productId": ids["Oil"], "quantity": 1, "unitId": units["KG"]}]})  # KG of a Liter item

    def test_packaged_food_deducts_the_product_directly(self):
        user = self._new_tenant()
        self.units(user)
        soda = self.product("Soda", "Bottle", price=12, opening=24, user=user)
        food = self.food("Cold Drink", 20, user=user, productType="PACKAGED", packagedProductId=soda["id"])
        self.assertClose(food["preparationCost"], 12)
        self.assertEqual(food["availableQty"], 24)
        self.sell(food["id"], 10, user=user)
        self.assertEqual(self.stock(soda["id"], user), 14)
        dash = self.dash(user)
        self.assertClose(dash["today"]["cogs"], 120)
        self.assertClose(dash["today"]["grossProfit"], 80)

    def test_food_without_recipe_is_flagged_and_sells(self):
        user = self._new_tenant()
        self.units(user)
        food = self.food("Poha", 25, user=user)
        result = self.sell(food["id"], 4, user=user)
        self.assertEqual(result["noRecipe"], ["Poha"])
        report = self.call("get", "/reports/food_profit", user=user, expect=200)
        self.assertEqual(report["rows"][0]["note"], "No recipe: cost unknown")

    def test_sale_payment_split_discount_and_pending(self):
        user = self._new_tenant()
        self.units(user)
        vada = self.food("Vada", 15, user=user)
        tea = self.food("Tea", 10, user=user)
        lines = [{"foodProductId": vada["id"], "quantity": 50}, {"foodProductId": tea["id"], "quantity": 30}]
        self.call("post", "/sales", user=user, idem=True, expect=422, json={"items": lines, "cashAmount": 100})
        self.call("post", "/sales", user=user, idem=True, expect=422, json={"items": lines, "discount": 50, "cashAmount": 500, "upiAmount": 300, "creditAmount": 200})  # no name
        sale = self.call("post", "/sales", user=user, idem=True, expect=201, json={
            "items": lines, "discount": 50, "cashAmount": 500, "upiAmount": 300, "creditAmount": 200, "customerName": "Ramesh"})["sale"]
        self.assertEqual((sale["subtotal"], sale["totalAmount"]), (1050, 1000))
        dash = self.dash(user)
        self.assertEqual((dash["today"]["revenue"], dash["cashReceived"], dash["upiReceived"], dash["pendingReceivable"]), (1000, 500, 300, 200))
        self.call("post", "/payments", user=user, idem=True, expect=422, json={"direction": "RECEIVED", "saleId": sale["id"], "amount": 201, "paymentMethod": "Cash"})
        self.call("post", "/payments", user=user, idem=True, expect=201, json={"direction": "RECEIVED", "saleId": sale["id"], "amount": 150, "paymentMethod": "UPI"})
        dash = self.dash(user)
        self.assertEqual((dash["upiReceived"], dash["pendingReceivable"]), (450, 50))
        pending = self.call("get", "/payments/pending", user=user, expect=200)
        self.assertEqual(pending["receivableTotal"], 50)
        self.call("post", f"/sales/{sale['id']}/void", user=user, idem=True, expect=409)

    # expenses and P&L -----------------------------------------------------
    def test_gas_and_packaging_are_not_counted_twice(self):
        user = self._new_tenant()
        boot = self.call("get", "/bootstrap", user=user, expect=200)
        categories = {c["name"]: c for c in boot["expenseCategories"]}
        self.assertFalse(categories["Gas Refill"]["countsInPnl"])
        self.assertTrue(categories["Shop Rent"]["countsInPnl"])
        self.call("post", "/expenses", user=user, idem=True, expect=201, json={"categoryId": categories["Shop Rent"]["id"], "amount": 500})
        self.call("post", "/expenses", user=user, idem=True, expect=201, json={"categoryId": categories["Gas Refill"]["id"], "amount": 950})
        dash = self.dash(user)
        self.assertEqual(dash["today"]["expenses"], 500)
        self.assertEqual(dash["today"]["expensesExcluded"], 950)
        self.assertEqual(dash["today"]["netProfit"], -500)
        # The owner who does not track gas as stock can count it for that entry.
        expense = self.call("post", "/expenses", user=user, idem=True, expect=201, json={"categoryId": categories["Gas Refill"]["id"], "amount": 100, "countsInPnl": True})["expense"]
        self.assertEqual(self.dash(user)["today"]["expenses"], 600)
        self.call("delete", f"/expenses/{expense['id']}", user=user, expect=200)
        listing = self.call("get", "/expenses", user=user, expect=200)
        self.assertEqual(listing["totals"]["dayTotal"], 1450)
        self.assertEqual(listing["totals"]["monthTotal"], 1450)

    def test_profit_and_stock_tie_out(self):
        """Opening + purchases + adjustments - everything that left = closing, in value, and it matches P&L."""
        user = self._new_tenant()
        ids, food = self._vada_pav_shop(user)
        opening_value = self.dash(user)["remainingStockValue"]
        self.buy([{"productId": ids["Potato"], "quantity": 10, "rate": 40}, {"productId": ids["Oil"], "quantity": 2, "rate": 170}], user=user)
        self.use(ids["Potato"], 7, user=user)
        self.sell(food["id"], 50, user=user)
        self.use(ids["Oil"], 0.5, purpose="WASTAGE", user=user)
        self.use(ids["Besan"], 0.25, purpose="SPOILAGE", user=user)
        self.use(ids["Masala"], 0.1, purpose="PERSONAL", user=user)
        self.call("post", "/adjustments", user=user, idem=True, expect=201, json={"productId": ids["Pav"], "direction": "OUT", "quantity": 5, "reason": "Count short"})
        self.call("post", "/adjustments", user=user, idem=True, expect=201, json={"productId": ids["Bag"], "direction": "IN", "quantity": 10, "reason": "Found box"})
        dash = self.dash(user)
        day = dash["today"]
        self.assertClose(dash["remainingStockValue"], opening_value + day["purchases"] + day["adjustmentNet"] - day["cogs"] - day["wastage"] - day["spoilage"] - day["personal"])
        self.assertClose(day["netProfit"], day["revenue"] - day["cogs"] - day["expenses"] - day["wastage"] - day["spoilage"] + day["adjustmentNet"])
        self.assertGreater(day["unmatchedCost"], 0)  # 2 KG potato used beyond the recipe
        # Line-level profit adds up to the P&L figure.
        sold = self.call("get", "/reports/food_profit", user=user, expect=200)["totals"]
        self.assertClose(sold["cost"], day["recipeCost"])
        # Wastage and personal use are separate, personal is outside profit.
        wastage = self.call("get", "/reports/wastage", user=user, expect=200)["rows"]
        self.assertEqual({r["kind"] for r in wastage}, {"WASTAGE", "SPOILAGE", "PERSONAL"})
        # The stock formula holds per product.
        for row in self.call("get", "/reports/stock", user=user, expect=200)["rows"]:
            self.assertClose(row["closing"], row["opening"] + row["purchased"] + row["adjust_in"] - row["consumption"] - row["sales_usage"] - row["adjust_out"] - row["lost"])

    def test_adjustment_void_and_negative_guard(self):
        user = self._new_tenant()
        self.units(user)
        product = self.product("Ginger", opening=1, price=100, user=user)
        self.call("post", "/adjustments", user=user, idem=True, expect=409, json={"productId": product["id"], "direction": "OUT", "quantity": 2, "reason": "x"})
        self.call("post", "/adjustments", user=user, idem=True, expect=422, json={"productId": product["id"], "direction": "OUT", "quantity": 1})
        adjustment = self.call("post", "/adjustments", user=user, idem=True, expect=201, json={"productId": product["id"], "direction": "IN", "quantity": 2, "reason": "Found"})["adjustment"]
        self.assertEqual(self.stock(product["id"], user), 3)
        self.call("post", f"/adjustments/{adjustment['id']}/void", user=user, idem=True, expect=200)
        self.assertEqual(self.stock(product["id"], user), 1)

    # integrity ------------------------------------------------------------
    def test_retried_request_posts_once(self):
        user = self._new_tenant()
        self.units(user)
        product = self.product("Onion", user=user)
        key = uuid.uuid4().hex
        payload = {"items": [{"productId": product["id"], "quantity": 5, "rate": 20}]}
        first = self.call("post", "/purchases", user=user, key=key, json=payload, expect=201)
        second = self.call("post", "/purchases", user=user, key=key, json=payload, expect=201)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["purchase"]["id"], second["purchase"]["id"])
        self.assertEqual(self.stock(product["id"], user), 5)

    def test_concurrent_consumption_cannot_oversell(self):
        user = self._new_tenant()
        self.units(user)
        product = self.product("Garlic", opening=10, price=100, user=user)
        results = []

        def worker():
            client = self.app.test_client()
            res = client.post("/api/foodseller/consumption", json={"items": [{"productId": product["id"], "quantity": 4}]},
                              headers={"Authorization": f"Bearer {self.tokens[user]}", "Idempotency-Key": uuid.uuid4().hex})
            results.append(res.status_code)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [201, 201, 409, 409, 409])
        self.assertEqual(self.stock(product["id"], user), 2)

    def test_ledger_rows_cannot_be_edited(self):
        user = self._new_tenant()
        self.units(user)
        self.product("Tomato", opening=5, price=10, user=user)
        with self.psycopg.connect(TEST_DB) as conn:
            with self.assertRaises(self.psycopg.errors.RaiseException), conn.cursor() as cur:
                cur.execute("UPDATE food_inventory_transactions SET quantity = 1 WHERE business_id = %s", (user,))

    def test_every_table_has_the_required_columns(self):
        from foodseller_service import FOOD_TABLES
        with self.psycopg.connect(TEST_DB) as conn, conn.cursor() as cur:
            for table in FOOD_TABLES:
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
                columns = {r[0] for r in cur.fetchall()}
                self.assertTrue({"business_id", "user_id", "transaction_date", "created_at", "updated_at"} <= columns, table)

    def test_deleting_the_customer_removes_their_data(self):
        user = self._new_tenant()
        ids, food = self._vada_pav_shop(user)
        self.sell(food["id"], 5, user=user)
        with self.psycopg.connect(TEST_DB) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM userdetails WHERE id = %s", (user,))
            cur.execute("SELECT COUNT(*) FROM food_inventory_transactions WHERE business_id = %s", (user,))
            self.assertEqual(cur.fetchone()[0], 0)

    # reports and notifications --------------------------------------------
    def test_every_report_runs_filters_and_exports(self):
        user = self._new_tenant()
        ids, food = self._vada_pav_shop(user)
        self.buy([{"productId": ids["Potato"], "quantity": 2, "rate": 30}], user=user)
        self.sell(food["id"], 10, user=user)
        self.use(ids["Oil"], 0.1, purpose="WASTAGE", user=user)
        catalog = self.call("get", "/reports", user=user, expect=200)["reports"]
        self.assertEqual(len(catalog), 12)
        month = self.today.strftime("%Y-%m")
        for report in catalog:
            data = self.call("get", f"/reports/{report['key']}?month={month}", user=user, expect=200)
            self.assertTrue(data["columns"], report["key"])
            self.assertTrue(data["rows"] or report["key"] == "low_stock", report["key"])
            data = self.call("get", f"/reports/{report['key']}?from={self.today}&to={self.today}", user=user, expect=200)
        filtered = self.call("get", f"/reports/stock?productId={ids['Oil']}", user=user, expect=200)
        self.assertEqual([r["name"] for r in filtered["rows"]], ["Oil"])
        self.call("get", "/reports/nope", user=user, expect=404)
        self.call("get", "/reports/stock?month=2026-13", user=user, expect=422)
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl is not installed")
        export = self.call("get", f"/reports/daily_sales?month={month}&format=xlsx", user=user, expect=200)
        self.assertTrue(base64.b64decode(export["fileBase64"]).startswith(b"PK"))

    def test_pnl_endpoint(self):
        user = self._new_tenant()
        ids, food = self._vada_pav_shop(user)
        self.sell(food["id"], 10, user=user)
        data = self.call("get", "/pnl", user=user, expect=200)
        self.assertEqual(data["pnl"]["revenue"], 150)
        self.assertEqual(data["foods"][0]["food"], "Vada Pav")
        self.assertEqual(len(data["sales"]), 1)

    def test_unusual_expense_excess_consumption_and_summaries(self):
        user = self._new_tenant()
        boot = self.call("get", "/bootstrap", user=user, expect=200)
        category = next(c for c in boot["expenseCategories"] if c["name"] == "Transportation")
        product = self.product("Rice", opening=500, price=40, user=user)
        yesterday = self.today - timedelta(days=1)
        for days_back in (1, 2, 3):
            day = (self.today - timedelta(days=days_back)).isoformat()
            self.call("post", "/expenses", user=user, idem=True, expect=201, json={"categoryId": category["id"], "amount": 100, "date": day})
            self.call("post", "/consumption", user=user, idem=True, expect=201, json={"date": day, "items": [{"productId": product["id"], "quantity": 2}]})
        food = self.food("Rice Plate", 50, user=user)
        self.call("post", "/sales", user=user, idem=True, expect=201, json={"date": yesterday.isoformat(), "items": [{"foodProductId": food["id"], "quantity": 2}], "paymentMode": "Credit", "customerName": "Sita"})
        self.call("post", "/expenses", user=user, idem=True, expect=201, json={"categoryId": category["id"], "amount": 900})
        self.use(product["id"], 10, user=user)
        notifications = self.call("get", "/notifications", user=user, expect=200)["notifications"]
        kinds = {n["type"] for n in notifications}
        self.assertTrue({"unusual_expense", "excess_consumption", "sales_summary", "profit_summary", "pending_payments"} <= kinds, kinds)
        # Read state survives a refresh and nothing is duplicated.
        self.call("post", "/notifications/read", user=user, json={}, expect=200)
        again = self.call("get", "/notifications", user=user, expect=200)["notifications"]
        self.assertEqual(len(again), len(notifications))
        self.assertTrue(all(n["isRead"] for n in again))
        self.assertEqual(self.dash(user)["unreadNotifications"], 0)

    # AI -------------------------------------------------------------------
    def _ask(self, user, message, lang="en"):
        return self.call("post", "/ai/chat", user=user, json={"message": message, "lang": lang}, expect=200)

    def test_ai_answers_the_brief_questions_from_records(self):
        user = self._new_tenant()
        ids, food = self._vada_pav_shop(user)
        self.buy([{"productId": ids["Potato"], "quantity": 10, "rate": 25}], user=user)
        self.use(ids["Oil"], 1, user=user)
        self.sell(food["id"], 50, user=user)
        self.assertIn("35", self._ask(user, "How much potato stock is remaining?")["reply"])
        self.assertIn("250", self._ask(user, "How much did I spend on purchases today?")["reply"])
        self.assertIn("Net profit", self._ask(user, "What is my net profit today?")["reply"])
        self.assertIn("Vada Pav", self._ask(user, "Which food product generated the highest profit?")["reply"])
        self.assertIn("1", self._ask(user, "How much oil did I consume this week?")["reply"])
        self.assertIn("All products", self._ask(user, "Which products have low stock?")["reply"])
        self.assertIn("yesterday", self._ask(user, "Why is my profit lower compared to yesterday?")["reply"])
        target = self._ask(user, "How much Vada Pav should I sell to achieve ₹1,000 profit?")["reply"]
        self.assertIn("Vada Pav", target)
        self.assertIn("शिल्लक", self._ask(user, "बटाटा potato किती शिल्लक आहे", lang="mr")["reply"])

    def _no_change_snapshot(self, user, product_id):
        return self.stock(product_id, user), self.dash(user)["today"]["purchases"]

    def test_ai_purchase_needs_confirmation(self):
        user = self._new_tenant()
        self.units(user)
        product = self.product("Oil", "Liter", price=150, opening=1, minimum=2, user=user)
        answer = self._ask(user, "Which ingredients should I purchase tomorrow?")
        proposal = answer["proposal"]
        self.assertEqual(proposal["status"], "Pending")
        self.assertEqual(self._no_change_snapshot(user, product["id"]), (1, 0))
        rejected = self.call("post", f"/ai/proposals/{proposal['id']}/reject", user=user, expect=200, json={})
        self.assertEqual(rejected["proposal"]["status"], "Rejected")
        self.call("post", f"/ai/proposals/{proposal['id']}/confirm", user=user, expect=409, json={})
        self.assertEqual(self._no_change_snapshot(user, product["id"]), (1, 0))
        plan = self.call("post", "/ai/purchase-suggestions", user=user, expect=200, json={})
        self.call("post", f"/ai/proposals/{plan['proposal']['id']}/confirm", user=self.b, expect=404, json={})  # other business
        confirmed = self.call("post", f"/ai/proposals/{plan['proposal']['id']}/confirm", user=user, expect=200, json={"paymentMethod": "UPI"})
        self.assertEqual(confirmed["proposal"]["status"], "Confirmed")
        self.assertEqual(self.stock(product["id"], user), 2)
        self.assertEqual(self.dash(user)["today"]["purchases"], 150)
        self.call("post", f"/ai/proposals/{plan['proposal']['id']}/confirm", user=user, expect=409, json={})  # double tap
        self.assertEqual(self.stock(product["id"], user), 2)

    def test_ai_purchase_without_a_price_cannot_be_confirmed(self):
        user = self._new_tenant()
        self.units(user)
        product = self.product("Unpriced", "Pieces", opening=1, minimum=5, user=user)
        proposal = self.call("post", "/ai/purchase-suggestions", user=user, expect=200, json={})["proposal"]
        self.call("post", f"/ai/proposals/{proposal['id']}/confirm", user=user, expect=422, json={})
        self.assertEqual(self.stock(product["id"], user), 1)
        listed = self.call("get", "/ai/proposals", user=user, expect=200)["proposals"][0]
        self.assertEqual(listed["status"], "Pending")  # still open for Edit first

    def test_model_output_is_validated_and_only_proposes(self):
        import foodseller_ai
        user = self._new_tenant()
        self.units(user)
        product = self.product("Oil", "Liter", price=150, opening=5, user=user)
        foreign = self.product(f"Foreign {uuid.uuid4().hex[:6]}", user=self.b)
        cases = [
            ({"type": "CREATE_PURCHASE", "items": [{"productId": foreign["id"], "quantity": 5, "rate": 1}]}, None),  # other business's id
            ({"type": "CREATE_PURCHASE", "items": [{"productId": product["id"], "quantity": -5}]}, None),
            ({"type": "DROP_TABLES"}, None),
            ({"type": "STOCK_ADJUSTMENT", "productId": product["id"], "direction": "OUT", "quantity": 1}, None),  # no reason
            ({"type": "STOCK_ADJUSTMENT", "productId": product["id"], "direction": "OUT", "quantity": 1, "reason": "Damaged"}, "STOCK_ADJUSTMENT"),
        ]
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            for action, expected in cases:
                with mock.patch.object(foodseller_ai, "_call_gemini", return_value={"reply": "ok", "action": action}):
                    data = self._ask(user, "do it")
                self.assertEqual(data["source"], "ai")
                self.assertEqual((data["proposal"] or {}).get("actionType"), expected, action)
                self.assertEqual(self.stock(product["id"], user), 5)
            # Gemini failing falls back to records with a notice.
            import urllib.error
            with mock.patch.object(foodseller_ai, "_call_gemini", side_effect=urllib.error.URLError("down")):
                data = self._ask(user, "What is my net profit today?")
            self.assertEqual(data["source"], "records")
            self.assertTrue(data["notice"])
        proposal = self.call("get", "/ai/proposals", user=user, expect=200)["proposals"][0]
        self.call("post", f"/ai/proposals/{proposal['id']}/confirm", user=user, expect=200, json={})
        self.assertEqual(self.stock(product["id"], user), 4)

    def test_snapshot_only_contains_this_business(self):
        import foodseller_ai
        user = self._new_tenant()
        self.units(user)
        self.product("Mine", user=user)
        secret = f"Secret{uuid.uuid4().hex[:6]}"
        self.product(secret, user=self.b)
        captured = {}

        def capture(system_prompt, history, message, api_key):
            captured["prompt"] = system_prompt
            return {"reply": "ok", "action": None}

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), mock.patch.object(foodseller_ai, "_call_gemini", side_effect=capture):
            self._ask(user, "stock?")
        self.assertIn("Mine", captured["prompt"])
        self.assertNotIn(secret, captured["prompt"])
        json.dumps(captured["prompt"])


if __name__ == "__main__":
    unittest.main()
