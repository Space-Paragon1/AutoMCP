import pytest

from core.config import settings
from core.runtime import executor


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Give every test its own database and output folders."""
    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "generated_tools_dir", tmp_path / "tools")
    monkeypatch.setattr(settings, "generated_specs_dir", tmp_path / "specs")
    executor.clear_auth_cache()
    yield
    executor._transport = None
    executor.clear_auth_cache()
