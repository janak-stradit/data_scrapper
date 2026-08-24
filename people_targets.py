"""Individual (person-level) targets for the scraper engine.

Mirrors targets.py, but for people instead of companies. Each entry maps a
person key to their public presence on every channel the engine can scrape
for an individual: LinkedIn, Reddit, Twitter/X, SEC EDGAR (only if they file
as a corporate insider or beneficial owner), general news coverage, and
patents (Google Patents inventor search).

Blog/newsroom don't apply to individuals (those are company-owned content
sections) and are intentionally omitted here.

Add new people here — no engine changes needed once engine.py/main.py are
wired to read from this table too.

Targets saved from the frontend's "Save as Target" button live in
custom_targets.json instead (see save() below) and are merged in below,
so they resolve the same way as anything hand-written here.
"""
from typing import Dict, Any

import custom_targets
import db

PEOPLE_TARGETS: Dict[str, Dict[str, Any]] = {
    "ranjit_samra": {
        "display_name": "Ranjit S. Samra (CIO, BNY)",
        "linkedin_url": "https://www.linkedin.com/in/ranjitsamra",
        # No public Twitter/X handle found yet — add one if you find it.
        "twitter_handle": None,
        # No Reddit presence found — fall back to a mentions search instead
        # of a username, same convention as targets.py.
        "reddit_query": '"Ranjit Samra"',
        # Set this if he turns out to file SEC insider forms (Form 3/4/5)
        # under his own CIK — check EDGAR full-text search by name.
        "sec_cik": None,
        "news_query": '"Ranjit Samra"',
        # Plain name for Google Patents' inventor search — no titles or
        # parentheticals, those would corrupt the query.
        "patents_query": "Ranjit Samra",
    },
    "robin_vince": {
        "display_name": "Robin Vince (Chairman, President & CEO, BNY)",
        "linkedin_url": "https://www.linkedin.com/in/robin-vince/",
        # No public Twitter/X handle found — add one if you find it.
        "twitter_handle": None,
        "reddit_query": '"Robin Vince"',
        # Files Form 3/4/5 as a BNY Section 16 officer — CIK confirmed via
        # EDGAR's reporting-owner search (browse-edgar?company=vince+robin).
        "sec_cik": "0001825740",
        "news_query": '"Robin Vince"',
        "patents_query": "Robin Vince",
        # Third-party filings naming him — clean signal for an individual
        # name (verified live: 157 hits vs. BNY-the-company's 10,000+),
        # already surfaced a real exhibit (an 8-K press-release attachment)
        # beyond what sec_cik alone finds.
        "sec_mentions_query": "Robin Vince",
    },
}
PEOPLE_TARGETS.update(custom_targets.load_section("people"))

# Convenience aliases so the CLI accepts what people actually type.
ALIASES: Dict[str, str] = {
    "ranjit_samra": "ranjit_samra",
    "ranjitsamra": "ranjit_samra",
    "ranjit-samra": "ranjit_samra",
    "ranjit_s_samra": "ranjit_samra",
    "robin_vince": "robin_vince",
    "robinvince": "robin_vince",
    "robin-vince": "robin_vince",
    "robin": "robin_vince",
}
for _key in custom_targets.load_section("people"):
    ALIASES.setdefault(_key, _key)


def resolve(person: str) -> Dict[str, Any]:
    """Look up a person target by key or alias (case/spacing insensitive)."""
    key = ALIASES.get(person.strip().lower().replace(" ", "_"))
    if not key:
        raise KeyError(
            f"Unknown person '{person}'. Known: {', '.join(sorted(PEOPLE_TARGETS))}"
        )
    return {"key": key, **PEOPLE_TARGETS[key]}


def save(target: Dict[str, Any]) -> Dict[str, Any]:
    """Register a person target at runtime (e.g. from the Run Pipeline
    page's "Save as Target" button) and persist it to custom_targets.json
    so it's still known next time the process starts.

    Returns the stored target with "key" folded back in, same shape as
    resolve(). Raises ValueError if target has no "key".
    """
    target = dict(target)
    key = (target.pop("key", "") or "").strip()
    if not key:
        raise ValueError("target.key is required")

    PEOPLE_TARGETS[key] = target
    ALIASES.setdefault(key, key)
    custom_targets.save("people", key, target)
    stored = {"key": key, **target}
    db.upsert_target("person", stored)
    return stored
