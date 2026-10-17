"""Blog post scraper using a local headless browser (page_fetcher.py).

Migrated off Apify's Website Content Crawler actor (see git history for
the old version) to cut per-run cost to zero. Article discovery (finding
which URLs are recent posts) still runs over plain aiohttp when possible
— cheap and fast — and only falls back to a rendered browser fetch of the
index page when that turns up no links, which happens on JS-heavy nav.

Behavior change from the Apify version: that actor's default (no
seed_from_index, no sitemap_url) crawled the site up to 2 links deep.
This version always seeds from links found directly on the index page
(rendered if needed) — no multi-level crawl. That was already the
documented preferred behavior for seed_from_index targets ("Reading the
links ourselves lets us sort by the date in the slug"); it just now
applies unconditionally. A target whose recent articles aren't linked
from the index page itself will need blog_sitemap set instead.
"""
import asyncio
import re
from fnmatch import fnmatch
from typing import List, Dict, Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import aiohttp

from config import TIMEOUTS, DEFAULT_POST_LIMIT
from . import page_fetcher
from .base_scraper import BaseScraper

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class BlogScraper(BaseScraper):
    """Scrapes last N posts from a blog using generic web scraping."""

    def __init__(self):
        super().__init__("blog")

    @staticmethod
    def _path_depth(url: str) -> int:
        """Number of non-empty path segments in a URL."""
        return len([p for p in urlparse(url).path.split("/") if p])

    @staticmethod
    def _slug_date(url: str) -> str:
        """Sortable date pulled from a URL slug, or '' if there isn't one.

        Handles /2026/ path segments and trailing MMDDYY / MMDDYYYY stamps
        (e.g. ...-04092026.html, ...-121025.html).
        """
        stamp = re.search(r"[-_](\d{6}|\d{8})(?:\.html?)?$", url)
        if stamp:
            digits = stamp.group(1)
            if len(digits) == 8:
                mm, dd, yyyy = digits[:2], digits[2:4], digits[4:]
            else:
                mm, dd, yy = digits[:2], digits[2:4], digits[4:]
                yyyy = f"20{yy}"
            if "01" <= mm <= "12":
                return f"{yyyy}{mm}{dd}"

        year = re.findall(r"/(20\d{2})/", url)
        return year[-1] + "0000" if year else ""

    async def _fetch(self, url: str, timeout_secs: int = 120) -> str:
        """GET a page with a browser User-Agent (bot mitigation blocks others)."""
        timeout = aiohttp.ClientTimeout(total=timeout_secs, sock_connect=30)
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": BROWSER_UA}
        ) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return await resp.text()

    async def _seed_from_index(
        self,
        index_url: str,
        url_glob: str,
        limit: int,
        min_path_segments: int = None,
        render: bool = False,
    ) -> List[str]:
        """Read article links off an index page, newest first.

        Crawlers follow links in document order, which on a long press-release
        archive means the oldest entries. Reading the links ourselves lets us
        sort by the date in the slug and take the genuinely recent ones.

        render=True renders the page in a headless browser first — needed
        when the plain-HTTP HTML has no article links because the nav is
        built with JavaScript.
        """
        try:
            html = await page_fetcher.get_html(index_url) if render else await self._fetch(index_url)
        except Exception as e:
            print(f"⚠️  [Blog] Could not read index page ({type(e).__name__}): {e}")
            return []

        pattern = url_glob.replace("**", "*") if url_glob else None
        found = {}
        for href in re.findall(r'href="([^"]+)"', html):
            absolute = urljoin(index_url, href.split("#")[0])
            if pattern and not fnmatch(absolute, pattern):
                continue
            if absolute.rstrip("/") == index_url.rstrip("/"):
                continue
            if min_path_segments and self._path_depth(absolute) < min_path_segments:
                continue
            found.setdefault(absolute, self._slug_date(absolute))

        # Undated links keep document order behind the dated ones.
        ordered = sorted(found.items(), key=lambda kv: kv[1] or "0", reverse=True)
        return [url for url, _ in ordered[:limit]]

    async def _seed_from_sitemap(
        self,
        sitemap_url: str,
        url_glob: str,
        min_path_segments: int,
        limit: int,
    ) -> List[str]:
        """Pull article URLs straight from a sitemap.

        Index pages behind a bot-mitigation CDN often render their article links
        in JavaScript the crawler never reaches; the sitemap always lists them.
        """
        try:
            timeout = aiohttp.ClientTimeout(total=120, sock_connect=30)
            # Bot mitigation (Akamai et al.) silently blackholes non-browser
            # User-Agents — a real browser string is required, not politeness.
            headers = {"User-Agent": BROWSER_UA}
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(sitemap_url) as resp:
                    if resp.status != 200:
                        print(f"⚠️  [Blog] Sitemap returned HTTP {resp.status}")
                        return []
                    body = await resp.text()

            root = ElementTree.fromstring(body)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            urls = [
                (
                    (el.findtext("sm:loc", "", ns) or "").strip(),
                    (el.findtext("sm:lastmod", "", ns) or "").strip(),
                )
                for el in root.findall("sm:url", ns)
            ]

            # fnmatch has no separate "**", but its "*" already spans slashes.
            pattern = url_glob.replace("**", "*") if url_glob else None
            picked = [
                (loc, mod)
                for loc, mod in urls
                if loc
                and (not pattern or fnmatch(loc, pattern))
                and (not min_path_segments or self._path_depth(loc) >= min_path_segments)
            ]

            # Sitemaps are regenerated wholesale, so lastmod is often identical
            # for every row; fall back to the year in the URL for recency.
            def year_of(loc):
                found = re.findall(r"/(20\d{2})/", loc)
                return found[-1] if found else ""

            picked.sort(key=lambda t: (t[1], year_of(t[0])), reverse=True)
            return [loc for loc, _ in picked[:limit]]

        except Exception as e:
            print(f"⚠️  [Blog] Could not read sitemap ({type(e).__name__}): {e}")
            return []

    async def scrape(
        self,
        blog_url: str,
        limit: int = DEFAULT_POST_LIMIT,
        url_glob: str = None,
        sitemap_url: str = None,
        min_path_segments: int = None,
        skip_urls: set = None,
        seed_from_index: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Scrape last N posts from a blog.

        Args:
            blog_url: Blog, insights, or newsroom index URL
            limit: Number of posts to fetch (default: 10)
            url_glob: Optional glob restricting the crawl to article URLs
            sitemap_url: Optional sitemap to seed article URLs from, instead of
                following links on a JS-heavy index page
            min_path_segments: Drop URLs with fewer path segments than this —
                filters out section hubs that match the article glob
            skip_urls: URLs already stored from earlier runs — never crawled
                again, which is where the per-run cost saving comes from
            seed_from_index: kept for call-site compatibility; index-page
                seeding is now always how article URLs are found when no
                sitemap_url is given (see module docstring)

        Returns:
            List of standardized post dictionaries
        """
        try:
            print(f"🟢 [Blog] Starting scrape for: {blog_url}")

            skip = {u.split("?")[0].rstrip("/") for u in (skip_urls or set())}

            # Over-fetch, then drop what we already stored, so a run still
            # returns `limit` genuinely new articles.
            if sitemap_url:
                found = await self._seed_from_sitemap(
                    sitemap_url, url_glob, min_path_segments, limit + len(skip)
                )
                fresh = [u for u in found if u.split("?")[0].rstrip("/") not in skip]
                seeds = fresh[:limit]
                print(
                    f"🗺️  [Blog] Seeded {len(seeds)} article URLs from sitemap"
                    + (f" ({len(found) - len(fresh)} already stored, skipped)" if skip else "")
                )
            else:
                found = await self._seed_from_index(
                    blog_url, url_glob, limit + len(skip), min_path_segments
                )
                if not found:
                    print(
                        "🎭 [Blog] No links found over plain HTTP — "
                        "retrying the index page with a rendered browser"
                    )
                    found = await self._seed_from_index(
                        blog_url, url_glob, limit + len(skip), min_path_segments, render=True
                    )
                fresh = [u for u in found if u.split("?")[0].rstrip("/") not in skip]
                seeds = fresh[:limit]
                print(
                    f"🗂️  [Blog] Seeded {len(seeds)} newest article URLs from the index"
                    + (f" ({len(found) - len(fresh)} already stored, skipped)" if skip else "")
                )

            targets = seeds or [blog_url]

            # Cap concurrent browser contexts — unbounded gather() here
            # would open one Chromium tab per article at once.
            sem = asyncio.Semaphore(4)

            async def _fetch_one(u: str):
                async with sem:
                    try:
                        return await page_fetcher.fetch_article(u, TIMEOUTS["blog"])
                    except Exception as e:
                        print(f"⚠️  [Blog] Could not fetch {u} ({type(e).__name__}): {e}")
                        return None

            fetched = await asyncio.gather(*[_fetch_one(u) for u in targets[:limit]])

            posts = []
            for item in fetched:
                if not item:
                    continue
                text = item["markdown"]
                extra = {
                    "title": item["title"],
                    "source": blog_url,
                    "word_count": len(text.split()),
                }
                post = self._format_post(
                    rank=len(posts) + 1,
                    post_url=item["url"],
                    text=text[:5000],
                    author=item["author"],
                    published_at=item["published_at"],
                    engagement={},
                    media=[],
                    extra=extra,
                )
                posts.append(post)

            print(f"✅ [Blog] Scraped {len(posts)} posts")
            return posts

        except Exception as e:
            print(f"❌ [Blog] Error: {e}")
            return self._error_response(str(e))
