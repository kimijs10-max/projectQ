"""
Checks on the fallen-stock screen in src/screener/fallen.py.

Run with pytest, or directly:
    python tests/test_fallen.py
"""

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screener import fallen  # noqa: E402
from screener.fallen import cfg  # noqa: E402


def _group(values: dict[str, float], name: str = "g") -> tuple[pd.Series, pd.Series]:
    return pd.Series(values), pd.Series({k: name for k in values})


def test_relative_is_against_own_group_only():
    values = pd.Series({"a": -0.30, "b": 0.00, "c": 0.10, "x": 0.50, "y": 0.60, "z": 0.70})
    groups = pd.Series({"a": "g1", "b": "g1", "c": "g1", "x": "g2", "y": "g2", "z": "g2"})
    out = fallen.relative_to_group(values, groups)
    assert math.isclose(out.loc["a", "shortfall"], -0.30)       # vs g1 median 0.00
    assert math.isclose(out.loc["x", "shortfall"], -0.10)       # vs g2 median 0.60
    assert out.loc["a", "rank"] < out.loc["b", "rank"] < out.loc["c", "rank"]


def test_sector_wide_fall_flags_nobody():
    values, groups = _group({f"s{i}": -0.40 + 0.01 * i for i in range(10)})
    assert not fallen.is_fallen(fallen.relative_to_group(values, groups)).any()


def test_one_collapse_in_a_flat_group_is_flagged():
    data = {f"s{i}": 0.01 * i for i in range(9)}
    data["bad"] = -0.45
    values, groups = _group(data)
    flags = fallen.is_fallen(fallen.relative_to_group(values, groups))
    assert flags["bad"] and flags.sum() == 1


def test_lagging_in_a_rising_group_is_not_fallen():
    # Up 13% against peers up ~40%: far behind, but the price did not fall.
    data = {f"s{i}": 0.38 + 0.01 * i for i in range(9)}
    data["lag"] = 0.13
    values, groups = _group(data)
    assert not fallen.is_fallen(fallen.relative_to_group(values, groups))["lag"]


def test_small_shortfall_is_not_fallen():
    data = {f"s{i}": 0.00 for i in range(9)}
    data["dip"] = -(cfg.MIN_SHORTFALL - 0.02)
    values, groups = _group(data)
    assert not fallen.is_fallen(fallen.relative_to_group(values, groups))["dip"]


def test_year_on_year_matches_the_same_quarter():
    dates = pd.to_datetime(["2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"])
    latest, prior, when = fallen.year_on_year(pd.Series([100, 110, 120, 130, 150], index=dates))
    assert (latest, prior, when) == (150.0, 100.0, "2026-06-30")


def test_year_on_year_none_without_a_matching_quarter():
    dates = pd.to_datetime(["2026-03-31", "2026-06-30"])
    latest, prior, _ = fallen.year_on_year(pd.Series([130, 150], index=dates))
    assert latest == 150.0 and prior is None
    assert fallen.year_on_year(pd.Series(dtype=float)) == (None, None, None)


def test_verdict_unexplained_when_results_held_up():
    assert fallen.business_verdict(110, 100, 20, 21)[0] == "unexplained"


def test_verdict_explained_by_revenue_profit_or_loss():
    assert "revenue" in fallen.business_verdict(95, 100, 20, 20)[1]
    assert "profit" in fallen.business_verdict(110, 100, 10, 20)[1]
    assert "loss" in fallen.business_verdict(110, 100, -5, 20)[1]
    assert fallen.business_verdict(95, 100, -5, 20)[0] == "explained"


def test_verdict_unknown_is_not_good_news():
    status, _ = fallen.business_verdict(110, None, 20, None)
    assert status == "unknown"


def test_reversal_test_finds_a_built_in_rebound():
    # Six groups of twelve. Every month one name per group drops 30% and
    # recovers it the next month; the test must see fallers beat peers.
    rng = np.random.default_rng(1)
    months = pd.date_range("2021-01-31", periods=36, freq="ME")
    names = [f"g{g}_{i}" for g in range(6) for i in range(12)]
    groups = pd.Series({n: n.split("_")[0] for n in names})
    returns = pd.DataFrame(rng.normal(0.0, 0.002, (len(months), len(names))),
                           index=months, columns=names)
    for t in range(1, len(months) - 1, 2):
        for g in range(6):
            victim = f"g{g}_{(t // 2) % 12}"
            returns.loc[months[t], victim] = -0.30
            returns.loc[months[t + 1], victim] = 1 / 0.70 - 1
    monthly = (1 + returns).cumprod()
    old = cfg.LOOKBACK_GRID, cfg.HOLDING_GRID, cfg.MIN_NAMES
    cfg.LOOKBACK_GRID, cfg.HOLDING_GRID, cfg.MIN_NAMES = (1,), (1,), 60
    try:
        out = fallen.reversal_test(monthly, groups)
    finally:
        cfg.LOOKBACK_GRID, cfg.HOLDING_GRID, cfg.MIN_NAMES = old
    row = out.iloc[0]
    assert row["spread_mean"] > 0.05 and row["months_ahead"] > 0.45


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"{len(tests)} passed")
