#!/usr/bin/env python
"""
reorganize_data.py
==================
One-off migration: move the flat ``data/raw`` + ``data/processed`` layout into
the thematic layout defined in ``paths.py``.

    data/<theme>/raw/        exactly as downloaded, never modified
    data/<theme>/processed/  derived, analysis-ready

Safe to re-run: files already in place are left alone, and anything the script
does not recognise is reported rather than moved blindly.

Usage
-----
    python scripts/reorganize_data.py --dry-run
    python scripts/reorganize_data.py
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import paths as P

# filename -> theme. Directories are matched by name too.
ROUTING: dict[str, str] = {
    # ---- boundaries
    "cb_2023_us_county_500k.zip": "boundaries",
    "tl_2024_48_place.zip": "boundaries",
    "tl_2024_us_county": "boundaries",
    "counties.parquet": "boundaries",
    "places.parquet": "boundaries",
    # ---- population
    "tl_2024_48_tabblock20.zip": "population",
    "tl_2024_48_bg.zip": "population",
    "tl_2024_48_tract.zip": "population",
    "blocks.parquet": "population",
    "blockgroups.parquet": "population",
    "block_points.parquet": "population",
    # ---- centroids
    "CenPop2020_Mean_BG48.txt": "centroids",
    "CenPop2020_Mean_TR48.txt": "centroids",
    "CenPop2020_Mean_CO.txt": "centroids",
    "bg_points.parquet": "centroids",
    "tract_points.parquet": "centroids",
    # ---- street network
    "texas-latest.osm.pbf": "street_network",
    "texas-latest.osm.pbf.md5": "street_network",
    "network_nodes.parquet": "street_network",
    "network_edges.parquet": "street_network",
    "network_meta.json": "street_network",
    # ---- facilities
    "CMOS Data.csv": "facilities",
    "Cleaned_CMOS_Data.csv": "facilities",
    "CMOS Dataset Description.pdf": "facilities",
    "Cleaned_texas_hospitals_HIFLD.csv": "facilities",
    "texas_obs_facilities_final.csv": "facilities",
    "facilities.parquet": "facilities",
    "facilities_analysis.parquet": "facilities",
    "block_access.parquet": "facilities",
    "access_meta.json": "facilities",
}

# Which subdirectory a file lands in. Anything produced by the pipeline is
# "processed"; anything downloaded or supplied by hand is "raw".
PROCESSED_SUFFIXES = {".parquet"}
PROCESSED_NAMES = {"network_meta.json", "access_meta.json"}


def destination(name: str) -> tuple[str, str] | None:
    theme = ROUTING.get(name)
    if theme is None:
        return None
    is_proc = Path(name).suffix in PROCESSED_SUFFIXES or name in PROCESSED_NAMES
    return theme, ("processed" if is_proc else "raw")


def move(src: Path, dst: Path, dry: bool) -> None:
    if dst.exists():
        print(f"  [skip]  {dst.relative_to(P.PROJECT_ROOT)} already exists")
        return
    print(f"  {'[plan]' if dry else '[move]'} {src.relative_to(P.PROJECT_ROOT)}"
          f"  ->  {dst.relative_to(P.PROJECT_ROOT)}")
    if not dry:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="show the plan only")
    args = ap.parse_args()

    if not args.dry_run:
        P.ensure_tree()

    # Everything currently sitting directly under data/, data/raw, data/processed
    candidates: list[Path] = []
    for parent in (P.DATA, P.DATA / "raw", P.DATA / "processed"):
        if parent.exists():
            candidates.extend(
                p for p in parent.iterdir()
                if p.name not in P.THEMES and not p.name.startswith(".")
            )

    unrouted: list[Path] = []
    for src in sorted(candidates):
        if src.name == "_manifest.json":
            move(src, P.MANIFEST, args.dry_run)
            continue
        dest = destination(src.name)
        if dest is None:
            unrouted.append(src)
            continue
        theme, sub = dest
        move(src, P.DATA / theme / sub / src.name, args.dry_run)

    if unrouted:
        print("\n  [warn] no routing rule - left in place:")
        for p in unrouted:
            print(f"         {p.relative_to(P.PROJECT_ROOT)}")

    # Drop the old empty directories.
    if not args.dry_run:
        for old in (P.DATA / "raw", P.DATA / "processed"):
            if old.exists() and not any(old.iterdir()):
                old.rmdir()
                print(f"  [rmdir] {old.relative_to(P.PROJECT_ROOT)}")

    print("\nFinal layout:")
    for theme in P.THEMES:
        for sub in ("raw", "processed"):
            d = P.DATA / theme / sub
            if d.exists():
                items = sorted(p.name for p in d.iterdir() if not p.name.startswith("."))
                print(f"  data/{theme}/{sub}/  ({len(items)})")
                for it in items:
                    print(f"      {it}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
