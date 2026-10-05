"""
Checks on the valuation maths in src/sizing/intrinsic.py.

Each test pins a function to an answer that can be derived by hand, so a
failure means the formula is wrong rather than that an input moved.

Run with pytest, or directly:
    python tests/test_intrinsic.py
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sizing import intrinsic  # noqa: E402
from sizing.intrinsic import cfg  # noqa: E402


def test_dcf_constant_growth_is_gordon():
    # With no fade the model is a growing perpetuity: CF * (1+g) / (r-g).
    value, _ = intrinsic.dcf_value(100.0, 0.02, 0.02, 0.08, years=10)
    assert math.isclose(value, 100.0 * 1.02 / (0.08 - 0.02), rel_tol=1e-12)


def test_dcf_terminal_share_between_zero_and_one():
    _, share = intrinsic.dcf_value(100.0, 0.15, 0.02, 0.10)
    assert 0 < share < 1


def test_dcf_rejects_discount_rate_below_growth():
    try:
        intrinsic.dcf_value(100.0, 0.05, 0.03, 0.03)
    except ValueError:
        return
    raise AssertionError("expected ValueError when r <= terminal growth")


def test_implied_growth_round_trips():
    value, _ = intrinsic.dcf_value(100.0, 0.37, 0.025, 0.12)
    implied = intrinsic.implied_growth(value, 100.0, 0.025, 0.12)
    assert math.isclose(implied, 0.37, abs_tol=1e-9)


def test_implied_growth_none_for_negative_cash_flow():
    assert intrinsic.implied_growth(1000.0, -5.0, 0.02, 0.08) is None


def test_residual_income_at_cost_of_equity_is_book():
    # A bank earning exactly its cost of equity is worth its book value.
    value = intrinsic.residual_income_value(1000.0, 0.09, 0.09, 0.5, 10)
    assert math.isclose(value, 1000.0, rel_tol=1e-12)


def test_residual_income_premium_and_discount():
    above = intrinsic.residual_income_value(1000.0, 0.12, 0.09, 0.5, 10)
    below = intrinsic.residual_income_value(1000.0, 0.06, 0.09, 0.5, 10)
    assert above > 1000.0 > below


def test_residual_income_longer_fade_is_worth_more():
    short = intrinsic.residual_income_value(1000.0, 0.12, 0.09, 0.5, 5)
    long = intrinsic.residual_income_value(1000.0, 0.12, 0.09, 0.5, 20)
    assert long > short


def test_implied_roe_at_book_is_cost_of_equity():
    assert math.isclose(intrinsic.implied_roe(1.0, 0.09, 0.01), 0.09)


def test_adjusted_beta_shrinks_toward_one():
    assert intrinsic.adjusted_beta(1.0) == 1.0
    assert 1.0 < intrinsic.adjusted_beta(2.0) < 2.0
    assert 0.4 < intrinsic.adjusted_beta(0.4) < 1.0


def test_target_weight_rule():
    assert intrinsic.target_weight(None) is None
    assert intrinsic.target_weight(-0.5) == 0.0
    assert intrinsic.target_weight(cfg.MIN_MARGIN_OF_SAFETY - 0.01) == 0.0
    mid = cfg.MIN_MARGIN_OF_SAFETY + 0.05
    assert math.isclose(intrinsic.target_weight(mid), cfg.SIZING_FRACTION * mid)
    assert intrinsic.target_weight(0.99) == cfg.MAX_WEIGHT


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"{len(tests)} passed")
