"""Group constraints and config conversion."""

import math

from converter.config import ConfigConverter
from converter.diagnostics import Diagnostics
from converter.groups import GroupConverter


def _groups(diag=None):
    return GroupConverter(diag or Diagnostics(), lambda t: [t] if t != "unknown" else [], lambda: ["A", "B"])


def test_carrier_con_is_negated():
    """Legacy consumption (negative) becomes positive inflow limits."""
    conv = _groups()
    data = conv.convert({"g": {"techs": ["d"], "locs": ["A"], "carrier_con_min": {"gas": -10}}}, "s")
    (item,) = data.groups[0].items
    assert (item.vtype, item.sense, item.key, item.value) == ("flow_in", "min", "gas", 10)


def test_equals_split_and_inf_skipped():
    """`equals` -> min and max; infinite max produces no constraint."""
    conv = _groups()
    data = conv.convert({"g": {"techs": ["t"], "energy_cap_equals": 5, "storage_cap_max": math.inf}}, "s")
    assert sorted((i.vtype, i.sense) for i in data.groups[0].items) == [("flow_cap", "max"), ("flow_cap", "min")]


def test_membership_lookup():
    """Members are a single (nodes, techs) lookup."""
    conv = _groups()
    data = conv.convert({"g": {"techs": ["t1", "t2"], "locs": ["A"], "energy_cap_max": 1}}, "s")
    defs = data.data_definitions()
    assert defs["group_g__members"]["index"] == [["A", "t1"], ["A", "t2"]]
    assert defs["group_g__flow_cap_max"] == 1


def test_math_is_gated_on_active_members():
    """Constraints only build if members are active (avoids empty constraints)."""
    conv = _groups()
    conv.convert({"g": {"techs": ["t"], "locs": ["A"], "cost_max": {"co2": 3}}}, "s")
    constraint = conv.math()["constraints"]["group_g__cost_max__co2_constraint"]
    assert constraint["foreach"] == ["costs"]
    assert "any(group_g__members, over=[nodes, techs])" in constraint["where"]
    assert "where(cost, group_g__members)" in constraint["equations"][0]["expression"]


def test_unknown_tech_and_bad_values_are_warnings():
    """Unknown techs / non-numeric values are reported, never raised."""
    diag = Diagnostics()
    conv = _groups(diag)
    conv.convert(
        {"g": {"techs": ["unknown"], "energy_cap_max": "lots"}, "h": "not a mapping", "i": {"weird_max": 1}}, "s"
    )
    codes = {m.code for m in diag.messages}
    assert {"unknown_tech", "bad_value", "bad_group", "unknown_group_constraint"} <= codes


def test_redefined_group_gets_own_id():
    """The same group name with different members in two scopes stays two constraints."""
    conv = _groups()
    a = conv.convert({"g": {"techs": ["t1"], "energy_cap_max": 1}}, "o1")
    b = conv.convert({"g": {"techs": ["t2"], "energy_cap_max": 2}}, "o2")
    assert a.groups[0].gid != b.groups[0].gid


def test_dspt_group():
    """demand_share_per_timestep_decision with an infinite share has no sum constraint."""
    conv = _groups()
    conv.convert({"g": {"techs": ["t"], "locs": ["A"], "demand_share_per_timestep_decision": {"heat": math.inf}}}, "s")
    names = list(conv.math()["constraints"])
    assert any("main_min" in n for n in names)
    assert any("main_max" in n for n in names)
    assert not any("dspt_sum" in n for n in names)
    assert ("A", "t", "heat") in conv.dspt_members


def test_config_conversion():
    """model/run keys map to v0.7 config and data_definitions."""
    diag = Diagnostics()
    res = ConfigConverter(diag).convert(
        {
            "name": "m",
            "subset_time": ["2018-01-01", "2018-01-02"],
            "time": {"function": "resample", "function_options": {"resolution": "6H"}},
        },
        {
            "mode": "spores",
            "solver": "gurobi_persistent",
            "solver_options": {"Threads": 2},
            "objective_options": {"cost_class": {"monetary": 1, "spores_score": 0}},
            "relax_constraint": {"demand_share_per_timestep_decision_main_constraint": 0.02},
            "spores_options": {
                "spores_number": 5,
                "slack": 0.1,
                "objective_cost_class": {"spores_score": 1, "excl_score": 10},
            },
        },
        "base",
        is_base=True,
    )
    init, solve = res.config["init"], res.config["solve"]
    assert init["subset"]["timesteps"] == ["2018-01-01", "2018-01-02"]
    assert init["resample"]["timesteps"] == "6h"
    assert init["mode"] == "spores"
    assert init["distance_unit"] == "m"
    assert solve["solver"] == "gurobi"
    assert solve["spores"]["number"] == 5
    assert res.data_definitions["objective_cost_weights"] == {"data": [1.0], "index": ["monetary"], "dims": "costs"}
    assert res.data_definitions["demand_share_relaxation"] == 0.02
    assert res.data_definitions["spores_slack"] == 0.1
    assert res.needs_spores_objective


def test_config_unknown_keys_warn():
    """Unsupported model/run keys are dropped with a warning."""
    diag = Diagnostics()
    ConfigConverter(diag).convert(
        {"time": {"function": "apply_clustering"}, "odd": 1}, {"odd_run": 1}, "s", is_base=False
    )
    codes = {m.code for m in diag.messages}
    assert {"unsupported_time", "unknown_model_key", "unknown_run_key"} <= codes


def _family(conv, copies, scope="s"):
    return conv.convert(
        {
            f"biofuel_{i}": {"techs": techs, "locs": [node], "carrier_prod_max": {"biofuel": value}}
            for i, (node, techs, value) in enumerate(copies, start=1)
        },
        scope,
    )


def test_group_family_contracted():
    """Per-region copies (one location each, same techs/constraints) -> one group with node-indexed values."""
    conv = _groups()
    data = _family(conv, [("A", ["bio"], 10), ("B", ["bio"], 20)])
    conv.contract_families([data], ["base"])
    (group,) = data.groups
    defs = data.data_definitions()
    assert (group.gid, group.sources) == ("biofuel", ["biofuel_1", "biofuel_2"])
    assert defs["group_biofuel__members"] == {"data": [True], "index": ["bio"], "dims": "techs"}
    assert defs["group_biofuel__flow_out_max__biofuel"] == {"data": [10, 20], "index": ["A", "B"], "dims": "nodes"}
    (constraint,) = conv.math()["constraints"].values()
    assert constraint["foreach"] == ["nodes", "carriers"]
    assert constraint["equations"][0]["expression"] == (
        "sum(where(group_flow_out_annual, group_biofuel__members), over=[techs]) <= group_biofuel__flow_out_max__biofuel"
    )


def test_group_family_not_contracted():
    """Copies with different techs, or a copy also defined in another scope, stay separate."""
    conv = _groups()
    data = _family(conv, [("A", ["bio"], 10), ("B", ["other"], 20)])
    conv.contract_families([data], ["s"])
    assert [g.name for g in data.groups] == ["biofuel_1", "biofuel_2"]

    conv = _groups()
    base = _family(conv, [("A", ["bio"], 10), ("B", ["bio"], 20)], "base")
    override = conv.convert({"biofuel_2": {"techs": ["bio"], "locs": ["B"], "carrier_prod_max": {"biofuel": 5}}}, "o")
    conv.contract_families([base, override], ["base", "o"])
    assert [g.name for g in base.groups] == ["biofuel_1", "biofuel_2"]


def test_group_family_named_after_override():
    """Families from an override carry its name: `biofuel_1..` and `1_biofuel..` in different overrides never clash."""
    conv = _groups()
    supply = _family(conv, [("A", ["bio"], 10), ("B", ["bio"], 20)], "biofuel_maximum")
    demand = conv.convert(
        {
            f"{i}_biofuel": {"techs": ["t"], "locs": [n], "carrier_con_max": {"biofuel": -5}}
            for i, n in ((1, "A"), (2, "B"))
        },
        "annual_fuel_demand",
    )
    conv.contract_families([supply, demand], ["biofuel_maximum", "annual_fuel_demand"])
    assert [g.gid for g in supply.groups + demand.groups] == ["biofuel_maximum_biofuel", "annual_fuel_demand_biofuel"]


def test_group_family_names_do_not_matter():
    """Copies with the node in the middle of the name or none at all (`ALB_1_diesel`, `biofuel_C`) are merged."""
    conv = _groups()
    data = conv.convert(
        {
            "A_1_diesel": {"techs": ["bio"], "locs": ["A"], "carrier_con_min": {"diesel": -1}},
            "B_2_diesel": {"techs": ["bio"], "locs": ["B"], "carrier_con_min": {"diesel": -2}},
            "diesel_C": {"techs": ["bio"], "locs": ["C"], "carrier_con_min": {"diesel": -3}},
        },
        "s",
    )
    conv.contract_families([data], ["base"])
    (group,) = data.groups
    assert (group.gid, group.sources) == ("diesel", ["A_1_diesel", "B_2_diesel", "diesel_C"])
    assert data.data_definitions()["group_diesel__flow_in_min__diesel"] == {
        "data": [1.0, 2.0, 3.0],
        "index": ["A", "B", "C"],
        "dims": "nodes",
    }


def test_group_family_same_node():
    """Two groups with the same structure at one node: only copies with matching names (`x_1..`) are merged."""
    conv = _groups()
    data = conv.convert(
        {
            f"{name}": {"techs": ["bio"], "locs": [node], "carrier_prod_max": {"biofuel": 1}}
            for name, node in (("cap_1", "A"), ("cap_2", "B"), ("limit_1", "A"), ("other", "C"))
        },
        "s",
    )
    conv.contract_families([data], ["base"])
    assert [g.gid for g in data.groups] == ["cap", "limit_1", "other"]


def test_spores_objective_classes_must_be_declared_in_v06():
    """v0.6 builds its objective from `objective_options.cost_class` only: undeclared SPORES classes are warned."""
    diag = Diagnostics()
    conv = ConfigConverter(diag)
    conv.convert(None, {"objective_options": {"cost_class": {"monetary": 1}}}, "base", is_base=True)
    conv.convert(
        None,
        {"spores_options": {"objective_cost_class": {"spores_score": 1, "pv": -1, "monetary": 0}}},
        "a",
        is_base=False,
    )
    conv.convert(
        None,
        {
            "objective_options": {"cost_class": {"monetary": 1, "spores_score": 0, "pv": 0}},
            "spores_options": {"objective_cost_class": {"spores_score": 1, "pv": 1}},
        },
        "b",
        is_base=False,
    )
    conv.check_spores_objective()
    warnings = [m for m in diag.messages if m.code == "spores_objective_v06"]
    assert len(warnings) == 1
    assert warnings[0].source == "a"
    assert "['pv', 'spores_score']" in warnings[0].message
