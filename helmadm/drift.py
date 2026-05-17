from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import (
    DynamicApiError,
    ResourceNotFoundError,
)
from kubernetes.dynamic.exceptions import ForbiddenError as DynamicForbiddenError
from kubernetes.dynamic.exceptions import GoneError as DynamicGoneError
from kubernetes.dynamic.exceptions import NotFoundError as DynamicNotFoundError
from kubernetes.dynamic.resource import ResourceList

from helmadm.drift_ssa import (
    DriftCompareMode,
    SSAUnsupportedError,
    minimal_normalize,
    ssa_merged_object,
)
from helmadm.env import get_k8s_request_timeout
from helmadm.logging_config import get_logger

logger = get_logger("drift")

_METADATA_NOISE_KEYS = frozenset(
    {
        "creationTimestamp",
        "generation",
        "managedFields",
        "resourceVersion",
        "selfLink",
        "uid",
    }
)

DriftSeverity = Literal["ok", "missing", "drift", "fetch_error"]
DriftCompareSide = Literal["manifest", "live"]


@dataclass
class ManifestObjectResult:
    api_version: str
    kind: str
    namespace: str
    name: str
    severity: DriftSeverity = "ok"
    detail: str = ""
    diff: str | None = None
    compare_method: DriftCompareMode | None = None
    legacy_fallback_reason: str = ""


@dataclass
class DriftReport:
    release_name: str
    namespace: str
    compare_mode: DriftCompareMode = "ssa"
    field_manager: str = "helm"
    items: list[ManifestObjectResult] = field(default_factory=list)
    extras: list[tuple[str, str, str, str]] = field(default_factory=list)
    """(apiVersion, kind, namespace, name) live objects in `-n` missing from manifest."""
    extras_errors: list[str] = field(default_factory=list)

    @property
    def has_problem(self) -> bool:
        if any(it.severity != "ok" for it in self.items):
            return True
        if self.extras:
            return True
        return False


def parse_release_manifest(release: dict[str, Any]) -> list[dict[str, Any]]:
    """YAML documents Helm stored as `manifest`. Skips blanks and validates shape."""
    raw = release.get("manifest")
    if raw is None or not str(raw).strip():
        return []
    out: list[dict[str, Any]] = []
    for doc in yaml.safe_load_all(raw):
        if doc is None:
            continue
        if not isinstance(doc, dict):
            raise ValueError(
                f"Helm manifest document is not an object (got {type(doc).__name__})"
            )
        if not isinstance(doc.get("apiVersion"), str):
            raise ValueError("manifest object is missing apiVersion string")
        if not isinstance(doc.get("kind"), str):
            raise ValueError("manifest object is missing kind string")
        out.append(doc)
    return out


def sort_keys_deep(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: sort_keys_deep(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [sort_keys_deep(item) for item in obj]
    return obj


# Helm injects these at install/upgrade; they are not part of chart-rendered YAML in release.manifest.
_HELM_INSTALL_ONLY_ANNOTATION_KEYS = frozenset(
    {
        "meta.helm.sh/release-name",
        "meta.helm.sh/release-namespace",
    }
)

# Added by kubectl rollout restart / similar; not chart output.
_KUBECTL_RUNTIME_ANNOTATION_KEYS = frozenset(
    {
        "kubectl.kubernetes.io/restartedAt",
        "kubectl.kubernetes.io/last-applied-configuration",
    }
)

_REPLICA_AWARE_KINDS = frozenset({"Deployment", "ReplicaSet", "StatefulSet"})

_HELM_MANAGED_BY_LABEL_KEY = "app.kubernetes.io/managed-by"

_WORKLOAD_POD_TEMPLATE_KINDS = frozenset(
    {
        "CronJob",
        "DaemonSet",
        "Deployment",
        "Job",
        "Pod",
        "ReplicaSet",
        "StatefulSet",
    }
)


def drift_ssa_annotation_lines() -> list[str]:
    """Human-readable ``# helmadm:`` lines for SSA merged-vs-live diffs."""
    return [
        "# helmadm: Unified diff below compares SSA dry-run merged vs live.",
        "# helmadm: Stripped before compare/diff: status; metadata.managedFields.",
        "# helmadm: Merged = API server result of apply-patch dry-run (field manager helm).",
    ]


def drift_ignore_annotation_lines(kind: str) -> list[str]:
    """Human-readable ``# helmadm:`` lines documenting normalization before drift unified diffs."""
    meta_noise = ", ".join(sorted(_METADATA_NOISE_KEYS))
    helm_ann = ", ".join(sorted(_HELM_INSTALL_ONLY_ANNOTATION_KEYS))
    kubectl_ann = ", ".join(sorted(_KUBECTL_RUNTIME_ANNOTATION_KEYS))
    lines: list[str] = [
        "# helmadm: Unified diff below uses YAML after normalization.",
        f"# helmadm: Removed everywhere: status; metadata.namespace; metadata ({meta_noise}).",
        f"# helmadm: Removed annotations: {helm_ann}; {kubectl_ann}.",
        "# helmadm: Removed label app.kubernetes.io/managed-by when value is Helm "
        "(chart manifests often omit it).",
    ]
    if kind == "Service":
        lines.append(
            "# helmadm: Service spec: strip clusterIP, clusterIPs, ipFamilies, ipFamilyPolicy, "
            "internalTrafficPolicy; sessionAffinity when unset/None; nodePort on live ports only."
        )
    if kind == "Deployment":
        lines.append(
            "# helmadm: Deployment: strip deployment.kubernetes.io/revision; "
            "spec.progressDeadlineSeconds when 600."
        )
    if kind in _WORKLOAD_POD_TEMPLATE_KINDS:
        lines.append(
            "# helmadm: Pod / embedded Pod spec: strip common API defaults (schedulerName, "
            "dnsPolicy, hostNetwork, ...), empty securityContext/resources, redundant "
            "serviceAccount when serviceAccountName is set."
        )
    if kind in _REPLICA_AWARE_KINDS:
        lines.append(
            "# helmadm: Workload spec.replicas is compared explicitly (omitted in manifest "
            "means default 1 for Deployment/StatefulSet/ReplicaSet)."
        )
    return lines


def _strip_drifting_annotations(md: dict[str, Any]) -> None:
    """Remove annotations Helm/kubectl add after rendering."""
    ann = md.get("annotations")
    if not isinstance(ann, dict):
        return
    for key in _HELM_INSTALL_ONLY_ANNOTATION_KEYS:
        ann.pop(key, None)
    for key in _KUBECTL_RUNTIME_ANNOTATION_KEYS:
        ann.pop(key, None)
    if not ann:
        md.pop("annotations", None)


def _normalize_metadata_recursive(obj: Any) -> None:
    """Strip namespaces (chart templates often omit them), null annotations/labels, and noise."""
    if isinstance(obj, dict):
        md = obj.get("metadata")
        if isinstance(md, dict):
            md.pop("namespace", None)
            if md.get("annotations") is None:
                md.pop("annotations", None)
            if md.get("labels") is None:
                md.pop("labels", None)
            for k in _METADATA_NOISE_KEYS:
                md.pop(k, None)
            _strip_drifting_annotations(md)
            _strip_helm_managed_by_label(md)
        for v in obj.values():
            _normalize_metadata_recursive(v)
    elif isinstance(obj, list):
        for item in obj:
            _normalize_metadata_recursive(item)


def _strip_helm_managed_by_label(md: dict[str, Any]) -> None:
    """Helm sets managed-by on live objects; release manifest often omits it."""
    labels = md.get("labels")
    if not isinstance(labels, dict):
        return
    if labels.get(_HELM_MANAGED_BY_LABEL_KEY) == "Helm":
        labels.pop(_HELM_MANAGED_BY_LABEL_KEY, None)
    if not labels:
        md.pop("labels", None)


_DEPLOYMENT_REVISION_ANNOTATION = "deployment.kubernetes.io/revision"

# apiserver / kubectl defaults commonly omitted from Helm manifests (PodSpec & Container).
_PODSPEC_DEFAULT_FIELDS = {
    "terminationGracePeriodSeconds": 30,
    "schedulerName": "default-scheduler",
    "hostNetwork": False,
    "dnsPolicy": "ClusterFirst",
    "restartPolicy": "Always",
}
_CONTAINER_DEFAULT_FIELDS = {
    "terminationMessagePath": "/dev/termination-log",
    "terminationMessagePolicy": "File",
}


def _pop_empty_nested_dict(obj: dict[str, Any], key: str) -> None:
    child = obj.get(key)
    if isinstance(child, dict) and len(child) == 0:
        obj.pop(key, None)


def _strip_pod_spec_defaults(pod_spec: dict[str, Any]) -> None:
    for field, default in _PODSPEC_DEFAULT_FIELDS.items():
        if pod_spec.get(field) == default:
            pod_spec.pop(field, None)
    _pop_empty_nested_dict(pod_spec, "securityContext")
    if pod_spec.get("serviceAccountName"):
        pod_spec.pop("serviceAccount", None)
    for list_key in ("containers", "initContainers"):
        ctrs = pod_spec.get(list_key)
        if not isinstance(ctrs, list):
            continue
        for ctr in ctrs:
            if not isinstance(ctr, dict):
                continue
            for field, default in _CONTAINER_DEFAULT_FIELDS.items():
                if ctr.get(field) == default:
                    ctr.pop(field, None)
            _pop_empty_nested_dict(ctr, "securityContext")
            _pop_empty_nested_dict(ctr, "resources")


def _iter_embedded_pod_specs(obj: dict[str, Any]):
    """Yield PodSpec dicts embedded in workload objects (same object tree, mutated in place)."""
    kind = obj.get("kind")
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return
    if kind == "Pod":
        yield spec
        return
    if kind in ("Deployment", "DaemonSet", "StatefulSet", "ReplicaSet"):
        tmpl = spec.get("template")
        if isinstance(tmpl, dict):
            ps = tmpl.get("spec")
            if isinstance(ps, dict):
                yield ps
        return
    if kind == "Job":
        tmpl = spec.get("template")
        if isinstance(tmpl, dict):
            ps = tmpl.get("spec")
            if isinstance(ps, dict):
                yield ps
        return
    if kind == "CronJob":
        jt = spec.get("jobTemplate")
        if not isinstance(jt, dict):
            return
        js = jt.get("spec")
        if not isinstance(js, dict):
            return
        tmpl = js.get("template")
        if isinstance(tmpl, dict):
            ps = tmpl.get("spec")
            if isinstance(ps, dict):
                yield ps


def _strip_workload_template_defaults(obj: dict[str, Any]) -> None:
    """Strip controller annotations and apiserver-filled Pod template defaults."""
    kind = obj.get("kind")
    md = obj.get("metadata")
    if kind == "Deployment" and isinstance(md, dict):
        ann = md.get("annotations")
        if isinstance(ann, dict):
            ann.pop(_DEPLOYMENT_REVISION_ANNOTATION, None)
            if not ann:
                md.pop("annotations", None)

    spec = obj.get("spec")
    if kind == "Deployment" and isinstance(spec, dict):
        # Default per Kubernetes API; manifests often omit it.
        if spec.get("progressDeadlineSeconds") == 600:
            spec.pop("progressDeadlineSeconds", None)

    for pod_spec in _iter_embedded_pod_specs(obj):
        _strip_pod_spec_defaults(pod_spec)


def _strip_service_runtime_fields(
    obj: dict[str, Any],
    *,
    drift_side: DriftCompareSide | None,
) -> None:
    """Strip Service fields assigned or defaulted by the apiserver."""
    if obj.get("kind") != "Service":
        return
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return
    sa = spec.get("sessionAffinity")
    if sa is None or sa == "None":
        spec.pop("sessionAffinity", None)
    for key in (
        "clusterIP",
        "clusterIPs",
        "ipFamilies",
        "ipFamilyPolicy",
        "internalTrafficPolicy",
    ):
        spec.pop(key, None)
    # Omit cluster-assigned nodePorts on the live side only so pinned chart nodePorts still drift if wrong.
    strip_node_ports = drift_side is None or drift_side == "live"
    ports = spec.get("ports")
    if isinstance(ports, list) and strip_node_ports:
        for entry in ports:
            if isinstance(entry, dict):
                entry.pop("nodePort", None)


def effective_spec_replicas(obj: dict[str, Any]) -> int | None:
    """
    Desired replica count from spec.replicas, or Kubernetes default (1) when omitted.

    Returns None for kinds that do not use spec.replicas.
    """
    kind = obj.get("kind")
    if kind not in _REPLICA_AWARE_KINDS:
        return None
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return 1
    if "replicas" in spec:
        value = spec["replicas"]
        if value is None:
            return 1
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return 1


def replicas_mismatch(manifest_obj: dict[str, Any], live_obj: dict[str, Any]) -> bool:
    """True when both sides have a replica model and counts differ."""
    manifest_replicas = effective_spec_replicas(manifest_obj)
    live_replicas = effective_spec_replicas(live_obj)
    if manifest_replicas is None or live_replicas is None:
        return False
    return manifest_replicas != live_replicas


def normalize_for_compare(
    obj: dict[str, Any],
    *,
    drift_side: DriftCompareSide | None = None,
) -> dict[str, Any]:
    """Strip server-managed noise and runtime Helm/kubectl/Service fields for drift compares."""
    c = copy.deepcopy(obj)
    c.pop("status", None)
    _normalize_metadata_recursive(c)
    _strip_workload_template_defaults(c)
    _strip_service_runtime_fields(c, drift_side=drift_side)
    return sort_keys_deep(c)


def canonical_yaml_for_diff(
    obj: dict[str, Any],
    *,
    drift_side: DriftCompareSide | None = None,
) -> str:
    return yaml.safe_dump(
        normalize_for_compare(obj, drift_side=drift_side),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def _manifest_namespace_for_compare(
    obj: dict[str, Any],
    *,
    dyn: DynamicClient,
    resolved_ns_hint: str,
) -> tuple[str | None, str | None]:
    """Return resolved namespace string for fetching; error message if unresolved."""
    try:
        api_version = obj["apiVersion"]
        kind = obj["kind"]
        res_type = dyn.resources.get(api_version=api_version, kind=kind)
        if res_type.namespaced:
            manifest_ns_obj = ""
            md = obj.get("metadata") or {}
            if isinstance(md, dict) and md.get("namespace"):
                manifest_ns_obj = str(md["namespace"])
            use_ns = manifest_ns_obj.strip() if manifest_ns_obj.strip() else resolved_ns_hint
            if not use_ns.strip():
                return None, "namespaced manifest object has no usable namespace context"
            return use_ns.strip(), None
        return "", None
    except ResourceNotFoundError as exc:
        return None, (
            f"unknown API resource {obj.get('apiVersion')}/{obj.get('kind')} "
            f"({exc}); CRD/group version may not be installed"
        )


def manifest_resource_key(
    obj: dict[str, Any],
    *,
    dyn: DynamicClient,
    release_namespace: str,
) -> tuple[tuple[str, str, str, str], str | None]:
    md = obj.get("metadata") or {}
    api_version = str(obj["apiVersion"])
    kind = str(obj["kind"])
    name = ""
    if isinstance(md, dict) and md.get("name"):
        name = str(md["name"])
    if not name.strip():
        return (api_version, kind, "", ""), "invalid manifest entry: metadata.name missing"
    resolved_ns_for_get, lookup_err = _manifest_namespace_for_compare(
        obj,
        dyn=dyn,
        resolved_ns_hint=release_namespace,
    )
    if lookup_err:
        return (api_version, kind, "", ""), lookup_err
    ns_part = (
        resolved_ns_for_get if resolved_ns_for_get is not None else ""
    )
    return ((api_version, kind, ns_part, name.strip()), None)


def fetch_live_object(
    dyn: DynamicClient,
    obj: dict[str, Any],
    *,
    release_namespace: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Retrieve live object matching manifest doc. Returns `(None, error_token_or_message)`."""
    timeout_kw = dict(_request_timeout=get_k8s_request_timeout())
    md = obj.get("metadata") or {}
    name = ""
    if isinstance(md, dict) and md.get("name"):
        name = str(md["name"])
    else:
        return None, "invalid manifest entry: metadata.name missing"

    api_version = str(obj["apiVersion"])
    kind = str(obj["kind"])
    resolved_ns_for_get, lookup_err = _manifest_namespace_for_compare(
        obj, dyn=dyn, resolved_ns_hint=release_namespace
    )
    if lookup_err:
        return None, lookup_err

    try:
        resource_type = dyn.resources.get(api_version=api_version, kind=kind)
    except ResourceNotFoundError as exc:
        return (
            None,
            f'unknown API resource {api_version}/{kind}: {exc}',
        )

    try:
        if resource_type.namespaced:
            ns = resolved_ns_for_get if resolved_ns_for_get is not None else release_namespace
            inst = resource_type.get(namespace=ns, name=name, **timeout_kw)
        else:
            inst = resource_type.get(name=name, **timeout_kw)
    except DynamicNotFoundError:
        return None, "missing"
    except DynamicGoneError:
        return None, "gone"
    except DynamicForbiddenError as exc:
        return None, f"forbidden: {exc.summary()}"
    except DynamicApiError as exc:
        logger.debug(
            "GET %s/%s name=%s: %s", api_version, kind, name, exc
        )
        return None, f"http_error ({exc.status}): {exc.summary()}"

    return inst.to_dict(), None


def _unified_diff_filenames(
    api_version: str,
    kind: str,
    namespace: str,
    name: str,
    *,
    from_prefix: str = "manifest",
) -> tuple[str, str]:
    """Paths shown in unified-diff headers (readable in delta / git-style viewers)."""
    ns_seg = namespace.strip() if namespace.strip() else "cluster-scoped"
    core = f"{api_version}/{kind}/{ns_seg}/{name}"
    return (f"{from_prefix}/{core}", f"live/{core}")


def _canonical_yaml_for_ssa_diff(obj: dict[str, Any]) -> str:
    return yaml.safe_dump(
        minimal_normalize(obj),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def _unified_yaml_diff(
    expected_obj: dict[str, Any],
    live_obj: dict[str, Any],
    *,
    api_version: str,
    kind: str,
    namespace: str,
    name: str,
    compare_method: DriftCompareMode = "legacy",
) -> str:
    from_prefix = "merged" if compare_method == "ssa" else "manifest"
    from_file, to_file = _unified_diff_filenames(
        api_version, kind, namespace, name, from_prefix=from_prefix
    )
    if compare_method == "ssa":
        a = _canonical_yaml_for_ssa_diff(expected_obj).splitlines(True)
        b = _canonical_yaml_for_ssa_diff(live_obj).splitlines(True)
    else:
        a = canonical_yaml_for_diff(expected_obj, drift_side="manifest").splitlines(True)
        b = canonical_yaml_for_diff(live_obj, drift_side="live").splitlines(True)
    lines_list = list(
        difflib.unified_diff(
            a,
            b,
            fromfile=from_file,
            tofile=to_file,
            lineterm="\n",
        )
    )
    if not lines_list:
        return ""
    return "".join(lines_list).rstrip() + "\n"


def _should_skip_extras_list_item(item: dict[str, Any]) -> bool:
    """Helm 3 release payloads live here and are never chart manifest objects."""
    return item.get("kind") == "Secret" and item.get("type") == "helm.sh/release.v1"


def _iter_namespaced_listable(dyn: DynamicClient):
    """Namespaced Kubernetes resources that support LIST (for `--detect-extras`)."""
    for resource_type in dyn.resources:
        if isinstance(resource_type, ResourceList):
            continue
        kind_name = getattr(resource_type, "kind", "") or ""
        if kind_name.endswith("List"):
            continue
        verbs = getattr(resource_type, "verbs", None)
        if not verbs or "list" not in verbs:
            continue
        if not getattr(resource_type, "namespaced", False):
            continue
        yield resource_type


def _collect_extras_live(
    dyn: DynamicClient,
    *,
    release_namespace: str,
    manifest_keys: set[tuple[str, str, str, str]],
) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """List every namespaced API kind in the namespace; extras are objects absent from manifest."""
    tt = dict(_request_timeout=get_k8s_request_timeout())
    extras: dict[tuple[str, str, str, str], bool] = {}
    errs: list[str] = []

    for resource_type in _iter_namespaced_listable(dyn):
        rk = getattr(resource_type, "kind", None)
        gv = getattr(resource_type, "group_version", "") or getattr(
            resource_type, "api_version", ""
        )
        logger.debug(
            "detect-extras: listing %s/%s ns=%s (no label filter)",
            gv,
            rk,
            release_namespace,
        )
        try:
            inst = resource_type.get(
                namespace=release_namespace,
                **tt,
            )
        except DynamicForbiddenError as exc:
            errs.append(f"list forbidden {gv}/{rk}: {exc.summary()}")
            continue
        except DynamicApiError as exc:
            errs.append(f"list failed {gv}/{rk}: ({exc.status}) {exc.summary()}")
            continue
        body = inst.to_dict()
        items_raw = body.get("items") or []
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            if _should_skip_extras_list_item(item):
                continue
            md = item.get("metadata") or {}
            if not isinstance(md, dict):
                continue
            n = md.get("name") or ""
            av = item.get("apiVersion")
            kd = item.get("kind") or rk
            if not av or not n:
                continue
            key = (str(av), str(kd), release_namespace, str(n))
            extras[key] = True

    extra_keys_sorted = sorted(
        (key for key in extras if key not in manifest_keys),
        key=lambda item: item[1],
    )
    return extra_keys_sorted, errs


def _ssa_drift_detail(merged: dict[str, Any], live: dict[str, Any]) -> str:
    merged_replicas = effective_spec_replicas(merged)
    live_replicas = effective_spec_replicas(live)
    if (
        merged_replicas is not None
        and live_replicas is not None
        and merged_replicas != live_replicas
    ):
        return (
            f"spec.replicas differs (merged {merged_replicas}, live {live_replicas})"
        )
    return "SSA merged object differs from live object"


def _legacy_compare_object(
    obj: dict[str, Any],
    live: dict[str, Any],
    *,
    api_version: str,
    kind: str,
    namespace: str,
    name: str,
) -> ManifestObjectResult:
    expected_norm_dict = normalize_for_compare(obj, drift_side="manifest")
    live_norm = normalize_for_compare(live, drift_side="live")
    replica_drift = replicas_mismatch(obj, live)
    if replica_drift:
        logger.debug(
            "replica drift %s/%s %s: manifest=%s live=%s",
            kind,
            name,
            namespace,
            effective_spec_replicas(obj),
            effective_spec_replicas(live),
        )
    if expected_norm_dict == live_norm and not replica_drift:
        return ManifestObjectResult(
            api_version=api_version,
            kind=kind,
            namespace=namespace,
            name=name,
            severity="ok",
            compare_method="legacy",
        )
    detail = "release manifest differs from live object spec/metadata"
    if replica_drift:
        detail = (
            f"spec.replicas differs (manifest "
            f"{effective_spec_replicas(obj)}, live {effective_spec_replicas(live)})"
        )
    return ManifestObjectResult(
        api_version=api_version,
        kind=kind,
        namespace=namespace,
        name=name,
        severity="drift",
        detail=detail,
        compare_method="legacy",
        diff=_unified_yaml_diff(
            obj,
            live,
            api_version=api_version,
            kind=kind,
            namespace=namespace,
            name=name,
            compare_method="legacy",
        ),
    )


def run_drift(
    dyn: DynamicClient,
    release: dict[str, Any],
    *,
    release_namespace: str,
    release_name: str,
    detect_extras: bool,
    compare_mode: DriftCompareMode = "ssa",
    field_manager: str = "helm",
    verbose: bool = False,
) -> DriftReport:
    objs = parse_release_manifest(release)
    report = DriftReport(
        release_name=release_name,
        namespace=release_namespace,
        compare_mode=compare_mode,
        field_manager=field_manager,
    )
    manifest_keys: set[tuple[str, str, str, str]] = set()

    for obj in objs:
        key_full, key_err = manifest_resource_key(
            obj, dyn=dyn, release_namespace=release_namespace
        )
        if key_err:
            report.items.append(
                ManifestObjectResult(
                    api_version=str(obj["apiVersion"]),
                    kind=str(obj["kind"]),
                    namespace=release_namespace,
                    name="",
                    severity="fetch_error",
                    detail=key_err,
                )
            )
            continue

        api_version, kind, ns_eff, nm = key_full
        manifest_keys.add((api_version, kind, ns_eff, nm))

        live, err_msg = fetch_live_object(
            dyn, obj, release_namespace=release_namespace
        )

        if err_msg and err_msg not in {"missing", "gone"}:
            report.items.append(
                ManifestObjectResult(
                    api_version=api_version,
                    kind=kind,
                    namespace=(ns_eff or release_namespace),
                    name=nm,
                    severity="fetch_error",
                    detail=err_msg,
                )
            )
            continue

        if err_msg in {"missing", "gone"}:
            detail = (
                "object missing in cluster"
                if err_msg == "missing"
                else "object Gone (possibly removed migration)"
            )
            report.items.append(
                ManifestObjectResult(
                    api_version=api_version,
                    kind=kind,
                    namespace=(ns_eff or release_namespace),
                    name=nm,
                    severity="missing",
                    detail=detail,
                )
            )
            continue

        if live is None:
            report.items.append(
                ManifestObjectResult(
                    api_version=api_version,
                    kind=kind,
                    namespace=(ns_eff or release_namespace),
                    name=nm,
                    severity="fetch_error",
                    detail="unexpected empty response",
                )
            )
            continue

        item_ns = ns_eff or release_namespace

        if compare_mode == "legacy":
            report.items.append(
                _legacy_compare_object(
                    obj,
                    live,
                    api_version=api_version,
                    kind=kind,
                    namespace=item_ns,
                    name=nm,
                )
            )
            continue

        use_legacy = False
        legacy_reason = ""
        merged: dict[str, Any] | None = None
        try:
            merged = ssa_merged_object(
                dyn,
                obj,
                resolved_namespace=ns_eff or None,
                field_manager=field_manager,
            )
        except SSAUnsupportedError as exc:
            use_legacy = True
            legacy_reason = str(exc)
            logger.debug(
                "SSA unavailable for %s/%s %s: %s",
                api_version,
                kind,
                nm,
                exc,
            )

        if use_legacy:
            result = _legacy_compare_object(
                obj,
                live,
                api_version=api_version,
                kind=kind,
                namespace=item_ns,
                name=nm,
            )
            result.legacy_fallback_reason = legacy_reason
            if verbose and legacy_reason:
                result.detail = (
                    f"{result.detail}; legacy fallback: {legacy_reason}"
                    if result.detail and result.severity == "drift"
                    else (
                        f"legacy fallback: {legacy_reason}"
                        if result.severity == "ok"
                        else result.detail
                    )
                )
            report.items.append(result)
            continue

        assert merged is not None
        merged_norm = minimal_normalize(merged)
        live_norm = minimal_normalize(live)
        if merged_norm == live_norm:
            report.items.append(
                ManifestObjectResult(
                    api_version=api_version,
                    kind=kind,
                    namespace=item_ns,
                    name=nm,
                    severity="ok",
                    compare_method="ssa",
                )
            )
        else:
            report.items.append(
                ManifestObjectResult(
                    api_version=api_version,
                    kind=kind,
                    namespace=item_ns,
                    name=nm,
                    severity="drift",
                    detail=_ssa_drift_detail(merged, live),
                    compare_method="ssa",
                    diff=_unified_yaml_diff(
                        merged,
                        live,
                        api_version=api_version,
                        kind=kind,
                        namespace=item_ns,
                        name=nm,
                        compare_method="ssa",
                    ),
                )
            )

    if detect_extras and release_namespace.strip():
        extra_list, errs = _collect_extras_live(
            dyn,
            release_namespace=release_namespace,
            manifest_keys=manifest_keys,
        )
        report.extras = extra_list
        report.extras_errors = errs

    return report


def format_report_text(
    report: DriftReport,
    *,
    ignore_annotations: bool = False,
    verbose: bool = False,
) -> str:
    lines: list[str] = []
    hr = "=" * 64
    lines.append(hr)
    compare_desc = (
        "SSA dry-run merged vs live API"
        if report.compare_mode == "ssa"
        else "legacy manifest normalization vs live API"
    )
    lines.append(
        f"Helm drift: release {report.release_name!r} "
        f"namespace {report.namespace!r} ({compare_desc}; read-only)"
    )
    if report.compare_mode == "ssa":
        lines.append(
            f"# helmadm: compare-mode=ssa field-manager={report.field_manager!r}"
        )
    if ignore_annotations:
        if report.compare_mode == "ssa":
            lines.append(
                "# helmadm: --ignore-annotations / -ia: notes per drift item "
                "(SSA merged vs live; legacy fallback uses normalization rules)."
            )
        else:
            lines.append(
                "# helmadm: --ignore-annotations / -ia: normalization rules per drift item "
                "(see helmadm drift --help)."
            )
    lines.append(hr)

    severity_order = {"fetch_error": 0, "missing": 1, "drift": 2, "ok": 3}
    for item in sorted(
        report.items, key=lambda it: severity_order[it.severity]
    ):
        ident = (
            f"{item.api_version}/{item.kind} "
            f"{item.namespace}/{item.name}"
            if item.namespace
            else f"{item.api_version}/{item.kind} {item.name}"
        )
        prefix = {"ok": "[ok]", "drift": "[drift]", "missing": "[missing]", "fetch_error": "[fetch_error]"}.get(item.severity, "[?]")
        lines.append(f"{prefix} {ident}")
        if item.detail.strip():
            lines.append(f"    {item.detail}")
        if (
            verbose
            and item.legacy_fallback_reason
            and item.compare_method == "legacy"
        ):
            lines.append(
                f"    # helmadm: SSA unavailable, legacy compare: "
                f"{item.legacy_fallback_reason}"
            )
        if item.diff and item.severity == "drift":
            if ignore_annotations:
                if item.compare_method == "ssa":
                    for note in drift_ssa_annotation_lines():
                        lines.append(note)
                else:
                    for note in drift_ignore_annotation_lines(item.kind):
                        lines.append(note)
            for diff_line in item.diff.rstrip().splitlines():
                lines.append(diff_line.rstrip())

    if report.extras:
        lines.append(
            "--- Namespace objects not in release manifest "
            "(full LIST per API kind; includes resources without Helm labels) ---"
        )
        for av, k, ns, n in sorted(report.extras):
            suffix = f" {ns}/{n}" if ns else f"/{n}"
            lines.append(f"[extra] {av}/{k}{suffix}")

    if report.extras_errors:
        lines.append("--- while scanning for extras ---")
        for err in report.extras_errors:
            lines.append(err)

    total = len(report.items)
    bad = sum(1 for item in report.items if item.severity != "ok")
    extras_n = len(report.extras)
    lines.append(hr)
    if report.has_problem:
        extras_part = f", {extras_n} extra object(s)" if extras_n else ""
        lines.append(
            f"RESULT: problems found ({bad} manifest object(s)"
            + extras_part
            + ")"
        )
    else:
        lines.append(f"RESULT: in sync ({total} manifest object(s))")

    lines.append("")
    return "\n".join(lines)
