"""Resolve what every legacy technology *is*.

Override fragments are partial (e.g. `techs.battery.constraints.energy_eff: 0.9`),
so converting them correctly needs model-wide knowledge of each tech: its root
abstract base, carriers, conversion_plus tiers and primary carriers. This is
collected once from the base model plus *all* overrides.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from converter.diagnostics import Diagnostics
from converter.legacy import LegacyModel, deep_merge

ABSTRACT_BASES = ("supply", "supply_plus", "demand", "storage", "transmission", "conversion", "conversion_plus")
V07_BASE_TECH = {
    "supply": "supply",
    "supply_plus": "supply",
    "demand": "demand",
    "storage": "storage",
    "transmission": "transmission",
    "conversion": "conversion",
    "conversion_plus": "conversion",
}
IN_TIERS = ("in", "in_2", "in_3")
OUT_TIERS = ("out", "out_2", "out_3")
STORAGE_KEYS = (
    "storage_cap_max",
    "storage_cap_min",
    "storage_cap_equals",
    "storage_cap_per_unit",
    "storage_initial",
    "storage_loss",
    "energy_cap_per_storage_cap_max",
    "energy_cap_per_storage_cap_min",
    "energy_cap_per_storage_cap_equals",
    "charge_rate",
)


def as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def template_name(group: str) -> str:
    """v0.7 template name of a v0.6 tech_group.

    Groups that extend a built-in abstract base (e.g. `tech_groups.conversion`)
    get a `base_` prefix so the name does not look like a `base_tech` value.
    """
    return f"base_{group}" if group in ABSTRACT_BASES else group


@dataclass
class TechInfo:
    """Model-wide resolved information about one legacy tech or tech_group."""

    name: str
    is_group: bool
    chain: list[str]  # parents from nearest to root, excluding the abstract base itself
    abstract: str | None  # root v0.6 abstract base, e.g. "conversion_plus"
    tiers: dict[str, list[str]] = field(default_factory=dict)  # "in", "out_2", ... -> carriers
    primary_in: str | None = None
    primary_out: str | None = None
    force_resource: bool = False
    include_storage: bool = False
    base_constraints: dict = field(default_factory=dict)  # resolved from the base model only
    base_costs: dict = field(default_factory=dict)  # resolved from the base model only (incl. tech_groups)

    @property
    def base_tech(self) -> str | None:
        return V07_BASE_TECH.get(self.abstract) if self.abstract else None

    @property
    def carriers_in(self) -> list[str]:
        return [c for t in IN_TIERS for c in self.tiers.get(t, [])]

    @property
    def carriers_out(self) -> list[str]:
        return [c for t in OUT_TIERS for c in self.tiers.get(t, [])]

    @property
    def is_defined(self) -> bool:
        return self.is_group or self.abstract is not None

    @property
    def is_conversion_plus(self) -> bool:
        return self.abstract == "conversion_plus"

    @property
    def is_transmission(self) -> bool:
        return self.abstract == "transmission"

    @property
    def cap_carrier(self) -> str | None:
        """Carrier on which v0.6 `energy_cap` (and its costs) is defined."""
        if self.abstract in ("conversion", "conversion_plus"):
            return self.primary_out
        if self.abstract == "demand":
            return self.primary_in
        return self.primary_out or self.primary_in

    @property
    def parent_template(self) -> str | None:
        """The v0.7 template this tech/group should point to (nearest parent group)."""
        return template_name(self.chain[0]) if self.chain else None


class TechContext:
    """Lookup of `TechInfo` for every tech and tech_group defined anywhere in the model."""

    def __init__(self, legacy: LegacyModel, diag: Diagnostics):
        self.diag = diag
        self.groups: dict[str, dict] = {}
        self.techs: dict[str, dict] = {}
        self.base_groups: dict[str, dict] = dict(legacy.base.get("tech_groups") or {})
        self.base_techs: dict[str, dict] = dict(legacy.base.get("techs") or {})
        self._collect(legacy)
        self.info: dict[str, TechInfo] = {}
        for name in list(self.groups) + list(self.techs):
            self.info[name] = self._resolve(name)
        self._detect_storage(legacy)

    # ------------------------------------------------------------------ build
    def _collect(self, legacy: LegacyModel) -> None:
        sources = [(None, legacy.base)] + list(legacy.overrides.items())
        for scope, data in sources:
            for section, target in (("tech_groups", self.groups), ("techs", self.techs)):
                for name, definition in (data.get(section) or {}).items():
                    definition = definition or {}
                    if not isinstance(definition, dict):
                        self.diag.warn("bad_tech", f"`{section}.{name}` is not a mapping; ignored.", scope)
                        continue
                    if name in target:
                        # base definition takes precedence for identity (essentials)
                        target[name] = deep_merge(definition, target[name])
                    else:
                        target[name] = definition

    def _parent(self, name: str) -> str | None:
        definition = self.techs.get(name) if name in self.techs else self.groups.get(name, {})
        parent = (definition or {}).get("essentials", {}).get("parent")
        return str(parent) if parent is not None else None

    def _resolve(self, name: str) -> TechInfo:
        is_group = name in self.groups and name not in self.techs
        chain: list[str] = []
        abstract = name if (is_group and name in ABSTRACT_BASES) else None
        current = name
        seen = {name}
        while abstract is None:
            parent = self._parent(current)
            if parent is None:
                break
            if parent in ABSTRACT_BASES:
                abstract = parent
                if parent in self.groups:
                    chain.append(parent)  # extension of a built-in base
                break
            if parent in seen:
                self.diag.warn("circular_parent", f"Circular `parent` chain for `{name}`.")
                break
            if parent not in self.groups:
                self.diag.warn("unknown_parent", f"`{name}` inherits from undefined tech_group `{parent}`.")
                break
            seen.add(parent)
            chain.append(parent)
            current = parent
        if abstract is None and not is_group:
            if self._parent(name) is None and not any(self._parent(g) for g in chain):
                self.diag.warn(
                    "undefined_tech",
                    f"Tech `{name}` is modified (e.g. in an override) but never defined with a `parent` in this model; "
                    "its fragments are skipped.",
                )
            else:
                self.diag.warn(
                    "unknown_base", f"Could not resolve the abstract base (supply, demand, ...) of tech `{name}`."
                )

        # Merge essentials/constraints from root to leaf.
        merged: dict = {}
        for member in reversed(chain):
            merged = deep_merge(merged, self.groups.get(member, {}) or {})
        merged = deep_merge(merged, (self.techs.get(name) if name in self.techs else self.groups.get(name)) or {})
        essentials = merged.get("essentials", {}) or {}
        constraints = merged.get("constraints", {}) or {}

        info = TechInfo(name=name, is_group=is_group, chain=chain, abstract=abstract)
        base_merged: dict = {}
        for member in reversed(chain):
            base_merged = deep_merge(base_merged, self.base_groups.get(member) or {})
        own = self.base_techs.get(name) if name in self.techs else self.base_groups.get(name)
        base_merged = deep_merge(base_merged, own or {})
        info.base_constraints = base_merged.get("constraints", {}) or {}
        info.base_costs = base_merged.get("costs", {}) or {}
        info.tiers = self._tiers(essentials, abstract)
        info.primary_out = self._primary(essentials, info, "out")
        info.primary_in = self._primary(essentials, info, "in")
        default_force = abstract == "demand"
        info.force_resource = bool(constraints.get("force_resource", default_force))
        if abstract == "conversion_plus" and info.tiers.get("out") and len(info.tiers["out"]) > 1:
            if not essentials.get("primary_carrier_out"):
                self.diag.warn(
                    "missing_primary_carrier",
                    f"conversion_plus tech `{name}` has several output carriers but no `primary_carrier_out`; "
                    f"using `{info.primary_out}`.",
                )
        return info

    @staticmethod
    def _tiers(essentials: dict, abstract: str | None) -> dict[str, list[str]]:
        tiers: dict[str, list[str]] = {}
        carrier = as_list(essentials.get("carrier"))
        for tier in IN_TIERS:
            key = "carrier_in" if tier == "in" else f"carrier_{tier}"
            tiers[tier] = as_list(essentials.get(key))
        for tier in OUT_TIERS:
            key = "carrier_out" if tier == "out" else f"carrier_{tier}"
            tiers[tier] = as_list(essentials.get(key))
        if carrier:
            if abstract in ("supply", "supply_plus"):
                tiers["out"] = tiers["out"] or carrier
            elif abstract == "demand":
                tiers["in"] = tiers["in"] or carrier
            elif abstract in ("storage", "transmission"):
                tiers["in"] = tiers["in"] or carrier
                tiers["out"] = tiers["out"] or carrier
        return {k: v for k, v in tiers.items() if v}

    @staticmethod
    def _primary(essentials: dict, info: TechInfo, direction: str) -> str | None:
        explicit = essentials.get(f"primary_carrier_{direction}")
        if explicit:
            return str(explicit)
        tier1 = info.tiers.get(direction, [])
        if tier1:
            return tier1[0]
        others = info.carriers_out if direction == "out" else info.carriers_in
        return others[0] if others else None

    def _detect_storage(self, legacy: LegacyModel) -> None:
        """supply_plus techs get `include_storage` if any storage parameter is set anywhere."""
        candidates = {n for n, i in self.info.items() if i.abstract == "supply_plus"}
        if not candidates:
            return

        def scan(fragment: dict) -> None:
            for name, tech in ((fragment or {}).get("techs") or {}).items():
                if name in candidates and isinstance(tech, dict):
                    if any(k in (tech.get("constraints") or {}) for k in STORAGE_KEYS):
                        self.info[name].include_storage = True
                        for dependent in self.descendants(name):
                            self.info[dependent].include_storage = True

        for data in [legacy.base, *legacy.overrides.values()]:
            scan(data)
            scan({"techs": data.get("tech_groups") or {}})
            for loc in (data.get("locations") or {}).values():
                scan(loc or {})

    # ----------------------------------------------------------------- lookup
    def get(self, name: str) -> TechInfo | None:
        return self.info.get(name)

    def descendants(self, group: str) -> list[str]:
        """All concrete techs whose parent chain contains `group`."""
        return [n for n, i in self.info.items() if not i.is_group and group in i.chain]

    @property
    def tech_names(self) -> list[str]:
        return [n for n, i in self.info.items() if not i.is_group]

    @property
    def transmission_techs(self) -> list[str]:
        return [n for n in self.tech_names if self.info[n].is_transmission]
