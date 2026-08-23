"""Google News scraper via the public News RSS feed.

Uses Google's own RSS endpoint rather than an Apify actor: it is free, needs no
token, and returns third-party coverage instead of company-published PR.
"""
import asyncio
import re
from typing import List, Dict, Any
from urllib.parse import quote
from xml.etree import ElementTree

import aiohttp

from config import DEFAULT_POST_LIMIT, NEWS_LOCALE, TIMEOUTS
from .base_scraper import BaseScraper

RSS_URL = (
    "https://news.google.com/rss/search"
    "?q={query}&hl={hl}&gl={gl}&ceid={gl}:{hl_short}"
)

TAG_RE = re.compile(r"<[^>]+>")
NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

# Aggregators auto-generate a story for every 13F position change. Google's RSS
# ignores negative phrase terms, so filter them out on our side instead.
NOISE_SOURCES = {
    "marketbeat", "marketbeat.com", "defense world", "etf daily news",
    "americanbankingnews", "tickerreport", "the cerbat gem", "modern readers",
    "mayfield recorder", "stocks register", "the markets daily",
}

NOISE_HEADLINE_RE = re.compile(
    r"\b(buys?|acquires?|purchases?|sells?|trims?|grows?|boosts?|lowers?|takes?|"
    r"raises?|reduces?|cuts?|sold|bought|invests?)\b.{0,60}?"
    r"\b(stake|position|shares?|holdings?|stock)\b",
    re.IGNORECASE,
)

DEFAULT_EXCLUSIONS = ""


class GoogleNewsScraper(BaseScraper):
    """Fetches recent news articles mentioning a company."""

    def __init__(self):
        super().__init__("news")

    @staticmethod
    def _key(text: str) -> str:
        """Normalize for duplicate detection (case/punctuation insensitive)."""
        return NORMALIZE_RE.sub("", (text or "").lower())

    @staticmethod
    def _is_noise(title: str, source: str) -> bool:
        """Filter auto-generated 13F/holdings churn from stock-ticker farms."""
        if (source or "").strip().lower() in NOISE_SOURCES:
            return True
        return bool(NOISE_HEADLINE_RE.search(title or ""))

    @staticmethod
    def _clean(html: str) -> str:
        """Strip the markup Google wraps around RSS descriptions."""
        return TAG_RE.sub("", html or "").replace("&nbsp;", " ").strip()

    async def scrape(
        self,
        query: str,
        limit: int = DEFAULT_POST_LIMIT,
        days: int = 30,
        exclusions: str = DEFAULT_EXCLUSIONS,
    ) -> List[Dict[str, Any]]:
        """
        Fetch recent news articles for a search query.

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
                part for part in (query, exclusions, f"when:{days}d" if days else "")
                if part
            )
            print(f"📰 [Google News] Starting fetch for: {full_query}")

            hl = NEWS_LOCALE["hl"]
            url = RSS_URL.format(
                query=quote(full_query),
                hl=hl,
                gl=NEWS_LOCALE["gl"],
                hl_short=hl.split("-")[0],
            )

            timeout = aiohttp.ClientTimeout(total=TIMEOUTS["news"])
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
            skipped = 0
            for item in items:
                if len(posts) >= limit:
                    break
                title = (item.findtext("title") or "").strip()
                source_el = item.find("source")
                source = (source_el.text or "").strip() if source_el is not None else ""
                # Headlines arrive as "Headline - Source"; the source has its
                # own field, so trim the suffix.
                if source and title.endswith(f" - {source}"):
                    title = title[: -len(f" - {source}")].strip()

                if self._is_noise(title, source):
                    skipped += 1
                    continue

                summary = self._clean(item.findtext("description"))

                # Google's RSS description is usually just the headline plus the
                # source name again — drop it rather than showing it twice.
                title_key = self._key(title)
                if title_key and title_key in self._key(summary):
                    summary = ""

                post = self._format_post(
                    rank=len(posts) + 1,
                    post_url=(item.findtext("link") or "").strip(),
                    text=summary,
                    author=source,
                    published_at=(item.findtext("pubDate") or "").strip(),
                    engagement={},
                    media=[],
                    extra={
                        "title": title,
                        "source": source,
                        "query": query,
                    },
                )
                posts.append(post)

            note = f" ({skipped} aggregator stubs filtered)" if skipped else ""
            print(f"✅ [Google News] Fetched {len(posts)} articles{note}")
            return posts

        except ElementTree.ParseError as e:
            print(f"❌ [Google News] Error: bad RSS payload ({e})")
            return self._error_response(f"Could not parse Google News RSS: {e}")
        except asyncio.TimeoutError:
            print("❌ [Google News] Error: request timed out")
            return self._error_response("Google News request timed out")
        except Exception as e:
            print(f"❌ [Google News] Error: {e}")
            return self._error_response(str(e))
