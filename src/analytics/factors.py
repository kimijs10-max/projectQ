"""
Phase 4a: factor exposure.

Regresses each sleeve's excess return on its region's Fama-French
factors:

    R_sleeve - RF = a + b_MKT*MKT + b_SMB*SMB + b_HML*HML
                      + b_RMW*RMW + b_CMA*CMA + b_MOM*MOM + e

The betas say how much of the return came from systematic tilts; alpha
is what the factors cannot explain. For a stated intrinsic-value
strategy this is a direct test of the thesis: a genuine value approach
should show a positive HML loading, and buying quality cheaply should
show positive RMW. If the factors explain everything and alpha is
indistinguishable from zero, the honest reading is that a cheap factor
ETF would have delivered the same thing.

Three deliberate choices:

1. Local currency, local factors. Each sleeve is regressed in its own
   trading currency against its own region's factors. Regressing
   SGD-denominated returns on USD-denominated factors would push the
   FX move into the residual and corrupt alpha, since the factors
   cannot explain currency. Phase 2 already separates r_local from
   r_fx, so this needs no extra data.

2. Sleeves separately, per the roadmap. A blended regression across two
   countries would produce betas describing neither.

3. Newey-West standard errors reported alongside plain OLS. Daily
   residuals are serially correlated, which makes OLS t-statistics
   overstated; Phase 2 found serial structure in this same data, so
   assuming it away here would be inconsistent. Judge significance on
   the HAC column.

Caveat that belongs on every reading of this output: the sleeves hold
two and three names respectively. Factor betas on a book that
concentrated are genuinely noisy, and R-squared will be low because
idiosyncratic risk dominates. The regression describes exposure, not
skill, and one year of daily data cannot establish skill either way.

Run directly:
    python src/analytics/factors.py
"""

from __future__ import annotations

import math
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics import pnl  # noqa: E402
from storage import db  # noqa: E402

FACTORS = ["MKT", "SMB", "HML", "RMW", "CMA", "MOM"]

# Which region's factors price each trading currency.
CURRENCY_TO_REGION = {"USD": "US", "JPY": "JP"}

TRADING_DAYS = 252


@dataclass
class RegressionResult:
    region: str
    currency: str
    n_obs: int
    r2: float
    adj_r2: float
    nw_lags: int
    names: list[str] = field(default_factory=list)
    beta: np.ndarray | None = None
    se_ols: np.ndarray | None = None
    se_nw: np.ndarray | None = None

    def table(self) -> pd.DataFrame:
        t_ols = self.beta / self.se_ols
        t_nw = self.beta / self.se_nw
        return pd.DataFrame({
            "term": self.names,
            "coef": self.beta,
            "se_ols": self.se_ols,
            "t_ols": t_ols,
            "se_nw": self.se_nw,
            "t_nw": t_nw,
            "p_nw": [_two_sided_p(t) for t in t_nw],
        })

    @property
    def alpha_annualised(self) -> float:
        """Intercept compounded over a trading year."""
        return (1.0 + self.beta[0]) ** TRADING_DAYS - 1.0


def _two_sided_p(t: float) -> float:
    """
    Two-sided p-value using the normal approximation to the t
    distribution. Exact t would need scipy; with ~230 observations the
    difference is immaterial, and this keeps the dependency list short.
    """
    z = abs(float(t))
    phi = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return 2.0 * (1.0 - phi)


def sleeve_returns(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Daily local-currency return per sleeve (currency group).

    The sleeve return is the value-weighted mean of its holdings' local
    returns. Weighting by base-currency value is valid here even though
    the returns are local: every holding in a sleeve shares one
    currency, so the FX rate is a common factor and cancels out of the
    weighted average.

    Returns: date, currency, sleeve_return, n_holdings.
    """
    mtm = pnl.compute_mark_to_market(conn)
    if mtm.empty:
        return pd.DataFrame(columns=["date", "currency", "sleeve_return", "n_holdings"])

    def weighted(group: pd.DataFrame) -> pd.Series:
        weight = group["value_prev_base"]
        total = weight.sum()
        if total == 0:
            return pd.Series({"sleeve_return": np.nan, "n_holdings": len(group)})
        return pd.Series({
            "sleeve_return": float((group["r_local"] * weight).sum() / total),
            "n_holdings": len(group),
        })

    out = (
        mtm.groupby(["date", "currency"], group_keys=True)[["r_local", "value_prev_base"]]
        .apply(weighted)
        .reset_index()
    )
    return out.dropna(subset=["sleeve_return"])


def _factor_frame(conn: sqlite3.Connection, region: str) -> pd.DataFrame:
    """Wide factor table for one region, indexed by date."""
    f = db.read_table(conn, "factor_returns")
    f = f[f["region"] == region]
    if f.empty:
        raise ValueError(
            f"No factor data for region {region!r} -- run src/data/factor_data.py"
        )
    return f.pivot_table(index="date", columns="factor", values="value")


def _ols_with_hac(y: np.ndarray, X: np.ndarray, nw_lags: int):
    """
    OLS plus Newey-West HAC standard errors.

    X must already include an intercept column. Returns
    (beta, se_ols, se_nw, r2, adj_r2).
    """
    n, k = X.shape
    xtx_inv = np.linalg.inv(X.T @ X)
    beta = xtx_inv @ X.T @ y

    resid = y - X @ beta
    rss = float(resid @ resid)
    tss = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - rss / tss if tss > 0 else np.nan
    adj_r2 = 1.0 - (1.0 - r2) * (n - 1) / (n - k) if tss > 0 else np.nan

    sigma2 = rss / (n - k)
    se_ols = np.sqrt(np.diag(sigma2 * xtx_inv))

    # Newey-West: S = sum_t u_t^2 x_t x_t' + weighted cross-lag terms,
    # with Bartlett weights 1 - l/(L+1).
    xu = X * resid[:, None]
    S = xu.T @ xu
    for lag in range(1, nw_lags + 1):
        w = 1.0 - lag / (nw_lags + 1.0)
        G = xu[lag:].T @ xu[:-lag]
        S += w * (G + G.T)
    se_nw = np.sqrt(np.diag(xtx_inv @ S @ xtx_inv))

    return beta, se_ols, se_nw, r2, adj_r2


def regress_sleeve(conn: sqlite3.Connection, currency: str) -> RegressionResult:
    """Full-sample factor regression for one sleeve."""
    region = CURRENCY_TO_REGION.get(currency)
    if region is None:
        raise ValueError(f"No factor region mapped for currency {currency!r}")

    sleeves = sleeve_returns(conn)
    sleeve = sleeves[sleeves["currency"] == currency].set_index("date")
    if sleeve.empty:
        raise ValueError(f"No sleeve returns for {currency!r}")

    factors = _factor_frame(conn, region)
    missing = [f for f in FACTORS + ["RF"] if f not in factors.columns]
    if missing:
        raise ValueError(f"Missing factors for {region}: {missing}")

    joined = sleeve[["sleeve_return"]].join(factors, how="inner").dropna()
    if len(joined) < len(FACTORS) + 2:
        raise ValueError(
            f"Only {len(joined)} overlapping observations for {currency} -- "
            "too few to regress on six factors"
        )

    y = (joined["sleeve_return"] - joined["RF"]).to_numpy(dtype=float)
    X = np.column_stack([np.ones(len(joined))] + [joined[f].to_numpy(dtype=float) for f in FACTORS])

    # Newey-West rule of thumb for the truncation lag.
    nw_lags = int(4.0 * (len(joined) / 100.0) ** (2.0 / 9.0))

    beta, se_ols, se_nw, r2, adj_r2 = _ols_with_hac(y, X, nw_lags)

    return RegressionResult(
        region=region,
        currency=currency,
        n_obs=len(joined),
        r2=r2,
        adj_r2=adj_r2,
        nw_lags=nw_lags,
        names=["alpha"] + FACTORS,
        beta=beta,
        se_ols=se_ols,
        se_nw=se_nw,
    )


def rolling_betas(
    conn: sqlite3.Connection,
    currency: str,
    factors: tuple[str, ...] = ("MKT", "HML"),
    window: int = 63,
) -> pd.DataFrame:
    """
    Rolling exposures over a trailing window (63 days ~ one quarter).

    Fitted on the named factors only, not all six: with roughly a year
    of data a six-factor fit in a 63-day window has too few degrees of
    freedom to say anything. Even at two factors these are indicative
    rather than precise.
    """
    region = CURRENCY_TO_REGION[currency]
    sleeves = sleeve_returns(conn)
    sleeve = sleeves[sleeves["currency"] == currency].set_index("date")
    joined = sleeve[["sleeve_return"]].join(_factor_frame(conn, region), how="inner").dropna()

    if len(joined) <= window:
        return pd.DataFrame()

    y_all = (joined["sleeve_return"] - joined["RF"]).to_numpy(dtype=float)
    X_all = np.column_stack(
        [np.ones(len(joined))] + [joined[f].to_numpy(dtype=float) for f in factors]
    )
    dates = joined.index.to_list()

    rows = []
    for end in range(window, len(joined) + 1):
        sl = slice(end - window, end)
        try:
            b = np.linalg.lstsq(X_all[sl], y_all[sl], rcond=None)[0]
        except np.linalg.LinAlgError:
            continue
        row = {"date": dates[end - 1]}
        row.update({f"beta_{f}": float(b[i + 1]) for i, f in enumerate(factors)})
        rows.append(row)

    return pd.DataFrame(rows)


def main() -> None:
    conn = db.connect()
    try:
        sleeves = sleeve_returns(conn)
        print("Sleeve coverage (local-currency returns):")
        print(
            sleeves.groupby("currency")
            .agg(days=("sleeve_return", "size"),
                 first=("date", "min"),
                 last=("date", "max"),
                 max_holdings=("n_holdings", "max"))
            .to_string()
        )

        for currency in sorted(sleeves["currency"].unique()):
            if currency not in CURRENCY_TO_REGION:
                print(f"\n{currency}: no factor region mapped, skipping")
                continue
            try:
                res = regress_sleeve(conn, currency)
            except ValueError as exc:
                print(f"\n{currency}: {exc}")
                continue

            print(f"\n{'=' * 66}")
            print(f"{res.currency} sleeve vs. {res.region} factors "
                  f"({res.n_obs} obs, Newey-West lags={res.nw_lags})")
            print("=" * 66)
            tbl = res.table().copy()
            for col in ("coef", "se_ols", "se_nw"):
                tbl[col] = (tbl[col] * 100).round(4)
            tbl[["t_ols", "t_nw"]] = tbl[["t_ols", "t_nw"]].round(2)
            tbl["p_nw"] = tbl["p_nw"].round(3)
            tbl.columns = ["term", "coef %/day", "se_ols %", "t_ols", "se_nw %", "t_nw", "p_nw"]
            print(tbl.to_string(index=False))
            print(f"\n  R-squared {res.r2:.3f} | adjusted {res.adj_r2:.3f}")
            print(f"  alpha {res.beta[0] * 100:.4f}%/day "
                  f"({res.alpha_annualised * 100:+.1f}%/yr compounded), "
                  f"t = {res.beta[0] / res.se_nw[0]:.2f} HAC")

            roll = rolling_betas(conn, currency)
            if not roll.empty:
                cols = [c for c in roll.columns if c.startswith("beta_")]
                print(f"\n  rolling 63-day exposures ({len(roll)} windows):")
                print(roll[cols].agg(["mean", "std", "min", "max"]).round(2).to_string())

        print(f"\n{'=' * 66}")
        print("Reading these numbers: judge significance on t_nw, not t_ols.")
        print("Each sleeve holds only two or three names, so betas are noisy")
        print("and low R-squared is expected -- idiosyncratic risk dominates.")
        print("One year of daily data cannot establish skill either way.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
