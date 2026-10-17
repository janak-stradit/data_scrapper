"""One scrape run (+ optional digest), recorded in run history.

Shared by POST /api/run (main.py, in a background thread) and the
scheduler (scheduler.py), so a scheduled run is recorded and behaves
exactly like one started from the frontend.
"""
import asyncio
import time

import history


def start(kind: str, target: dict, limit: int, only=None, trigger: str = None) -> dict:
    """Records the "running" history entry and returns it (with run_id)."""
    entry = {
        "kind": kind,
        "key": target["key"],
        "display_name": target.get("display_name") or target["key"],
        "limit": limit,
    }
    if only:
        entry["only"] = only
    if trigger:
        entry["trigger"] = trigger  # e.g. "schedule:news" — absent for manual runs
    return history.start(entry)


def execute(run_id: str, kind: str, target: dict, limit: int, only=None, generate_digest: bool = False) -> dict:
    """Runs the scrape (and digest if asked), finishes the history entry,
    and returns the updates written to it. Never raises — a failure is
    recorded as status "failed" with its error, same as a manual run."""
    from engine import scrape_and_store
    from digest.pipeline import run as run_digest
    from digest.llm_client import LLMError

    t0 = time.time()
    updates = {}
    try:
        stored = asyncio.run(scrape_and_store(limit=limit, kind=kind, target=target, only=only))
        meta = stored.get("metadata", {})
        updates["new_posts"] = meta.get("new_last_run")
        updates["total_posts"] = meta.get("total_posts")
        updates["platforms_scraped"] = meta.get("platforms_scraped", [])
        updates["platforms_failed"] = meta.get("platforms_failed", [])
        updates["cost_usd"] = meta.get("total_cost_usd", 0.0)

        if generate_digest:
            try:
                digest = run_digest(kind=kind, target=target)
                updates["digest"] = {
                    "llm": digest.get("llm"),
                    "posts_considered": digest.get("posts_considered"),
                    "input_tokens": digest.get("input_tokens", 0),
                    "output_tokens": digest.get("output_tokens", 0),
                }
            except (LLMError, RuntimeError, FileNotFoundError) as e:
                updates["digest"] = {"error": str(e)}

        updates["success"] = True
        updates["status"] = "success"
    except Exception as e:
        updates["success"] = False
        updates["error"] = str(e)
        updates["status"] = "failed"

    updates["duration_ms"] = int((time.time() - t0) * 1000)
    history.finish(run_id, updates)
    return updates
