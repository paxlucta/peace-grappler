/**
 * PeaceGrappler — Insights Report
 *
 * Recreates the third-party Instagram insights PDF layout as a self-contained
 * HTML page. Pulls from peacegrappler.db (account snapshots, account insights,
 * media insights, demographics, comments).
 *
 * Output: output/peacegrappler-insights.html
 */

const Database = require("better-sqlite3");
const fs = require("fs");
const path = require("path");

const ROOT_DIR = path.join(__dirname, "..");
const DB_PATH = path.join(ROOT_DIR, "peacegrappler.db");
const OUTPUT_PATH = path.join(ROOT_DIR, "output", "peacegrappler-insights.html");
const db = new Database(DB_PATH, { readonly: true });

const ACCOUNT_ID = db.prepare("SELECT id FROM ig_accounts LIMIT 1").get().id;

// ============================================================
// Date window — match the PDF's "last 30 days ending at latest sync"
// ============================================================

const latestSnap = db
  .prepare("SELECT MAX(snapshot_date) AS d FROM ig_account_snapshots")
  .get();
const END_DATE = latestSnap?.d || new Date().toISOString().slice(0, 10);
const endDt = new Date(END_DATE + "T00:00:00Z");
const startDt = new Date(endDt.getTime() - 29 * 86400000); // 30 days inclusive
const START_DATE = startDt.toISOString().slice(0, 10);

const prevEndDt = new Date(startDt.getTime() - 86400000);
const prevStartDt = new Date(prevEndDt.getTime() - 29 * 86400000);
const PREV_START_DATE = prevStartDt.toISOString().slice(0, 10);
const PREV_END_DATE = prevEndDt.toISOString().slice(0, 10);

// ============================================================
// Helpers
// ============================================================

const esc = (s) =>
  String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

const fmtK = (n) => {
  if (n == null) return "0";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return Number(n).toLocaleString();
};

const fmtNum = (n) => (n == null ? "0" : Number(n).toLocaleString());

const fmtDateLong = (iso) =>
  new Date(iso + "T00:00:00Z").toLocaleDateString("en-US", {
    month: "short",
    day: "2-digit",
    year: "numeric",
    timeZone: "UTC",
  });

const fmtDateShort = (iso) =>
  new Date(iso + "T00:00:00Z").toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });

const truncate = (s, n) => {
  if (!s) return "";
  return s.length > n ? s.slice(0, n) + "…" : s;
};

const pctChange = (cur, prev) => {
  if (!prev) return null;
  return Math.round(((cur - prev) / prev) * 100);
};

// ============================================================
// Account insights helpers
//
// The Graph API stores reach/views/total_interactions only with a breakdown
// dimension (no "raw" total row), so totals must be summed across breakdowns.
// For each metric/breakdown we prefer a days_28 snapshot if one exists,
// otherwise sum the daily values across the window.
// ============================================================

function metricBreakdown(metric, breakdown, start = START_DATE, end = END_DATE) {
  const days28 = db
    .prepare(
      `SELECT breakdown_value, value FROM ig_account_insights
       WHERE account_id = ? AND metric = ? AND period = 'days_28'
         AND breakdown_dimension = ?
         AND breakdown_value IS NOT NULL AND breakdown_value <> ''
         AND end_time = (
           SELECT MAX(end_time) FROM ig_account_insights
           WHERE account_id = ? AND metric = ? AND period = 'days_28' AND breakdown_dimension = ?
         )`
    )
    .all(ACCOUNT_ID, metric, breakdown, ACCOUNT_ID, metric, breakdown);

  if (days28.length) {
    const out = {};
    for (const r of days28) out[r.breakdown_value] = r.value;
    return out;
  }

  const rows = db
    .prepare(
      `SELECT breakdown_value, SUM(value) AS v
       FROM ig_account_insights
       WHERE account_id = ? AND metric = ? AND period = 'day'
         AND breakdown_dimension = ?
         AND breakdown_value IS NOT NULL AND breakdown_value <> ''
         AND date(end_time) BETWEEN ? AND ?
       GROUP BY breakdown_value`
    )
    .all(ACCOUNT_ID, metric, breakdown, start, end);
  const out = {};
  for (const r of rows) out[r.breakdown_value] = r.v || 0;
  return out;
}

function metricTotal(metric, breakdown, start = START_DATE, end = END_DATE) {
  const map = metricBreakdown(metric, breakdown, start, end);
  return Object.values(map).reduce((a, b) => a + b, 0);
}

// ============================================================
// Account header data
// ============================================================

const account = db
  .prepare("SELECT * FROM ig_accounts WHERE id = ?")
  .get(ACCOUNT_ID);

const followersTotal =
  db
    .prepare(
      "SELECT followers_count FROM ig_account_snapshots ORDER BY snapshot_date DESC LIMIT 1"
    )
    .get()?.followers_count || account.followers_count || 0;

const totalReachByProduct = metricBreakdown("reach", "media_product_type");
const totalReachByFollowType = metricBreakdown("reach", "follow_type");
const totalReach = Object.values(totalReachByProduct).reduce((a, b) => a + b, 0);

const totalViewsByProduct = metricBreakdown("views", "media_product_type");
const totalViewsByFollowType = metricBreakdown("views", "follow_type");
const totalViews = Object.values(totalViewsByProduct).reduce((a, b) => a + b, 0);

const interactionsByProduct = metricBreakdown("total_interactions", "media_product_type");
const totalInteractions = Object.values(interactionsByProduct).reduce((a, b) => a + b, 0);

const linkTapsByButton = metricBreakdown("profile_links_taps", "contact_button_type");
const profileLinksTaps = Object.values(linkTapsByButton).reduce((a, b) => a + b, 0);

const interactionRate = totalReach ? (totalInteractions / totalReach) * 100 : 0;

// Previous-period totals for delta arrows (always summed daily; prev period
// won't have a days_28 snapshot taken at that moment in time).
const prevReach = metricTotal("reach", "media_product_type", PREV_START_DATE, PREV_END_DATE);
const prevViews = metricTotal("views", "media_product_type", PREV_START_DATE, PREV_END_DATE);
const prevInteractions = metricTotal("total_interactions", "media_product_type", PREV_START_DATE, PREV_END_DATE);
const prevLinkTaps = metricTotal("profile_links_taps", "contact_button_type", PREV_START_DATE, PREV_END_DATE);

const reachDelta = pctChange(totalReach, prevReach);
const viewsDelta = pctChange(totalViews, prevViews);
const interactionsDelta = pctChange(totalInteractions, prevInteractions);
const linkTapsDelta = pctChange(profileLinksTaps, prevLinkTaps);

// ============================================================
// Follower growth trend
// ============================================================

const snapshotRows = db
  .prepare(
    `SELECT snapshot_date, followers_count
     FROM ig_account_snapshots
     WHERE snapshot_date BETWEEN date(?, '-1 day') AND ?
     ORDER BY snapshot_date`
  )
  .all(START_DATE, END_DATE);

const followerTrend = [];
for (let i = 1; i < snapshotRows.length; i++) {
  const diff = snapshotRows[i].followers_count - snapshotRows[i - 1].followers_count;
  followerTrend.push({ date: snapshotRows[i].snapshot_date, value: Math.max(0, diff) });
}
const newFollowersTotal = followerTrend.reduce((a, b) => a + b.value, 0);

// ============================================================
// Demographics (latest follower_demographics snapshot)
// ============================================================

// Each dimension (city, country, age, gender) is written in a separate API call,
// so they end up with different fetched_at timestamps even within one sync run.
// Pick the latest fetched_at per dimension rather than a single global MAX.
function demoRows(dim) {
  return db
    .prepare(
      `SELECT dimension_value, value FROM ig_audience_demographics
       WHERE account_id = ? AND metric = 'follower_demographics'
         AND timeframe = 'last_30_days' AND dimension = ?
         AND fetched_at = (
           SELECT MAX(fetched_at) FROM ig_audience_demographics
           WHERE account_id = ? AND metric = 'follower_demographics'
             AND timeframe = 'last_30_days' AND dimension = ?
         )
       ORDER BY value DESC`
    )
    .all(ACCOUNT_ID, dim, ACCOUNT_ID, dim);
}

const genderRows = demoRows("gender");
const ageRows = demoRows("age");
const cityRows = demoRows("city").slice(0, 8);
const countryRows = demoRows("country").slice(0, 8);

// PDF gender labels: M -> Male, F -> Female, U -> Unspecified
const genderTotals = { Male: 0, Female: 0, Unspecified: 0 };
for (const r of genderRows) {
  if (r.dimension_value === "M") genderTotals.Male = r.value;
  else if (r.dimension_value === "F") genderTotals.Female = r.value;
  else genderTotals.Unspecified += r.value;
}
const genderSum = Object.values(genderTotals).reduce((a, b) => a + b, 0) || 1;

// Age × gender — IG only provides total per age bucket (gender breakdown only at top level).
// We approximate by applying overall gender ratio to each age bucket so the chart matches the PDF.
const ageBuckets = ["13-17", "18-24", "25-34", "35-44", "45-54", "55-64", "65+"];
const ageMap = {};
for (const r of ageRows) ageMap[r.dimension_value] = r.value;
const malePct = genderTotals.Male / genderSum;
const femalePct = genderTotals.Female / genderSum;
const unkPct = genderTotals.Unspecified / genderSum;
const ageDist = ageBuckets.map((b) => ({
  bucket: b,
  male: Math.round((ageMap[b] || 0) * malePct),
  female: Math.round((ageMap[b] || 0) * femalePct),
  unknown: Math.round((ageMap[b] || 0) * unkPct),
}));

// ============================================================
// Online activity heatmap — derived from comment timestamps in window
// ============================================================

const commentTimes = db
  .prepare(
    `SELECT c.timestamp AS timestamp FROM ig_comments c
     JOIN ig_media m ON m.id = c.media_id
     WHERE m.account_id = ?
       AND substr(c.timestamp, 1, 10) BETWEEN ? AND ?`
  )
  .all(ACCOUNT_ID, START_DATE, END_DATE);

// 7 days × 24 hours grid (UTC). 0 = Mon ... 6 = Sun (PDF order)
const heatmap = Array.from({ length: 7 }, () => Array(24).fill(0));
for (const c of commentTimes) {
  const d = new Date(c.timestamp);
  if (isNaN(d)) continue;
  const dow = (d.getUTCDay() + 6) % 7; // Mon=0
  heatmap[dow][d.getUTCHours()]++;
}
let heatMax = 0;
for (const row of heatmap) for (const v of row) if (v > heatMax) heatMax = v;

// ============================================================
// Posts (FEED + CAROUSEL) and Reels — pulled with latest insight values
// ============================================================

const mediaInWindow = db.prepare(`
  SELECT m.id, m.caption, m.media_product_type, m.media_type, m.timestamp,
         m.permalink, m.thumbnail_url, m.media_url, m.like_count, m.comments_count,
         MAX(CASE WHEN i.metric='reach' THEN i.value END) AS reach,
         MAX(CASE WHEN i.metric='views' THEN i.value END) AS views,
         MAX(CASE WHEN i.metric='total_interactions' THEN i.value END) AS interactions,
         MAX(CASE WHEN i.metric='likes' THEN i.value END) AS likes,
         MAX(CASE WHEN i.metric='comments' THEN i.value END) AS comments,
         MAX(CASE WHEN i.metric='shares' THEN i.value END) AS shares,
         MAX(CASE WHEN i.metric='saved' THEN i.value END) AS saves
  FROM ig_media m
  LEFT JOIN (
    SELECT media_id, metric, value,
      ROW_NUMBER() OVER (PARTITION BY media_id, metric ORDER BY fetched_at DESC) AS rn
    FROM ig_media_insights
  ) i ON i.media_id = m.id AND i.rn = 1
  WHERE m.account_id = ?
    AND substr(m.timestamp, 1, 10) BETWEEN ? AND ?
  GROUP BY m.id
  ORDER BY m.timestamp DESC
`);

const allMedia = mediaInWindow.all(ACCOUNT_ID, START_DATE, END_DATE);
const posts = allMedia.filter((m) => m.media_product_type !== "REELS" && m.media_product_type !== "STORY");
const reels = allMedia.filter((m) => m.media_product_type === "REELS");

function sumMedia(arr, key) {
  return arr.reduce((a, b) => a + (b[key] || 0), 0);
}

const postsAgg = {
  count: posts.length,
  reach: sumMedia(posts, "reach"),
  views: sumMedia(posts, "views"),
  interactions: sumMedia(posts, "interactions"),
  likes: sumMedia(posts, "likes"),
  comments: sumMedia(posts, "comments"),
  shares: sumMedia(posts, "shares"),
  saves: sumMedia(posts, "saves"),
};
postsAgg.rate = postsAgg.reach ? (postsAgg.interactions / postsAgg.reach) * 100 : 0;

const reelsAgg = {
  count: reels.length,
  reach: sumMedia(reels, "reach"),
  views: sumMedia(reels, "views"),
  interactions: sumMedia(reels, "interactions"),
  likes: sumMedia(reels, "likes"),
  comments: sumMedia(reels, "comments"),
  shares: sumMedia(reels, "shares"),
  saves: sumMedia(reels, "saves"),
};
reelsAgg.rate = reelsAgg.reach ? (reelsAgg.interactions / reelsAgg.reach) * 100 : 0;

// Previous period for delta arrows
const prevMedia = mediaInWindow.all(ACCOUNT_ID, PREV_START_DATE, PREV_END_DATE);
const prevPosts = prevMedia.filter((m) => m.media_product_type !== "REELS" && m.media_product_type !== "STORY");
const prevReels = prevMedia.filter((m) => m.media_product_type === "REELS");

function deltaPair(curArr, prevArr, key, isCount) {
  const cur = isCount ? curArr.length : sumMedia(curArr, key);
  const prev = isCount ? prevArr.length : sumMedia(prevArr, key);
  return pctChange(cur, prev);
}

const postsDelta = {
  count: deltaPair(posts, prevPosts, null, true),
  reach: deltaPair(posts, prevPosts, "reach"),
  views: deltaPair(posts, prevPosts, "views"),
  interactions: deltaPair(posts, prevPosts, "interactions"),
  likes: deltaPair(posts, prevPosts, "likes"),
  comments: deltaPair(posts, prevPosts, "comments"),
  shares: deltaPair(posts, prevPosts, "shares"),
  saves: deltaPair(posts, prevPosts, "saves"),
};
const reelsDelta = {
  count: deltaPair(reels, prevReels, null, true),
  reach: deltaPair(reels, prevReels, "reach"),
  views: deltaPair(reels, prevReels, "views"),
  interactions: deltaPair(reels, prevReels, "interactions"),
  likes: deltaPair(reels, prevReels, "likes"),
  comments: deltaPair(reels, prevReels, "comments"),
  shares: deltaPair(reels, prevReels, "shares"),
  saves: deltaPair(reels, prevReels, "saves"),
};

// Per-day series for trend charts
function dailySeries(arr, key) {
  const buckets = {};
  for (let d = new Date(startDt); d <= endDt; d = new Date(d.getTime() + 86400000)) {
    buckets[d.toISOString().slice(0, 10)] = 0;
  }
  for (const m of arr) {
    const day = m.timestamp.slice(0, 10);
    if (day in buckets) buckets[day] += m[key] || 0;
  }
  return Object.entries(buckets).map(([date, value]) => ({ date, value }));
}

const postsReachSeries = dailySeries(posts, "reach");
const postsViewsSeries = dailySeries(posts, "views");
const postsLikesSeries = dailySeries(posts, "likes");
const postsCommentsSeries = dailySeries(posts, "comments");
const postsSavesSeries = dailySeries(posts, "saves");
const postsSharesSeries = dailySeries(posts, "shares");

const reelsReachSeries = dailySeries(reels, "reach");
const reelsViewsSeries = dailySeries(reels, "views");
const reelsLikesSeries = dailySeries(reels, "likes");
const reelsCommentsSeries = dailySeries(reels, "comments");
const reelsSavesSeries = dailySeries(reels, "saves");
const reelsSharesSeries = dailySeries(reels, "shares");

// ============================================================
// Hashtag performance — extracted from captions
// ============================================================

function hashtagStats(arr) {
  const tags = {};
  for (const m of arr) {
    if (!m.caption) continue;
    const found = m.caption.match(/#[A-Za-z0-9_]+/g) || [];
    const seen = new Set();
    for (const raw of found) {
      const tag = raw.slice(1);
      if (seen.has(tag)) continue; // count each post once per tag
      seen.add(tag);
      if (!tags[tag]) tags[tag] = { count: 0, reach: 0, interactions: 0 };
      tags[tag].count++;
      tags[tag].reach += m.reach || 0;
      tags[tag].interactions += m.interactions || 0;
    }
  }
  return Object.entries(tags)
    .map(([tag, v]) => ({
      tag,
      count: v.count,
      avgReach: v.count ? Math.round(v.reach / v.count) : 0,
      avgInteractions: v.count ? Math.round(v.interactions / v.count) : 0,
    }))
    .sort((a, b) => b.count - a.count || b.avgReach - a.avgReach);
}

const postHashtags = hashtagStats(posts).slice(0, 15);
const reelHashtags = hashtagStats(reels).slice(0, 15);

// ============================================================
// HTML
// ============================================================

const generatedAt = new Date().toLocaleString("en-US", {
  dateStyle: "medium",
  timeStyle: "short",
});

const dateRangeLabel = `${fmtDateLong(START_DATE)} to ${fmtDateLong(END_DATE)}`;

function deltaTag(d, suffix = "in last 30 days") {
  if (d == null) return "";
  const cls = d > 0 ? "up" : d < 0 ? "down" : "flat";
  const arrow = d > 0 ? "↑" : d < 0 ? "↓" : "";
  return `<div class="delta ${cls}">${arrow} ${Math.abs(d)}% <span class="delta-sub">${suffix}</span></div>`;
}

function metricCard(label, value, delta, sub = "") {
  return `
  <div class="metric-card">
    <div class="metric-label">${label}</div>
    <div class="metric-value">${value}</div>
    ${delta != null ? deltaTag(delta) : sub ? `<div class="delta-sub">${sub}</div>` : ""}
  </div>`;
}

function thumbHtml(m) {
  const url = m.thumbnail_url || m.media_url;
  if (!url) return `<div class="thumb-placeholder"></div>`;
  return `<img class="thumb" src="${esc(url)}" alt="" loading="lazy" onerror="this.style.display='none'">`;
}

function mediaRow(m, includeViews) {
  const cap = truncate(m.caption || "(no caption)", 36);
  const dt = new Date(m.timestamp).toLocaleString("en-US", {
    month: "short",
    day: "2-digit",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
  const link = m.permalink
    ? `<a href="${esc(m.permalink)}" target="_blank" rel="noopener" class="ext">↗</a>`
    : "";
  return `<tr>
    <td class="media-cell">
      ${thumbHtml(m)}
      <div class="media-meta">
        <div class="media-cap">${esc(cap)} ${link}</div>
        <div class="media-time">${esc(dt)}</div>
      </div>
    </td>
    <td>${fmtK(m.reach)}</td>
    ${includeViews ? `<td>${fmtK(m.views)}</td>` : ""}
    <td>${fmtNum(m.interactions)}</td>
    <td>${fmtNum(m.likes)}</td>
    <td>${fmtNum(m.comments)}</td>
    <td>${fmtNum(m.saves)}</td>
    <td>${fmtNum(m.shares)}</td>
  </tr>`;
}

const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PeaceGrappler — Insights</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #ffffff;
    --page: #fafafa;
    --card: #ffffff;
    --border: #e5e7eb;
    --text: #111827;
    --muted: #6b7280;
    --muted-2: #9ca3af;
    --accent: #6366f1;
    --accent-soft: #c7d2fe;
    --pink: #ec4899;
    --pink-soft: #fbcfe8;
    --green: #10b981;
    --red: #ef4444;
    --yellow: #f59e0b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--page); color: var(--text); line-height: 1.5; font-size: 14px; }
  .container { max-width: 1080px; margin: 0 auto; padding: 24px 24px 64px; }

  .topbar { display: flex; justify-content: space-between; align-items: center; padding-bottom: 12px; border-bottom: 1px solid var(--border); margin-bottom: 16px; }
  .topbar a { color: var(--accent); text-decoration: none; font-size: 13px; font-weight: 600; }
  .topbar .date-range { color: var(--muted); font-size: 13px; }

  .profile-row { display: flex; align-items: center; gap: 14px; padding: 14px 18px; border: 1px solid var(--border); border-radius: 12px; background: var(--card); margin-bottom: 24px; }
  .profile-row .name { font-weight: 700; font-size: 15px; }
  .profile-row .handle { color: var(--muted); font-size: 13px; }

  h1 { font-size: 26px; font-weight: 700; margin: 24px 0 16px; }
  h2 { font-size: 16px; font-weight: 600; margin-bottom: 4px; }
  .section-desc { color: var(--muted); font-size: 13px; margin-bottom: 14px; }

  .grid { display: grid; gap: 12px; margin-bottom: 16px; }
  .grid-4 { grid-template-columns: repeat(4, 1fr); }
  .grid-2 { grid-template-columns: repeat(2, 1fr); }
  .grid-3 { grid-template-columns: repeat(3, 1fr); }

  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 16px; }

  .metric-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; min-height: 120px; display: flex; flex-direction: column; justify-content: space-between; }
  .metric-label { color: var(--muted); font-size: 13px; }
  .metric-value { font-size: 32px; font-weight: 600; margin: 6px 0; color: var(--text); letter-spacing: -0.5px; }
  .delta { font-size: 12px; font-weight: 600; }
  .delta.up { color: var(--green); }
  .delta.down { color: var(--red); }
  .delta.flat { color: var(--muted); }
  .delta-sub { color: var(--muted); font-weight: 400; }

  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 16px; }
  .chart-card .legend-row { display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
  .chart-card .delta-line { color: var(--muted); font-size: 12px; }
  .chart-wrap { position: relative; height: 280px; }
  .chart-wrap.tall { height: 320px; }
  .chart-wrap.short { height: 220px; }

  .legend-item { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); margin-right: 12px; }
  .legend-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 10px 12px; color: var(--muted); font-weight: 500; font-size: 12px; border-bottom: 1px solid var(--border); }
  td { padding: 10px 12px; border-bottom: 1px solid #f3f4f6; vertical-align: middle; }

  .media-cell { display: flex; align-items: center; gap: 10px; min-width: 240px; }
  .thumb { width: 38px; height: 38px; border-radius: 6px; object-fit: cover; background: #f3f4f6; }
  .thumb-placeholder { width: 38px; height: 38px; border-radius: 6px; background: linear-gradient(135deg, #e5e7eb, #d1d5db); }
  .media-meta { display: flex; flex-direction: column; }
  .media-cap { font-size: 13px; color: var(--text); }
  .media-time { font-size: 11px; color: var(--muted); }
  .ext { color: var(--accent); text-decoration: none; font-size: 11px; padding: 0 4px; }

  .demo-row { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; }
  .pie-stats { display: flex; gap: 16px; justify-content: center; margin-top: 8px; }
  .pie-stat { text-align: center; }
  .pie-stat .pct { font-size: 18px; font-weight: 700; }
  .pie-stat .label { font-size: 11px; color: var(--muted); }
  .pie-stat.male .pct { color: var(--accent); }
  .pie-stat.female .pct { color: var(--pink); }
  .pie-stat.unspec .pct { color: var(--green); }

  .heatmap { display: grid; grid-template-columns: 80px repeat(24, 1fr); gap: 2px; font-size: 10px; color: var(--muted); }
  .heatmap .hh { text-align: center; padding: 2px 0; font-size: 10px; }
  .heatmap .day-label { padding: 4px 6px; }
  .heat-cell { height: 18px; border-radius: 2px; background: #f3f4f6; }
  .heatmap-legend { display: flex; align-items: center; gap: 8px; justify-content: flex-end; margin-top: 8px; font-size: 11px; color: var(--muted); }
  .legend-bar { width: 120px; height: 8px; background: linear-gradient(to right, #f3f4f6, var(--accent)); border-radius: 4px; }

  .bar-row { display: flex; align-items: center; gap: 10px; padding: 5px 0; font-size: 12px; }
  .bar-row .label { width: 200px; color: var(--muted); text-align: right; flex-shrink: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bar-row .bar { flex: 1; height: 14px; background: var(--accent-soft); border-radius: 3px; position: relative; }
  .bar-row .bar > span { display: block; height: 100%; background: var(--accent); border-radius: 3px; }
  .bar-row .val { width: 60px; color: var(--text); font-weight: 600; }

  .pdf-note { font-size: 11px; color: var(--muted-2); margin-top: 8px; font-style: italic; }

  @media (max-width: 800px) {
    .grid-4 { grid-template-columns: repeat(2, 1fr); }
    .grid-3 { grid-template-columns: 1fr; }
    .grid-2 { grid-template-columns: 1fr; }
    .demo-row { grid-template-columns: 1fr; }
    .heatmap { grid-template-columns: 60px repeat(24, minmax(8px, 1fr)); font-size: 8px; }
    .bar-row .label { width: 130px; font-size: 11px; }
  }
</style>
</head>
<body>
<div class="container">

  <div class="topbar">
    <a href="index.html">&larr; All Reports</a>
    <div class="date-range">${dateRangeLabel}</div>
  </div>

  <div class="profile-row">
    <div>
      <div class="name">@${esc(account?.username || "peacegrappler_mma")}</div>
      <div class="handle">${esc(account?.name || "")}</div>
    </div>
  </div>

  <h1>Instagram Profile</h1>

  <div class="grid grid-4">
    ${metricCard("Total Followers", fmtK(followersTotal), null, "Lifetime Data")}
    ${metricCard("Total Reach", fmtK(totalReach), reachDelta)}
    ${metricCard("Total Views", fmtK(totalViews), viewsDelta)}
    ${metricCard("Total Interactions", fmtK(totalInteractions), interactionsDelta)}
    ${metricCard("Interaction Rate", interactionRate.toFixed(2) + "%", null)}
    ${metricCard("Profile Link Taps", fmtNum(profileLinksTaps), linkTapsDelta)}
  </div>

  <div class="chart-card">
    <h2>Follower Growth Trend</h2>
    <div class="section-desc">Track followers gained.</div>
    ${followerTrend.length >= 1 ? `
    <div class="chart-wrap"><canvas id="ch_followers"></canvas></div>
    <div class="legend-row">
      <div></div>
      <div><span class="legend-item"><span class="legend-dot" style="background:var(--accent)"></span>New Followers (${newFollowersTotal})</span></div>
    </div>` : `
    <div class="section-desc" style="padding: 24px 0; text-align: center; color: var(--muted, #888);">
      Need at least 2 daily snapshots to chart growth. Have ${snapshotRows.length} snapshot${snapshotRows.length === 1 ? "" : "s"} so far — chart will populate after the next daily sync.
    </div>`}
  </div>

  <div class="chart-card">
    <h2>Reach Insights</h2>
    <div class="section-desc">Discover the number of unique users your content reached.</div>
    <div class="chart-wrap short"><canvas id="ch_reach"></canvas></div>
    <div class="legend-row">
      <div class="delta-line">${reachDelta != null ? `${reachDelta > 0 ? "↑" : "↓"} ${Math.abs(reachDelta)}% Since Previous Period` : "—"}</div>
      <div>
        <span class="legend-item"><span class="legend-dot" style="background:var(--accent)"></span>Follower (${fmtK(totalReachByFollowType.FOLLOWER || 0)})</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--pink)"></span>Non Follower (${fmtK(totalReachByFollowType.NON_FOLLOWER || 0)})</span>
      </div>
    </div>
  </div>

  <div class="chart-card">
    <h2>Views Insights</h2>
    <div class="section-desc">Displays the number of users who have viewed your content.</div>
    <div class="chart-wrap short"><canvas id="ch_views"></canvas></div>
    <div class="legend-row">
      <div class="delta-line">${viewsDelta != null ? `${viewsDelta > 0 ? "↑" : "↓"} ${Math.abs(viewsDelta)}% Since Previous Period` : "—"}</div>
      <div>
        <span class="legend-item"><span class="legend-dot" style="background:var(--accent)"></span>Follower (${fmtK(totalViewsByFollowType.FOLLOWER || 0)})</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--pink)"></span>Non Follower (${fmtK(totalViewsByFollowType.NON_FOLLOWER || 0)})</span>
      </div>
    </div>
  </div>

  <div class="chart-card">
    <h2>Interaction Insights</h2>
    <div class="section-desc">Displays the interaction received on your content.</div>
    <div class="chart-wrap short"><canvas id="ch_interactions"></canvas></div>
    <div class="legend-row">
      <div class="delta-line">${interactionsDelta != null ? `${interactionsDelta > 0 ? "↑" : "↓"} ${Math.abs(interactionsDelta)}% Since Previous Period` : "—"}</div>
      <div>
        <span class="legend-item"><span class="legend-dot" style="background:#6b7280"></span>Ad (${fmtNum(interactionsByProduct.AD || 0)})</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--pink)"></span>Feed (${fmtNum((interactionsByProduct.POST || 0) + (interactionsByProduct.CAROUSEL_CONTAINER || 0))})</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--green)"></span>Reel (${fmtK(interactionsByProduct.REEL || 0)})</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--yellow)"></span>Story (${fmtNum(interactionsByProduct.STORY || 0)})</span>
      </div>
    </div>
  </div>

  <div class="chart-card">
    <div class="demo-row">
      <div>
        <h2>Audience Demographics</h2>
        <div class="section-desc">Analyze your audience demographics by gender and age.</div>
        <div class="chart-wrap"><canvas id="ch_demo"></canvas></div>
      </div>
      <div>
        <h2>Gender Insights</h2>
        <div class="section-desc">Since Previous Period</div>
        <div class="chart-wrap short" style="height:200px"><canvas id="ch_gender"></canvas></div>
        <div class="pie-stats">
          <div class="pie-stat male"><div class="pct">${Math.round((genderTotals.Male / genderSum) * 100)}%</div><div class="label">Male</div></div>
          <div class="pie-stat female"><div class="pct">${Math.round((genderTotals.Female / genderSum) * 100)}%</div><div class="label">Female</div></div>
          <div class="pie-stat unspec"><div class="pct">${Math.round((genderTotals.Unspecified / genderSum) * 100)}%</div><div class="label">Unspecified</div></div>
        </div>
      </div>
    </div>
  </div>

  <div class="grid grid-2">
    <div class="card">
      <h2>Top Cities By Followers</h2>
      <div class="section-desc">Geographical distribution of your followers.</div>
      ${cityRows
        .map(
          (r) => `<div class="bar-row">
        <span class="label">${esc(truncate(r.dimension_value, 32))}</span>
        <span class="bar"><span style="width:${(r.value / (cityRows[0]?.value || 1)) * 100}%"></span></span>
        <span class="val">${fmtNum(r.value)}</span>
      </div>`
        )
        .join("")}
    </div>
    <div class="card">
      <h2>Top Countries By Followers</h2>
      <div class="section-desc">Geographical distribution of your followers.</div>
      ${countryRows
        .map(
          (r) => `<div class="bar-row">
        <span class="label">${esc(r.dimension_value)}</span>
        <span class="bar"><span style="width:${(r.value / (countryRows[0]?.value || 1)) * 100}%"></span></span>
        <span class="val">${fmtNum(r.value)}</span>
      </div>`
        )
        .join("")}
    </div>
  </div>

  <div class="card">
    <h2>Profile Link Taps Insights</h2>
    <div class="section-desc">Track user interaction on the links in your profile.</div>
    ${["BOOK_NOW", "CALL", "DIRECTION", "EMAIL", "TEXT", "INSTANT_EXPERIENCE"]
      .map((k) => {
        const labels = {
          BOOK_NOW: "Book Now",
          CALL: "Call",
          DIRECTION: "Get Directions",
          EMAIL: "Email",
          TEXT: "Text",
          INSTANT_EXPERIENCE: "Instant Experience",
        };
        const v = linkTapsByButton[k] || 0;
        const max = Math.max(...Object.values(linkTapsByButton), 1);
        return `<div class="bar-row">
          <span class="label">${labels[k]}</span>
          <span class="bar"><span style="width:${(v / max) * 100}%"></span></span>
          <span class="val">${fmtNum(v)}</span>
        </div>`;
      })
      .join("")}
    ${linkTapsDelta != null ? `<div class="delta-line" style="margin-top:8px">${linkTapsDelta > 0 ? "↑" : "↓"} ${Math.abs(linkTapsDelta)}% Since Previous Period</div>` : ""}
  </div>

  <div class="chart-card">
    <h2>Followers Online Activity</h2>
    <div class="section-desc">Engagement times derived from comment activity (proxy — IG no longer exposes online_followers).</div>
    <div class="heatmap">
      <div></div>
      ${Array.from({ length: 24 }, (_, h) => `<div class="hh">${h === 0 ? "12 AM" : h < 12 ? `${h} AM` : h === 12 ? "12 PM" : `${h - 12} PM`}</div>`).join("")}
      ${["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        .map(
          (d, i) =>
            `<div class="day-label">${d}</div>` +
            heatmap[i]
              .map((v) => {
                const a = heatMax ? v / heatMax : 0;
                return `<div class="heat-cell" style="background: rgba(99,102,241,${a.toFixed(2)})" title="${v} comments"></div>`;
              })
              .join("")
        )
        .join("")}
    </div>
    <div class="heatmap-legend"><span>Lower</span><span class="legend-bar"></span><span>Higher</span></div>
  </div>

  <h1>Instagram Post</h1>

  <div class="grid grid-4">
    ${metricCard("Post Published", fmtNum(postsAgg.count), postsDelta.count)}
    ${metricCard("Post Reach", fmtK(postsAgg.reach), postsDelta.reach)}
    ${metricCard("Post Views", fmtK(postsAgg.views), postsDelta.views)}
    ${metricCard("Post Interactions", fmtNum(postsAgg.interactions), postsDelta.interactions)}
    ${metricCard("Post Interaction Rate", postsAgg.rate.toFixed(2) + "%", null)}
    ${metricCard("Post Comments", fmtNum(postsAgg.comments), postsDelta.comments)}
    ${metricCard("Post Saves", fmtNum(postsAgg.saves), postsDelta.saves)}
    ${metricCard("Post Shares", fmtNum(postsAgg.shares), postsDelta.shares)}
    ${metricCard("Post Likes", fmtNum(postsAgg.likes), postsDelta.likes)}
  </div>

  <div class="chart-card">
    <h2>Post Reach &amp; Views Trend</h2>
    <div class="section-desc">Daily reach and views of posts over time.</div>
    <div class="chart-wrap"><canvas id="ch_post_trend"></canvas></div>
    <div class="legend-row">
      <div></div>
      <div>
        <span class="legend-item"><span class="legend-dot" style="background:var(--accent)"></span>Reach (${fmtK(postsAgg.reach)})</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--pink)"></span>Views (${fmtK(postsAgg.views)})</span>
      </div>
    </div>
  </div>

  <div class="chart-card">
    <h2>Post Interaction Trend</h2>
    <div class="section-desc">Likes, comments, saves and shares over time.</div>
    <div class="chart-wrap"><canvas id="ch_post_int"></canvas></div>
    <div class="legend-row">
      <div></div>
      <div>
        <span class="legend-item"><span class="legend-dot" style="background:var(--accent)"></span>Likes (${fmtNum(postsAgg.likes)})</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--pink)"></span>Comments (${fmtNum(postsAgg.comments)})</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--green)"></span>Saves (${fmtNum(postsAgg.saves)})</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--yellow)"></span>Shares (${fmtNum(postsAgg.shares)})</span>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Post Performance</h2>
    <div class="section-desc">Performance of your posts across all engagement metrics.</div>
    <table>
      <thead><tr><th>Posts</th><th>Reach</th><th>Interactions</th><th>Likes</th><th>Comments</th><th>Saves</th><th>Shares</th></tr></thead>
      <tbody>${posts.map((m) => mediaRow(m, false)).join("")}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>Post Hashtag Performance</h2>
    <div class="section-desc">Hashtag performance with average reach and interaction.</div>
    <table>
      <thead><tr><th>Hashtag</th><th>Post Count</th><th>Average Reach</th><th>Average Interaction</th></tr></thead>
      <tbody>
        ${postHashtags
          .map(
            (h) =>
              `<tr><td>${esc(h.tag)}</td><td>${h.count}</td><td>${fmtK(h.avgReach)}</td><td>${fmtNum(h.avgInteractions)}</td></tr>`
          )
          .join("") || `<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:20px">No hashtags found.</td></tr>`}
      </tbody>
    </table>
  </div>

  <h1>Instagram Reels</h1>

  <div class="grid grid-4">
    ${metricCard("Reels Published", fmtNum(reelsAgg.count), reelsDelta.count)}
    ${metricCard("Reels Reach", fmtK(reelsAgg.reach), reelsDelta.reach)}
    ${metricCard("Reels Interactions", fmtK(reelsAgg.interactions), reelsDelta.interactions)}
    ${metricCard("Reels Views", fmtK(reelsAgg.views), reelsDelta.views)}
    ${metricCard("Reel Interaction Rate", reelsAgg.rate.toFixed(2) + "%", null)}
    ${metricCard("Reels Saves", fmtNum(reelsAgg.saves), reelsDelta.saves)}
    ${metricCard("Reels Comments", fmtK(reelsAgg.comments), reelsDelta.comments)}
    ${metricCard("Reels Shares", fmtNum(reelsAgg.shares), reelsDelta.shares)}
    ${metricCard("Reels Likes", fmtK(reelsAgg.likes), reelsDelta.likes)}
  </div>

  <div class="chart-card">
    <h2>Reels Reach &amp; Views Trend</h2>
    <div class="section-desc">Daily reach and views of reels over time.</div>
    <div class="chart-wrap"><canvas id="ch_reel_trend"></canvas></div>
    <div class="legend-row">
      <div></div>
      <div>
        <span class="legend-item"><span class="legend-dot" style="background:var(--accent)"></span>Reach (${fmtK(reelsAgg.reach)})</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--pink)"></span>Views (${fmtK(reelsAgg.views)})</span>
      </div>
    </div>
  </div>

  <div class="chart-card">
    <h2>Reels Interactions Trend</h2>
    <div class="section-desc">Likes, saves, comments, and shares over time.</div>
    <div class="chart-wrap"><canvas id="ch_reel_int"></canvas></div>
    <div class="legend-row">
      <div></div>
      <div>
        <span class="legend-item"><span class="legend-dot" style="background:var(--accent)"></span>Likes (${fmtK(reelsAgg.likes)})</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--pink)"></span>Comments (${fmtK(reelsAgg.comments)})</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--green)"></span>Saves (${fmtNum(reelsAgg.saves)})</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--yellow)"></span>Shares (${fmtNum(reelsAgg.shares)})</span>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Reels Performance</h2>
    <div class="section-desc">Compare performance metrics of your reels.</div>
    <table>
      <thead><tr><th>Reels</th><th>Reach</th><th>Views</th><th>Interactions</th><th>Likes</th><th>Comments</th><th>Saves</th><th>Shares</th></tr></thead>
      <tbody>${reels.map((m) => mediaRow(m, true)).join("")}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>Reel Hashtag Performance</h2>
    <div class="section-desc">Hashtag performance with average reach and interaction.</div>
    <table>
      <thead><tr><th>Hashtag</th><th>Reels Count</th><th>Average Reach</th><th>Average Interaction</th></tr></thead>
      <tbody>
        ${reelHashtags
          .map(
            (h) =>
              `<tr><td>${esc(h.tag)}</td><td>${h.count}</td><td>${fmtK(h.avgReach)}</td><td>${fmtNum(h.avgInteractions)}</td></tr>`
          )
          .join("") || `<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:20px">No hashtags found.</td></tr>`}
      </tbody>
    </table>
  </div>

  <div style="text-align:center;color:var(--muted);font-size:11px;margin-top:32px">
    Generated ${esc(generatedAt)} · Data source: peacegrappler.db
  </div>

</div>

<script>
const FOLLOWER_TREND = ${JSON.stringify(followerTrend)};
const REACH_BREAKDOWN = ${JSON.stringify(totalReachByProduct)};
const VIEWS_BREAKDOWN = ${JSON.stringify(totalViewsByProduct)};
const INT_BREAKDOWN = ${JSON.stringify(interactionsByProduct)};
const AGE_DIST = ${JSON.stringify(ageDist)};
const GENDER_TOTALS = ${JSON.stringify(genderTotals)};
const POST_REACH = ${JSON.stringify(postsReachSeries)};
const POST_VIEWS = ${JSON.stringify(postsViewsSeries)};
const POST_LIKES = ${JSON.stringify(postsLikesSeries)};
const POST_COMMENTS = ${JSON.stringify(postsCommentsSeries)};
const POST_SAVES = ${JSON.stringify(postsSavesSeries)};
const POST_SHARES = ${JSON.stringify(postsSharesSeries)};
const REEL_REACH = ${JSON.stringify(reelsReachSeries)};
const REEL_VIEWS = ${JSON.stringify(reelsViewsSeries)};
const REEL_LIKES = ${JSON.stringify(reelsLikesSeries)};
const REEL_COMMENTS = ${JSON.stringify(reelsCommentsSeries)};
const REEL_SAVES = ${JSON.stringify(reelsSavesSeries)};
const REEL_SHARES = ${JSON.stringify(reelsSharesSeries)};

const C = { accent: "#6366f1", pink: "#ec4899", green: "#10b981", yellow: "#f59e0b", grid: "#e5e7eb", muted: "#9ca3af" };

function shortDate(iso) { const d = new Date(iso + "T00:00:00Z"); return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }); }

const baseLine = {
  responsive: true, maintainAspectRatio: false,
  plugins: { legend: { display: false } },
  scales: {
    x: { grid: { display: false }, ticks: { color: C.muted, autoSkip: true, maxTicksLimit: 8 } },
    y: { grid: { color: C.grid, drawBorder: false }, ticks: { color: C.muted } }
  }
};

function lineDataset(label, color, points) {
  return {
    label, data: points.map(p => p.value),
    borderColor: color, backgroundColor: color + "33",
    tension: 0.35, borderWidth: 2, pointRadius: 2.5, pointBackgroundColor: color, fill: false,
  };
}

new Chart(document.getElementById("ch_followers"), {
  type: "line",
  data: { labels: FOLLOWER_TREND.map(p => shortDate(p.date)), datasets: [lineDataset("New Followers", C.accent, FOLLOWER_TREND)] },
  options: { ...baseLine, plugins: { legend: { display: false }, tooltip: { mode: "index", intersect: false } } }
});

function horizontalBar(canvasId, breakdown) {
  const labels = ["Ad", "Feed", "Reel", "Story"];
  const map = {
    Ad: breakdown.AD || 0,
    Feed: (breakdown.POST || 0) + (breakdown.CAROUSEL_CONTAINER || 0),
    Reel: breakdown.REEL || 0,
    Story: breakdown.STORY || 0,
  };
  new Chart(document.getElementById(canvasId), {
    type: "bar",
    data: {
      labels,
      datasets: [{
        data: labels.map(l => map[l]),
        backgroundColor: [C.muted, C.pink, C.green, C.yellow],
        borderRadius: 4, barThickness: 22,
      }]
    },
    options: {
      indexAxis: "y", responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: C.grid }, ticks: { color: C.muted } },
        y: { grid: { display: false }, ticks: { color: C.muted } }
      }
    }
  });
}
horizontalBar("ch_reach", REACH_BREAKDOWN);
horizontalBar("ch_views", VIEWS_BREAKDOWN);
horizontalBar("ch_interactions", INT_BREAKDOWN);

new Chart(document.getElementById("ch_demo"), {
  type: "bar",
  data: {
    labels: AGE_DIST.map(a => a.bucket),
    datasets: [
      { label: "Male", data: AGE_DIST.map(a => a.male), backgroundColor: C.accent, borderRadius: 4 },
      { label: "Female", data: AGE_DIST.map(a => a.female), backgroundColor: C.pink, borderRadius: 4 },
      { label: "Unspecified", data: AGE_DIST.map(a => a.unknown), backgroundColor: C.green, borderRadius: 4 },
    ]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: true, position: "bottom", labels: { color: C.muted, boxWidth: 10 } } },
    scales: {
      x: { stacked: false, grid: { display: false }, ticks: { color: C.muted } },
      y: { grid: { color: C.grid }, ticks: { color: C.muted } }
    }
  }
});

new Chart(document.getElementById("ch_gender"), {
  type: "pie",
  data: {
    labels: ["Male", "Female", "Unspecified"],
    datasets: [{
      data: [GENDER_TOTALS.Male, GENDER_TOTALS.Female, GENDER_TOTALS.Unspecified],
      backgroundColor: [C.accent, C.pink, C.green], borderWidth: 0,
    }]
  },
  options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } }
});

function dualLine(canvasId, sA, sB, labelA, labelB, colorA, colorB) {
  new Chart(document.getElementById(canvasId), {
    type: "line",
    data: { labels: sA.map(p => shortDate(p.date)), datasets: [lineDataset(labelA, colorA, sA), lineDataset(labelB, colorB, sB)] },
    options: { ...baseLine, plugins: { legend: { display: false }, tooltip: { mode: "index", intersect: false } } }
  });
}

dualLine("ch_post_trend", POST_REACH, POST_VIEWS, "Reach", "Views", C.accent, C.pink);
dualLine("ch_reel_trend", REEL_REACH, REEL_VIEWS, "Reach", "Views", C.accent, C.pink);

function quadLine(canvasId, sL, sC, sSv, sSh) {
  new Chart(document.getElementById(canvasId), {
    type: "line",
    data: {
      labels: sL.map(p => shortDate(p.date)),
      datasets: [
        lineDataset("Likes", C.accent, sL),
        lineDataset("Comments", C.pink, sC),
        lineDataset("Saves", C.green, sSv),
        lineDataset("Shares", C.yellow, sSh),
      ]
    },
    options: { ...baseLine, plugins: { legend: { display: false }, tooltip: { mode: "index", intersect: false } } }
  });
}

quadLine("ch_post_int", POST_LIKES, POST_COMMENTS, POST_SAVES, POST_SHARES);
quadLine("ch_reel_int", REEL_LIKES, REEL_COMMENTS, REEL_SAVES, REEL_SHARES);
</script>
</body>
</html>`;

fs.mkdirSync(path.dirname(OUTPUT_PATH), { recursive: true });
fs.writeFileSync(OUTPUT_PATH, html);
console.log(`Insights report generated: ${OUTPUT_PATH}`);
