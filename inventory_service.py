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

        CREATE INDEX IF NOT EXISTS idx_inventory_products_owner ON inventory_products(owner_id);
        CREATE INDEX IF NOT EXISTS idx_inventory_tx_owner_date ON inventory_transactions(owner_id, transaction_date);
        CREATE INDEX IF NOT EXISTS idx_inventory_tx_product ON inventory_transactions(product_id);
    """)
