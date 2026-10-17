"""Shared local-Playwright page fetcher.

Replaces Apify's Website Content Crawler actor for the "fetch one page,
extract readable article text + metadata" job that blog_scraper.py (and,
in a later migration pass, news_scraper.py / cxo_news_scraper.py /
bloomberg_scraper.py) needs — at zero per-run Apify cost, since the
browser runs on this machine instead of Apify's cloud.

Not yet live-tested: this environment has no network access to install
playwright/trafilatura or download the Chromium binary. Run
`pip install -r requirements.txt && playwright install chromium` on a
machine with network access, then sanity-check `fetch_article()` against
a couple of real target URLs (including a JS-heavy one) before relying
on it in production.

One Chromium instance is launched lazily per event loop and reused for
that loop's lifetime — launching a fresh browser per article would dwarf
the cost of the fetch itself. It must be per loop, not per process:
`serve` runs every /api/run in its own thread via asyncio.run(), i.e. a
fresh event loop each time, and Playwright objects are bound to the loop
that created them. Sharing one module-level browser across loops made
every run after the first hang forever on `new_context()` (awaiting a
future on the first run's already-closed loop) — runs stuck "running"
with no error. scrape_and_store() calls `close_browser()` in a finally
so each run's browser dies with its loop.
"""
import asyncio
import json
import threading
from typing import Any, Dict, Optional

import trafilatura
from playwright.async_api import Browser, Playwright, async_playwright

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Resources that never affect extracted article text but cost real load
# time — same intent as the old actor's blockMedia:True.
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

class _LoopState:
    def __init__(self) -> None:
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.lock = asyncio.Lock()


# Keyed by the running event loop — see module docstring for why this
# can't be a single process-wide browser.
_states: Dict[asyncio.AbstractEventLoop, _LoopState] = {}
_states_lock = threading.Lock()


def _state() -> _LoopState:
    loop = asyncio.get_running_loop()
    with _states_lock:
        # Drop entries for loops that have since closed (their browser
        # subprocess is gone with the loop's transports anyway).
        for dead in [l for l in _states if l.is_closed()]:
            del _states[dead]
        return _states.setdefault(loop, _LoopState())


async def _get_browser() -> Browser:
    st = _state()
    async with st.lock:
        if st.browser is None or not st.browser.is_connected():
            st.playwright = await async_playwright().start()
            # channel="chromium" pins this to the regular Chromium build
            # `playwright install chromium` downloads, instead of the
            # separate chrome-headless-shell package Playwright 1.45+
            # otherwise uses by default for headless:True — that package
            # needs its own download and is easy to end up missing (seen
            # live: "BrowserType.launch: Executable doesn't exist ...
            # chrome-headless-shell.exe" even right after `playwright
            # install chromium`).
            st.browser = await st.playwright.chromium.launch(headless=True, channel="chromium")
        return st.browser


async def close_browser() -> None:
    """Release the current event loop's browser/Playwright process, if any.
    Call before the loop ends (scrape_and_store does, in a finally)."""
    loop = asyncio.get_running_loop()
    with _states_lock:
        st = _states.pop(loop, None)
    if st is None:
        return
    async with st.lock:
        if st.browser is not None:
            try:
                await st.browser.close()
            except Exception:
                pass
        if st.playwright is not None:
            try:
                await st.playwright.stop()
            except Exception:
                pass


async def _block_non_content(route) -> None:
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        await route.abort()
    else:
        await route.continue_()


async def get_html(url: str, timeout_secs: int = 60) -> str:
    """Render a URL in a headless browser and return its final HTML."""
    browser = await _get_browser()
    context = await browser.new_context(user_agent=BROWSER_UA)
    page = await context.new_page()
    try:
        await page.route("**/*", _block_non_content)
        await page.goto(url, timeout=timeout_secs * 1000, wait_until="domcontentloaded")
        try:
            # Best-effort: pages with continuous polling/analytics never go
            # fully idle — domcontentloaded is already enough content.
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        return await page.content()
    finally:
        await context.close()


async def fetch_article(url: str, timeout_secs: int = 60) -> Dict[str, Any]:
    """Fetch a URL and extract its readable article text + metadata.

    Returns {"url", "markdown", "title", "author", "published_at"} — any
    field trafilatura can't find comes back as "". Raises on navigation
    failure (bad HTTP status, timeout); extraction failure just yields
    empty text rather than raising, the same best-effort behavior the
    Apify actor it replaces had.
    """
    html = await get_html(url, timeout_secs)

    markdown = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_comments=False,
        include_tables=False,
        with_metadata=False,
        favor_precision=True,
    ) or ""

    meta_json = trafilatura.extract(
        html,
        url=url,
        output_format="json",
        include_comments=False,
        include_tables=False,
        with_metadata=True,
        favor_precision=True,
    ) or "{}"
    try:
        meta = json.loads(meta_json)
    except ValueError:
        meta = {}

    return {
        "url": url,
        "markdown": markdown,
        "title": meta.get("title") or "",
        "author": meta.get("author") or "",
        "published_at": meta.get("date") or "",
    }
