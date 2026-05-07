"""GKG / FamGKG Kostenverzeichnis multipliers.

Maps (court_level, family_law) to the GKG multiplier applied to the
1.0 Gebühr from RVG Anlage 2.

Civil proceedings (GKG):
  KV 1210 — first instance (AG / LG): factor 3.0
  KV 1220 — Berufung (LG / OLG):     factor 4.0
  KV 1230 — Revision (BGH):           factor 5.0

Family proceedings (FamGKG):
  Most Kindschaftssachen (§§ 151 ff. FamFG) use a fixed Verfahrenswert
  set by the court (§45 FamGKG, typically €3 000). The FamGKG multiplier
  for first-instance Kindschaftssachen is 2.0 (KV FamGKG Nr. 1310).
  Beschwerde: 2.0 (KV FamGKG Nr. 1500).
  For Unterhalt/Ehesachen the GKG base still applies with different factors;
  this table uses a conservative 2.0 default for all family-court instances.
"""

from app.models.enums import ProceedingCourtLevel

# (court_level, family_law) → multiplier
_GKG_MULTIPLIERS: dict[tuple[ProceedingCourtLevel, bool], float] = {
    # Civil
    (ProceedingCourtLevel.AG, False): 3.0,
    (ProceedingCourtLevel.LG, False): 3.0,  # LG first-instance also KV 1210
    (ProceedingCourtLevel.OLG, False): 4.0,
    (ProceedingCourtLevel.BGH, False): 5.0,
    (ProceedingCourtLevel.OTHER, False): 3.0,
    # Family (FamGKG)
    (ProceedingCourtLevel.AG, True): 2.0,
    (ProceedingCourtLevel.LG, True): 2.0,
    (ProceedingCourtLevel.OLG, True): 2.0,  # Beschwerde in family
    (ProceedingCourtLevel.BGH, True): 3.0,
    (ProceedingCourtLevel.OTHER, True): 2.0,
}


def gkg_multiplier(court_level: ProceedingCourtLevel, family_law: bool) -> float:
    """Return the GKG/FamGKG multiplier for the given court level."""
    return _GKG_MULTIPLIERS.get((court_level, family_law), 3.0)
