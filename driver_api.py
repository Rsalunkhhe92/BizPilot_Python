"""
Auto Driver REST API (Flask Blueprint), mounted at /api/driver.

- Trips are stored server-side (driver_trips).
- The driver's own static UPI QR image is stored as a base64 data URI (driver_upi_qr).
- QR payment tracking: for each fare a single-use, fixed-amount Razorpay UPI QR is created
  (driver_qr_payments). Payment is confirmed by polling Razorpay (works without a public
  URL) and/or by the Razorpay webhook; on confirmation a UPI trip is recorded automatically.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
import psycopg

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
        ALTER TABLE driver_trips ADD CONSTRAINT driver_trips_source_check CHECK (source IN ('MANUAL', 'QR', 'SMS'));
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


@driver_bp.get("/trips")
def list_trips():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        days = request.args.get("days")
        try:
            days = max(1, min(90, int(days))) if days else None
        except (TypeError, ValueError):
            days = None
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if days:
                    # days=7 means "today plus the 6 days before it"
                    cur.execute(
                        f"SELECT {TRIP_COLUMNS} FROM driver_trips WHERE owner_id = %s "
                        "AND NOT deleted AND trip_time::date >= CURRENT_DATE - (%s - 1) ORDER BY trip_time DESC",
                        (owner_id, days),
                    )
                else:
                    cur.execute(
                        f"SELECT {TRIP_COLUMNS} FROM driver_trips WHERE owner_id = %s "
                        "AND NOT deleted AND trip_time::date = CURRENT_DATE ORDER BY trip_time DESC",
                        (owner_id,),
                    )
                trips = [serialize_trip(r) for r in cur.fetchall()]
        return jsonify({"trips": trips})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def serialize_fuel_log(row):
    fid, fuel_type, quantity, rate, total_cost, odometer, station, fuel_time = row
    return {
        "id": str(fid),
        "fuelType": fuel_type,
        "quantity": float(quantity or 0),
        "rate": float(rate or 0),
        "totalCost": float(total_cost or 0),
        "odometer": float(odometer) if odometer is not None else None,
        "station": station or "",
        "fuelTime": fuel_time.isoformat(),
    }


FUEL_LOG_COLUMNS = "id, fuel_type, quantity, rate, total_cost, odometer, station, fuel_time"


@driver_bp.get("/fuel-logs")
def list_fuel_logs():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        days = request.args.get("days")
        try:
            days = max(1, min(90, int(days))) if days else None
        except (TypeError, ValueError):
            days = None
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if days:
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
    if fuel_type not in ("CNG", "Petrol", "Diesel"):
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
                        (owner_id, fuel_type, quantity, rate, total_cost, odometer, station)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING {FUEL_LOG_COLUMNS}
                    """,
                    (owner_id, fuel_type, quantity, rate, total_cost, odometer, str(payload.get("station", "")).strip()),
                )
                row = cur.fetchone()
                conn.commit()
        return jsonify({"fuelLog": serialize_fuel_log(row)}), 201
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
# QR payment tracking (Razorpay dynamic UPI QR)
# ---------------------------------------------------------------------------

RAZORPAY_API = "https://api.razorpay.com/v1"
QR_VALIDITY_SECONDS = 15 * 60


class GatewayNotConfigured(Exception):
    pass


def _razorpay_request(method, path, body=None):
    key = os.getenv("RAZORPAY_KEY_ID", "").strip()
    secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
    if not key or not secret:
        raise GatewayNotConfigured()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    auth = base64.b64encode(f"{key}:{secret}".encode()).decode()
    req = urllib.request.Request(
        RAZORPAY_API + path, data=data, method=method,
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


@driver_bp.post("/razorpay-webhook")
def razorpay_webhook():
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
    if not secret:
        return jsonify({"error": "Webhook not configured"}), 503
    raw = request.get_data()
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("X-Razorpay-Signature", "")):
        return jsonify({"error": "Invalid signature"}), 400

    event = json.loads(raw.decode("utf-8") or "{}")
    if event.get("event") == "qr_code.credited":
        entities = event.get("payload", {})
        gateway_qr_id = entities.get("qr_code", {}).get("entity", {}).get("id")
        payment_id = entities.get("payment", {}).get("entity", {}).get("id")
        if gateway_qr_id and payment_id:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM driver_qr_payments WHERE gateway_qr_id = %s", (gateway_qr_id,))
                    row = cur.fetchone()
                    if row:
                        _record_paid(cur, row[0], payment_id)
                        conn.commit()
    return jsonify({"status": "ok"})
