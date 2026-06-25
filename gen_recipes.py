"""Build-time recipe library generator for SnapCal Meals (the Wellos-style browser).

Generates a curated, goal-friendly recipe library via Gemini ONCE and bakes it to
data/recipes.json so the in-app browser is instant (no per-tap spinner) and the
category counts are HONEST (the real number we ship). Re-run to refresh/expand.

    python gen_recipes.py            # generate all categories
    python gen_recipes.py --n 18     # 18 recipes per category
"""
import json
import sys
import time
from pathlib import Path

import app  # reuse the same Gemini client/key/model + JSON parser the server uses

OUT = Path(__file__).parent / "data" / "recipes.json"

# Premium, goal-useful categories (mirrors the Wellos browse set, tuned to coaching).
CATEGORIES = [
    {"key": "high-protein", "label": "High Protein",      "cover": "grilled chicken quinoa bowl", "blurb": "Hit your protein goal"},
    {"key": "low-carb",     "label": "Low Carb",          "cover": "salmon asparagus plate",      "blurb": "Lean and light on carbs"},
    {"key": "mediterranean","label": "Mediterranean",     "cover": "greek chicken bowl",          "blurb": "Bright, olive-oil forward"},
    {"key": "vegan",        "label": "Vegan",             "cover": "colorful buddha bowl",        "blurb": "100% plant-based"},
    {"key": "vegetarian",   "label": "Vegetarian",        "cover": "vegetable stir fry",          "blurb": "Meat-free, never boring"},
    {"key": "quick",        "label": "Quick & Easy",      "cover": "avocado toast egg",           "blurb": "On the table in 15"},
    {"key": "batch",        "label": "Batch & Prep",      "cover": "meal prep containers",        "blurb": "Cook once, eat all week"},
    {"key": "breakfast",    "label": "Breakfast",         "cover": "berry protein oatmeal",       "blurb": "Start strong"},
    {"key": "asian",        "label": "Asian Inspired",    "cover": "teriyaki salmon rice bowl",   "blurb": "Big flavor, balanced"},
    {"key": "comfort",      "label": "Whole-Food Comfort","cover": "turkey chili bowl",           "blurb": "Hearty and clean"},
    {"key": "snacks",       "label": "Snacks & Treats",   "cover": "greek yogurt berry parfait",  "blurb": "Smart little bites"},
    {"key": "indian",       "label": "Indian Inspired",   "cover": "chana masala chickpea curry", "blurb": "Spiced and satisfying"},
]

SECTIONS = ["Produce", "Meat & Seafood", "Dairy & Eggs", "Pantry & Dry Goods",
            "Grains & Bread", "Frozen", "Spices & Condiments"]

PROMPT = """You are Coach Cal, a nutrition coach. Generate {n} DISTINCT, real, appealing recipes for the
category "{label}" ({blurb}). They must be genuinely healthy, home-cookable, and varied (no near-duplicates).

Return ONLY JSON: {{"recipes":[ ... ]}}. Each recipe object:
{{
  "name": "appetizing dish name (max 6 words)",
  "photo": "2-4 plain words for a stock-photo search of the finished dish, e.g. 'grilled salmon bowl'",
  "calories": integer 250-750,
  "protein_g": integer,
  "carbs_g": integer,
  "fat_g": integer,
  "minutes": integer total time,
  "diet": one of "omnivore" | "pescatarian" | "vegetarian" | "vegan" (be ACCURATE),
  "allergens": array, subset of ["dairy","gluten","eggs","tree nuts","peanuts","shellfish","fish","soy"] — only ones actually present,
  "ingredients": [{{"name":"plain item","qty":"e.g. 6 oz / 1 cup","section":"one of {sections}"}}],  (4-9 items)
  "steps": ["3-5 short imperative steps"],
  "tip": "one short Coach Cal tip (swap/goal angle), under 16 words"
}}
Macros must roughly add up to the calories (P*4 + C*4 + F*9 ≈ calories). For "{label}", honor the category:
high-protein >=30g protein; low-carb <=25g carbs; vegan strictly plant-only; etc. No commentary, JSON only."""


def gen_category(cat, n):
    from google import genai
    client = app.get_gemini_client()
    prompt = PROMPT.format(n=n, label=cat["label"], blurb=cat["blurb"], sections=SECTIONS)
    resp = client.models.generate_content(
        model=app.GEMINI_MODEL,
        contents=[prompt],
        config=genai.types.GenerateContentConfig(response_mime_type="application/json"),
    )
    data = app.parse_gemini_json(resp.text) or {}
    out = []
    rid = 0
    for r in (data.get("recipes") or []):
        if not isinstance(r, dict) or not r.get("name"):
            continue
        rid += 1
        out.append({
            "id": cat["key"] + "-" + str(rid),
            "cat": cat["key"],
            "name": str(r.get("name"))[:60],
            "photo": str(r.get("photo") or r.get("name"))[:48],
            "calories": app._int(r.get("calories"), 0),
            "protein_g": app._int(r.get("protein_g"), 0),
            "carbs_g": app._int(r.get("carbs_g"), 0),
            "fat_g": app._int(r.get("fat_g"), 0),
            "minutes": app._int(r.get("minutes"), 0),
            "diet": (str(r.get("diet") or "omnivore").lower()
                     if str(r.get("diet") or "").lower() in ("omnivore", "pescatarian", "vegetarian", "vegan")
                     else "omnivore"),
            "allergens": [a for a in (r.get("allergens") or []) if isinstance(a, str)],
            "ingredients": [
                {"name": str(i.get("name", "")).strip(), "qty": str(i.get("qty", "")).strip(),
                 "section": (i.get("section") if i.get("section") in SECTIONS else "Pantry & Dry Goods")}
                for i in (r.get("ingredients") or []) if isinstance(i, dict) and i.get("name")
            ],
            "steps": [str(s).strip() for s in (r.get("steps") or []) if str(s).strip()][:6],
            "tip": str(r.get("tip") or "").strip()[:120],
        })
    return out


def main():
    n = 16
    if "--n" in sys.argv:
        try:
            n = int(sys.argv[sys.argv.index("--n") + 1])
        except Exception:
            pass
    all_recipes = []
    cats_out = []
    for cat in CATEGORIES:
        for attempt in range(2):
            try:
                recs = gen_category(cat, n)
                if recs:
                    break
            except Exception as e:  # noqa: BLE001
                print("  ! %s attempt %d failed: %s" % (cat["key"], attempt + 1, e), flush=True)
                recs = []
                time.sleep(2)
        all_recipes.extend(recs)
        cats_out.append({"key": cat["key"], "label": cat["label"], "blurb": cat["blurb"],
                         "cover": cat["cover"], "count": len(recs)})
        print("  %-16s %d recipes" % (cat["key"], len(recs)), flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"categories": cats_out, "recipes": all_recipes}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print("WROTE %s  (%d categories, %d recipes)" % (OUT, len(cats_out), len(all_recipes)), flush=True)


if __name__ == "__main__":
    main()
