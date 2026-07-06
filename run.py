#!/usr/bin/env python3
"""Tech Scout CLI.

Usage:
    python run.py init                     First run — builds the baseline
    python run.py weekly                   Weekly run (scrape → analyze → report)
    python run.py list-topics              Show tracked topics
    python run.py add-topic "quantum ml"   Track a new topic (baselined next run)
    python run.py remove-topic "AI"        Stop tracking a topic
    python run.py add-repo owner/name      Pin a specific repo
    python run.py add-channel UC...        Pin a specific YouTube channel ID
    python run.py report                   Print the latest report to stdout
    python run.py serve [--port 8000]      Serve the interactive graph + reports
"""

from __future__ import annotations

import argparse
import logging
import sys

from techscout.config import load_settings, load_targets, save_targets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("techscout.cli")


def cmd_run(args) -> int:
    from techscout.database import Database
    from techscout.pipeline import run_pipeline

    settings = load_settings()

    if args.command == "init":
        db = Database(settings.db_path)
        already = db.has_any_completed_run()
        db.close()
        if already and not args.force:
            log.error("Baseline already exists. Use `weekly`, or `init --force` to re-run.")
            return 1
        log.info("Building initial baseline for %d topics...", len(settings.targets.topics))

    summary = run_pipeline(settings)
    print("\n--- Run summary ---")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print("\nNext: `python run.py report` or `python run.py serve` for the graph.")
    return 0


def cmd_list_topics(_args) -> int:
    for t in load_targets().topics:
        print(f"  - {t}")
    return 0


def cmd_add_topic(args) -> int:
    targets = load_targets()
    topic = args.topic.strip()
    if topic.lower() in (t.lower() for t in targets.topics):
        print(f"Topic already tracked: {topic}")
        return 0
    targets.topics.append(topic)
    save_targets(targets)
    print(f"Added topic: {topic}")
    print("An initial base for it will be built on the next run "
          "(`python run.py weekly`).")
    return 0


def cmd_remove_topic(args) -> int:
    targets = load_targets()
    before = len(targets.topics)
    targets.topics = [t for t in targets.topics if t.lower() != args.topic.strip().lower()]
    if len(targets.topics) == before:
        print(f"Topic not found: {args.topic}")
        return 1
    save_targets(targets)
    print(f"Removed topic: {args.topic}")
    return 0


def cmd_add_repo(args) -> int:
    targets = load_targets()
    repo = args.repo.strip().removeprefix("https://github.com/").strip("/")
    if "/" not in repo:
        print("Expected owner/name (or a github.com URL).")
        return 1
    if repo not in targets.extra_repos:
        targets.extra_repos.append(repo)
        save_targets(targets)
    print(f"Pinned repo: {repo}")
    return 0


def cmd_add_channel(args) -> int:
    targets = load_targets()
    cid = args.channel_id.strip()
    if cid not in targets.extra_channels:
        targets.extra_channels.append(cid)
        save_targets(targets)
    print(f"Pinned channel: {cid}")
    return 0


def cmd_report(_args) -> int:
    settings = load_settings()
    latest = settings.output_dir / "reports" / "latest.md"
    if not latest.exists():
        print("No report yet — run `python run.py init` first.")
        return 1
    print(latest.read_text())
    return 0


def cmd_serve(args) -> int:
    import functools
    import http.server

    settings = load_settings()
    if not (settings.output_dir / "index.html").exists():
        print("No output yet — run `python run.py init` first.")
        return 1
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(settings.output_dir)
    )
    print(f"Serving Tech Scout dashboard at http://localhost:{args.port}")
    print("Press Ctrl+C to stop.")
    http.server.ThreadingHTTPServer(("", args.port), handler).serve_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="techscout", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="First run: build the initial baseline")
    p_init.add_argument("--force", action="store_true",
                        help="Re-run even if a baseline already exists")
    p_init.set_defaults(func=cmd_run)

    p_weekly = sub.add_parser("weekly", help="Run the weekly pipeline")
    p_weekly.set_defaults(func=cmd_run)

    sub.add_parser("list-topics", help="List tracked topics").set_defaults(func=cmd_list_topics)

    p_add = sub.add_parser("add-topic", help="Track a new topic")
    p_add.add_argument("topic")
    p_add.set_defaults(func=cmd_add_topic)

    p_rm = sub.add_parser("remove-topic", help="Stop tracking a topic")
    p_rm.add_argument("topic")
    p_rm.set_defaults(func=cmd_remove_topic)

    p_repo = sub.add_parser("add-repo", help="Pin a specific GitHub repo (owner/name)")
    p_repo.add_argument("repo")
    p_repo.set_defaults(func=cmd_add_repo)

    p_chan = sub.add_parser("add-channel", help="Pin a YouTube channel ID (UC...)")
    p_chan.add_argument("channel_id")
    p_chan.set_defaults(func=cmd_add_channel)

    sub.add_parser("report", help="Print the latest report").set_defaults(func=cmd_report)

    p_serve = sub.add_parser("serve", help="Serve the interactive graph dashboard")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
