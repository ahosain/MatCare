#!/usr/bin/env python
"""
00_download_data.py
===================
Download every external dataset the MatCare spatial-access analysis depends on.

Design goals
------------
* **Reproducible** - every source is pinned to an explicit, documented URL.
* **Resumable**    - partial downloads continue via HTTP Range requests.
* **Auditable**    - size + SHA256 + retrieval timestamp for every file are
                     written to ``data/raw/_manifest.json``.
* **Idempotent**   - re-running skips files already downloaded intact.

All URLs in ``SOURCES`` were verified to return HTTP 200 on 2026-08-11.
See ``docs/DATA_SOURCES.md`` for provenance, licensing and citations.

Usage
-----
    python scripts/00_download_data.py            # download everything
    python scripts/00_download_data.py --only osm # download one source by key
    python scripts/00_download_data.py --verify   # re-hash, do not download
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
MANIFEST_PATH = RAW_DIR / "_manifest.json"

CHUNK = 1 << 20  # 1 MiB streaming chunk
TIMEOUT = (30, 120)  # (connect, read) seconds


# --------------------------------------------------------------------------
# Source catalogue
# --------------------------------------------------------------------------
# `expected_bytes` is the Content-Length observed at verification time. It is a
# sanity check, not a hard assertion: the Census re-publishes TIGER annually and
# Geofabrik rebuilds the OSM extract daily, so sizes drift. A mismatch warns.
SOURCES: dict[str, dict] = {
    # ---------------------------------------------------------------- census
    "blocks": {
        "url": "https://www2.census.gov/geo/tiger/TIGER2024/TABBLOCK20/tl_2024_48_tabblock20.zip",
        "filename": "tl_2024_48_tabblock20.zip",
        "expected_bytes": 435_898_815,
        "description": (
            "TIGER/Line 2024 vintage of the 2020 Census tabulation blocks for "
            "Texas (FIPS 48). Carries POP20 / HOUSING20 counts plus the "
            "official INTPTLAT20 / INTPTLON20 internal points."
        ),
    },
    "block_groups": {
        "url": "https://www2.census.gov/geo/tiger/TIGER2024/BG/tl_2024_48_bg.zip",
        "filename": "tl_2024_48_bg.zip",
        "expected_bytes": 50_301_029,
        "description": "TIGER/Line 2024 block group polygons, Texas.",
    },
    "tracts": {
        "url": "https://www2.census.gov/geo/tiger/TIGER2024/TRACT/tl_2024_48_tract.zip",
        "filename": "tl_2024_48_tract.zip",
        "expected_bytes": 32_628_659,
        "description": "TIGER/Line 2024 census tract polygons, Texas.",
    },
    "places": {
        "url": "https://www2.census.gov/geo/tiger/TIGER2024/PLACE/tl_2024_48_place.zip",
        "filename": "tl_2024_48_place.zip",
        "expected_bytes": 9_717_329,
        "description": (
            "TIGER/Line 2024 incorporated places, Texas. Supplies real city "
            "boundaries/centroids so map labels are not hard-coded."
        ),
    },
    "counties_cb": {
        "url": "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip",
        "filename": "cb_2023_us_county_500k.zip",
        "expected_bytes": 11_630_077,
        "description": (
            "Cartographic Boundary counties, 1:500k, 2023. Generalised "
            "coastlines - preferred for mapping over full TIGER county files."
        ),
    },
    # ------------------------------------------- population-weighted centroids
    "cenpop_bg": {
        "url": "https://www2.census.gov/geo/docs/reference/cenpop2020/blkgrp/CenPop2020_Mean_BG48.txt",
        "filename": "CenPop2020_Mean_BG48.txt",
        "expected_bytes": 815_278,
        "description": (
            "2020 Census Centers of Population, block group level, Texas. "
            "Population-WEIGHTED centroids computed by the Census Bureau. "
            "NOTE: the Bureau publishes these only at county / tract / block "
            "group level - there is no block-level equivalent."
        ),
    },
    "cenpop_tract": {
        "url": "https://www2.census.gov/geo/docs/reference/cenpop2020/tract/CenPop2020_Mean_TR48.txt",
        "filename": "CenPop2020_Mean_TR48.txt",
        "expected_bytes": 289_614,
        "description": "2020 Census Centers of Population, tract level, Texas.",
    },
    "cenpop_county": {
        "url": "https://www2.census.gov/geo/docs/reference/cenpop2020/county/CenPop2020_Mean_CO.txt",
        "filename": "CenPop2020_Mean_CO.txt",
        "expected_bytes": None,
        "description": "2020 Census Centers of Population, county level, national.",
    },
    # ------------------------------------------------------------------- osm
    "osm": {
        "url": "https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf",
        "filename": "texas-latest.osm.pbf",
        "expected_bytes": 713_707_612,
        "description": (
            "OpenStreetMap extract for Texas from Geofabrik. Source of the "
            "drivable road network used for network distance / travel time. "
            "Geofabrik rebuilds nightly, so the exact bytes change daily - the "
            "manifest records the resolved dated URL and SHA256 actually used."
        ),
    },
    "osm_md5": {
        "url": "https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf.md5",
        "filename": "texas-latest.osm.pbf.md5",
        "expected_bytes": None,
        "description": "Publisher-provided MD5 checksum for the Texas OSM extract.",
    },
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def human(n: float | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def remote_size(url: str, session: requests.Session) -> tuple[int | None, str]:
    """Return (content_length, resolved_url) following redirects."""
    try:
        r = session.head(url, allow_redirects=True, timeout=TIMEOUT)
        r.raise_for_status()
        cl = r.headers.get("Content-Length")
        return (int(cl) if cl else None, r.url)
    except requests.RequestException:
        return (None, url)


def download(key: str, spec: dict, session: requests.Session, manifest: dict) -> None:
    dest = RAW_DIR / spec["filename"]
    total, resolved = remote_size(spec["url"], session)

    if dest.exists() and total is not None and dest.stat().st_size == total:
        print(f"  [skip]  {spec['filename']} already complete ({human(total)})")
        record(key, spec, dest, resolved, manifest)
        return

    # Resume a partial file when the server supports Range requests.
    start = dest.stat().st_size if dest.exists() else 0
    if total is not None and start > total:
        start = 0  # stale/corrupt partial - restart
    headers = {"Range": f"bytes={start}-"} if start else {}
    mode = "ab" if start else "wb"

    if start:
        print(f"  [resume] {spec['filename']} from {human(start)}/{human(total)}")
    else:
        print(f"  [get]   {spec['filename']} ({human(total)})")

    with session.get(spec["url"], stream=True, headers=headers, timeout=TIMEOUT) as r:
        if start and r.status_code == 200:
            # Server ignored the Range header - start over from scratch.
            start, mode = 0, "wb"
        r.raise_for_status()
        done, t0, last = start, time.time(), time.time()
        with dest.open(mode) as fh:
            for chunk in r.iter_content(CHUNK):
                fh.write(chunk)
                done += len(chunk)
                if time.time() - last > 5:
                    rate = (done - start) / max(time.time() - t0, 1e-9)
                    pct = f"{100 * done / total:5.1f}%" if total else "  ?  "
                    print(
                        f"          {pct}  {human(done)}  @ {human(rate)}/s",
                        flush=True,
                    )
                    last = time.time()

    print(f"  [done]  {spec['filename']} -> {human(dest.stat().st_size)}")
    record(key, spec, dest, resolved, manifest)


def record(key: str, spec: dict, dest: Path, resolved: str, manifest: dict) -> None:
    size = dest.stat().st_size
    exp = spec.get("expected_bytes")
    if exp and size != exp:
        print(
            f"  [warn]  {spec['filename']}: {human(size)} but catalogue expected "
            f"{human(exp)} (publisher may have refreshed the file)"
        )
    print(f"  [hash]  computing sha256 for {spec['filename']} ...", flush=True)
    manifest[key] = {
        "filename": spec["filename"],
        "requested_url": spec["url"],
        "resolved_url": resolved,
        "bytes": size,
        "sha256": sha256_of(dest),
        "retrieved_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "description": spec["description"],
    }
    save_manifest(manifest)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", nargs="*", metavar="KEY", help="download only these keys")
    ap.add_argument("--verify", action="store_true", help="re-hash local files only")
    ap.add_argument("--list", action="store_true", help="list source keys and exit")
    args = ap.parse_args()

    if args.list:
        for k, s in SOURCES.items():
            print(f"{k:16s} {human(s.get('expected_bytes')):>9s}  {s['url']}")
        return 0

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    keys = args.only or list(SOURCES)

    unknown = [k for k in keys if k not in SOURCES]
    if unknown:
        print(f"Unknown source key(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    if args.verify:
        for k in keys:
            dest = RAW_DIR / SOURCES[k]["filename"]
            status = (
                f"{human(dest.stat().st_size)}  {sha256_of(dest)[:16]}..."
                if dest.exists()
                else "MISSING"
            )
            print(f"{k:16s} {status}")
        return 0

    session = requests.Session()
    session.headers["User-Agent"] = "MatCare-research/1.0 (academic use)"

    failed: list[str] = []
    for k in keys:
        print(f"\n[{k}]")
        try:
            download(k, SOURCES[k], session, manifest)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"  [FAIL]  {k}: {exc}", file=sys.stderr)
            failed.append(k)

    print(f"\nManifest written to {MANIFEST_PATH.relative_to(PROJECT_ROOT)}")
    if failed:
        print(f"FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("All sources retrieved successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
