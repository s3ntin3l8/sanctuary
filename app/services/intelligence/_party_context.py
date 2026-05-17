"""Party-identity context block injected into all originator-classification AI stages."""


def format_party_context(
    own_self: str,
    own_parties: list[str],
    opposing_parties: list[str],
) -> str:
    """Return a prompt-injection block for party-identity context.

    own_self: the user's own full name
    own_parties: user's lawyers / own-side firms (global)
    opposing_parties: opposing party names + their counsel (per-case)

    Returns an empty string when no parties are configured so callers can
    safely prepend it without adding noise to an unconfigured instance.
    """
    own_self = (own_self or "").strip()
    own_parties = [p.strip() for p in (own_parties or []) if p and str(p).strip()]
    opposing = [p.strip() for p in (opposing_parties or []) if p and str(p).strip()]

    if not own_self and not own_parties and not opposing:
        return ""

    own_names = [n for n in ([own_self] + own_parties) if n]

    lines = [
        "### Known Party Identity (authoritative — use these to set originator_type):"
    ]
    if own_names:
        lines.append(f"- YOUR SIDE (originator_type=own): {', '.join(own_names)}")
    if opposing:
        lines.append(
            f"- OPPOSING SIDE (originator_type=opposing): {', '.join(opposing)}"
        )
    lines += [
        "When a document's author matches a name on YOUR SIDE → originator_type=own.",
        "When it matches OPPOSING SIDE → originator_type=opposing.",
        "These override your general reasoning — treat them as authoritative facts.",
        "Note: originator_type=own means the document was produced BY the user's side,",
        "not simply self-authored in the grammatical sense.",
    ]
    return "\n".join(lines)
