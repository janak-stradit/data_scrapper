"""Append-only log of pipeline runs, read by the frontend's Run Pipeline
and Pipeline pages.

The JSON file is the source of truth (same reasoning as store.py: a
handful of runs a day, read by one local frontend, no concurrent writers
to coordinate beyond this process's own threads) — every write also
mirrors to Postgres via db.py when DATABASE_URL is set, best-effort, so
run history is queryable there too.

start()/finish() exist because main.py's /api/run and /api/digest now run
the actual scrape/digest work in a background thread instead of blocking
the HTTP request for however long a run takes (observed up to several
minutes) — start() records a "running" placeholder the instant a run is
kicked off, so the frontend can show it in-flight; finish() updates that
same entry in place once the background thread completes. record() stays
for the synchronous CLI path (python main.py scrape/digest/run), which
has no "in flight" state worth showing since it already blocks the
terminal until done.

_LOCK guards read-modify-write on the JSON file — necessary now that
multiple background threads can call start()/finish() concurrently;
without it, two overlapping writes could each read a stale list and
clobber the other's entry.
"""
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from paths import OUTPUT_DIR
import db

HISTORY_PATH = os.path.join(OUTPUT_DIR, "run_history.json")
MAX_ENTRIES = 200

_LOCK = threading.Lock()


def _load() -> List[Dict[str, Any]]:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []


def _save(entries: List[Dict[str, Any]]) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2, ensure_ascii=False)


def load_all() -> List[Dict[str, Any]]:
    """Every stored run, newest first — used by the paginated
    GET /api/run-history endpoint (and any other read-only consumer that
    wants the full list rather than one page of it)."""
    return _load()


def record(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Prepend one run's already-finished outcome to the history file,
    newest first — the synchronous, one-shot path (CLI).

    Returns the entry as stored (with `recorded_at` filled in), so callers
    can hand it straight back in an API response.
    """
    entry = {
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **entry,
    }
    with _LOCK:
        entries = _load()
        entries.insert(0, entry)
        del entries[MAX_ENTRIES:]
        _save(entries)
    db.insert_run_history(entry)
    return entry


def start(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Record a run as "running" the moment it's kicked off, before any
    scraping/digest work happens — the async path (HTTP API). Stamps a
    fresh `run_id` finish() later matches on to find and update this same
    entry, plus `status: "running"`.

    Returns the stored entry (with `run_id`/`recorded_at` filled in) —
    the caller hands `run_id` back to the client immediately so it can
    poll for completion instead of blocking on the HTTP response.
    """
    entry = {
        "run_id": uuid.uuid4().hex,
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "running",
        **entry,
    }
    with _LOCK:
        entries = _load()
        entries.insert(0, entry)
        del entries[MAX_ENTRIES:]
        _save(entries)
    db.insert_run_history(entry)
    return entry


def reap_orphaned_running() -> int:
    """Mark every "running" entry as interrupted — call once at server
    startup, before any new run can start.

    A "running" entry only exists because start() wrote it and the
    matching finish() hasn't run yet; if the process is only just
    starting, that background thread cannot possibly still be alive to
    finish it later (killing the server — a crash, a restart, Ctrl+C —
    kills its threads with it). Left alone, such an entry stays
    "running" forever, which a poller (pollRunStatus in the frontend)
    would wait on until it times out, and which permanently misrenders
    in the history table. Returns how many entries were reaped.
    """
    with _LOCK:
        entries = _load()
        reaped = 0
        for e in entries:
            if e.get("status") == "running":
                e["status"] = "failed"
                e["success"] = False
                e["error"] = "Interrupted — server restarted (or crashed) while this run was in progress"
                reaped += 1
        if reaped:
            _save(entries)
    return reaped


def finish(run_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update a previously start()-ed entry in place with its final
    outcome (status/success/counts/digest/error/duration) once the
    background thread doing the actual work completes.

    Returns the updated entry, or None if it's aged out of the last
    MAX_ENTRIES already (e.g. a lot of other runs happened while this one
    was in flight) — rare, and not worth raising over; the caller still
    has the full result to respond with even if history couldn't be
    updated.
    """
    with _LOCK:
        entries = _load()
        for e in entries:
            if e.get("run_id") == run_id:
                e.update(updates)
                _save(entries)
                db.insert_run_history(e)
                return e
    return None
