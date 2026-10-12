"""run.py capacity factor limits and per-month production limits."""

import pandas as pd
from converter.convert import convert_model
from ruamel.yaml import YAML

yaml = YAML(typ="safe", pure=True)


def add_capacity_factors(legacy) -> None:
    """The hydro reservoir may run at most 20% of the time; the ahp's heat (primary output) at least 20%."""
    techs = legacy / "model/techs.yaml"
    text = techs.read_text(encoding="utf-8")
    hydro = "            storage_cap_max: 50\n"
    ahp = "            energy_cap_max: 100\n            lifetime: 20\n            energy_cap_max_time_varying:"
    assert hydro in text
    assert ahp in text
    text = text.replace(hydro, hydro + "            capacity_factor_max: 0.2\n", 1)
    text = text.replace(ahp, ahp.replace("lifetime: 20\n", "lifetime: 20\n            capacity_factor_min: 0.2\n"), 1)
    techs.write_text(text, encoding="utf-8")


def add_per_month(legacy, share: float = 0.5) -> None:
    """The hydro reservoir may produce at most `share` of its output in January (the mini model: two January days)."""
    sub = legacy / "model/sub"
    index = pd.read_csv(sub / "charging-profile.csv", index_col=0).index
    pd.DataFrame({n: share / len(index) for n in "ABC"}, index=index).to_csv(sub / "month-share.csv")
    techs = legacy / "model/techs.yaml"
    text = techs.read_text(encoding="utf-8")
    hydro = "            storage_cap_max: 50\n"
    assert hydro in text
    line = '            carrier_prod_per_month_max_time_varying: "file=month-share.csv"\n'
    techs.write_text(text.replace(hydro, hydro + line, 1), encoding="utf-8")


def test_capacity_factor_math(legacy_copy, tmp_path):
    add_capacity_factors(legacy_copy)
    out = tmp_path / "out"
    result = convert_model(legacy_copy, out, copy_csvs=False)
    math = yaml.load((out / "math/capacity_factor.yaml").read_text(encoding="utf-8"))
    assert set(math["constraints"]) == {"capacity_factor_min_limit", "capacity_factor_max_limit"}
    techs = yaml.load((out / "techs.yaml").read_text(encoding="utf-8"))["techs"]
    assert techs["hydro_reservoir"]["capacity_factor_max"] == 0.2
    assert techs["ahp"]["capacity_factor_min"] == 0.2
    assert "primary_carrier_out" in techs["ahp"]  # the limit applies to the primary output only
    assert "capacity_factor" in {m.code for m in result.diagnostics.messages}


def test_per_month_math_and_month_table(legacy_copy, tmp_path):
    add_per_month(legacy_copy)
    out = tmp_path / "out"
    convert_model(legacy_copy, out)
    math = yaml.load((out / "math/flow_out_per_month.yaml").read_text(encoding="utf-8"))
    assert set(math["constraints"]) == {"flow_out_per_month_max"}
    assert math["dimensions"]["months"]["dtype"] == "string"
    assert math["parameters"]["flow_out_per_month_max_relative_per_ts"]["resample_method"] == "mean"
    table = pd.read_csv(out / "data_tables/generated/timestep_month.csv", index_col=0)
    assert list(table.columns) == ["month_01"]  # only months with timesteps
    assert len(table) == 48
    assert (table["month_01"] == 1).all()


def test_per_month_value_is_warned(legacy_copy, tmp_path):
    """run.py applies per-month limits only with a timeseries: a plain value is warned, not translated."""
    techs = legacy_copy / "model/techs.yaml"
    text = techs.read_text(encoding="utf-8")
    gas = "            energy_eff: 0.5\n            energy_cap_max: inf\n"
    techs.write_text(text.replace(gas, gas + "            carrier_prod_per_month_max: 0.2\n", 1), encoding="utf-8")
    out = tmp_path / "out"
    result = convert_model(legacy_copy, out, copy_csvs=False)
    warnings = [m.message for m in result.diagnostics.messages if m.code == "per_month"]
    assert any("gas_plant.flow_out_per_month_max" in w for w in warnings)
    assert not (out / "math/flow_out_per_month.yaml").exists()


def test_no_limits_math_without_params(converted_mini):
    _, out = converted_mini
    assert not (out / "math/capacity_factor.yaml").exists()
    assert not (out / "math/flow_out_per_month.yaml").exists()


def test_om_investment_fraction_math(converted_mini):
    _, out = converted_mini
    math = yaml.load((out / "math/om_investment_fraction.yaml").read_text(encoding="utf-8"))
    expression = math["global_expressions"]["cost_operation_fixed"]["equations"][0]["expression"]
    assert "cost_investment_annualised * cost_om_annual_investment_fraction" in expression
