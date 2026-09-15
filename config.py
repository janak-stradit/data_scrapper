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
}

# ─── Free public APIs (no Apify actor, no compute units) ─────────
# SEC requires a descriptive User-Agent with a contact address.
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT", "StockFinDataDownload janakpanchal13@gmail.com"
)

# YouTube Data API v3 — free key from Google Cloud Console (enable
# "YouTube Data API v3" under APIs & Services, then Credentials -> API key).
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

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
DATABASE_URL_NEON = os.getenv("DATABASE_URL_NEON", os.getenv("DATABASE_URL", ""))
DATABASE_URL_LOCAL = os.getenv("DATABASE_URL_LOCAL", "")
DB_USE_LOCAL = os.getenv("DB_USE_LOCAL", "false").strip().lower() in ("1", "true", "yes")
DATABASE_URL = DATABASE_URL_LOCAL if DB_USE_LOCAL else DATABASE_URL_NEON

# ─── API key (required only once this is reachable off localhost) ─
# When set, every /api/* request (GET and POST) must send a matching
# X-API-Key header or main.py's serve command rejects it with 401. Left
# blank for local dev on purpose — set it before deploying anywhere the
# app isn't the only thing that can reach the port, since /api/run can
# trigger billed Apify scrapes and /api/send-email sends real mail.
API_KEY = os.getenv("API_KEY", "")

# Google News RSS locale.
NEWS_LOCALE = {"hl": "en-US", "gl": "US"}

# ─── Validation ──────────────────────────────────────────────────
if APIFY_TOKEN == "YOUR_APIFY_TOKEN_HERE":
    import warnings
    warnings.warn(
        "⚠️  APIFY_TOKEN not set. Set it via environment variable or .env file",
        RuntimeWarning,
    )
