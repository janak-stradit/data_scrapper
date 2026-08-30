"""LinkedIn open-roles scraper using Apify Actor.

Distinct from linkedin_scraper.py (company page posts) — this pulls active
job postings for a company, which is often an earlier and cleaner signal of
a strategic initiative than a press release: a "Blockchain Settlement
Engineer" req shows up before the partnership announcement does.
"""
import asyncio
from typing import List, Dict, Any
from apify_client import ApifyClientAsync

from config import APIFY_TOKEN, ACTORS, TIMEOUTS, DEFAULT_POST_LIMIT
from .base_scraper import BaseScraper


class LinkedInJobsScraper(BaseScraper):
    """Scrapes currently open LinkedIn job postings for one company."""

    def __init__(self):
        super().__init__("linkedin_jobs")
        self.client = ApifyClientAsync(APIFY_TOKEN)
        self.actor_id = ACTORS["linkedin_jobs"]

    async def scrape(self, company: str, limit: int = DEFAULT_POST_LIMIT) -> List[Dict[str, Any]]:
        """
        Scrape open job postings for a company.

        Args:
            company: Company name or LinkedIn company URL (the actor accepts
                either — targets.py reuses linkedin_url by default).
            limit: Number of job postings to fetch (default: 10).

        Returns:
            List of standardized post dictionaries.
        """
        try:
            print(f"💼 [LinkedIn Jobs] Starting scrape for: {company}")

            run_input = {
                "company": [company],
                "maxItems": limit,
                "sortBy": "date",
            }

            items = await self._run_actor(
                self.client, self.actor_id, run_input, TIMEOUTS["linkedin_jobs"]
            )

            posts = []
            for idx, item in enumerate(items[:limit], start=1):
                salary = item.get("salary") or {}
                comp = item.get("company") or {}

                # location is a nested object ({linkedinText, parsed: {...},
                # ...}), not a plain string — flatten to the human-readable
                # text so every downstream reader (digest prompts, frontend,
                # the DB's TEXT column) gets a scalar, not a dict.
                loc = item.get("location")
                if isinstance(loc, dict):
                    location_text = loc.get("linkedinText") or (loc.get("parsed") or {}).get("text") or ""
                else:
                    location_text = loc or ""

                post = self._format_post(
                    rank=idx,
                    post_url=item.get("linkedinUrl") or item.get("applyUrl", ""),
                    text=item.get("descriptionText") or item.get("description", ""),
                    author=comp.get("name") if isinstance(comp, dict) else (comp or ""),
                    published_at=item.get("postedDate") or item.get("postedAt"),
                    engagement={
                        "applicants": item.get("applicants", 0),
                        "views": item.get("views", 0),
                    },
                    extra={
                        "title": item.get("title", ""),
                        "location": location_text,
                        "employment_type": item.get("employmentType", ""),
                        "workplace_type": item.get("workplaceType", ""),
                        "salary": salary if salary else None,
                    },
                )
                posts.append(post)

            print(f"✅ [LinkedIn Jobs] Scraped {len(posts)} postings")
            return posts

        except Exception as e:
            print(f"❌ [LinkedIn Jobs] Error: {e}")
            return self._error_response(str(e))
