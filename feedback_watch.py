#!/usr/bin/env python3
"""SnapCal tester-feedback watcher — the bridge-proof delivery layer (Tariq 2026-07-19:
"sometimes the telegram bridge breaks — make sure if these people are reporting issues, you get it").

Three delivery layers, each independent:
  1. Server-side Telegram ping on submit (app.py _notify_feedback_telegram) — instant, but can break.
  2. THIS watcher: polls /api/feedback/admin, remembers the last-seen id, and on new reports
     (a) appends work orders to OPEN_LOOPS.md and (b) sends a LOCAL Telegram message directly to the
     bot API (independent of the bridge process AND of the server-side ping).
  3. SessionStart hook runs this with --quiet: any un-actioned reports surface at the top of every
     Claude session, even if Telegram is fully dead. The DB queue is the source of truth throughout.

Usage: python feedback_watch.py            poll + notify + append open loops
       python feedback_watch.py --quiet    poll only; print new/open reports (for SessionStart)
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://snapcal-api-lgla.onrender.com"
SECRETS = Path("C:/Users/somme/.secrets")
STATE = Path(__file__).with_name("_feedback_seen.json")
OPEN_LOOPS = Path("C:/Users/somme/OPEN_LOOPS.md")
QUIET = "--quiet" in sys.argv


def _read(p):
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def fetch_queue():
    key = _read(SECRETS / "snapcal_admin_key.txt")
    if not key:
        print("[feedback-watch] no admin key at .secrets/snapcal_admin_key.txt")
        return None
    req = urllib.request.Request(BASE + "/api/feedback/admin", headers={"X-Admin-Key": key})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read()).get("feedback", [])
    except Exception as e:  # noqa: BLE001
        print("[feedback-watch] fetch failed: %s" % e)
        return None


def telegram_local(text):
    """Direct bot-API send from THIS machine — independent of the bridge process."""
    token = _read(Path("C:/Users/somme/youtube_videos/telegram_bot_token.txt"))
    chat = _read(Path("C:/Users/somme/youtube_videos/telegram_chat_id.txt"))
    if not token or not chat:
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(
            urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=data),
            timeout=10)
        return True
    except Exception:  # noqa: BLE001
        return False


def main():
    rows = fetch_queue()
    if rows is None:
        sys.exit(0 if QUIET else 1)   # a dead network must never block a session start
    last_seen = 0
    try:
        last_seen = json.loads(STATE.read_text()).get("last_seen", 0)
    except Exception:  # noqa: BLE001
        pass
    new = [r for r in rows if r["id"] > last_seen]
    open_reports = [r for r in rows if r.get("status") == "new"]

    if QUIET:
        if new or open_reports:
            print("[SNAPCAL FEEDBACK] %d NEW since last check, %d total un-actioned. "
                  "Tester reports are WORK ORDERS — pull them first when working SnapCal:" %
                  (len(new), len(open_reports)))
            for r in (new or open_reports)[:10]:
                print("  #%s [%s] %s (build %s, %s)" %
                      (r["id"], r.get("category"), (r.get("text") or "")[:120],
                       r.get("app_commit"), (r.get("ua") or "")[:40]))
        return

    if not new:
        print("[feedback-watch] no new reports (last_seen=%s, open=%d)" % (last_seen, len(open_reports)))
        return
    print("[feedback-watch] %d new report(s):" % len(new))
    lines = []
    for r in sorted(new, key=lambda x: x["id"]):
        line = "#%s [%s] %s — build %s — %s" % (r["id"], r.get("category"),
                                                (r.get("text") or "")[:300],
                                                r.get("app_commit"), r.get("ts"))
        print("  " + line)
        lines.append(line)
    try:
        with OPEN_LOOPS.open("a", encoding="utf-8") as f:
            for ln in lines:
                f.write("- [ ] SNAPCAL TESTER FEEDBACK %s\n" % ln)
        print("[feedback-watch] appended %d work order(s) to OPEN_LOOPS.md" % len(lines))
    except Exception as e:  # noqa: BLE001
        print("[feedback-watch] OPEN_LOOPS append failed: %s" % e)
    sent = telegram_local("SnapCal tester feedback (%d new, local watcher):\n%s" %
                          (len(new), "\n".join(lines)[:3500]))
    print("[feedback-watch] local telegram: %s" % ("sent" if sent else "unavailable"))
    STATE.write_text(json.dumps({"last_seen": max(r["id"] for r in rows)}))


if __name__ == "__main__":
    main()
