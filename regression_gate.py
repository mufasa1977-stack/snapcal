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

    from playwright.sync_api import sync_playwright
    errors = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        ctx = browser.new_context(geolocation={"latitude": LAT, "longitude": LNG},
                                  permissions=["geolocation"])
        page = ctx.new_page()
        page.route("**/api/nearby*", _route_nearby)   # deterministic near-me data (Overpass is too slow/flaky to gate on)

        def on_console(m):
            t = m.text.lower()
            # ignore benign network noise (favicon, CDN/tile/logo loads) — those degrade gracefully;
            # we only fail on REAL JS errors (TypeError/ReferenceError) + uncaught exceptions (pageerror).
            if m.type == "error" and "failed to load resource" not in t and "favicon" not in t:
                errors.append(m.text[:160])
        page.on("console", on_console)
        page.on("pageerror", lambda e: errors.append(str(e)[:160]))

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
