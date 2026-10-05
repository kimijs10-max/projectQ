"""
Phase 6: execution analysis.

Every trade has a price at which it was decided and a price at which it
filled. The gap is a cost that appears on no statement. This module
measures it for each fill in the account against four benchmarks, in
basis points, signed so that **positive is always a cost**:

    buy:   (fill - benchmark) / benchmark
    sell:  (benchmark - fill) / benchmark

**Arrival price** -- the bid/ask midpoint when the order was submitted.
This is the implementation-shortfall benchmark: it is the price that
existed when the decision was made, so everything after it (crossing the
spread, waiting, the market moving) is execution. The midpoint is taken
from the last quote bar that had *closed* by the time the order arrived,
so it can be one bar old but can never contain the order. Five-second
bars are used where IBKR still has them (it keeps them about six months)
and one-minute bars otherwise; the resolution used is printed per trade,
because it changes the answer -- see the data checks.

**Interval VWAP** -- volume-weighted price from order to fill. Only
exists for an order that waited; it asks whether waiting got a better
price than the market traded at meanwhile.

**Day VWAP** -- volume-weighted price over the primary session. The
standard "did I do better than the average participant" benchmark. It
includes trading after the fill, so it is a comparison, not something an
order could have achieved.

**Close** -- the same question against the closing price.

Arrival slippage plus commission and tax is the all-in cost of the trade.

Three things this report is careful about:

1. **Time zones are verified, not assumed.** Flex timestamps carry no
   zone. Each fill is checked against the high-low range of the
   one-minute bar it should fall in; a fill outside its own bar means the
   clock mapping, or the data, is wrong, and the report says so.

2. **A VWAP is only the market's VWAP if the bars cover the market.**
   Bar volume for the session is compared with the exchange's own
   reported volume and the ratio is printed. Where the bars carry a
   fraction of the day's volume the VWAP is flagged.

3. **The sample is the fills.** An order that was never filled is not in
   the Flex statement, so a patient limit order can only ever show up
   here as a success. And with a handful of trades nothing below is a
   statistic: it is a per-trade record, and no averages are reported.

Requires intraday bars (python src/data/ibkr_live.py, with IB Gateway up).

Run directly:
    python src/analytics/tca.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config"))

import execution as cfg  # noqa: E402
from storage import db  # noqa: E402

# Oldest midpoint that still counts as the arrival price.
MAX_ARRIVAL_STALENESS = pd.Timedelta(minutes=5)

# Quote bars for the arrival price, finest first: (kind, bar length, label).
ARRIVAL_SOURCES = [
    ("MIDPOINT_5S", pd.Timedelta(seconds=5), "5-second"),
    ("MIDPOINT", pd.Timedelta(minutes=1), "1-minute"),
]


# --------------------------------------------------------------------------
# Maths. Pure functions -- tested in tests/test_tca.py.
# --------------------------------------------------------------------------

def flex_time_to_utc(value: str, timezone: str = cfg.FLEX_TIMEZONE) -> pd.Timestamp:
    """A Flex 'yyyymmdd;hhmmss' timestamp as a UTC timestamp."""
    naive = pd.to_datetime(value, format="%Y%m%d;%H%M%S")
    return naive.tz_localize(timezone).tz_convert("UTC")


def slippage_bps(side: str, fill: float, benchmark: float | None) -> float | None:
    """Cost of `fill` against `benchmark` in basis points; positive = worse."""
    if benchmark is None or pd.isna(benchmark) or benchmark == 0:
        return None
    sign = 1.0 if side.upper().startswith("B") else -1.0
    return sign * (fill - benchmark) / benchmark * 1e4


def in_session(index: pd.DatetimeIndex, trade_date: str, currency: str) -> pd.Series:
    """Which UTC bar times fall inside the primary session on `trade_date`."""
    session = cfg.SESSIONS[currency]
    local = index.tz_convert(session["timezone"])
    mask = pd.Series(False, index=index)
    for start, end in session["hours"]:
        lo = pd.Timestamp(f"{trade_date} {start}", tz=session["timezone"])
        hi = pd.Timestamp(f"{trade_date} {end}", tz=session["timezone"])
        mask |= (local >= lo) & (local <= hi)
    return mask


def vwap(bars: pd.DataFrame) -> float | None:
    """Volume-weighted price of TRADES bars; None when nothing traded."""
    traded = bars[bars["volume"] > 0]
    volume = traded["volume"].sum()
    if not volume:
        return None
    return float((traded["average"] * traded["volume"]).sum() / volume)


def arrival_mid(
    mid_bars: pd.DataFrame,
    order_time: pd.Timestamp,
    bar_length: pd.Timedelta = pd.Timedelta(minutes=1),
) -> tuple[float | None, pd.Timestamp | None]:
    """
    The midpoint in force when the order arrived: the close of the last
    midpoint bar that had ended at or before the order time.

    Using the bar the order falls in would be look-ahead -- its close is
    after the order and can include the order's own effect on the quote.
    Returns (price, the time that price is as of).
    """
    ended = mid_bars[mid_bars.index + bar_length <= order_time]
    if ended.empty:
        return None, None
    as_of = ended.index[-1] + bar_length
    if order_time - as_of > MAX_ARRIVAL_STALENESS:
        return None, None
    return float(ended["close"].iloc[-1]), as_of


def fill_inside_bar(trade_bars: pd.DataFrame, fill_time: pd.Timestamp, price: float) -> bool | None:
    """Whether `price` lies in the range of the bar it filled in; None if no bar."""
    minute = fill_time.floor("min")
    if minute not in trade_bars.index:
        return None
    bar = trade_bars.loc[minute]
    return bool(bar["low"] <= price <= bar["high"])


# --------------------------------------------------------------------------
# Analysis.
# --------------------------------------------------------------------------

def _bars(conn: sqlite3.Connection, symbol: str, kind: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT * FROM intraday_bars WHERE symbol = ? AND kind = ? ORDER BY bar_time",
        conn, params=(symbol, kind),
    )
    df.index = pd.to_datetime(df.pop("bar_time"), utc=True)
    return df


def analyse(conn: sqlite3.Connection) -> pd.DataFrame:
    """One row per fill: times, benchmarks, slippage in bps, data checks."""
    trades = db.read_table(conn, "trades")
    volumes = db.read_table(conn, "exchange_volume")
    rows = []
    for t in trades.sort_values("date_time").itertuples(index=False):
        fill_time = flex_time_to_utc(t.date_time)
        order_time = flex_time_to_utc(t.order_time) if t.order_time else None
        trade_bars = _bars(conn, t.symbol, "TRADES")
        side, price = t.buy_sell, float(t.trade_price)

        row = {
            "symbol": t.symbol, "trade_date": t.trade_date, "side": side,
            "order_type": t.order_type, "venue": t.exchange, "currency": t.currency,
            "order_time": order_time, "fill_time": fill_time, "fill": price,
            "wait_minutes": None if order_time is None
            else (fill_time - order_time).total_seconds() / 60,
            "cost_bps": abs(float(t.ib_commission) + float(t.taxes or 0.0))
            / abs(float(t.trade_money)) * 1e4,
            "close_bps": slippage_bps(side, price, t.close_price),
        }

        if trade_bars.empty or t.currency not in cfg.SESSIONS:
            row["note"] = "no intraday bars stored"
            rows.append(row)
            continue

        session = trade_bars[in_session(trade_bars.index, t.trade_date, t.currency).to_numpy()]
        day_vwap = vwap(session)
        row["day_vwap"] = day_vwap
        row["day_vwap_bps"] = slippage_bps(side, price, day_vwap)
        row["fill_in_bar"] = fill_inside_bar(trade_bars, fill_time, price)

        exchange = volumes[(volumes["symbol"] == t.symbol) & (volumes["date"] == t.trade_date)]
        if not exchange.empty and exchange["volume"].iloc[0]:
            row["coverage"] = float(session["volume"].sum() / exchange["volume"].iloc[0])

        if order_time is not None:
            # Every resolution is computed and kept; the finest available
            # one is the answer and the rest show what resolution cost.
            for kind, length, label in ARRIVAL_SOURCES:
                mid, _ = arrival_mid(_bars(conn, t.symbol, kind), order_time, length)
                bps = slippage_bps(side, price, mid)
                row[f"arrival_bps_{kind}"] = bps
                if bps is not None and "arrival_bps" not in row:
                    row["arrival"], row["arrival_bps"], row["arrival_source"] = mid, bps, label
            # An interval needs at least one whole bar between order and fill.
            if fill_time.floor("min") > order_time.floor("min"):
                between = trade_bars[(trade_bars.index >= order_time.floor("min"))
                                     & (trade_bars.index <= fill_time.floor("min"))]
                row["interval_vwap"] = vwap(between)
                row["interval_vwap_bps"] = slippage_bps(side, price, row["interval_vwap"])
        rows.append(row)

    out = pd.DataFrame(rows)
    for column in ("arrival", "arrival_bps", "arrival_source", "interval_vwap_bps",
                   "day_vwap_bps", "coverage", "fill_in_bar", "note",
                   *(f"arrival_bps_{kind}" for kind, _, _ in ARRIVAL_SOURCES)):
        if column not in out.columns:
            out[column] = None
    out["total_bps"] = out["arrival_bps"].astype(float) + out["cost_bps"]
    return out


# --------------------------------------------------------------------------
# Report.
# --------------------------------------------------------------------------

def _bps(x, width: int = 9) -> str:
    if x is None or pd.isna(x):
        return "--".rjust(width)
    return f"{x:+.1f}".rjust(width)


def _thin(coverage) -> bool:
    """True when bar volume is too far from the exchange's to call it the market."""
    if coverage is None or pd.isna(coverage):
        return False
    return not (cfg.MIN_VOLUME_COVERAGE <= coverage <= 1 / cfg.MIN_VOLUME_COVERAGE)


def main() -> None:
    conn = db.connect()
    try:
        result = analyse(conn)
        if result.empty:
            print("No trades stored. Run src/data/flex.py first.")
            return

        print("=" * 78)
        print("EXECUTION ANALYSIS -- basis points, positive = cost")
        print("=" * 78)
        print(f"{len(result)} fills. This is a per-trade record, not a sample: "
              "no averages are reported.\n")

        print(f"{'symbol':<8}{'date':<12}{'side':<5}{'type':<5}{'venue':<9}"
              f"{'order':>9}{'fill':>9}{'waited':>9}")
        for r in result.itertuples(index=False):
            tz = cfg.SESSIONS.get(r.currency, {}).get("timezone", "UTC")
            order = "--" if r.order_time is None else r.order_time.tz_convert(tz).strftime("%H:%M:%S")
            fill = r.fill_time.tz_convert(tz).strftime("%H:%M:%S")
            if r.wait_minutes is None:
                waited = "--"
            elif r.wait_minutes < 1 / 60:
                waited = "none"
            else:
                waited = f"{r.wait_minutes:.0f} min"
            print(f"{r.symbol:<8}{r.trade_date:<12}{r.side:<5}{r.order_type:<5}{r.venue:<9}"
                  f"{order:>9}{fill:>9}{waited:>9}")
        print("  times are exchange-local.")

        print("\n" + "-" * 78)
        print("SLIPPAGE against each benchmark")
        print("-" * 78)
        print(f"{'symbol':<8}{'side':<5}{'arrival':>9}{'interval':>9}{'day':>9}{'close':>9}"
              f"{'commission':>12}{'all-in':>9}")
        print(f"{'':<8}{'':<5}{'mid':>9}{'VWAP':>9}{'VWAP':>9}{'':>9}{'+ tax':>12}"
              f"{'vs arrival':>11}")
        any_thin = False
        for r in result.itertuples(index=False):
            thin = _thin(r.coverage)
            any_thin |= thin
            mark = "*" if thin else " "
            print(f"{r.symbol:<8}{r.side:<5}{_bps(r.arrival_bps)}"
                  f"{_bps(r.interval_vwap_bps, 8)}{mark if pd.notna(r.interval_vwap_bps) else ' '}"
                  f"{_bps(r.day_vwap_bps, 8)}{mark if pd.notna(r.day_vwap_bps) else ' '}"
                  f"{_bps(r.close_bps)}{_bps(r.cost_bps, 12)}{_bps(r.total_bps)}")
        print("\n  all-in = arrival slippage + commission and tax. Interval VWAP exists")
        print("  only for an order that waited at least a minute.")
        if any_thin:
            print("  * VWAP from bars that cover only part of the day's volume (see data")
            print("    checks). It is that subset's VWAP, not the market's.")

        print("\n" + "-" * 78)
        print("DATA CHECKS")
        print("-" * 78)
        for r in result.itertuples(index=False):
            if r.note:
                print(f"  {r.symbol} {r.trade_date}: {r.note}")
                continue
            in_bar = {True: "inside its one-minute bar", False: "OUTSIDE its one-minute bar",
                      None: "has no bar at its minute"}[r.fill_in_bar]
            if r.coverage is None or pd.isna(r.coverage):
                coverage = "no exchange volume to compare"
            else:
                flagged = _thin(r.coverage)
                coverage = (f"bars carry {r.coverage * 100:.0f}% of exchange volume"
                            + ("  <-- day VWAP is not the market's" if flagged else ""))
            print(f"  {r.symbol} {r.trade_date}: fill {in_bar}; {coverage}")
            fine, coarse = (getattr(r, f"arrival_bps_{kind}", None)
                            for kind, _, _ in ARRIVAL_SOURCES)
            if r.arrival_source:
                detail = f"arrival mid from {r.arrival_source} quote bars"
                if pd.notna(fine) and pd.notna(coarse):
                    detail += (f" ({fine:+.1f} bps; one-minute bars alone would "
                               f"have said {coarse:+.1f})")
                print(f"  {'':<{len(r.symbol) + 11}}  {detail}")
        print(f"\n  Flex timestamps read as {cfg.FLEX_TIMEZONE}. A fill outside its bar would")
        print("  mean that mapping is wrong.")

        print("\n" + "-" * 78)
        print("WHAT THIS CANNOT SHOW")
        print("-" * 78)
        print("  Unfilled orders are not in the Flex statement. A limit order that the")
        print("  market never came back to cost the whole move it missed, and appears")
        print("  nowhere above -- so patient orders can only look good here.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
