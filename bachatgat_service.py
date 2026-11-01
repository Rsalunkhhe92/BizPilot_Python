"""
Mahila Bachat Gat (महिला बचत गट / Women's Self-Help Group) - schema provisioning.

Architecture note: like Building Maintenance, the tenant is one customer account
(userdetails.id, called owner_id here). One tenant can run many groups. SHG
members, office-bearers and officers are records maintained by the tenant -
they are not application users and have no login of their own.

Every table is prefixed shg_ so nothing collides with other business types'
tables (e.g. inventory's products / sales). All statements are idempotent
(CREATE ... IF NOT EXISTS), matching the other ensure_*_schema functions that
app.py runs at startup; the same SQL is mirrored in
migrations/2026_10_17_bachatgat.sql for review and manual application.

Money in the books lives in exactly one place: shg_ledger_lines (double entry,
one shg_transactions header per posting). Balances, cashbook, bankbook and
reports are always computed from posted lines - never stored or edited by hand.
"""

BUSINESS_TYPE_NAME = "Mahila Bachat Gat"
BUSINESS_TYPE_DESCRIPTION = (
    "महिला बचत गट - Self-Help Group (SHG): women's group savings, internal lending, "
    "financial management, meetings, accounting, and livelihood activities."
)

# Feature keys (namespaced shg_ like Building Maintenance's building_ keys).
BACHAT_GAT_FEATURES = [
    "shg_groups", "shg_members", "shg_savings", "shg_loans", "shg_meetings",
    "shg_accounts", "shg_business", "shg_schemes", "shg_reports", "shg_settings",
]

SCHEMA_SQL = """
-- Groups -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shg_groups (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    group_code VARCHAR(30) NOT NULL,
    name_en VARCHAR(150) NOT NULL,
    name_mr VARCHAR(150),
    formation_date DATE NOT NULL,
    registration_number VARCHAR(80),
    registration_authority VARCHAR(150),
    state VARCHAR(80),
    district VARCHAR(80),
    taluka VARCHAR(80),
    village VARCHAR(120),
    gram_panchayat VARCHAR(120),
    address TEXT,
    category VARCHAR(60),
    livelihood_activity VARCHAR(150),
    meeting_frequency VARCHAR(20) NOT NULL DEFAULT 'MONTHLY'
        CHECK (meeting_frequency IN ('WEEKLY', 'FORTNIGHTLY', 'MONTHLY')),
    status VARCHAR(20) NOT NULL DEFAULT 'Draft'
        CHECK (status IN ('Draft', 'Pending Approval', 'Active', 'Suspended', 'Closed')),
    verification_status VARCHAR(20) NOT NULL DEFAULT 'Not Verified'
        CHECK (verification_status IN ('Not Verified', 'Verified', 'Rejected')),
    reviewed_by_officer_id BIGINT,
    review_remarks TEXT,
    activated_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    version INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shg_group_code UNIQUE (owner_id, group_code)
);
CREATE INDEX IF NOT EXISTS idx_shg_groups_owner ON shg_groups(owner_id, status);

-- Group policy: savings, loan, penalty, approval and rounding rules (one row per group)
CREATE TABLE IF NOT EXISTS shg_group_policies (
    group_id BIGINT PRIMARY KEY REFERENCES shg_groups(id) ON DELETE CASCADE,
    savings_frequency VARCHAR(20) NOT NULL DEFAULT 'MONTHLY'
        CHECK (savings_frequency IN ('WEEKLY', 'FORTNIGHTLY', 'MONTHLY')),
    savings_amount NUMERIC(12, 2) NOT NULL DEFAULT 100 CHECK (savings_amount >= 0),
    savings_due_day INT NOT NULL DEFAULT 10 CHECK (savings_due_day BETWEEN 1 AND 28),
    allow_savings_withdrawal BOOLEAN NOT NULL DEFAULT FALSE,
    min_members INT NOT NULL DEFAULT 10 CHECK (min_members BETWEEN 1 AND 100),
    quorum_percent INT NOT NULL DEFAULT 50 CHECK (quorum_percent BETWEEN 1 AND 100),
    default_interest_method VARCHAR(10) NOT NULL DEFAULT 'FLAT' CHECK (default_interest_method IN ('FLAT', 'REDUCING')),
    default_interest_rate NUMERIC(6, 2) NOT NULL DEFAULT 12 CHECK (default_interest_rate BETWEEN 0 AND 60),
    default_frequency VARCHAR(20) NOT NULL DEFAULT 'MONTHLY'
        CHECK (default_frequency IN ('WEEKLY', 'FORTNIGHTLY', 'MONTHLY')),
    max_installments INT NOT NULL DEFAULT 24 CHECK (max_installments BETWEEN 1 AND 520),
    max_loan_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
    savings_multiple NUMERIC(6, 2) NOT NULL DEFAULT 4,
    min_membership_months INT NOT NULL DEFAULT 6,
    allow_multiple_loans BOOLEAN NOT NULL DEFAULT FALSE,
    loan_approval_limit NUMERIC(12, 2) NOT NULL DEFAULT 0,
    expense_approval_limit NUMERIC(12, 2) NOT NULL DEFAULT 1000,
    penalty_type VARCHAR(20) NOT NULL DEFAULT 'NONE' CHECK (penalty_type IN ('NONE', 'FLAT', 'PERCENT_PER_MONTH')),
    penalty_value NUMERIC(10, 2) NOT NULL DEFAULT 0 CHECK (penalty_value >= 0),
    grace_days INT NOT NULL DEFAULT 0 CHECK (grace_days BETWEEN 0 AND 90),
    round_to_rupee BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Authorized officers (Group Manager / Field Officer / Auditor) - records, not logins
CREATE TABLE IF NOT EXISTS shg_group_officers (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    full_name VARCHAR(150) NOT NULL,
    designation VARCHAR(30) NOT NULL CHECK (designation IN ('Group Manager', 'Field Officer', 'Auditor')),
    mobile VARCHAR(20),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shg_officers_group ON shg_group_officers(group_id);

-- Members ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shg_members (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    member_number INT NOT NULL,
    full_name VARCHAR(150) NOT NULL,
    mobile VARCHAR(20) NOT NULL,
    address TEXT,
    date_of_birth DATE,
    joining_date DATE NOT NULL,
    occupation VARCHAR(120),
    livelihood_activity VARCHAR(150),
    nominee_name VARCHAR(150),
    nominee_relation VARCHAR(60),
    nominee_mobile VARCHAR(20),
    id_proof_type VARCHAR(40),
    id_proof_last4 VARCHAR(4),
    id_verification_status VARCHAR(20) NOT NULL DEFAULT 'Pending'
        CHECK (id_verification_status IN ('Pending', 'Verified', 'Rejected', 'Not Required')),
    role VARCHAR(20) NOT NULL DEFAULT 'Member' CHECK (role IN ('President', 'Secretary', 'Treasurer', 'Member')),
    status VARCHAR(20) NOT NULL DEFAULT 'Pending'
        CHECK (status IN ('Pending', 'Active', 'Suspended', 'Exited', 'Rejected')),
    photo_document_id BIGINT,
    exit_date DATE,
    exit_reason TEXT,
    version INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shg_member_number UNIQUE (group_id, member_number)
);
CREATE INDEX IF NOT EXISTS idx_shg_members_group ON shg_members(group_id, status);
-- A woman may belong to only one SHG of a tenant at a time (exited/rejected records don't count).
CREATE UNIQUE INDEX IF NOT EXISTS uq_shg_member_mobile_active
    ON shg_members(owner_id, mobile) WHERE status IN ('Pending', 'Active', 'Suspended');
-- One President / Secretary / Treasurer per group.
CREATE UNIQUE INDEX IF NOT EXISTS uq_shg_office_bearer
    ON shg_members(group_id, role) WHERE role <> 'Member' AND status = 'Active';

-- Bank accounts ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shg_bank_accounts (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    bank_name VARCHAR(120) NOT NULL,
    branch VARCHAR(120),
    account_number VARCHAR(30) NOT NULL,
    ifsc VARCHAR(11),
    account_type VARCHAR(30) NOT NULL DEFAULT 'Savings',
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shg_bank_account UNIQUE (group_id, account_number)
);

-- Meetings (before loans: loans reference resolutions) ----------------------
CREATE TABLE IF NOT EXISTS shg_meetings (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    meeting_number INT NOT NULL,
    meeting_date DATE NOT NULL,
    meeting_time VARCHAR(10),
    location VARCHAR(200),
    agenda TEXT,
    discussions TEXT,
    minutes TEXT,
    decisions TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'Scheduled'
        CHECK (status IN ('Scheduled', 'Held', 'Minutes Approved', 'Cancelled')),
    president_confirmed_at TIMESTAMPTZ,
    secretary_confirmed_at TIMESTAMPTZ,
    next_meeting_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shg_meeting_number UNIQUE (group_id, meeting_number)
);
CREATE INDEX IF NOT EXISTS idx_shg_meetings_group ON shg_meetings(group_id, meeting_date);

CREATE TABLE IF NOT EXISTS shg_meeting_attendance (
    meeting_id BIGINT NOT NULL REFERENCES shg_meetings(id) ON DELETE CASCADE,
    member_id BIGINT NOT NULL REFERENCES shg_members(id) ON DELETE CASCADE,
    present BOOLEAN NOT NULL,
    remarks VARCHAR(200),
    PRIMARY KEY (meeting_id, member_id)
);

-- Loans (application + loan account in one row; status carries the lifecycle)
CREATE TABLE IF NOT EXISTS shg_loans (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    member_id BIGINT NOT NULL REFERENCES shg_members(id),
    application_number VARCHAR(40) NOT NULL,
    application_date DATE NOT NULL,
    requested_amount NUMERIC(12, 2) NOT NULL CHECK (requested_amount > 0),
    approved_amount NUMERIC(12, 2),
    purpose VARCHAR(200) NOT NULL,
    interest_method VARCHAR(10) NOT NULL CHECK (interest_method IN ('FLAT', 'REDUCING')),
    interest_rate NUMERIC(6, 2) NOT NULL CHECK (interest_rate BETWEEN 0 AND 60),
    installments INT NOT NULL CHECK (installments BETWEEN 1 AND 520),
    frequency VARCHAR(20) NOT NULL CHECK (frequency IN ('WEEKLY', 'FORTNIGHTLY', 'MONTHLY')),
    remarks TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'Draft' CHECK (status IN (
        'Draft', 'Submitted', 'Under Review', 'Approved', 'Rejected',
        'Disbursed', 'Active', 'Overdue', 'Closed', 'Written Off')),
    review_remarks TEXT,
    resolution_id BIGINT,
    rejection_reason TEXT,
    disbursement_authorized_by BIGINT REFERENCES shg_members(id),
    disbursed_on DATE,
    first_due_date DATE,
    schedule_version INT NOT NULL DEFAULT 0,
    closed_on DATE,
    version INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shg_loan_application UNIQUE (group_id, application_number)
);
CREATE INDEX IF NOT EXISTS idx_shg_loans_group ON shg_loans(group_id, status);
CREATE INDEX IF NOT EXISTS idx_shg_loans_member ON shg_loans(member_id);

CREATE TABLE IF NOT EXISTS shg_loan_installments (
    id BIGSERIAL PRIMARY KEY,
    loan_id BIGINT NOT NULL REFERENCES shg_loans(id) ON DELETE CASCADE,
    schedule_version INT NOT NULL,
    installment_number INT NOT NULL,
    due_date DATE NOT NULL,
    principal_due NUMERIC(12, 2) NOT NULL CHECK (principal_due >= 0),
    interest_due NUMERIC(12, 2) NOT NULL CHECK (interest_due >= 0),
    principal_paid NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (principal_paid >= 0),
    interest_paid NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (interest_paid >= 0),
    interest_waived NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (interest_waived >= 0),
    penalty_due NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (penalty_due >= 0),
    penalty_paid NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (penalty_paid >= 0),
    penalty_waived NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (penalty_waived >= 0),
    principal_written_off NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (principal_written_off >= 0),
    status VARCHAR(20) NOT NULL DEFAULT 'Pending'
        CHECK (status IN ('Pending', 'Partial', 'Paid', 'Rescheduled', 'Written Off')),
    CONSTRAINT uq_shg_installment UNIQUE (loan_id, schedule_version, installment_number),
    CONSTRAINT ck_shg_installment_principal CHECK (principal_paid + principal_written_off <= principal_due),
    CONSTRAINT ck_shg_installment_interest CHECK (interest_paid + interest_waived <= interest_due),
    CONSTRAINT ck_shg_installment_penalty CHECK (penalty_paid + penalty_waived <= penalty_due)
);
CREATE INDEX IF NOT EXISTS idx_shg_installments_due ON shg_loan_installments(loan_id, due_date);

-- Loan / group / member lifecycle history (approval & audit trail)
CREATE TABLE IF NOT EXISTS shg_events (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    group_id BIGINT REFERENCES shg_groups(id) ON DELETE CASCADE,
    entity_type VARCHAR(30) NOT NULL,
    entity_id BIGINT,
    action VARCHAR(60) NOT NULL,
    from_status VARCHAR(30),
    to_status VARCHAR(30),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    performed_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shg_events_entity ON shg_events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_shg_events_owner ON shg_events(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS shg_resolutions (
    id BIGSERIAL PRIMARY KEY,
    meeting_id BIGINT NOT NULL REFERENCES shg_meetings(id) ON DELETE CASCADE,
    resolution_number INT NOT NULL,
    resolution_type VARCHAR(30) NOT NULL CHECK (resolution_type IN (
        'LOAN_APPROVAL', 'LOAN_REJECTION', 'LOAN_RESCHEDULE', 'LOAN_WAIVER', 'LOAN_WRITE_OFF',
        'EXPENSE_APPROVAL', 'SAVINGS_WITHDRAWAL', 'BUSINESS_DECISION', 'GENERAL')),
    subject VARCHAR(200) NOT NULL,
    resolution_text TEXT,
    loan_id BIGINT REFERENCES shg_loans(id),
    amount NUMERIC(12, 2),
    votes_for INT NOT NULL DEFAULT 0 CHECK (votes_for >= 0),
    votes_against INT NOT NULL DEFAULT 0 CHECK (votes_against >= 0),
    votes_abstain INT NOT NULL DEFAULT 0 CHECK (votes_abstain >= 0),
    result VARCHAR(10) NOT NULL CHECK (result IN ('Passed', 'Failed')),
    consumed_by_type VARCHAR(30),
    consumed_by_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shg_resolution_number UNIQUE (meeting_id, resolution_number)
);
CREATE INDEX IF NOT EXISTS idx_shg_resolutions_loan ON shg_resolutions(loan_id);

-- Savings dues --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shg_savings_dues (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    member_id BIGINT NOT NULL REFERENCES shg_members(id) ON DELETE CASCADE,
    period_start DATE NOT NULL,
    due_date DATE NOT NULL,
    due_amount NUMERIC(12, 2) NOT NULL CHECK (due_amount >= 0),
    paid_amount NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (paid_amount >= 0),
    status VARCHAR(10) NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Partial', 'Paid', 'Waived')),
    CONSTRAINT uq_shg_savings_due UNIQUE (member_id, period_start),
    CONSTRAINT ck_shg_savings_due_paid CHECK (paid_amount <= due_amount)
);
CREATE INDEX IF NOT EXISTS idx_shg_savings_dues_group ON shg_savings_dues(group_id, period_start);

-- Business / livelihood -------------------------------------------------------
CREATE TABLE IF NOT EXISTS shg_business_activities (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    name VARCHAR(150) NOT NULL,
    activity_type VARCHAR(40) NOT NULL,
    description TEXT,
    start_date DATE,
    status VARCHAR(10) NOT NULL DEFAULT 'Active' CHECK (status IN ('Active', 'Paused', 'Closed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shg_business_group ON shg_business_activities(group_id);

CREATE TABLE IF NOT EXISTS shg_business_products (
    id BIGSERIAL PRIMARY KEY,
    activity_id BIGINT NOT NULL REFERENCES shg_business_activities(id) ON DELETE CASCADE,
    name VARCHAR(150) NOT NULL,
    kind VARCHAR(20) NOT NULL DEFAULT 'Product' CHECK (kind IN ('Product', 'Raw Material', 'Service')),
    unit VARCHAR(20) NOT NULL DEFAULT 'pcs',
    sale_price NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (sale_price >= 0),
    purchase_price NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (purchase_price >= 0),
    stock_qty NUMERIC(14, 3) NOT NULL DEFAULT 0,
    reorder_level NUMERIC(14, 3) NOT NULL DEFAULT 0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT uq_shg_product_name UNIQUE (activity_id, name),
    CONSTRAINT ck_shg_stock_non_negative CHECK (kind = 'Service' OR stock_qty >= 0)
);

CREATE TABLE IF NOT EXISTS shg_business_parties (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    party_type VARCHAR(10) NOT NULL CHECK (party_type IN ('Customer', 'Supplier')),
    name VARCHAR(150) NOT NULL,
    mobile VARCHAR(20),
    address TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shg_business_orders (
    id BIGSERIAL PRIMARY KEY,
    activity_id BIGINT NOT NULL REFERENCES shg_business_activities(id) ON DELETE CASCADE,
    order_type VARCHAR(10) NOT NULL CHECK (order_type IN ('SALE', 'PURCHASE')),
    order_number VARCHAR(40) NOT NULL,
    party_id BIGINT REFERENCES shg_business_parties(id),
    order_date DATE NOT NULL,
    total_amount NUMERIC(12, 2) NOT NULL CHECK (total_amount >= 0),
    paid_amount NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (paid_amount >= 0),
    status VARCHAR(12) NOT NULL DEFAULT 'Open' CHECK (status IN ('Open', 'Partial', 'Paid', 'Cancelled')),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shg_order_number UNIQUE (activity_id, order_number),
    CONSTRAINT ck_shg_order_paid CHECK (paid_amount <= total_amount)
);

CREATE TABLE IF NOT EXISTS shg_business_order_lines (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES shg_business_orders(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES shg_business_products(id),
    quantity NUMERIC(14, 3) NOT NULL CHECK (quantity > 0),
    rate NUMERIC(12, 2) NOT NULL CHECK (rate >= 0),
    amount NUMERIC(12, 2) NOT NULL CHECK (amount >= 0)
);

CREATE TABLE IF NOT EXISTS shg_stock_movements (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES shg_business_products(id) ON DELETE CASCADE,
    movement_date DATE NOT NULL,
    movement_type VARCHAR(20) NOT NULL CHECK (movement_type IN (
        'PURCHASE', 'SALE', 'PRODUCTION', 'CONSUMPTION', 'ADJUSTMENT', 'CANCEL')),
    quantity NUMERIC(14, 3) NOT NULL,
    order_id BIGINT REFERENCES shg_business_orders(id),
    notes VARCHAR(200),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shg_stock_product ON shg_stock_movements(product_id, movement_date);

CREATE TABLE IF NOT EXISTS shg_business_work (
    id BIGSERIAL PRIMARY KEY,
    activity_id BIGINT NOT NULL REFERENCES shg_business_activities(id) ON DELETE CASCADE,
    member_id BIGINT NOT NULL REFERENCES shg_members(id),
    work_date DATE NOT NULL,
    description VARCHAR(200) NOT NULL,
    quantity NUMERIC(14, 3),
    earnings NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (earnings >= 0),
    paid_txn_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shg_business_assets (
    id BIGSERIAL PRIMARY KEY,
    activity_id BIGINT NOT NULL REFERENCES shg_business_activities(id) ON DELETE CASCADE,
    name VARCHAR(150) NOT NULL,
    purchase_date DATE,
    cost NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (cost >= 0),
    condition VARCHAR(20) NOT NULL DEFAULT 'Good',
    location VARCHAR(150),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Bank linkage & government schemes -----------------------------------------
CREATE TABLE IF NOT EXISTS shg_scheme_applications (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    application_kind VARCHAR(20) NOT NULL CHECK (application_kind IN ('BANK_LINKAGE', 'BANK_LOAN', 'GOVT_SCHEME')),
    scheme_name VARCHAR(200) NOT NULL,
    authority VARCHAR(200),
    bank_account_id BIGINT REFERENCES shg_bank_accounts(id),
    application_number VARCHAR(80),
    reference_number VARCHAR(80),
    applied_on DATE,
    amount_requested NUMERIC(12, 2),
    amount_sanctioned NUMERIC(12, 2),
    required_documents TEXT,
    reporting_requirements TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'Draft' CHECK (status IN (
        'Draft', 'Submitted', 'Under Verification', 'Approved', 'Rejected', 'Disbursed', 'Closed')),
    verification_status VARCHAR(20) NOT NULL DEFAULT 'Pending'
        CHECK (verification_status IN ('Pending', 'Verified', 'Rejected')),
    remarks TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shg_schemes_group ON shg_scheme_applications(group_id, status);

CREATE TABLE IF NOT EXISTS shg_scheme_utilizations (
    id BIGSERIAL PRIMARY KEY,
    scheme_id BIGINT NOT NULL REFERENCES shg_scheme_applications(id) ON DELETE CASCADE,
    utilization_date DATE NOT NULL,
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    purpose VARCHAR(200) NOT NULL,
    txn_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Accounting ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shg_transactions (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    txn_number VARCHAR(40) NOT NULL,
    receipt_number VARCHAR(40),
    txn_date DATE NOT NULL,
    txn_type VARCHAR(30) NOT NULL,
    classification VARCHAR(30) NOT NULL,
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    principal_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
    interest_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
    penalty_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
    payment_method VARCHAR(20) NOT NULL DEFAULT 'Cash'
        CHECK (payment_method IN ('Cash', 'Bank Transfer', 'UPI', 'Cheque', 'Adjustment')),
    bank_account_id BIGINT REFERENCES shg_bank_accounts(id),
    reference_number VARCHAR(80),
    member_id BIGINT REFERENCES shg_members(id),
    loan_id BIGINT REFERENCES shg_loans(id),
    activity_id BIGINT REFERENCES shg_business_activities(id),
    order_id BIGINT REFERENCES shg_business_orders(id),
    scheme_id BIGINT REFERENCES shg_scheme_applications(id),
    resolution_id BIGINT REFERENCES shg_resolutions(id),
    narration VARCHAR(300),
    approval_status VARCHAR(20) NOT NULL DEFAULT 'Approved'
        CHECK (approval_status IN ('Approved', 'Not Required')),
    posting_status VARCHAR(10) NOT NULL DEFAULT 'Posted' CHECK (posting_status IN ('Posted', 'Reversed')),
    reversal_of_id BIGINT REFERENCES shg_transactions(id),
    reversed_by_id BIGINT REFERENCES shg_transactions(id),
    reversal_reason VARCHAR(300),
    bank_cleared_on DATE,
    idempotency_key VARCHAR(64),
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shg_txn_number UNIQUE (group_id, txn_number)
);
CREATE INDEX IF NOT EXISTS idx_shg_txn_group_date ON shg_transactions(group_id, txn_date);
CREATE INDEX IF NOT EXISTS idx_shg_txn_member ON shg_transactions(member_id);
CREATE INDEX IF NOT EXISTS idx_shg_txn_loan ON shg_transactions(loan_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_shg_txn_reversal ON shg_transactions(reversal_of_id) WHERE reversal_of_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS shg_ledger_lines (
    id BIGSERIAL PRIMARY KEY,
    txn_id BIGINT NOT NULL REFERENCES shg_transactions(id) ON DELETE CASCADE,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    txn_date DATE NOT NULL,
    account_code VARCHAR(30) NOT NULL,
    bank_account_id BIGINT REFERENCES shg_bank_accounts(id),
    member_id BIGINT REFERENCES shg_members(id),
    loan_id BIGINT REFERENCES shg_loans(id),
    activity_id BIGINT REFERENCES shg_business_activities(id),
    debit NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (credit >= 0),
    CONSTRAINT ck_shg_line_one_side CHECK ((debit = 0) <> (credit = 0))
);
CREATE INDEX IF NOT EXISTS idx_shg_lines_group_account ON shg_ledger_lines(group_id, account_code, txn_date);
CREATE INDEX IF NOT EXISTS idx_shg_lines_member ON shg_ledger_lines(member_id, account_code);
CREATE INDEX IF NOT EXISTS idx_shg_lines_loan ON shg_ledger_lines(loan_id);

-- Which installments / dues a posting paid, so a reversal can undo exactly that
CREATE TABLE IF NOT EXISTS shg_allocations (
    id BIGSERIAL PRIMARY KEY,
    txn_id BIGINT NOT NULL REFERENCES shg_transactions(id) ON DELETE CASCADE,
    installment_id BIGINT REFERENCES shg_loan_installments(id),
    savings_due_id BIGINT REFERENCES shg_savings_dues(id),
    principal NUMERIC(12, 2) NOT NULL DEFAULT 0,
    interest NUMERIC(12, 2) NOT NULL DEFAULT 0,
    penalty NUMERIC(12, 2) NOT NULL DEFAULT 0,
    amount NUMERIC(12, 2) NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_shg_allocations_txn ON shg_allocations(txn_id);

CREATE TABLE IF NOT EXISTS shg_period_closes (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    period_end DATE NOT NULL,
    cash_balance NUMERIC(14, 2) NOT NULL,
    bank_balance NUMERIC(14, 2) NOT NULL,
    notes TEXT,
    closed_by BIGINT NOT NULL,
    closed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shg_period_close UNIQUE (group_id, period_end)
);

CREATE TABLE IF NOT EXISTS shg_bank_reconciliations (
    id BIGSERIAL PRIMARY KEY,
    bank_account_id BIGINT NOT NULL REFERENCES shg_bank_accounts(id) ON DELETE CASCADE,
    statement_date DATE NOT NULL,
    statement_balance NUMERIC(14, 2) NOT NULL,
    book_balance NUMERIC(14, 2) NOT NULL,
    uncleared_amount NUMERIC(14, 2) NOT NULL,
    difference NUMERIC(14, 2) NOT NULL,
    notes TEXT,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Idempotency: a retried request (network drop, double tap) returns the stored
-- response instead of posting twice. Rolled back together with a failed request.
CREATE TABLE IF NOT EXISTS shg_request_keys (
    owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    idempotency_key VARCHAR(64) NOT NULL,
    endpoint VARCHAR(120) NOT NULL,
    status_code INT,
    response JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner_id, idempotency_key)
);

-- Per-group number sequences (receipts, transactions, applications, orders)
CREATE TABLE IF NOT EXISTS shg_counters (
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    counter_key VARCHAR(30) NOT NULL,
    value BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, counter_key)
);

-- Documents (stored in the database; only served through authenticated endpoints)
CREATE TABLE IF NOT EXISTS shg_documents (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    entity_type VARCHAR(20) NOT NULL CHECK (entity_type IN ('GROUP', 'MEMBER', 'LOAN', 'SCHEME', 'MEETING', 'TRANSACTION')),
    entity_id BIGINT NOT NULL,
    document_type VARCHAR(60) NOT NULL,
    file_name VARCHAR(200) NOT NULL,
    mime_type VARCHAR(80) NOT NULL,
    size_bytes INT NOT NULL,
    content BYTEA NOT NULL,
    verification_status VARCHAR(20) NOT NULL DEFAULT 'Pending'
        CHECK (verification_status IN ('Pending', 'Verified', 'Rejected')),
    verification_remarks VARCHAR(300),
    uploaded_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shg_documents_entity ON shg_documents(entity_type, entity_id);

-- In-app notifications (deduplicated by dedupe_key)
CREATE TABLE IF NOT EXISTS shg_notifications (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    group_id BIGINT REFERENCES shg_groups(id) ON DELETE CASCADE,
    kind VARCHAR(40) NOT NULL,
    title VARCHAR(200) NOT NULL,
    body VARCHAR(400) NOT NULL,
    entity_type VARCHAR(20),
    entity_id BIGINT,
    dedupe_key VARCHAR(160) NOT NULL,
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shg_notification UNIQUE (owner_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_shg_notifications_owner ON shg_notifications(owner_id, is_read, created_at DESC);

-- Online savings collection (Razorpay) ------------------------------------------
-- The group's own Razorpay account. Secrets are encrypted with DRIVER_KEYS_ENCRYPTION_KEY
-- (the same server key that protects drivers' Razorpay keys).
CREATE TABLE IF NOT EXISTS shg_group_gateway (
    group_id BIGINT PRIMARY KEY REFERENCES shg_groups(id) ON DELETE CASCADE,
    key_id VARCHAR(40) NOT NULL,
    key_secret_enc TEXT NOT NULL,
    webhook_secret_enc TEXT,
    mode VARCHAR(4) NOT NULL CHECK (mode IN ('test', 'live')),
    connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Each member's permanent UPI QR in the group's Razorpay account: whatever she pays on it
-- is her savings.
CREATE TABLE IF NOT EXISTS shg_member_gateway (
    member_id BIGINT PRIMARY KEY REFERENCES shg_members(id) ON DELETE CASCADE,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    qr_id VARCHAR(60) NOT NULL UNIQUE,
    qr_image_url TEXT,
    mode VARCHAR(4) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Payment links sent to a member for a specific amount (usually her current due).
CREATE TABLE IF NOT EXISTS shg_payment_links (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    member_id BIGINT NOT NULL REFERENCES shg_members(id) ON DELETE CASCADE,
    link_id VARCHAR(60) NOT NULL UNIQUE,
    short_url TEXT NOT NULL,
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    mode VARCHAR(4) NOT NULL,
    status VARCHAR(12) NOT NULL DEFAULT 'Open' CHECK (status IN ('Open', 'Paid', 'Cancelled', 'Expired')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shg_payment_links_open ON shg_payment_links(group_id) WHERE status = 'Open';

-- Every payment Razorpay reported for a member, posted once (gateway_payment_id is unique),
-- whether it arrived by webhook or by polling. Unposted = needs the treasurer's attention.
CREATE TABLE IF NOT EXISTS shg_gateway_payments (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES shg_groups(id) ON DELETE CASCADE,
    member_id BIGINT NOT NULL REFERENCES shg_members(id) ON DELETE CASCADE,
    gateway_payment_id VARCHAR(60) NOT NULL UNIQUE,
    source VARCHAR(4) NOT NULL CHECK (source IN ('QR', 'LINK')),
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    fee NUMERIC(12, 2) NOT NULL DEFAULT 0,
    payer_vpa VARCHAR(120) NOT NULL DEFAULT '',
    rrn VARCHAR(40) NOT NULL DEFAULT '',
    paid_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(10) NOT NULL CHECK (status IN ('Posted', 'Unposted')),
    note TEXT NOT NULL DEFAULT '',
    txn_id BIGINT REFERENCES shg_transactions(id),
    fee_txn_id BIGINT REFERENCES shg_transactions(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shg_gateway_payments_group ON shg_gateway_payments(group_id, paid_at DESC);
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

-- Integrity guards: every transaction's lines must balance when the database
-- transaction commits, and posted lines are never edited (corrections are
-- reversals). Deletes stay possible only through ON DELETE CASCADE when a
-- whole tenant/group is removed by the platform admin.
CREATE OR REPLACE FUNCTION shg_check_txn_balanced() RETURNS trigger AS $$
DECLARE diff NUMERIC;
BEGIN
    SELECT COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0) INTO diff
    FROM shg_ledger_lines WHERE txn_id = NEW.txn_id;
    IF diff <> 0 THEN
        RAISE EXCEPTION 'SHG ledger transaction % is unbalanced by %', NEW.txn_id, diff;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION shg_block_line_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Posted SHG ledger lines cannot be modified; post a reversal instead';
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_shg_lines_balanced') THEN
        CREATE CONSTRAINT TRIGGER trg_shg_lines_balanced
            AFTER INSERT ON shg_ledger_lines
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION shg_check_txn_balanced();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_shg_lines_immutable') THEN
        CREATE TRIGGER trg_shg_lines_immutable
            BEFORE UPDATE ON shg_ledger_lines
            FOR EACH ROW EXECUTE FUNCTION shg_block_line_update();
    END IF;
END $$;
"""


def ensure_bachatgat_schema(cursor):
    """Create all Mahila Bachat Gat tables and register the business type."""
    cursor.execute(SCHEMA_SQL)
    cursor.execute(
        "INSERT INTO business_types (name, description) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
        (BUSINESS_TYPE_NAME, BUSINESS_TYPE_DESCRIPTION),
    )
