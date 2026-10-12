"""v0.6 vs v0.7 run comparison on tiny synthetic result files (no Calliope runs)."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from converter.compare import compare

TIMES = pd.date_range("2018-01-01", periods=2, freq="h")


def _write_case(folder, *, v07_gas=1.0, free_gas=False):
    folder.mkdir(parents=True)
    v06 = xr.Dataset(
        {
            "energy_cap": ("loc_techs", [10.0, 5.0, 5.0]),
            "storage_cap": ("loc_techs_store", [4.0]),
            "carrier_prod": (("loc_tech_carriers_prod", "timesteps"), [[2.0, 3.0], [1.0, 1.0]]),
            "carrier_con": (("loc_tech_carriers_con", "timesteps"), [[-4.0, -6.0]]),
            "cost": (("costs", "loc_techs_cost"), [[7.0, 1.0], [2.0, 0.0]]),
        },
        coords={
            "loc_techs": ["A::gas_plant", "A::ac_line:B", "B::ac_line:A"],
            "loc_techs_store": ["A::battery"],
            "loc_tech_carriers_prod": ["A::gas_plant::electricity", "A::ac_line:B::electricity"],
            "loc_tech_carriers_con": ["A::gas_plant::gas"],
            "costs": ["monetary", "co2"],
            "loc_techs_cost": ["A::gas_plant", "A::ac_line:B"],
            "timesteps": TIMES,
        },
        attrs={"objective_function_value": 8.0},
    )
    v06.to_netcdf(folder / "v06.nc")

    nodes, techs, carriers = ["A", "B"], ["gas_plant", "A_to_B_ac_line", "battery"], ["electricity", "gas"]
    flow_cap = np.zeros((2, 3, 2))
    flow_cap[0, 0, 0] = 10.0 * v07_gas
    flow_cap[0, 1, 0] = flow_cap[1, 1, 0] = 5.0
    flow_out = np.full((2, 3, 2, 2), np.nan)
    flow_out[0, 0, 0] = [2.0, 3.0]
    flow_out[0, 1, 0] = [1.0, 1.0]
    flow_in = np.full((2, 3, 2, 2), np.nan)
    flow_in[0, 0, 1] = [4.0, 6.0]
    results = xr.Dataset(
        {
            "flow_cap": (("nodes", "techs", "carriers"), flow_cap),
            "storage_cap": (("nodes", "techs"), [[0.0, 0.0, 4.0], [0.0, 0.0, 0.0]]),
            "flow_out": (("nodes", "techs", "carriers", "timesteps"), flow_out),
            "flow_in": (("nodes", "techs", "carriers", "timesteps"), flow_in),
            "cost": (("nodes", "techs", "costs"), [[[7.0, 2.0], [1.0, 0.0], [0.0, 0.0]], [[0.0, 0.0]] * 3]),
        },
        coords={"nodes": nodes, "techs": techs, "carriers": carriers, "timesteps": TIMES, "costs": ["monetary", "co2"]},
    )
    carrier_out = np.zeros((2, 3, 2), dtype=bool)
    carrier_out[:, :, 0] = True
    # gas_plant and the line have a capacity cost; with `free_gas`, the gas plant's is in an unweighted class only.
    cost_flow_cap = [[0.0, 3.0] if free_gas else [3.0, 0.0], [1.0, 0.0], [0.0, 0.0]]
    inputs = xr.Dataset(
        {
            "carrier_out": (("nodes", "techs", "carriers"), carrier_out),
            "cost_flow_cap": (("techs", "costs"), cost_flow_cap),
            "cost_storage_cap": (("techs", "costs"), [[0.0, 0.0], [0.0, 0.0], [2.0, 0.0]]),
            "objective_cost_weights": ("costs", [1.0, 0.0]),
            "base_tech": ("techs", ["supply", "transmission", "storage"]),
        },
        coords={"nodes": nodes, "techs": techs, "carriers": carriers, "costs": ["monetary", "co2"]},
    )
    inputs.to_netcdf(folder / "v07.nc", group="inputs", mode="w")
    results.to_netcdf(folder / "v07.nc", group="results", mode="a")
    return results


def test_identical_runs_compare_equal(tmp_path):
    """Same solution in both: every section matches (transmission names, flow signs)."""
    _write_case(tmp_path / "case")
    summary = compare(tmp_path / "case", tmp_path / "out")
    for title, stats in summary.items():
        if title.startswith("Objective"):
            continue  # v0.7 files carry no objective value
        assert stats["different"] == 0, title
        assert stats["only v0.6"] == 0, title
    assert (tmp_path / "out" / "comparison.png").stat().st_size > 10_000  # diagnostic figure
    report = (tmp_path / "out" / "comparison.md").read_text(encoding="utf-8")
    assert "![Diagnostic summary](comparison.png)" in report
    assert "Shadow prices" not in report  # not compared (yet)
    assert "weighted error" in report


def test_differences_are_reported(tmp_path):
    """A different capacity shows up as a difference, with diff columns in the CSV tables."""
    _write_case(tmp_path / "case", v07_gas=1.5)
    summary = compare(tmp_path / "case", tmp_path / "out")
    capacities = next(v for k, v in summary.items() if k.startswith("Capacities"))
    assert capacities["different"] == 1
    assert capacities["max |diff|"] == pytest.approx(5.0)
    assert capacities["weighted error"] == pytest.approx(5.0 / 20.0)  # |15 - 10| / (10 + 5 + 5)
    table = pd.read_csv(next((tmp_path / "out").glob("02_*.csv")))
    assert {"v06", "v07", "diff", "rel_diff"} <= set(table.columns)


def test_free_capacities_excluded(tmp_path):
    """A capacity priced only in a cost class the objective ignores is not determined: marked free, not an error."""
    _write_case(tmp_path / "case", v07_gas=1.5, free_gas=True)
    summary = compare(tmp_path / "case", tmp_path / "out")
    capacities = next(v for k, v in summary.items() if k.startswith("Capacities:"))
    assert capacities["free"] == 1
    assert capacities["weighted error"] == pytest.approx(0.0)  # only the (free) gas plant differs
    report = (tmp_path / "out" / "comparison.md").read_text(encoding="utf-8")
    assert "| free |" in report


def test_no_fixed_design_in_compare(tmp_path):
    """The fixed-design check is separate (`converter.fixed_design`): compare writes no override and no verdict."""
    _write_case(tmp_path / "case")
    compare(tmp_path / "case", tmp_path / "out")
    assert not (tmp_path / "out" / "fix_v06_design.yaml").exists()
    assert "Fixed-design" not in (tmp_path / "out" / "comparison.md").read_text(encoding="utf-8")
