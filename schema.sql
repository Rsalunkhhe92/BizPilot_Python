CREATE TABLE IF NOT EXISTS userdetails (
    id BIGSERIAL PRIMARY KEY,
    full_name VARCHAR(120) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    dob DATE NOT NULL,
    business_type VARCHAR(100) NOT NULL DEFAULT 'Other local businesses',
    active_plan VARCHAR(20) NOT NULL DEFAULT 'monthly',
    user_type VARCHAR(20) NOT NULL CHECK (user_type IN ('admin', 'customer')),
    status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'suspended')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id BIGSERIAL PRIMARY KEY,
    plan_name VARCHAR(100) NOT NULL,
    billing_cycle VARCHAR(20) NOT NULL DEFAULT 'monthly' CHECK (billing_cycle IN ('monthly', 'quarterly', 'yearly')),
    plan_amount INT NOT NULL DEFAULT 0,
    status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'cancelled')),
    start_date DATE NOT NULL DEFAULT CURRENT_DATE,
    end_date DATE NOT NULL,
    description VARCHAR(255) NOT NULL DEFAULT '',
    features JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_popular BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS subscription_plans (
    id BIGSERIAL PRIMARY KEY,
    plan_name VARCHAR(50) NOT NULL UNIQUE,
    monthly_amount INT NOT NULL,
    annual_amount INT NOT NULL,
    description VARCHAR(255) NOT NULL DEFAULT '',
    features JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_popular BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS business_types (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL UNIQUE,
    description VARCHAR(500) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE userdetails ADD COLUMN IF NOT EXISTS business_type VARCHAR(100);
UPDATE userdetails SET business_type = 'Other local businesses' WHERE business_type IS NULL;
ALTER TABLE userdetails ALTER COLUMN business_type SET NOT NULL;

ALTER TABLE userdetails ADD COLUMN IF NOT EXISTS active_plan VARCHAR(20);
UPDATE userdetails u
SET active_plan = COALESCE(
    (
        SELECT s.plan_name
        FROM subscriptions s
        WHERE s.plan_name = u.active_plan
        ORDER BY s.start_date DESC, s.id DESC
        LIMIT 1
    ),
    'monthly'
)
WHERE u.active_plan IS NULL;
ALTER TABLE userdetails ALTER COLUMN active_plan SET DEFAULT 'monthly';
ALTER TABLE userdetails ALTER COLUMN active_plan SET NOT NULL;

ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS plan_amount INT;
UPDATE subscriptions SET plan_amount = 0 WHERE plan_amount IS NULL;
ALTER TABLE subscriptions ALTER COLUMN plan_amount SET NOT NULL;

ALTER TABLE userdetails ADD COLUMN IF NOT EXISTS enabled_features JSONB DEFAULT '[]'::jsonb;

INSERT INTO userdetails (full_name, email, dob,user_type)
VALUES ('Prem N', 'prem@gmail.com', '1992-08-01','customer')
ON CONFLICT (email) DO NOTHING;

INSERT INTO business_types (name) VALUES
    ('Auto-rickshaw drivers'),
    ('Vegetable sellers'),
    ('Fruit sellers'),
    ('Mechanics'),
    ('Small retailers'),
    ('Collection'),
    ('Other local businesses')
ON CONFLICT (name) DO NOTHING;

INSERT INTO subscriptions (plan_name, plan_amount, status, start_date, end_date)
SELECT plans.plan_name, plans.plan_amount, 'active', CURRENT_DATE, (CURRENT_DATE + INTERVAL '30 days')::date
FROM (VALUES
        ('monthly', 499),
        ('quarterly', 1299),
        ('yearly', 4999)
) AS plans(plan_name, plan_amount)
ON CONFLICT (plan_name) DO UPDATE SET
    plan_amount = EXCLUDED.plan_amount;

INSERT INTO subscription_plans (plan_name, monthly_amount, annual_amount, description, features, is_popular) VALUES
    ('Free', 49, 39, 'For individuals getting started', '["Up to 5 customers", "Basic customer management", "Email support", "Basic reports"]', FALSE),
    ('Small Business', 99, 79, 'For growing businesses', '["Up to 50 customers", "Advanced customer management", "Priority support", "Advanced reports", "Analytics dashboard"]', FALSE),
    ('Professional', 219, 175, 'For growing businesses', '["Up to 100 customers", "Advanced customer management", "24/7 priority support", "Advanced reports", "Analytics dashboard"]', TRUE),
    ('Enterprise', 419, 335, 'For large organizations', '["Unlimited customers", "Complete customer management", "24/7 priority support", "Advanced analytics", "Custom reports"]', FALSE),
    ('Growth', 299, 239, 'For expanding teams', '["Up to 250 customers", "Team management tools", "Priority support", "Growth reports", "Advanced analytics"]', FALSE),
    ('Scale', 599, 479, 'For high-volume operations', '["Unlimited customers", "Multi-team management", "Dedicated support", "Custom analytics", "Custom reports"]', FALSE)
ON CONFLICT (plan_name) DO UPDATE SET
    monthly_amount = EXCLUDED.monthly_amount,
    annual_amount = EXCLUDED.annual_amount,
    description = EXCLUDED.description,
    features = EXCLUDED.features,
    is_popular = EXCLUDED.is_popular,
    updated_at = NOW();

CREATE TABLE IF NOT EXISTS customer_logs (
    id BIGSERIAL PRIMARY KEY,
    customer_id BIGINT,
    customer_name VARCHAR(150),
    action VARCHAR(50) NOT NULL,
    performed_by VARCHAR(150) NOT NULL DEFAULT 'Admin',
    details TEXT NOT NULL,
    old_values JSONB DEFAULT '{}'::jsonb,
    new_values JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS subscription_logs (
    id BIGSERIAL PRIMARY KEY,
    subscription_id BIGINT,
    plan_name VARCHAR(100),
    action VARCHAR(50) NOT NULL,
    performed_by VARCHAR(150) NOT NULL DEFAULT 'Admin',
    details TEXT NOT NULL,
    old_values JSONB DEFAULT '{}'::jsonb,
    new_values JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS business_type_logs (
    id BIGSERIAL PRIMARY KEY,
    business_type_id BIGINT,
    name VARCHAR(120),
    action VARCHAR(50) NOT NULL,
    performed_by VARCHAR(150) NOT NULL DEFAULT 'Admin',
    details TEXT NOT NULL,
    old_values JSONB DEFAULT '{}'::jsonb,
    new_values JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_notifications (
    id BIGSERIAL PRIMARY KEY,
    category VARCHAR(50) NOT NULL, -- 'Customer' or 'Application'
    type VARCHAR(50) NOT NULL,     -- 'critical', 'warning', 'info', 'issue'
    title VARCHAR(200) NOT NULL,
    message TEXT NOT NULL,
    customer_id BIGINT,
    customer_name VARCHAR(150),
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    is_resolved BOOLEAN NOT NULL DEFAULT FALSE,
    action_url VARCHAR(100),       -- 'Customer Management', 'Subscription Plans', 'Audit Logs'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =========================================================
-- COLLECTION BUSINESS TYPE MODULE TABLES
-- =========================================================

CREATE TABLE IF NOT EXISTS collection_routes (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL UNIQUE,
    area VARCHAR(150) NOT NULL,
    code VARCHAR(50) NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS collection_customers (
    id BIGSERIAL PRIMARY KEY,
    collector_id BIGINT REFERENCES userdetails(id) ON DELETE SET NULL,
    route_id BIGINT REFERENCES collection_routes(id) ON DELETE SET NULL,
    account_number VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(150) NOT NULL,
    mobile VARCHAR(20) NOT NULL,
    address TEXT NOT NULL,
    area VARCHAR(100),
    status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS due_amount (
    id BIGSERIAL PRIMARY KEY,
    customer_id BIGINT NOT NULL UNIQUE REFERENCES collection_customers(id) ON DELETE CASCADE,
    total_due NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    expected_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    emi_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    last_payment_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS collection_schedule (
    id BIGSERIAL PRIMARY KEY,
    schedule_date DATE NOT NULL,
    collector_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    customer_id BIGINT NOT NULL REFERENCES collection_customers(id) ON DELETE CASCADE,
    route_id BIGINT REFERENCES collection_routes(id) ON DELETE SET NULL,
    route_order INT NOT NULL DEFAULT 1,
    expected_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    collected_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    status VARCHAR(30) NOT NULL DEFAULT 'PENDING' CHECK (status IN (
        'COLLECTED', 'PENDING', 'MISSED', 'NOT_AVAILABLE', 'REFUSED', 'RESCHEDULED', 'PARTIAL_PAYMENT'
    )),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_schedule_collector_customer_date UNIQUE (schedule_date, collector_id, customer_id)
);

CREATE TABLE IF NOT EXISTS collection_records (
    id BIGSERIAL PRIMARY KEY,
    schedule_id BIGINT REFERENCES collection_schedule(id) ON DELETE SET NULL,
    customer_id BIGINT NOT NULL REFERENCES collection_customers(id) ON DELETE CASCADE,
    collector_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    visit_date DATE NOT NULL DEFAULT CURRENT_DATE,
    status VARCHAR(30) NOT NULL CHECK (status IN (
        'COLLECTED', 'PENDING', 'MISSED', 'NOT_AVAILABLE', 'REFUSED', 'RESCHEDULED', 'PARTIAL_PAYMENT'
    )),
    reason TEXT,
    notes TEXT,
    next_followup_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_collection (
    id BIGSERIAL PRIMARY KEY,
    collection_record_id BIGINT REFERENCES collection_records(id) ON DELETE SET NULL,
    schedule_id BIGINT REFERENCES collection_schedule(id) ON DELETE SET NULL,
    customer_id BIGINT NOT NULL REFERENCES collection_customers(id) ON DELETE CASCADE,
    collector_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    payment_method VARCHAR(30) NOT NULL CHECK (payment_method IN ('CASH', 'UPI', 'BANK_TRANSFER')),
    transaction_ref VARCHAR(100),
    entry_type VARCHAR(20) NOT NULL DEFAULT 'IN' CHECK (entry_type IN ('IN', 'OUT')),
    notes TEXT DEFAULT '',
    status VARCHAR(30) NOT NULL DEFAULT 'SUCCESS' CHECK (status IN ('SUCCESS', 'PENDING', 'FAILED', 'REVERSED')),
    payment_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_receipts (
    id BIGSERIAL PRIMARY KEY,
    payment_id BIGINT NOT NULL UNIQUE REFERENCES payment_collection(id) ON DELETE CASCADE,
    receipt_number VARCHAR(50) NOT NULL UNIQUE,
    customer_id BIGINT NOT NULL REFERENCES collection_customers(id) ON DELETE CASCADE,
    collector_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    payment_method VARCHAR(30) NOT NULL CHECK (payment_method IN ('CASH', 'UPI', 'BANK_TRANSFER')),
    transaction_ref VARCHAR(100),
    receipt_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS collection_followups (
    id BIGSERIAL PRIMARY KEY,
    schedule_id BIGINT REFERENCES collection_schedule(id) ON DELETE SET NULL,
    customer_id BIGINT NOT NULL REFERENCES collection_customers(id) ON DELETE CASCADE,
    collector_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    notes TEXT,
    followup_date DATE NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'RESOLVED', 'RESCHEDULED', 'CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_collection_summary (
    id BIGSERIAL PRIMARY KEY,
    summary_date DATE NOT NULL,
    collector_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    total_assigned INT NOT NULL DEFAULT 0,
    total_collected INT NOT NULL DEFAULT 0,
    total_pending INT NOT NULL DEFAULT 0,
    total_missed INT NOT NULL DEFAULT 0,
    total_not_available INT NOT NULL DEFAULT 0,
    total_refused INT NOT NULL DEFAULT 0,
    total_rescheduled INT NOT NULL DEFAULT 0,
    total_partial INT NOT NULL DEFAULT 0,
    total_expected_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    total_collected_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    total_remaining_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    cash_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    upi_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    bank_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_daily_summary_date_collector UNIQUE (summary_date, collector_id)
);

CREATE TABLE IF NOT EXISTS collection_daily_closing (
    id BIGSERIAL PRIMARY KEY,
    closing_date DATE NOT NULL,
    collector_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    total_customers INT NOT NULL DEFAULT 0,
    collected_count INT NOT NULL DEFAULT 0,
    pending_count INT NOT NULL DEFAULT 0,
    missed_count INT NOT NULL DEFAULT 0,
    expected_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    collected_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    remaining_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    cash_collected NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    upi_collected NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    bank_collected NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    handover_notes TEXT,
    is_closed BOOLEAN NOT NULL DEFAULT TRUE,
    closed_by BIGINT REFERENCES userdetails(id),
    closed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reopened_by BIGINT REFERENCES userdetails(id),
    reopened_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_closing_date_collector UNIQUE (closing_date, collector_id)
);

CREATE TABLE IF NOT EXISTS cash_handover (
    id BIGSERIAL PRIMARY KEY,
    closing_id BIGINT REFERENCES collection_daily_closing(id) ON DELETE CASCADE,
    collector_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    received_by BIGINT REFERENCES userdetails(id),
    cash_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    handover_status VARCHAR(30) NOT NULL DEFAULT 'RECEIVED' CHECK (handover_status IN ('PENDING', 'RECEIVED', 'RECONCILED')),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);



