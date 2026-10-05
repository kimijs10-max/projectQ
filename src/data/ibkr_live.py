"""
Intraday bars from IB Gateway, for the Phase 6 execution analysis.

Flex records what each trade filled at. Judging the fill needs the market
around it -- the quote when the order went in, and the volume-weighted
price over the day -- and that is only available from the Gateway.

For every trade in the database this fetches two sets of one-minute bars
for the trade date and stores them:

- TRADES: prices and volume, for VWAP.
- MIDPOINT: the bid/ask midpoint, for the arrival price. A last-trade
  price can be a minute old or sit on either side of the spread; the
  midpoint is what the market was quoting.

plus five-second MIDPOINT bars for the ten minutes around each order,
where IBKR still has them. It keeps bars this fine for roughly six
months, so they exist for recent trades only -- and a trade's own bars
stop being retrievable if this is not run within that window. That is the
same reason the Flex statements are snapshotted: the source forgets.

and, from yfinance, the listing exchange's own total volume that day, so
the analysis can check how much of the market the bars actually cover.

Safety: connects with readonly=True and only ever calls qualifyContracts
and reqHistoricalData. Nothing here places, modifies or cancels an order.

ib_async's own logger is silenced below, deliberately: its error messages
include the account number (Gateway reports per-account status on
connect), and this project never prints one.

Requires IB Gateway on the configured port. One IBKR session per
username: a mobile-app login will disconnect it.

Run directly:
    python src/data/ibkr_live.py
"""

from __future__ import annotations

import logging
import os
import sys
import warnings
from pathlib import Path

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from ib_async import IB, Contract, util

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config"))

import execution  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "4001"))
# Offset from test_live.py's client id so the two can be run back to back.
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1")) + 10

BAR_SIZE = "1 min"
BAR_KINDS = ("TRADES", "MIDPOINT")

# Fine quote bars around each order, stored under their own kind.
FINE_BAR_SIZE = "5 secs"
FINE_KIND = "MIDPOINT_5S"
FINE_WINDOW_SECONDS = 600
FINE_AFTER_ORDER = pd.Timedelta(minutes=2)

# Not every Gateway message is a failure. These codes are connection
# status notices; anything else raised against a request is kept and shown.
_STATUS_CODES = {2104, 2106, 2107, 2108, 2158, 10275}


def _day_end_utc(trade_date: str) -> str:
    """End of the UTC day after `trade_date`'s session, in IBKR's UTC form."""
    end = pd.Timestamp(trade_date) + pd.Timedelta(days=1)
    return end.strftime("%Y%m%d-%H:%M:%S")


def get_intraday_bars(trades: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    One-minute TRADES and MIDPOINT bars for each (symbol, trade_date) in
    `trades`. Returns (bars, problems): bars as rows for the intraday_bars
    table with bar_time in UTC, and a list of human-readable request
    failures with no account detail in them.

    The contract is requested on SMART rather than its primary exchange.
    For Tokyo listings this account has no market-data permission on TSEJ
    itself and a primary-exchange request returns nothing; SMART returns
    the consolidated tape. How much of the market that tape covers is
    checked downstream against the exchange's own volume, not assumed.
    """
    logging.getLogger("ib_async").setLevel(logging.CRITICAL)
    ib = IB()
    problems: list[str] = []

    def on_error(req_id, code, message, contract):
        if code in _STATUS_CODES or (code == 162 and "returned no data" in message):
            return
        symbol = getattr(contract, "localSymbol", "") or "request"
        problems.append(f"{symbol}: [{code}] {message.split(', contract:')[0][:120]}")

    ib.errorEvent += on_error
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, readonly=True, timeout=15)

    rows: list[dict] = []
    try:
        days = trades[["symbol", "conid", "trade_date"]].drop_duplicates()
        for day in days.itertuples(index=False):
            qualified = ib.qualifyContracts(Contract(conId=int(day.conid), exchange="SMART"))
            if not qualified or qualified[0] is None:
                problems.append(f"{day.symbol}: contract not found")
                continue
            contract = qualified[0]
            contract.exchange = "SMART"
            for kind in BAR_KINDS:
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime=_day_end_utc(day.trade_date),
                    durationStr="2 D",
                    barSizeSetting=BAR_SIZE,
                    whatToShow=kind,
                    useRTH=True,
                    formatDate=2,
                    timeout=60,
                )
                if not bars:
                    problems.append(f"{day.symbol} {day.trade_date}: no {kind} bars")
                    continue
                df = util.df(bars)
                for bar in df.itertuples(index=False):
                    has_volume = kind == "TRADES"
                    rows.append({
                        "symbol": day.symbol,
                        "bar_time": pd.Timestamp(bar.date).tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S"),
                        "kind": kind,
                        "open": float(bar.open),
                        "high": float(bar.high),
                        "low": float(bar.low),
                        "close": float(bar.close),
                        "volume": float(bar.volume) if has_volume else None,
                        "average": float(bar.average) if has_volume else None,
                    })
                print(f"  {day.symbol:>8} {day.trade_date} {kind:<9} {len(df):>5} bars")

        for trade in trades.dropna(subset=["order_time"]).itertuples(index=False):
            qualified = ib.qualifyContracts(Contract(conId=int(trade.conid), exchange="SMART"))
            if not qualified or qualified[0] is None:
                continue
            contract = qualified[0]
            contract.exchange = "SMART"
            order_time = (pd.to_datetime(trade.order_time, format="%Y%m%d;%H%M%S")
                          .tz_localize(execution.FLEX_TIMEZONE).tz_convert("UTC"))
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=(order_time + FINE_AFTER_ORDER).strftime("%Y%m%d-%H:%M:%S"),
                durationStr=f"{FINE_WINDOW_SECONDS} S",
                barSizeSetting=FINE_BAR_SIZE,
                whatToShow="MIDPOINT",
                useRTH=False,
                formatDate=2,
                timeout=60,
            )
            print(f"  {trade.symbol:>8} {trade.trade_date} {FINE_KIND:<12} {len(bars):>4} bars"
                  + ("" if bars else "  (too old: IBKR keeps these ~6 months)"))
            for bar in bars:
                rows.append({
                    "symbol": trade.symbol,
                    "bar_time": pd.Timestamp(bar.date).tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": FINE_KIND,
                    "open": float(bar.open), "high": float(bar.high),
                    "low": float(bar.low), "close": float(bar.close),
                    "volume": None, "average": None,
                })
    finally:
        ib.disconnect()
    return pd.DataFrame(rows), problems


def get_exchange_volume(trades: pd.DataFrame) -> pd.DataFrame:
    """The listing exchange's reported volume on each trade date (yfinance)."""
    rows = []
    for day in trades[["symbol", "trade_date"]].drop_duplicates().itertuples(index=False):
        start = pd.Timestamp(day.trade_date)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            history = yf.Ticker(day.symbol).history(
                start=start.strftime("%Y-%m-%d"),
                end=(start + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                auto_adjust=False,
            )
        if history.empty or pd.isna(history["Volume"].iloc[0]):
            print(f"  warning: no exchange volume for {day.symbol} on {day.trade_date}")
            continue
        rows.append({"symbol": day.symbol, "date": day.trade_date,
                     "volume": float(history["Volume"].iloc[0])})
    return pd.DataFrame(rows)


def main() -> None:
    from storage import db

    conn = db.connect()
    try:
        trades = db.read_table(conn, "trades")
        if trades.empty:
            print("No trades stored. Run src/data/flex.py first.")
            return
        print(f"Connecting to IB Gateway at {IB_HOST}:{IB_PORT} (readonly)...")
        bars, problems = get_intraday_bars(trades)
        volume = get_exchange_volume(trades)

        n_bars = db.upsert_df(conn, "intraday_bars", bars)
        n_volume = db.upsert_df(conn, "exchange_volume", volume)
        print(f"intraday_bars    {n_bars:>6} rows")
        print(f"exchange_volume  {n_volume:>6} rows")
        for problem in problems:
            print(f"  problem: {problem}")
    finally:
        conn.close()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
