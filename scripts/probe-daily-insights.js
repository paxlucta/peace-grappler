/**
 * Probe whether IG's Insights API returns per-day buckets when called with
 * period=day + a multi-day since/until window. Determines backfill strategy:
 *   - if response is one item per day → one bulk call, parse buckets
 *   - if response is one aggregate → must call per-day
 *
 * Run: node scripts/probe-daily-insights.js
 * Requires TOKEN in .env (page-token discovery happens automatically).
 */
const https = require("https");
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const envFile = path.join(ROOT, ".env");
if (fs.existsSync(envFile)) {
  for (const line of fs.readFileSync(envFile, "utf-8").split("\n")) {
    const [k, ...v] = line.split("=");
    if (k && v.length) process.env[k.trim()] = v.join("=").trim();
  }
}

const API_VERSION = "v25.0";
const BASE_URL = `https://graph.facebook.com/${API_VERSION}`;
let TOKEN = process.env.TOKEN;
const ACCOUNT_ID = process.env.INSTAGRAM_ACCOUNT_ID || "17841447891636367";

function httpsGetJson(url) {
  return new Promise((resolve, reject) => {
    https.get(url, (res) => {
      let buf = "";
      res.on("data", (d) => (buf += d));
      res.on("end", () => {
        try { resolve(JSON.parse(buf)); } catch (e) { reject(e); }
      });
    }).on("error", reject);
  });
}

async function graphGet(endpoint, params) {
  const qs = new URLSearchParams({ ...params, access_token: TOKEN }).toString();
  return httpsGetJson(`${BASE_URL}${endpoint}?${qs}`);
}

async function getPageToken() {
  const pages = await graphGet(`/me/accounts`, {});
  const pg = pages?.data?.[0];
  if (pg?.access_token) TOKEN = pg.access_token;
  return pg;
}

async function main() {
  await getPageToken();
  console.log("Page token acquired.\n");

  // Probe: 7-day window, period=day, single metric (no breakdown first to keep it simple).
  const sevenDaysAgo = Math.floor((Date.now() - 7 * 86400e3) / 1000);
  const now = Math.floor(Date.now() / 1000);

  console.log(`Probing /insights?metric=profile_views&period=day&since=${sevenDaysAgo}&until=${now}`);
  console.log(`Window: ${new Date(sevenDaysAgo*1000).toISOString().slice(0,10)} → ${new Date(now*1000).toISOString().slice(0,10)}\n`);

  const r1 = await graphGet(`/${ACCOUNT_ID}/insights`, {
    metric: "profile_views",
    period: "day",
    metric_type: "total_value",
    since: sevenDaysAgo,
    until: now,
  });
  console.log("=== total_value response (simple metric) ===");
  console.log(JSON.stringify(r1, null, 2));

  console.log("\n=== Now try metric_type=time_series ===");
  const r2 = await graphGet(`/${ACCOUNT_ID}/insights`, {
    metric: "profile_views",
    period: "day",
    metric_type: "time_series",
    since: sevenDaysAgo,
    until: now,
  });
  console.log(JSON.stringify(r2, null, 2));

  console.log("\n=== Try views (with breakdown, time_series) ===");
  const r3 = await graphGet(`/${ACCOUNT_ID}/insights`, {
    metric: "views",
    period: "day",
    metric_type: "time_series",
    breakdown: "media_product_type",
    since: sevenDaysAgo,
    until: now,
  });
  console.log(JSON.stringify(r3, null, 2));
}

main().catch((e) => { console.error(e); process.exit(1); });
