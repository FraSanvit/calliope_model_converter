"""run.py `storage_ev_constraint` and the legacy `storage_charge_depth`."""

import pytest
from converter.convert import convert_model
from converter.runpy_inspector import storage_ev_name_filter
from ruamel.yaml import YAML


def add_charge_depth(legacy, value: str = "0.8") -> None:
    """The mini model's battery gets a maximum state of charge."""
    techs = legacy / "model/techs.yaml"
    text = techs.read_text(encoding="utf-8")
    old = "            energy_cap_per_storage_cap_max: 0.25\n"
    assert old in text
    techs.write_text(text.replace(old, old + f"            storage_charge_depth: {value}\n", 1), encoding="utf-8")


def test_charge_depth_math(legacy_copy, tmp_path):
    """`storage_charge_depth` is kept and its max state-of-charge constraint added."""
    add_charge_depth(legacy_copy)
    out = tmp_path / "out"
    convert_model(legacy_copy, out, copy_csvs=False)
    yaml = YAML(typ="safe", pure=True)
    math = yaml.load((out / "math/storage_charge_depth.yaml").read_text(encoding="utf-8"))
    assert math["constraints"]["storage_charge_depth_limit"]["equations"] == [
        {"expression": "storage - storage_charge_depth * storage_cap <= 0"}
    ]
    assert (
        yaml.load((out / "techs.yaml").read_text(encoding="utf-8"))["techs"]["battery"]["storage_charge_depth"] == 0.8
    )


def test_no_charge_depth_math_without_it(converted_mini):
    _, out = converted_mini
    assert not (out / "math/storage_charge_depth.yaml").exists()


def test_storage_ev_naming_rule_warned(converted_mini):
    """The mini `ev_battery` does not follow `<sector>_<mode>_battery`: warned, not guessed."""
    result, out = converted_mini
    warnings = [m.message for m in result.diagnostics.messages if m.code == "storage_ev"]
    assert any("`ev_battery`" in w and "naming" in w for w in warnings)
    assert not (out / "math/storage_ev.yaml").exists()


@pytest.mark.parametrize("name_filter", ["ev_battery", "_battery"])
def test_storage_ev_set_filter_read_from_run_py(legacy_copy, tmp_path, name_filter):
    """The battery set follows run.py's own name filter (e.g. `ev_battery` or `_battery`)."""
    run_py = legacy_copy / "run.py"
    run_py.write_text(
        run_py.read_text(encoding="utf-8").replace('("ev_battery" in', f'("{name_filter}" in'), encoding="utf-8"
    )
    assert storage_ev_name_filter(run_py) == name_filter
    techs = legacy_copy / "model/techs.yaml"
    text = techs.read_text(encoding="utf-8")
    old = "    boiler:\n"
    battery = (
        "    truck_ers_battery:\n        essentials:\n            name: 'ERS battery'\n            parent: storage\n"
        "            carrier: electricity\n        constraints:\n            lifetime: 12\n"
    )
    techs.write_text(text.replace(old, battery + old, 1), encoding="utf-8")
    result = convert_model(legacy_copy, tmp_path / "out", copy_csvs=False)
    messages = [m for m in result.diagnostics.messages if m.code == "storage_ev"]
    outside = any("truck_ers_battery" in m.message and "not constrained" in m.message for m in messages)
    considered = any("`truck_ers_battery`: no `truck_transport_ers`" in m.message for m in messages)
    assert (outside, considered) == ((True, False) if name_filter == "ev_battery" else (False, True))


def test_storage_ev_filter_not_found(tmp_path):
    run_py = tmp_path / "run.py"
    run_py.write_text("def f(model):\n    return model\n", encoding="utf-8")
    assert storage_ev_name_filter(run_py) is None
    assert storage_ev_name_filter(tmp_path / "missing.py") is None
