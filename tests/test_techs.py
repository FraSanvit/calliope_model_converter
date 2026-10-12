"""Tech context resolution and fragment conversion."""

import dataclasses

import pytest
from converter.context import TechContext
from converter.diagnostics import Diagnostics
from converter.legacy import load_legacy_model
from converter.techs import FragmentConverter, conversion_plus_math, parse_file_ref


@pytest.fixture(scope="module")
def ctx(mini_legacy):
    diag = Diagnostics()
    legacy = load_legacy_model(mini_legacy / "model/sub/model.yaml", mini_legacy / "model", diag)
    return TechContext(legacy, diag)


@pytest.fixture
def conv(ctx):
    return FragmentConverter(ctx, Diagnostics())


@pytest.mark.parametrize(
    ("tech", "abstract", "base_tech"),
    [
        ("pv", "supply", "supply"),
        ("hydro_reservoir", "supply_plus", "supply"),
        ("chp", "conversion_plus", "conversion"),
        ("ac_line", "transmission", "transmission"),
        ("demand_heat", "demand", "demand"),
    ],
)
def test_base_resolution(ctx, tech, abstract, base_tech):
    """Parent chains resolve to the root abstract base."""
    info = ctx.get(tech)
    assert info.abstract == abstract
    assert info.base_tech == base_tech


def test_templates_chain(ctx):
    """User groups and extended built-in groups both become templates."""
    assert ctx.get("pv").chain == ["pv_group", "supply"]
    assert ctx.get("pv").parent_template == "pv_group"
    assert ctx.get("gas_plant").parent_template == "base_supply"


def test_conversion_plus_carriers(ctx):
    """Tiers and primary carriers are resolved."""
    chp = ctx.get("chp")
    assert chp.tiers == {"in": ["gas"], "out": ["electricity"], "out_2": ["heat"]}
    assert chp.primary_out == "electricity"
    assert chp.cap_carrier == "electricity"


def test_supply_plus_storage_detection(ctx):
    """supply_plus with storage params gets include_storage."""
    assert ctx.get("hydro_reservoir").include_storage


def test_storage_efficiency_split(conv):
    """v0.6 storage `energy_eff` applies to both charge and discharge."""
    res = conv.convert("battery", {"constraints": {"energy_eff": 0.9}}, scope="t")
    assert res.params["flow_in_eff"] == 0.9
    assert res.params["flow_out_eff"] == 0.9


def test_demand_resource_is_negated_and_forced(conv):
    """Demand `resource` becomes a positive `sink_use_equals` (forced by default)."""
    res = conv.convert("demand_elec", {"constraints": {"resource": -5}}, scope="t")
    assert res.params == {"sink_use_equals": 5}
    res = conv.convert("demand_elec", {"constraints": {"resource": "file=x.csv"}}, scope="t")
    assert res.timeseries[0].param == "sink_use_equals"
    assert res.timeseries[0].transform == "negate"


def test_infinite_forced_resource_becomes_max(conv):
    """A forced infinite resource is converted as an upper bound."""
    res = conv.convert("demand_elec", {"constraints": {"resource": float("-inf")}}, scope="t")
    assert res.params == {"sink_use_max": float("inf")}
    assert conv.diag.by_code("infinite_forced_resource")


def test_unforced_supply_resource(conv):
    """Unforced supply resource becomes `source_use_max`."""
    res = conv.convert("gas_plant", {"constraints": {"resource": 3, "force_resource": False}}, scope="t")
    assert res.params == {"source_use_max": 3}


def test_equals_becomes_min_and_max(conv):
    """`*_equals` -> min == max."""
    res = conv.convert("battery", {"constraints": {"storage_cap_equals": 4}}, scope="t")
    assert res.params == {"storage_cap_min": 4, "storage_cap_max": 4}


def test_conversion_capacity_on_output_carrier(conv):
    """Capacity params and flow costs of conversion techs are indexed on the output carrier."""
    res = conv.convert(
        "boiler",
        {"constraints": {"energy_cap_max": 10}, "costs": {"monetary": {"energy_cap": 5, "om_con": 1}}},
        scope="t",
    )
    assert res.params["flow_cap_max"] == {"data": 10, "index": "heat", "dims": "carriers"}
    rows = {r.param: r for r in res.cost_rows}
    assert rows["cost_flow_cap"].carrier == "heat"
    assert rows["cost_flow_in"].carrier == "gas"


def test_om_con_on_supply_is_source_use(conv):
    """Supply `om_con` -> `cost_source_use` (no carrier)."""
    res = conv.convert("gas_plant", {"costs": {"monetary": {"om_con": 0.1}}}, scope="t")
    (row,) = res.cost_rows
    assert row.param == "cost_source_use"
    assert row.carrier is None


def test_carrier_ratio_tiers(conv):
    """Tier 2 becomes a neutral ratio; energy_eff goes on the tier-1 output carrier."""
    res = conv.convert(
        "chp", {"constraints": {"energy_eff": 0.4, "carrier_ratios": {"carrier_out_2": {"heat": 2.0}}}}, scope="t"
    )
    assert res.params["carrier_out_2_to_carrier_out_1_ratio"] == {"data": 2.0, "index": "heat", "dims": "carriers"}
    assert res.params["flow_out_eff"] == {"data": 0.4, "index": "electricity", "dims": "carriers"}
    assert not any("lookup" in p or p.startswith("carrier_out_1") for p in res.params)


def test_or_tier_becomes_efficiencies(ctx, monkeypatch):
    """v0.6 OR (several carriers in tier 1) -> per-carrier `flow_out_eff` = energy_eff * ratio (docs heat pump)."""
    info = ctx.info["heat_pump"]
    monkeypatch.setitem(
        ctx.info, "heat_pump", dataclasses.replace(info, tiers={**info.tiers, "out": ["heat", "cooling"]})
    )
    conv = FragmentConverter(ctx, Diagnostics())
    res = conv.convert(
        "heat_pump", {"constraints": {"carrier_ratios": {"carrier_out": {"heat": 5, "cooling": 3}}}}, scope="t"
    )
    assert res.params["flow_out_eff"] == {"data": [5.0, 3.0], "index": ["heat", "cooling"], "dims": "carriers"}


def test_time_varying_carrier_ratio(conv):
    """A `file=` tier-1 ratio (COP) becomes a `flow_out_eff` timeseries on its carrier."""
    res = conv.convert(
        "heat_pump", {"constraints": {"carrier_ratios": {"carrier_out": {"heat": "file=cop.csv"}}}}, scope="t"
    )
    (ref,) = [r for r in res.timeseries if r.param == "flow_out_eff"]
    assert (ref.transform, ref.factor) == (None, None)
    assert ref.extra_dims == (("carriers", "heat"),)


def test_time_varying_tier_ratio_inverted(conv):
    """A `file=` ratio on the tier-1 carrier is inverted and scaled in the tier-2 ratio."""
    res = conv.convert(
        "chp",
        {
            "constraints": {
                "carrier_ratios": {"carrier_out": {"electricity": "file=r.csv"}, "carrier_out_2": {"heat": 2}}
            }
        },
        scope="t",
    )
    (ref,) = [r for r in res.timeseries if r.param == "carrier_out_2_to_carrier_out_1_ratio"]
    assert (ref.transform, ref.factor) == ("invert", 2.0)
    assert ref.extra_dims == (("carriers", "heat"),)


def test_custom_params_renamed(conv):
    """run.py inputs get math-gallery names."""
    res = conv.convert(
        "chp", {"constraints": {"cb": 0.5, "cv": 0.1, "energy_cap_max_time_varying": "file=a.csv"}}, scope="t"
    )
    assert res.params["power_to_heat_ratio"] == 0.5
    assert res.params["power_loss_factor"] == 0.1
    assert res.timeseries[0].param == "flow_cap_max_relative_per_ts"


def test_unknown_param_kept_with_warning(conv):
    """Unknown params are copied and reported."""
    res = conv.convert("pv", {"constraints": {"my_custom_thing": 3}}, scope="t")
    assert res.params["my_custom_thing"] == 3
    assert conv.diag.by_code("unknown_param")


def test_spores_score_marks_tracking(conv):
    """Legacy spores_score costs mark techs for SPORES tracking instead of becoming costs."""
    res = conv.convert("pv", {"costs": {"spores_score": {"energy_cap": 0}, "excl_score": {"energy_cap": 0}}}, scope="t")
    assert res.spores_tracked
    assert [r.cost for r in res.cost_rows] == ["excl_score"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("file=a.csv", ("a.csv", None)),
        ('"file=a.csv"', ("a.csv", None)),
        ("file=a.csv:R1", ("a.csv", "R1")),
        (3, None),
        ("text", None),
    ],
)
def test_parse_file_ref(value, expected):
    """`file=` references, quoted or with a column."""
    assert parse_file_ref(value) == expected


def test_multi_carrier_tier1_link_is_weighted(ctx, monkeypatch):
    """Tier 1 with several carriers at different ratios + a tier 2: tier-1 flows weighted by r_ref / r_c."""
    info = ctx.info["chp"]
    tiers = {**info.tiers, "out": ["electricity", "steam"], "out_2": ["heat"]}
    monkeypatch.setitem(ctx.info, "chp", dataclasses.replace(info, tiers=tiers))
    conv = FragmentConverter(ctx, Diagnostics())
    ratios = {"carrier_out": {"electricity": 1, "steam": 2}, "carrier_out_2": {"heat": 0.5}}
    res = conv.convert("chp", {"constraints": {"energy_eff": 0.4, "carrier_ratios": ratios}}, scope="t")
    assert res.params["flow_out_eff"] == {"data": [0.4, 0.8], "index": ["electricity", "steam"], "dims": "carriers"}
    assert res.params["carrier_out_2_to_carrier_out_1_ratio"] == {"data": 0.5, "index": "heat", "dims": "carriers"}
    (link,) = [c for n, c in conversion_plus_math([ctx.info["chp"]])["constraints"].items() if n.startswith("link")]
    assert link["equations"][0]["expression"] == (
        "(flow_out[carriers=electricity] + flow_out[carriers=steam] * flow_out_eff[carriers=electricity] "
        "/ flow_out_eff[carriers=steam]) * carrier_out_2_to_carrier_out_1_ratio[carriers=heat] == flow_out[carriers=heat]"
    )
