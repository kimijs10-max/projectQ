"""
Peer-group verification for the sector-neutral screen (Phase 4c).

`config/peers.py` holds hand-curated candidate peers for each holding.
Hand-curation is unavoidable -- there is no free source of clean sector
membership for a mixed Tokyo/US/Greek universe -- but it is also the
weakest link in the screen, because a mistyped, delisted or repurposed
ticker would sit silently inside a peer group and shift every percentile
computed in it.

So nothing curated is trusted. Every candidate is resolved against the
issuer's own reported classification and must share its holding's sector
to survive. Sector rather than industry is the test on purpose: sector is
the level at which neutralisation is meaningful, and yfinance's industry
strings are granular enough ("Software - Infrastructure" versus "Software
- Application") that requiring an exact match would discard genuine peers.
The industry spread within each surviving group is reported instead, so
the reader can judge how tight the comparison really is.

Run directly to fetch and verify:
    python src/screener/universe.py
"""

from __future__ import annotations

import sqlite3
import sys
import warnings
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config"))

import peers as peer_config  # noqa: E402

from storage import db  # noqa: E402


def fetch_meta(symbols: list[str]) -> pd.DataFrame:
    """Sector, industry and listing currency for each symbol."""
    rows = []
    now = pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for n, symbol in enumerate(symbols, start=1):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                info = yf.Ticker(symbol).info or {}
        except Exception as exc:
            print(f"  [{n:>3}/{len(symbols)}] {symbol:<14} unresolved "
                  f"({type(exc).__name__})")
            continue
        sector = info.get("sector")
        quote_type = info.get("quoteType")
        # Requiring an equity quote type is not redundant with requiring a
        # sector, and the distinction is not academic. Tickers of acquired
        # companies get reused: EGLE belonged to Eagle Bulk Shipping until
        # Star Bulk acquired it and now belongs to a Global X S&P 500 ETF,
        # and GOGL belonged to Golden Ocean until the CMB.TECH merger and
        # now belongs to a 2x leveraged Google ETF. Both return live prices
        # and would survive any "does this ticker resolve?" check. Only the
        # instrument type distinguishes them from the shipping companies
        # they used to be.
        if quote_type and quote_type.upper() != "EQUITY":
            print(f"  [{n:>3}/{len(symbols)}] {symbol:<14} not an equity "
                  f"({quote_type}: {str(info.get('shortName'))[:34]})")
            continue
        if not sector:
            print(f"  [{n:>3}/{len(symbols)}] {symbol:<14} no sector reported")
            continue
        rows.append({
            "symbol": symbol,
            "short_name": info.get("shortName"),
            "sector": sector,
            "industry": info.get("industry"),
            "quote_type": info.get("quoteType"),
            "currency": info.get("currency"),
            # The currency the statements are reported in, which is not
            # always the one the shares trade in: a US-listed ADR prices
            # in dollars and reports in its home currency. Any per-share
            # value built from the statements is only comparable with the
            # price when the two match (see screener/candidates.py).
            "financial_currency": info.get("financialCurrency"),
            "fetched_at": now,
        })
        if n % 20 == 0:
            print(f"  [{n:>3}/{len(symbols)}] ...")
    return pd.DataFrame(rows)


def verified_peers(conn: sqlite3.Connection) -> tuple[dict[str, list[str]], pd.DataFrame]:
    """
    Peer groups reduced to candidates that share their holding's sector.

    Returns the surviving groups and a frame of every rejection with its
    reason, so the drops are reportable rather than invisible.
    """
    meta = db.read_table(conn, "security_meta")
    if meta.empty:
        return {}, pd.DataFrame()
    lookup = meta.set_index("symbol")

    groups: dict[str, list[str]] = {}
    rejected = []
    for holding, candidates in peer_config.PEER_GROUPS.items():
        if holding not in lookup.index:
            rejected.append({"holding": holding, "symbol": holding,
                             "reason": "holding itself did not resolve"})
            continue
        want = lookup.loc[holding, "sector"]
        kept = []
        for candidate in candidates:
            if candidate not in lookup.index:
                rejected.append({"holding": holding, "symbol": candidate,
                                 "reason": "unresolved"})
                continue
            quote_type = lookup.loc[candidate, "quote_type"]
            if isinstance(quote_type, str) and quote_type.upper() != "EQUITY":
                rejected.append({"holding": holding, "symbol": candidate,
                                 "reason": f"not an equity ({quote_type})"})
                continue
            got = lookup.loc[candidate, "sector"]
            if got != want:
                rejected.append({"holding": holding, "symbol": candidate,
                                 "reason": f"sector {got!r}, expected {want!r}"})
                continue
            kept.append(candidate)
        groups[holding] = kept
    return groups, pd.DataFrame(rejected)


def main() -> None:
    conn = db.connect()
    try:
        symbols = peer_config.universe()
        stored = db.read_table(conn, "security_meta")
        # A row stored before financial_currency existed is refetched too.
        known = set(stored.loc[stored["financial_currency"].notna(), "symbol"]) \
            if not stored.empty else set()
        todo = [s for s in symbols if s not in known]
        if todo:
            print(f"Resolving {len(todo)} new tickers "
                  f"({len(symbols) - len(todo)} already known)...")
            meta = fetch_meta(todo)
            if not meta.empty:
                db.upsert_df(conn, "security_meta", meta)
        else:
            print(f"All {len(symbols)} tickers already resolved.")
        meta = db.read_table(conn, "security_meta")
        resolved = meta[meta["symbol"].isin(symbols)]
        print(f"\nsecurity_meta  {len(resolved):>5} resolved of "
              f"{len(symbols)} candidates")

        groups, rejected = verified_peers(conn)
        lookup = resolved.set_index("symbol")

        print("\n" + "=" * 76)
        print("VERIFIED PEER GROUPS")
        print("=" * 76)
        for holding, kept in groups.items():
            name = lookup.loc[holding, "short_name"]
            sector = lookup.loc[holding, "sector"]
            candidates = len(peer_config.PEER_GROUPS[holding])
            print(f"\n{holding}  {str(name)[:30]:32s} {sector}")
            print(f"  {len(kept)} of {candidates} candidates verified")
            industries = lookup.loc[kept, "industry"].value_counts()
            for industry, count in industries.items():
                print(f"     {count:>3}  {industry}")

        if not rejected.empty:
            print("\n" + "=" * 76)
            print(f"DROPPED ({len(rejected)})")
            print("=" * 76)
            for holding, group in rejected.groupby("holding"):
                print(f"\n{holding}:")
                for _, row in group.iterrows():
                    print(f"  {row['symbol']:<14} {row['reason']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
