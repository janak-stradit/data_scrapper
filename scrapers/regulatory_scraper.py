"""Regulatory press/enforcement-action tracking — Federal Reserve and OCC.

Free, no signup, no Apify actor — reuses rss_scraper.py's RSS parsing
against a small, fixed set of regulator feeds (verified live before
adding), then filters for entries mentioning the target's name. Unlike a
per-target rss_url, these feeds are shared/global — every bank's
enforcement action is in the same feed — so without the name filter,
almost every entry would be about some other institution.

Feed URLs, confirmed working directly before use:
  https://www.federalreserve.gov/feeds/press_enforcement.xml
  https://www.occ.gov/rss/occ_news.xml
"""
import asyncio
from typing import Any, Dict, List

from config import DEFAULT_POST_LIMIT
from .base_scraper import BaseScraper
from .rss_scraper import RSSScraper

FEEDS = {
    "Federal Reserve": "https://www.federalreserve.gov/feeds/press_enforcement.xml",
    "OCC": "https://www.occ.gov/rss/occ_news.xml",
}

# Each feed is small (a rolling few dozen recent items) — over-fetching
# every entry is cheap, and necessary since the name filter has to run
# after the fact (these feeds have no search/query capability of their own).
_FETCH_PER_FEED = 100


class RegulatoryScraper(BaseScraper):
    """Finds Fed/OCC press releases or enforcement actions naming a target."""

    def __init__(self):
        super().__init__("regulatory")
        self._rss = RSSScraper()

    async def scrape(
        self, query: str, limit: int = DEFAULT_POST_LIMIT
    ) -> List[Dict[str, Any]]:
        """
        Search Federal Reserve and OCC feeds for entries naming `query`.

        Args:
            query: name to match, e.g. "Bank of New York Mellon"
            limit: number of matches to return, across both feeds combined

        Returns:
            List of standardized post dictionaries, newest first
        """
        try:
            print(f"🏦 [Regulatory] Starting search for: {query}")
            needle = query.strip('"').lower()

            matches: List[Dict[str, Any]] = []
            for source_name, feed_url in FEEDS.items():
                entries = await self._rss.scrape(feed_url, limit=_FETCH_PER_FEED)
                for entry in entries:
                    if entry.get("error"):
                        print(f"⚠️  [Regulatory] Could not read {source_name} feed: {entry.get('message')}")
                        continue
                    haystack = f"{entry.get('title', '')} {entry.get('text', '')}".lower()
                    if needle in haystack:
                        entry["platform"] = self.platform
                        entry["source"] = source_name
                        matches.append(entry)

            matches.sort(key=lambda p: p.get("published_at") or "", reverse=True)

            posts = matches[:limit]
            for idx, post in enumerate(posts, start=1):
                post["rank"] = idx

            print(f"✅ [Regulatory] Found {len(posts)} matching entries across {len(FEEDS)} feeds")
            return posts

        except asyncio.TimeoutError:
            print("❌ [Regulatory] Error: request timed out")
            return self._error_response("Regulatory feed request timed out")
        except Exception as e:
            print(f"❌ [Regulatory] Error: {e}")
            return self._error_response(str(e))
