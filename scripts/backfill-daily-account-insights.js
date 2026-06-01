/**
 * Backfill ig_account_insights `period='day'` rows for the past N days.
 *
 * The IG Insights API requires single-day windows for daily granularity
 * (multi-day windows aggregate; metric_type=time_series is not supported for
 * views/reach/profile_views). So we iterate calendar days and call once per
 * (metric, breakdown, day).
 *
 * Run: node scripts/backfill-daily-account-insights.js [DAYS_BACK=30]
 * Idempotent: INSERT OR REPLACE keyed on (account, metric, period, dim, value, end_time)
 * where end_time is each day's midnight UTC, so re-runs overwrite same rows.
 */
const https = require("https");
const fs = require("fs");
const path = require("path");
const Database = require("better-sqlite3");

const ROOT = path.join(__dirname, "..");
const envFile = path.join(ROOT, ".env");
if (fs.existsSync(envFile)) {
  for (const line of fs.readFileSync(envFile, "utf-8").split("\n")) {
    const [k, ...v] = line.split("=");
    if (k && v.length) process.env[k.trim()] = v.join("=").trim();
  }
}

const DAYS_BACK = parseInt(process.argv[2] || "30", 10);
const API_VERSION = "v25.0";
const BASE_URL = `https://graph.facebook.com/${API_VERSION}`;
const DB_PATH = path.join(ROOT, "peacegrappler.db");

let TOKEN = process.env.TOKEN;

const ACCOUNT_METRICS_SIMPLE = [
  "profile_views",
  "website_clicks",
  "accounts_engaged",
  "replies",
  "reposts",
];

const ACCOUNT_METRICS_BREAKDOWN = [
  { metric: "reach", breakdown: "media_product_type" },
  { metric: "reach", breakdown: "follow_type" },
  { metric: "views", breakdown: "media_product_type" },
  { metric: "views", breakdown: "follow_type" },
  { metric: "likes", breakdown: "media_product_type" },
  { metric: "comments", breakdown: "media_product_type" },
  { metric: "shares", breakdown: "media_product_type" },
  { metric: "saves", breakdown: "media_product_type" },
  { metric: "total_interactions", breakdown: "media_product_type" },
  { metric: "follows_and_unfollows", breakdown: "follow_type" },
  { metric: "profile_links_taps", breakdown: "contact_button_type" },
];

// Also pull the no-breakdown view/reach for the dedup row.
const ACCOUNT_METRICS_NOBREAK = ["views", "reach", "likes", "comments", "shares", "saves", "total_interactions"];

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function httpsGetJson(url) {
  return new Promise((resolve, reject) => {
    https.get(url, (res) => {
      let buf = "";
      res.on("data", (d) => (buf += d));
      res.on("end", () => {
        try { resolve(JSON.parse(buf)); } catch (e) { reject(new Error("bad JSON: " + buf.slice(0, 200))); }
      });
    }).on("error", reject);
  });
}

async function graphGet(endpoint, params) {
  const qs = new URLSearchParams({ ...params, access_token: TOKEN }).toString();
  const j = await httpsGetJson(`${BASE_URL}${endpoint}?${qs}`);
  if (j.error) {
    const e = new Error(j.error.message);
    e.code = j.error.code;
    throw e;
  }
  return j;
}

async function getPageToken() {
  const pages = await graphGet(`/me/accounts`, {});
  const pg = pages?.data?.[0];
  if (!pg?.access_token) throw new Error("No page token");
  TOKEN = pg.access_token;
}

function midnightUTC(d) { return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate())); }

async function backfillDay(accountId, dayDate, upsert) {
  const since = Math.floor(dayDate.getTime() / 1000);
  const until = since + 86400;
  const endTime = dayDate.toISOString().slice(0, 19) + "Z"; // "2026-05-28T00:00:00Z"
  const dayLabel = dayDate.toISOString().slice(0, 10);
  let count = 0;
  let errors = 0;

  // Simple no-breakdown metrics
  for (const metric of ACCOUNT_METRICS_SIMPLE) {
    try {
      const r = await graphGet(`/${accountId}/insights`, {
        metric, period: "day", metric_type: "total_value", since, until,
      });
      const v = r.data?.[0]?.total_value?.value;
      if (v !== undefined) {
        upsert.run(accountId, metric, "day", null, null, v, endTime);
        count++;
      }
    } catch (e) {
      errors++;
      if (e.code !== 100) process.stderr.write(`  ✗ ${dayLabel} ${metric}: ${e.message.slice(0, 80)}\n`);
    }
    await sleep(80);
  }

  // No-breakdown view of metrics that normally come with breakdowns —
  // this is the deduplicated account-wide row we use for reach.
  for (const metric of ACCOUNT_METRICS_NOBREAK) {
    try {
      const r = await graphGet(`/${accountId}/insights`, {
        metric, period: "day", metric_type: "total_value", since, until,
      });
      const v = r.data?.[0]?.total_value?.value;
      if (v !== undefined) {
        upsert.run(accountId, metric, "day", null, null, v, endTime);
        count++;
      }
    } catch (e) {
      errors++;
      if (e.code !== 100) process.stderr.write(`  ✗ ${dayLabel} ${metric} (nobreak): ${e.message.slice(0, 80)}\n`);
    }
    await sleep(80);
  }

  // Metrics with breakdowns
  for (const { metric, breakdown } of ACCOUNT_METRICS_BREAKDOWN) {
    try {
      const r = await graphGet(`/${accountId}/insights`, {
        metric, period: "day", metric_type: "total_value", breakdown, since, until,
      });
      const tv = r.data?.[0]?.total_value;
      const bks = tv?.breakdowns?.[0]?.results || [];
      if (bks.length) {
        for (const b of bks) {
          const dimVal = b.dimension_values?.[0] || "unknown";
          upsert.run(accountId, metric, "day", breakdown, dimVal, b.value, endTime);
          count++;
        }
      } else if (tv?.value !== undefined) {
        upsert.run(accountId, metric, "day", breakdown, null, tv.value, endTime);
        count++;
      }
    } catch (e) {
      errors++;
      if (e.code !== 100) process.stderr.write(`  ✗ ${dayLabel} ${metric}/${breakdown}: ${e.message.slice(0, 80)}\n`);
    }
    await sleep(80);
  }

  return { count, errors };
}

async function main() {
  await getPageToken();
  console.log(`Token acquired. Backfilling ${DAYS_BACK} days.`);

  const db = new Database(DB_PATH);
  const accountId = db.prepare("SELECT id FROM ig_accounts LIMIT 1").get().id;
  const upsert = db.prepare(`
    INSERT OR REPLACE INTO ig_account_insights
    (account_id, metric, period, breakdown_dimension, breakdown_value, value, end_time)
    VALUES (?, ?, ?, ?, ?, ?, ?)
  `);

  // Iterate from oldest day forward so logs read top-down.
  const today = midnightUTC(new Date());
  for (let i = DAYS_BACK; i >= 1; i--) {
    const day = new Date(today.getTime() - i * 86400e3);
    const dayLabel = day.toISOString().slice(0, 10);
    process.stdout.write(`${dayLabel}: `);
    const { count, errors } = await backfillDay(accountId, day, upsert);
    process.stdout.write(`${count} rows${errors ? ` (${errors} errors)` : ""}\n`);
  }

  // Now pull monthly-aggregate rows for "unique users" metrics (reach,
  // accounts_engaged). Summing daily values would overcount because each day's
  // reach is unique-within-day but not across days. A single API call with a
  // full-month window returns the deduplicated total.
  console.log("\nFetching monthly aggregates for dedup'd reach/accounts_engaged...");
  const monthsToFetch = new Set();
  for (let i = DAYS_BACK; i >= 1; i--) {
    const d = new Date(today.getTime() - i * 86400e3);
    monthsToFetch.add(`${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`);
  }
  for (const ym of [...monthsToFetch].sort()) {
    const [y, m] = ym.split("-").map(Number);
    const monthStart = Math.floor(Date.UTC(y, m - 1, 1) / 1000);
    // IG caps insights ranges at 30 days. For 31-day months we lose the last day's
    // dedup contribution; that's <= ~3% slop on reach and matches IG's own
    // "May 1 - May 30" calendar default.
    const naturalEnd = Math.floor(Date.UTC(y, m, 1) / 1000);
    const monthEnd = Math.min(naturalEnd, monthStart + 30 * 86400);
    const endTime = `${ym}-01T00:00:00Z`; // store at month-start
    process.stdout.write(`  ${ym}: `);
    let count = 0;
    // No-breakdown reach
    try {
      const r = await graphGet(`/${accountId}/insights`, {
        metric: "reach", period: "day", metric_type: "total_value",
        since: monthStart, until: monthEnd,
      });
      const v = r.data?.[0]?.total_value?.value;
      if (v !== undefined) {
        upsert.run(accountId, "reach", "monthly", null, null, v, endTime);
        count++;
      }
    } catch (e) { process.stderr.write(`reach: ${e.message.slice(0, 60)} `); }
    await sleep(80);
    // Reach with media_product_type breakdown
    try {
      const r = await graphGet(`/${accountId}/insights`, {
        metric: "reach", period: "day", metric_type: "total_value",
        breakdown: "media_product_type", since: monthStart, until: monthEnd,
      });
      const bks = r.data?.[0]?.total_value?.breakdowns?.[0]?.results || [];
      for (const b of bks) {
        upsert.run(accountId, "reach", "monthly", "media_product_type", b.dimension_values[0], b.value, endTime);
        count++;
      }
    } catch (e) { process.stderr.write(`reach/breakdown: ${e.message.slice(0, 60)} `); }
    await sleep(80);
    // accounts_engaged
    try {
      const r = await graphGet(`/${accountId}/insights`, {
        metric: "accounts_engaged", period: "day", metric_type: "total_value",
        since: monthStart, until: monthEnd,
      });
      const v = r.data?.[0]?.total_value?.value;
      if (v !== undefined) {
        upsert.run(accountId, "accounts_engaged", "monthly", null, null, v, endTime);
        count++;
      }
    } catch (e) { process.stderr.write(`engaged: ${e.message.slice(0, 60)} `); }
    await sleep(80);
    process.stdout.write(`${count} rows\n`);
  }

  // Final summary
  const summary = db.prepare(`
    SELECT period, COUNT(*) AS rows, COUNT(DISTINCT date(end_time)) AS distinct_dates
    FROM ig_account_insights WHERE period IN ('day', 'monthly')
    GROUP BY period
  `).all();
  console.log(`\nDB summary:`);
  for (const s of summary) console.log(`  ${s.period}: ${s.rows} rows, ${s.distinct_dates} distinct dates`);
  db.close();
}

main().catch((e) => { console.error("FATAL:", e); process.exit(1); });
