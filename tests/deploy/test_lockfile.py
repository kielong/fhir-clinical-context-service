# ORIGIN: H-spec — Kiel's decision: pin what the container installs, because pyproject.toml allows
#   ranges and two builds a month apart could otherwise ship different versions of the same code.
#   Test cases typed by Claude Code.
"""The lockfile: the container installs exactly the versions that were tested.

`pyproject.toml` says which versions are acceptable; `requirements.lock` says which ones are used.
These tests keep the two from drifting apart and keep the Dockerfile honest about using the lock.
They read files only; nothing is installed or downloaded.
"""

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).parent.parent.parent
LOCK = ROOT / "requirements.lock"
DOCKERFILE = ROOT / "Dockerfile"

DEV_TOOLS = {"pytest", "ruff", "mypy", "respx", "httpx2", "pip-tools"}


def _runtime_requirements() -> list[Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    return [Requirement(line) for line in project["dependencies"]]


def _pins() -> dict[str, str]:
    """name -> version for every requirement line in the lock (comments and blanks skipped)."""
    pins = {}
    for line in LOCK.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line and not line.startswith("-"):
            name, _, version = line.partition("==")
            pins[name.lower().replace("_", "-")] = version
    return pins


def test_every_line_of_the_lock_is_one_exact_version():
    for line in LOCK.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line and not line.startswith("-"):
            assert re.fullmatch(r"[A-Za-z0-9._\-\[\]]+==[0-9][A-Za-z0-9.+!]*", line), line


def test_every_runtime_dependency_is_locked_to_a_version_pyproject_allows():
    pins = _pins()
    for requirement in _runtime_requirements():
        name = requirement.name.lower().replace("_", "-")
        assert name in pins, f"{name} is in pyproject.toml but not locked"
        assert requirement.specifier.contains(pins[name]), (
            f"{name}=={pins[name]} does not satisfy {requirement.specifier}: "
            "the lock is stale, run `make lock`"
        )


def test_the_lock_holds_what_the_container_runs_and_none_of_the_dev_tools():
    pins = _pins()
    assert {"fastapi", "uvicorn", "pydantic", "pydantic-settings", "httpx"} <= set(pins)
    assert not DEV_TOOLS & set(pins)


def test_the_container_installs_from_the_lock_before_it_copies_the_code():
    lines = [line.strip() for line in DOCKERFILE.read_text().splitlines()]
    install_lock = next(
        i for i, line in enumerate(lines) if "pip install -r requirements.lock" in line
    )
    copy_lock = next(
        i for i, line in enumerate(lines) if line.startswith("COPY") and "requirements.lock" in line
    )
    copy_src = next(i for i, line in enumerate(lines) if line.startswith("COPY src"))
    install_package = next(
        i for i, line in enumerate(lines) if line.startswith("RUN pip install --no-deps")
    )

    # The lock is copied and installed first, so the layer with the dependencies is rebuilt only
    # when the lock changes, not on every edit to the code.
    assert copy_lock < install_lock < copy_src < install_package


def test_the_container_never_resolves_dependencies_again_when_installing_the_package():
    text = DOCKERFILE.read_text()
    assert "pip install --no-deps ." in text
    assert "pip install ." not in text.replace("pip install --no-deps .", "")


def test_the_lock_is_not_kept_out_of_the_image_or_the_repository():
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert not any("lock" in line for line in ignored if not line.startswith("#"))
    gitignore = (ROOT / ".gitignore").read_text().splitlines()
    assert not any("lock" in line for line in gitignore if not line.startswith("#"))
