"""Bad input must never break the tool.

Every broken legacy model must either convert (with the problem reported as a
warning) or raise a `ConversionError` with a clear message. Any other exception
is a bug.
"""

import shutil
from pathlib import Path

import pytest
from converter.convert import convert_model
from converter.diagnostics import ConversionError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

SUB = Path("model/sub")


def _append(path: Path, text: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + text + "\n")


def _replace(path: Path, old: str, new: str) -> None:
    content = path.read_text(encoding="utf-8")
    assert old in content, f"fixture changed: {old!r} not in {path.name}"
    path.write_text(content.replace(old, new), encoding="utf-8")


# (mutation, expected warning code or None if a ConversionError is expected)
CASES = {
    "missing_lifetime": (
        lambda root: _replace(root / "model/techs.yaml", "            lifetime: 15\n", ""),
        "missing_lifetime",
    ),
    "missing_csv": (lambda root: (root / SUB / "cf-pv.csv").unlink(), "missing_csv"),
    "csv_missing_node_column": (
        lambda root: _replace(root / SUB / "demand-heat.csv", "datetime,A,B,C", "datetime,X,B,C"),
        "missing_column",
    ),
    "csv_with_nan": (lambda root: _replace(root / SUB / "demand-heat.csv", "-0.5,", ","), "csv_nan"),
    "csv_with_text": (lambda root: _replace(root / SUB / "demand-heat.csv", "-0.5,", "abc,"), "bad_csv"),
    "csv_bad_dates": (lambda root: _replace(root / SUB / "cop-hp.csv", "2018-01-01 05:00", "not-a-date"), "bad_csv"),
    "csv_empty": (lambda root: (root / SUB / "charging-profile.csv").write_text("datetime,A,B,C\n"), "bad_csv"),
    "unknown_param": (
        lambda root: _replace(root / "model/techs.yaml", "storage_loss: 0", "storage_loss: 0\n            foo_bar: 1"),
        "unknown_param",
    ),
    "unknown_parent": (
        lambda root: _replace(root / "model/techs.yaml", "parent: pv_group", "parent: missing_group"),
        "unknown_parent",
    ),
    "override_unknown_tech": (lambda root: None, "undefined_tech"),
    "scenario_missing_override": (
        lambda root: _append(root / "model/overrides-extra/extra.yaml", "    broken: [ev_at_c, does_not_exist]"),
        "missing_override",
    ),
    "empty_override": (
        lambda root: _replace(
            root / "model/overrides-extra/extra.yaml", "    ev_at_c:\n", "    empty_one:\n    ev_at_c:\n"
        ),
        "empty_override",
    ),
    "missing_import": (
        lambda root: _replace(root / SUB / "model.yaml", "- './groups.yaml'", "- './groups.yaml'\n    - './nope.yaml'"),
        "missing_import",
    ),
    "bad_group_value": (
        lambda root: _replace(root / SUB / "groups.yaml", "energy_cap_max: 50", "energy_cap_max: lots"),
        "bad_value",
    ),
    "group_unknown_type": (
        lambda root: _replace(root / SUB / "groups.yaml", "energy_cap_max: 50", "magic_max: 50"),
        "unknown_group_constraint",
    ),
    "x_y_coordinates": (
        lambda root: _replace(root / SUB / "locations.yaml", "{lat: 46.0, lon: 9.0}", "{x: 1, y: 2}"),
        "xy_coordinates",
    ),
    "unknown_cost": (
        lambda root: _replace(
            root / "model/techs.yaml", "storage_cap: 100", "storage_cap: 100\n                weird_cost: 1"
        ),
        "unknown_cost",
    ),
    "zero_ratio": (lambda root: _replace(root / "model/techs.yaml", "heat: 1.2", "heat: 0"), "zero_ratio"),
    "unreadable_yaml": (lambda root: _append(root / "model/techs.yaml", "  bad: [unclosed"), None),
    "no_entry_file": (lambda root: [(root / SUB / n).unlink() for n in ("model.yaml", "model_.yaml")], None),
    "no_model_folder": (lambda root: shutil.rmtree(root / "model"), None),
    "two_entry_files": (lambda root: shutil.copy(root / SUB / "model.yaml", root / SUB / "other.yaml"), None),
    "no_run_py": (lambda root: (root / "run.py").unlink(), "no_run_py"),
    "bad_run_py": (lambda root: (root / "run.py").write_text("def broken(:\n"), "bad_run_py"),
}


@pytest.mark.parametrize("case", list(CASES))
def test_broken_input(case, legacy_copy, tmp_path):
    """Each kind of broken input is either a warning or a clear ConversionError."""
    mutate, code = CASES[case]
    mutate(legacy_copy)
    if code is None:
        with pytest.raises(ConversionError):
            convert_model(legacy_copy, tmp_path / "out", copy_csvs=False)
        return
    result = convert_model(legacy_copy, tmp_path / "out", copy_csvs=False)
    codes = {m.code for m in result.diagnostics.messages}
    assert code in codes, f"expected `{code}` in {sorted(codes)}"
    assert (tmp_path / "out" / "sub/model.yaml").exists()


_names = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True)
_values = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.integers(-10, 10),
    st.text(max_size=10),
    st.none(),
    st.booleans(),
    st.just("file=missing.csv"),
    st.lists(st.integers(), max_size=3),
)
_parents = st.sampled_from(
    ["supply", "supply_plus", "demand", "storage", "conversion", "conversion_plus", "transmission", "unknown", None]
)
_tech = st.fixed_dictionaries(
    {
        "essentials": st.fixed_dictionaries(
            {
                "parent": _parents,
                "carrier": st.sampled_from(["electricity", "heat", None]),
                "carrier_in": st.sampled_from(["gas", None]),
                "carrier_out": st.sampled_from(["heat", None]),
            }
        ),
        "constraints": st.dictionaries(
            st.sampled_from(
                [
                    "energy_cap_max",
                    "energy_eff",
                    "resource",
                    "carrier_ratios",
                    "force_resource",
                    "storage_cap_equals",
                    "cb",
                    "unknown_key",
                ]
            ),
            _values,
            max_size=4,
        ),
        "costs": st.dictionaries(
            st.sampled_from(["monetary", "co2", "spores_score"]),
            st.dictionaries(st.sampled_from(["energy_cap", "om_con", "om_prod", "nonsense"]), _values, max_size=3),
            max_size=2,
        ),
    }
)
_model = st.fixed_dictionaries(
    {
        "techs": st.dictionaries(_names, _tech, max_size=5),
        "locations": st.dictionaries(
            st.sampled_from(["A", "B", "A,B"]),
            st.fixed_dictionaries({"techs": st.dictionaries(_names, st.none(), max_size=3)}),
            max_size=2,
        ),
        "group_constraints": st.dictionaries(
            _names,
            st.dictionaries(
                st.sampled_from(
                    [
                        "techs",
                        "locs",
                        "energy_cap_max",
                        "carrier_con_min",
                        "cost_max",
                        "demand_share_per_timestep_decision",
                    ]
                ),
                _values,
                max_size=3,
            ),
            max_size=3,
        ),
        "overrides": st.dictionaries(
            _names,
            st.dictionaries(
                st.sampled_from(["techs", "run", "model", "locations"]),
                st.dictionaries(_names, _values, max_size=2),
                max_size=2,
            ),
            max_size=3,
        ),
    }
)


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow]
)
@given(model=_model)
def test_random_models_never_crash(model, tmp_path_factory):
    """Randomly generated (often invalid) legacy models never raise unexpected exceptions."""
    from ruamel.yaml import YAML

    root = tmp_path_factory.mktemp("random")
    (root / "model").mkdir()
    content = {
        "model": {"name": "random", "subset_time": ["2018-01-01", "2018-01-02"]},
        "run": {"mode": "plan"},
        **model,
    }
    yaml = YAML(typ="safe", pure=True)
    with open(root / "model" / "model.yaml", "w", encoding="utf-8") as f:
        yaml.dump(content, f)
    try:
        convert_model(root, root / "out", copy_csvs=False)
    except ConversionError:
        pass
