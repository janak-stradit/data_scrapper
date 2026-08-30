"""
╔══════════════════════════════════════════════════════════════════════╗
║           APIFY SOCIAL SCRAPER ENGINE (Python)                       ║
║  One call → Scrapes LinkedIn, Reddit, Twitter/X, and Blog           ║
║  Returns unified JSON with last N posts per platform                ║
╚══════════════════════════════════════════════════════════════════════╝

Usage:
    python engine.py bny --limit 10
    python engine.py northern_trust --limit 10 --out ntrs.json

    # Or import in your project:
    from engine import ApifyScraperEngine
    engine = ApifyScraperEngine()
    result = await engine.scrape_company("bny", limit=10)
"""
import asyncio
import json
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

from scrapers import (
    LinkedInScraper,
    RedditScraper,
    TwitterScraper,
    BlogScraper,
    SECScraper,
    GoogleNewsScraper,
    PatentsScraper,
    RSSScraper,
    YouTubeScraper,
    SECFullTextScraper,
    RegulatoryScraper,
    LinkedInJobsScraper,
)
from targets import resolve as resolve_company, COMPANY_TARGETS
from people_targets import resolve as resolve_person, PEOPLE_TARGETS
from paths import store_path
from store import ScrapeStore
import db


class ApifyScraperEngine:
    """
    Orchestrates multi-platform social scraping via Apify Actors.

    Each platform has its own scraper module. This engine runs them
    in parallel and returns a unified JSON response.
    """

    def __init__(self):
        self.linkedin = LinkedInScraper()
        self.reddit = RedditScraper()
        self.twitter = TwitterScraper()
        self.blog = BlogScraper()
        self.sec = SECScraper()
        self.news = GoogleNewsScraper()
        self.patents = PatentsScraper()
        self.rss = RSSScraper()
        self.youtube = YouTubeScraper()
        self.sec_mentions = SECFullTextScraper()
        self.regulatory = RegulatoryScraper()
        self.linkedin_jobs = LinkedInJobsScraper()

    async def scrape_all(
        self,
        linkedin_url: Optional[str] = None,
        reddit_username: Optional[str] = None,
        twitter_handle: Optional[str] = None,
        blog_url: Optional[str] = None,
        limit: int = 10,
        blog_glob: Optional[str] = None,
        blog_sitemap: Optional[str] = None,
        blog_min_segments: Optional[int] = None,
        reddit_keywords: Optional[List[str]] = None,
        reddit_exclude: Optional[List[str]] = None,
        sec_cik: Optional[str] = None,
        news_query: Optional[str] = None,
        blog_skip_urls: Optional[set] = None,
        blog_seed_from_index: bool = False,
        news_days: int = 30,
        news_exclude: Optional[str] = None,
        patents_query: Optional[str] = None,
        rss_url: Optional[str] = None,
        youtube_channel_id: Optional[str] = None,
        sec_mentions_query: Optional[str] = None,
        regulatory_query: Optional[str] = None,
        linkedin_jobs_query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Scrape all specified platforms in parallel.

        Args:
            linkedin_url: Full LinkedIn profile URL
            reddit_username: Reddit username (without u/)
            twitter_handle: Twitter/X handle (with or without @)
            blog_url: Blog homepage or RSS feed URL
            sec_cik: 10-digit SEC CIK for EDGAR filings
            news_query: Google News search query
            rss_url: Any RSS/Atom feed URL
            youtube_channel_id: YouTube channel ID (starts with "UC")
            sec_mentions_query: Name to search across ALL EDGAR filers'
                filings (distinct from sec_cik, which is one company's
                own filings) — noisy for large/ubiquitous names, best
                for individuals or narrower company names
            regulatory_query: Name to match against Federal Reserve and
                OCC press/enforcement-action feeds
            linkedin_jobs_query: Company name or LinkedIn company URL to
                filter open job postings by
            limit: Number of posts per platform (default: 10)

        Returns:
            Unified JSON object with results from all platforms
        """
        start_time = time.time()

        result = {
            "success": True,
            "query": {
                "linkedin_url": linkedin_url,
                "reddit_username": reddit_username,
                "twitter_handle": twitter_handle,
                "blog_url": blog_url,
                "sec_cik": sec_cik,
                "news_query": news_query,
                "patents_query": patents_query,
                "rss_url": rss_url,
                "youtube_channel_id": youtube_channel_id,
                "sec_mentions_query": sec_mentions_query,
                "regulatory_query": regulatory_query,
                "linkedin_jobs_query": linkedin_jobs_query,
                "limit": limit,
                "requested_at": datetime.utcnow().isoformat() + "Z",
            },
            "data": {},
            "metadata": {
                "total_posts": 0,
                "platforms_scraped": [],
                "platforms_failed": [],
                "execution_time_ms": 0,
            },
        }

        # Build list of scraping tasks
        tasks = []
        task_map = {}  # Maps task index to platform name

        if linkedin_url:
            tasks.append(self.linkedin.scrape(linkedin_url, limit))
            task_map[len(tasks) - 1] = "linkedin"

        if reddit_username:
            tasks.append(
                self.reddit.scrape(
                    reddit_username, limit, reddit_keywords, reddit_exclude
                )
            )
            task_map[len(tasks) - 1] = "reddit"

        if twitter_handle:
            tasks.append(self.twitter.scrape(twitter_handle, limit))
            task_map[len(tasks) - 1] = "twitter"

        if blog_url:
            tasks.append(
                self.blog.scrape(
                    blog_url,
                    limit,
                    blog_glob,
                    blog_sitemap,
                    blog_min_segments,
                    blog_skip_urls,
                    blog_seed_from_index,
                )
            )
            task_map[len(tasks) - 1] = "blog"

        if sec_cik:
            tasks.append(self.sec.scrape(sec_cik, limit))
            task_map[len(tasks) - 1] = "sec"

        if news_query:
            tasks.append(
                self.news.scrape(news_query, limit, news_days, news_exclude)
            )
            task_map[len(tasks) - 1] = "news"

        if patents_query:
            tasks.append(self.patents.scrape(patents_query, limit))
            task_map[len(tasks) - 1] = "patents"

        if rss_url:
            tasks.append(self.rss.scrape(rss_url, limit))
            task_map[len(tasks) - 1] = "rss"

        if youtube_channel_id:
            tasks.append(self.youtube.scrape(youtube_channel_id, limit))
            task_map[len(tasks) - 1] = "youtube"

        if sec_mentions_query:
            tasks.append(self.sec_mentions.scrape(sec_mentions_query, limit))
            task_map[len(tasks) - 1] = "sec_mentions"

        if regulatory_query:
            tasks.append(self.regulatory.scrape(regulatory_query, limit))
            task_map[len(tasks) - 1] = "regulatory"

        if linkedin_jobs_query:
            tasks.append(self.linkedin_jobs.scrape(linkedin_jobs_query, limit))
            task_map[len(tasks) - 1] = "linkedin_jobs"

        if not tasks:
            result["success"] = False
            result["data"]["error"] = "No platforms specified for scraping."
            return result

        # Execute all scrapers in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        for idx, platform in task_map.items():
            res = results[idx]

            if isinstance(res, Exception):
                result["data"][platform] = {
                    "success": False,
                    "error": str(res),
                    "count": 0,
                    "posts": [],
                }
                result["metadata"]["platforms_failed"].append(platform)
            elif isinstance(res, list) and res and res[0].get("error"):
                result["data"][platform] = {
                    "success": False,
                    "error": res[0].get("message", "Unknown error"),
                    "count": 0,
                    "posts": [],
                }
                result["metadata"]["platforms_failed"].append(platform)
            else:
                posts = res if isinstance(res, list) else []
                result["data"][platform] = {
                    "success": True,
                    "count": len(posts),
                    "posts": posts,
                }
                result["metadata"]["platforms_scraped"].append(platform)
                result["metadata"]["total_posts"] += len(posts)

        result["metadata"]["execution_time_ms"] = int((time.time() - start_time) * 1000)
        result["success"] = len(result["metadata"]["platforms_failed"]) == 0

        return result


    async def scrape_company(
        self,
        company: str = None,
        limit: int = 10,
        include_newsroom: bool = True,
        only: Optional[List[str]] = None,
        store: Optional["ScrapeStore"] = None,
        target: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Scrape every platform for a company — either a known key (see
        targets.py) or an ad-hoc `target` dict with the same shape, for a
        one-off run that was never registered there.

        Args:
            company: company key or alias, e.g. "bny" or "northern_trust"
                (ignored if `target` is given)
            limit: posts per platform
            include_newsroom: also scrape the corporate press-release page
            only: restrict to these channels, e.g. ["sec", "news"]
            store: existing store — narrows what the paid actors are asked for
                and lets already-stored URLs be skipped
            target: a pre-built target dict (must include "key") to scrape
                as-is instead of resolving `company` from targets.py

        Returns:
            Same unified JSON as scrape_all, plus a "company" block.
        """
        target = target or resolve_company(company)
        wanted = set(only) if only else None

        # Incremental hints: ask for a smaller window and skip known URLs.
        news_days = (
            store.days_since_last_run(channel="news")
            if store and store.exists
            else 30
        )
        skip_blog = store.seen_urls("blog") if store else set()
        skip_newsroom = store.seen_urls("newsroom") if store else set()
        if store and store.exists:
            print(
                f"♻️  [Store] {store.doc['metadata'].get('total_posts', 0)} posts on file; "
                f"asking news/social for the last {news_days}d and skipping "
                f"{len(skip_blog) + len(skip_newsroom)} known article URLs"
            )

        def want(channel):
            return wanted is None or channel in wanted

        result = await self.scrape_all(
            linkedin_url=target.get("linkedin_url") if want("linkedin") else None,
            reddit_username=target.get("reddit_query") if want("reddit") else None,
            twitter_handle=target.get("twitter_handle") if want("twitter") else None,
            blog_url=target.get("blog_url") if want("blog") else None,
            limit=limit,
            blog_glob=target.get("blog_glob"),
            blog_sitemap=target.get("blog_sitemap"),
            blog_min_segments=target.get("blog_min_segments"),
            blog_skip_urls=skip_blog,
            blog_seed_from_index=bool(target.get("blog_seed_from_index")),
            news_days=news_days,
            reddit_keywords=target.get("reddit_keywords"),
            reddit_exclude=target.get("reddit_exclude"),
            sec_cik=target.get("sec_cik") if want("sec") else None,
            news_query=(target.get("news_query") or target.get("reddit_query"))
            if want("news")
            else None,
            news_exclude=target.get("news_exclude"),
            rss_url=target.get("rss_url") if want("rss") else None,
            youtube_channel_id=target.get("youtube_channel_id") if want("youtube") else None,
            sec_mentions_query=target.get("sec_mentions_query") if want("sec_mentions") else None,
            regulatory_query=target.get("regulatory_query") if want("regulatory") else None,
            linkedin_jobs_query=target.get("linkedin_jobs_query") if want("linkedin_jobs") else None,
        )

        if include_newsroom and want("newsroom") and target.get("newsroom_url"):
            # scrape_all flags "no platforms" when newsroom is the only channel.
            result["data"].pop("error", None)
            news = await self.blog.scrape(
                target.get("newsroom_url"),
                limit,
                target.get("newsroom_glob"),
                target.get("newsroom_sitemap"),
                target.get("newsroom_min_segments"),
                skip_newsroom,
                bool(target.get("newsroom_seed_from_index")),
            )
            if isinstance(news, list) and news and news[0].get("error"):
                result["data"]["newsroom"] = {
                    "success": False,
                    "error": news[0].get("message", "Unknown error"),
                    "count": 0,
                    "posts": [],
                }
                result["metadata"]["platforms_failed"].append("newsroom")
            else:
                for post in news:
                    post["platform"] = "newsroom"
                result["data"]["newsroom"] = {
                    "success": True,
                    "count": len(news),
                    "posts": news,
                }
                result["metadata"]["platforms_scraped"].append("newsroom")
                result["metadata"]["total_posts"] += len(news)
            result["success"] = len(result["metadata"]["platforms_failed"]) == 0

        result["company"] = {
            "key": target["key"],
            "display_name": target.get("display_name", target["key"]),
            "ticker": target.get("ticker"),
        }
        result["query"]["company"] = target["key"]
        result["query"]["reddit_query"] = target.get("reddit_query")
        result["query"]["newsroom_url"] = target.get("newsroom_url") if include_newsroom else None
        result["query"]["sec_cik"] = target.get("sec_cik")
        return result

    async def scrape_person(
        self,
        person: str = None,
        limit: int = 10,
        only: Optional[List[str]] = None,
        store: Optional["ScrapeStore"] = None,
        target: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Scrape every applicable channel for a person — either a known key
        (see people_targets.py) or an ad-hoc `target` dict with the same
        shape, for a one-off run that was never registered there.

        No blog/newsroom here — those are company-owned content sections and
        don't apply to an individual.

        Args:
            person: person key or alias, e.g. "ranjit_samra"
                (ignored if `target` is given)
            limit: posts per platform
            only: restrict to these channels, e.g. ["linkedin", "news"]
            store: existing store — narrows what the paid actors are asked for
                and lets already-stored URLs be skipped
            target: a pre-built target dict (must include "key") to scrape
                as-is instead of resolving `person` from people_targets.py

        Returns:
            Same unified JSON as scrape_all, plus a "person" block.
        """
        target = target or resolve_person(person)
        wanted = set(only) if only else None

        news_days = (
            store.days_since_last_run(channel="news")
            if store and store.exists
            else 30
        )
        if store and store.exists:
            print(
                f"♻️  [Store] {store.doc['metadata'].get('total_posts', 0)} posts on file; "
                f"asking news/social for the last {news_days}d"
            )

        def want(channel):
            return wanted is None or channel in wanted

        result = await self.scrape_all(
            linkedin_url=target.get("linkedin_url") if want("linkedin") else None,
            reddit_username=target.get("reddit_query") if want("reddit") else None,
            twitter_handle=target.get("twitter_handle") if want("twitter") else None,
            limit=limit,
            news_days=news_days,
            reddit_keywords=target.get("reddit_keywords"),
            reddit_exclude=target.get("reddit_exclude"),
            sec_cik=target.get("sec_cik") if want("sec") else None,
            news_query=(target.get("news_query") or target.get("reddit_query"))
            if want("news")
            else None,
            patents_query=target.get("patents_query") if want("patents") else None,
            rss_url=target.get("rss_url") if want("rss") else None,
            youtube_channel_id=target.get("youtube_channel_id") if want("youtube") else None,
            sec_mentions_query=target.get("sec_mentions_query") if want("sec_mentions") else None,
            regulatory_query=target.get("regulatory_query") if want("regulatory") else None,
        )

        # store.merge() keys the stored identity block off result["company"];
        # mirrored here under "person" too so callers can tell them apart
        # without touching store.py.
        identity = {"key": target["key"], "display_name": target.get("display_name", target["key"])}
        result["person"] = identity
        result["company"] = identity
        result["query"]["person"] = target["key"]
        result["query"]["reddit_query"] = target.get("reddit_query")
        result["query"]["sec_cik"] = target.get("sec_cik")
        return result


async def scrape_and_store(
    target_key: str = None,
    limit: int = 10,
    only: Optional[List[str]] = None,
    include_newsroom: bool = True,
    out_path: Optional[str] = None,
    reset_channels: Optional[List[str]] = None,
    use_store: bool = True,
    kind: str = "company",
    target: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Scrape one company or person and fold the result into its store.

    kind: "company" (targets.py, the default) or "person" (people_targets.py).
    Shared by engine.py's CLI and main.py, so both behave identically.

    Pass `target` (a dict with at least "key") to scrape an ad-hoc target
    that isn't registered in targets.py/people_targets.py — used by the
    frontend's Run Pipeline page, which accepts a target as raw JSON.

    Returns the stored document (or the raw result when use_store is False).
    """
    target = target or (
        resolve_person(target_key) if kind == "person" else resolve_company(target_key)
    )
    path = out_path or store_path(target["key"])

    store = ScrapeStore(path) if use_store else None
    if store is not None and reset_channels:
        store.force_full = set(reset_channels)
        for channel in reset_channels:
            dropped = len(store.doc.get("data", {}).pop(channel, {}).get("posts", []))
            print(f"🗑️   [Store] Cleared {dropped} stored {channel} posts")

    engine = ApifyScraperEngine()
    if kind == "person":
        result = await engine.scrape_person(limit=limit, only=only, store=store, target=target)
    else:
        result = await engine.scrape_company(
            limit=limit,
            include_newsroom=include_newsroom,
            only=only,
            store=store,
            target=target,
        )

    if store is None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
        db.upsert_target(kind, target)
        db.upsert_posts(target["key"], kind, result.get("data", {}))
        return result

    merged, stats = store.merge(result)
    store.save()
    for channel, counts in sorted(stats.items()):
        if counts["new"] or counts["duplicate"] or counts.get("filtered"):
            filtered_note = f", {counts['filtered']} filtered" if counts.get("filtered") else ""
            print(
                f"   {channel:<9} +{counts['new']} new, "
                f"{counts['duplicate']} already stored{filtered_note}"
            )
    print(
        f"🆕  {merged['metadata']['new_last_run']} new posts, "
        f"{merged['metadata']['duplicates_skipped']} duplicates skipped "
        f"(store now holds {merged['metadata']['total_posts']})"
    )
    db.upsert_target(kind, target)
    db.upsert_posts(target["key"], kind, merged.get("data", {}))
    return merged


# ═══════════════════════════════════════════════════════════════════════
# CLI / Standalone Usage
# ═══════════════════════════════════════════════════════════════════════

async def main():
    """CLI: python engine.py [company] [--limit N] [--no-newsroom] [--out FILE]"""
    import argparse
    import json
    import os

    parser = argparse.ArgumentParser(
        description="Scrape all platforms for a company (LinkedIn, Reddit, X, blog, newsroom)."
    )
    parser.add_argument(
        "company",
        nargs="?",
        default="bny",
        help=f"company key or alias (known: {', '.join(sorted(COMPANY_TARGETS))}); "
        f"or a person key with --person (known: {', '.join(sorted(PEOPLE_TARGETS)) or 'none configured yet'})",
    )
    parser.add_argument(
        "--person",
        action="store_true",
        help="treat the target as a person from people_targets.py instead of a company "
        "(no blog/newsroom channels)",
    )
    parser.add_argument("--limit", type=int, default=10, help="posts per platform")
    parser.add_argument("--no-newsroom", action="store_true", help="skip the press-release page")
    parser.add_argument("--out", help="write the JSON result to this file")
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="dump the full JSON to stdout (default: print a summary only)",
    )
    parser.add_argument(
        "--only",
        help="comma-separated channels to scrape "
        "(linkedin,reddit,twitter,blog,newsroom,sec,news,patents,rss,"
        "youtube,sec_mentions,regulatory,linkedin_jobs)",
    )
    parser.add_argument(
        "--reset-channel",
        help="comma-separated channels to clear from the store before merging "
        "(use after changing a query, to drop stale results)",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="disable incremental storage (re-scrape and overwrite everything)",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="merge the scraped channels into an existing --out file "
        "instead of replacing it",
    )
    args = parser.parse_args()

    only = [c.strip() for c in args.only.split(",")] if args.only else None
    resolve_target = resolve_person if args.person else resolve_company
    out_path = args.out or store_path(resolve_target(args.company)["key"])

    # The output file is the store: re-runs add only what is genuinely new.
    store = None if args.no_store else ScrapeStore(out_path)

    if store is not None and args.reset_channel:
        store.force_full = {c.strip() for c in args.reset_channel.split(",")}
        for channel in (c.strip() for c in args.reset_channel.split(",")):
            dropped = len(store.doc.get("data", {}).pop(channel, {}).get("posts", []))
            print(f"🗑️   [Store] Cleared {dropped} stored {channel} posts")

    engine = ApifyScraperEngine()
    if args.person:
        response = await engine.scrape_person(
            args.company, limit=args.limit, only=only, store=store,
        )
    else:
        response = await engine.scrape_company(
            args.company,
            limit=args.limit,
            include_newsroom=not args.no_newsroom,
            only=only,
            store=store,
        )

    if store is not None:
        response, stats = store.merge(response)
        store.save()
        for channel, s_ in sorted(stats.items()):
            if s_["new"] or s_["duplicate"] or s_.get("filtered"):
                filtered_note = f", {s_['filtered']} filtered" if s_.get("filtered") else ""
                print(
                    f"   {channel:<9} +{s_['new']} new, {s_['duplicate']} already stored{filtered_note}"
                )
        print(
            f"🆕  {response['metadata']['new_last_run']} new posts, "
            f"{response['metadata']['duplicates_skipped']} duplicates skipped "
            f"(store now holds {response['metadata']['total_posts']})"
        )
    elif args.merge and args.out and os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            existing = json.load(fh)
        merged_data = {**existing.get("data", {}), **response["data"]}
        response["data"] = merged_data
        response["query"] = {**existing.get("query", {}), **response["query"]}
        response["metadata"]["total_posts"] = sum(
            b.get("count", 0) for b in merged_data.values()
        )
        print(f"🔀  Merged {', '.join(response['data'].keys())} into {args.out}")

    payload = json.dumps(response, indent=2, ensure_ascii=False)
    print("\n" + "=" * 70)
    print(f"📦  {response['company']['display_name']}")
    print("=" * 70)
    if args.print_json:
        print(payload)
        print("=" * 70)
    else:
        for channel, block in sorted(response["data"].items()):
            if not isinstance(block, dict):
                continue
            new = block.get("new_last_run")
            flag = f"  (+{new} new)" if new else ""
            print(f"  {channel:<9} {block.get('count', 0):>4} posts{flag}")
    meta = response["metadata"]
    print(
        f"total_posts={meta['total_posts']}  "
        f"ok={meta['platforms_scraped']}  failed={meta['platforms_failed']}  "
        f"{meta['execution_time_ms']}ms"
    )

    if args.out or args.no_store:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
    print(f"💾  Saved to {out_path}")

    return response


if __name__ == "__main__":
    asyncio.run(main())
