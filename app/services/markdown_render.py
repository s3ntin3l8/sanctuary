"""Markdown rendering for document bodies, with key-passage highlighting.

Used by the v1 document reader (``body_html``).
"""

from __future__ import annotations

import hashlib
import re

from markdown_it import MarkdownIt
from markupsafe import Markup, escape

from app.services.text_offsets import find_text_offsets

# html=False blocks raw-HTML passthrough — Docling-produced markdown can't inject
# <script> even if the source PDF was adversarial. typographer upgrades straight
# quotes / dashes; linkify auto-links bare URLs. Tables enabled for Schriftsatz
# cost tables.

_md = (
    MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
    .enable("table")
    .enable("strikethrough")
)


def _render_image(self, tokens, idx, options, env) -> str:
    """Pictures become a caption chip.

    The OCR engine describes pictures as `<img alt="...">` and is told not to
    fill `src`, so the markdown is `![alt]()` — a broken-image icon in the
    browser. Stored content never carries a resolvable image, so show the
    description (logo, stamp, signature) as a labelled placeholder instead.
    """
    alt = escape(tokens[idx].content.strip() or "Abbildung")
    return (
        f'<span class="reader-figure" role="img" aria-label="{alt}">'
        f'<span class="reader-figure-icon" aria-hidden="true">image</span>{alt}</span>'
    )


_md.add_render_rule("image", _render_image)


# Highlight sentinels — private-use unicode pairs that survive markdown
# rendering as opaque text and are easy to swap for <mark> tags afterwards.
# Earlier implementations regex-matched AI-quoted text against the rendered
# HTML, which broke whenever markdown escaping (smart quotes, dashes, &<>),
# inline formatting (**bold**, _em_), or paragraph wrapping rewrote the text.
# Splicing into the *raw* markdown sidesteps all of that: offsets are stamped
# at ingest time against doc.content, and sentinels travel with their text
# through the renderer.
_HL_SENT_BEGIN = "HLB{:04d}"
_HL_SENT_END = "HLE{:04d}"
_HL_SENT_PAIR_RE = re.compile(r"HLB(\d{4})(.*?)HLE\1", re.DOTALL)
_HL_SENT_ORPHAN_RE = re.compile(r"HL[BE]\d{4}")


def render_highlighted(
    value: str | None,
    key_passages: list | None = None,
    passage_claim_ids: dict | None = None,
    claim_excerpt_map: dict | None = None,
) -> Markup:
    """Render markdown with key_passages and claim excerpts wrapped in <mark>.

    For each highlight, sentinel pairs are spliced into the *raw* markdown at
    validated offsets, then markdown-it renders the whole thing, then a regex
    swap replaces sentinel pairs with <mark> tags. Highlights that can't be
    located fall back to a hidden anchor at the top of the body so spine
    clicks still have a target.
    """

    raw = str(value) if value else ""
    if not raw:
        return Markup("")
    if not key_passages and not claim_excerpt_map:
        return Markup(_md.render(raw))

    highlights: list[dict] = []  # {start, end, open, close}
    fallback_anchors: list[str] = []  # injected at top when match fails

    # ── Key passages ──────────────────────────────────────────────────────
    for passage in key_passages or []:
        text = (passage.get("text") or "").strip()
        if not text:
            continue
        kind = (passage.get("kind") or "neutral").lower()
        pid = (
            passage.get("id")
            or hashlib.sha1(f"{text}|{kind}".encode()).hexdigest()[:12]
        )

        start = passage.get("start_offset")
        end = passage.get("end_offset")
        valid = (
            isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start < end <= len(raw)
        )
        if not valid:
            offsets = find_text_offsets(raw, text)
            if offsets:
                start, end = offsets
                valid = True

        claim_id = (passage_claim_ids or {}).get(pid)
        claim_anchor = (
            f'<span id="claim-{claim_id}" class="claim-anchor" aria-hidden="true"></span>'
            if claim_id
            else ""
        )
        chip = (
            f'<a href="#claim-{claim_id}" class="hud-claim-chip ml-0.5 text-[10px] no-underline">⚖</a>'
            if claim_id
            else ""
        )

        if not valid:
            # No reliable position — leave a hidden anchor at the top so
            # spine clicks still resolve to *something*. The spine row already
            # surfaces "⚠ approx" for these.
            fallback_anchors.append(
                f'{claim_anchor}<a id="p-{pid}" class="passage-anchor-unmatched" aria-hidden="true"></a>'
            )
            continue

        mark_open = (
            f'{claim_anchor}<mark id="p-{pid}" data-passage-id="{pid}" data-kind="{kind}" '
            f'class="hud-mark hud-mark--{kind} '
            f"bg-[color:var(--color-key-passage-bg)] text-[color:var(--color-key-passage-fg)] "
            f'rounded px-0.5 ring-1 ring-[color:var(--color-key-passage-ring)]">'
        )
        mark_close = f"</mark>{chip}"
        highlights.append(
            {"start": start, "end": end, "open": mark_open, "close": mark_close}
        )

    # ── Independent claim excerpts (amber) ───────────────────────────────
    for claim_id, excerpt in (claim_excerpt_map or {}).items():
        if not excerpt:
            continue
        offsets = find_text_offsets(raw, excerpt)
        if not offsets:
            fallback_anchors.append(
                f'<a id="claim-{claim_id}" class="claim-anchor-unmatched" aria-hidden="true"></a>'
            )
            continue
        start, end = offsets
        mark_open = (
            f'<mark id="claim-{claim_id}" data-claim-id="{claim_id}" '
            f'class="hud-mark hud-mark--claim '
            f"bg-[color:var(--color-claim-bg)] text-[color:var(--color-claim-fg)] "
            f'rounded px-0.5 ring-1 ring-[color:var(--color-claim-ring)]">'
        )
        highlights.append(
            {"start": start, "end": end, "open": mark_open, "close": "</mark>"}
        )

    if not highlights:
        html = _md.render(raw)
        if fallback_anchors:
            html = "".join(fallback_anchors) + html
        return Markup(html)

    # Splice sentinels into raw at every (start, end) pair. We assemble events
    # and walk left-to-right so overlapping/nested ranges all line up against
    # the original offsets — splicing in reverse over a mutating string would
    # mis-align the second range when ranges nest.
    events: list[tuple[int, int, int, str]] = []
    # tuple: (position, priority, idx, sentinel_text)
    # priority: 0 = end-tag (so an end at pos N closes before another's start at N),
    #           1 = start-tag.
    for idx, h in enumerate(highlights):
        events.append((h["start"], 1, idx, _HL_SENT_BEGIN.format(idx)))
        events.append((h["end"], 0, idx, _HL_SENT_END.format(idx)))
    events.sort(key=lambda e: (e[0], e[1]))

    pieces: list[str] = []
    cursor = 0
    for pos, _, _, sentinel in events:
        if pos > cursor:
            pieces.append(raw[cursor:pos])
        pieces.append(sentinel)
        cursor = pos
    pieces.append(raw[cursor:])
    spliced = "".join(pieces)

    html = _md.render(spliced)

    sentinel_to_html = {
        idx: (h["open"], h["close"]) for idx, h in enumerate(highlights)
    }

    def _swap(match: re.Match) -> str:
        idx = int(match.group(1))
        body = match.group(2)
        open_html, close_html = sentinel_to_html.get(idx, ("", ""))
        return f"{open_html}{body}{close_html}"

    # Apply repeatedly so sentinel pairs nested inside another pair's body
    # also get swapped (regex captures the outer pair first, then we re-scan
    # the result). Bound the loop to avoid pathological inputs.
    for _ in range(8):
        new_html = _HL_SENT_PAIR_RE.sub(_swap, html)
        if new_html == html:
            break
        html = new_html

    # Strip any orphan sentinels (e.g. crossing ranges where one half lost
    # its mate during regex consumption). Better to render text cleanly than
    # leak  control glyphs.
    html = _HL_SENT_ORPHAN_RE.sub("", html)

    if fallback_anchors:
        html = "".join(fallback_anchors) + html

    return Markup(html)
