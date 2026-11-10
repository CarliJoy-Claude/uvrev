# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- `uvrev env create` failed for users without a git identity (`user.name` / `user.email`), e.g. service users,
  and left a half-created folder behind. The repository of an environment is now configured with uvrev's own
  identity (`uvrev <user@hostname>`) and without signing of commits and tags.
  `uvrev env fix` applies it to existing environments. See the [README](README.md#git-identity).

## [0.4] (2026-11-10)

### Added

- Protected environments: the `.venv` is read-only (`chmod -R a-w .venv`) and only writable
  (`chmod -R u+w .venv`) while uv changes it, so it can not be changed by accident.
  This includes the environments of `uvrev tool run`. See the [README](README.md#protected-environments).
  - `uvrev protect` / `uvrev unprotect`: change the protection manually.
- `uvrev verify`: checks the `.venv` against the `RECORD` files of the installed packages and against `uv.lock`.
  See the [README](README.md#verify-environments).
  - `--fix`: removes the broken packages from the uv cache and reinstalls them.
    uv hardlinks files from its cache, so a file changed in a `.venv` changes the cache as well
    ([uv#22979](https://github.com/astral-sh/uv/issues/22979)).
  - `--verify` for `add`, `remove`, `sync`, `upgrade`, `upgrade-python` and `restore`:
    runs `uvrev verify --fix` afterwards.
- Settings: `[uvrev]` table in a file in the user config directory (e.g. `~/.config/uvrev/config.toml`),
  each setting can be overridden by an environment variable (e.g. `UVREV_PROTECT=false`).
  See the [README](README.md#settings).
  - `uvrev settings`: shows the settings and the location of the file.
- `uvrev add`: two new options, each in one revision:
  - `--upgrade-all`: adds the packages and upgrades all packages (`uv add --raw --upgrade`, then `uv sync --upgrade`).
  - `--python <version>`: adds the packages and changes the Python version (`uv add --raw --python`, then `uv python pin`).
- `uvrev env create` does nothing for an environment that was created successfully before
  (marked by `.uvrev_created_successfully`). See the [README](README.md#creating-environments).
  - `--force`: deletes an environment folder without this file first.
  - `uvrev env fix`: marks an environment of an older uvrev as created successfully.
- `tool.uv.environments`: packages are only resolved for the current platform and the supported Python versions
  (setting `python_versions_supported`, by default the installed ones).
  `add`, `remove`, `upgrade` and `upgrade-python` update it. See the [README](README.md#supported-environments).
- JSON report: new fields `environments` and `options` (the options of the command), `schema_version` stays 2.

### Changed

- `uvrev upgrade-python` no longer changes `requires-python` (it runs `uv python pin`), so downgrades are possible.
- `uvrev run` syncs the environment only if needed and runs the command with the protected `.venv` (`uv run --no-sync`).
- `add`, `remove` and `upgrade` reset the files to the last revision if uv fails.
- uv compiles bytecode on install (setting `compile_bytecode`), as a protected `.venv` can not cache it later.
- The first revision `rev_0` is an annotated tag like all other revisions.
- `httpx2` is required without a minimum version.
- CI runs the tests as a normal user, so the protection is tested for real.
- `AGENTS.md` describes the conventions for contributors and AI agents.

### Fixed

- Signing a release (PEP 740 attestations) failed because of wrong `uv run` options.

## [0.3] (2026-11-01)

### Added

- `uvrev tool run` runs a tool like `uvx`, but installs its dependencies exactly as pinned in the `uv.lock`
  embedded in the wheel by `hatch-uv-lock`. See the [README](README.md#running-tools-with-locked-dependencies).
  The wheel comes from the package index or from a local path or URL.
  A `#sha256=<hex>` fragment in the URL is verified, downloads never go from `https` to `http`.
- `uvrev upgrade-python <major.minor>` changes the Python version of an environment (e.g. `3.12` to `3.14`).
  It pins the version in `.python-version`, sets `requires-python`, recreates the virtual environment and updates `uv.lock`.
  If uv fails (e.g. the version is not available), the files are reset to the last revision.
  See the [README](README.md#python-version).
- The exact version of the Python interpreter is recorded in `.python-version.lock` and shown in the changelog of a revision (`Python:` section).
  A changed interpreter (e.g. a patch update of a uv managed Python) is recorded with the next revision.
  Existing environments get the file with their next revision.
- `python` field in the JSON report with the old and new interpreter version, if it changed.
- `downgraded` list in `changes` and `dependency_changes` of the JSON report.
- Releases are signed with Sigstore and uploaded with [PEP 740](https://peps.python.org/pep-0740/) attestations,
  so PyPI shows their provenance.
  The signing tool is locked in the new `attestations` dependency group.

### Changed

- **Breaking:** JSON report `schema_version` 2: `updated` contains only upgrades,
  downgraded packages are listed in `downgraded` instead.
- `uvrev restore` restores the pinned Python version (`.python-version`) as well.
  The virtual environment is recreated before the used interpreter is recorded.
  If uv fails (e.g. the Python version of that revision is not available), the files are reset to the last revision.
- `UV_PYTHON` is no longer passed to uv, so an environment always uses the version pinned in `.python-version`
  instead of being recreated with another interpreter.
- `uvrev env create` without `--python` lets uv choose the interpreter instead of using `UV_PYTHON` or `3.12`.
- CI fails if `uv.lock` is not up to date with `pyproject.toml` (`UV_LOCKED` instead of `UV_FROZEN`).
- CI tests the built package instead of an editable install of the source tree (`UV_NO_EDITABLE`).
  Coverage measures the package `uvrev` and maps installed files back to `src/uvrev`, doctests run with `--pyargs uvrev`.
- More ruff rules are enabled, among them all security related ones (`S`, `ASYNC`, `BLE`, `DTZ`, `G`, `LOG`, `PGH`, `T10`, `YTT`) and `RUF`.

### Fixed

- Downgraded packages were shown as upgrade (`^`) in the changelog. They are now marked with `v`.

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

[Unreleased]: https://gitlab.com/Kound/uvrev/-/compare/0.4...main
[0.4]: https://gitlab.com/Kound/uvrev/-/compare/0.3...0.4
[0.3]: https://gitlab.com/Kound/uvrev/-/compare/0.2...0.3
[0.2]: https://gitlab.com/Kound/uvrev/-/compare/0.1.4...0.2
[0.1.4]: https://gitlab.com/Kound/uvrev/-/compare/0.1.3...0.1.4
[0.1.3]: https://gitlab.com/Kound/uvrev/-/tags/0.1.3
