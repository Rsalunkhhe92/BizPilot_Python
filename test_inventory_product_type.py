"""Sell / Purchase classification and recipe units for product shops (no database)."""

import base64
import io
import unittest
from decimal import Decimal as D

from inventory_api import convert_unit, product_type_of
from product_sheets import SheetFileError, build_sheets, build_workbook_base64, parse_workbook_base64


class ProductTypeTests(unittest.TestCase):
    def test_the_owners_choice_is_used(self):
        self.assertEqual(product_type_of("PURCHASE"), "PURCHASE")
        self.assertEqual(product_type_of("SELL"), "SELL")

    def test_unclassified_reads_as_sell(self):
        # A new product is usually bought before its rate is set; it must stay sellable.
        self.assertEqual(product_type_of(None), "SELL")


class UnitTests(unittest.TestCase):
    def test_same_unit_and_conversions(self):
        self.assertEqual(convert_unit(D(5), "piece", "piece"), D(5))
        self.assertEqual(convert_unit(D(250), "gram", "kg"), D("0.25"))
        self.assertEqual(convert_unit(D(200), "ml", "litre"), D("0.2"))
        self.assertEqual(convert_unit(D(1), "KG", "gram"), D(1000))
        self.assertEqual(convert_unit(D(2), "dozen", "piece"), D(24))

    def test_units_that_cannot_convert(self):
        self.assertIsNone(convert_unit(D(1), "kg", "litre"))
        self.assertIsNone(convert_unit(D(1), "packet", "kg"))
        self.assertEqual(convert_unit(D(1), "packet", "Packet"), D(1))


class SheetFileTests(unittest.TestCase):
    """The Type column in the Excel file, both ways."""

    def file_with(self, rows):
        from openpyxl import Workbook
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Foods"
        for row in rows:
            worksheet.append(row)
        buffer = io.BytesIO()
        workbook.save(buffer)
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def test_download_has_the_type_and_reads_back(self):
        sheets = build_sheets("Fruit sellers", [
            {"name": "Vadapav", "category": "Foods", "unit": "piece", "stockQty": 18, "sellingPrice": 15, "costPrice": 3, "productType": "SELL"},
            {"name": "Potatao", "category": "Foods", "unit": "kg", "stockQty": 25, "sellingPrice": 0, "costPrice": 32, "productType": "PURCHASE"},
        ])
        parsed = parse_workbook_base64(build_workbook_base64(sheets))
        rows = {r["name"]: r for r in parsed[0]["rows"]}
        self.assertEqual(rows["Vadapav"]["productType"], "SELL")
        self.assertEqual(rows["Potatao"]["productType"], "PURCHASE")
        self.assertEqual(rows["Potatao"]["unit"], "kg")
        self.assertEqual(rows["Potatao"]["stockQty"], 25)
        self.assertEqual(rows["Potatao"]["costPrice"], 32)
        # A suggested product nobody has chosen a type for stays blank.
        apple = next(r for s in parsed for r in s["rows"] if r["name"] == "Apple")
        self.assertIsNone(apple["productType"])

    def test_the_template_offers_a_dropdown(self):
        from openpyxl import load_workbook
        data = base64.b64decode(build_workbook_base64(build_sheets("Fruit sellers", [])))
        worksheet = load_workbook(io.BytesIO(data)).worksheets[0]
        self.assertEqual(worksheet["B1"].value, "Type")
        lists = [v for v in worksheet.data_validations.dataValidation if v.type == "list"]
        self.assertEqual(lists[0].formula1, '"Sell,Purchase"')

    def test_owner_words_and_marathi(self):
        parsed = parse_workbook_base64(self.file_with([
            ["Product", "Type", "Stock"], ["A", "purchase", 1], ["B", " SELL ", 1], ["C", "खरेदी", 1], ["D", "विक्री", 1], ["E", "Buy", 1], ["F", None, 1],
        ]))
        self.assertEqual([r["productType"] for r in parsed[0]["rows"]], ["PURCHASE", "SELL", "PURCHASE", "SELL", "PURCHASE", None])

    def test_columns_can_move(self):
        parsed = parse_workbook_base64(self.file_with([["Cost Price", "Product", "Stock", "Type"], [32, "Potatao", 25, "Purchase"]]))
        row = parsed[0]["rows"][0]
        self.assertEqual((row["name"], row["productType"], row["costPrice"]), ("Potatao", "PURCHASE", 32))

    def test_a_wrong_type_is_reported_with_its_product(self):
        with self.assertRaises(SheetFileError) as caught:
            parse_workbook_base64(self.file_with([["Product", "Type"], ["Potatao", "Maybe"]]))
        self.assertIn("Potatao", str(caught.exception))
        self.assertIn("Sell or Purchase", str(caught.exception))

    def test_older_files_still_load(self):
        with_headers = parse_workbook_base64(self.file_with([["Product", "Unit", "Stock", "Selling Price", "Cost Price"], ["Mango", "kg", 5, 100, 60]]))
        no_headers = parse_workbook_base64(self.file_with([["Mango", "kg", 5, 100, 60]]))
        for parsed in (with_headers, no_headers):
            row = parsed[0]["rows"][0]
            self.assertEqual((row["unit"], row["stockQty"], row["sellingPrice"], row["costPrice"], row["productType"]), ("kg", 5, 100, 60, None))


if __name__ == "__main__":
    unittest.main()
