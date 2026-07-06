#!/usr/bin/env python3
"""Lightweight scheduler — runs the full pipeline every 7 days.

Alternative to cron for environments where installing a crontab is awkward
(e.g. a long-lived container or a dev machine):

    python scheduler.py                 # run weekly, first run immediately
    python scheduler.py --day monday --at 07:00

For production, prefer the cron config in cron/techscout.cron or the
GitHub Actions workflow in .github/workflows/weekly-scout.yml.
"""

from __future__ import annotations

import argparse
import logging
import time

import schedule

from techscout.config import load_settings
from techscout.database import Database
from techscout.pipeline import run_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("techscout.scheduler")


def job() -> None:
    log.info("Scheduled Tech Scout run starting.")
    try:
        summary = run_pipeline(load_settings())
        log.info("Scheduled run finished: %s", summary)
    except Exception:
        # Never let one bad week kill the scheduler loop.
        log.exception("Scheduled run failed; will retry next week.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", default="monday",
                        choices=["monday", "tuesday", "wednesday", "thursday",
                                 "friday", "saturday", "sunday"])
    parser.add_argument("--at", default="06:00", help="Time of day, HH:MM (default 06:00)")
    parser.add_argument("--no-immediate", action="store_true",
                        help="Don't run immediately if no baseline exists yet")
    args = parser.parse_args()

    settings = load_settings()
    db = Database(settings.db_path)
    needs_baseline = not db.has_any_completed_run()
    db.close()
    if needs_baseline and not args.no_immediate:
        log.info("No baseline found — running the initial build now.")
        job()

    getattr(schedule.every(), args.day).at(args.at).do(job)
    log.info("Scheduler armed: every %s at %s. Ctrl+C to stop.", args.day, args.at)
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
