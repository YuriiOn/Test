# Tech Scout 🔭

An automated **weekly technology scouting tool**. It tracks the topics you
care about across **GitHub repositories** and **YouTube channels**, keeps a
weekly historical state, detects **ecosystem shifts** (star velocity, new
projects, rising channels, cross-mentions), and produces:

- an **LLM-written weekly briefing** (Anthropic API) with *Weekly Summary
  Review, Actual Overview, Ecosystem Shifts, Top / New / Promising Projects*
  and *Notable Videos*,
- an **interactive relationship map** (vis-network) with separate GitHub and
  YouTube topologies,
- a **markdown report** archived per week.

```
                ┌─────────────────────┐
   topics ────► │  Ingestion layer    │  GitHub REST API · YouTube Data API v3
                │  github_scraper.py  │  youtube-transcript-api
                │  youtube_scraper.py │
                └─────────┬───────────┘
                          ▼
                ┌─────────────────────┐
                │  State & storage    │  SQLite, one snapshot per week
                │  database.py        │  → deltas: star velocity, new arrivals
                └─────────┬───────────┘
                          ▼
                ┌─────────────────────┐
                │  Analysis pipeline  │  Anthropic API (structured JSON output)
                │  analyzer.py        │  + heuristic fallback (no key needed)
                └─────────┬───────────┘
                          ▼
                ┌─────────────────────┐
                │  Outputs            │  output/reports/report-<week>.md
                │  report.py          │  output/graph_data.json + index.html
                │  graph_builder.py   │  (interactive vis-network map)
                └─────────────────────┘
```

## 1. Setup

Requires **Python 3.10+**.

```bash
git clone <this repo> && cd <repo>
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### API keys (`.env`)

| Variable | Required? | What it does | Where to get it |
|---|---|---|---|
| `GITHUB_TOKEN` | Recommended | Raises GitHub limit from 60 → 5000 req/h | [github.com/settings/tokens](https://github.com/settings/tokens) — classic token, **no scopes** needed for public data |
| `YOUTUBE_API_KEY` | Optional | Enables channel/video/transcript scouting | [Google Cloud Console](https://console.cloud.google.com/apis/credentials) → enable *YouTube Data API v3* → create API key |
| `ANTHROPIC_API_KEY` | Optional | Enables the LLM analysis | [platform.claude.com](https://platform.claude.com) |
| `ANTHROPIC_MODEL` | Optional | Analysis model (default `claude-opus-4-8`) | — |

Everything degrades gracefully: with no keys at all the tool still scouts
GitHub anonymously and writes a heuristic (numbers-based) briefing.

## 2. Seed your topics and targets

Default topics ship in [`config/targets.json`](config/targets.json):
*AI, AI engineering, agent memory, agent engineering, game theory, chaos
theory, experimentation, A/B testing, crypto trading, time series
processing, machine learning, data science.*

Manage them from the CLI — a new topic gets its **initial base built on the
next run**, then is monitored every week:

```bash
python run.py list-topics
python run.py add-topic "quantum machine learning"
python run.py remove-topic "chaos theory"

# Pin specific targets that should always be tracked, regardless of search rank:
python run.py add-repo anthropics/claude-code
python run.py add-channel UCSHZKyawb77ixDdsGog4iWA   # YouTube channel ID (UC…)
```

You can also edit `config/targets.json` directly (repos per topic, video
lookback window, etc.).

## 3. Run

```bash
# First run — builds the initial baseline for every topic
python run.py init

# Every subsequent run compares against last week's stored state
python run.py weekly

# Read the briefing
python run.py report                    # prints output/reports/latest.md

# Explore the interactive relationship map
python run.py serve                     # http://localhost:8000
```

The map has two tabs — **GitHub** (topics → repos, sized by stars, colored
by weekly velocity, dashed edges = shared topic tags) and **YouTube**
(topics → channels → videos, with dashed edges to GitHub repos mentioned in
video titles/descriptions/transcripts).

## 4. Automation (every 7 days)

Pick **one** of the three options:

**a) cron** — see [`cron/techscout.cron`](cron/techscout.cron):

```cron
0 6 * * 1  cd /path/to/repo && flock -n /tmp/techscout.lock ./.venv/bin/python run.py weekly >> data/cron.log 2>&1
```

**b) Python scheduler** (for long-lived machines/containers):

```bash
python scheduler.py --day monday --at 06:00
```

**c) GitHub Actions** — [`.github/workflows/weekly-scout.yml`](.github/workflows/weekly-scout.yml)
runs Mondays 06:00 UTC, commits the updated SQLite state and outputs back to
the repo. Add `YOUTUBE_API_KEY` and `ANTHROPIC_API_KEY` as repository
secrets and trigger the first baseline via *Run workflow*.

## 5. What gets stored

`data/techscout.db` (SQLite):

| Table | Contents |
|---|---|
| `runs` | one row per weekly run (`2026-W28` style keys) |
| `repo_snapshots` | stars/forks/issues/README excerpt/recent commits, per repo per week |
| `channel_snapshots` | subscribers/videos/views per channel per week |
| `video_snapshots` | metadata + transcript excerpt per video |
| `analyses` | the full JSON analysis document per week |

Snapshots are immutable, so any week can be compared with any other and the
"first seen" week of every project is always known.

## 6. Robustness notes

- **GitHub rate limits**: reads `X-RateLimit-*` headers, sleeps until reset
  (capped at 2 min), exponential backoff on 5xx, paces search queries.
- **YouTube quota**: a `quotaExceeded` response disables YouTube for the rest
  of the run instead of failing the pipeline; already-fetched data is kept.
- **Missing transcripts**: disabled/absent transcripts are skipped silently —
  the video is still analyzed via title + description.
- **LLM failures**: auth/rate-limit/network errors fall back to the built-in
  heuristic analyzer, so a report and graph are produced every week no matter
  what.
- **Interrupted runs**: marked `failed` in the `runs` table; delta comparison
  only ever uses the last *completed* week.

## 7. Project layout

```
techscout/
  config.py           # .env + config/targets.json loading
  database.py         # SQLite state layer (weekly snapshots + deltas)
  github_scraper.py   # GitHub REST ingestion, rate-limit aware
  youtube_scraper.py  # YouTube Data API + transcripts, quota aware
  analyzer.py         # LLM orchestration (structured JSON) + heuristic fallback
  graph_builder.py    # GitHub & YouTube network topologies → graph_data.json
  report.py           # markdown briefing renderer
  pipeline.py         # end-to-end orchestration
frontend/index.html   # interactive map (vis-network), copied into output/
run.py                # CLI: init | weekly | topics | pins | report | serve
scheduler.py          # 7-day Python scheduler
cron/techscout.cron   # crontab entry
.github/workflows/    # GitHub Actions weekly run
config/targets.json   # topics + pinned repos/channels
```
