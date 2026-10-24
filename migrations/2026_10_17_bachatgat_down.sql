-- Rollback of 2026_10_17_bachatgat.sql
-- WARNING: permanently deletes ALL Mahila Bachat Gat data. Take a backup first:
--     pg_dump "$DATABASE_URL" -t 'shg_*' -f shg_backup.sql
-- Customers whose business type is 'Mahila Bachat Gat' are left in place; reassign them before deleting the type.

BEGIN;
DROP TABLE IF EXISTS shg_gateway_payments CASCADE;
DROP TABLE IF EXISTS shg_payment_links CASCADE;
DROP TABLE IF EXISTS shg_member_gateway CASCADE;
DROP TABLE IF EXISTS shg_group_gateway CASCADE;
DROP TABLE IF EXISTS shg_notifications CASCADE;
DROP TABLE IF EXISTS shg_documents CASCADE;
DROP TABLE IF EXISTS shg_counters CASCADE;
DROP TABLE IF EXISTS shg_request_keys CASCADE;
DROP TABLE IF EXISTS shg_bank_reconciliations CASCADE;
DROP TABLE IF EXISTS shg_period_closes CASCADE;
DROP TABLE IF EXISTS shg_allocations CASCADE;
DROP TABLE IF EXISTS shg_ledger_lines CASCADE;
DROP TABLE IF EXISTS shg_transactions CASCADE;
DROP TABLE IF EXISTS shg_scheme_utilizations CASCADE;
DROP TABLE IF EXISTS shg_scheme_applications CASCADE;
DROP TABLE IF EXISTS shg_business_assets CASCADE;
DROP TABLE IF EXISTS shg_business_work CASCADE;
DROP TABLE IF EXISTS shg_stock_movements CASCADE;
DROP TABLE IF EXISTS shg_business_order_lines CASCADE;
DROP TABLE IF EXISTS shg_business_orders CASCADE;
DROP TABLE IF EXISTS shg_business_parties CASCADE;
DROP TABLE IF EXISTS shg_business_products CASCADE;
DROP TABLE IF EXISTS shg_business_activities CASCADE;
DROP TABLE IF EXISTS shg_savings_dues CASCADE;
DROP TABLE IF EXISTS shg_resolutions CASCADE;
DROP TABLE IF EXISTS shg_events CASCADE;
DROP TABLE IF EXISTS shg_loan_installments CASCADE;
DROP TABLE IF EXISTS shg_loans CASCADE;
DROP TABLE IF EXISTS shg_meeting_attendance CASCADE;
DROP TABLE IF EXISTS shg_meetings CASCADE;
DROP TABLE IF EXISTS shg_bank_accounts CASCADE;
DROP TABLE IF EXISTS shg_members CASCADE;
DROP TABLE IF EXISTS shg_group_officers CASCADE;
DROP TABLE IF EXISTS shg_group_policies CASCADE;
DROP TABLE IF EXISTS shg_groups CASCADE;
DROP FUNCTION IF EXISTS shg_check_txn_balanced();
DROP FUNCTION IF EXISTS shg_block_line_update();
DELETE FROM business_types bt WHERE bt.name = 'Mahila Bachat Gat'
  AND NOT EXISTS (SELECT 1 FROM userdetails u WHERE u.business_type = bt.name);
COMMIT;
