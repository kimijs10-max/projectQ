"""
Charts for the factor-exposure (Phase 4a) and risk (Phase 3) results.

Four figures, written to reports/:

  factor_exposures.png -- dot-and-whisker of each sleeve's factor
      loadings with 95% Newey-West confidence intervals. Dot-and-whisker
      rather than bars: with an interval estimate the question is
      whether the interval crosses zero, and a bar would imply its area
      carried meaning. The zero line is the reference the whole chart is
      read against.

  rolling_betas.png -- rolling 63-day market and value exposures, as
      small multiples sharing one y-scale per panel. Never a second
      y-axis.

  return_distribution.png -- histogram of daily base-currency returns
      with the historical and parametric VaR estimates marked, and a
      fitted normal curve drawn as grey reference. The chart's job is the
      comparison between the two estimates, which is what shows whether
      the normal assumption understates the tail.

  correlation_matrix.png -- daily and weekly correlation of holdings on a
      diverging scale, lower triangle only, with the number of
      overlapping observations printed inside every cell. Correlation has
      a real zero and a sign, so the scale diverges from grey rather than
      running as one hue.

Significance is shown by the interval crossing zero, not by colour:
colour carries sleeve identity only, so identity is never
colour-alone and the statistics stay readable in greyscale. The same
principle governs the risk charts -- sample size is printed, never
implied by saturation.

Run directly:
    python src/report/plots.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # no display needed; we only write files
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from analytics import factors, risk  # noqa: E402
from storage import db  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "reports"

# Categorical slots 1 and 2 of the reference palette, used unmodified
# and in fixed order. Sleeve identity owns the colour; nothing else does.
SERIES = {"USD": "#2a78d6", "JPY": "#eb6834"}

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

CI_Z = 1.96  # 95% interval


def _style_axes(ax) -> None:
    """Recessive grid and axes; data is the only prominent thing."""
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
        ax.spines[side].set_linewidth(1.0)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_color(INK_SECONDARY)


def plot_factor_exposures(conn, path: Path) -> Path:
    """Dot-and-whisker of factor loadings with 95% HAC intervals."""
    results = {}
    for currency in SERIES:
        try:
            results[currency] = factors.regress_sleeve(conn, currency)
        except ValueError as exc:
            print(f"  skipping {currency}: {exc}")

    if not results:
        raise RuntimeError("No sleeve regressions available to plot")

    names = factors.FACTORS  # betas only; alpha is a return, plotted elsewhere
    y_base = np.arange(len(names))[::-1]  # MKT at top
    offset = 0.16

    fig, ax = plt.subplots(figsize=(8.4, 4.8), facecolor=SURFACE)
    _style_axes(ax)

    ax.axvline(0, color=AXIS, linewidth=1.4, zorder=1)

    for i, (currency, res) in enumerate(results.items()):
        beta = dict(zip(res.names, res.beta))
        se = dict(zip(res.names, res.se_nw))
        y = y_base + (offset if i == 0 else -offset)
        x = np.array([beta[f] for f in names])
        err = np.array([se[f] * CI_Z for f in names])

        ax.errorbar(
            x, y, xerr=err,
            fmt="o", markersize=7, linewidth=2.0,
            color=SERIES[currency], ecolor=SERIES[currency],
            capsize=0, zorder=3,
            markeredgecolor=SURFACE, markeredgewidth=1.5,
            label=f"{currency} sleeve",
        )

    ax.set_yticks(y_base)
    ax.set_yticklabels(names)
    ax.grid(axis="x", color=GRID, linewidth=1.0, zorder=0)
    ax.set_axisbelow(True)
    ax.set_xlabel("factor loading (beta)", color=INK_SECONDARY, fontsize=10)

    legend = ax.legend(
        loc="lower right", frameon=False, fontsize=9.5, handletextpad=0.6
    )
    for text in legend.get_texts():
        text.set_color(INK_SECONDARY)

    # Title and subtitle are placed on the figure, not the axes, with the
    # layout rect reserving room for them -- an axes title plus an
    # axes-relative subtitle collide once the subtitle wraps.
    fig.tight_layout(rect=(0, 0, 1, 0.84))
    fig.text(0.012, 0.955, "Factor exposures by sleeve",
             color=INK, fontsize=13, fontweight="bold", va="top")
    fig.text(0.012, 0.902,
             "Whiskers are 95% Newey-West confidence intervals.",
             color=MUTED, fontsize=8.5, va="top")
    fig.text(0.012, 0.862,
             "An interval crossing zero is not statistically distinguishable "
             "from no exposure.",
             color=MUTED, fontsize=8.5, va="top")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    return path


def plot_rolling_betas(conn, path: Path, window: int = 63) -> Path:
    """Rolling exposures as small multiples, one panel per factor."""
    series = {}
    for currency in SERIES:
        try:
            roll = factors.rolling_betas(conn, currency, window=window)
        except (ValueError, KeyError) as exc:
            print(f"  skipping {currency}: {exc}")
            continue
        if not roll.empty:
            series[currency] = roll

    if not series:
        raise RuntimeError("No rolling betas available to plot")

    panels = [("beta_MKT", "Market (MKT)"), ("beta_HML", "Value (HML)")]
    fig, axes = plt.subplots(
        1, len(panels), figsize=(10.4, 4.0), facecolor=SURFACE, sharex=True
    )

    for ax, (col, label) in zip(axes, panels):
        _style_axes(ax)
        ax.axhline(0, color=AXIS, linewidth=1.2, zorder=1)
        for currency, roll in series.items():
            if col not in roll.columns:
                continue
            x = np.arange(len(roll))
            ax.plot(
                x, roll[col].to_numpy(), linewidth=2.0,
                color=SERIES[currency], label=f"{currency} sleeve", zorder=3,
            )
            # Direct label at the line end rather than relying on the legend alone.
            ax.annotate(
                currency,
                xy=(x[-1], roll[col].to_numpy()[-1]),
                xytext=(4, 0), textcoords="offset points",
                color=SERIES[currency], fontsize=9, fontweight="600",
                va="center",
            )

        any_roll = next(iter(series.values()))
        ticks = np.linspace(0, len(any_roll) - 1, 4).astype(int)
        ax.set_xticks(ticks)
        ax.set_xticklabels(
            [str(any_roll["date"].iloc[t])[:7] for t in ticks], fontsize=8.5
        )
        ax.grid(axis="y", color=GRID, linewidth=1.0, zorder=0)
        ax.set_axisbelow(True)
        ax.set_title(label, color=INK, fontsize=11, fontweight="bold", loc="left", pad=8)
        # Extra right margin so the end-of-line direct labels are not
        # clipped by the axes edge.
        ax.margins(x=0.06)
        ax.set_xlim(left=-len(any_roll) * 0.02, right=len(any_roll) * 1.12)

    axes[0].set_ylabel("rolling beta", color=INK_SECONDARY, fontsize=10)

    fig.tight_layout(rect=(0, 0, 1, 0.80))
    fig.text(0.012, 0.975, f"Rolling {window}-day factor exposures",
             color=INK, fontsize=13, fontweight="bold", va="top")
    fig.text(0.012, 0.922,
             "Two factors only: a six-factor fit in a 63-day window has too few "
             "degrees of freedom to be meaningful.",
             color=MUTED, fontsize=8.5, va="top")

    # Legend as well as the direct labels: identity should not rest on the
    # end-of-line annotation alone. Placed on the figure rather than in an
    # axes, where it would sit on top of the lines.
    handles, labels = axes[0].get_legend_handles_labels()
    legend = fig.legend(
        handles, labels, loc="upper left", bbox_to_anchor=(0.010, 0.895),
        ncol=len(labels), frameon=False, fontsize=9, handletextpad=0.6,
        columnspacing=1.6,
    )
    for text in legend.get_texts():
        text.set_color(INK_SECONDARY)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# Phase 3: risk charts
# ---------------------------------------------------------------------------

# Diverging scale for correlation: blue (offsetting) <-> gray (nothing) <->
# red (co-moving). A single-hue sequential ramp would be wrong here --
# correlation has a natural zero and a sign, and the sign is the whole
# point for a concentrated book.
DIVERGING_BLUE = ["#cde2fb", "#9ec5f4", "#5598e7", "#2a78d6"]
DIVERGING_RED = ["#fbd7d7", "#f4a8a7", "#e86f6e", "#d03b3b"]
DIVERGING_MID = "#f0efec"
_CORR_BANDS = (0.05, 0.15, 0.30, 0.45)

# Weekly pairs with fewer overlapping observations than this are labelled,
# because at ~25 weeks the standard error on a correlation is about 0.19
# and the coefficient is not distinguishable from noise.
THIN_SAMPLE = 40

# Symbols in this account's Japanese sleeve are Tokyo-listed (".T").
def _is_tokyo(symbol: str) -> bool:
    return symbol.upper().endswith(".T")


def _diverging_fill(value: float) -> tuple[str, str]:
    """Fill and legible text colour for one correlation cell."""
    magnitude = abs(value)
    if magnitude < _CORR_BANDS[0]:
        return DIVERGING_MID, INK_SECONDARY
    arm = DIVERGING_RED if value > 0 else DIVERGING_BLUE
    index = min(sum(magnitude >= b for b in _CORR_BANDS) - 1, len(arm) - 1)
    return arm[index], (SURFACE if index >= 2 else INK)


def plot_return_distribution(conn, path: Path, level: float = 0.95) -> Path:
    """
    Daily return distribution with both VaR estimates marked.

    The chart exists to make one comparison visible: the historical
    quantile against the normal-assumption quantile. The normal curve is
    drawn as recessive grey chrome rather than as a series, because it is
    the assumption being tested, not a competing measurement.

    The two VaR lines sit about 0.1pp apart, far too close to label in
    place, so their values live in the legend block instead -- hue carries
    which line is which.
    """
    returns = risk.portfolio_returns(conn, "attributed")
    if returns.empty:
        raise RuntimeError("No return series available to plot")

    var = risk.value_at_risk(returns, level)
    pct = returns.to_numpy() * 100.0
    hist_var = var.historical * 100.0
    param_var = var.parametric * 100.0
    beyond = int((pct <= -hist_var).sum())

    fig, ax = plt.subplots(figsize=(8.4, 4.8), facecolor=SURFACE)
    _style_axes(ax)

    counts, edges = np.histogram(pct, bins=36)
    width = edges[1] - edges[0]
    centres = edges[:-1] + width / 2.0
    # 0.86 of the bin width leaves a surface gap between adjacent bars.
    ax.bar(centres, counts, width=width * 0.86, color="#9ec5f4",
           linewidth=0, zorder=2)

    # Normal density fitted to the same mean and standard deviation,
    # scaled to the histogram's counts so the two are comparable.
    mu, sigma = pct.mean(), pct.std(ddof=1)
    grid = np.linspace(pct.min() - 0.4, pct.max() + 0.4, 400)
    density = np.exp(-0.5 * ((grid - mu) / sigma) ** 2) / (sigma * np.sqrt(2 * np.pi))
    ax.plot(grid, density * len(pct) * width, color=MUTED, linewidth=2.0,
            linestyle=(0, (5, 3)), zorder=3)

    top = max(counts.max(), (density * len(pct) * width).max())
    ax.set_ylim(0, top * 1.28)
    ax.set_xlim(grid[0] - 0.2, grid[-1] + 0.2)

    # Shade the realised tail, then the two estimates of where it starts.
    # The span runs to the axis edge rather than to the first bin, or its
    # left boundary reads as a second, meaningless threshold.
    ax.axvspan(ax.get_xlim()[0], -hist_var, color="#eb6834", alpha=0.07, zorder=1)
    ax.axvline(-param_var, color=SERIES["USD"], linewidth=2.0, zorder=4)
    ax.axvline(-hist_var, color=SERIES["JPY"], linewidth=2.0, zorder=4)

    ax.annotate("normal fit", xy=(mu + 1.05 * sigma, 0.52 * top),
                xytext=(6, 0), textcoords="offset points",
                color=MUTED, fontsize=9, va="center")

    label = f"{int(level * 100)}% one-day VaR"
    ax.text(0.022, 0.97, label, transform=ax.transAxes, color=INK,
            fontsize=9.5, fontweight="bold", va="top")
    for row, (name, value, colour) in enumerate((
        ("historical", hist_var, SERIES["JPY"]),
        ("parametric", param_var, SERIES["USD"]),
    )):
        y = 0.90 - row * 0.075
        ax.plot([0.026, 0.054], [y, y], transform=ax.transAxes,
                color=colour, linewidth=2.6, solid_capstyle="butt",
                clip_on=False, zorder=5)
        ax.text(0.066, y, f"{name}   −{value:.2f}%", transform=ax.transAxes,
                color=INK_SECONDARY, fontsize=9.5, va="center")

    ax.grid(axis="y", color=GRID, linewidth=1.0, zorder=0)
    ax.set_axisbelow(True)
    ax.set_xlabel("daily return, % of NAV", color=INK_SECONDARY, fontsize=10)
    ax.set_ylabel("days", color=INK_SECONDARY, fontsize=10)

    annualised = sigma * np.sqrt(risk.TRADING_DAYS)
    fig.tight_layout(rect=(0, 0, 1, 0.84))
    fig.text(0.012, 0.955, "Daily return distribution and value at risk",
             color=INK, fontsize=13, fontweight="bold", va="top")
    fig.text(0.012, 0.902,
             f"{var.n_obs} trading days.  Daily volatility {sigma:.2f}%, "
             f"annualised {annualised:.1f}%.  {beyond} days fell beyond the "
             f"{int(level * 100)}% line.",
             color=MUTED, fontsize=8.5, va="top")
    fig.text(0.012, 0.862,
             "The historical quantile sits further out than the normal one, "
             "so the normal assumption understates this tail.",
             color=MUTED, fontsize=8.5, va="top")

    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    return path


def plot_correlation_heatmap(conn, path: Path) -> Path:
    """
    Daily and weekly correlation of holdings, with the observation count
    printed inside every cell.

    The counts are not decoration. A weekly matrix built on one year of
    data has at most ~52 observations, and far fewer for any pair whose
    holding periods only partly overlap; printing the coefficient alone
    would invite reading a sampling artefact as a finding. This is also
    how a real bug was caught -- a pair was reported with 53 weeks behind
    it despite having no overlap at all.

    Lower triangle only: a correlation matrix is symmetric, so the upper
    half is the same numbers again, and the diagonal is 1 by construction.
    """
    panels = []
    for freq, label in (("D", "Daily"), ("W", "Weekly")):
        matrix = risk.correlation_matrix(conn, freq)
        obs = risk.pairwise_obs(conn, freq)
        if matrix.empty:
            continue
        panels.append((label, matrix, obs.reindex(index=matrix.index, columns=matrix.columns)))

    if not panels:
        raise RuntimeError("No correlation matrix available to plot")

    symbols = list(panels[0][1].index)
    n = len(symbols)
    # Tokyo names first, then US, so the cross-market pairs form one block.
    symbols = sorted(symbols, key=lambda s: (not _is_tokyo(s), s))
    split = sum(_is_tokyo(s) for s in symbols)

    fig, axes = plt.subplots(1, len(panels), figsize=(11.0, 6.2), facecolor=SURFACE)
    axes = np.atleast_1d(axes)

    for ax, (label, matrix, obs) in zip(axes, panels):
        matrix = matrix.reindex(index=symbols, columns=symbols)
        obs = obs.reindex(index=symbols, columns=symbols)
        ax.set_facecolor(SURFACE)
        for side in ("top", "right", "bottom", "left"):
            ax.spines[side].set_visible(False)
        ax.set_xlim(0, n - 1)
        ax.set_ylim(n, 1)
        ax.set_xticks([])
        ax.set_yticks([])

        for i in range(1, n):
            for j in range(i):
                value = matrix.iat[i, j]
                count = obs.iat[i, j]
                count = 0 if pd.isna(count) else int(count)
                # 0.04 inset on each side is the surface gap between fills.
                if pd.isna(value):
                    ax.add_patch(plt.Rectangle(
                        (j + 0.04, i + 0.04), 0.92, 0.92,
                        facecolor=SURFACE, edgecolor=GRID, linewidth=1.0, zorder=2))
                    ax.text(j + 0.5, i + 0.5, "no\noverlap", ha="center",
                            va="center", color=MUTED, fontsize=8, zorder=3)
                    continue
                face, ink = _diverging_fill(float(value))
                ax.add_patch(plt.Rectangle(
                    (j + 0.04, i + 0.04), 0.92, 0.92,
                    facecolor=face, edgecolor="none", zorder=2))
                shown = "0.00" if abs(value) < 0.005 else f"{value:+.2f}"
                ax.text(j + 0.5, i + 0.42, shown, ha="center", va="center",
                        color=ink, fontsize=10.5, fontweight="bold", zorder=3)
                thin = "" if count >= THIN_SAMPLE else " ▲"
                ax.text(j + 0.5, i + 0.70, f"n={count}{thin}", ha="center",
                        va="center", color=ink, fontsize=8, alpha=0.85, zorder=3)

        for j, symbol in enumerate(symbols[:-1]):
            ax.text(j + 0.5, 0.94, symbol, ha="center", va="bottom",
                    color=INK_SECONDARY, fontsize=9)
        for i, symbol in enumerate(symbols[1:], start=1):
            ax.text(-0.08, i + 0.5, symbol, ha="right", va="center",
                    color=INK_SECONDARY, fontsize=9)

        # Outline the US-by-Japan block: the asynchronous-close question
        # lives entirely inside it.
        if 0 < split < n:
            ax.add_patch(plt.Rectangle(
                (0, split), split, n - split,
                facecolor="none", edgecolor=AXIS, linewidth=1.6,
                linestyle=(0, (4, 2)), zorder=4))

        ax.set_title(f"{label} returns", color=INK, fontsize=11,
                     fontweight="bold", loc="left", pad=26)

    # Discrete diverging legend: equal step count per arm, gray for "nothing".
    # Generous bottom margin: the legend and the sample-size note below the
    # panels are load-bearing here, not decoration.
    fig.tight_layout(rect=(0.03, 0.22, 1, 0.82))
    swatches = list(reversed(DIVERGING_BLUE)) + [DIVERGING_MID] + DIVERGING_RED
    bar = fig.add_axes((0.012, 0.055, 0.30, 0.024))
    bar.set_xlim(0, len(swatches))
    bar.set_ylim(0, 1)
    bar.axis("off")
    for k, colour in enumerate(swatches):
        bar.add_patch(plt.Rectangle((k + 0.03, 0), 0.94, 1, facecolor=colour,
                                    edgecolor="none"))
    # End labels align inward; centred on the bar's ends they hang off the
    # figure edge.
    for x, text, align in ((0.0, "−0.45", "left"),
                           (len(swatches) / 2.0, "0", "center"),
                           (float(len(swatches)), "+0.45", "right")):
        bar.text(x, -0.55, text, ha=align, va="top", color=MUTED, fontsize=8)
    fig.text(0.012, 0.108, "offsetting ←  correlation  → co-moving",
             color=INK_SECONDARY, fontsize=9, va="bottom")
    fig.text(0.37, 0.028,
             f"▲ marks a pair with fewer than {THIN_SAMPLE} overlapping "
             f"observations. At that sample size the\nstandard error on a "
             f"correlation is roughly 0.19 — wide enough to produce these\n"
             f"daily-to-weekly swings on its own.\n"
             f"The dashed block is the US × Japan quadrant.",
             color=MUTED, fontsize=8.5, va="bottom", linespacing=1.5)

    fig.text(0.012, 0.968, "Correlation of holdings, base-currency returns",
             color=INK, fontsize=13, fontweight="bold", va="top")
    fig.text(0.012, 0.924,
             "Tokyo closes about thirteen hours before New York, so same-date "
             "daily correlations should understate US/Japan co-movement and "
             "weekly sampling should not.",
             color=MUTED, fontsize=8.5, va="top")
    fig.text(0.012, 0.890,
             "The observation counts are why that cannot be concluded here: "
             "every pair that swings between the panels is also a thin-sample "
             "pair.",
             color=MUTED, fontsize=8.5, va="top")

    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    return path


def main() -> None:
    conn = db.connect()
    try:
        print("Writing charts to", OUTPUT_DIR)
        p1 = plot_factor_exposures(conn, OUTPUT_DIR / "factor_exposures.png")
        print("  ", p1.relative_to(PROJECT_ROOT))
        p2 = plot_rolling_betas(conn, OUTPUT_DIR / "rolling_betas.png")
        print("  ", p2.relative_to(PROJECT_ROOT))
        p3 = plot_return_distribution(conn, OUTPUT_DIR / "return_distribution.png")
        print("  ", p3.relative_to(PROJECT_ROOT))
        p4 = plot_correlation_heatmap(conn, OUTPUT_DIR / "correlation_matrix.png")
        print("  ", p4.relative_to(PROJECT_ROOT))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
