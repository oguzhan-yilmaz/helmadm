"""Tests for version and PyPI update hints."""

from __future__ import annotations

import json
from unittest.mock import patch

from helmadm.version_info import (
    UPGRADE_HINT_PIP,
    UPGRADE_HINT_UV,
    fetch_pypi_version,
    format_version_lines,
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


def test_format_version_lines_without_pypi() -> None:
    assert format_version_lines(current="0.2.0") == ["helmadm 0.2.0"]


def test_format_version_lines_shows_both_upgrade_hints() -> None:
    lines = format_version_lines(current="0.1.0", latest="0.2.0")
    assert lines[0] == "helmadm 0.1.0"
    assert lines[1] == "latest: 0.2.0"
    assert UPGRADE_HINT_PIP in lines[2]
    assert UPGRADE_HINT_UV in lines[2]


def test_format_version_lines_no_hint_when_current() -> None:
    lines = format_version_lines(current="0.2.0", latest="0.2.0")
    assert lines == ["helmadm 0.2.0", "latest: 0.2.0"]


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
