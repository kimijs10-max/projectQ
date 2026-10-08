"""
Valuation and sizing assumptions (Phase 4, margin-of-safety sizing).

Every number an intrinsic value depends on lives here rather than inside
`src/sizing/intrinsic.py`, because these are the judgement calls and they
should be readable -- and arguable -- in one place. None of them is
estimated from the portfolio; they are stated, and the output prints them
next to the results they produce.

Change one and re-run to see how much of a valuation was the assumption.
"""

# Ten-year government yields, by the currency a stock's cash flows are in.
# A JPY cash flow is discounted at a JPY rate: using a USD rate for a Tokyo
# listing would quietly import the rate differential as a valuation gap.
# Stated by hand rather than fetched -- check them against the current
# 10-year UST and JGB yields before relying on a result.
RISK_FREE = {"USD": 0.0425, "JPY": 0.020}

# One equity risk premium for both markets. A single round number is a
# simplification, but splitting it by market would be precision the rest
# of the model does not have.
EQUITY_RISK_PREMIUM = 0.05

# Long-run nominal growth a mature company can sustain, roughly inflation
# plus a little real growth. Lower for JPY, in line with the lower rate.
TERMINAL_GROWTH = {"USD": 0.025, "JPY": 0.010}

# Benchmark each stock's beta is measured against, in local currency.
BETA_BENCHMARK = {"USD": "SPY", "JPY": "1306.T"}

# Blume adjustment: adjusted = shrink * raw + (1 - shrink) * 1.0. Two
# years of weekly returns give a noisy beta, and measured betas drift
# toward 1 over time, so the raw figure is pulled a third of the way there.
BETA_SHRINK = 2 / 3

# Explicit forecast length, over which growth fades linearly to terminal.
HORIZON_YEARS = 10

# Fiscal years averaged to normalise the owner-cash-flow margin. One year
# is hostage to working-capital timing (5105.T's operating cash flow went
# 15bn -> 87bn yen between consecutive years).
NORMALISE_YEARS = 3

# DCF growth scenarios: (share of the trailing revenue CAGR assumed to
# persist as the starting growth rate, hard cap on that rate). Even the
# bull case assumes growth slows -- high growth rates do not persist, and
# an extrapolated one is the easiest way to make any price look cheap.
DCF_SCENARIOS = {
    "bear": (0.25, 0.10),
    "base": (0.50, 0.20),
    "bull": (0.75, 0.30),
}

# Residual-income scenarios: years over which a bank's return on equity
# fades to its cost of equity. After that it earns exactly its cost of
# capital and adds nothing above book value.
RI_FADE_YEARS = {"bear": 5, "base": 10, "bull": 20}

# Sizing rule: target weight = SIZING_FRACTION * margin of safety, held
# only above MIN_MARGIN_OF_SAFETY and never above MAX_WEIGHT. The minimum
# is the point of the whole idea -- the margin exists to absorb errors in
# the valuation, so a thin one is treated as no margin at all.
MIN_MARGIN_OF_SAFETY = 0.15
SIZING_FRACTION = 0.50
MAX_WEIGHT = 0.25

# --------------------------------------------------------------------------
# Screening the universe for candidates (src/screener/candidates.py).
#
# A mechanical valuation run over a hundred companies will call some of
# them cheap for reasons that are not cheapness. Each rule below removes
# one such reason, and every name it removes is listed with the rule that
# removed it rather than silently dropped.
# --------------------------------------------------------------------------

# Stage one: three ratios read straight off the statements, applied
# before any valuation. They are deliberately crude -- the point is to
# discard the expensive, the indebted and the illiquid cheaply, so that
# the valuation's judgement calls are only spent on sound businesses.
# Thresholds are the conventional ones; change them here.
#
#   PER            price / earnings per share, latest fiscal year.
#                  A company with no profit has no PER and fails.
#   equity ratio   shareholders' equity / total assets.
#   current ratio  current assets / current liabilities.
#
# The two balance-sheet ratios do not apply to a bank (equity is ~5% of
# assets by design and there is no current/non-current split), so a
# financial issuer is judged on PER alone and marked as such.
SCREEN_MAX_PER = 15.0
SCREEN_MIN_EQUITY_RATIO = 0.40
SCREEN_MIN_CURRENT_RATIO = 1.50

# Fiscal years of cash-flow history required. With fewer, "normal" cash
# flow is one or two data points.
SCREEN_MIN_YEARS = 3

# The worst year's owner-cash-flow margin must be at least this share of
# the normalised margin the valuation uses. Guards against valuing a
# cyclical business off its best years: container shipping earned more in
# 2022 than in the previous decade combined.
SCREEN_MARGIN_STABILITY = 0.50

# A price below this fraction of base-case value is treated as a reason
# to check the data, not as a bargain. Markets misprice things, but
# rarely by a factor of three in a large listed company; a model or data
# error is the likelier explanation.
SCREEN_MIN_PRICE_TO_VALUE = 0.33

# Most of a suggested portfolio that may sit in one peer group. A value
# screen left alone piles into whichever sector is cheapest, which is a
# sector bet wearing a stock-picking label (Phase 4c found exactly that
# in the existing book).
MAX_GROUP_WEIGHT = 0.30

# --------------------------------------------------------------------------
# Your own forecasts (src/sizing/intrinsic.py, "your thesis").
#
# Firm-foundation value includes growth: a stock is worth the present
# value of the cash it will produce, and for a growing company most of
# that cash is in the future. The base case above deliberately assumes
# growth slows sharply, which is a statement about caution, not about any
# particular company. The question that matters for a growth holding is
# the one only its owner can answer: how fast do *you* expect it to grow,
# and is the price below the value that forecast gives?
#
# Enter a forecast per symbol and the engine values the stock under it,
# next to what the price assumes. Leave a symbol out and it gets none --
# nothing is filled in on your behalf.
#
#   growth      starting growth in revenue, fading linearly to the
#               currency's terminal rate over HORIZON_YEARS (DCF names)
#   margin      owner-cash-flow margin reached by the end of the horizon,
#               moving linearly from today's; omit to hold today's margin
#   roe         return on equity the bank starts from (residual income)
#   fade_years  years over which that ROE fades to the cost of equity
#
# Example (delete the leading #, change the numbers to your own view):
#   "NVDA":   {"growth": 0.35},
#   "SHOP":   {"growth": 0.25, "margin": 0.20},
#   "4180.T": {"growth": 0.25, "margin": 0.10},
#   "8306.T": {"roe": 0.13, "fade_years": 15},
THESIS = {
}

# Rungs for the "what you would have to believe" ladder: price / value is
# shown at each. They are a grid, not forecasts.
GROWTH_LADDER = (0.05, 0.10, 0.20, 0.30, 0.40, 0.50)
ROE_LADDER = (0.08, 0.10, 0.12, 0.14, 0.16)
# Fade lengths shown for a bank. How long excess returns last matters as
# much as how high they start, and ten years is the cautious end.
FADE_LADDER = (10, 20, 40)

# Extra ladder rows for names whose case rests on margins rising: the
# owner-cash-flow margin reached at the end of the horizon. Also a grid,
# not a forecast -- it exists because holding today's margin constant
# prices one of these harshly and cannot value the other at all.
MARGIN_LADDER = {
    "SHOP": (0.15, 0.20, 0.25),
    "4180.T": (0.05, 0.10, 0.15),
}
