import pytest

from core.auth import relogin, vault
from core.config import settings
from core.runtime import executor


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Give every test its own database, vault and output folders."""
    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "generated_tools_dir", tmp_path / "tools")
    monkeypatch.setattr(settings, "generated_specs_dir", tmp_path / "specs")
    # Tests that want background login renewal turn it on themselves
    monkeypatch.setattr(settings, "auto_relogin", False)
    monkeypatch.setattr(vault, "_vault", None)
    executor.clear_auth_cache()
    relogin.reset()
    yield
    executor._transport = None
    executor._renew_login = None
    executor.clear_auth_cache()
    relogin.reset()
