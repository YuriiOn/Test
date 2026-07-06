"""Configuration loading for Tech Scout.

Two configuration sources:
  * `.env`            — API keys and paths (loaded via python-dotenv).
  * `config/targets.json` — topics and target lists, editable by the user
                            (also via `run.py add-topic / add-channel / add-repo`).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGETS_PATH = PROJECT_ROOT / "config" / "targets.json"

load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_TOPICS = [
    "AI",
    "AI engineering",
    "agent memory",
    "agent engineering",
    "game theory",
    "chaos theory",
    "experimentation",
    "A/B testing",
    "crypto trading",
    "time series processing",
    "machine learning",
    "data science",
]


@dataclass
class Targets:
    """Parsed contents of config/targets.json."""

    topics: list[str] = field(default_factory=lambda: list(DEFAULT_TOPICS))
    repos_per_topic: int = 12
    discovery_repos_per_topic: int = 6
    min_stars_for_discovery: int = 20
    extra_repos: list[str] = field(default_factory=list)
    channels_per_topic: int = 4
    videos_per_channel: int = 5
    video_lookback_days: int = 14
    extra_channels: list[str] = field(default_factory=list)


@dataclass
class Settings:
    github_token: str | None
    youtube_api_key: str | None
    anthropic_api_key: str | None
    anthropic_model: str
    db_path: Path
    output_dir: Path
    targets: Targets


def load_targets(path: Path = TARGETS_PATH) -> Targets:
    if not path.exists():
        return Targets()
    raw = json.loads(path.read_text())
    gh = raw.get("github", {})
    yt = raw.get("youtube", {})
    return Targets(
        topics=raw.get("topics", list(DEFAULT_TOPICS)),
        repos_per_topic=gh.get("repos_per_topic", 12),
        discovery_repos_per_topic=gh.get("discovery_repos_per_topic", 6),
        min_stars_for_discovery=gh.get("min_stars_for_discovery", 20),
        extra_repos=gh.get("extra_repos", []),
        channels_per_topic=yt.get("channels_per_topic", 4),
        videos_per_channel=yt.get("videos_per_channel", 5),
        video_lookback_days=yt.get("video_lookback_days", 14),
        extra_channels=yt.get("extra_channels", []),
    )


def save_targets(targets: Targets, path: Path = TARGETS_PATH) -> None:
    payload = {
        "topics": targets.topics,
        "github": {
            "repos_per_topic": targets.repos_per_topic,
            "discovery_repos_per_topic": targets.discovery_repos_per_topic,
            "min_stars_for_discovery": targets.min_stars_for_discovery,
            "extra_repos": targets.extra_repos,
        },
        "youtube": {
            "channels_per_topic": targets.channels_per_topic,
            "videos_per_channel": targets.videos_per_channel,
            "video_lookback_days": targets.video_lookback_days,
            "extra_channels": targets.extra_channels,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def load_settings() -> Settings:
    db_path = PROJECT_ROOT / os.getenv("TECHSCOUT_DB_PATH", "data/techscout.db")
    output_dir = PROJECT_ROOT / os.getenv("TECHSCOUT_OUTPUT_DIR", "output")
    return Settings(
        github_token=os.getenv("GITHUB_TOKEN") or None,
        youtube_api_key=os.getenv("YOUTUBE_API_KEY") or None,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8"),
        db_path=db_path,
        output_dir=output_dir,
        targets=load_targets(),
    )
