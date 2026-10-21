"""Background scheduler for `python main.py serve`.

Three recurring jobs (intervals and recipients come from config.py):

  news       every SCHEDULE_NEWS_HOURS — the news channels for every company
  companies  every SCHEDULE_COMPANIES_DAYS — a full scrape of every company
  people     every SCHEDULE_PEOPLE_DAYS — a full scrape of every person

Each job runs its targets one at a time through runner.py, so every run
shows up in Run history exactly like a manual one (tagged
trigger="schedule:<job>"), then sends ONE summary email for the whole
batch — successes and failures — to SCHEDULE_NOTIFY_TO.

Only one scheduled target runs at a time across both jobs: Apify rejects
more than a handful of concurrent actor runs on this plan, and a people
batch (~500 runs) overlapping a news batch would otherwise trip that.

Last-run times persist in output/scheduler_state.json, so a restart or
redeploy doesn't re-trigger a batch that just ran. On a brand-new
install the news and companies jobs run shortly after startup (only a
handful of companies, and the full run is what gives a fresh server its
non-news channel cards) and the first people batch is one interval
later — not hundreds of runs on first deploy.
"""
import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import config
from paths import OUTPUT_DIR

STATE_PATH = os.path.join(OUTPUT_DIR, "scheduler_state.json")
TICK_SECS = 60
STARTUP_DELAY_SECS = 120   # let the server settle before the first batch

_state_lock = threading.Lock()
_run_lock = threading.Lock()       # one scheduled target at a time, across jobs
_running = {}                      # job name -> {"started_at", "done", "total"}
_thread = None


def _jobs() -> dict:
    return {
        "news": {
            "label": "News fetch — every company",
            "kind": "company",
            "every_secs": config.SCHEDULE_NEWS_HOURS * 3600,
            "only": config.SCHEDULE_NEWS_CHANNELS,
            "limit": config.SCHEDULE_NEWS_LIMIT,
        },
        "companies": {
            "label": "Full run — every company",
            "kind": "company",
            "every_secs": config.SCHEDULE_COMPANIES_DAYS * 86400,
            "only": None,
            "limit": config.SCHEDULE_COMPANIES_LIMIT,
        },
        "people": {
            "label": "Full run — every person",
            "kind": "person",
            "every_secs": config.SCHEDULE_PEOPLE_DAYS * 86400,
            "only": None,
            "limit": config.SCHEDULE_PEOPLE_LIMIT,
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


def _update_state(job: str, **fields) -> None:
    with _state_lock:
        state = _load_state()
        state.setdefault(job, {}).update(fields)
        _save_state(state)


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
    """Runs one whole batch synchronously, then emails the summary.
    Safe to call from any thread; a job already in progress is skipped."""
    import runner

    job = _jobs()[name]
    with _state_lock:
        if name in _running:
            print(f"⏭️  [scheduler] {name} already running — skipped")
            return
        _running[name] = {"started_at": _now().isoformat(), "done": 0, "total": 0}
    started = _now()
    _update_state(name, last_started=started.isoformat())
    results, crash = [], None
    try:
        targets = _targets(job["kind"])
        _running[name]["total"] = len(targets)
        print(f"⏰ [scheduler] {name}: starting {len(targets)} {job['kind']} run(s)")
        for t in targets:
            with _run_lock:
                entry = runner.start(job["kind"], t, job["limit"], only=job["only"], trigger=f"schedule:{name}")
                updates = runner.execute(entry["run_id"], job["kind"], t, job["limit"], only=job["only"])
            results.append({"target": t, "run_id": entry["run_id"], **updates})
            _running[name]["done"] += 1
    except Exception:
        crash = traceback.format_exc()
        print(f"❌ [scheduler] {name} crashed:\n{crash}")
    finally:
        finished = _now()
        ok = sum(1 for r in results if r.get("success"))
        _update_state(
            name,
            last_finished=finished.isoformat(),
            last_ok=ok,
            last_failed=len(results) - ok,
            last_crash=(crash or "")[-2000:] or None,
        )
        with _state_lock:
            _running.pop(name, None)
        print(f"✅ [scheduler] {name}: {ok}/{len(results)} succeeded in {int((finished - started).total_seconds())}s")
        _notify(name, job, started, finished, results, crash)


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
def status() -> dict:
    """For GET /api/scheduler: per job, last/next run and live progress."""
    state = _load_state()
    out = {"enabled": config.SCHEDULER_ENABLED, "notify_to": config.SCHEDULE_NOTIFY_TO, "jobs": {}}
    for name, job in _jobs().items():
        s = state.get(name, {})
        nxt = None
        if s.get("last_started"):
            nxt = (datetime.fromisoformat(s["last_started"]) + timedelta(seconds=job["every_secs"])).isoformat()
        out["jobs"][name] = {
            "label": job["label"],
            "every_hours": job["every_secs"] / 3600,
            "channels": job["only"],
            "running": _running.get(name),
            "next_run": nxt,
            **{k: s.get(k) for k in ("last_started", "last_finished", "last_ok", "last_failed")},
        }
    return out


def trigger(name: str) -> bool:
    """Start a job now in its own thread (POST /api/scheduler/run).
    False if the name is unknown or it's already running."""
    if name not in _jobs() or name in _running:
        return False
    threading.Thread(target=run_job, args=(name,), daemon=True, name=f"sched-{name}-manual").start()
    return True


def _loop() -> None:
    time.sleep(STARTUP_DELAY_SECS)
    while True:
        try:
            state = _load_state()
            for name, job in _jobs().items():
                if name in _running or job["every_secs"] <= 0:
                    continue
                last = state.get(name, {}).get("last_started")
                if last is None and name == "people":
                    # Fresh install: start the clock instead of launching
                    # hundreds of person runs the moment it's deployed.
                    _update_state(name, last_started=_now().isoformat())
                    continue
                if last is None or (_now() - datetime.fromisoformat(last)).total_seconds() >= job["every_secs"]:
                    threading.Thread(target=run_job, args=(name,), daemon=True, name=f"sched-{name}").start()
        except Exception:
            print(f"⚠️  [scheduler] tick failed:\n{traceback.format_exc()}")
        time.sleep(TICK_SECS)


def start() -> bool:
    """Starts the loop once per process if SCHEDULER_ENABLED; returns whether it did."""
    global _thread
    if not config.SCHEDULER_ENABLED or _thread is not None:
        return False
    _thread = threading.Thread(target=_loop, daemon=True, name="scheduler")
    _thread.start()
    return True
