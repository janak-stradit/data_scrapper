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
      "last_run": "2026-08-22T14:11:56.913805Z",
      "target": { "key": "bny", "display_name": "BNY (Bank of New York Mellon)", "linkedin_url": "…" }
    }
  ]
}
```

`target` is the full target config — the same object `POST /api/run`
takes as its `target`, so a list consumer can re-run any row directly.

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

Kicks off the pipeline (scrape all applicable channels → merge into the
store → optionally generate the digest) for one target, in a background
thread, and returns immediately. **Asynchronous** — the response is not
the result of the run, just confirmation it started; a real run can take
anywhere from a couple of seconds (free channels only) to several minutes
(LinkedIn/Reddit/Twitter/blog go through real Apify actor runs) or, on a
slow day, tens of minutes (SEC EDGAR alone has taken up to 10 minutes in
testing). Poll `GET /output/run_history.json` for the `run_id` this
returns — that entry's `status` starts `"running"` and flips to
`"success"`/`"failed"` once the background thread finishes, at which
point the same fields the old synchronous response used to return
directly (`new_posts`, `digest`, etc.) are filled in on it.

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
| `only` | array of strings | — | restrict the run to these channels only (e.g. `["linkedin"]`) — used by the frontend's per-channel retry button; omit to scrape every channel the target has configured |
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

### Response — `202 Accepted`

```json
{
  "ok": true,
  "started": true,
  "entry": {
    "run_id": "a1b2c3d4e5f64a7b8c9d0e1f2a3b4c5d",
    "recorded_at": "2026-08-23T06:04:18.871686Z",
    "status": "running",
    "kind": "company",
    "key": "example_co",
    "display_name": "Example Co",
    "limit": 10
  }
}
```

`ok`/`started` just mean the background thread was launched — they say
nothing about whether the run itself will succeed. Poll
`GET /output/run_history.json`, find the entry whose `run_id` matches, and
watch its `status`:

```json
{
  "run_id": "a1b2c3d4e5f64a7b8c9d0e1f2a3b4c5d",
  "status": "success",
  "recorded_at": "2026-08-23T06:04:18.871686Z",
  "kind": "company",
  "key": "example_co",
  "display_name": "Example Co",
  "limit": 10,
  "new_posts": 6,
  "total_posts": 42,
  "platforms_scraped": ["sec", "news"],
  "platforms_failed": [],
  "cost_usd": 0.014,
  "digest": {
    "llm": "anthropic/claude-sonnet-5",
    "posts_considered": 12,
    "input_tokens": 4807,
    "output_tokens": 2493
  },
  "success": true,
  "duration_ms": 8213
}
```

- `status`: `"running"` → `"success"` or `"failed"`. Only meaningful once
  it's left `"running"` — every field below is absent/stale until then.
- `new_posts` / `total_posts` — posts added this run / total now on file for this target.
- `platforms_failed` — channels that errored (e.g. an Apify actor timing out); the run as a whole can still be `success: true` with some channels failed.
- `cost_usd` — total Apify actor cost this run actually incurred (`0` if every channel scraped was free — `sec`/`patents`/`rss`/`youtube`/`reddit`).
- `digest` — present only when `generate_digest: true` was requested.
  - `digest.llm` describes the model actually used. If no `ANTHROPIC_API_KEY` (or the configured provider's key) is set, this reads `"... — ANTHROPIC_API_KEY not set, falling back to dry-run"` and the digest is a placeholder, not a real LLM-written briefing — check for that substring if your integration cares.
  - `digest.input_tokens`/`digest.output_tokens` — LLM token usage across every channel-summary call plus the email rollup call.
  - `digest.error` appears instead of the above if digest generation itself failed (e.g. no posts were in scope).
- A finished run can still have `"status": "failed"`, `"success": false`, and an `"error"` field at the top level if the scrape itself threw (e.g. all channels failed).
- If your poller gives up before `status` leaves `"running"` (the run is unusually slow, or the process was restarted mid-run), the entry just stays `"running"` — there's no separate timeout/cancellation signal; the background thread either finishes and calls `finish()`, or the process exits and it's abandoned as one, distinguishable only by checking whether the process is still running.

### Response — `400 Bad Request`

Returned before anything is even scheduled, for malformed input:

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
# {"ok":true,"started":true,"entry":{"run_id":"...","status":"running",...}}

# then poll:
curl -s http://127.0.0.1:8001/output/run_history.json \
  | python3 -c "import json,sys; print(next(e for e in json.load(sys.stdin) if e['run_id']=='<run_id>'))"
```

### Cost note

`linkedin`, `twitter`, and `blog` each run a real Apify actor billed
against `APIFY_TOKEN` in `.env`. So does `news`, partially — the headline
list itself is free (Google's own RSS), but every article's full text is
then fetched via the same website-content-crawler actor `blog` uses, one
article per headline, best-effort (silently falls back to headline-only
per-article if a publisher blocks the fetch — this doesn't reduce the
Apify cost of having attempted it). `linkedin_jobs` also runs a real
Apify actor (`harvestapi/linkedin-job-search`), but pay-per-result at
$1/1,000 jobs — negligible at the ~10-20 postings per run this pipeline
asks for. `sec`, `patents`, `rss`, `youtube`, and `reddit` are the only
channels with no Apify cost at all (`youtube` needs a free
`YOUTUBE_API_KEY` from Google Cloud Console, with its own 10,000-unit/day
quota — not billed, but can be exhausted on very heavy use; `reddit` fetches
Reddit's own free `.rss` search/listing feed directly — no actor, no
token, no per-run charge, just Reddit's own unauthenticated rate limit).
An integration calling this frequently on a target with `linkedin`/
`twitter`/`blog` fields (or `news_query`) populated will consume Apify
compute units on every call — there's no dedupe/rate-limiting beyond what
the store's incremental fetch windows already do (see `store.py`).

The person-only public-records channels — `web_search` (DuckDuckGo),
`sec_proxy` (SEC DEF 14A), `brokercheck` (FINRA BrokerCheck) and
`uk_directorships` (UK Companies House, needs a free
`COMPANIES_HOUSE_API_KEY`) — have no Apify cost. `web_search` uses an
unofficial endpoint that rate-limits: requests are spaced 5s apart, and a
throttled request fails that channel with "rate-limited" rather than
returning nothing.

---

## `POST /api/digest`

Generates a digest from whatever is **already in the target's store** —
no scraping happens. Pairs with `POST /api/run` called with
`generate_digest: false`: run that first to fetch posts, then this
whenever you want a (re)generated digest without paying for another
scrape. This is what the frontend's **Pipeline** page uses for its two
independent stage buttons (① Fetch, ② Create digest).

**Asynchronous, same as `/api/run`**: returns immediately once the
background thread is launched; a real LLM call has taken over half a
minute in testing. Poll `GET /output/run_history.json` for the returned
`run_id` — see `/api/run` above for the full polling contract, `status`
values, and field meanings, all shared between the two endpoints.

### Request

```
POST /api/digest
Content-Type: application/json
```

```json
{
  "kind": "company",
  "since_days": 14,
  "max_posts": 25,
  "all_posts": false,
  "target": { "key": "example_co", "display_name": "Example Co" }
}
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `kind` | `"company"` \| `"person"` | `"company"` | same meaning as `/api/run` |
| `target` | object | — | **required**. Must include a non-empty `key` matching a target that already has a store file (`output/stores/<key>_output.json`) — see `GET /api/accounts/<key>` / `GET /api/people/<key>` to fetch the full target config first |
| `since_days` | integer | `14` | recency window passed to the digest pipeline |
| `max_posts` | integer | `25` | posts per channel considered |
| `all_posts` | boolean | `false` | `true` summarises everything in the window; `false` (default) summarises only posts added by the most recent scrape |

### Response — `202 Accepted`

```json
{
  "ok": true,
  "started": true,
  "entry": {
    "run_id": "f1e2d3c4b5a64738291607f5e4d3c2b1",
    "recorded_at": "2026-09-12T09:00:00.000000Z",
    "status": "running",
    "kind": "company",
    "key": "example_co",
    "display_name": "Example Co"
  }
}
```

Poll `GET /output/run_history.json` for that `run_id`; once `status`
leaves `"running"`, the same entry carries `digest`, `success`,
`duration_ms` — same shapes `/api/run`'s finished entry does. A failed
digest generation shows up as `digest.error` (with `status: "failed"`,
`success: false`) — most commonly `"No store at ... Run: python main.py
scrape <key> ..."` when nothing has been scraped for this target yet, or
`"Nothing to summarise — no posts in scope"` when `since_days`/`all_posts`
narrows the window to zero posts.

### Response — `400 Bad Request`

```json
{ "ok": false, "error": "target must be a JSON object with a non-empty \"key\"" }
```

---

## `POST /api/news-brief`

Builds a **Daily News Brief** — today's and yesterday's posts from the
`news` and `bloomberg` channels only, formatted as a subject/body/html
ready to send. Unlike `/api/digest`, this makes no LLM call: it's a plain
filter + format over whatever's already in the target's store, so it
answers **synchronously**, no polling required. Send it with the generic
`/api/send-email` below, passing this response's `subject`/`body`/`html`
straight through.

### Request

```
POST /api/news-brief
Content-Type: application/json
```

```json
{
  "kind": "company",
  "target": { "key": "example_co", "display_name": "Example Co" }
}
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `kind` | `"company"` \| `"person"` | `"company"` | same meaning as `/api/run` |
| `target` | object | — | **required**. Must include a non-empty `key` matching a target that already has a store file (`output/stores/<key>_output.json`) |

### Response — `200 OK`

```json
{
  "ok": true,
  "brief": {
    "company": "Example Co",
    "company_key": "example_co",
    "kind": "company",
    "generated_at": "2026-09-17T09:00:00.000000Z",
    "window": "today + yesterday (UTC)",
    "posts_considered": 3,
    "posts": [
      {
        "platform": "news",
        "channel": "news",
        "title": "Example Co announces...",
        "post_url": "https://news.google.com/...",
        "source": "Reuters",
        "published_at": "Wed, 17 Sep 2026 08:15:00 GMT"
      }
    ],
    "subject": "Daily News Brief — Example Co — Sep 17, 2026",
    "body": "Daily News Brief — Example Co — Sep 17, 2026\n\n- [Google News] ...",
    "html": "<div ...>...</div>"
  }
}
```

`posts` carries every field the original scraped post had, plus `channel`
(`"news"` or `"bloomberg"`) so the two can be told apart in a combined
list. An empty `posts` array (with a "No news or Bloomberg coverage..."
`body`/`html`) is a normal, successful response — not an error.

### Response — `400 Bad Request` / soft failure

```json
{ "ok": false, "error": "target must be a JSON object with a non-empty \"key\"" }
```

A target with no store yet comes back `200 OK` with `{"ok": false,
"error": "No store at ... Run: python main.py scrape <key> ..."}`, same
convention as `/api/send-email`'s failure responses below.

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

Sends an email via [SMTP with STARTTLS](mailer.py). Requires
`SMTP_HOST`/`SMTP_USERNAME`/`SMTP_PASSWORD` set in `.env` section 7 —
verify they work with `python mailer.py --test` (authenticates without
sending anything) before relying on this endpoint.

### Request

```
POST /api/send-email
Content-Type: application/json
```

```json
{
  "to": "someone@example.com, someone.else@example.com",
  "subject": "Subject line",
  "body": "Plain-text body.",
  "html": "<div>...</div>"
}
```

`to` accepts one address or several separated by commas/semicolons —
every one of them receives the mail.

`html` is optional — when given, the email is sent as
`multipart/alternative` (HTML + the plain-text `body` as a fallback for
clients that can't render HTML); when omitted, it's sent as plain text
only, same as before.

### Response — `200 OK`

```json
{ "ok": true }
```

or, on any failure (bad recipient, SMTP not configured, authentication
rejected, connection error):

```json
{ "ok": false, "error": "SMTP authentication failed: (535, b'...')" }
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
2. `APIFY_TOKEN` set in `.env` if you'll scrape linkedin/twitter/blog (reddit is free, no token needed).
3. `ANTHROPIC_API_KEY` (or your chosen `LLM_PROVIDER`'s key) set in `.env`
   if you want real digests instead of dry-run placeholders.
4. For `/api/send-email`: `SMTP_HOST`/`SMTP_USERNAME`/`SMTP_PASSWORD` set in
   `.env`, verified with `python mailer.py --test`.
5. Point your app at `POST /api/run` — it's async: read `entry.run_id`
   from the `202` response, then poll `GET /output/run_history.json` for
   that `run_id` until `status` leaves `"running"`. Once finished, read
   `digest.llm` to detect dry-run and `platforms_failed` to detect
   partial failures.
