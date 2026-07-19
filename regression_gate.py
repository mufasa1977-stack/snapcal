#!/usr/bin/env python3
"""
SnapCal REGRESSION GATE  (built 2026-06-21 after the map kept disappearing)

Drives the REAL app in headless Chrome (via Playwright, channel="chrome" — uses the installed
Chrome, no download) and asserts every flow we've locked in still works — INCLUDING the failure
paths, because "works on my fast localhost" is exactly how the map-gone bug shipped.

This turns the prose rule "never regress / always degrade-don't-disappear" into EXECUTION: a gate
that fails loudly the moment a locked-in behavior breaks.

Run:   python regression_gate.py
Exit:  0 = all green   |   1 = a regression   |   2 = couldn't start the app

Locked-in checks (each = a real regression Tariq hit at least once):
  1. Eat-Out near-me MAP renders (canvas + ready)
  2. Healthy<->Treat METER present on Eat Out
  3. Near-me LIST shows DISTANCES on every row
  4. Chain grid flows: a "Near you" band with distances
  5. *** MAP RENDERS EVEN WHEN THE FOOD LOOKUP FAILS *** (the map-gone guard)
  6. Stores METER present
  7. Store list shows DISTANCES on every row
  8. No no-food stores leak in (Ross / Burlington / Boscov's ...)
  9. Tapping a store opens the COACH SHEET (picks + Get directions), not instant directions
 10. No JS console / page errors
"""
import os
import sys
import time
import subprocess
import urllib.request
import urllib.error

# Windows consoles default stdout to the local codepage (cp1252 etc), which can't encode every
# character a check's detail string might carry (e.g. a fullwidth "+" from UI copy). Force UTF-8
# with a safe fallback so a detail string never crashes the gate mid-run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:5177"
LAT, LNG = 40.2452, -75.6496          # Pottstown — chain-dense enough to exercise near + far bands
HERE = os.path.dirname(os.path.abspath(__file__))

# PROACTIVE COACH CAL nudge tick (2026-07-15): arm a throwaway key BEFORE ensure_server() spawns the app.py
# subprocess so it inherits these and /api/nudge/tick is actually reachable to gate. A real deploy needs
# Tariq to set NUDGE_TICK_KEY in Render's dashboard — this is gate-only, never a real secret.
os.environ.setdefault("NUDGE_TICK_KEY", "gate-test-nudge-key-not-real")
os.environ.setdefault("NUDGES_ENABLED", "1")

# Deterministic fixtures: /api/nearby is backed by Overpass, which is slow + flaky (18s+). The gate must
# test OUR rendering, not Overpass's mood — so we intercept /api/nearby and return known data.
import json
FOOD_FIXTURE = {
    "matched": [
        {"chain": "McDonald's", "dist_m": 700, "lat": 40.2490, "lng": -75.6520},
        {"chain": "Wendy's", "dist_m": 800, "lat": 40.2470, "lng": -75.6540},
        {"chain": "Chipotle", "dist_m": 900, "lat": 40.2500, "lng": -75.6560},
        {"chain": "Panera Bread", "dist_m": 1000, "lat": 40.2510, "lng": -75.6570},
        {"chain": "Subway", "dist_m": 1200, "lat": 40.2520, "lng": -75.6600},
        {"chain": "Chick-fil-A", "dist_m": 6200, "lat": 40.2900, "lng": -75.6300},
    ],
    "nearby": [], "center": {"lat": LAT, "lng": LNG},
}
STORE_FIXTURE = {
    "stores": [
        {"name": "Wawa", "dist_m": 600, "lat": 40.2470, "lng": -75.6510, "shop": "convenience"},
        {"name": "Aldi", "dist_m": 1400, "lat": 40.2520, "lng": -75.6560, "shop": "supermarket"},
        {"name": "Walmart Supercenter", "dist_m": 1800, "lat": 40.2540, "lng": -75.6600, "shop": "supermarket"},
        {"name": "Giant", "dist_m": 2200, "lat": 40.2400, "lng": -75.6400, "shop": "supermarket"},
    ],
    "center": {"lat": LAT, "lng": LNG},
}
def _route_nearby(route):
    body = STORE_FIXTURE if "kind=store" in route.request.url else FOOD_FIXTURE
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


# Deterministic USDA name-search response for the "add an item the camera missed" flow — a query
# containing 'quickfail' simulates a no-match so the gate can exercise the manual quick-add fallback too.
def _route_nutrition(route):
    url = route.request.url
    if "quickfail" in url:
        route.fulfill(status=404, content_type="application/json", body=json.dumps({"error": "not_found", "query": "x"}))
        return
    route.fulfill(status=200, content_type="application/json", body=json.dumps({
        "food": "Cheddar cheese", "fdcId": 1, "dataType": "SR Legacy",
        "serving": "per 100 g (3.5 oz)", "source": "USDA FoodData Central", "accuracy_tier": "VERIFIED",
        "nutrients": {"calories": 403, "protein_g": 25, "carbs_g": 1.3, "fat_g": 33,
                      "fiber_g": 0, "sugar_g": 0.5, "sat_fat_g": 21, "sodium_mg": 621}
    }))

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   -> " + detail) if detail else ""))


def server_up():
    try:
        urllib.request.urlopen(BASE + "/", timeout=3)
        return True
    except Exception:
        return False


def server_is_fresh():
    """STALE-SERVER GUARD (2026-07-19 failure class: zombie processes squatted the port and the gate
       'verified' OLD code twice). Two probes: (1) served index.html byte-identical to disk (frontend);
       (2) /api/version app_mtime matches app.py's disk mtime (BACKEND — v2 2026-07-19: the frontend
       probe alone passed zombies because Flask serves static from disk; a zombie's PYTHON code is what's
       stale, detectable only by the mtime the process captured at import)."""
    try:
        served = urllib.request.urlopen(BASE + "/", timeout=5).read()
        disk = open(os.path.join(HERE, "static", "index.html"), "rb").read()
        if served != disk:
            return False
        import json as _json
        v = _json.loads(urllib.request.urlopen(BASE + "/api/version", timeout=5).read())
        proc_mtime = v.get("app_mtime")
        disk_mtime = int(os.path.getmtime(os.path.join(HERE, "app.py")))
        # old processes predate the app_mtime field entirely -> None -> stale.
        return proc_mtime == disk_mtime
    except Exception:
        return False


def ensure_server():
    if server_up():
        if server_is_fresh():
            return None
        # Zombie on our port serving stale code -> kill every listener by PID, then boot fresh.
        print("  [stale-server guard] port answers but serves STALE bytes — killing squatters")
        port = BASE.rsplit(":", 1)[-1].rstrip("/")
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=15).stdout
            pids = {line.split()[-1] for line in out.splitlines()
                    if (":" + port) in line and "LISTENING" in line}
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=10)
        except Exception:
            pass
    proc = subprocess.Popen([sys.executable, "app.py"], cwd=HERE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if server_up():
            return proc
        time.sleep(0.5)
    return proc


def main():
    ensure_server()
    if not server_up():
        print("FATAL: SnapCal server not reachable at " + BASE)
        return 2

    # ---- allergy/diet SAFETY (in-process, no Gemini/browser): the scan must NEVER suggest an allergen,
    #      must WARN when a logged food contains one, and the meal plan must FLAG leaked allergens.
    #      Added 2026-06-25 after the scan suggested fruit to a fruit-allergic user. ----
    try:
        import importlib.util
        _spec = importlib.util.spec_from_file_location("snapapp_gate", os.path.join(os.path.dirname(__file__), "app.py"))
        _m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_m)
        scan = _m.normalize_analysis(
            {"items": [{"name": "White rice", "calories": 200}], "total": {"calories": 200},
             "coach_tip": "Add fruit.",
             "swaps": [{"from": "rice", "to": "Mixed berries", "why": "fiber"},
                       {"from": "rice", "to": "Quinoa", "why": "protein"}]},
            allergies=["fruit", "tree nuts"], diet="")
        check("allergy-safe scan: allergen swap dropped, safe swap kept",
              all("berr" not in s["to"].lower() for s in scan["swaps"]) and any("quinoa" in s["to"].lower() for s in scan["swaps"]),
              "kept=" + str([s["to"] for s in scan["swaps"]]))
        warn = _m.normalize_analysis({"items": [{"name": "Greek yogurt with honey", "calories": 150}], "total": {"calories": 150}, "swaps": []},
                                     allergies=["dairy"])["allergen_warning"]
        check("allergy-safe scan: logged allergen (yogurt/dairy) raises a WARNING",
              "dairy" in warn.lower() and "yogurt" in warn.lower(), warn[:60])
        clean = _m.normalize_analysis({"items": [{"name": "rice", "calories": 200}], "total": {"calories": 200},
                                       "swaps": [{"from": "a", "to": "Mixed berries", "why": "x"}]}, allergies=[], diet="")
        check("allergy-safe scan: no allergies set -> behavior unchanged (no warning, swap kept)",
              clean["allergen_warning"] == "" and len(clean["swaps"]) == 1)
        mp = _m.normalize_mealplan({"days": [{"day": 1, "meals": [
            {"slot": "breakfast", "name": "Mixed berry smoothie", "ingredients": ["strawberries", "milk"]},
            {"slot": "lunch", "name": "Grilled chicken salad", "ingredients": ["chicken", "lettuce"]}]}]},
            allergies=["fruit"])
        bk, ln = mp["days"][0]["meals"][0], mp["days"][0]["meals"][1]
        check("allergy-safe meal plan: leaked allergen flagged, safe meal not flagged",
              bool(bk.get("allergen_warning")) and "allergen_warning" not in ln,
              "breakfast flag=" + str(bk.get("allergen_warning")))
        # Rung 4b: hybrid estimator blends AI grams x USDA density + tightens the confidence band
        _dens = {"grilled chicken breast": 165.0, "white rice": 130.0}
        hyb = _m._cross_check_calories(
            {"items": [{"name": "Grilled chicken breast", "qty": "200 g", "calories": 400},
                       {"name": "Side salad", "qty": "1 bowl", "calories": 90}],
             "total": {"calories": 490}},
            density_fn=lambda n: _dens.get(n.strip().lower()))
        i0, i1 = hyb["items"][0], hyb["items"][1]
        check("accuracy Rung 4b: AI grams x USDA density blends calories + tightens band, AI-only stays",
              i0["kcal_source"] == "hybrid" and i0["calories"] == 358 and i1["kcal_source"] == "ai"
              and 0 < hyb["total"]["band_pct"] < 0.25,
              "chicken=%scal(%s) salad=%s band=%s" % (i0["calories"], i0["kcal_source"], i1["kcal_source"], hyb["total"]["band_pct"]))
        # micros (shipped 2026-06-28): the USDA nutrient map must stay Cronometer-level (~30 vits/minerals).
        check("micros: USDA nutrient map covers ~30 vitamins/minerals (Cronometer-level)",
              len(_m._FDC_NUTRIENTS) >= 28, "%d nutrients in _FDC_NUTRIENTS" % len(_m._FDC_NUTRIENTS))
    except Exception as e:  # noqa: BLE001
        check("allergy-safe scan + meal plan (in-process)", False, "exception: " + str(e)[:120])

    # ---- PROACTIVE COACH CAL nudge engine — HARD LAWS (in-process unit checks, no browser needed) ----
    # Seeds real rows into the SAME db the server uses via a throwaway uid, calls the nudge helper functions
    # directly, then cleans up. Added 2026-07-15 alongside the greenlit meal-gap/water/workout nudge engine.
    try:
        import re as _re
        from datetime import date as _date, timedelta as _td
        _nm = _m  # the app.py module already imported above for the allergy/accuracy checks
        UID = "_gate_nudge_test_uid"
        UID2 = "_gate_nudge_test_uid_nohist"

        def _wipe(uid):
            con = _nm.get_db()
            try:
                for tbl in ("meals", "exercise", "water_log", "nudge_log", "push_subs"):
                    con.execute("DELETE FROM " + tbl + " WHERE uid = ?", (uid,))
                con.commit()
            finally:
                con.close()

        _wipe(UID); _wipe(UID2)
        con = _nm.get_db()
        try:
            # 5 days of meals (first ~07:30, second ~12:15) -> a clean learned pattern; TODAY left empty.
            for i in range(1, 6):
                d = (_date.today() - _td(days=i)).isoformat()
                con.execute("INSERT INTO meals(date, time, name, calories, protein_g, carbs_g, fat_g, uid) VALUES (?,?,?,?,?,?,?,?)",
                           (d, "07:30", "Breakfast", 400, 20, 40, 15, UID))
                con.execute("INSERT INTO meals(date, time, name, calories, protein_g, carbs_g, fat_g, uid) VALUES (?,?,?,?,?,?,?,?)",
                           (d, "12:15", "Lunch", 600, 30, 60, 20, UID))
            # 4 days of water history (glasses>0), today untouched.
            for i in range(1, 5):
                d = (_date.today() - _td(days=i)).isoformat()
                con.execute("INSERT INTO water_log(uid, date, glasses) VALUES (?,?,?)", (UID, d, 6))
            # 4 days of exercise history around 18:00, today untouched.
            for i in range(1, 5):
                d = (_date.today() - _td(days=i)).isoformat()
                con.execute("INSERT INTO exercise(uid, date, time, name, minutes, calories) VALUES (?,?,?,?,?,?)",
                           (UID, d, "18:00", "Walk", 30, 150))
            con.commit()
        finally:
            con.close()

        learned = _nm._nudge_learn_meal_times(UID)
        check("nudge: meal-time learning — median first/second-meal minutes from 14d history",
              learned[0] == 450 and learned[1] == 735 and learned[2] == 5, "learned=%s" % (learned,))
        no_hist = _nm._nudge_learn_meal_times(UID2)
        check("nudge: meal-gap silently unavailable for a user with NO meal history (never nags an unused feature)",
              no_hist[0] is None, "no_hist=%s" % (no_hist,))

        gap = _nm._nudge_meal_gap_candidate(UID, 450 + _nm.NUDGE_GRACE_MIN + 1, False)
        check("nudge: MEAL-GAP fires ~90min past the user's own learned first-log time with nothing logged today",
              bool(gap) and gap[0] == "meal_gap", "gap=%s" % (gap,))
        gap_early = _nm._nudge_meal_gap_candidate(UID, 450 + 10, False)
        check("nudge: MEAL-GAP does NOT fire before the 90min grace window",
              gap_early is None, "gap_early=%s" % (gap_early,))
        gap_gentle = _nm._nudge_meal_gap_candidate(UID, 450 + _nm.NUDGE_GRACE_MIN + 1, True)
        check("nudge: GENTLE MODE — meal-gap copy NEVER mentions a number (neither gentle nor default copy does)",
              bool(gap_gentle) and not _re.search(r"\d", gap_gentle[2]) and not _re.search(r"\d", gap[2]),
              "gentle_body=%r default_body=%r" % (gap_gentle[2] if gap_gentle else None, gap[2] if gap else None))

        water_none = _nm._nudge_water_candidate(UID2, 17 * 60, False)
        check("nudge: WATER silently unavailable for a user with NO water-logging history",
              water_none is None, "water_none=%s" % (water_none,))
        water_hit = _nm._nudge_water_candidate(UID, 17 * 60, False)
        check("nudge: WATER fires late afternoon for a user WITH a logging habit + none today",
              bool(water_hit) and water_hit[0] == "water", "water_hit=%s" % (water_hit,))
        water_early = _nm._nudge_water_candidate(UID, 10 * 60, False)
        check("nudge: WATER does NOT fire before late afternoon",
              water_early is None, "water_early=%s" % (water_early,))

        wo_none = _nm._nudge_workout_candidate(UID2, 20 * 60, False)
        check("nudge: WORKOUT silently unavailable for a user with NO exercise history",
              wo_none is None, "wo_none=%s" % (wo_none,))
        wo_hit = _nm._nudge_workout_candidate(UID, 18 * 60 + _nm.NUDGE_GRACE_MIN + 1, False)
        check("nudge: WORKOUT fires ~90min past the user's typical hour with none logged today",
              bool(wo_hit) and wo_hit[0] == "workout", "wo_hit=%s" % (wo_hit,))

        check("nudge: QUIET HOURS block 21:00-09:00 local (21:00/03:00/08:59 blocked, 09:00/14:00 allowed)",
              _nm._nudge_in_quiet_hours(21 * 60) and _nm._nudge_in_quiet_hours(3 * 60)
              and _nm._nudge_in_quiet_hours(8 * 60 + 59) and not _nm._nudge_in_quiet_hours(9 * 60)
              and not _nm._nudge_in_quiet_hours(14 * 60),
              "21:00=%s 03:00=%s 08:59=%s 09:00=%s 14:00=%s" % tuple(
                  _nm._nudge_in_quiet_hours(m) for m in (21*60, 3*60, 8*60+59, 9*60, 14*60)))

        con = _nm.get_db()
        try:
            con.execute("INSERT INTO nudge_log(uid, type, date, ts, ab_bucket) VALUES (?,?,?,?,?)",
                       (UID, "meal_gap", "2099-01-01", "x", "nudges_on"))
            con.execute("INSERT INTO nudge_log(uid, type, date, ts, ab_bucket) VALUES (?,?,?,?,?)",
                       (UID, "water", "2099-01-01", "x", "nudges_on"))
            con.commit()
        finally:
            con.close()
        check("nudge: MAX 2/DAY cap — count reflects exactly the 2 seeded rows for that local date",
              _nm._nudge_count_today(UID, "2099-01-01") == 2,
              "count=%s" % _nm._nudge_count_today(UID, "2099-01-01"))
        check("nudge: NEVER REPEAT A TYPE same day — sent_types_today includes both seeded types",
              _nm._nudge_sent_types_today(UID, "2099-01-01") == {"meal_gap", "water"},
              "types=%s" % _nm._nudge_sent_types_today(UID, "2099-01-01"))
        check("nudge: a fresh local date has zero nudges logged (dedupe is per-day, not global)",
              _nm._nudge_count_today(UID, "2099-01-02") == 0)

        b1, b2 = _nm._nudge_ab_bucket(UID), _nm._nudge_ab_bucket(UID)
        check("nudge: A/B bucket is a DETERMINISTIC function of uid (same uid -> same bucket every call)",
              b1 == b2 and b1 in ("nudges_on", "control"), "b1=%s b2=%s" % (b1, b2))
        buckets = [_nm._nudge_ab_bucket("gate-synthetic-uid-%d" % i) for i in range(300)]
        on_frac = buckets.count("nudges_on") / float(len(buckets))
        check("nudge: A/B split is roughly 50/50 across many uids (sanity, not exact)",
              0.35 <= on_frac <= 0.65, "on_frac=%.2f" % on_frac)

        check("nudge: fail-closed — NUDGES_ENABLED requires NUDGE_TICK_KEY to be set (gate armed it for this run)",
              bool(_nm.NUDGE_TICK_KEY) and _nm.NUDGES_ENABLED, "key_set=%s enabled=%s" % (bool(_nm.NUDGE_TICK_KEY), _nm.NUDGES_ENABLED))

        _wipe(UID); _wipe(UID2)
    except Exception as e:  # noqa: BLE001
        check("nudge engine hard-laws (in-process)", False, "exception: " + str(e)[:160])

    # ---- /api/nudge/tick AUTH (real HTTP against the running server; no browser needed) ----
    try:
        req = urllib.request.Request(BASE + "/api/nudge/tick", data=b"", method="POST",
                                     headers={"X-Nudge-Key": "definitely-the-wrong-key"})
        wrong_status = None
        try:
            urllib.request.urlopen(req, timeout=8)
        except urllib.error.HTTPError as he:
            wrong_status = he.code
        check("nudge tick endpoint: wrong X-Nudge-Key is REJECTED (403)", wrong_status == 403, "status=%s" % wrong_status)

        req2 = urllib.request.Request(BASE + "/api/nudge/tick", data=b"", method="POST",
                                      headers={"X-Nudge-Key": os.environ.get("NUDGE_TICK_KEY", "")})
        with urllib.request.urlopen(req2, timeout=15) as resp2:
            body2 = json.loads(resp2.read().decode("utf-8"))
        check("nudge tick endpoint: correct key -> 200 with sent/skipped/dead/failed/total in the response",
              all(k in body2 for k in ("sent", "skipped", "dead", "failed", "total")), "body=%s" % body2)
    except Exception as e:  # noqa: BLE001
        check("nudge tick endpoint auth (HTTP)", False, "exception: " + str(e)[:160])

    # ---- Coach Cal VOICE: daily TTS budget guard + dual-model fallback + rationing kill-switch
    #      (2026-07-16, built after a live 429 RESOURCE_EXHAUSTED on the primary TTS model's 100-req/day cap).
    #      Pure-function unit checks (no I/O) + an in-process Flask test-client run with _tts_generate
    #      monkeypatched to SIMULATE the 429 -> no real Gemini calls, no live quota burned by the gate. ----
    try:
        _tm = _m  # the app.py module already imported above for the allergy/nudge checks

        # -- 1. _tts_pick_order: pure decision function, unit-tested directly across the count matrix --
        check("tts budget: fresh day (0,0) -> try primary first, fallback available as 2nd attempt",
              _tm._tts_pick_order(0, 0) == ["primary", "fallback"], "order=%s" % _tm._tts_pick_order(0, 0))
        check("tts budget: just under soft cap (89,0) -> still primary-first",
              _tm._tts_pick_order(89, 0) == ["primary", "fallback"], "order=%s" % _tm._tts_pick_order(89, 0))
        check("tts budget: AT soft cap (90,0) -> preemptively switches to fallback FIRST",
              _tm._tts_pick_order(90, 0) == ["fallback", "primary"], "order=%s" % _tm._tts_pick_order(90, 0))
        check("tts budget: primary hard-capped (100,50) -> primary skipped entirely, only fallback tried",
              _tm._tts_pick_order(100, 50) == ["fallback"], "order=%s" % _tm._tts_pick_order(100, 50))
        check("tts budget: fallback hard-capped but primary still has real headroom (95,100) -> falls back to primary",
              _tm._tts_pick_order(95, 100) == ["primary"], "order=%s" % _tm._tts_pick_order(95, 100))
        check("tts budget: BOTH hard-capped (100,100) -> empty order -> caller goes straight to graceful 502",
              _tm._tts_pick_order(100, 100) == [], "order=%s" % _tm._tts_pick_order(100, 100))

        # -- 2. End-to-end via the in-process Flask test client, monkeypatching _tts_generate to SIMULATE
        #       real Gemini responses/errors without spending live quota. Uses the same sqlite db file as the
        #       live server subprocess, so counts written here are the real budget-guard counters. --
        def _wipe_tts_budget():
            con = _tm.get_db()
            try:
                con.execute("DELETE FROM usage WHERE uid = ? AND kind IN ('tts_primary', 'tts_fallback')", ("_global",))
                con.commit()
            finally:
                con.close()

        def _seed_tts_count(model_key, n):
            con = _tm.get_db()
            try:
                today = _tm.date.today().isoformat()
                con.execute("INSERT INTO usage(uid, date, kind, count) VALUES(?,?,?,?) "
                           "ON CONFLICT(uid, date, kind) DO UPDATE SET count = ?",
                           ("_global", today, "tts_" + model_key, n, n))
                con.commit()
            finally:
                con.close()

        import uuid
        _orig_generate = _tm._tts_generate
        _orig_rationing = _tm.TTS_RATIONING
        _calls = []

        def _fake_generate_primary_429(model, voice, text):
            _calls.append(model)
            if model == _tm.TTS_MODEL:
                raise RuntimeError("simulated 429 RESOURCE_EXHAUSTED: generate_requests_per_model_per_day, limit: 100")
            return b"RIFF____WAVEfake"

        def _fake_generate_ok(model, voice, text):
            _calls.append(model)
            return b"RIFF____WAVEfake"

        def _fake_generate_never(model, voice, text):
            _calls.append(model)
            raise RuntimeError("should never be called")

        cli = _tm.app.test_client()
        try:
            _wipe_tts_budget()
            _tm._TTS_CACHE.clear()

            # a) real 429 on the primary -> retries the SAME text on the fallback, 200 OK, only fallback billed
            _calls[:] = []
            _tm._tts_generate = _fake_generate_primary_429
            r = cli.get("/api/tts?voice=Charon&text=" + uuid.uuid4().hex)
            check("tts fallback-order: primary 429s -> falls back to the 2nd model, request still succeeds (200)",
                  r.status_code == 200 and _calls == [_tm.TTS_MODEL, _tm.TTS_MODEL_FALLBACK],
                  "status=%s calls=%s" % (r.status_code, _calls))
            check("tts fallback-order: only the model that actually served the request gets billed",
                  _tm._tts_count("primary") == 0 and _tm._tts_count("fallback") == 1,
                  "primary=%s fallback=%s" % (_tm._tts_count("primary"), _tm._tts_count("fallback")))

            # b) primary at/over its SOFT cap -> preemptively tries fallback FIRST (primary never even attempted)
            _wipe_tts_budget(); _seed_tts_count("primary", _tm.TTS_DAILY_SOFT_CAP)
            _calls[:] = []
            _tm._tts_generate = _fake_generate_ok
            r = cli.get("/api/tts?voice=Charon&text=" + uuid.uuid4().hex)
            check("tts budget guard: primary at soft cap -> fallback tried FIRST, primary never attempted",
                  r.status_code == 200 and _calls == [_tm.TTS_MODEL_FALLBACK],
                  "status=%s calls=%s" % (r.status_code, _calls))

            # c) both models at the HARD cap -> graceful 502, NO Gemini call attempted at all (no wasted round-trip)
            _wipe_tts_budget(); _seed_tts_count("primary", _tm.TTS_DAILY_HARD_CAP); _seed_tts_count("fallback", _tm.TTS_DAILY_HARD_CAP)
            _calls[:] = []
            _tm._tts_generate = _fake_generate_never
            r = cli.get("/api/tts?voice=Charon&text=" + uuid.uuid4().hex)
            check("tts budget guard: both models exhausted -> graceful 502, zero Gemini calls attempted",
                  r.status_code == 502 and _calls == [], "status=%s calls=%s" % (r.status_code, _calls))

            # d) warm prewarm ping is skipped once the primary is in its soft-cap zone (never steals real budget)
            _wipe_tts_budget(); _seed_tts_count("primary", _tm.TTS_DAILY_SOFT_CAP)
            _calls[:] = []
            _tm._tts_generate = _fake_generate_never
            r = cli.get("/api/tts?voice=Charon&text=" + uuid.uuid4().hex + "&warm=1")
            check("tts prewarm: skipped (204, no Gemini call) once primary is in its soft-cap zone",
                  r.status_code == 204 and _calls == [], "status=%s calls=%s" % (r.status_code, _calls))

            # e) a normal-budget warm ping DOES fire (and counts against the budget guard, per spec item 4)
            _wipe_tts_budget()
            _calls[:] = []
            _tm._tts_generate = _fake_generate_ok
            r = cli.get("/api/tts?voice=Charon&text=" + uuid.uuid4().hex + "&warm=1")
            check("tts prewarm: fires + counts against the budget guard when the primary has headroom",
                  r.status_code == 200 and _calls == [_tm.TTS_MODEL] and _tm._tts_count("primary") == 1,
                  "status=%s calls=%s primary_count=%s" % (r.status_code, _calls, _tm._tts_count("primary")))

            # f) rationing kill-switch OFF -> exact legacy behavior: single primary attempt, no fallback, no guard
            _wipe_tts_budget(); _seed_tts_count("primary", _tm.TTS_DAILY_HARD_CAP)   # would 502-without-trying under rationing
            _calls[:] = []
            _tm.TTS_RATIONING = False
            _tm._tts_generate = _fake_generate_primary_429   # primary still "429s" in this test
            r = cli.get("/api/tts?voice=Charon&text=" + uuid.uuid4().hex)
            check("tts rationing flag OFF: legacy behavior — only the primary is ever attempted, budget guard bypassed",
                  r.status_code == 502 and _calls == [_tm.TTS_MODEL],
                  "status=%s calls=%s (rationing flag correctly gates ALL of items 1/3/4)" % (r.status_code, _calls))
        finally:
            _tm._tts_generate = _orig_generate
            _tm.TTS_RATIONING = _orig_rationing
            _wipe_tts_budget()
            _tm._TTS_CACHE.clear()
    except Exception as e:  # noqa: BLE001
        check("tts budget guard + fallback-order + rationing flag (in-process)", False, "exception: " + str(e)[:200])

    from playwright.sync_api import sync_playwright
    errors = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        ctx = browser.new_context(geolocation={"latitude": LAT, "longitude": LNG},
                                  permissions=["geolocation"])
        page = ctx.new_page()
        page.route("**/api/nearby*", _route_nearby)   # deterministic near-me data (Overpass is too slow/flaky to gate on)
        page.route("**/api/nutrition*", _route_nutrition)   # deterministic USDA name-search for the "add missed item" flow

        def _benign(msg):
            # maplibre throws an AbortError when an in-flight tile/style request is cancelled by a tab
            # switch or reload teardown — a transient navigation race, not an app bug. It flaked this
            # gate ~5x/session (forcing a re-run). Suppress ONLY this exact pattern; real JS errors
            # (TypeError/ReferenceError + any other uncaught exception) still fail the gate.
            m = (msg or "").lower()
            # An AbortError is a cancelled in-flight fetch/request (a tab switch or browser teardown aborts
            # it) — never an app bug. Suppress the maplibre tile/style variants AND the generic fetch abort
            # ("the user aborted a request") that flakes when teardown races our extra tab-load fetches.
            return "aborterror" in m and ("maplibre" in m or "_remove" in m or "_updatestyle" in m
                                          or "signal is aborted" in m or "user aborted a request" in m)

        def on_console(m):
            t = m.text.lower()
            # ignore benign network noise (favicon, CDN/tile/logo loads) — those degrade gracefully;
            # we only fail on REAL JS errors (TypeError/ReferenceError) + uncaught exceptions (pageerror).
            if m.type == "error" and "failed to load resource" not in t and "favicon" not in t and not _benign(m.text):
                errors.append(m.text[:160])
        page.on("console", on_console)
        page.on("pageerror", lambda e: (None if _benign(str(e)) else errors.append(str(e)[:160])))

        page.add_init_script("try{localStorage.setItem('snapcal_goal','lose_weight');localStorage.setItem('snapcal_c_snapcal_loc_primed','1');}catch(e){}")  # pre-prime location (lsGet/lsSet namespace keys with 'snapcal_c_') so geo checks aren't blocked by the one-time primer
        page.goto(BASE + "/?gate=1", wait_until="domcontentloaded", timeout=20000)
        page.evaluate("() => { window.premiumActive = true; try { goal = 'lose_weight'; } catch(e){} }")
        page.evaluate("() => switchTab('eatout')")

        # 1. near-me map renders
        try:
            page.wait_for_function(
                "window.nmMapObj && window._nmReady && !!document.querySelector('#nmMap canvas')",
                timeout=30000)
            check("eatout: near-me MAP renders", True)
        except Exception:
            check("eatout: near-me MAP renders", False, "map never became ready")

        # 2. meter present
        check("eatout: Healthy<->Treat METER present",
              page.evaluate("!!document.getElementById('nearMoodSlider')"))

        # wait for matched + grid bands
        try:
            page.wait_for_function(
                "(window._nearMatched||[]).length>0 && document.querySelectorAll('#chainGridWrap .grid-sec').length>0",
                timeout=30000)
        except Exception:
            pass

        # 3. near-me list distances (open the List view first)
        nm = page.evaluate("""() => {
            var lb = document.querySelector('.nm-toggle button[data-v="list"]'); if (lb) lb.click();
            var rows = document.querySelectorAll('#nmListRows .nm-li');
            var withD = Array.prototype.filter.call(rows, function(r){ return r.querySelector('.nm-li-d'); }).length;
            return { rows: rows.length, withD: withD };
        }""")
        check("eatout: near-me LIST shows distances", nm["rows"] > 0 and nm["withD"] == nm["rows"],
              str(nm["withD"]) + "/" + str(nm["rows"]) + " rows")

        # 4. grid flow band
        grid = page.evaluate("""() => {
            var secs = Array.prototype.map.call(document.querySelectorAll('#chainGridWrap .grid-sec'), function(s){ return s.textContent; });
            var g = document.querySelector('#chainGridWrap .chain-grid');
            var near = g ? Array.prototype.filter.call(g.querySelectorAll('.chain-card'), function(c){ return c.querySelector('.chain-dist'); }).length : 0;
            return { secs: secs, near: near };
        }""")
        check("eatout: grid 'Near you' band has distances",
              ("Near you" in grid["secs"]) and grid["near"] > 0,
              "sections=" + str(grid["secs"]) + " near-with-dist=" + str(grid["near"]))

        page.wait_for_timeout(700)
        emap = page.evaluate("() => ({ pins: window.nmMapObj ? nmMapObj.queryRenderedFeatures({layers:['spot-pins']}).length : 0, zoom: window.nmMapObj ? Math.round(nmMapObj.getZoom()) : 0 })")
        check("eatout: map shows several pins in view (not just the dot)", emap["pins"] >= 2,
              str(emap["pins"]) + " pins in view, z" + str(emap["zoom"]))

        # 5. *** failure path: food lookup fails -> map STILL renders ***
        fp = page.evaluate("""async () => {
            Object.keys(localStorage).filter(function(k){ return k.indexOf('near_')===0; }).forEach(function(k){ localStorage.removeItem(k); });
            var real = window.api;
            window.api = function(u){
                if (u.indexOf('/api/nearby') >= 0 && u.indexOf('kind=store') < 0) {
                    return Promise.resolve({ matched: [], nearby: [], center: { lat: 40.2452, lng: -75.6496 }, error: 'lookup_failed' });
                }
                return real(u);
            };
            doNearMe();
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=300; if ((document.querySelector('#nmMap canvas') && window._nmReady) || t>16000){ clearInterval(iv); r(); } }, 300); });
            window.api = real;
            return { canvas: !!document.querySelector('#nmMap canvas'), ready: !!window._nmReady,
                     meter: !!document.getElementById('nearMoodSlider'),
                     updateBtn: !!document.getElementById('nearMeBtn') };
        }""")
        check("eatout: MAP renders even when the food lookup FAILS (map-gone guard)",
              fp["canvas"] and fp["ready"])
        check("eatout: METER survives when the food lookup FAILS", fp["meter"])
        check("eatout: 'Update my location' survives when the food lookup FAILS", fp["updateBtn"])

        # reset + go to Stores
        page.evaluate("() => { Object.keys(localStorage).filter(function(k){ return k.indexOf('near_')===0; }).forEach(function(k){ localStorage.removeItem(k); }); }")
        page.evaluate("() => { window._findMode = 'store'; renderEatOut(); }")   # store-only (chips are multi-select now)
        try:
            page.wait_for_function(
                "document.getElementById('storeMoodSlider') && document.querySelectorAll('.store-list .nm-li').length>0",
                timeout=30000)
        except Exception:
            pass

        st = page.evaluate("""() => {
            var meter = !!document.getElementById('storeMoodSlider');
            var rows = document.querySelectorAll('.store-list .nm-li');
            var withD = Array.prototype.filter.call(rows, function(r){ return r.querySelector('.nm-li-d'); }).length;
            var names = Array.prototype.map.call(document.querySelectorAll('.store-list .nm-li-n'), function(n){ return n.textContent; });
            var blocked = names.filter(function(n){ return /boscov|\\bross\\b|burlington|tj ?maxx|marshalls|kohl/i.test(n); });
            return { meter: meter, rows: rows.length, withD: withD, blocked: blocked };
        }""")
        check("stores: METER present", st["meter"])
        check("stores: list shows distances", st["rows"] > 0 and st["withD"] == st["rows"],
              str(st["withD"]) + "/" + str(st["rows"]) + " rows")

        page.wait_for_timeout(700)
        smap = page.evaluate("() => ({ pins: window.nmMapObj ? nmMapObj.queryRenderedFeatures({layers:['spot-pins']}).length : 0, zoom: window.nmMapObj ? Math.round(nmMapObj.getZoom()) : 0 })")
        check("stores: map shows several pins in view (not just the dot)", smap["pins"] >= 2,
              str(smap["pins"]) + " pins in view, z" + str(smap["zoom"]))
        check("stores: no no-food stores leak in", len(st["blocked"]) == 0,
              ("leaked: " + ", ".join(st["blocked"])) if st["blocked"] else "")

        # 9. store sheet = coach picks + directions (not instant directions)
        sh = page.evaluate("""async () => {
            var list = window._storeList || [];
            var idx = list.findIndex(function(s){ return /aldi|walmart|giant|wawa|target|redner/i.test(s.name); });
            if (idx < 0) idx = 0;
            var t0 = performance.now();
            var btn = document.querySelector('[data-store-idx="' + idx + '"]'); if (btn) btn.click();
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=15; var b=document.getElementById('ssBody'); if ((b && b.querySelector('.ss-pick')) || t>32000){ clearInterval(iv); r(); } }, 15); });
            return { fillMs: Math.round(performance.now() - t0), picks: document.querySelectorAll('#ssBody .ss-pick').length, dirBtn: !!document.querySelector('.ss-dir-btn') };
        }""")
        check("stores: tap opens COACH SHEET (picks + Get directions)",
              sh["picks"] > 0 and sh["dirBtn"],
              str(sh["picks"]) + " picks, dirBtn=" + str(sh["dirBtn"]))
        check("stores: sheet fills INSTANTLY (no AI spinner wait)",
              sh["picks"] > 0 and sh["fillMs"] < 800, str(sh["fillMs"]) + "ms")

        # stores empty/fail -> MAP + 'Update my location' (not a dead-end message)
        se = page.evaluate("""async () => {
            var real = window.api;
            window.api = function(u){ if (u.indexOf('kind=store') >= 0){ return Promise.resolve({ stores: [], center: { lat: 40.2452, lng: -75.6496 } }); } return real(u); };
            Object.keys(localStorage).filter(function(k){ return k.indexOf('stores_')===0; }).forEach(function(k){ localStorage.removeItem(k); });
            window._findMode = 'food'; renderEatOut();
            await new Promise(function(r){ setTimeout(r, 400); });
            window._findMode = 'store'; renderEatOut();
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=400; if (document.getElementById('storeUpdateBtn') || t>16000){ clearInterval(iv); r(); } }, 400); });
            window.api = real;
            return { mapCanvas: !!document.querySelector('#nmMap canvas'), updateBtn: !!document.getElementById('storeUpdateBtn') };
        }""")
        check("stores: empty/fail shows a MAP (not a dead end)", se["mapCanvas"])
        check("stores: empty/fail shows 'Update my location'", se["updateBtn"])

        # COMBINED view (both chips): one interleaved restaurants+stores list + a mixed-pin map
        cv = page.evaluate("""async () => {
            window._findMode = 'both'; renderEatOut();
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=200; if ((document.querySelectorAll('#combinedList .cmb-li').length && window._nmReady) || t>30000){ clearInterval(iv); r(); } }, 200); });
            await new Promise(function(r){ setTimeout(r, 1300); });
            try { nmMapObj.resize(); } catch(e){}
            await new Promise(function(r){ setTimeout(r, 400); });
            var rows = document.querySelectorAll('#combinedList .cmb-li'), food = 0, store = 0;
            Array.prototype.forEach.call(rows, function(r){ if (r.dataset.kind === 'food') food++; else if (r.dataset.kind === 'store') store++; });
            return { rows: rows.length, food: food, store: store, ready: !!window._nmReady, pins: window.nmMapObj ? nmMapObj.queryRenderedFeatures({layers:['spot-pins']}).length : 0 };
        }""")
        check("combined: both chips -> interleaved restaurants + stores", cv["food"] > 0 and cv["store"] > 0,
              str(cv["food"]) + " food + " + str(cv["store"]) + " stores in one list")
        check("combined: map renders with mixed pins", cv["ready"] and cv["pins"] >= 2, str(cv["pins"]) + " pins, ready=" + str(cv["ready"]))

        # Coach Cal sheets must FILL INSTANTLY (curated picks first, never a long AI 'thinking...' spinner)
        ci = page.evaluate("""async () => {
            window.premiumActive = true;
            try { Object.keys(localStorage).filter(function(k){ return k.indexOf('coach_')===0 || k.indexOf('gro_')===0; }).forEach(function(k){ localStorage.removeItem(k); }); window._coachCache = null; } catch(e){}
            switchTab('today');
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=200; if (document.getElementById('coachBtn') || t>14000){ clearInterval(iv); r(); } }, 200); });
            var t0 = performance.now();
            openCoachSheet();
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=15; if (document.querySelector('#sheetBody .coach-meal') || t>4000){ clearInterval(iv); r(); } }, 15); });
            var coachMs = Math.round(performance.now() - t0), coachMeals = document.querySelectorAll('#sheetBody .coach-meal').length;
            var t1 = performance.now();
            openGrocerySheet();
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=15; if (document.querySelector('#sheetBody .gro-item') || t>4000){ clearInterval(iv); r(); } }, 15); });
            var groMs = Math.round(performance.now() - t1), groItems = document.querySelectorAll('#sheetBody .gro-item').length;
            return { coachMs: coachMs, coachMeals: coachMeals, groMs: groMs, groItems: groItems };
        }""")
        check("coach: 'what should I eat?' fills instantly (no long spinner)",
              ci["coachMeals"] >= 1 and ci["coachMs"] < 800, str(ci["coachMs"]) + "ms, " + str(ci["coachMeals"]) + " meals")
        check("grocery: list fills instantly (no long spinner)",
              ci["groItems"] >= 1 and ci["groMs"] < 800, str(ci["groMs"]) + "ms, " + str(ci["groItems"]) + " items")

        # ALLERGIES: the toggle must strip allergen items from EVERY instant suggestion
        av = page.evaluate("""() => {
            window._allergies = ['treenut'];
            var test = [{name:'Grilled Chicken', why:'lean protein'}, {name:'Almond butter toast', why:'healthy fats'}, {name:'Mixed nuts', why:'crunchy snack'}];
            var kept = allergyFilterPicks(test).map(function(p){ return p.name; });
            var hasNut = function(arr){ return (arr||[]).some(function(p){ return /almond|peanut|walnut|pecan|cashew|pistachio|mixed nut|hazelnut/i.test((p.name||p.item||'')+' '+(p.desc||p.why||'')); }); };
            var coachNut = hasNut(coachFallback(window.goal||'maintain').meals);
            var groNut = hasNut(groceryFallback().items);
            var labels = (typeof allergyLabels==='function') ? allergyLabels() : [];
            window._allergies = [];
            return { kept: kept, coachNut: coachNut, groNut: groNut, labels: labels };
        }""")
        check("allergies: filter strips allergen picks (3 -> 1 safe)",
              av["kept"] == ["Grilled Chicken"], "kept " + ", ".join(av["kept"]))
        check("allergies: coach + grocery fallbacks stay allergen-free when set",
              (not av["coachNut"]) and (not av["groNut"]), "coachNut=" + str(av["coachNut"]) + " groNut=" + str(av["groNut"]))
        check("allergies: labels flow to the AI request payload",
              av["labels"] == ["Tree nuts"], "payload allergies=" + str(av["labels"]))

        # DIET: vegan/veg/pescatarian toggle re-themes every suggestion
        dv = page.evaluate("""() => {
            window._allergies = []; window._diet = 'vegan';
            var test = [{name:'Grilled Tofu Bowl', why:'plant protein'}, {name:'Grilled Chicken Salad', why:'lean protein'}, {name:'Bacon Cheeseburger', why:'treat'}, {name:'Garden Salad', why:'fresh veggies'}];
            var kept = allergyFilterPicks(test).map(function(p){ return p.name; });
            var dietLbl = (typeof dietLabel==='function') ? dietLabel(window._diet) : '';
            var pill = (typeof allergyPillText==='function') ? allergyPillText() : '';
            window._diet = '';
            return { kept: kept, dietLbl: dietLbl, pill: pill };
        }""")
        check("diet: vegan strips meat + dairy picks (tofu + garden salad kept)",
              dv["kept"] == ["Grilled Tofu Bowl", "Garden Salad"], "kept " + ", ".join(dv["kept"]))
        check("diet: label flows + pill reflects it",
              dv["dietLbl"] == "Vegan" and "Vegan" in dv["pill"], "pill='" + dv["pill"] + "'")

        # CHAIN PICKS: the static per-chain "what to order" cards must respect diet + allergies
        cp = page.evaluate("""() => {
            window._allergies = []; window._diet = 'vegan';
            var meatChain = { chain:'TestBurger', best_picks:{ lose_weight:[{name:'Grilled Chicken Sandwich', calories:350, protein_g:30}], maintain:[{name:'Double Cheeseburger', calories:700, protein_g:35}], build_muscle:[{name:'Bacon Burger', calories:800, protein_g:45}] } };
            var vegChain  = { chain:'TestVeg', best_picks:{ lose_weight:[{name:'Garden Salad', calories:150, protein_g:5},{name:'Grilled Chicken', calories:300, protein_g:30}] } };
            var meatFits = chainHasFittingPick(meatChain);
            var vegFits  = chainHasFittingPick(vegChain);
            var meatSheet = sheetPicksHTML(meatChain, 0);
            var vegSheet  = sheetPicksHTML(vegChain, 0);
            window._diet = '';
            return {
                meatFits: meatFits, vegFits: vegFits,
                meatSheetHasMeat: /chicken|burger|bacon|cheese/i.test(meatSheet),
                vegSheetHasSalad: /Garden Salad/.test(vegSheet),
                vegSheetHasChicken: /Grilled Chicken/.test(vegSheet)
            };
        }""")
        check("chain picks: vegan hides ALL meat picks (meat-only chain -> AI fallback path)",
              (cp["meatFits"] is False) and (cp["meatSheetHasMeat"] is False), "meatFits=" + str(cp["meatFits"]) + " meatInSheet=" + str(cp["meatSheetHasMeat"]))
        check("chain picks: vegan keeps the salad, drops the chicken in a mixed chain",
              cp["vegFits"] and cp["vegSheetHasSalad"] and (cp["vegSheetHasChicken"] is False),
              "salad=" + str(cp["vegSheetHasSalad"]) + " chicken=" + str(cp["vegSheetHasChicken"]))

        # CURATED diet picks baked into the data render INSTANTLY (no AI round-trip)
        cd = page.evaluate("""() => {
            window._allergies = []; window._diet = 'vegan';
            var R = { chain:'TestChipotle', best_picks:{ lose_weight:[{name:'Chicken Burrito Bowl', calories:600, protein_g:40}] }, diet_picks:{ vegan:[{name:'Sofritas Bowl (no cheese, no sour cream)', calories:520, why:'Spicy tofu, beans, guac'}], vegetarian:[{name:'Veggie Bowl', calories:570, why:'Cheese and guac'}] } };
            var fits = chainHasFittingPick(R);
            renderChainSheet(R);
            var body = (document.getElementById('sheetBody') || {}).innerHTML || '';
            window._diet = '';
            return { fits: fits, hasCurated: /Sofritas Bowl/.test(body), hasMeat: /Chicken Burrito Bowl/.test(body), wentToAi: /Finding the best/.test(body) };
        }""")
        check("chain picks: curated vegan picks show INSTANTLY (no AI) when baked into the data",
              cd["fits"] and cd["hasCurated"] and (cd["hasMeat"] is False) and (cd["wentToAi"] is False),
              "curated=" + str(cd["hasCurated"]) + " meat=" + str(cd["hasMeat"]) + " wentToAI=" + str(cd["wentToAi"]))

        # "Take me there" -> one tap from picks to Google Maps directions to that chain
        dirbtn = page.evaluate("""() => {
            window._diet=''; window._allergies=[];
            renderChainSheet({ chain:'Royal Farms', best_picks:{ lose_weight:[{name:'Grilled Chicken', calories:300, protein_g:30}] } });
            var a = document.querySelector('#sheetBody .cd-dir-btn');
            return { hasBtn: !!a, label: a ? a.textContent : '', href: a ? (a.getAttribute('href')||'') : '' };
        }""")
        check("eat-out: 'Take me there' button -> Google Maps directions to the chain",
              dirbtn["hasBtn"] and ("Take me there" in dirbtn["label"]) and ("google.com/maps/dir" in dirbtn["href"]) and ("Royal%20Farms" in dirbtn["href"]),
              "btn=" + str(dirbtn["hasBtn"]) + " href=" + str(dirbtn["href"]))

        # ASK COACH CAL: free-text "what should I get at <any restaurant>" returns a game plan
        ac = page.evaluate("""async () => {
            var cardHasInput = /askCoachInput/.test(askCoachCardHTML());
            var realApi = window.api;
            window.api = function(u){ if (u.indexOf('ctx=restaurant') >= 0) return Promise.resolve({ intro:'At a steakhouse the steak is fine, the sides are the trap.', picks:[{item:'8oz Filet Mignon', calories:500, why:'Leanest cut, all protein'},{item:'Grilled Asparagus', calories:90, why:'Light, smoky side'}], tip:'Split the truffle fries.', kind:'restaurant' }); return realApi(u); };
            askCoachSheet('Capital Grille');
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=50; if (document.querySelector('#askCoachBody .cd-pick') || t>4000){ clearInterval(iv); r(); } }, 50); });
            var body = (document.getElementById('sheetBody') || {}).innerHTML || '';
            window.api = realApi;
            return { cardHasInput: cardHasInput, hasIntro: /sides are the trap/.test(body), pickCount: document.querySelectorAll('#askCoachBody .cd-pick').length, hasFilet: /Filet Mignon/.test(body), hasCals: /500 Cal/.test(body) };
        }""")
        check("ask coach: input present + free-text restaurant returns picks with calories",
              ac["cardHasInput"] and ac["hasIntro"] and ac["pickCount"] >= 2 and ac["hasFilet"] and ac["hasCals"],
              "input=" + str(ac["cardHasInput"]) + " picks=" + str(ac["pickCount"]) + " cals=" + str(ac["hasCals"]))

        # TALK TO COACH CAL: floating button + back-and-forth conversation (text path; voice I/O is browser-native)
        vc = page.evaluate("""async () => {
            var realApi = window.api, lastBody = null;
            window.api = function(u, opts){ if (u.indexOf('/api/chat') >= 0){ try { lastBody = JSON.parse(opts.body); } catch(e){} return Promise.resolve({ reply: 'Protein keeps you full and helps build muscle — aim for some at every meal.' }); } return realApi(u, opts); };
            var fab = !!document.getElementById('coachFab');
            openVoice();
            sendChat('what does protein mean?');
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=50; if (document.querySelectorAll('#voiceLog .vmsg-coach').length >= 2 || t>4000){ clearInterval(iv); r(); } }, 50); });
            var coach = document.querySelectorAll('#voiceLog .vmsg-coach').length;
            var user = document.querySelectorAll('#voiceLog .vmsg-user').length;
            var hasReply = /keeps you full/.test((document.getElementById('voiceLog')||{}).innerHTML||'');
            window.api = realApi; closeVoice();
            return { fab: fab, coach: coach, user: user, hasReply: hasReply, bodyHasLoc: !!(lastBody && ('nearby' in lastBody) && ('has_location' in lastBody) && ('route_to' in lastBody)) };
        }""")
        check("talk to coach cal: floating button + conversation (you ask, it replies)",
              vc["fab"] and vc["user"] >= 1 and vc["coach"] >= 2 and vc["hasReply"],
              "fab=" + str(vc["fab"]) + " user=" + str(vc["user"]) + " coachReplies=" + str(vc["coach"]))
        check("talk to coach cal: sends nearby places + location + route destination",
              vc["bodyHasLoc"], "payload carries nearby + has_location + route_to: " + str(vc["bodyHasLoc"]))

        # CONVERSATIONAL LOGGING (PROACTIVE COACH CAL half 2, 2026-07-15): chat can PROPOSE a log, but must
        # NEVER silently write it — only a tap on "Log it" may call /api/meals.
        lp = page.evaluate("""async () => {
            var realApi = window.api, mealsPosted = null;
            window.api = function(u, opts){
                if (u.indexOf('/api/chat') >= 0){
                    return Promise.resolve({ reply: 'Nice, a cheesesteak with fries — solid choice!', log_proposal: { items: [
                        { name: 'Cheesesteak', calories: 700, protein_g: 35, carbs_g: 60, fat_g: 30 },
                        { name: 'French fries', calories: 450, protein_g: 5, carbs_g: 55, fat_g: 22 }
                    ], estimate: true, gentle: false } });
                }
                if (u.indexOf('/api/meals') >= 0 && opts && opts.method === 'POST'){
                    try { mealsPosted = JSON.parse(opts.body); } catch(e){}
                    return Promise.resolve({ id: 999 });
                }
                return realApi(u, opts);
            };
            openVoice();
            sendChat('I just had a cheesesteak and fries');
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=50; if (document.querySelector('#voiceLog .log-proposal') || t>4000){ clearInterval(iv); r(); } }, 50); });
            var chip = document.querySelector('#voiceLog .log-proposal');
            var chipText = chip ? chip.textContent : '';
            var postedBeforeClick = mealsPosted;   // must stay null until the user taps — never auto-logs
            var btns = chip ? chip.querySelectorAll('button') : [];
            if (btns.length) btns[0].click();   // "Log it"
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=50; if (mealsPosted || t>3000){ clearInterval(iv); r(); } }, 50); });
            window.api = realApi; closeVoice();
            return { chipPresent: !!chip, chipText: chipText, postedBeforeClick: postedBeforeClick, mealsPosted: mealsPosted };
        }""")
        check("conversational logging: an eating-report reply renders a confirm chip, NOT auto-logged before the tap",
              lp["chipPresent"] and lp["postedBeforeClick"] is None,
              "chipPresent=" + str(lp["chipPresent"]) + " postedBeforeClick=" + str(lp["postedBeforeClick"]))
        check("conversational logging: chip shows both food names + estimated calories (non-gentle)",
              "Cheesesteak" in lp["chipText"] and "French fries" in lp["chipText"] and "700" in lp["chipText"],
              "chipText=" + lp["chipText"][:120])
        mp = lp["mealsPosted"] or {}
        check("conversational logging: tapping 'Log it' posts THROUGH /api/meals — both items, summed totals, estimate tier",
              mp.get("calories") == 1150 and mp.get("accuracy_tier") == "estimate" and mp.get("source") == "Coach Cal chat"
              and "Cheesesteak" in (mp.get("name") or "") and "French fries" in (mp.get("name") or ""),
              "posted=" + str(mp))

        # Gentle mode: the SAME proposal must show food names only, no numbers — and the dismiss (X) button
        # must NEVER post anything (a 3rd way to confirm "never silently logs").
        lpg = page.evaluate("""async () => {
            var realApi = window.api, mealsPosted = null, wasGentle = _gentle;
            _gentle = true;
            window.api = function(u, opts){
                if (u.indexOf('/api/chat') >= 0){
                    return Promise.resolve({ reply: 'Got it, logged that mentally with you.', log_proposal: { items: [
                        { name: 'Grilled chicken bowl', calories: 550, protein_g: 45, carbs_g: 40, fat_g: 18 }
                    ], estimate: true, gentle: true } });
                }
                if (u.indexOf('/api/meals') >= 0 && opts && opts.method === 'POST'){
                    try { mealsPosted = JSON.parse(opts.body); } catch(e){}
                    return Promise.resolve({ id: 998 });
                }
                return realApi(u, opts);
            };
            openVoice();
            sendChat('I just ate a grilled chicken bowl');
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=50; if (document.querySelector('#voiceLog .log-proposal') || t>4000){ clearInterval(iv); r(); } }, 50); });
            var chip = document.querySelector('#voiceLog .log-proposal');
            var chipText = chip ? chip.textContent : '';
            var btns = chip ? chip.querySelectorAll('button') : [];
            if (btns.length > 2) btns[2].click();   // "✕" dismiss
            await new Promise(function(r){ setTimeout(r, 300); });
            var stillThere = !!document.querySelector('#voiceLog .log-proposal');
            _gentle = wasGentle;
            window.api = realApi; closeVoice();
            return { chipText: chipText, mealsPostedAfterDismiss: mealsPosted, stillThere: stillThere };
        }""")
        check("conversational logging: GENTLE MODE chip shows food name only, NO numbers",
              "Grilled chicken bowl" in lpg["chipText"] and not __import__("re").search(r"\d", lpg["chipText"]),
              "chipText=" + lpg["chipText"][:120])
        check("conversational logging: dismissing (X) NEVER posts to /api/meals and removes the chip",
              lpg["mealsPostedAfterDismiss"] is None and not lpg["stillThere"],
              "posted=" + str(lpg["mealsPostedAfterDismiss"]) + " stillThere=" + str(lpg["stillThere"]))

        # GREEN "Talk to Coach Cal" on the home card must open the VOICE panel (talk), not the picks sheet
        bchat = page.evaluate("""() => {
            closeVoice(); closeSheet();
            _renderBriefing('Time to fuel up for your goals today.', 'midday');
            var btn = document.getElementById('briefChat');
            var label = btn ? btn.textContent : '';
            if (btn) btn.click();
            var voiceOpen = !!document.querySelector('#voiceWrap.show');
            var sheetOpen = !!document.querySelector('#sheet.show');
            closeVoice();
            return { hasBtn: !!btn, label: label, voiceOpen: voiceOpen, sheetOpen: sheetOpen };
        }""")
        check("talk to coach cal: GREEN home button opens the voice panel to TALK (not the picks sheet)",
              bchat["hasBtn"] and ("Talk to Coach Cal" in bchat["label"]) and bchat["voiceOpen"] and (bchat["sheetOpen"] is False),
              "label=" + str(bchat["label"]) + " voiceOpen=" + str(bchat["voiceOpen"]) + " sheetOpen=" + str(bchat["sheetOpen"]))

        # FOOD PICK CARDS: every Coach Cal meal pick gets a "Take me there" -> Maps button (go get the food)
        mealdir = page.evaluate("""() => {
            renderCoachSheet({ meals:[{name:'Chicken & Rice Bowl', calories:600, protein_g:45, carbs_g:60, fat_g:15, desc:'Grilled chicken, jasmine rice', why:'High-protein muscle plate'}] }, 600, GOAL_INFO.build_muscle);
            var a = document.querySelector('#sheetBody .coach-meal-dir');
            return { hasBtn: !!a, label: a ? a.textContent : '', href: a ? (a.getAttribute('href')||'') : '', count: document.querySelectorAll('#sheetBody .coach-meal').length, dirs: document.querySelectorAll('#sheetBody .coach-meal-dir').length };
        }""")
        check("coach picks: EVERY food card has a 'Take me there' Maps button",
              mealdir["hasBtn"] and ("Take me there" in mealdir["label"]) and ("google.com/maps/search" in mealdir["href"]) and ("Chicken" in mealdir["href"]) and (mealdir["dirs"] == mealdir["count"]),
              "btn=" + str(mealdir["hasBtn"]) + " dirs=" + str(mealdir["dirs"]) + "/" + str(mealdir["count"]) + " href=" + str(mealdir["href"]))

        # SWIPE-DOWN dismiss: sheet has the grab handle (X alone isn't enough on a phone)
        grip = page.evaluate("() => ({ grip: !!document.querySelector('#sheet .sheet-grip') })")
        check("sheets: swipe-down grab handle present (a small X isn't enough on a phone)",
              grip["grip"], "grip=" + str(grip["grip"]))

        # PROACTIVE DIRECTIONS: when Coach names places, tappable "Take me to ___" buttons appear automatically
        # (user shouldn't have to type "take me there" or ask "where is that").
        pdir = page.evaluate("""async () => {
            var realApi = window.api;
            window._chatNearby = [{ name:'Hi Pot', dist_m:1200 }, { name:'Sakura Asian Cuisine', dist_m:3200 }];
            window._chatHasLoc = true;
            window.api = function(u, opts){
                if (u.indexOf('/api/chat') >= 0) return Promise.resolve({ reply: 'Grab a chicken and veggie rice bowl at Hi Pot (about $10). Sakura Asian Cuisine is another solid pick. Want me to take you there?' });
                return realApi(u, opts);
            };
            openVoice();
            sendChat('where can I grab something healthy near me?');
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=50; if (document.querySelectorAll('#voiceLog a.vmsg-coach').length >= 2 || t>4000){ clearInterval(iv); r(); } }, 50); });
            var links = Array.prototype.map.call(document.querySelectorAll('#voiceLog a.vmsg-coach'), function(a){ return { text:a.textContent||'', href:a.getAttribute('href')||'' }; });
            window.api = realApi; closeVoice();
            return { count: links.length, links: links };
        }""")
        check("coach directions: naming a place auto-shows tappable 'Take me to' buttons (no 'where is that?')",
              pdir["count"] >= 2
              and any("Hi Pot" in l["text"] for l in pdir["links"])
              and any("Sakura" in l["text"] for l in pdir["links"])
              and all("google.com/maps" in l["href"] for l in pdir["links"]),
              "buttons=" + str(pdir["count"]) + " hiPot=" + str(any("Hi Pot" in l["text"] for l in pdir["links"]))
              + " sakura=" + str(any("Sakura" in l["text"] for l in pdir["links"]))
              + " allMaps=" + str(all("google.com/maps" in l["href"] for l in pdir["links"])))

        # BUDGET MODE: opt-in price toggle (default OFF so it never offends a non-budget customer); when ON the
        # chat payload carries budget:true so the server adds estimated prices.
        budgettgl = page.evaluate("""async () => {
            var has = !!document.getElementById('budgetToggle');
            var defOff = (localStorage.getItem('snapcal_budget') !== '1');
            setBudget(true);
            var realApi = window.api, sent = null;
            window.api = function(u, opts){ if (u.indexOf('/api/chat') >= 0){ try { sent = JSON.parse(opts.body); } catch(e){} return Promise.resolve({ reply:'ok' }); } return realApi(u, opts); };
            openVoice(); sendChat('what should I eat for lunch?');
            await new Promise(function(r){ setTimeout(r, 700); });
            var on = !!(sent && sent.budget === true);
            window.api = realApi; closeVoice(); setBudget(false);
            return { has: has, defOff: defOff, on: on };
        }""")
        check("budget mode: opt-in price toggle present, OFF by default, sends budget flag when on",
              budgettgl["has"] and budgettgl["defOff"] and budgettgl["on"],
              "toggle=" + str(budgettgl["has"]) + " defaultOff=" + str(budgettgl["defOff"]) + " sentFlag=" + str(budgettgl["on"]))

        # SCAN RESULTS: each detected item has a Remove button to drop a wrong item (a friend's plate) before logging
        rmitem = page.evaluate("""() => {
            window.analyzeResult = { items:[
              {name:'Dumpling', calories:80, protein_g:3, carbs_g:10, fat_g:3, fiber_g:0, sugar_g:1, sat_fat_g:1, sodium_mg:150},
              {name:'Grilled Chicken', calories:300, protein_g:35, carbs_g:2, fat_g:12, fiber_g:0, sugar_g:0, sat_fat_g:3, sodium_mg:300}
            ], mults:[1,1],
            total:{calories:380,protein_g:38,carbs_g:12,fat_g:15,fiber_g:0,sugar_g:1,sat_fat_g:4,sodium_mg:450,band_pct:0},
            health_score:72, quality_grade:'B', verdict:'', coach_tip:'', swaps:[], good_flags:[], bad_flags:[], satiety:'' };
            renderScanCard();
            var before = document.querySelectorAll('#resultItems .item-del').length;
            var rowsBefore = document.querySelectorAll('#resultItems .item-row').length;
            var delBtn = document.querySelector('#resultItems .item-del[data-del="0"]');
            if (delBtn) delBtn.click();
            var rowsAfter = document.querySelectorAll('#resultItems .item-row').length;
            var remaining = (window.analyzeResult && window.analyzeResult.items.length) || 0;
            var firstName = (document.querySelector('#resultItems .item-l .n') || {}).textContent || '';
            return { before: before, rowsBefore: rowsBefore, rowsAfter: rowsAfter, remaining: remaining, firstName: firstName };
        }""")
        check("scan results: each item has a Remove button that drops a wrong item before logging",
              rmitem["before"] == 2 and rmitem["rowsBefore"] == 2 and rmitem["rowsAfter"] == 1 and rmitem["remaining"] == 1 and ("Chicken" in rmitem["firstName"]),
              "delBtns=" + str(rmitem["before"]) + " rowsAfter=" + str(rmitem["rowsAfter"]) + " remaining=" + str(rmitem["remaining"]) + " first=" + str(rmitem["firstName"]))

        # ADD A MISSED ITEM: scan caught the eggs but not the cheese -> search the SAME USDA path,
        # append it to the SAME meal, totals recalc, and the existing portion/remove controls still
        # work on the newly-added item (add-only: nothing about the original item/flow changes).
        addmiss = page.evaluate("""async () => {
            window.analyzeResult = { items:[
              {name:'Scrambled Eggs', calories:220, protein_g:14, carbs_g:2, fat_g:16, fiber_g:0, sugar_g:1, sat_fat_g:5, sodium_mg:380}
            ], mults:[1],
            total:{calories:220,protein_g:14,carbs_g:2,fat_g:16,fiber_g:0,sugar_g:1,sat_fat_g:5,sodium_mg:380,band_pct:0},
            health_score:70, quality_grade:'B', verdict:'', coach_tip:'', swaps:[], good_flags:[], bad_flags:[], satiety:'' };
            renderScanCard(); renderAddMissedRow();
            var hasAddBtn = !!document.getElementById('addMissedBtn');
            document.getElementById('addMissedBtn').click();
            var hasForm = !!document.getElementById('missedForm');
            document.getElementById('missedQ').value = 'cheddar cheese';
            document.getElementById('missedSearchBtn').click();
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=40; if (document.querySelector('#missedPick') || t>4000){ clearInterval(iv); r(); } }, 40); });
            var pickPresent = !!document.querySelector('#missedPick');
            var pickText = pickPresent ? (document.querySelector('#missedPick .item-l .n')||{}).textContent : '';
            if (pickPresent) document.querySelector('#missedPick').click();
            var rowsAfterAdd = document.querySelectorAll('#resultItems .item-row').length;
            var itemCount = window.analyzeResult.items.length;
            var multCount = window.analyzeResult.mults.length;
            var totalTextAfterAdd = document.getElementById('totalKcal').textContent;
            var mealName = document.getElementById('mealName').value;
            var formResetToBtn = !!document.getElementById('addMissedBtn');
            // portion +/- still works on the NEWLY added item (index 1)
            var pBtn = document.querySelector('#resultItems .pbtn[data-i="1"][data-d="1"]');
            if (pBtn) pBtn.click();
            var multAfterPortion = window.analyzeResult.mults[1];
            var totalAfterPortion = document.getElementById('totalKcal').textContent;
            // remove still works on the newly added item
            var delBtn = document.querySelector('#resultItems .item-del[data-del="1"]');
            if (delBtn) delBtn.click();
            var itemCountAfterDel = window.analyzeResult.items.length;
            return { hasAddBtn: hasAddBtn, hasForm: hasForm, pickPresent: pickPresent, pickText: pickText,
                      rowsAfterAdd: rowsAfterAdd, itemCount: itemCount, multCount: multCount,
                      totalTextAfterAdd: totalTextAfterAdd, mealName: mealName, formResetToBtn: formResetToBtn,
                      multAfterPortion: multAfterPortion, totalAfterPortion: totalAfterPortion, itemCountAfterDel: itemCountAfterDel };
        }""")
        check("scan results: '+ Add an item the camera missed' searches USDA + appends to the SAME meal, totals recalc",
              addmiss["hasAddBtn"] and addmiss["hasForm"] and addmiss["pickPresent"] and "Cheddar" in addmiss["pickText"]
              and addmiss["rowsAfterAdd"] == 2 and addmiss["itemCount"] == 2 and addmiss["multCount"] == 2
              and "623" in addmiss["totalTextAfterAdd"] and "Cheddar" in addmiss["mealName"] and addmiss["formResetToBtn"],
              "rows=" + str(addmiss["rowsAfterAdd"]) + " items=" + str(addmiss["itemCount"]) + " total=" + str(addmiss["totalTextAfterAdd"]) + " meal=" + str(addmiss["mealName"]))
        check("scan results: portion +/- and Remove still work on a just-added missed item (add-only, no regression)",
              addmiss["multAfterPortion"] == 1.25 and "724" in addmiss["totalAfterPortion"] and addmiss["itemCountAfterDel"] == 1,
              "multAfterPortion=" + str(addmiss["multAfterPortion"]) + " totalAfterPortion=" + str(addmiss["totalAfterPortion"]) + " itemsAfterDel=" + str(addmiss["itemCountAfterDel"]))

        # ADD A MISSED ITEM -> quick-add fallback when the USDA search can't find it
        quickadd = page.evaluate("""async () => {
            window.analyzeResult = { items:[
              {name:'Scrambled Eggs', calories:220, protein_g:14, carbs_g:2, fat_g:16, fiber_g:0, sugar_g:1, sat_fat_g:5, sodium_mg:380}
            ], mults:[1],
            total:{calories:220,protein_g:14,carbs_g:2,fat_g:16,fiber_g:0,sugar_g:1,sat_fat_g:5,sodium_mg:380,band_pct:0},
            health_score:70, quality_grade:'B', verdict:'', coach_tip:'', swaps:[], good_flags:[], bad_flags:[], satiety:'' };
            renderScanCard(); renderAddMissedRow();
            document.getElementById('addMissedBtn').click();
            document.getElementById('missedQ').value = 'mystery sauce quickfail';
            document.getElementById('missedSearchBtn').click();
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=40; if (document.getElementById('missedQuickBtn') || t>4000){ clearInterval(iv); r(); } }, 40); });
            var quickBtnPresent = !!document.getElementById('missedQuickBtn');
            if (quickBtnPresent) document.getElementById('missedQuickBtn').click();
            var formPresent = !!document.getElementById('missedQaAddBtn');
            if (document.getElementById('missedQaName')) document.getElementById('missedQaName').value = 'Mystery Sauce';
            if (document.getElementById('missedQaCal')) document.getElementById('missedQaCal').value = '120';
            if (document.getElementById('missedQaAddBtn')) document.getElementById('missedQaAddBtn').click();
            var itemCount = window.analyzeResult.items.length;
            var lastItem = window.analyzeResult.items[1] || {};
            var totalText = document.getElementById('totalKcal').textContent;
            return { quickBtnPresent: quickBtnPresent, formPresent: formPresent, itemCount: itemCount, lastName: lastItem.name, lastCal: lastItem.calories, totalText: totalText };
        }""")
        check("scan results: 'quick add manually' fallback appends a name+calories item when USDA search finds nothing",
              quickadd["quickBtnPresent"] and quickadd["formPresent"] and quickadd["itemCount"] == 2
              and quickadd["lastName"] == "Mystery Sauce" and quickadd["lastCal"] == 120 and "340" in quickadd["totalText"],
              "items=" + str(quickadd["itemCount"]) + " last=" + str(quickadd["lastName"]) + "/" + str(quickadd["lastCal"]) + " total=" + str(quickadd["totalText"]))

        # ============================================================================
        # 2026-07-15 "+ ADD A MEAL" (Today screen) — the discoverability fix for a real tester
        # (owner's mom) who couldn't find manual food logging: it existed, buried as fine print under
        # the camera button on the Scan tab. This puts a big, obvious entry point right under the
        # calorie ring on Today, at the moment of "I forgot to take a picture." feedback_discoverability_
        # grandma_test.md is the law this enacts. Add-only: ring/macro rendering must be untouched.
        # ============================================================================

        # 1) The button renders VISIBLY on Today, positioned right under the ring (above the macro
        #    rings card) — and the ring/macro elements it must never touch are still intact.
        addmealbtn = page.evaluate("""async () => {
            switchTab('today');
            await new Promise(function(r){ setTimeout(r, 200); });
            var btn = document.getElementById('addMealBtn');
            var ringCard = document.querySelector('#tab-today .ring-wrap');
            var macroCard = document.querySelector('#tab-today .macro-rings');
            var visible = !!btn && btn.offsetParent !== null;
            var ringRect = ringCard ? ringCard.getBoundingClientRect() : null;
            var btnRect = btn ? btn.getBoundingClientRect() : null;
            var macroRect = macroCard ? macroCard.getBoundingClientRect() : null;
            var betweenRingAndMacros = !!(ringRect && btnRect && macroRect) &&
                btnRect.top >= ringRect.bottom - 2 && btnRect.top <= macroRect.top + 2;
            var ringIntact = !!document.getElementById('ringBig') && !!document.getElementById('metaEaten') && !!document.getElementById('proRing');
            return { present: !!btn, visible: visible, betweenRingAndMacros: betweenRingAndMacros,
                     ringIntact: ringIntact, btnText: btn ? btn.textContent.trim() : '' };
        }""")
        check("Today screen: '+ Add a meal' button renders visibly right under the calorie ring (ring/macros untouched, add-only)",
              addmealbtn["present"] and addmealbtn["visible"] and addmealbtn["betweenRingAndMacros"]
              and addmealbtn["ringIntact"] and "Add a meal" in addmealbtn["btnText"],
              str(addmealbtn))

        # 2) Tap it -> type a comma-separated multi-item entry -> each term resolves via the SAME USDA
        #    /api/nutrition search the missed-item flow uses (mocked above) -> logs as ONE meal entry.
        addmeal_multi = page.evaluate("""async () => {
            switchTab('today');
            window._recents = [{ name:'placeholder', calories:1, protein_g:0, carbs_g:0, fat_g:0 }];  // skip the real fetch, deterministic
            var real = window.api, posted = null;
            window.api = function(u, opts){
                if (u.indexOf('/api/meals') >= 0 && opts && opts.method === 'POST'){ posted = JSON.parse(opts.body); return Promise.resolve({ ok:true, id:99 }); }
                return real(u, opts);
            };
            document.getElementById('addMealBtn').click();
            document.getElementById('addMealInput').value = 'cheesesteak, french fries';
            document.getElementById('addMealFindBtn').click();
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=40; if (document.querySelectorAll('#addMealResolved .item-row').length>=2 || t>4000){ clearInterval(iv); r(); } }, 40); });
            var rows = document.querySelectorAll('#addMealResolved .item-row').length;
            var totalBefore = (document.getElementById('addMealTotalKcal')||{}).textContent || '';
            var logVisible = document.getElementById('addMealLogBtn').style.display !== 'none';
            document.getElementById('addMealLogBtn').click();
            await new Promise(function(r){ setTimeout(r, 250); });
            window.api = real;
            var sheetClosed = document.getElementById('addMealWrap').innerHTML.trim() === '';
            return { rows: rows, totalBefore: totalBefore, logVisible: logVisible, posted: posted, sheetClosed: sheetClosed };
        }""")
        posted_items = json.loads(addmeal_multi["posted"]["items_json"]) if addmeal_multi.get("posted") else []
        check("Today: '+ Add a meal' resolves a comma-separated multi-item entry via USDA search and logs it as ONE meal",
              addmeal_multi["rows"] == 2 and addmeal_multi["logVisible"] and "806" in addmeal_multi["totalBefore"]
              and addmeal_multi["posted"] is not None and addmeal_multi["posted"].get("calories") == 806
              and len(posted_items) == 2 and addmeal_multi["posted"].get("source") == "Manual entry"
              and addmeal_multi["posted"].get("accuracy_tier") == "estimate" and addmeal_multi["sheetClosed"],
              "rows=" + str(addmeal_multi["rows"]) + " total=" + str(addmeal_multi["totalBefore"]) + " posted=" + str(addmeal_multi["posted"]))

        # 3) Small/Medium/Large portion picker (0.7x / 1.0x / 1.5x) updates the shown calories LIVE,
        #    per item, before logging.
        addmeal_portion = page.evaluate("""async () => {
            switchTab('today');
            window._recents = [{ name:'placeholder', calories:1, protein_g:0, carbs_g:0, fat_g:0 }];
            document.getElementById('addMealBtn').click();
            document.getElementById('addMealInput').value = 'cheddar cheese';
            document.getElementById('addMealFindBtn').click();
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=40; if (document.querySelectorAll('#addMealResolved .item-row').length>=1 || t>4000){ clearInterval(iv); r(); } }, 40); });
            var totalM = (document.getElementById('addMealTotalKcal')||{}).textContent || '';
            var id = window._addMealItems[0]._id;
            var sBtn = document.querySelector('[data-addmeal-size="'+id+'"][data-sz="S"]');
            if (sBtn) sBtn.click();
            var totalS = (document.getElementById('addMealTotalKcal')||{}).textContent || '';
            // re-render replaces the DOM node the click fired on, so re-query fresh before reading its class
            var sBtnAfter = document.querySelector('[data-addmeal-size="'+id+'"][data-sz="S"]');
            var sOn = sBtnAfter ? sBtnAfter.classList.contains('on') : false;
            var lBtn = document.querySelector('[data-addmeal-size="'+id+'"][data-sz="L"]');
            if (lBtn) lBtn.click();
            var totalL = (document.getElementById('addMealTotalKcal')||{}).textContent || '';
            return { totalM: totalM, totalS: totalS, totalL: totalL, sOn: sOn };
        }""")
        check("Today: '+ Add a meal' Small/Medium/Large portion picker changes the shown total live (0.7x / 1.0x / 1.5x)",
              "403" in addmeal_portion["totalM"] and "282" in addmeal_portion["totalS"] and "605" in addmeal_portion["totalL"] and addmeal_portion["sOn"],
              str(addmeal_portion))

        # ACCESSIBILITY + ROUTE-CORRIDOR UI
        ts = page.evaluate("""() => {
            localStorage.setItem('snapcal_textsize','1.15'); applyTextSize();
            var applied = document.documentElement.style.zoom;
            var active = (document.querySelector('#textSizeSeg button.active')||{}).getAttribute('data-ts');
            var destInput = !!document.getElementById('destInput');
            localStorage.setItem('snapcal_textsize','1'); applyTextSize();
            return { applied: applied, active: active, reset: document.documentElement.style.zoom, destInput: destInput };
        }""")
        check("accessibility: Large Text scales the whole app + persists",
              ts["applied"] == "1.15" and ts["active"] == "1.15" and ts["reset"] == "1",
              "applied=" + str(ts["applied"]) + " reset=" + str(ts["reset"]))
        check("route: destination input present (set work -> healthy food along your drive)",
              ts["destInput"], "destInput present=" + str(ts["destInput"]))

        # USDA NUTRITION: science-backed facts card
        nu = page.evaluate("""async () => {
            var realApi = window.api;
            window.api = function(u){ if (u.indexOf('/api/nutrition') >= 0) return Promise.resolve({ food:'Eggs, Grade A, Large', serving:'per 100 g', source:'USDA FoodData Central', nutrients:{calories:143, protein_g:12.4, fat_g:9.9, carbs_g:0.7} }); return realApi(u); };
            var inputPresent = !!document.getElementById('nutriInput');
            document.getElementById('nutriInput').value = 'large egg';
            lookupNutrition();
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=40; if (document.querySelector('#nutriResult .nf-card') || t>3000){ clearInterval(iv); r(); } }, 40); });
            var body = (document.getElementById('nutriResult')||{}).innerHTML || '';
            window.api = realApi;
            return { input: inputPresent, hasFood: /Eggs, Grade A/.test(body), hasCals: /143/.test(body), hasSource: /USDA FoodData Central/.test(body), rows: document.querySelectorAll('#nutriResult .nf-row').length };
        }""")
        check("nutrition: USDA facts card renders (food + calories + USDA source)",
              nu["input"] and nu["hasFood"] and nu["hasCals"] and nu["hasSource"] and nu["rows"] >= 3,
              "rows=" + str(nu["rows"]) + " source=" + str(nu["hasSource"]))

        # LOCATION: one-time privacy primer -> explain once, set flag on Allow, never re-ask
        prim = page.evaluate("""async () => {
            localStorage.removeItem('snapcal_c_snapcal_loc_primed');
            snapGeo(function(){}, function(){}, {});
            var ov = document.getElementById('locPrimer');
            var visible = !!ov && getComputedStyle(ov).display !== 'none';
            var body = ov ? ((ov.querySelector('.locp-body')||{}).textContent || '') : '';
            var hasPrivacy = /private/i.test(body) && /once/i.test(body) && /never shared/i.test(body);
            var primedBefore = localStorage.getItem('snapcal_c_snapcal_loc_primed');
            if (ov) ov.querySelector('.locp-allow').click();
            var primedAfter = localStorage.getItem('snapcal_c_snapcal_loc_primed');
            var hidden = ov ? getComputedStyle(ov).display === 'none' : false;
            snapGeo(function(){}, function(){}, {});                 // primed now -> must NOT reshow
            var reshown = ov ? getComputedStyle(ov).display !== 'none' : false;
            return { visible: visible, hasPrivacy: hasPrivacy, primedBefore: primedBefore, primedAfter: primedAfter, hidden: hidden, reshown: reshown };
        }""")
        check("location: one-time primer (privacy + 'asked once') -> Allow sets flag, then never re-asks",
              prim["visible"] and prim["hasPrivacy"] and not prim["primedBefore"] and prim["primedAfter"] and prim["hidden"] and not prim["reshown"],
              "visible=%s privacy=%s primed:%s->%s hidden=%s reshown=%s" % (prim["visible"], prim["hasPrivacy"], prim["primedBefore"], prim["primedAfter"], prim["hidden"], prim["reshown"]))

        # SCAN ESCAPE: a ✕ on the preview + result cards clears the scan -> back to home (first-tester bug: trapped on the result)
        sx = page.evaluate("""() => {
            var pcard = document.getElementById('previewCard'), rcard = document.getElementById('resultCard');
            pcard.style.display='block'; rcard.style.display='block';
            var pc = document.getElementById('previewClose'), rc = document.getElementById('resultClose');
            var hasBtns = !!pc && !!rc;
            if (rc) rc.click();
            var resultHidden = getComputedStyle(rcard).display === 'none';
            pcard.style.display='block';
            if (pc) pc.click();
            var previewHidden = getComputedStyle(pcard).display === 'none';
            return { hasBtns: hasBtns, resultHidden: resultHidden, previewHidden: previewHidden };
        }""")
        check("scan: close (X) on preview + result cards clears the scan -> back to home (no trap)",
              sx["hasBtns"] and sx["resultHidden"] and sx["previewHidden"],
              "btns=" + str(sx["hasBtns"]) + " resultHidden=" + str(sx["resultHidden"]) + " previewHidden=" + str(sx["previewHidden"]))

        # PROFILE PERSISTENCE: calories/macros/weight saved on the DEVICE survive reopen + server redeploy
        pp = page.evaluate("""() => {
            localStorage.removeItem('snapcal_profile'); localStorage.removeItem('snapcal_weight');
            profLocalSave({ daily_calories: 1850, protein_g: 140, carbs_g: 180, fat_g: 60 });
            localStorage.setItem('snapcal_weight', '165');
            var local = profLocalGet();                 // simulate a fresh reopen reading the device copy
            var loaded = local ? profFrom(local) : null;
            return { hasLocal: !!local, cal: loaded ? loaded.daily_calories : 0, pro: loaded ? loaded.protein_g : 0,
                     weight: localStorage.getItem('snapcal_weight') };
        }""")
        check("profile persists on device (calories + macros + weight survive reopen / redeploy)",
              pp["hasLocal"] and pp["cal"] == 1850 and pp["pro"] == 140 and pp["weight"] == "165",
              "cal=" + str(pp["cal"]) + " pro=" + str(pp["pro"]) + " weight=" + str(pp["weight"]))

        # TOP QUICK BAR: Coach Cal / Grocery / Meal Plan are top tabs (above the fold), single IDs, still wired
        qb = page.evaluate("""() => {
            var bar = document.querySelector('.quick-bar');
            var ids = ['coachBtn','groceryBtn','mealplanBtn'];
            var inBar = bar && ids.every(function(id){ var el=document.getElementById(id); return el && bar.contains(el); });
            var noDup = ids.every(function(id){ return document.querySelectorAll('#'+id).length === 1; });
            var aboveRing = false;
            try { aboveRing = bar.compareDocumentPosition(document.querySelector('.ring-wrap')) & Node.DOCUMENT_POSITION_FOLLOWING ? true : false; } catch(e){}
            return { bar: !!bar, inBar: !!inBar, noDup: noDup, aboveRing: aboveRing };
        }""")
        check("home: Coach Cal / Grocery / Meal Plan are top quick-tabs (above the ring, single IDs)",
              qb["bar"] and qb["inBar"] and qb["noDup"] and qb["aboveRing"],
              "bar=" + str(qb["bar"]) + " inBar=" + str(qb["inBar"]) + " noDup=" + str(qb["noDup"]) + " aboveRing=" + str(qb["aboveRing"]))

        # FOOD PHOTOS: pick images go through the real per-dish lookup, not the old mismatched category jpg
        fi = page.evaluate("""() => {
            var src = pickImg({name:'Filet Mignon'});
            var live = window.fetch ? null : null;
            return { src: src, usesApi: src.indexOf('/api/foodimg') === 0, hasDish: src.indexOf('Filet') >= 0, notStatic: src.indexOf('/static/img/food/') < 0 };
        }""")
        check("food photos: picks use the real per-dish image endpoint (not the generic category jpg)",
              fi["usesApi"] and fi["hasDish"] and fi["notStatic"], "src=" + fi["src"])
        # the endpoint itself must always 302 to SOMETHING (real photo or local fallback) — never a broken img
        st = page.evaluate("""async () => {
            try { var r = await fetch('/api/foodimg?dish=Grilled%20Asparagus', {redirect:'follow'}); return { ok: r.ok, type: (r.headers.get('content-type')||'') }; }
            catch(e){ return { ok:false, type:'err:'+e.message }; }
        }""")
        check("food photos: /api/foodimg resolves to an image (real or fallback)",
              st["ok"] and ("image" in st["type"]), "ok=" + str(st["ok"]) + " type=" + st["type"])

        # provenance router (ACCURACY_ENGINE.md): restaurant-EXACT menu lookup returns real macros
        mn = page.evaluate("""async () => {
            try { var r = await fetch('/api/menu?q=quarter%20pounder'); var d = await r.json();
                  var h = (d.results||[])[0]||{};
                  return { count: d.count, chains: (d.chains||[]).length, tier: h.accuracy_tier, cal: h.calories, name: h.name }; }
            catch(e){ return { count:-1, err:e.message }; }
        }""")
        check("provenance: /api/menu returns chain-EXACT items with macros",
              mn["count"] >= 1 and mn.get("tier") == "EXACT" and int(mn.get("cal") or 0) > 0 and mn["chains"] >= 30,
              "count=%s chains=%s tier=%s %s=%scal" % (mn["count"], mn.get("chains"), mn.get("tier"), mn.get("name"), mn.get("cal")))
        # provenance round-trips through the diary: tier stored + returned, photo defaults to ESTIMATE
        pv = page.evaluate("""async () => {
            var H = { 'Content-Type':'application/json', 'X-Device-Id':'gate_prov' };
            var day = '2099-01-01';
            await fetch('/api/meals', {method:'POST', headers:H, body: JSON.stringify({date:day, name:'Exact item', calories:520, source:'Published menu', accuracy_tier:'EXACT'})});
            await fetch('/api/meals', {method:'POST', headers:H, body: JSON.stringify({date:day, name:'Photo item', calories:600})});
            var r = await fetch('/api/meals?date='+day, {headers:H}); var d = await r.json();
            var byName = {}; (d.meals||[]).forEach(function(m){ byName[m.name]=m; });
            return { exact: (byName['Exact item']||{}).accuracy_tier, est: (byName['Photo item']||{}).accuracy_tier,
                     estConf: (byName['Photo item']||{}).confidence };
        }""")
        check("provenance: logged meals carry tier (EXACT stored, photo defaults ESTIMATE)",
              pv.get("exact") == "EXACT" and pv.get("est") == "ESTIMATE",
              "exact=%s photo=%s conf=%s" % (pv.get("exact"), pv.get("est"), pv.get("estConf")))

        # ============================================================================
        # 2026-06-28 SHIPPED FEATURES — per-feature regression coverage.
        # Was the crown gap: the gate had ZERO checks for these 9 features, so any of them
        # could silently break while the gate still reported all-green. Each check below drives
        # the REAL app (same as the rest of the gate); a negative test proves the checks bite.
        # ============================================================================

        # recents: /api/recents -> #recentsCard rows; tapping a row re-logs (POST /api/meals)
        rec = page.evaluate("""async () => {
            switchTab('scan');
            var real = window.api, posted = 0;
            window.api = function(u, opts){
                if (u.indexOf('/api/recents') >= 0) return Promise.resolve({ recents: [
                    { name:'Greek yogurt', calories:120, protein_g:17, carbs_g:9, fat_g:0, source:'Recent', accuracy_tier:'estimate' },
                    { name:'Banana', calories:105, protein_g:1, carbs_g:27, fat_g:0 } ] });
                if (u.indexOf('/api/meals') >= 0 && opts && opts.method === 'POST'){ posted++; return Promise.resolve({ ok:true, id:1 }); }
                return real(u, opts);
            };
            await loadRecents();
            var card = document.getElementById('recentsCard');
            var rows = document.querySelectorAll('#recentsList .recent-row');
            var shown = !!card && card.style.display === 'block';
            if (rows[0]) rows[0].click();
            await new Promise(function(r){ setTimeout(r, 250); });
            window.api = real;
            return { shown: shown, rows: rows.length, posted: posted };
        }""")
        check("recents: card renders deduped rows + one-tap re-log posts a meal",
              rec["shown"] and rec["rows"] == 2 and rec["posted"] == 1,
              "shown=%s rows=%s posted=%s" % (rec["shown"], rec["rows"], rec["posted"]))

        # NEGATIVE TEST: prove the recents check has TEETH — if /api/recents returns nothing, the card
        # hides and rows==0, i.e. a broken feature WOULD fail the positive check above. This is the
        # guard-the-guard: it confirms the per-feature checks actually catch a regression.
        rec_neg = page.evaluate("""async () => {
            var real = window.api;
            window.api = function(u, opts){ if (u.indexOf('/api/recents') >= 0) return Promise.resolve({ recents: [] }); return real(u, opts); };
            await loadRecents();
            var card = document.getElementById('recentsCard');
            window.api = real;
            // The positive check asserts card.style.display==='block' (shown). A broken/empty feature must
            // flip that to hidden — that's the discriminating signal proving the check has teeth.
            return { hidden: !card || card.style.display === 'none' };
        }""")
        check("negative test: a BROKEN recents feature is caught (empty -> card hidden)",
              rec_neg["hidden"], "card hidden when feed empty=%s" % rec_neg["hidden"])

        # fasting: startFast -> #fastTime + #fastEat (eating window); endFast -> idle (Start button back)
        fast = page.evaluate("""() => {
            switchTab('today');
            try { localStorage.removeItem('snapcal_fast'); } catch(e){}
            startFast();
            var t = document.getElementById('fastTime'), eat = document.getElementById('fastEat'), endb = document.getElementById('fastEndBtn');
            var running = !!t && !!eat && !!endb && /opens at|window is open/i.test(eat.textContent);
            endFast();
            var idle = !!document.getElementById('fastStartBtn') && !document.getElementById('fastTime');
            return { running: running, idle: idle };
        }""")
        check("fasting: start shows the live timer + eating window; end returns to idle",
              fast["running"] and fast["idle"], "running=%s idle=%s" % (fast["running"], fast["idle"]))

        # net-carbs (keto): toggle ON -> .nf-net row = carbs - fiber (floored); OFF -> hidden
        ncb = page.evaluate("""() => {
            setNetCarbs(true);  var on = netCarbRow(20, 5);
            setNetCarbs(false); var off = netCarbRow(20, 5);
            return { on: on, off: off };
        }""")
        check("net-carbs: ON renders 'Net carbs' = carbs-fiber; OFF hides it",
              ("nf-net" in ncb["on"]) and ("15 g" in ncb["on"]) and ncb["off"] == "",
              "on='%s' off='%s'" % (ncb["on"][:60], ncb["off"]))

        # gentle (ED-safe) mode: ON -> #ringBig hidden + #gentleBanner shown; chat payload carries gentle:true
        gen = page.evaluate("""async () => {
            switchTab('today');
            setGentle(false);
            var bigVisOff = getComputedStyle(document.getElementById('ringBig')).display !== 'none';
            setGentle(true);
            var bigHidden = getComputedStyle(document.getElementById('ringBig')).display === 'none';
            var bannerShown = getComputedStyle(document.getElementById('gentleBanner')).display !== 'none';
            var real = window.api, sentGentle = null;
            window.api = function(u, opts){ if (u.indexOf('/api/chat') >= 0){ try { sentGentle = JSON.parse(opts.body).gentle; } catch(e){} return Promise.resolve({ reply:'ok' }); } return real(u, opts); };
            openVoice(); sendChat('how am I doing?');
            await new Promise(function(r){ setTimeout(r, 300); });
            window.api = real; closeVoice(); setGentle(false);
            return { bigVisOff: bigVisOff, bigHidden: bigHidden, bannerShown: bannerShown, sentGentle: sentGentle };
        }""")
        check("gentle mode: hides the calorie ring + shows the balance banner; chat sends gentle:true",
              gen["bigVisOff"] and gen["bigHidden"] and gen["bannerShown"] and gen["sentGentle"] is True,
              "ringVisOff=%s ringHidden=%s banner=%s chatGentle=%s" % (gen["bigVisOff"], gen["bigHidden"], gen["bannerShown"], gen["sentGentle"]))

        # micros: microsPanel renders a grouped, collapsible "Vitamins & minerals (N)" panel; empty -> hidden
        mic = page.evaluate("""() => {
            var html = microsPanel({ mufa_g:5, pufa_g:2, trans_fat_g:0.1, cholesterol_mg:30, magnesium_mg:40, zinc_mg:2,
                phosphorus_mg:120, vita_mcg:300, vitd_mcg:1, vite_mg:2, vitk_mcg:10, b1_mg:0.1, b2_mg:0.2, b3_mg:1,
                b6_mg:0.3, folate_mcg:50, b12_mcg:0.5 });
            var m = html.match(/Vitamins &amp; minerals \\((\\d+)\\)/);
            var none = microsPanel({});
            return { count: m ? parseInt(m[1],10) : 0, grouped: /nf-grp/.test(html), emptyHidden: none === '' };
        }""")
        check("micros: 'Vitamins & minerals (N)' panel renders grouped; empty -> hidden",
              mic["count"] >= 15 and mic["grouped"] and mic["emptyHidden"],
              "count=%s grouped=%s emptyHidden=%s" % (mic["count"], mic["grouped"], mic["emptyHidden"]))

        # barcode sanity layer (the 2026-07-14 Funyuns catch): crowd label data gets CHECKED, never
        # blindly trusted. Deterministic fixtures = the three real breakages found that day, run
        # through app.label_sanity offline (no Open Food Facts network dependency in the gate).
        import app as _app
        fun = _app.label_sanity(67.9, 1.0, 14.0, 4.5, 0)        # Funyuns: bad kcal + 1000x-low sodium
        nut = _app.label_sanity(200.0, 2.0, 21.0, 11.0, 15)     # Nutella: clean record, must pass through
        cok = _app.label_sanity(0.0, 0.0, 0.0, 0.0, 142000)     # Coke Zero: mg typed into the grams field
        check("barcode: label_sanity fixes bad kcal (Atwater), drops typo sodium both ways, keeps clean data",
              fun[0] == 100 and fun[1] is None and "calories_recomputed" in fun[2] and "sodium_implausible" in fun[2]
              and nut == (200.0, 15, []) and cok[1] is None and "sodium_implausible" in cok[2],
              "funyuns=%s nutella=%s coke=%s" % (fun, nut, cok))

        # export: GET /api/export.csv -> text/csv with a header row
        exp = page.evaluate("""async () => {
            try { var r = await fetch('/api/export.csv', { headers: { 'X-Device-Id': 'gate_export' } });
                  var ct = r.headers.get('content-type') || ''; var txt = await r.text();
                  return { ct: ct, firstLine: (txt.split('\\n')[0] || '').trim() }; }
            catch(e){ return { ct: 'err:'+e.message, firstLine: '' }; }
        }""")
        check("export: /api/export.csv returns text/csv with a header row",
              ("text/csv" in exp["ct"]) and exp["firstLine"].startswith("Date,Time,Food"),
              "ct=%s header='%s'" % (exp["ct"], exp["firstLine"][:40]))

        # health sync (hub move): POST then GET /api/health round-trips steps/active-cal/weight
        hl = page.evaluate("""async () => {
            var H = { 'Content-Type':'application/json', 'X-Device-Id':'gate_health' };
            await fetch('/api/health', { method:'POST', headers:H, body: JSON.stringify({ steps:8200, active_cal:320, weight:181.5, source:'gate' }) });
            var r = await fetch('/api/health', { headers:H }); var d = await r.json();
            var t = d.today || {};
            return { steps: t.steps, cal: t.active_cal, weight: t.weight };
        }""")
        check("health: POST then GET /api/health round-trips steps/active-cal/weight",
              hl["steps"] == 8200 and hl["cal"] == 320 and abs((hl["weight"] or 0) - 181.5) < 0.01,
              "steps=%s cal=%s weight=%s" % (hl["steps"], hl["cal"], hl["weight"]))

        # tester feedback loop (2026-07-19): in-app report card + API round-trip + fail-closed admin
        fb = page.evaluate("""async () => {
            var H = { 'Content-Type':'application/json', 'X-Device-Id':'gate_feedback' };
            var ok = await (await fetch('/api/feedback', { method:'POST', headers:H,
                        body: JSON.stringify({ text:'gate test report', category:'bug' }) })).json();
            var empty = (await fetch('/api/feedback', { method:'POST', headers:H,
                        body: JSON.stringify({ text:'' }) })).status;
            var admin = (await fetch('/api/feedback/admin', { headers:H })).status;
            return { posted: !!(ok && ok.ok && ok.id), emptyStatus: empty, adminStatus: admin,
                     uiCard: !!(document.getElementById('fbSend') && document.getElementById('fbText')
                                && document.getElementById('fbCats')) };
        }""")
        check("feedback: POST /api/feedback stores a report; empty text -> 400",
              fb["posted"] and fb["emptyStatus"] == 400, "posted=%s empty=%s" % (fb["posted"], fb["emptyStatus"]))
        check("feedback: /api/feedback/admin is FAIL-CLOSED without the admin key (403)",
              fb["adminStatus"] == 403, "status=%s" % fb["adminStatus"])
        check("feedback: 'Report a problem' card present in Profile (cats + text + send)",
              fb["uiCard"], "uiCard=%s" % fb["uiCard"])
        fbui = page.evaluate("""async () => {
            var ta = document.getElementById('fbText'), btn = document.getElementById('fbSend');
            ta.value = 'gate ui test: the search gave me pork';
            btn.click();
            await new Promise(r => setTimeout(r, 1500));
            var st = document.getElementById('fbStatus');
            return { thanks: !!(st && /got it/i.test(st.textContent || '')), cleared: ta.value === '' };
        }""")
        check("feedback: sending from the UI shows the thank-you state + clears the box",
              fbui["thanks"] and fbui["cleared"], "thanks=%s cleared=%s" % (fbui["thanks"], fbui["cleared"]))

        # push: GET /api/push/key exposes a VAPID key; POST /api/push/test with no sub -> 404 not_subscribed
        psh = page.evaluate("""async () => {
            var k = await (await fetch('/api/push/key')).json();
            var tr = await fetch('/api/push/test', { method:'POST', headers:{ 'X-Device-Id':'gate_push_nosub' } });
            var tj = await tr.json().catch(function(){ return {}; });
            return { hasKey: !!(k && k.key), testStatus: tr.status, testErr: tj.error };
        }""")
        check("push: /api/push/key has a key; /api/push/test with no sub -> 404 not_subscribed",
              psh["hasKey"] and psh["testStatus"] == 404 and psh["testErr"] == "not_subscribed",
              "hasKey=%s status=%s err=%s" % (psh["hasKey"], psh["testStatus"], psh["testErr"]))

        # workout burn (shipped 2026-06-28): POST then GET /api/exercise round-trips burned cals + DELETE clears;
        # and the burned total adds BACK into the calorie budget (the calorie ring shows more remaining).
        wk = page.evaluate("""async () => {
            var H = { 'Content-Type':'application/json', 'X-Device-Id':'gate_wo' };
            var post = await (await fetch('/api/exercise', { method:'POST', headers:H, body: JSON.stringify({ date:'2099-02-02', name:'Walk', minutes:30, calories:250 }) })).json();
            var g1 = await (await fetch('/api/exercise?date=2099-02-02', { headers:H })).json();
            await fetch('/api/exercise/' + post.id, { method:'DELETE', headers:H });
            var g2 = await (await fetch('/api/exercise?date=2099-02-02', { headers:H })).json();
            // budget math: burned cals increase "remaining" on the ring (add-only; 0 when none logged)
            switchTab('today'); setGentle(false);
            profile.daily_calories = 2000; todayData.totals = { calories:500, protein_g:0, carbs_g:0, fat_g:0 };
            todayData.burned = 0; todayData.workouts = []; renderToday();
            var before = parseInt((document.getElementById('ringBig').textContent||'0').replace(/[^0-9]/g,''), 10);
            todayData.burned = 300; renderToday();
            var after = parseInt((document.getElementById('ringBig').textContent||'0').replace(/[^0-9]/g,''), 10);
            todayData.burned = 0; renderToday();
            return { burned1: g1.burned, n1: (g1.workouts||[]).length, burned2: g2.burned, before: before, after: after };
        }""")
        check("workout: POST/GET/DELETE /api/exercise round-trips; burned cals add back into the budget",
              wk["burned1"] == 250 and wk["n1"] == 1 and wk["burned2"] == 0 and (wk["after"] - wk["before"]) == 300,
              "burned=%s->del %s, ring +%s" % (wk["burned1"], wk["burned2"], wk["after"] - wk["before"]))

        # custom recipes (shipped 2026-06-28): POST/GET/DELETE /api/myrecipes round-trips a multi-ingredient
        # recipe; the builder's _mrTotals sums ingredient macros.
        mr = page.evaluate("""async () => {
            var H = { 'Content-Type':'application/json', 'X-Device-Id':'gate_mr' };
            var post = await (await fetch('/api/myrecipes', { method:'POST', headers:H, body: JSON.stringify({
                name:'Test bowl', items:[{name:'oats',calories:150,protein_g:5},{name:'banana',calories:105,protein_g:1}],
                calories:255, protein_g:6, carbs_g:0, fat_g:0 }) })).json();
            var g1 = await (await fetch('/api/myrecipes', { headers:H })).json();
            var r = (g1.recipes||[]).filter(function(x){ return x.id===post.id; })[0] || {};
            await fetch('/api/myrecipes/' + post.id, { method:'DELETE', headers:H });
            var g2 = await (await fetch('/api/myrecipes', { headers:H })).json();
            var t = _mrTotals([{calories:150,protein_g:5},{calories:105,protein_g:1}]);
            return { name:r.name, cal:r.calories, items:(r.items||[]).length,
                     gone:(g2.recipes||[]).filter(function(x){ return x.id===post.id; }).length===0,
                     sumCal:t.calories, sumPro:t.protein_g };
        }""")
        check("recipes: POST/GET/DELETE /api/myrecipes round-trips; builder sums ingredient macros",
              mr["name"] == "Test bowl" and mr["cal"] == 255 and mr["items"] == 2 and mr["gone"]
              and mr["sumCal"] == 255 and mr["sumPro"] == 6,
              "name=%s cal=%s items=%s gone=%s sum=%s/%s" % (mr["name"], mr["cal"], mr["items"], mr["gone"], mr["sumCal"], mr["sumPro"]))

        # body measurements (shipped 2026-06-28): POST/GET /api/measurements round-trips; same-date partial logs
        # keep prior fields (COALESCE upsert); latest/earliest power the "since you started" delta.
        ms = page.evaluate("""async () => {
            var H = { 'Content-Type':'application/json', 'X-Device-Id':'gate_ms' };
            await fetch('/api/measurements', { method:'POST', headers:H, body: JSON.stringify({ date:'2099-03-01', waist:40, hip:44 }) });
            await fetch('/api/measurements', { method:'POST', headers:H, body: JSON.stringify({ date:'2099-03-01', chest:42 }) });   // partial, same date
            await fetch('/api/measurements', { method:'POST', headers:H, body: JSON.stringify({ date:'2099-03-15', waist:38 }) });    // newer date
            var g = await (await fetch('/api/measurements?days=400', { headers:H })).json();
            var byDate = {}; (g.measurements||[]).forEach(function(m){ byDate[m.date]=m; });
            var d1 = byDate['2099-03-01'] || {};
            return { d1waist:d1.waist, d1hip:d1.hip, d1chest:d1.chest,
                     latestWaist:(g.latest||{}).waist, earliestWaist:(g.earliest||{}).waist, n:(g.measurements||[]).length };
        }""")
        check("measurements: POST/GET round-trips; same-date partial keeps prior fields; latest/earliest set",
              ms["d1waist"] == 40 and ms["d1hip"] == 44 and ms["d1chest"] == 42
              and ms["latestWaist"] == 38 and ms["earliestWaist"] == 40 and ms["n"] >= 2,
              "d1=%s/%s/%s latest=%s earliest=%s n=%s" % (ms["d1waist"], ms["d1hip"], ms["d1chest"], ms["latestWaist"], ms["earliestWaist"], ms["n"]))

        # daily lesson (shipped 2026-06-28): /api/lessons feeds a daily CBT micro-lesson card; "Got it" marks
        # it read for the day and flips to the done state.
        lsn = page.evaluate("""async () => {
            switchTab('today');
            try { localStorage.removeItem('snapcal_lesson_read'); } catch(e){}
            _lessons = null; loadLesson();
            await new Promise(function(r){ var t=0; var iv=setInterval(function(){ t+=50; if (document.querySelector('#lessonBody .lsn-title') || t>4000){ clearInterval(iv); r(); } }, 50); });
            var html = (document.getElementById('lessonBody')||{}).innerHTML || '';
            var hasTitle = !!document.querySelector('#lessonBody .lsn-title');
            var hasTip = html.indexOf('Try it:') >= 0;
            var btn = document.getElementById('lessonGotItBtn'); var hadBtn = !!btn;
            if (btn) btn.click();
            var doneHtml = (document.getElementById('lessonBody')||{}).innerHTML || '';
            return { count: (_lessons||[]).length, hasTitle: hasTitle, hasTip: hasTip, hadBtn: hadBtn,
                     done: doneHtml.indexOf('Done for today') >= 0, flag: !!localStorage.getItem('snapcal_lesson_read') };
        }""")
        check("lesson: daily CBT micro-lesson renders (title+tip); 'Got it' marks read + shows done state",
              lsn["count"] >= 10 and lsn["hasTitle"] and lsn["hasTip"] and lsn["hadBtn"] and lsn["done"] and lsn["flag"],
              "count=%s title=%s tip=%s done=%s flag=%s" % (lsn["count"], lsn["hasTitle"], lsn["hasTip"], lsn["done"], lsn["flag"]))

        # onboarding: first run shows #onboard; "Maybe later" sets the flag (lsSet namespaces snapcal_c_) + hides
        onb = page.evaluate("""() => {
            try { localStorage.removeItem('snapcal_c_snapcal_onboarded'); } catch(e){}
            var ex = document.getElementById('onboard'); if (ex) ex.remove();
            showOnboarding();
            var ov = document.getElementById('onboard');
            var visible = !!ov && getComputedStyle(ov).display !== 'none';
            var before = localStorage.getItem('snapcal_c_snapcal_onboarded');
            var skip = ov && ov.querySelector('.locp-skip'); if (skip) skip.click();
            var after = localStorage.getItem('snapcal_c_snapcal_onboarded');
            var hidden = ov ? getComputedStyle(ov).display === 'none' : false;
            return { visible: visible, before: before, after: after, hidden: hidden };
        }""")
        check("onboarding: first run shows the welcome/permissions card; 'Maybe later' sets the flag + hides",
              onb["visible"] and not onb["before"] and onb["after"] and onb["hidden"],
              "visible=%s flag:%s->%s hidden=%s" % (onb["visible"], onb["before"], onb["after"], onb["hidden"]))

        # onboarding: the "Enable & get started" path must close the modal IMMEDIATELY and never block on
        # permission prompts (the freeze Shannon hit 2026-06-28: handler used `await requestAllPerms(); done()`,
        # and Notification.requestPermission()/getUserMedia() hang forever if the user doesn't answer).
        # A SYNCHRONOUS read right after .click() catches any regression: the fixed handler runs done() before
        # any await (hidden=true here); the old async-await-first handler would still read 'flex' at this point.
        onb2 = page.evaluate("""() => {
            try { localStorage.removeItem('snapcal_c_snapcal_onboarded'); } catch(e){}
            var ex = document.getElementById('onboard'); if (ex) ex.remove();
            showOnboarding();
            var ov = document.getElementById('onboard');
            var allow = ov && ov.querySelector('.locp-allow');
            allow.click();
            var hiddenSync = getComputedStyle(ov).display === 'none';   // must already be closed (no blocking await)
            var flag = localStorage.getItem('snapcal_c_snapcal_onboarded');
            var btnGone = !document.body.contains(allow) || allow.textContent !== '';  // never a dead "Setting up…" trap
            return { hiddenSync: hiddenSync, flag: flag, btnGone: btnGone };
        }""")
        check("onboarding: 'Enable & get started' closes IMMEDIATELY + sets flag (never hangs on permission prompts)",
              onb2["hiddenSync"] and onb2["flag"] == "1",
              "hiddenSync=%s flag=%s" % (onb2["hiddenSync"], onb2["flag"]))

        # QUIZ ONBOARDING (2026-07-15): confessional quiz + plan reveal + hard paywall for brand-new users.
        # Drives the WHOLE flow (14 steps) via real DOM clicks/inputs + reads the computed plan, mirroring the
        # 2 checks above's pattern of calling the entry function directly for determinism.
        qz = page.evaluate("""() => {
            try { localStorage.removeItem('snapcal_c_snapcal_onboarded'); localStorage.removeItem('snapcal_coach_intensity'); } catch(e){}
            var ex = document.getElementById('qzOnboard'); if (ex) ex.remove();
            showQuizOnboarding();
            var ov = document.getElementById('qzOnboard');
            var introVisible = !!ov && getComputedStyle(ov).display !== 'none' && ov.textContent.indexOf("Let's build YOUR plan") >= 0;
            function pick(v){ var b = ov.querySelector('.qz-opt[data-qv="' + v + '"]'); if (b) b.click(); }
            function next(){ var b = ov.querySelector('#qzNext'); if (b) b.click(); }
            next();                       // intro -> goal
            pick('recomp');                // goal -> sex (recomp, NOT maintain -> weight_goal page must show)
            pick('female');                // sex -> age
            ov.querySelector('#qzAge').value = '30'; next();      // age -> height
            ov.querySelector('#qzFt').value = '5'; ov.querySelector('#qzIn').value = '6'; next();   // height -> weight_cur
            ov.querySelector('#qzWCur').value = '160'; next();    // weight_cur -> weight_goal
            var sawWeightGoal = ov.textContent.indexOf('goal weight') >= 0;
            ov.querySelector('#qzWGoal').value = '140'; next();   // weight_goal -> activity
            pick('moderate');              // activity -> habits
            pick('consistent');            // habits -> derail
            pick('late_night');            // derail -> intensity
            pick('soft');                  // intensity -> textsize (2026-07-19: senior-a11y step)
            // TEXT-SIZE STEP (new): assert it renders, tap "Larger" -> zoom applies instantly, reset, advance
            var tsRow = ov.querySelector('#qzTsRow');
            var tsStepShown = !!tsRow;
            var tsLarger = tsRow ? tsRow.querySelector('button[data-ts="1.3"]') : null;
            if (tsLarger) tsLarger.click();
            var tsZoomApplied = String(document.documentElement.style.zoom) === '1.3';
            var tsNormal = tsRow ? tsRow.querySelector('button[data-ts="1"]') : null;
            if (tsNormal) tsNormal.click();                       // reset so the rest of the run is unscaled
            var tsZoomReset = String(document.documentElement.style.zoom || '1') === '1';
            next();                        // textsize -> perms
            next();                        // perms -> reveal
            var calEl = ov.querySelector('.qz-plan-cal');
            var revealHasCal = !!calEl && /\\d/.test(calEl.textContent) && calEl.textContent.indexOf('Cal') >= 0;
            next();                        // reveal -> paywall
            var pwHtml = ov.innerHTML;
            var hasPlans = ov.querySelectorAll('.pw-plan:not(.qz-opt)').length === 2;
            var hasDevSkip = !!ov.querySelector('#qzDevSkip');
            var flagBefore = localStorage.getItem('snapcal_c_snapcal_onboarded');
            ov.querySelector('#qzDevSkip').click();
            var flagAfter = localStorage.getItem('snapcal_c_snapcal_onboarded');
            var hiddenAfter = getComputedStyle(ov).display === 'none';
            return { introVisible: introVisible, sawWeightGoal: sawWeightGoal, revealHasCal: revealHasCal,
                     hasPlans: hasPlans, hasDevSkip: hasDevSkip, flagBefore: flagBefore, flagAfter: flagAfter,
                     hiddenAfter: hiddenAfter, dailyCal: (typeof profile !== 'undefined' ? profile.daily_calories : 0),
                     intensitySaved: localStorage.getItem('snapcal_coach_intensity'),
                     tsStepShown: tsStepShown, tsZoomApplied: tsZoomApplied, tsZoomReset: tsZoomReset };
        }""")
        check("quiz onboarding: fresh device shows the confessional quiz + reaches the plan reveal with a real computed calorie target",
              qz["introVisible"] and qz["sawWeightGoal"] and qz["revealHasCal"] and qz["dailyCal"] and int(qz["dailyCal"]) > 0,
              "intro=%s sawWeightGoal=%s revealHasCal=%s dailyCal=%s" % (qz["introVisible"], qz["sawWeightGoal"], qz["revealHasCal"], qz["dailyCal"]))
        check("quiz onboarding: coaching-intensity answer persists to localStorage for /api/chat to pick up",
              qz["intensitySaved"] == "soft", "intensitySaved=%s" % qz["intensitySaved"])
        check("quiz onboarding: TEXT-SIZE step shows (senior a11y discoverability); tapping 'Larger' zooms the app instantly, 'Normal' resets",
              qz["tsStepShown"] and qz["tsZoomApplied"] and qz["tsZoomReset"],
              "shown=%s zoomApplied=%s reset=%s" % (qz["tsStepShown"], qz["tsZoomApplied"], qz["tsZoomReset"]))

        # GENTLE / ED-SAFE MODE (2026-07-19, clinical judge's lock request): toggling gentle ON must hide the
        # numeric ring copy, show the word-state, and stamp gentle:true into the coach payload — and toggling
        # OFF must restore numbers exactly. Deterministic UI+payload assertions (no LLM call -> never flaky).
        gm = page.evaluate("""() => {
            function ringText(){ var rc = document.querySelector('.ring-center'); return rc ? rc.innerText.replace(/\\s+/g,' ') : ''; }
            var before = ringText();
            if (typeof setGentle === 'function') setGentle(true);
            else { localStorage.setItem('snapcal_gentle','1'); if (typeof applyGentle === 'function') applyGentle(); }
            var bodyFlag = document.body.classList.contains('gentle');
            var gw = document.getElementById('gentleWord');
            var wordShown = !!gw && getComputedStyle(gw).display !== 'none';
            var during = ringText();
            var payloadGentle = (typeof buildCoachBody === 'function') ? buildCoachBody().gentle === true : false;
            if (typeof setGentle === 'function') setGentle(false);
            else { localStorage.setItem('snapcal_gentle','0'); if (typeof applyGentle === 'function') applyGentle(); }
            var after = ringText();
            var numbersHidden = during.indexOf('Calories') < 0 && !/\\d{3,}/.test(during);
            var restored = /Calories/.test(after) || /\\d/.test(after);
            return { bodyFlag: bodyFlag, wordShown: wordShown, numbersHidden: numbersHidden,
                     payloadGentle: payloadGentle, restored: restored, during: during.slice(0, 40) };
        }""")
        check("gentle mode: ON hides ring numbers + shows word-state + coach payload carries gentle:true; OFF restores numbers",
              gm["bodyFlag"] and gm["wordShown"] and gm["numbersHidden"] and gm["payloadGentle"] and gm["restored"],
              "body=%s word=%s hidden=%s payload=%s restored=%s during='%s'" % (
                  gm["bodyFlag"], gm["wordShown"], gm["numbersHidden"], gm["payloadGentle"], gm["restored"], gm["during"]))
        check("quiz onboarding: paywall step REUSES the real plan-selector (2 plans) + a closed-testing dev bypass",
              qz["hasPlans"] and qz["hasDevSkip"], "hasPlans=%s hasDevSkip=%s" % (qz["hasPlans"], qz["hasDevSkip"]))
        check("quiz onboarding: dev bypass completes onboarding (sets the flag, hides the overlay) without a purchase",
              not qz["flagBefore"] and qz["flagAfter"] == "1" and qz["hiddenAfter"],
              "before=%s after=%s hidden=%s" % (qz["flagBefore"], qz["flagAfter"], qz["hiddenAfter"]))

        # Skip-logic lock: 'Maintain' goal must NEVER show the goal-weight page (the calc uses current weight).
        qzSkip = page.evaluate("""() => {
            try { localStorage.removeItem('snapcal_c_snapcal_onboarded'); } catch(e){}
            var ex = document.getElementById('qzOnboard'); if (ex) ex.remove();
            showQuizOnboarding();
            var ov = document.getElementById('qzOnboard');
            function pick(v){ var b = ov.querySelector('.qz-opt[data-qv="' + v + '"]'); if (b) b.click(); }
            function next(){ var b = ov.querySelector('#qzNext'); if (b) b.click(); }
            next(); pick('maintain'); pick('male');
            ov.querySelector('#qzAge').value = '40'; next();
            ov.querySelector('#qzFt').value = '6'; ov.querySelector('#qzIn').value = '0'; next();
            ov.querySelector('#qzWCur').value = '190'; next();   // weight_cur -> should skip straight to ACTIVITY
            var landedOnActivity = ov.textContent.indexOf('How active are you') >= 0;
            var ex2 = document.getElementById('qzOnboard'); if (ex2) ex2.remove();
            return { landedOnActivity: landedOnActivity };
        }""")
        check("quiz onboarding: 'Maintain' goal skips the goal-weight page",
              qzSkip["landedOnActivity"], "landedOnActivity=%s" % qzSkip["landedOnActivity"])

        # Add-only lock: an ALREADY-onboarded device must NEVER see the quiz (or the old card) again on reload —
        # the new-user-only gate must not regress existing/verified users' experience.
        page.evaluate("() => { try { localStorage.setItem('snapcal_c_snapcal_onboarded', '1'); } catch(e){} }")
        page.reload(wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1200)   # let the 600ms auto-onboarding timer fire if it wrongly would
        existingUserState = page.evaluate("""() => {
            var qo = document.getElementById('qzOnboard'), ob = document.getElementById('onboard');
            return { qzShown: !!qo && getComputedStyle(qo).display !== 'none',
                     obShown: !!ob && getComputedStyle(ob).display !== 'none' };
        }""")
        check("quiz onboarding: an already-onboarded device is NEVER shown the quiz (or the old card) again — add-only law",
              not existingUserState["qzShown"] and not existingUserState["obShown"],
              "qzShown=%s obShown=%s" % (existingUserState["qzShown"], existingUserState["obShown"]))
        page.evaluate("() => { window.premiumActive = true; try { goal = 'lose_weight'; } catch(e){} }")   # restore gate state after reload for any later checks

        # ==================== 2026-07-19 SAFETY-GUARD LOCKS (RD-review round 4) ====================
        # The clinical judge's demand: "only one of eight safety behaviors is regression-locked."
        # Prompt-directive guards are the most silently-regressable layer — lock each REQUIRED directive
        # string in the SOURCE (deterministic; no flaky LLM probes), + the two quiz gates as live UI probes.
        _app_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"),
                        encoding="utf-8", errors="replace").read()
        _GUARDS = [
            ("distress response UN-GATED (fires in every mode)", "REGARDLESS of any mode or setting"),
            ("live Alliance crisis line in chat prompt", "1-866-662-1235"),
            ("pregnancy no-deficit rule", "pregnant or breastfeeding"),
            ("under-eating guard (no praising <1000 kcal)", "eating too little"),
            ("GLP-1 never-discuss-medication rule", "NEVER comment on medication dose"),
            ("fasting contraindications exception", "do NOT encourage extending"),
            ("gentle mode overrides ALL prompt rules", "OVERRIDES ALL OTHER RULES"),
            ("hard-mode yields to gentle (no numbers)", "gentle's no-numbers rules OVERRIDE"),
        ]
        for _gname, _gstr in _GUARDS:
            check("safety-lock: %s — directive present in app.py" % _gname, _gstr in _app_src, "needle=%r" % _gstr)

        # ---- 2026-07-19 failure-class locks (prose->gate, same day as the wounds) ----
        # (a) NAME-COLLISION HOISTING: a duplicate top-level `function name(` in the single-file app lets
        # the later declaration win the hoist and silently kill every var below (the coachKey incident).
        _idx_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"),
                        encoding="utf-8", errors="replace").read()
        import re as _re2
        _toplevel_fns = _re2.findall(r"^function ([A-Za-z_$][\w$]*)\s*\(", _idx_src, _re2.M)
        _dupes = sorted({n for n in _toplevel_fns if _toplevel_fns.count(n) > 1})
        check("failure-class lock: no duplicate TOP-LEVEL function declarations in index.html (hoist-collision guard)",
              not _dupes, "dupes=%s" % (_dupes or "none"))
        # (b) ARTIFACT-FORMAT MISMATCH: image bytes must match their extension (the JPEG-in-.png incident —
        # desktop sniffs past it, devices show broken images).
        _bad_magic = []
        _magic = {".png": b"\x89PNG", ".jpg": b"\xff\xd8", ".jpeg": b"\xff\xd8", ".webp": b"RIFF", ".gif": b"GIF8"}
        _img_dirs = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", d) for d in ("coaches", "img")]
        for _d in _img_dirs:
            if not os.path.isdir(_d):
                continue
            for _f in os.listdir(_d):
                _ext = os.path.splitext(_f)[1].lower()
                if _ext in _magic:
                    with open(os.path.join(_d, _f), "rb") as _fh:
                        if not _fh.read(8).startswith(_magic[_ext]):
                            _bad_magic.append(_f)
        check("failure-class lock: every shipped image's magic bytes match its extension (JPEG-in-.png guard)",
              not _bad_magic, "mismatched=%s" % (_bad_magic or "none"))
        # (c) TYPEAHEAD FOOD SEARCH (Mom's 'Extra butt' -> Boston-butt pork catch, 2026-07-19): a
        # partially-typed word must resolve to the food being typed, and full exact queries must keep
        # winning. Deterministic — recorded USDA pools, no network. Locks _pick_food's prefix scoring
        # AND the merged-pool ordering that made 'Almond butter' beat plain 'Butter,' on idx tiebreak.
        # Fixtures mirror the REAL two-stage production flow: raw pool -> full-word-coverage guard ->
        # (only if uncovered) wildcard merge -> re-pick. Testing the picker alone on a merged pool is a
        # STRONGER property than production has (the guard is what protects 'boston butt') — the gate's
        # own maiden run caught that overreach; keep the check flow-shaped.
        import app as _appmod
        _PORK = {"description": "Pork, fresh, shoulder, (Boston butt), blade (steaks), separable lean and fat, raw", "fdcId": 1}
        _RAW = [_PORK] * 8 + [{"description": "Oil, olive, extra light", "fdcId": 9}]
        _WILD = [{"description": "Almond butter, creamy", "fdcId": 10},        # wildcard pool, alphabetical
                 {"description": "Biscuits, plain or buttermilk, dry mix", "fdcId": 11},
                 {"description": "Butter, Clarified butter (ghee)", "fdcId": 12}]

        def _flow_pick(query):
            _f = _appmod._pick_food(_RAW, query)
            _dt = set(_appmod._toks(_f.get("description") or ""))
            _qw = _appmod._toks(query)
            if all(w in _dt or (w + "s") in _dt for w in _qw):
                return _f["description"], False                    # covered: retry must NOT fire
            return _appmod._pick_food(_RAW + _WILD, query)["description"], True

        _t1, _retried1 = _flow_pick("extra butt")
        check("failure-class lock: typeahead 'extra butt' retries wildcard + picks Butter (not pork/almond)",
              _retried1 and _t1.lower().startswith("butter,"), "picked=%r retried=%s" % (_t1, _retried1))
        _t2, _retried2 = _flow_pick("boston butt")
        check("failure-class lock: exact 'boston butt' keeps pork and NEVER triggers the wildcard retry",
              (not _retried2) and _t2.lower().startswith("pork"), "picked=%r retried=%s" % (_t2, _retried2))
        check("failure-class lock: typeahead wildcard retry present in /api/nutrition",
              "qwords[-1] + \"*\"" in _app_src and "_fdc_search" in _app_src, "retry code present")
        # (d) SCAN OMISSION (Mom's missed-cabbage / missed-cheese class, 2026-07-19): the analyze prompt
        # must carry the sweep-the-whole-frame enumeration directive — visible low-calorie items are items.
        check("failure-class lock: analyze prompt orders a full-frame sweep (missed-cabbage guard)",
              "ENUMERATE EVERY VISIBLE FOOD" in _app_src and "If you can SEE\n   it, LIST it" in _app_src,
              "enumeration directive present")

        # UI probe: quiz REJECTS a 17-year-old (stays on the age step) and ACCEPTS 30.
        agegate = page.evaluate("""() => {
            try { localStorage.removeItem('snapcal_c_snapcal_onboarded'); } catch(e){}
            var ex = document.getElementById('qzOnboard'); if (ex) ex.remove();
            showQuizOnboarding();
            var ov = document.getElementById('qzOnboard');
            function pick(v){ var b = ov.querySelector('.qz-opt[data-qv="' + v + '"]'); if (b) b.click(); }
            function next(){ var b = ov.querySelector('#qzNext'); if (b) b.click(); }
            next(); pick('recomp'); pick('female');
            ov.querySelector('#qzAge').value = '17'; next();
            var rejected17 = !!ov.querySelector('#qzAge');            // still on the age step
            ov.querySelector('#qzAge').value = '30'; next();
            var advanced30 = !ov.querySelector('#qzAge');             // moved on to height
            // continue to weight_goal and probe the BMI floor: 5'6" goal 95 lb (BMI ~15) must be rejected
            ov.querySelector('#qzFt').value = '5'; ov.querySelector('#qzIn').value = '6'; next();
            ov.querySelector('#qzWCur').value = '160'; next();
            ov.querySelector('#qzWGoal').value = '95'; next();
            var rejectedUnderweight = !!ov.querySelector('#qzWGoal'); // still on the goal-weight step
            ov.querySelector('#qzWGoal').value = '140'; next();
            var advancedHealthy = !ov.querySelector('#qzWGoal');
            var dev = ov.querySelector('#qzDevSkip'); // finish + clean up via the dev skip if reachable
            try { pick('moderate'); pick('consistent'); pick('late_night'); pick('soft'); next(); next(); next();
                  dev = ov.querySelector('#qzDevSkip'); if (dev) dev.click(); } catch(e){}
            if (ov && getComputedStyle(ov).display !== 'none') ov.remove();
            return { rejected17: rejected17, advanced30: advanced30,
                     rejectedUnderweight: rejectedUnderweight, advancedHealthy: advancedHealthy };
        }""")
        check("safety-lock UI: quiz rejects age 17 + accepts 30 (18+ gate live)",
              agegate["rejected17"] and agegate["advanced30"], str(agegate))
        check("safety-lock UI: quiz rejects underweight goal (BMI<18.5 floor) + accepts healthy goal",
              agegate["rejectedUnderweight"] and agegate["advancedHealthy"], str(agegate))

        # ==================== 2026-07-15 AUDIT PUNCH LIST ====================

        # P0-1: TAB BAR LEGIBILITY. Measure the RENDERED (post-transform) size, same method as the
        # audit's pixel-sample screenshot — getBoundingClientRect for the icon (already post-transform),
        # and computed unscaled font-size x the tabbtn's own CSS transform scaleX for the label (a plain
        # computed-style read would miss the .97 recede scale the audit's real bug was hiding inside).
        page.evaluate("() => switchTab('today')")   # today = active -> eatout/scan/history/profile = inactive
        tabbar = page.evaluate("""() => {
            function scaleOf(el){
                var m = getComputedStyle(el).transform;
                if (!m || m === 'none') return 1;
                var mm = m.match(/matrix\\(([^)]+)\\)/);
                if (!mm) return 1;
                return parseFloat(mm[1].split(',')[0]);
            }
            function luminance(rgb){
                var m = rgb.match(/[\\d.]+/g).map(Number);
                var lin = m.slice(0,3).map(function(v){ v/=255; return v<=0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055,2.4); });
                return 0.2126*lin[0] + 0.7152*lin[1] + 0.0722*lin[2];
            }
            var btn = document.querySelector('.tabbtn[data-tab="eatout"]');   // inactive (today is active)
            var span = btn.querySelector('span'), svg = btn.querySelector('svg');
            var s = scaleOf(btn);
            var renderedFont = parseFloat(getComputedStyle(span).fontSize) * s;
            var svgRect = svg.getBoundingClientRect();
            var strokeW = parseFloat(getComputedStyle(svg).strokeWidth || svg.getAttribute('stroke-width'));
            var ink = getComputedStyle(span).color;
            var navBg = getComputedStyle(document.querySelector('.nav-inner')).backgroundColor;
            var alphaM = navBg.match(/[\\d.]+/g);
            var navAlpha = alphaM && alphaM.length > 3 ? parseFloat(alphaM[3]) : 1;
            var Lwhite = 1, Link = luminance(ink);
            var contrastOnWhite = (Lwhite + 0.05) / (Link + 0.05);
            return { renderedFont: renderedFont, svgW: svgRect.width, svgH: svgRect.height, strokeW: strokeW,
                     ink: ink, navBg: navBg, navAlpha: navAlpha, contrastOnWhite: contrastOnWhite, scale: s };
        }""")
        check("P0-1 tab bar: inactive label renders >= 14.5px (was ~13.33px/11px base x .92 scale)",
              tabbar["renderedFont"] >= 14.5, "renderedFont=%.2fpx (base x scale=%.2f)" % (tabbar["renderedFont"], tabbar["scale"]))
        check("P0-1 tab bar: inactive icon renders >= 24px (was ~22px)",
              tabbar["svgW"] >= 24 and tabbar["svgH"] >= 24, "svg=%.1fx%.1fpx" % (tabbar["svgW"], tabbar["svgH"]))
        check("P0-1 tab bar: inactive icon stroke-width >= 2.1 (was 1.8)",
              tabbar["strokeW"] >= 2.1, "strokeW=%s" % tabbar["strokeW"])
        check("P0-1 tab bar: inactive ink >= 8.5:1 contrast on a white backdrop (was ~6.7:1 rendered)",
              tabbar["contrastOnWhite"] >= 8.5, "ink=%s contrastOnWhite=%.2f:1" % (tabbar["ink"], tabbar["contrastOnWhite"]))
        check("P0-1 tab bar: nav glass raised to ~.80 opacity (was .62) so the backdrop never washes labels",
              tabbar["navAlpha"] >= 0.75, "navBg=%s alpha=%.2f" % (tabbar["navBg"], tabbar["navAlpha"]))

        # P0-2: COACH CAL VERDICT BURIED. DOM-order assertion (per the fix-brief's own fallback — the
        # vision /api/analyze call is too flaky for a headless gate) using the SAME window.analyzeResult +
        # renderScanCard() injection pattern already used above for the scan-result checks.
        page.evaluate("() => switchTab('scan')")
        coachtop = page.evaluate("""() => {
            var items = [];
            for (var i = 0; i < 6; i++) items.push({ name: 'Item ' + i, calories: 200, protein_g: 10, carbs_g: 20, fat_g: 8,
                fiber_g: 1, sugar_g: 2, sat_fat_g: 3, sodium_mg: 300 });
            window.analyzeResult = { items: items, mults: items.map(function(){ return 1; }),
                total: { calories: 1200, protein_g: 60, carbs_g: 120, fat_g: 48, fiber_g: 6, sugar_g: 12, sat_fat_g: 18, sodium_mg: 1800,
                         potassium_mg: 500, calcium_mg: 100, iron_mg: 2, vitamin_a_dv: 10, vitamin_c_dv: 10, vitamin_d_dv: 10, est_weight_g: 500, band_pct: 0 },
                health_score: 40, quality_grade: 'D', satiety: 'medium', good_flags: [], bad_flags: ['High sodium'],
                verdict: 'This meal is very high in calories, saturated fat, and sodium.',
                coach_tip: 'Opt for a grilled option and swap the fries for a side salad.',
                swaps: [], note: '' };
            renderScanCard();
            var top = document.getElementById('resultCoachTop'), items_el = document.getElementById('resultItems'),
                detail = document.getElementById('resultDetail');
            var topHasVerdict = !!top.querySelector('.verdict'), topHasTip = !!top.querySelector('.tip');
            var itemsFollowsTop = !!(top.compareDocumentPosition(items_el) & Node.DOCUMENT_POSITION_FOLLOWING);
            var detailHasVerdict = !!detail.querySelector('.verdict'), detailHasTip = !!detail.querySelector('.tip');   // must NOT duplicate
            var nutx = detail.querySelector('details.nutx');
            var nutxCollapsed = !!nutx && !nutx.open;
            var nutxHasMicro = !!nutx && /Micronutrients/.test(nutx.textContent) && /Smart metrics/.test(nutx.textContent);
            var topRect = top.getBoundingClientRect();
            return { topHasVerdict: topHasVerdict, topHasTip: topHasTip, itemsFollowsTop: itemsFollowsTop,
                     detailHasVerdict: detailHasVerdict, detailHasTip: detailHasTip,
                     nutxPresent: !!nutx, nutxCollapsed: nutxCollapsed, nutxHasMicro: nutxHasMicro, topTop: topRect.top };
        }""")
        check("P0-2 scan result: Coach Cal grade+tip render into the TOP slot, BEFORE the item list",
              coachtop["topHasVerdict"] and coachtop["topHasTip"] and coachtop["itemsFollowsTop"],
              str(coachtop))
        check("P0-2 scan result: grade+tip do NOT also duplicate further down in #resultDetail",
              not coachtop["detailHasVerdict"] and not coachtop["detailHasTip"], str(coachtop))
        check("P0-2 scan result: Micronutrients + Smart metrics collapsed behind a closed-by-default expander",
              coachtop["nutxPresent"] and coachtop["nutxCollapsed"] and coachtop["nutxHasMicro"], str(coachtop))

        # History's meal-detail sheet (openMealSheet) must be UNCHANGED — verdict/tip stay inline, no opts.
        histsheet = page.evaluate("""() => {
            var r = { items: [{ name: 'Test', calories: 100, protein_g: 5, carbs_g: 10, fat_g: 3 }],
                total: { calories: 100, protein_g: 5, carbs_g: 10, fat_g: 3, fiber_g: 0, sugar_g: 0, sat_fat_g: 0, sodium_mg: 0,
                         potassium_mg: 0, calcium_mg: 0, iron_mg: 0, vitamin_a_dv: 0, vitamin_c_dv: 0, vitamin_d_dv: 0, est_weight_g: 100, band_pct: 0 },
                health_score: 80, quality_grade: 'B', satiety: 'medium', good_flags: [], bad_flags: [],
                verdict: 'Solid choice.', coach_tip: 'Keep it up.', swaps: [] };
            var html = buildDetailHTML(r);
            return { hasVerdictInline: html.indexOf('class="verdict"') >= 0, hasTipInline: html.indexOf('class="tip"') >= 0,
                     hasCollapsedExpander: html.indexOf('class="nutx"') >= 0 };
        }""")
        check("P0-2 add-only: History's buildDetailHTML(r) (no opts) is UNCHANGED — verdict/tip stay inline, no collapse",
              histsheet["hasVerdictInline"] and histsheet["hasTipInline"] and not histsheet["hasCollapsedExpander"],
              str(histsheet))

        # P1-3: MIC FAB OVERLAP. The 58px FAB floats at bottom:88px (top edge 146px above the viewport
        # floor) — <main>'s bottom padding must clear that + a buffer, on every tab (viewport-independent
        # since padding is a fixed px value, so no phone-viewport swap needed for this lock).
        mainpad = page.evaluate("() => parseFloat(getComputedStyle(document.querySelector('main')).paddingBottom)")
        check("P1-3 mic FAB: <main> bottom padding clears the FAB's full extent (88+58=146px) + buffer",
              mainpad >= 160, "paddingBottom=%.0fpx" % mainpad)

        # P1-4: LOCATION PROMPT FIRES TWICE. (a) dismissing ("Not now") must ALSO prime the shared flag —
        # the copy says "asked once" full stop, not "once per Allow". (b) requestAllPerms() must prime the
        # flag SYNCHRONOUSLY (before its first await) so a location feature opened right after onboarding
        # never re-races it. (c) two consecutive snapGeo() calls (Coach Cal then Eat Out) must only ever
        # show the custom explainer ONCE.
        # NOTE: every await is Promise.race'd with a timeout — page.evaluate has NO default timeout, and
        # an unsettled browser-permission promise hung this gate for 10+ min twice on 2026-07-15. A stall
        # now returns 'timeout:<step>' in the trace and FAILS the check instead of hanging the run.
        locfix = page.evaluate("""async () => {
            var steps = [];
            function tmo(p, tag, ms){
                var settled = false;
                return Promise.race([
                    Promise.resolve(p).then(function(){ if (!settled){ settled = true; steps.push(tag); } }),
                    new Promise(function(r){ setTimeout(function(){ if (!settled){ settled = true; steps.push('timeout:' + tag); } r(); }, ms || 8000); })
                ]);
            }
            try { localStorage.removeItem('snapcal_c_snapcal_loc_primed'); } catch(e){}
            var ex = document.getElementById('locPrimer'); if (ex) ex.remove();
            await tmo(new Promise(function(resolve){
                showLocPrimer(function(){ resolve(); }, function(){ resolve(); });
                document.querySelector('#locPrimer .locp-skip').click();   // "Not now"
            }), 'dismiss');
            var flagAfterDismiss = localStorage.getItem('snapcal_c_snapcal_loc_primed');

            try { localStorage.removeItem('snapcal_c_snapcal_loc_primed'); } catch(e){}
            // The whole point of the fix: the flag must be set in the SYNCHRONOUS window before
            // requestAllPerms' first await. So call it, read the flag immediately, and do NOT await
            // the promise — headless Chrome's camera/notification prompts never settle. Fire-and-forget
            // is exactly how the real onboarding code calls it (Promise.resolve(requestAllPerms()).catch()).
            requestAllPerms().catch(function(){});
            var flagImmediatelyAfterCall = localStorage.getItem('snapcal_c_snapcal_loc_primed');

            try { localStorage.removeItem('snapcal_c_snapcal_loc_primed'); } catch(e){}
            var showCount = 0, origShow = window.showLocPrimer;
            window.showLocPrimer = function(onAllow, onDismiss){
                showCount++; origShow(onAllow, onDismiss);
                var b = document.querySelector('#locPrimer .locp-allow'); if (b) b.click();
            };
            await tmo(new Promise(function(resolve){ snapGeo(function(){ resolve(); }, function(){ resolve(); }); }), 'geo1');   // simulates Coach Cal opening
            await tmo(new Promise(function(resolve){ snapGeo(function(){ resolve(); }, function(){ resolve(); }); }), 'geo2');   // simulates Eat Out right after
            window.showLocPrimer = origShow;
            return { flagAfterDismiss: flagAfterDismiss, flagImmediatelyAfterCall: flagImmediatelyAfterCall,
                     showCount: showCount, steps: steps };
        }""")
        check("P1-4 location prompt: 'Not now' also primes the shared flag (explainer truly shows once, not once-per-Allow)",
              locfix["flagAfterDismiss"] == "1", "flagAfterDismiss=%s" % locfix["flagAfterDismiss"])
        check("P1-4 location prompt: requestAllPerms() primes the flag SYNCHRONOUSLY (closes the onboarding/Coach-Cal race)",
              locfix["flagImmediatelyAfterCall"] == "1", "flagImmediatelyAfterCall=%s" % locfix["flagImmediatelyAfterCall"])
        # NOTE on steps: 'dismiss' must resolve (pure DOM). geo1/geo2 CALLBACK timeouts are tolerated —
        # headless Chrome's geolocation flakes on rapid successive getCurrentPosition calls (hung this
        # gate twice on 2026-07-15) — because the behavior under test (show the explainer or not) is
        # decided SYNCHRONOUSLY inside snapGeo before geolocation is ever consulted; showCount is the
        # complete measurement of it.
        check("P1-4 location prompt: 2 consecutive location features (Coach Cal then Eat Out) show the explainer only ONCE",
              locfix["showCount"] == 1 and "dismiss" in locfix["steps"],
              "showCount=%s steps=%s" % (locfix["showCount"], locfix["steps"]))
        page.evaluate("() => { try { localStorage.setItem('snapcal_c_snapcal_loc_primed', '1'); } catch(e){} }")   # restore gate's primed state for any later checks

        # P1-5: ED-SUPPORT HELPLINE — its own visible, dignified card (not a buried 11.5px line).
        # 2026-07-19 RD-review fix: NEDA's helpline was DISCONTINUED in June 2023 (verified NPR/CBS) — the
        # card now carries the National Alliance for Eating Disorders line 1-866-662-1235 (licensed
        # clinicians, verified live) + crisis 988. The gate MUST lock the live number and MUST FAIL if the
        # dead NEDA number ever reappears anywhere in the page.
        neda = page.evaluate("""() => {
            var was = document.body.classList.contains('gentle');
            document.body.classList.add('gentle');
            var card = document.getElementById('nedaCard');
            var visible = !!card && getComputedStyle(card).display !== 'none';
            var hasNumber = visible && /1-866-662-1235/.test(card.textContent);
            var has988 = visible && /988/.test(card.textContent);
            var hasIcon = visible && !!card.querySelector('.neda-ic');
            var deadNumberAnywhere = /1-800-931-2237/.test(document.documentElement.innerHTML);
            if (!was) document.body.classList.remove('gentle');
            return { visible: visible, hasNumber: hasNumber, has988: has988, hasIcon: hasIcon,
                     deadNumberAnywhere: deadNumberAnywhere };
        }""")
        check("P1-5 ED-support helpline card: visible in Gentle mode w/ LIVE Alliance number 1-866-662-1235 + 988; dead NEDA number absent",
              neda["visible"] and neda["hasNumber"] and neda["has988"] and neda["hasIcon"] and not neda["deadNumberAnywhere"],
              str(neda))

        # P1-6: HISTORY EMPTY STATE — friendly skeleton + a working "+ Add a meal" shortcut to Today.
        hist = page.evaluate("""async () => {
            var real = window.api;
            window.api = function(u, opts){ if (u.indexOf('/api/history') >= 0) return Promise.resolve({ days: [] }); return real(u, opts); };
            await Promise.race([loadHistory(), new Promise(function(r){ setTimeout(r, 8000); })]);   // never hang the gate on a stray await inside loadHistory
            window.api = real;
            var empty = document.querySelector('#historyList .history-empty');
            var hasSkel = !!empty && !!empty.querySelector('.hist-skel');
            var hasCta = !!empty && !!document.getElementById('histAddMealBtn');
            if (hasCta) document.getElementById('histAddMealBtn').click();
            return { hasSkel: hasSkel, hasCta: hasCta };
        }""")
        page.wait_for_timeout(400)
        histNav = page.evaluate("() => document.getElementById('tab-today').classList.contains('active')")
        check("P1-6 history empty state: friendly skeleton + '+ Add a meal' present",
              hist["hasSkel"] and hist["hasCta"], str(hist))
        check("P1-6 history empty state: '+ Add a meal' shortcut jumps to Today",
              histNav, "today tab active=%s" % histNav)
        page.evaluate("() => { var a = document.getElementById('addMealCancelBtn'); if (a) a.click(); }")   # close the sheet it opened

        # 10. no JS errors
        check("no JS console / page errors", len(errors) == 0,
              ("; ".join(errors[:3])) if errors else "")

        browser.close()

    failed = [n for n, ok, d in results if not ok]
    bar = "=" * 60
    print("\n" + bar)
    print("REGRESSION GATE: %d checks  |  %d passed  |  %d FAILED"
          % (len(results), len(results) - len(failed), len(failed)))
    if failed:
        print("REGRESSED -> " + " | ".join(failed))
        print("This is a GOING-BACKWARDS bug. Fix before shipping.")
    else:
        print("All locked-in flows intact. Safe to ship.")
    print(bar)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
