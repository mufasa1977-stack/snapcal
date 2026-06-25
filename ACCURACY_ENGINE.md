# SnapCal Accuracy Engine — the plan to be the MOST accurate calorie app

_2026-06-25. From the surviving accuracy-architecture research + the MyFitnessPal/Lose It/Cronometer deep-dive. This is how we beat all of them on the one thing none of them get right: **the numbers.**_

## Core idea: a provenance ROUTER
Every logged food flows through one `resolve_food()` dispatcher that **always logs from the most accurate available source** and stamps each entry with `source` + `accuracy_tier`. **No competitor shows where a number came from. SnapCal will — and will be EXACT wherever exact is possible.** Add `source` + `confidence` columns to the `meals` table.

## The accuracy ladder (most → least accurate)
| Rung | Source | Tier | Status |
|---|---|---|---|
| 1 | **Barcode → label** (Open Food Facts) | EXACT | ✅ shipped. Upgrade: add USDA FDC **Branded** UPC fallback (free) for US coverage |
| 2 | **Restaurant/chain → published menu nutrition** | EXACT | 🔨 **BUILD FIRST — biggest moat.** Free data via **MenuStat.org** (Harvard Dataverse CSV) + patch top-20 chains from their official PDFs → `data/menus.json` + `/api/menu`. Eating out becomes tap-to-log the chain's *exact* number, not a photo guess |
| 3 | **Generic whole food → USDA FDC + portion helper** | VERIFIED | 🔨 lookup done (`/api/nutrition` + smart `_pick_food`); add household-measure chips (FDC `foodPortions`: "1 medium = 182 g") + reference-object scale |
| 4 | **AI photo estimate** (last resort) | ESTIMATE ± range | 🔨 **Rung 4a SHIPPED 2026-06-25:** grams-first + reference-object scale + hidden-fats counting (caught a missed "oil/dressing 120 kcal" → fixed a 19% under-count). **4b next:** cross-check — AI estimates GRAMS, USDA supplies the nutrient DENSITY (hybrid > pure-AI); return calories_low/high |
| 5 | **Learn from corrections** | calibration | 🔨 `corrections` table → per-user + global bias multipliers; confidence tightens as a food is corrected more. A compounding moat competitors can't copy without our correction data |

## The honest claim we earn (no fabricated %)
> "SnapCal logs the **exact** number wherever one exists — every packaged food (barcode → label) and every chain-restaurant item (published menu). For home-cooked/generic food it uses **USDA federal data** with confirmed portions. Only when nothing else fits does it estimate from your photo — and even then it shows the range and gets smarter every time you correct it. **Most apps estimate everything; SnapCal estimates only what truly can't be looked up.**"

## Build order (accuracy-gain-per-effort)
1. ~~**Router + `source`/`accuracy_tier`/`confidence` columns + provenance badges**~~ ✅ **done 2026-06-25** — `meals` table carries `source`/`accuracy_tier`/`confidence`; `/api/meals` stores+returns them; barcode→EXACT, USDA→VERIFIED, photo→ESTIMATE; Today list shows a green Exact / blue Verified / amber Estimate pill. Locked by 2 new `regression_gate.py` checks (44/44 green).
2. ~~**Rung 2 — restaurant-exact (`/api/menu`)**~~ ✅ **done 2026-06-25** — `/api/menu?q=&chain=` searches all 35 chains' items and returns EXACT published macros (tier=EXACT, source="Published menu — CHAIN"). Data already in `data/restaurants.json`. **NEXT for this rung:** a tap-to-log Eat-Out UI surface + widen coverage via MenuStat.org CSV (more chains/items).
3. ~~Rung 4a grams-first + hidden-fats~~ ✅ **done 2026-06-25**
4. **Rung 4b** — USDA density cross-check + model confidence band (~1 day) ← **do next**
5. **Rung 3** — portion helper (household measures + reference-object) (~1 day)
6. **Rung 5** — correction learning (~1 day, compounds over time)
7. **Rung 1 upgrade** — FDC Branded UPC fallback (~2 hrs)
8. **Off-ladder must-have** — Apple Health / Google Fit sync + home-screen widget (retention + editor credibility)

## Science update (2026-06-25, peer-reviewed) — three findings that sharpen the plan
1. **We can hit near-LiDAR accuracy WITHOUT going native.** Browsers can't read iPhone LiDAR or Android ToF (no API) — but a stacked web pipeline (verified-DB + hybrid LLM→grams→DB + fiducial scaling + weight-trend calibration) captures **nearly all** the available accuracy. LiDAR buys ~13% portion error vs ~15-20% for the web stack — a real but **marginal** gain. **Verdict: build the web stack first; native LiDAR = a later premium accuracy tier, NOT a prerequisite.**
2. **The hybrid lookup is THE core estimator (confirmed):** LLM identifies + estimates GRAMS, the app computes calories from USDA density — **never** trust LLM calorie memory. Measured: **47.7 kcal/dish error vs 168-277 for prior systems = 76-83% reduction** (DietAI24, Nature 2025). This is Rung 4b — highest single lever after verified-DB.
3. **Reference-object (credit-card / plate-rim) scaling** is the highest-leverage *pure-web-CV* fix: a known-size object in frame cuts portion error from ~50-90% down to **~5-20% (3-10×)**, all in-browser via OpenCV.js / js-aruco2. A standard credit card (85.6×54 mm, globally fixed) is the best everyday fiducial; the plate rim is the zero-effort passive version. → fold into Rung 3/4's gram step.

**NEW Rung 6 — Weight-trend calibration (MacroFactor's secret; the quiet killer):** body-weight change is the physical integral of true net calories, so an EWMA trend-weight + the user's logged intake reveals their TRUE expenditure **regardless of logging error** — and silently cancels the camera's blind spots (oil, off-screen bites, big bowls), because they all wash into the weight trend. Measured **135 kcal/day error vs 335 for static formulas (~3× better), r=0.94**. Pure web (weigh-ins + arithmetic). This makes SnapCal's *targets* accurate even when an individual photo isn't — a moat no photo-only app has.

**Best model note:** Gemini 2.5/3 Pro is competitive with GPT-5.x/Claude 4.x at the top (~36% naive energy error) and the **cheapest per scan** — keep using a CURRENT Gemini (not 1.5). The hybrid lookup is what turns that 36% into single-digit-per-dish error. Prompt levers (measured): expert persona > multimodal CoT > itemize > scale-hint; grams-first; output a range. **(Rung 4a already does persona + itemize + grams-first + hidden-fats.)**

## Why this wins (from the competitor deep-dive)
Lose It!'s "Snap It" is **~68-70% accurate, ~11s** — beatable. MyFitnessPal is **crowd-sourced/unverified** + paywalls its barcode scanner (US). Cal AI photo-estimates *everything* (incl. barcoded + restaurant food) and is 25-50% off. Cronometer is accurate but has **no restaurant-exact lane and no AI-photo lane**. **None of them can make our provenance claim.** LiDAR volumetric portion (iOS/Android depth) only improves Rung 4 step 2 and needs the native shell — slot it into the same `grams` field later.

**Data sources:** MenuStat.org (free, Harvard Dataverse) · USDA FoodData Central (have key) · Open Food Facts (have it) · Nutritionix (202K restaurant items but $1,850/mo — defer). [[snapcal-competitive-strategy]] · COMPETITIVE.md
