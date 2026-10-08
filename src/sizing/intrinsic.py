"""
Phase 4, final piece: intrinsic value and margin-of-safety sizing.

Firm-foundation theory says a share is worth the present value of the
cash it will return to its owners, and that the time to buy is when the
price is below that. This module puts a number on both halves for the
names actually held, then asks what the book would look like if position
size followed the discount.

**Two models, because one does not fit a bank.**

*Discounted cash flow* for non-financials. The cash flow is owner cash
flow: operating cash flow, minus capital expenditure, minus stock-based
compensation. The last term matters. Operating cash flow adds SBC back
because no cash left the building, but the owners paid for it in dilution,
and for a software company it can be a third of the reported figure.
Because operating cash flow is already after interest, the result is a
flow to equity, discounted at the cost of equity, giving equity value
directly. It ignores net borrowing and gives no separate credit for cash
on the balance sheet.

*Residual income* for banks. A bank's operating cash flow is not a
measure of what it earns -- deposits and loans are the product, so the
line tracks balance-sheet growth (8306.T reports +13tn and -23tn yen in
different years on steadily rising profits). A bank is valued instead as
its book value plus the present value of the profit it earns above its
cost of equity. A bank earning exactly its cost of equity is worth book.

**The price's own forecast is the headline, not mine.** A DCF on a fast
grower is mostly its growth assumption, and any intrinsic value printed
here inherits that. So each valuation is also run backwards: solve for the
growth rate (or, for a bank, the return on equity) that makes the model
equal today's price, and set it beside what the company has delivered.
"The price needs 35% growth fading over ten years" is a statement about
the market that can be checked; "it is worth $140" is a statement about
config/valuation.py.

**A value is withheld when the method does not apply.** A company whose
owner cash flow is negative has no DCF value under this model -- not a
value of zero. It gets no target weight and no claim that it is
overpriced, the same rule the screener applies to the F-Score for banks.

Fundamentals are read through fundamentals.as_of(), so nothing is used
before its assumed availability date.

Run directly:
    python src/sizing/intrinsic.py
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

import valuation as cfg  # noqa: E402
from data import fundamentals  # noqa: E402
from screener.quality import is_financial  # noqa: E402
from storage import db  # noqa: E402

SCENARIOS = ("bear", "base", "bull")

# Weekly returns for beta: Tokyo and New York do not close at the same
# time, and although each stock is regressed on its own market here, daily
# data still carries more microstructure noise than a two-year beta needs.
BETA_FREQ = "W-FRI"
MIN_BETA_OBS = 52
# Weeks of returns behind a universe beta; matches the two years the
# holdings' own tables hold.
BETA_WEEKS = 104


# --------------------------------------------------------------------------
# Valuation maths. Pure functions of numbers, no database -- tested in
# tests/test_intrinsic.py against closed-form answers.
# --------------------------------------------------------------------------

def dcf_value(
    cash_flow: float,
    g_start: float,
    g_terminal: float,
    r: float,
    years: int = cfg.HORIZON_YEARS,
) -> tuple[float, float]:
    """
    Present value of a cash flow whose growth fades linearly from
    `g_start` in year 1 to `g_terminal` in year `years`, then grows at
    `g_terminal` for ever.

    Returns (value, share of that value coming from the terminal period).
    The second number is the honest one: when it is 0.8, four fifths of
    the answer rests on years nobody has forecast.
    """
    if r <= g_terminal:
        raise ValueError("discount rate must exceed terminal growth")
    growth = np.linspace(g_start, g_terminal, years)
    flows = cash_flow * np.cumprod(1 + growth)
    discount = (1 + r) ** np.arange(1, years + 1)
    explicit = float((flows / discount).sum())
    terminal = float(flows[-1] * (1 + g_terminal) / (r - g_terminal) / discount[-1])
    value = explicit + terminal
    return value, (terminal / value if value else float("nan"))


def dcf_value_path(
    revenue: float,
    g_start: float,
    g_terminal: float,
    r: float,
    margin_start: float,
    margin_end: float,
    years: int = cfg.HORIZON_YEARS,
) -> tuple[float, float]:
    """
    dcf_value() with the margin allowed to move: revenue grows at a rate
    fading from `g_start` to `g_terminal`, and the owner-cash-flow margin
    moves linearly from `margin_start` today to `margin_end` in the final
    year, where it stays.

    With margin_start == margin_end this is exactly dcf_value() on
    margin * revenue. It exists for the two cases a constant margin
    cannot express: a business whose margin is expected to rise, and one
    that is investing more than it earns today -- early cash flows may be
    negative, and are discounted as such.
    """
    if r <= g_terminal:
        raise ValueError("discount rate must exceed terminal growth")
    growth = np.linspace(g_start, g_terminal, years)
    revenues = revenue * np.cumprod(1 + growth)
    steps = np.arange(1, years + 1) / years
    margins = margin_start + (margin_end - margin_start) * steps
    flows = revenues * margins
    discount = (1 + r) ** np.arange(1, years + 1)
    explicit = float((flows / discount).sum())
    terminal = float(flows[-1] * (1 + g_terminal) / (r - g_terminal) / discount[-1])
    value = explicit + terminal
    return value, (terminal / value if value else float("nan"))


def solve_increasing(fn, target: float, lo: float = -0.50, hi: float = 3.00) -> float | None:
    """The x in [lo, hi] at which an increasing fn(x) equals `target`, by bisection."""
    if fn(lo) > target or fn(hi) < target:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if fn(mid) > target:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def implied_growth(
    target_value: float,
    cash_flow: float,
    g_terminal: float,
    r: float,
    years: int = cfg.HORIZON_YEARS,
    lo: float = -0.50,
    hi: float = 3.00,
) -> float | None:
    """
    The starting growth rate at which dcf_value() equals `target_value`.

    Value rises monotonically with starting growth, so bisection is
    enough. None when the cash flow is not positive (no growth rate can
    rescue a negative base) or the answer lies outside [lo, hi].
    """
    if cash_flow <= 0 or target_value <= 0:
        return None

    def gap(g: float) -> float:
        return dcf_value(cash_flow, g, g_terminal, r, years)[0] - target_value

    if gap(lo) > 0 or gap(hi) < 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if gap(mid) > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def residual_income_value(
    book: float,
    roe_start: float,
    r: float,
    payout: float,
    fade_years: int,
) -> float:
    """
    Book value plus the present value of profits above the cost of equity.

    Return on equity fades linearly from `roe_start` to `r`, reaching it
    in year `fade_years`; from then on residual income is zero, so there
    is no terminal value to argue about. Book grows by retained earnings,
    (1 - payout) of each year's profit.
    """
    value = book
    for t in range(1, fade_years + 1):
        roe = roe_start + (r - roe_start) * t / fade_years
        value += (roe - r) * book / (1 + r) ** t
        book += roe * book * (1 - payout)
    return value


def implied_roe(price_to_book: float, r: float, g: float) -> float:
    """
    The return on equity a price-to-book multiple assumes is permanent.

    In steady state P/B = (ROE - g) / (r - g), so ROE = g + P/B * (r - g).
    At P/B = 1 this returns r: a bank priced at book is being priced to
    earn its cost of equity and no more.
    """
    return g + price_to_book * (r - g)


def adjusted_beta(raw: float) -> float:
    """Blume-adjusted beta: shrunk toward 1.0 by cfg.BETA_SHRINK."""
    return cfg.BETA_SHRINK * raw + (1 - cfg.BETA_SHRINK) * 1.0


def cost_of_equity(currency: str, beta: float) -> float:
    """CAPM in the stock's own currency: risk-free + beta * premium."""
    return cfg.RISK_FREE[currency] + beta * cfg.EQUITY_RISK_PREMIUM


def target_weight(margin_of_safety: float | None) -> float | None:
    """
    Position size from the discount to intrinsic value.

    None (not zero) when there is no valuation to size from. Zero below
    the minimum margin; above it, a fixed fraction of the margin, capped.
    The size depends only on the stock's own discount, not on how many
    other names qualify -- a book with one cheap stock should hold a
    measured position and cash, not everything in the one stock.
    """
    if margin_of_safety is None:
        return None
    if margin_of_safety < cfg.MIN_MARGIN_OF_SAFETY:
        return 0.0
    return min(cfg.MAX_WEIGHT, cfg.SIZING_FRACTION * margin_of_safety)


# --------------------------------------------------------------------------
# Inputs from the database.
# --------------------------------------------------------------------------

@dataclass
class Valuation:
    symbol: str
    currency: str
    price: float
    price_date: str
    beta_raw: float | None = None
    beta_obs: int = 0
    discount_rate: float | None = None
    model: str | None = None            # "dcf" | "residual income"
    values: dict[str, float] = field(default_factory=dict)  # per share
    inputs: dict[str, float] = field(default_factory=dict)
    implied: float | None = None        # what the price assumes
    delivered: float | None = None      # what the company has done
    implied_label: str = ""
    terminal_share: float | None = None
    withheld: str | None = None
    notes: list[str] = field(default_factory=list)

    def price_to_value(self, scenario: str = "base") -> float | None:
        value = self.values.get(scenario)
        if value is None or value <= 0:
            return None
        return self.price / value

    def margin_of_safety(self, scenario: str = "base") -> float | None:
        """(value - price) / value, i.e. 1 - price/value."""
        ratio = self.price_to_value(scenario)
        return None if ratio is None else 1 - ratio


def _latest(group: pd.DataFrame, *items: str) -> tuple[float | None, str | None]:
    """
    Most recent non-null value among `items`, with its fiscal date.

    Read per item rather than per row because yfinance's statements are
    not always in sync (5105.T's balance sheet stops a year before its
    income statement). A share count a year old is close enough to
    divide by; the vintage is returned so the caller can say so.
    """
    for item in items:
        if item not in group.columns:
            continue
        series = group.set_index("fiscal_date")[item].dropna()
        if not series.empty:
            return float(series.iloc[-1]), str(series.index[-1])
    return None, None


def _weekly_returns(series: pd.Series) -> pd.Series:
    return series.resample(BETA_FREQ).last().pct_change()


def local_betas(
    conn: sqlite3.Connection,
    currencies: dict[str, str],
    price_table: str = "holding_prices",
    knowledge_date: str | None = None,
) -> dict[str, tuple[float, int]]:
    """
    Raw beta of each symbol to its own market's benchmark, both in local
    currency, on weekly returns. Returns {symbol: (beta, observations)}.

    For the holdings this reads the two-year tables Phase 3 uses. For the
    screening universe (`price_table="screen_prices"`) it pairs the
    five-year screen prices with the long benchmark history in
    scenario_prices, cut off at `knowledge_date` and limited to the last
    BETA_WEEKS weeks before it, so a valuation as of a past date uses a
    beta that could have been measured then.
    """
    prices = db.read_table(conn, price_table)
    if price_table == "holding_prices":
        bench = db.read_table(conn, "benchmark_prices")
    else:
        bench = db.read_table(conn, "scenario_prices").rename(columns={"series": "ticker"})
    prices["date"] = pd.to_datetime(prices["date"])
    bench["date"] = pd.to_datetime(bench["date"])
    if knowledge_date is not None:
        prices = prices[prices["date"] <= knowledge_date]
        bench = bench[bench["date"] <= knowledge_date]
    by_symbol = {symbol: g.set_index("date")["close"] for symbol, g in prices.groupby("symbol")}
    by_ticker = {ticker: g.set_index("date")["close"] for ticker, g in bench.groupby("ticker")}

    out: dict[str, tuple[float, int]] = {}
    for symbol, currency in currencies.items():
        ticker = cfg.BETA_BENCHMARK.get(currency)
        if symbol not in by_symbol or ticker not in by_ticker:
            continue
        p, b = by_symbol[symbol], by_ticker[ticker]
        # Align on days both traded before resampling, so a holiday in
        # one market cannot pair this week's stock with last week's index.
        joined = pd.concat([p.rename("p"), b.rename("b")], axis=1, sort=True).dropna()
        if joined.empty:
            continue
        rets = joined.apply(_weekly_returns).dropna()
        if price_table != "holding_prices":
            rets = rets.tail(BETA_WEEKS)
        if len(rets) < MIN_BETA_OBS or rets["b"].var() == 0:
            continue
        beta = float(rets.cov().loc["p", "b"] / rets["b"].var())
        out[symbol] = (beta, len(rets))
    return out


def _value_dcf(v: Valuation, group: pd.DataFrame, shares: float) -> None:
    """Fill `v` with a DCF valuation, or the reason there is not one."""
    needed = ["Total Revenue", "Operating Cash Flow", "Capital Expenditure"]
    if any(item not in group.columns for item in needed):
        v.withheld = "revenue, operating cash flow or capex not reported"
        return
    years = group.dropna(subset=needed).copy()
    if len(years) < 2:
        v.withheld = "fewer than two fiscal years of cash-flow data"
        return

    if "Stock Based Compensation" in years.columns and years["Stock Based Compensation"].notna().any():
        sbc = years["Stock Based Compensation"].fillna(0.0)
        sbc_share = float(sbc.iloc[-1] / years["Operating Cash Flow"].iloc[-1])
    else:
        sbc = 0.0
        sbc_share = float("nan")
        v.notes.append("no stock-based compensation reported; none deducted")

    # Capex is stored as a negative outflow, so it is added.
    years["owner_cf"] = years["Operating Cash Flow"] + years["Capital Expenditure"] - sbc
    years["margin"] = years["owner_cf"] / years["Total Revenue"]

    recent = years.tail(cfg.NORMALISE_YEARS)
    margin = float(recent["margin"].mean())
    revenue = float(years["Total Revenue"].iloc[-1])
    span = len(years) - 1
    revenue_cagr = float((revenue / years["Total Revenue"].iloc[0]) ** (1 / span) - 1)

    v.model = "dcf"
    v.delivered = revenue_cagr
    v.implied_label = "starting growth"
    v.inputs = {
        "owner_cf_margin": margin,
        "latest_margin": float(years["margin"].iloc[-1]),
        # Worst year on record and how many years there are: the screener
        # uses these to tell a steady cash generator from a cyclical peak.
        "margin_min": float(years["margin"].min()),
        "margin_years": len(years),
        "revenue_cagr": revenue_cagr,
        "cagr_years": span,
        "sbc_share_of_ocf": sbc_share,
        # Kept so a forecast can be valued later without re-reading the
        # statements; never printed.
        "revenue": revenue,
        "shares": shares,
    }

    if margin <= 0:
        negative = int((years["owner_cf"] < 0).sum())
        v.withheld = (
            f"owner cash flow is negative on average ({margin * 100:+.1f}% of "
            f"revenue; negative in {negative} of {len(years)} years) -- "
            f"capex and SBC exceed operating cash flow"
        )
        return

    cash_flow = margin * revenue
    g_terminal = cfg.TERMINAL_GROWTH[v.currency]
    r = v.discount_rate
    for name in SCENARIOS:
        share, cap = cfg.DCF_SCENARIOS[name]
        g_start = min(cap, max(g_terminal, share * revenue_cagr))
        value, terminal_share = dcf_value(cash_flow, g_start, g_terminal, r)
        v.values[name] = value / shares
        v.inputs[f"g_{name}"] = g_start
        if name == "base":
            v.terminal_share = terminal_share
    v.implied = implied_growth(v.price * shares, cash_flow, g_terminal, r)


def _value_residual_income(v: Valuation, group: pd.DataFrame, shares: float) -> None:
    """Fill `v` with a residual-income valuation, or the reason there is not one."""
    needed = ["Stockholders Equity", "Net Income"]
    if any(item not in group.columns for item in needed):
        v.withheld = "book equity or net income not reported"
        return
    years = group.dropna(subset=needed)
    if len(years) < 2:
        v.withheld = "fewer than two fiscal years of book equity"
        return

    book = float(years["Stockholders Equity"].iloc[-1])
    # ROE on average equity: profit was earned on the capital in place
    # during the year, not the amount left at the end of it.
    average_equity = float(years["Stockholders Equity"].iloc[-2:].mean())
    roe = float(years["Net Income"].iloc[-1]) / average_equity

    # Payout = dividends + buybacks over profit. Both are stored as
    # negative outflows. Buybacks count: they shrink book exactly as a
    # dividend does.
    returned = pd.Series(0.0, index=years.index)
    found = False
    for item in ("Cash Dividends Paid", "Repurchase Of Capital Stock"):
        if item in years.columns and years[item].notna().any():
            returned = returned - years[item].fillna(0.0)
            found = True
    if not found:
        v.withheld = "no dividend or buyback data to set the retention rate"
        return
    recent = years.tail(cfg.NORMALISE_YEARS)
    payout = float(returned.loc[recent.index].sum() / recent["Net Income"].sum())
    payout = min(max(payout, 0.0), 1.0)

    r = v.discount_rate
    g_terminal = cfg.TERMINAL_GROWTH[v.currency]
    v.model = "residual income"
    v.delivered = roe
    v.implied_label = "permanent ROE"
    v.inputs = {
        "book_per_share": book / shares,
        "price_to_book": v.price * shares / book,
        "roe": roe,
        "payout": payout,
        "book": book,
        "shares": shares,
    }
    for name in SCENARIOS:
        value = residual_income_value(book, roe, r, payout, cfg.RI_FADE_YEARS[name])
        v.values[name] = value / shares
    v.implied = implied_roe(v.inputs["price_to_book"], r, g_terminal)


def value_holdings(conn: sqlite3.Connection, knowledge_date: str | None = None) -> list[Valuation]:
    """One Valuation per stock currently held, most recent price."""
    positions = db.read_table(conn, "positions")
    if positions.empty:
        return []
    latest = positions[positions["report_date"] == positions["report_date"].max()]
    latest = latest[latest["asset_category"] == "STK"]
    currencies = dict(zip(latest["symbol"], latest["currency"]))
    return value_symbols(conn, currencies, knowledge_date)


def value_symbols(
    conn: sqlite3.Connection,
    currencies: dict[str, str],
    knowledge_date: str | None = None,
    price_table: str = "holding_prices",
) -> list[Valuation]:
    """
    One Valuation per symbol in `currencies` ({symbol: trading currency}),
    as of `knowledge_date`: last price on or before it, fundamentals
    available by it, and (for the screening universe) a beta measured on
    returns up to it.
    """
    prices = db.read_table(conn, price_table).dropna(subset=["close"])
    if knowledge_date is not None:
        prices = prices[prices["date"] <= knowledge_date]
    wide = fundamentals.as_of(conn, knowledge_date, list(currencies))
    if price_table == "holding_prices":
        betas = local_betas(conn, currencies)
    else:
        betas = local_betas(conn, currencies, price_table, knowledge_date)
    price_groups = {symbol: g.sort_values("date") for symbol, g in prices.groupby("symbol")}

    out: list[Valuation] = []
    for symbol in sorted(currencies):
        currency = currencies[symbol]
        own = price_groups.get(symbol)
        if own is None or own.empty:
            continue
        v = Valuation(
            symbol=symbol,
            currency=currency,
            price=float(own["close"].iloc[-1]),
            price_date=str(own["date"].iloc[-1]),
        )
        out.append(v)

        if currency not in cfg.RISK_FREE:
            v.withheld = f"no discount-rate assumptions for {currency}"
            continue
        if symbol not in betas:
            v.withheld = "not enough price history for a beta"
            continue
        v.beta_raw, v.beta_obs = betas[symbol]
        v.discount_rate = cost_of_equity(currency, adjusted_beta(v.beta_raw))

        group = wide[wide["symbol"] == symbol].sort_values("fiscal_date") if not wide.empty else wide
        if group.empty:
            v.withheld = "no fundamentals stored"
            continue
        shares, shares_date = _latest(group, "Ordinary Shares Number", "Share Issued")
        if not shares:
            v.withheld = "share count not reported"
            continue
        newest = str(group["fiscal_date"].max())
        if shares_date != newest:
            v.notes.append(f"share count is from {shares_date}; statements run to {newest}")

        if is_financial(group):
            _value_residual_income(v, group, shares)
        else:
            _value_dcf(v, group, shares)
    return out


def value_under(v: Valuation, growth: float | None = None, margin: float | None = None,
                roe: float | None = None, fade_years: int | None = None) -> float | None:
    """
    Per-share value of `v` under stated assumptions instead of the
    scenarios: a starting growth rate and (optionally) a margin to move
    to for a DCF name, a starting ROE and fade for a bank. None when the
    name lacks the inputs or the assumption its model needs.

    Works for a name whose base valuation was withheld for negative cash
    flow, provided a positive margin is supplied -- that is the case it
    is for.
    """
    i, r = v.inputs, v.discount_rate
    if r is None or not i.get("shares"):
        return None
    if v.model == "dcf":
        if growth is None:
            return None
        start = i["owner_cf_margin"]
        end = start if margin is None else margin
        value, _ = dcf_value_path(i["revenue"], growth, cfg.TERMINAL_GROWTH[v.currency], r,
                                  start, end)
        return value / i["shares"]
    if v.model == "residual income":
        value = residual_income_value(
            i["book"], i["roe"] if roe is None else roe, r, i["payout"],
            cfg.RI_FADE_YEARS["base"] if fade_years is None else fade_years)
        return value / i["shares"]
    return None


def thesis_value(v: Valuation) -> tuple[float | None, dict]:
    """Per-share value under the owner's forecast in cfg.THESIS, and that forecast."""
    thesis = cfg.THESIS.get(v.symbol)
    if not thesis:
        return None, {}
    value = value_under(v, growth=thesis.get("growth"), margin=thesis.get("margin"),
                        roe=thesis.get("roe"), fade_years=thesis.get("fade_years"))
    return value, thesis


def belief_ladder(v: Valuation) -> list[dict]:
    """
    Price / value across a grid of assumptions: what would have to be
    believed for the price to be fair.

    One row per margin case for a DCF name (today's margin, then any in
    cfg.MARGIN_LADDER), one row per fade length for a bank. Each row carries the ratios
    along the grid and `breakeven`, the assumption at which price equals
    value. A ratio is None where the value is not positive.
    """
    def ratio(value: float | None) -> float | None:
        return None if value is None or value <= 0 else v.price / value

    rows: list[dict] = []
    if v.model == "dcf" and v.inputs.get("shares") and v.discount_rate is not None:
        current = v.inputs["owner_cf_margin"]
        cases = [(f"margin stays {current * 100:.0f}%", None)]
        cases += [(f"margin reaches {m * 100:.0f}%", m) for m in cfg.MARGIN_LADDER.get(v.symbol, ())]
        for label, margin in cases:
            end = current if margin is None else margin
            if end <= 0:
                # A margin that never turns positive has no value at any
                # growth rate; faster growth only makes it more negative.
                rows.append({"label": label, "grid": cfg.GROWTH_LADDER,
                             "ratios": [None] * len(cfg.GROWTH_LADDER), "breakeven": None,
                             "unit": "growth"})
                continue
            rows.append({
                "label": label, "grid": cfg.GROWTH_LADDER, "unit": "growth",
                "ratios": [ratio(value_under(v, growth=g, margin=margin))
                           for g in cfg.GROWTH_LADDER],
                "breakeven": solve_increasing(
                    lambda g, m=margin: value_under(v, growth=g, margin=m), v.price),
            })
    elif v.model == "residual income" and v.inputs.get("shares"):
        for fade in cfg.FADE_LADDER:
            rows.append({
                "label": f"fades over {fade} years", "grid": cfg.ROE_LADDER, "unit": "ROE",
                "ratios": [ratio(value_under(v, roe=x, fade_years=fade)) for x in cfg.ROE_LADDER],
                "breakeven": solve_increasing(
                    lambda x, f=fade: value_under(v, roe=x, fade_years=f), v.price, 0.0, 1.0),
            })
    return rows


def current_weights(conn: sqlite3.Connection) -> dict[str, float]:
    """Each stock's share of the invested book, as a fraction."""
    positions = db.read_table(conn, "positions")
    latest = positions[positions["report_date"] == positions["report_date"].max()]
    latest = latest[latest["asset_category"] == "STK"]
    total = latest["percent_of_nav"].sum()
    if not total:
        return {}
    return dict(zip(latest["symbol"], latest["percent_of_nav"] / total))


# --------------------------------------------------------------------------
# Report.
# --------------------------------------------------------------------------

def _pct(x: float | None, width: int = 7, signed: bool = False) -> str:
    if x is None or pd.isna(x):
        return "--".rjust(width)
    return f"{x * 100:{'+' if signed else ''}.1f}%".rjust(width)


def _multiple(x: float | None, width: int = 9) -> str:
    if x is None or pd.isna(x):
        return "--".rjust(width)
    return f"{x:.2f}x".rjust(width)


def _money(x: float | None, width: int = 9) -> str:
    if x is None or pd.isna(x):
        return "--".rjust(width)
    return f"{x:,.0f}".rjust(width) if abs(x) >= 1000 else f"{x:,.2f}".rjust(width)


def main() -> None:
    conn = db.connect()
    try:
        results = value_holdings(conn)
        if not results:
            print("No positions stored. Run the Phase 1 pipeline first.")
            return
        weights = current_weights(conn)

        print("=" * 78)
        print("INTRINSIC VALUE AND MARGIN OF SAFETY")
        print("=" * 78)
        print("Assumptions (config/valuation.py) -- stated, not estimated:")
        for ccy in sorted({v.currency for v in results if v.currency in cfg.RISK_FREE}):
            print(f"  {ccy}: risk-free {cfg.RISK_FREE[ccy] * 100:.2f}%, "
                  f"terminal growth {cfg.TERMINAL_GROWTH[ccy] * 100:.1f}%, "
                  f"beta vs {cfg.BETA_BENCHMARK[ccy]}")
        print(f"  equity risk premium {cfg.EQUITY_RISK_PREMIUM * 100:.1f}%, "
              f"{cfg.HORIZON_YEARS}-year fade, betas shrunk "
              f"{(1 - cfg.BETA_SHRINK) * 100:.0f}% toward 1")

        print("\n" + "-" * 78)
        print("VALUE PER SHARE, local currency")
        print("-" * 78)
        print(f"{'symbol':<8}{'model':<17}{'beta':>5}{'r':>7}{'price':>9}"
              f"{'bear':>9}{'base':>9}{'bull':>9}{'price/base':>12}")
        for v in results:
            beta = "--" if v.beta_raw is None else f"{adjusted_beta(v.beta_raw):.2f}"
            print(f"{v.symbol:<8}{(v.model or 'withheld'):<17}{beta:>5}"
                  f"{_pct(v.discount_rate)}{_money(v.price)}"
                  + "".join(_money(v.values.get(s)) for s in SCENARIOS)
                  + _multiple(v.price_to_value(), 12))
        print("\n  price/base below 1.00x is a discount to the base-case value.")

        print("\n" + "-" * 78)
        print("WHAT THE PRICE ASSUMES, against what the company has delivered")
        print("-" * 78)
        for v in results:
            if v.model == "dcf" and not v.withheld:
                i = v.inputs
                implied = (_pct(v.implied).strip() if v.implied is not None
                           else "outside the solver's range")
                print(f"  {v.symbol}: price needs owner cash flow to start growing at "
                      f"{implied},")
                print(f"     fading to {cfg.TERMINAL_GROWTH[v.currency] * 100:.1f}% over "
                      f"{cfg.HORIZON_YEARS} years. Revenue grew "
                      f"{_pct(i['revenue_cagr']).strip()}/yr over the last "
                      f"{int(i['cagr_years'])} years;")
                print(f"     base case assumes {_pct(i['g_base']).strip()}. Owner cash "
                      f"flow margin {_pct(i['owner_cf_margin']).strip()} "
                      f"(latest year {_pct(i['latest_margin']).strip()})"
                      + ("." if pd.isna(i["sbc_share_of_ocf"]) else
                         f"; SBC is {_pct(i['sbc_share_of_ocf']).strip()} of "
                         f"operating cash flow."))
                print(f"     {_pct(v.terminal_share).strip()} of the base value is "
                      f"the terminal period.")
            elif v.model == "residual income":
                i = v.inputs
                print(f"  {v.symbol}: at {i['price_to_book']:.2f}x book the price "
                      f"assumes a permanent ROE of {_pct(v.implied).strip()}.")
                print(f"     Latest ROE {_pct(v.delivered).strip()} against a cost of "
                      f"equity of {_pct(v.discount_rate).strip()}; book value "
                      f"{_money(i['book_per_share']).strip()} per share; "
                      f"{_pct(i['payout']).strip()} of profit paid out.")

        withheld = [v for v in results if v.withheld]
        if withheld:
            print("\n" + "-" * 78)
            print("VALUE WITHHELD -- the method does not apply, which is not a value of zero")
            print("-" * 78)
            for v in withheld:
                print(f"  {v.symbol}: {v.withheld}")

        print("\n" + "-" * 78)
        print("WHAT YOU WOULD HAVE TO BELIEVE -- price / value across a grid of assumptions")
        print("-" * 78)
        print("  Below 1.00x the price is under the value that assumption gives. The grid is")
        print("  not a forecast; find the column you actually believe.\n")
        for v in results:
            ladder = belief_ladder(v)
            if not ladder:
                continue
            unit = ladder[0]["unit"]
            head = "starting growth" if unit == "growth" else "starting ROE"
            print(f"  {v.symbol}  ({head}; delivered "
                  f"{_pct(v.delivered).strip()})")
            print(f"  {'':<24}" + "".join(f"{g * 100:>7.0f}%" for g in ladder[0]["grid"])
                  + f"{'fair at':>10}")
            for row in ladder:
                cells = "".join("     n/a" if x is None else f"{x:>7.2f}x" for x in row["ratios"])
                fair = "--" if row["breakeven"] is None else f"{row['breakeven'] * 100:.0f}%"
                print(f"  {row['label']:<24}{cells}{fair:>10}")
            print()
        print("  'fair at' is the assumption at which price equals value. Margin rows move the")
        print("  owner-cash-flow margin linearly to that level over the horizon.")

        print("\n" + "-" * 78)
        print("YOUR THESIS -- value under your own forecast (config/valuation.py, THESIS)")
        print("-" * 78)
        entered = False
        for v in results:
            value, thesis = thesis_value(v)
            if not thesis:
                continue
            entered = True
            stated = ", ".join(
                f"{k} {x * 100:.0f}%" if k != "fade_years" else f"fade {x} yrs"
                for k, x in thesis.items() if k in ("growth", "margin", "roe", "fade_years"))
            if value is None or value <= 0:
                print(f"  {v.symbol}: {stated} -> no positive value under this forecast "
                      f"(a {v.model or 'valuation'} needs "
                      f"{'growth and a positive margin' if v.model == 'dcf' else 'an ROE'})")
                continue
            mos = 1 - v.price / value
            print(f"  {v.symbol}: {stated} -> value {_money(value).strip()} against price "
                  f"{_money(v.price).strip()}: {v.price / value:.2f}x, margin of safety "
                  f"{_pct(mos, signed=True).strip()}, rule weight "
                  f"{_pct(target_weight(mos)).strip()}")
        missing = [v.symbol for v in results if v.symbol not in cfg.THESIS]
        if not entered:
            print("  None entered. The forecasts are yours to make; nothing is assumed for you.")
        if missing:
            print(f"  No forecast for: {', '.join(missing)}.")

        print("\n" + "-" * 78)
        print("SIZING: weight held against weight the discount would justify")
        print("-" * 78)
        print("  (on the cautious base case, not on your thesis)")
        print(f"  rule: {cfg.SIZING_FRACTION:.2f} x margin of safety, nothing below "
              f"{cfg.MIN_MARGIN_OF_SAFETY * 100:.0f}%, capped at "
              f"{cfg.MAX_WEIGHT * 100:.0f}%\n")
        print(f"{'symbol':<8}{'held':>8}{'-- price / value --':>30}"
              f"{'margin of':>11}{'target':>9}")
        print(f"{'':<8}{'':>8}{'bear':>10}{'base':>10}{'bull':>10}"
              f"{'safety':>11}{'':>9}")
        invested = 0.0
        for v in results:
            mos = None if v.withheld else v.margin_of_safety()
            target = target_weight(mos)
            invested += target or 0.0
            print(f"{v.symbol:<8}{_pct(weights.get(v.symbol), 8)}"
                  + "".join(_multiple(None if v.withheld else v.price_to_value(s), 10)
                            for s in SCENARIOS)
                  + (_pct(mos, 11, signed=True) if mos is None or mos > 0
                     else "none".rjust(11))
                  + _pct(target, 9))
        print(f"{'cash':<8}{'':>8}{'':>41}{_pct(1 - invested, 9)}")
        print("\n  'held' is each stock's share of the invested book. Margin of")
        print("  safety is 1 - price/value on the base case. A withheld name has")
        print("  no target: the model has no opinion on it either way.")

        notes = [(v.symbol, n) for v in results for n in v.notes]
        if notes:
            print("\n" + "-" * 78)
            print("DATA NOTES")
            print("-" * 78)
            for symbol, note in notes:
                print(f"  {symbol}: {note}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
