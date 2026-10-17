"""UK Companies House — company directorships and other officer roles a
person holds or has held in UK-registered companies (board seats,
company secretary, subsidiaries such as "Pershing International Nominees
Limited").

Free, but needs a key: the Companies House REST API requires a (free)
API key — COMPANIES_HOUSE_API_KEY in .env. Without it this channel is
not scheduled at all (see person_identity.channel_inputs).

Officer search is by name only, so matching is strict: the officer's
first name and surname must both match exactly, and then either (a) one
of their appointments is at a company matching this person's employer,
or (b) they're the ONLY exact-name officer found. Anything else is too
ambiguous to attribute and is dropped.
"""
import asyncio
import re
from typing import Any, Dict, List

import aiohttp

from config import COMPANIES_HOUSE_API_KEY, DEFAULT_POST_LIMIT, TIMEOUTS
from person_identity import companies_match
from .base_scraper import BaseScraper

API = "https://api.company-information.service.gov.uk"
COMPANY_PAGE = "https://find-and-update.company-information.service.gov.uk/company/{number}/officers"
MAX_OFFICERS = 10


def _officer_name_ok(title: str, ident: Dict[str, Any]) -> bool:
    # Titles look like "Robin Antony VINCE" or "VINCE, Robin Antony".
    words = [w.lower() for w in re.findall(r"[A-Za-z'-]+", title or "")]
    return ident["first"].lower() in words and ident["last"].lower() in words


class UKDirectorshipsScraper(BaseScraper):
    def __init__(self):
        super().__init__("uk_directorships")

    async def scrape(self, ident: Dict[str, Any], limit: int = DEFAULT_POST_LIMIT) -> List[Dict[str, Any]]:
        if not COMPANIES_HOUSE_API_KEY:
            return self._error_response("COMPANIES_HOUSE_API_KEY is not set in .env")
        name = f'{ident["first"]} {ident["last"]}'
        try:
            print(f"🇬🇧 [Companies House] Searching officers: {name}")
            timeout = aiohttp.ClientTimeout(total=TIMEOUTS["uk_directorships"])
            auth = aiohttp.BasicAuth(COMPANIES_HOUSE_API_KEY, "")
            async with aiohttp.ClientSession(timeout=timeout, auth=auth) as session:
                async with session.get(f"{API}/search/officers", params={"q": name, "items_per_page": "50"}) as resp:
                    if resp.status == 401:
                        return self._error_response("Companies House rejected COMPANIES_HOUSE_API_KEY (HTTP 401)")
                    if resp.status != 200:
                        return self._error_response(f"Companies House returned HTTP {resp.status}")
                    data = await resp.json()

                officers = [
                    o for o in data.get("items", [])
                    if _officer_name_ok(o.get("title"), ident)
                ][:MAX_OFFICERS]

                appointments_by_officer = []
                for o in officers:
                    link = (o.get("links") or {}).get("self")
                    if not link:
                        continue
                    async with session.get(f"{API}{link}", params={"items_per_page": "50"}) as resp:
                        if resp.status != 200:
                            continue
                        appts = (await resp.json()).get("items", [])
                    appointments_by_officer.append((o, appts))

            employer_match = [
                (o, appts) for o, appts in appointments_by_officer
                if any(companies_match((a.get("appointed_to") or {}).get("company_name", ""), ident["company_tokens"])
                       for a in appts)
            ]
            if employer_match:
                keep = employer_match
            elif len(appointments_by_officer) == 1:
                keep = appointments_by_officer
            else:
                keep = []

            posts = []
            for officer, appts in keep:
                for a in appts:
                    company = a.get("appointed_to") or {}
                    number = company.get("company_number", "")
                    role = (a.get("officer_role") or "").replace("-", " ")
                    status = f"resigned {a['resigned_on']}" if a.get("resigned_on") else "current"
                    posts.append(self._format_post(
                        rank=len(posts) + 1,
                        post_url=COMPANY_PAGE.format(number=number),
                        text=(
                            f"{officer.get('title')}: {role} of {company.get('company_name')} "
                            f"(company {number}, {company.get('company_status') or 'status unknown'}) — "
                            f"appointed {a.get('appointed_on') or 'date unknown'}, {status}."
                        ),
                        author="UK Companies House",
                        published_at=a.get("appointed_on"),
                        extra={
                            "company_name": company.get("company_name"),
                            "company_number": number,
                            "company_status": company.get("company_status"),
                            "role": role,
                            "appointed_on": a.get("appointed_on"),
                            "resigned_on": a.get("resigned_on"),
                            "matched_by": "employer" if employer_match else "only exact-name officer",
                        },
                    ))
                    if len(posts) >= limit:
                        break

            print(f"✅ [Companies House] {len(posts)} appointments "
                  f"({len(officers)} exact-name officers, {len(keep)} attributed)")
            return posts
        except asyncio.TimeoutError:
            return self._error_response("Companies House timed out")
        except Exception as e:
            print(f"❌ [Companies House] Error: {e}")
            return self._error_response(str(e))
