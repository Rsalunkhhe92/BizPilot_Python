import time
t0 = time.time()
print("Starting...", flush=True)
from app import app
print("App imported in", round(time.time() - t0, 2), "s", flush=True)

with app.test_request_context('/?collector_id=36&date=2026-09-07'):
    from collection_api import get_dashboard
    print("Calling get_dashboard...", flush=True)
    res = get_dashboard()
    print("Result in", round(time.time() - t0, 2), "s:", res.get_json()["summary"], flush=True)
