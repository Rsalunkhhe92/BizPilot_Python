-- Mahila Bachat Gat Autopilot (AI agent) - 2026-10-30
-- Applied automatically at startup by ensure_bachatgat_schema(); this file is for manual review/apply.
-- Idempotent. Rollback: 2026_10_30_bachatgat_autopilot_down.sql
BEGIN;
-- A payment link may collect a loan installment instead of savings (sent by the Autopilot).
ALTER TABLE shg_payment_links ADD COLUMN IF NOT EXISTS loan_id BIGINT REFERENCES shg_loans(id);
ALTER TABLE shg_gateway_payments ADD COLUMN IF NOT EXISTS loan_id BIGINT REFERENCES shg_loans(id);

-- Autopilot (AI agent) ----------------------------------------------------------
-- Per-group switches. auto_loan_limit NULL = use the policy's loan_approval_limit.
CREATE TABLE IF NOT EXISTS shg_agent_settings (
    group_id BIGINT PRIMARY KEY REFERENCES shg_groups(id) ON DELETE CASCADE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    auto_members BOOLEAN NOT NULL DEFAULT TRUE,
    auto_dues BOOLEAN NOT NULL DEFAULT TRUE,
    auto_payment_links BOOLEAN NOT NULL DEFAULT TRUE,
    auto_loans BOOLEAN NOT NULL DEFAULT TRUE,
    auto_loan_limit NUMERIC(12, 2) CHECK (auto_loan_limit >= 0),
    auto_disburse BOOLEAN NOT NULL DEFAULT TRUE,
    disburse_method VARCHAR(20) NOT NULL DEFAULT 'Cash' CHECK (disburse_method IN ('Cash', 'Bank Transfer', 'UPI')),
    auto_meetings BOOLEAN NOT NULL DEFAULT TRUE,
    auto_close_books BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shg_agent_runs (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    trigger VARCHAR(10) NOT NULL CHECK (trigger IN ('SCHEDULE', 'MANUAL', 'SCAN', 'APPROVAL')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    done_count INT NOT NULL DEFAULT 0,
    attention_count INT NOT NULL DEFAULT 0,
    failed_count INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_shg_agent_runs_group ON shg_agent_runs(group_id, started_at DESC);

-- Everything the Autopilot did (Done / Failed / Undone) and what it needs a person for
-- (Needs Attention, one row per open issue via dedupe_key; Resolved when it no longer applies).
CREATE TABLE IF NOT EXISTS shg_agent_actions (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    run_id BIGINT REFERENCES shg_agent_runs(id) ON DELETE SET NULL,
    action_type VARCHAR(40) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('Done', 'Failed', 'Needs Attention', 'Resolved', 'Undone')),
    message VARCHAR(500) NOT NULL,
    entity_type VARCHAR(20),
    entity_id BIGINT,
    txn_id BIGINT REFERENCES shg_transactions(id),
    amount NUMERIC(12, 2),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key VARCHAR(120),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shg_agent_actions_group ON shg_agent_actions(group_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shg_agent_actions_entity ON shg_agent_actions(entity_type, entity_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_shg_agent_attention ON shg_agent_actions(group_id, dedupe_key) WHERE dedupe_key IS NOT NULL;

-- Photos of the meeting register read by the AI; the image hash stops one photo being posted twice.
CREATE TABLE IF NOT EXISTS shg_agent_scans (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    image_sha256 VARCHAR(64) NOT NULL,
    document_id BIGINT REFERENCES shg_documents(id) ON DELETE SET NULL,
    run_id BIGINT REFERENCES shg_agent_runs(id) ON DELETE SET NULL,
    extracted JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shg_agent_scan UNIQUE (group_id, image_sha256)
);
COMMIT;
