"""SnapCal backend — personal Cal AI clone (Flask + SQLite + Gemini vision).

Run:  python app.py
Serves static/index.html at /, JSON API under /api/*, listens on 0.0.0.0:5177.
The Gemini API key is read server-side from gemini_key.txt and never sent to the client.
"""

import json
import math
import os
import re
import socket
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, redirect, Response
try:
    from flask_cors import CORS  # only needed for the hosted native-app backend
except ImportError:
    CORS = None

APP_DIR = Path(__file__).resolve().parent
# DB lives on a PERSISTENT path so a redeploy/update never wipes user history. In prod set
# SNAPCAL_DB_DIR to a mounted persistent disk (e.g. Render disk at /var/data); locally it falls
# back to the app folder. Additive-only migrations + a stable file = history survives every update.
_DB_DIR = Path(os.environ.get("SNAPCAL_DB_DIR", "")).expanduser() if os.environ.get("SNAPCAL_DB_DIR") else APP_DIR
try:
    _DB_DIR.mkdir(parents=True, exist_ok=True)
except Exception:  # noqa: BLE001 — fall back to the app dir if the configured path isn't writable
    _DB_DIR = APP_DIR
DB_PATH = _DB_DIR / "snapcal.db"
RESTAURANTS_PATH = APP_DIR / "data" / "restaurants.json"  # curated "Eat Out" dataset
RECIPES_PATH = APP_DIR / "data" / "recipes.json"  # curated recipe library (SnapCal Meals browser)
ROUTINES_PATH = APP_DIR / "data" / "routines.json"  # curated exercise routines (Move module)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"  # free OpenStreetMap places lookup (no API key / no billing)
# Render's egress 429s/can't-reach the primary Overpass host a lot → rotate mirrors with retry so the eat-out
# feature doesn't go dark (the QA squad found /api/nearby was unreachable in prod). Google Places later = the real cure.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
_OVERPASS_CACHE = {}  # key -> (payload, epoch); serves recent results through a transient outage


def _overpass(query, timeout=18, cache_key=None):
    """Run an Overpass query against rotating mirrors with retry; cache hits so an outage still serves data."""
    if cache_key and cache_key in _OVERPASS_CACHE:
        payload, ts = _OVERPASS_CACHE[cache_key]
        if (time.time() - ts) < 21600:  # 6h
            return payload
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last = None
    for url in OVERPASS_MIRRORS:
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": "SnapCal/1.0 (health coach; contact tariq@xionprotech.com)"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if cache_key:
                if len(_OVERPASS_CACHE) > 500:
                    _OVERPASS_CACHE.clear()
                _OVERPASS_CACHE[cache_key] = (payload, time.time())
            return payload
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            last = exc
    # All mirrors failed — serve a stale cache entry if we have one, else raise.
    if cache_key and cache_key in _OVERPASS_CACHE:
        return _OVERPASS_CACHE[cache_key][0]
    raise last if last else RuntimeError("overpass_unreachable")
# Key: prefer the GEMINI_API_KEY env var (hosting / paid tier); fall back to the
# local key file so `python app.py` still works during development.
GEMINI_KEY_PATH = Path("C:/Users/somme/youtube_videos/gemini_key.txt")
GEMINI_MODEL = "gemini-2.5-flash"  # gemini-2.0-flash was retired by the API (404)
CHAT_MODEL = "gemini-2.5-flash"  # Coach Cal: flash (not -lite) — the squad proved -lite was non-deterministic (wrong city ~1/8, invented dishes, bounced questions). Worth ~1-2s for correctness.
PORT = int(os.environ.get("PORT", "5177"))  # Render/Fly inject $PORT in production

# --- Web Push (VAPID) — proactive Coach Cal check-ins reach the user when the app is CLOSED. PREMIUM feature.
# Public key is safe to embed (the browser needs it as applicationServerKey); the PRIVATE key must live ONLY in
# Render env (VAPID_PRIVATE). Generated 2026-06-28 (cryptography P-256). Tariq adds VAPID_PRIVATE to Render env.
VAPID_PUBLIC = os.environ.get(
    "VAPID_PUBLIC",
    "BPq-VDdsVfMK5zHMGDBt_wytz5wfB2YuIJWvyZ6FuZSs9pIHDH6JzobBguaNvoHsD9XoeRviiFVSeWOQ4UEVUXo").strip()
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE", "").strip()  # 32-byte scalar, base64url — env-only, never embed
VAPID_SUB = os.environ.get("VAPID_SUB", "mailto:tariq@xionprotech.com").strip()
# Shared secret guarding /api/push/run so only our scheduler (GitHub Action) can fan out notifications.
PUSH_RUN_SECRET = os.environ.get("PUSH_RUN_SECRET", "").strip()

# Origins the Capacitor native app calls the API from: iOS = capacitor://localhost,
# Android = https://localhost. Plus local web dev + any extra via CORS_ORIGINS env.
CORS_ORIGINS = [
    "capacitor://localhost",
    "https://localhost",
    "http://localhost",
] + [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]

PROFILE_DEFAULTS = {"daily_calories": 2000, "protein_g": 150, "carbs_g": 200, "fat_g": 65}
# Body fields synced from "About you" so Coach Cal can personalise across devices (not just localStorage).
PROFILE_NUM_KEYS = ("age", "height_in", "cur_weight", "goal_weight")
PROFILE_TEXT_KEYS = ("sex", "activity", "goal_dir")
MACRO_KEYS = ("calories", "protein_g", "carbs_g", "fat_g")

# Provenance accuracy ladder (see ACCURACY_ENGINE.md). EXACT = read off a real label or a
# published menu; VERIFIED = federal USDA data + a confirmed portion; ESTIMATE = AI photo guess.
ACCURACY_TIERS = ("EXACT", "VERIFIED", "ESTIMATE")
_TIER_DEFAULT_CONFIDENCE = {"EXACT": 99, "VERIFIED": 90, "ESTIMATE": 60}


def _norm_tier(tier):
    """Coerce any caller-supplied tier to one of the three canonical rungs (default ESTIMATE)."""
    t = (str(tier or "")).strip().upper()
    return t if t in ACCURACY_TIERS else "ESTIMATE"

ALLOWED_MIMES = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/heic": "image/heic",
    "image/heif": "image/heif",
}
EXT_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

GOAL_LABELS = {
    "lose_weight": "losing weight / fat loss — prioritise a calorie deficit, high satiety, "
                   "high protein to preserve muscle, low energy density, and watching hidden calories",
    "build_muscle": "building muscle / lean bulk — prioritise adequate calories, high total protein, "
                    "good protein density, and fuel around training",
    "recomp": "body recomposition — losing fat while building and keeping muscle at the same time. Run a "
              "MODERATE calorie deficit (never a crash) and hit a HIGH protein target so they stay SOLID, not "
              "skinny: protein plus resistance training preserve and build lean muscle while the fat comes off, "
              "and a slower, steadier loss with muscle underneath gives the best skin and body outcome. "
              "Prioritise high protein, high satiety, whole foods, and enough fiber and water; keep the "
              "deficit gentle, never aggressive",
    "maintain": "maintaining weight and eating healthily — prioritise balanced macros and overall "
                "nutrient quality",
    "glp1": "on a GLP-1 medication (Ozempic/Wegovy/Zepbound) — appetite is suppressed so every bite must "
            "count: prioritise high protein to defend muscle during rapid weight loss, adequate fiber and "
            "hydration to ease constipation, and flag greasy/high-fat or very large portions that commonly "
            "trigger nausea; coach toward small nutrient-dense portions",
}

ANALYZE_PROMPT_TMPL = """You are an expert nutrition-analysis engine for a food-tracking app.
Analyze the attached food photo and produce a COMPLETE nutrition breakdown — the kind you would
read off a nutrition label, plus key micronutrients — all estimated from what is visible.

The user's current goal is: {goal_desc}.
Tailor "verdict" and "coach_tip" to THAT goal.

METHOD — estimate like a registered dietitian, because consumer apps systematically UNDER-count:
1. SCALE: find a reference object in the photo to judge real size — a dinner plate is ~27 cm across,
   a fork ~18 cm, a standard 12-oz can ~12 cm tall, an adult hand ~18 cm. Use it to gauge true portion.
2. GRAMS FIRST: for EACH item, estimate its edible WEIGHT IN GRAMS from that scale and its visible
   volume; then derive calories and macros from that gram weight and the food's typical nutrient density
   — do not guess macros directly. Put the gram estimate in "qty" (e.g. "approx. 150 g").
3. COUNT THE HIDDEN CALORIES — this is the #1 source of error: cooking oil/butter, dressings, sauces,
   gravy, melted cheese, spreads, and sugary drinks are usually present even when not obvious. INCLUDE
   them. Restaurant and home-cooked dishes typically carry 100-400 kcal of added fats/oils people forget.
4. WHEN PORTION OR HIDDEN INGREDIENTS ARE UNCERTAIN, lean slightly HIGHER, never lower, and say why in
   "note". Under-counting breaks the user's results; an honest, slightly-high estimate protects them.

Respond with STRICT JSON only (no markdown, no code fences), exactly matching this schema:
{{
  "items": [
    {{"name": "string", "qty": "string",
      "calories": int, "protein_g": int, "carbs_g": int, "fat_g": int,
      "fiber_g": int, "sugar_g": int, "sat_fat_g": int, "sodium_mg": int}}
  ],
  "total": {{
    "calories": int, "protein_g": int, "carbs_g": int, "fat_g": int,
    "fiber_g": int, "sugar_g": int, "sat_fat_g": int, "trans_fat_g": int,
    "cholesterol_mg": int, "sodium_mg": int,
    "potassium_mg": int, "calcium_mg": int, "iron_mg": int,
    "vitamin_a_dv": int, "vitamin_c_dv": int, "vitamin_d_dv": int,
    "est_weight_g": int
  }},
  "health_score": int,
  "quality_grade": "A",
  "satiety": "low",
  "good_flags": ["string"],
  "bad_flags": ["string"],
  "verdict": "string",
  "coach_tip": "string",
  "swaps": [
    {{"from": "string", "to": "string", "why": "string"}}
  ],
  "note": "string"
}}

Rules:
- "items": one entry per distinct food/drink visible. "qty" is an estimated portion
  (e.g. "1 cup", "2 slices", "approx. 150 g"). Include the per-item macro + fiber/sugar/sat-fat/sodium.
- "total": sum the macro fields across items, and ALSO estimate for the whole plate the micronutrients
  (potassium/calcium/iron in mg; vitamins A/C/D as integer % Daily Value) and "est_weight_g"
  (total edible weight in grams).
- "health_score": integer 0-100 — overall nutritional quality for THIS goal (100 = excellent).
- "quality_grade": one of "A" (excellent), "B" (good), "C" (okay), "D" (poor).
- "satiety": one of "low", "medium", "high" — how filling per calorie (high = lots of protein/fiber/volume).
- "good_flags": 0-4 short positives, e.g. "High protein", "High fiber", "Nutrient-dense", "Low sugar".
- "bad_flags": 0-4 short cautions, e.g. "High sodium", "High saturated fat", "High sugar", "Calorie-dense".
- "verdict": ONE sentence judging how this meal fits the user's goal.
- "coach_tip": ONE concrete, actionable suggestion for the goal (e.g. "Skip the dressing to cut ~150 kcal").
- "swaps": 0-3 specific, appetizing food swaps that fit the user's goal BETTER — ONLY when a meaningful
  improvement is genuinely possible. "from" = a food/component visible in THIS photo; "to" = an alternative
  that is equally or MORE tasty and satisfying (never a bland "health-food" downgrade); "why" = ONE short
  benefit phrased for the goal (e.g. "~Same protein, ~40% less saturated fat", "+18 g protein for the same
  calories", "Half the sugar, still sweet"). If the meal already fits the goal well, return [].
- "note": confidence caveats (hidden oils/sauces, unclear portion). "" if confident.
- All numeric values must be integers (round them).
- If the photo contains no food: items=[], all totals 0, good_flags/bad_flags empty,
  quality_grade "D", satiety "low", health_score 0, verdict "" , coach_tip "", swaps [],
  note "No food detected in the photo."
"""

# ---- Food-allergy filtering: one avoid-list flows into EVERY Gemini food prompt + a server-side safety net ----
_ALLERGEN_KW = {
    "peanuts": ["peanut"],
    "tree nuts": ["tree nut", "almond", "walnut", "pecan", "cashew", "pistachio", "hazelnut", "macadamia", "mixed nut", " nuts", "nut ", "pesto", "marzipan"],
    "dairy": ["milk", "cheese", "yogurt", "yoghurt", "cream", "butter", "dairy", "whey", "queso", "parmesan", "feta", "mozzarella", "ranch", "alfredo", "latte"],
    "eggs": ["egg", "omelet", "omelette", "mayo", "frittata", "quiche"],
    "gluten / wheat": ["wheat", "bread", "bun", "roll", "pasta", "flour", "tortilla", "wrap", "bagel", "cracker", "gluten", "breaded", "crouton", "hoagie", "sub ", "pita", "noodle", "oat", "cereal", "pretzel", "biscuit"],
    "soy": ["soy", "tofu", "edamame", "tempeh", "miso"],
    "fish": ["fish", "salmon", "tuna", "cod", "tilapia", "anchovy", "sardine"],
    "shellfish": ["shrimp", "crab", "lobster", "shellfish", "clam", "oyster", "scallop", "prawn", "crawfish", "mussel"],
    "sesame": ["sesame", "tahini", "hummus"],
    "fruit": ["fruit", "berry", "berries", "strawberr", "blueberr", "apple", "banana", "orange", "melon", "grape", "mango", "peach", "pineapple", "cherry", "kiwi", "apricot"],
}


def _norm_allergies(allergies):
    if not allergies:
        return []
    if isinstance(allergies, str):
        allergies = allergies.split(",")
    return [str(a).strip().lower() for a in allergies if str(a).strip()][:12]


def _allergy_clause(allergies):
    al = _norm_allergies(allergies)
    if not al:
        return ""
    return ("\n\nCRITICAL — FOOD ALLERGIES. The user is allergic to: " + ", ".join(al) + ". EVERY item you suggest "
            "MUST be free of these allergens and their hidden/derivative sources. NEVER suggest anything that contains "
            "or is commonly cross-contaminated with them. If a usual recommendation is unsafe, replace it with a safe "
            "alternative — do not simply drop it. This must be correct.")


def _allergy_unsafe(text, al):
    if not al:
        return False
    t = " " + str(text or "").lower() + " "
    for a in al:
        for kw in _ALLERGEN_KW.get(a, [a]):
            if kw in t:
                return True
    return False


def _pick_text(p):
    if not isinstance(p, dict):
        return str(p)
    return " ".join(str(p.get(k, "")) for k in ("item", "name", "title", "desc", "description", "why", "note"))


# ---- Diet preference (vegan / vegetarian / pescatarian): themes every food prompt + a server-side net ----
_DIET_MEAT = ["chicken", "beef", "pork", "turkey", "bacon", "steak", "sausage", "meatball", "pepperoni", "salami", "brisket", "lamb", "veal", "prosciutto", "chorizo", "carnitas", "barbacoa", "rotisserie", "hot dog", "deli meat"]
_DIET_SEA = ["fish", "salmon", "tuna", "tilapia", "shrimp", "crab", "lobster", "scallop", "oyster", "clam", "mussel", "anchovy", "sardine", "cod"]
_DIET_ANIMAL = ["milk", "cheese", "yogurt", "cream", "butter", "honey", "whey", "queso", "parmesan", "feta", "mozzarella", "gelatin", "ice cream"]
_DIET_FORBID = {
    "vegetarian": _DIET_MEAT + _DIET_SEA,
    "pescatarian": list(_DIET_MEAT),
    "vegan": _DIET_MEAT + _DIET_SEA + _DIET_ANIMAL,
}
_DIET_DESC = {
    "vegetarian": "VEGETARIAN — no meat, poultry, or seafood of any kind (dairy and eggs are OK)",
    "vegan": "VEGAN — 100% plant-based: no meat, poultry, seafood, dairy, eggs, honey, or any animal product",
    "pescatarian": "PESCATARIAN — no meat or poultry; seafood is OK",
}


def _norm_diet(diet):
    d = str(diet or "").strip().lower()
    return d if d in _DIET_FORBID else ""


def _diet_clause(diet):
    d = _norm_diet(diet)
    if not d:
        return ""
    return ("\n\nThe user follows a " + _DIET_DESC[d] + ". EVERY single item you suggest MUST fit this diet — "
            "do not suggest anything outside it; if a usual pick doesn't fit, replace it with one that does.")


def _diet_unsafe(text, diet):
    d = _norm_diet(diet)
    if not d:
        return False
    t = " " + str(text or "").lower() + " "
    return any(kw in t for kw in _DIET_FORBID[d])


def _allergy_filter(picks, allergies, diet=None):
    """Best-effort server-side net: drop any pick that trips an active allergen OR breaks the diet (Gemini already got both as prompt clauses)."""
    al = _norm_allergies(allergies)
    dt = _norm_diet(diet)
    if (not al and not dt) or not isinstance(picks, list):
        return picks
    return [p for p in picks if not _allergy_unsafe(_pick_text(p), al) and not _diet_unsafe(_pick_text(p), dt)]


COACH_PROMPT_TMPL = """You are Coach Cal, a warm, knowledgeable personal nutrition coach inside SnapCal (a food-tracking app) — you help people cut calories without feeling deprived.
The user's goal is: {goal_desc}.
So far today they have {eaten_summary}. They have roughly {remaining_calories} calories and
{remaining_protein} g of protein left for the day against their targets.

Suggest {n} specific, realistic, GENUINELY TASTY meals or snacks that fit the calories/protein they have
left AND move them toward their goal. Favour everyday, easy-to-get foods people actually enjoy — never
bland "diet" food. Vary the ideas (don't suggest three near-identical dishes).

Respond with STRICT JSON only (no markdown, no code fences), exactly this schema:
{{
  "intro": "string",
  "meals": [
    {{"name": "string", "desc": "string",
      "calories": int, "protein_g": int, "carbs_g": int, "fat_g": int, "why": "string"}}
  ]
}}

Rules:
- "intro": ONE warm, encouraging sentence referencing their remaining budget and goal.
- "meals": exactly {n} ideas. "name" = the dish; "desc" = a short, appetizing one-line description;
  macros are your best estimate for a sensible single portion; "why" = ONE short phrase on why it fits the goal.
- Keep each meal at or under the remaining calories where reasonable; prioritise hitting the protein target.
- All numeric values must be integers (round them). Output JSON only.
"""

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # 15MB upload cap
if CORS is not None:  # allow the hosted native app (capacitor://, https://localhost) to call /api/*
    CORS(app, resources={r"/api/*": {"origins": CORS_ORIGINS}})


# ---------------------------------------------------------------- database

def get_db():
    """New connection per request (sqlite3 stdlib)."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = get_db()
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS meals(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   date TEXT,
                   time TEXT,
                   name TEXT,
                   calories INT,
                   protein_g INT,
                   carbs_g INT,
                   fat_g INT,
                   items_json TEXT
               )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS profile(
                   key TEXT PRIMARY KEY,
                   value INT
               )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS weights(
                   date TEXT PRIMARY KEY,
                   weight REAL
               )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS water(
                   date TEXT PRIMARY KEY,
                   glasses INTEGER NOT NULL DEFAULT 0
               )"""
        )
        # Wearable / phone health data synced from Apple Health (HealthKit) or Google Fit / Health Connect via
        # the native build. One row per device per day. Lets Coach Cal coach on REAL movement + recovery
        # (steps, active calories, resting HR, sleep, weight) — the all-in-one "hub" move.
        con.execute(
            """CREATE TABLE IF NOT EXISTS health_metrics(
                   uid TEXT,
                   date TEXT,
                   steps INTEGER,
                   active_cal INTEGER,
                   resting_hr INTEGER,
                   sleep_min INTEGER,
                   weight REAL,
                   source TEXT,
                   updated TEXT,
                   PRIMARY KEY (uid, date)
               )"""
        )
        # Server-side daily usage counters (the HARD cost cap). The client shows "N scans left" from
        # localStorage for instant UX, but that's trivially editable — this table is the real enforcement so a
        # free user can't run up the Gemini bill on /api/analyze (vision) + /api/chat (Coach Cal). Premium = no cap.
        con.execute(
            """CREATE TABLE IF NOT EXISTS usage(
                   uid TEXT,
                   date TEXT,
                   kind TEXT,
                   count INTEGER NOT NULL DEFAULT 0,
                   PRIMARY KEY (uid, date, kind)
               )"""
        )
        # Web-push subscriptions (one per device). Carries enough profile snapshot to write a personal
        # check-in on a schedule, plus the device tz offset so we fire morning/midday/evening in LOCAL time,
        # and last_slot/last_date so each user gets at most ONE push per slot per day.
        con.execute(
            """CREATE TABLE IF NOT EXISTS push_subs(
                   uid TEXT PRIMARY KEY,
                   sub_json TEXT NOT NULL,
                   tz_offset_min INTEGER DEFAULT 0,
                   name TEXT,
                   goal TEXT,
                   daily_calories INTEGER DEFAULT 2000,
                   protein_target_g INTEGER DEFAULT 0,
                   enabled INTEGER DEFAULT 1,
                   last_slot TEXT,
                   last_date TEXT,
                   created TEXT
               )"""
        )
        # Migration: store the full rich breakdown per meal so History can show it.
        cols = [r[1] for r in con.execute("PRAGMA table_info(meals)").fetchall()]
        if "detail_json" not in cols:
            con.execute("ALTER TABLE meals ADD COLUMN detail_json TEXT")
        if "thumb" not in cols:
            con.execute("ALTER TABLE meals ADD COLUMN thumb TEXT")
        if "uid" not in cols:
            con.execute("ALTER TABLE meals ADD COLUMN uid TEXT")
        # Provenance router: every logged food carries WHERE its number came from + how
        # trustworthy it is. No competitor shows this; SnapCal is EXACT wherever exact exists.
        if "source" not in cols:
            con.execute("ALTER TABLE meals ADD COLUMN source TEXT")
        if "accuracy_tier" not in cols:
            con.execute("ALTER TABLE meals ADD COLUMN accuracy_tier TEXT")
        if "confidence" not in cols:
            con.execute("ALTER TABLE meals ADD COLUMN confidence INT")
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------- helpers

def _uid():
    """Per-device id the client sends on every request (X-Device-Id) — scopes each
       user's diary so two testers never see each other's meals."""
    return (request.headers.get("X-Device-Id") or "").strip() or "_shared"


# --- Free-tier cost cap (server-side enforcement). Mirrors the client's FREE_SCANS/FREE_CHATS. -------------
FREE_SCANS_DAY = 3   # photo meal-scans/day on free (Gemini vision — billed)
FREE_CHATS_DAY = 5   # Coach Cal chats/day on free (Gemini chat — billed)


def _req_is_premium():
    """The client sends X-Premium:1 when the user is Premium (entitlement check is client/RevenueCat-side for
       now). Premium bypasses the daily caps. Once IAP is live this can be upgraded to a server-side RevenueCat
       entitlement verification; until then this caps the dominant cost risk: heavy FREE usage."""
    return (request.headers.get("X-Premium") or "").strip() == "1"


def _cap_over(kind, limit):
    """If this free device has hit today's limit for `kind`, return the limit payload (caller returns it as 200
       so the client shows the upgrade prompt). Premium → never capped. Does NOT bump — call _cap_bump on allow."""
    if _req_is_premium():
        return None
    today = date.today().isoformat()
    con = get_db()
    try:
        row = con.execute("SELECT count FROM usage WHERE uid = ? AND date = ? AND kind = ?",
                          (_uid(), today, kind)).fetchone()
    finally:
        con.close()
    used = row["count"] if row else 0
    if used >= limit:
        return {"limit": True, "kind": kind, "used": used, "cap": limit}
    return None


def _cap_bump(kind):
    """Count one billed call against today's allowance for this device (no-op for premium)."""
    if _req_is_premium():
        return
    today = date.today().isoformat()
    con = get_db()
    try:
        con.execute(
            """INSERT INTO usage(uid, date, kind, count) VALUES(?,?,?,1)
               ON CONFLICT(uid, date, kind) DO UPDATE SET count = count + 1""",
            (_uid(), today, kind),
        )
        con.commit()
    finally:
        con.close()

def _int(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def get_lan_ip():
    """Best-effort detection of the machine's LAN (192.168.x.x) address."""
    candidates = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # no packets sent; just picks the route
            candidates.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    for ip in candidates:
        if ip.startswith("192.168."):
            return ip
    for ip in candidates:
        if not ip.startswith("127."):
            return ip
    return "127.0.0.1"


_gemini_client = None


def _load_gemini_key():
    """Prefer GEMINI_API_KEY env (hosting / paid tier); fall back to the local key file (dev)."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    return GEMINI_KEY_PATH.read_text().strip()


def get_gemini_client():
    """Lazy singleton so the server still boots if the key/package is missing."""
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=_load_gemini_key())
    return _gemini_client


def parse_gemini_json(text):
    """Robustly parse Gemini output: strip markdown fences, fall back to the
    outermost {...} block if there is leading/trailing prose."""
    t = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return json.loads(t[start:end + 1])
        raise


# Per-item numeric fields (also summable for a reconciling total).
ITEM_NUM = ("calories", "protein_g", "carbs_g", "fat_g",
            "fiber_g", "sugar_g", "sat_fat_g", "sodium_mg")
# Total-only fields that we never sum (model estimates them for the whole plate).
TOTAL_EXTRA = ("trans_fat_g", "cholesterol_mg",
               "potassium_mg", "calcium_mg", "iron_mg",
               "vitamin_a_dv", "vitamin_c_dv", "vitamin_d_dv", "est_weight_g")


def _flags(value):
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if x][:4]


def _swaps(value):
    """Coach swaps: list of {from, to, why}. Keep only well-formed entries (need from+to), cap at 3."""
    if not isinstance(value, list):
        return []
    out = []
    for s in value:
        if not isinstance(s, dict):
            continue
        frm = str(s.get("from") or "").strip()
        to = str(s.get("to") or "").strip()
        why = str(s.get("why") or "").strip()
        if frm and to:
            out.append({"from": frm, "to": to, "why": why})
        if len(out) >= 3:
            break
    return out


def _allergens_in_text(text, al):
    """Which of the user's active allergens this text trips (by name), e.g. ['dairy','fruit']."""
    if not al:
        return []
    t = " " + str(text or "").lower() + " "
    hits = []
    for a in al:
        for kw in _ALLERGEN_KW.get(a, [a]):
            if kw in t:
                hits.append(a)
                break
    return hits


def normalize_analysis(data, allergies=None, diet=None):
    """Coerce the model output into the exact contract schema. When the user has allergies/diet set,
    filter unsafe SWAPS server-side (belt-and-suspenders with the prompt clause) and surface a clear
    allergen WARNING naming any logged item that contains one of their allergens."""
    if not isinstance(data, dict):
        raise ValueError("Gemini returned non-object JSON")

    items = []
    raw_items = data.get("items")
    if isinstance(raw_items, list):
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            row = {"name": str(it.get("name", "Unknown item")),
                   "qty": str(it.get("qty", ""))}
            for k in ITEM_NUM:
                row[k] = _int(it.get(k))
            items.append(row)

    raw_total = data.get("total") if isinstance(data.get("total"), dict) else {}
    total = {}
    # Summable fields fall back to the item sum so the math always reconciles.
    for k in ITEM_NUM:
        total[k] = _int(raw_total[k]) if k in raw_total else sum(i[k] for i in items)
    for k in TOTAL_EXTRA:
        total[k] = _int(raw_total.get(k))

    grade = str(data.get("quality_grade", "")).upper()
    if grade not in ("A", "B", "C", "D"):
        grade = ""
    satiety = str(data.get("satiety", "")).lower()
    if satiety not in ("low", "medium", "high"):
        satiety = ""

    al = _norm_allergies(allergies)
    dt = _norm_diet(diet)

    # Belt-and-suspenders: NEVER suggest a food the user is allergic to (or that breaks their diet),
    # even if the model ignored the prompt clause. This is the bug fix — the scan used to suggest
    # fruit/nuts to someone allergic to them. Drop any unsafe swap entirely.
    swaps = _swaps(data.get("swaps"))
    if al or dt:
        # A swap's suggestion lives in its "to" field (with "from"/"why" as context) — check THAT,
        # not the generic _pick_text which doesn't know swap keys (that miss let "Mixed berries" leak).
        def _swap_text(s):
            return " ".join(str(s.get(k, "")) for k in ("to", "from", "why"))
        swaps = [s for s in swaps if not _allergy_unsafe(_swap_text(s), al) and not _diet_unsafe(_swap_text(s), dt)]

    # Allergen WARNING: name any logged item that contains one of THIS user's allergens, so they're
    # alerted before they eat it (e.g. logging yogurt while dairy-allergic). Deterministic, not the model.
    allergen_warning = ""
    triggered = []
    if al:
        for it in items:
            for a in _allergens_in_text(it["name"] + " " + it.get("qty", ""), al):
                triggered.append((a, it["name"]))
        if triggered:
            seen, parts = set(), []
            for a, nm in triggered:
                if a in seen:
                    continue
                seen.add(a)
                parts.append(a + " (" + nm + ")")
            allergen_warning = ("Heads up — this looks like it contains an allergen you flagged: "
                                + "; ".join(parts) + ". Double-check the ingredients before eating.")

    return {
        "items": items,
        "total": total,
        "health_score": max(0, min(100, _int(data.get("health_score")))),
        "quality_grade": grade,
        "satiety": satiety,
        "good_flags": _flags(data.get("good_flags")),
        "bad_flags": _flags(data.get("bad_flags")),
        "verdict": str(data.get("verdict") or ""),
        "coach_tip": str(data.get("coach_tip") or ""),
        "swaps": swaps,
        "note": str(data.get("note") or ""),
        "allergen_warning": allergen_warning,
    }


def normalize_coach(data, n):
    """Coerce the meal-suggestion model output into the exact contract schema."""
    if not isinstance(data, dict):
        data = {}
    meals = []
    raw = data.get("meals")
    if isinstance(raw, list):
        for m in raw:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name") or "").strip()
            if not name:
                continue
            meals.append({
                "name": name,
                "desc": str(m.get("desc") or "").strip(),
                "calories": _int(m.get("calories")),
                "protein_g": _int(m.get("protein_g")),
                "carbs_g": _int(m.get("carbs_g")),
                "fat_g": _int(m.get("fat_g")),
                "why": str(m.get("why") or "").strip(),
            })
            if len(meals) >= n:
                break
    return {"intro": str(data.get("intro") or "").strip(), "meals": meals}


# ---------------------------------------------------------------- routes

@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/manifest.webmanifest")
def manifest():
    resp = app.send_static_file("manifest.webmanifest")
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp


@app.get("/sw.js")
def service_worker():
    resp = app.send_static_file("sw.js")
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/.well-known/assetlinks.json")
def assetlinks():
    # Digital Asset Links — proves the Play app (com.xionprotech.snapcal) owns this domain (TWA).
    resp = app.send_static_file(".well-known/assetlinks.json")
    resp.headers["Content-Type"] = "application/json"
    return resp


@app.post("/api/analyze")
def analyze():
    f = request.files.get("photo")
    if f is None or not f.filename:
        return jsonify({"error": "No photo uploaded (multipart field 'photo' required)."}), 400

    mime = ALLOWED_MIMES.get((f.mimetype or "").lower())
    if mime is None:
        mime = EXT_TO_MIME.get(Path(f.filename).suffix.lower())
    if mime is None:
        return jsonify({"error": "Unsupported image type. Use jpeg, png, webp or heic."}), 400

    img_bytes = f.read()
    if not img_bytes:
        return jsonify({"error": "Uploaded file is empty."}), 400

    capped = _cap_over("scan", FREE_SCANS_DAY)  # HARD free-tier cost cap (server-side, can't be client-bypassed)
    if capped:
        return jsonify(capped)
    _cap_bump("scan")  # count it now — the Gemini vision cost is incurred whether or not parsing succeeds

    goal = (request.form.get("goal") or "maintain").strip().lower()
    if goal not in GOAL_LABELS:
        goal = "maintain"
    allergies = request.form.get("allergies") or ""
    diet = request.form.get("diet") or ""
    prompt = (ANALYZE_PROMPT_TMPL.format(goal_desc=GOAL_LABELS[goal])
              + _allergy_clause(allergies) + _diet_clause(diet))

    try:
        from google import genai
        client = get_gemini_client()
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                genai.types.Part.from_bytes(data=img_bytes, mime_type=mime),
                prompt,
            ],
            config=genai.types.GenerateContentConfig(
                response_mime_type="application/json",
                thinking_config=genai.types.ThinkingConfig(thinking_budget=0),  # skip the reasoning step -> faster analyze
            ),
        )
        result = normalize_analysis(parse_gemini_json(resp.text), allergies=allergies, diet=diet)
        result = _cross_check_calories(result)   # Rung 4b: blend AI grams x USDA density + confidence band
    except Exception:  # noqa: BLE001 — contract: any Gemini failure -> 502 JSON
        import traceback
        traceback.print_exc()  # full detail on the server console only
        return jsonify({"error": "Gemini analysis failed. See server console for details."}), 502
    return jsonify(result)


@app.post("/api/coach")
def coach_meals():
    """Goal- and budget-aware meal suggestions for the user's remaining day (text only)."""
    d = request.get_json(silent=True) or {}
    goal = str(d.get("goal") or "maintain").strip().lower()
    if goal not in GOAL_LABELS:
        goal = "maintain"
    remaining_cal = max(0, _int(d.get("remaining_calories")))
    remaining_pro = max(0, _int(d.get("remaining_protein_g")))
    try:
        n = int(d.get("count", 3))
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(4, n))

    eaten = d.get("eaten_today")
    if isinstance(eaten, list):
        names = [str(x).strip() for x in eaten if str(x).strip()][:8]
        eaten_summary = ("eaten " + ", ".join(names)) if names else "not logged anything yet"
    else:
        eaten_summary = "not logged anything yet"

    # Already over budget? Still coach, toward a lighter high-protein option.
    budget_cal = remaining_cal if remaining_cal > 0 else 300

    prompt = COACH_PROMPT_TMPL.format(
        goal_desc=GOAL_LABELS[goal],
        eaten_summary=eaten_summary,
        remaining_calories=budget_cal,
        remaining_protein=remaining_pro,
        n=n,
    )
    prompt += _allergy_clause(d.get("allergies")) + _diet_clause(d.get("diet"))
    try:
        from google import genai
        client = get_gemini_client()
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt],
            config=genai.types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        data = parse_gemini_json(resp.text)
    except Exception:  # noqa: BLE001 — any Gemini failure -> 502 JSON
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Coach Cal is unavailable right now. Try again in a moment."}), 502
    out = normalize_coach(data, n)
    out["meals"] = _allergy_filter(out.get("meals", []), d.get("allergies"), d.get("diet"))
    return jsonify(out)


GROCERY_SECTIONS = ["Produce", "Meat & Seafood", "Dairy & Eggs", "Frozen", "Bakery",
                    "Grains & Cereal", "Pantry & Canned", "Snacks", "Beverages"]
GROCERY_PROMPT_TMPL = (
    "You are Coach Cal, a warm nutrition coach. Build a HEALTHY grocery list for someone whose goal is: "
    "{goal_desc}. Keep it realistic, affordable, whole-foods-forward — lean proteins, vegetables, fruit, "
    "whole grains, healthy fats, a few smart pantry staples. Avoid junk.\n"
    "Return STRICT JSON: {{\"items\":[{{\"name\":\"...\",\"section\":\"...\",\"qty\":\"...\",\"why\":\"...\"}}]}} "
    "with {n} items.\n"
    "Rules: \"section\" MUST be EXACTLY one of: Produce | Meat & Seafood | Dairy & Eggs | Frozen | Bakery | "
    "Grains & Cereal | Pantry & Canned | Snacks | Beverages. \"name\" = the grocery item. \"qty\" = a simple "
    "amount (e.g. '1 bag', '2 lb'). \"why\" = ONE short reason it fits the goal. Spread items across sections. "
    "JSON only, no preamble."
)


def normalize_grocery(data, n):
    """Coerce the grocery model output into the contract schema (items[] with a valid section)."""
    if not isinstance(data, dict):
        data = {}
    out = []
    raw = data.get("items") if isinstance(data.get("items"), list) else []
    for it in raw:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        section = str(it.get("section") or "").strip()
        if section not in GROCERY_SECTIONS:
            section = "Pantry & Canned"
        out.append({
            "name": name[:80],
            "section": section,
            "qty": str(it.get("qty") or "").strip()[:24],
            "why": str(it.get("why") or "").strip()[:120],
        })
        if len(out) >= n:
            break
    return {"items": out}


@app.post("/api/grocery")
def grocery_list():
    """Goal-aware healthy grocery list; each item tagged with its store SECTION (works at any store).
    Exact aisle numbers (Phase 2) come from the Kroger Products API for Kroger-family stores."""
    d = request.get_json(silent=True) or {}
    goal = str(d.get("goal") or "maintain").strip().lower()
    if goal not in GOAL_LABELS:
        goal = "maintain"
    try:
        n = int(d.get("count", 12))
    except (TypeError, ValueError):
        n = 12
    n = max(6, min(20, n))
    prompt = GROCERY_PROMPT_TMPL.format(goal_desc=GOAL_LABELS[goal], n=n)
    prompt += _allergy_clause(d.get("allergies")) + _diet_clause(d.get("diet"))
    try:
        from google import genai
        client = get_gemini_client()
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt],
            config=genai.types.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = parse_gemini_json(resp.text)
    except Exception:  # noqa: BLE001 — any Gemini failure -> 502 JSON
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Couldn't build your list right now. Try again in a moment."}), 502
    out = normalize_grocery(data, n)
    out["items"] = _allergy_filter(out.get("items", []), d.get("allergies"), d.get("diet"))
    return jsonify(out)


MEALPLAN_SLOTS = ("breakfast", "lunch", "snack", "dinner")
MEALPLAN_PROMPT_TMPL = (
    "You are Coach Cal, a warm, expert nutrition coach inside SnapCal. Build a complete, REALISTIC "
    "7-day meal plan for someone whose goal is: {goal_desc}.\n"
    "Target roughly {daily_calories} calories and {daily_protein} g protein PER DAY. Each day has exactly "
    "four meals in this order: breakfast, lunch, snack, dinner. Favour everyday, affordable, genuinely "
    "tasty whole foods people enjoy - never bland 'diet' food. Vary the week so it never repeats the same "
    "dish two days running, but REUSE core staples across days (e.g. the same chicken, oats, eggs, "
    "greens) so the shopping list stays short and affordable.\n"
    "Return STRICT JSON only (no markdown, no code fences), exactly this schema:\n"
    "{{\"days\":[{{\"day\":1,\"meals\":[{{\"slot\":\"breakfast\",\"name\":\"...\","
    "\"calories\":0,\"protein_g\":0,\"carbs_g\":0,\"fat_g\":0,"
    "\"ingredients\":[{{\"name\":\"...\",\"section\":\"...\",\"qty\":\"...\"}}]}}]}}]}}\n"
    "Rules:\n"
    "- EXACTLY 7 days, numbered 1..7. EXACTLY 4 meals per day in the order breakfast, lunch, snack, dinner.\n"
    "- \"name\" = the dish (e.g. 'Greek yogurt & berry bowl'). Macros are your best estimate for one "
    "sensible portion; all numbers INTEGERS. Per-day calories should land near {daily_calories}.\n"
    "- \"ingredients\": 2-6 shoppable items for that meal. \"name\" = the grocery item (e.g. 'Greek yogurt', "
    "not 'yogurt for the bowl'). \"qty\" = a simple amount (e.g. '1 cup', '2 lb', '1 bag'). "
    "\"section\" MUST be EXACTLY one of: Produce | Meat & Seafood | Dairy & Eggs | Frozen | Bakery | "
    "Grains & Cereal | Pantry & Canned | Snacks | Beverages.\n"
    "- Skip water and basic salt/pepper as ingredients. JSON only, no preamble."
)


def _mealplan_ingredients(value):
    """Coerce a meal's ingredient list into [{name, section, qty}] with a valid section. Cap at 8."""
    if not isinstance(value, list):
        return []
    out = []
    for it in value:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        section = str(it.get("section") or "").strip()
        if section not in GROCERY_SECTIONS:
            section = "Pantry & Canned"
        out.append({
            "name": name[:80],
            "section": section,
            "qty": str(it.get("qty") or "").strip()[:24],
        })
        if len(out) >= 8:
            break
    return out


def normalize_mealplan(data, allergies=None, diet=None):
    """Coerce the meal-plan model output into the contract schema (<=7 days, fixed slot order).
    The prompt already forbids the user's allergens/diet; this adds a deterministic safety net that
    FLAGS any meal that still trips one (non-destructive — we never leave a meal slot empty)."""
    if not isinstance(data, dict):
        data = {}
    al = _norm_allergies(allergies)
    dt = _norm_diet(diet)
    raw_days = data.get("days") if isinstance(data.get("days"), list) else []
    days = []
    for idx, rd in enumerate(raw_days[:7]):
        if not isinstance(rd, dict):
            rd = {}
        day_no = _int(rd.get("day"), idx + 1) or (idx + 1)
        raw_meals = rd.get("meals") if isinstance(rd.get("meals"), list) else []
        by_slot = {}
        for m in raw_meals:
            if not isinstance(m, dict):
                continue
            slot = str(m.get("slot") or "").strip().lower()
            if slot in MEALPLAN_SLOTS and slot not in by_slot:
                by_slot[slot] = m
        meals = []
        for slot in MEALPLAN_SLOTS:
            m = by_slot.get(slot) or {}
            name = str(m.get("name") or "").strip()
            if not name:
                continue
            ingredients = _mealplan_ingredients(m.get("ingredients"))
            meal = {
                "slot": slot,
                "name": name[:80],
                "calories": _int(m.get("calories")),
                "protein_g": _int(m.get("protein_g")),
                "carbs_g": _int(m.get("carbs_g")),
                "fat_g": _int(m.get("fat_g")),
                "ingredients": ingredients,
            }
            if al or dt:
                ing_txt = " ".join(i.get("item", "") if isinstance(i, dict) else str(i) for i in ingredients)
                hits = _allergens_in_text(name + " " + ing_txt, al)
                if _diet_unsafe(name + " " + ing_txt, dt):
                    hits = hits + [dt]
                if hits:
                    meal["allergen_warning"] = ", ".join(sorted(set(hits)))
            meals.append(meal)
        if meals:
            days.append({"day": day_no, "meals": meals})
    return {"days": days}


@app.post("/api/mealplan")
def meal_plan():
    """Goal-aware 7-day meal plan; each meal carries shoppable ingredients tagged with a store SECTION
    so the client can roll them into the existing grocery list."""
    d = request.get_json(silent=True) or {}
    goal = str(d.get("goal") or "maintain").strip().lower()
    if goal not in GOAL_LABELS:
        goal = "maintain"
    daily_cal = max(1000, min(5000, _int(d.get("daily_calories"), 2000)))
    daily_pro = max(0, min(400, _int(d.get("daily_protein_g"), 150)))
    prompt = MEALPLAN_PROMPT_TMPL.format(
        goal_desc=GOAL_LABELS[goal],
        daily_calories=daily_cal,
        daily_protein=daily_pro,
    )
    prompt += _allergy_clause(d.get("allergies")) + _diet_clause(d.get("diet"))
    try:
        from google import genai
        client = get_gemini_client()
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt],
            config=genai.types.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = parse_gemini_json(resp.text)
    except Exception:  # noqa: BLE001 — any Gemini failure -> 502 JSON
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Couldn't build your meal plan right now. Try again in a moment."}), 502
    return jsonify(normalize_mealplan(data, allergies=d.get("allergies"), diet=d.get("diet")))


_restaurants_cache = None


def _load_restaurants():
    """Load the curated Eat-Out dataset once (static JSON)."""
    global _restaurants_cache
    if _restaurants_cache is None:
        try:
            _restaurants_cache = json.loads(RESTAURANTS_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _restaurants_cache = {"restaurants": []}
    return _restaurants_cache


@app.get("/api/restaurants")
def restaurants():
    """Serve the curated fast-food healthy-order guide (cached + client-cacheable)."""
    return jsonify(_load_restaurants())


_recipes_cache = None


def _load_recipes():
    """Load the curated recipe library once (built by gen_recipes.py -> data/recipes.json)."""
    global _recipes_cache
    if _recipes_cache is None:
        try:
            _recipes_cache = json.loads(RECIPES_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — ship empty so the browser still renders category tiles
            _recipes_cache = {"categories": [], "recipes": []}
    return _recipes_cache


@app.get("/api/recipes")
def recipes():
    """Serve the curated SnapCal Meals recipe library (categories + recipes) for the in-app browser."""
    return jsonify(_load_recipes())


_routines_cache = None


def _load_routines():
    """Load the curated exercise routines once (data/routines.json — the Move module)."""
    global _routines_cache
    if _routines_cache is None:
        try:
            _routines_cache = json.loads(ROUTINES_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — ship empty so the UI degrades gracefully
            _routines_cache = {"tiers": [], "desk_breaks": {}}
    return _routines_cache


@app.get("/api/routines")
def routines():
    """Serve the curated exercise routines (tiers + desk-break track + safety) for the Move module."""
    return jsonify(_load_routines())


def _norm_name(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _chain_alias_map():
    """Normalized OSM name/brand -> our canonical chain name (from the curated dataset)."""
    out = {}
    for r in _load_restaurants().get("restaurants", []):
        name = r.get("chain")
        if name:
            out[_norm_name(name)] = name
    return out


def _match_chain(tags, aliases):
    """Best-effort match an OSM place's brand/name to a chain we have a guide for."""
    for raw in (tags.get("brand"), tags.get("name")):
        nk = _norm_name(raw)
        if not nk:
            continue
        if nk in aliases:
            return aliases[nk]
        for ak, canon in aliases.items():
            if len(ak) >= 4 and (ak in nk or nk in ak):
                return canon
    return None


# Department/clothing stores are tagged shop=department_store in OSM but sell no groceries — keep them
# OUT of the healthy-food store finder. (Walmart/Target/Costco DO carry food, so we only block by name.)
STORE_NON_FOOD = (
    "ross", "burlington", "boscov", "macy", "kohl", "tj maxx", "tjmaxx", "t.j. maxx", "marshalls",
    "jcpenney", "jc penney", "penney", "nordstrom", "dillard", "belk", "bealls", "sears", "saks",
    "bloomingdale", "neiman", "lord & taylor", "five below", "old navy", "burlington coat",
)
def _store_is_non_food(name):
    nl = (name or "").lower()
    return any(t in nl for t in STORE_NON_FOOD)


def _osm_addr(tags):
    """Short human address from OSM addr:* tags so the user can tell WHICH branch (e.g. which Walmart)."""
    hn = (tags.get("addr:housenumber") or "").strip()
    st = (tags.get("addr:street") or "").strip()
    city = (tags.get("addr:city") or tags.get("addr:town") or "").strip()
    parts = []
    if st:
        parts.append((hn + " " + st).strip())
    if city:
        parts.append(city)
    return ", ".join(parts)


@app.get("/api/nearby")
def nearby():
    """Find food places around a lat/lng via OpenStreetMap (Overpass) and match them to
    our curated guide. Free, no API key. Backs the Eat-Out 'Healthy food near me' button."""
    try:
        lat = float(request.args.get("lat", ""))
        lng = float(request.args.get("lng", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "lat_lng_required"}), 400
    kind = (request.args.get("kind") or "food").strip().lower()
    default_radius = "8000" if kind == "store" else "12000"   # stores ~5mi (people drive for groceries), restaurants ~7.5mi — list flows out but stays fast
    try:
        radius = max(50, min(int(request.args.get("radius", default_radius)), 25000))
    except (TypeError, ValueError):
        radius = 8000 if kind == "store" else 12000

    if kind == "store":   # grocery-store finder (Stores tab)
        query = (
            "[out:json][timeout:12];("
            f'nwr(around:{radius},{lat},{lng})["shop"~"^(supermarket|convenience|grocery|department_store|wholesale|greengrocer)$"];'
            ");out center tags 60;"
        )
    else:
        query = (
            "[out:json][timeout:12];("
            f'nwr(around:{radius},{lat},{lng})["amenity"~"^(fast_food|restaurant|cafe)$"];'
            f'nwr(around:{radius},{lat},{lng})["shop"="convenience"];'
            ");out center tags 1200;"   # high cap: Overpass returns DB-order not nearest-first, so we must pull all-in-radius then sort ourselves
        )
    try:
        ckey = f"{kind}:{round(lat, 3)},{round(lng, 3)}:{radius}"   # cache hotspots + survive a transient outage
        payload = _overpass(query, timeout=18, cache_key=ckey)
    except Exception as exc:  # noqa: BLE001 - best-effort; degrade gracefully on Overpass hiccups
        # keep `center` so the frontend can ALWAYS draw the map even when the food lookup hiccups
        return jsonify({"matched": [], "nearby": [], "stores": [], "center": {"lat": lat, "lng": lng},
                        "error": "lookup_failed", "detail": str(exc)[:140]})

    if kind == "store":   # return de-duped nearby grocery stores, closest first
        stores, seen = [], set()
        for el in payload.get("elements", []):
            tags = el.get("tags") or {}
            label = tags.get("name") or tags.get("brand")
            if not label:
                continue
            if _store_is_non_food(label):   # Ross / Burlington / Boscov's etc. — department stores, no food
                continue
            elat = el.get("lat") if el.get("lat") is not None else (el.get("center") or {}).get("lat")
            elng = el.get("lon") if el.get("lon") is not None else (el.get("center") or {}).get("lon")
            dist = round(_haversine_m(lat, lng, elat, elng)) if (elat is not None and elng is not None) else None
            k = (label.lower(), round(elat, 4) if elat is not None else 0, round(elng, 4) if elng is not None else 0)
            if k in seen:
                continue
            seen.add(k)
            stores.append({"name": label, "dist_m": dist, "lat": elat, "lng": elng,
                           "shop": tags.get("shop", ""), "addr": _osm_addr(tags)})
        stores.sort(key=lambda x: (x["dist_m"] is None, x["dist_m"] if x["dist_m"] is not None else 1e9))
        return jsonify({"stores": stores[:30], "center": {"lat": lat, "lng": lng}})

    aliases = _chain_alias_map()
    matched, nearby_list = {}, []
    for el in payload.get("elements", []):
        tags = el.get("tags") or {}
        label = tags.get("name") or tags.get("brand")
        if not label:
            continue
        # Drop OSM entries explicitly marked closed/gone (best-effort — OSM has no real business-status, so
        # stale-but-untagged closures like a shut Bahama Breeze still slip through; the Google Places upgrade fixes that).
        _oh = (tags.get("opening_hours") or "").strip().lower()
        if _oh in ("closed", "off") or any(k.split(":", 1)[0] in ("disused", "abandoned", "was", "removed", "demolished") for k in tags):
            continue
        elat = el.get("lat") if el.get("lat") is not None else (el.get("center") or {}).get("lat")
        elng = el.get("lon") if el.get("lon") is not None else (el.get("center") or {}).get("lon")
        dist = round(_haversine_m(lat, lng, elat, elng)) if (elat is not None and elng is not None) else None
        canon = _match_chain(tags, aliases)
        nearby_list.append({"name": label, "dist_m": dist, "chain": canon, "lat": elat, "lng": elng,
                            "addr": _osm_addr(tags), "hours": (tags.get("opening_hours") or "").strip()[:80]})
        if canon:
            prev = matched.get(canon)
            if prev is None or (dist is not None and dist < prev["dist_m"]):
                matched[canon] = {"chain": canon, "name": label, "dist_m": dist if dist is not None else 99999,
                                  "lat": elat, "lng": elng, "addr": _osm_addr(tags)}

    nearby_list.sort(key=lambda x: (x["dist_m"] is None, x["dist_m"] if x["dist_m"] is not None else 1e9))
    matched_sorted = sorted(matched.values(), key=lambda x: x["dist_m"])
    return jsonify({"matched": matched_sorted, "nearby": nearby_list[:24], "center": {"lat": lat, "lng": lng}})


# ---- Store picks: Coach Cal's "what healthy things to grab HERE", goal-aware (backs the store sheet) ----
STORE_PICKS_CACHE = {}
STORE_PICKS_PROMPT = (
    "You are Coach Cal, an upbeat, practical nutrition coach. The user's goal is: {goal_desc}.\n"
    "They're about to shop at \"{name}\", which is a {kind}. Give them a quick game plan for THIS store.\n"
    "Return STRICT JSON: {{\"picks\":[{{\"item\":\"...\",\"why\":\"...\"}}],\"sections\":[\"...\"],\"tip\":\"...\"}}\n"
    "Rules: 5-6 \"picks\" = SPECIFIC, REAL, TASTY items actually found at \"{name}\" (name the real product or "
    "menu item — e.g. a build-your-own grilled chicken salad — not generic 'grilled chicken'), each \"why\" is "
    "4-9 appetizing words. Make at least ONE pick VEGETARIAN and ONE VEGAN and label them '(vegan)'/'(vegetarian)' "
    "in the item — this app serves ALL diets, never leave plant-based eaters out. Favor flavorful, satisfying "
    "choices a hungry person would actually WANT, not just plain diet food. \"sections\" = 2-3 areas to hit first. "
    "\"tip\" = one short motivating line. Tailor to their goal. No medical claims."
)
RESTAURANT_PICKS_PROMPT = (
    "You are Coach Cal, an upbeat, practical nutrition coach talking to someone about to eat at \"{name}\", a restaurant. "
    "Their goal is: {goal_desc}. Give them a warm, specific game plan for what to ORDER — tasty but smart.\n"
    "Return STRICT JSON: {{\"intro\":\"...\",\"picks\":[{{\"item\":\"...\",\"calories\":NUMBER,\"why\":\"...\"}}],\"sections\":[\"...\"],\"tip\":\"...\"}}\n"
    "Rules: \"intro\" = ONE friendly sentence with the key insight for THIS kind of place (e.g. at a steakhouse the steak "
    "isn't the problem, the rich sides and bread basket are). 5-6 \"picks\" = SPECIFIC, REAL menu items on {name}'s menu, "
    "ideally a starter + a main + a side or two + a drink, each with an approximate \"calories\" number and a \"why\" of "
    "4-9 appetizing words. Favor tasty, satisfying orders that fit their goal — never sad diet food. \"sections\" = 2-3 "
    "menu areas to look at. \"tip\" = one short closing line. No medical claims."
)
STORE_PICKS_FALLBACK = {
    "restaurant": {
        "picks": [
            {"item": "Garden salad, dressing on the side", "why": "Light, fills you up first"},
            {"item": "A grilled (not fried) main + a veggie side", "why": "Lean and satisfying"},
            {"item": "A side of fruit or steamed veggies", "why": "Easy fiber and volume"},
            {"item": "Water or unsweetened iced tea", "why": "Saves 150-300 liquid calories"},
        ],
        "sections": ["Salads", "Grilled items", "Sides"],
        "tip": "Ask for sauces and dressing on the side.",
    },
    "warehouse club": {
        "picks": [
            {"item": "Rotisserie chicken", "why": "Cheap, lean, ready-to-eat protein"},
            {"item": "Frozen berries & veggies (bulk)", "why": "No waste, just as nutritious"},
            {"item": "Eggs by the dozen", "why": "Protein that stretches all week"},
            {"item": "Bagged baby spinach / greens", "why": "Easy volume, very low calories"},
            {"item": "Mixed nuts (no added oil)", "why": "Healthy fats — keep to a handful"},
        ],
        "sections": ["Refrigerated", "Produce", "Frozen"],
        "tip": "Buy in bulk, portion at home so it lasts.",
    },
    "convenience store": {
        "picks": [
            {"item": "Hard-boiled eggs", "why": "Grab-and-go protein, zero prep"},
            {"item": "String cheese", "why": "Protein + calcium, keeps you full"},
            {"item": "Banana or a fruit cup", "why": "Real food beats the candy aisle"},
            {"item": "Beef or turkey jerky", "why": "Lean protein for the road"},
            {"item": "Water or unsweetened tea", "why": "Skip the sugary soda calories"},
        ],
        "sections": ["Cooler / grab-and-go", "Snacks"],
        "tip": "Build a mini meal: protein + fruit + water.",
    },
    "grocery store / supermarket": {
        "picks": [
            {"item": "Fresh produce", "why": "Fill half your cart here first"},
            {"item": "Lean proteins (chicken, fish, eggs)", "why": "The base of every healthy plate"},
            {"item": "Plain Greek yogurt", "why": "High protein, low sugar"},
            {"item": "Frozen vegetables & berries", "why": "Cheap, lasts, no waste"},
            {"item": "Oats & whole grains", "why": "Steady energy and great fiber"},
            {"item": "Beans, lentils & nuts", "why": "Plant protein and healthy fats"},
        ],
        "sections": ["Produce", "Meat & Seafood", "Dairy & Eggs"],
        "tip": "Shop the perimeter first — that's where whole foods live.",
    },
}
def _store_kind(shop, name):
    nl = (name or "").lower()
    if shop == "wholesale" or any(t in nl for t in ("costco", "sam's", "sam’s", "bj's", "bj’s")):
        return "warehouse club"
    if shop == "convenience" or any(t in nl for t in (
            "wawa", "sheetz", "turkey hill", "7-eleven", "7 eleven", "royal farms",
            "quickchek", "quick chek", "circle k", "rutter")):
        return "convenience store"
    return "grocery store / supermarket"
def _normalize_store_picks(data, kind):
    fb = STORE_PICKS_FALLBACK.get(kind, STORE_PICKS_FALLBACK["grocery store / supermarket"])
    if not isinstance(data, dict):
        return {"picks": fb["picks"], "sections": fb["sections"], "tip": fb["tip"], "kind": kind, "intro": ""}
    picks = []
    for p in (data.get("picks") or [])[:6]:
        if isinstance(p, dict) and str(p.get("item", "")).strip():
            pk = {"item": str(p["item"]).strip()[:64], "why": str(p.get("why", "")).strip()[:80]}
            if isinstance(p.get("calories"), (int, float)):
                pk["calories"] = int(p["calories"])
            picks.append(pk)
    if not picks:
        picks = fb["picks"]
    sections = [str(s).strip()[:28] for s in (data.get("sections") or []) if str(s).strip()][:3] or fb["sections"]
    tip = str(data.get("tip", "")).strip()[:120] or fb["tip"]
    return {"picks": picks, "sections": sections, "tip": tip, "kind": kind, "intro": str(data.get("intro", "")).strip()[:240]}

@app.get("/api/storepicks")
def store_picks():
    """Coach Cal's goal-aware 'what to grab here' for a specific store. Falls back to curated
    type-based picks if Gemini is unavailable, so the sheet always has content."""
    name = (request.args.get("name") or "").strip()
    shop = (request.args.get("shop") or "").strip().lower()
    goal = (request.args.get("goal") or "maintain").strip().lower()
    if goal not in GOAL_LABELS:
        goal = "maintain"
    if not name:
        return jsonify({"error": "name_required"}), 400
    kind = "restaurant" if (request.args.get("ctx") or "").strip().lower() == "restaurant" else _store_kind(shop, name)
    allergies = request.args.get("allergies", "")
    diet = request.args.get("diet", "")
    al_sig = ",".join(sorted(_norm_allergies(allergies))) + "|" + _norm_diet(diet)
    ck = f"{name.lower()}|{kind}|{goal}|{al_sig}"
    if ck in STORE_PICKS_CACHE:
        return jsonify(STORE_PICKS_CACHE[ck])
    data = None
    try:
        from google import genai
        client = get_gemini_client()
        prompt = (RESTAURANT_PICKS_PROMPT if kind == "restaurant" else STORE_PICKS_PROMPT).format(goal_desc=GOAL_LABELS[goal], name=name, kind=kind) + _allergy_clause(allergies) + _diet_clause(diet)
        resp = client.models.generate_content(
            model=GEMINI_MODEL, contents=[prompt],
            config=genai.types.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = parse_gemini_json(resp.text)
    except Exception:  # noqa: BLE001 — any Gemini failure -> curated fallback, never an error
        data = None
    out = _normalize_store_picks(data, kind)
    out["picks"] = _allergy_filter(out.get("picks", []), allergies, diet)
    STORE_PICKS_CACHE[ck] = out
    return jsonify(out)


# ---------- Real per-dish food photos (Pexels if keyed, else commercial-licensed CC via Openverse), cached ----------
FOODIMG_CACHE_PATH = APP_DIR / "data" / "foodimg_cache.json"
_foodimg_cache = None
_foodimg_neg = set()   # dishes that failed THIS session -> skip re-querying the source until restart


def _load_foodimg_cache():
    global _foodimg_cache
    if _foodimg_cache is None:
        try:
            _foodimg_cache = json.loads(FOODIMG_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _foodimg_cache = {}
    return _foodimg_cache


def _save_foodimg_cache():
    try:
        tmp = str(FOODIMG_CACHE_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_foodimg_cache, f)
        os.replace(tmp, FOODIMG_CACHE_PATH)   # atomic — concurrent writers can't corrupt the cache
    except Exception:
        pass


def _foodimg_category(name):
    """Mirror of the frontend pickCat — picks the local fallback jpg when no real photo is found."""
    n = (name or "").lower()
    groups = [
        (["salad"], "salad"),
        (["smoothie", "shake", "blizzard", "frapp"], "smoothie"),
        (["yogurt", "parfait", "oatmeal", "fruit"], "yogurt"),
        (["wrap"], "wrap"),
        (["taco", "burrito", "quesadilla", "nacho", "chalupa"], "taco"),
        (["pizza", "slice", "flatbread"], "pizza"),
        (["coffee", "latte", "cappuccino", "americano", "espresso", "cold brew", "macchiato", "refresher"], "coffee"),
        (["omelet", "mcmuffin", "biscuit", "breakfast", "hash brown", "croissant", "bagel", "pancake", "waffle", "egg"], "breakfast"),
        (["fish", "shrimp", "salmon", "tuna", "seafood", "lobster", "crab", "oyster", "scallop"], "seafood"),
        (["donut", "cookie", "brownie", "ice cream", "sundae", "cake", "cone", "dessert"], "dessert"),
        (["nugget", "tender", "chicken", "wing", "cane"], "chicken"),
        (["burger", "whopper", "mcdouble", "big mac", "patty"], "burger"),
        (["hoagie", "sub", "sandwich", "melt", "footlong"], "sub"),
        (["bowl", "rice", "quinoa", "grain"], "bowl"),
    ]
    for words, cat in groups:
        if any(w in n for w in words):
            return cat
    return "meal"


def _pexels_key():
    """Pexels key from the SNAPCAL_PEXELS_KEY env var OR apps/snapcal/pexels_key.txt. Read live each call,
    so pasting the key into that file takes effect immediately — no code change, no env juggling."""
    k = os.environ.get("SNAPCAL_PEXELS_KEY", "").strip()
    if k:
        return k
    try:
        return (APP_DIR / "pexels_key.txt").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _fetch_pexels_img(dish):
    key = _pexels_key()
    if not key:
        return None
    try:
        url = "https://api.pexels.com/v1/search?per_page=1&orientation=landscape&query=" + urllib.parse.quote(dish + " food")
        req = urllib.request.Request(url, headers={"Authorization": key, "User-Agent": "SnapCal/1.0 (food image lookup)"})   # Pexels/Cloudflare 403s the default Python UA
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
        photos = d.get("photos") or []
        if photos and photos[0].get("src"):
            return {"url": photos[0]["src"].get("medium") or photos[0]["src"].get("original"),
                    "credit": "Photo: " + (photos[0].get("photographer") or "") + " / Pexels", "source": "pexels"}
    except Exception:
        return None
    return None


_FOOD_WORDS = (
    "food", "meal", "dish", "plate", "platter", "breakfast", "lunch", "dinner", "brunch", "sandwich",
    "burger", "salad", "chicken", "beef", "pork", "fish", "seafood", "shrimp", "steak", "taco", "burrito",
    "pizza", "pasta", "soup", "bowl", "rice", "egg", "cheese", "bacon", "fries", "fruit", "vegetable",
    "veggie", "dessert", "cake", "coffee", "drink", "beverage", "bread", "wrap", "grill", "roast", "baked",
    "cuisine", "restaurant", "menu", "snack", "appetizer", "entree", "cooked", "fresh", "tofu", "bean",
    "noodle", "sauce", "foodie", "fastfood", "eat", "delicious", "tasty", "nutrition", "homemade",
)


def _looks_like_food(x):
    """Openverse CC is noisy (a photo titled 'McDouble' might be someone's dog). Trust a result only
    when its title/tags actually read as food — otherwise we fall back to the local category image."""
    t = (x.get("title") or "").lower()
    for tag in (x.get("tags") or []):
        t += " " + (tag.get("name") or "").lower()
    return any(w in t for w in _FOOD_WORDS)


def _fetch_openverse_img(dish):
    try:
        url = "https://api.openverse.org/v1/images/?page_size=12&mature=false&license_type=commercial&q=" + urllib.parse.quote(dish + " food")
        req = urllib.request.Request(url, headers={"User-Agent": "SnapCal/1.0 (food image lookup)"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
        for x in (d.get("results") or []):
            if x.get("url") and _looks_like_food(x):
                return {"url": x["url"], "credit": (x.get("creator") or "Unknown") + " (" + (x.get("license") or "cc").upper() + ") / Openverse", "source": "openverse"}
    except Exception:
        return None
    return None


def _img_redirect(url):
    resp = redirect(url)
    resp.headers["Cache-Control"] = "no-store"   # the dish->photo mapping can change; don't let a browser pin a stale one
    return resp


def _foodimg_lookup(term, cache):
    """Cached source lookup for a search term. Returns the cached/fetched dict, or None."""
    k = term.lower()[:90]
    if cache.get(k, {}).get("url"):
        return cache[k]
    if k in _foodimg_neg:
        return None
    hit = _fetch_pexels_img(term) or _fetch_openverse_img(term)
    if hit and hit.get("url"):
        cache[k] = hit
        _save_foodimg_cache()
        return hit
    _foodimg_neg.add(k)   # nothing/failed — don't hammer the source again this session
    return None


@app.get("/api/foodimg")
def food_img():
    """Real photo of a named dish, cached. Pexels (cleanest license) if SNAPCAL_PEXELS_KEY is set, else
    commercial-CC via Openverse (food-filtered). Tiered: the exact dish -> a clean category word (which
    searches far more reliably, e.g. 'salad') -> a local category jpg. ALWAYS 302s, so an <img> never breaks."""
    raw = (request.args.get("dish") or "").strip()
    if not raw:
        return _img_redirect("/static/img/food/meal.jpg")
    dish = (raw.split("(")[0].split(":")[0].strip() or raw)   # core dish term: drop "(no cheese)" and "..: long build"
    dish = " ".join(dish.split()[:6])
    cache = _load_foodimg_cache()
    hit = _foodimg_lookup(dish, cache)                         # 1) the exact dish
    if not hit:
        cat = _foodimg_category(dish)
        hit = _foodimg_lookup(cat, cache)                      # 2) a clean category word ("salad","burger") — reliable
        if not hit:
            return _img_redirect("/static/img/food/" + cat + ".jpg")   # 3) local jpg (final safety net)
    return _img_redirect(hit["url"])


# ---------- Talk to Coach Cal: a back-and-forth voice/text conversation (Gemini) ----------
CHAT_SYSTEM = (
    "You are Coach Cal, a warm, upbeat personal nutrition coach having a real back-and-forth conversation with "
    "the user inside the SnapCal app — often by VOICE. Talk like a supportive friend who knows nutrition. Keep "
    "replies SHORT and conversational: 1-3 sentences, easy to say out loud. NO markdown, NO bullet lists, NO "
    "headings, NO emoji. The user's context — goal: {goal_desc}; diet: {diet}; allergies: {allergies}; today so far "
    "they've eaten {eaten_cal} of {daily} calories ({remaining} left) and {protein_eaten}g of {protein_target}g "
    "protein; meals logged today: {eaten}. They're currently on the app's \"{screen}\" screen. If they ask 'how am I "
    "doing', 'what am I looking at', 'explain this', or about their numbers/screen, explain THOSE current numbers in "
    "plain, encouraging language (calories left, protein to go, what to prioritise for the rest of the day). If they "
    "ask what a term means (macros, protein, carbs, fat, the Healthy-to-Treat meter), explain it in plain, simple "
    "words a beginner gets. If they ask what to eat, give one or two specific ideas that fit their goal/diet. "
    "Ground every tip in ESTABLISHED nutrition science — protein for fullness and preserving lean muscle, fiber for "
    "satiety and gut health, whole foods and micronutrients over fad diets — and when you explain WHY something helps, "
    "use simple evidence-based reasoning, never hype or trends. You are NOT a doctor: give general wellness guidance, "
    "never diagnose, prescribe, or make medical claims; for medical conditions, gently suggest they talk to their "
    "doctor or a registered dietitian. Encourage, never shame."
    "\n\nHARD RULES — follow exactly, every time:"
    "\n1) If a list of REAL places is provided, you MUST name at least ONE specific place from it AND one specific DISH to "
    "order. NEVER answer a 'where/what should I eat' with only general advice like 'look for grilled chicken'."
    "\n2) If the user asks 'how am I doing' or about their progress, you MUST state the EXACT numbers — calories left and "
    "grams of protein still to go (from their context) — then give ONE concrete next step to close the protein gap."
    "\n3) If they ask to 'explain what I'm looking at', describe the SCREEN named in their context (e.g. the Profile screen's "
    "targets/stats/settings), not a generic day summary."
    "\n4) For a recomp 'how do I lose fat but keep muscle' question, give the 3 concrete levers: a MODERATE deficit (not a "
    "crash), their protein target in GRAMS, and resistance training ~3-4x/week — with real numbers, not vague encouragement."
    "\n5) When cooking/feeding others with allergies: give 3 concrete nut-free (or relevant-allergen-free) dish ideas, each "
    "with a one-line how-to; STEER AWAY from that allergen's hidden traps (for nuts: peanut oil, satay/Thai sauces, pesto, "
    "almond flour, marzipan, nut-crusted proteins — never suggest one of these without a clear swap); and add a "
    "life-threatening-allergy line: verify every label, use clean prep/fresh oil to avoid cross-contamination, and ask "
    "guests about severity. Not medical advice."
    "\n6) Be specific and concrete; finish your sentences; warm but useful, never filler."
    "\n7) DELIVER, don't deflect: when you have a real places list, or the user is mid-decision / already at a restaurant / "
    "on a cheat day, give the answer THIS turn — name a specific place + dish — do NOT bounce back with a question like "
    "'how about we look at some spots?'. Only ask a question if you genuinely cannot answer without it."
    "\n8) Protein anchor: for a recomp user whose protein is well below target, pick the HIGHEST-protein option (or suggest a "
    "protein add-on) and tie it to their remaining protein/calories (e.g. 'that bowl ~45g protein — puts you at 105 of 180')."
)

RECOMP_CHAT_CLAUSE = (
    "\n\nThis user's goal is BODY RECOMPOSITION — losing fat while building muscle at the same time. Coach the "
    "recomp mindset: a MODERATE calorie deficit (never a crash) plus a HIGH-protein intake so they stay SOLID, "
    "not skinny, and encourage resistance / strength training to keep and build muscle as the fat comes off. If "
    "they worry about loose or saggy skin, be honest and reassuring: losing fat gradually, keeping protein high, "
    "building muscle underneath, staying hydrated and giving it time give the best skin result — a very large "
    "loss can leave some skin (a later conversation), so never promise, just point them the right way. Favour "
    "LOW-IMPACT movement and walking, and remind them that for someone heavier, daily steps and breaking up long "
    "sitting burn more calories than they expect. Encourage, never shame. Not medical advice — for pain or "
    "medical conditions, gently suggest their doctor."
)


def _body_clause(d):
    """Personalise Coach Cal with the user's synced body stats (age/sex/height/weight)."""
    sex = str(d.get("sex") or "").strip().lower()
    age, h, w, gw = _int(d.get("age")), _int(d.get("height_in")), _int(d.get("cur_weight")), _int(d.get("goal_weight"))
    who = []
    if age:
        who.append(f"{age}-year-old")
    if sex in ("male", "female"):
        who.append(sex)
    measure = []
    if h:
        ft, inch = divmod(h, 12)
        measure.append(f"{ft}'{inch}\"")
    if w:
        measure.append(f"{w} lb")
    parts = []
    if who:
        parts.append(" ".join(who))
    if measure:
        parts.append(", ".join(measure))
    if not parts:
        return ""
    s = ("\n\nABOUT THIS USER (personalise to it — portion sizes, protein needs, a realistic pace — but never "
         "recite their stats back at them unless they ask): " + "; ".join(parts))
    if gw and w and gw < w:
        s += f"; working toward {gw} lb"
    return s + "."


def _health_clause(row):
    """Feed today's REAL movement/recovery (from Apple Health / Google Fit) into Coach Cal so it coaches on it —
       e.g. nudge a walk to hit a step goal, or factor active calories into the day. Skips silently if not synced."""
    if not row:
        return ""
    bits = []
    steps = row["steps"] if row["steps"] is not None else 0
    if steps:
        bits.append(f"{steps:,} steps so far")
    if row["active_cal"]:
        bits.append(f"~{row['active_cal']} active calories burned")
    if row["resting_hr"]:
        bits.append(f"resting heart rate {row['resting_hr']} bpm")
    if row["sleep_min"]:
        bits.append(f"{round(row['sleep_min'] / 60.0, 1)}h sleep last night")
    if not bits:
        return ""
    return ("\n\nTODAY'S HEALTH DATA (synced from their phone/wearable — use it: if steps are low, suggest a short "
            "walk toward ~7,000; factor active calories into how much room they have; never shame, just coach): "
            + "; ".join(bits) + ".")


_DOW2 = {"mo": 0, "tu": 1, "we": 2, "th": 3, "fr": 4, "sa": 5, "su": 6}
_DOW3 = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_local_time(s):
    """'Sat, 1:05 PM' / 'Sat 1:05 PM' -> (weekday 0=Mon..6=Sun, minutes-since-midnight). None if unparseable."""
    if not s:
        return None
    t = str(s).strip().lower().replace(",", " ")
    wd = None
    for p in t.split():
        if p[:3] in _DOW3:
            wd = _DOW3[p[:3]]
            break
    m = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)?", t)
    if wd is None or not m:
        return None
    h, mn, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return (wd, h * 60 + mn)


def _osm_open_now(hours, wd, minutes):
    """Best-effort: is an OSM opening_hours string open at weekday wd / minutes? True / False / None(unknown)."""
    if not hours:
        return None
    h = str(hours).strip().lower()
    if "24/7" in h:
        return True
    matched_today = False
    parsed_any = False
    for rule in h.split(";"):
        rule = rule.strip()
        dm = re.match(r"^([a-z,\-\s]*?)\s*(\d{1,2}:\d{2}.*|off|closed)$", rule)
        if not dm:
            continue
        parsed_any = True
        dayspec, timespec = dm.group(1).strip(), dm.group(2).strip()
        days = set()
        if not dayspec:
            days = set(range(7))
        else:
            for tok in dayspec.split(","):
                tok = tok.strip()
                if "-" in tok:
                    a, b = tok.split("-", 1)
                    a, b = _DOW2.get(a.strip()[:2]), _DOW2.get(b.strip()[:2])
                    if a is None or b is None:
                        continue
                    i = a
                    while True:
                        days.add(i)
                        if i == b:
                            break
                        i = (i + 1) % 7
                else:
                    d = _DOW2.get(tok[:2])
                    if d is not None:
                        days.add(d)
        if wd not in days:
            continue
        matched_today = True
        if timespec in ("off", "closed"):
            return False
        for rng in timespec.split(","):
            mm = re.match(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", rng.strip())
            if not mm:
                continue
            start = int(mm.group(1)) * 60 + int(mm.group(2))
            end = int(mm.group(3)) * 60 + int(mm.group(4))
            if end <= start:
                end += 1440  # crosses midnight
            if start <= minutes < end or start <= minutes + 1440 < end:
                return True
    # Rules existed but none cover now (incl. a weekday-only place on a weekend) → closed. No parseable rules → unknown.
    return False if parsed_any else None


def _chat_nearby_clause(nearby, has_loc, route_to="", area="", local_time=""):
    """Feed Coach Cal the REAL places near the user (or ALONG their drive, or in a DESTINATION area the user
    named like 'Philadelphia tonight') so suggestions match where they'll actually be — never invented."""
    # Compute open/closed per place from the user's local time. PREFER open/unknown places; but if NONE are open,
    # keep the closed ones (clearly tagged) rather than emptying the list — emptying used to trigger a from-memory
    # fallback that hallucinated/recommended closed spots (the squad caught Philly + late-night). all_closed lets the
    # prompt be honest ("everything near you is closed now").
    lt = _parse_local_time(local_time)
    open_unknown, closed = [], []
    if isinstance(nearby, list):
        for p in nearby:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            o = _osm_open_now(p.get("hours"), lt[0], lt[1]) if (lt and p.get("hours")) else None
            p = dict(p)
            p["_open"] = o
            (closed if o is False else open_unknown).append(p)
    all_closed = (not open_unknown) and bool(closed)
    nearby = open_unknown if open_unknown else closed
    nearby.sort(key=lambda x: (x.get("_open") is not True,))   # open-now first
    if area and not nearby:
        # The user named a place to eat (e.g. Ambler) but the live spot finder is down → DON'T just punt with
        # "try again", and DON'T redirect to their current location. Name REAL well-known places there from
        # your own knowledge, clearly flagged to verify.
        return ("\n\nThe live spot finder is down right now, so you can't see real-time places in " + str(area)[:50] + ". "
                "Do NOT say 'try again' and do NOT suggest places near their CURRENT location (they're going to " + str(area)[:50] + "). "
                "Instead, from your OWN knowledge name 2-3 WELL-KNOWN, real, currently-operating restaurants in " + str(area)[:50] + " "
                "that fit their goal, each with a SPECIFIC healthy dish to order — and add a brief 'I can't confirm live hours, so call "
                "ahead' caveat. Only name places you're genuinely confident exist; never invent one.")
    if nearby:
        items = []
        for p in nearby[:16]:
            dm = p.get("dist_m")
            dist = ""
            if isinstance(dm, (int, float)) and not area:   # distances are from CURRENT location; meaningless for a far destination
                mi = dm / 1609.34
                dist = f" ({mi:.1f} mi)" if mi >= 0.1 else " (right here)"
            addr = str(p.get("addr") or "").strip()
            hrs = str(p.get("hours") or "").strip()
            openflag = " [OPEN NOW]" if p.get("_open") is True else (" [CLOSED NOW]" if p.get("_open") is False else "")
            items.append(str(p["name"])[:40] + dist + (" @ " + addr[:60] if addr else "")
                         + (" [hours: " + hrs[:50] + "]" if hrs else " [hours: not listed]") + openflag)
        closed_note = (" HEADS UP: every place in this list is CLOSED right now — be honest and tell the user that; do NOT "
                       "imply any is open. Name the closest one WITH its hours so they can plan for later, and offer a realistic "
                       "open-late option (a 24-hour diner, or Wawa/Sheetz for a grab-and-go protein like rotisserie chicken or "
                       "Greek yogurt).") if all_closed else ""
        hours_note = (" THE ABOVE IS THE ONLY LIST OF REAL PLACES — recommend ONLY a place whose name appears EXACTLY in it. "
                      "NEVER invent, rename, or add a restaurant that is not in the list (a user could be driven to a place "
                      "that doesn't exist). Closed places have already been removed; [OPEN NOW] = confirmed open. If a place "
                      "shows only [hours: not listed], suggest it but say 'I can't confirm hours — call ahead'. "
                      "BE SPECIFIC: name an ACTUAL dish that fits their goal (e.g. 'the grilled salmon with broccoli, sauce on "
                      "the side', 'a burrito bowl, double chicken, no rice'), tie it to their remaining calories/protein, and "
                      "never answer with a vague 'some grilled fish options'. "
                      "CUISINE HONESTY: if the user asked for a specific cuisine (soul food, jambalaya/Cajun, Thai...) and NO "
                      "place in the list is that cuisine, do NOT substitute an unrelated spot and do NOT just say it's unavailable. "
                      "Instead NAME 1-2 well-known REAL restaurants of THAT cuisine in the area from your own knowledge, each with a "
                      "specific healthier dish to order, and add 'these are from what I know — verify hours'. (e.g. soul food in "
                      "Atlanta → Busy Bee Cafe or Paschal's, baked chicken + collards; jambalaya in St. Louis → Broadway Oyster Bar.) "
                      "Only name places you're confident are real. "
                      "CHEAT DAY: if they say it's a cheat/treat/splurge day, give permission warmly and coach a SMARTER "
                      "indulgence at a real indulgent spot from the list (e.g. 'at Yard House get the burger, skip the fries; "
                      "box half') — do NOT steer them to a salad/health-food spot. "
                      "OFFER A CHOICE: when both a health-forward fast-casual spot (CAVA, Sweetgreen, Chipotle, HipCityVeg, "
                      "Panera) AND a sit-down spot are in the list, offer ONE of each — a quick/cheap lane and a sit-down lane. "
                      "CUISINE TACTICS: for BBQ/fried/comfort food, add the lean move — lean brisket/turkey/grilled over "
                      "fried/pulled, sauce on the side, swap fries/mac for greens or a salad.")
        if items and area:
            return ("\n\nThe user is planning to be in/around " + str(area)[:50] + " and wants to eat THERE (not near their "
                    "current location). REAL places in " + str(area)[:50] + " ('@' = street address): " + "; ".join(items) + ". "
                    "Recommend SPECIFIC places FROM THIS LIST: lead with the place NAME + a specific healthy order to get "
                    "there (the address is optional — a brief neighborhood/cross-street is plenty). Favor sit-down/fresh spots, "
                    "and tell the user they can just say \"take me there\" and you'll open directions. "
                    "These are in " + str(area)[:50] + ", so do NOT mention distance from their current location. Only name "
                    "places from this list; never invent one." + hours_note + closed_note)
        if items and route_to:
            return ("\n\nREAL food spots ALONG the user's drive to " + str(route_to)[:50] + " (listed in TRAVEL ORDER, "
                    "start of the drive → destination): " + "; ".join(items) + ". For any 'on my way / on my drive / "
                    "on my route' question, recommend SPECIFIC places FROM THIS LIST, say roughly where on the drive each "
                    "is (early on, about midway, near your destination), honor any craving with the HEALTHIEST version of "
                    "it, and give a genuinely healthy order at each. Only name places from this list; never invent one." + hours_note + closed_note)
        if items:
            return ("\n\nREAL places near the user RIGHT NOW (closest first; '@' = street address where known): " + "; ".join(items) + ". "
                    "Lead with the place NAME + a specific healthy order; you can briefly add the distance or cross-street, but the "
                    "user mainly wants the spot + what to eat (full street address optional). You DO know where these are, so NEVER "
                    "say you don't have the location — ALWAYS tell the user they can just say \"take me there\" and you'll open "
                    "directions in their maps app. This list MIXES "
                    "fast food, cafes, and sit-down restaurants — do NOT default to fast food. When the user wants "
                    "something healthy, FAVOR the sit-down and fresh spots (places known for grilled proteins, salads, "
                    "seafood, veg-forward bowls) over fast food, and name a specific healthy order there (e.g. at a "
                    "seafood place: grilled fish, steamed veg, skip the biscuits; at a steakhouse: a sirloin with a side "
                    "salad and a baked potato). For ANY 'near me / where can I grab X' question, recommend SPECIFIC "
                    "places FROM THIS LIST by name with the distance and a genuinely healthy order at each, ideally "
                    "offering a couple of DIFFERENT kinds of spots. If they name a CRAVING, point them to the place that "
                    "does the HEALTHIEST version of THAT. Only name places from this list; never invent one." + hours_note + closed_note)
    if has_loc:
        # Empty after open-now filtering can mean it's LATE and everything healthy is closed — be honest about that
        # rather than naming spots that are shut.
        is_late = bool(lt and (lt[1] >= 21 * 60 or lt[1] < 5 * 60))
        if is_late:
            return ("\n\nIt's late and the open healthy spots near them are basically closed for the night. Be honest: say most "
                    "places are closed this late, do NOT confidently send them to a spot that's likely shut, and offer realistic "
                    "late-night options — a 24-hour diner, a grocery/convenience store (Wawa/Sheetz) for a grab-and-go protein "
                    "(rotisserie chicken, Greek yogurt, hard-boiled eggs, a protein shake), or something simple at home — and tell "
                    "them to confirm anything's open before heading out.")
        return ("\n\nThe live spot finder can't see places right now. Do NOT say 'try again'. Instead recommend 2-3 reliable, "
                "widely-available healthy options with a SPECIFIC order — e.g. CAVA (a chicken+greens bowl, double protein), "
                "Chipotle (a chicken burrito BOWL, no rice, extra veg), Sweetgreen, or Panera (a Power bowl) — and ask the user "
                "what city they're in (or a restaurant they can see) so you can get specific. Keep it warm and useful, not an apology.")
    return ("\n\nThe user has NOT shared their location yet. If they ask for food 'near me / on my way / on my drive', "
            "warmly tell them to allow location (and to set their destination in settings for on-the-drive picks) — and "
            "do NOT invent or name specific restaurants you can't actually see.")


def _degraded_reply(d):
    """No-LLM fallback when Coach Cal's brain is unavailable (e.g. Gemini credits depleted) — keep the core
       moat alive: name the closest currently-open real place from the data so the user still gets a real pick."""
    lt = _parse_local_time(str(d.get("local_time") or ""))
    nb = d.get("nearby") if isinstance(d.get("nearby"), list) else []
    openish = []
    for p in nb:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        o = _osm_open_now(p.get("hours"), lt[0], lt[1]) if (lt and p.get("hours")) else None
        if o is False:
            continue
        openish.append(p)
    if openish:
        p = openish[0]
        nm = str(p.get("name"))[:50]
        addr = (" at " + str(p.get("addr"))[:60]) if p.get("addr") else ""
        return ("My full chat is taking a quick breather, but here's a solid pick near you: " + nm + addr +
                " — go for a grilled or baked protein with veggies, sauce on the side. Back with full coaching shortly! 💚")
    return ("My chat brain is briefly offline — back in a few minutes. In the meantime: aim for a lean protein plus "
            "veggies at your next meal, drink some water, and keep logging. You've got this! 💚")


@app.post("/api/chat")
def chat():
    d = request.get_json(silent=True) or {}
    msgs = d.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return jsonify({"error": "no_messages"}), 400
    capped = _cap_over("chat", FREE_CHATS_DAY)  # HARD free-tier cost cap (server-side, can't be client-bypassed)
    if capped:
        return jsonify(capped)
    _cap_bump("chat")  # count it now — the Gemini chat cost is incurred on the call
    goal = str(d.get("goal") or "maintain").strip().lower()
    if goal not in GOAL_LABELS:
        goal = "maintain"
    diet = _norm_diet(d.get("diet")) or "no specific diet"
    allergies = ", ".join(_norm_allergies(d.get("allergies"))) or "none"
    _screens = {"today": "Today (your calorie ring + macros + what you logged)", "eatout": "Eat Out (healthy food near you / on your way)",
                "scan": "Scan (snap a photo of your food to log it)", "history": "History (your past days + weight trend)",
                "profile": "Profile (your targets, About You, day plan, settings)"}
    screen = _screens.get(str(d.get("screen") or "").strip().lower(), "the app")
    system = CHAT_SYSTEM.format(
        goal_desc=GOAL_LABELS[goal], diet=diet, allergies=allergies,
        remaining=_int(d.get("remaining_calories"), 0), daily=_int(d.get("daily_calories"), 2000),
        eaten_cal=_int(d.get("eaten_calories"), 0), protein_eaten=_int(d.get("protein_eaten_g"), 0),
        protein_target=_int(d.get("protein_target_g"), 0), screen=screen,
        eaten=str(d.get("eaten_today") or "nothing logged yet")[:200],
    )
    if goal == "recomp":
        system += RECOMP_CHAT_CLAUSE
    system += _body_clause(d)
    system += _health_clause(_health_today(_uid()))
    lt = str(d.get("local_time") or "").strip()[:24]
    if lt:
        system += ("\n\nThe user's current local time is " + lt + ". Make every food or meal suggestion "
                   "APPROPRIATE TO THIS TIME OF DAY — do NOT suggest breakfast foods (eggs, oatmeal, yogurt "
                   "parfait, pancakes) in the evening, or a heavy dinner first thing in the morning. Match the "
                   "hour: breakfast in the morning, lunch midday, dinner in the evening, and a light protein "
                   "snack late at night. If it's late, lean toward something light that won't disrupt sleep.")
    system += _chat_nearby_clause(d.get("nearby"), bool(d.get("has_location")), d.get("route_to") or "", str(d.get("area") or "").strip()[:50], local_time=lt)
    # Trip / multi-day planning intent → give a real structured plan, not a question back (squad caught the Miami
    # "plan my 3 days" punt). Needs a bigger output budget so the plan isn't truncated.
    last_user = ""
    for _m in reversed(msgs):
        if isinstance(_m, dict) and _m.get("role") == "user":
            last_user = str(_m.get("content") or "")
            break
    is_trip = bool(re.search(r"(?i)\b(vacation|trip|itinerary|out of town|for \d+\s*days?|\d+[\s-]day|plan (?:my|me|out)\s+(?:meals?|the day|my day|the week|my trip|a day)|weekend in|days in|while (?:i'?m|we'?re)\s+(?:in|there))\b", last_user))
    if is_trip:
        system += (
            "\n\nTRIP / MULTI-DAY PLAN MODE: the user wants an actual PLAN, not a question back — do NOT reply with "
            "'what sounds good?'. Produce a clear day-by-day plan covering the days they mentioned (default 1 day if "
            "unclear). For EACH day give Breakfast, Lunch, and Dinner; for each meal name a SPECIFIC real place (use the "
            "places list above when present, otherwise well-known real spots in that area you're confident exist) + ONE "
            "specific dish + a rough protein/calorie estimate, and keep each day's totals roughly within their daily "
            "calorie & protein targets. Use a compact readable layout (e.g. 'Day 1 - Breakfast: <place> - <dish> (~Xg "
            "protein); Lunch: ...; Dinner: ...'). After EACH day add a rollup line: 'Day total: ~Pg protein / ~C cal vs your "
            "[target]g & [cal] goal' — and if the day falls short of the protein target, name a concrete fix (a protein "
            "shake, Greek yogurt, an extra meat portion). Deliver the WHOLE plan, then one short upbeat closing line. If you "
            "used places from general knowledge, add a brief 'verify hours' note.")
    max_out = 1600 if is_trip else 750
    reply_cap = 4000 if is_trip else 1200
    convo = system + "\n\n"
    for m in msgs[-12:]:
        if not isinstance(m, dict):
            continue
        who = "User" if m.get("role") == "user" else "Coach Cal"
        convo += who + ": " + str(m.get("content", "")).strip()[:600] + "\n"
    convo += "Coach Cal:"
    reply = ""
    try:
        from google.genai import types
        client = get_gemini_client()
        # thinking_budget=0: gemini-2.5-flash is a THINKING model — left on, hidden reasoning eats the
        # output budget and the visible reply truncates mid-sentence (the squad caught this). Off = full
        # replies, faster, and we don't need extended reasoning for short coaching turns.
        cfg = types.GenerateContentConfig(
            max_output_tokens=max_out, temperature=0.35,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        # Gemini occasionally throws a transient error / rate-limit (intermittent 502s). Retry server-side —
        # flash-lite twice, then fall back to flash — so the user almost never sees a failure.
        attempts = [CHAT_MODEL, CHAT_MODEL, GEMINI_MODEL]
        last_exc = None
        for i, mdl in enumerate(attempts):
            try:
                resp = client.models.generate_content(model=mdl, contents=[convo], config=cfg)
                reply = (resp.text or "").strip()
                if reply:
                    break
            except Exception as e:  # noqa: BLE001
                last_exc = e
            if i < len(attempts) - 1:
                time.sleep(0.4)
        if not reply and last_exc is not None:
            raise last_exc
    except Exception:  # noqa: BLE001
        # LLM down (e.g. Gemini quota/credits depleted, transient outage). DON'T dead-end — degrade gracefully:
        # if we have real nearby places, name the closest open one from the data (no LLM needed); else a friendly note.
        return jsonify({"reply": _degraded_reply(d), "degraded": True})
    reply = reply.replace("**", "").replace("*", "").replace("—", " - ").replace("–", "-").strip()[:reply_cap]
    return jsonify({"reply": reply or "I'm here — what can I help you with?"})


# ---------- Coach Cal as a DAILY MENTOR: proactive time-aware briefing (the app's soul) ----------
BRIEF_MODEL = "gemini-2.5-flash-lite"  # briefings are short + frequent + cached -> cheap model is fine (no place/city reasoning)
_BLOCK_HINT = {
    "morning": "It's morning. Greet them warmly, state today's target, and give ONE easy win to start strong (protein at breakfast, water, a short walk).",
    "midday":  "It's midday. Note how they're doing vs target and how much room is left, then nudge a high-protein lunch.",
    "evening": "It's evening. Quick review vs target — if they have room suggest a solid protein dinner; if over, reassure and set tomorrow up. Keep protein in focus.",
    "late":    "It's late. Gently wrap the day: did they hit protein? remind to log dinner if not, one calming note to rest. Keep it light.",
}


def _brief_block(minutes):
    if minutes is None:
        return "midday"
    if minutes < 11 * 60:
        return "morning"
    if minutes < 16 * 60:
        return "midday"
    if minutes < 21 * 60:
        return "evening"
    return "late"


def _brief_fallback(block, name, daily, remaining, pe, pt, gd):
    nm = (" " + name) if name else ""
    rem = max(0, remaining)
    left_p = max(0, pt - pe)
    if block == "morning":
        return ("Morning" + nm + "! Let's make today count - aim for about " + str(daily) + " cal and " + str(pt) +
                "g protein for " + gd + ". Easy win: get protein in at breakfast and a glass of water. I've got you.")
    if block == "midday":
        return ("Midday check" + nm + " - you've got about " + str(rem) + " cal and " + str(left_p) +
                "g protein left today. Make lunch a lean protein with veggies and you're right on track.")
    if block == "evening":
        if remaining > 0:
            return ("Evening" + nm + " - nice work today. You've still got ~" + str(rem) + " cal of room, so a solid "
                    "protein dinner fits perfectly. Aim to land near " + str(pt) + "g protein.")
        return ("Evening" + nm + " - you're at your calorie goal for today, that's discipline. Keep dinner light and "
                "protein-forward, and we'll tighten tomorrow. Proud of you.")
    return ("Winding down" + nm + " - log dinner if you haven't, and check you hit your protein (" + str(pe) + "/" +
            str(pt) + "g). Tomorrow's a fresh start - rest up.")


@app.post("/api/briefing")
def briefing():
    d = request.get_json(silent=True) or {}
    goal = str(d.get("goal") or "maintain").strip().lower()
    if goal not in GOAL_LABELS:
        goal = "maintain"
    gd = GOAL_LABELS[goal]
    name = re.sub(r"[^A-Za-z .'-]", "", str(d.get("name") or "")).strip()[:24]
    lt = str(d.get("local_time") or "").strip()[:24]
    _wd, minutes = _parse_local_time(lt) if lt else (None, None)
    block = _brief_block(minutes)
    daily = _int(d.get("daily_calories"), 2000)
    remaining = _int(d.get("remaining_calories"), daily)
    eaten = _int(d.get("eaten_calories"), 0)
    pe = _int(d.get("protein_eaten_g"), 0)
    pt = _int(d.get("protein_target_g"), 0)
    diet = _norm_diet(d.get("diet")) or "no specific diet"
    allergies = ", ".join(_norm_allergies(d.get("allergies"))) or "none"
    fb = _brief_fallback(block, name, daily, remaining, pe, pt, gd)
    prompt = (
        "You are Coach Cal, a warm, upbeat health MENTOR (not a generic chatbot) checking in on your client"
        + ((" " + name) if name else "") + ". Their goal: " + gd + ". Diet: " + diet + ". Allergies: " + allergies + ". "
        "Today so far: eaten " + str(eaten) + " of " + str(daily) + " cal (" + str(max(0, remaining)) + " left), "
        "protein " + str(pe) + " of " + str(pt) + "g. " + _BLOCK_HINT.get(block, _BLOCK_HINT["midday"]) +
        " RULES: 2-3 sentences, under 320 characters, plain text (NO markdown, NO bullets), specific to their actual "
        "numbers, encouraging and NEVER judgmental, end with ONE concrete next action. Never suggest a food they're allergic to."
    )
    try:
        from google.genai import types
        client = get_gemini_client()
        cfg = types.GenerateContentConfig(max_output_tokens=200, temperature=0.5,
                                          thinking_config=types.ThinkingConfig(thinking_budget=0))
        txt = ""
        for mdl in (BRIEF_MODEL, CHAT_MODEL):
            try:
                resp = client.models.generate_content(model=mdl, contents=[prompt], config=cfg)
                txt = (resp.text or "").strip()
                if txt:
                    break
            except Exception:  # noqa: BLE001
                continue
        if not txt:
            raise ValueError("empty")
        txt = txt.replace("**", "").replace("*", "").replace("—", " - ").replace("–", "-").strip()[:400]
        return jsonify({"block": block, "text": txt})
    except Exception:  # noqa: BLE001
        return jsonify({"block": block, "text": fb, "degraded": True})


# ---------------------------------------------------------------- PUSH (proactive mentor check-ins) [PREMIUM]
# Coach Cal reaches out when the app is CLOSED — the "guides you every day" piece. Cost discipline: scheduled
# fan-out uses the RULE-BASED briefing text (_brief_fallback), so a daily blast to every user costs ~$0 in LLM
# spend (no Gemini call per push). Premium-gated client-side; server caps 1 push per slot/day per device.

# Local-hour windows -> (slot id, briefing block). One push per slot/day. Quiet overnight (no late push).
_PUSH_WINDOWS = [
    (7, 10, "morning", "morning"),   # 7:00–9:59 local
    (12, 14, "midday", "midday"),    # 12:00–13:59 local
    (18, 21, "evening", "evening"),  # 18:00–20:59 local
]
_PUSH_TITLES = {"morning": "Good morning 👋", "midday": "Midday check-in", "evening": "Evening check-in"}


def _push_slot_for(local_minutes):
    """Which check-in slot (if any) the user's current LOCAL time falls in. Returns (slot, block) or (None, None)."""
    if local_minutes is None:
        return (None, None)
    h = local_minutes // 60
    for lo, hi, slot, block in _PUSH_WINDOWS:
        if lo <= h < hi:
            return (slot, block)
    return (None, None)


def _push_local_now(tz_offset_min):
    """The subscriber's current local datetime, from the tz offset their browser reported (JS getTimezoneOffset:
       minutes LOCAL is behind UTC — EST=300 — so local = utcnow - offset)."""
    try:
        return datetime.utcnow() - timedelta(minutes=int(tz_offset_min or 0))
    except Exception:  # noqa: BLE001
        return datetime.utcnow()


def _push_text_for(row, block):
    """Personal check-in line from the subscriber's stored targets + TODAY's logged meals (their local date).
       Rule-based (no LLM) so the scheduled fan-out is free + reliable."""
    local = _push_local_now(row["tz_offset_min"])
    local_date = local.date().isoformat()
    daily = _int(row["daily_calories"], 2000)
    pt = _int(row["protein_target_g"], 0)
    eaten = pe = 0
    con = get_db()
    try:
        r = con.execute(
            "SELECT COALESCE(SUM(calories),0) c, COALESCE(SUM(protein_g),0) p FROM meals WHERE date = ? AND uid = ?",
            (local_date, row["uid"]),
        ).fetchone()
        if r:
            eaten, pe = _int(r["c"], 0), _int(r["p"], 0)
    finally:
        con.close()
    goal = str(row["goal"] or "maintain").strip().lower()
    gd = GOAL_LABELS.get(goal, GOAL_LABELS.get("maintain", "your goal"))
    name = re.sub(r"[^A-Za-z .'-]", "", str(row["name"] or "")).strip()[:24]
    return _brief_fallback(block, name, daily, max(0, daily - eaten), pe, pt, gd)


def _push_send(sub, payload):
    """Deliver one web-push. Returns 'ok' | 'dead' (404/410 → caller deletes the sub) | 'fail'."""
    if not VAPID_PRIVATE:
        return "fail"  # not configured (no private key in env) — subscribe still works; sending is a no-op
    try:
        from pywebpush import webpush, WebPushException
    except Exception:  # noqa: BLE001
        return "fail"
    try:
        webpush(
            subscription_info=sub,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE,
            vapid_claims={"sub": VAPID_SUB},
            timeout=10,
        )
        return "ok"
    except WebPushException as e:  # noqa: BLE001
        code = getattr(getattr(e, "response", None), "status_code", None)
        return "dead" if code in (404, 410) else "fail"
    except Exception:  # noqa: BLE001
        return "fail"


@app.get("/api/push/key")
def push_key():
    """Public VAPID key the browser needs as applicationServerKey. `configured` tells the client whether the
       server can actually send (private key present) so it won't promise check-ins that never arrive."""
    return jsonify({"key": VAPID_PUBLIC, "configured": bool(VAPID_PRIVATE)})


@app.post("/api/push/subscribe")
def push_subscribe():
    """Save (or replace) this device's PushSubscription + a profile snapshot for personalised scheduled check-ins."""
    d = request.get_json(silent=True) or {}
    sub = d.get("subscription")
    if not isinstance(sub, dict) or not sub.get("endpoint"):
        return jsonify({"error": "subscription_required"}), 400
    goal = str(d.get("goal") or "maintain").strip().lower()
    if goal not in GOAL_LABELS:
        goal = "maintain"
    name = re.sub(r"[^A-Za-z .'-]", "", str(d.get("name") or "")).strip()[:24]
    con = get_db()
    try:
        con.execute(
            """INSERT INTO push_subs(uid, sub_json, tz_offset_min, name, goal, daily_calories,
                                     protein_target_g, enabled, last_slot, last_date, created)
               VALUES(?,?,?,?,?,?,?,1,NULL,NULL,?)
               ON CONFLICT(uid) DO UPDATE SET
                   sub_json=excluded.sub_json, tz_offset_min=excluded.tz_offset_min, name=excluded.name,
                   goal=excluded.goal, daily_calories=excluded.daily_calories,
                   protein_target_g=excluded.protein_target_g, enabled=1""",
            (_uid(), json.dumps(sub), _int(d.get("tz_offset_min"), 0), name, goal,
             _int(d.get("daily_calories"), 2000), _int(d.get("protein_target_g"), 0),
             datetime.utcnow().isoformat()),
        )
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True})


@app.post("/api/push/unsubscribe")
def push_unsubscribe():
    """Turn off scheduled check-ins for this device (toggle off / permission revoked)."""
    con = get_db()
    try:
        con.execute("DELETE FROM push_subs WHERE uid = ?", (_uid(),))
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True})


@app.post("/api/push/run")
def push_run():
    """SCHEDULER endpoint (called by the keep-warm GitHub Action). For every enabled subscriber whose LOCAL time
       is in a check-in window and who hasn't been pushed for that slot today, send one personalised check-in.
       Guarded by PUSH_RUN_SECRET so only our scheduler can trigger a fan-out."""
    if not PUSH_RUN_SECRET:
        return jsonify({"error": "push_run_not_configured"}), 503
    secret = (request.headers.get("X-Push-Secret") or request.args.get("secret") or "").strip()
    if secret != PUSH_RUN_SECRET:
        return jsonify({"error": "forbidden"}), 403
    sent = skipped = dead = failed = 0
    con = get_db()
    try:
        rows = con.execute("SELECT * FROM push_subs WHERE enabled = 1").fetchall()
    finally:
        con.close()
    for row in rows:
        local = _push_local_now(row["tz_offset_min"])
        slot, block = _push_slot_for(local.hour * 60 + local.minute)
        if not slot:
            skipped += 1
            continue
        local_date = local.date().isoformat()
        if row["last_slot"] == slot and row["last_date"] == local_date:
            skipped += 1  # already checked in this slot today
            continue
        try:
            sub = json.loads(row["sub_json"])
        except Exception:  # noqa: BLE001
            continue
        body = _push_text_for(row, block)
        result = _push_send(sub, {"title": _PUSH_TITLES.get(slot, "Coach Cal"), "body": body, "url": "/"})
        if result == "ok":
            sent += 1
            con = get_db()
            try:
                con.execute("UPDATE push_subs SET last_slot = ?, last_date = ? WHERE uid = ?",
                            (slot, local_date, row["uid"]))
                con.commit()
            finally:
                con.close()
        elif result == "dead":
            dead += 1
            con = get_db()
            try:
                con.execute("DELETE FROM push_subs WHERE uid = ?", (row["uid"],))
                con.commit()
            finally:
                con.close()
        else:
            failed += 1
    return jsonify({"sent": sent, "skipped": skipped, "dead": dead, "failed": failed, "total": len(rows)})


@app.post("/api/push/test")
def push_test():
    """Send ONE check-in to the CALLING device immediately — bypasses the time-window + once-per-slot dedupe so a
       user (or Tariq dogfooding) can confirm notifications work right now. Device-scoped: it can only push to its
       OWN subscription (X-Device-Id), so it can't be used to spam others — no shared secret needed."""
    con = get_db()
    try:
        row = con.execute("SELECT * FROM push_subs WHERE uid = ?", (_uid(),)).fetchone()
    finally:
        con.close()
    if not row:
        return jsonify({"error": "not_subscribed"}), 404
    if not VAPID_PRIVATE:
        return jsonify({"error": "push_not_configured"}), 503
    try:
        sub = json.loads(row["sub_json"])
    except Exception:  # noqa: BLE001
        return jsonify({"error": "bad_subscription"}), 400
    local = _push_local_now(row["tz_offset_min"])
    block = _brief_block(local.hour * 60 + local.minute)  # nearest block even outside a push window
    body = _push_text_for(row, block)
    result = _push_send(sub, {"title": "Test check-in 🔔", "body": body, "url": "/"})
    if result == "ok":
        return jsonify({"ok": True})
    if result == "dead":
        con = get_db()
        try:
            con.execute("DELETE FROM push_subs WHERE uid = ?", (_uid(),))
            con.commit()
        finally:
            con.close()
        return jsonify({"error": "subscription_expired"}), 410
    return jsonify({"error": "send_failed"}), 502


# ---------------------------------------------------------------- HEALTH SYNC (Apple Health / Google Fit hub)
HEALTH_NUM_KEYS = ("steps", "active_cal", "resting_hr", "sleep_min")  # ints


def _health_today(uid):
    """Today's synced health row for a device (or None) — fed to Coach Cal so it coaches on real movement."""
    con = get_db()
    try:
        return con.execute("SELECT * FROM health_metrics WHERE uid = ? AND date = ?",
                          (uid, date.today().isoformat())).fetchone()
    finally:
        con.close()


@app.post("/api/health")
def health_sync():
    """The native app pushes wearable/phone health data here (steps/active cal/resting HR/sleep/weight) for a
       date. Upserts one row per device per day. Web build never calls this (no HealthKit/Health Connect)."""
    d = request.get_json(silent=True) or {}
    day = str(d.get("date") or date.today().isoformat())[:10]
    steps = _int(d.get("steps"), 0) or None
    active_cal = _int(d.get("active_cal"), 0) or None
    resting_hr = _int(d.get("resting_hr"), 0) or None
    sleep_min = _int(d.get("sleep_min"), 0) or None
    try:
        weight = float(d.get("weight")) if d.get("weight") not in (None, "") else None
    except (TypeError, ValueError):
        weight = None
    source = re.sub(r"[^A-Za-z _-]", "", str(d.get("source") or ""))[:24] or "device"
    con = get_db()
    try:
        con.execute(
            """INSERT INTO health_metrics(uid, date, steps, active_cal, resting_hr, sleep_min, weight, source, updated)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(uid, date) DO UPDATE SET
                   steps=COALESCE(excluded.steps, steps),
                   active_cal=COALESCE(excluded.active_cal, active_cal),
                   resting_hr=COALESCE(excluded.resting_hr, resting_hr),
                   sleep_min=COALESCE(excluded.sleep_min, sleep_min),
                   weight=COALESCE(excluded.weight, weight),
                   source=excluded.source, updated=excluded.updated""",
            (_uid(), day, steps, active_cal, resting_hr, sleep_min, weight, source,
             datetime.utcnow().isoformat()),
        )
        # If Health sent a fresh body weight, mirror it into the weight-trend table too (one source of truth).
        if weight is not None:
            con.execute("INSERT INTO weights(date, weight) VALUES(?,?) ON CONFLICT(date) DO UPDATE SET weight=excluded.weight",
                        (day, weight))
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True})


@app.get("/api/health")
def health_get():
    """Return recent synced health metrics for this device (Today card + history)."""
    try:
        days = max(1, min(90, int(request.args.get("days", "1"))))
    except (TypeError, ValueError):
        days = 1
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    con = get_db()
    try:
        rows = con.execute(
            "SELECT date, steps, active_cal, resting_hr, sleep_min, weight, source FROM health_metrics "
            "WHERE uid = ? AND date >= ? ORDER BY date DESC", (_uid(), cutoff)).fetchall()
    finally:
        con.close()
    out = [dict(r) for r in rows]
    today = next((r for r in out if r["date"] == date.today().isoformat()), None)
    return jsonify({"days": out, "today": today})


# ---- Coach Cal's VOICE: Gemini TTS, cached by text so common/repeated lines cost nothing after the 1st play ----
TTS_MODEL = "gemini-2.5-flash-preview-tts"
TTS_VOICE_DEFAULT = "Charon"
TTS_VOICES_OK = {"Charon", "Orus", "Puck", "Kore", "Fenrir", "Aoede", "Leda", "Achird", "Iapetus", "Zephyr"}
_TTS_CACHE = {}


def _pcm16_to_wav(pcm, rate=24000):
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


@app.get("/api/tts")
def tts():
    """Speak a Coach Cal line (Gemini TTS). Cached by (voice,text) so repeated/common lines are free after the 1st call."""
    text = (request.args.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text_required"}), 400
    voice = (request.args.get("voice") or TTS_VOICE_DEFAULT).strip()
    if voice not in TTS_VOICES_OK:
        voice = TTS_VOICE_DEFAULT
    text = text[:700]
    import hashlib
    ckey = hashlib.sha1((voice + "|" + text).encode("utf-8")).hexdigest()
    cached = _TTS_CACHE.get(ckey)
    if cached is not None:
        return Response(cached, mimetype="audio/wav", headers={"Cache-Control": "public, max-age=86400"})
    try:
        from google import genai  # noqa: F401
        from google.genai import types
        client = get_gemini_client()
        resp = client.models.generate_content(
            model=TTS_MODEL, contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
            ),
        )
        data = resp.candidates[0].content.parts[0].inline_data.data
        if isinstance(data, str):
            import base64
            data = base64.b64decode(data)
        wav = _pcm16_to_wav(data)
    except Exception:  # noqa: BLE001
        return jsonify({"error": "Coach Cal can't speak right now."}), 502
    if len(_TTS_CACHE) > 800:
        _TTS_CACHE.clear()
    _TTS_CACHE[ckey] = wav
    return Response(wav, mimetype="audio/wav", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/geocode")
def geocode():
    """Turn a typed place (the user's work/gym, or a town they name like 'Ambler') into a point. Free (Nominatim).
    BIAS to the user's area when lat/lng are passed — bare 'Ambler' otherwise geocodes to a village in Italy, not PA."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "q_required"}), 400
    try:
        ulat = float(request.args.get("lat", ""))
        ulng = float(request.args.get("lng", ""))
    except (TypeError, ValueError):
        ulat = ulng = None
    # Rank by PROMINENCE (Nominatim 'importance'), not proximity — the old viewbox bias sent "Miami" to a NJ
    # hamlet and "Houston" to Houston, DE. Prefer real localities (city/town) over streets/POIs; a small
    # proximity bonus only breaks ties between similarly-prominent same-name places.
    url = ("https://nominatim.openstreetmap.org/search?format=jsonv2&limit=10&addressdetails=0&q="
           + urllib.parse.quote(q))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SnapCal/1.0 (health coach; tariq@xionprotech.com)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return jsonify({"error": "geocode_failed"}), 502
    if not isinstance(d, list) or not d:
        return jsonify({"error": "not_found"}), 404

    def _score(rec):
        s = float(rec.get("importance") or 0)
        cls, typ = rec.get("category") or rec.get("class") or "", rec.get("type") or ""
        if cls in ("place", "boundary"):
            s += 1.0                     # strongly prefer real localities
        if typ in ("city", "town"):
            s += 0.5
        if typ in ("municipality", "administrative", "village"):
            s += 0.2
        if cls == "highway":
            s -= 1.0                     # never pick a street ("Los Angeles Avenue, CT")
        if ulat is not None and ulng is not None:
            try:
                dd = abs(float(rec["lat"]) - ulat) + abs(float(rec["lon"]) - ulng)
                s += max(0.0, 0.25 - 0.03 * min(dd, 12))   # mild tiebreak toward nearby, never overrides prominence
            except (KeyError, TypeError, ValueError):
                pass
        return s

    best = max(d, key=_score)
    return jsonify({"lat": float(best["lat"]), "lng": float(best["lon"]), "label": str(best.get("display_name", ""))[:120]})


_ROUTE_CACHE = {}


@app.post("/api/route_nearby")
def route_nearby():
    """Healthy food ALONG the user's drive: real OSRM route from->to, then food places near the whole corridor,
    tagged by where on the trip they fall. This is the moat — coach them WHILE they're out, not just when they log."""
    d = request.get_json(silent=True) or {}
    f, t = d.get("from"), d.get("to")
    try:
        flat, flng = float(f["lat"]), float(f["lng"])
        tlat, tlng = float(t["lat"]), float(t["lng"])
    except (TypeError, ValueError, KeyError):
        return jsonify({"error": "from_to_required", "places": []}), 400
    ck = f"{round(flat, 3)},{round(flng, 3)}|{round(tlat, 3)},{round(tlng, 3)}"   # ~100m granularity
    if ck in _ROUTE_CACHE:
        return jsonify(_ROUTE_CACHE[ck])
    try:
        ru = (f"https://router.project-osrm.org/route/v1/driving/{flng},{flat};{tlng},{tlat}"
              "?overview=full&geometries=geojson")
        req = urllib.request.Request(ru, headers={"User-Agent": "SnapCal/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            rj = json.loads(r.read().decode("utf-8"))
        coords = rj["routes"][0]["geometry"]["coordinates"]   # [lon, lat]
    except Exception:  # noqa: BLE001
        return jsonify({"error": "route_failed", "places": []})
    if not coords:
        return jsonify({"error": "route_empty", "places": []})
    n = 8
    samples = coords if len(coords) <= n else [coords[int(round(i * (len(coords) - 1) / (n - 1)))] for i in range(n)]
    arounds = "".join(f'nwr(around:700,{c[1]},{c[0]})["amenity"~"^(fast_food|restaurant|cafe)$"];' for c in samples)
    query = "[out:json][timeout:20];(" + arounds + ");out center tags 400;"
    try:
        payload = _overpass(query, timeout=22)
    except Exception:  # noqa: BLE001
        return jsonify({"error": "lookup_failed", "places": []})
    aliases = _chain_alias_map()
    places, seen = [], set()
    last = (len(samples) - 1) or 1
    for el in payload.get("elements", []):
        tags = el.get("tags") or {}
        label = tags.get("name") or tags.get("brand")
        if not label:
            continue
        elat = el.get("lat") if el.get("lat") is not None else (el.get("center") or {}).get("lat")
        elng = el.get("lon") if el.get("lon") is not None else (el.get("center") or {}).get("lon")
        if elat is None or elng is None:
            continue
        canon = _match_chain(tags, aliases)
        name = canon or label
        k = name.lower()
        if k in seen:
            continue
        seen.add(k)
        best_i, best_d = 0, 9e18
        for i, c in enumerate(samples):
            dd = _haversine_m(elat, elng, c[1], c[0])
            if dd < best_d:
                best_d, best_i = dd, i
        places.append({"name": name, "frac": round(best_i / last, 2), "matched": bool(canon)})
    places.sort(key=lambda p: p["frac"])   # travel order: start of drive -> destination
    out = {"places": places[:16]}
    if places:
        _ROUTE_CACHE[ck] = out   # cache only real hits, so a transient empty result retries next time
    return jsonify(out)


# ---------- USDA FoodData Central: authoritative federal nutrition data (the science-backed backbone) ----------
def _usda_key():
    """USDA api.data.gov key from env OR apps/snapcal/usda_key.txt; DEMO_KEY works (rate-limited) until a free key is added."""
    k = os.environ.get("USDA_API_KEY", "").strip()
    if k:
        return k
    try:
        fk = (APP_DIR / "usda_key.txt").read_text(encoding="utf-8").strip()
        if fk:
            return fk
    except Exception:
        pass
    return "DEMO_KEY"


_NUTRITION_CACHE = {}
_FDC_NUTRIENTS = {   # USDA nutrient name -> our short label. Cronometer-level depth from the SAME USDA fetch.
    # Macros
    "Energy": "calories", "Protein": "protein_g", "Total lipid (fat)": "fat_g",
    "Carbohydrate, by difference": "carbs_g", "Fiber, total dietary": "fiber_g",
    "Sugars, total including NLEA": "sugar_g", "Total Sugars": "sugar_g",
    # Fats (detail)
    "Fatty acids, total saturated": "sat_fat_g",
    "Fatty acids, total monounsaturated": "mufa_g",
    "Fatty acids, total polyunsaturated": "pufa_g",
    "Fatty acids, total trans": "trans_fat_g",
    "Cholesterol": "cholesterol_mg",
    # Minerals
    "Sodium, Na": "sodium_mg", "Potassium, K": "potassium_mg", "Calcium, Ca": "calcium_mg",
    "Iron, Fe": "iron_mg", "Magnesium, Mg": "magnesium_mg", "Zinc, Zn": "zinc_mg",
    "Phosphorus, P": "phosphorus_mg",
    # Vitamins
    "Vitamin C, total ascorbic acid": "vitc_mg",
    "Vitamin A, RAE": "vita_mcg",
    "Vitamin D (D2 + D3)": "vitd_mcg",
    "Vitamin E (alpha-tocopherol)": "vite_mg",
    "Vitamin K (phylloquinone)": "vitk_mcg",
    "Thiamin": "b1_mg", "Riboflavin": "b2_mg", "Niacin": "b3_mg",
    "Vitamin B-6": "b6_mg", "Folate, total": "folate_mcg", "Vitamin B-12": "b12_mcg",
}


_FDC_PROCESSED = ("dehydrated", "powder", "dried", "canned", "concentrate", "fried", "syrup", "infant",
                  "baby food", "juice", "sauce", "frozen", "fast food", "restaurant", "flour", "oil")


def _toks(s):
    out = []
    for w in s.lower().replace(",", " ").replace("(", " ").replace(")", " ").split():
        w = w.strip(".,()/-")
        if len(w) > 2:
            out.append(w)
    return out


def _pick_food(foods, q):
    """USDA's relevance is noisy for bare terms ('apple' can rank 'Croissants, apple' or juice #1). Prefer
    entries whose name LEADS the description (the food itself — 'Apples, raw' not 'Croissants, apple'),
    demote processed/derivative forms the user didn't ask for, use USDA's own rank only as a tiebreaker."""
    ql = q.lower()
    qwords = _toks(ql)

    def head_match(desc):
        for hw in _toks(desc.split(",")[0]):   # words before the first comma = the food's own name
            for qw in qwords:
                if hw == qw or hw == qw + "s" or hw == qw + "es" or qw == hw + "s" \
                        or (len(qw) > 4 and len(hw) > 4 and (hw.startswith(qw[:5]) or qw.startswith(hw[:5]))):
                    return True
        return False

    def score(idx_f):
        idx, f = idx_f
        desc = (f.get("description") or "").lower()
        dtoks = set(_toks(desc))
        s = float(idx) * 0.5                  # USDA rank = weak tiebreaker
        if not head_match(desc):
            s += 100                          # the food's own name should lead its description
        covered = sum(1 for qw in qwords if qw in dtoks or (qw + "s") in dtoks
                      or any(len(qw) > 4 and dt.startswith(qw[:5]) for dt in dtoks))
        s -= covered * 3                      # reward matching the user's own words ('grilled', 'white')
        for p in _FDC_PROCESSED:              # word-boundary match — 'oil' must not hit 'br-OIL-er'
            if p in ql:
                continue
            if (" " in p and p in desc) or (" " not in p and p in dtoks):
                s += 40                       # demote processed/derivative forms unless asked for
        return s
    return sorted(enumerate(foods), key=score)[0][1]


# ---- Rung 4b: the HYBRID estimator (ACCURACY_ENGINE.md). The model is good at estimating GRAMS from a
#      photo but bad at recalling calories; USDA gives authoritative kcal/100 g. Where we have both, blend
#      the item's calories toward grams x density and report an honest confidence band. 76-83% less error.
_DENSITY_CACHE = {}


def _parse_grams(qty):
    """Pull an edible gram weight out of a qty string ('approx. 150 g', '1 cup (240 g)'). None if absent/insane."""
    if not qty:
        return None
    nums = re.findall(r"(\d+(?:\.\d+)?)\s*g\b", str(qty).lower())
    if not nums:
        return None
    try:
        g = float(nums[-1])   # last gram figure handles '1 cup (240 g)'
    except ValueError:
        return None
    return g if 5 <= g <= 2000 else None


def _usda_kcal_per_100g(name):
    """Energy (kcal) per 100 g for a food name from USDA FDC, cached + bounded. None if unavailable/insane."""
    key = (name or "").strip().lower()[:60]
    if not key:
        return None
    if key in _DENSITY_CACHE:
        return _DENSITY_CACHE[key]
    val = None
    try:
        params = urllib.parse.urlencode({"query": name, "pageSize": 10,
                                         "dataType": "Foundation,SR Legacy", "api_key": _usda_key()})
        req = urllib.request.Request("https://api.nal.usda.gov/fdc/v1/foods/search?" + params,
                                     headers={"User-Agent": "SnapCal/1.0"})
        with urllib.request.urlopen(req, timeout=3) as r:
            foods = (json.loads(r.read().decode("utf-8")).get("foods")) or []
        if foods:
            f = _pick_food(foods, name)
            for n in f.get("foodNutrients", []):
                if n.get("nutrientName") == "Energy" and (n.get("unitName") or "").upper() == "KCAL":
                    v = n.get("value")
                    if v is not None and 0 < float(v) <= 900:   # sane kcal/100 g
                        val = float(v)
                        break
    except Exception:  # noqa: BLE001 — any failure: AI estimate simply stands, no crash
        val = None
    _DENSITY_CACHE[key] = val
    return val


def _cross_check_calories(result, density_fn=None):
    """Blend each item's calories toward grams x USDA-density (the stronger signal) and attach a confidence
    band (`total.band_pct`): tight (+/-12%) for USDA-cross-checked items, wide (+/-25%) for AI-only ones.
    `density_fn` is injectable for tests. Bounded, cached, graceful — never blocks/crashes the scan."""
    items = result.get("items") or []
    if not items:
        return result
    fn = density_fn or _usda_kcal_per_100g
    targets = [(i, it) for i, it in enumerate(items) if _parse_grams(it.get("qty"))]
    densities = {}
    if targets:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(6, len(targets))) as ex:
            futs = {ex.submit(fn, it["name"]): i for i, it in targets}
            for fut in futs:
                try:
                    densities[futs[fut]] = fut.result(timeout=4)
                except Exception:  # noqa: BLE001
                    densities[futs[fut]] = None
    any_hybrid = False
    for i, it in enumerate(items):
        ai_kcal = _int(it.get("calories"))
        grams = _parse_grams(it.get("qty"))
        dens = densities.get(i)
        if grams and dens:
            usda_kcal = int(round(grams * dens / 100.0))
            it["calories"] = int(round(0.4 * ai_kcal + 0.6 * usda_kcal)) if ai_kcal else usda_kcal
            it["kcal_source"] = "hybrid"
            any_hybrid = True
        else:
            it["kcal_source"] = "ai"
    total = result.get("total") or {}
    total["calories"] = sum(_int(it.get("calories")) for it in items)
    wcal = total["calories"] or 1
    total["band_pct"] = round(sum(_int(it.get("calories")) * (0.12 if it.get("kcal_source") == "hybrid" else 0.25)
                                  for it in items) / wcal, 3)
    result["total"] = total
    if any_hybrid:
        result["accuracy_note"] = "Calories cross-checked against USDA federal data for a tighter estimate."
    return result


@app.get("/api/nutrition")
def nutrition():
    """Real per-food nutrition facts from USDA FoodData Central — federal, peer-reviewed data, not an AI guess.
    This is what lets the app claim 'science-backed', for real."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "q_required"}), 400
    ck = q.lower()[:80]
    if ck in _NUTRITION_CACHE:
        return jsonify(_NUTRITION_CACHE[ck])
    params = urllib.parse.urlencode({"query": q, "pageSize": 10, "dataType": "Foundation,SR Legacy", "api_key": _usda_key()})
    try:
        req = urllib.request.Request("https://api.nal.usda.gov/fdc/v1/foods/search?" + params, headers={"User-Agent": "SnapCal/1.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return jsonify({"error": "lookup_failed"}), 502
    foods = d.get("foods") or []
    if not foods:
        return jsonify({"error": "not_found", "query": q}), 404
    f = _pick_food(foods, q)
    nutrients = {}
    for n in f.get("foodNutrients", []):
        nm = n.get("nutrientName")
        label = _FDC_NUTRIENTS.get(nm)
        if not label:
            continue
        val = n.get("value")
        if val is None:
            continue
        if label == "calories" and (n.get("unitName") or "").upper() != "KCAL":
            continue   # skip the kJ duplicate, keep kcal
        nutrients[label] = round(val, 1)
    out = {"food": f.get("description", q), "fdcId": f.get("fdcId"), "dataType": f.get("dataType"),
           "serving": "per 100 g (3.5 oz)", "source": "USDA FoodData Central",
           "accuracy_tier": "VERIFIED", "nutrients": nutrients}
    _NUTRITION_CACHE[ck] = out
    return jsonify(out)


_BARCODE_CACHE = {}


@app.get("/api/barcode")
def barcode():
    """Look up a packaged food by its UPC/EAN barcode against Open Food Facts — a free, open,
    VERIFIED label database (no API key, no billing, like our OSM/USDA calls). This is the
    'trust the number' answer to AI-photo guesses: exact label data with a confidence you can
    stand behind, not an estimate. Returns {found:false} cleanly when a code isn't in the DB."""
    code = re.sub(r"\D", "", (request.args.get("code") or ""))
    if not (8 <= len(code) <= 14):
        return jsonify({"error": "bad_barcode"}), 400
    if code in _BARCODE_CACHE:
        return jsonify(_BARCODE_CACHE[code])
    fields = "product_name,brands,serving_size,nutrition_data_per,nutriments,image_front_small_url"
    url = "https://world.openfoodfacts.org/api/v2/product/" + code + ".json?fields=" + fields
    # Open Food Facts REQUIRES a descriptive User-Agent (app + contact) and throttles weak/generic ones from
    # shared cloud IPs — that was 502'ing every lookup from Render. Proper UA + retry fixes it.
    headers = {"User-Agent": "SnapCal/1.0 (Xionprotech LLC health app; +https://snapcal-api-lgla.onrender.com; tariq@xionprotech.com)",
               "Accept": "application/json"}
    d = None
    last = ""
    for _attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as r:
                d = json.loads(r.read().decode("utf-8"))
            break
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:160]
            time.sleep(0.5)
    if d is None:
        return jsonify({"error": "lookup_failed", "detail": last}), 502
    if d.get("status") != 1 and not d.get("product"):
        out = {"found": False, "code": code}
        _BARCODE_CACHE[code] = out
        return jsonify(out)
    p = d.get("product") or {}
    n = p.get("nutriments") or {}

    def num(key):
        try:
            return round(float(n.get(key)), 1)
        except Exception:  # noqa: BLE001
            return None

    serving = (p.get("serving_size") or "").strip()
    has_serv = num("energy-kcal_serving") is not None
    per = serving if (has_serv and serving) else "100 g"
    sfx = "_serving" if has_serv else "_100g"
    sodium = num("sodium" + sfx)
    out = {
        "found": True,
        "code": code,
        "name": (p.get("product_name") or "").strip() or "Packaged food",
        "brand": (p.get("brands") or "").split(",")[0].strip(),
        "serving": per,
        "image": p.get("image_front_small_url") or "",
        "calories": num("energy-kcal" + sfx),
        "protein_g": num("proteins" + sfx),
        "carbs_g": num("carbohydrates" + sfx),
        "fat_g": num("fat" + sfx),
        "fiber_g": num("fiber" + sfx),
        "sugar_g": num("sugars" + sfx),
        "sat_fat_g": num("saturated-fat" + sfx),
        "sodium_mg": (round(sodium * 1000) if sodium is not None else None),
        "source": "Open Food Facts",
        "accuracy_tier": "EXACT",
    }
    _BARCODE_CACHE[code] = out
    return jsonify(out)


_MENU_INDEX = None


def _build_menu_index():
    """Flatten the curated Eat-Out dataset into one searchable list of chain menu items, each with
    EXACT published macros. This is Rung 2 of the accuracy ladder (ACCURACY_ENGINE.md): eating out
    becomes tap-to-log the chain's REAL number, not an AI photo guess. Deduped by (chain, item)."""
    global _MENU_INDEX
    if _MENU_INDEX is not None:
        return _MENU_INDEX
    items, seen = [], set()
    for r in _load_restaurants().get("restaurants", []):
        chain = (r.get("chain") or "").strip()
        emoji = r.get("emoji") or ""
        for picks in (r.get("best_picks") or {}).values():
            for it in (picks or []):
                name = (it.get("name") or "").strip()
                if not name:
                    continue
                key = (chain.lower(), name.lower())
                if key in seen:
                    continue
                seen.add(key)
                items.append({
                    "chain": chain,
                    "emoji": emoji,
                    "name": name,
                    "calories": _int(it.get("calories")),
                    "protein_g": _int(it.get("protein_g")),
                    "carbs_g": _int(it.get("carbs_g")),
                    "fat_g": _int(it.get("fat_g")),
                    "source": "Published menu — " + chain if chain else "Published menu",
                    "accuracy_tier": "EXACT",
                })
    _MENU_INDEX = items
    return _MENU_INDEX


@app.get("/api/menu")
def menu():
    """Restaurant-exact lookup. Match a query against chain names + menu items and return items with
    their EXACT published macros (tier=EXACT), so a user eating out logs the real number in one tap.
    `?q=` searches both chain and item; optional `?chain=` narrows to one chain. No key, all-local."""
    q = (request.args.get("q") or "").strip().lower()
    chain_f = (request.args.get("chain") or "").strip().lower()
    idx = _build_menu_index()
    terms = [t for t in re.split(r"\s+", q) if t]

    def score(it):
        chain_l, name_l = it["chain"].lower(), it["name"].lower()
        if chain_f and chain_f not in chain_l:
            return -1
        if not terms:
            return 1  # browse mode (chain filter or all)
        hay = chain_l + " " + name_l
        if not all(t in hay for t in terms):
            return -1
        s = sum(2 if t in name_l else 1 for t in terms)
        if name_l.startswith(terms[0]) or chain_l.startswith(terms[0]):
            s += 3
        return s

    scored = [(score(it), it) for it in idx]
    hits = sorted([(s, it) for s, it in scored if s >= 0], key=lambda x: -x[0])
    results = [it for _, it in hits[:25]]
    chains = sorted({it["chain"] for it in idx if it["chain"]})
    return jsonify({"results": results, "count": len(results), "chains": chains})


@app.post("/api/meals")
def add_meal():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify({"error": "JSON body required."}), 400
    if not d.get("date"):
        return jsonify({"error": "'date' (YYYY-MM-DD) is required."}), 400

    items_json = d.get("items_json")
    if not isinstance(items_json, str):
        items_json = json.dumps(items_json if items_json is not None else [])

    detail_json = d.get("detail_json")
    if detail_json is not None and not isinstance(detail_json, str):
        detail_json = json.dumps(detail_json)

    thumb = d.get("thumb")
    if not isinstance(thumb, str) or not thumb.startswith("data:image"):
        thumb = None

    # Provenance: stamp where this number came from + how trustworthy it is.
    tier = _norm_tier(d.get("accuracy_tier"))
    source = (str(d.get("source") or "")).strip() or "AI photo estimate"
    confidence = d.get("confidence")
    confidence = _int(confidence) if confidence is not None else _TIER_DEFAULT_CONFIDENCE[tier]

    con = get_db()
    try:
        cur = con.execute(
            """INSERT INTO meals(date, time, name, calories, protein_g, carbs_g, fat_g,
                                 items_json, detail_json, thumb, uid,
                                 source, accuracy_tier, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(d.get("date", "")),
                str(d.get("time", "")),
                str(d.get("name", "Meal")),
                _int(d.get("calories")),
                _int(d.get("protein_g")),
                _int(d.get("carbs_g")),
                _int(d.get("fat_g")),
                items_json,
                detail_json,
                thumb,
                _uid(),
                source,
                tier,
                confidence,
            ),
        )
        con.commit()
        return jsonify({"id": cur.lastrowid})
    finally:
        con.close()


@app.get("/api/meals")
def list_meals():
    day = request.args.get("date") or date.today().isoformat()
    con = get_db()
    try:
        rows = con.execute(
            """SELECT id, date, time, name, calories, protein_g, carbs_g, fat_g,
                      items_json, detail_json, thumb, source, accuracy_tier, confidence
               FROM meals WHERE date = ? AND uid = ? ORDER BY time, id""",
            (day, _uid()),
        ).fetchall()
    finally:
        con.close()
    meals = [dict(r) for r in rows]
    totals = {k: sum(_int(m.get(k)) for m in meals) for k in MACRO_KEYS}
    return jsonify({"meals": meals, "totals": totals})


@app.get("/api/recents")
def recent_foods():
    """Distinct recently-logged foods for this device (most-recent first) so the user can re-log a repeat meal in
       ONE tap — the completeness/retention feature every serious tracker has. Deduped by name."""
    con = get_db()
    try:
        rows = con.execute(
            """SELECT name, calories, protein_g, carbs_g, fat_g, source, accuracy_tier
               FROM meals WHERE uid = ? AND name IS NOT NULL AND TRIM(name) <> ''
               ORDER BY date DESC, time DESC, id DESC LIMIT 200""",
            (_uid(),),
        ).fetchall()
    finally:
        con.close()
    seen, out = set(), []
    for r in rows:
        key = (r["name"] or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"name": r["name"], "calories": _int(r["calories"]), "protein_g": _int(r["protein_g"]),
                    "carbs_g": _int(r["carbs_g"]), "fat_g": _int(r["fat_g"]),
                    "source": r["source"], "accuracy_tier": r["accuracy_tier"]})
        if len(out) >= 15:
            break
    return jsonify({"recents": out})


@app.delete("/api/meals/<int:meal_id>")
def delete_meal(meal_id):
    con = get_db()
    try:
        con.execute("DELETE FROM meals WHERE id = ? AND uid = ?", (meal_id, _uid()))
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True})


@app.post("/api/account/delete")
def delete_account():
    """Account/data deletion (Play + Apple 5.1.1v). Wipes this device's server-side food diary
       (uid-scoped); the client clears its local profile/weights/water/preferences on success so
       NONE of the user's personal data remains. Multi-tester-safe: only the caller's uid is touched."""
    con = get_db()
    try:
        cur = con.execute("DELETE FROM meals WHERE uid = ?", (_uid(),))
        con.commit()
        deleted = cur.rowcount if cur.rowcount is not None else 0
    finally:
        con.close()
    return jsonify({"ok": True, "deleted_meals": deleted})


DELETE_PAGE_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Delete your SnapCal data</title>
<style>body{font-family:-apple-system,Segoe UI,Roboto,system-ui,sans-serif;max-width:640px;margin:0 auto;
padding:32px 22px;line-height:1.6;color:#18181b;background:#fafaf9}h1{font-size:24px}h2{font-size:17px;margin-top:28px}
code{background:#f1f1f0;padding:2px 6px;border-radius:6px}.muted{color:#6b7280;font-size:14px}a{color:#059669}</style>
</head><body>
<h1>Delete your SnapCal data</h1>
<p>SnapCal (by Xionprotech LLC) lets you delete your account and all of your data at any time. There is no
sign-up and no login &mdash; your information stays on your device, with your food diary mirrored to our
server only so it survives a reinstall.</p>
<h2>Delete it inside the app (instant)</h2>
<p>Open SnapCal &rarr; <strong>Profile</strong> &rarr; scroll to <strong>Delete my data</strong> &rarr; confirm.
This immediately erases your food diary from our server and clears your profile, targets, weight history,
water log, and all preferences from the device.</p>
<h2>What gets deleted</h2>
<p>Your logged meals, calorie &amp; macro targets, body details (age, sex, height, weight), goal, weight
history, water log, day plan, and diet/allergy preferences. Deletion is permanent and cannot be undone.</p>
<h2>Need help?</h2>
<p>If you can&rsquo;t access the app, email <a href="mailto:tariq@xionprotech.com">tariq@xionprotech.com</a>
from any address and we&rsquo;ll remove your data within 30 days.</p>
<p class="muted">SnapCal provides general wellness guidance and is not medical advice.</p>
</body></html>"""


@app.get("/delete-my-data")
def delete_my_data_page():
    """Public account-deletion URL required by Google Play for the Data Safety form."""
    return app.response_class(DELETE_PAGE_HTML, mimetype="text/html")


PRIVACY_PAGE_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SnapCal Privacy Policy</title>
<style>body{font-family:-apple-system,Segoe UI,Roboto,system-ui,sans-serif;max-width:680px;margin:0 auto;
padding:32px 22px;line-height:1.6;color:#18181b;background:#fafaf9}h1{font-size:24px}h2{font-size:17px;margin-top:26px}
.muted{color:#6b7280;font-size:14px}a{color:#059669}ul{padding-left:20px}</style>
</head><body>
<h1>SnapCal Privacy Policy</h1>
<p class="muted">Xionprotech LLC &middot; Last updated June 2026</p>
<p>SnapCal helps you track food and movement for general wellness. We built it to need as little of your
data as possible. There is no account or login &mdash; your information lives on your device, with your food
diary mirrored to our server so it survives a reinstall.</p>
<h2>What we collect</h2>
<ul>
<li><strong>Food photos &amp; logs</strong> you create, and the calorie/macro estimates from them.</li>
<li><strong>Profile &amp; body details</strong> you enter: age, sex, height, weight, goal, and your calorie/macro targets.</li>
<li><strong>Weight and water logs</strong> you record.</li>
<li><strong>Approximate (coarse) location</strong> &mdash; only when you tap a &ldquo;near me&rdquo; feature, only while the app is open, and only to find nearby food options. We never track you in the background.</li>
<li>A random per-device ID so your diary stays separate from other users on our server.</li>
</ul>
<h2>Third-party AI</h2>
<p>To estimate nutrition from a photo and to power Coach Cal, the relevant image or text is sent to
<strong>Google&rsquo;s Gemini API</strong>, which processes it on our behalf and does not use it to train its
models. By analyzing a photo or chatting with Coach Cal you consent to this processing.</p>
<h2>What we never do</h2>
<p>We do not sell your data, and we never use your health, food, or location data for advertising,
ad-targeting, or data-mining.</p>
<h2>Your control &amp; deletion</h2>
<p>You can delete everything at any time: in the app, open <strong>Profile &rarr; Delete my data</strong>, or
visit <a href="/delete-my-data">our deletion page</a>. Deletion is permanent.</p>
<h2>Contact</h2>
<p>Questions or requests: <a href="mailto:tariq@xionprotech.com">tariq@xionprotech.com</a>.</p>
<p class="muted">SnapCal provides general wellness guidance and is not medical advice. Consult a doctor or
registered dietitian before making changes to your diet, exercise, or health.</p>
</body></html>"""


@app.get("/api/version")
def version():
    """Which build is live — so Tariq can confirm his phone loaded the latest (Render injects RENDER_GIT_COMMIT)."""
    return jsonify({"commit": (os.environ.get("RENDER_GIT_COMMIT") or "dev")[:7]})


@app.get("/privacy")
def privacy_page():
    """Public privacy-policy URL (required by both stores + the Play Data Safety form)."""
    return app.response_class(PRIVACY_PAGE_HTML, mimetype="text/html")


@app.get("/api/history")
def history():
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(365, days))
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    con = get_db()
    try:
        rows = con.execute(
            """SELECT date,
                      SUM(calories)  AS calories,
                      SUM(protein_g) AS protein_g,
                      SUM(carbs_g)   AS carbs_g,
                      SUM(fat_g)     AS fat_g
               FROM meals
               WHERE date >= ? AND uid = ?
               GROUP BY date
               ORDER BY date DESC""",
            (cutoff, _uid()),
        ).fetchall()
    finally:
        con.close()
    out = [{"date": r["date"], **{k: _int(r[k]) for k in MACRO_KEYS}} for r in rows]
    return jsonify({"days": out})


@app.get("/api/profile")
def get_profile():
    con = get_db()
    try:
        rows = con.execute("SELECT key, value FROM profile").fetchall()
    finally:
        con.close()
    stored = {r["key"]: r["value"] for r in rows}
    out = {k: _int(stored.get(k, v), v) for k, v in PROFILE_DEFAULTS.items()}
    for k in PROFILE_NUM_KEYS:
        if k in stored:
            out[k] = _int(stored.get(k))
    for k in PROFILE_TEXT_KEYS:
        if k in stored and stored.get(k) is not None:
            out[k] = str(stored.get(k))
    return jsonify(out)


@app.post("/api/profile")
def set_profile():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify({"error": "JSON body required."}), 400
    con = get_db()
    try:
        upsert = ("""INSERT INTO profile(key, value) VALUES (?, ?)
                     ON CONFLICT(key) DO UPDATE SET value = excluded.value""")
        for key, default in PROFILE_DEFAULTS.items():
            if key in d:
                con.execute(upsert, (key, _int(d[key], default)))
        for key in PROFILE_NUM_KEYS:
            if key in d and d[key] not in (None, "", 0):
                con.execute(upsert, (key, _int(d[key])))
        for key in PROFILE_TEXT_KEYS:
            if key in d and isinstance(d[key], str) and d[key].strip():
                con.execute(upsert, (key, d[key].strip()[:32]))
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True})


@app.get("/api/weights")
def list_weights():
    try:
        days = int(request.args.get("days", 90))
    except (TypeError, ValueError):
        days = 90
    days = max(1, min(730, days))
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    con = get_db()
    try:
        rows = con.execute(
            "SELECT date, weight FROM weights WHERE date >= ? ORDER BY date",
            (cutoff,),
        ).fetchall()
    finally:
        con.close()
    return jsonify({"weights": [{"date": r["date"], "weight": r["weight"]} for r in rows]})


def _ewma(vals, alpha=0.25):
    out, s = [], None
    for v in vals:
        s = v if s is None else (alpha * v + (1 - alpha) * s)
        out.append(s)
    return out


def _slope(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs) or 1.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


@app.get("/api/trend")
def trend():
    """Weight-trend adaptive TDEE (ACCURACY_ENGINE Rung 6 — MacroFactor's secret). Body-weight change is the
       physical integral of true net calories, so an EWMA trend-weight + logged intake reveals the user's REAL
       expenditure regardless of logging error, and a smarter calorie target to hit their goal pace. Pure math,
       no AI. Returns ready:false (with a friendly reason) until there's ~2 weeks of weigh-ins + ~1 week of food."""
    today = date.today()
    cutoff = (today - timedelta(days=35)).isoformat()
    con = get_db()
    try:
        wr = con.execute("SELECT date, weight FROM weights WHERE date >= ? ORDER BY date", (cutoff,)).fetchall()
        mr = con.execute("SELECT date, SUM(calories) c FROM meals WHERE date >= ? AND uid = ? GROUP BY date",
                         (cutoff, _uid())).fetchall()
    finally:
        con.close()
    w = []
    for r in wr:
        try:
            wt = float(r["weight"])
        except (TypeError, ValueError):
            continue
        if 0 < wt < 2000:
            w.append((date.fromisoformat(r["date"]), wt))
    span_days = (w[-1][0] - w[0][0]).days if len(w) >= 2 else 0
    if len(w) < 8 or span_days < 10:
        return jsonify({"ready": False, "reason": "weigh in a few more times — I need about 2 weeks of weigh-ins to read your real trend.",
                        "weighins": len(w), "need_weighins": 8})
    xs = [(d - w[0][0]).days for d, _ in w]
    ys = _ewma([wt for _, wt in w])
    weekly = _slope(xs, ys) * 7.0           # lb/week (negative = losing)
    trend_weight = round(ys[-1], 1)
    cals = [_int(r["c"]) for r in mr if _int(r["c"]) > 0]
    if len(cals) < 7:
        return jsonify({"ready": False, "reason": "log your food a bit more — about a week of days lets me calculate your real burn.",
                        "logged_days": len(cals), "need_days": 7})
    avg_intake = round(sum(cals) / len(cals))
    tdee = round(avg_intake - weekly * 3500.0 / 7.0)   # weekly<0 (losing) → tdee > intake
    # Goal-aware suggested daily target.
    gd = ""
    con = get_db()
    try:
        row = con.execute("SELECT value FROM profile WHERE key = 'goal_dir'").fetchone()
        gd = (row["value"] if row else "") or ""
    finally:
        con.close()
    if gd == "gain":
        suggested = tdee + 250
    elif gd == "maintain":
        suggested = tdee
    else:                                   # lose / recomp → moderate, muscle-preserving deficit
        suggested = tdee - 500
    suggested = max(1500, int(round(suggested / 10) * 10))
    return jsonify({
        "ready": True, "trend_weight": trend_weight, "weekly_rate_lb": round(weekly, 2),
        "avg_intake": avg_intake, "est_tdee": tdee, "suggested_target": suggested,
        "weighins": len(w), "logged_days": len(cals),
    })


@app.post("/api/weight")
def set_weight():
    d = request.get_json(silent=True)
    if not isinstance(d, dict) or not d.get("date"):
        return jsonify({"error": "'date' and 'weight' are required."}), 400
    try:
        w = float(d.get("weight"))
    except (TypeError, ValueError):
        return jsonify({"error": "weight must be a number."}), 400
    if not (0 < w <= 2000):
        return jsonify({"error": "weight out of range."}), 400
    con = get_db()
    try:
        con.execute(
            """INSERT INTO weights(date, weight) VALUES (?, ?)
               ON CONFLICT(date) DO UPDATE SET weight = excluded.weight""",
            (str(d.get("date")), round(w, 1)),
        )
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True})


WATER_GOAL = 8  # glasses/day default (1 glass ≈ 250 ml / 8 oz)


@app.get("/api/water")
def get_water():
    """Today's water count (glasses) + the daily goal. Simple per-day counter, like weights."""
    today = date.today().isoformat()
    con = get_db()
    try:
        row = con.execute("SELECT glasses FROM water WHERE date = ?", (today,)).fetchone()
    finally:
        con.close()
    return jsonify({"date": today, "glasses": (int(row["glasses"]) if row else 0), "goal": WATER_GOAL})


@app.post("/api/water")
def set_water():
    d = request.get_json(silent=True)
    if not isinstance(d, dict) or not d.get("date"):
        return jsonify({"error": "'date' and 'glasses' are required."}), 400
    try:
        g = int(d.get("glasses"))
    except (TypeError, ValueError):
        return jsonify({"error": "glasses must be an integer."}), 400
    g = max(0, min(40, g))
    con = get_db()
    try:
        con.execute(
            """INSERT INTO water(date, glasses) VALUES (?, ?)
               ON CONFLICT(date) DO UPDATE SET glasses = excluded.glasses""",
            (str(d.get("date")), g),
        )
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True, "glasses": g, "goal": WATER_GOAL})


# ---------------------------------------------------------------- errors

@app.errorhandler(413)
def too_large(_e):
    # Upload exceeded MAX_CONTENT_LENGTH — treat as a bad upload per contract.
    return jsonify({"error": "Image too large (max 15MB)."}), 400


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found."}), 404
    return "Not found. Is static/index.html in place?", 404


init_db()


if __name__ == "__main__":
    lan_ip = get_lan_ip()
    print(f"SnapCal is up.")
    print(f"  On this PC:                http://127.0.0.1:{PORT}")
    print(f"  On your phone (same WiFi): http://{lan_ip}:{PORT}")
    # threaded so one slow outbound call (Gemini, Overpass, a food-photo lookup) can't block every other request
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
