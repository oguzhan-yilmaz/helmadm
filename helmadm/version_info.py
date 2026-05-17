"""Installed version and optional PyPI update checks."""

from __future__ import annotations

import json
import re
import tomllib
import urllib.error
import urllib.request
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_PYPI_JSON_URL = "https://pypi.org/pypi/helmadm/json"
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

UPGRADE_HINT_PIP = "pip install --upgrade helmadm"
UPGRADE_HINT_UV = "uv tool upgrade helmadm"


def get_version() -> str:
    """Return the installed package version, or read pyproject.toml in a checkout."""
    try:
        return version("helmadm")
    except PackageNotFoundError:
        from_pyproject = _read_version_from_pyproject()
        return from_pyproject if from_pyproject is not None else "unknown"


def _read_version_from_pyproject() -> str | None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.is_file():
        return None
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    v = project.get("version")
    return str(v) if isinstance(v, str) and v.strip() else None


def parse_version_tuple(v: str) -> tuple[int, int, int] | None:
    """Parse a leading semver triple; return None if not recognizable."""
    m = _VERSION_RE.match(v.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def fetch_pypi_version(*, timeout: float = 3.0) -> str | None:
    """Return the latest version string from PyPI, or None on failure."""
    req = urllib.request.Request(
        _PYPI_JSON_URL,
        headers={"Accept": "application/json", "User-Agent": "helmadm"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None
    info = payload.get("info") if isinstance(payload, dict) else None
    if not isinstance(info, dict):
        return None
    latest = info.get("version")
    return str(latest) if isinstance(latest, str) and latest.strip() else None


def format_version_lines(*, current: str, latest: str | None = None) -> list[str]:
    """Human-readable lines for `helmadm version`."""
    lines = [f"helmadm {current}"]
    if latest is None:
        return lines
    lines.append(f"latest: {latest}")
    cur_t = parse_version_tuple(current)
    lat_t = parse_version_tuple(latest)
    if cur_t is None or lat_t is None or cur_t >= lat_t:
        return lines
    lines.append(
        "update: a newer release is on PyPI — run "
        f"'{UPGRADE_HINT_PIP}' or '{UPGRADE_HINT_UV}'"
    )
    return lines
