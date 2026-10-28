"""Central configuration for Apify Social Scraper Engine."""
import os
from dotenv import load_dotenv

load_dotenv()

# ─── Apify API Token ─────────────────────────────────────────────
# Get yours from: https://console.apify.com/account/integrations
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "YOUR_APIFY_TOKEN_HERE")

# ─── Default Settings ────────────────────────────────────────────
DEFAULT_POST_LIMIT = 10

# ─── Apify Actor IDs (Community Actors - verified 2026) ──────────
# Each can be overridden via *_ACTOR_ID in .env, e.g. LINKEDIN_ACTOR_ID.
ACTORS = {
    # LinkedIn company/profile posts - no cookies required
    "linkedin": os.getenv("LINKEDIN_ACTOR_ID", "harvestapi/linkedin-company-posts"),

    # Reddit Scraper Lite - subreddits, users, and search queries
    "reddit": os.getenv("REDDIT_ACTOR_ID", "trudax/reddit-scraper-lite"),

    # Twitter/X Scraper Lite - handles profiles, search, URLs
    "twitter": os.getenv("TWITTER_ACTOR_ID", "apidojo/twitter-scraper-lite"),

    # Website Content Crawler - blogs, insights, and newsroom pages
    "blog": os.getenv("BLOG_ACTOR_ID", "apify/website-content-crawler"),

    # LinkedIn Jobs Scraper - open roles filtered by company, no cookies
    "linkedin_jobs": os.getenv("LINKEDIN_JOBS_ACTOR_ID", "harvestapi/linkedin-job-search"),

    # Google News Scraper (Full Article Bodies) - CXO movement tracking
    # (executive joins/resigns/appointments). Verified live 2026-09-03:
    # startInputs takes search-query strings; enrichBody fetches the real
    # publisher page (best-effort — paywalled/CF-protected sites without
    # a paid Apify plan return metadata only, no body).
    "cxo_news": os.getenv("CXO_NEWS_ACTOR_ID", "memo23/google-news-scraper"),
}

# ─── Timeouts (seconds) ──────────────────────────────────────────
TIMEOUTS = {
    "linkedin": 120,
    "reddit": 90,
    "twitter": 120,
    "blog": 300,
    "sec": 30,
    "news": 30,
    "news_content": 180,
    "sec_mentions": 30,
    "regulatory": 30,
    "patents": 30,
    "rss": 30,
    "youtube": 30,
    "linkedin_jobs": 90,
    # Confirmed by a live test run (5 items, enrichBody on): ~40s. Left
    # well above that since maxItems scales the number of publisher
    # pages it has to fetch.
    "cxo_news": 180,
    "bloomberg": 30,
    "bloomberg_content": 60,
    "web_search": 30,
    "sec_proxy": 60,
    "brokercheck": 30,
    "uk_directorships": 30,
}

# ─── Resource limits (sized for a 2GB EC2 instance) ──────────────
# Headless-browser pages open at once across the whole process — every
# channel and every concurrent run share this (scrapers/page_fetcher.py).
BROWSER_MAX_PAGES = max(1, int(os.getenv("BROWSER_MAX_PAGES", "3")))

# Wall-clock cap on one whole target run (scrape + store + DB mirror +
# optional digest) — past this, runner.py records it as failed and the
# scheduler moves on, instead of one wedged run freezing every schedule.
RUN_HARD_TIMEOUT_SECS = max(60, int(os.getenv("RUN_HARD_TIMEOUT_SECS", "3600")))

# Days a scraped post stays in its JSON store (output/stores/) after it was
# first stored — older posts are dropped on the target's next run (see
# store.py). 0 keeps everything. The Postgres mirror is not pruned.
STORE_RETENTION_DAYS = int(os.getenv("STORE_RETENTION_DAYS", "15"))

# ─── Free public APIs (no Apify actor, no compute units) ─────────
# SEC requires a descriptive User-Agent with a contact address.
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT", "StockFinDataDownload janakpanchal13@gmail.com"
)

# YouTube Data API v3 — free key from Google Cloud Console (enable
# "YouTube Data API v3" under APIs & Services, then Credentials -> API key).
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

# UK Companies House API — free key from
# https://developer.company-information.service.gov.uk/ (register, then
# "Create an application" -> REST API key). Unset = the uk_directorships
# channel is simply skipped.
COMPANIES_HOUSE_API_KEY = os.getenv("COMPANIES_HOUSE_API_KEY", "")

# ─── Outbound mail (SMTP, STARTTLS) ──────────────────────────────
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
# The address mail appears From — defaults to the login username, which
# is also the address most SMTP relays require the From header to match.
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USERNAME)

# ─── Database (optional — Postgres mirror of the JSON output) ────
# When unset, db.py's writes are no-ops: the JSON files under output/
# stay fully functional on their own either way.
#
# Two named connections so this can point at either Postgres without
# losing the other:
#   - NEON:  the cloud DB this repo has always written to (default).
#   - LOCAL: sales_agent-ai's dev Postgres (localhost:5432/sales_ai),
#            for testing against the same DB that repo's dashboard reads.
# DATABASE_URL_NEON falls back to the old DATABASE_URL var so existing
# .env files keep working unchanged. Flip DB_USE_LOCAL=true to switch —
# see MERGE_PLAN.md Phase 1 before pointing this at local Postgres, the
# sales_agent-ai `posts` table needs its UNIQUE constraint fixed first.
def _libpq_url(url: str) -> str:
    """Accept SQLAlchemy-style URLs (postgresql+psycopg2://, postgres://)
    too — db.py hands this straight to psycopg2.connect(), and libpq
    rejects a "+driver" suffix as an invalid DSN, which db.py would
    swallow as "could not connect; continuing with JSON only"."""
    import re
    return re.sub(r"^postgres(?:ql)?(?:\+\w+)?://", "postgresql://", (url or "").strip())


DATABASE_URL_NEON = _libpq_url(os.getenv("DATABASE_URL_NEON", os.getenv("DATABASE_URL", "")))
DATABASE_URL_LOCAL = _libpq_url(os.getenv("DATABASE_URL_LOCAL", ""))
DB_USE_LOCAL = os.getenv("DB_USE_LOCAL", "false").strip().lower() in ("1", "true", "yes")
DATABASE_URL = DATABASE_URL_LOCAL if DB_USE_LOCAL else DATABASE_URL_NEON

# ─── Scheduler (runs inside `python main.py serve`) ──────────────
# Off by default so a local dev server never starts billed runs on its
# own — set SCHEDULER_ENABLED=true in the production env file only.
#   news job:   every SCHEDULE_NEWS_HOURS, the news channels below for
#               every company target.
#   companies job: every SCHEDULE_COMPANIES_DAYS, a full scrape (all
#               configured channels) of every company target.
#   people job: every SCHEDULE_PEOPLE_DAYS, a full scrape of every
#               person target, one at a time.
# One summary email per finished batch goes to SCHEDULE_NOTIFY_TO
# (comma-separated for several; blank = no email).
def _env_list(name: str, default: str) -> list:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "false").strip().lower() in ("1", "true", "yes")
# True when the scheduler runs as its own process (`python main.py
# scheduler` — the scheduler container in docker-compose.yml): `serve` then
# leaves the jobs to it instead of running them in a thread of its own.
SCHEDULER_EXTERNAL = os.getenv("SCHEDULER_EXTERNAL", "false").strip().lower() in ("1", "true", "yes")
SCHEDULE_NEWS_HOURS = _env_float("SCHEDULE_NEWS_HOURS", 3)
SCHEDULE_NEWS_CHANNELS = _env_list("SCHEDULE_NEWS_CHANNELS", "news,bloomberg,cxo_news")
SCHEDULE_NEWS_LIMIT = int(_env_float("SCHEDULE_NEWS_LIMIT", 20))
SCHEDULE_COMPANIES_DAYS = _env_float("SCHEDULE_COMPANIES_DAYS", 3)
SCHEDULE_COMPANIES_LIMIT = int(_env_float("SCHEDULE_COMPANIES_LIMIT", 20))
SCHEDULE_PEOPLE_DAYS = _env_float("SCHEDULE_PEOPLE_DAYS", 3)
SCHEDULE_PEOPLE_LIMIT = int(_env_float("SCHEDULE_PEOPLE_LIMIT", 20))
SCHEDULE_NOTIFY_TO = os.getenv("SCHEDULE_NOTIFY_TO", "Janak@stradit.com").strip()

# Google News RSS locale.
NEWS_LOCALE = {"hl": "en-US", "gl": "US"}

# ─── Validation ──────────────────────────────────────────────────
if APIFY_TOKEN == "YOUR_APIFY_TOKEN_HERE":
    import warnings
    warnings.warn(
        "⚠️  APIFY_TOKEN not set. Set it via environment variable or .env file",
        RuntimeWarning,
    )
