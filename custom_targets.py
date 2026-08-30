"""Sidecar store for targets saved from the Run Pipeline page's "Save as
Target" button.

targets.py and people_targets.py stay the hand-written catalog; this file
holds anything saved through the UI instead, in one JSON file with a
"companies" and a "people" section, merged into COMPANY_TARGETS /
PEOPLE_TARGETS at import time. Doesn't exist until the first save.
"""
import json
import os
from typing import Any, Dict

_PATH = os.path.join(os.path.dirname(__file__), "custom_targets.json")
_EMPTY: Dict[str, Dict[str, Dict[str, Any]]] = {"companies": {}, "people": {}}


def _load() -> Dict[str, Dict[str, Dict[str, Any]]]:
    if not os.path.exists(_PATH):
        return {"companies": {}, "people": {}}
    try:
        with open(_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"companies": {}, "people": {}}
    data.setdefault("companies", {})
    data.setdefault("people", {})
    return data


def load_section(section: str) -> Dict[str, Dict[str, Any]]:
    """The saved targets for one section ("companies" or "people")."""
    return _load().get(section, {})


def save(section: str, key: str, target: Dict[str, Any]) -> None:
    """Persist one target under `section`, overwriting any existing entry."""
    data = _load()
    data[section][key] = target
    with open(_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def delete(section: str, key: str) -> bool:
    """Remove one target from `section`. Returns whether it was present."""
    data = _load()
    if key not in data[section]:
        return False
    del data[section][key]
    with open(_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    return True
