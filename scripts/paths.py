"""
paths.py
========
Single source of truth for the project's data layout.

The data directory is organised **by theme**, and each theme carries its own
``raw/`` (exactly as downloaded, never modified) and ``processed/`` (derived,
analysis-ready) subdirectory:

    data/
      boundaries/      county / state / place shapefiles
      population/      census blocks, block groups, tracts + populations
      centroids/       Census population-weighted centres of population
      street_network/  OpenStreetMap extract + the routable graph
      facilities/      CMS Provider-of-Services + HIFLD hospital data
      _manifest.json   provenance for every raw download

Import from here rather than hard-coding paths, so the layout can change in one
place.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA = PROJECT_ROOT / "data"
RESULTS = PROJECT_ROOT / "results"
FIGURES = RESULTS / "figures"
TABLES = RESULTS / "tables"
DOCS = PROJECT_ROOT / "docs"
PROPOSAL = PROJECT_ROOT / "proposal"

MANIFEST = DATA / "_manifest.json"

# Theme directories -------------------------------------------------------
THEMES = ("boundaries", "population", "centroids", "street_network", "facilities")


def raw(theme: str) -> Path:
    """Path to a theme's raw (as-downloaded) directory."""
    _check(theme)
    return DATA / theme / "raw"


def processed(theme: str) -> Path:
    """Path to a theme's processed (derived) directory."""
    _check(theme)
    return DATA / theme / "processed"


def _check(theme: str) -> None:
    if theme not in THEMES:
        raise KeyError(f"unknown data theme {theme!r}; expected one of {THEMES}")


def ensure_tree() -> None:
    """Create the full data tree plus results/docs directories."""
    for theme in THEMES:
        raw(theme).mkdir(parents=True, exist_ok=True)
        processed(theme).mkdir(parents=True, exist_ok=True)
    for d in (FIGURES, TABLES, DOCS):
        d.mkdir(parents=True, exist_ok=True)


# Convenience handles for the files the pipeline reads and writes ----------
BOUNDARIES_RAW = DATA / "boundaries" / "raw"
POPULATION_RAW = DATA / "population" / "raw"
CENTROIDS_RAW = DATA / "centroids" / "raw"
NETWORK_RAW = DATA / "street_network" / "raw"
FACILITIES_RAW = DATA / "facilities" / "raw"

BOUNDARIES_PROC = DATA / "boundaries" / "processed"
POPULATION_PROC = DATA / "population" / "processed"
CENTROIDS_PROC = DATA / "centroids" / "processed"
NETWORK_PROC = DATA / "street_network" / "processed"
FACILITIES_PROC = DATA / "facilities" / "processed"

# Coordinate reference systems --------------------------------------------
WGS84 = "EPSG:4326"
# NAD83 / Texas Centric Albers Equal Area - equal-area, metre units.
TX_ALBERS = "EPSG:3083"
TX_FIPS = "48"
