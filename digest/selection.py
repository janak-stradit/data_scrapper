"""Choosing and formatting the posts a digest looks at."""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from store import ScrapeStore
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


def _post_line(post: Dict[str, Any]) -> str:
    """One compact line per post, including the URL the model must cite."""
    date = (post.get("published_at") or "")[:16]
    title = post.get("title") or ""
    text = " ".join((post.get("text") or "").split())[:600]
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
    return posts[:cap]


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
        + "\n".join(_post_line(p) for p in posts)
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
            + (f"DO NOT SAY: {avoid}\n" if avoid else "")
        )
    prompt = (
        f"{label}: {subject}" + (f" ({ticker})" if ticker else "") + "\n"
        f"Date: {datetime.now(timezone.utc):%d %B %Y}\n\n"
        "Channel summaries:\n\n" + "\n\n".join(blocks)
    )
    return client.complete_json(system, prompt)


