"""
AI Assistant (Chatbot) REST API
Mounted at /api/assistant

Answers natural-language questions about the logged-in customer's own
business data (Fruit Sellers / Inventory businesses, or Collection agents)
by grounding an LLM call with their real numbers pulled fresh from the DB.
"""

from datetime import date, timedelta
import json
import math
import os
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request
import psycopg

assistant_bp = Blueprint("assistant", __name__, url_prefix="/api/assistant")


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two GPS points, in km."""
    r = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _estimate_driver_distance_km(rows):
    """Blends real odometer distance (trips with start/end km) with a GPS straight-line
    estimate chained across consecutive trips that only have a location - so quick
    Cash/UPI/voice-logged trips (no odometer reading) still contribute to the total.
    `rows` is a list of (trip_time, start_km, end_km, latitude, longitude) tuples.
    """
    odometer_total = 0.0
    gps_points = []
    for trip_time, start_km, end_km, lat, lon in rows:
        if start_km is not None and end_km is not None and float(end_km) > float(start_km):
            odometer_total += float(end_km) - float(start_km)
        elif lat is not None and lon is not None:
            gps_points.append((trip_time, float(lat), float(lon)))
    gps_points.sort(key=lambda p: p[0])
    gps_total = 0.0
    for i in range(1, len(gps_points)):
        gps_total += _haversine_km(gps_points[i - 1][1], gps_points[i - 1][2], gps_points[i][1], gps_points[i][2])
    return round(odometer_total + gps_total, 1)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_VOICE_AUDIO_CHARS = 8_000_000  # ~6 MB of base64, plenty for a short voice question


def ensure_assistant_schema(cursor):
    """Cache table for the proactive AI daily summary, one row per owner per day."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_daily_summaries (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            summary_date DATE NOT NULL,
            summary_text TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_ai_summary_owner_date UNIQUE (owner_id, summary_date)
        );
    """)


def _call_gemini(system_prompt, message, api_key):
    """Call the free-tier Google Gemini API and return the reply text."""
    url = GEMINI_API_URL.format(model=GEMINI_MODEL) + f"?key={api_key}"
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": message}]}],
        "generationConfig": {"maxOutputTokens": 400, "temperature": 0.3, "thinkingConfig": {"thinkingBudget": 0}},
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    candidates = result.get("candidates", [])
    if not candidates:
        raise RuntimeError("No response candidates from Gemini")
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


def _call_gemini_audio(system_prompt, audio_base64, mime_type, api_key):
    """Sends a short voice question to Gemini (multimodal) and returns its text reply."""
    url = GEMINI_API_URL.format(model=GEMINI_MODEL) + f"?key={api_key}"
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": mime_type, "data": audio_base64}},
                {"text": "Listen to this audio and reply with the JSON described in your instructions."},
            ],
        }],
        "generationConfig": {"maxOutputTokens": 400, "temperature": 0.2, "thinkingConfig": {"thinkingBudget": 0}},
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    candidates = result.get("candidates", [])
    if not candidates:
        raise RuntimeError("No response candidates from Gemini")
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(database_url, options="-c timezone=Asia/Kolkata")


def _build_inventory_context(cur, owner_id):
    today = date.today()
    week_start = today - timedelta(days=6)
    month_start = today.replace(day=1)

    cur.execute(
        """
        SELECT transaction_type, COALESCE(SUM(amount), 0), COALESCE(SUM(quantity), 0), COUNT(*)
        FROM inventory_transactions
        WHERE owner_id = %s AND transaction_date = %s
        GROUP BY transaction_type
        """,
        (owner_id, today),
    )
    today_totals = {r[0]: {"amount": float(r[1] or 0), "qty": float(r[2] or 0), "count": r[3]} for r in cur.fetchall()}
    sale = today_totals.get("SALE", {"amount": 0, "qty": 0, "count": 0})
    wastage = today_totals.get("WASTAGE", {"amount": 0, "qty": 0, "count": 0})
    purchase = today_totals.get("PURCHASE", {"amount": 0, "qty": 0, "count": 0})

    cur.execute(
        """
        SELECT COALESCE(SUM(amount), 0) FROM inventory_transactions
        WHERE owner_id = %s AND transaction_type = 'SALE' AND transaction_date BETWEEN %s AND %s
        """,
        (owner_id, week_start, today),
    )
    week_sales = float(cur.fetchone()[0] or 0)

    cur.execute(
        """
        SELECT COALESCE(SUM(amount), 0) FROM inventory_transactions
        WHERE owner_id = %s AND transaction_type = 'SALE' AND transaction_date >= %s
        """,
        (owner_id, month_start),
    )
    month_sales = float(cur.fetchone()[0] or 0)

    cur.execute(
        """
        SELECT product_name, COALESCE(SUM(amount), 0), COALESCE(SUM(quantity), 0)
        FROM inventory_transactions
        WHERE owner_id = %s AND transaction_type = 'SALE' AND transaction_date = %s
        GROUP BY product_name ORDER BY SUM(amount) DESC LIMIT 5
        """,
        (owner_id, today),
    )
    top_today = [{"product": r[0], "amount": float(r[1] or 0), "quantity": float(r[2] or 0)} for r in cur.fetchall()]

    cur.execute(
        """
        SELECT payment_method, COALESCE(SUM(amount), 0)
        FROM inventory_transactions
        WHERE owner_id = %s AND transaction_type = 'SALE' AND transaction_date = %s
        GROUP BY payment_method
        """,
        (owner_id, today),
    )
    payment_today = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    cur.execute(
        """
        SELECT name, stock_qty, unit, low_stock_threshold, selling_price
        FROM inventory_products
        WHERE owner_id = %s AND status = 'active'
        ORDER BY name ASC
        """,
        (owner_id,),
    )
    all_products = [
        {"name": r[0], "stockQty": float(r[1] or 0), "unit": r[2], "lowStockThreshold": float(r[3] or 0), "sellingPrice": float(r[4] or 0)}
        for r in cur.fetchall()
    ]
    low_stock = [p for p in all_products if p["stockQty"] <= p["lowStockThreshold"]]

    return {
        "businessType": "inventory (Fruit/Vegetable/Retail seller)",
        "today": today.isoformat(),
        "todaySalesAmount": sale["amount"],
        "todaySalesTransactionCount": sale["count"],
        "todaySalesQuantity": sale["qty"],
        "todayWastageValue": wastage["amount"],
        "todayWastageQuantity": wastage["qty"],
        "todayPurchaseValue": purchase["amount"],
        "todayPurchaseQuantity": purchase["qty"],
        "last7DaysSales": week_sales,
        "currentMonthSalesToDate": month_sales,
        "topSellingProductsToday": top_today,
        "paymentMethodSplitToday": payment_today,
        "allProductsWithStock": all_products,
        "lowStockProducts": low_stock,
        "totalActiveProducts": len(all_products),
    }


def _build_collection_context(cur, owner_id):
    today = date.today()

    cur.execute(
        """
        SELECT
            COALESCE(SUM(amount) FILTER (WHERE entry_type = 'IN'), 0) -
            COALESCE(SUM(amount) FILTER (WHERE entry_type = 'OUT'), 0)
        FROM payment_collection
        WHERE collector_id = %s AND payment_date::date = %s AND status = 'SUCCESS'
        """,
        (owner_id, today),
    )
    today_net = float(cur.fetchone()[0] or 0)

    cur.execute(
        "SELECT COUNT(*) FROM collection_customers WHERE collector_id = %s AND status = 'active'",
        (owner_id,),
    )
    total_customers = cur.fetchone()[0] or 0

    cur.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(d.total_due), 0)
        FROM due_amount d
        JOIN collection_customers c ON c.id = d.customer_id
        WHERE c.collector_id = %s AND c.status = 'active' AND d.total_due > 0
        """,
        (owner_id,),
    )
    row = cur.fetchone()
    pending_customers_count = row[0] or 0
    total_pending_amount = float(row[1] or 0)

    cur.execute(
        """
        SELECT c.name, d.total_due
        FROM due_amount d
        JOIN collection_customers c ON c.id = d.customer_id
        WHERE c.collector_id = %s AND c.status = 'active' AND d.total_due > 0
        ORDER BY d.total_due DESC LIMIT 5
        """,
        (owner_id,),
    )
    top_overdue = [{"name": r[0], "pendingAmount": float(r[1] or 0)} for r in cur.fetchall()]

    cur.execute(
        """
        SELECT COUNT(*) FROM collection_schedule
        WHERE collector_id = %s AND schedule_date = %s AND status = 'COLLECTED'
        """,
        (owner_id, today),
    )
    collected_today_count = cur.fetchone()[0] or 0

    return {
        "businessType": "collection agent (micro-lending / door-to-door collection)",
        "today": today.isoformat(),
        "todayNetCollectedAmount": today_net,
        "customersCollectedFromToday": collected_today_count,
        "totalActiveCustomers": total_customers,
        "pendingCustomersCount": pending_customers_count,
        "totalPendingAmountAcrossAllCustomers": total_pending_amount,
        "topOverdueCustomers": top_overdue,
    }


def _build_driver_context(cur, owner_id):
    today = date.today()

    cur.execute(
        """
        SELECT payment_mode, COALESCE(SUM(fare), 0), COUNT(*)
        FROM driver_trips
        WHERE owner_id = %s AND NOT deleted AND trip_time::date = %s
        GROUP BY payment_mode
        """,
        (owner_id, today),
    )
    today_by_mode = {r[0]: {"amount": float(r[1] or 0), "count": r[2]} for r in cur.fetchall()}
    today_total = sum(v["amount"] for v in today_by_mode.values())
    today_count = sum(v["count"] for v in today_by_mode.values())

    cur.execute(
        """
        SELECT trip_time, start_km, end_km, latitude, longitude
        FROM driver_trips
        WHERE owner_id = %s AND NOT deleted AND trip_time::date = %s
        """,
        (owner_id, today),
    )
    today_distance_km = _estimate_driver_distance_km(cur.fetchall())

    week_start = today - timedelta(days=6)
    cur.execute(
        """
        SELECT trip_time::date, COALESCE(SUM(fare), 0)
        FROM driver_trips
        WHERE owner_id = %s AND NOT deleted AND trip_time::date BETWEEN %s AND %s
        GROUP BY trip_time::date ORDER BY trip_time::date
        """,
        (owner_id, week_start, today),
    )
    last_7_days_daily_earnings = [{"date": r[0].isoformat(), "amount": float(r[1] or 0)} for r in cur.fetchall()]
    last_7_days_total_earnings = sum(d["amount"] for d in last_7_days_daily_earnings)

    def _sum_fuel(start_date, end_date):
        cur.execute(
            "SELECT COALESCE(SUM(total_cost), 0) FROM driver_fuel_logs "
            "WHERE owner_id = %s AND NOT deleted AND fuel_time::date BETWEEN %s AND %s",
            (owner_id, start_date, end_date),
        )
        return float(cur.fetchone()[0] or 0)

    def _sum_earnings(start_date, end_date):
        cur.execute(
            "SELECT COALESCE(SUM(fare), 0) FROM driver_trips "
            "WHERE owner_id = %s AND NOT deleted AND trip_time::date BETWEEN %s AND %s",
            (owner_id, start_date, end_date),
        )
        return float(cur.fetchone()[0] or 0)

    today_fuel_cost = _sum_fuel(today, today)
    last_7_days_fuel_cost = _sum_fuel(week_start, today)

    prev_week_start = week_start - timedelta(days=7)
    prev_week_end = week_start - timedelta(days=1)
    previous_7_days_total_earnings = _sum_earnings(prev_week_start, prev_week_end)
    previous_7_days_fuel_cost = _sum_fuel(prev_week_start, prev_week_end)

    month_start = today.replace(day=1)
    month_to_date_earnings = _sum_earnings(month_start, today)
    month_to_date_fuel_cost = _sum_fuel(month_start, today)

    return {
        "businessType": "auto-rickshaw driver",
        "today": today.isoformat(),
        "todayTotalEarnings": today_total,
        "todayTripCount": today_count,
        "todayDistanceKm": today_distance_km,
        "todayEarningsByPaymentMode": {k: v["amount"] for k, v in today_by_mode.items()},
        "todayFuelCost": today_fuel_cost,
        "todayNetProfit": today_total - today_fuel_cost,
        "last7DaysDailyEarnings": last_7_days_daily_earnings,
        "last7DaysTotalEarnings": last_7_days_total_earnings,
        "last7DaysFuelCost": last_7_days_fuel_cost,
        "previous7DaysTotalEarnings": previous_7_days_total_earnings,
        "previous7DaysFuelCost": previous_7_days_fuel_cost,
        "monthToDateEarnings": month_to_date_earnings,
        "monthToDateFuelCost": month_to_date_fuel_cost,
    }


def _is_driver_business(bt_lower):
    return any(k in bt_lower for k in ("auto", "driver", "rickshaw"))


def _is_travel_business(bt_lower):
    return "travel" in bt_lower or "bus" in bt_lower


def _build_travel_context(cur, owner_id):
    today = date.today()

    cur.execute(
        "SELECT COUNT(*) FROM travel_trips WHERE owner_id = %s AND status != 'cancelled' AND travel_date = %s",
        (owner_id, today),
    )
    today_trip_count = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COALESCE(SUM(b.fare) FILTER (WHERE b.payment_status = 'paid'), 0),
               COALESCE(SUM(b.fare) FILTER (WHERE b.payment_status = 'pending'), 0),
               COUNT(*) FILTER (WHERE b.payment_status = 'paid')
        FROM travel_seat_bookings b
        JOIN travel_trips t ON b.trip_id = t.id
        WHERE t.owner_id = %s AND b.booked_at::date = %s
        """,
        (owner_id, today),
    )
    today_row = cur.fetchone()
    today_revenue = float(today_row[0] or 0)
    today_pending = float(today_row[1] or 0)
    today_booking_count = today_row[2]

    week_start = today - timedelta(days=6)
    cur.execute(
        """
        SELECT b.booked_at::date, COALESCE(SUM(b.fare), 0)
        FROM travel_seat_bookings b
        JOIN travel_trips t ON b.trip_id = t.id
        WHERE t.owner_id = %s AND b.payment_status = 'paid' AND b.booked_at::date BETWEEN %s AND %s
        GROUP BY b.booked_at::date ORDER BY b.booked_at::date
        """,
        (owner_id, week_start, today),
    )
    last_7_days_daily_revenue = [{"date": r[0].isoformat(), "amount": float(r[1] or 0)} for r in cur.fetchall()]
    last_7_days_total_revenue = sum(d["amount"] for d in last_7_days_daily_revenue)

    def _sum_fuel(start_d, end_d):
        cur.execute(
            "SELECT COALESCE(SUM(total_cost), 0) FROM travel_fuel_logs WHERE owner_id = %s AND fuel_date BETWEEN %s AND %s",
            (owner_id, start_d, end_d),
        )
        return float(cur.fetchone()[0] or 0)

    def _sum_revenue(start_d, end_d):
        cur.execute(
            """
            SELECT COALESCE(SUM(b.fare), 0)
            FROM travel_seat_bookings b
            JOIN travel_trips t ON b.trip_id = t.id
            WHERE t.owner_id = %s AND b.payment_status = 'paid' AND b.booked_at::date BETWEEN %s AND %s
            """,
            (owner_id, start_d, end_d),
        )
        return float(cur.fetchone()[0] or 0)

    today_fuel_cost = _sum_fuel(today, today)
    last_7_days_fuel_cost = _sum_fuel(week_start, today)

    month_start = today.replace(day=1)
    month_to_date_revenue = _sum_revenue(month_start, today)
    month_to_date_fuel_cost = _sum_fuel(month_start, today)

    cur.execute(
        "SELECT COUNT(*) FROM travel_trips WHERE owner_id = %s AND status != 'cancelled' AND travel_date > %s",
        (owner_id, today),
    )
    upcoming_trip_count = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM travel_vehicles WHERE owner_id = %s AND status = 'active'",
        (owner_id,),
    )
    active_vehicle_count = cur.fetchone()[0]

    return {
        "businessType": "bus travel operator",
        "today": today.isoformat(),
        "todayTripCount": today_trip_count,
        "todayBookingCount": today_booking_count,
        "todayRevenue": today_revenue,
        "todayPendingAmount": today_pending,
        "todayFuelCost": today_fuel_cost,
        "todayNetProfit": today_revenue - today_fuel_cost,
        "last7DaysDailyRevenue": last_7_days_daily_revenue,
        "last7DaysTotalRevenue": last_7_days_total_revenue,
        "last7DaysFuelCost": last_7_days_fuel_cost,
        "monthToDateRevenue": month_to_date_revenue,
        "monthToDateFuelCost": month_to_date_fuel_cost,
        "upcomingTripCount": upcoming_trip_count,
        "activeVehicleCount": active_vehicle_count,
    }


def _resolve_business_context(cur, owner_id):
    """Looks up the owner's business type and builds their grounding data context.
    Returns (full_name, business_type, context), or None if the account doesn't exist."""
    cur.execute("SELECT full_name, business_type FROM userdetails WHERE id = %s", (owner_id,))
    row = cur.fetchone()
    if not row:
        return None
    full_name, business_type = row
    bt_lower = (business_type or "").lower()

    if any(k in bt_lower for k in ("fruit", "vegetable", "retail")):
        context = _build_inventory_context(cur, owner_id)
    elif "collection" in bt_lower:
        context = _build_collection_context(cur, owner_id)
    elif _is_travel_business(bt_lower):
        context = _build_travel_context(cur, owner_id)
    elif _is_driver_business(bt_lower):
        context = _build_driver_context(cur, owner_id)
    else:
        context = {
            "businessType": business_type or "unknown",
            "note": "Detailed data tracking isn't available for this business type yet.",
        }
    return full_name, business_type, context


@assistant_bp.post("/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    owner_id = payload.get("ownerId") or payload.get("owner_id")
    message = str(payload.get("message", "")).strip()

    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not message:
        return jsonify({"error": "message is required"}), 400

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return jsonify({
            "reply": "The AI assistant isn't set up yet. Ask the app owner to add a free GEMINI_API_KEY (from aistudio.google.com/apikey) to the backend configuration to enable me."
        })

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                resolved = _resolve_business_context(cur, owner_id)
                if not resolved:
                    return jsonify({"error": "Account not found"}), 404
                full_name, business_type, context = resolved
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    system_prompt = (
        f"You are the AI Assistant inside the AutoLedger app, answering questions for {full_name or 'the business owner'} "
        f"strictly about their own business, using ONLY the JSON data below. Today's date is {date.today().isoformat()}.\n"
        "Rules:\n"
        "- Never invent or guess any number, fact, or detail that isn't explicitly present in the data. No hallucination.\n"
        "- Answer only what was asked. Do not add extra facts, suggestions, tips, or unrelated details the user didn't ask for.\n"
        "- Simple social pleasantries (hi, hello, thank you, thanks, how are you, good morning, bye, etc.) are NOT "
        "information requests — reply to these naturally and warmly in a short, friendly line, without mentioning data.\n"
        "- For anything else — general knowledge, opinions/advice, or business questions the data doesn't cover — "
        "reply with EXACTLY: \"I don't have information about that.\" Nothing else.\n"
        "- Format money with the ₹ symbol and comma-separated numbers (e.g. ₹5,250).\n"
        "- Keep answers short: 1-2 sentences, plain text, no markdown headers or bullet lists.\n\n"
        f"DATA:\n{json.dumps(context)}"
    )

    try:
        reply_text = _call_gemini(system_prompt, message, api_key)
        if not reply_text:
            reply_text = "Sorry, I couldn't generate a response. Please try again."
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"AI assistant HTTP error {e.code}: {err_body}")
        if e.code in (400, 403):
            friendly = "AI assistant isn't configured correctly (invalid API key). Ask the app owner to check the GEMINI_API_KEY."
        elif e.code == 429:
            friendly = "AI assistant is getting too many requests right now (free tier limit). Please try again in a minute."
        else:
            friendly = "AI assistant is unavailable right now. Please try again in a moment."
        return jsonify({"reply": friendly})
    except Exception as e:
        print(f"AI assistant error: {e}")
        return jsonify({"reply": "AI assistant is unavailable right now. Please try again in a moment."})

    return jsonify({"reply": reply_text})


@assistant_bp.post("/voice-chat")
def voice_chat():
    """Lets the owner ask a short spoken question about their own business data
    instead of typing it - e.g. 'how much fuel did I spend this month'. One
    multimodal Gemini call both transcribes the question and answers it, grounded
    in the same data context as the text /chat endpoint. Never saves anything."""
    payload = request.get_json(silent=True) or {}
    owner_id = payload.get("ownerId") or payload.get("owner_id")
    audio_base64 = str(payload.get("audioBase64", ""))
    mime_type = str(payload.get("mimeType", "audio/m4a")).strip() or "audio/m4a"

    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not audio_base64:
        return jsonify({"error": "audioBase64 is required"}), 400
    if len(audio_base64) > MAX_VOICE_AUDIO_CHARS:
        return jsonify({"error": "Recording is too long. Please keep it under a few seconds."}), 200

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return jsonify({
            "reply": "The AI assistant isn't set up yet. Ask the app owner to add a free GEMINI_API_KEY "
                     "(from aistudio.google.com/apikey) to the backend configuration to enable me."
        })

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                resolved = _resolve_business_context(cur, owner_id)
                if not resolved:
                    return jsonify({"error": "Account not found"}), 404
                full_name, business_type, context = resolved
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    system_prompt = (
        f"You are the AI Assistant inside the AutoLedger app, answering a SPOKEN question from "
        f"{full_name or 'the business owner'} about their own business, using ONLY the JSON data below. "
        f"The question may be in English, Hindi, or Marathi. Today's date is {date.today().isoformat()}.\n"
        "Rules:\n"
        "- First, transcribe what was said as accurately as possible, in the original language.\n"
        "- If the audio is silent, unintelligible, or just background noise, set transcript to an empty "
        "string and reply to \"Sorry, I couldn't hear a question. Please try again.\" Do NOT guess a question "
        "that wasn't actually asked.\n"
        "- Never invent or guess any number, fact, or detail that isn't explicitly present in the data. No hallucination.\n"
        "- Answer only what was asked. Do not add extra facts, suggestions, tips, or unrelated details.\n"
        "- Simple social pleasantries (hi, hello, thank you, how are you, etc.) are NOT information requests — "
        "reply to these naturally and warmly in a short, friendly line, without mentioning data.\n"
        "- For anything else the data doesn't cover, reply with EXACTLY: \"I don't have information about that.\"\n"
        "- Format money with the ₹ symbol and comma-separated numbers (e.g. ₹5,250).\n"
        "- Keep the reply short: 1-2 sentences, plain text, no markdown.\n"
        "- Reply with STRICT JSON only, no markdown fences, in exactly this shape: "
        '{"transcript": "<what you heard>", "reply": "<your answer>"}\n\n'
        f"DATA:\n{json.dumps(context)}"
    )

    try:
        raw_reply = _call_gemini_audio(system_prompt, audio_base64, mime_type, api_key)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"AI voice assistant HTTP error {e.code}: {err_body}")
        return jsonify({"reply": "AI assistant is unavailable right now. Please try again in a moment."})
    except Exception as e:
        print(f"AI voice assistant error: {e}")
        return jsonify({"reply": "AI assistant is unavailable right now. Please try again in a moment."})

    cleaned = raw_reply.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return jsonify({
            "transcript": "",
            "reply": "Sorry, I couldn't process that. Please try again or type your question instead.",
        })

    return jsonify({
        "transcript": parsed.get("transcript", ""),
        "reply": parsed.get("reply") or "Sorry, I couldn't generate a response. Please try again.",
    })


def _build_yesterday_summary_context(cur, owner_id):
    """Yesterday's inventory activity plus a 7-day baseline average for comparison."""
    yesterday = date.today() - timedelta(days=1)
    baseline_start = yesterday - timedelta(days=7)
    baseline_end = yesterday - timedelta(days=1)

    cur.execute(
        """
        SELECT transaction_type, COALESCE(SUM(amount), 0), COALESCE(SUM(quantity), 0), COUNT(*)
        FROM inventory_transactions
        WHERE owner_id = %s AND transaction_date = %s
        GROUP BY transaction_type
        """,
        (owner_id, yesterday),
    )
    totals = {r[0]: {"amount": float(r[1] or 0), "qty": float(r[2] or 0), "count": r[3]} for r in cur.fetchall()}
    sale = totals.get("SALE", {"amount": 0, "qty": 0, "count": 0})
    wastage = totals.get("WASTAGE", {"amount": 0, "qty": 0, "count": 0})
    purchase = totals.get("PURCHASE", {"amount": 0, "qty": 0, "count": 0})
    has_data = (sale["count"] + wastage["count"] + purchase["count"]) > 0

    cur.execute(
        """
        SELECT product_name, COALESCE(SUM(amount), 0)
        FROM inventory_transactions
        WHERE owner_id = %s AND transaction_type = 'SALE' AND transaction_date = %s
        GROUP BY product_name ORDER BY SUM(amount) DESC LIMIT 1
        """,
        (owner_id, yesterday),
    )
    top_row = cur.fetchone()
    top_product = {"name": top_row[0], "amount": float(top_row[1] or 0)} if top_row else None

    cur.execute(
        """
        SELECT transaction_type, COALESCE(AVG(daily_amt), 0)
        FROM (
            SELECT transaction_type, transaction_date, SUM(amount) AS daily_amt
            FROM inventory_transactions
            WHERE owner_id = %s AND transaction_type IN ('SALE', 'WASTAGE')
                AND transaction_date BETWEEN %s AND %s
            GROUP BY transaction_type, transaction_date
        ) sub
        GROUP BY transaction_type
        """,
        (owner_id, baseline_start, baseline_end),
    )
    baseline = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    context = {
        "yesterdayDate": yesterday.isoformat(),
        "yesterdaySalesAmount": sale["amount"],
        "yesterdaySalesTransactionCount": sale["count"],
        "yesterdayWastageValue": wastage["amount"],
        "yesterdayWastageQuantity": wastage["qty"],
        "yesterdayPurchaseValue": purchase["amount"],
        "topSellingProductYesterday": top_product,
        "averageDailySalesLast7Days": round(baseline.get("SALE", 0), 2),
        "averageDailyWastageLast7Days": round(baseline.get("WASTAGE", 0), 2),
    }
    return context, has_data


def _build_driver_yesterday_summary_context(cur, owner_id):
    """Yesterday's driver earnings plus a 7-day baseline average for comparison."""
    yesterday = date.today() - timedelta(days=1)
    baseline_start = yesterday - timedelta(days=7)
    baseline_end = yesterday - timedelta(days=1)

    cur.execute(
        """
        SELECT payment_mode, COALESCE(SUM(fare), 0), COUNT(*)
        FROM driver_trips
        WHERE owner_id = %s AND NOT deleted AND trip_time::date = %s
        GROUP BY payment_mode
        """,
        (owner_id, yesterday),
    )
    by_mode = {r[0]: {"amount": float(r[1] or 0), "count": r[2]} for r in cur.fetchall()}
    yesterday_total = sum(v["amount"] for v in by_mode.values())
    yesterday_count = sum(v["count"] for v in by_mode.values())
    has_data = yesterday_count > 0

    cur.execute(
        """
        SELECT COALESCE(AVG(daily_amt), 0)
        FROM (
            SELECT trip_time::date AS d, SUM(fare) AS daily_amt
            FROM driver_trips
            WHERE owner_id = %s AND NOT deleted AND trip_time::date BETWEEN %s AND %s
            GROUP BY trip_time::date
        ) sub
        """,
        (owner_id, baseline_start, baseline_end),
    )
    avg_daily_earnings = float(cur.fetchone()[0] or 0)

    context = {
        "yesterdayDate": yesterday.isoformat(),
        "yesterdayTotalEarnings": yesterday_total,
        "yesterdayTripCount": yesterday_count,
        "yesterdayEarningsByPaymentMode": {k: v["amount"] for k, v in by_mode.items()},
        "averageDailyEarningsLast7Days": round(avg_daily_earnings, 2),
    }
    return context, has_data


@assistant_bp.get("/daily-summary")
def daily_summary():
    owner_id = request.args.get("owner_id") or request.args.get("ownerId")
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400

    yesterday = date.today() - timedelta(days=1)
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT full_name, business_type FROM userdetails WHERE id = %s", (owner_id,))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Account not found"}), 404
                full_name, business_type = row
                bt_lower = (business_type or "").lower()

                cur.execute(
                    "SELECT summary_text FROM ai_daily_summaries WHERE owner_id = %s AND summary_date = %s",
                    (owner_id, yesterday),
                )
                cached = cur.fetchone()
                if cached:
                    return jsonify({"summary": cached[0], "date": yesterday.isoformat()})

                is_inventory = any(k in bt_lower for k in ("fruit", "vegetable", "retail"))
                is_driver = _is_driver_business(bt_lower)
                if not (is_inventory or is_driver):
                    return jsonify({"summary": None, "date": yesterday.isoformat()})

                if is_driver:
                    context, has_data = _build_driver_yesterday_summary_context(cur, owner_id)
                else:
                    context, has_data = _build_yesterday_summary_context(cur, owner_id)

                if not has_data or not api_key:
                    return jsonify({"summary": None, "date": yesterday.isoformat()})

                if is_driver:
                    system_prompt = (
                        f"You are the AI Assistant inside the AutoLedger app, writing a short proactive daily "
                        f"earnings summary for {full_name or 'the driver'}, based ONLY on the JSON data below.\n"
                        "Write 2-3 sentences, warm and conversational, like a helpful assistant greeting them about "
                        "yesterday's driving income. Mention yesterday's total earnings and how many trips/payments "
                        "were recorded, and the Cash vs UPI split if both are present. Call out anything notable by "
                        "comparing yesterday's earnings to the 7-day average (e.g. much higher/lower than usual), "
                        "with a brief encouraging or actionable note if relevant. Never invent numbers not in the "
                        "data. Format money with ₹ and comma separators. Plain text only, no markdown.\n\n"
                        f"DATA:\n{json.dumps(context)}"
                    )
                else:
                    system_prompt = (
                        f"You are the AI Assistant inside the AutoLedger app, writing a short proactive daily business "
                        f"summary for {full_name or 'the business owner'}, based ONLY on the JSON data below.\n"
                        "Write 2-3 sentences, warm and conversational, like a helpful assistant greeting them in the "
                        "morning about yesterday's business. Mention yesterday's total sales and the best-selling "
                        "product if any. Call out anything notable by comparing yesterday's sales or wastage to the "
                        "7-day average (e.g. much higher/lower than usual), with a brief actionable tip if relevant. "
                        "Never invent numbers not in the data. Format money with ₹ and comma separators. "
                        "Plain text only, no markdown.\n\n"
                        f"DATA:\n{json.dumps(context)}"
                    )

                try:
                    summary_text = _call_gemini(system_prompt, "Write today's proactive summary.", api_key)
                except Exception as e:
                    print(f"Daily summary generation error: {e}")
                    return jsonify({"summary": None, "date": yesterday.isoformat()})

                if not summary_text:
                    return jsonify({"summary": None, "date": yesterday.isoformat()})

                cur.execute(
                    """
                    INSERT INTO ai_daily_summaries (owner_id, summary_date, summary_text)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (owner_id, summary_date) DO NOTHING
                    """,
                    (owner_id, yesterday, summary_text),
                )
                conn.commit()
        return jsonify({"summary": summary_text, "date": yesterday.isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
