"""Filters out posts containing profanity, abuse, or explicit sexual language.

Applied once, in store.py's merge() — every channel's scraped posts pass
through the store before being persisted (JSON, then mirrored to
Postgres), so filtering here keeps this content out of storage, the
digest LLM prompt, and the frontend, in one place rather than duplicating
it per-scraper. Reddit is the channel most likely to trip this (see
CHANNEL_GUIDANCE in digest/prompts.py: "Never treat a hostile ... thread
as a market view") but any channel's post text/title is checked the
same way.

This is a plain, easily-edited word list rather than a moderation API —
add or remove terms below as needed. Matching is whole-word
(case-insensitive) so this doesn't false-positive on substrings (e.g. an
"assassinate" headline from a naive "ass" match), but it's still a blunt
instrument: it can't tell "fucking evil" (hostile rant, correctly
dropped) from a post that quotes hostile language while reporting on it.
Keep the list tight — false positives cost real signal.
"""
import re

BLOCKED_TERMS = [
    # Profanity / general abuse
    "fuck", "fucking", "fucked", "fucker", "motherfucker",
    "shit", "bullshit", "asshole", "bastard", "bitch", "bitches",
    "dumbass", "dipshit",
    # Explicit sexual terms
    "porn", "pornographic", "blowjob", "handjob", "cumshot",
    "dick pic", "nude pics", "sex tape",
    # Common slurs — kept minimal, enough to catch hateful/hostile
    # content, not an exhaustive slur database
    "retard", "retarded",
]

_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in BLOCKED_TERMS) + r")\b",
    re.IGNORECASE,
)


def is_abusive(*texts: str) -> bool:
    """True if any of the given text fields contain a blocked term."""
    combined = " ".join(t for t in texts if t)
    return bool(_PATTERN.search(combined))
