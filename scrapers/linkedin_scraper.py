"""LinkedIn post scraper using Apify Actor."""
import asyncio
from typing import List, Dict, Any
from apify_client import ApifyClientAsync

from config import APIFY_TOKEN, ACTORS, TIMEOUTS, DEFAULT_POST_LIMIT
from .base_scraper import BaseScraper


class LinkedInScraper(BaseScraper):
    """Scrapes last N posts from a LinkedIn public profile."""

    def __init__(self):
        super().__init__("linkedin")
        self.client = ApifyClientAsync(APIFY_TOKEN)
        self.actor_id = ACTORS["linkedin"]

    async def scrape(self, profile_url: str, limit: int = DEFAULT_POST_LIMIT) -> List[Dict[str, Any]]:
        """
        Scrape last N posts from a LinkedIn profile.

        Args:
            profile_url: Full LinkedIn profile URL (e.g. https://linkedin.com/in/username)
            limit: Number of posts to fetch (default: 10)

        Returns:
            List of standardized post dictionaries
        """
        try:
            print(f"🔵 [LinkedIn] Starting scrape for: {profile_url}")

            run_input = {
                "targetUrls": [profile_url],
                "maxPosts": limit,
                "includeReposts": True,
                "includeQuotePosts": True,
                "scrapeReactions": False,
                "scrapeComments": False,
            }

            items = await self._run_actor(
                self.client, self.actor_id, run_input, TIMEOUTS["linkedin"]
            )

            posts = []
            for idx, item in enumerate(items[:limit], start=1):
                social = item.get("socialContent") or {}
                counts = item.get("engagement") or item.get("socialCounts") or {}
                author = item.get("author") or item.get("actor") or {}

                engagement = {
                    "likes": counts.get("likes", counts.get("likeCount", 0)),
                    "comments": counts.get("comments", counts.get("commentCount", 0)),
                    "shares": counts.get("shares", counts.get("shareCount", 0)),
                    "reactions": counts.get("reactions", 0),
                }

                post = self._format_post(
                    rank=idx,
                    post_url=item.get("linkedinUrl") or item.get("url") or social.get("shareUrl", ""),
                    text=item.get("content") or item.get("text") or item.get("postText", ""),
                    author=(author.get("name") if isinstance(author, dict) else author) or "",
                    published_at=item.get("postedAt", {}).get("date")
                    if isinstance(item.get("postedAt"), dict)
                    else item.get("postedAt") or item.get("date"),
                    engagement=engagement,
                    media=[
                        m.get("url") if isinstance(m, dict) else m
                        for m in (item.get("postImages") or item.get("images") or [])
                    ],
                    extra={"post_type": item.get("type", "")},
                )
                posts.append(post)

            print(f"✅ [LinkedIn] Scraped {len(posts)} posts")
            return posts

        except Exception as e:
            print(f"❌ [LinkedIn] Error: {e}")
            return self._error_response(str(e))
