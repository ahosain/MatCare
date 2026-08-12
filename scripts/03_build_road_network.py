#!/usr/bin/env python
"""
03_build_road_network.py
========================
Build a routable, directed drive network for Texas from the raw OSM extract.

Input : data/street_network/raw/texas-latest.osm.pbf   (Geofabrik, MD5-verified in step 00)
Output: data/street_network/processed/network_nodes.parquet   node id -> lon/lat
        data/street_network/processed/network_edges.parquet   u, v, length_m, time_s, highway
        data/street_network/processed/network_meta.json       build parameters + counts

Method
------
1. Stream the PBF once with pyosmium, keeping only ways whose ``highway`` tag is
   in ``DRIVE_TAGS`` and that are not access-restricted to motor vehicles.
2. Emit one directed edge per consecutive node pair, honouring ``oneway``.
3. Edge length is the **geodesic** distance on the WGS84 ellipsoid
   (``pyproj.Geod``), computed vectorised - not a projected approximation, so
   it stays accurate across Texas's ~13 degrees of longitude.
4. Edge traversal time uses the posted ``maxspeed`` where OSM has one, and a
   documented free-flow default by road class otherwise.

Important modelling assumptions (see docs/METHODS.md)
-----------------------------------------------------
* ``service`` roads (driveways, parking aisles) are EXCLUDED, matching the
  convention of OSMnx's "drive" network. This means a route ends at the public
  road nearest the hospital, not at its front door - an error of tens of metres,
  negligible against drive distances measured in tens of kilometres.
* Default speeds are **free-flow** estimates. They contain no congestion, no
  traffic-signal delay and no turn restrictions, so reported travel times are
  best-case. This biases every unit in the same direction and is stated as a
  limitation rather than silently corrected.
* Ferries are excluded; Texas has very few and none are material to obstetric
  access.

Usage
-----
    python scripts/03_build_road_network.py
    python scripts/03_build_road_network.py --include-service
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import numpy as np
import osmium
import pandas as pd
from pyproj import Geod

import paths as P

PROJECT_ROOT = P.PROJECT_ROOT
PBF = P.NETWORK_RAW / "texas-latest.osm.pbf"
PROC = P.NETWORK_PROC

# --------------------------------------------------------------------------
# Network definition
# --------------------------------------------------------------------------
# Public, drivable road classes. Mirrors the OSMnx "drive" filter.
DRIVE_TAGS = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "motorway_link",
    "trunk_link",
    "primary_link",
    "secondary_link",
    "tertiary_link",
    "road",
}

# Free-flow speed assumptions in mph, applied only when OSM has no `maxspeed`.
# Chosen to reflect typical Texas posted limits by road class; TxDOT's statutory
# maximum is 75 mph on rural highways (85 on one tolled segment), 30 mph in
# urban districts absent other posting. These are ASSUMPTIONS, not measurements.
DEFAULT_SPEED_MPH = {
    "motorway": 70,
    "trunk": 65,
    "primary": 60,
    "secondary": 55,
    "tertiary": 45,
    "unclassified": 40,
    "residential": 30,
    "living_street": 15,
    "motorway_link": 45,
    "trunk_link": 40,
    "primary_link": 35,
    "secondary_link": 35,
    "tertiary_link": 30,
    "road": 35,
    "service": 15,
}

# Values of access / motor_vehicle that make a way unusable by the public.
BLOCKED_ACCESS = {"no", "private", "customers", "delivery", "agricultural", "forestry"}

MPH_TO_MS = 0.44704
KPH_TO_MS = 1 / 3.6

_SPEED_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(mph|km/h|kph|knots)?\s*$", re.I)


def parse_maxspeed(value: str | None) -> float | None:
    """
    Convert an OSM ``maxspeed`` tag to metres per second.

    Handles the common forms: "60", "60 mph", "100 km/h". Returns None for
    anything non-numeric ("none", "signals", "walk", country codes such as
    "US:urban"), so the caller falls back to a class default.
    """
    if not value:
        return None
    m = _SPEED_RE.match(value)
    if not m:
        return None
    num = float(m.group(1))
    if num <= 0:
        return None
    unit = (m.group(2) or "").lower()
    if unit == "mph":
        return num * MPH_TO_MS
    if unit in ("km/h", "kph", ""):
        # A bare number in OSM means km/h by specification.
        return num * KPH_TO_MS
    return None  # knots - not a road speed


def is_oneway(tags) -> tuple[bool, bool]:
    """Return (forward_allowed, backward_allowed)."""
    ow = (tags.get("oneway") or "").strip().lower()
    if ow in ("yes", "true", "1"):
        return True, False
    if ow in ("-1", "reverse"):
        return False, True
    if ow in ("no", "false", "0"):
        return True, True
    # Roundabouts are one-way by definition unless explicitly tagged otherwise.
    if (tags.get("junction") or "").lower() in ("roundabout", "circular"):
        return True, False
    return True, True


def blocked(tags) -> bool:
    """True when the way is not usable by a private motor vehicle."""
    # An explicit motor-vehicle permission overrides a general access ban.
    for key in ("motorcar", "motor_vehicle"):
        v = (tags.get(key) or "").strip().lower()
        if v in BLOCKED_ACCESS:
            return True
        if v in ("yes", "designated", "permissive"):
            return False
    if (tags.get("access") or "").strip().lower() in BLOCKED_ACCESS:
        return True
    if (tags.get("area") or "").strip().lower() == "yes":
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--include-service",
        action="store_true",
        help="also traverse service roads (driveways, parking aisles)",
    )
    args = ap.parse_args()

    PROC.mkdir(parents=True, exist_ok=True)
    keep_tags = DRIVE_TAGS | ({"service"} if args.include_service else set())

    print(f"Reading {PBF.name} ...")
    print(f"  road classes kept: {len(keep_tags)}")

    # -- pass: stream ways, collect directed edge list ----------------------
    fp = (
        osmium.FileProcessor(PBF, osmium.osm.NODE | osmium.osm.WAY)
        .with_locations("flex_mem")
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
        .with_filter(osmium.filter.KeyFilter("highway"))
    )

    u_list: list[np.ndarray] = []
    v_list: list[np.ndarray] = []
    spd_list: list[np.ndarray] = []
    cls_list: list[np.ndarray] = []

    node_ids: list[np.ndarray] = []
    node_lon: list[np.ndarray] = []
    node_lat: list[np.ndarray] = []

    classes: list[str] = sorted(keep_tags)
    class_idx = {c: i for i, c in enumerate(classes)}

    t0 = time.time()
    n_seen = n_kept = n_blocked = n_maxspeed = 0

    for w in fp:
        n_seen += 1
        hw = w.tags.get("highway")
        if hw not in keep_tags:
            continue
        if blocked(w.tags):
            n_blocked += 1
            continue

        # Collect the way's node ids and coordinates.
        try:
            ids = np.fromiter((n.ref for n in w.nodes), dtype=np.int64)
            lons = np.fromiter((n.location.lon for n in w.nodes), dtype=np.float64)
            lats = np.fromiter((n.location.lat for n in w.nodes), dtype=np.float64)
        except osmium.InvalidLocationError:
            # A node referenced by the way is outside the extract's bounds.
            continue
        if ids.size < 2:
            continue

        n_kept += 1
        node_ids.append(ids)
        node_lon.append(lons)
        node_lat.append(lats)

        speed = parse_maxspeed(w.tags.get("maxspeed"))
        if speed is not None:
            n_maxspeed += 1
        else:
            speed = DEFAULT_SPEED_MPH[hw] * MPH_TO_MS

        fwd, bwd = is_oneway(w.tags)
        a, b = ids[:-1], ids[1:]
        ci = class_idx[hw]
        if fwd:
            u_list.append(a)
            v_list.append(b)
            spd_list.append(np.full(a.size, speed))
            cls_list.append(np.full(a.size, ci, dtype=np.int8))
        if bwd:
            u_list.append(b)
            v_list.append(a)
            spd_list.append(np.full(a.size, speed))
            cls_list.append(np.full(a.size, ci, dtype=np.int8))

        if n_kept % 250_000 == 0:
            print(f"  {n_kept:,} drivable ways ({time.time() - t0:.0f}s)", flush=True)

    print(
        f"  scanned {n_seen:,} highway ways -> kept {n_kept:,} "
        f"({n_blocked:,} access-blocked) in {time.time() - t0:.0f}s"
    )
    if n_kept == 0:
        raise SystemExit("No drivable ways found - check the PBF and filters.")

    # -- build the node table ------------------------------------------------
    print("Building node table ...")
    all_ids = np.concatenate(node_ids)
    all_lon = np.concatenate(node_lon)
    all_lat = np.concatenate(node_lat)
    del node_ids, node_lon, node_lat

    osm_ids, first = np.unique(all_ids, return_index=True)
    nodes = pd.DataFrame(
        {"osm_id": osm_ids, "lon": all_lon[first], "lat": all_lat[first]}
    )
    del all_ids, all_lon, all_lat
    print(f"  {len(nodes):,} unique nodes")

    # -- map OSM ids to contiguous indices -----------------------------------
    print("Building edge table ...")
    u_osm = np.concatenate(u_list)
    v_osm = np.concatenate(v_list)
    speeds = np.concatenate(spd_list)
    edge_cls = np.concatenate(cls_list)
    del u_list, v_list, spd_list, cls_list

    u = np.searchsorted(osm_ids, u_osm).astype(np.int32)
    v = np.searchsorted(osm_ids, v_osm).astype(np.int32)
    del u_osm, v_osm

    # -- geodesic edge lengths ------------------------------------------------
    print("Computing geodesic edge lengths ...")
    geod = Geod(ellps="WGS84")
    lon = nodes["lon"].to_numpy()
    lat = nodes["lat"].to_numpy()
    _, _, length_m = geod.inv(lon[u], lat[u], lon[v], lat[v])
    length_m = np.abs(length_m)

    # Degenerate edges (duplicate coordinates) would create zero-cost shortcuts.
    zero = length_m <= 0
    if zero.any():
        print(f"  [info] {int(zero.sum()):,} zero-length edge(s) clamped to 0.1 m")
        length_m[zero] = 0.1

    time_s = length_m / speeds

    edges = pd.DataFrame(
        {
            "u": u,
            "v": v,
            "length_m": length_m.astype(np.float32),
            "time_s": time_s.astype(np.float32),
            "highway": pd.Categorical.from_codes(edge_cls, categories=classes),
        }
    )

    # -- write ----------------------------------------------------------------
    nodes.to_parquet(PROC / "network_nodes.parquet", index=False)
    edges.to_parquet(PROC / "network_edges.parquet", index=False)

    meta = {
        "source_pbf": PBF.name,
        "include_service": args.include_service,
        "road_classes": sorted(keep_tags),
        "default_speed_mph": DEFAULT_SPEED_MPH,
        "n_nodes": int(len(nodes)),
        "n_directed_edges": int(len(edges)),
        "n_ways_kept": int(n_kept),
        "n_ways_access_blocked": int(n_blocked),
        "n_ways_with_maxspeed_tag": int(n_maxspeed),
        "pct_ways_with_maxspeed": round(100 * n_maxspeed / n_kept, 2),
        "total_network_km": round(float(length_m.sum()) / 1000, 1),
        "build_seconds": round(time.time() - t0, 1),
    }
    (PROC / "network_meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    print("\n--- network summary ---")
    print(f"  nodes            : {meta['n_nodes']:,}")
    print(f"  directed edges   : {meta['n_directed_edges']:,}")
    print(f"  network length   : {meta['total_network_km']:,.0f} km (directed sum)")
    print(f"  maxspeed tagged  : {meta['pct_ways_with_maxspeed']}% of kept ways")
    print(f"  elapsed          : {meta['build_seconds']}s")
    print("\n--- edges by road class ---")
    print(
        edges.groupby("highway", observed=True)["length_m"]
        .agg(n="size", km=lambda s: s.sum() / 1000)
        .sort_values("km", ascending=False)
        .to_string(float_format=lambda x: f"{x:,.0f}")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
