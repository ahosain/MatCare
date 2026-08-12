#!/usr/bin/env python
"""
01_prepare_facilities.py
========================
Rebuild the Texas obstetric-facility list from primary sources, replacing the
exact-name join that silently dropped roughly a quarter of eligible hospitals.

Sources
-------
``Cleaned_CMOS_Data.csv``
    CMS **Provider of Services (POS)** current file, hospital category, filtered
    to records reporting some obstetric service. Despite the project's "CMOS"
    filename this is the CMS POS extract (record layout dated 2023-04-02; see
    ``CMOS Dataset Description.pdf``). It is the authority on *which* hospitals
    provide obstetric care, and carries no coordinates.

``Cleaned_texas_hospitals_HIFLD.csv``
    HIFLD hospital layer, 876 Texas hospitals. Authority on *where* they are.

Neither file carries the other's identifier, so they must be matched.

Eligibility (from the POS record layout, not inferred)
------------------------------------------------------
``OB_SRVC_CD``     0=NOT PROVIDED  1=BY STAFF  2=UNDER ARRANGEMENT  3=BOTH
``PGM_TRMNTN_CD``  00=ACTIVE PROVIDER; any other value is a terminated provider
                   (01=voluntary merger/closure, 05=involuntary, ...)

The analysis set keeps **active** providers delivering obstetrics **on site**
(``OB_SRVC_CD in {1, 3}``). Code 2 means the service exists only "under
arrangement" - referred elsewhere - which is not a delivery site, so those are
retained but flagged rather than counted.

Why the previous list was wrong
-------------------------------
``MCD.ipynb`` used ``df2[df2['NAME'].isin(df1['FAC_NAME'])]`` - exact,
case-sensitive string equality. That matched 136 of 211 eligible hospitals (64%)
and simultaneously admitted terminated providers, because it never filtered on
``PGM_TRMNTN_CD``. Both errors push the same way: they manufacture deserts.

Matching strategy
-----------------
Progressive passes, strongest evidence first. Each POS record is claimed once,
by the first pass that matches it; each HIFLD record can be claimed once.

    1. normalised name  + same ZIP5
    2. normalised name  + same city
    3. fuzzy name >= 88 + same ZIP5
    4. fuzzy name >= 90 + same city
    5. fuzzy name >= 92 + same county FIPS

Name normalisation expands ST->SAINT and CTR->CENTER, drops corporate suffixes
and punctuation. Fuzzy scoring uses ``rapidfuzz`` token-set ratio, which is
insensitive to word order and to one name carrying extra tokens.

Every match records the pass and score that produced it, so the whole list is
auditable and the borderline band can be reviewed by hand.

Outputs
-------
data/facilities/processed/facilities.parquet           all matched, flagged
data/facilities/processed/facilities_analysis.parquet  the modelling set
results/tables/facility_match_report.csv               per-pass match counts
results/tables/facility_unmatched.csv                  needs manual review
results/tables/facility_summary.csv                    counts by type/level

Usage
-----
    python scripts/01_prepare_facilities.py
    python scripts/01_prepare_facilities.py --include-arrangement
"""

from __future__ import annotations

import argparse
import zipfile
import re

import geopandas as gpd
import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

import paths as P

PROJECT_ROOT = P.PROJECT_ROOT
RAW = P.FACILITIES_RAW
PROC = P.FACILITIES_PROC
TABLES = P.TABLES

# Texas bounding box (WGS84) with a margin, for coordinate validation.
TX_BBOX = dict(lon_min=-107.0, lon_max=-93.4, lat_min=25.7, lat_max=36.6)
HIFLD_NULLS = {-999, -999.0, "-999", "NOT AVAILABLE", "NOT-AVAILABLE", ""}

# POS service codes -> human labels (from the POS record layout).
SRVC_LABEL = {
    0: "not provided",
    1: "provided by staff",
    2: "provided under arrangement",
    3: "provided by staff and under arrangement",
}

_ABBREV = [
    (r"\bST\.?\b", "SAINT"),
    (r"\bCTR\b", "CENTER"),
    (r"\bCTRS\b", "CENTERS"),
    (r"\bMED\b", "MEDICAL"),
    (r"\bHOSP\b", "HOSPITAL"),
    (r"\bREG\b", "REGIONAL"),
    (r"\bMEM\b", "MEMORIAL"),
    (r"\bMT\.?\b", "MOUNT"),
    (r"\bUNIV\b", "UNIVERSITY"),
    (r"\bGEN\b", "GENERAL"),
]
_DROP = r"\b(LLC|INC|LP|LTD|CORP|COMPANY|THE|OF|AND|A|AN)\b"

# Street-type and directional abbreviations, for address comparison.
_ADDR_ABBREV = [
    (r"\bBLVD\b", "BOULEVARD"), (r"\bDR\b", "DRIVE"), (r"\bST\b", "STREET"),
    (r"\bAVE\b", "AVENUE"), (r"\bRD\b", "ROAD"), (r"\bPKWY\b", "PARKWAY"),
    (r"\bPKY\b", "PARKWAY"), (r"\bHWY\b", "HIGHWAY"), (r"\bFWY\b", "FREEWAY"),
    (r"\bLN\b", "LANE"), (r"\bCT\b", "COURT"), (r"\bCIR\b", "CIRCLE"),
    (r"\bN\b", "NORTH"), (r"\bS\b", "SOUTH"), (r"\bE\b", "EAST"), (r"\bW\b", "WEST"),
]


def norm_name(s: object) -> str:
    """Normalise a facility name for comparison."""
    t = str(s).upper().replace("&", " AND ")
    for pat, rep in _ABBREV:
        t = re.sub(pat, rep, t)
    t = re.sub(_DROP, " ", t)
    return re.sub(r"[^A-Z0-9]+", " ", t).strip()


def norm_addr(s: object) -> tuple[str, str]:
    """
    Split a street address into (leading house number, normalised street name).

    Suite/floor detail after a comma is discarded, and ranged numbers such as
    "5200 - 5201 HARRY HINES" keep only the first number, so the CMS form of an
    address still matches the HIFLD form.
    """
    t = str(s).upper().split(",")[0]
    t = re.sub(r"[^A-Z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    m = re.match(r"^(\d+)\s+(.*)$", t)
    if not m:
        return "", ""
    num, rest = m.group(1), m.group(2)
    # Drop a second number in a range, e.g. "5200 5201 HARRY HINES".
    rest = re.sub(r"^\d+\s+", "", rest)
    for pat, rep in _ADDR_ABBREV:
        rest = re.sub(pat, rep, rest)
    rest = re.sub(r"\b(SUITE|STE|FLOOR|FL|BLDG|BUILDING|UNIT)\b.*$", "", rest).strip()
    return num, re.sub(r"\s+", " ", rest).strip()


def zip5(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d{5})")[0]


def blank_sentinels(s: pd.Series) -> pd.Series:
    return s.mask(s.isin(HIFLD_NULLS))


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
# (label, blocking key, mode, threshold)
# Strongest evidence first. Address passes come before loose fuzzy-name passes
# because a shared street number and street name is far better evidence of
# identity than a similar corporate name - hospitals get renamed, they do not
# move. This is what recovers facilities such as "BSA HOSPITAL" ->
# "BAPTIST ST ANTHONYS HOSPITAL" (both at 1600 WALLACE BLVD, Amarillo).
PASSES = [
    ("1. exact name + ZIP", "ZIP5", "name", 100),
    ("2. exact name + city", "CITY", "name", 100),
    ("3. address + ZIP", "ZIP5", "addr", 100),
    ("4. address + city", "CITY", "addr", 100),
    ("5. fuzzy name + ZIP", "ZIP5", "name", 88),
    ("6. fuzzy name + city", "CITY", "name", 90),
    ("7. fuzzy address + city", "CITY", "addr_fuzzy", 92),
    ("8. fuzzy name + county", "CFIPS", "name", 92),
]


def match(pos: pd.DataFrame, hif: pd.DataFrame) -> pd.DataFrame:
    """
    Assign each POS record at most one HIFLD record.

    Returns the POS frame with match_stage / match_score / hifld_idx columns.
    """
    pos = pos.copy()
    pos["hifld_idx"] = -1
    pos["match_score"] = np.nan
    pos["match_stage"] = pd.NA

    taken: set[int] = set()

    for label, block_key, mode, threshold in PASSES:
        todo = pos.index[pos["hifld_idx"] < 0]
        n_before = len(todo)
        if n_before == 0:
            break

        for i in todo:
            key = pos.at[i, block_key]
            if pd.isna(key):
                continue
            # Candidate HIFLD rows sharing the blocking key and not yet claimed.
            cand = hif[(hif[block_key] == key) & (~hif.index.isin(taken))]
            if cand.empty:
                continue

            if mode == "name" and threshold == 100:
                hit = cand.index[cand["nname"] == pos.at[i, "nname"]]
                if len(hit) == 0:
                    continue
                j, score = int(hit[0]), 100.0

            elif mode == "name":
                best = process.extractOne(
                    pos.at[i, "nname"],
                    cand["nname"].to_dict(),
                    scorer=fuzz.token_set_ratio,
                    score_cutoff=threshold,
                )
                if best is None:
                    continue
                _, score, j = best
                j = int(j)

            elif mode == "addr":
                num, street = pos.at[i, "addr_num"], pos.at[i, "addr_street"]
                if not num or not street:
                    continue
                hit = cand.index[
                    (cand["addr_num"] == num) & (cand["addr_street"] == street)
                ]
                if len(hit) == 0:
                    continue
                j, score = int(hit[0]), 100.0

            else:  # addr_fuzzy - same house number, similar street name
                num, street = pos.at[i, "addr_num"], pos.at[i, "addr_street"]
                if not num or not street:
                    continue
                same_num = cand[cand["addr_num"] == num]
                if same_num.empty:
                    continue
                best = process.extractOne(
                    street,
                    same_num["addr_street"].to_dict(),
                    scorer=fuzz.token_set_ratio,
                    score_cutoff=threshold,
                )
                if best is None:
                    continue
                _, score, j = best
                j = int(j)

            pos.at[i, "hifld_idx"] = j
            pos.at[i, "match_score"] = float(score)
            pos.at[i, "match_stage"] = label
            taken.add(j)

        n_after = int((pos["hifld_idx"] < 0).sum())
        print(f"  {label:24s} matched {n_before - n_after:3d}   remaining {n_after:3d}")

    return pos


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--include-arrangement",
        action="store_true",
        help="also count OB_SRVC_CD=2 (under arrangement only) as a delivery site",
    )
    args = ap.parse_args()

    P.ensure_tree()

    # ---------------------------------------------------------------- load POS
    pos = pd.read_csv(RAW / "Cleaned_CMOS_Data.csv", encoding="utf-8-sig", low_memory=False)
    pos = pos[pos["STATE_CD"] == "TX"].copy()
    print(f"CMS POS: {len(pos)} Texas hospital records reporting obstetric service")

    pos["ACTIVE"] = pos["PGM_TRMNTN_CD"].fillna(-1).astype(int) == 0
    pos["OB_SRVC_CD"] = pd.to_numeric(pos["OB_SRVC_CD"], errors="coerce").astype("Int64")

    onsite = {1, 3} | ({2} if args.include_arrangement else set())
    pos["OB_ONSITE"] = pos["OB_SRVC_CD"].isin(onsite)

    print("\n  POS eligibility breakdown")
    print(
        pd.crosstab(
            pos["OB_SRVC_CD"].map(SRVC_LABEL), pos["ACTIVE"], rownames=["OB service"],
            colnames=["active provider"],
        ).to_string()
    )

    elig = pos[pos["ACTIVE"] & pos["OB_ONSITE"]].copy()
    print(f"\n  eligible (active + obstetrics on site): {len(elig)}")
    print(f"  excluded, terminated provider          : {int((~pos['ACTIVE']).sum())}")
    print(f"  excluded, under arrangement only       : "
          f"{int((pos['OB_SRVC_CD'] == 2).sum())}")

    elig["ZIP5"] = zip5(elig["ZIP_CD"])
    elig["CITY"] = elig["CITY_NAME"].astype(str).str.upper().str.strip()
    elig["CFIPS"] = (
        P.TX_FIPS
        + pd.to_numeric(elig["FIPS_CNTY_CD"], errors="coerce")
        .astype("Int64").astype(str).str.zfill(3)
    )
    elig["nname"] = elig["FAC_NAME"].map(norm_name)
    elig[["addr_num", "addr_street"]] = pd.DataFrame(
        elig["ST_ADR"].map(norm_addr).tolist(), index=elig.index
    )

    # -------------------------------------------------------------- load HIFLD
    hif = pd.read_csv(RAW / "Cleaned_texas_hospitals_HIFLD.csv", low_memory=False)
    hif = hif.drop(columns=[c for c in ("geometry", "bbox") if c in hif.columns])
    print(f"\nHIFLD: {len(hif)} Texas hospitals ({int((hif['STATUS'] == 'OPEN').sum())} open)")

    hif["ZIP5"] = zip5(hif["ZIP"])
    hif["CITY"] = hif["CITY"].astype(str).str.upper().str.strip()
    hif["CFIPS"] = hif["COUNTYFIPS"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(5)
    hif["nname"] = hif["NAME"].map(norm_name)
    hif[["addr_num", "addr_street"]] = pd.DataFrame(
        hif["ADDRESS"].map(norm_addr).tolist(), index=hif.index
    )
    hif["LATITUDE"] = pd.to_numeric(hif["LATITUDE"], errors="coerce")
    hif["LONGITUDE"] = pd.to_numeric(hif["LONGITUDE"], errors="coerce")
    for col in ("BEDS", "TTL_STAFF"):
        hif[col] = pd.to_numeric(blank_sentinels(hif[col]), errors="coerce")

    # ------------------------------------------------------------------ match
    print("\nMatching CMS POS -> HIFLD")
    m = match(elig.reset_index(drop=True), hif)

    ok = m["hifld_idx"] >= 0
    print(f"\n  matched {int(ok.sum())} of {len(m)} ({100 * ok.mean():.1f}%)")

    report = (
        m.loc[ok].groupby("match_stage")
        .agg(n=("hifld_idx", "size"), mean_score=("match_score", "mean"))
        .reset_index()
    )
    report.to_csv(TABLES / "facility_match_report.csv", index=False)
    print(report.to_string(index=False, float_format=lambda x: f"{x:.1f}"))

    unmatched = m.loc[~ok, ["PRVDR_NUM", "FAC_NAME", "CITY_NAME", "ZIP5", "CFIPS"]]
    unmatched.to_csv(TABLES / "facility_unmatched.csv", index=False)
    if len(unmatched):
        print(f"\n  [action] {len(unmatched)} eligible hospital(s) had no HIFLD match "
              f"-> results/tables/facility_unmatched.csv")
        for _, r in unmatched.head(12).iterrows():
            print(f"           {r['FAC_NAME']}  ({r['CITY_NAME']}, {r['ZIP5']})")

    # -------------------------------------------------------------- assemble
    hcols = ["NAME", "ADDRESS", "CITY", "COUNTY", "CFIPS", "LATITUDE", "LONGITUDE",
             "BEDS", "TRAUMA", "TYPE", "OWNER", "STATUS", "TTL_STAFF", "HELIPAD"]
    # Keep unmatched rows too - dropping them is what manufactures false deserts.
    joined = m.join(hif[hcols].add_prefix("HIFLD_"), on="hifld_idx", how="left")

    out = pd.DataFrame(
        {
            "CCN": joined["PRVDR_NUM"].astype(str),
            "FAC_NAME": joined["FAC_NAME"].str.strip().str.upper(),
            "HIFLD_NAME": joined["HIFLD_NAME"],
            # Fall back to the CMS values wherever there is no HIFLD row.
            "ADDRESS": joined["HIFLD_ADDRESS"].fillna(joined["ST_ADR"]),
            "CITY": joined["HIFLD_CITY"].fillna(joined["CITY"]),
            "COUNTY": joined["HIFLD_COUNTY"],
            "COUNTYFIPS": joined["HIFLD_CFIPS"].fillna(joined["CFIPS"]),
            "ZIP5": joined["ZIP5"],
            "LATITUDE": joined["HIFLD_LATITUDE"],
            "LONGITUDE": joined["HIFLD_LONGITUDE"],
            "OB_SRVC_CD": joined["OB_SRVC_CD"],
            "OB_SRVC": joined["OB_SRVC_CD"].map(SRVC_LABEL),
            "NICU_CD": pd.to_numeric(joined["NEONTL_ICU_SRVC_CD"], errors="coerce").astype("Int64"),
            "NURSERY_CD": pd.to_numeric(joined["NEONTL_NRSRY_SRVC_CD"], errors="coerce").astype("Int64"),
            "BED_CNT": pd.to_numeric(joined["BED_CNT"], errors="coerce"),
            "HIFLD_BEDS": joined["HIFLD_BEDS"],
            "TYPE": joined["HIFLD_TYPE"],
            "OWNER": joined["HIFLD_OWNER"],
            "RURAL_URBAN": joined["CBSA_URBN_RRL_IND"],
            "match_stage": joined["match_stage"],
            "match_score": joined["match_score"],
        }
    )

    # NICU on site is the usable proxy for higher level of maternal care.
    out["NICU_ONSITE"] = out["NICU_CD"].isin([1, 3])

    # ------------------------------------------- fall back to a ZIP centroid
    # A hospital HIFLD does not list still exists. Locating it at its ZIP Code
    # Tabulation Area centroid costs a kilometre or two of precision; dropping
    # it costs an entire false maternity care desert. The precision of every
    # coordinate is recorded so the weaker ones can be filtered or reviewed.
    out["GEOCODE"] = np.where(out["LATITUDE"].notna(), "hifld_point", "unlocated")

    gaz = P.CENTROIDS_RAW / "2024_Gaz_zcta_national.zip"
    need = out["LATITUDE"].isna()
    if need.any() and gaz.exists():
        with zipfile.ZipFile(gaz) as zf:
            member = next(n for n in zf.namelist() if n.endswith(".txt"))
            with zf.open(member) as fh:
                z = pd.read_csv(fh, sep="\t", dtype={"GEOID": str}, encoding="latin-1")
        z.columns = [c.strip() for c in z.columns]
        zmap = z.set_index("GEOID")[["INTPTLAT", "INTPTLONG"]]
        idx = out.loc[need, "ZIP5"]
        out.loc[need, "LATITUDE"] = idx.map(zmap["INTPTLAT"]).to_numpy()
        out.loc[need, "LONGITUDE"] = idx.map(zmap["INTPTLONG"]).to_numpy()
        filled = need & out["LATITUDE"].notna()
        out.loc[filled, "GEOCODE"] = "zip_centroid"
        print(f"\n  [fallback] {int(filled.sum())} unmatched hospital(s) located at "
              f"their ZIP centroid; {int((out['GEOCODE'] == 'unlocated').sum())} "
              f"still unlocated")

    # -------------------------------------------------- validate coordinates
    bad = (
        out["LATITUDE"].isna()
        | out["LONGITUDE"].isna()
        | ~out["LATITUDE"].between(TX_BBOX["lat_min"], TX_BBOX["lat_max"])
        | ~out["LONGITUDE"].between(TX_BBOX["lon_min"], TX_BBOX["lon_max"])
    )
    if bad.any():
        print(f"\n  [warn] {int(bad.sum())} row(s) outside the Texas bbox - dropped")
        out = out[~bad].copy()

    dup = out["CCN"].duplicated().sum()
    if dup:
        print(f"  [info] dropping {dup} duplicate CCN row(s)")
        out = out.drop_duplicates("CCN")

    gdf = gpd.GeoDataFrame(
        out,
        geometry=gpd.points_from_xy(out["LONGITUDE"], out["LATITUDE"]),
        crs=P.WGS84,
    ).to_crs(P.TX_ALBERS)
    gdf["FAC_ID"] = np.arange(len(gdf), dtype=np.int32)

    gdf.to_parquet(PROC / "facilities.parquet", index=False)
    gdf.to_parquet(PROC / "facilities_analysis.parquet", index=False)

    # ------------------------------------------------------------- summaries
    summary = (
        gdf.groupby("TYPE", dropna=False)
        .agg(n=("FAC_ID", "size"), nicu=("NICU_ONSITE", "sum"),
             beds=("BED_CNT", "sum"), median_beds=("BED_CNT", "median"))
        .sort_values("n", ascending=False)
    )
    summary.to_csv(TABLES / "facility_summary.csv")

    n_cty = gdf["COUNTYFIPS"].nunique()
    print("\n" + "=" * 62)
    print("REBUILT OBSTETRIC FACILITY LIST")
    print("=" * 62)
    print(f"  facilities (active, obstetrics on site) : {len(gdf)}")
    print(f"  ...with NICU on site                    : {int(gdf['NICU_ONSITE'].sum())}")
    print(f"  counties represented                    : {n_cty} of 254")
    print(f"  counties with NO facility               : {254 - n_cty}")
    print("\n--- by hospital type ---")
    print(summary.to_string())

    old = RAW / "texas_obs_facilities_final.csv"
    if old.exists():
        n_old = len(pd.read_csv(old))
        print(f"\n  previous exact-name list : {n_old} facilities")
        print(f"  rebuilt list             : {len(gdf)} facilities "
              f"({len(gdf) - n_old:+d})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
