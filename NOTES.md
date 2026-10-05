# Notes

Running log of problems hit and how they were solved.

Account identifiers are redacted: `ACCOUNT_A` is the trading account,
`ACCOUNT_B` a second account linked to the same login.

## Phase 0 — Setup (2026-09-28)

- **VSCode "environment file is configured but terminal environment injection is
  disabled" notice when saving `.env`.** Harmless — it's the Python extension
  saying it won't auto-inject `.env` vars into VSCode's integrated terminal.
  Doesn't affect the scripts, since they load `.env` themselves via
  `python-dotenv`.

- **`ssl.SSLCertVerificationError: unable to get local issuer certificate`
  when `test_flex.py` called `urlopen()`.** Standard python.org macOS
  framework-build issue — it doesn't use the system keychain for CA certs.
  Fixed by running `/Applications/Python 3.14/Install Certificates.command`,
  which installs `certifi` and symlinks its CA bundle as the framework
  Python's default cert file. One-time fix, applies to all venvs built off
  that framework Python.

- **First `test_flex.py` run returned only a `FlexStatementResponse` topic
  (no `Position` data), second run succeeded with full `FlexQueryResponse`.**
  Two separate bugs, both fixed properly in Phase 1's `flex.py`:
  1. `test_flex.py` looked for a topic called `"Position"` — the real tag is
     `"OpenPosition"`. Would've failed even on a successful download.
  2. `ib_async.FlexReport.download()` polls IBKR until ready, but its loop
     only checks whether the response looks like a "still generating"
     message — it doesn't verify the final response is actually a
     `FlexQueryResponse`. On a transient response it can break out of the
     loop early and hand back something else entirely (its root tag was
     `FlexStatementResponse`, the status-wrapper tag, not real data).
     `flex.py`'s `download_report()` now wraps it with its own retry that
     checks the root tag before accepting the result.

- **`Error 10275: Positions info is not available for account(s): ACCOUNT_B
  until the application is finished and approved.`** during `test_live.py`.
  This account ID does *not* match the trading account (`ACCOUNT_A`) that
  Flex and Gateway both report positions for — it's a second account linked
  to the same login without API/position permissions approved. Didn't block
  position data for the real account, but did stop `accountSummary()` from
  returning a `Currency` value, so the "Account base currency" print line
  was silently skipped. Confirmed base currency (SGD) from the Flex
  `EquitySummaryInBase` currency attribute instead.

- **VSCode Python extension error:** `Failed to run python -m pip list
  --format=json`. Caused by no bare `python` on PATH (only `python3`) and no
  interpreter selected for the workspace. Fixed with
  `.vscode/settings.json` → `"python.defaultInterpreterPath":
  "${workspaceFolder}/.venv/bin/python"` (gitignored, local only).

## Phase 1 — Data pipeline (2026-09-29)

- **Every `pandas`/`numpy` import hung indefinitely** (near-zero CPU,
  looked "stuck" rather than crashed) the moment Phase 1 code touched
  `storage/db.py`. Root cause found via `brctl status` mid-hang: **this
  whole project lives under `~/Desktop`, which is iCloud-synced.** Every
  first-time `.pyc` bytecode write during import triggers an iCloud sync
  round-trip for that file; `pandas` alone has hundreds of submodules, so
  imports could take minutes or never finish. `brctl status` caught it
  live, blocked on a sync op for a file under
  `.venv/.../pandas/core/arrays/__pycache__`.
  **Fix:** moved `.venv` out of the iCloud tree to `~/.venvs/portfolio-risk`
  (outside `~/Desktop`/`~/Documents`, which are the only iCloud-synced
  folders) and symlinked `.venv -> ~/.venvs/portfolio-risk` at the project
  root, so every script/path referencing `.venv/bin/python` kept working
  unmodified. Reinstalled `requirements.txt` there; `import pandas` went
  from "never finishes" to ~4s (first run, bytecode compile) / <1s after.
  **Side effect:** `.gitignore`'s `.venv/` line stopped matching, because
  git's trailing-slash directory patterns don't match symlinks. Changed to
  `.venv` (no slash).
  **Also noticed:** disk was 97% full (5.9GB free of 228GB) — not the cause
  here, but worth keeping an eye on since it can make iCloud eviction/sync
  behavior worse.
  **Takeaway:** don't put `.venv` (or any dir with many small files that
  get rewritten, like `__pycache__` or `node_modules`) inside
  `~/Desktop` or `~/Documents` on a Mac with iCloud Drive's "Desktop &
  Documents" sync on.

- **Reconciliation confirmed exact.** `computed_stock` (sum of
  `position_value * fx_rate_to_base` across positions) matched Flex's own
  `stock` figure to the last decimal, as expected since both use Flex's
  own numbers. The full NAV total needed `dividend_accruals` and
  `interest_accruals` added to `cash + stock` — without them there was a
  ~0.02% gap that looked like a bug but was just a missing NAV component.
  With all four included: `computed_total == reported_total` exactly, live
  end-to-end.

- **`.gitignore`'s `data/` rule was silently excluding `src/data/` too.**
  Git's `data/` pattern (no leading slash) matches any directory named
  `data` anywhere in the tree, not just the top-level one — so
  `flex.py`, `market_data.py`, `test_flex.py`, and `test_live.py` (all
  under `src/data/`) were never actually stageable, with no visible
  error (`git status` just doesn't list files that are ignored).
  `git add -n src/` catching only 2 of 6 files is what surfaced it.
  Fixed by anchoring to the repo root: `/data/` instead of `data/`.
  Worth remembering for any future `.gitignore` rule aimed at one
  specific top-level folder.

- **`JPYSGD=X` on yfinance is unreliable** — returned only 1 data point
  over a 1-year window, vs. full daily history for `USDSGD=X` and `JPY=X`
  (USDJPY). `market_data.py` derives JPY/SGD as `USDSGD / USDJPY` instead
  of pulling the cross pair directly.

## Phase 2 — P&L attribution (2026-09-29)

- **Flex has no per-holding daily history.** `OpenPosition` is a
  point-in-time snapshot, `EquitySummaryByReportDateInBase` (nav_history)
  is portfolio-level only. To get a daily local-currency return per
  symbol, `pnl.py`'s `reconstruct_quantity_history()` rebuilds each
  symbol's daily share count by starting from today's known quantity
  and undoing each trade in `trades` in reverse chronological order.
  Works for any date within the 365-day trades window, for any symbol
  with a trade in that window or a current position, regardless of
  when the position was actually opened.

- **First reconciliation attempt was off by 72% of the true
  change.** Two stacked bugs, found by walking the
  gap down component by component rather than guessing:
  1. **Double-counted realized P&L (~72% of the gap).**
     `fifo_pnl_realized` on a closed trade (9101.T, sold with no
     matching BUY in our 365-day trades window) is IBKR's realized
     gain since the position's *original* cost basis -- which predates
     our window. Adding it on top of `compute_mark_to_market()`'s
     already-computed in-window gain for that same position
     double-counted the in-window slice and pulled in pre-window gains
     that don't belong in a rolling-window P&L at all. Fix: excluded
     `fifo_pnl_realized` from `daily_summary()`'s total; it's kept
     separately via `compute_trading_pnl()` as a reference/cross-check
     figure, clearly labeled as spanning pre-window history.
  2. **Missing forward-fill across market holidays (~14% of the gap,
     from NVDA alone).** This is literally the "Known trap" already written down
     in CLAUDE.md ("Different market holidays: forward-fill prices,
     never invent returns") -- and the bug walked right into it. When
     one market is closed (e.g. US Thanksgiving) but the portfolio's
     date grid still has a row for that day, the naive day-pair lookup
     found no price on both sides of that date and silently skipped
     *both* adjacent day-pairs, dropping that period's real price move
     entirely rather than carrying the last known price forward. Since
     which days got dropped was arbitrary, it could push the total
     either direction -- confirmed by manually checking NVDA alone:
     chaining only the "covered" day-pairs overstated the true
     endpoint-to-endpoint change by ~24%, purely from silently
     dropping a decline that
     happened on a skipped day. Fixed by forward-filling both price
     and FX series across the full date grid in
     `compute_mark_to_market()` before differencing.
  These two fixes closed the gap from 72% to ~7% of the true
  change.

- **Residual gap diagnosed properly (2026-09-29, after adding
  `daily_returns()`).** Characterising it as a "~7% gap" was
  misleading. Comparing the attributed daily return against the
  NAV-implied daily return *per day* rather than cumulatively shows
  the real picture:
  - mean difference **0.44 bps/day**, t-stat **0.25** -> statistically
    indistinguishable from zero. **The attribution is unbiased.**
  - daily tracking error (std) **28.56 bps**; the two return series
    correlate at **0.967**.
  - mean/std ratio 0.015 -- the cumulative residual is the endpoint
    of ~261 days of accumulated zero-mean noise, not a systematic
    error. Quoting the cumulative figure as a "7% error" overstates it.
  - lag-1 autocorrelation of the difference: **-0.4393**, i.e. an
    error one day tends to reverse the next. That is the signature of
    a *timing* mismatch, not a valuation mistake.

  Two timing hypotheses tested directly (scratch experiments, not
  committed):
  1. **Tokyo price lag -- REFUTED.** Lagging JPY-listed prices one day
     made things much worse: std 28.56 -> 112.15 bps, series
     correlation 0.967 -> 0.487. The existing alignment is already the
     better one, so the JP/NY asynchronous close is *not* the driver
     here (contrary to the initial guess recorded above).
  2. **FX snapshot timing -- CONFIRMED as the source of the serial
     structure.** Lagging the FX series one day takes the lag-1
     autocorrelation from -0.4393 to **+0.0710** (i.e. essentially
     zero), with correlation roughly unchanged (0.964) and cumulative
     difference down by ~30%. IBKR converts to base at its own
     FX snapshot instant; Yahoo's daily FX close is stamped at a
     different one, and that offset is what makes the daily error
     reverse. Note std ticks slightly *up* (28.56 -> 30.01), so this
     was left out of production code for now -- worth revisiting when
     Phase 3 starts, since serial independence of errors matters more
     for VaR than raw variance does.

  **Conclusion:** the remaining ~29 bps/day of tracking error is
  mostly IBKR's own mark price vs. Yahoo's closing print, which can't
  be closed without IBKR historical marks. Defensible summary for a
  writeup or interview: *unbiased, 0.967 correlation against the
  broker's own NAV, with the residual's serial structure traced to FX
  snapshot timing rather than valuation error.*

- **Earlier (superseded) characterisation of the same residual, kept
  for the record since the reasoning process matters:**
  Two known, unquantified-in-full contributors:
  1. **Trade-day mispricing (~31% of the residual).** On the day a trade opens or
     closes a position, `compute_mark_to_market()` anchors to that
     day's *close* price (from `holding_prices`), not the actual
     *trade execution* price -- so the gain/loss between execution and
     close on entry/exit days is missing. IBKR's own `mtmPnl` field on
     each `Trade` row shows this directly, and the three trades'
     combined figure matches what the model omits on those days.
  2. **Likely JP/US date-alignment (the remaining ~69%).** Another
     instance of the same "Known trap" -- Tokyo closes ~13h before New
     York, so Yahoo's per-market trading calendar for JPY-listed
     symbols may not line up exactly with whatever "as of" convention
     IBKR uses for `nav_history`'s daily snapshot. A one-day lag/lead
     on JPY positions, compounded over a volatile year, is the leading
     suspect but wasn't directly confirmed. Possible secondary
     contributor: IBKR's own mark price vs. Yahoo's closing print can
     differ slightly, especially for JP stocks after hours.
  **Why not chased further:** unlike Phase 1 (reconciling against
  Flex's own pre-computed numbers, so an exact match was achievable),
  Phase 2 reconstructs price history from a third-party source
  (yfinance) that structurally can't match IBKR's internal marks to
  the cent. A small, disclosed, understood-in-nature residual is
  normal here and is itself legitimate methodology content for a
  "Known traps" writeup, rather than a bug to eliminate.

## Phase 2 follow-ups (2026-09-30)

- **`percentOfNAV` from Flex is a share of the equity sleeve, not of NAV.**
  The five position rows sum to exactly 100.00 while cash is ~12% of the
  account, so the field ignores cash despite its name. Caught while
  building the README's weight table — taking it at face value would have
  published weights that quietly overstate every holding by ~14%
  relative. Compute weights as `position_value * fx_rate_to_base / nav
  total` instead.

- **`.env.example` had gone missing** from the working tree (not
  gitignored, just absent) despite being referenced in CLAUDE.md's repo
  structure and needed by the README's setup steps. Recreated. Worth a
  check that documented files actually exist before pointing readers at
  them.

## Phase 4a — Factor exposure (2026-09-30)

- **Kenneth French daily data exists for Japan, which was not assumed.**
  The worry going in was that international factor sets are published
  monthly, which would have left ~12 observations against 6 predictors
  — statistically meaningless. Probed the library first:
  `Japan_5_Factors_Daily_CSV.zip` and `Japan_MOM_Factor_Daily_CSV.zip`
  both exist, so both sleeves get ~230 daily observations.

- **French CSV format quirks, all verified against the live files
  rather than assumed** (they differ between the US and Japan sets):
  preamble length differs, so the header is located as the first line
  beginning with a comma; values are published in percent; missing
  values are `-99.99` or `-999`; Japan pads its date column with
  trailing spaces; the momentum factor is `Mom` for the US but `WML`
  for Japan; a copyright line trails the data. Parser takes only rows
  whose first field is an 8-digit date, which also skips any
  annual-frequency table appended below the daily one.

- **The library lags about a month.** Factor data ends 2026-08-31
  while the portfolio series runs to 2026-09-28, so the regression
  sample stops a month short of the present. Not a bug, but it means
  the newest month is never in the regression.

- **Local currency against local factors.** Each sleeve is regressed in
  its trading currency against its own region's factors. Regressing
  SGD-denominated returns on USD-denominated factors would push the FX
  move into the residual and corrupt alpha, since the factors cannot
  explain currency. Phase 2's separation of `r_local` from `r_fx` is
  what makes this free. Within a sleeve, weighting by base-currency
  value is still valid for local returns because all holdings share
  one currency and the FX rate cancels out of the weighted average.

- **Newey-West HAC standard errors reported alongside OLS.** Daily
  residuals are serially correlated, which inflates OLS t-statistics.
  Phase 2 already found serial structure in this same data, so
  assuming it away here would have been inconsistent. Truncation lag
  from the usual rule of thumb, `4*(n/100)^(2/9)`, giving 4. Hand-rolled
  OLS with numpy rather than adding statsmodels/scipy; p-values use the
  normal approximation, immaterial at n≈230.

- **Sleeve return code validated by hand** before trusting any
  regression output: recomputed one day's USD sleeve return directly
  from `holding_prices` with weights taken from the prior day's values,
  and it matched the engine to 10 decimal places.

- **Result: neither sleeve shows a positive HML (value) loading.**
  USD −0.41 (t = −1.78 HAC, p = 0.075), JPY −0.07 (t = −0.24). The
  stated strategy is firm-foundation / intrinsic value, which predicted
  positive HML, so this is a falsification of the prediction as stated.
  **The nuance that matters:** HML is constructed on book-to-market,
  whereas firm-foundation theory values discounted future cash flows.
  Those are different constructs — a DCF-based investor can rationally
  own a high book-multiple name if future cash flows justify the price.
  So the defensible conclusion is not "the value thesis failed" but
  "the strategy as implemented is not academic-HML value", which is a
  sharper and more honest statement. Worth resolving properly in Phase
  4b–d, where the screener builds explicit value/quality metrics that
  can be compared against these loadings.

- **Other loadings.** USD sleeve is dominated by market beta 1.53
  (t = 12.2) with R² 0.59 — a large part of that sleeve is simply
  leveraged market exposure. It also shows CMA −0.93 (t = −2.30,
  p = 0.02), i.e. a tilt toward aggressive-investment firms, consistent
  with high-capex growth names. JPY sleeve is market beta 0.78
  (t = 4.80) with R² only 0.23, so it is far more idiosyncratic — as
  expected for three concentrated names.

- **Alpha is not significant and should not be claimed.** Both sleeves
  show roughly +10%/yr compounded intercepts, but t = 0.33 (JPY) and
  0.50 (USD). One year of daily data cannot distinguish that from zero;
  the standard error on an annualised alpha at this sample size is far
  too wide. Stating the point estimate without the t-statistic would be
  the single easiest way to get caught out on this project.

## Phase 3 — Risk (2026-09-30)

- **1306.T (TOPIX benchmark) had two days of corrupt prices from Yahoo,
  and it showed up as an impossible beta.** The first run reported TOPIX
  beta 0.004 alongside correlation 0.204 — not internally consistent,
  since beta = corr x (sigma_p / sigma_b), so those two together imply
  the benchmark is ~50x more volatile than the portfolio. Chasing it
  down: on 2026-03-30 and 03-31 the price sits at roughly a tenth of the
  surrounding level (375 -> 37 -> 36 -> 382), producing a -90% return
  followed by a +948% one. Yahoo reports **no split**, and the raw
  unadjusted `Close` carries the same break, so it is not an adjustment
  artefact that a different fetch setting fixes — it is bad source data
  for exactly two days. Those two points alone inflated the benchmark's
  daily vol from 1.28% to 43.3%.
  **Fix:** a spike filter at ingestion in `market_data.py` — any point
  more than 2x away from its own five-day centred median is dropped, and
  the return is then computed across the gap, which is the correct
  treatment. A five-day centred median is robust to a one- or two-day
  break and a 2x band is far outside any real single-day move for an
  index fund. Verified it removes exactly the two bad points and touches
  **none** of the seven holdings, so Phase 2 and 4a results were never
  affected. The two rows already in the database had to be deleted
  explicitly, since an upsert overwrites but never removes.
  **Lesson:** a figure that is internally inconsistent is worth more
  attention than one that merely looks surprising. The beta being small
  was not the tell; the beta being small *while the correlation was not*
  was.

- **`resample("W").prod()` fabricates returns for weeks with no data.**
  Found only because the pairwise observation counts were printed
  alongside the weekly correlation matrix: it claimed 53 weekly
  observations for **every** pair, including 5105.T vs 9101.T, which have
  no overlapping holding period at all (one was bought after the other
  was sold, and the daily matrix correctly showed NaN). Cause: `prod()`
  skips NaN by default, so a week with no observations returns 1.0,
  i.e. a fabricated 0% return. The weekly correlations were therefore
  computed partly on invented data, and it mattered — 9101.T/SHOP moved
  from 0.28 to 0.47 once fixed. **Fix:** `prod(min_count=1)`.
  **Lesson:** printing the observation count behind a statistic is
  cheap and catches things the statistic itself hides. Same principle as
  the VaR tail counts, and it paid off immediately.

- **The asynchronous-close test is inconclusive, and the observation
  counts say why.** The daily-vs-weekly correlation comparison was meant
  to expose the Tokyo/New York close gap. Cross-market pairs move both
  directions weekly, averaging about +0.03 — no clear effect. But the
  counts point at the reason: NVDA/SHOP is the only pair with the full
  53 weeks, and it is also the only one that barely moves (0.28 -> 0.29).
  Every pair that swings hard has 23-27 weeks behind it, where the
  standard error is around 0.19. So the instability is a sample-size
  artefact, not a real weekly-vs-daily difference. Revisit once there is
  more history; the daily matrix is better supported for now.

- **99% VaR is reported with its tail count and flagged unusable.** At
  261 observations the 99% quantile rests on 3 points. The figure is
  printed because omitting it invites someone to compute it themselves
  without the caveat, but it carries the count inline so it cannot be
  quoted innocently.

## Phase 3 charts (2026-09-30)

Two figures added to `src/report/plots.py`, same design constraints as the
Phase 4a charts.

**`return_distribution.png`** — histogram of daily base-currency returns
with both VaR estimates marked and a fitted normal drawn in grey.

The normal curve is deliberately *not* a coloured series. It is the
assumption under test, not a competing measurement, so it wears chrome
grey while the two VaR lines take the two categorical slots. That also
solved a layout problem: the two lines sit 0.12pp apart, far too close to
label in place, so their values live in a legend block and hue carries
which line is which.

Also fixed: the shaded tail region originally ran from the first histogram
bin rather than the axis edge, which put a visible vertical boundary at
−4% that read as a second, meaningless threshold.

**`correlation_matrix.png`** — daily and weekly panels, lower triangle
only, on a diverging blue↔grey↔red scale.

Diverging rather than sequential because correlation has a real zero and a
sign; a single-hue ramp would make −0.30 and +0.30 look like different
magnitudes of the same thing. Lower triangle only because the matrix is
symmetric and the diagonal is 1 by construction.

The design decision that matters: **every cell prints its observation
count.** That is not decoration. It is the same discipline that caught the
`resample().prod()` bug in the first place, and it is what makes the
chart's conclusion legible — every pair that swings between the daily and
weekly panels is also a pair carrying a thin-sample mark, which is
precisely why the asynchronous-close effect cannot be claimed from this
data. Without the counts the weekly panel would look like a finding.

Pairs with no overlapping holding period render as "no overlap" rather
than as an empty or zero-valued cell.

Layout bugs found by rendering and looking, not by reasoning:

- the sample-size footnote overflowed the right edge of the figure
- the colourbar's end labels, centred on the bar's ends, hung half off
  the figure
- the panel titles collided with the column headers
- one cell read `-0.00`

All four are invisible in code and obvious in the PNG.


## Phase 4b — Screener metrics (2026-10-01)

`src/screener/` did not exist; "screener upgrade" in the build order was
aspirational. Built from scratch: `src/data/fundamentals.py` (fetch and
store annual statements) and `src/screener/quality.py` (the four metrics).

New table `fundamentals`, long format
`(symbol, fiscal_date, available_date, statement, item, value)`.

### The reporting lag is stored, not assumed

`available_date` = fiscal_date + 90 days, written into the row rather than
applied at read time. A fiscal year ending 2026-03-31 is not public
knowledge on 2026-03-31; 8306.T files in June. Every read goes through
`fundamentals.as_of(knowledge_date)`, which filters on that column, so a
screen run for a past date cannot see a filing that had not happened.

The holdings already span three fiscal year-ends — March (8306.T),
December (4180.T, 5105.T, SHOP) and January (NVDA) — so this had to be
per-company off its own year-end, never a calendar rule.

### Three real problems, found by checking rather than by trusting

**1. Statements are out of sync at the source.** 5105.T has income and
cash-flow statements through 2025-12-31 but a balance sheet only through
2024-12-31. Verified directly against yfinance, so it is upstream, not a
parser bug.

The first version took the latest `fiscal_date` as the current year,
which for 5105.T selected a row with no balance sheet at all: gross
profitability, P/B and three F-Score tests all came back unavailable, even
though the fetcher had reported the symbol as having complete coverage.
That contradiction is what exposed it.

Fixed by anchoring every metric to the latest fiscal year with a complete
balance sheet, and reporting the staleness. The alternative — pairing the
2025 income statement with 2024 assets — would build a ratio out of two
vintages, and ROA already uses beginning-of-year assets, so vintage
consistency matters more than freshness. 5105.T now scores 5/9 with gross
profitability 0.319 and P/B 1.26.

**2. An absent line item is not a zero, and the data proves it.** SHOP's
`Long Term Debt` row is absent for 2024 and 2025. The tempting read is
"no debt, so zero". But 4180.T reports an explicit `0.0` for 2024 and a
positive figure for 2025 — so yfinance *does* write a real zero when it
means zero, and an absent row therefore means not reported. The
leverage test is left unscored for SHOP rather than passed by default.
Had I defaulted absent to zero, SHOP would have scored a free point.

**3. The F-Score does not apply to banks, and the missing fields are the
weaker half of the reason.** 8306.T (Mitsubishi UFJ) reports no cost of
revenue and no current/non-current balance-sheet split, which kills gross
profitability and two of the nine tests outright.

I initially described this as five of nine tests lost. That was wrong —
the direct count is two (gross margin, current ratio), leaving seven
computable. But computing those seven would have been the real mistake.
A bank's operating cash flow tracks changes in loans and deposits:
8306.T reports roughly **−¥23tn**, which says nothing whatever about
whether the bank is healthy. So the cash-flow and accruals tests would
have scored balance-sheet growth and labelled it earnings quality.

Piotroski's original sample excludes financial firms for exactly this
reason. The score is now withheld entirely, with a reason, rather than
reported as 4/7 with a caveat — 4/7 sitting beside a genuine 4/7 reads as
a weak company rather than an unmeasured one.

Detection is structural (no cost of revenue *and* no current/non-current
split, across all years) rather than a sector string, because the absence
of both concepts is the balance sheet itself saying what the company is,
and a sector label can be missing or wrong. It also generalises to any
issuer whose statements lack the inputs. VNQ, an ETF held earlier in the
window, returns no statements at all and drops out before this point.

### What the screen says about the book

| | gross prof. | F-score | 12-1 mom. | P/B |
|---|---|---|---|---|
| 4180.T | 0.389 | 5/9 | −4.1% | 3.83 |
| 5105.T | 0.319 | 5/9 | +1.4% | 1.26 |
| 8306.T | — | withheld | +62.7% | 1.88 |
| 9101.T | 0.083 | 4/9 | +44.5% | 0.95 |
| NVDA | 0.742 | 4/9 | +16.3% | 35.29 |
| SHOP | 0.366 | 5/8 | −6.5% | 14.35 |

Five names is a description, not evidence, and nothing here is a
statistic. But the description is consistent and it confirms Phase 4a
from independent data.

- **Not value on book multiples.** One name of five trades below book.
  Two trade at 14x and 35x.
- **Not high quality on the F-Score.** Nothing scores above 5. Piotroski
  treats 8–9 as high; the book has no high-F-Score name in it.
- **The two biggest gainers are the two strongest momentum names**
  (8306.T +63%, 9101.T +45%).

Phase 4a's factor regression found no positive HML loading. This screen
reaches the same conclusion from financial statements rather than return
covariance — two independent methods, one answer. It also adds what 4a
could not: the book looks closer to momentum plus a US quality/growth
tilt (NVDA's 0.742 gross profitability is genuinely exceptional) than to
value on any academic definition.

The same caveat as Phase 4a still applies and still matters: P/B *is* the
book-to-market construct that firm-foundation investing does not use, so a
high multiple is not by itself evidence of departing from the strategy — a
DCF investor can rationally own a 35x-book company. The defensible claim
is the narrow one, now doubly supported: **this is not academic value.**
Whether it is *good* is a different question, and neither phase answers
it.

One profile does deserve flagging on its own terms. 9101.T is cheap
(P/B 0.95, TSE reform flag set), low quality (gross profitability 0.083),
and middling on the F-Score at 4/9. Cheap plus weak fundamentals plus
strong recent price is the "cheap for a reason" profile the F-Score exists
to separate out, and 4/9 is not reassuring about which side of that line
it sits on.


## Phase 4c — Sector-neutral composite (2026-10-01)

Peer groups in `config/peers.py`, verification in `src/screener/universe.py`,
bulk fetch in `src/screener/fetch_universe.py`, scoring in
`src/screener/composite.py`. Two new tables: `security_meta` (issuer
classification) and `screen_prices` (prices for the screening universe,
deliberately separate from `holding_prices` so that table keeps meaning
"things this account held").

Universe: 6 holdings + 126 verified peers = 132 symbols.

### Curate by hand, verify by machine

There is no free source of clean sector membership for a mixed
Tokyo/US/Greek/Danish universe, so the peer lists are hand-written. That
makes them the weakest link in the whole screen: a mistyped or repurposed
ticker sits silently inside a group and shifts every percentile computed
in it. So nothing curated is trusted — every candidate must resolve to an
equity whose reported sector matches its holding's, and the rejects are
printed.

It caught more than expected.

**Ticker reuse, which is survivorship bias with teeth.** `EGLE` was Eagle
Bulk Shipping until Star Bulk acquired it; it is now a Global X S&P 500
ETF. `GOGL` was Golden Ocean until the CMB.TECH merger; it is now a *2x
leveraged Google ETF*. Both return live price data and would pass any
"does this ticker resolve?" test. Only the instrument type gives them
away, so the equity check is now explicit rather than incidental — the
first version excluded them only because ETFs happen to report no sector,
which is luck, not design.

**Eleven genuinely dead tickers**, including five Japanese regional banks
(Shizuoka, Kyoto, Hiroshima, Chugoku, Iyo) that reorganised into holding
companies under new codes. Their successors are now in the list.

**Six tanker operators rejected as Energy rather than Industrials.** This
is correct taxonomy, not a glitch: crude and product tanker operators
classify under Energy, while dry bulk, container and car carriers sit
under Industrials/Marine Shipping. NYK is the latter, so the rejection
kept the comparison inside one cycle instead of blurring two. The
classification encoded a distinction I was about to lose.

**Several of my own misclassifications** — DeNA, Mercari, Nexon,
CyberAgent, Etsy, eBay, PayPal and AppLovin all sit outside their
intended holding's sector, and 4588.T turned out to be Healthcare.

### A null price that produced a plausible answer

First composite run returned **no value sleeve for any Japanese holding**
— four of six. Not an error, just four blanks, and the output read as a
reasonable "insufficient data" result.

It was wrong. The holdings-only screen an hour earlier had produced P/B
for all four, which is the only reason it got questioned.

Cause: the fetch ran on 2026-10-01 while the Tokyo session was unsettled,
and yfinance returns a row dated today with a null close for every
Japanese listing. 58 such rows went into `screen_prices`. `_price_asof`
took the last row on or before the target date without checking for
nulls, so every as-of price lookup for a Japanese name returned NaN, and
P/B went with it. Gross profitability was unaffected because it never
touches a price, which is exactly why the failure looked selective and
therefore plausible.

Fixed in two places. At ingestion, `market_data._download_close` now drops
null closes before anything else — it is not a missing day, it is a day
that has not happened yet. At lookup, `_price_asof` drops nulls before
selecting, because an as-of function should never return one regardless
of what is stored.

`benchmark_prices` also held one null close. Phase 3 turned out to be
unaffected — `_benchmark_base_returns` already calls `.dropna()` on the
joined frame — and re-running risk.py reproduced every figure exactly
(vol 17.9%, VaR 1.90/1.78, beta 0.652/0.379). Verified rather than
assumed.

This is the third bug of the same family in this project: fabricated
zeros from `resample().prod()`, mis-scaled benchmark prints, and now null
closes. All three produced output that looked plausible. The pattern is
that bad data rarely announces itself — it is caught by a cross-check
against a number computed a different way.

### Results

Percentile within the holding's own peer group, 100 = best.

| | B/M | gross prof. | F | mom | value | +quality | +mom |
|---|---|---|---|---|---|---|---|
| 4180.T | 56 | 56 | 22 | 39 | 56 | 47 | 44 |
| 5105.T | 45 | 100 | 14 | 41 | 45 | 51 | 48 |
| 8306.T | 17 | — | — | 9 | 17 | withheld | withheld |
| 9101.T | 68 | 60 | 25 | 10 | 68 | 55 | 40 |
| NVDA | 13 | 100 | 7 | 39 | 13 | 33 | 35 |
| SHOP | 28 | 42 | 38 | 42 | 28 | 34 | 37 |

**The headline: the two biggest gainers were sector beta, and lagging
sector beta at that.**

| | holding | peer median | rank |
|---|---|---|---|
| 8306.T vs Japanese banks | +62.7% | +86.5% | 22 of 23 |
| 9101.T vs marine shipping | +44.5% | +67.3% | 19 of 20 |

Raw momentum said these were the book's two strongest names. Against
their own sectors they are close to the bottom. Japanese banks rallied on
rate normalisation and shipping on freight rates; the positions captured
the sector and gave back roughly 23–24 points of it. "I owned a stock that
rose 63%" and "I picked a good bank" are different claims, and the
sector-neutral view separates them.

Three further readings:

- **Sector-neutralising moves the value conclusion.** Raw P/B made 8306.T
  look moderate at 1.88x. Against Japanese banks it is 20th of 23 — on
  the expensive side of its own sector. Four of six holdings sit below
  their sector's median book-to-market, so the book is not value-tilted
  on a sector-neutral basis either. Third independent method, same
  answer as Phase 4a.
- **NVDA's F-Score percentile of 7 is not a quality verdict.** It has the
  best gross profitability in its group (100th percentile). The F-Score
  rewards year-on-year *improvement* and balance-sheet conservatism —
  falling leverage, rising current ratio, no dilution, rising margin and
  turnover — so a company already at peak margins and investing heavily
  scores badly even when the business is outstanding. Piotroski designed
  it for distressed value names. For NVDA the gross profitability reading
  is the informative one and the F-Score is the wrong instrument.
- **The composite columns do not test the strategy.** value averages 37.8,
  +quality 44, +momentum 40.8 across six names. With n=6 and no
  out-of-sample test these are descriptions, not evidence, and nothing
  about "quality improves the book" can be claimed from them. That is
  what the 4d backtest would be for.

### Design decision: when a sleeve is withheld

A sleeve named in a composite must exist. 8306.T gets a value score (17)
but no value+quality and no value+quality+momentum, because "value +
quality" computed without a quality measurement is the value score under
another label, and it would sit in the same column as composites that
genuinely carry both.

A sleeve may be *internally* partial and still form — quality blends
gross profitability and the F-Score ratio, and one of the two is enough,
with the component count reported. The distinction is deliberate: a
half-measured sleeve is noisier, an absent sleeve is not a measurement.

The F-Score ratio is only used when at least 7 of 9 tests were evaluable.
Below that, score-over-evaluable is too coarse to rank on — 3 of 4 and 6
of 8 are the same number from very different evidence.


## Phase 4d — Cross-sectional validation (2026-10-01)

The build order asked for a backtest of value-only against value+quality
against value+quality+momentum. Built literally that is not possible here:
yfinance supplies four or five annual statements, so after the 90-day
reporting lag there are three or four annual rebalances. Three
observations is an anecdote, and a weak backtest in the repo is worse than
none — a reader who notices the sample size discounts everything near it.

So the question is asked **across companies instead of across time**.
Every name in the universe is scored as of a past date using only
fundamentals available then, and the ranking is tested against the
following year's return *relative to its own peer group's median*. That
trades time-series depth, which this data lacks, for cross-sectional
breadth, which it has: ~130 names rather than three rebalances.

Returns are measured against the group median for the reason Phase 4c made
unavoidable — sector moves dominate single names over a year. The sector
medians over the two windows: Japanese banks +56.9% then +82.4%, shipping
−1.6% then +63.6%. A raw forward return would almost entirely measure
which industry was in favour.

### Two windows, not one

Initially one window. Extended to two non-overlapping years once the price
history was long enough, because the test's central weakness is being a
single draw, and a signal that reverses between adjacent years tells you
something a single window cannot. They are reported separately and never
pooled — pooling adjacent cross-sections inflates n without adding
independent information.

That required re-fetching prices at 5 years rather than 2. A 12-1 momentum
measured as of a date one year back needs two years of prices *before*
that date plus the forward year. With 2 years fetched, momentum was
unavailable for 130 of 132 names and every composite containing it was
withheld — correctly, by the sleeve rule, which is why it showed up as
blanks rather than as wrong numbers.

### Results

Spearman rho of each signal's within-group percentile against forward
excess return.

| signal | 2024-09 → 2025-09 | 2025-09 → 2026-09 | verdict |
|---|---|---|---|
| book-to-market | **+0.183** (p 0.035) | +0.091 | positive both |
| gross profitability | −0.124 | **−0.164** | negative both |
| F-Score ratio | −0.050 | −0.069 | negative, negligible |
| 12-1 momentum | −0.055 | +0.152 | **sign flips** |
| value | **+0.183** | +0.091 | positive both |
| value + quality | +0.051 | −0.028 | one negligible |
| value + quality + momentum | +0.045 | +0.097 | one negligible |

**The build order's question gets a clear answer, and it is the opposite
of the expected one: adding quality to value made it worse, in both
windows.** Value alone scored +0.183 and +0.091; value+quality +0.051 and
−0.028. Gross profitability had a negative rank correlation with
within-sector outperformance in both years, and in the second window the
tercile spread was −36.7% — the top third by gross profitability averaged
+0.7% against its sector while the bottom third averaged +37.4%.

This is consistent with the single most visible case in the book: NVDA has
the highest gross profitability in its peer group and returned −33.7%
against that group over the second window.

**Value was the only signal to hold a non-negligible sign across both
windows**, and the only one to reach nominal p < 0.05.

**Momentum flipped sign** (−0.055 then +0.152), which rules out reading
either window's momentum result on its own.

### What this does and does not support

It does *not* say Novy-Marx is wrong. The quality proxies here are crude —
annual gross profitability from a free data source, plus an F-Score ratio
available for only 82 to 108 of 132 names — against a literature built on
far better data and decades of history. Two adjacent windows are two draws
from one regime, dominated by the Japanese bank and shipping rallies. Every
name is a survivor, which is not hypothetical given that two candidate
tickers turned out to be ETFs sitting on the codes of acquired shipping
companies. And the p-values assume independent observations, which returns
in a single cross-section are not, so they are optimistic by an unknown
margin.

What it does support is narrower and still useful: **in this universe over
these two years, quality as measured here detracted from value rather than
adding to it, and value was the only signal that held its direction.**

That lands awkwardly against the book, and the awkwardness is the point.
Phases 4a, 4b and 4c all concluded independently that the portfolio is not
value-tilted — no positive HML loading, one of five names below book, four
of six below their sector's median book-to-market. Phase 4d now finds that
value is the one signal in this data with any consistency behind it. So the
strategy as implemented is underweighting the only factor the data
supports. That is a conclusion worth defending in an interview precisely
because it is not flattering.

### A labelling bug in my own verdict column

First run reported `value+quality+momentum` (+0.045, +0.097) as "sign
flips". Both coefficients are positive; one simply fell inside the 0.05
dead band I had added for negligible correlations, and my condition
conflated "too small to call a direction" with "changed direction". Fixed
by separating the two verdicts. Worth recording because it is the same
error in miniature as everything else in this log: a threshold doing one
job while being read as if it did another.


## Phase 4 — Intrinsic value and margin-of-safety sizing (2026-10-05)

The last piece of Phase 4, and the one that tests the philosophy directly:
put a value on each holding, compare it with the price, and ask what the
book would look like if size followed the discount. `src/sizing/intrinsic.py`,
assumptions in `config/valuation.py`, maths tested in `tests/test_intrinsic.py`.

### Two models

**DCF for non-financials**, on *owner cash flow*: operating cash flow
− capex − stock-based compensation. SBC is added back in operating cash
flow because it is non-cash, but owners pay for it in dilution. It is
22% of SHOP's operating cash flow and 6% of NVDA's, so leaving it in
would flatter exactly the names most likely to look expensive. Growth
fades linearly over ten years to a terminal rate; the discount rate is
CAPM in the stock's own currency with a Blume-adjusted weekly beta
against its own market.

**Residual income for 8306.T**: book value plus the present value of
profit above the cost of equity, with ROE fading to the cost of equity.
The reason is the same one that made the screener withhold its F-Score —
a bank's operating cash flow (+13tn, −10tn, +0.006tn, −23tn yen across
four years of steadily rising profit) measures deposit and loan flows,
not earnings. The routing reuses `screener.quality.is_financial`, the
structural test, rather than a sector label.

### The reverse DCF is the headline

A DCF on a fast grower is mostly its growth assumption. So every valuation
is also solved backwards for what the price assumes, and that is set
beside what the company has delivered:

| | price assumes | delivered | price / base value |
|---|---|---|---|
| 5105.T | cash flow *shrinking* 7.6%/yr at the start | revenue +6.2%/yr | 0.63x |
| 8306.T | permanent ROE of 15.0% | ROE 11.3% (cost of equity 8.4%) | 1.69x |
| NVDA | 51% starting growth, fading over 10 yrs | revenue +100%/yr | 3.00x |
| SHOP | 89% starting growth, fading over 10 yrs | revenue +27%/yr | 11.74x |
| 4180.T | withheld | | |

The two columns on the right disagree about NVDA, and the disagreement is
the useful part. The base case caps starting growth at 20%, which makes
the price look like three times value. But the price only needs about
half of the growth NVDA has actually produced to persist and then fade.
The 3.00x is a statement about my cap; the 51%-against-100% is a statement
about the stock. SHOP is the opposite case: it needs more than three times
its delivered growth, which can only come from margins expanding — and
this model holds the margin constant (see limitations).

### Results

One of five holdings trades below its base-case value. 5105.T (Toyo
Tire) at 0.63x, a 37% margin of safety that survives the bear case
(0.68x). It also survives the discount rate: the base value stays above
the price until the cost of equity reaches about 11.3%, against the 7.5%
CAPM gives. The sizing rule (half the margin of safety, nothing below
15%, capped at 25%) gives it an 18% target against the 16.5% of the
invested book it actually is — and gives everything else zero, leaving
82% in cash, against 62% of the invested book actually sitting in NVDA
and 8306.T.

That is a fifth method reaching the same answer as 4a–4d, and the first
one that uses the philosophy's own yardstick rather than a factor proxy
for it.

### Things hit while building it

**4180.T has no DCF value, and that is not a value of zero.** Appier's
owner cash flow is negative in all four years available (−5.7% of
revenue on average): capex alone exceeds operating cash flow every year.
A DCF on a negative base returns a negative number, and no growth rate
rescues it — the reverse solver correctly returns nothing. Reported as
withheld with no target weight, the same rule as the F-Score for banks.
The model is not saying Appier is worthless; it is saying a company still
investing more than it generates cannot be valued from current cash flow.

**Margin of safety is a bad display metric for anything expensive.**
(value − price) / value divides by the value, so a stock at 11.7x its
value shows as −1074%. Correct, and unreadable. The report now shows
price/value for every scenario and a margin of safety only where one
exists.

**One year of cash flow is hostage to working capital.** 5105.T's
operating cash flow went 15bn → 87bn → 67bn → 93bn yen. The cash-flow
margin is averaged over three years and applied to the latest revenue.
The cost is that it understates a genuinely improving business (SHOP's
margin is 13.5% in the latest year against a 10.2% average).

**Statement vintages differ.** 5105.T's balance sheet stops at 2024 while
its cash-flow statement runs to 2025. The screener anchors everything to
the older year for ratio consistency; here each item is read at its own
latest date, because a share count a year old is good enough to divide
by, and the note is printed.

### What this does and does not support

- The risk-free rates are typed in by hand (USD 4.25%, JPY 2.0%), not
  fetched. Every value moves with them.
- Margins are held constant. That is the right conservative default for a
  tyre maker and a real blind spot for a platform business whose bull
  case *is* margin expansion.
- 5105.T's three-year margin window may be a cyclical high (capex fell
  from 46bn to 27bn yen over it). The margin of safety is large enough to
  absorb a lot of that, but the test above stresses the discount rate,
  not the margin.
- The DCF is a flow to equity that ignores net borrowing and gives no
  separate credit for balance-sheet cash.
- Blume-adjusted betas of 1.57 and 1.89 put NVDA and SHOP at 12–14% costs
  of equity. Two years of weekly returns is a noisy basis for that.


## Phase 5 — Stress tests (2026-10-05)

VaR describes an ordinary bad day from the last year of returns. Phase 5
asks what today's book would lose in a named event. `src/analytics/stress.py`,
scenarios in `config/scenarios.py`, maths tested in `tests/test_stress.py`.
Everything is % of NAV in SGD.

### Historical replay

Today's weights carried through three past episodes on what each holding
and currency actually did, split with the Phase 2 identity
R_base = r_local + r_fx + r_local·r_fx:

| | peak → trough | loss | stock | FX | cross |
|---|---|---|---|---|---|
| COVID crash | 2020-02-19 → 03-16 | −27.4% | −29.9% | +3.4% | −0.9% |
| Yen carry unwind | 2024-07-10 → 08-05 | −19.4% | −23.1% | +4.8% | −1.1% |
| Spring 2025 selloff | 2025-02-18 → 04-07 | −21.9% | −24.2% | +3.0% | −0.6% |

These are 10–14 times Phase 3's one-day 95% VaR of 1.90%. The fairer
comparison scales VaR by √time: the COVID drawdown took 18 trading days,
and 1.90% × √18 ≈ 8.1%, against a replayed 27.4%. Square-root-of-time
scaling of a calm year's VaR understates the episode by a factor of
about 3.4, because it assumes the days are independent and a crash is
exactly the case where they are not.

**The yen cushioned all three.** 62.6% of NAV is in yen once cash is
counted (52.7% stock, 9.9% cash), and the yen rose against SGD in every
episode (+4.6%, +8.8%, +4.6%), giving back 3–5% of NAV. Three episodes
is not a law — the yen fell alongside equities through 2022 — but it is
the reason the losses above are smaller than the stock moves alone.

### The result worth the phase: "yen +10%" has three answers

| | direct only | + calm betas | + episode betas |
|---|---|---|---|
| Yen +10% | **+6.3%** | +3.6% | **−16.5%** |
| Nasdaq −15% | 0.0% | −11.7% | −17.0% |

*Direct* is pure translation: the yen rises, nothing else moves, and a
book that is 63% yen gains. *Calm betas* add each stock's sensitivity to
the yen measured over the last 104 weekly returns. *Episode betas* take
the sensitivity from what each stock did per unit of yen move in August
2024.

The calm betas say a yen rally is mildly good for this book. Their
R-squared is between 0.00 and 0.04: in ordinary weeks the yen explains
essentially none of these stocks' moves, so the regression returns
roughly nothing and the translation gain survives. In the one episode
where the yen actually moved 10.8% in under a month, every holding fell
20–31% — including NVDA and SHOP, which have no yen exposure at all,
because the same deleveraging hit everything.

So the sign of the answer depends on which of the three you believe,
and the reason matters: a linear beta from calm data cannot
represent a relationship that only exists when the move is forced. I had
written the engine with two columns and added the third only after the
second contradicted a replay printed twenty lines above it.

The Nasdaq shock shows the same thing less dramatically. NVDA and SHOP
barely change between calm and episode betas (1.46 → 1.40, 1.72 → 1.83).
The whole 5-point gap is the three Tokyo names, whose betas to QQQ
roughly double or triple (0.34–0.73 → 0.71–1.73). Diversification
across markets is real in ordinary weeks and mostly gone in a selloff.

### Design decisions and things hit

**Peak and trough are the portfolio's, not an index's.** The first
design had fixed dates per scenario. But whose? In April 2025 Tokyo
bottomed on the 7th and New York on the 8th, and this book holds both —
S&P dates would have measured the Tokyo half after a 6% rebound. Each
scenario is now a generous window, and the engine reports the worst
drawdown of the replayed portfolio inside it with the dates it found.
The trough it found for 2025 was the 7th.

**Long history lives in its own table.** The episodes need prices back
to 2020; holding_prices, benchmark_prices and fx_rates hold two years and
Phase 2 and 3 read them whole. Widening them would have silently changed
every volatility, beta and correlation already reported. A separate
`scenario_prices` table costs some duplicated public data. Checked after
the fetch: Phase 3 still reports 17.9% annualised vol and 1.90% VaR.

**A holding that did not exist is proxied, and the proxied share is
printed.** 4180.T listed in March 2021. For the COVID replay it is
carried as 1.10 × the TOPIX ETF (its two-year weekly beta), marked with
an asterisk, with "10.5% of NAV proxied" under the table. Dropping it
would have rescaled the other weights; holding it flat would have
understated the loss. A missing series with no proxy raises rather than
defaulting.

**Cash is an exposure.** Yen cash is 9.9% of NAV, with no price risk and
full currency risk. Cash by currency from Flex's cash report, converted
at stored FX, sums to 1.0003 of the NAV row's own cash figure.

**Refreshing prices moved Phase 4's output.** Running market_data.py for
the long history also advanced holding_prices from 2026-09-30 to
2026-10-05, so `intrinsic.py` now prints slightly different multiples
(5105.T 0.64x, not 0.63x). Nothing is wrong; the Phase 4 tables in this
file and the README are as of 2026-09-30 and are now dated as such.

### What this does and does not support

- An episode beta is one observation and credits the factor with the
  entire fall in that episode. It is an order of magnitude, not an
  estimate.
- The hypothetical shocks move FX only as stated. The safe-haven yen
  response that cushioned every replay is deliberately not added to the
  Nasdaq shock, so that column is somewhat harsher than history.
- Replays use adjusted closes (dividends included) and let weights drift
  from the start of the window, as an unrebalanced book would.
- No rate, credit or volatility shocks: the book is long-only cash
  equity, so every exposure is linear and nothing needs repricing.
- The comparison with IBKR's own stress report is still manual, for the
  same reason as the VaR one — it is not in the Flex Query.


## Open items to revisit

- Second linked account (`ACCOUNT_B`) throwing permission errors — harmless
  for now, but figure out what it is before Phase 6 (live data) in case it
  matters for account selection.
- `corporate_actions` table/parser in `flex.py` is best-effort — the query
  has returned 0 rows so far, so the column mapping is based on IBKR's
  documented schema, not verified against real data. Check it against a
  real row whenever the first corporate action shows up.
- Phase 2's ~7% residual gap (see above) -- revisit if it starts to matter
  for later phases (e.g. if Phase 3's VaR/risk numbers look off, or the
  gap grows as more trades/holidays accumulate).
- **Cross-check stress results against IBKR's own stress test report**
  (Risk Navigator), by hand, for the same reason as the VaR item below.
- **Cross-check risk figures against IBKR's own VaR report.** Phase 3's
  roadmap entry calls for it, but IBKR's VaR is not part of the Flex
  Query -- it lives in Portfolio Analyst / the risk report. Remains a
  manual comparison until a data path exists.
- Check `config/valuation.py`'s risk-free rates against current 10-year UST
  and JGB yields; they are stated by hand and every intrinsic value moves
  with them.
- The DCF holds the owner-cash-flow margin constant. A margin path
  (current -> a stated mature margin) would let it say something about
  SHOP and 4180.T instead of pricing one harshly and refusing the other.
- Re-run the asynchronous-close correlation test once there is more than
  a year of history, when the weekly matrix has enough observations to
  distinguish signal from noise.
