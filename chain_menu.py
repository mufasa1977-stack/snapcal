"""
chain_menu.py — the OFFICIAL nutrition tier for SnapCal.

When the user is at a known chain restaurant, their published menu nutrition is the
GROUND TRUTH — it beats any AI photo-estimate or even USDA-density cross-check, because
the chain measured the actual recipe. This module is that lookup: given a chain + item
name, return the chain's official published macros, tagged source="OFFICIAL".

Design matches the app's existing free-data philosophy (USDA FDC, OSM/Overpass,
OpenFoodFacts barcodes) — NO paid API, NO key. Verified item data is cached here and
can be grown by a background scraper of the chains' own nutrition pages/PDFs.

Provenance tiers (see app.py header): OFFICIAL (published menu) > VERIFIED (USDA + known
portion) > ESTIMATE (AI photo guess). This module supplies the top tier.

Seed data below was verified 2026-07-05 directly from the chains' official product pages
(the meal that exposed the gap: Panera "Ultimate Garden Steak Salad" + Starbucks Trenta
"Mango Dragonfruit Refresher"). All numbers are per the WHOLE listed size.
"""
from __future__ import annotations
import re

# Each entry: canonical item -> official macros for the listed size.
# kcal, protein_g, carbs_g, fat_g, sugar_g, sodium_mg, plus the size + source URL.
# Grown over time by scraping the chains' published nutrition (panerabread.com product
# pages, Starbucks nutrition pages, etc.), cached the way USDA/OpenFoodFacts already are.
# Outback's official published nutrition PDF (their Azure blob, linked from
# outback.com/nutrition/smart-dining). "Updated February 2023" per the doc header.
# Fetched + table-extracted directly from this URL on 2026-07-20; row alignment was
# coordinate-verified (item-name y-position matched its number row) before use.
_OUTBACK_PDF = (
    "https://outback.blob.core.windows.net/content/images/"
    "OBS_Full_Nutrition_Information_Core_Menu_Items.pdf"
)

_MENU = {
    "panera": {
        "ultimate garden steak salad": {
            "size": "Whole", "calories": 800, "protein_g": 33, "carbs_g": 53,
            "fat_g": 52, "sugar_g": 21, "sodium_mg": 1690,
            "url": "https://www.panerabread.com/en-us/menu/products/ultimate-garden-steak-salad.html",
        },
    },
    "starbucks": {
        "mango dragonfruit refresher": {
            "size": "Trenta (30 oz)", "calories": 180, "protein_g": 1, "carbs_g": 44,
            "fat_g": 0, "sugar_g": 39, "sodium_mg": 15,
            "url": "https://www.starbucks.com/menu/product/2122725/iced",
        },
    },
    # Outback Steakhouse — all numbers per the WHOLE listed size, transcribed from
    # Outback's own published nutrition PDF (_OUTBACK_PDF). Sizes 6oz/8oz etc. kept as
    # distinct keys because the token matcher drops bare single digits; the size label
    # carries the portion. Sugar/fiber shown as "<1" in the PDF are stored as 1.
    "outback": {
        "aussie cheese fries": {
            "size": "Full order (shareable)", "calories": 2620, "protein_g": 89, "carbs_g": 153,
            "fat_g": 182, "sugar_g": 1, "sodium_mg": 7490,
            "url": _OUTBACK_PDF,
        },
        "bloomin onion": {
            "size": "Whole (with sauce)", "calories": 1620, "protein_g": 15, "carbs_g": 107,
            "fat_g": 126, "sugar_g": 20, "sodium_mg": 4140,
            "url": _OUTBACK_PDF,
        },
        "bloomin fried shrimp": {
            "size": "1 serving", "calories": 990, "protein_g": 45, "carbs_g": 53,
            "fat_g": 66, "sugar_g": 1, "sodium_mg": 5830,
            "url": _OUTBACK_PDF,
        },
        "gold coast coconut shrimp": {
            "size": "1 serving (appetizer)", "calories": 520, "protein_g": 31, "carbs_g": 49,
            "fat_g": 21, "sugar_g": 28, "sodium_mg": 640,
            "url": _OUTBACK_PDF,
        },
        "victorias filet mignon 6oz": {
            "size": "6 oz", "calories": 380, "protein_g": 47, "carbs_g": 1,
            "fat_g": 19, "sugar_g": 0, "sodium_mg": 470,
            "url": _OUTBACK_PDF,
        },
        "victorias filet mignon 8oz": {
            "size": "8 oz", "calories": 530, "protein_g": 62, "carbs_g": 1,
            "fat_g": 29, "sugar_g": 0, "sodium_mg": 540,
            "url": _OUTBACK_PDF,
        },
        "outback center-cut sirloin 6oz": {
            "size": "6 oz", "calories": 370, "protein_g": 46, "carbs_g": 1,
            "fat_g": 20, "sugar_g": 0, "sodium_mg": 510,
            "url": _OUTBACK_PDF,
        },
        "outback center-cut sirloin 8oz": {
            "size": "8 oz", "calories": 450, "protein_g": 60, "carbs_g": 1,
            "fat_g": 23, "sugar_g": 0, "sodium_mg": 710,
            "url": _OUTBACK_PDF,
        },
        "ribeye 12oz": {
            "size": "12 oz", "calories": 900, "protein_g": 58, "carbs_g": 1,
            "fat_g": 72, "sugar_g": 0, "sodium_mg": 610,
            "url": _OUTBACK_PDF,
        },
        "ribeye 15oz": {
            "size": "15 oz", "calories": 1110, "protein_g": 73, "carbs_g": 1,
            "fat_g": 88, "sugar_g": 0, "sodium_mg": 650,
            "url": _OUTBACK_PDF,
        },
        "alice springs chicken": {
            "size": "1 serving", "calories": 780, "protein_g": 79, "carbs_g": 14,
            "fat_g": 47, "sugar_g": 12, "sodium_mg": 1160,
            "url": _OUTBACK_PDF,
        },
        "grilled chicken on the barbie": {
            "size": "1 serving", "calories": 410, "protein_g": 62, "carbs_g": 22,
            "fat_g": 9, "sugar_g": 17, "sodium_mg": 780,
            "url": _OUTBACK_PDF,
        },
        "toowoomba salmon": {
            "size": "1 serving", "calories": 760, "protein_g": 61, "carbs_g": 7,
            "fat_g": 53, "sugar_g": 3, "sodium_mg": 1100,
            "url": _OUTBACK_PDF,
        },
        "perfectly grilled salmon": {
            "size": "1 serving", "calories": 550, "protein_g": 45, "carbs_g": 1,
            "fat_g": 39, "sugar_g": 0, "sodium_mg": 430,
            "url": _OUTBACK_PDF,
        },
        "baby back ribs full rack": {
            "size": "Full rack", "calories": 1430, "protein_g": 96, "carbs_g": 53,
            "fat_g": 91, "sugar_g": 42, "sodium_mg": 2310,
            "url": _OUTBACK_PDF,
        },
        "baby back ribs half rack": {
            "size": "1/2 rack", "calories": 720, "protein_g": 48, "carbs_g": 26,
            "fat_g": 46, "sugar_g": 21, "sodium_mg": 1160,
            "url": _OUTBACK_PDF,
        },
        "steakhouse mac & cheese": {
            "size": "1 serving (side/entree)", "calories": 720, "protein_g": 25, "carbs_g": 74,
            "fat_g": 37, "sugar_g": 8, "sodium_mg": 1010,
            "url": _OUTBACK_PDF,
        },
        "aussie fries": {
            "size": "1 serving (side)", "calories": 500, "protein_g": 7, "carbs_g": 67,
            "fat_g": 23, "sugar_g": 1, "sodium_mg": 1960,
            "url": _OUTBACK_PDF,
        },
        "loaded mashed potatoes": {
            "size": "1 serving", "calories": 320, "protein_g": 11, "carbs_g": 22,
            "fat_g": 22, "sugar_g": 3, "sodium_mg": 1440,
            "url": _OUTBACK_PDF,
        },
        "homestyle mashed potatoes": {
            "size": "1 serving", "calories": 230, "protein_g": 4, "carbs_g": 28,
            "fat_g": 11, "sugar_g": 1, "sodium_mg": 540,
            "url": _OUTBACK_PDF,
        },
        "baked potato with everything": {
            "size": "1 (loaded)", "calories": 440, "protein_g": 13, "carbs_g": 58,
            "fat_g": 17, "sugar_g": 7, "sodium_mg": 940,
            "url": _OUTBACK_PDF,
        },
        "broccoli": {
            "size": "1 serving (fresh)", "calories": 140, "protein_g": 5, "carbs_g": 12,
            "fat_g": 9, "sugar_g": 4, "sodium_mg": 290,
            "url": _OUTBACK_PDF,
        },
        "house salad no dressing": {
            "size": "1 side salad", "calories": 120, "protein_g": 5, "carbs_g": 8,
            "fat_g": 7, "sugar_g": 2, "sodium_mg": 180,
            "url": _OUTBACK_PDF,
        },
        "caesar salad side": {
            "size": "Side, with dressing", "calories": 270, "protein_g": 6, "carbs_g": 7,
            "fat_g": 25, "sugar_g": 1, "sodium_mg": 600,
            "url": _OUTBACK_PDF,
        },
        "chocolate thunder from down under": {
            "size": "1 serving", "calories": 1520, "protein_g": 18, "carbs_g": 142,
            "fat_g": 105, "sugar_g": 119, "sodium_mg": 380,
            "url": _OUTBACK_PDF,
        },
    },
}

# Common chain-name aliases -> our canonical key (what OSM/geocoding might return).
_CHAIN_ALIASES = {
    "panera": "panera", "panera bread": "panera",
    "starbucks": "starbucks", "starbucks coffee": "starbucks",
    "outback": "outback", "outback steakhouse": "outback",
}

_STOP = {"the", "a", "with", "and", "of", "&", "cal", "calorie", "size", "large",
         "largest", "whole", "trenta", "venti", "grande"}


def _norm_chain(name: str) -> str | None:
    key = re.sub(r"[^a-z0-9 ]", "", (name or "").lower()).strip()
    return _CHAIN_ALIASES.get(key)


def _tokens(text: str) -> set[str]:
    words = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split()
    return {w for w in words if w not in _STOP and len(w) > 1}


def lookup(chain: str, item_name: str, min_overlap: float = 0.6) -> dict | None:
    """Return official macros for `item_name` at `chain`, or None if not covered.

    Matching is token-overlap (order-independent, tolerant of the AI's descriptive
    renames) so "Steak Garden Salad" still matches "Ultimate Garden Steak Salad".
    `min_overlap` = fraction of the MENU item's key tokens that must be present.
    """
    ck = _norm_chain(chain)
    if not ck or ck not in _MENU:
        return None
    want = _tokens(item_name)
    if not want:
        return None
    best, best_score = None, 0.0
    for canon, data in _MENU[ck].items():
        keyset = _tokens(canon)
        if not keyset:
            continue
        overlap = len(want & keyset) / len(keyset)
        if overlap > best_score:
            best, best_score = (canon, data), overlap
    if best and best_score >= min_overlap:
        canon, data = best
        out = dict(data)
        out.update(source="OFFICIAL", chain=ck, item=canon, match_confidence=round(best_score, 2))
        return out
    return None


def covered_chains() -> list[str]:
    return sorted(_MENU.keys())


if __name__ == "__main__":
    # Self-test against the REAL 2026-07-05 lunch that exposed the accuracy gap.
    salad = lookup("Panera Bread", "Ultimate Garden Steak Salad")
    assert salad and salad["calories"] == 800 and salad["protein_g"] == 33, salad
    # AI's descriptive rename still matches via token overlap:
    salad2 = lookup("panera", "garden steak salad with creamy dressing")
    assert salad2 and salad2["calories"] == 800, salad2
    drink = lookup("Starbucks", "Mango Dragonfruit Refresher")
    assert drink and drink["calories"] == 180 and drink["sugar_g"] == 39, drink
    # Unknown item / chain -> None (falls back to USDA/AI, never a wrong OFFICIAL):
    assert lookup("Panera", "grilled cheese") is None
    assert lookup("McDonalds", "Big Mac") is None

    # Outback — the 2026-07-19 dinner that exposed the gap: Tariq scanned Aussie Cheese
    # Fries. The published number (from Outback's own nutrition PDF) is the ground truth.
    fries = lookup("Outback Steakhouse", "Aussie Cheese Fries")
    assert fries and fries["calories"] == 2620 and fries["protein_g"] == 89, fries
    assert fries["source"] == "OFFICIAL" and fries["url"] == _OUTBACK_PDF, fries
    # Alias without "Steakhouse" also resolves:
    onion = lookup("Outback", "Bloomin Onion")
    assert onion and onion["calories"] == 1620, onion
    # Item Outback doesn't sell -> None (never a wrong OFFICIAL; falls back to USDA/AI):
    assert lookup("Outback", "kangaroo burger") is None

    meal_cal = salad["calories"] + drink["calories"]
    print(f"OK  chains={covered_chains()}")
    print(f"OK  salad={salad['calories']}cal/{salad['protein_g']}g  drink={drink['calories']}cal")
    print(f"OK  official meal total = {meal_cal} cal (vs app's ~1,180 photo-guess)")
    print(f"OK  Outback Aussie Cheese Fries = {fries['calories']}cal / {fries['protein_g']}g protein "
          f"/ {fries['carbs_g']}g carbs / {fries['fat_g']}g fat / {fries['sodium_mg']}mg sodium")
    print("all assertions passed")
