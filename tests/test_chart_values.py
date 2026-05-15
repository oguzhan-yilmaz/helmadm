import io
import tarfile
from unittest.mock import patch

import pytest
import yaml

from helmadm.chart_values import (
    ChartValuesFetchError,
    fetch_remote_chart_values,
)


def _make_chart_tgz(chart_name: str, values: dict) -> bytes:
    payload = yaml.dump(values).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name=f"{chart_name}/values.yaml")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _mock_urlopen(repo_url: str, chart_name: str, remote_values: dict):
    index = {
        "apiVersion": "v1",
        "entries": {
            chart_name: [
                {
                    "version": "1.0.0",
                    "urls": [f"{repo_url.rstrip('/')}/charts/{chart_name}-1.0.0.tgz"],
                }
            ]
        }
    }
    index_bytes = yaml.dump(index).encode()
    chart_bytes = _make_chart_tgz(chart_name, remote_values)

    def fake_urlopen(request, timeout=0):
        url = request.full_url
        if url.endswith("/index.yaml"):
            return _FakeResponse(index_bytes)
        if url.endswith(".tgz"):
            return _FakeResponse(chart_bytes)
        raise ChartValuesFetchError(f"unexpected url {url}")

    return fake_urlopen


def test_fetch_remote_chart_values_from_http_repo():
    remote = {"replicas": 1, "image": {"tag": "1.0.0"}}
    repo_url = "https://charts.example.com"
    with patch(
        "helmadm.chart_values.urllib.request.urlopen",
        side_effect=_mock_urlopen(repo_url, "mychart", remote),
    ):
        values = fetch_remote_chart_values(repo_url, "mychart", "1.0.0")

    assert values == remote


def test_fetch_remote_chart_values_missing_version():
    repo_url = "https://charts.example.com"
    index = {
        "apiVersion": "v1",
        "entries": {"mychart": [{"version": "2.0.0", "urls": ["chart.tgz"]}]},
    }

    def fake_urlopen(request, timeout=0):
        return _FakeResponse(yaml.dump(index).encode())

    with (
        patch(
            "helmadm.chart_values.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ),
        pytest.raises(ChartValuesFetchError, match="1.0.0"),
    ):
        fetch_remote_chart_values(repo_url, "mychart", "1.0.0")
