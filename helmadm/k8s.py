import os
from pathlib import Path

from kubernetes import client, config
from kubernetes.client import CoreV1Api
from kubernetes.client.rest import ApiException
from kubernetes.dynamic import DynamicClient
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, ReadTimeoutError

from helmadm.env import (
    ENV_K8S_CONNECT_TIMEOUT,
    ENV_K8S_READ_TIMEOUT,
    get_k8s_request_timeout,
)
from helmadm.logging_config import get_logger

logger = get_logger("k8s")


class KubernetesApiError(RuntimeError):
    """The Kubernetes API did not respond successfully (network, TLS, or HTTP error)."""


def _api_exception_user_message(exc: ApiException) -> str:
    status = exc.status
    reason = (exc.reason or "").strip()
    if status in (None, 0):
        base = "No response from the Kubernetes API server."
        hint = " Check your network, firewall, and that the cluster endpoint URL in kubeconfig is correct."
        tail = f" ({reason})" if reason else ""
        return base + tail + hint
    if status == 401:
        return (
            "Kubernetes API returned 401 Unauthorized. "
            "Refresh your credentials (for example re-run kubectl or cloud login)."
        )
    if status == 403:
        return (
            "Kubernetes API returned 403 Forbidden. "
            "Your current credentials are not allowed to access this cluster."
        )
    body = (getattr(exc, "body", None) or "").strip()
    parts = [f"Kubernetes API request failed (HTTP {status})."]
    if reason:
        parts.append(reason)
    if body and body not in reason:
        parts.append(body[:500])
    return " ".join(parts)


def _timeout_user_message() -> str:
    return (
        "Timed out contacting the Kubernetes API (VPN off or network issue?). "
        f"Tune {ENV_K8S_CONNECT_TIMEOUT} / {ENV_K8S_READ_TIMEOUT} if needed."
    )


def check_kubernetes_accessible(api: CoreV1Api) -> None:
    """Verify the API server is reachable via a lightweight /version/ call."""
    timeout = get_k8s_request_timeout()
    try:
        version_api = client.VersionApi(api.api_client)
        version_api.get_code(_request_timeout=timeout)
    except ApiException as exc:
        logger.debug("kubernetes connectivity check failed: ApiException %s", exc)
        raise KubernetesApiError(_api_exception_user_message(exc)) from exc
    except (ConnectTimeoutError, ReadTimeoutError) as exc:
        logger.debug("kubernetes connectivity check failed: timeout %s", exc)
        raise KubernetesApiError(_timeout_user_message()) from exc
    except MaxRetryError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (ConnectTimeoutError, ReadTimeoutError)):
            logger.debug(
                "kubernetes connectivity check failed: MaxRetry timeout %s", exc
            )
            raise KubernetesApiError(_timeout_user_message()) from exc
        logger.debug("kubernetes connectivity check failed: MaxRetryError %s", exc)
        raise KubernetesApiError(
            "Could not connect to the Kubernetes API (network error). "
            "Check that the cluster is reachable and kubeconfig points to the correct server."
        ) from exc
    except OSError as exc:
        logger.debug("kubernetes connectivity check failed: OSError %s", exc)
        raise KubernetesApiError(
            "Could not connect to the Kubernetes API (network error). "
            "Check that the cluster is reachable and kubeconfig points to the correct server."
        ) from exc
    except Exception as exc:
        etype = type(exc).__name__
        if "Timeout" in etype or etype == "TimeoutError":
            logger.debug("kubernetes connectivity check failed: %s", exc)
            raise KubernetesApiError(_timeout_user_message()) from exc
        logger.debug("kubernetes connectivity check failed: %s", exc)
        raise KubernetesApiError(
            "Could not reach the Kubernetes API. "
            "Check kubeconfig, credentials, and that the cluster is running."
        ) from exc


def get_kubeconfig_default_namespace(kubeconfig: Path | None = None) -> str | None:
    """Return the namespace from the current kubeconfig context (kubectl-style)."""
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        logger.debug("in-cluster environment detected; no kubeconfig namespace")
        return None

    try:
        kwargs: dict[str, str] = {}
        if kubeconfig is not None:
            kwargs["config_file"] = str(kubeconfig)
        logger.debug("reading kubeconfig contexts (kubeconfig=%s)", kubeconfig)
        _contexts, active_context = config.list_kube_config_contexts(**kwargs)
    except config.ConfigException as exc:
        logger.debug("kubeconfig not available: %s", exc)
        return None

    if not active_context:
        logger.debug("no active kubeconfig context")
        return None

    context = active_context.get("context") or {}
    namespace = context.get("namespace")
    if namespace:
        logger.debug("kubeconfig default namespace: %r", namespace)
        return str(namespace)
    logger.debug("active context has no default namespace")
    return None


def _ensure_kubernetes_config_loaded(
    kubeconfig: str | None = None,
    context: str | None = None,
) -> None:
    try:
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            logger.debug("loading in-cluster kubernetes config")
            config.load_incluster_config()
        else:
            kwargs: dict[str, str] = {}
            if kubeconfig is not None:
                kwargs["config_file"] = kubeconfig
            if context is not None:
                kwargs["context"] = context
            logger.debug(
                "loading kubeconfig (kubeconfig=%r context=%r)", kubeconfig, context
            )
            config.load_kube_config(**kwargs)
    except config.ConfigException as exc:
        logger.debug("kubernetes config load failed: %s", exc)
        raise KubernetesApiError(
            "Could not load Kubernetes configuration. "
            "Set KUBECONFIG or place a valid config at ~/.kube/config, "
            "and ensure the chosen context exists. "
            f"({exc})"
        ) from exc


def load_kubernetes_client(
    kubeconfig: str | None = None,
    context: str | None = None,
) -> CoreV1Api:
    _ensure_kubernetes_config_loaded(kubeconfig=kubeconfig, context=context)
    logger.debug("kubernetes CoreV1Api client ready")
    return client.CoreV1Api()


def load_dynamic_client(
    kubeconfig: str | None = None,
    context: str | None = None,
) -> DynamicClient:
    """Build a dynamic Kubernetes client using the same config as ``load_kubernetes_client``."""
    _ensure_kubernetes_config_loaded(kubeconfig=kubeconfig, context=context)
    logger.debug("kubernetes DynamicClient ready")
    return DynamicClient(client.ApiClient())
