"""
Phase 4b: value, quality and momentum metrics on the names actually held.

Four metrics, each chosen for a reason rather than for being available:

**Gross profitability** -- (revenue - cost of revenue) / total assets.
Novy-Marx's argument is that the further down the income statement you
read, the more the number has been shaped by accounting choices:
depreciation schedules, tax strategy, one-off charges. Gross profit is the
cleanest available measure of what the business actually produces. Its
usefulness here is that it is *negatively* correlated with book-to-market,
so quality and value are additive rather than two labels for one bet.

**Piotroski F-Score** -- nine binary accounting tests across
profitability, leverage/liquidity and operating efficiency. Piotroski's
result was specifically that it works *within* the value universe: cheap
stocks are often cheap because they are dying, and the F-Score separates
those from the ones recovering.

**12-1 momentum** -- total return from twelve months ago to one month ago.
The skipped month is not a rounding convenience: short-horizon reversal is
a separate and opposite effect, and including the most recent month mixes
the two.

**Price-to-book, flagged below 1.0 for Tokyo listings.** In Japan this is
a catalyst rather than a valuation measure. The Tokyo Stock Exchange's
March 2023 reform formally asked companies trading below book to publish
capital-efficiency plans, which is why a sub-1.0 multiple is treated as a
separate flag for Tokyo names and not folded into generic cheapness.

Two rules govern every number below.

**No metric is scored when its inputs are missing.** A bank reports no
cost of revenue and no current/non-current balance-sheet split, because
neither concept applies to it. That leaves gross profitability undefined
and five of the nine F-Score tests unevaluable for 8306.T. The honest
output is a refusal, not a partial score: a 4-out-of-9 presented beside a
genuine 4-out-of-9 would read as a weak company rather than as an
unmeasured one. So every F-Score is reported as score-out-of-evaluable,
and the composite declines to rank a name whose metrics do not exist.

**Nothing is read before it was knowable.** All fundamentals come through
fundamentals.as_of(), which filters on the stored availability date, so a
screen run for a past date cannot see a filing that had not happened yet.

Run directly:
    python src/screener/quality.py
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data import fundamentals  # noqa: E402
from storage import db  # noqa: E402

# The nine F-Score tests, in Piotroski's three groups.
F_TESTS = [
    ("roa_positive", "profitability", "ROA > 0"),
    ("cfo_positive", "profitability", "operating cash flow > 0"),
    ("roa_improving", "profitability", "ROA higher than last year"),
    ("accruals", "profitability", "cash flow exceeds net income"),
    ("leverage_falling", "leverage", "long-term debt ratio fell"),
    ("liquidity_rising", "leverage", "current ratio rose"),
    ("no_dilution", "leverage", "no new shares issued"),
    ("margin_rising", "efficiency", "gross margin rose"),
    ("turnover_rising", "efficiency", "asset turnover rose"),
]

# Tokyo listings only: the TSE reform gives a sub-1.0 multiple a policy
# meaning that does not transfer to other markets.
TSE_PB_THRESHOLD = 1.0


@dataclass
class FScore:
    """
    A Piotroski score together with how many of its tests could be run.

    `score` is meaningless without `evaluable`. Reporting them separately
    is the whole point: 4 of 9 and 4 of 4 describe very different
    companies, and collapsing them to "4" would misrepresent a bank as a
    weak industrial.
    """
    tests: dict[str, bool | None] = field(default_factory=dict)

    @property
    def score(self) -> int:
        return sum(1 for v in self.tests.values() if v is True)

    @property
    def evaluable(self) -> int:
        return sum(1 for v in self.tests.values() if v is not None)

    @property
    def complete(self) -> bool:
        return self.evaluable == len(F_TESTS)

    @property
    def unevaluable(self) -> list[str]:
        return [k for k, v in self.tests.items() if v is None]

    def __str__(self) -> str:
        return f"{self.score}/{self.evaluable}"


def _pick(row: pd.Series | None, *items: str) -> float | None:
    """First present, non-null value among `items`; None if none exist."""
    if row is None:
        return None
    for item in items:
        if item in row.index:
            value = row[item]
            if pd.notna(value):
                return float(value)
    return None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    """Guarded division: None propagates, and a zero denominator is None."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _gt(a: float | None, b: float | None) -> bool | None:
    """Comparison that stays None when either side is unavailable."""
    if a is None or b is None:
        return None
    return a > b


def gross_profitability(row: pd.Series) -> float | None:
    """
    (Revenue - cost of revenue) / total assets.

    Uses the reported gross profit when present and falls back to the
    subtraction. Returns None for a company that reports neither, which
    is the correct answer for a bank rather than an error.
    """
    gross = _pick(row, "Gross Profit")
    if gross is None:
        revenue = _pick(row, "Total Revenue")
        cogs = _pick(row, "Cost Of Revenue")
        if revenue is None or cogs is None:
            return None
        gross = revenue - cogs
    return _ratio(gross, _pick(row, "Total Assets"))


def piotroski(
    current: pd.Series,
    prior: pd.Series | None,
    prior2: pd.Series | None,
) -> FScore:
    """
    The nine F-Score tests, each True, False, or None when unevaluable.

    ROA uses beginning-of-year total assets, as in the original paper,
    which is why the year-on-year ROA and turnover tests need three years
    of balance-sheet data rather than two.
    """
    ta_t1 = _pick(prior, "Total Assets")
    ta_t2 = _pick(prior2, "Total Assets")

    roa_t = _ratio(_pick(current, "Net Income"), ta_t1)
    roa_t1 = _ratio(_pick(prior, "Net Income"), ta_t2)
    cfo_t = _ratio(_pick(current, "Operating Cash Flow"), ta_t1)

    lever_t = _ratio(_pick(current, "Long Term Debt"), _pick(current, "Total Assets"))
    lever_t1 = _ratio(_pick(prior, "Long Term Debt"), ta_t1)

    liquid_t = _ratio(_pick(current, "Current Assets"),
                      _pick(current, "Current Liabilities"))
    liquid_t1 = _ratio(_pick(prior, "Current Assets"),
                       _pick(prior, "Current Liabilities"))

    shares_t = _pick(current, "Ordinary Shares Number", "Share Issued")
    shares_t1 = _pick(prior, "Ordinary Shares Number", "Share Issued")

    margin_t = _ratio(_pick(current, "Gross Profit"), _pick(current, "Total Revenue"))
    margin_t1 = _ratio(_pick(prior, "Gross Profit"), _pick(prior, "Total Revenue"))

    turn_t = _ratio(_pick(current, "Total Revenue"), ta_t1)
    turn_t1 = _ratio(_pick(prior, "Total Revenue"), ta_t2)

    tests: dict[str, bool | None] = {
        "roa_positive": _gt(roa_t, 0.0),
        "cfo_positive": _gt(cfo_t, 0.0),
        "roa_improving": _gt(roa_t, roa_t1),
        "accruals": _gt(cfo_t, roa_t),
        # The two leverage/liquidity directions are inverted: falling
        # debt and rising liquidity are the healthy signs.
        "leverage_falling": None if (lever_t is None or lever_t1 is None)
                            else lever_t < lever_t1,
        "liquidity_rising": _gt(liquid_t, liquid_t1),
        "no_dilution": None if (shares_t is None or shares_t1 is None)
                       else shares_t <= shares_t1,
        "margin_rising": _gt(margin_t, margin_t1),
        "turnover_rising": _gt(turn_t, turn_t1),
    }
    return FScore(tests=tests)


def is_financial(group: pd.DataFrame) -> bool:
    """
    True when a company reports neither a cost of revenue nor a
    current/non-current balance-sheet split, across every year available.

    This is a structural test, not a sector label, and it is deliberately
    the stronger of the two: a sector string can be missing or wrong,
    whereas the absence of both concepts is the balance sheet itself
    saying the company is a bank or insurer. It also generalises -- any
    issuer whose statements lack the inputs is caught for the same
    reason.
    """
    for item in ("Cost Of Revenue", "Current Assets"):
        if item in group.columns and group[item].notna().any():
            return False
    return True


def _anchor(group: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    """
    Restrict a symbol's fiscal years to those with a balance sheet, and
    report any newer income statement that is therefore being ignored.

    yfinance's statements are not always in sync: 5105.T has income and
    cash-flow statements through 2025 but a balance sheet only through
    2024. Scoring the newest income statement against the previous year's
    assets would invent a ratio out of two different vintages, and ROA
    already depends on beginning-of-year assets, so the whole metric set
    is anchored to the latest year with a complete balance sheet instead.
    The cost is a year of staleness, which is reported rather than hidden.
    """
    if "Total Assets" not in group.columns:
        return group.iloc[0:0], None
    complete = group[group["Total Assets"].notna()]
    if complete.empty:
        return complete, None
    latest_complete = complete["fiscal_date"].max()
    newer = group[group["fiscal_date"] > latest_complete]
    note = None
    if not newer.empty:
        note = (f"income statement runs to {newer['fiscal_date'].max()} but the "
                f"balance sheet stops at {latest_complete}; metrics use "
                f"{latest_complete}")
    return complete, note


def _price_asof(prices: pd.Series, target: pd.Timestamp) -> float | None:
    """
    Last close on or before `target`; None if the series starts later.

    Nulls are dropped before the selection rather than trusted to be
    absent. An unsettled session returns a dated row with no price, and
    taking the last row blindly turns that into a null that then
    propagates silently -- it cost every Japanese holding its value
    sleeve, and the output looked plausible enough to accept.
    """
    eligible = prices[prices.index <= target].dropna()
    if eligible.empty:
        return None
    return float(eligible.iloc[-1])


def momentum_12_1(
    prices: pd.Series,
    knowledge_date: pd.Timestamp,
) -> float | None:
    """
    Total return from twelve months before `knowledge_date` to one month
    before it, in the symbol's own local currency.

    Local currency, deliberately: momentum as the literature defines it is
    a local-market effect, and converting to base currency would fold the
    FX move into it. The consequence is that ranking a Tokyo name against
    a US name on this number compares two different currencies, which is
    what region-neutral ranking in the composite step exists to handle.
    """
    start = _price_asof(prices, knowledge_date - pd.DateOffset(months=12))
    end = _price_asof(prices, knowledge_date - pd.DateOffset(months=1))
    if start is None or end is None or start == 0:
        return None
    return end / start - 1.0


def price_to_book(
    prices: pd.Series,
    knowledge_date: pd.Timestamp,
    row: pd.Series,
) -> float | None:
    """
    Market capitalisation over book equity, both in local currency.

    Built from price x share count rather than taken from a quote
    service's own field, so the book value and the share count come from
    the same filing and the look-ahead filter applies to both.
    """
    price = _price_asof(prices, knowledge_date)
    shares = _pick(row, "Ordinary Shares Number", "Share Issued")
    equity = _pick(row, "Stockholders Equity")
    if price is None or shares is None:
        return None
    return _ratio(price * shares, equity)


def _price_series(
    conn: sqlite3.Connection,
    table: str = "holding_prices",
) -> dict[str, pd.Series]:
    """
    Local-currency close history per symbol.

    The table is a parameter because the screen runs against two
    different universes: holding_prices covers the account's own
    positions, screen_prices covers those plus their verified peers.
    """
    df = db.read_table(conn, table)
    out: dict[str, pd.Series] = {}
    if df.empty:
        return out
    for symbol, group in df.groupby("symbol"):
        series = group.set_index("date")["close"].astype(float)
        series.index = pd.to_datetime(series.index)
        out[symbol] = series.sort_index()
    return out


def screen(
    conn: sqlite3.Connection,
    knowledge_date: str | None = None,
    symbols: list[str] | None = None,
    price_table: str = "holding_prices",
) -> pd.DataFrame:
    """
    Run all four metrics for every symbol with fundamentals available as
    of `knowledge_date` (default: today).

    One row per symbol, carrying the fiscal year the fundamentals came
    from, so the vintage of the data is never implicit.
    """
    knowledge = pd.Timestamp(knowledge_date) if knowledge_date else pd.Timestamp.today().normalize()
    wide = fundamentals.as_of(conn, knowledge.strftime("%Y-%m-%d"), symbols)
    if wide.empty:
        return pd.DataFrame()

    prices = _price_series(conn, price_table)
    rows = []
    for symbol, group in wide.groupby("symbol"):
        group = group.sort_values("fiscal_date")
        financial = is_financial(group)
        complete, vintage_note = _anchor(group)
        if complete.empty:
            continue

        current = complete.iloc[-1]
        prior = complete.iloc[-2] if len(complete) >= 2 else None
        prior2 = complete.iloc[-3] if len(complete) >= 3 else None

        # Piotroski's own sample excludes financial firms, and for good
        # reason beyond the missing line items: a bank's operating cash
        # flow is dominated by changes in loans and deposits, so the
        # cash-flow and accruals tests would score balance-sheet growth
        # rather than earnings quality. 8306.T reports operating cash
        # flow of about -23 trillion yen, which says nothing at all about
        # whether the bank is healthy. Computing seven of nine tests for
        # it would produce a number that looks like a quality score and
        # is not one, so the whole score is withheld.
        if financial:
            f = FScore(tests={name: None for name, _, _ in F_TESTS})
            reason = ("financial issuer: reports no cost of revenue and no "
                      "current/non-current split, and its operating cash flow "
                      "tracks loan and deposit flows rather than earnings")
        else:
            f = piotroski(current, prior, prior2)
            reason = None

        series = prices.get(symbol, pd.Series(dtype=float))
        pb = price_to_book(series, knowledge, current)

        rows.append({
            "symbol": symbol,
            "fiscal_date": current["fiscal_date"],
            "years_available": len(complete),
            "is_financial": financial,
            "not_applicable_reason": reason,
            "vintage_note": vintage_note,
            "gross_profitability": gross_profitability(current),
            "f_score": f.score,
            "f_evaluable": f.evaluable,
            "f_complete": f.complete,
            "f_unevaluable": ", ".join(f.unevaluable),
            "momentum_12_1": momentum_12_1(series, knowledge),
            "price_to_book": pb,
            "tse_reform_flag": bool(
                symbol.upper().endswith(".T") and pb is not None
                and pb < TSE_PB_THRESHOLD
            ),
        })

    out = pd.DataFrame(rows)
    return out.sort_values("symbol").reset_index(drop=True)


def main() -> None:
    conn = db.connect()
    try:
        knowledge = pd.Timestamp.today().normalize()
        result = screen(conn)
        if result.empty:
            print("No fundamentals stored. Run src/data/fundamentals.py first.")
            return

        print("=" * 76)
        print(f"SCREEN AS OF {knowledge.date()}")
        print("=" * 76)
        print("Fundamentals are filtered on their assumed availability date")
        print(f"(fiscal year-end + {fundamentals.REPORTING_LAG_DAYS} days), not on "
              f"fiscal year-end.\n")

        show = result[[
            "symbol", "fiscal_date", "gross_profitability", "f_score",
            "f_evaluable", "momentum_12_1", "price_to_book", "tse_reform_flag",
        ]].copy()
        show["gross_profitability"] = show["gross_profitability"].map(
            lambda v: "   --" if pd.isna(v) else f"{v:5.3f}")
        show["momentum_12_1"] = show["momentum_12_1"].map(
            lambda v: "     --" if pd.isna(v) else f"{v * 100:+6.1f}%")
        show["price_to_book"] = show["price_to_book"].map(
            lambda v: "  --" if pd.isna(v) else f"{v:4.2f}")
        show["f"] = [
            "  n/a" if ev == 0 else f"{sc}/{ev}"
            for sc, ev in zip(show["f_score"], show["f_evaluable"])
        ]
        show = show.drop(columns=["f_score", "f_evaluable"])
        show = show[["symbol", "fiscal_date", "gross_profitability", "f",
                     "momentum_12_1", "price_to_book", "tse_reform_flag"]]
        show.columns = ["symbol", "fiscal yr", "gross prof.", "F-score",
                        "12-1 mom.", "P/B", "TSE <1"]
        print(show.to_string(index=False))

        withheld = result[result["is_financial"]]
        if not withheld.empty:
            print("\n" + "-" * 76)
            print("SCORE WITHHELD -- not applicable, which is not the same as low")
            print("-" * 76)
            for _, row in withheld.iterrows():
                print(f"  {row['symbol']}: {row['not_applicable_reason']}")
            print("\n  Piotroski's own sample excludes financial firms. Two of the")
            print("  nine tests are unavailable outright for a bank, but the")
            print("  stronger objection is that two more would be actively")
            print("  misleading: operating cash flow for a bank tracks loan and")
            print("  deposit flows, so the cash-flow and accruals tests would")
            print("  measure balance-sheet growth and call it earnings quality.")
            print("  Seven of nine tests is computable here and still wrong, so")
            print("  the score is withheld rather than caveated. A bank needs")
            print("  residual income, not a DCF and not an F-Score -- which is")
            print("  what the sizing phase already plans for it.")

        partial = result[(~result["is_financial"]) & (~result["f_complete"])]
        if not partial.empty:
            print("\n" + "-" * 76)
            print("PARTIALLY SCOREABLE -- read as unmeasured, not as weak")
            print("-" * 76)
            for _, row in partial.iterrows():
                print(f"  {row['symbol']}: {row['f_score']} of "
                      f"{row['f_evaluable']} tests evaluable")
                print(f"     unavailable: {row['f_unevaluable']}")
            print("\n  These are genuine reporting gaps rather than conceptual")
            print("  ones. yfinance omits the long-term-debt row entirely when a")
            print("  company carries none, and that is distinguishable from a")
            print("  real zero: 4180.T reports an explicit 0 for 2024 and a")
            print("  positive figure for 2025, so an absent row means not")
            print("  reported and is left unscored rather than read as zero.")

        stale = result[result["vintage_note"].notna()]
        if not stale.empty:
            print("\n" + "-" * 76)
            print("DATA VINTAGE")
            print("-" * 76)
            for _, row in stale.iterrows():
                print(f"  {row['symbol']}: {row['vintage_note']}")
            print("\n  Metrics are anchored to the latest fiscal year with a")
            print("  complete balance sheet. Pairing a newer income statement")
            print("  with older assets would produce a ratio from two different")
            print("  vintages, and ROA already depends on beginning-of-year")
            print("  assets, so consistency matters more than freshness here.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
