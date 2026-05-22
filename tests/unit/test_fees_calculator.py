"""Tests for the RVG/GKG fee calculator.

Golden values are derived from the statutory RVG Anlage 2 (2021) table and
published Gebührentabellen. All EUR amounts.
"""

import pytest

from app.models.enums import CaseType, ProceedingCourtLevel
from app.services.fees.calculator import (
    allocation_from_ruling,
    court_fees,
    default_allocation,
    lawyer_fees,
)
from app.services.fees.rvg_table import lookup_base_fee

# ---------------------------------------------------------------------------
# RVG table lookups
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "streitwert,expected_base",
    [
        (500, 49.00),
        (499, 49.00),  # rounds up to first bracket
        (1_000, 88.00),
        (3_000, 222.00),
        (10_000, 614.00),
        (25_000, 1_004.00),
        (50_000, 1_719.00),
        (200_000, 4_299.00),
        (500_000, 8_159.00),
    ],
)
def test_rvg_table_lookup(streitwert, expected_base):
    assert lookup_base_fee(streitwert) == expected_base


@pytest.mark.unit
def test_rvg_table_above_500k():
    # 501_000: 1 commenced thousand above 500K → 8_159 + 4.15 = 8_163.15
    assert lookup_base_fee(501_000) == pytest.approx(8_163.15, abs=0.01)
    # 510_000: 10 commenced thousands → 8_159 + 10×4.15 = 8_200.50
    assert lookup_base_fee(510_000) == pytest.approx(8_200.50, abs=0.01)


@pytest.mark.unit
def test_rvg_table_rejects_zero():
    with pytest.raises(ValueError):
        lookup_base_fee(0)


# ---------------------------------------------------------------------------
# Lawyer fees
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lawyer_fees_ag_3000():
    # Streitwert €3 000, AG first instance
    # base = 222.00; 1.3×222 = 288.60; 1.2×222 = 266.40; auslagen = 20.00
    # net = 288.60 + 266.40 + 20.00 = 575.00; gross = 575.00 × 1.19 = 684.25
    result = lawyer_fees(3_000, ProceedingCourtLevel.AG)
    assert result["net"] == pytest.approx(575.00, abs=0.01)
    assert result["gross"] == pytest.approx(684.25, abs=0.01)


@pytest.mark.unit
def test_lawyer_fees_ag_10000():
    # base = 614.00; 1.3×614 = 798.20; 1.2×614 = 736.80; net = 1555.00
    # gross = 1555.00 × 1.19 = 1850.45
    result = lawyer_fees(10_000, ProceedingCourtLevel.AG)
    assert result["net"] == pytest.approx(1_555.00, abs=0.01)
    assert result["gross"] == pytest.approx(1_850.45, abs=0.01)


@pytest.mark.unit
def test_lawyer_fees_olg_10000():
    # OLG: Verfahren factor 1.6, Termin 1.2
    # 1.6×614 = 982.40; 1.2×614 = 736.80; net = 982.40 + 736.80 + 20 = 1739.20
    # gross = 1739.20 × 1.19 = 2069.65 (rounded)
    result = lawyer_fees(10_000, ProceedingCourtLevel.OLG)
    assert result["net"] == pytest.approx(1_739.20, abs=0.01)
    assert result["gross"] == pytest.approx(2_069.65, abs=0.01)


@pytest.mark.unit
def test_lawyer_fees_breakdown_keys():
    result = lawyer_fees(10_000, ProceedingCourtLevel.AG)
    bd = result["breakdown"]
    assert bd["base_fee"] == 614.00
    assert bd["verfahren_factor"] == 1.3
    assert bd["termin_factor"] == 1.2
    assert bd["auslagen"] == 20.0


# ---------------------------------------------------------------------------
# Court fees
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_court_fees_ag_3000():
    # base 222.00 × 3.0 = 666.00
    assert court_fees(3_000, ProceedingCourtLevel.AG) == pytest.approx(666.00, abs=0.01)


@pytest.mark.unit
def test_court_fees_ag_10000():
    # base 614.00 × 3.0 = 1842.00
    assert court_fees(10_000, ProceedingCourtLevel.AG) == pytest.approx(
        1_842.00, abs=0.01
    )


@pytest.mark.unit
def test_court_fees_olg_10000():
    # base 614.00 × 4.0 = 2456.00
    assert court_fees(10_000, ProceedingCourtLevel.OLG) == pytest.approx(
        2_456.00, abs=0.01
    )


@pytest.mark.unit
def test_court_fees_bgh_10000():
    # base 614.00 × 5.0 = 3070.00
    assert court_fees(10_000, ProceedingCourtLevel.BGH) == pytest.approx(
        3_070.00, abs=0.01
    )


@pytest.mark.unit
def test_court_fees_family_ag():
    # Family AG: factor 2.0 (FamGKG)
    assert court_fees(3_000, ProceedingCourtLevel.AG, family_law=True) == pytest.approx(
        444.00, abs=0.01
    )


# ---------------------------------------------------------------------------
# Allocation helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_allocation_from_ruling_loser_legacy_defaults_to_we_lost():
    # Legacy data without client_role falls back to worst-case (we lost).
    alloc = allocation_from_ruling({"loser": 1.0})
    assert alloc["own_court_share"] == 1.0
    assert alloc["own_opposing_share"] == 1.0
    assert alloc["source"] == "ruling_we_lost"


@pytest.mark.unit
def test_allocation_from_ruling_we_won():
    alloc = allocation_from_ruling({"loser": 1.0, "client_role": "winner"})
    assert alloc["own_court_share"] == 0.0
    assert alloc["own_opposing_share"] == 0.0
    assert alloc["source"] == "ruling_we_won"


@pytest.mark.unit
def test_allocation_from_ruling_we_lost():
    alloc = allocation_from_ruling({"loser": 1.0, "client_role": "loser"})
    assert alloc["own_court_share"] == 1.0
    assert alloc["own_opposing_share"] == 1.0
    assert alloc["source"] == "ruling_we_lost"


@pytest.mark.unit
def test_allocation_from_ruling_each_own():
    alloc = allocation_from_ruling({"each_own": True})
    assert alloc["own_court_share"] == 0.5
    assert alloc["own_opposing_share"] == 0.0
    assert alloc["source"] == "ruling_each_own"


@pytest.mark.unit
def test_allocation_from_ruling_shared():
    alloc = allocation_from_ruling(
        {"own": 0.75, "opposing": 0.25, "client_role": "shared"}
    )
    assert alloc["own_court_share"] == pytest.approx(0.75)
    assert alloc["own_opposing_share"] == pytest.approx(0.25)
    assert alloc["source"] == "ruling_shared"


@pytest.mark.unit
def test_allocation_from_ruling_split_legacy():
    alloc = allocation_from_ruling({"own": 0.75, "opposing": 0.25})
    assert alloc["own_court_share"] == pytest.approx(0.75)
    assert alloc["own_opposing_share"] == pytest.approx(0.25)
    assert alloc["source"] == "ruling_split"


@pytest.mark.unit
def test_default_allocation_family():
    alloc = default_allocation(CaseType.FAMILY, ProceedingCourtLevel.AG)
    assert alloc["own_court_share"] == 0.5
    assert alloc["own_opposing_share"] == 0.0
    assert alloc["source"] == "family_default"


@pytest.mark.unit
def test_default_allocation_civil():
    alloc = default_allocation(CaseType.CIVIL, ProceedingCourtLevel.AG)
    assert alloc["own_court_share"] == 0.5
    assert alloc["own_opposing_share"] == 0.0
    assert alloc["source"] == "placeholder"
