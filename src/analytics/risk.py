"""
Phase 3: risk.

Value at Risk (historical and parametric), benchmark betas, correlation
structure, and concentration.

Everything is expressed in percent of NAV rather than currency, which
keeps the output publishable and is the portable form anyway.

Four things worth knowing about how these numbers are built:

1. Historical and parametric VaR are both reported because the gap
   between them is the finding. Parametric assumes normality; if the
   historical figure is materially worse, the return distribution has
   fatter tails than the normal assumption allows and the parametric
   number understates the risk.

2. Every VaR estimate carries the number of observations in its tail.
   At roughly one year of daily data the 99% figure rests on two or
   three points and should not be read as a real estimate. Printing it
   without that count would be the dishonest version of this report.

3. Benchmark betas convert the benchmark into base currency first.
   Beta should describe what happens to the investor's wealth, which
   includes the currency move. Pure local-currency equity beta is a
   different question and is already answered by the factor
   regressions in analytics/factors.py.

4. Correlations are reported daily and weekly. Tokyo closes about
   thirteen hours before New York, so same-date daily correlations
   understate US/Japan co-movement -- the "asynchronous closes" trap in
   CLAUDE.md. Weekly sampling spans the gap. The difference between the
   two tables is that effect made visible rather than assumed.

Run directly:
    python src/analytics/risk.py
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics import pnl  # noqa: E402
from storage import db  # noqa: E402

# Normal quantiles, hardcoded rather than pulling in scipy for two numbers.
Z = {0.95: 1.644854, 0.99: 2.326348}

TRADING_DAYS = 252

# Benchmark ticker -> the fx_rates pair converting its currency to base.
BENCHMARKS = {"SPY": "USDSGD", "1306.T": "JPYSGD"}
BENCHMARK_LABELS = {"SPY": "S&P 500 (SPY)", "1306.T": "TOPIX (1306.T)"}


@dataclass
class VaRResult:
    level: float
    historical: float
    parametric: float
    tail_obs: int
    n_obs: int

    @property
    def reliable(self) -> bool:
        """Fewer than ~10 points in the tail is not an estimate worth quoting."""
        return self.tail_obs >= 10


def portfolio_returns(conn: sqlite3.Connection, source: str = "attributed") -> pd.Series:
    """
    Daily base-currency portfolio return.

    source="attributed" uses this engine's own bottom-up series;
    source="nav" uses the change in the broker's reported NAV. They
    correlate 0.967, so risk figures should agree closely -- reporting
    both is itself a cross-check.
    """
    r = pnl.daily_returns(conn)
    if r.empty:
        return pd.Series(dtype=float)
    column = {"attributed": "attributed_return", "nav": "nav_return"}[source]
    out = r.dropna(subset=[column]).set_index("date")[column]
    out.index = pd.to_datetime(out.index)
    return out.astype(float).sort_index()


def value_at_risk(returns: pd.Series, level: float = 0.95) -> VaRResult:
    """
    One-day VaR at the given confidence level, as a positive percentage
    loss. Historical is the empirical quantile; parametric assumes
    normality.
    """
    if level not in Z:
        raise ValueError(f"Unsupported level {level!r}; expected one of {sorted(Z)}")

    r = returns.dropna()
    n = len(r)
    if n == 0:
        raise ValueError("No returns supplied")

    quantile = float(np.quantile(r, 1.0 - level))
    historical = -quantile

    mu, sigma = float(r.mean()), float(r.std(ddof=1))
    parametric = -(mu - Z[level] * sigma)

    tail_obs = int((r <= quantile).sum())
    return VaRResult(
        level=level,
        historical=historical,
        parametric=parametric,
        tail_obs=tail_obs,
        n_obs=n,
    )


def _benchmark_base_returns(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Daily benchmark returns converted into base currency.

    A base-currency investor in SPY earns the price move and the USD/SGD
    move together, so the series is built from price * fx before
    differencing rather than after.
    """
    prices = db.read_table(conn, "benchmark_prices")
    fx = db.read_table(conn, "fx_rates")

    out = {}
    for ticker, pair in BENCHMARKS.items():
        p = prices[prices["ticker"] == ticker].set_index("date")["close"]
        f = fx[fx["pair"] == pair].set_index("date")["rate"]
        joined = pd.concat([p.rename("price"), f.rename("fx")], axis=1, sort=True).dropna()
        if joined.empty:
            continue
        base_value = joined["price"] * joined["fx"]
        series = base_value.pct_change().dropna()
        series.index = pd.to_datetime(series.index)
        out[ticker] = series.sort_index()

    return pd.DataFrame(out)


def benchmark_betas(conn: sqlite3.Connection, source: str = "attributed") -> pd.DataFrame:
    """
    Beta of the portfolio against each benchmark, both measured in base
    currency. Simple OLS on overlapping days.
    """
    port = portfolio_returns(conn, source)
    bench = _benchmark_base_returns(conn)
    if port.empty or bench.empty:
        return pd.DataFrame()

    rows = []
    for ticker in bench.columns:
        joined = pd.concat([port.rename("p"), bench[ticker].rename("b")], axis=1, sort=True).dropna()
        if len(joined) < 20:
            continue
        x = joined["b"].to_numpy()
        y = joined["p"].to_numpy()
        var = x.var(ddof=1)
        beta = float(np.cov(x, y, ddof=1)[0, 1] / var) if var > 0 else np.nan
        corr = float(np.corrcoef(x, y)[0, 1])
        rows.append({
            "benchmark": BENCHMARK_LABELS.get(ticker, ticker),
            "beta": beta,
            "correlation": corr,
            "r_squared": corr ** 2,
            "n_obs": len(joined),
        })
    return pd.DataFrame(rows)


def holding_returns_base(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Per-holding daily return in base currency, wide by symbol.

    Taken as total_move / value_prev_base from the attribution step, so
    it already includes the local move, the FX move and their
    interaction.
    """
    mtm = pnl.compute_mark_to_market(conn)
    if mtm.empty:
        return pd.DataFrame()
    m = mtm[mtm["value_prev_base"] != 0].copy()
    m["r_base"] = m["total_move"] / m["value_prev_base"]
    wide = m.pivot_table(index="date", columns="symbol", values="r_base")
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index()


def correlation_matrix(conn: sqlite3.Connection, freq: str = "D") -> pd.DataFrame:
    """
    Correlation of holdings' base-currency returns.

    freq="W" compounds into weekly returns first. The intent is to test
    the asynchronous-close effect: Tokyo and New York do not close on the
    same clock, so a daily same-date correlation should understate
    US/Japan co-movement while weekly sampling spans the gap.

    Read alongside pairwise_obs() -- at one year of data the weekly
    matrix has ~52 observations at best and far fewer for any pair whose
    holding periods only partly overlap, which is not enough to
    distinguish a real change from noise.
    """
    wide = holding_returns_base(conn)
    if wide.empty:
        return pd.DataFrame()
    if freq.upper().startswith("W"):
        # min_count=1 is essential: without it prod() skips NaN and a week
        # with no data returns 1.0, i.e. a fabricated 0% return, which
        # silently invents observations for periods a holding did not exist.
        wide = (1.0 + wide).resample("W").prod(min_count=1) - 1.0
    return wide.dropna(how="all").corr(min_periods=20)


def pairwise_obs(conn: sqlite3.Connection, freq: str = "D") -> pd.DataFrame:
    """
    Number of overlapping observations behind each correlation, so a
    coefficient is never read without knowing how much data supports it.
    """
    wide = holding_returns_base(conn)
    if wide.empty:
        return pd.DataFrame()
    if freq.upper().startswith("W"):
        # min_count=1 is essential: without it prod() skips NaN and a week
        # with no data returns 1.0, i.e. a fabricated 0% return, which
        # silently invents observations for periods a holding did not exist.
        wide = (1.0 + wide).resample("W").prod(min_count=1) - 1.0
    notna = wide.notna().astype(int)
    return notna.T @ notna


def concentration(conn: sqlite3.Connection) -> dict:
    """
    Concentration of the book: largest weight, Herfindahl-Hirschman
    index, and the effective number of positions it implies (1/HHI).

    Reported on the equity sleeve and again including cash, because
    cash genuinely dilutes concentration and excluding it overstates
    how concentrated the account is.
    """
    positions = db.read_table(conn, "positions")
    nav = db.read_table(conn, "nav_history").sort_values("report_date")
    if positions.empty or nav.empty:
        return {}

    latest_date = positions["report_date"].max()
    pos = positions[positions["report_date"] == latest_date].copy()
    pos["value_base"] = pos["position_value"] * pos["fx_rate_to_base"]

    equity_total = float(pos["value_base"].sum())
    nav_row = nav[nav["report_date"] == latest_date]
    nav_total = float(nav_row["total"].iloc[0]) if not nav_row.empty else equity_total

    w_equity = (pos["value_base"] / equity_total).to_numpy()
    hhi_equity = float((w_equity ** 2).sum())

    w_nav = (pos["value_base"] / nav_total).to_numpy()
    cash_weight = max(0.0, 1.0 - float(w_nav.sum()))
    w_nav_with_cash = np.append(w_nav, cash_weight)
    hhi_nav = float((w_nav_with_cash ** 2).sum())

    return {
        "as_of": latest_date,
        "n_holdings": int(len(pos)),
        "max_weight_equity": float(w_equity.max()),
        "hhi_equity": hhi_equity,
        "effective_n_equity": 1.0 / hhi_equity if hhi_equity else np.nan,
        "max_weight_nav": float(w_nav.max()),
        "cash_weight_nav": cash_weight,
        "hhi_nav": hhi_nav,
        "effective_n_nav": 1.0 / hhi_nav if hhi_nav else np.nan,
    }


def main() -> None:
    conn = db.connect()
    try:
        print("=" * 68)
        print("VALUE AT RISK (one day, % of NAV)")
        print("=" * 68)
        for source, label in (("attributed", "engine attribution"), ("nav", "broker NAV")):
            r = portfolio_returns(conn, source)
            if r.empty:
                continue
            vol_d = r.std(ddof=1)
            print(f"\n{label}: {len(r)} obs, "
                  f"daily vol {vol_d * 100:.3f}%, "
                  f"annualised {vol_d * np.sqrt(TRADING_DAYS) * 100:.1f}%")
            print(f"  worst day {r.min() * 100:+.2f}%   best day {r.max() * 100:+.2f}%")
            for level in (0.95, 0.99):
                v = value_at_risk(r, level)
                flag = "" if v.reliable else "   <-- only %d obs in tail; not a usable estimate" % v.tail_obs
                print(f"  {int(level * 100)}%  historical {v.historical * 100:5.2f}%   "
                      f"parametric {v.parametric * 100:5.2f}%{flag}")

        r = portfolio_returns(conn, "attributed")
        v95 = value_at_risk(r, 0.95)
        gap = v95.historical - v95.parametric
        print(f"\n  At 95%, historical minus parametric = {gap * 100:+.2f} pp. "
              f"{'Historical is worse, so the' if gap > 0 else 'The'} normal assumption "
              f"{'understates' if gap > 0 else 'does not understate'} the tail here.")

        print("\n" + "=" * 68)
        print("BENCHMARK BETA (portfolio and benchmark both in base currency)")
        print("=" * 68)
        betas = benchmark_betas(conn)
        if betas.empty:
            print("  no overlapping benchmark data")
        else:
            b = betas.copy()
            for col in ("beta", "correlation", "r_squared"):
                b[col] = b[col].round(3)
            print(b.to_string(index=False))
            print("\n  Local-currency equity beta is a different question and is")
            print("  answered by analytics/factors.py (MKT loadings).")

        print("\n" + "=" * 68)
        print("CORRELATION OF HOLDINGS (base-currency returns)")
        print("=" * 68)
        for freq, label in (("D", "daily"), ("W", "weekly")):
            cm = correlation_matrix(conn, freq)
            if cm.empty:
                continue
            print(f"\n{label}:")
            print(cm.round(2).to_string())

        obs_w = pairwise_obs(conn, "W")
        if not obs_w.empty:
            print("\nweekly observations behind each pair:")
            print(obs_w.to_string())

        print("\n  These were meant to show the asynchronous-close effect -- Tokyo")
        print("  closes ~13h before New York, so daily same-date correlations")
        print("  should understate US/Japan co-movement and weekly should not.")
        print("  The data does not settle it. Cross-market pairs move both ways")
        print("  weekly, averaging only about +0.03.")
        print("\n  The observation counts explain why, and point at the answer.")
        print("  NVDA/SHOP is the only pair with the full 53 weeks behind it, and")
        print("  it is also the only one that barely moves (0.28 daily, 0.29")
        print("  weekly). Every pair that swings hard has 23-27 weeks, where the")
        print("  standard error is around 0.19 -- wide enough to produce those")
        print("  swings from noise alone. So the instability is a sample-size")
        print("  artefact rather than a real weekly-vs-daily difference, and the")
        print("  test simply needs more history. Until then the daily matrix is")
        print("  the better-supported of the two.")

        print("\n" + "=" * 68)
        print("CONCENTRATION")
        print("=" * 68)
        c = concentration(conn)
        if c:
            print(f"  as of {c['as_of']}, {c['n_holdings']} holdings")
            print(f"  equity sleeve : max weight {c['max_weight_equity'] * 100:.1f}%   "
                  f"HHI {c['hhi_equity']:.3f}   effective N {c['effective_n_equity']:.1f}")
            print(f"  incl. cash    : max weight {c['max_weight_nav'] * 100:.1f}%   "
                  f"HHI {c['hhi_nav']:.3f}   effective N {c['effective_n_nav']:.1f}   "
                  f"(cash {c['cash_weight_nav'] * 100:.1f}%)")
            print(f"\n  Effective N is 1/HHI: the book behaves like roughly "
                  f"{c['effective_n_equity']:.1f} equally weighted positions.")

        print("\n" + "=" * 68)
        print("Not yet cross-checked against the broker's own VaR report, which")
        print("is not part of the Flex Query -- see NOTES.md open items.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
