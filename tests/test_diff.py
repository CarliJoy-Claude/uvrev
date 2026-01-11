from collections.abc import Mapping

import pytest
from packaging.utils import NormalizedName
from packaging.version import Version

from uvrev.differ import compute_diff, create_changelog


class TestComputeDiff:
    """Tests for compute_diff function."""

    # Package additions
    @pytest.mark.parametrize(
        "old, new, expected_count",
        [
            ({"pandas": ">=1.0"}, {"pandas": ">=1.0", "numpy": ""}, 1),
            ({}, {"pandas": ">=2.0", "numpy": "", "scipy": ">=1.5"}, 3),
            (
                {"pandas": ">=1.0"},
                {"pandas": ">=1.0", "numpy": "", "scipy": ">=1.5"},
                2,
            ),
        ],
    )
    def test_additions(
        self,
        old: Mapping[NormalizedName, str],
        new: Mapping[NormalizedName, str],
        expected_count: int,
    ) -> None:
        """Detect package additions."""
        diff = compute_diff(old, new)
        additions = [line for line in diff if line.strip().startswith("+")]

        assert len(additions) == expected_count

    # Package removals
    @pytest.mark.parametrize(
        "old,new,expected_count",
        [
            ({"pandas": ">=1.0", "numpy": ""}, {"pandas": ">=1.0"}, 1),
            ({"pandas": ">=2.0", "numpy": "", "scipy": ">=1.5"}, {}, 3),
            (
                {"pandas": ">=1.0", "numpy": "", "scipy": ">=1.5"},
                {"pandas": ">=1.0"},
                2,
            ),
        ],
    )
    def test_removals(
        self,
        old: Mapping[NormalizedName, str],
        new: Mapping[NormalizedName, str],
        expected_count: int,
    ) -> None:
        """Detect package removals."""
        diff = compute_diff(old, new)
        removals = [line for line in diff if line.strip().startswith("-")]

        assert len(removals) == expected_count

    # Package updates
    @pytest.mark.parametrize(
        "old,new,expected_count",
        [
            ({"pandas": ">=1.0"}, {"pandas": ">=2.0"}, 1),
            (
                {"pandas": ">=1.0", "numpy": ">=1.20"},
                {"pandas": ">=2.0", "numpy": ">=1.24"},
                2,
            ),
        ],
    )
    def test_updates(
        self,
        old: Mapping[NormalizedName, str],
        new: Mapping[NormalizedName, str],
        expected_count: int,
    ) -> None:
        """Detect package updates."""
        diff = compute_diff(old, new)
        updates = [line for line in diff if "^" in line]

        assert len(updates) == expected_count
        for line in updates:
            assert "→" in line

    def test_no_changes(self) -> None:
        """No changes returns placeholder."""
        old = {NormalizedName("pandas"): ">=1.0", NormalizedName("numpy"): ""}
        new = {NormalizedName("pandas"): ">=1.0", NormalizedName("numpy"): ""}

        diff = compute_diff(old, new)

        assert diff == ()

    def test_empty_dicts(self) -> None:
        """Empty dictionaries return placeholder."""
        diff = compute_diff({}, {})

        assert diff == ()

    def test_output_sorted(self) -> None:
        """Output is sorted alphabetically."""
        old: Mapping[NormalizedName, str] = {}
        new: Mapping[NormalizedName, str] = {
            NormalizedName("scipy"): ">=1.5",
            NormalizedName("numpy"): "",
            NormalizedName("pandas"): ">=2.0",
        }

        diff = compute_diff(old, new)

        packages = [
            line.strip()[1:].split(">=")[0].split("~=")[0].split("==")[0]
            for line in diff
            if line.strip().startswith("+")
        ]

        assert packages == sorted(packages)


class TestCreateChangelog:
    """Tests for create_changelog function."""

    # Both dependencies and packages change
    def test_mixed_changes(self) -> None:
        """Both dependencies and packages change."""
        old_deps: dict[str, None] = {"pandas": None}
        new_deps: dict[str, None] = {"pandas": None, "numpy": None}
        old_pkgs = {"pandas": Version("1.0.0")}
        new_pkgs = {"pandas": Version("2.0.0"), "numpy": Version("1.24.0")}

        changelog = create_changelog(old_deps, new_deps, old_pkgs, new_pkgs)  # type: ignore[arg-type]

        assert "Dependencies:" in changelog
        assert "+numpy" in changelog
        assert "Pinned Versions:" in changelog
        assert "pandas" in changelog
        assert "1.0.0" in changelog
        assert "2.0.0" in changelog

    # Only dependencies change
    def test_only_dependencies_changed(self) -> None:
        """Only dependencies change."""
        old_deps: dict[str, None] = {"pandas": None}
        new_deps: dict[str, None] = {"pandas": None, "numpy": None}
        old_pkgs: dict[str, Version] = {}
        new_pkgs: dict[str, Version] = {}

        changelog = create_changelog(old_deps, new_deps, old_pkgs, new_pkgs)  # type: ignore[arg-type]

        assert "Dependencies:" in changelog
        assert "+numpy" in changelog
        assert "Pinned Versions:" not in changelog

    # Only packages change
    def test_only_packages_changed(self) -> None:
        """Only packages change."""
        old_deps: dict[str, None] = {}
        new_deps: dict[str, None] = {}
        old_pkgs = {"pandas": Version("1.0.0")}
        new_pkgs = {"pandas": Version("2.0.0")}

        changelog = create_changelog(old_deps, new_deps, old_pkgs, new_pkgs)  # type: ignore[arg-type]

        assert "Dependencies:" not in changelog
        assert "Pinned Versions:" in changelog
        assert "1.0.0" in changelog
        assert "2.0.0" in changelog

    # No changes
    def test_no_changes(self) -> None:
        """No changes returns placeholder."""
        old_deps: dict[str, None] = {"pandas": None}
        new_deps: dict[str, None] = {"pandas": None}
        old_pkgs = {"pandas": Version("1.0.0")}
        new_pkgs = {"pandas": Version("1.0.0")}

        changelog = create_changelog(old_deps, new_deps, old_pkgs, new_pkgs)  # type: ignore[arg-type]

        assert changelog == "(no changes)"

    # All empty states
    def test_empty_states(self) -> None:
        """All empty states return placeholder."""
        old_deps: dict[str, None] = {}
        new_deps: dict[str, None] = {}
        old_pkgs: dict[str, Version] = {}
        new_pkgs: dict[str, Version] = {}

        changelog = create_changelog(old_deps, new_deps, old_pkgs, new_pkgs)  # type: ignore[arg-type]

        assert changelog == "(no changes)"
