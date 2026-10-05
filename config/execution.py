"""
Execution-analysis settings (Phase 6).
"""

# Time zone of the dateTime and orderTime fields in the Flex statement.
# Flex does not label it. Verified rather than assumed: read as US
# Eastern, every fill in the account lands inside the high-low range of
# its own one-minute bar; tca.py repeats that check on every run and
# says so when a fill does not.
FLEX_TIMEZONE = "America/New_York"

# Trading sessions by listing currency, in exchange-local time, both ends
# inclusive: the bar that starts at the closing minute holds the closing
# auction, which is a large share of a Tokyo day's volume. Used to keep
# VWAP to the hours the primary market is open -- IBKR's bars for Tokyo
# listings also include the lunch break and the PTS night session, which
# are a different and far thinner market.
SESSIONS = {
    "JPY": {
        "timezone": "Asia/Tokyo",
        # Tokyo's close moved from 15:00 to 15:30 in November 2024.
        "hours": [("09:00", "11:30"), ("12:30", "15:30")],
    },
    "USD": {
        "timezone": "America/New_York",
        "hours": [("09:30", "16:00")],
    },
}

# A day VWAP is flagged when the bars behind it carry less than this
# share of the exchange's own reported volume (or more than its inverse).
MIN_VOLUME_COVERAGE = 0.80
