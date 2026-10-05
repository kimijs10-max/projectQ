"""
Phase 4d: does the composite carry any information?

The build order asked for a backtest of value-only against value+quality
against value+quality+momentum. Built literally, that is not possible with
this data: yfinance supplies four or five annual statements, so after the
reporting lag there are three or four annual rebalances. Three
observations is an anecdote, and a weak backtest in a repository is worse
than none -- a reader who notices the sample size discounts everything
near it.

So the question is asked across companies instead of across time. Score
every name in the screening universe as of a date one year back, using
only fundamentals that were available then, and ask whether the ranking
predicted the following year's returns *within each sector*. That trades
time-series depth, which this data does not have, for cross-sectional
breadth, which it does: roughly 130 names rather than three rebalances.

Returns are measured relative to the peer group's own median, for the
reason Phase 4c made obvious -- sector moves dominate single-name moves
over a year, and 8306.T rose 63% while its sector rose 87%. A raw forward
return would mostly measure which industry was in favour.

**What this cannot establish.** One window, so one draw: a rank
correlation here says the signal worked over this particular year, not
that it works. The names are also all survivors, since the universe was
assembled from tickers that exist today -- and that is not hypothetical,
two candidates turned out to be ETFs occupying the tickers of acquired
shipping companies. Finally, the p-values use a normal approximation and
assume independent observations, which cross-sectional returns in a
single window are not, so they are optimistic by an unknown margin.
Treated as a sanity check on whether the composite is informative at all,
the test is worth running. Treated as evidence that it generalises, it
would be misleading.

Run directly:
    python src/screener/validate.py
"""

from __future__ import annotations

import math
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from screener import composite, quality, universe  # noqa: E402
from storage import db  # noqa: E402

# Two non-overlapping annual windows. Two is not many, but the test's
# main weakness is being a single draw, and a signal that reverses sign
# between two adjacent years is telling you something a single window
# cannot. Both are reported separately and never pooled -- pooling
# overlapping or adjacent cross-sections would inflate n without adding
# independent information.
KNOWLEDGE_DATES = ["2024-09-30", "2025-09-30"]
HORIZON_MONTHS = 12

SIGNALS = ["book_to_market", "gross_profitability", "f_ratio", "momentum_12_1"]

# A rank correlation smaller than this is treated as carrying no
# direction at all, rather than as a weak positive or negative.
DEAD_BAND = 0.05


def _two_sided_p(t: float) -> float:
    """Normal-approximation two-sided p-value; see the module caveats."""
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))


def spearman(x: pd.Series, y: pd.Series) -> tuple[float, float, int]:
    """
    Rank correlation, its p-value and the pair count.

    Spearman rather than Pearson because the signals are percentiles and
    the returns are heavy-tailed; a single quadrupling would otherwise
    drive the whole coefficient.
    """
    joined = pd.concat([x.rename("x"), y.rename("y")], axis=1).dropna()
    n = len(joined)
    if n < 10:
        return float("nan"), float("nan"), n
    rx = joined["x"].rank()
    ry = joined["y"].rank()
    rho = float(np.corrcoef(rx, ry)[0, 1])
    if abs(rho) >= 1.0:
        return rho, 0.0, n
    t = rho * math.sqrt((n - 2) / (1.0 - rho ** 2))
    return rho, _two_sided_p(t), n


def forward_returns(
    conn: sqlite3.Connection,
    start: str,
    months: int = HORIZON_MONTHS,
) -> pd.Series:
    """Local-currency total price return over the forward window."""
    prices = quality._price_series(conn, "screen_prices")
    begin = pd.Timestamp(start)
    end = begin + pd.DateOffset(months=months)
    out = {}
    for symbol, series in prices.items():
        p0 = quality._price_asof(series, begin)
        p1 = quality._price_asof(series, end)
        if p0 and p1 and p0 > 0:
            out[symbol] = p1 / p0 - 1.0
    return pd.Series(out, name="forward_return")


def panel(
    conn: sqlite3.Connection,
    knowledge_date: str,
    months: int = HORIZON_MONTHS,
) -> pd.DataFrame:
    """
    One row per (symbol, peer group): its signal percentiles as of
    `knowledge_date`, and its forward return relative to the group median.
    """
    metrics = composite.universe_metrics(conn, knowledge_date)
    if metrics.empty:
        return pd.DataFrame()
    groups, _ = universe.verified_peers(conn)
    indexed = metrics.set_index("symbol")
    fwd = forward_returns(conn, knowledge_date, months)

    rows = []
    for holding, peers in groups.items():
        members = [s for s in [holding, *peers] if s in indexed.index]
        if len(members) < 5:
            continue
        frame = indexed.loc[members]
        group_fwd = fwd.reindex(members).dropna()
        if len(group_fwd) < 5:
            continue
        # Relative to the group median rather than its mean: a single
        # name that tripled would otherwise drag the benchmark it is
        # being measured against.
        median = float(group_fwd.median())

        ranks = {s: frame[s].rank(pct=True) for s in SIGNALS}
        for symbol in members:
            row = {
                "group": holding,
                "symbol": symbol,
                "is_holding": symbol == holding,
                "forward_return": fwd.get(symbol, np.nan),
                "group_median_forward": median,
                "forward_excess": fwd.get(symbol, np.nan) - median,
            }
            sleeve_values: dict[str, float | None] = {}
            for sleeve, signals in composite.SLEEVES.items():
                parts = []
                for signal in signals:
                    value = ranks[signal].get(symbol, np.nan)
                    row[f"{signal}_pct"] = None if pd.isna(value) else float(value)
                    if pd.notna(value):
                        parts.append(float(value))
                sleeve_values[sleeve] = sum(parts) / len(parts) if parts else None
            for name, needed in composite.COMPOSITES.items():
                row[name] = (None if any(sleeve_values[s] is None for s in needed)
                             else sum(sleeve_values[s] for s in needed) / len(needed))
            rows.append(row)

    return pd.DataFrame(rows)


def _tercile_spread(df: pd.DataFrame, column: str) -> tuple[float, float, int, int]:
    """Mean forward excess of the top and bottom third by `column`."""
    valid = df[[column, "forward_excess"]].dropna()
    if len(valid) < 15:
        return float("nan"), float("nan"), 0, 0
    lo, hi = valid[column].quantile([1 / 3, 2 / 3])
    bottom = valid[valid[column] <= lo]["forward_excess"]
    top = valid[valid[column] >= hi]["forward_excess"]
    return float(top.mean()), float(bottom.mean()), len(top), len(bottom)


def report_window(conn: sqlite3.Connection, kd: str) -> pd.DataFrame | None:
    """Print one window's results and return its panel."""
    df = panel(conn, kd)
    if df.empty:
        print(f"\n{kd}: no panel.")
        return None

    end = (pd.Timestamp(kd) + pd.DateOffset(months=HORIZON_MONTHS)).date()
    print("\n" + "=" * 78)
    print(f"WINDOW  {kd}  ->  {end}")
    print("=" * 78)
    print(f"{len(df)} name-group observations across {df['group'].nunique()} "
          f"groups. Returns are relative to each")
    print("peer group's own median, so the sector move is removed:")
    for group, g in df.groupby("group"):
        print(f"  {group:<9} peer-group median "
              f"{g['group_median_forward'].iloc[0] * 100:+7.1f}%")

    print(f"\n{'signal':<26}{'rho':>8}{'p':>8}{'n':>6}"
          f"{'top 3rd':>10}{'bot 3rd':>10}{'spread':>9}")
    print("-" * 78)
    tested = [f"{s}_pct" for s in SIGNALS] + list(composite.COMPOSITES)
    for column in tested:
        rho, p, n = spearman(df[column], df["forward_excess"])
        top, bottom, _, _ = _tercile_spread(df, column)
        if n < 10:
            print(f"{column:<26}{'--':>8}{'--':>8}{n:>6}   insufficient data")
            continue
        star = " *" if p < 0.05 else ""
        print(f"{column:<26}{rho:>8.3f}{p:>8.3f}{n:>6}"
              f"{top * 100:>9.1f}%{bottom * 100:>9.1f}%"
              f"{(top - bottom) * 100:>8.1f}%{star}")
    return df


def main() -> None:
    conn = db.connect()
    try:
        print("=" * 78)
        print("CROSS-SECTIONAL VALIDATION")
        print("=" * 78)
        print("Each name in the screening universe is scored using only")
        print("fundamentals available at the knowledge date, then measured over")
        print("the following year against its own peer group's median return.")
        print("This asks the build order's question across companies rather than")
        print("across time, because four annual statements cannot support a")
        print("time-series backtest -- see the module docstring.")

        panels = {}
        for kd in KNOWLEDGE_DATES:
            df = report_window(conn, kd)
            if df is not None:
                panels[kd] = df

        print("\n  rho is Spearman against forward excess return; terciles are")
        print("  mean forward excess of the top and bottom third by signal.")
        print("  * marks p < 0.05 on a normal approximation assuming")
        print("  independent observations, which these are not.")

        if len(panels) >= 2:
            print("\n" + "=" * 78)
            print("DOES ANY SIGNAL HOLD ITS SIGN ACROSS BOTH WINDOWS?")
            print("=" * 78)
            tested = [f"{s}_pct" for s in SIGNALS] + list(composite.COMPOSITES)
            print(f"{'signal':<26}" + "".join(f"{kd[:7]:>12}" for kd in panels)
                  + f"{'verdict':>16}")
            print("-" * 78)
            for column in tested:
                rhos = []
                for kd, df in panels.items():
                    rho, _, n = spearman(df[column], df["forward_excess"])
                    rhos.append(rho if n >= 10 else float("nan"))
                cells = "".join(
                    f"{'--':>12}" if pd.isna(r) else f"{r:>12.3f}" for r in rhos)
                # The dead band marks a coefficient too small to carry a
                # direction. It must not be confused with a sign flip:
                # +0.045 and +0.097 agree in sign, one is just negligible,
                # and calling that "flips" would overstate the instability.
                if any(pd.isna(r) for r in rhos):
                    verdict = "untested"
                elif any(abs(r) < DEAD_BAND for r in rhos):
                    verdict = "one negligible"
                elif all(r > 0 for r in rhos):
                    verdict = "positive both"
                elif all(r < 0 for r in rhos):
                    verdict = "negative both"
                else:
                    verdict = "SIGN FLIPS"
                print(f"{column:<26}{cells}{verdict:>16}")
            print(f"\n  A coefficient inside +/-{DEAD_BAND} carries no direction and is")
            print("  reported as negligible rather than as a weak sign. Two")
            print("  windows cannot establish persistence; what they can do is")
            print("  rule out reading a single window as a result when the sign")
            print("  does not survive the adjacent year.")

        print("\n" + "=" * 78)
        print("WHAT THIS DOES NOT SHOW")
        print("=" * 78)
        print("  Two adjacent windows, so two draws from one regime. The")
        print("  Japanese bank and shipping rallies dominate both.")
        print()
        print("  Every name is a survivor: the universe was built from tickers")
        print("  live today. Not hypothetical -- two candidates turned out to")
        print("  be ETFs sitting on the tickers of acquired shipping companies.")
        print()
        print("  Returns within a cross-section are correlated, so p-values")
        print("  overstate significance by an unknown margin even after the")
        print("  sector median is removed. Read them as ordering the signals,")
        print("  not as hypothesis tests.")

        latest = panels.get(KNOWLEDGE_DATES[-1])
        if latest is not None:
            holdings = latest[latest["is_holding"]]
            if not holdings.empty:
                print("\n" + "=" * 78)
                print(f"THE HOLDINGS, {KNOWLEDGE_DATES[-1]} window")
                print("=" * 78)
                print(f"{'symbol':<9}{'composite':>11}{'fwd':>9}{'vs sector':>11}")
                print("-" * 78)
                for _, r in holdings.sort_values("symbol").iterrows():
                    comp = r["value+quality+momentum"]
                    comp_s = ("  --" if comp is None or pd.isna(comp)
                              else f"{comp * 100:4.0f}")
                    print(f"{r['symbol']:<9}{comp_s:>11}"
                          f"{r['forward_return'] * 100:>8.1f}%"
                          f"{r['forward_excess'] * 100:>10.1f}%")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
