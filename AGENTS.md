# AGENTS.md

Guidance for AI agents (and humans) working on uvrev.

## Project

uvrev wraps `uv` and adds conda-style revision tracking: every change of an
environment is a git commit plus an annotated `rev_<n>` tag in the environment's
project folder. Environments are uv projects with `[tool.uv] package = false`.
`uvrev tool run` runs tools like `uvx`, with the dependencies locked in their release.

- Source: `src/uvrev/`, tests: `tests/`, test data: `tests/data/`
- Upstream: GitLab `Kound/uvrev` (main branch, CI, releases to PyPI on tags).
  GitHub is only a mirror.

## Workflow

- Before changing anything, run `prek run --all-files` to make sure the
  current state is green.
- Validate before every commit with `prek run --all-files`
  (ruff format, ruff check, mypy strict, doctests, tests, 100% coverage).
- Run it like CI as well: uv version from `.gitlab-ci.yml`, `UV_LOCKED=true`,
  on the lowest and highest Python of the matrix (e.g. `UV_PYTHON=3.12` and `3.15`),
  and once as a normal user with a system Python (`UV_PYTHON_PREFERENCE=system`,
  `UV_PYTHON_DOWNLOADS=false`): both found bugs that the default run as root hid.
- Keep the history clear: one commit per logical step, gitmoji style
  (✨ feature, 🐛 fix, ♻️ refactoring, 📝 docs, 👷 CI, ⬆️ dependencies, 🏷️ typing).
- Update `CHANGELOG.md` (Keep a Changelog): new entries go to `[Unreleased]`.
  Only if the maintainer says so, the section of a prepared but not yet
  released version is extended instead.
- Propose names, schemas and user-visible interfaces first and wait for
  approval before implementing them.
- Put the requirements and what you learned in a notes file before bigger
  reworks, so you don't have to rediscover them.
- Before rebasing onto another branch, look for changes that conflict in
  logic (not only in text), e.g. new uv calls that change the `.venv`.

## Code style

- mypy runs in strict mode: everything is typed, no untyped decorators.
- Many ruff rules are enabled, the security related ones included. Do not
  silence a rule globally; use a targeted `# noqa: <rule>` with a reason
  (it must be on the line ruff reports).
- Prefer immutables wherever possible: `@dataclass(frozen=True)`, tuples
  instead of lists, `Mapping` (or `MappingProxyType`) instead of `dict` in
  signatures and stored state. Mutate only local, short-lived objects.
- Match the surrounding code: docstrings with Args/Returns/Examples, doctests
  for small pure functions.
- Every module starts with a docstring that defines its scope: what belongs
  in it and where related things are instead. A module that outgrows its
  docstring is split.
- stdout is reserved for results (e.g. the `--json` report). Logs, executed
  commands and the output of `uv`, `git` and `chmod` go to stderr.
  Only `uvrev run` and the tools of `uvrev tool run` write to stdout.
- JSON reports (`--json`): all commands are `ReportingCommand`s (`reporting.py`), which
  print a report for every failure. A command with `--json` gives it an
  `error_report` builder and the `report_options` shown as `options`.
  Never raise for a failure after printing a success report.
  `schema_version` only changes if existing fields change; new fields and new
  keys in `options` are added without a new version.
- Errors found while parsing options (unknown `--env`, invalid settings) are
  stored in `ctx.meta` and raised when the command is invoked, so they are
  reported as well. Usage errors (exit code 2) print no report.
- A command that changes the files of an environment is planned as data:
  a pure `plan_*()` function in `operations.py` returns a `Change` (the uv
  calls, the message of the revision), `apply_change()` runs it and returns
  an `Outcome`. This also gives the command `--dry-run`: the change runs in a
  scratch copy, translated by `dry_run_call()` (`--no-sync`, `uv lock`).
  The cli shows the `Outcome` with `report_outcome()`.
  `sync` does not change the files, so `sync_packages()` is its own function.
- All constants live in `constants.py`, also if only one module uses them.
  A value that depends on the environment at the time of the call is a
  function there (`config_file()`: the tests change `XDG_CONFIG_HOME`).
- `constants.py` and `exceptions.py` import nothing of uvrev and must stay
  that way (`tests/test_layers.py`). An exception gets what it needs as an
  argument, or imports late within a method (`terminal` in `UvrevError.show`).
  `types.py` only imports `constants` (`Settings` only for type checkers).
  The same test checks this.
- `UVRevProject.settings` has no default: a project always gets the loaded
  settings (`get_settings(ctx)`, `named_project()` in `options.py`), so no
  caller has to know whether a function uses them. Only the report of a
  failure may fall back to the defaults (`get_settings_or_defaults()`), as
  the failure can be that the settings are invalid.
- Import cycle to keep in mind: `report` imports `revisions`, `operations`
  imports both. So `revisions` must not import `report`, and `report` must
  not import `operations`.
- The modules from bottom to top: `constants`, `exceptions`, `types`,
  `settings`, `terminal`, `commands`, then the domain (`parser`, `differ`,
  `io`, `marker`, `protection`, `verify`, `repair`, `interpreter`, `revisions`,
  `environment`, `operations`, `tool`), then `report`, `reporting`,
  `options` and `cli`.

## Testing

- **Never use `monkeypatch.setattr`** (or `unittest.mock.patch`). Patching
  globals has hidden side effects. Make dependencies explicit instead:
  - small functions take the dependency as a parameter, defaulting to the
    real implementation (e.g. `uv: UvRunner = uv_tool`),
  - bigger flows take a frozen service object: `Services` in `reporting.py`
    (settings, verify, tool services) and `ToolServices` in `tool.py`
    (uv runner, download, build, lock). Tests pass their own, e.g.
    `CliRunner().invoke(cli, args, obj=Services(...))`,
  - logic that only computes something is a pure function, tested directly
    (e.g. `uv_environ()` instead of intercepting `subprocess.run`),
  - prefer settings or other explicit state over faking (e.g.
    `Settings(chmod_command="missing")` to test a missing `chmod`).
  `monkeypatch.setenv` / `delenv` for environment variables is fine.
- Tests must never read the settings of the user running them:
  `isolate_settings` in `tests/conftest.py` sets `XDG_CONFIG_HOME`
  (only effective on Linux, a guard fails the tests elsewhere).
- CI runs the tests as a normal user (`runuser` in `.gitlab-ci.yml`), as root
  ignores file permissions. Tests that change a protected venv must make it
  writable first. Tests that check denied writes are skipped as root.
  To run the tests like CI locally, use a normal user as well.
- Tests use `UV_LINK_MODE=copy` (`tests/conftest.py`), so they never change
  the real uv cache. A test that needs uv's default hardlinks must use a
  private cache (`UV_CACHE_DIR` in `tmp_path`).
- The tests run under `uv run`, which holds a shared lock on the uv cache:
  a uv command that needs an exclusive lock (e.g. `uv cache clean` without
  `--force`) waits forever. Run long test commands with a timeout.
- Integration tests use the uv cache and PyPI (`tests/data/sample`,
  `tests/data/upgradeable`). The `uv.lock` of `tests/data/sample` does not
  match its `pyproject.toml`: run `uv lock` before using `--locked`.
- The `uv.lock` of `tests/data/upgradeable` requires Python 3.14: tests must not
  install it with `uv sync --frozen`, as CI also runs older Python versions.
- `uvrev sync --clean` deletes the complete uv cache: tests of it must set a
  private `UV_CACHE_DIR`.
- To test a missing command, restrict `PATH` to a folder with symlinks to the
  others (`only_commands` in `tests/test_errors.py`).
- There is no `tests/__init__.py`: import helpers with `from conftest import ...`.

## Domain knowledge

- **Disk space is scarce on the target systems: never copy packages** from the
  uv cache (do not set `UV_LINK_MODE` / `link-mode`), uv hardlinks them.
- **Hardlinks share the file with the uv cache and all other venvs**: a file
  changed in a `.venv` changes the cache as well
  ([uv#22979](https://github.com/astral-sh/uv/issues/22979)). Reinstalling alone
  links the changed file again: remove the package from the cache first
  (`uv cache clean --force <pkg>`), then `uv sync --reinstall-package <pkg>`.
- Protection: `chmod -R a-w .venv` / `chmod -R u+w .venv` (modes and command
  are settings). GNU `chmod -R` does not follow symlinks found while traversing,
  so the python interpreter `.venv/bin/python` links to is safe. Through the
  hardlinks, the files in the uv cache get the same permissions.
- A protected `.venv` must stay usable. uv locks a venv with `.venv/.lock`
  before it uses it, also to only read it (`uv sync --check`), and opens the
  file for writing. If it can not, uv goes on without the lock and only warns
  with `--verbose`. So `protect_venv()` keeps this one file writable for the
  owner. `uv run --no-sync`, `uv tree`, `uv pip list` and `uv export` need no
  write access. To check such things, run as a normal user under `strace`
  and look for `EACCES`.
- Every uv call that changes a `.venv` runs inside `writable_venv()` (tool
  venvs: `writable()`), which always protects again in `finally`. Nest it
  inside `reset_on_failure`. A protected `.venv` must be made writable before
  it is deleted (`env create --force`, `tool run --refresh`).
- If protection is enabled and `chmod` is missing, fail with an error that
  explains why and how to disable the protection.
- `uvrev unprotect` is for hotfixes: fast, always succeeds, not interruptible.
  It writes the marker `.unprotected` (`marker.py`: time + state, atomic,
  read-only). The next protecting command repairs the venv (`repair.py`):
  `post-chmod` by the files with a ctime later than the stamp, every other
  state by the hash of every file. Never delete the `.venv` or the cache for a
  repair (running processes), only `sync --clean` does.
  `writable()` / `writable_venv()` are for uvrev's own changes and never touch
  the marker. `uvrev run` never repairs.
- Timestamps (tested on ext3 and ext4): `chmod` always changes the ctime, also
  if the mode stays the same, and never the mtime. The mtime can be set back,
  the ctime can not. Old ext3 only stores seconds. `time.time()` is not aligned
  with file timestamps: take "now" from the filesystem (touch a file, read its
  ctime) and wait until its clock moved on (`wait_for_clock()`).
- A valid venv contains only: the files in the `RECORD` of its packages, the
  files of the venv itself (`is_venv_file()`, they can not be restored) and
  bytecode of an existing source. `uv sync --reinstall` does not restore the
  venv's own files.
- Settings: typed-settings with frozen dataclasses, `[uvrev]` table in
  `user_config_path("uvrev") / "config.toml"`, more files in `UVREV_SETTINGS`,
  overridable by `UVREV_*` environment variables.
  - typed-settings cannot convert PEP 695 `type` aliases (use `TypeAlias`).
  - It splits environment variables for tuples at `:`, not at `,`.
  - A `default_factory` runs for every instance without a value: cache it,
    if it is expensive (e.g. `installed_python_versions()`).
  - Validate in `__post_init__`; the error ends up in `InvalidSettingsError`.
- `importlib.metadata.Distribution.files` omits files that no longer exist:
  parse `RECORD` directly. Bytecode in `__pycache__` and the venv's own
  `_virtualenv.py` / `_virtualenv.pth` are not part of any `RECORD`.
- `uv sync --frozen --check` tells whether the venv matches `uv.lock`.
- `uv sync --output-format json` is still a preview schema: do not rely on it yet.
- `uv add NAME` keeps an existing constraint of NAME (no flag loosens it), so a
  requirement without version is passed as `NAME>=0` (`any_version()`), which
  is treated like no constraint everywhere (`ANY_VERSION` in `parser.py`).
- The repository of an environment has its own git identity (`uvrev`,
  `<user>@<hostname>`) and no signing (`configure_git()`), so uvrev works for
  service users and never waits for a passphrase.
- Python: `.python-version` holds a *request* (everything `uv python pin`
  accepts: version, range, implementation, path), never parse it as a version.
  Compare interpreters with `get_python_version()`. `add --python`,
  `upgrade --python` and `upgrade-python` share the pin step in `run_steps()`
  (pin first, then the command's uv call). `env create` pins the request too.
- uv runs in the folder of the environment (a dry run in a temporary copy), not
  in the working directory of the user: paths given by the user must be made
  absolute first (`absolute_python_request()`, a click callback in `options.py`).
  `uv python pin` accepts a wrong path, only the following uv command fails.
- `uv add` keeps the locked versions of other packages; with `--upgrade-all`
  use `uv add --upgrade`, then `uv sync --upgrade`.

## Writing

- Use `:` instead of `–` in texts written for the user.
- On GitLab, coverage needs the `coverage:` regex in `.gitlab-ci.yml`;
  test results are uploaded as JUnit (`junit-*.xml`).
