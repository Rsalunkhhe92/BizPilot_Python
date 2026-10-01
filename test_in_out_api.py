import urllib.request
import json
import sys

BASE_URL = "http://localhost:5000/api/collection"
HEADERS = {
    "Content-Type": "application/json",
    "X-User-Email": "riya@gmail.com",
    "X-User-Type": "customer",
}

def request(method, path, data=None):
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, method=method, headers=HEADERS)
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req.data = body
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        print(f"HTTPError {e.code}: {body}")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"error": body}
    except Exception as e:
        return 500, {"error": str(e)}

def main():
    print("Testing Customer In/Out API...")
    # 1. Get first customer
    status, res = request("GET", "/customers")
    assert status == 200, f"Failed /customers: {status} {res}"
    customers = res.get("customers", [])
    assert len(customers) > 0, "No customers found"
    cust = customers[0]
    cid = cust["id"]
    cust_name = cust["name"]
    initial_due = cust["totalDue"]
    print(f"Target Customer: {cust_name} (ID: {cid}), Initial Due: {initial_due}")

    # 2. Get history before
    status, hist_before = request("GET", f"/customer/{cid}/history")
    assert status == 200, f"Failed /customer/{cid}/history: {status} {hist_before}"
    summary_before = hist_before.get("summary", {})
    paid_before = summary_before.get("totalPaidTillNow", 0)
    given_before = summary_before.get("totalGivenTillNow", 0)
    print(f"Before - Total Paid: {paid_before}, Total Given: {given_before}, Balance: {summary_before.get('currentBalance')}")

    # 3. Add an OUT entry (Cash Out / Given / Loan: 500)
    status, out_res = request("POST", f"/customer/{cid}/entry", {
        "type": "OUT",
        "amount": 500,
        "paymentMethod": "CASH",
        "notes": "Loan Advance Given to customer",
    })
    print(f"POST OUT Entry status: {status}, res: {out_res}")
    assert status == 201, f"Failed POST OUT entry: {out_res}"
    expected_due_after_out = initial_due + 500
    assert abs(out_res["entry"]["newTotalDue"] - expected_due_after_out) < 0.01

    # 4. Add an IN entry (Cash In / Payment Received: 1200)
    status, in_res = request("POST", f"/customer/{cid}/entry", {
        "type": "IN",
        "amount": 1200,
        "paymentMethod": "UPI",
        "transactionRef": "UPI-TEST-9988",
        "notes": "Daily Collection via UPI QR",
    })
    print(f"POST IN Entry status: {status}, res: {in_res}")
    assert status == 201, f"Failed POST IN entry: {in_res}"
    expected_due_after_in = max(0, expected_due_after_out - 1200)
    assert abs(in_res["entry"]["newTotalDue"] - expected_due_after_in) < 0.01

    # 5. Fetch updated history
    status, hist_after = request("GET", f"/customer/{cid}/history")
    assert status == 200, f"Failed /customer/{cid}/history: {status} {hist_after}"
    summary_after = hist_after.get("summary", {})
    paid_after = summary_after.get("totalPaidTillNow", 0)
    given_after = summary_after.get("totalGivenTillNow", 0)
    print(f"After - Total Paid: {paid_after}, Total Given: {given_after}, Balance: {summary_after.get('currentBalance')}")
    assert abs(paid_after - (paid_before + 1200)) < 0.01
    assert abs(given_after - (given_before + 500)) < 0.01

    transactions = hist_after.get("transactions", [])
    print(f"Total Transactions in Passbook: {len(transactions)}")
    assert len(transactions) >= 2
    # Verify latest is the IN entry
    assert transactions[0]["entryType"] == "IN"
    assert transactions[0]["amount"] == 1200
    assert transactions[1]["entryType"] == "OUT"
    assert transactions[1]["amount"] == 500

    print("ALL IN/OUT API TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    main()
