"""Blog post scraper using Apify's Website Content Crawler."""
import re
from fnmatch import fnmatch
from typing import List, Dict, Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import aiohttp
from apify_client import ApifyClientAsync

from config import APIFY_TOKEN, ACTORS, TIMEOUTS, DEFAULT_POST_LIMIT
from .base_scraper import BaseScraper

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class BlogScraper(BaseScraper):
    """Scrapes last N posts from a blog using generic web scraping."""

    def __init__(self):
        super().__init__("blog")
        self.client = ApifyClientAsync(APIFY_TOKEN)
        self.actor_id = ACTORS["blog"]

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
        self, index_url: str, url_glob: str, limit: int
    ) -> List[str]:
        """Read article links off an index page, newest first.

        Crawlers follow links in document order, which on a long press-release
        archive means the oldest entries. Reading the links ourselves lets us
        sort by the date in the slug and take the genuinely recent ones.
        """
        try:
            html = await self._fetch(index_url)
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
            seed_from_index: read article links off the index page and take the
                newest by slug date, instead of crawling in document order

        Returns:
            List of standardized post dictionaries
        """
        try:
            print(f"🟢 [Blog] Starting scrape for: {blog_url}")

            skip = {u.split("?")[0].rstrip("/") for u in (skip_urls or set())}

            seeds = []
            if seed_from_index and not sitemap_url:
                found = await self._seed_from_index(
                    blog_url, url_glob, limit + len(skip)
                )
                fresh = [u for u in found if u.split("?")[0].rstrip("/") not in skip]
                seeds = fresh[:limit]
                print(
                    f"🗂️  [Blog] Seeded {len(seeds)} newest article URLs from the index"
                    + (f" ({len(found) - len(fresh)} already stored, skipped)" if skip else "")
                )
            elif sitemap_url:
                # Over-fetch, then drop what we already stored, so a run still
                # returns `limit` genuinely new articles.
                found = await self._seed_from_sitemap(
                    sitemap_url, url_glob, min_path_segments, limit + len(skip)
                )
                fresh = [u for u in found if u.split("?")[0].rstrip("/") not in skip]
                seeds = fresh[:limit]
                print(
                    f"🗺️  [Blog] Seeded {len(seeds)} article URLs from sitemap"
                    + (f" ({len(found) - len(fresh)} already stored, skipped)" if skip else "")
                )

            run_input = {
                "startUrls": [{"url": u} for u in seeds] or [{"url": blog_url}],
                "crawlerType": "playwright:adaptive",
                # Seeded URLs are already articles, so no link-following needed.
                "maxCrawlDepth": 0 if seeds else 2,
                "maxCrawlPages": limit + 2,
                "maxResults": limit + 2,
                "saveMarkdown": True,
                "removeCookieWarnings": True,
                "blockMedia": True,
                "proxyConfiguration": {"useApifyProxy": True},
            }
            if url_glob and not seeds:
                run_input["includeUrlGlobs"] = [{"glob": url_glob}]
            if skip and not seeds:
                # Cap the exclusion list: the actor rejects very large inputs.
                run_input["excludeUrlGlobs"] = [{"glob": u} for u in list(skip)[:200]]

            # 4 GB keeps parallel crawls inside the account memory quota.
            items = await self._run_actor(
                self.client, self.actor_id, run_input, TIMEOUTS["blog"],
                memory_mbytes=4096,
            )

            # Drop the landing page itself; keep the article pages it links to.
            articles = [i for i in items if (i.get("url") or "").rstrip("/") != blog_url.rstrip("/")]
            if min_path_segments:
                deep = [
                    i for i in articles
                    if self._path_depth(i.get("url") or "") >= min_path_segments
                ]
                articles = deep or articles
            articles = articles or items

            posts = []
            for idx, item in enumerate(articles[:limit], start=1):
                meta = item.get("metadata") or {}
                text = item.get("markdown") or item.get("text") or ""

                extra = {
                    "title": item.get("title") or meta.get("title") or meta.get("h1", ""),
                    "source": blog_url,
                    "word_count": len(text.split()),
                }

                post = self._format_post(
                    rank=idx,
                    post_url=item.get("url") or blog_url,
                    text=text[:5000],
                    author=meta.get("author", ""),
                    published_at=item.get("crawl", {}).get("loadedTime")
                    or meta.get("publishedTime")
                    or meta.get("date"),
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
