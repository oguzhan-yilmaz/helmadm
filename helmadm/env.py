import os
from pathlib import Path

from helmadm.logging_config import get_logger

logger = get_logger("env")

ENV_NAMESPACE = "HELMADM_NAMESPACE"
ENV_CONTEXT = "HELMADM_CONTEXT"
ENV_REPO_URL = "HELMADM_REPO_URL"
ENV_RELEASE_NAME = "HELMADM_RELEASE_NAME"
# Per-key values/diff trace logs (off unless set; use with --verbose).
ENV_TRACE_VALUES = "HELMADM_TRACE_VALUES"

# HTTP timeouts for all Kubernetes API calls (connect, read) in seconds as floats.
ENV_K8S_CONNECT_TIMEOUT = "HELMADM_K8S_CONNECT_TIMEOUT"
ENV_K8S_READ_TIMEOUT = "HELMADM_K8S_READ_TIMEOUT"
_DEFAULT_K8S_CONNECT_TIMEOUT_S = 5.0
_DEFAULT_K8S_READ_TIMEOUT_S = 60.0

# Used by kubectl and the Kubernetes client when --kubeconfig is not passed.
ENV_KUBECONFIG = "KUBECONFIG"


def getenv_namespace() -> str | None:
    return os.environ.get(ENV_NAMESPACE) or None


def getenv_context() -> str | None:
    return os.environ.get(ENV_CONTEXT) or None


def getenv_repo_url() -> str | None:
    return os.environ.get(ENV_REPO_URL) or None


def getenv_release_name() -> str | None:
    return os.environ.get(ENV_RELEASE_NAME) or None


def trace_values_enabled() -> bool:
    return os.environ.get(ENV_TRACE_VALUES, "").lower() in ("1", "true", "yes")


def _parse_timeout_seconds(
    env_name: str,
    raw: str | None,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        logger.debug("invalid %s=%r; using default %s", env_name, raw, default)
        return default
    return max(minimum, min(maximum, value))


def get_k8s_request_timeout() -> tuple[float, float]:
    """
    Return (connect_timeout, read_timeout) for kubernetes ``_request_timeout``.

    Short connect default fails fast when the API server is unreachable (e.g. VPN off).
    """
    connect = _parse_timeout_seconds(
        ENV_K8S_CONNECT_TIMEOUT,
        os.environ.get(ENV_K8S_CONNECT_TIMEOUT),
        _DEFAULT_K8S_CONNECT_TIMEOUT_S,
        minimum=1.0,
        maximum=300.0,
    )
    read = _parse_timeout_seconds(
        ENV_K8S_READ_TIMEOUT,
        os.environ.get(ENV_K8S_READ_TIMEOUT),
        _DEFAULT_K8S_READ_TIMEOUT_S,
        minimum=5.0,
        maximum=3600.0,
    )
    return (connect, read)


def resolve_namespace(cli_value: str | None, kubeconfig: Path | None = None) -> str | None:
    if cli_value:
        logger.debug("namespace from CLI: %r", cli_value)
        return cli_value
    if env_value := getenv_namespace():
        logger.debug("namespace from %s: %r", ENV_NAMESPACE, env_value)
        return env_value
    from helmadm.k8s import get_kubeconfig_default_namespace

    kube_ns = get_kubeconfig_default_namespace(kubeconfig)
    logger.debug("namespace from kubeconfig: %r", kube_ns)
    return kube_ns


def resolve_release_name(cli_value: str | None) -> str | None:
    if cli_value:
        logger.debug("release name from CLI: %r", cli_value)
        return cli_value
    env_value = getenv_release_name()
    logger.debug("release name from %s: %r", ENV_RELEASE_NAME, env_value)
    return env_value


def resolve_context(cli_value: str | None) -> str | None:
    if cli_value:
        logger.debug("context from CLI: %r", cli_value)
        return cli_value
    env_value = getenv_context()
    logger.debug("context from %s: %r", ENV_CONTEXT, env_value)
    return env_value


def resolve_repo_url_option(cli_value: str | None) -> str | None:
    if cli_value:
        logger.debug("repo URL from CLI: %r", cli_value)
        return cli_value
    env_value = getenv_repo_url()
    logger.debug("repo URL from %s: %r", ENV_REPO_URL, env_value)
    return env_value
