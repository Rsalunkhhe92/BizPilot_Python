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
            cart_id VARCHAR(100),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS transaction_type VARCHAR(10) NOT NULL DEFAULT 'SALE';
        ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS payment_method VARCHAR(20) NOT NULL DEFAULT 'CASH';
        ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS cart_id VARCHAR(100);

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
        -- (name, mobile, email) before any purchase from them exists. Also doubles
        -- as the "archived" marker for vendors removed from the Vendors list -
        -- their purchase/payment history is never deleted, only hidden.
        CREATE TABLE IF NOT EXISTS inventory_vendors (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            name VARCHAR(150) NOT NULL,
            mobile_number VARCHAR(20) DEFAULT '',
            email VARCHAR(150) DEFAULT '',
            address TEXT DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE inventory_vendors ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active';
        ALTER TABLE inventory_vendors DROP CONSTRAINT IF EXISTS inventory_vendors_status_check;
        ALTER TABLE inventory_vendors ADD CONSTRAINT inventory_vendors_status_check CHECK (status IN ('active', 'archived'));

        -- 5. Customer credit ledger (Udhaar): the receivables mirror of the vendor
        -- payable system above. inventory_customer_payments logs money collected
        -- from a customer later, independent of any single credit sale.
        CREATE TABLE IF NOT EXISTS inventory_customer_payments (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            customer_name VARCHAR(150) NOT NULL,
            amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
            payment_method VARCHAR(20) NOT NULL DEFAULT 'CASH',
            payment_date DATE NOT NULL DEFAULT CURRENT_DATE,
            note TEXT DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        -- 6. Customer contact profiles - same role as inventory_vendors: lets an
        -- owner add a regular customer proactively, and doubles as the "archived"
        -- marker when removed from the Customers list (history is never deleted).
        CREATE TABLE IF NOT EXISTS inventory_customers (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            name VARCHAR(150) NOT NULL,
            mobile_number VARCHAR(20) DEFAULT '',
            email VARCHAR(150) DEFAULT '',
            address TEXT DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        -- 7. Daily business expenses not tied to a specific product (rent, transport,
        -- labor) - feeds into a true Net Profit alongside sales/purchases/wastage.
        CREATE TABLE IF NOT EXISTS inventory_expenses (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            category VARCHAR(100) NOT NULL DEFAULT 'General',
            amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
            payment_method VARCHAR(20) NOT NULL DEFAULT 'CASH',
            expense_date DATE NOT NULL DEFAULT CURRENT_DATE,
            note TEXT DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        -- transaction_id: set when a follow-up payment was made against one specific
        -- purchase / credit sale ("Pay This"), so the ledger can mark that exact
        -- entry as settled. NULL for general payments against the overall balance.
        ALTER TABLE inventory_vendor_payments ADD COLUMN IF NOT EXISTS transaction_id BIGINT;
        ALTER TABLE inventory_customer_payments ADD COLUMN IF NOT EXISTS transaction_id BIGINT;

        -- cost_amount: what the goods in a SALE cost the owner (quantity x cost price
        -- at the time), so profit is measured against goods sold, not goods bought.
        -- NULL on older sales, which fall back to the product's current cost price.
        ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS cost_amount NUMERIC(12, 2);

        -- 8. Daily selling rates: one row per product per day the owner set or
        -- confirmed its price, so the app knows whether today's rates are done and
        -- what the last rate was. Sales keep their own amount, so this is history
        -- for the owner, not something profit is calculated from.
        CREATE TABLE IF NOT EXISTS inventory_price_history (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            product_id BIGINT NOT NULL REFERENCES inventory_products(id) ON DELETE CASCADE,
            price_date DATE NOT NULL DEFAULT CURRENT_DATE,
            selling_price NUMERIC(12, 2) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (product_id, price_date)
        );

        CREATE INDEX IF NOT EXISTS idx_inventory_products_owner ON inventory_products(owner_id);
        CREATE INDEX IF NOT EXISTS idx_inventory_tx_owner_date ON inventory_transactions(owner_id, transaction_date);
        CREATE INDEX IF NOT EXISTS idx_inventory_tx_product ON inventory_transactions(product_id);
        CREATE INDEX IF NOT EXISTS idx_inventory_vendor_payments_owner ON inventory_vendor_payments(owner_id, vendor_name);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_vendors_owner_name ON inventory_vendors(owner_id, lower(name));
        CREATE INDEX IF NOT EXISTS idx_inventory_customer_payments_owner ON inventory_customer_payments(owner_id, customer_name);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_customers_owner_name ON inventory_customers(owner_id, lower(name));
        CREATE INDEX IF NOT EXISTS idx_inventory_expenses_owner_date ON inventory_expenses(owner_id, expense_date);
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
