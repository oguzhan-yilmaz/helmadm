"""Tests for version and PyPI update hints."""

from __future__ import annotations

import json
from unittest.mock import patch

import yaml

from helmadm.version_info import (
    UPGRADE_HINT_PIP,
    UPGRADE_HINT_UV,
    build_version_info,
    fetch_pypi_version,
    format_version,
    format_version_lines,
    format_version_yaml,
    get_version,
    parse_version_tuple,
)


def test_get_version_reads_pyproject_when_not_installed() -> None:
    v = get_version()
    assert v == "0.2.0"


def test_parse_version_tuple() -> None:
    assert parse_version_tuple("0.2.0") == (0, 2, 0)
    assert parse_version_tuple("1.10.3rc1") == (1, 10, 3)
    assert parse_version_tuple("bad") is None


def test_build_version_info_minimal() -> None:
    doc = build_version_info(current="0.2.0")
    assert doc == {"name": "helmadm", "version": "0.2.0"}


def test_build_version_info_with_update() -> None:
    doc = build_version_info(
        current="0.1.0", latest="0.2.0", pypi_checked=True
    )
    assert doc["pypi"]["latest"] == "0.2.0"
    assert doc["pypi"]["update_available"] is True
    assert doc["upgrade"]["pip"] == UPGRADE_HINT_PIP
    assert doc["upgrade"]["uv"] == UPGRADE_HINT_UV


def test_format_version_yaml() -> None:
    out = format_version_yaml(current="0.2.0", latest="0.2.0", pypi_checked=True)
    doc = yaml.safe_load(out)
    assert doc["name"] == "helmadm"
    assert doc["version"] == "0.2.0"
    assert doc["pypi"]["latest"] == "0.2.0"
    assert doc["pypi"]["update_available"] is False
    assert "upgrade" not in doc


def test_format_version_yaml_update_available() -> None:
    out = format_version_yaml(current="0.1.0", latest="0.2.0", pypi_checked=True)
    doc = yaml.safe_load(out)
    assert doc["pypi"]["update_available"] is True
    assert doc["upgrade"]["uv"] == UPGRADE_HINT_UV


def test_format_version_text_output() -> None:
    out = format_version(
        current="0.2.0",
        latest="0.2.0",
        pypi_checked=True,
        output="text",
    )
    assert out == "helmadm 0.2.0\nlatest: 0.2.0"


def test_format_version_lines_shows_both_upgrade_hints() -> None:
    lines = format_version_lines(current="0.1.0", latest="0.2.0")
    assert UPGRADE_HINT_PIP in lines[2]
    assert UPGRADE_HINT_UV in lines[2]


def test_fetch_pypi_version_parses_json() -> None:
    payload = json.dumps({"info": {"version": "0.2.0"}}).encode()

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return None

        def read(self):
            return payload

    with patch("helmadm.version_info.urllib.request.urlopen", return_value=_Resp()):
        assert fetch_pypi_version() == "0.2.0"
