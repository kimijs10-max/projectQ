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
- [ ] **3 — Risk:** historical + parametric VaR (95/99%), beta to S&P 500 and TOPIX, correlation matrix, concentration (max weight, HHI). Compare to IBKR's own VaR report as a sanity check.
- [ ] **4b–d — Screener upgrade, backtest, sizing:** add gross profitability, Piotroski F-Score, 12-1 momentum, P/B < 1 flag (TSE reform); sector-neutral composite; backtest value-only vs. value+quality vs. value+quality+momentum; margin-of-safety sizing from DCF / residual income (banks).
- [ ] **5 — Stress tests:** spring 2025 selloff, Aug 2024 yen carry unwind, March 2020, hypothetical (yen +10%, Nasdaq −15%). Compare to IBKR's stress test report.
- [ ] **6 — Execution analysis:** fills vs. arrival price and VWAP, slippage in bps; state small-sample caveat.
- [ ] **7 — Reporting & packaging:** one-page daily HTML report, README with methodology + sample output (weights only), LinkedIn write-up.

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
Phases 0, 1, and 2 done.
- `src/data/flex.py`: downloads the Flex Query (with a retry wrapper around
  ib_async's polling, see NOTES.md), parses positions/trades/cash
  transactions/NAV history/corporate actions/cash report into DataFrames,
  caches raw XML under `data/flex_raw/`.
- `src/storage/db.py`: SQLite schema (9 tables) + idempotent upsert helper.
- `src/data/market_data.py`: yfinance FX (USDSGD direct, JPYSGD derived),
  benchmark (SPY, 1306.T) history, and per-holding daily price history
  (holding_prices) for every symbol ever held/traded.
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
Next: Phase 4a — factor exposure (regress against French factors), per the
build order (done early, before Phase 3).

## Notes
Keep a running `NOTES.md` of problems hit and how they were solved.
