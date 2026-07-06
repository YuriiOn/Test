"""Markdown report generator.

Renders the weekly analysis document (LLM or heuristic) plus raw delta
tables into `output/reports/report-<week>.md` and `output/reports/latest.md`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path


def _projects_table(projects: list[dict]) -> str:
    if not projects:
        return "_None identified this week._\n"
    lines = ["| Project | Topic | Why it matters |", "|---|---|---|"]
    for p in projects:
        name = p.get("name", "")
        link = f"[{name}](https://github.com/{name})" if "/" in name else name
        lines.append(f"| {link} | {p.get('topic', '')} | {p.get('reason', '')} |")
    return "\n".join(lines) + "\n"


def _movers_table(repo_deltas: list[dict], limit: int = 15) -> str:
    movers = [d for d in repo_deltas if d.get("stars_delta") is not None]
    movers.sort(key=lambda d: d["stars_delta"], reverse=True)
    movers = movers[:limit]
    if not movers:
        return "_No delta data yet (first run establishes the baseline)._\n"
    lines = ["| Repo | Topic | ★ now | ★ Δ week | Commits this week |", "|---|---|---|---|---|"]
    for d in movers:
        lines.append(
            f"| [{d['full_name']}](https://github.com/{d['full_name']}) "
            f"| {d['topic']} | {d['stars']:,} | {d['stars_delta']:+,} "
            f"| {d.get('commits_this_week', 0)} |"
        )
    return "\n".join(lines) + "\n"


def render_report(week_key: str, previous_week: str | None, source: str,
                  analysis: dict, deltas: dict, stats: dict) -> str:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        f"# Tech Scout — Weekly Report {week_key}",
        "",
        f"_Generated {now} · analysis source: **{source}** · "
        f"compared against: **{previous_week or 'first run (baseline)'}**_",
        "",
        f"Tracked this week: **{stats.get('repos', 0)} repositories**, "
        f"**{stats.get('channels', 0)} channels**, **{stats.get('videos', 0)} videos** "
        f"across **{stats.get('topics', 0)} topics**.",
        "",
        "## Weekly Summary Review",
        "",
        analysis.get("weekly_summary_review", "_n/a_"),
        "",
        "## Ecosystem Shifts",
        "",
    ]
    shifts = analysis.get("ecosystem_shifts") or []
    parts += [f"- {s}" for s in shifts] or ["_None detected._"]

    parts += ["", "## Actual Overview", ""]
    for item in analysis.get("actual_overview", []):
        parts.append(f"### {item.get('topic')}")
        parts.append(item.get("state", ""))
        parts.append("")

    parts += [
        "## Top Projects", "", _projects_table(analysis.get("top_projects", [])),
        "## New Projects", "", _projects_table(analysis.get("new_projects", [])),
        "## Promising Projects", "", _projects_table(analysis.get("promising_projects", [])),
        "## Biggest Movers (raw data)", "", _movers_table(deltas.get("repos", [])),
    ]

    videos = analysis.get("notable_videos") or []
    if videos:
        parts += ["## Notable Videos", ""]
        for v in videos:
            parts.append(f"- [{v.get('title')}]({v.get('url')}) — {v.get('reason', '')}")
        parts.append("")

    parts += [
        "---",
        "_Interactive relationship map: open `output/index.html` "
        "(serve the folder with `python run.py serve`)._",
        "",
    ]
    return "\n".join(parts)


def write_report(content: str, week_key: str, output_dir: Path) -> Path:
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"report-{week_key}.md"
    path.write_text(content)
    (reports_dir / "latest.md").write_text(content)
    return path
