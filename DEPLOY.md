# SnapCal — permanent, always‑on deploy (not tied to Tariq's PC)

The quick `cloudflared` tunnel is great for testing but dies if the PC sleeps. This makes SnapCal a
real cloud service that's up 24/7 on its own, reachable at **coach.xionprotech.com**.

**Recommended host: Render** (render.com) — easiest Flask deploy, free tier for testing, custom domain support.
The app is already cloud‑ready: `PORT`, `GEMINI_API_KEY`, `SNAPCAL_PEXELS_KEY`, `USDA_API_KEY` all read from
env; `gunicorn` + `Procfile` + `render.yaml` are in place; `.gitignore` keeps every secret out of git.

## The ONE thing only Tariq can do
Create a free Render account → **render.com** → "Get Started" (sign in with the GitHub or Google account
you want to own it). That's it. I (Claude) do everything after that.

## What I do once the account exists (≈10 min, no further input)
1. Push this folder to a **private GitHub repo** (secrets are gitignored — verified).
2. In Render: **New → Blueprint**, point at the repo. It reads `render.yaml` automatically.
3. Set the three secrets in the Render dashboard (I paste them from the local key files — they never enter git):
   - `GEMINI_API_KEY`  (from `gemini_key.txt`)
   - `SNAPCAL_PEXELS_KEY`  (from `pexels_key.txt`)
   - `USDA_API_KEY`  (the free api.data.gov key — removes the demo rate limit)
4. Deploy → it goes live at `https://snapcal-api.onrender.com`.
5. **Custom domain:** add `coach.xionprotech.com` in Render → it gives a CNAME target → I add that CNAME in
   Cloudflare DNS (your domain is already on Cloudflare). HTTPS is automatic. Done — permanent branded link.

## Notes / honest caveats
- **Free tier sleeps** after ~15 min idle (≈30s cold start on the next visit). Fine for testing. For real
  users, bump `plan: free` → `plan: starter` ($7/mo) in `render.yaml` to keep it always warm.
- **Database / user history:** `render.yaml` now declares a **persistent disk** (`/var/data`) and the app
  reads `SNAPCAL_DB_DIR`, so `snapcal.db` lives on it — **history survives every redeploy/update.** Disks need
  a paid plan, so it activates once `plan: free` → `plan: starter`. On the **free tier (no disk)** the app
  falls back to its folder and the DB is still ephemeral — fine for throwaway testing, NOT for real users. So
  before onboarding real users, set `plan: starter`. Migrations are additive-only (guarded
  `ALTER TABLE ADD COLUMN`) so schema changes never drop rows. Verified: meal logged → server restarted
  (simulated redeploy) → meal + provenance tier still present.
- This does NOT replace the native app plan — background GPS / instant location still needs the App Store /
  Play app. This just gives a rock‑solid always‑on web link to test and share today.

## Alternative (faster, but still PC‑tied — not recommended for "always on")
A **named Cloudflare tunnel** would give the permanent `coach.xionprotech.com` URL in ~10 min, but it still
routes to this PC, so it's down whenever the PC sleeps. Since the goal is "not tied to the machine," the
Render deploy above is the right call.
