# Methods

How spatial access to obstetric care is measured in this project, and every
assumption that goes into the numbers.

---

## 1. The question

For every populated place in Texas: **how far, by road, is the nearest facility
providing obstetric care — in kilometres and in minutes?**

Note what this does *not* measure. Proximity is not access. Capacity, insurance
acceptance, staffing, transport availability and whether a unit is currently
open all matter and none are modelled here. This is a geographic-accessibility
analysis, and should be described as one.

---

## 2. Units of analysis and their centroids

| Unit | n | Centroid used | Why |
|---|---|---|---|
| Block | 668,757 | TIGER internal point | No weighted centroid is published at this level |
| Block group | 18,638 | Census 2020 population-weighted center | Official, avoids areal bias |
| Tract | 6,896 | Census 2020 population-weighted center | Official |

**Why the mixed definition is defensible.** An *areal* centroid of a large rural
polygon can sit in empty desert far from where anyone lives, which inflates
measured travel distance. That bias is what population weighting exists to
remove. The Census Bureau publishes weighted centers only at county, tract and
block-group level (verified — see `DATA_SOURCES.md`).

Blocks are the Bureau's finest tabulation geography. In populated areas a block
is typically a single city block, so the distance between its internal point and
its true population centroid is on the order of 100 m — negligible against drive
distances whose population-weighted median is 11.2 km. The coarse units, where
the bias *would* bite, use the official weighted centers. The analysis therefore
never relies on an areal centroid of a large polygon.

Results are computed at block level and aggregated upward, population-weighted.

---

## 3. Road network

Built by `scripts/03_build_road_network.py` from the MD5-verified Geofabrik
Texas extract.

**Result:** 10,987,427 nodes, 21,590,108 directed edges, ~1.4 M km of directed
road (~700,000 km of centreline).

### Included road classes

`motorway`, `trunk`, `primary`, `secondary`, `tertiary`, `unclassified`,
`residential`, `living_street`, `road`, and the `_link` ramp variants. This
mirrors the OSMnx "drive" network filter.

`service` roads (driveways, parking aisles) are **excluded** — 1.93 M ways.
A route therefore ends at the public road nearest the hospital rather than at
its entrance. Facility snap distances are a median of 95 m and a maximum of
322 m, so this contributes error of well under a minute. Re-runnable with
`--include-service` for sensitivity testing.

Ways tagged `access`/`motor_vehicle`/`motorcar` = `no`, `private`, `customers`,
`delivery`, `agricultural` or `forestry` are dropped (32,502 ways), unless an
explicit motor-vehicle permission overrides the general ban.

### Direction of travel

The graph is **directed**. `oneway=yes|true|1` gives forward-only traversal,
`oneway=-1|reverse` backward-only, and `junction=roundabout` implies one-way
unless explicitly tagged otherwise.

### Edge length

Geodesic distance on the WGS84 ellipsoid via `pyproj.Geod`, computed vectorised
over all 21.6 M edges. Not a projected approximation — Texas spans ~13° of
longitude, where a single projection's scale distortion would be measurable.

### Speeds — **the largest assumption in this analysis**

Only **20.06%** of kept ways carry a `maxspeed` tag. Where present it is parsed
("60", "60 mph", "100 km/h"; a bare number is km/h per OSM specification). For
the remaining ~80%, a free-flow default is applied by road class:

| Class | mph | Class | mph |
|---|---|---|---|
| motorway | 70 | residential | 30 |
| trunk | 65 | living_street | 15 |
| primary | 60 | motorway_link | 45 |
| secondary | 55 | trunk_link | 40 |
| tertiary | 45 | primary/secondary_link | 35 |
| unclassified | 40 | tertiary_link | 30 |

These are **assumptions chosen to reflect typical Texas posted limits**, not
measurements, and not values taken from any cited source. They are recorded in
`data/processed/network_meta.json` so any run can be audited or re-parameterised.

Consequences, stated plainly:

* Travel times contain **no congestion, no traffic-signal delay, no turn
  restrictions**. They are best-case free-flow times.
* Real urban peak-hour travel is slower, so urban times are **underestimated**
  more than rural ones. This compresses the measured urban–rural gap, making the
  reported disparity **conservative**.
* Reported distances do not depend on speed assumptions at all, and are the more
  robust of the two metrics.

---

## 4. The routing algorithm

The naive framing — every block to every facility — is 668,757 × 160 ≈ **107
million** shortest paths. That framing is unnecessary: only the *nearest*
facility matters.

`scripts/04_compute_access.py` instead runs a **multi-source Dijkstra**. Seeding
the priority queue with all 160 facility nodes simultaneously and relaxing
outward labels every node in the network with its cost to the closest facility
and the identity of that facility, in a single pass. SciPy exposes this as
`dijkstra(..., min_only=True)`.

**Direction is handled correctly.** Multi-source Dijkstra on graph *G* yields
cost(facility → node). The quantity of interest is cost(node → facility) — a
patient travelling *to* care. On a directed network with one-way streets these
differ, so the search runs on the **transpose** *Gᵀ*.

Two passes are run: one weighted by `length_m`, one by `time_s`.

**Cost:** ~3 seconds per pass over 11 M nodes. No routing server, no Docker, no
API rate limits, no sampling — every block is computed exactly.

### Snapping

Centroids and facilities are snapped to the nearest network node with a
`cKDTree` on EPSG:3083 coordinates. The residual straight-line "snap" distance is
added back: in metres for distance, and at an assumed **30 mph** for time.

Observed snap distances — facilities: median 95 m, max 322 m. Blocks: median
71 m, 99th percentile 3.2 km, max 37.2 km. The extreme block values are remote
West Texas blocks genuinely distant from any mapped road; they are reported
rather than hidden.

### Unreachable blocks

The network has 7,908 weakly-connected components; the largest holds 98.07% of
nodes. **9,075 blocks (236,849 people)** cannot reach any facility and are
reported as unreachable, not as an arbitrary large number.

Causes: excluded ferry links (notably Bolivar Peninsula), and small OSM
components disconnected from the main graph by mapping gaps. Population totals
include these blocks; drive-time statistics exclude them. Any county summary
containing one must exclude them before averaging or the mean becomes infinite.

---

## 5. Projection

**EPSG:3083** (NAD83 / Texas Centric Albers Equal Area) throughout — equal-area
with metre units, the appropriate choice for statewide area and distance work in
Texas. Used for KD-tree snapping, straight-line comparison and all mapping.

Edge lengths are the one exception: geodesic, as above.

---

## 6. Statistics

All summary figures are **population-weighted**. An unweighted mean across
668,757 blocks would let empty rural blocks dominate a statistic about people.

Population-weighted quantiles are computed by sorting on the value, taking the
cumulative population share, and interpolating — not by `numpy.percentile`,
which ignores weights.

---

## 7. Known limitations

1. **The facility list is incomplete.** Validation identifies 77 counties
   holding 3.28 M people flagged as deserts despite having hospitals mapped in
   OSM. Every reported travel statistic is therefore an **upper bound**; true
   access is better than these numbers show. See `FINDINGS.md` § 1. This
   dominates every other limitation listed here.
2. Free-flow speeds; no congestion (§ 3).
3. Total population is the denominator, not women of reproductive age — pending
   a Census API key.
4. Facility *capacity* and level of care are not modelled. The `OB_SRVC_CD`
   field needed for this was dropped upstream.
5. Straight-line road access only — no ferries, no air-ambulance transfer.
6. Blocks with zero population still receive a value; they carry zero weight.
7. State borders are hard boundaries: a Texarkana resident's nearest obstetric
   unit may be in Arkansas. The extract is Texas-only, so cross-border access is
   not modelled and border-county times are overestimated.

---

## 8. Reproducing

```bash
conda activate matcare
python scripts/00_download_data.py       # ~1.25 GB
python scripts/01_prepare_facilities.py
python scripts/02_prepare_census.py      # ~50 s
python scripts/03_build_road_network.py  # ~2.5 min, peak RSS ~2.2 GB
python scripts/04_compute_access.py      # ~30 s
python scripts/05_validate_facilities.py
python scripts/06_make_figures.py
```

Runs on 8 cores / 16 GB RAM. No Docker, no routing server, no API keys.
