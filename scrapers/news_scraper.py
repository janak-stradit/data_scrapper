"""Google News scraper via the public News RSS feed, plus full-article
content fetched through Apify's website-content-crawler.

The RSS feed itself is free — Google's own endpoint, no actor, no token —
and gives only headlines; Google doesn't syndicate article bodies via RSS
at all, regardless of how the feed is parsed. Getting real content means
fetching each article's own page, which is what the Apify pass below is
for. That part is NOT free — it runs the same actor blog_scraper.py uses,
billed against APIFY_TOKEN, once per article per scrape.

Two things confirmed by testing against live Google News links before
writing this:
1. Google News redirect links land on a cookie-consent interstitial when
   Apify's proxy exits from an EU/GDPR region — apifyProxyCountry="US"
   avoids that.
2. Even with that fixed, individual publishers can still block the
   crawler outright (Cloudflare, paywalls, bot-detection) — this is
   inherent to scraping arbitrary third-party sites, not fixable in
   general. So extraction is best-effort per article: on success the
   post's text is replaced with the real article content; on failure
   (blocked, or Apify itself fails/times out) it silently keeps the
   RSS-only headline/summary instead of erroring the whole scrape.
"""
import asyncio
import re
from typing import List, Dict, Any
from urllib.parse import quote
from xml.etree import ElementTree

import aiohttp
from apify_client import ApifyClientAsync

from config import ACTORS, APIFY_TOKEN, DEFAULT_POST_LIMIT, NEWS_LOCALE, TIMEOUTS
from .base_scraper import BaseScraper

RSS_URL = (
    "https://news.google.com/rss/search"
    "?q={query}&hl={hl}&gl={gl}&ceid={gl}:{hl_short}"
)

TAG_RE = re.compile(r"<[^>]+>")
NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

# Heuristics for "this page is a block/consent/challenge wall, not the
# article" — checked against the extracted title + the first stretch of
# markdown. Kept short and lowercase; matched case-insensitively.
_BLOCKED_MARKERS = (
    "attention required", "enable javascript", "checking your browser",
    "verify you are human", "just a moment", "cloudflare",
    "before you continue", "we use cookies and data", "access denied",
    "are you a robot", "unusual traffic",
)

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
        self.client = ApifyClientAsync(APIFY_TOKEN)
        self.actor_id = ACTORS["blog"]  # generic website-content-crawler

    @staticmethod
    def _looks_blocked(title: str, markdown: str) -> bool:
        """True if this looks like a consent/challenge/block page, not the
        real article — a short body plus a known phrase is a strong tell;
        a very short body alone (some legitimate articles are brief) is
        weaker but still treated as "couldn't get real content"."""
        combined = f"{title} {markdown[:500]}".lower()
        if any(marker in combined for marker in _BLOCKED_MARKERS):
            return True
        return len(markdown.strip()) < 200

    async def _fetch_full_content(self, posts: List[Dict[str, Any]]) -> None:
        """Best-effort: replace each post's text with its real article
        content. Mutates `posts` in place; never raises — any failure here
        (actor error, timeout, every article blocked) just leaves the
        RSS-only headline/summary already on each post.
        """
        if not posts:
            return
        try:
            run_input = {
                "startUrls": [{"url": p["post_url"]} for p in posts if p.get("post_url")],
                "crawlerType": "playwright:adaptive",
                "maxCrawlDepth": 0,
                "maxCrawlPages": len(posts),
                "maxResults": len(posts),
                "saveMarkdown": True,
                "removeCookieWarnings": True,
                "blockMedia": True,
                # US avoids the GDPR cookie-consent interstitial Google
                # News redirects show to EU-region proxy exits — confirmed
                # by testing directly against live links before this was
                # written; without it every result is a consent page, not
                # the article.
                "proxyConfiguration": {"useApifyProxy": True, "apifyProxyCountry": "US"},
            }
            items = await self._run_actor(
                self.client, self.actor_id, run_input, TIMEOUTS["news_content"],
                memory_mbytes=4096,
            )
        except Exception as e:
            print(f"⚠️  [Google News] Full-content fetch failed, keeping headlines only ({e})")
            return

        # crawl.referrerUrl is the original start URL (the Google redirect
        # link) each result came from — that's what correlates a result
        # back to the post it belongs to, since the *loaded* URL is
        # whatever the redirect chain ends up at.
        by_referrer = {
            item.get("crawl", {}).get("referrerUrl"): item
            for item in items
            if isinstance(item, dict)
        }

        filled = 0
        for post in posts:
            item = by_referrer.get(post.get("post_url"))
            if not item:
                continue
            title = (item.get("metadata", {}) or {}).get("title", "")
            markdown = item.get("markdown") or ""
            if self._looks_blocked(title, markdown):
                continue
            post["text"] = markdown[:5000]
            post["full_content"] = True
            filled += 1

        print(f"   [Google News] Full content: {filled}/{len(posts)} articles (rest kept headline-only)")

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

            await self._fetch_full_content(posts)
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
