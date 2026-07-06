"""State & storage layer.

Lightweight SQLite store that keeps one snapshot of every tracked repository,
channel, and video per weekly run. Historical snapshots are never mutated,
so week-over-week deltas (star velocity, subscriber growth, new arrivals)
can always be recomputed from raw data.

Week keys use ISO year-week format, e.g. "2026-W28".
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    week_key    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running'   -- running | completed | failed
);

CREATE TABLE IF NOT EXISTS repo_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    week_key      TEXT NOT NULL,
    topic         TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    url           TEXT,
    description   TEXT,
    language      TEXT,
    stars         INTEGER,
    forks         INTEGER,
    open_issues   INTEGER,
    watchers      INTEGER,
    pushed_at     TEXT,
    created_at    TEXT,
    repo_topics   TEXT,           -- JSON array of GitHub topic tags
    readme_excerpt TEXT,
    recent_commits TEXT,          -- JSON array of {sha, message, date}
    UNIQUE(week_key, topic, full_name)
);

CREATE TABLE IF NOT EXISTS channel_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES runs(id),
    week_key         TEXT NOT NULL,
    topic            TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    title            TEXT,
    url              TEXT,
    description      TEXT,
    subscriber_count INTEGER,
    video_count      INTEGER,
    view_count       INTEGER,
    UNIQUE(week_key, topic, channel_id)
);

CREATE TABLE IF NOT EXISTS video_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             INTEGER NOT NULL REFERENCES runs(id),
    week_key           TEXT NOT NULL,
    topic              TEXT NOT NULL,
    video_id           TEXT NOT NULL,
    channel_id         TEXT NOT NULL,
    title              TEXT,
    url                TEXT,
    published_at       TEXT,
    description        TEXT,
    view_count         INTEGER,
    transcript_excerpt TEXT,
    UNIQUE(week_key, video_id)
);

CREATE TABLE IF NOT EXISTS analyses (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL REFERENCES runs(id),
    week_key  TEXT NOT NULL,
    source    TEXT NOT NULL,      -- 'llm' | 'heuristic'
    payload   TEXT NOT NULL,      -- JSON analysis document
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_repo_week   ON repo_snapshots(week_key);
CREATE INDEX IF NOT EXISTS idx_repo_name   ON repo_snapshots(full_name);
CREATE INDEX IF NOT EXISTS idx_chan_week   ON channel_snapshots(week_key);
CREATE INDEX IF NOT EXISTS idx_video_week  ON video_snapshots(week_key);
"""


def current_week_key(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- runs ---------------------------------------------------------------

    def start_run(self, week_key: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (week_key, started_at) VALUES (?, ?)",
            (week_key, dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str = "completed") -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status = ? WHERE id = ?",
            (dt.datetime.now(dt.timezone.utc).isoformat(), status, run_id),
        )
        self.conn.commit()

    def has_any_completed_run(self) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status = 'completed'"
        ).fetchone()
        return row["n"] > 0

    def previous_week_key(self, current: str) -> str | None:
        """Most recent completed week before `current`."""
        row = self.conn.execute(
            """SELECT week_key FROM runs
               WHERE status = 'completed' AND week_key < ?
               ORDER BY week_key DESC LIMIT 1""",
            (current,),
        ).fetchone()
        return row["week_key"] if row else None

    # -- repo snapshots -----------------------------------------------------

    def save_repo_snapshot(self, run_id: int, week_key: str, topic: str, repo: dict) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO repo_snapshots
               (run_id, week_key, topic, full_name, url, description, language,
                stars, forks, open_issues, watchers, pushed_at, created_at,
                repo_topics, readme_excerpt, recent_commits)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, week_key, topic,
                repo["full_name"], repo.get("url"), repo.get("description"),
                repo.get("language"), repo.get("stars"), repo.get("forks"),
                repo.get("open_issues"), repo.get("watchers"),
                repo.get("pushed_at"), repo.get("created_at"),
                json.dumps(repo.get("repo_topics", [])),
                repo.get("readme_excerpt"),
                json.dumps(repo.get("recent_commits", [])),
            ),
        )
        self.conn.commit()

    def repos_for_week(self, week_key: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM repo_snapshots WHERE week_key = ?", (week_key,)
        ).fetchall()
        return [self._repo_row_to_dict(r) for r in rows]

    def repo_first_seen(self, full_name: str) -> str | None:
        row = self.conn.execute(
            "SELECT MIN(week_key) AS wk FROM repo_snapshots WHERE full_name = ?",
            (full_name,),
        ).fetchone()
        return row["wk"] if row else None

    @staticmethod
    def _repo_row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["repo_topics"] = json.loads(d.get("repo_topics") or "[]")
        d["recent_commits"] = json.loads(d.get("recent_commits") or "[]")
        return d

    # -- channel snapshots ----------------------------------------------------

    def save_channel_snapshot(self, run_id: int, week_key: str, topic: str, ch: dict) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO channel_snapshots
               (run_id, week_key, topic, channel_id, title, url, description,
                subscriber_count, video_count, view_count)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, week_key, topic,
                ch["channel_id"], ch.get("title"), ch.get("url"),
                ch.get("description"), ch.get("subscriber_count"),
                ch.get("video_count"), ch.get("view_count"),
            ),
        )
        self.conn.commit()

    def channels_for_week(self, week_key: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM channel_snapshots WHERE week_key = ?", (week_key,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- video snapshots ------------------------------------------------------

    def save_video_snapshot(self, run_id: int, week_key: str, topic: str, video: dict) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO video_snapshots
               (run_id, week_key, topic, video_id, channel_id, title, url,
                published_at, description, view_count, transcript_excerpt)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, week_key, topic,
                video["video_id"], video.get("channel_id"), video.get("title"),
                video.get("url"), video.get("published_at"),
                video.get("description"), video.get("view_count"),
                video.get("transcript_excerpt"),
            ),
        )
        self.conn.commit()

    def videos_for_week(self, week_key: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM video_snapshots WHERE week_key = ?", (week_key,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- analyses ---------------------------------------------------------------

    def save_analysis(self, run_id: int, week_key: str, source: str, payload: dict) -> None:
        self.conn.execute(
            """INSERT INTO analyses (run_id, week_key, source, payload, created_at)
               VALUES (?,?,?,?,?)""",
            (run_id, week_key, source, json.dumps(payload),
             dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        self.conn.commit()

    def analysis_for_week(self, week_key: str) -> dict | None:
        row = self.conn.execute(
            """SELECT payload FROM analyses WHERE week_key = ?
               ORDER BY id DESC LIMIT 1""",
            (week_key,),
        ).fetchone()
        return json.loads(row["payload"]) if row else None
