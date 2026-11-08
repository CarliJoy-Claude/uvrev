from pathlib import Path

from packaging.utils import canonicalize_name

from uvrev.operations import drop_dependencies, list_projects
from uvrev.types import UVRevProject


class TestListProjects:
    """Tests for list_projects function."""

    # Empty directory
    def test_empty_directory(self, tmp_path: Path) -> None:
        """Empty directory returns empty list."""
        projects = list_projects(tmp_path)

        assert projects == ()

    # Non-existent directory
    def test_nonexistent_directory(self) -> None:
        """Non-existent directory returns empty list."""
        projects = list_projects(Path("/nonexistent"))

        assert projects == ()

    # Multiple valid projects
    def test_with_valid_projects(self, tmp_path: Path) -> None:
        """Lists all valid projects."""
        for name in ["proj1", "proj2", "proj3"]:
            proj_dir = tmp_path / name
            proj_dir.mkdir()
            (proj_dir / "pyproject.toml").write_text("[project]\nname = 'test'")

        projects = list_projects(tmp_path)

        assert len(projects) == 3
        assert set(projects) == {"proj1", "proj2", "proj3"}

    # Excludes directories without pyproject.toml
    def test_excludes_invalid(self, tmp_path: Path) -> None:
        """Excludes directories without pyproject.toml."""
        proj1 = tmp_path / "proj1"
        proj1.mkdir()
        (proj1 / "pyproject.toml").write_text("[project]\nname = 'test'")

        proj2 = tmp_path / "proj2"
        proj2.mkdir()

        projects = list_projects(tmp_path)

        assert len(projects) == 1
        assert "proj1" in projects
        assert "proj2" not in projects

    # Excludes regular files
    def test_excludes_files(self, tmp_path: Path) -> None:
        """Excludes regular files."""
        (tmp_path / "not_a_project.txt").write_text("test")

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text("[project]\nname = 'test'")

        projects = list_projects(tmp_path)

        assert len(projects) == 1
        assert "proj" in projects


class TestDropDependencies:
    def test_no_dependencies(self, tmp_path: Path) -> None:
        """pyproject.toml without dependencies stays untouched"""
        content = "[project]\nname = 'test'\n"
        (tmp_path / "pyproject.toml").write_text(content)

        drop_dependencies(UVRevProject(tmp_path), frozenset({canonicalize_name("a")}))

        assert (tmp_path / "pyproject.toml").read_text() == content

    def test_keeps_format(self, tmp_path: Path) -> None:
        """Only the given packages are removed, comments are kept"""
        (tmp_path / "pyproject.toml").write_text(
            "# comment\n[project]\ndependencies = [\n"
            '    "A>=1",\n    "b",\n    "c[x]; python_version>\'3\'",\n]\n'
        )

        drop_dependencies(
            UVRevProject(tmp_path), frozenset(map(canonicalize_name, ("a", "c")))
        )

        content = (tmp_path / "pyproject.toml").read_text()
        assert content.startswith("# comment\n")
        assert '"b"' in content
        assert "A>=1" not in content
        assert "c[x]" not in content
