from pathlib import Path

import pytest

from helmadm.env import (
    ENV_K8S_CONNECT_TIMEOUT,
    ENV_K8S_READ_TIMEOUT,
    ENV_NAMESPACE,
    ENV_RELEASE_NAME,
    get_k8s_request_timeout,
    resolve_namespace,
    resolve_release_name,
)


def test_k8s_request_timeout_defaults(monkeypatch):
    monkeypatch.delenv(ENV_K8S_CONNECT_TIMEOUT, raising=False)
    monkeypatch.delenv(ENV_K8S_READ_TIMEOUT, raising=False)
    assert get_k8s_request_timeout() == (5.0, 60.0)


def test_k8s_request_timeout_from_env(monkeypatch):
    monkeypatch.setenv(ENV_K8S_CONNECT_TIMEOUT, "8")
    monkeypatch.setenv(ENV_K8S_READ_TIMEOUT, "90")
    assert get_k8s_request_timeout() == (8.0, 90.0)
    monkeypatch.setenv(ENV_RELEASE_NAME, "from-env")
    assert resolve_release_name("from-cli") == "from-cli"
    assert resolve_release_name(None) == "from-env"


def test_resolve_namespace_prefers_cli_over_env(monkeypatch):
    monkeypatch.setenv(ENV_NAMESPACE, "from-env")
    assert resolve_namespace("from-cli") == "from-cli"
    assert resolve_namespace(None) == "from-env"


def test_resolve_namespace_from_kubeconfig_context(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text(
        """
apiVersion: v1
kind: Config
clusters:
- name: test
  cluster:
    server: https://127.0.0.1:6443
contexts:
- name: test
  context:
    cluster: test
    user: test
    namespace: monitoring
users:
- name: test
  user:
    token: test
current-context: test
""".strip()
    )
    monkeypatch.delenv(ENV_NAMESPACE, raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)

    assert resolve_namespace(None, kubeconfig) == "monitoring"


def test_resolve_namespace_returns_none_without_sources(monkeypatch):
    monkeypatch.delenv(ENV_NAMESPACE, raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(
        "helmadm.k8s.get_kubeconfig_default_namespace",
        lambda _kubeconfig=None: None,
    )

    assert resolve_namespace(None) is None
