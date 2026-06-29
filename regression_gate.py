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

BASE = "http://127.0.0.1:5177"
LAT, LNG = 40.2452, -75.6496          # Pottstown — chain-dense enough to exercise near + far bands
HERE = os.path.dirname(os.path.abspath(__file__))

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


def ensure_server():
    if server_up():
        return None
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

    from playwright.sync_api import sync_playwright
    errors = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        ctx = browser.new_context(geolocation={"latitude": LAT, "longitude": LNG},
                                  permissions=["geolocation"])
        page = ctx.new_page()
        page.route("**/api/nearby*", _route_nearby)   # deterministic near-me data (Overpass is too slow/flaky to gate on)

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
