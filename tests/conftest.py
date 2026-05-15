import base64
import gzip
import json
from pathlib import Path

import pytest

from helmadm.env import ENV_NAMESPACE, ENV_RELEASE_NAME, ENV_TRACE_VALUES

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def mock_remote_chart_values_for_cli(monkeypatch, request):
    """Avoid network during CLI tests; individual tests may override."""
    if request.module.__name__ == "tests.test_chart_values":
        return

    monkeypatch.setattr(
        "helmadm.cli.fetch_remote_chart_values",
        lambda _repo_url, _chart, _version: {},
    )


@pytest.fixture(autouse=True)
def stub_cli_kubernetes_access_check(request, monkeypatch):
    """CLI tests mock the client; avoid calling a real API server for connectivity."""
    mod = request.module.__name__
    if mod not in ("tests.test_cli", "tests.test_logging"):
        return
    if request.node.get_closest_marker("no_stub_k8s_access"):
        return
    monkeypatch.setattr(
        "helmadm.cli.check_kubernetes_accessible",
        lambda _api: None,
    )


@pytest.fixture
def clean_env(monkeypatch):
    for key in (
        ENV_NAMESPACE,
        "KUBECONFIG",
        "HELM_TO_ARGOCD_CONTEXT",
        "HELM_TO_ARGOCD_REPO_URL",
        ENV_RELEASE_NAME,
        ENV_TRACE_VALUES,
        "HELM_TO_ARGOCD_K8S_CONNECT_TIMEOUT",
        "HELM_TO_ARGOCD_K8S_READ_TIMEOUT",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def sample_release() -> dict:
    return {
        "name": "prometheus",
        "namespace": "monitoring",
        "config": {
            "server": {"retention": "30d"},
            "ingress": {"enabled": True},
        },
        "chart": {
            "metadata": {
                "name": "prometheus",
                "version": "25.0.0",
                "repoURL": "https://prometheus-community.github.io/helm-charts",
            },
            "values": {
                "server": {"retention": "15d"},
                "ingress": {"enabled": False},
            },
        },
        "manifest": (
            "---\n"
            "apiVersion: v1\n"
            "kind: ConfigMap\n"
            "metadata:\n"
            "  name: drift-cm\n"
            "  namespace: monitoring\n"
            "data:\n"
            "  key: hello\n"
        ),
    }


@pytest.fixture
def encoded_release_data(sample_release: dict) -> str:
    payload = json.dumps(sample_release).encode()
    compressed = gzip.compress(payload)
    once = base64.b64encode(compressed).decode()
    twice = base64.b64encode(once.encode()).decode()
    return twice
