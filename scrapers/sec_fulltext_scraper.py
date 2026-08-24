"""SEC EDGAR full-text search — finds a name mentioned across ALL filers'
filings, not just one company's own (that's sec_scraper.py, which only
fetches a single CIK's own submissions).

Free, no signup, no Apify actor — efts.sec.gov is the same backend behind
EDGAR's own full-text search UI (sec.gov/edgar/search), and needs only
the same descriptive User-Agent sec_scraper.py already requires.

Real, verified noise problem worth knowing before using this: for a
large custodian bank or asset manager, most hits are boilerplate — their
name appears as custodian/sub-adviser/index-benchmark in thousands of
unrelated funds' routine administrative filings (485BPOS, N-CEN, N-PX,
497), not genuine third-party coverage. Searching "Robin Vince" (a
specific person) returned ~170 hits; searching "BNY Mellon" (the company)
returned 10,000+. This channel is far more useful for individual names
than for large, ubiquitous company names — see CHANNEL_GUIDANCE in
digest/prompts.py for how the digest step is told to read it.
"""
import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List

import aiohttp

from config import DEFAULT_POST_LIMIT, SEC_USER_AGENT, TIMEOUTS
from .base_scraper import BaseScraper

SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"


class SECFullTextScraper(BaseScraper):
    """Finds a name mentioned across every EDGAR filer's filings."""

    def __init__(self):
        super().__init__("sec_mentions")

    async def scrape(
        self, query: str, limit: int = DEFAULT_POST_LIMIT, days: int = 90
    ) -> List[Dict[str, Any]]:
        """
        Search EDGAR full-text for a name, across all filers.

        Args:
            query: name to search for, e.g. "Robin Vince" or "BNY Mellon"
            limit: number of matches to return
            days: only search filings from the last N days

        Returns:
            List of standardized post dictionaries, newest first
        """
        try:
            print(f"🔎 [SEC Full-Text] Starting search for: {query}")

            end = datetime.utcnow().date()
            start = end - timedelta(days=days)

            headers = {"User-Agent": SEC_USER_AGENT}
            timeout = aiohttp.ClientTimeout(total=TIMEOUTS["sec_mentions"])
            params = {
                "q": f'"{query}"',
                "dateRange": "custom",
                "startdt": start.isoformat(),
                "enddt": end.isoformat(),
            }

            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(SEARCH_URL, params=params) as resp:
                    if resp.status != 200:
                        return self._error_response(
                            f"EDGAR full-text search returned HTTP {resp.status}"
                        )
                    data = await resp.json()

            hits = data.get("hits", {}).get("hits", [])
            total = data.get("hits", {}).get("total", {}).get("value", len(hits))

            posts = []
            for idx, hit in enumerate(hits[:limit], start=1):
                src = hit.get("_source", {})
                doc_id = hit.get("_id", "")
                accession = src.get("adsh", "")
                cik_raw = (src.get("ciks") or [""])[0]
                filename = doc_id.split(":", 1)[1] if ":" in doc_id else ""

                try:
                    cik_int = int(cik_raw) if cik_raw else None
                except ValueError:
                    cik_int = None
                accession_nodash = accession.replace("-", "")
                if cik_int and accession_nodash and filename:
                    url = (
                        f"https://www.sec.gov/Archives/edgar/data/"
                        f"{cik_int}/{accession_nodash}/{filename}"
                    )
                else:
                    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_raw}"

                filer = (src.get("display_names") or [""])[0]
                form = src.get("form", "")
                description = src.get("file_description") or ""

                post = self._format_post(
                    rank=idx,
                    post_url=url,
                    text=f"{form}" + (f" — {description}" if description else f" filed by {filer}"),
                    author=filer,
                    published_at=src.get("file_date"),
                    engagement={},
                    media=[],
                    extra={
                        "form": form,
                        "filer": filer,
                        "accession": accession,
                        "total_matches": total,
                    },
                )
                posts.append(post)

            print(f"✅ [SEC Full-Text] Found {len(posts)} of {total} total matches (last {days}d)")
            return posts

        except asyncio.TimeoutError:
            print("❌ [SEC Full-Text] Error: request timed out")
            return self._error_response("EDGAR full-text search timed out")
        except Exception as e:
            print(f"❌ [SEC Full-Text] Error: {e}")
            return self._error_response(str(e))
