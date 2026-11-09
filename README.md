# uvrev

[![pipeline](https://gitlab.com/Kound/uvrev/badges/main/pipeline.svg)](https://gitlab.com/Kound/uvrev/-/pipelines?ref=main)
[![coverage](https://gitlab.com/Kound/uvrev/badges/main/coverage.svg)](https://gitlab.com/Kound/uvrev/-/pipelines?ref=main)
[![PyPI](https://img.shields.io/pypi/v/uvrev)](https://pypi.org/project/uvrev/)
[![Python versions](https://img.shields.io/pypi/pyversions/uvrev)](https://pypi.org/project/uvrev/)
[![License](https://img.shields.io/pypi/l/uvrev)](https://gitlab.com/Kound/uvrev/-/blob/main/LICENSE)
[![Typed](https://img.shields.io/badge/typing-typed-blue)](https://peps.python.org/pep-0561/)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![prek](https://raw.githubusercontent.com/j178/prek/master/docs/assets/badge.svg)](https://github.com/j178/prek)

**uvrev** is a CLI tool that wraps the **uv** package manager and adds **git-based, conda-style revision tracking** for Python environments.

It allows you to manage UV projects as isolated environments with a full, inspectable revision history. Every package change (add, remove, update) creates a new Git commit and tag, making it easy to understand what changed and to roll back to any previous state.

---

## Overview

- Manage UV environments with automatic revision tracking
- Track dependency changes over time using Git
- Restore environments to any previous revision
- Inspect package history in a familiar conda-style format

Each environment is stored as a Git repository. Dependency changes are committed automatically, providing a complete and auditable history of your environment state.

---

## Installation

### Prerequisites

- `git`
- `uv`

### Install with uv

```bash
uv tool install uvrev
```

After installation, ensure that `uvrev` is available on your `PATH`:

```bash
uvrev --help
```

---

## Key Features

- Conda-style revision history showing added, removed, and updated packages
- Git-based storage with automatic commits and tags
- Rollback to any previous revision
- Detailed changelog tracking the Python version, the dependencies and the pinned versions

---

## Commands

| Command            | Description |
|--------------------|-------------|
| `uvrev env create` | Create a new UV project with revision tracking (does nothing if it exists) |
| `uvrev env list`   | List all UV projects |
| `uvrev env fix`    | Mark an environment as created successfully (see [Creating Environments](#creating-environments)) |
| `uvrev add`        | Add packages to the current project (`--upgrade-all` also upgrades all others, `--python` changes the Python version) |
| `uvrev remove`     | Remove packages from the current project |
| `uvrev sync`       | Sync project dependencies |
| `uvrev upgrade`    | Upgrade all or the given packages |
| `uvrev upgrade-python` | Change the Python minor version (e.g. `3.12` to `3.14`) |
| `uvrev list`       | List installed packages in the current environment |
| `uvrev history`    | Show revision history with package changes |
| `uvrev restore`    | Restore project to a specific revision |
| `uvrev path`       | Show the path to the current project |
| `uvrev run`        | Run uv commands in the project |
| `uvrev tool run`   | Run a tool like `uvx`, with the dependencies locked in its release |

---

## JSON Output

The commands that change an environment (`add`, `remove`, `sync`, `upgrade`, `upgrade-python`, `restore`) accept `--json`.
Then only a JSON report is written to **stdout**; all logs and the output of `uv` and `git` go to **stderr**.

```bash
uvrev add pandas numpy --json
```

```json
{
  "schema_version": 2,
  "status": "success",
  "command": "add",
  "arguments": ["pandas", "numpy"],
  "options": {"upgrade_all": false, "python": null},
  "revision": 42,
  "previous_revision": 41,
  "changes": {"added": ["numpy", "pandas", "pytz"], "updated": ["python-dateutil"], "downgraded": [], "removed": []},
  "all_changed_packages": ["numpy", "pandas", "python-dateutil", "pytz"],
  "dependency_changes": {"added": ["numpy", "pandas"], "updated": [], "downgraded": [], "removed": []},
  "all_changed_dependencies": ["numpy", "pandas"],
  "versions": {
    "packages": {
      "numpy": {"old": null, "new": "2.1.0"},
      "pandas": {"old": null, "new": "2.2.3"},
      "python-dateutil": {"old": "2.8.2", "new": "2.9.0"},
      "pytz": {"old": null, "new": "2024.2"}
    },
    "dependencies": {
      "numpy": {"old": null, "new": ""},
      "pandas": {"old": null, "new": ""}
    }
  },
  "python": null,
  "environments": null,
  "error_message": null
}
```

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | `integer` | Version of this format, currently `2`. |
| `status` | `string` | `"success"` or `"error"`. |
| `command` | `string` | The executed subcommand. |
| `arguments` | `array[string]` | The arguments given to the subcommand. |
| `options` | `object` | The options given to the subcommand, e.g. `{"upgrade_all": true, "python": null}` for `uvrev add --upgrade-all`. Empty for commands without options. |
| `revision` | `integer \| null` | Current revision after the command (unchanged if nothing changed). `null` only if it could not be determined. |
| `previous_revision` | `integer \| null` | Revision before the command. A new revision was created if it differs from `revision`. |
| `changes` | `object` | Changed packages (`added`, `updated`, `downgraded`, `removed`), including transitive ones. Taken from `uv.lock`, for `sync` from the installed packages. `updated` contains the upgrades, `downgraded` the packages whose version decreased. Every package is in one list only. |
| `all_changed_packages` | `array[string]` | All names in `changes`. |
| `dependency_changes` | `object` | Changed dependencies in `pyproject.toml` (what was requested, e.g. with `uvrev add`), same structure as `changes`. Constraints can not be compared, so a changed one is always in `updated` and `downgraded` is always empty. |
| `all_changed_dependencies` | `array[string]` | All names in `dependency_changes`. |
| `versions.packages` | `object` | For every changed package the `old` and `new` version. `null` means absent (added or removed). |
| `versions.dependencies` | `object` | For every changed dependency the `old` and `new` specifier. `""` means no version constraint, `null` means absent. |
| `python` | `object \| null` | `old` and `new` exact version of the Python interpreter, if it changed. `null` if it did not change. |
| `environments` | `object \| null` | `old` and `new` markers of `tool.uv.environments` (arrays of strings), if they changed. `null` if they did not change. See [Supported Environments](#supported-environments). |
| `error_message` | `string \| null` | The error, if `status` is `"error"`. |

All package names are [normalized](https://packaging.python.org/en/latest/specifications/name-normalization/) and sorted.
On failure, the report is still written (without changes) and the exit code is `1`.

Example usage in Ansible:

```yaml
- name: Run uvrev add
  ansible.builtin.shell: uvrev add {{ uv_packages | join(' ') }} --json
  register: uvrev_result

- name: Parse uvrev output
  ansible.builtin.set_fact:
    updated_packages: "{{ (uvrev_result.stdout | from_json).all_changed_packages }}"
```

---

## Changelog

Every revision lists what changed compared to the previous one:

```text
(rev 3) Upgraded Python: 3.12 → 3.13
  Environments:
    +python_version == '3.13' and sys_platform == 'linux' and platform_machine == 'x86_64'
  Python:
    ^python==3.12.3 → python==3.13.1
  Pinned Versions:
    ^numpy==2.1.0 → numpy==2.2.0
    vpandas==2.2.3 → pandas==2.2.2
```

| Marker | Meaning |
|--------|---------|
| `+` | added |
| `-` | removed |
| `^` | upgraded (or changed constraint in `Dependencies`) |
| `v` | downgraded (only for `Python` and `Pinned Versions`) |

Constraints in `pyproject.toml` (`Dependencies`) can not be compared, so they are never shown as downgrade.

---

## Python Version

The Python minor version of an environment is pinned in `.python-version` (e.g. `3.12`).
The exact version of the interpreter (e.g. `3.12.3`) is recorded in `.python-version.lock`.
The interpreter can change without uvrev, e.g. by a patch update of a uv managed Python.
Such a change is recorded with the next revision.

Use `uvrev upgrade-python` to change the minor version:

```bash
uvrev upgrade-python 3.14
```

It pins the version with `uv python pin`, recreates the virtual environment and updates `uv.lock`.
`requires-python` is not changed, `uv python pin` refuses versions that don't match it.
Going back to an older version works the same way, `uvrev restore` restores the pinned version as well.

`uvrev add --python 3.14 <pkg>...` adds packages and changes the Python version in one revision.
It runs `uv add --raw --python 3.14 <pkg>...` and then `uv python pin 3.14`.

`uvrev env create` pins the version given with `--python`.
Without it, uv chooses its default interpreter and pins its minor version.
`UV_PYTHON` is ignored, so an environment always uses the version of its `.python-version`.

### Supported Environments

uvrev resolves the packages only for the current platform and the supported Python versions,
by setting [`tool.uv.environments`](https://docs.astral.sh/uv/concepts/resolution/#limited-resolution-environments) in `pyproject.toml`:

```toml
[tool.uv]
environments = [
    "python_version == '3.12' and sys_platform == 'linux' and platform_machine == 'x86_64'",
    "python_version == '3.14' and sys_platform == 'linux' and platform_machine == 'x86_64'",
]
```

- `sys_platform` and `platform_machine` are taken from the Python interpreter running uvrev.
- The Python versions are taken from `UVREV_SUPPORT_PYTHON_VERSION` (comma separated, e.g. `3.12,3.14`).
  If it is not set, the Python versions installed for uv are used (`uv python list --only-installed`).
- Only versions matching `requires-python` are used, as uv rejects the others.
  If no version is left, `tool.uv.environments` is removed.

`add`, `remove`, `upgrade` and `upgrade-python` update `tool.uv.environments` first, if needed.
The change is part of the new revision (`Environments` in the changelog, `environments` in the JSON report).
`sync` and `restore` don't change it, so a restored revision is resolved exactly as before.

The pinned Python version must be one of the supported versions, otherwise uv fails.
Install it (e.g. `uv python install 3.14`) or add it to `UVREV_SUPPORT_PYTHON_VERSION`.
The lock file is only valid on the platform it was created on.

---

## Creating Environments

`uvrev env create` writes `.uvrev_created_successfully` (containing the uvrev version) as its last step.
The file is ignored by git (`.git/info/exclude`).

- If the environment was created successfully before, `uvrev env create` does nothing.
  The `--python` version is **not** checked against the existing environment.
- If the folder exists without the file (e.g. an aborted `uvrev env create`), it fails.
  `uvrev env create --force` deletes the folder and creates the environment again.
- Commands using an environment without the file warn about it.

**Warning:** environments created by uvrev before 0.4 don't have the file,
so `--force` deletes them including their history.
Mark them with `uvrev env fix <name>` first.

---

## Environment Selection

`uvrev` determines the active environment based on the **currently activated virtual environment**.

Activate an environment as usual:

```bash
source .venv/bin/activate
```

Once activated, all `uvrev` commands operate on that environment automatically. No additional flags or environment variables are required.

This mirrors standard Python workflows and avoids implicit global state.

---

## Running Tools with Locked Dependencies

`uvrev tool run` works like `uvx`, but installs the dependencies of the tool exactly as pinned in the `uv.lock` of its release.

```bash
uvrev tool run mytool --help
uvrev tool run --from mytool==1.2 --extra fast mytool-cli -v
uvrev tool run --from ./dist/mytool-1.2-py3-none-any.whl mytool
uvrev tool run --from "mytool @ https://example.com/mytool-1.2-py3-none-any.whl" mytool
```

```
uvrev tool run [--from SPEC] [--extra NAME]... [--python PYTHON] [--refresh] COMMAND [ARGS]...
```

- Without `--from`, `COMMAND` is also the package name.
- `SPEC` is one of:
  - `name` or `name==version` from the package index; without a version the latest one is resolved by uv.
  - The path or URL (`http(s)://`, `file://`) of a wheel, or `name @ URL`.
    Without `--from`, the command is the normalized package name from the wheel filename.
- `--extra` installs an extra of the package, it can be repeated.
- `--python` is passed to uv, `--refresh` rebuilds the cached environment.
- Everything after `COMMAND` is passed to the command unchanged.

The release must be a wheel built with the hatchling build hook `hatch-uv-lock`,
which embeds the original `uv.lock` and `pyproject.toml` in `<name>-<version>.dist-info/extra_metadata/`.
Releases without them are refused, there is no fallback to an unlocked install.
The dependencies are installed with `uv sync --locked`, so a release whose lock was outdated at build time is refused too.
Projects with dynamic `dependencies`, `optional-dependencies` or `requires-python` are not supported,
as uv would need the source tree to build that metadata. A dynamic `version` is fine.

Index releases can not change, so the cached environment of a release is identified by its version.
A wheel from a path or URL can change with the same version, e.g. after a rebuild.
So it is identified by its sha256: it is copied (or downloaded) on every run, and a changed wheel gets a new environment.
URLs are downloaded by uvrev directly with [httpx2](https://github.com/pydantic/httpx2), uv's index credentials are not used for them.
The download uses the trust store of the OS (or `SSL_CERT_FILE`/`SSL_CERT_DIR`) and timeouts, and an `https` download is never redirected to `http`.
A `#sha256=<hex>` fragment in a URL is verified, e.g. `https://example.com/mytool-1.2-py3-none-any.whl#sha256=…`.
Plain `http` URLs without it work, but uvrev warns that the integrity of the wheel is not verified.
sdists, source directories and VCS URLs are not supported, as they would need a build of the source tree.
To run a tool from its source tree with its locked dependencies, use `uv sync --locked` there.

uv's normal configuration is used (indexes, credentials), except `UV_FROZEN`, `UV_LOCKED`, `UV_NO_SYNC` and `UV_PROJECT_ENVIRONMENT`, which are ignored.
The environments are cached in `tools/` of the uvrev cache directory (`platformdirs.user_cache_dir("uvrev")`, can be changed with `UVREV_CACHE`).

---

## Examples

```bash
uvrev env create myproject --python 3.12

# activate venv
source "$(uvrev path -e myproject)/.venv/bin/activate"

uvrev add pandas numpy
uvrev add requests

# make sure pandas and numpy are installed and upgrade everything
uvrev add --upgrade-all pandas numpy

# add polars and change to Python 3.14
uvrev add --python 3.14 polars

uvrev list
uvrev history
uvrev restore 3
uvrev run pip list
```

---

## Why uvrev?

`uvrev` combines the speed and simplicity of **uv** with the safety and transparency of **Git**, giving you:

- Reproducible environments
- Clear insight into dependency changes
- Confidence to experiment, knowing you can always roll back
