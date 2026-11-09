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
- Run it like CI as well: uv version from `.gitlab-ci.yml`, `UV_FROZEN=true`,
  on the lowest and highest Python of the matrix (e.g. `UV_PYTHON=3.12` and `3.15`).
  CI also checks the lock file: `uv lock --check`.
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
- stdout is reserved for results (e.g. the `--json` report). Logs, executed
  commands and the output of `uv`, `git` and `chmod` go to stderr.
  Only `uvrev run` and the tools of `uvrev tool run` write to stdout.
- Import cycle to keep in mind: `types` imports `settings`, `commands` imports
  `types`. So `settings` must not import `commands`.

## Testing

- **Never use `monkeypatch.setattr`** (or `unittest.mock.patch`). Patching
  globals has hidden side effects. Make dependencies explicit instead:
  - small functions take the dependency as a parameter, defaulting to the
    real implementation (e.g. `uv: UvRunner = uv_tool`),
  - bigger flows take a frozen service object: `Services` in `cli.py`
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
- Every uv call that changes a `.venv` runs inside `writable_venv()` (tool
  venvs: `writable()`), which always protects again in `finally`. Nest it
  inside `reset_on_failure`. A protected `.venv` must be made writable before
  it is deleted (`env create --force`, `tool run --refresh`).
- If protection is enabled and `chmod` is missing, fail with an error that
  explains why and how to disable the protection.
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
- `uv add` keeps the locked versions of other packages; with `--upgrade-all`
  use `uv add --upgrade`, then `uv sync --upgrade`.

## Writing

- Use `:` instead of `–` in texts written for the user.
- On GitLab, coverage needs the `coverage:` regex in `.gitlab-ci.yml`;
  test results are uploaded as JUnit (`junit-*.xml`).
