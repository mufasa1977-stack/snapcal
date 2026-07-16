#!/usr/bin/env python3
"""One-off capture: verify the 2026-07-15 audit punch-list fixes in headless Chrome at the audit's
375x812 phone viewport and save AFTER screenshots + printed measurements (audit's own method) to
_review/fixes_2026-07-15/. Not part of the regression gate."""
import os
import sys
import json

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5177"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fixes_2026-07-15")
BURGER = os.path.join(os.path.dirname(HERE), "static", "img", "food", "burger.jpg")
os.makedirs(OUT, exist_ok=True)

report = {}

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel="chrome")
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, color_scheme="light",
                              geolocation={"latitude": 40.2452, "longitude": -75.6496},
                              permissions=["geolocation"])
    page = ctx.new_page()
    page.add_init_script("try{localStorage.setItem('snapcal_goal','lose_weight');"
                         "localStorage.setItem('snapcal_c_snapcal_onboarded','1');"
                         "localStorage.setItem('snapcal_c_snapcal_loc_primed','1');}catch(e){}")
    page.goto(BASE + "/?gate=1", wait_until="domcontentloaded", timeout=20000)
    page.evaluate("() => { window.premiumActive = true; try { goal = 'lose_weight'; } catch(e){} }")
    page.wait_for_timeout(900)

    # ---------- P0-1: tab bar (Today active; measure an INACTIVE tab) ----------
    tb = page.evaluate("""() => {
        function scaleOf(el){
            var m = getComputedStyle(el).transform;
            if (!m || m === 'none') return 1;
            var mm = m.match(/matrix\\(([^)]+)\\)/); if (!mm) return 1;
            return parseFloat(mm[1].split(',')[0]);
        }
        var btn = document.querySelector('.tabbtn[data-tab="eatout"]');
        var span = btn.querySelector('span'), svg = btn.querySelector('svg');
        var s = scaleOf(btn);
        return { baseFont: getComputedStyle(span).fontSize, scale: s,
                 renderedFont: (parseFloat(getComputedStyle(span).fontSize) * s).toFixed(2) + 'px',
                 fontWeight: getComputedStyle(span).fontWeight,
                 svgRect: svg.getBoundingClientRect().width.toFixed(1) + 'px',
                 strokeW: getComputedStyle(svg).strokeWidth,
                 ink: getComputedStyle(span).color,
                 navBg: getComputedStyle(document.querySelector('.nav-inner')).backgroundColor };
    }""")
    report["P0-1_tabbar"] = tb
    page.screenshot(path=os.path.join(OUT, "after_01_tabbar_today.png"))

    # pixel-sample the rendered label area (audit method): screenshot the nav strip only
    nav = page.query_selector("nav")
    nav.screenshot(path=os.path.join(OUT, "after_01b_tabbar_strip.png"))

    # ---------- P0-2: REAL burger.jpg gallery-upload analyze at 375x812 ----------
    page.evaluate("() => switchTab('scan')")
    page.wait_for_timeout(400)
    page.set_input_files("#galInput", BURGER)
    page.wait_for_timeout(600)
    # click Analyze
    page.evaluate("() => { var b = document.getElementById('analyzeBtn'); if (b) b.click(); }")
    # wait for the result card (vision call — allow long)
    try:
        page.wait_for_function("document.getElementById('resultCard').style.display === 'block'", timeout=90000)
        analyzed = True
    except Exception:
        analyzed = False
    page.wait_for_timeout(3200)   # audit spec: measure AFTER layout settles ~3s
    if analyzed:
        pos = page.evaluate("""() => {
            var top = document.getElementById('resultCoachTop');
            var tip = top ? top.querySelector('.tip') : null;
            var verdict = top ? top.querySelector('.verdict') : null;
            function absTop(el){ return el ? el.getBoundingClientRect().top + window.scrollY : null; }
            // scroll so the result card is where a user lands (analyze auto-scrolls; keep as-is), then
            // report DOCUMENT offsets — audit measured tip settle at 2777px document offset.
            return { coachTipDocTop: absTop(tip), verdictDocTop: absTop(verdict),
                     cardDocTop: absTop(document.getElementById('resultCard')),
                     itemsDocTop: absTop(document.getElementById('resultItems')),
                     nutxOpen: (function(){ var n = document.querySelector('#resultDetail details.nutx'); return n ? n.open : null; })(),
                     viewportH: window.innerHeight };
        }""")
        report["P0-2_coach_position"] = pos
        # scroll to the top of the result card — what the user sees when the result lands
        page.evaluate("() => { document.getElementById('resultCard').scrollIntoView({block:'start'}); }")
        page.wait_for_timeout(500)
        vis = page.evaluate("""() => {
            var tip = document.querySelector('#resultCoachTop .tip');
            var v = document.querySelector('#resultCoachTop .verdict');
            function inView(el){ if (!el) return null; var r = el.getBoundingClientRect(); return { top: Math.round(r.top), inFirstViewport: r.top >= 0 && r.top < window.innerHeight }; }
            return { tip: inView(tip), verdict: inView(v), vh: window.innerHeight };
        }""")
        report["P0-2_first_viewport"] = vis
        page.screenshot(path=os.path.join(OUT, "after_02_scan_result_coach_on_top.png"))
        # expander open state screenshot
        page.evaluate("() => { var n = document.querySelector('#resultDetail details.nutx'); if (n){ n.open = true; n.scrollIntoView({block:'start'}); } }")
        page.wait_for_timeout(300)
        page.screenshot(path=os.path.join(OUT, "after_02b_nutrition_expander_open.png"))
    else:
        report["P0-2_coach_position"] = "ANALYZE DID NOT COMPLETE (vision call failed locally)"

    # ---------- P1-3: Scan tab bottom — FAB must not cover 'Quick add' ----------
    page.evaluate("() => { var c = document.getElementById('resultClose'); if (c) c.click(); switchTab('scan'); window.scrollTo(0, document.body.scrollHeight); }")
    page.wait_for_timeout(500)
    fab = page.evaluate("""() => {
        var fab = document.getElementById('coachFab').getBoundingClientRect();
        var qa = document.getElementById('quickAddToggle');
        var mainPad = getComputedStyle(document.querySelector('main')).paddingBottom;
        // at max scroll, does the FAB rect intersect ANY tappable control in main?
        var els = document.querySelectorAll('#tab-scan button, #tab-scan input');
        var overlaps = [];
        els.forEach(function(el){
            var r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return;
            var ix = Math.max(0, Math.min(r.right, fab.right) - Math.max(r.left, fab.left));
            var iy = Math.max(0, Math.min(r.bottom, fab.bottom) - Math.max(r.top, fab.top));
            if (ix > 4 && iy > 4) overlaps.push((el.id || el.className || el.tagName) + ' @' + Math.round(r.top));
        });
        return { mainPaddingBottom: mainPad, fabTop: Math.round(fab.top), overlaps: overlaps,
                 quickAddBottom: qa ? Math.round(qa.getBoundingClientRect().bottom) : null };
    }""")
    report["P1-3_fab"] = fab
    page.screenshot(path=os.path.join(OUT, "after_03_scan_bottom_fab_clear.png"))

    # ---------- P1-4: location primer fires once across Coach Cal + Eat Out ----------
    loc = page.evaluate("""async () => {
        try { localStorage.removeItem('snapcal_c_snapcal_loc_primed'); } catch(e){}
        var ex = document.getElementById('locPrimer'); if (ex) ex.remove();
        var shows = 0, orig = window.showLocPrimer;
        window.showLocPrimer = function(a, b){ shows++; orig(a, b); var btn = document.querySelector('#locPrimer .locp-allow'); if (btn) setTimeout(function(){ btn.click(); }, 60); };
        openVoice();                                             // Coach Cal open -> loadChatNearby -> snapGeo
        await new Promise(function(r){ setTimeout(r, 900); });
        closeVoice();
        switchTab('eatout');                                     // Eat Out -> autoNearMe -> snapGeo
        await new Promise(function(r){ setTimeout(r, 1500); });
        window.showLocPrimer = orig;
        return { primerShows: shows, flag: localStorage.getItem('snapcal_c_snapcal_loc_primed') };
    }""")
    report["P1-4_location_once"] = loc

    # ---------- P1-5: NEDA card in Gentle mode ----------
    page.evaluate("() => { document.body.classList.add('gentle'); switchTab('today'); window.scrollTo(0,0); }")
    page.wait_for_timeout(400)
    neda = page.evaluate("""() => {
        var c = document.getElementById('nedaCard');
        var st = getComputedStyle(c);
        return { display: st.display, fontT: getComputedStyle(c.querySelector('.neda-t')).fontSize,
                 fontS: getComputedStyle(c.querySelector('.neda-s')).fontSize,
                 bg: st.backgroundColor };
    }""")
    report["P1-5_neda"] = neda
    page.evaluate("() => { var c = document.getElementById('nedaCard'); c.scrollIntoView({block:'center'}); }")
    page.wait_for_timeout(300)
    page.screenshot(path=os.path.join(OUT, "after_05_neda_card_gentle.png"))
    page.evaluate("() => document.body.classList.remove('gentle')")

    # ---------- P1-6: history empty state ----------
    page.evaluate("""async () => {
        var real = window.api;
        window.api = function(u, opts){ if (u.indexOf('/api/history') >= 0) return Promise.resolve({ days: [] }); return real(u, opts); };
        switchTab('history');
        await loadHistory();
        window.api = real;
        window.scrollTo(0, 0);
    }""")
    page.wait_for_timeout(500)
    hist = page.evaluate("""() => {
        var e = document.querySelector('#historyList .history-empty');
        return { present: !!e, hasSkel: !!(e && e.querySelector('.hist-skel')),
                 hasCta: !!document.getElementById('histAddMealBtn'),
                 height: e ? Math.round(e.getBoundingClientRect().height) : 0 };
    }""")
    report["P1-6_history_empty"] = hist
    page.screenshot(path=os.path.join(OUT, "after_06_history_empty_state.png"))

    browser.close()

print(json.dumps(report, indent=2))
