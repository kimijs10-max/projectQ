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
