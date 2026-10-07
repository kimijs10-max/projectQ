"""
Settings for the fallen-stock screen (src/screener/fallen.py).

The screen looks for a stock that has fallen much further than comparable
stocks without its reported results getting worse. Both halves need a
threshold, and neither threshold is known in advance, so the screen ships
with a test of the first (did the worst fallers go on to recover?) and
states plainly what the second can and cannot see.
"""

# How far back the fall is measured. The history test in fallen.py runs
# every window in LOOKBACK_GRID; none stood out, so this is a middle
# choice, not an optimum -- long enough not to be one bad week, short
# enough that the news behind it is still current.
LOOKBACK_MONTHS = 6

# A name is "fallen" when ALL hold:
#   - its price is actually down over the window,
#   - its return is in the bottom FALL_QUANTILE of its own peer group, and
#   - it is at least MIN_SHORTFALL behind that group's median return.
# The first keeps the word honest: in a strong market the bottom of a
# group can be up 13% against peers up 40%, which is lagging, not falling.
# The second makes the test relative (a sector-wide fall flags nobody);
# the third stops the bottom fifth of a group that barely moved being
# called a collapse.
FALL_QUANTILE = 0.20
MIN_SHORTFALL = 0.15

# A single-day move larger than this inside the window marks the price
# history as suspect and the name is set aside, not flagged. Real one-day
# moves of this size are almost unknown in listed large and mid caps; a
# stock split the data source has not adjusted for yet is the usual cause
# (8377.T showed -90% in a day and "-84% against peers" five days after a
# ten-for-one split).
MAX_DAILY_MOVE = 0.50

# "Did the business get worse?" -- latest quarter against the same quarter
# a year earlier. A fall counts as explained by reported results when
# revenue shrank, when profit fell by more than PROFIT_DROP, or when the
# quarter was a loss.
PROFIT_DROP = 0.20

# Grids for the history test: months of look-back and of holding.
LOOKBACK_GRID = (1, 3, 6, 12)
HOLDING_GRID = (3, 6, 12)

# Fewest names with both a past and a forward return for a formation
# month to count.
MIN_NAMES = 60
