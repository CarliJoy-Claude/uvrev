from collections.abc import Mapping

import pytest
from packaging.specifiers import SpecifierSet
from packaging.utils import NormalizedName
from packaging.version import Version

from uvrev.differ import Changes, Diff, VersionChange, create_changelog


class TestDiffLines:
    """Tests for Diff.lines."""

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
        diff = Diff(old, new).lines()
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
        diff = Diff(old, new).lines()
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
        diff = Diff(old, new).lines()
        updates = [line for line in diff if "^" in line]

        assert len(updates) == expected_count
        for line in updates:
            assert "→" in line

    def test_no_changes(self) -> None:
        """No changes returns placeholder."""
        old = {NormalizedName("pandas"): ">=1.0", NormalizedName("numpy"): ""}
        new = {NormalizedName("pandas"): ">=1.0", NormalizedName("numpy"): ""}

        diff = Diff(old, new).lines()

        assert diff == ()

    def test_empty_dicts(self) -> None:
        """Empty dictionaries return placeholder."""
        diff = Diff({}, {}).lines()

        assert diff == ()

    def test_output_sorted(self) -> None:
        """Output is sorted alphabetically."""
        old: Mapping[NormalizedName, str] = {}
        new: Mapping[NormalizedName, str] = {
            NormalizedName("scipy"): ">=1.5",
            NormalizedName("numpy"): "",
            NormalizedName("pandas"): ">=2.0",
        }

        diff = Diff(old, new).lines()

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

        changelog = create_changelog(Diff(old_deps, new_deps), Diff(old_pkgs, new_pkgs))  # type: ignore[arg-type]

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

        changelog = create_changelog(Diff(old_deps, new_deps), Diff(old_pkgs, new_pkgs))  # type: ignore[arg-type]

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

        changelog = create_changelog(Diff(old_deps, new_deps), Diff(old_pkgs, new_pkgs))  # type: ignore[arg-type]

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

        changelog = create_changelog(Diff(old_deps, new_deps), Diff(old_pkgs, new_pkgs))  # type: ignore[arg-type]

        assert changelog == "(no changes)"

    # All empty states
    def test_empty_states(self) -> None:
        """All empty states return placeholder."""
        old_deps: dict[str, None] = {}
        new_deps: dict[str, None] = {}
        old_pkgs: dict[str, Version] = {}
        new_pkgs: dict[str, Version] = {}

        changelog = create_changelog(Diff(old_deps, new_deps), Diff(old_pkgs, new_pkgs))  # type: ignore[arg-type]

        assert changelog == "(no changes)"


class TestDiff:
    """Tests for the structured Diff."""

    def test_kinds(self) -> None:
        old = {"pandas": Version("1.0"), "numpy": Version("1.0"), "six": Version("1")}
        new = {"pandas": Version("2.0"), "scipy": Version("1.5"), "six": Version("1")}

        diff = Diff(old, new)  # type: ignore[arg-type]

        assert diff.changes() == Changes(
            added=(NormalizedName("scipy"),),
            updated=(NormalizedName("pandas"),),
            removed=(NormalizedName("numpy"),),
        )
        assert diff.all_changed == ("numpy", "pandas", "scipy")
        assert diff.versions() == {
            "numpy": VersionChange(old="1.0", new=None),
            "pandas": VersionChange(old="1.0", new="2.0"),
            "scipy": VersionChange(old=None, new="1.5"),
        }

    def test_downgrade_is_update(self) -> None:
        diff = Diff({"pandas": Version("2.0")}, {"pandas": Version("1.0")})  # type: ignore[dict-item]

        assert diff.updated == ("pandas",)

    def test_specifier_none_is_empty_string(self) -> None:
        """An unconstrained dependency is "", an absent one None"""
        diff = Diff({"numpy": SpecifierSet(">=1")}, {"numpy": None, "pandas": None})  # type: ignore[dict-item]

        assert diff.versions() == {
            "numpy": VersionChange(old=">=1", new=""),
            "pandas": VersionChange(old=None, new=""),
        }

    def test_empty(self) -> None:
        diff: Diff[Version] = Diff()

        assert not diff
        assert diff.changes() == Changes(added=(), updated=(), removed=())
        assert diff.all_changed == ()
        assert diff.versions() == {}

    def test_immutable(self) -> None:
        """Changing the given states does not change the diff"""
        old = {NormalizedName("pandas"): Version("1.0")}
        diff = Diff(old, {})

        old[NormalizedName("numpy")] = Version("1.0")

        assert diff.removed == ("pandas",)
