"""Metric math for PriceMemory. Pure functions, no I/O, so every number on the site is testable.

Definitions here are the source of truth for the public /methodology page.
"""
from __future__ import annotations

import math

import pandas as pd

# ---- index math -----------------------------------------------------------------------


def rebase(series: pd.Series, base: str | pd.Timestamp) -> pd.Series:
    """Rebase a monthly index so the value at `base` equals 100.

    `series` must have a DatetimeIndex. `base` is matched to the month (e.g. "2020-01").
    Raises if the base month is missing or zero, rather than silently returning NaNs.
    """
    base_ts = pd.Period(base, freq="M")
    periods = series.index.to_period("M")
    mask = periods == base_ts
    if not mask.any():
        raise ValueError(f"base month {base_ts} not present in series")
    base_val = float(series[mask].iloc[0])
    if base_val == 0 or math.isnan(base_val):
        raise ValueError(f"base value at {base_ts} is zero/NaN")
    return series / base_val * 100.0


def wedge(price_idx: pd.Series, cost_idx: pd.Series) -> pd.Series:
    """Wedge = price index - cost index, in index points (both rebased to the same base).

    Positive means retail price rose more than the producer-cost proxy since the base month.
    Only months present in both series are returned.
    """
    both = pd.concat({"p": price_idx, "c": cost_idx}, axis=1).dropna()
    return both["p"] - both["c"]


def pass_through(price_idx: pd.Series, cost_idx: pd.Series, min_cost_drop: float = 1.0,
                 since: str | pd.Timestamp | None = None) -> float:
    """Share of the cost decline since the cost peak that showed up in the price index.

        pass_through = (P_at_cost_peak - P_now) / (C_peak - C_now)

    - C_peak is the maximum of the cost index; P_at_cost_peak is the price index in that month.
    - "now" is the last month present in both series.
    - 1.0 means prices fell one-for-one with costs; 0.0 means prices did not fall at all;
      negative means prices kept rising while costs fell.
    - Returns NaN when costs have not fallen by at least `min_cost_drop` index points
      from their peak (ratio is meaningless on a tiny denominator).
    - `since` restricts the search for the cost peak (and everything after) to months on or
      after that date, so a pre-2020 spike cannot be mistaken for the pandemic-era peak.
    """
    both = pd.concat({"p": price_idx, "c": cost_idx}, axis=1).dropna()
    if since is not None:
        both = both[both.index >= pd.Timestamp(since)]
    if both.empty:
        return float("nan")
    peak_date = both["c"].idxmax()
    c_peak = float(both.loc[peak_date, "c"])
    p_at_peak = float(both.loc[peak_date, "p"])
    c_now = float(both["c"].iloc[-1])
    p_now = float(both["p"].iloc[-1])
    cost_drop = c_peak - c_now
    if cost_drop < min_cost_drop:
        return float("nan")
    return (p_at_peak - p_now) / cost_drop


# ---- company margins --------------------------------------------------------------------


def ttm_margin(revenue: pd.Series, profit: pd.Series) -> pd.Series:
    """Trailing-twelve-month margin from quarterly revenue and profit (same index).

    TTM margin = sum(profit over last 4 quarters) / sum(revenue over last 4 quarters).
    The first three quarters are NaN. Callers are responsible for filling fiscal Q4
    (10-K minus 9M YTD) before calling. A window is NaN (never silently computed) if
      - any of its 4 quarters has a NaN revenue or profit, or
      - consecutive quarter-end dates in it are not ~one quarter apart (80-100 days), i.e. a
        quarter is missing or duplicated. 52/53-week calendars stay within that tolerance.
    """
    both = pd.concat({"rev": revenue, "pro": profit}, axis=1).sort_index()
    rev4 = both["rev"].rolling(4, min_periods=4).sum()
    pro4 = both["pro"].rolling(4, min_periods=4).sum()
    gap_days = both.index.to_series().diff().dt.days
    irregular = ((gap_days > 100) | (gap_days < 80)).astype(float)   # first diff is NaN -> False
    bad_window = irregular.rolling(3, min_periods=1).max() > 0       # 4 quarters span 3 gaps
    out = pro4 / rev4
    out[bad_window.values] = float("nan")
    return out


def baseline_margin(revenue: pd.Series, profit: pd.Series, year: int = 2019) -> float:
    """Full-year margin for `year` = sum(profit) / sum(revenue) over that calendar year's quarters.

    Requires all four quarters; returns NaN otherwise (a partial year would bias the baseline).
    """
    both = pd.concat({"rev": revenue, "pro": profit}, axis=1).sort_index()
    yr = both[both.index.year == year]
    if len(yr) != 4 or yr.isna().any().any():
        return float("nan")
    return float(yr["pro"].sum() / yr["rev"].sum())


def margin_expansion_pp(revenue: pd.Series, profit: pd.Series, baseline_year: int = 2019) -> pd.Series:
    """TTM margin minus the baseline-year margin, in percentage points (e.g. +2.3 = 2.3 pp higher)."""
    base = baseline_margin(revenue, profit, baseline_year)
    return (ttm_margin(revenue, profit) - base) * 100.0


# ---- unit prices --------------------------------------------------------------------------

_MASS_TO_OZ = {"oz": 1.0, "lb": 16.0, "g": 0.03527396, "kg": 35.27396}
_VOL_TO_FLOZ = {"fl oz": 1.0, "floz": 1.0, "ml": 0.03381402, "l": 33.81402, "gal": 128.0,
                "qt": 32.0, "pt": 16.0}
_COUNT = {"ct": 1.0, "count": 1.0, "ea": 1.0, "each": 1.0, "dz": 12.0}


def normalize_size(size_value: float, size_unit: str) -> tuple[float, str]:
    """Convert a package size to a canonical (quantity, unit) where unit is 'oz', 'fl oz' or 'ct'.

    Raises ValueError on unknown units so bad data is loud, not silently mis-priced.
    """
    unit = size_unit.strip().lower().rstrip(".")
    if size_value is None or size_value <= 0:
        raise ValueError(f"invalid size {size_value!r}")
    if unit in _MASS_TO_OZ:
        return size_value * _MASS_TO_OZ[unit], "oz"
    if unit in _VOL_TO_FLOZ:
        return size_value * _VOL_TO_FLOZ[unit], "fl oz"
    if unit in _COUNT:
        return size_value * _COUNT[unit], "ct"
    raise ValueError(f"unknown size unit {size_unit!r}")


def unit_price_cents(price_cents: int, size_value: float, size_unit: str) -> tuple[float, str]:
    """Price per canonical unit, in cents. Returns (cents_per_unit, unit_label)."""
    qty, label = normalize_size(size_value, size_unit)
    return price_cents / qty, label


def pct_change(old: float, new: float) -> float:
    """Percent change from old to new. NaN when old is zero/NaN."""
    if old is None or math.isnan(old) or old == 0:
        return float("nan")
    return (new - old) / old * 100.0
