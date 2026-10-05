"""
Checks on the stress-test maths in src/analytics/stress.py.

Synthetic price paths with answers that can be worked out by hand: the
replay must find the right peak and trough, its stock/FX/cross split must
sum to the loss, and a holiday must carry the last price forward.

Run with pytest, or directly:
    python tests/test_stress.py
"""

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analytics import stress  # noqa: E402
from analytics.stress import Exposure  # noqa: E402

DATES = pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"])


def _levels(**columns) -> pd.DataFrame:
    return pd.DataFrame(columns, index=DATES)


def test_max_drawdown_finds_peak_before_trough():
    value = pd.Series([1.0, 1.2, 0.9, 0.6, 1.5], index=DATES)
    peak, trough, loss = stress.max_drawdown(value)
    assert (peak, trough) == (DATES[1], DATES[3])
    assert math.isclose(loss, 0.6 / 1.2 - 1)


def test_replay_single_stock_in_base_currency():
    levels = _levels(AAA=[100, 110, 99, 88, 120])
    r = stress.replay("t", [Exposure("AAA", 1.0, "SGD", "AAA")], levels, "2020-01-01", "2020-01-07")
    assert math.isclose(r.loss, 88 / 110 - 1)
    assert math.isclose(r.stock, r.loss) and r.fx == 0 and r.cross == 0


def test_replay_split_sums_to_loss():
    levels = _levels(
        AAA=[100, 105, 90, 70, 95],
        BBB=[50, 49, 45, 40, 44],
        USDSGD=[1.30, 1.31, 1.35, 1.38, 1.33],
        JPYSGD=[0.0090, 0.0091, 0.0095, 0.0099, 0.0092],
    )
    book = [
        Exposure("AAA", 0.5, "USD", "AAA"),
        Exposure("BBB", 0.3, "JPY", "BBB"),
        Exposure("JPY cash", 0.15, "JPY"),
        Exposure("SGD cash", 0.05, "SGD"),
    ]
    r = stress.replay("t", book, levels, "2020-01-01", "2020-01-07")
    assert math.isclose(r.stock + r.fx + r.cross, r.loss, rel_tol=1e-12)
    assert math.isclose(r.by_exposure["contribution"].sum(), r.loss, rel_tol=1e-12)
    assert math.isclose(r.by_exposure["weight"].sum(), 1.0, rel_tol=1e-12)


def test_foreign_cash_has_currency_risk_only():
    levels = _levels(JPYSGD=[0.010, 0.010, 0.009, 0.008, 0.010])
    r = stress.replay("t", [Exposure("JPY cash", 1.0, "JPY")], levels, "2020-01-01", "2020-01-07")
    assert math.isclose(r.loss, -0.2)
    assert r.stock == 0 and math.isclose(r.fx, -0.2)


def test_holiday_is_forward_filled_not_skipped():
    # BBB does not trade on the 3rd; its last price must carry through.
    levels = _levels(AAA=[100, 100, 80, 80, 80], BBB=[100, 100, np.nan, 100, 100])
    book = [Exposure("AAA", 0.5, "SGD", "AAA"), Exposure("BBB", 0.5, "SGD", "BBB")]
    r = stress.replay("t", book, levels, "2020-01-01", "2020-01-07")
    assert r.trough == DATES[2]
    assert math.isclose(r.loss, -0.1)


def test_missing_history_uses_proxy_and_reports_it():
    levels = _levels(NEW=[np.nan] * 5, IDX=[100, 100, 90, 80, 85])
    book = [Exposure("NEW", 1.0, "SGD", "NEW")]
    r = stress.replay("t", book, levels, "2020-01-01", "2020-01-07", proxies={"NEW": (1.5, "IDX")})
    assert math.isclose(r.loss, 1.5 * -0.2)
    assert "NEW" in r.proxied and math.isclose(r.proxied_weight, 1.0)


def test_missing_history_without_proxy_is_an_error():
    levels = _levels(NEW=[np.nan] * 5)
    try:
        stress.replay("t", [Exposure("NEW", 1.0, "SGD", "NEW")], levels, "2020-01-01", "2020-01-07")
    except ValueError:
        return
    raise AssertionError("expected ValueError for a series with no history and no proxy")


def test_shock_direct_and_full():
    book = [
        Exposure("JP", 0.4, "JPY", "JP"),
        Exposure("US", 0.4, "USD", "US"),
        Exposure("JPY cash", 0.2, "JPY"),
    ]
    out = stress.shock(book, 0.10, {"JPY": 0.10}, {"JP": -2.0, "US": -1.0}).set_index("name")
    # Translation only: 60% of NAV is in yen.
    assert math.isclose(out["direct"].sum(), 0.06)
    assert math.isclose(out.loc["JP", "full"], 0.4 * ((1 - 0.20) * 1.10 - 1))
    assert math.isclose(out.loc["US", "full"], 0.4 * -0.10)
    assert math.isclose(out.loc["JPY cash", "full"], 0.02)


def test_shock_missing_beta_is_nan_not_zero():
    out = stress.shock([Exposure("X", 1.0, "USD", "X")], -0.15, {}, {})
    assert np.isnan(out["full"].iloc[0])


def test_factor_betas_recovers_known_beta():
    idx = pd.date_range("2022-01-07", periods=120, freq="W-FRI")
    rng = np.random.default_rng(0)
    factor = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.02, len(idx))), index=idx)
    stock = 50 * (factor / factor.iloc[0]) ** 1.0
    doubled = pd.Series(50 * np.cumprod(1 + 2 * factor.pct_change().fillna(0)), index=idx)
    betas = stress.factor_betas(pd.DataFrame({"one": stock, "two": doubled}), factor)
    assert math.isclose(betas.loc["one", "beta"], 1.0, abs_tol=1e-9)
    assert math.isclose(betas.loc["two", "beta"], 2.0, abs_tol=1e-9)
    assert math.isclose(betas.loc["two", "r_squared"], 1.0, abs_tol=1e-9)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"{len(tests)} passed")
