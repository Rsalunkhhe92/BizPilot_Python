import json
import os
from datetime import date, timedelta

import psycopg
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from collection_api import collection_bp
from collection_service import ensure_collection_schema, seed_collection_demo_data
from inventory_api import inventory_bp
from inventory_service import ensure_inventory_schema
from assistant_api import assistant_bp, ensure_assistant_schema
from building_api import building_bp
from building_service import ensure_building_schema
from driver_api import driver_bp, ensure_driver_schema
from travel_api import travel_bp, ensure_travel_schema

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = Flask(__name__)
CORS(app)
app.register_blueprint(collection_bp)
app.register_blueprint(inventory_bp)
app.register_blueprint(assistant_bp)
app.register_blueprint(building_bp)
app.register_blueprint(driver_bp)
app.register_blueprint(travel_bp)
WEB_DIRECTORY = os.path.join(os.path.dirname(__file__), "web")
VALID_USER_TYPES = {"admin", "customer"}
VALID_PLAN_TYPES = {"monthly", "quarterly", "yearly"}
VALID_STATUS_TYPES = {"active", "inactive", "suspended"}

# Building Maintenance feature keys are namespaced with "building_" so they never collide
# with an unrelated business type's same-named feature (e.g. Fruit Sellers' bare "inventory").
BUILDING_MAINTENANCE_FEATURES = [
    "building_setup", "building_floors", "building_flats", "building_members",
    "building_maintenance", "building_bills", "building_payments", "building_receipts",
    "building_complaints", "building_staff", "building_vendors", "building_assets",
    "building_preventive_maintenance", "building_parking", "building_visitors",
    "building_inventory", "building_expenses", "building_notices", "building_documents",
    "building_emergency_contacts", "building_reports", "building_notifications",
]

DEFAULT_BUSINESS_FEATURES = {
    "Auto-rickshaw drivers": ["vehicle", "trips", "fuel"],
    "Auto-rickshaw driver": ["vehicle", "trips", "fuel"],
    "Auto Driver": ["vehicle", "trips", "fuel"],
    "Auto driver": ["vehicle", "trips", "fuel"],
    "Fruit sellers": ["products", "inventory", "purchases", "sales"],
    "Fruit seller": ["products", "inventory", "purchases", "sales"],
    "Vegetable sellers": ["products", "inventory", "purchases", "sales"],
    "Vegetable seller": ["products", "inventory", "purchases", "sales"],
    "Small retailers": ["products", "inventory", "sales", "purchases"],
    "Small retailer": ["products", "inventory", "sales", "purchases"],
    "Mechanics": ["service_jobs", "vehicle_customer", "spare_parts", "job_status"],
    "Mechanic": ["service_jobs", "vehicle_customer", "spare_parts", "job_status"],
    "Collection": ["collection_records", "due_amount", "payment_collection"],
    "Building Maintenance": BUILDING_MAINTENANCE_FEATURES,
    "Society Management": BUILDING_MAINTENANCE_FEATURES,
    "Travels Bus Booking Online": ["online_booking", "vehicle", "trips", "fuel"],
    "Travels Online Booking": ["online_booking", "vehicle", "trips", "fuel"],
    "Travels": ["online_booking", "vehicle", "trips", "fuel"],
    "Other local businesses": ["ledger", "dues"],
}


def get_default_features_for_business(business_type):
    if not business_type:
        return ["ledger", "dues"]
    clean_bt = str(business_type).strip()
    if clean_bt in DEFAULT_BUSINESS_FEATURES:
        return DEFAULT_BUSINESS_FEATURES[clean_bt]
    clean_lower = clean_bt.lower()
    for k, v in DEFAULT_BUSINESS_FEATURES.items():
        if k.lower() == clean_lower or k.lower().startswith(clean_lower) or clean_lower.startswith(k.lower()):
            return v
    return ["ledger", "dues"]


def get_database_url():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return database_url


def ensure_schema_compatibility():
    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT data_type, character_maximum_length 
                    FROM information_schema.columns 
                    WHERE table_name = 'userdetails' AND column_name = 'active_plan'
                """)
                col_info = cursor.fetchone()
                if col_info and (col_info[0] != 'character varying' or (col_info[1] or 0) < 100):
                    cursor.execute("ALTER TABLE userdetails ALTER COLUMN active_plan TYPE VARCHAR(100);")

                cursor.execute("""
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'userdetails' AND column_name = 'enabled_features'
                """)
                if not cursor.fetchone():
                    cursor.execute("ALTER TABLE userdetails ADD COLUMN enabled_features JSONB DEFAULT '[]'::jsonb;")

                cursor.execute("""
                    DO $$
                    DECLARE r RECORD;
                    BEGIN
                        FOR r IN (
                            SELECT conname
                            FROM pg_constraint
                            WHERE conrelid = 'userdetails'::regclass AND contype = 'c' AND conname LIKE '%active_plan%'
                        ) LOOP
                            EXECUTE 'ALTER TABLE userdetails DROP CONSTRAINT ' || quote_ident(r.conname);
                        END LOOP;
                    END $$;
                """)
                # Clean up any orphaned plan records that have no monthly subscription row
                cursor.execute("""
                    DELETE FROM subscriptions 
                    WHERE plan_name IN (
                        SELECT plan_name 
                        FROM subscriptions 
                        GROUP BY plan_name 
                        HAVING COUNT(CASE WHEN billing_cycle = 'monthly' THEN 1 END) = 0
                    );
                """)
                cursor.execute("""
                    DELETE FROM subscription_plans 
                    WHERE plan_name NOT IN (SELECT DISTINCT plan_name FROM subscriptions);
                """)

                # 3 Separate Audit Logs Tables
                cursor.execute("""
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
                """)

                # Seed initial logs if tables are empty
                cursor.execute("SELECT COUNT(*) FROM customer_logs")
                if cursor.fetchone()[0] == 0:
                    cursor.execute("""
                        INSERT INTO customer_logs (customer_id, customer_name, action, performed_by, details, new_values, created_at)
                        SELECT id, full_name, 'CREATED', 'System Admin', 'Customer record registered for plan: ' || active_plan,
                               jsonb_build_object('fullName', full_name, 'email', email, 'plan', active_plan, 'businessType', business_type),
                               created_at
                        FROM userdetails
                        WHERE user_type = 'customer';
                    """)

                cursor.execute("SELECT COUNT(*) FROM subscription_logs")
                if cursor.fetchone()[0] == 0:
                    cursor.execute("""
                        INSERT INTO subscription_logs (subscription_id, plan_name, action, performed_by, details, new_values, created_at)
                        SELECT id, plan_name, 'CREATED', 'System Admin', 'Subscription configured: ' || plan_name || ' (' || billing_cycle || ' at $' || plan_amount || ')',
                               jsonb_build_object('planName', plan_name, 'cycle', billing_cycle, 'amount', plan_amount),
                               created_at
                        FROM subscriptions;
                    """)

                cursor.execute("SELECT COUNT(*) FROM business_type_logs")
                if cursor.fetchone()[0] == 0:
                    cursor.execute("""
                        INSERT INTO business_type_logs (business_type_id, name, action, performed_by, details, new_values, created_at)
                        SELECT id, name, 'CREATED', 'System Admin', 'Business type established: ' || name,
                               jsonb_build_object('name', name),
                               created_at
                        FROM business_types;
                    """)

                # Admin Notifications Table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS admin_notifications (
                        id BIGSERIAL PRIMARY KEY,
                        category VARCHAR(50) NOT NULL,
                        type VARCHAR(50) NOT NULL,
                        title VARCHAR(200) NOT NULL,
                        message TEXT NOT NULL,
                        customer_id BIGINT,
                        customer_name VARCHAR(150),
                        is_read BOOLEAN NOT NULL DEFAULT FALSE,
                        is_resolved BOOLEAN NOT NULL DEFAULT FALSE,
                        action_url VARCHAR(100),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                # Provision Collection Business Schema
                ensure_collection_schema(cursor)
                # Collection tables have been truncated. Demo seeding disabled.

                # Provision Inventory Business Schema (Fruit Sellers / product-based businesses)
                ensure_inventory_schema(cursor)

                # Provision AI Assistant Schema (chatbot + proactive daily summary cache)
                ensure_assistant_schema(cursor)

                # Provision Building Maintenance Schema (Phase 1: Building/Floors/Flats/Members)
                ensure_building_schema(cursor)
                ensure_driver_schema(cursor)
                ensure_travel_schema(cursor)
                connection.commit()
    except Exception as e:
        print(f"Schema compatibility notice: {e}")

try:
    ensure_schema_compatibility()
except Exception:
    pass


def create_admin_notification(cursor, category, notification_type, title, message, customer_id=None, customer_name=None, action_url=None):
    try:
        cursor.execute(
            """
            INSERT INTO admin_notifications (category, type, title, message, customer_id, customer_name, action_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (category, notification_type, title, message, customer_id, customer_name, action_url),
        )
    except Exception as e:
        print(f"Notification creation notice: {e}")


def sync_admin_notifications(cursor):
    try:
        # 1. Suspended customers issue alert
        cursor.execute("""
            SELECT id, full_name, email 
            FROM userdetails 
            WHERE status = 'suspended' AND user_type = 'customer'
        """)
        for cid, cname, cemail in cursor.fetchall():
            cursor.execute("""
                SELECT 1 FROM admin_notifications 
                WHERE customer_id = %s AND title LIKE '%%Suspended%%' AND is_resolved = FALSE
            """, (cid,))
            if not cursor.fetchone():
                create_admin_notification(
                    cursor,
                    category="Customer",
                    notification_type="critical",
                    title=f"Customer Account Suspended: {cname}",
                    message=f"Customer {cname} ({cemail}) has been placed on suspended status. Please review customer standing and resolve if compliant.",
                    customer_id=cid,
                    customer_name=cname,
                    action_url="Customer Management"
                )

        # 2. Inactive customers alert
        cursor.execute("""
            SELECT id, full_name, email, active_plan 
            FROM userdetails 
            WHERE status = 'inactive' AND user_type = 'customer'
        """)
        for cid, cname, cemail, cplan in cursor.fetchall():
            cursor.execute("""
                SELECT 1 FROM admin_notifications 
                WHERE customer_id = %s AND title LIKE '%%Inactive%%' AND is_resolved = FALSE
            """, (cid,))
            if not cursor.fetchone():
                create_admin_notification(
                    cursor,
                    category="Customer",
                    notification_type="warning",
                    title=f"Customer Inactive: {cname}",
                    message=f"Customer {cname} assigned to plan '{cplan}' is marked inactive. Follow up suggested.",
                    customer_id=cid,
                    customer_name=cname,
                    action_url="Customer Management"
                )

        # 3. Application level check: missing or zero active plans
        cursor.execute("""
            SELECT COUNT(*)
            FROM userdetails
            WHERE user_type = 'customer'
              AND (active_plan IS NULL OR active_plan = '' OR LOWER(active_plan) = 'none')
        """)
        no_plan_count = cursor.fetchone()[0]
        if no_plan_count > 0:
            cursor.execute("SELECT 1 FROM admin_notifications WHERE title = 'Customers Without Assigned Plan' AND is_resolved = FALSE")
            if not cursor.fetchone():
                create_admin_notification(
                    cursor,
                    category="Application",
                    notification_type="issue",
                    title="Customers Without Assigned Plan",
                    message=f"{no_plan_count} customer(s) have no active subscription plan attached.",
                    action_url="Customer Management"
                )

        # 4. Application system integrity
        cursor.execute("SELECT 1 FROM admin_notifications WHERE title = 'System & Database Operational' AND is_resolved = FALSE")
        if not cursor.fetchone():
            create_admin_notification(
                cursor,
                category="Application",
                notification_type="info",
                title="System & Database Operational",
                message="PostgreSQL database, background pipelines, and REST APIs are running at peak health.",
                action_url="Audit Logs"
            )
    except Exception as e:
        print(f"Sync notifications notice: {e}")


def log_customer_action(cursor, customer_id, customer_name, action, performed_by, details, old_values=None, new_values=None):
    try:
        cursor.execute(
            """
            INSERT INTO customer_logs (customer_id, customer_name, action, performed_by, details, old_values, new_values)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            """,
            (
                customer_id,
                customer_name,
                action,
                performed_by or "Admin",
                details,
                json.dumps(old_values or {}),
                json.dumps(new_values or {}),
            ),
        )
    except Exception as e:
        print(f"Customer audit log notice: {e}")


def log_subscription_action(cursor, subscription_id, plan_name, action, performed_by, details, old_values=None, new_values=None):
    try:
        cursor.execute(
            """
            INSERT INTO subscription_logs (subscription_id, plan_name, action, performed_by, details, old_values, new_values)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            """,
            (
                subscription_id,
                plan_name,
                action,
                performed_by or "Admin",
                details,
                json.dumps(old_values or {}),
                json.dumps(new_values or {}),
            ),
        )
    except Exception as e:
        print(f"Subscription audit log notice: {e}")


def log_business_type_action(cursor, business_type_id, name, action, performed_by, details, old_values=None, new_values=None):
    try:
        cursor.execute(
            """
            INSERT INTO business_type_logs (business_type_id, name, action, performed_by, details, old_values, new_values)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            """,
            (
                business_type_id,
                name,
                action,
                performed_by or "Admin",
                details,
                json.dumps(old_values or {}),
                json.dumps(new_values or {}),
            ),
        )
    except Exception as e:
        print(f"Business type audit log notice: {e}")


def require_admin_access():
    user_type = request.headers.get("X-User-Type", "").strip().lower()
    user_email = request.headers.get("X-User-Email", "").strip().lower()

    if user_type != "admin" or not user_email:
        return False

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM userdetails WHERE LOWER(email) = %s AND user_type = 'admin'",
                    (user_email,),
                )
                return cursor.fetchone() is not None
    except (psycopg.Error, RuntimeError):
        return False


def serialize_driver(row):
    has_business_type = len(row) >= 9
    b_type = row[5] if has_business_type else "Other local businesses"
    has_features = len(row) >= 10
    features = row[9] if (has_features and row[9] is not None) else get_default_features_for_business(b_type)
    return {
        "id": row[0],
        "fullName": row[1],
        "email": row[2],
        "dob": row[3],
        "status": row[4],
        "businessType": b_type,
        "subscriptionPlan": row[6] if has_business_type and row[6] else (row[5] or "none"),
        "planAmount": int(row[7] or 0) if has_business_type else 0,
        "subscriptionStatus": row[8] if has_business_type and row[8] else "inactive",
        "enabledFeatures": features if isinstance(features, list) else [],
        "enabled_features": features if isinstance(features, list) else [],
    }


def calculate_end_date(plan_name):
    days_map = {"monthly": 30, "quarterly": 90, "yearly": 365}
    return date.today() + timedelta(days=days_map[plan_name])


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "service": "AutoLedger API"})


@app.get("/admin")
def admin_web_app():
    return send_from_directory(WEB_DIRECTORY, "admin.html")


@app.get("/api/health/db")
def database_health():
    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        return jsonify({"status": "ok", "database": "connected"})
    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"status": "error", "database": "unavailable", "message": str(error)}), 503


@app.post("/api/login")
def login():
    credentials = request.get_json(silent=True) or {}
    email = credentials.get("email", "").strip().lower()
    dob_text = credentials.get("dob", "").strip()

    if not email or not dob_text:
        return jsonify({"message": "Email and date of birth are required"}), 400

    try:
        dob = date.fromisoformat(dob_text)
    except ValueError:
        return jsonify({"message": "Date of birth must use YYYY-MM-DD"}), 400

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT u.id, u.full_name, u.email, u.user_type, u.status,
                           u.business_type, u.active_plan, TO_CHAR(u.dob, 'YYYY-MM-DD'),
                           s.plan_amount, s.billing_cycle, s.status,
                           TO_CHAR(s.start_date, 'YYYY-MM-DD'), TO_CHAR(s.end_date, 'YYYY-MM-DD'),
                           u.enabled_features
                    FROM userdetails u
                    LEFT JOIN LATERAL (
                        SELECT plan_amount, billing_cycle, status, start_date, end_date
                        FROM subscriptions
                        WHERE LOWER(plan_name) = LOWER(u.active_plan)
                        ORDER BY start_date DESC, id DESC
                        LIMIT 1
                    ) s ON TRUE
                    WHERE LOWER(u.email) = %s AND u.dob = %s
                    """,
                    (email, dob),
                )
                user = cursor.fetchone()
    except (psycopg.Error, RuntimeError):
        return jsonify({"message": "Database is unavailable"}), 503

    if user is None:
        return jsonify({"message": "User not available. Email or date of birth does not match."}), 401

    b_type = user[5] or "Other local businesses"
    has_features = len(user) > 13 and user[13] is not None
    features = user[13] if has_features else get_default_features_for_business(b_type)

    return jsonify(
        {
            "user": {
                "id": user[0],
                "fullName": user[1],
                "email": user[2],
                "userType": user[3],
                "status": user[4],
                "businessType": b_type,
                "activePlan": user[6] or "monthly",
                "dob": user[7] or "",
                "planAmount": int(user[8] or 0),
                "billingCycle": user[9] or "monthly",
                "subscriptionStatus": user[10] or "active",
                "startDate": user[11] or "",
                "endDate": user[12] or "",
                "enabledFeatures": features if isinstance(features, list) else [],
            }
        }
    )


@app.get("/api/customer/plans")
def customer_subscription_plans():
    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT 
                        MIN(id) as id,
                        plan_name,
                        COALESCE(MAX(CASE WHEN billing_cycle = 'monthly' THEN plan_amount END), 0) as monthly_amount,
                        COALESCE(MAX(CASE WHEN billing_cycle = 'yearly' THEN plan_amount END), 0) as annual_amount,
                        COALESCE(MAX(description), '') as description,
                        COALESCE(MAX(features::text)::jsonb, '[]'::jsonb) as features,
                        BOOL_OR(is_popular) as is_popular
                    FROM subscriptions
                    WHERE plan_name IS NOT NULL AND plan_name != ''
                    GROUP BY plan_name
                    HAVING COALESCE(MAX(CASE WHEN billing_cycle = 'monthly' THEN plan_amount END), 0) > 0
                    ORDER BY MIN(id)
                """)
                plans = [{
                    "id": row[0],
                    "name": row[1],
                    "monthlyAmount": int(row[2]),
                    "annualAmount": int(row[3]),
                    "description": row[4],
                    "features": row[5] if isinstance(row[5], list) else [],
                    "popular": bool(row[6])
                } for row in cursor.fetchall()]
    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": "Database is unavailable", "error": str(error)}), 503

    return jsonify({"plans": plans})


@app.get("/api/admin/dashboard")
def admin_dashboard():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE user_type = 'customer') AS total_users,
                        COUNT(*) FILTER (WHERE user_type = 'customer' AND status = 'active') AS active_users,
                        COUNT(*) FILTER (WHERE user_type = 'customer' AND status != 'active') AS inactive_users
                    FROM userdetails
                    """
                )
                row = cursor.fetchone()
    except (psycopg.Error, RuntimeError):
        return jsonify({"message": "Database is unavailable"}), 503

    return jsonify(
        {
            "totalUsers": int(row[0] or 0),
            "activeUsers": int(row[1] or 0),
            "inactiveUsers": int(row[2] or 0),
        }
    )


@app.get("/api/admin/drivers")
def admin_customer_list():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        u.id,
                        u.full_name,
                        u.email,
                        TO_CHAR(u.dob, 'YYYY-MM-DD'),
                        u.status,
                        u.business_type,
                        u.active_plan,
                        s.plan_amount,
                        s.status,
                        u.enabled_features
                    FROM userdetails u
                    LEFT JOIN LATERAL (
                        SELECT plan_name, plan_amount, status, end_date
                        FROM subscriptions
                        WHERE plan_name = u.active_plan
                        ORDER BY start_date DESC
                        LIMIT 1
                    ) s ON TRUE
                    WHERE u.user_type = 'customer'
                    ORDER BY u.id DESC
                    """
                )
                drivers = [serialize_driver(row) for row in cursor.fetchall()]
    except (psycopg.Error, RuntimeError):
        return jsonify({"message": "Database is unavailable"}), 503

    return jsonify({"drivers": drivers})


@app.post("/api/admin/drivers")
def create_customer():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    payload = request.get_json(silent=True) or {}
    full_name = str(payload.get("fullName", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    dob_text = str(payload.get("dob", "")).strip()
    business_type = str(payload.get("businessType", "")).strip()
    status = str(payload.get("status", "active")).strip().lower()
    subscription_plan = str(payload.get("subscriptionPlan", "")).strip()
    raw_amount = payload.get("planAmount", 0)

    if not full_name or not email or not dob_text or not business_type or not subscription_plan:
        return jsonify({"message": "Full name, email, date of birth, business type, and plan are required"}), 400

    try:
        plan_amount = int(raw_amount)
    except (TypeError, ValueError):
        plan_amount = 0

    if plan_amount < 0:
        return jsonify({"message": "Plan amount cannot be negative"}), 400

    if status not in VALID_STATUS_TYPES:
        return jsonify({"message": "Invalid status value"}), 400

    try:
        dob = date.fromisoformat(dob_text)
    except ValueError:
        return jsonify({"message": "Date of birth must use YYYY-MM-DD"}), 400

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                # 1. Validate business type
                cursor.execute("SELECT 1 FROM business_types WHERE name = %s", (business_type,))
                if cursor.fetchone() is None:
                    return jsonify({"message": "Selected business type is unavailable"}), 400

                # 2. Validate subscription plan case-insensitively
                cursor.execute(
                    "SELECT plan_name FROM subscriptions WHERE LOWER(plan_name) = LOWER(%s) LIMIT 1",
                    (subscription_plan,),
                )
                matched = cursor.fetchone()
                if not matched:
                    cursor.execute(
                        "SELECT plan_name FROM subscription_plans WHERE LOWER(plan_name) = LOWER(%s) LIMIT 1",
                        (subscription_plan,),
                    )
                    matched = cursor.fetchone()

                if not matched:
                    return jsonify({"message": f"The subscription plan '{subscription_plan}' is unavailable"}), 400

                actual_plan_name = matched[0]

                # If amount wasn't provided, pick the monthly or min amount
                if plan_amount <= 0:
                    cursor.execute(
                        """
                        SELECT COALESCE(MAX(CASE WHEN billing_cycle = 'monthly' THEN plan_amount END), MIN(plan_amount))
                        FROM subscriptions
                        WHERE LOWER(plan_name) = LOWER(%s)
                        """,
                        (actual_plan_name,),
                    )
                    amt_row = cursor.fetchone()
                    if amt_row and amt_row[0]:
                        plan_amount = int(amt_row[0])

                raw_features = payload.get("enabledFeatures") if "enabledFeatures" in payload else payload.get("enabled_features")
                if isinstance(raw_features, list) and len(raw_features) > 0:
                    enabled_features = raw_features
                else:
                    enabled_features = get_default_features_for_business(business_type)

                # 3. Insert customer
                cursor.execute(
                    """
                    INSERT INTO userdetails (full_name, email, dob, business_type, active_plan, user_type, status, enabled_features)
                    VALUES (%s, %s, %s, %s, %s, 'customer', %s, %s::jsonb)
                    RETURNING id
                    """,
                    (full_name, email, dob, business_type, actual_plan_name, status, json.dumps(enabled_features)),
                )
                user_id = cursor.fetchone()[0]

                # Customer Audit Log
                admin_actor = request.headers.get("X-User-Email", "Admin")
                log_customer_action(
                    cursor,
                    customer_id=user_id,
                    customer_name=full_name,
                    action="CREATED",
                    performed_by=admin_actor,
                    details=f"Customer '{full_name}' registered with plan '{actual_plan_name}' and business category '{business_type}' ({len(enabled_features)} features enabled).",
                    new_values={
                        "fullName": full_name,
                        "email": email,
                        "plan": actual_plan_name,
                        "businessType": business_type,
                        "status": status,
                        "planAmount": plan_amount,
                        "enabledFeatures": enabled_features,
                    }
                )
                connection.commit()

    except psycopg.IntegrityError:
        return jsonify({"message": "A customer with that email already exists. Use a different email address."}), 409
    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": f"Database is unavailable: {str(error)}"}), 503

    return jsonify({"message": "Customer created successfully", "id": user_id, "planAmount": plan_amount}), 201


@app.put("/api/admin/drivers/<int:user_id>")
def update_customer(user_id):
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    payload = request.get_json(silent=True) or {}
    full_name = str(payload.get("fullName", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    dob_text = str(payload.get("dob", "")).strip()
    business_type = str(payload.get("businessType", "")).strip()
    status = str(payload.get("status", "")).strip().lower()
    subscription_plan = str(payload.get("subscriptionPlan", "")).strip()
    raw_amount = payload.get("planAmount", 0)
    raw_features = payload.get("enabledFeatures") if "enabledFeatures" in payload else payload.get("enabled_features")

    if not full_name and not email and not dob_text and not business_type and not status and not subscription_plan and raw_amount in (None, "") and raw_features is None:
        return jsonify({"message": "No updates provided"}), 400

    try:
        plan_amount = int(raw_amount or 0)
    except (TypeError, ValueError):
        plan_amount = 0
    if plan_amount < 0:
        return jsonify({"message": "Plan amount cannot be negative"}), 400

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                # Fetch existing data for audit diff
                cursor.execute(
                    "SELECT full_name, email, TO_CHAR(dob, 'YYYY-MM-DD'), business_type, active_plan, status, enabled_features FROM userdetails WHERE id = %s",
                    (user_id,)
                )
                old_row = cursor.fetchone()
                old_data = {
                    "fullName": old_row[0],
                    "email": old_row[1],
                    "dob": old_row[2],
                    "businessType": old_row[3],
                    "plan": old_row[4],
                    "status": old_row[5],
                    "enabledFeatures": old_row[6] if len(old_row) > 6 and old_row[6] is not None else []
                } if old_row else {}

                if business_type:
                    cursor.execute("SELECT 1 FROM business_types WHERE name = %s", (business_type,))
                    if cursor.fetchone() is None:
                        return jsonify({"message": "Selected business type is unavailable"}), 400

                actual_plan_name = None
                if subscription_plan:
                    cursor.execute(
                        "SELECT plan_name FROM subscriptions WHERE LOWER(plan_name) = LOWER(%s) LIMIT 1",
                        (subscription_plan,),
                    )
                    matched = cursor.fetchone()
                    if not matched:
                        cursor.execute(
                            "SELECT plan_name FROM subscription_plans WHERE LOWER(plan_name) = LOWER(%s) LIMIT 1",
                            (subscription_plan,),
                        )
                        matched = cursor.fetchone()
                    if not matched:
                        if subscription_plan.lower() in ('monthly', 'quarterly', 'yearly', 'none', 'standard'):
                            actual_plan_name = subscription_plan.capitalize()
                        else:
                            return jsonify({"message": f"The subscription plan '{subscription_plan}' is unavailable"}), 400
                    else:
                        actual_plan_name = matched[0]

                has_feature_update = raw_features is not None and isinstance(raw_features, list)
                if full_name or email or dob_text or business_type or status or actual_plan_name or has_feature_update:
                    update_fields = []
                    values = []

                    if full_name:
                        update_fields.append("full_name = %s")
                        values.append(full_name)
                    if email:
                        update_fields.append("email = %s")
                        values.append(email)
                    if dob_text:
                        try:
                            dob = date.fromisoformat(dob_text)
                        except ValueError:
                            return jsonify({"message": "Date of birth must use YYYY-MM-DD"}), 400
                        update_fields.append("dob = %s")
                        values.append(dob)
                    if business_type:
                        update_fields.append("business_type = %s")
                        values.append(business_type)
                    if actual_plan_name:
                        update_fields.append("active_plan = %s")
                        values.append(actual_plan_name)
                    if status:
                        if status not in VALID_STATUS_TYPES:
                            return jsonify({"message": "Invalid status value"}), 400
                        update_fields.append("status = %s")
                        values.append(status)
                    if has_feature_update:
                        update_fields.append("enabled_features = %s::jsonb")
                        values.append(json.dumps(raw_features))

                    values.extend([user_id])
                    cursor.execute(
                        f"UPDATE userdetails SET {', '.join(update_fields)} WHERE id = %s AND user_type = 'customer'",
                        values,
                    )

                    # Customer Audit Log
                    admin_actor = request.headers.get("X-User-Email", "Admin")
                    changed_list = []
                    if full_name and full_name != old_data.get("fullName"):
                        changed_list.append(f"Name to '{full_name}'")
                    if email and email != old_data.get("email"):
                        changed_list.append(f"Email to '{email}'")
                    if actual_plan_name and actual_plan_name != old_data.get("plan"):
                        changed_list.append(f"Plan to '{actual_plan_name}'")
                    if business_type and business_type != old_data.get("businessType"):
                        changed_list.append(f"Business type to '{business_type}'")
                    if status and status != old_data.get("status"):
                        changed_list.append(f"Status to '{status}'")
                    if has_feature_update and set(raw_features) != set(old_data.get("enabledFeatures") or []):
                        changed_list.append(f"Features to [{', '.join(raw_features)}]")

                    details_msg = f"Customer '{old_data.get('fullName', user_id)}' modified: " + (", ".join(changed_list) if changed_list else "profile updated.")
                    log_customer_action(
                        cursor,
                        customer_id=user_id,
                        customer_name=full_name or old_data.get("fullName"),
                        action="UPDATED",
                        performed_by=admin_actor,
                        details=details_msg,
                        old_values=old_data,
                        new_values={
                            "fullName": full_name or old_data.get("fullName"),
                            "plan": actual_plan_name or old_data.get("plan"),
                            "status": status or old_data.get("status"),
                            "businessType": business_type or old_data.get("businessType"),
                            "enabledFeatures": raw_features if has_feature_update else old_data.get("enabledFeatures"),
                        }
                    )

                    connection.commit()
    except psycopg.IntegrityError:
        return jsonify({"message": "A customer with that email already exists"}), 409
    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": f"Database is unavailable: {str(error)}"}), 503

    return jsonify({"message": "Customer updated successfully"})


@app.delete("/api/admin/drivers/<int:user_id>")
def delete_customer(user_id):
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT full_name, email, active_plan, business_type FROM userdetails WHERE id = %s AND user_type = 'customer'",
                    (user_id,)
                )
                cust = cursor.fetchone()
                if cust:
                    admin_actor = request.headers.get("X-User-Email", "Admin")
                    log_customer_action(
                        cursor,
                        customer_id=user_id,
                        customer_name=cust[0],
                        action="DELETED",
                        performed_by=admin_actor,
                        details=f"Customer '{cust[0]}' ({cust[1]}) removed from workspace.",
                        old_values={"fullName": cust[0], "email": cust[1], "plan": cust[2], "businessType": cust[3]}
                    )

                cursor.execute(
                    "DELETE FROM userdetails WHERE id = %s AND user_type = 'customer'",
                    (user_id,),
                )
                connection.commit()
    except (psycopg.Error, RuntimeError):
        return jsonify({"message": "Database is unavailable"}), 503

    return jsonify({"message": "Customer deleted successfully"})


def serialize_business_type(row):
    return {
        "id": row[0],
        "name": row[1],
        "description": row[2],
        "createdAt": row[3],
    }


@app.get("/api/business-types")
def public_business_type_list():
    """Public, unauthenticated business type list for the mobile self-registration screen."""
    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, name, description, TO_CHAR(created_at, 'YYYY-MM-DD')
                    FROM business_types
                    ORDER BY name
                    """
                )
                business_types = [serialize_business_type(row) for row in cursor.fetchall()]
    except (psycopg.Error, RuntimeError):
        return jsonify({"message": "Database is unavailable"}), 503

    return jsonify({"businessTypes": business_types})


@app.post("/api/register")
def register_customer():
    """
    Public, unauthenticated self-registration for the mobile app.
    Writes to the same `userdetails` table as the admin "Create customer" flow
    (POST /api/admin/drivers) so a freshly registered account can log in immediately
    via the existing /api/login (email + dob) flow.

    Unlike the admin flow, this endpoint never trusts client-supplied `status` or
    `enabledFeatures` â€” status is always 'active' and features are always derived
    from the business type, since a self-registering customer shouldn't be able to
    grant themselves arbitrary feature access or account status.
    """
    payload = request.get_json(silent=True) or {}
    full_name = str(payload.get("fullName", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    dob_text = str(payload.get("dob", "")).strip()
    business_type = str(payload.get("businessType", "")).strip()
    subscription_plan = str(payload.get("subscriptionPlan", "")).strip()

    if not full_name or not email or not dob_text or not business_type or not subscription_plan:
        return jsonify({"message": "Full name, email, date of birth, business type, and plan are required"}), 400

    try:
        dob = date.fromisoformat(dob_text)
    except ValueError:
        return jsonify({"message": "Date of birth must use YYYY-MM-DD"}), 400

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                # 1. Validate business type
                cursor.execute("SELECT 1 FROM business_types WHERE name = %s", (business_type,))
                if cursor.fetchone() is None:
                    return jsonify({"message": "Selected business type is unavailable"}), 400

                # 2. Validate subscription plan case-insensitively
                cursor.execute(
                    "SELECT plan_name FROM subscriptions WHERE LOWER(plan_name) = LOWER(%s) LIMIT 1",
                    (subscription_plan,),
                )
                matched = cursor.fetchone()
                if not matched:
                    cursor.execute(
                        "SELECT plan_name FROM subscription_plans WHERE LOWER(plan_name) = LOWER(%s) LIMIT 1",
                        (subscription_plan,),
                    )
                    matched = cursor.fetchone()

                if not matched:
                    return jsonify({"message": f"The subscription plan '{subscription_plan}' is unavailable"}), 400

                actual_plan_name = matched[0]

                # Pick the monthly (or min) amount for the chosen plan
                plan_amount = 0
                cursor.execute(
                    """
                    SELECT COALESCE(MAX(CASE WHEN billing_cycle = 'monthly' THEN plan_amount END), MIN(plan_amount))
                    FROM subscriptions
                    WHERE LOWER(plan_name) = LOWER(%s)
                    """,
                    (actual_plan_name,),
                )
                amt_row = cursor.fetchone()
                if amt_row and amt_row[0]:
                    plan_amount = int(amt_row[0])

                enabled_features = get_default_features_for_business(business_type)

                # 3. Insert customer (status always 'active', features always business-type derived)
                cursor.execute(
                    """
                    INSERT INTO userdetails (full_name, email, dob, business_type, active_plan, user_type, status, enabled_features)
                    VALUES (%s, %s, %s, %s, %s, 'customer', 'active', %s::jsonb)
                    RETURNING id
                    """,
                    (full_name, email, dob, business_type, actual_plan_name, json.dumps(enabled_features)),
                )
                user_id = cursor.fetchone()[0]

                log_customer_action(
                    cursor,
                    customer_id=user_id,
                    customer_name=full_name,
                    action="CREATED",
                    performed_by=f"Self-registered ({email})",
                    details=f"Customer '{full_name}' self-registered via mobile app with plan '{actual_plan_name}' and business category '{business_type}'.",
                    new_values={
                        "fullName": full_name,
                        "email": email,
                        "plan": actual_plan_name,
                        "businessType": business_type,
                        "status": "active",
                        "planAmount": plan_amount,
                        "enabledFeatures": enabled_features,
                    }
                )
                connection.commit()

    except psycopg.IntegrityError:
        return jsonify({"message": "An account with that email already exists. Please sign in instead."}), 409
    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": f"Database is unavailable: {str(error)}"}), 503

    return jsonify({"message": "Account created successfully", "id": user_id, "planAmount": plan_amount}), 201


@app.get("/api/admin/business-types")
def admin_business_type_list():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, name, description, TO_CHAR(created_at, 'YYYY-MM-DD')
                    FROM business_types
                    ORDER BY name
                    """
                )
                business_types = [serialize_business_type(row) for row in cursor.fetchall()]
    except (psycopg.Error, RuntimeError):
        return jsonify({"message": "Database is unavailable"}), 503

    return jsonify({"businessTypes": business_types})


@app.get("/api/admin/subscription-options")
def admin_subscription_options():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT plan_name,
                           COALESCE(billing_cycle, 'monthly') as cycle,
                           plan_amount
                    FROM subscriptions
                    WHERE plan_name IS NOT NULL AND TRIM(plan_name) != ''
                    ORDER BY plan_name ASC,
                             CASE billing_cycle WHEN 'monthly' THEN 1 WHEN 'quarterly' THEN 2 WHEN 'yearly' THEN 3 ELSE 4 END ASC,
                             plan_amount ASC
                    """
                )
                rows = cursor.fetchall()

                plan_map = {}
                for row in rows:
                    p_name = row[0]
                    cycle = row[1]
                    amt = int(row[2])
                    if p_name not in plan_map:
                        plan_map[p_name] = {
                            "plan": p_name,
                            "amount": amt,
                            "amounts": []
                        }
                    plan_map[p_name]["amounts"].append({
                        "cycle": cycle,
                        "amount": amt,
                        "label": f"${amt} ({cycle.capitalize()})"
                    })

                options = list(plan_map.values())
    except (psycopg.Error, RuntimeError):
        return jsonify({"message": "Database is unavailable"}), 503

    return jsonify({"subscriptionOptions": options})


@app.get("/api/admin/subscription-plans")
def admin_subscription_plan_list():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT 
                        MIN(id) as id,
                        plan_name,
                        COALESCE(MAX(CASE WHEN billing_cycle = 'monthly' THEN plan_amount END), 0) as monthly_amount,
                        COALESCE(MAX(CASE WHEN billing_cycle = 'yearly' THEN plan_amount END), 0) as annual_amount,
                        COALESCE(MAX(description), '') as description,
                        COALESCE(MAX(features::text)::jsonb, '[]'::jsonb) as features,
                        BOOL_OR(is_popular) as is_popular
                    FROM subscriptions
                    WHERE plan_name IS NOT NULL AND plan_name != ''
                    GROUP BY plan_name
                    HAVING COALESCE(MAX(CASE WHEN billing_cycle = 'monthly' THEN plan_amount END), 0) > 0
                    ORDER BY MIN(id)
                """)
                plans = [{
                    "id": row[0],
                    "name": row[1],
                    "monthlyAmount": int(row[2]),
                    "annualAmount": int(row[3]),
                    "description": row[4],
                    "features": row[5] if isinstance(row[5], list) else [],
                    "popular": bool(row[6])
                } for row in cursor.fetchall()]
    except (psycopg.Error, RuntimeError):
        return jsonify({"message": "Database is unavailable"}), 503

    return jsonify({"plans": plans})


@app.get("/api/admin/subscriptions")
def admin_subscription_list():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT id, plan_name, COALESCE(billing_cycle, 'monthly') as billing_cycle,
                           plan_amount, status,
                           TO_CHAR(start_date, 'YYYY-MM-DD') as start_date,
                           TO_CHAR(end_date, 'YYYY-MM-DD') as end_date,
                           description, features, is_popular,
                           TO_CHAR(created_at, 'YYYY-MM-DD HH24:MI') as created_at
                    FROM subscriptions
                    ORDER BY plan_amount ASC,
                             plan_name ASC,
                             id DESC
                """)
                subscriptions = [{
                    "id": row[0],
                    "planName": row[1],
                    "billingCycle": row[2],
                    "amount": int(row[3]),
                    "status": row[4],
                    "startDate": row[5],
                    "endDate": row[6],
                    "description": row[7] or "",
                    "features": row[8] if isinstance(row[8], list) else [],
                    "isPopular": bool(row[9]),
                    "createdAt": row[10] or ""
                } for row in cursor.fetchall()]
    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": f"Database is unavailable: {str(error)}"}), 503

    return jsonify({"subscriptions": subscriptions})


@app.post("/api/admin/subscriptions")
def create_subscription():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    payload = request.get_json(silent=True) or {}

    plan_name = str(payload.get("planName") or payload.get("plan_name") or "").strip()
    if not plan_name:
        return jsonify({"message": "Plan name is required"}), 400

    description = str(payload.get("description", "")).strip()
    is_popular = bool(payload.get("isPopular") or payload.get("is_popular") or False)
    status = str(payload.get("status", "active")).strip().lower()
    if status not in {"active", "expired", "cancelled"}:
        status = "active"

    start_date_str = str(payload.get("startDate") or payload.get("start_date") or "").strip()
    try:
        start_date = date.fromisoformat(start_date_str) if start_date_str else date.today()
    except ValueError:
        start_date = date.today()

    raw_features = payload.get("features", [])
    if isinstance(raw_features, str):
        features = [f.strip() for f in raw_features.replace("\n", ",").split(",") if f.strip()]
    elif isinstance(raw_features, list):
        features = [str(f).strip() for f in raw_features if str(f).strip()]
    else:
        features = []

    try:
        monthly_amount = int(payload.get("monthlyAmount") if payload.get("monthlyAmount") is not None else (payload.get("amount") or 0))
    except (TypeError, ValueError):
        return jsonify({"message": "Monthly amount must be a number"}), 400

    try:
        quarterly_amount = int(payload.get("quarterlyAmount") if payload.get("quarterlyAmount") is not None else (payload.get("quarterly_amount") or round(monthly_amount * 3 * 0.9)))
    except (TypeError, ValueError):
        quarterly_amount = int(round(monthly_amount * 3 * 0.9))

    try:
        yearly_amount = int(payload.get("annualAmount") if payload.get("annualAmount") is not None else (payload.get("yearlyAmount") or payload.get("annual_amount") or (monthly_amount * 12)))
    except (TypeError, ValueError):
        yearly_amount = monthly_amount * 12

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                # 1. Insert Monthly row
                monthly_end = start_date + timedelta(days=30)
                cursor.execute(
                    """
                    INSERT INTO subscriptions (plan_name, billing_cycle, plan_amount, status, start_date, end_date, description, features, is_popular)
                    VALUES (%s, 'monthly', %s, %s, %s, %s, %s, %s::jsonb, %s)
                    RETURNING id
                    """,
                    (plan_name, monthly_amount, status, start_date, monthly_end, description, json.dumps(features), is_popular),
                )
                monthly_id = cursor.fetchone()[0]

                # 2. Insert Quarterly row (One row for monthly and second row for quarterly)
                quarterly_end = start_date + timedelta(days=90)
                cursor.execute(
                    """
                    INSERT INTO subscriptions (plan_name, billing_cycle, plan_amount, status, start_date, end_date, description, features, is_popular)
                    VALUES (%s, 'quarterly', %s, %s, %s, %s, %s, %s::jsonb, %s)
                    RETURNING id
                    """,
                    (plan_name, quarterly_amount, status, start_date, quarterly_end, description, json.dumps(features), is_popular),
                )
                quarterly_id = cursor.fetchone()[0]

                # 3. Insert Yearly row if provided
                yearly_id = None
                if yearly_amount > 0:
                    yearly_end = start_date + timedelta(days=365)
                    cursor.execute(
                        """
                        INSERT INTO subscriptions (plan_name, billing_cycle, plan_amount, status, start_date, end_date, description, features, is_popular)
                        VALUES (%s, 'yearly', %s, %s, %s, %s, %s, %s::jsonb, %s)
                        RETURNING id
                        """,
                        (plan_name, yearly_amount, status, start_date, yearly_end, description, json.dumps(features), is_popular),
                    )
                    yearly_id = cursor.fetchone()[0]

                # Keep subscription_plans in sync if needed
                cursor.execute(
                    """
                    INSERT INTO subscription_plans (plan_name, monthly_amount, annual_amount, description, features, is_popular)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (plan_name) DO UPDATE SET
                        monthly_amount = EXCLUDED.monthly_amount,
                        annual_amount = EXCLUDED.annual_amount,
                        description = EXCLUDED.description,
                        features = EXCLUDED.features,
                        is_popular = EXCLUDED.is_popular,
                        updated_at = NOW()
                    """,
                    (plan_name, monthly_amount, int(yearly_amount / 12) if yearly_amount else monthly_amount, description, json.dumps(features), is_popular),
                )

                # Subscription Audit Log
                admin_actor = request.headers.get("X-User-Email", "Admin")
                log_subscription_action(
                    cursor,
                    subscription_id=monthly_id,
                    plan_name=plan_name,
                    action="CREATED",
                    performed_by=admin_actor,
                    details=f"Subscription plan '{plan_name}' created (Monthly: ${monthly_amount}, Quarterly: ${quarterly_amount}, Annual: ${yearly_amount}).",
                    new_values={
                        "planName": plan_name,
                        "monthly": monthly_amount,
                        "quarterly": quarterly_amount,
                        "annual": yearly_amount,
                        "isPopular": is_popular
                    }
                )

                connection.commit()
    except psycopg.IntegrityError as error:
        return jsonify({"message": f"Database integrity error: {str(error)}"}), 409
    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": f"Database is unavailable: {str(error)}"}), 503

    return jsonify({
        "message": f"Plan '{plan_name}' added successfully with monthly and quarterly rows in subscriptions table",
        "monthlyId": monthly_id,
        "quarterlyId": quarterly_id,
        "yearlyId": yearly_id,
        "id": monthly_id
    }), 201


@app.delete("/api/admin/subscriptions")
def delete_subscriptions():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("ids", [])
    if isinstance(raw_ids, (int, str)):
        raw_ids = [raw_ids]

    ids_to_delete = []
    for item in raw_ids:
        try:
            ids_to_delete.append(int(item))
        except (ValueError, TypeError):
            pass

    if not ids_to_delete:
        return jsonify({"message": "No valid subscription IDs provided"}), 400

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                # Find plan_names of deleted subscriptions to check if subscription_plans needs cleanup
                cursor.execute(
                    "SELECT DISTINCT plan_name FROM subscriptions WHERE id = ANY(%s)",
                    (ids_to_delete,),
                )
                affected_plans = [row[0] for row in cursor.fetchall() if row[0]]

                # Delete all rows for the selected plans so no orphaned cycle rows remain
                cursor.execute(
                    "DELETE FROM subscriptions WHERE plan_name = ANY(%s) OR id = ANY(%s) RETURNING id",
                    (affected_plans, ids_to_delete),
                )
                deleted_ids = [row[0] for row in cursor.fetchall()]

                # Clean up subscription_plans for affected plans
                if affected_plans:
                    cursor.execute(
                        "DELETE FROM subscription_plans WHERE plan_name = ANY(%s)",
                        (affected_plans,),
                    )

                # Subscription Audit Log
                admin_actor = request.headers.get("X-User-Email", "Admin")
                for p_name in affected_plans:
                    log_subscription_action(
                        cursor,
                        subscription_id=None,
                        plan_name=p_name,
                        action="DELETED",
                        performed_by=admin_actor,
                        details=f"Subscription plan '{p_name}' and associated billing tiers deleted.",
                        old_values={"planName": p_name}
                    )

                connection.commit()
    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": f"Database is unavailable: {str(error)}"}), 503

    return jsonify({
        "message": f"Successfully deleted {len(deleted_ids)} subscription record(s)",
        "deletedIds": deleted_ids,
    }), 200


@app.post("/api/admin/business-types")
def create_business_type():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    description = str(payload.get("description", "")).strip()
    if not name:
        return jsonify({"message": "Business type name is required"}), 400
    if len(name) > 120 or len(description) > 500:
        return jsonify({"message": "Business type data is too long"}), 400

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO business_types (name, description) VALUES (%s, %s) RETURNING id",
                    (name, description),
                )
                business_type_id = cursor.fetchone()[0]

                # Business Type Audit Log
                admin_actor = request.headers.get("X-User-Email", "Admin")
                log_business_type_action(
                    cursor,
                    business_type_id=business_type_id,
                    name=name,
                    action="CREATED",
                    performed_by=admin_actor,
                    details=f"Business category '{name}' established.",
                    new_values={"name": name, "description": description}
                )

                connection.commit()
    except psycopg.IntegrityError:
        return jsonify({"message": "That business type already exists"}), 409
    except (psycopg.Error, RuntimeError):
        return jsonify({"message": "Database is unavailable"}), 503

    return jsonify({"id": business_type_id, "message": "Business type created successfully"}), 201


@app.put("/api/admin/business-types/<int:business_type_id>")
def update_business_type(business_type_id):
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    description = str(payload.get("description", "")).strip()
    if not name:
        return jsonify({"message": "Business type name is required"}), 400
    if len(name) > 120 or len(description) > 500:
        return jsonify({"message": "Business type data is too long"}), 400

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT name, description FROM business_types WHERE id = %s", (business_type_id,))
                old_row = cursor.fetchone()
                old_name, old_desc = (old_row[0], old_row[1]) if old_row else ("", "")

                cursor.execute(
                    "UPDATE business_types SET name = %s, description = %s, updated_at = NOW() WHERE id = %s",
                    (name, description, business_type_id),
                )
                if cursor.rowcount == 0:
                    return jsonify({"message": "Business type not found"}), 404

                # Business Type Audit Log
                admin_actor = request.headers.get("X-User-Email", "Admin")
                log_business_type_action(
                    cursor,
                    business_type_id=business_type_id,
                    name=name,
                    action="UPDATED",
                    performed_by=admin_actor,
                    details=f"Business category updated from '{old_name}' to '{name}'.",
                    old_values={"name": old_name, "description": old_desc},
                    new_values={"name": name, "description": description}
                )

                connection.commit()
    except psycopg.IntegrityError:
        return jsonify({"message": "That business type already exists"}), 409
    except (psycopg.Error, RuntimeError):
        return jsonify({"message": "Database is unavailable"}), 503

    return jsonify({"message": "Business type updated successfully"})


@app.delete("/api/admin/business-types/<int:business_type_id>")
def delete_business_type(business_type_id):
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT name FROM business_types WHERE id = %s", (business_type_id,))
                old_row = cursor.fetchone()
                old_name = old_row[0] if old_row else f"ID {business_type_id}"

                cursor.execute("DELETE FROM business_types WHERE id = %s", (business_type_id,))
                if cursor.rowcount == 0:
                    return jsonify({"message": "Business type not found"}), 404

                # Business Type Audit Log
                admin_actor = request.headers.get("X-User-Email", "Admin")
                log_business_type_action(
                    cursor,
                    business_type_id=business_type_id,
                    name=old_name,
                    action="DELETED",
                    performed_by=admin_actor,
                    details=f"Business category '{old_name}' deleted.",
                    old_values={"name": old_name}
                )

                connection.commit()
    except (psycopg.Error, RuntimeError):
        return jsonify({"message": "Database is unavailable"}), 503

    return jsonify({"message": "Business type deleted successfully"})


@app.get("/api/admin/business-reports")
def admin_business_reports():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                # 1. Total overview stats
                cursor.execute("""
                    SELECT 
                        COUNT(*) as total_customers,
                        COUNT(CASE WHEN status = 'active' THEN 1 END) as active_customers,
                        COUNT(CASE WHEN status = 'inactive' THEN 1 END) as inactive_customers,
                        COUNT(CASE WHEN status = 'suspended' THEN 1 END) as suspended_customers
                    FROM userdetails
                    WHERE user_type = 'customer'
                """)
                cust_row = cursor.fetchone() or (0, 0, 0, 0)
                total_cust, active_cust, inactive_cust, suspended_cust = cust_row

                # 2. Revenue by active plan
                cursor.execute("""
                    SELECT 
                        u.active_plan,
                        COUNT(u.id) as customer_count,
                        COALESCE(SUM(s.plan_amount), 0) as total_amount,
                        COALESCE(AVG(s.plan_amount), 0) as avg_amount
                    FROM userdetails u
                    LEFT JOIN LATERAL (
                        SELECT plan_amount FROM subscriptions 
                        WHERE LOWER(plan_name) = LOWER(u.active_plan) 
                        ORDER BY id DESC LIMIT 1
                    ) s ON TRUE
                    WHERE u.user_type = 'customer'
                    GROUP BY u.active_plan
                    ORDER BY total_amount DESC, customer_count DESC
                """)
                plan_reports = [{
                    "planName": r[0] or "None",
                    "customers": int(r[1]),
                    "revenue": int(r[2]),
                    "avgAmount": round(float(r[3]), 1)
                } for r in cursor.fetchall()]

                # 3. Revenue by business type (industry sector)
                cursor.execute("""
                    SELECT 
                        u.business_type,
                        COUNT(u.id) as total_customers,
                        COUNT(CASE WHEN u.status = 'active' THEN 1 END) as active_customers,
                        COALESCE(SUM(s.plan_amount), 0) as total_revenue
                    FROM userdetails u
                    LEFT JOIN LATERAL (
                        SELECT plan_amount FROM subscriptions 
                        WHERE LOWER(plan_name) = LOWER(u.active_plan) 
                        ORDER BY id DESC LIMIT 1
                    ) s ON TRUE
                    WHERE u.user_type = 'customer'
                    GROUP BY u.business_type
                    ORDER BY total_revenue DESC, total_customers DESC
                """)
                sector_reports = [{
                    "businessType": r[0] or "Unclassified",
                    "totalCustomers": int(r[1]),
                    "activeCustomers": int(r[2]),
                    "revenue": int(r[3]),
                    "activeRate": round((int(r[2]) / int(r[1]) * 100), 1) if int(r[1]) > 0 else 0
                } for r in cursor.fetchall()]

                # 4. Billing cycle inventory from subscriptions table
                cursor.execute("""
                    SELECT 
                        billing_cycle,
                        COUNT(*) as plan_count,
                        COALESCE(SUM(plan_amount), 0) as sum_amounts,
                        COALESCE(AVG(plan_amount), 0) as avg_amount
                    FROM subscriptions
                    WHERE plan_name IS NOT NULL AND TRIM(plan_name) != ''
                    GROUP BY billing_cycle
                    ORDER BY CASE billing_cycle WHEN 'monthly' THEN 1 WHEN 'quarterly' THEN 2 WHEN 'yearly' THEN 3 ELSE 4 END
                """)
                cycle_reports = [{
                    "billingCycle": r[0],
                    "planCount": int(r[1]),
                    "sumAmounts": int(r[2]),
                    "avgAmount": round(float(r[3]), 1)
                } for r in cursor.fetchall()]

                # 5. Customer signup trends (by month)
                cursor.execute("""
                    SELECT 
                        TO_CHAR(created_at, 'Mon YYYY') as period,
                        DATE_TRUNC('month', created_at) as sort_month,
                        COUNT(*) as signups,
                        COUNT(CASE WHEN status = 'active' THEN 1 END) as active_signups
                    FROM userdetails
                    WHERE user_type = 'customer'
                    GROUP BY period, sort_month
                    ORDER BY sort_month ASC
                    LIMIT 12
                """)
                growth_reports = [{
                    "period": r[0],
                    "signups": int(r[2]),
                    "activeSignups": int(r[3])
                } for r in cursor.fetchall()]

                # Calculate MRR, ARR, ARPU
                total_monthly_revenue = sum(p["revenue"] for p in plan_reports)
                mrr = total_monthly_revenue
                arr = mrr * 12
                arpu = round(total_monthly_revenue / total_cust, 2) if total_cust > 0 else 0
                retention_rate = round((active_cust / total_cust * 100), 1) if total_cust > 0 else 100

                reports = {
                    "kpi": {
                        "totalCustomers": total_cust,
                        "activeCustomers": active_cust,
                        "inactiveCustomers": inactive_cust,
                        "suspendedCustomers": suspended_cust,
                        "mrr": mrr,
                        "arr": arr,
                        "arpu": arpu,
                        "retentionRate": retention_rate
                    },
                    "planReports": plan_reports,
                    "sectorReports": sector_reports,
                    "cycleReports": cycle_reports,
                    "growthReports": growth_reports
                }

    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": f"Database error: {str(error)}"}), 503

    return jsonify({"reports": reports})


@app.get("/api/admin/audit-logs")
def admin_audit_logs():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    entity = request.args.get("entity", "all").strip().lower()

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                customer_logs = []
                subscription_logs = []
                business_type_logs = []

                if entity in ("all", "customer"):
                    cursor.execute("""
                        SELECT id, customer_id, customer_name, action, performed_by, details,
                               old_values, new_values, TO_CHAR(created_at, 'YYYY-MM-DD HH24:MI:SS')
                        FROM customer_logs
                        ORDER BY id DESC
                        LIMIT 200
                    """)
                    customer_logs = [{
                        "id": r[0],
                        "recordId": r[1],
                        "name": r[2] or "Customer",
                        "action": r[3],
                        "performedBy": r[4],
                        "details": r[5],
                        "oldValues": r[6] if isinstance(r[6], dict) else {},
                        "newValues": r[7] if isinstance(r[7], dict) else {},
                        "createdAt": r[8],
                        "category": "Customer"
                    } for r in cursor.fetchall()]

                if entity in ("all", "subscription"):
                    cursor.execute("""
                        SELECT id, subscription_id, plan_name, action, performed_by, details,
                               old_values, new_values, TO_CHAR(created_at, 'YYYY-MM-DD HH24:MI:SS')
                        FROM subscription_logs
                        ORDER BY id DESC
                        LIMIT 200
                    """)
                    subscription_logs = [{
                        "id": r[0],
                        "recordId": r[1],
                        "name": r[2] or "Plan",
                        "action": r[3],
                        "performedBy": r[4],
                        "details": r[5],
                        "oldValues": r[6] if isinstance(r[6], dict) else {},
                        "newValues": r[7] if isinstance(r[7], dict) else {},
                        "createdAt": r[8],
                        "category": "Subscription"
                    } for r in cursor.fetchall()]

                if entity in ("all", "business_type", "businesstype"):
                    cursor.execute("""
                        SELECT id, business_type_id, name, action, performed_by, details,
                               old_values, new_values, TO_CHAR(created_at, 'YYYY-MM-DD HH24:MI:SS')
                        FROM business_type_logs
                        ORDER BY id DESC
                        LIMIT 200
                    """)
                    business_type_logs = [{
                        "id": r[0],
                        "recordId": r[1],
                        "name": r[2] or "Category",
                        "action": r[3],
                        "performedBy": r[4],
                        "details": r[5],
                        "oldValues": r[6] if isinstance(r[6], dict) else {},
                        "newValues": r[7] if isinstance(r[7], dict) else {},
                        "createdAt": r[8],
                        "category": "Business Type"
                    } for r in cursor.fetchall()]

    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": f"Database error: {str(error)}"}), 503

    all_logs = sorted(
        customer_logs + subscription_logs + business_type_logs,
        key=lambda x: x.get("createdAt", ""),
        reverse=True
    )

    return jsonify({
        "allLogs": all_logs,
        "customerLogs": customer_logs,
        "subscriptionLogs": subscription_logs,
        "businessTypeLogs": business_type_logs,
        "stats": {
            "totalCustomerLogs": len(customer_logs),
            "totalSubscriptionLogs": len(subscription_logs),
            "totalBusinessTypeLogs": len(business_type_logs),
            "totalLogs": len(all_logs)
        }
    })


@app.get("/api/admin/notifications")
def admin_notifications():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                # Sync any newly occurring customer or application alerts
                sync_admin_notifications(cursor)
                connection.commit()

                cursor.execute("""
                    SELECT id, category, type, title, message, customer_id, customer_name,
                           is_read, is_resolved, action_url, TO_CHAR(created_at, 'YYYY-MM-DD HH24:MI:SS')
                    FROM admin_notifications
                    ORDER BY id DESC
                    LIMIT 100
                """)
                notifications = [{
                    "id": r[0],
                    "category": r[1],
                    "type": r[2],
                    "title": r[3],
                    "message": r[4],
                    "customerId": r[5],
                    "customerName": r[6],
                    "isRead": bool(r[7]),
                    "isResolved": bool(r[8]),
                    "actionUrl": r[9] or "Dashboard",
                    "createdAt": r[10]
                } for r in cursor.fetchall()]

                unread_count = sum(1 for n in notifications if not n["isRead"])
                customer_issues = sum(1 for n in notifications if n["category"] == "Customer" and not n["isResolved"])
                application_issues = sum(1 for n in notifications if n["category"] == "Application" and not n["isResolved"])

    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": f"Database error: {str(error)}"}), 503

    return jsonify({
        "notifications": notifications,
        "unreadCount": unread_count,
        "customerIssuesCount": customer_issues,
        "applicationIssuesCount": application_issues,
        "totalCount": len(notifications)
    })


@app.post("/api/admin/notifications/<int:notif_id>/read")
def mark_notification_read(notif_id):
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE admin_notifications SET is_read = TRUE WHERE id = %s", (notif_id,))
                connection.commit()
    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": f"Database error: {str(error)}"}), 503

    return jsonify({"message": "Notification marked as read"})


@app.post("/api/admin/notifications/mark-all-read")
def mark_all_notifications_read():
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE admin_notifications SET is_read = TRUE WHERE is_read = FALSE")
                connection.commit()
    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": f"Database error: {str(error)}"}), 503

    return jsonify({"message": "All notifications marked as read"})


@app.post("/api/admin/notifications/<int:notif_id>/resolve")
def resolve_notification(notif_id):
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE admin_notifications SET is_resolved = TRUE, is_read = TRUE WHERE id = %s",
                    (notif_id,)
                )
                connection.commit()
    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": f"Database error: {str(error)}"}), 503

    return jsonify({"message": "Notification marked as resolved"})


@app.delete("/api/admin/notifications/<int:notif_id>")
def delete_notification(notif_id):
    if not require_admin_access():
        return jsonify({"message": "Admin access required"}), 403

    try:
        with psycopg.connect(get_database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM admin_notifications WHERE id = %s", (notif_id,))
                connection.commit()
    except (psycopg.Error, RuntimeError) as error:
        return jsonify({"message": f"Database error: {str(error)}"}), 503

    return jsonify({"message": "Notification dismissed"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")


