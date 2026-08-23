"""Digest pipeline package.

Turns stored posts into a per-account sales email and per-channel storylines
for the social team.

    from digest import run
    digest = run("bny", new_only=True)
"""
from .llm_client import LLMClient, LLMError, describe_config
from .pipeline import run
from .prompts import CHANNEL_LABELS
from .renderer import render_markdown

__all__ = [
    "run",
    "render_markdown",
    "describe_config",
    "LLMClient",
    "LLMError",
    "CHANNEL_LABELS",
]
