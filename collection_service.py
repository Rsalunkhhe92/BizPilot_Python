"""
Collection Business Service Module
Handles schema provisioning, demo seed generation (50 customers on 2026-09-07),
and database service operations for field collection.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
import psycopg


def ensure_collection_schema(cursor):
    """Create all required collection tables, indexes, and constraints if they don't exist."""
    cursor.execute("""
        -- 1. Routes
        CREATE TABLE IF NOT EXISTS collection_routes (
            id BIGSERIAL PRIMARY KEY,
            name VARCHAR(120) NOT NULL UNIQUE,
            area VARCHAR(150) NOT NULL,
            code VARCHAR(50) NOT NULL UNIQUE,
            description TEXT DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        -- 2. Collection Customers (Borrowers / Debtors / Clients)
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

        -- 3. Customer Due Amounts (Balances, EMIs)
        CREATE TABLE IF NOT EXISTS due_amount (
            id BIGSERIAL PRIMARY KEY,
            customer_id BIGINT NOT NULL UNIQUE REFERENCES collection_customers(id) ON DELETE CASCADE,
            total_due NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
            expected_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
            emi_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
            interest_rate NUMERIC(6, 2) NOT NULL DEFAULT 0.00,
            last_payment_date DATE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE due_amount ADD COLUMN IF NOT EXISTS interest_rate NUMERIC(6, 2) NOT NULL DEFAULT 0.00;

        -- Account closure tracking on the customer record
        ALTER TABLE collection_customers ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
        ALTER TABLE collection_customers ADD COLUMN IF NOT EXISTS closing_summary JSONB;

        -- 4. Daily Collection Schedule
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

        -- 5. Field Collection Records (Visit Logs)
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

        -- 6. Payment Collection
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
        ALTER TABLE payment_collection ADD COLUMN IF NOT EXISTS entry_type VARCHAR(20) NOT NULL DEFAULT 'IN';
        ALTER TABLE payment_collection ADD COLUMN IF NOT EXISTS notes TEXT DEFAULT '';

        -- 7. Payment Receipts
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

        -- 8. Follow-ups
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

        -- 9. Daily Summary
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

        -- 10. Daily Closing
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

        -- 11. Cash Handover
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

        -- Indexes for high-frequency queries
        CREATE INDEX IF NOT EXISTS idx_sched_date_col ON collection_schedule(schedule_date, collector_id);
        CREATE INDEX IF NOT EXISTS idx_sched_status ON collection_schedule(status);
        CREATE INDEX IF NOT EXISTS idx_payment_col_date ON payment_collection(collector_id, payment_date);
        CREATE INDEX IF NOT EXISTS idx_records_cust ON collection_records(customer_id);
        CREATE INDEX IF NOT EXISTS idx_receipts_num ON payment_receipts(receipt_number);
    """)


SAMPLE_50_CUSTOMERS = [
    ("Ramesh Patil", "+91 98201 11201", "Shop 4, Gandhi Market", "Gandhi Market", 1000, 4000),
    ("Suresh Jadhav", "+91 98201 11202", "12/A, Shivaji Chowk", "Shivaji Chowk", 1500, 3000),
    ("Rahul Pawar", "+91 98201 11203", "Plot 45, MIDC Phase 1", "MIDC Phase 1", 1000, 2000),
    ("Sunil Jagtap", "+91 98201 11204", "Near Maruti Temple, Station Rd", "Station Road", 1000, 5000),
    ("Anil Deshmukh", "+91 98201 11205", "Flat 203, Shanti Heights", "Gandhi Market", 1200, 4800),
    ("Vijay Shinde", "+91 98201 11206", "Gala 7, Wholesale Mandi", "Gandhi Market", 800, 3200),
    ("Mahesh Kulkarni", "+91 98201 11207", "House 18, Tilak Ali", "Shivaji Chowk", 1500, 6000),
    ("Ganesh Gaikwad", "+91 98201 11208", "Opposite Bus Stand, Main Rd", "Station Road", 1000, 3000),
    ("Prakash Chavan", "+91 98201 11209", "Block C-4, Industrial Area", "MIDC Phase 1", 2000, 8000),
    ("Deepak More", "+91 98201 11210", "Shop 11, Vegetable Market", "Gandhi Market", 1000, 2500),
    ("Sanjay Kamble", "+91 98201 11211", "Lane 3, Ambedkar Nagar", "Station Road", 750, 2250),
    ("Nitin Bhosale", "+91 98201 11212", "Shop 2, Laxmi Narayan Complex", "Shivaji Chowk", 1100, 4400),
    ("Ajay Ghorpade", "+91 98201 11213", "B-14, Green Valley Society", "Gandhi Market", 950, 2850),
    ("Sachin Sawant", "+91 98201 11214", "Plot 89, Sector 4", "MIDC Phase 1", 1300, 5200),
    ("Amit Salunkhe", "+91 98201 11215", "Near City Post Office", "Station Road", 1000, 4000),
    ("Pradeep Mane", "+91 98201 11216", "House 55, Bazar Peth", "Shivaji Chowk", 1250, 5000),
    ("Sandip Wagh", "+91 98201 11217", "Gala 15, APMC Fruit Market", "Gandhi Market", 1600, 4800),
    ("Santosh Mohite", "+91 98201 11218", "Workshop 3, Old Agra Rd", "Station Road", 1000, 3500),
    ("Kiran Thorat", "+91 98201 11219", "Plot 12, ITI Road", "MIDC Phase 1", 1400, 4200),
    ("Rajendra Nikam", "+91 98201 11220", "Shop 9, Municipal Market", "Shivaji Chowk", 1000, 3000),
    ("Dattatray Kale", "+91 98201 11221", "Flat 101, Sai Plaza", "Gandhi Market", 850, 2550),
    ("Vikas Tambe", "+91 98201 11222", "Behind Petrol Pump, Highway", "Station Road", 1000, 4000),
    ("Dinesh Shelar", "+91 98201 11223", "Plot 67, Auto Hub", "MIDC Phase 1", 1500, 4500),
    ("Baban Kadam", "+91 98201 11224", "House 24, Subhash Nagar", "Shivaji Chowk", 1000, 3000),
    ("Pandurang Ghodke", "+91 98201 11225", "Stall 18, Fish Market", "Gandhi Market", 700, 2100),
    ("Ashok Nalawade", "+91 98201 11226", "23/B, Railway Colony", "Station Road", 1000, 4000),
    ("Kishor Yadav", "+91 98201 11227", "Unit 5, Precision Works", "MIDC Phase 1", 1750, 5250),
    ("Gopal Joshi", "+91 98201 11228", "Shop 6, Brahmin Ali", "Shivaji Chowk", 1200, 3600),
    ("Baburao Shelke", "+91 98201 11229", "Shop 22, Flower Galli", "Gandhi Market", 800, 2400),
    ("Hanumant Raut", "+91 98201 11230", "Old Bridge Corner", "Station Road", 1000, 3000),
    ("Namdev Doke", "+91 98201 11231", "Shed 14, Small Scale Zone", "MIDC Phase 1", 1500, 4500),
    ("Arun Sonawane", "+91 98201 11232", "House 78, Shivaji Nagar", "Shivaji Chowk", 900, 2700),
    ("Balasaheb Gite", "+91 98201 11233", "Gala 3, Cloth Market", "Gandhi Market", 1100, 3300),
    ("Chandrakant Londhe", "+91 98201 11234", "Near Overbridge, East", "Station Road", 1000, 4000),
    ("Dilip Jagdale", "+91 98201 11235", "Plot 33, Foundry Cluster", "MIDC Phase 1", 1600, 6400),
    ("Eknath Shirke", "+91 98201 11236", "Shop 14, Grain Merchants", "Shivaji Chowk", 1000, 3000),
    ("Gorakh Borse", "+91 98201 11237", "Behind Hanuman Temple", "Gandhi Market", 750, 2250),
    ("Haribhau Gavali", "+91 98201 11238", "Dairy Booth 2, Station Sq", "Station Road", 1000, 3000),
    ("Ishwar Mahajan", "+91 98201 11239", "Plot 92, Chemical Zone", "MIDC Phase 1", 1800, 5400),
    ("Janardhan Khot", "+91 98201 11240", "House 4, Kasba Peth", "Shivaji Chowk", 1000, 4000),
    ("Kailas Badgujar", "+91 98201 11241", "Shop 31, Kirana Bazar", "Gandhi Market", 1050, 3150),
    ("Laxman Sanas", "+91 98201 11242", "Platform Road Gate 1", "Station Road", 1000, 3000),
    ("Madhukar Gunjal", "+91 98201 11243", "Plot 104, Fabrication Row", "MIDC Phase 1", 1400, 4200),
    ("Narayan Bhise", "+91 98201 11244", "Shop 8, Vegetable Shed", "Shivaji Chowk", 950, 2850),
    ("Omkar Sutar", "+91 98201 11245", "Timber Market Yard", "Gandhi Market", 1300, 3900),
    ("Prashant Bagal", "+91 98201 11246", "Taxi Stand, Railway Stn", "Station Road", 1000, 4000),
    ("Ranganath Date", "+91 98201 11247", "Plot 51, Machine Tools", "MIDC Phase 1", 1500, 4500),
    ("Shankar Narwade", "+91 98201 11248", "House 102, Shaniwar Ali", "Shivaji Chowk", 1100, 3300),
    ("Tukaram Methe", "+91 98201 11249", "Shop 19, Spice Market", "Gandhi Market", 850, 2550),
    ("Uttam Khade", "+91 98201 11250", "Parcel Office Lane", "Station Road", 1000, 3000),
]


def seed_collection_demo_data(cursor):
    """Disabled to allow user testing with new live data from mobile."""
    return
    routes_data = [
        ("Route A - Market Hub", "Gandhi Market", "RT-A", "Gandhi Market & Wholesale APMC Area"),
        ("Route B - Central Chowk", "Shivaji Chowk", "RT-B", "Shivaji Chowk & Old Town commercial centers"),
        ("Route C - Station Circle", "Station Road", "RT-C", "Railway Station, Overbridge & Bus Stand shops"),
        ("Route D - Industrial Belt", "MIDC Phase 1", "RT-D", "MIDC Phase 1, auto garages & workshops"),
    ]
    route_id_map = {}
    for r_name, r_area, r_code, r_desc in routes_data:
        cursor.execute(
            """
            INSERT INTO collection_routes (name, area, code, description)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, area = EXCLUDED.area
            RETURNING id, area;
            """,
            (r_name, r_area, r_code, r_desc),
        )
        row = cursor.fetchone()
        if row:
            route_id_map[row[1]] = row[0]

    # Find collector Riya N or fallback to first collection user
    cursor.execute("SELECT id FROM userdetails WHERE LOWER(email) = 'riya@gmail.com' LIMIT 1")
    col_row = cursor.fetchone()
    if not col_row:
        cursor.execute("SELECT id FROM userdetails WHERE business_type = 'Collection' LIMIT 1")
        col_row = cursor.fetchone()
    if not col_row:
        # Create collector Riya N if missing
        cursor.execute(
            """
            INSERT INTO userdetails (full_name, email, dob, business_type, active_plan, user_type, status, enabled_features)
            VALUES ('Riya N', 'riya@gmail.com', '1992-09-01', 'Collection', 'Small Business', 'customer', 'active', '["collection_records", "due_amount", "payment_collection"]'::jsonb)
            ON CONFLICT (email) DO NOTHING
            RETURNING id;
            """
        )
        new_row = cursor.fetchone()
        collector_id = new_row[0] if new_row else 36
    else:
        collector_id = col_row[0]

    target_date = date(2026, 9, 7)

    # If already seeded, skip to avoid running 150 queries on every import
    cursor.execute(
        "SELECT COUNT(*) FROM collection_schedule WHERE schedule_date = %s AND collector_id = %s",
        (target_date, collector_id),
    )
    if cursor.fetchone()[0] >= 50:
        return

    # 2. Insert 50 Customers & Dues
    customer_ids = []
    for idx, (name, mobile, address, area, exp_amt, total_due) in enumerate(SAMPLE_50_CUSTOMERS, start=1):
        acct_no = f"ACC-2026-{idx:04d}"
        route_id = route_id_map.get(area)

        cursor.execute(
            """
            INSERT INTO collection_customers (collector_id, route_id, account_number, name, mobile, address, area, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')
            ON CONFLICT (account_number) DO UPDATE SET
                name = EXCLUDED.name,
                mobile = EXCLUDED.mobile,
                address = EXCLUDED.address,
                area = EXCLUDED.area,
                collector_id = EXCLUDED.collector_id
            RETURNING id;
            """,
            (collector_id, route_id, acct_no, name, mobile, address, area),
        )
        c_id = cursor.fetchone()[0]
        customer_ids.append((c_id, route_id, exp_amt, total_due, idx))

        # Insert / update due_amount
        cursor.execute(
            """
            INSERT INTO due_amount (customer_id, total_due, expected_amount, emi_amount, last_payment_date)
            VALUES (%s, %s, %s, %s, '2026-08-07')
            ON CONFLICT (customer_id) DO UPDATE SET
                total_due = EXCLUDED.total_due,
                expected_amount = EXCLUDED.expected_amount,
                emi_amount = EXCLUDED.emi_amount;
            """,
            (c_id, Decimal(str(total_due)), Decimal(str(exp_amt)), Decimal(str(exp_amt))),
        )

    # 3. Insert or check 50 Schedule records for 2026-09-07
    cursor.execute(
        "SELECT COUNT(*) FROM collection_schedule WHERE schedule_date = %s AND collector_id = %s",
        (target_date, collector_id),
    )
    existing_count = cursor.fetchone()[0]
    if existing_count < 50:
        for c_id, route_id, exp_amt, total_due, route_order in customer_ids:
            cursor.execute(
                """
                INSERT INTO collection_schedule (schedule_date, collector_id, customer_id, route_id, route_order, expected_amount, collected_amount, status)
                VALUES (%s, %s, %s, %s, %s, %s, 0.00, 'PENDING')
                ON CONFLICT (schedule_date, collector_id, customer_id) DO UPDATE SET
                    route_order = EXCLUDED.route_order,
                    expected_amount = EXCLUDED.expected_amount;
                """,
                (target_date, collector_id, c_id, route_id, route_order, Decimal(str(exp_amt))),
            )

    # 4. Initialize or Recalculate daily_collection_summary
    recalculate_daily_summary(cursor, collector_id, target_date)
    # 5. Populate exact dashboard demo data (Today: 50k/42k/8k, August last month, July)
    seed_collection_dashboard_data(cursor, collector_id)


def seed_collection_dashboard_data(cursor, collector_id=None):
    """Disabled to allow user testing with new live data from mobile."""
    return

    cursor.execute(
        "SELECT id, route_id, name, mobile FROM collection_customers WHERE collector_id = %s ORDER BY id ASC LIMIT 50",
        (collector_id,),
    )
    customers = cursor.fetchall()
    if len(customers) < 50:
        return

    today = date(2026, 9, 7)

    # 1. 2026-09-07: Exactly 50 customers: 42 Collected (25 Cash, 17 UPI), 3 Pending, 5 Missed
    for idx, (cid, rid, cname, cmobile) in enumerate(customers):
        exp = Decimal("1000.00")
        if idx < 42:
            status = "COLLECTED"
            col_amt = exp
            pay_method = "CASH" if idx < 25 else "UPI"
        elif idx < 45:
            status = "PENDING"
            col_amt = Decimal("0.00")
            pay_method = None
        else:
            missed_statuses = ["NOT_AVAILABLE", "REFUSED", "MISSED", "RESCHEDULED", "MISSED"]
            status = missed_statuses[idx - 45]
            col_amt = Decimal("0.00")
            pay_method = None

        cursor.execute(
            """
            INSERT INTO collection_schedule (schedule_date, collector_id, customer_id, route_id, route_order, expected_amount, collected_amount, status, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (schedule_date, collector_id, customer_id) DO UPDATE SET
                expected_amount = EXCLUDED.expected_amount,
                collected_amount = EXCLUDED.collected_amount,
                status = EXCLUDED.status,
                notes = EXCLUDED.notes
            RETURNING id;
            """,
            (today, collector_id, cid, rid, idx + 1, exp, col_amt, status, f"Daily beat order #{idx + 1}"),
        )
        sched_id = cursor.fetchone()[0]

        if status == "COLLECTED" and pay_method:
            cursor.execute(
                "SELECT id FROM payment_collection WHERE schedule_id = %s AND customer_id = %s LIMIT 1",
                (sched_id, cid),
            )
            pay_row = cursor.fetchone()
            if not pay_row:
                cursor.execute(
                    """
                    INSERT INTO payment_collection (schedule_id, customer_id, collector_id, amount, payment_method, transaction_ref, entry_type, notes, status, payment_date)
                    VALUES (%s, %s, %s, %s, %s, %s, 'IN', %s, 'SUCCESS', '2026-09-07 10:00:00'::timestamptz + (interval '12 minutes' * %s))
                    RETURNING id;
                    """,
                    (sched_id, cid, collector_id, col_amt, pay_method, f"TXN-20260907-{idx+1:04d}", f"EMI received via {pay_method}", idx),
                )
                pid = cursor.fetchone()[0]
                rec_no = f"REC-20260907-{idx+1:04d}"
                cursor.execute(
                    """
                    INSERT INTO payment_receipts (payment_id, receipt_number, customer_id, collector_id, amount, payment_method, transaction_ref, notes, receipt_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '2026-09-07 10:00:00'::timestamptz + (interval '12 minutes' * %s))
                    ON CONFLICT (receipt_number) DO NOTHING;
                    """,
                    (pid, rec_no, cid, collector_id, col_amt, pay_method, f"TXN-20260907-{idx+1:04d}", f"Receipt for {cname}", idx),
                )
        elif status in ("NOT_AVAILABLE", "REFUSED", "MISSED", "RESCHEDULED"):
            reasons = {
                "NOT_AVAILABLE": ("Customer Not Available", "Shop was closed during regular morning beat"),
                "REFUSED": ("Payment Dispute", "Customer requested dispute resolution before payment"),
                "MISSED": ("Premises Locked", "Customer out of town, no response on phone"),
                "RESCHEDULED": ("Customer Rescheduled", "Customer requested evening collection on 8th Sep"),
            }
            r_title, r_notes = reasons.get(status, ("Visit Missed", "Customer could not be visited"))
            cursor.execute(
                """
                INSERT INTO collection_records (schedule_id, customer_id, collector_id, visit_date, status, reason, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING;
                """,
                (sched_id, cid, collector_id, today, status, r_title, r_notes),
            )

    recalculate_daily_summary(cursor, collector_id, today)

    # 2. Seed Historical Data for August 2026 (Last Month)
    aug_dates = [date(2026, 8, 8), date(2026, 8, 15), date(2026, 8, 22), date(2026, 8, 29)]
    for a_idx, a_date in enumerate(aug_dates):
        batch = customers[a_idx * 10 : (a_idx + 1) * 10]
        for c_idx, (cid, rid, cname, cmobile) in enumerate(batch):
            exp = Decimal("1000.00")
            is_col = (c_idx % 8 != 7)  # 7 out of 8 collected
            stat = "COLLECTED" if is_col else "MISSED"
            amt = exp if is_col else Decimal("0.00")
            cursor.execute(
                """
                INSERT INTO collection_schedule (schedule_date, collector_id, customer_id, route_id, route_order, expected_amount, collected_amount, status, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (schedule_date, collector_id, customer_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    collected_amount = EXCLUDED.collected_amount
                RETURNING id;
                """,
                (a_date, collector_id, cid, rid, c_idx + 1, exp, amt, stat, f"August weekly beat #{a_idx+1}"),
            )
            s_id = cursor.fetchone()[0]
            if is_col:
                pm = "CASH" if c_idx % 2 == 0 else "UPI"
                cursor.execute("SELECT id FROM payment_collection WHERE schedule_id = %s LIMIT 1", (s_id,))
                if not cursor.fetchone():
                    cursor.execute(
                        """
                        INSERT INTO payment_collection (schedule_id, customer_id, collector_id, amount, payment_method, transaction_ref, entry_type, notes, status, payment_date)
                        VALUES (%s, %s, %s, %s, %s, %s, 'IN', %s, 'SUCCESS', %s)
                        RETURNING id;
                        """,
                        (s_id, cid, collector_id, amt, pm, f"TXN-AUG-{a_idx}-{c_idx}", "August monthly EMI", datetime(a_date.year, a_date.month, a_date.day, 11, 0, 0)),
                    )
                    p_id = cursor.fetchone()[0]
                    cursor.execute(
                        """
                        INSERT INTO payment_receipts (payment_id, receipt_number, customer_id, collector_id, amount, payment_method, transaction_ref, notes, receipt_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING;
                        """,
                        (p_id, f"REC-AUG-{a_idx}-{c_idx:03d}", cid, collector_id, amt, pm, f"TXN-AUG-{a_idx}-{c_idx}", "August Receipt", datetime(a_date.year, a_date.month, a_date.day, 11, 0, 0)),
                    )
            else:
                cursor.execute(
                    """
                    INSERT INTO collection_records (schedule_id, customer_id, collector_id, visit_date, status, reason, notes)
                    VALUES (%s, %s, %s, %s, 'MISSED', 'Shop closed', 'Owner not present during August beat')
                    ON CONFLICT DO NOTHING;
                    """,
                    (s_id, cid, collector_id, a_date),
                )
        recalculate_daily_summary(cursor, collector_id, a_date)

    # 3. Seed July 2026 (Q3)
    july_date = date(2026, 7, 20)
    for c_idx, (cid, rid, cname, cmobile) in enumerate(customers[:15]):
        exp = Decimal("1000.00")
        cursor.execute(
            """
            INSERT INTO collection_schedule (schedule_date, collector_id, customer_id, route_id, route_order, expected_amount, collected_amount, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'COLLECTED')
            ON CONFLICT (schedule_date, collector_id, customer_id) DO UPDATE SET
                status = 'COLLECTED',
                collected_amount = EXCLUDED.expected_amount
            RETURNING id;
            """,
            (july_date, collector_id, cid, rid, c_idx + 1, exp, exp),
        )
        row = cursor.fetchone()
        if row:
            s_id = row[0]
            cursor.execute("SELECT id FROM payment_collection WHERE schedule_id = %s LIMIT 1", (s_id,))
            if not cursor.fetchone():
                cursor.execute(
                    """
                    INSERT INTO payment_collection (schedule_id, customer_id, collector_id, amount, payment_method, transaction_ref, entry_type, status, payment_date)
                    VALUES (%s, %s, %s, %s, 'CASH', %s, 'IN', 'SUCCESS', %s);
                    """,
                    (s_id, cid, collector_id, exp, f"TXN-JUL-{c_idx}", datetime(2026, 7, 20, 14, 0, 0)),
                )
    recalculate_daily_summary(cursor, collector_id, july_date)



def recalculate_daily_summary(cursor, collector_id, target_date):
    """Computes exact aggregated summary from collection_schedule and payment_collection."""
    cursor.execute(
        """
        SELECT 
            COUNT(*) as total_assigned,
            COUNT(CASE WHEN status = 'COLLECTED' THEN 1 END) as total_collected,
            COUNT(CASE WHEN status = 'PENDING' THEN 1 END) as total_pending,
            COUNT(CASE WHEN status = 'MISSED' THEN 1 END) as total_missed,
            COUNT(CASE WHEN status = 'NOT_AVAILABLE' THEN 1 END) as total_not_available,
            COUNT(CASE WHEN status = 'REFUSED' THEN 1 END) as total_refused,
            COUNT(CASE WHEN status = 'RESCHEDULED' THEN 1 END) as total_rescheduled,
            COUNT(CASE WHEN status = 'PARTIAL_PAYMENT' THEN 1 END) as total_partial,
            COALESCE(SUM(expected_amount), 0) as expected_amt,
            COALESCE(SUM(collected_amount), 0) as collected_amt
        FROM collection_schedule
        WHERE schedule_date = %s AND collector_id = %s
        """,
        (target_date, collector_id),
    )
    sched = cursor.fetchone()
    (
        total_assigned,
        total_collected,
        total_pending,
        total_missed,
        total_not_available,
        total_refused,
        total_rescheduled,
        total_partial,
        expected_amt,
        collected_amt,
    ) = sched

    # Payment breakdown for the date & collector - NET per method (Cash In minus Cash Out),
    # since a returned advance should reduce the method's total, not add to it.
    cursor.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN payment_method = 'CASH' AND entry_type = 'IN' THEN amount
                               WHEN payment_method = 'CASH' AND entry_type = 'OUT' THEN -amount
                               ELSE 0 END), 0) as cash_amt,
            COALESCE(SUM(CASE WHEN payment_method = 'UPI' AND entry_type = 'IN' THEN amount
                               WHEN payment_method = 'UPI' AND entry_type = 'OUT' THEN -amount
                               ELSE 0 END), 0) as upi_amt,
            COALESCE(SUM(CASE WHEN payment_method = 'BANK_TRANSFER' AND entry_type = 'IN' THEN amount
                               WHEN payment_method = 'BANK_TRANSFER' AND entry_type = 'OUT' THEN -amount
                               ELSE 0 END), 0) as bank_amt
        FROM payment_collection
        WHERE collector_id = %s AND DATE(payment_date) = %s AND status = 'SUCCESS'
        """,
        (collector_id, target_date),
    )
    pay_row = cursor.fetchone()
    cash_amt, upi_amt, bank_amt = (max(Decimal("0.00"), Decimal(str(v))) for v in pay_row)

    # "Actually Collected" must match the real payment ledger, not just the schedule's
    # collected_amount sum - once a customer's backlog is fully cleared, an extra payment has
    # no schedule row left to attach to, so the schedule-based sum alone under-reports what was
    # genuinely received today.
    collected_amt = max(Decimal(str(collected_amt)), cash_amt + upi_amt + bank_amt)
    remaining_amt = max(Decimal("0.00"), Decimal(str(expected_amt)) - collected_amt)

    # Total Cash Out (advances/change given back to customers) for the day - this is the
    # real "You Will Give (Payable)" figure, computed live from the ledger rather than a
    # hardcoded placeholder.
    cursor.execute(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM payment_collection
        WHERE collector_id = %s AND DATE(payment_date) = %s AND status = 'SUCCESS' AND entry_type = 'OUT'
        """,
        (collector_id, target_date),
    )
    total_given_amt = Decimal(str(cursor.fetchone()[0] or 0))

    cursor.execute(
        """
        INSERT INTO daily_collection_summary (
            summary_date, collector_id,
            total_assigned, total_collected, total_pending, total_missed,
            total_not_available, total_refused, total_rescheduled, total_partial,
            total_expected_amount, total_collected_amount, total_remaining_amount,
            cash_amount, upi_amount, bank_amount, updated_at
        ) VALUES (
            %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, NOW()
        )
        ON CONFLICT (summary_date, collector_id) DO UPDATE SET
            total_assigned = EXCLUDED.total_assigned,
            total_collected = EXCLUDED.total_collected,
            total_pending = EXCLUDED.total_pending,
            total_missed = EXCLUDED.total_missed,
            total_not_available = EXCLUDED.total_not_available,
            total_refused = EXCLUDED.total_refused,
            total_rescheduled = EXCLUDED.total_rescheduled,
            total_partial = EXCLUDED.total_partial,
            total_expected_amount = EXCLUDED.total_expected_amount,
            total_collected_amount = EXCLUDED.total_collected_amount,
            total_remaining_amount = EXCLUDED.total_remaining_amount,
            cash_amount = EXCLUDED.cash_amount,
            upi_amount = EXCLUDED.upi_amount,
            bank_amount = EXCLUDED.bank_amount,
            updated_at = NOW();
        """,
        (
            target_date,
            collector_id,
            total_assigned,
            total_collected,
            total_pending,
            total_missed,
            total_not_available,
            total_refused,
            total_rescheduled,
            total_partial,
            expected_amt,
            collected_amt,
            remaining_amt,
            cash_amt,
            upi_amt,
            bank_amt,
        ),
    )

    is_closed = is_day_closed(cursor, collector_id, target_date)
    return {
        "summaryDate": str(target_date),
        "date": str(target_date),
        "collectorId": collector_id,
        "totalAssigned": total_assigned,
        "totalCollected": total_collected,
        "totalPending": total_pending,
        "totalMissed": total_missed,
        "totalNotAvailable": total_not_available,
        "totalRefused": total_refused,
        "totalRescheduled": total_rescheduled,
        "totalPartial": total_partial,
        "totalExpected": float(expected_amt),
        "totalExpectedAmount": float(expected_amt),
        "totalCollectedAmount": float(collected_amt),
        "totalRemainingAmount": float(remaining_amt),
        "totalPendingAmount": float(remaining_amt),
        "cashAmount": float(cash_amt),
        "upiAmount": float(upi_amt),
        "bankAmount": float(bank_amt),
        "totalGivenAmount": float(total_given_amt),
        "collectionRate": round((total_collected / total_assigned * 100) if total_assigned > 0 else 0, 1),
        "amountRate": round((float(collected_amt) / float(expected_amt) * 100) if float(expected_amt) > 0 else 0, 1),
        "isClosed": is_closed,
        "closingStatus": "CLOSED" if is_closed else "OPEN",
    }


def is_day_closed(cursor, collector_id, target_date):
    """Check if the collector's daily collection is already closed."""
    cursor.execute(
        """
        SELECT id, is_closed, closed_at, closed_by 
        FROM collection_daily_closing 
        WHERE closing_date = %s AND collector_id = %s AND is_closed = TRUE
        """,
        (target_date, collector_id),
    )
    return cursor.fetchone() is not None


def generate_receipt_number(cursor, target_date):
    """Generates unique formatted receipt number: REC-YYYYMMDD-XXXX."""
    date_part = target_date.strftime("%Y%m%d")
    cursor.execute(
        """
        SELECT COUNT(*) 
        FROM payment_receipts 
        WHERE receipt_number LIKE %s
        """,
        (f"REC-{date_part}-%",),
    )
    count = cursor.fetchone()[0] + 1
    return f"REC-{date_part}-{count:04d}"
