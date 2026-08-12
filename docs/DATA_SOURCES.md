# Data Sources

Every dataset used by this project, where it came from, and how to verify it.
All URLs were confirmed to return HTTP 200 on **2026-08-11**. Retrieval
timestamps, byte counts and SHA-256 hashes for the copy actually used are
recorded in `data/_manifest.json`, written by `scripts/00_download_data.py`.

Raw files live under `data/<theme>/raw/` and derived files under
`data/<theme>/processed/`, where theme is one of `boundaries`, `population`,
`centroids`, `street_network`, `facilities`. Paths come from `scripts/paths.py`.

Nothing in this file is inferred. If a fact is not verifiable from the source
listed, it is not stated.

---

## 1. Census geography and population

All Census products are US federal government works and are in the **public
domain** (Title 17 U.S.C. § 105). No licence restriction, attribution
appreciated.

| Key | File | Size | What it provides |
|---|---|---|---|
| `blocks` | `tl_2024_48_tabblock20.zip` | 436 MB | 2020 tabulation blocks, Texas |
| `zcta_gazetteer` | `2024_Gaz_zcta_national.zip` | 1 MB | ZCTA centroids, geocoding fallback |
| `block_groups` | `tl_2024_48_bg.zip` | 50 MB | Block group polygons |
| `tracts` | `tl_2024_48_tract.zip` | 33 MB | Census tract polygons |
| `places` | `tl_2024_48_place.zip` | 9.7 MB | Incorporated places / CDPs |
| `counties_cb` | `cb_2023_us_county_500k.zip` | 12 MB | Cartographic county boundaries |

Base URL: `https://www2.census.gov/geo/tiger/TIGER2024/…`
Cartographic boundaries: `https://www2.census.gov/geo/tiger/GENZ2023/shp/…`

**Why the 2024 vintage of 2020 blocks?** TIGER/Line republishes the 2020
tabulation blocks annually with corrected geometry. The 2024 vintage carries the
`POP20` and `HOUSING20` fields directly, so block population needs no separate
join and no API key.

**Verified on load** (`scripts/02_prepare_census.py`):

```
blocks       668,757   total 2020 population 29,145,505
block groups  18,638   total 2020 population 29,145,505
tracts         6,896   total 2020 population 29,145,505
counties         254
```

All three independently-sourced totals agree exactly, and 29,145,505 is the
published 2020 Census resident population of Texas. 254 is the correct number of
Texas counties. These are the integrity checks for this layer.

### Population-weighted centroids ("Centers of Population")

| File | Level |
|---|---|
| `CenPop2020_Mean_BG48.txt` | Block group, Texas |
| `CenPop2020_Mean_TR48.txt` | Tract, Texas |
| `CenPop2020_Mean_CO.txt` | County, national |

Base URL: `https://www2.census.gov/geo/docs/reference/cenpop2020/`

**Important limitation, verified not assumed.** That directory contains exactly
three subdirectories — `county/`, `tract/`, `blkgrp/`. The Census Bureau does
**not** publish population-weighted centroids at block level. Blocks therefore
use the TIGER internal point (`INTPTLAT20` / `INTPTLON20`), which the Bureau
guarantees falls inside the polygon. See `docs/METHODS.md` for why this is
acceptable at block scale.

---

## 2. Road network — OpenStreetMap

| Field | Value |
|---|---|
| Source | Geofabrik GmbH regional extract |
| URL | `https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf` |
| Size | 713,707,612 bytes |
| Publisher MD5 | `20dcac4956607e953d721700c048bc99` |
| **Verification** | **Local MD5 recomputed and matched exactly** |
| Licence | **ODbL 1.0** — © OpenStreetMap contributors |

Geofabrik rebuilds this file nightly, so re-running `00_download_data.py` on a
later date yields a different, equally valid extract. The manifest records the
hash of the copy used, which is what makes a specific run reproducible.

### Licensing obligation

ODbL is share-alike. Any published map or derived database built on this extract
**must** carry the attribution "© OpenStreetMap contributors" and note the ODbL.
This matters for the proposal figures and any paper. Census data carries no such
obligation; OSM does.

---

## 3. Facility data

Both upstream files are now present in `data/facilities/raw/`, and the facility
list is fully reproducible from them via `scripts/01_prepare_facilities.py`.

### Correction: "CMOS" is the CMS Provider of Services file

An earlier revision of this document guessed that "CMOS" meant the Texas HHS
*Certificate of Maternal and Obstetric Services*. **That was wrong.** Inspecting
`CMOS Dataset Description.pdf` (733 pages, record layout dated 2023-04-02) shows
the files are an extract of the **CMS Provider of Services (POS) current file**,
hospital category. The column names are CMS POS names — `PRVDR_NUM`,
`PGM_TRMNTN_CD`, `OB_SRVC_CD`, `NEONTL_ICU_SRVC_CD`.

| File | Rows | Contents |
|---|---|---|
| `CMOS Data.csv` | 77,522 | CMS POS, all US providers |
| `Cleaned_CMOS_Data.csv` | 4,907 | filtered to hospitals reporting obstetric service; 377 in Texas |
| `Cleaned_texas_hospitals_HIFLD.csv` | 876 | HIFLD Texas hospitals, with coordinates |
| `CMOS Dataset Description.pdf` | — | official CMS POS record layout and code definitions |
| `texas_obs_facilities_final.csv` | 170 | **superseded** output of the old exact-name join |

### Code definitions (quoted from the record layout, not inferred)

```
OB_SRVC_CD      0=NOT PROVIDED  1=PROVIDED BY STAFF
                2=PROVIDED UNDER ARRANGEMENT
                3=PROVIDED BY STAFF AND UNDER ARRANGEMENT

PGM_TRMNTN_CD   00=ACTIVE PROVIDER  01=VOLUNTARY-MERGER, CLOSURE
                02..07=other terminations
```

`NEONTL_ICU_SRVC_CD` and `NEONTL_NRSRY_SRVC_CD` use the same 0–3 scale and give
the NICU-capability proxy used for level-of-care analysis.

### Why the two sources must be matched

CMS POS is authoritative for *which* hospitals provide obstetrics but carries no
coordinates. HIFLD is authoritative for *where* hospitals are but does not record
obstetric service. Neither carries the other's identifier, so they are joined by
name, address and geography — see `docs/METHODS.md` § 2 and `docs/FINDINGS.md`
§ 1. The rebuilt matcher achieves 98.1% coverage; the previous exact-name join
achieved 64%.

### HIFLD re-download

`MCD.ipynb` cites `https://source.coop/seerai/hifld/hospitals/hospitals/hospitals.parquet`.
That host now returns an HTML page rather than the Parquet file for
unauthenticated requests, so HIFLD cannot currently be refreshed automatically.
The supplied `Cleaned_texas_hospitals_HIFLD.csv` is used instead, and its vintage
should be confirmed before publication — four hospitals in the CMS list postdate
it.

---

## 4. Datasets deliberately NOT used

**Census ACS via API.** `api.census.gov` now rejects unauthenticated requests
with `Missing Key`, verified 2026-08-11. ACS would supply women aged 15–44
(table `B01001`, variables `B01001_030E`–`B01001_039E`) — the correct denominator
for obstetric access, far better than total population. A free key from
<https://api.census.gov/data/key_signup.html> is issued instantly. The pipeline
uses total 2020 population until a key is available.

**Ferry routes.** Excluded from the road network. This affects Bolivar Peninsula
in Galveston County, whose only direct link to Galveston is the Texas
DOT ferry. Those blocks are reported as unreachable rather than being given a
fabricated crossing time.

---

## Reproducing the raw data

```bash
conda activate matcare
python scripts/00_download_data.py          # ~1.25 GB, resumable
python scripts/00_download_data.py --verify # re-hash local copies
python scripts/00_download_data.py --list   # show catalogue
```
