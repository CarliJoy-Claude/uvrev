from pathlib import Path

import pytest

from uvrev.types import NormalizedRequirement, UVRevProject


class TestUVRevProject:
    """Tests for UVRevProject dataclass."""

    # Directory doesn't exist
    def test_not_exists_no_directory(self) -> None:
        """Non-existent directory."""
        project = UVRevProject(Path("/nonexistent/path"))

        assert not project.exists()

    # Directory exists but no pyproject.toml
    def test_not_exists_no_pyproject(self, tmp_path: Path) -> None:
        """Directory exists but no pyproject.toml."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        project = UVRevProject(project_dir)

        assert not project.exists()

    # Both exist and not a package
    def test_exists(self, tmp_path: Path) -> None:
        """Both directory and pyproject.toml exist."""
        project_dir = tmp_path / "testproject"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("[tool.uv]\npackage = false\n")
        proj = UVRevProject(project_dir)

        assert proj.exists() is True

    # Properties return correct paths
    def test_properties(self, tmp_path: Path) -> None:
        """Properties return correct paths."""
        project_dir = tmp_path / "myproject"
        project = UVRevProject(project_dir)

        assert project.pyproject_path == project_dir / "pyproject.toml"
        assert project.lock_path == project_dir / "uv.lock"


class TestNormalizedRequirement:
    """Tests for NormalizedRequirement class."""

    # Names are canonicalized
    @pytest.mark.parametrize(
        "req_string,expected_name",
        [
            ("Scikit-Learn>=1.0", "scikit-learn"),
            ("scikit_learn>=1.0", "scikit-learn"),
            ("scikit-learn>=1.0", "scikit-learn"),
            ("SCIKIT-LEARN>=1.0", "scikit-learn"),
            ("NumPy>=1.20", "numpy"),
            ("numpy>=1.20", "numpy"),
            ("Pillow>=9.0", "pillow"),
        ],
    )
    def test_name_canonicalization(self, req_string: str, expected_name: str) -> None:
        """Names are canonicalized."""
        req = NormalizedRequirement(req_string)

        assert req.name == expected_name

    # Different variants normalize to same value
    def test_variant_equivalence(self) -> None:
        """Different variants normalize to same value."""
        req1 = NormalizedRequirement("Scikit-Learn>=1.0")
        req2 = NormalizedRequirement("scikit_learn>=1.0")
        req3 = NormalizedRequirement("SCIKIT_LEARN>=1.0")

        assert req1.name == req2.name == req3.name

    # Specifiers are preserved
    @pytest.mark.parametrize(
        "req_string,expected_specifier",
        [
            ("pandas>=2.0", ">=2.0"),
            ("numpy~=1.24.0", "~=1.24.0"),
            ("scipy==1.10.1", "==1.10.1"),
            ("requests>=2.0,<3.0", "<3.0,>=2.0"),
            ("flask", ""),
        ],
    )
    def test_specifier_preservation(
        self, req_string: str, expected_specifier: str
    ) -> None:
        """Specifiers are preserved."""
        req = NormalizedRequirement(req_string)

        assert str(req.specifier) == expected_specifier
