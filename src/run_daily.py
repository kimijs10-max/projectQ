"""
The daily run: refresh the data, check it, build the report.

One command in place of the sequence of scripts in the README:

    python src/run_daily.py

Steps, in order:

  1. Flex statement      -- positions, trades, cash, NAV (src/data/flex.py)
  2. Market data         -- FX, benchmarks, holding and scenario prices
  3. Reconciliation      -- stored positions against the broker's NAV
  4. Intraday bars       -- only if IB Gateway is listening; skipped otherwise
  5. Report              -- reports/daily_report.html

A failed step is reported and the run continues: the report is built from
whatever is in the database, and its own "can these numbers be trusted"
section shows the latest date and whether reconciliation holds. The
summary at the end lists every step that did not complete, and the exit
code is non-zero if any did not, so a scheduler can tell.

Read-only throughout. Nothing here places, modifies or cancels orders.

The slower, rarely-changing inputs are deliberately not part of the daily
run: factor returns (src/data/factor_data.py), fundamentals
(src/data/fundamentals.py) and the peer universe (src/screener/).
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))


def _gateway_listening() -> bool:
    from data import ibkr_live

    try:
        with socket.create_connection((ibkr_live.IB_HOST, ibkr_live.IB_PORT), timeout=2):
            return True
    except OSError:
        return False


def _steps():
    from checks import reconcile
    from data import flex, ibkr_live, market_data
    from report import build_report

    return [
        ("Flex statement", flex.main, None),
        ("Market data", market_data.main, None),
        ("Reconciliation", reconcile.main, None),
        ("Intraday bars", ibkr_live.main, _gateway_listening),
        ("Report", build_report.main, None),
    ]


def main() -> int:
    outcomes: list[tuple[str, str]] = []
    for name, run, available in _steps():
        print(f"\n{'=' * 78}\n{name}\n{'=' * 78}")
        if available is not None and not available():
            print("skipped: IB Gateway is not listening")
            outcomes.append((name, "skipped (Gateway not running)"))
            continue
        started = time.time()
        try:
            run()
            outcomes.append((name, f"ok ({time.time() - started:.0f}s)"))
        except Exception as exc:  # one step failing must not hide the others
            # Type only: exception text from the broker libraries can
            # contain account identifiers.
            print(f"FAILED: {type(exc).__name__}")
            outcomes.append((name, f"FAILED ({type(exc).__name__})"))

    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    for name, outcome in outcomes:
        print(f"  {name:<16}{outcome}")
    return 1 if any(o.startswith("FAILED") for _, o in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
