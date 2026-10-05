# Portfolio Risk & P&L Attribution Engine

## What this project is
A desk-style risk report built on my real IBKR account (US + Japanese equities).
Every day it answers four questions:
1. What did my portfolio make or lose?
2. Why? (stock move vs. FX vs. dividends/fees)
3. How much could it lose tomorrow? (VaR, stress tests)
4. How well did I trade? (execution vs. arrival price / VWAP)

I invest using **firm-foundation theory** (intrinsic value from future cash flows; buy below it).
Phase 4 tests and strengthens that philosophy with quant factors (value, quality, momentum)
and margin-of-safety position sizing.

Purpose beyond the tool itself: a portfolio project for Markets / S&T and IB 
interviews (summer 2027). Clean code, a clear README, and honest methodology matter.

## How I like to work (please follow)
- Explain the concept briefly before writing code — I want to understand what we're building.
- I learn by building with my real portfolio data, not toy examples.
- Build incrementally; each phase ends with something working and testable.
- When something breaks, walk through the cause, don't just patch it.

## Safety rules (never break these)
- **Read-only.** Never write code that places, modifies, or cancels orders.
  Always connect with `readonly=True`. IB Gateway has "Read-Only API" enabled.
- **No secrets in code or git.** Tokens and IDs live only in `.env` (gitignored).
  Never print, log, or hard-code the Flex token.
- **No raw account data in git.** `data/`, `*.xml`, `*.db` are gitignored.
  Public outputs show portfolio *weights*, never dollar amounts or account numbers.
- Before any commit, check `git status` for `.env`, `data/`, or `.xml` files.
- Don't run `git add` / `commit` / `push` unless I ask.

## Data sources
| Source | Use | Notes |
|---|---|---|
| IBKR Flex Web Service | History: trades, open positions, cash transactions, NAV in base, corporate actions, cash report | XML, last 365 days per request, as of prior close. `ib_async.FlexReport` |
| IB Gateway + `ib_async` | Live positions, market data, intraday bars (Phase 6) | Gateway on localhost, port 4001 (live, read-only). One IBKR session per username — mobile app login can disconnect Gateway |
| yfinance | FX history (USDSGD direct, JPYSGD derived), benchmarks (SPY, 1306.T as TOPIX proxy) | JPYSGD=X is thin on Yahoo (1 data point); derived as USDSGD/USDJPY instead |
| Kenneth French Data Library | US + Japan factor returns (Phase 4a) | |

Holdings span JPY (Tokyo-listed) and USD. Base currency confirmed: **SGD** (from Flex `EquitySummaryInBase`).

## Architecture
IBKR Flex + ib_async + yfinance → data layer → SQLite daily snapshots →
analytics (pnl, risk, stress, factors, tca) + reconciliation → daily HTML report.

## Repo structure
```
portfolio-risk/
├── config/            # settings, scenario definitions
├── src/
│   ├── data/          # flex.py, ibkr_live.py, market_data.py (+ test scripts)
│   ├── storage/       # db.py
│   ├── analytics/     # pnl.py, risk.py, stress.py, tca.py, factors.py
│   ├── screener/      # quality.py, composite.py
│   ├── sizing/        # intrinsic.py
│   ├── checks/        # reconcile.py
│   └── report/        # build_report.py, templates/
├── notebooks/         # exploration only
├── tests/
├── .env.example
└── README.md
```

## Phases (build order)
- [x] **0 — Setup:** Flex Query + token, IB Gateway read-only on 4001, repo/venv/.env/.gitignore.
  Done when: `test_flex.py` and `test_live.py` print the same positions and quantities; base currency known; no secrets staged in git.
- [x] **1 — Data pipeline:** parse Flex XML into pandas, daily SQLite snapshots, FX + benchmark history.
  Done when: computed portfolio value reconciles to IBKR's reported NAV within a small tolerance.
- [x] **2 — P&L attribution:** per holding and per day, split into stock move, FX, dividends, fees; realized vs. unrealized; roll up by currency/market.
  Key formula: R_base = (1 + r_local)(1 + r_fx) − 1 = r_local + r_fx + r_local·r_fx.
  → **MVP / first interview talking point after this phase.**
  Validated against IBKR's own NAV on a *daily return* basis: **unbiased** (mean
  difference 0.44 bps/day, t = 0.25), series correlation **0.967**, daily tracking
  error ~29 bps. The residual's serial structure (lag-1 autocorrelation −0.44) traces
  to FX snapshot timing, not valuation error; remaining noise is IBKR mark vs. Yahoo
  close. Not exact like Phase 1, since this rebuilds history from a third-party price
  source rather than re-summing Flex's own numbers. See NOTES.md.
- [x] **4a — Factor exposure (done early):** regress portfolio excess returns on French factors (MKT, SMB, HML, RMW, CMA, MOM), US and Japan sleeves separately, plus rolling.
  Each sleeve regressed in its own local currency against its own region's factors
  (mixing SGD returns with USD factors would push FX into the residual). Newey-West
  HAC standard errors alongside OLS. **Headline result: neither sleeve shows a positive
  HML loading** — USD −0.41 (t = −1.8), JPY −0.07 (t = −0.2). Alpha ~+10%/yr in both
  sleeves but t ≈ 0.3–0.5, indistinguishable from zero on one year of data. See NOTES.md
  for the interpretation, which is more nuanced than "the value thesis failed".
- [x] **3 — Risk:** historical + parametric VaR (95/99%), beta to S&P 500 and TOPIX, correlation matrix, concentration (max weight, HHI). Compare to IBKR's own VaR report as a sanity check.
  Annualised vol ~17.9%; 95% VaR 1.90% historical vs 1.78% parametric (normal assumption
  understates the tail). 99% VaR is printed with its tail count — 3 observations, so not a
  usable estimate at this sample size. Base-currency beta 0.65 to SPY, 0.38 to TOPIX;
  equity HHI 0.243, effective N 4.1. **Still outstanding:** the cross-check against IBKR's
  own VaR report — it is not part of the Flex Query, so it remains a manual comparison.
- [x] **4b–c — Screener + sector-neutral composite:** built; see Current status.
- [x] **4d — Validation (backtest reframed) and margin-of-safety sizing:** add gross profitability, Piotroski F-Score, 12-1 momentum, P/B < 1 flag (TSE reform); sector-neutral composite; backtest value-only vs. value+quality vs. value+quality+momentum; margin-of-safety sizing from DCF / residual income (banks).
- [x] **5 — Stress tests:** spring 2025 selloff, Aug 2024 yen carry unwind, March 2020, hypothetical (yen +10%, Nasdaq −15%). Compare to IBKR's stress test report.
  Replayed losses −27.4% / −19.4% / −21.9% of NAV, FX cushioning each by 3–5%.
  **Headline: "yen +10%" is +6.3% as pure translation, +3.6% with calm-period
  betas, −16.5% with betas from the Aug 2024 unwind.** **Still outstanding:**
  the comparison with IBKR's stress report, manual for the same reason as VaR.
- [x] **6 — Execution analysis:** fills vs. arrival price and VWAP, slippage in bps; state small-sample caveat.
  Three fills only (all Tokyo limit orders, March 2026), so a per-trade record with no
  averages. Marketable buys within ~1 tick of mid (+2.5, −2.3 bps); commission + tax
  8.7 bps dominates. **Headline is methodological: one-minute quote bars said +18.0 bps
  arrival slippage for 8306.T; five-second bars say −2.3.**
- [~] **7 — Reporting & packaging:** one-page daily HTML report, README with methodology + sample output (weights only), LinkedIn write-up.
  Report, README and a single entry point (`python src/run_daily.py`) done. **Still to do: the LinkedIn write-up.**

## Known traps
- **Asynchronous closes:** Tokyo closes ~13h before New York. Same-date correlations understate US–JP co-movement → use weekly returns or lag JP prices.
  Tested as a cause of Phase 2's residual and **refuted** — lagging JP prices made
  tracking error 4× worse. Still expect it to matter for correlations in Phase 3.
- **Different market holidays:** forward-fill prices, never invent returns.
  Hit this literally in Phase 2 (skipped, not forward-filled, silently biased P&L by
  ~14% of the reconciliation gap for one symbol alone) — fixed, see NOTES.md.
- **Corporate actions and dividends** break naive P&L — handle explicitly from Flex.
- **Look-ahead bias:** assume ~3-month lag before annual fundamentals are usable.
- **Survivorship bias:** today's universe excludes failed companies — state it in README.
- **Limited fundamentals history** in yfinance (~4 years) → fundamental backtests are demonstrations, not proof.
- Live Tokyo quotes via API may need a paid subscription; delayed/historical data is fine.

## Current status
Phases 0–6 done; Phase 7 done except the LinkedIn write-up.
- `src/data/flex.py`: downloads the Flex Query (with a retry wrapper around
  ib_async's polling, see NOTES.md), parses positions/trades/cash
  transactions/NAV history/corporate actions/cash report into DataFrames,
  caches raw XML under `data/flex_raw/`.
- `src/storage/db.py`: SQLite schema (10 tables) + idempotent upsert helper.
- `src/data/market_data.py`: yfinance FX (USDSGD direct, JPYSGD derived),
  benchmark (SPY, 1306.T) history, and per-holding daily price history
  (holding_prices) for every symbol ever held/traded. Drops mis-scaled
  price points (>2x from their own 5-day centred median) at ingestion --
  two bad days in 1306.T inflated the benchmark's volatility from 1.3%
  to 43% and drove its beta to nearly zero, see NOTES.md.
- `src/checks/reconcile.py`: sums stored positions (in base currency) +
  cash + accruals and compares to Flex's own NAV. Ran live end-to-end:
  **exact match, 0.000000% difference.**
- `src/analytics/pnl.py`: reconstructs daily share-count history per
  symbol from trades (Flex only gives today's snapshot), then splits
  daily P&L into stock move / FX move / interaction (forward-filled
  across market holidays), dividends, and trading costs; realized vs.
  unrealized split by whether a symbol is still held; roll-up by
  currency. Reconciles to nav_history's actual change within ~7%
  (down from an initial 72% after fixing two real bugs — a
  double-counted pre-window realized P&L, and a missing forward-fill
  across market holidays — see NOTES.md for the full writeup, which is
  good methodology content in its own right).
- `src/data/factor_data.py` + `src/analytics/factors.py`: Kenneth French
  factor returns, and per-sleeve regressions with Newey-West HAC standard
  errors. Headline result in the Phase 4a entry above.
- `src/analytics/risk.py`: historical and parametric VaR (each with its
  tail observation count), base-currency benchmark betas, daily and
  weekly holding correlations alongside their pairwise observation
  counts, and concentration (max weight, HHI, effective N).
- `src/report/plots.py`: four charts under `reports/` — factor exposures,
  rolling betas, return distribution with VaR lines, correlation matrix.
  Embedded in the README.
- `src/data/fundamentals.py` + `src/screener/quality.py`: Phase 4b. Annual
  statements stored with an explicit `available_date` (fiscal year-end +
  90 days) so no read can see a filing before it was public, and four
  metrics on top — gross profitability, Piotroski F-Score, 12-1 momentum,
  and P/B with the TSE-reform flag for Tokyo listings. The F-Score is
  withheld entirely for financial issuers rather than part-scored: see
  NOTES.md on why seven of nine computable tests would still have been
  the wrong answer for 8306.T.
  **Result: the screen independently confirms Phase 4a.** One of five
  names trades below book, nothing scores above 5 on the F-Score, and the
  two biggest gainers are the two strongest momentum names. Two
  independent methods — return covariance and financial statements — now
  agree this is not academic value.
- `config/peers.py` + `src/screener/universe.py` + `fetch_universe.py` +
  `composite.py`: Phase 4c. Hand-curated peer groups, every candidate
  verified against its own reported sector and instrument type before
  use (it caught two tickers reused by ETFs after their companies were
  acquired, eleven delistings, and several of my own misclassifications).
  132 symbols: 6 holdings + 126 verified peers. Metrics become percentiles
  within each holding's own peer group; a composite is withheld when a
  sleeve it names does not exist.
  **Result: the two biggest gainers were sector beta, and lagging it** —
  8306.T +62.7% against a Japanese-bank peer median of +86.5% (22nd of
  23), 9101.T +44.5% against a shipping median of +67.3% (19th of 20).
  Sector-neutrally the book is still not value-tilted: four of six sit
  below their sector's median book-to-market. Third independent method,
  same answer as Phase 4a.
- `src/screener/validate.py`: Phase 4d. The time-series backtest the build
  order asked for is not supportable on four annual statements (three
  rebalances), so the question is asked across companies instead: score
  the universe as of a past date using only then-available fundamentals,
  and test the ranking against the next year's return relative to each
  peer group's median. Two non-overlapping windows, reported separately.
  **Result: adding quality to value made it worse in both windows.** Value
  alone +0.183 / +0.091 Spearman; value+quality +0.051 / −0.028. Gross
  profitability was negatively correlated with within-sector
  outperformance both years. Value was the only signal to hold a
  non-negligible sign across both; momentum flipped.
  This cuts against the book: phases 4a–4c all found it is *not*
  value-tilted, and 4d finds value is the one signal with consistency
  behind it. Caveats in NOTES.md are substantial — crude quality proxies,
  two draws from one regime, survivors only, optimistic p-values.
- `config/valuation.py` + `src/sizing/intrinsic.py` +
  `tests/test_intrinsic.py`: Phase 4 sizing. DCF on owner cash flow
  (operating cash flow − capex − SBC) for non-financials, residual income
  for 8306.T, each also solved backwards for what the price assumes.
  All assumptions (risk-free rates, ERP, terminal growth, scenarios,
  sizing rule) are stated in the config, not estimated.
  **Result: one of five holdings trades below base-case value** —
  5105.T at 0.63x (37% margin of safety, holds in the bear case and up to
  an ~11% cost of equity). 8306.T 1.69x, NVDA 3.00x, SHOP 11.74x; 4180.T
  withheld (owner cash flow negative in 4 of 4 years). The rule would
  hold 82% cash. Fifth method, same answer as 4a–4d. The reverse DCF is
  the more honest read: NVDA's price needs about half its delivered
  growth, SHOP's needs three times it. See NOTES.md for limitations
  (hand-typed rates, constant margins).
- `config/scenarios.py` + `src/analytics/stress.py` +
  `tests/test_stress.py`: Phase 5. Historical replay of today's weights
  (stocks and cash by currency) through an episode window, reporting the
  worst drawdown on the portfolio's own peak/trough dates, split into
  stock / FX / cross. Hypothetical shocks reported three ways: direct
  only, with two-year weekly ("calm") betas, and with betas observed in
  the matching episode. Long history lives in its own `scenario_prices`
  table (fetched by market_data.py) so Phase 2/3 numbers are untouched.
  4180.T did not trade in 2020 and is proxied by beta × 1306.T, with the
  proxied share of NAV printed. Results in the Phase 5 entry above; the
  calm-vs-episode gap is the interview point (calm yen betas have
  R² ≤ 0.04; in Aug 2024 every holding fell 20–31%). See NOTES.md.
- `config/execution.py` + `src/data/ibkr_live.py` + `src/analytics/tca.py`
  + `tests/test_tca.py`: Phase 6. `ibkr_live.py` connects `readonly=True`
  and only calls qualifyContracts / reqHistoricalData: one-minute TRADES
  and MIDPOINT bars per trade date plus five-second MIDPOINT bars around
  each order, stored in `intraday_bars`; exchange daily volume (yfinance)
  in `exchange_volume`. `trades` now stores `order_time` (added to
  existing databases by `db.connect()` via `ADDED_COLUMNS`). `tca.py`
  reports slippage vs. arrival mid, interval VWAP, day VWAP and close.
  Things to remember: Flex timestamps are US Eastern (verified by a
  fill-inside-its-bar check on every run); TSEJ has no market-data
  permission so bars come via SMART, and their volume is checked against
  the exchange's (9% for 9101.T on 2026-03-03, so that VWAP is flagged);
  IBKR keeps five-second bars ~6 months, so run `ibkr_live.py` soon after
  any trade; ib_async's logger is silenced there because Gateway messages
  include the account number. See NOTES.md.
- `src/report/build_report.py`: Phase 7. Renders
  `reports/daily_report.html` — one self-contained page (inline CSS and
  SVG, no JS), percent of NAV / weights / bps only, enforced by each
  section converting to fractions of NAV before returning. A section with
  missing inputs prints a one-line reason instead of failing the page.
  `reports/daily_report.png` is a screenshot for the README (regenerate
  with headless Chrome after a rebuild if the README image should match).
- `src/run_daily.py`: the single entry point. Flex → market data →
  reconcile → intraday bars (skipped if Gateway is down) → report.
  Continues past a failed step, prints a summary, exits non-zero on
  failure. Factor data, fundamentals and the peer universe are not part
  of the daily run.
All build phases are complete. Remaining: the LinkedIn write-up.

Still worth doing before the project is presentable: `tests/` covers only
the valuation, stress and execution maths (plain asserts; pytest is not
installed); and `reconcile.py` prints the account number and NAV to the
terminal, which `run_daily.py` would carry into any scheduler log.

## Notes
Keep a running `NOTES.md` of problems hit and how they were solved.
