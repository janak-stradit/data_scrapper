"""Incremental store for scraped posts.

The output file per company is the store: re-running the pipeline adds only
posts that were not seen before, and never rewrites a post that is already
there. It also records what was seen, so the next run can narrow what it asks
the paid actors for instead of re-fetching the same posts.

Retention: posts first stored more than config.STORE_RETENTION_DAYS ago are
dropped from the file on the next merge, so stores don't grow forever. Each
dropped post's key is remembered (store.expired_keys, for EXPIRED_KEY_DAYS)
so a source that still lists it doesn't bring it back as "new" — no repeat
digest entry, no re-fetching the article. JSON only: the Postgres mirror
(db.py) keeps every post it was ever sent.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import STORE_RETENTION_DAYS
from profanity_filter import is_abusive

# How long an expired post's key is remembered after it's dropped. Sources
# rarely resurface anything older; past this, the key list would only grow.
EXPIRED_KEY_DAYS = 180


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def post_key(channel: str, post: Dict[str, Any]) -> str:
    """Stable identity for a post, used to detect duplicates across runs.

    URLs are the natural key almost everywhere; SEC uses the accession number
    (permanent), and anything without a URL falls back to author + text.
    """
    if channel == "sec" and post.get("accession"):
        return f"sec:{post['accession']}"

    if channel in ("news", "bloomberg"):
        # Google News RSS links carry a per-request token, so the same article
        # arrives with a different URL every fetch — key on the headline.
        headline = post.get("title") or post.get("text") or ""
        return f"{channel}:{re.sub(r'[^a-z0-9]+', '', headline.lower())[:120]}"

    url = (post.get("post_url") or "").strip()
    if url:
        # Drop tracking/query noise so the same article isn't stored twice.
        return f"{channel}:{url.split('?')[0].rstrip('/')}"

    text = (post.get("text") or post.get("title") or "")[:120]
    return f"{channel}:{post.get('author', '')}:{text}"


class ScrapeStore:
    """Loads, de-duplicates, and saves one company's accumulated posts."""

    def __init__(self, path: str):
        self.path = path
        self.doc: Dict[str, Any] = self._load()
        # Channels to re-fetch in full rather than incrementally this run.
        self.force_full: set = set()

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️  [Store] Could not read {self.path} ({e}); starting fresh")
            return {}

    @property
    def exists(self) -> bool:
        return bool(self.doc)

    # ── What we already have ─────────────────────────────────────

    def _expired_keys(self, channel: str) -> Dict[str, str]:
        return self.doc.get("store", {}).get("expired_keys", {}).get(channel, {})

    def seen_keys(self, channel: str) -> set:
        posts = self.doc.get("data", {}).get(channel, {}).get("posts", [])
        return {post_key(channel, p) for p in posts} | set(self._expired_keys(channel))

    def seen_urls(self, channel: str) -> set:
        posts = self.doc.get("data", {}).get(channel, {}).get("posts", [])
        urls = {
            (p.get("post_url") or "").split("?")[0].rstrip("/")
            for p in posts
            if p.get("post_url")
        }
        # Expired posts too, so they aren't crawled again. URL-keyed
        # channels' keys are "<channel>:<normalized url>" (post_key).
        prefix = f"{channel}:"
        urls |= {k[len(prefix):] for k in self._expired_keys(channel)
                 if k.startswith(prefix + "http")}
        return urls

    def last_run(self) -> Optional[str]:
        return self.doc.get("store", {}).get("last_run")

    def days_since_last_run(
        self, default: int = 30, cap: int = 30, channel: str = None
    ) -> int:
        """How many days to ask news/social for — smaller windows cost less."""
        if channel and channel in self.force_full:
            return default
        last = self.last_run()
        if not last:
            return default
        try:
            then = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            return default
        gap = (datetime.now(timezone.utc) - then).days
        # Always overlap by a day so nothing slips through a boundary.
        return max(1, min(gap + 1, cap))

    # ── Merging a new run in ─────────────────────────────────────

    def dedupe(self) -> int:
        """Collapse posts already stored under more than one key.

        Needed when a keying rule changes (or a source hands out unstable URLs)
        after some runs have already been written.
        """
        removed = 0
        for channel, block in self.doc.get("data", {}).items():
            seen, kept = {}, []
            for post in block.get("posts", []):
                key = post_key(channel, post)
                if key in seen:
                    # Keep the copy we saw first.
                    if post.get("first_seen", "") < seen[key].get("first_seen", ""):
                        kept[kept.index(seen[key])] = post
                        seen[key] = post
                    removed += 1
                    continue
                seen[key] = post
                kept.append(post)
            block["posts"] = kept
            block["count"] = len(kept)
        return removed

    def purge_abusive(self) -> int:
        """Remove already-stored posts matching profanity_filter.

        merge() keeps every future scrape clean; this is for posts that
        were stored before that filter existed. Doesn't save() — caller
        decides when to persist.
        """
        removed = 0
        for block in self.doc.get("data", {}).values():
            posts = block.get("posts", [])
            kept = [p for p in posts if not is_abusive(p.get("text", ""), p.get("title", ""))]
            removed += len(posts) - len(kept)
            block["posts"] = kept
            block["count"] = len(kept)
        if removed:
            self.doc["metadata"]["total_posts"] = sum(
                b.get("count", 0) for b in self.doc.get("data", {}).values()
            )
        return removed

    def expire(self, retention_days: int = STORE_RETENTION_DAYS) -> int:
        """Drop posts first stored more than `retention_days` ago, remembering
        their keys (see module docstring); 0 or less keeps everything. Posts
        from before first_seen existed get stamped now, so their clock starts
        today. Returns how many posts were dropped. Doesn't save()."""
        if retention_days <= 0 or not self.doc:
            return 0
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        expired = self.doc.setdefault("store", {}).setdefault("expired_keys", {})
        dropped = 0
        for channel, block in self.doc.get("data", {}).items():
            if not isinstance(block, dict):
                continue
            kept = []
            for post in block.get("posts", []):
                seen = _parse_ts(post.get("first_seen"))
                if seen is None:
                    post["first_seen"] = _now()
                elif seen < cutoff:
                    expired.setdefault(channel, {})[post_key(channel, post)] = _now()
                    dropped += 1
                    continue
                kept.append(post)
            block["posts"] = kept
            block["count"] = len(kept)

        forget_before = now - timedelta(days=EXPIRED_KEY_DAYS)
        for channel in list(expired):
            keys = expired[channel]
            for key in [k for k, at in keys.items() if (_parse_ts(at) or now) < forget_before]:
                del keys[key]
            if not keys:
                del expired[channel]
        return dropped

    def merge(self, result: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Dict[str, int]]]:
        """Fold a fresh scrape into the store, keeping one copy of each post.

        Returns (stored document, per-channel {"new": n, "duplicate": n}).
        """
        run_at = _now()
        stats: Dict[str, Dict[str, int]] = {}

        collapsed = self.dedupe()
        if collapsed:
            print(f"🧹  [Store] Collapsed {collapsed} duplicate posts already on file")
        dropped = self.expire()
        if dropped:
            print(f"🗓️   [Store] Dropped {dropped} post(s) stored more than {STORE_RETENTION_DAYS} days ago")

        merged = self.doc if self.doc else {
            "success": True,
            "company": result.get("company", {}),
            "query": {},
            "data": {},
            "metadata": {},
            "store": {"created_at": run_at, "runs": 0},
        }

        merged["company"] = result.get("company", merged.get("company", {}))
        merged["query"] = {**merged.get("query", {}), **result.get("query", {})}

        for channel, block in result.get("data", {}).items():
            if channel == "error":
                continue

            existing_block = merged["data"].get(channel, {"posts": []})
            existing_posts = existing_block.get("posts", [])
            index = {post_key(channel, p): p for p in existing_posts}
            expired = merged.get("store", {}).get("expired_keys", {}).get(channel, {})

            new_count = 0
            filtered_count = 0
            for post in block.get("posts", []):
                if is_abusive(post.get("text", ""), post.get("title", "")):
                    filtered_count += 1
                    continue
                key = post_key(channel, post)
                if key in index:
                    # Already stored — just note that it is still live.
                    index[key]["last_seen"] = run_at
                    index[key]["new_in_last_run"] = False
                    continue
                if key in expired:
                    continue  # stored before and aged out — not new again
                post["first_seen"] = run_at
                post["last_seen"] = run_at
                post["new_in_last_run"] = True
                existing_posts.append(post)
                index[key] = post
                new_count += 1
            if filtered_count:
                print(f"🚫  [Store] Filtered {filtered_count} {channel} post(s) with abusive/explicit language")

            # Posts absent from this run stay stored, but are no longer "new".
            for post in existing_posts:
                if post.get("last_seen") != run_at:
                    post["new_in_last_run"] = False

            existing_posts.sort(key=_sort_key, reverse=True)
            for rank, post in enumerate(existing_posts, start=1):
                post["rank"] = rank

            merged["data"][channel] = {
                "success": block.get("success", True),
                "error": block.get("error"),
                "count": len(existing_posts),
                "new_last_run": new_count,
                "posts": existing_posts,
            }
            if merged["data"][channel]["error"] is None:
                merged["data"][channel].pop("error")

            stats[channel] = {
                "new": new_count,
                "duplicate": len(block.get("posts", [])) - new_count - filtered_count,
                "filtered": filtered_count,
            }

        totals = sum(b.get("count", 0) for b in merged["data"].values())
        merged["metadata"] = {
            **result.get("metadata", {}),
            "total_posts": totals,
            "new_last_run": sum(s["new"] for s in stats.values()),
            "duplicates_skipped": sum(s["duplicate"] for s in stats.values()),
        }
        merged["store"] = {
            **merged.get("store", {}),
            "last_run": run_at,
            "runs": merged.get("store", {}).get("runs", 0) + 1,
            "path": self.path,
        }
        merged["success"] = not result.get("metadata", {}).get("platforms_failed")

        self.doc = merged
        return merged, stats

    def save(self) -> None:
        # Atomic, so the web server never serves a half-written store.
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.doc, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def new_posts(self, channel: str = None) -> List[Dict[str, Any]]:
        """Posts added by the most recent run — the input to the digest."""
        out = []
        for ch, block in self.doc.get("data", {}).items():
            if channel and ch != channel:
                continue
            out.extend([p for p in block.get("posts", []) if p.get("new_in_last_run")])
        return out


def _sort_key(post: Dict[str, Any]) -> Tuple[int, str]:
    """Newest first, with undated posts last rather than interleaved."""
    raw = post.get("published_at") or ""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %Z",
                "%a %b %d %H:%M:%S %z %Y"):
        try:
            cleaned = raw.replace("Z", "").split(".")[0]
            return (1, datetime.strptime(cleaned, fmt).isoformat())
        except (ValueError, TypeError):
            continue
    return (0, str(raw))
