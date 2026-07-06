"""Visualization layer — relationship graph builder.

Produces `graph_data.json` with two independent network topologies (rendered
as separate tabs in the frontend):

GitHub graph
  * topic nodes  ── tracked-topic hubs
  * repo nodes   ── sized by log(stars), colored by weekly star velocity
  * edges: topic→repo membership, repo↔repo shared GitHub topic tags

YouTube graph
  * topic nodes, channel nodes (sized by log(subscribers)), video nodes
  * edges: topic→channel, channel→video
  * cross-signal: video→repo "mention" edges when a tracked repo's name
    appears in a video title/description/transcript

The frontend (frontend/index.html) reads this file with vis-network.
"""

from __future__ import annotations

import json
import math
import re
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Node color palette
COLOR_TOPIC = "#7c6ff0"
COLOR_REPO = "#4ea8de"
COLOR_REPO_HOT = "#f4845f"     # high star velocity this week
COLOR_REPO_NEW = "#43c59e"     # first seen this week
COLOR_CHANNEL = "#e5484d"
COLOR_VIDEO = "#f2c14e"
COLOR_MENTION = "#9aa0a6"


def _repo_node_id(full_name: str) -> str:
    return f"repo:{full_name}"


def _topic_node_id(section: str, topic: str) -> str:
    return f"topic:{section}:{topic}"


def build_github_graph(repos: list[dict], deltas: dict, week_key: str) -> dict:
    delta_by_name = {d["full_name"]: d for d in deltas.get("repos", [])}
    nodes, edges = [], []
    seen_nodes: set[str] = set()

    topics = sorted({r["topic"] for r in repos})
    for topic in topics:
        nid = _topic_node_id("gh", topic)
        nodes.append({
            "id": nid, "label": topic, "group": "topic",
            "color": COLOR_TOPIC, "shape": "hexagon", "size": 28,
            "title": f"Topic: {topic}",
        })
        seen_nodes.add(nid)

    for repo in repos:
        nid = _repo_node_id(repo["full_name"])
        d = delta_by_name.get(repo["full_name"], {})
        stars_delta = d.get("stars_delta")
        is_new = d.get("is_new_this_week", False)

        color = COLOR_REPO
        if is_new:
            color = COLOR_REPO_NEW
        elif stars_delta is not None and stars_delta >= 50:
            color = COLOR_REPO_HOT

        delta_txt = (f"{stars_delta:+,} ★ this week" if stars_delta is not None
                     else "baseline week")
        tooltip = (
            f"{repo['full_name']}\n"
            f"★ {repo['stars']:,} ({delta_txt})  ⑂ {repo['forks']:,}  "
            f"issues {repo['open_issues']:,}\n"
            f"{(repo.get('description') or '')[:180]}"
        )
        if nid not in seen_nodes:
            nodes.append({
                "id": nid,
                "label": repo["full_name"].split("/")[-1],
                "group": "repo",
                "color": color,
                "size": 8 + 3 * math.log10(max(repo["stars"], 1) + 1),
                "title": tooltip,
                "url": repo.get("url"),
            })
            seen_nodes.add(nid)
        edges.append({
            "from": _topic_node_id("gh", repo["topic"]),
            "to": nid,
            "color": {"color": "#c9c9d4", "opacity": 0.5},
        })

    # repo↔repo edges via shared GitHub topic tags (capped to keep it readable)
    tag_index: dict[str, list[str]] = {}
    for repo in repos:
        for tag in repo.get("repo_topics", []):
            tag_index.setdefault(tag, []).append(repo["full_name"])
    linked: set[tuple[str, str]] = set()
    for tag, members in tag_index.items():
        if len(members) < 2 or len(members) > 12:  # ignore giant generic tags
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                key = tuple(sorted((a, b)))
                if key in linked:
                    continue
                linked.add(key)
                edges.append({
                    "from": _repo_node_id(a), "to": _repo_node_id(b),
                    "dashes": True, "color": {"color": "#8888aa", "opacity": 0.35},
                    "title": f"shared tag: {tag}",
                })

    return {"nodes": nodes, "edges": edges}


def build_youtube_graph(channels: list[dict], videos: list[dict],
                        repos: list[dict]) -> dict:
    nodes, edges = [], []
    seen_nodes: set[str] = set()

    topics = sorted({c["topic"] for c in channels})
    for topic in topics:
        nid = _topic_node_id("yt", topic)
        nodes.append({
            "id": nid, "label": topic, "group": "topic",
            "color": COLOR_TOPIC, "shape": "hexagon", "size": 28,
            "title": f"Topic: {topic}",
        })
        seen_nodes.add(nid)

    for ch in channels:
        nid = f"channel:{ch['channel_id']}"
        if nid not in seen_nodes:
            nodes.append({
                "id": nid, "label": ch.get("title") or ch["channel_id"],
                "group": "channel", "color": COLOR_CHANNEL,
                "size": 10 + 2.5 * math.log10(max(ch.get("subscriber_count") or 1, 1) + 1),
                "title": (f"{ch.get('title')}\n"
                          f"{(ch.get('subscriber_count') or 0):,} subscribers, "
                          f"{(ch.get('video_count') or 0):,} videos"),
                "url": ch.get("url"),
            })
            seen_nodes.add(nid)
        edges.append({
            "from": _topic_node_id("yt", ch["topic"]), "to": nid,
            "color": {"color": "#c9c9d4", "opacity": 0.5},
        })

    # Pre-compile repo-name patterns for mention detection. Only match short
    # names of 4+ chars to avoid false positives like "go" or "ml".
    repo_patterns = []
    for repo in repos:
        short = repo["full_name"].split("/")[-1]
        if len(short) >= 4:
            repo_patterns.append(
                (repo["full_name"], re.compile(rf"\b{re.escape(short)}\b", re.IGNORECASE))
            )

    mentioned_repos: set[str] = set()
    for v in videos:
        vid = f"video:{v['video_id']}"
        if vid not in seen_nodes:
            nodes.append({
                "id": vid, "label": (v.get("title") or "")[:40],
                "group": "video", "color": COLOR_VIDEO, "shape": "square", "size": 9,
                "title": (f"{v.get('title')}\n{(v.get('view_count') or 0):,} views\n"
                          f"published {v.get('published_at')}"),
                "url": v.get("url"),
            })
            seen_nodes.add(vid)
        edges.append({
            "from": f"channel:{v['channel_id']}", "to": vid,
            "color": {"color": "#d9c9a4", "opacity": 0.5},
        })

        # Repo mentions in video text → dashed cross-signal edges.
        haystack = " ".join(filter(None, [
            v.get("title"), v.get("description"), v.get("transcript_excerpt"),
        ]))
        for full_name, pattern in repo_patterns:
            if pattern.search(haystack):
                mention_id = f"mention:{full_name}"
                if mention_id not in seen_nodes:
                    nodes.append({
                        "id": mention_id, "label": full_name, "group": "mention",
                        "color": COLOR_MENTION, "shape": "box", "size": 8,
                        "title": f"GitHub repo mentioned in videos: {full_name}",
                        "url": f"https://github.com/{full_name}",
                    })
                    seen_nodes.add(mention_id)
                edges.append({
                    "from": vid, "to": mention_id, "dashes": True,
                    "color": {"color": COLOR_MENTION, "opacity": 0.6},
                    "title": "repo mentioned in video",
                })
                mentioned_repos.add(full_name)

    return {"nodes": nodes, "edges": edges,
            "mentioned_repos": sorted(mentioned_repos)}


def build_graph_data(week_key: str, repos: list[dict], channels: list[dict],
                     videos: list[dict], deltas: dict, output_dir: Path) -> Path:
    """Assemble both graphs and write graph_data.json + the frontend viewer."""
    data = {
        "week": week_key,
        "github": build_github_graph(repos, deltas, week_key),
        "youtube": build_youtube_graph(channels, videos, repos),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "graph_data.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False))

    # Ship the viewer next to the data so `python run.py serve` (or any static
    # file server pointed at output/) just works.
    frontend_src = PROJECT_ROOT / "frontend" / "index.html"
    if frontend_src.exists():
        shutil.copy(frontend_src, output_dir / "index.html")
    return out_path
