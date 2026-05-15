import base64
import gzip
import json
from unittest.mock import MagicMock

import pytest

from helmadm.helm_release import (
    HelmReleaseDecodeError,
    _latest_release_secrets,
    list_releases,
)
from kubernetes.client import V1ObjectMeta, V1Secret, V1SecretList


def _encode_release(payload: dict) -> str:
    compressed = gzip.compress(json.dumps(payload).encode())
    once = base64.b64encode(compressed).decode()
    return base64.b64encode(once.encode()).decode()


def _make_secret(
    namespace: str,
    name: str,
    release_name: str,
    revision: int,
    *,
    release_payload: dict | None = None,
    status: str = "deployed",
) -> V1Secret:
    payload = release_payload or {
        "name": release_name,
        "config": {},
        "chart": {
            "metadata": {
                "name": "test-chart",
                "version": "1.0.0",
                "repoURL": "https://charts.example.com",
            },
            "values": {},
        },
    }
    return V1Secret(
        metadata=V1ObjectMeta(
            namespace=namespace,
            name=f"sh.helm.release.v1.{release_name}.v{revision}",
            labels={
                "owner": "helm",
                "name": release_name,
                "version": str(revision),
                "status": status,
            },
        ),
        type="helm.sh/release.v1",
        data={"release": _encode_release(payload)},
    )


def test_latest_release_secrets_keeps_highest_revision():
    secrets = [
        _make_secret("monitoring", "sh.helm.release.v1.app.v1", "app", 1),
        _make_secret("monitoring", "sh.helm.release.v1.app.v2", "app", 2),
    ]
    latest = _latest_release_secrets(secrets)
    assert len(latest) == 1
    assert latest[0].metadata.labels["version"] == "2"


def test_list_releases_basic_without_decode_detail(monkeypatch):
    api = MagicMock()
    api.list_namespaced_secret.return_value = V1SecretList(
        items=[
            _make_secret("monitoring", "sh.helm.release.v1.prom.v1", "prom", 1),
        ]
    )

    with monkeypatch.context() as patcher:
        patcher.setattr(
            "helmadm.helm_release.decode_release_data",
            MagicMock(side_effect=AssertionError("should not decode in basic mode")),
        )
        releases = list_releases(api, "monitoring", detail=False)

    assert len(releases) == 1
    assert releases[0].name == "prom"
    assert releases[0].revision == 1
    assert releases[0].chart_name is None


def test_list_releases_detail_sets_needs_repo_url():
    api = MagicMock()
    api.list_namespaced_secret.return_value = V1SecretList(
        items=[
            _make_secret(
                "monitoring",
                "sh.helm.release.v1.with-repo.v1",
                "with-repo",
                1,
                release_payload={
                    "name": "with-repo",
                    "config": {},
                    "chart": {
                        "metadata": {
                            "name": "chart-a",
                            "version": "2.0.0",
                            "repoURL": "https://charts.example.com",
                        },
                        "values": {},
                    },
                },
            ),
            _make_secret(
                "monitoring",
                "sh.helm.release.v1.without-repo.v1",
                "without-repo",
                1,
                release_payload={
                    "name": "without-repo",
                    "config": {},
                    "chart": {
                        "metadata": {"name": "chart-b", "version": "1.0.0"},
                        "values": {},
                    },
                },
            ),
        ]
    )

    releases = list_releases(api, "monitoring", detail=True)
    by_name = {item.name: item for item in releases}

    assert by_name["with-repo"].needs_repo_url is False
    assert by_name["with-repo"].repo_url == "https://charts.example.com"
    assert by_name["without-repo"].needs_repo_url is True
    assert by_name["without-repo"].repo_url is None


def test_list_releases_decode_error_propagates():
    api = MagicMock()
    bad = _make_secret("monitoring", "sh.helm.release.v1.bad.v1", "bad", 1)
    bad.data = {"release": "not-valid"}
    api.list_namespaced_secret.return_value = V1SecretList(items=[bad])

    with pytest.raises(HelmReleaseDecodeError):
        list_releases(api, "monitoring", detail=True)
