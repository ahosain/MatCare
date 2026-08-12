#!/usr/bin/env python
"""
10_prepare_acs_women.py
=======================
Build the **women aged 15-44** denominator - the population that actually uses
obstetric care - and recompute access statistics against it.

Why this matters
----------------
Every statistic in the pipeline up to this point weights by *total* population.
That answers "how far is the average Texan from obstetric care", which is not the
question. The right denominator is women of reproductive age, and their spatial
distribution is not the same as everyone else's: university towns, retirement
communities and prisons all skew it. Reviewers of a maternal-health proposal will
expect the correct denominator.

No API key required
-------------------
``api.census.gov`` now rejects unauthenticated requests with ``Missing Key``.
This script sidesteps that entirely by reading the **table-based ACS Summary
File**, a bulk download that needs no registration and carries identical data.

Definition (read from the official table shells, not from memory)
-----------------------------------------------------------------
Table **B01001 "Sex by Age"**, female lines covering ages 15-44:

    B01001_030  Female: 15 to 17 years        B01001_035  Female: 25 to 29 years
    B01001_031  Female: 18 and 19 years       B01001_036  Female: 30 to 34 years
    B01001_032  Female: 20 years              B01001_037  Female: 35 to 39 years
    B01001_033  Female: 21 years              B01001_038  Female: 40 to 44 years
    B01001_034  Female: 22 to 24 years

The column list is *derived at runtime* from ``ACS20235YR_Table_Shells.txt`` by
parsing the published labels, so a mis-remembered variable number cannot silently
corrupt the denominator. (An earlier draft of the project notes had the range as
_030 to _039; _039 is "45 to 49 years". The parser catches that class of error.)

Geography
---------
ACS publishes block-group data but **not block data** - the sample is too small.
So:

* Access is measured at the **block group** centroid, using the Census Bureau's
  official 2020 population-weighted centre of population.
* For block-level work (siting), block-group women are **disaggregated to blocks
  in proportion to each block's 2020 decennial population**. This is a standard
  areal-interpolation step and is explicitly an assumption: it presumes the age
  and sex mix is uniform within a block group.

Outputs
-------
data/population/processed/bg_women.parquet     block group women 15-44 + centroid
data/population/processed/block_women.parquet  disaggregated to blocks
results/tables/women_access_summary.csv        access distribution
results/tables/women_access_thresholds.csv     % within mileage/time benchmarks
results/tables/women_county_access.csv         county roll-up

Usage
-----
    python scripts/10_prepare_acs_women.py
"""

from __future__ import annotations

import re
import time

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

import paths as P

TABLES = P.TABLES
ACS_DAT = P.POPULATION_RAW / "acsdt5y2023-b01001.dat"
SHELLS = P.POPULATION_RAW / "ACS20235YR_Table_Shells.txt"

# Block group GEO_ID prefix in the summary file, Texas.
BG_PREFIX = "1500000US48"

# Planning benchmarks. Texas sets no statewide mileage standard - maternal care
# is designated by capability level - so these are planning benchmarks, not
# statutory requirements. Miles mirror the previous proposal; minutes are the
# conventional breakpoints in the access literature.
MILE_BENCHMARKS = (25, 35, 50)
TIME_BENCHMARKS = (30, 60)
KM_PER_MILE = 1.609344


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def women_15_44_columns() -> list[str]:
    """
    Derive the B01001 column list for females aged 15-44 from the official
    table shells, rather than hard-coding variable numbers.
    """
    shells = pd.read_csv(SHELLS, sep="|", encoding="utf-8-sig", dtype=str)
    b = shells[shells["Table ID"] == "B01001"].copy()
    b["line"] = b["Line"].astype(float)

    # Lines after the "Female:" header belong to women.
    female_start = b.loc[b["Label"].str.strip() == "Female:", "line"].iloc[0]
    fem = b[b["line"] > female_start]

    keep: list[str] = []
    for _, r in fem.iterrows():
        label = r["Label"].strip()
        nums = [int(n) for n in re.findall(r"\d+", label)]
        if not nums:
            continue
        lo = nums[0]
        hi = nums[1] if len(nums) > 1 else lo
        if "under" in label.lower():
            continue
        if "over" in label.lower():
            continue
        if lo >= 15 and hi <= 44:
            keep.append(r["Unique ID"])
            print(f"    {r['Unique ID']}  Female: {label}")
    if not keep:
        raise SystemExit("Could not derive women 15-44 columns from table shells")
    return keep


def main() -> int:
    P.ensure_tree()
    if not ACS_DAT.exists():
        raise SystemExit(f"missing {ACS_DAT}\n  run: python scripts/00_download_data.py "
                         "--only acs_b01001 acs_shells")

    # -------------------------------------------------- derive the variables
    log("Deriving women 15-44 columns from the official ACS table shells ...")
    cols = women_15_44_columns()
    # The .dat file names estimate columns B01001_Ennn, not B01001_nnn.
    est_cols = [c.replace("B01001_", "B01001_E") for c in cols]
    log(f"  {len(cols)} age bands: {cols[0]} .. {cols[-1]}")

    # ------------------------------------------------------- read ACS, Texas
    log("Reading ACS B01001 (streaming, Texas block groups only) ...")
    keep_cols = ["GEO_ID", "B01001_E001", "B01001_E026"] + est_cols
    chunks = []
    for chunk in pd.read_csv(ACS_DAT, sep="|", dtype={"GEO_ID": str},
                             usecols=keep_cols, chunksize=200_000, low_memory=False):
        sel = chunk[chunk["GEO_ID"].str.startswith(BG_PREFIX)]
        if len(sel):
            chunks.append(sel)
    acs = pd.concat(chunks, ignore_index=True)
    log(f"  {len(acs):,} Texas block groups")

    for c in est_cols + ["B01001_E001", "B01001_E026"]:
        acs[c] = pd.to_numeric(acs[c], errors="coerce").fillna(0)

    acs["GEOID"] = acs["GEO_ID"].str.replace("1500000US", "", regex=False)
    acs["women_15_44"] = acs[est_cols].sum(axis=1).astype(int)
    acs["total_pop_acs"] = acs["B01001_E001"].astype(int)
    acs["female_total"] = acs["B01001_E026"].astype(int)

    tot_w = int(acs["women_15_44"].sum())
    log(f"  women aged 15-44 in Texas: {tot_w:,}")
    log(f"  share of ACS total population: "
        f"{100 * tot_w / acs['total_pop_acs'].sum():.1f}%")

    # ------------------------------------- join to weighted centroids
    bg_pts = gpd.read_parquet(P.CENTROIDS_PROC / "bg_points.parquet").to_crs(P.TX_ALBERS)
    bg = bg_pts.merge(
        acs[["GEOID", "women_15_44", "total_pop_acs", "female_total"]],
        on="GEOID", how="left",
    )
    miss = bg["women_15_44"].isna().sum()
    if miss:
        log(f"  [warn] {miss} block group(s) present in TIGER but not in ACS "
            "(usually zero-population water or new splits) - treated as 0")
    bg["women_15_44"] = bg["women_15_44"].fillna(0).astype(int)

    # ---------------------------------------------- access at BG centroids
    log("Computing drive time from block-group population-weighted centroids ...")
    nodes = pd.read_parquet(P.NETWORK_PROC / "network_nodes.parquet")
    edges = pd.read_parquet(P.NETWORK_PROC / "network_edges.parquet",
                            columns=["u", "v", "length_m", "time_s"])
    tf = Transformer.from_crs(P.WGS84, P.TX_ALBERS, always_xy=True)
    x, y = tf.transform(nodes["lon"].to_numpy(), nodes["lat"].to_numpy())
    tree = cKDTree(np.column_stack([x, y]))

    fac = gpd.read_parquet(P.FACILITIES_PROC / "facilities_analysis.parquet").to_crs(P.TX_ALBERS)
    _, fnode = tree.query(np.column_stack([fac.geometry.x, fac.geometry.y]), k=1)
    src = np.unique(fnode)

    def sssp(weight: str) -> np.ndarray:
        g = csr_matrix(
            (edges[weight].to_numpy(np.float64),
             (edges["u"].to_numpy(), edges["v"].to_numpy())),
            shape=(len(nodes), len(nodes)),
        ).T.tocsr()
        return dijkstra(g, directed=True, indices=src, min_only=True)

    d_m = sssp("length_m")
    d_s = sssp("time_s")

    b_xy = np.column_stack([bg.geometry.x, bg.geometry.y])
    snap, bnode = tree.query(b_xy, k=1)
    bg["drive_km"] = (d_m[bnode] + snap) / 1000.0
    bg["drive_min"] = (d_s[bnode] + snap / (30 * 0.44704)) / 60.0
    bg["drive_mi"] = bg["drive_km"] / KM_PER_MILE

    nic = fac[fac["NICU_ONSITE"]]
    _, nnode = tree.query(np.column_stack([nic.geometry.x, nic.geometry.y]), k=1)
    src = np.unique(nnode)
    bg["nicu_min"] = (sssp("time_s")[bnode] + snap / (30 * 0.44704)) / 60.0

    bg.to_parquet(P.POPULATION_PROC / "bg_women.parquet", index=False)
    log(f"  wrote {P.POPULATION_PROC / 'bg_women.parquet'}")

    # ------------------------------------------ disaggregate to blocks
    log("Disaggregating block-group women to blocks, proportional to POP20 ...")
    blk = pd.read_parquet(P.POPULATION_PROC / "block_points.parquet",
                          columns=["GEOID20", "COUNTYFP", "POP20"])
    blk["BG"] = blk["GEOID20"].str[:12]
    bg_pop = blk.groupby("BG")["POP20"].transform("sum")
    share = np.where(bg_pop > 0, blk["POP20"] / bg_pop.replace(0, np.nan), 0.0)
    blk["women_15_44"] = (
        blk["BG"].map(bg.set_index("GEOID")["women_15_44"]).fillna(0).to_numpy()
        * np.nan_to_num(share)
    )
    blk[["GEOID20", "COUNTYFP", "POP20", "BG", "women_15_44"]].to_parquet(
        P.POPULATION_PROC / "block_women.parquet", index=False)
    log(f"  block total {blk['women_15_44'].sum():,.0f} vs BG total {tot_w:,} "
        f"(diff {blk['women_15_44'].sum() - tot_w:+,.0f})")

    # --------------------------------------------------------- statistics
    ok = np.isfinite(bg["drive_min"]) & (bg["women_15_44"] > 0)
    w = bg.loc[ok, "women_15_44"].to_numpy(float)
    tmin = bg.loc[ok, "drive_min"].to_numpy()
    dmi = bg.loc[ok, "drive_mi"].to_numpy()
    nmin = bg.loc[ok, "nicu_min"].to_numpy()
    W = w.sum()

    def wq(v, q):
        o = np.argsort(v)
        return float(np.interp(q, np.cumsum(w[o]) / w.sum(), v[o]))

    rows = []
    for label, v, unit in (("drive_time_min", tmin, "min"),
                           ("drive_distance_mi", dmi, "miles"),
                           ("nicu_drive_time_min", nmin, "min")):
        rows.append({"metric": label, "unit": unit,
                     "mean": float(np.average(v, weights=w)),
                     "p50": wq(v, .50), "p75": wq(v, .75), "p90": wq(v, .90),
                     "p95": wq(v, .95), "p99": wq(v, .99), "max": float(v.max())})
    summary = pd.DataFrame(rows)
    summary.to_csv(TABLES / "women_access_summary.csv", index=False)

    thr = []
    for mi in MILE_BENCHMARKS:
        n = float(w[dmi <= mi].sum())
        thr.append({"benchmark": f"within {mi} miles", "women_15_44": int(n),
                    "pct": round(100 * n / W, 2)})
    for mn in TIME_BENCHMARKS:
        n = float(w[tmin <= mn].sum())
        thr.append({"benchmark": f"within {mn} min", "women_15_44": int(n),
                    "pct": round(100 * n / W, 2)})
        n2 = float(w[nmin <= mn].sum())
        thr.append({"benchmark": f"within {mn} min of NICU", "women_15_44": int(n2),
                    "pct": round(100 * n2 / W, 2)})
    thresholds = pd.DataFrame(thr)
    thresholds.to_csv(TABLES / "women_access_thresholds.csv", index=False)

    cty = bg.loc[ok].assign(_t=lambda d: d["drive_min"] * d["women_15_44"]).groupby("COUNTYFP5")
    counties = gpd.read_parquet(P.BOUNDARIES_PROC / "counties.parquet")
    cdf = pd.DataFrame({"women_15_44": cty["women_15_44"].sum(),
                        "mean_drive_min": cty["_t"].sum() / cty["women_15_44"].sum()})
    cdf = cdf.join(counties.set_index("COUNTYFP5")["COUNTY_NAME"])
    cdf.sort_values("mean_drive_min", ascending=False).to_csv(
        TABLES / "women_county_access.csv")

    print("\n" + "=" * 68)
    print("ACCESS FOR WOMEN AGED 15-44")
    print(f"({int(W):,} women, {len(fac)} facilities, block-group resolution)")
    print("=" * 68)
    print(summary.to_string(index=False, float_format=lambda v: f"{v:8.2f}"))
    print("\n--- planning benchmarks ---")
    print(thresholds.to_string(index=False))
    print("\n--- 10 worst counties for women 15-44 ---")
    print(cdf.sort_values("mean_drive_min", ascending=False).head(10)
          .to_string(float_format=lambda v: f"{v:.1f}"))

    beyond30 = int(w[tmin > 30].sum())
    print(f"\n  beyond 30 min of any obstetric facility : {beyond30:,} "
          f"({100 * beyond30 / W:.2f}%)")
    n30 = int(w[nmin > 30].sum())
    print(f"  beyond 30 min of a NICU-capable facility: {n30:,} "
          f"({100 * n30 / W:.2f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
