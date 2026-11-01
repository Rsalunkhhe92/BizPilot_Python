"""Food Seller calculations (no database): .venv\\Scripts\\python.exe -m unittest test_foodseller_core -v"""

import unittest
from decimal import Decimal as D

from foodseller_core import (
    CoreError, allocate_discount, average_cost, compute_pnl, convert_qty, purchase_suggestion, split_usage_cost, stock_state,
    units_for_target, usage_target,
)

KG = {"id": 1, "name": "KG", "dimension": "mass", "to_base": D(1000)}
GRAM = {"id": 2, "name": "Gram", "dimension": "mass", "to_base": D(1)}
LITER = {"id": 3, "name": "Liter", "dimension": "volume", "to_base": D(1000)}
ML = {"id": 4, "name": "ML", "dimension": "volume", "to_base": D(1)}
PACKET = {"id": 5, "name": "Packet", "dimension": "custom", "to_base": D(1)}


class CoreTests(unittest.TestCase):
    def test_unit_conversion(self):
        self.assertEqual(convert_qty(500, GRAM, KG), D("0.5"))
        self.assertEqual(convert_qty(200, ML, LITER), D("0.2"))
        self.assertEqual(convert_qty(2, KG, GRAM), D(2000))
        self.assertEqual(convert_qty(3, PACKET, PACKET), D(3))
        with self.assertRaises(CoreError):
            convert_qty(1, KG, LITER)
        with self.assertRaises(CoreError):
            convert_qty(1, PACKET, KG)

    def test_usage_is_the_larger_of_manual_and_recipe(self):
        self.assertEqual(usage_target(5, 5), 5)
        self.assertEqual(usage_target(5, 3), 5)
        self.assertEqual(usage_target(3, 5), 5)
        self.assertEqual(usage_target(0, 2), 2)

    def test_usage_cost_split_adds_up(self):
        recipe, unmatched = split_usage_cost(D(100), D(5), D(3))
        self.assertEqual(recipe, D(60))
        self.assertEqual(unmatched, D(40))
        self.assertEqual(split_usage_cost(D(100), D(2), D(5)), (D(100), D(0)))
        self.assertEqual(split_usage_cost(D(0), D(0), D(0)), (D(0), D(0)))

    def test_average_cost_falls_back_when_stock_is_not_positive(self):
        self.assertEqual(average_cost(D(10), D(250), D(99)), D(25))
        self.assertEqual(average_cost(D(0), D(0), D(30)), D(30))
        self.assertEqual(average_cost(D(-2), D(-50), D(30)), D(30))
        self.assertEqual(average_cost(D(2), D(-50), D(30)), D(30))

    def test_discount_shares_add_up_exactly(self):
        shares = allocate_discount([D("10.00"), D("10.00"), D("10.00")], D("10.00"))
        self.assertEqual(sum(shares), D("10.00"))
        self.assertEqual(allocate_discount([D(50)], D(0)), [D(0)])

    def test_stock_state(self):
        self.assertEqual(stock_state(0, 5), "OUT")
        self.assertEqual(stock_state(-1, 0), "OUT")
        self.assertEqual(stock_state(5, 5), "LOW")
        self.assertEqual(stock_state(6, 5), "OK")
        self.assertEqual(stock_state(1, 0), "OK")

    def test_profit_ignores_purchases_and_personal_use(self):
        result = compute_pnl({"revenue": 1000, "recipe_cost": 300, "unmatched_cost": 50, "wastage": 20, "spoilage": 10, "personal": 99,
                              "adjustment_net": -5, "expenses": 100, "purchases": 5000})
        self.assertEqual(result["cogs"], 350)
        self.assertEqual(result["gross_profit"], 650)
        self.assertEqual(result["net_profit"], 515)

    def test_purchase_suggestion(self):
        self.assertEqual(purchase_suggestion(D(2), D(1), D(3), 2), D(5))
        self.assertEqual(purchase_suggestion(D(100), D(1), D(3), 2), 0)
        self.assertEqual(purchase_suggestion(D(0), D(0), D("0.33"), 1), D("0.4"))

    def test_units_for_target(self):
        self.assertEqual(units_for_target(1000, 0, D(5)), 200)
        self.assertEqual(units_for_target(1000, 100, D(3)), 367)
        self.assertIsNone(units_for_target(1000, 0, D(0)))
        self.assertIsNone(units_for_target(1000, 0, D(-1)))

    def test_bad_numbers_are_rejected(self):
        for bad in ("abc", "NaN", "Infinity", None, True):
            with self.assertRaises(CoreError):
                usage_target(bad, 1)


if __name__ == "__main__":
    unittest.main()
