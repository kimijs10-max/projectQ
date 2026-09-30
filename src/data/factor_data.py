"""
Fama-French factor returns from the Kenneth French Data Library.

Two regions, matched to this portfolio's two sleeves:
  US -- F-F_Research_Data_5_Factors_2x3_daily + F-F_Momentum_Factor_daily
  JP -- Japan_5_Factors_Daily + Japan_MOM_Factor_Daily

Each sleeve is regressed in its own local currency against its own
region's factors (see analytics/factors.py). Regressing SGD-denominated
returns against USD-denominated factors would push the currency move
into the residual and corrupt alpha, since the factors cannot explain
FX.

Format notes, all verified against the live files rather than assumed:
  - a preamble of descriptive text precedes the header; the header is
    the first line beginning with a comma
  - values are published in percent, so they are divided by 100 here
  - missing values are sentinels: -99.99 or -999
  - the Japan files pad their date column with trailing spaces
  - the momentum factor is named "Mom" for the US but "WML" for Japan;
    both are stored as MOM
  - a copyright line trails the data
  - the library runs roughly a month behind the present, so the usable
    regression sample ends earlier than the portfolio's own history

Run directly to fetch and store all four datasets:
    python src/data/factor_data.py
"""

import io
import re
import zipfile
from urllib.request import urlopen

import pandas as pd

FRENCH_BASE = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp"
)

# region -> list of (zip filename, {source column: stored factor name})
DATASETS: dict[str, list[tuple[str, dict[str, str]]]] = {
    "US": [
        (
            "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip",
            {"Mkt-RF": "MKT", "SMB": "SMB", "HML": "HML",
             "RMW": "RMW", "CMA": "CMA", "RF": "RF"},
        ),
        ("F-F_Momentum_Factor_daily_CSV.zip", {"Mom": "MOM"}),
    ],
    "JP": [
        (
            "Japan_5_Factors_Daily_CSV.zip",
            {"Mkt-RF": "MKT", "SMB": "SMB", "HML": "HML",
             "RMW": "RMW", "CMA": "CMA", "RF": "RF"},
        ),
        ("Japan_MOM_Factor_Daily_CSV.zip", {"WML": "MOM"}),
    ],
}

MISSING_SENTINELS = (-99.99, -999.0)
_DATE_ROW = re.compile(r"^\s*(\d{8})\s*,")


def _fetch_csv_text(zip_name: str) -> str:
    """Download one French library zip and return its CSV member as text."""
    url = f"{FRENCH_BASE}/{zip_name}"
    with urlopen(url, timeout=120) as resp:
        raw = resp.read()
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not members:
            raise RuntimeError(f"No CSV inside {zip_name}")
        return zf.read(members[0]).decode("utf-8-sig", errors="replace")


def parse_french_csv(text: str, column_map: dict[str, str]) -> pd.DataFrame:
    """
    Parse one French library CSV into long format: date, factor, value.

    Only rows whose first field is an 8-digit date are taken, which
    skips the preamble, the blank lines, the trailing copyright notice,
    and any annual-frequency table appended below the daily one.
    """
    lines = text.splitlines()

    header_idx = next(
        (i for i, line in enumerate(lines) if line.startswith(",")), None
    )
    if header_idx is None:
        raise RuntimeError("Could not locate header row (no line starts with ',')")
    header = [h.strip() for h in lines[header_idx].split(",")]

    rows = []
    for line in lines[header_idx + 1:]:
        m = _DATE_ROW.match(line)
        if not m:
            continue  # preamble, blank, copyright, or a second table
        fields = [f.strip() for f in line.split(",")]
        raw_date = m.group(1)
        date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        for col_idx, col_name in enumerate(header):
            if col_name not in column_map or col_idx >= len(fields):
                continue
            try:
                value = float(fields[col_idx])
            except ValueError:
                continue
            if value in MISSING_SENTINELS:
                continue
            rows.append({
                "date": date,
                "factor": column_map[col_name],
                "value": value / 100.0,  # published in percent
            })

    if not rows:
        raise RuntimeError("Parsed no data rows — file format may have changed")
    return pd.DataFrame(rows)


def get_factor_returns(region: str) -> pd.DataFrame:
    """All factors for one region as long format: date, region, factor, value."""
    if region not in DATASETS:
        raise ValueError(f"Unknown region {region!r}; expected one of {list(DATASETS)}")

    frames = []
    for zip_name, column_map in DATASETS[region]:
        print(f"  fetching {zip_name}")
        df = parse_french_csv(_fetch_csv_text(zip_name), column_map)
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True)
    combined["region"] = region
    # A factor could in principle appear in both files for a region;
    # keep the first occurrence so the primary key stays unique.
    combined = combined.drop_duplicates(subset=["date", "region", "factor"])
    return combined[["date", "region", "factor", "value"]]


def main() -> None:
    from storage import db

    conn = db.connect()
    try:
        total = 0
        for region in DATASETS:
            print(f"{region}:")
            df = get_factor_returns(region)
            n = db.upsert_df(conn, "factor_returns", df)
            total += n
            span = f"{df['date'].min()} to {df['date'].max()}"
            factors = ", ".join(sorted(df["factor"].unique()))
            print(f"  {n} rows | {span} | {factors}")
        print(f"\nfactor_returns  {total} rows total")
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
