"""FINRA BrokerCheck — a registered securities professional's employment
history, licences (exams passed), registration status, and any
disclosures (customer disputes, regulatory or criminal events).

Free, no key: api.brokercheck.finra.org is the JSON API behind
brokercheck.finra.org itself (not a documented public API, so its shape
could change).

Matching is deliberately strict. BrokerCheck is a name-searchable
database that can carry criminal and regulatory disclosures, so a loose
match could attach a stranger's record to this person. A record is only
kept when (1) first name matches, (2) surname matches (or starts with the
initial, for an abbreviated "Arun K."), AND (3) at least one of its
current or previous employers matches this person's company. No employer
match -> no record, never a best guess.
"""
import asyncio
import json
from typing import Any, Dict, List

import aiohttp

from config import DEFAULT_POST_LIMIT, TIMEOUTS
from person_identity import companies_match
from .base_scraper import BaseScraper

SEARCH_URL = "https://api.brokercheck.finra.org/search/individual"
PROFILE_URL = "https://brokercheck.finra.org/individual/summary/{id}"
MAX_CANDIDATES = 12
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def _name_ok(src: Dict[str, Any], ident: Dict[str, Any]) -> bool:
    first = (src.get("ind_firstname") or "").lower()
    last = (src.get("ind_lastname") or "").lower()
    if first != ident["first"].lower():
        return False
    want_last = ident["last"].lower()
    if ident["abbreviated"]:
        return bool(want_last) and last.startswith(want_last)
    return last == want_last


class BrokerCheckScraper(BaseScraper):
    def __init__(self):
        super().__init__("brokercheck")

    async def _detail(self, session, ind_id: str) -> Dict[str, Any]:
        params = {"hl": "true", "includePrevious": "true", "nrows": "12", "query": "", "r": "25", "wt": "json"}
        async with session.get(f"{SEARCH_URL}/{ind_id}", params=params) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json(content_type=None)
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            return {}
        try:
            return json.loads(hits[0]["_source"]["content"])
        except (KeyError, ValueError, TypeError):
            return {}

    async def scrape(self, ident: Dict[str, Any], limit: int = DEFAULT_POST_LIMIT) -> List[Dict[str, Any]]:
        query = f'{ident["first"]} {ident["last"]}'.strip()
        try:
            print(f"🪪 [BrokerCheck] Searching: {query} (employer must match {ident['company_short']})")
            params = {
                "query": query, "hl": "true", "nrows": str(MAX_CANDIDATES),
                "start": "0", "r": "25", "sort": "score desc", "wt": "json",
            }
            timeout = aiohttp.ClientTimeout(total=TIMEOUTS["brokercheck"])
            async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
                async with session.get(SEARCH_URL, params=params) as resp:
                    if resp.status != 200:
                        return self._error_response(f"BrokerCheck returned HTTP {resp.status}")
                    data = await resp.json(content_type=None)

                candidates = [
                    h.get("_source", {}) for h in data.get("hits", {}).get("hits", [])
                    if _name_ok(h.get("_source", {}), ident)
                ]

                posts = []
                for src in candidates:
                    detail = await self._detail(session, src.get("ind_source_id"))
                    if not detail:
                        continue
                    current = detail.get("currentEmployments") or []
                    previous = detail.get("previousEmployments") or []
                    firms = [e.get("firmName") or "" for e in current + previous]
                    if not any(companies_match(f, ident["company_tokens"]) for f in firms):
                        continue
                    # Some detail records omit this; the search hit carries it.
                    basic = detail.setdefault("basicInformation", {})
                    if not basic.get("daysInIndustry"):
                        try:
                            basic["daysInIndustry"] = int(src.get("ind_industry_days") or 0)
                        except ValueError:
                            pass
                    posts.append(self._format_post(
                        rank=len(posts) + 1,
                        post_url=PROFILE_URL.format(id=src.get("ind_source_id")),
                        text=self._summary(detail),
                        author="FINRA BrokerCheck",
                        extra=self._fields(detail),
                    ))
                    if len(posts) >= limit:
                        break

            print(f"✅ [BrokerCheck] {len(posts)} employer-confirmed matches of {len(candidates)} name matches")
            return posts
        except asyncio.TimeoutError:
            return self._error_response("BrokerCheck timed out")
        except Exception as e:
            print(f"❌ [BrokerCheck] Error: {e}")
            return self._error_response(str(e))

    @staticmethod
    def _fields(d: Dict[str, Any]) -> Dict[str, Any]:
        b = d.get("basicInformation") or {}

        def jobs(key):
            return [
                {
                    "firm": e.get("firmName"),
                    "start": e.get("registrationBeginDate"),
                    "end": e.get("registrationEndDate"),
                }
                for e in d.get(key) or []
            ]

        exams = [
            e.get("examName")
            for cat in ("principalExamCategory", "productExamCategory", "stateExamCategory")
            for e in d.get(cat) or []
        ]
        disclosures = [
            {"type": x.get("disclosureType"), "date": x.get("eventDate"), "resolution": x.get("disclosureResolution")}
            for x in d.get("disclosures") or []
        ]
        return {
            "brokercheck_id": b.get("individualId"),
            "registered_name": " ".join(filter(None, (b.get("firstName"), b.get("middleName"), b.get("lastName")))),
            "broker_status": b.get("bcScope"),
            "adviser_status": b.get("iaScope"),
            "years_in_industry": round((b.get("daysInIndustry") or 0) / 365.25, 1),
            "current_employers": jobs("currentEmployments"),
            "previous_employers": jobs("previousEmployments"),
            "exams": exams,
            "disclosures": disclosures,
        }

    @classmethod
    def _summary(cls, d: Dict[str, Any]) -> str:
        f = cls._fields(d)
        lines = [
            f"FINRA BrokerCheck record: {f['registered_name']} — "
            f"broker registration {f['broker_status']}, {f['years_in_industry']} years in the industry."
        ]
        if f["current_employers"]:
            lines.append("Current: " + "; ".join(
                f"{j['firm']} (since {j['start']})" for j in f["current_employers"]))
        if f["previous_employers"]:
            lines.append("Previous: " + "; ".join(
                f"{j['firm']} ({j['start']}–{j['end']})" for j in f["previous_employers"]))
        if f["exams"]:
            lines.append("Exams passed: " + ", ".join(f["exams"]))
        if f["disclosures"]:
            lines.append(f"Disclosures on record: {len(f['disclosures'])} — " + "; ".join(
                f"{x['type']} ({x['date']}, {x['resolution']})" for x in f["disclosures"]))
        else:
            lines.append("No disclosures on record.")
        return "\n".join(lines)
