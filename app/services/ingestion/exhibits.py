"""Exhibit labels (Anlage K 3, B 2, 1) and their order, from a lawyer's list.

A part cut out of a PDF by slicing does not inherit its source's label: the
parts are different documents, so one label would be wrong for most of them.

Two sources, both deterministic: a free-text list in the mail ("Anlage K3:
Vertrag vom …") and the label a filename carries ("Anlage_K3_Vertrag.pdf",
"K3.pdf"). The list gives the order the sender intended; a bare label only
orders by its number. Ambiguous matches are left unset: a wrong exhibit number
in a court file is worse than none.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import NamedTuple

_ANLAGE = r"(?:Anlagen?|Anlg?\.?)"
# Prefixes courts and lawyers use for exhibits: Kläger, Beklagte, Bf, Antragsteller…
_PREFIXES = {"K", "B", "BF", "AST", "AG", "ASG", "KE"}
_DISPLAY = {"AST": "ASt", "BF": "Bf"}

_LABEL_RE = re.compile(
    rf"^\s*(?P<word>{_ANLAGE})?[\s_.\-]*(?P<prefix>[A-Za-z]{{0,3}})[\s_.\-]*"
    r"(?P<num>\d{1,3})(?P<suffix>[a-z]?)\s*$",
    re.IGNORECASE,
)
# A list line: optional bullet, a label, a separator, the description.
_LIST_LINE_RE = re.compile(
    rf"^\s*(?:[-*•–]\s*|\d+[.)]\s+)?(?P<label>(?:{_ANLAGE}[\s_.\-]*)?[A-Za-z]{{0,3}}"
    r"[\s_.\-]*\d{1,3}[a-z]?)\s*[:\-–—)]\s*(?P<desc>\S.*)$",
    re.IGNORECASE,
)
# A label inside a filename: needs the word "Anlage", or a bare exhibit prefix
# at the start of the name ("K3 Vertrag.pdf", "B2.pdf").
_FILENAME_ANLAGE_RE = re.compile(
    rf"(?:^|[\s_.\-]){_ANLAGE}[\s_.\-]*(?P<prefix>[A-Za-z]{{0,3}})[\s_.\-]*(?P<num>\d{{1,3}})(?P<suffix>[a-z]?)(?![\d])",
    re.IGNORECASE,
)
# AG/ASG are exhibit prefixes only after the word "Anlage", in filenames and on
# list lines alike: bare, "AG 3 C 123-26" is a court and a file number.
_NEEDS_ANLAGE = {"AG", "ASG"}
_BARE_PREFIXES = sorted(_PREFIXES - _NEEDS_ANLAGE, key=len, reverse=True)
_FILENAME_BARE_RE = re.compile(
    rf"^(?P<prefix>{'|'.join(_BARE_PREFIXES)})[\s_.\-]*(?P<num>\d{{1,3}})(?P<suffix>[a-z]?)(?![\d])",
    re.IGNORECASE,
)


class ListItem(NamedTuple):
    label: str
    description: str
    position: int  # 1-based place in the list


def _format(prefix: str, num: str, suffix: str) -> str:
    prefix = prefix.upper()
    head = _DISPLAY.get(prefix, prefix)
    return f"{head} {int(num)}{suffix.lower()}".strip()


def parse_label(text: str) -> str | None:
    """ "Anlage K3", "K 3", "B2", "Anlage 1" → "K 3", "K 3", "B 2", "1"; else None."""
    match = _LABEL_RE.match(text)
    if not match:
        return None
    prefix = match["prefix"].upper()
    if prefix and prefix not in _PREFIXES:
        return None
    # A bare number, or a court-like AG/ASG, is only an exhibit when the word
    # "Anlage" says so.
    if (not prefix or prefix in _NEEDS_ANLAGE) and not match["word"]:
        return None
    return _format(prefix, match["num"], match["suffix"])


def parse_filename_label(filename: str) -> str | None:
    stem = PurePosixPath(filename).stem
    for pattern in (_FILENAME_ANLAGE_RE, _FILENAME_BARE_RE):
        match = (
            pattern.search(stem)
            if pattern is _FILENAME_ANLAGE_RE
            else pattern.match(stem)
        )
        if match:
            prefix = match["prefix"].upper()
            if prefix and prefix not in _PREFIXES:
                continue
            return _format(prefix, match["num"], match["suffix"])
    return None


def parse_anlage_list(text: str) -> list[ListItem]:
    """The exhibits a mail lists, in the order it lists them."""
    items: list[ListItem] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = _LIST_LINE_RE.match(line)
        if not match:
            continue
        label = parse_label(match["label"])
        if label is None or label in seen:
            continue
        seen.add(label)
        items.append(ListItem(label, match["desc"].strip(), len(items) + 1))
    return items


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-zäöüß0-9]+", text.lower()) if len(t) > 2}


def _similarity(description: str, filename: str) -> float:
    wanted, have = _tokens(description), _tokens(PurePosixPath(filename).stem)
    if not wanted or not have:
        return 0.0
    return len(wanted & have) / len(wanted | have)


_MIN_SIMILARITY = 0.5


def assign_exhibits(
    attachments: list[tuple[str, str]], items: list[ListItem]
) -> dict[str, dict]:
    """Exhibit info per attachment part id; ``attachments`` are (part_id, filename).

    A listed label wins over a filename label; the description is only used when
    no filename carries the label, and only when one file clearly fits it.
    """
    by_filename = {pid: parse_filename_label(name) for pid, name in attachments}
    result: dict[str, dict] = {}
    claimed: set[str] = set()
    for item in items:
        exact = [pid for pid, label in by_filename.items() if label == item.label]
        if len(exact) == 1 and exact[0] not in claimed:
            chosen = exact[0]
        elif exact:
            continue  # two files claim the label: leave both unset
        else:
            scored = sorted(
                (
                    (_similarity(item.description, name), pid)
                    for pid, name in attachments
                    if pid not in claimed and by_filename[pid] is None
                ),
                reverse=True,
            )
            if not scored or scored[0][0] < _MIN_SIMILARITY:
                continue
            if len(scored) > 1 and scored[1][0] == scored[0][0]:
                continue
            chosen = scored[0][1]
        claimed.add(chosen)
        result[chosen] = {
            "label": item.label,
            "position": item.position,
            "source": "anlage_list",
        }
    for pid, label in by_filename.items():
        if label and pid not in result:
            labels = [x for x in by_filename.values() if x == label]
            if len(labels) == 1:
                result[pid] = {"label": label, "position": None, "source": "filename"}
    return result


def sort_key(meta: dict | None) -> tuple:
    """Order among siblings: listed order first, then by label number; a document
    without an exhibit sorts after every exhibit."""
    exhibit = (meta or {}).get("exhibit")
    if not exhibit:
        return (2, 0, 0, "")
    if exhibit.get("position") is not None:
        return (0, exhibit["position"], 0, "")
    label = exhibit.get("label") or ""
    match = re.match(r"^(?:(?P<p>[A-Za-z]+) )?(?P<n>\d+)(?P<s>[a-z]?)$", label)
    if not match:
        return (1, 0, 0, label)
    return (1, 0, int(match["n"]), f"{match['p'] or ''}{match['s']}")
