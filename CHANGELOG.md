# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `uvrev tool run` runs a tool like `uvx`, but installs its dependencies exactly as pinned in the `uv.lock`
  embedded in the wheel by `hatch-uv-lock`. See the [README](README.md#running-tools-with-locked-dependencies).
  The wheel comes from the package index or from a local path or URL.
  A `#sha256=<hex>` fragment in the URL is verified, downloads never go from `https` to `http`.
- Releases are signed with Sigstore and uploaded with [PEP 740](https://peps.python.org/pep-0740/) attestations,
  so PyPI shows their provenance.
  The signing tool is locked in the new `attestations` dependency group.

### Changed

- CI fails if `uv.lock` is not up to date with `pyproject.toml` (`UV_LOCKED` instead of `UV_FROZEN`).
- More ruff rules are enabled, among them all security related ones (`S`, `ASYNC`, `BLE`, `DTZ`, `G`, `LOG`, `PGH`, `T10`, `YTT`) and `RUF`.
- CI tests the built package instead of an editable install of the source tree (`UV_NO_EDITABLE`).
  Coverage measures the package `uvrev` and maps installed files back to `src/uvrev`, doctests run with `--pyargs uvrev`.

## [0.2] (2026-10-29)

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
- Test results are reported to GitLab as JUnit reports, so failing tests are listed in merge requests.

### Changed

- The executed commands, the output of `uv` and `git` and all messages are written to stderr,
  so stdout only contains the actual result (`uvrev run` still writes to stdout).
- `uvrev upgrade` tells if everything is already up to date instead of reporting `created revision None`.
- Development and CI use [prek](https://github.com/j178/prek) instead of pre-commit.
- mypy runs in strict mode, and diffs, revision changes and reports are immutable.
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
