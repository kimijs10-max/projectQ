"""
Charts for the factor-exposure results (Phase 4a).

Two figures, written to reports/:

  factor_exposures.png -- dot-and-whisker of each sleeve's factor
      loadings with 95% Newey-West confidence intervals. Dot-and-whisker
      rather than bars: with an interval estimate the question is
      whether the interval crosses zero, and a bar would imply its area
      carried meaning. The zero line is the reference the whole chart is
      read against.

  rolling_betas.png -- rolling 63-day market and value exposures, as
      small multiples sharing one y-scale per panel. Never a second
      y-axis.

Significance is shown by the interval crossing zero, not by colour:
colour carries sleeve identity only, so identity is never
colour-alone and the statistics stay readable in greyscale.

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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from analytics import factors  # noqa: E402
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


def main() -> None:
    conn = db.connect()
    try:
        print("Writing charts to", OUTPUT_DIR)
        p1 = plot_factor_exposures(conn, OUTPUT_DIR / "factor_exposures.png")
        print("  ", p1.relative_to(PROJECT_ROOT))
        p2 = plot_rolling_betas(conn, OUTPUT_DIR / "rolling_betas.png")
        print("  ", p2.relative_to(PROJECT_ROOT))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
