"""Markdown rendering for a generated digest."""
from typing import Any, Dict


def render_markdown(digest: Dict[str, Any]) -> str:
    """Human-readable version of the digest."""
    email = digest.get("email", {})
    is_person = digest.get("kind") == "person"
    lines = [
        f"# {digest['company']} — {'contact' if is_person else 'account'} digest",
        "",
        f"*Generated {digest['generated_at'][:16]} · {digest['posts_considered']} posts "
        f"across {len(digest['channels'])} channels · {digest['llm']}*",
        "",
        f"## {'Contact briefing' if is_person else 'Sales email'}",
        "",
        f"**Subject:** {email.get('subject', '—')}",
        "",
        f"**Priority:** {email.get('priority', '—')} — {email.get('priority_reason', '')}",
        "",
        f"**Confidence:** {email.get('confidence', '—')}",
        "",
        "```",
        email.get("body", ""),
        "```",
        "",
    ]

    points = email.get("talking_points") or []
    if points:
        lines += ["### Talking points (sourced)", ""]
        lines += [
            f"- {p.get('point', '')}  \n  _{p.get('channel', '')}_ — <{p.get('source_url', '')}>"
            for p in points
        ]
        lines.append("")

    opportunities = email.get("capability_opportunities") or []
    if opportunities:
        lines += ["### Capability opportunities", ""]
        lines += ["| Theme | Offering | Pitch | Source |", "| --- | --- | --- | --- |"]
        lines += [
            f"| {o.get('theme', '')} | {o.get('offering', '')} | {o.get('pitch', '')} "
            f"| [link]({o.get('source_url', '')}) |"
            for o in opportunities
        ]
        lines.append("")
        lines += ["**Supporting quotes:**", ""]
        lines += [
            f"- _{o.get('offering', '')}_ — \"{o.get('supporting_quote', '')}\" "
            f"— <{o.get('source_url', '')}>"
            for o in opportunities
            if o.get("supporting_quote")
        ]
        lines.append("")

    for label, key in (("⚠️ Do not say", "do_not_say"), ("Data gaps", "data_gaps")):
        items = email.get(key) or []
        if items:
            lines += [f"### {label}", ""] + [f"- {i}" for i in items] + [""]

    lines += ["## Channel storylines (social media team)", ""]

    for ch in digest["channels"]:
        story = ch.get("storyline", {}) or {}
        strength = ch.get("evidence_strength", "unrated")
        lines += [
            f"### {ch['channel_label']} · {ch['posts_considered']} posts · "
            f"evidence: {strength}",
            "",
            ch.get("summary", ""),
            "",
        ]

        observed = ch.get("observed") or []
        if observed:
            lines += ["**Observed:**"]
            lines += [
                f"- {o.get('fact', '')} — <{o.get('source_url', '')}>" for o in observed
            ]
            lines.append("")

        if ch.get("interpretation"):
            lines += [f"**Interpretation:** {ch['interpretation']}", ""]
        if ch.get("evidence_note"):
            lines += [f"**Evidence note:** {ch['evidence_note']}", ""]
        if ch.get("do_not_say"):
            lines += ["**⚠️ Do not say:**"] + [f"- {d}" for d in ch["do_not_say"]] + [""]

        notable = ch.get("notable_posts") or []
        if notable and isinstance(notable[0], dict):
            lines += ["**Notable posts:**"]
            lines += [
                f"- [{n.get('headline', '')}]({n.get('source_url', '')}) — {n.get('why', '')}"
                for n in notable
            ]
            lines.append("")

        matches = ch.get("capability_matches") or []
        if matches:
            lines += ["**Capability matches:**"]
            lines += [
                f"- **{m.get('offering', '')}** — {m.get('pitch', '')}  \n"
                f"  \"{m.get('supporting_quote', '')}\" — <{m.get('source_url', '')}>"
                for m in matches
            ]
            lines.append("")

        lines += [
            f"**Themes:** {', '.join(ch.get('themes', []) or []) or '—'}",
            "",
            f"**Sales angle:** {ch.get('sales_angle', '—')}",
            "",
            f"**Hook:** {story.get('hook', '—')}",
            "",
            f"**Angle:** {story.get('angle', '—')}"
            + (f" *(tone: {story['suggested_tone']})*" if story.get("suggested_tone") else ""),
            "",
            "**Post ideas:**",
        ]
        lines += [f"- {idea}" for idea in (story.get("post_ideas") or ["—"])]
        if story.get("avoid"):
            lines += ["", f"**Avoid:** {story['avoid']}"]
        lines.append("")

    return "\n".join(lines)


