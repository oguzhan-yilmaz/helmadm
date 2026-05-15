"""Tests for Kubernetes client helpers and connectivity checks."""

from unittest.mock import MagicMock, patch

import pytest
from kubernetes import config
from kubernetes.client.rest import ApiException
from urllib3.exceptions import ConnectTimeoutError

from helmadm.k8s import (
    KubernetesApiError,
    _api_exception_user_message,
    check_kubernetes_accessible,
    load_kubernetes_client,
)


def test_api_exception_user_message_unauthorized():
    exc = ApiException(status=401, reason="Unauthorized")
    msg = _api_exception_user_message(exc)
    assert "401" in msg or "Unauthorized" in msg
    assert "credential" in msg.lower()


def test_api_exception_user_message_forbidden():
    exc = ApiException(status=403, reason="Forbidden")
    msg = _api_exception_user_message(exc)
    assert "403" in msg or "Forbidden" in msg or "allowed" in msg.lower()


def test_api_exception_user_message_status_zero():
    exc = ApiException(status=0, reason="connection refused")
    msg = _api_exception_user_message(exc)
    assert "response" in msg.lower() or "Kubernetes" in msg


def test_check_kubernetes_accessible_calls_version_endpoint():
    version_api = MagicMock()
    with (
        patch("helmadm.k8s.get_k8s_request_timeout", return_value=(4.0, 30.0)),
        patch("helmadm.k8s.client.VersionApi", return_value=version_api),
    ):
        api = MagicMock()
        check_kubernetes_accessible(api)
    version_api.get_code.assert_called_once_with(_request_timeout=(4.0, 30.0))


def test_check_kubernetes_accessible_maps_connect_timeout():
    version_api = MagicMock()
    version_api.get_code.side_effect = ConnectTimeoutError(
        None, "/", "Connection timed out."
    )

    with patch("helmadm.k8s.client.VersionApi", return_value=version_api):
        with pytest.raises(KubernetesApiError, match="Timed out contacting"):
            check_kubernetes_accessible(MagicMock())


def test_check_kubernetes_accessible_maps_api_exception():
    version_api = MagicMock()
    version_api.get_code.side_effect = ApiException(status=503, reason="Unavailable")

    with patch("helmadm.k8s.client.VersionApi", return_value=version_api):
        with pytest.raises(KubernetesApiError) as excinfo:
            check_kubernetes_accessible(MagicMock())

    assert "503" in str(excinfo.value) or "Unavailable" in str(excinfo.value)


def test_load_kubernetes_client_wraps_config_error(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with patch(
        "helmadm.k8s.config.load_kube_config",
        side_effect=config.ConfigException("no such file"),
    ):
        with pytest.raises(KubernetesApiError, match="Could not load Kubernetes"):
            load_kubernetes_client()
