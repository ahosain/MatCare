# MatCare — Spatial Access to Obstetric Care in Texas

Measures road-network driving distance and driving time from **every** Texas
census block to the nearest obstetric facility, and maps the resulting maternity
care deserts.

> **⚠️ Read [`docs/FINDINGS.md`](docs/FINDINGS.md) § 1 before citing any number.**
> The facility list is materially incomplete — 77 counties holding 3.28 M people
> are flagged as deserts despite having hospitals mapped in OpenStreetMap. All
> travel statistics are currently **upper bounds**.

---

## Results at a glance

Population-weighted, 28.9 M people, 668,757 blocks, 160 facilities:

| | Mean | Median | p90 | p99 | Max |
|---|---|---|---|---|---|
| Drive time (min) | 12.9 | 8.7 | 28.4 | 63.7 | 173.4 |
| Road distance (km) | 18.5 | 11.2 | 43.6 | 106.8 | 221.2 |

- **2,581,427 Texans (8.9%)** are more than 30 min from obstetric care
- **386,386 (1.3%)** are more than 60 min away
- **153 of 254 counties** have no facility in the analysis set
- Road detour ratio **1.33×** — straight-line analysis understates travel by ~⅓

---

## Pipeline

Numbered, idempotent, runs end to end from raw downloads. No Docker, no routing
server, no API keys.

```bash
conda activate matcare

python scripts/00_download_data.py       # ~1.25 GB, resumable, hashes everything
python scripts/01_prepare_facilities.py  # clean + flag the facility list
python scripts/02_prepare_census.py      # ~50 s   blocks / BGs / tracts / counties
python scripts/03_build_road_network.py  # ~2.5 min  11.0 M nodes, 21.6 M edges
python scripts/04_compute_access.py      # ~30 s   multi-source Dijkstra
python scripts/05_validate_facilities.py # cross-check against OSM hospitals
python scripts/06_make_figures.py        # all figures, PNG + PDF
```

### How the routing works

Naively this is 668,757 blocks × 160 facilities ≈ **107 million** shortest
paths. But only the *nearest* facility matters, so the pipeline runs a single
**multi-source Dijkstra** — seeded with all 160 facility nodes at once, on the
**transposed** directed graph so costs measure patient → facility rather than
the reverse.

Every node in Texas gets its distance, its travel time, and the identity of its
nearest facility in **~3 seconds per pass**. Every block is computed exactly;
nothing is sampled.

---

## Layout

```
scripts/     00–06, the reproducible pipeline
docs/        DATA_SOURCES.md · METHODS.md · FINDINGS.md
results/
  figures/   fig1–fig5, each as 300 dpi PNG + 600 dpi PDF
  tables/    access summaries, county roll-ups, validation output
data/        raw/ + processed/ (git-ignored; rebuild with the scripts)
*.ipynb      original exploratory notebooks
```

### Documentation

| File | Contents |
|---|---|
| [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) | Every dataset, URL, licence, checksum, and what was verified |
| [`docs/METHODS.md`](docs/METHODS.md) | Algorithm, projections, **every assumption**, limitations |
| [`docs/FINDINGS.md`](docs/FINDINGS.md) | Results, data-quality problems, recommendations |

---

## Figures

| Figure | Content |
|---|---|
| `fig1_maternity_care_deserts` | Counties with no obstetric facility + facilities, county names, cities, legend |
| `fig2_drive_time` | Block-group choropleth of drive time |
| `fig3_drive_distance` | Block-group choropleth of road distance |
| `fig4_population_by_drivetime` | Population by drive-time band |
| `fig5_facility_validation` | Deserts that nonetheless have OSM-mapped hospitals |

Figures 2 and 3 are shaded by **area**; rural block groups are enormous and
urban ones tiny, so the maps look far darker than the population statistics.
Always pair them with the population table.

---

## Data sources

- **US Census Bureau** — TIGER/Line 2024, 2020 Decennial population, 2020
  Centers of Population. Public domain.
- **OpenStreetMap** via Geofabrik — road network. **ODbL 1.0**, MD5-verified
  against the publisher's checksum.
- **HIFLD × Texas HHS CMOS** — facility list (provenance incomplete, see
  `DATA_SOURCES.md` § 3).

> Maps derived from the road network must credit **© OpenStreetMap contributors
> (ODbL)**. This is a licence obligation, not a courtesy.

---

## Environment

Conda env `matcare` (Python 3.12): geopandas 1.1, shapely 2.1, scipy 1.16,
pyosmium 4.3, pyproj 3.6, matplotlib 3.10.

Built and run on 8 cores / 16 GB RAM; peak memory ~2.2 GB.
