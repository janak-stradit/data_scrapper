# 🕷️ Account Intelligence Engine (Python)

One command scrapes **LinkedIn, Reddit, Twitter/X, Insights blog, Newsroom,
SEC EDGAR filings, and Google News** for a target company, stores the results
without duplicates, and turns them into a sourced sales briefing.

## 🚀 Quick start

```bash
pip install -r requirements.txt
cp .env .env.local   # or edit .env directly — it holds APIFY_TOKEN and the LLM key

python main.py list                    # what accounts are configured
python main.py list --person           # what people are configured

python main.py run --all --limit 20    # scrape, then digest, every account
python main.py run bny --limit 20      # scrape, then digest, one account only
python main.py run robin_vince --person --limit 20   # same, for one person

python main.py status                  # what is stored and how fresh it is
python main.py serve                   # http://127.0.0.1:8001/frontend/
```

## 🎛️ `main.py` — the single entry point

Everything runs through `main.py`. Each subcommand takes a company key or
`--all`, and every subcommand accepts `--help`.

| Command | What it does |
|---------|--------------|
| `scrape` | Scrape accounts into their stores |
| `digest` | Generate the sales email + channel storylines |
| `run` | `scrape`, then `digest` |
| `status` | Stored post counts, last scrape, digest freshness |
| `serve` | Serve the frontend on localhost |
| `list` | Show configured accounts |

```bash
python main.py scrape bny --limit 20
python main.py scrape bny --only sec,news        # refresh the free channels
python main.py digest northern_trust --since-days 60 --all-posts
python main.py run --all --limit 20
python main.py serve --port 8001
```

Every one of these takes a single company key (e.g. `bny`) instead of
`--all` to run against just that account, and `--person` to run against a
person from `people_targets.py` instead of a company:

```bash
python main.py scrape bny --limit 20                     # one account
python main.py digest bny                                 # one account
python main.py run bny --limit 20                          # one account, scrape + digest

python main.py scrape robin_vince --person --limit 20     # one person
python main.py digest robin_vince --person                 # one person
python main.py run robin_vince --person --limit 20         # one person, scrape + digest
```

Scrape flags: `--limit N`, `--only a,b`, `--reset-channel news`,
`--no-newsroom`, `--no-store`.
Digest flags: `--all-posts`, `--since-days N`, `--max-posts N`, `--out-dir DIR`.

`serve` binds to `127.0.0.1` deliberately — this directory holds `.env` with
API tokens, and the default bind would expose it to the whole network.

`engine.py` keeps its own CLI for direct scraping, but `main.py` is the
supported path.

## 🏢 Accounts & 🧑 People

Companies live in `targets.py` (LinkedIn page, X handle, Reddit query, insights
blog, newsroom, SEC CIK, news query). Currently configured: `bny`,
`northern_trust`, `blackrock`, `vanguard`.

People live in `people_targets.py` (LinkedIn, Reddit/news mention search,
X handle, SEC CIK if they file as a corporate insider, patents). Currently
configured: `ranjit_samra`, `robin_vince`. Every subcommand that takes a
company key also accepts a person key with `--person`:

```bash
python main.py list --person
python main.py run robin_vince --person --limit 20
```

`list` and `status` also show targets that only exist in Postgres (tagged
`[db only]`) — anything scraped ad-hoc via `POST /api/run` gets mirrored to
the database (see `db.py`) even if it was never explicitly saved into
`targets.py`/`people_targets.py`/`custom_targets.json`.

`list --person` additionally lists **CRM personas** — contacts from the
`personas` table in the same Neon database, owned by the sibling
`sales_agent-ai` app this repo is subtree-merged into (see `MERGE_PLAN.md`).
These are shown for discovery only (visibility, not scraping): a persona
isn't a scrape target until it's added to `people_targets.py` (or saved via
`/api/save-target`), since scraping all of them would be far more Apify
usage than the handful of people this pipeline is meant to track closely.

## 📂 Where output goes

Everything the pipeline writes lives under `output/`, resolved in `paths.py`, so
the project root stays code-only and results can be archived in one move.

```
output/
├── stores/<company>_output.json     ← accumulated posts (the incremental store)
└── digests/<company>_digest.json    ← generated digest
    <company>_digest.md              ← same digest, human-readable
```

## ♻️ Incremental runs (no duplicate data, lower cost)

The store file **is** the state. Re-running a company adds only posts that were
not already stored, and narrows what it asks the paid actors for:

- Google News is asked only for the days since the last run, not a flat 30.
- Blog/newsroom article URLs already stored are dropped from the crawl seeds, so
  the crawler never pays to re-render a page you already have.
- Every post carries `first_seen`, `last_seen`, and `new_in_last_run`; each
  channel reports `new_last_run`.

De-duplication keys are per channel: SEC uses the permanent accession number,
Google News uses the headline (its RSS links carry a per-request token, so the
same article arrives with a new URL every fetch), everything else uses the URL.

## 🧠 Digest pipeline (`digest/`)

The digest is a package, not a script:

```
digest/
├── pipeline.py     ← orchestration: select → summarise → roll up → write
├── selection.py    ← which posts a digest looks at, and how they are formatted
├── prompts.py      ← channel labels, per-channel guidance, the two system prompts
├── llm_client.py   ← provider-agnostic LLM adapter
└── renderer.py     ← Markdown rendering
```

It produces two things per account:

1. **A sales email** — subject, priority, confidence, body, sourced talking points.
2. **A storyline per channel** — hook, angle, tone, and post ideas for the social team.

```bash
python main.py digest bny                    # only posts the last run added
python main.py digest --all --since-days 14
python main.py digest bny --all-posts        # everything in the window
```

### The digest schema is evidence-first

Every claim is separated from every inference, and everything carries a source
URL. The point is that a rep can verify any sentence before saying it out loud.

Per channel:

| Field | Why it exists |
|-------|---------------|
| `observed[]` | Only what the posts literally say — each with a `source_url` |
| `interpretation` | Inference, explicitly hedged, kept out of `observed` |
| `evidence_strength` | `strong` / `moderate` / `weak` — forces thin channels to be reported as thin |
| `evidence_note` | One line on why: volume, specificity, recency, or what was missing |
| `summary`, `themes`, `sales_angle` | The 30-second read |
| `notable_posts[]` | `headline` + `source_url` + `why` |
| `do_not_say[]` | Real but unsafe to repeat — unannounced personnel moves, hostile sentiment |
| `storyline` | `hook`, `angle`, `post_ideas`, `suggested_tone`, `avoid` |

Per account, the email adds `confidence`, `talking_points[]` (each with its own
`source_url` and originating channel), `do_not_say[]`, and `data_gaps[]` — what
was missing or stale enough to limit the briefing.

Splitting fact from inference is the whole design: blending them into one fluent
paragraph is exactly what makes a confident hallucination read like analysis.
A collection failure (a crawler that only reached navigation pages) is reported
as a collection failure, never as a quiet account.

### LLM configuration

Provider-agnostic (`digest/llm_client.py`) — set in `.env`:

```bash
LLM_PROVIDER=anthropic     # anthropic | openai | ollama | dry-run
ANTHROPIC_API_KEY=sk-...   # or OPENAI_API_KEY; ollama needs no key
# LLM_MODEL=claude-sonnet-5   # optional override
```

**Without a key the pipeline still runs**, emitting clearly-marked `[DRY RUN]`
placeholders in the same schema so the wiring is testable — it never fabricates
analysis.

## 🖥️ Frontend

`frontend/index.html` is a Bootstrap + jQuery explorer: click an account → read
its digest (evidence, interpretation, do-not-say, talking points) → drill into
the channels and the underlying posts. It fetches the JSON files, so serve it
over HTTP:

```bash
python main.py serve
# open http://127.0.0.1:8001/frontend/
```

Accounts and file paths are listed in `frontend/manifest.json`. The same
`main.py serve` process also exposes `POST /api/run` (trigger the pipeline
for a target given as raw JSON, no pre-registration needed) and
`POST /api/send-email` — see **[API.md](API.md)** if you want another app
to call this instead of using the UI. To put this on AWS instead of
running it locally, see **[DEPLOY.md](DEPLOY.md)**.

## 📁 Structure

```
apify_scraper_engine/
├── main.py                ← Single entry point (scrape · digest · run · status · serve · list)
├── engine.py              ← Orchestrator: one call → all channels
├── config.py              ← Central config (tokens, actors, timeouts)
├── targets.py             ← Company targets (BNY, Northern Trust, BlackRock, Vanguard)
├── store.py               ← Incremental store (dedup + cost savings)
├── paths.py               ← Canonical output locations
├── digest/                ← LLM digest package (see above)
├── scrapers/              ← One module per channel
│   ├── base_scraper.py    ← Abstract base with shared utilities
│   ├── linkedin_scraper.py
│   ├── reddit_scraper.py
│   ├── twitter_scraper.py
│   ├── blog_scraper.py    ← Insights blog + newsroom
│   ├── sec_scraper.py
│   └── news_scraper.py
├── frontend/              ← Bootstrap + jQuery explorer + digest view
├── output/                ← stores/ and digests/ (generated)
├── requirements.txt
└── .env                   ← APIFY_TOKEN, LLM provider + key, SEC_USER_AGENT
```

## 🔌 Programmatic usage

```python
import asyncio
from engine import scrape_and_store
from digest import run as run_digest

asyncio.run(scrape_and_store("bny", limit=20))
digest = run_digest("bny", new_only=False, since_days=60)
print(digest["email"]["subject"])
```

## 🔗 Apify actors used

| Channel   | Actor | Purpose |
|-----------|-------|---------|
| LinkedIn  | `harvestapi/linkedin-company-posts` | Company page posts (no cookies) |
| Reddit    | `trudax/reddit-scraper-lite` | Search-based mention tracking |
| Twitter/X | `apidojo/twitter-scraper-lite` | Profile tweets |
| Blog      | `apify/website-content-crawler` | Insights articles |
| Newsroom  | `apify/website-content-crawler` | Press releases |
| SEC EDGAR | *none* — free `data.sec.gov` API | 8-K / 10-K / 10-Q / 13F filings |
| Google News | *none* — free Google News RSS | Third-party press coverage |

SEC and Google News cost nothing and need no token. EDGAR wants a contact address
in the User-Agent — override it with `SEC_USER_AGENT` in `.env`. CIKs live in
`targets.py` (`sec_cik`).

## ⚠️ Requirements

- **Apify account** + API token ([get one free](https://console.apify.com/))
- Python 3.9+
- Apify compute units for the paid actors (free tier includes $5/month)

> On the Apify **free plan** the Twitter actor caps output at 10 items per run, and
> total actor memory is capped at 16 GB, so blog crawls run at 4 GB each.
