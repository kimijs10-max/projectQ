# Portfolio Risk & P&L Attribution Engine

A daily risk and P&L attribution report built on a live Interactive Brokers
account holding US and Japanese equities, reported in SGD base currency.

The point of the project is not the plumbing — it is answering, every day,
where the money actually came from. A multi-currency portfolio makes that
non-trivial: a Tokyo-listed holding can rise in yen while the yen falls
against the base currency, and the two effects have to be separated before
either can be judged.

**Status:** data pipeline, P&L attribution, factor exposure, risk and the
value/quality/momentum screener complete, all validated against the broker's
own NAV. Stress tests are next.

---

## What it answers

1. **What did the portfolio make or lose?** — daily P&L in base currency.
2. **Why?** — split into local price move, FX move, their interaction,
   dividends, and trading costs.
3. **How much could it lose tomorrow?** — historical and parametric VaR,
   beta, correlation and concentration; stress tests *(planned)*.
4. **How well were trades executed?** — fills vs. arrival price and VWAP
   *(planned)*.

---

## Validation

Every figure the engine produces is checked against Interactive Brokers'
own reported numbers, because an attribution model that cannot reproduce
the broker's NAV is not worth reading.

**Position and NAV reconciliation** — stored positions converted to base
currency, plus cash and accruals, against the broker's reported NAV:

| Check | Result |
|---|---|
| Equity value vs. broker's reported equity | exact |
| Total NAV vs. broker's reported NAV | **0.000000% difference** |

**Daily return attribution** — the bottom-up attributed return against the
top-down NAV-implied return, over 261 trading days:

| Metric | Value | Reading |
|---|---|---|
| Mean difference | 0.44 bps/day | — |
| t-statistic on that mean | 0.25 | not significant; **attribution is unbiased** |
| Series correlation | 0.967 | — |
| Daily tracking error | 28.6 bps | residual noise |
| Lag-1 autocorrelation of the difference | −0.44 | a *timing* artefact, not a valuation error |

The negative autocorrelation is the informative number. An error that
systematically reverses the next day is a timing mismatch rather than a
mispricing, and it was traced to the FX snapshot instant: the broker
converts to base currency at its own fixing time, while the public FX
series used here is stamped at a different one. Re-running with the FX
series shifted one day moves that autocorrelation to +0.07 — essentially
zero — which identifies the cause without needing the broker's internal
fixings.

A competing hypothesis, that the Tokyo/New York asynchronous close was
responsible, was tested and **rejected**: lagging Tokyo prices made
tracking error roughly four times worse. That negative result is kept in
[`NOTES.md`](NOTES.md) alongside the rest of the diagnostic record.

---

## Methodology

### Currency decomposition

A holding's return in base currency is not the sum of its local return and
the FX return — the two compound:

```
R_base = (1 + r_local)(1 + r_fx) − 1
       = r_local + r_fx + r_local · r_fx
```

The engine reports all three terms separately rather than folding the
cross-term into either leg. It is small day to day but it is real, and
attributing it to the wrong leg quietly misstates whether a position made
money on the stock or on the currency.

Contribution to gross mark-to-market over the window:

| Sleeve | Local price move | FX move |
|---|---|---|
| JPY (Tokyo-listed) | +76.5% | −18.4% |
| USD | +44.9% | −2.7% |

The cross-term accounts for −0.3% over this window, which is why it is
reported rather than assumed away: negligible here, but not structurally
negligible, and it grows with volatility in either leg.

Read plainly: the Japanese sleeve's stock selection worked, and a weaker
yen against the base currency gave back roughly a quarter of it. That is
the kind of statement the decomposition exists to support.

### Reconstructing position history

The broker's Flex statement gives open positions only as a point-in-time
snapshot, not a daily history, so a naive daily return series cannot be
built from it directly. Instead, each holding's daily share count is
reconstructed by starting from today's known position and undoing each
trade in reverse chronological order. This is exact for any date inside
the statement's 365-day trade window, regardless of when the position was
originally opened.

### Deliberate exclusions

The broker's `fifoPnlRealized` is **not** added to the window's P&L. It
measures gain since a position's original cost basis, which may predate the
reporting window entirely — including it both double-counts the in-window
portion and imports gains that belong to an earlier period. It is retained
separately as a cross-check figure. This distinction is easy to miss and
was worth roughly 70% of an early reconciliation error.

Prices and FX are forward-filled across the date grid before differencing.
Without it, a holding with no print on a foreign market holiday causes the
surrounding day-pairs to be skipped, silently discarding that period's real
move.

---

## Factor exposure

Each sleeve's excess return is regressed on its own region's Fama-French
factors, in its own trading currency:

```
R_sleeve − RF = α + β_MKT·MKT + β_SMB·SMB + β_HML·HML
                  + β_RMW·RMW + β_CMA·CMA + β_MOM·MOM + ε
```

Regressing base-currency returns on USD-denominated factors would push
the FX move into the residual and corrupt α, since the factors cannot
explain currency. Separating local return from FX return in the
attribution step is what makes the correct version free.

![Factor exposures by sleeve](reports/factor_exposures.png)

Intervals are 95% Newey-West. HAC rather than plain OLS standard errors
because daily residuals are serially correlated — the same property that
showed up diagnosing the reconciliation residual — so OLS t-statistics
would be overstated here.

**The result contradicts the stated strategy, which is the interesting
part.** An intrinsic-value approach predicts a positive HML loading.
Neither sleeve has one: USD −0.41 (t = −1.8), JPY −0.07 (t = −0.2).

The careful reading is narrower than "the value thesis failed". HML is
constructed on book-to-market, whereas firm-foundation investing values
discounted future cash flows — different constructs, and a DCF-based
investor can rationally hold a high book-multiple name if future cash
flows justify the price. The defensible claim is that **the strategy as
implemented is not academic-HML value**. Phase 4b–d builds explicit
value and quality metrics, which will let that be settled properly
rather than argued.

Two further readings. The USD sleeve carries market beta 1.53 with
R² 0.59, so a large part of it is simply leveraged market exposure; the
JPY sleeve's R² of 0.23 is far more idiosyncratic, which is what stock
selection is supposed to look like. And α is roughly +10%/yr in both
sleeves but with t of 0.33 and 0.50 — **indistinguishable from zero.**
One year of daily data cannot establish skill, and quoting the point
estimate without the t-statistic would be the easiest way to overclaim
on this project.

![Rolling factor exposures](reports/rolling_betas.png)

---

## Portfolio shape

Weights only; absolute values are deliberately omitted.

As of the latest snapshot:

| Holding | Listing | Currency | Weight (% of NAV) |
|---|---|---|---|
| 8306.T | Tokyo | JPY | 27.7 |
| NVDA | NASDAQ | USD | 26.8 |
| 5105.T | Tokyo | JPY | 14.5 |
| 4180.T | Tokyo | JPY | 10.5 |
| SHOP | NASDAQ | USD | 8.4 |
| Cash | — | SGD | 12.1 |

Sleeves: 52.7% JPY, 35.2% USD, 12.1% base-currency cash. Concentration is
high and deliberate — the investment approach is a concentrated
intrinsic-value one — which is exactly why the risk phase matters.

Note: the broker's `percentOfNAV` field is a share of the *equity sleeve*,
not of total NAV, and sums to 100% while ignoring cash. The weights above
are computed against total NAV.

---

## Risk

All figures in percent of NAV, over 261 trading days.

Annualised volatility is **17.9%**. One-day value at risk:

| Confidence | Historical | Parametric (normal) | Observations in tail |
|---|---|---|---|
| 95% | **1.90%** | 1.78% | 14 |
| 99% | 2.80% | 2.55% | **3 — not a usable estimate** |

![Daily return distribution and value at risk](reports/return_distribution.png)

Both estimates are reported because the gap between them is the finding.
Parametric VaR assumes returns are normal; historical VaR reads the
empirical quantile. Historical is worse at both levels, so the normal
assumption understates this tail — mildly, at 0.12pp on the 95% figure,
but in the direction the shape of the left tail predicts.

Every VaR figure carries the number of observations behind its tail, and
the 99% row is the reason that matters: at one year of daily data it rests
on three points. Quoting it as an estimate would be the easiest dishonest
number on this project, so the engine prints the count next to it rather
than leaving the reader to work out the sample size.

Re-running the same calculation on the broker's own NAV series instead of
the engine's attributed returns gives 1.85% historical and 1.75%
parametric — close enough that the risk figures do not depend on which
return series is used, which is a cross-check the two-source design makes
free.

### Beta

Portfolio and benchmark are both converted to base currency before
differencing. Beta should describe what happens to the investor's wealth,
which includes the currency move; the local-currency equity beta is a
different question and is answered by the factor regressions above.

| Benchmark | Beta | Correlation | R² |
|---|---|---|---|
| S&P 500 (SPY) | 0.65 | 0.48 | 0.23 |
| TOPIX (1306.T) | 0.38 | 0.43 | 0.18 |

Neither R² is large, which is the expected result for a five-name book and
consistent with the factor regressions: most of the variance here is
idiosyncratic rather than market.

### Correlation, and an honest non-result

![Correlation of holdings](reports/correlation_matrix.png)

Tokyo closes about thirteen hours before New York, so same-date daily
correlations should understate US/Japan co-movement while weekly sampling
spans the gap. This was written into the project's list of known traps
before the data was looked at, and the two panels were built to
demonstrate it.

**They do not.** Cross-market pairs move in both directions between the
panels, averaging about +0.03.

The observation counts printed in each cell explain why, and they are the
reason the chart is worth keeping. NVDA/SHOP is the only pair with the
full 53 weeks behind it, and it is also the only one that barely moves
(0.28 daily, 0.29 weekly). Every pair that swings hard has 23–27 weeks,
where the standard error on a correlation is roughly 0.19 — wide enough to
produce those swings on its own. So the instability is a sample-size
artefact, not a weekly-versus-daily effect, and the daily matrix is the
better-supported of the two until there is more history.

### Concentration

| Measure | Equity sleeve | Including cash |
|---|---|---|
| Largest weight | 31.5% | 27.7% |
| Herfindahl-Hirschman index | 0.243 | 0.202 |
| Effective number of positions (1/HHI) | **4.1** | 4.9 |

Five holdings that behave like roughly four equally weighted ones. Cash is
shown separately because it genuinely dilutes concentration, and reporting
only the equity sleeve would overstate how concentrated the account is.

---

## Screener: value, quality and momentum

The factor regression above says what the portfolio is *not*. This section
builds the metrics to say what it is, from financial statements rather than
from return covariance — an independent check on the same question.

Four metrics, each chosen for a reason:

- **Gross profitability** — (revenue − cost of revenue) / total assets.
  The further down the income statement you read, the more the figure has
  been shaped by depreciation schedules, tax strategy and one-offs. Its
  value here is that it is *negatively* correlated with book-to-market, so
  quality and value are additive rather than two names for one bet.
- **Piotroski F-Score** — nine binary tests across profitability,
  leverage/liquidity and operating efficiency. Designed to work *within*
  the value universe: cheap stocks are often cheap because they are dying,
  and the F-Score separates those from the ones recovering.
- **12-1 momentum** — return from twelve months ago to one month ago. The
  skipped month is not a convenience; short-horizon reversal is a separate
  and opposite effect.
- **Price-to-book, flagged below 1.0 for Tokyo listings** — in Japan this
  is a catalyst, not a valuation measure. The Tokyo Stock Exchange's March
  2023 reform asked sub-book companies to publish capital-efficiency
  plans.

### No reading before it was knowable

A fiscal year ending 31 March is not public knowledge on 31 March; the
filing lands in June. Every stored statement row therefore carries an
explicit availability date of fiscal year-end plus 90 days, and every read
filters on that column rather than on the fiscal date. A screen run for a
past date cannot see a filing that had not happened.

The lag is deliberately conservative. A real filing calendar would be more
precise, but erring late only understates a result, whereas erring early
manufactures one.

### Refusing to score

A bank reports no cost of revenue and no current/non-current balance-sheet
split, because neither concept applies to it. That leaves gross
profitability undefined and two of the nine F-Score tests unevaluable for
the largest holding.

Seven of nine tests remain computable — and computing them would be the
real error. A bank's operating cash flow tracks changes in loans and
deposits; the holding here reports roughly −¥23tn, which says nothing
about whether the bank is healthy. The cash-flow and accruals tests would
score balance-sheet growth and label it earnings quality. Piotroski's
original sample excludes financial firms for exactly this reason, so the
score is **withheld entirely** rather than reported with a caveat: 4 of 7
sitting beside a genuine 4 of 7 reads as a weak company rather than an
unmeasured one.

Detection is structural — no cost of revenue *and* no current/non-current
split, across every year — rather than a sector label, because the absence
of both concepts is the balance sheet itself saying what the company is.

---

## Sector-neutral ranking

A raw metric means little across sectors. Gross profitability of 0.08 is
poor for a software company and ordinary for a shipping line; a
price-to-book of 0.95 is cheap for a semiconductor maker and unremarkable
for a Japanese regional bank. Ranking one list would mostly rank sectors.

So each holding is scored against its own verified peer group — 132
symbols in total, six holdings and 126 peers — and no number is compared
across groups.

### Curate by hand, verify by machine

There is no free source of clean sector membership for a mixed
Tokyo/US/Greek/Danish universe, so the peer lists are hand-written. That
makes them the weakest link: a mistyped or repurposed ticker sits silently
inside a group and shifts every percentile in it. So nothing curated is
trusted — each candidate must resolve to an *equity* whose reported sector
matches its holding's, and the rejections are printed.

It earned its place immediately.

**Ticker reuse.** `EGLE` belonged to Eagle Bulk Shipping until Star Bulk
acquired it, and now belongs to a Global X S&P 500 ETF. `GOGL` belonged to
Golden Ocean until the CMB.TECH merger, and now belongs to a 2×
leveraged Google ETF. Both return live price data and would survive any
"does this ticker resolve?" check. Only the instrument type distinguishes
them from the shipping companies they used to be — which is why the equity
test is explicit rather than incidental.

**Taxonomy encoding a real distinction.** Six oil-tanker operators were
rejected as Energy rather than Industrials. That is correct: crude and
product tankers classify under Energy, while dry bulk, container and car
carriers sit under Industrials. The holding is the latter, so the
rejection kept the comparison inside one cycle instead of blurring two.

Also dropped: eleven delisted tickers, including five Japanese regional
banks that reorganised into holding companies under new codes, and several
of the author's own misclassifications.

### The result that reframes everything else

| | Holding's 12-1 return | Peer median | Rank |
|---|---|---|---|
| Tokyo-listed bank vs Japanese banks | +62.7% | **+86.5%** | 22 of 23 |
| Tokyo-listed shipper vs marine shipping | +44.5% | **+67.3%** | 19 of 20 |

On raw momentum these are the two strongest names in the book. Against
their own sectors they are near the bottom. Japanese banks rallied on rate
normalisation and shipping on freight rates; the positions captured the
sector and gave back 23 to 24 points of it.

*"I owned a stock that rose 63%"* and *"I picked a good bank"* are
different claims. This is the view that separates them.

Percentiles within each holding's own peer group, 100 = best:

| | B/M | gross prof. | F | mom | value | +quality | +mom |
|---|---|---|---|---|---|---|---|
| 4180.T | 56 | 56 | 22 | 39 | 56 | 47 | 44 |
| 5105.T | 45 | **100** | 14 | 41 | 45 | 51 | 48 |
| 8306.T | 17 | — | — | 9 | 17 | withheld | withheld |
| 9101.T | 68 | 60 | 25 | 10 | 68 | 55 | 40 |
| NVDA | 13 | **100** | 7 | 39 | 13 | 33 | 35 |
| SHOP | 28 | 42 | 38 | 42 | 28 | 34 | 37 |

Two readings worth drawing out. Sector-neutralising moves the value
conclusion: a 1.88× price-to-book looks moderate in isolation but ranks
20th of 23 against Japanese banks. And four of six holdings sit below
their sector's median book-to-market, so **the book is not value-tilted on
a sector-neutral basis either** — a third independent method reaching the
factor regression's conclusion.

The 7th-percentile F-Score on the semiconductor holding is *not* a quality
verdict; it has the best gross profitability in its group. The F-Score
rewards year-on-year improvement and balance-sheet conservatism, so a
company already at peak margins and investing heavily scores badly even
when the business is exceptional. It is the wrong instrument for that
name, and saying so is more useful than quoting the number.

A composite is withheld when a sleeve it names does not exist: "value +
quality" computed without a quality measurement is the value score under
another label, and would sit in the same column as composites that
genuinely carry both.

---

## Does any of it predict anything?

The build order called for a backtest of value-only against value+quality
against value+quality+momentum. That is not supportable on this data: the
free fundamentals source gives four or five annual statements, so after
the reporting lag there are three or four annual rebalances. Three
observations is an anecdote, and a weak backtest is worse than none —
a reader who notices the sample size discounts everything near it.

So the question is asked **across companies rather than across time**.
Each name is scored as of a past date using only fundamentals available
then, and the ranking is tested against the following year's return
*relative to its own peer group's median*. That trades time-series depth,
which this data lacks, for cross-sectional breadth, which it has: ~130
names per window instead of three rebalances. Two non-overlapping annual
windows, reported separately and never pooled.

Spearman rank correlation of each signal against forward excess return:

| Signal | 2024-09 → 2025-09 | 2025-09 → 2026-09 | Verdict |
|---|---|---|---|
| Book-to-market | **+0.183** | +0.091 | positive both |
| Gross profitability | −0.124 | **−0.164** | negative both |
| F-Score ratio | −0.050 | −0.069 | negative, negligible |
| 12-1 momentum | −0.055 | +0.152 | **sign flips** |
| Value | **+0.183** | +0.091 | positive both |
| Value + quality | +0.051 | −0.028 | one negligible |
| Value + quality + momentum | +0.045 | +0.097 | one negligible |

**The answer is the opposite of the expected one: adding quality to value
made it worse, in both windows.** Gross profitability was negatively
related to within-sector outperformance in both years, and in the second
window the tercile spread was −36.7% — the top third by gross
profitability averaged +0.7% against its sector while the bottom third
averaged +37.4%. Value was the only signal to hold a non-negligible sign
across both windows. Momentum changed sign, which rules out reading either
window's momentum result alone.

This does **not** say the gross-profitability literature is wrong. The
proxy here is annual gross profitability from a free data source plus a
coarse F-Score ratio available for only 82 to 108 of 132 names, against a
literature built on far better data and decades of history. Two adjacent
windows are two draws from one regime, dominated by the same bank and
shipping rallies. Every name is a survivor — not a hypothetical concern
given that two candidate tickers turned out to be ETFs occupying the codes
of acquired companies. And the p-values assume independent observations,
which returns in a single cross-section are not.

What it supports is narrower and still worth saying: in this universe over
these two years, quality as measured here detracted from value rather than
adding to it, and value was the only signal that held its direction.

That lands awkwardly against the portfolio, which is the point. Three
methods already concluded the book is not value-tilted. The validation
then finds value is the one signal in this data with any consistency
behind it. **So the strategy as implemented underweights the only factor
this data supports** — a conclusion kept because it is not flattering.

---

## Architecture

```
IBKR Flex Web Service ─┐
IB Gateway (read-only) ─┼─→ data layer ─→ SQLite daily snapshots ─→ attribution ─→ reconciliation
yfinance (FX, prices) ─┘
```

SQLite is the durable store, not a cache. The Flex statement only serves a
rolling 365-day window, so anything not captured is eventually unavailable;
snapshots accumulate a history the source cannot reproduce later. All
writes are idempotent upserts on natural keys, so re-running a day's pull
never duplicates rows.

```
src/
├── data/
│   ├── flex.py          # Flex download, XML parsing, raw-statement caching
│   ├── market_data.py   # FX, benchmark and per-holding price history
│   ├── factor_data.py   # Fama-French factors (Kenneth French library)
│   ├── fundamentals.py  # annual statements, stored with availability dates
│   ├── test_flex.py     # connectivity test: positions via Flex
│   └── test_live.py     # connectivity test: positions via IB Gateway
├── screener/
│   ├── quality.py       # gross profitability, F-Score, momentum, P/B
│   ├── universe.py      # peer-group resolution and verification
│   ├── fetch_universe.py# bulk fundamentals and prices for the universe
│   ├── composite.py     # sector-neutral percentiles and composites
│   └── validate.py      # cross-sectional validation over two windows
├── storage/db.py        # schema and idempotent upserts
├── analytics/
│   ├── pnl.py           # attribution, daily returns, roll-ups
│   ├── factors.py       # factor regressions, OLS + Newey-West
│   └── risk.py          # VaR, beta, correlation, concentration
├── report/plots.py      # charts
└── checks/reconcile.py  # NAV reconciliation
```

---

## Running it

Requires Python 3.14 and an Interactive Brokers account with a configured
Flex Query.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill in FLEX_TOKEN and FLEX_QUERY_ID
```

Then, in order:

```bash
python src/data/flex.py          # download, parse and store the statement
python src/data/market_data.py   # FX, benchmark and holding price history
python src/checks/reconcile.py   # verify against the broker's NAV
python src/analytics/pnl.py      # attribution and daily-return validation
python src/data/factor_data.py   # Fama-French factor returns
python src/analytics/factors.py  # factor regressions
python src/analytics/risk.py     # VaR, beta, correlation, concentration
python src/report/plots.py       # regenerate the charts above
```

Then the screener, which has its own universe to assemble:

```bash
python src/data/fundamentals.py      # statements for the holdings
python src/screener/quality.py       # the four metrics, holdings only
python src/screener/universe.py      # resolve and verify peer groups
python src/screener/fetch_universe.py# fundamentals + prices for all 132
python src/screener/composite.py     # sector-neutral percentiles
python src/screener/validate.py      # cross-sectional validation
```

The live-position test additionally needs IB Gateway running on port 4001
with the read-only API enabled:

```bash
python src/data/test_live.py
```

There is no single orchestrating entry point yet; the sequence above is run
manually.

### Safety

The engine is strictly read-only. It never places, modifies or cancels
orders, and connects with `readonly=True` against a gateway that has the
read-only API enabled. Credentials live only in `.env`; raw statements,
the SQLite store and the account identifiers are excluded from version
control.

---

## Known limitations

- **Entry and exit day pricing.** On a day a trade opens or closes a
  position, the model anchors to that day's close rather than the actual
  execution price, so the execution-to-close move is unattributed. The
  broker reports a per-trade figure that would close this exactly; wiring
  it in is a known improvement.
- **Residual tracking error.** The ~29 bps of daily noise that remains is
  largely the broker's own mark price versus the public closing print, and
  cannot be eliminated without the broker's historical marks.
- **No per-holding history before the trade window.** Reconstruction is
  exact only inside the statement's 365-day window. Accumulated snapshots
  extend coverage going forward but cannot be backfilled.
- **Corporate action handling is untested.** The account has had none in
  the window, so the parser for them is written to the documented schema
  but unverified against real data.
- **The 99% VaR is not usable at this sample size.** Three tail
  observations is not an estimate. It is printed with its observation
  count rather than omitted, because the count is the honest way to say
  so, and it becomes meaningful as the snapshot history accumulates.
- **VaR has not been cross-checked against the broker's own figure.**
  Interactive Brokers computes a VaR in Portfolio Analyst, but it is not
  exposed through the Flex Query, so the comparison stays manual.
- **Public price data needs cleaning.** The benchmark series carried two
  mis-scaled days that inflated its volatility from 1.3% to 43% and drove
  a beta estimate to nearly zero. Ingestion now drops points more than 2×
  from their own five-day centred median, but this is a mitigation for a
  known failure mode, not a guarantee against the next one.
- **Fundamentals history is shallow.** The public source offers four or
  five annual statements, which is why the screener is validated
  cross-sectionally rather than backtested through time — three annual
  rebalances would not support the latter.
- **Peer groups are hand-curated.** Verification against each issuer's
  reported sector and instrument type catches wrong tickers, but it
  cannot catch a *missing* peer, so a group may be unrepresentative in
  ways the rejections do not reveal.
- **Survivorship bias is live, not theoretical.** The universe is built
  from tickers trading today. Two candidates turned out to be ETFs
  occupying the codes of shipping companies that had been acquired, which
  is the bias in its most concrete form.
- **Quality is crudely measured.** Annual gross profitability from a free
  source plus an F-Score ratio available for 82 to 108 of 132 names is a
  weak proxy for the constructs in the literature, so the finding that
  quality detracted from value is a result about this measurement in this
  universe, not about the factor.

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Broker connectivity, credentials, repo setup | Complete |
| 1 | Flex parsing, SQLite snapshots, market data, NAV reconciliation | Complete |
| 2 | P&L attribution: stock vs. FX vs. dividends vs. fees | Complete |
| 4a | Factor exposure against Fama-French factors, US and Japan separately | Complete |
| 3 | VaR, beta, correlation, concentration | Complete |
| 4b–c | Quality/momentum screener, sector-neutral composite | Complete |
| 4d | Cross-sectional validation (backtest reframed; see above) | Complete |
| 4d | Margin-of-safety sizing from DCF / residual income | Planned |
| 5 | Stress tests, including the 2024 yen carry unwind | Next |
| 6 | Execution analysis vs. arrival price and VWAP | Planned |
| 7 | Daily HTML report | Planned |

Phase 4a precedes phase 3 deliberately: factor exposure is the more
informative diagnostic for a concentrated book, and both depend on the same
daily return series.
