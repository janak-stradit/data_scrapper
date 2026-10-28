"""Scheduled scrapes — three recurring jobs (intervals and recipients come
from config.py):

  news       every SCHEDULE_NEWS_HOURS — the news channels for every company
  companies  every SCHEDULE_COMPANIES_DAYS — a full scrape of every company
  people     every SCHEDULE_PEOPLE_DAYS — a full scrape of every person

Each job runs its targets one at a time through runner.py, so every run
shows up in Run history exactly like a manual one (tagged
trigger="schedule:<job>"), then sends ONE summary email for the whole
batch — successes and failures — to SCHEDULE_NOTIFY_TO.

Where it runs: in production, its own container (`python main.py
scheduler`, see docker-compose.yml), so redeploying or restarting the web
server never interrupts a batch. Locally, `python main.py serve` runs the
same loop in a background thread unless SCHEDULER_EXTERNAL=true.

Scheduled targets run one at a time per lane: the news job has its own
lane, companies and people share the other. So at most two scheduled
targets run at once — Apify rejects more than a handful of concurrent
actor runs on this plan — while a people batch (~500 runs, a day or
more) can no longer hold the 3-hourly news fetch back for its whole
duration, which a single shared lock did.

All state lives in output/scheduler_state.json, shared by both processes
under a file lock: last-run times (so a restart doesn't re-trigger a
batch that just ran), each in-flight batch's progress, "Run now" requests
from the web server (picked up within TICK_SECS), and a heartbeat the web
server reports to show the scheduler is alive. Stopping the scheduler
(SIGTERM, e.g. a deploy) finishes the target in progress, starts no new
one, and leaves the batch's progress on file — the next process resumes
it with the targets still to do instead of starting over.

On a brand-new install the news and companies jobs run shortly after
startup (only a handful of companies, and the full run is what gives a
fresh server its non-news channel cards) and the first people batch is
one interval later — not hundreds of runs on first deploy.
"""
import json
import os
import signal
import threading
import traceback
from datetime import datetime, timedelta, timezone

import config
import file_lock
from paths import OUTPUT_DIR

STATE_PATH = os.path.join(OUTPUT_DIR, "scheduler_state.json")
_STATE_LOCK_PATH = STATE_PATH + ".lock"
TICK_SECS = 10
STARTUP_DELAY_SECS = 120   # let the server settle before the first batch
# A heartbeat older than this means no scheduler process is running.
HEARTBEAT_STALE_SECS = 90

_state_lock = threading.Lock()
# One scheduled target at a time per lane (see module docstring).
_lane_locks = {"news": threading.Lock(), "full": threading.Lock()}
_active = set()                    # jobs with a live batch thread in THIS process
_batch_threads = []
_stopping = threading.Event()      # set on SIGTERM: finish current targets, start nothing new
_thread = None


def _jobs() -> dict:
    return {
        "news": {
            "label": "News fetch — every company",
            "kind": "company",
            "every_secs": config.SCHEDULE_NEWS_HOURS * 3600,
            "only": config.SCHEDULE_NEWS_CHANNELS,
            "limit": config.SCHEDULE_NEWS_LIMIT,
            "lane": "news",
        },
        "companies": {
            "label": "Full run — every company",
            "kind": "company",
            "every_secs": config.SCHEDULE_COMPANIES_DAYS * 86400,
            "only": None,
            "limit": config.SCHEDULE_COMPANIES_LIMIT,
            "lane": "full",
        },
        "people": {
            "label": "Full run — every person",
            "kind": "person",
            "every_secs": config.SCHEDULE_PEOPLE_DAYS * 86400,
            "only": None,
            "limit": config.SCHEDULE_PEOPLE_LIMIT,
            "lane": "full",
        },
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def _mutate_state(fn):
    """Read-modify-write the state file under both locks; returns fn's result."""
    with _state_lock, file_lock.locked(_STATE_LOCK_PATH):
        state = _load_state()
        result = fn(state)
        _save_state(state)
        return result


def _update_state(job: str, **fields) -> None:
    _mutate_state(lambda state: state.setdefault(job, {}).update(fields))


def _targets(kind: str) -> list:
    """Every registered target of one kind — same set the sidebar lists
    (targets.py/people_targets.py/custom targets, plus ones only in Postgres)."""
    from targets import COMPANY_TARGETS
    from people_targets import PEOPLE_TARGETS
    import db

    table = dict(PEOPLE_TARGETS if kind == "person" else COMPANY_TARGETS)
    for row in db.list_targets(kind):
        table.setdefault(row["key"], row["config"])
    return [dict(cfg, key=key, display_name=cfg.get("display_name") or key)
            for key, cfg in sorted(table.items())]


# ─── Running a batch ─────────────────────────────────────────────
def run_job(name: str) -> None:
    """Runs one whole batch synchronously, then emails the summary —
    or resumes the batch a previous process left unfinished. Returns
    early, leaving the batch on file to resume, if the scheduler is
    stopping. Safe to call from any thread; a job already running in
    this process is skipped."""
    import runner

    job = _jobs()[name]
    with _state_lock:
        if name in _active:
            print(f"⏭️  [scheduler] {name} already running — skipped")
            return
        _active.add(name)
    try:
        def begin(state):
            s = state.setdefault(name, {})
            s["requested"] = None
            if not s.get("batch"):
                s["batch"] = {"started_at": _now().isoformat(), "total": 0, "done": [], "results": []}
                s["last_started"] = s["batch"]["started_at"]
            return s["batch"]

        batch = _mutate_state(begin)
        started = datetime.fromisoformat(batch["started_at"])
        results, crash = list(batch["results"]), None
        try:
            targets = _targets(job["kind"])
            done = set(batch["done"])
            todo = [t for t in targets if t["key"] not in done]
            _mutate_state(lambda state: state[name]["batch"].update(total=len(targets)))
            print(f"⏰ [scheduler] {name}: {'resuming' if done else 'starting'} — "
                  f"{len(todo)} of {len(targets)} {job['kind']} run(s) to go")
            for t in todo:
                if _stopping.is_set():
                    break
                with _lane_locks[job["lane"]]:
                    if _stopping.is_set():
                        break
                    entry = runner.start(job["kind"], t, job["limit"], only=job["only"], trigger=f"schedule:{name}")
                    updates = runner.execute(entry["run_id"], job["kind"], t, job["limit"], only=job["only"])
                row = {"target": {"key": t["key"], "display_name": t["display_name"]},
                       "run_id": entry["run_id"], **updates}
                results.append(row)

                def record(state, key=t["key"], row=row):
                    state[name]["batch"]["done"].append(key)
                    state[name]["batch"]["results"].append(row)
                _mutate_state(record)
        except Exception:
            crash = traceback.format_exc()
            print(f"❌ [scheduler] {name} crashed:\n{crash}")

        if _stopping.is_set() and not crash:
            print(f"⏸️  [scheduler] {name}: stopping — {len(results)} done, the rest resume on restart")
            return

        finished = _now()
        ok = sum(1 for r in results if r.get("success"))

        def finish(state):
            s = state.setdefault(name, {})
            s.pop("batch", None)
            s.update(
                last_finished=finished.isoformat(),
                last_ok=ok,
                last_failed=len(results) - ok,
                last_crash=(crash or "")[-2000:] or None,
            )
        _mutate_state(finish)
        print(f"✅ [scheduler] {name}: {ok}/{len(results)} succeeded in {int((finished - started).total_seconds())}s")
        _notify(name, job, started, finished, results, crash)
    finally:
        with _state_lock:
            _active.discard(name)


def _notify(name, job, started, finished, results, crash) -> None:
    to = config.SCHEDULE_NOTIFY_TO
    if not to:
        return
    from mailer import send_email, MailerError

    subject, text, html = build_summary(name, job, started, finished, results, crash)
    try:
        send_email(to, subject, text, html=html)
        print(f"📧 [scheduler] {name} summary sent to {to}")
    except MailerError as e:
        print(f"⚠️  [scheduler] could not email {name} summary: {e}")


def build_summary(name, job, started, finished, results, crash):
    """(subject, plain text, html) for one finished batch."""
    from html import escape as esc

    ok = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]
    partial = [r for r in ok if r.get("platforms_failed")]
    new_posts = sum(r.get("new_posts") or 0 for r in ok)
    cost = sum(r.get("cost_usd") or 0 for r in results)
    mins = max(1, round((finished - started).total_seconds() / 60))

    status = "CRASHED" if crash else ("FAILED" if failed and not ok else ("partly failed" if failed else "succeeded"))
    subject = (f"[Scraper] {job['label']} {status} — {len(ok)}/{len(results)} ok, "
               f"{new_posts} new posts")

    def channels(r):
        return ", ".join(r.get("platforms_failed") or [])

    lines = [
        f"{job['label']}: {status}",
        f"Started {started:%Y-%m-%d %H:%M} UTC, took {mins} min",
        f"{len(ok)} succeeded, {len(failed)} failed, {len(partial)} with failed channels",
        f"{new_posts} new posts, Apify cost ${cost:.4f}",
        "",
    ]
    if crash:
        lines += ["The batch stopped early with an error:", crash[-1500:], ""]
    if failed:
        lines.append("FAILED:")
        lines += [f"  - {r['target']['display_name']}: {r.get('error') or 'unknown error'}" for r in failed]
        lines.append("")
    if partial:
        lines.append("Succeeded, but some channels failed:")
        lines += [f"  - {r['target']['display_name']}: {channels(r)}" for r in partial]
        lines.append("")
    lines.append("All targets:")
    lines += [f"  {'OK ' if r.get('success') else 'ERR'} {r['target']['display_name']}"
              f" — {r.get('new_posts') or 0} new / {r.get('total_posts') or 0} total" for r in results]
    text = "\n".join(lines)

    color = "#c62828" if (crash or (failed and not ok)) else ("#b45309" if failed or partial else "#15803d")
    rows = "".join(
        "<tr>"
        f"<td style='padding:6px 10px;border-top:1px solid #eef0f4;'>{esc(r['target']['display_name'])}</td>"
        f"<td style='padding:6px 10px;border-top:1px solid #eef0f4;color:{'#15803d' if r.get('success') else '#c62828'};font-weight:600;'>"
        f"{'ok' if r.get('success') else 'failed'}</td>"
        f"<td style='padding:6px 10px;border-top:1px solid #eef0f4;text-align:right;'>{r.get('new_posts') or 0} / {r.get('total_posts') or 0}</td>"
        f"<td style='padding:6px 10px;border-top:1px solid #eef0f4;color:#8a94a6;font-size:12px;'>"
        f"{esc(r.get('error') or (('channels failed: ' + channels(r)) if r.get('platforms_failed') else ''))}</td>"
        "</tr>"
        # Failures first, then the rest in their original order.
        for r in sorted(results, key=lambda r: (bool(r.get("success")), bool(not r.get("platforms_failed"))))
    )
    html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f5f6f8;padding:24px;">
<table role="presentation" width="100%" style="max-width:720px;margin:0 auto;background:#fff;border-radius:12px;border-collapse:separate;overflow:hidden;">
<tr><td style="padding:20px 24px;border-top:4px solid {color};">
  <div style="font-size:12px;color:#8a94a6;text-transform:uppercase;letter-spacing:.06em;">Scheduled run · {esc(name)}</div>
  <div style="font-size:20px;font-weight:700;color:#101828;margin-top:4px;">{esc(job['label'])} {esc(status)}</div>
  <div style="font-size:14px;color:#475467;margin-top:8px;">
    <b>{len(ok)}</b> succeeded · <b style="color:{'#c62828' if failed else '#475467'};">{len(failed)}</b> failed ·
    <b>{len(partial)}</b> with failed channels · <b>{new_posts}</b> new posts · ${cost:.4f} Apify ·
    {mins} min (started {started:%Y-%m-%d %H:%M} UTC)
  </div>
  {f'<pre style="white-space:pre-wrap;background:#fdecec;color:#c62828;padding:10px;border-radius:8px;font-size:12px;">{esc(crash[-1500:])}</pre>' if crash else ''}
</td></tr>
<tr><td style="padding:0 14px 16px;">
  <table width="100%" style="border-collapse:collapse;font-size:13px;color:#101828;">
    <tr style="color:#8a94a6;font-size:12px;text-align:left;"><th style="padding:6px 10px;">Target</th><th style="padding:6px 10px;">Status</th>
    <th style="padding:6px 10px;text-align:right;">New / total</th><th style="padding:6px 10px;">Notes</th></tr>
    {rows or '<tr><td colspan="4" style="padding:10px;color:#8a94a6;">No targets were run.</td></tr>'}
  </table>
</td></tr></table></div>"""
    return subject, text, html


# ─── Scheduling loop ─────────────────────────────────────────────
def _alive(state: dict) -> bool:
    beat = state.get("_heartbeat")
    if not beat:
        return False
    return (_now() - datetime.fromisoformat(beat)).total_seconds() < HEARTBEAT_STALE_SECS


def status() -> dict:
    """For GET /api/scheduler: per job, last/next run and live progress —
    read from the shared state file, so it's right whichever process
    (this one or the scheduler container) is running the jobs."""
    state = _load_state()
    out = {
        "enabled": config.SCHEDULER_ENABLED,
        "alive": _alive(state),
        "notify_to": config.SCHEDULE_NOTIFY_TO,
        "jobs": {},
    }
    for name, job in _jobs().items():
        s = state.get(name, {})
        nxt = None
        if s.get("last_started"):
            nxt = (datetime.fromisoformat(s["last_started"]) + timedelta(seconds=job["every_secs"])).isoformat()
        running = None
        if s.get("batch"):
            b = s["batch"]
            running = {"started_at": b["started_at"], "done": len(b["done"]), "total": b["total"]}
        elif s.get("requested"):
            running = {"started_at": s["requested"], "done": 0, "total": 0, "queued": True}
        out["jobs"][name] = {
            "label": job["label"],
            "every_hours": job["every_secs"] / 3600,
            "channels": job["only"],
            "running": running,
            "next_run": nxt,
            **{k: s.get(k) for k in ("last_started", "last_finished", "last_ok", "last_failed")},
        }
    return out


def trigger(name: str) -> bool:
    """Queue a job to start now (POST /api/scheduler/run) — whichever
    process runs the loop picks it up within TICK_SECS. False if the name
    is unknown or it's already running or queued."""
    if name not in _jobs():
        return False

    def request(state):
        s = state.setdefault(name, {})
        if s.get("batch") or s.get("requested"):
            return False
        s["requested"] = _now().isoformat()
        return True
    return _mutate_state(request)


def _start_batch(name: str) -> None:
    t = threading.Thread(target=run_job, args=(name,), daemon=True, name=f"sched-{name}")
    _batch_threads.append(t)
    t.start()


def _heartbeat(state: dict) -> dict:
    state["_heartbeat"] = _now().isoformat()
    return dict(state)


def _tick() -> None:
    state = _mutate_state(_heartbeat)
    for name, job in _jobs().items():
        if name in _active:
            continue
        s = state.get(name, {})
        # An unfinished batch (the previous process stopped mid-way) or a
        # "Run now" request starts regardless of the timetable.
        if s.get("batch") or s.get("requested"):
            _start_batch(name)
            continue
        if not config.SCHEDULER_ENABLED or job["every_secs"] <= 0:
            continue
        last = s.get("last_started")
        if last is None and name == "people":
            # Fresh install: start the clock instead of launching
            # hundreds of person runs the moment it's deployed.
            _update_state(name, last_started=_now().isoformat())
            continue
        if last is None or (_now() - datetime.fromisoformat(last)).total_seconds() >= job["every_secs"]:
            _start_batch(name)


def _loop() -> None:
    if _stopping.wait(STARTUP_DELAY_SECS):
        return
    while not _stopping.is_set():
        try:
            _tick()
        except Exception:
            print(f"⚠️  [scheduler] tick failed:\n{traceback.format_exc()}")
        _stopping.wait(TICK_SECS)


def start() -> bool:
    """Starts the loop in a background thread of this process (`serve`),
    once — unless SCHEDULER_EXTERNAL, where the scheduler container runs
    it instead. Runs even with SCHEDULER_ENABLED off, so "Run now" and
    resuming an interrupted batch still work; the timetable itself only
    fires when enabled. Returns whether it started."""
    global _thread
    if config.SCHEDULER_EXTERNAL or _thread is not None:
        return False
    _thread = threading.Thread(target=_loop, daemon=True, name="scheduler")
    _thread.start()
    return True


def run_forever() -> int:
    """`python main.py scheduler` — the scheduler container's whole job.
    SIGTERM/SIGINT stop it gracefully: no new targets start, the ones in
    progress finish (runner.py bounds each at RUN_HARD_TIMEOUT_SECS),
    unfinished batches stay on file to resume, then it exits."""
    import history

    def stop(signum, _frame):
        if not _stopping.is_set():
            print(f"🛑 [scheduler] signal {signum} — finishing the target(s) in progress, then exiting")
        _stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    reaped = history.reap_orphaned_running(scope="scheduled")
    if reaped:
        print(f"⚠️  [scheduler] marked {reaped} scheduled run(s) left \"running\" by the previous process as interrupted")
    print(f"⏰ [scheduler] {'ON' if config.SCHEDULER_ENABLED else 'timetable OFF (Run now only)'}: "
          f"news every {config.SCHEDULE_NEWS_HOURS:g}h, companies every {config.SCHEDULE_COMPANIES_DAYS:g}d, "
          f"people every {config.SCHEDULE_PEOPLE_DAYS:g}d, summaries to {config.SCHEDULE_NOTIFY_TO or '(nobody)'}")

    _loop()
    for t in list(_batch_threads):
        t.join()
    print("👋 [scheduler] stopped")
    return 0
