"""YouTube channel scraper via the official YouTube Data API v3.

Free (10,000 quota units/day on the default project quota), no Apify
actor — same "free public API, no compute units" pattern as
sec_scraper.py, news_scraper.py, patents_scraper.py, and rss_scraper.py.
Needs a free API key: Google Cloud Console -> APIs & Services -> enable
"YouTube Data API v3" -> Credentials -> API key. Set as YOUTUBE_API_KEY
in .env.

Uses playlistItems (channel's uploads playlist) rather than search.list —
1 quota unit instead of search's 100, for the same result when all you
want is "this channel's recent uploads" rather than a keyword search.
"""
import asyncio
from typing import Any, Dict, List

import aiohttp

from config import DEFAULT_POST_LIMIT, TIMEOUTS, YOUTUBE_API_KEY
from .base_scraper import BaseScraper

CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


class YouTubeScraper(BaseScraper):
    """Fetches the most recent uploads from a YouTube channel."""

    def __init__(self):
        super().__init__("youtube")

    async def scrape(
        self, channel_id: str, limit: int = DEFAULT_POST_LIMIT
    ) -> List[Dict[str, Any]]:
        """
        Fetch recent uploads from a YouTube channel.

        Args:
            channel_id: the channel's ID (starts with "UC..." — found in
                the channel's "About" page, or via a channel URL that
                already has it: youtube.com/channel/UCxxxxxxxx)
            limit: number of videos to return (endpoint caps at 50/page)

        Returns:
            List of standardized post dictionaries, newest first
        """
        if not YOUTUBE_API_KEY:
            return self._error_response(
                "YOUTUBE_API_KEY is not set in .env — get a free key from "
                "Google Cloud Console (enable YouTube Data API v3)"
            )
        try:
            print(f"▶️  [YouTube] Starting fetch for channel: {channel_id}")

            timeout = aiohttp.ClientTimeout(total=TIMEOUTS["youtube"])
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    CHANNELS_URL,
                    params={
                        "part": "contentDetails",
                        "id": channel_id,
                        "key": YOUTUBE_API_KEY,
                    },
                ) as resp:
                    if resp.status != 200:
                        return self._error_response(
                            f"YouTube API returned HTTP {resp.status} looking up channel"
                        )
                    channel_data = await resp.json()

                channel_items = channel_data.get("items") or []
                if not channel_items:
                    return self._error_response(f"No YouTube channel found for id {channel_id}")
                uploads_playlist = (
                    channel_items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
                )

                async with session.get(
                    PLAYLIST_ITEMS_URL,
                    params={
                        "part": "snippet",
                        "playlistId": uploads_playlist,
                        "maxResults": min(limit, 50),
                        "key": YOUTUBE_API_KEY,
                    },
                ) as resp:
                    if resp.status != 200:
                        return self._error_response(
                            f"YouTube API returned HTTP {resp.status} listing uploads"
                        )
                    playlist_data = await resp.json()

                videos = playlist_data.get("items") or []
                video_ids = [
                    v["snippet"]["resourceId"]["videoId"]
                    for v in videos
                    if v.get("snippet", {}).get("resourceId", {}).get("videoId")
                ]

                # View/like counts — a separate call since playlistItems
                # doesn't carry statistics. Not fatal if this one fails;
                # posts still come back, just without engagement numbers.
                stats_by_id: Dict[str, Dict[str, Any]] = {}
                if video_ids:
                    async with session.get(
                        VIDEOS_URL,
                        params={
                            "part": "statistics",
                            "id": ",".join(video_ids),
                            "key": YOUTUBE_API_KEY,
                        },
                    ) as resp:
                        if resp.status == 200:
                            stats_data = await resp.json()
                            for v in stats_data.get("items") or []:
                                stats_by_id[v["id"]] = v.get("statistics", {})

            posts = []
            for idx, v in enumerate(videos[:limit], start=1):
                snippet = v.get("snippet", {})
                video_id = snippet.get("resourceId", {}).get("videoId", "")
                stats = stats_by_id.get(video_id, {})

                post = self._format_post(
                    rank=idx,
                    post_url=f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
                    text=(snippet.get("description") or "")[:1000],
                    author=snippet.get("channelTitle", ""),
                    published_at=snippet.get("publishedAt"),
                    engagement={
                        "views": int(stats.get("viewCount", 0)),
                        "likes": int(stats.get("likeCount", 0)),
                        "comments": int(stats.get("commentCount", 0)),
                    },
                    media=[
                        (snippet.get("thumbnails", {}).get("high") or {}).get("url", "")
                    ],
                    extra={"title": snippet.get("title", "")},
                )
                posts.append(post)

            print(f"✅ [YouTube] Fetched {len(posts)} videos")
            return posts

        except asyncio.TimeoutError:
            print("❌ [YouTube] Error: request timed out")
            return self._error_response("YouTube API request timed out")
        except Exception as e:
            print(f"❌ [YouTube] Error: {e}")
            return self._error_response(str(e))
