#!/usr/bin/env python3
"""Backfill ig_account_snapshots from historical commits of comprehensive-growth-report.html.

The DB was wiped, leaving only today's snapshot. Each daily commit of the report
shows the follower/following/media counts at that time — we can reverse-engineer
a per-day history by reading the file at each commit.
"""
import re
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB = ROOT / "peacegrappler.db"
REPORT_PATH = "comprehensive-growth-report.html"

LABEL_RE = re.compile(
    r'<div class="value">([\d,]+)</div>\s*<div class="label">(Followers|Following|Total Posts)</div>'
)
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True)


def main():
    log = git("log", "--follow", "--format=%H\t%s", "--", REPORT_PATH).strip().splitlines()
    print(f"Found {len(log)} commits touching {REPORT_PATH}")

    con = sqlite3.connect(DB)
    account_id = con.execute("SELECT id FROM ig_accounts LIMIT 1").fetchone()[0]
    print(f"Account: {account_id}")

    # date -> (followers, following, media) — keep the LAST commit seen per date,
    # which (because git log is newest-first) is the EARLIEST commit of that day.
    # We want the LATEST (evening) value per day, so process oldest-first and
    # always overwrite — the last write wins per date.
    by_date = {}
    for line in reversed(log):  # oldest first
        sha, subject = line.split("\t", 1)
        m = DATE_RE.search(subject)
        if not m:
            continue
        date = m.group(1)

        try:
            content = git("show", f"{sha}:{REPORT_PATH}")
        except subprocess.CalledProcessError:
            continue

        vals = {}
        for v, label in LABEL_RE.findall(content):
            vals[label] = int(v.replace(",", ""))
        if "Followers" not in vals:
            continue
        by_date[date] = (
            vals.get("Followers"),
            vals.get("Following"),
            vals.get("Total Posts"),
        )

    print(f"Extracted {len(by_date)} unique dates")

    existing = {row[0] for row in con.execute(
        "SELECT snapshot_date FROM ig_account_snapshots WHERE account_id = ?",
        (account_id,)
    )}
    print(f"DB already has snapshots for: {sorted(existing)}")

    inserts = []
    for date, (followers, following, media) in sorted(by_date.items()):
        inserts.append((account_id, followers, following, media, date))

    con.executemany(
        """INSERT OR REPLACE INTO ig_account_snapshots
           (account_id, followers_count, follows_count, media_count, snapshot_date)
           VALUES (?, ?, ?, ?, ?)""",
        inserts,
    )
    con.commit()
    print(f"Upserted {len(inserts)} snapshots.")

    rows = con.execute(
        "SELECT snapshot_date, followers_count FROM ig_account_snapshots WHERE account_id = ? ORDER BY snapshot_date",
        (account_id,)
    ).fetchall()
    print(f"\nFinal snapshots ({len(rows)} total):")
    for d, f in rows:
        print(f"  {d}: {f:,}")

    con.close()


if __name__ == "__main__":
    main()
