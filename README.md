# SnapCal

Personal Cal AI clone: snap a photo of your food, Gemini estimates calories + macros, log it, track against daily goals. Mobile-first web app served by Flask on this PC.

## Run

```
cd C:/Users/somme/apps/snapcal
python app.py
```

Requirements: `flask` and `google-genai` (both already installed on this machine).
The SQLite database `snapcal.db` is created automatically next to `app.py`.
The Gemini API key is read from `C:/Users/somme/youtube_videos/gemini_key.txt` — server-side only, never exposed to the browser.

## Use it from your phone

1. Make sure the phone is on the **same WiFi** as this PC.
2. Start the server (`python app.py`). It prints the LAN address, e.g.:
   `On your phone (same WiFi): http://192.168.1.23:5177`
3. Open that URL in the phone browser.
4. Optional: use "Add to Home Screen" (Share menu on iPhone, browser menu on Android) to install it like a native app.

If the phone can't reach it, allow Python through Windows Firewall for private networks (Windows will usually prompt on first run).

## Cost per scan

Each photo analysis is one Gemini 2.5 Flash vision call: roughly **$0.001 per scan** (a tenth of a cent). Logging, history, and profile are free — local SQLite only.

## API (for reference)

- `POST /api/analyze` — multipart `photo` (jpeg/png/webp/heic, max 15MB) → items + totals JSON
- `POST /api/meals` / `GET /api/meals?date=YYYY-MM-DD` / `DELETE /api/meals/<id>`
- `GET /api/history?days=30`
- `GET /api/profile` / `POST /api/profile`
