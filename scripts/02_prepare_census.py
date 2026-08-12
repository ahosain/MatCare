#!/usr/bin/env python
"""
02_prepare_census.py
====================
Turn the raw Census downloads into analysis-ready population layers.

Geographic units produced
-------------------------
========  ============  ==========================================  ============
unit      count (TX)    centroid definition                          population
========  ============  ==========================================  ============
block     ~490 k        TIGER internal point (INTPTLAT20/LON20)      2020 P.L.
blkgrp    ~18.6 k       Census 2020 population-WEIGHTED center       2020 P.L.
tract     ~6.9 k        Census 2020 population-WEIGHTED center       2020 P.L.
========  ============  ==========================================  ============

Why two different centroid definitions?
---------------------------------------
The Census Bureau publishes population-weighted "Centers of Population" only at
county, tract and block-group level - **there is no block-level equivalent**
(verified against https://www2.census.gov/geo/docs/reference/cenpop2020/, which
contains exactly three subdirectories: county/, tract/, blkgrp/).

For blocks we therefore use the TIGER *internal point*, which the Bureau
guarantees to fall inside the polygon. Because blocks are the finest census
geography (median area well under 1 km^2 in populated areas), the difference
between an internal point and a true population-weighted centroid is small
relative to the drive distances being measured. Block groups and tracts use the
Bureau's official weighted centers, so the coarser units do not inherit the
areal-centroid bias that would otherwise distort rural travel estimates.

Outputs (GeoParquet, EPSG:3083)
-------------------------------
data/processed/blocks.parquet          block polygons + POP20 + HOUSING20
data/processed/block_points.parquet    block internal points (routing input)
data/processed/blockgroups.parquet     block group polygons
data/processed/bg_points.parquet       BG population-weighted centroids
data/processed/tract_points.parquet    tract population-weighted centroids
data/processed/counties.parquet        Texas county polygons (cartographic)
data/processed/places.parquet          incorporated places (for map labels)

Usage
-----
    python scripts/02_prepare_census.py
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW = PROJECT_ROOT / "data" / "raw"
PROC = PROJECT_ROOT / "data" / "processed"
TABLES = PROJECT_ROOT / "results" / "tables"

WGS84 = "EPSG:4326"
TX_ALBERS = "EPSG:3083"
TX_FIPS = "48"


def read_zip(path: Path, **kw) -> gpd.GeoDataFrame:
    """Read a shapefile straight out of its zip archive."""
    print(f"  reading {path.name} ...", flush=True)
    return gpd.read_file(f"zip://{path}", **kw)


def read_cenpop(path: Path, level: str) -> gpd.GeoDataFrame:
    """
    Read a Census 'Centers of Population' file into a point GeoDataFrame.

    Files are UTF-8 with a BOM and carry signed, zero-padded coordinates.
    GEOID is assembled from the component FIPS fields so it matches TIGER.
    """
    df = pd.read_csv(
        path,
        encoding="utf-8-sig",
        dtype={
            "STATEFP": str,
            "COUNTYFP": str,
            "TRACTCE": str,
            "BLKGRPCE": str,
        },
    )
    df = df[df["STATEFP"] == TX_FIPS].copy()

    parts = ["STATEFP", "COUNTYFP"]
    if level in ("tract", "blkgrp"):
        parts.append("TRACTCE")
    if level == "blkgrp":
        parts.append("BLKGRPCE")
    df["GEOID"] = df[parts].agg("".join, axis=1)

    df["LATITUDE"] = pd.to_numeric(df["LATITUDE"], errors="coerce")
    df["LONGITUDE"] = pd.to_numeric(df["LONGITUDE"], errors="coerce")
    df = df.rename(columns={"POPULATION": "POP20"})

    gdf = gpd.GeoDataFrame(
        df[["GEOID", "POP20", "LATITUDE", "LONGITUDE"]],
        geometry=gpd.points_from_xy(df["LONGITUDE"], df["LATITUDE"]),
        crs=WGS84,
    ).to_crs(TX_ALBERS)
    gdf["CENTROID_TYPE"] = "population_weighted"
    return gdf


def main() -> int:
    PROC.mkdir(parents=True, exist_ok=True)
    TABLES.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ blocks
    print("\n[blocks]")
    blocks = read_zip(RAW / "tl_2024_48_tabblock20.zip")
    print(f"  {len(blocks):,} blocks, columns: {list(blocks.columns)[:12]} ...")

    required = {"GEOID20", "POP20", "HOUSING20", "INTPTLAT20", "INTPTLON20", "ALAND20"}
    missing = required - set(blocks.columns)
    if missing:
        raise SystemExit(f"TABBLOCK20 is missing expected fields: {sorted(missing)}")

    blocks["POP20"] = pd.to_numeric(blocks["POP20"], errors="coerce").fillna(0).astype(np.int32)
    blocks["HOUSING20"] = (
        pd.to_numeric(blocks["HOUSING20"], errors="coerce").fillna(0).astype(np.int32)
    )
    blocks["COUNTYFP"] = blocks["GEOID20"].str[:5]

    # TIGER internal point: signed decimal degrees stored as text.
    lat = pd.to_numeric(blocks["INTPTLAT20"], errors="coerce")
    lon = pd.to_numeric(blocks["INTPTLON20"], errors="coerce")
    if lat.isna().any() or lon.isna().any():
        raise SystemExit("Unparseable INTPTLAT20/INTPTLON20 values in TABBLOCK20")

    blocks = blocks.to_crs(TX_ALBERS)
    keep = ["GEOID20", "COUNTYFP", "POP20", "HOUSING20", "ALAND20", "AWATER20", "geometry"]
    blocks[[c for c in keep if c in blocks.columns]].to_parquet(
        PROC / "blocks.parquet", index=False
    )

    block_pts = gpd.GeoDataFrame(
        blocks[["GEOID20", "COUNTYFP", "POP20", "HOUSING20", "ALAND20"]].copy(),
        geometry=gpd.points_from_xy(lon, lat),
        crs=WGS84,
    ).to_crs(TX_ALBERS)
    block_pts["CENTROID_TYPE"] = "tiger_internal_point"
    block_pts.to_parquet(PROC / "block_points.parquet", index=False)

    pop_total = int(blocks["POP20"].sum())
    n_pop = int((blocks["POP20"] > 0).sum())
    print(f"  total 2020 population : {pop_total:,}")
    print(f"  populated blocks      : {n_pop:,} of {len(blocks):,} "
          f"({100 * n_pop / len(blocks):.1f}%)")

    # ------------------------------------------------------------- block groups
    print("\n[block groups]")
    bg = read_zip(RAW / "tl_2024_48_bg.zip").to_crs(TX_ALBERS)
    bg["COUNTYFP5"] = bg["GEOID"].str[:5]
    bg[["GEOID", "COUNTYFP5", "ALAND", "AWATER", "geometry"]].to_parquet(
        PROC / "blockgroups.parquet", index=False
    )
    bg_pts = read_cenpop(RAW / "CenPop2020_Mean_BG48.txt", "blkgrp")
    bg_pts["COUNTYFP5"] = bg_pts["GEOID"].str[:5]
    bg_pts.to_parquet(PROC / "bg_points.parquet", index=False)
    print(f"  {len(bg):,} polygons, {len(bg_pts):,} weighted centroids, "
          f"pop {int(bg_pts['POP20'].sum()):,}")

    unmatched = set(bg["GEOID"]) ^ set(bg_pts["GEOID"])
    if unmatched:
        print(f"  [warn] {len(unmatched)} GEOID(s) not present in both BG sources")

    # -------------------------------------------------------------------- tracts
    print("\n[tracts]")
    tr_pts = read_cenpop(RAW / "CenPop2020_Mean_TR48.txt", "tract")
    tr_pts["COUNTYFP5"] = tr_pts["GEOID"].str[:5]
    tr_pts.to_parquet(PROC / "tract_points.parquet", index=False)
    print(f"  {len(tr_pts):,} weighted centroids, pop {int(tr_pts['POP20'].sum()):,}")

    # ------------------------------------------------------------------ counties
    print("\n[counties]")
    cty = read_zip(RAW / "cb_2023_us_county_500k.zip")
    state_col = "STATEFP" if "STATEFP" in cty.columns else "STATEFP20"
    cty = cty[cty[state_col] == TX_FIPS].to_crs(TX_ALBERS)
    cty = cty.rename(columns={"NAME": "COUNTY_NAME", "GEOID": "COUNTYFP5"})
    cty[["COUNTYFP5", "COUNTY_NAME", "ALAND", "AWATER", "geometry"]].to_parquet(
        PROC / "counties.parquet", index=False
    )
    print(f"  {len(cty)} Texas counties")
    if len(cty) != 254:
        print(f"  [warn] expected 254 Texas counties, got {len(cty)}")

    # -------------------------------------------------------------------- places
    print("\n[places]")
    pl = read_zip(RAW / "tl_2024_48_place.zip").to_crs(TX_ALBERS)
    pl = pl.rename(columns={"NAME": "PLACE_NAME"})
    pl[["GEOID", "PLACE_NAME", "ALAND", "geometry"]].to_parquet(
        PROC / "places.parquet", index=False
    )
    print(f"  {len(pl):,} incorporated places / CDPs")

    # ------------------------------------------------------- consistency report
    print("\n[cross-check] 2020 population by source")
    rows = [
        ("blocks (TABBLOCK20 POP20)", pop_total),
        ("block groups (CenPop2020)", int(bg_pts["POP20"].sum())),
        ("tracts (CenPop2020)", int(tr_pts["POP20"].sum())),
    ]
    for label, val in rows:
        print(f"  {label:32s} {val:>12,}")
    spread = max(v for _, v in rows) - min(v for _, v in rows)
    print(f"  max discrepancy: {spread:,}")
    pd.DataFrame(rows, columns=["source", "population_2020"]).to_csv(
        TABLES / "population_crosscheck.csv", index=False
    )

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
