"""
Phase 5: stress tests.

VaR (Phase 3) describes an ordinary bad day, estimated from the last year
of returns. A stress test asks a different question: what would *today's*
portfolio lose in a named event -- including events worse than anything
in that year. Everything is in percent of NAV, in base currency.

**Historical replay.** Today's weights are carried through a past episode
using what each holding and each currency actually did. The loss reported
is the worst peak-to-trough fall inside the scenario's window, on the
portfolio's own dates rather than an index's, and it is split the same
way Phase 2 splits P&L:

    R_base = (1 + r_local)(1 + r_fx) - 1 = r_local + r_fx + r_local * r_fx

so each episode says how much was the stocks and how much was the
currency. Cash is an exposure too: yen cash has no price risk and full
currency risk.

Prices are forward-filled across market holidays, never skipped (see
Known traps in CLAUDE.md). A holding that did not trade during an episode
is not treated as flat and not dropped: it is proxied by its beta to its
local index, and the share of the book that was proxied is printed next
to the result.

**Hypothetical shocks.** A stated move in one factor, reported twice:

- *direct only* -- the factor moves and nothing else does. For a currency
  shock this is pure translation.
- *calm beta* -- each holding's price also moves by its sensitivity to
  that factor, measured on the last two years of weekly returns.
- *episode beta* -- the same, but with the sensitivity taken from what
  each holding actually did, per unit of factor move, in the matching
  historical episode.

The first two are separated because for this book they disagree. A stronger yen
raises the base-currency value of every Tokyo holding, so the direct
effect of "yen +10%" is a gain. But the yen does not rally in isolation;
in August 2024 it rallied because carry trades were being unwound, and
Japanese equities fell hard on the same days. Reporting only the
translation gain would describe a hedge the book does not have.

The third column exists because a regression over ordinary weeks cannot
see that. Most weeks the yen and these stocks are barely related, so the
calm betas are small and their R-squared is near zero; the relationship
only appears when the move is large and forced. An episode beta is one
observation, not an estimate, and is labelled as such -- but it is the
only one of the three that has ever been the right order of magnitude.

FX is not given a response of its own in the hypothetical shocks -- the
currencies move only as the scenario states. How currencies actually
behaved in a selloff is what the historical replays are for.

This engine does not reprice anything: there are no options in the book,
so every exposure is linear and a return applied to a weight is exact.

Requires the long history in scenario_prices (python src/data/market_data.py).

Run directly:
    python src/analytics/stress.py
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config"))

import scenarios as cfg  # noqa: E402
from analytics import risk  # noqa: E402
from storage import db  # noqa: E402

BASE_CURRENCY = "SGD"
WEEKLY = "W-FRI"
MIN_BETA_OBS = 52
# How far before a window's start a series' last price may be and still
# count as trading at the start (covers a long holiday, not a listing gap).
MAX_STALE_DAYS = 10


@dataclass
class Exposure:
    """One line of the book: a stock, or cash in one currency."""
    name: str
    weight: float                 # fraction of NAV
    currency: str
    series: str | None = None     # price series; None for cash


@dataclass
class Replay:
    scenario: str
    peak: pd.Timestamp
    trough: pd.Timestamp
    loss: float                   # portfolio return peak to trough
    stock: float                  # ...of which local price moves
    fx: float                     # ...of which currency moves
    cross: float                  # ...of which the interaction term
    by_exposure: pd.DataFrame     # name, weight, r_local, r_fx, contribution
    proxied: dict[str, str] = field(default_factory=dict)  # name -> how
    proxied_weight: float = 0.0


# --------------------------------------------------------------------------
# Maths. Pure functions of weights and price levels -- tested in
# tests/test_stress.py.
# --------------------------------------------------------------------------

def window_levels(levels: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """
    Price levels over [start, end] on one shared calendar.

    The calendar is every date on which anything traded, and each series
    is forward-filled onto it, so a Tokyo holiday carries Friday's price
    instead of dropping the row. The fill starts from before the window
    (up to MAX_STALE_DAYS) so the first row is a real last price; a series
    with nothing that recent stays empty rather than being back-filled.
    """
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    lead = levels.loc[start_ts - pd.Timedelta(days=MAX_STALE_DAYS):end_ts]
    return lead.ffill().loc[start_ts:end_ts]


def max_drawdown(value: pd.Series) -> tuple[pd.Timestamp, pd.Timestamp, float]:
    """Worst peak-to-trough fall of a value path: (peak, trough, return)."""
    drawdown = value / value.cummax() - 1
    trough = drawdown.idxmin()
    peak = value.loc[:trough].idxmax()
    return peak, trough, float(drawdown.loc[trough])


def replay(
    scenario: str,
    exposures: list[Exposure],
    levels: pd.DataFrame,
    start: str,
    end: str,
    proxies: dict[str, tuple[float, str]] | None = None,
) -> Replay:
    """
    Carry `exposures` through [start, end] and find the worst drawdown.

    `levels` holds one column per price series and FX pair. `proxies`
    maps a price series to (beta, benchmark series) for use when the
    series itself has no history in the window.

    The weights are today's at the start of the window and drift with
    prices from there, as an unrebalanced portfolio would. The split into
    stock, FX and cross is computed between the peak and trough dates on
    the weights the portfolio had at the peak, and sums to the loss
    exactly.
    """
    proxies = proxies or {}
    win = window_levels(levels, start, end)
    if win.empty:
        raise ValueError(f"no price data in {start}..{end}")
    ones = pd.Series(1.0, index=win.index)

    local: dict[str, pd.Series] = {}
    fx: dict[str, pd.Series] = {}
    proxied: dict[str, str] = {}
    for e in exposures:
        if e.series is None:
            local[e.name] = ones
        elif e.series in win.columns and pd.notna(win[e.series].iloc[0]):
            local[e.name] = win[e.series] / win[e.series].iloc[0]
        elif e.series in proxies:
            beta, bench = proxies[e.series]
            local[e.name] = 1 + beta * (win[bench] / win[bench].iloc[0] - 1)
            proxied[e.name] = f"{beta:.2f} x {bench}"
        else:
            raise ValueError(f"{e.name}: no history in {start}..{end} and no proxy")

        if e.currency == BASE_CURRENCY:
            fx[e.name] = ones
        else:
            pair = cfg.FX_SERIES[e.currency]
            fx[e.name] = win[pair] / win[pair].iloc[0]

    value = sum(e.weight * local[e.name] * fx[e.name] for e in exposures)
    peak, trough, loss = max_drawdown(value)

    rows = []
    for e in exposures:
        weight_at_peak = e.weight * local[e.name][peak] * fx[e.name][peak] / value[peak]
        r_local = float(local[e.name][trough] / local[e.name][peak] - 1)
        r_fx = float(fx[e.name][trough] / fx[e.name][peak] - 1)
        rows.append({
            "name": e.name,
            "weight": float(weight_at_peak),
            "r_local": r_local,
            "r_fx": r_fx,
            "stock": float(weight_at_peak * r_local),
            "fx": float(weight_at_peak * r_fx),
            "cross": float(weight_at_peak * r_local * r_fx),
        })
    table = pd.DataFrame(rows)
    table["contribution"] = table["stock"] + table["fx"] + table["cross"]

    return Replay(
        scenario=scenario,
        peak=peak,
        trough=trough,
        loss=loss,
        stock=float(table["stock"].sum()),
        fx=float(table["fx"].sum()),
        cross=float(table["cross"].sum()),
        by_exposure=table,
        proxied=proxied,
        proxied_weight=float(sum(e.weight for e in exposures if e.name in proxied)),
    )


def factor_betas(levels: pd.DataFrame, factor: pd.Series, weeks: int = cfg.BETA_WEEKS) -> pd.DataFrame:
    """
    Sensitivity of each column of `levels` to `factor`, on the last
    `weeks` weekly returns. Returns beta, r_squared and n_obs per column;
    a column with too little overlap is left out, not given a zero.
    """
    factor_ret = factor.resample(WEEKLY).last().pct_change()
    rows = {}
    for column in levels.columns:
        own = levels[column].dropna().resample(WEEKLY).last().pct_change()
        joined = pd.concat([own.rename("y"), factor_ret.rename("x")], axis=1, sort=True).dropna().tail(weeks)
        if len(joined) < MIN_BETA_OBS or joined["x"].var() == 0:
            continue
        beta = joined.cov().loc["y", "x"] / joined["x"].var()
        rows[column] = {
            "beta": float(beta),
            "r_squared": float(joined.corr().loc["y", "x"] ** 2),
            "n_obs": len(joined),
        }
    return pd.DataFrame.from_dict(rows, orient="index")


def shock(
    exposures: list[Exposure],
    size: float,
    fx_moves: dict[str, float],
    betas: dict[str, float],
) -> pd.DataFrame:
    """
    Effect of a factor move of `size`, per exposure, as fractions of NAV.

    direct   -- only the stated currency moves (`fx_moves`) apply.
    full     -- each stock's local price also moves by beta * size.
    A stock with no beta gets NaN for `full`, so a missing sensitivity
    cannot pass as "unaffected".
    """
    rows = []
    for e in exposures:
        r_fx = fx_moves.get(e.currency, 0.0)
        if e.series is None:
            r_local = 0.0
        elif e.series in betas:
            r_local = betas[e.series] * size
        else:
            r_local = np.nan
        rows.append({
            "name": e.name,
            "weight": e.weight,
            "r_local": r_local,
            "r_fx": r_fx,
            "direct": e.weight * r_fx,
            "full": e.weight * ((1 + r_local) * (1 + r_fx) - 1),
        })
    return pd.DataFrame(rows)


def episode_sensitivities(episode: Replay, factor_move: float) -> dict[str, float]:
    """
    Each stock's local price move per unit of factor move, as observed
    between an episode's peak and trough. One data point per stock, and
    it credits the factor with everything that happened in the episode.
    Holdings that were proxied in the episode are left out: a beta
    inferred from a beta is not an observation.
    """
    if not factor_move:
        return {}
    table = episode.by_exposure
    return {
        row["name"]: row["r_local"] / factor_move
        for _, row in table.iterrows()
        if row["name"] not in episode.proxied and row["r_local"] != 0.0
    }


# --------------------------------------------------------------------------
# Inputs from the database.
# --------------------------------------------------------------------------

def current_exposures(conn: sqlite3.Connection) -> tuple[list[Exposure], str]:
    """
    Today's book as fractions of NAV: one Exposure per stock, one per
    currency of cash, and a base-currency remainder (accruals, rounding)
    so the weights sum to one. Returns (exposures, snapshot date).
    """
    positions = db.read_table(conn, "positions")
    nav = db.read_table(conn, "nav_history")
    cash = db.read_table(conn, "cash_report")
    fx = db.read_table(conn, "fx_rates")
    if positions.empty or nav.empty:
        return [], ""

    as_of = positions["report_date"].max()
    pos = positions[positions["report_date"] == as_of]
    nav_row = nav[nav["report_date"] == as_of]
    if nav_row.empty:
        return [], as_of
    nav_total = float(nav_row["total"].iloc[0])

    out = [
        Exposure(
            name=row.symbol,
            weight=float(row.position_value * row.fx_rate_to_base / nav_total),
            currency=row.currency,
            series=row.symbol,
        )
        for row in pos.itertuples(index=False)
    ]

    latest_cash = cash[cash["to_date"] == cash["to_date"].max()] if not cash.empty else cash
    for row in latest_cash.itertuples(index=False):
        if row.currency in ("BASE_SUMMARY", BASE_CURRENCY) or not row.ending_cash:
            continue
        pair = cfg.FX_SERIES.get(row.currency)
        rates = fx[(fx["pair"] == pair) & (fx["date"] <= as_of)].sort_values("date")
        if rates.empty:
            continue
        weight = float(row.ending_cash * rates["rate"].iloc[-1] / nav_total)
        out.append(Exposure(name=f"{row.currency} cash", weight=weight, currency=row.currency))

    remainder = 1.0 - sum(e.weight for e in out)
    out.append(Exposure(name=f"{BASE_CURRENCY} cash + accruals", weight=remainder,
                        currency=BASE_CURRENCY))
    return out, as_of


def scenario_levels(conn: sqlite3.Connection) -> pd.DataFrame:
    """scenario_prices as a wide frame, plus the derived JPYUSD factor."""
    df = db.read_table(conn, "scenario_prices").dropna(subset=["close"])
    if df.empty:
        return pd.DataFrame()
    wide = df.pivot(index="date", columns="series", values="close")
    wide.index = pd.to_datetime(wide.index)
    wide = wide.sort_index()
    if {"JPYSGD", "USDSGD"} <= set(wide.columns):
        wide["JPYUSD"] = wide["JPYSGD"] / wide["USDSGD"]
    return wide


def proxy_betas(exposures: list[Exposure], levels: pd.DataFrame) -> dict[str, tuple[float, str]]:
    """Each stock's beta to its local index, for episodes it did not trade in."""
    out: dict[str, tuple[float, str]] = {}
    for currency, bench in cfg.PROXY_BENCHMARK.items():
        names = [e.series for e in exposures if e.series and e.currency == currency
                 and e.series in levels.columns]
        if bench not in levels.columns or not names:
            continue
        betas = factor_betas(levels[names], levels[bench])
        for series, row in betas.iterrows():
            out[series] = (float(row["beta"]), bench)
    return out


# --------------------------------------------------------------------------
# Report.
# --------------------------------------------------------------------------

def _pct(x: float, width: int = 8) -> str:
    if x is None or pd.isna(x):
        return "--".rjust(width)
    return f"{x * 100:+.1f}%".rjust(width)


def main() -> None:
    conn = db.connect()
    try:
        exposures, as_of = current_exposures(conn)
        levels = scenario_levels(conn)
        if not exposures or levels.empty:
            print("Need positions and scenario_prices. Run src/data/flex.py and "
                  "src/data/market_data.py first.")
            return

        print("=" * 78)
        print(f"STRESS TESTS -- book as of {as_of}, % of NAV, base currency {BASE_CURRENCY}")
        print("=" * 78)
        by_currency: dict[str, float] = {}
        for e in exposures:
            by_currency[e.currency] = by_currency.get(e.currency, 0.0) + e.weight
        stock_weight = sum(e.weight for e in exposures if e.series)
        print(f"Stocks {stock_weight * 100:.1f}% of NAV, cash {(1 - stock_weight) * 100:.1f}%. "
              "Currency exposure including cash: "
              + ", ".join(f"{c} {w * 100:.1f}%" for c, w in sorted(by_currency.items())))

        var = None
        returns = risk.portfolio_returns(conn)
        if not returns.empty:
            var = risk.value_at_risk(returns, 0.95).historical

        # ---- historical ----
        proxies = proxy_betas(exposures, levels)
        replays = [
            replay(name, exposures, levels, start, end, proxies)
            for name, (start, end, _) in cfg.HISTORICAL.items()
        ]

        print("\n" + "-" * 78)
        print("HISTORICAL REPLAY: worst peak-to-trough loss inside each window")
        print("-" * 78)
        print(f"{'scenario':<21}{'peak':>12}{'trough':>12}{'loss':>8}{'stock':>8}"
              f"{'FX':>8}{'cross':>7}{'x VaR':>7}")
        for r in replays:
            multiple = "--" if not var else f"{abs(r.loss) / abs(var):.1f}"
            print(f"{r.scenario:<21}{str(r.peak.date()):>12}{str(r.trough.date()):>12}"
                  f"{_pct(r.loss)}{_pct(r.stock)}{_pct(r.fx)}{_pct(r.cross, 7)}{multiple:>7}")
        if var:
            print(f"\n  x VaR: the loss as a multiple of Phase 3's one-day 95% "
                  f"historical VaR ({abs(var) * 100:.2f}%).")
            print("  A multi-day drawdown against a one-day figure -- scale, not a like-for-like.")

        print("\n  Contribution by holding (stock + FX + cross), % of NAV:")
        names = [e.name for e in exposures if abs(e.weight) >= 0.005]
        print(f"  {'':<20}" + "".join(f"{r.scenario[:15]:>17}" for r in replays))
        for name in names:
            cells = ""
            for r in replays:
                row = r.by_exposure.set_index("name").loc[name]
                mark = "*" if name in r.proxied else " "
                cells += f"{row['contribution'] * 100:+15.1f}%{mark}"
            print(f"  {name:<20}{cells}")

        print("\n  Local price move and currency move behind those, peak to trough:")
        for r in replays:
            parts = []
            for name in names:
                row = r.by_exposure.set_index("name").loc[name]
                if name.endswith("cash") or "cash" in name:
                    continue
                parts.append(f"{name} {row['r_local'] * 100:+.0f}%")
            fx_parts = []
            for currency, pair in cfg.FX_SERIES.items():
                move = levels[pair].asof(r.trough) / levels[pair].asof(r.peak) - 1
                fx_parts.append(f"{currency} {move * 100:+.1f}%")
            print(f"  {r.scenario}: " + ", ".join(parts))
            print(f"     vs {BASE_CURRENCY}: " + ", ".join(fx_parts))

        for r in replays:
            if r.proxied:
                how = "; ".join(f"{n} as {h}" for n, h in r.proxied.items())
                print(f"\n  * {r.scenario}: {r.proxied_weight * 100:.1f}% of NAV did not "
                      f"trade then and is proxied ({how}).")

        # ---- hypothetical ----
        print("\n" + "-" * 78)
        print("HYPOTHETICAL SHOCKS")
        print("-" * 78)
        stock_series = [e.series for e in exposures if e.series and e.series in levels.columns]
        for name, spec in cfg.HYPOTHETICAL.items():
            factor = spec["factor"]
            if factor not in levels.columns:
                print(f"\n{name}: factor series {factor!r} not stored; skipped.")
                continue
            betas = factor_betas(levels[stock_series], levels[factor])
            calm = shock(exposures, spec["size"], spec["fx"], betas["beta"].to_dict())

            episode = next((r for r in replays if r.scenario == spec.get("episode")), None)
            stressed, factor_move = None, None
            if episode is not None:
                factor_move = float(levels[factor].asof(episode.trough)
                                    / levels[factor].asof(episode.peak) - 1)
                episode_betas = episode_sensitivities(episode, factor_move)
                stressed = shock(exposures, spec["size"], spec["fx"], episode_betas)

            print(f"\n{name}")
            print(f"  {'':<20}{'weight':>8}{'direct':>9}"
                  f"{'-- calm beta --':>26}{'-- episode beta --':>22}")
            print(f"  {'':<20}{'':>8}{'':>9}{'beta':>7}{'R2':>6}{'effect':>13}"
                  f"{'beta':>9}{'effect':>13}")
            for i, row in enumerate(calm.itertuples(index=False)):
                if abs(row.weight) < 0.005:
                    continue
                if row.name in betas.index:
                    b = betas.loc[row.name]
                    beta, r2 = f"{b['beta']:+.2f}", f"{b['r_squared']:.2f}"
                else:
                    beta = r2 = "--"
                line = (f"  {row.name:<20}{row.weight * 100:7.1f}%{_pct(row.direct, 9)}"
                        f"{beta:>7}{r2:>6}{_pct(row.full, 13)}")
                if stressed is not None:
                    e_beta = episode_betas.get(row.name)
                    line += (f"{'--' if e_beta is None else f'{e_beta:+.2f}':>9}"
                             f"{_pct(stressed['full'].iloc[i], 13)}")
                print(line)
            total = (f"  {'TOTAL':<20}{'':>8}{_pct(calm['direct'].sum(), 9)}{'':>13}"
                     f"{_pct(calm['full'].sum(), 13)}")
            if stressed is not None:
                total += f"{'':>9}{_pct(stressed['full'].sum(), 13)}"
            print(total)
            print(f"  calm beta: to {factor}, last {cfg.BETA_WEEKS} weekly returns.")
            if episode is not None:
                print(f"  episode beta: each stock's move per unit of {factor} in "
                      f"'{episode.scenario}' ({factor} {factor_move * 100:+.1f}%, "
                      f"{episode.peak.date()} to {episode.trough.date()}).")
            for label, table in (("calm", calm), ("episode", stressed)):
                if table is not None and table["full"].isna().any():
                    missing = table[table["full"].isna()]["name"].tolist()
                    print(f"  no {label} beta for {missing}; that total excludes them.")

        print("\n  direct: only the stated move happens. The other two add each stock")
        print("  moving by beta x the shock. A calm beta with R2 near zero means the")
        print("  factor explains almost none of that stock's ordinary weekly moves.")
        print("  An episode beta is a single observation of a forced move, not an")
        print("  estimate -- it attributes the whole fall in that episode to the factor.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
