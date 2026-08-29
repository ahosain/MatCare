#!/usr/bin/env python
"""
08_proposal_figures.py
======================
Build the three figures carried by the Discovery Foundation proposal.

    FIG 1  The access landscape (4-panel "superfigure")
           a. statewide ROAD DISTANCE (miles) to the nearest obstetric facility
           b. population by drive-time band
           c. population by road-distance band
           d. counties with a hospital but no obstetric service

    FIG 2  From diagnosis to decision - the siting optimiser
           a. where the 10 recommended facilities go, over unmet demand
           b. coverage curve: population reached vs number of facilities

    FIG 3  Why the method matters - measured, not assumed
           a. road detour ratio vs straight-line distance
           b. what the corrected facility list changed

Figure 1c needs drive time to NICU-capable facilities, which is not in the
standard pipeline output, so this script runs that one extra multi-source
Dijkstra itself (~10 s).

Colour rules follow the project's data-viz standard: magnitude uses a
single-hue sequential ramp light->dark, categories use fixed hues assigned by
identity, and text always wears ink colours rather than a series colour.

Usage
-----
    python scripts/08_proposal_figures.py
"""

from __future__ import annotations

import geopandas as gpd
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from pyproj import Transformer
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

import paths as P

FIGS = P.FIGURES
TABLES = P.TABLES

# ------------------------------------------------------------------ palette
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#5598e7", "#2a78d6", "#1c5cab", "#104281"]
SEQ_ORANGE = ["#fbdfd0", "#f6bda0", "#f09a72", "#eb6834", "#b94b21", "#8a3616"]
INK, INK_2, INK_MUTED = "#0b0b0b", "#52514e", "#8a8983"
SURFACE, GRID = "#fcfcfb", "#e8e7e2"
NEUTRAL_FILL, NEUTRAL_EDGE = "#ffffff", "#b8b7b1"
C_BLUE, C_ORANGE, C_AQUA, C_YELLOW, C_RED = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e34948")

TIME_BOUNDS = [0, 15, 30, 45, 60, 90, np.inf]
TIME_LABELS = ["< 15", "15–30", "30–45", "45–60", "60–90", "90 +"]
# Distance bands in MILES. Texas planners and the Foundation work in miles, so
# every distance the proposal reports is in miles; the pipeline stores km.
DIST_BOUNDS = [0, 10, 25, 50, 80, 160, np.inf]
DIST_LABELS = ["< 10", "10–25", "25–50", "50–80", "80–160", "160 +"]
KM_PER_MILE = 1.609344

mpl.rcParams.update({
    "figure.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.family": "sans-serif", "axes.edgecolor": NEUTRAL_EDGE,
    "text.color": INK, "pdf.fonttype": 42,
})


def save(fig, stem: str) -> None:
    FIGS.mkdir(parents=True, exist_ok=True)
    for ext, dpi in (("png", 300), ("pdf", 600)):
        p = FIGS / f"{stem}.{ext}"
        fig.savefig(p, dpi=dpi, bbox_inches="tight", facecolor=SURFACE)
        print(f"  wrote {p.relative_to(P.PROJECT_ROOT)}")
    plt.close(fig)


def panel_tag(ax, letter: str, title: str) -> None:
    ax.set_title(f"  {letter}  {title}", loc="left", fontsize=11.5,
                 fontweight="bold", color=INK, pad=8)


def reserve_for_legend(ax, side: str = "bottom", frac: float = 0.16) -> None:
    """
    Expand the axis limits so a legend has guaranteed clear space.

    Texas leaves the lower-left of a map panel empty, which is why legends live
    there - but nudging one upward, or into a corner the state actually reaches,
    puts it on top of the map. Rather than hand-tuning anchors per panel, this
    grows the view box on the chosen side by `frac` of the current span. The
    state is drawn smaller but nothing is ever covered.
    """
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    dx, dy = x1 - x0, y1 - y0
    if side == "bottom":
        ax.set_ylim(y0 - dy * frac, y1)
    elif side == "top":
        ax.set_ylim(y0, y1 + dy * frac)
    elif side == "left":
        ax.set_xlim(x0 - dx * frac, x1)
    elif side == "right":
        ax.set_xlim(x0, x1 + dx * frac)


def nicu_access(acc: pd.DataFrame) -> np.ndarray:
    """Drive time (min) from every block to the nearest NICU-capable facility."""
    print("  computing NICU access (one multi-source Dijkstra) ...", flush=True)
    nodes = pd.read_parquet(P.NETWORK_PROC / "network_nodes.parquet")
    edges = pd.read_parquet(P.NETWORK_PROC / "network_edges.parquet",
                            columns=["u", "v", "time_s"])
    tf = Transformer.from_crs(P.WGS84, P.TX_ALBERS, always_xy=True)
    x, y = tf.transform(nodes["lon"].to_numpy(), nodes["lat"].to_numpy())
    tree = cKDTree(np.column_stack([x, y]))

    fac = gpd.read_parquet(P.FACILITIES_PROC / "facilities_analysis.parquet")
    nic = fac[fac["NICU_ONSITE"]].to_crs(P.TX_ALBERS)
    _, fnode = tree.query(np.column_stack([nic.geometry.x, nic.geometry.y]), k=1)

    gt = csr_matrix(
        (edges["time_s"].to_numpy(np.float64),
         (edges["u"].to_numpy(), edges["v"].to_numpy())),
        shape=(len(nodes), len(nodes)),
    ).T.tocsr()
    t = dijkstra(gt, directed=True, indices=np.unique(fnode), min_only=True)

    blocks = gpd.read_parquet(P.POPULATION_PROC / "block_points.parquet").to_crs(P.TX_ALBERS)
    _, bnode = tree.query(np.column_stack([blocks.geometry.x, blocks.geometry.y]), k=1)
    print(f"  NICU-capable facilities: {len(nic)}")
    return t[bnode] / 60.0


def bg_aggregate(acc: pd.DataFrame, col: str) -> pd.Series:
    """Population-weighted block-group mean of a per-block column."""
    a = acc[np.isfinite(acc[col])].copy()
    a["BG"] = a["GEOID20"].str[:12]
    g = a.groupby("BG").apply(
        lambda d: np.average(d[col], weights=d["POP20"]) if d["POP20"].sum() > 0
        else d[col].mean(),
        include_groups=False,
    )
    return g


def main() -> int:
    P.ensure_tree()
    counties = gpd.read_parquet(P.BOUNDARIES_PROC / "counties.parquet").to_crs(P.TX_ALBERS)
    bg = gpd.read_parquet(P.POPULATION_PROC / "blockgroups.parquet").to_crs(P.TX_ALBERS)
    fac = gpd.read_parquet(P.FACILITIES_PROC / "facilities_analysis.parquet").to_crs(P.TX_ALBERS)
    acc = pd.read_parquet(P.FACILITIES_PROC / "block_access.parquet")
    bands = pd.read_csv(TABLES / "access_distance_bands_mi.csv")
    print(f"Loaded {len(fac)} facilities, {len(acc):,} blocks")

    acc["nicu_min"] = nicu_access(acc)

    bg = bg.merge(bg_aggregate(acc, "net_min").rename("drive_min"),
                  left_on="GEOID", right_index=True, how="left")
    # Panel (a) reports road distance, so the block-group aggregate of net_km
    # is needed as well as the travel-time aggregate used by panel (b).
    bg = bg.merge(bg_aggregate(acc, "net_km").rename("drive_km"),
                  left_on="GEOID", right_index=True, how="left")
    bg["drive_mi"] = bg["drive_km"] / KM_PER_MILE
    bg = bg.merge(bg_aggregate(acc, "nicu_min").rename("nicu_min"),
                  left_on="GEOID", right_index=True, how="left")

    # Panels (a) and (c) report ROAD DISTANCE; panel (b) reports TRAVEL TIME.
    # They therefore get different hues as well as different legends, so the
    # unit change is visible at a glance and the two maps cannot be misread as
    # directly comparable.
    cmap_d = LinearSegmentedColormap.from_list("seq_d", SEQ_ORANGE, N=len(DIST_LABELS))
    norm_d = BoundaryNorm(DIST_BOUNDS, cmap_d.N)
    cmap = LinearSegmentedColormap.from_list("seq", SEQ_BLUE, N=len(TIME_LABELS))
    norm = BoundaryNorm(TIME_BOUNDS, cmap.N)

    # ==================================================== FIG 1 superfigure
    print("\n[FIG 1] access landscape")
    fig = plt.figure(figsize=(16, 12.6))
    gs = fig.add_gridspec(2, 2, height_ratios=[1.30, 1], hspace=0.10, wspace=0.04)

    # -- (a) statewide drive time -----------------------------------------
    ax = fig.add_subplot(gs[0, 0])
    bg.plot(ax=ax, column="drive_mi", cmap=cmap_d, norm=norm_d, linewidth=0,
            rasterized=True)
    counties.boundary.plot(ax=ax, color=NEUTRAL_EDGE, linewidth=0.25)
    fac.plot(ax=ax, marker="^", color=INK, markersize=9, edgecolor="white", linewidth=0.3)
    ax.set_axis_off()
    panel_tag(ax, "a", "Road distance to nearest obstetric facility")
    reserve_for_legend(ax, "bottom", 0.10)
    ax.legend(
        handles=[Patch(facecolor=cmap_d(i), label=DIST_LABELS[i]) for i in range(len(DIST_LABELS))]
        + [Line2D([0], [0], marker="^", color="none", markerfacecolor=INK,
                  markeredgecolor="white", markersize=7,
                  label=f"Obstetric facility (n={len(fac)})")],
        loc="lower left", fontsize=8, frameon=True, framealpha=0.95,
        title="miles", title_fontsize=8.5,
    )

    # -- (b) NICU-capable access ------------------------------------------
    ax = fig.add_subplot(gs[0, 1])
    nic = fac[fac["NICU_ONSITE"]]
    bg.plot(ax=ax, column="nicu_min", cmap=cmap, norm=norm, linewidth=0, rasterized=True)
    counties.boundary.plot(ax=ax, color=NEUTRAL_EDGE, linewidth=0.25)
    nic.plot(ax=ax, marker="^", color=INK, markersize=9, edgecolor="white", linewidth=0.3)
    ax.set_axis_off()
    panel_tag(ax, "b", "Drive time to nearest NICU-capable facility")
    reserve_for_legend(ax, "bottom", 0.10)
    ax.legend(
        handles=[Patch(facecolor=cmap(i), label=TIME_LABELS[i]) for i in range(len(TIME_LABELS))]
        + [Line2D([0], [0], marker="^", color="none", markerfacecolor=INK,
                  markeredgecolor="white", markersize=7,
                  label=f"NICU-capable (n={len(nic)})")],
        loc="lower left", fontsize=8, frameon=True, framealpha=0.95,
        title="minutes", title_fontsize=8.5,
    )

    # -- (c) population by band -------------------------------------------
    ax = fig.add_subplot(gs[1, 0])
    bars = ax.bar(bands["band_mi"], bands["population"] / 1e6, width=0.66,
                  color=[cmap_d(i) for i in range(len(bands))])
    for b, pct, pm in zip(bars, bands["pct_of_state"], bands["population"] / 1e6):
        ax.annotate(f"{pm:.2f}M\n{pct:.1f}%",
                    xy=(b.get_x() + b.get_width() / 2, b.get_height()),
                    xytext=(0, 4), textcoords="offset points", ha="center",
                    va="bottom", fontsize=8, color=INK_2)
    ax.set_xlabel("Road distance to nearest obstetric facility (miles)",
                  fontsize=9.5, color=INK_2)
    ax.set_ylabel("Texas population (millions)", fontsize=9.5, color=INK_2)
    ax.set_ylim(0, (bands["population"].max() / 1e6) * 1.2)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_2, labelsize=8.5)
    panel_tag(ax, "c", "Where Texans live relative to obstetric care")

    # -- (d) hospital but no obstetric unit --------------------------------
    ax = fig.add_subplot(gs[1, 1])
    val = pd.read_csv(TABLES / "facility_validation_by_county.csv", dtype={"COUNTYFP5": str})
    c5 = counties.merge(val[["COUNTYFP5", "osm_hospitals", "matched_facilities", "population"]],
                        on="COUNTYFP5", how="left")
    has_ob = c5["matched_facilities"].fillna(0) > 0
    hosp_no_ob = (~has_ob) & (c5["osm_hospitals"].fillna(0) >= 1)
    neither = (~has_ob) & (~hosp_no_ob)
    c5[has_ob].plot(ax=ax, color=NEUTRAL_FILL, edgecolor=NEUTRAL_EDGE, linewidth=0.3)
    c5[neither].plot(ax=ax, color=C_RED, alpha=0.35, edgecolor="#a32e2d", linewidth=0.4)
    c5[hosp_no_ob].plot(ax=ax, color=C_YELLOW, alpha=0.85, edgecolor="#9c6b00", linewidth=0.5)
    ax.set_axis_off()
    panel_tag(ax, "d", "Counties with a hospital but no obstetric unit")
    reserve_for_legend(ax, "top", 0.17)
    pop_hosp_no_ob = int(c5.loc[hosp_no_ob, "population"].sum())
    ax.legend(
        handles=[
            Patch(facecolor=C_YELLOW, alpha=0.85, edgecolor="#9c6b00",
                  label=f"Hospital, no obstetric unit — {int(hosp_no_ob.sum())} counties, "
                        f"{pop_hosp_no_ob / 1e6:.2f}M people"),
            Patch(facecolor=C_RED, alpha=0.35, edgecolor="#a32e2d",
                  label=f"No hospital at all ({int(neither.sum())} counties)"),
            Patch(facecolor=NEUTRAL_FILL, edgecolor=NEUTRAL_EDGE,
                  label=f"Obstetric unit present ({int(has_ob.sum())} counties)"),
        ],
        loc="upper right", fontsize=8, frameon=True, framealpha=0.95)

    save(fig, "proposal_fig1_access_landscape")

    # ================================================== FIG 2 the optimiser
    print("\n[FIG 2] siting optimiser")
    sit = gpd.read_parquet(P.FACILITIES_PROC / "siting.parquet").to_crs(P.TX_ALBERS)
    curve = pd.read_csv(TABLES / "siting_marginal_gain.csv")

    fig = plt.figure(figsize=(16, 7.6))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.5, 1], wspace=0.12)

    ax = fig.add_subplot(gs[0, 0])
    und = bg[bg["drive_min"] > 30]
    counties.plot(ax=ax, color=NEUTRAL_FILL, edgecolor=NEUTRAL_EDGE, linewidth=0.3)
    und.plot(ax=ax, color=C_ORANGE, alpha=0.55, linewidth=0, rasterized=True)
    fac.plot(ax=ax, marker="^", color=INK_MUTED, markersize=7, linewidth=0)
    sit.plot(ax=ax, marker="*", color=C_BLUE, markersize=420,
             edgecolor="white", linewidth=1.1, zorder=6)
    for _, r in sit.iterrows():
        ax.annotate(f"{r['rank']}. {r['place']}",
                    xy=(r.geometry.x, r.geometry.y), xytext=(9, 5),
                    textcoords="offset points", fontsize=8.5, fontweight="bold",
                    color=INK, zorder=7)
    ax.set_axis_off()
    panel_tag(ax, "a", "Ten highest-impact sites for a new obstetric facility")
    ax.legend(handles=[
        Line2D([0], [0], marker="*", color="none", markerfacecolor=C_BLUE,
               markeredgecolor="white", markersize=17, label="Recommended new facility"),
        Patch(facecolor=C_ORANGE, alpha=0.55, label="Women 15-44 currently > 30 min from care"),
        Line2D([0], [0], marker="^", color="none", markerfacecolor=INK_MUTED,
               markersize=7, label="Existing obstetric facility"),
    ], loc="lower left", fontsize=8.5, frameon=True, framealpha=0.95)

    ax = fig.add_subplot(gs[0, 1])
    xs = np.concatenate([[0], curve["n_sites"].to_numpy()])
    ys = np.concatenate([[0], curve["pct_of_underserved"].to_numpy()])
    ax.plot(xs, ys, color=C_BLUE, linewidth=2.2, marker="o", markersize=6,
            markeredgecolor="white", markeredgewidth=1.1)
    ax.fill_between(xs, ys, color=C_BLUE, alpha=0.10)
    for _, r in curve.iterrows():
        if int(r["n_sites"]) in (1, 5, 10):
            ax.annotate(f"{r['pct_of_underserved']:.0f}%",
                        xy=(r["n_sites"], r["pct_of_underserved"]),
                        xytext=(6, -12), textcoords="offset points",
                        fontsize=9, color=INK_2, fontweight="bold")
    ax.set_xlabel("Number of new facilities sited", fontsize=9.5, color=INK_2)
    ax.set_ylabel("Underserved women 15–44 brought within 30 min (%)",
                  fontsize=9.5, color=INK_2)
    ax.set_xlim(0, curve["n_sites"].max() + 0.4)
    ax.set_ylim(0, max(ys) * 1.22)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_2, labelsize=8.5)
    panel_tag(ax, "b", "Diminishing returns guide the budget")
    total_under = int(round(curve["cum_pop_covered"].iloc[-1]
                            / (curve["pct_of_underserved"].iloc[-1] / 100)))
    ax.annotate(
        f"{curve['cum_pop_covered'].iloc[-1]:,} of {total_under:,}\n"
        f"underserved women aged 15–44 covered\nby {int(curve['n_sites'].iloc[-1])} new facilities",
        xy=(0.97, 0.06), xycoords="axes fraction", ha="right", va="bottom",
        fontsize=9, color=INK_2,
        bbox=dict(boxstyle="round,pad=0.5", facecolor="white",
                  edgecolor=GRID, linewidth=1),
    )
    save(fig, "proposal_fig2_siting_optimizer")

    # ============================================= FIG 3 method validation
    print("\n[FIG 3] method validation")
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.4))

    # -- (a) detour ratio ---------------------------------------------------
    ax = axes[0]
    r = acc[acc["reachable"] & np.isfinite(acc["detour_ratio"]) & (acc["POP20"] > 0)]
    ratio = r["detour_ratio"].clip(1, 3).to_numpy()
    w = r["POP20"].to_numpy()
    ax.hist(ratio, bins=60, weights=w / w.sum() * 100, color=C_BLUE, alpha=0.85)
    mean_r = float(np.average(r["detour_ratio"].clip(1, 5), weights=w))
    ax.axvline(mean_r, color=C_ORANGE, linewidth=2.2, linestyle="--")
    ax.annotate(f"population-weighted mean {mean_r:.2f}×",
                xy=(mean_r, ax.get_ylim()[1] * 0.86), xytext=(10, 0),
                textcoords="offset points", fontsize=9.5, color=C_ORANGE,
                fontweight="bold")
    ax.set_xlabel("Road distance ÷ straight-line distance", fontsize=9.5, color=INK_2)
    ax.set_ylabel("% of Texas population", fontsize=9.5, color=INK_2)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_2, labelsize=8.5)
    panel_tag(ax, "a", "Straight-line distance understates travel")

    # -- (b) what the corrected list changed --------------------------------
    ax = axes[1]
    labels = ["Facilities\nidentified", "Counties\ncovered",
              "Texans > 30 min\nfrom care (100k)"]
    old = [170, 101, 25.8]
    new = [211, 103, 10.6]  # 2,581,427 -> 1,059,501, shown in units of 100k
    xpos = np.arange(len(labels))
    ax.bar(xpos - 0.2, old, width=0.38, color=INK_MUTED, label="Exact-name join (previous)")
    ax.bar(xpos + 0.2, new, width=0.38, color=C_BLUE, label="Multi-stage match (this work)")
    for x, (o, n) in enumerate(zip(old, new)):
        ax.annotate(f"{o:g}", xy=(x - 0.2, o), xytext=(0, 3), textcoords="offset points",
                    ha="center", fontsize=9, color=INK_2)
        ax.annotate(f"{n:g}", xy=(x + 0.2, n), xytext=(0, 3), textcoords="offset points",
                    ha="center", fontsize=9, color=INK, fontweight="bold")
    ax.set_xticks(xpos)
    ax.set_xticklabels(labels, fontsize=9, color=INK_2)
    ax.set_ylabel("Count", fontsize=9.5, color=INK_2)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_2, labelsize=8.5)
    ax.legend(fontsize=8.5, frameon=True, framealpha=0.95, loc="upper right")
    panel_tag(ax, "b", "Facility-list correction changes the answer")

    plt.tight_layout()
    save(fig, "proposal_fig3_method_validation")

    # ------------------------------------------------------------- summary
    pop = acc.loc[acc["reachable"] & (acc["POP20"] > 0), "POP20"].to_numpy()
    nm = acc.loc[acc["reachable"] & (acc["POP20"] > 0), "nicu_min"].to_numpy()
    ok = np.isfinite(nm)
    print("\n--- NICU access, population weighted ---")
    print(f"  mean {np.average(nm[ok], weights=pop[ok]):.1f} min")
    for t in (30, 60):
        share = pop[ok][nm[ok] > t].sum() / pop[ok].sum() * 100
        print(f"  > {t} min from a NICU-capable facility: "
              f"{int(pop[ok][nm[ok] > t].sum()):,} ({share:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
