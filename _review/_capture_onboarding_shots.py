#!/usr/bin/env python3
"""One-off script: drive the new quiz onboarding + paywall in headless Chrome and save screenshots.
Not part of the regression gate — a throwaway capture helper for the 2026-07-15 onboarding rebuild review."""
import os
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5177"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "onboarding_2026-07-15")
os.makedirs(OUT, exist_ok=True)

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel="chrome")
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="light")
    page = ctx.new_page()
    page.goto(BASE + "/?gate=1", wait_until="domcontentloaded", timeout=20000)
    page.evaluate("() => { try { localStorage.clear(); } catch(e){} }")
    page.reload(wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(1200)  # let the 600ms auto-onboarding timer fire

    page.screenshot(path=os.path.join(OUT, "01_intro.png"))

    def js(code):
        return page.evaluate(code)

    js("document.querySelector('#qzOnboard #qzNext').click()")  # intro -> goal
    page.screenshot(path=os.path.join(OUT, "02_goal_question.png"))

    js("""() => {
        var ov = document.getElementById('qzOnboard');
        function pick(v){ ov.querySelector('.qz-opt[data-qv="'+v+'"]').click(); }
        function next(){ ov.querySelector('#qzNext').click(); }
        pick('lose'); pick('female');
        ov.querySelector('#qzAge').value = '29'; next();
        ov.querySelector('#qzFt').value = '5'; ov.querySelector('#qzIn').value = '5'; next();
        ov.querySelector('#qzWCur').value = '175'; next();
        ov.querySelector('#qzWGoal').value = '150'; next();
        pick('light'); pick('eat_out');
    }""")
    page.screenshot(path=os.path.join(OUT, "03_derail_confessional.png"))

    js("""() => {
        var ov = document.getElementById('qzOnboard');
        function pick(v){ ov.querySelector('.qz-opt[data-qv="'+v+'"]').click(); }
        function next(){ ov.querySelector('#qzNext').click(); }
        pick('late_night'); pick('middle'); next();
    }""")
    page.screenshot(path=os.path.join(OUT, "04_plan_reveal.png"))

    js("document.querySelector('#qzOnboard #qzNext').click()")  # reveal -> paywall
    page.screenshot(path=os.path.join(OUT, "05_paywall_top.png"))
    js("document.querySelector('#qzOnboard').scrollTo(0, 9999)")
    page.screenshot(path=os.path.join(OUT, "06_paywall_devbypass.png"))

    browser.close()
    print("Saved screenshots to", OUT)
