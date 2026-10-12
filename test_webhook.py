"""
Sends a signed, made-up Razorpay `qr_code.credited` event to the webhook endpoint, the
same way Razorpay would, so the webhook path can be tested without a real payment.

    python test_webhook.py                         # local backend, first driver with a QR
    python test_webhook.py --url https://your-backend.example.com --owner 12 --amount 85
    python test_webhook.py --repeat                # send the same event twice (duplicate check)

Needs DATABASE_URL and RAZORPAY_WEBHOOK_SECRET in backend/.env (the secret must match the
server being tested). The payment id starts with pay_TEST, so it is hidden once live keys
are in use.
"""

import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

import psycopg
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:5000", help="backend base URL")
    parser.add_argument("--owner", type=int, help="driver's user id (default: first driver with a QR)")
    parser.add_argument("--amount", type=float, default=50, help="amount in rupees")
    parser.add_argument("--repeat", action="store_true", help="deliver the same event a second time")
    args = parser.parse_args()

    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise SystemExit("RAZORPAY_WEBHOOK_SECRET is not set in backend/.env")

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT owner_id, gateway_qr_id FROM driver_merchant_accounts "
            "WHERE gateway_qr_id IS NOT NULL AND (%s::bigint IS NULL OR owner_id = %s) ORDER BY owner_id LIMIT 1",
            (args.owner, args.owner),
        )
        row = cur.fetchone()
    if not row:
        raise SystemExit("No driver with a merchant QR found. Set up a merchant account in the app first.")
    owner_id, qr_id = row

    stamp = int(time.time())
    payment_id = f"pay_TEST{stamp}"
    body = json.dumps({
        "entity": "event",
        "event": "qr_code.credited",
        "payload": {
            "qr_code": {"entity": {"id": qr_id}},
            "payment": {"entity": {
                "id": payment_id,
                "amount": int(round(args.amount * 100)),
                "status": "captured",
                "method": "upi",
                "vpa": "webhook.test@okaxis",
                "acquirer_data": {"rrn": str(stamp).rjust(12, "0")},
                "created_at": stamp,
            }},
        },
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
        "X-Razorpay-Event-Id": f"evt_TEST{stamp}",
    }
    endpoint = args.url.rstrip("/") + "/api/driver/razorpay-webhook"

    print(f"Driver {owner_id}, QR {qr_id}, payment {payment_id}, Rs {args.amount}")
    for attempt in range(2 if args.repeat else 1):
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                print(f"  delivery {attempt + 1}: {response.status} {response.read().decode()}")
        except urllib.error.HTTPError as e:
            print(f"  delivery {attempt + 1}: {e.code} {e.read().decode()}")
        except urllib.error.URLError as e:
            raise SystemExit(f"Could not reach {endpoint} ({e.reason}). Is the backend running? Start it with: python app.py")

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, attempts, error FROM gateway_webhook_events WHERE event_id = %s", (f"evt_TEST{stamp}",)
        )
        print("  event log row:", cur.fetchone())
        cur.execute("SELECT amount, trip_id FROM driver_merchant_payments WHERE gateway_payment_id = %s", (payment_id,))
        print("  payment row:  ", cur.fetchone())


if __name__ == "__main__":
    main()
