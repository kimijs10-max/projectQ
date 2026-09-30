"""
Phase 0 connectivity test — IB Gateway (live, read-only).

Connects read-only to IB Gateway on port 4001 and prints open positions
(symbol, quantity, avg cost, currency) plus the account's base currency.
Compare this output to test_flex.py's output: Phase 0 is "done" when
the two agree.

Safety: connects with readonly=True. Never places, modifies, or
cancels orders. IB Gateway must have "Read-Only API" enabled.

Run from the project root:
    python src/data/test_live.py
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from ib_async import IB

# .env lives at the project root, two levels up from src/data/
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "4001"))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1"))


def main() -> None:
    ib = IB()
    print(f"Connecting to IB Gateway at {IB_HOST}:{IB_PORT} (readonly)...")
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, readonly=True)

    try:
        print("Connected.\n")

        positions = ib.positions()
        if not positions:
            print("No open positions returned.")
        else:
            print("Open positions (from IB Gateway):")
            for p in positions:
                print(
                    f"  {p.contract.symbol:<8} qty={p.position:<10} "
                    f"avgCost={p.avgCost:.2f} currency={p.contract.currency}"
                )

        summary = ib.accountSummary()
        base_currency = next(
            (s.value for s in summary if s.tag == "Currency"), None
        )
        if base_currency:
            print(f"\nAccount base currency: {base_currency}")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
