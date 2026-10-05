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
