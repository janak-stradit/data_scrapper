"""Optional Postgres mirror of the JSON files under output/.

Every write here is best-effort: if DATABASE_URL isn't set in .env, or the
database is unreachable, these functions log a warning and return without
raising. The JSON files stay the source of truth for the frontend and CLI
either way — this module exists so the same data is also queryable with
SQL (e.g. across every target/channel/date at once, which the per-target
JSON files can't do).

Schema is created lazily on first successful connection — no separate
migration step.
"""
import json
from typing import Any, Dict

from config import DATABASE_URL
from store import post_key

_SCHEMA_READY = False

# Fields already broken out into their own columns — everything else on a
# post goes into the `extra` JSONB column instead of being dropped.
_CORE_POST_FIELDS = {
    "platform", "rank", "post_url", "text", "author", "published_at",
    "engagement", "media", "scraped_at", "first_seen", "last_seen",
    "new_in_last_run",
}

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS targets (
        key TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        display_name TEXT NOT NULL,
        ticker TEXT,
        config JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS posts (
        id BIGSERIAL PRIMARY KEY,
        target_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        channel TEXT NOT NULL,
        post_key TEXT NOT NULL,
        rank INT,
        post_url TEXT,
        body TEXT,
        author TEXT,
        published_at TEXT,
        engagement JSONB,
        media JSONB,
        extra JSONB,
        first_seen TIMESTAMPTZ,
        last_seen TIMESTAMPTZ,
        new_in_last_run BOOLEAN,
        raw JSONB NOT NULL,
        UNIQUE (target_key, channel, post_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS posts_target_channel_idx ON posts (target_key, channel)",
    """
    CREATE TABLE IF NOT EXISTS linkedin_jobs (
        id BIGSERIAL PRIMARY KEY,
        target_key TEXT NOT NULL REFERENCES targets (key) ON DELETE CASCADE,
        job_key TEXT NOT NULL,
        title TEXT,
        company_name TEXT,
        location TEXT,
        employment_type TEXT,
        workplace_type TEXT,
        posted_date TEXT,
        applicants INT,
        views INT,
        salary JSONB,
        job_url TEXT,
        description TEXT,
        first_seen TIMESTAMPTZ,
        last_seen TIMESTAMPTZ,
        new_in_last_run BOOLEAN,
        raw JSONB NOT NULL,
        UNIQUE (target_key, job_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS linkedin_jobs_target_idx ON linkedin_jobs (target_key)",
    """
    CREATE TABLE IF NOT EXISTS digests (
        target_key TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        generated_at TIMESTAMPTZ,
        llm TEXT,
        posts_considered INT,
        priority TEXT,
        digest JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_history (
        id BIGSERIAL PRIMARY KEY,
        recorded_at TIMESTAMPTZ NOT NULL,
        kind TEXT,
        target_key TEXT,
        display_name TEXT,
        limit_requested INT,
        new_posts INT,
        total_posts INT,
        platforms_scraped JSONB,
        platforms_failed JSONB,
        digest JSONB,
        success BOOLEAN,
        error TEXT,
        duration_ms INT,
        entry JSONB NOT NULL
    )
    """,
]


def _connect():
    if not DATABASE_URL:
        return None
    import psycopg2
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"⚠️  [DB] Could not connect ({e}); continuing with JSON only")
        return None


def _ensure_schema(conn) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with conn.cursor() as cur:
        for statement in _SCHEMA_STATEMENTS:
            cur.execute(statement)
    conn.commit()
    _SCHEMA_READY = True


def upsert_target(kind: str, target: Dict[str, Any]) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO targets (key, kind, display_name, ticker, config, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (key) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    display_name = EXCLUDED.display_name,
                    ticker = EXCLUDED.ticker,
                    config = EXCLUDED.config,
                    updated_at = now()
                """,
                (
                    target["key"],
                    kind,
                    target.get("display_name", target["key"]),
                    target.get("ticker"),
                    json.dumps(target),
                ),
            )
        conn.commit()
    except Exception as e:
        print(f"⚠️  [DB] Could not save target '{target.get('key')}' ({e})")
    finally:
        conn.close()


def delete_target(kind: str, key: str) -> None:
    """Remove a target and everything Postgres holds about it.

    Covers the row in `targets` plus every `posts`/`digests`/`run_history`
    row keyed to it — leaving those behind would let a "deleted" account
    keep reappearing via get_store()/get_digest()'s DB fallback.
    """
    conn = _connect()
    if conn is None:
        return
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM targets WHERE key = %s AND kind = %s", (key, kind))
            cur.execute("DELETE FROM posts WHERE target_key = %s AND kind = %s", (key, kind))
            cur.execute("DELETE FROM digests WHERE target_key = %s AND kind = %s", (key, kind))
            cur.execute("DELETE FROM run_history WHERE target_key = %s AND kind = %s", (key, kind))
            # No kind column here — linkedin_jobs is company-only (see
            # engine.py's scrape_company), so this is a no-op for persons.
            cur.execute("DELETE FROM linkedin_jobs WHERE target_key = %s", (key,))
        conn.commit()
    except Exception as e:
        print(f"⚠️  [DB] Could not delete target '{key}' ({e})")
    finally:
        conn.close()


def list_targets(kind: str) -> list:
    """Every target of one kind Postgres knows about — key + full config.

    Includes ad-hoc targets that were scraped via /api/run and mirrored
    here (engine.py calls upsert_target on every scrape) but never
    explicitly registered via /api/save-target — those don't exist in
    targets.py/people_targets.py/custom_targets.json at all, so this is
    the only place they're discoverable from.
    """
    conn = _connect()
    if conn is None:
        return []
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, config FROM targets WHERE kind = %s ORDER BY key",
                (kind,),
            )
            rows = cur.fetchall()
        return [{"key": key, "config": config} for key, config in rows]
    except Exception as e:
        print(f"⚠️  [DB] Could not list {kind} targets ({e})")
        return []
    finally:
        conn.close()


def upsert_posts(target_key: str, kind: str, data: Dict[str, Any]) -> None:
    """Mirror one scrape's posts. `data` is a store document's "data" block:
    {channel: {"posts": [...]}, ...} — same shape store.py writes to JSON.
    """
    conn = _connect()
    if conn is None:
        return
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            for channel, block in (data or {}).items():
                if not isinstance(block, dict):
                    continue
                for post in block.get("posts", []):
                    extra = {k: v for k, v in post.items() if k not in _CORE_POST_FIELDS}
                    cur.execute(
                        """
                        INSERT INTO posts (
                            target_key, kind, channel, post_key, rank, post_url,
                            body, author, published_at, engagement, media, extra,
                            first_seen, last_seen, new_in_last_run, raw
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s::timestamptz, %s::timestamptz, %s, %s
                        )
                        ON CONFLICT (target_key, channel, post_key) DO UPDATE SET
                            rank = EXCLUDED.rank,
                            last_seen = EXCLUDED.last_seen,
                            new_in_last_run = EXCLUDED.new_in_last_run,
                            raw = EXCLUDED.raw
                        """,
                        (
                            target_key, kind, channel, post_key(channel, post),
                            post.get("rank"), post.get("post_url"),
                            post.get("text"), post.get("author"), post.get("published_at"),
                            json.dumps(post.get("engagement") or {}),
                            json.dumps(post.get("media") or []),
                            json.dumps(extra),
                            post.get("first_seen"), post.get("last_seen"),
                            post.get("new_in_last_run"),
                            json.dumps(post),
                        ),
                    )
                    if channel == "linkedin_jobs":
                        _upsert_linkedin_job(cur, target_key, post)
        conn.commit()
    except Exception as e:
        print(f"⚠️  [DB] Could not save posts for '{target_key}' ({e})")
    finally:
        conn.close()


def _upsert_linkedin_job(cur, target_key: str, post: Dict[str, Any]) -> None:
    """One job posting into the dedicated linkedin_jobs table, structured
    instead of buried in the generic posts table's JSONB `extra` column —
    lets job data be queried/filtered directly (by employment_type,
    location, etc.) rather than only read back whole.

    Called from upsert_posts() for every post on the linkedin_jobs channel,
    so it's mirrored alongside (not instead of) the generic posts row —
    every other read path (get_store, get_digest) keeps working unchanged.
    """
    engagement = post.get("engagement") or {}
    cur.execute(
        """
        INSERT INTO linkedin_jobs (
            target_key, job_key, title, company_name, location,
            employment_type, workplace_type, posted_date, applicants, views,
            salary, job_url, description, first_seen, last_seen,
            new_in_last_run, raw
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s::timestamptz, %s::timestamptz, %s, %s
        )
        ON CONFLICT (target_key, job_key) DO UPDATE SET
            applicants = EXCLUDED.applicants,
            views = EXCLUDED.views,
            last_seen = EXCLUDED.last_seen,
            new_in_last_run = EXCLUDED.new_in_last_run,
            raw = EXCLUDED.raw
        """,
        (
            target_key, post_key("linkedin_jobs", post),
            post.get("title"), post.get("author"), post.get("location"),
            post.get("employment_type"), post.get("workplace_type"),
            post.get("published_at"),
            engagement.get("applicants"), engagement.get("views"),
            json.dumps(post.get("salary")) if post.get("salary") else None,
            post.get("post_url"), post.get("text"),
            post.get("first_seen"), post.get("last_seen"),
            post.get("new_in_last_run"),
            json.dumps(post),
        ),
    )


def get_linkedin_jobs(target_key: str) -> list:
    """All job postings on file for one account, newest-posted first."""
    conn = _connect()
    if conn is None:
        return []
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT title, company_name, location, employment_type,
                       workplace_type, posted_date, applicants, views,
                       salary, job_url, description
                FROM linkedin_jobs
                WHERE target_key = %s
                ORDER BY posted_date DESC NULLS LAST
                """,
                (target_key,),
            )
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]
    except Exception as e:
        print(f"⚠️  [DB] Could not read linkedin_jobs for '{target_key}' ({e})")
        return []
    finally:
        conn.close()


def upsert_digest(target_key: str, kind: str, digest: Dict[str, Any]) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        _ensure_schema(conn)
        email = digest.get("email") or {}
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO digests (
                    target_key, kind, generated_at, llm, posts_considered,
                    priority, digest, updated_at
                )
                VALUES (%s, %s, %s::timestamptz, %s, %s, %s, %s, now())
                ON CONFLICT (target_key) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    generated_at = EXCLUDED.generated_at,
                    llm = EXCLUDED.llm,
                    posts_considered = EXCLUDED.posts_considered,
                    priority = EXCLUDED.priority,
                    digest = EXCLUDED.digest,
                    updated_at = now()
                """,
                (
                    target_key, kind, digest.get("generated_at"), digest.get("llm"),
                    digest.get("posts_considered"), email.get("priority"),
                    json.dumps(digest),
                ),
            )
        conn.commit()
    except Exception as e:
        print(f"⚠️  [DB] Could not save digest for '{target_key}' ({e})")
    finally:
        conn.close()


def insert_run_history(entry: Dict[str, Any]) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_history (
                    recorded_at, kind, target_key, display_name, limit_requested,
                    new_posts, total_posts, platforms_scraped, platforms_failed,
                    digest, success, error, duration_ms, entry
                )
                VALUES (%s::timestamptz, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    entry.get("recorded_at"), entry.get("kind"), entry.get("key"),
                    entry.get("display_name"), entry.get("limit"),
                    entry.get("new_posts"), entry.get("total_posts"),
                    json.dumps(entry.get("platforms_scraped") or []),
                    json.dumps(entry.get("platforms_failed") or []),
                    json.dumps(entry["digest"]) if entry.get("digest") is not None else None,
                    entry.get("success"), entry.get("error"), entry.get("duration_ms"),
                    json.dumps(entry),
                ),
            )
        conn.commit()
    except Exception as e:
        print(f"⚠️  [DB] Could not save run history entry ({e})")
    finally:
        conn.close()


# ── Reads ───────────────────────────────────────────────────────────
# Used by main.py's GET /api/accounts(/<key>) and /api/people(/<key>) as
# a fallback when the local output/ JSON file doesn't exist — e.g. a
# freshly deployed instance that's never scraped anything itself, but
# shares DATABASE_URL with one that has.

def get_store(target_key: str):
    """Posts for one target, reconstructed from Postgres into the same
    {"data": {channel: {"posts": [...], "count": N}}} shape the JSON
    store file uses — or None if nothing's there (DB unreachable, or
    genuinely never scraped anywhere).
    """
    conn = _connect()
    if conn is None:
        return None
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT channel, raw FROM posts WHERE target_key = %s ORDER BY channel, rank",
                (target_key,),
            )
            rows = cur.fetchall()
        if not rows:
            return None
        data: Dict[str, Any] = {}
        for channel, raw in rows:
            data.setdefault(channel, {"posts": []})["posts"].append(raw)
        for block in data.values():
            block["count"] = len(block["posts"])
        return {
            "data": data,
            "metadata": {
                "total_posts": len(rows),
                "platforms_scraped": sorted(data.keys()),
                "platforms_failed": [],
            },
        }
    except Exception as e:
        print(f"⚠️  [DB] Could not read posts for '{target_key}' ({e})")
        return None
    finally:
        conn.close()


def get_digest(target_key: str):
    conn = _connect()
    if conn is None:
        return None
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT digest FROM digests WHERE target_key = %s", (target_key,))
            row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        print(f"⚠️  [DB] Could not read digest for '{target_key}' ({e})")
        return None
    finally:
        conn.close()


def get_summary(target_key: str):
    """Cheap total_posts/last_run for the list endpoint — avoids pulling
    every post's full JSON just to count them.
    """
    conn = _connect()
    if conn is None:
        return None
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*), max(last_seen) FROM posts WHERE target_key = %s",
                (target_key,),
            )
            total, last_seen = cur.fetchone()
        if not total:
            return None
        return {
            "total_posts": total,
            "last_run": last_seen.isoformat() if last_seen else None,
        }
    except Exception as e:
        print(f"⚠️  [DB] Could not read summary for '{target_key}' ({e})")
        return None
    finally:
        conn.close()


def has_digest(target_key: str) -> bool:
    conn = _connect()
    if conn is None:
        return False
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM digests WHERE target_key = %s", (target_key,))
            return cur.fetchone() is not None
    except Exception as e:
        print(f"⚠️  [DB] Could not check digest for '{target_key}' ({e})")
        return False
    finally:
        conn.close()
