"""Canonical output locations.

Everything the pipeline writes lives under output/ so the project root stays
code-only and the whole set of results can be archived or cleared in one move.
"""
import os

OUTPUT_DIR = "output"
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
