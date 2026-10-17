"""Daily News Brief — today + yesterday's press coverage, no LLM.

Distinct from the full Sales Email (pipeline.py): that one runs every
stored channel through an LLM to synthesize talking points and costs a
real API call each time it's generated. This is just a filtered,
formatted list of the "news" and "bloomberg" channels' posts published
today or yesterday — free, instant, meant for a quick daily send rather
than a deep account briefing.

    from digest.news_brief import build
    brief = build("bny")
    # brief["subject"], brief["body"] (plain text), brief["html"]
"""
import html as html_lib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from paths import store_path
from people_targets import resolve as resolve_person
from store import ScrapeStore
from targets import resolve as resolve_company

# The two channels that carry third-party press coverage in this pipeline
# (see digest/prompts.py's CHANNEL_GUIDANCE) — everything else is either
# owned content (blog/newsroom/youtube) or a non-news signal (jobs, SEC,
# social chatter), out of scope for a "news brief".
NEWS_CHANNELS = ("news", "bloomberg")


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    """Best-effort parse of a post's published_at into an aware UTC datetime.

    news/bloomberg posts carry Google News RSS's RFC822 pubDate (e.g.
    "Tue, 16 Sep 2026 08:15:00 GMT") — parsedate_to_datetime handles that.
    A handful of other channels use ISO 8601, tried as a fallback in case
    this ever runs against a channel outside NEWS_CHANNELS.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw.replace("Z", "").split(".")[0], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def collect(store: ScrapeStore, channels=NEWS_CHANNELS, now: datetime = None) -> List[Dict[str, Any]]:
    """Every post from `channels` published today or yesterday (UTC),
    newest first. Undated posts are dropped — unlike the full digest's
    _recent() (which keeps them), a "what happened in the last 2 days"
    brief with no reliable date on a post is worse than just omitting it.
    """
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    posts = []
    for channel in channels:
        for post in store.doc.get("data", {}).get(channel, {}).get("posts", []):
            when = _parse_date(post.get("published_at"))
            if when is not None and start <= when <= now:
                item = dict(post)
                item["channel"] = channel
                item["_when"] = when
                posts.append(item)

    posts.sort(key=lambda p: p["_when"], reverse=True)
    for p in posts:
        del p["_when"]
    return posts


def _text_body(display_name: str, posts: List[Dict[str, Any]], date_label: str) -> str:
    lines = [f"Daily News Brief — {display_name} — {date_label}", ""]
    if not posts:
        lines.append("No news or Bloomberg coverage in the last 2 days.")
        return "\n".join(lines)
    for p in posts:
        title = p.get("title") or p.get("text") or "(untitled)"
        source = p.get("source") or p.get("author") or ""
        label = "Bloomberg" if p.get("channel") == "bloomberg" else "Google News"
        url = p.get("post_url") or ""
        lines.append(f"- [{label}] {title}" + (f" — {source}" if source else ""))
        if url:
            lines.append(f"  {url}")
    return "\n".join(lines)


def _html_body(display_name: str, posts: List[Dict[str, Any]], date_label: str) -> str:
    """Same branded email shell as the Sales Email (see buildEmailHtml() in
    frontend/index.html): indigo header bar, table-based layout (mail
    clients — Outlook especially — don't reliably support flex/div-only
    layouts), same footer line. Kept here in Python rather than only in
    JS so the CLI's `news-brief --send` sends the identical look, not a
    plainer fallback.
    """
    esc = html_lib.escape
    brand = "#6366f1"

    if not posts:
        rows_html = (
            "<tr><td style='padding:28px 32px;color:#64748b;font-size:14px;'>"
            "No news or Bloomberg coverage in the last 2 days."
            "</td></tr>"
        )
    else:
        items = []
        for i, p in enumerate(posts):
            title = esc(p.get("title") or p.get("text") or "(untitled)")
            source = esc(p.get("source") or p.get("author") or "")
            url = esc(p.get("post_url") or "")
            label = "Bloomberg" if p.get("channel") == "bloomberg" else "Google News"
            border = "" if i == 0 else "border-top:1px solid #f1f5f9;"
            source_html = f"<span style='color:#94a3b8;font-size:12px;'>{source}</span>" if source else ""
            items.append(
                f"<tr><td style='padding:14px 0;{border}'>"
                "<table role='presentation' cellpadding='0' cellspacing='0' border='0'><tr>"
                f"<td valign='top' style='width:20px;color:{brand};font-size:15px;font-weight:700;'>&#8594;</td>"
                "<td style='color:#1e293b;font-size:14px;line-height:1.55;'>"
                f"<a href='{url}' style='color:#0f172a;font-weight:700;text-decoration:none;'>{title}</a>"
                f"<div style='margin-top:6px;'>"
                "<span style='display:inline-block;margin-right:8px;padding:2px 8px;border-radius:99px;"
                "background:#f1f5f9;color:#64748b;font-size:11px;font-weight:600;text-transform:uppercase;"
                f"letter-spacing:.02em;'>{esc(label)}</span>{source_html}</div>"
                "</td></tr></table></td></tr>"
            )
        rows_html = (
            "<tr><td style='padding:28px 32px 8px;'>"
            "<table role='presentation' cellpadding='0' cellspacing='0' border='0' width='100%'>"
            + "".join(items) +
            "</table></td></tr>"
        )

    return (
        "<div style='background:#eef2ff;padding:32px 16px;"
        "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'>"
        "<table role='presentation' cellpadding='0' cellspacing='0' border='0' width='100%' "
        "style='max-width:600px;margin:0 auto;background:#ffffff;border-radius:14px;overflow:hidden;'>"
        f"<tr><td style='background:{brand};padding:28px 32px;'>"
        "<div style='font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;"
        "color:rgba(255,255,255,.65);'>Sales AI Agent &middot; Daily News Brief</div>"
        f"<div style='font-size:24px;font-weight:800;color:#ffffff;margin-top:8px;'>{esc(display_name)}</div>"
        f"<div style='margin-top:10px;color:rgba(255,255,255,.85);font-size:13px;'>"
        f"{esc(date_label)} &middot; today + yesterday</div>"
        "</td></tr>"
        + rows_html +
        "<tr><td style='padding:20px 32px;margin-top:20px;background:#f8fafc;border-top:1px solid #eef2ff;"
        "font-size:11.5px;color:#94a3b8;'>Sent from the Account Intelligence Engine.</td></tr>"
        "</table></div>"
    )


def build(
    company_key: str = None,
    kind: str = "company",
    target: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """Build a Daily News Brief for a company or person — today + yesterday's
    news/bloomberg posts, formatted as subject/body/html, ready to send.

    kind: "company" (targets.py, the default) or "person" (people_targets.py).
    Pass `target` (a dict with at least "key") to brief an ad-hoc target
    that isn't registered there, same convention as digest/pipeline.py.

    Raises FileNotFoundError if there's no store yet for this target.
    """
    is_person = kind == "person"
    target = target or (
        resolve_person(company_key) if is_person else resolve_company(company_key)
    )
    key = target["key"]
    target.setdefault("display_name", key)
    path = store_path(key)

    store = ScrapeStore(path)
    if not store.exists:
        cli_flag = " --person" if is_person else ""
        raise FileNotFoundError(
            f"No store at {path}. Run: python main.py scrape {key}{cli_flag} --limit 20"
        )

    posts = collect(store)
    date_label = datetime.now(timezone.utc).strftime("%b %d, %Y")
    display_name = target["display_name"]

    return {
        "company": display_name,
        "company_key": key,
        "kind": kind,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "window": "today + yesterday (UTC)",
        "posts_considered": len(posts),
        "posts": posts,
        "subject": f"Daily News Brief — {display_name} — {date_label}",
        "body": _text_body(display_name, posts, date_label),
        "html": _html_body(display_name, posts, date_label),
    }
