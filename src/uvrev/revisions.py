"""The revisions of an environment: a git commit and a ``rev_<n>`` tag each.

Compares the state of the tracked files (``pyproject.toml``, ``uv.lock``,
``.python-version``, ``.python-version.lock``) with the last revision, creates
revisions, reads the files of older ones and shows the history.

What a command changes in the files is decided in ``operations.py``.
"""

import contextlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import click
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from . import constants
from .commands import git
from .differ import Diff, create_changelog
from .exceptions import RevisionNotFoundError, SubprocessFailure
from .interpreter import preview_python, update_python_lock
from .parser import (
    parse_pyproject_deps,
    parse_python_lock,
    parse_python_request,
    parse_uv_environments,
    parse_uv_lock_packages,
    same_except_any_version,
)
from .types import Revision, UVRevProject


@dataclass(frozen=True)
class RevisionChange:
    """Outcome of a command that may change an environment"""

    previous_revision: Revision
    revision: Revision
    packages: Diff[Version] = field(default_factory=Diff)
    dependencies: Diff[SpecifierSet | None] = field(default_factory=Diff)
    # exact version of the interpreter
    python: Diff[Version] = field(default_factory=Diff)
    environments: Diff[None] = field(default_factory=Diff)
    # pinned Python request (.python-version)
    python_request: Diff[str] = field(default_factory=Diff)

    @property
    def created(self) -> bool:
        """True if a new revision was created"""
        return self.revision != self.previous_revision


def committed_files(
    project: UVRevProject, ref: str, paths: tuple[Path, ...]
) -> tuple[str, ...]:
    """Names of the given files (in the project root) that exist in the git ref."""
    names = tuple(path.name for path in paths)
    output = git(
        "ls-tree",
        "--name-only",
        ref,
        "--",
        *names,
        project=project,
        capture_output=True,
    )
    existing = set(output.splitlines())
    return tuple(name for name in names if name in existing)


def get_last_revision_number(project: UVRevProject) -> Revision:
    """Get the number of the latest revision.

    Uses git tag -l to list all numeric tags and returns the highest.

    Args:
        project: Project to get revision number for.

    Returns:
        Latest revision number (0 if no revisions exist).
    """
    output = git(
        "tag",
        "--list",
        "rev_[0-9]*",
        project=project,
        capture_output=True,
    )

    last_rev = 0

    for rev in output.splitlines(keepends=False):
        with contextlib.suppress(ValueError):
            last_rev = max(last_rev, int(rev.removeprefix("rev_")))

    return Revision(last_rev)


def no_change(project: UVRevProject) -> RevisionChange:
    """The outcome of a command that changed nothing: no new revision"""
    revision = get_last_revision_number(project)
    return RevisionChange(revision, revision)


def committed_state(project: UVRevProject) -> Mapping[str, str]:
    """Content of the tracked files in the last revision, by file name.

    Files missing in the revision are empty.
    """
    paths = project.tracked_paths
    committed = committed_files(project, "HEAD", paths)
    return {
        path.name: git(
            "show", f"HEAD:{path.name}", project=project, capture_output=True
        )
        if path.name in committed
        else ""
        for path in paths
    }


def current_state(project: UVRevProject) -> Mapping[str, str]:
    """Current content of the tracked files, by file name (empty if missing)"""
    return {
        path.name: path.read_text() if path.exists() else ""
        for path in project.tracked_paths
    }


def is_same_state(old: Mapping[str, str], new: Mapping[str, str]) -> bool:
    """True if two states of the tracked files describe the same environment.

    This is also the case if only ``name`` was rewritten to ``name>=0``
    (or back), which is the same requirement.
    """
    if old == new:
        return True
    others = (constants.PYTHON_VERSION_FILE, constants.PYTHON_VERSION_LOCK_FILE)
    return all(old[name] == new[name] for name in others) and same_except_any_version(
        old[constants.PYPROJECT_FILE],
        new[constants.PYPROJECT_FILE],
        old[constants.LOCK_FILE],
        new[constants.LOCK_FILE],
    )


def state_change(
    old: Mapping[str, str],
    new: Mapping[str, str],
    previous_revision: Revision,
    revision: Revision,
) -> RevisionChange:
    """Compare two states of the tracked files"""
    return RevisionChange(
        previous_revision,
        revision,
        packages=Diff(
            parse_uv_lock_packages(old[constants.LOCK_FILE]),
            parse_uv_lock_packages(new[constants.LOCK_FILE]),
        ),
        dependencies=Diff(
            parse_pyproject_deps(old[constants.PYPROJECT_FILE]),
            parse_pyproject_deps(new[constants.PYPROJECT_FILE]),
        ),
        python=Diff(
            parse_python_lock(old[constants.PYTHON_VERSION_LOCK_FILE]),
            parse_python_lock(new[constants.PYTHON_VERSION_LOCK_FILE]),
        ),
        environments=Diff(
            parse_uv_environments(old[constants.PYPROJECT_FILE]),
            parse_uv_environments(new[constants.PYPROJECT_FILE]),
        ),
        python_request=Diff(
            parse_python_request(old[constants.PYTHON_VERSION_FILE]),
            parse_python_request(new[constants.PYTHON_VERSION_FILE]),
        ),
    )


def changelog_of(change: RevisionChange) -> str:
    """Changelog of a change, as written in the commit of a revision"""
    return create_changelog(
        change.dependencies, change.packages, change.python, change.environments
    )


def create_revision(
    project: UVRevProject, message: str = "Update packages"
) -> RevisionChange:
    """Create a new revision (commit + tag).

    Args:
        project: Project to create revision in.
        message: Commit message.

    Returns:
        Changes compared to the previous revision.
        If nothing changed, no revision is created.
    """
    last_rev = get_last_revision_number(project)
    update_python_lock(project)

    old = committed_state(project)
    current = current_state(project)
    if old == current:
        return RevisionChange(last_rev, last_rev)
    if is_same_state(old, current):
        # only "name" was rewritten to "name>=0" (or back): keep the committed
        # files instead of an empty revision
        committed = (name for name, content in old.items() if content)
        git("checkout", "HEAD", "--", *committed, project=project)
        return RevisionChange(last_rev, last_rev)

    rev_num = Revision(last_rev + 1)
    change = state_change(old, current, last_rev, rev_num)

    # Commit changes
    git(
        "add",
        "--",
        *(path.name for path in project.tracked_paths if path.exists()),
        project=project,
    )

    commit_msg = f"(rev {rev_num}) {message}\n{changelog_of(change)}"
    git("commit", "-m", commit_msg, project=project)

    # Create annotated tag (can be signed)
    git("tag", "-m", commit_msg, "--annotate", f"rev_{rev_num}", project=project)

    return change


def preview_change(project: UVRevProject, scratch: UVRevProject) -> RevisionChange:
    """What a command would change, from the scratch copy it was applied to.

    No revision is created, so ``revision`` equals ``previous_revision``.
    """
    last_rev = get_last_revision_number(project)
    old = committed_state(project)
    new = {
        **current_state(scratch),
        constants.PYTHON_VERSION_LOCK_FILE: preview_python(project, scratch),
    }
    if is_same_state(old, new):
        return RevisionChange(last_rev, last_rev)
    return state_change(old, new, last_rev, last_rev)


@contextlib.contextmanager
def reset_on_failure(project: UVRevProject) -> Iterator[None]:
    """Reset the files to the last revision, if a command fails.

    Used around commands that change the files, so a failure (e.g. the Python
    version is not available) leaves no state behind that is not part of a
    revision.
    """
    try:
        yield
    except click.ClickException:
        # a failed command, or a missing one (uv, chmod)
        reset = committed_files(project, "HEAD", project.source_paths)
        git("checkout", "HEAD", "--", *reset, project=project)
        raise


def restore_files(
    project: UVRevProject, target: UVRevProject, revision: Revision
) -> None:
    """Write the files of a revision of the project into the target.

    The exact Python version is not restored, it is recorded again from the
    recreated virtual environment.

    Args:
        project: Project with the revisions.
        target: The project itself, or its scratch copy in a dry run.
        revision: Revision to take the files from.

    Raises:
        RevisionNotFoundError: If the revision does not exist.
    """
    tag_name = f"rev_{revision}"
    try:
        git("rev-parse", tag_name, project=project, capture_output=True)
    except SubprocessFailure:
        raise RevisionNotFoundError(f"Revision {revision} does not exist") from None
    for name in committed_files(project, tag_name, project.source_paths):
        content = git(
            "show", f"{tag_name}:{name}", project=project, capture_output=True
        )
        target.project_path.joinpath(name).write_text(content)


def show_history(project: UVRevProject) -> str:
    """Get revision history.

    Args:
        project: Project to get history for.

    Returns:
        Formatted git log output.
    """
    return git(
        "log",
        "--tags",
        "--color=always",
        "--format=%ai  %C(bold)%C(green)%s%C(reset)%n%b",
        "--reverse",
        project=project,
        capture_output=True,
    )
