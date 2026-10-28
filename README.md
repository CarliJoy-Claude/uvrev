# uvrev

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
- Detailed changelog tracking both dependencies and pinned versions

---

## Commands

| Command            | Description |
|--------------------|-------------|
| `uvrev env create` | Create a new UV project with revision tracking |
| `uvrev env list`   | List all UV projects |
| `uvrev add`        | Add packages to the current project |
| `uvrev remove`     | Remove packages from the current project |
| `uvrev sync`       | Sync project dependencies |
| `uvrev upgrade`    | Upgrade all or the given packages |
| `uvrev list`       | List installed packages in the current environment |
| `uvrev history`    | Show revision history with package changes |
| `uvrev restore`    | Restore project to a specific revision |
| `uvrev path`       | Show the path to the current project |
| `uvrev run`        | Run uv commands in the project |

---

## JSON Output

The commands that change an environment (`add`, `remove`, `sync`, `upgrade`, `restore`) accept `--json`.
Then only a JSON report is written to **stdout**; all logs and the output of `uv` and `git` go to **stderr**.

```bash
uvrev add pandas numpy --json
```

```json
{
  "schema_version": 1,
  "status": "success",
  "command": "add",
  "arguments": ["pandas", "numpy"],
  "revision": 42,
  "previous_revision": 41,
  "changes": {"added": ["numpy", "pandas", "pytz"], "updated": ["python-dateutil"], "removed": []},
  "all_changed_packages": ["numpy", "pandas", "python-dateutil", "pytz"],
  "dependency_changes": {"added": ["numpy", "pandas"], "updated": [], "removed": []},
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
  "error_message": null
}
```

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | `integer` | Version of this format, currently `1`. |
| `status` | `string` | `"success"` or `"error"`. |
| `command` | `string` | The executed subcommand. |
| `arguments` | `array[string]` | The arguments given to the subcommand. |
| `revision` | `integer \| null` | Current revision after the command (unchanged if nothing changed). `null` only if it could not be determined. |
| `previous_revision` | `integer \| null` | Revision before the command. A new revision was created if it differs from `revision`. |
| `changes` | `object` | Changed packages (`added`, `updated`, `removed`), including transitive ones. Taken from `uv.lock`, for `sync` from the installed packages. `updated` contains up- and downgrades. |
| `all_changed_packages` | `array[string]` | All names in `changes`. |
| `dependency_changes` | `object` | Changed dependencies in `pyproject.toml` (what was requested, e.g. with `uvrev add`), same structure as `changes`. |
| `all_changed_dependencies` | `array[string]` | All names in `dependency_changes`. |
| `versions.packages` | `object` | For every changed package the `old` and `new` version. `null` means absent (added or removed). |
| `versions.dependencies` | `object` | For every changed dependency the `old` and `new` specifier. `""` means no version constraint, `null` means absent. |
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

## Environment Selection

`uvrev` determines the active environment based on the **currently activated virtual environment**.

Activate an environment as usual:

```bash
source .venv/bin/activate
```

Once activated, all `uvrev` commands operate on that environment automatically. No additional flags or environment variables are required.

This mirrors standard Python workflows and avoids implicit global state.

---

## Examples

```bash
uvrev env create myproject --python 3.12

# activate venv
source "$(uvrev path -e myproject)/.venv/bin/activate"

uvrev add pandas numpy
uvrev add requests

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
