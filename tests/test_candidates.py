"""
Checks on the screening rules in src/screener/candidates.py.

Run with pytest, or directly:
    python tests/test_candidates.py
"""

import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screener import candidates  # noqa: E402
from screener.candidates import cfg  # noqa: E402


def _row(**overrides) -> dict:
    """A DCF name that passes every rule, to be broken one field at a time."""
    row = {
        "per": 10.0, "equity_ratio": 0.60, "current_ratio": 2.0, "is_financial": False,
        "has_earnings": True,
        "model": "dcf", "price_to_value": 0.60, "price_to_value_bear": 0.70,
        "owner_cf_margin": 0.10, "margin_min": 0.08,
        "margin_years": cfg.SCREEN_MIN_YEARS,
    }
    row.update(overrides)
    return row


def test_clean_name_is_a_candidate():
    assert candidates.exclusion_reason(_row()) is None


def test_above_value_is_excluded():
    assert "not below" in candidates.exclusion_reason(_row(price_to_value=1.2))
    assert "not below" in candidates.exclusion_reason(_row(price_to_value=float("nan")))


def test_thin_margin_is_excluded():
    ratio = 1 - cfg.MIN_MARGIN_OF_SAFETY + 0.01
    assert "margin of safety" in candidates.exclusion_reason(_row(price_to_value=ratio))


def test_short_history_is_excluded():
    reason = candidates.exclusion_reason(_row(margin_years=cfg.SCREEN_MIN_YEARS - 1))
    assert "years of cash-flow history" in reason


def test_cyclical_peak_is_excluded():
    # Normalised margin 10%, but one year on record was 2%: valued off a peak.
    assert "unstable" in candidates.exclusion_reason(_row(margin_min=0.02))
    # A loss-making year fails the same rule.
    assert "unstable" in candidates.exclusion_reason(_row(margin_min=-0.05))


def test_discount_must_survive_bear_case():
    assert "bear case" in candidates.exclusion_reason(_row(price_to_value_bear=0.95))


def test_implausible_discount_is_flagged_not_ranked_first():
    ratio = cfg.SCREEN_MIN_PRICE_TO_VALUE - 0.05
    reason = candidates.exclusion_reason(_row(price_to_value=ratio, price_to_value_bear=ratio))
    assert "check the data" in reason


def test_bank_skips_cash_flow_rules():
    bank = {"per": 9.0, "equity_ratio": 0.05, "current_ratio": float("nan"),
            "is_financial": True, "has_earnings": True,
            "model": "residual income", "price_to_value": 0.75,
            "price_to_value_bear": 0.80, "owner_cf_margin": float("nan"),
            "margin_min": float("nan"), "margin_years": float("nan")}
    assert candidates.exclusion_reason(bank) is None


def test_stage_one_thresholds():
    assert "PER above" in candidates.exclusion_reason(_row(per=cfg.SCREEN_MAX_PER + 1))
    assert "equity ratio under" in candidates.exclusion_reason(
        _row(equity_ratio=cfg.SCREEN_MIN_EQUITY_RATIO - 0.01))
    assert "current ratio under" in candidates.exclusion_reason(
        _row(current_ratio=cfg.SCREEN_MIN_CURRENT_RATIO - 0.01))


def test_stage_one_comes_first():
    # Fails PER *and* is above value: reported as the stage-one failure.
    reason = candidates.exclusion_reason(_row(per=40.0, price_to_value=1.5))
    assert reason.startswith("PER")


def test_loss_maker_fails_per_rather_than_passing_it():
    row = _row(per=float("nan"), has_earnings=False)
    assert "no profit" in candidates.exclusion_reason(row)


def test_missing_ratio_is_not_a_pass():
    assert "not computable" in candidates.exclusion_reason(_row(equity_ratio=float("nan")))
    assert "not computable" in candidates.exclusion_reason(_row(current_ratio=float("nan")))


def test_bank_is_not_failed_on_ratios_that_do_not_apply():
    bank = _row(is_financial=True, equity_ratio=0.05, current_ratio=float("nan"),
                model="residual income")
    assert candidates.exclusion_reason(bank) is None
    assert "PER above" in candidates.exclusion_reason({**bank, "per": 30.0})


def test_first_stage_ratios_use_one_fiscal_year_per_ratio():
    # The newest year has an income statement but no balance sheet, as
    # yfinance sometimes returns. Each ratio must come from a single year.
    group = pd.DataFrame({
        "fiscal_date": ["2024-12-31", "2025-12-31"],
        "Net Income": [80.0, 100.0],
        "Ordinary Shares Number": [10.0, float("nan")],
        "Stockholders Equity": [600.0, float("nan")],
        "Total Assets": [1000.0, float("nan")],
        "Current Assets": [300.0, float("nan")],
        "Current Liabilities": [150.0, float("nan")],
        "Cost Of Revenue": [1.0, 1.0],
    })
    out = candidates.first_stage_ratios(group, price=120.0)
    assert math.isclose(out["per"], 120.0 * 10.0 / 100.0)   # latest earnings
    assert math.isclose(out["equity_ratio"], 0.60)
    assert math.isclose(out["current_ratio"], 2.0)
    assert out["is_financial"] is False


def test_first_stage_ratios_loss_has_no_per():
    group = pd.DataFrame({"fiscal_date": ["2025-12-31"], "Net Income": [-5.0],
                          "Ordinary Shares Number": [10.0], "Cost Of Revenue": [1.0]})
    out = candidates.first_stage_ratios(group, price=50.0)
    assert math.isnan(out["per"]) and out["has_earnings"] is False


def _frame(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["symbol", "group", "margin_of_safety"])


def test_weights_follow_the_sizing_rule_and_leave_cash():
    weights = candidates.suggest_weights(_frame([("A", "g1", 0.20), ("B", "g2", 0.30)]))
    assert math.isclose(weights["A"], cfg.SIZING_FRACTION * 0.20)
    assert math.isclose(weights["B"], cfg.SIZING_FRACTION * 0.30)
    assert weights.sum() < 1  # never grossed up to fill the portfolio


def test_group_cap_scales_a_crowded_group():
    rows = [(f"S{i}", "ships", 0.50) for i in range(4)] + [("X", "other", 0.20)]
    weights = candidates.suggest_weights(_frame(rows))
    ships = weights[[f"S{i}" for i in range(4)]]
    assert math.isclose(ships.sum(), cfg.MAX_GROUP_WEIGHT)
    assert math.isclose(ships.max(), ships.min())      # scaled pro rata
    assert math.isclose(weights["X"], cfg.SIZING_FRACTION * 0.20)


def test_total_never_exceeds_one():
    rows = [(f"S{i}", f"g{i}", 0.60) for i in range(8)]
    weights = candidates.suggest_weights(_frame(rows))
    assert math.isclose(weights.sum(), 1.0)
    assert (weights <= cfg.MAX_WEIGHT + 1e-12).all()


def test_no_candidates_is_all_cash():
    assert candidates.suggest_weights(_frame([])).empty


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"{len(tests)} passed")
