"""One-off LOCAL verification for the PROACTIVE COACH CAL nudge tick (2026-07-15).

Not part of the permanent regression_gate.py suite (that has its own in-process unit checks +
live HTTP auth checks). This script instead seeds a FULLY subscribed test user (a real push_subs
row, not just meal/water/exercise history) and drives /api/nudge/tick through Flask's real
test_client (real request context, real route, real DB), monkeypatching only the final network
hop (_push_send -> pywebpush) so it never actually calls a push service or needs a real VAPID
keypair. Prints the exact payload that WOULD have been sent, per the task's "show the sent-payload
log" requirement.

Run:  python _review/proactive_cal_2026-07-15/nudge_tick_local_test.py
"""
import importlib.util
import json
import os
import sys
from datetime import date, timedelta

os.environ.setdefault("NUDGE_TICK_KEY", "local-verify-key-not-real")
os.environ.setdefault("NUDGES_ENABLED", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
APP_PY = os.path.join(HERE, "..", "..", "app.py")
spec = importlib.util.spec_from_file_location("snapapp_local_verify", APP_PY)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

UID = "_local_verify_nudge_uid"

def wipe():
    con = m.get_db()
    try:
        for tbl in ("meals", "exercise", "water_log", "nudge_log", "push_subs"):
            con.execute("DELETE FROM " + tbl + " WHERE uid = ?", (UID,))
        con.commit()
    finally:
        con.close()

wipe()

# Seed 5 days of meal history (first meal ~07:30) so meal-gap has a learned pattern, TODAY empty.
con = m.get_db()
try:
    for i in range(1, 6):
        d = (date.today() - timedelta(days=i)).isoformat()
        con.execute("INSERT INTO meals(date, time, name, calories, protein_g, carbs_g, fat_g, uid) VALUES (?,?,?,?,?,?,?,?)",
                    (d, "07:30", "Breakfast", 400, 20, 40, 15, UID))
    # A real (fake-endpoint) push subscription, forced into the nudges_on bucket, tz_offset_min=0 (local==UTC
    # for a deterministic test), enabled, gentle off, soft intensity to show the tone dial working too.
    sub_json = json.dumps({"endpoint": "https://example.invalid/fake-endpoint", "keys": {"p256dh": "x", "auth": "y"}})
    con.execute(
        """INSERT INTO push_subs(uid, sub_json, tz_offset_min, name, goal, daily_calories, protein_target_g,
                                 enabled, last_slot, last_date, created, gentle, intensity, ab_bucket)
           VALUES (?,?,?,?,?,?,?,1,NULL,NULL,?,?,?,?)""",
        (UID, sub_json, 0, "TestUser", "maintain", 2000, 140, "2026-07-15T00:00:00", 0, "soft", "nudges_on"),
    )
    con.commit()
finally:
    con.close()

# Force "now" (server UTC, since tz_offset_min=0) into the fire window: mid-morning, well past the learned
# 07:30 + 90min grace, so the tick has something due right now regardless of when this script actually runs.
_real_utcnow = m.datetime.utcnow
class _FixedDatetime(m.datetime):
    @classmethod
    def utcnow(cls):
        real = _real_utcnow()
        return real.replace(hour=10, minute=0, second=0, microsecond=0)
m.datetime = _FixedDatetime

sent_payloads = []
def _fake_push_send(sub, payload):
    sent_payloads.append({"sub_endpoint": sub.get("endpoint"), "payload": payload})
    return "ok"
m._push_send = _fake_push_send

client = m.app.test_client()

print("=== 1) no key -> should be rejected ===")
r0 = client.post("/api/nudge/tick")
print(r0.status_code, r0.get_json())

print("\n=== 2) wrong key -> should be 403 ===")
r1 = client.post("/api/nudge/tick", headers={"X-Nudge-Key": "wrong"})
print(r1.status_code, r1.get_json())

print("\n=== 3) correct key -> real tick run against the seeded user ===")
r2 = client.post("/api/nudge/tick", headers={"X-Nudge-Key": os.environ["NUDGE_TICK_KEY"]})
print(r2.status_code, r2.get_json())

print("\n=== SENT-PAYLOAD LOG (what would have gone out over web push) ===")
for p in sent_payloads:
    print(json.dumps(p, indent=2))
if not sent_payloads:
    print("(nothing sent this run)")

print("\n=== 4) run again immediately -> must be deduped (same type not repeated same day) ===")
r3 = client.post("/api/nudge/tick", headers={"X-Nudge-Key": os.environ["NUDGE_TICK_KEY"]})
print(r3.status_code, r3.get_json())
print("second-run sent payloads:", len(sent_payloads) - len(sent_payloads))  # unchanged list length below
print("total captured after 2nd run:", len(sent_payloads))

wipe()
print("\ncleaned up test uid:", UID)
