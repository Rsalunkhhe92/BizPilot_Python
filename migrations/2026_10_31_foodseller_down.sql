-- Rollback of 2026_10_31_foodseller.sql
-- WARNING: permanently deletes ALL Food Seller data. Take a backup first:
--     pg_dump "$DATABASE_URL" -t 'food_*' -f food_backup.sql
-- Customers whose business type is 'Food Seller' are left in place; reassign them before deleting the type.

BEGIN;
DROP TABLE IF EXISTS food_request_keys CASCADE;
DROP TABLE IF EXISTS food_ai_proposals CASCADE;
DROP TABLE IF EXISTS food_notifications CASCADE;
DROP TABLE IF EXISTS food_payments CASCADE;
DROP TABLE IF EXISTS food_stock_adjustments CASCADE;
DROP TABLE IF EXISTS food_expense_transactions CASCADE;
DROP TABLE IF EXISTS food_expense_categories CASCADE;
DROP TABLE IF EXISTS food_sales_item_usage CASCADE;
DROP TABLE IF EXISTS food_sales_items CASCADE;
DROP TABLE IF EXISTS food_sales_transactions CASCADE;
DROP TABLE IF EXISTS food_recipe_ingredients CASCADE;
DROP TABLE IF EXISTS food_recipes CASCADE;
DROP TABLE IF EXISTS food_products CASCADE;
DROP TABLE IF EXISTS food_consumption_items CASCADE;
DROP TABLE IF EXISTS food_consumption_transactions CASCADE;
DROP TABLE IF EXISTS food_purchase_items CASCADE;
DROP TABLE IF EXISTS food_purchase_transactions CASCADE;
DROP TABLE IF EXISTS food_inventory_transactions CASCADE;
DROP TABLE IF EXISTS food_inventory_products CASCADE;
DROP TABLE IF EXISTS food_suppliers CASCADE;
DROP TABLE IF EXISTS food_product_categories CASCADE;
DROP TABLE IF EXISTS food_units CASCADE;
DROP TABLE IF EXISTS food_settings CASCADE;
DROP FUNCTION IF EXISTS food_touch_updated_at();
DROP FUNCTION IF EXISTS food_ledger_is_append_only();
DELETE FROM business_types WHERE name = 'Food Seller';
COMMIT;
