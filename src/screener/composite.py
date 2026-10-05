"""
Phase 4c: sector-neutral composite scores.

A raw metric means nothing on its own across sectors. Gross profitability
of 0.08 is poor for a software company and ordinary for a shipping line;
a price-to-book of 0.95 is cheap for a semiconductor maker and unremarkable
for a Japanese regional bank. Scoring the book on one list would mostly
rank sectors, and would say more about which industries are currently
expensive than about which companies are.

So every metric is converted to a percentile *within the holding's own
verified peer group*, and no number is ever compared across groups. That
is what "sector-neutral" means here: the comparison is to companies facing
the same economics, not to the market.

Three composites are built, matching the build order's question of whether
quality and momentum add anything to value alone:

    value                 book-to-market percentile
    value + quality       + gross profitability and F-Score
    value + quality + momentum    + 12-1 price momentum

Two rules decide when a composite is withheld.

**A named sleeve must exist.** "Value + quality" computed without a
quality score is just the value score wearing a different label, so a
company with no quality metrics gets no value+quality composite at all.
8306.T is the case that forces this: a bank has no gross profitability and
no applicable F-Score, so only its value sleeve is real.

**A sleeve may be internally partial, and says so.** Quality blends gross
profitability and the F-Score ratio; if only one is available the sleeve
still forms, and the component count is reported alongside it. The
distinction from the rule above is deliberate -- a half-measured sleeve is
noisier, an absent sleeve is not a measurement.

Percentiles also carry their peer-group size. A percentile drawn from
seventeen peers is a coarser instrument than one drawn from twenty-five,
and the rank position is printed next to it for that reason.

Run directly:
    python src/screener/composite.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from screener import quality, universe  # noqa: E402
from storage import db  # noqa: E402

# The F-Score ratio is only used when most of its tests could be run.
# Below this, score-over-evaluable is too coarse a fraction to rank on:
# 3 of 4 and 6 of 8 are the same number from very different evidence.
MIN_F_EVALUABLE = 7

SLEEVES = {
    "value": ["book_to_market"],
    "quality": ["gross_profitability", "f_ratio"],
    "momentum": ["momentum_12_1"],
}

COMPOSITES = {
    "value": ["value"],
    "value+quality": ["value", "quality"],
    "value+quality+momentum": ["value", "quality", "momentum"],
}


def universe_metrics(
    conn: sqlite3.Connection,
    knowledge_date: str | None = None,
) -> pd.DataFrame:
    """
    Raw metrics for every symbol in the screening universe.

    Prices come from screen_prices, which covers the peers as well as the
    holdings, and fundamentals go through the same availability filter as
    everywhere else.
    """
    groups, _ = universe.verified_peers(conn)
    symbols = sorted(set(groups) | {p for peers in groups.values() for p in peers})
    if not symbols:
        return pd.DataFrame()

    df = quality.screen(conn, knowledge_date, symbols, price_table="screen_prices")
    if df.empty:
        return df

    # Book-to-market rather than price-to-book, so that higher is better
    # for every signal and the ranking direction never has to be
    # remembered. Non-positive book value is dropped rather than inverted:
    # a company with negative equity is not "infinitely cheap", the value
    # concept simply does not apply to it.
    df["book_to_market"] = [
        1.0 / pb if (pd.notna(pb) and pb > 0) else None
        for pb in df["price_to_book"]
    ]
    df["f_ratio"] = [
        sc / ev if ev >= MIN_F_EVALUABLE else None
        for sc, ev in zip(df["f_score"], df["f_evaluable"])
    ]
    return df


def _percentile(series: pd.Series, symbol: str) -> tuple[float | None, int, int]:
    """
    A symbol's percentile within `series`, with its rank and the number of
    peers that number is drawn from.

    Returns (percentile, rank_from_top, n_with_data).
    """
    valid = series.dropna()
    if symbol not in valid.index or len(valid) < 2:
        return None, 0, len(valid)
    pct = valid.rank(pct=True)[symbol]
    rank = int(valid.rank(ascending=False)[symbol])
    return float(pct), rank, len(valid)


def sector_neutral_scores(
    conn: sqlite3.Connection,
    knowledge_date: str | None = None,
) -> pd.DataFrame:
    """
    Each holding's percentile within its own peer group, by signal and by
    sleeve, plus the three composites.
    """
    metrics = universe_metrics(conn, knowledge_date)
    if metrics.empty:
        return pd.DataFrame()
    groups, _ = universe.verified_peers(conn)
    indexed = metrics.set_index("symbol")

    rows = []
    for holding, peers in groups.items():
        members = [s for s in [holding, *peers] if s in indexed.index]
        if holding not in members:
            continue
        frame = indexed.loc[members]
        row: dict = {
            "symbol": holding,
            "peers_with_data": len(members) - 1,
        }

        sleeve_values: dict[str, float | None] = {}
        for sleeve, signals in SLEEVES.items():
            parts = []
            for signal in signals:
                pct, rank, n = _percentile(frame[signal], holding)
                row[f"{signal}_pct"] = pct
                row[f"{signal}_rank"] = rank
                row[f"{signal}_n"] = n
                if pct is not None:
                    parts.append(pct)
            sleeve_values[sleeve] = sum(parts) / len(parts) if parts else None
            row[f"{sleeve}_score"] = sleeve_values[sleeve]
            row[f"{sleeve}_components"] = f"{len(parts)}/{len(signals)}"

        for name, needed in COMPOSITES.items():
            # Every named sleeve must exist; a missing one withholds the
            # composite rather than quietly reducing it to the others.
            if any(sleeve_values[s] is None for s in needed):
                row[name] = None
                row[f"{name}_missing"] = ", ".join(
                    s for s in needed if sleeve_values[s] is None)
            else:
                row[name] = sum(sleeve_values[s] for s in needed) / len(needed)
                row[f"{name}_missing"] = None
        rows.append(row)

    return pd.DataFrame(rows).sort_values("symbol").reset_index(drop=True)


def _fmt_pct(value) -> str:
    return "   --" if value is None or pd.isna(value) else f"{value * 100:4.0f}"


def main() -> None:
    conn = db.connect()
    try:
        scores = sector_neutral_scores(conn)
        if scores.empty:
            print("No peer groups with data. Run src/screener/universe.py and "
                  "src/screener/fetch_universe.py first.")
            return

        meta = db.read_table(conn, "security_meta").set_index("symbol")

        print("=" * 78)
        print("SECTOR-NEUTRAL PERCENTILES (100 = best in peer group)")
        print("=" * 78)
        print("Each holding is ranked only against its own verified peers.")
        print("Nothing below is comparable across rows.\n")

        header = (f"{'symbol':<9}{'peers':>6}{'B/M':>6}{'grossP':>8}"
                  f"{'F':>6}{'mom':>6}   rank in group")
        print(header)
        print("-" * 78)
        for _, r in scores.iterrows():
            detail = (f"B/M {r['book_to_market_rank']}/{r['book_to_market_n']}"
                      if r["book_to_market_rank"] else "B/M --")
            mom = (f"mom {r['momentum_12_1_rank']}/{r['momentum_12_1_n']}"
                   if r["momentum_12_1_rank"] else "mom --")
            print(f"{r['symbol']:<9}{r['peers_with_data']:>6}"
                  f"{_fmt_pct(r['book_to_market_pct']):>6}"
                  f"{_fmt_pct(r['gross_profitability_pct']):>8}"
                  f"{_fmt_pct(r['f_ratio_pct']):>6}"
                  f"{_fmt_pct(r['momentum_12_1_pct']):>6}"
                  f"   {detail}, {mom}")

        print("\n" + "=" * 78)
        print("COMPOSITES (mean of sleeve percentiles)")
        print("=" * 78)
        print(f"{'symbol':<9}{'value':>8}{'+quality':>10}{'+momentum':>11}"
              f"   quality sleeve")
        print("-" * 78)
        for _, r in scores.iterrows():
            print(f"{r['symbol']:<9}"
                  f"{_fmt_pct(r['value']):>8}"
                  f"{_fmt_pct(r['value+quality']):>10}"
                  f"{_fmt_pct(r['value+quality+momentum']):>11}"
                  f"   {r['quality_components']} components")

        withheld = scores[scores["value+quality"].isna()]
        if not withheld.empty:
            print("\n" + "-" * 78)
            print("COMPOSITE WITHHELD")
            print("-" * 78)
            for _, r in withheld.iterrows():
                name = meta.loc[r["symbol"], "short_name"] if r["symbol"] in meta.index else ""
                print(f"  {r['symbol']} ({str(name)[:30]}): "
                      f"no {r['value+quality_missing']} sleeve")
            print("\n  A 'value + quality' score computed without a quality")
            print("  measurement is the value score under another name, and")
            print("  would sit in the same column as composites that really do")
            print("  carry both. The value sleeve above is still real and still")
            print("  comparable to its peers; only the blends are withheld.")

        print("\n" + "-" * 78)
        print("Percentiles are within-group and small-sample: a group of 17")
        print("resolves to about 6 percentage points per place. Read the rank")
        print("positions rather than the decimals.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
