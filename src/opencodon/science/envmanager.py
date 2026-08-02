"""Durable environments via micromamba.

``execution_log.env_snapshot`` records what happened to be installed when a
cell ran. That is evidence, not a recipe — you cannot hand it to someone and
get the same environment back, which is why ``reproduce()`` has never been
able to claim more than "the bytes matched".

This module supplies the missing half: a named environment that survives the
session, and a **recreatable identity** for it. ``micromamba env export
--explicit`` emits the exact package URLs with hashes, so two environments
with the same lock hash are the same environment in the sense that matters.

micromamba rather than conda or a venv, for three reasons:

- one static binary, no base installation, no shell hooks
- conda-forge and bioconda, which is where the bioinformatics tooling lives
  and where a Python-only manager cannot reach
- R, CLI tools and compiled dependencies are ordinary packages here

The binary is fetched on first use into the opencodon home, the same
lazy-dependency posture the kernel stack takes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MICROMAMBA_RELEASE = (
    "https://github.com/mamba-org/micromamba-releases/releases/latest/download"
)
DEFAULT_CHANNELS = ("conda-forge", "bioconda")
CREATE_TIMEOUT_S = 1800
COMMAND_TIMEOUT_S = 600
DOWNLOAD_TIMEOUT_S = 300

# Environments a kernel can be started in must have ipykernel; adding it at
# creation avoids a second solve later.
KERNEL_PACKAGES = ("ipykernel",)


class EnvError(RuntimeError):
    """A named, model-facing environment failure."""


@dataclass(frozen=True)
class EnvSpec:
    """A durable environment and the identity that makes it recreatable."""

    name: str
    prefix: Path
    lock: str
    lock_hash: str

    @property
    def python(self) -> Path:
        return self.prefix / "bin" / "python"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "prefix": str(self.prefix),
            # The identity, not the contents: 64 hex characters that two
            # machines can compare without shipping the lock around.
            "lock_hash": self.lock_hash,
            "packages": self.lock.count("\n"),
        }


def _platform_tag() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        return "osx-arm64" if machine in ("arm64", "aarch64") else "osx-64"
    if system == "linux":
        return "linux-aarch64" if machine in ("arm64", "aarch64") else "linux-64"
    if system == "windows":
        return "win-64"
    raise EnvError(f"micromamba has no build for {system}/{machine}")


def _home() -> Path:
    try:
        from opencodon.common.constants import get_opencodon_home

        return Path(get_opencodon_home())
    except Exception:
        return Path.home() / ".opencodon"


def root_prefix() -> Path:
    """Where durable environments live — outside any session's workspace.

    Session workspaces are swept; an environment that vanished with one would
    not be durable, which is the entire point.
    """
    return _home() / "science-envs"


def micromamba_path() -> Path:
    return root_prefix() / "bin" / "micromamba"


def ensure_micromamba(*, timeout: float = DOWNLOAD_TIMEOUT_S) -> Path:
    """Return a usable micromamba, fetching the static binary if needed."""
    existing = shutil.which("micromamba")
    if existing:
        return Path(existing)

    binary = micromamba_path()
    if binary.exists() and os.access(binary, os.X_OK):
        return binary

    import urllib.request

    url = f"{MICROMAMBA_RELEASE}/micromamba-{_platform_tag()}"
    binary.parent.mkdir(parents=True, exist_ok=True)
    partial = binary.with_suffix(".partial")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            partial.write_bytes(response.read())
        partial.chmod(0o755)
        # Rename only once it is complete and executable, so an interrupted
        # download never leaves something that looks installed.
        partial.replace(binary)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise EnvError(f"could not fetch micromamba from {url}: {exc}") from exc
    return binary


def _run(args: List[str], *, timeout: float = COMMAND_TIMEOUT_S) -> str:
    binary = ensure_micromamba()
    command = [str(binary), "--root-prefix", str(root_prefix()), *args]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise EnvError(f"micromamba timed out after {timeout:.0f}s: {' '.join(args[:3])}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise EnvError(f"micromamba {' '.join(args[:2])} failed: {detail[-600:]}")
    return result.stdout


def env_prefix(name: str) -> Path:
    return root_prefix() / "envs" / name


def exists(name: str) -> bool:
    return (env_prefix(name) / "conda-meta").is_dir()


def list_envs() -> List[str]:
    envs_root = root_prefix() / "envs"
    if not envs_root.is_dir():
        return []
    return sorted(p.name for p in envs_root.iterdir() if (p / "conda-meta").is_dir())


def export_lock(name: str) -> str:
    """The explicit lock: exact package URLs with hashes.

    This is what makes an environment recreatable rather than merely observed —
    ``micromamba create --file <lock>`` on another machine resolves nothing and
    installs exactly these artifacts.
    """
    if not exists(name):
        raise EnvError(f"environment {name!r} does not exist")
    return _run(["env", "export", "--explicit", "--prefix", str(env_prefix(name))])


def lock_hash(lock: str) -> str:
    """Stable identity for a lock.

    Comments and ordering are stripped first: the same environment exported
    twice must hash the same, and micromamba writes a platform comment line
    that is informative but not part of the identity.
    """
    lines = sorted(
        line.strip()
        for line in lock.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def describe(name: str) -> EnvSpec:
    """The environment's identity, as recorded against a cell."""
    lock = export_lock(name)
    return EnvSpec(
        name=name, prefix=env_prefix(name), lock=lock, lock_hash=lock_hash(lock)
    )


def create(
    name: str,
    packages,
    *,
    channels=DEFAULT_CHANNELS,
    python: str = "3.11",
    with_kernel: bool = True,
    timeout: float = CREATE_TIMEOUT_S,
) -> EnvSpec:
    """Create a durable environment; returns its recreatable identity."""
    if not (name or "").strip():
        raise EnvError("an environment name is required")
    name = name.strip()
    if exists(name):
        raise EnvError(f"environment {name!r} already exists — use install() to add to it")

    requested = list(packages or [])
    if with_kernel:
        requested += [p for p in KERNEL_PACKAGES if p not in requested]

    args = ["create", "--yes", "--prefix", str(env_prefix(name)), f"python={python}"]
    for channel in channels:
        args += ["-c", channel]
    args += requested
    _run(args, timeout=timeout)
    return describe(name)


def install(
    name: str, packages, *, channels=DEFAULT_CHANNELS, timeout: float = CREATE_TIMEOUT_S
) -> EnvSpec:
    """Add packages to an existing environment; the identity changes with it."""
    if not exists(name):
        raise EnvError(f"environment {name!r} does not exist — create it first")
    requested = list(packages or [])
    if not requested:
        raise EnvError("at least one package is required")

    args = ["install", "--yes", "--prefix", str(env_prefix(name))]
    for channel in channels:
        args += ["-c", channel]
    args += requested
    _run(args, timeout=timeout)
    return describe(name)


def remove(name: str) -> None:
    if not exists(name):
        raise EnvError(f"environment {name!r} does not exist")
    _run(["env", "remove", "--yes", "--prefix", str(env_prefix(name))], timeout=600)


def env_snapshot(name: str) -> str:
    """The env_snapshot payload for execution_log.

    Carries the lock itself as well as its hash: the hash answers "is this the
    same environment", the lock answers "then build me one".
    """
    spec = describe(name)
    return json.dumps(
        {
            "manager": "micromamba",
            "env_name": spec.name,
            "lock_hash": spec.lock_hash,
            "lock": spec.lock,
            "platform": _platform_tag(),
        },
        sort_keys=True,
    )


def snapshot_lock_hash(snapshot: Optional[str]) -> Optional[str]:
    """Read the lock hash back out of a recorded snapshot, if it has one.

    Returns None for the observational snapshots that predate this module —
    those record what was installed but cannot recreate it, and must not be
    mistaken for an identity.
    """
    if not snapshot:
        return None
    try:
        payload = json.loads(snapshot)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload.get("lock_hash")
