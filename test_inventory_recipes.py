"""
End-to-end tests for recipes and Sell / Purchase products on product shops, on a real database.

They CREATE TABLES and WRITE DATA, so they only run against a database you name, never the one in .env:

    $env:INVENTORY_TEST_DATABASE_URL = "postgresql://user@127.0.0.1:5432/empty_test_db"
    .venv\\Scripts\\python.exe -m unittest test_inventory_recipes -v

The suite does not import app.py; it mounts only the inventory blueprint.
"""

import os
import threading
import unittest
import uuid

TEST_DB = os.getenv("INVENTORY_TEST_DATABASE_URL")


def _configured_db():
    from dotenv import dotenv_values
    return (dotenv_values(os.path.join(os.path.dirname(__file__), ".env")) or {}).get("DATABASE_URL")


@unittest.skipUnless(TEST_DB, "set INVENTORY_TEST_DATABASE_URL to an empty test database to run")
class RecipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if TEST_DB == _configured_db():
            raise RuntimeError("Refusing to run: INVENTORY_TEST_DATABASE_URL must not be the application database")
        os.environ["DATABASE_URL"] = TEST_DB
        os.environ.setdefault("AUTH_SECRET", "inventory-test-secret")
        import psycopg
        from flask import Flask
        from inventory_api import inventory_bp
        from inventory_service import ensure_inventory_schema

        cls.psycopg = psycopg
        with psycopg.connect(TEST_DB) as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS userdetails (
                    id BIGSERIAL PRIMARY KEY, full_name VARCHAR(120) NOT NULL, email VARCHAR(255) NOT NULL UNIQUE,
                    dob DATE NOT NULL, business_type VARCHAR(100) NOT NULL DEFAULT 'Fruit sellers',
                    active_plan VARCHAR(100) NOT NULL DEFAULT 'monthly', user_type VARCHAR(20) NOT NULL DEFAULT 'customer',
                    status VARCHAR(20) NOT NULL DEFAULT 'active', enabled_features JSONB DEFAULT '[]'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
            """)
            ensure_inventory_schema(cur)
            ensure_inventory_schema(cur)  # idempotent re-run
        app = Flask("inventory-test")
        app.register_blueprint(inventory_bp)
        cls.app = app
        cls.client = app.test_client()

    # helpers --------------------------------------------------------------
    def setUp(self):
        from auth_token import issue_token
        with self.psycopg.connect(TEST_DB) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO userdetails (full_name, email, dob) VALUES ('Owner', %s, '1990-01-01') RETURNING id", (f"{uuid.uuid4().hex}@test.local",))
            self.owner = cur.fetchone()[0]
        self.headers = {"Authorization": f"Bearer {issue_token(self.owner)}"}
        self.shop = {}
        for name, unit, stock, cost in (("Potato", "kg", 30, 25), ("Oil", "litre", 5, 150), ("Pav", "piece", 100, 4), ("Besan", "kg", 5, 80)):
            self.shop[name] = self.product(name, unit, stock, 0, cost, "PURCHASE")["id"]
        self.vada = self.product("Vada Pav", "piece", 18, 15, 3, "SELL")["id"]
        self.recipe(self.vada, 10, [("Potato", 1, "kg"), ("Oil", 200, "ml"), ("Pav", 10, "piece"), ("Besan", 250, "gram")])

    def call(self, method, path, expect=None, json=None, **kwargs):
        res = getattr(self.client, method)(f"/api/inventory{path}", json=json, headers=self.headers, **kwargs)
        data = res.get_json()
        if expect is not None:
            self.assertEqual(res.status_code, expect, f"{method} {path}: {data}")
        return data

    def product(self, name, unit, stock, selling, cost, kind=None):
        body = {"ownerId": self.owner, "name": name, "category": "Foods", "unit": unit, "stockQty": stock, "sellingPrice": selling, "costPrice": cost}
        if kind:
            body["productType"] = kind
        return self.call("post", "/products", 201, body)["product"]

    def recipe(self, product_id, makes, lines, expect=200):
        return self.call("put", f"/products/{product_id}/recipe", expect, {
            "ownerId": self.owner, "yieldQty": makes,
            "ingredients": [{"productId": self.shop.get(n, n), "quantity": q, "unit": u} for n, q, u in lines],
        })

    def stock(self):
        rows = self.call("get", f"/products?owner_id={self.owner}", 200)["products"]
        return {p["name"]: p for p in rows}

    def qty(self, name):
        return self.stock()[name]["stockQty"]

    def sell(self, product_id, quantity, expect=201, **extra):
        return self.call("post", "/transactions", expect, {"ownerId": self.owner, "productId": product_id, "quantity": quantity, "amount": max(quantity * 15, 1), "type": "SALE", **extra})

    def cost_of(self, transaction_id):
        with self.psycopg.connect(TEST_DB) as conn, conn.cursor() as cur:
            cur.execute("SELECT cost_amount FROM inventory_transactions WHERE id = %s", (transaction_id,))
            return float(cur.fetchone()[0])

    def assertIngredients(self, potato, oil, pav, besan):
        s = self.stock()
        self.assertAlmostEqual(s["Potato"]["stockQty"], potato, places=4)
        self.assertAlmostEqual(s["Oil"]["stockQty"], oil, places=4)
        self.assertAlmostEqual(s["Pav"]["stockQty"], pav, places=4)
        self.assertAlmostEqual(s["Besan"]["stockQty"], besan, places=4)

    # the request ----------------------------------------------------------
    def test_selling_10_vada_pav_deducts_each_ingredient(self):
        tx = self.sell(self.vada, 10)["transaction"]
        self.assertIngredients(29, 4.8, 90, 4.75)
        # Cost of 10 = 25 + 30 + 40 + 20 = 115, frozen on the sale for profit.
        self.assertAlmostEqual(self.cost_of(tx["id"]), 115, places=2)

    def test_small_amounts_do_not_drift(self):
        for _ in range(10):
            self.sell(self.vada, 1)
        self.assertIngredients(29, 4.8, 90, 4.75)

    def test_recipe_product_shows_what_it_can_make(self):
        vada = self.stock()["Vada Pav"]
        self.assertTrue(vada["hasRecipe"])
        self.assertEqual(vada["ingredientCount"], 4)
        self.assertEqual(vada["stockQty"], 100)  # 100 pav at 1 each runs out first
        self.assertAlmostEqual(vada["costPrice"], 11.5, places=2)
        self.sell(self.vada, 10)
        self.assertEqual(self.qty("Vada Pav"), 90)

    # undo -----------------------------------------------------------------
    def test_deleting_a_sale_gives_everything_back(self):
        tx = self.sell(self.vada, 10)["transaction"]
        self.call("delete", f"/transactions/{tx['id']}?owner_id={self.owner}", 200)
        self.assertIngredients(30, 5, 100, 5)

    def test_editing_a_sale_moves_stock_by_the_difference(self):
        tx = self.sell(self.vada, 10)["transaction"]
        self.call("put", f"/transactions/{tx['id']}", 200, {"ownerId": self.owner, "quantity": 20, "amount": 300})
        self.assertIngredients(28, 4.6, 80, 4.5)
        self.assertAlmostEqual(self.cost_of(tx["id"]), 230, places=2)
        self.call("put", f"/transactions/{tx['id']}", 200, {"ownerId": self.owner, "quantity": 5, "amount": 75})
        self.assertIngredients(29.5, 4.9, 95, 4.875)
        self.call("delete", f"/transactions/{tx['id']}?owner_id={self.owner}", 200)
        self.assertIngredients(30, 5, 100, 5)

    def test_recipe_change_does_not_touch_past_sales(self):
        tx = self.sell(self.vada, 10)["transaction"]
        self.recipe(self.vada, 10, [("Potato", 2, "kg")])
        self.call("put", f"/transactions/{tx['id']}", 200, {"ownerId": self.owner, "quantity": 20, "amount": 300})
        self.assertIngredients(28, 4.6, 80, 4.5)  # the old recipe, not 2 KG per 10
        self.sell(self.vada, 10)
        self.assertIngredients(26, 4.6, 80, 4.5)  # the new recipe from here on
        self.call("delete", f"/transactions/{tx['id']}?owner_id={self.owner}", 200)
        self.assertIngredients(28, 5, 100, 5)

    # shortages ------------------------------------------------------------
    def test_shortage_refuses_the_whole_sale(self):
        data = self.sell(self.vada, 2000, 409)
        self.assertEqual(data["code"], "INSUFFICIENT_STOCK")
        self.assertEqual({s["name"] for s in data["shortages"]}, {"Potato", "Oil", "Pav", "Besan"})
        self.assertIngredients(30, 5, 100, 5)
        with self.psycopg.connect(TEST_DB) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM inventory_transactions WHERE owner_id = %s", (self.owner,))
            self.assertEqual(cur.fetchone()[0], 0)

    def test_confirmed_shortage_stops_at_zero_and_undo_is_exact(self):
        tx = self.sell(self.vada, 200, allowShortage=True)["transaction"]
        self.assertIngredients(10, 1, 0, 0)  # pav and besan ran out
        self.call("delete", f"/transactions/{tx['id']}?owner_id={self.owner}", 200)
        self.assertIngredients(30, 5, 100, 5)

    def test_concurrent_sales_cannot_oversell(self):
        results = []

        def worker():
            res = self.app.test_client().post("/api/inventory/transactions", headers=self.headers, json={
                "ownerId": self.owner, "productId": self.vada, "quantity": 40, "amount": 600, "type": "SALE"})
            results.append(res.status_code)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [201, 201, 409, 409])
        self.assertEqual(self.qty("Pav"), 20)

    # ordinary products must behave as before ------------------------------
    def test_ordinary_product_still_uses_its_own_stock(self):
        mango = self.product("Mango", "kg", 10, 100, 60)["id"]
        tx = self.sell(mango, 2)["transaction"]
        self.assertEqual(self.qty("Mango"), 8)
        self.assertIngredients(30, 5, 100, 5)
        self.assertAlmostEqual(self.cost_of(tx["id"]), 120, places=2)
        self.call("put", f"/transactions/{tx['id']}", 200, {"ownerId": self.owner, "quantity": 3, "amount": 300})
        self.assertEqual(self.qty("Mango"), 7)
        self.call("delete", f"/transactions/{tx['id']}?owner_id={self.owner}", 200)
        self.assertEqual(self.qty("Mango"), 10)
        # As before, an ordinary sale over stock stops at zero rather than being refused.
        self.sell(mango, 50)
        self.assertEqual(self.qty("Mango"), 0)

    def test_purchase_raises_stock_and_cost_and_recipe_follows(self):
        self.call("post", "/transactions", 201, {"ownerId": self.owner, "productId": self.shop["Pav"], "quantity": 100, "amount": 500, "type": "PURCHASE"})
        s = self.stock()
        self.assertEqual(s["Pav"]["stockQty"], 200)
        self.assertEqual(s["Pav"]["costPrice"], 5)
        self.assertEqual(s["Vada Pav"]["stockQty"], 200)  # besan now limits it: 5 KG at 0.025 each
        self.assertAlmostEqual(s["Vada Pav"]["costPrice"], 12.5, places=2)

    # Sell / Purchase ------------------------------------------------------
    def test_purchase_product_cannot_be_sold(self):
        self.sell(self.shop["Potato"], 1, 409)
        self.assertEqual(self.qty("Potato"), 30)

    def test_new_product_with_a_cost_stays_a_selling_product(self):
        mango = self.product("Mango", "kg", 0, 0, 0)["id"]
        self.call("post", "/transactions", 201, {"ownerId": self.owner, "productId": mango, "quantity": 10, "amount": 600, "type": "PURCHASE"})
        p = self.stock()["Mango"]
        self.assertEqual(p["productType"], "SELL")
        rates = self.call("get", f"/daily-rates?owner_id={self.owner}", 200)["rates"]
        self.assertIn("Mango", [r["productName"] for r in rates])
        self.assertNotIn("Potato", [r["productName"] for r in rates])
        self.sell(mango, 1)

    def test_existing_products_are_sorted_once(self):
        from inventory_service import ensure_inventory_schema
        with self.psycopg.connect(TEST_DB) as conn, conn.cursor() as cur:
            for name, selling, cost, created in (("OldBought", 0, 32, "2026-10-01"), ("OldSold", 15, 3, "2026-10-01"), ("OldNoPrices", 0, 0, "2026-10-01"), ("NewBought", 0, 32, "2026-12-01")):
                cur.execute(
                    "INSERT INTO inventory_products (owner_id, name, unit, selling_price, cost_price, created_at) VALUES (%s, %s, 'kg', %s, %s, %s)",
                    (self.owner, name, selling, cost, created),
                )
            ensure_inventory_schema(cur)
        s = self.stock()
        self.assertEqual(s["OldBought"]["productType"], "PURCHASE")
        self.assertEqual(s["OldSold"]["productType"], "SELL")
        self.assertEqual(s["OldNoPrices"]["productType"], "SELL")
        self.assertEqual(s["NewBought"]["productType"], "SELL")
        # The owner's later choice survives a restart.
        self.call("put", f"/products/{s['OldBought']['id']}", 200, {"ownerId": self.owner, "productType": "SELL"})
        with self.psycopg.connect(TEST_DB) as conn, conn.cursor() as cur:
            ensure_inventory_schema(cur)
        self.assertEqual(self.stock()["OldBought"]["productType"], "SELL")

    def test_a_selling_product_can_also_be_an_ingredient(self):
        pav = self.product("Sold Pav", "piece", 100, 5, 2, "SELL")["id"]
        tea_masala = self.product("Masala Pav", "piece", 0, 20, 0, "SELL")["id"]
        self.recipe(tea_masala, 1, [(pav, 2, "piece")])
        self.sell(tea_masala, 10)  # uses 20 of it
        self.assertEqual(self.qty("Sold Pav"), 80)
        self.sell(pav, 5)  # still sold on its own, from the same stock
        s = self.stock()
        self.assertEqual(s["Sold Pav"]["stockQty"], 75)
        self.assertEqual(s["Sold Pav"]["productType"], "SELL")
        self.assertEqual(s["Masala Pav"]["stockQty"], 37)  # what is left can make 37, and a pav sold on its own lowers it

    # By-hand use ----------------------------------------------------------
    def use(self, name, quantity, expect=201, **extra):
        return self.call("post", "/usage", expect, {"ownerId": self.owner, "productId": self.shop[name], "quantity": quantity, **extra})

    def summary(self):
        return self.call("get", f"/transactions?owner_id={self.owner}&period=daily", 200)["summary"]

    def test_purchased_30_used_20_leaves_10(self):
        self.assertEqual(self.use("Potato", 20)["stockQty"], 10)
        self.assertEqual(self.qty("Potato"), 10)
        self.assertEqual(self.stock()["Potato"]["usedToday"], 20)
        self.assertEqual(self.summary()["usageCost"], 500)  # 20 KG at 25
        self.assertEqual(self.summary()["netProfit"], -500)

    def test_use_converts_units_and_adds_up_through_the_day(self):
        self.assertEqual(self.use("Besan", 500, unit="gram")["stockQty"], 4.5)
        self.use("Besan", 1)
        self.assertEqual(self.qty("Besan"), 3.5)
        self.assertEqual(self.stock()["Besan"]["usedToday"], 1.5)
        self.use("Besan", 1, 400, unit="litre")

    def test_use_cannot_exceed_stock(self):
        data = self.use("Potato", 31, 409)
        self.assertIn("Only 30", data["error"])
        self.assertEqual(self.qty("Potato"), 30)
        self.assertEqual(self.call("get", f"/usage?owner_id={self.owner}&product_id={self.shop['Potato']}", 200)["entries"], [])
        self.use("Potato", 0, 400)
        self.use("Potato", -1, 400)
        self.call("post", "/usage", 409, {"ownerId": self.owner, "productId": self.vada, "quantity": 1})  # made from a recipe

    def test_undo_returns_it_to_stock(self):
        entry = self.use("Potato", 20)
        data = self.call("delete", f"/usage/{entry['id']}?owner_id={self.owner}", 200)
        self.assertEqual(data["stockQty"], 30)
        self.assertEqual(self.summary()["usageCost"], 0)
        self.call("delete", f"/usage/{entry['id']}?owner_id={self.owner}", 404)

    def test_use_then_sale_is_not_deducted_twice(self):
        self.use("Potato", 5)
        self.assertEqual(self.qty("Potato"), 25)
        self.sell(self.vada, 50)  # the recipe needs exactly those 5 KG
        self.assertEqual(self.qty("Potato"), 25)
        self.assertEqual(self.qty("Pav"), 50)  # not used by hand, so the recipe takes it
        self.assertEqual(self.summary()["usageCost"], 0)  # that cost is in the sale already

    def test_sale_then_use_is_not_deducted_twice(self):
        self.sell(self.vada, 50)
        self.assertEqual(self.qty("Potato"), 25)
        self.assertEqual(self.use("Potato", 5)["deducted"], 0)
        self.assertEqual(self.qty("Potato"), 25)

    def test_only_the_difference_leaves_stock_and_counts_as_cost(self):
        self.use("Potato", 7)
        self.sell(self.vada, 50)  # needs 5 of the 7 taken
        self.assertEqual(self.qty("Potato"), 23)
        self.assertEqual(self.summary()["usageCost"], 50)  # the 2 KG beyond the recipe
        self.sell(self.vada, 30)  # total need 8: one more
        self.assertEqual(self.qty("Potato"), 22)
        self.assertEqual(self.summary()["usageCost"], 0)
        self.assertEqual(self.stock()["Potato"]["usedToday"], 8)

    def test_removing_either_side_settles_correctly(self):
        entry = self.use("Potato", 5)
        tx = self.sell(self.vada, 50)["transaction"]
        self.assertEqual(self.qty("Potato"), 25)
        # The use is undone, but the sale still needs its 5 KG.
        self.call("delete", f"/usage/{entry['id']}?owner_id={self.owner}", 200)
        self.assertEqual(self.qty("Potato"), 25)
        self.call("delete", f"/transactions/{tx['id']}?owner_id={self.owner}", 200)
        self.assertEqual(self.qty("Potato"), 30)
        # Now the other way: the sale goes first and the use still stands.
        entry = self.use("Potato", 5)
        tx = self.sell(self.vada, 50)["transaction"]
        self.call("delete", f"/transactions/{tx['id']}?owner_id={self.owner}", 200)
        self.assertEqual(self.qty("Potato"), 25)
        self.call("delete", f"/usage/{entry['id']}?owner_id={self.owner}", 200)
        self.assertEqual(self.qty("Potato"), 30)

    def test_editing_a_sale_with_use_on_the_same_day(self):
        self.use("Potato", 7)
        tx = self.sell(self.vada, 50)["transaction"]
        self.call("put", f"/transactions/{tx['id']}", 200, {"ownerId": self.owner, "quantity": 100, "amount": 1500})
        self.assertEqual(self.qty("Potato"), 20)  # needs 10 now, more than the 7
        self.call("put", f"/transactions/{tx['id']}", 200, {"ownerId": self.owner, "quantity": 10, "amount": 150})
        self.assertEqual(self.qty("Potato"), 23)  # back to the 7 used by hand

    def test_a_sale_on_another_day_is_separate(self):
        self.use("Potato", 5, date="2026-10-01")
        self.sell(self.vada, 50)
        self.assertEqual(self.qty("Potato"), 20)

    def test_other_owners_cannot_touch_usage(self):
        from auth_token import issue_token
        entry = self.use("Potato", 5)
        with self.psycopg.connect(TEST_DB) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO userdetails (full_name, email, dob) VALUES ('Other', %s, '1990-01-01') RETURNING id", (f"{uuid.uuid4().hex}@test.local",))
            other = cur.fetchone()[0]
        mine, self.headers = self.headers, {"Authorization": f"Bearer {issue_token(other)}"}
        self.call("post", "/usage", 404, {"ownerId": other, "productId": self.shop["Potato"], "quantity": 1})
        self.call("delete", f"/usage/{entry['id']}?owner_id={other}", 404)
        self.assertEqual(self.call("get", f"/usage?owner_id={other}&product_id={self.shop['Potato']}", 200)["entries"], [])
        self.headers = mine
        self.assertEqual(self.qty("Potato"), 25)

    # Product Sheet and Excel ----------------------------------------------
    def bulk(self, products, expect=201):
        return self.call("post", "/products/bulk", expect, {"ownerId": self.owner, "stockMode": "replace", "products": products})

    def test_sheet_sets_the_type_of_new_and_existing_products(self):
        self.bulk([
            {"name": "Onion", "category": "Foods", "unit": "kg", "stockQty": 10, "costPrice": 20, "productType": "PURCHASE"},
            {"name": "Samosa", "category": "Foods", "unit": "piece", "stockQty": 5, "sellingPrice": 15, "productType": "SELL"},
            {"name": "NoType", "category": "Foods", "unit": "kg", "stockQty": 1},
            {"name": "Tea", "stockQty": 2, "productType": "PURCHASE"},
        ])
        s = self.stock()
        self.assertEqual(s["Onion"]["productType"], "PURCHASE")
        self.assertEqual(s["Samosa"]["productType"], "SELL")
        self.assertEqual(s["NoType"]["productType"], "SELL")
        self.assertEqual(s["Tea"]["productType"], "PURCHASE")
        self.bulk([{"name": "tea", "productType": "SELL"}])  # same product despite the case
        self.assertEqual(self.stock()["Tea"]["productType"], "SELL")
        self.bulk([{"name": "Onion", "stockQty": 12}])  # no type: leaves it as it is
        self.assertEqual(self.stock()["Onion"]["productType"], "PURCHASE")
        names = {r["name"]: r for sheet in self.call("get", f"/product-sheets?owner_id={self.owner}", 200)["sheets"] for r in sheet["rows"]}
        self.assertEqual(names["Onion"]["productType"], "PURCHASE")
        self.assertIsNone(names["Apple"]["productType"])

    def test_sheet_rejects_a_bad_save_as_a_whole(self):
        self.bulk([{"name": "Onion", "stockQty": 1}, {"name": "Bad", "productType": "MAYBE"}], 400)
        self.assertNotIn("Onion", self.stock())
        data = self.bulk([{"name": "Onion", "stockQty": 1}, {"name": "Vada Pav", "productType": "PURCHASE"}], 409)
        self.assertIn("recipe", data["error"])
        s = self.stock()
        self.assertNotIn("Onion", s)
        self.assertEqual(s["Vada Pav"]["productType"], "SELL")

    def test_excel_download_and_upload_through_the_api(self):
        file = self.call("get", f"/product-sheets/xlsx?owner_id={self.owner}", 200)
        parsed = self.call("post", "/product-sheets/parse", 200, {"ownerId": self.owner, "fileBase64": file["fileBase64"]})
        rows = {r["name"]: r for sheet in parsed["sheets"] for r in sheet["rows"]}
        self.assertEqual(rows["Potato"]["productType"], "PURCHASE")
        self.assertEqual(rows["Vada Pav"]["productType"], "SELL")

    # recipe rules ---------------------------------------------------------
    def test_ingredient_in_a_recipe_cannot_be_deleted(self):
        data = self.call("delete", f"/products/{self.shop['Potato']}?owner_id={self.owner}", 409)
        self.assertIn("Vada Pav", data["error"])
        self.recipe(self.vada, 10, [("Oil", 1, "litre")])
        self.call("delete", f"/products/{self.shop['Potato']}?owner_id={self.owner}", 200)

    def test_recipe_validation(self):
        self.recipe(self.vada, 10, [("Potato", 1, "litre")], 400)  # cannot convert
        self.recipe(self.vada, 10, [("Potato", 1, "kg"), ("Potato", 2, "kg")], 400)  # twice
        self.recipe(self.vada, 0, [("Potato", 1, "kg")], 400)  # makes nothing
        self.recipe(self.vada, 10, [("Potato", 0, "kg")], 400)  # no quantity
        self.recipe(self.vada, 10, [(self.vada, 1, "piece")], 400)  # itself
        self.recipe(self.shop["Potato"], 10, [("Oil", 1, "litre")], 409)  # a purchase product
        tea = self.product("Tea", "piece", 0, 10, 0, "SELL")["id"]
        self.recipe(tea, 10, [(self.vada, 1, "piece")], 400)  # an ingredient with its own recipe
        self.call("put", f"/products/{self.vada}", 409, {"ownerId": self.owner, "productType": "PURCHASE"})  # has a recipe
        self.call("put", f"/products/{self.vada}/recipe", 400, {"ownerId": self.owner, "yieldQty": 10, "ingredients": [{"productId": "x", "quantity": 1}]})
        self.assertEqual(self.stock()["Vada Pav"]["ingredientCount"], 4)  # untouched by any of those

    def test_other_owners_cannot_use_or_see_recipes(self):
        from auth_token import issue_token
        with self.psycopg.connect(TEST_DB) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO userdetails (full_name, email, dob) VALUES ('Other', %s, '1990-01-01') RETURNING id", (f"{uuid.uuid4().hex}@test.local",))
            other = cur.fetchone()[0]
        mine, self.headers = self.headers, {"Authorization": f"Bearer {issue_token(other)}"}
        owner, self.owner = self.owner, other
        theirs = self.product("Theirs", "piece", 5, 10, 1, "SELL")["id"]
        self.recipe(theirs, 1, [(self.shop["Pav"], 1, "piece")], 404)  # someone else's ingredient
        self.call("put", f"/products/{self.vada}/recipe", 404, {"ownerId": other, "yieldQty": 1, "ingredients": []})
        self.assertIsNone(self.call("get", f"/products/{self.vada}/recipe?owner_id={other}", 200)["recipe"])
        self.call("post", "/transactions", 404, {"ownerId": other, "productId": self.vada, "quantity": 1, "amount": 15, "type": "SALE"})
        self.headers, self.owner = mine, owner
        self.assertIngredients(30, 5, 100, 5)

    def test_removing_a_recipe_returns_to_own_stock(self):
        self.call("put", f"/products/{self.vada}/recipe", 200, {"ownerId": self.owner, "ingredients": []})
        vada = self.stock()["Vada Pav"]
        self.assertFalse(vada["hasRecipe"])
        self.assertEqual(vada["stockQty"], 18)
        self.sell(self.vada, 3)
        self.assertEqual(self.qty("Vada Pav"), 15)
        self.assertIngredients(30, 5, 100, 5)

    def test_restock_counts_ingredients_not_the_recipe_product(self):
        self.sell(self.vada, 90)
        data = self.call("get", f"/restock-suggestions?owner_id={self.owner}", 200)
        names = [s.get("productName") or s.get("name") for s in data.get("suggestions", data if isinstance(data, list) else [])]
        self.assertNotIn("Vada Pav", names)
        self.assertIn("Pav", names)


if __name__ == "__main__":
    unittest.main()
