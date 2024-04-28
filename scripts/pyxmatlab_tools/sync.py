"""Sync tools."""

from datetime import UTC, datetime
from json import dumps, loads
from pathlib import Path
from platform import platform
from re import finditer, search
from shlex import quote, split
from subprocess import run
from sys import version_info

from pyxmatlab_tools.types import Platform, PythonVersion

# ! For local dev config tooling
PYTEST = Path("pytest.ini")
"""Resulting pytest configuration file."""

# ! Dependencies
COPIER_ANSWERS = Path(".copier-answers.yml")
"""Copier answers file."""
PYTHON_VERSIONS_FILE = Path(".python-versions")
"""File containing supported Python versions."""
REQS = Path("requirements")
"""Requirements."""
UV = REQS / "uv.in"
"""UV requirement."""
DEV = REQS / "dev.in"
"""Other development tools and editable local dependencies."""
NODEPS = REQS / "nodeps.in"
"""Dependencies appended to locks without compiling their dependencies."""
OVERRIDE = REQS / "override.txt"
"""Overrides to satisfy otherwise incompatible combinations."""

# ! Platforms and Python versions
SYS_PLATFORM: Platform = platform(terse=True).casefold().split("-")[0]  # pyright: ignore[reportAssignmentType] 1.1.356
"""Platform identifier."""
SYS_PYTHON_VERSION: PythonVersion = ".".join([str(v) for v in version_info[:2]])  # pyright: ignore[reportAssignmentType] 1.1.356
"""Python version associated with this platform."""
PLATFORMS: tuple[Platform, ...] = ("linux", "macos", "windows")
"""Supported platforms."""
PYTHON_VERSIONS: tuple[PythonVersion, ...] = (  # pyright: ignore[reportAssignmentType] 1.1.356
    tuple(PYTHON_VERSIONS_FILE.read_text("utf-8").splitlines())
    if PYTHON_VERSIONS_FILE.exists()
    else ("3.9", "3.10", "3.11", "3.12")
)
"""Supported Python versions."""

# ! Checking
UV_PAT = r"(?m)^# uv\s(?P<version>.+)$"
"""Pattern for stored `uv` version comment."""
SUB_PAT = r"(?m)^# submodules/(?P<name>[^\s]+)\s(?P<rev>[^\s]+)$"
"""Pattern for stored submodule revision comments."""
DEP_PAT = r"(?mi)^(?P<name>[A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])==.+$"
"""Pattern for compiled dependencies.

See: https://packaging.python.org/en/latest/specifications/name-normalization/#name-format
"""


def check_compilation(high: bool = False) -> str:  # noqa: PLR0911
    """Check compilation, re-lock if incompatible, and return the compilation.

    Parameters
    ----------
    high
        Highest dependencies.
    """
    old = get_compilation(SYS_PLATFORM, SYS_PYTHON_VERSION, high)
    if not old:
        return lock(high)  # Old compilation missing
    old_uv = search(UV_PAT, old)
    if not old_uv:
        return lock(high)  # Unknown `uv` version last used to compile
    if old_uv["version"] != get_uv_version():
        return lock(high)  # Older `uv` version last used to compile
    directs = compile(SYS_PLATFORM, SYS_PYTHON_VERSION, high, no_deps=True)
    try:
        subs = dict(
            zip(finditer(SUB_PAT, old), finditer(SUB_PAT, directs), strict=False)
        )
    except ValueError:
        return lock(high)  # Submodule missing
    if any(old_sub.groups() != new_sub.groups() for old_sub, new_sub in subs.items()):
        return lock(high)  # Submodule pinned commit SHA mismatch
    old_directs: list[str] = []
    for direct in finditer(DEP_PAT, directs):
        pat = rf"(?mi)^(?P<name>{direct['name']})==(?P<ver>.+$)"
        if match := search(pat, old):
            old_directs.append(match.group())
            continue
        return lock(high)  # Direct dependency missing
    sys_compilation = compile(SYS_PLATFORM, SYS_PYTHON_VERSION, high)
    if any(d not in sys_compilation for d in old_directs):
        return lock(high, sys_compilation)  # Direct dependency version mismatch
    return old  # The old compilation is compatible


def lock(high: bool, sys_compilation: str = "") -> str:
    """Lock dependencies for all platforms and Python versions."""
    lock_contents: dict[str, str] = {}
    for platform in PLATFORMS:  # noqa: F402
        for python_version in PYTHON_VERSIONS:
            key = get_compilation_key(platform, python_version, high)
            compilation = compile(platform, python_version, high)
            if (
                not sys_compilation
                and platform == SYS_PLATFORM
                and python_version == SYS_PYTHON_VERSION
            ):
                sys_compilation = compilation
            lock_contents[key] = compilation
    get_lockfile(high).write_text(
        encoding="utf-8", data=dumps(indent=2, sort_keys=True, obj=lock_contents) + "\n"
    )
    return sys_compilation


def get_compilation(
    platform: Platform, python_version: PythonVersion, high: bool
) -> str:
    """Get existing dependency compilations.

    Parameters
    ----------
    high
        Highest dependencies.
    platform
        Platform to compile for.
    python_version
        Python version to compile for.
    """
    lockfile = get_lockfile(high)
    if not lockfile.exists():
        return ""
    contents = loads(lockfile.read_text("utf-8"))
    return contents.get(get_compilation_key(platform, python_version, high), "")


def get_compilation_key(
    platform: Platform, python_version: PythonVersion, high: bool
) -> str:
    """Get the name of a dependency compilation.

    Parameters
    ----------
    platform
        Platform to compile for.
    python_version
        Python version to compile for.
    high
        Highest dependencies.
    """
    return "_".join([platform, python_version, *(["high"] if high else [])])


def get_lockfile(high: bool) -> Path:
    """Get lockfile path.

    Parameters
    ----------
    high
        Highest dependencies.
    """
    return Path(f"lock{'-high' if high else ''}.json")


def compile(  # noqa: A001
    platform: Platform, python_version: PythonVersion, high: bool, no_deps: bool = False
) -> str:
    """Compile system dependencies.

    Parameters
    ----------
    high
        Highest dependencies.
    no_deps
        Without transitive dependencies.
    platform
        Platform to compile for.
    python_version
        Python version to compile for.
    """
    sep = " "
    result = run(
        args=split(
            sep.join([
                "bin/uv pip compile",
                f"--exclude-newer {datetime.now(UTC).isoformat().replace('+00:00', 'Z')}",
                f"--python-platform {platform} --python-version {python_version}",
                f"--resolution {'highest' if high else 'lowest-direct'}",
                f"--override {escape(OVERRIDE)}",
                f"--all-extras {'--no-deps' if no_deps else ''}",
                *[
                    escape(path)
                    for path in [
                        DEV,
                        *[
                            Path(editable["path"]) / "pyproject.toml"
                            for editable in finditer(
                                r"(?m)^(?:-e|--editable)\s(?P<path>.+)$",
                                DEV.read_text("utf-8"),
                            )
                        ],
                    ]
                ],
            ])
        ),
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    deps = result.stdout
    submodules = {
        sub.group(): run(
            split(f"git rev-parse HEAD:{sub.group()}"),  # noqa: S603
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        for sub in finditer(r"submodules/.+\b", DEV.read_text("utf-8"))
    }
    return (
        "\n".join([
            f"# uv {get_uv_version()}",
            *[f"# {sub} {rev}" for sub, rev in submodules.items()],
            *[line.strip() for line in deps.splitlines()],
            *[line.strip() for line in NODEPS.read_text("utf-8").splitlines()],
        ])
        + "\n"
    )


def get_uv_version() -> str:
    """Get the installed version of `uv`."""
    result = run(
        args=split("bin/uv --version"), capture_output=True, check=False, text=True
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip().split(" ")[1]


def escape(path: str | Path) -> str:
    """Path escape suitable for all operating systems.

    Parameters
    ----------
    path
        Path to escape.
    """
    return quote(Path(path).as_posix())
