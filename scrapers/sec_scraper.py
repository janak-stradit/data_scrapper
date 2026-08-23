"""SEC EDGAR filings scraper.

EDGAR is a free, structured public API — no Apify actor and no compute units.
It only requires a descriptive User-Agent identifying the caller.
"""
import asyncio
from typing import List, Dict, Any

import aiohttp

from config import DEFAULT_POST_LIMIT, SEC_USER_AGENT, TIMEOUTS
from .base_scraper import BaseScraper

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{doc}"

# Forms that carry company news, ranked ahead of routine ownership noise.
MATERIAL_FORMS = {
    "8-K": "Material event",
    "10-K": "Annual report",
    "10-Q": "Quarterly report",
    "DEF 14A": "Proxy statement",
    "S-1": "Registration statement",
    "424B2": "Prospectus",
    "13F-HR": "Institutional holdings",
    "SC 13D": "Beneficial ownership",
    "SCHEDULE 13G": "Passive ownership stake",
}


class SECScraper(BaseScraper):
    """Fetches the latest EDGAR filings for a company CIK."""

    def __init__(self):
        super().__init__("sec")

    async def scrape(
        self,
        cik: str,
        limit: int = DEFAULT_POST_LIMIT,
        material_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Fetch the most recent EDGAR filings for a CIK.

        Args:
            cik: 10-digit zero-padded CIK (e.g. "0001390777" for BNY)
            limit: Number of filings to return
            material_only: Keep only news-bearing forms (8-K, 10-K, 10-Q, ...)

        Returns:
            List of standardized post dictionaries, newest first
        """
        try:
            cik = str(cik).strip().zfill(10)
            print(f"🏛️  [SEC EDGAR] Starting fetch for CIK {cik}")

            headers = {
                "User-Agent": SEC_USER_AGENT,
                "Accept-Encoding": "gzip, deflate",
                "Host": "data.sec.gov",
            }
            timeout = aiohttp.ClientTimeout(total=TIMEOUTS["sec"])

            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(SUBMISSIONS_URL.format(cik=cik)) as resp:
                    if resp.status != 200:
                        return self._error_response(
                            f"EDGAR returned HTTP {resp.status} for CIK {cik}"
                        )
                    data = await resp.json()

            company = data.get("name", "")
            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])

            # The parallel arrays in "recent" are already newest-first.
            rows = []
            for i, form in enumerate(forms):
                if material_only and form.upper() not in MATERIAL_FORMS:
                    continue
                rows.append(
                    {
                        "form": form,
                        "filing_date": recent["filingDate"][i],
                        "report_date": recent.get("reportDate", [""] * len(forms))[i],
                        "accession": recent["accessionNumber"][i],
                        "primary_doc": recent.get("primaryDocument", [""] * len(forms))[i],
                        "description": recent.get("primaryDocDescription", [""] * len(forms))[i],
                        "items": recent.get("items", [""] * len(forms))[i],
                    }
                )
                if len(rows) >= limit:
                    break

            cik_int = int(cik)
            posts = []
            for idx, row in enumerate(rows, start=1):
                accession_nodash = row["accession"].replace("-", "")
                url = FILING_INDEX_URL.format(
                    cik_int=cik_int,
                    accession_nodash=accession_nodash,
                    doc=row["primary_doc"] or f"{row['accession']}-index.htm",
                )

                label = MATERIAL_FORMS.get(row["form"].upper(), "Filing")
                text_parts = [label]
                # The description often just repeats the form name — skip that.
                desc = (row["description"] or "").strip()
                if desc and desc.upper() != row["form"].upper():
                    text_parts.append(desc)
                if row["items"]:
                    text_parts.append(f"Items: {row['items']}")
                if row["report_date"]:
                    text_parts.append(f"Period covered: {row['report_date']}")

                post = self._format_post(
                    rank=idx,
                    post_url=url,
                    text="\n".join(text_parts),
                    author=company,
                    published_at=row["filing_date"],
                    engagement={},
                    media=[],
                    extra={
                        "title": f"{row['form']} filed {row['filing_date']}",
                        "form": row["form"],
                        "form_label": label,
                        "accession": row["accession"],
                        "report_date": row["report_date"],
                        "cik": cik,
                    },
                )
                posts.append(post)

            print(f"✅ [SEC EDGAR] Fetched {len(posts)} filings")
            return posts

        except asyncio.TimeoutError:
            print("❌ [SEC EDGAR] Error: request timed out")
            return self._error_response("EDGAR request timed out")
        except Exception as e:
            print(f"❌ [SEC EDGAR] Error: {e}")
            return self._error_response(str(e))
