# Findings, Observations and Recommendations

Results from the spatial-access pipeline, the data-quality problems found along
the way, and what they mean for the proposal.

**Status:** the facility-list defect described in the previous revision of this
document has been **fixed**. Section 1 records what it was, how it was corrected,
and how much it changed the answer — because that correction is itself one of the
strongest methodological arguments in the proposal.

---

## 1. The facility list: what was wrong, and the fix

### The defect

`MCD.ipynb` built the facility list with one line:

```python
matched_df = df2[df2['NAME'].isin(df1['FAC_NAME'])]
```

An exact, case-sensitive string join between the CMS Provider of Services file
(which knows *which* hospitals provide obstetrics) and HIFLD (which knows *where*
hospitals are). Two independent failures resulted:

1. **It matched only 136 of 211 eligible hospitals (64%).** Any divergence in how
   two agencies typed a name dropped a real hospital: `ST.` vs `SAINT`,
   `MED CTR` vs `MEDICAL CENTER`, `BSA HOSPITAL` vs `BAPTIST ST ANTHONYS
   HOSPITAL`, `PARKLAND HEALTH AND HOSPITAL SYSTEM` vs `PARKLAND MEMORIAL
   HOSPITAL`.
2. **It never filtered `PGM_TRMNTN_CD`,** so hospitals that had already closed or
   merged were counted as open. Of 377 Texas POS records reporting obstetric
   service, **163 were terminated providers.**

Both errors push the same direction — they manufacture deserts. The most visible
symptom: **Jefferson County (Beaumont, pop. 256,526) showed zero obstetric
facilities and a 61.6-minute mean drive time**, the second-worst in Texas, in a
metro of ~400,000.

### The fix

`scripts/01_prepare_facilities.py` now rebuilds the list from primary sources.

**Eligibility**, from the POS record layout (not inferred — see
`CMOS Dataset Description.pdf`):

| Field | Codes | Rule applied |
|---|---|---|
| `OB_SRVC_CD` | 0=not provided, 1=by staff, 2=under arrangement, 3=both | keep 1 and 3 |
| `PGM_TRMNTN_CD` | 00=active; anything else terminated | keep 00 only |

Code 2 means the service exists only "under arrangement" — patients are referred
elsewhere — so it is not a delivery site. Nine Texas records carry it; they are
retained and flagged, not counted.

**Matching**, progressive passes, strongest evidence first:

| Pass | Matched |
|---|---|
| 1. exact normalized name + ZIP | 145 |
| 2. exact normalized name + city | 9 |
| 3. street address + ZIP | 39 |
| 4. street address + city | 2 |
| 5. fuzzy name (≥88) + ZIP | 11 |
| 7. fuzzy address + city | 1 |
| **Total** | **207 of 211 (98.1%)** |

Address passes are placed *ahead* of loose fuzzy-name passes on purpose: a shared
street number and street name is far better evidence of identity than a similar
corporate name. **Hospitals get renamed; they do not move.** This is what recovers
`BSA HOSPITAL` → `BAPTIST ST ANTHONYS HOSPITAL` (both at 1600 Wallace Blvd,
Amarillo) and `UNIVERSITY HEALTH SYSTEM` → `UNIVERSITY HOSPITAL` (both at 4502
Medical Dr, San Antonio).

The remaining 4 — mostly hospitals built after the HIFLD vintage — are located at
their ZIP Code Tabulation Area centroid and flagged `GEOCODE = zip_centroid`.
That costs a kilometre or two of precision; dropping them would cost an entire
false desert.

### How much it changed the answer

| Measure | Exact-name join | Rebuilt list | Change |
|---|---|---|---|
| Facilities identified | 170 | **211** | +41 |
| Counties covered | 101 | **103** | +2 |
| Texans > 30 min from care | 2,581,427 | **1,059,501** | **−59%** |
| Texans > 60 min from care | 386,386 | **41,940** | −89% |
| Population-weighted mean drive | 12.9 min | **9.5 min** | −26% |
| Jefferson County mean drive | 61.6 min | **18.7 min** | −70% |

**A published desert map built on an uncorrected join is wrong by roughly a factor
of two.** Worst-county rankings flip entirely: the previous list put Orange,
Hardin and Jefferson (all Southeast Texas metro) in the worst ten; the corrected
list puts Crockett, Culberson, Terrell, Reagan and Edwards there — all genuinely
remote West Texas. That change of face validity is itself the check that the fix
worked.

### Still outstanding

The 4 ZIP-centroid facilities should be geocoded properly before publication, and
every match scoring 88–95 (12 records) deserves a manual glance. Both are listed
in `results/tables/facility_match_report.csv` and `facility_unmatched.csv`.

---

## 2. Preliminary access results

All 668,757 blocks against 211 facilities, population-weighted over 28,908,656
people.

| Metric | Mean | p50 | p75 | p90 | p95 | p99 | Max |
|---|---|---|---|---|---|---|---|
| Drive time (min) | 9.5 | 7.1 | 11.1 | 19.5 | 26.9 | 41.5 | 173.4 |
| Road distance (km) | 13.0 | 8.8 | 14.8 | 28.2 | 40.7 | 65.4 | 221.2 |

**Population by drive-time band**

| Band (min) | Population | Share |
|---|---|---|
| < 15 | 24,412,775 | 84.5% |
| 15 – 30 | 3,436,380 | 11.9% |
| 30 – 45 | 871,709 | 3.0% |
| 45 – 60 | 145,852 | 0.5% |
| 60 – 90 | 40,858 | 0.14% |
| 90 + | 1,082 | 0.004% |

**Level of care matters more than proximity.** Access to *any* obstetric facility
is decent; access to one with a NICU is three times worse:

| | > 30 min | > 60 min |
|---|---|---|
| Any obstetric facility (n=211) | 1,059,501 (3.7%) | 41,940 (0.15%) |
| **NICU-capable facility (n=117)** | **3,204,055 (11.1%)** | **670,331 (2.3%)** |

This is the finding to lead with. Proximity to a door is not proximity to care,
and the gap is exactly where preventable maternal and neonatal deaths occur.

**Ten worst counties by mean drive time** — all genuinely remote, all without a
facility: Crockett (79.8 min), Culberson (68.7), Terrell (64.7), Reagan (64.0),
Edwards (63.9), Sutton (63.0), Presidio (61.9), Wheeler (60.0), Menard (58.3),
Dickens (58.2).

---

## 2b. Access for women aged 15–44 — the correct denominator

ACS 2019–2023 table B01001 gives **6,180,678** women aged 15–44 in Texas (20.9%
of the population). Weighting by them rather than by everyone is what a
maternal-health reviewer will expect.

| Benchmark | Women 15–44 within it |
|---|---|
| 25 miles of an obstetric facility | 95.9% |
| 35 miles | 98.6% |
| 50 miles | 99.7% |
| 30 minutes | 97.0% |
| 60 minutes | 99.9% |
| **30 minutes of a NICU-capable facility** | **90.6%** |
| 60 minutes of a NICU-capable facility | 98.2% |

- **185,251 women (3.0%)** are beyond 30 minutes of any obstetric facility.
- **576,262 women (9.4%)** are beyond 30 minutes of a NICU-capable facility —
  **3.1× as many.**

Worst counties for women 15–44: Crockett (79.1 min), Culberson (69.4), King
(68.1), Terrell (66.5), Edwards (66.4), Presidio (66.0), Sutton (64.1),
Reagan (64.0).

**No API key was needed.** `api.census.gov` now rejects unauthenticated requests,
so the pipeline reads the bulk table-based ACS Summary File instead — identical
data, no registration. The nine female age bands (`B01001_030`…`_038`) are
derived at runtime by parsing the official Census table shells rather than being
hard-coded, so a mis-remembered variable number cannot silently corrupt the
denominator.

**Caveat.** ACS publishes block groups, not blocks. Block-level weights are
obtained by sharing each block group's count across its blocks in proportion to
2020 decennial population, which assumes a uniform age–sex mix within a block
group.

---

## 3. The finding the county-level literature cannot see

Cross-checking against hospitals mapped in OpenStreetMap (an independent source,
already in hand for the road network):

- **78 counties have at least one hospital but no obstetric unit.**
- **1,730,926 people live in them.**

These counties are not places that need a new hospital. They need an obstetric
unit **restored inside a hospital that already exists** — a dramatically cheaper
intervention. A binary county-level desert map cannot distinguish them from
counties that never had a hospital at all, so it cannot support this
recommendation. Our block-level measurement can.

This is the strongest policy hook in the analysis and belongs in the proposal's
opening.

---

## 4. Road networks vs straight lines

Population-weighted mean detour ratio: **1.33×**. Road travel is a third longer
than straight-line distance, and worse in the sparse western road grid.

Buffer- and centroid-based studies — still common in this literature — therefore
understate real travel by ~33% on average. This is a concrete, quantified
methodological advance over the approach reviewers will have seen before, and it
cost nothing extra to produce once the network was built.

---

## 5. Siting optimization (preliminary)

`scripts/07_optimize_siting.py` solves the Maximal Covering Location Problem
greedily on the real network. Candidate sites are restricted to existing
incorporated places — a hospital needs staff, utilities and road access, so an
optimum in empty rangeland is not actionable.

Demand is weighted by **women aged 15–44** (`--weight pop` for total population).

**Ten new facilities would bring 72,737 of the 178,685 underserved women aged
15–44 (40.7%) within 30 minutes:**

| # | Site | County | Newly covered | Cumulative |
|---|---|---|---|---|
| 1 | Wharton | Wharton | 10,280 | 5.8% |
| 2 | Bastrop | Bastrop | 10,132 | 11.4% |
| 3 | Wills Point | Van Zandt | 9,192 | 16.6% |
| 4 | Hardin | Liberty | 8,917 | 21.6% |
| 5 | Floresville | Wilson | 6,669 | 25.3% |
| 6 | Jasper | Jasper | 6,348 | 28.8% |
| 7 | Teague | Freestone | 6,313 | 32.4% |
| 8 | Sunset | Montague | 5,237 | 35.3% |
| 9 | Tenaha | Shelby | 4,989 | 38.1% |
| 10 | Rockdale | Milam | 4,656 | 40.7% |

Because coverage is a monotone submodular function, greedy carries a provable
(1 − 1/e) ≈ 63% approximation guarantee — a bounded result, not a heuristic
guess. Runtime: ~30 seconds including 697 bounded Dijkstra searches.

---

## 6. Notebook reproducibility problems (all addressed)

| Issue | Status |
|---|---|
| `mat_car_des_plot.ipynb` referenced `tx_counties`, `gdf_fac`, `HasFacility` — never defined; the published map was not reproducible | rebuilt in `06_make_figures.py` |
| Both notebooks read bare filenames while data lived elsewhere | fixed; all paths via `scripts/paths.py` |
| Cells run out of order (`len(df)` before `df` exists) | pipeline is scripted and linear |
| HIFLD `-999` / `"NOT AVAILABLE"` sentinels averaged into statistics | converted to nulls |
| `geometry` column was a stringified `bytes` repr, unparseable | geometry rebuilt from lat/lon |
| Four intermediate files missing | supplied; provenance now documented |

Treat notebooks as exploration and `scripts/` as the record of results.

---

## 7. Recommendations for the resubmission

**Lead with these, in this order:**

1. **78 counties have a hospital but no obstetric unit** (§3). Cheapest
   intervention, invisible to every published desert map, uniquely visible to us.
2. **The NICU gap** (§2). 11.1% vs 3.7% — level of care, not proximity.
3. **10 sites, 399,723 people** (§5). A concrete, already-computed deliverable.
4. **The correction** (§1). Shows methodological rigor and implicitly critiques
   the existing literature without naming anyone.

**Still to close before submitting:**

1. ~~Get a Census API key~~ **Done — and no key was needed.** Women aged 15–44
   are now the default denominator, read from the bulk ACS Summary File. The
   remaining refinement is sub-block-group disaggregation, which currently
   assumes a uniform age–sex mix within each block group.
2. **Validate travel times** against a commercial routing engine on ~500 sampled
   routes. Converts the free-flow speed assumption from a limitation into a
   measured calibration. One table, large credibility gain.
3. **Add 2SFCA** for capacity. Bed counts are already in the data. This is the
   standard next question and distinguishes a proximity study from an access study.
4. **Date the closures.** CMS POS archives would let the panel run backward and
   turn a snapshot into a trend — the actual policy story in rural Texas.
5. **Model cross-border access.** The Texas-only extract overestimates travel for
   border counties whose nearest facility is in another state.

**On reinforcement learning — a caution.** For a *static* facility-location
problem, ILP is superior to RL and an OR-literate reviewer will say so. Do not
claim RL beats ILP on the static problem. The defensible framing, used in the
revised proposal: ILP is the shipped default; RL is evaluated for the
**sequential, budget-phased, uncertainty-aware** version, where facilities are
funded over years and demand shifts. That is a setting where RL genuinely earns
its place, and offering to report the comparison honestly — including a negative
result — reads as confidence rather than hedging.

---

## 8. Figures

`results/figures/`, each as 300 dpi PNG and 600 dpi PDF.

**Proposal figures**

| Figure | Content |
|---|---|
| `proposal_fig1_access_landscape` | 4 panels: drive time, NICU drive time, population by band, hospital-but-no-obstetrics counties |
| `proposal_fig2_siting_optimizer` | 10 recommended sites over unmet demand + coverage curve |
| `proposal_fig3_method_validation` | Detour ratio distribution + what the facility correction changed |

**Standard figures**: `fig1_maternity_care_deserts`, `fig2_drive_time`,
`fig3_drive_distance`, `fig4_population_by_drivetime`, `fig5_facility_validation`.

**A caution when reading the choropleths.** They are shaded by *area*, and rural
block groups are enormous while urban ones are tiny. The maps look far darker
than the population statistics because most Texans live inside the small light
polygons. Always pair a map with the §2 population table.

**Attribution.** Figures derived from the road network must carry
**"© OpenStreetMap contributors (ODbL)"** wherever published.
