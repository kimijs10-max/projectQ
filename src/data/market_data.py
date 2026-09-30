"""
Market data via yfinance: FX history and benchmark prices.

Base currency is SGD (confirmed from Flex EquitySummaryInBase, see
NOTES.md -- CLAUDE.md's original data-source table said CAD, which was
wrong and is corrected here).

Direct USD-quoted pairs (USDSGD=X, JPY=X i.e. USDJPY) have full daily
history on Yahoo. The JPY/SGD cross itself (JPYSGD=X) does not -- it
returned only 1 data point over a 1-year window in testing, vs. full
history for the two USD legs. So JPY/SGD is derived here as
USDSGD / USDJPY rather than pulled directly.

Run directly to fetch and store both:
    python src/data/market_data.py
"""

import pandas as pd
import yfinance as yf

# Benchmarks: S&P 500 and a TOPIX proxy (1306.T), per CLAUDE.md.
BENCHMARK_TICKERS = ["SPY", "1306.T"]

# The two USD legs used to build every FX rate this project needs.
_USD_LEG_TICKERS = {"SGD": "SGD=X", "JPY": "JPY=X"}


def _drop_price_spikes(s: pd.Series, tolerance: float = 2.0) -> tuple[pd.Series, int]:
    """
    Remove isolated price points that sit more than `tolerance` times
    away from their local median in either direction.

    Yahoo occasionally publishes a day or two of mis-scaled prices around
    a corporate action. 1306.T, the TOPIX ETF used as a benchmark, has
    two days in March 2026 priced at roughly a tenth of the surrounding
    level, with no split reported and the level fully restored
    afterwards. Those two points produce a -90% return followed by a
    +948% return, which on its own inflates the benchmark's volatility
    enough to drag a beta estimate to nearly zero -- it made TOPIX beta
    read 0.004 alongside a correlation of 0.20, which is not even
    internally consistent.

    A five-day centred median is robust to a one- or two-day break, and a
    2x band is far outside any legitimate single-day move for an index
    fund or a large-cap equity, so this removes the artefact without
    touching real volatility. Returns are then computed across the gap,
    which is the correct treatment: the price genuinely did move from the
    last good day to the next good day.
    """
    if len(s) < 5:
        return s, 0
    median = s.rolling(5, center=True, min_periods=3).median()
    ratio = s / median
    bad = (ratio > tolerance) | (ratio < 1.0 / tolerance)
    if not bad.any():
        return s, 0
    return s[~bad], int(bad.sum())


def _download_close(ticker: str, period: str) -> pd.Series:
    """Daily close prices for one yfinance ticker, indexed by timestamp."""
    df = yf.Ticker(ticker).history(period=period)
    if df.empty:
        return pd.Series(dtype=float)
    cleaned, dropped = _drop_price_spikes(df["Close"])
    if dropped:
        print(f"  warning: dropped {dropped} mis-scaled price point(s) for {ticker}")
    return cleaned


def get_fx_history(period: str = "2y") -> pd.DataFrame:
    """
    Daily FX rates as '1 unit of foreign currency = ? SGD', for USD and
    JPY -- the two currencies this portfolio actually holds.
    """
    usd_sgd = _download_close(_USD_LEG_TICKERS["SGD"], period)  # SGD per USD
    usd_jpy = _download_close(_USD_LEG_TICKERS["JPY"], period)  # JPY per USD

    rows = []
    for ts, sgd_per_usd in usd_sgd.items():
        rows.append({"date": ts.strftime("%Y-%m-%d"), "pair": "USDSGD", "rate": sgd_per_usd})
    for ts, jpy_per_usd in usd_jpy.items():
        if ts in usd_sgd.index and jpy_per_usd:
            sgd_per_jpy = usd_sgd.loc[ts] / jpy_per_usd
            rows.append({"date": ts.strftime("%Y-%m-%d"), "pair": "JPYSGD", "rate": sgd_per_jpy})

    return pd.DataFrame(rows)


def get_benchmark_history(tickers: list[str] = BENCHMARK_TICKERS, period: str = "2y") -> pd.DataFrame:
    """Daily close prices for each benchmark ticker."""
    rows = []
    for ticker in tickers:
        closes = _download_close(ticker, period)
        for ts, close in closes.items():
            rows.append({"date": ts.strftime("%Y-%m-%d"), "ticker": ticker, "close": close})
    return pd.DataFrame(rows)


def get_holding_price_history(symbols: list[str], period: str = "2y") -> pd.DataFrame:
    """
    Daily close prices (in each symbol's own local currency) for every
    symbol this account has ever held or traded. Needed for Phase 2's
    per-holding stock-move vs. FX-move decomposition, since Flex's
    OpenPosition only gives today's snapshot, not a daily history.
    """
    rows = []
    for symbol in symbols:
        closes = _download_close(symbol, period)
        if closes.empty:
            print(f"  warning: no price history found for {symbol!r}")
            continue
        for ts, close in closes.items():
            rows.append({"date": ts.strftime("%Y-%m-%d"), "symbol": symbol, "close": close})
    return pd.DataFrame(rows)


def _held_symbols(conn) -> list[str]:
    """Every symbol seen across positions, trades, and cash_transactions."""
    from storage import db

    symbols: set[str] = set()
    for table in ("positions", "trades", "cash_transactions"):
        df = db.read_table(conn, table)
        if "symbol" in df.columns:
            symbols |= set(df["symbol"].dropna().unique())
    return sorted(symbols)


def main() -> None:
    from storage import db

    conn = db.connect()
    try:
        print("Fetching FX history (USDSGD direct, JPYSGD derived)...")
        fx = get_fx_history()
        print("Fetching benchmark history (SPY, 1306.T)...")
        bench = get_benchmark_history()

        symbols = _held_symbols(conn)
        print(f"Fetching holding price history for {symbols}...")
        holdings = get_holding_price_history(symbols)

        n_fx = db.upsert_df(conn, "fx_rates", fx)
        n_bench = db.upsert_df(conn, "benchmark_prices", bench)
        n_holdings = db.upsert_df(conn, "holding_prices", holdings)
        print(f"fx_rates          {n_fx:>5} rows")
        print(f"benchmark_prices  {n_bench:>5} rows")
        print(f"holding_prices    {n_holdings:>5} rows")
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
