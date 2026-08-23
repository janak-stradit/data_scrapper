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
}

# ─── Timeouts (seconds) ──────────────────────────────────────────
TIMEOUTS = {
    "linkedin": 120,
    "reddit": 90,
    "twitter": 120,
    "blog": 300,
    "sec": 30,
    "news": 30,
    "patents": 30,
}

# ─── Free public APIs (no Apify actor, no compute units) ─────────
# SEC requires a descriptive User-Agent with a contact address.
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT", "StockFinDataDownload janakpanchal13@gmail.com"
)

# ─── Outbound mail (Microsoft Graph, OAuth2 device-code login) ───
# Legacy SMTP AUTH is disabled on many M365 tenants, so mailer.py signs in
# as a user via Graph instead. Needs an Azure AD app registration (public
# client, "Mail.Send" delegated permission) — see mailer.py's docstring.
GRAPH_CLIENT_ID = os.getenv("GRAPH_CLIENT_ID", "")
GRAPH_TENANT_ID = os.getenv("GRAPH_TENANT_ID", "common")

# ─── Database (optional — Postgres mirror of the JSON output) ────
# When unset, db.py's writes are no-ops: the JSON files under output/
# stay fully functional on their own either way.
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Google News RSS locale.
NEWS_LOCALE = {"hl": "en-US", "gl": "US"}

# ─── Validation ──────────────────────────────────────────────────
if APIFY_TOKEN == "YOUR_APIFY_TOKEN_HERE":
    import warnings
    warnings.warn(
        "⚠️  APIFY_TOKEN not set. Set it via environment variable or .env file",
        RuntimeWarning,
    )
