"""GitHub ingestion layer.

Uses the GitHub REST API to discover repositories for each tracked topic and
fetch their metadata: stars, forks, open issues, recent commit log, and a
README excerpt.

Rate-limit handling:
  * Reads `X-RateLimit-Remaining` / `X-RateLimit-Reset` headers.
  * On 403/429 rate-limit responses, sleeps until the reset time (capped)
    and retries with exponential backoff.
  * Search API has its own, much lower limit (10/min unauthenticated,
    30/min with a token) — a small pause is inserted between searches.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

import requests

log = logging.getLogger("techscout.github")

API = "https://api.github.com"
README_EXCERPT_CHARS = 2500
MAX_RETRIES = 4
MAX_RATE_LIMIT_WAIT = 120  # seconds we are willing to sleep on a rate limit


class GitHubScraper:
    def __init__(self, token: str | None = None):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "techscout/1.0",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        else:
            log.warning("No GITHUB_TOKEN set — using the 60 req/hour anonymous limit.")

    # -- low-level request with rate-limit handling ---------------------------

    def _get(self, url: str, params: dict | None = None) -> requests.Response | None:
        """GET with retries. Returns None for permanent failures (404 etc.)."""
        backoff = 2.0
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                log.warning("Network error for %s: %s (retry %d)", url, exc, attempt + 1)
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code == 200:
                return resp
            if resp.status_code in (404, 409, 451):
                # Missing repo, empty repo (409 on /commits), or DMCA'd — skip.
                return None
            if resp.status_code in (403, 429):
                wait = self._rate_limit_wait(resp)
                if wait is None:
                    log.warning("GitHub returned %s for %s — giving up.", resp.status_code, url)
                    return None
                log.info("GitHub rate limit hit; sleeping %.0fs", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                time.sleep(backoff)
                backoff *= 2
                continue
            log.warning("Unexpected status %s for %s", resp.status_code, url)
            return None
        log.warning("Exhausted retries for %s", url)
        return None

    @staticmethod
    def _rate_limit_wait(resp: requests.Response) -> float | None:
        """How long to wait for a rate-limited response, or None to give up."""
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            return min(float(retry_after), MAX_RATE_LIMIT_WAIT)
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
            wait = reset - time.time() + 2
            if 0 < wait <= MAX_RATE_LIMIT_WAIT:
                return wait
            # Reset is too far away — don't block the whole pipeline.
            return None
        # Secondary rate limit without headers — brief pause.
        return 30.0

    # -- discovery -------------------------------------------------------------

    def search_repos(self, topic: str, established_limit: int,
                     discovery_limit: int, min_discovery_stars: int) -> list[dict]:
        """Find repos for a topic: top-starred plus recently created ones.

        The second "discovery" query surfaces new projects that would never
        outrank established ones by stars — this is what lets the tool spot
        ecosystem shifts early.
        """
        results: dict[str, dict] = {}

        top = self._search(f'"{topic}" in:name,description,topics', "stars", established_limit)
        for item in top:
            results[item["full_name"]] = item

        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)).date().isoformat()
        fresh = self._search(
            f'"{topic}" in:name,description,topics created:>{cutoff} stars:>={min_discovery_stars}',
            "stars", discovery_limit,
        )
        for item in fresh:
            results.setdefault(item["full_name"], item)

        return list(results.values())

    def _search(self, query: str, sort: str, limit: int) -> list[dict]:
        if limit <= 0:
            return []
        resp = self._get(f"{API}/search/repositories", {
            "q": query, "sort": sort, "order": "desc", "per_page": min(limit, 100),
        })
        # Search API allowance is small; pace ourselves between queries.
        time.sleep(2)
        if resp is None:
            return []
        items = resp.json().get("items", [])[:limit]
        return [self._normalize_repo(i) for i in items]

    @staticmethod
    def _normalize_repo(item: dict) -> dict:
        return {
            "full_name": item["full_name"],
            "url": item["html_url"],
            "description": (item.get("description") or "")[:500],
            "language": item.get("language"),
            "stars": item.get("stargazers_count", 0),
            "forks": item.get("forks_count", 0),
            "open_issues": item.get("open_issues_count", 0),
            "watchers": item.get("watchers_count", 0),
            "pushed_at": item.get("pushed_at"),
            "created_at": item.get("created_at"),
            "repo_topics": item.get("topics", []),
        }

    # -- enrichment ---------------------------------------------------------------

    def fetch_repo(self, full_name: str) -> dict | None:
        """Fetch full metadata for a single repo (used for extra_repos)."""
        resp = self._get(f"{API}/repos/{full_name}")
        if resp is None:
            return None
        return self._normalize_repo(resp.json())

    def enrich_repo(self, repo: dict, commit_lookback_days: int = 7) -> dict:
        """Attach README excerpt and recent commit log to a repo dict."""
        repo["readme_excerpt"] = self._fetch_readme(repo["full_name"])
        repo["recent_commits"] = self._fetch_recent_commits(
            repo["full_name"], commit_lookback_days
        )
        return repo

    def _fetch_readme(self, full_name: str) -> str | None:
        resp = self._get(f"{API}/repos/{full_name}/readme")
        if resp is None:
            return None
        import base64
        data = resp.json()
        try:
            text = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        except Exception:
            return None
        return text[:README_EXCERPT_CHARS]

    def _fetch_recent_commits(self, full_name: str, days: int) -> list[dict]:
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        resp = self._get(f"{API}/repos/{full_name}/commits",
                         {"since": since, "per_page": 20})
        if resp is None:
            return []
        commits = []
        for c in resp.json():
            commit = c.get("commit", {})
            commits.append({
                "sha": c.get("sha", "")[:8],
                "message": (commit.get("message") or "").split("\n")[0][:200],
                "date": (commit.get("author") or {}).get("date"),
            })
        return commits

    # -- top-level entry point -------------------------------------------------

    def scrape_topic(self, topic: str, established_limit: int, discovery_limit: int,
                     min_discovery_stars: int) -> list[dict]:
        repos = self.search_repos(topic, established_limit, discovery_limit,
                                  min_discovery_stars)
        log.info("Topic %r: %d repos found", topic, len(repos))
        for repo in repos:
            self.enrich_repo(repo)
        return repos
