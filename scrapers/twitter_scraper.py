"""Twitter/X post scraper using Apify Actor."""
from typing import List, Dict, Any
from apify_client import ApifyClientAsync

from config import APIFY_TOKEN, ACTORS, TIMEOUTS, DEFAULT_POST_LIMIT
from .base_scraper import BaseScraper


class TwitterScraper(BaseScraper):
    """Scrapes last N tweets from a Twitter/X profile."""

    def __init__(self):
        super().__init__("twitter")
        self.client = ApifyClientAsync(APIFY_TOKEN)
        self.actor_id = ACTORS["twitter"]

    async def scrape(self, handle: str, limit: int = DEFAULT_POST_LIMIT) -> List[Dict[str, Any]]:
        """
        Scrape last N tweets from a Twitter/X profile.

        Args:
            handle: Twitter handle (with or without @)
            limit: Number of tweets to fetch (default: 10)

        Returns:
            List of standardized post dictionaries
        """
        try:
            clean_handle = handle.lstrip("@")
            print(f"⚫ [Twitter/X] Starting scrape for: @{clean_handle}")

            run_input = {
                "twitterHandles": [clean_handle],
                "maxItems": limit,
                "includeReplies": False,
                "includeRetweets": False,
                "sort": "Latest",
            }

            items = await self._run_actor(
                self.client, self.actor_id, run_input, TIMEOUTS["twitter"]
            )

            # The actor sometimes returns empty envelopes (rate limits, blocked
            # profiles). Storing those would poison the store, so drop them.
            usable = [
                i for i in items
                if (i.get("id") or i.get("url"))
                and (i.get("text") or i.get("fullText"))
            ]
            if items and not usable:
                return self._error_response(
                    f"Actor returned {len(items)} empty items for @{clean_handle} "
                    "(rate limited or profile unavailable)"
                )

            posts = []
            for idx, item in enumerate(usable[:limit], start=1):
                tweet_id = item.get("id", "")
                post_url = item.get("url") or f"https://x.com/{clean_handle}/status/{tweet_id}"
                author = item.get("author", {})

                engagement = {
                    "likes": item.get("likeCount") or item.get("favoriteCount", 0),
                    "retweets": item.get("retweetCount", 0),
                    "replies": item.get("replyCount", 0),
                    "quotes": item.get("quoteCount", 0),
                    "views": item.get("viewCount", 0),
                }

                extra = {
                    "author_display_name": author.get("name", ""),
                    "is_retweet": item.get("isRetweet", False),
                    "is_reply": item.get("isReply", False),
                    "hashtags": item.get("hashtags", []),
                    "mentions": item.get("mentions", []),
                }

                post = self._format_post(
                    rank=idx,
                    post_url=post_url,
                    text=item.get("text") or item.get("fullText", ""),
                    author=author.get("userName") or clean_handle,
                    published_at=item.get("createdAt") or item.get("timestamp"),
                    engagement=engagement,
                    media=item.get("media", []),
                    extra=extra,
                )
                posts.append(post)

            print(f"✅ [Twitter/X] Scraped {len(posts)} tweets")
            return posts

        except Exception as e:
            print(f"❌ [Twitter/X] Error: {e}")
            return self._error_response(str(e))
