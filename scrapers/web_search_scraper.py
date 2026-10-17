"""General web search for a person — interviews, conference speaker pages,
company leadership bios, quotes in articles, their LinkedIn profile URL.

Free, no key, no Apify actor: uses DuckDuckGo's plain-HTML results page
(html.duckduckgo.com), the same endpoint its no-JavaScript site serves.
That page returns one page of results (~10) per query, so `limit` above
that has no effect. It is unofficial and rate-limited: requests are
spaced out process-wide (bulk re-runs fire several people at once), and
a block comes back as a clear channel error rather than silent zero.
"""
import asyncio
import html as html_lib
import re
import threading
import time
import urllib.parse
from typing import Any, Dict, List

import aiohttp

from config import DEFAULT_POST_LIMIT, TIMEOUTS
from .base_scraper import BaseScraper

SEARCH_URL = "https://html.duckduckgo.com/html/"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MIN_INTERVAL_SECS = 5.0

_pace_lock = threading.Lock()
_next_slot = 0.0

_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
    r'(?:.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>)?',
    re.S,
)
_TAG = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    return html_lib.unescape(_TAG.sub("", fragment or "")).strip()


def _real_url(href: str) -> str:
    """DuckDuckGo wraps results as //duckduckgo.com/l/?uddg=<real url>."""
    href = html_lib.unescape(href)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    if "uddg" in qs:
        return qs["uddg"][0]
    return "https:" + href if href.startswith("//") else href


async def _wait_for_slot() -> None:
    """Space requests MIN_INTERVAL_SECS apart across every thread/loop."""
    global _next_slot
    with _pace_lock:
        now = time.monotonic()
        slot = max(now, _next_slot)
        _next_slot = slot + MIN_INTERVAL_SECS
    if slot > now:
        await asyncio.sleep(slot - now)


class WebSearchScraper(BaseScraper):
    def __init__(self):
        super().__init__("web_search")

    async def scrape(self, query, limit: int = DEFAULT_POST_LIMIT) -> List[Dict[str, Any]]:
        """`query` may be a list of queries, most specific first — each is
        tried in turn until one returns results."""
        queries = [query] if isinstance(query, str) else list(query)
        result: List[Dict[str, Any]] = []
        for q in queries:
            result = await self._search(q, limit)
            if result and not result[0].get("error"):
                return result
            if result and "rate-limited" in (result[0].get("message") or ""):
                return result
        return result

    async def _search(self, query: str, limit: int, retry: bool = True) -> List[Dict[str, Any]]:
        try:
            return await self._search_once(query, limit)
        except aiohttp.ClientConnectionError as e:
            # A dropped connection (seen live: "network name is no longer
            # available") is usually a blip — one retry before giving up.
            if retry:
                await asyncio.sleep(3)
                return await self._search(query, limit, retry=False)
            print(f"❌ [Web Search] Error: {e}")
            return self._error_response(f"Could not reach DuckDuckGo: {e}")

    async def _search_once(self, query: str, limit: int) -> List[Dict[str, Any]]:
        try:
            print(f"🌐 [Web Search] Searching: {query}")
            await _wait_for_slot()
            timeout = aiohttp.ClientTimeout(total=TIMEOUTS["web_search"])
            async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": BROWSER_UA}) as session:
                async with session.post(SEARCH_URL, data={"q": query, "kl": "us-en"}) as resp:
                    body = await resp.text()
                    # 202 is how DuckDuckGo answers a request it's throttling.
                    if resp.status == 202:
                        return self._error_response("DuckDuckGo rate-limited this request — try again in a few minutes")
                    if resp.status != 200:
                        return self._error_response(f"DuckDuckGo returned HTTP {resp.status}")

            matches = _RESULT.findall(body)
            if not matches and ("anomaly" in body.lower() or "challenge" in body.lower()):
                return self._error_response("DuckDuckGo rate-limited this request — try again in a few minutes")

            posts, seen = [], set()
            for href, title, snippet in matches:
                url = _real_url(href)
                # Skip DuckDuckGo's own sponsored-result redirects.
                if not url.startswith("http") or "duckduckgo.com/y.js" in url or url in seen:
                    continue
                seen.add(url)
                title, snippet = _clean(title), _clean(snippet)
                posts.append(self._format_post(
                    rank=len(posts) + 1,
                    post_url=url,
                    text=f"{title} — {snippet}" if snippet else title,
                    author=urllib.parse.urlparse(url).netloc.replace("www.", ""),
                    extra={"title": title, "snippet": snippet, "query": query},
                ))
                if len(posts) >= limit:
                    break

            print(f"✅ [Web Search] {len(posts)} results")
            return posts
        except asyncio.TimeoutError:
            return self._error_response("Web search timed out")
        except aiohttp.ClientConnectionError:
            raise  # _search() retries these once
        except Exception as e:
            print(f"❌ [Web Search] Error: {e}")
            return self._error_response(str(e))
