"""End-to-end conversion of the mini legacy model (no Calliope needed)."""

import pandas as pd
import pytest
from ruamel.yaml import YAML


def _load(path):
    return YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def entry(converted_mini):
    _, out = converted_mini
    return _load(out / "sub/model.yaml"), out


def test_layout_mirrors_legacy_files(converted_mini):
    """One converted YAML per legacy YAML, plus math, data tables and the report."""
    result, out = converted_mini
    for rel in [
        "sub/model.yaml",
        "techs.yaml",
        "interest-rate.yaml",
        "sub/locations.yaml",
        "sub/groups.yaml",
        "overrides-extra/extra.yaml",
    ]:
        assert (out / rel).exists(), rel
    assert (out / "math/group_constraints.yaml").exists()
    assert (out / "math/conversion_plus.yaml").exists()
    assert (out.parent / "report.md").exists()
    assert result.report["stats"]["link_techs"] == 2


def test_entry_config(entry):
    """Config, math and imports are set in the entry file."""
    model, _ = entry
    init = model["config"]["init"]
    assert init["subset"]["timesteps"] == ["2018-01-01", "2018-01-02"]
    assert set(init["extra_math"]) >= {"legacy_conversion_plus", "legacy_group_constraints"}
    assert "system_balance" in model["config"]["solve"]["shadow_prices"]
    assert any(s.endswith("__cost_max__co2_constraint") for s in model["config"]["solve"]["shadow_prices"])
    assert "../techs.yaml" in model["import"]


def test_all_table_paths_exist(entry):
    """Every data table points to an existing CSV (base and overrides)."""
    _, out = entry
    for path in out.rglob("*.yaml"):
        data = _load(path) or {}
        tables = dict(data.get("data_tables") or {})
        for override in (data.get("overrides") or {}).values():
            tables.update(override.get("data_tables") or {})
        for name, table in tables.items():
            assert (path.parent / table["table"]).exists(), f"{path.name}: {name}"


def test_csv_header_normalised_and_demand_negated(entry, mini_legacy):
    """CSVs get a `timesteps` index header; demand is made positive under its legacy name (the original is not copied)."""
    _, out = entry
    legacy = pd.read_csv(mini_legacy / "model/sub/demand-elec.csv", index_col=0)
    positive = pd.read_csv(out / "data_tables/sub/demand-elec.csv", index_col=0)
    copied = pd.read_csv(out / "data_tables/sub/cf-pv.csv", index_col=0)  # used as it is
    assert positive.index.name == copied.index.name == "timesteps"
    assert (positive.values > 0).all()
    assert (positive.values == -legacy.loc[[str(t) for t in positive.index]].values).all()
    assert not list((out / "data_tables/sub").glob("*__positive.csv"))
    assert (out / "data_tables/sub/unused.csv").exists()


def test_unlisted_cost_classes_not_in_objective(entry):
    """Cost classes missing from the legacy objective get weight 0 (v0.7 would default them to 1)."""
    model, _ = entry
    weights = model["data_definitions"]["objective_cost_weights"]
    assert dict(zip(weights["index"], weights["data"])) == {"monetary": 1.0, "co2": 0.0, "excl_score": 0.0}


def test_time_varying_cop(entry):
    """A time-varying COP becomes the heat pump's `flow_out_eff` on its output carrier."""
    model, _ = entry
    (cop,) = [t for t in model["data_tables"].values() if t.get("add_dims", {}).get("inputs") == "flow_out_eff"]
    assert cop["table"].endswith("data_tables/sub/cop-hp.csv")
    assert (cop["add_dims"]["techs"], cop["add_dims"]["carriers"]) == ("heat_pump", "heat")


def test_conversion_plus_math(entry):
    """Gallery CHP form: tier-1 balance and neutral tier-2 links (v0.6 AND across tiers, OR within a tier)."""
    _, out = entry
    math = _load(out / "math/conversion_plus.yaml")
    balance = math["constraints"]["balance_conversion"]["equations"]
    assert balance[1] == {
        "where": "[cchp, chp] in techs",
        "expression": "flow_out_inc_eff[carriers=electricity] == sum(flow_in_inc_eff, over=carriers)",
    }
    assert math["constraints"]["link_out_2_heat_to_electricity"]["equations"] == [
        {
            "expression": "flow_out[carriers=electricity] * carrier_out_2_to_carrier_out_1_ratio[carriers=heat] "
            "== flow_out[carriers=heat]"
        }
    ]
    # CCHP (docs): one unit of tier 2 per unit of electricity, met by any mix of 0.8 heat / 0.5 cooling
    assert math["constraints"]["link_out_2_heat_cooling_to_electricity"]["equations"] == [
        {
            "expression": "flow_out[carriers=electricity] == "
            "flow_out[carriers=heat] / carrier_out_2_to_carrier_out_1_ratio[carriers=heat] + "
            "flow_out[carriers=cooling] / carrier_out_2_to_carrier_out_1_ratio[carriers=cooling]"
        }
    ]
    techs = _load(out / "techs.yaml")["techs"]
    assert techs["cchp"]["carrier_out_2_to_carrier_out_1_ratio"] == {
        "data": [0.8, 0.5],
        "index": ["heat", "cooling"],
        "dims": "carriers",
    }
    # Air source heat pump (docs): OR within tier 1 -> per-carrier efficiencies, no custom math
    assert techs["ahp"]["flow_out_eff"] == {"data": [5.0, 3.0], "index": ["heat", "cooling"], "dims": "carriers"}
    assert "ahp" not in str(math["constraints"]["balance_conversion"])
    # v0.6 capacity: tier-1 outputs share the primary carrier's capacity (one `energy_cap`)
    assert math["constraints"]["conversion_plus_flow_out_max"] == {
        "description": "Legacy conversion_plus: tier-1 outputs share the capacity of the primary output carrier.",
        "foreach": ["nodes", "techs", "timesteps"],
        "where": "[ahp] in techs",
        "equations": [
            {
                "expression": "flow_out[carriers=heat] + flow_out[carriers=cooling] <= flow_cap[carriers=heat] * timestep_resolution"
            }
        ],
    }
    ramp = math["constraints"]["ramping_up"]["equations"]
    assert {
        "where": "[ahp, heat_pump] in techs",
        "expression": "$flow - roll($flow, timesteps=1) <= flow_ramping * flow_cap[carriers=heat]",
    } in ramp


def test_yaml_layout(entry):
    """Block-style mappings, inline lists, a blank line between top-level keys."""
    _, out = entry
    assert "carrier_out: [electricity, heat]" in (out / "techs.yaml").read_text(encoding="utf-8")
    text = (out / "sub/model.yaml").read_text(encoding="utf-8")
    assert "{" not in text
    assert "\n\nconfig:" in text
    assert "\n\ndata_tables:" in text


def test_techs_templates_and_links(entry):
    """Templates, transmission links and costs land where expected."""
    _, out = entry
    techs = _load(out / "techs.yaml")
    assert techs["templates"]["ac_line"]["base_tech"] == "transmission"
    assert techs["techs"]["pv"]["template"] == "pv_group"
    assert techs["techs"]["chp"]["flow_out_eff"] == {"data": 0.4, "index": "electricity", "dims": "carriers"}
    assert "cost_flow_cap" not in techs["techs"]["pv"]  # costs live in data tables
    locations = _load(out / "sub/locations.yaml")
    link = locations["techs"]["A_to_B_ac_line"]
    assert (link["link_from"], link["link_to"], link["template"]) == ("A", "B", "ac_line")
    assert locations["nodes"]["A"]["latitude"] == 45.0
    costs = pd.read_csv(out / "data_tables/generated/costs_per_carrier.csv", index_col=[0, 1, 2])
    assert costs.loc[("boiler", "monetary", "heat"), "cost_flow_cap"] == 50
    assert costs.loc[("B_to_C_ac_line", "monetary", "electricity"), "cost_flow_cap"] == 25


def test_activation_guards(entry):
    """Techs with tech-level timeseries are deactivated where v0.6 did not define them."""
    _, out = entry
    nodes = _load(out / "sub/locations.yaml")["nodes"]
    assert nodes["C"]["techs"]["ev_battery"] == {"active": False}
    assert nodes["C"]["techs"]["pv"] == {"active": False}


def test_scenario_with_clashing_overrides_is_merged(entry):
    """Overrides that set the same key are merged for declared scenarios (v0.7 forbids duplicates)."""
    _, out = entry
    extra = _load(out / "overrides-extra/extra.yaml")
    assert extra["scenarios"]["ev"] == ["scenario_ev_merged"]
    assert "scenario_ev_merged" in extra["overrides"]
    assert extra["scenarios"]["policy"] == ["co2_cap", "renewable_share", "annual_gas_use"]


def test_report_lists_custom_math(converted_mini):
    """The report lists run.py constraints, their trigger and the techs they apply to."""
    result, out = converted_mini
    custom = {c["name"]: c for c in result.report["custom_constraints"]}
    assert set(custom) == {
        "carrier_consumption_max_time_varying_constraint",
        "chp_extraction_cb_constraint",
        "storage_ev_constraint",
    }
    assert custom["chp_extraction_cb_constraint"]["gallery"] == "chp_htp.yaml"
    assert any("chp" in a for a in custom["chp_extraction_cb_constraint"]["applies_to"])
    text = (out.parent / "report.md").read_text(encoding="utf-8")
    assert "Custom math in run.py" in text
    assert "unreferenced_csv" in text


def test_conversion_is_deterministic(tmp_path, mini_legacy):
    """Converting twice gives identical YAML."""
    from converter.convert import convert_model

    convert_model(mini_legacy, tmp_path / "a", copy_csvs=False)
    convert_model(mini_legacy, tmp_path / "b", copy_csvs=False)
    for path in (tmp_path / "a").rglob("*.yaml"):
        assert path.read_text() == (tmp_path / "b" / path.relative_to(tmp_path / "a")).read_text()


def test_conversion_plus_capacity_warned(converted_mini):
    """conversion_plus capacity is not translated: one warning per tech."""
    result, _ = converted_mini
    warned = {m.message.split("`")[1] for m in result.diagnostics.messages if m.code == "conversion_plus_capacity"}
    assert warned == {"chp", "heat_pump", "cchp", "ahp"}


def test_group_capacity_counts_outputs(converted_mini):
    """v0.6 capacities are output capacities: group sums take `flow_cap` on `carrier_out`, no weights."""
    _, out = converted_mini
    math = _load(out / "math/group_constraints.yaml")
    expression = math["global_expressions"]["group_flow_cap_out"]
    assert expression["where"] == (
        "flow_cap AND carrier_out AND (primary_carrier_out OR NOT any(primary_carrier_out, over=carriers))"
    )
    assert math["lookups"]["primary_carrier_out"]["dtype"] == "bool"
    techs = _load(out / "techs.yaml")["techs"]
    # v0.6 had one `energy_cap`: multi-output techs count their primary output only
    assert techs["chp"]["primary_carrier_out"] == {"data": True, "index": "electricity", "dims": "carriers"}
    assert "primary_carrier_out" not in techs["boiler"]
    assert "capacity_carrier_weight" not in (out / "techs.yaml").read_text(encoding="utf-8")


def test_interest_rate_in_templates(converted_mini):
    """Interest rates live in YAML: v0.6 abstract base groups -> `base_*` templates, overridden per tech."""
    _, out = converted_mini
    templates = _load(out / "interest-rate.yaml")["templates"]
    assert templates["base_supply"]["cost_interest_rate"] == {"data": 0.07, "index": "monetary", "dims": "costs"}
    assert "cost_interest_rate" not in pd.read_csv(out / "data_tables/generated/costs.csv").columns
    overrides = _load(out / "overrides-extra/extra.yaml")["overrides"]
    techs = [o.get("techs", {}).get("battery", {}) for o in overrides.values()]
    rates = [t["cost_interest_rate"] for t in techs if "cost_interest_rate" in t]
    assert {"data": [0.07, 0.0], "index": ["monetary", "excl_score"], "dims": "costs"} in rates


def test_group_math_has_no_key_lookups(converted_mini):
    """Carriers / cost classes are selected with `[key] in dim`; dspt main constraints are one pair for all groups."""
    _, out = converted_mini
    math = _load(out / "math/group_constraints.yaml")
    assert all(name.endswith(("__members", "__nodes")) for name in math["lookups"] if name != "primary_carrier_out")
    dspt = [n for n in math["constraints"] if "_main_" in n]
    assert dspt == [
        "demand_share_per_timestep_decision_main_min_constraint",
        "demand_share_per_timestep_decision_main_max_constraint",
    ]
    assert "[heat] in carriers" in math["constraints"][dspt[0]]["where"]


def test_math_entries_spaced(converted_mini):
    """Generated math files have a blank line between entries (each constraint, lookup, ...)."""
    _, out = converted_mini
    text = (out / "math/group_constraints.yaml").read_text(encoding="utf-8")
    assert "\n\n  group_gas_use_a__members:" in text
    assert "\n\n  demand_share_per_timestep_decision_main_max_constraint:" in text


def test_sign_flip_keeps_suffix_when_source_also_used_as_is(legacy_copy, tmp_path):
    """A CSV used as it is and sign-flipped (demand) needs two files: the flipped one gets `__positive`."""
    from converter.convert import convert_model

    techs = legacy_copy / "model/techs.yaml"
    techs.write_text(techs.read_text(encoding="utf-8").replace("resource: -2", "resource: file=cf-pv.csv"))
    convert_model(legacy_copy, tmp_path / "out")
    tables = tmp_path / "out/data_tables/sub"
    assert (tables / "cf-pv.csv").exists()
    assert (tables / "cf-pv__positive.csv").exists()
