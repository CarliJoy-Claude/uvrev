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
| `uvrev list`       | List installed packages in the current environment |
| `uvrev history`    | Show revision history with package changes |
| `uvrev restore`    | Restore project to a specific revision |
| `uvrev path`       | Show the path to the current project |
| `uvrev run`        | Run uv commands in the project |

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
