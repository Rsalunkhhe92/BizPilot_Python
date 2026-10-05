"""
Inventory Business Service Module (Fruit Sellers / Product-based businesses)
Handles schema provisioning for product catalog + daily product transactions.
"""


def ensure_inventory_schema(cursor):
    """Create all required inventory tables if they don't exist."""
    cursor.execute("""
        -- 1. Product Catalog (per business owner)
        CREATE TABLE IF NOT EXISTS inventory_products (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            name VARCHAR(150) NOT NULL,
            category VARCHAR(100) NOT NULL DEFAULT 'General',
            unit VARCHAR(30) NOT NULL DEFAULT 'kg',
            stock_qty NUMERIC(12, 2) NOT NULL DEFAULT 0,
            selling_price NUMERIC(12, 2) NOT NULL DEFAULT 0,
            cost_price NUMERIC(12, 2) NOT NULL DEFAULT 0,
            low_stock_threshold NUMERIC(12, 2) NOT NULL DEFAULT 0,
            status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        -- 2. Daily Product Transactions (sales recorded against a product)
        CREATE TABLE IF NOT EXISTS inventory_transactions (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            product_id BIGINT REFERENCES inventory_products(id) ON DELETE SET NULL,
            product_name VARCHAR(150) NOT NULL,
            quantity NUMERIC(12, 2) NOT NULL DEFAULT 0,
            unit VARCHAR(30) NOT NULL DEFAULT '',
            amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
            transaction_type VARCHAR(10) NOT NULL DEFAULT 'SALE' CHECK (transaction_type IN ('SALE', 'WASTAGE', 'PURCHASE')),
            payment_method VARCHAR(20) NOT NULL DEFAULT 'CASH',
            transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
            note TEXT DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS transaction_type VARCHAR(10) NOT NULL DEFAULT 'SALE';
        ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS payment_method VARCHAR(20) NOT NULL DEFAULT 'CASH';

        -- amount_paid: how much was actually paid at the time of a PURCHASE (vs the
        -- total cost in `amount`). Lets a purchase be partly on credit to a vendor.
        -- Existing rows predate this feature, so backfill them as fully paid.
        ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS amount_paid NUMERIC(12, 2) NOT NULL DEFAULT 0;

        -- 3. Vendor ledger: follow-up payments made against a vendor's outstanding
        -- due balance, independent of any single purchase line item.
        CREATE TABLE IF NOT EXISTS inventory_vendor_payments (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            vendor_name VARCHAR(150) NOT NULL,
            amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
            payment_method VARCHAR(20) NOT NULL DEFAULT 'CASH',
            payment_date DATE NOT NULL DEFAULT CURRENT_DATE,
            note TEXT DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        -- 4. Vendor contact profiles - lets an owner add a vendor proactively
        -- (name, mobile, email) before any purchase from them exists.
        CREATE TABLE IF NOT EXISTS inventory_vendors (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            name VARCHAR(150) NOT NULL,
            mobile_number VARCHAR(20) DEFAULT '',
            email VARCHAR(150) DEFAULT '',
            address TEXT DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_inventory_products_owner ON inventory_products(owner_id);
        CREATE INDEX IF NOT EXISTS idx_inventory_tx_owner_date ON inventory_transactions(owner_id, transaction_date);
        CREATE INDEX IF NOT EXISTS idx_inventory_tx_product ON inventory_transactions(product_id);
        CREATE INDEX IF NOT EXISTS idx_inventory_vendor_payments_owner ON inventory_vendor_payments(owner_id, vendor_name);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_vendors_owner_name ON inventory_vendors(owner_id, lower(name));
    """)
    # One-time backfill for rows created before the amount_paid column existed -
    # treat them as fully paid (the only behavior that existed back then). Scoped
    # to a fixed cutoff so it never touches genuinely-unpaid rows created after
    # this feature shipped, even on repeated server restarts.
    cursor.execute("""
        UPDATE inventory_transactions
        SET amount_paid = amount
        WHERE amount_paid = 0 AND amount > 0 AND created_at < '2026-10-05'::timestamptz
    """)
