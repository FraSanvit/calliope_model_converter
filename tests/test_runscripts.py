"""Legacy run scripts: read statically, converted to a v0.7 run script that selects overrides by name."""

from converter.convert import convert_model
from converter.diagnostics import Diagnostics
from converter.runscripts import expand_scenario, read_run_script
from ruamel.yaml import YAML

RUN_SCRIPT = """
import run
battery = "cheap"
co2 = "cap"
scenarios_string = "ev,"\\
    f"{battery}_battery,co2_{co2}"
scenario_name = f"test-{co2}"
path_to_model_yaml = "2030/model/sub/model.yaml"
model_input = run.build(scenarios_string)  # never executed
"""


def test_scenario_string_evaluated_statically(tmp_path):
    """f-strings, concatenations and earlier variables are resolved without running the script."""
    path = tmp_path / "run-test.py"
    path.write_text(RUN_SCRIPT, encoding="utf-8")
    script = read_run_script(path, Diagnostics())
    assert script.overrides == ["ev", "cheap_battery", "co2_cap"]
    assert script.settings["scenario_name"] == "test-cap"
    assert 'battery = "cheap"' in script.source  # kept verbatim for the v0.7 script
    assert not any(line.startswith("path_") for line in script.source)  # legacy file layout dropped


def test_nested_scenarios_expanded():
    """Legacy scenario names inside the list are replaced by their overrides."""
    scenarios = {"ev": ["ev_at_c", "ev_everywhere"], "all": ["ev", "co2_cap"]}
    assert expand_scenario(["all", "cheap_battery"], scenarios) == [
        "ev_at_c",
        "ev_everywhere",
        "co2_cap",
        "cheap_battery",
    ]


def test_run_script_converted(legacy_copy, tmp_path):
    """A v0.7 script with the legacy settings verbatim; no scenario is added to the model YAML."""
    (legacy_copy / "run-test.py").write_text(RUN_SCRIPT, encoding="utf-8")
    out = tmp_path / "out"
    result = convert_model(legacy_copy, out, copy_csvs=False)
    assert not (out / "run-test.py").exists()
    script = (out.parent / "run-test.py").read_text(encoding="utf-8")  # next to the model folder, as in the legacy one
    assert 'MODEL = HERE / "out/sub/model.yaml"' in script
    assert 'battery = "cheap"' in script
    assert 'f"{battery}_battery,co2_{co2}"' in script
    assert 'RESULTS = HERE / "results" / f"{scenario_name}.nc"' in script
    assert "def v06_overrides(" in script
    compile(script, "run-test.py", "exec")
    for path in out.rglob("*.yaml"):
        content = YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8")) or {}
        assert "run-test" not in (content.get("scenarios") or {})
    assert "run_script" in {m.code for m in result.diagnostics.messages}


def test_unknown_override_in_run_script_warned(legacy_copy, tmp_path):
    (legacy_copy / "run-test.py").write_text('scenarios_string = "ev,not_an_override"\n', encoding="utf-8")
    result = convert_model(legacy_copy, tmp_path / "out", copy_csvs=False)
    assert "missing_override" in {m.code for m in result.diagnostics.messages}


def test_unreadable_run_script_is_a_warning(legacy_copy, tmp_path):
    (legacy_copy / "run-broken.py").write_text("scenarios_string = (\n", encoding="utf-8")
    result = convert_model(legacy_copy, tmp_path / "out", copy_csvs=False)
    assert "bad_run_script" in {m.code for m in result.diagnostics.messages}
