"""
Phase 0 connectivity test — IBKR Flex Web Service.

Downloads the configured Flex Query and prints open positions
(symbol, quantity, currency). Compare this output to test_live.py's
output: Phase 0 is "done" when the two agree.

Run from the project root:
    python src/data/test_flex.py
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from ib_async import FlexReport

# .env lives at the project root, two levels up from src/data/
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

FLEX_TOKEN = os.getenv("FLEX_TOKEN")
FLEX_QUERY_ID = os.getenv("FLEX_QUERY_ID")


def main() -> None:
    if not FLEX_TOKEN or not FLEX_QUERY_ID:
        raise SystemExit(
            "FLEX_TOKEN and FLEX_QUERY_ID must be set in .env "
            "(see .env.example)."
        )

    print("Downloading Flex report...")
    report = FlexReport(token=FLEX_TOKEN, queryId=FLEX_QUERY_ID)

    print(f"Topics available: {report.topics()}")

    positions = report.df("OpenPosition")
    if positions is None or positions.empty:
        print(
            "No 'OpenPosition' topic found in this Flex Query. "
            "Check the query includes open positions."
        )
        return

    cols = [c for c in ("symbol", "position", "currency") if c in positions.columns]
    print("\nOpen positions (from Flex):")
    print(positions[cols].to_string(index=False))


if __name__ == "__main__":
    main()
