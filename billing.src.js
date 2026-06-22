// billing.src.js — RevenueCat in-app purchases for the native (Capacitor) build.
// esbuild bundles this into www/billing.js, which exposes window.Billing.
// On WEB it safely no-ops to the localStorage preview; on iOS/Android it uses RevenueCat.
//
// >>> PASTE YOUR REAL VALUES AFTER CREATING THE REVENUECAT PROJECT <<<
import { Purchases, LOG_LEVEL } from "@revenuecat/purchases-capacitor";
import { Capacitor } from "@capacitor/core";

const RC_APIKEY_IOS     = "appl_REPLACE_ME";   // RevenueCat → Project → API keys (Apple, starts appl_)
const RC_APIKEY_ANDROID = "goog_REPLACE_ME";   // RevenueCat → Project → API keys (Google, starts goog_)
const ENTITLEMENT_ID    = "premium";           // RevenueCat → Entitlements identifier
const DEFAULT_PACKAGE   = "$rc_monthly";       // RevenueCat package id ($rc_monthly / $rc_annual)

const isNative = (Capacitor.isNativePlatform && Capacitor.isNativePlatform())
              || Capacitor.getPlatform() !== "web";

async function init() {
  if (!isNative) return false;                 // web preview — nothing to configure
  await Purchases.setLogLevel({ level: LOG_LEVEL.WARN });
  const apiKey = Capacitor.getPlatform() === "ios" ? RC_APIKEY_IOS : RC_APIKEY_ANDROID;
  await Purchases.configure({ apiKey });        // anonymous user; ties to device until login
  return true;
}

async function isPremium() {
  if (!isNative) return localStorage.getItem("snapcal_premium") === "1";
  try {
    const { customerInfo } = await Purchases.getCustomerInfo();
    return typeof customerInfo.entitlements.active[ENTITLEMENT_ID] !== "undefined";
  } catch (e) { return false; }
}

async function getPackages() {
  if (!isNative) return [];
  const offerings = await Purchases.getOfferings();
  return (offerings.current && offerings.current.availablePackages) || [];
}

async function buy(packageId) {
  if (!isNative) { localStorage.setItem("snapcal_premium", "1"); return true; } // web preview
  const pkgs = await getPackages();
  const pkg = pkgs.find((p) => p.identifier === (packageId || DEFAULT_PACKAGE)) || pkgs[0];
  if (!pkg) throw new Error("No packages — check the RevenueCat Offering + store products.");
  try {
    const { customerInfo } = await Purchases.purchasePackage({ aPackage: pkg });
    return typeof customerInfo.entitlements.active[ENTITLEMENT_ID] !== "undefined";
  } catch (e) {
    if (e && e.userCancelled) return false;
    throw e;
  }
}

async function restore() {
  if (!isNative) return localStorage.getItem("snapcal_premium") === "1";
  const { customerInfo } = await Purchases.restorePurchases(); // Apple REQUIRES a Restore button
  return typeof customerInfo.entitlements.active[ENTITLEMENT_ID] !== "undefined";
}

window.Billing = { isNative, init, isPremium, getPackages, buy, restore };
