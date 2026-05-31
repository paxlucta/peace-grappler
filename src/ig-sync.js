const path = require("path");
const ROOT_DIR = path.join(__dirname, "..");
require("dotenv").config({ path: path.join(ROOT_DIR, ".env") });
const Database = require("better-sqlite3");
const https = require("https");

const DB_PATH = path.join(ROOT_DIR, "peacegrappler.db");
const API_VERSION = "v25.0";
const BASE_URL = `https://graph.facebook.com/${API_VERSION}`;
const USER_TOKEN = process.env.TOKEN;
let TOKEN = USER_TOKEN; // Will be replaced with page token after discovery

const db = new Database(DB_PATH);
db.pragma("journal_mode = WAL");
db.pragma("foreign_keys = ON");

// ============================================================
// HTTP helper
// ============================================================

// Network errors (ECONNRESET, ETIMEDOUT, etc.) are transient. Retry once
// with a short backoff so a single dropped TLS connection doesn't crash sync.
const TRANSIENT_NET_CODES = new Set(["ECONNRESET", "ETIMEDOUT", "EAI_AGAIN", "ECONNREFUSED", "ENOTFOUND"]);

function httpsGetJson(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, (res) => {
      let body = "";
      res.on("data", (chunk) => (body += chunk));
      res.on("end", () => {
        try { resolve(JSON.parse(body)); } catch (e) { reject(e); }
      });
      res.on("error", reject);
    });
    req.on("error", reject); // crucial: ECONNRESET fires here, not on res
    req.setTimeout(60_000, () => req.destroy(new Error("ETIMEDOUT")));
  });
}

async function httpsGetJsonRetry(url) {
  try {
    return await httpsGetJson(url);
  } catch (e) {
    if (TRANSIENT_NET_CODES.has(e.code)) {
      await new Promise((r) => setTimeout(r, 1500));
      return await httpsGetJson(url); // one retry
    }
    throw e;
  }
}

async function graphGet(endpoint, params = {}, opts = {}) {
  const { silent = false } = opts;
  const queryParams = { ...params, access_token: TOKEN };
  const qs = new URLSearchParams(queryParams).toString();
  const url = `${BASE_URL}${endpoint}?${qs}`;

  let json;
  try {
    json = await httpsGetJsonRetry(url);
  } catch (e) {
    if (!silent) console.error(`  Network error on ${endpoint}:`, e.message);
    throw e;
  }
  if (json.error) {
    if (!silent) console.error(`  API error on ${endpoint}:`, json.error.message);
    throw new Error(JSON.stringify(json.error));
  }
  return json;
}

async function graphGetAll(endpoint, params = {}) {
  const results = [];
  let data = await graphGet(endpoint, params);
  if (data.data) results.push(...data.data);
  else return data;

  while (data.paging && data.paging.next) {
    data = await fetchUrl(data.paging.next);
    if (data.data) results.push(...data.data);
  }
  return results;
}

async function fetchUrl(url) {
  const json = await httpsGetJsonRetry(url);
  if (json.error) throw new Error(JSON.stringify(json.error));
  return json;
}

// ============================================================
// Sync log helpers
// ============================================================

function getLastSync(entityType) {
  const row = db
    .prepare(
      `SELECT completed_at FROM sync_log
       WHERE entity_type = ? AND status = 'success'
       ORDER BY completed_at DESC LIMIT 1`
    )
    .get(entityType);
  return row ? row.completed_at : null;
}

function startSync(entityType, accountId) {
  const stmt = db.prepare(
    `INSERT INTO sync_log (entity_type, account_id, status) VALUES (?, ?, 'running')`
  );
  return stmt.run(entityType, accountId).lastInsertRowid;
}

function completeSync(syncId, recordCount) {
  db.prepare(
    `UPDATE sync_log SET completed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
     status = 'success', records_fetched = ? WHERE id = ?`
  ).run(recordCount, syncId);
}

function failSync(syncId, error) {
  db.prepare(
    `UPDATE sync_log SET completed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
     status = 'error', error_message = ? WHERE id = ?`
  ).run(error, syncId);
}

// ============================================================
// Sync: Account
// ============================================================

async function syncAccount() {
  const syncId = startSync("accounts", null);
  try {
    // User token -> Pages -> IG Business Account
    const pages = await graphGet("/me/accounts", { fields: "id,name,instagram_business_account" });
    const page = pages.data?.find((p) => p.instagram_business_account);
    if (!page) throw new Error("No Instagram Business Account linked to any Page");

    // Get page access token (needed for IG API calls)
    const pageData = await graphGet(`/${page.id}`, { fields: "access_token" });
    TOKEN = pageData.access_token;
    console.log(`  Page: ${page.name} (${page.id})`);

    const igAccountId = page.instagram_business_account.id;
    const data = await graphGet(`/${igAccountId}`, {
      fields:
        "id,username,name,biography,profile_picture_url,website,followers_count,follows_count,media_count,is_published",
    });

    db.prepare(
      `INSERT OR REPLACE INTO ig_accounts
       (id, username, name, biography, profile_picture_url, website,
        followers_count, follows_count, media_count, is_published,
        shopping_product_tag_eligibility, fetched_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))`
    ).run(
      data.id, data.username, data.name, data.biography,
      data.profile_picture_url, data.website,
      data.followers_count, data.follows_count, data.media_count,
      data.is_published ? 1 : 0,
      data.shopping_product_tag_eligibility ? 1 : 0
    );

    // Daily snapshot
    db.prepare(
      `INSERT OR REPLACE INTO ig_account_snapshots
       (account_id, followers_count, follows_count, media_count, snapshot_date)
       VALUES (?, ?, ?, ?, date('now'))`
    ).run(data.id, data.followers_count, data.follows_count, data.media_count);

    console.log(`  Account: ${data.username} (${data.id})`);
    completeSync(syncId, 1);
    return { igAccountId: data.id, pageId: page.id };
  } catch (e) {
    failSync(syncId, e.message);
    throw e;
  }
}

// ============================================================
// Sync: Media
// ============================================================

async function syncMedia(accountId) {
  const syncId = startSync("media", accountId);
  const lastSync = getLastSync("media");
  let count = 0;

  try {
    const mediaList = await graphGetAll(`/${accountId}/media`, {
      fields:
        "id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,shortcode,alt_text,like_count,comments_count,is_comment_enabled,is_shared_to_feed,timestamp",
      limit: 50,
    });

    const upsert = db.prepare(
      `INSERT OR REPLACE INTO ig_media
       (id, account_id, caption, media_type, media_product_type, media_url,
        thumbnail_url, permalink, shortcode, alt_text, like_count, comments_count,
        is_comment_enabled, is_shared_to_feed, timestamp, fetched_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))`
    );

    for (const m of mediaList) {
      if (lastSync && m.timestamp < lastSync) continue;

      upsert.run(
        m.id, accountId, m.caption, m.media_type, m.media_product_type,
        m.media_url, m.thumbnail_url, m.permalink, m.shortcode, m.alt_text,
        m.like_count, m.comments_count,
        m.is_comment_enabled ? 1 : 0,
        m.is_shared_to_feed ? 1 : 0,
        m.timestamp
      );
      count++;

      // Fetch carousel children
      if (m.media_type === "CAROUSEL_ALBUM") {
        await syncCarouselChildren(m.id);
      }
    }

    console.log(`  Media: ${count} items`);
    completeSync(syncId, count);
    return mediaList;
  } catch (e) {
    failSync(syncId, e.message);
    throw e;
  }
}

async function syncCarouselChildren(mediaId) {
  const children = await graphGetAll(`/${mediaId}/children`, {
    fields: "id,media_type,media_url,thumbnail_url,alt_text",
  });

  const upsert = db.prepare(
    `INSERT OR REPLACE INTO ig_media_children
     (id, parent_id, media_type, media_url, thumbnail_url, alt_text, position)
     VALUES (?, ?, ?, ?, ?, ?, ?)`
  );

  children.forEach((c, i) => {
    upsert.run(c.id, mediaId, c.media_type, c.media_url, c.thumbnail_url, c.alt_text, i);
  });
}

// ============================================================
// Sync: Stories (ephemeral, always full fetch)
// ============================================================

async function syncStories(accountId) {
  const syncId = startSync("stories", accountId);
  try {
    const stories = await graphGetAll(`/${accountId}/stories`, {
      fields:
        "id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,shortcode,alt_text,like_count,comments_count,timestamp",
      limit: 50,
    });

    const upsert = db.prepare(
      `INSERT OR REPLACE INTO ig_media
       (id, account_id, caption, media_type, media_product_type, media_url,
        thumbnail_url, permalink, shortcode, alt_text, like_count, comments_count,
        is_comment_enabled, is_shared_to_feed, timestamp, fetched_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))`
    );

    for (const s of stories) {
      upsert.run(
        s.id, accountId, s.caption, s.media_type, s.media_product_type || "STORY",
        s.media_url, s.thumbnail_url, s.permalink, s.shortcode, s.alt_text,
        s.like_count, s.comments_count, s.timestamp
      );
    }

    console.log(`  Stories: ${stories.length} active`);
    completeSync(syncId, stories.length);
  } catch (e) {
    failSync(syncId, e.message);
    throw e;
  }
}

// ============================================================
// Sync: Comments
// ============================================================

async function syncComments(accountId) {
  const syncId = startSync("comments", accountId);
  const lastSync = getLastSync("comments");
  let count = 0;

  try {
    // Get media IDs to fetch comments for
    let mediaRows;
    if (lastSync) {
      mediaRows = db
        .prepare("SELECT id FROM ig_media WHERE account_id = ? AND media_product_type != 'STORY' AND timestamp >= ?")
        .all(accountId, lastSync);
    } else {
      mediaRows = db
        .prepare("SELECT id FROM ig_media WHERE account_id = ? AND media_product_type != 'STORY'")
        .all(accountId);
    }

    const upsertComment = db.prepare(
      `INSERT OR REPLACE INTO ig_comments
       (id, media_id, parent_comment_id, username, from_id, text, like_count, hidden, timestamp, fetched_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))`
    );

    for (const { id: mediaId } of mediaRows) {
      try {
        const comments = await graphGetAll(`/${mediaId}/comments`, {
          fields: "id,text,timestamp,username,from,like_count,hidden",
          limit: 50,
        });

        for (const c of comments) {
          upsertComment.run(
            c.id, mediaId, null, c.username, c.from?.id,
            c.text, c.like_count, c.hidden ? 1 : 0, c.timestamp
          );
          count++;

          // Fetch replies
          try {
            const replies = await graphGetAll(`/${c.id}/replies`, {
              fields: "id,text,timestamp,username,from,like_count,hidden",
              limit: 50,
            });
            for (const r of replies) {
              upsertComment.run(
                r.id, mediaId, c.id, r.username, r.from?.id,
                r.text, r.like_count, r.hidden ? 1 : 0, r.timestamp
              );
              count++;
            }
          } catch (_) {
            // Some media types don't support reply fetching
          }
        }
      } catch (_) {
        // Some media types don't support comments
      }
    }

    console.log(`  Comments: ${count}`);
    completeSync(syncId, count);
  } catch (e) {
    failSync(syncId, e.message);
    throw e;
  }
}

// ============================================================
// Sync: Media Insights
// ============================================================

const MEDIA_METRICS = {
  FEED: "reach,views,total_interactions,shares,saved,likes,comments,follows,profile_activity,profile_visits",
  REELS: "reach,views,total_interactions,shares,saved,likes,comments,ig_reels_avg_watch_time,ig_reels_video_view_total_time",
  STORY: "reach,views,total_interactions,shares,follows,profile_activity,profile_visits,navigation,replies",
};

// Re-poll insights for media published within this window. Reach/views/etc.
// keep accruing for weeks after publish, so a one-shot fetch at publish time
// captures only the first few hours and undercounts cumulative engagement.
// 30 days matches the dashboard reporting window and stays under the app-level
// Graph API rate limit (~200 calls/hour); engagement is mostly stable past 30d.
const MEDIA_INSIGHTS_LOOKBACK_DAYS = 30;

async function syncMediaInsights(accountId) {
  const syncId = startSync("media_insights", accountId);
  let count = 0;

  try {
    const cutoff = new Date(
      Date.now() - MEDIA_INSIGHTS_LOOKBACK_DAYS * 24 * 60 * 60 * 1000
    ).toISOString();
    const mediaRows = db
      .prepare("SELECT id, media_product_type FROM ig_media WHERE account_id = ? AND timestamp >= ?")
      .all(accountId, cutoff);

    // Append-only by design: each sync writes a new dated snapshot rather than
    // overwriting. Downstream queries pick `MAX(fetched_at) <= anchor` to get
    // the value as-of any point in time. Do NOT add a DELETE/prune here.
    const upsert = db.prepare(
      `INSERT INTO ig_media_insights (media_id, metric, value, fetched_at)
       VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))`
    );

    let skipped = 0;
    for (const { id: mediaId, media_product_type: mpt } of mediaRows) {
      const metrics = MEDIA_METRICS[mpt] || MEDIA_METRICS.FEED;
      try {
        const data = await graphGet(
          `/${mediaId}/insights`,
          { metric: metrics },
          { silent: true } // deleted/expired posts in the lookback window are expected
        );

        if (data.data) {
          for (const insight of data.data) {
            const val = insight.values?.[0]?.value;
            if (val !== undefined) {
              upsert.run(mediaId, insight.name, typeof val === "number" ? val : 0);
              count++;
            }
          }
        }
      } catch (_) {
        skipped++;
      }
    }

    console.log(`  Media insights: ${count} data points${skipped ? ` (${skipped} media skipped)` : ""}`);
    completeSync(syncId, count);
  } catch (e) {
    failSync(syncId, e.message);
    throw e;
  }
}

// ============================================================
// Sync: Account Insights
// ============================================================

const ACCOUNT_METRICS_SIMPLE = [
  "accounts_engaged",
  "replies",
  "reposts",
  "views",
  "reach",
  "total_interactions",
  "likes",
  "comments",
  "shares",
  "saves",
  "profile_views",
  "website_clicks",
  "profile_links_taps",
];

const ACCOUNT_METRICS_BREAKDOWN = [
  { metric: "total_interactions", breakdown: "media_product_type" },
  { metric: "likes", breakdown: "media_product_type" },
  { metric: "comments", breakdown: "media_product_type" },
  { metric: "shares", breakdown: "media_product_type" },
  { metric: "saves", breakdown: "media_product_type" },
  { metric: "reach", breakdown: "media_product_type" },
  { metric: "reach", breakdown: "follow_type" },
  { metric: "views", breakdown: "media_product_type" },
  { metric: "follows_and_unfollows", breakdown: "follow_type" },
  { metric: "profile_links_taps", breakdown: "contact_button_type" },
  { metric: "views", breakdown: "follow_type" },
];

async function fetchAccountInsightsWindow(accountId, periodLabel, since, until, endTime) {
  let count = 0;

  const upsert = db.prepare(
    `INSERT OR REPLACE INTO ig_account_insights
     (account_id, metric, period, breakdown_dimension, breakdown_value, value, end_time)
     VALUES (?, ?, ?, ?, ?, ?, ?)`
  );

  // Simple metrics (no breakdown) — total_value response format
  for (const metric of ACCOUNT_METRICS_SIMPLE) {
    try {
      const data = await graphGet(`/${accountId}/insights`, {
        metric,
        period: "day",
        metric_type: "total_value",
        since,
        until,
      });

      if (data.data) {
        for (const insight of data.data) {
          const val = insight.total_value?.value;
          if (val !== undefined) {
            upsert.run(accountId, insight.name, periodLabel, null, null, val, endTime);
            count++;
          }
        }
      }
    } catch (e) { console.error(`    Skipped ${metric} (${periodLabel}):`, e.message?.substring(0, 80)); }
  }

  // Metrics with breakdowns — total_value.breakdowns response format
  for (const { metric, breakdown } of ACCOUNT_METRICS_BREAKDOWN) {
    try {
      const data = await graphGet(`/${accountId}/insights`, {
        metric,
        period: "day",
        metric_type: "total_value",
        breakdown,
        since,
        until,
      });

      if (data.data) {
        for (const insight of data.data) {
          const tv = insight.total_value;
          const breakdowns = tv?.breakdowns?.[0]?.results || [];
          if (breakdowns.length) {
            for (const b of breakdowns) {
              const dimVal = b.dimension_values?.[0] || "unknown";
              upsert.run(accountId, insight.name, periodLabel, breakdown, dimVal, b.value, endTime);
              count++;
            }
          } else if (tv?.value !== undefined) {
            upsert.run(accountId, insight.name, periodLabel, breakdown, null, tv.value, endTime);
            count++;
          }
        }
      }
    } catch (e) { console.error(`    Skipped ${metric}/${breakdown} (${periodLabel}):`, e.message?.substring(0, 80)); }
  }

  return count;
}

async function syncAccountInsights(accountId) {
  const syncId = startSync("account_insights", accountId);
  const lastSync = getLastSync("account_insights");

  const since = lastSync
    ? Math.floor(new Date(lastSync).getTime() / 1000)
    : Math.floor((Date.now() - 30 * 24 * 60 * 60 * 1000) / 1000);
  const until = Math.floor(Date.now() / 1000);
  const endTime = new Date().toISOString();

  try {
    const count = await fetchAccountInsightsWindow(accountId, "day", since, until, endTime);
    console.log(`  Account insights: ${count} data points`);
    completeSync(syncId, count);
  } catch (e) {
    failSync(syncId, e.message);
    throw e;
  }
}

// Pull deduplicated 28-day-rolling totals — what IG dashboard headline tiles show.
// Summing daily values double-counts users who came back across days; this query
// returns one period-deduped total per metric instead.
async function syncAccountInsights28d(accountId) {
  const syncId = startSync("account_insights_28d", accountId);

  const since = Math.floor((Date.now() - 28 * 24 * 60 * 60 * 1000) / 1000);
  const until = Math.floor(Date.now() / 1000);
  const endTime = new Date().toISOString();

  try {
    const count = await fetchAccountInsightsWindow(accountId, "days_28", since, until, endTime);
    console.log(`  Account insights (28d): ${count} data points`);
    completeSync(syncId, count);
  } catch (e) {
    failSync(syncId, e.message);
    throw e;
  }
}

// ============================================================
// Sync: Follower Count Time Series
// ============================================================

async function syncFollowerCountTimeSeries(accountId) {
  const syncId = startSync("follower_count_ts", accountId);
  let count = 0;

  const since = Math.floor((Date.now() - 30 * 24 * 60 * 60 * 1000) / 1000);
  const until = Math.floor(Date.now() / 1000);

  const upsert = db.prepare(
    `INSERT OR REPLACE INTO ig_account_insights
     (account_id, metric, period, breakdown_dimension, breakdown_value, value, end_time)
     VALUES (?, ?, ?, ?, ?, ?, ?)`
  );

  try {
    const data = await graphGet(`/${accountId}/insights`, {
      metric: "follower_count",
      period: "day",
      since,
      until,
    });

    if (data.data) {
      for (const insight of data.data) {
        const values = insight.values || [];
        for (const v of values) {
          upsert.run(accountId, "follower_count", "day", null, null, v.value, v.end_time);
          count++;
        }
      }
    }

    console.log(`  Follower count time series: ${count} daily values`);
    completeSync(syncId, count);
  } catch (e) {
    failSync(syncId, e.message);
    throw e;
  }
}

// ============================================================
// Sync: Audience Demographics
// ============================================================

const DEMO_METRICS = ["follower_demographics", "engaged_audience_demographics"];
const DEMO_DIMENSIONS = ["age", "city", "country", "gender"];
// engaged_audience_demographics only supports this_week, this_month in v20+
// follower_demographics supports all timeframes
const DEMO_TIMEFRAMES = {
  follower_demographics: ["last_30_days", "this_month", "this_week"],
  engaged_audience_demographics: ["this_month", "this_week"],
};

async function syncDemographics(accountId) {
  const syncId = startSync("demographics", accountId);
  let count = 0;

  const upsert = db.prepare(
    `INSERT OR REPLACE INTO ig_audience_demographics
     (account_id, metric, dimension, dimension_value, value, timeframe, fetched_at)
     VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))`
  );

  try {
    for (const metric of DEMO_METRICS) {
      for (const dimension of DEMO_DIMENSIONS) {
        for (const timeframe of DEMO_TIMEFRAMES[metric]) {
          try {
            const data = await graphGet(`/${accountId}/insights`, {
              metric,
              period: "lifetime",
              metric_type: "total_value",
              breakdown: dimension,
              timeframe,
            });

            if (data.data) {
              for (const insight of data.data) {
                const breakdowns =
                  insight.total_value?.breakdowns?.[0]?.results || [];
                for (const b of breakdowns) {
                  const dimVal = b.dimension_values?.[0] || "unknown";
                  upsert.run(accountId, metric, dimension, dimVal, b.value, timeframe);
                  count++;
                }
              }
            }
          } catch (_) {}
        }
      }
    }

    console.log(`  Demographics: ${count} data points`);
    completeSync(syncId, count);
  } catch (e) {
    failSync(syncId, e.message);
    throw e;
  }
}

// ============================================================
// Sync: Conversations & Messages
// ============================================================

async function syncMessages(accountId, pageId) {
  const syncId = startSync("messages", accountId);
  let count = 0;

  try {
    // TOKEN is already the page token at this point

    const upsertConvo = db.prepare(
      `INSERT OR REPLACE INTO ig_conversations
       (id, account_id, updated_time, fetched_at)
       VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))`
    );

    const upsertMsg = db.prepare(
      `INSERT OR REPLACE INTO ig_messages
       (id, conversation_id, from_id, from_username, to_id, to_username,
        message, created_time, is_unsupported, reply_to_message_id, fetched_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))`
    );

    const upsertAttachment = db.prepare(
      `INSERT OR REPLACE INTO ig_message_attachments
       (id, message_id, type, file_url, name)
       VALUES (?, ?, ?, ?, ?)`
    );

    try {
        const convos = await graphGetAll(`/${pageId}/conversations`, {
          platform: "instagram",
          fields: "id,updated_time",
          limit: 25,
        });

        for (const convo of convos) {
          upsertConvo.run(convo.id, accountId, convo.updated_time);

          // Fetch messages in this conversation
          try {
            const messages = await graphGetAll(`/${convo.id}/messages`, {
              fields: "id,message,created_time,from,to,attachments,is_unsupported",
            });

            for (const msg of messages) {
              const fromUser = msg.from?.data?.[0] || msg.from || {};
              const toUser = msg.to?.data?.[0] || msg.to || {};

              upsertMsg.run(
                msg.id, convo.id,
                fromUser.id, fromUser.username || fromUser.name,
                toUser.id, toUser.username || toUser.name,
                msg.message, msg.created_time,
                msg.is_unsupported ? 1 : 0,
                null
              );
              count++;

              // Attachments
              if (msg.attachments?.data) {
                for (const att of msg.attachments.data) {
                  upsertAttachment.run(
                    att.id || `${msg.id}_att_${Math.random().toString(36).slice(2, 8)}`,
                    msg.id,
                    att.mime_type || att.type || null,
                    att.file_url || att.image_data?.url || att.video_data?.url || null,
                    att.name || null
                  );
                }
              }
            }
          } catch (_) {
            // Some conversations may not be accessible
          }
        }
    } catch (_) {
      // Page may not have IG conversations
    }

    console.log(`  Messages: ${count}`);
    completeSync(syncId, count);
  } catch (e) {
    failSync(syncId, e.message);
    throw e;
  }
}

// ============================================================
// Main
// ============================================================

async function main() {
  console.log("Starting Instagram sync...\n");

  const { igAccountId, pageId } = await syncAccount();
  await syncMedia(igAccountId);
  await syncStories(igAccountId);
  await syncComments(igAccountId);
  await syncMediaInsights(igAccountId);
  await syncAccountInsights(igAccountId);
  await syncAccountInsights28d(igAccountId);
  await syncDemographics(igAccountId);
  await syncMessages(igAccountId, pageId);

  console.log("\nSync complete.");
}

main().catch((e) => {
  console.error("Fatal error:", e.message);
  process.exit(1);
});
