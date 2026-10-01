"""
Travels Bus Booking Online REST API (Flask Blueprint), mounted at /api/travel.

A travel operator creates Trips (route + date + departure time + total seats + fare).
Each trip has its own independent seat map (seat numbers 1..totalSeats). Booking a seat
records a passenger against it; the seat map shows booked (green) vs available (red) -
this is the app's own booking record, not connected to any external bus reservation system.

Security follows the same pattern as inventory_api.py / driver_api.py / building_api.py:
the caller passes ownerId (or X-User-Id), and every trip/seat endpoint re-verifies that the
trip belongs to that owner before reading or writing anything.
"""

import os
from datetime import date, datetime

from flask import Blueprint, jsonify, request
import psycopg

travel_bp = Blueprint("travel", __name__, url_prefix="/api/travel")

MAX_SEATS = 80


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


def ensure_travel_schema(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS travel_trips (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            route VARCHAR(200) NOT NULL,
            travel_date DATE NOT NULL,
            departure_time VARCHAR(10) NOT NULL DEFAULT '',
            bus_number VARCHAR(30) DEFAULT '',
            bus_type VARCHAR(10) NOT NULL DEFAULT 'seater' CHECK (bus_type IN ('seater', 'sleeper')),
            total_seats INT NOT NULL CHECK (total_seats > 0 AND total_seats <= 80),
            sleeper_seats INT NOT NULL DEFAULT 0,
            fare NUMERIC(10, 2) NOT NULL DEFAULT 0,
            status VARCHAR(12) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled', 'completed')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE travel_trips ADD COLUMN IF NOT EXISTS bus_type VARCHAR(10) NOT NULL DEFAULT 'seater';
        ALTER TABLE travel_trips ADD COLUMN IF NOT EXISTS sleeper_seats INT NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_travel_trips_owner_date ON travel_trips(owner_id, travel_date DESC);

        CREATE TABLE IF NOT EXISTS travel_seat_bookings (
            id BIGSERIAL PRIMARY KEY,
            trip_id BIGINT NOT NULL REFERENCES travel_trips(id) ON DELETE CASCADE,
            seat_number INT NOT NULL,
            passenger_name VARCHAR(150) NOT NULL,
            mobile_number VARCHAR(20) DEFAULT '',
            fare NUMERIC(10, 2) NOT NULL DEFAULT 0,
            payment_mode VARCHAR(10) NOT NULL DEFAULT 'Cash' CHECK (payment_mode IN ('Cash', 'UPI', 'Online')),
            payment_status VARCHAR(10) NOT NULL DEFAULT 'paid',
            notes TEXT DEFAULT '',
            booked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_trip_seat UNIQUE (trip_id, seat_number)
        );
        ALTER TABLE travel_seat_bookings ADD COLUMN IF NOT EXISTS payment_status VARCHAR(10) NOT NULL DEFAULT 'paid';
        ALTER TABLE travel_seat_bookings ADD COLUMN IF NOT EXISTS pickup_location VARCHAR(150) NOT NULL DEFAULT '';
        ALTER TABLE travel_seat_bookings ADD COLUMN IF NOT EXISTS drop_location VARCHAR(150) NOT NULL DEFAULT '';
        CREATE INDEX IF NOT EXISTS idx_travel_bookings_trip ON travel_seat_bookings(trip_id);

        CREATE TABLE IF NOT EXISTS travel_route_stops (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            route VARCHAR(200) NOT NULL,
            stop_name VARCHAR(150) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_travel_route_stop ON travel_route_stops (owner_id, LOWER(route), LOWER(stop_name));
        CREATE INDEX IF NOT EXISTS idx_travel_route_stops_owner_route ON travel_route_stops(owner_id, route);

        CREATE TABLE IF NOT EXISTS travel_seat_blocks (
            id BIGSERIAL PRIMARY KEY,
            trip_id BIGINT NOT NULL REFERENCES travel_trips(id) ON DELETE CASCADE,
            seat_number INT NOT NULL,
            reason VARCHAR(100) DEFAULT '',
            blocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_trip_seat_block UNIQUE (trip_id, seat_number)
        );
        CREATE INDEX IF NOT EXISTS idx_travel_seat_blocks_trip ON travel_seat_blocks(trip_id);

        CREATE TABLE IF NOT EXISTS travel_fuel_logs (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            trip_id BIGINT REFERENCES travel_trips(id) ON DELETE SET NULL,
            fuel_type VARCHAR(20) NOT NULL DEFAULT 'Diesel' CHECK (fuel_type IN ('Diesel', 'Petrol', 'CNG')),
            quantity NUMERIC(10, 2) NOT NULL CHECK (quantity > 0),
            rate NUMERIC(10, 2) NOT NULL CHECK (rate > 0),
            total_cost NUMERIC(10, 2) NOT NULL CHECK (total_cost > 0),
            odometer NUMERIC(10, 1) DEFAULT 0,
            station VARCHAR(200) DEFAULT '',
            bill_number VARCHAR(100) DEFAULT '',
            notes TEXT DEFAULT '',
            fuel_date DATE NOT NULL DEFAULT CURRENT_DATE,
            fuel_time VARCHAR(20) DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_travel_fuel_owner ON travel_fuel_logs(owner_id, fuel_date DESC);
        CREATE INDEX IF NOT EXISTS idx_travel_fuel_trip ON travel_fuel_logs(trip_id);

        CREATE TABLE IF NOT EXISTS travel_vehicles (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES userdetails(id) ON DELETE CASCADE,
            reg_number VARCHAR(30) NOT NULL,
            model VARCHAR(150) DEFAULT '',
            bus_type VARCHAR(10) NOT NULL DEFAULT 'seater' CHECK (bus_type IN ('seater', 'sleeper')),
            fuel_type VARCHAR(20) NOT NULL DEFAULT 'Diesel' CHECK (fuel_type IN ('Diesel', 'Petrol', 'CNG', 'Electric')),
            reg_date DATE,
            total_km NUMERIC(10, 1) NOT NULL DEFAULT 0,
            insurance_expiry DATE,
            fitness_expiry DATE,
            puc_expiry DATE,
            status VARCHAR(12) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE travel_vehicles ADD COLUMN IF NOT EXISTS bus_type VARCHAR(10) NOT NULL DEFAULT 'seater';
        CREATE INDEX IF NOT EXISTS idx_travel_vehicles_owner ON travel_vehicles(owner_id);
    """)


def _seat_type(seat_number, total_seats, sleeper_seats, bus_type):
    """Which type a given seat number is: 'seater' or 'sleeper'.

    Seats are laid out as [seater seats 1..seaterCount][sleeper berths seaterCount+1..total].
    A pure seater bus (bus_type='seater') never has sleeper seats. A sleeper bus with
    sleeper_seats=0 is treated as legacy/all-berths (trips created before mixed seating
    was supported), so it still renders as a full sleeper layout.
    """
    if bus_type != "sleeper":
        return "seater"
    if sleeper_seats <= 0:
        return "sleeper"
    seater_count = total_seats - sleeper_seats
    return "seater" if seat_number <= seater_count else "sleeper"


def serialize_trip(row):
    (tid, route, travel_date, departure_time, bus_number, bus_type, total_seats, sleeper_seats, fare, status,
     booked_count, revenue, pending_amount, blocked_count, fuel_cost, fuel_quantity) = row
    sleeper_seats = int(sleeper_seats or 0)
    booked_count = int(booked_count or 0)
    blocked_count = int(blocked_count or 0)
    return {
        "id": str(tid),
        "route": route,
        "travelDate": travel_date.isoformat(),
        "departureTime": departure_time or "",
        "busNumber": bus_number or "",
        "busType": bus_type or "seater",
        "totalSeats": total_seats,
        "sleeperSeats": sleeper_seats,
        "seaterSeats": total_seats - sleeper_seats,
        "fare": float(fare or 0),
        "status": status,
        "bookedCount": booked_count,
        "blockedCount": blocked_count,
        "availableCount": total_seats - booked_count - blocked_count,
        "revenue": float(revenue or 0),
        "pendingAmount": float(pending_amount or 0),
        "fuelCost": float(fuel_cost or 0),
        "fuelQuantity": float(fuel_quantity or 0),
    }


def serialize_fuel_log(row):
    (fid, owner_id, trip_id, fuel_type, quantity, rate, total_cost, odometer, station, bill_number, notes, fuel_date, fuel_time, created_at,
     trip_route, trip_date, trip_bus_number, trip_departure_time) = row
    return {
        "id": str(fid),
        "ownerId": owner_id,
        "tripId": str(trip_id) if trip_id else None,
        "fuelType": fuel_type or "Diesel",
        "quantity": float(quantity or 0),
        "rate": float(rate or 0),
        "totalCost": float(total_cost or 0),
        "odometer": float(odometer or 0),
        "station": station or "",
        "billNumber": bill_number or "",
        "notes": notes or "",
        "fuelDate": fuel_date.isoformat() if hasattr(fuel_date, "isoformat") else str(fuel_date),
        "fuelTime": fuel_time or "",
        "createdAt": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
        "tripRoute": trip_route or "",
        "tripDate": trip_date.isoformat() if trip_date and hasattr(trip_date, "isoformat") else (str(trip_date) if trip_date else ""),
        "tripBusNumber": trip_bus_number or "",
        "tripDepartureTime": trip_departure_time or "",
    }


TRIP_SELECT = """
    SELECT t.id, t.route, t.travel_date, t.departure_time, t.bus_number, t.bus_type, t.total_seats,
           t.sleeper_seats, t.fare, t.status,
           COALESCE(bk.booked_count, 0), COALESCE(bk.revenue, 0), COALESCE(bk.pending_amount, 0),
           COALESCE(bl.blocked_count, 0),
           COALESCE(fl.fuel_cost, 0), COALESCE(fl.fuel_quantity, 0)
    FROM travel_trips t
    LEFT JOIN (
        SELECT trip_id, COUNT(*) AS booked_count,
               COALESCE(SUM(fare) FILTER (WHERE payment_status = 'paid'), 0) AS revenue,
               COALESCE(SUM(fare) FILTER (WHERE payment_status = 'pending'), 0) AS pending_amount
        FROM travel_seat_bookings GROUP BY trip_id
    ) bk ON bk.trip_id = t.id
    LEFT JOIN (
        SELECT trip_id, COUNT(*) AS blocked_count
        FROM travel_seat_blocks GROUP BY trip_id
    ) bl ON bl.trip_id = t.id
    LEFT JOIN (
        SELECT trip_id,
               COALESCE(SUM(total_cost), 0) AS fuel_cost,
               COALESCE(SUM(quantity), 0) AS fuel_quantity
        FROM travel_fuel_logs GROUP BY trip_id
    ) fl ON fl.trip_id = t.id
"""
TRIP_GROUP_BY = ""  # aggregation now happens in TRIP_SELECT's subqueries; no outer GROUP BY needed


def _get_owned_trip(cur, trip_id, owner_id):
    cur.execute(
        "SELECT id, total_seats, bus_type, sleeper_seats FROM travel_trips WHERE id = %s AND owner_id = %s",
        (trip_id, owner_id),
    )
    return cur.fetchone()


@travel_bp.get("/trips")
def list_trips():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"{TRIP_SELECT} WHERE t.owner_id = %s AND t.status != 'cancelled' {TRIP_GROUP_BY} "
                    "ORDER BY t.travel_date DESC, t.departure_time DESC",
                    (owner_id,),
                )
                trips = [serialize_trip(r) for r in cur.fetchall()]
        return jsonify({"trips": trips})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.get("/route-stops")
def list_route_stops():
    """Owner-managed pickup/drop stop list for a route (case-insensitive match on route name)."""
    owner_id = get_owner_id()
    route = (request.args.get("route") or "").strip()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    if not route:
        return jsonify({"stops": []})
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT stop_name FROM travel_route_stops WHERE owner_id = %s AND LOWER(route) = LOWER(%s) ORDER BY stop_name",
                    (owner_id, route),
                )
                stops = [r[0] for r in cur.fetchall()]
        return jsonify({"stops": stops})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.post("/route-stops")
def add_route_stop():
    """Adds a stop to a route's pickup/drop list (called from the New Trip form)."""
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    route = str(payload.get("route", "")).strip()
    stop_name = str(payload.get("stopName", "")).strip()
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not route or not stop_name:
        return jsonify({"error": "route and stopName are required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO travel_route_stops (owner_id, route, stop_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (owner_id, LOWER(route), LOWER(stop_name)) DO NOTHING
                    """,
                    (owner_id, route, stop_name),
                )
                cur.execute(
                    "SELECT stop_name FROM travel_route_stops WHERE owner_id = %s AND LOWER(route) = LOWER(%s) ORDER BY stop_name",
                    (owner_id, route),
                )
                stops = [r[0] for r in cur.fetchall()]
                conn.commit()
        return jsonify({"stops": stops}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.delete("/route-stops")
def delete_route_stop():
    """Removes a stop from a route's pickup/drop list."""
    owner_id = get_owner_id()
    route = (request.args.get("route") or "").strip()
    stop_name = (request.args.get("stopName") or "").strip()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    if not route or not stop_name:
        return jsonify({"error": "route and stopName are required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM travel_route_stops WHERE owner_id = %s AND LOWER(route) = LOWER(%s) AND LOWER(stop_name) = LOWER(%s)",
                    (owner_id, route, stop_name),
                )
                conn.commit()
        return jsonify({"message": "Stop removed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.post("/trips")
def create_trip():
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    route = str(payload.get("route", "")).strip()
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not route:
        return jsonify({"error": "route is required"}), 400
    try:
        total_seats = int(payload.get("totalSeats"))
    except (TypeError, ValueError):
        total_seats = 0
    if total_seats <= 0 or total_seats > MAX_SEATS:
        return jsonify({"error": f"totalSeats must be between 1 and {MAX_SEATS}"}), 400
    try:
        travel_date = date.fromisoformat(str(payload.get("travelDate", "")).strip())
    except ValueError:
        return jsonify({"error": "travelDate must be YYYY-MM-DD"}), 400
    try:
        fare = float(payload.get("fare") or 0)
    except (TypeError, ValueError):
        fare = 0
    bus_type = str(payload.get("busType", "seater")).strip().lower()
    if bus_type not in ("seater", "sleeper"):
        bus_type = "seater"
    try:
        sleeper_seats = int(payload.get("sleeperSeats") or 0)
    except (TypeError, ValueError):
        sleeper_seats = 0
    if bus_type != "sleeper":
        sleeper_seats = 0
    if sleeper_seats < 0 or sleeper_seats > total_seats:
        return jsonify({"error": "sleeperSeats must be between 0 and totalSeats"}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO travel_trips
                        (owner_id, route, travel_date, departure_time, bus_number, bus_type, total_seats, sleeper_seats, fare)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                    """,
                    (owner_id, route, travel_date, str(payload.get("departureTime", "")).strip(),
                     str(payload.get("busNumber", "")).strip(), bus_type, total_seats, sleeper_seats, fare),
                )
                trip_id = cur.fetchone()[0]
                cur.execute(f"{TRIP_SELECT} WHERE t.id = %s {TRIP_GROUP_BY}", (trip_id,))
                trip = serialize_trip(cur.fetchone())
                conn.commit()
        return jsonify({"trip": trip}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.put("/trips/<int:trip_id>")
def update_trip(trip_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    fields, values = [], []
    if "route" in payload:
        fields.append("route = %s")
        values.append(str(payload.get("route", "")).strip())
    if "departureTime" in payload:
        fields.append("departure_time = %s")
        values.append(str(payload.get("departureTime", "")).strip())
    if "busNumber" in payload:
        fields.append("bus_number = %s")
        values.append(str(payload.get("busNumber", "")).strip())
    if "busType" in payload:
        bus_type = str(payload.get("busType", "")).strip().lower()
        if bus_type not in ("seater", "sleeper"):
            return jsonify({"error": "busType must be 'seater' or 'sleeper'"}), 400
        fields.append("bus_type = %s")
        values.append(bus_type)
        if bus_type != "sleeper":
            fields.append("sleeper_seats = 0")
    if "fare" in payload:
        try:
            fields.append("fare = %s")
            values.append(float(payload.get("fare") or 0))
        except (TypeError, ValueError):
            return jsonify({"error": "fare must be a number"}), 400
    if "status" in payload:
        status = str(payload.get("status")).strip()
        if status not in ("active", "cancelled", "completed"):
            return jsonify({"error": "invalid status"}), 400
        fields.append("status = %s")
        values.append(status)
    if not fields:
        return jsonify({"error": "No fields to update"}), 400
    fields.append("updated_at = NOW()")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE travel_trips SET {', '.join(fields)} WHERE id = %s AND owner_id = %s RETURNING id",
                    values + [trip_id, owner_id],
                )
                if not cur.fetchone():
                    return jsonify({"error": "Trip not found"}), 404
                cur.execute(f"{TRIP_SELECT} WHERE t.id = %s {TRIP_GROUP_BY}", (trip_id,))
                trip = serialize_trip(cur.fetchone())
                conn.commit()
        return jsonify({"trip": trip})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.get("/trips/<int:trip_id>/seats")
def get_seats(trip_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                trip_row = _get_owned_trip(cur, trip_id, owner_id)
                if not trip_row:
                    return jsonify({"error": "Trip not found"}), 404
                total_seats = trip_row[1]
                bus_type = trip_row[2]
                sleeper_seats = int(trip_row[3] or 0)
                cur.execute(
                    "SELECT seat_number, passenger_name, mobile_number, fare, payment_mode, payment_status, notes, booked_at, "
                    "pickup_location, drop_location "
                    "FROM travel_seat_bookings WHERE trip_id = %s",
                    (trip_id,),
                )
                bookings = {
                    r[0]: {
                        "passengerName": r[1], "mobileNumber": r[2] or "", "fare": float(r[3] or 0),
                        "paymentMode": r[4], "paymentStatus": r[5] or "paid", "notes": r[6] or "", "bookedAt": r[7].isoformat(),
                        "pickupLocation": r[8] or "", "dropLocation": r[9] or "",
                    }
                    for r in cur.fetchall()
                }
                cur.execute("SELECT seat_number, reason FROM travel_seat_blocks WHERE trip_id = %s", (trip_id,))
                blocks = {r[0]: (r[1] or "") for r in cur.fetchall()}
        seats = []
        for n in range(1, total_seats + 1):
            booking = bookings.get(n)
            blocked = n in blocks
            seats.append({
                "seatNumber": n,
                "seatType": _seat_type(n, total_seats, sleeper_seats, bus_type),
                "booked": booking is not None,
                "blocked": blocked,
                **({"booking": booking} if booking else {}),
                **({"blockReason": blocks[n]} if blocked else {}),
            })
        return jsonify({"seats": seats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.post("/trips/<int:trip_id>/seats/<int:seat_number>")
def book_seat(trip_id, seat_number):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    passenger_name = str(payload.get("passengerName", "")).strip()
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not passenger_name:
        return jsonify({"error": "passengerName is required"}), 400

    payment_mode = str(payload.get("paymentMode", "Cash")).strip()
    if payment_mode not in ("Cash", "UPI", "Online"):
        payment_mode = "Cash"
    payment_status = str(payload.get("paymentStatus", "paid")).strip()
    if payment_status not in ("paid", "pending"):
        payment_status = "paid"
    try:
        fare = float(payload.get("fare")) if payload.get("fare") not in (None, "") else None
    except (TypeError, ValueError):
        fare = None

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                trip_row = _get_owned_trip(cur, trip_id, owner_id)
                if not trip_row:
                    return jsonify({"error": "Trip not found"}), 404
                total_seats = trip_row[1]
                if seat_number < 1 or seat_number > total_seats:
                    return jsonify({"error": "Invalid seat number"}), 400
                cur.execute(
                    "SELECT 1 FROM travel_seat_blocks WHERE trip_id = %s AND seat_number = %s",
                    (trip_id, seat_number),
                )
                if cur.fetchone():
                    return jsonify({"error": "This seat is blocked"}), 409
                if fare is None:
                    cur.execute("SELECT fare FROM travel_trips WHERE id = %s", (trip_id,))
                    fare = float(cur.fetchone()[0] or 0)
                cur.execute(
                    """
                    INSERT INTO travel_seat_bookings
                        (trip_id, seat_number, passenger_name, mobile_number, fare, payment_mode, payment_status, notes,
                         pickup_location, drop_location)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (trip_id, seat_number) DO NOTHING
                    RETURNING id
                    """,
                    (trip_id, seat_number, passenger_name, str(payload.get("mobileNumber", "")).strip(),
                     fare, payment_mode, payment_status, str(payload.get("notes", "")).strip(),
                     str(payload.get("pickupLocation", "")).strip(), str(payload.get("dropLocation", "")).strip()),
                )
                if not cur.fetchone():
                    return jsonify({"error": "This seat is already booked"}), 409
                conn.commit()
        return jsonify({"message": "Seat booked"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.put("/trips/<int:trip_id>/seats/<int:seat_number>/payment")
def update_payment(trip_id, seat_number):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    fields, values = [], []
    if "paymentStatus" in payload:
        payment_status = str(payload.get("paymentStatus", "")).strip()
        if payment_status not in ("paid", "pending"):
            return jsonify({"error": "paymentStatus must be 'paid' or 'pending'"}), 400
        fields.append("payment_status = %s")
        values.append(payment_status)
    if "paymentMode" in payload:
        payment_mode = str(payload.get("paymentMode", "")).strip()
        if payment_mode not in ("Cash", "UPI", "Online"):
            return jsonify({"error": "paymentMode must be 'Cash', 'UPI' or 'Online'"}), 400
        fields.append("payment_mode = %s")
        values.append(payment_mode)
    if not fields:
        return jsonify({"error": "No fields to update"}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_trip(cur, trip_id, owner_id):
                    return jsonify({"error": "Trip not found"}), 404
                cur.execute(
                    f"UPDATE travel_seat_bookings SET {', '.join(fields)} "
                    "WHERE trip_id = %s AND seat_number = %s RETURNING id",
                    values + [trip_id, seat_number],
                )
                if not cur.fetchone():
                    return jsonify({"error": "Booking not found"}), 404
                conn.commit()
        return jsonify({"message": "Payment updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.post("/trips/<int:trip_id>/seats/<int:seat_number>/block")
def block_seat(trip_id, seat_number):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                trip_row = _get_owned_trip(cur, trip_id, owner_id)
                if not trip_row:
                    return jsonify({"error": "Trip not found"}), 404
                total_seats = trip_row[1]
                if seat_number < 1 or seat_number > total_seats:
                    return jsonify({"error": "Invalid seat number"}), 400
                cur.execute(
                    "SELECT 1 FROM travel_seat_bookings WHERE trip_id = %s AND seat_number = %s",
                    (trip_id, seat_number),
                )
                if cur.fetchone():
                    return jsonify({"error": "This seat is already booked"}), 409
                cur.execute(
                    """
                    INSERT INTO travel_seat_blocks (trip_id, seat_number, reason)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (trip_id, seat_number) DO NOTHING
                    RETURNING id
                    """,
                    (trip_id, seat_number, str(payload.get("reason", "")).strip()),
                )
                if not cur.fetchone():
                    return jsonify({"error": "This seat is already blocked"}), 409
                conn.commit()
        return jsonify({"message": "Seat blocked"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.delete("/trips/<int:trip_id>/seats/<int:seat_number>/block")
def unblock_seat(trip_id, seat_number):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_trip(cur, trip_id, owner_id):
                    return jsonify({"error": "Trip not found"}), 404
                cur.execute(
                    "DELETE FROM travel_seat_blocks WHERE trip_id = %s AND seat_number = %s RETURNING id",
                    (trip_id, seat_number),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Seat is not blocked"}), 404
                conn.commit()
        return jsonify({"message": "Seat unblocked"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.delete("/trips/<int:trip_id>/seats/<int:seat_number>")
def cancel_seat(trip_id, seat_number):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_trip(cur, trip_id, owner_id):
                    return jsonify({"error": "Trip not found"}), 404
                cur.execute(
                    "DELETE FROM travel_seat_bookings WHERE trip_id = %s AND seat_number = %s RETURNING id",
                    (trip_id, seat_number),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Booking not found"}), 404
                conn.commit()
        return jsonify({"message": "Booking cancelled"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.get("/fuel-logs")
def list_fuel_logs():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    trip_id = request.args.get("trip_id")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                ensure_travel_schema(cur)
                query = """
                    SELECT f.id, f.owner_id, f.trip_id, f.fuel_type, f.quantity, f.rate, f.total_cost,
                           f.odometer, f.station, f.bill_number, f.notes, f.fuel_date, f.fuel_time, f.created_at,
                           t.route AS trip_route, t.travel_date AS trip_date, t.bus_number AS trip_bus_number,
                           t.departure_time AS trip_departure_time
                    FROM travel_fuel_logs f
                    LEFT JOIN travel_trips t ON f.trip_id = t.id
                    WHERE f.owner_id = %s
                """
                params = [owner_id]
                if trip_id and str(trip_id).strip() and str(trip_id).lower() != "all":
                    query += " AND f.trip_id = %s"
                    params.append(int(trip_id))
                query += " ORDER BY f.fuel_date DESC, f.id DESC"
                cur.execute(query, tuple(params))
                logs = [serialize_fuel_log(r) for r in cur.fetchall()]

                total_cost = sum(log["totalCost"] for log in logs)
                total_qty = sum(log["quantity"] for log in logs)
                avg_rate = round(total_cost / total_qty, 2) if total_qty > 0 else 0

                return jsonify({
                    "fuelLogs": logs,
                    "summary": {
                        "totalCost": total_cost,
                        "totalQuantity": round(total_qty, 2),
                        "avgRate": avg_rate,
                        "count": len(logs),
                    }
                })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.post("/fuel-logs")
def create_fuel_log():
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400

    try:
        quantity = float(payload.get("quantity") or 0)
        rate = float(payload.get("rate") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Valid quantity and rate are required"}), 400

    if quantity <= 0 or rate <= 0:
        return jsonify({"error": "Quantity and rate must be greater than zero"}), 400

    total_cost = payload.get("totalCost")
    if total_cost is None or total_cost == "":
        total_cost = round(quantity * rate, 2)
    else:
        try:
            total_cost = float(total_cost)
        except (TypeError, ValueError):
            total_cost = round(quantity * rate, 2)

    trip_id = payload.get("tripId")
    if trip_id in (None, "", "null", "undefined", 0):
        trip_id = None
    else:
        try:
            trip_id = int(trip_id)
        except (TypeError, ValueError):
            trip_id = None

    fuel_type = str(payload.get("fuelType", "Diesel")).strip()
    if fuel_type not in ("Diesel", "Petrol", "CNG"):
        fuel_type = "Diesel"

    odometer = 0.0
    try:
        odometer = float(payload.get("odometer") or 0)
    except (TypeError, ValueError):
        pass

    station = str(payload.get("station", "")).strip()
    bill_number = str(payload.get("billNumber", "")).strip()
    notes = str(payload.get("notes", "")).strip()

    raw_date = str(payload.get("fuelDate", "")).strip()
    fuel_date_val = date.today()
    if raw_date:
        try:
            fuel_date_val = date.fromisoformat(raw_date[:10])
        except Exception:
            pass

    fuel_time_val = str(payload.get("fuelTime", "")).strip()
    if not fuel_time_val:
        fuel_time_val = datetime.now().strftime("%I:%M %p")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                ensure_travel_schema(cur)
                # Verify trip ownership if trip_id provided
                if trip_id:
                    cur.execute("SELECT id FROM travel_trips WHERE id = %s AND owner_id = %s", (trip_id, owner_id))
                    if not cur.fetchone():
                        return jsonify({"error": "Trip not found or does not belong to user"}), 404

                cur.execute(
                    """
                    INSERT INTO travel_fuel_logs
                        (owner_id, trip_id, fuel_type, quantity, rate, total_cost, odometer, station, bill_number, notes, fuel_date, fuel_time)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (owner_id, trip_id, fuel_type, quantity, rate, total_cost, odometer, station, bill_number, notes, fuel_date_val, fuel_time_val)
                )
                new_id = cur.fetchone()[0]
                conn.commit()

                # Fetch full serialized row with trip joined
                cur.execute(
                    """
                    SELECT f.id, f.owner_id, f.trip_id, f.fuel_type, f.quantity, f.rate, f.total_cost,
                           f.odometer, f.station, f.bill_number, f.notes, f.fuel_date, f.fuel_time, f.created_at,
                           t.route AS trip_route, t.travel_date AS trip_date, t.bus_number AS trip_bus_number,
                           t.departure_time AS trip_departure_time
                    FROM travel_fuel_logs f
                    LEFT JOIN travel_trips t ON f.trip_id = t.id
                    WHERE f.id = %s
                    """,
                    (new_id,)
                )
                row = cur.fetchone()
                return jsonify({"fuelLog": serialize_fuel_log(row)}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.delete("/fuel-logs/<int:fuel_id>")
def delete_fuel_log(fuel_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                ensure_travel_schema(cur)
                cur.execute(
                    "DELETE FROM travel_fuel_logs WHERE id = %s AND owner_id = %s RETURNING id",
                    (fuel_id, owner_id)
                )
                if not cur.fetchone():
                    return jsonify({"error": "Fuel log entry not found"}), 404
                conn.commit()
        return jsonify({"message": "Fuel log deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def serialize_vehicle(row):
    (vid, owner_id, reg_number, model, bus_type, fuel_type, reg_date, total_km,
     insurance_expiry, fitness_expiry, puc_expiry, status, created_at, updated_at) = row
    return {
        "id": str(vid),
        "ownerId": owner_id,
        "regNumber": reg_number,
        "model": model or "",
        "busType": bus_type or "seater",
        "fuelType": fuel_type or "Diesel",
        "regDate": reg_date.isoformat() if reg_date else "",
        "totalKm": float(total_km or 0),
        "insuranceExpiry": insurance_expiry.isoformat() if insurance_expiry else "",
        "fitnessExpiry": fitness_expiry.isoformat() if fitness_expiry else "",
        "pucExpiry": puc_expiry.isoformat() if puc_expiry else "",
        "status": status or "active",
        "createdAt": created_at.isoformat() if created_at else "",
        "updatedAt": updated_at.isoformat() if updated_at else "",
    }


VEHICLE_SELECT = """
    SELECT id, owner_id, reg_number, model, bus_type, fuel_type, reg_date, total_km,
           insurance_expiry, fitness_expiry, puc_expiry, status, created_at, updated_at
    FROM travel_vehicles
"""


def _parse_optional_date(raw):
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


@travel_bp.get("/vehicles")
def list_vehicles():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                ensure_travel_schema(cur)
                cur.execute(
                    f"{VEHICLE_SELECT} WHERE owner_id = %s ORDER BY status = 'active' DESC, created_at DESC",
                    (owner_id,),
                )
                vehicles = [serialize_vehicle(r) for r in cur.fetchall()]
        return jsonify({"vehicles": vehicles})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.post("/vehicles")
def create_vehicle():
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    reg_number = str(payload.get("regNumber", "")).strip()
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not reg_number:
        return jsonify({"error": "regNumber is required"}), 400

    fuel_type = str(payload.get("fuelType", "Diesel")).strip()
    if fuel_type not in ("Diesel", "Petrol", "CNG", "Electric"):
        fuel_type = "Diesel"
    bus_type = str(payload.get("busType", "seater")).strip().lower()
    if bus_type not in ("seater", "sleeper"):
        bus_type = "seater"
    try:
        total_km = float(payload.get("totalKm") or 0)
    except (TypeError, ValueError):
        total_km = 0

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                ensure_travel_schema(cur)
                cur.execute(
                    """
                    INSERT INTO travel_vehicles
                        (owner_id, reg_number, model, bus_type, fuel_type, reg_date, total_km,
                         insurance_expiry, fitness_expiry, puc_expiry)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (owner_id, reg_number, str(payload.get("model", "")).strip(), bus_type, fuel_type,
                     _parse_optional_date(payload.get("regDate")), total_km,
                     _parse_optional_date(payload.get("insuranceExpiry")),
                     _parse_optional_date(payload.get("fitnessExpiry")),
                     _parse_optional_date(payload.get("pucExpiry"))),
                )
                vid = cur.fetchone()[0]
                cur.execute(f"{VEHICLE_SELECT} WHERE id = %s", (vid,))
                vehicle = serialize_vehicle(cur.fetchone())
                conn.commit()
        return jsonify({"vehicle": vehicle}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.put("/vehicles/<int:vehicle_id>")
def update_vehicle(vehicle_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    fields, values = [], []
    if "regNumber" in payload:
        reg_number = str(payload.get("regNumber", "")).strip()
        if not reg_number:
            return jsonify({"error": "regNumber cannot be empty"}), 400
        fields.append("reg_number = %s")
        values.append(reg_number)
    if "model" in payload:
        fields.append("model = %s")
        values.append(str(payload.get("model", "")).strip())
    if "busType" in payload:
        bus_type = str(payload.get("busType", "")).strip().lower()
        if bus_type not in ("seater", "sleeper"):
            return jsonify({"error": "busType must be 'seater' or 'sleeper'"}), 400
        fields.append("bus_type = %s")
        values.append(bus_type)
    if "fuelType" in payload:
        fuel_type = str(payload.get("fuelType", "")).strip()
        if fuel_type not in ("Diesel", "Petrol", "CNG", "Electric"):
            return jsonify({"error": "fuelType must be Diesel, Petrol, CNG or Electric"}), 400
        fields.append("fuel_type = %s")
        values.append(fuel_type)
    if "regDate" in payload:
        fields.append("reg_date = %s")
        values.append(_parse_optional_date(payload.get("regDate")))
    if "totalKm" in payload:
        try:
            fields.append("total_km = %s")
            values.append(float(payload.get("totalKm") or 0))
        except (TypeError, ValueError):
            return jsonify({"error": "totalKm must be a number"}), 400
    if "insuranceExpiry" in payload:
        fields.append("insurance_expiry = %s")
        values.append(_parse_optional_date(payload.get("insuranceExpiry")))
    if "fitnessExpiry" in payload:
        fields.append("fitness_expiry = %s")
        values.append(_parse_optional_date(payload.get("fitnessExpiry")))
    if "pucExpiry" in payload:
        fields.append("puc_expiry = %s")
        values.append(_parse_optional_date(payload.get("pucExpiry")))
    if "status" in payload:
        status = str(payload.get("status", "")).strip()
        if status not in ("active", "inactive"):
            return jsonify({"error": "status must be 'active' or 'inactive'"}), 400
        fields.append("status = %s")
        values.append(status)
    if not fields:
        return jsonify({"error": "No fields to update"}), 400
    fields.append("updated_at = NOW()")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                ensure_travel_schema(cur)
                cur.execute(
                    f"UPDATE travel_vehicles SET {', '.join(fields)} WHERE id = %s AND owner_id = %s RETURNING id",
                    values + [vehicle_id, owner_id],
                )
                if not cur.fetchone():
                    return jsonify({"error": "Vehicle not found"}), 404
                cur.execute(f"{VEHICLE_SELECT} WHERE id = %s", (vehicle_id,))
                vehicle = serialize_vehicle(cur.fetchone())
                conn.commit()
        return jsonify({"vehicle": vehicle})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@travel_bp.delete("/vehicles/<int:vehicle_id>")
def delete_vehicle(vehicle_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                ensure_travel_schema(cur)
                cur.execute(
                    "DELETE FROM travel_vehicles WHERE id = %s AND owner_id = %s RETURNING id",
                    (vehicle_id, owner_id),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Vehicle not found"}), 404
                conn.commit()
        return jsonify({"message": "Vehicle deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

