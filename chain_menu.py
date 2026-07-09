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
}

# Common chain-name aliases -> our canonical key (what OSM/geocoding might return).
_CHAIN_ALIASES = {
    "panera": "panera", "panera bread": "panera",
    "starbucks": "starbucks", "starbucks coffee": "starbucks",
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
    meal_cal = salad["calories"] + drink["calories"]
    print(f"OK  chains={covered_chains()}")
    print(f"OK  salad={salad['calories']}cal/{salad['protein_g']}g  drink={drink['calories']}cal")
    print(f"OK  official meal total = {meal_cal} cal (vs app's ~1,180 photo-guess)")
    print("all assertions passed")
