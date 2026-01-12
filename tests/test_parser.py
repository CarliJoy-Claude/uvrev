from textwrap import dedent

import pytest
from packaging.utils import NormalizedName, canonicalize_name

from uvrev.parser import parse_pyproject_deps, parse_uv_lock_packages


class TestParsePyprojectDeps:
    """Tests for parse_pyproject_deps function."""

    # Valid dependency specifications
    @pytest.mark.parametrize(
        "content,expected_deps",
        [
            # Basic dependencies
            (
                dedent(
                    """
                    [project]
                    dependencies = ["pandas>=2.0", "numpy"]
                    """
                ),
                {"pandas": ">=2.0", "numpy": None},
            ),
            # Complex specifiers
            (
                dedent(
                    """
                    [project]
                    dependencies = [
                        "pandas>=2.0,<3.0",
                        "numpy~=1.24.0",
                        "scipy==1.10.1"
                    ]
                    """
                ),
                {"pandas": "<3.0,>=2.0", "numpy": "~=1.24.0", "scipy": "==1.10.1"},
            ),
            # Single dependency
            (
                dedent("""
                [project]
                dependencies = ["requests"]
            """),
                {"requests": None},
            ),
        ],
    )
    def test_valid_dependencies(
        self, content: str, expected_deps: dict[NormalizedName, str | None]
    ) -> None:
        """Parse valid dependency specifications."""
        deps = parse_pyproject_deps(content)

        for pkg, spec in expected_deps.items():
            assert pkg in deps
            if spec is None:
                assert deps[pkg] is None
            else:
                assert str(deps[pkg]) == spec

    # Empty or missing dependencies
    @pytest.mark.parametrize(
        "content, expected",
        [
            ("", {}),
            ('[project]\nname = "test"', {}),
            ("[project]\ndependencies = []", {}),
            ("   \n\n  ", {}),
        ],
    )
    def test_empty_or_missing(self, content: str, expected: dict) -> None:
        """Handle missing or empty dependencies."""
        assert parse_pyproject_deps(content) == expected

    # Package names are canonicalized
    def test_name_canonicalization(self) -> None:
        """Package names are canonicalized."""
        content = dedent("""
            [project]
            dependencies = ["Scikit-Learn>=1.0", "NumPy>=1.20"]
        """)

        deps = parse_pyproject_deps(content)

        assert "scikit-learn" in deps
        assert "numpy" in deps


class TestParseUvLockPackages:
    """Tests for parse_uv_lock_packages function."""

    # Valid package entries
    @pytest.mark.parametrize(
        "content, expected_pkgs",
        [
            (
                dedent(
                    """
                    [[package]]
                    name = "pandas"
                    version = "2.0.0"

                    [[package]]
                    name = "numpy"
                    version = "1.24.3"
                     """
                ),
                {"pandas": "2.0.0", "numpy": "1.24.3"},
            ),
            (
                dedent(
                    """
                    [[package]]
                    name = "requests"
                    version = "2.31.0"
                    """
                ),
                {"requests": "2.31.0"},
            ),
        ],
    )
    def test_valid_packages(
        self, content: str, expected_pkgs: dict[NormalizedName, str]
    ) -> None:
        """Parse valid package entries."""
        pkgs = parse_uv_lock_packages(content)

        for name, version in expected_pkgs.items():
            assert name in pkgs
            assert str(pkgs[name]) == version

    # Empty or missing packages
    @pytest.mark.parametrize(
        "content,expected",
        [
            ("", {}),
            ('[metadata]\nversion = "1.0"', {}),
            ("   \n\n  ", {}),
        ],
    )
    def test_empty_or_missing(self, content: str, expected: dict) -> None:
        """Handle missing or empty packages."""
        assert parse_uv_lock_packages(content) == expected

    # Package names are canonicalized
    def test_name_canonicalization(self) -> None:
        """Package names are canonicalized."""
        content = dedent("""
            [[package]]
            name = "NumPy"
            version = "1.24.3"

            [[package]]
            name = "Scikit-Learn"
            version = "1.3.0"
        """)

        pkgs = parse_uv_lock_packages(content)

        assert "numpy" in pkgs
        assert "scikit-learn" in pkgs
        assert str(pkgs[canonicalize_name("numpy")]) == "1.24.3"
        assert str(pkgs[canonicalize_name("scikit-learn")]) == "1.3.0"
