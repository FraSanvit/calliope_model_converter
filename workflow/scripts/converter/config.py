"""Convert v0.6 `model:` and `run:` sections to v0.7 `config:` / `data_definitions:`.

Works on full sections (base model) and partial ones (overrides).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from converter.diagnostics import Diagnostics
from converter.legacy import deep_merge

MODES = {"plan": "base", "operate": "operate", "spores": "spores"}
SPORES_SCORE_CLASS = "spores_score"
SPORES_TRACKING_PARAM = "spores_tracked"


@dataclass
class ConfigResult:
    config: dict = field(default_factory=dict)
    data_definitions: dict = field(default_factory=dict)
    needs_spores_objective: bool = False

    def set(self, path: str, value: Any) -> None:
        node = self.config
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value


def _costs_indexed(weights: dict) -> dict:
    classes = [str(k) for k in weights]
    return {"data": [float(weights[k]) for k in weights], "index": classes, "dims": "costs"}


def _resolution(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r"(\d+)\s*([A-Za-z]+)", text)
    if not match:
        return text.lower()
    number, unit = match.groups()
    return f"{number}{unit.lower()}"


class ConfigConverter:
    def __init__(self, diag: Diagnostics):
        self.diag = diag
        self.base_scope: str | None = None
        self.declared_classes: dict[str, set[str]] = {}  # scope -> classes of `objective_options.cost_class`
        self.spores_classes: dict[str, set[str]] = {}  # scope -> non-zero classes of SPORES `objective_cost_class`

    def convert(self, model: dict | None, run: dict | None, scope: str, *, is_base: bool) -> ConfigResult:
        result = ConfigResult()
        if is_base:
            self.base_scope = scope
            result.set("init.calliope_version", "0.7.0")
            result.set("init.distance_unit", "m")  # v0.6 derived link distances in metres
        self._model(model or {}, scope, result)
        self._run(run or {}, scope, result)
        return result

    # ------------------------------------------------------------------ model
    def _model(self, model: dict, scope: str, result: ConfigResult) -> None:
        for key, value in model.items():
            if key == "name":
                result.set("init.name", value)
            elif key in ("calliope_version", "timeseries_data_path"):
                continue
            elif key == "subset_time":
                if value is None:
                    result.set("init.subset.timesteps", None)
                else:
                    span = value if isinstance(value, list) else [value, value]
                    result.set("init.subset.timesteps", [str(v) for v in span])
            elif key == "time":
                self._time(value, scope, result)
            elif key == "timeseries_dateformat":
                result.set("init.datetime_format", value)
            elif key in ("random_seed", "file_allowed"):
                self.diag.info("dropped_key", f"`model.{key}` has no v0.7 equivalent; dropped.", scope)
            else:
                self.diag.warn("unknown_model_key", f"`model.{key}` has no v0.7 equivalent; dropped.", scope)

    def _time(self, value: Any, scope: str, result: ConfigResult) -> None:
        if value is None:
            result.set("init.resample.timesteps", None)
            return
        if not isinstance(value, dict):
            self.diag.warn("bad_value", f"`model.time: {value}` not understood; dropped.", scope)
            return
        function = value.get("function")
        if function == "resample":
            resolution = (value.get("function_options") or {}).get("resolution")
            result.set("init.resample.timesteps", _resolution(resolution))
        else:
            self.diag.warn(
                "unsupported_time",
                f"`model.time.function: {function}` is not supported in v0.7 "
                "(clustering is done with `config.init.time_cluster`); dropped.",
                scope,
            )

    # -------------------------------------------------------------------- run
    def _run(self, run: dict, scope: str, result: ConfigResult) -> None:
        for key, value in run.items():
            if key == "mode":
                mode = MODES.get(str(value))
                if mode is None:
                    self.diag.warn("bad_value", f"Unknown `run.mode: {value}`; dropped.", scope)
                else:
                    result.set("init.mode", mode)
            elif key == "solver":
                solver = str(value)
                if solver.endswith("_persistent"):
                    self.diag.warn(
                        "solver", f"Solver `{solver}` mapped to `{solver.removesuffix('_persistent')}`.", scope
                    )
                    solver = solver.removesuffix("_persistent")
                result.set("solve.solver", solver)
            elif key in ("solver_io", "zero_threshold", "save_logs"):
                result.set(f"solve.{key}", value)
            elif key == "solver_options":
                result.set("solve.solver_options", value or {})
            elif key == "ensure_feasibility":
                result.set("build.ensure_feasibility", bool(value))
            elif key == "backend":
                result.set("build.backend", value)
            elif key == "bigM":
                result.data_definitions["bigM"] = value
            elif key == "objective_options":
                self._objective_options(value or {}, scope, result)
            elif key == "objective":
                if value not in (None, "minmax_cost_optimization"):
                    self.diag.warn("bad_value", f"`run.objective: {value}` not supported; v0.7 default used.", scope)
            elif key == "relax_constraint":
                self._relax(value or {}, scope, result)
            elif key == "operation":
                self._operation(value or {}, scope, result)
            elif key == "spores_options":
                self._spores(value or {}, scope, result)
            elif key == "cyclic_storage":
                if value is False:
                    self.diag.warn(
                        "cyclic_storage",
                        "`run.cyclic_storage: false` is per-tech in v0.7 (`cyclic_storage: false`); set it on storage techs.",
                        scope,
                    )
            else:
                self.diag.warn("unknown_run_key", f"`run.{key}` has no v0.7 equivalent; dropped.", scope)

    def _objective_options(self, options: dict, scope: str, result: ConfigResult) -> None:
        for key, value in options.items():
            if key == "cost_class":
                self.declared_classes[scope] = set(value or {})
                weights = {k: v for k, v in (value or {}).items() if k != SPORES_SCORE_CLASS}
                if len(weights) != len(value or {}):
                    self.diag.info(
                        "spores_score",
                        "`spores_score` cost class removed from objective weights (v0.7 SPORES scores internally).",
                        scope,
                    )
                if weights:
                    result.data_definitions["objective_cost_weights"] = _costs_indexed(weights)
            elif key == "sense":
                if str(value).startswith("max"):
                    self.diag.warn(
                        "objective_sense", "Maximisation: use negative `objective_cost_weights` in v0.7.", scope
                    )
            else:
                self.diag.warn("unknown_run_key", f"`run.objective_options.{key}` dropped.", scope)

    def _relax(self, relax: dict, scope: str, result: ConfigResult) -> None:
        for key, value in relax.items():
            if key == "demand_share_per_timestep_decision_main_constraint":
                result.data_definitions["demand_share_relaxation"] = float(value)
            else:
                self.diag.warn(
                    "unknown_run_key", f"`run.relax_constraint.{key}` has no v0.7 equivalent; dropped.", scope
                )

    def _operation(self, operation: dict, scope: str, result: ConfigResult) -> None:
        for key in ("window", "horizon"):
            if key in operation:
                result.set(f"build.operate.{key}", f"{int(operation[key])}h")
                self.diag.info("operate", f"`run.operation.{key}` converted assuming hourly timesteps.", scope)
        for key in set(operation) - {"window", "horizon"}:
            self.diag.warn("unknown_run_key", f"`run.operation.{key}` has no v0.7 equivalent; dropped.", scope)

    def _spores(self, options: dict, scope: str, result: ConfigResult) -> None:
        for key, value in options.items():
            if key == "spores_number":
                result.set("solve.spores.number", int(value))
            elif key == "slack":
                result.data_definitions["spores_slack"] = float(value)
            elif key == "save_per_spore":
                if not value:
                    result.set("solve.spores.save_per_spore_path", None)
            elif key == "save_per_spore_path":
                path = str(value)
                directory = str(PurePosixPath(path).parent) if "{" in PurePosixPath(path).name else path
                result.set("solve.spores.save_per_spore_path", directory)
                if directory != path:
                    self.diag.info("spores", f"`save_per_spore_path` `{path}` -> directory `{directory}`.", scope)
            elif key == "score_cost_class":
                self.diag.info(
                    "spores",
                    f"SPORES score class `{value}`: techs with legacy `{value}` costs get "
                    f"`{SPORES_TRACKING_PARAM}: true` and `config.solve.spores.tracking_parameter`.",
                    scope,
                )
            elif key == "slack_cost_group":
                self.diag.info(
                    "spores",
                    f"`slack_cost_group: {value}` replaced by v0.7 `total_system_cost_max` (spores_slack x baseline cost).",
                    scope,
                )
            elif key == "objective_cost_class":
                self.spores_classes[scope] = {k for k, v in (value or {}).items() if v}
                weights = {k: v for k, v in (value or {}).items() if k != SPORES_SCORE_CLASS}
                if weights:
                    result.data_definitions["spores_objective_cost_weights"] = _costs_indexed(weights)
                    result.needs_spores_objective = True
            else:
                self.diag.warn("unknown_run_key", f"`run.spores_options.{key}` has no v0.7 equivalent; dropped.", scope)

    def check_spores_objective(self) -> None:
        """v0.6 builds its objective from `objective_options.cost_class` only (Calliope 0.6 `backend/pyomo/model.py`).

        Classes that `spores_options.objective_cost_class` adds later are never in the v0.6 objective unless declared
        there (with weight 0): v0.6 then ran the SPORES without them, while v0.7 uses them. Warned, not changed.
        """
        base = self.declared_classes.get(self.base_scope or "", {"monetary"})
        for scope, classes in sorted(self.spores_classes.items()):
            declared = base | self.declared_classes.get(scope, set())
            missing = sorted(classes - declared)
            if missing:
                self.diag.warn(
                    "spores_objective_v06",
                    f"SPORES `objective_cost_class` uses {missing}, not declared in `run.objective_options.cost_class` "
                    "(base model or this override): Calliope 0.6 builds its objective from those classes only, so the "
                    "v0.6 SPORES ignored them (unless another override of the scenario declares them), while v0.7 "
                    "applies them. Declare them there with weight 0 to make the legacy run do what it meant.",
                    scope,
                )


def merge_config(*results: ConfigResult) -> ConfigResult:
    out = ConfigResult()
    for r in results:
        out.config = deep_merge(out.config, r.config)
        out.data_definitions = deep_merge(out.data_definitions, r.data_definitions)
        out.needs_spores_objective = out.needs_spores_objective or r.needs_spores_objective
    return out
