"""
Phase 2: P&L attribution.

For each holding, per day: split the change in base-currency value into
stock move (local price return) and FX move (exchange-rate return),
per R_base = (1 + r_local)(1 + r_fx) - 1 = r_local + r_fx + r_local*r_fx.
The interaction term (r_local * r_fx) is kept as its own line rather
than folded into either side, matching that formula literally. Plus
dividends (net of withholding tax) and trading costs/realized P&L,
both taken directly from Flex.

Key limitation: Flex's OpenPosition only gives today's snapshot, not a
daily history -- so daily share counts are reconstructed backward from
today's known quantity by undoing each trade in the `trades` table in
reverse chronological order (reconstruct_quantity_history). This only
works within the 365-day trades window, and only for symbols with a
trade in that window or a current position. VNQ, for example, was
fully exited more than 365 days ago (no trade shows up in our window),
so it has no reconstructible quantity history even though a
late-reported withholding-tax adjustment for it still shows up in
cash_transactions (see NOTES.md) -- its cash flows are still included
here, just not its mark-to-market attribution.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import db  # noqa: E402


def _fx_pair_for_currency(currency: str) -> str | None:
    """Which fx_rates 'pair' converts this currency to base (SGD)."""
    if currency == "USD":
        return "USDSGD"
    if currency == "JPY":
        return "JPYSGD"
    if currency == "SGD":
        return None  # already base currency
    raise ValueError(f"No FX pair configured for currency {currency!r}")


def reconstruct_quantity_history(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Daily share count per symbol, for every date in nav_history's
    range, reconstructed backward from today's known position by
    undoing each later trade. Returns: date, symbol, currency, quantity.
    """
    positions = db.read_table(conn, "positions")
    trades = db.read_table(conn, "trades")
    nav = db.read_table(conn, "nav_history")

    all_dates = sorted(nav["report_date"].unique())

    current_qty = dict(zip(positions["symbol"], positions["position"]))
    currency_by_symbol = dict(zip(positions["symbol"], positions["currency"]))
    for symbol, currency in zip(trades["symbol"], trades["currency"]):
        currency_by_symbol.setdefault(symbol, currency)

    symbols = sorted(set(current_qty) | set(trades["symbol"].dropna().unique()))

    rows = []
    for symbol in symbols:
        sym_trades = trades[trades["symbol"] == symbol]
        qty_now = current_qty.get(symbol, 0.0)
        currency = currency_by_symbol[symbol]
        for date in all_dates:
            later_qty = sym_trades.loc[sym_trades["trade_date"] > date, "quantity"].sum()
            rows.append({
                "date": date,
                "symbol": symbol,
                "currency": currency,
                "quantity": qty_now - later_qty,
            })

    return pd.DataFrame(rows)


def compute_mark_to_market(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Daily stock-move / FX-move / interaction P&L (in base currency,
    SGD) per symbol, using the quantity held overnight into each day.

    Prices and FX rates are forward-filled across nav_history's full
    date grid before differencing. Markets close on different holidays
    (see CLAUDE.md's "Known traps": forward-fill prices, never invent
    returns) -- without forward-fill, a day with no price for one
    symbol just drops that day's real price move instead of correctly
    carrying it forward into the next priced day, which silently
    biases the total in whichever direction the dropped days happened
    to move (before this fix, this overstated one symbol's contribution
    by ~24% of its true change -- see NOTES.md).
    """
    qty_history = reconstruct_quantity_history(conn).sort_values(["symbol", "date"])
    prices = db.read_table(conn, "holding_prices")
    fx = db.read_table(conn, "fx_rates")
    nav = db.read_table(conn, "nav_history")

    all_dates = sorted(nav["report_date"].unique())

    def _forward_fill(raw: dict, dates: list[str]) -> dict:
        filled: dict = {}
        last = None
        for d in dates:
            if d in raw:
                last = raw[d]
            filled[d] = last
        return filled

    price_series = {
        symbol: _forward_fill(dict(zip(g["date"], g["close"])), all_dates)
        for symbol, g in prices.groupby("symbol")
    }
    fx_series = {
        pair: _forward_fill(dict(zip(g["date"], g["rate"])), all_dates)
        for pair, g in fx.groupby("pair")
    }

    rows = []
    for symbol, group in qty_history.groupby("symbol"):
        currency = group["currency"].iloc[0]
        pair = _fx_pair_for_currency(currency)
        dates = group["date"].tolist()
        quantities = group["quantity"].tolist()
        prices_ff = price_series.get(symbol, {})
        fx_ff = {} if pair is None else fx_series.get(pair, {})

        for prev_date, date, qty_prev in zip(dates, dates[1:], quantities):
            if qty_prev == 0:
                continue  # not held overnight -- nothing to mark

            price_prev = prices_ff.get(prev_date)
            price_now = prices_ff.get(date)
            if not price_prev or price_now is None:
                continue  # no price known yet at all, this far back

            fx_prev = 1.0 if pair is None else fx_ff.get(prev_date)
            fx_now = 1.0 if pair is None else fx_ff.get(date)
            if not fx_prev or fx_now is None:
                continue

            r_local = price_now / price_prev - 1
            r_fx = fx_now / fx_prev - 1
            value_prev_base = qty_prev * price_prev * fx_prev

            rows.append({
                "date": date,
                "symbol": symbol,
                "currency": currency,
                "quantity": qty_prev,
                "value_prev_base": value_prev_base,
                "r_local": r_local,
                "r_fx": r_fx,
                "stock_move": value_prev_base * r_local,
                "fx_move": value_prev_base * r_fx,
                "interaction": value_prev_base * r_local * r_fx,
            })

    df = pd.DataFrame(rows)
    if not df.empty:
        df["total_move"] = df["stock_move"] + df["fx_move"] + df["interaction"]
    return df


def compute_dividend_cashflows(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Cash events per day, in base currency, from cash_transactions
    (currently just Dividends and Withholding Tax; not filtered by
    type so any new type shows up here rather than being silently
    dropped). Note: report_date reflects when IBKR posted the
    transaction, which can lag the actual event by months for
    corrections (see NOTES.md) -- treat it as "when this hit the
    ledger", not "when the dividend was paid".
    """
    cash = db.read_table(conn, "cash_transactions")
    if cash.empty:
        return pd.DataFrame(columns=["date", "symbol", "currency", "type", "amount", "amount_base"])
    df = cash.copy()
    df["amount_base"] = df["amount"] * df["fx_rate_to_base"]
    return df[["report_date", "symbol", "currency", "type", "amount", "amount_base"]].rename(
        columns={"report_date": "date"}
    )


def compute_trading_pnl(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Per-trade realized P&L, commissions, and taxes, in base currency.

    NOTE: fifo_pnl_realized is IBKR's own realized gain since the
    position's *original* cost basis, which can predate our 365-day
    window entirely (e.g. a position opened over a year ago and sold
    this week). It is NOT "the gain that happened within this window"
    -- that part is already covered by compute_mark_to_market. Shown
    here as a reference/cross-check figure; daily_summary() excludes
    it from the window P&L total to avoid double-counting (see
    NOTES.md).
    """
    trades = db.read_table(conn, "trades")
    if trades.empty:
        return pd.DataFrame(columns=["date", "symbol", "currency", "realized_pnl_base", "commission_base", "taxes_base"])
    df = trades.copy()
    df["realized_pnl_base"] = df["fifo_pnl_realized"] * df["fx_rate_to_base"]
    df["commission_base"] = df["ib_commission"] * df["fx_rate_to_base"]
    df["taxes_base"] = df["taxes"] * df["fx_rate_to_base"]
    return df[["trade_date", "symbol", "currency", "realized_pnl_base", "commission_base", "taxes_base"]].rename(
        columns={"trade_date": "date"}
    )


def daily_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    One row per date: every in-window P&L component, plus the total.
    Trading costs (commissions, taxes) are real cash costs incurred in
    the window and are included; fifo_pnl_realized is deliberately
    excluded (see compute_trading_pnl's docstring) -- it's available
    separately via compute_trading_pnl() for reference.
    """
    mtm = compute_mark_to_market(conn)
    div = compute_dividend_cashflows(conn)
    trd = compute_trading_pnl(conn)

    parts = []
    if not mtm.empty:
        parts.append(mtm.groupby("date")[["stock_move", "fx_move", "interaction"]].sum())
    if not div.empty:
        parts.append(div.groupby("date")[["amount_base"]].sum().rename(columns={"amount_base": "dividends"}))
    if not trd.empty:
        parts.append(trd.groupby("date")[["commission_base", "taxes_base"]].sum())

    if not parts:
        return pd.DataFrame()

    summary = pd.concat(parts, axis=1).fillna(0.0).sort_index()
    summary["total_pnl"] = summary.sum(axis=1)
    summary.index.name = "date"
    return summary.reset_index()


def daily_returns(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Daily portfolio return series -- the input Phase 3 (VaR, beta,
    correlation) and Phase 4a (factor regression) both need, since
    daily_summary() gives P&L in SGD but risk/factor work needs %.

    Returns two independent return series per date, which is
    deliberate:
      - `attributed_return`: our own computed P&L / prior day's NAV,
        i.e. bottom-up from the attribution components.
      - `nav_return`: the change in Flex's own reported NAV / prior
        day's NAV, i.e. top-down from IBKR's numbers.
    Their difference (`return_diff`) localises the residual
    reconciliation gap to specific days instead of leaving it as one
    cumulative number -- much sharper for diagnosis. See NOTES.md.

    Caveat: nav_return treats every NAV change as investment return,
    which only holds while external cash flows are zero (verified for
    this account via cash_report -- deposits and withdrawals are both
    0). Re-check that assumption after any deposit or withdrawal.
    """
    summary = daily_summary(conn)
    nav = db.read_table(conn, "nav_history").sort_values("report_date").copy()
    if summary.empty or nav.empty:
        return pd.DataFrame()

    nav["prior_total"] = nav["total"].shift(1)
    nav["nav_return"] = (nav["total"] - nav["prior_total"]) / nav["prior_total"]

    merged = summary.merge(
        nav[["report_date", "prior_total", "total", "nav_return"]],
        left_on="date",
        right_on="report_date",
        how="left",
    ).drop(columns=["report_date"])

    merged["attributed_return"] = merged["total_pnl"] / merged["prior_total"]
    merged["return_diff"] = merged["attributed_return"] - merged["nav_return"]

    return merged[[
        "date", "total_pnl", "prior_total", "total",
        "attributed_return", "nav_return", "return_diff",
    ]].rename(columns={"prior_total": "nav_prior", "total": "nav_close"})


def rollup_by_currency(conn: sqlite3.Connection) -> pd.DataFrame:
    """Mark-to-market P&L summed by currency (JPY sleeve vs. USD sleeve)."""
    mtm = compute_mark_to_market(conn)
    if mtm.empty:
        return pd.DataFrame()
    return (
        mtm.groupby("currency")[["stock_move", "fx_move", "interaction", "total_move"]]
        .sum()
        .reset_index()
    )


def realized_vs_unrealized(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Split each symbol's in-window mark-to-market P&L into realized
    (position was closed during the window -- P&L is locked in) vs.
    unrealized (still held -- a paper gain/loss as of the latest date).
    Based on whether the symbol appears in the current positions table,
    not on IBKR's fifo_pnl_realized (see compute_trading_pnl).
    """
    mtm = compute_mark_to_market(conn)
    if mtm.empty:
        return pd.DataFrame()
    positions = db.read_table(conn, "positions")
    still_held = set(positions["symbol"].unique())

    by_symbol = mtm.groupby("symbol")["total_move"].sum().reset_index()
    by_symbol["status"] = by_symbol["symbol"].map(
        lambda s: "unrealized" if s in still_held else "realized"
    )
    return by_symbol.groupby("status")["total_move"].sum().reset_index()


def validate_against_nav(conn: sqlite3.Connection) -> dict:
    """
    Sanity check: cumulative computed P&L over the window should match
    the change in Flex's own NAV, net of external cash flows (deposits
    minus withdrawals, from cash_report).
    """
    summary = daily_summary(conn)
    nav = db.read_table(conn, "nav_history").sort_values("report_date")

    nav_start = nav["total"].iloc[0]
    nav_end = nav["total"].iloc[-1]
    nav_change = nav_end - nav_start
    computed_change = summary["total_pnl"].sum() if not summary.empty else 0.0

    cash_report = db.read_table(conn, "cash_report")
    base_row = cash_report[cash_report["currency"] == "BASE_SUMMARY"]
    net_flows = 0.0
    if not base_row.empty:
        net_flows = float(base_row["deposits"].iloc[0] + base_row["withdrawals"].iloc[0])

    expected_pnl = nav_change - net_flows

    return {
        "nav_change": nav_change,
        "net_external_flows": net_flows,
        "expected_investment_pnl": expected_pnl,
        "computed_pnl": computed_change,
        "diff": computed_change - expected_pnl,
    }


def main() -> None:
    conn = db.connect()
    try:
        summary = daily_summary(conn)
        print("Daily P&L summary (last 10 days):")
        print(summary.tail(10).to_string(index=False))

        print("\nRoll-up by currency (mark-to-market only):")
        print(rollup_by_currency(conn).to_string(index=False))

        print("\nRealized vs. unrealized (in-window mark-to-market):")
        print(realized_vs_unrealized(conn).to_string(index=False))

        trd = compute_trading_pnl(conn)
        print(f"\nIBKR's own realized P&L on closed trades (reference, spans pre-window cost basis, NOT in total): "
              f"{trd['realized_pnl_base'].sum():,.4f}" if not trd.empty else "\nNo closed trades in window.")

        check = validate_against_nav(conn)
        print("\nValidation vs. nav_history (cumulative, whole window):")
        for k, v in check.items():
            print(f"  {k}: {v:,.4f}")

        rets = daily_returns(conn).dropna(subset=["return_diff"])
        if not rets.empty:
            d = rets["return_diff"]
            print("\nDaily return tracking vs. IBKR's own NAV:")
            print(f"  mean difference:   {d.mean() * 10000:>8.2f} bps/day")
            print(f"  tracking error:    {d.std() * 10000:>8.2f} bps/day")
            print(f"  series correlation:{rets['attributed_return'].corr(rets['nav_return']):>8.4f}")
            print(f"  lag-1 autocorr:    {d.autocorr(1):>8.4f}  (FX snapshot timing, see NOTES.md)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
