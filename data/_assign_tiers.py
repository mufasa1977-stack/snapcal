"""Assign a health_tier to every chain in restaurants.json:
   1 = GREEN  (easiest to eat clean: salad / grain-bowl / grilled / deli-forward)
   2 = YELLOW (doable with intention: mixed menu, lean options present)
   3 = MAROON (fried-heavy / indulgent, but a genuinely lean pick exists)
   4 = RED    (greasy / treat-heavy: hardest to eat clean — still surfaces the least-bad / cheat-day pick)
Curated; re-runnable (covers future batches too). Tariq's eye is final — tweak TIER below, re-run, restart."""
import json
from collections import Counter
from pathlib import Path

TIER = {
    # 1 = GREEN
    "Chick-fil-A": 1, "Subway": 1, "Panera Bread": 1, "Chipotle": 1, "Jersey Mike's": 1,
    "Sweetgreen": 1, "CAVA": 1, "Qdoba": 1, "El Pollo Loco": 1, "Starbucks": 1,
    "Jimmy John's": 1, "Firehouse Subs": 1,
    # 2 = YELLOW
    "McDonald's": 2, "Wendy's": 2, "Taco Bell": 2, "Panda Express": 2, "Dunkin'": 2,
    "Tim Hortons": 2, "Smoothie King": 2, "Wawa": 2, "Sheetz": 2, "QuikTrip": 2,
    "Cumberland Farms": 2, "Whataburger": 2, "In-N-Out Burger": 2,
    # 3 = MAROON
    "Burger King": 3, "KFC": 3, "Popeyes": 3, "Five Guys": 3, "Raising Cane's": 3,
    "Zaxby's": 3, "Bojangles": 3, "Wingstop": 3, "Shake Shack": 3, "Royal Farms": 3,
    "Casey's General Store": 3, "7-Eleven": 3, "Arby's": 3, "Sonic Drive-In": 3,
    "Jack in the Box": 3, "Culver's": 3,
    # 4 = RED
    "Dairy Queen": 4, "Buc-ee's": 4, "Domino's": 4, "Pizza Hut": 4, "Little Caesars": 4,
    "Papa John's": 4,
}

DATA = Path(__file__).resolve().parent / "restaurants.json"
d = json.loads(DATA.read_text(encoding="utf-8"))
unmapped = []
for c in d["restaurants"]:
    ch = c.get("chain")
    if ch in TIER:
        c["health_tier"] = TIER[ch]
    else:
        c["health_tier"] = c.get("health_tier", 2)
        unmapped.append(ch)
DATA.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
print("tiers:", dict(sorted(Counter(c["health_tier"] for c in d["restaurants"]).items())))
if unmapped:
    print("UNMAPPED (defaulted to 2):", unmapped)
