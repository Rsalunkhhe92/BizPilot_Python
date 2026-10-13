"""
Auto Driver REST API (Flask Blueprint), mounted at /api/driver.

- Trips are stored server-side (driver_trips).
- The driver's own static UPI QR image is stored as a base64 data URI (driver_upi_qr).
- QR payment tracking: for each fare a single-use, fixed-amount Razorpay UPI QR is created
  (driver_qr_payments). Payment is confirmed by polling Razorpay (works without a public
  URL) and/or by the Razorpay webhook; on confirmation a UPI trip is recorded automatically.
- Merchant account (driver_merchant_accounts): each driver gets one permanent UPI QR and every
  payment on it is saved (driver_merchant_payments) and recorded as a UPI trip automatically.
  Default (DRIVER_MERCHANT_MODE=own_keys): the driver connects his own Razorpay account by
  entering its API keys, stored encrypted; the QR is created in his account, so the money
  goes straight to him. Alternative (platform): QRs are created in the platform's account
  and the money is paid out manually or, with Route, transferred to a linked account.
"""

import base64
import hashlib
import hmac
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
import psycopg

from cryptography.fernet import Fernet, InvalidToken

from auth_token import verify_token

driver_bp = Blueprint("driver", __name__, url_prefix="/api/driver")

MAX_QR_CHARS = 2_500_000  # ~1.8 MB of base64, plenty for a QR image
MAX_VOICE_AUDIO_CHARS = 8_000_000  # ~6 MB of base64, plenty for a short voice note

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _call_gemini_audio(system_prompt, audio_base64, mime_type, api_key):
    """Sends a short voice note to Gemini (multimodal) and returns its text reply."""
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
        "generationConfig": {"maxOutputTokens": 200, "temperature": 0.1, "thinkingConfig": {"thinkingBudget": 0}},
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


def get_owner_id(payload=None):
    for c in (
        request.args.get("owner_id"),
        (payload or {}).get("ownerId"),
        request.headers.get("X-User-Id"),
    ):
        if c not in (None, ""):
            try:
                return int(c)
            except (TypeError, ValueError):
                continue
    return None


def ensure_driver_schema(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS driver_upi_qr (
            owner_id BIGINT PRIMARY KEY REFERENCES userdetails(id) ON DELETE CASCADE,
            qr_image TEXT NOT NULL,
            upi_id VARCHAR(100),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS driver_trips (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            route TEXT NOT NULL DEFAULT '',
            fare NUMERIC(10, 2) NOT NULL CHECK (fare > 0),
            payment_mode VARCHAR(10) NOT NULL DEFAULT 'Cash' CHECK (payment_mode IN ('Cash', 'UPI')),
            start_km NUMERIC(10, 1),
            end_km NUMERIC(10, 1),
            source VARCHAR(10) NOT NULL DEFAULT 'MANUAL' CHECK (source IN ('MANUAL', 'QR')),
            qr_payment_id BIGINT,
            trip_time TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_driver_trips_owner_time ON driver_trips(owner_id, trip_time DESC);

        -- SMS-detected UPI credits: external_ref (SMS id + timestamp) makes recording idempotent
        ALTER TABLE driver_trips ADD COLUMN IF NOT EXISTS external_ref VARCHAR(120);
        ALTER TABLE driver_trips ADD COLUMN IF NOT EXISTS deleted BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE driver_trips ADD COLUMN IF NOT EXISTS location_name VARCHAR(200) NOT NULL DEFAULT '';
        ALTER TABLE driver_trips ADD COLUMN IF NOT EXISTS latitude NUMERIC(9, 6);
        ALTER TABLE driver_trips ADD COLUMN IF NOT EXISTS longitude NUMERIC(9, 6);
        ALTER TABLE driver_trips DROP CONSTRAINT IF EXISTS driver_trips_source_check;
        ALTER TABLE driver_trips ADD CONSTRAINT driver_trips_source_check CHECK (source IN ('MANUAL', 'QR', 'SMS', 'NOTIF'));
        CREATE UNIQUE INDEX IF NOT EXISTS uq_driver_trips_external_ref ON driver_trips(owner_id, external_ref) WHERE external_ref IS NOT NULL;

        CREATE TABLE IF NOT EXISTS driver_qr_payments (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            amount NUMERIC(10, 2) NOT NULL CHECK (amount > 0),
            route TEXT NOT NULL DEFAULT '',
            gateway_qr_id VARCHAR(60) NOT NULL UNIQUE,
            image_url TEXT,
            status VARCHAR(12) NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'PAID', 'EXPIRED', 'CANCELLED')),
            gateway_payment_id VARCHAR(60) UNIQUE,
            trip_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            paid_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_driver_qr_owner ON driver_qr_payments(owner_id, created_at DESC);

        -- Razorpay Route linked account + permanent QR per driver. PAN and bank account
        -- number go straight to the gateway; only the last 4 characters are kept here.
        CREATE TABLE IF NOT EXISTS driver_merchant_accounts (
            owner_id BIGINT PRIMARY KEY REFERENCES userdetails(id) ON DELETE CASCADE,
            legal_name VARCHAR(120) NOT NULL,
            phone VARCHAR(15) NOT NULL,
            email VARCHAR(150) NOT NULL,
            pan_last4 VARCHAR(4) NOT NULL DEFAULT '',
            bank_last4 VARCHAR(4) NOT NULL DEFAULT '',
            ifsc VARCHAR(11) NOT NULL DEFAULT '',
            gateway_account_id VARCHAR(60) UNIQUE,
            gateway_product_id VARCHAR(60),
            onboarding_step SMALLINT NOT NULL DEFAULT 0,
            status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
            status_note TEXT NOT NULL DEFAULT '',
            gateway_qr_id VARCHAR(60) UNIQUE,
            qr_image_url TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS driver_merchant_payments (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            gateway_payment_id VARCHAR(60) NOT NULL UNIQUE,
            gateway_qr_id VARCHAR(60) NOT NULL,
            amount NUMERIC(10, 2) NOT NULL CHECK (amount > 0),
            payer_vpa VARCHAR(120) NOT NULL DEFAULT '',
            rrn VARCHAR(40) NOT NULL DEFAULT '',
            transfer_id VARCHAR(60),
            transfer_amount NUMERIC(10, 2),
            transfer_status VARCHAR(12) NOT NULL DEFAULT 'PENDING' CHECK (transfer_status IN ('PENDING', 'SENDING', 'CREATED', 'FAILED', 'MANUAL', 'DIRECT')),
            transfer_error TEXT NOT NULL DEFAULT '',
            transfer_attempts SMALLINT NOT NULL DEFAULT 0,
            transfer_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            trip_id BIGINT,
            paid_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_driver_merchant_payments_owner ON driver_merchant_payments(owner_id, paid_at DESC);

        -- Without Route the platform collects the money and pays drivers out itself, so the
        -- full account number is needed here and such payments are marked MANUAL.
        ALTER TABLE driver_merchant_accounts ADD COLUMN IF NOT EXISTS bank_account_number VARCHAR(20);
        ALTER TABLE driver_merchant_accounts ADD COLUMN IF NOT EXISTS beneficiary_name VARCHAR(120) NOT NULL DEFAULT '';
        -- 'test' or 'live': which Razorpay keys the QR was created with. A QR only exists in
        -- its own mode, so it is reissued when the keys are switched.
        ALTER TABLE driver_merchant_accounts ADD COLUMN IF NOT EXISTS gateway_mode VARCHAR(4);
        ALTER TABLE driver_merchant_payments ADD COLUMN IF NOT EXISTS refunded_amount NUMERIC(10, 2) NOT NULL DEFAULT 0;
        -- The driver's own Razorpay account. Secrets are encrypted with DRIVER_KEYS_ENCRYPTION_KEY,
        -- which lives only in the server environment. Payments on such an account are DIRECT:
        -- the money is already the driver's, nothing is owed or transferred.
        ALTER TABLE driver_merchant_accounts ADD COLUMN IF NOT EXISTS own_key_id VARCHAR(40);
        ALTER TABLE driver_merchant_accounts ADD COLUMN IF NOT EXISTS own_key_secret_enc TEXT;
        ALTER TABLE driver_merchant_accounts ADD COLUMN IF NOT EXISTS own_webhook_secret_enc TEXT;

        -- Every webhook delivery, logged before it is processed. event_id is the gateway's
        -- own id, so a retried delivery updates the same row instead of being handled twice.
        CREATE TABLE IF NOT EXISTS gateway_webhook_events (
            id BIGSERIAL PRIMARY KEY,
            event_id VARCHAR(80) NOT NULL UNIQUE,
            event_type VARCHAR(60) NOT NULL DEFAULT '',
            payload JSONB NOT NULL,
            status VARCHAR(10) NOT NULL DEFAULT 'RECEIVED' CHECK (status IN ('RECEIVED', 'PROCESSED', 'IGNORED', 'FAILED')),
            error TEXT NOT NULL DEFAULT '',
            attempts SMALLINT NOT NULL DEFAULT 1,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            processed_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_gateway_webhook_events_received ON gateway_webhook_events(received_at DESC);
        ALTER TABLE driver_merchant_payments DROP CONSTRAINT IF EXISTS driver_merchant_payments_transfer_status_check;
        ALTER TABLE driver_merchant_payments ADD CONSTRAINT driver_merchant_payments_transfer_status_check
            CHECK (transfer_status IN ('PENDING', 'SENDING', 'CREATED', 'FAILED', 'MANUAL', 'DIRECT'));

        CREATE TABLE IF NOT EXISTS driver_fuel_logs (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            fuel_type VARCHAR(10) NOT NULL DEFAULT 'Petrol' CHECK (fuel_type IN ('CNG', 'Petrol', 'Diesel')),
            quantity NUMERIC(10, 2) NOT NULL DEFAULT 0,
            rate NUMERIC(10, 2) NOT NULL DEFAULT 0,
            total_cost NUMERIC(10, 2) NOT NULL CHECK (total_cost > 0),
            odometer NUMERIC(10, 1),
            station VARCHAR(200) DEFAULT '',
            fuel_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted BOOLEAN NOT NULL DEFAULT FALSE
        );
        CREATE INDEX IF NOT EXISTS idx_driver_fuel_owner_time ON driver_fuel_logs(owner_id, fuel_time DESC);
        -- Distance tracked by GPS while the driver is on duty, one running total per day.
        CREATE TABLE IF NOT EXISTS driver_daily_distance (
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            day DATE NOT NULL DEFAULT CURRENT_DATE,
            km NUMERIC(8, 3) NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (owner_id, day)
        );

        -- How the driver paid the pump (recorded by him; the app does not move this money).
        ALTER TABLE driver_fuel_logs ADD COLUMN IF NOT EXISTS payment_mode VARCHAR(10) NOT NULL DEFAULT 'Cash';
        ALTER TABLE driver_fuel_logs DROP CONSTRAINT IF EXISTS driver_fuel_logs_fuel_type_check;
        ALTER TABLE driver_fuel_logs ADD CONSTRAINT driver_fuel_logs_fuel_type_check
            CHECK (fuel_type IN ('CNG', 'Petrol', 'Diesel', 'Other'));
    """)


@driver_bp.get("/upi-qr")
def get_upi_qr():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT qr_image, upi_id FROM driver_upi_qr WHERE owner_id = %s", (owner_id,))
                row = cur.fetchone()
        if not row:
            return jsonify({"qrImage": None, "upiId": ""})
        return jsonify({"qrImage": row[0], "upiId": row[1] or ""})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@driver_bp.put("/upi-qr")
def save_upi_qr():
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    qr_image = str(payload.get("qrImage", "")).strip()
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not qr_image.startswith("data:image/"):
        return jsonify({"error": "qrImage must be an image data URI"}), 400
    if len(qr_image) > MAX_QR_CHARS:
        return jsonify({"error": "Image is too large. Please pick a smaller image."}), 413
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO driver_upi_qr (owner_id, qr_image, upi_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (owner_id) DO UPDATE SET
                        qr_image = EXCLUDED.qr_image, upi_id = EXCLUDED.upi_id, updated_at = NOW()
                    """,
                    (owner_id, qr_image, str(payload.get("upiId", "")).strip()),
                )
                conn.commit()
        return jsonify({"message": "QR saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@driver_bp.delete("/upi-qr")
def delete_upi_qr():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM driver_upi_qr WHERE owner_id = %s", (owner_id,))
                conn.commit()
        return jsonify({"message": "QR removed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------

def serialize_trip(row):
    (tid, route, fare, payment_mode, start_km, end_km, source, trip_time, location_name, latitude, longitude) = row
    start = float(start_km) if start_km is not None else None
    end = float(end_km) if end_km is not None else None
    return {
        "id": str(tid),
        "route": route or "",
        "fare": float(fare),
        "paymentMode": payment_mode,
        "startKm": start,
        "endKm": end,
        "distanceKm": round(end - start, 1) if start is not None and end is not None else 0,
        "source": source,
        "tripTime": trip_time.isoformat(),
        "locationName": location_name or "",
        "latitude": float(latitude) if latitude is not None else None,
        "longitude": float(longitude) if longitude is not None else None,
    }


TRIP_COLUMNS = "id, route, fare, payment_mode, start_km, end_km, source, trip_time, location_name, latitude, longitude"

# Trip list with, for a trip created from a merchant-QR payment, that payment's details.
TRIP_LIST_SELECT = (
    "SELECT t.id, t.route, t.fare, t.payment_mode, t.start_km, t.end_km, t.source, t.trip_time, t.location_name, "
    "t.latitude, t.longitude, p.gateway_payment_id, p.payer_vpa, p.rrn, p.transfer_status, p.refunded_amount "
    "FROM driver_trips t LEFT JOIN driver_merchant_payments p ON p.trip_id = t.id "
    "WHERE t.owner_id = %s AND NOT t.deleted "
)


def serialize_listed_trip(row):
    trip = serialize_trip(row[:11])
    gateway_payment_id, payer_vpa, rrn, transfer_status, refunded_amount = row[11:]
    trip["payment"] = {
        "payerVpa": payer_vpa,
        "ref": rrn or gateway_payment_id,
        "transferStatus": transfer_status,
        "refundedAmount": float(refunded_amount),
    } if gateway_payment_id else None
    return trip


def _parse_date_range_args():
    """Shared by /trips and /fuel-logs: an explicit start_date/end_date (YYYY-MM-DD)
    range takes priority over the relative `days` param, which in turn falls back to
    today-only. Invalid or partial dates are ignored rather than erroring out."""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    try:
        if start_date:
            datetime.strptime(start_date, "%Y-%m-%d")
        if end_date:
            datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        start_date = None
        end_date = None
    if not (start_date and end_date):
        start_date = None
        end_date = None

    days = request.args.get("days")
    try:
        days = max(1, min(90, int(days))) if days else None
    except (TypeError, ValueError):
        days = None

    return start_date, end_date, days


@driver_bp.get("/trips")
def list_trips():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        start_date, end_date, days = _parse_date_range_args()
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if start_date and end_date:
                    cur.execute(
                        TRIP_LIST_SELECT + "AND t.trip_time::date BETWEEN %s AND %s ORDER BY t.trip_time DESC",
                        (owner_id, start_date, end_date),
                    )
                elif days:
                    # days=7 means "today plus the 6 days before it"
                    cur.execute(
                        TRIP_LIST_SELECT + "AND t.trip_time::date >= CURRENT_DATE - (%s - 1) ORDER BY t.trip_time DESC",
                        (owner_id, days),
                    )
                else:
                    cur.execute(
                        TRIP_LIST_SELECT + "AND t.trip_time::date = CURRENT_DATE ORDER BY t.trip_time DESC",
                        (owner_id,),
                    )
                trips = [serialize_listed_trip(r) for r in cur.fetchall()]
        return jsonify({"trips": trips})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def serialize_fuel_log(row):
    fid, fuel_type, quantity, rate, total_cost, odometer, station, fuel_time, payment_mode = row
    return {
        "id": str(fid),
        "fuelType": fuel_type,
        "quantity": float(quantity or 0),
        "rate": float(rate or 0),
        "totalCost": float(total_cost or 0),
        "odometer": float(odometer) if odometer is not None else None,
        "station": station or "",
        "fuelTime": fuel_time.isoformat(),
        "paymentMode": payment_mode,
    }


FUEL_LOG_COLUMNS = "id, fuel_type, quantity, rate, total_cost, odometer, station, fuel_time, payment_mode"


@driver_bp.get("/fuel-logs")
def list_fuel_logs():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        start_date, end_date, days = _parse_date_range_args()
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if request.args.get("all") == "1":
                    # Full history for the Fuel Log list (newest first, capped to keep the reply small).
                    cur.execute(
                        f"SELECT {FUEL_LOG_COLUMNS} FROM driver_fuel_logs WHERE owner_id = %s "
                        "AND NOT deleted ORDER BY fuel_time DESC LIMIT 500",
                        (owner_id,),
                    )
                elif start_date and end_date:
                    cur.execute(
                        f"SELECT {FUEL_LOG_COLUMNS} FROM driver_fuel_logs WHERE owner_id = %s "
                        "AND NOT deleted AND fuel_time::date BETWEEN %s AND %s ORDER BY fuel_time DESC",
                        (owner_id, start_date, end_date),
                    )
                elif days:
                    cur.execute(
                        f"SELECT {FUEL_LOG_COLUMNS} FROM driver_fuel_logs WHERE owner_id = %s "
                        "AND NOT deleted AND fuel_time::date >= CURRENT_DATE - (%s - 1) ORDER BY fuel_time DESC",
                        (owner_id, days),
                    )
                else:
                    cur.execute(
                        f"SELECT {FUEL_LOG_COLUMNS} FROM driver_fuel_logs WHERE owner_id = %s "
                        "AND NOT deleted AND fuel_time::date = CURRENT_DATE ORDER BY fuel_time DESC",
                        (owner_id,),
                    )
                logs = [serialize_fuel_log(r) for r in cur.fetchall()]
        return jsonify({"fuelLogs": logs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@driver_bp.post("/fuel-logs")
def create_fuel_log():
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    fuel_type = str(payload.get("fuelType", "Petrol")).strip()
    if fuel_type not in ("CNG", "Petrol", "Diesel", "Other"):
        fuel_type = "Petrol"
    try:
        quantity = float(payload.get("quantity") or 0)
    except (TypeError, ValueError):
        quantity = 0
    try:
        rate = float(payload.get("rate") or 0)
    except (TypeError, ValueError):
        rate = 0
    try:
        total_cost = float(payload.get("totalCost"))
    except (TypeError, ValueError):
        total_cost = quantity * rate
    if not total_cost or total_cost <= 0:
        return jsonify({"error": "totalCost (or quantity + rate) must be greater than 0"}), 400
    try:
        odometer = float(payload.get("odometer")) if payload.get("odometer") not in (None, "") else None
    except (TypeError, ValueError):
        odometer = None

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO driver_fuel_logs
                        (owner_id, fuel_type, quantity, rate, total_cost, odometer, station, payment_mode)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {FUEL_LOG_COLUMNS}
                    """,
                    (owner_id, fuel_type, quantity, rate, total_cost, odometer, str(payload.get("station", "")).strip(),
                     "UPI" if str(payload.get("paymentMode", "Cash")).upper() == "UPI" else "Cash"),
                )
                row = cur.fetchone()
                conn.commit()
        return jsonify({"fuelLog": serialize_fuel_log(row)}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@driver_bp.delete("/fuel-logs/<int:fuel_id>")
def delete_fuel_log(fuel_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE driver_fuel_logs SET deleted = TRUE WHERE id = %s AND owner_id = %s AND NOT deleted RETURNING id",
                    (fuel_id, owner_id),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Fuel log not found"}), 404
                conn.commit()
        return jsonify({"message": "Fuel log deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@driver_bp.post("/voice-fuel-log")
def voice_fuel_log():
    """Transcribes a short driver voice note about a fuel fill-up ('diesel 500',
    'cng four litres 350 rupees', etc.) and extracts fuel type + amount using Gemini.
    Never saves anything itself - the app shows the result to the driver to review
    and save, same as manual entry."""
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
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
            "error": "Voice logging isn't set up yet. Ask the app owner to add a free GEMINI_API_KEY "
                     "(from aistudio.google.com/apikey) to the backend configuration to enable it."
        }), 200

    system_prompt = (
        "You are a voice transcription assistant for an auto-rickshaw driver's fuel-logging app. "
        "The driver says something short about a fuel fill-up they just paid for, in English, Hindi, "
        "or Marathi - e.g. 'diesel 500', 'cng four litres 350 rupees', 'petrol do sau rupaye', "
        "'tीनशे रुपयाचे पेट्रोल'. Listen to the audio and reply with STRICT JSON only - no markdown "
        "fences, no explanation - in exactly this shape: "
        '{"fuelType": "CNG" or "Petrol" or "Diesel" or null, "totalCost": <number or null>, '
        '"quantity": <number or null>, "transcript": "<what you heard, in the original language>"}. '
        "If the audio is silent, contains no intelligible speech, is just background/road noise, or you "
        "are not highly confident an amount was actually spoken, you MUST set totalCost to null, "
        "fuelType to null, quantity to null, and transcript to an empty string - do NOT invent or guess "
        "a plausible-sounding amount just because one is expected. Quantity is optional - only set it if "
        "a litre/kg amount was explicitly said. Never guess a number that wasn't said."
    )

    try:
        reply_text = _call_gemini_audio(system_prompt, audio_base64, mime_type, api_key)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"Voice fuel log HTTP error {e.code}: {err_body}")
        return jsonify({"error": "Voice logging is unavailable right now. Please try again or enter it manually."}), 200
    except Exception as e:
        print(f"Voice fuel log error: {e}")
        return jsonify({"error": "Voice logging is unavailable right now. Please try again or enter it manually."}), 200

    cleaned = reply_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return jsonify({
            "error": "Could not understand the recording. Please try again or enter it manually.",
            "transcript": reply_text,
        }), 200

    def _num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    total_cost = _num(parsed.get("totalCost"))
    quantity = _num(parsed.get("quantity"))
    fuel_type = parsed.get("fuelType")
    if fuel_type not in ("CNG", "Petrol", "Diesel"):
        fuel_type = None

    return jsonify({
        "totalCost": total_cost,
        "quantity": quantity,
        "fuelType": fuel_type,
        "transcript": parsed.get("transcript", ""),
    })


@driver_bp.post("/voice-log")
def voice_log():
    """Transcribes a short driver voice note ('cash 150', 'upi one fifty', etc.) and
    extracts an amount + payment mode using Gemini. Never saves anything itself - the
    app shows the result to the driver to review and save, same as manual entry."""
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
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
            "error": "Voice logging isn't set up yet. Ask the app owner to add a free GEMINI_API_KEY "
                     "(from aistudio.google.com/apikey) to the backend configuration to enable it."
        }), 200

    system_prompt = (
        "You are a voice transcription assistant for an auto-rickshaw driver's fare-logging app. "
        "The driver says something short about a fare they just received, in English, Hindi, or "
        "Marathi - e.g. 'cash 150', 'upi one fifty', 'do sau rupaye cash mila', 'dedashe UPI zhala'. "
        "Listen to the audio and reply with STRICT JSON only - no markdown fences, no explanation - "
        "in exactly this shape: "
        '{"amount": <number or null>, "paymentMode": "Cash" or "UPI" or null, '
        '"transcript": "<what you heard, in the original language>"}. '
        "If the audio is silent, contains no intelligible speech, is just background/road noise, or you "
        "are not highly confident a rupee amount was actually spoken, you MUST set amount to null, "
        "paymentMode to null, and transcript to an empty string - do NOT invent or guess a plausible-"
        "sounding amount or word just because a number is expected. If the payment mode isn't clearly "
        "stated, set paymentMode to null even if amount is present. Never guess a number that wasn't said."
    )

    try:
        reply_text = _call_gemini_audio(system_prompt, audio_base64, mime_type, api_key)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"Voice log HTTP error {e.code}: {err_body}")
        return jsonify({"error": "Voice logging is unavailable right now. Please try again or enter it manually."}), 200
    except Exception as e:
        print(f"Voice log error: {e}")
        return jsonify({"error": "Voice logging is unavailable right now. Please try again or enter it manually."}), 200

    cleaned = reply_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return jsonify({
            "error": "Could not understand the recording. Please try again or enter it manually.",
            "transcript": reply_text,
        }), 200

    amount = parsed.get("amount")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    payment_mode = parsed.get("paymentMode")
    if payment_mode not in ("Cash", "UPI"):
        payment_mode = None

    return jsonify({
        "amount": amount,
        "paymentMode": payment_mode,
        "transcript": parsed.get("transcript", ""),
    })


@driver_bp.post("/trips")
def create_trip():
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    try:
        fare = float(payload.get("fare"))
    except (TypeError, ValueError):
        fare = 0
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if fare <= 0:
        return jsonify({"error": "fare must be greater than 0"}), 400
    mode = "UPI" if str(payload.get("paymentMode", "Cash")).upper() == "UPI" else "Cash"
    external_ref = str(payload.get("externalRef", "")).strip() or None
    source = "SMS" if external_ref else "MANUAL"
    trip_time = None
    if payload.get("tripTimeMs"):
        try:
            trip_time = datetime.fromtimestamp(float(payload["tripTimeMs"]) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            trip_time = None
    try:
        latitude = float(payload.get("latitude"))
    except (TypeError, ValueError):
        latitude = None
    try:
        longitude = float(payload.get("longitude"))
    except (TypeError, ValueError):
        longitude = None
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO driver_trips (owner_id, route, fare, payment_mode, start_km, end_km, source, "
                    f"external_ref, trip_time, location_name, latitude, longitude) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()), %s, %s, %s) "
                    f"ON CONFLICT (owner_id, external_ref) WHERE external_ref IS NOT NULL DO NOTHING "
                    f"RETURNING {TRIP_COLUMNS}",
                    (owner_id, str(payload.get("route", "")).strip(), fare, mode,
                     payload.get("startKm"), payload.get("endKm"), source, external_ref, trip_time,
                     str(payload.get("locationName", "")).strip(), latitude, longitude),
                )
                row = cur.fetchone()
                conn.commit()
        if not row:
            return jsonify({"duplicate": True}), 200
        return jsonify({"trip": serialize_trip(row)}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@driver_bp.delete("/trips/<int:trip_id>")
def delete_trip(trip_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE driver_trips SET deleted = TRUE WHERE id = %s AND owner_id = %s AND NOT deleted RETURNING id", (trip_id, owner_id))
                if not cur.fetchone():
                    return jsonify({"error": "Trip not found"}), 404
                conn.commit()
        return jsonify({"message": "Trip deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Tracked distance (GPS while on duty)
# ---------------------------------------------------------------------------

MAX_DISTANCE_STEP_KM = 50  # one upload covers about a minute of driving; more is a client fault


@driver_bp.get("/distance")
def get_distance():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT day, km FROM driver_daily_distance WHERE owner_id = %s AND day >= CURRENT_DATE - 6 ORDER BY day",
                    (owner_id,),
                )
                rows = cur.fetchall()
                cur.execute("SELECT CURRENT_DATE")
                today = cur.fetchone()[0]
        return jsonify({
            "todayKm": next((float(km) for day, km in rows if day == today), 0.0),
            "days": [{"date": day.isoformat(), "km": float(km)} for day, km in rows],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@driver_bp.post("/distance/add")
def add_distance():
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    try:
        km = float(payload.get("km"))
    except (TypeError, ValueError):
        km = 0
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if km <= 0 or km > MAX_DISTANCE_STEP_KM:
        return jsonify({"error": "km must be between 0 and 50"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO driver_daily_distance (owner_id, km) VALUES (%s, %s) "
                    "ON CONFLICT (owner_id, day) DO UPDATE SET km = driver_daily_distance.km + EXCLUDED.km, updated_at = NOW() "
                    "RETURNING km",
                    (owner_id, km),
                )
                today_km = float(cur.fetchone()[0])
                conn.commit()
        return jsonify({"todayKm": today_km})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# QR payment tracking (Razorpay dynamic UPI QR)
# ---------------------------------------------------------------------------

RAZORPAY_HOST = "https://api.razorpay.com"
RAZORPAY_API = RAZORPAY_HOST + "/v1"
QR_VALIDITY_SECONDS = 15 * 60


class GatewayNotConfigured(Exception):
    pass


def _razorpay_request(method, path, body=None, creds=None):
    """creds is a driver's own (key_id, key_secret); without it the platform's keys are used."""
    key, secret = creds or (os.getenv("RAZORPAY_KEY_ID", "").strip(), os.getenv("RAZORPAY_KEY_SECRET", "").strip())
    if not key or not secret:
        raise GatewayNotConfigured()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    auth = base64.b64encode(f"{key}:{secret}".encode()).decode()
    # Route onboarding lives under /v2; everything else is /v1.
    url = RAZORPAY_HOST + path if path.startswith("/v2/") else RAZORPAY_API + path
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _record_paid(cur, qr_row_id, gateway_payment_id):
    """Idempotently marks a QR payment PAID and creates its UPI trip. Returns trip id or None."""
    cur.execute(
        "UPDATE driver_qr_payments SET status = 'PAID', gateway_payment_id = %s, paid_at = NOW() "
        "WHERE id = %s AND status IN ('PENDING', 'EXPIRED') RETURNING owner_id, amount, route",
        (gateway_payment_id, qr_row_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    owner_id, amount, route = row
    cur.execute(
        "INSERT INTO driver_trips (owner_id, route, fare, payment_mode, source, qr_payment_id) "
        "VALUES (%s, %s, %s, 'UPI', 'QR', %s) RETURNING id",
        (owner_id, route, amount, qr_row_id),
    )
    trip_id = cur.fetchone()[0]
    cur.execute("UPDATE driver_qr_payments SET trip_id = %s WHERE id = %s", (trip_id, qr_row_id))
    return trip_id


QR_COLUMNS = "id, amount, route, image_url, status, expires_at, paid_at, trip_id, gateway_qr_id"


def serialize_qr_payment(row):
    (qid, amount, route, image_url, status, expires_at, paid_at, trip_id, _gateway_qr_id) = row
    return {
        "id": str(qid),
        "amount": float(amount),
        "route": route or "",
        "imageUrl": image_url,
        "status": status,
        "expiresAt": expires_at.isoformat(),
        "paidAt": paid_at.isoformat() if paid_at else None,
        "tripId": str(trip_id) if trip_id else None,
    }


@driver_bp.post("/qr-payments")
def create_qr_payment():
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    try:
        amount = round(float(payload.get("amount")), 2)
    except (TypeError, ValueError):
        amount = 0
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if amount <= 0:
        return jsonify({"error": "amount must be greater than 0"}), 400
    route = str(payload.get("route", "")).strip()

    try:
        expires_ts = int(time.time()) + QR_VALIDITY_SECONDS
        qr = _razorpay_request("POST", "/payments/qr_codes", {
            "type": "upi_qr",
            "name": "AutoLedger Ride Fare",
            "usage": "single_use",
            "fixed_amount": True,
            "payment_amount": int(round(amount * 100)),
            "description": route or "Ride fare",
            "close_by": expires_ts,
            "notes": {"owner_id": str(owner_id)},
        })
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO driver_qr_payments (owner_id, amount, route, gateway_qr_id, image_url, expires_at) "
                    f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {QR_COLUMNS}",
                    (owner_id, amount, route, qr["id"], qr.get("image_url"),
                     datetime.fromtimestamp(expires_ts, tz=timezone.utc)),
                )
                result = serialize_qr_payment(cur.fetchone())
                conn.commit()
        return jsonify({"qrPayment": result}), 201
    except GatewayNotConfigured:
        return jsonify({"error": "Payment gateway is not configured.", "code": "GATEWAY_NOT_CONFIGURED"}), 503
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        print(f"Razorpay QR create failed {e.code}: {detail}")
        return jsonify({"error": "Could not create the payment QR. Please try again.", "code": "GATEWAY_ERROR"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@driver_bp.get("/qr-payments/<int:qr_id>")
def get_qr_payment(qr_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {QR_COLUMNS} FROM driver_qr_payments WHERE id = %s AND owner_id = %s",
                    (qr_id, owner_id),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "QR payment not found"}), 404
                status, expires_at, gateway_qr_id = row[4], row[5], row[8]

                if status == "PENDING":
                    try:
                        items = _razorpay_request("GET", f"/payments/qr_codes/{gateway_qr_id}/payments").get("items", [])
                        paid = next((p for p in items if p.get("status") in ("captured", "authorized")), None)
                        if paid:
                            _record_paid(cur, qr_id, paid["id"])
                        elif expires_at <= datetime.now(timezone.utc):
                            cur.execute("UPDATE driver_qr_payments SET status = 'EXPIRED' WHERE id = %s AND status = 'PENDING'", (qr_id,))
                    except GatewayNotConfigured:
                        pass
                    except urllib.error.URLError as e:
                        print(f"Razorpay status poll failed: {e}")
                    conn.commit()
                    cur.execute(f"SELECT {QR_COLUMNS} FROM driver_qr_payments WHERE id = %s", (qr_id,))
                    row = cur.fetchone()
        return jsonify({"qrPayment": serialize_qr_payment(row)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@driver_bp.post("/qr-payments/<int:qr_id>/cancel")
def cancel_qr_payment(qr_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE driver_qr_payments SET status = 'CANCELLED' "
                    "WHERE id = %s AND owner_id = %s AND status = 'PENDING' RETURNING gateway_qr_id",
                    (qr_id, owner_id),
                )
                row = cur.fetchone()
                conn.commit()
        if row:
            try:
                _razorpay_request("POST", f"/payments/qr_codes/{row[0]}/close")
            except Exception as e:
                print(f"Could not close Razorpay QR {row[0]}: {e}")
        return jsonify({"message": "Cancelled"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Merchant account (Razorpay Route linked account + one permanent QR per driver)
# ---------------------------------------------------------------------------

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MERCHANT_SYNC_MIN_SECONDS = 8
MAX_TRANSFER_ATTEMPTS = 5
_last_merchant_sync = {}
# Why the latest sync for an owner could not read his payments (None when it worked).
_last_merchant_sync_error = {}

MERCHANT_COLUMNS = (
    "legal_name, phone, email, pan_last4, bank_last4, ifsc, gateway_account_id, "
    "gateway_product_id, onboarding_step, status, status_note, gateway_qr_id, qr_image_url, gateway_mode, own_key_id"
)
KEY_ID_RE = re.compile(r"^rzp_(test|live)_[A-Za-z0-9]{8,30}$")


class KeysEncryptionNotConfigured(Exception):
    pass


def _own_keys_mode():
    """own_keys (default): each driver connects his own Razorpay account. platform: every QR
    is created in the platform's Razorpay account."""
    return os.getenv("DRIVER_MERCHANT_MODE", "own_keys").strip().lower() != "platform"


def _keys_cipher():
    key = os.getenv("DRIVER_KEYS_ENCRYPTION_KEY", "").strip()
    if not key:
        raise KeysEncryptionNotConfigured()
    try:
        return Fernet(key.encode())
    except ValueError:
        raise KeysEncryptionNotConfigured()


def _decrypt(value):
    try:
        return _keys_cipher().decrypt(value.encode()).decode()
    except InvalidToken:
        # Encrypted with a different DRIVER_KEYS_ENCRYPTION_KEY than this server has.
        raise KeysEncryptionNotConfigured()


def _driver_creds(cur, owner_id):
    """(key_id, key_secret) of the driver's own Razorpay account, or None when he has not
    connected one (the platform's keys then apply)."""
    cur.execute("SELECT own_key_id, own_key_secret_enc FROM driver_merchant_accounts WHERE owner_id = %s", (owner_id,))
    row = cur.fetchone()
    if not row or not row[0] or not row[1]:
        return None
    return row[0], _decrypt(row[1])


def _token_owner(payload=None):
    """Merchant endpoints carry KYC and bank details, so unlike the rest of this API they
    require the login token and only act on the owner it was issued to.
    Returns (owner_id, None) or (None, error_response)."""
    header = request.headers.get("Authorization", "")
    user_id = verify_token(header[7:].strip()) if header.startswith("Bearer ") else None
    if user_id is None:
        return None, (jsonify({"error": "Session expired. Please log in again."}), 401)
    requested = get_owner_id(payload)
    if requested is not None and requested != user_id:
        return None, (jsonify({"error": "You can only access your own business data."}), 403)
    return user_id, None


def _gateway_configured():
    return bool(os.getenv("RAZORPAY_KEY_ID", "").strip() and os.getenv("RAZORPAY_KEY_SECRET", "").strip())


def _gateway_mode(creds=None):
    key_id = creds[0] if creds else os.getenv("RAZORPAY_KEY_ID", "").strip()
    return "live" if key_id.startswith("rzp_live_") else "test"


def _route_enabled():
    """Route (linked accounts + automatic transfers) needs Razorpay's approval. When it is
    off, drivers still get a QR and automatic records, but the money stays in the platform's
    Razorpay account and each payment is marked MANUAL for the platform to pay out."""
    return os.getenv("RAZORPAY_ROUTE_ENABLED", "0").strip().lower() in ("1", "true", "yes")


def _gateway_error_text(http_error):
    try:
        body = json.loads(http_error.read().decode("utf-8", errors="ignore"))
        return str(body.get("error", {}).get("description") or "")
    except (ValueError, AttributeError):
        return ""


def _platform_fee_percent():
    try:
        return min(max(float(os.getenv("DRIVER_PLATFORM_FEE_PERCENT", "0")), 0.0), 100.0)
    except ValueError:
        return 0.0


def serialize_merchant_account(row):
    (legal_name, phone, email, pan_last4, bank_last4, ifsc, _account_id, _product_id,
     _step, status, status_note, gateway_qr_id, qr_image_url, gateway_mode, own_key_id) = row
    return {
        "ownKeys": bool(own_key_id),
        "keyIdMasked": f"{own_key_id[:9]}\u2022\u2022\u2022\u2022{own_key_id[-4:]}" if own_key_id else "",
        "testMode": bool(gateway_qr_id) and gateway_mode != "live",
        "legalName": legal_name,
        "phone": phone,
        "email": email,
        "panLast4": pan_last4,
        "bankLast4": bank_last4,
        "ifsc": ifsc,
        "status": status,
        "statusNote": status_note or "",
        "hasQr": bool(gateway_qr_id),
        "qrImageUrl": qr_image_url,
    }


def _apply_product_status(cur, owner_id, product):
    """Stores the linked account's activation status as reported by Razorpay. Returns it."""
    status = str(product.get("activation_status") or "under_review").upper()
    note = ", ".join(
        r.get("field_reference", "") for r in (product.get("requirements") or []) if r.get("field_reference")
    )
    cur.execute(
        "UPDATE driver_merchant_accounts SET status = %s, status_note = %s, updated_at = NOW() WHERE owner_id = %s",
        (status, note, owner_id),
    )
    return status


def _ensure_merchant_qr(cur, owner_id, legal_name, creds=None):
    """Creates the driver's permanent, any-amount UPI QR - in his own Razorpay account when
    creds are given, otherwise in the platform's. Caller holds the account row lock."""
    qr = _razorpay_request("POST", "/payments/qr_codes", {
        "type": "upi_qr",
        "name": legal_name[:40],
        "usage": "multiple_use",
        "fixed_amount": False,
        "description": "Auto fare",
        "notes": {"owner_id": str(owner_id), "kind": "driver_merchant"},
    }, creds=creds)
    cur.execute(
        "UPDATE driver_merchant_accounts SET gateway_qr_id = %s, qr_image_url = %s, gateway_mode = %s, "
        "updated_at = NOW() WHERE owner_id = %s",
        (qr["id"], qr.get("image_url"), _gateway_mode(creds), owner_id),
    )


@driver_bp.get("/merchant-account")
def get_merchant_account():
    owner_id, denied = _token_owner()
    if denied:
        return denied
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {MERCHANT_COLUMNS} FROM driver_merchant_accounts WHERE owner_id = %s FOR UPDATE", (owner_id,))
                row = cur.fetchone()
                # Until the account is active with a QR, ask Razorpay where KYC review stands.
                # A QR made with the other mode's keys (test vs live) is dead and is reissued.
                creds = None
                if row and row[14]:
                    try:
                        creds = _driver_creds(cur, owner_id)
                    except KeysEncryptionNotConfigured:
                        print("DRIVER_KEYS_ENCRYPTION_KEY is missing or does not match the stored driver keys")
                        row = row[:9] + ("NEEDS_CLARIFICATION", "server key") + row[11:]
                has_keys = bool(creds) or (_gateway_configured() and not (row and row[14]))
                needs_qr = bool(row) and has_keys and (not row[11] or row[13] != _gateway_mode(creds))
                if row and (row[9] != "ACTIVATED" or needs_qr):
                    try:
                        status = row[9]
                        if status != "ACTIVATED" and row[7]:
                            product = _razorpay_request("GET", f"/v2/accounts/{row[6]}/products/{row[7]}")
                            status = _apply_product_status(cur, owner_id, product)
                        if status == "ACTIVATED" and needs_qr:
                            _ensure_merchant_qr(cur, owner_id, row[0], creds)
                        cur.execute(f"SELECT {MERCHANT_COLUMNS} FROM driver_merchant_accounts WHERE owner_id = %s", (owner_id,))
                        row = cur.fetchone()
                    except GatewayNotConfigured:
                        pass
                    except urllib.error.URLError as e:
                        print(f"Razorpay linked account status check failed: {e}")
                conn.commit()
        return jsonify({
            "account": serialize_merchant_account(row) if row else None,
            "gatewayConfigured": _own_keys_mode() or _gateway_configured(),
            "routeEnabled": _route_enabled(),
            "ownKeysMode": _own_keys_mode(),
            "webhookUrl": f"{request.url_root.rstrip('/')}/api/driver/razorpay-webhook/{owner_id}",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _save_own_keys_account(owner_id, payload):
    """Connects the driver's own Razorpay account: checks the keys with Razorpay, stores them
    encrypted, and creates his QR in that account. Resubmitting replaces the keys."""
    def field(name):
        return str(payload.get(name, "")).strip()

    legal_name = field("legalName")
    phone = re.sub(r"\D", "", field("phone"))[-10:]
    key_id = field("keyId")
    key_secret = field("keySecret")
    webhook_secret = field("webhookSecret")

    if len(legal_name) < 3:
        return jsonify({"error": "Enter your full name."}), 400
    if len(phone) != 10:
        return jsonify({"error": "Enter a valid 10-digit mobile number."}), 400
    if not KEY_ID_RE.match(key_id):
        return jsonify({"error": "Enter a valid Razorpay Key Id (it starts with rzp_test_ or rzp_live_)."}), 400
    if len(key_secret) < 10:
        return jsonify({"error": "Enter your Razorpay Key Secret."}), 400
    if not payload.get("tncAccepted"):
        return jsonify({"error": "Please accept the terms to continue."}), 400

    creds = (key_id, key_secret)
    try:
        cipher = _keys_cipher()
        # Cheapest authenticated call: proves the pair is real before anything is stored.
        _razorpay_request("GET", "/payments?count=1", creds=creds)
    except KeysEncryptionNotConfigured:
        return jsonify({"error": "The server is not set up to store payment keys yet.", "code": "KEYS_NOT_CONFIGURED"}), 503
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return jsonify({"error": "Razorpay did not accept these keys. Check the Key Id and Key Secret."}), 400
        return jsonify({"error": _gateway_error_text(e) or "Could not verify the keys with Razorpay.", "code": "GATEWAY_ERROR"}), 502
    except urllib.error.URLError:
        return jsonify({"error": "Could not reach Razorpay. Please try again.", "code": "GATEWAY_ERROR"}), 502

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT own_key_id FROM driver_merchant_accounts WHERE owner_id = %s FOR UPDATE", (owner_id,))
                existing = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO driver_merchant_accounts
                        (owner_id, legal_name, phone, email, status, own_key_id, own_key_secret_enc, own_webhook_secret_enc)
                    VALUES (%s, %s, %s, '', 'ACTIVATED', %s, %s, %s)
                    ON CONFLICT (owner_id) DO UPDATE SET
                        legal_name = EXCLUDED.legal_name, phone = EXCLUDED.phone, status = 'ACTIVATED', status_note = '',
                        own_key_id = EXCLUDED.own_key_id, own_key_secret_enc = EXCLUDED.own_key_secret_enc,
                        own_webhook_secret_enc = COALESCE(EXCLUDED.own_webhook_secret_enc, driver_merchant_accounts.own_webhook_secret_enc),
                        updated_at = NOW()
                    """,
                    (owner_id, legal_name, phone, key_id, cipher.encrypt(key_secret.encode()).decode(),
                     cipher.encrypt(webhook_secret.encode()).decode() if webhook_secret else None),
                )
                # A QR made in another account (the platform's, or his previous keys) is not his.
                if not existing or existing[0] != key_id:
                    cur.execute(
                        "UPDATE driver_merchant_accounts SET gateway_qr_id = NULL, qr_image_url = NULL, gateway_mode = NULL "
                        "WHERE owner_id = %s",
                        (owner_id,),
                    )
                conn.commit()

                cur.execute("SELECT gateway_qr_id FROM driver_merchant_accounts WHERE owner_id = %s FOR UPDATE", (owner_id,))
                if not cur.fetchone()[0]:
                    _ensure_merchant_qr(cur, owner_id, legal_name, creds)
                cur.execute(f"SELECT {MERCHANT_COLUMNS} FROM driver_merchant_accounts WHERE owner_id = %s", (owner_id,))
                result = serialize_merchant_account(cur.fetchone())
                conn.commit()
        return jsonify({"account": result}), 201
    except urllib.error.HTTPError as e:
        detail = _gateway_error_text(e)
        print(f"Razorpay QR creation with driver keys failed {e.code}: {detail}")
        return jsonify({
            "error": detail or "Your keys were saved, but Razorpay could not create the QR. Check that QR Codes are enabled on your Razorpay account.",
            "code": "GATEWAY_ERROR",
        }), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@driver_bp.post("/merchant-account")
def save_merchant_account():
    """Onboards the driver. With Route: as a Razorpay linked account, each gateway step saved
    as it completes so a failed attempt (bad IFSC, network drop) resumes instead of starting
    over. Without Route: bank details are stored for manual payouts and the QR is issued at once."""
    payload = request.get_json(silent=True) or {}
    owner_id, denied = _token_owner(payload)
    if denied:
        return denied

    if _own_keys_mode():
        return _save_own_keys_account(owner_id, payload)

    def field(name):
        return str(payload.get(name, "")).strip()

    legal_name = field("legalName")
    phone = re.sub(r"\D", "", field("phone"))[-10:]
    email = field("email")
    pan = field("pan").upper()
    street = field("street")
    city = field("city")
    state = field("state")
    postal_code = field("postalCode")
    account_number = re.sub(r"\s", "", field("accountNumber"))
    ifsc = field("ifsc").upper()
    beneficiary_name = field("beneficiaryName") or legal_name

    if len(legal_name) < 3:
        return jsonify({"error": "Enter your full name as on your PAN card."}), 400
    if len(phone) != 10:
        return jsonify({"error": "Enter a valid 10-digit mobile number."}), 400
    route_enabled = _route_enabled()
    if route_enabled:
        # KYC fields are only needed for the linked account.
        if not EMAIL_RE.match(email):
            return jsonify({"error": "Enter a valid email address."}), 400
        if not PAN_RE.match(pan):
            return jsonify({"error": "Enter a valid PAN (e.g. ABCDE1234F)."}), 400
        if not street or not city or not state or not re.fullmatch(r"\d{6}", postal_code):
            return jsonify({"error": "Enter your full address with a 6-digit PIN code."}), 400
    if not re.fullmatch(r"\d{9,18}", account_number):
        return jsonify({"error": "Enter a valid bank account number."}), 400
    if not IFSC_RE.match(ifsc):
        return jsonify({"error": "Enter a valid IFSC code (e.g. SBIN0001234)."}), 400
    if not payload.get("tncAccepted"):
        return jsonify({"error": "Please accept the payment terms to continue."}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT gateway_account_id, gateway_product_id, onboarding_step, status "
                    "FROM driver_merchant_accounts WHERE owner_id = %s FOR UPDATE",
                    (owner_id,),
                )
                existing = cur.fetchone()
                # An account activated without Route has no linked account yet, so it may be
                # resubmitted once Route is switched on.
                if existing and existing[3] == "ACTIVATED" and (existing[0] or not route_enabled):
                    return jsonify({"error": "Your merchant account is already active."}), 409
                account_id, product_id, step = existing[:3] if existing else (None, None, 0)

                cur.execute(
                    """
                    INSERT INTO driver_merchant_accounts
                        (owner_id, legal_name, phone, email, pan_last4, bank_last4, ifsc, bank_account_number, beneficiary_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (owner_id) DO UPDATE SET
                        legal_name = EXCLUDED.legal_name, phone = EXCLUDED.phone, email = EXCLUDED.email,
                        pan_last4 = EXCLUDED.pan_last4, bank_last4 = EXCLUDED.bank_last4, ifsc = EXCLUDED.ifsc,
                        bank_account_number = EXCLUDED.bank_account_number, beneficiary_name = EXCLUDED.beneficiary_name,
                        updated_at = NOW()
                    """,
                    (owner_id, legal_name, phone, email if route_enabled else "", pan[-4:] if route_enabled else "",
                     account_number[-4:], ifsc, None if route_enabled else account_number, beneficiary_name),
                )
                conn.commit()

                if not route_enabled:
                    cur.execute(
                        "SELECT gateway_qr_id FROM driver_merchant_accounts WHERE owner_id = %s FOR UPDATE", (owner_id,)
                    )
                    has_qr = cur.fetchone()[0]
                    cur.execute(
                        "UPDATE driver_merchant_accounts SET status = 'ACTIVATED', status_note = '' WHERE owner_id = %s",
                        (owner_id,),
                    )
                    conn.commit()
                    if not has_qr:
                        cur.execute("SELECT 1 FROM driver_merchant_accounts WHERE owner_id = %s FOR UPDATE", (owner_id,))
                        _ensure_merchant_qr(cur, owner_id, legal_name)
                    cur.execute(f"SELECT {MERCHANT_COLUMNS} FROM driver_merchant_accounts WHERE owner_id = %s", (owner_id,))
                    result = serialize_merchant_account(cur.fetchone())
                    conn.commit()
                    return jsonify({"account": result}), 201

                def save_step(new_step, column=None, value=None):
                    assignment = f", {column} = %s" if column else ""
                    params = (new_step, value, owner_id) if column else (new_step, owner_id)
                    cur.execute(
                        f"UPDATE driver_merchant_accounts SET onboarding_step = %s{assignment}, updated_at = NOW() WHERE owner_id = %s",
                        params,
                    )
                    conn.commit()

                if step < 1:
                    account = _razorpay_request("POST", "/v2/accounts", {
                        "email": email,
                        "phone": phone,
                        "type": "route",
                        "reference_id": f"drv{owner_id}",
                        "legal_business_name": legal_name,
                        "business_type": os.getenv("RAZORPAY_LINKED_BUSINESS_TYPE", "individual"),
                        "contact_name": legal_name,
                        "profile": {
                            "category": os.getenv("RAZORPAY_LINKED_CATEGORY", "transport"),
                            "subcategory": os.getenv("RAZORPAY_LINKED_SUBCATEGORY", "cab_hailing"),
                            "addresses": {"registered": {
                                "street1": street, "street2": city, "city": city, "state": state.upper(),
                                "postal_code": postal_code, "country": "IN",
                            }},
                        },
                    })
                    account_id = account["id"]
                    save_step(1, "gateway_account_id", account_id)
                if step < 2:
                    _razorpay_request("POST", f"/v2/accounts/{account_id}/stakeholders", {
                        "name": legal_name,
                        "email": email,
                        "kyc": {"pan": pan},
                        "addresses": {"residential": {
                            "street": street, "city": city, "state": state, "postal_code": postal_code, "country": "IN",
                        }},
                    })
                    save_step(2)
                if step < 3:
                    product = _razorpay_request("POST", f"/v2/accounts/{account_id}/products", {
                        "product_name": "route", "tnc_accepted": True,
                    })
                    product_id = product["id"]
                    save_step(3, "gateway_product_id", product_id)

                # Bank details are (re)submitted every time so a corrected account number or IFSC goes through.
                product = _razorpay_request("PATCH", f"/v2/accounts/{account_id}/products/{product_id}", {
                    "settlements": {"account_number": account_number, "ifsc_code": ifsc, "beneficiary_name": beneficiary_name},
                    "tnc_accepted": True,
                })
                cur.execute("SELECT 1 FROM driver_merchant_accounts WHERE owner_id = %s FOR UPDATE", (owner_id,))
                cur.execute("UPDATE driver_merchant_accounts SET onboarding_step = 4 WHERE owner_id = %s", (owner_id,))
                if _apply_product_status(cur, owner_id, product) == "ACTIVATED":
                    cur.execute("SELECT gateway_qr_id FROM driver_merchant_accounts WHERE owner_id = %s", (owner_id,))
                    if not cur.fetchone()[0]:
                        _ensure_merchant_qr(cur, owner_id, legal_name)
                cur.execute(f"SELECT {MERCHANT_COLUMNS} FROM driver_merchant_accounts WHERE owner_id = %s", (owner_id,))
                result = serialize_merchant_account(cur.fetchone())
                conn.commit()
        return jsonify({"account": result}), 201
    except GatewayNotConfigured:
        return jsonify({"error": "Payment gateway is not configured.", "code": "GATEWAY_NOT_CONFIGURED"}), 503
    except urllib.error.HTTPError as e:
        detail = _gateway_error_text(e)
        print(f"Razorpay linked account onboarding failed {e.code}: {detail}")
        return jsonify({
            "error": detail or "Could not set up the merchant account. Please try again.",
            "code": "GATEWAY_ERROR",
        }), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _record_merchant_payment(cur, owner_id, gateway_qr_id, payment, direct=False):
    """Idempotently saves one payment made on a driver's permanent QR and records its UPI
    trip. Returns the new driver_merchant_payments id, or None if it was already saved."""
    payment_id = payment.get("id")
    try:
        amount = round(int(payment.get("amount") or 0) / 100, 2)
    except (TypeError, ValueError):
        amount = 0
    if not payment_id or amount <= 0:
        return None
    payer_vpa = str(payment.get("vpa") or "")[:120]
    rrn = str((payment.get("acquirer_data") or {}).get("rrn") or "")[:40]
    try:
        paid_at = datetime.fromtimestamp(int(payment["created_at"]), tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OSError):
        paid_at = datetime.now(timezone.utc)

    cur.execute(
        "INSERT INTO driver_merchant_payments (owner_id, gateway_payment_id, gateway_qr_id, amount, payer_vpa, rrn, paid_at, transfer_status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (gateway_payment_id) DO NOTHING RETURNING id",
        (owner_id, payment_id, gateway_qr_id, amount, payer_vpa, rrn, paid_at,
         "DIRECT" if direct else "PENDING" if _route_enabled() else "MANUAL"),
    )
    row = cur.fetchone()
    if not row:
        return None
    cur.execute(
        "INSERT INTO driver_trips (owner_id, route, fare, payment_mode, source, external_ref, trip_time) "
        "VALUES (%s, %s, %s, 'UPI', 'QR', %s, %s) "
        "ON CONFLICT (owner_id, external_ref) WHERE external_ref IS NOT NULL DO NOTHING RETURNING id",
        (owner_id, f"UPI from {payer_vpa}" if payer_vpa else "QR payment", amount, f"rzp:{payment_id}", paid_at),
    )
    trip = cur.fetchone()
    if trip:
        cur.execute("UPDATE driver_merchant_payments SET trip_id = %s WHERE id = %s", (trip[0], row[0]))
    return row[0]


def _transfer_to_driver(payment_row_id):
    """Sends one saved payment on to the driver's linked account (Route transfer).
    Safe to call repeatedly and from the webhook and the poller at once: the row is claimed
    first, and an existing transfer on the payment is adopted rather than duplicated."""
    if not _route_enabled():
        return
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE driver_merchant_payments p SET transfer_status = 'SENDING', "
                    "transfer_attempts = p.transfer_attempts + 1, transfer_updated_at = NOW() "
                    "FROM driver_merchant_accounts a "
                    "WHERE p.id = %s AND a.owner_id = p.owner_id AND a.status = 'ACTIVATED' "
                    "AND a.gateway_account_id IS NOT NULL "
                    "AND p.transfer_attempts < %s "
                    "AND (p.transfer_status IN ('PENDING', 'FAILED') "
                    "     OR (p.transfer_status = 'SENDING' AND p.transfer_updated_at < NOW() - INTERVAL '5 minutes')) "
                    "RETURNING p.gateway_payment_id, p.amount, a.gateway_account_id, p.owner_id",
                    (payment_row_id, MAX_TRANSFER_ATTEMPTS),
                )
                claimed = cur.fetchone()
                conn.commit()
                if not claimed:
                    return
                gateway_payment_id, amount, account_id, owner_id = claimed

                error = ""
                transfer = None
                try:
                    existing = _razorpay_request("GET", f"/payments/{gateway_payment_id}/transfers").get("items", [])
                    if existing:
                        transfer = existing[0]
                    else:
                        paise = int(round(float(amount) * (100 - _platform_fee_percent())))
                        transfer = _razorpay_request("POST", f"/payments/{gateway_payment_id}/transfers", {
                            "transfers": [{
                                "account": account_id,
                                "amount": paise,
                                "currency": "INR",
                                "notes": {"owner_id": str(owner_id)},
                                "on_hold": False,
                            }],
                        })["items"][0]
                except urllib.error.HTTPError as e:
                    error = _gateway_error_text(e) or f"Gateway error {e.code}"
                except (GatewayNotConfigured, urllib.error.URLError, KeyError, IndexError) as e:
                    error = str(e) or type(e).__name__

                if transfer:
                    cur.execute(
                        "UPDATE driver_merchant_payments SET transfer_status = 'CREATED', transfer_id = %s, "
                        "transfer_amount = %s, transfer_error = '', transfer_updated_at = NOW() WHERE id = %s",
                        (transfer.get("id"), round(int(transfer.get("amount") or 0) / 100, 2), payment_row_id),
                    )
                else:
                    print(f"Route transfer failed for {gateway_payment_id}: {error}")
                    cur.execute(
                        "UPDATE driver_merchant_payments SET transfer_status = 'FAILED', transfer_error = %s, "
                        "transfer_updated_at = NOW() WHERE id = %s",
                        (error[:500], payment_row_id),
                    )
                conn.commit()
    except psycopg.Error as e:
        print(f"Route transfer bookkeeping failed for payment row {payment_row_id}: {e}")


def _sync_merchant_payments(owner_id):
    """Pulls recent payments on the driver's QR from Razorpay, so records appear even when
    the webhook cannot reach this server (local development), then sends pending transfers."""
    now = time.time()
    if now - _last_merchant_sync.get(owner_id, 0) < MERCHANT_SYNC_MIN_SECONDS:
        return
    _last_merchant_sync[owner_id] = now

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                creds = _driver_creds(cur, owner_id)
            except KeysEncryptionNotConfigured:
                _last_merchant_sync_error[owner_id] = "Server cannot read the saved Razorpay keys (encryption key missing or different)."
                return
            cur.execute(
                "SELECT gateway_qr_id FROM driver_merchant_accounts WHERE owner_id = %s AND gateway_qr_id IS NOT NULL "
                "AND gateway_mode = %s",
                (owner_id, _gateway_mode(creds)),
            )
            row = cur.fetchone()
            if row:
                _last_merchant_sync_error[owner_id] = None
                try:
                    items = _razorpay_request(
                        "GET", f"/payments/qr_codes/{row[0]}/payments?count=100", creds=creds
                    ).get("items", [])
                    for payment in items:
                        if payment.get("status") == "captured":
                            _record_merchant_payment(cur, owner_id, row[0], payment, direct=bool(creds))
                    conn.commit()
                except GatewayNotConfigured:
                    _last_merchant_sync_error[owner_id] = "Payment gateway keys are not configured on the server."
                except urllib.error.HTTPError as e:
                    detail = _gateway_error_text(e)
                    print(f"Razorpay merchant payment sync failed {e.code}: {detail}")
                    _last_merchant_sync_error[owner_id] = f"Razorpay refused the request ({e.code}): {detail or 'no details'}"
                except urllib.error.URLError as e:
                    print(f"Razorpay merchant payment sync failed: {e}")
                    _last_merchant_sync_error[owner_id] = f"Server could not reach Razorpay: {e.reason}"
            cur.execute(
                "SELECT id FROM driver_merchant_payments WHERE owner_id = %s "
                "AND transfer_status IN ('PENDING', 'FAILED', 'SENDING') AND transfer_attempts < %s ORDER BY id LIMIT 10",
                (owner_id, MAX_TRANSFER_ATTEMPTS),
            )
            unsent = [r[0] for r in cur.fetchall()]
    for payment_row_id in unsent:
        _transfer_to_driver(payment_row_id)


TEST_PAYMENT_PREFIX = "pay_TEST"
TEST_PAYERS = ("ramesh.k@okaxis", "sunita92@ybl", "9822012345@paytm", "amit.patil@oksbi", "priya.d@ibl")


@driver_bp.post("/merchant-payments/simulate")
def simulate_merchant_payment():
    """Test mode only: records a made-up payment on the driver's QR, exactly as a real one
    would be, so the grid and trips can be demonstrated without moving money."""
    payload = request.get_json(silent=True) or {}
    owner_id, denied = _token_owner(payload)
    if denied:
        return denied
    try:
        amount = round(float(payload.get("amount")), 2)
    except (TypeError, ValueError):
        amount = float(random.randrange(30, 260, 10))
    if amount <= 0:
        return jsonify({"error": "amount must be greater than 0"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT gateway_qr_id, gateway_mode, own_key_id FROM driver_merchant_accounts "
                    "WHERE owner_id = %s AND gateway_qr_id IS NOT NULL",
                    (owner_id,),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Set up the merchant account first."}), 404
                if row[1] == "live":
                    return jsonify({"error": "Test payments are only available with test keys."}), 403
                payment_row_id = _record_merchant_payment(cur, owner_id, row[0], {
                    "id": f"{TEST_PAYMENT_PREFIX}{os.urandom(6).hex()}",
                    "amount": int(round(amount * 100)),
                    "vpa": random.choice(TEST_PAYERS),
                    "acquirer_data": {"rrn": str(random.randrange(10**11, 10**12))},
                    "created_at": int(time.time()),
                })
                # Nothing real to transfer, so keep the Route poller away from it.
                cur.execute(
                    "UPDATE driver_merchant_payments SET transfer_status = %s WHERE id = %s",
                    ("DIRECT" if row[2] else "MANUAL", payment_row_id),
                )
                conn.commit()
        return jsonify({"message": "Test payment recorded"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@driver_bp.get("/merchant-payments")
def list_merchant_payments():
    owner_id, denied = _token_owner()
    if denied:
        return denied
    try:
        _sync_merchant_payments(owner_id)
        not_test = f"(%s OR gateway_payment_id NOT LIKE '{TEST_PAYMENT_PREFIX}%%')"
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Simulated payments are only shown while the driver's QR is a test-mode one.
                cur.execute("SELECT gateway_mode FROM driver_merchant_accounts WHERE owner_id = %s", (owner_id,))
                account = cur.fetchone()
                show_test = not account or account[0] != "live"
                cur.execute(
                    "SELECT id, amount, payer_vpa, rrn, gateway_payment_id, transfer_status, transfer_amount, paid_at, refunded_amount "
                    f"FROM driver_merchant_payments WHERE owner_id = %s AND {not_test} ORDER BY paid_at DESC, id DESC LIMIT 50",
                    (owner_id, show_test),
                )
                rows = cur.fetchall()
                cur.execute(
                    "SELECT COALESCE(SUM(amount - refunded_amount), 0), COUNT(*) FROM driver_merchant_payments "
                    f"WHERE owner_id = %s AND {not_test} AND paid_at::date = CURRENT_DATE",
                    (owner_id, show_test),
                )
                today_total, today_count = cur.fetchone()
        return jsonify({
            "payments": [{
                "id": str(r[0]),
                "amount": float(r[1]),
                "payerVpa": r[2],
                "rrn": r[3],
                "gatewayPaymentId": r[4],
                "transferStatus": r[5],
                "transferAmount": float(r[6]) if r[6] is not None else None,
                "paidAt": r[7].isoformat(),
                "refundedAmount": float(r[8]),
            } for r in rows],
            "todayTotal": float(today_total),
            "todayCount": int(today_count),
            "syncError": _last_merchant_sync_error.get(owner_id),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Razorpay webhook: every event is logged first (gateway_webhook_events), then processed.
# ---------------------------------------------------------------------------

def _handle_qr_credited(cur, entities, only_owner=None):
    """A payment landed on one of our QRs. Returns a driver_merchant_payments id whose Route
    transfer should be sent once the transaction is committed, or None.
    only_owner is set for a driver's own webhook: it may only touch that driver's QR."""
    gateway_qr_id = entities.get("qr_code", {}).get("entity", {}).get("id")
    payment = entities.get("payment", {}).get("entity", {})
    if not gateway_qr_id or not payment.get("id"):
        return None
    if only_owner is None:
        cur.execute("SELECT id FROM driver_qr_payments WHERE gateway_qr_id = %s", (gateway_qr_id,))
        row = cur.fetchone()
        if row:
            _record_paid(cur, row[0], payment["id"])
            return None
    # Not a per-fare QR: a driver's permanent merchant QR.
    cur.execute(
        "SELECT owner_id, own_key_id FROM driver_merchant_accounts "
        "WHERE gateway_qr_id = %s AND (%s::bigint IS NULL OR owner_id = %s)",
        (gateway_qr_id, only_owner, only_owner),
    )
    account = cur.fetchone()
    if not account:
        return None
    return _record_merchant_payment(cur, account[0], gateway_qr_id, payment, direct=bool(account[1]))


def _handle_refund(cur, entities):
    """Money went back to the passenger: reduce (or remove) the fare the driver was credited.
    Returns whether the refund matched a payment we know."""
    refund = entities.get("refund", {}).get("entity", {})
    payment = entities.get("payment", {}).get("entity", {})
    payment_id = refund.get("payment_id") or payment.get("id")
    if not payment_id:
        return False
    # The payment entity carries the running total; a lone refund entity only its own amount.
    refunded = round(int(payment.get("amount_refunded") or refund.get("amount") or 0) / 100, 2)
    if refunded <= 0:
        return False

    cur.execute(
        "UPDATE driver_merchant_payments SET refunded_amount = LEAST(amount, GREATEST(refunded_amount, %s)) "
        "WHERE gateway_payment_id = %s RETURNING trip_id, amount, refunded_amount",
        (refunded, payment_id),
    )
    row = cur.fetchone()
    if not row:
        # A per-fare QR payment: no refund column there, only its trip to correct.
        cur.execute(
            "SELECT trip_id, amount, LEAST(amount, %s::numeric) FROM driver_qr_payments WHERE gateway_payment_id = %s",
            (refunded, payment_id),
        )
        row = cur.fetchone()
    if not row:
        return False
    trip_id, amount, refunded_total = row
    if trip_id:
        remaining = amount - refunded_total
        if remaining <= 0:
            cur.execute("UPDATE driver_trips SET deleted = TRUE WHERE id = %s", (trip_id,))
        else:
            cur.execute("UPDATE driver_trips SET fare = %s WHERE id = %s", (remaining, trip_id))
    return True


def _handle_transfer(cur, event_type, entities):
    """Route transfer outcome for a payment already sent on to a driver."""
    transfer = entities.get("transfer", {}).get("entity", {})
    if not transfer.get("id"):
        return False
    if event_type == "transfer.failed":
        # Attempts are maxed so the poller does not keep re-adopting the failed transfer.
        cur.execute(
            "UPDATE driver_merchant_payments SET transfer_status = 'FAILED', transfer_error = %s, "
            "transfer_attempts = %s, transfer_updated_at = NOW() WHERE transfer_id = %s RETURNING id",
            (str((transfer.get("error") or {}).get("description") or "Transfer failed")[:500],
             MAX_TRANSFER_ATTEMPTS, transfer["id"]),
        )
    else:
        cur.execute(
            "UPDATE driver_merchant_payments SET transfer_status = 'CREATED', transfer_error = '', "
            "transfer_updated_at = NOW() WHERE transfer_id = %s RETURNING id",
            (transfer["id"],),
        )
    return cur.fetchone() is not None


@driver_bp.post("/razorpay-webhook/<int:owner_id>")
def razorpay_driver_webhook(owner_id):
    """Webhook for a driver's own Razorpay account, signed with the secret he saved in the app."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT own_webhook_secret_enc FROM driver_merchant_accounts WHERE owner_id = %s", (owner_id,))
                row = cur.fetchone()
        secret = _decrypt(row[0]) if row and row[0] else ""
    except (psycopg.Error, KeysEncryptionNotConfigured) as e:
        print(f"Driver webhook secret unavailable for {owner_id}: {type(e).__name__}")
        secret = ""
    return _receive_webhook(secret, only_owner=owner_id)


@driver_bp.post("/razorpay-webhook")
def razorpay_webhook():
    return _receive_webhook(os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip())


def _receive_webhook(secret, only_owner=None):
    if not secret:
        return jsonify({"error": "Webhook not configured"}), 503
    raw = request.get_data()
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("X-Razorpay-Signature", "")):
        return jsonify({"error": "Invalid signature"}), 400

    try:
        event = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        return jsonify({"error": "Invalid payload"}), 400
    event_type = str(event.get("event") or "")[:60]
    # Razorpay sends the same id on every retry of one event; fall back to the body hash.
    event_id = (request.headers.get("X-Razorpay-Event-Id") or hashlib.sha256(raw).hexdigest())[:80]

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Log before processing, so an event that crashes below is still on record.
                cur.execute(
                    "INSERT INTO gateway_webhook_events (event_id, event_type, payload) VALUES (%s, %s, %s) "
                    "ON CONFLICT (event_id) DO UPDATE SET attempts = gateway_webhook_events.attempts + 1 "
                    "RETURNING id, status",
                    (event_id, event_type, json.dumps(event)),
                )
                event_row_id, status = cur.fetchone()
                conn.commit()
                if status in ("PROCESSED", "IGNORED"):
                    return jsonify({"status": "duplicate"})

                merchant_payment_row = None
                try:
                    entities = event.get("payload") or {}
                    if event_type == "qr_code.credited":
                        merchant_payment_row = _handle_qr_credited(cur, entities, only_owner)
                        handled = True
                    elif event_type in ("refund.processed", "payment.refunded"):
                        handled = _handle_refund(cur, entities)
                    elif event_type in ("transfer.processed", "transfer.failed"):
                        handled = _handle_transfer(cur, event_type, entities)
                    else:
                        handled = False
                    cur.execute(
                        "UPDATE gateway_webhook_events SET status = %s, error = '', processed_at = NOW() WHERE id = %s",
                        ("PROCESSED" if handled else "IGNORED", event_row_id),
                    )
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    print(f"Razorpay webhook {event_type} ({event_id}) failed: {e}")
                    cur.execute(
                        "UPDATE gateway_webhook_events SET status = 'FAILED', error = %s WHERE id = %s",
                        (str(e)[:500], event_row_id),
                    )
                    conn.commit()
                    # A non-2xx reply makes Razorpay retry; the retry reprocesses this logged event.
                    return jsonify({"error": "Processing failed"}), 500
        if merchant_payment_row:
            _transfer_to_driver(merchant_payment_row)
        return jsonify({"status": "ok"})
    except psycopg.Error as e:
        print(f"Razorpay webhook could not be logged: {e}")
        return jsonify({"error": "Temporarily unavailable"}), 500
