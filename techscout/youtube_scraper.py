"""YouTube ingestion layer.

Uses the YouTube Data API v3 (plain HTTPS, no Google client library needed)
to discover channels per topic and pull recent video metadata, plus
`youtube-transcript-api` for transcripts.

Graceful degradation:
  * No YOUTUBE_API_KEY            → scraper is disabled, pipeline continues.
  * quotaExceeded / rate limits   → stop scraping, keep whatever was fetched.
  * Missing/disabled transcripts  → video is stored without a transcript.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

import requests

log = logging.getLogger("techscout.youtube")

API = "https://www.googleapis.com/youtube/v3"
TRANSCRIPT_EXCERPT_CHARS = 3000

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    _TRANSCRIPTS_AVAILABLE = True
except ImportError:  # optional dependency
    _TRANSCRIPTS_AVAILABLE = False


class QuotaExceeded(Exception):
    """Raised when the YouTube API daily quota is exhausted."""


class YouTubeScraper:
    def __init__(self, api_key: str | None):
        self.api_key = api_key
        self.enabled = bool(api_key)
        self.session = requests.Session()
        if not self.enabled:
            log.warning("No YOUTUBE_API_KEY set — YouTube scraping disabled.")
        if not _TRANSCRIPTS_AVAILABLE:
            log.warning("youtube-transcript-api not installed — transcripts disabled.")

    # -- low-level -------------------------------------------------------------

    def _get(self, endpoint: str, params: dict) -> dict | None:
        params = {**params, "key": self.api_key}
        backoff = 2.0
        for _ in range(3):
            try:
                resp = self.session.get(f"{API}/{endpoint}", params=params, timeout=30)
            except requests.RequestException as exc:
                log.warning("Network error on %s: %s", endpoint, exc)
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 403:
                reason = self._error_reason(resp)
                if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                    raise QuotaExceeded(reason)
                log.warning("YouTube 403 (%s) on %s", reason, endpoint)
                return None
            if resp.status_code >= 500:
                time.sleep(backoff)
                backoff *= 2
                continue
            log.warning("YouTube status %s on %s: %s",
                        resp.status_code, endpoint, resp.text[:200])
            return None
        return None

    @staticmethod
    def _error_reason(resp: requests.Response) -> str:
        try:
            errors = resp.json()["error"]["errors"]
            return errors[0].get("reason", "unknown")
        except Exception:
            return "unknown"

    # -- channel discovery -------------------------------------------------------

    def search_channels(self, topic: str, limit: int) -> list[str]:
        """Return channel IDs relevant to a topic."""
        data = self._get("search", {
            "part": "snippet", "type": "channel", "q": topic,
            "maxResults": min(limit, 50), "order": "relevance",
        })
        if not data:
            return []
        return [item["snippet"]["channelId"] for item in data.get("items", [])]

    def fetch_channels(self, channel_ids: list[str]) -> list[dict]:
        """Fetch metadata + statistics for a batch of channel IDs."""
        if not channel_ids:
            return []
        data = self._get("channels", {
            "part": "snippet,statistics", "id": ",".join(channel_ids[:50]),
        })
        if not data:
            return []
        channels = []
        for item in data.get("items", []):
            stats = item.get("statistics", {})
            snippet = item.get("snippet", {})
            channels.append({
                "channel_id": item["id"],
                "title": snippet.get("title"),
                "url": f"https://www.youtube.com/channel/{item['id']}",
                "description": (snippet.get("description") or "")[:500],
                "subscriber_count": int(stats.get("subscriberCount", 0) or 0),
                "video_count": int(stats.get("videoCount", 0) or 0),
                "view_count": int(stats.get("viewCount", 0) or 0),
            })
        return channels

    # -- videos ---------------------------------------------------------------------

    def fetch_recent_videos(self, channel_id: str, limit: int,
                            lookback_days: int) -> list[dict]:
        published_after = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        data = self._get("search", {
            "part": "snippet", "channelId": channel_id, "type": "video",
            "order": "date", "publishedAfter": published_after,
            "maxResults": min(limit, 50),
        })
        if not data:
            return []
        video_ids = [i["id"]["videoId"] for i in data.get("items", [])]
        if not video_ids:
            return []

        details = self._get("videos", {
            "part": "snippet,statistics", "id": ",".join(video_ids),
        })
        if not details:
            return []

        videos = []
        for item in details.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            videos.append({
                "video_id": item["id"],
                "channel_id": channel_id,
                "title": snippet.get("title"),
                "url": f"https://www.youtube.com/watch?v={item['id']}",
                "published_at": snippet.get("publishedAt"),
                "description": (snippet.get("description") or "")[:1000],
                "view_count": int(stats.get("viewCount", 0) or 0),
                "transcript_excerpt": self.fetch_transcript(item["id"]),
            })
        return videos

    # -- transcripts ------------------------------------------------------------------

    @staticmethod
    def fetch_transcript(video_id: str) -> str | None:
        """Best-effort transcript fetch. Returns None when unavailable."""
        if not _TRANSCRIPTS_AVAILABLE:
            return None
        try:
            api = YouTubeTranscriptApi()
            transcript = api.fetch(video_id, languages=["en", "en-US"])
            text = " ".join(snippet.text for snippet in transcript)
            return text[:TRANSCRIPT_EXCERPT_CHARS]
        except Exception as exc:
            # TranscriptsDisabled, NoTranscriptFound, age-restricted, etc. —
            # all non-fatal; the video is still useful via title/description.
            log.debug("No transcript for %s: %s", video_id, exc)
            return None

    # -- top-level entry point ------------------------------------------------------

    def scrape_topic(self, topic: str, channels_per_topic: int,
                     videos_per_channel: int, lookback_days: int,
                     extra_channel_ids: list[str] | None = None
                     ) -> tuple[list[dict], list[dict]]:
        """Return (channels, videos) for one topic."""
        if not self.enabled:
            return [], []
        try:
            channel_ids = self.search_channels(topic, channels_per_topic)
            for cid in (extra_channel_ids or []):
                if cid not in channel_ids:
                    channel_ids.append(cid)
            channels = self.fetch_channels(channel_ids)
            videos: list[dict] = []
            for ch in channels:
                videos.extend(self.fetch_recent_videos(
                    ch["channel_id"], videos_per_channel, lookback_days))
            log.info("Topic %r: %d channels, %d videos", topic, len(channels), len(videos))
            return channels, videos
        except QuotaExceeded:
            log.warning("YouTube quota exceeded — stopping YouTube scraping for this run.")
            self.enabled = False
            return [], []
