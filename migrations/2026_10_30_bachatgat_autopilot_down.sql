-- Rollback of 2026_10_30_bachatgat_autopilot.sql (drops the Autopilot log and settings).
BEGIN;
DROP TABLE IF EXISTS shg_agent_scans;
DROP TABLE IF EXISTS shg_agent_actions;
DROP TABLE IF EXISTS shg_agent_runs;
DROP TABLE IF EXISTS shg_agent_settings;
ALTER TABLE shg_gateway_payments DROP COLUMN IF EXISTS loan_id;
ALTER TABLE shg_payment_links DROP COLUMN IF EXISTS loan_id;
COMMIT;
