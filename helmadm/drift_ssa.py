"""Server-side apply dry-run for drift compare (kubectl diff --server-side model)."""

from __future__ import annotations

import copy
from typing import Any, Literal

from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import DynamicApiError
from kubernetes.dynamic.exceptions import ForbiddenError as DynamicForbiddenError
from kubernetes.dynamic.exceptions import ResourceNotFoundError

from helmadm.env import get_k8s_request_timeout
from helmadm.logging_config import get_logger

logger = get_logger("drift_ssa")

DriftCompareMode = Literal["ssa", "legacy"]

# Helm/kubectl add these at install; chart manifests in release.manifest often omit them.
_HELM_INSTALL_ANNOTATION_KEYS = frozenset(
    {
        "meta.helm.sh/release-name",
        "meta.helm.sh/release-namespace",
    }
)
_KUBECTL_RUNTIME_ANNOTATION_KEYS = frozenset(
    {
        "kubectl.kubernetes.io/restartedAt",
        "kubectl.kubernetes.io/last-applied-configuration",
    }
)
_HELM_MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"


def _sort_keys_deep(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sort_keys_deep(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_sort_keys_deep(item) for item in obj]
    return obj

_DEFAULT_FIELD_MANAGER = "helm"
_SSA_MAX_RETRIES = 4


class SSAUnsupportedError(Exception):
    """SSA dry-run cannot be used for this object; caller should use legacy compare."""


def _strip_install_metadata(md: dict[str, Any]) -> None:
    """Remove Helm/kubectl install-time metadata not present in chart manifests."""
    md.pop("managedFields", None)
    ann = md.get("annotations")
    if isinstance(ann, dict):
        for key in _HELM_INSTALL_ANNOTATION_KEYS:
            ann.pop(key, None)
        for key in _KUBECTL_RUNTIME_ANNOTATION_KEYS:
            ann.pop(key, None)
        if not ann:
            md.pop("annotations", None)
    labels = md.get("labels")
    if isinstance(labels, dict):
        if labels.get(_HELM_MANAGED_BY_LABEL) == "Helm":
            labels.pop(_HELM_MANAGED_BY_LABEL, None)
        if not labels:
            md.pop("labels", None)


def _strip_install_metadata_recursive(obj: Any) -> None:
    if isinstance(obj, dict):
        md = obj.get("metadata")
        if isinstance(md, dict):
            _strip_install_metadata(md)
        for value in obj.values():
            _strip_install_metadata_recursive(value)
    elif isinstance(obj, list):
        for item in obj:
            _strip_install_metadata_recursive(item)


def minimal_normalize(obj: dict[str, Any]) -> dict[str, Any]:
    """Strip status, managedFields, and Helm/kubectl install metadata before SSA compare."""
    c = copy.deepcopy(obj)
    c.pop("status", None)
    _strip_install_metadata_recursive(c)
    return _sort_keys_deep(c)


def _apply_body_from_manifest(
    manifest_obj: dict[str, Any],
    *,
    resolved_namespace: str,
) -> dict[str, Any]:
    body = copy.deepcopy(manifest_obj)
    body.pop("status", None)
    md = body.setdefault("metadata", {})
    if not isinstance(md, dict):
        md = {}
        body["metadata"] = md
    if resolved_namespace and not md.get("namespace"):
        md["namespace"] = resolved_namespace
    return body


def _is_ssa_unsupported(exc: DynamicApiError) -> bool:
    status = getattr(exc, "status", None)
    if status in (405, 415, 422):
        return True
    summary = (exc.summary() or "").lower()
    markers = (
        "apply-patch",
        "apply patch",
        "server-side apply",
        "serversideapply",
        "fieldmanager",
        "field manager",
        "content type",
        "unsupported",
    )
    return any(m in summary for m in markers)


def ssa_merged_object(
    dyn: DynamicClient,
    manifest_obj: dict[str, Any],
    *,
    resolved_namespace: str | None,
    field_manager: str = _DEFAULT_FIELD_MANAGER,
) -> dict[str, Any]:
    """
    Return the object as the API server would store it after SSA apply (dry-run).

    Raises SSAUnsupportedError when the cluster/API cannot perform SSA dry-run.
    """
    api_version = str(manifest_obj["apiVersion"])
    kind = str(manifest_obj["kind"])
    md = manifest_obj.get("metadata") or {}
    name = str(md.get("name") or "").strip()
    if not name:
        raise SSAUnsupportedError("manifest object has no metadata.name")

    resolved_ns = resolved_namespace

    try:
        resource_type = dyn.resources.get(api_version=api_version, kind=kind)
    except ResourceNotFoundError as exc:
        raise SSAUnsupportedError(
            f"unknown API resource {api_version}/{kind}: {exc}"
        ) from exc

    apply_body = _apply_body_from_manifest(
        manifest_obj,
        resolved_namespace=resolved_ns or "",
    )
    timeout_kw = dict(_request_timeout=get_k8s_request_timeout())
    patch_ns = resolved_ns if resource_type.namespaced else None
    last_exc: DynamicApiError | None = None

    for attempt in range(1, _SSA_MAX_RETRIES + 1):
        try:
            inst = dyn.server_side_apply(
                resource_type,
                body=apply_body,
                name=name,
                namespace=patch_ns,
                dry_run="All",
                field_manager=field_manager,
                **timeout_kw,
            )
            return inst.to_dict()
        except DynamicForbiddenError as exc:
            raise SSAUnsupportedError(f"forbidden: {exc.summary()}") from exc
        except DynamicApiError as exc:
            last_exc = exc
            if exc.status == 409 and attempt < _SSA_MAX_RETRIES:
                logger.debug(
                    "SSA dry-run conflict %s/%s %s (attempt %d/%d), retrying",
                    api_version,
                    kind,
                    name,
                    attempt,
                    _SSA_MAX_RETRIES,
                )
                continue
            if _is_ssa_unsupported(exc):
                raise SSAUnsupportedError(exc.summary() or str(exc)) from exc
            raise SSAUnsupportedError(
                f"http_error ({exc.status}): {exc.summary()}"
            ) from exc

    assert last_exc is not None
    raise SSAUnsupportedError(
        f"http_error ({last_exc.status}): {last_exc.summary()}"
    ) from last_exc
