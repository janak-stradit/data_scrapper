# Pipeline API

The `python main.py serve` process exposes a small local HTTP API for
triggering the scrape → store → digest pipeline and sending the resulting
sales email — on top of the same server that serves the frontend and its
static JSON output files. This lets another app drive the pipeline
programmatically instead of going through the UI or the CLI.

## Base URL

```
http://127.0.0.1:8001
```

Start it with:

```bash
python main.py serve --port 8001
```

**This server is not hardened for external or multi-tenant use.** It binds
to `127.0.0.1` only, has no authentication, and serves the whole project
directory as static files (including `.env`) to anything that can reach
that port. Treat it as a local integration point — e.g. another process on
the same machine, or a backend you control that proxies to it — not
something to expose on a network directly.

It's a threaded `http.server`, not a production WSGI/ASGI server: fine for
one integration talking to it at a time, not for high concurrency.

## Authentication

None. Anything that can reach `127.0.0.1:<port>` can call every endpoint.

---

## `GET /api/accounts` / `GET /api/people`

List every registered company (or person) target — built-in ones from
`targets.py`/`people_targets.py` plus anything saved via
`/api/save-target` — with a quick-glance status for each, without having
to know the underlying file layout.

```
GET /api/accounts
GET /api/people
```

### Response — `200 OK`

```json
{
  "accounts": [
    {
      "key": "bny",
      "display_name": "BNY (Bank of New York Mellon)",
      "ticker": "BK",
      "channels": ["linkedin", "twitter", "reddit", "sec", "news", "blog", "newsroom"],
      "has_store": true,
      "has_digest": true,
      "total_posts": 99,
      "last_run": "2026-08-22T14:11:56.913805Z"
    }
  ]
}
```

`/api/people` returns the same shape under a `"people"` key; `ticker` is
always `null` there. `channels` lists which channels this target has
configured (derived from which fields are set — `linkedin_url`,
`sec_cik`, etc. — not from an actual scrape). `total_posts` / `last_run`
are `null` if nothing has been scraped for this target yet.

---

## `GET /api/accounts/<key>` / `GET /api/people/<key>`

The full picture for one target: its config, everything scraped so far,
and its latest digest — in one call, instead of separately fetching the
store and digest JSON files.

```
GET /api/accounts/bny
GET /api/people/robin_vince
```

`<key>` also accepts any alias defined in `targets.py`/`people_targets.py`
(e.g. `GET /api/accounts/bnymellon` works the same as `/api/accounts/bny`).

### Response — `200 OK`

```json
{
  "target": { "key": "bny", "display_name": "BNY (Bank of New York Mellon)", "...": "..." },
  "store": { "...": "everything in output/stores/bny_output.json, or null if never scraped" },
  "digest": { "...": "everything in output/digests/bny_digest.json, or null if never generated" }
}
```

### Response — `404 Not Found`

```json
{ "ok": false, "error": "Unknown company 'nonexistent'. Known: blackrock, bny, northern_trust, vanguard" }
```

---

## `POST /api/run`

Runs the pipeline (scrape all applicable channels → merge into the store →
optionally generate the digest) for one target, then logs the outcome to
run history. This is synchronous — the request blocks until the whole run
finishes, which can take from a couple of seconds (free channels only) to
several minutes (LinkedIn/Reddit/Twitter/blog all go through real Apify
actor runs).

The target does **not** need to be pre-registered in `targets.py` /
`people_targets.py`. Pass it as JSON and it's scraped as a one-off; only
`key` is required, and it's used to name the output files
(`output/stores/<key>_output.json`, `output/digests/<key>_digest.json`).
Fields you omit are simply not scraped for that channel — nothing is
inferred or defaulted beyond that.

### Request

```
POST /api/run
Content-Type: application/json
```

```json
{
  "kind": "company",
  "limit": 10,
  "generate_digest": true,
  "target": {
    "key": "example_co",
    "display_name": "Example Co",
    "ticker": "EXCO",
    "linkedin_url": "https://www.linkedin.com/company/example/",
    "twitter_handle": "@example",
    "reddit_query": "\"Example Co\"",
    "reddit_keywords": ["example", "exco"],
    "reddit_exclude": ["some-unrelated-thing"],
    "sec_cik": "0000000000",
    "news_query": "\"Example Co\"",
    "news_exclude": "",
    "blog_url": "https://example.com/insights",
    "blog_glob": "https://example.com/insights/**",
    "newsroom_url": "https://example.com/newsroom"
  }
}
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `kind` | `"company"` \| `"person"` | `"company"` | `"person"` skips blog/newsroom (those don't apply to an individual) and enables `patents_query` |
| `limit` | integer | `10` | posts fetched per channel |
| `generate_digest` | boolean | `true` | run the LLM digest after scraping; `false` to scrape only |
| `target` | object | — | **required**. Must include a non-empty `key` |

#### `target` fields (all optional except `key`)

Same shape as an entry in `targets.py` (company) or `people_targets.py`
(person) — see those files for the full field list per channel. The
channels that get scraped are exactly the ones with a truthy value here:

| Field | Channel | company | person |
|---|---|---|---|
| `key` | — (required, used for output filenames) | ✓ | ✓ |
| `display_name` | — (defaults to `key` if omitted) | ✓ | ✓ |
| `ticker` | — (cosmetic only) | ✓ | — |
| `linkedin_url` | linkedin | ✓ | ✓ |
| `twitter_handle` | twitter | ✓ | ✓ |
| `reddit_query`, `reddit_keywords`, `reddit_exclude` | reddit | ✓ | ✓ |
| `sec_cik` | sec | ✓ | ✓ |
| `news_query`, `news_exclude` | news | ✓ | ✓ |
| `blog_url`, `blog_glob`, `blog_sitemap`, `blog_min_segments` | blog | ✓ | — |
| `newsroom_url`, `newsroom_glob`, `newsroom_sitemap` | newsroom | ✓ | — |
| `patents_query` | patents | — | ✓ |
| `rss_url` | rss | ✓ | ✓ |
| `youtube_channel_id` | youtube | ✓ | ✓ |
| `sec_mentions_query` | sec_mentions | ✓ | ✓ |
| `regulatory_query` | regulatory | ✓ | ✓ |
| `linkedin_jobs_query` | linkedin_jobs | ✓ | — |

### Response — `200 OK`

```json
{
  "ok": true,
  "entry": {
    "recorded_at": "2026-08-23T06:04:18.871686Z",
    "kind": "company",
    "key": "example_co",
    "display_name": "Example Co",
    "limit": 10,
    "new_posts": 6,
    "total_posts": 42,
    "platforms_scraped": ["sec", "news"],
    "platforms_failed": [],
    "digest": {
      "llm": "anthropic/claude-sonnet-5",
      "posts_considered": 12
    },
    "success": true,
    "duration_ms": 8213
  }
}
```

- `ok` mirrors `entry.success`.
- `new_posts` / `total_posts` — posts added this run / total now on file for this target.
- `platforms_failed` — channels that errored (e.g. an Apify actor timing out); the run as a whole can still be `success: true` with some channels failed.
- `digest` — present only when `generate_digest: true` was requested.
  - `digest.llm` describes the model actually used. If no `ANTHROPIC_API_KEY` (or the configured provider's key) is set, this reads `"... — ANTHROPIC_API_KEY not set, falling back to dry-run"` and the digest is a placeholder, not a real LLM-written briefing — check for that substring if your integration cares.
  - `digest.error` appears instead of the above if digest generation itself failed (e.g. no posts were in scope).
- A run can still return `200` with `"success": false` and an `"error"` field at the top level of `entry` if the scrape itself threw (e.g. all channels failed).

### Response — `400 Bad Request`

Returned before anything runs, for malformed input:

```json
{ "ok": false, "error": "target must be a JSON object with a non-empty \"key\"" }
```

Other `400` cases: malformed JSON body, non-numeric `limit`.

### curl example

```bash
curl -s -X POST http://127.0.0.1:8001/api/run \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "person",
    "limit": 15,
    "generate_digest": true,
    "target": {
      "key": "jane_doe",
      "display_name": "Jane Doe (CEO, Example Co)",
      "linkedin_url": "https://www.linkedin.com/in/janedoe/",
      "news_query": "\"Jane Doe\"",
      "sec_cik": "0001234567"
    }
  }'
```

### Cost note

`linkedin`, `reddit`, `twitter`, and `blog` each run a real Apify actor
billed against `APIFY_TOKEN` in `.env`. So does `news`, partially — the
headline list itself is free (Google's own RSS), but every article's full
text is then fetched via the same website-content-crawler actor
`blog` uses, one article per headline, best-effort (silently falls back
to headline-only per-article if a publisher blocks the fetch — this
doesn't reduce the Apify cost of having attempted it). `linkedin_jobs` also
runs a real Apify actor (`harvestapi/linkedin-job-search`), but pay-per-result
at $1/1,000 jobs — negligible at the ~10-20 postings per run this pipeline
asks for. `sec`, `patents`, `rss`, and `youtube` are the only channels with
no Apify cost at all
(`youtube` needs a free `YOUTUBE_API_KEY` from Google Cloud Console, with
its own 10,000-unit/day quota — not billed, but can be exhausted on very
heavy use). An integration calling this frequently on a target with
social fields (or `news_query`) populated will consume Apify compute
units on every call — there's no dedupe/rate-limiting beyond what the
store's incremental fetch windows already do (see `store.py`).

---

## `POST /api/save-target`

Registers a target permanently, so it shows up in the regular
Accounts/People sidebar and can be run by key from the CLI afterward
(`python main.py scrape <key>`), instead of only existing as a one-off
`/api/run` call. Writes to `custom_targets.json` (loaded alongside
`targets.py`/`people_targets.py` at process start) and adds an entry to
`frontend/manifest.json`. Doesn't scrape anything itself — pair it with
`/api/run` if you also want data pulled immediately.

### Request

```
POST /api/save-target
Content-Type: application/json
```

```json
{
  "kind": "company",
  "target": {
    "key": "example_co",
    "display_name": "Example Co",
    "ticker": "EXCO",
    "news_query": "\"Example Co\""
  }
}
```

Same `target` shape as `/api/run` — only `key` is required. Saving a `key`
that already exists (built-in or previously saved) overwrites it.

### Response — `200 OK`

```json
{
  "ok": true,
  "target": {
    "key": "example_co",
    "display_name": "Example Co",
    "ticker": "EXCO",
    "news_query": "\"Example Co\""
  }
}
```

### Response — `400 Bad Request`

```json
{ "ok": false, "error": "target must be a JSON object with a non-empty \"key\"" }
```

---

## `POST /api/send-email`

Sends a plain-text email as the signed-in Microsoft account, via
[Microsoft Graph](mailer.py) (`me/sendMail`). Requires a one-time
interactive sign-in already completed on this machine — see
`mailer.py`'s docstring and `.env` section 7 — this endpoint never
triggers that login itself; it fails fast if there's no cached session.

### Request

```
POST /api/send-email
Content-Type: application/json
```

```json
{
  "to": "someone@example.com",
  "subject": "Subject line",
  "body": "Plain-text body."
}
```

### Response — `200 OK`

```json
{ "ok": true }
```

or, on any failure (bad recipient, not signed in, Graph error):

```json
{ "ok": false, "error": "Not signed in to Microsoft yet (or the cached session expired). Run `python mailer.py --login` from a terminal once, then Send Mail will work from the app." }
```

`error` messages are written to be shown directly to an end user — they
don't leak internal stack traces.

---

## Reading results

Account/people data has proper endpoints (above). A few other things
don't yet — they're plain static JSON files the server already serves,
same as the frontend uses:

| What | URL |
|---|---|
| Run history (most recent first, capped at 200) | `GET /output/run_history.json` |
| One target's accumulated posts (same data as `/api/accounts/<key>`'s `store`) | `GET /output/stores/<key>_output.json` |
| One target's latest digest (same data as `/api/accounts/<key>`'s `digest`) | `GET /output/digests/<key>_digest.json` |
| Frontend's own account list (used by the sidebar; prefer `/api/accounts`+`/api/people` for integrations) | `GET /frontend/manifest.json` |
| Targets saved via `/api/save-target`, raw | `GET /custom_targets.json` |

`run_history.json` is a JSON array, newest entry first, in exactly the
shape returned in `entry` above (plus every past run). Poll it after a
`POST /api/run` call if you'd rather not hold the connection open for the
duration of a long scrape — though note `/api/run` is currently
synchronous, so you already have the result in the response body by the
time it returns.

---

## Integration checklist

1. `python main.py serve --port 8001` running on the target machine.
2. `APIFY_TOKEN` set in `.env` if you'll scrape linkedin/reddit/twitter/blog.
3. `ANTHROPIC_API_KEY` (or your chosen `LLM_PROVIDER`'s key) set in `.env`
   if you want real digests instead of dry-run placeholders.
4. For `/api/send-email`: `GRAPH_CLIENT_ID` set and `python mailer.py --login`
   already run once on this machine.
5. Point your app at `POST /api/run`, read `entry.digest.llm` to detect
   dry-run, and `entry.platforms_failed` to detect partial failures.
