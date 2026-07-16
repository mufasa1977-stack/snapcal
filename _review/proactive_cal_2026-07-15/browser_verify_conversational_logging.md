# Browser verification — Conversational Logging (375x812)

Driven live in the Claude Browser pane against the local dev server (http://127.0.0.1:5177),
viewport forced to 375x812 (iPhone-size). `computer{action:"screenshot"|"zoom"}` timed out
repeatedly this session (infra-side — read_page / find / clicks / javascript_exec all worked
normally against the same tab, so the page itself was never stuck). Pixel screenshots could not
be captured; the DOM state below was captured instead via `javascript_exec` against the SAME real
rendered page (real service worker, real app.js, real user-gesture-equivalent .click() calls) —
not a headless mock. Server-only fields (/api/chat, POST /api/meals) were stubbed via `window.api`
override so the flow is deterministic and costs no Gemini credits, exactly like the regression
gate's existing pattern (`page.route` fixtures) — everything else (openVoice, sendChat, chip
rendering, click handling, toast) is the real client code path.

## Step 1 — open Coach Cal, report eating something

Called `openVoice()` (the real handler behind the "Talk to Coach Cal" button), confirmed the panel
is actually visible (`#voiceWrap` computed `display: flex`), then `sendChat('I just had a
cheesesteak and fries')` with `/api/chat` stubbed to return a `log_proposal`.

Resulting `#voiceLog` text (real DOM, real render):

```
Hey, I'm Coach Cal! Tap the mic and talk to me — or type. Ask me what to eat, what a word like
'protein' or 'macros' means, anything at all.
I just had a cheesesteak and fries
Nice, a cheesesteak with fries — solid pick! I've got that logged for you below.
Log it: Cheesesteak ~700 Cal  +  French fries ~450 Cal
✓ Log
✎ Edit
✕
```

Chip HTML (`.log-proposal`, real rendered element):

```html
<div class="vmsg vmsg-coach log-proposal" style="display: block;">
  <div>Log it: <b>Cheesesteak ~700 Cal  +  French fries ~450 Cal</b></div>
  <div style="display:flex;gap:8px;margin-top:9px">
    <button class="btn btn-dark">✓ Log</button>
    <button>✎ Edit</button>
    <button aria-label="Dismiss">✕</button>
  </div>
</div>
```

`mealsPostedBeforeTap: null` — confirms the proposal renders WITHOUT ever calling `/api/meals`
(never silently logs).

## Step 2 — tap "✓ Log"

Real `.click()` on the button element (same click path a finger tap fires).

Result:
- `stillThere: false` — the chip removed itself.
- `toastText: "✓ Logged"` — the success toast fired.
- (from the earlier gate run, same code path, exact POST body observed):
  `{"calories": 1150, "protein_g": 40, "carbs_g": 115, "fat_g": 52, "source": "Coach Cal chat",
  "accuracy_tier": "estimate", "name": "Cheesesteak, French fries", ...}` — both items summed into
  ONE meal entry through the existing `/api/meals` endpoint, exactly like a manual add.

## Gentle mode + dismiss (from the regression gate's Playwright run, same real code path)

- Gentle-mode chip text: `Log it: Grilled chicken bowl✓ Log✎ Edit✕` — food name only, zero digits.
- Dismiss (✕): `mealsPostedAfterDismiss: None`, `stillThere: False` — never posts, always removable.

## Honest gap

No PNG/JPEG screenshots in this folder — the `computer` tool's screenshot/zoom actions timed out
repeatedly this session for infra reasons unrelated to the app (confirmed: every other browser
action — navigate, read_page, find, click-by-ref, javascript_exec — worked normally against the
same tab throughout). The interaction above is genuine live-browser DOM evidence, not a claim
without a mechanism to back it up, but it is not a pixel image. Flagging this gap rather than
silently substituting text for the requested screenshots.
