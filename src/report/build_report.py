"""
Phase 7: the one-page daily report.

Reads what every earlier phase computes and renders a single HTML file,
reports/daily_report.html, that answers the project's four questions for
the latest day in the database:

  1. What did the portfolio make or lose?
  2. Why -- stock move, currency, dividends, costs?
  3. How much could it lose -- VaR and stress scenarios?
  4. How well were the trades executed?

plus the valuation view from Phase 4 and the checks that say whether the
numbers can be trusted today.

**Nothing on the page is a currency amount.** Every figure is a percent of
NAV, a weight, a ratio or basis points; share prices are public. The page
is built to be shown, and the rule is enforced by construction: the
functions here convert to fractions of NAV before anything reaches the
template, and no NAV, position value or account identifier is passed in.

The page is one self-contained file: inline CSS, inline SVG, no
JavaScript and no network requests, so it opens from disk and can be
attached to an email. Colours are the same reference palette as
plots.py -- categorical slots 1 and 2, used in fixed order -- with a dark
variant selected for the dark surface rather than inverted. Every chart
has the table it was drawn from directly beneath or beside it.

A section whose inputs are missing (no intraday bars, no scenario
prices) renders a one-line reason instead of failing the whole page.

Run directly:
    python src/report/build_report.py
"""

from __future__ import annotations

import html
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from analytics import factors, pnl, risk, stress, tca  # noqa: E402
from checks import reconcile  # noqa: E402
from screener import candidates  # noqa: E402
from sizing import intrinsic  # noqa: E402
from storage import db  # noqa: E402

OUTPUT = PROJECT_ROOT / "reports" / "daily_report.html"
MINUS = "−"


# --------------------------------------------------------------------------
# Formatting. Everything that reaches the page goes through one of these.
# --------------------------------------------------------------------------

def _missing(x) -> bool:
    return x is None or (isinstance(x, float) and np.isnan(x)) or x is pd.NA


def pct(x, digits: int = 2, signed: bool = True) -> str:
    """A fraction as a percent, with a real minus sign."""
    if _missing(x):
        return "n/a"
    text = f"{abs(x) * 100:.{digits}f}%"
    if x < 0:
        return MINUS + text
    return ("+" if signed else "") + text


def bps(x, digits: int = 1) -> str:
    if _missing(x):
        return "n/a"
    return (MINUS if x < 0 else "+") + f"{abs(x):.{digits}f}"


def num(x, digits: int = 2) -> str:
    if _missing(x):
        return "n/a"
    return (MINUS if x < 0 else "") + f"{abs(x):.{digits}f}"


def esc(text) -> str:
    return html.escape(str(text))


def table(headers: list[str], rows: list[list[str]], numeric_from: int = 1,
          text: tuple[int, ...] = ()) -> str:
    """
    An HTML table. Columns from `numeric_from` on are right-aligned so
    figures line up, except those listed in `text`.
    """
    def cells(values, tag):
        return "".join(
            f'<{tag}{" class=num" if i >= numeric_from and i not in text else ""}>{v}</{tag}>'
            for i, v in enumerate(values)
        )
    body = "".join(f"<tr>{cells(row, 'td')}</tr>" for row in rows)
    return f"<table><thead><tr>{cells(headers, 'th')}</tr></thead><tbody>{body}</tbody></table>"


def tile(label: str, value: str, note: str = "") -> str:
    note_html = f'<div class="tile-note">{note}</div>' if note else ""
    return (f'<div class="tile"><div class="tile-label">{esc(label)}</div>'
            f'<div class="tile-value">{value}</div>{note_html}</div>')


def unavailable(reason: str) -> str:
    return f'<p class="unavailable">Not available: {esc(reason)}</p>'


# --------------------------------------------------------------------------
# Charts. Inline SVG, horizontal bars from a zero baseline.
# --------------------------------------------------------------------------

BAR = 16          # bar thickness, px
ROW = 30          # row pitch for a single-bar row
LABEL_W = 124     # left gutter for category labels
VALUE_W = 58      # room at each end for the value label
WIDTH = 520


def _bar_path(x0: float, x1: float, y: float, height: float, radius: float = 4) -> str:
    """A bar from baseline x0 to x1: square at the baseline, rounded at the data end."""
    r = min(radius, abs(x1 - x0), height / 2)
    if x1 >= x0:
        return (f"M{x0:.1f},{y:.1f} H{x1 - r:.1f} Q{x1:.1f},{y:.1f} {x1:.1f},{y + r:.1f} "
                f"V{y + height - r:.1f} Q{x1:.1f},{y + height:.1f} {x1 - r:.1f},{y + height:.1f} "
                f"H{x0:.1f} Z")
    return (f"M{x0:.1f},{y:.1f} H{x1 + r:.1f} Q{x1:.1f},{y:.1f} {x1:.1f},{y + r:.1f} "
            f"V{y + height - r:.1f} Q{x1:.1f},{y + height:.1f} {x1 + r:.1f},{y + height:.1f} "
            f"H{x0:.1f} Z")


def bar_chart(
    groups: list[tuple[str, list[tuple[str, float]]]],
    fmt,
    title: str,
    series: list[str] | None = None,
) -> str:
    """
    Horizontal bars around a shared zero line.

    `groups` is [(row label, [(series name, value), ...])]. With one
    value per row the chart is single-series and takes no legend; with
    more, `series` names them in fixed order and a legend is drawn.
    Every bar carries its value at the tip and a native tooltip.
    """
    values = [v for _, items in groups for _, v in items if not _missing(v)]
    if not values:
        return unavailable("no values to chart")
    lo, hi = min(0.0, min(values)), max(0.0, max(values))
    span = (hi - lo) or 1.0
    plot_left, plot_right = LABEL_W + VALUE_W, WIDTH - VALUE_W
    scale = (plot_right - plot_left) / span
    zero = plot_left + (0.0 - lo) * scale

    per_row = max(len(items) for _, items in groups)
    row_height = max(ROW, per_row * (BAR + 2) + 12)
    top = 26 if series else 8
    height = top + row_height * len(groups) + 8

    parts = [f'<svg class="chart" viewBox="0 0 {WIDTH} {height}" role="img" '
             f'aria-label="{esc(title)}">']
    if series:
        x = plot_left
        for i, name in enumerate(series):
            parts.append(f'<rect x="{x}" y="4" width="10" height="10" rx="2" class="s{i + 1}"/>'
                         f'<text x="{x + 15}" y="13" class="legend">{esc(name)}</text>')
            x += 15 + 7 * len(name) + 22

    for g, (label, items) in enumerate(groups):
        y_row = top + g * row_height
        block = len(items) * BAR + (len(items) - 1) * 2
        y = y_row + (row_height - block) / 2
        parts.append(f'<text x="{LABEL_W}" y="{y_row + row_height / 2 + 4:.1f}" '
                     f'class="cat" text-anchor="end">{esc(label)}</text>')
        for i, (name, value) in enumerate(items):
            if _missing(value):
                continue
            x1 = zero + value * scale
            tip = f"{label}{' · ' + name if series else ''}: {fmt(value)}"
            anchor, dx = ("start", 6) if value >= 0 else ("end", -6)
            parts.append(
                f'<g class="mark"><title>{esc(tip)}</title>'
                f'<rect x="{plot_left - VALUE_W}" y="{y - 1:.1f}" '
                f'width="{plot_right - plot_left + 2 * VALUE_W}" height="{BAR + 2}" class="hit"/>'
                f'<path d="{_bar_path(zero, x1, y, BAR)}" class="s{i + 1}"/>'
                f'<text x="{x1 + dx:.1f}" y="{y + BAR / 2 + 4:.1f}" class="val" '
                f'text-anchor="{anchor}">{fmt(value)}</text></g>'
            )
            y += BAR + 2
    parts.append(f'<line x1="{zero:.1f}" x2="{zero:.1f}" y1="{top}" y2="{height - 6}" class="axis"/>')
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------
# Sections. Each returns HTML and reads only fractions of NAV.
# --------------------------------------------------------------------------

def section_pnl(conn: sqlite3.Connection) -> tuple[str, dict]:
    """Questions 1 and 2: the day's return and where it came from."""
    returns = pnl.daily_returns(conn)
    summary = pnl.daily_summary(conn)
    mtm = pnl.compute_mark_to_market(conn)
    if returns.empty:
        return unavailable("no attribution history; run src/analytics/pnl.py"), {}

    last = returns.dropna(subset=["attributed_return"]).iloc[-1]
    day, nav_prior = last["date"], float(last["nav_prior"])
    row = summary[summary["date"] == day].iloc[0]
    components = [
        ("Stock moves", row.get("stock_move", 0.0)),
        ("Currency", row.get("fx_move", 0.0)),
        ("Stock x currency", row.get("interaction", 0.0)),
        ("Dividends", row.get("dividends", 0.0)),
        ("Trading costs", row.get("commission_base", 0.0) + row.get("taxes_base", 0.0)),
    ]
    components = [(name, float(value) / nav_prior * 1e4) for name, value in components]

    chart = bar_chart([(name, [("", value)]) for name, value in components],
                      lambda v: bps(v) + " bp", f"Return attribution for {day}")

    holdings = mtm[mtm["date"] == day].copy()
    holdings["weight"] = holdings["value_prev_base"] / nav_prior
    holdings["contribution"] = holdings["total_move"] / nav_prior
    holdings = holdings[holdings["quantity"] != 0].sort_values("contribution")
    rows = [[esc(h.symbol), esc(h.currency), pct(h.weight, 1, signed=False),
             pct(h.r_local), pct(h.r_fx), bps(h.contribution * 1e4)]
            for h in holdings.itertuples(index=False)]
    by_holding = table(["Holding", "Ccy", "Weight", "Local return", "FX return",
                        "Contribution (bp)"], rows, numeric_from=2)

    valid = returns.dropna(subset=["nav_return"])
    period = float((1 + valid["nav_return"]).prod() - 1)
    facts = {
        "day": day,
        "day_return": float(last["attributed_return"]),
        "nav_return": None if pd.isna(last["nav_return"]) else float(last["nav_return"]),
        "period_return": period,
        "period_days": len(valid),
        "period_start": valid["date"].iloc[0],
    }
    body = (f'<div class="split"><div>{chart}'
            f'<p class="note">Basis points of the previous day\'s NAV. '
            f'R<sub>base</sub> = r<sub>local</sub> + r<sub>fx</sub> + '
            f'r<sub>local</sub>·r<sub>fx</sub>; the third term is the interaction.</p></div>'
            f'<div>{by_holding}</div></div>')
    return body, facts


def section_risk(conn: sqlite3.Connection) -> tuple[str, dict]:
    """Question 3, first half: ordinary-day risk."""
    returns = risk.portfolio_returns(conn)
    if returns.empty:
        return unavailable("no return history"), {}
    vol = float(returns.std(ddof=1) * np.sqrt(risk.TRADING_DAYS))
    rows = []
    facts = {"vol": vol, "n_obs": len(returns)}
    for level in (0.95, 0.99):
        v = risk.value_at_risk(returns, level)
        facts[f"var_{int(level * 100)}"] = v.historical
        rows.append([
            f"{int(level * 100)}% one-day VaR",
            pct(abs(v.historical), signed=False), pct(abs(v.parametric), signed=False),
            f"{v.tail_obs}" + ("" if v.reliable else " (too few to rely on)"),
        ])
    var_table = table(["", "Historical", "Parametric (normal)", "Observations in tail"], rows)

    betas = risk.benchmark_betas(conn)
    beta_rows = [[esc(b.benchmark), num(b.beta), num(b.r_squared), str(b.n_obs)]
                 for b in betas.itertuples(index=False)]
    beta_table = table(["Benchmark (in base currency)", "Beta", "R²", "Days"], beta_rows)

    conc = risk.concentration(conn)
    conc_text = ""
    if conc:
        conc_text = (f'<p class="note">Largest position {pct(conc["max_weight_nav"], 1, signed=False)} '
                     f'of NAV; cash {pct(conc["cash_weight_nav"], 1, signed=False)}; '
                     f'effective number of positions {conc["effective_n_nav"]:.1f} '
                     f'(1 / HHI, cash included).</p>')
    return f'<div class="split"><div>{var_table}</div><div>{beta_table}{conc_text}</div></div>', facts


def section_stress(conn: sqlite3.Connection) -> tuple[str, dict]:
    """Question 3, second half: named events."""
    result = stress.run_scenarios(conn)
    if not result:
        return unavailable("no scenario prices; run src/data/market_data.py"), {}
    replays = result["replays"]
    chart = bar_chart(
        [(r.scenario, [("Stocks", r.stock), ("Currency", r.fx)]) for r in replays],
        lambda v: pct(v, 1), "Historical replay losses by source",
        series=["Stocks", "Currency"],
    )
    rows = []
    for r in replays:
        proxied = "" if not r.proxied else f" <sup>{pct(r.proxied_weight, 0, signed=False)} proxied</sup>"
        rows.append([esc(r.scenario) + proxied, f"{r.peak.date()} → {r.trough.date()}",
                     f"<strong>{pct(r.loss, 1)}</strong>", pct(r.stock, 1), pct(r.fx, 1),
                     pct(r.cross, 1)])
    replay_table = table(["Episode", "Peak → trough", "Loss", "Stocks", "Currency", "Cross"],
                         rows, numeric_from=2)

    shock_rows = [[esc(s["name"]), pct(s["direct"], 1), pct(s["calm"], 1), pct(s["episode"], 1),
                   esc(s["episode_name"] or "")] for s in result["shocks"]]
    shock_table = table(["Shock", "Direct only", "+ calm betas", "+ episode betas",
                         "Episode used"], shock_rows, text=(4,))

    worst = min(replays, key=lambda r: r.loss)
    facts = {"worst_loss": worst.loss, "worst_name": worst.scenario}
    body = (f'{chart}<p class="note">Today\'s weights carried through each episode; worst '
            f'peak-to-trough fall on the portfolio\'s own dates, % of NAV.</p>'
            f'<div class="scroll">{replay_table}</div>'
            f'<h3>Hypothetical shocks</h3>{shock_table}'
            f'<p class="note">Direct: only the stated move happens. Calm betas: each stock also '
            f'moves by its two-year weekly sensitivity. Episode betas: by what it did, per unit '
            f'of the factor, in the named episode — one observation, not an estimate.</p>')
    return body, facts


def section_book(conn: sqlite3.Connection) -> str:
    """What is held, what it is worth, and what the factor regressions say."""
    valuations = intrinsic.value_holdings(conn)
    if not valuations:
        return unavailable("no positions or fundamentals stored")
    scenario = stress.run_scenarios(conn)
    nav_weights = {e.name: e.weight for e in scenario.get("exposures", [])}

    rows = []
    for v in valuations:
        if v.withheld:
            implied = "withheld"
            delivered = ratio = target = "n/a"
        else:
            unit = "growth" if v.model == "dcf" else "ROE"
            implied = f"{pct(v.implied, 1, signed=v.model == 'dcf')} {unit}"
            delivered = pct(v.delivered, 1, signed=v.model == "dcf")
            ratio = f"{v.price_to_value():.2f}x"
            target = pct(intrinsic.target_weight(v.margin_of_safety()), 1, signed=False)
        rows.append([esc(v.symbol), esc(v.currency),
                     pct(nav_weights.get(v.symbol), 1, signed=False),
                     esc(v.model or "none"), implied, delivered, ratio, target])
    cash = sum(w for name, w in nav_weights.items() if "cash" in name)
    if nav_weights:
        rows.append(["Cash", "", pct(cash, 1, signed=False), "", "", "", "", ""])
    value_table = table(["Holding", "Ccy", "Weight of NAV", "Model", "Price assumes",
                         "Delivered", "Price / value", "Target weight"], rows, numeric_from=2,
                        text=(3,))

    factor_rows = []
    for currency in ("USD", "JPY"):
        try:
            t = factors.regress_sleeve(conn, currency).table().set_index("term")
        except Exception:
            continue
        factor_rows.append([f"{currency} sleeve"] + [
            f"{num(t.loc[f, 'coef'])} <span class=t>(t {num(t.loc[f, 't_nw'], 1)})</span>"
            for f in ("MKT", "HML", "RMW", "MOM")
        ])
    factor_html = ""
    if factor_rows:
        factor_html = ("<h3>Factor exposure</h3>"
                       + table(["", "Market", "Value (HML)", "Profitability (RMW)", "Momentum"],
                               factor_rows)
                       + '<p class="note">Each sleeve regressed in its own currency on its own '
                         "region's Fama-French factors; Newey-West t-statistics.</p>")
    note = ('<p class="note">Price / value is against the base-case intrinsic value; below 1.00x '
            "is a discount. A withheld value means the method does not apply, not that the "
            "value is zero. Assumptions are in config/valuation.py.</p>")
    return value_table + note + factor_html


def section_screen(conn: sqlite3.Connection) -> str:
    """Names the intrinsic-value screen passes, and the portfolio its rule implies."""
    result = candidates.screen(conn)
    if result.empty:
        return unavailable("no verified universe; run src/screener/universe.py")
    passed = result[result["status"] == "candidate"].sort_values("price_to_value")
    weights = candidates.suggest_weights(passed)
    held = candidates.current_book(conn)
    counts = result["status"].value_counts()

    rows = []
    for r in passed.itertuples(index=False):
        unit = "growth" if r.model == "dcf" else "ROE"
        signed = r.model == "dcf"
        rows.append([
            esc(r.symbol) + (" <sup>held</sup>" if r.symbol in held else ""),
            esc(str(r.name).title()[:26]), esc(r.group_label),
            f"{r.price_to_value:.2f}x", f"{r.price_to_value_bear:.2f}x",
            f"{pct(r.implied, 0, signed=signed)} {unit}", pct(r.delivered, 0, signed=signed),
            pct(float(weights.get(r.symbol, 0.0)), 1, signed=False),
        ])
    rows.append(["Cash", "", "", "", "", "", "",
                 pct(max(0.0, 1 - float(weights.sum())), 1, signed=False)])
    out = table(["Candidate", "Name", "Peer group", "Price / value", "Bear case",
                 "Price assumes", "Delivered", "Rule weight"], rows, numeric_from=3)

    reasons = result.set_index("symbol")["reason"]
    dropped = [f"{h} ({str(reasons.get(h, 'not in the universe')).split(':')[0]})"
               for h in sorted(held) if h not in set(passed["symbol"])]
    summary = (f'<p class="note">{len(result)} names screened: {counts.get("candidate", 0)} '
               f'candidates, {counts.get("excluded", 0)} valued but excluded, '
               f'{counts.get("withheld", 0)} with no valuation. The universe is the holdings\' '
               f'sector peers, not the market. Current holdings that do not pass: '
               f'{esc("; ".join(dropped) or "none")}. The screen is stricter than the holdings table '
               f'above: it also requires cash flow that held up in the worst year on record.</p>')

    check = candidates.validation(conn)
    evidence = ""
    if not check.empty:
        parts = [f"as of {r.knowledge_date}, rank correlation {num(r.spearman)} "
                 f"(p {r.p_value:.2f}, {r.n} names), would-be candidates "
                 f"{pct(r.candidate_excess, 1)} vs. peers" for r in check.itertuples(index=False)]
        evidence = ('<p class="note"><strong>Has the ranking worked?</strong> Valued on past data '
                    "and tested on the following 12 months: " + "; ".join(parts) +
                    ". Weak and not statistically distinguishable from zero — a list of names "
                    "to research, not a portfolio shown to be better.</p>")
    return out + summary + evidence


def section_execution(conn: sqlite3.Connection) -> str:
    """Question 4."""
    result = tca.analyse(conn)
    if result.empty:
        return unavailable("no trades stored")
    if result["arrival_bps"].isna().all():
        return unavailable("no intraday bars; run src/data/ibkr_live.py with IB Gateway up")
    rows = []
    for r in result.itertuples(index=False):
        thin = tca._thin(r.coverage)
        star = "<sup>*</sup>" if thin else ""
        wait = "n/a" if _missing(r.wait_minutes) else (
            "none" if r.wait_minutes < 1 / 60 else f"{r.wait_minutes:.0f} min")
        rows.append([esc(r.symbol), esc(r.trade_date), esc(r.side.lower()), wait,
                     bps(r.arrival_bps), bps(r.day_vwap_bps) + (star if not _missing(r.day_vwap_bps) else ""),
                     bps(r.close_bps), bps(r.cost_bps), f"<strong>{bps(r.total_bps)}</strong>",
                     esc(r.arrival_source or "n/a")])
    out = table(["Holding", "Date", "Side", "Waited", "vs arrival", "vs day VWAP", "vs close",
                 "Commission + tax", "All-in", "Quote bars"], rows, numeric_from=4,
                text=(9,))
    note = (f'<p class="note">Basis points, positive = cost. {len(result)} fills: a per-trade '
            "record, not a sample, so no averages. Unfilled orders are not in the broker "
            "statement, so a patient limit order can only appear here as a success."
            + (" <sup>*</sup> VWAP from bars covering only part of that day's volume."
               if any(tca._thin(c) for c in result["coverage"]) else "") + "</p>")
    return out + note


def section_checks(conn: sqlite3.Connection) -> str:
    """Whether today's numbers can be trusted."""
    items = []
    try:
        rec = reconcile.reconcile(conn)
        state = "match" if rec.passed else "DO NOT MATCH"
        items.append(f"Positions + cash + accruals {state} the broker's NAV for {esc(rec.report_date)} "
                     f"(difference {abs(rec.diff_pct):.4f}%).")
    except Exception as exc:
        items.append(f"Reconciliation did not run ({esc(type(exc).__name__)}).")

    returns = pnl.daily_returns(conn).dropna(subset=["return_diff"])
    if not returns.empty:
        diff = returns["return_diff"]
        items.append(
            f"Attributed daily return vs. broker NAV return over {len(returns)} days: mean "
            f"difference {bps(diff.mean() * 1e4, 2)} bp, tracking error "
            f"{diff.std(ddof=1) * 1e4:.0f} bp/day, correlation "
            f"{returns['attributed_return'].corr(returns['nav_return']):.3f}.")
    items.append("Fundamentals are read only from their assumed availability date "
                 "(fiscal year-end + 90 days).")
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"


# --------------------------------------------------------------------------
# Page.
# --------------------------------------------------------------------------

CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);
--s1:#2a78d6;--s2:#eb6834}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1080px;margin:0 auto;padding:28px 16px 48px}
header h1{font-size:22px;margin:0 0 2px;font-weight:650}
header p{margin:0;color:var(--ink2)}
section{background:var(--surface);border:1px solid var(--border);border-radius:10px;
padding:18px 20px;margin-top:16px}
h2{font-size:15px;margin:0 0 12px;font-weight:650}
.scroll+h3,.note+h3{margin-top:22px}
h3{font-size:13px;margin:18px 0 8px;font-weight:650;color:var(--ink2)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,150px),1fr));gap:12px;margin-top:16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.tile-label{color:var(--ink2);font-size:12px}
.tile-value{font-size:26px;font-weight:650;line-height:1.25;margin-top:2px}
.tile-note{color:var(--muted);font-size:12px}
.split{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:20px;align-items:start}
.split>div{min-width:0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--grid);white-space:nowrap}
th{color:var(--ink2);font-weight:600;font-size:12px;border-bottom-color:var(--axis)}
tbody tr:last-child td{border-bottom:0}
.num{text-align:right}
.t{color:var(--muted)}
sup{color:var(--muted);font-size:10px}
.note,.unavailable{color:var(--ink2);font-size:12px;margin:8px 0 0}
.note+.scroll{margin-top:14px}
.unavailable{font-style:italic}
ul{margin:0;padding-left:18px;color:var(--ink2)}
.scroll{overflow-x:auto}
.chart{width:100%;height:auto;display:block;max-width:520px}
.chart text{font:12px system-ui,-apple-system,"Segoe UI",sans-serif}
.chart .cat{fill:var(--ink2)}.chart .val{fill:var(--ink);font-variant-numeric:tabular-nums}
.chart .legend{fill:var(--ink2)}.chart .axis{stroke:var(--axis);stroke-width:1}
.chart .s1{fill:var(--s1)}.chart .s2{fill:var(--s2)}
.chart .hit{fill:transparent}.chart .mark:hover path{opacity:.75}
footer{color:var(--muted);font-size:12px;margin-top:18px}
"""


def build(conn: sqlite3.Connection) -> str:
    pnl_html, p = section_pnl(conn)
    risk_html, r = section_risk(conn)
    stress_html, s = section_stress(conn)

    tiles = []
    if p:
        note = "" if p["nav_return"] is None else f"broker NAV: {pct(p['nav_return'])}"
        tiles.append(tile("Day return", pct(p["day_return"]), note))
        tiles.append(tile(f"Return since {p['period_start']}", pct(p["period_return"], 1),
                          f"{p['period_days']} trading days, broker NAV"))
    if r:
        tiles.append(tile("Annualised volatility", pct(r["vol"], 1, signed=False),
                          f"{r['n_obs']} daily returns"))
        tiles.append(tile("One-day 95% VaR", pct(abs(r["var_95"]), signed=False), "historical"))
    if s:
        tiles.append(tile("Worst replayed episode", pct(s["worst_loss"], 1), esc(s["worst_name"])))

    as_of = p.get("day", "n/a")
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio risk report {esc(as_of)}</title><style>{CSS}</style></head>
<body><main>
<header><h1>Portfolio risk report</h1>
<p>As of {esc(as_of)} · base currency SGD · all figures are % of NAV, weights or basis points</p></header>
<div class="tiles">{''.join(tiles)}</div>
<section><h2>What did it make, and why?</h2>{pnl_html}</section>
<section><h2>How much could it lose on an ordinary day?</h2>{risk_html}</section>
<section><h2>What would a crisis cost?</h2>{stress_html}</section>
<section><h2>What is held, and what is it worth?</h2><div class="scroll">{section_book(conn)}</div></section>
<section><h2>What does the value screen pass?</h2><div class="scroll">{section_screen(conn)}</div></section>
<section><h2>How well were the trades executed?</h2><div class="scroll">{section_execution(conn)}</div></section>
<section><h2>Can these numbers be trusted today?</h2>{section_checks(conn)}</section>
<footer>Generated {generated} from the local database. Read-only: nothing in this project places,
modifies or cancels orders. Not investment advice.</footer>
</main></body></html>
"""


def main() -> None:
    conn = db.connect()
    try:
        page = build(conn)
    finally:
        conn.close()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(PROJECT_ROOT)}  ({len(page) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
