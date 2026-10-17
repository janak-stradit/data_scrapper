"""Reddit post scraper via Reddit's own free public RSS/Atom feeds — no
Apify actor, no per-run cost.

Same interface (RedditScraper.scrape(identifier, limit, keywords, exclude))
as the Apify-backed version it replaces, so engine.py needs no changes.

Reddit's JSON API (reddit.com/.../.json) is blocked outright as of this
writing — verified live: every .json request returns HTTP 403 with a
Cloudflare-style challenge page, regardless of User-Agent, from this
pipeline's own network path. The much older .rss/Atom feeds (the only
programmatic access Reddit had before its JSON API existed) are NOT
blocked and still return real data — verified live against both
reddit.com/search.rss and a subreddit's .rss feed.

Tradeoff versus the old actor: Atom has no fields for score/upvotes/
comment counts, so `engagement` is always empty here. Everything else —
title, body, author, subreddit, permalink, timestamp — comes through the
same as before, and post_url stays the canonical reddit.com permalink, so
dedup keys line up with posts already stored from the old actor.
"""
import asyncio
import html
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from xml.etree import ElementTree

import aiohttp

from config import DEFAULT_POST_LIMIT, TIMEOUTS
from .base_scraper import BaseScraper

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
TAG_RE = re.compile(r"<[^>]+>")
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# Every entry's <content> ends with "submitted by /u/x to r/y [link]
# [comments]" boilerplate Reddit appends to every RSS item — stripped so
# `text` holds only the actual post body.
BOILERPLATE_RE = re.compile(
    r"\s*submitted\s+by\s+/?u/\S+\s+to\s+/?r/\S+\s*(\[link\])?\s*(\[comments\])?\s*$",
    re.IGNORECASE,
)

# A real browser UA is markedly less likely to be challenged than a bare
# library default — Reddit's guidance asks for a descriptive one regardless.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class RedditScraper(BaseScraper):
    """Scrapes last N submissions from a Reddit user, subreddit, URL, or search query."""

    def __init__(self):
        super().__init__("reddit")

    @staticmethod
    def _as_query(identifier: str) -> Optional[str]:
        """Return a search query if the identifier is free text, else None."""
        ident = identifier.strip()
        if ident.startswith(("http://", "https://", "r/", "u/")):
            return None
        return ident if any(c in ident for c in " \"'") else None

    @staticmethod
    def _listing_rss_url(identifier: str, count: int) -> str:
        """Turn a username, subreddit, or full URL into its .rss listing URL.

        A URL that's already a ready-made .rss feed — e.g. a CRM record's
        osint_feed_manifest.feeds.reddit_rss_url, which already has
        ?q=...&sort=... baked in — is used as-is plus a limit param,
        rather than treated like a bare profile/subreddit URL that still
        needs ".rss" appended. Doing that unconditionally would turn
        ".../search.rss?q=x" into the broken ".../search.rss?q=x.rss?limit=20".
        """
        ident = identifier.strip()
        if ".rss" in ident:
            if "limit=" in ident:
                return ident
            sep = "&" if "?" in ident else "?"
            return f"{ident}{sep}limit={count}"
        if ident.startswith("http://") or ident.startswith("https://"):
            base = ident.rstrip("/")
        elif ident.startswith("r/"):
            base = f"https://www.reddit.com/{ident.rstrip('/')}/new"
        elif ident.startswith("u/"):
            base = f"https://www.reddit.com/user/{ident[2:].rstrip('/')}/submitted"
        else:
            base = f"https://www.reddit.com/user/{ident.rstrip('/')}/submitted"
        return f"{base}.rss?limit={count}"

    @staticmethod
    def _clean_body(raw_html: str) -> str:
        text = COMMENT_RE.sub("", raw_html or "")
        text = TAG_RE.sub(" ", text)
        # <content> is HTML-escaped-inside-XML, so after XML parsing already
        # unescaped &lt;div&gt; -> <div> (stripped above), what's left can
        # still hold HTML entities like &#39; or &amp;#32; — unescape twice
        # to catch Reddit's occasional double-encoding (&amp;#32; -> &#32; -> ' ').
        text = html.unescape(html.unescape(text))
        text = BOILERPLATE_RE.sub("", text)
        return re.sub(r"\s+", " ", text).strip()

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
            List of standardized post dictionaries (engagement always {})
        """
        username = identifier
        try:
            search_query = self._as_query(identifier)
            # Reddit's RSS listing caps out around 100 per request;
            # over-fetch within that cap when keyword-filtering, same
            # reasoning as the old actor's maxItems/maxPostCount.
            fetch_count = min(limit * 3, 100) if keywords else min(limit, 100)

            if search_query:
                url = (
                    "https://www.reddit.com/search.rss"
                    f"?q={quote(search_query)}&sort=relevance&t=year"
                    f"&limit={fetch_count}&type=link"
                )
                print(f"🟠 [Reddit] Starting scrape for: {search_query}")
            else:
                url = self._listing_rss_url(identifier, fetch_count)
                print(f"🟠 [Reddit] Starting scrape for: {url.split('.rss')[0]}")

            timeout = aiohttp.ClientTimeout(total=TIMEOUTS["reddit"])
            headers = {"User-Agent": USER_AGENT}

            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status == 429:
                        return self._error_response(
                            "Reddit rate-limited this request (HTTP 429) — try again shortly"
                        )
                    if resp.status == 403:
                        return self._error_response(
                            "Reddit returned HTTP 403 on its RSS feed — unusual; it may have "
                            "tightened access further since this was last verified"
                        )
                    if resp.status != 200:
                        return self._error_response(f"Reddit returned HTTP {resp.status}")
                    body = await resp.text()

            try:
                root = ElementTree.fromstring(body)
            except ElementTree.ParseError as e:
                return self._error_response(f"Could not parse Reddit RSS: {e}")

            entries = root.findall("atom:entry", ATOM_NS)

            def haystack(title, text, subreddit):
                return f"{title} {text} {subreddit}".lower()

            parsed = []
            for entry in entries:
                title = (entry.findtext("atom:title", "", ATOM_NS) or "").strip()
                link_el = entry.find("atom:link", ATOM_NS)
                permalink = (link_el.get("href") if link_el is not None else "") or ""
                content = self._clean_body(entry.findtext("atom:content", "", ATOM_NS))

                author_el = entry.find("atom:author/atom:name", ATOM_NS)
                author = (author_el.text or "").strip() if author_el is not None else ""
                if author.startswith("/u/"):
                    author = author[3:]
                elif author.startswith("u/"):
                    author = author[2:]

                category_el = entry.find("atom:category", ATOM_NS)
                subreddit = (category_el.get("term") or "").strip() if category_el is not None else ""

                published = (
                    entry.findtext("atom:published", "", ATOM_NS)
                    or entry.findtext("atom:updated", "", ATOM_NS)
                )

                parsed.append({
                    "title": title,
                    "permalink": permalink,
                    "content": content,
                    "author": author,
                    "subreddit": subreddit,
                    "published": published,
                })

            if keywords:
                terms = [k.lower() for k in keywords]
                parsed = [
                    p for p in parsed
                    if any(t in haystack(p["title"], p["content"], p["subreddit"]) for t in terms)
                ]

            if exclude:
                banned = [k.lower() for k in exclude]
                before = len(parsed)
                parsed = [
                    p for p in parsed
                    if not any(b in haystack(p["title"], p["content"], p["subreddit"]) for b in banned)
                ]
                if before - len(parsed):
                    print(f"   [Reddit] Dropped {before - len(parsed)} posts matching excluded terms")

            posts = []
            for idx, p in enumerate(parsed[:limit], start=1):
                post = self._format_post(
                    rank=idx,
                    post_url=p["permalink"],
                    text=p["content"] or p["title"],
                    author=p["author"] or username,
                    published_at=p["published"],
                    engagement={},
                    media=[],
                    extra={
                        "title": p["title"],
                        "subreddit": f"r/{p['subreddit']}" if p["subreddit"] else "",
                        "is_self_post": bool(p["content"]),
                    },
                )
                posts.append(post)

            print(f"✅ [Reddit] Scraped {len(posts)} posts")
            return posts

        except asyncio.TimeoutError:
            print("❌ [Reddit] Error: request timed out")
            return self._error_response("Reddit request timed out")
        except Exception as e:
            print(f"❌ [Reddit] Error: {e}")
            return self._error_response(str(e))
