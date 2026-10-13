"""Convert a v0.6 tech definition fragment into v0.7 parameters.

A fragment is any partial v0.6 tech definition: a full `techs.X` entry, a
`tech_groups.X` entry, a `locations.N.techs.X` entry, a `links.A,B.techs.X`
entry, or the same inside an override.

Outputs are split three ways:
- `params`: flat v0.7 YAML keys for the tech/template/node definition;
- `cost_rows`: costs, written to data tables (tables merge element-wise, which
  reproduces v0.6's deep-merge of cost classes across overrides);
- `timeseries`: `file=` references, written as data tables.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from converter import rules
from converter.context import IN_TIERS, OUT_TIERS, TechContext, TechInfo
from converter.diagnostics import Diagnostics

MEMBER_CARRIER = "__member_carrier__"
PRIMARY_CARRIER_OUT = "primary_carrier_out"  # bool lookup on multi-output techs (see groups.py)  # placeholder for costs defined on tech_groups


# Legacy conversion_plus tiers 2/3 and the tier-1 carriers they are linked to (same side).
LINK_TIERS = (("out_2", "out"), ("out_3", "out"), ("in_2", "in"), ("in_3", "in"))


def tier_name(tier: str) -> str:
    """Neutral name of a legacy tier: `carrier_out_1`, `carrier_in_2`, ..."""
    return f"carrier_{tier}" if tier[-1].isdigit() else f"carrier_{tier}_1"


def tier_reference(info: TechInfo, tier: str) -> str:
    """Reference carrier of a tier-1 side: the primary carrier if it is in the tier, else the first one."""
    carriers = info.tiers.get(tier, [])
    primary = info.primary_out if tier == "out" else info.primary_in
    return primary if primary in carriers else carriers[0]


def ratio_param(tier: str, base: str) -> str:
    """Neutral ratio of a tier to the tier-1 carriers, e.g. `carrier_out_2_to_carrier_out_1_ratio`."""
    return f"{tier_name(tier)}_to_{tier_name(base)}_ratio"


@dataclass(frozen=True)
class CostRow:
    tech: str
    cost: str
    param: str
    value: Any  # float, or a TimeseriesRef for time-varying costs
    carrier: str | None = None
    node: str | None = None


@dataclass(frozen=True)
class TimeseriesRef:
    file: str
    param: str
    tech: str
    node: str | None = None
    column: str | None = None
    transform: str | None = None  # None | "negate" | "invert"
    extra_dims: tuple[tuple[str, str], ...] = ()
    factor: float | None = None  # multiply the (transformed) values


@dataclass
class FragmentResult:
    params: dict[str, Any] = field(default_factory=dict)
    cost_rows: list[CostRow] = field(default_factory=list)
    timeseries: list[TimeseriesRef] = field(default_factory=list)
    spores_tracked: bool = False

    def extend(self, other: FragmentResult) -> None:
        self.params.update(other.params)
        self.cost_rows.extend(other.cost_rows)
        self.timeseries.extend(other.timeseries)
        self.spores_tracked = self.spores_tracked or other.spores_tracked


def parse_file_ref(value: Any) -> tuple[str, str | None] | None:
    """Return (file, column) if `value` is a v0.6 `file=` reference."""
    if not isinstance(value, str):
        return None
    text = value.strip().strip("\"'")
    if not text.startswith("file="):
        return None
    ref = text[len("file=") :]
    if ":" in ref and not ref.lower().endswith(".csv"):
        file, column = ref.rsplit(":", 1)
        return file, column
    return ref, None


def _indexed(values: dict[str, Any], dim: str) -> dict | Any:
    if len(values) == 1:
        ((index, data),) = values.items()
        return {"data": data, "index": index, "dims": dim}
    return {"data": list(values.values()), "index": list(values.keys()), "dims": dim}


class FragmentConverter:
    """Converts fragments for one model, sharing the tech context and diagnostics."""

    SPORES_SCORE_CLASS = "spores_score"

    def __init__(self, ctx: TechContext, diag: Diagnostics):
        self.ctx = ctx
        self.diag = diag

    # ----------------------------------------------------------------- public
    def convert(
        self,
        name: str,
        fragment: dict | None,
        *,
        scope: str,
        node: str | None = None,
        target: str | None = None,
        defining: bool = False,
    ) -> FragmentResult:
        """Convert a fragment of tech/tech_group `name`.

        Args:
            name: legacy tech or tech_group name (used for the TechInfo lookup).
            fragment: the v0.6 definition fragment (may be None, e.g. `techs: {pv: }`).
            scope: label for diagnostics (file or override name).
            node: node name for location-level fragments.
            target: tech name to attach costs/timeseries to (defaults to `name`;
                differs for transmission link techs).
            defining: True if this fragment (re)defines the tech identity
                (has `essentials`), so base_tech/carriers/template are emitted.
        """
        result = FragmentResult()
        fragment = fragment or {}
        if not isinstance(fragment, dict):
            self.diag.warn("bad_fragment", f"Definition of `{name}` is not a mapping; ignored.", scope)
            return result
        info = self.ctx.get(name)
        if info is None:
            self.diag.warn("unknown_tech", f"`{name}` is not defined as a tech or tech_group anywhere.", scope)
            info = TechInfo(name=name, is_group=False, chain=[], abstract=None)
        target = target or name

        for key, value in fragment.items():
            if key == "essentials":
                result.params.update(self._essentials(info, value or {}, scope, defining or "parent" in (value or {})))
            elif key == "constraints":
                result.extend(self._constraints(info, value or {}, scope, node, target))
            elif key == "costs":
                result.extend(self._costs(info, value or {}, scope, node, target))
            elif key == "exists":
                result.params["active"] = bool(value)
            elif key in ("x", "y"):
                continue
            elif key == "distance" and info.is_transmission:
                result.params["distance"] = value  # v0.6 link-level `links.A,B.techs.<tech>.distance`
            else:
                self.diag.warn("unknown_key", f"`{name}.{key}` is not a v0.6 tech key; copied unchanged.", scope)
                result.params[key] = value
        constraints = fragment.get("constraints") or {}
        if not isinstance(constraints, dict):
            constraints = {}
        if (
            info.is_conversion_plus
            and not info.is_group
            and (defining or "energy_eff" in constraints or "carrier_ratios" in constraints)
        ):
            result.extend(self._conversion_plus(info, constraints, scope, node, target, defining))
        return result

    # ------------------------------------------------------------- essentials
    def _essentials(self, info: TechInfo, essentials: dict, scope: str, defining: bool) -> dict:
        params: dict[str, Any] = {}
        if "name" in essentials:
            params["name"] = essentials["name"]
        if "color" in essentials:
            params["color"] = essentials["color"]
        carrier_keys = {k for k in essentials if k.startswith("carrier") or k.startswith("primary_carrier")}
        if info.is_group:
            # Templates only carry what the group itself defines.
            tiers = self.ctx._tiers(essentials, info.abstract)
            carriers_in = [c for t in IN_TIERS for c in tiers.get(t, [])]
            carriers_out = [c for t in OUT_TIERS for c in tiers.get(t, [])]
        elif defining or carrier_keys:
            # Concrete techs always carry their fully resolved carriers.
            carriers_in, carriers_out = info.carriers_in, info.carriers_out
        else:
            carriers_in, carriers_out = [], []
        if carriers_in:
            params["carrier_in"] = carriers_in[0] if len(carriers_in) == 1 else carriers_in
        if carriers_out:
            params["carrier_out"] = carriers_out[0] if len(carriers_out) == 1 else carriers_out
        if defining:
            if not info.is_group and info.base_tech:
                params["base_tech"] = info.base_tech
                if info.include_storage:
                    params["include_storage"] = True
                if len(info.carriers_out) > 1 and info.cap_carrier:
                    # v0.6 had one `energy_cap`: capacity constraints apply to the primary output carrier only.
                    params[PRIMARY_CARRIER_OUT] = {"data": True, "index": info.cap_carrier, "dims": "carriers"}
            if info.parent_template:
                params["template"] = info.parent_template
        unknown = set(essentials) - {"name", "color", "parent"} - carrier_keys
        for key in sorted(unknown):
            self.diag.warn("unknown_key", f"Unknown essentials key `{info.name}.essentials.{key}`; dropped.", scope)
        return params

    # ------------------------------------------------------------ constraints
    def _constraints(
        self, info: TechInfo, constraints: dict, scope: str, node: str | None, target: str
    ) -> FragmentResult:
        result = FragmentResult()
        for key, value in constraints.items():
            if key in rules.DROPPED_PARAMS:
                continue
            if key in rules.UNSUPPORTED_PARAMS:
                self.diag.warn(
                    "unsupported_param", f"`{info.name}.{key}`: {rules.UNSUPPORTED_PARAMS[key]}; dropped.", scope
                )
                continue
            if key == "resource":
                self._resource(info, value, constraints, scope, node, target, result)
            elif key == "resource_unit":
                unit = rules.RESOURCE_UNITS.get(str(value))
                if unit is None:
                    self.diag.warn("bad_value", f"`{info.name}.resource_unit: {value}` not recognised; dropped.", scope)
                else:
                    result.params["sink_unit" if info.abstract == "demand" else "source_unit"] = unit
            elif key == "energy_eff" and info.is_conversion_plus and not info.is_group:
                continue  # combined with the tier-1 carrier ratios in `_conversion_plus`
            elif key == "energy_eff":
                for param in self._eff_params(info):
                    self._set(result, info, param, value, scope, node, target)
            elif key == "carrier_ratios" and info.is_group:
                self.diag.info(
                    "group_carrier_ratios",
                    f"carrier_ratios on tech_group `{info.name}` are resolved onto each member tech.",
                    scope,
                )
            elif key == "carrier_ratios":
                if not info.is_conversion_plus:
                    self.diag.warn(
                        "bad_param", f"`carrier_ratios` on non-conversion_plus tech `{info.name}`; dropped.", scope
                    )
            elif key == "export_carrier":
                result.params["carrier_export"] = value
            elif key in rules.EQUALS_RENAMES:
                for param in rules.EQUALS_RENAMES[key]:
                    self._set(result, info, param, value, scope, node, target)
            elif key in rules.PARAM_RENAMES:
                param = rules.PARAM_RENAMES[key]
                if key.startswith("units"):
                    self.diag.warn(
                        "milp", f"`{info.name}.{key}` needs the `milp` math and `cap_method: integer` in v0.7.", scope
                    )
                self._set(result, info, param, value, scope, node, target)
            elif key in rules.CUSTOM_PARAMS:
                param, gallery = rules.CUSTOM_PARAMS[key]
                self.diag.info(
                    "custom_param", f"`{key}` -> `{param}`" + (f" (math gallery: {gallery})" if gallery else ""), scope
                )
                self._set(result, info, param, value, scope, node, target, custom=True)
            else:
                self.diag.warn(
                    "unknown_param",
                    f"`{info.name}.constraints.{key}` has no known v0.7 equivalent; copied unchanged.",
                    scope,
                )
                self._set(result, info, key, value, scope, node, target, custom=True)
        return result

    def _eff_params(self, info: TechInfo) -> list[str]:
        if info.abstract == "storage":
            return ["flow_in_eff", "flow_out_eff"]
        if info.abstract == "demand":
            return ["flow_in_eff"]
        return ["flow_out_eff"]

    def _resource(self, info, value, constraints, scope, node, target, result) -> None:
        force = constraints.get("force_resource", info.force_resource)
        if force and isinstance(value, int | float) and math.isinf(value):
            self.diag.warn(
                "infinite_forced_resource",
                f"`{info.name}` forces an infinite `resource`; converted as an upper bound (`*_use_max`) instead.",
                scope,
            )
            force = False
        if info.abstract == "demand":
            param = "sink_use_equals" if force else "sink_use_max"
            self._set(result, info, param, value, scope, node, target, negate=True)
        elif info.abstract in ("supply", "supply_plus"):
            param = "source_use_equals" if force else "source_use_max"
            self._set(result, info, param, value, scope, node, target)
        else:
            self.diag.warn(
                "bad_param", f"`resource` on `{info.name}` ({info.abstract}) has no v0.7 meaning; dropped.", scope
            )

    def _set(self, result, info, param, value, scope, node, target, *, negate=False, custom=False) -> None:
        file_ref = parse_file_ref(value)
        carrier_dim = None
        if param in rules.CAP_CARRIER_PARAMS and self._multi_carrier(info):
            carrier_dim = info.cap_carrier
        if file_ref is not None:
            extra = (("carriers", carrier_dim),) if carrier_dim else ()
            result.timeseries.append(
                TimeseriesRef(
                    file=file_ref[0],
                    column=file_ref[1],
                    param=param,
                    tech=target,
                    node=node,
                    transform="negate" if negate else None,
                    extra_dims=extra,
                )
            )
            return
        if isinstance(value, str) and value.strip().startswith("df="):
            self.diag.warn(
                "unsupported_df", f"`{info.name}.{param}` uses `df=`, which cannot be converted from files.", scope
            )
            return
        if isinstance(value, dict):
            value = self._flatten_dict_param(info, param, value, scope)
            if value is None:
                return
        elif negate and isinstance(value, int | float):
            value = -value
            if value == 0:
                value = 0.0
        if carrier_dim and not isinstance(value, dict):
            value = {"data": value, "index": carrier_dim, "dims": "carriers"}
        result.params[param] = value

    def _flatten_dict_param(self, info, param, value: dict, scope) -> dict | None:
        """Turn `{tier: {carrier: v}}` / `{carrier: v}` custom params into carrier-indexed params."""
        flat: dict[str, Any] = {}
        for key, inner in value.items():
            if isinstance(inner, dict):
                flat.update({str(c): v for c, v in inner.items()})
            else:
                flat[str(key)] = inner
        if any(parse_file_ref(v) for v in flat.values()) or not flat:
            self.diag.warn("bad_value", f"`{info.name}.{param}` nested value could not be converted; dropped.", scope)
            return None
        return _indexed(flat, "carriers")

    @staticmethod
    def _multi_carrier(info: TechInfo) -> bool:
        return info.abstract in ("conversion", "conversion_plus")

    # --------------------------------------------------------- conversion_plus
    def _conversion_plus(self, info, constraints, scope, node, target, defining) -> FragmentResult:
        """Legacy conversion_plus `energy_eff` + `carrier_ratios` (v0.6 docs: AND across tiers, OR within a tier).

        - tier 1 -> native per-carrier efficiencies: `flow_out_eff = energy_eff * r_out`, `flow_in_eff = r_in`,
          so `sum(flow_out / r) == sum(flow_in * r) * energy_eff` holds through `balance_conversion`;
        - tiers 2/3 -> a neutral ratio per carrier of the tier, `r_carrier / r_reference` (reference: the primary
          tier-1 carrier), used by the `link_*` constraints of `conversion_plus_math`.
        Values are the fragment's on top of the tech's base definition, so overrides stay complete.
        """
        result = FragmentResult()
        ratios = self._ratios(info, constraints, scope)
        given = {str(k).replace("carrier_", "") for k in (constraints.get("carrier_ratios") or {})}
        eff = constraints.get("energy_eff", info.base_constraints.get("energy_eff", 1))
        # Tier-1 efficiencies are always written when a multi-carrier tier 1 is linked to tiers 2/3:
        # the link weighs its carriers by their efficiency ratios.
        weighted = {
            side: len(ratios[side]) > 1 and any(ratios[t] for t in (f"{side}_2", f"{side}_3")) for side in ("out", "in")
        }
        if defining or "energy_eff" in constraints or "out" in given:
            values = {
                c: self._combine(info, eff, r, False, scope, f"flow_out_eff[{c}]") for c, r in ratios["out"].items()
            }
            if defining and not weighted["out"] and all(v == 1 for v in values.values()):
                values = {}
            self._carriers_param(result, "flow_out_eff", values, node, target)
        if "in" in given or (defining and (weighted["in"] or any(r != 1 for r in ratios["in"].values()))):
            values = {
                c: self._combine(info, r, 1.0, False, scope, f"flow_in_eff[{c}]") for c, r in ratios["in"].items()
            }
            self._carriers_param(result, "flow_in_eff", values, node, target)
        if defining or given:
            for tier, base in LINK_TIERS:
                if not ratios[tier] or not ratios[base]:
                    continue
                param = ratio_param(tier, base)
                r_ref = ratios[base][tier_reference(info, base)]
                values = {}
                for carrier, ratio in ratios[tier].items():
                    if ratio == 0:
                        self.diag.warn(
                            "zero_ratio",
                            f"carrier_ratios of `{info.name}`: `carrier_{tier}.{carrier}` is 0"
                            + (
                                ", so that flow is forced to zero."
                                if len(ratios[tier]) == 1
                                else "; v0.6 divides by it within the tier, which v0.7 cannot: not converted."
                            ),
                            scope,
                        )
                        if len(ratios[tier]) > 1:
                            continue
                    values[carrier] = self._combine(info, ratio, r_ref, True, scope, f"{param}[{carrier}]")
                self._carriers_param(result, param, values, node, target)
        return result

    def _ratios(self, info, constraints, scope) -> dict[str, dict[str, Any]]:
        """Ratio of every tier carrier: 1, then the base definition, then this fragment (float or `file=` ref)."""
        ratios: dict[str, dict[str, Any]] = {t: {c: 1.0 for c in info.tiers.get(t, [])} for t in IN_TIERS + OUT_TIERS}
        for source in (info.base_constraints.get("carrier_ratios") or {}, constraints.get("carrier_ratios") or {}):
            if not isinstance(source, dict):
                continue
            for tier_key, carriers in source.items():
                tier = str(tier_key).replace("carrier_", "")
                if tier not in ratios or not isinstance(carriers, dict | None):
                    self.diag.warn("bad_value", f"Unknown carrier_ratios tier `{tier_key}` on `{info.name}`.", scope)
                    continue
                for carrier, ratio in (carriers or {}).items():
                    if str(carrier) in info.duplicate_carriers.get(tier, []):
                        continue  # a repeated carrier, ignored (warned once as `duplicate_carrier`)
                    if str(carrier) not in ratios[tier]:
                        self.diag.warn(
                            "bad_value",
                            f"carrier_ratios `{tier_key}.{carrier}` of `{info.name}`: `{carrier}` is not a "
                            f"`carrier_{tier}` carrier; dropped.",
                            scope,
                        )
                        continue
                    ref = parse_file_ref(ratio)
                    if ref is None:
                        try:
                            ratio = float(ratio)
                        except (TypeError, ValueError):
                            self.diag.warn(
                                "bad_value",
                                f"carrier_ratios `{tier}.{carrier}` of `{info.name}` is not numeric.",
                                scope,
                            )
                            continue
                    ratios[tier][str(carrier)] = ref if ref is not None else ratio
        return ratios

    def _combine(self, info, a: Any, b: Any, divide: bool, scope: str, what: str) -> Any:
        """`a * b` (or `a / b`) where each is a float or a `file=` reference; a TimeseriesRef if one is a file."""
        a = parse_file_ref(a) or a
        ref_a, ref_b = isinstance(a, tuple), isinstance(b, tuple)
        if ref_a and ref_b:
            self.diag.warn(
                "bad_value", f"`{info.name}`: `{what}` combines two time-varying values; not converted.", scope
            )
            return None
        try:
            a = a if ref_a else float(a)
            b = b if ref_b else float(b)
        except (TypeError, ValueError):
            self.diag.warn("bad_value", f"`{info.name}`: `{what}` is not numeric; not converted.", scope)
            return None
        if divide and not ref_b and b == 0:
            self.diag.warn("zero_ratio", f"`{info.name}`: `{what}` divides by a zero ratio; not converted.", scope)
            return None
        if not ref_a and not ref_b:
            return a / b if divide else a * b
        if ref_a:  # a(t) * b or a(t) / b
            factor, transform, ref = (1 / b if divide else b), None, a
        else:  # a * b(t) or a / b(t)
            factor, transform, ref = a, ("invert" if divide else None), b
        return TimeseriesRef(
            file=ref[0], column=ref[1], param="", tech="", transform=transform, factor=None if factor == 1 else factor
        )

    def _carriers_param(self, result: FragmentResult, param: str, values: dict, node, target) -> None:
        static = {}
        for carrier, value in values.items():
            if isinstance(value, TimeseriesRef):
                result.timeseries.append(
                    TimeseriesRef(
                        **{
                            **value.__dict__,
                            "param": param,
                            "tech": target,
                            "node": node,
                            "extra_dims": (("carriers", carrier),),
                        }
                    )
                )
            elif value is not None:
                static[carrier] = value
        if static:
            result.params[param] = _indexed(static, "carriers")

    # ------------------------------------------------------------------ costs
    def _costs(self, info: TechInfo, costs: dict, scope: str, node: str | None, target: str) -> FragmentResult:
        result = FragmentResult()
        interest_rates: dict[str, float] = {}
        for cost_class, entries in costs.items():
            if not isinstance(entries, dict):
                self.diag.warn("bad_value", f"`{info.name}.costs.{cost_class}` is not a mapping; dropped.", scope)
                continue
            if cost_class == self.SPORES_SCORE_CLASS:
                # v0.7 SPORES scores flow_cap internally; legacy spores_score costs only mark tracked techs.
                result.spores_tracked = True
                continue
            for key, value in entries.items():
                param = self._cost_param(info, key, scope)
                if param is None:
                    continue
                carrier = None
                if param in rules.CARRIER_COSTS:
                    carrier = info.primary_in if param == "cost_flow_in" else info.cap_carrier
                    if info.is_group:
                        carrier = MEMBER_CARRIER  # resolved per member tech by the builder
                    elif carrier is None:
                        self.diag.warn(
                            "no_carrier", f"Cannot determine the carrier of `{info.name}` for `{param}`.", scope
                        )
                file_ref = parse_file_ref(value)
                if file_ref is not None:
                    dims = [("costs", str(cost_class))] + ([("carriers", carrier)] if carrier else [])
                    value = TimeseriesRef(
                        file=file_ref[0],
                        column=file_ref[1],
                        param=param,
                        tech=target,
                        node=node,
                        extra_dims=tuple(dims),
                    )
                else:
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        self.diag.warn(
                            "bad_value", f"Cost `{info.name}.{cost_class}.{key}: {value}` is not numeric.", scope
                        )
                        continue
                    if math.isnan(value):
                        continue
                    if param == "cost_interest_rate":
                        # YAML, not the cost tables: set on templates (v0.6 abstract base groups), overridden per tech.
                        interest_rates[str(cost_class)] = value
                        continue
                result.cost_rows.append(CostRow(target, str(cost_class), param, value, carrier, node))
        if interest_rates:
            # A tech-level value replaces the inherited (template) one as a whole: keep the other classes.
            inherited = {
                str(c): e["interest_rate"]
                for c, e in info.base_costs.items()
                if isinstance(e, dict) and "interest_rate" in e and c != self.SPORES_SCORE_CLASS
            }
            result.params["cost_interest_rate"] = _indexed({**inherited, **interest_rates}, "costs")
        return result

    def _cost_param(self, info: TechInfo, key: str, scope: str) -> str | None:
        if key == "om_con":
            return "cost_source_use" if info.abstract in ("supply", "supply_plus") else "cost_flow_in"
        if key in rules.COST_RENAMES:
            return rules.COST_RENAMES[key]
        self.diag.warn("unknown_cost", f"Unknown cost `{info.name}.costs.*.{key}`; dropped.", scope)
        return None


def conversion_plus_math(infos: list[TechInfo]) -> dict:
    """Math gallery CHP form for legacy conversion_plus techs with tier-2/3 carriers.

    `balance_conversion` only keeps the tier-1 carriers of these techs and each tier 2/3 is linked to tier 1,
    e.g. `flow_out[carriers=electricity] * carrier_out_2_to_carrier_out_1_ratio == flow_out[carriers=heat]`.
    Techs with the same carriers share an equation, selected with `[tech, ...] in techs`.
    """
    balances: dict[str, list[str]] = {}
    links: dict[str, dict] = {}
    params: dict[str, dict] = {}
    for info in infos:
        if not any(info.tiers.get(tier) for tier, _ in LINK_TIERS):
            continue  # tier 1 only: the native balance with per-carrier efficiencies is exact
        out_1, in_1 = info.tiers.get("out", []), info.tiers.get("in", [])
        balance = (
            f"{_flows('flow_out_inc_eff', out_1, len(out_1) == len(info.carriers_out))} == "
            f"{_flows('flow_in_inc_eff', in_1, len(in_1) == len(info.carriers_in))}"
        )
        balances.setdefault(balance, []).append(info.name)
        for tier, base in LINK_TIERS:
            carriers, base_carriers = info.tiers.get(tier, []), info.tiers.get(base, [])
            if not carriers or not base_carriers:
                continue
            param = ratio_param(tier, base)
            expression = _link_expression(info, tier, base, param)
            name = f"link_{tier}_{'_'.join(carriers)}_to_{'_'.join(base_carriers)}"
            variants = links.setdefault(name, {})
            variants.setdefault(expression, {"tier": tier, "techs": []})["techs"].append(info.name)
            params[param] = {
                "default": 1,
                "title": f"{tier_name(tier).replace('_', ' ')} to {tier_name(base).replace('_', ' ')} ratio",
                "description": (
                    f"Legacy `carrier_ratios.carrier_{tier}` (per carrier) / `carrier_ratios.carrier_{base}` "
                    "of the reference (primary) tier-1 carrier."
                ),
            }
    constraints: dict[str, dict] = {}
    if balances:
        tiered = sorted(t for techs in balances.values() for t in techs)
        equations = [
            {
                "where": f"NOT {_techs_where(tiered)}",
                "expression": "sum(flow_out_inc_eff, over=carriers) == sum(flow_in_inc_eff, over=carriers)",
            }
        ]
        equations += [{"where": _techs_where(techs), "expression": expr} for expr, techs in balances.items()]
        constraints["balance_conversion"] = {"equations": equations}
    constraints.update(_capacity_constraints(infos))
    if not constraints:
        return {}
    for name, variants in links.items():
        for i, (expression, link) in enumerate(variants.items()):
            constraints[name + (f"_{i + 1}" if i else "")] = {
                "description": f"Legacy conversion_plus: `carrier_{link['tier']}` flows follow the tier-1 flows.",
                "foreach": ["nodes", "techs", "timesteps"],
                "where": _techs_where(link["techs"]),
                "equations": [{"expression": expression}],
            }
    return {"parameters": dict(sorted(params.items())), "constraints": constraints}


def _link_expression(info: TechInfo, tier: str, base: str, param: str) -> str:
    """v0.6 `sum(tier_1 flow / r) == sum(tier_n flow / r)`, scaled by the ratio of the reference tier-1 carrier.

    Tier-1 carriers are weighted by `r_ref / r_c`, i.e. their efficiency ratio (`energy_eff` cancels out);
    one tier-n carrier: `tier_1 * ratio == flow` (gallery CHP form), several: `tier_1 == sum(flow / ratio)`.
    """
    var, eff = ("flow_out", "flow_out_eff") if tier.startswith("out") else ("flow_in", "flow_in_eff")
    ref = tier_reference(info, base)
    terms = [f"{var}[carriers={ref}]"] + [
        f"{var}[carriers={c}] * {eff}[carriers={ref}] / {eff}[carriers={c}]"
        for c in info.tiers.get(base, [])
        if c != ref
    ]
    lhs = " + ".join(terms)
    carriers = info.tiers.get(tier, [])
    if len(carriers) == 1:
        lhs = f"({lhs})" if len(terms) > 1 else lhs
        return f"{lhs} * {param}[carriers={carriers[0]}] == {var}[carriers={carriers[0]}]"
    return f"{lhs} == " + " + ".join(f"{var}[carriers={c}] / {param}[carriers={c}]" for c in carriers)


def _techs_where(techs: list[str]) -> str:
    return f"[{', '.join(sorted(set(techs)))}] in techs"


def _flows(var: str, carriers: list[str], whole: bool) -> str:
    if whole:
        return f"sum({var}, over=carriers)"
    return " + ".join(f"{var}[carriers={c}]" for c in carriers)


def _capacity_constraints(infos: list[TechInfo]) -> dict[str, dict]:
    """v0.6 capacity of conversion_plus techs: one `energy_cap`, the primary output carrier's `flow_cap`.

    - tier-1 outputs share it (`carrier_production_max/min_conversion_plus`), only needed with several tier-1
      outputs (one tier-1 output is the primary: the v0.7 per-carrier constraints are already exact);
    - ramping of every carrier refers to it (v0.6 `energy_ramping` x `energy_cap`).
    """
    shared: dict[tuple, list[str]] = {}  # (tier-1 outputs, primary) -> techs
    ramping: dict[str, list[str]] = {}  # primary -> techs
    for info in infos:
        out_1 = info.tiers.get("out", [])
        if not out_1 or info.cap_carrier is None:
            continue
        if len(out_1) > 1:
            shared.setdefault((tuple(out_1), info.cap_carrier), []).append(info.name)
        if len(info.carriers_in) + len(info.carriers_out) > 1:
            ramping.setdefault(info.cap_carrier, []).append(info.name)
    constraints: dict[str, dict] = {}
    for i, ((out_1, primary), techs) in enumerate(shared.items()):
        flows = " + ".join(f"flow_out[carriers={c}]" for c in out_1)
        cap = f"flow_cap[carriers={primary}] * timestep_resolution"
        suffix = f"_{i + 1}" if len(shared) > 1 else ""
        constraints[f"conversion_plus_flow_out_max{suffix}"] = {
            "description": "Legacy conversion_plus: tier-1 outputs share the capacity of the primary output carrier.",
            "foreach": ["nodes", "techs", "timesteps"],
            "where": _techs_where(techs),
            "equations": [{"expression": f"{flows} <= {cap}"}],
        }
        constraints[f"conversion_plus_flow_out_min{suffix}"] = {
            "description": "Legacy conversion_plus `energy_cap_min_use`: on the sum of tier-1 outputs.",
            "foreach": ["nodes", "techs", "timesteps"],
            "where": f"{_techs_where(techs)} AND any(flow_out_min_relative, over=carriers)",
            "equations": [{"expression": f"{flows} >= {cap} * flow_out_min_relative[carriers={primary}]"}],
        }
    if shared:
        multi = sorted(t for techs in shared.values() for t in techs)
        constraints["flow_out_min"] = {"where": f"flow_cap AND flow_out_min_relative AND NOT {_techs_where(multi)}"}
    if ramping:
        cp = sorted(t for techs in ramping.values() for t in techs)
        flow = [
            {"where": "carrier_out AND NOT carrier_in", "expression": "flow_out / timestep_resolution"},
            {"where": "carrier_in AND NOT carrier_out", "expression": "flow_in / timestep_resolution"},
            {"where": "carrier_in AND carrier_out", "expression": "(flow_out - flow_in) / timestep_resolution"},
        ]
        for direction, template in (
            ("up", "$flow - roll($flow, timesteps=1) <= flow_ramping * {cap}"),
            ("down", "-1 * flow_ramping * {cap} <= $flow - roll($flow, timesteps=1)"),
        ):
            equations = [{"where": f"NOT {_techs_where(cp)}", "expression": template.format(cap="flow_cap")}]
            equations += [
                {"where": _techs_where(techs), "expression": template.format(cap=f"flow_cap[carriers={primary}]")}
                for primary, techs in ramping.items()
            ]
            constraints[f"ramping_{direction}"] = {
                "equations": equations,
                "sub_expressions": {"flow": [dict(e) for e in flow]},
            }
    return constraints


def time_varying_limits_math(static: dict, techs: list[tuple[TechInfo, set[str]]]) -> dict:
    """run.py time-varying limits for conversion techs measured against the one v0.6 `energy_cap`.

    v0.6 limited the sum of tier-1 outputs (or inputs) by `energy_cap` x value(t), i.e. the primary output's
    `flow_cap` in v0.7. The generic per-carrier constraints of `static` are exact for single-carrier techs and the
    primary output; conversion techs with several tier-1 outputs (e.g. v2g) or with an inflow limit get per-tech
    constraints here and are excluded from the generic ones.
    """
    math_def = {
        key: {k: dict(v) if isinstance(v, dict) else v for k, v in value.items()} for key, value in static.items()
    }
    constraints = math_def["constraints"]
    outflow: dict[tuple, list[str]] = {}  # (tier-1 outputs, primary, param) -> techs
    inflow: dict[tuple, list[str]] = {}  # (tier-1 inputs, primary) -> techs
    for info, params in techs:
        out_1, in_1, primary = info.tiers.get("out", []), info.tiers.get("in", []), info.cap_carrier
        if primary is None:
            continue
        if len(out_1) > 1:
            for param in sorted(params - {"flow_in_max_relative_per_ts"}):
                outflow.setdefault((tuple(out_1), primary, param), []).append(info.name)
        if "flow_in_max_relative_per_ts" in params and in_1:
            inflow.setdefault((tuple(in_1), primary), []).append(info.name)
    for name, generic in (
        ("max_time_varying_flow_out", "flow_cap_max_relative_per_ts"),
        ("equals_time_varying_flow_out", "flow_cap_equals_relative_per_ts"),
    ):
        special = sorted({t for (_, _, p), ts in outflow.items() if p == generic for t in ts})
        if special:
            constraints[name]["where"] += f" AND NOT {_techs_where(special)}"
    special_in = sorted({t for ts in inflow.values() for t in ts})
    if special_in:
        constraints["max_time_varying_flow_in"]["where"] += f" AND NOT {_techs_where(special_in)}"
    for i, ((carriers, primary, param), names) in enumerate(outflow.items()):
        op = "==" if "equals" in param else "<="
        kind = "equals" if op == "==" else "max"
        constraints[f"{kind}_time_varying_flow_out_sum_{i + 1}"] = {
            "description": "Legacy time-varying limit on the sum of tier-1 outputs (one v0.6 `energy_cap`).",
            "foreach": ["nodes", "techs", "timesteps"],
            "where": f"{_techs_where(names)} AND {param}",
            "equations": [
                {
                    "expression": f"{' + '.join(f'flow_out[carriers={c}]' for c in carriers)} {op} "
                    f"flow_cap[carriers={primary}] * timestep_resolution * {param}"
                }
            ],
        }
    for i, ((carriers, primary), names) in enumerate(inflow.items()):
        constraints[f"max_time_varying_flow_in_sum_{i + 1}"] = {
            "description": "Legacy time-varying limit on the sum of tier-1 inputs (one v0.6 `energy_cap`).",
            "foreach": ["nodes", "techs", "timesteps"],
            "where": f"{_techs_where(names)} AND flow_in_max_relative_per_ts",
            "equations": [
                {
                    "expression": f"{' + '.join(f'flow_in[carriers={c}]' for c in carriers)} <= "
                    f"flow_cap[carriers={primary}] * timestep_resolution * flow_in_max_relative_per_ts"
                }
            ],
        }
    return math_def if (outflow or inflow) else static
