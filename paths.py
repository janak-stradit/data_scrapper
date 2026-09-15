"""Canonical output locations.

Everything the pipeline writes lives under output/ so the project root stays
code-only and the whole set of results can be archived or cleared in one move.
"""
import os

# Anchored to this file's own directory, not the process's cwd — so
# `output/` always resolves inside this project even when it's nested
# under another app's root (e.g. apps/content_pipeline/ inside a
# monorepo) and run from a different working directory. main.py's serve
# command reuses PROJECT_ROOT for the same reason, to anchor the static
# file server instead of trusting cwd.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
STORE_DIR = os.path.join(OUTPUT_DIR, "stores")
DIGEST_DIR = os.path.join(OUTPUT_DIR, "digests")


def store_path(company_key: str) -> str:
    """Where a company's accumulated posts are stored."""
    os.makedirs(STORE_DIR, exist_ok=True)
    return os.path.join(STORE_DIR, f"{company_key}_output.json")


def digest_path(company_key: str, extension: str = "json") -> str:
    """Where a company's generated digest is written."""
    os.makedirs(DIGEST_DIR, exist_ok=True)
    return os.path.join(DIGEST_DIR, f"{company_key}_digest.{extension}")
