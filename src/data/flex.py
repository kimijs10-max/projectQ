"""
IBKR Flex Web Service: download the configured Flex Query and reshape
each section into the DataFrame shape src/storage/db.py's tables expect.

ib_async.FlexReport.download() already polls IBKR until the report is
ready, but its loop only checks whether the response looks like a
"still generating" message -- it doesn't check that what it settles on
is actually the final FlexQueryResponse. In testing, one run broke out
of that loop early on some other transient response (see NOTES.md).
download_report() below wraps it with its own retry that verifies the
root tag before accepting the result.

Run directly to do a full download + parse + store + reconcile pass:
    python src/data/flex.py
"""

import os
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from ib_async import FlexReport
from ib_async.flexreport import FlexError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

FLEX_TOKEN = os.getenv("FLEX_TOKEN")
FLEX_QUERY_ID = os.getenv("FLEX_QUERY_ID")

MAX_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 15

# Raw XML is cached here on every successful download -- both as an
# audit trail and so local dev/testing doesn't need to re-hit IBKR's
# (slow, occasionally flaky) endpoint every time.
RAW_CACHE_DIR = PROJECT_ROOT / "data" / "flex_raw"


def _to_iso_date(value) -> str | None:
    """IBKR's 'YYYYMMDD' (str or float-ish) -> 'YYYY-MM-DD'. Blank -> None."""
    if value is None or value == "":
        return None
    s = str(int(float(value)))
    if len(s) != 8:
        return None
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def download_report(token: str | None = None, query_id: str | None = None) -> FlexReport:
    """Download the Flex Query, retrying on transient not-ready responses."""
    token = token or FLEX_TOKEN
    query_id = query_id or FLEX_QUERY_ID
    if not token or not query_id:
        raise RuntimeError(
            "FLEX_TOKEN and FLEX_QUERY_ID must be set in .env (see .env.example)."
        )

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            report = FlexReport(token=token, queryId=query_id)
        except FlexError as exc:
            last_error = exc
        else:
            if report.root.tag == "FlexQueryResponse":
                _cache_raw(report)
                return report
            last_error = RuntimeError(
                f"Got root tag {report.root.tag!r} instead of FlexQueryResponse "
                "-- report likely wasn't ready yet."
            )
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)
    raise RuntimeError(
        f"Flex report never became ready after {MAX_ATTEMPTS} attempts"
    ) from last_error


def _cache_raw(report: FlexReport) -> None:
    RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    report.save(RAW_CACHE_DIR / f"{stamp}.xml")


def load_cached_report(path: Path) -> FlexReport:
    """Load a previously-saved raw XML file instead of hitting the network."""
    report = FlexReport()
    report.load(str(path))
    return report


# --- section parsers -----------------------------------------------------
# Each returns a DataFrame whose columns already match the corresponding
# table in src/storage/db.py.

def get_positions(report: FlexReport) -> pd.DataFrame:
    df = report.df("OpenPosition")
    if df is None or df.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "report_date": df["reportDate"].map(_to_iso_date),
        "account_id": df["accountId"],
        "conid": df["conid"].astype(str),
        "symbol": df["symbol"],
        "description": df["description"],
        "currency": df["currency"],
        "fx_rate_to_base": df["fxRateToBase"],
        "asset_category": df["assetCategory"],
        "listing_exchange": df["listingExchange"],
        "position": df["position"],
        "mark_price": df["markPrice"],
        "position_value": df["positionValue"],
        "cost_basis_price": df["costBasisPrice"],
        "cost_basis_money": df["costBasisMoney"],
        "open_price": df["openPrice"],
        "percent_of_nav": df["percentOfNAV"],
        "fifo_pnl_unrealized": df["fifoPnlUnrealized"],
        "side": df["side"],
    })


def get_trades(report: FlexReport) -> pd.DataFrame:
    df = report.df("Trade")
    if df is None or df.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "transaction_id": df["transactionID"].astype(str),
        "account_id": df["accountId"],
        "conid": df["conid"].astype(str),
        "symbol": df["symbol"],
        "description": df["description"],
        "currency": df["currency"],
        "fx_rate_to_base": df["fxRateToBase"],
        "trade_date": df["tradeDate"].map(_to_iso_date),
        "date_time": df["dateTime"],
        # When the order was submitted, as opposed to when it filled.
        # Phase 6 needs it for the arrival price. Same format and time
        # zone as dateTime.
        "order_time": df["orderTime"] if "orderTime" in df.columns else None,
        "buy_sell": df["buySell"],
        "quantity": df["quantity"],
        "trade_price": df["tradePrice"],
        "trade_money": df["tradeMoney"],
        "proceeds": df["proceeds"],
        "ib_commission": df["ibCommission"],
        "taxes": df["taxes"],
        "net_cash": df["netCash"],
        "close_price": df["closePrice"],
        "fifo_pnl_realized": df["fifoPnlRealized"],
        "order_type": df["orderType"],
        "exchange": df["exchange"],
    })


def get_cash_transactions(report: FlexReport) -> pd.DataFrame:
    df = report.df("CashTransaction")
    if df is None or df.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "transaction_id": df["transactionID"].astype(str),
        "account_id": df["accountId"],
        "conid": df["conid"].astype(str),
        "symbol": df["symbol"],
        "description": df["description"],
        "currency": df["currency"],
        "fx_rate_to_base": df["fxRateToBase"],
        "date_time": df["dateTime"],
        "settle_date": df["settleDate"].map(_to_iso_date),
        "report_date": df["reportDate"].map(_to_iso_date),
        "type": df["type"],
        "amount": df["amount"],
        "dividend_type": df["dividendType"],
    })


def get_nav_history(report: FlexReport) -> pd.DataFrame:
    df = report.df("EquitySummaryByReportDateInBase")
    if df is None or df.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "report_date": df["reportDate"].map(_to_iso_date),
        "account_id": df["accountId"],
        "currency": df["currency"],
        "cash": df["cash"],
        "stock": df["stock"],
        "dividend_accruals": df["dividendAccruals"],
        "interest_accruals": df["interestAccruals"],
        "total": df["total"],
    })


def get_corporate_actions(report: FlexReport) -> pd.DataFrame:
    """
    Best-effort mapping -- the account hasn't had a corporate action yet
    (query returns 0 rows as of Phase 0/1), so these column names are
    from IBKR's documented Flex schema, not verified against real data.
    Revisit once a real row shows up.
    """
    df = report.df("CorporateAction")
    if df is None or df.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "action_id": df.get("actionID", pd.Series(dtype=str)).astype(str),
        "account_id": df.get("accountId"),
        "conid": df.get("conid", pd.Series(dtype=str)).astype(str),
        "symbol": df.get("symbol"),
        "description": df.get("description"),
        "currency": df.get("currency"),
        "fx_rate_to_base": df.get("fxRateToBase"),
        "report_date": df["reportDate"].map(_to_iso_date) if "reportDate" in df else None,
        "date_time": df.get("dateTime"),
        "type": df.get("type"),
        "quantity": df.get("quantity"),
        "proceeds": df.get("proceeds"),
        "value": df.get("value"),
    })


def get_cash_report(report: FlexReport) -> pd.DataFrame:
    """Period cash-flow summary (dividends, fees, deposits, ...) per currency."""
    df = report.df("CashReportCurrency")
    if df is None or df.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "account_id": df["accountId"],
        "currency": df["currency"],
        "to_date": df["toDate"].map(_to_iso_date),
        "from_date": df["fromDate"].map(_to_iso_date),
        "starting_cash": df["startingCash"],
        "ending_cash": df["endingCash"],
        "deposits": df["deposits"],
        "withdrawals": df["withdrawals"],
        "dividends": df["dividends"],
        "commissions": df["commissions"],
        "withholding_tax": df["withholdingTax"],
        "broker_interest": df["brokerInterest"],
        "net_trades_sales": df["netTradesSales"],
        "net_trades_purchases": df["netTradesPurchases"],
    })


def main() -> None:
    from storage import db  # local import so this file can be imported standalone

    print("Downloading Flex report...")
    report = download_report()
    print(f"Cached raw XML under {RAW_CACHE_DIR}/")

    sections = {
        "positions": get_positions(report),
        "trades": get_trades(report),
        "cash_transactions": get_cash_transactions(report),
        "nav_history": get_nav_history(report),
        "corporate_actions": get_corporate_actions(report),
        "cash_report": get_cash_report(report),
    }

    conn = db.connect()
    try:
        for table, df in sections.items():
            n = db.upsert_df(conn, table, df)
            print(f"{table:<20} {n:>5} rows")
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    main()
