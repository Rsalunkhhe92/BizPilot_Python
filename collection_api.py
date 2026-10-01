"""
Collection Business REST API Endpoints (Flask Blueprint)
Mounted at /api/collection
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os

from flask import Blueprint, jsonify, request
import psycopg

from collection_service import (
    ensure_collection_schema,
    generate_receipt_number,
    is_day_closed,
    recalculate_daily_summary,
    seed_collection_demo_data,
)

collection_bp = Blueprint("collection", __name__, url_prefix="/api/collection")

# Indian Standard Time (IST, UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(database_url, options="-c timezone=Asia/Kolkata")


def parse_date(date_str):
    if not date_str:
        return date.today()
    s = str(date_str).strip()
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return date.today()


def format_relative_time(dt_val):
    if not dt_val:
        return "Today"
    try:
        if isinstance(dt_val, str):
            dt = datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
        else:
            dt = dt_val
        if dt.tzinfo:
            dt = dt.astimezone(IST)
        else:
            dt = dt.replace(tzinfo=IST)
        now = datetime.now(IST)
        diff = now - dt
        secs = diff.total_seconds()
        if secs < 180:
            return "Just now"
        elif secs < 3600:
            mins = max(1, int(secs // 60))
            return f"{mins} mins ago"
        elif dt.date() == now.date():
            return "Today"
        elif dt.date() == (now.date() - timedelta(days=1)):
            return "Yesterday"
        elif diff.days < 7:
            return f"{diff.days} days ago"
        else:
            return dt.strftime("%d %b %Y")
    except Exception:
        return "Today"


def get_active_collector_id(cursor, collector_param=None):
    """
    Resolve collector_id with strict role-based data isolation:
    - If the requester is an agent (X-User-Type != 'admin' or X-User-Email / X-User-Id of non-admin),
      they are STRICTLY LOCKED to their own collector_id. No other agent's account can be viewed.
    - If the requester is an admin (user_type == 'admin'), they can view any collector via collector_param.
    - Fallback: Riya N (id: 36) or first collection user.
    """
    user_email = request.headers.get("X-User-Email", "").strip().lower()
    user_type = request.headers.get("X-User-Type", "").strip().lower()
    user_id = request.headers.get("X-User-Id", "").strip()

    if cursor is not None:
        # Check if caller is non-admin agent via header
        if user_email and user_type != "admin":
            cursor.execute("SELECT id, user_type FROM userdetails WHERE LOWER(email) = %s", (user_email,))
            row = cursor.fetchone()
            if row and row[1] != "admin":
                return row[0]

        if user_id and user_type != "admin":
            try:
                uid = int(user_id)
                cursor.execute("SELECT id, user_type FROM userdetails WHERE id = %s", (uid,))
                row = cursor.fetchone()
                if row and row[1] != "admin":
                    return row[0]
            except (ValueError, TypeError):
                pass

        if collector_param:
            try:
                cid = int(collector_param)
                cursor.execute("SELECT id FROM userdetails WHERE id = %s", (cid,))
                if cursor.fetchone():
                    return cid
            except (ValueError, TypeError):
                pass

        # Fallback to Riya N or first collection user
        cursor.execute("SELECT id FROM userdetails WHERE LOWER(email) = 'riya@gmail.com' LIMIT 1")
        row = cursor.fetchone()
        if row:
            return row[0]

        cursor.execute("SELECT id FROM userdetails WHERE business_type = 'Collection' LIMIT 1")
        row = cursor.fetchone()
        return row[0] if row else 1

    # If cursor is None, resolve safely
    if collector_param:
        try:
            return int(collector_param)
        except (ValueError, TypeError):
            pass
    return 36


# -------------------------------------------------------------------------
# 1. DASHBOARD
# -------------------------------------------------------------------------
@collection_bp.get("/dashboard")
def get_dashboard():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            target_date = parse_date(request.args.get("date"))
            collector_id = get_active_collector_id(cur, request.args.get("collector_id"))

            summary = recalculate_daily_summary(cur, collector_id, target_date)
            conn.commit()

            # Check daily closing status
            cur.execute(
                """
                SELECT id, is_closed, TO_CHAR(closed_at, 'YYYY-MM-DD HH24:MI:SS'), closed_by, handover_notes
                FROM collection_daily_closing
                WHERE closing_date = %s AND collector_id = %s
                """,
                (target_date, collector_id),
            )
            closing_row = cur.fetchone()
            closing_info = {
                "isClosed": bool(closing_row and closing_row[1]),
                "closingId": closing_row[0] if closing_row else None,
                "closedAt": closing_row[2] if closing_row else None,
                "closedBy": closing_row[3] if closing_row else None,
                "handoverNotes": closing_row[4] if closing_row else "",
            }

            # Collector details
            cur.execute("SELECT id, full_name, email, business_type FROM userdetails WHERE id = %s", (collector_id,))
            col_user = cur.fetchone()
            collector_info = {
                "id": col_user[0] if col_user else collector_id,
                "fullName": col_user[1] if col_user else "Field Collector",
                "email": col_user[2] if col_user else "",
                "businessType": col_user[3] if col_user else "Collection",
            }

            # Route wise distribution for today
            cur.execute(
                """
                SELECT COALESCE(r.name, 'Unassigned Route'), COUNT(s.id),
                       COUNT(CASE WHEN s.status = 'COLLECTED' THEN 1 END),
                       COALESCE(SUM(s.expected_amount), 0),
                       COALESCE(SUM(s.collected_amount), 0)
                FROM collection_schedule s
                LEFT JOIN collection_routes r ON s.route_id = r.id
                WHERE s.schedule_date = %s AND s.collector_id = %s
                GROUP BY r.name
                ORDER BY r.name ASC
                """,
                (target_date, collector_id),
            )
            routes_summary = [
                {
                    "routeName": row[0],
                    "total": row[1],
                    "collected": row[2],
                    "expectedAmount": float(row[3]),
                    "collectedAmount": float(row[4]),
                }
                for row in cur.fetchall()
            ]

            return jsonify({
                "date": str(target_date),
                "summary": summary,
                "closing": closing_info,
                "collector": collector_info,
                "routes": routes_summary,
            })


# -------------------------------------------------------------------------
# 1B. DASHBOARD ANALYTICS WITH MULTI-PERIOD FILTERING (DATE, MONTH, QUARTER, YEAR)
# -------------------------------------------------------------------------
@collection_bp.get("/dashboard-analytics")
def get_dashboard_analytics():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            filter_type = str(request.args.get("filter_type", "date")).strip().lower()
            collector_id = get_active_collector_id(cur, request.args.get("collector_id"))
            sub_filter = str(request.args.get("sub_filter", "ALL")).strip().upper()
            search = str(request.args.get("search", "")).strip().lower()

            import calendar

            # 1. Resolve date range & display labels based on filter_type
            if filter_type == "monthly":
                month_param = str(request.args.get("month", "2026-09")).strip()
                if month_param in ("last_month", "last", "prev"):
                    yr, mo = 2026, 8
                else:
                    try:
                        p = month_param.split("-")
                        yr, mo = int(p[0]), int(p[1])
                    except Exception:
                        yr, mo = 2026, 9
                start_date = date(yr, mo, 1)
                _, last_day = calendar.monthrange(yr, mo)
                end_date = date(yr, mo, last_day)
                period_label = start_date.strftime("%B %Y")
                period_sublabel = f"Monthly View ({start_date.strftime('%b %Y')})"

            elif filter_type == "quarterly":
                quarter_param = str(request.args.get("quarter", "Q3-2026")).strip().upper()
                try:
                    parts = quarter_param.split("-")
                    q_num = int(parts[0].replace("Q", ""))
                    q_year = int(parts[1])
                except Exception:
                    q_num, q_year = 3, 2026
                if q_num == 1:
                    start_date = date(q_year, 1, 1)
                    end_date = date(q_year, 3, 31)
                elif q_num == 2:
                    start_date = date(q_year, 4, 1)
                    end_date = date(q_year, 6, 30)
                elif q_num == 3:
                    start_date = date(q_year, 7, 1)
                    end_date = date(q_year, 9, 30)
                else:
                    start_date = date(q_year, 10, 1)
                    end_date = date(q_year, 12, 31)
                period_label = f"Q{q_num} {q_year}"
                period_sublabel = f"{start_date.strftime('%b')} - {end_date.strftime('%b')} {q_year}"

            elif filter_type == "yearly":
                try:
                    y_int = int(request.args.get("year", "2026"))
                except Exception:
                    y_int = 2026
                start_date = date(y_int, 1, 1)
                end_date = date(y_int, 12, 31)
                period_label = f"Year {y_int}"
                period_sublabel = f"Jan - Dec {y_int}"

            elif filter_type == "all":
                # No date restriction - every record this collector has, regardless of when.
                start_date = date(2000, 1, 1)
                end_date = date(2100, 12, 31)
                period_label = "All Records"
                period_sublabel = "Every collection record on file"

            else:  # 'date'
                date_param = request.args.get("date", "")
                target_d = parse_date(date_param) if date_param else date.today()
                start_date = target_d
                end_date = target_d
                period_label = "Today's Target" if target_d == date.today() else f"Target for {target_d.strftime('%d %b')}"
                period_sublabel = target_d.strftime("%A, %d %B %Y")

            # 2. Query target & people stats from collection_schedule in [start_date, end_date]
            cur.execute(
                """
                SELECT 
                    COUNT(DISTINCT s.customer_id) as total_people,
                    COUNT(DISTINCT CASE WHEN s.status = 'COLLECTED' THEN s.customer_id END) as collected_people,
                    COUNT(DISTINCT CASE WHEN s.status = 'PENDING' THEN s.customer_id END) as pending_people,
                    COUNT(DISTINCT CASE WHEN s.status IN ('MISSED', 'NOT_AVAILABLE', 'REFUSED', 'RESCHEDULED') THEN s.customer_id END) as missed_people,
                    COALESCE(SUM(s.expected_amount), 0) as total_target,
                    COALESCE(SUM(s.collected_amount), 0) as total_collected
                FROM collection_schedule s
                WHERE s.schedule_date BETWEEN %s AND %s AND s.collector_id = %s
                """,
                (start_date, end_date, collector_id),
            )
            s_row = cur.fetchone()
            tot_people = s_row[0] or 0
            col_people = s_row[1] or 0
            pen_people = s_row[2] or 0
            mis_people = s_row[3] or 0
            target_amount = float(s_row[4] or 0)
            collected_amount = float(s_row[5] or 0)

            # 3. Query Payment Method breakdown from payment_collection - NET per method
            # (Cash In minus Cash Out), not the raw Cash In total, so a returned advance
            # is reflected here too.
            cur.execute(
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
                WHERE collector_id = %s AND DATE(payment_date) BETWEEN %s AND %s AND status = 'SUCCESS'
                """,
                (collector_id, start_date, end_date),
            )
            p_row = cur.fetchone()
            cash_amount = max(0.0, float(p_row[0] or 0))
            upi_amount = max(0.0, float(p_row[1] or 0))
            bank_amount = max(0.0, float(p_row[2] or 0))
            sum_payments = cash_amount + upi_amount + bank_amount

            # Keep collectedAmount synchronized with net payments (both are net figures now)
            collected_amount = max(collected_amount, sum_payments)
            remaining_amount = max(0.0, target_amount - collected_amount)

            # 4. Count stats for quick buttons
            cur.execute(
                """
                SELECT COUNT(*) FROM payment_collection 
                WHERE collector_id = %s AND DATE(payment_date) BETWEEN %s AND %s AND status = 'SUCCESS'
                """,
                (collector_id, start_date, end_date),
            )
            history_count = cur.fetchone()[0] or 0

            cur.execute(
                """
                SELECT COUNT(DISTINCT summary_date) FROM daily_collection_summary
                WHERE collector_id = %s AND summary_date BETWEEN %s AND %s
                """,
                (collector_id, start_date, end_date),
            )
            daily_report_count = cur.fetchone()[0] or 0

            # 5. Customer Details or Daily Reports based on sub_filter
            customer_items = []
            daily_records = []

            if sub_filter == "DAILY_REPORT":
                recalculate_daily_summary(cur, collector_id, start_date)
                conn.commit()
                cur.execute(
                    """
                    SELECT s.summary_date,
                           s.total_assigned, s.total_collected, s.total_pending,
                           (s.total_missed + s.total_not_available + s.total_refused + s.total_rescheduled) as total_missed,
                           s.total_expected_amount, s.total_collected_amount, s.total_remaining_amount,
                           s.cash_amount, s.upi_amount, s.bank_amount,
                           COALESCE(c.is_closed, FALSE)
                    FROM daily_collection_summary s
                    LEFT JOIN collection_daily_closing c ON s.summary_date = c.closing_date AND s.collector_id = c.collector_id
                    WHERE s.summary_date BETWEEN %s AND %s AND s.collector_id = %s
                    ORDER BY s.summary_date DESC
                    """,
                    (start_date, end_date, collector_id),
                )
                for r in cur.fetchall():
                    exp_d = float(r[5] or 0)
                    col_d = float(r[6] or 0)
                    rate_d = round((col_d / exp_d * 100) if exp_d > 0 else 0, 1)
                    daily_records.append({
                        "date": str(r[0]),
                        "displayDate": r[0].strftime("%d %b %Y"),
                        "totalAssigned": r[1],
                        "people": r[1],
                        "collectedCount": r[2],
                        "collectedPeople": r[2],
                        "pendingCount": r[3],
                        "missedCount": r[4],
                        "expectedAmount": exp_d,
                        "target": exp_d,
                        "collectedAmount": col_d,
                        "collected": col_d,
                        "remainingAmount": float(r[7] or 0),
                        "remaining": float(r[7] or 0),
                        "cashAmount": float(r[8] or 0),
                        "cash": float(r[8] or 0),
                        "upiAmount": float(r[9] or 0),
                        "upi": float(r[9] or 0),
                        "bankAmount": float(r[10] or 0),
                        "bank": float(r[10] or 0),
                        "completionRate": rate_d,
                        "isClosed": r[11],
                    })

            elif sub_filter == "HISTORY":
                h_query = """
                    SELECT p.id, p.amount, p.payment_method, p.transaction_ref,
                           TO_CHAR(p.payment_date AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD"T"HH24:MI:SS+05:30'),
                           pr.receipt_number, c.id, c.name, c.mobile, c.account_number,
                           c.address, c.area, p.notes
                    FROM payment_collection p
                    JOIN collection_customers c ON p.customer_id = c.id
                    LEFT JOIN payment_receipts pr ON p.id = pr.payment_id
                    WHERE p.collector_id = %s AND DATE(p.payment_date) BETWEEN %s AND %s AND p.status = 'SUCCESS'
                """
                h_params = [collector_id, start_date, end_date]
                if search:
                    h_query += " AND (LOWER(c.name) LIKE %s OR c.mobile LIKE %s OR LOWER(c.account_number) LIKE %s)"
                    h_params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
                h_query += " ORDER BY p.payment_date DESC, p.id DESC"
                cur.execute(h_query, tuple(h_params))
                for r in cur.fetchall():
                    customer_items.append({
                        "id": f"pay-{r[0]}",
                        "paymentId": r[0],
                        "customerId": r[6],
                        "customerName": r[7],
                        "mobile": r[8],
                        "accountNumber": r[9],
                        "address": r[10],
                        "area": r[11] or "",
                        "collectedAmount": float(r[1] or 0),
                        "expectedAmount": float(r[1] or 0),
                        "paymentMethod": r[2],
                        "transactionRef": r[3] or "",
                        "date": r[4],
                        "receiptNumber": r[5],
                        "status": "COLLECTED",
                        "notes": r[12] or "",
                    })

            else:
                # ALL, COLLECTED, MISSED, PENDING
                c_query = """
                    SELECT 
                        s.id AS schedule_id,
                        s.schedule_date,
                        s.expected_amount,
                        s.collected_amount,
                        s.status,
                        s.notes,
                        c.id AS customer_id,
                        c.name,
                        c.mobile,
                        c.address,
                        c.area,
                        c.account_number,
                        d.total_due,
                        p.receipt_number,
                        p.payment_method,
                        p.payment_date,
                        cr.reason,
                        cr.notes as record_notes,
                        GREATEST(COALESCE(s.updated_at, s.created_at, NOW()), COALESCE(c.updated_at, c.created_at, NOW())) AS last_updated_time
                    FROM collection_schedule s
                    JOIN collection_customers c ON s.customer_id = c.id
                    LEFT JOIN due_amount d ON c.id = d.customer_id
                    LEFT JOIN LATERAL (
                        SELECT pr.receipt_number, pc.payment_method, TO_CHAR(pc.payment_date AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD"T"HH24:MI:SS+05:30') as payment_date
                        FROM payment_collection pc
                        LEFT JOIN payment_receipts pr ON pc.id = pr.payment_id
                        WHERE pc.schedule_id = s.id AND pc.status = 'SUCCESS'
                        ORDER BY pc.id DESC
                        LIMIT 1
                    ) p ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT reason, notes
                        FROM collection_records
                        WHERE schedule_id = s.id
                        ORDER BY id DESC
                        LIMIT 1
                    ) cr ON TRUE
                    WHERE s.schedule_date BETWEEN %s AND %s AND s.collector_id = %s
                """
                c_params = [start_date, end_date, collector_id]

                if sub_filter == "COLLECTED":
                    c_query += " AND s.status = 'COLLECTED'"
                elif sub_filter == "MISSED":
                    c_query += " AND s.status IN ('MISSED', 'NOT_AVAILABLE', 'REFUSED', 'RESCHEDULED')"
                elif sub_filter == "PENDING":
                    c_query += " AND s.status = 'PENDING'"

                if search:
                    c_query += " AND (LOWER(c.name) LIKE %s OR c.mobile LIKE %s OR LOWER(c.account_number) LIKE %s OR LOWER(c.address) LIKE %s)"
                    c_params.extend([f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%"])

                c_query += " ORDER BY GREATEST(COALESCE(s.updated_at, s.created_at), COALESCE(c.updated_at, c.created_at)) DESC, s.schedule_date DESC, s.id DESC, s.route_order DESC"
                cur.execute(c_query, tuple(c_params))
                for r in cur.fetchall():
                    customer_items.append({
                        "scheduleId": r[0],
                        "id": r[0],
                        "scheduleDate": str(r[1]),
                        "expectedAmount": float(r[2] or 0),
                        "collectedAmount": float(r[3] or 0),
                        "status": r[4],
                        "notes": r[5] or "",
                        "customerId": r[6],
                        "customerName": r[7],
                        "name": r[7],
                        "mobile": r[8],
                        "phone": r[8],
                        "address": r[9],
                        "area": r[10] or "",
                        "accountNumber": r[11],
                        "totalDue": float(r[12] or 0),
                        "receiptNumber": r[13],
                        "paymentMethod": r[14],
                        "paymentDate": r[15],
                        "missedReason": r[16] or "",
                        "recordNotes": r[17] or "",
                        "updatedAt": str(r[18]) if r[18] else None,
                        "updated_at": str(r[18]) if r[18] else None,
                        "lastUpdated": format_relative_time(r[18]),
                        "last_updated": format_relative_time(r[18]),
                    })

            return jsonify({
                "filterType": filter_type,
                "periodLabel": period_label,
                "periodSublabel": period_sublabel,
                "startDate": str(start_date),
                "endDate": str(end_date),
                "subFilter": sub_filter,
                "metrics": {
                    "targetAmount": target_amount,
                    "collectedAmount": collected_amount,
                    "remainingAmount": remaining_amount,
                    "collectionPercentage": round((collected_amount / target_amount * 100) if target_amount > 0 else 0, 1),
                    "people": {
                        "total": tot_people,
                        "collected": col_people,
                        "pending": pen_people,
                        "missed": mis_people,
                    },
                    "paymentMethods": {
                        "cash": cash_amount,
                        "upi": upi_amount,
                        "bankTransfer": bank_amount,
                    },
                },
                "quickCounts": {
                    "collected": col_people,
                    "missed": mis_people,
                    "pending": pen_people,
                    "history": history_count,
                    "dailyReports": daily_report_count or len(daily_records),
                    "totalPeople": tot_people,
                },
                "customers": customer_items,
                "dailyReports": daily_records,
            })


# -------------------------------------------------------------------------
# 2. TODAY'S SCHEDULED CUSTOMERS
# -------------------------------------------------------------------------
@collection_bp.get("/today")
def get_today_schedule():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            target_date = parse_date(request.args.get("date"))
            collector_id = get_active_collector_id(cur, request.args.get("collector_id"))
            status_filter = request.args.get("status", "").strip().upper()
            search = request.args.get("search", "").strip().lower()
            route_id = request.args.get("route_id", "").strip()

            # Auto-seed the beat: every active customer for this collector should have a
            # schedule row for every day from their creation date through today, not just
            # today. If the app wasn't opened for a few days, those days would otherwise get
            # no row at all - silently erasing that backlog from any "total pending"
            # calculation, since there'd be no row to sum. Gaps can appear anywhere (not only
            # after the most recent row), so this always regenerates the full date range and
            # relies on ON CONFLICT DO NOTHING to skip days that already have a row.
            cur.execute(
                """
                INSERT INTO collection_schedule (schedule_date, collector_id, customer_id, expected_amount, status)
                SELECT gs.d::date, c.collector_id, c.id, COALESCE(d.expected_amount, 0), 'PENDING'
                FROM collection_customers c
                LEFT JOIN due_amount d ON d.customer_id = c.id
                CROSS JOIN LATERAL generate_series(c.created_at::date, %s, interval '1 day') AS gs(d)
                WHERE c.collector_id = %s AND c.status = 'active'
                ON CONFLICT (schedule_date, collector_id, customer_id) DO NOTHING
                """,
                (target_date, collector_id),
            )

            query = """
                SELECT 
                    s.id AS schedule_id,
                    s.schedule_date,
                    s.route_order,
                    s.expected_amount,
                    s.collected_amount,
                    s.status,
                    s.notes,
                    c.id AS customer_id,
                    c.name,
                    c.mobile,
                    c.address,
                    c.area,
                    c.account_number,
                    r.id AS route_id,
                    r.name AS route_name,
                    d.total_due,
                    d.emi_amount,
                    d.last_payment_date,
                    p.receipt_number,
                    p.payment_method,
                    GREATEST(COALESCE(s.updated_at, s.created_at, NOW()), COALESCE(c.updated_at, c.created_at, NOW())) AS last_updated_time,
                    COALESCE(pend.total_pending, 0) AS total_pending_amount,
                    COALESCE(pend.overdue_days, 0) AS overdue_days,
                    COALESCE(todaypay.today_net_paid, 0) AS today_net_paid,
                    COALESCE(d.interest_rate, 0) AS interest_rate
                FROM collection_schedule s
                JOIN collection_customers c ON s.customer_id = c.id
                LEFT JOIN collection_routes r ON s.route_id = r.id
                LEFT JOIN due_amount d ON c.id = d.customer_id
                LEFT JOIN LATERAL (
                    SELECT pr.receipt_number, pc.payment_method
                    FROM payment_collection pc
                    JOIN payment_receipts pr ON pc.id = pr.payment_id
                    WHERE pc.schedule_id = s.id
                    ORDER BY pc.id DESC
                    LIMIT 1
                ) p ON TRUE
                LEFT JOIN LATERAL (
                    -- Total unpaid amount across today AND every earlier missed/pending day,
                    -- so an agent can see the real amount owed, not just today's target.
                    -- overdue_days counts only the earlier (not today) unpaid days.
                    SELECT
                        SUM(GREATEST(s2.expected_amount - s2.collected_amount, 0)) AS total_pending,
                        COUNT(*) FILTER (WHERE s2.schedule_date < %s AND s2.expected_amount > s2.collected_amount) AS overdue_days
                    FROM collection_schedule s2
                    WHERE s2.customer_id = c.id AND s2.collector_id = s.collector_id AND s2.schedule_date <= %s
                ) pend ON TRUE
                LEFT JOIN LATERAL (
                    -- Actual net cash received from this customer today (Cash In minus Cash
                    -- Out), independent of how the schedule bookkeeping allocated it. Once a
                    -- customer's whole backlog is cleared, an extra payment has no schedule
                    -- row left to attach to, so collected_amount alone would under-report what
                    -- was really paid today.
                    SELECT COALESCE(SUM(CASE WHEN pc.entry_type = 'IN' THEN pc.amount ELSE -pc.amount END), 0) AS today_net_paid
                    FROM payment_collection pc
                    WHERE pc.customer_id = c.id AND pc.collector_id = s.collector_id
                      AND DATE(pc.payment_date) = %s AND pc.status = 'SUCCESS'
                ) todaypay ON TRUE
                WHERE s.schedule_date = %s AND s.collector_id = %s
            """
            params = [target_date, target_date, target_date, target_date, collector_id]

            if status_filter and status_filter != "ALL":
                query += " AND s.status = %s"
                params.append(status_filter)

            if route_id:
                query += " AND s.route_id = %s"
                params.append(route_id)

            if search:
                query += " AND (LOWER(c.name) LIKE %s OR c.mobile LIKE %s OR LOWER(c.account_number) LIKE %s OR LOWER(c.address) LIKE %s)"
                s_param = f"%{search}%"
                params.extend([s_param, s_param, s_param, s_param])

            query += " ORDER BY GREATEST(COALESCE(s.updated_at, s.created_at), COALESCE(c.updated_at, c.created_at)) DESC, s.id DESC, s.route_order DESC"

            cur.execute(query, tuple(params))
            items = []
            for row in cur.fetchall():
                items.append({
                    "id": row[0],
                    "scheduleId": row[0],
                    "schedule_id": row[0],
                    "scheduleDate": str(row[1]),
                    "schedule_date": str(row[1]),
                    "routeOrder": row[2],
                    "route_order": row[2],
                    "visitSequence": row[2],
                    "visit_sequence": row[2],
                    "expectedAmount": float(row[3]),
                    "expected_amount": float(row[3]),
                    "collectedAmount": float(row[4]),
                    "collected_amount": float(row[4]),
                    "status": row[5],
                    "notes": row[6] or "",
                    "customerId": row[7],
                    "customer_id": row[7],
                    "customerName": row[8],
                    "customer_name": row[8],
                    "name": row[8],
                    "mobile": row[9],
                    "phone": row[9],
                    "address": row[10],
                    "area": row[11] or "",
                    "area_name": row[11] or "",
                    "accountNumber": row[12],
                    "account_number": row[12],
                    "routeId": row[13],
                    "route_id": row[13],
                    "routeName": row[14] or "Standard Area",
                    "route_name": row[14] or "Standard Area",
                    "totalDue": float(row[15] or 0),
                    "total_due": float(row[15] or 0),
                    "emiAmount": float(row[16] or 0),
                    "installment_amount": float(row[16] or 0),
                    "lastPaymentDate": str(row[17]) if row[17] else None,
                    "receiptNumber": row[18] or None,
                    "receipt_number": row[18] or None,
                    "paymentMethod": row[19] or None,
                    "payment_method": row[19] or None,
                    "updatedAt": str(row[20]) if row[20] else None,
                    "updated_at": str(row[20]) if row[20] else None,
                    "lastUpdated": format_relative_time(row[20]),
                    "last_updated": format_relative_time(row[20]),
                    "pendingAmount": float(row[21] or 0),
                    "pending_amount": float(row[21] or 0),
                    "overdueDays": int(row[22] or 0),
                    "overdue_days_count": int(row[22] or 0),
                    "todayCollectedAmount": float(row[23] or 0),
                    "today_collected_amount": float(row[23] or 0),
                    "interestRate": float(row[24] or 0),
                    "interest_rate": float(row[24] or 0),
                })

            summary = recalculate_daily_summary(cur, collector_id, target_date)
            conn.commit()

            return jsonify({
                "date": str(target_date),
                "collectorId": collector_id,
                "total": len(items),
                "customers": items,
                "schedule": items,
                "summary": summary,
            })


# -------------------------------------------------------------------------
# 3. CUSTOMER MANAGEMENT
# -------------------------------------------------------------------------
@collection_bp.get("/customers")
def get_customers():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            search = request.args.get("search", "").strip().lower()
            route_id = request.args.get("route_id", "").strip()
            collector_id = request.args.get("collector_id")

            # Enforce strict agent isolation: If non-admin agent is calling, lock to their collector_id
            user_email = request.headers.get("X-User-Email", "").strip().lower()
            user_type = request.headers.get("X-User-Type", "").strip().lower()
            if user_email and user_type != "admin":
                cur.execute("SELECT id, user_type FROM userdetails WHERE LOWER(email) = %s", (user_email,))
                urow = cur.fetchone()
                if urow and urow[1] != "admin":
                    collector_id = str(urow[0])

            query = """
                SELECT c.id, c.account_number, c.name, c.mobile, c.address, c.area, c.status,
                       r.id, r.name,
                       d.total_due, d.expected_amount, d.emi_amount, d.last_payment_date,
                       u.id, u.full_name
                FROM collection_customers c
                LEFT JOIN collection_routes r ON c.route_id = r.id
                LEFT JOIN due_amount d ON c.id = d.customer_id
                LEFT JOIN userdetails u ON c.collector_id = u.id
                WHERE 1=1
            """
            params = []

            if collector_id:
                query += " AND c.collector_id = %s"
                params.append(collector_id)

            if route_id:
                query += " AND c.route_id = %s"
                params.append(route_id)

            if search:
                query += " AND (LOWER(c.name) LIKE %s OR c.mobile LIKE %s OR LOWER(c.account_number) LIKE %s)"
                s = f"%{search}%"
                params.extend([s, s, s])

            query += " ORDER BY c.id DESC, c.name ASC"
            cur.execute(query, tuple(params))

            customers = []
            for r in cur.fetchall():
                customers.append({
                    "id": r[0],
                    "accountNumber": r[1],
                    "account_number": r[1],
                    "name": r[2],
                    "full_name": r[2],
                    "mobile": r[3],
                    "phone": r[3],
                    "address": r[4],
                    "area": r[5] or "",
                    "area_name": r[5] or "",
                    "status": r[6],
                    "routeId": r[7],
                    "route_id": r[7],
                    "routeName": r[8] or "No Route",
                    "route_name": r[8] or "No Route",
                    "totalDue": float(r[9] or 0),
                    "total_due": float(r[9] or 0),
                    "expectedAmount": float(r[10] or 0),
                    "expected_amount": float(r[10] or 0),
                    "emiAmount": float(r[11] or 0),
                    "installment_amount": float(r[11] or 0),
                    "lastPaymentDate": str(r[12]) if r[12] else None,
                    "collectorId": r[13],
                    "collector_id": r[13],
                    "collectorName": r[14] or "Unassigned",
                    "overdue_days": 14,
                })
            return jsonify({"customers": customers, "total": len(customers)})


@collection_bp.post("/customers")
def create_customer():
    payload = request.get_json(silent=True) or {}
    name = payload.get("name", "").strip()
    mobile = payload.get("mobile", "").strip()
    address = payload.get("address", "").strip() or "Local"
    area = payload.get("area", "").strip() or "Main Area"
    route_id = payload.get("routeId") or payload.get("route_id")
    collector_id = payload.get("collectorId") or payload.get("collector_id")
    expected_amount = Decimal(str(payload.get("expectedAmount", payload.get("expected_amount", 1000))))
    total_due = Decimal(str(payload.get("totalDue", payload.get("total_due", 4000))))
    interest_rate = Decimal(str(payload.get("interestRate", payload.get("interest_rate", 0)) or 0))

    if not name or not mobile:
        return jsonify({"message": "Name and mobile number are required."}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Auto-assign collector if not provided
            if not collector_id:
                collector_id = get_active_collector_id(cur)

            # Generate account number
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM collection_customers")
            next_idx = cur.fetchone()[0] + 1
            account_number = f"ACC-2026-{next_idx:04d}"

            cur.execute(
                """
                INSERT INTO collection_customers (collector_id, route_id, account_number, name, mobile, address, area, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')
                RETURNING id;
                """,
                (collector_id, route_id, account_number, name, mobile, address, area),
            )
            customer_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO due_amount (customer_id, total_due, expected_amount, emi_amount, interest_rate, last_payment_date)
                VALUES (%s, %s, %s, %s, %s, NULL)
                ON CONFLICT (customer_id) DO UPDATE SET
                    total_due = EXCLUDED.total_due,
                    expected_amount = EXCLUDED.expected_amount,
                    emi_amount = EXCLUDED.emi_amount,
                    interest_rate = EXCLUDED.interest_rate;
                """,
                (customer_id, total_due, expected_amount, expected_amount, interest_rate),
            )

            # Auto-schedule for today's collection beat
            today = date.today()
            cur.execute("SELECT COALESCE(MAX(route_order), 0) FROM collection_schedule WHERE schedule_date = %s AND collector_id = %s", (today, collector_id))
            max_order = cur.fetchone()[0] or 0

            cur.execute(
                """
                INSERT INTO collection_schedule (schedule_date, collector_id, customer_id, route_id, route_order, expected_amount, collected_amount, status, notes)
                VALUES (%s, %s, %s, %s, %s, %s, 0.00, 'PENDING', 'Newly added customer')
                ON CONFLICT (schedule_date, collector_id, customer_id) DO NOTHING
                RETURNING id;
                """,
                (today, collector_id, customer_id, route_id, max_order + 1, expected_amount),
            )
            sched_res = cur.fetchone()
            schedule_id = sched_res[0] if sched_res else None

            conn.commit()

            return jsonify({
                "message": "Customer created successfully and added to today's collection beat.",
                "customer": {
                    "id": customer_id,
                    "scheduleId": schedule_id,
                    "accountNumber": account_number,
                    "name": name,
                    "mobile": mobile,
                    "address": address,
                    "area": area,
                    "totalDue": float(total_due),
                    "expectedAmount": float(expected_amount),
                    "collectorId": collector_id,
                },
            }), 201


@collection_bp.put("/customers/<int:customer_id>")
def update_customer(customer_id):
    payload = request.get_json(silent=True) or {}
    name = payload.get("name", "").strip()
    mobile = payload.get("mobile", "").strip()
    address = payload.get("address", "").strip()
    area = payload.get("area", "").strip()
    expected_amount = payload.get("expectedAmount", payload.get("expected_amount"))
    total_due = payload.get("totalDue", payload.get("total_due"))
    interest_rate = payload.get("interestRate", payload.get("interest_rate"))
    status = payload.get("status", "active")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, collector_id, name, mobile, address, area, status FROM collection_customers WHERE id = %s", (customer_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"message": "Customer not found."}), 404

            active_cid = get_active_collector_id(cur)
            user_type = request.headers.get("X-User-Type", "").strip().lower()
            if user_type != "admin" and row[1] and row[1] != active_cid:
                return jsonify({"message": "Unauthorized to modify another collector's customer."}), 403

            new_name = name or row[2]
            new_mobile = mobile or row[3]
            new_address = address if address else (row[4] or "")
            new_area = area if area else (row[5] or "")
            new_status = status if status in ("active", "inactive") else (row[6] or "active")

            cur.execute(
                """
                UPDATE collection_customers
                SET name = %s, mobile = %s, address = %s, area = %s, status = %s, updated_at = NOW()
                WHERE id = %s;
                """,
                (new_name, new_mobile, new_address, new_area, new_status, customer_id),
            )

            # Ensure collection_schedule reflects recent update immediately
            cur.execute(
                """
                UPDATE collection_schedule
                SET updated_at = NOW()
                WHERE customer_id = %s;
                """,
                (customer_id,),
            )

            if expected_amount is not None or total_due is not None or interest_rate is not None:
                cur.execute("SELECT total_due, expected_amount, interest_rate FROM due_amount WHERE customer_id = %s", (customer_id,))
                due_row = cur.fetchone()
                curr_due = due_row[0] if due_row else Decimal("0.00")
                curr_exp = due_row[1] if due_row else Decimal("0.00")
                curr_rate = due_row[2] if due_row else Decimal("0.00")

                upd_due = Decimal(str(total_due)) if total_due is not None else curr_due
                upd_exp = Decimal(str(expected_amount)) if expected_amount is not None else curr_exp
                upd_rate = Decimal(str(interest_rate)) if interest_rate is not None else curr_rate

                cur.execute(
                    """
                    INSERT INTO due_amount (customer_id, total_due, expected_amount, emi_amount, interest_rate, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (customer_id) DO UPDATE SET
                        total_due = EXCLUDED.total_due,
                        expected_amount = EXCLUDED.expected_amount,
                        emi_amount = EXCLUDED.emi_amount,
                        interest_rate = EXCLUDED.interest_rate,
                        updated_at = NOW();
                    """,
                    (customer_id, upd_due, upd_exp, upd_exp, upd_rate),
                )

                cur.execute(
                    """
                    UPDATE collection_schedule
                    SET expected_amount = %s, updated_at = NOW()
                    WHERE customer_id = %s AND status != 'COLLECTED';
                    """,
                    (upd_exp, customer_id),
                )

            conn.commit()

            return jsonify({
                "message": "Customer updated successfully.",
                "customer": {
                    "id": customer_id,
                    "name": new_name,
                    "mobile": new_mobile,
                    "address": new_address,
                    "area": new_area,
                    "status": new_status,
                    "expectedAmount": float(expected_amount) if expected_amount is not None else None,
                    "totalDue": float(total_due) if total_due is not None else None,
                    "lastUpdated": "Just now",
                    "last_updated": "Just now",
                    "updatedAt": datetime.now().isoformat(),
                },
            }), 200


def _compute_closing_summary(cur, customer_id):
    """
    Shared calculation for the account-closing summary: total credit (Cash In),
    total debit (Cash Out), outstanding principal, and simple interest on that
    principal (annual rate x days outstanding / 365).
    """
    cur.execute(
        "SELECT id, name, mobile, account_number, created_at FROM collection_customers WHERE id = %s",
        (customer_id,),
    )
    cust_row = cur.fetchone()
    if not cust_row:
        return None

    cur.execute(
        "SELECT total_due, interest_rate, last_payment_date FROM due_amount WHERE customer_id = %s",
        (customer_id,),
    )
    due_row = cur.fetchone()
    principal = Decimal(str(due_row[0])) if due_row and due_row[0] is not None else Decimal("0.00")
    interest_rate = Decimal(str(due_row[1])) if due_row and due_row[1] is not None else Decimal("0.00")
    last_payment_date = due_row[2] if due_row else None

    cur.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN entry_type = 'IN' THEN amount ELSE 0 END), 0) as credit,
            COALESCE(SUM(CASE WHEN entry_type = 'OUT' THEN amount ELSE 0 END), 0) as debit
        FROM payment_collection
        WHERE customer_id = %s AND status = 'SUCCESS'
        """,
        (customer_id,),
    )
    credit_row = cur.fetchone()
    total_credit = Decimal(str(credit_row[0] or 0))
    total_debit = Decimal(str(credit_row[1] or 0))

    today = date.today()
    since_date = last_payment_date or (cust_row[4].date() if hasattr(cust_row[4], "date") else cust_row[4])
    days_outstanding = max(0, (today - since_date).days) if since_date and principal > Decimal("0.00") else 0

    interest_amount = Decimal("0.00")
    if principal > Decimal("0.00") and interest_rate > Decimal("0.00") and days_outstanding > 0:
        interest_amount = (principal * interest_rate / Decimal("100") * Decimal(days_outstanding) / Decimal("365")).quantize(Decimal("0.01"))

    final_settlement = principal + interest_amount

    return {
        "customer": {
            "id": cust_row[0],
            "name": cust_row[1],
            "mobile": cust_row[2],
            "accountNumber": cust_row[3],
        },
        "totalCredit": float(total_credit),
        "totalDebit": float(total_debit),
        "netPaid": float(total_credit - total_debit),
        "outstandingPrincipal": float(principal),
        "interestRate": float(interest_rate),
        "daysOutstanding": days_outstanding,
        "interestAmount": float(interest_amount),
        "finalSettlementAmount": float(final_settlement),
    }


@collection_bp.get("/customer/<int:customer_id>/closing-summary")
def get_customer_closing_summary(customer_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            summary = _compute_closing_summary(cur, customer_id)
            if summary is None:
                return jsonify({"message": "Customer not found."}), 404
            return jsonify(summary)


@collection_bp.post("/customer/<int:customer_id>/close")
def close_customer_account(customer_id):
    payload = request.get_json(silent=True) or {}
    notes = str(payload.get("notes", "")).strip()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            summary = _compute_closing_summary(cur, customer_id)
            if summary is None:
                return jsonify({"message": "Customer not found."}), 404

            cur.execute(
                """
                UPDATE collection_customers
                SET status = 'inactive', closed_at = NOW(), closing_summary = %s::jsonb, updated_at = NOW()
                WHERE id = %s
                """,
                (json.dumps(summary), customer_id),
            )

            actor = request.headers.get("X-User-Email", "Agent")
            details = (
                f"Account closed. Credit ₹{summary['totalCredit']:,.2f}, Debit ₹{summary['totalDebit']:,.2f}, "
                f"outstanding ₹{summary['outstandingPrincipal']:,.2f} + interest ₹{summary['interestAmount']:,.2f} "
                f"= final settlement ₹{summary['finalSettlementAmount']:,.2f}."
                + (f" Notes: {notes}" if notes else "")
            )
            cur.execute(
                """
                INSERT INTO customer_logs (customer_id, customer_name, action, performed_by, details, new_values)
                VALUES (%s, %s, 'CLOSED', %s, %s, %s::jsonb)
                """,
                (customer_id, summary["customer"]["name"], actor or "Agent", details, json.dumps(summary)),
            )
            conn.commit()

    return jsonify({"message": "Customer account closed.", "summary": summary})


@collection_bp.delete("/customers/<int:customer_id>")
def delete_customer(customer_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, collector_id, name FROM collection_customers WHERE id = %s", (customer_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"message": "Customer not found."}), 404

            active_cid = get_active_collector_id(cur)
            user_type = request.headers.get("X-User-Type", "").strip().lower()
            if user_type != "admin" and row[1] and row[1] != active_cid:
                return jsonify({"message": "Unauthorized to delete another collector's customer."}), 403

            cust_name = row[2]

            cur.execute("DELETE FROM payment_collection WHERE customer_id = %s", (customer_id,))
            cur.execute("DELETE FROM collection_schedule WHERE customer_id = %s", (customer_id,))
            cur.execute("DELETE FROM due_amount WHERE customer_id = %s", (customer_id,))
            cur.execute("DELETE FROM collection_customers WHERE id = %s", (customer_id,))

            conn.commit()

            return jsonify({
                "message": f"Customer '{cust_name}' deleted successfully.",
                "deletedCustomerId": customer_id,
            }), 200


@collection_bp.get("/customer/<int:customer_id>/history")
def get_customer_history(customer_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:

            # Customer details
            cur.execute(
                """
                SELECT c.id, c.account_number, c.name, c.mobile, c.address, c.area, c.status,
                       d.total_due, d.expected_amount, d.emi_amount, d.last_payment_date,
                       r.name as route_name
                FROM collection_customers c
                LEFT JOIN due_amount d ON c.id = d.customer_id
                LEFT JOIN collection_routes r ON c.route_id = r.id
                WHERE c.id = %s
                """,
                (customer_id,),
            )
            c_row = cur.fetchone()
            if not c_row:
                return jsonify({"message": "Customer not found"}), 404

            customer_info = {
                "id": c_row[0],
                "accountNumber": c_row[1],
                "name": c_row[2],
                "mobile": c_row[3],
                "address": c_row[4],
                "area": c_row[5] or "",
                "status": c_row[6],
                "totalDue": float(c_row[7] or 0),
                "expectedAmount": float(c_row[8] or 0),
                "emiAmount": float(c_row[9] or 0),
                "lastPaymentDate": str(c_row[10]) if c_row[10] else None,
                "routeName": c_row[11] or "Standard Route",
            }

            # Payments & All In/Out Transactions history
            cur.execute(
                """
                SELECT p.id, p.amount, p.payment_method, p.transaction_ref,
                       TO_CHAR(p.payment_date AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD"T"HH24:MI:SS+05:30'),
                       r.receipt_number, u.full_name,
                       COALESCE(p.entry_type, 'IN') as entry_type,
                       COALESCE(p.notes, '') as notes,
                       TO_CHAR(p.payment_date AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
                FROM payment_collection p
                LEFT JOIN payment_receipts r ON p.id = r.payment_id
                LEFT JOIN userdetails u ON p.collector_id = u.id
                WHERE p.customer_id = %s
                ORDER BY p.payment_date DESC, p.id DESC
                """,
                (customer_id,),
            )
            transactions = []
            total_paid_in = Decimal("0.00")
            total_given_out = Decimal("0.00")

            for p in cur.fetchall():
                amt = Decimal(str(p[1]))
                etype = (p[7] or "IN").upper()
                if etype == "IN":
                    total_paid_in += amt
                else:
                    total_given_out += amt

                transactions.append({
                    "id": p[0],
                    "amount": float(amt),
                    "paymentMethod": p[2],
                    "transactionRef": p[3] or "",
                    "paymentDate": p[4],
                    "payment_date": p[4],
                    "paymentDateFormatted": p[9] or p[4],
                    "receiptNumber": p[5],
                    "collectorName": p[6] or "Agent",
                    "entryType": etype,
                    "type": etype,
                    "notes": p[8] or "",
                })

            # Field visits & collection records
            cur.execute(
                """
                SELECT cr.id, cr.visit_date, cr.status, cr.reason, cr.notes,
                       cr.next_followup_date, u.full_name
                FROM collection_records cr
                LEFT JOIN userdetails u ON cr.collector_id = u.id
                WHERE cr.customer_id = %s
                ORDER BY cr.visit_date DESC, cr.id DESC
                """,
                (customer_id,),
            )
            records = [
                {
                    "id": cr[0],
                    "visitDate": str(cr[1]),
                    "status": cr[2],
                    "reason": cr[3] or "",
                    "notes": cr[4] or "",
                    "nextFollowupDate": str(cr[5]) if cr[5] else None,
                    "collectorName": cr[6] or "Agent",
                }
                for cr in cur.fetchall()
            ]

            # Day-by-day collection status for this customer, so the passbook can render a
            # calendar (green = paid, red = pending/missed) instead of just a transaction feed.
            cur.execute(
                """
                SELECT schedule_date, status, expected_amount, collected_amount
                FROM collection_schedule
                WHERE customer_id = %s
                ORDER BY schedule_date ASC
                """,
                (customer_id,),
            )
            schedule_days = [
                {
                    "date": str(row[0]),
                    "status": row[1],
                    "expectedAmount": float(row[2] or 0),
                    "collectedAmount": float(row[3] or 0),
                }
                for row in cur.fetchall()
            ]

            # "Total Paid" is the net amount actually collected: what was received minus
            # whatever was handed back to the customer (e.g. change/advance), not the raw
            # sum of Cash In entries alone.
            net_collected = total_paid_in - total_given_out

            return jsonify({
                "customer": customer_info,
                "summary": {
                    "totalPaidTillNow": float(net_collected),
                    "totalPaid": float(net_collected),
                    "grossPaidTillNow": float(total_paid_in),
                    "totalGivenTillNow": float(total_given_out),
                    "totalGiven": float(total_given_out),
                    "currentBalance": customer_info["totalDue"],
                    "totalDue": customer_info["totalDue"],
                },
                "transactions": transactions,
                "payments": [t for t in transactions if t["entryType"] == "IN"],
                "records": records,
                "scheduleDays": schedule_days,
            })


@collection_bp.post("/customer/<int:customer_id>/entry")
@collection_bp.post("/entries")
def create_customer_entry(customer_id=None):
    payload = request.get_json(silent=True) or {}
    cid = customer_id or payload.get("customerId") or payload.get("customer_id")
    if not cid:
        return jsonify({"message": "Customer ID is required."}), 400

    entry_type = str(payload.get("type") or payload.get("entry_type") or "IN").strip().upper()
    payment_method = str(payload.get("paymentMethod") or payload.get("payment_method") or "CASH").strip().upper()
    transaction_ref = str(payload.get("transactionRef") or payload.get("transaction_ref") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    collector_id_param = payload.get("collectorId") or payload.get("collector_id")

    try:
        amount = Decimal(str(payload.get("amount", 0)))
    except Exception:
        return jsonify({"message": "Valid amount is required."}), 400

    if amount <= Decimal("0.00"):
        return jsonify({"message": "Amount must be greater than 0."}), 400

    if entry_type not in ("IN", "OUT"):
        return jsonify({"message": "Entry type must be 'IN' or 'OUT'."}), 400

    if payment_method not in ("CASH", "UPI", "BANK_TRANSFER"):
        payment_method = "CASH"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            collector_id = get_active_collector_id(cur, collector_id_param)

            # Check customer exists
            cur.execute("SELECT id, name, collector_id FROM collection_customers WHERE id = %s", (cid,))
            cust_row = cur.fetchone()
            if not cust_row:
                return jsonify({"message": "Customer not found."}), 404

            # Resolve current due
            cur.execute("SELECT total_due, expected_amount FROM due_amount WHERE customer_id = %s FOR UPDATE", (cid,))
            due_row = cur.fetchone()
            current_due = due_row[0] if due_row else Decimal("0.00")
            expected_amt = due_row[1] if due_row else Decimal("0.00")

            today = date.today()

            # 1. IN: Customer Paid / Received Cash
            if entry_type == "IN":
                new_due = max(Decimal("0.00"), current_due - amount)
                cur.execute(
                    """
                    INSERT INTO due_amount (customer_id, total_due, expected_amount, emi_amount, last_payment_date)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (customer_id) DO UPDATE SET
                        total_due = EXCLUDED.total_due,
                        last_payment_date = EXCLUDED.last_payment_date;
                    """,
                    (cid, new_due, expected_amt, expected_amt, today),
                )

                # Update today's schedule if it exists, otherwise create it - a passbook payment
                # should always land on today's beat, even for a customer whose only schedule
                # entry (if any) was from an earlier day.
                cur.execute(
                    """
                    SELECT id, collected_amount, expected_amount
                    FROM collection_schedule
                    WHERE schedule_date = %s AND customer_id = %s
                    FOR UPDATE
                    """,
                    (today, cid),
                )
                sched_row = cur.fetchone()
                if sched_row:
                    sched_id, existing_collected, today_expected = sched_row
                    existing_collected = existing_collected or Decimal("0.00")
                    today_expected = today_expected or Decimal("0.00")
                else:
                    cur.execute(
                        """
                        INSERT INTO collection_schedule (schedule_date, collector_id, customer_id, expected_amount, collected_amount, status)
                        VALUES (%s, %s, %s, %s, 0, 'PENDING')
                        ON CONFLICT (schedule_date, collector_id, customer_id) DO NOTHING
                        RETURNING id;
                        """,
                        (today, collector_id, cid, expected_amt),
                    )
                    inserted = cur.fetchone()
                    if inserted:
                        sched_id = inserted[0]
                    else:
                        cur.execute(
                            "SELECT id FROM collection_schedule WHERE schedule_date = %s AND customer_id = %s",
                            (today, cid),
                        )
                        sched_id = cur.fetchone()[0]
                    existing_collected = Decimal("0.00")
                    today_expected = expected_amt

                # Cap what applies to today's row at what it actually still needs; any excess
                # (e.g. a customer paying off several days' backlog in one passbook entry)
                # sweeps to the customer's oldest other unresolved days instead of being
                # absorbed into today's row and vanishing from the pending total.
                remaining_for_today = max(Decimal("0.00"), today_expected - existing_collected)
                amount_for_today = min(amount, remaining_for_today) if remaining_for_today > Decimal("0.00") else Decimal("0.00")
                excess_for_backlog = amount - amount_for_today

                new_sched_collected = existing_collected + amount_for_today
                sched_status = "COLLECTED" if new_sched_collected >= today_expected else "PARTIAL_PAYMENT"
                cur.execute(
                    """
                    UPDATE collection_schedule
                    SET collected_amount = %s, status = %s, updated_at = NOW()
                    WHERE id = %s;
                    """,
                    (new_sched_collected, sched_status, sched_id),
                )

                if excess_for_backlog > Decimal("0.00"):
                    cur.execute(
                        """
                        SELECT id, expected_amount, collected_amount
                        FROM collection_schedule
                        WHERE customer_id = %s AND collector_id = %s AND id != %s
                          AND expected_amount > collected_amount
                        ORDER BY schedule_date ASC
                        FOR UPDATE
                        """,
                        (cid, collector_id, sched_id),
                    )
                    for row_id, row_expected, row_collected in cur.fetchall():
                        if excess_for_backlog <= Decimal("0.00"):
                            break
                        row_remaining = Decimal(str(row_expected)) - Decimal(str(row_collected))
                        apply_amt = min(excess_for_backlog, row_remaining)
                        new_row_collected = Decimal(str(row_collected)) + apply_amt
                        row_status = "COLLECTED" if new_row_collected >= Decimal(str(row_expected)) else "PARTIAL_PAYMENT"
                        cur.execute(
                            "UPDATE collection_schedule SET collected_amount = %s, status = %s, updated_at = NOW() WHERE id = %s",
                            (new_row_collected, row_status, row_id),
                        )
                        excess_for_backlog -= apply_amt

                # Record in payment_collection
                cur.execute(
                    """
                    INSERT INTO payment_collection (customer_id, collector_id, schedule_id, amount, payment_method, transaction_ref, entry_type, notes, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'IN', %s, 'SUCCESS')
                    RETURNING id;
                    """,
                    (cid, collector_id, sched_id, amount, payment_method, transaction_ref, notes),
                )
                payment_id = cur.fetchone()[0]

                # Generate receipt
                receipt_number = generate_receipt_number(cur, today)
                cur.execute(
                    """
                    INSERT INTO payment_receipts (payment_id, receipt_number, customer_id, collector_id, amount, payment_method, transaction_ref, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (payment_id, receipt_number, cid, collector_id, amount, payment_method, transaction_ref, notes),
                )

                # Log visit record
                cur.execute(
                    """
                    INSERT INTO collection_records (schedule_id, customer_id, collector_id, visit_date, status, notes)
                    VALUES (%s, %s, %s, %s, 'COLLECTED', %s);
                    """,
                    (sched_id, cid, collector_id, today, notes or f"Cash In payment ₹{amount}"),
                )

            # 2. OUT: Given to Customer / Loan / Due Added
            else:
                new_due = current_due + amount
                cur.execute(
                    """
                    INSERT INTO due_amount (customer_id, total_due, expected_amount, emi_amount)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (customer_id) DO UPDATE SET
                        total_due = EXCLUDED.total_due;
                    """,
                    (cid, new_due, expected_amt, expected_amt),
                )

                # Reduce today's schedule collected_amount too, so the beat list shows the
                # NET amount actually collected today (received minus given back), not the
                # raw Cash In total.
                cur.execute(
                    """
                    SELECT id, collected_amount, expected_amount
                    FROM collection_schedule
                    WHERE schedule_date = %s AND customer_id = %s
                    FOR UPDATE
                    """,
                    (today, cid),
                )
                sched_row = cur.fetchone()
                if sched_row:
                    sched_id = sched_row[0]
                    new_sched_collected = max(Decimal("0.00"), (sched_row[1] or Decimal("0.00")) - amount)
                    expected_sched = sched_row[2] or Decimal("0.00")
                    if new_sched_collected <= Decimal("0.00"):
                        sched_status = "PENDING"
                    elif new_sched_collected >= expected_sched:
                        sched_status = "COLLECTED"
                    else:
                        sched_status = "PARTIAL_PAYMENT"
                    cur.execute(
                        """
                        UPDATE collection_schedule
                        SET collected_amount = %s, status = %s, updated_at = NOW()
                        WHERE id = %s;
                        """,
                        (new_sched_collected, sched_status, sched_id),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO collection_schedule (schedule_date, collector_id, customer_id, expected_amount, collected_amount, status)
                        VALUES (%s, %s, %s, %s, 0, 'PENDING')
                        ON CONFLICT (schedule_date, collector_id, customer_id) DO NOTHING
                        RETURNING id;
                        """,
                        (today, collector_id, cid, expected_amt),
                    )
                    inserted = cur.fetchone()
                    sched_id = inserted[0] if inserted else None

                # Record in payment_collection with 'OUT'
                cur.execute(
                    """
                    INSERT INTO payment_collection (customer_id, collector_id, schedule_id, amount, payment_method, transaction_ref, entry_type, notes, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'OUT', %s, 'SUCCESS')
                    RETURNING id;
                    """,
                    (cid, collector_id, sched_id, amount, payment_method, transaction_ref, notes),
                )
                payment_id = cur.fetchone()[0]
                receipt_number = None

                # Log visit record
                cur.execute(
                    """
                    INSERT INTO collection_records (schedule_id, customer_id, collector_id, visit_date, status, notes)
                    VALUES (%s, %s, %s, %s, 'PENDING', %s);
                    """,
                    (sched_id, cid, collector_id, today, notes or f"Cash Out advance ₹{amount}"),
                )

            # Recalculate daily summary for collector
            recalculate_daily_summary(cur, collector_id, today)
            conn.commit()

            return jsonify({
                "success": True,
                "message": f"Successfully recorded Cash {'In (Payment Received)' if entry_type == 'IN' else 'Out (Given / Due Added)'}.",
                "entry": {
                    "id": payment_id,
                    "customerId": cid,
                    "type": entry_type,
                    "entryType": entry_type,
                    "amount": float(amount),
                    "paymentMethod": payment_method,
                    "notes": notes,
                    "receiptNumber": receipt_number,
                    "newTotalDue": float(new_due),
                    "date": str(today),
                },
            }), 201


@collection_bp.delete("/entries/<int:payment_id>")
def delete_customer_entry(payment_id):
    """
    Delete a single passbook (Cash In/Out) entry, reversing only that entry's effect
    on the customer's due balance and today's schedule - not touching any other
    entries for that customer.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT customer_id, collector_id, schedule_id, amount, entry_type FROM payment_collection WHERE id = %s FOR UPDATE",
                (payment_id,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"message": "Entry not found."}), 404

            customer_id, collector_id, schedule_id, amount, entry_type = row

            # Reverse the effect this specific entry had on the customer's due balance
            cur.execute("SELECT total_due FROM due_amount WHERE customer_id = %s FOR UPDATE", (customer_id,))
            due_row = cur.fetchone()
            current_due = due_row[0] if due_row else Decimal("0.00")

            if entry_type == "IN":
                new_due = current_due + amount  # undo the reduction this payment made
            else:
                new_due = max(Decimal("0.00"), current_due - amount)  # undo the increase this advance made

            cur.execute(
                "UPDATE due_amount SET total_due = %s, updated_at = NOW() WHERE customer_id = %s",
                (new_due, customer_id),
            )

            # Reverse this entry's contribution to its linked schedule row, if any.
            # IN entries added to collected_amount, so deleting one subtracts it back out;
            # OUT entries subtracted from collected_amount, so deleting one adds it back.
            if schedule_id:
                cur.execute(
                    "SELECT collected_amount, expected_amount FROM collection_schedule WHERE id = %s FOR UPDATE",
                    (schedule_id,),
                )
                sched_row = cur.fetchone()
                if sched_row:
                    delta = -amount if entry_type == "IN" else amount
                    new_collected = max(Decimal("0.00"), (sched_row[0] or Decimal("0.00")) + delta)
                    expected = sched_row[1] or Decimal("0.00")
                    if new_collected <= Decimal("0.00"):
                        new_status = "PENDING"
                    elif new_collected >= expected:
                        new_status = "COLLECTED"
                    else:
                        new_status = "PARTIAL_PAYMENT"
                    cur.execute(
                        "UPDATE collection_schedule SET collected_amount = %s, status = %s, updated_at = NOW() WHERE id = %s",
                        (new_collected, new_status, schedule_id),
                    )

            # payment_receipts row cascades away automatically (ON DELETE CASCADE)
            cur.execute("DELETE FROM payment_collection WHERE id = %s", (payment_id,))
            conn.commit()

    return jsonify({"message": "Entry deleted and balance reversed.", "newTotalDue": float(new_due)})


# -------------------------------------------------------------------------
# 4. PAYMENT COLLECTION (WITH TRANSACTIONS & RECEIPT GENERATION)
# -------------------------------------------------------------------------
@collection_bp.post("/collect")
def collect_payment():
    payload = request.get_json(silent=True) or {}
    schedule_id = payload.get("scheduleId") or payload.get("schedule_id")
    customer_id = payload.get("customerId") or payload.get("customer_id")
    collector_id_param = payload.get("collectorId") or payload.get("collector_id")
    payment_method = str(payload.get("paymentMethod") or payload.get("payment_method", "CASH")).strip().upper()
    transaction_ref = str(payload.get("transactionRef") or payload.get("transaction_ref", "")).strip()
    notes = str(payload.get("notes", "")).strip()

    try:
        amount = Decimal(str(payload.get("amount", 0)))
    except Exception:
        return jsonify({"message": "Valid payment amount is required."}), 400

    if amount <= Decimal("0.00"):
        return jsonify({"message": "Payment amount must be greater than 0."}), 400

    if payment_method not in ("CASH", "UPI", "BANK_TRANSFER"):
        return jsonify({"message": "Payment method must be CASH, UPI, or BANK_TRANSFER."}), 400

    if payment_method in ("UPI", "BANK_TRANSFER") and not transaction_ref:
        return jsonify({"message": f"Transaction reference is required for {payment_method}."}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            collector_id = get_active_collector_id(cur, collector_id_param)
            target_date = parse_date(payload.get("date"))

            # 1. Guard against collecting on a closed day
            if is_day_closed(cur, collector_id, target_date):
                return jsonify({
                    "message": "Today's collection has been closed and locked. No further payments can be collected.",
                }), 400

            # 2. Resolve Schedule & Customer
            if schedule_id:
                cur.execute(
                    """
                    SELECT s.id, s.customer_id, s.collector_id, s.expected_amount, s.collected_amount, s.schedule_date, s.status,
                           c.name, c.mobile, c.account_number, d.total_due
                    FROM collection_schedule s
                    JOIN collection_customers c ON s.customer_id = c.id
                    LEFT JOIN due_amount d ON c.id = d.customer_id
                    WHERE s.id = %s
                    FOR UPDATE OF s
                    """,
                    (schedule_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT s.id, s.customer_id, s.collector_id, s.expected_amount, s.collected_amount, s.schedule_date, s.status,
                           c.name, c.mobile, c.account_number, d.total_due
                    FROM collection_schedule s
                    JOIN collection_customers c ON s.customer_id = c.id
                    LEFT JOIN due_amount d ON c.id = d.customer_id
                    WHERE s.schedule_date = %s AND s.customer_id = %s AND s.collector_id = %s
                    FOR UPDATE OF s
                    """,
                    (target_date, customer_id, collector_id),
                )
            sched = cur.fetchone()
            if not sched:
                return jsonify({"message": "Schedule entry not found for customer."}), 404

            (
                s_id,
                c_id,
                col_id,
                expected_amt,
                already_collected,
                s_date,
                current_status,
                cust_name,
                cust_mobile,
                acct_no,
                total_due,
            ) = sched

            # 3. Duplicate protection / Idempotency check:
            # Check if an identical payment was submitted within the last 5 seconds for this customer
            cur.execute(
                """
                SELECT id FROM payment_collection
                WHERE schedule_id = %s AND amount = %s AND payment_method = %s
                  AND payment_date >= NOW() - INTERVAL '5 seconds'
                """,
                (s_id, amount, payment_method),
            )
            if cur.fetchone():
                return jsonify({"message": "Duplicate payment detected. Please wait a moment."}), 409

            # 4. Split the payment between the targeted day and any older backlog days.
            # A customer paying more than one day's expected amount (e.g. catching up on a
            # backlog) should have the excess applied to their oldest unpaid days, not have it
            # all absorbed into the single targeted row while older PENDING rows stay untouched
            # and still count as owed in the pending total.
            remaining_for_target = max(Decimal("0.00"), Decimal(str(expected_amt)) - Decimal(str(already_collected)))
            amount_for_target = min(amount, remaining_for_target) if remaining_for_target > Decimal("0.00") else Decimal("0.00")
            excess_for_backlog = amount - amount_for_target

            new_collected_total = Decimal(str(already_collected)) + amount_for_target
            new_status = "COLLECTED" if new_collected_total >= Decimal(str(expected_amt)) else "PARTIAL_PAYMENT"

            # 5. Insert field collection record
            cur.execute(
                """
                INSERT INTO collection_records (schedule_id, customer_id, collector_id, visit_date, status, reason, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (s_id, c_id, col_id, s_date, new_status, "Payment collected", notes),
            )
            record_id = cur.fetchone()[0]

            # 6. Insert payment_collection record
            cur.execute(
                """
                INSERT INTO payment_collection (
                    collection_record_id, schedule_id, customer_id, collector_id,
                    amount, payment_method, transaction_ref, status, payment_date
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'SUCCESS', NOW())
                RETURNING id, payment_date;
                """,
                (record_id, s_id, c_id, col_id, amount, payment_method, transaction_ref or None),
            )
            pay_id, payment_date = cur.fetchone()

            # 7. Generate Receipt Number and Insert payment_receipts
            receipt_no = generate_receipt_number(cur, s_date)
            cur.execute(
                """
                INSERT INTO payment_receipts (
                    payment_id, receipt_number, customer_id, collector_id,
                    amount, payment_method, transaction_ref, notes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (pay_id, receipt_no, c_id, col_id, amount, payment_method, transaction_ref or None, notes),
            )

            # 8. Update collection_schedule
            cur.execute(
                """
                UPDATE collection_schedule
                SET collected_amount = %s,
                    status = %s,
                    notes = COALESCE(NULLIF(%s, ''), notes),
                    updated_at = NOW()
                WHERE id = %s;
                """,
                (new_collected_total, new_status, notes, s_id),
            )

            # 8b. Sweep any leftover payment across the customer's other outstanding days
            # (oldest first), so paying more than one day's amount actually clears the
            # backlog instead of leaving older days still marked owed.
            if excess_for_backlog > Decimal("0.00"):
                cur.execute(
                    """
                    SELECT id, expected_amount, collected_amount
                    FROM collection_schedule
                    WHERE customer_id = %s AND collector_id = %s AND id != %s
                      AND expected_amount > collected_amount
                    ORDER BY schedule_date ASC
                    FOR UPDATE
                    """,
                    (c_id, col_id, s_id),
                )
                for row_id, row_expected, row_collected in cur.fetchall():
                    if excess_for_backlog <= Decimal("0.00"):
                        break
                    row_remaining = Decimal(str(row_expected)) - Decimal(str(row_collected))
                    apply_amt = min(excess_for_backlog, row_remaining)
                    new_row_collected = Decimal(str(row_collected)) + apply_amt
                    row_status = "COLLECTED" if new_row_collected >= Decimal(str(row_expected)) else "PARTIAL_PAYMENT"
                    cur.execute(
                        "UPDATE collection_schedule SET collected_amount = %s, status = %s, updated_at = NOW() WHERE id = %s",
                        (new_row_collected, row_status, row_id),
                    )
                    excess_for_backlog -= apply_amt

            # 9. Update due_amount (deduct paid amount, update last payment date)
            cur.execute(
                """
                UPDATE due_amount
                SET total_due = GREATEST(0.00, total_due - %s),
                    last_payment_date = %s,
                    updated_at = NOW()
                WHERE customer_id = %s
                RETURNING total_due;
                """,
                (amount, s_date, c_id),
            )
            remaining_due_row = cur.fetchone()
            new_remaining_due = float(remaining_due_row[0]) if remaining_due_row else 0.0

            # 10. Recalculate daily_collection_summary
            summary = recalculate_daily_summary(cur, col_id, s_date)

            conn.commit()

            return jsonify({
                "message": f"Successfully collected ₹{amount:,.2f} via {payment_method}.",
                "receipt": {
                    "receiptNumber": receipt_no,
                    "paymentId": pay_id,
                    "amount": float(amount),
                    "paymentMethod": payment_method,
                    "transactionRef": transaction_ref,
                    "paymentDate": payment_date.strftime("%Y-%m-%d %H:%M:%S") if hasattr(payment_date, "strftime") else str(payment_date),
                    "customerName": cust_name,
                    "accountNumber": acct_no,
                    "mobile": cust_mobile,
                    "remainingDue": new_remaining_due,
                    "notes": notes,
                },
                "schedule": {
                    "scheduleId": s_id,
                    "status": new_status,
                    "collectedAmount": float(new_collected_total),
                    "expectedAmount": float(expected_amt),
                },
                "summary": summary,
            }), 201


# -------------------------------------------------------------------------
# 5. UPDATE VISIT STATUS (NOT_AVAILABLE, REFUSED, MISSED, PENDING)
# -------------------------------------------------------------------------
@collection_bp.post("/status")
def update_status():
    payload = request.get_json(silent=True) or {}
    schedule_id = payload.get("scheduleId") or payload.get("schedule_id")
    new_status = str(payload.get("status", "")).strip().upper()
    reason = str(payload.get("reason", "")).strip()
    notes = str(payload.get("notes", "")).strip()

    valid_statuses = ("NOT_AVAILABLE", "REFUSED", "MISSED", "PENDING")
    if new_status not in valid_statuses:
        return jsonify({"message": f"Status must be one of: {', '.join(valid_statuses)}"}), 400

    if new_status in ("NOT_AVAILABLE", "REFUSED", "MISSED") and not reason:
        return jsonify({"message": "Reason is required when marking customer as not collected."}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.customer_id, s.collector_id, s.schedule_date, s.status, c.name
                FROM collection_schedule s
                JOIN collection_customers c ON s.customer_id = c.id
                WHERE s.id = %s
                FOR UPDATE OF s
                """,
                (schedule_id,),
            )
            sched = cur.fetchone()
            if not sched:
                return jsonify({"message": "Schedule item not found."}), 404

            s_id, c_id, col_id, s_date, old_status, cust_name = sched

            if is_day_closed(cur, col_id, s_date):
                return jsonify({"message": "Today's collection is closed. Cannot update status."}), 400

            # Update schedule
            cur.execute(
                """
                UPDATE collection_schedule
                SET status = %s, notes = COALESCE(NULLIF(%s, ''), notes), updated_at = NOW()
                WHERE id = %s;
                """,
                (new_status, notes or reason, s_id),
            )

            # Insert collection record
            cur.execute(
                """
                INSERT INTO collection_records (schedule_id, customer_id, collector_id, visit_date, status, reason, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
                """,
                (s_id, c_id, col_id, s_date, new_status, reason, notes),
            )

            # Log to followups if missed/refused/not available
            if new_status in ("NOT_AVAILABLE", "REFUSED", "MISSED"):
                cur.execute(
                    """
                    INSERT INTO collection_followups (schedule_id, customer_id, collector_id, reason, notes, followup_date, status)
                    VALUES (%s, %s, %s, %s, %s, CURRENT_DATE + INTERVAL '1 day', 'PENDING');
                    """,
                    (s_id, c_id, col_id, reason, notes),
                )

            summary = recalculate_daily_summary(cur, col_id, s_date)
            conn.commit()

            return jsonify({
                "message": f"Updated status for {cust_name} to {new_status}.",
                "scheduleId": s_id,
                "status": new_status,
                "summary": summary,
            })


# -------------------------------------------------------------------------
# 6. RESCHEDULE COLLECTION VISIT
# -------------------------------------------------------------------------
@collection_bp.post("/reschedule")
def reschedule_collection():
    payload = request.get_json(silent=True) or {}
    schedule_id = payload.get("scheduleId") or payload.get("schedule_id")
    reason = str(payload.get("reason", "")).strip()
    next_date_str = str(payload.get("nextFollowupDate") or payload.get("next_followup_date", "")).strip()
    notes = str(payload.get("notes", "")).strip()

    if not reason:
        return jsonify({"message": "Reason for rescheduling is required."}), 400
    if not next_date_str:
        return jsonify({"message": "Next follow-up date is required for rescheduling."}), 400

    next_date = parse_date(next_date_str)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.customer_id, s.collector_id, s.schedule_date, s.expected_amount, s.route_id, c.name
                FROM collection_schedule s
                JOIN collection_customers c ON s.customer_id = c.id
                WHERE s.id = %s
                FOR UPDATE OF s
                """,
                (schedule_id,),
            )
            sched = cur.fetchone()
            if not sched:
                return jsonify({"message": "Schedule item not found."}), 404

            s_id, c_id, col_id, s_date, exp_amt, route_id, cust_name = sched

            if is_day_closed(cur, col_id, s_date):
                return jsonify({"message": "Today's collection is closed. Cannot reschedule."}), 400

            # Update schedule to RESCHEDULED
            cur.execute(
                """
                UPDATE collection_schedule
                SET status = 'RESCHEDULED', notes = %s, updated_at = NOW()
                WHERE id = %s;
                """,
                (f"Rescheduled to {next_date}. Reason: {reason}", s_id),
            )

            # Insert visit record
            cur.execute(
                """
                INSERT INTO collection_records (schedule_id, customer_id, collector_id, visit_date, status, reason, notes, next_followup_date)
                VALUES (%s, %s, %s, %s, 'RESCHEDULED', %s, %s, %s);
                """,
                (s_id, c_id, col_id, s_date, reason, notes, next_date),
            )

            # Insert into followups queue
            cur.execute(
                """
                INSERT INTO collection_followups (schedule_id, customer_id, collector_id, reason, notes, followup_date, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'PENDING');
                """,
                (s_id, c_id, col_id, reason, notes, next_date),
            )

            # Create entry on next_date schedule if doesn't exist
            cur.execute(
                """
                INSERT INTO collection_schedule (schedule_date, collector_id, customer_id, route_id, route_order, expected_amount, status, notes)
                VALUES (%s, %s, %s, %s, 999, %s, 'PENDING', %s)
                ON CONFLICT (schedule_date, collector_id, customer_id) DO NOTHING;
                """,
                (next_date, col_id, c_id, route_id, exp_amt, f"Rescheduled from {s_date}. {reason}"),
            )

            summary = recalculate_daily_summary(cur, col_id, s_date)
            conn.commit()

            return jsonify({
                "message": f"{cust_name} successfully rescheduled to {next_date}.",
                "scheduleId": s_id,
                "status": "RESCHEDULED",
                "nextFollowupDate": str(next_date),
                "summary": summary,
            })


# -------------------------------------------------------------------------
# 7. MISSED & FOLLOW-UPS
# -------------------------------------------------------------------------
@collection_bp.get("/missed")
def get_missed_collections():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            target_date = parse_date(request.args.get("date"))
            collector_id = get_active_collector_id(cur, request.args.get("collector_id"))

            cur.execute(
                """
                SELECT 
                    s.id AS schedule_id,
                    c.id AS customer_id,
                    c.name,
                    c.mobile,
                    c.address,
                    c.area,
                    c.account_number,
                    s.expected_amount,
                    s.status,
                    COALESCE(cr.reason, 'Not Visited') AS reason,
                    cr.notes,
                    cr.next_followup_date,
                    f.id AS followup_id,
                    f.status AS followup_status
                FROM collection_schedule s
                JOIN collection_customers c ON s.customer_id = c.id
                LEFT JOIN LATERAL (
                    SELECT reason, notes, next_followup_date
                    FROM collection_records
                    WHERE schedule_id = s.id
                    ORDER BY id DESC
                    LIMIT 1
                ) cr ON TRUE
                LEFT JOIN LATERAL (
                    SELECT id, status
                    FROM collection_followups
                    WHERE schedule_id = s.id
                    ORDER BY id DESC
                    LIMIT 1
                ) f ON TRUE
                WHERE s.schedule_date = %s AND s.collector_id = %s
                  AND s.status IN ('MISSED', 'NOT_AVAILABLE', 'REFUSED', 'RESCHEDULED')
                ORDER BY s.route_order ASC, s.id ASC
                """,
                (target_date, collector_id),
            )
            missed_list = [
                {
                    "scheduleId": r[0],
                    "customerId": r[1],
                    "customerName": r[2],
                    "mobile": r[3],
                    "address": r[4],
                    "area": r[5] or "",
                    "accountNumber": r[6],
                    "expectedAmount": float(r[7]),
                    "status": r[8],
                    "reason": r[9],
                    "notes": r[10] or "",
                    "nextFollowupDate": str(r[11]) if r[11] else None,
                    "followupId": r[12],
                    "followupStatus": r[13] or "PENDING",
                }
                for r in cur.fetchall()
            ]

            return jsonify({
                "date": str(target_date),
                "totalMissed": len(missed_list),
                "customers": missed_list,
            })


# -------------------------------------------------------------------------
# 8. RECEIPTS & PAYMENTS
# -------------------------------------------------------------------------
@collection_bp.get("/payments")
def get_payments():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            target_date_str = request.args.get("date")
            collector_id = request.args.get("collector_id")

            query = """
                SELECT p.id, p.amount, p.payment_method, p.transaction_ref,
                       TO_CHAR(p.payment_date AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
                       pr.receipt_number, c.id, c.name, c.mobile, c.account_number,
                       u.full_name
                FROM payment_collection p
                JOIN payment_receipts pr ON p.id = pr.payment_id
                JOIN collection_customers c ON p.customer_id = c.id
                LEFT JOIN userdetails u ON p.collector_id = u.id
                WHERE 1=1
            """
            params = []
            if target_date_str:
                query += " AND DATE(p.payment_date) = %s"
                params.append(parse_date(target_date_str))
            if collector_id:
                query += " AND p.collector_id = %s"
                params.append(collector_id)

            query += " ORDER BY p.payment_date DESC, p.id DESC"
            cur.execute(query, tuple(params))

            payments = [
                {
                    "id": row[0],
                    "paymentId": row[0],
                    "amount": float(row[1]),
                    "amount_paid": float(row[1]),
                    "paymentMethod": row[2],
                    "payment_method": row[2],
                    "transactionRef": row[3] or "",
                    "transaction_ref": row[3] or "",
                    "paymentDate": row[4],
                    "created_at": row[4],
                    "receiptNumber": row[5],
                    "receipt_number": row[5],
                    "customerId": row[6],
                    "customer_id": row[6],
                    "customerName": row[7],
                    "customer_name": row[7],
                    "mobile": row[8],
                    "phone": row[8],
                    "accountNumber": row[9],
                    "customer_account": row[9],
                    "collectorName": row[10] or "Agent",
                    "collector_name": row[10] or "Agent",
                    "updated_balance": 0.0,
                }
                for row in cur.fetchall()
            ]

            return jsonify({"total": len(payments), "payments": payments})


@collection_bp.get("/receipts/<receipt_identifier>")
def get_receipt(receipt_identifier):
    with get_db_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT pr.receipt_number, pr.amount, pr.payment_method, pr.transaction_ref,
                       TO_CHAR(pr.receipt_date AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
                       c.id, c.name, c.mobile, c.address, c.area, c.account_number,
                       u.full_name, u.email, d.total_due, pr.notes
                FROM payment_receipts pr
                JOIN collection_customers c ON pr.customer_id = c.id
                LEFT JOIN due_amount d ON c.id = d.customer_id
                LEFT JOIN userdetails u ON pr.collector_id = u.id
                WHERE pr.receipt_number = %s OR CAST(pr.id AS TEXT) = %s
                LIMIT 1
                """,
                (receipt_identifier, receipt_identifier),
            )
            r = cur.fetchone()
            if not r:
                return jsonify({"message": "Receipt not found."}), 404

            return jsonify({
                "receipt": {
                    "receiptNumber": r[0],
                    "amount": float(r[1]),
                    "paymentMethod": r[2],
                    "transactionRef": r[3] or "N/A (Cash)",
                    "receiptDate": r[4],
                    "customerId": r[5],
                    "customerName": r[6],
                    "customerMobile": r[7],
                    "customerAddress": r[8],
                    "customerArea": r[9] or "",
                    "accountNumber": r[10],
                    "collectorName": r[11] or "AutoLedger Agent",
                    "collectorEmail": r[12] or "",
                    "currentOutstandingBalance": float(r[13] or 0),
                    "notes": r[14] or "",
                }
            })


# -------------------------------------------------------------------------
# 9. DAILY SUMMARY & DAILY CLOSING
# -------------------------------------------------------------------------
@collection_bp.get("/daily-summary")
def get_daily_summary():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            target_date = parse_date(request.args.get("date"))
            collector_id = get_active_collector_id(cur, request.args.get("collector_id"))
            summary = recalculate_daily_summary(cur, collector_id, target_date)
            conn.commit()
            return jsonify({
                **summary,
                "summary": summary
            })


@collection_bp.get("/daily-closing")
def get_daily_closing():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            target_date = parse_date(request.args.get("date"))
            collector_id = get_active_collector_id(cur, request.args.get("collector_id"))
            cur.execute("""
                SELECT id, closing_date, collector_id, total_customers, collected_count,
                       pending_count, missed_count, expected_amount, collected_amount,
                       remaining_amount, cash_collected, upi_collected, bank_collected,
                       handover_notes, is_closed, closed_at, reopened_at
                FROM collection_daily_closing
                WHERE closing_date = %s AND collector_id = %s
            """, (target_date, collector_id))
            row = cur.fetchone()
            if not row:
                summary = recalculate_daily_summary(cur, collector_id, target_date)
                return jsonify({
                    "isClosed": False,
                    "closingStatus": "OPEN",
                    "closingDate": str(target_date),
                    "collectorId": collector_id,
                    "summary": summary
                })
            
            return jsonify({
                "id": row[0],
                "closingDate": str(row[1]),
                "collectorId": row[2],
                "totalCustomers": row[3],
                "collectedCount": row[4],
                "pendingCount": row[5],
                "missedCount": row[6],
                "expectedAmount": float(row[7] or 0),
                "collectedAmount": float(row[8] or 0),
                "remainingAmount": float(row[9] or 0),
                "cashCollected": float(row[10] or 0),
                "upiCollected": float(row[11] or 0),
                "bankCollected": float(row[12] or 0),
                "handoverNotes": row[13] or "",
                "isClosed": bool(row[14]),
                "closingStatus": "CLOSED" if row[14] else "OPEN",
                "closedAt": row[15].isoformat() if row[15] else None,
                "reopenedAt": row[16].isoformat() if row[16] else None
            })


@collection_bp.post("/daily-closing")
def close_daily_collection():
    payload = request.get_json(silent=True) or {}
    collector_id = get_active_collector_id(None, payload.get("collectorId") or payload.get("collector_id"))
    target_date = parse_date(payload.get("date") or payload.get("closingDate"))
    handover_notes = str(payload.get("handoverNotes", "")).strip()

    with get_db_connection() as conn:
        with conn.cursor() as cur:

            # Recalculate latest numbers
            summary = recalculate_daily_summary(cur, collector_id, target_date)

            # Insert or update closing record
            cur.execute(
                """
                INSERT INTO collection_daily_closing (
                    closing_date, collector_id,
                    total_customers, collected_count, pending_count, missed_count,
                    expected_amount, collected_amount, remaining_amount,
                    cash_collected, upi_collected, bank_collected,
                    handover_notes, is_closed, closed_by, closed_at, updated_at
                ) VALUES (
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, TRUE, %s, NOW(), NOW()
                )
                ON CONFLICT (closing_date, collector_id) DO UPDATE SET
                    total_customers = EXCLUDED.total_customers,
                    collected_count = EXCLUDED.collected_count,
                    pending_count = EXCLUDED.pending_count,
                    missed_count = EXCLUDED.missed_count,
                    expected_amount = EXCLUDED.expected_amount,
                    collected_amount = EXCLUDED.collected_amount,
                    remaining_amount = EXCLUDED.remaining_amount,
                    cash_collected = EXCLUDED.cash_collected,
                    upi_collected = EXCLUDED.upi_collected,
                    bank_collected = EXCLUDED.bank_collected,
                    handover_notes = EXCLUDED.handover_notes,
                    is_closed = TRUE,
                    closed_by = EXCLUDED.closed_by,
                    closed_at = NOW(),
                    updated_at = NOW()
                RETURNING id;
                """,
                (
                    target_date,
                    collector_id,
                    summary["totalAssigned"],
                    summary["totalCollected"],
                    summary["totalPending"],
                    summary["totalMissed"] + summary["totalNotAvailable"] + summary["totalRefused"],
                    Decimal(str(summary["totalExpectedAmount"])),
                    Decimal(str(summary["totalCollectedAmount"])),
                    Decimal(str(summary["totalRemainingAmount"])),
                    Decimal(str(summary["cashAmount"])),
                    Decimal(str(summary["upiAmount"])),
                    Decimal(str(summary["bankAmount"])),
                    handover_notes,
                    collector_id,
                ),
            )
            closing_id = cur.fetchone()[0]

            # Record cash handover record
            cur.execute(
                """
                INSERT INTO cash_handover (closing_id, collector_id, cash_amount, handover_status, notes)
                VALUES (%s, %s, %s, 'RECEIVED', %s);
                """,
                (closing_id, collector_id, Decimal(str(summary["cashAmount"])), handover_notes),
            )

            conn.commit()

            return jsonify({
                "message": f"Daily collection for {target_date} closed successfully. Further modifications are locked.",
                "closingId": closing_id,
                "closingDate": str(target_date),
                "summary": summary,
            })


@collection_bp.post("/daily-closing/reopen")
def reopen_daily_closing():
    payload = request.get_json(silent=True) or {}
    closing_id = payload.get("closingId")
    collector_id = payload.get("collectorId")
    target_date = parse_date(payload.get("date"))

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if closing_id:
                cur.execute(
                    """
                    UPDATE collection_daily_closing
                    SET is_closed = FALSE, reopened_at = NOW(), updated_at = NOW()
                    WHERE id = %s
                    RETURNING closing_date, collector_id;
                    """,
                    (closing_id,),
                )
            else:
                cur.execute(
                    """
                    UPDATE collection_daily_closing
                    SET is_closed = FALSE, reopened_at = NOW(), updated_at = NOW()
                    WHERE closing_date = %s AND collector_id = %s
                    RETURNING closing_date, collector_id;
                    """,
                    (target_date, collector_id),
                )
            row = cur.fetchone()
            if not row:
                return jsonify({"message": "Closing record not found."}), 404

            conn.commit()
            return jsonify({"message": f"Collection schedule for {row[0]} reopened successfully."})


# -------------------------------------------------------------------------
# 10. REPORTS & RECONCILIATION
# -------------------------------------------------------------------------
@collection_bp.get("/reports")
def get_reports():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            start_date = parse_date(request.args.get("startDate") or request.args.get("start_date") or "2026-09-01")
            end_date = parse_date(request.args.get("endDate") or request.args.get("end_date") or "2026-09-07")
            collector_id = request.args.get("collector_id")

            # Daily breakdown over range
            query = """
                SELECT s.summary_date,
                       COALESCE(u.full_name, 'Agent'),
                       s.total_assigned, s.total_collected, s.total_pending,
                       (s.total_missed + s.total_not_available + s.total_refused) as total_missed,
                       s.total_expected_amount, s.total_collected_amount, s.total_remaining_amount,
                       s.cash_amount, s.upi_amount, s.bank_amount,
                       COALESCE(c.is_closed, FALSE)
                FROM daily_collection_summary s
                LEFT JOIN userdetails u ON s.collector_id = u.id
                LEFT JOIN collection_daily_closing c ON s.summary_date = c.closing_date AND s.collector_id = c.collector_id
                WHERE s.summary_date BETWEEN %s AND %s
            """
            params = [start_date, end_date]
            if collector_id:
                query += " AND s.collector_id = %s"
                params.append(collector_id)

            query += " ORDER BY s.summary_date DESC"
            cur.execute(query, tuple(params))

            daily_records = [
                {
                    "date": str(r[0]),
                    "collectorName": r[1],
                    "totalCustomers": r[2],
                    "collectedCount": r[3],
                    "pendingCount": r[4],
                    "missedCount": r[5],
                    "expectedAmount": float(r[6]),
                    "collectedAmount": float(r[7]),
                    "remainingAmount": float(r[8]),
                    "cashAmount": float(r[9]),
                    "upiAmount": float(r[10]),
                    "bankAmount": float(r[11]),
                    "isClosed": r[12],
                }
                for r in cur.fetchall()
            ]

            # Overall aggregated totals
            tot_exp = sum(d["expectedAmount"] for d in daily_records)
            tot_col = sum(d["collectedAmount"] for d in daily_records)
            tot_cash = sum(d["cashAmount"] for d in daily_records)
            tot_upi = sum(d["upiAmount"] for d in daily_records)
            tot_bank = sum(d["bankAmount"] for d in daily_records)

            return jsonify({
                "startDate": str(start_date),
                "endDate": str(end_date),
                "totalDays": len(daily_records),
                "totals": {
                    "expectedAmount": tot_exp,
                    "collectedAmount": tot_col,
                    "remainingAmount": max(0, tot_exp - tot_col),
                    "cashAmount": tot_cash,
                    "upiAmount": tot_upi,
                    "bankAmount": tot_bank,
                    "completionRate": round((tot_col / tot_exp * 100) if tot_exp > 0 else 0, 1),
                },
                "records": daily_records,
            })


# -------------------------------------------------------------------------
# 11. ROUTES & COLLECTORS
# -------------------------------------------------------------------------
@collection_bp.get("/routes")
def get_routes():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.id, r.name, r.area, r.code, r.description,
                       COUNT(c.id) as customer_count
                FROM collection_routes r
                LEFT JOIN collection_customers c ON r.id = c.route_id
                GROUP BY r.id
                ORDER BY r.code ASC
                """
            )
            routes = [
                {
                    "id": r[0],
                    "name": r[1],
                    "area": r[2],
                    "code": r[3],
                    "description": r[4] or "",
                    "customerCount": r[5],
                }
                for r in cur.fetchall()
            ]
            return jsonify({"routes": routes})


@collection_bp.post("/routes")
def create_route():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    area = str(payload.get("area", "")).strip()
    code = str(payload.get("code", "")).strip().upper()
    description = str(payload.get("description", "")).strip()

    if not name or not area or not code:
        return jsonify({"message": "Name, area, and unique route code are required."}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collection_routes (name, area, code, description)
                VALUES (%s, %s, %s, %s)
                RETURNING id;
                """,
                (name, area, code, description),
            )
            route_id = cur.fetchone()[0]
            conn.commit()
            return jsonify({"message": "Route created successfully.", "routeId": route_id}), 201


@collection_bp.get("/collectors")
def get_collectors():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            user_email = request.headers.get("X-User-Email", "").strip().lower()
            user_type = request.headers.get("X-User-Type", "").strip().lower()

            # Strict Agent Privacy Isolation: If non-admin agent asks, ONLY return that agent!
            if user_email and user_type != "admin":
                cur.execute(
                    """
                    SELECT id, full_name, email, business_type, status
                    FROM userdetails
                    WHERE LOWER(email) = %s
                    LIMIT 1
                    """,
                    (user_email,)
                )
                row = cur.fetchone()
                if row and row[3] == "Collection":
                    return jsonify({"collectors": [{
                        "id": row[0],
                        "fullName": row[1],
                        "email": row[2],
                        "businessType": row[3],
                        "status": row[4],
                    }]})

            cur.execute(
                """
                SELECT id, full_name, email, business_type, status
                FROM userdetails
                WHERE business_type = 'Collection' OR user_type = 'customer'
                ORDER BY full_name ASC
                """
            )
            collectors = [
                {
                    "id": r[0],
                    "fullName": r[1],
                    "email": r[2],
                    "businessType": r[3],
                    "status": r[4],
                }
                for r in cur.fetchall()
            ]
            return jsonify({"collectors": collectors})


# -------------------------------------------------------------------------
# 12. INITIALIZATION / SEED HOOK
# -------------------------------------------------------------------------
@collection_bp.post("/seed-demo")
def trigger_seed_demo():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            seed_collection_demo_data(cur)
            conn.commit()
            return jsonify({"message": "Collection demo data (50 customers on 2026-09-07) successfully seeded."})
