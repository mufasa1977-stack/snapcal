// build-www.mjs — assemble the Capacitor web bundle (www/) from the existing frontend.
//   1. bundles billing.src.js (RevenueCat) -> www/billing.js
//   2. copies static/index.html -> www/index.html, injecting the hosted API base + billing.js
// Run: node build-www.mjs   (or: npm run build:www)
// Set the backend URL once you've deployed it:  SNAPCAL_API_BASE=https://snapcal-api.onrender.com node build-www.mjs
import { build } from "esbuild";
import { mkdirSync, readFileSync, writeFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const ROOT = dirname(fileURLToPath(import.meta.url));
const WWW = join(ROOT, "www");
const API_BASE = process.env.SNAPCAL_API_BASE || ""; // "" = same-origin (won't work in the native app — set this!)

mkdirSync(WWW, { recursive: true });

// 1) bundle RevenueCat billing -> www/billing.js (exposes window.Billing)
await build({
  entryPoints: [join(ROOT, "billing.src.js")],
  bundle: true,
  format: "iife",
  platform: "browser",
  outfile: join(WWW, "billing.js"),
  logLevel: "info",
});

// 2) inject config + billing into the frontend
let html = readFileSync(join(ROOT, "static", "index.html"), "utf8");
const inject =
  `<script>window.SNAPCAL_API_BASE=${JSON.stringify(API_BASE)};</script>\n` +
  `<script src="billing.js"></script>\n`;
html = html.includes("</head>") ? html.replace("</head>", inject + "</head>") : inject + html;
writeFileSync(join(WWW, "index.html"), html);

console.log(`Built www/  (API_BASE = ${API_BASE || "(same-origin — set SNAPCAL_API_BASE!)"})`);
