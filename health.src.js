// health.src.js — Apple Health (HealthKit) + Google Fit / Health Connect bridge for the native (Capacitor) build.
// esbuild bundles this into www/health.js, which exposes window.Health.
// On WEB it safely no-ops (no HealthKit/Health Connect in a browser); on iOS/Android it reads real data.
//
// Uses the `capacitor-health` plugin (v8) — verified API: isHealthAvailable / requestHealthPermissions /
// queryAggregated({dataType:'steps'|'active-calories', bucket:'day'}). That plugin currently exposes steps,
// active calories and workouts — so we sync STEPS + ACTIVE CALORIES (the highest-signal movement metrics for
// Coach Cal). Sleep / weight / resting-HR aren't in this plugin; the backend schema already has columns for
// them, so a future plugin can fill them without any API change. Contract the rest of the app depends on:
//   window.Health.pull() -> { date, steps, active_cal, resting_hr, sleep_min, weight }  (nulls where unknown)
import { Capacitor } from "@capacitor/core";
import { Health as HK } from "capacitor-health";   // the native plugin (aliased so it doesn't clash with our window.Health bridge)

const isNative = (Capacitor.isNativePlatform && Capacitor.isNativePlatform())
              || Capacitor.getPlatform() !== "web";

const READ_PERMS = ["READ_STEPS", "READ_ACTIVE_CALORIES", "READ_WORKOUTS"];

async function isAvailable() {
  if (!isNative) return false;
  try { const r = await HK.isHealthAvailable(); return !!(r && r.available); }
  catch (e) { return false; }
}

// Ask the user to grant read access (HealthKit sheet / Health Connect screen). Returns true if it didn't error.
async function connect() {
  if (!isNative) return false;
  try {
    await HK.requestHealthPermissions({ permissions: READ_PERMS });
    localStorage.setItem("snapcal_health_on", "1");
    return true;
  } catch (e) { return false; }
}

function isConnected() { return localStorage.getItem("snapcal_health_on") === "1"; }
function disconnect() { localStorage.removeItem("snapcal_health_on"); }

function _todayBounds() {
  const start = new Date(); start.setHours(0, 0, 0, 0);
  const end = new Date();   end.setHours(23, 59, 59, 999);
  return { startDate: start.toISOString(), endDate: end.toISOString() };
}

// Sum a day's aggregated buckets for a dataType ('steps' | 'active-calories'). Returns a rounded int or null.
async function _dayTotal(dataType) {
  try {
    const r = await HK.queryAggregated({ ..._todayBounds(), dataType, bucket: "day" });
    const arr = (r && r.aggregatedData) || [];
    if (!arr.length) return 0;
    const total = arr.reduce((s, b) => s + (b.value || 0), 0);
    return Math.round(total);
  } catch (e) { return null; }
}

// Pull today's metrics, normalized to the backend (/api/health) + UI contract.
async function pull() {
  if (!isNative) return null;
  const steps = await _dayTotal("steps");
  const active = await _dayTotal("active-calories");
  return {
    date: new Date().toISOString().slice(0, 10),
    steps: steps,
    active_cal: active,
    resting_hr: null,   // not provided by capacitor-health v8
    sleep_min: null,    // "
    weight: null,       // "
  };
}

window.Health = { isNative, isAvailable, connect, disconnect, isConnected, pull };
