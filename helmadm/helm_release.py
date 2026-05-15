import base64
import gzip
import json
from dataclasses import dataclass
from typing import Any

from kubernetes.client import CoreV1Api, V1Secret

from helmadm.argocd_manifest import chart_repo_url, needs_repo_url
from helmadm.env import get_k8s_request_timeout
from helmadm.logging_config import get_logger

logger = get_logger("helm_release")

HELM_RELEASE_LABEL_OWNER = "owner"
HELM_RELEASE_LABEL_NAME = "name"
HELM_RELEASE_LABEL_VERSION = "version"
HELM_RELEASE_LABEL_STATUS = "status"
HELM_RELEASE_OWNER = "helm"
HELM_RELEASE_SECRET_TYPE = "helm.sh/release.v1"
HELM_RELEASE_DATA_KEY = "release"


class HelmReleaseNotFoundError(Exception):
    pass


class HelmReleaseDecodeError(Exception):
    pass


@dataclass(frozen=True)
class HelmReleaseSummary:
    namespace: str
    name: str
    revision: int
    status: str | None = None
    chart_name: str | None = None
    chart_version: str | None = None
    repo_url: str | None = None
    needs_repo_url: bool = False


def validate_decoded_release(
    release: Any,
    *,
    expected_name: str | None = None,
) -> dict[str, Any]:
    """Sanity-check a decoded Helm release object before conversion."""
    if not isinstance(release, dict):
        raise HelmReleaseDecodeError(
            f"decoded release is not a JSON object (got {type(release).__name__})"
        )

    top_level_keys = sorted(release.keys())
    logger.debug("release top-level keys: %s", top_level_keys)

    release_name = release.get("name")
    if not isinstance(release_name, str) or not release_name:
        raise HelmReleaseDecodeError("release is missing a non-empty 'name' field")
    if expected_name is not None and release_name != expected_name:
        logger.debug(
            "release name %r does not match requested %r",
            release_name,
            expected_name,
        )

    chart = release.get("chart")
    if not isinstance(chart, dict):
        raise HelmReleaseDecodeError("release is missing a 'chart' object")

    metadata = chart.get("metadata")
    if not isinstance(metadata, dict):
        raise HelmReleaseDecodeError("release.chart is missing 'metadata'")

    chart_name = metadata.get("name")
    chart_version = metadata.get("version")
    if not chart_name or not chart_version:
        raise HelmReleaseDecodeError(
            "release.chart.metadata is missing 'name' or 'version'"
        )

    config = release.get("config")
    if config is not None and not isinstance(config, dict):
        raise HelmReleaseDecodeError(
            f"release.config must be an object (got {type(config).__name__})"
        )

    chart_values = chart.get("values")
    if chart_values is not None and not isinstance(chart_values, dict):
        raise HelmReleaseDecodeError(
            f"release.chart.values must be an object (got {type(chart_values).__name__})"
        )

    config_keys = len(config) if isinstance(config, dict) else 0
    default_keys = len(chart_values) if isinstance(chart_values, dict) else 0
    logger.debug(
        "release values: config=%d top-level key(s), chart.values=%d top-level key(s)",
        config_keys,
        default_keys,
    )
    if config_keys == 0 and default_keys > 0:
        logger.debug(
            "release.config is empty; valuesObject will only contain overrides "
            "that differ from chart defaults"
        )

    return release


def decode_release_data(
    b64_release: str,
    *,
    expected_name: str | None = None,
) -> dict[str, Any]:
    logger.debug("decoding helm release payload (%d base64 chars)", len(b64_release))
    if not b64_release or not str(b64_release).strip():
        raise HelmReleaseDecodeError("release secret data is empty")

    try:
        round1 = base64.b64decode(b64_release)
        logger.debug("base64 decode round 1: %d bytes", len(round1))
        round2 = base64.b64decode(round1)
        logger.debug("base64 decode round 2: %d bytes", len(round2))
        decompressed = gzip.decompress(round2)
        logger.debug("gzip decompress: %d bytes JSON payload", len(decompressed))
        release = json.loads(decompressed)
        logger.debug(
            "json parsed release name=%r chart=%r version=%r",
            release.get("name") if isinstance(release, dict) else None,
            release.get("chart", {}).get("metadata", {}).get("name")
            if isinstance(release, dict)
            else None,
            release.get("chart", {}).get("metadata", {}).get("version")
            if isinstance(release, dict)
            else None,
        )
        return validate_decoded_release(release, expected_name=expected_name)
    except HelmReleaseDecodeError:
        raise
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        logger.debug("failed to decode helm release secret: %s", exc)
        raise HelmReleaseDecodeError("failed to decode helm release secret") from exc


def _revision(secret: V1Secret) -> int:
    labels = secret.metadata.labels or {}
    version = labels.get(HELM_RELEASE_LABEL_VERSION, "0")
    try:
        return int(version)
    except ValueError:
        return 0


def _release_name(secret: V1Secret) -> str | None:
    labels = secret.metadata.labels or {}
    return labels.get(HELM_RELEASE_LABEL_NAME)


def _release_status(secret: V1Secret) -> str | None:
    labels = secret.metadata.labels or {}
    return labels.get(HELM_RELEASE_LABEL_STATUS)


def _is_helm_release_secret(secret: V1Secret) -> bool:
    return (
        secret.type == HELM_RELEASE_SECRET_TYPE
        and secret.data is not None
        and HELM_RELEASE_DATA_KEY in secret.data
    )


def _latest_release_secrets(secrets: list[V1Secret]) -> list[V1Secret]:
    logger.debug("filtering %d secret(s) to latest revision per release", len(secrets))
    latest_by_key: dict[tuple[str, str], V1Secret] = {}
    for secret in secrets:
        if not _is_helm_release_secret(secret):
            continue
        namespace = secret.metadata.namespace
        name = _release_name(secret)
        if not namespace or not name:
            continue
        key = (namespace, name)
        existing = latest_by_key.get(key)
        if existing is None or _revision(secret) > _revision(existing):
            latest_by_key[key] = secret
    result = list(latest_by_key.values())
    logger.debug("kept %d latest release secret(s)", len(result))
    return result


def _summary_from_secret(secret: V1Secret, detail: bool) -> HelmReleaseSummary:
    namespace = secret.metadata.namespace or ""
    name = _release_name(secret) or ""
    revision = _revision(secret)
    status = _release_status(secret)

    if not detail:
        return HelmReleaseSummary(
            namespace=namespace,
            name=name,
            revision=revision,
            status=status,
        )

    release = decode_release_data(secret.data[HELM_RELEASE_DATA_KEY])
    metadata = release.get("chart", {}).get("metadata", {})
    return HelmReleaseSummary(
        namespace=namespace,
        name=name,
        revision=revision,
        status=status,
        chart_name=metadata.get("name"),
        chart_version=metadata.get("version"),
        repo_url=chart_repo_url(release),
        needs_repo_url=needs_repo_url(release),
    )


def list_releases(
    api: CoreV1Api,
    namespace: str | None = None,
    *,
    all_namespaces: bool = False,
    detail: bool = False,
) -> list[HelmReleaseSummary]:
    label_selector = f"{HELM_RELEASE_LABEL_OWNER}={HELM_RELEASE_OWNER}"
    logger.debug(
        "listing releases: namespace=%r all_namespaces=%s detail=%s selector=%r",
        namespace,
        all_namespaces,
        detail,
        label_selector,
    )
    timeout = get_k8s_request_timeout()

    if all_namespaces:
        secrets = api.list_secret_for_all_namespaces(
            label_selector=label_selector,
            _request_timeout=timeout,
        ).items
        logger.debug("listed %d secret(s) cluster-wide", len(secrets))
    else:
        if not namespace:
            raise ValueError("namespace is required when not listing all namespaces")
        secrets = api.list_namespaced_secret(
            namespace=namespace,
            label_selector=label_selector,
            _request_timeout=timeout,
        ).items
        logger.debug(
            "listed %d secret(s) in namespace %r", len(secrets), namespace
        )

    latest_secrets = _latest_release_secrets(secrets)
    summaries = [_summary_from_secret(secret, detail) for secret in latest_secrets]
    summaries.sort(key=lambda item: (item.namespace, item.name))
    logger.debug("built %d release summary(ies)", len(summaries))
    return summaries


def find_latest_release_secret(
    api: CoreV1Api,
    namespace: str,
    release_name: str,
) -> V1Secret:
    label_selector = (
        f"{HELM_RELEASE_LABEL_OWNER}={HELM_RELEASE_OWNER},"
        f"{HELM_RELEASE_LABEL_NAME}={release_name}"
    )
    logger.debug(
        "finding latest secret for release=%r namespace=%r selector=%r",
        release_name,
        namespace,
        label_selector,
    )
    timeout = get_k8s_request_timeout()
    secrets = api.list_namespaced_secret(
        namespace=namespace,
        label_selector=label_selector,
        _request_timeout=timeout,
    ).items
    logger.debug("found %d matching secret(s)", len(secrets))

    helm_secrets = [secret for secret in secrets if _is_helm_release_secret(secret)]
    logger.debug("%d secret(s) are helm release secrets", len(helm_secrets))

    if not helm_secrets:
        raise HelmReleaseNotFoundError(
            f"helm release {release_name!r} not found in namespace {namespace!r}"
        )

    latest = max(helm_secrets, key=_revision)
    logger.debug(
        "selected secret %r revision %d",
        latest.metadata.name,
        _revision(latest),
    )
    return latest


def get_release(
    api: CoreV1Api,
    namespace: str,
    release_name: str,
) -> dict[str, Any]:
    logger.debug("fetching release %r in namespace %r", release_name, namespace)
    secret = find_latest_release_secret(api, namespace, release_name)
    encoded = secret.data[HELM_RELEASE_DATA_KEY]
    return decode_release_data(encoded, expected_name=release_name)
