"""Legacy run scripts (e.g. `run-cost_base-15-200kW-1h.py`) -> v0.7 run scripts.

A legacy run script sets a few variables, builds a comma-separated list of overrides (`scenarios_string`) and runs
the model through `create_input` / `run.py`. It is read statically (never executed): top-level assignments of
strings, numbers, f-strings and `+` concatenations are evaluated in order, to check the overrides it selects.

The v0.7 script keeps those settings verbatim and selects the overrides by name, as in v0.6. Calliope v0.7's
`scenario=` refuses overrides that set the same key twice (v0.6 let the later one win), so the script merges the
named overrides itself, in order, with the v0.6 rule, and passes them as `override_dict`.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from converter.diagnostics import Diagnostics

# Helper modules of a legacy model folder, never run scripts.
NOT_RUN_SCRIPTS = {"run.py", "create_input.py", "dual_utils.py"}
SCENARIO_VARIABLE = "scenarios_string"
RESULTS_VARIABLE = "scenario_name"


@dataclass
class RunScript:
    path: Path
    overrides: list[str]  # legacy scenario items, in order
    settings: dict[str, str] = field(default_factory=dict)  # evaluated top-level string/number assignments
    source: list[str] = field(default_factory=list)  # source code of those assignments, in order


def find_run_scripts(model_root: Path, diag: Diagnostics) -> list[RunScript]:
    scripts = []
    for path in sorted(Path(model_root).glob("*.py")):
        if path.name in NOT_RUN_SCRIPTS:
            continue
        script = read_run_script(path, diag)
        if script is not None:
            scripts.append(script)
    return scripts


def read_run_script(path: Path, diag: Diagnostics) -> RunScript | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError as err:
        diag.warn("bad_run_script", f"`{path.name}` could not be parsed ({err}); not converted.")
        return None
    values: dict[str, str] = {}
    source: list[str] = []
    unresolved = False
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        name = node.targets[0].id
        value = _evaluate(node.value, values)
        if value is None:
            unresolved = unresolved or name == SCENARIO_VARIABLE
            continue
        values[name] = value
        if not name.startswith("path_"):  # legacy file layout, replaced in the v0.7 script
            source.append(ast.get_source_segment(text, node) or f"{name} = {value!r}")
    if SCENARIO_VARIABLE not in values:
        if unresolved:
            diag.warn(
                "bad_run_script", f"`{path.name}`: `{SCENARIO_VARIABLE}` cannot be evaluated statically; not converted."
            )
        return None
    overrides = [item.strip() for item in values[SCENARIO_VARIABLE].split(",") if item.strip()]
    return RunScript(path=path, overrides=overrides, settings=values, source=source)


def _evaluate(node: ast.AST, values: dict[str, str]) -> str | None:
    """Value of a string/number expression built from constants and earlier variables; None otherwise."""
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str | int | float)
        and not isinstance(node.value, bool)
    ):
        return str(node.value)
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                if part.format_spec is not None or part.conversion != -1:
                    return None
                part = part.value
            text = _evaluate(part, values)
            if text is None:
                return None
            parts.append(text)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _evaluate(node.left, values), _evaluate(node.right, values)
        return None if left is None or right is None else left + right
    return None


def expand_scenario(items: list[str], scenarios: dict[str, list[str]], seen: tuple[str, ...] = ()) -> list[str]:
    """Replace legacy scenario names by their overrides (recursively), keeping the order."""
    out: list[str] = []
    for item in items:
        if item in scenarios and item not in seen:
            out.extend(expand_scenario(scenarios[item], scenarios, (*seen, item)))
        else:
            out.append(item)
    return out


def v07_run_script(script: RunScript, entry_rel: str, shadow_prices: list[str]) -> str:
    """A v0.7 run script equivalent to the legacy one (without `run.py` custom math and duals post-processing)."""
    settings = "\n".join(script.source)
    results = (
        f'HERE / "results" / f"{{{RESULTS_VARIABLE}}}.nc"'
        if RESULTS_VARIABLE in script.settings
        else f'HERE / "results" / "{script.path.stem}.nc"'
    )
    co2 = [c for c in shadow_prices if c != "system_balance"]
    co2_note = (
        f"    #   - `shadow_price_{co2[0]}` (CO2 cap; <= 0 for a cap: v0.6 `dual_utils` reported -1 x this)\n"
        if co2
        else ""
    )
    return f'''"""Calliope v0.7 version of the legacy run script `{script.path.name}`.

Generated by calliope_model_converter. As in v0.6, the overrides are selected by name (`scenarios_string`) and
combined in order, the later ones winning. Calliope v0.7's `scenario=` refuses overrides that set the same key
twice, so `v06_overrides` merges them here with the v0.6 rule and passes them as `override_dict`.
Change the settings below to run another combination.

Not included (on purpose):
- the custom math of the legacy `run.py`, apart from its time-varying limits (math/time_varying_limits.yaml):
  results differ from the v0.6 run until the rest is translated (see conversion_report.md);
- the `dual_utils.py` post-processing: shadow prices are standard v0.7 results (see below).
"""

from pathlib import Path

import calliope
from calliope.attrdict import AttrDict
from calliope.io import read_rich_yaml

# SETTINGS (from the legacy script)

{settings}

HERE = Path(__file__).parent
MODEL = HERE / "{entry_rel}"
RESULTS = {results}


def v06_overrides(model_file: Path, names: list[str]) -> dict:
    """Combine overrides as Calliope v0.6 did: in the given order, later ones win.

    Legacy scenario names (e.g. `uncontrolled`) are expanded to their overrides.
    """
    definition = read_rich_yaml(model_file)
    overrides, scenarios = definition.get("overrides", {{}}), definition.get("scenarios", {{}})
    merged = AttrDict()

    def add(name: str) -> None:
        if name in overrides:
            merged.union(overrides[name], allow_override=True)
        elif name in scenarios:
            for item in scenarios[name]:
                add(item)
        else:
            raise KeyError(f"`{{name}}` is neither an override nor a scenario of {{model_file}}")

    for name in names:
        add(name)
    return merged.as_dict()


def main() -> None:
    calliope.set_log_verbosity("info")
    overrides = [name.strip() for name in {SCENARIO_VARIABLE}.split(",") if name.strip()]
    model = calliope.read_yaml(MODEL, override_dict=v06_overrides(MODEL, overrides))
    model.build()
    model.solve()

    # Inputs and results (including shadow prices) in one file:
    #   - `shadow_price_system_balance` (nodes, carriers, timesteps)
{co2_note}    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    model.to_netcdf(RESULTS)


if __name__ == "__main__":
    main()
'''
