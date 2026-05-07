"""RVG / GKG fee calculator — pure functions, no I/O.

All monetary values are in EUR. The caller is responsible for converting
to cents (×100) when storing in the database.

Lawyer fees (RVG):
  own_lawyer always 100% regardless of allocation.
  Factor schedule per instance (VV RVG):
    AG/LG  — Verfahren 1.3 (Nr. 3100) + Termin 1.2 (Nr. 3104)
    OLG    — Verfahren 1.6 (Nr. 3200) + Termin 1.2 (Nr. 3202)
    BGH    — Verfahren 2.3 (Nr. 3206) + Termin 2.3 (Nr. 3210)
  Plus €20 Auslagenpauschale (VV RVG Nr. 7002) and 19% VAT.

Court fees (GKG/FamGKG):
  base_fee × multiplier (no VAT on court fees).

Allocation dict keys:
  own_court_share    — fraction of court fees we pay (0.0–1.0)
  own_opposing_share — fraction of opposing lawyer fees we pay (0.0–1.0)
  source             — display label for UI
"""

from __future__ import annotations

from app.models.enums import CaseType, ProceedingCourtLevel

from .gkg_table import gkg_multiplier
from .rvg_table import lookup_base_fee

# (court_level) → (Verfahrensgebühr factor, Terminsgebühr factor)
_RVG_FACTORS: dict[ProceedingCourtLevel, tuple[float, float]] = {
    ProceedingCourtLevel.AG: (1.3, 1.2),
    ProceedingCourtLevel.LG: (1.3, 1.2),
    ProceedingCourtLevel.OLG: (1.6, 1.2),
    ProceedingCourtLevel.BGH: (2.3, 2.3),
    ProceedingCourtLevel.OTHER: (1.3, 1.2),
}

_AUSLAGENPAUSCHALE = 20.0  # VV RVG Nr. 7002
_VAT_RATE = 0.19


def lawyer_fees(
    streitwert: float,
    court_level: ProceedingCourtLevel,
    family_law: bool = False,
) -> dict:
    """Compute own-lawyer fees for a given Streitwert and court level.

    Returns:
        net:       EUR net (before VAT)
        gross:     EUR gross (incl. 19% VAT)
        breakdown: itemised components
    """
    base = lookup_base_fee(streitwert)
    verfahren_f, termin_f = _RVG_FACTORS.get(court_level, (1.3, 1.2))
    verfahren = round(base * verfahren_f, 2)
    termin = round(base * termin_f, 2)
    net = round(verfahren + termin + _AUSLAGENPAUSCHALE, 2)
    vat = round(net * _VAT_RATE, 2)
    gross = round(net + vat, 2)
    return {
        "net": net,
        "gross": gross,
        "breakdown": {
            "base_fee": base,
            "verfahren": verfahren,
            "verfahren_factor": verfahren_f,
            "termin": termin,
            "termin_factor": termin_f,
            "auslagen": _AUSLAGENPAUSCHALE,
            "vat": vat,
        },
    }


def court_fees(
    streitwert: float,
    court_level: ProceedingCourtLevel,
    family_law: bool = False,
) -> float:
    """Compute court fees (GKG / FamGKG) for a Streitwert. Returns EUR (no VAT)."""
    base = lookup_base_fee(streitwert)
    multiplier = gkg_multiplier(court_level, family_law)
    return round(base * multiplier, 2)


def default_allocation(case_type: CaseType, court_level: ProceedingCourtLevel) -> dict:
    """Return the default cost-allocation dict for a case type and court level.

    Used when no Kostenentscheidung signal has been extracted and no
    assume_worst_case flag is set.

    Family cases: §81 FamFG → each party bears its own costs as the default.
    Civil cases: placeholder 50/50 court share, no opposing share assumed.
    """
    if case_type == CaseType.FAMILY:
        return {
            "own_court_share": 0.5,
            "own_opposing_share": 0.0,
            "source": "family_default",
        }
    return {
        "own_court_share": 0.5,
        "own_opposing_share": 0.0,
        "source": "placeholder",
    }


def allocation_from_ruling(ruling: dict) -> dict:
    """Convert a cost_ruling signal's ``allocation`` dict to the canonical form.

    Ruling shapes (from AI extraction):
      {"loser": 1.0}      — loser pays all (court + opposing counsel)
      {"each_own": true}  — each party bears its own costs
      {"own": 0.5, "opposing": 0.5} — explicit split fractions

    We assume we are the losing party when "loser" is present (worst-case),
    and the winning party when "each_own" is true (cost-neutral outcome).
    """
    if not ruling:
        return {"own_court_share": 0.5, "own_opposing_share": 0.0, "source": "unknown"}

    if ruling.get("each_own"):
        return {
            "own_court_share": 0.5,
            "own_opposing_share": 0.0,
            "source": "ruling_each_own",
        }

    if "loser" in ruling:
        return {
            "own_court_share": 1.0,
            "own_opposing_share": 1.0,
            "source": "ruling_loser_pays",
        }

    own_share = float(ruling.get("own", 0.5))
    opposing_share = float(ruling.get("opposing", 0.0))
    return {
        "own_court_share": own_share,
        "own_opposing_share": opposing_share,
        "source": "ruling_split",
    }
