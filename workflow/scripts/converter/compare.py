"""Compare a legacy Calliope v0.6 run with a run of the converted v0.7 model.

Inputs (produced by the user, not by this tool), in one folder per case:
    v06.nc                  legacy results (`model.to_netcdf` of the v0.6 run, incl. its run.py math)
    v07.nc                  converted-model results (`model.to_netcdf` of the v0.7 run)

Compared: objective and total cost, capacities per (node, tech), total flows per (node, tech, carrier, direction),
and both summed over nodes. Capacities without a capacity cost are not determined by the optimum: they are marked
"free" and left out of the error figures. Shadow prices are not compared (yet). The optional fixed-design check is
in `converter.fixed_design`. Only xarray/pandas are needed: Calliope is not imported.

Usage:
    python -m converter.compare <case_folder> <output_folder>
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from converter.legacy import V07_NAME, v07_name

RTOL = 1e-3  # relative tolerance for "equal" (solver tolerances, different solvers)
ATOL = 1e-6  # absolute tolerance, model units
TOP = 15  # rows listed per section in the markdown report
# Costs that scale with the capacity: without any of them (weighted in the objective), the optimum does not
# determine the capacity (any value above the peak use costs the same).
FLOW_CAP_COSTS = (
    "cost_flow_cap",
    "cost_flow_cap_per_distance",
    "cost_purchase",
    "cost_purchase_per_distance",
    "cost_om_annual",
)
STORAGE_CAP_COSTS = ("cost_storage_cap", "cost_purchase", "cost_purchase_per_distance")


@dataclass
class Section:
    title: str
    table: pd.DataFrame  # index: keys; columns: v06, v07
    unit: str = ""
    note: str = ""
    free: set = field(default_factory=set)  # index keys not determined by the optimum (no capacity cost)


# --------------------------------------------------------------------------------------------- loading
def load_v06(path: Path) -> xr.Dataset:
    return xr.open_dataset(path)


def load_v07(path: Path) -> tuple[xr.Dataset, xr.Dataset]:
    return xr.open_dataset(path, group="inputs"), xr.open_dataset(path, group="results")


def _node(name: str) -> str:
    """The v0.7 name of a v0.6 node (the converter renames invalid identifiers, e.g. `region1-1` -> `region1_1`)."""
    return name if V07_NAME.match(name) else v07_name(name)


def _split_loc_tech(loc_tech: str, v07_techs: set[str]) -> tuple[str, str]:
    """`R1::pv` -> (R1, pv); transmission `R1::ac:R2` -> (R1, `R1_to_R2_ac` or `R2_to_R1_ac`, as in v0.7)."""
    node, tech = str(loc_tech).split("::", 1)
    node = _node(node)
    if ":" in tech:
        name, remote = tech.split(":", 1)
        remote = _node(remote)
        for candidate in (f"{node}_to_{remote}_{name}", f"{remote}_to_{node}_{name}"):
            if candidate in v07_techs:
                return node, candidate
        return node, f"{node}_to_{remote}_{name}"
    return node, tech


def _split_loc_tech_carrier(item: str, v07_techs: set[str]) -> tuple[str, str, str]:
    loc_tech, carrier = str(item).rsplit("::", 1)
    node, tech = _split_loc_tech(loc_tech, v07_techs)
    return node, tech, carrier


# --------------------------------------------------------------------------------------------- sections
def objective(v06: xr.Dataset, v07_results: xr.Dataset) -> Section:
    rows = {("objective (v0.6 reported)", ""): (float(v06.attrs.get("objective_function_value", np.nan)), np.nan)}
    for cost in sorted(set(map(str, v06.costs.values)) | set(map(str, v07_results.costs.values))):
        a = float(v06.cost.sel(costs=cost).sum()) if cost in v06.costs else np.nan
        b = float(v07_results.cost.sel(costs=cost).sum()) if cost in v07_results.costs else np.nan
        rows[("total cost", cost)] = (a, b)
    table = pd.DataFrame.from_dict(rows, orient="index", columns=["v06", "v07"])
    table.index = pd.MultiIndex.from_tuples(table.index, names=["quantity", "costs"])
    return Section(
        "Objective and total costs",
        table,
        note="v0.7 runs do not store the objective value; "
        "compare the total costs (the objective is the weighted sum of the cost classes).",
    )


def _pairs(flags: xr.DataArray, inputs: xr.Dataset) -> set[tuple[str, str]]:
    """(node, tech) pairs where `flags` (over techs, maybe nodes; other dims reduced with any) is true."""
    extra = [d for d in flags.dims if d not in ("nodes", "techs")]
    if extra:
        flags = flags.any(extra)
    if "techs" not in flags.dims:
        return set()
    if "nodes" not in flags.dims:
        nodes = [str(n) for n in inputs.nodes.values] if "nodes" in inputs.coords else []
        return {(n, str(t)) for t, flag in flags.to_series().items() if flag for n in nodes}
    return {(str(n), str(t)) for (n, t), flag in flags.transpose("nodes", "techs").to_series().items() if flag}


def _priced(inputs: xr.Dataset, params: tuple[str, ...]) -> set[tuple[str, str]]:
    """(node, tech) pairs with a non-zero cost among `params` in a cost class weighted in the objective."""
    weights = inputs.get("objective_cost_weights")
    priced: set[tuple[str, str]] = set()
    for name in params:
        if name not in inputs:
            continue
        data = inputs[name].fillna(0)
        if weights is not None and "costs" in data.dims:
            data = data * weights.reindex(costs=data.costs).fillna(1)
        priced |= _pairs(data != 0, inputs)
    return priced


def _fixed_by_bounds(inputs: xr.Dataset, low: str, high: str) -> set[tuple[str, str]]:
    """(node, tech) pairs whose capacity bounds are equal (min == max): determined without a cost."""
    if low not in inputs or high not in inputs:
        return set()
    return _pairs((inputs[low] == inputs[high]) & inputs[low].notnull(), inputs)


def capacities(v06: xr.Dataset, v07_inputs: xr.Dataset, v07_results: xr.Dataset) -> list[Section]:
    techs07 = set(map(str, v07_results.techs.values))
    priced = _priced(v07_inputs, FLOW_CAP_COSTS) | _fixed_by_bounds(v07_inputs, "flow_cap_min", "flow_cap_max")
    primary = _primary_carriers(v07_inputs, v07_results)
    rows = {}
    for loc_tech, value in v06.energy_cap.to_series().items():
        node, tech = _split_loc_tech(loc_tech, techs07)
        carrier = primary.get(tech)
        b = np.nan
        if carrier is not None and tech in techs07 and node in v07_results.nodes:
            b = float(v07_results.flow_cap.sel(nodes=node, techs=tech, carriers=carrier))
        rows[(node, tech, carrier or "")] = (float(value), b)
    flow = pd.DataFrame.from_dict(rows, orient="index", columns=["v06", "v07"])
    flow.index = pd.MultiIndex.from_tuples(flow.index, names=["nodes", "techs", "carrier (v0.7 flow_cap)"])
    sections = [
        Section(
            "Capacities: v0.6 `energy_cap` vs v0.7 `flow_cap` (primary output carrier)",
            flow,
            note="Transmission links are compared at each end (v0.6 `R1::tech:R2` = v0.7 `R1_to_R2_tech` at R1). "
            "Free: no capacity cost in the objective (and no min == max bound), so the optimum does not determine "
            "the capacity; free rows are left out of the error figures.",
            free={k for k in flow.index if (k[0], k[1]) not in priced},
        )
    ]
    if "storage_cap" in v06 and "storage_cap" in v07_results:
        rows = {}
        for loc_tech, value in v06.storage_cap.to_series().items():
            node, tech = _split_loc_tech(loc_tech, techs07)
            b = np.nan
            if tech in techs07 and node in v07_results.nodes:
                b = float(v07_results.storage_cap.sel(nodes=node, techs=tech))
            rows[(node, tech)] = (float(value), b)
        storage = pd.DataFrame.from_dict(rows, orient="index", columns=["v06", "v07"])
        storage.index = pd.MultiIndex.from_tuples(storage.index, names=["nodes", "techs"])
        priced = _priced(v07_inputs, STORAGE_CAP_COSTS) | _fixed_by_bounds(
            v07_inputs, "storage_cap_min", "storage_cap_max"
        )
        priced |= _fixed_by_bounds(v07_inputs, "flow_cap_min", "flow_cap_max") & _priced(v07_inputs, FLOW_CAP_COSTS)
        sections.append(
            Section(
                "Storage capacities: `storage_cap`",
                storage,
                note="Free: no storage-capacity cost in the objective, so the optimum does not determine it.",
                free={k for k in storage.index if k not in priced},
            )
        )
    return sections


def _primary_carriers(inputs: xr.Dataset, results: xr.Dataset) -> dict[str, str]:
    """Carrier of each v0.7 tech that holds the v0.6 `energy_cap` (primary output, else the single output/input)."""
    out: dict[str, str] = {}
    lookup = inputs.get("primary_carrier_out")
    carrier_out = inputs.get("carrier_out")
    carrier_in = inputs.get("carrier_in")
    for tech in map(str, results.techs.values):
        if lookup is not None and tech in lookup.techs:
            flagged = lookup.sel(techs=tech).fillna(0).astype(bool)
            if bool(flagged.any()):
                out[tech] = str(flagged.carriers.values[flagged.values][0])
                continue
        for candidates in (carrier_out, carrier_in):
            if candidates is None or tech not in candidates.techs:
                continue
            used = candidates.sel(techs=tech).fillna(0).astype(bool)
            used = used.any("nodes") if "nodes" in used.dims else used
            names = [str(c) for c, flag in zip(used.carriers.values, used.values, strict=True) if flag]
            if names:
                out[tech] = names[0]
                break
    return out


def flows(v06: xr.Dataset, v07_results: xr.Dataset) -> Section:
    techs07 = set(map(str, v07_results.techs.values))
    rows = {}
    for var, direction, sign in (("carrier_prod", "out", 1), ("carrier_con", "in", -1)):
        if var not in v06:
            continue
        totals = v06[var].sum("timesteps").to_series()
        for item, value in totals.items():
            node, tech, carrier = _split_loc_tech_carrier(item, techs07)
            rows[(node, tech, carrier, direction)] = [sign * float(value), np.nan]
    for var, direction in (("flow_out", "out"), ("flow_in", "in")):
        totals = v07_results[var].sum("timesteps").to_series().dropna()
        for (node, tech, carrier), value in totals.items():
            rows.setdefault((str(node), str(tech), str(carrier), direction), [np.nan, np.nan])[1] = float(value)
    table = pd.DataFrame.from_dict(rows, orient="index", columns=["v06", "v07"])
    table.index = pd.MultiIndex.from_tuples(table.index, names=["nodes", "techs", "carriers", "direction"])
    table = table[~((table.v06.fillna(0).abs() <= ATOL) & (table.v07.fillna(0).abs() <= ATOL))]
    return Section(
        "Total energy flows (sum over timesteps)",
        table,
        note="v0.6 `carrier_prod` / -`carrier_con` vs v0.7 `flow_out` / `flow_in`; rows that are zero in both are left out.",
    )


SUMMED = "summed over nodes"


def summed_over_nodes(section: Section, title: str) -> Section:
    """The same quantity per tech over all nodes: separates differences from a different regional allocation."""
    keys = [name for name in section.table.index.names if name != "nodes"]
    determined = section.table.drop(index=list(section.free), errors="ignore")
    table = determined.groupby(level=keys).sum(min_count=1)
    return Section(
        f"{title} ({SUMMED})",
        table,
        note="Equal totals with different per-node values are the same solution allocated differently across "
        "regions (often an equal-cost alternative)." + (" Free capacities are left out." if section.free else ""),
    )


def _sanitise(name: str) -> str:
    clean = re.sub(r"\W", "_", str(name)).strip("_").lower()
    return clean or "unnamed"


# --------------------------------------------------------------------------------------------- statistics
def _active(section: Section) -> pd.DataFrame:
    """Determined rows in both runs, non-zero in at least one (rows zero in both carry no information on the error)."""
    both = section.table.drop(index=list(section.free), errors="ignore").dropna()
    return both[np.maximum(both.v06.abs(), both.v07.abs()) > ATOL]


def _errors(section: Section) -> dict:
    """Overall error of a section, on its active rows (free rows excluded).

    weighted error = sum |v07 - v06| / sum |v06| (share of the section's total that differs).
    """
    active = _active(section)
    diff = active.v07 - active.v06
    total = float(active.v06.abs().sum())
    return {
        "active rows": len(active),
        "weighted error": float(diff.abs().sum()) / total if total > 0 else float("nan"),
    }


def _stats(section: Section) -> dict:
    table = section.table
    both = table.dropna()
    diff = (both.v07 - both.v06).abs()
    scale = np.maximum(both.v06.abs(), both.v07.abs())
    equal = diff <= ATOL + RTOL * scale
    return {
        **_errors(section),
        "free": len(section.free),
        "rows": len(table),
        "in both": len(both),
        "only v0.6": int(table.v07.isna().sum()),
        "only v0.7": int(table.v06.isna().sum()),
        "equal (rtol 1e-3)": int(equal.sum()),
        "different": int((~equal).sum()),
        "max |diff|": float(diff.max()) if len(diff) else 0.0,
        "sum v0.6": float(both.v06.sum()),
        "sum v0.7": float(both.v07.sum()),
    }


def _percent(value: float) -> str:
    if np.isnan(value):
        return "n/a"
    return "<0.001%" if 0 < value < 1e-5 else f"{100 * value:.3g}%"


def _with_diff(table: pd.DataFrame) -> pd.DataFrame:
    out = table.copy()
    out["diff"] = out.v07 - out.v06
    scale = np.maximum(out.v06.abs(), out.v07.abs()).replace(0, np.nan)
    out["rel_diff"] = out["diff"] / scale
    return out


# --------------------------------------------------------------------------------------------- report
def compare(case_folder: Path, out_folder: Path) -> dict:
    case_folder, out_folder = Path(case_folder), Path(out_folder)
    out_folder.mkdir(parents=True, exist_ok=True)
    v06 = load_v06(case_folder / "v06.nc")
    v07_inputs, v07_results = load_v07(case_folder / "v07.nc")
    capacity_sections = capacities(v06, v07_inputs, v07_results)
    flow_section = flows(v06, v07_results)
    sections = [
        objective(v06, v07_results),
        *capacity_sections,
        flow_section,
        summed_over_nodes(capacity_sections[0], "Capacities"),
        summed_over_nodes(flow_section, "Total energy flows"),
    ]

    lines = [f"# Comparison v0.6 vs v0.7: `{case_folder.name}`", ""]
    lines += [
        f"Inputs: `{case_folder}`. Equal means |v07 - v06| <= {ATOL} + {RTOL} x max(|v06|, |v07|).",
        "Full tables (with `diff` and `rel_diff`) are in the CSV files next to this report.",
        "",
        "Overall error per section, on rows non-zero in at least one run and determined by the optimum (free "
        "capacities excluded): weighted error = sum |diff| / sum |v06|.",
        "",
        "| section | rows | in both | only v0.6 | only v0.7 | equal | different | free | max abs diff | weighted error |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    summary = {}
    for i, section in enumerate(sections, start=1):
        stats = _stats(section)
        summary[section.title] = stats
        lines.append(
            f"| {section.title} | {stats['rows']} | {stats['in both']} | {stats['only v0.6']} | {stats['only v0.7']} "
            f"| {stats['equal (rtol 1e-3)']} | {stats['different']} | {stats['free']} | {stats['max |diff|']:.6g} "
            f"| {_percent(stats['weighted error'])} |"
        )
        table = _with_diff(section.table)
        if section.free:
            table["free"] = [key in section.free for key in table.index]
        table.to_csv(out_folder / f"{i:02d}_{_sanitise(section.title)[:60]}.csv")
    for section in sections:
        table = _with_diff(section.table)
        lines += ["", f"## {section.title}", ""]
        if section.note:
            lines += [section.note, ""]
        if table.empty:
            lines += ["(no rows)"]
            continue
        if section.free:
            table["free"] = ["yes" if key in section.free else "" for key in table.index]
        worst = table.reindex(table["diff"].abs().sort_values(ascending=False).index).head(TOP)
        lines += [f"Largest differences (top {TOP}):", "", *_markdown(worst)]
    plot(sections, summary, out_folder / "comparison.png", case_folder.name)
    lines[2:2] = ["![Diagnostic summary](comparison.png)", ""]
    (out_folder / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


# --------------------------------------------------------------------------------------------- figure
# Reference data-viz palette (light mode). Categorical slots 1, 2, 3, 7 validated all-pairs (worst CVD dE 9.2); aqua
# is below 3:1 on the surface, so every curve is also named in the legend. Sequential: blue ramp, steps 250-700.
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BAR = "#2a78d6"
SECTION_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#4a3aa7"]  # categorical slots, fixed order
SEQUENTIAL = ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]  # light (small divergence) -> dark (large)
ERROR_FLOOR = 1e-8  # errors below this (relative to the section's largest value) are drawn at the floor
DIVERGENCE_RANGE = (1e-4, 1.0)  # colour scale of the parity plots (relative difference, log)
SHORT_TITLES = {
    "Capacities": "capacities",
    "Storage capacities": "storage capacities",
    "Total energy flows (sum over timesteps)": "total flows",
    f"Capacities ({SUMMED})": f"capacities, {SUMMED}",
    f"Total energy flows ({SUMMED})": f"total flows, {SUMMED}",
}


def plot(sections: list[Section], summary: dict, path: Path, case: str) -> None:
    """2x2 diagnostic summary: error distribution, capacity parity, overall error per section, flow parity."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.size": 9,
            "axes.edgecolor": GRID,
            "axes.labelcolor": INK_2,
            "xtick.color": INK_2,
            "ytick.color": INK_2,
            "text.color": INK,
            "axes.titlecolor": INK,
            "axes.titleweight": "bold",
            "axes.titlesize": 10.5,
        }
    )
    fig, axes = plt.subplots(2, 2, figsize=(15, 12), facecolor=SURFACE)
    for ax in axes.flat:
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    by_title = {s.title: s for s in sections}
    compared = [s for s in sections if not s.title.startswith("Objective")]
    _plot_error_distribution(axes[0, 0], [s for s in compared if SUMMED not in s.title])
    capacity = next(s for s in sections if s.title.startswith("Capacities"))
    _plot_parity(axes[0, 1], capacity, "Capacities: v0.6 energy_cap vs v0.7 flow_cap", "capacity", label_top=4)
    _plot_overall_error(axes[1, 0], compared, by_title["Objective and total costs"].table)
    flows_section = by_title["Total energy flows (sum over timesteps)"]
    _plot_parity(
        axes[1, 1], flows_section, "Total flows: v0.6 carrier_prod/con vs v0.7 flow_out/in", "flow", label_top=4
    )
    fig.suptitle(f"v0.6 vs v0.7 comparison: {case}", x=0.01, ha="left", fontsize=14, fontweight="bold")
    fig.text(
        0.01,
        0.948,
        "Errors on rows non-zero in at least one run, free capacities excluded. "
        "Details: comparison.md and the CSV tables.",
        color=INK_2,
        fontsize=9,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94), h_pad=3, w_pad=4)
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def _title(ax, title: str, legend_rows: int = 0) -> None:
    """Title above a legend of `legend_rows` rows, which sits between the title and the axes."""
    ax.set_title(title, loc="left", pad=8 + 14 * legend_rows)


def _legend(ax, **kwargs) -> None:
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), frameon=False, borderaxespad=0.4, fontsize=8.5, **kwargs)


def _short(title: str) -> str:
    if title in SHORT_TITLES:
        return SHORT_TITLES[title]
    return SHORT_TITLES.get(title.split(":")[0] if title.startswith(("Capacities", "Storage")) else title, title)


def _plot_error_distribution(ax, sections: list[Section]) -> None:
    """Share of rows whose error is at most x, relative to the section's largest value (log x)."""
    drawn = 0
    for color, section in zip(SECTION_COLORS, sections, strict=False):
        active = _active(section)
        if active.empty:
            continue
        scale = float(np.maximum(active.v06.abs(), active.v07.abs()).max())
        error = np.sort(np.clip((active.v07 - active.v06).abs().to_numpy() / scale, ERROR_FLOOR, None))
        share = np.arange(1, len(error) + 1) / len(error)
        stats = _errors(section)
        ax.step(
            error,
            share,
            where="post",
            color=color,
            linewidth=2,
            label=f"{_short(section.title)} (n={len(error)}; weighted error {_percent(stats['weighted error'])})",
        )
        drawn += 1
    _title(ax, "Error distribution: |v0.7 - v0.6| relative to the section's largest value", drawn)
    if not drawn:
        ax.text(0.5, 0.5, "no data", ha="center", va="center", transform=ax.transAxes, color=INK_2)
        return
    from matplotlib.ticker import PercentFormatter

    ax.set_xscale("log")
    ax.set_xlim(ERROR_FLOOR / 2, 2)
    ax.set_ylim(0, 1.02)
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=1, decimals=0))
    for value, label in ((1e-3, "0.1%"), (1e-2, "1%")):
        ax.axvline(value, color=INK_2, linewidth=1, linestyle="--")
        ax.text(value, 0.03, f" {label}", color=INK_2, fontsize=8)
    ax.set_xlabel(f"error / largest value in the section (left edge: <= {ERROR_FLOOR:g}, i.e. identical)")
    ax.set_ylabel("share of rows with at most this error")
    _legend(ax)


def _plot_overall_error(ax, sections: list[Section], costs: pd.DataFrame) -> None:
    """One number per section (weighted error), below the total-cost gaps."""
    rows = []
    for (quantity, cost), row in costs.dropna().iterrows():
        if quantity == "total cost" and row.v06:
            rows.append((f"total cost ({cost})", abs(row.v07 - row.v06) / abs(row.v06)))
    for section in sections:
        rows.append((_short(section.title), _errors(section)["weighted error"]))
    labels = [r[0] for r in rows][::-1]
    values = np.array([r[1] for r in rows], dtype=float)[::-1]
    y = np.arange(len(rows))
    ax.barh(y, np.nan_to_num(values), height=0.6, color=BAR, edgecolor=SURFACE, linewidth=2)
    top = float(np.nanmax(np.concatenate([values, [RTOL]])))
    for yi, value in zip(y, values, strict=True):
        if not np.isnan(value):
            ax.text(value + 0.01 * top, yi, _percent(value), va="center", fontsize=8.5, color=INK)
    from matplotlib.ticker import PercentFormatter

    ax.set_yticks(y, labels)
    ax.set_xlim(0, top * 1.15)
    # enough decimals that ticks stay distinct for tiny errors
    ax.xaxis.set_major_formatter(PercentFormatter(xmax=1, decimals=max(0, int(-np.floor(np.log10(100 * top))) + 1)))
    ax.grid(axis="y", visible=False)
    _title(ax, "Overall error per section (total costs: relative gap)")
    ax.set_xlabel("weighted error = sum |v0.7 - v0.6| / sum |v0.6|")


def _plot_parity(ax, section: Section, title: str, quantity: str, label_top: int) -> None:
    from matplotlib.colors import LinearSegmentedColormap, LogNorm

    data = section.table.dropna()
    free = data[[key in section.free for key in data.index]]
    data = data.drop(index=free.index)
    _title(ax, title, 1 if len(free) else 0)
    if data.empty:
        ax.text(0.5, 0.5, "no data", ha="center", va="center", transform=ax.transAxes, color=INK_2)
        return
    # Divergence from the 1:1 line: |v0.7 - v0.6| / max(|v0.6|, |v0.7|) (0: on the line, 1: one of them is zero).
    scale = np.maximum(data.v06.abs(), data.v07.abs())
    divergence = ((data.v07 - data.v06).abs() / scale.where(scale > ATOL)).fillna(0)
    everything = pd.concat([data, free])
    low = float(min(everything.v06.min(), everything.v07.min()))
    high = float(max(everything.v06.max(), everything.v07.max()))
    pad = 0.04 * ((high - low) or 1)
    ax.plot([low - pad, high + pad], [low - pad, high + pad], color=INK_2, linewidth=1, linestyle="--", zorder=1)
    if len(free):
        ax.scatter(
            free.v06,
            free.v07,
            s=30,
            facecolor="none",
            edgecolor=INK_2,
            linewidth=1,
            zorder=2,
            label=f"free capacity, not determined by the optimum ({len(free)}; not coloured)",
        )
        _legend(ax)
    order = divergence.sort_values().index  # largest divergence drawn on top
    cmap = LinearSegmentedColormap.from_list("divergence", SEQUENTIAL)
    norm = LogNorm(*DIVERGENCE_RANGE, clip=True)
    points = ax.scatter(
        data.v06[order],
        data.v07[order],
        c=np.clip(divergence[order], *DIVERGENCE_RANGE),
        cmap=cmap,
        norm=norm,
        s=30,
        edgecolor=SURFACE,
        linewidth=0.8,
        zorder=2,
    )
    bar = ax.figure.colorbar(points, ax=ax, fraction=0.04, pad=0.02)
    bar.set_label("|v0.7 - v0.6| / max(|v0.6|, |v0.7|)", color=INK_2)
    bar.set_ticks([1e-4, 1e-3, 1e-2, 1e-1, 1], labels=["<=0.01%", "0.1%", "1%", "10%", "100%"])
    bar.outline.set_visible(False)
    gaps = (data.v07 - data.v06).abs()
    gaps = gaps[gaps > ATOL + RTOL * scale.reindex(gaps.index)]  # only real differences are listed
    worst = gaps.sort_values(ascending=False).head(label_top).index
    # Numbered points + a key below the axes: labels never collide or hide data. Points at the same spot share
    # one marker ("1,2"), as do points within 2% of the axis range (labels would overlap).
    key_lines, spots = [], {}
    for number, key in enumerate(worst, start=1):
        x, y = float(data.v06[key]), float(data.v07[key])
        near = next(
            (s for s in spots if abs(s[0] - x) <= 0.02 * (high - low) and abs(s[1] - y) <= 0.02 * (high - low)), None
        )
        spots.setdefault(near or (x, y), []).append(number)
        # flows: (node, tech, carrier, direction) -> "tech (carrier, in) @ node"
        detail = f" ({key[2]}, {key[3]})" if len(key) == 4 else ""
        key_lines.append(f"{number}  {key[1]}{detail} @ {key[0]}: v0.6 {data.v06[key]:.4g}, v0.7 {data.v07[key]:.4g}")
    for (x, y), numbers in spots.items():
        ax.annotate(
            ",".join(map(str, numbers)),
            (x, y),
            xytext=(5, 5),
            textcoords="offset points",
            fontsize=8,
            fontweight="bold",
            color=INK,
        )
    ax.set_xlim(low - pad, high + pad)
    ax.set_ylim(low - pad, high + pad)
    ax.set_xlabel(f"v0.6 {quantity}  (dashed line: v0.7 = v0.6)")
    ax.set_ylabel(f"v0.7 {quantity}")
    if key_lines:
        ax.text(
            0,
            -0.16,
            "\n".join(["Largest gaps:", *key_lines]),
            transform=ax.transAxes,
            va="top",
            fontsize=8,
            color=INK,
            linespacing=1.4,
        )


def _markdown(table: pd.DataFrame) -> list[str]:
    names = [str(n) for n in table.index.names]
    header = names + list(table.columns)
    rows = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for key, values in table.iterrows():
        keys = key if isinstance(key, tuple) else (key,)
        cells = [str(k) for k in keys] + [f"{v:.6g}" if isinstance(v, float) else str(v) for v in values]
        rows.append("| " + " | ".join(cells) + " |")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case_folder", type=Path, help="Folder with v06.nc and v07.nc")
    parser.add_argument("output_folder", type=Path, help="Folder for comparison.md and the CSV tables")
    args = parser.parse_args(argv)
    compare(args.case_folder, args.output_folder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
