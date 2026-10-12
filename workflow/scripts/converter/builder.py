"""Orchestrate the conversion of one legacy model into an in-memory v0.7 model."""

from __future__ import annotations

import dataclasses
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import pandas as pd
from ruamel.yaml import YAML

from converter.config import SPORES_TRACKING_PARAM, ConfigConverter
from converter.context import TechContext, TechInfo, template_name
from converter.diagnostics import Diagnostics
from converter.groups import GroupConverter, GroupData, sanitise
from converter.legacy import LegacyModel, deep_merge
from converter.techs import (
    MEMBER_CARRIER,
    PRIMARY_CARRIER_OUT,
    CostRow,
    FragmentConverter,
    TimeseriesRef,
    conversion_plus_math,
    time_varying_limits_math,
)
from converter.timeseries import DerivedCsv, in_span, read_header, validate_csv

MATH_DIR = "math"
TABLE_DIR = "data_tables"
GENERATED_DIR = f"{TABLE_DIR}/generated"
STATIC_MATH = Path(__file__).parent / "math"
# Resampled by sum, as v0.6 `resource` (v0.7 base math sets it); every other timeseries is averaged.
# run.py `carrier_prod_per_month_<sense>_constraint` (only built for the `_time_varying` parameters).
PER_MONTH_PARAMS = {f"flow_out_per_month_{sense}_relative_per_ts": sense for sense in ("min", "max", "equals")}
PER_MONTH_STATIC = {f"flow_out_per_month_{sense}" for sense in ("min", "max", "equals")}
MONTH_LOOKUP = "timestep_month"
SENSE_OPERATOR = {"min": ">=", "max": "<=", "equals": "=="}
SUMMED_ON_RESAMPLE = {f"{kind}_use_{bound}" for kind in ("source", "sink") for bound in ("min", "max", "equals")}

AUTO_ACTIVE = "__auto_active__"  # placeholder for `active: true` added by the converter

# Precedence of cost rows within one scope (higher wins), mirroring v0.6 inheritance.
LEVEL_GROUP = 0
LEVEL_TECH = 100
LEVEL_LINK = 150
LEVEL_NODE = 200
# run.py fuel distribution (math/fuel_distribution.yaml): legacy tech naming rule of `fuel_distribution_constraint`.
FUEL_IMPORT_SUFFIX, FUEL_EXPORT_SUFFIX = "_distribution_import", "_distribution_export"
FUEL_IMPORT_LOOKUP, FUEL_EXPORT_LOOKUP = "fuel_distribution_import", "fuel_distribution_export"
# run.py time-varying limits (math/time_varying_limits.yaml).
TIME_VARYING_LIMITS = ("flow_cap_max_relative_per_ts", "flow_cap_equals_relative_per_ts", "flow_in_max_relative_per_ts")
# Costs annualised with `lifetime` and `cost_interest_rate`.
INVESTMENT_COSTS = {
    "cost_flow_cap",
    "cost_flow_cap_per_distance",
    "cost_storage_cap",
    "cost_source_cap",
    "cost_area_use",
    "cost_purchase",
    "cost_purchase_per_distance",
}


@dataclass
class Scope:
    """Converted content of the base model or of one override."""

    name: str | None  # None == base model
    file: str  # output YAML (relative) where scope-wide content is written
    templates: dict[str, dict] = field(default_factory=dict)
    techs: dict[str, dict] = field(default_factory=dict)
    nodes: dict[str, dict] = field(default_factory=dict)
    data_definitions: dict[str, Any] = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    cost_rows: list[tuple[int, CostRow]] = field(default_factory=list)
    timeseries: list[TimeseriesRef] = field(default_factory=list)
    groups: GroupData | None = None
    group_files: dict[str, str] = field(default_factory=dict)  # data_definitions key -> file (base only)
    entity_files: dict[tuple[str, str], str] = field(default_factory=dict)  # (section, name) -> file (base only)
    data_tables: dict[str, dict] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name or "base"


@dataclass
class ConvertedModel:
    entry_rel: str
    yaml_files: dict[str, dict]
    csv_copies: list[tuple[Path, str]]
    derived_csvs: list[DerivedCsv]
    generated_csvs: dict[str, pd.DataFrame]
    math_files: dict[str, dict | Path]
    stats: dict[str, Any]
    time_span: tuple[str, str] | None = None
    node_renames: dict[str, str] = field(default_factory=dict)  # applied to CSV headers


def _rel_path(target: str, from_file: str) -> str:
    """Path of `target` relative to the directory of `from_file` (both relative to the model root)."""
    target_parts = PurePosixPath(target).parts
    from_parts = PurePosixPath(from_file).parent.parts
    common = 0
    for a, b in zip(target_parts, from_parts, strict=False):
        if a != b:
            break
        common += 1
    up = [".."] * (len(from_parts) - common)
    return "/".join(up + list(target_parts[common:])) or "."


def expand_keys(section: dict) -> list[tuple[str, Any, str]]:
    """Expand comma-separated location/link keys. Returns (name, value, original key)."""
    out = []
    for key, value in (section or {}).items():
        for name in str(key).split(","):
            name = name.strip()
            if name:
                out.append((name, value, str(key)))
    return out


class ModelBuilder:
    def __init__(
        self,
        legacy: LegacyModel,
        diag: Diagnostics,
        *,
        trim_timeseries: bool = True,
        contract_group_families: bool = True,
        runpy_constraints: set[str] | None = None,
        storage_ev_filter: str | None = None,
    ):
        self.legacy = legacy
        self.diag = diag
        self.span = self._time_span() if trim_timeseries else None
        self.ctx = TechContext(legacy, diag)
        self.conv = FragmentConverter(self.ctx, diag)
        self.config_conv = ConfigConverter(diag)
        self.entry_rel = legacy.rel(legacy.entry)
        self.all_nodes = self._collect_nodes()
        self.links = self._collect_links()  # (from, to, tech) -> link tech name
        self.groups = GroupConverter(diag, self.expand_techs, lambda: self.all_nodes)
        self.base: Scope | None = None
        self.overrides: dict[str, Scope] = {}
        self.tracked_scopes: set[str] = set()
        self.override_overlaps: list[tuple[str, str, int]] = []
        self.shadow_prices: list[str] = []
        self.contract_group_families = contract_group_families
        self.runpy_constraints = runpy_constraints or set()  # custom constraints of the legacy run.py
        self.storage_ev_filter = storage_ev_filter  # name substring of run.py's storage_ev set
        self.per_month: set[str] = set()  # senses of run.py per-month limits in use
        self._raw_sources: set[Path] = set()  # legacy CSVs used as they are by a data table
        self._derived_sources: set[Path] = set()  # legacy CSVs used through a derived CSV

    def _time_span(self) -> tuple[str, str] | None:
        """Hull of every `model.subset_time` in the base model and overrides (None: keep all)."""
        spans = []
        for data in [self.legacy.base, *self.legacy.overrides.values()]:
            subset = (data.get("model") or {}).get("subset_time")
            if subset is None:
                if data is self.legacy.base:
                    return None  # the base model uses the whole timeseries
                continue
            bounds = subset if isinstance(subset, list) else [subset, subset]
            if len(bounds) != 2:
                return None
            spans.append((str(bounds[0]), str(bounds[1])))
        span = (min(s[0] for s in spans), max(s[1] for s in spans))
        self.diag.info(
            "trimmed_timeseries", f"Timeseries trimmed to {span[0]} .. {span[1]} (union of all `subset_time`)."
        )
        return span

    # ------------------------------------------------------------- registries
    def _collect_nodes(self) -> list[str]:
        nodes: list[str] = []
        for data in [self.legacy.base, *self.legacy.overrides.values()]:
            for name, _, _ in expand_keys(data.get("locations") or {}):
                if name not in nodes:
                    nodes.append(name)
        return nodes

    def _collect_links(self) -> dict[tuple[str, str, str], str]:
        links: dict[tuple[str, str, str], str] = {}
        for data in [self.legacy.base, *self.legacy.overrides.values()]:
            for key, link in (data.get("links") or {}).items():
                ends = [e.strip() for e in str(key).split(",")]
                if len(ends) != 2:
                    self.diag.warn("bad_link", f"Link key `{key}` must name exactly two locations; skipped.")
                    continue
                for tech in (link or {}).get("techs") or {}:
                    links.setdefault((ends[0], ends[1], str(tech)), f"{ends[0]}_to_{ends[1]}_{tech}")
        return links

    def link_techs(self, tech: str) -> list[str]:
        return [name for (_, _, t), name in self.links.items() if t == tech]

    def expand_techs(self, name: str) -> list[str]:
        """Concrete v0.7 techs that a legacy tech / tech_group name refers to."""
        info = self.ctx.get(name)
        if info is None:
            return []
        concrete = self.ctx.descendants(name) if info.is_group else [name]
        out: list[str] = []
        for tech in concrete:
            tech_info = self.ctx.get(tech)
            out.extend(self.link_techs(tech) if tech_info and tech_info.is_transmission else [tech])
        return out

    # ------------------------------------------------------------------ build
    def build(self) -> ConvertedModel:
        self.base = self._convert_scope(None, self.legacy.base, self.entry_rel)
        for name, data in self.legacy.overrides.items():
            file = self.legacy.source_of("overrides", name)
            self.overrides[name] = self._convert_scope(name, data, file)
        if self.contract_group_families:
            self._contract_group_families()
        self._add_activation_guards()
        self._add_dspt_members()
        self._complete_objective_weights()
        self.config_conv.check_spores_objective()
        self._check_lifetimes()
        derived, generated = self._build_tables()
        self.per_month = self._per_month_table(generated)
        math_files = self._math_and_config()
        csv_copies = self._csv_copies()
        yaml_files = self._assemble_yaml()
        stats = {
            "techs": len(self.ctx.tech_names),
            "tech_groups": len(self.ctx.groups),
            "nodes": len(self.all_nodes),
            "link_techs": len(self.links),
            "overrides": len(self.legacy.overrides),
            "scenarios": len(self.legacy.scenarios),
            "group_constraints": len(self.groups.items),
            "dspt_groups": len(self.groups.dspt_groups),
            "conversion_plus_techs": sum(1 for n in self.ctx.tech_names if self.ctx.info[n].is_conversion_plus),
            "csv_files": len(csv_copies),
            "derived_csv_files": len(derived),
            "group_types": dict(sorted(self._group_type_counts().items())),
            "overlapping_override_pairs": len(self.override_overlaps),
        }
        return ConvertedModel(
            self.entry_rel,
            yaml_files,
            csv_copies,
            derived,
            generated,
            math_files,
            stats,
            self.span,
            self.legacy.node_renames,
        )

    def _contract_group_families(self) -> None:
        """Merge per-region copies of legacy groups (see `GroupConverter.contract_families`)."""
        assert self.base is not None
        scopes = [s for s in self._scopes() if s.groups]
        self.groups.contract_families([s.groups for s in scopes], [s.label for s in scopes])
        if self.base.groups:
            self.base.group_files = {}
            for group in self.base.groups.groups:
                group_file = self.legacy.source_of("group_constraints", (group.sources or [group.name])[0])
                for key in GroupData(groups=[group]).data_definitions():
                    self.base.group_files[key] = group_file

    def _group_type_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for item in self.groups.items.values():
            counts[f"{item.vtype}_{item.sense}"] += 1
        for group in self.groups.dspt_groups.values():
            counts["demand_share_per_timestep_decision"] += len(group.dspt)
        return counts

    # ------------------------------------------------------------ conversion
    def _convert_scope(self, name: str | None, data: dict, file: str) -> Scope:
        scope = Scope(name=name, file=file)
        label = scope.label
        is_base = name is None
        known = {"model", "run", "techs", "tech_groups", "locations", "links", "group_constraints"}
        for key in data:
            if key not in known:
                self.diag.warn("unknown_section", f"Top-level `{key}` is not a v0.6 model section; dropped.", label)

        for group, definition in (data.get("tech_groups") or {}).items():
            self._convert_group(scope, str(group), definition)
        for tech, definition in (data.get("techs") or {}).items():
            self._convert_tech(scope, str(tech), definition)
        for node, definition, key in expand_keys(data.get("locations") or {}):
            self._convert_location(scope, node, definition, key)
        for key, link in (data.get("links") or {}).items():
            self._convert_link(scope, str(key), link)

        if data.get("group_constraints"):
            scope.groups = self.groups.convert(data["group_constraints"], label)
            if is_base:
                for group in scope.groups.groups:
                    group_file = self.legacy.source_of("group_constraints", group.name)
                    for key in GroupData(groups=[group]).data_definitions():
                        scope.group_files[key] = group_file

        if data.get("model") or data.get("run") or is_base:
            result = self.config_conv.convert(data.get("model"), data.get("run"), label, is_base=is_base)
            scope.config = result.config
            scope.data_definitions.update(result.data_definitions)
            if result.needs_spores_objective:
                scope.config.setdefault("init", {})["__spores_objective__"] = True
        return scope

    def _source(self, scope: Scope, section: str, name: str) -> str:
        return self.legacy.source_of(section, name) if scope.name is None else scope.file

    def _add_costs(self, scope: Scope, level: int, rows: list[CostRow], techs: list[str] | None = None) -> None:
        for row in rows:
            targets = techs if techs is not None else [row.tech]
            for target in targets:
                carrier = row.carrier
                if carrier == MEMBER_CARRIER:
                    carrier = self._member_carrier(target, row.param)
                    if carrier is None:
                        self.diag.warn(
                            "no_carrier", f"Cannot determine the carrier of `{target}` for `{row.param}`.", scope.label
                        )
                        continue
                scope.cost_rows.append((level, CostRow(target, row.cost, row.param, row.value, carrier, row.node)))

    def _member_carrier(self, tech: str, param: str) -> str | None:
        info = self.ctx.get(tech)
        if info is None:  # link techs: use their transmission template
            for (_, _, template), name in self.links.items():
                if name == tech:
                    info = self.ctx.get(template)
                    break
        if info is None:
            return None
        return info.primary_in if param == "cost_flow_in" else info.cap_carrier

    def _convert_group(self, scope: Scope, group: str, definition: Any) -> None:
        info = self.ctx.get(group)
        res = self.conv.convert(group, definition, scope=scope.label, defining=True)
        tpl = template_name(group)
        if res.params or scope.name is None:  # base templates always exist: techs reference them
            scope.templates[tpl] = deep_merge(scope.templates.get(tpl, {}), res.params)
            scope.entity_files[("templates", tpl)] = self._source(scope, "tech_groups", group)
        members = self.expand_techs(group)
        depth = len(info.chain) if info else 0
        self._add_costs(scope, LEVEL_GROUP + depth, res.cost_rows, members)
        for ref in res.timeseries:
            for member in members:
                scope.timeseries.append(_retarget(ref, member))
        if res.spores_tracked:
            self._mark_tracked(scope, members)

    def _convert_tech(self, scope: Scope, tech: str, definition: Any) -> None:
        info = self.ctx.get(tech) or TechInfo(tech, False, [], None)
        if not info.is_defined:
            self.diag.warn(
                "undefined_tech", f"`techs.{tech}` skipped: the tech is not defined in this model.", scope.label
            )
            return
        defining = isinstance(definition, dict) and (
            "parent" in (definition.get("essentials") or {}) or scope.name is None
        )
        res = self.conv.convert(tech, definition, scope=scope.label, defining=defining)
        params = dict(res.params)
        file = self._source(scope, "techs", tech)
        if info.is_transmission:
            if params:
                scope.templates[tech] = deep_merge(scope.templates.get(tech, {}), params)
                scope.entity_files[("templates", tech)] = file
            targets = self.link_techs(tech)
            self._add_costs(scope, LEVEL_TECH, res.cost_rows, targets)
            if res.timeseries:
                self.diag.warn(
                    "transmission_timeseries",
                    f"Timeseries on transmission tech `{tech}` are not supported; dropped.",
                    scope.label,
                )
        else:
            if params or scope.name is None:
                scope.techs[tech] = deep_merge(scope.techs.get(tech, {}), params)
                scope.entity_files[("techs", tech)] = file
            self._add_costs(scope, LEVEL_TECH, res.cost_rows)
            scope.timeseries.extend(res.timeseries)
            if res.spores_tracked:
                self._mark_tracked(scope, [tech])

    def _mark_tracked(self, scope: Scope, techs: list[str]) -> None:
        self.tracked_scopes.add(scope.label)
        for tech in techs:
            info = self.ctx.get(tech)
            if info and info.is_transmission:
                continue
            target = scope.techs.setdefault(tech, {})
            target[SPORES_TRACKING_PARAM] = True
            if scope.name is None:
                scope.entity_files.setdefault(("techs", tech), self.legacy.source_of("techs", tech))
        # link techs inherit from their template
        for tech in techs:
            for link in self.link_techs(tech):
                scope.techs.setdefault(link, {})[SPORES_TRACKING_PARAM] = True

    def _convert_location(self, scope: Scope, node: str, definition: Any, key: str) -> None:
        definition = definition or {}
        if not isinstance(definition, dict):
            self.diag.warn("bad_location", f"Location `{key}` is not a mapping; skipped.", scope.label)
            return
        out = scope.nodes.setdefault(node, {})
        if scope.name is None:
            scope.entity_files.setdefault(("nodes", node), self.legacy.source_of("locations", key))
        for item, value in definition.items():
            if item == "coordinates":
                value = value or {}
                if "lat" in value or "lon" in value:
                    out["latitude"] = value.get("lat")
                    out["longitude"] = value.get("lon")
                else:
                    self.diag.info(
                        "xy_coordinates",
                        f"`{node}` uses x/y coordinates (no v0.7 equivalent; dropped): link distances are computed "
                        "from them, as v0.6 did.",
                        scope.label,
                    )
            elif item == "available_area":
                out["available_area"] = value
            elif item == "exists":
                out["active"] = bool(value)
            elif item == "techs":
                for tech, fragment in (value or {}).items():
                    self._convert_node_tech(scope, node, str(tech), fragment, out)
            else:
                self.diag.warn("unknown_key", f"`locations.{key}.{item}` has no v0.7 equivalent; dropped.", scope.label)

    def _convert_node_tech(self, scope: Scope, node: str, tech: str, fragment: Any, out: dict) -> None:
        info = self.ctx.get(tech)
        if info is not None and info.is_transmission:
            self.diag.warn(
                "transmission_at_node",
                f"Transmission tech `{tech}` at location `{node}` ignored (use links).",
                scope.label,
            )
            return
        if info is not None and info.is_group:
            self.diag.warn("group_at_node", f"tech_group `{tech}` listed at location `{node}`; ignored.", scope.label)
            return
        if info is None or not info.is_defined:
            self.diag.warn(
                "undefined_tech",
                f"`{tech}` at location `{node}` skipped: the tech is not defined in this model.",
                scope.label,
            )
            return
        res = self.conv.convert(tech, fragment, scope=scope.label, node=node)
        params = dict(res.params)
        if scope.name is not None and "active" not in params:
            if self._base_defines(node, tech):
                if not params and not res.cost_rows and not res.timeseries:
                    return  # re-listing a tech that already exists there: no-op (avoids override key clashes)
            elif not self._base_inactive(node, tech):
                params["active"] = AUTO_ACTIVE  # resolved in `_add_activation_guards`
        techs = out.setdefault("techs", {})
        techs[tech] = deep_merge(techs.get(tech) or {}, params) if params else techs.get(tech)
        self._add_costs(scope, LEVEL_NODE, res.cost_rows)
        scope.timeseries.extend(res.timeseries)

    def _base_defines(self, node: str, tech: str) -> bool:
        for name, definition, _ in expand_keys(self.legacy.base.get("locations") or {}):
            if name == node:
                techs = (definition or {}).get("techs") or {}
                if tech in techs and not (isinstance(techs[tech], dict) and techs[tech].get("exists") is False):
                    return True
        return False

    def _base_inactive(self, node: str, tech: str) -> bool:
        for name, definition, _ in expand_keys(self.legacy.base.get("locations") or {}):
            if name == node:
                frag = ((definition or {}).get("techs") or {}).get(tech)
                if isinstance(frag, dict) and frag.get("exists") is False:
                    return True
        return False

    def _convert_link(self, scope: Scope, key: str, link: Any) -> None:
        ends = [e.strip() for e in key.split(",")]
        if len(ends) != 2:
            return  # already reported
        link = link or {}
        for item in set(link) - {"techs"}:
            self.diag.warn("unknown_key", f"`links.{key}.{item}` has no v0.7 equivalent; dropped.", scope.label)
        for tech, fragment in (link.get("techs") or {}).items():
            tech = str(tech)
            info = self.ctx.get(tech)
            if info is None or not info.is_transmission:
                self.diag.warn(
                    "bad_link", f"`links.{key}` uses `{tech}`, which is not a transmission tech.", scope.label
                )
                continue
            name = self.links[(ends[0], ends[1], tech)]
            res = self.conv.convert(tech, fragment, scope=scope.label, target=name)
            params = {"template": tech, "link_from": ends[0], "link_to": ends[1], **res.params}
            if "distance" not in params and not self._link_has_distance(name):
                distance = self._xy_distance(ends[0], ends[1], scope)
                if distance is not None:
                    params["distance"] = distance
            scope.techs[name] = deep_merge(scope.techs.get(name, {}), params)
            if scope.name is None:
                scope.entity_files[("techs", name)] = self.legacy.source_of("links", key)
            self._add_costs(scope, LEVEL_LINK, res.cost_rows)

    def _link_has_distance(self, name: str) -> bool:
        """The base model already gives this link a distance (explicit or computed)."""
        return self.base is not None and "distance" in (self.base.techs.get(name) or {})

    def _xy_distance(self, a: str, b: str, scope: Scope) -> float | None:
        """v0.6 link length between x/y coordinates (Euclidean), used when a link sets no `distance`.

        v0.7 computes distances from lat/lon only; lat/lon links are left to it (as v0.6 did with vincenty).
        """
        points = []
        for node in (a, b):
            coords = None
            for data in [self.legacy.base] + ([] if scope.name is None else [self.legacy.overrides.get(scope.name)]):
                location = ((data or {}).get("locations") or {}).get(node) or {}
                coords = location.get("coordinates") or coords
            if not isinstance(coords, dict) or "x" not in coords or "y" not in coords:
                return None
            try:
                points.append((float(coords["x"]), float(coords["y"])))
            except (TypeError, ValueError):
                return None
        (x1, y1), (x2, y2) = points
        return math.hypot(x1 - x2, y1 - y2)

    # ----------------------------------------------------- activation guards
    def _add_activation_guards(self) -> None:
        """Data tables over (nodes, techs) activate techs at every node with data.

        Techs with tech-level timeseries are therefore explicitly deactivated at
        base nodes where v0.6 did not define them. Overrides that add the tech
        at a node set `active: true` (see `_convert_node_tech`).
        """
        assert self.base is not None
        scopes = [self.base, *self.overrides.values()]
        guarded: dict[str, Scope] = {}
        guards: set[tuple[str, str]] = set()
        for scope in scopes:
            for ref in scope.timeseries:
                if ref.node is None:
                    owner = self.base if ref.tech in self.ctx.base_techs else scope
                    guarded.setdefault(ref.tech, owner)
        for tech, owner in guarded.items():
            for node in self.all_nodes:
                base_node = self.base.nodes.get(node)
                if base_node is not None and tech in (base_node.get("techs") or {}):
                    continue
                if owner is not self.base and tech in ((owner.nodes.get(node) or {}).get("techs") or {}):
                    continue
                if owner is self.base and base_node is None:
                    continue
                target = owner.nodes.setdefault(node, {}).setdefault("techs", {})
                target[tech] = {"active": False}
                guards.add((node, tech))
                if owner is not self.base:
                    self.diag.warn(
                        "activation_guard",
                        f"`{tech}` is only defined in override `{owner.name}`; it is deactivated at nodes it is not "
                        "listed at in that override, which may clash with other overrides adding it.",
                        owner.label,
                    )

        # `active: true` is only needed to lift a guard, or when a bare listing must define the tech at the node.
        for scope in self.overrides.values():
            for node, node_def in scope.nodes.items():
                for tech, params in (node_def.get("techs") or {}).items():
                    if isinstance(params, dict) and params.get("active") == AUTO_ACTIVE:
                        if (node, tech) in guards or len(params) == 1:
                            params["active"] = True
                        else:
                            del params["active"]

    def _add_dspt_members(self) -> None:
        if not self.groups.dspt_members:
            return
        members = sorted(self.groups.dspt_members)
        assert self.base is not None
        self.base.data_definitions["demand_share_per_timestep_decision_member"] = {
            "data": [True] * len(members),
            "index": [list(m) for m in members],
            "dims": ["nodes", "techs", "carriers"],
        }

    # ----------------------------------------------------------------- tables
    def _csv_copies(self) -> list[tuple[Path, str]]:
        model_dir = self.legacy.model_dir
        referenced = self._referenced_csvs()
        copies = []
        for path in sorted(model_dir.rglob("*.csv")):
            rel = self.legacy.rel(path)
            if path.resolve() in self._derived_sources and path.resolve() not in self._raw_sources:
                continue  # only used through its derived CSV(s), which are its conversion
            copies.append((path, f"{TABLE_DIR}/{rel}"))
            if path.resolve() not in referenced:
                self.diag.warn("unreferenced_csv", f"`{rel}` is not referenced by any YAML file (converted anyway).")
        return copies

    def _referenced_csvs(self) -> set[Path]:
        refs: set[Path] = set()
        for scope in self._scopes():
            for ref in scope.timeseries:
                refs.add((self.legacy.timeseries_dir / ref.file).resolve())
            for _, row in scope.cost_rows:
                if isinstance(row.value, TimeseriesRef):
                    refs.add((self.legacy.timeseries_dir / row.value.file).resolve())
        return refs

    def _storage_ev_math(self) -> dict | None:
        """run.py `storage_ev_constraint`: EV battery capacity follows the share of transport served by its powertrain.

        `storage_cap[<sector>_<mode>_battery] == demand_share_per_timestep_decision[<sector>_transport_<mode>,
        <sector>_transport] * storage_cap_max[<sector>_<mode>_battery]`, at nodes where the battery has a finite
        `storage_cap_max` and the transport tech has a demand-share decision (as run.py). Pairs follow run.py's
        naming rule; batteries that do not fit it are warned about, not guessed.
        """
        if "storage_ev_constraint" not in self.runpy_constraints:
            return None
        members = self.groups.dspt_members  # (node, tech, carrier) with a demand-share decision variable
        pairs = []
        batteries = [
            t for t in self.ctx.tech_names if t.endswith("_battery") and self.ctx.info[t].abstract == "storage"
        ]
        # run.py's constraint set (`loc_tech_ev_storage_constraint`) keeps names containing a substring.
        name_filter = self.storage_ev_filter
        if name_filter is None:
            name_filter = "_battery"
            self.diag.warn(
                "storage_ev",
                "Could not read the battery-name filter of run.py's `loc_tech_ev_storage_constraint` set; "
                "every `*_battery` storage tech is considered (the rule's own condition).",
            )
        skipped = sorted(t for t in batteries if name_filter not in t)
        if skipped:
            self.diag.info(
                "storage_ev",
                f"{skipped}: name without `{name_filter}`, so outside run.py's storage_ev_constraint set; "
                "not constrained.",
            )
        for battery in sorted(t for t in batteries if name_filter in t):
            parts = battery.removesuffix("_battery").split("_")
            if len(parts) < 2:
                self.diag.warn(
                    "storage_ev",
                    f"`{battery}` does not follow run.py's `<sector>_<mode>_battery` naming; "
                    "storage_ev_constraint not applied to it.",
                )
                continue
            mode, sector = parts[-1], "_".join(parts[:-1])
            transport, carrier = f"{sector}_transport_{mode}", f"{sector}_transport"
            nodes = sorted(n for n, t, c in members if t == transport and c == carrier)
            if transport not in self.ctx.info or not nodes:
                self.diag.warn(
                    "storage_ev",
                    f"`{battery}`: no `{transport}` with a demand-share decision on `{carrier}`; "
                    "storage_ev_constraint not applied to it (run.py skips it too).",
                )
                continue
            pairs.append((sector, mode, battery, transport, nodes))
        if not pairs:
            return None
        ev_math: dict = {"lookups": {}, "constraints": {}}
        for sector, mode, battery, transport, nodes in pairs:
            lookup = sanitise(f"storage_ev__{sector}_{mode}")
            index = [[n, battery] for n in nodes] + [[n, transport] for n in nodes]
            self.base.data_definitions[lookup] = {
                "data": [True] * len(index),
                "index": index,
                "dims": ["nodes", "techs"],
            }
            ev_math["lookups"][lookup] = {
                "description": f"`{battery}` and `{transport}` at the nodes where run.py links them.",
                "dtype": "bool",
                "default": False,
            }
            ev_math["constraints"][sanitise(f"storage_ev_{sector}_{mode}")] = {
                "description": (
                    f"Legacy run.py `storage_ev_constraint`: the capacity of `{battery}` is the share of "
                    f"`{sector}_transport` served by `{transport}` times its `storage_cap_max`."
                ),
                "foreach": ["nodes", "techs"],
                "where": f"{lookup} AND base_tech==storage AND storage_cap_max",
                "equations": [
                    {
                        "expression": "storage_cap == sum(where(demand_share_per_timestep_decision, "
                        f"{lookup}), over=[techs, carriers]) * storage_cap_max"
                    }
                ],
            }
        self.diag.info(
            "storage_ev",
            f"run.py `storage_ev_constraint` translated (math/storage_ev.yaml) for {[p[2] for p in pairs]}.",
        )
        return ev_math

    def _resample_math(self, math_files: dict[str, dict | Path]) -> dict | None:
        """Time resampling as in v0.6: timeseries are averaged, except `resource` (`source_use_*`/`sink_use_*`), summed.

        v0.7 resamples with each parameter's `resample_method`, which defaults to `first` where the math does not set
        one. Timeseries parameters not already given a method by the converter's math get `mean` here (v0.7 merges
        this partial definition into the base or custom one).
        """
        defined: set[str] = set()
        for content in math_files.values():
            if isinstance(content, Path):
                content = YAML(typ="safe", pure=True).load(content.read_text(encoding="utf-8"))
            for name, spec in ((content or {}).get("parameters") or {}).items():
                if isinstance(spec, dict) and "resample_method" in spec:
                    defined.add(name)
        params = {ref.param for scope in self._scopes() for ref in self._table_refs(scope)}
        params -= defined | SUMMED_ON_RESAMPLE
        if not params:
            return None
        return {"parameters": {p: {"resample_method": "mean"} for p in sorted(params)}}

    def _per_month_table(self, generated: dict[str, pd.DataFrame]) -> set[str]:
        """run.py `carrier_prod_per_month_*`: a (timesteps, months) lookup of the timeseries' months -> senses used.

        Months are calendar months (v0.6 `timesteps.dt.month`, over all years), read from the parameters' own CSVs.
        """
        refs = [ref for scope in self._scopes() for ref in scope.timeseries if ref.param in PER_MONTH_PARAMS]
        for scope in self._scopes():
            definitions = list(scope.techs.items())
            definitions += [kv for node in scope.nodes.values() for kv in (node.get("techs") or {}).items()]
            for tech, definition in definitions:
                if not isinstance(definition, dict):
                    continue
                for param in sorted(set(definition) & set(PER_MONTH_PARAMS)):
                    self.diag.warn(
                        "per_month",
                        f"`{tech}.{param}` is a value, not a timeseries: run.py then reads `carrier_prod_per_month_"
                        f"{PER_MONTH_PARAMS[param]}` instead; not translated.",
                        scope.label,
                    )
                    definition.pop(param)
                for param in sorted(set(definition) & PER_MONTH_STATIC):
                    self.diag.warn(
                        "per_month",
                        f"`{tech}.{param}`: run.py applies per-month limits only with the `_time_varying` timeseries; "
                        "carried over, no constraint.",
                        scope.label,
                    )
        stamps: set[pd.Timestamp] = set()
        for ref in refs:
            src = (self.legacy.timeseries_dir / ref.file).resolve()
            if not src.exists() or not read_header(src):
                continue  # warned when building the tables
            index = pd.read_csv(src, usecols=[0], index_col=0).index.astype(str)
            stamps.update(pd.to_datetime([t for t in index if in_span(t, self.span)], errors="coerce").dropna())
        if not stamps:
            return set()
        timesteps = pd.DatetimeIndex(sorted(stamps), name="timesteps")
        months = [f"month_{m:02d}" for m in range(1, 13)]
        table = pd.DataFrame({m: (timesteps.month == i).astype(int) for i, m in enumerate(months, 1)}, index=timesteps)
        table = table.loc[:, table.any()]
        rel = f"{GENERATED_DIR}/{MONTH_LOOKUP}.csv"
        generated[rel] = table
        assert self.base is not None
        self.base.data_tables[MONTH_LOOKUP] = {
            "table": _rel_path(rel, self.entry_rel),
            "rows": "timesteps",
            "columns": "months",
            "add_dims": {"inputs": MONTH_LOOKUP},
        }
        techs = sorted({ref.tech for ref in refs})
        self.diag.info(
            "per_month", f"run.py per-month production limits translated (math/flow_out_per_month.yaml) for {techs}."
        )
        return {PER_MONTH_PARAMS[ref.param] for ref in refs}

    def _per_month_math(self) -> dict:
        """Share of the horizon's outflow (primary carrier) produced in each calendar month, as run.py.

        `sum(flow_out in month) <op> sum(flow_out) * sum(param * timestep_resolution in month)`: unweighted sums.
        """
        math: dict[str, Any] = {
            "dimensions": {
                "months": {"title": "Calendar months", "dtype": "string", "ordered": True, "iterator": "month"}
            },
            "parameters": {},
            "lookups": {
                MONTH_LOOKUP: {
                    "description": "Calendar month of each timestep (v0.6 `month_numbers`).",
                    "dtype": "bool",
                    "default": False,
                },
                PRIMARY_CARRIER_OUT: {
                    "description": "Primary output carrier of multi-output techs (v0.6 `primary_carrier_out`).",
                    "dtype": "bool",
                    "default": False,
                },
            },
            "constraints": {},
        }
        for param, sense in PER_MONTH_PARAMS.items():
            if sense not in self.per_month:
                continue
            math["parameters"][param] = {
                "description": f"Legacy `carrier_prod_per_month_{sense}_time_varying`: per-timestep share of the "
                "horizon's outflow, summed (times the resolution) over each month.",
                "default": float("nan"),
                "unit": "fraction",
                "resample_method": "mean",
            }
            math["constraints"][f"flow_out_per_month_{sense}"] = {
                "description": f"Legacy run.py `carrier_prod_per_month_{sense}_constraint`.",
                "foreach": ["nodes", "techs", "carriers", "months"],
                "where": f"any({param}, over=timesteps) AND carrier_out AND any({MONTH_LOOKUP}, over=timesteps) "
                "AND (primary_carrier_out OR NOT any(primary_carrier_out, over=carriers))",
                "equations": [
                    {
                        "expression": f"sum(where(flow_out, {MONTH_LOOKUP}), over=timesteps) {SENSE_OPERATOR[sense]} "
                        "sum(flow_out, over=timesteps) * "
                        f"sum(where({param} * timestep_resolution, {MONTH_LOOKUP}), over=timesteps)"
                    }
                ],
            }
        return math

    def _techs_using(self, params: set[str]) -> set[str]:
        """Techs that set any of `params` (value or timeseries), in any scope."""
        return set(self._params_by_tech(params))

    def _fuel_distribution_math(self) -> dict | None:
        """run.py `fuel_distribution_constraint`: `<carrier>_distribution_import` / `_export` techs are a distribution.

        As run.py: per carrier, what all nodes import over the whole horizon equals what they export (unweighted
        sums), in the form of the math gallery `fuel_dist` (`restrict_total_imports_and_exports`, which balances
        each timestep). The techs are kept and tagged with lookups.
        """
        pairs = {}
        for tech in self.ctx.tech_names:
            if not tech.endswith(FUEL_IMPORT_SUFFIX):
                continue
            carrier = tech.removesuffix(FUEL_IMPORT_SUFFIX)
            export = f"{carrier}{FUEL_EXPORT_SUFFIX}"
            info_in, info_out = self.ctx.info[tech], self.ctx.info.get(export)
            if info_out is None or carrier not in info_in.carriers_out or carrier not in info_out.carriers_in:
                self.diag.warn(
                    "fuel_distribution",
                    f"`{tech}` has no matching `{export}` (or other carriers); not part of the fuel distribution.",
                )
                continue
            pairs[carrier] = (tech, export)
        if not pairs:
            return None
        for scope in self._scopes():
            for carrier, (tech_in, tech_out) in pairs.items():
                for tech, lookup in ((tech_in, FUEL_IMPORT_LOOKUP), (tech_out, FUEL_EXPORT_LOOKUP)):
                    definition = scope.techs.get(tech)
                    if isinstance(definition, dict) and "base_tech" in definition:
                        definition[lookup] = True
        carriers = ", ".join(sorted(pairs))
        self.diag.info(
            "fuel_distribution",
            f"Fuel distribution (run.py `fuel_distribution_constraint`) for [{carriers}]: imports equal exports "
            "over all nodes and timesteps (math/fuel_distribution.yaml).",
        )
        return {
            "lookups": {
                FUEL_IMPORT_LOOKUP: {
                    "description": "Legacy `<carrier>_distribution_import` techs (fuel bought from the distribution).",
                    "dtype": "bool",
                    "default": False,
                },
                FUEL_EXPORT_LOOKUP: {
                    "description": "Legacy `<carrier>_distribution_export` techs (fuel sold to the distribution).",
                    "dtype": "bool",
                    "default": False,
                },
            },
            "constraints": {
                "fuel_distribution_balance": {
                    "description": (
                        "Legacy run.py `fuel_distribution_constraint` (form of the math gallery `fuel_dist` "
                        "`restrict_total_imports_and_exports`): per carrier, imports over all nodes and timesteps "
                        "equal exports over all nodes and timesteps."
                    ),
                    "foreach": ["carriers"],
                    "where": f"[{carriers}] in carriers AND any({FUEL_IMPORT_LOOKUP}, over=techs) "
                    f"AND any({FUEL_EXPORT_LOOKUP}, over=techs)",
                    "equations": [
                        {
                            "expression": f"sum(where(flow_out, {FUEL_IMPORT_LOOKUP}), over=[nodes, techs, timesteps]) == "
                            f"sum(where(flow_in, {FUEL_EXPORT_LOOKUP}), over=[nodes, techs, timesteps])"
                        }
                    ],
                }
            },
        }

    def _time_varying_limit_techs(self) -> dict[str, set[str]]:
        return self._params_by_tech(set(TIME_VARYING_LIMITS))

    def _params_by_tech(self, params: set[str]) -> dict[str, set[str]]:
        """Techs setting any of `params` (as a timeseries or a value, in any scope) -> the params they set."""
        link_tech = {name: tech for (_, _, tech), name in self.links.items()}
        techs: dict[str, set[str]] = defaultdict(set)
        for scope in self._scopes():
            for ref in scope.timeseries:
                if ref.param in params:
                    techs[link_tech.get(ref.tech, ref.tech)].add(ref.param)
            definitions = list(scope.techs.items())
            definitions += [kv for node in scope.nodes.values() for kv in (node.get("techs") or {}).items()]
            for name, definition in definitions:
                if isinstance(definition, dict):
                    techs[name].update(params & set(definition))
        return {tech: used for tech, used in techs.items() if used}

    def _check_lifetimes(self) -> None:
        """Techs with investment costs need a `lifetime`: without it v0.7 annualised costs (and the objective) are NaN."""
        link_tech = {name: tech for (_, _, tech), name in self.links.items()}
        invested: set[str] = set()
        for scope in self._scopes():
            for _, row in scope.cost_rows:
                if row.param in INVESTMENT_COSTS and not (isinstance(row.value, float) and row.value == 0):
                    invested.add(link_tech.get(row.tech, row.tech))
        missing = sorted(t for t in invested if t in self.ctx.info and not self._has_lifetime(t))
        if missing:
            where = ", ".join(f"`{t}` ({self.legacy.source_of('techs', t)})" for t in missing)
            self.diag.warn(
                "missing_lifetime",
                f"FAILED CHECK: {len(missing)} tech(s) have investment costs but no `lifetime` anywhere (tech, its "
                f"tech_groups, locations or overrides): {where}. Calliope v0.7 cannot annualise their costs and the "
                "objective becomes NaN. Add a `lifetime` to them.",
            )

    def _has_lifetime(self, tech: str) -> bool:
        info = self.ctx.info[tech]
        if info.base_constraints.get("lifetime") is not None:
            return True
        names = [tech, *(template_name(g) for g in info.chain)]
        for scope in self._scopes():
            entries = [scope.techs.get(tech), *(scope.templates.get(n) for n in names)]
            entries += [(node.get("techs") or {}).get(tech) for node in scope.nodes.values()]
            if any(isinstance(e, dict) and e.get("lifetime") is not None for e in entries):
                return True
        return False

    def _complete_objective_weights(self) -> None:
        """Give unlisted cost classes an objective weight of 0.

        v0.6 optimises only the classes in `objective_options.cost_class` (default `monetary: 1`);
        v0.7 weights every class not in `objective_cost_weights` by its default of 1.
        """
        assert self.base is not None
        classes = sorted({row.cost for scope in self._scopes() for _, row in scope.cost_rows})
        for scope in self._scopes():
            weights = scope.data_definitions.get("objective_cost_weights")
            if weights is None and scope is not self.base:
                continue
            weights = weights or {"data": [1.0], "index": ["monetary"], "dims": "costs"}
            listed = dict(zip(weights["index"], weights["data"], strict=True))
            missing = [c for c in classes if c not in listed]
            if not missing:
                continue
            listed.update(dict.fromkeys(missing, 0.0))
            scope.data_definitions["objective_cost_weights"] = {
                "data": list(listed.values()),
                "index": list(listed),
                "dims": "costs",
            }
            self.diag.info(
                "objective_weights",
                f"Cost classes {missing} are not in the objective: weight 0 (v0.7 would weight them by 1).",
                scope.label,
            )

    def _scopes(self) -> list[Scope]:
        assert self.base is not None
        return [self.base, *self.overrides.values()]

    def _table_refs(self, scope: Scope) -> list[TimeseriesRef]:
        return list(scope.timeseries) + [
            r.value for _, r in self._dedupe_costs(scope) if isinstance(r.value, TimeseriesRef)
        ]

    def _build_tables(self) -> tuple[list[DerivedCsv], dict[str, pd.DataFrame]]:
        derived: dict[str, DerivedCsv] = {}
        generated: dict[str, pd.DataFrame] = {}
        names: set[str] = set()
        headers: dict[Path, list[str]] = {}

        def unique(name: str) -> str:
            name = sanitise(name)
            candidate, i = name, 2
            while candidate in names:
                candidate, i = f"{name}_{i}", i + 1
            names.add(candidate)
            return candidate

        # Sources also used as they are: their sign-flipped version needs a different name.
        used_raw = {
            (self.legacy.timeseries_dir / ref.file).resolve()
            for scope in self._scopes()
            for ref in self._table_refs(scope)
            if not (ref.transform or ref.column or ref.factor is not None)
        }
        for scope in self._scopes():
            # Calliope resolves table paths of overrides relative to the model entry file too.
            table_file = self.entry_rel
            for ref in self._table_refs(scope):
                src = (self.legacy.timeseries_dir / ref.file).resolve()
                if not src.exists():
                    self.diag.warn(
                        "missing_csv", f"`{ref.file}` (for `{ref.tech}.{ref.param}`) not found; dropped.", scope.label
                    )
                    continue
                if src not in headers:
                    headers[src] = (
                        read_header(src) if validate_csv(src, self.diag, self.legacy.rel(src), self.span) else []
                    )
                if not headers[src]:
                    continue
                columns = [self.legacy.node_renames.get(c, c) for c in headers[src][1:]]
                if ref.column is None:
                    available = [n for n in self.all_nodes if n in columns]
                else:
                    available = list(self.all_nodes)
                if ref.node is not None:
                    if ref.column is None and ref.node not in columns:
                        self.diag.warn(
                            "missing_column",
                            f"`{ref.file}` has no column `{ref.node}` (for `{ref.tech}`).",
                            scope.label,
                        )
                        continue
                    select = [ref.node]
                else:
                    select = available
                    if ref.column is None:
                        missing = [n for n in self._nodes_with(ref.tech, scope) if n not in columns]
                        if missing:
                            self.diag.warn(
                                "missing_column",
                                f"`{ref.file}` has no column for {missing}, where `{ref.tech}` exists; "
                                f"`{ref.param}` is undefined there.",
                                scope.label,
                            )
                if not select:
                    self.diag.warn("missing_column", f"`{ref.file}` has no column for any model location.", scope.label)
                    continue
                if ref.transform or ref.column or ref.factor is not None:
                    rel_src = self.legacy.rel(src)
                    factor = None if ref.factor is None else f"x{ref.factor:.12g}"
                    # Named after the transform so derived files never collide. A sign flip (demand made positive)
                    # keeps the legacy name, as the original is then not copied, unless it is also used as it is.
                    transform = ref.transform
                    if transform == "negate":
                        transform = "positive" if src in used_raw else None
                    suffix = "".join(f"__{x}" for x in (transform, ref.column, factor) if x)
                    # Next to its source: the suffix keeps it apart from the copied original.
                    dest = f"{TABLE_DIR}/{PurePosixPath(rel_src).with_suffix('').as_posix()}{suffix}.csv"
                    derived.setdefault(
                        dest,
                        DerivedCsv(src, dest, ref.transform, ref.column, tuple(self.all_nodes), self.span, ref.factor),
                    )
                    csv_rel = dest
                    self._derived_sources.add(src.resolve())
                else:
                    csv_rel = f"{TABLE_DIR}/{self.legacy.rel(src)}"
                    self._raw_sources.add(src.resolve())
                add_dims: dict[str, Any] = {"techs": ref.tech, "inputs": ref.param}
                add_dims.update(dict(ref.extra_dims))
                entry = {
                    "table": _rel_path(csv_rel, table_file),
                    "rows": "timesteps",
                    "columns": "nodes",
                    "select": {"nodes": select},
                    "add_dims": add_dims,
                }
                scope.data_tables[unique(f"ts_{ref.tech}_{ref.param}" + (f"_{ref.node}" if ref.node else ""))] = entry
            self._cost_tables(scope, table_file, generated, unique)
        return list(derived.values()), generated

    def _nodes_with(self, tech: str, scope: Scope) -> list[str]:
        """Nodes where `tech` is (actively) listed in the base model or in `scope`."""
        assert self.base is not None
        nodes = []
        for candidate in (self.base, scope):
            for node, node_def in candidate.nodes.items():
                params = (node_def.get("techs") or {}).get(tech, "absent")
                if params == "absent" or (isinstance(params, dict) and params.get("active") is False):
                    continue
                if node not in nodes:
                    nodes.append(node)
        return nodes

    @staticmethod
    def _dedupe_costs(scope: Scope) -> list[tuple[int, CostRow]]:
        best: dict[tuple, tuple[int, int, CostRow]] = {}
        for order, (level, row) in enumerate(scope.cost_rows):
            key = (row.tech, row.node, row.cost, row.param, row.carrier)
            if key not in best or level >= best[key][0]:
                best[key] = (level, order, row)
        return [(level, row) for level, _, row in sorted(best.values(), key=lambda x: x[1])]

    def _cost_tables(self, scope: Scope, table_file: str, generated: dict, unique) -> None:
        rows = [row for _, row in self._dedupe_costs(scope) if not isinstance(row.value, TimeseriesRef)]
        if not rows:
            return
        folder = GENERATED_DIR if scope.name is None else f"{GENERATED_DIR}/overrides/{sanitise(scope.name)}"
        for at_node in (False, True):
            for with_carrier in (False, True):
                subset = [
                    r for r in rows if (r.node is not None) == at_node and (r.carrier is not None) == with_carrier
                ]
                if not subset:
                    continue
                index = (["nodes"] if at_node else []) + ["techs", "costs"] + (["carriers"] if with_carrier else [])
                records = []
                for r in subset:
                    record = {"techs": r.tech, "costs": r.cost, "param": r.param, "value": r.value}
                    if at_node:
                        record["nodes"] = r.node
                    if with_carrier:
                        record["carriers"] = r.carrier
                    records.append(record)
                df = pd.DataFrame(records).pivot_table(index=index, columns="param", values="value", aggfunc="last")
                df.columns.name = None
                stem = ("node_" if at_node else "") + "costs" + ("_per_carrier" if with_carrier else "")
                csv_rel = f"{folder}/{stem}.csv"
                generated[csv_rel] = df
                scope.data_tables[unique(f"{stem}_{scope.label}")] = {
                    "table": _rel_path(csv_rel, table_file),
                    "rows": index,
                    "columns": "inputs",
                }

    # ------------------------------------------------------------ math/config
    def _math_and_config(self) -> dict[str, dict | Path]:
        assert self.base is not None
        math_files: dict[str, dict | Path] = {}
        extra: list[str] = []
        paths: dict[str, str] = {}

        def add(name: str, rel: str, content: dict | Path) -> None:
            math_files[rel] = content
            paths[name] = _rel_path(rel, self.entry_rel)
            extra.append(name)

        conversion_plus = [self.ctx.info[n] for n in self.ctx.tech_names if self.ctx.info[n].is_conversion_plus]
        for info in conversion_plus:
            self.diag.info(
                "conversion_plus_capacity",
                f"conversion_plus tech `{info.name}`: capacity translated as in v0.6, one capacity on the primary "
                f"output carrier `{info.cap_carrier}` (tier-1 outputs share it; min use and ramping refer to it).",
            )
        cp_math = conversion_plus_math(conversion_plus)
        if cp_math:
            add("legacy_conversion_plus", f"{MATH_DIR}/conversion_plus.yaml", cp_math)
        if self._techs_using({"storage_charge_depth"}):
            path = STATIC_MATH / "storage_charge_depth.yaml"
            add("legacy_storage_charge_depth", f"{MATH_DIR}/storage_charge_depth.yaml", path)
        ev_math = self._storage_ev_math()
        if ev_math:
            add("legacy_storage_ev", f"{MATH_DIR}/storage_ev.yaml", ev_math)
        fuel_math = self._fuel_distribution_math()
        if fuel_math:
            add("legacy_fuel_distribution", f"{MATH_DIR}/fuel_distribution.yaml", fuel_math)
        time_varying = self._time_varying_limit_techs()
        if time_varying:
            path = STATIC_MATH / "time_varying_limits.yaml"
            static = YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8"))
            techs = [(self.ctx.info[t], used) for t, used in sorted(time_varying.items()) if t in self.ctx.info]
            tv_math = time_varying_limits_math(static, techs)
            add(
                "legacy_time_varying_limits",
                f"{MATH_DIR}/time_varying_limits.yaml",
                path if tv_math is static else tv_math,
            )
            self.diag.info(
                "time_varying_limits",
                "run.py time-varying limits (`energy_cap_max/equals_time_varying`, `energy_cap_max_time_varying_con`) "
                f"translated in math/time_varying_limits.yaml for {sorted(time_varying)}.",
            )
        if self.groups.dspt_groups:
            add(
                "legacy_demand_share_per_timestep_decision",
                f"{MATH_DIR}/demand_share_per_timestep_decision.yaml",
                STATIC_MATH / "demand_share_per_timestep_decision.yaml",
            )
        if any(
            row.param == "cost_om_annual_investment_fraction" for scope in self._scopes() for _, row in scope.cost_rows
        ):
            path = STATIC_MATH / "om_investment_fraction.yaml"
            add("legacy_om_investment_fraction", f"{MATH_DIR}/om_investment_fraction.yaml", path)
            self.diag.info(
                "om_investment_fraction",
                "`om_annual_investment_fraction` is a fraction of the annualised investment in v0.6 (of the whole "
                "investment in v0.7): `cost_operation_fixed` is redefined as in v0.6 (math/om_investment_fraction.yaml).",
            )
        capacity_factor = self._params_by_tech({"capacity_factor_min", "capacity_factor_max"})
        if capacity_factor:
            add("legacy_capacity_factor", f"{MATH_DIR}/capacity_factor.yaml", STATIC_MATH / "capacity_factor.yaml")
            self.diag.info(
                "capacity_factor",
                f"run.py capacity factor limits translated (math/capacity_factor.yaml) for {sorted(capacity_factor)}.",
            )
        if self.per_month:
            add("legacy_flow_out_per_month", f"{MATH_DIR}/flow_out_per_month.yaml", self._per_month_math())
        if self.groups.has_groups:
            group_math = self.groups.math()
            group_math.setdefault("lookups", {})[PRIMARY_CARRIER_OUT] = {
                "description": "Primary output carrier of multi-output techs (v0.6 `primary_carrier_out`).",
                "dtype": "bool",
                "default": False,
            }
            add("legacy_group_constraints", f"{MATH_DIR}/group_constraints.yaml", group_math)
        elif not (capacity_factor or self.per_month):
            # Only group capacity sums, capacity factors and per-month limits use it.
            for scope in self._scopes():
                for tech in scope.techs.values():
                    tech.pop(PRIMARY_CARRIER_OUT, None)

        resample_math = self._resample_math(math_files)
        if resample_math:
            add("legacy_resample", f"{MATH_DIR}/resample.yaml", resample_math)

        spores_scopes = [s for s in self._scopes() if s.config.get("init", {}).pop("__spores_objective__", False)]
        init = self.base.config.setdefault("init", {})
        if paths:
            init["extra_math"] = list(extra)
        if spores_scopes:
            math_files[f"{MATH_DIR}/spores_objective.yaml"] = STATIC_MATH / "spores_objective.yaml"
            paths["legacy_spores_objective"] = _rel_path(f"{MATH_DIR}/spores_objective.yaml", self.entry_rel)
            for scope in spores_scopes:
                if scope.name is None:
                    init.setdefault("extra_math", []).append("legacy_spores_objective")
                else:
                    scope.config.setdefault("init", {})["extra_math"] = list(extra) + ["legacy_spores_objective"]
            self.diag.info(
                "spores",
                "SPORES `objective_cost_class` weights of non-score cost classes are added to the v0.7 SPORES objective "
                "(math/spores_objective.yaml).",
            )
        if paths:
            init["math_paths"] = paths

        if self.tracked_scopes:
            # One place only (overrides may not set the same key twice in a scenario):
            # the scope(s) switching SPORES mode on, else the base model.
            spores_mode = [s for s in self._scopes() if s.config.get("init", {}).get("mode") == "spores"] or [self.base]
            for scope in spores_mode:
                scope.config.setdefault("solve", {}).setdefault("spores", {})["tracking_parameter"] = (
                    SPORES_TRACKING_PARAM
                )

        shadow = ["system_balance"]
        for item in self.groups.items.values():
            if item.vtype == "cost" and item.key and "co2" in item.key.lower():
                shadow.append(item.constraint)
        self.base.config.setdefault("solve", {})["shadow_prices"] = shadow
        self.shadow_prices = shadow
        return math_files

    # ------------------------------------------------------------------- yaml
    def _assemble_yaml(self) -> dict[str, dict]:
        assert self.base is not None
        files: dict[str, dict] = defaultdict(dict)
        entry = files[self.entry_rel]

        base = self.base
        entry_config = base.config
        if entry_config:
            entry["config"] = entry_config
        defs = dict(base.data_definitions)
        if base.groups:
            group_defs = base.groups.data_definitions()
            for key, value in group_defs.items():
                files[base.group_files.get(key, self.entry_rel)].setdefault("data_definitions", {})[key] = value
        if defs:
            entry["data_definitions"] = deep_merge(entry.get("data_definitions", {}), defs)
        if base.data_tables:
            entry["data_tables"] = base.data_tables

        for section, content in (("templates", base.templates), ("techs", base.techs), ("nodes", base.nodes)):
            for name, value in content.items():
                file = base.entity_files.get((section, name), self.entry_rel)
                files[file].setdefault(section, {})[name] = value if value is not None else {}

        override_dicts = {name: self._override_dict(scope) for name, scope in self.overrides.items()}
        for name, scope in self.overrides.items():
            files[scope.file].setdefault("overrides", {})[name] = override_dicts[name]
        for name, items in self.legacy.scenarios.items():
            file = self.legacy.source_of("scenarios", name)
            clashes = _clashes([override_dicts[i] for i in items if i in override_dicts])
            if clashes:
                merged_name = sanitise(f"scenario_{name}_merged")
                merged: dict = {}
                for item in items:
                    if item in override_dicts:
                        merged = deep_merge(merged, override_dicts[item])
                files[file].setdefault("overrides", {})[merged_name] = merged
                files[file].setdefault("scenarios", {})[name] = [merged_name] + [
                    i for i in items if i not in override_dicts
                ]
                self.diag.info(
                    "scenario_merged",
                    f"Scenario `{name}`: its overrides set {len(clashes)} identical keys (e.g. `{clashes[0]}`), which v0.7 "
                    f"forbids; the scenario now points to the merged override `{merged_name}` (v0.6 semantics: later wins).",
                    file,
                )
            else:
                files[file].setdefault("scenarios", {})[name] = items
        self._report_overlaps(override_dicts)

        imports = [_rel_path(f, self.entry_rel) for f in files if f != self.entry_rel and files[f]]
        result = {}
        result[self.entry_rel] = (
            {"import": imports, **files[self.entry_rel]} if imports else dict(files[self.entry_rel])
        )
        for f, content in files.items():
            if f != self.entry_rel and content:
                result[f] = content
        return result

    def _report_overlaps(self, override_dicts: dict[str, dict]) -> None:
        keys = {name: set(_leaves(d)) for name, d in override_dicts.items()}
        names = list(keys)
        pairs = []
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                shared = keys[a] & keys[b]
                if shared:
                    pairs.append((a, b, len(shared)))
        self.override_overlaps = pairs
        if pairs:
            self.diag.info(
                "override_overlap",
                f"{len(pairs)} pairs of overrides set the same keys and cannot be combined ad hoc in v0.7 "
                "(e.g. `scenario='a,b'`); see the report. Declared scenarios are handled.",
            )

    def _override_dict(self, scope: Scope) -> dict:
        out: dict[str, Any] = {}
        if scope.config:
            out["config"] = scope.config
        defs = dict(scope.data_definitions)
        if scope.groups:
            defs.update(scope.groups.data_definitions())
        if defs:
            out["data_definitions"] = defs
        if scope.data_tables:
            out["data_tables"] = scope.data_tables
        for section, content in (("templates", scope.templates), ("techs", scope.techs), ("nodes", scope.nodes)):
            if content:
                out[section] = {k: (v if v is not None else {}) for k, v in content.items()}
        return out


def _leaves(d: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in d.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            out.update(_leaves(value, path))
        else:
            out[path] = value
    return out


def _clashes(dicts: list[dict]) -> list[str]:
    seen: set[str] = set()
    clashes = []
    for d in dicts:
        for key in _leaves(d):
            if key in seen:
                clashes.append(key)
            seen.add(key)
    return clashes


def _retarget(ref: TimeseriesRef, tech: str) -> TimeseriesRef:
    return dataclasses.replace(ref, tech=tech)
