# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.7] (2026-11-13)

### Added

- `uvrev unprotect` is made for hotfixes: it is fast (nothing is checked), can not be interrupted with Ctrl+C
  and marks the environment with `.unprotected`, which contains the time when the `.venv` became writable.
  See the [README](README.md#hotfixes-unprotect-and-protect).
- Repair of an unprotected environment by `uvrev protect` and by every command that changes the environment:
  packages with files changed since `unprotect` (by their ctime) are removed from the uv cache and reinstalled,
  files that no package installed are removed, the bytecode is brought in sync. The `.venv` and the cache are kept.
  - If the time of unprotecting is unknown, every file is compared with its hash instead.
  - If a file of the virtual environment itself was changed, the repair fails and asks for `uvrev sync --clean`.
- `uvrev sync --clean`: deletes the complete uv cache and the `.venv` and installs everything again.
- `uvrev restore --dry-run`: reports what restoring a revision would change, like the other commands.
- JSON reports (`schema_version` stays 2):
  - change reports and `protect`: `repair` (`mode`, `packages`, `removed_files`), `null` if nothing was repaired.
  - `unprotect`: `state` (`post-chmod` or `forced`).
  - `sync`: `options` contains `clean`.
  - `restore`: `options` contains `dry_run`.

### Changed

- `uvrev verify` checks the whole `.venv`, not only `site-packages`. Files that no package installed are
  problems instead of warnings (exit code 1), and `uvrev verify --fix` removes them.
  Bytecode without source is removed, the rest is compiled again.
- `uvrev unprotect` for an environment that is unprotected already does nothing.
- `uvrev protect --json` and `uvrev unprotect --json`: `changed` tells whether the environment was marked as
  unprotected, the permissions are no longer inspected.
- `uvrev run` neither syncs nor protects an environment that is unprotected.
- `uvrev unprotect` warns if there are several environments, as they share the files of their packages.
- `uvrev tool run`: a package specification ending in `.tar.bz2` is a path, like the other archives,
  instead of the name of a package.

### Fixed

- uv could not lock a protected `.venv`, as it opens `.venv/.lock` for writing. It went on without the lock
  (e.g. the check of `uvrev run`), so it could read a `.venv` that another uv was changing.
  The lock file now stays writable for the owner, everything else is read-only as before.
- The help of `uvrev verify` said that files no package installed are only reported.

## [0.6] (2026-11-11)

### Added

- `uvrev upgrade --python REQUEST`: changes the Python and upgrades all (or the given) packages in one revision.
- `--python` of `add` and `upgrade` and the argument of `upgrade-python` accept everything `uv python pin` accepts,
  not only `major.minor`: a version (`3.14`, `3.14.1`), a range (`'>=3.13,<3.15'`), an implementation
  (`cpython-3.14`, `pypy@3.11`) or the path of an interpreter. See the [README](README.md#python-version).
  A relative path refers to the working directory and is pinned as absolute path.
- JSON reports: new field `python_request` with the pinned Python request (the content of `.python-version`).
  - Change reports: `old` and `new` request, `null` if it did not change (like `python`).
  - `env create`: the pinned request.
  - `upgrade`: `options` contains `python`.
  - `schema_version` stays 2.

### Changed

- `add --python`, `upgrade --python` and `upgrade-python` behave the same: they pin the request with
  `uv python pin` first and then run their uv command. `uv add` is no longer called with `--python`.
- The revision message tells what happened to the interpreter, e.g.
  `Upgraded Python: 3.13.9 → 3.14.1 (pinned 3.14)` or `Downgraded Python: ...`, compared by the interpreter versions.
  It is `Pinned Python REQUEST` if only the request changed, and Python is not mentioned if nothing changed
  (before: `Upgraded Python: 3.13 → 3.14` and `; pinned Python 3.14`).
- `uvrev env create --json`: `python` is the exact version of the interpreter (e.g. `3.14.1`) instead of the pin,
  which is now in `python_request`.
- `uvrev env create --python REQUEST` pins the request as given (e.g. a range or the path of an interpreter).
  Before, only the minor version of the interpreter found was pinned.
- `uvrev env create --python` for an existing environment warns if the pinned request differs as text,
  ignoring spaces (before: if the minor version differs).
- `uvrev upgrade-python` no longer checks the request itself (exit code 2), uv does (exit code 1).

## [0.5] (2026-11-10)

### Added

- `--json` for more commands, so a caller can tell whether anything changed.
  See the [README](README.md#reports-of-other-commands).
  - `env create`: reports `created`, `path` and `python` (the pinned version).
  - `protect` / `unprotect`: report `changed`, `protected` and `path`.
  - `verify`: reports `problems`, `remaining_problems` and `fixed`.
- `--dry-run` for `add`, `remove`, `upgrade` and `upgrade-python`: reports what would change
  without touching the environment, its files or its revisions. See the [README](README.md#dry-run).
- `uvrev remove --ignore-missing`: packages that are no dependencies are skipped instead of failing.
  If none is left, the command succeeds without a new revision.
- JSON report of `add`, `remove`, `sync`, `upgrade`, `upgrade-python` and `restore`:
  - `verify`: the result of `--verify` (`problems`, `remaining_problems`, `fixed`), `null` if it did not run.
    If the venv can not be fixed, the report still contains the change that was made.
  - `options` contains `dry_run` (and `ignore_missing` for `remove`).
  - `schema_version` stays 2: fields are only added. It only changes if existing fields change.
- `uvrev env create --python` for an existing environment warns if the environment uses another Python version
  (the option is still not applied).

### Changed

- `uvrev add NAME` (without version) means "any version" and replaces an existing constraint:
  after `uvrev add 'six==1.15.0'`, `uvrev add six` drops the pin (it changed nothing before).
  The requirement is passed to uv as `six>=0`, which uvrev treats and shows like no constraint.
  The locked version is kept until an upgrade. See the [README](README.md#adding-packages-without-a-version).
- The revisions are committed as `uvrev <user@hostname>` instead of the git identity of the user,
  and never signed. See the [README](README.md#git-identity).
- With `--json`, an unknown environment exits with `1` (and a report) instead of `2`.

### Fixed

- `uvrev env create` failed for users without a git identity (`user.name` / `user.email`), e.g. service users,
  and left a half-created folder behind. `uvrev env fix` configures existing environments.
- With `--json`, some failures printed no report: an unknown environment (`--env`), invalid settings and a
  missing `uv` command. Now every failure after parsing the command line prints a report with
  `"status": "error"` and exits with `1`. See the [README](README.md#failures).
- A missing `uv` or `git` command gives a clear message naming the command, instead of
  `Could not convert loaded settings: FileNotFoundError`.
- The environment variable `UVREV_ENV` was documented in the help of `--env`, but not used.
  The environment is taken from `--env`, then `UVREV_ENV`, then the activated uvrev environment, then `base`.
  See the [README](README.md#environment-selection).

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

[0.7]: https://gitlab.com/Kound/uvrev/-/compare/0.6...0.7
[0.6]: https://gitlab.com/Kound/uvrev/-/compare/0.5...0.6
[0.5]: https://gitlab.com/Kound/uvrev/-/compare/0.4...0.5
[0.4]: https://gitlab.com/Kound/uvrev/-/compare/0.3...0.4
[0.3]: https://gitlab.com/Kound/uvrev/-/compare/0.2...0.3
[0.2]: https://gitlab.com/Kound/uvrev/-/compare/0.1.4...0.2
[0.1.4]: https://gitlab.com/Kound/uvrev/-/compare/0.1.3...0.1.4
[0.1.3]: https://gitlab.com/Kound/uvrev/-/tags/0.1.3
