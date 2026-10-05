"""
Checks on the execution-analysis maths in src/analytics/tca.py.

Run with pytest, or directly:
    python tests/test_tca.py
"""

import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analytics import tca  # noqa: E402


def _bars(start: str, rows: list[tuple], freq: str = "1min") -> pd.DataFrame:
    """rows of (open, high, low, close, volume, average)."""
    index = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(rows, index=index,
                        columns=["open", "high", "low", "close", "volume", "average"])


def test_slippage_sign_positive_is_cost():
    # Paying more than the benchmark on a buy is a cost...
    assert math.isclose(tca.slippage_bps("BUY", 101.0, 100.0), 100.0)
    # ...and so is receiving less than it on a sell.
    assert math.isclose(tca.slippage_bps("SELL", 99.0, 100.0), 100.0)
    assert math.isclose(tca.slippage_bps("SELL", 101.0, 100.0), -100.0)


def test_slippage_none_without_benchmark():
    assert tca.slippage_bps("BUY", 100.0, None) is None
    assert tca.slippage_bps("BUY", 100.0, float("nan")) is None


def test_flex_time_is_eastern_and_handles_dst():
    # 23:55 EDT on 29 March is 12:55 the next day in Tokyo.
    summer = tca.flex_time_to_utc("20260329;235552", "America/New_York")
    assert summer.tz_convert("Asia/Tokyo").strftime("%Y-%m-%d %H:%M:%S") == "2026-03-30 12:55:52"
    # 21:12 EST on 2 March (before the clocks change) is 11:12 in Tokyo.
    winter = tca.flex_time_to_utc("20260302;211230", "America/New_York")
    assert winter.tz_convert("Asia/Tokyo").strftime("%H:%M:%S") == "11:12:30"


def test_vwap_weights_by_volume_and_skips_empty_bars():
    bars = _bars("2026-03-30 00:00", [
        (0, 0, 0, 0, 100, 10.0),
        (0, 0, 0, 0, 300, 20.0),
        (0, 0, 0, 0, 0, 999.0),
    ])
    assert math.isclose(tca.vwap(bars), (100 * 10 + 300 * 20) / 400)
    assert tca.vwap(bars.iloc[2:]) is None


def test_arrival_uses_last_bar_closed_before_order_never_its_own():
    mid = _bars("2026-03-30 03:55", [
        (0, 0, 0, 100.0, None, None),   # 03:55 bar, closes 03:56:00
        (0, 0, 0, 105.0, None, None),   # 03:56 bar -- contains the order
        (0, 0, 0, 110.0, None, None),
    ])
    order = pd.Timestamp("2026-03-30 03:56:48", tz="UTC")
    price, as_of = tca.arrival_mid(mid, order)
    assert price == 100.0
    assert as_of == pd.Timestamp("2026-03-30 03:56:00", tz="UTC")


def test_arrival_with_fine_bars_gets_closer_to_the_order():
    mid = _bars("2026-03-30 03:56:35", [
        (0, 0, 0, 104.0, None, None),   # :35-:40
        (0, 0, 0, 104.5, None, None),   # :40-:45
        (0, 0, 0, 106.0, None, None),   # :45-:50 -- contains the order
    ], freq="5s")
    order = pd.Timestamp("2026-03-30 03:56:48", tz="UTC")
    price, as_of = tca.arrival_mid(mid, order, pd.Timedelta(seconds=5))
    assert price == 104.5
    assert as_of == pd.Timestamp("2026-03-30 03:56:45", tz="UTC")


def test_arrival_none_when_quote_is_stale_or_missing():
    mid = _bars("2026-03-30 03:00", [(0, 0, 0, 100.0, None, None)])
    late = pd.Timestamp("2026-03-30 03:30:00", tz="UTC")
    assert tca.arrival_mid(mid, late) == (None, None)
    early = pd.Timestamp("2026-03-30 02:00:00", tz="UTC")
    assert tca.arrival_mid(mid, early) == (None, None)


def test_session_includes_closing_auction_and_excludes_lunch():
    index = pd.DatetimeIndex(pd.to_datetime([
        "2026-03-30 08:59", "2026-03-30 09:00", "2026-03-30 11:30",
        "2026-03-30 12:00", "2026-03-30 12:30", "2026-03-30 15:30",
        "2026-03-30 15:31", "2026-03-29 10:00",
    ])).tz_localize("Asia/Tokyo").tz_convert("UTC")
    mask = tca.in_session(index, "2026-03-30", "JPY").tolist()
    assert mask == [False, True, True, False, True, True, False, False]


def test_fill_inside_bar():
    bars = _bars("2026-03-30 03:55", [(100, 102, 99, 101, 10, 100.5)])
    at = pd.Timestamp("2026-03-30 03:55:52", tz="UTC")
    assert tca.fill_inside_bar(bars, at, 101.5) is True
    assert tca.fill_inside_bar(bars, at, 103.0) is False
    assert tca.fill_inside_bar(bars, at + pd.Timedelta(minutes=5), 101.5) is None


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"{len(tests)} passed")
