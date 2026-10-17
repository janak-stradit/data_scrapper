"""SEC proxy statements (DEF 14A) that name a person — the official
source for a senior executive's or director's bio, title history, other
board seats, and compensation.

Free, no key: same EDGAR full-text search backend as
sec_fulltext_scraper.py, restricted to form DEF 14A and searched over
several years (proxies are annual). The newest matching proxy is also
downloaded and the passages naming the person are pulled out as
`excerpts`, so the actual bio text is stored, not just a link.

Only run for full names (see person_identity.channel_inputs) — a
quoted abbreviated name like "Arun K." would match strangers.
"""
import asyncio
import html as html_lib
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List

import aiohttp

from config import DEFAULT_POST_LIMIT, SEC_USER_AGENT, TIMEOUTS
from .base_scraper import BaseScraper

SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
LOOKBACK_DAYS = 365 * 5
EXCERPT_CHARS = 700
MAX_EXCERPTS = 4
# Words that mark a passage as biography rather than a passing mention
# (a letter signature, a table-of-contents line, a vote tally).
_BIO_WORDS = re.compile(
    r"\b(age[ds]?|joined|served|serves|serving|prior to|previously|appointed|"
    r"elected|director since|career|experience|degree|university|graduated|"
    r"responsible for|oversees|leads|committee|chief|officer|head of|president)\b",
    re.I,
)


def _doc_url(hit: Dict[str, Any]) -> str:
    src = hit.get("_source", {})
    doc_id = hit.get("_id", "")
    filename = doc_id.split(":", 1)[1] if ":" in doc_id else ""
    try:
        cik = int((src.get("ciks") or [""])[0])
    except ValueError:
        cik = None
    accession = (src.get("adsh") or "").replace("-", "")
    if cik and accession and filename:
        return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{filename}"
    return ""


def _excerpts(document_html: str, name: str) -> List[str]:
    """The passages naming the person that read most like a bio, in
    document order — scored by bio vocabulary, so a signature line or a
    table-of-contents entry loses to "…joined BNY in 2020, previously…".
    Proxies name someone in full once, then say "Mr./Ms. <surname>", so
    both forms are searched."""
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", document_html))
    text = re.sub(r"\s+", " ", text)
    surname = name.split()[-1]
    pattern = re.compile(
        re.escape(name) + r"|\b(?:Mr|Ms|Mrs|Dr)\.? " + re.escape(surname) + r"\b", re.I
    )
    windows, last_end = [], -1
    for m in pattern.finditer(text):
        if m.start() < last_end:
            continue
        start = max(0, m.start() - 150)
        end = min(len(text), m.start() + EXCERPT_CHARS)
        passage = text[start:end].strip()
        windows.append((len(_BIO_WORDS.findall(passage)), start, passage))
        last_end = end
    best = sorted(windows, key=lambda w: -w[0])[:MAX_EXCERPTS]
    return ["…" + p + "…" for _, _, p in sorted(best, key=lambda w: w[1])]


class SECProxyScraper(BaseScraper):
    def __init__(self):
        super().__init__("sec_proxy")

    async def scrape(self, name: str, limit: int = DEFAULT_POST_LIMIT) -> List[Dict[str, Any]]:
        try:
            print(f"🏛️ [SEC Proxy] Searching DEF 14A filings for: {name}")
            end = datetime.utcnow().date()
            params = {
                "q": f'"{name}"',
                "forms": "DEF 14A",
                "dateRange": "custom",
                "startdt": (end - timedelta(days=LOOKBACK_DAYS)).isoformat(),
                "enddt": end.isoformat(),
            }
            timeout = aiohttp.ClientTimeout(total=TIMEOUTS["sec_proxy"])
            async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": SEC_USER_AGENT}) as session:
                async with session.get(SEARCH_URL, params=params) as resp:
                    if resp.status != 200:
                        return self._error_response(f"EDGAR full-text search returned HTTP {resp.status}")
                    data = await resp.json()

                hits = sorted(
                    data.get("hits", {}).get("hits", []),
                    key=lambda h: h.get("_source", {}).get("file_date") or "",
                    reverse=True,
                )[:limit]

                posts = []
                for idx, hit in enumerate(hits, start=1):
                    src = hit.get("_source", {})
                    url = _doc_url(hit)
                    if not url:
                        continue
                    filer = (src.get("display_names") or [""])[0]
                    filer_short = re.sub(r"\s*\(.*$", "", filer).strip()
                    excerpts: List[str] = []
                    if idx == 1:
                        # Newest proxy only — these documents run to several MB.
                        try:
                            async with session.get(url) as doc:
                                if doc.status == 200:
                                    excerpts = _excerpts(await doc.text(errors="ignore"), name)
                        except (aiohttp.ClientError, asyncio.TimeoutError):
                            pass
                    text = f"Proxy statement (DEF 14A) filed by {filer_short} naming {name}"
                    if excerpts:
                        text += ":\n\n" + "\n\n".join(excerpts)
                    posts.append(self._format_post(
                        rank=idx,
                        post_url=url,
                        text=text,
                        author=filer_short,
                        published_at=src.get("file_date"),
                        extra={
                            "form": src.get("form"),
                            "filer": filer,
                            "accession": src.get("adsh"),
                            "excerpts": excerpts,
                        },
                    ))

            print(f"✅ [SEC Proxy] {len(posts)} proxy statements")
            return posts
        except asyncio.TimeoutError:
            return self._error_response("EDGAR proxy search timed out")
        except Exception as e:
            print(f"❌ [SEC Proxy] Error: {e}")
            return self._error_response(str(e))
