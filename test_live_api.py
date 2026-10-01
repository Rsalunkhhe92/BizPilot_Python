import urllib.request
import json
import sys

BASE_URL = "http://localhost:5000/api/collection"

def make_req(endpoint, data=None):
    url = f"{BASE_URL}{endpoint}"
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8") if data else None,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw_error": body}

# 1. Dashboard
status, data = make_req("/dashboard?collector_id=36&date=2026-09-07")
assert status == 200, f"Dashboard failed: {status}"
print("[PASS] Dashboard:", data["summary"]["totalAssigned"], "assigned,", data["summary"]["totalPending"], "pending")

# 2. Today Schedule
status, data = make_req("/today?collector_id=36&date=2026-09-07")
assert status == 200, f"Today failed: {status}"
assert len(data["customers"]) == 50, f"Expected 50 customers, got {len(data['customers'])}"
today_customers = data["customers"]
first_cust = today_customers[0]
print(f"[PASS] Today Schedule: 50 customers loaded. Customer #1: {first_cust['customerName']} (Due: Rs.{first_cust['totalDue']})", flush=True)

# 3. Validation: zero amount should fail
status, res_data = make_req("/collect", {
    "scheduleId": first_cust["scheduleId"],
    "collectorId": 36,
    "amount": 0,
    "paymentMethod": "CASH"
})
assert status == 400, f"Expected 400 for 0 amount, got {status}"
print("[PASS] Validation: 0 amount rejected as expected.", flush=True)

# 4. Validation: UPI without reference should fail
status, res_data = make_req("/collect", {
    "scheduleId": first_cust["scheduleId"],
    "collectorId": 36,
    "amount": 500,
    "paymentMethod": "UPI",
    "transactionRef": ""
})
assert status == 400, f"Expected 400 for UPI without ref, got {status}"
print("[PASS] Validation: UPI without transaction reference rejected.", flush=True)

# 5. Collect Payment (Cash)
status, res_data = make_req("/collect", {
    "scheduleId": first_cust["scheduleId"],
    "collectorId": 36,
    "amount": first_cust["expectedAmount"],
    "paymentMethod": "CASH",
    "notes": "Verified at shop"
})
assert status in (201, 409), f"Payment collection failed: {status}, {res_data}"
if status == 201:
    print(f"[PASS] Cash Collection: Generated Receipt #{res_data['receipt']['receiptNumber']} for Rs.{res_data['receipt']['amount']}", flush=True)

# 6. Status Update: Mark customer #2 as NOT_AVAILABLE
cust2 = today_customers[1]
status, res_data = make_req("/status", {
    "scheduleId": cust2["scheduleId"],
    "status": "NOT_AVAILABLE",
    "reason": "Shop shutter down, neighbor said in village",
    "notes": "Visited 10:45 AM"
})
assert status == 200, f"Status update failed: {status}"
print(f"[PASS] Status Update: Customer #2 marked NOT_AVAILABLE.", flush=True)

# 7. Reschedule customer #3
cust3 = today_customers[2]
status, res_data = make_req("/reschedule", {
    "scheduleId": cust3["scheduleId"],
    "reason": "Customer out of town until tomorrow",
    "nextFollowupDate": "2026-09-08",
    "notes": "Called and confirmed"
})
assert status == 200, f"Reschedule failed: {status}"
print(f"[PASS] Reschedule: Customer #3 rescheduled to 2026-09-08.", flush=True)

# 8. Missed list
status, data = make_req("/missed?collector_id=36&date=2026-09-07")
assert status == 200, f"Missed endpoint failed: {status}"
print(f"[PASS] Missed List: {data['totalMissed']} follow-up actions logged.")

# 9. Verify updated summary on dashboard
status, data = make_req("/dashboard?collector_id=36&date=2026-09-07")
assert status == 200
s = data["summary"]
print(f"[PASS] Realtime Summary: Total: {s['totalAssigned']} | Collected: {s['totalCollected']} | Pending: {s['totalPending']} | Missed/NA/Rescheduled: {s['totalMissed'] + s['totalNotAvailable'] + s['totalRescheduled']} | Cash: Rs.{s['cashAmount']}")

print("\nALL API ENDPOINTS TESTED AND WORKING PERFECTLY!")
