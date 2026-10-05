"""
Annual financial-statement data via yfinance, for the Phase 4b screener.

Two things about this module are more important than the fetching.

**The reporting lag is stored, not assumed.** A fiscal year ending
2026-03-31 is not public knowledge on 2026-03-31 -- a Japanese company
with a March year-end files in June, a US company with a January year-end
files in March. Scoring a stock on the fiscal-year-end date uses
information nobody had, which makes any backtest built on it fiction. So
every row carries an `available_date` of fiscal_date + REPORTING_LAG_DAYS,
and the screener filters on that column rather than on fiscal_date. The
lag is deliberately conservative: a real filing calendar would be more
precise, but erring late only ever understates a result, while erring
early manufactures one.

**Missing line items are not zeros.** A bank has no cost of revenue and
no current/non-current balance-sheet split, because those concepts do not
apply to it -- Mitsubishi UFJ (8306.T) is missing Cost Of Revenue, Gross
Profit, Current Assets and Current Liabilities for that reason, not
because the data is patchy. Nothing is defaulted or filled here; absent
items are simply absent, and the screener is responsible for refusing to
score a metric whose inputs it does not have.

Run directly to fetch and store for every symbol the account has held:
    python src/data/fundamentals.py
"""

from __future__ import annotations

import warnings

import pandas as pd
import yfinance as yf

# Conservative uniform lag from fiscal year-end to the date the figures
# could first have been acted on. Three months is the convention in the
# factor literature and errs late rather than early; see the module
# docstring on why the direction of the error matters.
REPORTING_LAG_DAYS = 90

# Line items the screener needs, by statement. Names are yfinance's own
# row labels, verified against live data for both US and Tokyo listings.
ITEMS = {
    "income_stmt": [
        "Total Revenue",
        "Cost Of Revenue",
        "Gross Profit",
        "Net Income",
    ],
    "balance_sheet": [
        "Total Assets",
        "Current Assets",
        "Current Liabilities",
        "Long Term Debt",
        "Stockholders Equity",
        "Ordinary Shares Number",
        "Share Issued",
    ],
    "cashflow": [
        "Operating Cash Flow",
    ],
}


def get_fundamentals(symbols: list[str]) -> pd.DataFrame:
    """
    Annual statement line items for each symbol, in long format.

    Returns columns (symbol, fiscal_date, available_date, statement,
    item, value). Items a company does not report are omitted rather than
    written as nulls, so the screener can tell "not reported" from
    "reported as zero" -- a distinction that decides whether a metric is
    unavailable or genuinely bad.
    """
    rows: list[dict] = []
    for symbol in symbols:
        ticker = yf.Ticker(symbol)
        found = 0
        for statement, wanted in ITEMS.items():
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    df = getattr(ticker, statement)
            except Exception as exc:  # yfinance raises a variety of these
                print(f"  warning: {symbol} {statement} failed: {type(exc).__name__}")
                continue
            if df is None or df.empty:
                print(f"  warning: {symbol} {statement} is empty")
                continue

            for item in wanted:
                if item not in df.index:
                    continue
                for fiscal_ts, value in df.loc[item].items():
                    if pd.isna(value):
                        continue
                    fiscal = pd.Timestamp(fiscal_ts)
                    available = fiscal + pd.Timedelta(days=REPORTING_LAG_DAYS)
                    rows.append({
                        "symbol": symbol,
                        "fiscal_date": fiscal.strftime("%Y-%m-%d"),
                        "available_date": available.strftime("%Y-%m-%d"),
                        "statement": statement,
                        "item": item,
                        "value": float(value),
                    })
                    found += 1
        print(f"  {symbol:>8}  {found:>3} line items")

    return pd.DataFrame(rows)


def as_of(
    conn,
    knowledge_date: str | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    """
    Fundamentals as they were knowable on `knowledge_date`, wide by item.

    This is the only function the screener should use to read
    fundamentals, because it is where the look-ahead filter lives: rows
    whose available_date is after the knowledge date are dropped, so a
    screen run for a past date cannot see a filing that had not happened.

    Returns one row per (symbol, fiscal_date) with items as columns,
    sorted most recent fiscal year last.
    """
    from storage import db

    df = db.read_table(conn, "fundamentals")
    if df.empty:
        return pd.DataFrame()
    if symbols is not None:
        df = df[df["symbol"].isin(symbols)]
    if knowledge_date is not None:
        df = df[df["available_date"] <= knowledge_date]
    if df.empty:
        return pd.DataFrame()

    wide = df.pivot_table(
        index=["symbol", "fiscal_date"],
        columns="item",
        values="value",
        aggfunc="first",
    )
    return wide.sort_index().reset_index()


def main() -> None:
    from data import market_data
    from storage import db

    conn = db.connect()
    try:
        symbols = market_data._held_symbols(conn)
        print(f"Fetching annual fundamentals for {len(symbols)} symbols...")
        df = get_fundamentals(symbols)
        if df.empty:
            print("No fundamentals returned.")
            return
        n = db.upsert_df(conn, "fundamentals", df)
        print(f"\nfundamentals  {n:>5} rows  "
              f"({df['symbol'].nunique()} symbols, "
              f"{df['fiscal_date'].nunique()} fiscal year-ends)")
        print(f"Reporting lag applied: {REPORTING_LAG_DAYS} days from fiscal "
              f"year-end to assumed availability.")

        # Report coverage per symbol, since the screener's behaviour
        # depends entirely on which items exist.
        pivot = df.pivot_table(index="symbol", columns="item", values="value",
                               aggfunc="count")
        wanted = [i for items in ITEMS.values() for i in items]
        missing = {
            symbol: [i for i in wanted if i not in row.index or pd.isna(row.get(i))]
            for symbol, row in pivot.iterrows()
        }
        print("\nItems not reported (these disable metrics rather than scoring zero):")
        for symbol, items in missing.items():
            print(f"  {symbol:>8}  {', '.join(items) if items else '-- complete --'}")
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
