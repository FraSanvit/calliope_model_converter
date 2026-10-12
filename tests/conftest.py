"""Shared pytest fixtures."""

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "workflow" / "scripts"))

FIXTURES = Path(__file__).parent / "fixtures"
MINI = FIXTURES / "mini_legacy"


@pytest.fixture(scope="module")
def module_path():
    """Parent directory of the project."""
    return ROOT


@pytest.fixture(scope="session")
def mini_legacy() -> Path:
    """The small legacy (v0.6) model used across tests."""
    return MINI


@pytest.fixture
def legacy_copy(tmp_path, mini_legacy) -> Path:
    """A writable copy of the mini legacy model, for tests that break it on purpose."""
    dest = tmp_path / "legacy"
    shutil.copytree(mini_legacy, dest)
    return dest


@pytest.fixture(scope="session")
def converted_mini(tmp_path_factory, mini_legacy):
    """The mini legacy model converted once per test session."""
    from converter.convert import convert_model

    out = tmp_path_factory.mktemp("converted")
    result = convert_model(mini_legacy, out / "model", report_md=out / "report.md", report_json=out / "report.json")
    return result, out / "model"


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "calliope: needs Calliope v0.7 (run in the `calliope` pixi environment)")
    config.addinivalue_line("markers", "slow: converts the real legacy models in resources/user/")
