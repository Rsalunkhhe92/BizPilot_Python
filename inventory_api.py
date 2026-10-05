"""
Inventory Business REST API Endpoints (Flask Blueprint)
Mounted at /api/inventory
For Fruit Sellers / other product-based business types: a product catalog
plus daily per-product transactions with daily/weekly/monthly filtering.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request
import psycopg

inventory_bp = Blueprint("inventory", __name__, url_prefix="/api/inventory")

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
        "generationConfig": {"maxOutputTokens": 300, "temperature": 0.15, "thinkingConfig": {"thinkingBudget": 0}},
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
    """Resolve the logged-in business owner's userdetails.id from query/body/header."""
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


def parse_date(date_str):
    if not date_str:
        return date.today()
    try:
        return date.fromisoformat(str(date_str).strip())
    except ValueError:
        return date.today()


def serialize_product(row):
    (pid, name, category, unit, stock_qty, selling_price, cost_price,
     low_stock_threshold, status, created_at, updated_at) = row
    return {
        "id": str(pid),
        "name": name,
        "category": category,
        "unit": unit,
        "stockQty": float(stock_qty or 0),
        "sellingPrice": float(selling_price or 0),
        "costPrice": float(cost_price or 0),
        "lowStockThreshold": float(low_stock_threshold or 0),
        "status": status,
    }


def serialize_transaction(row):
    (tid, product_id, product_name, quantity, unit, amount, transaction_type, payment_method,
     transaction_date, note, created_at, amount_paid) = row
    amount_f = float(amount or 0)
    paid_f = float(amount_paid or 0)
    return {
        "id": str(tid),
        "productId": str(product_id) if product_id is not None else None,
        "productName": product_name,
        "quantity": float(quantity or 0),
        "unit": unit,
        "amount": amount_f,
        "amountPaid": paid_f,
        "amountDue": round(amount_f - paid_f, 2) if transaction_type == "PURCHASE" else 0,
        "type": transaction_type or "SALE",
        "paymentMethod": payment_method or "CASH",
        "date": transaction_date.isoformat() if transaction_date else None,
        "note": note or "",
        "createdAt": created_at.isoformat() if created_at else None,
    }


@inventory_bp.get("/products")
def list_products():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, category, unit, stock_qty, selling_price, cost_price,
                           low_stock_threshold, status, created_at, updated_at
                    FROM inventory_products
                    WHERE owner_id = %s AND status = 'active'
                    ORDER BY name ASC
                    """,
                    (owner_id,),
                )
                products = [serialize_product(row) for row in cur.fetchall()]
        return jsonify({"products": products})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.post("/products")
def create_product():
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    name = str(payload.get("name", "")).strip()
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not name:
        return jsonify({"error": "Product name is required"}), 400

    category = str(payload.get("category", "General")).strip() or "General"
    unit = str(payload.get("unit", "kg")).strip() or "kg"
    stock_qty = to_decimal(payload.get("stockQty"))
    selling_price = to_decimal(payload.get("sellingPrice"))
    cost_price = to_decimal(payload.get("costPrice"))
    low_stock_threshold = to_decimal(payload.get("lowStockThreshold"))

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO inventory_products
                        (owner_id, name, category, unit, stock_qty, selling_price, cost_price, low_stock_threshold)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, name, category, unit, stock_qty, selling_price, cost_price,
                              low_stock_threshold, status, created_at, updated_at
                    """,
                    (owner_id, name, category, unit, stock_qty, selling_price, cost_price, low_stock_threshold),
                )
                product = serialize_product(cur.fetchone())
                conn.commit()
        return jsonify({"product": product}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.put("/products/<int:product_id>")
def update_product(product_id):
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400

    fields = []
    values = []
    if "name" in payload:
        fields.append("name = %s")
        values.append(str(payload.get("name", "")).strip())
    if "category" in payload:
        fields.append("category = %s")
        values.append(str(payload.get("category", "")).strip() or "General")
    if "unit" in payload:
        fields.append("unit = %s")
        values.append(str(payload.get("unit", "")).strip() or "kg")
    if "stockQty" in payload:
        fields.append("stock_qty = %s")
        values.append(to_decimal(payload.get("stockQty")))
    if "sellingPrice" in payload:
        fields.append("selling_price = %s")
        values.append(to_decimal(payload.get("sellingPrice")))
    if "costPrice" in payload:
        fields.append("cost_price = %s")
        values.append(to_decimal(payload.get("costPrice")))
    if "lowStockThreshold" in payload:
        fields.append("low_stock_threshold = %s")
        values.append(to_decimal(payload.get("lowStockThreshold")))

    if not fields:
        return jsonify({"error": "No fields to update"}), 400

    fields.append("updated_at = NOW()")
    values.extend([product_id, owner_id])

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE inventory_products SET {', '.join(fields)}
                    WHERE id = %s AND owner_id = %s
                    RETURNING id, name, category, unit, stock_qty, selling_price, cost_price,
                              low_stock_threshold, status, created_at, updated_at
                    """,
                    values,
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Product not found"}), 404
                product = serialize_product(row)
                conn.commit()
        return jsonify({"product": product})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.delete("/products/<int:product_id>")
def delete_product(product_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE inventory_products SET status = 'inactive', updated_at = NOW() WHERE id = %s AND owner_id = %s RETURNING id",
                    (product_id, owner_id),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Product not found"}), 404
                conn.commit()
        return jsonify({"message": "Product removed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.post("/transactions")
def create_transaction():
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    product_id = payload.get("productId") or payload.get("product_id")
    amount = to_decimal(payload.get("amount"))
    quantity = to_decimal(payload.get("quantity"))
    tx_type = str(payload.get("type", "SALE")).strip().upper()
    if tx_type not in ("SALE", "WASTAGE", "PURCHASE"):
        tx_type = "SALE"
    payment_method = str(payload.get("paymentMethod", "CASH")).strip().upper()
    if payment_method not in ("CASH", "UPI", "CREDIT"):
        payment_method = "CASH"

    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not product_id:
        return jsonify({"error": "productId is required"}), 400
    if tx_type == "WASTAGE":
        if quantity <= 0:
            return jsonify({"error": "quantity is required for wastage entries"}), 400
    elif tx_type == "PURCHASE":
        if quantity <= 0:
            return jsonify({"error": "quantity is required for purchase entries"}), 400
        if amount <= 0:
            return jsonify({"error": "total cost is required for purchase entries"}), 400
    elif amount <= 0:
        return jsonify({"error": "amount must be greater than 0"}), 400

    tx_date = parse_date(payload.get("date"))
    note = str(payload.get("note", "")).strip()

    # amount_paid: how much of a PURCHASE's total cost was actually paid now. Defaults
    # to the full amount (preserving old "always fully paid" behavior) when the
    # frontend doesn't send it; SALE/WASTAGE have no due concept, so always "paid".
    if tx_type == "PURCHASE" and "amountPaid" in payload:
        amount_paid = to_decimal(payload.get("amountPaid"))
        if amount_paid < 0:
            amount_paid = Decimal("0")
        if amount_paid > amount:
            amount_paid = amount
    else:
        amount_paid = amount

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, unit, stock_qty, cost_price FROM inventory_products WHERE id = %s AND owner_id = %s",
                    (product_id, owner_id),
                )
                product_row = cur.fetchone()
                if not product_row:
                    return jsonify({"error": "Product not found"}), 404
                _, product_name, product_unit, stock_qty, cost_price = product_row

                if tx_type == "WASTAGE" and amount <= 0:
                    amount = (quantity * cost_price) if cost_price else Decimal("0")
                    amount_paid = amount

                cur.execute(
                    """
                    INSERT INTO inventory_transactions
                        (owner_id, product_id, product_name, quantity, unit, amount, transaction_type, payment_method, transaction_date, note, amount_paid)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, product_id, product_name, quantity, unit, amount, transaction_type, payment_method, transaction_date, note, created_at, amount_paid
                    """,
                    (owner_id, product_id, product_name, quantity, product_unit, amount, tx_type, payment_method, tx_date, note, amount_paid),
                )
                transaction = serialize_transaction(cur.fetchone())

                if quantity > 0:
                    if tx_type == "PURCHASE":
                        cur.execute(
                            "UPDATE inventory_products SET stock_qty = stock_qty + %s, updated_at = NOW() WHERE id = %s",
                            (quantity, product_id),
                        )
                    else:
                        cur.execute(
                            "UPDATE inventory_products SET stock_qty = GREATEST(stock_qty - %s, 0), updated_at = NOW() WHERE id = %s",
                            (quantity, product_id),
                        )
                conn.commit()
        return jsonify({"transaction": transaction}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.delete("/transactions/<int:transaction_id>")
def delete_transaction(transaction_id):
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM inventory_transactions WHERE id = %s AND owner_id = %s RETURNING id",
                    (transaction_id, owner_id),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Transaction not found"}), 404
                conn.commit()
        return jsonify({"message": "Transaction deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.get("/vendors")
def list_vendors():
    """One row per vendor the owner has ever bought from, paid, or added a contact
    profile for - with running totals. Powers the Vendors tab's passbook list."""
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                vendors = {}

                def _get_or_create(name):
                    key = name.strip().lower()
                    if key not in vendors:
                        vendors[key] = {
                            "vendorName": name.strip(), "totalCost": 0.0, "totalPaid": 0.0,
                            "lastActivity": None, "purchaseCount": 0, "mobileNumber": "", "email": "",
                        }
                    return vendors[key]

                cur.execute(
                    """
                    SELECT note, COALESCE(SUM(amount), 0), COALESCE(SUM(amount_paid), 0), MAX(transaction_date), COUNT(*)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_type = 'PURCHASE' AND note IS NOT NULL AND note != ''
                    GROUP BY note
                    """,
                    (owner_id,),
                )
                for name, total_cost, total_paid_at_purchase, last_date, purchase_count in cur.fetchall():
                    v = _get_or_create(name)
                    v["totalCost"] += float(total_cost or 0)
                    v["totalPaid"] += float(total_paid_at_purchase or 0)
                    v["purchaseCount"] += purchase_count
                    last_iso = last_date.isoformat() if last_date else None
                    if last_iso and (not v["lastActivity"] or last_iso > v["lastActivity"]):
                        v["lastActivity"] = last_iso

                cur.execute(
                    """
                    SELECT vendor_name, COALESCE(SUM(amount), 0), MAX(payment_date)
                    FROM inventory_vendor_payments
                    WHERE owner_id = %s
                    GROUP BY vendor_name
                    """,
                    (owner_id,),
                )
                for name, total_followup, last_date in cur.fetchall():
                    v = _get_or_create(name)
                    v["totalPaid"] += float(total_followup or 0)
                    last_iso = last_date.isoformat() if last_date else None
                    if last_iso and (not v["lastActivity"] or last_iso > v["lastActivity"]):
                        v["lastActivity"] = last_iso

                cur.execute(
                    "SELECT name, mobile_number, email FROM inventory_vendors WHERE owner_id = %s",
                    (owner_id,),
                )
                for name, mobile_number, email in cur.fetchall():
                    v = _get_or_create(name)
                    v["vendorName"] = name.strip()
                    v["mobileNumber"] = mobile_number or ""
                    v["email"] = email or ""

        result = []
        for v in vendors.values():
            v["totalDue"] = round(v["totalCost"] - v["totalPaid"], 2)
            result.append(v)
        result.sort(key=lambda v: (-v["totalDue"], v["vendorName"] or ""))
        return jsonify({"vendors": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.post("/vendors")
def create_vendor():
    """Lets the owner add a vendor's contact details proactively, before any
    purchase from them exists."""
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    name = str(payload.get("name", "")).strip()
    mobile_number = str(payload.get("mobileNumber", "")).strip()
    email = str(payload.get("email", "")).strip()
    address = str(payload.get("address", "")).strip()

    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not name:
        return jsonify({"error": "Vendor name is required"}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO inventory_vendors (owner_id, name, mobile_number, email, address)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (owner_id, lower(name)) DO UPDATE
                        SET mobile_number = EXCLUDED.mobile_number, email = EXCLUDED.email,
                            address = EXCLUDED.address, updated_at = NOW()
                    RETURNING id, name, mobile_number, email, address
                    """,
                    (owner_id, name, mobile_number, email, address),
                )
                row = cur.fetchone()
                conn.commit()
        return jsonify({
            "vendor": {
                "id": str(row[0]), "name": row[1], "mobileNumber": row[2] or "",
                "email": row[3] or "", "address": row[4] or "",
            }
        }), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.get("/vendor-ledger")
def get_vendor_ledger():
    """All purchases from, and payments made to, one vendor - with running totals so
    the owner can see exactly how much they still owe that vendor."""
    owner_id = get_owner_id()
    vendor_name = str(request.args.get("vendor", "")).strip()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    if not vendor_name:
        return jsonify({"error": "vendor is required"}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT mobile_number, email, address FROM inventory_vendors WHERE owner_id = %s AND lower(name) = lower(%s)",
                    (owner_id, vendor_name),
                )
                profile_row = cur.fetchone()
                mobile_number, email, address = profile_row if profile_row else ("", "", "")

                cur.execute(
                    """
                    SELECT id, product_name, quantity, unit, amount, amount_paid, transaction_date, created_at
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_type = 'PURCHASE' AND note = %s
                    ORDER BY transaction_date DESC, created_at DESC
                    """,
                    (owner_id, vendor_name),
                )
                purchases = []
                total_cost = Decimal("0")
                total_paid_at_purchase = Decimal("0")
                for (pid, product_name, quantity, unit, amount, amount_paid, tx_date, created_at) in cur.fetchall():
                    amount = amount or Decimal("0")
                    amount_paid = amount_paid or Decimal("0")
                    total_cost += amount
                    total_paid_at_purchase += amount_paid
                    purchases.append({
                        "id": str(pid),
                        "productName": product_name,
                        "quantity": float(quantity or 0),
                        "unit": unit,
                        "totalCost": float(amount),
                        "amountPaid": float(amount_paid),
                        "amountDue": float(amount - amount_paid),
                        "date": tx_date.isoformat() if tx_date else None,
                    })

                cur.execute(
                    """
                    SELECT id, amount, payment_method, payment_date, note, created_at
                    FROM inventory_vendor_payments
                    WHERE owner_id = %s AND vendor_name = %s
                    ORDER BY payment_date DESC, created_at DESC
                    """,
                    (owner_id, vendor_name),
                )
                payments = []
                total_followup_paid = Decimal("0")
                for (pid, amount, payment_method, pay_date, note, created_at) in cur.fetchall():
                    amount = amount or Decimal("0")
                    total_followup_paid += amount
                    payments.append({
                        "id": str(pid),
                        "amount": float(amount),
                        "paymentMethod": payment_method,
                        "date": pay_date.isoformat() if pay_date else None,
                        "note": note or "",
                    })

        total_paid = total_paid_at_purchase + total_followup_paid
        return jsonify({
            "vendorName": vendor_name,
            "mobileNumber": mobile_number or "",
            "email": email or "",
            "address": address or "",
            "purchases": purchases,
            "payments": payments,
            "totals": {
                "totalCost": float(total_cost),
                "totalPaid": float(total_paid),
                "totalDue": float(total_cost - total_paid),
            },
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.post("/vendor-payments")
def create_vendor_payment():
    """Records a follow-up payment to a vendor, reducing their outstanding due
    balance without being tied to any single purchase line item."""
    payload = request.get_json(silent=True) or {}
    owner_id = get_owner_id(payload)
    vendor_name = str(payload.get("vendorName", "")).strip()
    amount = to_decimal(payload.get("amount"))
    payment_method = str(payload.get("paymentMethod", "CASH")).strip().upper()
    if payment_method not in ("CASH", "UPI", "CREDIT"):
        payment_method = "CASH"
    note = str(payload.get("note", "")).strip()
    pay_date = parse_date(payload.get("date"))

    if not owner_id:
        return jsonify({"error": "ownerId is required"}), 400
    if not vendor_name:
        return jsonify({"error": "vendorName is required"}), 400
    if amount <= 0:
        return jsonify({"error": "amount must be greater than 0"}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO inventory_vendor_payments (owner_id, vendor_name, amount, payment_method, payment_date, note)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, vendor_name, amount, payment_method, payment_date, note, created_at
                    """,
                    (owner_id, vendor_name, amount, payment_method, pay_date, note),
                )
                row = cur.fetchone()
                conn.commit()
        return jsonify({
            "payment": {
                "id": str(row[0]),
                "vendorName": row[1],
                "amount": float(row[2] or 0),
                "paymentMethod": row[3],
                "date": row[4].isoformat() if row[4] else None,
                "note": row[5] or "",
            }
        }), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.post("/voice-agent")
def voice_agent():
    """Transcribes a short voice note from a Fruit Seller-style inventory owner and figures
    out whether they're adding a new product or recording a sale/purchase/wastage, e.g.
    'add new product mango, 50 rupees per kg' or 'sold 3 kg apple for 200 cash'.
    Never writes to the database itself - the app shows the parsed result in the existing
    Add Product / Record Sale form for the owner to review and save, same as manual entry."""
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

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name, unit FROM inventory_products WHERE owner_id = %s AND status = 'active' ORDER BY name ASC",
                    (owner_id,),
                )
                existing_products = cur.fetchall()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    product_list_text = (
        ", ".join(f"{name} ({unit})" for name, unit in existing_products)
        if existing_products else "(no products yet)"
    )

    system_prompt = (
        "You are a voice assistant for a small fruit/vegetable seller's inventory app. The owner "
        "says something short in English, Hindi, or Marathi, about either (a) adding a brand new "
        "product to their catalog, or (b) recording a sale, purchase (restock), or wastage of a "
        "product they already sell. Examples: 'add new product mango, cost forty rupees per kg, "
        "selling price sixty', 'naya item kela, pachees rupaye kilo', 'sold 3 kg apple for 200 "
        "rupees cash', 'bought 10 kg onion for 150 upi', '2 kg tomato kharab ho gaya'.\n\n"
        f"The owner's EXISTING active products are: {product_list_text}.\n\n"
        "Listen to the audio and reply with STRICT JSON only - no markdown fences, no explanation - "
        "in exactly this shape:\n"
        '{"intent": "add_product" or "record_transaction" or "unclear", '
        '"transcript": "<what you heard, in the original language>", '
        '"product": {"name": <string or null>, "unit": "kg" or "piece" or "dozen" or "litre" or null, '
        '"costPrice": <number or null>, "sellingPrice": <number or null>}, '
        '"transaction": {"productName": <string or null, MUST be copied verbatim from the existing '
        'products list above - never invent or guess a product name that is not in that list>, '
        '"quantity": <number or null>, "amount": <number or null>, '
        '"type": "SALE" or "PURCHASE" or "WASTAGE" or null, '
        '"paymentMethod": "CASH" or "UPI" or "CREDIT" or null}}.\n\n'
        'Set "intent" to "add_product" only if the owner is clearly describing a brand new product '
        'not already in their list. Set it to "record_transaction" if they are describing a sale, '
        'purchase/restock, or wastage of one of their EXISTING products - match it to the closest '
        'name in the list (e.g. "tamatar" matches "Tomato"), and if there is no reasonable match, '
        'set transaction.productName to null and intent to "unclear". If the audio is silent, '
        'contains no intelligible speech, is just background noise, or you are not highly confident '
        'about what was said, you MUST set intent to "unclear", transcript to an empty string, and '
        'every field inside product/transaction to null - do NOT invent or guess plausible-sounding '
        'values just because a field is expected. Only fill in the "product" object for add_product, '
        'and only the "transaction" object for record_transaction - leave the other one with all '
        "null fields. Never guess a number, name, or amount that wasn't actually said."
    )

    try:
        reply_text = _call_gemini_audio(system_prompt, audio_base64, mime_type, api_key)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"Inventory voice agent HTTP error {e.code}: {err_body}")
        return jsonify({"error": "Voice logging is unavailable right now. Please try again or enter it manually."}), 200
    except Exception as e:
        print(f"Inventory voice agent error: {e}")
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

    intent = parsed.get("intent")
    if intent not in ("add_product", "record_transaction"):
        intent = "unclear"

    def _num(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    raw_product = parsed.get("product") or {}
    product_name = raw_product.get("name")
    product_out = {
        "name": str(product_name).strip() if product_name else None,
        "unit": raw_product.get("unit") if raw_product.get("unit") in ("kg", "piece", "dozen", "litre") else None,
        "costPrice": _num(raw_product.get("costPrice")),
        "sellingPrice": _num(raw_product.get("sellingPrice")),
    }
    if intent == "add_product" and not product_out["name"]:
        intent = "unclear"

    raw_tx = parsed.get("transaction") or {}
    tx_product_name = raw_tx.get("productName")
    matched_name = None
    if tx_product_name:
        tx_product_name_lower = str(tx_product_name).strip().lower()
        for existing_name, _unit in existing_products:
            if existing_name.strip().lower() == tx_product_name_lower:
                matched_name = existing_name
                break
    tx_type = raw_tx.get("type")
    if tx_type not in ("SALE", "PURCHASE", "WASTAGE"):
        tx_type = None
    payment_method = raw_tx.get("paymentMethod")
    if payment_method not in ("CASH", "UPI", "CREDIT"):
        payment_method = None
    transaction_out = {
        "productName": matched_name,
        "quantity": _num(raw_tx.get("quantity")),
        "amount": _num(raw_tx.get("amount")),
        "type": tx_type,
        "paymentMethod": payment_method,
    }
    if intent == "record_transaction" and not matched_name:
        intent = "unclear"

    return jsonify({
        "intent": intent,
        "transcript": parsed.get("transcript", ""),
        "product": product_out,
        "transaction": transaction_out,
    })


def _period_bounds(period, anchor):
    if period == "weekly":
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=6)
    elif period == "monthly":
        start = anchor.replace(day=1)
        if anchor.month == 12:
            end = anchor.replace(day=31)
        else:
            end = anchor.replace(month=anchor.month + 1, day=1) - timedelta(days=1)
    else:
        start = anchor
        end = anchor
    return start, end


@inventory_bp.get("/transactions")
def list_transactions():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400

    period = str(request.args.get("period", "daily")).strip().lower()
    if period not in ("daily", "weekly", "monthly"):
        period = "daily"
    anchor = parse_date(request.args.get("date"))
    start_date, end_date = _period_bounds(period, anchor)

    type_filter = str(request.args.get("type", "all")).strip().upper()
    if type_filter not in ("ALL", "SALE", "WASTAGE", "PURCHASE"):
        type_filter = "ALL"

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                type_clause = ""
                params = [owner_id, start_date, end_date]
                if type_filter != "ALL":
                    type_clause = "AND transaction_type = %s"
                    params.append(type_filter)

                cur.execute(
                    f"""
                    SELECT id, product_id, product_name, quantity, unit, amount, transaction_type, payment_method, transaction_date, note, created_at, amount_paid
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date BETWEEN %s AND %s {type_clause}
                    ORDER BY transaction_date DESC, created_at DESC
                    """,
                    params,
                )
                rows = cur.fetchall()
                transactions = [serialize_transaction(row) for row in rows]

                cur.execute(
                    """
                    SELECT product_name, COALESCE(SUM(quantity), 0), COALESCE(SUM(amount), 0), COUNT(*)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date BETWEEN %s AND %s AND transaction_type = 'SALE'
                    GROUP BY product_name
                    ORDER BY SUM(amount) DESC
                    """,
                    (owner_id, start_date, end_date),
                )
                by_product = [
                    {
                        "productName": r[0],
                        "quantity": float(r[1] or 0),
                        "amount": float(r[2] or 0),
                        "count": r[3],
                    }
                    for r in cur.fetchall()
                ]

                cur.execute(
                    """
                    SELECT transaction_type, COALESCE(SUM(amount), 0), COALESCE(SUM(quantity), 0), COUNT(*)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date BETWEEN %s AND %s
                    GROUP BY transaction_type
                    """,
                    (owner_id, start_date, end_date),
                )
                type_totals = {
                    r[0]: {"amount": float(r[1] or 0), "quantity": float(r[2] or 0), "count": r[3]}
                    for r in cur.fetchall()
                }

                cur.execute(
                    """
                    SELECT payment_method, COALESCE(SUM(amount), 0)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date BETWEEN %s AND %s AND transaction_type = 'SALE'
                    GROUP BY payment_method
                    """,
                    (owner_id, start_date, end_date),
                )
                payment_totals = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

        sale_totals = type_totals.get("SALE", {"amount": 0, "quantity": 0, "count": 0})
        wastage_totals = type_totals.get("WASTAGE", {"amount": 0, "quantity": 0, "count": 0})
        purchase_totals = type_totals.get("PURCHASE", {"amount": 0, "quantity": 0, "count": 0})

        return jsonify({
            "transactions": transactions,
            "period": period,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "summary": {
                "totalAmount": sale_totals["amount"],
                "totalQuantity": sale_totals["quantity"],
                "totalCount": len(transactions),
                "wastageValue": wastage_totals["amount"],
                "wastageQuantity": wastage_totals["quantity"],
                "wastageCount": wastage_totals["count"],
                "purchaseValue": purchase_totals["amount"],
                "purchaseQuantity": purchase_totals["quantity"],
                "purchaseCount": purchase_totals["count"],
                "netProfit": sale_totals["amount"] - purchase_totals["amount"] - wastage_totals["amount"],
                "paymentMethodSplit": {
                    "cash": payment_totals.get("CASH", 0),
                    "upi": payment_totals.get("UPI", 0),
                    "credit": payment_totals.get("CREDIT", 0),
                },
                "byProduct": by_product,
            },
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inventory_bp.get("/insights")
def get_insights():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Top selling products (last 30 days, by amount)
                cur.execute(
                    """
                    SELECT product_name, COALESCE(SUM(amount), 0), COALESCE(SUM(quantity), 0)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date >= CURRENT_DATE - INTERVAL '30 days'
                        AND transaction_type = 'SALE'
                    GROUP BY product_name
                    ORDER BY SUM(amount) DESC
                    LIMIT 8
                    """,
                    (owner_id,),
                )
                top_products = [
                    {"productName": r[0], "amount": float(r[1] or 0), "quantity": float(r[2] or 0)}
                    for r in cur.fetchall()
                ]

                # Most wasted products (last 30 days, by lost value)
                cur.execute(
                    """
                    SELECT product_name, COALESCE(SUM(amount), 0), COALESCE(SUM(quantity), 0)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date >= CURRENT_DATE - INTERVAL '30 days'
                        AND transaction_type = 'WASTAGE'
                    GROUP BY product_name
                    ORDER BY SUM(amount) DESC
                    LIMIT 8
                    """,
                    (owner_id,),
                )
                top_wasted_products = [
                    {"productName": r[0], "amount": float(r[1] or 0), "quantity": float(r[2] or 0)}
                    for r in cur.fetchall()
                ]

                # Day-wise trend: last 7 days, zero-filled
                cur.execute(
                    """
                    SELECT d::date, COALESCE(SUM(t.amount), 0)
                    FROM generate_series(CURRENT_DATE - INTERVAL '6 days', CURRENT_DATE, INTERVAL '1 day') AS d
                    LEFT JOIN inventory_transactions t
                        ON t.transaction_date = d::date AND t.owner_id = %s AND t.transaction_type = 'SALE'
                    GROUP BY d
                    ORDER BY d
                    """,
                    (owner_id,),
                )
                daily_trend = [
                    {"label": r[0].strftime("%d %b"), "date": r[0].isoformat(), "amount": float(r[1] or 0)}
                    for r in cur.fetchall()
                ]

                # Week-wise trend: last 8 weeks (Mon-start), zero-filled
                cur.execute(
                    """
                    SELECT gs::date, COALESCE(SUM(t.amount), 0)
                    FROM generate_series(
                        date_trunc('week', CURRENT_DATE) - INTERVAL '7 weeks',
                        date_trunc('week', CURRENT_DATE),
                        INTERVAL '1 week'
                    ) AS gs
                    LEFT JOIN inventory_transactions t
                        ON date_trunc('week', t.transaction_date) = gs AND t.owner_id = %s AND t.transaction_type = 'SALE'
                    GROUP BY gs
                    ORDER BY gs
                    """,
                    (owner_id,),
                )
                weekly_trend = [
                    {"label": r[0].strftime("%d %b"), "weekStart": r[0].isoformat(), "amount": float(r[1] or 0)}
                    for r in cur.fetchall()
                ]

                # Month-wise trend: last 6 months, zero-filled
                cur.execute(
                    """
                    SELECT gs::date, COALESCE(SUM(t.amount), 0)
                    FROM generate_series(
                        date_trunc('month', CURRENT_DATE) - INTERVAL '5 months',
                        date_trunc('month', CURRENT_DATE),
                        INTERVAL '1 month'
                    ) AS gs
                    LEFT JOIN inventory_transactions t
                        ON date_trunc('month', t.transaction_date) = gs AND t.owner_id = %s AND t.transaction_type = 'SALE'
                    GROUP BY gs
                    ORDER BY gs
                    """,
                    (owner_id,),
                )
                monthly_trend = [
                    {"label": r[0].strftime("%b %Y"), "monthStart": r[0].isoformat(), "amount": float(r[1] or 0)}
                    for r in cur.fetchall()
                ]

                # Purchase vs Sales (last 30 days)
                cur.execute(
                    """
                    SELECT transaction_type, COALESCE(SUM(amount), 0)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date >= CURRENT_DATE - INTERVAL '30 days'
                        AND transaction_type IN ('SALE', 'PURCHASE')
                    GROUP BY transaction_type
                    """,
                    (owner_id,),
                )
                type_amounts = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
                purchase_vs_sales = {
                    "purchase": type_amounts.get("PURCHASE", 0),
                    "sales": type_amounts.get("SALE", 0),
                }

                # Payment method split (last 30 days, sales only)
                cur.execute(
                    """
                    SELECT payment_method, COALESCE(SUM(amount), 0)
                    FROM inventory_transactions
                    WHERE owner_id = %s AND transaction_date >= CURRENT_DATE - INTERVAL '30 days'
                        AND transaction_type = 'SALE'
                    GROUP BY payment_method
                    """,
                    (owner_id,),
                )
                payment_amounts = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
                payment_method_split = {
                    "cash": payment_amounts.get("CASH", 0),
                    "upi": payment_amounts.get("UPI", 0),
                    "credit": payment_amounts.get("CREDIT", 0),
                }

        return jsonify({
            "topProducts": top_products,
            "topWastedProducts": top_wasted_products,
            "dailyTrend": daily_trend,
            "weeklyTrend": weekly_trend,
            "monthlyTrend": monthly_trend,
            "purchaseVsSales": purchase_vs_sales,
            "paymentMethodSplit": payment_method_split,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _compute_restock_suggestions(cur, owner_id):
    """
    Smart restock suggestions: for each product, estimate daily sales velocity and
    daily wastage rate from the last 14 days of activity, project how many days of
    stock remain, and suggest a restock quantity sized to a short buffer window that
    shrinks when wastage is a large share of depletion (so high-spoilage items get
    bought little-and-often instead of in bulk).
    """
    window_days = 14
    window_start = date.today() - timedelta(days=window_days - 1)

    cur.execute(
        """
        SELECT id, name, unit, stock_qty, low_stock_threshold
        FROM inventory_products
        WHERE owner_id = %s AND status = 'active'
        """,
        (owner_id,),
    )
    products = cur.fetchall()

    suggestions = []
    for pid, name, unit, stock_qty, low_stock_threshold in products:
        cur.execute(
            """
            SELECT transaction_type, COALESCE(SUM(quantity), 0), MIN(transaction_date)
            FROM inventory_transactions
            WHERE product_id = %s AND transaction_type IN ('SALE', 'WASTAGE')
                AND transaction_date >= %s
            GROUP BY transaction_type
            """,
            (pid, window_start),
        )
        rows = {r[0]: {"qty": float(r[1] or 0), "minDate": r[2]} for r in cur.fetchall()}
        sale_qty = rows.get("SALE", {}).get("qty", 0)
        wastage_qty = rows.get("WASTAGE", {}).get("qty", 0)

        if sale_qty <= 0 and wastage_qty <= 0:
            continue

        min_dates = [v["minDate"] for v in rows.values() if v.get("minDate")]
        earliest = min(min_dates) if min_dates else window_start
        days_active = max((date.today() - earliest).days, 1)
        days_active = min(days_active, window_days)

        avg_daily_sales = sale_qty / days_active
        avg_daily_wastage = wastage_qty / days_active
        net_daily_depletion = avg_daily_sales + avg_daily_wastage

        if net_daily_depletion <= 0:
            continue

        stock_qty_f = float(stock_qty or 0)
        days_of_stock_left = stock_qty_f / net_daily_depletion
        wastage_ratio = avg_daily_wastage / net_daily_depletion

        # High wastage share -> shorter buffer (buy little and often); low wastage -> normal buffer.
        if wastage_ratio >= 0.35:
            buffer_days = 1
        elif wastage_ratio >= 0.15:
            buffer_days = 2
        else:
            buffer_days = 3

        target_stock = buffer_days * net_daily_depletion
        suggested_qty = max(0.0, target_stock - stock_qty_f)

        is_low_stock = stock_qty_f <= float(low_stock_threshold or 0)
        needs_restock = days_of_stock_left <= 2 or is_low_stock

        if not needs_restock or suggested_qty <= 0:
            continue

        suggestions.append({
            "productId": str(pid),
            "productName": name,
            "unit": unit,
            "currentStock": round(stock_qty_f, 2),
            "avgDailySales": round(avg_daily_sales, 2),
            "avgDailyWastage": round(avg_daily_wastage, 2),
            "wastageRatio": round(wastage_ratio, 2),
            "daysOfStockLeft": round(days_of_stock_left, 1),
            "suggestedQty": round(suggested_qty, 1),
            "urgency": "critical" if days_of_stock_left <= 1 else "soon",
        })

    suggestions.sort(key=lambda s: s["daysOfStockLeft"])
    return suggestions


@inventory_bp.get("/restock-suggestions")
def restock_suggestions():
    owner_id = get_owner_id()
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                suggestions = _compute_restock_suggestions(cur, owner_id)
        return jsonify({"suggestions": suggestions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
