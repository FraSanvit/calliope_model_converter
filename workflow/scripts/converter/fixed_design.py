"""Fixed-design check: is the v0.6 solution feasible and equally cheap in the converted v0.7 model?

Optional and separate from `compare` (it needs one more solve per scenario, which large models or models with annual
constraints may not afford). In one case folder (`resources/user/<model>/comparison/<case>/`):

1. `write_override` reads `v06.nc` and `v07.nc` and writes `fix_v06_design.yaml`: a v0.7 override fixing every
   capacity of the converted model to the v0.6 solution (relative slack 1e-6).
2. The user solves the converted model with the same scenario plus this override (merged after the scenario's
   overrides) and saves the results as `v07_fixed.nc` in the case folder.
3. `check` reads `v07_fixed.nc` and writes `fixed_design.md`: a verdict and the costs per tech against v0.6.

Verdicts: EQUIVALENT (the v0.6 solution is also optimal in v0.7: remaining differences are equal-cost alternatives),
COSTLIER / CHEAPER (costs or constraints differ: the costs per tech show where), INFEASIBLE (a constraint differs).

Usage:
    python -m converter.fixed_design <case_folder> <output_folder>
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from converter.compare import (
    ATOL,
    RTOL,
    Section,
    _markdown,
    _pairs,
    _split_loc_tech,
    _with_diff,
    capacities,
    load_v06,
    load_v07,
)

FIX_RTOL, FIX_ATOL = 1e-6, 1e-9  # slack of the fixed capacities (solver round-off must not make them infeasible)
FIX_FILE = "fix_v06_design.yaml"
FIXED_RUN = "v07_fixed.nc"
REPORT = "fixed_design.md"


def write_override(capacity_sections: list[Section], inputs: xr.Dataset, path: Path) -> None:
    """A v0.7 override fixing every capacity of the converted model to the v0.6 solution.

    Solve the converted model (same scenario) with it and save the results as `v07_fixed.nc` in the case folder.
    """
    from ruamel.yaml import YAML

    links = set()
    if "base_tech" in inputs:
        base = inputs.base_tech.to_series()
        links = {str(t) for t, value in base.items() if str(value) == "transmission"}

    # Multi-carrier (conversion) techs: the capacity is indexed on its carrier, as the converter writes it; other
    # techs get plain values (Calliope cannot merge an indexed value onto a plain one set by a template).
    carrier_sets: dict[str, set[str]] = {}  # distinct carriers (storage has the same one in and out)
    for name in ("carrier_in", "carrier_out"):
        if name in inputs:
            used = inputs[name].fillna(0).astype(bool)
            extra = [d for d in used.dims if d not in ("techs", "carriers")]
            used = used.any(extra) if extra else used
            for (tech, carrier), flag in used.transpose("techs", "carriers").to_series().items():
                if flag:
                    carrier_sets.setdefault(str(tech), set()).add(str(carrier))
    carriers_used = {tech: len(found) for tech, found in carrier_sets.items()}

    def bounds(value: float) -> tuple[float, float]:
        value = max(value, 0.0)  # capacities are non-negative (v0.6 reports round-off like -6e-9)
        return max(value * (1 - FIX_RTOL) - FIX_ATOL, 0.0), value * (1 + FIX_RTOL) + FIX_ATOL

    def value(tech: str, number: float, carrier: str):
        return {"data": number, "index": carrier, "dims": "carriers"} if carriers_used.get(tech, 0) > 1 else number

    nodes: dict = {}
    techs: dict = {}
    link_caps: dict[str, float] = {}
    flow = capacity_sections[0].table.dropna()
    for (node, tech, carrier), row in flow.iterrows():
        if not carrier:
            continue
        low, high = bounds(float(row.v06))
        entry = {"flow_cap_min": value(tech, low, carrier), "flow_cap_max": value(tech, high, carrier)}
        if tech in links:  # one capacity per link (v0.6 reports it at both ends)
            if high > link_caps.get(tech, -1.0):
                link_caps[tech] = high
                techs[tech] = entry
        else:
            nodes.setdefault(node, {"techs": {}})["techs"].setdefault(tech, {}).update(entry)
    # `storage_cap_max` is also a coefficient of the translated run.py `storage_ev_constraint` (storage_cap == share x
    # storage_cap_max): there, only the lower bound is fixed and the constraint itself gives the upper one.
    linked: set[tuple[str, str]] = set()
    for name in inputs.data_vars:
        if str(name).startswith("storage_ev__"):
            linked |= _pairs(inputs[name].fillna(0).astype(bool), inputs)
    for section in capacity_sections[1:]:
        for (node, tech), row in section.table.dropna().iterrows():
            low, high = bounds(float(row.v06))
            target = nodes.setdefault(node, {"techs": {}})["techs"].setdefault(tech, {})
            target["storage_cap_min"] = low
            if (node, tech) not in linked:
                target["storage_cap_max"] = high
    override = {k: v for k, v in (("nodes", nodes), ("techs", techs)) if v}
    header = (
        "# Fixed-design check (generated by the `fixed_design` rule): the capacities of the v0.6 solution, fixed in the\n"
        f"# converted v0.7 model (relative slack {FIX_RTOL:g}). Solve the converted model with the same scenario and\n"
        "# this override (e.g. `override_dict=calliope.io.read_rich_yaml(<this file>)`, merged after the scenario's\n"
        f"# overrides) and save the results as `{FIXED_RUN}` in the case folder; then re-run `fixed_design`.\n"
    )
    yaml = YAML()
    yaml.default_flow_style = False
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.dump(override, f)


def verdict(v06: xr.Dataset, v07_results: xr.Dataset, fixed_path: Path) -> tuple[str, Section | None]:
    """Verdict of the fixed-design run and its costs per tech against v0.6."""
    if not fixed_path.exists():
        return (
            f"Not run yet: solve the converted model with `{FIX_FILE}` (written next to this report) and save the "
            f"results as `{FIXED_RUN}` in the case folder. If the v0.6 design is feasible and equally cheap in "
            "v0.7, the remaining differences are equal-cost alternatives, not conversion errors.",
            None,
        )
    try:
        fixed = xr.open_dataset(fixed_path, group="results")
    except (OSError, ValueError) as err:
        return f"`{FIXED_RUN}` could not be read ({err}).", None
    if "cost" not in fixed:
        return (
            "**INFEASIBLE**: the converted model cannot reproduce the v0.6 capacities. A constraint differs "
            "between the versions (or a capacity is outside a v0.7 bound): look for it among the techs with "
            "the largest capacity differences.",
            None,
        )
    techs07 = set(map(str, fixed.techs.values))
    rows: dict[tuple[str, str], list[float]] = {}
    for (cost, loc_tech), value in v06.cost.to_series().items():
        _, tech = _split_loc_tech(loc_tech, techs07)
        rows.setdefault((tech, str(cost)), [0.0, 0.0])[0] += float(value) if np.isfinite(value) else 0.0
    for (tech, cost), value in fixed.cost.sum("nodes", min_count=1).to_series().dropna().items():
        rows.setdefault((str(tech), str(cost)), [0.0, 0.0])[1] += float(value)
    table = pd.DataFrame.from_dict(rows, orient="index", columns=["v06", "v07"])
    table.index = pd.MultiIndex.from_tuples(table.index, names=["techs", "costs"])
    table = table[(table.v06.abs() > ATOL) | (table.v07.abs() > ATOL)]
    lines = []
    worst = "ok"
    for cost in sorted(set(map(str, v06.costs.values)) & set(map(str, fixed.costs.values))):
        a, b = float(v06.cost.sel(costs=cost).sum()), float(fixed.cost.sel(costs=cost).sum())
        gap = (b - a) / abs(a) if abs(a) > ATOL else (0.0 if abs(b) <= ATOL else np.inf)
        lines.append(f"`{cost}`: v0.6 {a:.6g}, v0.7 with the v0.6 design {b:.6g} ({100 * gap:+.3g}%)")
        if abs(gap) > RTOL and cost == "monetary" or (abs(gap) > RTOL and worst == "ok" and cost != "monetary"):
            worst = "higher" if gap > 0 else "lower"
    if worst == "ok":
        head = (
            "**EQUIVALENT**: the v0.6 design is feasible in the converted model and costs the same (within "
            f"{100 * RTOL:g}%). The v0.6 solution is also optimal in v0.7, so the remaining differences between the "
            "two runs are equal-cost alternatives, not conversion errors."
        )
    elif worst == "higher":
        head = (
            "**COSTLIER**: the v0.6 design is feasible in the converted model but costs more there: costs or "
            "constraints differ between the versions. The costs per tech show where."
        )
    else:
        head = (
            "**CHEAPER**: the converted model runs the v0.6 design more cheaply than v0.6 did: a v0.6 constraint "
            "or cost may be missing or looser in v0.7. The costs per tech show where."
        )
    section = Section(
        "Costs per tech: v0.6 vs v0.7 with the v0.6 design (fixed-design check)",
        table,
        note="Same capacities in both runs, so differing costs come from the dispatch or from the cost definitions.",
    )
    return head + "\n\n" + "\n".join(f"- {line}" for line in lines), section


def check(case_folder: Path, out_folder: Path) -> str:
    """Write the fix override and, if `v07_fixed.nc` exists, the verdict report. Returns the verdict text."""
    case_folder, out_folder = Path(case_folder), Path(out_folder)
    out_folder.mkdir(parents=True, exist_ok=True)
    v06 = load_v06(case_folder / "v06.nc")
    v07_inputs, v07_results = load_v07(case_folder / "v07.nc")
    write_override(capacities(v06, v07_inputs, v07_results), v07_inputs, out_folder / FIX_FILE)
    text, section = verdict(v06, v07_results, case_folder / FIXED_RUN)
    lines = [f"# Fixed-design check: `{case_folder.name}`", "", text, ""]
    if section is not None:
        table = _with_diff(section.table)
        table.to_csv(out_folder / "fixed_design_costs_per_tech.csv")
        worst = table.reindex(table["diff"].abs().sort_values(ascending=False).index).head(15)
        lines += [f"## {section.title}", "", section.note, "", "Largest differences (top 15):", "", *_markdown(worst)]
    (out_folder / REPORT).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("case_folder", type=Path, help="Folder with v06.nc, v07.nc and optionally v07_fixed.nc")
    parser.add_argument("output_folder", type=Path)
    args = parser.parse_args(argv)
    print(check(args.case_folder, args.output_folder).splitlines()[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
