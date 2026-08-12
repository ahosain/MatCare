# MatCare — Spatial Access to Obstetric Care in Texas

Measures road-network driving distance and driving time from **every one of the
668,757 Texas census blocks** to the nearest hospital that actually provides
obstetric care, identifies maternity care deserts, and optimizes where the next
facility should go.

Supports a Discovery Foundation grant proposal — see
[`proposal/Revised_Proposal_2026.md`](proposal/Revised_Proposal_2026.md).

---

## Headline results

Population-weighted over 28.9 M Texans, 211 facilities:

| | Mean | Median | p90 | p99 | Max |
|---|---|---|---|---|---|
| Drive time (min) | 9.5 | 7.1 | 19.5 | 41.5 | 173.4 |
| Road distance (km) | 13.0 | 8.8 | 28.2 | 65.4 | 221.2 |

- **1,059,501 Texans (3.7%)** are more than 30 min from obstetric care
- **3,204,055 (11.1%)** are more than 30 min from **NICU-capable** care
- **151 of 254 counties** have no obstetric facility
- **78 counties (1.73 M residents)** have a hospital but **no obstetric unit** —
  invisible to any county-level desert map
- Road detour ratio **1.33×** — straight-line analysis understates travel by a third
- **10 optimally sited facilities** would bring **72,737** underserved women
  15–44 (40.7%) within 30 minutes

### Weighted by women aged 15–44

The population that actually uses obstetric care — 6,180,678 of them
(ACS 2019–2023, table B01001):

| Benchmark | Women 15–44 within it |
|---|---|
| 25 miles | 95.9% |
| 35 miles | 98.6% |
| 50 miles | 99.7% |
| 30 minutes | 97.0% |
| **30 minutes of a NICU-capable facility** | **90.6%** |

185,251 women 15–44 are beyond 30 min of obstetric care; **576,262 (9.4%) are
beyond 30 min of a NICU** — three times as many. That gap, not the level, is the
finding.

---

## Pipeline

Numbered, idempotent, end to end from raw downloads. No Docker, no routing
server, no API keys. Full statewide analysis runs in under ten minutes.

```bash
conda activate matcare

python scripts/00_download_data.py       # ~1.25 GB, resumable, hashes everything
python scripts/01_prepare_facilities.py  # multi-stage CMS POS <-> HIFLD match
python scripts/02_prepare_census.py      # ~50 s   blocks / BGs / tracts / counties
python scripts/03_build_road_network.py  # ~2.5 min  11.0M nodes, 21.6M edges
python scripts/04_compute_access.py      # ~30 s   multi-source Dijkstra
python scripts/05_validate_facilities.py # cross-check against OSM hospitals
python scripts/06_make_figures.py        # standard figures
python scripts/07_optimize_siting.py     # ~30 s   greedy MCLP siting
python scripts/10_prepare_acs_women.py   # ACS women 15-44 (no API key needed)
python scripts/08_proposal_figures.py    # proposal figures
python scripts/09_build_proposal_docx.py # proposal .docx
python scripts/11_build_presentation.py  # 15-slide walkthrough deck
```

### How the routing works

Naively this is 668,757 blocks × 211 facilities ≈ **141 million** shortest paths.
But only the *nearest* facility matters, so the pipeline runs a single
**multi-source Dijkstra** — all 211 facilities seeded at once, on the
**transposed** directed graph so cost measures patient → facility rather than the
reverse. Every node in Texas gets its distance, travel time and nearest facility
in **~3 seconds per pass**. Every block exact; nothing sampled.

### How siting works

The **Maximal Covering Location Problem**, solved greedily on the same network.
Because coverage is monotone submodular, greedy carries a provable
(1 − 1/e) ≈ 63% approximation guarantee.

---

## Data layout

Organized by theme; each theme carries its own `raw/` (exactly as downloaded,
never modified) and `processed/` (derived) directory. Paths come from
[`scripts/paths.py`](scripts/paths.py) — never hard-coded.

```
data/
  boundaries/      county / state / place shapefiles
  population/      census blocks, block groups, tracts + populations
  centroids/       Census population-weighted centers of population
  street_network/  OpenStreetMap extract + the routable graph
  facilities/      CMS Provider-of-Services + HIFLD hospital data
  _manifest.json   URL, bytes, SHA256 and timestamp for every raw download
```

`data/` is git-ignored; rebuild it with `scripts/00_download_data.py`.

```
scripts/     00–11 pipeline + paths.py + reorganize_data.py
docs/        DATA_SOURCES.md · METHODS.md · FINDINGS.md
results/
  figures/   standard figures + proposal figures (300 dpi PNG, 600 dpi PDF)
  tables/    access summaries, county roll-ups, siting plan, validation
  MatCare_Project_Walkthrough.pptx   15-slide walkthrough of the whole project
proposal/    revised proposal (.md source + .docx), last year's materials
```

| Doc | Contents |
|---|---|
| [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) | Every dataset, URL, licence, checksum, what was verified |
| [`docs/METHODS.md`](docs/METHODS.md) | Algorithms, projections, **every assumption**, limitations |
| [`docs/FINDINGS.md`](docs/FINDINGS.md) | Results, data-quality problems found and fixed |

---

## The facility-list correction

The project's prior facility list was built with an exact, case-sensitive string
join:

```python
matched_df = df2[df2['NAME'].isin(df1['FAC_NAME'])]
```

That matched 136 of 211 eligible hospitals (64%) and simultaneously retained
hospitals that had already closed, because it never filtered on
`PGM_TRMNTN_CD`. Both errors manufacture deserts. Jefferson County — Beaumont,
pop. 256,526 — showed **zero** obstetric facilities and a 61.6-minute mean drive
time.

The rebuilt matcher runs progressive passes (exact name + ZIP → address + ZIP →
address + city → fuzzy name, each blocked geographically) and recovers **98.1%**
automatically; the remaining 4 are placed at their ZIP centroid and flagged.
Jefferson County now correctly shows 3 facilities and 18.7 minutes.

Correcting this moved the count of Texans beyond 30 minutes from **2.58 M to
1.06 M** — a factor of 2.4.

---

## Data sources

- **US Census Bureau** — TIGER/Line 2024, 2020 Decennial population, 2020 Centers
  of Population, ACS 2019–2023 (table B01001), ZCTA Gazetteer. Public domain.
  **No API key required** — the pipeline reads bulk Summary Files, sidestepping
  `api.census.gov`, which now rejects unauthenticated requests.
- **CMS Provider of Services** file — obstetric (`OB_SRVC_CD`) and neonatal ICU
  service codes. (The repo's "CMOS" filenames refer to this CMS POS extract.)
- **HIFLD** — hospital coordinates, 876 Texas hospitals.
- **OpenStreetMap** via Geofabrik — road network. **ODbL 1.0**, MD5-verified
  against the publisher's checksum.

> Maps derived from the road network must credit **© OpenStreetMap contributors
> (ODbL)**. This is a licence obligation, not a courtesy.

---

## Environment

Conda env `matcare` (Python 3.12): geopandas 1.1, shapely 2.1, scipy 1.16,
pyosmium 4.3, rapidfuzz, pyproj 3.6, matplotlib 3.10, python-docx, python-pptx.

Built and run on 8 cores / 16 GB RAM; peak memory ~2.2 GB.
