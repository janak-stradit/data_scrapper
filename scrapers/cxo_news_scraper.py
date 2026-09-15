"""CXO movement news via Apify's memo23/google-news-scraper actor.

Purpose-built for tracking executive leadership changes — joins,
resignations, promotions, retirements — rather than general company news
(see news_scraper.py for that). Unlike news_scraper.py's RSS + generic
crawler combo, this actor does the Google News search *and* full-article
extraction in one call.

Schema confirmed by a live test run (2026-09-03), not just the actor's
docs page — request/response shapes below are what was actually observed:
    run_input: {startInputs: [query, ...], maxItems, maxArticlesPerInput,
                enrichBody, resolveUrls, country, language, since, until}
    result item: {title, publisher, publisherDomain, googleNewsUrl,
                  publisherUrl, publishedAt, body, bodyExtracted,
                  wordCount, author, description, ...}

bodyExtracted is often false even with enrichBody on — paywalled or
Cloudflare-protected publishers return metadata only without a paid
Apify plan (CF-bypass is a free-tier restriction, confirmed in the same
test run). Handled here as "keep the article, just with whatever text is
available" rather than dropping it — a headline-only CXO move is still a
real signal, same as blocked articles in news_scraper.py.
"""
from typing import List, Dict, Any

from apify_client import ApifyClientAsync

from config import ACTORS, APIFY_TOKEN, DEFAULT_POST_LIMIT, NEWS_LOCALE, TIMEOUTS
from .base_scraper import BaseScraper


class CxoNewsScraper(BaseScraper):
    """Fetches full-text Google News articles about executive moves."""

    def __init__(self):
        super().__init__("cxo_news")
        self.client = ApifyClientAsync(APIFY_TOKEN)
        self.actor_id = ACTORS["cxo_news"]

    async def scrape(
        self,
        query: str,
        limit: int = DEFAULT_POST_LIMIT,
        since: str = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch recent CXO-movement news articles for a search query.

        Args:
            query: Search query, e.g.
                '"BlackRock" (CEO OR CFO OR COO OR CTO OR President OR
                "Chief Investment Officer" OR "Chief Risk Officer")
                (joins OR appointed OR named OR resigns OR "steps down" OR
                retires OR promoted OR succeeds)'
            limit: Number of articles to return
            since: Only include articles published on/after this date
                (YYYY-MM-DD) — omit for no lower bound

        Returns:
            List of standardized post dictionaries, newest first
        """
        try:
            print(f"👔 [CXO News] Starting fetch for: {query}")

            run_input = {
                "startInputs": [query],
                "maxItems": limit,
                "maxArticlesPerInput": limit,
                "enrichBody": True,
                "resolveUrls": True,
                "country": NEWS_LOCALE["gl"],
                "language": NEWS_LOCALE["hl"].split("-")[0],
            }
            if since:
                run_input["since"] = since

            items = await self._run_actor(
                self.client, self.actor_id, run_input, TIMEOUTS["cxo_news"],
            )

            posts = []
            filled = 0
            for idx, item in enumerate(items[:limit], start=1):
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or ""
                publisher = item.get("publisher") or item.get("publisherDomain") or ""
                body_extracted = bool(item.get("bodyExtracted"))
                if body_extracted:
                    filled += 1
                text = item.get("body") or item.get("description") or title

                post = self._format_post(
                    rank=idx,
                    # The resolved publisher URL is the real article link;
                    # the Google News URL is a redirect wrapper — prefer
                    # the former, fall back to the latter if resolution failed.
                    post_url=item.get("publisherUrl") or item.get("googleNewsUrl") or "",
                    text=text,
                    author=item.get("author") or publisher,
                    published_at=item.get("publishedAt"),
                    engagement={},
                    media=[],
                    extra={
                        "title": title,
                        "publisher": publisher,
                        "publisher_domain": item.get("publisherDomain"),
                        "word_count": item.get("wordCount"),
                        "full_content": body_extracted,
                        "query": query,
                    },
                )
                posts.append(post)

            note = f" ({filled}/{len(posts)} with full article text)" if posts else ""
            print(f"✅ [CXO News] Fetched {len(posts)} articles{note}")
            return posts

        except Exception as e:
            print(f"❌ [CXO News] Error: {e}")
            return self._error_response(str(e))
