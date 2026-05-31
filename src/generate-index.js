/**
 * Generates index.html for GitHub Pages with links to all reports.
 * Scans output/ for monthly report files and builds the navigation page.
 */

const fs = require("fs");
const path = require("path");

const ROOT_DIR = path.join(__dirname, "..");
const OUTPUT_DIR = path.join(ROOT_DIR, "output");

function getMonthlyReports() {
  const files = fs.readdirSync(OUTPUT_DIR);
  const months = new Set();

  for (const f of files) {
    const match = f.match(/engagement-report-(\d{4}-\d{2})\.html/);
    if (match) months.add(match[1]);
  }

  return [...months].sort().reverse(); // newest first
}

function monthLabel(ym) {
  const [year, month] = ym.split("-");
  const date = new Date(parseInt(year), parseInt(month) - 1);
  return date.toLocaleString("en-US", { month: "long", year: "numeric" });
}

function getVideoAnalyses() {
  const files = fs.readdirSync(OUTPUT_DIR);
  const analyses = [];

  for (const f of files) {
    const match = f.match(/^video-analysis-(\d{4}-\d{2}-\d{2})\.json$/);
    if (!match) continue;
    try {
      const data = JSON.parse(fs.readFileSync(path.join(OUTPUT_DIR, f), "utf-8"));
      analyses.push(data);
    } catch (e) {
      // skip malformed sidecar
    }
  }

  return analyses.sort((a, b) => b.date.localeCompare(a.date)); // newest first
}

function tierBadge(tier) {
  if (tier === "Top Performer") return `<span style="color:#34d399;font-size:11px;font-weight:700">🔥 Top</span>`;
  if (tier === "Average")       return `<span style="color:#f59e0b;font-size:11px;font-weight:700">📊 Avg</span>`;
  return                               `<span style="color:#f87171;font-size:11px;font-weight:700">⚠️ Weak</span>`;
}

const VA_RECENT_LIMIT = 7;

function vaCard(a) {
  const totalViews = a.reels.reduce((s, r) => s + (r.views || 0), 0);
  const totalReach = a.reels.reduce((s, r) => s + (r.reach || 0), 0);
  const fmtK = (n) => n >= 1000 ? (n / 1000).toFixed(1) + "K" : n.toLocaleString();
  const cardId = `va-${a.date}`;
  return `
  <div class="va-card" id="${cardId}">
    <div class="va-header">
      <span class="va-date">${a.date}</span>
      <span class="va-stat"><strong>${a.reels.length}</strong>reel${a.reels.length !== 1 ? "s" : ""}</span>
      <span class="va-spacer"></span>
      <span class="va-stat"><strong>${fmtK(totalViews)}</strong>views</span>
      <span class="va-stat"><strong>${fmtK(totalReach)}</strong>reach</span>
      <a href="video-analysis-${a.date}.html" class="link" style="font-size:13px;padding:5px 14px">View Analysis</a>
      ${a.reels.length ? `<button class="va-toggle" onclick="toggleVA('${cardId}', this)">Expand &#9662;</button>` : ""}
    </div>
    ${a.reels.length ? `
    <div class="va-table-wrap">
    <table class="va-table">
      <thead><tr>
        <th>Score</th><th>Caption</th><th>Posted</th><th>Views</th><th>Reach</th><th>Links</th>
      </tr></thead>
      <tbody>
        ${a.reels.map(r => `
        <tr>
          <td><span class="va-score">${r.score}</span> ${tierBadge(r.tier)}</td>
          <td class="va-caption">${(r.caption || "(no caption)").replace(/</g, "&lt;")}</td>
          <td style="color:var(--muted);white-space:nowrap">${r.timestamp || ""}</td>
          <td>${r.views != null ? r.views.toLocaleString() : "—"}</td>
          <td>${r.reach != null ? r.reach.toLocaleString() : "—"}</td>
          <td style="white-space:nowrap">
            <a href="${r.permalink}" target="_blank" class="link link-secondary" style="font-size:12px;padding:4px 10px">IG Video</a>
          </td>
        </tr>`).join("")}
      </tbody>
    </table>
    </div>` : ""}
  </div>`;
}

function pageShell(title, bodyHtml, generatedAt) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${title}</title>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a;
    --text: #e1e4ed; --muted: #8b8fa3;
    --accent: #6366f1; --accent2: #818cf8; --green: #34d399;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; padding: 24px; }
  .container { max-width: 800px; margin: 0 auto; }
  h1 { font-size: 32px; margin-bottom: 8px; color: var(--accent2); }
  .subtitle { color: var(--muted); font-size: 14px; margin-bottom: 32px; }
  h2 { font-size: 20px; margin: 32px 0 16px; color: var(--accent2); }
  .section { margin-bottom: 32px; }
  .section-header { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }
  .section-header h2 { margin: 0; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 12px; }
  .card h3 { font-size: 16px; margin-bottom: 8px; }
  .card p { color: var(--muted); font-size: 14px; margin-bottom: 12px; }
  .links { display: flex; gap: 12px; flex-wrap: wrap; }
  .link { display: inline-block; padding: 8px 16px; background: var(--accent); color: white; border-radius: 8px; text-decoration: none; font-size: 14px; font-weight: 600; transition: opacity 0.2s; }
  .link:hover { opacity: 0.85; }
  .link-secondary { background: transparent; border: 1px solid var(--border); color: var(--accent2); }
  .link-secondary:hover { background: var(--card); }
  .nav-back { display: inline-block; color: var(--accent2); text-decoration: none; font-size: 13px; font-weight: 600; margin-bottom: 12px; }
  .nav-back:hover { text-decoration: underline; }
  .month-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .month-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
  .month-card h3 { font-size: 16px; margin-bottom: 10px; color: var(--text); }
  .month-card .links { flex-direction: column; gap: 6px; }
  .month-card .link { text-align: center; font-size: 13px; padding: 6px 12px; }
  .footer { text-align: center; margin-top: 48px; padding-top: 24px; border-top: 1px solid var(--border); color: var(--muted); font-size: 12px; }
  .va-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px 16px; margin-bottom: 8px; }
  .va-header { display: grid; grid-template-columns: 110px auto 1fr auto auto auto auto; align-items: center; gap: 14px; }
  .va-date { font-size: 14px; font-weight: 600; color: var(--text); }
  .va-stat { color: var(--muted); font-size: 13px; white-space: nowrap; }
  .va-stat strong { color: var(--text); font-weight: 600; margin-right: 4px; }
  .va-toggle { background: transparent; border: 1px solid var(--border); color: var(--accent2); border-radius: 8px; padding: 5px 12px; font-size: 13px; font-weight: 600; cursor: pointer; font-family: inherit; }
  .va-toggle:hover { background: rgba(99,102,241,0.1); }
  .va-table-wrap { display: none; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border); }
  .va-card.open .va-table-wrap { display: block; }
  .va-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .va-table th { color: var(--muted); font-weight: 600; padding: 6px 10px; text-align: left; border-bottom: 1px solid var(--border); }
  .va-table td { padding: 7px 10px; border-bottom: 1px solid #1e2130; vertical-align: middle; }
  .va-table tr:last-child td { border-bottom: none; }
  .va-caption { color: var(--text); max-width: 340px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .va-score { font-weight: 700; font-size: 15px; }
  @media (max-width: 700px) {
    .va-header { grid-template-columns: 1fr 1fr; gap: 8px; }
    .va-spacer { display: none; }
  }
</style>
</head>
<body>
<div class="container">
${bodyHtml}
<div class="footer">
  <p>PeaceGrappler Analytics &mdash; Data from Instagram Graph API</p>
  <p>Last updated: ${generatedAt}</p>
</div>
</div>
<script>
function toggleVA(id, btn) {
  const card = document.getElementById(id);
  const open = card.classList.toggle("open");
  btn.innerHTML = open ? "Hide &#9652;" : "Expand &#9662;";
}
</script>
</body>
</html>`;
}

function generate() {
  const months   = getMonthlyReports();
  const analyses = getVideoAnalyses();
  const generatedAt = new Date().toLocaleString("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  });

  const recentAnalyses = analyses.slice(0, VA_RECENT_LIMIT);
  const hasMoreAnalyses = analyses.length > VA_RECENT_LIMIT;

  const indexBody = `
  <h1>PeaceGrappler</h1>
  <div class="subtitle">Instagram Analytics Reports &mdash; Updated ${generatedAt}</div>

  <div class="section">
    <h2>Rolling Reports (Last 30 Days)</h2>
    <div class="card">
      <h3>Engagement Report</h3>
      <p>Interactive report with post performance, engagement analytics, shares, commenters, and community rankings.</p>
      <div class="links">
        <a href="engagement-report.html" class="link">Full Report</a>
        <a href="engagement-rankings.html" class="link link-secondary">Community Rankings</a>
        <a href="Engagement Rankings.xlsx" class="link link-secondary">Download Excel</a>
      </div>
    </div>
    <div class="card">
      <h3>Comprehensive Growth Report</h3>
      <p>Comprehensive email report with follower growth, per-post metrics, top commenters, and UFC hype analysis.</p>
      <div class="links">
        <a href="comprehensive-growth-report.html" class="link">View Report</a>
      </div>
    </div>
    <div class="card">
      <h3>PeaceGrappler Insights</h3>
      <p>PDF-style dashboard: profile overview, reach/views/interaction breakdowns, demographics, follower geography, post and reel performance, and hashtag stats.</p>
      <div class="links">
        <a href="peacegrappler-insights.html" class="link">View Report</a>
      </div>
    </div>
  </div>

  ${analyses.length ? `
  <div class="section">
    <div class="section-header">
      <h2>Video Analysis Reports</h2>
      ${hasMoreAnalyses ? `<a href="video-analysis-index.html" class="link link-secondary">View all (${analyses.length}) &rarr;</a>` : ""}
    </div>
    <p style="color: var(--muted); font-size: 14px; margin-bottom: 16px;">
      Last ${recentAnalyses.length} day${recentAnalyses.length !== 1 ? "s" : ""} of reel analysis &mdash; engagement scores, watch time, and growth recommendations.
    </p>
    ${recentAnalyses.map(vaCard).join("")}
  </div>
  ` : ""}

  ${months.length ? `
  <div class="section">
    <h2>Engagement Monthly Reports</h2>
    <p style="color: var(--muted); font-size: 14px; margin-bottom: 16px;">
      Each month's report captures all activity from the 1st through the latest sync. Previous months are preserved.
    </p>
    <div class="month-grid">
      ${months.map(ym => {
        const compMonthly = fs.existsSync(path.join(OUTPUT_DIR, `comprehensive-growth-report-${ym}.html`));
        return `
      <div class="month-card">
        <h3>${monthLabel(ym)}</h3>
        <div class="links">
          <a href="engagement-report-${ym}.html" class="link">Full Report</a>
          <a href="engagement-rankings-${ym}.html" class="link link-secondary">Community Rankings</a>
          <a href="Engagement Rankings ${ym}.xlsx" class="link link-secondary">Download Excel</a>
          ${compMonthly ? `<a href="comprehensive-growth-report-${ym}.html" class="link link-secondary">Comprehensive Growth</a>` : ""}
        </div>
      </div>
      `;
      }).join("")}
    </div>
  </div>
  ` : ""}
  `;

  fs.writeFileSync(path.join(OUTPUT_DIR, "index.html"), pageShell("PeaceGrappler — Reports", indexBody, generatedAt));
  console.log(`Index page generated: ${path.join(OUTPUT_DIR, "index.html")}`);

  if (analyses.length) {
    const vaBody = `
    <a href="index.html" class="nav-back">&larr; All Reports</a>
    <h1>Video Analysis</h1>
    <div class="subtitle">${analyses.length} daily reports &mdash; Updated ${generatedAt}</div>
    <div class="section">
      ${analyses.map(vaCard).join("")}
    </div>`;
    fs.writeFileSync(path.join(OUTPUT_DIR, "video-analysis-index.html"), pageShell("PeaceGrappler — Video Analysis", vaBody, generatedAt));
    console.log(`Video analysis index generated: ${path.join(OUTPUT_DIR, "video-analysis-index.html")}`);
  }
}

generate();
