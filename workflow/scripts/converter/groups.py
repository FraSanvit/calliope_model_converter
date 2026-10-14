"""Convert v0.6 `group_constraints` into v0.7 custom math + data.

Design (see CLAUDE.md for the reasoning):

- one small constraint per (group, constraint type[, carrier/cost]) in a
  generated math file. Members are selected with the `where()` helper and
  boolean lookups over `techs` / `nodes` / `carriers` / `costs`, so absent or
  inactive techs are silently ignored (literal slices would raise);
- the constraint value is a scalar parameter in `data_definitions`, defined in
  the base model or in the override that defines the group. A constraint whose
  value parameter is undefined is not built, so each override only switches on
  its own groups;
- flows are summed over timesteps once (global expressions) before members are
  selected, to keep the per-group cost low on large models.

Semantics follow `calliope/backend/pyomo/constraints/group.py` (v0.6.10):
sums over timesteps are *not* weighted, `carrier_con` values are negative
(v0.7 `flow_in` is positive). Transmission capacity is not halved.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from converter.diagnostics import Diagnostics

SENSES = ("min", "max", "equals")
OPS = {"min": ">=", "max": "<=", "equals": "=="}

# v0.6 group constraint -> (v0.7 short name, keyed by: None | "carriers" | "costs")
GROUP_TYPES: dict[str, tuple[str, str | None]] = {
    "energy_cap": ("flow_cap", None),
    "energy_cap_share": ("flow_cap_share", None),
    "storage_cap": ("storage_cap", None),
    "resource_area": ("area_use", None),
    "carrier_prod": ("flow_out", "carriers"),
    "carrier_con": ("flow_in", "carriers"),
    "demand_share": ("demand_share", "carriers"),
    "demand_share_per_timestep": ("demand_share_per_timestep", "carriers"),
    "carrier_prod_share": ("flow_out_share", "carriers"),
    "carrier_prod_share_per_timestep": ("flow_out_share_per_timestep", "carriers"),
    "net_import_share": ("net_import_share", "carriers"),
    "cost": ("cost", "costs"),
    "cost_var": ("cost_operation_variable", "costs"),
    "cost_investment": ("cost_investment", "costs"),
}
DSPT = "demand_share_per_timestep_decision"

# v0.6 had one `energy_cap` per tech: multi-output techs count their primary output carrier only.
_PRIMARY_ONLY = "(primary_carrier_out OR NOT any(primary_carrier_out, over=carriers))"

# Shared global expressions referenced by the generated constraints.
GLOBAL_EXPRESSIONS: dict[str, dict] = {
    "group_flow_out_annual": {
        "description": "Unweighted sum of outflows over all timesteps (legacy group constraint semantics).",
        "foreach": ["nodes", "techs", "carriers"],
        "where": "carrier_out",
        "equations": [{"expression": "sum(flow_out, over=timesteps)"}],
    },
    "group_flow_in_annual": {
        "description": "Unweighted sum of inflows over all timesteps (legacy group constraint semantics).",
        "foreach": ["nodes", "techs", "carriers"],
        "where": "carrier_in",
        "equations": [{"expression": "sum(flow_in, over=timesteps)"}],
    },
    "group_demand_flow_in_annual": {
        "description": "Annual inflow into demand technologies.",
        "foreach": ["nodes", "techs", "carriers"],
        "where": "carrier_in AND base_tech==demand",
        "equations": [{"expression": "sum(flow_in, over=timesteps)"}],
    },
    "group_demand_flow_in": {
        "description": "Inflow into demand technologies.",
        "foreach": ["nodes", "techs", "carriers", "timesteps"],
        "where": "carrier_in AND base_tech==demand",
        "equations": [{"expression": "flow_in"}],
    },
    "group_supply_conversion_flow_out_annual": {
        "description": "Annual outflow of supply and conversion technologies.",
        "foreach": ["nodes", "techs", "carriers"],
        "where": "carrier_out AND (base_tech==supply OR base_tech==conversion)",
        "equations": [{"expression": "sum(flow_out, over=timesteps)"}],
    },
    "group_supply_conversion_flow_out": {
        "description": "Outflow of supply and conversion technologies.",
        "foreach": ["nodes", "techs", "carriers", "timesteps"],
        "where": "carrier_out AND (base_tech==supply OR base_tech==conversion)",
        "equations": [{"expression": "flow_out"}],
    },
    "group_flow_cap_out": {
        "description": (
            "Legacy `energy_cap`: the output capacity, on the primary output carrier only for multi-output techs."
        ),
        "foreach": ["nodes", "techs", "carriers"],
        "where": f"flow_cap AND carrier_out AND {_PRIMARY_ONLY}",
        "equations": [{"expression": "flow_cap"}],
    },
    "group_supply_conversion_flow_cap_out": {
        "description": "Legacy `energy_cap` of supply and conversion technologies.",
        "foreach": ["nodes", "techs", "carriers"],
        "where": f"flow_cap AND carrier_out AND {_PRIMARY_ONLY} AND (base_tech==supply OR base_tech==conversion)",
        "equations": [{"expression": "flow_cap"}],
    },
    "group_cost_investment_legacy": {
        "description": "Legacy `cost_investment`: annualised investment plus fixed O&M.",
        "foreach": ["nodes", "techs", "costs"],
        "where": "cost_investment_annualised OR cost_operation_fixed",
        "equations": [{"expression": "cost_investment_annualised + cost_operation_fixed"}],
    },
    "group_cost_operation_variable_annual": {
        "description": "Variable operating cost summed over timesteps.",
        "foreach": ["nodes", "techs", "costs"],
        "where": "cost_operation_variable",
        "equations": [{"expression": "sum(cost_operation_variable, over=timesteps)"}],
    },
}

# Which global expressions each v0.7 type needs (to keep generated math minimal).
_TYPE_EXPRESSIONS = {
    "flow_cap": ["group_flow_cap_out"],
    "flow_cap_share": ["group_flow_cap_out", "group_supply_conversion_flow_cap_out"],
    "flow_out": ["group_flow_out_annual"],
    "flow_in": ["group_flow_in_annual"],
    "demand_share": ["group_flow_out_annual", "group_demand_flow_in_annual"],
    "demand_share_per_timestep": ["group_demand_flow_in"],
    "flow_out_share": ["group_flow_out_annual", "group_supply_conversion_flow_out_annual"],
    "flow_out_share_per_timestep": ["group_supply_conversion_flow_out"],
    "net_import_share": ["group_flow_out_annual", "group_flow_in_annual", "group_demand_flow_in_annual"],
    "cost_operation_variable": ["group_cost_operation_variable_annual"],
    "cost_investment": ["group_cost_investment_legacy"],
}


@dataclass(frozen=True)
class _Spec:
    lhs: str  # array summed over the group's members
    dims: tuple[str, ...]  # dims of `lhs`
    denominator: str | None = None  # share constraints: array summed over the group's nodes
    lhs_minus: str | None = None  # net import: subtracted array
    per_timestep: bool = False


_NTC = ("nodes", "techs", "carriers")
_CONSTRAINT_SPECS: dict[str, _Spec] = {
    "flow_cap": _Spec("group_flow_cap_out", _NTC),
    "storage_cap": _Spec("storage_cap", ("nodes", "techs")),
    "area_use": _Spec("area_use", ("nodes", "techs")),
    "flow_cap_share": _Spec("group_flow_cap_out", _NTC, "group_supply_conversion_flow_cap_out"),
    "flow_out": _Spec("group_flow_out_annual", _NTC),
    "flow_in": _Spec("group_flow_in_annual", _NTC),
    "demand_share": _Spec("group_flow_out_annual", _NTC, "group_demand_flow_in_annual"),
    "demand_share_per_timestep": _Spec("flow_out", _NTC + ("timesteps",), "group_demand_flow_in", per_timestep=True),
    "flow_out_share": _Spec("group_flow_out_annual", _NTC, "group_supply_conversion_flow_out_annual"),
    "flow_out_share_per_timestep": _Spec(
        "flow_out", _NTC + ("timesteps",), "group_supply_conversion_flow_out", per_timestep=True
    ),
    "net_import_share": _Spec("group_flow_out_annual", _NTC, "group_demand_flow_in_annual", "group_flow_in_annual"),
    "cost": _Spec("cost", ("nodes", "techs", "costs")),
    "cost_operation_variable": _Spec("group_cost_operation_variable_annual", ("nodes", "techs", "costs")),
    "cost_investment": _Spec("group_cost_investment_legacy", ("nodes", "techs", "costs")),
}


def sanitise(name: str) -> str:
    """Make a valid Calliope component name fragment."""
    clean = re.sub(r"\W", "_", str(name)).strip("_").lower()
    return clean or "unnamed"


@dataclass
class GroupItem:
    """One constraint: a (group, type, sense, key) combination with its value."""

    gid: str
    vtype: str  # v0.7 short type, e.g. "flow_in"
    sense: str  # min | max
    key: str | None  # carrier or cost class
    value: Any  # float (None for a group family: values are per node)

    @property
    def param(self) -> str:
        key = f"__{sanitise(self.key)}" if self.key else ""
        return f"group_{self.gid}__{self.vtype}_{self.sense}{key}"

    @property
    def constraint(self) -> str:
        return f"{self.param}_constraint"


@dataclass
class GroupDefinition:
    name: str
    gid: str
    techs: list[str]  # empty == all
    nodes: list[str]  # empty == all
    items: list[GroupItem] = field(default_factory=list)
    dspt: dict[str, float] = field(default_factory=dict)  # carrier -> share (inf == no sum constraint)
    # Group family (per-region copies merged): legacy group names and per-node values
    sources: list[str] = field(default_factory=list)
    node_values: dict[str, dict[str, float]] = field(default_factory=dict)  # param -> node -> value

    @property
    def per_node(self) -> bool:
        return bool(self.sources)

    @property
    def member_dims(self) -> list[str]:
        return (["nodes"] if self.nodes else []) + (["techs"] if self.techs else [])

    @property
    def needs_nodes_lookup(self) -> bool:
        return bool(self.nodes) and any(_CONSTRAINT_SPECS[i.vtype].denominator for i in self.items)

    @property
    def lookup(self) -> str | None:
        return f"group_{self.gid}__members" if self.member_dims else None

    def members(self) -> dict | None:
        """Boolean membership lookup over the dims that are restricted.

        Given as data_definitions: entries for techs that are not defined at a
        node (in the scenario being run) are dropped by Calliope, so
        `any(members)` tells whether the group has any active member.
        """
        if self.nodes and self.techs:
            index = [[n, t] for n in self.nodes for t in self.techs]
            return {"data": [True] * len(index), "index": index, "dims": ["nodes", "techs"]}
        if self.nodes:
            return {"data": [True] * len(self.nodes), "index": list(self.nodes), "dims": "nodes"}
        if self.techs:
            return {"data": [True] * len(self.techs), "index": list(self.techs), "dims": "techs"}
        return None

    def gate(self) -> str:
        """Where-string fragment: the group has at least one active member."""
        if not self.lookup:
            return ""
        return f" AND any({self.lookup}, over=[{', '.join(self.member_dims)}])"


@dataclass
class GroupData:
    """Groups of one scope (base or one override)."""

    groups: list[GroupDefinition] = field(default_factory=list)
    relaxation: float | None = None

    def data_definitions(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for group in self.groups:
            members = group.members()
            if members is not None:
                out[f"group_{group.gid}__members"] = members
            if group.needs_nodes_lookup:
                out[f"group_{group.gid}__nodes"] = {
                    "data": [True] * len(group.nodes),
                    "index": group.nodes,
                    "dims": "nodes",
                }
            for item in group.items:
                out[item.param] = _by_node(group.node_values[item.param]) if group.per_node else item.value
            for carrier, share in group.dspt.items():
                key = sanitise(carrier)
                param, sum_param = f"group_{group.gid}__dspt__{key}", f"group_{group.gid}__dspt_sum__{key}"
                if group.per_node:
                    shares = group.node_values[param]
                    out[param] = _by_node({n: v if math.isfinite(v) else 1.0 for n, v in shares.items()})
                    if math.isfinite(share):
                        out[sum_param] = _by_node(shares)
                    continue
                out[param] = share if math.isfinite(share) else 1.0
                if math.isfinite(share):
                    out[sum_param] = share
        return out


class GroupConverter:
    """Converts group_constraints of every scope and accumulates the generated math."""

    def __init__(self, diag: Diagnostics, expand_techs: Callable[[str], list[str]], all_nodes: Callable[[], list[str]]):
        self.diag = diag
        self.expand_techs = expand_techs
        self.all_nodes = all_nodes
        self._signatures: dict[str, tuple] = {}  # gid -> (techs, nodes)
        self.items: dict[str, GroupItem] = {}  # param -> item (for math generation)
        self.definitions: dict[str, GroupDefinition] = {}  # gid -> definition (membership)
        self.dspt_groups: dict[str, GroupDefinition] = {}
        self.dspt_members: set[tuple[str, str, str]] = set()  # (node, tech, carrier)
        self.cost_classes: set[str] = set()

    @property
    def has_groups(self) -> bool:
        return bool(self.items or self.dspt_groups)

    def convert(self, raw: dict, scope: str) -> GroupData:
        data = GroupData()
        self._unknown: dict[str, int] = defaultdict(int)
        self._redefined: list[str] = []
        for name, definition in (raw or {}).items():
            if not isinstance(definition, dict):
                self.diag.warn("bad_group", f"Group constraint `{name}` is not a mapping; skipped.", scope)
                continue
            group = self._convert_group(str(name), definition, scope)
            if group is not None:
                data.groups.append(group)
        for tech, count in self._unknown.items():
            self.diag.warn(
                "unknown_tech",
                f"{count} group constraint(s) reference `{tech}`, which is not defined in this model; it is ignored there.",
                scope,
            )
        if self._redefined:
            self.diag.warn(
                "group_redefined",
                f"{len(self._redefined)} group(s) (e.g. `{self._redefined[0]}`) are also defined elsewhere with different "
                "techs/locs; each definition becomes its own constraint (both apply if combined).",
                scope,
            )
        return data

    # ------------------------------------------------------------------ parse
    def _members(self, definition: dict, scope: str, name: str) -> tuple[list[str], list[str]]:
        techs: list[str] = []
        for tech in definition.get("techs") or []:
            expanded = self.expand_techs(str(tech))
            if not expanded:
                self._unknown[str(tech)] += 1
            techs.extend(t for t in expanded if t not in techs)
        nodes: list[str] = []
        known_nodes = set(self.all_nodes())
        for loc in definition.get("locs") or []:
            for node in str(loc).split(","):
                node = node.strip()
                if node not in known_nodes:
                    self.diag.warn("unknown_node", f"Group `{name}` references unknown location `{node}`.", scope)
                if node not in nodes:
                    nodes.append(node)
        return techs, nodes

    def _gid(self, name: str, techs: list[str], nodes: list[str], scope: str) -> str:
        gid = sanitise(name)
        signature = (tuple(techs), tuple(nodes))
        if gid in self._signatures and self._signatures[gid] != signature:
            self._redefined.append(name)
            gid = f"{gid}__{sanitise(scope)}"
        self._signatures.setdefault(gid, signature)
        return gid

    def _convert_group(self, name: str, definition: dict, scope: str) -> GroupDefinition | None:
        techs, nodes = self._members(definition, scope, name)
        gid = self._gid(name, techs, nodes, scope)
        group = GroupDefinition(name=name, gid=gid, techs=techs, nodes=nodes)
        for key, value in definition.items():
            if key in ("techs", "locs", "exists"):
                if key == "exists" and value is False:
                    self.diag.warn("group_exists", f"Group `{name}` has `exists: false`; it is skipped.", scope)
                    return None
                continue
            if key == DSPT:
                self._dspt(group, value, scope)
                continue
            base, _, sense = key.rpartition("_")
            if sense not in SENSES or base not in GROUP_TYPES:
                self.diag.warn("unknown_group_constraint", f"Group `{name}`: `{key}` is not supported; skipped.", scope)
                continue
            vtype, keyed = GROUP_TYPES[base]
            entries = value if keyed else {None: value}
            if keyed and not isinstance(value, dict):
                self.diag.warn("bad_value", f"Group `{name}`: `{key}` must be keyed by {keyed}; skipped.", scope)
                continue
            for entry_key, entry_value in entries.items():
                self._add_item(group, vtype, sense, entry_key, entry_value, base, scope)
        self.definitions[gid] = group
        if group.dspt:
            self.dspt_groups[gid] = group
        return group

    def _add_item(self, group, vtype, sense, key, value, base, scope) -> None:
        try:
            value = float(value)
        except (TypeError, ValueError):
            self.diag.warn("bad_value", f"Group `{group.name}`: value `{value}` is not numeric; skipped.", scope)
            return
        if base == "carrier_con":
            value = -value  # v0.6 consumption is negative, v0.7 flow_in is positive
        senses = ["min", "max"] if sense == "equals" else [sense]
        for s in senses:
            if (s == "max" and value == math.inf) or (s == "min" and value == -math.inf):
                continue  # no constraint
            if math.isnan(value) or math.isinf(value):
                self.diag.warn("bad_value", f"Group `{group.name}`: non-finite `{s}` value; skipped.", scope)
                continue
            item = GroupItem(group.gid, vtype, s, str(key) if key is not None else None, value)
            group.items.append(item)
            self.items[item.param] = item
            if vtype.startswith("cost") and key is not None:
                self.cost_classes.add(str(key))

    def _dspt(self, group: GroupDefinition, value: Any, scope: str) -> None:
        if not isinstance(value, dict):
            self.diag.warn("bad_value", f"Group `{group.name}`: `{DSPT}` must be keyed by carrier; skipped.", scope)
            return
        for carrier, share in value.items():
            try:
                share = float(share)
            except (TypeError, ValueError):
                self.diag.warn("bad_value", f"Group `{group.name}`: `{DSPT}.{carrier}` is not numeric.", scope)
                continue
            group.dspt[str(carrier)] = share
            nodes = group.nodes or self.all_nodes()
            for node in nodes:
                for tech in group.techs:
                    self.dspt_members.add((node, tech, str(carrier)))

    # -------------------------------------------------------------- families
    def contract_families(self, scopes: list[GroupData], labels: list[str]) -> None:
        """Merge per-region copies of a legacy group into one group with node-indexed values.

        A family: groups of one scope with exactly one location each (a different one per copy), the same techs and
        the same constraint types/keys, and defined nowhere else (so no override can change one copy alone). Names
        do not matter (`ALB_1_diesel`, `AUT_2_diesel`, `biofuel_ALB`, ...); if two such groups share a location, only
        copies whose names match once the number is removed (`biofuel_1..4`) are merged.
        Per-node sums over the family's techs are exactly the per-group sums of the copies.
        """
        name_count: dict[str, int] = defaultdict(int)
        for data in scopes:
            for group in data.groups:
                name_count[group.name] += 1
        for data, label in zip(scopes, labels, strict=True):
            candidates: dict[tuple, list[GroupDefinition]] = defaultdict(list)
            for group in data.groups:
                if len(group.nodes) != 1 or name_count[group.name] > 1:
                    continue
                if group.gid != sanitise(group.name):
                    continue  # redefined elsewhere with other members
                items = tuple(sorted((i.vtype, i.sense, i.key or "") for i in group.items))
                dspt = tuple(sorted((c, math.isfinite(v)) for c, v in group.dspt.items()))
                candidates[(tuple(group.techs), items, dspt)].append(group)
            for copies in (f for c in candidates.values() for f in _one_per_node(c)):
                if len(copies) < 2:
                    continue
                merged = self._merge(_family_id(_family_name(copies), label), copies)
                index = data.groups.index(copies[0])
                data.groups = [g for g in data.groups if g not in copies]
                data.groups.insert(index, merged)
                names = [g.name for g in copies]
                shown = names if len(names) <= 5 else [*names[:3], "...", names[-1]]
                self.diag.info(
                    "group_family",
                    f"{len(names)} groups {shown} (one location each) -> one group `{merged.gid}` "
                    "with values indexed on nodes.",
                    label,
                )

    def _merge(self, family: str, copies: list[GroupDefinition]) -> GroupDefinition:
        gid = sanitise(family)
        while gid in self.definitions:
            gid += "_group"
        merged = GroupDefinition(
            name=family, gid=gid, techs=list(copies[0].techs), nodes=[], sources=[g.name for g in copies]
        )
        for item in copies[0].items:
            new = GroupItem(gid, item.vtype, item.sense, item.key, None)
            merged.items.append(new)
            merged.node_values[new.param] = {}
            self.items[new.param] = new
        for copy in copies:
            node = copy.nodes[0]
            for item in copy.items:
                param = GroupItem(gid, item.vtype, item.sense, item.key, None).param
                merged.node_values[param][node] = item.value
                self.items.pop(item.param, None)
            for carrier, share in copy.dspt.items():
                merged.dspt[carrier] = share
                merged.node_values.setdefault(f"group_{gid}__dspt__{sanitise(carrier)}", {})[node] = share
            self.definitions.pop(copy.gid, None)
            self.dspt_groups.pop(copy.gid, None)
        self.definitions[gid] = merged
        if merged.dspt:
            self.dspt_groups[gid] = merged
        return merged

    # ------------------------------------------------------------------- math
    def math(self) -> dict:
        """Generated math covering every group of every scope."""
        parameters: dict[str, dict] = {}
        lookups: dict[str, dict] = {}
        constraints: dict[str, dict] = {}
        needed_expr: set[str] = set()

        for group in self.definitions.values():
            if group.lookup:
                lookups[group.lookup] = {"dtype": "bool", "default": False}
            if group.needs_nodes_lookup:
                lookups[f"group_{group.gid}__nodes"] = {"dtype": "bool", "default": False}

        for item in self.items.values():
            group = self.definitions[item.gid]
            parameters[item.param] = {"default": float("nan"), "description": f"Legacy group `{group.name}`."}
            needed_expr.update(_TYPE_EXPRESSIONS.get(item.vtype, []))
            constraints[item.constraint] = self._constraint(item, group)

        dspt_where: list[str] = []
        for group in self.dspt_groups.values():
            for carrier in group.dspt:
                key = sanitise(carrier)
                param = f"group_{group.gid}__dspt__{key}"
                parameters[param] = {"default": float("nan"), "description": f"Legacy group `{group.name}` ({DSPT})."}
                parameters[f"group_{group.gid}__dspt_sum__{key}"] = {"default": float("nan")}
                dspt_where.append(" AND ".join(([group.lookup] if group.lookup else []) + [_key_in(carrier), param]))
                constraints.update(self._dspt_sum_constraint(group, carrier))
        if dspt_where:
            constraints.update(_dspt_main_constraints(dspt_where))

        out: dict[str, Any] = {}
        if parameters:
            out["parameters"] = parameters
        if lookups:
            out["lookups"] = lookups
        if needed_expr:
            out["global_expressions"] = {k: GLOBAL_EXPRESSIONS[k] for k in GLOBAL_EXPRESSIONS if k in needed_expr}
        if constraints:
            out["constraints"] = constraints
        return out

    @staticmethod
    def _select(array: str, group: GroupDefinition) -> str:
        return f"where({array}, {group.lookup})" if group.lookup else array

    @staticmethod
    def _nodes_only(array: str, group: GroupDefinition) -> str:
        """Select the group's nodes only (denominators of share constraints)."""
        return f"where({array}, group_{group.gid}__nodes)" if group.nodes else array

    def _constraint(self, item: GroupItem, group: GroupDefinition) -> dict:
        """One constraint per item, iterating over its key (carrier / cost class) if any.

        Constraints are only built if the group has an active member and the
        summed array exists (`any(...)` gates, as in Calliope's own math), so a
        group whose members are absent in a scenario is skipped instead of
        producing an empty (constant) constraint.
        """
        spec = _CONSTRAINT_SPECS[item.vtype]
        op = OPS[item.sense]
        p = item.param
        key_dim = "costs" if item.vtype.startswith("cost") else ("carriers" if item.key else None)
        foreach = (
            (["nodes"] if group.per_node else [])
            + ([key_dim] if key_dim else [])
            + (["timesteps"] if spec.per_timestep else [])
        )
        sum_dims = [d for d in spec.dims if d not in foreach]
        over = f"[{', '.join(sum_dims)}]"

        lhs = f"sum({self._select(spec.lhs, group)}, over={over})"
        if spec.lhs_minus:
            lhs += f" - sum({self._select(spec.lhs_minus, group)}, over={over})"
        rhs = p
        if spec.denominator:
            rhs = f"{p} * sum({self._nodes_only(spec.denominator, group)}, over={over})"

        where = [p]
        if key_dim:
            where.append(_key_in(item.key, key_dim))
        where_str = " AND ".join(where) + group.gate() + f" AND any({spec.lhs}, over={over})"
        constraint: dict[str, Any] = {
            "description": f"Legacy v0.6 group constraint{_names(group)} ({item.vtype}_{item.sense}"
            + (f", {item.key}" if item.key else "")
            + ").",
            "where": where_str,
            "equations": [{"expression": f"{lhs} {op} {rhs}"}],
        }
        if foreach:
            constraint["foreach"] = foreach
        return constraint

    def _dspt_sum_constraint(self, group: GroupDefinition, carrier: str) -> dict[str, dict]:
        """Legacy finite share: the decision shares of the group's members sum to it."""
        if not math.isfinite(group.dspt.get(carrier, math.inf)):
            return {}
        sum_param = f"group_{group.gid}__dspt_sum__{sanitise(carrier)}"
        return {
            f"{sum_param}_constraint": {
                "description": f"Legacy group{_names(group)}: decision shares sum to the given share.",
                "foreach": (["nodes"] if group.per_node else []) + ["carriers"],
                "where": f"{sum_param} AND {_key_in(carrier)}"
                + group.gate()
                + f" AND any({DSPT}, over={_sum_over(group)})",
                "equations": [
                    {"expression": f"sum({self._select(DSPT, group)}, over={_sum_over(group)}) == {sum_param}"}
                ],
            }
        }


def _key_in(key: str, dim: str = "carriers") -> str:
    """Where-string selecting one carrier / cost class (safe when it is absent, unlike a slice)."""
    return f"[{key}] in {dim}"


def _dspt_main_constraints(group_wheres: list[str]) -> dict[str, dict]:
    """`demand_share_per_timestep_decision` main constraints: one pair for all groups.

    Each acts on single members (the decision variable is not group-indexed), so the groups only differ in their
    `where`: the group's members, its carrier and its value parameter (defined where the group's override is active).
    """
    where = f"{DSPT} AND (" + " OR ".join(f"({w})" for w in group_wheres) + ")"
    demand = "sum(where(sink_use_equals, carrier_in), over=techs)"
    return {
        f"{DSPT}_main_{sense}_constraint": {
            "description": f"Legacy `{DSPT}` ({sense}) of every group, per node and timestep.",
            "foreach": ["nodes", "techs", "carriers", "timesteps"],
            "where": where,
            "equations": [{"expression": f"flow_out {op} (1 {sign} demand_share_relaxation) * $demand * {DSPT}"}],
            "sub_expressions": {"demand": [{"expression": demand}]},
        }
        for sense, sign, op in (("min", "-", ">="), ("max", "+", "<="))
    }


def _without_number(name: str) -> str | None:
    """`biofuel_1` -> `biofuel`, `1_demand_share_x` -> `demand_share_x`; None if there is no number."""
    family = re.sub(r"^\d+_|_\d+$", "", name)
    return family if family and family != name else None


def _one_per_node(copies: list[GroupDefinition]) -> list[list[GroupDefinition]]:
    """Candidate copies as families with one copy per node.

    All of them if their nodes differ; otherwise (two groups with the same structure at one node) only copies whose
    names match once the number is removed, as long as their nodes differ.
    """
    if len({g.nodes[0] for g in copies}) == len(copies):
        return [copies]
    by_name: dict[str, list[GroupDefinition]] = defaultdict(list)
    for group in copies:
        name = _without_number(group.name)
        if name is not None:
            by_name[name].append(group)
    return [f for f in by_name.values() if len({g.nodes[0] for g in f}) == len(f)]


def _family_name(copies: list[GroupDefinition]) -> str:
    """Name parts shared by every copy, without numbers: `ALB_1_diesel`, `AUT_2_diesel` -> `diesel`.

    Falls back to the constraint type (`flow_in_min`, `dspt`) if the names share nothing.
    """
    names = [sanitise(g.name).split("_") for g in copies]
    common = set(names[0]).intersection(*names[1:])
    family = "_".join(t for t in names[0] if t in common and not t.isdigit())
    if family:
        return family
    item = copies[0].items[0] if copies[0].items else None
    return f"{item.vtype}_{item.sense}" if item else "dspt"


def _family_id(family: str, scope: str) -> str:
    """Families from an override carry its name (`biofuel_maximum_biofuel`), so they never clash across overrides."""
    if scope == "base" or family.startswith(sanitise(scope)):
        return family
    return f"{sanitise(scope)}_{family}"


def _by_node(values: dict[str, float]) -> dict:
    return {"data": list(values.values()), "index": list(values), "dims": "nodes"}


def _sum_over(group: GroupDefinition) -> str:
    return "techs" if group.per_node else "[nodes, techs]"


def _names(group: GroupDefinition) -> str:
    if group.per_node:
        return f"s {', '.join(f'`{n}`' for n in group.sources)} (one per node)"
    return f" `{group.name}`"
