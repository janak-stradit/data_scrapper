"""Bloomberg news scraper — free discovery only, no login.

Bloomberg's own site has no public per-company search RSS, so discovery
reuses the same Google News RSS endpoint news_scraper.py uses, scoped to
Bloomberg's domain with a `site:bloomberg.com` search term. That's free,
no token, no actor.

Deliberately does NOT log into a Bloomberg account: Bloomberg's Terms of
Use prohibit automated/bot access, even for a paying subscriber, and an
automated login session risks the account itself getting flagged or
locked. So most article bodies stay paywalled — full-text extraction
below is the same best-effort pass news_scraper.py runs (local headless
browser via page_fetcher.py, zero Apify cost), and for Bloomberg specifically
expect a low fill rate: `_looks_blocked` will treat most paywalled pages
as blocked and the post just keeps its free RSS headline/summary, which
is still a real signal same as a blocked article anywhere else in this
pipeline.
"""
import asyncio
import re
from typing import Any, Dict, List
from urllib.parse import quote
from xml.etree import ElementTree

import aiohttp

from config import DEFAULT_POST_LIMIT, NEWS_LOCALE, TIMEOUTS
from . import page_fetcher
from .base_scraper import BaseScraper

RSS_URL = (
    "https://news.google.com/rss/search"
    "?q={query}&hl={hl}&gl={gl}&ceid={gl}:{hl_short}"
)

TAG_RE = re.compile(r"<[^>]+>")
NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

# Same "this is a block/consent/challenge wall, not the article" heuristics
# news_scraper.py uses — Bloomberg's own paywall interstitial is what this
# mostly catches here.
_BLOCKED_MARKERS = (
    "attention required", "enable javascript", "checking your browser",
    "verify you are human", "just a moment", "cloudflare",
    "before you continue", "we use cookies and data", "access denied",
    "are you a robot", "unusual traffic", "subscribe to continue",
    "subscriber exclusive", "sign in to continue",
)

DEFAULT_EXCLUSIONS = ""


class BloombergScraper(BaseScraper):
    """Fetches recent Bloomberg articles mentioning a company, via Google News RSS."""

    def __init__(self):
        super().__init__("bloomberg")

    @staticmethod
    def _looks_blocked(title: str, markdown: str) -> bool:
        combined = f"{title} {markdown[:500]}".lower()
        if any(marker in combined for marker in _BLOCKED_MARKERS):
            return True
        return len(markdown.strip()) < 200

    async def _fetch_full_content(self, posts: List[Dict[str, Any]]) -> None:
        """Best-effort: replace each post's text with its real article
        content where Bloomberg's paywall allows it. Mutates `posts` in
        place; never raises — most Bloomberg articles will stay
        headline-only, which is expected, not an error.
        """
        if not posts:
            return

        sem = asyncio.Semaphore(4)

        async def _fetch_one(post: Dict[str, Any]) -> None:
            url = post.get("post_url")
            if not url:
                return
            async with sem:
                try:
                    item = await page_fetcher.fetch_article(url, TIMEOUTS["bloomberg_content"])
                except Exception:
                    return
            if self._looks_blocked(item["title"], item["markdown"]):
                return
            post["text"] = item["markdown"][:5000]
            post["full_content"] = True

        await asyncio.gather(*[_fetch_one(p) for p in posts])

        filled = sum(1 for p in posts if p.get("full_content"))
        print(f"   [Bloomberg] Full content: {filled}/{len(posts)} articles (rest paywalled/headline-only)")

    @staticmethod
    def _key(text: str) -> str:
        return NORMALIZE_RE.sub("", (text or "").lower())

    @staticmethod
    def _clean(html: str) -> str:
        return TAG_RE.sub("", html or "").replace("&nbsp;", " ").strip()

    async def scrape(
        self,
        query: str,
        limit: int = DEFAULT_POST_LIMIT,
        days: int = 30,
        exclusions: str = DEFAULT_EXCLUSIONS,
    ) -> List[Dict[str, Any]]:
        """
        Fetch recent Bloomberg articles for a search query.

        Args:
            query: Search query, e.g. '"Northern Trust"'
            limit: Number of articles to return
            days: Only include articles from the last N days
            exclusions: Negative search terms appended to the query

        Returns:
            List of standardized post dictionaries, newest first
        """
        try:
            full_query = " ".join(
                part for part in (
                    "site:bloomberg.com", query, exclusions,
                    f"when:{days}d" if days else "",
                )
                if part
            )
            print(f"🟧 [Bloomberg] Starting fetch for: {full_query}")

            hl = NEWS_LOCALE["hl"]
            url = RSS_URL.format(
                query=quote(full_query),
                hl=hl,
                gl=NEWS_LOCALE["gl"],
                hl_short=hl.split("-")[0],
            )

            timeout = aiohttp.ClientTimeout(total=TIMEOUTS["bloomberg"])
            headers = {"User-Agent": "Mozilla/5.0 (compatible; ScraperEngine/1.0)"}

            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return self._error_response(
                            f"Google News returned HTTP {resp.status}"
                        )
                    body = await resp.text()

            root = ElementTree.fromstring(body)
            items = root.findall("./channel/item")

            posts = []
            for item in items:
                if len(posts) >= limit:
                    break
                title = (item.findtext("title") or "").strip()
                source_el = item.find("source")
                source = (source_el.text or "").strip() if source_el is not None else ""
                if source and title.endswith(f" - {source}"):
                    title = title[: -len(f" - {source}")].strip()

                summary = self._clean(item.findtext("description"))
                title_key = self._key(title)
                if title_key and title_key in self._key(summary):
                    summary = ""

                post = self._format_post(
                    rank=len(posts) + 1,
                    post_url=(item.findtext("link") or "").strip(),
                    text=summary,
                    author=source or "Bloomberg",
                    published_at=(item.findtext("pubDate") or "").strip(),
                    engagement={},
                    media=[],
                    extra={
                        "title": title,
                        "source": source or "Bloomberg",
                        "query": query,
                    },
                )
                posts.append(post)

            print(f"✅ [Bloomberg] Fetched {len(posts)} articles")

            await self._fetch_full_content(posts)
            return posts

        except ElementTree.ParseError as e:
            print(f"❌ [Bloomberg] Error: bad RSS payload ({e})")
            return self._error_response(f"Could not parse Google News RSS: {e}")
        except asyncio.TimeoutError:
            print("❌ [Bloomberg] Error: request timed out")
            return self._error_response("Bloomberg request timed out")
        except Exception as e:
            print(f"❌ [Bloomberg] Error: {e}")
            return self._error_response(str(e))
