"""Merge _pending_*.json chain files into restaurants.json. Idempotent + re-runnable
(dedupes by chain name), so it's safe to run again as more research batches land."""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent
main = json.loads((DATA / "restaurants.json").read_text(encoding="utf-8"))
existing = {c["chain"] for c in main["restaurants"]}
added, skipped = [], []

for f in sorted(DATA.glob("_pending_*.json")):
    try:
        arr = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print("SKIP (bad json):", f.name, e)
        continue
    for c in arr:
        ch = c.get("chain")
        if not ch or not c.get("best_picks"):
            skipped.append(str(c.get("chain")))
            continue
        if ch in existing:
            continue
        main["restaurants"].append(c)
        existing.add(ch)
        added.append(ch)

main["version"] = "2026-06-21"
(DATA / "restaurants.json").write_text(json.dumps(main, ensure_ascii=False, indent=2), encoding="utf-8")
print("ADDED", len(added), ":", ", ".join(added))
print("TOTAL", len(main["restaurants"]))
if skipped:
    print("SKIPPED malformed:", skipped)
