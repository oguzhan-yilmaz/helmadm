"""Installed version and optional PyPI update checks."""

from __future__ import annotations

import json
import re
import tomllib
import urllib.error
import urllib.request
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal

import yaml

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


def is_update_available(current: str, latest: str) -> bool:
    cur_t = parse_version_tuple(current)
    lat_t = parse_version_tuple(latest)
    if cur_t is None or lat_t is None:
        return False
    return cur_t < lat_t


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


def build_version_info(
    *,
    current: str,
    latest: str | None = None,
    pypi_checked: bool = False,
) -> dict[str, Any]:
    """Structured version document for YAML (and other machine-readable output)."""
    doc: dict[str, Any] = {"name": "helmadm", "version": current}
    if not pypi_checked:
        return doc
    pypi: dict[str, Any] = {"latest": latest}
    if latest is not None:
        pypi["update_available"] = is_update_available(current, latest)
    doc["pypi"] = pypi
    if latest is not None and pypi.get("update_available"):
        doc["upgrade"] = {"pip": UPGRADE_HINT_PIP, "uv": UPGRADE_HINT_UV}
    return doc


def format_version_yaml(
    *,
    current: str,
    latest: str | None = None,
    pypi_checked: bool = False,
) -> str:
    doc = build_version_info(
        current=current, latest=latest, pypi_checked=pypi_checked
    )
    return yaml.safe_dump(
        doc,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def format_version_lines(*, current: str, latest: str | None = None) -> list[str]:
    """Human-readable lines for `helmadm version --output text`."""
    lines = [f"helmadm {current}"]
    if latest is None:
        return lines
    lines.append(f"latest: {latest}")
    if not is_update_available(current, latest):
        return lines
    lines.append(
        "update: a newer release is on PyPI — run "
        f"'{UPGRADE_HINT_PIP}' or '{UPGRADE_HINT_UV}'"
    )
    return lines


VersionOutputFormat = Literal["yaml", "text"]


def format_version(
    *,
    current: str,
    latest: str | None = None,
    pypi_checked: bool = False,
    output: VersionOutputFormat = "yaml",
) -> str:
    if output == "text":
        return "\n".join(
            format_version_lines(current=current, latest=latest if pypi_checked else None)
        )
    return format_version_yaml(
        current=current, latest=latest, pypi_checked=pypi_checked
    )
