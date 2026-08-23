"""Reddit post scraper using Apify Actor."""
from urllib.parse import quote
from typing import List, Dict, Any
from apify_client import ApifyClientAsync

from config import APIFY_TOKEN, ACTORS, TIMEOUTS, DEFAULT_POST_LIMIT
from .base_scraper import BaseScraper


class RedditScraper(BaseScraper):
    """Scrapes last N submissions from a Reddit user profile."""

    def __init__(self):
        super().__init__("reddit")
        self.client = ApifyClientAsync(APIFY_TOKEN)
        self.actor_id = ACTORS["reddit"]

    @staticmethod
    def _as_query(identifier: str) -> str | None:
        """Return a search query if the identifier is free text, else None."""
        ident = identifier.strip()
        if ident.startswith(("http://", "https://", "r/", "u/")):
            return None
        return ident if any(c in ident for c in " \"'") else None

    @staticmethod
    def _build_url(identifier: str) -> str:
        """Turn a username, subreddit, full URL, or free-text query into a Reddit URL."""
        ident = identifier.strip()
        if ident.startswith("http://") or ident.startswith("https://"):
            return ident
        if ident.startswith("r/"):
            return f"https://www.reddit.com/{ident}/"
        if ident.startswith("u/"):
            return f"https://www.reddit.com/user/{ident[2:]}/"
        # Anything with spaces or quotes is a search query, not a username.
        if any(c in ident for c in " \"'"):
            return f"https://www.reddit.com/search/?q={quote(ident)}&sort=new&t=year"
        return f"https://www.reddit.com/user/{ident}/"

    async def scrape(
        self,
        identifier: str,
        limit: int = DEFAULT_POST_LIMIT,
        keywords: List[str] = None,
        exclude: List[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Scrape last N Reddit posts for a user, subreddit, URL, or search query.

        Args:
            identifier: username, u/name, r/subreddit, full reddit URL, or a
                search query (e.g. '"Northern Trust"') to track company mentions
            limit: Number of posts to fetch (default: 10)
            keywords: Optional terms — a post must mention one of them to be
                kept. Reddit search is noisy, so this filters false positives.
            exclude: Terms that disqualify a post — for company names that
                collide with games, films, or other products.

        Returns:
            List of standardized post dictionaries
        """
        username = identifier
        try:
            search_query = self._as_query(identifier)
            target_url = None if search_query else self._build_url(identifier)
            print(f"🟠 [Reddit] Starting scrape for: {search_query or target_url}")

            base_input = {
                # Relevance beats recency here: Reddit's "new" feed for a
                # company query is mostly unrelated posts.
                "sort": "relevance",
                "time": "year",
                # Over-fetch, because keyword filtering drops false positives.
                "maxItems": min(limit * 3, 60) if keywords else limit,
                "maxPostCount": min(limit * 3, 60) if keywords else limit,
                "skipComments": True,
                "searchComments": False,
                "searchCommunities": False,
                "searchUsers": False,
                "includeNSFW": False,
                "proxy": {"useApifyProxy": True},
            }
            if search_query:
                run_input = {**base_input, "searches": [search_query], "searchPosts": True}
            else:
                run_input = {**base_input, "startUrls": [{"url": target_url}]}

            items = await self._run_actor(
                self.client, self.actor_id, run_input, TIMEOUTS["reddit"]
            )

            # Keep submissions only — no comments, communities, or user rows.
            posts_only = [
                i for i in items
                if (i.get("dataType") or i.get("type") or "post") in ("post", "community_post")
                and (i.get("title") or i.get("body"))
                and not i.get("parentId")
            ]

            def haystack(item):
                return f"{item.get('title', '')} {item.get('body', '')} " \
                       f"{item.get('communityName', '')}".lower()

            if keywords:
                terms = [k.lower() for k in keywords]
                posts_only = [i for i in posts_only if any(t in haystack(i) for t in terms)]

            if exclude:
                banned = [k.lower() for k in exclude]
                before = len(posts_only)
                posts_only = [
                    i for i in posts_only if not any(b in haystack(i) for b in banned)
                ]
                if before - len(posts_only):
                    print(
                        f"   [Reddit] Dropped {before - len(posts_only)} posts "
                        "matching excluded terms"
                    )

            posts = []
            for idx, item in enumerate(posts_only[:limit], start=1):
                permalink = item.get("permalink", "")
                post_url = item.get("url") or (f"https://www.reddit.com{permalink}" if permalink else "")

                engagement = {
                    "upvotes": item.get("upVotes") or item.get("ups", 0),
                    "downvotes": item.get("downVotes") or item.get("downs", 0),
                    "comments": item.get("numberOfComments") or item.get("numComments", 0),
                    "score": item.get("score", 0),
                }

                extra = {
                    "title": item.get("title", ""),
                    "subreddit": item.get("communityName") or item.get("subreddit", ""),
                    "is_self_post": item.get("isSelf", False),
                }

                post = self._format_post(
                    rank=idx,
                    post_url=post_url,
                    text=item.get("body") or item.get("text") or item.get("selftext", ""),
                    author=item.get("author") or username,
                    published_at=item.get("createdAt") or item.get("date"),
                    engagement=engagement,
                    media=item.get("media", []),
                    extra=extra,
                )
                posts.append(post)

            print(f"✅ [Reddit] Scraped {len(posts)} posts")
            return posts

        except Exception as e:
            print(f"❌ [Reddit] Error: {e}")
            return self._error_response(str(e))
