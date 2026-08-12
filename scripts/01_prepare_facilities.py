#!/usr/bin/env python
"""
01_prepare_facilities.py
========================
Clean the cross-referenced Texas obstetric facility list and turn it into an
analysis-ready GeoParquet point layer.

Input
-----
``data/texas_obs_facilities_final.csv`` - produced by ``MCD.ipynb`` by joining
the HIFLD hospital layer to the Texas HHS CMOS (Certificate of Maternal and
Obstetric Services) facility list on an exact facility-name match.

What this script fixes
----------------------
1. **Unusable geometry column.** The CSV's ``geometry`` field is a Python
   ``bytes`` repr of WKB that was stringified on write - it cannot be parsed
   back. Geometry is rebuilt from ``LONGITUDE`` / ``LATITUDE`` instead.
2. **HIFLD sentinel values.** HIFLD encodes "unknown" as ``-999`` and
   ``"NOT AVAILABLE"``. Those are converted to proper nulls so they cannot be
   averaged into a statistic by accident.
3. **Duplicate facilities.** De-duplicated on HIFLD ``ID``, then flagged where
   a name repeats across distinct IDs (multi-campus systems).
4. **Facility types that do not provide obstetric care.** The upstream join was
   an exact string match on facility name, which admits false positives. A
   psychiatric, rehabilitation or long-term-care hospital does not staff a
   labour-and-delivery unit. These are *flagged*, not silently dropped, and the
   analysis set is controlled by ``--include-types``.
5. **Coordinate sanity.** Points are checked against the Texas bounding box.

Outputs
-------
``data/processed/facilities.parquet``      all rows, cleaned + flagged
``data/processed/facilities_analysis.parquet``  the modelling subset
``results/tables/facility_summary.csv``    counts by type / ownership / county

Usage
-----
    python scripts/01_prepare_facilities.py
    python scripts/01_prepare_facilities.py --include-types ALL
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_CSV = PROJECT_ROOT / "data" / "texas_obs_facilities_final.csv"
PROC_DIR = PROJECT_ROOT / "data" / "processed"
TABLE_DIR = PROJECT_ROOT / "results" / "tables"

WGS84 = "EPSG:4326"
# NAD83 / Texas Centric Albers Equal Area - equal-area, metre units, the right
# choice for statewide distance and area work in Texas.
TX_ALBERS = "EPSG:3083"

# Texas bounding box (WGS84) with a small margin, for coordinate validation.
TX_BBOX = dict(lon_min=-107.0, lon_max=-93.4, lat_min=25.7, lat_max=36.6)

# HIFLD sentinel values meaning "not reported".
HIFLD_NULLS = {-999, -999.0, "-999", "NOT AVAILABLE", "NOT-AVAILABLE", ""}

# Hospital types that plausibly operate a labour-and-delivery unit. Psychiatric,
# rehabilitation and long-term-care hospitals do not; children's hospitals may
# run a NICU but generally do not deliver, so they are excluded from the core
# set and retained for sensitivity analysis.
OBSTETRIC_PLAUSIBLE_TYPES = {"GENERAL ACUTE CARE", "CRITICAL ACCESS"}


def blank_sentinels(s: pd.Series) -> pd.Series:
    """Replace HIFLD 'unknown' sentinels with NA."""
    return s.mask(s.isin(HIFLD_NULLS))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--include-types",
        default="CORE",
        choices=["CORE", "ALL"],
        help=(
            "CORE (default) keeps GENERAL ACUTE CARE + CRITICAL ACCESS; "
            "ALL keeps every matched facility for sensitivity analysis."
        ),
    )
    args = ap.parse_args()

    PROC_DIR.mkdir(parents=True, exist_ok=True)
    TABLE_DIR.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(SRC_CSV)
    n_in = len(df)
    print(f"Read {n_in} rows from {SRC_CSV.relative_to(PROJECT_ROOT)}")

    # -- 1. drop unusable columns ------------------------------------------
    # 'geometry' and 'bbox' are stringified Python reprs, not parseable WKB.
    df = df.drop(columns=[c for c in ("geometry", "bbox") if c in df.columns])

    # -- 2. sentinels -> NA --------------------------------------------------
    for col in ("BEDS", "TTL_STAFF", "POPULATION"):
        if col in df.columns:
            df[col] = pd.to_numeric(blank_sentinels(df[col]), errors="coerce")
    for col in ("ALT_NAME", "WEBSITE", "TELEPHONE", "TRAUMA", "ZIP4", "OWNER"):
        if col in df.columns:
            df[col] = blank_sentinels(df[col].astype("string").str.strip())

    # -- 3. tidy key text fields --------------------------------------------
    for col in ("NAME", "CITY", "COUNTY", "TYPE", "STATUS", "ADDRESS"):
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip().str.upper()

    df["COUNTYFIPS"] = (
        df["COUNTYFIPS"].astype("string").str.replace(r"\.0$", "", regex=True).str.zfill(5)
    )

    # -- 4. coordinate validation -------------------------------------------
    df["LATITUDE"] = pd.to_numeric(df["LATITUDE"], errors="coerce")
    df["LONGITUDE"] = pd.to_numeric(df["LONGITUDE"], errors="coerce")

    bad_coord = (
        df["LATITUDE"].isna()
        | df["LONGITUDE"].isna()
        | ~df["LATITUDE"].between(TX_BBOX["lat_min"], TX_BBOX["lat_max"])
        | ~df["LONGITUDE"].between(TX_BBOX["lon_min"], TX_BBOX["lon_max"])
    )
    if bad_coord.any():
        print(f"  [warn] {int(bad_coord.sum())} row(s) outside the Texas bbox - dropped:")
        for _, r in df.loc[bad_coord, ["NAME", "LATITUDE", "LONGITUDE"]].iterrows():
            print(f"         {r['NAME']}  ({r['LATITUDE']}, {r['LONGITUDE']})")
        df = df.loc[~bad_coord].copy()

    # -- 5. de-duplicate -----------------------------------------------------
    dup_id = df["ID"].duplicated().sum()
    if dup_id:
        print(f"  [info] dropping {dup_id} row(s) duplicated on HIFLD ID")
        df = df.drop_duplicates(subset="ID").copy()

    # Same name at different IDs = separate campuses; keep both but mark them.
    df["NAME_REPEATED"] = df["NAME"].duplicated(keep=False)
    n_rep = int(df["NAME_REPEATED"].sum())
    if n_rep:
        print(f"  [info] {n_rep} row(s) share a facility name across distinct IDs")

    # Coincident coordinates are a stronger duplicate signal than name.
    coord_key = list(zip(df["LATITUDE"].round(5), df["LONGITUDE"].round(5)))
    df["COORD_REPEATED"] = pd.Series(coord_key, index=df.index).duplicated(keep=False)
    if df["COORD_REPEATED"].any():
        print(
            f"  [warn] {int(df['COORD_REPEATED'].sum())} row(s) share identical "
            "coordinates - possible duplicate facilities"
        )

    # -- 6. obstetric plausibility flag --------------------------------------
    df["OB_PLAUSIBLE"] = df["TYPE"].isin(OBSTETRIC_PLAUSIBLE_TYPES)
    excluded = df.loc[~df["OB_PLAUSIBLE"], "TYPE"].value_counts()
    if len(excluded):
        print("  [flag] facility types unlikely to provide obstetric care:")
        for t, n in excluded.items():
            print(f"         {t:22s} {n}")

    # -- 7. build the GeoDataFrame -------------------------------------------
    gdf = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["LONGITUDE"], df["LATITUDE"]),
        crs=WGS84,
    ).to_crs(TX_ALBERS)

    gdf["FAC_ID"] = np.arange(len(gdf), dtype=np.int32)

    out_all = PROC_DIR / "facilities.parquet"
    gdf.to_parquet(out_all, index=False)
    print(f"\nWrote {len(gdf)} cleaned facilities -> {out_all.relative_to(PROJECT_ROOT)}")

    # -- 8. analysis subset ---------------------------------------------------
    keep = gdf["OB_PLAUSIBLE"] if args.include_types == "CORE" else pd.Series(True, index=gdf.index)
    sub = gdf.loc[keep].copy()
    out_sub = PROC_DIR / "facilities_analysis.parquet"
    sub.to_parquet(out_sub, index=False)
    print(
        f"Wrote {len(sub)} analysis facilities (--include-types {args.include_types}) "
        f"-> {out_sub.relative_to(PROJECT_ROOT)}"
    )

    # -- 9. summary tables -----------------------------------------------------
    summary = (
        gdf.groupby("TYPE", dropna=False)
        .agg(
            n_facilities=("FAC_ID", "size"),
            n_counties=("COUNTYFIPS", "nunique"),
            total_beds=("BEDS", "sum"),
            median_beds=("BEDS", "median"),
            ob_plausible=("OB_PLAUSIBLE", "first"),
        )
        .sort_values("n_facilities", ascending=False)
    )
    summary.to_csv(TABLE_DIR / "facility_summary.csv")

    print("\n--- Facilities by type ---")
    print(summary.to_string())
    print(
        f"\nCounties with >=1 analysis facility: {sub['COUNTYFIPS'].nunique()} of 254"
        f"\nCounties with NO analysis facility : {254 - sub['COUNTYFIPS'].nunique()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
