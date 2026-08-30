"""Digest pipeline — per-channel summaries rolled into an account email."""
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

from paths import DIGEST_DIR, digest_path, store_path
from store import ScrapeStore
from targets import resolve as resolve_company
from people_targets import resolve as resolve_person
import db

from . import cache
from .llm_client import LLMClient, LLMError, channel_model, describe_config
from .renderer import render_markdown
from .selection import build_email, select_posts, summarize_channel


def run(
    company_key: str = None,
    new_only: bool = True,
    since_days: int = 14,
    cap: int = 25,
    out_dir: str = DIGEST_DIR,
    store_path_override: str = None,
    kind: str = "company",
    target: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """Generate one account's (or one person's) digest and write JSON + Markdown.

    kind: "company" (targets.py, the default) or "person" (people_targets.py).
    Pass `target` (a dict with at least "key" and "display_name") to digest
    an ad-hoc target that isn't registered in targets.py/people_targets.py.
    """
    is_person = kind == "person"
    target = target or (
        resolve_person(company_key) if is_person else resolve_company(company_key)
    )
    key = target["key"]
    target.setdefault("display_name", key)
    path = store_path_override or store_path(key)

    store = ScrapeStore(path)
    if not store.exists:
        cli_flag = " --person" if is_person else ""
        raise FileNotFoundError(
            f"No store at {path}. Run: python main.py scrape {key}{cli_flag} --limit 20"
        )

    channel_client = LLMClient(model=channel_model())
    # The email step aggregates every channel's output in one call, so it
    # needs more output headroom than any single channel does — this is what
    # was hitting the 8000-token ceiling and getting cut off mid-JSON for
    # accounts with several channels' worth of capability_matches to roll up.
    email_client = LLMClient(max_tokens=16000)
    # A dry-run's placeholder text is deterministic *per prompt*, not a real
    # summary — caching it would mean a later run with a real key silently
    # keeps serving stale "[DRY RUN]" text instead of ever calling the model.
    use_cache = channel_client.provider != "dry-run" and channel_client.configured
    print(
        f"🧠  {target['display_name']} — LLM: {describe_config()} "
        f"(channels: {channel_client.model})"
    )

    channels, considered = [], 0
    for channel in store.doc.get("data", {}):
        posts = select_posts(store, channel, new_only, since_days, cap)
        if not posts:
            print(f"   {channel:<9} no posts in scope, skipped")
            continue

        sig = cache.signature(channel, posts)
        cached = cache.get(key, channel, sig) if use_cache else None
        if cached is not None:
            print(f"   {channel:<9} unchanged since last digest, reusing cached summary")
            channels.append(cached)
            considered += len(posts)
            continue

        print(f"   {channel:<9} summarising {len(posts)} posts…")
        try:
            result = summarize_channel(
                channel_client, target["display_name"], channel, posts, kind=kind
            )
            channels.append(result)
            considered += len(posts)
            if use_cache:
                cache.put(key, channel, sig, result)
        except LLMError as e:
            print(f"   {channel:<9} ❌ {e}")

    if not channels:
        raise RuntimeError("Nothing to summarise — no posts in scope.")

    try:
        email = build_email(
            email_client, target["display_name"], target.get("ticker"), channels, kind=kind
        )
    except LLMError as e:
        # Every channel above already made (and paid for) a real LLM call —
        # discarding all of that because only the final rollup call failed
        # is the single most expensive failure mode in this pipeline. Write
        # a degraded but honest email instead of losing that work; the
        # channel storylines below it are unaffected and still real.
        print(f"   email     ❌ {e}")
        email = {
            "subject": f"{target['display_name']} — digest (email synthesis failed)",
            "body": f"Channel-level summaries below are real and complete, but "
            f"the final email rollup failed: {e}. Re-run the digest to retry "
            "just the email step — the channel data is already cached.",
            "talking_points": [],
            "capability_opportunities": [],
            "priority": "low",
            "priority_reason": "Email synthesis failed; see data_gaps.",
            "confidence": "low",
            "do_not_say": [],
            "data_gaps": [f"Email synthesis error: {e}"],
        }

    digest = {
        "company": target["display_name"],
        "company_key": key,
        "kind": kind,
        "ticker": target.get("ticker"),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope": {
            "new_only": new_only,
            "since_days": since_days,
            "max_posts_per_channel": cap,
            "store": path,
            "store_last_run": store.last_run(),
        },
        "llm": describe_config(),
        "posts_considered": considered,
        "email": email,
        "channels": channels,
    }

    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, f"{key}_digest.json")
    md_path = os.path.join(out_dir, f"{key}_digest.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(digest, fh, indent=2, ensure_ascii=False)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(digest))

    print(f"💾  {json_path}")
    print(f"💾  {md_path}")
    db.upsert_digest(key, kind, digest)
    return digest


