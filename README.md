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
| `uvrev add`        | Add packages to the current project (`--upgrade-all` also upgrades all others, `--python` changes the Python) |
| `uvrev remove`     | Remove packages from the current project (`--ignore-missing` skips packages that are no dependencies) |
| `uvrev sync`       | Sync project dependencies (`--clean` deletes the uv cache and the venv first) |
| `uvrev upgrade`    | Upgrade all or the given packages (`--python` changes the Python) |
| `uvrev upgrade-python` | Change the Python (e.g. `3.12` to `3.14`) without upgrading packages |
| `uvrev list`       | List installed packages in the current environment |
| `uvrev history`    | Show revision history with package changes |
| `uvrev restore`    | Restore project to a specific revision |
| `uvrev path`       | Show the path to the current project |
| `uvrev run`        | Run uv commands in the project |
| `uvrev tool run`   | Run a tool like `uvx`, with the dependencies locked in its release |
| `uvrev protect`    | Repair an unprotected venv and make it read-only |
| `uvrev unprotect`  | Make the venv writable for a hotfix |
| `uvrev verify`     | Check every file of the venv against the installed packages, `--fix` repairs it |
| `uvrev settings`   | Show the settings and the location of the settings file |

---

## Protected Environments

By default the `.venv` of an environment is **read-only**: uvrev runs `chmod -R a-w .venv`,
which removes all write permissions and keeps read and execute permissions as they are.
So nobody can change the installed packages by accident, e.g. with `pip install` or an editor.

uvrev makes the `.venv` writable (`chmod -R u+w .venv`) only while `uv` changes it and protects it
again afterwards, even if `uv` fails. `uvrev run` syncs the environment first if needed and then
runs the command with the protected `.venv`. The environments of `uvrev tool run` are protected as well.
Only the lock file of uv (`.venv/.lock`, an empty file) stays writable for its owner: uv needs it to
lock the `.venv`, also when it only reads it.

uv hardlinks the installed files from its cache, so `chmod` also changes the permissions of
these files in the uv cache, and with that in other environments using the same files.
If that is a problem, disable the protection or use other modes (see [Settings](#settings)).

The protection needs the `chmod` command. If it is missing, uvrev stops with an error
explaining how to disable the protection.

### Hotfixes: `unprotect` and `protect`

```bash
uvrev unprotect   # make the .venv writable for a hotfix
# ... change files in the .venv ...
uvrev protect     # repair the .venv and make it read-only again
```

`uvrev unprotect` is made for hotfixes: it is fast, as nothing is checked, and it can not be
interrupted with Ctrl+C. It marks the environment with the file `.unprotected`,
which contains the time when the `.venv` became writable.

The mark is the reason for a **repair**. It is done by `uvrev protect`, and by every command that changes
the environment (`add`, `remove`, `sync`, `upgrade`, `upgrade-python`, `restore`) before it does its work.
Afterwards the `.venv` is valid again: it only contains the files of the packages locked in `uv.lock`,
with the content of their wheels.

- Packages with files changed since `unprotect` are removed from the uv cache and reinstalled.
  A file counts as changed if its ctime is later than the time in `.unprotected`.
  The ctime can not be set back by a user, unlike the modification time.
- Files that no package installed are removed, in the whole `.venv`.
- The bytecode (`__pycache__`) is brought in sync with the sources: without source it is removed,
  the rest is compiled again (setting `compile_bytecode`).
- The `.venv` is protected and `.unprotected` is removed.

The `.venv` and the uv cache are kept, so running processes are not disturbed more than necessary.
`uvrev run` does **not** repair and does not sync an unprotected environment: it runs with the hotfix.
`uvrev unprotect` for an environment that is unprotected already does nothing, the time is kept.

What the repair can not do:

- The files of the virtual environment itself (`pyvenv.cfg`, the `python` links and the `activate` scripts)
  are not part of any package. If one of them was changed, the repair fails and the environment stays marked.
  Run `uvrev sync --clean`.
- If `uvrev unprotect` did not finish (e.g. it was killed) or `.unprotected` was changed, there is no time
  that can be trusted. `uvrev unprotect` still makes the `.venv` writable and warns. The repair then
  compares every file with the hash recorded by its package, like `uvrev verify --fix`, which is slower.
  It warns that `uvrev sync --clean` might be needed, as this can not see everything (see below).
- Other environments: uvrev is made for one environment. With several, a changed file of a shared package
  is changed in the others as well, where nobody knows about it. `uvrev unprotect` warns about this.
  Run `uvrev verify --fix` for the others.
- The time comes from the clock of the filesystem. If the system clock is set back while the environment
  is unprotected, later changes can be missed. `uvrev verify --fix` does not depend on the time.

### Starting anew: `sync --clean`

```bash
uvrev sync --clean
```

deletes the **complete** uv cache and the `.venv` and installs everything again from `uv.lock`.
It is the way out of every state that can not be repaired. It needs the network,
and running processes of the environment lose their files.
The JSON report has `"clean": true` in `options`. It lists no changed packages if the versions are the
same as before, although everything was installed again.

## Verify Environments

`uvrev verify` checks the whole `.venv` and finds

- missing or modified files of the installed packages, compared with the hashes in their `RECORD` files,
- packages not in sync with `uv.lock`,
- files that no package installed. Allowed are the files of the virtual environment itself
  and bytecode of an existing source.

`uvrev verify` exits with `1` if problems were found.
`uvrev verify --fix` removes the affected packages from the uv cache (`uv cache clean --force`),
reinstalls them, removes the files that no package installed, brings the bytecode in sync
and protects the `.venv` again (if enabled). A mark of `uvrev unprotect` is removed.

It reads every file, so it is slower than the repair after `uvrev unprotect`, but it does not depend on
timestamps. It trusts the `RECORD` files in the `.venv` and can not check the files of the virtual
environment itself. To be sure about those, use `uvrev sync --clean`.

uv hardlinks the installed files from its cache, so a file changed in a `.venv` is changed in the
cache as well, and in all other environments using it ([uv#22979](https://github.com/astral-sh/uv/issues/22979)).
Reinstalling alone would link the changed file again, so the cache entry is removed first.
Other environments sharing the changed file are only repaired by running `uvrev verify --fix` for them, too.

To verify and fix the environment after every change, pass `--verify` to
`add`, `remove`, `sync`, `upgrade`, `upgrade-python` and `restore`, or enable `verify_after_change`.

## Settings

uvrev reads its settings from the `[uvrev]` table of a TOML file in the user config directory,
e.g. `~/.config/uvrev/config.toml` on Linux (`uvrev settings` shows the exact location).
`UVREV_SETTINGS` can list additional settings files (separated by `:`).

```toml
[uvrev]
# Make the .venv read-only, so it can only be changed by uvrev
protect = true
# Command used to change the permissions (name or path)
chmod_command = "chmod"
# Mode for `chmod -R <mode> .venv` to protect the .venv
protect_mode = "a-w"
# Mode for `chmod -R <mode> .venv` to make the .venv writable again
unprotect_mode = "u+w"
# Compile bytecode on install, a protected .venv can not cache it later
compile_bytecode = true
# Python versions (major.minor) the environments are resolved for,
# by default the Python versions installed for uv (see Supported Environments)
python_versions_supported = ["3.12", "3.14"]
# Run `uvrev verify --fix` after every change of an environment
verify_after_change = false
```

Each setting can be overridden by an environment variable, e.g. `UVREV_PROTECT=false`
or `UVREV_PYTHON_VERSIONS_SUPPORTED=3.12:3.14` (`3.12,3.14` works as well).

---

## JSON Output

With `--json` only a JSON report is written to **stdout**; all logs and the output of `uv` and `git` go to **stderr**.
It is accepted by

- the commands that change the packages of an environment (`add`, `remove`, `sync`, `upgrade`, `upgrade-python`,
  `restore`), which write the *change report* described here,
- `env create`, `protect`, `unprotect` and `verify`, see [Reports of other commands](#reports-of-other-commands).

All reports start with the fields `schema_version`, `status`, `command`, `arguments` and `options`
and end with `error_message`.

```bash
uvrev add pandas numpy --json
```

```json
{
  "schema_version": 2,
  "status": "success",
  "command": "add",
  "arguments": ["pandas", "numpy"],
  "options": {"upgrade_all": false, "python": null, "dry_run": false},
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
  "python_request": null,
  "environments": null,
  "repair": null,
  "verify": null,
  "error_message": null
}
```

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | `integer` | Version of this format, currently `2`. It only changes if existing fields change or are removed: new fields and new keys in `options` are added without a new version, so ignore fields you don't know. |
| `status` | `string` | `"success"` or `"error"`. |
| `command` | `string` | The executed subcommand. |
| `arguments` | `array[string]` | The arguments given to the subcommand. |
| `options` | `object` | The options given to the subcommand, e.g. `{"upgrade_all": true, "python": null, "dry_run": false}` for `uvrev add --upgrade-all`. Empty for commands without options. |
| `revision` | `integer \| null` | Current revision after the command (unchanged if nothing changed). `null` only if it could not be determined. |
| `previous_revision` | `integer \| null` | Revision before the command. A new revision was created if it differs from `revision`. |
| `changes` | `object` | Changed packages (`added`, `updated`, `downgraded`, `removed`), including transitive ones. Taken from `uv.lock`, for `sync` from the installed packages. `updated` contains the upgrades, `downgraded` the packages whose version decreased. Every package is in one list only. |
| `all_changed_packages` | `array[string]` | All names in `changes`. |
| `dependency_changes` | `object` | Changed dependencies in `pyproject.toml` (what was requested, e.g. with `uvrev add`), same structure as `changes`. Constraints can not be compared, so a changed one is always in `updated` and `downgraded` is always empty. |
| `all_changed_dependencies` | `array[string]` | All names in `dependency_changes`. |
| `versions.packages` | `object` | For every changed package the `old` and `new` version. `null` means absent (added or removed). |
| `versions.dependencies` | `object` | For every changed dependency the `old` and `new` specifier. `""` means no version constraint, `null` means absent. |
| `python` | `object \| null` | `old` and `new` exact version of the Python interpreter, if it changed. `null` if it did not change. |
| `python_request` | `object \| null` | `old` and `new` pinned Python request (the content of `.python-version`, e.g. `3.14`, `>=3.13, <3.15` or `/usr/bin/python3.14`), if it changed. `null` if it did not change. See [Python Version](#python-version). |
| `environments` | `object \| null` | `old` and `new` markers of `tool.uv.environments` (arrays of strings), if they changed. `null` if they did not change. See [Supported Environments](#supported-environments). |
| `repair` | `object \| null` | The repair of an environment that was unprotected with `uvrev unprotect`, done before the change: `mode` (`fast`: by the files changed since unprotecting, `full`: by the hash of every file), `packages` (the reinstalled packages) and `removed_files`. `null` if the environment was not unprotected. See [Hotfixes](#hotfixes-unprotect-and-protect). |
| `verify` | `object \| null` | The result of verifying the venv after the change (`--verify` or the setting `verify_after_change`): `problems`, `remaining_problems` and `fixed` like in the [report of `uvrev verify`](#reports-of-other-commands). `null` if it did not run. If problems remain, `status` is `"error"`, but the other fields still describe the change that was made. |
| `error_message` | `string \| null` | The error, if `status` is `"error"`. |

All package names are [normalized](https://packaging.python.org/en/latest/specifications/name-normalization/) and sorted.

### Dry run

`add`, `remove`, `upgrade`, `upgrade-python` and `restore` accept `--dry-run` (e.g. for the check mode of Ansible).
It reports what the command would change, without touching the environment, its files or its revisions:

```bash
uvrev upgrade --dry-run          # shows the changelog the upgrade would have
uvrev add --dry-run --json six   # the same report as without --dry-run
uvrev restore --dry-run 3        # shows what restoring revision 3 would change
```

The report is the one of the real command, with these differences:

- `options` contains `"dry_run": true`.
- `revision` equals `previous_revision`, as no revision is created.
- `python` (the exact interpreter version) is only predicted if the pinned Python version would change
  and uv finds an interpreter for the new version. A newer patch version of the current Python,
  or an interpreter uv would have to download, is only reported by the real command.
- `verify` is always `null`: nothing is installed, so `--verify` is ignored.

uvrev copies `pyproject.toml`, `uv.lock` and `.python-version` to a temporary folder and lets uv resolve
the change there without installing anything (`uv add --no-sync`, `uv remove --no-sync`, `uv lock`).
So a dry run needs the package index like the real command, but no disk space for packages.

### Reports of other commands

`uvrev env create [name] --json` (command `env create`, options `python` and `force`):

| Field | Type | Description |
|-------|------|-------------|
| `created` | `boolean` | `false` if the environment existed already (nothing was done). |
| `path` | `string` | Path of the project of the environment. |
| `python` | `string \| null` | The exact version of the Python interpreter of the environment (e.g. `3.14.1`), also if it existed already. `null` if unknown. |
| `python_request` | `string \| null` | The pinned Python request of the environment (the content of `.python-version`, e.g. `3.14` or `/usr/bin/python3.14`). `null` if unknown. |

`uvrev protect --json` and `uvrev unprotect --json`:

| Field | Type | Description |
|-------|------|-------------|
| `changed` | `boolean` | `unprotect`: `true` if the environment was not unprotected before. `protect`: `true` if it was unprotected (and therefore repaired). The permissions are not inspected. |
| `protected` | `boolean \| null` | The state afterwards, `null` if the command failed. |
| `path` | `string` | Path of the `.venv`. |
| `state` | `string \| null` | `unprotect`: `post-chmod` (the time of unprotecting is known) or `forced` (it is not, the repair will compare every file). `null` for `protect`. |
| `repair` | `object \| null` | `protect`: the repair that was done, like `repair` of the change report. `null` if the environment was not unprotected, and for `unprotect`. |

`uvrev verify [--fix] --json` (option `fix`):

| Field | Type | Description |
|-------|------|-------------|
| `problems` | `array[object]` | The problems found (before fixing). Each has `kind` (`missing`, `modified`, `not_synced` or `untracked`), `path`, `package` (`string \| null`) and `is_warning`. |
| `remaining_problems` | `array[object]` | The problems left after `--fix`, the same as `problems` without `--fix`. |
| `fixed` | `boolean` | `true` if `--fix` repaired at least one problem. |

If problems remain, `status` is `"error"`, `error_message` says how many, and the exit code is `1`.
`is_warning` is always `false`: files that no package installed were warnings before, now `--fix` removes them.

```json
{
  "schema_version": 2,
  "status": "success",
  "command": "verify",
  "arguments": [],
  "options": {"fix": true},
  "problems": [
    {"kind": "modified", "path": "/envs/base/.venv/lib/python3.14/site-packages/six.py", "package": "six", "is_warning": false}
  ],
  "remaining_problems": [],
  "fixed": true,
  "error_message": null
}
```

### Failures

With `--json`, **every** failure after the command line was parsed writes a report with `"status": "error"`
and a readable `error_message`, and the exit code is `1`. This includes an unknown environment,
invalid settings, a missing `uv` or `git` command and unexpected errors.
The report has the fields of the command, without changes.

Only wrong usage of the command line (e.g. an unknown option) exits with `2` and writes no report.

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

The Python of an environment is pinned in `.python-version` as a *Python request*:
a version (e.g. `3.12`), but also a range, an implementation or the path of an interpreter.
The exact version of the interpreter (e.g. `3.12.3`) is recorded in `.python-version.lock`.
The interpreter can change without uvrev, e.g. by a patch update of a uv managed Python.
Such a change is recorded with the next revision.

Three commands change the pinned request, all in the same way:

```bash
uvrev upgrade-python 3.14         # only change Python, the packages keep their versions
uvrev upgrade --python 3.14       # change Python and upgrade all (or the given) packages
uvrev add --python 3.14 polars    # change Python and add packages

uvrev upgrade-python /usr/bin/python3.14    # the request can be the path of an interpreter
```

They first pin the request with `uv python pin`, then run their uv command (`uv sync`, `uv sync --upgrade`,
`uv add`), which recreates the virtual environment if needed. Everything is one revision.

- The request is everything `uv python pin` accepts: a version (`3.14`, `3.14.1`), a range (`'>=3.13,<3.15'`),
  an implementation (`cpython-3.14`, `pypy@3.11`) or the path of an interpreter (`/usr/bin/python3.14`).
- A path can also be the folder of an installation that contains `bin/python` (e.g. another venv).
- A relative path (`./bin/python`) refers to your working directory and is pinned as absolute path.
  A path is pinned as given (symlinks are kept), so the environment only works where that interpreter exists.
- `requires-python` is not changed, uv refuses requests that don't match it. So going back to an older
  version works the same way. `uvrev restore` restores the pinned request as well.
- The revision tells what happened, e.g. `Upgraded Python: 3.13.9 → 3.14.1 (pinned 3.14)` (or `Downgraded`),
  compared by the versions of the interpreter. If only the request changed it is `Pinned Python cpython-3.14`,
  and nothing is mentioned if neither changed.
- The JSON report has the interpreter versions in `python` and the requests in `python_request`.

`uvrev env create` pins the request given with `--python` as well, exactly as given
(e.g. the path of an interpreter or a range, not only the minor version of the interpreter found).
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
- The Python versions are taken from the setting `python_versions_supported`
  (environment variable `UVREV_PYTHON_VERSIONS_SUPPORTED`, see [Settings](#settings)).
  If it is not set, the Python versions installed for uv are used (`uv python list --only-installed`).
- Only versions matching `requires-python` are used, as uv rejects the others.
  If no version is left, `tool.uv.environments` is removed.

`add`, `remove`, `upgrade` and `upgrade-python` update `tool.uv.environments` first, if needed.
The change is part of the new revision (`Environments` in the changelog, `environments` in the JSON report).
`sync` and `restore` don't change it, so a restored revision is resolved exactly as before.

The pinned Python version must be one of the supported versions, otherwise uv fails.
Install it (e.g. `uv python install 3.14`) or add it to the setting `python_versions_supported`.
The lock file is only valid on the platform it was created on.

---

## Creating Environments

`uvrev env create` writes `.uvrev_created_successfully` (containing the uvrev version) as its last step.
The file is ignored by git (`.git/info/exclude`).

- If the environment was created successfully before, `uvrev env create` does nothing.
  `--python` is **not** applied to the existing environment: uvrev warns if another request is pinned
  (the texts are compared, ignoring spaces), and `--json` reports the pinned request as `python_request`, the version of
  its interpreter as `python` and `"created": false`.
  Change it with `uvrev upgrade-python`.
- If the folder exists without the file (e.g. an aborted `uvrev env create`), it fails.
  `uvrev env create --force` deletes the folder and creates the environment again.
- Commands using an environment without the file warn about it.

**Warning:** environments created by uvrev before 0.4 don't have the file,
so `--force` deletes them including their history.
Mark them with `uvrev env fix <name>` first.

---

## Git Identity

Each environment is a git repository, and uvrev commits the revisions itself.
`uvrev env create` configures the repository with its own identity
(`user.name = uvrev`, `user.email = <user>@<hostname>`) and disables signing of commits and tags.
So it works for users without a git identity (e.g. service users), and never waits for a passphrase.
`uvrev env fix` applies the same configuration to an existing environment.

---

## Environment Selection

The commands working on an environment use the first of:

1. the option `--env <name>` (`-e <name>`),
2. the environment variable `UVREV_ENV=<name>`,
3. the **currently activated virtual environment**, if it is a uvrev environment,
4. the environment `base`.

So after activating an environment as usual, all `uvrev` commands operate on it:

```bash
source "$(uvrev path -e myproject)/.venv/bin/activate"
uvrev add pandas   # added to myproject
```

`uvrev env create [name]` and `uvrev env fix [name]` take the name as argument and don't use `UVREV_ENV`.

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

## Adding Packages without a Version

A package given without a version means **any version**, like in conda:

```bash
uvrev add 'six==1.15.0'   # pin six
uvrev add six             # drop the pin again
```

uv keeps an existing constraint when a bare name is added, so uvrev passes such a requirement
as `six>=0` to uv (`uv add --raw 'six>=0'`), and `pyproject.toml` contains `six>=0`.

- Extras and markers are kept: `httpie[socks]` becomes `httpie[socks]>=0`.
- Requirements with a version, URLs, paths and VCS requirements are passed to uv unchanged.
- `six` and `six>=0` are the same requirement for uvrev: the changelog and the JSON report show both
  as no constraint (`""` in `versions.dependencies`), and an environment that still contains `six`
  gets no new revision by `uvrev add six`.
- Dropping a constraint does not change the installed version: uv keeps the locked version until
  an upgrade (`uvrev upgrade six` or `uvrev add --upgrade-all six`).

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

# upgrade everything and change to Python 3.15
uvrev upgrade --python 3.15

# use a specific interpreter, given by its path
uvrev upgrade-python /usr/bin/python3.14

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
