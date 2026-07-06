"""Analysis pipeline (LLM orchestration).

Aggregates this week's scraped data with last week's stored state, computes
week-over-week deltas, and sends a structured payload to the Anthropic API.
The model is constrained to a JSON schema so the output is always parseable:

  weekly_summary_review  — narrative of what happened this week
  actual_overview        — current state of each tracked topic/ecosystem
  ecosystem_shifts       — notable movements detected in the deltas
  top_projects           — established leaders
  new_projects           — first seen this week
  promising_projects     — high velocity / early momentum

If no ANTHROPIC_API_KEY is configured (or the API call fails), a heuristic
analyzer produces the same document shape from the raw numbers so the rest
of the pipeline (report + graph) always works.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("techscout.analyzer")

MAX_REPOS_IN_PROMPT = 120
MAX_VIDEOS_IN_PROMPT = 80

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "weekly_summary_review": {
            "type": "string",
            "description": "3-6 paragraph narrative review of the week across all topics.",
        },
        "actual_overview": {
            "type": "array",
            "description": "Current state of each tracked topic.",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "state": {"type": "string"},
                },
                "required": ["topic", "state"],
                "additionalProperties": False,
            },
        },
        "ecosystem_shifts": {
            "type": "array",
            "description": "Notable week-over-week movements and why they matter.",
            "items": {"type": "string"},
        },
        "top_projects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "topic": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "topic", "reason"],
                "additionalProperties": False,
            },
        },
        "new_projects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "topic": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "topic", "reason"],
                "additionalProperties": False,
            },
        },
        "promising_projects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "topic": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "topic", "reason"],
                "additionalProperties": False,
            },
        },
        "notable_videos": {
            "type": "array",
            "description": "Videos worth watching this week and why.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "channel": {"type": "string"},
                    "url": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["title", "channel", "url", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "weekly_summary_review", "actual_overview", "ecosystem_shifts",
        "top_projects", "new_projects", "promising_projects", "notable_videos",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are a senior technology scout producing a weekly intelligence briefing.
You receive structured data about GitHub repositories and YouTube channels
tracked across a set of technology topics, including last week's state and
computed deltas (star velocity, new arrivals, subscriber growth, fresh
commits, video transcripts).

Your job:
- Identify real ecosystem shifts, not noise. Ground every claim in the data.
- Weigh *velocity* (change) over absolute size when judging what is promising.
- Read commit messages, README excerpts, video titles and transcripts for
  qualitative signal: pivots, new releases, emerging patterns, cross-project
  mentions.
- Be specific: name projects, numbers, and channels. Avoid filler.
- If it is the first run (no previous state), say so and produce a baseline
  overview instead of delta analysis.
"""


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------

def compute_deltas(current_repos: list[dict], previous_repos: list[dict],
                   current_channels: list[dict], previous_channels: list[dict],
                   first_seen: dict[str, str], week_key: str) -> dict:
    """Compute week-over-week changes used by both the LLM and the fallback."""
    prev_repo_by_name = {r["full_name"]: r for r in previous_repos}
    prev_chan_by_id = {c["channel_id"]: c for c in previous_channels}

    repo_deltas = []
    for repo in current_repos:
        prev = prev_repo_by_name.get(repo["full_name"])
        stars_delta = (repo["stars"] - prev["stars"]) if prev else None
        repo_deltas.append({
            "full_name": repo["full_name"],
            "topic": repo["topic"],
            "stars": repo["stars"],
            "stars_delta": stars_delta,
            "forks_delta": (repo["forks"] - prev["forks"]) if prev else None,
            "open_issues_delta": (repo["open_issues"] - prev["open_issues"]) if prev else None,
            "commits_this_week": len(repo.get("recent_commits", [])),
            "is_new_this_week": first_seen.get(repo["full_name"]) == week_key,
            "created_at": repo.get("created_at"),
        })

    channel_deltas = []
    for ch in current_channels:
        prev = prev_chan_by_id.get(ch["channel_id"])
        channel_deltas.append({
            "channel_id": ch["channel_id"],
            "title": ch["title"],
            "topic": ch["topic"],
            "subscriber_count": ch["subscriber_count"],
            "subscriber_delta": (ch["subscriber_count"] - prev["subscriber_count"]) if prev else None,
            "video_count_delta": (ch["video_count"] - prev["video_count"]) if prev else None,
            "is_new_this_week": prev is None,
        })

    return {"repos": repo_deltas, "channels": channel_deltas}


# ---------------------------------------------------------------------------
# LLM analyzer
# ---------------------------------------------------------------------------

def _build_payload(week_key: str, previous_week: str | None,
                   repos: list[dict], videos: list[dict],
                   channels: list[dict], deltas: dict) -> str:
    """Compact JSON payload for the model. Trims the heaviest fields."""
    slim_repos = []
    for r in sorted(repos, key=lambda x: x["stars"], reverse=True)[:MAX_REPOS_IN_PROMPT]:
        slim_repos.append({
            "full_name": r["full_name"],
            "topic": r["topic"],
            "description": r.get("description"),
            "language": r.get("language"),
            "stars": r["stars"],
            "forks": r["forks"],
            "open_issues": r["open_issues"],
            "created_at": r.get("created_at"),
            "repo_topics": r.get("repo_topics", [])[:8],
            "recent_commit_messages": [c["message"] for c in r.get("recent_commits", [])][:8],
            "readme_excerpt": (r.get("readme_excerpt") or "")[:600],
        })

    slim_videos = []
    for v in sorted(videos, key=lambda x: x.get("view_count") or 0, reverse=True)[:MAX_VIDEOS_IN_PROMPT]:
        slim_videos.append({
            "title": v.get("title"),
            "topic": v.get("topic"),
            "channel_id": v.get("channel_id"),
            "url": v.get("url"),
            "published_at": v.get("published_at"),
            "view_count": v.get("view_count"),
            "description": (v.get("description") or "")[:300],
            "transcript_excerpt": (v.get("transcript_excerpt") or "")[:800],
        })

    return json.dumps({
        "week": week_key,
        "previous_week": previous_week,
        "is_first_run": previous_week is None,
        "deltas": deltas,
        "repositories": slim_repos,
        "channels": channels,
        "videos": slim_videos,
    }, ensure_ascii=False)


def run_llm_analysis(api_key: str, model: str, week_key: str,
                     previous_week: str | None, repos: list[dict],
                     videos: list[dict], channels: list[dict],
                     deltas: dict) -> dict | None:
    """Call the Anthropic API with schema-constrained output.

    Returns the parsed analysis dict, or None on failure (caller falls back
    to the heuristic analyzer).
    """
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed — skipping LLM analysis.")
        return None

    client = anthropic.Anthropic(api_key=api_key, max_retries=3)
    payload = _build_payload(week_key, previous_week, repos, videos, channels, deltas)

    try:
        with client.messages.stream(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
            messages=[{
                "role": "user",
                "content": (
                    f"Produce the weekly technology scouting analysis for {week_key} "
                    f"from this data:\n\n{payload}"
                ),
            }],
        ) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError:
        log.error("Anthropic authentication failed — check ANTHROPIC_API_KEY.")
        return None
    except anthropic.RateLimitError:
        log.error("Anthropic rate limit exhausted after SDK retries.")
        return None
    except anthropic.APIStatusError as exc:
        log.error("Anthropic API error %s: %s", exc.status_code, exc.message)
        return None
    except anthropic.APIConnectionError as exc:
        log.error("Network error reaching Anthropic API: %s", exc)
        return None

    if response.stop_reason == "refusal":
        log.error("Anthropic model refused the request.")
        return None
    if response.stop_reason == "max_tokens":
        log.warning("Analysis truncated at max_tokens; output may be incomplete.")

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("Could not parse model output as JSON: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Heuristic fallback (no API key required)
# ---------------------------------------------------------------------------

def run_heuristic_analysis(week_key: str, previous_week: str | None,
                           repos: list[dict], videos: list[dict],
                           channels: list[dict], deltas: dict) -> dict:
    """Numbers-only analysis with the same document shape as the LLM output."""
    delta_by_name = {d["full_name"]: d for d in deltas["repos"]}

    top = sorted(repos, key=lambda r: r["stars"], reverse=True)[:10]
    new = [r for r in repos if delta_by_name.get(r["full_name"], {}).get("is_new_this_week")]
    new = sorted(new, key=lambda r: r["stars"], reverse=True)[:10]

    def velocity(r: dict) -> float:
        d = delta_by_name.get(r["full_name"], {})
        sd = d.get("stars_delta")
        if sd is not None:
            return sd
        # First run: use stars/age as a proxy for momentum.
        return r["stars"] / 90.0
    promising = sorted(
        (r for r in repos if r["stars"] >= 10),
        key=velocity, reverse=True,
    )[:10]

    topics = sorted({r["topic"] for r in repos} | {c["topic"] for c in channels})
    overview = []
    for topic in topics:
        t_repos = [r for r in repos if r["topic"] == topic]
        t_videos = [v for v in videos if v.get("topic") == topic]
        top_repo = max(t_repos, key=lambda r: r["stars"], default=None)
        state = (
            f"{len(t_repos)} tracked repos, {len(t_videos)} recent videos. "
            + (f"Leader: {top_repo['full_name']} ({top_repo['stars']:,}★)." if top_repo else "")
        )
        overview.append({"topic": topic, "state": state})

    shifts = []
    for d in sorted(deltas["repos"], key=lambda x: x.get("stars_delta") or 0, reverse=True)[:5]:
        if (d.get("stars_delta") or 0) > 0:
            shifts.append(
                f"{d['full_name']} gained {d['stars_delta']:,} stars this week ({d['topic']})."
            )
    if not shifts and previous_week is None:
        shifts.append("First run — baseline established; deltas will appear next week.")

    notable = [
        {
            "title": v.get("title") or "",
            "channel": v.get("channel_id") or "",
            "url": v.get("url") or "",
            "reason": f"{v.get('view_count', 0):,} views",
        }
        for v in sorted(videos, key=lambda x: x.get("view_count") or 0, reverse=True)[:8]
    ]

    def project_entry(r: dict, reason: str) -> dict:
        return {"name": r["full_name"], "topic": r["topic"], "reason": reason}

    summary = (
        f"Heuristic analysis for {week_key} (no LLM key configured). "
        f"Tracked {len(repos)} repositories and {len(channels)} channels across "
        f"{len(topics)} topics. "
        + ("This is the first run — a baseline snapshot was recorded; "
           "week-over-week insights will start next week."
           if previous_week is None else
           f"Compared against {previous_week}: see ecosystem shifts below.")
    )

    return {
        "weekly_summary_review": summary,
        "actual_overview": overview,
        "ecosystem_shifts": shifts,
        "top_projects": [
            project_entry(r, f"{r['stars']:,} stars, {r['forks']:,} forks") for r in top
        ],
        "new_projects": [
            project_entry(r, f"First seen this week with {r['stars']:,} stars") for r in new
        ],
        "promising_projects": [
            project_entry(
                r,
                (lambda sd: f"+{sd:,} stars this week"
                 if sd is not None else f"{r['stars']:,} stars on a young repo")(
                    delta_by_name.get(r["full_name"], {}).get("stars_delta")),
            )
            for r in promising
        ],
        "notable_videos": notable,
    }


def analyze(settings, week_key: str, previous_week: str | None,
            repos: list[dict], videos: list[dict], channels: list[dict],
            deltas: dict) -> tuple[str, dict]:
    """Run the best available analyzer. Returns (source, analysis_doc)."""
    if settings.anthropic_api_key:
        result = run_llm_analysis(
            settings.anthropic_api_key, settings.anthropic_model,
            week_key, previous_week, repos, videos, channels, deltas,
        )
        if result is not None:
            return "llm", result
        log.warning("LLM analysis failed — falling back to heuristic analyzer.")
    return "heuristic", run_heuristic_analysis(
        week_key, previous_week, repos, videos, channels, deltas
    )
