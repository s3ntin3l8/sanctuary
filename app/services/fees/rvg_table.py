"""RVG Anlage 2 Gebührentabelle (fee schedule).

Source: RVG Anlage 2 (valid from 2021 reform, BGBl. I 2021 S. 2083).
The table maps an upper Streitwert bracket to the 1.0 Gebühr (Einfachgebühr).
All higher fees are integer multiples (Gebührenfaktoren) of this value.
"""

import math

# (upper_bound_eur, 1.0_gebuehr_eur) — sorted ascending by upper_bound
_TABLE: list[tuple[float, float]] = [
    (500, 49.00),
    (1_000, 88.00),
    (1_500, 127.00),
    (2_000, 166.00),
    (3_000, 222.00),
    (4_000, 278.00),
    (5_000, 334.00),
    (6_000, 390.00),
    (7_000, 446.00),
    (8_000, 502.00),
    (9_000, 558.00),
    (10_000, 614.00),
    (13_000, 692.00),
    (16_000, 770.00),
    (19_000, 848.00),
    (22_000, 926.00),
    (25_000, 1_004.00),
    (30_000, 1_147.00),
    (35_000, 1_290.00),
    (40_000, 1_433.00),
    (45_000, 1_576.00),
    (50_000, 1_719.00),
    (65_000, 1_977.00),
    (80_000, 2_235.00),
    (95_000, 2_493.00),
    (110_000, 2_751.00),
    (125_000, 3_009.00),
    (140_000, 3_267.00),
    (155_000, 3_525.00),
    (170_000, 3_783.00),
    (185_000, 4_041.00),
    (200_000, 4_299.00),
    (230_000, 4_685.00),
    (260_000, 5_071.00),
    (290_000, 5_457.00),
    (320_000, 5_843.00),
    (350_000, 6_229.00),
    (380_000, 6_615.00),
    (410_000, 7_001.00),
    (440_000, 7_387.00),
    (470_000, 7_773.00),
    (500_000, 8_159.00),
]

# Above €500 K: RVG §13 Abs. 2 — for each commenced €1 000 above €500 000 add €4.15
_ABOVE_500K_BASE = 8_159.00
_ABOVE_500K_PER_1K = 4.15
_ABOVE_500K_THRESHOLD = 500_000.0


def lookup_base_fee(streitwert: float) -> float:
    """Return the 1.0 Gebühr for the given Streitwert (EUR).

    Uses the statutory table up to €500 000 and the linear formula above that.
    Raises ValueError for non-positive values.
    """
    if streitwert <= 0:
        raise ValueError(f"Streitwert must be positive, got {streitwert}")
    for upper, fee in _TABLE:
        if streitwert <= upper:
            return fee
    excess = math.ceil((streitwert - _ABOVE_500K_THRESHOLD) / 1_000)
    return round(_ABOVE_500K_BASE + excess * _ABOVE_500K_PER_1K, 2)
