"""Scheduled digest emails — the recipient list, and building + sending one
company's digest to it.

The scheduler's "digest" job (scheduler.py) calls send_digest() for every
company with at least one recipient: it generates a digest from all posts
stored in the last SCHEDULE_DIGEST_SINCE_DAYS (not just the newest run's)
and mails it, one message per recipient so nobody sees anyone else's
address. Companies nobody receives are skipped before any LLM call.

Recipients live in output/digest_recipients.json — shared by the web server
(GET/POST /api/digest-recipients) and the scheduler container, so writes
take the same cross-process file lock as run history:

    [{"email": "a@x.com", "name": "Ann", "active": true, "companies": []}, ...]

An empty `companies` list means every company.
"""
import json
import os
import re
from html import escape
from typing import Any, Dict, List

import file_lock
from paths import OUTPUT_DIR

RECIPIENTS_PATH = os.path.join(OUTPUT_DIR, "digest_recipients.json")
_LOCK_PATH = RECIPIENTS_PATH + ".lock"
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")

# Same subject line the frontend's "Send" button uses.
SUBJECT_PREFIX = "Newsletter Sales AI Agent | Weekly Update | "


# ─── Recipient list ──────────────────────────────────────────────
def load_recipients() -> List[Dict[str, Any]]:
    try:
        with open(RECIPIENTS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_recipients(items: Any) -> List[Dict[str, Any]]:
    """Validate and replace the whole list; returns it as stored. Raises
    ValueError with a message fit to show the user."""
    if not isinstance(items, list):
        raise ValueError("recipients must be a list")
    clean, seen = [], set()
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"recipient {i} must be an object")
        email = str(item.get("email") or "").strip()
        if not _EMAIL_RE.match(email):
            raise ValueError(f"\"{email or '(blank)'}\" is not a valid email address")
        if email.lower() in seen:
            raise ValueError(f"{email} is listed twice")
        seen.add(email.lower())
        companies = item.get("companies") or []
        if not isinstance(companies, list) or not all(isinstance(c, str) for c in companies):
            raise ValueError(f"companies for {email} must be a list of company keys")
        clean.append({
            "email": email,
            "name": str(item.get("name") or "").strip()[:120],
            "active": item.get("active", True) is not False,
            "companies": sorted({c.strip() for c in companies if c.strip()}),
        })

    with file_lock.locked(_LOCK_PATH):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        tmp = RECIPIENTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, RECIPIENTS_PATH)
    return clean


def recipients_for(company_key: str) -> List[str]:
    """Active recipients who get this company's digest."""
    return [r["email"] for r in load_recipients()
            if r.get("active", True) and (not r.get("companies") or company_key in r["companies"])]


# ─── The email ───────────────────────────────────────────────────
# A port of buildEmailHtml() in frontend/index.html, so a scheduled digest
# looks exactly like one sent from the app — keep the two in step.
_BRAND = "#6366f1"
_PRIORITY = {
    "high": ("#dc2626", "#fee2e2"),
    "medium": ("#ca8a04", "#fef9c3"),
    "low": ("#16a34a", "#dcfce7"),
}


def _section_heading(emoji: str, badge_bg: str, title: str) -> str:
    return ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:32px 0 14px;"><tr>'
            f'<td style="width:26px;height:26px;background:{badge_bg};border-radius:7px;text-align:center;'
            f'font-size:13px;line-height:26px;">{emoji}</td>'
            f'<td style="padding-left:9px;font-size:14.5px;font-weight:700;color:#0f172a;">{title}</td>'
            '</tr></table>')


def email_html(email: Dict[str, Any], company_name: str) -> str:
    color, bg = _PRIORITY.get(email.get("priority"), (_BRAND, "#e0e7ff"))

    paras = [p for p in re.split(r"\n\n+", email.get("body") or "") if p]
    body_html = "".join(
        f'<p style="margin:0 0 16px;line-height:1.65;'
        f'{"font-size:15.5px;color:#0f172a;" if i == 0 else "font-size:14.5px;color:#334155;"}">'
        f'{escape(p).replace(chr(10), "<br>")}</p>'
        for i, p in enumerate(paras)
    )

    points_html = ""
    points = email.get("talking_points") or []
    if points:
        rows = []
        for i, p in enumerate(points):
            src = (f' <a href="{escape(p["source_url"])}" style="color:{_BRAND};font-size:12px;'
                   'font-weight:600;text-decoration:none;">source &#8599;</a>') if p.get("source_url") else ""
            ch = ('<span style="display:inline-block;margin-right:8px;padding:2px 8px;border-radius:99px;'
                  'background:#f1f5f9;color:#64748b;font-size:11px;font-weight:600;text-transform:uppercase;'
                  f'letter-spacing:.02em;">{escape(p["channel"])}</span>') if p.get("channel") else ""
            border = "" if i == 0 else "border-top:1px solid #f1f5f9;"
            rows.append(
                f'<tr><td style="padding:12px 0;{border}">'
                '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
                f'<td valign="top" style="width:20px;color:{_BRAND};font-size:15px;font-weight:700;">&#8594;</td>'
                f'<td style="color:#1e293b;font-size:14px;line-height:1.55;">{escape(p.get("point") or "")}'
                + (f'<div style="margin-top:6px;">{ch}{src}</div>' if ch or src else "")
                + '</td></tr></table></td></tr>')
        points_html = (_section_heading("&#128172;", "#e0e7ff", "Talking points")
                       + '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
                       + "".join(rows) + "</table>")

    caps_html = ""
    caps = email.get("capability_opportunities") or []
    if caps:
        cards = []
        for c in caps:
            cards.append(
                '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
                'style="margin-bottom:12px;background:#f0fdf4;border-radius:8px;">'
                '<tr><td style="border-left:3px solid #22c55e;padding:14px 16px;border-radius:0 8px 8px 0;">'
                f'<div style="font-weight:700;color:#15803d;font-size:14px;">{escape(c.get("offering") or "")}</div>'
                + (f'<div style="margin-top:3px;"><span style="display:inline-block;padding:2px 8px;border-radius:99px;'
                   f'background:#dcfce7;color:#166534;font-size:11px;font-weight:600;">{escape(c["theme"])}</span></div>'
                   if c.get("theme") else "")
                + (f'<div style="margin-top:8px;color:#1e293b;font-size:13.5px;line-height:1.55;">{escape(c["pitch"])}</div>'
                   if c.get("pitch") else "")
                + (f'<div style="margin-top:8px;padding-left:10px;border-left:2px solid #bbf7d0;color:#475569;'
                   f'font-size:13px;font-style:italic;line-height:1.5;">&ldquo;{escape(c["supporting_quote"])}&rdquo;</div>'
                   if c.get("supporting_quote") else "")
                + (f'<div style="margin-top:8px;"><a href="{escape(c["source_url"])}" style="color:#16a34a;'
                   'font-size:12px;font-weight:600;text-decoration:none;">source &#8599;</a></div>'
                   if c.get("source_url") else "")
                + "</td></tr></table>")
        caps_html = _section_heading("&#127919;", "#dcfce7", "Capability opportunities") + "".join(cards)

    prio_badge = ""
    if email.get("priority"):
        prio_badge = (f'<span style="display:inline-block;padding:4px 12px;border-radius:99px;background:{bg};'
                      f'color:{color};font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;">'
                      f'{escape(email["priority"])} priority</span>')
        if email.get("confidence"):
            prio_badge += (f'<span style="margin-left:8px;color:rgba(255,255,255,.75);font-size:12px;">'
                           f'confidence: {escape(email["confidence"])}</span>')

    return (
        '<div style="background:#eef2ff;padding:32px 16px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:14px;overflow:hidden;">'
        f'<tr><td style="background:{_BRAND};padding:28px 32px;">'
        '<div style="font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;'
        'color:rgba(255,255,255,.65);">Sales AI Agent &middot; Weekly Update</div>'
        f'<div style="font-size:24px;font-weight:800;color:#ffffff;margin-top:8px;">{escape(company_name)}</div>'
        + (f'<div style="margin-top:14px;">{prio_badge}</div>' if prio_badge else "")
        + '</td></tr>'
        f'<tr><td style="padding:28px 32px 8px;">{body_html}{points_html}{caps_html}</td></tr>'
        '<tr><td style="padding:20px 32px;background:#f8fafc;border-top:1px solid #eef2ff;font-size:11.5px;color:#94a3b8;">'
        'Sent from the Account Intelligence Engine &middot; weekly digest.</td></tr>'
        '</table></div>'
    )


def email_text(email: Dict[str, Any]) -> str:
    """Plain-text alternative for clients that don't render HTML."""
    lines = [email.get("body") or ""]
    points = email.get("talking_points") or []
    if points:
        lines += ["", "Talking points:"]
        lines += [f"- {p.get('point', '')}" + (f" ({p['source_url']})" if p.get("source_url") else "")
                  for p in points]
    return "\n".join(lines)


# ─── One company's run ───────────────────────────────────────────
def send_digest(target: Dict[str, Any], since_days: int) -> Dict[str, Any]:
    """Generate `target`'s digest from the last `since_days` of posts and
    mail it to its recipients. Returns run-history updates (runner.py
    records them); never raises for an expected failure."""
    from digest.pipeline import run as run_digest
    from digest.llm_client import LLMError
    from mailer import send_email, MailerError

    name = target.get("display_name") or target["key"]
    to = recipients_for(target["key"])
    if not to:
        return {"success": True, "status": "success", "digest": {"skipped": "no recipients"}}

    try:
        digest = run_digest(kind="company", target=dict(target), new_only=False, since_days=since_days)
    except RuntimeError as e:
        # "Nothing to summarise" — a quiet week, not a failure; no mail.
        return {"success": True, "status": "success",
                "digest": {"skipped": f"no posts in the last {since_days} days ({e})"}}
    except (LLMError, FileNotFoundError) as e:
        return {"success": False, "status": "failed", "error": f"Digest failed: {e}"}

    email = digest.get("email") or {}
    subject = SUBJECT_PREFIX + name
    html, text = email_html(email, name), email_text(email)
    sent, errors = [], []
    for addr in to:
        try:
            send_email(addr, subject, text, html=html)
            sent.append(addr)
        except MailerError as e:
            errors.append(f"{addr}: {e}")

    updates = {
        "success": bool(sent),
        "status": "success" if sent else "failed",
        "digest": {
            "llm": digest.get("llm"),
            "posts_considered": digest.get("posts_considered"),
            "input_tokens": digest.get("input_tokens", 0),
            "output_tokens": digest.get("output_tokens", 0),
            "emailed_to": sent,
        },
    }
    if errors:
        updates["error"] = "Email not sent to " + "; ".join(errors)
    return updates
