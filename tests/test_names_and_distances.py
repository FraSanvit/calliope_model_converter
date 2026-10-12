"""v0.6 names that are invalid in v0.7 (e.g. `region1-1`) and link distances from x/y coordinates."""

import pandas as pd
from converter.convert import convert_model
from converter.diagnostics import Diagnostics
from converter.legacy import _valid_node_names, v07_name
from converter.timeseries import DerivedCsv, copy_csv, write_derived
from ruamel.yaml import YAML

yaml = YAML(typ="safe", pure=True)


def test_v07_name():
    assert v07_name("region1-1") == "region1_1"
    assert v07_name("1st") == "n1st"
    assert v07_name("_x") == "n_x"
    assert v07_name("ok_name") == "ok_name"


def test_nodes_renamed_everywhere():
    base = {
        "locations": {"region1-1, region1-2": {}, "region1": {}},
        "links": {"region1,region1-1": {"techs": {}}},
        "group_constraints": {"g": {"locs": ["region1-2"]}},
    }
    override = {"locations": {"region1-1": {"techs": {}}}}
    diag = Diagnostics()
    renames = _valid_node_names(base, {"o": override}, diag)
    assert renames == {"region1-1": "region1_1", "region1-2": "region1_2"}
    assert set(base["locations"]) == {"region1_1, region1_2", "region1"}
    assert set(base["links"]) == {"region1,region1_1"}
    assert base["group_constraints"]["g"]["locs"] == ["region1_2"]
    assert set(override["locations"]) == {"region1_1"}
    assert "renamed_nodes" in {m.code for m in diag.messages}


def test_rename_avoids_clashes():
    base = {"locations": {"a-b": {}, "a_b": {}}}
    renames = _valid_node_names(base, {}, Diagnostics())
    assert renames == {"a-b": "a_b_2"}


def test_csv_headers_renamed(tmp_path):
    src = tmp_path / "in.csv"
    pd.DataFrame({"region1-1": [1.0, 2.0]}, index=pd.Index(["2005-01-01 00:00", "2005-01-01 01:00"])).to_csv(src)
    copy_csv(src, tmp_path / "copy.csv", renames={"region1-1": "region1_1"})
    assert pd.read_csv(tmp_path / "copy.csv", index_col=0).columns.tolist() == ["region1_1"]
    spec = DerivedCsv(src, "derived.csv", "negate", None, ("region1_1",))
    assert write_derived(spec, tmp_path, Diagnostics(), {"region1-1": "region1_1"})
    derived = pd.read_csv(tmp_path / "derived.csv", index_col=0)
    assert derived.columns.tolist() == ["region1_1"]
    assert derived["region1_1"].tolist() == [-1.0, -2.0]


def test_link_distance_from_xy(legacy_copy, tmp_path):
    """As v0.6: links without `distance` between x/y nodes get the Euclidean distance."""
    locations = legacy_copy / "model/sub/locations.yaml"
    text = locations.read_text(encoding="utf-8")
    for old, new in [
        ("{lat: 45.0, lon: 8.0}", "{x: 0, y: 0}"),
        ("{lat: 45.5, lon: 8.5}", "{x: 3, y: 4}"),
        ("{lat: 46.0, lon: 9.0}", "{x: 3, y: 10}"),
    ]:
        assert old in text
        text = text.replace(old, new)
    locations.write_text(text, encoding="utf-8")
    out = tmp_path / "out"
    convert_model(legacy_copy, out, copy_csvs=False)
    techs = {}
    for path in out.rglob("*.yaml"):
        techs.update((yaml.load(path.read_text(encoding="utf-8")) or {}).get("techs") or {})
    distances = {name: tech.get("distance") for name, tech in techs.items() if "_to_" in name}
    assert distances["A_to_B_ac_line"] == 5.0
    assert distances["B_to_C_ac_line"] == 6.0
