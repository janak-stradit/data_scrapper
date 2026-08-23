"""USPTO patent scraper via Google Patents' public search endpoint.

Free, no signup: the official PatentsView/USPTO API now sits behind USPTO's
Open Data Portal, which requires a full USPTO.gov account with MFA to get an
API key. Google Patents' own search UI calls an undocumented but stable JSON
endpoint (patents.google.com/xhr/query) with no auth at all, so that's what
this uses instead — same "free public API, no Apify actor" pattern as
sec_scraper.py and news_scraper.py, just against an unofficial endpoint.

Because it's unofficial, the response shape isn't a stable contract the way
EDGAR's is: this parses defensively and reports a clear error rather than
crashing if Google changes it.
"""
import asyncio
import json
import re
from typing import List, Dict, Any
from urllib.parse import quote, urlencode

import aiohttp

from config import DEFAULT_POST_LIMIT, TIMEOUTS
from .base_scraper import BaseScraper

SEARCH_URL = "https://patents.google.com/xhr/query?url={inner}&exp="

TAG_RE = re.compile(r"<[^>]+>")


class PatentsScraper(BaseScraper):
    """Fetches patents matching an inventor name from Google Patents."""

    def __init__(self):
        super().__init__("patents")

    @staticmethod
    def _clean(html: str) -> str:
        return TAG_RE.sub("", html or "").strip()

    async def scrape(
        self, inventor: str, limit: int = DEFAULT_POST_LIMIT
    ) -> List[Dict[str, Any]]:
        """
        Fetch patents naming this person as an inventor.

        Args:
            inventor: inventor name to search, e.g. "Ranjit Samra"
            limit: number of patents to return (endpoint caps at 100/page)

        Returns:
            List of standardized post dictionaries, newest first
        """
        try:
            print(f"🧪 [Patents] Starting search for inventor: {inventor}")

            inner = urlencode({"inventor": inventor, "num": min(limit, 100)})
            url = SEARCH_URL.format(inner=quote(inner, safe=""))

            timeout = aiohttp.ClientTimeout(total=TIMEOUTS["patents"])
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; ScraperEngine/1.0)",
                "X-Same-Domain": "1",
            }

            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return self._error_response(
                            f"Google Patents returned HTTP {resp.status}"
                        )
                    raw = await resp.text()

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return self._error_response("Google Patents returned a non-JSON payload")

            # The endpoint is unofficial and undocumented: results have shown up
            # both at the top level and nested under "content" as a JSON string
            # depending on version, so accept either shape.
            results_block = payload.get("results")
            if results_block is None and isinstance(payload.get("content"), str):
                try:
                    results_block = json.loads(payload["content"]).get("results")
                except json.JSONDecodeError:
                    results_block = None

            if not isinstance(results_block, dict):
                return self._error_response(
                    "Unrecognized response shape from Google Patents "
                    "(the unofficial endpoint may have changed)"
                )

            rows = []
            for cluster in results_block.get("cluster") or []:
                rows.extend(cluster.get("result") or [])

            posts = []
            for idx, row in enumerate(rows[:limit], start=1):
                patent = row.get("patent") or {}
                pub_number = (patent.get("publication_number") or "").strip()
                title = self._clean(patent.get("title", ""))
                snippet = self._clean(patent.get("snippet", ""))
                published_at = (
                    patent.get("publication_date")
                    or patent.get("grant_date")
                    or patent.get("filing_date")
                    or patent.get("priority_date")
                )
                post_url = (
                    f"https://patents.google.com/patent/{pub_number}/en"
                    if pub_number
                    else ""
                )

                post = self._format_post(
                    rank=idx,
                    post_url=post_url,
                    text=snippet or title,
                    author=self._clean(patent.get("inventor", "")) or inventor,
                    published_at=published_at,
                    engagement={},
                    media=[],
                    extra={
                        "title": title,
                        "publication_number": pub_number,
                        "assignee": patent.get("assignee", ""),
                    },
                )
                posts.append(post)

            print(f"✅ [Patents] Found {len(posts)} patents")
            return posts

        except asyncio.TimeoutError:
            print("❌ [Patents] Error: request timed out")
            return self._error_response("Google Patents request timed out")
        except Exception as e:
            print(f"❌ [Patents] Error: {e}")
            return self._error_response(str(e))
