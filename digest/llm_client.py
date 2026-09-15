"""Provider-agnostic LLM client.

The digest pipeline only needs one operation — send a prompt, get text back —
so each provider is a small adapter behind `LLMClient.complete()`. Pick the
provider with `LLM_PROVIDER` in .env; each reads its own API key.

Supported: anthropic (default), openai, openrouter, ollama (local, no key), dry-run.
"""
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        "key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-5",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        # OpenRouter mirrors each provider's own model ids under a
        # "<provider>/" prefix — see https://openrouter.ai/models.
        "default_model": "anthropic/claude-sonnet-5",
    },
    "ollama": {
        "url": os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat"),
        "key_env": None,
        "default_model": "llama3.1",
    },
}

# Per-channel calls (extract facts from one channel's posts) run far more
# often than the one email-rollup call per digest and need less judgment —
# a lighter/faster model handles that extraction fine, so only the email
# synthesis step pays for the flagship model. No cheaper tier exists for
# ollama (already local/free), so it keeps its one model.
_CHEAP_MODELS: Dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "openrouter": "anthropic/claude-haiku-4.5",
}


def channel_model(provider: str = None) -> str:
    """Which model per-channel summarisation should use.

    CHANNEL_LLM_MODEL in .env overrides this outright; otherwise falls back
    to the cheap tier for the active provider, or that provider's normal
    default if it has no cheap tier (e.g. ollama).
    """
    provider = (provider or PROVIDER).lower()
    override = os.getenv("CHANNEL_LLM_MODEL")
    if override:
        return override
    return _CHEAP_MODELS.get(provider) or PROVIDERS.get(provider, {}).get("default_model")


class LLMError(RuntimeError):
    """Raised when the provider rejects a request or is not configured."""


class LLMClient:
    """One `complete()` call, whichever provider is configured."""

    def __init__(
        self,
        provider: str = None,
        model: str = None,
        max_tokens: int = 8000,
        timeout: int = 240,
    ):
        self.provider = (provider or PROVIDER).lower()
        if self.provider not in PROVIDERS and self.provider != "dry-run":
            raise LLMError(
                f"Unknown LLM_PROVIDER '{self.provider}'. "
                f"Choose from: {', '.join(PROVIDERS)}, dry-run"
            )

        cfg = PROVIDERS.get(self.provider, {})
        self.url = cfg.get("url")
        self.model = model or os.getenv("LLM_MODEL") or cfg.get("default_model")
        self.max_tokens = max_tokens
        self.timeout = timeout

        key_env = cfg.get("key_env")
        self.api_key = os.getenv(key_env) if key_env else None
        self.configured = self.provider == "dry-run" or not key_env or bool(self.api_key)
        self.missing_key_env = key_env if (key_env and not self.api_key) else None

        # Running totals across every complete()/complete_json() call made
        # through this instance — a digest makes several calls per channel
        # plus one email rollup call, all reusing the same client, so this
        # is how pipeline.py reads back the whole digest's token spend.
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    # ── Public API ───────────────────────────────────────────────

    def complete(self, system: str, user: str) -> str:
        """Send one prompt, return the model's text."""
        if self.provider == "dry-run" or not self.configured:
            return self._dry_run(system, user)

        body, headers = self._build_request(system, user)
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise LLMError(f"{self.provider} returned HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"Could not reach {self.provider}: {e.reason}") from e
        except TimeoutError as e:
            # Newer Python raises a bare TimeoutError from the socket layer
            # for a slow read, rather than wrapping it in URLError — a large
            # max_tokens response on a busy channel can legitimately take
            # longer than the timeout to finish streaming. Without this,
            # the exception was uncaught and crashed the whole `digest --all`
            # run instead of just skipping this one channel.
            raise LLMError(f"{self.provider} timed out after {self.timeout}s") from e

        # OpenRouter (routing across upstream providers) sometimes answers
        # with HTTP 200 and an {"error": ...} body instead of "choices" —
        # e.g. no upstream provider currently available for a free model.
        # Left unchecked this raised a raw KeyError from _extract_text,
        # uncaught by pipeline.py's `except LLMError`, which crashed the
        # whole `digest` run instead of just this one channel/email call.
        if isinstance(payload, dict) and payload.get("error"):
            raise LLMError(f"{self.provider} returned an error: {payload['error']}")

        # Best-effort — an unexpected payload shape here shouldn't break the
        # actual response, just leave this call's tokens uncounted.
        try:
            usage = self._extract_usage(payload)
            self.total_input_tokens += usage["input_tokens"]
            self.total_output_tokens += usage["output_tokens"]
        except Exception:
            pass

        try:
            return self._extract_text(payload)
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"{self.provider} returned an unexpected response shape: {payload}") from e

    def complete_json(self, system: str, user: str) -> Dict[str, Any]:
        """Same as complete(), but parse the reply as JSON."""
        raw = self.complete(system, user).strip()

        # Models often wrap JSON in a ```json fence.
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            raw = raw[4:].lstrip() if raw.lower().startswith("json") else raw

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(raw[start : end + 1])
                except json.JSONDecodeError:
                    pass
            raise LLMError(f"Model did not return JSON. Got: {raw[:300]}")

    # ── Provider adapters ────────────────────────────────────────

    def _build_request(self, system: str, user: str):
        if self.provider == "anthropic":
            return (
                {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
                {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            )

        if self.provider in ("openai", "openrouter"):
            return (
                {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                {"Authorization": f"Bearer {self.api_key}"},
            )

        # ollama
        return (
            {
                "model": self.model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            {},
        )

    def _extract_text(self, payload: Dict[str, Any]) -> str:
        if self.provider == "anthropic":
            blocks = payload.get("content", [])
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if self.provider in ("openai", "openrouter"):
            return payload["choices"][0]["message"]["content"]
        return payload.get("message", {}).get("content", "")

    def _extract_usage(self, payload: Dict[str, Any]) -> Dict[str, int]:
        """Token counts this one call reports, in each provider's own
        response shape — 0/0 for whichever fields a given response omits,
        never raises (the caller already wraps this best-effort)."""
        if self.provider == "anthropic":
            u = payload.get("usage") or {}
            return {"input_tokens": u.get("input_tokens") or 0, "output_tokens": u.get("output_tokens") or 0}
        if self.provider in ("openai", "openrouter"):
            u = payload.get("usage") or {}
            return {"input_tokens": u.get("prompt_tokens") or 0, "output_tokens": u.get("completion_tokens") or 0}
        # ollama
        return {"input_tokens": payload.get("prompt_eval_count") or 0, "output_tokens": payload.get("eval_count") or 0}

    # ── Offline fallback ─────────────────────────────────────────

    def _dry_run(self, system: str, user: str) -> str:
        """Deterministic stand-in so the pipeline is testable without a key.

        Returns valid JSON in whichever shape the caller asked for, built from
        the lines in the prompt and clearly marked, so it can never be mistaken
        for model output.
        """
        tag = "[DRY RUN — no LLM configured]"
        lines = [
            l.strip("- ").strip()
            for l in user.splitlines()
            if l.strip().startswith("-") and len(l.strip()) > 12
        ]
        top = lines[:5] or ["(no posts supplied)"]

        if "briefing emails" in system:
            digest_noun = "contact digest" if "individual contact" in system else "account digest"
            return json.dumps(
                {
                    "dry_run": True,
                    "subject": f"{tag} {digest_noun}",
                    "body": (
                        f"{tag} No API key is configured, so this email was not "
                        "generated by a model.\n\n"
                        "Set LLM_PROVIDER and the matching API key in .env, then "
                        "re-run digest.py to produce the real briefing."
                    ),
                    "talking_points": [
                        {"point": f"{tag} {t[:120]}", "source_url": "", "channel": "n/a"}
                        for t in top[:3]
                    ],
                    "capability_opportunities": [],
                    "priority": "low",
                    "priority_reason": f"{tag} placeholder, not a real assessment.",
                    "confidence": "low",
                    "data_gaps": [f"{tag} no model was called; set an API key."],
                }
            )

        return json.dumps(
            {
                "dry_run": True,
                "observed": [
                    {"fact": f"{tag} {t[:150]}", "source_url": ""} for t in top[:3]
                ],
                "interpretation": f"{tag} no model was called, so nothing was inferred.",
                "evidence_strength": "weak",
                "evidence_note": f"{tag} placeholder output.",
                "summary": f"{tag} {len(lines)} posts supplied. "
                f"Most recent: {top[0][:200]}",
                "themes": [t[:120] for t in top[:3]],
                "sales_angle": f"{tag} set an API key to generate real analysis.",
                "notable_posts": [
                    {"headline": t[:120], "source_url": "", "why": tag} for t in top[:3]
                ],
                "capability_matches": [],
                "storyline": {
                    "hook": f"{tag} {top[0][:120]}",
                    "angle": f"{tag} set an API key to generate real analysis.",
                    "post_ideas": [t[:120] for t in top[:3]],
                    "suggested_tone": "n/a",
                },
            }
        )


def describe_config() -> str:
    """One-line description of what the pipeline will actually use."""
    client = LLMClient()
    if client.provider == "dry-run":
        return "dry-run (no calls made)"
    if not client.configured:
        return (
            f"{client.provider}/{client.model} — {client.missing_key_env} not set, "
            "falling back to dry-run"
        )
    return f"{client.provider}/{client.model}"
