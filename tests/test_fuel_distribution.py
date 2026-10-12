"""run.py `fuel_distribution_constraint` -> math/fuel_distribution.yaml (annual import/export balance, as run.py)."""

from converter.convert import convert_model
from ruamel.yaml import YAML

FUEL_TECHS = """
    gas_distribution_import:
        essentials:
            name: 'Gas bought from the distribution'
            parent: supply
            carrier: gas
        constraints.resource: .inf
        costs.monetary.om_prod: 0.001
    gas_distribution_export:
        essentials:
            name: 'Gas sold to the distribution'
            parent: demand
            carrier: gas
        constraints:
            resource: -.inf
            force_resource: false
"""


def add_fuel_distribution(legacy) -> None:
    """Gas can be bought from the distribution at A and sold to it at B (A has no gas supply of its own here)."""
    techs = legacy / "model/techs.yaml"
    text = techs.read_text(encoding="utf-8")
    techs.write_text(text.replace("techs:\n", "techs:\n" + FUEL_TECHS, 1), encoding="utf-8")
    locations = legacy / "model/sub/locations.yaml"
    text = locations.read_text(encoding="utf-8")
    text = text.replace("    A:\n        coordinates:", "    A:\n        coordinates:", 1)
    text = text.replace(
        "        available_area: 10\n        techs:\n",
        "        available_area: 10\n        techs:\n            gas_distribution_import:\n",
        1,
    )
    text = text.replace(
        "        coordinates: {lat: 45.5, lon: 8.5}\n        techs:\n",
        "        coordinates: {lat: 45.5, lon: 8.5}\n        techs:\n            gas_distribution_export:\n",
        1,
    )
    locations.write_text(text, encoding="utf-8")


def test_fuel_distribution_math(legacy_copy, tmp_path):
    """Import/export pairs get lookups and one balance over the whole horizon (as run.py)."""
    add_fuel_distribution(legacy_copy)
    out = tmp_path / "out"
    result = convert_model(legacy_copy, out, copy_csvs=False)
    yaml = YAML(typ="safe", pure=True)
    math = yaml.load((out / "math/fuel_distribution.yaml").read_text(encoding="utf-8"))
    constraint = math["constraints"]["fuel_distribution_balance"]
    assert constraint["foreach"] == ["carriers"]
    assert constraint["where"].startswith("[gas] in carriers")
    assert constraint["equations"][0]["expression"] == (
        "sum(where(flow_out, fuel_distribution_import), over=[nodes, techs, timesteps]) == "
        "sum(where(flow_in, fuel_distribution_export), over=[nodes, techs, timesteps])"
    )
    techs = yaml.load((out / "techs.yaml").read_text(encoding="utf-8"))["techs"]
    assert techs["gas_distribution_import"]["fuel_distribution_import"] is True
    assert techs["gas_distribution_export"]["fuel_distribution_export"] is True
    assert "fuel_distribution" in {m.code for m in result.diagnostics.messages}


def test_no_fuel_distribution_without_pairs(converted_mini):
    _, out = converted_mini
    assert not (out / "math/fuel_distribution.yaml").exists()
