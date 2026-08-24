"""Generic RSS/Atom feed scraper.

Free, no signup, no Apify actor — same "free public API" pattern as
sec_scraper.py, news_scraper.py, and patents_scraper.py, but pointed at
whatever feed URL a target supplies (a company blog's own RSS feed, a
newsletter, a YouTube channel's RSS feed as an alternative to the API
scraper, etc.) instead of a fixed search endpoint.
"""
import asyncio
import re
from typing import Any, Dict, List
from xml.etree import ElementTree

import aiohttp

from config import DEFAULT_POST_LIMIT, TIMEOUTS
from .base_scraper import BaseScraper

TAG_RE = re.compile(r"<[^>]+>")

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
DC_NS = {"dc": "http://purl.org/dc/elements/1.1/"}


class RSSScraper(BaseScraper):
    """Fetches recent entries from an arbitrary RSS 2.0 or Atom feed."""

    def __init__(self):
        super().__init__("rss")

    @staticmethod
    def _clean(html: str) -> str:
        return TAG_RE.sub("", html or "").strip()

    async def scrape(
        self, feed_url: str, limit: int = DEFAULT_POST_LIMIT
    ) -> List[Dict[str, Any]]:
        """
        Fetch recent entries from an RSS or Atom feed.

        Args:
            feed_url: the feed's URL
            limit: number of entries to return

        Returns:
            List of standardized post dictionaries, newest first (as the
            feed orders them — RSS/Atom feeds are conventionally
            newest-first already, this doesn't re-sort)
        """
        try:
            print(f"📡 [RSS] Starting fetch for: {feed_url}")

            timeout = aiohttp.ClientTimeout(total=TIMEOUTS["rss"])
            headers = {"User-Agent": "Mozilla/5.0 (compatible; ScraperEngine/1.0)"}

            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(feed_url) as resp:
                    if resp.status != 200:
                        return self._error_response(f"Feed returned HTTP {resp.status}")
                    body = await resp.text()

            try:
                root = ElementTree.fromstring(body)
            except ElementTree.ParseError as e:
                return self._error_response(f"Could not parse feed XML: {e}")

            # RSS 2.0: <rss><channel><item>...</item></channel></rss>
            # Atom:    <feed><entry>...</entry></feed>
            items = root.findall("./channel/item")
            is_atom = False
            if not items:
                items = root.findall("atom:entry", ATOM_NS)
                is_atom = True

            posts = []
            for idx, item in enumerate(items[:limit], start=1):
                if is_atom:
                    title = (item.findtext("atom:title", "", ATOM_NS) or "").strip()
                    link_el = item.find("atom:link", ATOM_NS)
                    link = (link_el.get("href") if link_el is not None else "") or ""
                    summary = self._clean(
                        item.findtext("atom:summary", "", ATOM_NS)
                        or item.findtext("atom:content", "", ATOM_NS)
                    )
                    author = (item.findtext("atom:author/atom:name", "", ATOM_NS) or "").strip()
                    published = (
                        item.findtext("atom:published", "", ATOM_NS)
                        or item.findtext("atom:updated", "", ATOM_NS)
                    )
                else:
                    title = (item.findtext("title") or "").strip()
                    link = (item.findtext("link") or "").strip()
                    summary = self._clean(item.findtext("description"))
                    author = (
                        item.findtext("author")
                        or item.findtext("dc:creator", "", DC_NS)
                        or ""
                    ).strip()
                    published = item.findtext("pubDate")

                post = self._format_post(
                    rank=idx,
                    post_url=link,
                    text=summary or title,
                    author=author,
                    published_at=published,
                    engagement={},
                    media=[],
                    extra={"title": title, "feed_url": feed_url},
                )
                posts.append(post)

            print(f"✅ [RSS] Fetched {len(posts)} entries")
            return posts

        except asyncio.TimeoutError:
            print("❌ [RSS] Error: request timed out")
            return self._error_response("Feed request timed out")
        except Exception as e:
            print(f"❌ [RSS] Error: {e}")
            return self._error_response(str(e))
