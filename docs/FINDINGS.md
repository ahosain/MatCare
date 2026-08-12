# Findings, Observations and Recommendations

Preliminary results from the spatial-access pipeline, plus the data-quality
issues found along the way. Written for the proposal resubmission.

**Read § 1 before quoting any number in this document.**

---

## 1. ⚠️ The facility list is materially incomplete — fix this first

### What was found

The map produced by the current facility list says Jefferson County, Texas has
**no obstetric facility**. Jefferson County contains Beaumont, population
256,526. Adjacent Orange (84,808) and Hardin (56,231) are likewise flagged as
deserts. The model consequently reports a mean drive time of **61.6 minutes**
for Jefferson County and **75.6 minutes** for Orange County — the two worst
values in the entire state, in a metropolitan area of roughly 400,000 people.

That is not a finding about Texas. It is a defect in the input data.

### Independent verification

`scripts/05_validate_facilities.py` cross-checks the facility list against
hospitals tagged `amenity=hospital` in the same MD5-verified OpenStreetMap
extract used to build the road network — an independent source that was already
in hand.

| Check | Result |
|---|---|
| Texas counties | 254 |
| Counties with **0** matched obstetric facilities | 150 |
| …of which OSM maps **≥ 1 hospital** | **77** |
| Population living in those 77 counties | **3,284,130** |
| OSM hospitals statewide | 748 |
| Matched obstetric facilities | 170 (23%) |

Jefferson County alone has **11 hospitals mapped in OSM** and zero matched
facilities. Galveston County has 7. Hays has 4. Brazos has 4.

Not every hospital delivers babies, so the matched count *should* be lower than
748. The signal is not the ratio — it is populous counties with multiple mapped
hospitals and *zero* matches. Figure `fig5_facility_validation` maps them.

### Root cause

`MCD.ipynb` builds the facility list with a single line:

```python
matched_df = df2[df2['NAME'].isin(df1['FAC_NAME'])]
```

This is an **exact, case-sensitive string equality** join between the HIFLD
hospital layer and the Texas HHS CMOS obstetric-services list. It matches only
where the two agencies typed a facility's name identically, character for
character. Any divergence drops a real hospital silently:

- `ST.` vs `SAINT` vs `ST`
- `MEDICAL CENTER` vs `MED CTR` vs `MEDICAL CTR`
- corporate renaming (`CHRISTUS SOUTHEAST TEXAS ST ELIZABETH` vs `CHRISTUS ST. ELIZABETH`)
- campus suffixes, `LLC`/`INC`, trailing whitespace, `&` vs `AND`

A join like this fails *quietly*. It raises no error and produces a smaller
table that looks entirely plausible. This one produced 170 rows.

### Why it matters more than an ordinary data-cleaning issue

The error is **not random with respect to the outcome**. Every dropped facility
converts its own county — and its neighbours — into an artificial desert, and
inflates measured travel time precisely where the analysis makes its strongest
claims. A reviewer who spot-checks a single familiar county (Beaumont is an
obvious candidate) will find the error immediately, and it would discredit the
result set.

**Every travel statistic in this document is therefore an upper bound. Real
access is better than these numbers show.**

### Recommended fix

Restore the three upstream files — `Cleaned_CMOS_Data.csv`,
`Cleaned_texas_hospitals_HIFLD.csv`, `test_HIFLD.parquet` — none of which are in
the repository, and rebuild the join as a **multi-stage match**:

1. **Licence/CMS ID** where both sources carry one — exact, authoritative, and
   the only join that is actually safe.
2. **Geospatial**: HIFLD carries coordinates. Match CMOS by geocoded address
   within ~500 m. Two hospitals rarely share a parcel.
3. **Normalised name**: uppercase, strip punctuation, expand `ST→SAINT`,
   `CTR→CENTER`, drop `LLC|INC|LP`, then token-set fuzzy match (`rapidfuzz`)
   at ≥ 90, blocked within county to keep it tractable.
4. **Manual review** of every match scoring 80–90 and every CMOS row that
   remains unmatched. At this scale that is a few hundred rows — an afternoon,
   and it makes the list defensible.

Report the match rate at each stage. A reviewer will ask.

### Also recommended

- **Restore `OB_SRVC_CD`.** The notebook computed obstetric level-of-care into
  `texas_obs_fac_with_ob_srvc.csv`, but the file that survived
  (`texas_obs_facilities_final.csv`) does not contain it. Level of care is
  arguably the most valuable variable available — Level I through IV determines
  whether a facility can handle a high-risk delivery at all. Access to *any*
  facility and access to a facility that can manage a haemorrhage are different
  questions, and the second is the more fundable one.
- **Model closures over time.** Texas rural obstetric-unit closures are the
  policy story. HIFLD is a snapshot; a time series would let the proposal say
  something about trend rather than state.

---

## 2. Ten facility types in the list do not provide obstetric care

`01_prepare_facilities.py` flags these among the 170 matched rows:

| Type | n |
|---|---|
| CHILDREN | 3 |
| LONG TERM CARE | 2 |
| PSYCHIATRIC | 2 |
| SPECIAL | 2 |
| REHABILITATION | 1 |

A psychiatric or rehabilitation hospital does not staff a labour-and-delivery
unit. These are false positives from the name join, and confirm its unreliability
in the opposite direction — it both drops real facilities and admits wrong ones.

The analysis set is restricted to `GENERAL ACUTE CARE` + `CRITICAL ACCESS`
(**160 facilities**). Nothing is deleted; run with `--include-types ALL` for
sensitivity. Children's hospitals are the debatable exclusion — some operate a
NICU without a delivery service.

---

## 3. Preliminary access results

Computed over all 668,757 blocks and 160 facilities. **Subject to § 1.**

### Statewide, population-weighted (28,908,656 people)

| Metric | Mean | Median | p75 | p90 | p95 | p99 | Max |
|---|---|---|---|---|---|---|---|
| Drive time (min) | 12.9 | 8.7 | 15.1 | 28.4 | 39.4 | 63.7 | 173.4 |
| Road distance (km) | 18.5 | 11.2 | 21.0 | 43.6 | 63.2 | 106.8 | 221.2 |

### Population by drive-time band

| Band (min) | Population | Share |
|---|---|---|
| < 15 | 21,634,234 | 74.8% |
| 15 – 30 | 4,692,995 | 16.2% |
| 30 – 45 | 1,517,606 | 5.3% |
| 45 – 60 | 677,435 | 2.3% |
| 60 – 90 | 385,467 | 1.3% |
| 90 + | 919 | 0.0% |

**Headline numbers:**

- **2,581,427 Texans (8.9%)** live more than 30 minutes from the nearest
  obstetric facility.
- **386,386 (1.3%)** live more than 60 minutes away.
- **861,715** are more than 80 km away by road.
- 153 of 254 counties have no facility in the analysis set; **4,588,974 people**
  live in them.

### Structural concentration

160 facilities serve 254 counties. The five most-served counties hold **38 of
160 facilities (24%)**. Access is not merely uneven — it is concentrated in
metropolitan cores while the rural tail extends past 170 minutes.

### Road detour ratio

Population-weighted mean **1.33** — road distance runs a third longer than
straight-line. This is the quantitative argument for having built the network at
all: a Euclidean-buffer analysis (still common in this literature) would
understate real travel by roughly 33% on average, and considerably more in West
Texas where the road grid is sparse.

**Use this in the proposal.** It is a concrete methodological advance over the
buffer-based approach reviewers will have seen before.

---

## 4. Reproducibility problems found in the existing notebooks

| Issue | Detail |
|---|---|
| **Lost plotting code** | `mat_car_des_plot.ipynb` references `tx_counties`, `gdf_fac` and `HasFacility`, none of which are defined anywhere in the notebook. The cells that built them were deleted. The published map was **not reproducible.** Rebuilt in `06_make_figures.py`. |
| **Broken data paths** | Both notebooks read bare filenames (`pd.read_csv("texas_obs_facilities_final.csv")`) while the file lives in `data/`. |
| **Out-of-order cells** | `mat_car_des_plot.ipynb` cell 1 calls `len(df)` before cell 2 defines `df`. Execution counts (`In[8]`, `In[4]`, `In[20]`) show the notebook was run out of order and never re-run clean. |
| **Missing inputs** | Four intermediate files referenced by `MCD.ipynb` are absent (see `DATA_SOURCES.md` § 3). |
| **Silent duplicates** | 7 rows share a facility name across distinct HIFLD IDs. |
| **Sentinel values** | HIFLD encodes unknowns as `-999` and `"NOT AVAILABLE"`. Averaging `BEDS` without handling these silently corrupts the statistic. Now converted to nulls. |
| **Corrupt geometry column** | The `geometry` column in the CSV is a stringified Python `bytes` repr of WKB and cannot be parsed back. Geometry is rebuilt from lat/lon. |

The `scripts/` pipeline is numbered, idempotent and runs end to end from raw
downloads. Recommend treating notebooks as exploration and the scripts as the
record of results.

---

## 5. Suggestions for the resubmission

**Methodological strengths worth foregrounding:**

1. **Every block, exactly — no sampling.** 668,757 blocks against 160 facilities
   is 107 M origin–destination pairs if computed naively. Multi-source Dijkstra
   on the transposed graph solves it in **~3 seconds per pass** with no routing
   server, no API quota and no sampling. That is a genuine methodological
   contribution and it is cheap to state.
2. **Direction-correct routing.** Costs are computed node→facility on the
   transposed directed graph, so one-way networks are handled properly. Most
   comparable studies quietly assume symmetry.
3. **Population-weighted throughout**, with official Census weighted centroids
   at every level where the Bureau publishes them.
4. **Full provenance.** Every input is pinned, hashed and timestamped; the OSM
   extract is MD5-verified against the publisher's own checksum.

**Gaps to close before submitting:**

1. **Fix the facility list** (§ 1). Nothing else matters as much.
2. **Get a Census API key** — free, instant, from
   <https://api.census.gov/data/key_signup.html>. This unlocks women aged 15–44
   (ACS table `B01001`) as the denominator. "X% of *women of reproductive age*
   live more than 30 minutes from obstetric care" is a far stronger sentence
   than one about total population, and reviewers will expect it.
3. **Validate travel times against a routing engine.** Take a stratified sample
   of ~500 origin–destination pairs, route them through Google Distance Matrix
   or OpenRouteService, and report the correlation. This converts the free-flow
   speed assumption from a limitation into a *measured* calibration — one table,
   large credibility gain.
4. **Add level of care** (`OB_SRVC_CD`) and report access separately by level.
5. **Consider a 2SFCA measure.** Two-step floating catchment area accounts for
   facility *capacity* against surrounding demand, not just proximity. Bed counts
   are already in the data. This is the standard next step reviewers ask for, and
   distinguishes a proximity study from an access study.
6. **Model cross-border access.** Texas-border residents may use out-of-state
   facilities; the Texas-only extract overestimates their travel time.
7. **Overlay social vulnerability.** Intersecting drive time with CDC/ATSDR SVI
   or rural-urban commuting codes turns a geography result into a health-equity
   result — usually what such proposals are actually funded for.

---

## 6. Figures

All in `results/figures/`, each as 300 dpi PNG and 600 dpi PDF.

| Figure | Content |
|---|---|
| `fig1_maternity_care_deserts` | Counties with no obstetric facility; facilities, county names, city labels, full legend — the original to-do list, rebuilt reproducibly |
| `fig2_drive_time` | Block-group choropleth, drive time to nearest facility |
| `fig3_drive_distance` | Block-group choropleth, road distance |
| `fig4_population_by_drivetime` | Population by drive-time band |
| `fig5_facility_validation` | Counties flagged as deserts that have OSM-mapped hospitals |

**A caution when reading fig2 and fig3.** They are shaded by *area*, and rural
block groups are enormous while urban ones are tiny. The maps look far more
red/dark than the population statistics because most Texans live inside the
small light polygons. Always pair the map with the § 3 population table; a
reviewer who reads only the map will overestimate the share of people affected.

**Attribution requirement.** Figures 2 and 3 are derived from OpenStreetMap and
must carry **"© OpenStreetMap contributors (ODbL)"** wherever published.
