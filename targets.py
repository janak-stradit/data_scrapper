"""Company targets for the scraper engine.

Each entry maps a company key to its public presence on every channel the
engine supports — social, owned content, SEC filings, and news coverage. Add new companies here — no engine changes needed.

Targets saved from the frontend's "Save as Target" button live in
custom_targets.json instead (see save() below) and are merged in below,
so they resolve the same way as anything hand-written here.
"""
from typing import Dict, Any

import custom_targets
import db

COMPANY_TARGETS: Dict[str, Dict[str, Any]] = {
    "bny": {
        "display_name": "BNY (Bank of New York Mellon)",
        "ticker": "BK",
        "linkedin_url": "https://www.linkedin.com/company/bny-mellon/",
        # Same company page, used as the company filter for the LinkedIn
        # Jobs actor — open roles are often an earlier signal than a press
        # release (a "Blockchain Settlement Engineer" req shows up before
        # the partnership announcement does).
        "linkedin_jobs_query": "https://www.linkedin.com/company/bny-mellon/",
        "twitter_handle": "@BNYglobal",
        # Reddit has no company account — track mentions instead.
        "reddit_query": '"BNY Mellon"',
        "reddit_keywords": ["bny", "bank of new york"],
        "sec_cik": "0001390777",
        "news_query": '"BNY Mellon" OR "Bank of New York Mellon"',
        "blog_url": "https://www.bny.com/corporate/global/en/insights.html",
        "blog_glob": "https://www.bny.com/corporate/global/en/insights/**",
        "newsroom_url": "https://www.bny.com/corporate/global/en/about-us/newsroom.html",
        "newsroom_glob": "https://www.bny.com/corporate/global/en/about-us/newsroom/**",
        # BNY's own press-release RSS feed (all categories) — found via
        # bny.com/.../newsroom/rss-feeds.html, verified live before adding.
        "rss_url": "https://www.bny.com/bin/bnymellon/rssFeedGeneratorServlet.report",
        # Fed/OCC enforcement-action feeds naming BNY — low-noise (unlike
        # sec_mentions_query below, these feeds are small and don't have
        # BNY's custodian-boilerplate problem), so safe as a default for
        # the company itself. Deliberately NOT adding sec_mentions_query
        # here — "BNY Mellon"/"Bank of New York Mellon" returns 10,000+
        # hits there, nearly all custodian-relationship boilerplate in
        # unrelated funds' filings (verified live). That channel is far
        # more useful on individual names — see people_targets.py.
        "regulatory_query": "Bank of New York Mellon",
    },
    "northern_trust": {
        "display_name": "Northern Trust",
        "ticker": "NTRS",
        "linkedin_url": "https://www.linkedin.com/company/northern-trust/",
        "linkedin_jobs_query": "https://www.linkedin.com/company/northern-trust/",
        "twitter_handle": "@NorthernTrust",
        "reddit_query": '"Northern Trust"',
        "reddit_keywords": ["northern trust", "ntrs"],
        "sec_cik": "0000073124",
        "news_query": '"Northern Trust"',
        "blog_url": "https://www.northerntrust.com/united-states/insights-research",
        "blog_glob": "https://www.northerntrust.com/united-states/insights-research/**",
        # The insights index renders its article links in JS behind Akamai, so
        # seed from the sitemap; 5+ segments skips the section hub pages.
        "blog_sitemap": "https://www.northerntrust.com/sitemap.xml",
        "blog_min_segments": 5,
        # Press releases live under /pr/<year>/, not under media-resources.
        "newsroom_url": "https://www.northerntrust.com/united-states/about-us/media-resources/press-release-archive",
        "newsroom_glob": "https://www.northerntrust.com/united-states/pr/**",
    },
    "blackrock": {
        "display_name": "BlackRock",
        "ticker": "BLK",
        "linkedin_url": "https://www.linkedin.com/company/blackrock/",
        "linkedin_jobs_query": "https://www.linkedin.com/company/blackrock/",
        "twitter_handle": "@blackrock",
        "reddit_query": '"BlackRock"',
        "reddit_keywords": ["blackrock", "blk", "ishares"],
        "sec_cik": "0002012383",
        "news_query": '"BlackRock"',
        "blog_url": "https://www.blackrock.com/corporate/insights",
        "blog_glob": "https://www.blackrock.com/corporate/insights/**",
        "newsroom_url": "https://www.blackrock.com/corporate/newsroom",
        "newsroom_glob": "https://www.blackrock.com/corporate/newsroom/**",
    },
    "vanguard": {
        "display_name": "The Vanguard Group",
        "ticker": None,  # privately held, client-owned
        "linkedin_url": "https://www.linkedin.com/company/vanguard/",
        "linkedin_jobs_query": "https://www.linkedin.com/company/vanguard/",
        "twitter_handle": "@Vanguard_Group",
        # "Vanguard" also names a Riot Games anti-cheat driver and a Marvel
        # character, so the query is narrowed and gaming hits are excluded.
        "reddit_query": '"Vanguard Group" OR "Vanguard funds" OR "Vanguard ETF" OR "Vanguard brokerage"',
        "reddit_keywords": ["vanguard", "vti", "voo", "vtsax", "vxus", "bogle"],
        "reddit_exclude": [
            "valorant", "riot games", "league of legends", "anti-cheat",
            "anticheat", "kernel driver", "marvel", "call of duty", "warzone",
            "cheaters", "r/valorant", "r/leagueoflegends", "r/marvelstudios",
        ],
        # Vanguard Group Inc — files 13F/13G as an institutional manager.
        "sec_cik": "0000102909",
        "news_query": '"Vanguard Group" OR "Vanguard funds"',
        "blog_url": "https://corporate.vanguard.com/content/corporatesite/us/en/corp/what-we-think/investing-insights/perspectives-and-commentary.html",
        "blog_glob": "https://corporate.vanguard.com/content/corporatesite/us/en/corp/what-we-think/**",
        "newsroom_url": "https://corporate.vanguard.com/content/corporatesite/us/en/corp/who-we-are/pressroom/index.html",
        "newsroom_glob": "https://corporate.vanguard.com/content/corporatesite/us/en/corp/who-we-are/pressroom/press-release-*",
        # The archive lists years of releases; crawling in document order finds
        # the oldest. Seed from the index and sort by the date in the slug.
        "newsroom_seed_from_index": True,
    },
}
COMPANY_TARGETS.update(custom_targets.load_section("companies"))

# Convenience aliases so the CLI accepts what people actually type.
ALIASES = {
    "bny": "bny",
    "bnymellon": "bny",
    "bny_mellon": "bny",
    "bank-of-new-york-mellon": "bny",
    "bk": "bny",
    "northern_trust": "northern_trust",
    "northerntrust": "northern_trust",
    "northern-trust": "northern_trust",
    "nt": "northern_trust",
    "ntrs": "northern_trust",
    "blackrock": "blackrock",
    "black_rock": "blackrock",
    "blk": "blackrock",
    "vanguard": "vanguard",
    "vanguard_group": "vanguard",
    "the_vanguard_group": "vanguard",
}
for _key in custom_targets.load_section("companies"):
    ALIASES.setdefault(_key, _key)


def resolve(company: str) -> Dict[str, Any]:
    """Look up a company target by key or alias (case/spacing insensitive)."""
    key = ALIASES.get(company.strip().lower().replace(" ", "_"))
    if not key:
        raise KeyError(
            f"Unknown company '{company}'. Known: {', '.join(sorted(COMPANY_TARGETS))}"
        )
    return {"key": key, **COMPANY_TARGETS[key]}


def save(target: Dict[str, Any]) -> Dict[str, Any]:
    """Register a company target at runtime (e.g. from the Run Pipeline
    page's "Save as Target" button) and persist it to custom_targets.json
    so it's still known next time the process starts.

    Returns the stored target with "key" folded back in, same shape as
    resolve(). Raises ValueError if target has no "key".
    """
    target = dict(target)
    key = (target.pop("key", "") or "").strip()
    if not key:
        raise ValueError("target.key is required")

    COMPANY_TARGETS[key] = target
    ALIASES.setdefault(key, key)
    custom_targets.save("companies", key, target)
    stored = {"key": key, **target}
    db.upsert_target("company", stored)
    return stored
