"""
Stress-test scenario definitions (Phase 5).

Two kinds, and they answer different questions.

**Historical episodes** are date windows. The engine replays today's
portfolio through each one and reports the worst peak-to-trough loss
*inside* the window, on the portfolio's own dates. The windows are
therefore deliberately generous: they only need to contain the episode.
Fixing the peak and trough by hand instead would mean choosing the S&P's
dates or the Nikkei's, and this book holds both -- Tokyo bottomed a day
before New York in April 2025.

**Hypothetical shocks** are a stated move in one factor. Each has a
direct effect (an FX move translating foreign assets into base currency)
and an indirect one (what the holdings' prices have tended to do when
that factor moved), and the engine reports them separately because for
this book they have opposite signs.
"""

# name -> (window start, window end, what happened)
HISTORICAL = {
    "COVID crash": (
        "2020-02-12", "2020-04-03",
        "global equity selloff, S&P 500 -34% in 23 trading days",
    ),
    "Yen carry unwind": (
        "2024-07-10", "2024-08-09",
        "BoJ hike, yen +12% in three weeks, Nikkei -12% on 5 August",
    ),
    "Spring 2025 selloff": (
        "2025-02-18", "2025-04-11",
        "US tariff announcements, S&P 500 -19% peak to trough",
    ),
}

# Each shock names:
#   factor    the series the holdings' sensitivities are measured against
#             ("JPYUSD" is the yen's value in dollars, derived from the
#             two stored pairs; a rise is yen strength)
#   size      the move in that factor
#   fx        direct moves in currencies against the base currency
#   episode   the historical scenario whose observed price moves, per unit
#             of factor move, give the "stressed" sensitivities
#
# For the yen shock the direct move is applied to JPY/SGD with USD/SGD
# unchanged: a yen-specific move, not a broad one.
HYPOTHETICAL = {
    "Yen +10%": {
        "factor": "JPYUSD",
        "size": 0.10,
        "fx": {"JPY": 0.10},
        "episode": "Yen carry unwind",
    },
    "Nasdaq -15%": {
        "factor": "QQQ",
        "size": -0.15,
        "fx": {},
        "episode": "Spring 2025 selloff",
    },
}

# Extra tickers the scenarios need beyond the holdings and the two
# benchmarks: QQQ as the Nasdaq-100 factor.
FACTOR_TICKERS = ["QQQ"]

# Weeks of weekly returns behind each sensitivity. Weekly because Tokyo
# and New York close 13 hours apart and same-day returns understate how
# much a Tokyo stock moves with a US factor.
BETA_WEEKS = 104

# Benchmark used to proxy a holding that did not trade during an episode
# (4180.T listed in 2021, after the COVID crash), by its currency.
PROXY_BENCHMARK = {"USD": "SPY", "JPY": "1306.T"}

# FX pair that converts each currency to base (SGD). Base itself is absent.
FX_SERIES = {"USD": "USDSGD", "JPY": "JPYSGD"}
