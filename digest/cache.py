"""Per-channel LLM result cache, keyed on the exact set of posts summarised.

`digest --all` re-summarises every channel from scratch on every run, even
when nothing changed — which is exactly what happened across three back-to-
back debug runs of the same 4 companies while chasing an unrelated bug.
Each channel's post selection is deterministic given the same store, so if
the same posts are in scope as last time, the LLM call is pure waste; reuse
the cached result instead.

One JSON file per target under output/digests/.cache/, mapping channel name
to {"signature": ..., "result": ...}. Best-effort: a missing/corrupt cache
file just means everything gets summarised fresh, same as today.
"""
import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from paths import DIGEST_DIR
from store import post_key

_CACHE_DIR = os.path.join(DIGEST_DIR, ".cache")


def _cache_path(target_key: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, f"{target_key}.json")


def signature(channel: str, posts: List[Dict[str, Any]]) -> str:
    """Fingerprint of exactly which posts (and nothing else) are in scope."""
    ids = sorted(post_key(channel, p) for p in posts)
    return hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()


def get(target_key: str, channel: str, sig: str) -> Optional[Dict[str, Any]]:
    path = _cache_path(target_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    entry = data.get(channel)
    if entry and entry.get("signature") == sig:
        return entry.get("result")
    return None


def put(target_key: str, channel: str, sig: str, result: Dict[str, Any]) -> None:
    path = _cache_path(target_key)
    data: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            data = {}
    data[channel] = {"signature": sig, "result": result}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
