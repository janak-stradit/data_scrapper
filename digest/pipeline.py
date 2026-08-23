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

from .llm_client import LLMClient, LLMError, describe_config
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

    client = LLMClient()
    print(f"🧠  {target['display_name']} — LLM: {describe_config()}")

    channels, considered = [], 0
    for channel in store.doc.get("data", {}):
        posts = select_posts(store, channel, new_only, since_days, cap)
        if not posts:
            print(f"   {channel:<9} no posts in scope, skipped")
            continue
        print(f"   {channel:<9} summarising {len(posts)} posts…")
        try:
            channels.append(
                summarize_channel(client, target["display_name"], channel, posts, kind=kind)
            )
            considered += len(posts)
        except LLMError as e:
            print(f"   {channel:<9} ❌ {e}")

    if not channels:
        raise RuntimeError("Nothing to summarise — no posts in scope.")

    email = build_email(
        client, target["display_name"], target.get("ticker"), channels, kind=kind
    )

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


