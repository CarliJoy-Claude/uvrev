# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2] (unreleased)

### Added

- `--json` option for `add`, `remove`, `sync`, `upgrade` and `restore`.
  It prints a JSON report of the changes to stdout, e.g. to decide in automation which services need a restart.
  The report contains the changed packages (`changes`, `all_changed_packages`),
  the changed dependencies of `pyproject.toml` (`dependency_changes`, `all_changed_dependencies`),
  their old and new versions (`versions`), the revision before and after the command and the status.
  On failure the report is still printed with `"status": "error"` and the exit code is `1`.
  See the [README](README.md#json-output) for the full format.
- `uvrev sync --json` reports the changes of the packages installed in the virtual environment.
- Support for Python 3.15.
- `py.typed` marker, so type checkers use the type hints of uvrev ([PEP 561](https://peps.python.org/pep-0561/)).
- Coverage is shown in GitLab merge requests and as badge, together with other badges in the README.

### Changed

- The executed commands, the output of `uv` and `git` and all messages are written to stderr,
  so stdout only contains the actual result (`uvrev run` still writes to stdout).
- `uvrev upgrade` tells if everything is already up to date instead of reporting `created revision None`.
- Development and CI use [prek](https://github.com/j178/prek) instead of pre-commit.
- CI uses uv 0.13.6 and tests Python 3.12 to 3.15.

### Removed

- The Python 3.10 and 3.11 classifiers, which were never supported (`requires-python = ">=3.12"`).

## [0.1.4] (2026-01-13)

### Fixed

- Revision tags are annotated, so they can be signed.

## [0.1.3] (2026-01-13)

First real release.

### Added

- `uvrev env create` and `uvrev env list` to manage environments.
- `uvrev add`, `remove`, `sync`, `upgrade`, `list` and `run` to work with the packages of an environment.
- Every change creates a git commit and a `rev_<n>` tag with a changelog of the dependencies and pinned versions.
- `uvrev history` shows the revisions and `uvrev restore` goes back to a previous one.
- `uvrev path` shows the location of an environment.

[0.2]: https://gitlab.com/Kound/uvrev/-/compare/0.1.4...main
[0.1.4]: https://gitlab.com/Kound/uvrev/-/compare/0.1.3...0.1.4
[0.1.3]: https://gitlab.com/Kound/uvrev/-/tags/0.1.3
