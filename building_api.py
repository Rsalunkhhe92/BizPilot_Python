"""
Building Maintenance REST API Endpoints (Flask Blueprint)
Mounted at /api/building

Phase 1: Building Setup -> Floors -> Flats -> Building Members.

Security: the authenticated Business Saathi user (the Building Manager) is
identified by owner_id (matching the pattern used by inventory_api.py /
collection_api.py - this app authenticates the mobile session and passes the
logged-in user's id, not a bearer token). Every floor/flat/member endpoint
re-verifies that the parent building belongs to the requesting owner_id via a
SQL join before reading or writing anything, so one manager can never reach
another manager's building by guessing an id.
"""

import calendar
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import os

from flask import Blueprint, jsonify, request
import psycopg

from building_service import generate_maintenance_receipt_number, recompute_bill_status

building_bp = Blueprint("building", __name__, url_prefix="/api/building")


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(database_url, options="-c timezone=Asia/Kolkata")


def get_owner_id(payload=None):
    """Resolve the logged-in Building Manager's userdetails.id from query/body/header."""
    candidates = [
        request.args.get("owner_id"),
        request.args.get("ownerId"),
        (payload or {}).get("ownerId") if payload else None,
        (payload or {}).get("owner_id") if payload else None,
        request.headers.get("X-User-Id"),
    ]
    for c in candidates:
        if c not in (None, ""):
            try:
                return int(c)
            except (TypeError, ValueError):
                continue
    return None


def to_decimal(value, default="0"):
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal(default)


def to_int(value, default=None):
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_owned_building_id(cur, building_id, owner_id):
    """Returns building_id if it exists and belongs to owner_id, else None."""
    cur.execute("SELECT id FROM buildings WHERE id = %s AND owner_id = %s", (building_id, owner_id))
    row = cur.fetchone()
    return row[0] if row else None


def serialize_building(row):
    (bid, name, code, address, area, city, state, pincode, num_floors, num_flats,
     building_type, construction_year, contact_number, emergency_contact, photo_url,
     description, notes, status, created_at, updated_at) = row
    return {
        "id": str(bid),
        "name": name,
        "code": code or "",
        "address": address or "",
        "area": area or "",
        "city": city or "",
        "state": state or "",
        "pincode": pincode or "",
        "numFloors": num_floors,
        "numFlats": num_flats,
        "buildingType": building_type,
        "constructionYear": construction_year,
        "contactNumber": contact_number or "",
        "emergencyContact": emergency_contact or "",
        "photoUrl": photo_url or "",
        "description": description or "",
        "notes": notes or "",
        "status": status,
    }


def serialize_floor(row):
    (fid, building_id, floor_number, floor_name, num_flats, description, status, created_at, updated_at) = row
    return {
        "id": str(fid),
        "buildingId": str(building_id),
        "floorNumber": floor_number,
        "floorName": floor_name or "",
        "numFlats": num_flats,
        "description": description or "",
        "status": status,
    }


def serialize_flat(row):
    (fid, building_id, floor_id, flat_number, flat_type, area, occupancy_status, owner_name,
     tenant_name, primary_mobile, parking_slot, maintenance_amount, notes, created_at, updated_at) = row
    return {
        "id": str(fid),
        "buildingId": str(building_id),
        "floorId": str(floor_id) if floor_id is not None else None,
        "flatNumber": flat_number,
        "flatType": flat_type or "",
        "area": float(area) if area is not None else None,
        "occupancyStatus": occupancy_status,
        "ownerName": owner_name or "",
        "tenantName": tenant_name or "",
        "primaryMobile": primary_mobile or "",
        "parkingSlot": parking_slot or "",
        "maintenanceAmount": float(maintenance_amount or 0),
        "notes": notes or "",
    }


def serialize_member(row):
    (mid, building_id, flat_id, flat_number, full_name, mobile_number, email, member_type,
     emergency_contact, vehicle_number, status, notes, created_at, updated_at) = row
    return {
        "id": str(mid),
        "buildingId": str(building_id),
        "flatId": str(flat_id) if flat_id is not None else None,
        "flatNumber": flat_number,
        "fullName": full_name,
        "mobileNumber": mobile_number or "",
        "email": email or "",
        "memberType": member_type,
        "emergencyContact": emergency_contact or "",
        "vehicleNumber": vehicle_number or "",
        "status": status,
        "notes": notes or "",
    }


# ---------------------------------------------------------------------------
# Buildings
# ---------------------------------------------------------------------------

@building_bp.get("/buildings")
def list_buildings():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, code, address, area, city, state, pincode, num_floors, num_flats,
                           building_type, construction_year, contact_number, emergency_contact, photo_url,
                           description, notes, status, created_at, updated_at
                    FROM buildings WHERE owner_id = %s AND status = 'active' ORDER BY created_at ASC
                    """,
                    (owner_id,),
                )
                buildings = [serialize_building(row) for row in cur.fetchall()]
        return jsonify({"buildings": buildings})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.post("/buildings")
def create_building():
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    name = str(payload.get("name", "")).strip()
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not name:
        return jsonify({"error": "Building name is required"}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Subscription limit: default tier allows 1 building per manager.
                # TODO(Phase 7): read the actual max-buildings limit from the subscription plan config.
                cur.execute("SELECT COUNT(*) FROM buildings WHERE owner_id = %s AND status = 'active'", (owner_id,))
                existing_count = cur.fetchone()[0]
                if existing_count >= 1:
                    return jsonify({"error": "Your plan allows only 1 building. Upgrade to add more."}), 403

                cur.execute(
                    """
                    INSERT INTO buildings
                        (owner_id, name, code, address, area, city, state, pincode, num_floors, num_flats,
                         building_type, construction_year, contact_number, emergency_contact, photo_url,
                         description, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, name, code, address, area, city, state, pincode, num_floors, num_flats,
                              building_type, construction_year, contact_number, emergency_contact, photo_url,
                              description, notes, status, created_at, updated_at
                    """,
                    (
                        owner_id,
                        name,
                        str(payload.get("code", "")).strip(),
                        str(payload.get("address", "")).strip(),
                        str(payload.get("area", "")).strip(),
                        str(payload.get("city", "")).strip(),
                        str(payload.get("state", "")).strip(),
                        str(payload.get("pincode", "")).strip(),
                        to_int(payload.get("numFloors"), 0),
                        to_int(payload.get("numFlats"), 0),
                        str(payload.get("buildingType", "Residential")).strip() or "Residential",
                        to_int(payload.get("constructionYear")),
                        str(payload.get("contactNumber", "")).strip(),
                        str(payload.get("emergencyContact", "")).strip(),
                        str(payload.get("photoUrl", "")).strip(),
                        str(payload.get("description", "")).strip(),
                        str(payload.get("notes", "")).strip(),
                    ),
                )
                building = serialize_building(cur.fetchone())
                conn.commit()
        return jsonify({"building": building}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.get("/buildings/<int:building_id>")
def get_building(building_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, code, address, area, city, state, pincode, num_floors, num_flats,
                           building_type, construction_year, contact_number, emergency_contact, photo_url,
                           description, notes, status, created_at, updated_at
                    FROM buildings WHERE id = %s AND owner_id = %s
                    """,
                    (building_id, owner_id),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Building not found"}), 404
        return jsonify({"building": serialize_building(row)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.put("/buildings/<int:building_id>")
def update_building(building_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    field_map = {
        "name": "name", "code": "code", "address": "address", "area": "area", "city": "city",
        "state": "state", "pincode": "pincode", "buildingType": "building_type",
        "contactNumber": "contact_number", "emergencyContact": "emergency_contact",
        "photoUrl": "photo_url", "description": "description", "notes": "notes",
    }
    int_field_map = {"numFloors": "num_floors", "numFlats": "num_flats", "constructionYear": "construction_year"}

    fields, values = [], []
    for k, col in field_map.items():
        if k in payload:
            fields.append(f"{col} = %s")
            values.append(str(payload.get(k, "")).strip())
    for k, col in int_field_map.items():
        if k in payload:
            fields.append(f"{col} = %s")
            values.append(to_int(payload.get(k)))

    if not fields:
        return jsonify({"error": "No fields to update"}), 400

    fields.append("updated_at = NOW()")
    values.extend([building_id, owner_id])

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE buildings SET {', '.join(fields)}
                    WHERE id = %s AND owner_id = %s
                    RETURNING id, name, code, address, area, city, state, pincode, num_floors, num_flats,
                              building_type, construction_year, contact_number, emergency_contact, photo_url,
                              description, notes, status, created_at, updated_at
                    """,
                    values,
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Building not found"}), 404
                building = serialize_building(row)
                conn.commit()
        return jsonify({"building": building})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Floors
# ---------------------------------------------------------------------------

@building_bp.get("/buildings/<int:building_id>/floors")
def list_floors(building_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                cur.execute(
                    """
                    SELECT id, building_id, floor_number, floor_name, num_flats, description, status,
                           created_at, updated_at
                    FROM building_floors WHERE building_id = %s AND status = 'active'
                    ORDER BY floor_number ASC
                    """,
                    (building_id,),
                )
                floors = [serialize_floor(row) for row in cur.fetchall()]
        return jsonify({"floors": floors})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.post("/buildings/<int:building_id>/floors")
def create_floor(building_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    floor_number = to_int(payload.get("floorNumber"))
    if floor_number is None:
        return jsonify({"error": "floorNumber is required"}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                cur.execute(
                    """
                    INSERT INTO building_floors (building_id, floor_number, floor_name, num_flats, description)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (building_id, floor_number) DO UPDATE SET
                        floor_name = EXCLUDED.floor_name,
                        num_flats = EXCLUDED.num_flats,
                        description = EXCLUDED.description,
                        updated_at = NOW()
                    RETURNING id, building_id, floor_number, floor_name, num_flats, description, status,
                              created_at, updated_at
                    """,
                    (
                        building_id,
                        floor_number,
                        str(payload.get("floorName", "")).strip(),
                        to_int(payload.get("numFlats"), 0),
                        str(payload.get("description", "")).strip(),
                    ),
                )
                floor = serialize_floor(cur.fetchone())
                conn.commit()
        return jsonify({"floor": floor}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.put("/floors/<int:floor_id>")
def update_floor(floor_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    fields, values = [], []
    if "floorName" in payload:
        fields.append("floor_name = %s")
        values.append(str(payload.get("floorName", "")).strip())
    if "numFlats" in payload:
        fields.append("num_flats = %s")
        values.append(to_int(payload.get("numFlats"), 0))
    if "description" in payload:
        fields.append("description = %s")
        values.append(str(payload.get("description", "")).strip())
    if not fields:
        return jsonify({"error": "No fields to update"}), 400
    fields.append("updated_at = NOW()")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE building_floors f SET {', '.join(fields)}
                    FROM buildings b
                    WHERE f.id = %s AND f.building_id = b.id AND b.owner_id = %s
                    RETURNING f.id, f.building_id, f.floor_number, f.floor_name, f.num_flats, f.description,
                              f.status, f.created_at, f.updated_at
                    """,
                    values + [floor_id, owner_id],
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Floor not found"}), 404
                floor = serialize_floor(row)
                conn.commit()
        return jsonify({"floor": floor})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.delete("/floors/<int:floor_id>")
def delete_floor(floor_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE building_floors f SET status = 'inactive', updated_at = NOW()
                    FROM buildings b
                    WHERE f.id = %s AND f.building_id = b.id AND b.owner_id = %s
                    RETURNING f.id
                    """,
                    (floor_id, owner_id),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Floor not found"}), 404
                conn.commit()
        return jsonify({"message": "Floor removed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Flats
# ---------------------------------------------------------------------------

@building_bp.get("/buildings/<int:building_id>/flats")
def list_flats(building_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    floor_id = request.args.get("floor_id")
    occupancy_status = request.args.get("occupancy_status")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                clauses = ["building_id = %s"]
                params = [building_id]
                if floor_id:
                    clauses.append("floor_id = %s")
                    params.append(floor_id)
                if occupancy_status:
                    clauses.append("occupancy_status = %s")
                    params.append(occupancy_status)
                cur.execute(
                    f"""
                    SELECT id, building_id, floor_id, flat_number, flat_type, area, occupancy_status,
                           owner_name, tenant_name, primary_mobile, parking_slot, maintenance_amount, notes,
                           created_at, updated_at
                    FROM building_flats
                    WHERE {' AND '.join(clauses)}
                    ORDER BY flat_number ASC
                    """,
                    params,
                )
                flats = [serialize_flat(row) for row in cur.fetchall()]
        return jsonify({"flats": flats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.post("/buildings/<int:building_id>/flats")
def create_flat(building_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    flat_number = str(payload.get("flatNumber", "")).strip()
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not flat_number:
        return jsonify({"error": "flatNumber is required"}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                floor_id = payload.get("floorId")
                cur.execute(
                    """
                    INSERT INTO building_flats
                        (building_id, floor_id, flat_number, flat_type, area, occupancy_status,
                         owner_name, tenant_name, primary_mobile, parking_slot, maintenance_amount, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, building_id, floor_id, flat_number, flat_type, area, occupancy_status,
                              owner_name, tenant_name, primary_mobile, parking_slot, maintenance_amount, notes,
                              created_at, updated_at
                    """,
                    (
                        building_id,
                        floor_id,
                        flat_number,
                        str(payload.get("flatType", "")).strip(),
                        to_decimal(payload.get("area")) if payload.get("area") not in (None, "") else None,
                        str(payload.get("occupancyStatus", "Vacant")).strip() or "Vacant",
                        str(payload.get("ownerName", "")).strip(),
                        str(payload.get("tenantName", "")).strip(),
                        str(payload.get("primaryMobile", "")).strip(),
                        str(payload.get("parkingSlot", "")).strip(),
                        to_decimal(payload.get("maintenanceAmount")),
                        str(payload.get("notes", "")).strip(),
                    ),
                )
                flat = serialize_flat(cur.fetchone())
                conn.commit()
        return jsonify({"flat": flat}), 201
    except psycopg.errors.UniqueViolation:
        return jsonify({"error": f'Flat "{flat_number}" already exists in this building.'}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.put("/flats/<int:flat_id>")
def update_flat(flat_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    text_fields = {
        "flatNumber": "flat_number", "flatType": "flat_type", "occupancyStatus": "occupancy_status",
        "ownerName": "owner_name", "tenantName": "tenant_name", "primaryMobile": "primary_mobile",
        "parkingSlot": "parking_slot", "notes": "notes",
    }
    fields, values = [], []
    for k, col in text_fields.items():
        if k in payload:
            fields.append(f"{col} = %s")
            values.append(str(payload.get(k, "")).strip())
    if "floorId" in payload:
        fields.append("floor_id = %s")
        values.append(payload.get("floorId"))
    if "area" in payload:
        fields.append("area = %s")
        values.append(to_decimal(payload.get("area")) if payload.get("area") not in (None, "") else None)
    if "maintenanceAmount" in payload:
        fields.append("maintenance_amount = %s")
        values.append(to_decimal(payload.get("maintenanceAmount")))
    if not fields:
        return jsonify({"error": "No fields to update"}), 400
    fields.append("updated_at = NOW()")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE building_flats fl SET {', '.join(fields)}
                    FROM buildings b
                    WHERE fl.id = %s AND fl.building_id = b.id AND b.owner_id = %s
                    RETURNING fl.id, fl.building_id, fl.floor_id, fl.flat_number, fl.flat_type, fl.area,
                              fl.occupancy_status, fl.owner_name, fl.tenant_name, fl.primary_mobile,
                              fl.parking_slot, fl.maintenance_amount, fl.notes, fl.created_at, fl.updated_at
                    """,
                    values + [flat_id, owner_id],
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Flat not found"}), 404
                flat = serialize_flat(row)
                conn.commit()
        return jsonify({"flat": flat})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.delete("/flats/<int:flat_id>")
def delete_flat(flat_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM building_flats fl
                    USING buildings b
                    WHERE fl.id = %s AND fl.building_id = b.id AND b.owner_id = %s
                    RETURNING fl.id
                    """,
                    (flat_id, owner_id),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Flat not found"}), 404
                conn.commit()
        return jsonify({"message": "Flat deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Building Members (records only - NOT application users)
# ---------------------------------------------------------------------------

@building_bp.get("/buildings/<int:building_id>/members")
def list_members(building_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    search = request.args.get("search", "").strip()
    flat_id = request.args.get("flat_id")
    member_type = request.args.get("member_type")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                clauses = ["m.building_id = %s", "m.status = 'active'"]
                params = [building_id]
                if search:
                    clauses.append("(m.full_name ILIKE %s OR m.mobile_number ILIKE %s)")
                    params.extend([f"%{search}%", f"%{search}%"])
                if flat_id:
                    clauses.append("m.flat_id = %s")
                    params.append(flat_id)
                if member_type:
                    clauses.append("m.member_type = %s")
                    params.append(member_type)
                cur.execute(
                    f"""
                    SELECT m.id, m.building_id, m.flat_id, fl.flat_number, m.full_name, m.mobile_number,
                           m.email, m.member_type, m.emergency_contact, m.vehicle_number, m.status, m.notes,
                           m.created_at, m.updated_at
                    FROM building_members m
                    LEFT JOIN building_flats fl ON fl.id = m.flat_id
                    WHERE {' AND '.join(clauses)}
                    ORDER BY m.full_name ASC
                    """,
                    params,
                )
                members = [serialize_member(row) for row in cur.fetchall()]
        return jsonify({"members": members})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.post("/buildings/<int:building_id>/members")
def create_member(building_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    full_name = str(payload.get("fullName", "")).strip()
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not full_name:
        return jsonify({"error": "fullName is required"}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                cur.execute(
                    """
                    INSERT INTO building_members
                        (building_id, flat_id, full_name, mobile_number, email, member_type,
                         emergency_contact, vehicle_number, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, building_id, flat_id, full_name, mobile_number, email, member_type,
                              emergency_contact, vehicle_number, status, notes, created_at, updated_at
                    """,
                    (
                        building_id,
                        payload.get("flatId"),
                        full_name,
                        str(payload.get("mobileNumber", "")).strip(),
                        str(payload.get("email", "")).strip(),
                        str(payload.get("memberType", "Owner")).strip() or "Owner",
                        str(payload.get("emergencyContact", "")).strip(),
                        str(payload.get("vehicleNumber", "")).strip(),
                        str(payload.get("notes", "")).strip(),
                    ),
                )
                row = cur.fetchone()
                conn.commit()
        member = serialize_member(row[:3] + (None,) + row[3:])
        return jsonify({"member": member}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.put("/members/<int:member_id>")
def update_member(member_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    text_fields = {
        "fullName": "full_name", "mobileNumber": "mobile_number", "email": "email",
        "memberType": "member_type", "emergencyContact": "emergency_contact",
        "vehicleNumber": "vehicle_number", "notes": "notes",
    }
    fields, values = [], []
    for k, col in text_fields.items():
        if k in payload:
            fields.append(f"{col} = %s")
            values.append(str(payload.get(k, "")).strip())
    if "flatId" in payload:
        fields.append("flat_id = %s")
        values.append(payload.get("flatId"))
    if not fields:
        return jsonify({"error": "No fields to update"}), 400
    fields.append("updated_at = NOW()")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE building_members m SET {', '.join(fields)}
                    FROM buildings b
                    WHERE m.id = %s AND m.building_id = b.id AND b.owner_id = %s
                    RETURNING m.id, m.building_id, m.flat_id, m.full_name, m.mobile_number, m.email,
                              m.member_type, m.emergency_contact, m.vehicle_number, m.status, m.notes,
                              m.created_at, m.updated_at
                    """,
                    values + [member_id, owner_id],
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Member not found"}), 404
                member = serialize_member(row[:3] + (None,) + row[3:])
                conn.commit()
        return jsonify({"member": member})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.delete("/members/<int:member_id>")
def delete_member(member_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE building_members m SET status = 'inactive', updated_at = NOW()
                    FROM buildings b
                    WHERE m.id = %s AND m.building_id = b.id AND b.owner_id = %s
                    RETURNING m.id
                    """,
                    (member_id, owner_id),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Member not found"}), 404
                conn.commit()
        return jsonify({"message": "Member removed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Phase 2: Maintenance Config -> Bills -> Payments -> Receipts
# ---------------------------------------------------------------------------

CHARGE_FIELDS = [
    ("waterCharges", "water_charges"),
    ("parkingCharges", "parking_charges"),
    ("commonElectricity", "common_electricity"),
    ("securityCharges", "security_charges"),
    ("cleaningCharges", "cleaning_charges"),
    ("liftCharges", "lift_charges"),
    ("otherCharges", "other_charges"),
    ("latePaymentCharges", "late_payment_charges"),
    ("discount", "discount"),
]


def serialize_config(row):
    (cid, building_id, water, parking, electricity, security, cleaning, lift, other,
     late_fee, discount, due_day, created_at, updated_at) = row
    return {
        "id": str(cid),
        "buildingId": str(building_id),
        "waterCharges": float(water or 0),
        "parkingCharges": float(parking or 0),
        "commonElectricity": float(electricity or 0),
        "securityCharges": float(security or 0),
        "cleaningCharges": float(cleaning or 0),
        "liftCharges": float(lift or 0),
        "otherCharges": float(other or 0),
        "latePaymentCharges": float(late_fee or 0),
        "discount": float(discount or 0),
        "dueDayOfMonth": due_day,
    }


def serialize_bill(row):
    (bid, building_id, flat_id, flat_number, billing_month, base, water, parking, electricity,
     security, cleaning, lift, other, late_fee, discount, prev_outstanding, total_amount,
     paid_amount, status, due_date, notes, created_at, updated_at) = row
    is_overdue = status in ("Pending", "Partially Paid") and due_date and due_date < date.today()
    return {
        "id": str(bid),
        "buildingId": str(building_id),
        "flatId": str(flat_id),
        "flatNumber": flat_number,
        "billingMonth": billing_month.isoformat() if billing_month else None,
        "baseMaintenance": float(base or 0),
        "waterCharges": float(water or 0),
        "parkingCharges": float(parking or 0),
        "commonElectricity": float(electricity or 0),
        "securityCharges": float(security or 0),
        "cleaningCharges": float(cleaning or 0),
        "liftCharges": float(lift or 0),
        "otherCharges": float(other or 0),
        "latePaymentCharges": float(late_fee or 0),
        "discount": float(discount or 0),
        "previousOutstanding": float(prev_outstanding or 0),
        "totalAmount": float(total_amount or 0),
        "paidAmount": float(paid_amount or 0),
        "balanceAmount": float((total_amount or 0) - (paid_amount or 0)),
        "status": "Overdue" if is_overdue else status,
        "dueDate": due_date.isoformat() if due_date else None,
        "notes": notes or "",
    }


def serialize_payment(row):
    (pid, building_id, flat_id, flat_number, member_id, member_name, bill_id, amount,
     payment_date, payment_method, transaction_reference, notes, receipt_number,
     created_at, updated_at) = row
    return {
        "id": str(pid),
        "buildingId": str(building_id),
        "flatId": str(flat_id),
        "flatNumber": flat_number,
        "memberId": str(member_id) if member_id is not None else None,
        "memberName": member_name,
        "billId": str(bill_id),
        "amount": float(amount or 0),
        "paymentDate": payment_date.isoformat() if payment_date else None,
        "paymentMethod": payment_method,
        "transactionReference": transaction_reference or "",
        "notes": notes or "",
        "receiptNumber": receipt_number,
    }


def _month_start(value):
    """Parses 'YYYY-MM' or 'YYYY-MM-DD' into the first-of-month date."""
    s = str(value).strip()
    if len(s) == 7:
        return date.fromisoformat(f"{s}-01")
    d = date.fromisoformat(s)
    return d.replace(day=1)


BILL_SELECT = """
    SELECT b.id, b.building_id, b.flat_id, fl.flat_number, b.billing_month, b.base_maintenance,
           b.water_charges, b.parking_charges, b.common_electricity, b.security_charges,
           b.cleaning_charges, b.lift_charges, b.other_charges, b.late_payment_charges,
           b.discount, b.previous_outstanding, b.total_amount, b.paid_amount, b.status,
           b.due_date, b.notes, b.created_at, b.updated_at
    FROM maintenance_bills b
    JOIN building_flats fl ON fl.id = b.flat_id
"""

PAYMENT_SELECT = """
    SELECT p.id, p.building_id, p.flat_id, fl.flat_number, p.member_id, m.full_name, p.bill_id,
           p.amount, p.payment_date, p.payment_method, p.transaction_reference, p.notes,
           p.receipt_number, p.created_at, p.updated_at
    FROM maintenance_payments p
    JOIN building_flats fl ON fl.id = p.flat_id
    LEFT JOIN building_members m ON m.id = p.member_id
"""


# --- Maintenance Config ---

@building_bp.get("/buildings/<int:building_id>/maintenance-config")
def get_maintenance_config(building_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                cur.execute(
                    """
                    SELECT id, building_id, water_charges, parking_charges, common_electricity,
                           security_charges, cleaning_charges, lift_charges, other_charges,
                           late_payment_charges, discount, due_day_of_month, created_at, updated_at
                    FROM building_maintenance_config WHERE building_id = %s
                    """,
                    (building_id,),
                )
                row = cur.fetchone()
        if not row:
            return jsonify({"config": None})
        return jsonify({"config": serialize_config(row)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.put("/buildings/<int:building_id>/maintenance-config")
def upsert_maintenance_config(building_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    values = [to_decimal(payload.get(k)) for k, _ in CHARGE_FIELDS]
    due_day = to_int(payload.get("dueDayOfMonth"), 10)

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                cur.execute(
                    """
                    INSERT INTO building_maintenance_config
                        (building_id, water_charges, parking_charges, common_electricity,
                         security_charges, cleaning_charges, lift_charges, other_charges,
                         late_payment_charges, discount, due_day_of_month)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (building_id) DO UPDATE SET
                        water_charges = EXCLUDED.water_charges,
                        parking_charges = EXCLUDED.parking_charges,
                        common_electricity = EXCLUDED.common_electricity,
                        security_charges = EXCLUDED.security_charges,
                        cleaning_charges = EXCLUDED.cleaning_charges,
                        lift_charges = EXCLUDED.lift_charges,
                        other_charges = EXCLUDED.other_charges,
                        late_payment_charges = EXCLUDED.late_payment_charges,
                        discount = EXCLUDED.discount,
                        due_day_of_month = EXCLUDED.due_day_of_month,
                        updated_at = NOW()
                    RETURNING id, building_id, water_charges, parking_charges, common_electricity,
                              security_charges, cleaning_charges, lift_charges, other_charges,
                              late_payment_charges, discount, due_day_of_month, created_at, updated_at
                    """,
                    (building_id, *values, due_day),
                )
                config = serialize_config(cur.fetchone())
                conn.commit()
        return jsonify({"config": config})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Maintenance Bills ---

@building_bp.post("/buildings/<int:building_id>/bills/generate")
def generate_bills(building_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not payload.get("billingMonth"):
        return jsonify({"error": "billingMonth is required (YYYY-MM)"}), 400

    try:
        billing_month = _month_start(payload.get("billingMonth"))
    except ValueError:
        return jsonify({"error": "billingMonth must be YYYY-MM"}), 400

    flat_ids = payload.get("flatIds") or []
    entire_building = bool(payload.get("entireBuilding"))

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404

                cur.execute(
                    """
                    SELECT water_charges, parking_charges, common_electricity, security_charges,
                           cleaning_charges, lift_charges, other_charges, late_payment_charges,
                           discount, due_day_of_month
                    FROM building_maintenance_config WHERE building_id = %s
                    """,
                    (building_id,),
                )
                cfg = cur.fetchone()
                (water, parking, electricity, security, cleaning, lift, other, late_fee,
                 discount, due_day) = cfg if cfg else (0, 0, 0, 0, 0, 0, 0, 0, 0, 10)

                if entire_building:
                    cur.execute(
                        "SELECT id, maintenance_amount FROM building_flats WHERE building_id = %s",
                        (building_id,),
                    )
                else:
                    if not flat_ids:
                        return jsonify({"error": "flatIds or entireBuilding is required"}), 400
                    cur.execute(
                        "SELECT id, maintenance_amount FROM building_flats WHERE building_id = %s AND id = ANY(%s)",
                        (building_id, flat_ids),
                    )
                target_flats = cur.fetchall()

                try:
                    last_day = calendar.monthrange(billing_month.year, billing_month.month)[1]
                    due_date = billing_month.replace(day=min(due_day, last_day))
                except Exception:
                    due_date = billing_month + timedelta(days=9)

                created, skipped = [], 0
                for flat_id, base_maintenance in target_flats:
                    cur.execute(
                        """
                        SELECT COALESCE(SUM(total_amount - paid_amount), 0)
                        FROM maintenance_bills
                        WHERE flat_id = %s AND status NOT IN ('Paid', 'Cancelled') AND billing_month < %s
                        """,
                        (flat_id, billing_month),
                    )
                    previous_outstanding = cur.fetchone()[0] or 0
                    total_amount = (
                        (base_maintenance or 0) + water + parking + electricity + security +
                        cleaning + lift + other + previous_outstanding + late_fee - discount
                    )
                    cur.execute(
                        """
                        INSERT INTO maintenance_bills
                            (building_id, flat_id, billing_month, base_maintenance, water_charges,
                             parking_charges, common_electricity, security_charges, cleaning_charges,
                             lift_charges, other_charges, late_payment_charges, discount,
                             previous_outstanding, total_amount, due_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (flat_id, billing_month) DO NOTHING
                        RETURNING id
                        """,
                        (
                            building_id, flat_id, billing_month, base_maintenance, water, parking,
                            electricity, security, cleaning, lift, other, late_fee, discount,
                            previous_outstanding, total_amount, due_date,
                        ),
                    )
                    row = cur.fetchone()
                    if row:
                        created.append(row[0])
                    else:
                        skipped += 1
                conn.commit()
        return jsonify({"created": len(created), "skipped": skipped}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.get("/buildings/<int:building_id>/bills")
def list_bills(building_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    month = request.args.get("month")
    flat_id = request.args.get("flat_id")
    status = request.args.get("status")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                clauses = ["b.building_id = %s"]
                params = [building_id]
                if month:
                    clauses.append("b.billing_month = %s")
                    params.append(_month_start(month))
                if flat_id:
                    clauses.append("b.flat_id = %s")
                    params.append(flat_id)
                if status:
                    clauses.append("b.status = %s")
                    params.append(status)
                cur.execute(
                    f"{BILL_SELECT} WHERE {' AND '.join(clauses)} ORDER BY b.billing_month DESC, fl.flat_number ASC",
                    params,
                )
                bills = [serialize_bill(row) for row in cur.fetchall()]
        summary = {
            "totalBilled": sum(b["totalAmount"] for b in bills),
            "totalCollected": sum(b["paidAmount"] for b in bills),
            "totalPending": sum(b["balanceAmount"] for b in bills if b["status"] != "Cancelled"),
        }
        return jsonify({"bills": bills, "summary": summary})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.get("/bills/<int:bill_id>")
def get_bill(bill_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"{BILL_SELECT} JOIN buildings bd ON bd.id = b.building_id "
                    "WHERE b.id = %s AND bd.owner_id = %s",
                    (bill_id, owner_id),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Bill not found"}), 404
        return jsonify({"bill": serialize_bill(row)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.put("/bills/<int:bill_id>")
def update_bill(bill_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    editable = {
        "baseMaintenance": "base_maintenance", "waterCharges": "water_charges",
        "parkingCharges": "parking_charges", "commonElectricity": "common_electricity",
        "securityCharges": "security_charges", "cleaningCharges": "cleaning_charges",
        "liftCharges": "lift_charges", "otherCharges": "other_charges",
        "latePaymentCharges": "late_payment_charges", "discount": "discount",
        "previousOutstanding": "previous_outstanding", "notes": "notes",
    }
    fields, values = [], []
    for k, col in editable.items():
        if k in payload:
            fields.append(f"{col} = %s")
            values.append(str(payload.get(k, "")).strip() if k == "notes" else to_decimal(payload.get(k)))
    if payload.get("status") == "Cancelled":
        fields.append("status = 'Cancelled'")
    if not fields:
        return jsonify({"error": "No fields to update"}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT b.id FROM maintenance_bills b
                    JOIN buildings bd ON bd.id = b.building_id
                    WHERE b.id = %s AND bd.owner_id = %s
                    """,
                    (bill_id, owner_id),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Bill not found"}), 404

                # Recompute total_amount from the (possibly updated) charge fields.
                fields_sql = ', '.join(fields) + ", updated_at = NOW()"
                cur.execute(f"UPDATE maintenance_bills SET {fields_sql} WHERE id = %s", values + [bill_id])
                cur.execute(
                    """
                    UPDATE maintenance_bills SET total_amount = (
                        base_maintenance + water_charges + parking_charges + common_electricity +
                        security_charges + cleaning_charges + lift_charges + other_charges +
                        late_payment_charges + previous_outstanding - discount
                    ), updated_at = NOW()
                    WHERE id = %s
                    """,
                    (bill_id,),
                )
                recompute_bill_status(cur, bill_id)
                cur.execute(f"{BILL_SELECT} WHERE b.id = %s", (bill_id,))
                bill = serialize_bill(cur.fetchone())
                conn.commit()
        return jsonify({"bill": bill})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Maintenance Payments & Receipts ---

@building_bp.post("/bills/<int:bill_id>/payments")
def create_payment(bill_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    amount = to_decimal(payload.get("amount"))
    if amount <= 0:
        return jsonify({"error": "amount must be greater than 0"}), 400

    payment_method = str(payload.get("paymentMethod", "Cash")).strip() or "Cash"
    if payment_method not in ("Cash", "UPI", "Bank Transfer", "Card", "Other"):
        payment_method = "Other"
    payment_date = payload.get("paymentDate")
    try:
        payment_date = date.fromisoformat(payment_date) if payment_date else date.today()
    except ValueError:
        payment_date = date.today()

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT b.building_id, b.flat_id FROM maintenance_bills b
                    JOIN buildings bd ON bd.id = b.building_id
                    WHERE b.id = %s AND bd.owner_id = %s
                    """,
                    (bill_id, owner_id),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Bill not found"}), 404
                building_id, flat_id = row

                member_id = payload.get("memberId")
                if not member_id:
                    cur.execute(
                        "SELECT id FROM building_members WHERE flat_id = %s AND status = 'active' "
                        "ORDER BY (member_type = 'Owner') DESC LIMIT 1",
                        (flat_id,),
                    )
                    m = cur.fetchone()
                    member_id = m[0] if m else None

                receipt_number = generate_maintenance_receipt_number(cur, payment_date)
                cur.execute(
                    """
                    INSERT INTO maintenance_payments
                        (building_id, flat_id, member_id, bill_id, amount, payment_date,
                         payment_method, transaction_reference, notes, receipt_number)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        building_id, flat_id, member_id, bill_id, amount, payment_date,
                        payment_method, str(payload.get("transactionReference", "")).strip(),
                        str(payload.get("notes", "")).strip(), receipt_number,
                    ),
                )
                payment_id = cur.fetchone()[0]
                recompute_bill_status(cur, bill_id)
                cur.execute(f"{PAYMENT_SELECT} WHERE p.id = %s", (payment_id,))
                payment = serialize_payment(cur.fetchone())
                cur.execute(f"{BILL_SELECT} WHERE b.id = %s", (bill_id,))
                bill = serialize_bill(cur.fetchone())
                conn.commit()
        return jsonify({"payment": payment, "bill": bill}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.get("/buildings/<int:building_id>/payments")
def list_payments(building_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    flat_id = request.args.get("flat_id")
    payment_method = request.args.get("payment_method")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                clauses = ["p.building_id = %s"]
                params = [building_id]
                if flat_id:
                    clauses.append("p.flat_id = %s")
                    params.append(flat_id)
                if payment_method:
                    clauses.append("p.payment_method = %s")
                    params.append(payment_method)
                if start_date:
                    clauses.append("p.payment_date >= %s")
                    params.append(start_date)
                if end_date:
                    clauses.append("p.payment_date <= %s")
                    params.append(end_date)
                cur.execute(
                    f"{PAYMENT_SELECT} WHERE {' AND '.join(clauses)} ORDER BY p.payment_date DESC, p.created_at DESC",
                    params,
                )
                payments = [serialize_payment(row) for row in cur.fetchall()]
        return jsonify({"payments": payments, "totalAmount": sum(p["amount"] for p in payments)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.put("/payments/<int:payment_id>")
def update_payment(payment_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    fields, values = [], []
    if "amount" in payload:
        amt = to_decimal(payload.get("amount"))
        if amt <= 0:
            return jsonify({"error": "amount must be greater than 0"}), 400
        fields.append("amount = %s")
        values.append(amt)
    if "paymentDate" in payload:
        fields.append("payment_date = %s")
        values.append(payload.get("paymentDate"))
    if "paymentMethod" in payload:
        pm = str(payload.get("paymentMethod", "Cash")).strip()
        fields.append("payment_method = %s")
        values.append(pm if pm in ("Cash", "UPI", "Bank Transfer", "Card", "Other") else "Other")
    if "transactionReference" in payload:
        fields.append("transaction_reference = %s")
        values.append(str(payload.get("transactionReference", "")).strip())
    if "notes" in payload:
        fields.append("notes = %s")
        values.append(str(payload.get("notes", "")).strip())
    if not fields:
        return jsonify({"error": "No fields to update"}), 400
    fields.append("updated_at = NOW()")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE maintenance_payments p SET {', '.join(fields)}
                    FROM buildings bd
                    WHERE p.id = %s AND p.building_id = bd.id AND bd.owner_id = %s
                    RETURNING p.bill_id
                    """,
                    values + [payment_id, owner_id],
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Payment not found"}), 404
                bill_id = row[0]
                recompute_bill_status(cur, bill_id)
                cur.execute(f"{PAYMENT_SELECT} WHERE p.id = %s", (payment_id,))
                payment = serialize_payment(cur.fetchone())
                conn.commit()
        return jsonify({"payment": payment})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.get("/payments/<int:payment_id>/receipt")
def get_payment_receipt(payment_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    {PAYMENT_SELECT}
                    JOIN buildings bd ON bd.id = p.building_id
                    WHERE p.id = %s AND bd.owner_id = %s
                    """,
                    (payment_id, owner_id),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Payment not found"}), 404
                payment = serialize_payment(row)

                cur.execute(
                    "SELECT name, address, city FROM buildings WHERE id = %s",
                    (payment["buildingId"],),
                )
                b = cur.fetchone()
                cur.execute(f"{BILL_SELECT} WHERE b.id = %s", (payment["billId"],))
                bill_row = cur.fetchone()
        receipt = {
            "receiptNumber": payment["receiptNumber"],
            "buildingName": b[0] if b else "",
            "buildingAddress": f"{b[1] or ''}{', ' + b[2] if b and b[2] else ''}" if b else "",
            "flatNumber": payment["flatNumber"],
            "memberName": payment["memberName"],
            "amount": payment["amount"],
            "paymentDate": payment["paymentDate"],
            "paymentMethod": payment["paymentMethod"],
            "transactionReference": payment["transactionReference"],
            "billingMonth": serialize_bill(bill_row)["billingMonth"] if bill_row else None,
            "balanceAfterPayment": serialize_bill(bill_row)["balanceAmount"] if bill_row else None,
        }
        return jsonify({"receipt": receipt})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Phase 3: Staff -> Vendors -> Complaints
# ---------------------------------------------------------------------------

def serialize_staff(row):
    (sid, building_id, full_name, mobile_number, job_type, joining_date, salary, status,
     address, emergency_contact, notes, created_at, updated_at) = row
    return {
        "id": str(sid),
        "buildingId": str(building_id),
        "fullName": full_name,
        "mobileNumber": mobile_number or "",
        "jobType": job_type,
        "joiningDate": joining_date.isoformat() if joining_date else None,
        "salary": float(salary or 0),
        "status": status,
        "address": address or "",
        "emergencyContact": emergency_contact or "",
        "notes": notes or "",
    }


def serialize_vendor(row):
    (vid, building_id, vendor_name, service_type, contact_person, mobile, email, address,
     contract_start, contract_end, status, notes, created_at, updated_at) = row
    return {
        "id": str(vid),
        "buildingId": str(building_id),
        "vendorName": vendor_name,
        "serviceType": service_type,
        "contactPerson": contact_person or "",
        "mobile": mobile or "",
        "email": email or "",
        "address": address or "",
        "contractStartDate": contract_start.isoformat() if contract_start else None,
        "contractEndDate": contract_end.isoformat() if contract_end else None,
        "status": status,
        "notes": notes or "",
    }


def serialize_complaint(row):
    (cid, building_id, flat_id, flat_number, member_id, member_name, category, title, description,
     photo_url, priority, staff_id, staff_name, vendor_id, vendor_name, status, resolution_notes,
     complaint_date, resolved_date, created_at, updated_at) = row
    return {
        "id": str(cid),
        "buildingId": str(building_id),
        "flatId": str(flat_id) if flat_id is not None else None,
        "flatNumber": flat_number,
        "memberId": str(member_id) if member_id is not None else None,
        "memberName": member_name,
        "category": category,
        "title": title,
        "description": description or "",
        "photoUrl": photo_url or "",
        "priority": priority,
        "assignedStaffId": str(staff_id) if staff_id is not None else None,
        "assignedStaffName": staff_name,
        "assignedVendorId": str(vendor_id) if vendor_id is not None else None,
        "assignedVendorName": vendor_name,
        "status": status,
        "resolutionNotes": resolution_notes or "",
        "complaintDate": complaint_date.isoformat() if complaint_date else None,
        "resolvedDate": resolved_date.isoformat() if resolved_date else None,
    }


COMPLAINT_SELECT = """
    SELECT c.id, c.building_id, c.flat_id, fl.flat_number, c.member_id, m.full_name,
           c.category, c.title, c.description, c.photo_url, c.priority,
           c.assigned_staff_id, st.full_name, c.assigned_vendor_id, ve.vendor_name,
           c.status, c.resolution_notes, c.complaint_date, c.resolved_date, c.created_at, c.updated_at
    FROM building_complaints c
    LEFT JOIN building_flats fl ON fl.id = c.flat_id
    LEFT JOIN building_members m ON m.id = c.member_id
    LEFT JOIN building_staff st ON st.id = c.assigned_staff_id
    LEFT JOIN building_vendors ve ON ve.id = c.assigned_vendor_id
"""


# --- Staff ---

@building_bp.get("/buildings/<int:building_id>/staff")
def list_staff(building_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    job_type = request.args.get("job_type")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                clauses = ["building_id = %s", "status = 'active'"]
                params = [building_id]
                if job_type:
                    clauses.append("job_type = %s")
                    params.append(job_type)
                cur.execute(
                    f"""
                    SELECT id, building_id, full_name, mobile_number, job_type, joining_date, salary,
                           status, address, emergency_contact, notes, created_at, updated_at
                    FROM building_staff WHERE {' AND '.join(clauses)} ORDER BY full_name ASC
                    """,
                    params,
                )
                staff = [serialize_staff(row) for row in cur.fetchall()]
        return jsonify({"staff": staff})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.post("/buildings/<int:building_id>/staff")
def create_staff(building_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    full_name = str(payload.get("fullName", "")).strip()
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not full_name:
        return jsonify({"error": "fullName is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                cur.execute(
                    """
                    INSERT INTO building_staff
                        (building_id, full_name, mobile_number, job_type, joining_date, salary,
                         address, emergency_contact, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, building_id, full_name, mobile_number, job_type, joining_date, salary,
                              status, address, emergency_contact, notes, created_at, updated_at
                    """,
                    (
                        building_id, full_name, str(payload.get("mobileNumber", "")).strip(),
                        str(payload.get("jobType", "Other")).strip() or "Other",
                        payload.get("joiningDate") or None, to_decimal(payload.get("salary")),
                        str(payload.get("address", "")).strip(), str(payload.get("emergencyContact", "")).strip(),
                        str(payload.get("notes", "")).strip(),
                    ),
                )
                staff = serialize_staff(cur.fetchone())
                conn.commit()
        return jsonify({"staff": staff}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.put("/staff/<int:staff_id>")
def update_staff(staff_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    text_fields = {
        "fullName": "full_name", "mobileNumber": "mobile_number", "jobType": "job_type",
        "address": "address", "emergencyContact": "emergency_contact", "notes": "notes",
    }
    fields, values = [], []
    for k, col in text_fields.items():
        if k in payload:
            fields.append(f"{col} = %s")
            values.append(str(payload.get(k, "")).strip())
    if "joiningDate" in payload:
        fields.append("joining_date = %s")
        values.append(payload.get("joiningDate") or None)
    if "salary" in payload:
        fields.append("salary = %s")
        values.append(to_decimal(payload.get("salary")))
    if not fields:
        return jsonify({"error": "No fields to update"}), 400
    fields.append("updated_at = NOW()")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE building_staff s SET {', '.join(fields)}
                    FROM buildings b
                    WHERE s.id = %s AND s.building_id = b.id AND b.owner_id = %s
                    RETURNING s.id, s.building_id, s.full_name, s.mobile_number, s.job_type, s.joining_date,
                              s.salary, s.status, s.address, s.emergency_contact, s.notes, s.created_at, s.updated_at
                    """,
                    values + [staff_id, owner_id],
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Staff not found"}), 404
                staff = serialize_staff(row)
                conn.commit()
        return jsonify({"staff": staff})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.delete("/staff/<int:staff_id>")
def delete_staff(staff_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE building_staff s SET status = 'inactive', updated_at = NOW()
                    FROM buildings b
                    WHERE s.id = %s AND s.building_id = b.id AND b.owner_id = %s
                    RETURNING s.id
                    """,
                    (staff_id, owner_id),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Staff not found"}), 404
                conn.commit()
        return jsonify({"message": "Staff removed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Vendors ---

@building_bp.get("/buildings/<int:building_id>/vendors")
def list_vendors(building_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    service_type = request.args.get("service_type")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                clauses = ["building_id = %s", "status = 'active'"]
                params = [building_id]
                if service_type:
                    clauses.append("service_type = %s")
                    params.append(service_type)
                cur.execute(
                    f"""
                    SELECT id, building_id, vendor_name, service_type, contact_person, mobile, email,
                           address, contract_start_date, contract_end_date, status, notes, created_at, updated_at
                    FROM building_vendors WHERE {' AND '.join(clauses)} ORDER BY vendor_name ASC
                    """,
                    params,
                )
                vendors = [serialize_vendor(row) for row in cur.fetchall()]
        return jsonify({"vendors": vendors})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.post("/buildings/<int:building_id>/vendors")
def create_vendor(building_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    vendor_name = str(payload.get("vendorName", "")).strip()
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not vendor_name:
        return jsonify({"error": "vendorName is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                cur.execute(
                    """
                    INSERT INTO building_vendors
                        (building_id, vendor_name, service_type, contact_person, mobile, email,
                         address, contract_start_date, contract_end_date, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, building_id, vendor_name, service_type, contact_person, mobile, email,
                              address, contract_start_date, contract_end_date, status, notes, created_at, updated_at
                    """,
                    (
                        building_id, vendor_name, str(payload.get("serviceType", "Other")).strip() or "Other",
                        str(payload.get("contactPerson", "")).strip(), str(payload.get("mobile", "")).strip(),
                        str(payload.get("email", "")).strip(), str(payload.get("address", "")).strip(),
                        payload.get("contractStartDate") or None, payload.get("contractEndDate") or None,
                        str(payload.get("notes", "")).strip(),
                    ),
                )
                vendor = serialize_vendor(cur.fetchone())
                conn.commit()
        return jsonify({"vendor": vendor}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.put("/vendors/<int:vendor_id>")
def update_vendor(vendor_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    text_fields = {
        "vendorName": "vendor_name", "serviceType": "service_type", "contactPerson": "contact_person",
        "mobile": "mobile", "email": "email", "address": "address", "notes": "notes",
    }
    fields, values = [], []
    for k, col in text_fields.items():
        if k in payload:
            fields.append(f"{col} = %s")
            values.append(str(payload.get(k, "")).strip())
    if "contractStartDate" in payload:
        fields.append("contract_start_date = %s")
        values.append(payload.get("contractStartDate") or None)
    if "contractEndDate" in payload:
        fields.append("contract_end_date = %s")
        values.append(payload.get("contractEndDate") or None)
    if not fields:
        return jsonify({"error": "No fields to update"}), 400
    fields.append("updated_at = NOW()")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE building_vendors v SET {', '.join(fields)}
                    FROM buildings b
                    WHERE v.id = %s AND v.building_id = b.id AND b.owner_id = %s
                    RETURNING v.id, v.building_id, v.vendor_name, v.service_type, v.contact_person, v.mobile,
                              v.email, v.address, v.contract_start_date, v.contract_end_date, v.status, v.notes,
                              v.created_at, v.updated_at
                    """,
                    values + [vendor_id, owner_id],
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Vendor not found"}), 404
                vendor = serialize_vendor(row)
                conn.commit()
        return jsonify({"vendor": vendor})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.delete("/vendors/<int:vendor_id>")
def delete_vendor(vendor_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE building_vendors v SET status = 'inactive', updated_at = NOW()
                    FROM buildings b
                    WHERE v.id = %s AND v.building_id = b.id AND b.owner_id = %s
                    RETURNING v.id
                    """,
                    (vendor_id, owner_id),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Vendor not found"}), 404
                conn.commit()
        return jsonify({"message": "Vendor removed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Complaints ---

@building_bp.get("/buildings/<int:building_id>/complaints")
def list_complaints(building_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    status = request.args.get("status")
    category = request.args.get("category")
    priority = request.args.get("priority")
    flat_id = request.args.get("flat_id")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                clauses = ["c.building_id = %s"]
                params = [building_id]
                if status:
                    clauses.append("c.status = %s")
                    params.append(status)
                if category:
                    clauses.append("c.category = %s")
                    params.append(category)
                if priority:
                    clauses.append("c.priority = %s")
                    params.append(priority)
                if flat_id:
                    clauses.append("c.flat_id = %s")
                    params.append(flat_id)
                cur.execute(
                    f"{COMPLAINT_SELECT} WHERE {' AND '.join(clauses)} "
                    "ORDER BY (c.status NOT IN ('Resolved', 'Closed', 'Rejected')) DESC, c.complaint_date DESC",
                    params,
                )
                complaints = [serialize_complaint(row) for row in cur.fetchall()]
        open_count = sum(1 for c in complaints if c["status"] in ("New", "Assigned", "In Progress"))
        return jsonify({"complaints": complaints, "openCount": open_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.post("/buildings/<int:building_id>/complaints")
def create_complaint(building_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    title = str(payload.get("title", "")).strip()
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not title:
        return jsonify({"error": "title is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if not _get_owned_building_id(cur, building_id, owner_id):
                    return jsonify({"error": "Building not found"}), 404
                cur.execute(
                    """
                    INSERT INTO building_complaints
                        (building_id, flat_id, member_id, category, title, description, priority,
                         assigned_staff_id, assigned_vendor_id, complaint_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, CURRENT_DATE))
                    RETURNING id
                    """,
                    (
                        building_id, payload.get("flatId") or None, payload.get("memberId") or None,
                        str(payload.get("category", "Other")).strip() or "Other", title,
                        str(payload.get("description", "")).strip(),
                        str(payload.get("priority", "Medium")).strip() or "Medium",
                        payload.get("assignedStaffId") or None, payload.get("assignedVendorId") or None,
                        payload.get("complaintDate") or None,
                    ),
                )
                complaint_id = cur.fetchone()[0]
                if payload.get("assignedStaffId") or payload.get("assignedVendorId"):
                    cur.execute(
                        "UPDATE building_complaints SET status = 'Assigned' WHERE id = %s",
                        (complaint_id,),
                    )
                cur.execute(f"{COMPLAINT_SELECT} WHERE c.id = %s", (complaint_id,))
                complaint = serialize_complaint(cur.fetchone())
                conn.commit()
        return jsonify({"complaint": complaint}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.put("/complaints/<int:complaint_id>")
def update_complaint(complaint_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    text_fields = {
        "category": "category", "title": "title", "description": "description",
        "priority": "priority", "resolutionNotes": "resolution_notes",
    }
    fields, values = [], []
    for k, col in text_fields.items():
        if k in payload:
            fields.append(f"{col} = %s")
            values.append(str(payload.get(k, "")).strip())
    if "flatId" in payload:
        fields.append("flat_id = %s")
        values.append(payload.get("flatId") or None)
    if "assignedStaffId" in payload:
        fields.append("assigned_staff_id = %s")
        values.append(payload.get("assignedStaffId") or None)
    if "assignedVendorId" in payload:
        fields.append("assigned_vendor_id = %s")
        values.append(payload.get("assignedVendorId") or None)
    if "status" in payload:
        new_status = str(payload.get("status")).strip()
        fields.append("status = %s")
        values.append(new_status)
        if new_status in ("Resolved", "Closed"):
            fields.append("resolved_date = COALESCE(resolved_date, CURRENT_DATE)")
        elif new_status in ("New", "Assigned", "In Progress"):
            fields.append("resolved_date = NULL")
    if not fields:
        return jsonify({"error": "No fields to update"}), 400
    fields.append("updated_at = NOW()")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE building_complaints c SET {', '.join(fields)}
                    FROM buildings b
                    WHERE c.id = %s AND c.building_id = b.id AND b.owner_id = %s
                    RETURNING c.id
                    """,
                    values + [complaint_id, owner_id],
                )
                if not cur.fetchone():
                    return jsonify({"error": "Complaint not found"}), 404
                cur.execute(f"{COMPLAINT_SELECT} WHERE c.id = %s", (complaint_id,))
                complaint = serialize_complaint(cur.fetchone())
                conn.commit()
        return jsonify({"complaint": complaint})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@building_bp.delete("/complaints/<int:complaint_id>")
def delete_complaint(complaint_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM building_complaints c
                    USING buildings b
                    WHERE c.id = %s AND c.building_id = b.id AND b.owner_id = %s
                    RETURNING c.id
                    """,
                    (complaint_id, owner_id),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Complaint not found"}), 404
                conn.commit()
        return jsonify({"message": "Complaint deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
