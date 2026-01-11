from pathlib import Path

import pytest

from uvrev.types import UVRevProject


@pytest.fixture
def fake_basedir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base_dir = tmp_path / "fake_basedir"
    monkeypatch.setenv("UVREV_BASE", str(base_dir))
    return base_dir


@pytest.fixture
def tmp_project(tmp_path: Path) -> UVRevProject:
    """Create a temporary project with pyproject.toml."""
    project_dir = tmp_path / "testproject"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text("[project]\nname = 'test'")
    return UVRevProject(project_dir)


@pytest.fixture(autouse=True)
def unset_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
