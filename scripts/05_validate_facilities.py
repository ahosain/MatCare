#!/usr/bin/env python
"""
05_validate_facilities.py
=========================
Independently cross-check the obstetric facility list against hospitals mapped
in OpenStreetMap, to quantify how much the upstream exact-name join missed.

Why this check exists
---------------------
``MCD.ipynb`` builds the facility list with::

    matched_df = df2[df2['NAME'].isin(df1['FAC_NAME'])]

That is an **exact, case-sensitive string equality** join between the HIFLD
hospital layer and the Texas HHS CMOS obstetric-services list. Any difference in
punctuation, abbreviation, corporate renaming or campus suffix - "ST." vs
"SAINT", "MEDICAL CENTER" vs "MED CTR", "CHRISTUS SOUTHEAST TEXAS ST ELIZABETH"
vs "CHRISTUS ST. ELIZABETH" - silently drops a real facility. A join like this
fails quietly: it produces a smaller, plausible-looking table with no error.

The resulting under-count does not bias the map randomly. Every dropped facility
turns its surroundings into an artificial "desert", inflating measured travel
times exactly where the analysis makes its strongest claims.

The check
---------
OpenStreetMap tags hospitals as ``amenity=hospital``. Those are extracted from
the same MD5-verified Texas extract used to build the road network, aggregated
by county, and compared with the facility list. OSM hospitals are NOT
obstetric-specific, so this cannot confirm which hospitals deliver babies - but a
county with a large population and several mapped hospitals yet **zero** matched
facilities is a strong signal that the join dropped something real.

Outputs
-------
results/tables/facility_validation_by_county.csv
results/tables/suspect_desert_counties.csv

Usage
-----
    python scripts/05_validate_facilities.py
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import osmium
import pandas as pd
from shapely.geometry import Point

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PBF = PROJECT_ROOT / "data" / "raw" / "texas-latest.osm.pbf"
PROC = PROJECT_ROOT / "data" / "processed"
TABLES = PROJECT_ROOT / "results" / "tables"

TX_ALBERS = "EPSG:3083"
WGS84 = "EPSG:4326"


def extract_osm_hospitals() -> gpd.GeoDataFrame:
    """
    Pull amenity=hospital features out of the Texas OSM extract.

    Nodes contribute their own location; ways contribute the centroid of their
    node coordinates. Relations are skipped - hospital multipolygons are rare
    and always have a node or way representation as well.
    """
    print("Scanning OSM extract for amenity=hospital ...", flush=True)
    recs: list[dict] = []

    fp = (
        osmium.FileProcessor(PBF, osmium.osm.NODE | osmium.osm.WAY)
        .with_locations("flex_mem")
        .with_filter(osmium.filter.KeyFilter("amenity"))
    )

    for obj in fp:
        if obj.tags.get("amenity") != "hospital":
            continue
        name = obj.tags.get("name")
        try:
            if isinstance(obj, osmium.osm.Node):
                lon, lat = obj.location.lon, obj.location.lat
            else:
                lons = [n.location.lon for n in obj.nodes if n.location.valid()]
                lats = [n.location.lat for n in obj.nodes if n.location.valid()]
                if not lons:
                    continue
                lon, lat = float(np.mean(lons)), float(np.mean(lats))
        except (osmium.InvalidLocationError, AttributeError):
            continue

        recs.append(
            {
                "osm_name": name,
                "osm_type": "node" if isinstance(obj, osmium.osm.Node) else "way",
                "emergency": obj.tags.get("emergency"),
                "healthcare": obj.tags.get("healthcare"),
                "geometry": Point(lon, lat),
            }
        )

    gdf = gpd.GeoDataFrame(recs, crs=WGS84).to_crs(TX_ALBERS)
    print(f"  {len(gdf):,} hospital features in OSM")

    # A hospital campus is often mapped as both a node and a building way.
    # Collapse features within 250 m that share a name.
    gdf["_key"] = (
        gdf["osm_name"].fillna("").str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
        + "_"
        + (gdf.geometry.x // 250).astype(int).astype(str)
        + "_"
        + (gdf.geometry.y // 250).astype(int).astype(str)
    )
    before = len(gdf)
    gdf = gdf.drop_duplicates("_key").drop(columns="_key")
    print(f"  {before - len(gdf):,} duplicate campus features collapsed -> {len(gdf):,}")
    return gdf


def main() -> int:
    TABLES.mkdir(parents=True, exist_ok=True)

    counties = gpd.read_parquet(PROC / "counties.parquet")
    fac = gpd.read_parquet(PROC / "facilities.parquet").to_crs(TX_ALBERS)
    blocks = pd.read_parquet(
        PROC / "block_access.parquet", columns=["COUNTYFP", "POP20", "net_min", "reachable"]
    )
    # Unreachable blocks carry net_min = inf; including them would turn every
    # county mean containing one into inf. Population totals keep all blocks,
    # the drive-time mean uses reachable blocks only.
    reach = blocks[blocks["reachable"] & np.isfinite(blocks["net_min"])]

    osm = extract_osm_hospitals()
    osm = gpd.sjoin(osm, counties[["COUNTYFP5", "COUNTY_NAME", "geometry"]],
                    how="inner", predicate="within")

    osm_by_cty = (
        osm.groupby("COUNTYFP5").agg(osm_hospitals=("osm_name", "size")).astype(int)
    )
    fac_by_cty = fac.groupby("COUNTYFIPS").agg(matched_facilities=("NAME", "size"))

    pop_by_cty = blocks.groupby("COUNTYFP").agg(
        population=("POP20", "sum"),
    )
    pw = reach.assign(_x=reach["net_min"] * reach["POP20"]).groupby("COUNTYFP")["_x"].sum()
    pw_denom = reach.groupby("COUNTYFP")["POP20"].sum()

    out = (
        counties.set_index("COUNTYFP5")[["COUNTY_NAME"]]
        .join(pop_by_cty)
        .join(osm_by_cty)
        .join(fac_by_cty)
    )
    out["osm_hospitals"] = out["osm_hospitals"].fillna(0).astype(int)
    out["matched_facilities"] = out["matched_facilities"].fillna(0).astype(int)
    out["population"] = out["population"].fillna(0).astype(int)
    out["mean_drive_min"] = (pw / pw_denom.replace(0, np.nan)).round(1)
    out = out.sort_values("population", ascending=False)
    out.to_csv(TABLES / "facility_validation_by_county.csv")

    # A "suspect desert": no matched facility, but OSM maps >=1 hospital there.
    suspect = out[(out["matched_facilities"] == 0) & (out["osm_hospitals"] >= 1)].copy()
    suspect = suspect.sort_values("population", ascending=False)
    suspect.to_csv(TABLES / "suspect_desert_counties.csv")

    print("\n" + "=" * 74)
    print("VALIDATION: counties classified as 'desert' that DO have mapped hospitals")
    print("=" * 74)
    print(f"\n  Texas counties                                : {len(out)}")
    print(f"  Counties with 0 matched obstetric facilities  : {int((out['matched_facilities'] == 0).sum())}")
    print(f"  ...of which OSM maps at least one hospital    : {len(suspect)}")
    print(f"  Population living in those suspect counties   : {int(suspect['population'].sum()):,}")

    print("\n--- 15 largest suspect counties ---")
    print(
        suspect.head(15)[
            ["COUNTY_NAME", "population", "osm_hospitals", "mean_drive_min"]
        ].to_string()
    )

    tot_osm = int(out["osm_hospitals"].sum())
    tot_fac = int(out["matched_facilities"].sum())
    print(f"\n  OSM hospitals statewide      : {tot_osm:,}")
    print(f"  Matched obstetric facilities : {tot_fac:,}  ({100 * tot_fac / tot_osm:.0f}% of OSM count)")
    print(
        "\n  Note: not every hospital provides obstetric care, so the matched\n"
        "  count SHOULD be lower than the OSM count. The signal to act on is the\n"
        "  list above - populous counties with hospitals but no matched facility."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
