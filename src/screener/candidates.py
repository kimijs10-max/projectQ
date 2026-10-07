"""
Screening the universe on intrinsic value, and the portfolio it implies.

Phase 4 valued the five holdings. This runs the same two models -- DCF on
owner cash flow, residual income for banks -- over every verified name in
the screening universe, ranks them by discount to value, and applies the
Phase 4 sizing rule to what survives.

Three things separate this from "sort by cheapest".

**A value is only compared with a price in the same currency.** A
US-listed ADR trades in dollars and reports in its home currency; TSM's
statements are in Taiwan dollars and one ADR is five ordinary shares. A
per-share value built from those statements is not comparable with the
dollar price, and would show the company as absurdly cheap or expensive
depending on the exchange rate. Any name whose reporting currency is not
its trading currency is withheld.

**Cheap for a bad reason is filtered, and reported.** Four rules, each in
config/valuation.py with the failure it guards against: enough history, a
discount that survives the bear case, cash flow that is not a cyclical
peak, and a discount that is not too large to believe. A name removed by
a rule is listed under that rule. Nothing is dropped silently, because
the list of what was excluded and why is as informative as the list of
what passed.

**The signal is tested before it is trusted.** The same valuation is run
as of two past dates, using only fundamentals and prices available then,
and its ranking is compared with each stock's return over the following
year relative to its own peer group's median -- the Phase 4d test. A
screen that cannot rank last year's stocks has no claim on this year's.

What this is not: a universe of 132 names in six sectors, chosen as peers
of the existing holdings, is not the market. The output is a list of
names worth researching and a model portfolio that follows one rule. It
is not a recommendation, and nothing here places an order.

Run directly:
    python src/screener/candidates.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config"))

import valuation as cfg  # noqa: E402
from screener import universe, validate  # noqa: E402
from sizing import intrinsic  # noqa: E402
from storage import db  # noqa: E402


# --------------------------------------------------------------------------
# Rules. Pure functions -- tested in tests/test_candidates.py.
# --------------------------------------------------------------------------

def exclusion_reason(row: pd.Series | dict) -> str | None:
    """
    Why a valued name is not a candidate, or None if it is one.

    Rules are checked in a fixed order and the first failure is the
    reason, so each name appears under exactly one heading.
    """
    ratio = row["price_to_value"]
    if pd.isna(ratio) or ratio >= 1:
        return "not below base-case value"
    if 1 - ratio < cfg.MIN_MARGIN_OF_SAFETY:
        return f"margin of safety under {cfg.MIN_MARGIN_OF_SAFETY * 100:.0f}%"
    if row["model"] == "dcf":
        if row["margin_years"] < cfg.SCREEN_MIN_YEARS:
            return f"fewer than {cfg.SCREEN_MIN_YEARS} years of cash-flow history"
        if row["margin_min"] < cfg.SCREEN_MARGIN_STABILITY * row["owner_cf_margin"]:
            return "cash flow unstable: worst year far below the normalised level"
    bear = row["price_to_value_bear"]
    if pd.isna(bear) or 1 - bear < cfg.MIN_MARGIN_OF_SAFETY:
        return "discount does not survive the bear case"
    if ratio < cfg.SCREEN_MIN_PRICE_TO_VALUE:
        return "discount too large to take at face value: check the data"
    return None


def suggest_weights(candidates: pd.DataFrame) -> pd.Series:
    """
    Target weights for the candidates, as fractions of the portfolio.

    Each name starts from the Phase 4 rule (a fraction of its own margin
    of safety, capped per name). A peer group over MAX_GROUP_WEIGHT is
    scaled back pro rata, and if the total exceeds 100% everything is
    scaled to fit. Whatever is left unallocated is cash: the rule never
    grosses a short list up to fill the portfolio.
    """
    if candidates.empty:
        return pd.Series(dtype=float)
    weights = candidates.set_index("symbol")["margin_of_safety"].map(intrinsic.target_weight)
    groups = candidates.set_index("symbol")["group"]
    for _, members in groups.groupby(groups):
        total = weights[members.index].sum()
        if total > cfg.MAX_GROUP_WEIGHT:
            weights[members.index] *= cfg.MAX_GROUP_WEIGHT / total
    if weights.sum() > 1:
        weights /= weights.sum()
    return weights


# --------------------------------------------------------------------------
# The screen.
# --------------------------------------------------------------------------

def _universe(conn: sqlite3.Connection) -> pd.DataFrame:
    """symbol, group (the holding whose peer set it belongs to), name, currencies."""
    groups, _ = universe.verified_peers(conn)
    meta = db.read_table(conn, "security_meta").set_index("symbol")
    rows, seen = [], set()
    for holding, peers in groups.items():
        label = f"{meta.loc[holding, 'industry']}"
        for symbol in [holding, *peers]:
            if symbol in seen or symbol not in meta.index:
                continue
            seen.add(symbol)
            rows.append({
                "symbol": symbol,
                "group": holding,
                "group_label": label,
                "name": meta.loc[symbol, "short_name"],
                "currency": meta.loc[symbol, "currency"],
                "financial_currency": meta.loc[symbol, "financial_currency"],
            })
    return pd.DataFrame(rows)


def screen(conn: sqlite3.Connection, knowledge_date: str | None = None) -> pd.DataFrame:
    """
    Every universe name with its valuation and its status: one of
    "candidate", "excluded" (valued, but fails a rule) or "withheld" (no
    valuation), with the reason in `reason`.
    """
    names = _universe(conn)
    if names.empty:
        return names
    comparable = names[names["currency"] == names["financial_currency"]]
    valuations = intrinsic.value_symbols(
        conn, dict(zip(comparable["symbol"], comparable["currency"])),
        knowledge_date, price_table="screen_prices",
    )
    by_symbol = {v.symbol: v for v in valuations}

    rows = []
    for n in names.itertuples(index=False):
        row = {**n._asdict(), "model": None, "price": np.nan, "price_to_value": np.nan,
               "price_to_value_bear": np.nan, "margin_of_safety": np.nan,
               "implied": np.nan, "delivered": np.nan, "status": "withheld", "reason": None}
        v = by_symbol.get(n.symbol)
        if n.currency != n.financial_currency:
            reported = n.financial_currency if isinstance(n.financial_currency, str) else "unknown"
            row["reason"] = f"reports in {reported}, trades in {n.currency}"
        elif v is None:
            row["reason"] = "no price history"
        elif v.withheld:
            row["reason"] = v.withheld.split(" (")[0].split(" -- ")[0]
        else:
            row.update({
                "model": v.model, "price": v.price,
                "price_to_value": v.price_to_value("base"),
                "price_to_value_bear": v.price_to_value("bear"),
                "margin_of_safety": v.margin_of_safety("base"),
                "implied": v.implied, "delivered": v.delivered,
                "owner_cf_margin": v.inputs.get("owner_cf_margin", np.nan),
                "margin_min": v.inputs.get("margin_min", np.nan),
                "margin_years": v.inputs.get("margin_years", np.nan),
            })
            row["reason"] = exclusion_reason(row)
            row["status"] = "candidate" if row["reason"] is None else "excluded"
        rows.append(row)
    return pd.DataFrame(rows)


def validation(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Did the valuation rank stocks correctly in the past?

    For each knowledge date in validate.KNOWLEDGE_DATES: value the
    universe as of that date, and rank-correlate value/price with the
    following year's return relative to the peer group's median. Also
    reports how the names that would have been candidates then did
    against everything else that was valued.
    """
    rows = []
    for kd in validate.KNOWLEDGE_DATES:
        past = screen(conn, kd)
        valued = past[past["status"] != "withheld"].copy()
        if valued.empty:
            continue
        forward = validate.forward_returns(conn, kd)
        valued["forward"] = valued["symbol"].map(forward)
        valued = valued.dropna(subset=["forward"])
        # Relative to the group median, for the reason Phase 4c made
        # unavoidable: over a year, sector moves swamp stock selection.
        all_names = past.assign(forward=past["symbol"].map(forward)).dropna(subset=["forward"])
        medians = all_names.groupby("group")["forward"].median()
        valued["excess"] = valued["forward"] - valued["group"].map(medians)
        valued["value_to_price"] = 1 / valued["price_to_value"]

        rho, p, n = validate.spearman(valued["value_to_price"], valued["excess"])
        cheap = valued[valued["price_to_value"] < 1 - cfg.MIN_MARGIN_OF_SAFETY]
        rest = valued[valued["price_to_value"] >= 1 - cfg.MIN_MARGIN_OF_SAFETY]
        passed = valued[valued["status"] == "candidate"]
        rows.append({
            "knowledge_date": kd, "n": n, "spearman": rho, "p_value": p,
            "n_cheap": len(cheap), "cheap_excess": cheap["excess"].median(),
            "n_rest": len(rest), "rest_excess": rest["excess"].median(),
            "n_candidates": len(passed), "candidate_excess": passed["excess"].median(),
            "candidate_beat_median": float((passed["excess"] > 0).mean()) if len(passed) else np.nan,
        })
    return pd.DataFrame(rows)


def current_book(conn: sqlite3.Connection) -> dict[str, float]:
    """Today's holdings as fractions of the invested book."""
    return intrinsic.current_weights(conn)


# --------------------------------------------------------------------------
# Report.
# --------------------------------------------------------------------------

def _pct(x, width: int = 8, signed: bool = False) -> str:
    if x is None or pd.isna(x):
        return "--".rjust(width)
    return f"{x * 100:{'+' if signed else ''}.1f}%".rjust(width)


def _assumes(row) -> str:
    if pd.isna(row.implied):
        return "--"
    if row.model == "dcf":
        return f"{row.implied * 100:+.0f}% growth vs {row.delivered * 100:+.0f}%"
    return f"{row.implied * 100:.0f}% ROE vs {row.delivered * 100:.0f}%"


def main() -> None:
    conn = db.connect()
    try:
        result = screen(conn)
        if result.empty:
            print("No verified universe. Run src/screener/universe.py and fetch_universe.py.")
            return

        print("=" * 96)
        print("INTRINSIC-VALUE SCREEN")
        print("=" * 96)
        counts = result["status"].value_counts()
        print(f"{len(result)} names in the universe: {counts.get('candidate', 0)} candidates, "
              f"{counts.get('excluded', 0)} valued but excluded, "
              f"{counts.get('withheld', 0)} with no valuation.")
        print("The universe is the holdings' sector peers, not the market.\n")

        candidates = result[result["status"] == "candidate"].sort_values("price_to_value")
        weights = suggest_weights(candidates)
        held = current_book(conn)

        print("-" * 96)
        print("CANDIDATES -- below value on the base case, and passing every rule")
        print("-" * 96)
        print(f"{'symbol':<9}{'name':<26}{'group':<22}{'model':<8}{'price/value':>12}"
              f"{'bear':>7}   price assumes vs delivered")
        for r in candidates.itertuples(index=False):
            mark = "  (held)" if r.symbol in held else ""
            print(f"{r.symbol:<9}{str(r.name)[:24]:<26}{r.group_label[:20]:<22}"
                  f"{'DCF' if r.model == 'dcf' else 'RI':<8}{r.price_to_value:>11.2f}x"
                  f"{r.price_to_value_bear:>6.2f}x   {_assumes(r)}{mark}")

        print("\n" + "-" * 96)
        print("BELOW VALUE BUT EXCLUDED -- cheap on paper, removed by a rule")
        print("-" * 96)
        cheap = result[(result["status"] == "excluded") & (result["price_to_value"] < 1)
                       & (result["reason"] != "not below base-case value")]
        for reason, group in cheap.groupby("reason"):
            print(f"\n  {reason}:")
            for r in group.sort_values("price_to_value").itertuples(index=False):
                print(f"    {r.symbol:<9}{str(r.name)[:24]:<26}{r.group_label[:20]:<22}"
                      f"{r.price_to_value:>6.2f}x")

        above = result[(result["status"] == "excluded") & (result["price_to_value"] >= 1)]
        print(f"\n  {len(above)} more are valued at or above their price.")

        print("\n" + "-" * 96)
        print("NO VALUATION")
        print("-" * 96)
        for reason, group in result[result["status"] == "withheld"].groupby("reason"):
            names = ", ".join(sorted(group["symbol"]))
            print(f"  {len(group):>3}  {reason}")
            print(f"       {names[:86]}{'...' if len(names) > 86 else ''}")

        print("\n" + "-" * 96)
        print("PORTFOLIO THE RULE IMPLIES, against the book held today")
        print("-" * 96)
        print(f"  {cfg.SIZING_FRACTION:.2f} x margin of safety, at most {cfg.MAX_WEIGHT * 100:.0f}% "
              f"a name and {cfg.MAX_GROUP_WEIGHT * 100:.0f}% a peer group; remainder in cash.\n")
        print(f"{'symbol':<9}{'name':<26}{'group':<22}{'held':>8}{'suggested':>11}{'change':>9}")
        names = result.set_index("symbol")
        for symbol in sorted(set(weights.index) | set(held),
                             key=lambda s: -(weights.get(s, 0.0))):
            w_now, w_new = held.get(symbol, 0.0), float(weights.get(symbol, 0.0))
            name = names.loc[symbol, "name"] if symbol in names.index else ""
            label = names.loc[symbol, "group_label"] if symbol in names.index else ""
            print(f"{symbol:<9}{str(name)[:24]:<26}{str(label)[:20]:<22}{_pct(w_now)}"
                  f"{_pct(w_new, 11)}{_pct(w_new - w_now, 9, signed=True)}")
        print(f"{'cash':<57}{'':>8}{_pct(max(0.0, 1 - weights.sum()), 11)}")
        by_group = weights.groupby(candidates.set_index("symbol")["group_label"]).sum()
        print("\n  by peer group: " + ", ".join(f"{g} {w * 100:.0f}%" for g, w in
                                                by_group.sort_values(ascending=False).items()))
        print("  'held' is each stock's share of the invested book, cash excluded.")

        print("\n" + "-" * 96)
        print("HAS THIS RANKING WORKED? Valued as of a past date, tested on the next 12 months")
        print("-" * 96)
        check = validation(conn)
        if check.empty:
            print("  Not enough history to test.")
        else:
            print(f"{'as of':<12}{'names':>6}{'rank corr.':>12}{'p':>7}   "
                  f"median return vs. peer group, next 12 months")
            for r in check.itertuples(index=False):
                print(f"{r.knowledge_date:<12}{r.n:>6}{r.spearman:>+12.3f}{r.p_value:>7.2f}   "
                      f"cheap ({r.n_cheap}) {_pct(r.cheap_excess, 7, True)}, "
                      f"rest ({r.n_rest}) {_pct(r.rest_excess, 7, True)}, "
                      f"would-be candidates ({r.n_candidates}) "
                      f"{_pct(r.candidate_excess, 7, True)}")
            print("\n  rank corr.: Spearman between value/price and the forward return relative to")
            print("  the peer group's median. 'cheap' is everything at least "
                  f"{cfg.MIN_MARGIN_OF_SAFETY * 100:.0f}% below value before the")
            print("  other rules. Two overlapping-regime windows, survivors only, p-values")
            print("  assume independent names: a demonstration, not proof (see NOTES.md, Phase 4d).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
