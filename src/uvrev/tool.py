"""Run tools with the dependencies pinned in the uv.lock embedded in their wheel.

Wheels built with the hatchling build hook ``hatch-uv-lock`` contain the
original ``uv.lock`` and ``pyproject.toml`` of the project in
``<name>-<version>.dist-info/extra_metadata/``.
``uvrev tool run`` installs the dependencies exactly as pinned in that lock.

The wheel comes from the package index (``name`` or ``name==version``) or from
a local path or URL. Index releases can not change, so their version identifies
them. Wheels from a path or URL can, so they are identified by their sha256.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from itertools import chain
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import httpx2
from filelock import FileLock
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import (
    InvalidWheelFilename,
    NormalizedName,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import Version
from platformdirs import user_cache_path

from . import constants
from .commands import UvRunner, show_cmd, uv_tool
from .exceptions import SubprocessFailure, ToolError
from .protection import writable
from .settings import Settings
from .terminal import warning


@dataclass(frozen=True)
class WheelLocation:
    """A wheel given as local path or URL"""

    filename: str
    version: Version
    # URL for remote wheels, path for local ones (file:// URLs become paths)
    source: str
    remote: bool
    # expected sha256 from the #sha256=... fragment of an URL
    sha256: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    """Package given by ``--from`` or as COMMAND"""

    name: NormalizedName
    version: Version | None
    requested_name: str
    wheel: WheelLocation | None = None


@dataclass(frozen=True)
class ToolRequest:
    """Fully resolved tool environment request"""

    name: NormalizedName
    version: Version
    extras: tuple[NormalizedName, ...]
    python: str | None
    # local snapshot of a wheel from a path or URL, None for the package index
    wheel: Path | None = None
    wheel_sha256: str | None = None
    # path or URL as given, None for the package index
    origin: str | None = None

    @property
    def requirement(self) -> str:
        return f"{self.name}=={self.version}"

    @property
    def install_source(self) -> str:
        """What uv installs: the requirement or the wheel snapshot"""
        if self.wheel is None:
            return self.requirement
        return str(self.wheel)

    @property
    def python_args(self) -> tuple[str, ...]:
        if self.python is None:
            return ()
        return ("--python", self.python)

    @property
    def key(self) -> str:
        """Cache key: the version and a short hash of the extras, the python request
        and the sha256 of a wheel from a path or URL

        >>> request = ToolRequest(NormalizedName("a"), Version("1.0"), (), None)
        >>> request.key
        '1.0-f8d3418d438a'
        >>> other = ToolRequest(NormalizedName("a"), Version("1.0"), (), "3.13")
        >>> request.key != other.key
        True
        >>> wheel = ToolRequest(
        ...     NormalizedName("a"), Version("1.0"), (), None, wheel_sha256="ab"
        ... )
        >>> request.key != wheel.key
        True
        """
        data: dict[str, object] = {"extras": self.extras, "python": self.python}
        if self.wheel_sha256 is not None:
            data["wheel_sha256"] = self.wheel_sha256
        payload = json.dumps(data)
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
    source: str
    wheel_sha256: str | None
    uv_lock_sha256: str
    pyproject_sha256: str
    extras: tuple[str, ...]
    python: str | None
    uv_version: str
    created: str

    def write(self, directory: Path) -> None:
        tmp = directory / f"{constants.RECEIPT_NAME}.tmp"
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
        tmp.replace(directory / constants.RECEIPT_NAME)


def is_url_or_path(spec: str) -> bool:
    """True if the spec is not a requirement but a URL or path

    >>> [is_url_or_path(s) for s in ("tool==1", "./x", "x.whl", "https://x")]
    [False, True, True, True]
    """
    return (
        "://" in spec
        or "/" in spec
        or "\\" in spec
        or spec.startswith((".", "~"))
        or spec.lower().endswith(constants.ARCHIVE_SUFFIXES)
    )


def parse_hash_fragment(fragment: str) -> str | None:
    """Expected sha256 from an URL fragment like ``sha256=<hex>`` (PEP 503)

    Examples:
        >>> parse_hash_fragment("")
        >>> parse_hash_fragment("sha256=" + "A" * 64) == "a" * 64
        True
        >>> parse_hash_fragment("md5=abc")
        Traceback (most recent call last):
        ...
        uvrev.exceptions.ToolError: Unsupported hash in the URL fragment '#md5=abc': only #sha256=<hex> is supported
    """
    if not fragment:
        return None
    algorithm, _, digest = fragment.partition("=")
    digest = digest.lower()
    if (
        algorithm != "sha256"
        or len(digest) != constants.SHA256_HEX_LENGTH
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ToolError(
            f"Unsupported hash in the URL fragment '#{fragment}': "
            "only #sha256=<hex> is supported"
        )
    return digest


def parse_wheel_location(location: str) -> WheelLocation:
    """Parse the path or URL of a wheel.

    Examples:
        >>> parse_wheel_location("https://host/My_Tool-1.0-py3-none-any.whl?x=1")
        WheelLocation(filename='My_Tool-1.0-py3-none-any.whl', version=<Version('1.0')>, source='https://host/My_Tool-1.0-py3-none-any.whl?x=1', remote=True, sha256=None)
        >>> parse_wheel_location("file:///wheels/tool-2.0-py3-none-any.whl").source
        '/wheels/tool-2.0-py3-none-any.whl'
        >>> parse_wheel_location("dist/tool-2.0-py3-none-any.whl").remote
        False
    """
    parts = urlsplit(location)
    scheme = parts.scheme.lower()
    sha256: str | None = None
    if scheme in ("http", "https"):
        filename = unquote(PurePosixPath(parts.path).name)
        source = parts._replace(fragment="").geturl()
        sha256 = parse_hash_fragment(parts.fragment)
    elif scheme == "file":
        source = url2pathname(unquote(parts.path))
        filename = Path(source).name
        sha256 = parse_hash_fragment(parts.fragment)
    elif "://" in location:
        raise ToolError(
            f"Unsupported URL '{location}': only http(s) and file URLs of wheels "
            "are supported, VCS URLs would need a build of the source tree"
        )
    else:
        source = location
        filename = Path(location).name
    if not filename.endswith(".whl"):
        raise ToolError(
            f"'{location}' is not a wheel (.whl). sdists and source directories "
            "would need a build of the source tree. To run a tool from its source "
            "tree with its locked dependencies, use 'uv sync --locked' there."
        )
    try:
        _, version, _, _ = parse_wheel_filename(filename)
    except InvalidWheelFilename as e:
        raise ToolError(f"Invalid wheel filename '{filename}': {e}") from None
    return WheelLocation(
        filename, version, source, remote=scheme in ("http", "https"), sha256=sha256
    )


def parse_spec(spec: str) -> ToolSpec:
    """Parse ``name``, ``name==version``, ``name @ URL`` or the path or URL of a wheel

    Examples:
        >>> parse_spec("My_Tool")
        ToolSpec(name='my-tool', version=None, requested_name='My_Tool', wheel=None)
        >>> parse_spec("tool==1.2").version
        <Version('1.2')>
        >>> parse_spec("./dist/My_Tool-1.2-py3-none-any.whl").requested_name
        'my-tool'
        >>> parse_spec("tool[a,b]")
        Traceback (most recent call last):
        ...
        uvrev.exceptions.ToolError: Extras in brackets are not supported ('tool[a,b]'), use --extra a --extra b
        >>> parse_spec("tool>=1")
        Traceback (most recent call last):
        ...
        uvrev.exceptions.ToolError: Unsupported package specification 'tool>=1'. Give 'name', 'name==version' or the path or URL of a wheel
    """
    try:
        requirement = Requirement(spec)
    except InvalidRequirement as e:
        # paths and URLs are no valid requirements, "name @ URL" is
        if "@" in spec or not is_url_or_path(spec):
            raise ToolError(
                f"Invalid package specification '{spec}': {e}. {constants.SUPPORTED_SPEC}"
            ) from None
        wheel = parse_wheel_location(spec)
        name = canonicalize_name(parse_wheel_filename(wheel.filename)[0])
        return ToolSpec(name, wheel.version, name, wheel)
    if requirement.url is None and is_url_or_path(spec):
        # a wheel filename like tool-1.0-py3-none-any.whl is a valid name
        wheel = parse_wheel_location(spec)
        name = canonicalize_name(parse_wheel_filename(wheel.filename)[0])
        return ToolSpec(name, wheel.version, name, wheel)
    if requirement.extras:
        options = " ".join(f"--extra {e}" for e in sorted(requirement.extras))
        raise ToolError(
            f"Extras in brackets are not supported ('{spec}'), use {options}"
        )
    name = canonicalize_name(requirement.name)
    specifiers = tuple(requirement.specifier)

    if requirement.url is not None and requirement.marker is None:
        wheel = parse_wheel_location(requirement.url)
        wheel_name = canonicalize_name(parse_wheel_filename(wheel.filename)[0])
        if wheel_name != name:
            raise ToolError(
                f"The wheel {wheel.filename} is '{wheel_name}', not '{name}'"
            )
        return ToolSpec(name, wheel.version, requirement.name, wheel)

    if not specifiers and requirement.marker is None:
        return ToolSpec(name, None, requirement.name)
    if (
        len(specifiers) != 1
        or requirement.marker is not None
        or specifiers[0].operator != "=="
        or specifiers[0].version.endswith("*")
    ):
        raise ToolError(
            f"Unsupported package specification '{spec}'. {constants.SUPPORTED_SPEC}"
        )
    return ToolSpec(name, Version(specifiers[0].version), requirement.name)


def refuse_insecure_redirect(request: httpx2.Request) -> None:
    """Request hook: an https download must not be redirected to http"""
    if request.url.scheme != "https":
        raise ToolError(f"Refusing the redirect from https to insecure {request.url}")


def download(
    url: str, target: Path, transport: httpx2.BaseTransport | None = None
) -> None:
    """Download with the trust store of the OS, timeouts and safe redirects.

    Args:
        transport: Only for tests
    """
    hooks: dict[str, list[Any]] = {}
    if urlsplit(url).scheme == "https":
        hooks["request"] = [refuse_insecure_redirect]
    try:
        with (
            httpx2.Client(
                follow_redirects=True,
                timeout=constants.DOWNLOAD_TIMEOUT,
                event_hooks=hooks,
                transport=transport,
            ) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            with target.open("wb") as file:
                for chunk in response.iter_bytes():
                    file.write(chunk)
    except httpx2.HTTPError as e:
        raise ToolError(f"Can not download the wheel {url}: {e}") from None


type Downloader = Callable[[str, Path], None]


def fetch_wheel(
    wheel: WheelLocation, directory: Path, download_file: Downloader = download
) -> Path:
    """Copy or download the wheel into the directory.

    The snapshot is hashed and installed, so a wheel that is rebuilt or replaced
    meanwhile can not lead to an environment that does not match the hash.
    """
    target = directory / wheel.filename
    if wheel.remote:
        if urlsplit(wheel.source).scheme == "http" and wheel.sha256 is None:
            warning(
                f"{wheel.source} is downloaded over plain http without "
                "#sha256=<hex> in the URL, so its integrity is not verified"
            )
        show_cmd(("download", wheel.source))
        download_file(wheel.source, target)
    else:
        try:
            shutil.copyfile(Path(wheel.source).expanduser(), target)
        except OSError as e:
            raise ToolError(f"Can not read the wheel {wheel.source}: {e}") from None
    return target


def tool_request(
    spec: ToolSpec,
    extras: Sequence[str],
    python: str | None,
    directory: Path,
    uv: UvRunner = uv_tool,
    download_file: Downloader = download,
) -> ToolRequest:
    """Resolve the version or fetch the wheel (into the directory)"""
    normalized_extras = normalize_extras(extras)
    if spec.wheel is not None:
        wheel = fetch_wheel(spec.wheel, directory, download_file)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if spec.wheel.sha256 is not None and digest != spec.wheel.sha256:
            raise ToolError(
                f"The sha256 of {spec.wheel.source} is {digest}, "
                f"but {spec.wheel.sha256} was expected"
            )
        return ToolRequest(
            spec.name,
            spec.wheel.version,
            normalized_extras,
            python,
            wheel=wheel,
            wheel_sha256=digest,
            origin=spec.wheel.source,
        )
    if spec.version is None:
        version = resolve_version(spec.name, python, uv)
    else:
        version = spec.version
    return ToolRequest(spec.name, version, normalized_extras, python)


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
    env = {k: v for k, v in os.environ.items() if k not in constants.IGNORED_VARIABLES}
    if venv is not None:
        env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    return MappingProxyType(env)


def resolve_version(
    name: NormalizedName, python: str | None, uv: UvRunner = uv_tool
) -> Version:
    """Resolve the latest version with uv's index configuration and credentials"""
    python_args: tuple[str, ...] = ()
    if python is not None:
        python_args = ("--python", python)
    output = uv(
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
    folder = dist_info / constants.EXTRA_METADATA
    missing = tuple(f for f in constants.EMBEDDED_FILES if not (folder / f).is_file())
    if missing:
        files = ", ".join(f"{constants.EXTRA_METADATA}/{f}" for f in missing)
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

    dynamic = sorted(
        set(project.get("dynamic", ())) & set(constants.DYNAMIC_NEEDS_SOURCE)
    )
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


def get_uv_version(env: Mapping[str, str], uv: UvRunner = uv_tool) -> str:
    """Version of uv

    >>> get_uv_version(os.environ).count(".") >= 1
    True
    """
    output = uv("--version", env=env, capture_stdout=True)
    return output.strip().removeprefix("uv ").split(" ")[0]


def has_receipt(directory: Path, request: ToolRequest) -> bool:
    """True if the environment in the directory is complete"""
    try:
        data = json.loads((directory / constants.RECEIPT_NAME).read_text())
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and data.get("package") == request.name
        and data.get("version") == str(request.version)
        and data.get("wheel_sha256") == request.wheel_sha256
    )


def sync_dependencies(
    directory: Path,
    request: ToolRequest,
    env: Mapping[str, str],
    uv: UvRunner = uv_tool,
) -> None:
    """Install the locked dependencies without the project itself"""
    extra_args = tuple(chain.from_iterable(("--extra", e) for e in request.extras))
    try:
        uv(
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


def build_environment(
    directory: Path, request: ToolRequest, settings: Settings, uv: UvRunner = uv_tool
) -> None:
    """Build the environment in place (venvs are not relocatable)"""
    directory.mkdir(parents=True)
    venv = directory / ".venv"
    env = MappingProxyType(
        {
            **uv_environment(venv),
            # a protected venv can not cache bytecode later
            "UV_COMPILE_BYTECODE": str(settings.compile_bytecode).lower(),
        }
    )

    with tempfile.TemporaryDirectory(prefix="uvrev-tool-") as target:
        uv(
            "pip",
            "install",
            "--no-deps",
            "--only-binary",
            request.name,
            "--target",
            target,
            *request.python_args,
            request.install_source,
            env=env,
        )
        embedded = read_embedded(find_dist_info(Path(target), request.name))

    check_embedded(embedded, request.name, request.extras)
    directory.joinpath("pyproject.toml").write_bytes(embedded.pyproject)
    directory.joinpath("uv.lock").write_bytes(embedded.lock)

    sync_dependencies(directory, request, env, uv)

    # after the sync, as uv sync is exact and would remove the package again
    uv(
        "pip",
        "install",
        "--python",
        str(venv),
        "--no-deps",
        "--only-binary",
        request.name,
        request.install_source,
        env=env,
    )
    uv("pip", "check", "--python", str(venv), env=env)

    Receipt(
        package=request.name,
        version=str(request.version),
        source=request.origin or request.requirement,
        wheel_sha256=request.wheel_sha256,
        uv_lock_sha256=hashlib.sha256(embedded.lock).hexdigest(),
        pyproject_sha256=hashlib.sha256(embedded.pyproject).hexdigest(),
        extras=request.extras,
        python=request.python,
        uv_version=get_uv_version(env, uv),
        created=datetime.now(UTC).isoformat(timespec="seconds"),
    ).write(directory)


type Builder = Callable[[Path, ToolRequest, Settings, UvRunner], None]
type LockFactory = Callable[[Path], AbstractContextManager[object]]


@dataclass(frozen=True)
class ToolServices:
    """External services of ``uvrev tool run``, tests can replace them"""

    uv: UvRunner = uv_tool
    download: Downloader = download
    build: Builder = build_environment
    # lock of an environment, so parallel runs don't build it twice
    lock: LockFactory = FileLock


def ensure_environment(
    request: ToolRequest,
    refresh: bool,
    settings: Settings,
    services: ToolServices | None = None,
) -> Path:
    """Get the directory of a complete environment, build it if necessary.

    The venv of the tool is protected like the venvs of the environments.
    """
    services = services or ToolServices()
    directory = tool_directory(request)
    if not refresh and has_receipt(directory, request):
        return directory

    directory.parent.mkdir(parents=True, exist_ok=True)
    # the lock is next to the directory, as the directory is deleted on failure
    with services.lock(directory.parent / f"{directory.name}.lock"):
        if not refresh and has_receipt(directory, request):
            # built by a parallel run while waiting for the lock
            return directory
        # a protected venv can only be deleted when it is writable
        with writable(directory / ".venv", settings):
            shutil.rmtree(directory, ignore_errors=True)
            try:
                services.build(directory, request, settings, services.uv)
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
    settings: Settings,
    services: ToolServices | None = None,
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

    services = services or ToolServices()
    with tempfile.TemporaryDirectory(prefix="uvrev-wheel-") as directory:
        request = tool_request(
            spec, extras, python, Path(directory), services.uv, services.download
        )
        venv = ensure_environment(request, refresh, settings, services) / ".venv"
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
