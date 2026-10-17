"""Structured identity for a person target — name parts, title, company —
used by the person-only channels that search public records by name
(web_search, sec_proxy, brokercheck, uk_directorships).

Most people targets come from the CRM and only carry a display name like
"Arun K. (Vice President, The Bank of New York Mellon Corporation)", so
this parses that shape. Explicit target fields ("full_name", "title",
"company") win when set — add "full_name" to a target once you know an
abbreviated person's real surname and every name-based channel improves.

`abbreviated` matters: a surname cut to an initial ("Arun K.") can't be
searched precisely, so channels that would otherwise attach a stranger's
records to this person (proxy statements, directorships) skip it.
"""
import re
from typing import Any, Dict, List, Optional

from config import COMPANIES_HOUSE_API_KEY

# Words that say nothing about WHICH company this is — matching on them
# would link "The Bank of New York Mellon" to "New York Life".
_GENERIC_COMPANY_WORDS = {
    "the", "and", "of", "for", "bank", "banks", "corporation", "corp", "company", "co",
    "inc", "incorporated", "llc", "ltd", "limited", "plc", "group", "holdings",
    "holding", "financial", "services", "capital", "securities", "international",
    "national", "association", "trust", "america", "american", "global", "new",
    "york", "investment", "investments", "management", "partners", "advisors",
    "asset", "assets", "markets", "wealth", "fund", "funds", "na", "n.a", "us",
    "usa", "uk", "europe", "emea", "apac", "asia", "north", "south", "east",
    "west", "de", "sa", "ag", "gmbh",
}

_COMPANY_SUFFIX = re.compile(
    r"[,\s]+(corporation|corp\.?|inc\.?|incorporated|limited|ltd\.?|llc|plc|company|co\.?|group|holdings)$",
    re.I,
)


# Subsidiaries/brands whose names share no word with the parent — a BNY
# person registered only through Pershing LLC is still a BNY employee.
_COMPANY_ALIASES = {
    "mellon": {"bny", "pershing"},
    "bny": {"mellon", "pershing"},
}


def company_tokens(company: str) -> set:
    """Distinctive words of a company name, for fuzzy employer matching —
    "The Bank of New York Mellon Corporation" -> {"mellon", "bny", "pershing"},
    "BNY MELLON CAPITAL MARKETS, LLC" -> {"bny", "mellon", "pershing"}."""
    words = re.findall(r"[a-z0-9]+", (company or "").lower())
    tokens = {w for w in words if len(w) >= 3 and w not in _GENERIC_COMPANY_WORDS}
    for t in list(tokens):
        tokens |= _COMPANY_ALIASES.get(t, set())
    return tokens


def companies_match(a: str, b_tokens: set) -> bool:
    return bool(b_tokens) and bool(company_tokens(a) & b_tokens)


def _short_company(company: str) -> str:
    c = re.sub(r"^the\s+", "", (company or "").strip(), flags=re.I)
    prev = None
    while prev != c:
        prev, c = c, _COMPANY_SUFFIX.sub("", c).strip()
    return c


def parse(target: Dict[str, Any]) -> Dict[str, Any]:
    display = (target.get("display_name") or target.get("key") or "").strip()
    name, title, company = display, target.get("title") or "", target.get("company") or ""

    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", display)
    if m:
        name, inner = m.group(1).strip(), m.group(2)
        cut = inner.rfind(",")
        if not title:
            title = (inner[:cut] if cut != -1 else inner).strip()
        if not company and cut != -1:
            company = inner[cut + 1:].strip()

    name = (target.get("full_name") or name).strip()
    parts = [p for p in re.split(r"\s+", name) if p]
    first = parts[0] if parts else ""
    last = parts[-1].rstrip(".") if len(parts) > 1 else ""
    # "Arun K." / "Arun K" — the surname is just an initial.
    abbreviated = len(last) <= 1

    return {
        "name": name,
        "first": first,
        "last": last,
        "abbreviated": abbreviated,
        "title": title,
        "company": company,
        "company_short": _short_company(company),
        "company_tokens": company_tokens(company),
    }


def channel_inputs(target: Dict[str, Any]) -> Dict[str, Optional[Any]]:
    """What each person-only public-records channel should be run with, or
    None when that channel can't be run responsibly for this person.
    A target can switch one off by setting e.g. "web_search_query": "" ."""
    ident = parse(target)
    out: Dict[str, Optional[Any]] = {}

    def override(field):
        return target[field] if field in target else None

    # Web search — always possible: a list of queries, most specific first,
    # tried in turn until one has results. An abbreviated name leads with
    # its title too (otherwise far too ambiguous), falling back to name +
    # company when that's too narrow to match anything.
    web = override("web_search_query")
    if web is None and ident["first"]:
        quoted = f'"{ident["first"]} {ident["last"]}"' if ident["last"] else f'"{ident["name"]}"'
        company = [ident["company_short"]] if ident["company_short"] else []
        web = []
        if ident["abbreviated"] and ident["title"]:
            # Unquoted on purpose: verified live that `Carl B. Chief Financial
            # Officer BNY` surfaces "Carl Bennett - Chief Financial Officer at
            # BNY" (so the full surname becomes findable), where the quoted
            # "Carl B" form matched nothing.
            web.append(" ".join([f'{ident["first"]} {ident["last"]}.', ident["title"]] + company))
        web.append(" ".join([quoted] + company))
    out["web_search"] = web or None

    # Proxy statements — full names only; "Arun K." would match anyone.
    proxy = override("sec_proxy_query")
    if proxy is None and not ident["abbreviated"]:
        proxy = f'{ident["first"]} {ident["last"]}'
    out["sec_proxy"] = proxy or None

    # BrokerCheck — only with a known employer to confirm the match against.
    out["brokercheck"] = ident if ident["company_tokens"] and ident["first"] else None
    if override("brokercheck") is False:
        out["brokercheck"] = None

    # UK directorships — full name + a Companies House API key.
    out["uk_directorships"] = (
        ident if (COMPANIES_HOUSE_API_KEY and not ident["abbreviated"] and ident["first"]) else None
    )
    if override("uk_directorships") is False:
        out["uk_directorships"] = None

    return out


def configured_channels(target: Dict[str, Any]) -> List[str]:
    return [ch for ch, v in channel_inputs(target).items() if v]
