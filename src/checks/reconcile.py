"""
Phase 1 reconciliation check: does our own SQLite snapshot of positions
sum up to the NAV IBKR itself reports? See CLAUDE.md Phase 1 "done when".

This is a self-consistency check, not an independent revaluation: it
uses IBKR's own positionValue and fxRateToBase (already in the Flex
data), so it mainly proves the parsing/storage pipeline round-trips
Flex's numbers correctly without dropping or misconverting anything --
not that Flex's own mark prices are correct. Independent revaluation
from live/historical prices comes later once market_data.py has price
history for each holding.

The stock component should match Flex's own "stock" figure almost
exactly. The full NAV total needs dividend_accruals and
interest_accruals added back in alongside cash and stock -- IBKR's
EquitySummaryByReportDateInBase.total includes those.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

# So `storage.db` resolves whether this is run as a script
# (python src/checks/reconcile.py) or imported as part of the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import db  # noqa: E402

DEFAULT_TOLERANCE_PCT = 0.01  # 1 basis point


@dataclass
class ReconciliationResult:
    report_date: str
    account_id: str
    computed_stock: float
    reported_stock: float
    computed_total: float
    reported_total: float
    diff: float
    diff_pct: float
    passed: bool


def reconcile(
    conn: sqlite3.Connection,
    report_date: str | None = None,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
) -> ReconciliationResult:
    """
    Reconcile computed portfolio value against Flex's reported NAV for
    one date. Defaults to the latest date with a positions snapshot.
    """
    positions = db.read_table(conn, "positions")
    nav = db.read_table(conn, "nav_history")

    if positions.empty:
        raise ValueError("positions table is empty -- run flex.py first")
    if report_date is None:
        report_date = positions["report_date"].max()

    day_positions = positions[positions["report_date"] == report_date]
    nav_rows = nav[nav["report_date"] == report_date]
    if nav_rows.empty:
        raise ValueError(f"No nav_history row for {report_date}")
    nav_row = nav_rows.iloc[0]

    computed_stock = (day_positions["position_value"] * day_positions["fx_rate_to_base"]).sum()
    computed_total = (
        computed_stock
        + nav_row["cash"]
        + nav_row["dividend_accruals"]
        + nav_row["interest_accruals"]
    )
    reported_total = nav_row["total"]
    diff = computed_total - reported_total
    diff_pct = abs(diff) / reported_total * 100 if reported_total else float("inf")

    return ReconciliationResult(
        report_date=report_date,
        account_id=nav_row["account_id"],
        computed_stock=computed_stock,
        reported_stock=nav_row["stock"],
        computed_total=computed_total,
        reported_total=reported_total,
        diff=diff,
        diff_pct=diff_pct,
        passed=diff_pct < tolerance_pct,
    )


def main() -> None:
    conn = db.connect()
    try:
        result = reconcile(conn)
    finally:
        conn.close()

    status = "PASS" if result.passed else "FAIL"
    print(f"Reconciliation as of {result.report_date} (account {result.account_id}): {status}")
    print(f"  Computed stock value: {result.computed_stock:,.4f}")
    print(f"  Reported stock value: {result.reported_stock:,.4f}")
    print(f"  Computed total (NAV): {result.computed_total:,.4f}")
    print(f"  Reported total (NAV): {result.reported_total:,.4f}")
    print(f"  Difference: {result.diff:,.4f} ({result.diff_pct:.6f}%)")


if __name__ == "__main__":
    main()
