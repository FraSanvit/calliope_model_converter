"""Convert the real legacy test model in resources/user/ (skipped when absent: it is not in git).

Only `ERS_test_example` is converted; `2030_ERS_eurospores` is too big to run in the test suite.
"""

from pathlib import Path

import pytest
from converter.convert import convert_model

RESOURCES = Path(__file__).parent.parent / "resources" / "user"
MODELS = ["ERS_test_example"]


@pytest.mark.slow
@pytest.mark.parametrize("model", MODELS)
def test_real_model_converts(model, tmp_path):
    """The real legacy model converts and every run.py constraint is reported."""
    if not (RESOURCES / model / "model").is_dir():
        pytest.skip(f"{model} not found in resources/user")
    result = convert_model(RESOURCES / model, tmp_path / "model", copy_csvs=False)
    assert result.output_entry.exists()
    names = {c["name"] for c in result.report["custom_constraints"]}
    assert "storage_ev_constraint" in names
    assert result.report["stats"]["techs"] > 0
