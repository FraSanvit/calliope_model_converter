"""Static analysis of a legacy `run.py` (pyomo custom constraints).

The custom math is not translated (yet): the report lists every custom
constraint the legacy run script would add, what triggers it, which inputs it
reads, which techs it would apply to in this model, and the closest Calliope
math-gallery entry.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from converter import rules
from converter.diagnostics import Diagnostics

# Attributes of the pyomo backend model that are standard v0.6 components.
_STANDARD = {
    "carrier_prod",
    "carrier_con",
    "energy_cap",
    "storage_cap",
    "storage",
    "timesteps",
    "timestep_resolution",
    "timestep_weights",
    "techs",
    "carriers",
    "dual",
    "loc_techs_conversion_plus",
    "loc_techs_conversion",
    "loc_techs_storage",
    "loc_tech_carriers_prod",
    "loc_tech_carriers_con",
    "month_numbers",
    "resource_con",
}


@dataclass
class CustomConstraint:
    name: str  # pyomo constraint name as added to the backend
    builder: str  # python function adding it
    trigger: str  # condition (source) under which it is added
    trigger_params: list[str] = field(default_factory=list)  # legacy params tested in the trigger
    trigger_tech_patterns: list[str] = field(default_factory=list)  # substrings of tech names tested
    reads: list[str] = field(default_factory=list)  # non-standard inputs/variables read by the rule
    gallery: str | None = None
    note: str = ""
    applies_to: list[str] = field(default_factory=list)  # techs/scopes in this model


def _strings(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


class _Visitor:
    def __init__(self, tree: ast.Module):
        self.functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def triggers(self) -> list[tuple[str, str, ast.expr]]:
        """(called builder function, condition source, condition node) in `add_eurocalliope_constraints`."""
        dispatcher = self.functions.get("add_eurocalliope_constraints")
        out: list[tuple[str, str, ast.expr]] = []
        if dispatcher is None:
            return out
        for node in ast.walk(dispatcher):
            if isinstance(node, ast.If):
                for stmt in node.body:
                    for call in ast.walk(stmt):
                        if (
                            isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Name)
                            and call.func.id.startswith("add_")
                        ):
                            out.append((call.func.id, ast.unparse(node.test), node.test))
        return out

    def constraints_added(self, builder: str) -> list[tuple[str, ast.FunctionDef | None]]:
        func = self.functions.get(builder)
        if func is None:
            return []
        out = []
        for call in ast.walk(func):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "add_constraint"
                and call.args
            ):
                name_node = call.args[0]
                if isinstance(name_node, ast.Constant):
                    names = [name_node.value]
                elif isinstance(name_node, ast.JoinedStr):
                    names = [ast.unparse(name_node)]
                else:
                    names = [ast.unparse(name_node)]
                rule = None
                if len(call.args) >= 3:
                    rule_node = call.args[2]
                    rule_name = rule_node.id if isinstance(rule_node, ast.Name) else None
                    if rule_name:
                        rule = next(
                            (n for n in ast.walk(func) if isinstance(n, ast.FunctionDef) and n.name == rule_name), None
                        )
                out.extend((n, rule or func) for n in names)
        return out

    @staticmethod
    def reads(func: ast.AST) -> list[str]:
        found: list[str] = []
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "backend_model"
            ):
                if node.attr not in _STANDARD and not node.attr.startswith("_"):
                    found.append(node.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "get_param":
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    found.append(str(node.args[1].value))
                elif len(node.args) >= 2 and isinstance(node.args[1], ast.JoinedStr):
                    found.append(ast.unparse(node.args[1]))
        return sorted(set(found))


def _clean_names(name: str) -> list[str]:
    """Strip f-string syntax and expand the `{sense}` placeholder used in run.py."""
    name = name.strip()
    if name.startswith(("f'", 'f"')):
        name = name[2:-1]
    if "{sense}" in name:
        return [name.replace("{sense}", s) for s in ("min", "max", "equals")]
    return [name]


STORAGE_EV_SET = "loc_tech_ev_storage_constraint"


def storage_ev_name_filter(path: Path) -> str | None:
    """The substring run.py requires in battery names for its `storage_ev_constraint` set (None if not found).

    Read from the set definition `coords["loc_tech_ev_storage_constraint"] = [... if "<x>" in <name>.split("::")[1]]`.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.ListComp)):
            continue
        if not any(STORAGE_EV_SET in ast.unparse(target) for target in node.targets):
            continue
        for sub in ast.walk(node.value):
            if (
                isinstance(sub, ast.Compare)
                and isinstance(sub.left, ast.Constant)
                and isinstance(sub.left.value, str)
                and isinstance(sub.ops[0], ast.In)
                and ".split(" in ast.unparse(sub.comparators[0])
            ):
                return sub.left.value
    return None


def inspect_run_py(path: Path, diag: Diagnostics) -> list[CustomConstraint]:
    if not path.exists():
        diag.info("no_run_py", f"No run script at {path.name}; no custom math to report.")
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as err:
        diag.warn("bad_run_py", f"Could not parse {path.name}: {err}")
        return []
    visitor = _Visitor(tree)
    out: list[CustomConstraint] = []
    for builder, condition, test in visitor.triggers():
        strings = _strings(test)
        params: list[str] = []
        patterns: list[str] = []
        if "data_vars" in condition:
            params = [f"{s}*" for s in strings] if ".startswith" in condition else strings
        elif "techs" in condition:
            patterns = strings
        reads = sorted({r for raw in visitor.reads(visitor.functions[builder]) for r in _clean_names(raw)})
        for raw_name, _rule in visitor.constraints_added(builder):
            for name in _clean_names(raw_name):
                gallery, note = rules.RUNPY_GALLERY.get(name, (None, ""))
                out.append(
                    CustomConstraint(
                        name=name,
                        builder=builder,
                        trigger=condition,
                        trigger_params=params,
                        trigger_tech_patterns=patterns,
                        reads=reads,
                        gallery=gallery,
                        note=note,
                    )
                )
    if not out:
        diag.warn("no_custom_math_found", f"No custom constraints detected in {path.name}; check it manually.")
    return out


def find_applications(constraints: list[CustomConstraint], legacy, ctx) -> None:
    """Fill `applies_to` with the techs (and scopes) that trigger each custom constraint."""
    usage: dict[str, set[str]] = {}

    def scan(data: dict, scope: str) -> None:
        def visit_tech(tech: str, fragment) -> None:
            info = ctx.get(tech.split("@")[0])
            if not isinstance(fragment, dict) or info is None or not info.is_defined:
                return  # techs not defined in this model are skipped by the conversion
            for key in fragment.get("constraints") or {}:
                usage.setdefault(key, set()).add(f"{tech} ({scope})")

        for tech, fragment in (data.get("techs") or {}).items():
            visit_tech(str(tech), fragment)
        for tech, fragment in (data.get("tech_groups") or {}).items():
            visit_tech(str(tech), fragment)
        for loc, loc_def in (data.get("locations") or {}).items():
            for tech, fragment in ((loc_def or {}).get("techs") or {}).items():
                visit_tech(f"{tech}@{loc}", fragment)

    scan(legacy.base, "base")
    for name, data in legacy.overrides.items():
        scan(data, f"override {name}")

    for constraint in constraints:
        hits: set[str] = set()
        for param in constraint.trigger_params:
            if param.endswith("*"):
                prefix = param[:-1]
                for key, users in usage.items():
                    if key.startswith(prefix):
                        hits.update(users)
            else:
                hits.update(usage.get(param, set()))
        for pattern in constraint.trigger_tech_patterns:
            hits.update(t for t in ctx.tech_names if pattern in t)
        constraint.applies_to = sorted(hits)
