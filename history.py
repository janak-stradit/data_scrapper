"""Append-only log of pipeline runs, read by the frontend's Run Pipeline page.

The JSON file is the source of truth (same reasoning as store.py: a
handful of runs a day, read by one local frontend, no concurrent writers
to coordinate) — record() also mirrors each entry to Postgres via db.py
when DATABASE_URL is set, best-effort, so run history is queryable there
too.
"""
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from paths import OUTPUT_DIR
import db

HISTORY_PATH = os.path.join(OUTPUT_DIR, "run_history.json")
MAX_ENTRIES = 200


def _load() -> List[Dict[str, Any]]:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []


def record(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Prepend one run's outcome to the history file, newest first.

    Returns the entry as stored (with `recorded_at` filled in), so callers
    can hand it straight back in an API response.
    """
    entry = {
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **entry,
    }
    entries = _load()
    entries.insert(0, entry)
    del entries[MAX_ENTRIES:]

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2, ensure_ascii=False)
    db.insert_run_history(entry)
    return entry
