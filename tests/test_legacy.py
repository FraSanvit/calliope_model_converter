"""Loading legacy (v0.6) YAML."""

import math

import pytest
from converter.diagnostics import ConversionError, Diagnostics
from converter.legacy import deep_merge, expand_dotted, find_entry_files, load_legacy_model, read_yaml


def test_expand_dotted_nested_and_merged():
    """Dotted keys become nested mappings and merge with explicit ones."""
    data = expand_dotted({"a.b.c": 1, "a": {"b": {"d": 2}}, "x": {"y.z": 3}})
    assert data == {"a": {"b": {"c": 1, "d": 2}}, "x": {"y": {"z": 3}}}


@pytest.mark.parametrize(
    ("raw", "expected"), [("inf", math.inf), (".inf", math.inf), ("-inf", -math.inf), ("text", "text")]
)
def test_inf_strings(raw, expected):
    """v0.6 accepted `inf` given as a string."""
    assert expand_dotted({"k": raw})["k"] == expected


def test_location_keys_with_commas_and_dots():
    """Location keys keep their comma; the dotted suffix is expanded."""
    data = expand_dotted({"R1,R2.techs": {"pv": None}})
    assert data == {"R1,R2": {"techs": {"pv": None}}}


def test_deep_merge_reports_conflicts():
    """Conflicting leaves are reported; the second value wins."""
    conflicts: list[str] = []
    merged = deep_merge({"a": {"b": 1, "c": 1}}, {"a": {"b": 2}}, conflicts)
    assert merged == {"a": {"b": 2, "c": 1}}
    assert conflicts == ["a.b"]


def test_entry_detection_excludes_discarded(mini_legacy):
    """`model_.yaml` copies are discarded by default."""
    entries = find_entry_files(mini_legacy / "model", ["*_.yaml"])
    assert [e.name for e in entries] == ["model.yaml"]
    assert len(find_entry_files(mini_legacy / "model", [])) == 2


def test_load_mini_model(mini_legacy):
    """Imports, overrides and scenarios are all collected with provenance."""
    diag = Diagnostics()
    model = load_legacy_model(mini_legacy / "model/sub/model.yaml", mini_legacy / "model", diag)
    assert "chp" in model.base["techs"]
    assert "A,B" in model.base["locations"]
    assert {"ev", "policy", "everything"} <= set(model.scenarios)
    assert model.scenarios["ev"] == ["ev_at_c", "ev_everywhere"]
    assert model.source_of("techs", "chp") == "techs.yaml"
    assert model.source_of("overrides", "co2_cap") == "sub/groups.yaml"
    assert model.timeseries_dir == (mini_legacy / "model/sub").resolve()


def test_bad_yaml_raises_conversion_error(tmp_path):
    """Unparseable YAML is a ConversionError, not a raw parser exception."""
    path = tmp_path / "bad.yaml"
    path.write_text("a: [unclosed\n")
    with pytest.raises(ConversionError, match="Could not parse"):
        read_yaml(path)


def test_non_mapping_yaml_raises(tmp_path):
    """A YAML list at the top level is rejected."""
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n")
    with pytest.raises(ConversionError, match="mapping"):
        read_yaml(path)


def test_circular_import(tmp_path):
    """Circular imports are detected."""
    (tmp_path / "a.yaml").write_text("import: [b.yaml]\nmodel: {name: x}\n")
    (tmp_path / "b.yaml").write_text("import: [a.yaml]\n")
    with pytest.raises(ConversionError, match="Circular"):
        load_legacy_model(tmp_path / "a.yaml", tmp_path, Diagnostics())


def test_missing_import_is_a_warning(tmp_path):
    """A missing imported file does not stop the conversion."""
    (tmp_path / "a.yaml").write_text("import: [missing.yaml]\nmodel: {name: x}\n")
    diag = Diagnostics()
    load_legacy_model(tmp_path / "a.yaml", tmp_path, diag)
    assert diag.by_code("missing_import")


def test_scenario_string_and_missing_override(tmp_path):
    """Comma-separated scenarios are split; unknown overrides are reported."""
    (tmp_path / "a.yaml").write_text("model: {name: x}\noverrides: {o1: {}}\nscenarios: {s: 'o1, o2'}\n")
    diag = Diagnostics()
    model = load_legacy_model(tmp_path / "a.yaml", tmp_path, diag)
    assert model.scenarios["s"] == ["o1", "o2"]
    assert diag.by_code("missing_override")
