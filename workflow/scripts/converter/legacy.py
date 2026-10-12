"""Load legacy (Calliope v0.6) model YAML files.

Re-implements the parts of v0.6 YAML semantics the converter needs, without
importing Calliope v0.6:

- `import:` lists, resolved relative to the importing file;
- dotted keys (`a.b.c: 1` == `a: {b: {c: 1}}`);
- `inf` / `-inf` given as strings;
- provenance: the file in which each top-level entity was first defined.
"""

from __future__ import annotations

import copy
import fnmatch
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from converter.diagnostics import ConversionError, Diagnostics

# Top-level sections whose children are tracked individually for provenance.
ENTITY_SECTIONS = ("techs", "tech_groups", "locations", "links", "overrides", "scenarios", "group_constraints")

_INF_STRINGS = {"inf": math.inf, ".inf": math.inf, "-inf": -math.inf, "-.inf": -math.inf}


def _clean_value(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in _INF_STRINGS:
        return _INF_STRINGS[value.strip().lower()]
    return value


def expand_dotted(data: Any) -> Any:
    """Recursively expand dotted keys into nested dictionaries."""
    if isinstance(data, list):
        return [expand_dotted(i) for i in data]
    if not isinstance(data, dict):
        return _clean_value(data)
    out: dict = {}
    for key, value in data.items():
        key = str(key)
        value = expand_dotted(value)
        parts = key.split(".")
        target = out
        for part in parts[:-1]:
            existing = target.get(part)
            if not isinstance(existing, dict):
                existing = {}
                target[part] = existing
            target = existing
        last = parts[-1]
        if isinstance(value, dict) and isinstance(target.get(last), dict):
            target[last] = deep_merge(target[last], value)
        else:
            target[last] = value
    return out


def deep_merge(base: dict, other: dict, conflicts: list[str] | None = None, _path: str = "") -> dict:
    """Merge `other` into a copy of `base`; `other` wins on leaf conflicts.

    Conflicting leaf paths (both sides define a different non-dict value) are
    appended to `conflicts` if given.
    """
    result = copy.deepcopy(base)
    for key, value in other.items():
        path = f"{_path}.{key}" if _path else str(key)
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value, conflicts, path)
        else:
            if conflicts is not None and key in result and result[key] != value:
                conflicts.append(path)
            result[key] = copy.deepcopy(value)
    return result


def read_yaml(path: Path) -> dict:
    """Read a YAML file into plain python containers, with dotted keys expanded."""
    yaml = YAML(typ="safe", pure=True)
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.load(f)
    except FileNotFoundError as err:
        raise ConversionError(f"YAML file not found: {path}") from err
    except (YAMLError, UnicodeDecodeError) as err:
        raise ConversionError(f"Could not parse YAML file {path}: {err}") from err
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConversionError(f"YAML file {path} must contain a mapping at the top level.")
    return expand_dotted(data)


@dataclass
class LegacyModel:
    """A fully loaded legacy model: base definition + overrides + scenarios."""

    model_dir: Path
    entry: Path
    base: dict
    overrides: dict[str, dict]
    scenarios: dict[str, list[str]]
    provenance: dict[tuple[str, str], str] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    node_renames: dict[str, str] = field(default_factory=dict)  # legacy node name -> valid v0.7 name

    @property
    def timeseries_dir(self) -> Path:
        ts_path = self.base.get("model", {}).get("timeseries_data_path", "./")
        return (self.entry.parent / str(ts_path)).resolve()

    def rel(self, path: Path) -> str:
        """Path relative to the model directory, with forward slashes."""
        try:
            return path.resolve().relative_to(self.model_dir.resolve()).as_posix()
        except ValueError:
            return path.resolve().as_posix()

    def source_of(self, section: str, name: str) -> str:
        return self.provenance.get((section, name), self.rel(self.entry))


_TOP_LEVEL_MODEL = re.compile(r"^model\s*:", re.MULTILINE)


def find_entry_files(model_dir: Path, exclude: list[str] | None = None) -> list[Path]:
    """Find YAML files with a top-level `model:` key (the v0.6 model entry points)."""
    exclude = exclude or []
    entries = []
    for path in sorted(model_dir.rglob("*.yaml")):
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in exclude):
            continue
        try:
            # Cheap text check first: only files with a top-level `model:` line are parsed.
            if not _TOP_LEVEL_MODEL.search(path.read_text(encoding="utf-8", errors="replace")):
                continue
            yaml = YAML(typ="safe", pure=True)
            with open(path, encoding="utf-8") as f:
                data = yaml.load(f)
        except Exception:  # unreadable files are reported when actually loaded
            continue
        if isinstance(data, dict) and "model" in data:
            entries.append(path)
    return entries


class _Loader:
    def __init__(self, model_dir: Path, diag: Diagnostics):
        self.model_dir = model_dir
        self.diag = diag
        self.provenance: dict[tuple[str, str], str] = {}
        self.files: list[str] = []
        self._stack: list[Path] = []

    def rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.model_dir.resolve()).as_posix()
        except ValueError:
            return path.resolve().as_posix()

    def load(self, path: Path) -> dict:
        path = path.resolve()
        if path in self._stack:
            raise ConversionError(f"Circular import detected involving {path}")
        self._stack.append(path)
        data = read_yaml(path)
        rel = self.rel(path)
        if rel not in self.files:
            self.files.append(rel)

        imports = data.pop("import", []) or []
        if isinstance(imports, str):
            imports = [imports]
        result: dict = {}
        for imp in imports:
            imp_path = (path.parent / str(imp)).resolve()
            if not imp_path.exists():
                self.diag.warn("missing_import", f"Imported file not found: {imp}", rel)
                continue
            imported = self.load(imp_path)
            result = self._merge(result, imported, self.rel(imp_path))

        self._record_provenance(data, rel)
        result = self._merge(result, data, rel)
        self._stack.pop()
        return result

    def _record_provenance(self, data: dict, rel: str) -> None:
        for section in ENTITY_SECTIONS:
            for name in data.get(section, {}) or {}:
                self.provenance.setdefault((section, str(name)), rel)

    def _merge(self, base: dict, other: dict, source: str) -> dict:
        conflicts: list[str] = []
        merged = deep_merge(base, other, conflicts)
        for path in conflicts:
            self.diag.warn(
                "import_conflict",
                f"Key `{path}` is defined in more than one imported file; the last definition wins.",
                source,
            )
        return merged


def _normalise_scenarios(raw: dict, diag: Diagnostics, overrides: dict) -> dict[str, list[str]]:
    scenarios: dict[str, list[str]] = {}
    for name, value in (raw or {}).items():
        if isinstance(value, str):
            items = [i.strip() for i in value.split(",") if i.strip()]
        elif isinstance(value, list):
            items = [str(i).strip() for i in value]
        else:
            diag.warn("bad_scenario", f"Scenario `{name}` must be a list of overrides; skipped.")
            continue
        for item in items:
            if item not in overrides and item not in (raw or {}):
                diag.warn(
                    "missing_override", f"Scenario `{name}` references `{item}`, which is not a defined override."
                )
        scenarios[str(name)] = items
    return scenarios


class _Sanitiser:
    """Enforce the structural types the converter relies on.

    Entries of the wrong type are dropped (or coerced, for lists given as a
    single string) and reported as `bad_structure` warnings, so that malformed
    input can never surface as a traceback further down.
    """

    def __init__(self, diag: Diagnostics, scope: str):
        self.diag = diag
        self.scope = scope

    def _warn(self, path: str, expected: str, value: Any) -> None:
        self.diag.warn(
            "bad_structure", f"`{path}` should be {expected}, got {type(value).__name__}; ignored.", self.scope
        )

    def mapping(self, value: Any, path: str) -> dict:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        self._warn(path, "a mapping", value)
        return {}

    def str_list(self, value: Any, path: str) -> list:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list) and all(isinstance(v, str | int | float) and not isinstance(v, bool) for v in value):
            return [str(v) for v in value]
        self._warn(path, "a list of names", value)
        return []

    def tech(self, value: Any, path: str) -> dict:
        tech = self.mapping(value, path)
        for key in ("essentials", "constraints"):
            if key in tech:
                tech[key] = self.mapping(tech[key], f"{path}.{key}")
        if "costs" in tech:
            costs = self.mapping(tech["costs"], f"{path}.costs")
            tech["costs"] = {k: self.mapping(v, f"{path}.costs.{k}") for k, v in costs.items()}
        ratios = tech.get("constraints", {}).get("carrier_ratios")
        if ratios is not None:
            ratios = self.mapping(ratios, f"{path}.constraints.carrier_ratios")
            tech["constraints"]["carrier_ratios"] = {
                k: self.mapping(v, f"{path}.constraints.carrier_ratios.{k}") for k, v in ratios.items()
            }
        return tech

    def techs(self, value: Any, path: str) -> dict:
        return {str(k): self.tech(v, f"{path}.{k}") for k, v in self.mapping(value, path).items()}

    def model(self, data: dict) -> dict:
        for section in ("techs", "tech_groups"):
            if section in data:
                data[section] = self.techs(data[section], section)
        for section in ("locations", "links"):
            if section in data:
                entries = {}
                for key, value in self.mapping(data[section], section).items():
                    entry = self.mapping(value, f"{section}.{key}")
                    if "techs" in entry:
                        entry["techs"] = self.techs(entry["techs"], f"{section}.{key}.techs")
                    entries[str(key)] = entry
                data[section] = entries
        if "group_constraints" in data:
            groups = {}
            for name, value in self.mapping(data["group_constraints"], "group_constraints").items():
                group = self.mapping(value, f"group_constraints.{name}")
                for key in ("techs", "locs"):
                    if key in group:
                        group[key] = self.str_list(group[key], f"group_constraints.{name}.{key}")
                groups[str(name)] = group
            data["group_constraints"] = groups
        for section in ("model", "run"):
            if section in data:
                data[section] = self.mapping(data[section], section)
        return data


V07_NAME = re.compile(r"^[^_\W\d]\w*$")  # v0.7 identifier: word characters, not starting with `_` or a digit


def v07_name(name: str) -> str:
    """A valid v0.7 identifier for a legacy name (`region1-1` -> `region1_1`, `1a` -> `n1a`)."""
    clean = re.sub(r"\W", "_", str(name))
    return clean if V07_NAME.match(clean) else f"n{clean}"


def _split_locs(key: str) -> list[str]:
    return [part.strip() for part in str(key).split(",") if part.strip()]


def _node_names(data: dict) -> set[str]:
    names = {n for key in (data.get("locations") or {}) for n in _split_locs(key)}
    names |= {n for key in (data.get("links") or {}) for n in _split_locs(key)}
    for group in (data.get("group_constraints") or {}).values():
        if isinstance(group, dict):
            names |= {str(n) for n in group.get("locs") or []}
    return names


def _rename_nodes(data: dict, renames: dict[str, str]) -> None:
    """Apply `renames` to location keys (incl. comma lists), link keys and group `locs`, in place."""
    for section, sep in (("locations", ", "), ("links", ",")):
        if isinstance(data.get(section), dict):
            data[section] = {
                sep.join(renames.get(n, n) for n in _split_locs(key)): value for key, value in data[section].items()
            }
    for group in (data.get("group_constraints") or {}).values():
        if isinstance(group, dict) and isinstance(group.get("locs"), list):
            group["locs"] = [renames.get(str(n), n) for n in group["locs"]]


def _valid_node_names(base: dict, overrides: dict[str, dict], diag: Diagnostics) -> dict[str, str]:
    """Rename legacy node names that are not valid v0.7 identifiers (v0.6 allowed e.g. hyphens)."""
    names = _node_names(base).union(*(_node_names(o) for o in overrides.values()))
    renames: dict[str, str] = {}
    taken = {n for n in names if V07_NAME.match(n)}
    for name in sorted(names - taken):
        new, i = v07_name(name), 2
        candidate = new
        while candidate in taken:
            candidate, i = f"{new}_{i}", i + 1
        taken.add(candidate)
        renames[name] = candidate
    if renames:
        pairs = ", ".join(f"`{a}` -> `{b}`" for a, b in renames.items())
        diag.warn(
            "renamed_nodes",
            f"Node names that are not valid v0.7 identifiers (letters, digits, `_`; not starting with a digit or `_`) "
            f"are renamed, also in CSV headers: {pairs}.",
        )
        for data in (base, *overrides.values()):
            _rename_nodes(data, renames)
    return renames


def load_legacy_model(entry: Path, model_dir: Path, diag: Diagnostics) -> LegacyModel:
    """Load the legacy model starting at `entry` (a YAML with a top-level `model:` key)."""
    loader = _Loader(model_dir, diag)
    data = loader.load(entry)
    if "model" not in data:
        raise ConversionError(f"{entry} has no top-level `model:` section; it is not a v0.6 model entry file.")

    overrides_raw = data.pop("overrides", {}) or {}
    if not isinstance(overrides_raw, dict):
        diag.warn("bad_structure", "`overrides` should be a mapping; ignored.")
        overrides_raw = {}
    overrides: dict[str, dict] = {}
    for name, value in overrides_raw.items():
        if value is None:
            diag.warn("empty_override", f"Override `{name}` is empty.", loader.provenance.get(("overrides", str(name))))
            value = {}
        if not isinstance(value, dict):
            diag.warn("bad_override", f"Override `{name}` is not a mapping; skipped.")
            continue
        overrides[str(name)] = _Sanitiser(diag, str(name)).model(value)
    scenarios_raw = data.pop("scenarios", {}) or {}
    if not isinstance(scenarios_raw, dict):
        diag.warn("bad_structure", "`scenarios` should be a mapping; ignored.")
        scenarios_raw = {}
    scenarios = _normalise_scenarios(scenarios_raw, diag, overrides)
    data = _Sanitiser(diag, "base").model(data)
    node_renames = _valid_node_names(data, overrides, diag)

    return LegacyModel(
        model_dir=model_dir,
        entry=entry.resolve(),
        base=data,
        overrides=overrides,
        scenarios=scenarios,
        provenance=loader.provenance,
        files=loader.files,
        node_renames=node_renames,
    )
