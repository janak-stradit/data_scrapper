"""Choosing and formatting the posts a digest looks at."""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from store import ScrapeStore
from . import capabilities
from .llm_client import LLMClient
from .prompts import (
    CHANNEL_GUIDANCE,
    CHANNEL_LABELS,
    CHANNEL_SYSTEM,
    EMAIL_SYSTEM,
    PERSON_CHANNEL_GUIDANCE,
    PERSON_CHANNEL_SYSTEM,
    PERSON_EMAIL_SYSTEM,
)


# Owned/press channels already carry full article text (Apify's full-page
# crawl, not just a headline) — truncating them at the same 600 chars used
# for short-form chatter cuts off exactly the quotes and named-vendor detail
# a "double-click" pitch needs. Chatter channels stay short since there's
# rarely more than a headline's worth of signal in them anyway.
_LONG_TEXT_CHANNELS = {"newsroom", "blog", "rss", "linkedin_jobs"}

# Capability matching ("does this map to one of our offerings?") only pays
# off on channels where a company actually announces vendors, technology, or
# initiatives — not on Reddit chatter or a routine Form 4. Sending the
# reference table on every channel call regardless of relevance was pure
# token overhead for no analytical benefit, so restrict it to where a real
# match is plausible. linkedin_jobs is often the *best* fit here — a role
# posting for a specific technology is an earlier, cleaner signal than a
# press release.
_CAPABILITY_CHANNELS = {"newsroom", "blog", "news", "rss", "linkedin_jobs"}

# These two channels routinely carry 20 posts (the CLI/API default cap), most
# of which never get cited in the final digest anyway. Capping input here
# cuts token cost roughly in half for the two highest-volume channels with
# minimal loss — the newest/most relevant posts are what select_posts already
# prioritises first.
_HIGH_VOLUME_CAP = {"news": 12, "blog": 12}


def _post_line(post: Dict[str, Any], channel: str = "") -> str:
    """One compact line per post, including the URL the model must cite."""
    date = (post.get("published_at") or "")[:16]
    title = post.get("title") or ""
    limit = 3500 if channel in _LONG_TEXT_CHANNELS else 600
    text = " ".join((post.get("text") or "").split())[:limit]
    url = post.get("post_url") or ""

    parts = [p for p in (date, title, text) if p]
    line = "- " + " | ".join(parts)
    return f"{line}\n  source_url: {url}" if url else line


def _recent(posts: List[Dict[str, Any]], since_days: int) -> List[Dict[str, Any]]:
    """Posts published within the window, keeping undated ones."""
    if not since_days:
        return posts
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    kept = []
    for post in posts:
        raw = (post.get("published_at") or "").strip()
        if not raw:
            kept.append(post)
            continue
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                when = datetime.strptime(raw.replace("Z", "").split(".")[0], fmt)
                if when.replace(tzinfo=timezone.utc) >= cutoff:
                    kept.append(post)
                break
            except ValueError:
                continue
        else:
            kept.append(post)
    return kept


def select_posts(
    store: ScrapeStore, channel: str, new_only: bool, since_days: int, cap: int
) -> List[Dict[str, Any]]:
    """Which posts this digest should look at for one channel."""
    posts = store.doc.get("data", {}).get(channel, {}).get("posts", [])
    if new_only:
        fresh = [p for p in posts if p.get("new_in_last_run")]
        # Fall back to the recency window when the last run added nothing.
        posts = fresh if fresh else _recent(posts, since_days)
    else:
        posts = _recent(posts, since_days)
    effective_cap = min(cap, _HIGH_VOLUME_CAP.get(channel, cap))
    return posts[:effective_cap]


def summarize_channel(
    client: LLMClient,
    subject: str,
    channel: str,
    posts: List[Dict[str, Any]],
    kind: str = "company",
) -> Dict[str, Any]:
    """Run one channel through the model.

    kind: "company" (account digest) or "person" (individual contact digest) —
    picks the matching guidance table and system prompt.
    """
    is_person = kind == "person"
    guidance = (PERSON_CHANNEL_GUIDANCE if is_person else CHANNEL_GUIDANCE).get(channel)
    system = PERSON_CHANNEL_SYSTEM if is_person else CHANNEL_SYSTEM
    label = "Person" if is_person else "Company"
    prompt = (
        f"{label}: {subject}\n"
        f"Channel: {CHANNEL_LABELS.get(channel, channel)}\n"
        + (f"How to read this channel: {guidance}\n" if guidance else "")
        + f"\nPosts ({len(posts)}), newest first. Cite these source_urls "
        "exactly as given:\n\n"
        + "\n".join(_post_line(p, channel) for p in posts)
        # Capability matching is a company-account concept (our offerings vs.
        # what the account is doing) — not meaningful for a person digest,
        # and only worth the tokens on channels that actually announce
        # vendors/technology/initiatives (see _CAPABILITY_CHANNELS above).
        + (
            f"\n\n{capabilities.context_block()}"
            if not is_person and channel in _CAPABILITY_CHANNELS
            else ""
        )
    )
    result = client.complete_json(system, prompt)
    result["channel"] = channel
    result["channel_label"] = CHANNEL_LABELS.get(channel, channel)
    result["posts_considered"] = len(posts)
    return result


def build_email(
    client: LLMClient,
    subject: str,
    ticker: str,
    channels: List[Dict[str, Any]],
    kind: str = "company",
) -> Dict[str, Any]:
    """Roll the channel summaries into one account- or contact-level briefing.

    kind: "company" (account digest) or "person" (individual contact digest).
    """
    is_person = kind == "person"
    system = PERSON_EMAIL_SYSTEM if is_person else EMAIL_SYSTEM
    label = "Contact" if is_person else "Account"
    blocks = []
    for ch in channels:
        observed = "\n".join(
            f"  - {o.get('fact', '')} [{o.get('source_url', '')}]"
            for o in (ch.get("observed") or [])[:8]
        )
        notable = "\n".join(
            f"  - {n.get('headline', '')} [{n.get('source_url', '')}]"
            for n in (ch.get("notable_posts") or [])[:4]
        )
        avoid = "; ".join(ch.get("do_not_say") or [])
        matches = "\n".join(
            f"  - theme: {m.get('theme', '')}\n"
            f"    offering: {m.get('offering', '')}\n"
            f"    source_url: {m.get('source_url', '')}\n"
            f"    supporting_quote: {m.get('supporting_quote', '')}\n"
            f"    pitch: {m.get('pitch', '')}"
            for m in (ch.get("capability_matches") or [])[:3]
        )
        blocks.append(
            f"## {ch['channel_label']} ({ch['posts_considered']} posts, "
            f"evidence: {ch.get('evidence_strength', 'unrated')})\n"
            f"Evidence note: {ch.get('evidence_note', '')}\n"
            f"Summary: {ch.get('summary', '')}\n"
            f"Observed facts:\n{observed or '  (none recorded)'}\n"
            f"Notable posts:\n{notable or '  (none)'}\n"
            f"Interpretation: {ch.get('interpretation', '')}\n"
            f"Themes: {', '.join(ch.get('themes', []) or [])}\n"
            f"Sales angle: {ch.get('sales_angle', '')}\n"
            + (f"Capability matches:\n{matches}\n" if matches else "")
            + (f"DO NOT SAY: {avoid}\n" if avoid else "")
        )
    prompt = (
        f"{label}: {subject}" + (f" ({ticker})" if ticker else "") + "\n"
        f"Date: {datetime.now(timezone.utc):%d %B %Y}\n\n"
        "Channel summaries:\n\n" + "\n\n".join(blocks)
    )
    return client.complete_json(system, prompt)


