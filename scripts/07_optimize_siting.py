#!/usr/bin/env python
"""
07_optimize_siting.py
=====================
Where should the next obstetric facility go?

Solves the **Maximal Covering Location Problem (MCLP)** greedily on the real
road network: given the 211 existing facilities, choose K new sites that bring
the largest number of currently-underserved Texans within a 30-minute drive.

This is the *baseline* the proposed ILP and reinforcement-learning methods must
beat. Shipping it as a preliminary result proves the pipeline can already
produce actionable siting recommendations, and gives the funder a concrete
artefact rather than a promise.

Method
------
1. **Demand.** Every populated census block whose drive time to the nearest
   existing facility exceeds the coverage threshold (default 30 min).
2. **Candidate sites.** TIGER incorporated places and CDPs that are themselves
   currently underserved. Restricting candidates to existing settlements keeps
   recommendations actionable - a hospital needs staff, utilities and road
   access, so an optimum in an empty rangeland is not a real answer.
3. **Coverage.** For each candidate, one Dijkstra bounded at the coverage
   threshold on the transposed graph gives every block that could reach it in
   time. The bound prunes the search hard, which is what makes hundreds of
   candidate evaluations cheap.
4. **Greedy selection.** Repeatedly take the candidate covering the most
   still-uncovered population. Greedy is the standard MCLP heuristic and
   carries a (1 - 1/e) ~ 63% approximation guarantee because coverage is a
   monotone submodular function - so the result is not merely a guess, it is
   provably within a known factor of optimal.

Outputs
-------
results/tables/siting_recommendations.csv  ranked new sites + population gained
results/tables/siting_marginal_gain.csv    coverage curve vs number of sites
data/facilities/processed/siting.parquet   geometry for mapping

Usage
-----
    python scripts/07_optimize_siting.py
    python scripts/07_optimize_siting.py --threshold-min 60 --n-sites 15
"""

from __future__ import annotations

import argparse
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


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threshold-min", type=float, default=30.0,
                    help="coverage standard in minutes (default 30)")
    ap.add_argument("--n-sites", type=int, default=10,
                    help="how many new facilities to site")
    ap.add_argument("--min-candidate-min", type=float, default=20.0,
                    help="only consider places at least this far from care")
    args = ap.parse_args()

    P.ensure_tree()
    limit_s = args.threshold_min * 60.0

    # ------------------------------------------------------------------ load
    log("Loading network and access results ...")
    nodes = pd.read_parquet(P.NETWORK_PROC / "network_nodes.parquet")
    edges = pd.read_parquet(P.NETWORK_PROC / "network_edges.parquet",
                            columns=["u", "v", "time_s"])
    n_nodes = len(nodes)

    acc = pd.read_parquet(P.FACILITIES_PROC / "block_access.parquet")
    blocks = gpd.read_parquet(P.POPULATION_PROC / "block_points.parquet").to_crs(P.TX_ALBERS)
    counties = gpd.read_parquet(P.BOUNDARIES_PROC / "counties.parquet").to_crs(P.TX_ALBERS)

    tf = Transformer.from_crs(P.WGS84, P.TX_ALBERS, always_xy=True)
    nx_, ny_ = tf.transform(nodes["lon"].to_numpy(), nodes["lat"].to_numpy())
    node_xy = np.column_stack([nx_, ny_])
    tree = cKDTree(node_xy)

    # Transposed, time-weighted graph: cost(node -> site).
    gt = csr_matrix(
        (edges["time_s"].to_numpy(np.float64),
         (edges["u"].to_numpy(), edges["v"].to_numpy())),
        shape=(n_nodes, n_nodes),
    ).T.tocsr()
    del edges

    # ---------------------------------------------------------------- demand
    b_xy = np.column_stack([blocks.geometry.x.to_numpy(), blocks.geometry.y.to_numpy()])
    _, b_node = tree.query(b_xy, k=1)

    under = (
        (acc["net_min"] > args.threshold_min)
        & (acc["POP20"] > 0)
        & np.isfinite(acc["net_min"])
    ).to_numpy()
    d_node = b_node[under]
    d_pop = acc.loc[under, "POP20"].to_numpy().astype(np.int64)
    total_under = int(d_pop.sum())
    log(f"  underserved: {under.sum():,} blocks, {total_under:,} people "
        f"beyond {args.threshold_min:.0f} min")

    # ------------------------------------------------------------ candidates
    places = gpd.read_parquet(P.BOUNDARIES_PROC / "places.parquet").to_crs(P.TX_ALBERS)
    p_pt = places.geometry.representative_point()
    p_xy = np.column_stack([p_pt.x.to_numpy(), p_pt.y.to_numpy()])
    _, p_node = tree.query(p_xy, k=1)

    # A place's own drive time = that of its nearest block centroid.
    blk_tree = cKDTree(b_xy)
    _, nearest_blk = blk_tree.query(p_xy, k=1)
    p_time = acc["net_min"].to_numpy()[nearest_blk]

    keep = np.isfinite(p_time) & (p_time >= args.min_candidate_min)
    cand_idx = np.flatnonzero(keep)
    cand_node = p_node[keep]
    log(f"  candidate sites: {len(cand_idx):,} places at least "
        f"{args.min_candidate_min:.0f} min from existing care")

    # ------------------------------------------- coverage set per candidate
    log("Computing bounded coverage per candidate ...")
    t0 = time.time()
    cover: list[np.ndarray] = []
    for k, node in enumerate(cand_node):
        d = dijkstra(gt, directed=True, indices=int(node), limit=limit_s)
        cover.append(np.flatnonzero(d[d_node] <= limit_s))
        if (k + 1) % 100 == 0:
            log(f"  {k + 1:,}/{len(cand_node):,} ({time.time() - t0:.0f}s)")
    log(f"  done in {time.time() - t0:.0f}s")

    # ------------------------------------------------------ greedy selection
    log("Greedy MCLP selection ...")
    covered = np.zeros(len(d_pop), dtype=bool)
    chosen: list[dict] = []
    curve: list[dict] = []

    for step in range(args.n_sites):
        best_k, best_gain, best_new = -1, 0, None
        for k, idx in enumerate(cover):
            if idx.size == 0:
                continue
            new = idx[~covered[idx]]
            gain = int(d_pop[new].sum())
            if gain > best_gain:
                best_k, best_gain, best_new = k, gain, new
        if best_k < 0 or best_gain == 0:
            log(f"  no further gain after {step} sites")
            break

        covered[best_new] = True
        row = places.iloc[cand_idx[best_k]]
        chosen.append(
            {
                "rank": step + 1,
                "place": row["PLACE_NAME"],
                "place_geoid": row["GEOID"],
                "pop_newly_covered": best_gain,
                "cum_pop_covered": int(d_pop[covered].sum()),
                "pct_of_underserved": round(100 * d_pop[covered].sum() / total_under, 2),
                "geometry": p_pt.iloc[cand_idx[best_k]],
            }
        )
        curve.append({"n_sites": step + 1,
                      "cum_pop_covered": int(d_pop[covered].sum()),
                      "pct_of_underserved": round(100 * d_pop[covered].sum() / total_under, 2)})
        log(f"  #{step + 1}: {row['PLACE_NAME']:28s} +{best_gain:>8,} "
            f"(cum {100 * d_pop[covered].sum() / total_under:5.1f}%)")
        cover[best_k] = np.array([], dtype=int)  # cannot be chosen twice

    # ---------------------------------------------------------------- output
    sit = gpd.GeoDataFrame(chosen, crs=P.TX_ALBERS)
    sit = gpd.sjoin(sit, counties[["COUNTY_NAME", "geometry"]], how="left", predicate="within")
    sit = sit.drop(columns=[c for c in ("index_right",) if c in sit.columns])
    sit.to_parquet(P.FACILITIES_PROC / "siting.parquet", index=False)
    sit.drop(columns="geometry").to_csv(TABLES / "siting_recommendations.csv", index=False)
    pd.DataFrame(curve).to_csv(TABLES / "siting_marginal_gain.csv", index=False)

    print("\n" + "=" * 72)
    print(f"OPTIMAL SITING - {len(sit)} NEW FACILITIES, {args.threshold_min:.0f}-MIN STANDARD")
    print("=" * 72)
    print(sit.drop(columns="geometry").to_string(index=False))
    if len(curve):
        print(f"\n  {total_under:,} Texans currently beyond {args.threshold_min:.0f} min")
        print(f"  {curve[-1]['cum_pop_covered']:,} ({curve[-1]['pct_of_underserved']}%) "
              f"brought within {args.threshold_min:.0f} min by {len(sit)} new facilities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
