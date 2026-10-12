"""Optional fixed-design check (converter.fixed_design) on the synthetic comparison case of test_compare."""

import pytest
import xarray as xr
from converter.fixed_design import check
from ruamel.yaml import YAML
from test_compare import _write_case


def _write_fixed(folder, results: xr.Dataset, *, cost_factor: float | None = 1.0) -> None:
    """`v07_fixed.nc`: the fixed-design run (cost_factor None: infeasible, no results)."""
    out = xr.Dataset() if cost_factor is None else results.assign(cost=results.cost * cost_factor)
    out.to_netcdf(folder / "v07_fixed.nc", group="results", mode="w")


def test_fix_override_written(tmp_path):
    _write_case(tmp_path / "case")
    text = check(tmp_path / "case", tmp_path / "out")
    fix = YAML(typ="safe", pure=True).load((tmp_path / "out" / "fix_v06_design.yaml").read_text(encoding="utf-8"))
    gas = fix["nodes"]["A"]["techs"]["gas_plant"]
    assert gas["flow_cap_min"] == pytest.approx(10.0, rel=1e-5)  # single-carrier tech: a plain value
    assert gas["flow_cap_max"] >= 10.0
    assert fix["techs"]["A_to_B_ac_line"]["flow_cap_max"] == pytest.approx(5.0, rel=1e-5)  # once per link
    assert fix["nodes"]["A"]["techs"]["battery"]["storage_cap_min"] == pytest.approx(4.0, rel=1e-5)
    assert text.startswith("Not run yet")
    assert (tmp_path / "out" / "fixed_design.md").exists()


@pytest.mark.parametrize(
    ("cost_factor", "verdict"),
    [(1.0, "**EQUIVALENT**"), (1.1, "**COSTLIER**"), (0.9, "**CHEAPER**"), (None, "**INFEASIBLE**")],
)
def test_fixed_design_verdict(tmp_path, cost_factor, verdict):
    results = _write_case(tmp_path / "case")
    _write_fixed(tmp_path / "case", results, cost_factor=cost_factor)
    text = check(tmp_path / "case", tmp_path / "out")
    assert text.startswith(verdict)
    report = (tmp_path / "out" / "fixed_design.md").read_text(encoding="utf-8")
    assert ("Costs per tech" in report) == (cost_factor is not None)
