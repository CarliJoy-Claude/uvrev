"""Run tools with the dependencies pinned in the uv.lock embedded in their wheel.

Wheels built with the hatchling build hook ``hatch-uv-lock`` contain the
original ``uv.lock`` and ``pyproject.toml`` of the project in
``<name>-<version>.dist-info/extra_metadata/``.
``uvrev tool run`` installs the dependencies exactly as pinned in that lock.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from itertools import chain
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from filelock import FileLock
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import NormalizedName, canonicalize_name
from packaging.version import Version
from platformdirs import user_cache_path

from .commands import show_cmd, uv_tool
from .exceptions import ToolError
from .types import SubprocessFailure

RECEIPT_NAME: Final[str] = "uvrev-receipt.json"
EXTRA_METADATA: Final[str] = "extra_metadata"
EMBEDDED_FILES: Final[tuple[str, ...]] = ("uv.lock", "pyproject.toml")
# Variables of the caller that would change how the environment is built
IGNORED_VARIABLES: Final[frozenset[str]] = frozenset(
    {"UV_FROZEN", "UV_NO_SYNC", "UV_LOCKED", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV"}
)
# Dynamic fields that make uv build the metadata, which needs the source tree
DYNAMIC_NEEDS_SOURCE: Final[tuple[str, ...]] = (
    "dependencies",
    "optional-dependencies",
    "requires-python",
)
ARCHIVE_SUFFIXES: Final[tuple[str, ...]] = (".whl", ".zip", ".tar.gz", ".tgz")
SUPPORTED_SPEC: Final[str] = "Give 'name' or 'name==version'"


@dataclass(frozen=True)
class ToolSpec:
    """Package given by ``--from`` or as COMMAND"""

    name: NormalizedName
    version: Version | None
    requested_name: str


@dataclass(frozen=True)
class ToolRequest:
    """Fully resolved tool environment request"""

    name: NormalizedName
    version: Version
    extras: tuple[NormalizedName, ...]
    python: str | None

    @property
    def requirement(self) -> str:
        return f"{self.name}=={self.version}"

    @property
    def python_args(self) -> tuple[str, ...]:
        if self.python is None:
            return ()
        return ("--python", self.python)

    @property
    def key(self) -> str:
        """Cache key: the version and a short hash of the extras and python request

        >>> request = ToolRequest(NormalizedName("a"), Version("1.0"), (), None)
        >>> request.key
        '1.0-f8d3418d438a'
        >>> other = ToolRequest(NormalizedName("a"), Version("1.0"), (), "3.13")
        >>> request.key != other.key
        True
        """
        payload = json.dumps({"extras": self.extras, "python": self.python})
        digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
        return f"{self.version}-{digest}"


@dataclass(frozen=True)
class EmbeddedProject:
    """The original uv.lock and pyproject.toml, byte for byte"""

    lock: bytes
    pyproject: bytes


@dataclass(frozen=True)
class Receipt:
    """Written last, marks an environment as complete"""

    package: str
    version: str
    uv_lock_sha256: str
    pyproject_sha256: str
    extras: tuple[str, ...]
    python: str | None
    uv_version: str
    created: str

    def write(self, directory: Path) -> None:
        tmp = directory / f"{RECEIPT_NAME}.tmp"
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
        os.replace(tmp, directory / RECEIPT_NAME)


def parse_spec(spec: str) -> ToolSpec:
    """Parse ``name`` or ``name==version``.

    Examples:
        >>> parse_spec("My_Tool")
        ToolSpec(name='my-tool', version=None, requested_name='My_Tool')
        >>> parse_spec("tool==1.2").version
        <Version('1.2')>
        >>> parse_spec("tool[a,b]")
        Traceback (most recent call last):
        ...
        uvrev.exceptions.ToolError: Extras in brackets are not supported ('tool[a,b]'), use --extra a --extra b
        >>> parse_spec("tool>=1")
        Traceback (most recent call last):
        ...
        uvrev.exceptions.ToolError: Unsupported package specification 'tool>=1'. Give 'name' or 'name==version'
    """
    url_or_path = (
        "://" in spec
        or "/" in spec
        or "\\" in spec
        or spec.startswith(".")
        or spec.lower().endswith(ARCHIVE_SUFFIXES)
    )
    if not url_or_path:
        try:
            requirement = Requirement(spec)
        except InvalidRequirement as e:
            raise ToolError(
                f"Invalid package specification '{spec}': {e}. {SUPPORTED_SPEC}"
            ) from None
        url_or_path = requirement.url is not None
    if url_or_path:
        raise ToolError(
            f"URLs and paths are not supported ('{spec}'): only released wheels "
            f"from the package index can be run. {SUPPORTED_SPEC}"
        )
    if requirement.extras:
        options = " ".join(f"--extra {e}" for e in sorted(requirement.extras))
        raise ToolError(
            f"Extras in brackets are not supported ('{spec}'), use {options}"
        )
    specifiers = tuple(requirement.specifier)
    name = canonicalize_name(requirement.name)
    if not specifiers and requirement.marker is None:
        return ToolSpec(name, None, requirement.name)
    if (
        len(specifiers) != 1
        or requirement.marker is not None
        or specifiers[0].operator != "=="
        or specifiers[0].version.endswith("*")
    ):
        raise ToolError(f"Unsupported package specification '{spec}'. {SUPPORTED_SPEC}")
    return ToolSpec(name, Version(specifiers[0].version), requirement.name)


def parse_resolved_version(output: str, name: NormalizedName) -> Version:
    """Get the version of ``name`` from the output of ``uv pip compile``.

    Examples:
        >>> parse_resolved_version("\\n# comment\\nnot valid!\\nother==1\\nMy-Tool==2.0\\n", "my-tool")
        <Version('2.0')>
        >>> parse_resolved_version("my-tool>=1\\n", "my-tool")
        Traceback (most recent call last):
        ...
        uvrev.exceptions.ToolError: Could not find the version of 'my-tool' in the output of uv:
        my-tool>=1
    """
    for line in output.splitlines():
        try:
            requirement = Requirement(line.strip())
        except InvalidRequirement:
            continue
        specifiers = tuple(requirement.specifier)
        if (
            canonicalize_name(requirement.name) == name
            and len(specifiers) == 1
            and specifiers[0].operator == "=="
        ):
            return Version(specifiers[0].version)
    raise ToolError(
        f"Could not find the version of '{name}' in the output of uv:\n{output.strip()}"
    )


def normalize_extras(extras: Sequence[str]) -> tuple[NormalizedName, ...]:
    """Sorted, unique and normalized extras

    >>> normalize_extras(["B", "a", "b"])
    ('a', 'b')
    """
    return tuple(sorted({canonicalize_name(extra) for extra in extras}))


def get_cache_dir() -> Path:
    """Cache directory of uvrev, can be changed with UVREV_CACHE"""
    if custom := os.getenv("UVREV_CACHE"):
        return Path(custom)
    return user_cache_path("uvrev")


def tool_directory(request: ToolRequest) -> Path:
    return get_cache_dir() / "tools" / request.name / request.key


def uv_environment(venv: Path | None = None) -> Mapping[str, str]:
    """Environment for uv: the user's configuration without variables that change
    how the environment is built"""
    env = {k: v for k, v in os.environ.items() if k not in IGNORED_VARIABLES}
    if venv is not None:
        env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    return MappingProxyType(env)


def resolve_version(name: NormalizedName, python: str | None) -> Version:
    """Resolve the latest version with uv's index configuration and credentials"""
    python_args: tuple[str, ...] = ()
    if python is not None:
        python_args = ("--python", python)
    output = uv_tool(
        "pip",
        "compile",
        "-",
        "--no-deps",
        "--no-header",
        "--no-annotate",
        "--quiet",
        *python_args,
        env=uv_environment(),
        stdin=f"{name}\n",
        capture_stdout=True,
    )
    return parse_resolved_version(output, name)


def find_dist_info(target: Path, name: NormalizedName) -> Path:
    """Find the single dist-info folder of the package in an install target"""
    candidates = tuple(
        path
        for path in target.glob("*.dist-info")
        if canonicalize_name(path.name.partition("-")[0]) == name
    )
    if len(candidates) != 1:
        raise ToolError(
            f"Expected exactly one dist-info folder of '{name}' in {target}, "
            f"found {len(candidates)}"
        )
    return candidates[0]


def read_embedded(dist_info: Path) -> EmbeddedProject:
    """Read the uv.lock and pyproject.toml embedded by hatch-uv-lock"""
    folder = dist_info / EXTRA_METADATA
    missing = tuple(f for f in EMBEDDED_FILES if not (folder / f).is_file())
    if missing:
        files = ", ".join(f"{EXTRA_METADATA}/{f}" for f in missing)
        raise ToolError(
            f"The release ({dist_info.name}) does not contain {files}: "
            "it was not built with hatch-uv-lock, so its dependencies are not "
            "locked. Installing without a lock is not supported."
        )
    return EmbeddedProject(
        lock=(folder / "uv.lock").read_bytes(),
        pyproject=(folder / "pyproject.toml").read_bytes(),
    )


def load_toml(content: bytes, filename: str) -> Mapping[str, Any]:
    try:
        return MappingProxyType(tomllib.loads(content.decode()))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ToolError(f"The embedded {filename} is not valid TOML: {e}") from None


def check_embedded(
    embedded: EmbeddedProject,
    name: NormalizedName,
    extras: Sequence[NormalizedName],
) -> None:
    """Checks that uv can sync the embedded files without the source tree"""
    project: Mapping[str, Any] = load_toml(embedded.pyproject, "pyproject.toml").get(
        "project", {}
    )
    lock = load_toml(embedded.lock, "uv.lock")

    embedded_name = project.get("name")
    if not isinstance(embedded_name, str) or canonicalize_name(embedded_name) != name:
        raise ToolError(
            f"The embedded pyproject.toml belongs to project '{embedded_name}', "
            f"not to '{name}'"
        )

    if not any(
        canonicalize_name(package.get("name", "")) == name
        and package.get("source") == {"editable": "."}
        for package in lock.get("package", ())
    ):
        raise ToolError(
            f"The embedded uv.lock has no package '{name}' with "
            'source = { editable = "." }, so it is not the lock of this project'
        )

    dynamic = sorted(set(project.get("dynamic", ())) & set(DYNAMIC_NEEDS_SOURCE))
    if dynamic:
        raise ToolError(
            f"The embedded pyproject.toml declares {', '.join(dynamic)} as dynamic. "
            "uv would need the source tree to build this metadata, which is not "
            "part of the release."
        )

    available = sorted(
        canonicalize_name(extra) for extra in project.get("optional-dependencies", {})
    )
    unknown = tuple(extra for extra in extras if extra not in available)
    if unknown:
        raise ToolError(
            f"Unknown extra(s) of '{name}': {', '.join(unknown)}. "
            f"Available: {', '.join(available) or 'none'}"
        )


def get_uv_version(env: Mapping[str, str]) -> str:
    """Version of uv

    >>> get_uv_version(os.environ).count(".") >= 1
    True
    """
    output = uv_tool("--version", env=env, capture_stdout=True)
    return output.strip().removeprefix("uv ").split(" ")[0]


def has_receipt(directory: Path, request: ToolRequest) -> bool:
    """True if the environment in the directory is complete"""
    try:
        data = json.loads((directory / RECEIPT_NAME).read_text())
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and data.get("package") == request.name
        and data.get("version") == str(request.version)
    )


def sync_dependencies(
    directory: Path, request: ToolRequest, env: Mapping[str, str]
) -> None:
    """Install the locked dependencies without the project itself"""
    extra_args = tuple(chain.from_iterable(("--extra", e) for e in request.extras))
    try:
        uv_tool(
            "sync",
            "--project",
            str(directory),
            "--locked",
            "--no-install-project",
            "--no-default-groups",
            *extra_args,
            *request.python_args,
            env=env,
            capture_stderr=True,
        )
    except SubprocessFailure as e:
        if "--locked" in (e.error.stderr or ""):
            raise ToolError(
                f"The uv.lock in the release {request.requirement} does not match "
                "its pyproject.toml: the lock was outdated when the release was "
                "built, so the locked dependencies can not be installed."
            ) from None
        raise


def build_environment(directory: Path, request: ToolRequest) -> None:
    """Build the environment in place (venvs are not relocatable)"""
    directory.mkdir(parents=True)
    venv = directory / ".venv"
    env = uv_environment(venv)

    with tempfile.TemporaryDirectory(prefix="uvrev-tool-") as target:
        uv_tool(
            "pip",
            "install",
            "--no-deps",
            "--only-binary",
            request.name,
            "--target",
            target,
            *request.python_args,
            request.requirement,
            env=env,
        )
        embedded = read_embedded(find_dist_info(Path(target), request.name))

    check_embedded(embedded, request.name, request.extras)
    directory.joinpath("pyproject.toml").write_bytes(embedded.pyproject)
    directory.joinpath("uv.lock").write_bytes(embedded.lock)

    sync_dependencies(directory, request, env)

    # after the sync, as uv sync is exact and would remove the package again
    uv_tool(
        "pip",
        "install",
        "--python",
        str(venv),
        "--no-deps",
        "--only-binary",
        request.name,
        request.requirement,
        env=env,
    )
    uv_tool("pip", "check", "--python", str(venv), env=env)

    Receipt(
        package=request.name,
        version=str(request.version),
        uv_lock_sha256=hashlib.sha256(embedded.lock).hexdigest(),
        pyproject_sha256=hashlib.sha256(embedded.pyproject).hexdigest(),
        extras=request.extras,
        python=request.python,
        uv_version=get_uv_version(env),
        created=datetime.now(UTC).isoformat(timespec="seconds"),
    ).write(directory)


def ensure_environment(request: ToolRequest, refresh: bool) -> Path:
    """Get the directory of a complete environment, build it if necessary"""
    directory = tool_directory(request)
    if not refresh and has_receipt(directory, request):
        return directory

    directory.parent.mkdir(parents=True, exist_ok=True)
    # the lock is next to the directory, as the directory is deleted on failure
    with FileLock(directory.parent / f"{directory.name}.lock"):
        if not refresh and has_receipt(directory, request):
            # built by a parallel run while waiting for the lock
            return directory
        shutil.rmtree(directory, ignore_errors=True)
        try:
            build_environment(directory, request)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
    return directory


def script_path(venv: Path, command: str, windows: bool = os.name == "nt") -> Path:
    """Path of a console script in a venv

    >>> script_path(Path("venv"), "tool", windows=False).as_posix()
    'venv/bin/tool'
    >>> script_path(Path("venv"), "tool", windows=True).as_posix()
    'venv/Scripts/tool.exe'
    """
    if windows:
        return venv / "Scripts" / f"{command}.exe"
    return venv / "bin" / command


def console_scripts(venv: Path, name: NormalizedName) -> tuple[str, ...]:
    """Console scripts from the entry_points.txt of the installed package"""
    site_packages = (
        *venv.glob("lib/*/site-packages"),
        *venv.glob("Lib/site-packages"),
    )
    for dist in metadata.distributions(path=[str(p) for p in site_packages]):
        if canonicalize_name(dist.metadata["Name"]) == name:
            return tuple(
                sorted(
                    ep.name for ep in dist.entry_points.select(group="console_scripts")
                )
            )
    return ()


def run_tool(
    command: str,
    args: Sequence[str],
    from_spec: str | None,
    extras: Sequence[str],
    python: str | None,
    refresh: bool,
) -> int:
    """Install the tool with its locked dependencies and run it.

    Returns:
        Exit code of the command
    """
    if from_spec is None:
        spec = parse_spec(command)
        command = spec.requested_name
    else:
        spec = parse_spec(from_spec)
    if not command or Path(command).name != command or "\\" in command:
        raise ToolError(
            f"COMMAND must be the name of a console script, not '{command}'"
        )

    if spec.version is None:
        version = resolve_version(spec.name, python)
    else:
        version = spec.version
    request = ToolRequest(spec.name, version, normalize_extras(extras), python)

    venv = ensure_environment(request, refresh) / ".venv"
    executable = script_path(venv, command)
    if not executable.is_file():
        scripts = console_scripts(venv, request.name)
        raise ToolError(
            f"'{command}' is not a console script of {request.requirement}. "
            f"Available: {', '.join(scripts) or 'none'}"
        )

    cmd = (str(executable), *args)
    show_cmd(cmd)
    return subprocess.run(cmd, check=False).returncode
