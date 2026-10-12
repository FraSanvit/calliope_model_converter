"""Build docs/figures/consistency_cases.png: the v0.6 vs v0.7 consistency checks on the tested cases.

Reads the user-provided runs in `resources/user/<model>/comparison/<case>/` (v06.nc, v07.nc and, for the
fixed-design check, v07_fixed.nc) with the `compare` functions, so the figure can be rebuilt after any change:

    pixi run python docs/figures/make_consistency_figure.py

Layout (2 x 3): total cost gap, error distribution, per node vs summed over nodes; then one parity plot per case
(capacities and flows together, coloured by their divergence from the 1:1 line on one shared scale).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workflow" / "scripts"))

from converter.compare import (  # noqa: E402
    ATOL,
    DIVERGENCE_RANGE,
    ERROR_FLOOR,
    RTOL,
    SEQUENTIAL,
    Section,
    _active,
    _errors,
    _percent,
    capacities,
    flows,
    load_v06,
    load_v07,
    summed_over_nodes,
)

# (label, short name, case folder, categorical hue): fixed order; the hue identifies the case in the top row.
CASES = [
    ("ERS_test_example, year (730 h)", "ERS year", "ERS_test_example/comparison/year_730h", "#2a78d6"),
    ("national_scale (v0.6 example)", "national", "national_scale/comparison/base", "#eb6834"),
    ("urban_scale (v0.6 example)", "urban", "urban_scale/comparison/base", "#1baf7a"),
]
OUT = ROOT / "docs" / "figures" / "consistency_cases.png"
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
FLOOR = 1e-9  # cost gaps below this are drawn at the floor
TICK, NUMBER, TITLE, LEGEND = 13.5, 12, 15.1, 11  # tick labels and numbers +50%, titles +20%


def _mix(color: str, other: str, share: float) -> tuple[float, float, float]:
    from matplotlib.colors import to_rgb

    a, b = np.array(to_rgb(color)), np.array(to_rgb(other))
    return tuple((1 - share) * a + share * b)


def _load(folder: Path) -> dict:
    v06 = load_v06(folder / "v06.nc")
    inputs, results = load_v07(folder / "v07.nc")
    caps = capacities(v06, inputs, results)
    flow = flows(v06, results)
    out = {
        "caps": caps[0],
        "flows": flow,
        "caps_summed": summed_over_nodes(caps[0], "Capacities"),
        "flows_summed": summed_over_nodes(flow, "Total energy flows"),
        "cost_v06": float(v06.cost.sel(costs="monetary").sum()),
        "cost_v07": float(results.cost.sel(costs="monetary").sum()),
        "cost_fixed": np.nan,
    }
    fixed = folder / "v07_fixed.nc"
    if fixed.exists():
        data = xr.open_dataset(fixed, group="results")
        if "cost" in data:
            out["cost_fixed"] = float(data.cost.sel(costs="monetary").sum())
    return out


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def _legend(ax, handles, ncol: int = 1, **kwargs) -> None:
    ax.legend(
        handles=handles,
        loc="lower left",
        bbox_to_anchor=(0, 1.0),
        ncol=ncol,
        frameon=False,
        fontsize=LEGEND,
        borderaxespad=0.4,
        **kwargs,
    )


def _cost_panel(ax, data: dict) -> None:
    """Total cost gap of the v0.7 run (filled) and of the fixed-design run (hollow), on one line per case."""
    from matplotlib.lines import Line2D

    y = np.arange(len(CASES))[::-1]
    for yi, (label, _, _, color) in zip(y, CASES, strict=True):
        d = data[label]
        free = max(abs(d["cost_v07"] - d["cost_v06"]) / abs(d["cost_v06"]), FLOOR)
        ax.scatter(free, yi, s=80, color=color, edgecolor=SURFACE, linewidth=1, zorder=4)
        ax.annotate(
            _percent(free), (free, yi), xytext=(0, 10), textcoords="offset points", ha="center", fontsize=NUMBER
        )
        if not np.isnan(d["cost_fixed"]):
            fixed = max(abs(d["cost_fixed"] - d["cost_v06"]) / abs(d["cost_v06"]), FLOOR)
            ax.scatter(fixed, yi, s=80, facecolor="none", edgecolor=color, linewidth=1.8, zorder=3)
            ax.annotate(
                _percent(fixed), (fixed, yi), xytext=(0, -15), textcoords="offset points", ha="center", fontsize=NUMBER
            )
    ax.axvline(RTOL, color=INK_2, linewidth=1, linestyle="--")
    ax.text(RTOL, len(CASES) - 0.55, " 0.1%", color=INK_2, fontsize=NUMBER)
    ax.set_xscale("log")
    ax.set_xlim(FLOOR / 3, 3e-2)
    ax.set_ylim(-0.6, len(CASES) - 0.4)
    ax.set_yticks(y, [c[0].replace(", ", "\n").replace(" (", "\n(") for c in CASES])
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("|total cost v0.7 - v0.6| / v0.6  (monetary)")
    ax.set_title("A. Total cost gap", loc="left", pad=30)
    _legend(
        ax,
        [
            Line2D([], [], marker="o", linestyle="", color=INK_2, label="v0.7 run"),
            Line2D(
                [],
                [],
                marker="o",
                linestyle="",
                markerfacecolor="none",
                markeredgecolor=INK_2,
                label="v0.7, v0.6 capacities fixed",
            ),
        ],
        ncol=2,
    )


def _distribution_panel(ax, data: dict) -> None:
    """Share of rows within a given error, relative to the section's largest value: capacities solid, flows dashed."""
    from matplotlib.lines import Line2D
    from matplotlib.ticker import PercentFormatter

    for label, _, _, color in CASES:
        for key, style in (("caps", "-"), ("flows", "--")):
            active = _active(data[label][key])
            if active.empty:
                continue
            scale = float(np.maximum(active.v06.abs(), active.v07.abs()).max())
            error = np.sort(np.clip((active.v07 - active.v06).abs().to_numpy() / scale, ERROR_FLOOR, None))
            share = np.arange(1, len(error) + 1) / len(error)
            ax.step(error, share, where="post", color=color, linestyle=style, linewidth=2)
    for value, text in ((1e-3, "0.1%"), (1e-2, "1%")):
        ax.axvline(value, color=INK_2, linewidth=1, linestyle=":")
        ax.text(value, 0.03, f" {text}", color=INK_2, fontsize=NUMBER)
    ax.set_xscale("log")
    ax.set_xlim(ERROR_FLOOR / 2, 2)
    ax.set_ylim(0, 1.02)
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=1, decimals=0))
    ax.set_xlabel(f"error / largest value of its kind (left edge: <= {ERROR_FLOOR:g}, identical)")
    ax.set_ylabel("share of rows with at most this error")
    ax.set_title("B. Error distribution (free capacities excluded)", loc="left", pad=30)
    handles = [Line2D([], [], color=color, linewidth=2, label=short) for _, short, _, color in CASES]
    handles += [
        Line2D([], [], color=INK_2, linewidth=2, linestyle="-", label="capacities"),
        Line2D([], [], color=INK_2, linewidth=2, linestyle="--", label="flows"),
    ]
    _legend(ax, handles, ncol=5, columnspacing=0.8, handlelength=1.4, handletextpad=0.4)


def _node_panel(ax, data: dict) -> None:
    """Weighted error per node (wide, light) and summed over nodes (narrow, dark)."""
    from matplotlib.patches import Patch
    from matplotlib.ticker import PercentFormatter

    rows, y, ticks = [], 0.0, []
    for label, short, _, color in CASES:
        d = data[label]
        for quantity, per_node, summed in (("capacities", "caps", "caps_summed"), ("flows", "flows", "flows_summed")):
            rows.append((y, color, _errors(d[per_node])["weighted error"], _errors(d[summed])["weighted error"]))
            free = f"\n({len(d['caps'].free)}/{len(d['caps'].table)} free)" if quantity == "capacities" else ""
            ticks.append((y, f"{short}\n{quantity}{free}"))
            y += 1
        y += 0.6
    for yi, color, per_node, summed in rows:
        ax.barh(yi, np.nan_to_num(per_node), height=0.7, color=_mix(color, SURFACE, 0.55), edgecolor=SURFACE)
        ax.barh(yi, np.nan_to_num(summed), height=0.32, color=_mix(color, INK, 0.45), edgecolor=SURFACE)
        ax.text(
            max(np.nan_to_num(per_node), np.nan_to_num(summed)),
            yi,
            f"  {_percent(per_node)} / {_percent(summed)}",
            va="center",
            fontsize=NUMBER,
        )
    ax.set_yticks([t[0] for t in ticks], [t[1] for t in ticks], fontsize=TICK)
    ax.invert_yaxis()
    top = max(max(np.nan_to_num(r[2]), np.nan_to_num(r[3])) for r in rows)
    ax.set_xlim(0, top * 1.55)
    ax.xaxis.set_major_formatter(PercentFormatter(xmax=1, decimals=0))
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("weighted error = sum |v0.7 - v0.6| / sum |v0.6|")
    ax.set_title("C. Per node vs summed over nodes", loc="left", pad=30)
    _legend(
        ax,
        [
            Patch(color=_mix(INK_2, SURFACE, 0.55), label="per node"),
            Patch(color=_mix(INK_2, INK, 0.3), label="summed over nodes"),
        ],
        ncol=2,
    )


def _scaled(section: Section) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Determined and free rows, each scaled by the largest value of the section (its kind in this case)."""
    table = section.table.dropna()
    scale = float(np.maximum(table.v06.abs(), table.v07.abs()).max()) or 1.0
    free = table[[key in section.free for key in table.index]] / scale
    return table.drop(index=free.index) / scale, free


def _parity_panel(ax, d: dict, title: str, cmap, norm):
    """Capacities (circles) and flows (triangles) of one case against the 1:1 line, coloured by divergence."""
    points = None
    for key, marker in (("caps", "o"), ("flows", "^")):
        determined, free = _scaled(d[key])
        if len(free):
            ax.scatter(
                free.v06, free.v07, s=30, marker=marker, facecolor="none", edgecolor=INK_2, linewidth=0.9, zorder=2
            )
        scale = np.maximum(determined.v06.abs(), determined.v07.abs())
        divergence = ((determined.v07 - determined.v06).abs() / scale.where(scale > ATOL)).fillna(0)
        order = divergence.sort_values().index  # largest divergence on top
        points = ax.scatter(
            determined.v06[order],
            determined.v07[order],
            c=np.clip(divergence[order], *DIVERGENCE_RANGE),
            cmap=cmap,
            norm=norm,
            s=36,
            marker=marker,
            edgecolor=SURFACE,
            linewidth=0.7,
            zorder=3,
        )
    ax.plot([-0.03, 1.03], [-0.03, 1.03], color=INK_2, linewidth=1, linestyle="--", zorder=1)
    ax.set_xlim(-0.03, 1.03)
    ax.set_ylim(-0.03, 1.03)
    ax.set_aspect("equal")
    ax.set_xlabel("v0.6, relative to the largest value of its kind")
    ax.set_ylabel("v0.7, relative to the largest value of its kind")
    ax.set_title(title, loc="left", pad=10)
    from matplotlib.lines import Line2D

    ax.legend(
        handles=[
            Line2D([], [], marker="o", linestyle="", color=SEQUENTIAL[1], markersize=8, label="capacity"),
            Line2D([], [], marker="^", linestyle="", color=SEQUENTIAL[1], markersize=8, label="total flow"),
            Line2D(
                [],
                [],
                marker="o",
                linestyle="",
                markerfacecolor="none",
                markeredgecolor=INK_2,
                markersize=8,
                label="free capacity (not determined)",
            ),
        ],
        loc="upper left",
        frameon=True,
        facecolor=SURFACE,
        edgecolor=GRID,
        framealpha=0.9,
        fontsize=LEGEND,
    )
    return points


def main() -> int:
    """Build the figure from the comparison runs of the tested cases."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap, LogNorm

    plt.rcParams.update(
        {
            "font.size": 11,
            "xtick.labelsize": TICK,
            "ytick.labelsize": TICK,
            "axes.labelsize": 12,
            "axes.edgecolor": GRID,
            "axes.labelcolor": INK_2,
            "xtick.color": INK_2,
            "ytick.color": INK_2,
            "text.color": INK,
            "axes.titlecolor": INK,
            "axes.titleweight": "bold",
            "axes.titlesize": TITLE,
        }
    )
    data = {label: _load(ROOT / "resources" / "user" / folder) for label, _, folder, _ in CASES}
    fig, axes = plt.subplots(2, 3, figsize=(22, 13.5), facecolor=SURFACE, gridspec_kw={"height_ratios": [1, 1.25]})
    for ax in axes.flat:
        _style(ax)
    _cost_panel(axes[0, 0], data)
    _distribution_panel(axes[0, 1], data)
    _node_panel(axes[0, 2], data)
    cmap = LinearSegmentedColormap.from_list("divergence", SEQUENTIAL)
    norm = LogNorm(*DIVERGENCE_RANGE, clip=True)
    points = None
    for i, (label, _, _, _) in enumerate(CASES):
        points = _parity_panel(axes[1, i], data[label], f"{'DEF'[i]}. {label}", cmap, norm)
    fig.subplots_adjust(left=0.1, right=0.9, top=0.93, bottom=0.06, hspace=0.12, wspace=0.26)
    box = axes[1, 2].get_position()
    bar = fig.colorbar(points, cax=fig.add_axes((0.915, box.y0, 0.009, box.height)))
    bar.set_label("divergence from 1:1: |v0.7 - v0.6| / max(|v0.6|, |v0.7|)", color=INK_2, fontsize=12)
    bar.set_ticks([1e-4, 1e-3, 1e-2, 1e-1, 1], labels=["<=0.01%", "0.1%", "1%", "10%", "100%"])
    bar.outline.set_visible(False)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"written {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
