"""
One-time script to seed exact Collection Dashboard metrics:
- 2026-09-07:
  Target: 50,000 | Collected: 42,000 | Remaining: 8,000
  People: 50 Total, 42 Collected, 3 Pending, 5 Missed
  Payment Methods: Cash 25,000, UPI 17,000, Bank Transfer 0
- August 2026 (Last Month): Weekly beats with collected & missed data
- July 2026: Q3 data
"""

from datetime import date, datetime
from decimal import Decimal
import os
from dotenv import load_dotenv
import psycopg

load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()

print("Resolving collector...", flush=True)
cur.execute("SELECT id FROM userdetails WHERE LOWER(email) = 'riya@gmail.com' LIMIT 1")
row = cur.fetchone()
collector_id = row[0] if row else 36

cur.execute(
    "SELECT id, route_id, name, mobile FROM collection_customers WHERE collector_id = %s ORDER BY id ASC LIMIT 50",
    (collector_id,),
)
customers = cur.fetchall()
print(f"Found {len(customers)} customers for collector {collector_id}.", flush=True)

today = date(2026, 9, 7)

# 1. Update 50 customers on today
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

    cur.execute(
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
    sched_id = cur.fetchone()[0]

    if status == "COLLECTED" and pay_method:
        cur.execute(
            "SELECT id FROM payment_collection WHERE schedule_id = %s AND customer_id = %s LIMIT 1",
            (sched_id, cid),
        )
        pay_row = cur.fetchone()
        if not pay_row:
            cur.execute(
                """
                INSERT INTO payment_collection (schedule_id, customer_id, collector_id, amount, payment_method, transaction_ref, entry_type, notes, status, payment_date)
                VALUES (%s, %s, %s, %s, %s, %s, 'IN', %s, 'SUCCESS', '2026-09-07 10:00:00'::timestamptz + (interval '12 minutes' * %s))
                RETURNING id;
                """,
                (sched_id, cid, collector_id, col_amt, pay_method, f"TXN-20260907-{idx+1:04d}", f"EMI received via {pay_method}", idx),
            )
            pid = cur.fetchone()[0]
            rec_no = f"REC-20260907-{idx+1:04d}"
            cur.execute(
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
        cur.execute(
            """
            INSERT INTO collection_records (schedule_id, customer_id, collector_id, visit_date, status, reason, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING;
            """,
            (sched_id, cid, collector_id, today, status, r_title, r_notes),
        )

print("Today's 50 records processed.", flush=True)

# 2. August 2026 (Last Month)
aug_dates = [date(2026, 8, 8), date(2026, 8, 15), date(2026, 8, 22), date(2026, 8, 29)]
for a_idx, a_date in enumerate(aug_dates):
    batch = customers[a_idx * 10 : (a_idx + 1) * 10]
    for c_idx, (cid, rid, cname, cmobile) in enumerate(batch):
        exp = Decimal("1000.00")
        is_col = (c_idx % 8 != 7)
        stat = "COLLECTED" if is_col else "MISSED"
        amt = exp if is_col else Decimal("0.00")
        cur.execute(
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
        s_id = cur.fetchone()[0]
        if is_col:
            pm = "CASH" if c_idx % 2 == 0 else "UPI"
            cur.execute("SELECT id FROM payment_collection WHERE schedule_id = %s LIMIT 1", (s_id,))
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO payment_collection (schedule_id, customer_id, collector_id, amount, payment_method, transaction_ref, entry_type, notes, status, payment_date)
                    VALUES (%s, %s, %s, %s, %s, %s, 'IN', %s, 'SUCCESS', %s)
                    RETURNING id;
                    """,
                    (s_id, cid, collector_id, amt, pm, f"TXN-AUG-{a_idx}-{c_idx}", "August monthly EMI", datetime(a_date.year, a_date.month, a_date.day, 11, 0, 0)),
                )
                p_id = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO payment_receipts (payment_id, receipt_number, customer_id, collector_id, amount, payment_method, transaction_ref, notes, receipt_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING;
                    """,
                    (p_id, f"REC-AUG-{a_idx}-{c_idx:03d}", cid, collector_id, amt, pm, f"TXN-AUG-{a_idx}-{c_idx}", "August Receipt", datetime(a_date.year, a_date.month, a_date.day, 11, 0, 0)),
                )

print("August records processed.", flush=True)

# 3. July 2026
july_date = date(2026, 7, 20)
for c_idx, (cid, rid, cname, cmobile) in enumerate(customers[:15]):
    exp = Decimal("1000.00")
    cur.execute(
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
    row = cur.fetchone()
    if row:
        s_id = row[0]
        cur.execute("SELECT id FROM payment_collection WHERE schedule_id = %s LIMIT 1", (s_id,))
        if not cur.fetchone():
            cur.execute(
                """
                INSERT INTO payment_collection (schedule_id, customer_id, collector_id, amount, payment_method, transaction_ref, entry_type, status, payment_date)
                VALUES (%s, %s, %s, %s, 'CASH', %s, 'IN', 'SUCCESS', %s);
                """,
                (s_id, cid, collector_id, exp, f"TXN-JUL-{c_idx}", datetime(2026, 7, 20, 14, 0, 0)),
            )

# Recalculate daily collection summary for all dates
from collection_service import recalculate_daily_summary
recalculate_daily_summary(cur, collector_id, today)
for d in aug_dates:
    recalculate_daily_summary(cur, collector_id, d)
recalculate_daily_summary(cur, collector_id, july_date)

conn.commit()
print("All dashboard data successfully committed!", flush=True)
conn.close()
