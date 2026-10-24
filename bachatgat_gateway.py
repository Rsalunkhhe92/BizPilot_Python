"""
Mahila Bachat Gat - automatic savings collection through the group's own Razorpay account.

How a payment becomes savings
- The group connects its Razorpay keys (stored encrypted, like the drivers' keys).
- Each member gets a permanent UPI QR in that account (notes carry group/member ids), and the
  treasurer can send her a payment link for a specific amount (usually her current due).
- A captured payment on her QR or link is posted as her savings exactly like counter collection
  (record_savings_deposit: receipt, dues oldest first, ledger), dated the day she paid, into the
  group's primary bank account (Razorpay settles there). Razorpay's fee is booked as a bank
  charge so bank reconciliation matches what actually lands in the bank.
- Payments arrive by webhook (instant, needs the backend on a public URL) and by polling
  (sync endpoint, called by the app). shg_gateway_payments.gateway_payment_id is unique, so a
  payment seen by both is posted once.
- If a payment cannot be posted (member exited, no bank account, group suspended), it is kept
  as Unposted with the reason, and the treasurer can retry after fixing the cause.
"""

import hashlib
import hmac
import json
import time
import urllib.error
from datetime import date, datetime, timedelta, timezone

from flask import g, request

from bachatgat_api import record_savings_deposit
from bachatgat_common import (
    ApiError,
    bachatgat_bp,
    body,
    camel,
    fetch_txn,
    load_bank_account,
    load_group,
    load_member,
    load_policy,
    ok,
    parse_money,
    post_transaction,
    record_event,
    text,
    tx,
)
from bachatgat_finance import FinanceError, ZERO, money
from driver_api import (
    KEY_ID_RE,
    GatewayNotConfigured,
    KeysEncryptionNotConfigured,
    _decrypt,
    _gateway_error_text,
    _gateway_mode,
    _keys_cipher,
    _razorpay_request,
)

IST = timezone(timedelta(hours=5, minutes=30))
SYNC_MIN_SECONDS = 10
_last_sync = {}


def _group_creds(cur, group_id):
    """(key_id, key_secret, mode) of the group's Razorpay account, or None if not connected."""
    cur.execute("SELECT key_id, key_secret_enc, mode FROM shg_group_gateway WHERE group_id = %s", (group_id,))
    row = cur.fetchone()
    if not row:
        return None
    try:
        return row["key_id"], _decrypt(row["key_secret_enc"]), row["mode"]
    except KeysEncryptionNotConfigured:
        raise ApiError(503, "The server cannot read the saved Razorpay keys (DRIVER_KEYS_ENCRYPTION_KEY missing or changed). Reconnect the keys.")


def _require_creds(cur, group_id):
    creds = _group_creds(cur, group_id)
    if not creds:
        raise ApiError(409, "Connect the group's Razorpay account first (Group > Officers & bank > Online payments)")
    return creds


def _gateway_call(method, path, payload=None, creds=None):
    """Razorpay call with errors turned into readable ApiErrors."""
    try:
        return _razorpay_request(method, path, payload, creds=(creds[0], creds[1]))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise ApiError(400, "Razorpay did not accept the group's keys. Reconnect them.")
        raise ApiError(502, _gateway_error_text(e) or f"Razorpay refused the request ({e.code})")
    except urllib.error.URLError:
        raise ApiError(502, "Could not reach Razorpay. Please try again.")
    except GatewayNotConfigured:
        raise ApiError(409, "Razorpay keys are not configured")


def _mask(key_id):
    return f"{key_id[:9]}••••{key_id[-4:]}" if key_id else ""


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------
def _post_gateway_payment(cur, group, gp):
    """Tries to post one stored gateway payment as savings. Updates its status/note."""
    try:
        with cur.connection.transaction():  # savepoint: a failed posting leaves no partial rows
            cur.execute("SELECT * FROM shg_members WHERE id = %s AND group_id = %s FOR UPDATE", (gp["member_id"], group["id"]))
            member = cur.fetchone()
            if not member or member["status"] != "Active":
                raise ApiError(409, "Member is not active - refund the payment or reactivate her, then retry")
            txn_date = gp["paid_at"].astimezone(IST).date()
            cur.execute("SELECT MAX(period_end) AS closed FROM shg_period_closes WHERE group_id = %s", (group["id"],))
            closed = cur.fetchone()["closed"]
            if closed and txn_date <= closed:
                txn_date = date.today()  # books for that day are closed: post it today
            bank = load_bank_account(cur, group["id"], None)
            reference = gp["rrn"] or gp["gateway_payment_id"]
            txn, _alloc, _vol = record_savings_deposit(
                cur, group, member, money(gp["amount"]), txn_date, "UPI", bank["id"],
                narration=f"Online savings via Razorpay {'QR' if gp['source'] == 'QR' else 'payment link'}"
                          + (f" from {gp['payer_vpa']}" if gp["payer_vpa"] else ""),
                reference_number=reference[:80],
            )
            fee_txn_id = None
            if money(gp["fee"]) > 0:
                fee_txn = post_transaction(
                    cur, group, "BANK_CHARGES", money(gp["fee"]), txn_date, "Bank Transfer", bank["id"],
                    narration=f"Razorpay fee for {txn['receipt_number']}", reference_number=gp["gateway_payment_id"],
                )
                fee_txn_id = fee_txn["id"]
            cur.execute(
                "UPDATE shg_gateway_payments SET status = 'Posted', note = '', txn_id = %s, fee_txn_id = %s WHERE id = %s",
                (txn["id"], fee_txn_id, gp["id"]),
            )
        return True
    except (ApiError, FinanceError) as e:
        note = e.message if isinstance(e, ApiError) else str(e)
        cur.execute("UPDATE shg_gateway_payments SET status = 'Unposted', note = %s WHERE id = %s", (note[:500], gp["id"]))
        record_event(cur, group["id"], "GATEWAY_PAYMENT", gp["id"], "NOT_POSTED", details={"reason": note})
        return False


def record_gateway_payment(cur, group, member_id, payment, source):
    """Stores a captured Razorpay payment for a member (once) and posts it.
    Returns True if this call posted it, False if it was already known or could not be posted."""
    payment_id = str(payment.get("id") or "")
    if not payment_id or payment.get("status") != "captured":
        return False
    try:
        amount = money(int(payment.get("amount") or 0) / 100)
        fee = money(int(payment.get("fee") or 0) / 100)
        paid_at = datetime.fromtimestamp(int(payment.get("created_at")), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return False
    if amount <= 0:
        return False
    rrn = str((payment.get("acquirer_data") or {}).get("rrn") or "")[:40]
    cur.execute(
        """INSERT INTO shg_gateway_payments (group_id, member_id, gateway_payment_id, source, amount, fee, payer_vpa, rrn, paid_at, status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Unposted')
           ON CONFLICT (gateway_payment_id) DO NOTHING RETURNING *""",
        (group["id"], member_id, payment_id, source, amount, fee, str(payment.get("vpa") or "")[:120], rrn, paid_at),
    )
    gp = cur.fetchone()
    if not gp:
        return False
    record_event(cur, group["id"], "GATEWAY_PAYMENT", gp["id"], "RECEIVED",
                 details={"paymentId": payment_id, "memberId": member_id, "amount": amount, "source": source})
    return _post_gateway_payment(cur, group, gp)


def sync_group(cur, group, force=False):
    """Pulls captured payments on the group's member QRs and open payment links."""
    now = time.time()
    if not force and now - _last_sync.get(group["id"], 0) < SYNC_MIN_SECONDS:
        return {"posted": 0, "skipped": True}
    _last_sync[group["id"]] = now
    creds = _group_creds(cur, group["id"])
    if not creds:
        return {"posted": 0, "connected": False}
    posted, errors = 0, []
    cur.execute("SELECT member_id, qr_id FROM shg_member_gateway WHERE group_id = %s AND mode = %s", (group["id"], creds[2]))
    for row in cur.fetchall():
        try:
            items = _gateway_call("GET", f"/payments/qr_codes/{row['qr_id']}/payments?count=100", creds=creds).get("items", [])
        except ApiError as e:
            errors.append(e.message)
            continue
        for p in items:
            posted += 1 if record_gateway_payment(cur, group, row["member_id"], p, "QR") else 0
    cur.execute("SELECT id, member_id, link_id FROM shg_payment_links WHERE group_id = %s AND status = 'Open' AND mode = %s",
                (group["id"], creds[2]))
    for link in cur.fetchall():
        try:
            info = _gateway_call("GET", f"/payment_links/{link['link_id']}", creds=creds)
        except ApiError as e:
            errors.append(e.message)
            continue
        status = str(info.get("status") or "")
        if status in ("paid", "partially_paid"):
            for pay in info.get("payments") or []:
                if pay.get("status") != "captured" or not pay.get("payment_id"):
                    continue
                try:
                    payment = _gateway_call("GET", f"/payments/{pay['payment_id']}", creds=creds)
                except ApiError as e:
                    errors.append(e.message)
                    continue
                posted += 1 if record_gateway_payment(cur, group, link["member_id"], payment, "LINK") else 0
            if status == "paid":
                cur.execute("UPDATE shg_payment_links SET status = 'Paid' WHERE id = %s", (link["id"],))
        elif status in ("cancelled", "expired"):
            cur.execute("UPDATE shg_payment_links SET status = %s WHERE id = %s", (status.title(), link["id"]))
    return {"posted": posted, "errors": errors[:3], "connected": True}


# ---------------------------------------------------------------------------
# Group gateway settings
# ---------------------------------------------------------------------------
def _gateway_status(cur, group_id):
    cur.execute("SELECT key_id, mode, webhook_secret_enc IS NOT NULL AS has_webhook, connected_at FROM shg_group_gateway WHERE group_id = %s", (group_id,))
    row = cur.fetchone()
    cur.execute(
        """SELECT (SELECT COUNT(*) FROM shg_member_gateway WHERE group_id = %s) AS member_qrs,
                  (SELECT COUNT(*) FROM shg_gateway_payments WHERE group_id = %s AND status = 'Unposted') AS unposted,
                  (SELECT COUNT(*) FROM shg_gateway_payments WHERE group_id = %s AND status = 'Posted') AS posted""",
        (group_id, group_id, group_id),
    )
    counts = cur.fetchone()
    return {
        "connected": bool(row),
        "keyIdMasked": _mask(row["key_id"]) if row else "",
        "mode": row["mode"] if row else None,
        "hasWebhookSecret": bool(row and row["has_webhook"]),
        "connectedAt": row["connected_at"] if row else None,
        "webhookUrl": f"{request.url_root.rstrip('/')}/api/bachatgat/gateway-webhook/{group_id}",
        "memberQrs": counts["member_qrs"],
        "unposted": counts["unposted"],
        "posted": counts["posted"],
    }


@bachatgat_bp.get("/groups/<int:group_id>/gateway")
def get_gateway(group_id):
    with tx() as cur:
        load_group(cur, group_id)
        return ok({"gateway": _gateway_status(cur, group_id)})


@bachatgat_bp.put("/groups/<int:group_id>/gateway")
def connect_gateway(group_id):
    payload = body()
    key_id = text(payload, "keyId", 40, required=True, label="Razorpay Key Id")
    key_secret = text(payload, "keySecret", 100, required=True, label="Razorpay Key Secret")
    webhook_secret = text(payload, "webhookSecret", 200)
    if not KEY_ID_RE.match(key_id):
        raise ApiError(422, "Enter a valid Razorpay Key Id (it starts with rzp_test_ or rzp_live_)")
    try:
        cipher = _keys_cipher()
    except KeysEncryptionNotConfigured:
        raise ApiError(503, "The server is not set up to store payment keys (DRIVER_KEYS_ENCRYPTION_KEY is missing)")
    creds = (key_id, key_secret, _gateway_mode((key_id, key_secret)))
    _gateway_call("GET", "/payments?count=1", creds=creds)  # proves the keys before storing them
    with tx() as cur:
        group = load_group(cur, group_id, lock=True)
        if group["status"] == "Closed":
            raise ApiError(409, "Group is closed")
        cur.execute("SELECT key_id FROM shg_group_gateway WHERE group_id = %s", (group_id,))
        previous = cur.fetchone()
        cur.execute(
            """INSERT INTO shg_group_gateway (group_id, key_id, key_secret_enc, webhook_secret_enc, mode)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (group_id) DO UPDATE SET key_id = EXCLUDED.key_id, key_secret_enc = EXCLUDED.key_secret_enc,
                   webhook_secret_enc = COALESCE(EXCLUDED.webhook_secret_enc, shg_group_gateway.webhook_secret_enc),
                   mode = EXCLUDED.mode, updated_at = NOW()""",
            (group_id, key_id, cipher.encrypt(key_secret.encode()).decode(),
             cipher.encrypt(webhook_secret.encode()).decode() if webhook_secret else None, creds[2]),
        )
        if previous and previous["key_id"] != key_id:
            # QRs and links live in the old Razorpay account: members get new ones on demand.
            cur.execute("DELETE FROM shg_member_gateway WHERE group_id = %s", (group_id,))
            cur.execute("UPDATE shg_payment_links SET status = 'Cancelled' WHERE group_id = %s AND status = 'Open'", (group_id,))
        record_event(cur, group_id, "GATEWAY", group_id, "CONNECTED" if not previous else "KEYS_UPDATED",
                     details={"keyId": _mask(key_id), "mode": creds[2], "webhookSecret": bool(webhook_secret)})
        return ok({"gateway": _gateway_status(cur, group_id)})


@bachatgat_bp.delete("/groups/<int:group_id>/gateway")
def disconnect_gateway(group_id):
    with tx() as cur:
        load_group(cur, group_id, lock=True)
        cur.execute("DELETE FROM shg_member_gateway WHERE group_id = %s", (group_id,))
        cur.execute("UPDATE shg_payment_links SET status = 'Cancelled' WHERE group_id = %s AND status = 'Open'", (group_id,))
        cur.execute("DELETE FROM shg_group_gateway WHERE group_id = %s", (group_id,))
        record_event(cur, group_id, "GATEWAY", group_id, "DISCONNECTED")
        return ok({"gateway": _gateway_status(cur, group_id)})


@bachatgat_bp.post("/groups/<int:group_id>/gateway/sync")
def sync_gateway(group_id):
    with tx() as cur:
        group = load_group(cur, group_id, lock=True)
        result = sync_group(cur, group, force=request.args.get("force") == "1")
    return ok(result)


@bachatgat_bp.get("/groups/<int:group_id>/gateway/payments")
def list_gateway_payments(group_id):
    with tx() as cur:
        load_group(cur, group_id)
        params = [group_id]
        sql = """SELECT gp.*, m.full_name AS member_name, m.member_number, t.receipt_number
                 FROM shg_gateway_payments gp JOIN shg_members m ON m.id = gp.member_id
                 LEFT JOIN shg_transactions t ON t.id = gp.txn_id WHERE gp.group_id = %s"""
        if request.args.get("status"):
            sql += " AND gp.status = %s"
            params.append(request.args["status"])
        cur.execute(sql + " ORDER BY gp.paid_at DESC LIMIT 200", params)
        return ok({"payments": [camel(r) for r in cur.fetchall()]})


@bachatgat_bp.post("/gateway-payments/<int:gp_id>/retry")
def retry_gateway_payment(gp_id):
    with tx() as cur:
        cur.execute(
            "SELECT gp.* FROM shg_gateway_payments gp JOIN shg_groups gr ON gr.id = gp.group_id WHERE gp.id = %s AND gr.owner_id = %s FOR UPDATE OF gp",
            (gp_id, g.owner_id),
        )
        gp = cur.fetchone()
        if not gp:
            raise ApiError(404, "Payment not found")
        if gp["status"] == "Posted":
            raise ApiError(409, "Already posted")
        group = load_group(cur, gp["group_id"], lock=True)
        posted = _post_gateway_payment(cur, group, gp)
        cur.execute("SELECT * FROM shg_gateway_payments WHERE id = %s", (gp_id,))
        out = camel(cur.fetchone())
    if not posted:
        raise ApiError(409, out["note"] or "Could not post the payment")
    return ok({"payment": out})


# ---------------------------------------------------------------------------
# Member QR and payment links
# ---------------------------------------------------------------------------
@bachatgat_bp.post("/members/<int:member_id>/payment-qr")
def member_payment_qr(member_id):
    with tx() as cur:
        member = load_member(cur, member_id, lock=True)
        if member["status"] != "Active":
            raise ApiError(409, "Only active members get a payment QR")
        group = load_group(cur, member["group_id"])
        creds = _require_creds(cur, group["id"])
        cur.execute("SELECT * FROM shg_member_gateway WHERE member_id = %s", (member_id,))
        existing = cur.fetchone()
        if existing and existing["mode"] == creds[2]:
            return ok({"qrImageUrl": existing["qr_image_url"], "mode": existing["mode"], "created": False})
        qr = _gateway_call("POST", "/payments/qr_codes", {
            "type": "upi_qr",
            "name": f"{member['full_name']}"[:40],
            "usage": "multiple_use",
            "fixed_amount": False,
            "description": f"Savings - {group['name_en']} #{member['member_number']}"[:255],
            "notes": {"kind": "shg_savings", "shg_group_id": str(group["id"]), "shg_member_id": str(member_id)},
        }, creds=creds)
        cur.execute(
            """INSERT INTO shg_member_gateway (member_id, group_id, qr_id, qr_image_url, mode) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (member_id) DO UPDATE SET qr_id = EXCLUDED.qr_id, qr_image_url = EXCLUDED.qr_image_url,
                   mode = EXCLUDED.mode, created_at = NOW()""",
            (member_id, group["id"], qr["id"], qr.get("image_url"), creds[2]),
        )
        record_event(cur, group["id"], "MEMBER", member_id, "PAYMENT_QR_CREATED", details={"mode": creds[2]})
        return ok({"qrImageUrl": qr.get("image_url"), "mode": creds[2], "created": True}, 201)


@bachatgat_bp.post("/members/<int:member_id>/payment-link")
def member_payment_link(member_id):
    payload = body()
    with tx() as cur:
        member = load_member(cur, member_id, lock=True)
        if member["status"] != "Active":
            raise ApiError(409, "Only active members can be sent a payment link")
        group = load_group(cur, member["group_id"])
        creds = _require_creds(cur, group["id"])
        amount = parse_money(payload, "amount", label="Amount")
        if amount <= 0:
            cur.execute(
                """SELECT COALESCE(SUM(due_amount - paid_amount), 0) AS due FROM shg_savings_dues
                   WHERE member_id = %s AND status IN ('Pending', 'Partial') AND due_date <= CURRENT_DATE""",
                (member_id,),
            )
            amount = money(cur.fetchone()["due"])
            if amount <= 0:
                amount = money(load_policy(cur, group["id"])["savings_amount"])
        if amount <= 0:
            raise ApiError(422, "Enter the amount for the link")
        link = _gateway_call("POST", "/payment_links", {
            "amount": int(amount * 100),
            "currency": "INR",
            "accept_partial": False,
            "description": f"Savings - {group['name_en']}"[:2048],
            "customer": {"name": member["full_name"][:50], "contact": f"+91{member['mobile']}"},
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "reference_id": f"shg{member_id}-{int(time.time())}",
            "notes": {"kind": "shg_savings", "shg_group_id": str(group["id"]), "shg_member_id": str(member_id)},
        }, creds=creds)
        cur.execute(
            """INSERT INTO shg_payment_links (group_id, member_id, link_id, short_url, amount, mode)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (group["id"], member_id, link["id"], link.get("short_url") or "", amount, creds[2]),
        )
        row = cur.fetchone()
        record_event(cur, group["id"], "MEMBER", member_id, "PAYMENT_LINK_SENT", details={"amount": amount})
        return ok({"link": camel(row), "groupName": group["name_en"]}, 201)


# ---------------------------------------------------------------------------
# Webhook (no login: Razorpay calls it; authenticated by the group's webhook secret)
# ---------------------------------------------------------------------------
@bachatgat_bp.post("/gateway-webhook/<int:group_id>")
def gateway_webhook(group_id):
    raw = request.get_data()
    with tx() as cur:
        cur.execute("SELECT * FROM shg_groups WHERE id = %s", (group_id,))
        group = cur.fetchone()
        cur.execute("SELECT webhook_secret_enc FROM shg_group_gateway WHERE group_id = %s", (group_id,))
        gw = cur.fetchone()
        if not group or not gw or not gw["webhook_secret_enc"]:
            return ok({"error": "Webhook not configured"}, 503)
        try:
            secret = _decrypt(gw["webhook_secret_enc"])
        except KeysEncryptionNotConfigured:
            return ok({"error": "Webhook secret unreadable"}, 503)
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("X-Razorpay-Signature", "")):
            return ok({"error": "Invalid signature"}, 400)
        try:
            event = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return ok({"error": "Invalid payload"}, 400)
        g.owner_id = group["owner_id"]
        entities = event.get("payload") or {}
        payment = (entities.get("payment") or {}).get("entity") or {}
        kind = str(event.get("event") or "")
        handled = False
        if kind == "qr_code.credited":
            qr_id = ((entities.get("qr_code") or {}).get("entity") or {}).get("id")
            cur.execute("SELECT member_id FROM shg_member_gateway WHERE qr_id = %s AND group_id = %s", (qr_id, group_id))
            row = cur.fetchone()
            if row:
                record_gateway_payment(cur, group, row["member_id"], payment, "QR")
                handled = True
        elif kind == "payment_link.paid":
            link_id = ((entities.get("payment_link") or {}).get("entity") or {}).get("id")
            cur.execute("SELECT id, member_id FROM shg_payment_links WHERE link_id = %s AND group_id = %s", (link_id, group_id))
            row = cur.fetchone()
            if row:
                record_gateway_payment(cur, group, row["member_id"], payment, "LINK")
                cur.execute("UPDATE shg_payment_links SET status = 'Paid' WHERE id = %s", (row["id"],))
                handled = True
    # Always 200 for a correctly signed event, so Razorpay does not retry ones we do not use.
    return ok({"ok": True, "handled": handled})
