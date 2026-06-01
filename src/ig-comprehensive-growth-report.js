/**
 * PeaceGrappler - Comprehensive Daily Email Report
 *
 * Generates an email-friendly HTML report with:
 * - Account summary & follower growth (yesterday/7d/30d)
 * - Per-post metrics (views, likes, comments, shares)
 * - Top posts by engagement
 * - Shares/reposts per post
 * - Top active commenters (30d + per-post breakdown)
 * - Top tagged/liked posts
 * - Engagement analytics
 * - UFC hype analysis (web-sourced)
 *
 * Usage: node src/ig-email-report.js [--morning|--evening]
 * Output: output/comprehensive-growth-report.html (email-ready, no JS dependencies)
 */

const Database = require("better-sqlite3");
const https = require("https");
const fs = require("fs");
const path = require("path");

const ROOT_DIR = path.join(__dirname, "..");
const DB_PATH = path.join(ROOT_DIR, "peacegrappler.db");
const db = new Database(DB_PATH, { readonly: true });

const argv = process.argv.slice(2);
const isEvening = argv.includes("--evening");

// Monthly mode: --month YYYY-MM (specific month) or --monthly (current month).
const monthIdx = argv.indexOf("--month");
const monthArg = monthIdx >= 0 ? argv[monthIdx + 1]
  : (argv.find((a) => a.startsWith("--month=")) || "").split("=")[1] || null;
const isMonthly = !!monthArg || argv.includes("--monthly");

let YEAR_MONTH = null;
let MONTH_LABEL = null;
if (isMonthly) {
  const now = monthArg ? new Date(`${monthArg}-01T00:00:00Z`) : new Date();
  const y = now.getUTCFullYear();
  const m = now.getUTCMonth() + 1;
  YEAR_MONTH = `${y}-${String(m).padStart(2, "0")}`;
  MONTH_LABEL = new Date(Date.UTC(y, m - 1, 1)).toLocaleString("en-US", { month: "long", year: "numeric", timeZone: "UTC" });
}

const OUTPUT_PATH = isMonthly
  ? path.join(ROOT_DIR, "output", `comprehensive-growth-report-${YEAR_MONTH}.html`)
  : path.join(ROOT_DIR, "output", "comprehensive-growth-report.html");

const reportType = isMonthly ? `Monthly (${MONTH_LABEL})` : (isEvening ? "Evening" : "Morning");

// SQL date-range expressions for the engagement window(s) shown in the report.
// Daily mode: 7d + 30d. Monthly mode: full target month (MTD if current).
function monthBounds(yyyymm) {
  // Returns [sinceISO, untilISO] for the calendar month.
  const [y, m] = yyyymm.split("-").map(Number);
  const since = `${yyyymm}-01 00:00:00`;
  const nextMonth = new Date(Date.UTC(y, m, 1));
  const until = nextMonth.toISOString().slice(0, 19).replace("T", " ");
  return [since, until];
}

let ENGAGEMENT_WINDOWS;
if (isMonthly) {
  const [since, until] = monthBounds(YEAR_MONTH);
  ENGAGEMENT_WINDOWS = [
    { key: "mtd", label: MONTH_LABEL, sinceExpr: `'${since}'`, untilExpr: `'${until}'` },
  ];
} else {
  ENGAGEMENT_WINDOWS = [
    { key: "last7",  label: "7d",  sinceExpr: "datetime('now', '-7 days')",  untilExpr: null },
    { key: "last30", label: "30d", sinceExpr: "datetime('now', '-30 days')", untilExpr: null },
  ];
}

// ============================================================
// Helpers
// ============================================================

function esc(str) {
  if (!str) return "";
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function truncate(str, len = 60) {
  if (!str) return "(no caption)";
  return str.length > len ? str.substring(0, len) + "..." : str;
}

function fmtNum(n) {
  if (n === null || n === undefined) return "-";
  return Number(n).toLocaleString();
}

function fmtDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function fmtPct(val, total) {
  if (!total) return "0%";
  return ((val / total) * 100).toFixed(1) + "%";
}

function delta(current, previous) {
  if (previous === null || previous === undefined) return { val: 0, str: "-", cls: "" };
  const d = current - previous;
  if (d > 0) return { val: d, str: `+${fmtNum(d)}`, cls: "positive" };
  if (d < 0) return { val: d, str: fmtNum(d), cls: "negative" };
  return { val: 0, str: "0", cls: "neutral" };
}

// ============================================================
// Data Queries
// ============================================================

function getAccount() {
  return db.prepare("SELECT * FROM ig_accounts LIMIT 1").get();
}

function getFollowerGrowth() {
  // Get the newest snapshot as our baseline
  const newest = db.prepare(
    "SELECT * FROM ig_account_snapshots ORDER BY snapshot_date DESC LIMIT 1"
  ).get();

  if (!newest) {
    return {
      current: 0,
      yesterday: delta(0, undefined),
      last7: delta(0, undefined),
      last30: delta(0, undefined),
      snapshots: [],
    };
  }

  const newestDate = newest.snapshot_date;

  // Find the snapshot closest to 1 day ago
  const yesterdaySnap = db.prepare(
    `SELECT * FROM ig_account_snapshots
     WHERE snapshot_date <= date(?, '-1 day')
     ORDER BY snapshot_date DESC LIMIT 1`
  ).get(newestDate);

  // Find the snapshot closest to 7 days ago
  const sevenDaySnap = db.prepare(
    `SELECT * FROM ig_account_snapshots
     WHERE snapshot_date <= date(?, '-7 days')
     ORDER BY snapshot_date DESC LIMIT 1`
  ).get(newestDate);

  // Find the snapshot closest to 30 days ago, falling back to oldest available
  const thirtyDaySnap = db.prepare(
    `SELECT * FROM ig_account_snapshots
     WHERE snapshot_date <= date(?, '-30 days')
     ORDER BY snapshot_date DESC LIMIT 1`
  ).get(newestDate) || db.prepare(
    `SELECT * FROM ig_account_snapshots
     WHERE snapshot_date < ?
     ORDER BY snapshot_date ASC LIMIT 1`
  ).get(newestDate);

  // Get all snapshots for the chart (last 31 days)
  const snapshots = db.prepare(
    `SELECT * FROM ig_account_snapshots
     WHERE snapshot_date >= date(?, '-30 days')
     ORDER BY snapshot_date ASC`
  ).all(newestDate);

  return {
    current: newest.followers_count || 0,
    yesterday: delta(newest.followers_count || 0, yesterdaySnap?.followers_count),
    last7: delta(newest.followers_count || 0, sevenDaySnap?.followers_count),
    last30: delta(newest.followers_count || 0, thirtyDaySnap?.followers_count),
    snapshots,
  };
}

function getPostCounts() {
  const periods = {
    yesterday: "datetime('now', '-1 day')",
    last7: "datetime('now', '-7 days')",
    last30: "datetime('now', '-30 days')",
  };

  const result = {};
  for (const [key, since] of Object.entries(periods)) {
    const row = db.prepare(`
      SELECT
        COUNT(*) as total,
        SUM(CASE WHEN media_product_type = 'REELS' THEN 1 ELSE 0 END) as reels,
        SUM(CASE WHEN media_product_type = 'FEED' THEN 1 ELSE 0 END) as feed,
        SUM(CASE WHEN media_type = 'CAROUSEL_ALBUM' THEN 1 ELSE 0 END) as carousels
      FROM ig_media
      WHERE media_product_type != 'STORY'
        AND timestamp >= ${since}
    `).get();
    result[key] = row;
  }
  return result;
}

function getPerPostMetrics(days = 30) {
  return db.prepare(`
    SELECT
      m.id, m.caption, m.media_type, m.media_product_type,
      m.like_count, m.comments_count, m.permalink, m.timestamp,
      m.shortcode,
      MAX(CASE WHEN i.metric = 'reach' THEN i.value END) AS reach,
      MAX(CASE WHEN i.metric = 'views' THEN i.value END) AS views,
      MAX(CASE WHEN i.metric = 'total_interactions' THEN i.value END) AS total_interactions,
      MAX(CASE WHEN i.metric = 'shares' THEN i.value END) AS shares,
      MAX(CASE WHEN i.metric = 'saved' THEN i.value END) AS saved,
      MAX(CASE WHEN i.metric = 'likes' THEN i.value END) AS insight_likes,
      MAX(CASE WHEN i.metric = 'comments' THEN i.value END) AS insight_comments,
      MAX(CASE WHEN i.metric = 'follows' THEN i.value END) AS follows,
      MAX(CASE WHEN i.metric = 'profile_visits' THEN i.value END) AS profile_visits
    FROM ig_media m
    LEFT JOIN (
      SELECT media_id, metric, value,
        ROW_NUMBER() OVER (PARTITION BY media_id, metric ORDER BY fetched_at DESC) AS rn
      FROM ig_media_insights
    ) i ON i.media_id = m.id AND i.rn = 1
    WHERE m.media_product_type != 'STORY'
      AND m.timestamp >= datetime('now', '-${days} days')
    GROUP BY m.id
    ORDER BY m.timestamp DESC
  `).all();
}

function getTopPostsByEngagement(limit = 10) {
  return db.prepare(`
    SELECT
      m.id, m.caption, m.media_type, m.media_product_type,
      m.like_count, m.comments_count, m.permalink, m.timestamp,
      MAX(CASE WHEN i.metric = 'views' THEN i.value END) AS views,
      MAX(CASE WHEN i.metric = 'shares' THEN i.value END) AS shares,
      MAX(CASE WHEN i.metric = 'saved' THEN i.value END) AS saved,
      MAX(CASE WHEN i.metric = 'reach' THEN i.value END) AS reach,
      COALESCE(m.like_count, 0) + COALESCE(m.comments_count, 0) AS engagement
    FROM ig_media m
    LEFT JOIN (
      SELECT media_id, metric, value,
        ROW_NUMBER() OVER (PARTITION BY media_id, metric ORDER BY fetched_at DESC) AS rn
      FROM ig_media_insights
    ) i ON i.media_id = m.id AND i.rn = 1
    WHERE m.media_product_type != 'STORY'
      AND m.timestamp >= datetime('now', '-30 days')
    GROUP BY m.id
    ORDER BY engagement DESC
    LIMIT ?
  `).all(limit);
}

function getTopPostsByLikes(limit = 10) {
  return db.prepare(`
    SELECT m.id, m.caption, m.media_product_type, m.like_count,
      m.comments_count, m.permalink, m.timestamp
    FROM ig_media m
    WHERE m.media_product_type != 'STORY'
      AND m.timestamp >= datetime('now', '-30 days')
    ORDER BY m.like_count DESC
    LIMIT ?
  `).all(limit);
}

function getTopCommenters(days = 30) {
  const ignored = db.prepare("SELECT username FROM ig_ignored_accounts").all()
    .map(r => r.username.toLowerCase());

  const comments = db.prepare(`
    SELECT c.username, c.text, c.media_id, c.parent_comment_id, c.timestamp,
      m.caption AS media_caption, m.timestamp AS media_timestamp
    FROM ig_comments c
    LEFT JOIN ig_media m ON m.id = c.media_id
    WHERE c.hidden = 0
      AND c.timestamp >= datetime('now', '-${days} days')
    ORDER BY c.timestamp DESC
  `).all();

  const users = {};
  for (const c of comments) {
    if (!c.username) continue;
    const uname = c.username.toLowerCase();
    if (ignored.includes(uname)) continue;

    if (!users[uname]) {
      users[uname] = {
        username: c.username,
        total: 0,
        replies: 0,
        posts: {},
      };
    }
    users[uname].total++;
    if (c.parent_comment_id) users[uname].replies++;

    // Track per-post comments
    if (c.media_id) {
      if (!users[uname].posts[c.media_id]) {
        users[uname].posts[c.media_id] = {
          caption: c.media_caption,
          count: 0,
        };
      }
      users[uname].posts[c.media_id].count++;
    }
  }

  return Object.values(users)
    .sort((a, b) => b.total - a.total)
    .map(u => ({
      ...u,
      topPosts: Object.entries(u.posts)
        .sort((a, b) => b[1].count - a[1].count)
        .slice(0, 5)
        .map(([id, data]) => ({ mediaId: id, caption: data.caption, count: data.count })),
    }));
}

function getCommentersPerPost() {
  // Top commenters for the last 5 posts
  const recentPosts = db.prepare(`
    SELECT id, caption, timestamp
    FROM ig_media
    WHERE media_product_type != 'STORY'
    ORDER BY timestamp DESC
    LIMIT 5
  `).all();

  const ignored = db.prepare("SELECT username FROM ig_ignored_accounts").all()
    .map(r => r.username.toLowerCase());

  const result = [];
  for (const post of recentPosts) {
    const commenters = db.prepare(`
      SELECT username, COUNT(*) as count
      FROM ig_comments
      WHERE media_id = ? AND hidden = 0
        AND username IS NOT NULL
      GROUP BY LOWER(username)
      ORDER BY count DESC
      LIMIT 10
    `).all(post.id);

    result.push({
      postId: post.id,
      caption: post.caption,
      timestamp: post.timestamp,
      commenters: commenters.filter(c => !ignored.includes(c.username.toLowerCase())),
    });
  }
  return result;
}

function getEngagementMetrics(sinceExpr, untilExpr = null) {
  const untilClause = untilExpr ? `AND m.timestamp < ${untilExpr}` : "";
  return db.prepare(`
    SELECT
      COUNT(*) as posts,
      SUM(COALESCE(m.like_count, 0)) as total_likes,
      SUM(COALESCE(m.comments_count, 0)) as total_comments,
      SUM(COALESCE(i_shares.value, 0)) as total_shares,
      SUM(COALESCE(i_views.value, 0)) as total_views,
      SUM(COALESCE(i_reach.value, 0)) as total_reach,
      SUM(COALESCE(i_saved.value, 0)) as total_saved
    FROM ig_media m
    LEFT JOIN (SELECT media_id, value FROM ig_media_insights WHERE metric = 'shares'
      AND fetched_at = (SELECT MAX(fetched_at) FROM ig_media_insights mi2 WHERE mi2.media_id = ig_media_insights.media_id AND mi2.metric = 'shares')
    ) i_shares ON i_shares.media_id = m.id
    LEFT JOIN (SELECT media_id, value FROM ig_media_insights WHERE metric = 'views'
      AND fetched_at = (SELECT MAX(fetched_at) FROM ig_media_insights mi2 WHERE mi2.media_id = ig_media_insights.media_id AND mi2.metric = 'views')
    ) i_views ON i_views.media_id = m.id
    LEFT JOIN (SELECT media_id, value FROM ig_media_insights WHERE metric = 'reach'
      AND fetched_at = (SELECT MAX(fetched_at) FROM ig_media_insights mi2 WHERE mi2.media_id = ig_media_insights.media_id AND mi2.metric = 'reach')
    ) i_reach ON i_reach.media_id = m.id
    LEFT JOIN (SELECT media_id, value FROM ig_media_insights WHERE metric = 'saved'
      AND fetched_at = (SELECT MAX(fetched_at) FROM ig_media_insights mi2 WHERE mi2.media_id = ig_media_insights.media_id AND mi2.metric = 'saved')
    ) i_saved ON i_saved.media_id = m.id
    WHERE m.media_product_type != 'STORY'
      AND m.timestamp >= ${sinceExpr}
      ${untilClause}
  `).get();
}

function getEngagementAnalytics() {
  const out = {};
  for (const w of ENGAGEMENT_WINDOWS) {
    out[w.key] = getEngagementMetrics(w.sinceExpr, w.untilExpr);
  }
  return out;
}

function getAccountInsightsSummary() {
  return db.prepare(`
    SELECT metric, breakdown_dimension, breakdown_value, SUM(value) as value
    FROM ig_account_insights
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, value DESC
  `).all();
}

/**
 * Account-wide totals from ig_account_insights (matches what IG's native app shows).
 * Reads the no-breakdown row (breakdown_dimension IS NULL) so reach is the account-wide
 * deduplicated count rather than a sum across content types (which would double-count
 * users who saw both an ad AND a Reel, for instance).
 *
 * Two modes:
 *   - rolling (default): returns the latest `days_28` snapshot — what IG's app shows.
 *   - month(YYYY-MM): sums `period='day'` rows whose end_time falls in the target month.
 *                     Falls back to days_28 with a note when not enough daily history exists.
 *
 * Returns: { views, reach, label, isFallback }
 */
function getAccountWideTotals({ monthYM = null } = {}) {
  // Helper: read the latest days_28 (no breakdown) snapshot.
  function days28() {
    const rows = db.prepare(`
      SELECT metric, value
      FROM ig_account_insights
      WHERE period = 'days_28'
        AND breakdown_dimension IS NULL
        AND end_time = (SELECT MAX(end_time) FROM ig_account_insights WHERE period = 'days_28' AND breakdown_dimension IS NULL)
    `).all();
    const out = {};
    for (const r of rows) out[r.metric] = r.value;
    return out;
  }

  if (!monthYM) {
    const t = days28();
    return { ...t, label: "28d", isFallback: false };
  }

  // Monthly mode: sum day-period rows whose end_time falls in the target month.
  const [y, m] = monthYM.split("-").map(Number);
  const monthStart = `${monthYM}-01`;
  const nextMonth = new Date(Date.UTC(y, m, 1)).toISOString().slice(0, 10);
  const dailyRows = db.prepare(`
    SELECT metric, SUM(value) AS total, COUNT(*) AS days
    FROM ig_account_insights
    WHERE period = 'day'
      AND breakdown_dimension IS NULL
      AND end_time >= ?
      AND end_time < ?
    GROUP BY metric
  `).all(monthStart, nextMonth);

  if (dailyRows.length > 0 && dailyRows[0].days >= 14) {
    // Have at least half a month of daily snapshots — use them.
    const out = { label: monthYM, isFallback: false };
    for (const r of dailyRows) out[r.metric] = r.total;
    return out;
  }

  // Fallback: not enough daily history yet, use rolling days_28.
  const t = days28();
  return { ...t, label: "28d (rolling)", isFallback: true };
}

function getTopTaggedPosts() {
  // Posts with most comments (proxy for "tagged"/mentioned - most discussed)
  return db.prepare(`
    SELECT m.id, m.caption, m.media_product_type, m.permalink, m.timestamp,
      m.like_count, m.comments_count,
      COALESCE(m.like_count, 0) + COALESCE(m.comments_count, 0) AS engagement
    FROM ig_media m
    WHERE m.media_product_type != 'STORY'
    ORDER BY m.comments_count DESC
    LIMIT 10
  `).all();
}

function getRepostsAndShares() {
  return db.prepare(`
    SELECT
      m.id, m.caption, m.media_product_type, m.timestamp, m.permalink,
      MAX(CASE WHEN i.metric = 'shares' THEN i.value END) AS shares
    FROM ig_media m
    LEFT JOIN (
      SELECT media_id, metric, value,
        ROW_NUMBER() OVER (PARTITION BY media_id, metric ORDER BY fetched_at DESC) AS rn
      FROM ig_media_insights
    ) i ON i.media_id = m.id AND i.rn = 1
    WHERE m.media_product_type != 'STORY'
      AND m.timestamp >= datetime('now', '-30 days')
    GROUP BY m.id
    HAVING shares > 0
    ORDER BY shares DESC
    LIMIT 10
  `).all();
}

// ============================================================
// HTML Report Generation
// ============================================================

function generateReport() {
  const account = getAccount();
  const growth = getFollowerGrowth();
  const postCounts = getPostCounts();
  const perPost = getPerPostMetrics(30);
  const topPosts = getTopPostsByEngagement(10);
  const topLiked = getTopPostsByLikes(10);
  const topCommenters = getTopCommenters(30);
  const commentersPerPost = getCommentersPerPost();
  const engagement = getEngagementAnalytics();
  const topTagged = getTopTaggedPosts();
  const shares = getRepostsAndShares();
  const accountWide = getAccountWideTotals({ monthYM: isMonthly ? YEAR_MONTH : null });

  const now = new Date();
  const generatedAt = now.toLocaleString("en-US", { dateStyle: "full", timeStyle: "short" });

  // Per-window engagement totals + averages.
  const engagementSummary = {};
  for (const w of ENGAGEMENT_WINDOWS) {
    const e = engagement[w.key] || {};
    const total = (e.total_likes || 0) + (e.total_comments || 0);
    engagementSummary[w.key] = {
      label: w.label,
      total,
      avgPerPost: e.posts ? Math.round(total / e.posts) : 0,
      views: e.total_views || 0,
      reach: e.total_reach || 0,
      shares: e.total_shares || 0,
      saves: e.total_saved || 0,
      likes: e.total_likes || 0,
      comments: e.total_comments || 0,
      posts: e.posts || 0,
    };
  }
  // Primary window for the Account Summary cards: first defined window.
  const primaryKey = ENGAGEMENT_WINDOWS[0].key;
  const primary = engagementSummary[primaryKey];

  const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PeaceGrappler - ${reportType} Report</title>
<link rel="stylesheet" href="styles.css">
<style>
  /* comprehensive-growth-report tweaks */
  h1 { font-size: 28px; margin-bottom: 4px; }
  h2 { font-size: 20px; margin: 28px 0 16px; }
  h2:first-child { margin-top: 8px; }
</style>
</head>
<body>

<div class="page-header">
  <div class="site-nav">
    <a href="index.html">&#8592; All Reports</a>
    <span class="site-nav-label">PeaceGrappler</span>
  </div>
  <h1>PeaceGrappler</h1>
  <div class="subtitle">@${esc(account?.username || "peacegrappler")} &mdash; ${generatedAt}</div>
  <div class="report-badge">${reportType} Report</div>
  <div class="nav-tabs">
    <div class="nav-tab active"    onclick="switchTab('overview')">Overview</div>
    <div class="nav-tab"           onclick="switchTab('top-posts')">Top Posts</div>
    <div class="nav-tab"           onclick="switchTab('breakdown')">Post Breakdown</div>
    <div class="nav-tab"           onclick="switchTab('shares')">Shares</div>
    <div class="nav-tab"           onclick="switchTab('commenters')">Commenters</div>
    <div class="nav-tab"           onclick="switchTab('ufc')">UFC Hype</div>
  </div>
</div>

<div class="page-content">

<!-- ==================== TAB 1: OVERVIEW ==================== -->
<div id="page-overview" class="page-panel active">

  <h2>Account Summary</h2>
  <div class="metrics-grid">
    <div class="metric-card">
      <div class="value">${fmtNum(growth.current)}</div>
      <div class="label">Followers</div>
    </div>
    <div class="metric-card">
      <div class="value">${fmtNum(account?.media_count)}</div>
      <div class="label">Total Posts</div>
    </div>
    <div class="metric-card">
      <div class="value">${fmtNum(account?.follows_count)}</div>
      <div class="label">Following</div>
    </div>
    <div class="metric-card">
      <div class="value">${fmtNum(primary.total)}</div>
      <div class="label">Engagement (${primary.label})</div>
    </div>
    <div class="metric-card">
      <div class="value">${fmtNum(accountWide.views)}</div>
      <div class="label">Views (${accountWide.label}, all sources)</div>
    </div>
    <div class="metric-card">
      <div class="value">${fmtNum(accountWide.reach)}</div>
      <div class="label">Reach (${accountWide.label}, all sources)</div>
    </div>
  </div>
  <div class="section-note">
    Views/Reach include ads, stories, and carousels — matching what Instagram's native app shows. Per-post breakdowns in Engagement Analytics below count organic posts only.${accountWide.isFallback ? ` <strong>Note:</strong> not enough daily snapshots yet for true month totals; showing rolling 28d.` : ""}
  </div>

  <h2>Follower Growth</h2>
  <div class="growth-row">
    <div class="growth-card">
      <div class="period">Yesterday</div>
      <div class="growth-value ${growth.yesterday.cls}">${growth.yesterday.str}</div>
      <div class="sub">followers</div>
    </div>
    <div class="growth-card">
      <div class="period">Last 7 Days</div>
      <div class="growth-value ${growth.last7.cls}">${growth.last7.str}</div>
      <div class="sub">followers</div>
    </div>
    <div class="growth-card">
      <div class="period">Last 30 Days</div>
      <div class="growth-value ${growth.last30.cls}">${growth.last30.str}</div>
      <div class="sub">followers</div>
    </div>
  </div>

  <h2>Content Published</h2>
  <div class="card" style="overflow-x:auto;">
    <table>
      <thead><tr><th>Period</th><th>Total</th><th>Reels</th><th>Feed Posts</th><th>Carousels</th></tr></thead>
      <tbody>
        <tr><td>Yesterday</td><td><strong>${fmtNum(postCounts.yesterday?.total)}</strong></td><td>${fmtNum(postCounts.yesterday?.reels)}</td><td>${fmtNum(postCounts.yesterday?.feed)}</td><td>${fmtNum(postCounts.yesterday?.carousels)}</td></tr>
        <tr><td>Last 7 Days</td><td><strong>${fmtNum(postCounts.last7?.total)}</strong></td><td>${fmtNum(postCounts.last7?.reels)}</td><td>${fmtNum(postCounts.last7?.feed)}</td><td>${fmtNum(postCounts.last7?.carousels)}</td></tr>
        <tr><td>Last 30 Days</td><td><strong>${fmtNum(postCounts.last30?.total)}</strong></td><td>${fmtNum(postCounts.last30?.reels)}</td><td>${fmtNum(postCounts.last30?.feed)}</td><td>${fmtNum(postCounts.last30?.carousels)}</td></tr>
      </tbody>
    </table>
  </div>

  <h2>Engagement Analytics</h2>
  ${ENGAGEMENT_WINDOWS.map((w) => {
    const s = engagementSummary[w.key];
    return `
  <h3>${esc(w.label)}</h3>
  <div class="metrics-grid">
    <div class="metric-card"><div class="value">${fmtNum(s.total)}</div><div class="label">Total Engagement</div></div>
    <div class="metric-card"><div class="value">${fmtNum(s.avgPerPost)}</div><div class="label">Avg per Post</div></div>
    <div class="metric-card"><div class="value">${fmtNum(s.views)}</div><div class="label">Views</div></div>
    <div class="metric-card"><div class="value">${fmtNum(s.reach)}</div><div class="label">Reach</div></div>
    <div class="metric-card"><div class="value">${fmtNum(s.shares)}</div><div class="label">Shares</div></div>
    <div class="metric-card"><div class="value">${fmtNum(s.saves)}</div><div class="label">Saves</div></div>
    <div class="metric-card"><div class="value">${fmtNum(s.likes)}</div><div class="label">Likes</div></div>
    <div class="metric-card"><div class="value">${fmtNum(s.comments)}</div><div class="label">Comments</div></div>
    <div class="metric-card"><div class="value">${fmtNum(s.posts)}</div><div class="label">Posts</div></div>
  </div>`;
  }).join("")}

</div>

<!-- ==================== TAB 2: TOP POSTS ==================== -->
<div id="page-top-posts" class="page-panel">

  <h2>Top 10 Posts by Engagement (Last 30 Days)</h2>
  <div class="section-note">Ranked by likes + comments</div>
  ${topPosts.map((p, i) => `
  <div class="post-card">
    <div class="post-header">
      <span><span class="rank ${i < 3 ? 'rank-' + (i + 1) : ''}">#${i + 1}</span>
      <span class="badge badge-${(p.media_product_type || 'feed').toLowerCase()}">${esc(p.media_product_type || p.media_type)}</span></span>
      <span style="color:var(--muted);font-size:12px">${fmtDate(p.timestamp)}</span>
    </div>
    <div class="post-caption">${esc(truncate(p.caption, 100))}</div>
    <div class="post-metrics">
      <div class="pm"><div class="pm-val engagement-val">${fmtNum(p.engagement)}</div><div class="pm-label">Engagement</div></div>
      <div class="pm"><div class="pm-val">${fmtNum(p.like_count)}</div><div class="pm-label">Likes</div></div>
      <div class="pm"><div class="pm-val">${fmtNum(p.comments_count)}</div><div class="pm-label">Comments</div></div>
      <div class="pm"><div class="pm-val">${fmtNum(p.views)}</div><div class="pm-label">Views</div></div>
      <div class="pm"><div class="pm-val">${fmtNum(p.shares)}</div><div class="pm-label">Shares</div></div>
      <div class="pm"><div class="pm-val">${fmtNum(p.reach)}</div><div class="pm-label">Reach</div></div>
    </div>
  </div>
  `).join("")}

  <h2>Top 10 Most Liked Posts (Last 30 Days)</h2>
  <div class="card" style="overflow-x:auto;">
    <table>
      <thead><tr><th>#</th><th>Likes</th><th>Comments</th><th>Type</th><th>Caption</th><th>Date</th></tr></thead>
      <tbody>
        ${topLiked.map((p, i) => `
        <tr>
          <td class="rank ${i < 3 ? 'rank-' + (i + 1) : ''}">${i + 1}</td>
          <td class="engagement-val">${fmtNum(p.like_count)}</td>
          <td>${fmtNum(p.comments_count)}</td>
          <td><span class="badge badge-${(p.media_product_type || 'feed').toLowerCase()}">${esc(p.media_product_type || 'FEED')}</span></td>
          <td class="caption-cell">${esc(truncate(p.caption, 60))}</td>
          <td>${fmtDate(p.timestamp)}</td>
        </tr>`).join("")}
      </tbody>
    </table>
  </div>

  <h2>Top 10 Most Discussed Posts (All Time)</h2>
  <div class="section-note">Posts with the most comments across all time</div>
  <div class="card" style="overflow-x:auto;">
    <table>
      <thead><tr><th>#</th><th>Comments</th><th>Likes</th><th>Engagement</th><th>Caption</th><th>Date</th></tr></thead>
      <tbody>
        ${topTagged.map((p, i) => `
        <tr>
          <td class="rank ${i < 3 ? 'rank-' + (i + 1) : ''}">${i + 1}</td>
          <td class="engagement-val">${fmtNum(p.comments_count)}</td>
          <td>${fmtNum(p.like_count)}</td>
          <td>${fmtNum(p.engagement)}</td>
          <td class="caption-cell">${esc(truncate(p.caption, 60))}</td>
          <td>${fmtDate(p.timestamp)}</td>
        </tr>`).join("")}
      </tbody>
    </table>
  </div>

</div>

<!-- ==================== TAB 3: POST BREAKDOWN ==================== -->
<div id="page-breakdown" class="page-panel">

  <h2>Per-Post Breakdown (Last 30 Days)</h2>
  <div class="section-note">Views and metrics for every post in the last 30 days</div>
  <div class="card" style="overflow-x:auto;">
    <table>
      <thead>
        <tr><th>#</th><th>Date</th><th>Type</th><th>Caption</th><th>Views</th><th>Likes</th><th>Comments</th><th>Shares</th><th>Reach</th><th>Link</th></tr>
      </thead>
      <tbody>
        ${perPost.map((m, i) => `
        <tr>
          <td>${i + 1}</td>
          <td>${fmtDate(m.timestamp)}</td>
          <td><span class="badge badge-${(m.media_product_type || 'feed').toLowerCase()}">${esc(m.media_product_type || 'FEED')}</span></td>
          <td class="caption-cell">${esc(truncate(m.caption, 50))}</td>
          <td><strong>${fmtNum(m.views)}</strong></td>
          <td>${fmtNum(m.insight_likes || m.like_count)}</td>
          <td>${fmtNum(m.insight_comments || m.comments_count)}</td>
          <td>${fmtNum(m.shares)}</td>
          <td>${fmtNum(m.reach)}</td>
          <td>${m.permalink ? `<a href="${esc(m.permalink)}" target="_blank">View</a>` : '-'}</td>
        </tr>`).join("")}
      </tbody>
    </table>
  </div>

</div>

<!-- ==================== TAB 4: SHARES ==================== -->
<div id="page-shares" class="page-panel">

  <h2>Top Shared &amp; Reposted Content (Last 30 Days)</h2>
  ${shares.length ? `
  <div class="card" style="overflow-x:auto;">
    <table>
      <thead><tr><th>#</th><th>Shares</th><th>Type</th><th>Caption</th><th>Date</th><th>Link</th></tr></thead>
      <tbody>
        ${shares.map((s, i) => `
        <tr>
          <td class="rank ${i < 3 ? 'rank-' + (i + 1) : ''}">${i + 1}</td>
          <td class="engagement-val">${fmtNum(s.shares)}</td>
          <td><span class="badge badge-${(s.media_product_type || 'feed').toLowerCase()}">${esc(s.media_product_type || 'FEED')}</span></td>
          <td class="caption-cell">${esc(truncate(s.caption, 60))}</td>
          <td>${fmtDate(s.timestamp)}</td>
          <td>${s.permalink ? `<a href="${esc(s.permalink)}" target="_blank">View</a>` : '-'}</td>
        </tr>`).join("")}
      </tbody>
    </table>
  </div>` : '<div class="section-note">No shared content data available yet.</div>'}

</div>

<!-- ==================== TAB 5: COMMENTERS ==================== -->
<div id="page-commenters" class="page-panel">

  <h2>Top Active Commenters (Last 30 Days)</h2>
  <div class="card" style="overflow-x:auto;">
    <table>
      <thead><tr><th>#</th><th>Username</th><th>Comments</th><th>Replies</th><th>Total</th><th>Top Posts Commented On</th></tr></thead>
      <tbody>
        ${topCommenters.slice(0, 20).map((u, i) => `
        <tr>
          <td class="rank ${i < 3 ? 'rank-' + (i + 1) : ''}">${i + 1}</td>
          <td><strong>@${esc(u.username)}</strong></td>
          <td>${fmtNum(u.total - u.replies)}</td>
          <td>${fmtNum(u.replies)}</td>
          <td class="engagement-val">${fmtNum(u.total)}</td>
          <td style="max-width:200px;white-space:normal;font-size:11px;color:var(--muted);">
            ${u.topPosts.slice(0, 3).map(p => `${p.count}x on "${esc(truncate(p.caption, 30))}"`).join('<br>')}
          </td>
        </tr>`).join("")}
      </tbody>
    </table>
  </div>

  <h2>Commenter Breakdown: Last 5 Posts</h2>
  <div class="section-note">Who commented on each of the 5 most recent posts</div>
  ${commentersPerPost.map(post => `
  <div class="commenter-block">
    <div class="cb-header">${fmtDate(post.timestamp)} &mdash; ${esc(truncate(post.caption, 70))}</div>
    <div class="cb-sub">${post.commenters.length} unique commenters</div>
    <div class="cb-posts">
      ${post.commenters.slice(0, 8).map(c => `<span>@${esc(c.username)}</span> (${c.count})`).join(' &bull; ')}
      ${post.commenters.length > 8 ? `<br><span style="color:var(--muted);">... and ${post.commenters.length - 8} more</span>` : ''}
    </div>
  </div>`).join("")}

</div>

<!-- ==================== TAB 6: UFC HYPE ==================== -->
<div id="page-ufc" class="page-panel">

  <h2>UFC Hype &amp; Fighter News</h2>
  <div class="section-note">Analysis based on recent post captions and engagement patterns</div>
  ${generateUFCSection(perPost)}

</div>

</div><!-- .page-content -->

<div class="footer" style="max-width:1200px;margin:0 auto;padding:20px 24px;">
  <p>Report generated by PeaceGrappler Automation for @${esc(account?.username || "peacegrappler")}</p>
  <p>Data from Instagram Meta Graph API &bull; ${generatedAt}</p>
  <p>
    <a href="https://paxlucta.github.io/peace-grappler/index.html">All Reports</a> &bull;
    <a href="https://paxlucta.github.io/peace-grappler/engagement-report.html">Full Interactive Report</a> &bull;
    <a href="https://paxlucta.github.io/peace-grappler/engagement-rankings.html">Community Rankings</a>
  </p>
</div>

<script>
function switchTab(id) {
  document.querySelectorAll('.page-panel').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
  document.getElementById('page-' + id).classList.add('active');
  event.target.classList.add('active');
}
<\/script>

</body>
</html>`;

  fs.writeFileSync(OUTPUT_PATH, html);
  console.log(`${isMonthly ? "Monthly" : "Daily"} email report generated: ${OUTPUT_PATH}`);
  return OUTPUT_PATH;
}

// ============================================================
// UFC Hype Section - extracted from post captions
// ============================================================

function generateUFCSection(posts) {
  // Extract fighter names and themes from recent post captions
  const fightKeywords = ['ufc', 'fight', 'luta', 'card', 'main event', 'bellator', 'pfl', 'saturday', 'sabado'];
  const hypeKeywords = ['knockout', 'nocaute', 'ko', 'submission', 'finalização', 'title', 'cinturão', 'champion', 'campeão'];

  const fightPosts = posts.filter(p => {
    const cap = (p.caption || '').toLowerCase();
    return fightKeywords.some(k => cap.includes(k));
  });

  const hypePosts = posts.filter(p => {
    const cap = (p.caption || '').toLowerCase();
    return hypeKeywords.some(k => cap.includes(k));
  });

  // Find posts with highest engagement that mention fights
  const topFightPosts = fightPosts
    .sort((a, b) => ((b.like_count || 0) + (b.comments_count || 0)) - ((a.like_count || 0) + (a.comments_count || 0)))
    .slice(0, 5);

  // Determine algorithm status based on engagement trend
  const recentViews = posts.slice(0, 5).reduce((s, p) => s + (p.views || 0), 0);
  const olderViews = posts.slice(5, 10).reduce((s, p) => s + (p.views || 0), 0);
  let algorithmStatus = 'Stable';
  let algorithmColor = '#8b8fa3';
  if (recentViews > olderViews * 1.3) {
    algorithmStatus = 'HIGH - Recent posts trending up';
    algorithmColor = '#34d399';
  } else if (recentViews < olderViews * 0.7) {
    algorithmStatus = 'LOW - Recent posts underperforming';
    algorithmColor = '#f87171';
  } else {
    algorithmStatus = 'STABLE - Consistent performance';
    algorithmColor = '#fb923c';
  }

  // Identify high-interest athletes from captions
  const athleteMentions = {};
  for (const p of posts) {
    const cap = p.caption || '';
    // Look for names (capitalized words that appear to be names)
    const namePattern = /([A-Z][a-záàâãéèêíìîóòôõúùûç]+(?:\s+[A-Z][a-záàâãéèêíìîóòôõúùûç]+)+)/g;
    let match;
    while ((match = namePattern.exec(cap)) !== null) {
      const name = match[1].trim();
      if (name.length > 5 && name.length < 40 && !name.startsWith('Para ') && !name.startsWith('Que ')) {
        if (!athleteMentions[name]) athleteMentions[name] = { count: 0, engagement: 0 };
        athleteMentions[name].count++;
        athleteMentions[name].engagement += (p.like_count || 0) + (p.comments_count || 0);
      }
    }
  }

  const topAthletes = Object.entries(athleteMentions)
    .sort((a, b) => b[1].engagement - a[1].engagement)
    .slice(0, 10);

  return `
  <div class="post-card">
    <h3 style="margin-top:0">Algorithm Status</h3>
    <div style="font-size:18px;font-weight:700;color:${algorithmColor};margin:8px 0">${algorithmStatus}</div>
    <div style="font-size:12px;color:#8b8fa3">
      Recent 5 posts: ${fmtNum(recentViews)} views &bull;
      Previous 5 posts: ${fmtNum(olderViews)} views
    </div>
  </div>

  ${topFightPosts.length ? `
  <h3>Top UFC/Fight Content (Last 30 Days)</h3>
  <div class="section-note">Posts mentioning fights with highest engagement</div>
  ${topFightPosts.map((p, i) => `
  <div class="post-card">
    <div class="post-header">
      <span><span class="rank ${i < 3 ? 'rank-' + (i+1) : ''}">#${i+1}</span></span>
      <span style="font-size:12px;color:#8b8fa3">${fmtDate(p.timestamp)}</span>
    </div>
    <div class="post-caption">${esc(truncate(p.caption, 120))}</div>
    <div class="post-metrics">
      <div class="pm"><div class="pm-val">${fmtNum((p.like_count||0) + (p.comments_count||0))}</div><div class="pm-label">Engagement</div></div>
      <div class="pm"><div class="pm-val">${fmtNum(p.views)}</div><div class="pm-label">Views</div></div>
      <div class="pm"><div class="pm-val">${fmtNum(p.shares)}</div><div class="pm-label">Shares</div></div>
    </div>
  </div>
  `).join("")}
  ` : ''}

  ${topAthletes.length ? `
  <h3>Athletes with Highest Interest</h3>
  <div class="section-note">Athletes mentioned in posts, ranked by engagement generated</div>
  <table>
    <tr>
      <th>#</th>
      <th>Athlete</th>
      <th>Mentions</th>
      <th>Total Engagement</th>
    </tr>
    ${topAthletes.map(([name, data], i) => `
    <tr>
      <td class="rank ${i < 3 ? 'rank-' + (i+1) : ''}">${i+1}</td>
      <td><strong>${esc(name)}</strong></td>
      <td>${data.count}</td>
      <td class="engagement-val">${fmtNum(data.engagement)}</td>
    </tr>
    `).join("")}
  </table>
  ` : ''}
  `;
}

// ============================================================
// Main
// ============================================================

generateReport();
