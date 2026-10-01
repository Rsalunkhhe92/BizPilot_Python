"""
Building Maintenance Business Service Module
Handles schema provisioning for Building / Floors / Flats / Building Members.

Architecture note: a "building" belongs to one Business Saathi (AutoLedger)
customer account (owner_id -> userdetails.id), who acts as the Building Manager.
Building members, staff, and visitors are plain records the manager maintains -
they are NOT application users and have no auth credentials.
"""

from datetime import date


def ensure_building_schema(cursor):
    """Create all required Building Maintenance tables if they don't exist (Phase 1)."""
    cursor.execute("""
        -- 1. Buildings (schema allows more than one per owner for future multi-building plans;
        --    the current subscription tier enforces a limit of 1 at the application layer)
        CREATE TABLE IF NOT EXISTS buildings (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            name VARCHAR(150) NOT NULL,
            code VARCHAR(50),
            address TEXT,
            area VARCHAR(150),
            city VARCHAR(100),
            state VARCHAR(100),
            pincode VARCHAR(12),
            num_floors INT NOT NULL DEFAULT 0,
            num_flats INT NOT NULL DEFAULT 0,
            building_type VARCHAR(50) NOT NULL DEFAULT 'Residential',
            construction_year INT,
            contact_number VARCHAR(20),
            emergency_contact VARCHAR(20),
            photo_url TEXT,
            description TEXT,
            notes TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_buildings_owner ON buildings(owner_id);

        -- 2. Floors
        CREATE TABLE IF NOT EXISTS building_floors (
            id BIGSERIAL PRIMARY KEY,
            building_id BIGINT NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
            floor_number INT NOT NULL,
            floor_name VARCHAR(100),
            num_flats INT NOT NULL DEFAULT 0,
            description TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_building_floor_number UNIQUE (building_id, floor_number)
        );
        CREATE INDEX IF NOT EXISTS idx_building_floors_building ON building_floors(building_id);

        -- 3. Flats / Rooms
        CREATE TABLE IF NOT EXISTS building_flats (
            id BIGSERIAL PRIMARY KEY,
            building_id BIGINT NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
            floor_id BIGINT REFERENCES building_floors(id) ON DELETE SET NULL,
            flat_number VARCHAR(30) NOT NULL,
            flat_type VARCHAR(50),
            area NUMERIC(10, 2),
            occupancy_status VARCHAR(20) NOT NULL DEFAULT 'Vacant' CHECK (
                occupancy_status IN ('Occupied', 'Vacant', 'Rented', 'Under Maintenance')
            ),
            owner_name VARCHAR(150),
            tenant_name VARCHAR(150),
            primary_mobile VARCHAR(20),
            parking_slot VARCHAR(30),
            maintenance_amount NUMERIC(10, 2) NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_building_flat_number UNIQUE (building_id, flat_number)
        );
        CREATE INDEX IF NOT EXISTS idx_building_flats_building ON building_flats(building_id);
        CREATE INDEX IF NOT EXISTS idx_building_flats_floor ON building_flats(floor_id);

        -- 4. Building Members (records only - NOT application users, no auth credentials)
        CREATE TABLE IF NOT EXISTS building_members (
            id BIGSERIAL PRIMARY KEY,
            building_id BIGINT NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
            flat_id BIGINT REFERENCES building_flats(id) ON DELETE SET NULL,
            full_name VARCHAR(150) NOT NULL,
            mobile_number VARCHAR(20),
            email VARCHAR(150),
            member_type VARCHAR(20) NOT NULL DEFAULT 'Owner' CHECK (
                member_type IN ('Owner', 'Tenant', 'Family Member', 'Other')
            ),
            emergency_contact VARCHAR(20),
            vehicle_number VARCHAR(30),
            status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_building_members_building ON building_members(building_id);
        CREATE INDEX IF NOT EXISTS idx_building_members_flat ON building_members(flat_id);

        -- 5. Maintenance Charge Config (Phase 2): one default additional-charges
        --    template per building, applied whenever new bills are generated.
        CREATE TABLE IF NOT EXISTS building_maintenance_config (
            id BIGSERIAL PRIMARY KEY,
            building_id BIGINT NOT NULL UNIQUE REFERENCES buildings(id) ON DELETE CASCADE,
            water_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            parking_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            common_electricity NUMERIC(10, 2) NOT NULL DEFAULT 0,
            security_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            cleaning_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            lift_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            other_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            late_payment_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            discount NUMERIC(10, 2) NOT NULL DEFAULT 0,
            due_day_of_month INT NOT NULL DEFAULT 10 CHECK (due_day_of_month BETWEEN 1 AND 28),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        -- 6. Maintenance Bills (one per flat per billing month)
        CREATE TABLE IF NOT EXISTS maintenance_bills (
            id BIGSERIAL PRIMARY KEY,
            building_id BIGINT NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
            flat_id BIGINT NOT NULL REFERENCES building_flats(id) ON DELETE CASCADE,
            billing_month DATE NOT NULL,
            base_maintenance NUMERIC(10, 2) NOT NULL DEFAULT 0,
            water_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            parking_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            common_electricity NUMERIC(10, 2) NOT NULL DEFAULT 0,
            security_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            cleaning_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            lift_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            other_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            late_payment_charges NUMERIC(10, 2) NOT NULL DEFAULT 0,
            discount NUMERIC(10, 2) NOT NULL DEFAULT 0,
            previous_outstanding NUMERIC(10, 2) NOT NULL DEFAULT 0,
            total_amount NUMERIC(10, 2) NOT NULL DEFAULT 0,
            paid_amount NUMERIC(10, 2) NOT NULL DEFAULT 0,
            status VARCHAR(20) NOT NULL DEFAULT 'Pending' CHECK (
                status IN ('Pending', 'Partially Paid', 'Paid', 'Overdue', 'Cancelled')
            ),
            due_date DATE,
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_bill_flat_month UNIQUE (flat_id, billing_month)
        );
        CREATE INDEX IF NOT EXISTS idx_maintenance_bills_building ON maintenance_bills(building_id);
        CREATE INDEX IF NOT EXISTS idx_maintenance_bills_flat ON maintenance_bills(flat_id);
        CREATE INDEX IF NOT EXISTS idx_maintenance_bills_month ON maintenance_bills(billing_month);

        -- 7. Maintenance Payments (against a specific bill)
        CREATE TABLE IF NOT EXISTS maintenance_payments (
            id BIGSERIAL PRIMARY KEY,
            building_id BIGINT NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
            flat_id BIGINT NOT NULL REFERENCES building_flats(id) ON DELETE CASCADE,
            member_id BIGINT REFERENCES building_members(id) ON DELETE SET NULL,
            bill_id BIGINT NOT NULL REFERENCES maintenance_bills(id) ON DELETE CASCADE,
            amount NUMERIC(10, 2) NOT NULL CHECK (amount > 0),
            payment_date DATE NOT NULL DEFAULT CURRENT_DATE,
            payment_method VARCHAR(20) NOT NULL DEFAULT 'Cash' CHECK (
                payment_method IN ('Cash', 'UPI', 'Bank Transfer', 'Card', 'Other')
            ),
            transaction_reference VARCHAR(100),
            notes TEXT,
            receipt_number VARCHAR(50) NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_maintenance_payments_building ON maintenance_payments(building_id);
        CREATE INDEX IF NOT EXISTS idx_maintenance_payments_flat ON maintenance_payments(flat_id);
        CREATE INDEX IF NOT EXISTS idx_maintenance_payments_bill ON maintenance_payments(bill_id);

        -- 8. Building Staff (Phase 3) - records only, NOT application users
        CREATE TABLE IF NOT EXISTS building_staff (
            id BIGSERIAL PRIMARY KEY,
            building_id BIGINT NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
            full_name VARCHAR(150) NOT NULL,
            mobile_number VARCHAR(20),
            job_type VARCHAR(50) NOT NULL DEFAULT 'Other' CHECK (job_type IN (
                'Security Guard', 'Cleaner', 'Electrician', 'Plumber', 'Maintenance Worker', 'Gardener', 'Other'
            )),
            joining_date DATE,
            salary NUMERIC(10, 2) NOT NULL DEFAULT 0,
            status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
            address TEXT,
            emergency_contact VARCHAR(20),
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_building_staff_building ON building_staff(building_id);

        -- 9. Building Vendors (Phase 3)
        CREATE TABLE IF NOT EXISTS building_vendors (
            id BIGSERIAL PRIMARY KEY,
            building_id BIGINT NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
            vendor_name VARCHAR(150) NOT NULL,
            service_type VARCHAR(50) NOT NULL DEFAULT 'Other' CHECK (service_type IN (
                'Electrician', 'Plumber', 'Lift Service', 'Pest Control', 'CCTV Vendor', 'Cleaning Vendor',
                'Generator Service', 'Fire Safety Vendor', 'Other'
            )),
            contact_person VARCHAR(150),
            mobile VARCHAR(20),
            email VARCHAR(150),
            address TEXT,
            contract_start_date DATE,
            contract_end_date DATE,
            status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_building_vendors_building ON building_vendors(building_id);

        -- 10. Complaints (Phase 3) - can be assigned to a staff member and/or a vendor
        CREATE TABLE IF NOT EXISTS building_complaints (
            id BIGSERIAL PRIMARY KEY,
            building_id BIGINT NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
            flat_id BIGINT REFERENCES building_flats(id) ON DELETE SET NULL,
            member_id BIGINT REFERENCES building_members(id) ON DELETE SET NULL,
            category VARCHAR(30) NOT NULL DEFAULT 'Other' CHECK (category IN (
                'Plumbing', 'Electrical', 'Water', 'Lift', 'Cleaning', 'Security', 'Parking',
                'Common Area', 'Generator', 'CCTV', 'Other'
            )),
            title VARCHAR(200) NOT NULL,
            description TEXT,
            photo_url TEXT,
            priority VARCHAR(10) NOT NULL DEFAULT 'Medium' CHECK (priority IN ('Low', 'Medium', 'High', 'Urgent')),
            assigned_staff_id BIGINT REFERENCES building_staff(id) ON DELETE SET NULL,
            assigned_vendor_id BIGINT REFERENCES building_vendors(id) ON DELETE SET NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'New' CHECK (status IN (
                'New', 'Assigned', 'In Progress', 'Resolved', 'Closed', 'Rejected'
            )),
            resolution_notes TEXT,
            complaint_date DATE NOT NULL DEFAULT CURRENT_DATE,
            resolved_date DATE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_building_complaints_building ON building_complaints(building_id);
        CREATE INDEX IF NOT EXISTS idx_building_complaints_status ON building_complaints(status);
    """)


def generate_maintenance_receipt_number(cursor, target_date):
    """Generates a unique formatted receipt number: MRC-YYYYMMDD-XXXX."""
    date_part = target_date.strftime("%Y%m%d")
    cursor.execute(
        "SELECT COUNT(*) FROM maintenance_payments WHERE receipt_number LIKE %s",
        (f"MRC-{date_part}-%",),
    )
    count = cursor.fetchone()[0] + 1
    return f"MRC-{date_part}-{count:04d}"


def recompute_bill_status(cursor, bill_id):
    """Recalculates a bill's paid_amount (from its payments) and status, idempotently."""
    cursor.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM maintenance_payments WHERE bill_id = %s",
        (bill_id,),
    )
    paid_amount = cursor.fetchone()[0]

    cursor.execute("SELECT total_amount, due_date, status FROM maintenance_bills WHERE id = %s", (bill_id,))
    row = cursor.fetchone()
    if not row:
        return
    total_amount, due_date, current_status = row

    if current_status == "Cancelled":
        new_status = "Cancelled"
    elif paid_amount >= total_amount and total_amount > 0:
        new_status = "Paid"
    elif paid_amount > 0:
        new_status = "Partially Paid"
    elif due_date and due_date < date.today():
        new_status = "Overdue"
    else:
        new_status = "Pending"

    cursor.execute(
        "UPDATE maintenance_bills SET paid_amount = %s, status = %s, updated_at = NOW() WHERE id = %s",
        (paid_amount, new_status, bill_id),
    )
