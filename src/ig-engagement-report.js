const Database = require("better-sqlite3");
const fs = require("fs");
const path = require("path");
const XLSX = require("xlsx");

const ROOT_DIR = path.join(__dirname, "..");
const OUTPUT_DIR = path.join(ROOT_DIR, "output");
const DB_PATH = path.join(ROOT_DIR, "peacegrappler.db");
const db = new Database(DB_PATH, { readonly: true });

// CLI: --month YYYY-MM regenerates a specific month against the current DB.
// Without it, generates rolling 30-day + current-month-to-date as before.
const argv = process.argv.slice(2);
const monthIdx = argv.indexOf("--month");
const monthArg = monthIdx >= 0 ? argv[monthIdx + 1] : null
  || (argv.find((a) => a.startsWith("--month=")) || "").split("=")[1] || null;

let now;
let YEAR_MONTH;
let MONTH_LABEL;
let SQL_NOW; // SQL expression that stands in for "now" (anchor for relative date math)

if (monthArg) {
  const [y, m] = monthArg.split("-").map(Number);
  // Anchor at last second of target month so relative math (-30 days, etc.)
  // and the MTD month-start cap both behave correctly.
  now = new Date(Date.UTC(y, m, 0, 23, 59, 59));
  YEAR_MONTH = monthArg;
  MONTH_LABEL = now.toLocaleString("en-US", { month: "long", year: "numeric", timeZone: "UTC" });
  SQL_NOW = `'${now.toISOString().slice(0, 19).replace("T", " ")}'`;
} else {
  now = new Date();
  YEAR_MONTH = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  MONTH_LABEL = now.toLocaleString("en-US", { month: "long", year: "numeric" });
  SQL_NOW = "'now'";
}

// ============================================================
// Engagement scoring
// ============================================================

const EMOJI_REGEX = /^[\p{Emoji}\p{Emoji_Presentation}\p{Emoji_Modifier}\p{Emoji_Modifier_Base}\p{Emoji_Component}\s\u200d\ufe0f]+$/u;

function isEmojiOnly(text) {
  if (!text) return true;
  return EMOJI_REGEX.test(text.trim());
}

function isEarly(comment) {
  if (!comment.timestamp || !comment.ref_timestamp) return false;
  const commentTime = new Date(comment.timestamp).getTime();
  const refTime = new Date(comment.ref_timestamp).getTime();
  return (commentTime - refTime) <= 30 * 60 * 1000; // 30 minutes
}

function scoreComment(comment) {
  const isReply = !!comment.parent_comment_id;
  const emojiOnly = isEmojiOnly(comment.text);
  const early = isEarly(comment);
  let base;
  if (isReply) base = emojiOnly ? 2 : 7;
  else base = emojiOnly ? 1 : 5;
  return early ? base * 2 : base;
}

// ============================================================
// Data queries
// ============================================================

function getAccount() {
  return db.prepare("SELECT * FROM ig_accounts LIMIT 1").get();
}

function getAccountSnapshots() {
  return db.prepare(
    "SELECT * FROM ig_account_snapshots ORDER BY snapshot_date ASC"
  ).all();
}

function getFollowerGrowth(mode = "rolling") {
  const snapshots = db.prepare(
    "SELECT * FROM ig_account_snapshots ORDER BY snapshot_date DESC LIMIT 31"
  ).all();
  const today = snapshots[0] || {};

  // For MTD: cap comparison points at month start
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).toISOString().slice(0, 10);

  function snapAtOrAfter(dateStr) {
    // Find the snapshot closest to dateStr, but not before month start in MTD mode
    const effectiveDate = (mode === "mtd" && dateStr < monthStart) ? monthStart : dateStr;
    return snapshots.find(s => s.snapshot_date <= effectiveDate) || {};
  }

  const yesterday = new Date(now); yesterday.setDate(yesterday.getDate() - 1);
  const sevenAgo = new Date(now); sevenAgo.setDate(sevenAgo.getDate() - 7);

  const yesterdaySnap = snapshots[1] || {};
  const sevenAgoSnap = snapAtOrAfter(sevenAgo.toISOString().slice(0, 10));
  const periodSnap = mode === "mtd"
    ? (snapshots.find(s => s.snapshot_date < monthStart) || snapshots[snapshots.length - 1] || {})
    : (snapshots[snapshots.length - 1] || {});

  function d(a, b) {
    if (b === undefined || b === null) return { val: 0, str: "-", cls: "" };
    const v = a - b;
    if (v > 0) return { val: v, str: "+" + v.toLocaleString(), cls: "positive" };
    if (v < 0) return { val: v, str: v.toLocaleString(), cls: "negative" };
    return { val: 0, str: "0", cls: "neutral" };
  }

  return {
    current: today.followers_count || 0,
    yesterday: d(today.followers_count || 0, yesterdaySnap.followers_count),
    last7: d(today.followers_count || 0, sevenAgoSnap.followers_count),
    period: d(today.followers_count || 0, periodSnap.followers_count),
  };
}

function getPostCountsByPeriod(mode = "rolling", untilExpr = null) {
  const monthStart = `date(${SQL_NOW}, 'start of month')`;
  const untilClause = untilExpr ? `AND timestamp < ${untilExpr}` : "";

  // For MTD, cap each period at month start using CASE WHEN
  function sinceExpr(offset) {
    if (mode !== "mtd") return `datetime(${SQL_NOW}, '${offset}')`;
    return `CASE WHEN datetime(${SQL_NOW}, '${offset}') < ${monthStart} THEN ${monthStart} ELSE datetime(${SQL_NOW}, '${offset}') END`;
  }

  const periods = {
    yesterday: sinceExpr("-1 day"),
    last7: sinceExpr("-7 days"),
    period: mode === "mtd" ? monthStart : `datetime(${SQL_NOW}, '-30 days')`,
  };
  const result = {};
  for (const [key, expr] of Object.entries(periods)) {
    result[key] = db.prepare(`
      SELECT COUNT(*) as total,
        SUM(CASE WHEN media_product_type = 'REELS' THEN 1 ELSE 0 END) as reels,
        SUM(CASE WHEN media_product_type = 'FEED' THEN 1 ELSE 0 END) as feed
      FROM ig_media WHERE media_product_type != 'STORY'
        AND timestamp >= ${expr} ${untilClause}
    `).get();
  }
  return result;
}

function getTopCommentersWithPosts(sinceExpr = `datetime(${SQL_NOW}, '-30 days')`, untilExpr = null) {
  const ignored = getIgnoredUsernames();
  const untilClause = untilExpr ? `AND c.timestamp < ${untilExpr}` : "";
  const comments = db.prepare(`
    SELECT c.username, c.media_id, c.parent_comment_id, m.caption
    FROM ig_comments c LEFT JOIN ig_media m ON m.id = c.media_id
    WHERE c.hidden = 0 AND c.timestamp >= ${sinceExpr} ${untilClause}
  `).all();
  const users = {};
  for (const c of comments) {
    if (!c.username) continue;
    const u = c.username.toLowerCase();
    if (ignored.includes(u)) continue;
    if (!users[u]) { users[u] = { username: c.username, total: 0, replies: 0, posts: {} }; }
    users[u].total++;
    if (c.parent_comment_id) users[u].replies++;
    if (c.media_id) {
      if (!users[u].posts[c.media_id]) users[u].posts[c.media_id] = { caption: c.caption, count: 0 };
      users[u].posts[c.media_id].count++;
    }
  }
  return Object.values(users).sort((a, b) => b.total - a.total).slice(0, 20);
}

function getSharesLeaderboard(sinceExpr = `datetime(${SQL_NOW}, '-30 days')`, untilExpr = null) {
  const untilClause = untilExpr ? `AND m.timestamp < ${untilExpr}` : "";
  return db.prepare(`
    SELECT m.id, m.caption, m.media_product_type, m.timestamp, m.permalink,
      MAX(CASE WHEN i.metric = 'shares' THEN i.value END) AS shares
    FROM ig_media m
    LEFT JOIN (SELECT media_id, metric, value,
      ROW_NUMBER() OVER (PARTITION BY media_id, metric ORDER BY fetched_at DESC) AS rn
      FROM ig_media_insights) i ON i.media_id = m.id AND i.rn = 1
    WHERE m.media_product_type != 'STORY' AND m.timestamp >= ${sinceExpr} ${untilClause}
    GROUP BY m.id HAVING shares > 0 ORDER BY shares DESC LIMIT 10
  `).all();
}

function getMediaPerformance(sinceExpr = null, untilExpr = null) {
  const sinceClause = sinceExpr ? `AND m.timestamp >= ${sinceExpr}` : "";
  const untilClause = untilExpr ? `AND m.timestamp < ${untilExpr}` : "";
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
      MAX(CASE WHEN i.metric = 'comments' THEN i.value END) AS insight_comments
    FROM ig_media m
    LEFT JOIN (
      SELECT media_id, metric, value,
        ROW_NUMBER() OVER (PARTITION BY media_id, metric ORDER BY fetched_at DESC) AS rn
      FROM ig_media_insights
    ) i ON i.media_id = m.id AND i.rn = 1
    WHERE m.media_product_type != 'STORY' ${sinceClause} ${untilClause}
    GROUP BY m.id
    ORDER BY m.timestamp DESC
  `).all();
}

function getMediaByType(sinceExpr = null, untilExpr = null) {
  const sinceClause = sinceExpr ? `AND timestamp >= ${sinceExpr}` : "";
  const untilClause = untilExpr ? `AND timestamp < ${untilExpr}` : "";
  return db.prepare(`
    SELECT media_product_type, COUNT(*) as count
    FROM ig_media
    WHERE media_product_type != 'STORY' ${sinceClause} ${untilClause}
    GROUP BY media_product_type
  `).all();
}

function getAccountInsights(sinceExpr = null, untilExpr = null) {
  const conds = [];
  if (sinceExpr) conds.push(`end_time >= ${sinceExpr}`);
  if (untilExpr) conds.push(`end_time < ${untilExpr}`);
  const whereClause = conds.length ? `WHERE ${conds.join(" AND ")}` : "";
  return db.prepare(`
    SELECT metric, breakdown_dimension, breakdown_value, SUM(value) as value
    FROM ig_account_insights
    ${whereClause}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric
  `).all();
}

function getDemographics(metric, dimension) {
  return db.prepare(`
    SELECT dimension_value, value
    FROM ig_audience_demographics
    WHERE metric = ? AND dimension = ? AND timeframe = 'this_month'
      AND fetched_at = (
        SELECT MAX(fetched_at) FROM ig_audience_demographics
        WHERE metric = ? AND dimension = ? AND timeframe = 'this_month'
      )
    ORDER BY value DESC
  `).all(metric, dimension, metric, dimension);
}

function getIgnoredUsernames() {
  return db.prepare("SELECT username FROM ig_ignored_accounts").all()
    .map(r => r.username.toLowerCase());
}

function getAllComments() {
  return db.prepare(`
    SELECT c.username, c.text, c.parent_comment_id, c.timestamp, c.like_count,
      CASE
        WHEN c.parent_comment_id IS NOT NULL THEN p.timestamp
        ELSE m.timestamp
      END AS ref_timestamp
    FROM ig_comments c
    LEFT JOIN ig_media m ON m.id = c.media_id
    LEFT JOIN ig_comments p ON p.id = c.parent_comment_id
    WHERE c.hidden = 0
  `).all();
}

function getCommentsSince(sinceExpr = `datetime(${SQL_NOW}, '-30 days')`, untilExpr = null) {
  const untilClause = untilExpr ? `AND c.timestamp < ${untilExpr}` : "";
  return db.prepare(`
    SELECT c.username, c.text, c.parent_comment_id, c.timestamp, c.like_count,
      CASE
        WHEN c.parent_comment_id IS NOT NULL THEN p.timestamp
        ELSE m.timestamp
      END AS ref_timestamp
    FROM ig_comments c
    LEFT JOIN ig_media m ON m.id = c.media_id
    LEFT JOIN ig_comments p ON p.id = c.parent_comment_id
    WHERE c.hidden = 0
      AND c.timestamp >= ${sinceExpr} ${untilClause}
  `).all();
}

// ============================================================
// Engagement ranking
// ============================================================

function buildRanking(comments, ignoredUsernames) {
  const users = {};

  for (const c of comments) {
    if (!c.username) continue;
    const uname = c.username.toLowerCase();
    if (ignoredUsernames.includes(uname)) continue;

    if (!users[uname]) {
      users[uname] = {
        username: c.username,
        score: 0,
        textComments: 0,
        emojiComments: 0,
        textReplies: 0,
        emojiReplies: 0,
        earlyCount: 0,
        total: 0,
      };
    }

    const isReply = !!c.parent_comment_id;
    const emojiOnly = isEmojiOnly(c.text);
    const early = isEarly(c);
    const points = scoreComment(c);

    users[uname].score += points;
    users[uname].total++;
    if (early) users[uname].earlyCount++;

    if (isReply) {
      if (emojiOnly) users[uname].emojiReplies++;
      else users[uname].textReplies++;
    } else {
      if (emojiOnly) users[uname].emojiComments++;
      else users[uname].textComments++;
    }
  }

  return Object.values(users).sort((a, b) => b.score - a.score);
}

// ============================================================
// HTML helpers
// ============================================================

function esc(str) {
  if (!str) return "";
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function truncate(str, len = 80) {
  if (!str) return "";
  return str.length > len ? str.substring(0, len) + "..." : str;
}

function fmtNum(n) {
  if (n === null || n === undefined) return "-";
  return Number(n).toLocaleString();
}

function rawNum(n) {
  if (n === null || n === undefined) return "";
  return String(n);
}

function fmtDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

// ============================================================
// HTML generation
// ============================================================

function generateReport(config = {}) {
  const {
    sinceExpr = `datetime(${SQL_NOW}, '-30 days')`,
    untilExpr = null,
    periodLabel = "Last 30 Days",
    periodLabel3 = "Last 30 Days",   // label for the 3rd row in post counts / growth
    reportTitle = "Instagram Analytics Report",
    outputPath = path.join(OUTPUT_DIR, "engagement-report.html"),
    excelPath = path.join(OUTPUT_DIR, "Engagement Rankings.xlsx"),
    rankingsPath = path.join(OUTPUT_DIR, "engagement-rankings.html"),
    filterMedia = false,
    mode = "rolling",
  } = config;

  const account = getAccount();
  const snapshots = getAccountSnapshots();
  const media = filterMedia ? getMediaPerformance(sinceExpr, untilExpr) : getMediaPerformance();
  const mediaByType = getMediaByType(sinceExpr, untilExpr);
  const ignoredUsernames = getIgnoredUsernames();
  const allComments = getAllComments();
  const periodComments = getCommentsSince(sinceExpr, untilExpr);
  const rankAllTime = buildRanking(allComments, ignoredUsernames);
  const rankPeriod = buildRanking(periodComments, ignoredUsernames);
  const accountInsights = getAccountInsights(sinceExpr, untilExpr);

  const demoAge = getDemographics("follower_demographics", "age");
  const demoCountry = getDemographics("follower_demographics", "country");
  const demoGender = getDemographics("follower_demographics", "gender");
  const demoCity = getDemographics("follower_demographics", "city");

  const insightsByMetric = {};
  for (const row of accountInsights) {
    const key = row.metric;
    if (!insightsByMetric[key]) insightsByMetric[key] = { total: 0, breakdowns: {} };
    insightsByMetric[key].total += row.value;
    if (row.breakdown_value) {
      insightsByMetric[key].breakdowns[row.breakdown_value] =
        (insightsByMetric[key].breakdowns[row.breakdown_value] || 0) + row.value;
    }
  }

  const followerGrowth = getFollowerGrowth(mode);
  const postCounts = getPostCountsByPeriod(mode, untilExpr);
  const topCommentersDetailed = getTopCommentersWithPosts(sinceExpr, untilExpr);
  const sharesLeaderboard = getSharesLeaderboard(sinceExpr, untilExpr);

  const totalReach = media.reduce((s, m) => s + (m.reach || 0), 0);
  const totalViews = media.reduce((s, m) => s + (m.views || 0), 0);
  const totalInteractions = media.reduce((s, m) => s + (m.total_interactions || 0), 0);
  const avgReach = media.length ? Math.round(totalReach / media.length) : 0;

  const generatedAt = new Date().toLocaleString("en-US", {
    dateStyle: "medium", timeStyle: "short",
  });

  // Pre-compute Shares tab HTML
  const sharesTableHtml = sharesLeaderboard.length ? `
    <div class="card" style="overflow-x: auto;">
      <table class="sortable" id="shares-table">
        <thead>
          <tr>
            <th data-type="number"># <span class="sort-arrow">&#9650;</span></th>
            <th data-type="number">Shares <span class="sort-arrow">&#9650;</span></th>
            <th data-type="string">Type <span class="sort-arrow">&#9650;</span></th>
            <th data-type="string">Caption <span class="sort-arrow">&#9650;</span></th>
            <th data-type="date">Date <span class="sort-arrow">&#9650;</span></th>
            <th class="no-sort">Link</th>
          </tr>
        </thead>
        <tbody>
          ${sharesLeaderboard.map((s, i) => `
            <tr>
              <td data-sort="${i + 1}"><span class="rank ${i < 3 ? "rank-" + (i + 1) : ""}">${i + 1}</span></td>
              <td data-sort="${s.shares}" class="score">${fmtNum(s.shares)}</td>
              <td data-sort="${esc(s.media_product_type || "FEED")}"><span class="badge badge-${(s.media_product_type || "feed").toLowerCase()}">${esc(s.media_product_type || "FEED")}</span></td>
              <td class="caption" data-sort="${esc(truncate(s.caption, 60))}">${esc(truncate(s.caption, 60))}</td>
              <td data-sort="${s.timestamp || ""}">${fmtDate(s.timestamp)}</td>
              <td>${s.permalink ? `<a href="${esc(s.permalink)}" target="_blank">View</a>` : "-"}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  ` : '<div class="card"><p style="color:var(--muted)">No shares data available yet.</p></div>';

  // Pre-compute Commenters tab HTML
  const commentersTableHtml = `
    <div class="card" style="overflow-x: auto;">
      <table class="sortable" id="commenters-table">
        <thead>
          <tr>
            <th data-type="number"># <span class="sort-arrow">&#9650;</span></th>
            <th data-type="string">Username <span class="sort-arrow">&#9650;</span></th>
            <th data-type="number">Comments <span class="sort-arrow">&#9650;</span></th>
            <th data-type="number">Replies <span class="sort-arrow">&#9650;</span></th>
            <th data-type="number">Total <span class="sort-arrow">&#9650;</span></th>
            <th class="no-sort">Top Posts Commented On</th>
          </tr>
        </thead>
        <tbody>
          ${topCommentersDetailed.map((u, i) => {
            const topPostsHtml = Object.entries(u.posts)
              .sort((a, b) => b[1].count - a[1].count)
              .slice(0, 3)
              .map(([id, d]) => d.count + 'x on "' + esc(truncate(d.caption, 25)) + '"')
              .join('<br>');
            return `
            <tr>
              <td data-sort="${i + 1}"><span class="rank ${i < 3 ? "rank-" + (i + 1) : ""}">${i + 1}</span></td>
              <td data-sort="${esc(u.username.toLowerCase())}"><strong>@${esc(u.username)}</strong></td>
              <td data-sort="${u.total - u.replies}">${fmtNum(u.total - u.replies)}</td>
              <td data-sort="${u.replies}">${fmtNum(u.replies)}</td>
              <td data-sort="${u.total}" class="score">${fmtNum(u.total)}</td>
              <td style="font-size:12px;color:var(--muted);max-width:250px;">${topPostsHtml}</td>
            </tr>`;
          }).join("")}
        </tbody>
      </table>
    </div>
  `;

  const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Peace Grappler - ${esc(reportTitle)}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"><\/script>
<style>
  :root {
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #2a2d3a;
    --text: #e1e4ed;
    --muted: #8b8fa3;
    --accent: #6366f1;
    --accent2: #818cf8;
    --green: #34d399;
    --orange: #fb923c;
    --red: #f87171;
    --pink: #f472b6;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text);
    line-height: 1.6; padding: 0; margin: 0;
  }
  .page-header {
    padding: 24px 24px 0;
    max-width: 1400px; margin: 0 auto;
  }
  .page-content {
    padding: 0 24px 24px;
    max-width: 1400px; margin: 0 auto;
  }
  h1 { font-size: 28px; margin-bottom: 4px; }
  h2 { font-size: 20px; margin: 32px 0 16px; color: var(--accent2); }
  h2:first-child { margin-top: 8px; }
  h3 { font-size: 16px; margin: 24px 0 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; }
  .subtitle { color: var(--muted); font-size: 14px; margin-bottom: 16px; }

  /* Top navigation tabs */
  .nav-tabs {
    display: flex; gap: 0;
    border-bottom: 2px solid var(--border);
    margin-bottom: 24px;
  }
  .nav-tab {
    padding: 12px 24px; cursor: pointer;
    color: var(--muted); font-size: 15px; font-weight: 600;
    border-bottom: 2px solid transparent;
    margin-bottom: -2px; transition: all 0.2s;
    user-select: none;
  }
  .nav-tab:hover { color: var(--text); }
  .nav-tab.active { color: var(--accent2); border-bottom-color: var(--accent2); }
  .page-panel { display: none; }
  .page-panel.active { display: block; }

  .site-nav { display: flex; align-items: center; gap: 16px; padding: 12px 0; margin-bottom: 12px; border-bottom: 1px solid var(--border); }
  .site-nav a { color: var(--accent2); text-decoration: none; font-size: 13px; font-weight: 600; }
  .site-nav a:hover { opacity: 0.8; }
  .site-nav-label { color: var(--muted); font-size: 13px; }

  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px; text-align: center;
  }
  .stat-card .value { font-size: 32px; font-weight: 700; color: var(--accent2); }
  .stat-card .label { font-size: 13px; color: var(--muted); margin-top: 4px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 24px; margin-bottom: 24px;
  }
  .chart-container { position: relative; height: 300px; margin: 16px 0; }
  .chart-row { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 24px; }
  .chart-row .card { margin-bottom: 0; }
  @media (max-width: 768px) { .chart-row { grid-template-columns: 1fr; } }

  /* Sortable tables */
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); }
  th {
    color: var(--muted); font-weight: 600; font-size: 12px;
    text-transform: uppercase; letter-spacing: 0.5px;
    cursor: pointer; user-select: none; white-space: nowrap;
  }
  th:hover { color: var(--text); }
  th .sort-arrow { margin-left: 4px; font-size: 10px; opacity: 0.4; }
  th.sorted .sort-arrow { opacity: 1; color: var(--accent2); }
  th.no-sort { cursor: default; }
  th.no-sort:hover { color: var(--muted); }
  tr:hover { background: rgba(99,102,241,0.05); }

  .rank { font-weight: 700; color: var(--accent2); min-width: 36px; display: inline-block; }
  .rank-1 { color: #fbbf24; }
  .rank-2 { color: #d1d5db; }
  .rank-3 { color: #cd7f32; }
  .score { font-weight: 700; color: var(--green); }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 6px;
    font-size: 11px; font-weight: 600; text-transform: uppercase;
  }
  .badge-feed { background: #6366f133; color: var(--accent2); }
  .badge-reels { background: #f472b633; color: var(--pink); }
  .badge-carousel { background: #fb923c33; color: var(--orange); }
  .caption { color: var(--muted); font-size: 13px; max-width: 300px; }
  a { color: var(--accent2); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .breakdown { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 8px; }
  .breakdown-item { font-size: 13px; }
  .breakdown-item .bval { font-weight: 600; color: var(--text); }
  .breakdown-item .blabel { color: var(--muted); }

  /* Sub-tabs (for ranking periods) */
  .sub-tabs { display: flex; gap: 0; margin-bottom: 0; }
  .sub-tab {
    padding: 10px 20px; cursor: pointer; border: 1px solid var(--border);
    background: var(--bg); color: var(--muted); font-size: 14px; font-weight: 600;
    border-bottom: none; border-radius: 8px 8px 0 0; margin-right: -1px;
    user-select: none;
  }
  .sub-tab.active { background: var(--card); color: var(--accent2); }
  .sub-content { display: none; }
  .sub-content.active { display: block; }

  .scoring-legend { font-size: 13px; color: var(--muted); margin-bottom: 16px; }
  .scoring-legend span { color: var(--text); font-weight: 600; }
</style>
</head>
<body>

<div class="page-header">
  <div class="site-nav">
    <a href="index.html">&#8592; All Reports</a>
    <span class="site-nav-label">PeaceGrappler</span>
  </div>
  <h1>@${esc(account?.username || "peacegrappler")}</h1>
  <div class="subtitle">${esc(account?.name || "")} &mdash; Report generated ${generatedAt}</div>
  <div class="nav-tabs">
    <div class="nav-tab active" onclick="switchPage('overview')">Overview</div>
    <div class="nav-tab" onclick="switchPage('posts')">Post Performance</div>
    <div class="nav-tab" onclick="switchPage('shares')">Shares &amp; Reposts</div>
    <div class="nav-tab" onclick="switchPage('commenters')">Top Commenters</div>
    <div class="nav-tab" onclick="switchPage('rankings')">Community Rankings</div>
  </div>
</div>

<div class="page-content">

<!-- ==================== TAB 1: OVERVIEW ==================== -->
<div id="page-overview" class="page-panel active">

  <div class="stats-grid">
    <div class="stat-card">
      <div class="value">${fmtNum(account?.followers_count)}</div>
      <div class="label">Followers</div>
    </div>
    <div class="stat-card">
      <div class="value">${fmtNum(account?.follows_count)}</div>
      <div class="label">Following</div>
    </div>
    <div class="stat-card">
      <div class="value">${fmtNum(account?.media_count)}</div>
      <div class="label">Posts</div>
    </div>
    <div class="stat-card">
      <div class="value">${fmtNum(totalReach)}</div>
      <div class="label">Total Reach</div>
    </div>
    <div class="stat-card">
      <div class="value">${fmtNum(totalViews)}</div>
      <div class="label">Total Views</div>
    </div>
    <div class="stat-card">
      <div class="value">${fmtNum(totalInteractions)}</div>
      <div class="label">Total Interactions</div>
    </div>
    <div class="stat-card">
      <div class="value">${fmtNum(avgReach)}</div>
      <div class="label">Avg Reach / Post</div>
    </div>
    <div class="stat-card">
      <div class="value">${fmtNum(allComments.length)}</div>
      <div class="label">Total Comments</div>
    </div>
  </div>

  <h2>Follower Growth</h2>
  <div class="stats-grid">
    <div class="stat-card">
      <div class="value" style="color: ${followerGrowth.yesterday.val > 0 ? 'var(--green)' : followerGrowth.yesterday.val < 0 ? 'var(--red)' : 'var(--muted)'}">${followerGrowth.yesterday.str}</div>
      <div class="label">Yesterday</div>
    </div>
    <div class="stat-card">
      <div class="value" style="color: ${followerGrowth.last7.val > 0 ? 'var(--green)' : followerGrowth.last7.val < 0 ? 'var(--red)' : 'var(--muted)'}">${followerGrowth.last7.str}</div>
      <div class="label">Last 7 Days</div>
    </div>
    <div class="stat-card">
      <div class="value" style="color: ${followerGrowth.period.val > 0 ? 'var(--green)' : followerGrowth.period.val < 0 ? 'var(--red)' : 'var(--muted)'}">${followerGrowth.period.str}</div>
      <div class="label">${esc(periodLabel3)}</div>
    </div>
  </div>

  <h2>Content Published</h2>
  <div class="card" style="overflow-x: auto;">
    <table>
      <thead><tr><th>Period</th><th>Total</th><th>Reels</th><th>Feed</th></tr></thead>
      <tbody>
        <tr><td>Yesterday</td><td>${fmtNum(postCounts.yesterday?.total)}</td><td>${fmtNum(postCounts.yesterday?.reels)}</td><td>${fmtNum(postCounts.yesterday?.feed)}</td></tr>
        <tr><td>Last 7 Days</td><td>${fmtNum(postCounts.last7?.total)}</td><td>${fmtNum(postCounts.last7?.reels)}</td><td>${fmtNum(postCounts.last7?.feed)}</td></tr>
        <tr><td>${esc(periodLabel3)}</td><td>${fmtNum(postCounts.period?.total)}</td><td>${fmtNum(postCounts.period?.reels)}</td><td>${fmtNum(postCounts.period?.feed)}</td></tr>
      </tbody>
    </table>
  </div>

  ${snapshots.length > 1 ? `
  <h2>Account Growth</h2>
  <div class="card">
    <div class="chart-container"><canvas id="growthChart"></canvas></div>
  </div>
  ` : ""}

  <h2>Content Overview</h2>
  <div class="chart-row">
    <div class="card">
      <h3>Content Mix</h3>
      <div class="chart-container"><canvas id="contentMixChart"></canvas></div>
    </div>
    <div class="card">
      <h3>Reach by Post</h3>
      <div class="chart-container"><canvas id="reachChart"></canvas></div>
    </div>
  </div>

  ${Object.keys(insightsByMetric).length ? `
  <h2>Account Insights (${esc(periodLabel)})</h2>
  <div class="stats-grid">
    ${Object.entries(insightsByMetric).map(([metric, data]) => `
      <div class="stat-card">
        <div class="value">${fmtNum(data.total)}</div>
        <div class="label">${esc(metric.replace(/_/g, " "))}</div>
        ${Object.keys(data.breakdowns).length ? `
          <div class="breakdown">
            ${Object.entries(data.breakdowns).map(([k, v]) => `
              <div class="breakdown-item"><span class="bval">${fmtNum(v)}</span> <span class="blabel">${esc(k)}</span></div>
            `).join("")}
          </div>
        ` : ""}
      </div>
    `).join("")}
  </div>
  ` : ""}

  ${demoAge.length ? `
  <h2>Audience Demographics</h2>
  <div class="chart-row">
    <div class="card">
      <h3>Age Distribution</h3>
      <div class="chart-container"><canvas id="ageChart"></canvas></div>
    </div>
    <div class="card">
      <h3>Gender Distribution</h3>
      <div class="chart-container"><canvas id="genderChart"></canvas></div>
    </div>
  </div>
  <div class="chart-row">
    <div class="card">
      <h3>Top Countries</h3>
      <div class="chart-container"><canvas id="countryChart"></canvas></div>
    </div>
    <div class="card">
      <h3>Top Cities</h3>
      <div class="chart-container"><canvas id="cityChart"></canvas></div>
    </div>
  </div>
  ` : ""}

</div>

<!-- ==================== TAB 2: POST PERFORMANCE ==================== -->
<div id="page-posts" class="page-panel">

  <h2>Post Performance</h2>
  <div class="card" style="overflow-x: auto;">
    <table class="sortable" id="posts-table">
      <thead>
        <tr>
          <th data-type="date">Date <span class="sort-arrow">&#9650;</span></th>
          <th data-type="string">Type <span class="sort-arrow">&#9650;</span></th>
          <th data-type="string">Caption <span class="sort-arrow">&#9650;</span></th>
          <th data-type="number">Reach <span class="sort-arrow">&#9650;</span></th>
          <th data-type="number">Views <span class="sort-arrow">&#9650;</span></th>
          <th data-type="number">Likes <span class="sort-arrow">&#9650;</span></th>
          <th data-type="number">Comments <span class="sort-arrow">&#9650;</span></th>
          <th data-type="number">Shares <span class="sort-arrow">&#9650;</span></th>
          <th data-type="number">Saves <span class="sort-arrow">&#9650;</span></th>
          <th class="no-sort">Link</th>
        </tr>
      </thead>
      <tbody>
        ${media.map(m => `
          <tr>
            <td data-sort="${m.timestamp || ""}">${fmtDate(m.timestamp)}</td>
            <td data-sort="${esc(m.media_product_type || m.media_type)}"><span class="badge badge-${(m.media_product_type || "feed").toLowerCase()}">${esc(m.media_product_type || m.media_type)}</span></td>
            <td class="caption" data-sort="${esc(truncate(m.caption, 60))}">${esc(truncate(m.caption, 60))}</td>
            <td data-sort="${rawNum(m.reach)}">${fmtNum(m.reach)}</td>
            <td data-sort="${rawNum(m.views)}">${fmtNum(m.views)}</td>
            <td data-sort="${rawNum(m.insight_likes || m.like_count)}">${fmtNum(m.insight_likes || m.like_count)}</td>
            <td data-sort="${rawNum(m.insight_comments || m.comments_count)}">${fmtNum(m.insight_comments || m.comments_count)}</td>
            <td data-sort="${rawNum(m.shares)}">${fmtNum(m.shares)}</td>
            <td data-sort="${rawNum(m.saved)}">${fmtNum(m.saved)}</td>
            <td>${m.permalink ? `<a href="${esc(m.permalink)}" target="_blank">View</a>` : "-"}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  </div>

</div>

<!-- ==================== TAB 3: SHARES & REPOSTS ==================== -->
<div id="page-shares" class="page-panel">
  <h2>Top Shared &amp; Reposted Content (${esc(periodLabel)})</h2>
  ${sharesTableHtml}
</div>

<!-- ==================== TAB 4: TOP COMMENTERS ==================== -->
<div id="page-commenters" class="page-panel">
  <h2>Top Active Commenters (${esc(periodLabel)})</h2>
  ${commentersTableHtml}
</div>

<!-- ==================== TAB 5: COMMUNITY ENGAGEMENT ==================== -->
<div id="page-rankings" class="page-panel">

  <h2>Community Engagement Rankings</h2>
  <div class="scoring-legend">
    Scoring: <span>Text comment = 5 pts</span> &bull;
    <span>Text reply = 7 pts</span> &bull;
    <span>Emoji comment = 1 pt</span> &bull;
    <span>Emoji reply = 2 pts</span><br>
    <span style="color: var(--orange);">2x bonus</span> for interactions within 30 minutes of post/comment creation
  </div>

  <div class="sub-tabs">
    <div class="sub-tab active" onclick="switchSubTab(this, 'ranking-alltime')">All Time</div>
    <div class="sub-tab" onclick="switchSubTab(this, 'ranking-last30')">${esc(periodLabel)}</div>
  </div>

  <div class="card" style="border-top-left-radius: 0; overflow-x: auto;">
    <div id="ranking-alltime" class="sub-content active">
      ${rankingTable(rankAllTime, "ranking-alltime-table")}
    </div>
    <div id="ranking-last30" class="sub-content">
      ${rankingTable(rankPeriod, "ranking-last30-table")}
    </div>
  </div>

</div>

</div><!-- .page-content -->

<script>
Chart.defaults.color = '#8b8fa3';
Chart.defaults.borderColor = '#2a2d3a';
const COLORS = ['#6366f1','#818cf8','#34d399','#fb923c','#f472b6','#fbbf24','#f87171','#a78bfa','#38bdf8','#4ade80'];
let chartsInitialized = false;

// ---- Page navigation ----
function switchPage(id) {
  document.querySelectorAll('.page-panel').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
  document.getElementById('page-' + id).classList.add('active');
  event.target.classList.add('active');
  if (id === 'overview' && !chartsInitialized) initCharts();
}

// ---- Ranking sub-tabs ----
function switchSubTab(btn, id) {
  const container = btn.closest('.page-panel');
  container.querySelectorAll('.sub-content').forEach(el => el.classList.remove('active'));
  container.querySelectorAll('.sub-tab').forEach(el => el.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}

// ---- Sortable tables ----
function makeSortable(table) {
  const headers = table.querySelectorAll('th:not(.no-sort)');
  headers.forEach((th, colIdx) => {
    th.addEventListener('click', () => {
      const tbody = table.querySelector('tbody');
      const rows = Array.from(tbody.querySelectorAll('tr'));
      const type = th.dataset.type || 'string';
      const currentDir = th.dataset.dir === 'asc' ? 'desc' : 'asc';

      // Reset all headers in this table
      headers.forEach(h => { h.dataset.dir = ''; h.classList.remove('sorted'); h.querySelector('.sort-arrow').innerHTML = '&#9650;'; });
      th.dataset.dir = currentDir;
      th.classList.add('sorted');
      th.querySelector('.sort-arrow').innerHTML = currentDir === 'asc' ? '&#9650;' : '&#9660;';

      rows.sort((a, b) => {
        const aVal = a.cells[colIdx].dataset.sort ?? a.cells[colIdx].textContent.trim();
        const bVal = b.cells[colIdx].dataset.sort ?? b.cells[colIdx].textContent.trim();

        let cmp = 0;
        if (type === 'number') {
          const aNum = parseFloat(aVal.replace(/,/g, '')) || 0;
          const bNum = parseFloat(bVal.replace(/,/g, '')) || 0;
          cmp = aNum - bNum;
        } else {
          cmp = aVal.localeCompare(bVal, undefined, { numeric: true });
        }
        return currentDir === 'asc' ? cmp : -cmp;
      });

      // Re-number rank column if present
      const hasRank = rows[0]?.cells[0]?.querySelector('.rank');
      rows.forEach((row, i) => {
        tbody.appendChild(row);
        if (hasRank) {
          const rankEl = row.cells[0].querySelector('.rank');
          if (rankEl) {
            rankEl.textContent = i + 1;
            rankEl.className = 'rank' + (i < 3 ? ' rank-' + (i + 1) : '');
          }
        }
      });
    });
  });
}

// Init sortable on all tables
document.querySelectorAll('table.sortable').forEach(makeSortable);

// ---- Charts (lazy init on overview tab) ----
function initCharts() {
  chartsInitialized = true;

  ${snapshots.length > 1 ? `
  new Chart(document.getElementById('growthChart'), {
    type: 'line',
    data: {
      labels: ${JSON.stringify(snapshots.map(s => s.snapshot_date))},
      datasets: [{
        label: 'Followers',
        data: ${JSON.stringify(snapshots.map(s => s.followers_count))},
        borderColor: '#6366f1', backgroundColor: '#6366f133',
        fill: true, tension: 0.3, pointRadius: 2,
      }]
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: false } }
    }
  });
  ` : ""}

  new Chart(document.getElementById('contentMixChart'), {
    type: 'doughnut',
    data: {
      labels: ${JSON.stringify(mediaByType.map(m => m.media_product_type || "FEED"))},
      datasets: [{ data: ${JSON.stringify(mediaByType.map(m => m.count))}, backgroundColor: COLORS }]
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom' } }
    }
  });

  const reachMedia = ${JSON.stringify(
    [...media].reverse().map(m => ({
      date: fmtDate(m.timestamp),
      reach: m.reach || 0,
      type: m.media_product_type || "FEED",
    }))
  )};
  new Chart(document.getElementById('reachChart'), {
    type: 'bar',
    data: {
      labels: reachMedia.map(m => m.date),
      datasets: [{
        label: 'Reach',
        data: reachMedia.map(m => m.reach),
        backgroundColor: reachMedia.map(m =>
          m.type === 'REELS' ? '#f472b6' : m.type === 'FEED' ? '#6366f1' : '#fb923c'
        ),
        borderRadius: 4,
      }]
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { display: false } }
    }
  });

  ${demoAge.length ? `
  new Chart(document.getElementById('ageChart'), {
    type: 'bar',
    data: {
      labels: ${JSON.stringify(demoAge.map(d => d.dimension_value))},
      datasets: [{ data: ${JSON.stringify(demoAge.map(d => d.value))}, backgroundColor: '#6366f1', borderRadius: 4 }]
    },
    options: { responsive: true, maintainAspectRatio: false, indexAxis: 'y',
      plugins: { legend: { display: false } }
    }
  });

  new Chart(document.getElementById('genderChart'), {
    type: 'doughnut',
    data: {
      labels: ${JSON.stringify(demoGender.map(d => d.dimension_value === "M" ? "Male" : d.dimension_value === "F" ? "Female" : d.dimension_value))},
      datasets: [{ data: ${JSON.stringify(demoGender.map(d => d.value))}, backgroundColor: ['#6366f1','#f472b6','#fb923c'] }]
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom' } }
    }
  });

  new Chart(document.getElementById('countryChart'), {
    type: 'bar',
    data: {
      labels: ${JSON.stringify(demoCountry.slice(0, 10).map(d => d.dimension_value))},
      datasets: [{ data: ${JSON.stringify(demoCountry.slice(0, 10).map(d => d.value))}, backgroundColor: '#34d399', borderRadius: 4 }]
    },
    options: { responsive: true, maintainAspectRatio: false, indexAxis: 'y',
      plugins: { legend: { display: false } }
    }
  });

  new Chart(document.getElementById('cityChart'), {
    type: 'bar',
    data: {
      labels: ${JSON.stringify(demoCity.slice(0, 10).map(d => d.dimension_value))},
      datasets: [{ data: ${JSON.stringify(demoCity.slice(0, 10).map(d => d.value))}, backgroundColor: '#fb923c', borderRadius: 4 }]
    },
    options: { responsive: true, maintainAspectRatio: false, indexAxis: 'y',
      plugins: { legend: { display: false } }
    }
  });
  ` : ""}
}

// Init charts immediately since overview is the default tab
initCharts();
<\/script>

</body>
</html>`;

  fs.writeFileSync(outputPath, html);
  console.log(`Report generated: ${outputPath}`);

  // Generate Excel and HTML rankings files
  generateExcel(rankAllTime, rankPeriod, excelPath, periodLabel);
  generateRankingsHtml(rankAllTime, rankPeriod, generatedAt, rankingsPath, periodLabel);
}

function rankingTable(ranking, tableId) {
  if (!ranking.length) return "<p style='color:var(--muted)'>No data available.</p>";

  return `
    <table class="sortable" id="${tableId}">
      <thead>
        <tr>
          <th data-type="number"># <span class="sort-arrow">&#9650;</span></th>
          <th data-type="string">Username <span class="sort-arrow">&#9650;</span></th>
          <th data-type="number">Score <span class="sort-arrow">&#9650;</span></th>
          <th data-type="number">Early <span class="sort-arrow">&#9650;</span></th>
          <th data-type="number">Text Comments <span class="sort-arrow">&#9650;</span></th>
          <th data-type="number">Emoji Comments <span class="sort-arrow">&#9650;</span></th>
          <th data-type="number">Text Replies <span class="sort-arrow">&#9650;</span></th>
          <th data-type="number">Emoji Replies <span class="sort-arrow">&#9650;</span></th>
          <th data-type="number">Total <span class="sort-arrow">&#9650;</span></th>
        </tr>
      </thead>
      <tbody>
        ${ranking.slice(0, 50).map((u, i) => `
          <tr>
            <td data-sort="${i + 1}"><span class="rank ${i < 3 ? "rank-" + (i + 1) : ""}">${i + 1}</span></td>
            <td data-sort="${esc(u.username.toLowerCase())}"><strong>@${esc(u.username)}</strong></td>
            <td data-sort="${u.score}" class="score">${fmtNum(u.score)}</td>
            <td data-sort="${u.earlyCount}" style="color: var(--orange);">${fmtNum(u.earlyCount)}</td>
            <td data-sort="${u.textComments}">${fmtNum(u.textComments)}</td>
            <td data-sort="${u.emojiComments}">${fmtNum(u.emojiComments)}</td>
            <td data-sort="${u.textReplies}">${fmtNum(u.textReplies)}</td>
            <td data-sort="${u.emojiReplies}">${fmtNum(u.emojiReplies)}</td>
            <td data-sort="${u.total}">${fmtNum(u.total)}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function rankingToRows(ranking) {
  return ranking.map(u => ({
    Username: u.username,
    Text: u.textComments + u.textReplies,
    Emoji: u.emojiComments + u.emojiReplies,
    Early: u.earlyCount,
  }));
}

function generateExcel(rankAllTime, rankPeriod, excelPath, periodLabel = "Last 30 Days") {
  const wb = XLSX.utils.book_new();

  const wsAllTime = XLSX.utils.json_to_sheet(rankingToRows(rankAllTime));
  XLSX.utils.book_append_sheet(wb, wsAllTime, "All Time");

  const wsPeriod = XLSX.utils.json_to_sheet(rankingToRows(rankPeriod));
  XLSX.utils.book_append_sheet(wb, wsPeriod, periodLabel);

  XLSX.writeFile(wb, excelPath);
  console.log(`Excel generated: ${excelPath}`);
}

function generateRankingsHtml(rankAllTime, rankPeriod, generatedAt, rankingsPath, periodLabel = "Last 30 Days") {
  const allTimeRows = rankingToRows(rankAllTime);
  const last30Rows = rankingToRows(rankPeriod);

  function tableHtml(rows) {
    if (!rows.length) return '<p style="color:#8b8fa3;">No data available.</p>';
    return `<table>
      <thead><tr><th>#</th><th>Username</th><th>Text</th><th>Emoji</th><th>Early</th></tr></thead>
      <tbody>${rows.map((r, i) => `
        <tr>
          <td>${i + 1}</td>
          <td>@${esc(r.Username)}</td>
          <td>${fmtNum(r.Text)}</td>
          <td>${fmtNum(r.Emoji)}</td>
          <td>${fmtNum(r.Early)}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
  }

  const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Community Engagement Rankings</title>
<style>
  :root { --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a; --text: #e1e4ed; --muted: #8b8fa3; --accent: #6366f1; --accent2: #818cf8; --green: #34d399; --orange: #fb923c; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 24px; }
  h1 { font-size: 28px; margin-bottom: 24px; }
  .site-nav { display: flex; align-items: center; gap: 16px; padding: 0 0 12px; margin-bottom: 16px; border-bottom: 1px solid var(--border); }
  .site-nav a { color: var(--accent2); text-decoration: none; font-size: 13px; font-weight: 600; }
  .site-nav a:hover { opacity: 0.8; }
  .site-nav-label { color: var(--muted); font-size: 13px; }
  .tabs { display: flex; gap: 0; margin-bottom: 0; }
  .tab { padding: 10px 20px; cursor: pointer; border: 1px solid var(--border); background: var(--bg); color: var(--muted); font-size: 14px; font-weight: 600; border-bottom: none; border-radius: 8px 8px 0 0; margin-right: -1px; user-select: none; }
  .tab.active { background: var(--card); color: var(--accent2); }
  .panel { display: none; background: var(--card); border: 1px solid var(--border); border-radius: 0 12px 12px 12px; padding: 24px; }
  .panel.active { display: block; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
  tr:hover { background: rgba(99,102,241,0.05); }
  td:first-child { font-weight: 700; color: var(--accent2); }
</style>
</head>
<body>
  <div class="site-nav">
    <a href="index.html">&#8592; All Reports</a>
    <span class="site-nav-label">PeaceGrappler</span>
  </div>
  <h1>Community Engagement Rankings</h1>
  <div style="color: var(--muted); font-size: 14px; margin-bottom: 16px;">Report generated ${generatedAt}</div>
  <div class="tabs">
    <div class="tab active" onclick="switchTab('alltime')">All Time</div>
    <div class="tab" onclick="switchTab('last30')">${esc(periodLabel)}</div>
  </div>
  <div id="panel-alltime" class="panel active">${tableHtml(allTimeRows)}</div>
  <div id="panel-last30" class="panel">${tableHtml(last30Rows)}</div>
  <script>
    function switchTab(id) {
      document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.getElementById('panel-' + id).classList.add('active');
      event.target.classList.add('active');
    }
  <\/script>
</body>
</html>`;

  fs.writeFileSync(rankingsPath, html);
  console.log(`Rankings HTML generated: ${rankingsPath}`);
}

if (monthArg) {
  // --month YYYY-MM: regenerate one historical month against the current DB.
  // Brackets sinceExpr/untilExpr to that calendar month exactly.
  const [yr, mo] = monthArg.split("-").map(Number);
  const nextMonth = new Date(Date.UTC(yr, mo, 1)).toISOString().slice(0, 10);
  console.log(`\n=== Regenerating Monthly Report (${MONTH_LABEL}) ===`);
  generateReport({
    mode: "mtd",
    sinceExpr: `'${monthArg}-01'`,
    untilExpr: `'${nextMonth}'`,
    periodLabel: MONTH_LABEL,
    periodLabel3: "Month Total",
    reportTitle: `Instagram Analytics Report — ${MONTH_LABEL}`,
    outputPath: path.join(OUTPUT_DIR, `engagement-report-${YEAR_MONTH}.html`),
    excelPath: path.join(OUTPUT_DIR, `Engagement Rankings ${YEAR_MONTH}.xlsx`),
    rankingsPath: path.join(OUTPUT_DIR, `engagement-rankings-${YEAR_MONTH}.html`),
    filterMedia: true,
  });
} else {
  // Default: rolling 30-day + current month-to-date
  console.log("\n=== Rolling 30-Day Reports ===");
  generateReport({
    mode: "rolling",
    sinceExpr: `datetime(${SQL_NOW}, '-30 days')`,
    periodLabel: "Last 30 Days",
    periodLabel3: "Last 30 Days",
    reportTitle: "Instagram Analytics Report — Last 30 Days",
    outputPath: path.join(OUTPUT_DIR, "engagement-report.html"),
    excelPath: path.join(OUTPUT_DIR, "Engagement Rankings.xlsx"),
    rankingsPath: path.join(OUTPUT_DIR, "engagement-rankings.html"),
    filterMedia: false,
  });

  console.log(`\n=== Month-to-Date Reports (${MONTH_LABEL}) ===`);
  generateReport({
    mode: "mtd",
    sinceExpr: `date(${SQL_NOW}, 'start of month')`,
    periodLabel: MONTH_LABEL,
    periodLabel3: "Month Total",
    reportTitle: `Instagram Analytics Report — ${MONTH_LABEL}`,
    outputPath: path.join(OUTPUT_DIR, `engagement-report-${YEAR_MONTH}.html`),
    excelPath: path.join(OUTPUT_DIR, `Engagement Rankings ${YEAR_MONTH}.xlsx`),
    rankingsPath: path.join(OUTPUT_DIR, `engagement-rankings-${YEAR_MONTH}.html`),
    filterMedia: true,
  });
}
