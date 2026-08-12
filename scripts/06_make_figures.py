#!/usr/bin/env python
"""
06_make_figures.py
==================
Render every figure for the MatCare proposal. Each figure is written to
``results/figures/`` as both a 300 dpi PNG and a 600 dpi vector-friendly PDF.

Figures
-------
fig1_maternity_care_deserts   counties with no obstetric facility (rebuilt from
                              scratch - the notebook cells that created
                              ``tx_counties`` / ``gdf_fac`` / ``HasFacility``
                              were lost, so the map was not reproducible)
fig2_drive_time               block-group choropleth, drive time to nearest
                              obstetric facility
fig3_drive_distance           block-group choropleth, drive distance
fig4_population_by_drivetime  population living in each drive-time band
fig5_facility_validation      counties flagged as deserts that nonetheless have
                              hospitals mapped in OpenStreetMap

Colour decisions
----------------
* Drive time and drive distance encode **magnitude**, so each uses a single-hue
  sequential ramp, light -> dark, with the lightest step meaning "closest".
  No rainbow ramps: they introduce false category boundaries and are not
  colourblind-safe.
* The desert map encodes a **binary category**, so it uses one saturated fill
  against a neutral, matching the convention in the maternity-access literature.
* Text always uses ink colours, never a series colour.

Usage
-----
    python scripts/06_make_figures.py
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

import paths as P

PROJECT_ROOT = P.PROJECT_ROOT
FIGS = P.FIGURES
TABLES = P.TABLES

TX_ALBERS = "EPSG:3083"

# ---------------------------------------------------------------- palette ---
# Sequential blue ramp (magnitude), light -> dark.
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#5598e7", "#2a78d6", "#1c5cab", "#104281"]
# Second sequential context takes the orange hue as its own one-hue ramp.
SEQ_ORANGE = ["#fbdfd0", "#f6bda0", "#f09a72", "#eb6834", "#b94b21", "#8a3616"]

INK = "#0b0b0b"
INK_2 = "#52514e"
INK_MUTED = "#8a8983"
SURFACE = "#fcfcfb"
NEUTRAL_FILL = "#ffffff"
NEUTRAL_EDGE = "#b8b7b1"
DESERT_RED = "#e34948"
FACILITY_BLUE = "#2a78d6"

TIME_BOUNDS = [0, 15, 30, 45, 60, 90, np.inf]
TIME_LABELS = ["< 15", "15 – 30", "30 – 45", "45 – 60", "60 – 90", "90 +"]
DIST_BOUNDS = [0, 10, 25, 50, 80, 160, np.inf]
DIST_LABELS = ["< 10", "10 – 25", "25 – 50", "50 – 80", "80 – 160", "160 +"]

# Major Texas cities for orientation labels.
MAJOR_CITIES = {
    "Houston": (-95.3698, 29.7604),
    "San Antonio": (-98.4936, 29.4241),
    "Dallas": (-96.7970, 32.7767),
    "Austin": (-97.7431, 30.2672),
    "El Paso": (-106.4850, 31.7619),
    "Corpus Christi": (-97.3964, 27.8006),
    "Lubbock": (-101.8552, 33.5779),
    "Amarillo": (-101.8313, 35.2220),
    "Beaumont": (-94.1266, 30.0802),
}

mpl.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "font.family": "sans-serif",
        "axes.edgecolor": NEUTRAL_EDGE,
        "text.color": INK,
        "pdf.fonttype": 42,  # embed TrueType so text stays selectable
    }
)


def save(fig: plt.Figure, stem: str) -> None:
    FIGS.mkdir(parents=True, exist_ok=True)
    for ext, dpi in (("png", 300), ("pdf", 600)):
        path = FIGS / f"{stem}.{ext}"
        fig.savefig(path, dpi=dpi, bbox_inches="tight", facecolor=SURFACE)
        print(f"  wrote {path.relative_to(PROJECT_ROOT)}")
    plt.close(fig)


def city_layer(ax, crs: str) -> None:
    """Plot and label the orientation cities."""
    cdf = gpd.GeoDataFrame(
        {"CITY": list(MAJOR_CITIES)},
        geometry=gpd.points_from_xy(
            [c[0] for c in MAJOR_CITIES.values()],
            [c[1] for c in MAJOR_CITIES.values()],
        ),
        crs="EPSG:4326",
    ).to_crs(crs)
    cdf.plot(ax=ax, marker="o", color=INK, edgecolor="white", markersize=26, zorder=6)
    for _, r in cdf.iterrows():
        ax.annotate(
            r["CITY"],
            xy=(r.geometry.x, r.geometry.y),
            xytext=(5, 4),
            textcoords="offset points",
            fontsize=8,
            fontweight="bold",
            color=INK,
            zorder=7,
            path_effects=None,
        )


def load_layers():
    counties = gpd.read_parquet(P.BOUNDARIES_PROC / "counties.parquet").to_crs(TX_ALBERS)
    fac = gpd.read_parquet(P.FACILITIES_PROC / "facilities_analysis.parquet").to_crs(TX_ALBERS)
    bg = gpd.read_parquet(P.POPULATION_PROC / "blockgroups.parquet").to_crs(TX_ALBERS)
    acc = pd.read_parquet(P.FACILITIES_PROC / "block_access.parquet")
    return counties, fac, bg, acc


def bg_access(acc: pd.DataFrame) -> pd.DataFrame:
    """Aggregate block results to block groups, population-weighted."""
    a = acc[acc["reachable"] & np.isfinite(acc["net_min"])].copy()
    a["BG"] = a["GEOID20"].str[:12]
    a["_t"] = a["net_min"] * a["POP20"]
    a["_d"] = a["net_km"] * a["POP20"]
    g = a.groupby("BG").agg(
        pop=("POP20", "sum"), t=("_t", "sum"), d=("_d", "sum"),
        t_unw=("net_min", "mean"), d_unw=("net_km", "mean"),
    )
    # Unpopulated block groups fall back to the unweighted block mean so the
    # map has no holes; they carry no weight in any statistic.
    g["drive_min"] = np.where(g["pop"] > 0, g["t"] / g["pop"].replace(0, np.nan), g["t_unw"])
    g["drive_km"] = np.where(g["pop"] > 0, g["d"] / g["pop"].replace(0, np.nan), g["d_unw"])
    return g[["pop", "drive_min", "drive_km"]]


def choropleth(bg, counties, fac, column, bounds, labels, ramp, title, unit, stem):
    cmap = LinearSegmentedColormap.from_list("seq", ramp, N=len(labels))
    norm = BoundaryNorm(bounds, cmap.N)

    fig, ax = plt.subplots(figsize=(15, 13))
    # rasterized=True keeps the 18,638 polygon fills as a 600 dpi raster inside
    # the PDF instead of 18,638 vector paths. Visually identical at print size,
    # ~40 MB -> ~2 MB. Text, legend and axes stay vector.
    bg.plot(ax=ax, column=column, cmap=cmap, norm=norm, linewidth=0, zorder=1,
            rasterized=True)
    # Missing data (block groups with no reachable block) stay visibly blank.
    miss = bg[bg[column].isna()]
    if len(miss):
        miss.plot(ax=ax, color="#e8e7e2", hatch="///", edgecolor="white",
                  linewidth=0.2, zorder=1, rasterized=True)
    counties.boundary.plot(ax=ax, color=NEUTRAL_EDGE, linewidth=0.35, zorder=2)
    fac.plot(ax=ax, marker="^", color=INK, markersize=16, edgecolor="white",
             linewidth=0.4, zorder=5)
    city_layer(ax, TX_ALBERS)

    handles = [
        Patch(facecolor=cmap(i), edgecolor="none", label=labels[i])
        for i in range(len(labels))
    ]
    if len(miss):
        handles.append(Patch(facecolor="#e8e7e2", hatch="///", edgecolor="white",
                             label="no routable population"))
    handles.append(
        Line2D([0], [0], marker="^", color="none", markerfacecolor=INK,
               markeredgecolor="white", markersize=8, label="Obstetric facility")
    )
    leg = ax.legend(handles=handles, loc="lower left", fontsize=9, frameon=True,
                    framealpha=0.96, title=f"{title}\n({unit})", title_fontsize=10)
    leg.get_title().set_color(INK)
    leg._legend_box.align = "left"

    ax.set_axis_off()
    plt.tight_layout()
    save(fig, stem)


def main() -> int:
    counties, fac, bg, acc = load_layers()
    print(f"Loaded {len(counties)} counties, {len(fac)} facilities, "
          f"{len(bg):,} block groups, {len(acc):,} blocks")

    g = bg_access(acc)
    bg = bg.merge(g, left_on="GEOID", right_index=True, how="left")

    # ---------------------------------------------------------------- fig 1
    # Rebuild the desert classification the lost notebook cells performed.
    print("\n[fig1] maternity care deserts")
    fac_counties = set(fac["COUNTYFIPS"].astype(str))
    counties["HasFacility"] = counties["COUNTYFP5"].isin(fac_counties)
    n_desert = int((~counties["HasFacility"]).sum())

    fig, ax = plt.subplots(figsize=(15, 13))
    counties[counties["HasFacility"]].plot(
        ax=ax, color=NEUTRAL_FILL, edgecolor=NEUTRAL_EDGE, linewidth=0.45)
    counties[~counties["HasFacility"]].plot(
        ax=ax, color=DESERT_RED, alpha=0.45, edgecolor="#a32e2d", linewidth=0.6)
    for name, pt in zip(counties["COUNTY_NAME"], counties.geometry.representative_point()):
        ax.annotate(name, xy=(pt.x, pt.y), ha="center", va="center",
                    fontsize=4.5, color=INK_2, zorder=3)
    fac.plot(ax=ax, marker="^", color=FACILITY_BLUE, edgecolor="white",
             linewidth=0.4, markersize=28, zorder=4)
    city_layer(ax, TX_ALBERS)

    ax.legend(
        handles=[
            Patch(facecolor=DESERT_RED, alpha=0.45, edgecolor="#a32e2d",
                  label=f"No obstetric facility in county (n = {n_desert})"),
            Patch(facecolor=NEUTRAL_FILL, edgecolor=NEUTRAL_EDGE,
                  label=f"County with obstetric facility (n = {254 - n_desert})"),
            Line2D([0], [0], marker="^", color="none", markerfacecolor=FACILITY_BLUE,
                   markeredgecolor="white", markersize=9,
                   label=f"Obstetric facility (n = {len(fac)})"),
            Line2D([0], [0], marker="o", color="none", markerfacecolor=INK,
                   markeredgecolor="white", markersize=7, label="Major city"),
        ],
        loc="upper left", fontsize=9, frameon=True, framealpha=0.95,
    )
    ax.set_axis_off()
    plt.tight_layout()
    save(fig, "fig1_maternity_care_deserts")

    # ---------------------------------------------------------------- fig 2
    print("\n[fig2] drive time to nearest obstetric facility")
    choropleth(bg, counties, fac, "drive_min", TIME_BOUNDS, TIME_LABELS, SEQ_BLUE,
               "Drive time to nearest\nobstetric facility", "minutes",
               "fig2_drive_time")

    # ---------------------------------------------------------------- fig 3
    print("\n[fig3] drive distance to nearest obstetric facility")
    choropleth(bg, counties, fac, "drive_km", DIST_BOUNDS, DIST_LABELS, SEQ_ORANGE,
               "Road distance to nearest\nobstetric facility", "kilometres",
               "fig3_drive_distance")

    # ---------------------------------------------------------------- fig 4
    print("\n[fig4] population by drive-time band")
    bands = pd.read_csv(TABLES / "access_time_bands.csv")
    fig, ax = plt.subplots(figsize=(9, 5.2))
    cmap = LinearSegmentedColormap.from_list("seq", SEQ_BLUE, N=len(bands))
    bars = ax.bar(
        bands["band_min"], bands["population"] / 1e6,
        color=[cmap(i) for i in range(len(bands))], width=0.68,
    )
    for b, pct, popm in zip(bars, bands["pct_of_state"], bands["population"] / 1e6):
        ax.annotate(f"{popm:,.2f} M\n{pct:.1f}%", xy=(b.get_x() + b.get_width() / 2,
                    b.get_height()), xytext=(0, 4), textcoords="offset points",
                    ha="center", va="bottom", fontsize=8.5, color=INK_2)
    ax.set_xlabel("Drive time to nearest obstetric facility (minutes)",
                  fontsize=10, color=INK_2)
    ax.set_ylabel("Texas population (millions)", fontsize=10, color=INK_2)
    ax.set_ylim(0, (bands["population"].max() / 1e6) * 1.18)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#e8e7e2", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_2, labelsize=9)
    plt.tight_layout()
    save(fig, "fig4_population_by_drivetime")

    # ---------------------------------------------------------------- fig 5
    print("\n[fig5] facility-list validation")
    val = pd.read_csv(TABLES / "facility_validation_by_county.csv",
                      dtype={"COUNTYFP5": str}).set_index("COUNTYFP5")
    c5 = counties.merge(val[["osm_hospitals", "matched_facilities", "population"]],
                        left_on="COUNTYFP5", right_index=True, how="left")
    suspect = (c5["matched_facilities"].fillna(0) == 0) & (c5["osm_hospitals"].fillna(0) >= 1)
    confirmed = c5["matched_facilities"].fillna(0) > 0
    true_desert = ~suspect & ~confirmed

    fig, ax = plt.subplots(figsize=(15, 13))
    c5[confirmed].plot(ax=ax, color=NEUTRAL_FILL, edgecolor=NEUTRAL_EDGE, linewidth=0.45)
    c5[true_desert].plot(ax=ax, color=DESERT_RED, alpha=0.40,
                         edgecolor="#a32e2d", linewidth=0.6)
    c5[suspect].plot(ax=ax, color="#eda100", alpha=0.75,
                     edgecolor="#9c6b00", linewidth=0.8, hatch="\\\\\\")
    for name, pt in zip(c5["COUNTY_NAME"], c5.geometry.representative_point()):
        ax.annotate(name, xy=(pt.x, pt.y), ha="center", va="center",
                    fontsize=4.5, color=INK_2, zorder=3)
    city_layer(ax, TX_ALBERS)
    ax.legend(
        handles=[
            Patch(facecolor="#eda100", alpha=0.75, edgecolor="#9c6b00", hatch="\\\\\\",
                  label=f"Flagged desert BUT hospitals mapped in OSM (n = {int(suspect.sum())})"),
            Patch(facecolor=DESERT_RED, alpha=0.40, edgecolor="#a32e2d",
                  label=f"No facility, no OSM hospital (n = {int(true_desert.sum())})"),
            Patch(facecolor=NEUTRAL_FILL, edgecolor=NEUTRAL_EDGE,
                  label=f"Has matched obstetric facility (n = {int(confirmed.sum())})"),
        ],
        loc="upper left", fontsize=9, frameon=True, framealpha=0.95,
        title="Facility-list validation", title_fontsize=10,
    )
    ax.set_axis_off()
    plt.tight_layout()
    save(fig, "fig5_facility_validation")

    print("\nAll figures written to results/figures/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
