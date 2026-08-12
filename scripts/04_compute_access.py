#!/usr/bin/env python
"""
04_compute_access.py
====================
Compute, for every Texas census block (and block group / tract / county), the
road-network driving distance and driving time to the NEAREST obstetric
facility, plus the straight-line comparison.

The algorithm
-------------
The naive framing - "distance from each of 668,757 blocks to each of 160
facilities" - is a 107-million-pair matrix nobody needs, because the question is
only ever about the *nearest* facility.

Instead this uses a single **multi-source Dijkstra**. Seeding the priority queue
with all 160 facility nodes at once and relaxing outward labels every node in
the network with (a) its cost to the closest facility and (b) which facility
that is, in one pass over the graph. SciPy exposes exactly this via
``dijkstra(..., min_only=True)``.

Direction matters. Multi-source Dijkstra on the graph G yields
cost(facility -> node). What we want is cost(node -> facility), i.e. a pregnant
patient travelling *to* care. On a directed network with one-way streets those
differ, so the search is run on the **transpose** G^T.

Cost is computed twice: once weighted by ``length_m`` (distance) and once by
``time_s`` (travel time).

Off-network access
------------------
A census centroid never sits exactly on a road. Each centroid is snapped to the
nearest network node with a KD-tree, and the residual straight-line "snap"
distance is added back: as metres for distance, and at an assumed 30 mph local
speed for time. Snap distances are reported so outliers (very remote blocks) are
visible rather than hidden.

Outputs
-------
data/processed/block_access.parquet     per-block results
results/tables/county_access.csv        population-weighted county summary
results/tables/access_summary.csv       statewide distribution
results/tables/desert_counties.csv      counties with no facility, ranked

Usage
-----
    python scripts/04_compute_access.py
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROC = PROJECT_ROOT / "data" / "processed"
TABLES = PROJECT_ROOT / "results" / "tables"

TX_ALBERS = "EPSG:3083"
WGS84 = "EPSG:4326"

# Assumed speed for the off-network "last mile" between a census centroid and
# the nearest mapped road, in mph. Documented assumption, not a measurement.
SNAP_SPEED_MPH = 30.0
MPH_TO_MS = 0.44704

# Reporting thresholds. 30 and 60 minutes are the conventional breakpoints in
# the obstetric-access literature; 50 miles mirrors HRSA-style distance rules.
TIME_BINS_MIN = [0, 15, 30, 45, 60, 90, np.inf]
DIST_BINS_KM = [0, 10, 25, 50, 80, 160, np.inf]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_graph(edges: pd.DataFrame, n_nodes: int, weight: str) -> csr_matrix:
    """Directed sparse adjacency weighted by `weight`."""
    return csr_matrix(
        (edges[weight].to_numpy(np.float64), (edges["u"].to_numpy(), edges["v"].to_numpy())),
        shape=(n_nodes, n_nodes),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--facilities",
        default="facilities_analysis.parquet",
        help="facility layer in data/processed to route to",
    )
    args = ap.parse_args()

    TABLES.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ load
    log("Loading network ...")
    nodes = pd.read_parquet(PROC / "network_nodes.parquet")
    edges = pd.read_parquet(PROC / "network_edges.parquet", columns=["u", "v", "length_m", "time_s"])
    n_nodes = len(nodes)
    log(f"  {n_nodes:,} nodes / {len(edges):,} directed edges")

    log("Projecting node coordinates to EPSG:3083 ...")
    tf = Transformer.from_crs(WGS84, TX_ALBERS, always_xy=True)
    nx_, ny_ = tf.transform(nodes["lon"].to_numpy(), nodes["lat"].to_numpy())
    node_xy = np.column_stack([nx_, ny_])
    del nx_, ny_

    log("Building KD-tree over network nodes ...")
    tree = cKDTree(node_xy)

    # ------------------------------------------------------------ facilities
    fac = gpd.read_parquet(PROC / args.facilities).to_crs(TX_ALBERS)
    fac_xy = np.column_stack([fac.geometry.x.to_numpy(), fac.geometry.y.to_numpy()])
    fac_snap_m, fac_node = tree.query(fac_xy, k=1)
    log(f"  {len(fac)} facilities snapped to network "
        f"(median snap {np.median(fac_snap_m):.0f} m, max {fac_snap_m.max():.0f} m)")

    far = fac_snap_m > 2000
    if far.any():
        log(f"  [warn] {int(far.sum())} facility(ies) >2 km from any drivable road:")
        for nm, d in zip(fac.loc[far, "NAME"], fac_snap_m[far]):
            log(f"         {nm}  {d / 1000:.1f} km")

    src_nodes = np.unique(fac_node).astype(np.int32)

    # --------------------------------------------------- reachability report
    log("Checking network connectivity ...")
    g_len = build_graph(edges, n_nodes, "length_m")
    n_comp, labels = connected_components(g_len, directed=True, connection="weak")
    comp_sizes = np.bincount(labels)
    main_comp = int(comp_sizes.argmax())
    log(f"  {n_comp:,} weakly-connected components; largest holds "
        f"{comp_sizes[main_comp]:,} nodes ({100 * comp_sizes[main_comp] / n_nodes:.2f}%)")

    # ------------------------------------------------------ multi-source SSSP
    # Transpose so the search measures cost(node -> facility), the direction a
    # patient actually travels.
    log("Running multi-source Dijkstra on the transposed graph (distance) ...")
    t0 = time.time()
    gt_len = g_len.T.tocsr()
    del g_len
    dist_m, _, src_of = dijkstra(
        gt_len, directed=True, indices=src_nodes, min_only=True, return_predecessors=True
    )
    del gt_len
    log(f"  distance pass done in {time.time() - t0:.0f}s")

    log("Running multi-source Dijkstra (travel time) ...")
    t0 = time.time()
    g_time = build_graph(edges, n_nodes, "time_s")
    gt_time = g_time.T.tocsr()
    del g_time, edges
    time_s = dijkstra(gt_time, directed=True, indices=src_nodes, min_only=True)
    del gt_time
    log(f"  time pass done in {time.time() - t0:.0f}s")

    reach = np.isfinite(dist_m)
    log(f"  {reach.sum():,} of {n_nodes:,} nodes can reach a facility "
        f"({100 * reach.mean():.2f}%)")

    # Map the winning source node back to a facility row.
    node_to_fac = np.full(n_nodes, -1, dtype=np.int32)
    fac_by_node: dict[int, int] = {}
    for i, nd in enumerate(fac_node):
        fac_by_node.setdefault(int(nd), i)
    lut = np.full(n_nodes, -1, dtype=np.int32)
    for nd, fi in fac_by_node.items():
        lut[nd] = fi
    valid_src = (src_of >= 0) & (src_of < n_nodes)
    node_to_fac[valid_src] = lut[src_of[valid_src]]

    # --------------------------------------------------------------- blocks
    log("Loading block centroids and snapping ...")
    blocks = gpd.read_parquet(PROC / "block_points.parquet").to_crs(TX_ALBERS)
    b_xy = np.column_stack([blocks.geometry.x.to_numpy(), blocks.geometry.y.to_numpy()])
    snap_m, b_node = tree.query(b_xy, k=1)
    log(f"  {len(blocks):,} blocks snapped "
        f"(median {np.median(snap_m):.0f} m, p99 {np.percentile(snap_m, 99):.0f} m, "
        f"max {snap_m.max() / 1000:.1f} km)")

    net_m = dist_m[b_node] + snap_m
    net_s = time_s[b_node] + snap_m / (SNAP_SPEED_MPH * MPH_TO_MS)
    fac_i = node_to_fac[b_node]

    # Straight-line comparison, facility-to-block, for the detour ratio.
    fac_tree = cKDTree(fac_xy)
    sl_m, sl_i = fac_tree.query(b_xy, k=1)

    out = pd.DataFrame(
        {
            "GEOID20": blocks["GEOID20"].to_numpy(),
            "COUNTYFP": blocks["COUNTYFP"].to_numpy(),
            "POP20": blocks["POP20"].to_numpy(),
            "HOUSING20": blocks["HOUSING20"].to_numpy(),
            "snap_m": snap_m.astype(np.float32),
            "net_km": (net_m / 1000).astype(np.float32),
            "net_min": (net_s / 60).astype(np.float32),
            "straight_km": (sl_m / 1000).astype(np.float32),
            "nearest_fac_i": fac_i,
            "nearest_fac_straight_i": sl_i.astype(np.int32),
            "reachable": np.isfinite(net_m),
        }
    )
    out["detour_ratio"] = (out["net_km"] / out["straight_km"].replace(0, np.nan)).astype(np.float32)

    name_by_i = fac["NAME"].reset_index(drop=True)
    out["nearest_fac_name"] = (
        pd.Series(fac_i).map(name_by_i).where(pd.Series(fac_i) >= 0).to_numpy()
    )

    unreach = ~out["reachable"]
    if unreach.any():
        log(f"  [warn] {int(unreach.sum()):,} blocks unreachable "
            f"({int(out.loc[unreach, 'POP20'].sum()):,} people) - islands in the OSM graph")

    out.to_parquet(PROC / "block_access.parquet", index=False)
    log(f"Wrote {PROC / 'block_access.parquet'}")

    # ------------------------------------------------------------- summaries
    pop = out["POP20"].to_numpy()
    ok = out["reachable"].to_numpy() & (pop > 0)
    w = pop[ok]
    tmin = out["net_min"].to_numpy()[ok]
    dkm = out["net_km"].to_numpy()[ok]

    def wq(vals: np.ndarray, q: float) -> float:
        """Population-weighted quantile."""
        o = np.argsort(vals)
        v, ww = vals[o], w[o]
        c = np.cumsum(ww) / ww.sum()
        return float(np.interp(q, c, v))

    rows = []
    for label, vals, unit in (("drive_time_min", tmin, "min"), ("drive_distance_km", dkm, "km")):
        rows.append(
            {
                "metric": label,
                "unit": unit,
                "pop_weighted_mean": float(np.average(vals, weights=w)),
                "p50": wq(vals, 0.50),
                "p75": wq(vals, 0.75),
                "p90": wq(vals, 0.90),
                "p95": wq(vals, 0.95),
                "p99": wq(vals, 0.99),
                "max": float(vals.max()),
            }
        )
    summary = pd.DataFrame(rows)
    summary.to_csv(TABLES / "access_summary.csv", index=False)

    total_pop = int(w.sum())
    band_rows = []
    for lo, hi in zip(TIME_BINS_MIN[:-1], TIME_BINS_MIN[1:]):
        m = (tmin >= lo) & (tmin < hi)
        band_rows.append(
            {
                "band_min": f"{lo}-{hi:g}" if np.isfinite(hi) else f"{lo}+",
                "population": int(w[m].sum()),
                "pct_of_state": round(100 * w[m].sum() / total_pop, 2),
            }
        )
    bands = pd.DataFrame(band_rows)
    bands.to_csv(TABLES / "access_time_bands.csv", index=False)

    # County roll-up, population weighted.
    out["_pw_time"] = out["net_min"] * out["POP20"]
    out["_pw_dist"] = out["net_km"] * out["POP20"]
    cty = (
        out[out["reachable"]]
        .groupby("COUNTYFP")
        .agg(
            population=("POP20", "sum"),
            n_blocks=("GEOID20", "size"),
            pw_time_min=("_pw_time", "sum"),
            pw_dist_km=("_pw_dist", "sum"),
            max_time_min=("net_min", "max"),
            max_dist_km=("net_km", "max"),
        )
    )
    cty["mean_time_min"] = cty["pw_time_min"] / cty["population"].replace(0, np.nan)
    cty["mean_dist_km"] = cty["pw_dist_km"] / cty["population"].replace(0, np.nan)
    cty = cty.drop(columns=["pw_time_min", "pw_dist_km"])

    counties = gpd.read_parquet(PROC / "counties.parquet")
    cty = cty.join(counties.set_index("COUNTYFP5")["COUNTY_NAME"])
    fac_counties = set(fac["COUNTYFIPS"].astype(str))
    cty["has_facility"] = cty.index.isin(fac_counties)
    cty.sort_values("mean_time_min", ascending=False).to_csv(TABLES / "county_access.csv")

    deserts = cty[~cty["has_facility"]].sort_values("mean_time_min", ascending=False)
    deserts.to_csv(TABLES / "desert_counties.csv")

    # ------------------------------------------------------------- reporting
    print("\n" + "=" * 68)
    print("STATEWIDE ACCESS TO THE NEAREST OBSTETRIC FACILITY")
    print(f"(population-weighted, {total_pop:,} people, {len(fac)} facilities)")
    print("=" * 68)
    print(summary.to_string(index=False, float_format=lambda x: f"{x:8.2f}"))

    print("\n--- Population by drive-time band ---")
    print(bands.to_string(index=False))

    over30 = int(w[tmin > 30].sum())
    over60 = int(w[tmin > 60].sum())
    print(f"\n  > 30 min from care : {over30:>10,}  ({100 * over30 / total_pop:5.2f}%)")
    print(f"  > 60 min from care : {over60:>10,}  ({100 * over60 / total_pop:5.2f}%)")
    print(f"\n  counties with no facility : {int((~cty['has_facility']).sum())}")
    print(f"  population in those counties: "
          f"{int(cty.loc[~cty['has_facility'], 'population'].sum()):,}")

    print("\n--- 10 worst counties by mean drive time ---")
    print(
        cty.sort_values("mean_time_min", ascending=False)
        .head(10)[["COUNTY_NAME", "population", "mean_time_min", "max_time_min", "has_facility"]]
        .to_string(float_format=lambda x: f"{x:.1f}")
    )

    meta = {
        "facility_layer": args.facilities,
        "n_facilities": int(len(fac)),
        "n_blocks": int(len(out)),
        "n_blocks_unreachable": int(unreach.sum()),
        "population_total": total_pop,
        "pop_over_30min": over30,
        "pop_over_60min": over60,
        "snap_speed_mph": SNAP_SPEED_MPH,
        "median_block_snap_m": float(np.median(snap_m)),
    }
    (PROC / "access_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
