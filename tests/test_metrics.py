import math

import pandas as pd
import pytest

from pipeline.transform import metrics as m


def monthly(values, start="2020-01-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="MS"), dtype=float)


def quarterly(values, start="2019-03-31"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="QE"), dtype=float)


# ---- rebase / wedge --------------------------------------------------------------------

def test_rebase_sets_base_to_100():
    s = monthly([50, 55, 60])
    r = m.rebase(s, "2020-01")
    assert r.iloc[0] == pytest.approx(100.0)
    assert r.iloc[2] == pytest.approx(120.0)


def test_rebase_missing_base_raises():
    with pytest.raises(ValueError):
        m.rebase(monthly([1, 2, 3]), "2019-01")


def test_rebase_zero_base_raises():
    with pytest.raises(ValueError):
        m.rebase(monthly([0, 1, 2]), "2020-01")


def test_wedge_is_price_minus_cost_on_overlap_only():
    p = monthly([100, 110, 120, 130])
    c = monthly([100, 105], start="2020-02-01")  # overlaps Feb, Mar only
    w = m.wedge(p, c)
    assert list(w.index) == list(pd.date_range("2020-02-01", periods=2, freq="MS"))
    assert w.iloc[0] == pytest.approx(110 - 100)
    assert w.iloc[1] == pytest.approx(120 - 105)


# ---- pass-through ----------------------------------------------------------------------

def test_pass_through_full():
    # costs peak at 150 then fall to 120; price tracks them down one-for-one
    c = monthly([100, 150, 120])
    p = monthly([100, 140, 110])
    assert m.pass_through(p, c) == pytest.approx(1.0)


def test_pass_through_zero_when_prices_stick():
    c = monthly([100, 150, 120])
    p = monthly([100, 140, 140])
    assert m.pass_through(p, c) == pytest.approx(0.0)


def test_pass_through_negative_when_prices_keep_rising():
    c = monthly([100, 150, 120])
    p = monthly([100, 140, 150])
    assert m.pass_through(p, c) < 0


def test_pass_through_nan_when_costs_have_not_fallen():
    c = monthly([100, 110, 120])  # peak is "now"
    p = monthly([100, 120, 130])
    assert math.isnan(m.pass_through(p, c))


def test_pass_through_nan_on_tiny_cost_drop():
    c = monthly([100, 150, 149.5])
    p = monthly([100, 140, 130])
    assert math.isnan(m.pass_through(p, c))


# ---- margins ---------------------------------------------------------------------------

def test_ttm_margin_and_baseline_and_expansion():
    # 2019: 4 quarters, revenue 100 each, profit 30 each -> baseline margin 30%
    # 2020: revenue 100 each, profit 35 each -> TTM at 2020Q4 = 35% -> +5pp
    rev = quarterly([100] * 8)
    pro = quarterly([30] * 4 + [35] * 4)
    assert m.baseline_margin(rev, pro, 2019) == pytest.approx(0.30)
    exp = m.margin_expansion_pp(rev, pro, 2019)
    assert exp.iloc[3] == pytest.approx(0.0)       # TTM through 2019Q4 equals baseline
    assert exp.iloc[7] == pytest.approx(5.0)       # TTM through 2020Q4
    assert exp.iloc[:3].isna().all()               # fewer than 4 quarters


def test_baseline_nan_when_year_incomplete():
    rev = quarterly([100, 100, 100])               # only 3 quarters of 2019
    pro = quarterly([30, 30, 30])
    assert math.isnan(m.baseline_margin(rev, pro, 2019))


def test_ttm_is_revenue_weighted_not_average_of_ratios():
    rev = quarterly([100, 100, 100, 700])
    pro = quarterly([50, 50, 50, 70])               # ratios .5,.5,.5,.1 -> avg .4; weighted 220/1000
    assert m.ttm_margin(rev, pro).iloc[3] == pytest.approx(0.22)


def test_ttm_nan_when_a_quarter_is_missing():
    # drop 2019Q3 -> any 4-row window spanning the hole is not a trailing twelve months
    rev = quarterly([100] * 8).drop(pd.Timestamp("2019-09-30"))
    pro = quarterly([30] * 8).drop(pd.Timestamp("2019-09-30"))
    t = m.ttm_margin(rev, pro)
    # rows: 0=19Q1 1=19Q2 2=19Q4 3=20Q1 4=20Q2 5=20Q3 6=20Q4. The hole is the gap before row 2,
    # so only windows ending at rows 2,3,4 contain it (rows 0-2 are also short of 4 quarters).
    assert t.iloc[:5].isna().all()
    assert t.iloc[5] == pytest.approx(0.30)        # 19Q4..20Q3: four consecutive quarters
    assert t.iloc[6] == pytest.approx(0.30)


def test_ttm_nan_when_a_value_is_nan():
    rev = quarterly([100] * 7)
    pro = quarterly([30, 30, float("nan"), 30, 30, 30, 30])
    t = m.ttm_margin(rev, pro)
    assert t.iloc[3:6].isna().all()                # every 4-row window containing row 2 (rows 2..5)
    assert t.iloc[6] == pytest.approx(0.30)        # rows 3..6 are clean again


def test_ttm_nan_when_a_quarter_is_duplicated():
    # SEC data can carry the same period twice (original + restatement); never sum both.
    idx = pd.to_datetime(["2019-03-31", "2019-06-30", "2019-06-30", "2019-09-30", "2019-12-31"])
    rev = pd.Series([100.0] * 5, index=idx)
    pro = pd.Series([30.0] * 5, index=idx)
    assert m.ttm_margin(rev, pro).isna().all()


def test_ttm_sorts_unsorted_input():
    idx = pd.to_datetime(["2019-12-31", "2019-03-31", "2019-09-30", "2019-06-30"])
    rev = pd.Series([100.0] * 4, index=idx)
    pro = pd.Series([30.0] * 4, index=idx)
    t = m.ttm_margin(rev, pro)
    assert t.iloc[:3].isna().all()
    assert t.iloc[3] == pytest.approx(0.30)


def test_ttm_tolerates_52_53_week_quarter_ends():
    idx = pd.to_datetime(["2019-03-30", "2019-06-29", "2019-09-28", "2019-12-28"])
    rev = pd.Series([100.0] * 4, index=idx)
    pro = pd.Series([25.0] * 4, index=idx)
    assert m.ttm_margin(rev, pro).iloc[3] == pytest.approx(0.25)


def test_pass_through_since_ignores_earlier_peak():
    # a huge pre-2020 cost spike would dominate without `since`
    idx = pd.date_range("2018-01-01", periods=6, freq="MS")
    c = pd.Series([100, 300, 100, 110, 150, 120], index=idx, dtype=float)
    p = pd.Series([100, 200, 100, 110, 140, 140], index=idx, dtype=float)
    assert m.pass_through(p, c, since="2018-03-01") == pytest.approx(0.0)  # peak 150 -> 120, price stuck
    assert m.pass_through(p, c) == pytest.approx((200 - 140) / (300 - 120))


def test_baseline_nan_when_a_value_is_nan():
    rev = quarterly([100] * 4)
    pro = quarterly([30, float("nan"), 30, 30])
    assert math.isnan(m.baseline_margin(rev, pro, 2019))


# ---- units -----------------------------------------------------------------------------

@pytest.mark.parametrize("size,unit,expected_qty,expected_label", [
    (16, "oz", 16, "oz"),
    (1, "lb", 16, "oz"),
    (500, "g", 17.63698, "oz"),
    (12, "fl oz", 12, "fl oz"),
    (1, "gal", 128, "fl oz"),
    (2, "L", 67.62804, "fl oz"),
    (1, "dz", 12, "ct"),
    (18, "ct", 18, "ct"),
])
def test_normalize_size(size, unit, expected_qty, expected_label):
    qty, label = m.normalize_size(size, unit)
    assert label == expected_label
    assert qty == pytest.approx(expected_qty, rel=1e-4)


def test_unit_price_detects_shrinkflation():
    # same shelf price, package shrinks 16oz -> 14oz: unit price rises ~14.3%
    before, _ = m.unit_price_cents(499, 16, "oz")
    after, _ = m.unit_price_cents(499, 14, "oz")
    assert m.pct_change(before, after) == pytest.approx(14.2857, rel=1e-3)


def test_unknown_unit_raises():
    with pytest.raises(ValueError):
        m.normalize_size(5, "furlongs")


def test_nonpositive_size_raises():
    with pytest.raises(ValueError):
        m.normalize_size(0, "oz")


def test_pct_change_zero_old_is_nan():
    assert math.isnan(m.pct_change(0, 5))
