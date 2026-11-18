"""The Apify API token the scrapers use, editable from the UI.

A token saved from the UI (POST /api/apify-token) overrides APIFY_TOKEN
from the environment / .env. It lives under output/ because that is the
one directory shared by the web container and the scheduler container
(docker-compose.yml), so a token changed in the UI is picked up by the
next run in either — scrapers read it when they're constructed, and
engine.py builds fresh scrapers for every run.

The file sits in output/.secrets/, and main.py's static handler refuses
any path with a dot-segment, so it is never served over /output/.
"""
import json
import os
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

import file_lock
from paths import OUTPUT_DIR

SECRETS_DIR = os.path.join(OUTPUT_DIR, ".secrets")
TOKEN_PATH = os.path.join(SECRETS_DIR, "apify.json")
_LOCK_PATH = TOKEN_PATH + ".lock"
_PLACEHOLDER = "YOUR_APIFY_TOKEN_HERE"
_API = "https://api.apify.com/v2"

load_dotenv()


def _saved() -> Dict[str, Any]:
    try:
        with open(TOKEN_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _env_token() -> str:
    token = (os.getenv("APIFY_TOKEN") or "").strip()
    return "" if token == _PLACEHOLDER else token


def get_token() -> str:
    """The token to run actors with: the UI-saved one, else the env one."""
    return (_saved().get("token") or "").strip() or _env_token()


def mask(token: str) -> str:
    if not token:
        return ""
    return token[:10] + "…" + token[-4:] if len(token) > 18 else "…" + token[-4:]


def status() -> Dict[str, Any]:
    """What the UI may see — never the token itself."""
    saved = _saved()
    token = get_token()
    return {
        "configured": bool(token),
        "source": "ui" if (saved.get("token") or "").strip() else ("env" if token else None),
        "masked": mask(token),
        "updated_at": saved.get("updated_at") if saved.get("token") else None,
    }


class ApifyCheckError(Exception):
    """The token couldn't be verified; message is fit to show the user."""


def check(token: Optional[str] = None) -> Dict[str, Any]:
    """Verify a token against Apify and return the account's usage this
    billing cycle. Uses the active token when none is given."""
    token = (token or "").strip() or get_token()
    if not token:
        raise ApifyCheckError("No Apify token is set.")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        me = requests.get(f"{_API}/users/me", headers=headers, timeout=15)
        if me.status_code in (401, 403):
            raise ApifyCheckError("Apify rejected this token — check it was copied in full.")
        me.raise_for_status()
        user = me.json().get("data") or {}
        lim = requests.get(f"{_API}/users/me/limits", headers=headers, timeout=15)
        lim.raise_for_status()
        limits = lim.json().get("data") or {}
    except ApifyCheckError:
        raise
    except requests.RequestException as e:
        raise ApifyCheckError(f"Could not reach Apify: {e}") from e

    used = (limits.get("current") or {}).get("monthlyUsageUsd")
    cap = (limits.get("limits") or {}).get("maxMonthlyUsageUsd")
    remaining = (cap - used) if isinstance(cap, (int, float)) and isinstance(used, (int, float)) else None
    cycle = limits.get("monthlyUsageCycle") or {}
    plan = user.get("plan") or {}
    return {
        "ok": True,
        "username": user.get("username"),
        "plan": plan.get("id") or plan.get("description"),
        "usage_usd": used,
        "limit_usd": cap,
        "remaining_usd": remaining,
        "cycle_start": cycle.get("startAt"),
        "cycle_end": cycle.get("endAt"),
        "masked": mask(token),
    }


def save_token(token: str) -> Dict[str, Any]:
    """Verify then store a token; an empty token clears the UI override
    (falling back to APIFY_TOKEN from the environment). Returns status()
    plus, when a token was saved, its balance."""
    from datetime import datetime, timezone

    token = (token or "").strip()
    balance = check(token) if token else None
    with file_lock.locked(_LOCK_PATH):
        os.makedirs(SECRETS_DIR, exist_ok=True)
        if not token:
            try:
                os.remove(TOKEN_PATH)
            except FileNotFoundError:
                pass
        else:
            tmp = TOKEN_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"token": token,
                           "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, fh)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, TOKEN_PATH)
    result = status()
    if balance:
        result["balance"] = balance
    return result
