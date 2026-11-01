-- Food Seller business type. Generated from foodseller_service.SCHEMA_SQL, which the API applies at startup.
-- Additive and idempotent: creates food_* tables, two trigger functions and one business_types row.
-- It does not alter any existing table.

BEGIN;

CREATE TABLE IF NOT EXISTS food_settings (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    allow_negative_stock BOOLEAN NOT NULL DEFAULT FALSE,
    cover_days INT NOT NULL DEFAULT 2 CHECK (cover_days BETWEEN 1 AND 30),
    excess_ratio NUMERIC(6, 2) NOT NULL DEFAULT 1.5 CHECK (excess_ratio >= 1),
    unusual_expense_ratio NUMERIC(6, 2) NOT NULL DEFAULT 2 CHECK (unusual_expense_ratio >= 1),
    starter_choice VARCHAR(20),
    UNIQUE (business_id)
);

CREATE TABLE IF NOT EXISTS food_units (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    name VARCHAR(40) NOT NULL,
    -- Units of one dimension convert through to_base. 'custom' only equals itself.
    dimension VARCHAR(10) NOT NULL DEFAULT 'custom' CHECK (dimension IN ('mass', 'volume', 'count', 'custom')),
    to_base NUMERIC(18, 6) NOT NULL DEFAULT 1 CHECK (to_base > 0),
    UNIQUE (business_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_food_unit_name ON food_units (business_id, lower(name));

CREATE TABLE IF NOT EXISTS food_product_categories (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    name VARCHAR(80) NOT NULL,
    UNIQUE (business_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_food_category_name ON food_product_categories (business_id, lower(name));

CREATE TABLE IF NOT EXISTS food_suppliers (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    name VARCHAR(120) NOT NULL,
    mobile VARCHAR(15),
    address VARCHAR(300),
    status VARCHAR(10) NOT NULL DEFAULT 'Active' CHECK (status IN ('Active', 'Inactive')),
    UNIQUE (business_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_food_supplier_name ON food_suppliers (business_id, lower(name));

CREATE TABLE IF NOT EXISTS food_inventory_products (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    name VARCHAR(120) NOT NULL,
    category_id BIGINT,
    unit_id BIGINT NOT NULL,
    purchase_price NUMERIC(12, 4) NOT NULL DEFAULT 0 CHECK (purchase_price >= 0),
    selling_price NUMERIC(12, 2) CHECK (selling_price >= 0),
    opening_stock NUMERIC(16, 4) NOT NULL DEFAULT 0 CHECK (opening_stock >= 0),
    min_stock_alert NUMERIC(16, 4) NOT NULL DEFAULT 0 CHECK (min_stock_alert >= 0),
    supplier_id BIGINT,
    status VARCHAR(10) NOT NULL DEFAULT 'Active' CHECK (status IN ('Active', 'Inactive')),
    deleted_at TIMESTAMPTZ,
    UNIQUE (business_id, id),
    FOREIGN KEY (business_id, category_id) REFERENCES food_product_categories (business_id, id),
    FOREIGN KEY (business_id, unit_id) REFERENCES food_units (business_id, id),
    FOREIGN KEY (business_id, supplier_id) REFERENCES food_suppliers (business_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_food_inv_product_name ON food_inventory_products (business_id, lower(name)) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS food_inventory_transactions (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    product_id BIGINT NOT NULL,
    txn_type VARCHAR(12) NOT NULL CHECK (txn_type IN
        ('OPENING', 'PURCHASE', 'CONSUMPTION', 'SALE_USAGE', 'ADJUST_IN', 'ADJUST_OUT', 'WASTAGE', 'SPOILAGE', 'PERSONAL')),
    quantity NUMERIC(16, 4) NOT NULL,
    value NUMERIC(16, 4) NOT NULL,
    source_type VARCHAR(20),
    source_id BIGINT,
    note VARCHAR(300),
    FOREIGN KEY (business_id, product_id) REFERENCES food_inventory_products (business_id, id)
);
CREATE INDEX IF NOT EXISTS idx_food_ledger_product ON food_inventory_transactions (business_id, product_id, transaction_date);
CREATE INDEX IF NOT EXISTS idx_food_ledger_date ON food_inventory_transactions (business_id, transaction_date, txn_type);

CREATE TABLE IF NOT EXISTS food_purchase_transactions (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    supplier_id BIGINT,
    invoice_no VARCHAR(60),
    total_amount NUMERIC(14, 2) NOT NULL CHECK (total_amount >= 0),
    amount_paid NUMERIC(14, 2) NOT NULL DEFAULT 0 CHECK (amount_paid >= 0),
    payment_method VARCHAR(10) NOT NULL DEFAULT 'Cash' CHECK (payment_method IN ('Cash', 'UPI', 'Credit')),
    notes VARCHAR(300),
    status VARCHAR(10) NOT NULL DEFAULT 'Posted' CHECK (status IN ('Posted', 'Voided')),
    UNIQUE (business_id, id),
    FOREIGN KEY (business_id, supplier_id) REFERENCES food_suppliers (business_id, id)
);
CREATE INDEX IF NOT EXISTS idx_food_purchase_date ON food_purchase_transactions (business_id, transaction_date);

CREATE TABLE IF NOT EXISTS food_purchase_items (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    purchase_id BIGINT NOT NULL,
    product_id BIGINT NOT NULL,
    entered_qty NUMERIC(16, 4) NOT NULL CHECK (entered_qty > 0),
    unit_id BIGINT NOT NULL,
    rate NUMERIC(12, 4) NOT NULL CHECK (rate >= 0),
    total NUMERIC(14, 2) NOT NULL CHECK (total >= 0),
    stock_qty NUMERIC(16, 4) NOT NULL CHECK (stock_qty > 0),
    FOREIGN KEY (business_id, purchase_id) REFERENCES food_purchase_transactions (business_id, id) ON DELETE CASCADE,
    FOREIGN KEY (business_id, product_id) REFERENCES food_inventory_products (business_id, id),
    FOREIGN KEY (business_id, unit_id) REFERENCES food_units (business_id, id)
);
CREATE INDEX IF NOT EXISTS idx_food_purchase_items ON food_purchase_items (business_id, purchase_id);

CREATE TABLE IF NOT EXISTS food_consumption_transactions (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    notes VARCHAR(300),
    status VARCHAR(10) NOT NULL DEFAULT 'Posted' CHECK (status IN ('Posted', 'Voided')),
    UNIQUE (business_id, id)
);
CREATE INDEX IF NOT EXISTS idx_food_consumption_date ON food_consumption_transactions (business_id, transaction_date);

CREATE TABLE IF NOT EXISTS food_consumption_items (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    consumption_id BIGINT NOT NULL,
    product_id BIGINT NOT NULL,
    entered_qty NUMERIC(16, 4) NOT NULL CHECK (entered_qty > 0),
    unit_id BIGINT NOT NULL,
    stock_qty NUMERIC(16, 4) NOT NULL CHECK (stock_qty > 0),
    purpose VARCHAR(12) NOT NULL DEFAULT 'PREPARATION' CHECK (purpose IN ('PREPARATION', 'WASTAGE', 'SPOILAGE', 'PERSONAL')),
    FOREIGN KEY (business_id, consumption_id) REFERENCES food_consumption_transactions (business_id, id) ON DELETE CASCADE,
    FOREIGN KEY (business_id, product_id) REFERENCES food_inventory_products (business_id, id),
    FOREIGN KEY (business_id, unit_id) REFERENCES food_units (business_id, id)
);
CREATE INDEX IF NOT EXISTS idx_food_consumption_items ON food_consumption_items (business_id, product_id, transaction_date);

CREATE TABLE IF NOT EXISTS food_products (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    name VARCHAR(120) NOT NULL,
    category_id BIGINT,
    product_type VARCHAR(10) NOT NULL DEFAULT 'RECIPE' CHECK (product_type IN ('RECIPE', 'PACKAGED')),
    selling_price NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (selling_price >= 0),
    -- PACKAGED: selling one unit takes packaged_qty of this stock item directly.
    packaged_product_id BIGINT,
    packaged_qty NUMERIC(16, 4) NOT NULL DEFAULT 1 CHECK (packaged_qty > 0),
    status VARCHAR(10) NOT NULL DEFAULT 'Active' CHECK (status IN ('Active', 'Inactive')),
    deleted_at TIMESTAMPTZ,
    UNIQUE (business_id, id),
    FOREIGN KEY (business_id, category_id) REFERENCES food_product_categories (business_id, id),
    FOREIGN KEY (business_id, packaged_product_id) REFERENCES food_inventory_products (business_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_food_product_name ON food_products (business_id, lower(name)) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS food_recipes (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    food_product_id BIGINT NOT NULL,
    yield_qty NUMERIC(12, 4) NOT NULL DEFAULT 1 CHECK (yield_qty > 0),
    notes VARCHAR(300),
    UNIQUE (business_id, id),
    UNIQUE (food_product_id),
    FOREIGN KEY (business_id, food_product_id) REFERENCES food_products (business_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS food_recipe_ingredients (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    recipe_id BIGINT NOT NULL,
    product_id BIGINT NOT NULL,
    quantity NUMERIC(16, 4) NOT NULL CHECK (quantity > 0),
    unit_id BIGINT NOT NULL,
    UNIQUE (recipe_id, product_id),
    FOREIGN KEY (business_id, recipe_id) REFERENCES food_recipes (business_id, id) ON DELETE CASCADE,
    FOREIGN KEY (business_id, product_id) REFERENCES food_inventory_products (business_id, id),
    FOREIGN KEY (business_id, unit_id) REFERENCES food_units (business_id, id)
);

CREATE TABLE IF NOT EXISTS food_sales_transactions (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    customer_name VARCHAR(120),
    subtotal NUMERIC(14, 2) NOT NULL CHECK (subtotal >= 0),
    discount NUMERIC(14, 2) NOT NULL DEFAULT 0 CHECK (discount >= 0),
    total_amount NUMERIC(14, 2) NOT NULL CHECK (total_amount >= 0),
    cash_amount NUMERIC(14, 2) NOT NULL DEFAULT 0 CHECK (cash_amount >= 0),
    upi_amount NUMERIC(14, 2) NOT NULL DEFAULT 0 CHECK (upi_amount >= 0),
    credit_amount NUMERIC(14, 2) NOT NULL DEFAULT 0 CHECK (credit_amount >= 0),
    notes VARCHAR(300),
    status VARCHAR(10) NOT NULL DEFAULT 'Posted' CHECK (status IN ('Posted', 'Voided')),
    UNIQUE (business_id, id)
);
CREATE INDEX IF NOT EXISTS idx_food_sales_date ON food_sales_transactions (business_id, transaction_date);

CREATE TABLE IF NOT EXISTS food_sales_items (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    sale_id BIGINT NOT NULL,
    food_product_id BIGINT,
    food_name VARCHAR(120) NOT NULL,
    quantity NUMERIC(12, 4) NOT NULL CHECK (quantity > 0),
    price NUMERIC(12, 2) NOT NULL CHECK (price >= 0),
    total NUMERIC(14, 2) NOT NULL CHECK (total >= 0),
    discount_share NUMERIC(14, 2) NOT NULL DEFAULT 0 CHECK (discount_share >= 0),
    has_cost_source BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (business_id, id),
    FOREIGN KEY (business_id, sale_id) REFERENCES food_sales_transactions (business_id, id) ON DELETE CASCADE,
    FOREIGN KEY (business_id, food_product_id) REFERENCES food_products (business_id, id)
);
CREATE INDEX IF NOT EXISTS idx_food_sales_items ON food_sales_items (business_id, sale_id);

-- What each sold line needed from stock, frozen when sold, so a later recipe edit never rewrites history.
CREATE TABLE IF NOT EXISTS food_sales_item_usage (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    sale_id BIGINT NOT NULL,
    sale_item_id BIGINT NOT NULL,
    product_id BIGINT NOT NULL,
    required_qty NUMERIC(16, 4) NOT NULL CHECK (required_qty > 0),
    FOREIGN KEY (business_id, sale_id) REFERENCES food_sales_transactions (business_id, id) ON DELETE CASCADE,
    FOREIGN KEY (business_id, sale_item_id) REFERENCES food_sales_items (business_id, id) ON DELETE CASCADE,
    FOREIGN KEY (business_id, product_id) REFERENCES food_inventory_products (business_id, id)
);
CREATE INDEX IF NOT EXISTS idx_food_usage_product ON food_sales_item_usage (business_id, product_id, transaction_date);

CREATE TABLE IF NOT EXISTS food_expense_categories (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    name VARCHAR(80) NOT NULL,
    -- FALSE: this cost already reaches profit through stock consumption (gas, packaging).
    counts_in_pnl BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (business_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_food_expense_category_name ON food_expense_categories (business_id, lower(name));

CREATE TABLE IF NOT EXISTS food_expense_transactions (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    category_id BIGINT NOT NULL,
    amount NUMERIC(14, 2) NOT NULL CHECK (amount > 0),
    payment_method VARCHAR(10) NOT NULL DEFAULT 'Cash' CHECK (payment_method IN ('Cash', 'UPI', 'Credit')),
    counts_in_pnl BOOLEAN NOT NULL DEFAULT TRUE,
    notes VARCHAR(300),
    status VARCHAR(10) NOT NULL DEFAULT 'Posted' CHECK (status IN ('Posted', 'Voided')),
    UNIQUE (business_id, id),
    FOREIGN KEY (business_id, category_id) REFERENCES food_expense_categories (business_id, id)
);
CREATE INDEX IF NOT EXISTS idx_food_expense_date ON food_expense_transactions (business_id, transaction_date);

CREATE TABLE IF NOT EXISTS food_stock_adjustments (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    product_id BIGINT NOT NULL,
    direction VARCHAR(3) NOT NULL CHECK (direction IN ('IN', 'OUT')),
    entered_qty NUMERIC(16, 4) NOT NULL CHECK (entered_qty > 0),
    unit_id BIGINT NOT NULL,
    stock_qty NUMERIC(16, 4) NOT NULL CHECK (stock_qty > 0),
    reason VARCHAR(300) NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'Posted' CHECK (status IN ('Posted', 'Voided')),
    UNIQUE (business_id, id),
    FOREIGN KEY (business_id, product_id) REFERENCES food_inventory_products (business_id, id),
    FOREIGN KEY (business_id, unit_id) REFERENCES food_units (business_id, id)
);

CREATE TABLE IF NOT EXISTS food_payments (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    direction VARCHAR(10) NOT NULL CHECK (direction IN ('RECEIVED', 'PAID')),
    sale_id BIGINT,
    purchase_id BIGINT,
    amount NUMERIC(14, 2) NOT NULL CHECK (amount > 0),
    payment_method VARCHAR(10) NOT NULL CHECK (payment_method IN ('Cash', 'UPI')),
    notes VARCHAR(300),
    status VARCHAR(10) NOT NULL DEFAULT 'Posted' CHECK (status IN ('Posted', 'Voided')),
    CHECK ((direction = 'RECEIVED' AND sale_id IS NOT NULL AND purchase_id IS NULL)
        OR (direction = 'PAID' AND purchase_id IS NOT NULL AND sale_id IS NULL)),
    FOREIGN KEY (business_id, sale_id) REFERENCES food_sales_transactions (business_id, id),
    FOREIGN KEY (business_id, purchase_id) REFERENCES food_purchase_transactions (business_id, id)
);
CREATE INDEX IF NOT EXISTS idx_food_payments_date ON food_payments (business_id, transaction_date);

CREATE TABLE IF NOT EXISTS food_notifications (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    dedupe_key VARCHAR(120) NOT NULL,
    type VARCHAR(30) NOT NULL,
    severity VARCHAR(10) NOT NULL DEFAULT 'info' CHECK (severity IN ('info', 'warning', 'critical')),
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    message VARCHAR(400) NOT NULL,
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (business_id, dedupe_key)
);

-- Everything the AI assistant wants to change waits here until the owner confirms it.
CREATE TABLE IF NOT EXISTS food_ai_proposals (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    action_type VARCHAR(30) NOT NULL CHECK (action_type IN ('CREATE_PURCHASE', 'STOCK_ADJUSTMENT', 'RECORD_EXPENSE')),
    payload JSONB NOT NULL,
    summary VARCHAR(1000) NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Confirmed', 'Rejected', 'Expired')),
    result JSONB,
    decided_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS food_request_keys (
    id BIGSERIAL PRIMARY KEY,
    
    business_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
,
    idempotency_key VARCHAR(64) NOT NULL,
    endpoint VARCHAR(120) NOT NULL,
    status_code INT,
    response JSONB,
    UNIQUE (business_id, idempotency_key)
);

CREATE OR REPLACE FUNCTION food_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION food_ledger_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'food_inventory_transactions rows cannot be changed; post a correcting row';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_food_ledger_append_only ON food_inventory_transactions;
CREATE TRIGGER trg_food_ledger_append_only BEFORE UPDATE ON food_inventory_transactions
    FOR EACH ROW EXECUTE FUNCTION food_ledger_is_append_only();

DROP TRIGGER IF EXISTS trg_touch_food_settings ON food_settings;
CREATE TRIGGER trg_touch_food_settings BEFORE UPDATE ON food_settings
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_units ON food_units;
CREATE TRIGGER trg_touch_food_units BEFORE UPDATE ON food_units
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_product_categories ON food_product_categories;
CREATE TRIGGER trg_touch_food_product_categories BEFORE UPDATE ON food_product_categories
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_suppliers ON food_suppliers;
CREATE TRIGGER trg_touch_food_suppliers BEFORE UPDATE ON food_suppliers
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_inventory_products ON food_inventory_products;
CREATE TRIGGER trg_touch_food_inventory_products BEFORE UPDATE ON food_inventory_products
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_purchase_transactions ON food_purchase_transactions;
CREATE TRIGGER trg_touch_food_purchase_transactions BEFORE UPDATE ON food_purchase_transactions
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_purchase_items ON food_purchase_items;
CREATE TRIGGER trg_touch_food_purchase_items BEFORE UPDATE ON food_purchase_items
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_consumption_transactions ON food_consumption_transactions;
CREATE TRIGGER trg_touch_food_consumption_transactions BEFORE UPDATE ON food_consumption_transactions
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_consumption_items ON food_consumption_items;
CREATE TRIGGER trg_touch_food_consumption_items BEFORE UPDATE ON food_consumption_items
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_products ON food_products;
CREATE TRIGGER trg_touch_food_products BEFORE UPDATE ON food_products
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_recipes ON food_recipes;
CREATE TRIGGER trg_touch_food_recipes BEFORE UPDATE ON food_recipes
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_recipe_ingredients ON food_recipe_ingredients;
CREATE TRIGGER trg_touch_food_recipe_ingredients BEFORE UPDATE ON food_recipe_ingredients
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_sales_transactions ON food_sales_transactions;
CREATE TRIGGER trg_touch_food_sales_transactions BEFORE UPDATE ON food_sales_transactions
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_sales_items ON food_sales_items;
CREATE TRIGGER trg_touch_food_sales_items BEFORE UPDATE ON food_sales_items
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_sales_item_usage ON food_sales_item_usage;
CREATE TRIGGER trg_touch_food_sales_item_usage BEFORE UPDATE ON food_sales_item_usage
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_expense_categories ON food_expense_categories;
CREATE TRIGGER trg_touch_food_expense_categories BEFORE UPDATE ON food_expense_categories
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_expense_transactions ON food_expense_transactions;
CREATE TRIGGER trg_touch_food_expense_transactions BEFORE UPDATE ON food_expense_transactions
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_stock_adjustments ON food_stock_adjustments;
CREATE TRIGGER trg_touch_food_stock_adjustments BEFORE UPDATE ON food_stock_adjustments
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_payments ON food_payments;
CREATE TRIGGER trg_touch_food_payments BEFORE UPDATE ON food_payments
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_notifications ON food_notifications;
CREATE TRIGGER trg_touch_food_notifications BEFORE UPDATE ON food_notifications
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_ai_proposals ON food_ai_proposals;
CREATE TRIGGER trg_touch_food_ai_proposals BEFORE UPDATE ON food_ai_proposals
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_food_request_keys ON food_request_keys;
CREATE TRIGGER trg_touch_food_request_keys BEFORE UPDATE ON food_request_keys
    FOR EACH ROW EXECUTE FUNCTION food_touch_updated_at();

INSERT INTO business_types (name, description) VALUES ('Food Seller', 'Vada pav, tea and snack sellers, fast-food stalls, street food vendors and small restaurants')
    ON CONFLICT (name) DO NOTHING;

COMMIT;
