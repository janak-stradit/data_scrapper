"""Account intelligence pipeline — single entry point.

Manages the whole process: scrape the configured accounts, store results
without duplicates, generate the sales/social digests, and serve the frontend.

    python main.py scrape bny --limit 20
    python main.py digest bny
    python main.py run --all              # scrape, then digest
    python main.py status
    python main.py serve

Every subcommand accepts --help.
"""
import argparse
import asyncio
import json
import os
import sys

from engine import scrape_and_store
from paths import DIGEST_DIR, OUTPUT_DIR, PROJECT_ROOT, store_path as _resolved_store_path
from targets import COMPANY_TARGETS, resolve as resolve_company
from people_targets import PEOPLE_TARGETS, resolve as resolve_person

DEFAULT_PORT = 8001

# Which target field means a channel is configured — used by /api/accounts
# and /api/people to report "channels" without re-deriving engine.py's
# want()/scrape_all() wiring.
CHANNEL_FIELDS = {
    "linkedin": "linkedin_url",
    "twitter": "twitter_handle",
    "reddit": "reddit_query",
    "sec": "sec_cik",
    "news": "news_query",
    "blog": "blog_url",
    "newsroom": "newsroom_url",
    "patents": "patents_query",
    "rss": "rss_url",
    "youtube": "youtube_channel_id",
    "sec_mentions": "sec_mentions_query",
    "regulatory": "regulatory_query",
    "linkedin_jobs": "linkedin_jobs_query",
    "cxo_news": "cxo_news_query",
    "bloomberg": "news_query",
}


def _configured_channels(target: dict, kind: str = "company") -> list:
    channels = [ch for ch, field in CHANNEL_FIELDS.items() if target.get(field)]
    if kind == "person":
        # Free public-records channels derived from name/title/company
        # rather than a dedicated target field.
        import person_identity
        channels += person_identity.configured_channels(target)
    return channels


def _table(args) -> dict:
    return PEOPLE_TARGETS if getattr(args, "person", False) else COMPANY_TARGETS


def _resolve(args, key: str) -> dict:
    """Look up a target by key, same as resolve_person()/resolve_company()
    but falling back to Postgres for a target that only got there via an
    ad-hoc /api/run (mirrored by db.upsert_target, never saved into
    targets.py/people_targets.py/custom_targets.json) — otherwise every
    subcommand but list/status would 500 on exactly the keys those two
    are happy to show.
    """
    kind = "person" if getattr(args, "person", False) else "company"
    resolve_fn = resolve_person if kind == "person" else resolve_company
    try:
        return resolve_fn(key)
    except KeyError:
        import db
        for row in db.list_targets(kind):
            if row["key"] == key:
                return {"key": key, **row["config"]}
        raise


def _companies(args) -> list:
    """Which targets (companies, or people with --person) a subcommand should act on."""
    table = _table(args)
    if getattr(args, "all", False):
        return sorted(table)
    if args.company:
        return [args.company]
    return sorted(table)


def _store_path(args, company: str) -> str:
    return _resolved_store_path(_resolve(args, company)["key"])


def _split(value: str) -> list:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def _banner(title: str) -> None:
    print("\n" + "═" * 70)
    print(f"  {title}")
    print("═" * 70)


# ── scrape ───────────────────────────────────────────────────────

async def cmd_scrape(args) -> int:
    failures = 0
    kind = "person" if args.person else "company"
    for company in _companies(args):
        target = _resolve(args, company)
        _banner(f"SCRAPE · {target['display_name']}")
        try:
            await scrape_and_store(
                limit=args.limit,
                only=_split(args.only),
                include_newsroom=not args.no_newsroom,
                out_path=_store_path(args, company),
                reset_channels=_split(args.reset_channel),
                use_store=not args.no_store,
                kind=kind,
                target=target,
            )
        except Exception as e:
            print(f"❌  {target['display_name']}: {type(e).__name__}: {e}")
            failures += 1
    return 1 if failures else 0


# ── digest ───────────────────────────────────────────────────────

def cmd_digest(args) -> int:
    # Imported lazily so `scrape` still works if the LLM config is broken.
    from digest import LLMError, run as run_digest

    kind = "person" if args.person else "company"
    failures = 0
    for company in _companies(args):
        target = _resolve(args, company)
        _banner(f"DIGEST · {target['display_name']}")
        try:
            run_digest(
                new_only=not args.all_posts,
                since_days=args.since_days,
                cap=args.max_posts,
                out_dir=args.out_dir,
                store_path_override=_store_path(args, company),
                kind=kind,
                target=target,
            )
        except (FileNotFoundError, RuntimeError, LLMError) as e:
            print(f"❌  {target['display_name']}: {e}")
            failures += 1
    return 1 if failures else 0


# ── news-brief ───────────────────────────────────────────────────

def cmd_news_brief(args) -> int:
    """Today + yesterday's news/bloomberg coverage — no LLM, instant.
    Distinct from `digest`, which runs the full LLM-synthesized Sales Email.
    """
    from digest.news_brief import build as build_news_brief
    from mailer import MailerError, send_email

    kind = "person" if args.person else "company"
    failures = 0
    for company in _companies(args):
        target = _resolve(args, company)
        _banner(f"NEWS BRIEF · {target['display_name']}")
        try:
            brief = build_news_brief(kind=kind, target=target)
        except FileNotFoundError as e:
            print(f"❌  {target['display_name']}: {e}")
            failures += 1
            continue

        print(brief["body"])
        if args.send:
            try:
                send_email(args.send, brief["subject"], brief["body"], html=brief["html"])
                print(f"\n📧  Sent to {args.send}")
            except MailerError as e:
                print(f"\n❌  Send failed: {e}")
                failures += 1
    return 1 if failures else 0


# ── run (scrape then digest) ─────────────────────────────────────

async def cmd_run(args) -> int:
    scrape_code = await cmd_scrape(args)
    digest_code = cmd_digest(args)
    return scrape_code or digest_code


# ── status ───────────────────────────────────────────────────────

def cmd_status(args) -> int:
    from digest import describe_config

    _banner("STATUS")
    print(f"  LLM: {describe_config()}\n")

    header = f"  {'ACCOUNT':<24}{'POSTS':>6}{'NEW':>5}{'CH':>4}   {'LAST SCRAPE':<18}DIGEST"
    print(header)
    print("  " + "─" * (len(header) - 2))

    kind = "person" if getattr(args, "person", False) else "company"
    table = _merged_table(kind, _table(args))
    for company in sorted(table):
        try:
            target = _resolve(args, company)
        except KeyError:
            target = {"key": company, **table[company]}
        path = _resolved_store_path(company)
        if not os.path.exists(path):
            print(f"  {target['display_name'][:23]:<24}{'—':>6}{'—':>5}{'—':>4}   not scraped yet")
            continue

        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        meta = doc.get("metadata", {})
        last = (doc.get("store", {}).get("last_run") or "")[:16].replace("T", " ")

        digest_path = os.path.join(args.out_dir, f"{target['key']}_digest.json")
        digest_state = "none"
        if os.path.exists(digest_path):
            with open(digest_path, encoding="utf-8") as fh:
                digest_state = json.load(fh).get("generated_at", "")[:16].replace("T", " ")

        print(
            f"  {target['display_name'][:23]:<24}"
            f"{meta.get('total_posts', 0):>6}"
            f"{meta.get('new_last_run', 0):>5}"
            f"{len(doc.get('data', {})):>4}   "
            f"{last:<18}{digest_state}"
        )

    print("\n  Channels per account:")
    for company in sorted(table):
        path = _resolved_store_path(company)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        counts = "  ".join(
            f"{ch}:{blk.get('count', 0)}"
            for ch, blk in sorted(doc.get("data", {}).items())
            if isinstance(blk, dict)
        )
        print(f"    {company:<16}{counts}")
    print()
    return 0


# ── db-backfill ─────────────────────────────────────────────────

def cmd_db_backfill(args) -> int:
    """Sync every existing JSON output file into Postgres.

    Normal scrape/digest/save-target runs already mirror themselves to the
    database as they happen (see db.py) — this is only needed once, the
    first time DATABASE_URL is set, to backfill data collected before the
    database existed. Safe to re-run any time; every write is an upsert.
    """
    from config import DATABASE_URL
    from paths import DIGEST_DIR, store_path, digest_path
    import db

    if not DATABASE_URL:
        print("❌  DATABASE_URL is not set in .env — nothing to backfill to.")
        return 1

    _banner("DB BACKFILL")

    target_count = post_count = digest_count = 0
    for kind, table, resolve_fn in (
        ("company", COMPANY_TARGETS, resolve_company),
        ("person", PEOPLE_TARGETS, resolve_person),
    ):
        for key in sorted(table):
            target = resolve_fn(key)
            db.upsert_target(kind, target)
            target_count += 1

            store_file = store_path(key)
            posts_here = 0
            if os.path.exists(store_file):
                with open(store_file, encoding="utf-8") as fh:
                    doc = json.load(fh)
                db.upsert_posts(key, kind, doc.get("data", {}))
                posts_here = doc.get("metadata", {}).get("total_posts", 0)
                post_count += posts_here

            has_digest = False
            digest_file = digest_path(key)
            if os.path.exists(digest_file):
                with open(digest_file, encoding="utf-8") as fh:
                    digest = json.load(fh)
                db.upsert_digest(key, kind, digest)
                digest_count += 1
                has_digest = True

            print(f"  {target['display_name']:<40}{posts_here:>5} posts   digest: {'yes' if has_digest else 'no'}")

    history_file = os.path.join(OUTPUT_DIR, "run_history.json")
    history_count = 0
    if os.path.exists(history_file):
        with open(history_file, encoding="utf-8") as fh:
            entries = json.load(fh)
        for entry in entries:
            db.insert_run_history(entry)
            history_count += 1

    print(
        f"\n  Synced {target_count} targets, {post_count} posts, "
        f"{digest_count} digests, {history_count} run-history rows.\n"
    )
    return 0


# ── serve ────────────────────────────────────────────────────────

def _upsert_manifest_entry(kind: str, target: dict) -> None:
    """Add or update one target in frontend/manifest.json.

    Called after targets.save()/people_targets.save() so a target saved
    from the Run Pipeline page shows up in the sidebar's Accounts/People
    list immediately, not just in the CLI's target registry.
    """
    manifest_path = os.path.join(PROJECT_ROOT, "frontend", "manifest.json")
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        manifest = {}
    manifest.setdefault("accounts", [])
    manifest.setdefault("people", [])

    section = "people" if kind == "person" else "accounts"
    key = target["key"]
    entry = {
        "key": key,
        "name": target.get("display_name", key),
        "file": f"../output/stores/{key}_output.json",
        "digest": f"../output/digests/{key}_digest.json",
    }
    if kind != "person":
        entry["ticker"] = target.get("ticker") or "—"

    manifest[section] = [e for e in manifest[section] if e.get("key") != key] + [entry]

    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)


def cmd_serve(args) -> int:
    """Serve the frontend on localhost only.

    Bound to 127.0.0.1 deliberately: this directory holds .env with API
    tokens, and the default bind would expose it to the whole network.

    Threaded so a long-running /api/run POST (a full scrape can take
    minutes) doesn't block the page from loading stores/history meanwhile.
    """
    import http.server
    import socketserver
    import time

    class Handler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            # Make the browser revalidate every file (a cheap 304 when
            # unchanged) — without this Chrome heuristically caches
            # frontend/index.html off its Last-Modified and keeps running a
            # stale copy after an edit (seen live: a new sidebar button
            # rendered, but its click handler didn't exist yet).
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def _send_json(self, status: int, payload: dict):
            body = json.dumps(payload).encode("utf-8")
            # Store payloads run to several MB of very repetitive JSON —
            # gzip typically shrinks them ~8-10x. Skipped for tiny bodies,
            # where the header overhead isn't worth it.
            gzipped = len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "")
            if gzipped:
                import gzip
                body = gzip.compress(body, compresslevel=5)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if gzipped:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                # Browser gave up on the request (reload, navigated away,
                # superseded fetch) — nothing to send it to, not a server error.
                pass

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def _read_json_file(self, path: str):
            if not os.path.exists(path):
                return None
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                return None

        # path -> (mtime_ns, size, summary). Store files run up to ~5MB each
        # and the list endpoints only need two fields from each, so parse a
        # file once and re-read it only when it changes on disk.
        _store_summary_cache: dict = {}

        def _store_summary(self, path: str):
            """{"total_posts", "last_run"} from a store JSON file, or None if missing/unreadable."""
            try:
                st = os.stat(path)
            except OSError:
                return None
            cached = self._store_summary_cache.get(path)
            if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                return cached[2]
            store = self._read_json_file(path)
            if store is None:
                return None
            summary = {
                "total_posts": (store.get("metadata") or {}).get("total_posts"),
                "last_run": (store.get("store") or {}).get("last_run"),
            }
            self._store_summary_cache[path] = (st.st_mtime_ns, st.st_size, summary)
            return summary

        # Everything else on disk (.env, *.py source, custom_targets.json,
        # .git/, ...) is deliberately NOT reachable over HTTP — this list is
        # the complete set of what's safe to serve publicly.
        _PUBLIC_PREFIXES = ("/frontend/", "/output/")
        _PUBLIC_EXACT = {"/API.md"}

        def do_GET(self):
            path = self.path.split("?", 1)[0]

            if path.startswith("/api/"):
                if path == "/api/accounts":
                    self._handle_list("company")
                elif path.startswith("/api/accounts/"):
                    self._handle_detail("company", path[len("/api/accounts/"):])
                elif path == "/api/people":
                    self._handle_list("person")
                elif path.startswith("/api/people/"):
                    self._handle_detail("person", path[len("/api/people/"):])
                elif path == "/api/run-history":
                    self._handle_run_history()
                elif path == "/api/crm-accounts":
                    self._handle_crm_list("company")
                elif path == "/api/crm-people":
                    self._handle_crm_list("person")
                elif path == "/api/scheduler":
                    import scheduler
                    self._send_json(200, scheduler.status())
                else:
                    self.send_error(404)
                return

            if path.startswith(self._PUBLIC_PREFIXES) or path in self._PUBLIC_EXACT:
                super().do_GET()
                return

            self.send_error(404)

        def _handle_list(self, kind: str):
            from targets import COMPANY_TARGETS
            from people_targets import PEOPLE_TARGETS
            from paths import store_path, digest_path
            import db

            table = dict(PEOPLE_TARGETS if kind == "person" else COMPANY_TARGETS)
            # Targets that exist in Postgres but were never registered here
            # (e.g. an ad-hoc /api/run target, scraped but never explicitly
            # saved via /api/save-target) — DB-unreachable is a silent no-op.
            for row in db.list_targets(kind):
                table.setdefault(row["key"], row["config"])

            local = {key: self._store_summary(store_path(key)) for key in table}
            # Only keys with something missing locally need Postgres — and
            # they're all looked up in one batched call (see db.get_list_info).
            need_db = [k for k in table
                       if local[k] is None or not os.path.exists(digest_path(k))]
            db_info = db.get_list_info(need_db)

            items = []
            for key in sorted(table):
                cfg = table[key]
                summary = local[key]
                has_store = summary is not None
                has_digest = os.path.exists(digest_path(key))
                total_posts = summary["total_posts"] if summary else None
                last_run = summary["last_run"] if summary else None

                # No local JSON file (e.g. a freshly deployed instance that
                # hasn't scraped this target itself) — fall back to Postgres,
                # which may have it from wherever it was originally scraped.
                if summary is None and key in db_info["summaries"]:
                    has_store = True
                    total_posts = db_info["summaries"][key]["total_posts"]
                    last_run = db_info["summaries"][key]["last_run"]
                if not has_digest and key in db_info["digests"]:
                    has_digest = True

                items.append({
                    "key": key,
                    "display_name": cfg.get("display_name", key),
                    "ticker": cfg.get("ticker") if kind == "company" else None,
                    "channels": _configured_channels(cfg, kind),
                    "has_store": has_store,
                    "has_digest": has_digest,
                    "total_posts": total_posts,
                    "last_run": last_run,
                    # Full target config, so a list consumer (the frontend's
                    # People page re-run button) can POST it straight back
                    # to /api/run without fetching the whole store first.
                    "target": {"key": key, **cfg},
                })
            list_key = "people" if kind == "person" else "accounts"
            self._send_json(200, {list_key: items})

        def _handle_crm_list(self, kind: str):
            """CRM accounts/personas that are NOT yet registered as scrape
            targets — the frontend sidebar's "CRM (not yet tracked)"
            section, mirroring `main.py list`'s [db only] discovery list.
            See db.list_accounts()/list_personas() for why these live in
            a genuinely separate table from this app's own `targets`
            registry rather than being merged into it.
            """
            import urllib.parse
            from targets import COMPANY_TARGETS
            from people_targets import PEOPLE_TARGETS
            import db

            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            q = (query.get("q", [""])[0] or "").strip().lower()
            account_filter = (query.get("account", [""])[0] or "").strip()
            title_filter = (query.get("title", [""])[0] or "").strip().lower()
            title_exact = (query.get("title_exact", [""])[0] or "").strip()

            def _int_param(name, default, lo, hi):
                try:
                    return min(max(int(query.get(name, [str(default)])[0]), lo), hi)
                except (TypeError, ValueError):
                    return default

            page = _int_param("page", 1, 1, 10_000)
            limit = _int_param("limit", 20, 1, 100)

            tracked = set(dict(PEOPLE_TARGETS if kind == "person" else COMPANY_TARGETS).keys())
            for row in db.list_targets(kind):
                tracked.add(row["key"])

            rows = db.list_personas() if kind == "person" else db.list_accounts()
            items = [r for r in rows if r.get("key") not in tracked]

            # Distinct companies among the currently-untracked personas —
            # feeds the frontend's Account filter dropdown. Computed before
            # q/account/title narrow `items` further, so the dropdown's
            # options don't shrink out from under the user as they filter.
            companies = sorted({r["company"] for r in items if r.get("company")}) if kind == "person" else None
            # Same idea for the Title filter dropdown.
            titles = sorted({r["title"].strip() for r in items if (r.get("title") or "").strip()},
                            key=str.lower) if kind == "person" else None

            if q:
                def _haystack(r):
                    return f"{r.get('display_name', '')} {r.get('company', '') or r.get('domain', '') or ''}".lower()
                items = [r for r in items if q in _haystack(r)]

            if kind == "person" and account_filter:
                items = [r for r in items if (r.get("company") or "") == account_filter]
            if kind == "person" and title_filter:
                items = [r for r in items if title_filter in (r.get("title") or "").lower()]
            if kind == "person" and title_exact:
                items = [r for r in items if (r.get("title") or "").strip() == title_exact]

            total = len(items)
            total_pages = max(1, -(-total // limit))  # ceil division
            page = min(page, total_pages)
            start = (page - 1) * limit

            list_key = "people" if kind == "person" else "accounts"
            response = {
                list_key: items[start:start + limit],
                "page": page,
                "limit": limit,
                "total": total,
                "total_pages": total_pages,
            }
            if companies is not None:
                response["companies"] = companies
            if titles is not None:
                response["titles"] = titles
            self._send_json(200, response)

        def _handle_detail(self, kind: str, raw_key: str):
            import urllib.parse
            from targets import resolve as resolve_company
            from people_targets import resolve as resolve_person
            from paths import store_path, digest_path
            import db

            key = urllib.parse.unquote(raw_key).strip().strip("/")
            if not key:
                self._send_json(404, {"ok": False, "error": "Missing key in URL"})
                return

            resolve_fn = resolve_person if kind == "person" else resolve_company
            try:
                target = resolve_fn(key)
            except KeyError as e:
                # Not registered in targets.py/people_targets.py/
                # custom_targets.json — check Postgres before giving up
                # (an ad-hoc /api/run target that was scraped but never
                # explicitly saved still has a row there).
                target = next(
                    (dict(row["config"], key=row["key"]) for row in db.list_targets(kind) if row["key"] == key),
                    None,
                )
                if target is None:
                    # KeyError.__str__ reprs its message (adds quotes) — unwrap it.
                    self._send_json(404, {"ok": False, "error": e.args[0] if e.args else str(e)})
                    return

            # No local JSON file (e.g. a freshly deployed instance that
            # hasn't scraped this target itself) — fall back to Postgres.
            # Reconstructed from `posts`/`digests` rows, so it won't be
            # byte-identical to the JSON file (no query/store metadata
            # block), but carries the same post/digest content.
            store = self._read_json_file(store_path(target["key"])) or db.get_store(target["key"])
            digest = self._read_json_file(digest_path(target["key"])) or db.get_digest(target["key"])

            # ?view=summary — every channel block without its posts (counts,
            # success/error, new_last_run stay), so opening an account
            # doesn't download every post up front. ?channel=<name> — only
            # that channel's block, posts included, for when it's opened.
            # Neither param = the full store, as before.
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            view = (query.get("view", [""])[0] or "").strip()
            channel = (query.get("channel", [""])[0] or "").strip()
            if store and (view == "summary" or channel):
                data = store.get("data") or {}
                if channel:
                    data = {channel: data[channel]} if channel in data else {}
                else:
                    data = {
                        ch: {k: v for k, v in blk.items() if k != "posts"} if isinstance(blk, dict) else blk
                        for ch, blk in data.items()
                    }
                store = dict(store, data=data)

            self._send_json(200, {
                "target": target,
                "store": store,
                "digest": digest,
            })

        def _handle_run_history(self):
            """Paginated run history for the frontend's dedicated History
            page — GET /output/run_history.json (used by the polling
            helper and the older embedded tables) still serves the full
            capped-at-200 file; this endpoint slices it server-side so
            the page only ever transfers one page's worth of rows.
            """
            import urllib.parse
            import history

            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

            def _int_param(name, default, lo, hi):
                try:
                    return min(max(int(query.get(name, [str(default)])[0]), lo), hi)
                except (TypeError, ValueError):
                    return default

            page = _int_param("page", 1, 1, 10_000)
            limit = _int_param("limit", 20, 1, 100)

            entries = history.load_all()
            total = len(entries)
            total_pages = max(1, -(-total // limit))  # ceil division
            page = min(page, total_pages)
            start = (page - 1) * limit

            # Computed across the FULL history (not just this page) — cheap,
            # it's already loaded in memory — so the page header can show a
            # meaningful "N runs, X% success, $Y total" summary without the
            # frontend having to fetch every page to add it up itself.
            summary = {
                "total_runs": total,
                "success_count": sum(1 for e in entries if e.get("status") == "success" or (e.get("success") and e.get("status") != "running")),
                "failed_count": sum(1 for e in entries if e.get("status") == "failed" or (e.get("success") is False and e.get("status") != "running")),
                "running_count": sum(1 for e in entries if e.get("status") == "running"),
                "total_cost_usd": sum(e.get("cost_usd") or 0 for e in entries),
                "total_input_tokens": sum((e.get("digest") or {}).get("input_tokens") or 0 for e in entries),
                "total_output_tokens": sum((e.get("digest") or {}).get("output_tokens") or 0 for e in entries),
            }

            self._send_json(200, {
                "entries": entries[start:start + limit],
                "summary": summary,
                "page": page,
                "limit": limit,
                "total": total,
                "total_pages": total_pages,
            })

        def do_POST(self):
            if self.path == "/api/scheduler/run":
                self._handle_scheduler_run()
            elif self.path == "/api/send-email":
                self._handle_send_email()
            elif self.path == "/api/news-brief":
                self._handle_news_brief()
            elif self.path == "/api/run":
                self._handle_run()
            elif self.path == "/api/digest":
                self._handle_digest()
            elif self.path == "/api/save-target":
                self._handle_save_target()
            else:
                self.send_error(404)

        def _handle_send_email(self):
            from mailer import send_email, MailerError

            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "Malformed JSON body"})
                return

            try:
                send_email(
                    payload.get("to", ""), payload.get("subject", ""), payload.get("body", ""),
                    html=payload.get("html") or None,
                )
                self._send_json(200, {"ok": True})
            except MailerError as e:
                self._send_json(200, {"ok": False, "error": str(e)})
            except Exception as e:
                self._send_json(200, {"ok": False, "error": f"Unexpected error: {e}"})

        def _handle_news_brief(self):
            """Today + yesterday's news/bloomberg coverage — no LLM, so
            unlike _handle_digest this answers synchronously instead of
            kicking off a background thread; it only reads the already-
            scraped local store file.
            """
            from digest.news_brief import build as build_news_brief

            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "Malformed JSON body"})
                return

            kind = "person" if payload.get("kind") == "person" else "company"
            target = payload.get("target")
            if not isinstance(target, dict) or not (target.get("key") or "").strip():
                self._send_json(400, {"ok": False, "error": "target must be a JSON object with a non-empty \"key\""})
                return
            target = dict(target)
            target["key"] = target["key"].strip()
            target.setdefault("display_name", target["key"])

            try:
                brief = build_news_brief(kind=kind, target=target)
                self._send_json(200, {"ok": True, "brief": brief})
            except FileNotFoundError as e:
                self._send_json(200, {"ok": False, "error": str(e)})
            except Exception as e:
                self._send_json(200, {"ok": False, "error": f"Unexpected error: {e}"})

        def _handle_scheduler_run(self):
            """POST {"job": "news"|"people"} — start a scheduled job now,
            in the background; its summary email goes out when it ends."""
            import scheduler

            try:
                job = (self._read_json_body().get("job") or "").strip()
            except (json.JSONDecodeError, AttributeError):
                self._send_json(400, {"ok": False, "error": "Malformed JSON body"})
                return
            if job not in scheduler.status()["jobs"]:
                self._send_json(400, {"ok": False, "error": "job must be \"news\", \"companies\" or \"people\""})
                return
            # Queued, not started here: whichever process runs the
            # scheduler (possibly another container) picks it up within
            # scheduler.TICK_SECS.
            if not scheduler.trigger(job):
                self._send_json(409, {"ok": False, "error": f"{job} is already running or queued"})
                return
            self._send_json(202, {"ok": True, "started": job, "queued": True})

        def _handle_run(self):
            """Kicks off a scrape (optionally + digest) in a background
            thread and returns immediately — a run can take from seconds
            to several minutes (LinkedIn/Reddit/Twitter/blog go through
            real actor runs), and blocking the HTTP response for that
            whole time left the frontend with nothing to show but a dead
            spinner. The response's `entry` is the "running" placeholder
            from history.start(); poll GET /output/run_history.json for
            that same `run_id` — its `status` flips to "success"/"failed"
            (history.finish()) once the thread completes.
            """
            import threading

            import runner

            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "Malformed JSON body"})
                return

            kind = "person" if payload.get("kind") == "person" else "company"
            target = payload.get("target")
            generate_digest = payload.get("generate_digest", True)
            only = payload.get("only")  # e.g. ["linkedin"] to retry a single failed channel

            if not isinstance(target, dict) or not (target.get("key") or "").strip():
                self._send_json(400, {"ok": False, "error": "target must be a JSON object with a non-empty \"key\""})
                return
            target = dict(target)
            target["key"] = target["key"].strip()
            target.setdefault("display_name", target["key"])

            if only is not None and (not isinstance(only, list) or not all(isinstance(c, str) for c in only)):
                self._send_json(400, {"ok": False, "error": "only must be a list of channel name strings"})
                return

            try:
                limit = int(payload.get("limit") or 10)
            except (TypeError, ValueError):
                self._send_json(400, {"ok": False, "error": "limit must be a number"})
                return

            started_entry = runner.start(kind, target, limit, only=only)
            run_id = started_entry["run_id"]
            threading.Thread(
                target=runner.execute,
                args=(run_id, kind, target, limit, only, generate_digest),
                daemon=True, name=f"run-{run_id}",
            ).start()
            self._send_json(202, {"ok": True, "started": True, "entry": started_entry})

        def _handle_digest(self):
            """Digest-only: summarise whatever is already in this target's
            store, without scraping first. The frontend's Pipeline page
            uses this for its "② Create digest" stage, kept independent of
            "① Fetch" (POST /api/run with generate_digest:false) so either
            stage can be run/re-run on its own.

            Same async pattern as _handle_run: kicks off a background
            thread and returns immediately (a real LLM digest call has
            taken over half a minute in testing); poll
            GET /output/run_history.json for this run_id's status.
            """
            import threading

            from digest.pipeline import run as run_digest
            import history

            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "Malformed JSON body"})
                return

            kind = "person" if payload.get("kind") == "person" else "company"
            target = payload.get("target")
            if not isinstance(target, dict) or not (target.get("key") or "").strip():
                self._send_json(400, {"ok": False, "error": "target must be a JSON object with a non-empty \"key\""})
                return
            target = dict(target)
            target["key"] = target["key"].strip()
            target.setdefault("display_name", target["key"])

            try:
                since_days = int(payload.get("since_days") or 14)
                cap = int(payload.get("max_posts") or 25)
            except (TypeError, ValueError):
                self._send_json(400, {"ok": False, "error": "since_days and max_posts must be numbers"})
                return
            new_only = not payload.get("all_posts", False)

            entry = {
                "kind": kind,
                "key": target["key"],
                "display_name": target["display_name"],
            }
            started_entry = history.start(entry)
            run_id = started_entry["run_id"]

            def worker():
                t0 = time.time()
                updates = {}
                try:
                    digest = run_digest(
                        kind=kind, target=target, new_only=new_only, since_days=since_days, cap=cap
                    )
                    updates["digest"] = {
                        "llm": digest.get("llm"),
                        "posts_considered": digest.get("posts_considered"),
                        "input_tokens": digest.get("input_tokens", 0),
                        "output_tokens": digest.get("output_tokens", 0),
                    }
                    updates["success"] = True
                    updates["status"] = "success"
                except Exception as e:
                    # Broad on purpose: any uncaught error here would kill
                    # the thread before finish(), leaving the run "running"
                    # forever in history.
                    updates["digest"] = {"error": str(e)}
                    updates["success"] = False
                    updates["error"] = str(e)
                    updates["status"] = "failed"

                updates["duration_ms"] = int((time.time() - t0) * 1000)
                history.finish(run_id, updates)

            threading.Thread(target=worker, daemon=True, name=f"digest-{run_id}").start()
            self._send_json(202, {"ok": True, "started": True, "entry": started_entry})

        def _handle_save_target(self):
            import targets
            import people_targets

            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "Malformed JSON body"})
                return

            kind = "person" if payload.get("kind") == "person" else "company"
            target = payload.get("target")
            if not isinstance(target, dict) or not (target.get("key") or "").strip():
                self._send_json(
                    400,
                    {"ok": False, "error": 'target must be a JSON object with a non-empty "key"'},
                )
                return

            try:
                registry = people_targets if kind == "person" else targets
                saved = registry.save(target)
            except ValueError as e:
                self._send_json(400, {"ok": False, "error": str(e)})
                return

            _upsert_manifest_entry(kind, saved)
            self._send_json(200, {"ok": True, "target": saved})

        def do_DELETE(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/api/accounts/"):
                self._handle_delete("company", path[len("/api/accounts/"):])
            elif path.startswith("/api/people/"):
                self._handle_delete("person", path[len("/api/people/"):])
            else:
                self.send_error(404)

        def _handle_delete(self, kind: str, raw_key: str):
            """Remove a target: custom_targets.json entry (if any), local
            store/digest files, and its Postgres rows.

            Refuses to delete a target hand-written in targets.py/
            people_targets.py — those are source code, not data; removing
            one means editing the file, not calling this endpoint.
            """
            import urllib.parse
            import custom_targets
            import db
            from targets import COMPANY_TARGETS
            from people_targets import PEOPLE_TARGETS
            from paths import store_path, digest_path

            key = urllib.parse.unquote(raw_key).strip().strip("/")
            if not key:
                self._send_json(404, {"ok": False, "error": "Missing key in URL"})
                return

            section = "people" if kind == "person" else "companies"
            table = PEOPLE_TARGETS if kind == "person" else COMPANY_TARGETS
            in_custom = key in custom_targets.load_section(section)

            if key in table and not in_custom:
                source_file = "people_targets.py" if kind == "person" else "targets.py"
                self._send_json(400, {
                    "ok": False,
                    "error": f"'{key}' is a built-in target defined in {source_file} "
                    "— edit that file to remove it, this endpoint only removes "
                    "targets saved from the UI or scraped ad-hoc.",
                })
                return

            db_hit = any(row["key"] == key for row in db.list_targets(kind))
            if not in_custom and not db_hit:
                self._send_json(404, {"ok": False, "error": f"No such {kind} target: '{key}'"})
                return

            if in_custom:
                custom_targets.delete(section, key)
                table.pop(key, None)

            for path in (store_path(key), digest_path(key, "json"), digest_path(key, "md")):
                if os.path.exists(path):
                    os.remove(path)

            db.delete_target(kind, key)
            self._send_json(200, {"ok": True, "deleted": key})

    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True

    # Local default stays 127.0.0.1 — the path allowlist above keeps
    # binding 0.0.0.0 from exposing source/.env (e.g. inside a container,
    # where SERVE_HOST=0.0.0.0 lets Docker's port mapping reach it), but
    # /api/* has no auth, so there's no reason to widen the local default.
    host = os.getenv("SERVE_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", args.port))

    import functools
    # Anchored to PROJECT_ROOT, not cwd — otherwise running `serve` from
    # outside this project's directory (e.g. a monorepo root one level up)
    # would serve whatever frontend/output happens to sit at that cwd
    # instead of this project's own.
    ServeHandler = functools.partial(Handler, directory=PROJECT_ROOT)

    import history
    import config
    # With the scheduler in its own process, its runs aren't ours to reap.
    reaped = history.reap_orphaned_running(scope="manual" if config.SCHEDULER_EXTERNAL else "all")

    import scheduler
    scheduler_on = scheduler.start()

    try:
        with Server((host, port), ServeHandler) as httpd:
            _banner("SERVE")
            shown_host = "127.0.0.1" if host == "0.0.0.0" else host
            print(f"  Frontend:  http://{shown_host}:{port}/frontend/")
            if reaped:
                print(f"  ⚠️  Marked {reaped} run(s) left \"running\" by the previous process as interrupted")
            print(
                "  Bound to localhost only, so .env stays private" if host == "127.0.0.1"
                else f"  Bound to {host} — only /frontend, /output, /API.md, and /api/* are servable"
            )
            print("  /api/* has no authentication — anyone who can reach this port can use it")
            if config.SCHEDULER_EXTERNAL:
                print("  Scheduler runs in its own process (python main.py scheduler)")
            elif scheduler_on and config.SCHEDULER_ENABLED:
                print(f"  Scheduler ON: news every {config.SCHEDULE_NEWS_HOURS:g}h, companies every "
                      f"{config.SCHEDULE_COMPANIES_DAYS:g}d, people every "
                      f"{config.SCHEDULE_PEOPLE_DAYS:g}d, summaries to {config.SCHEDULE_NOTIFY_TO or '(nobody)'}")
            else:
                print("  Scheduler timetable off (set SCHEDULER_ENABLED=true); Run now still works")
            print("  Ctrl+C to stop\n")
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    except OSError as e:
        print(f"❌  Could not bind {host}:{port}: {e}")
        return 1
    return 0


# ── list ─────────────────────────────────────────────────────────

def _merged_table(kind: str, table: dict) -> dict:
    """`table` (PEOPLE_TARGETS/COMPANY_TARGETS) plus anything Postgres knows
    about that isn't in it — targets scraped ad-hoc via /api/run and never
    explicitly saved via /api/save-target only live in the database, so
    `list`/`status` would otherwise silently omit them.
    """
    import db

    merged = dict(table)
    for row in db.list_targets(kind):
        merged.setdefault(row["key"], row["config"])
    return merged


def cmd_list(args) -> int:
    if args.person:
        table = _merged_table("person", PEOPLE_TARGETS)
        _banner("CONFIGURED PEOPLE")
        for key in sorted(table):
            try:
                t = resolve_person(key)
            except KeyError:
                t = {"key": key, **table[key]}
            tag = "" if key in PEOPLE_TARGETS else "  [db only]"
            print(f"\n  {t.get('display_name', key)}{tag}")
            print(f"    key        {key}")
            print(f"    linkedin   {t.get('linkedin_url') or '—'}")
            print(f"    twitter    {t.get('twitter_handle') or '—'}")
            print(f"    reddit     {t.get('reddit_query') or '—'}")
            print(f"    sec cik    {t.get('sec_cik') or '—'}")
            print(f"    news query {t.get('news_query') or '—'}")
            print(f"    patents    {t.get('patents_query') or '—'}")
        print(
            "\n  Add or edit people in people_targets.py — [db only] entries were "
            "scraped ad-hoc (e.g. via /api/run) and exist in Postgres but not yet "
            "in that file."
        )

        import db
        personas = [p for p in db.list_personas() if p["key"] not in table]
        if personas:
            _banner(f"CRM PERSONAS (sales_agent-ai) — {len(personas)}")
            print("  Visibility only — not scrape targets until added to people_targets.py.\n")
            header = f"  {'KEY':<28}{'NAME':<40}{'COMPANY':<24}TITLE"
            print(header)
            print("  " + "─" * (len(header) - 2))
            for p in personas:
                print(
                    f"  {p['key'][:27]:<28}{(p.get('display_name') or '—')[:39]:<40}"
                    f"{(p.get('company') or '—')[:23]:<24}{p.get('title') or '—'}"
                )
            print()
        return 0

    table = _merged_table("company", COMPANY_TARGETS)
    _banner("CONFIGURED ACCOUNTS")
    for key in sorted(table):
        try:
            t = resolve_company(key)
        except KeyError:
            t = {"key": key, **table[key]}
        tag = "" if key in COMPANY_TARGETS else "  [db only]"
        print(f"\n  {t.get('display_name', key)}  ({t.get('ticker') or 'private'}){tag}")
        print(f"    key        {key}")
        print(f"    linkedin   {t.get('linkedin_url') or '—'}")
        print(f"    twitter    {t.get('twitter_handle') or '—'}")
        print(f"    sec cik    {t.get('sec_cik')}")
        print(f"    blog       {(t.get('blog_url') or '—')[:76]}")
        print(f"    newsroom   {(t.get('newsroom_url') or '—')[:76]}")
    print(
        "\n  Add or edit accounts in targets.py — [db only] entries were scraped "
        "ad-hoc (e.g. via /api/run) and exist in Postgres but not yet in that file.\n"
    )
    return 0


# ── argument parsing ─────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Account intelligence pipeline: scrape → store → digest → serve.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python main.py run --all --limit 20        full pipeline, every account\n"
            "  python main.py scrape bny --only sec,news  refresh the free channels only\n"
            "  python main.py digest northern_trust\n"
            "  python main.py news-brief bny --send you@company.com\n"
            "  python main.py status\n"
            "  python main.py serve --port 8005\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_selection(p):
        p.add_argument(
            "company",
            nargs="?",
            help=f"one of: {', '.join(sorted(COMPANY_TARGETS))}; "
            "or a person key with --person",
        )
        p.add_argument("--all", action="store_true", help="every configured account/person")
        p.add_argument(
            "--person",
            action="store_true",
            help="treat the target as a person from people_targets.py instead of a company",
        )

    def add_scrape_flags(p):
        p.add_argument("--limit", type=int, default=20, help="posts per channel (default 20)")
        p.add_argument("--only", help="channels to scrape, comma separated")
        p.add_argument("--reset-channel", help="channels to clear before merging")
        p.add_argument("--no-newsroom", action="store_true", help="skip press releases")
        p.add_argument("--no-store", action="store_true", help="overwrite instead of merging")

    def add_digest_flags(p):
        p.add_argument(
            "--all-posts",
            action="store_true",
            help="summarise the whole window, not only newly added posts",
        )
        p.add_argument("--since-days", type=int, default=14, help="recency window")
        p.add_argument("--max-posts", type=int, default=25, help="posts per channel")
        p.add_argument("--out-dir", default=DIGEST_DIR, help="digest output directory")

    p_scrape = sub.add_parser("scrape", help="scrape accounts into their stores")
    add_selection(p_scrape)
    add_scrape_flags(p_scrape)

    p_digest = sub.add_parser("digest", help="generate sales email + channel storylines")
    add_selection(p_digest)
    add_digest_flags(p_digest)

    p_news_brief = sub.add_parser(
        "news-brief", help="today + yesterday's news/bloomberg coverage (no LLM, instant)"
    )
    add_selection(p_news_brief)
    p_news_brief.add_argument("--send", help="recipient email(s) to send the brief to, comma separated")

    p_run = sub.add_parser("run", help="scrape, then digest")
    add_selection(p_run)
    add_scrape_flags(p_run)
    add_digest_flags(p_run)

    p_status = sub.add_parser("status", help="what is stored, and how fresh it is")
    p_status.add_argument("--out-dir", default=DIGEST_DIR)
    p_status.add_argument("--person", action="store_true", help="show people instead of companies")

    sub.add_parser(
        "db-backfill",
        help="sync existing JSON output into Postgres (needs DATABASE_URL in .env)",
    )

    p_serve = sub.add_parser("serve", help="serve the frontend on localhost")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)

    sub.add_parser(
        "scheduler",
        help="run the scheduled jobs in this process until stopped "
        "(production: its own container — see docker-compose.yml)",
    )

    p_list = sub.add_parser("list", help="show configured accounts")
    p_list.add_argument("--person", action="store_true", help="show people instead of companies")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.command == "scrape":
        return asyncio.run(cmd_scrape(args))
    if args.command == "digest":
        return cmd_digest(args)
    if args.command == "news-brief":
        return cmd_news_brief(args)
    if args.command == "run":
        return asyncio.run(cmd_run(args))
    if args.command == "status":
        return cmd_status(args)
    if args.command == "db-backfill":
        return cmd_db_backfill(args)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "list":
        return cmd_list(args)
    if args.command == "scheduler":
        import scheduler
        return scheduler.run_forever()
    return 1


if __name__ == "__main__":
    sys.exit(main())
