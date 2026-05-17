"""normalize_existing_case_titles

Revision ID: a4bef00d
Revises: 751c06b1f6db
Create Date: 2026-05-17 00:00:00.000000

Walk every row in the ``cases`` table and coerce legacy titles to the
canonical form expected by the current application:

  "Hansen ./. Liu (Sorgerecht)"      → "Hansen ./. Liu - Sorgerecht"
  "8372/25 - Mueller ./. Schmidt, eA" → "Mueller ./. Schmidt (eA)"

This is a one-time data migration that brings historical data in line with
the normalization logic now applied on write. It is irreversible.
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4bef00d"
down_revision: str | Sequence[str] | None = "751c06b1f6db"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ---------------------------------------------------------------------------
# Frozen copy of _normalize_case_title from app/services/case_service.py.
# IMPORTANT: Do NOT import from app — migrations must be standalone.
# Keep this in sync manually if the live function changes materially.
# ---------------------------------------------------------------------------

_PAREN_MATTER_RE_MIG = re.compile(r"^(.+?\s+\./\.\s+.+?)\s+\((?!eA\)$)([^()]+?)\)\s*$")
_INTERNAL_ID_PREFIX_RE_MIG = re.compile(r"^\d{3,5}[/-]\d{2,4}\s*[-:/]\s*")
_EA_TRAILING_COMMA_RE_MIG = re.compile(r"\s*,\s*eA\s*$", re.IGNORECASE)
_EA_TRAILING_BARE_RE_MIG = re.compile(r"\s+eA\s*$", re.IGNORECASE)
_EA_TRAILING_DASH_RE_MIG = re.compile(r"\s*[-–]\s*eA\s*$", re.IGNORECASE)
_EA_FULL_NAME_RE_MIG = re.compile(
    r"\s*[-–,(]?\s*einstweilige[r]?\s+Anordnung\s*\)?\s*$", re.IGNORECASE
)
_TRAILING_PUNCT_RE_MIG = re.compile(r"[\s\-–:/,;]+$")


def _normalize_case_title_mig(title: str | None) -> str | None:
    """Coerce a raw AI / email-subject-derived title into canonical form.

    Operates in this order:
    1. extract any eA marker into a flag (so other rewrites don't disturb it),
    2. strip a leading echoed internal_id like "8372/25 - ",
    3. strip trailing punctuation artifacts ("-", ":", ",", " "),
    4. convert "X ./. Y (Matter)" → "X ./. Y - Matter",
    5. re-attach the eA marker as " (eA)" if present,
    6. cap at 120 chars.

    Idempotent: feeding the result back in returns the same string.
    """
    if title is None:
        return None
    s = title.strip()
    if not s:
        return None

    # 1. Detect + strip any eA marker. A bare " (eA)" suffix is preserved
    #    verbatim; other variants (", eA" / " - eA" / "einstweilige Anordnung")
    #    are stripped here and re-appended in canonical form at the end.
    has_ea = False
    if s.endswith(" (eA)") or s.endswith("(eA)"):
        has_ea = True
        s = s[: -len("(eA)")].rstrip().rstrip("(").rstrip()
    else:
        for pat in (
            _EA_FULL_NAME_RE_MIG,
            _EA_TRAILING_DASH_RE_MIG,
            _EA_TRAILING_COMMA_RE_MIG,
            _EA_TRAILING_BARE_RE_MIG,
        ):
            new_s, n = pat.subn("", s)
            if n:
                has_ea = True
                s = new_s
                break

    # 2. Strip leading internal_id echo.
    s = _INTERNAL_ID_PREFIX_RE_MIG.sub("", s, count=1).lstrip()

    # 3. Strip trailing punctuation/whitespace.
    s = _TRAILING_PUNCT_RE_MIG.sub("", s)

    # 4. Paren-style matter → dash-style. Only fires for "X ./. Y (Matter)";
    #    matter-only titles like "Kindesunterhalt" are untouched.
    m = _PAREN_MATTER_RE_MIG.match(s)
    if m:
        parties, matter = m.group(1).strip(), m.group(2).strip()
        s = f"{parties} - {matter}"

    # 5. Re-append eA in canonical form.
    if has_ea:
        s = f"{s} (eA)" if s else "(eA)"

    # 6. Cap. Smart-truncate so we don't slice mid-"(eA)".
    if len(s) > 120:
        if has_ea and s.endswith(" (eA)"):
            s = s[: 120 - len(" (eA)")].rstrip() + " (eA)"
        else:
            s = s[:120].rstrip()

    return s or None


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, title FROM cases")).fetchall()
    for row_id, title in rows:
        if title is None:
            continue
        normalized = _normalize_case_title_mig(title)
        if normalized and normalized != title:
            conn.execute(
                sa.text("UPDATE cases SET title = :t WHERE id = :id"),
                {"t": normalized, "id": row_id},
            )


def downgrade() -> None:
    raise NotImplementedError("data migration; irreversible")
