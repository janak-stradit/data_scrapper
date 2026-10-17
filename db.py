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
import threading
from typing import Any, Dict

from config import DATABASE_URL
from store import post_key

_SCHEMA_READY = False
# Serializes _ensure_schema() across the HTTP server's request threads.
_SCHEMA_LOCK = threading.Lock()
# Arbitrary constant key for pg_advisory_lock — same serialization across
# separate processes (the server plus a CLI run) hitting one database.
_SCHEMA_ADVISORY_KEY = 727_340_001

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
    # ── Migrations for databases created before the constraints/types
    # above existed — `CREATE TABLE IF NOT EXISTS` is a no-op once a table
    # already exists, so an older deployment's schema never gets upgraded
    # without an explicit ALTER. Each of these is safe to run on every
    # process start: the ADD CONSTRAINT ones catch "already exists" and
    # no-op, and the type-fix ones check the live column type first.
    # (If a live DB somehow already has duplicate targets.key /
    # posts(target_key,channel,post_key) rows from having run without
    # these constraints, ADD CONSTRAINT will fail with a real
    # unique_violation — that needs a manual dedup, not something this
    # lazy-migration approach can safely resolve on its own.)
    """
    DO $$
    BEGIN
        ALTER TABLE targets ADD CONSTRAINT targets_key_unique UNIQUE (key);
    EXCEPTION
        -- A named UNIQUE constraint's backing index shares its name, so
        -- Postgres raises duplicate_table (42P07) for "already exists"
        -- here, not duplicate_object (42710) — confirmed live: the first
        -- run of this statement succeeds and adds the constraint, every
        -- restart after that was hitting this exact case uncaught.
        WHEN duplicate_object OR duplicate_table THEN NULL;
    END $$;
    """,
    """
    DO $$
    BEGIN
        ALTER TABLE posts ADD CONSTRAINT posts_target_channel_postkey_unique
            UNIQUE (target_key, channel, post_key);
    EXCEPTION
        WHEN duplicate_object OR duplicate_table THEN NULL;
    END $$;
    """,
    """
    DO $$
    BEGIN
        ALTER TABLE linkedin_jobs ADD CONSTRAINT linkedin_jobs_target_jobkey_unique
            UNIQUE (target_key, job_key);
    EXCEPTION
        WHEN duplicate_object OR duplicate_table THEN NULL;
    END $$;
    """,
    """
    DO $$
    BEGIN
        IF (SELECT data_type FROM information_schema.columns
            WHERE table_name = 'run_history' AND column_name = 'platforms_scraped') = 'ARRAY' THEN
            ALTER TABLE run_history ALTER COLUMN platforms_scraped
                TYPE JSONB USING to_jsonb(platforms_scraped);
        END IF;
    END $$;
    """,
    """
    DO $$
    BEGIN
        IF (SELECT data_type FROM information_schema.columns
            WHERE table_name = 'run_history' AND column_name = 'platforms_failed') = 'ARRAY' THEN
            ALTER TABLE run_history ALTER COLUMN platforms_failed
                TYPE JSONB USING to_jsonb(platforms_failed);
        END IF;
    END $$;
    """,
] + [
    # Same drift pattern for `id` on every BIGSERIAL-PRIMARY-KEY table: a
    # live column created before `BIGSERIAL` was in the code has no
    # sequence-backed default, so an INSERT that (correctly) omits `id`
    # hits "null value in column id violates not-null constraint" — seen
    # live for run_history once the platforms_* fix above got far enough
    # for this to be the next thing INSERT hit. Checked proactively for
    # posts/linkedin_jobs too rather than waiting to hit each one in turn.
    f"""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_attrdef ad
            JOIN pg_attribute a ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum
            WHERE ad.adrelid = '{table}'::regclass AND a.attname = 'id'
        ) THEN
            CREATE SEQUENCE IF NOT EXISTS {table}_id_seq OWNED BY {table}.id;
            PERFORM setval('{table}_id_seq', COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false);
            ALTER TABLE {table} ALTER COLUMN id SET DEFAULT nextval('{table}_id_seq');
        END IF;
    END $$;
    """
    for table in ("posts", "linkedin_jobs", "run_history")
] + [
    # Sequence drift: rows written with explicit ids (imports, or another
    # app sharing this DB) don't advance {table}_id_seq, so a later INSERT
    # that omits id gets one that's already taken — seen live as
    # "duplicate key ... posts_pkey (id)=(54251)" with the sequence at
    # 54251 and MAX(id) at 97254. Moves the sequence past MAX(id); never
    # moves it backwards.
    f"""
    DO $$
    DECLARE m BIGINT;
    BEGIN
        IF to_regclass('{table}_id_seq') IS NOT NULL THEN
            SELECT MAX(id) INTO m FROM {table};
            IF m IS NOT NULL THEN
                PERFORM setval('{table}_id_seq',
                               GREATEST(m, (SELECT last_value FROM {table}_id_seq)));
            END IF;
        END IF;
    END $$;
    """
    for table in ("posts", "linkedin_jobs", "run_history")
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
    """Runs each CREATE TABLE / migration statement in its OWN transaction
    (commit or rollback per-statement), not one big all-or-nothing batch.
    One statement failing — e.g. an ADD CONSTRAINT hitting genuine
    duplicate data on an old, unmigrated table — used to roll back every
    other statement in the same call along with it, silently discarding
    unrelated fixes that would otherwise have gone through fine, and
    leaving only a downstream symptom (the next INSERT's error) to debug
    from instead of the real statement that failed. Only sets
    _SCHEMA_READY once every statement has actually succeeded, so a
    partial failure keeps retrying on the next call instead of being
    forgotten — already-applied statements just no-op again quickly.

    Serialized (thread lock + Postgres advisory lock) because `CREATE
    INDEX IF NOT EXISTS` isn't race-safe: concurrent first requests on the
    threaded server each saw the index missing, both tried to create it,
    and the loser failed with a duplicate pg_class_relname_nsp_index key.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:  # another thread finished while we waited
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_ADVISORY_KEY,))
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"⚠️  [DB] Could not take schema lock, continuing ({e})")
        all_ok = True
        try:
            for statement in _SCHEMA_STATEMENTS:
                try:
                    with conn.cursor() as cur:
                        cur.execute(statement)
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    print(f"⚠️  [DB] Schema statement failed, continuing ({e})")
                    all_ok = False
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_ADVISORY_KEY,))
                conn.commit()
            except Exception:
                conn.rollback()
        if all_ok:
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


def get_list_info(target_keys: list) -> dict:
    """Batched get_summary() + has_digest() for the list endpoints: one
    connection and two queries for every key at once, instead of a fresh
    Neon connection per key — with ~500 people that per-key pattern made
    GET /api/people take ~34s.

    Returns {"summaries": {key: {"total_posts", "last_run"}}, "digests": set(keys)}.
    """
    out = {"summaries": {}, "digests": set()}
    keys = list(target_keys or [])
    if not keys:
        return out
    conn = _connect()
    if conn is None:
        return out
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT target_key, count(*), max(last_seen) FROM posts "
                "WHERE target_key = ANY(%s) GROUP BY target_key",
                (keys,),
            )
            for key, total, last_seen in cur.fetchall():
                if total:
                    out["summaries"][key] = {
                        "total_posts": total,
                        "last_run": last_seen.isoformat() if last_seen else None,
                    }
            cur.execute("SELECT target_key FROM digests WHERE target_key = ANY(%s)", (keys,))
            out["digests"] = {row[0] for row in cur.fetchall()}
    except Exception as e:
        print(f"⚠️  [DB] Could not read list info ({e})")
    finally:
        conn.close()
    return out


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


def list_personas() -> list:
    """Every person in the `personas` table — the sales_agent-ai CRM's
    contact list, shared via this same Neon database but otherwise
    unrelated to this app's own `targets` (kind=person) registry (163
    CRM contacts vs. a handful of people this scraper is actually asked
    to run LinkedIn/Reddit/SEC/news collection against).

    Read-only and best-effort like every other function here: used only
    so `main.py list --person` can surface CRM contacts that aren't yet
    registered as scrape targets, for discovery. Never wired into
    resolve()/scrape/digest — a persona isn't a scrape target until it's
    added to people_targets.py (or saved via /api/save-target).
    """
    conn = _connect()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.key, p.display_name, p.title, a.display_name AS company,
                       p.linkedin_url, p.twitter_handle, p.reddit_query,
                       p.sec_cik, p.news_query, p.patents_query,
                       p.osint_feed_manifest
                FROM personas p
                LEFT JOIN accounts a ON a.id = p.account_id
                WHERE p.key IS NOT NULL
                ORDER BY p.key
                """
            )
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]
    except Exception as e:
        print(f"⚠️  [DB] Could not list personas ({e})")
        return []
    finally:
        conn.close()


def list_accounts() -> list:
    """Every company in the `accounts` table — the sales_agent-ai CRM's
    firmographic registry, shared via this same Neon database but
    otherwise unrelated to this app's own `targets` (kind=company)
    registry (see MERGE_PLAN.md §7.3: accounts is "do we track this
    company at all", targets is scrape config — deliberately kept
    separate, not merged).

    Read-only and best-effort like every other function here: used only
    so `main.py list`/the frontend sidebar can surface CRM accounts that
    aren't yet registered as scrape targets, for discovery. Never wired
    into resolve()/scrape/digest — an account isn't a scrape target
    until it's added to targets.py (or saved via /api/save-target).
    """
    conn = _connect()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT key, display_name, stock_symbol, domain,
                       linkedin_url, twitter_handle, reddit_query,
                       sec_cik, news_query, blog_url, rss_url,
                       employee_count_range, headquarters_location,
                       osint_feed_manifest
                FROM accounts
                WHERE key IS NOT NULL
                ORDER BY key
                """
            )
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]
    except Exception as e:
        print(f"⚠️  [DB] Could not list accounts ({e})")
        return []
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
