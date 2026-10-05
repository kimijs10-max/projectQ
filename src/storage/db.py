"""
SQLite storage for daily snapshots pulled from IBKR Flex and market data.

Six tables map directly to the sections of the configured Flex Query:
positions, trades, cash_transactions, nav_history, corporate_actions,
cash_report. Three more (fx_rates, benchmark_prices, holding_prices) hold
price history pulled from yfinance in market_data.py, factor_returns holds
the Kenneth French factors, and fundamentals holds annual financial
statement line items for the screener.

All writes are idempotent upserts keyed on each table's natural primary
key, so re-running the same day's pull never creates duplicates.
"""

import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "portfolio.db"

SCHEMA = {
    "positions": """
        CREATE TABLE IF NOT EXISTS positions (
            report_date TEXT NOT NULL,
            account_id TEXT NOT NULL,
            conid TEXT NOT NULL,
            symbol TEXT,
            description TEXT,
            currency TEXT,
            fx_rate_to_base REAL,
            asset_category TEXT,
            listing_exchange TEXT,
            position REAL,
            mark_price REAL,
            position_value REAL,
            cost_basis_price REAL,
            cost_basis_money REAL,
            open_price REAL,
            percent_of_nav REAL,
            fifo_pnl_unrealized REAL,
            side TEXT,
            PRIMARY KEY (report_date, account_id, conid)
        )
    """,
    "trades": """
        CREATE TABLE IF NOT EXISTS trades (
            transaction_id TEXT PRIMARY KEY,
            account_id TEXT,
            conid TEXT,
            symbol TEXT,
            description TEXT,
            currency TEXT,
            fx_rate_to_base REAL,
            trade_date TEXT,
            date_time TEXT,
            buy_sell TEXT,
            quantity REAL,
            trade_price REAL,
            trade_money REAL,
            proceeds REAL,
            ib_commission REAL,
            taxes REAL,
            net_cash REAL,
            close_price REAL,
            fifo_pnl_realized REAL,
            order_type TEXT,
            exchange TEXT
        )
    """,
    "cash_transactions": """
        CREATE TABLE IF NOT EXISTS cash_transactions (
            transaction_id TEXT PRIMARY KEY,
            account_id TEXT,
            conid TEXT,
            symbol TEXT,
            description TEXT,
            currency TEXT,
            fx_rate_to_base REAL,
            date_time TEXT,
            settle_date TEXT,
            report_date TEXT,
            type TEXT,
            amount REAL,
            dividend_type TEXT
        )
    """,
    "nav_history": """
        CREATE TABLE IF NOT EXISTS nav_history (
            report_date TEXT NOT NULL,
            account_id TEXT NOT NULL,
            currency TEXT,
            cash REAL,
            stock REAL,
            dividend_accruals REAL,
            interest_accruals REAL,
            total REAL,
            PRIMARY KEY (report_date, account_id)
        )
    """,
    "corporate_actions": """
        CREATE TABLE IF NOT EXISTS corporate_actions (
            action_id TEXT PRIMARY KEY,
            account_id TEXT,
            conid TEXT,
            symbol TEXT,
            description TEXT,
            currency TEXT,
            fx_rate_to_base REAL,
            report_date TEXT,
            date_time TEXT,
            type TEXT,
            quantity REAL,
            proceeds REAL,
            value REAL
        )
    """,
    "cash_report": """
        CREATE TABLE IF NOT EXISTS cash_report (
            account_id TEXT NOT NULL,
            currency TEXT NOT NULL,
            to_date TEXT NOT NULL,
            from_date TEXT,
            starting_cash REAL,
            ending_cash REAL,
            deposits REAL,
            withdrawals REAL,
            dividends REAL,
            commissions REAL,
            withholding_tax REAL,
            broker_interest REAL,
            net_trades_sales REAL,
            net_trades_purchases REAL,
            PRIMARY KEY (account_id, currency, to_date)
        )
    """,
    "fx_rates": """
        CREATE TABLE IF NOT EXISTS fx_rates (
            date TEXT NOT NULL,
            pair TEXT NOT NULL,
            rate REAL,
            PRIMARY KEY (date, pair)
        )
    """,
    "benchmark_prices": """
        CREATE TABLE IF NOT EXISTS benchmark_prices (
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            close REAL,
            PRIMARY KEY (date, ticker)
        )
    """,
    "holding_prices": """
        CREATE TABLE IF NOT EXISTS holding_prices (
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            close REAL,
            PRIMARY KEY (date, symbol)
        )
    """,
    # Fama-French factor returns, stored long rather than wide so a new
    # factor or region needs no schema change. Values are decimal
    # returns (the source publishes percent; factor_data.py converts).
    "factor_returns": """
        CREATE TABLE IF NOT EXISTS factor_returns (
            date TEXT NOT NULL,
            region TEXT NOT NULL,
            factor TEXT NOT NULL,
            value REAL,
            PRIMARY KEY (date, region, factor)
        )
    """,
    # Daily closes for the screening universe (holdings and their verified
    # peers), in each symbol's own local currency.
    #
    # Deliberately separate from holding_prices rather than shared with
    # it. holding_prices is populated from symbols the account has
    # actually held, and anything reading it is entitled to assume that;
    # dropping 140 peer tickers into it would make the table quietly lie
    # about what the account owns. The duplicated rows for the holdings
    # themselves are public price data and cost nothing.
    "screen_prices": """
        CREATE TABLE IF NOT EXISTS screen_prices (
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            close REAL,
            PRIMARY KEY (date, symbol)
        )
    """,
    # Issuer classification, used to verify peer groups. Stored rather
    # than fetched on demand because the peer sets are hand-curated and
    # the whole point of the table is to let the data contradict the
    # curation -- a ticker that resolves into the wrong sector must be
    # caught and dropped, not silently ranked.
    "security_meta": """
        CREATE TABLE IF NOT EXISTS security_meta (
            symbol TEXT PRIMARY KEY,
            short_name TEXT,
            sector TEXT,
            industry TEXT,
            quote_type TEXT,
            currency TEXT,
            fetched_at TEXT
        )
    """,
    # Annual financial-statement line items, long format.
    #
    # available_date is stored rather than derived at read time so the
    # look-ahead lag is visible in the data itself: a fiscal year ending
    # 2026-03-31 is not public knowledge on 2026-03-31, and any screen or
    # backtest that reads fiscal_date as the knowledge date is fiction.
    # Every query that scores a point in time filters on available_date.
    "fundamentals": """
        CREATE TABLE IF NOT EXISTS fundamentals (
            symbol TEXT NOT NULL,
            fiscal_date TEXT NOT NULL,
            available_date TEXT NOT NULL,
            statement TEXT NOT NULL,
            item TEXT NOT NULL,
            value REAL,
            PRIMARY KEY (symbol, fiscal_date, statement, item)
        )
    """,
}


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite DB and ensure all tables exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    for ddl in SCHEMA.values():
        conn.execute(ddl)
    conn.commit()
    return conn


def upsert_df(conn: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """
    Insert-or-replace every row of df into table.

    df's columns must already use the table's column names (a subset is
    fine). Returns the number of rows written.
    """
    if df is None or df.empty:
        return 0
    if table not in SCHEMA:
        raise ValueError(f"Unknown table: {table!r}")

    cols = list(df.columns)
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
    rows = [tuple(r) for r in df[cols].itertuples(index=False, name=None)]
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def read_table(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    """Convenience read-back for a whole table, e.g. for reconciliation checks."""
    return pd.read_sql_query(f"SELECT * FROM {table}", conn)
