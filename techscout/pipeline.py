"""End-to-end pipeline orchestration.

    scrape (GitHub + YouTube)
        → snapshot into SQLite (weekly state)
        → compute deltas vs last week's state
        → LLM / heuristic analysis
        → markdown report + interactive graph

The same pipeline serves the first run (baseline build) and every weekly
run afterwards — the only difference is whether a previous week exists.
"""

from __future__ import annotations

import logging

from .analyzer import analyze, compute_deltas
from .config import Settings
from .database import Database, current_week_key
from .github_scraper import GitHubScraper
from .graph_builder import build_graph_data
from .report import render_report, write_report
from .youtube_scraper import YouTubeScraper

log = logging.getLogger("techscout.pipeline")


def run_pipeline(settings: Settings, week_key: str | None = None) -> dict:
    """Execute a full scouting run. Returns a summary dict."""
    week_key = week_key or current_week_key()
    db = Database(settings.db_path)
    run_id = db.start_run(week_key)
    targets = settings.targets

    try:
        # -- 1. Ingestion ---------------------------------------------------
        gh = GitHubScraper(settings.github_token)
        yt = YouTubeScraper(settings.youtube_api_key)

        all_repos: list[dict] = []
        all_channels: list[dict] = []
        all_videos: list[dict] = []

        for topic in targets.topics:
            log.info("=== Scraping topic: %s ===", topic)
            repos = gh.scrape_topic(
                topic, targets.repos_per_topic,
                targets.discovery_repos_per_topic,
                targets.min_stars_for_discovery,
            )
            for repo in repos:
                repo["topic"] = topic
                db.save_repo_snapshot(run_id, week_key, topic, repo)
            all_repos.extend(repos)

            channels, videos = yt.scrape_topic(
                topic, targets.channels_per_topic,
                targets.videos_per_channel, targets.video_lookback_days,
            )
            for ch in channels:
                ch["topic"] = topic
                db.save_channel_snapshot(run_id, week_key, topic, ch)
            for v in videos:
                v["topic"] = topic
                db.save_video_snapshot(run_id, week_key, topic, v)
            all_channels.extend(channels)
            all_videos.extend(videos)

        # User-pinned repos tracked regardless of search ranking.
        for full_name in targets.extra_repos:
            if any(r["full_name"] == full_name for r in all_repos):
                continue
            repo = gh.fetch_repo(full_name)
            if repo:
                gh.enrich_repo(repo)
                repo["topic"] = "pinned"
                db.save_repo_snapshot(run_id, week_key, "pinned", repo)
                all_repos.append(repo)

        # User-pinned channels.
        if targets.extra_channels and yt.enabled:
            pinned = yt.fetch_channels(targets.extra_channels)
            for ch in pinned:
                if any(c["channel_id"] == ch["channel_id"] for c in all_channels):
                    continue
                ch["topic"] = "pinned"
                db.save_channel_snapshot(run_id, week_key, "pinned", ch)
                all_channels.append(ch)
                for v in yt.fetch_recent_videos(
                    ch["channel_id"], targets.videos_per_channel,
                    targets.video_lookback_days,
                ):
                    v["topic"] = "pinned"
                    db.save_video_snapshot(run_id, week_key, "pinned", v)
                    all_videos.append(v)

        log.info("Ingestion complete: %d repos, %d channels, %d videos",
                 len(all_repos), len(all_channels), len(all_videos))

        # -- 2. Load previous state & compute deltas -------------------------
        previous_week = db.previous_week_key(week_key)
        prev_repos = db.repos_for_week(previous_week) if previous_week else []
        prev_channels = db.channels_for_week(previous_week) if previous_week else []
        first_seen = {r["full_name"]: db.repo_first_seen(r["full_name"]) or week_key
                      for r in all_repos}
        deltas = compute_deltas(all_repos, prev_repos, all_channels,
                                prev_channels, first_seen, week_key)

        # -- 3. Analysis (LLM with heuristic fallback) ------------------------
        source, analysis = analyze(settings, week_key, previous_week,
                                   all_repos, all_videos, all_channels, deltas)
        db.save_analysis(run_id, week_key, source, analysis)

        # -- 4. Outputs: report + interactive graph ---------------------------
        stats = {
            "repos": len(all_repos),
            "channels": len(all_channels),
            "videos": len(all_videos),
            "topics": len(targets.topics),
        }
        report_md = render_report(week_key, previous_week, source,
                                  analysis, deltas, stats)
        report_path = write_report(report_md, week_key, settings.output_dir)
        graph_path = build_graph_data(week_key, all_repos, all_channels,
                                      all_videos, deltas, settings.output_dir)

        db.finish_run(run_id, "completed")
        log.info("Run complete. Report: %s | Graph: %s", report_path, graph_path)
        return {
            "week": week_key,
            "previous_week": previous_week,
            "analysis_source": source,
            "report": str(report_path),
            "graph": str(graph_path),
            **stats,
        }
    except Exception:
        db.finish_run(run_id, "failed")
        raise
    finally:
        db.close()
