"""
Stocks that fell further than their peers, and whether the business did.

The idea is contrarian: a price that has dropped much more than comparable
companies', while the company's own results have not deteriorated, may be
a fall the business does not justify -- buy it and wait.

That is two separate claims, and this module treats them separately.

**"Fell more than others" is measured against the peer group.** A stock's
return over the look-back window is compared with the median of its own
verified peer group. A sector-wide fall therefore flags nobody, which is
the point: the question is what happened to *this* company. A name is
flagged when it is in the bottom fifth of its group and well behind the
group's median (config/reversal.py).

**"Irrational" cannot be observed; "unexplained by reported results"
can.** For each flagged name the latest quarter's revenue and profit are
compared with the same quarter a year earlier. If revenue shrank, profit
fell sharply or the quarter was a loss, the fall has a visible reason. If
none of those happened, the fall is *unexplained by the numbers* -- which
is as far as statements can take it. The market also falls on things no
income statement shows yet: a cut to guidance, a lost customer, a lawsuit,
a technology shift. Every name this flags needs its news read before
anything else. The flag means "the obvious explanation is absent", not
"there is no explanation".

**Whether fallers recover is tested, not assumed.** The well-documented
pattern over three to twelve months is the opposite of this strategy:
losers tend to keep losing (momentum). So before listing anything, the
module replays the first half of the idea over the price history: at each
month-end, take the worst fifth of every peer group and follow them. It
reports the average, but also the share of fallers that beat their peers
-- because an average can be carried by a few large rebounds while the
typical faller does nothing.

**Survivorship bias runs in this strategy's favour, and cannot be
removed.** The universe is companies that exist today. The fallers that
kept falling until they were delisted are not in it, so the history test
only ever follows fallers that survived. Every figure it reports is an
upper bound on what buying fallers actually returned.

Balance-sheet strength (equity ratio, current ratio) is carried over from
the stage-one screen: waiting for a recovery is only an option for a
company that can afford the wait.

Run directly (fetches quarterly results for the flagged names):
    python src/screener/fallen.py
"""

from __future__ import annotations

import sqlite3
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config"))

import reversal as cfg  # noqa: E402
import valuation as cfg_value  # noqa: E402
from screener import candidates  # noqa: E402
from storage import db  # noqa: E402

QUARTERLY_ITEMS = ["Total Revenue", "Net Income"]
# How far from exactly one year earlier the comparison quarter may be.
YEAR_TOLERANCE_DAYS = 20


# --------------------------------------------------------------------------
# Maths. Pure functions -- tested in tests/test_fallen.py.
# --------------------------------------------------------------------------

def relative_to_group(values: pd.Series, groups: pd.Series) -> pd.DataFrame:
    """
    Each name's value against its own peer group: the group median, the
    shortfall from it, and the name's percentile rank within the group
    (0 = worst). Names without a value are dropped before ranking.
    """
    frame = pd.DataFrame({"value": values, "group": groups}).dropna()
    grouped = frame.groupby("group")["value"]
    frame["group_median"] = grouped.transform("median")
    frame["shortfall"] = frame["value"] - frame["group_median"]
    frame["rank"] = grouped.rank(pct=True)
    frame["group_size"] = grouped.transform("size")
    return frame


def is_fallen(relative: pd.DataFrame) -> pd.Series:
    """Down in absolute terms, bottom FALL_QUANTILE of its group, and at
    least MIN_SHORTFALL behind the group median."""
    return ((relative["value"] < 0)
            & (relative["rank"] <= cfg.FALL_QUANTILE)
            & (relative["shortfall"] <= -cfg.MIN_SHORTFALL))


def year_on_year(series: pd.Series) -> tuple[float | None, float | None, str | None]:
    """
    Latest value and the value one year before it, from a series indexed
    by fiscal date. Returns (latest, year_ago, latest date); year_ago is
    None when no quarter sits within YEAR_TOLERANCE_DAYS of a year back.
    """
    series = series.dropna().sort_index()
    if series.empty:
        return None, None, None
    latest_date = series.index[-1]
    target = latest_date - pd.DateOffset(years=1)
    gaps = abs(series.index - target)
    nearest = gaps.argmin()
    year_ago = float(series.iloc[nearest]) if gaps[nearest].days <= YEAR_TOLERANCE_DAYS else None
    return float(series.iloc[-1]), year_ago, str(latest_date.date())


def business_verdict(
    revenue: float | None, revenue_prior: float | None,
    profit: float | None, profit_prior: float | None,
) -> tuple[str, str]:
    """
    (status, reason). Status is "explained" when reported results give a
    visible reason for the fall, "unexplained" when they do not, and
    "unknown" when there is no year-on-year comparison to make -- which
    is never treated as good news.
    """
    if None in (revenue, revenue_prior, profit, profit_prior) or not revenue_prior:
        return "unknown", "no year-on-year quarter to compare"
    reasons = []
    revenue_change = revenue / revenue_prior - 1
    if revenue_change < 0:
        reasons.append(f"revenue {revenue_change * 100:+.0f}%")
    if profit <= 0:
        reasons.append("quarter was a loss")
    elif profit_prior > 0 and profit / profit_prior - 1 < -cfg.PROFIT_DROP:
        reasons.append(f"profit {(profit / profit_prior - 1) * 100:+.0f}%")
    if reasons:
        return "explained", ", ".join(reasons)
    return "unexplained", "revenue and profit held up"


def reversal_test(monthly: pd.DataFrame, groups: pd.Series) -> pd.DataFrame:
    """
    Did the worst fallers go on to beat their peers?

    `monthly` is month-end prices, one column per name. For every
    look-back L and holding period H, at each month-end: rank each name's
    L-month return within its peer group, take the bottom FALL_QUANTILE,
    and measure everyone's next-H-month return relative to the group
    median. Reports, averaged over formation months:

      faller_median  the median faller's forward return vs. peers
      rest_median    the same for everyone else
      spread_mean    mean faller minus mean non-faller
      months_ahead   share of formation months in which that spread > 0
      beat_peers     share of fallers that beat their group's median

    Formation months overlap, so the months are not independent and no
    p-value is reported.
    """
    rows = []
    for lookback in cfg.LOOKBACK_GRID:
        past = monthly / monthly.shift(lookback) - 1
        for holding in cfg.HOLDING_GRID:
            forward = monthly.shift(-holding) / monthly - 1
            stats = []
            for date in monthly.index:
                p, f = past.loc[date].dropna(), forward.loc[date].dropna()
                names = p.index.intersection(f.index)
                if len(names) < cfg.MIN_NAMES:
                    continue
                formed = relative_to_group(p[names], groups.reindex(names))
                ahead = relative_to_group(f[names], groups.reindex(names))["shortfall"]
                fallers = ahead[formed["rank"] <= cfg.FALL_QUANTILE]
                rest = ahead[formed["rank"] > cfg.FALL_QUANTILE]
                if fallers.empty or rest.empty:
                    continue
                stats.append((fallers.median(), rest.median(),
                              fallers.mean() - rest.mean(), (fallers > 0).mean()))
            if not stats:
                continue
            arr = np.array(stats)
            rows.append({
                "lookback": lookback, "holding": holding, "months": len(arr),
                "faller_median": arr[:, 0].mean(), "rest_median": arr[:, 1].mean(),
                "spread_mean": arr[:, 2].mean(),
                "months_ahead": float((arr[:, 2] > 0).mean()),
                "beat_peers": arr[:, 3].mean(),
            })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Inputs.
# --------------------------------------------------------------------------

def _prices(conn: sqlite3.Connection) -> pd.DataFrame:
    """Daily closes, one column per universe name, forward-filled over holidays."""
    df = db.read_table(conn, "screen_prices").dropna(subset=["close"])
    df["date"] = pd.to_datetime(df["date"])
    wide = df.pivot(index="date", columns="symbol", values="close").sort_index()
    # Carry a price across a market holiday, but not across a long gap:
    # a name that stopped trading should drop out, not sit flat.
    return wide.ffill(limit=5)


def fetch_quarterly(symbols: list[str]) -> pd.DataFrame:
    """Latest quarterly revenue and net income for `symbols`, from yfinance."""
    import yfinance as yf

    now = pd.Timestamp.now("UTC").strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for symbol in symbols:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                statement = yf.Ticker(symbol).quarterly_income_stmt
        except Exception as exc:
            print(f"  warning: {symbol} quarterly statement failed: {type(exc).__name__}")
            continue
        if statement is None or statement.empty:
            print(f"  warning: {symbol} has no quarterly statement")
            continue
        for item in QUARTERLY_ITEMS:
            if item not in statement.index:
                continue
            for fiscal, value in statement.loc[item].items():
                if pd.notna(value):
                    rows.append({"symbol": symbol,
                                 "fiscal_date": pd.Timestamp(fiscal).strftime("%Y-%m-%d"),
                                 "item": item, "value": float(value), "fetched_at": now})
    return pd.DataFrame(rows)


def screen(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Every universe name with its look-back return against its peer group,
    whether it counts as fallen, and -- for the fallen -- what its latest
    quarter says. Quarterly data is read from the database only; main()
    is what fetches it.
    """
    names = candidates._universe(conn)
    prices = _prices(conn)
    if names.empty or prices.empty:
        return pd.DataFrame()
    groups = names.set_index("symbol")["group"]

    latest = prices.index[-1]
    start = latest - pd.DateOffset(months=cfg.LOOKBACK_MONTHS)
    then = prices.loc[:start].iloc[-1] if not prices.loc[:start].empty else None
    if then is None:
        return pd.DataFrame()
    now = prices.iloc[-1]
    returns = (now / then - 1).dropna()
    high = prices.loc[latest - pd.DateOffset(years=1):].max()

    # Set aside names whose history has an implausible one-day move in
    # the window before ranking, so a data error can neither be flagged
    # nor drag its group's median.
    window = prices.loc[prices.loc[:start].index[-1]:]
    jumps = window.pct_change().abs().max()
    suspect = jumps[jumps > cfg.MAX_DAILY_MOVE]

    out = relative_to_group(returns.drop(index=suspect.index, errors="ignore"), groups)
    out["fallen"] = is_fallen(out)
    lagging = int(((out["value"] >= 0) & (out["rank"] <= cfg.FALL_QUANTILE)
                   & (out["shortfall"] <= -cfg.MIN_SHORTFALL)).sum())
    out["from_high"] = now.reindex(out.index) / high.reindex(out.index) - 1
    out = out.rename(columns={"value": "return"}).reset_index(names="symbol")
    out = out.merge(names[["symbol", "name", "group_label"]], on="symbol", how="left")

    # Balance-sheet strength and valuation come from the two-stage screen.
    staged = candidates.screen(conn)[["symbol", "per", "equity_ratio", "current_ratio",
                                      "is_financial", "price_to_value", "stage_one", "reason"]]
    out = out.merge(staged.rename(columns={"reason": "screen_reason"}), on="symbol", how="left")

    quarterly = db.read_table(conn, "quarterly_fundamentals")
    verdicts = []
    for row in out.itertuples(index=False):
        if not row.fallen:
            verdicts.append((None, None, None, np.nan, np.nan))
            continue
        own = quarterly[quarterly["symbol"] == row.symbol].copy()
        own["fiscal_date"] = pd.to_datetime(own["fiscal_date"])
        series = {item: own[own["item"] == item].set_index("fiscal_date")["value"]
                  for item in QUARTERLY_ITEMS}
        revenue, revenue_prior, quarter = year_on_year(series["Total Revenue"])
        profit, profit_prior, _ = year_on_year(series["Net Income"])
        status, why = business_verdict(revenue, revenue_prior, profit, profit_prior)
        verdicts.append((
            status, why, quarter,
            np.nan if not revenue_prior or revenue is None else revenue / revenue_prior - 1,
            np.nan if profit is None or not profit_prior or profit_prior <= 0
            else profit / profit_prior - 1,
        ))
    out[["verdict", "verdict_reason", "quarter", "revenue_yoy", "profit_yoy"]] = verdicts

    def can_wait(row) -> bool | None:
        if not row["fallen"]:
            return None
        if row["is_financial"] is True:
            return None  # the ratios do not apply to a bank
        if pd.isna(row["equity_ratio"]) or pd.isna(row["current_ratio"]):
            return None
        return bool(row["equity_ratio"] >= cfg_value.SCREEN_MIN_EQUITY_RATIO
                    and row["current_ratio"] >= cfg_value.SCREEN_MIN_CURRENT_RATIO)

    out["can_wait"] = out.apply(can_wait, axis=1)
    out.attrs["suspect"] = {s: float(v) for s, v in suspect.items()}
    out.attrs["lagging"] = lagging
    out.attrs["as_of"] = str(latest.date())
    out.attrs["since"] = str(prices.loc[:start].index[-1].date())
    return out


def history(conn: sqlite3.Connection) -> pd.DataFrame:
    """reversal_test() on the stored price history."""
    names = candidates._universe(conn)
    prices = _prices(conn)
    if names.empty or prices.empty:
        return pd.DataFrame()
    monthly = prices.resample("ME").last()
    # The last month-end label can be in the future; it is the latest
    # price, not a month-end, so it is not used as a formation date.
    monthly = monthly[monthly.index <= prices.index[-1]]
    return reversal_test(monthly, names.set_index("symbol")["group"])


# --------------------------------------------------------------------------
# Report.
# --------------------------------------------------------------------------

def _pct(x, width: int = 8, signed: bool = True) -> str:
    if x is None or pd.isna(x):
        return "--".rjust(width)
    return f"{x * 100:{'+' if signed else ''}.0f}%".rjust(width)


def main() -> None:
    conn = db.connect()
    try:
        result = screen(conn)
        if result.empty:
            print("No universe prices. Run src/screener/fetch_universe.py first.")
            return
        fallen = result[result["fallen"]]

        print("Fetching latest quarterly results for the flagged names...")
        quarterly = fetch_quarterly(sorted(fallen["symbol"]))
        if not quarterly.empty:
            db.upsert_df(conn, "quarterly_fundamentals", quarterly)
        result = screen(conn)
        fallen = result[result["fallen"]].sort_values("shortfall")

        print("\n" + "=" * 100)
        print(f"STOCKS THAT FELL FURTHER THAN THEIR PEERS -- {result.attrs['since']} to "
              f"{result.attrs['as_of']}")
        print("=" * 100)
        print(f"Flagged: price down over {cfg.LOOKBACK_MONTHS} months, in the bottom "
              f"{cfg.FALL_QUANTILE * 100:.0f}% of its own peer group, AND at least "
              f"{cfg.MIN_SHORTFALL * 100:.0f} points behind the group median.")
        print(f"{len(fallen)} of {len(result)} names flagged. Returns are local-currency, "
              f"dividends included.")
        print(f"  {result.attrs['lagging']} more trail their peers by as much but are up over the "
              f"window: lagging, not fallen.")
        for symbol, move in result.attrs["suspect"].items():
            print(f"  set aside: {symbol} has a one-day move of {move * 100:.0f}% in the window "
                  f"-- unadjusted split or data error.")
        print()

        print("-" * 100)
        print("HAS BUYING THE WORST FALLERS WORKED? Five years of month-ends, this universe")
        print("-" * 100)
        test = history(conn)
        if test.empty:
            print("  Not enough price history.")
        else:
            print(f"  {'fell over':<11}{'then held':<11}{'months':>7}{'median faller':>15}"
                  f"{'median other':>14}{'mean gap':>10}{'months ahead':>14}{'fallers that':>14}")
            print(f"  {'':<11}{'':<11}{'':>7}{'vs peers':>15}{'vs peers':>14}{'':>10}{'':>14}"
                  f"{'beat peers':>14}")
            for r in test.itertuples(index=False):
                chosen = " <" if r.lookback == cfg.LOOKBACK_MONTHS else ""
                print(f"  {str(r.lookback) + ' mo':<11}{str(r.holding) + ' mo':<11}{r.months:>7}"
                      f"{r.faller_median * 100:>+14.1f}%{r.rest_median * 100:>+13.1f}%"
                      f"{r.spread_mean * 100:>+9.1f}%{r.months_ahead * 100:>13.0f}%"
                      f"{r.beat_peers * 100:>13.0f}%{chosen}")
            print("\n  'median faller' is the typical outcome; 'mean gap' is the average, which a few")
            print("  large rebounds can carry. Where the median is near zero and about half of")
            print("  fallers beat their peers, the strategy is a coin flip per stock with a")
            print("  lottery-ticket tail. Companies that fell until they were delisted are not")
            print("  in this universe, so every figure here flatters the strategy.")

        def show(rows: pd.DataFrame) -> None:
            print(f"  {'symbol':<9}{'name':<24}{'group':<17}{'return':>7}{'peers':>7}{'gap':>6}"
                  f"{'off high':>9}{'rev':>6}{'profit':>7}{'PER':>6}{'eq.':>6}{'cur.':>6}"
                  f"{'P/V':>7}  wait?")
            for r in rows.itertuples(index=False):
                wait = {True: "yes", False: "NO", None: "n/a"}[r.can_wait]
                per = "--" if pd.isna(r.per) else f"{r.per:.0f}"
                pv = "--" if pd.isna(r.price_to_value) else f"{r.price_to_value:.2f}x"
                current = "--" if pd.isna(r.current_ratio) else f"{r.current_ratio:.1f}"
                print(f"  {r.symbol:<9}{str(r.name)[:22]:<24}{str(r.group_label)[:15]:<17}"
                      f"{_pct(r._1, 7)}{_pct(r.group_median, 7)}{_pct(r.shortfall, 6)}"
                      f"{_pct(r.from_high, 9)}{_pct(r.revenue_yoy, 6)}{_pct(r.profit_yoy, 7)}"
                      f"{per:>6}{_pct(r.equity_ratio, 6, signed=False)}{current:>6}{pv:>7}  {wait}")

        headings = [
            ("unexplained", "FELL, AND REPORTED RESULTS DO NOT EXPLAIN IT -- read the news on these"),
            ("explained", "FELL, AND REPORTED RESULTS GIVE A REASON"),
            ("unknown", "FELL, NO YEAR-ON-YEAR QUARTER TO JUDGE BY"),
        ]
        for status, heading in headings:
            rows = fallen[fallen["verdict"] == status]
            print("\n" + "-" * 100)
            print(heading)
            print("-" * 100)
            if rows.empty:
                print("  none")
                continue
            show(rows)
            if status == "explained":
                for r in rows.itertuples(index=False):
                    print(f"     {r.symbol}: {r.verdict_reason} (quarter to {r.quarter})")
        print("\n  return / peers / gap: the name, its peer-group median, and the difference, over")
        print("  the window. rev / profit: latest quarter against the same quarter a year earlier.")
        print("  eq. / cur.: equity ratio and current ratio. P/V: price over intrinsic value where")
        print("  the model applies. wait?: balance sheet passes the stage-one thresholds.")
        print("\n  'Unexplained' means the obvious reason is absent from the last reported")
        print("  quarter. It does not mean there is no reason: guidance, litigation, a lost")
        print("  customer or a change in the industry all move prices before they reach an")
        print("  income statement. Nothing here places an order.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
