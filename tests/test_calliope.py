"""Converted models load, build and solve with Calliope v0.7.

Run in the `calliope` pixi environment: `pixi run test-calliope`.
These are checks of the converter output; real model runs stay outside this tool.
"""

import logging
import warnings

import pytest

calliope = pytest.importorskip("calliope")
pytestmark = pytest.mark.calliope

SCENARIOS = [None, "ev", "policy", "everything", "res_2h", "no_hydro", "cheap_battery", "spores_run"]


@pytest.fixture(scope="module")
def entry(converted_mini):
    logging.getLogger("calliope").setLevel(logging.ERROR)
    _, out = converted_mini
    return out / "sub/model.yaml"


def _solve(entry, scenario):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = calliope.read_yaml(entry, scenario=scenario)
        model.build(backend="highs")
        model.solve(solver="highs")
    return model


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_scenarios_solve(entry, scenario, tmp_path, monkeypatch):
    """Every scenario of the mini model solves to optimality without unmet demand."""
    monkeypatch.chdir(tmp_path)  # SPORES results are saved relative to the working directory
    model = _solve(entry, scenario)
    assert model.runtime.termination_condition == "optimal"
    assert float(abs(model.results.unmet_demand).sum()) == pytest.approx(0, abs=1e-6)


def test_conversion_plus_ratio_respected(entry):
    """CHP heat output follows the legacy carrier ratio (heat = 1.2 * electricity)."""
    model = _solve(entry, None)
    flow = model.results.flow_out.sel(techs="chp", nodes="A")
    elec, heat = flow.sel(carriers="electricity"), flow.sel(carriers="heat")
    assert (heat - 1.2 * elec).fillna(0).pipe(abs).max() == pytest.approx(0, abs=1e-6)


def test_conversion_plus_or_tiers(entry):
    """v0.6 docs examples: CCHP (OR within tier 2) and air source heat pump (OR within tier 1)."""
    model = _solve(entry, None)
    out = model.results.flow_out.sel(nodes="B").fillna(0)
    inflow = model.results.flow_in.sel(nodes="B").fillna(0)
    assert float(out.sel(techs=["cchp", "ahp"], carriers="cooling").sum()) > 0  # cooling demand is met by them
    cchp = out.sel(techs="cchp")
    elec, heat, cool = (cchp.sel(carriers=c) for c in ("electricity", "heat", "cooling"))
    assert (elec - heat / 0.8 - cool / 0.5).pipe(abs).max() == pytest.approx(0, abs=1e-6)
    gas = inflow.sel(techs="cchp", carriers="gas")
    assert (elec - 0.45 * gas).pipe(abs).max() == pytest.approx(0, abs=1e-6)
    ahp = out.sel(techs="ahp")
    power = inflow.sel(techs="ahp", carriers="electricity")
    balance = ahp.sel(carriers="heat") / 5 + ahp.sel(carriers="cooling") / 3 - power
    assert balance.pipe(abs).max() == pytest.approx(0, abs=1e-6)


def test_group_constraint_binds(entry):
    """The legacy group cap on gas capacity at A is enforced."""
    model = _solve(entry, None)
    caps = model.results.flow_cap.sel(nodes="A", techs=["gas_plant", "chp"], carriers="electricity")
    assert float(caps.sum()) <= 50 + 1e-6


def test_shadow_prices_reported(entry):
    """The energy balance duals are part of the standard results."""
    model = _solve(entry, "policy")
    assert "shadow_price_system_balance" in model.results


def test_complex_conversion_plus(tmp_path):
    """v0.6 docs "Complex fictional technology": every v0.6 tier balance holds, with mixes within OR tiers."""
    from pathlib import Path

    from converter.convert import convert_model

    fixture = Path(__file__).parent / "fixtures" / "complex_conversion_plus"
    result = convert_model(fixture, tmp_path / "model")
    mix = {
        "techs.supply_oil.flow_cap_max": 4,
        "techs.supply_biomass.flow_cap_max": 3,
        "techs.supply_coal.flow_cap_max": 3,
        "techs.sink_high_T_heat.sink_use_equals": 2,
        "techs.sink_mid_T_heat.sink_use_equals": 1,
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = calliope.read_yaml(result.output_entry, override_dict=mix)
        model.build(backend="highs")
        model.solve(solver="highs")
    assert model.runtime.termination_condition == "optimal"
    out = model.results.flow_out.sel(techs="complex", nodes="X").fillna(0)
    inp = model.results.flow_in.sel(techs="complex", nodes="X").fillna(0)
    o = {c: out.sel(carriers=c) for c in ("high_T_heat", "electricity", "mid_T_heat", "cooling", "low_T_heat")}
    i = {c: inp.sel(carriers=c) for c in ("coal", "gas", "oil", "biomass", "waste")}
    assert all(float(v.sum()) > 0 for v in [*o.values(), *i.values()])  # every carrier of every OR tier is used
    tier1 = o["high_T_heat"] / 0.8 + o["electricity"] / 0.6
    balances = [
        tier1 - (1.2 * i["coal"] + 1.0 * i["gas"] + 1.6 * i["oil"]),  # primary (energy_eff = 1)
        i["coal"] / 1.2 + i["gas"] / 1 + i["oil"] / 1.6 - (i["biomass"] / 1 + i["waste"] / 1.25),  # in_2
        tier1 - (o["mid_T_heat"] / 0.3 + o["cooling"] / 0.2),  # out_2
        tier1 - o["low_T_heat"] / 0.15,  # out_3
    ]
    for balance in balances:
        assert float(abs(balance).max()) == pytest.approx(0, abs=1e-6)
    # v0.6 capacity: one `energy_cap` shared by the tier-1 outputs, on the primary carrier (electricity)
    cap = float(model.results.flow_cap.sel(techs="complex", nodes="X", carriers="electricity"))
    assert float((o["high_T_heat"] + o["electricity"]).max()) <= cap + 1e-6
    assert float((o["high_T_heat"] + o["electricity"]).max()) > float(o["electricity"].max())  # heat uses it too


def test_generated_run_script_merges_overrides(legacy_copy, tmp_path):
    """The v0.7 run script merges overrides that set the same keys (v0.6 "later wins") and the model solves."""
    import importlib.util

    from converter.convert import convert_model

    (legacy_copy / "run-test.py").write_text('scenarios_string = "ev_at_c,ev_everywhere,cheap_battery"\n')
    out = tmp_path / "out"
    convert_model(legacy_copy, out)
    spec = importlib.util.spec_from_file_location("run_test", tmp_path / "run-test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    merged = module.v06_overrides(module.MODEL, ["ev_at_c", "ev_everywhere", "cheap_battery"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = calliope.read_yaml(module.MODEL, override_dict=merged)
        model.build(backend="highs")
        model.solve(solver="highs")
    assert model.runtime.termination_condition == "optimal"


def test_time_varying_limits(entry):
    """run.py time-varying limits (math/time_varying_limits.yaml) hold on the solved model, and bind."""
    model = _solve(entry, "ev")
    r, i = model.results, model.inputs
    checks = [
        ("pv", "flow_out", "flow_cap_max_relative_per_ts", "electricity"),
        ("heat_pump", "flow_out", "flow_cap_equals_relative_per_ts", "heat"),
        ("ev_battery", "flow_in", "flow_in_max_relative_per_ts", "electricity"),
    ]
    for tech, flow, param, carrier in checks:
        caps = r.flow_cap.sel(techs=tech, carriers=carrier).to_series().dropna()
        assert (caps > 1e-6).any(), f"{tech} is not built: the check would be trivial"
        for node, cap in caps.items():
            value = r[flow].sel(techs=tech, nodes=node, carriers=carrier).fillna(0)
            limit = cap * i.timestep_resolution * i[param].sel(techs=tech, nodes=node)
            gap = value - limit
            if param.startswith("flow_cap_equals"):
                assert float(abs(gap).max()) == pytest.approx(0, abs=1e-6)
            else:
                assert float(gap.max()) <= 1e-6


def test_resampled_timeseries_are_averaged(entry):
    """Resampling averages custom timeseries as v0.6 did (v0.7 defaults custom parameters to `first`)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        hourly = calliope.read_yaml(entry, scenario="ev").inputs
        resampled = calliope.read_yaml(
            entry, scenario="ev", override_dict={"config.init.resample": {"timesteps": "2h"}}
        ).inputs
    params = ["flow_cap_max_relative_per_ts", "flow_cap_equals_relative_per_ts", "flow_in_max_relative_per_ts"]
    for param in [*params, "flow_out_eff"]:  # a custom parameter, and a base one (math/resample.yaml)
        expected = hourly[param].resample(timesteps="2h").mean()
        assert "timesteps" in hourly[param].dims
        assert hourly[param].notnull().any()
        assert float(abs(resampled[param] - expected).max()) == pytest.approx(0, abs=1e-9), param


def test_time_varying_limits_on_tier1_sums(entry):
    """Conversion techs: time-varying limits apply to the sum of tier-1 flows (v2g-like outputs, and inputs).

    v0.6 measured them against the one `energy_cap`, i.e. the primary output's `flow_cap`.
    """
    model = _solve(entry, None)
    r, i = model.results, model.inputs
    cap = r.flow_cap.sel(techs="ahp", carriers="heat").to_series().dropna()
    assert (cap > 1e-6).any()
    for node, value in cap.items():
        limit = value * i.timestep_resolution * i.flow_cap_max_relative_per_ts.sel(techs="ahp", nodes=node)
        out = r.flow_out.sel(techs="ahp", nodes=node).fillna(0)
        assert float((out.sel(carriers="heat") + out.sel(carriers="cooling") - limit).max()) <= 1e-6
        limit_in = value * i.timestep_resolution * i.flow_in_max_relative_per_ts.sel(techs="ahp", nodes=node)
        power = r.flow_in.sel(techs="ahp", nodes=node, carriers="electricity").fillna(0)
        assert float((power - limit_in).max()) <= 1e-6
    names = list(model.backend.constraints)
    assert any(n.startswith("max_time_varying_flow_out_sum") for n in names)
    assert any(n.startswith("max_time_varying_flow_in_sum") for n in names)


def test_fuel_distribution_balances_over_the_horizon(legacy_copy, tmp_path):
    """run.py fuel distribution: what all nodes import equals what all nodes export, over the whole horizon."""
    from converter.convert import convert_model
    from test_fuel_distribution import add_fuel_distribution

    add_fuel_distribution(legacy_copy)
    techs = legacy_copy / "model/techs.yaml"
    text = techs.read_text(encoding="utf-8")
    # B sells a fixed 0.5 per timestep to the distribution, so A must import exactly that in total.
    old = "            resource: -.inf\n            force_resource: false\n"
    assert old in text
    text = text.replace(old, "            resource: -0.5\n            force_resource: true\n", 1)
    techs.write_text(text, encoding="utf-8")
    result = convert_model(legacy_copy, tmp_path / "out")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = calliope.read_yaml(result.output_entry)
        model.build(backend="highs")
        model.solve(solver="highs")
    assert model.runtime.termination_condition == "optimal"
    imported = model.results.flow_out.sel(techs="gas_distribution_import", carriers="gas").fillna(0).sum("nodes")
    exported = model.results.flow_in.sel(techs="gas_distribution_export", carriers="gas").fillna(0).sum("nodes")
    assert float(exported.min()) == pytest.approx(0.5)  # forced: the balance binds
    assert float(imported.sum()) == pytest.approx(float(exported.sum()), rel=1e-6)


def test_storage_charge_depth(legacy_copy, tmp_path):
    """Legacy `storage_charge_depth`: the stored energy never exceeds that share of the storage capacity."""
    from converter.convert import convert_model
    from test_storage_ev import add_charge_depth

    add_charge_depth(legacy_copy, "0.8")
    result = convert_model(legacy_copy, tmp_path / "out")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = calliope.read_yaml(result.output_entry)
        model.build(backend="highs")
        model.solve(solver="highs")
    assert model.runtime.termination_condition == "optimal"
    storage = model.results.storage.sel(techs="battery").fillna(0)
    cap = model.results.storage_cap.sel(techs="battery").fillna(0)
    assert float(cap.sum()) > 1e-6
    assert float((storage - 0.8 * cap).max()) <= 1e-6
    assert "storage_charge_depth_limit" in model.backend.constraints


def _solve_converted(legacy, out):
    from converter.convert import convert_model

    result = convert_model(legacy, out)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = calliope.read_yaml(result.output_entry)
        model.build(backend="highs")
        model.solve(solver="highs")
    assert model.runtime.termination_condition == "optimal"
    return model


def test_capacity_factor_limits(legacy_copy, tmp_path):
    """run.py capacity factors: weighted output over the horizon vs flow_cap * factor * hours (primary carrier)."""
    from test_runpy_limits import add_capacity_factors

    add_capacity_factors(legacy_copy)
    model = _solve_converted(legacy_copy, tmp_path / "out")
    r, i = model.results, model.inputs
    hours = float((i.timestep_resolution * i.timestep_weights).sum())
    for tech, carrier, factor, sense in [("hydro_reservoir", "electricity", 0.2, "max"), ("ahp", "heat", 0.2, "min")]:
        caps = r.flow_cap.sel(techs=tech, carriers=carrier).to_series().dropna()
        assert (caps > 1e-6).any(), f"{tech} is not built: the check would be trivial"
        for node, cap in caps.items():
            out = float((r.flow_out.sel(techs=tech, nodes=node, carriers=carrier) * i.timestep_weights).sum())
            gap = out - cap * factor * hours
            assert gap >= -1e-6 if sense == "min" else gap <= 1e-6
    limited = model.backend.get_constraint("capacity_factor_min_limit", as_backend_objs=False)
    carriers = limited.sel(techs="ahp").dropna("carriers", how="all").carriers.values
    assert list(carriers) == ["heat"]  # the primary output only, not the ahp's cooling


def test_per_month_limit_binds(legacy_copy, tmp_path):
    """run.py per-month max: at most half the horizon's output in January, i.e. none in a January-only model."""
    from test_runpy_limits import add_per_month

    reference = _solve_converted(legacy_copy, tmp_path / "reference")
    assert float(reference.results.flow_out.sel(techs="hydro_reservoir").fillna(0).sum()) > 1e-3
    add_per_month(legacy_copy, share=0.5)
    model = _solve_converted(legacy_copy, tmp_path / "out")
    assert float(model.results.flow_out.sel(techs="hydro_reservoir").fillna(0).sum()) == pytest.approx(0, abs=1e-6)


def test_om_investment_fraction_on_annualised_investment(entry):
    """v0.6 `om_annual_investment_fraction` is a fraction of the annualised investment (not the whole one)."""
    model = _solve(entry, None)
    r = model.results
    fixed = r.cost_operation_fixed.sel(costs="monetary").to_series().dropna()
    links = [t for t in fixed.index.get_level_values("techs") if "ac_line" in t]
    assert links, "no transmission line built: the check would be trivial"
    for node, tech in fixed.index:
        if tech not in links:
            continue
        annualised = float(r.cost_investment_annualised.sel(nodes=node, techs=tech, costs="monetary"))
        assert annualised > 0
        assert fixed[(node, tech)] == pytest.approx(0.01 * annualised, rel=1e-6)
