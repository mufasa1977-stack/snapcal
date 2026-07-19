#!/usr/bin/env python3
"""
SnapCal Accuracy Study A — REAL, reproducible, honest.

Method: for a stratified sample of dishes from SnapCal's curated recipe library
(data/recipes.json — reference macros derived from USDA ingredient data), fetch a real
photo of the dish (the app's own /api/foodimg pipeline: Pexels/Openverse/local), run it
through the LIVE /api/analyze photo estimator (real Gemini), and compare the estimated
calories/protein against the reference values. No simulated numbers anywhere: every
estimate is a real model output, every reference is the curated USDA-derived value.

Reported: MAPE, median APE, % within +/-20% and +/-30%, direction of bias, per-item table.
Known limitation (disclosed, inherent to ANY photo-estimate study without weighed meals):
the photographed portion may differ from the reference portion — so this measures the
real-world end-to-end pipeline (photo in -> number out), which is exactly what a user
experiences. It is NOT a clinical weighed-food validation and is not claimed as one.

Usage: python data/accuracy_study.py [--base https://snapcal-api-lgla.onrender.com] [--n 24]
Writes: data/accuracy_study_results.json (+ prints a summary table)
"""
import argparse
import json
import random
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "SnapCal-StudyA/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.geturl()


def analyze(base, img_bytes, dev):
    boundary = "----studyA%d" % int(time.time() * 1000)
    body = (
        ("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"dish.jpg\"\r\n"
         "Content-Type: image/jpeg\r\n\r\n") % boundary
    ).encode() + img_bytes + ("\r\n--%s\r\nContent-Disposition: form-data; name=\"goal\"\r\n\r\nmaintain\r\n--%s--\r\n" % (boundary, boundary)).encode()
    req = urllib.request.Request(
        base + "/api/analyze", data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary,
                 "X-Device-Id": dev, "User-Agent": "SnapCal-StudyA/1.0"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://snapcal-api-lgla.onrender.com")
    ap.add_argument("--n", type=int, default=24)
    args = ap.parse_args()

    recipes = json.load(open(APP / "data" / "recipes.json", encoding="utf-8"))
    if isinstance(recipes, dict):
        recipes = recipes.get("recipes", [])
    random.seed(20260719)  # fixed seed -> reproducible sample
    by_cat = {}
    for r in recipes:
        if r.get("calories") and r.get("name"):
            by_cat.setdefault(r.get("cat", "other"), []).append(r)
    sample = []
    cats = sorted(by_cat)
    per = max(1, args.n // max(1, len(cats)))
    for c in cats:
        sample.extend(random.sample(by_cat[c], min(per, len(by_cat[c]))))
    sample = sample[: args.n]

    dev = "study-a-%d" % int(time.time())
    rows, failures = [], 0
    for i, r in enumerate(sample):
        name, truth_cal, truth_pro = r["name"], int(r["calories"]), int(r.get("protein_g") or 0)
        try:
            img, final_url = fetch(args.base + "/api/foodimg?dish=" + urllib.parse.quote(name))
            if len(img) < 5000:
                raise ValueError("image too small (%dB)" % len(img))
            est = analyze(args.base, img, dev)
            est_cal = int((est.get("total") or {}).get("calories") or 0)
            est_pro = int((est.get("total") or {}).get("protein_g") or 0)
            if est_cal <= 0:
                raise ValueError("no calories in estimate")
            ape = abs(est_cal - truth_cal) / truth_cal * 100.0
            rows.append({"dish": name, "cat": r.get("cat", ""), "ref_cal": truth_cal, "est_cal": est_cal,
                         "ref_protein_g": truth_pro, "est_protein_g": est_pro,
                         "ape_pct": round(ape, 1), "signed_err_pct": round((est_cal - truth_cal) / truth_cal * 100.0, 1),
                         "photo_src": ("local-category" if "/static/img/food/" in final_url else "pexels/openverse")})
            print("%2d/%d  %-42s ref %4d  est %4d  APE %5.1f%%" % (i + 1, len(sample), name[:42], truth_cal, est_cal, ape))
        except Exception as e:  # noqa: BLE001
            failures += 1
            print("%2d/%d  %-42s SKIP (%s)" % (i + 1, len(sample), name[:42], str(e)[:60]))
        time.sleep(2.0)  # be gentle on the live server

    if not rows:
        print("NO DATA — study failed"); sys.exit(1)
    apes = sorted(x["ape_pct"] for x in rows)
    mape = sum(apes) / len(apes)
    med = apes[len(apes) // 2]
    w20 = sum(1 for a in apes if a <= 20) / len(apes) * 100
    w30 = sum(1 for a in apes if a <= 30) / len(apes) * 100
    bias = sum(x["signed_err_pct"] for x in rows) / len(rows)
    out = {"date": "2026-07-19", "n_analyzed": len(rows), "n_failed": failures,
           "mape_pct": round(mape, 1), "median_ape_pct": round(med, 1),
           "within_20pct": round(w20), "within_30pct": round(w30),
           "mean_signed_error_pct": round(bias, 1),
           "method": "Stratified sample of curated USDA-derived reference dishes; real dish photo via /api/foodimg; "
                     "LIVE /api/analyze (Gemini) estimate vs reference calories. Fixed seed 20260719 = reproducible.",
           "limitation": "Photographed portion may differ from reference portion (no weighed meals). Measures the "
                         "real-world end-to-end photo pipeline, not a clinical weighed-food validation.",
           "rows": rows}
    (APP / "data" / "accuracy_study_results.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("\n===== STUDY A RESULTS (n=%d, %d failed fetches) =====" % (len(rows), failures))
    print("MAPE %.1f%%  |  median APE %.1f%%  |  within ±20%%: %.0f%%  |  within ±30%%: %.0f%%  |  mean bias %+.1f%%"
          % (mape, med, w20, w30, bias))
    print("saved -> data/accuracy_study_results.json")


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (used in main)
    main()
