"""Tests for manifest vs live Helm drift comparisons."""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from helmadm.cli import app
from helmadm.drift import (
    effective_spec_replicas,
    replicas_mismatch,
    DriftReport,
    ExtraObjectResult,
    ManifestObjectResult,
    _collect_extras_live,
    _is_managed_by_helm_release,
    _should_skip_extras_list_item,
    _unified_yaml_diff,
    drift_ignore_annotation_lines,
    drift_ssa_annotation_lines,
    fetch_live_object,
    format_report_text,
    normalize_for_compare,
    parse_release_manifest,
    run_drift,
)
from helmadm.drift_ssa import (
    SSAUnsupportedError,
    minimal_normalize,
    ssa_merged_object,
)
from helmadm.env import ENV_NAMESPACE, ENV_RELEASE_NAME

runner = CliRunner()


def _mock_ssa_returns_manifest(
    _dyn: object,
    manifest_obj: dict,
    *,
    resolved_namespace: str | None,  # noqa: ARG001
    field_manager: str = "helm",  # noqa: ARG001
) -> dict:
    merged = copy.deepcopy(manifest_obj)
    merged.pop("status", None)
    return merged


class _FakeDynDiscovery:
    @staticmethod
    def get(*, api_version: str, kind: str):  # noqa: ARG004
        return type("FakeResDescriptor", (), {"namespaced": True})()


class FakeDynamicClient:
    resources = _FakeDynDiscovery()


def test_parse_release_manifest_extracts_documents(sample_release: dict) -> None:
    objs = parse_release_manifest(sample_release)
    assert len(objs) == 1
    assert objs[0]["kind"] == "ConfigMap"
    assert objs[0]["metadata"]["name"] == "drift-cm"


def test_parse_release_manifest_raises_on_bad_shape(sample_release: dict) -> None:
    broken = dict(sample_release)
    broken["manifest"] = "---\n[]\n"
    with pytest.raises(ValueError, match="not an object"):
        parse_release_manifest(broken)


def test_parse_release_manifest_empty_returns_empty(sample_release: dict) -> None:
    r = dict(sample_release)
    r.pop("manifest", None)
    assert parse_release_manifest(r) == []


def test_parse_release_manifest_expands_list_documents() -> None:
    release = {
        "manifest": """---
apiVersion: v1
kind: List
metadata:
  name: bundled-rules
items:
- apiVersion: monitoring.coreos.com/v1
  kind: PrometheusRule
  metadata:
    name: rule-a
    namespace: monitoring
- apiVersion: monitoring.coreos.com/v1
  kind: PrometheusRule
  metadata:
    name: rule-b
    namespace: monitoring
""",
    }
    objs = parse_release_manifest(release)
    assert len(objs) == 2
    assert all(o["kind"] == "PrometheusRule" for o in objs)
    assert [o["metadata"]["name"] for o in objs] == ["rule-a", "rule-b"]


def test_minimal_normalize_strips_status_and_managed_fields() -> None:
    obj = {
        "apiVersion": "v1",
        "kind": "Foo",
        "metadata": {
            "name": "n",
            "managedFields": [{"manager": "helm"}],
        },
        "status": {"x": 1},
        "spec": {"a": "b"},
    }
    normalized = minimal_normalize(obj)
    assert "status" not in normalized
    assert "managedFields" not in normalized["metadata"]
    assert normalized["spec"] == {"a": "b"}


def test_minimal_normalize_strips_helm_managed_by_label() -> None:
    merged = {
        "metadata": {"name": "d", "labels": {"app": "x"}},
    }
    live = {
        "metadata": {
            "name": "d",
            "labels": {"app": "x", "app.kubernetes.io/managed-by": "Helm"},
        },
    }
    assert minimal_normalize(merged) == minimal_normalize(live)


def test_normalize_strips_noise() -> None:
    obj = {
        "apiVersion": "v1",
        "kind": "Foo",
        "metadata": {"name": "n", "uid": "zzz", "resourceVersion": "1"},
        "status": {"x": 1},
        "spec": {"a": "b"},
    }
    normalized = normalize_for_compare(obj)
    assert "status" not in normalized
    assert "uid" not in normalized["metadata"]
    assert "spec" in normalized


def test_normalize_strips_helm_install_annotations_only() -> None:
    manifest_side = {"metadata": {"name": "traefik"}}
    live_side = {
        "metadata": {
            "name": "traefik",
            "annotations": {
                "meta.helm.sh/release-name": "traefik",
                "meta.helm.sh/release-namespace": "kube-system",
            },
        },
    }
    assert normalize_for_compare(manifest_side) == normalize_for_compare(live_side)


def test_normalize_strips_kubectl_restarted_at() -> None:
    m = {
        "metadata": {
            "name": "d",
            "annotations": {"kubectl.kubernetes.io/restartedAt": "2026-04-07T07:55:15Z"},
        }
    }
    l = {"metadata": {"name": "d"}}
    assert normalize_for_compare(m) == normalize_for_compare(l)


def test_normalize_strips_helm_managed_by_label() -> None:
    m = {"metadata": {"name": "d", "labels": {"app": "x"}}}
    l = {
        "metadata": {
            "name": "d",
            "labels": {"app": "x", "app.kubernetes.io/managed-by": "Helm"},
        }
    }
    assert normalize_for_compare(m) == normalize_for_compare(l)


def test_normalize_strips_service_runtime_fields() -> None:
    minimal = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "web"},
        "spec": {
            "type": "NodePort",
            "selector": {"app": "web"},
            "ports": [{"name": "web", "port": 80, "targetPort": 8080, "protocol": "TCP"}],
        },
    }
    noisy = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "web"},
        "spec": {
            "type": "NodePort",
            "clusterIP": "10.43.116.139",
            "clusterIPs": ["10.43.116.139"],
            "internalTrafficPolicy": "Cluster",
            "ipFamilies": ["IPv4"],
            "ipFamilyPolicy": "SingleStack",
            "selector": {"app": "web"},
            "ports": [
                {
                    "name": "web",
                    "port": 80,
                    "targetPort": 8080,
                    "protocol": "TCP",
                    "nodePort": 31592,
                }
            ],
        },
    }
    assert normalize_for_compare(minimal, drift_side="manifest") == normalize_for_compare(
        noisy,
        drift_side="live",
    )


def test_normalize_keeps_manifest_node_port_for_compare() -> None:
    manifest_svc = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "web"},
        "spec": {
            "type": "NodePort",
            "selector": {"app": "web"},
            "ports": [{"name": "web", "port": 80, "nodePort": 30007, "protocol": "TCP"}],
        },
    }
    live_svc = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "web"},
        "spec": {
            "type": "NodePort",
            "clusterIP": "10.0.0.5",
            "selector": {"app": "web"},
            "ports": [{"name": "web", "port": 80, "nodePort": 31592, "protocol": "TCP"}],
        },
    }
    assert normalize_for_compare(manifest_svc, drift_side="manifest") != normalize_for_compare(
        live_svc,
        drift_side="live",
    )


def test_unified_yaml_diff_ssa_uses_merged_prefix() -> None:
    diff = _unified_yaml_diff(
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x"}, "data": {"a": "1"}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x"}, "data": {"a": "2"}},
        api_version="v1",
        kind="ConfigMap",
        namespace="ns",
        name="x",
        compare_method="ssa",
    )
    assert "merged/v1/ConfigMap/ns/x" in diff
    assert "live/v1/ConfigMap/ns/x" in diff


def test_unified_yaml_diff_headers_include_resource_identity() -> None:
    diff = _unified_yaml_diff(
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cm"}},
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "cm"},
            "data": {"x": "y"},
        },
        api_version="v1",
        kind="ConfigMap",
        namespace="kube-system",
        name="cm",
    )
    lines = diff.splitlines()
    assert lines[0].startswith("--- manifest/")
    assert "v1/ConfigMap/kube-system/cm" in lines[0]
    assert lines[1].startswith("+++ live/")
    assert "v1/ConfigMap/kube-system/cm" in lines[1]


def test_unified_yaml_diff_headers_cluster_scoped_name() -> None:
    diff = _unified_yaml_diff(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "admin"},
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "admin"},
            "rules": [],
        },
        api_version="rbac.authorization.k8s.io/v1",
        kind="ClusterRole",
        namespace="",
        name="admin",
    )
    assert "cluster-scoped" in diff.splitlines()[0]


def test_normalize_strips_service_session_affinity_none() -> None:
    bare = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "s"}, "spec": {"ports": []}}
    with_none = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "s"},
        "spec": {"ports": [], "sessionAffinity": None},
    }
    with_str = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "s"},
        "spec": {"ports": [], "sessionAffinity": "None"},
    }
    assert normalize_for_compare(bare, drift_side="manifest") == normalize_for_compare(
        with_none,
        drift_side="live",
    )
    assert normalize_for_compare(bare, drift_side="manifest") == normalize_for_compare(
        with_str,
        drift_side="live",
    )


def test_normalize_strips_metadata_namespace_for_compare() -> None:
    a = {"metadata": {"name": "cm", "namespace": "alpha"}, "data": {"x": "1"}}
    b = {"metadata": {"name": "cm", "namespace": "beta"}, "data": {"x": "1"}}
    assert normalize_for_compare(a, drift_side="manifest") == normalize_for_compare(
        b,
        drift_side="live",
    )


def test_normalize_null_metadata_annotations_like_absent() -> None:
    a = {"metadata": {"name": "cm"}, "data": {"x": "1"}}
    b = {"metadata": {"name": "cm", "annotations": None}, "data": {"x": "1"}}
    assert normalize_for_compare(a) == normalize_for_compare(b)


def test_normalize_null_metadata_labels_like_absent() -> None:
    a = {"metadata": {"name": "cm"}, "data": {"x": "1"}}
    b = {"metadata": {"name": "cm", "labels": None}, "data": {"x": "1"}}
    assert normalize_for_compare(a) == normalize_for_compare(b)


def test_normalize_pod_restart_policy_always_and_empty_resources() -> None:
    minimal = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "p"},
        "spec": {"containers": [{"name": "c", "image": "img:latest"}]},
    }
    noisy = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "p"},
        "spec": {
            "restartPolicy": "Always",
            "containers": [
                {"name": "c", "image": "img:latest", "resources": {}},
            ],
        },
    }
    assert normalize_for_compare(minimal, drift_side="manifest") == normalize_for_compare(
        noisy,
        drift_side="live",
    )


def test_normalize_strips_deployment_controller_and_pod_defaults() -> None:
    minimal = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "x"},
        "spec": {
            "selector": {"matchLabels": {"app": "x"}},
            "template": {
                "metadata": {"labels": {"app": "x"}},
                "spec": {
                    "serviceAccountName": "sa-x",
                    "containers": [{"name": "c", "image": "img:latest"}],
                },
            },
        },
    }
    noisy = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "x",
            "annotations": {"deployment.kubernetes.io/revision": "4"},
        },
        "spec": {
            "progressDeadlineSeconds": 600,
            "selector": {"matchLabels": {"app": "x"}},
            "template": {
                "metadata": {"labels": {"app": "x"}},
                "spec": {
                    "restartPolicy": "Always",
                    "schedulerName": "default-scheduler",
                    "hostNetwork": False,
                    "dnsPolicy": "ClusterFirst",
                    "terminationGracePeriodSeconds": 30,
                    "securityContext": {},
                    "serviceAccount": "sa-x",
                    "serviceAccountName": "sa-x",
                    "containers": [
                        {
                            "name": "c",
                            "image": "img:latest",
                            "terminationMessagePath": "/dev/termination-log",
                            "terminationMessagePolicy": "File",
                            "securityContext": {},
                            "resources": {},
                        }
                    ],
                },
            },
        },
    }
    assert normalize_for_compare(minimal, drift_side="manifest") == normalize_for_compare(
        noisy,
        drift_side="live",
    )


def test_effective_spec_replicas_defaults_when_omitted() -> None:
    deployment = {
        "kind": "Deployment",
        "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "i"}]}}},
    }
    assert effective_spec_replicas(deployment) == 1
    assert effective_spec_replicas({"kind": "ConfigMap", "data": {}}) is None


def test_replicas_mismatch_detects_scaled_deployment() -> None:
    manifest = {
        "kind": "Deployment",
        "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "c", "image": "i"}]}}},
    }
    live = copy.deepcopy(manifest)
    live["spec"]["replicas"] = 5
    assert replicas_mismatch(manifest, live)
    assert normalize_for_compare(manifest, drift_side="manifest") != normalize_for_compare(
        live, drift_side="live"
    )


def test_ssa_merged_object_calls_server_side_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "cm", "namespace": "ns"},
        "data": {"k": "v"},
    }
    calls: list[dict] = []

    class _Res:
        namespaced = True

    class _Dyn:
        resources = type("R", (), {"get": staticmethod(lambda **_: _Res())})()

        def server_side_apply(self, resource, body=None, **kwargs):  # noqa: ANN001, ARG002
            calls.append(kwargs)
            return type("I", (), {"to_dict": lambda self: body})()

    merged = ssa_merged_object(
        _Dyn(),
        manifest,
        resolved_namespace="ns",
        field_manager="helm",
    )
    assert merged["data"]["k"] == "v"
    assert calls[0]["dry_run"] == "All"
    assert calls[0]["field_manager"] == "helm"


def test_ssa_merged_object_raises_ssa_unsupported_on_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kubernetes.client.rest import ApiException
    from kubernetes.dynamic.exceptions import DynamicApiError

    class _Res:
        namespaced = True

    class _Dyn:
        resources = type("R", (), {"get": staticmethod(lambda **_: _Res())})()

        def server_side_apply(self, *args, **kwargs):  # noqa: ANN002, ARG002
            api_exc = ApiException(status=415, reason="Unsupported Media Type")
            api_exc.body = "must use application/apply-patch+yaml"
            raise DynamicApiError(api_exc)

    with pytest.raises(SSAUnsupportedError):
        ssa_merged_object(
            _Dyn(),
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "cm"},
            },
            resolved_namespace="ns",
        )


def test_run_drift_detects_deployment_replica_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_doc = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "app", "namespace": "monitoring"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "app"}},
            "template": {
                "metadata": {"labels": {"app": "app"}},
                "spec": {"containers": [{"name": "c", "image": "img:latest"}]},
            },
        },
    }
    release = {
        "name": "prometheus",
        "manifest": "---\n" + yaml.dump(manifest_doc, default_flow_style=False),
    }

    def _fetch(_dyn: object, obj: dict, *, release_namespace: str) -> tuple:  # noqa: ARG001
        live = copy.deepcopy(obj)
        live["spec"]["replicas"] = 3
        return live, None

    monkeypatch.setattr("helmadm.drift.fetch_live_object", _fetch)
    monkeypatch.setattr(
        "helmadm.drift.ssa_merged_object", _mock_ssa_returns_manifest
    )

    dyn = FakeDynamicClient()
    report = run_drift(
        dyn,
        release,
        release_namespace="monitoring",
        release_name="prometheus",
        detect_extras=False,
    )
    assert report.has_problem
    dep = next(it for it in report.items if it.kind == "Deployment")
    assert dep.severity == "drift"
    assert dep.compare_method == "ssa"
    assert "spec.replicas differs" in dep.detail
    assert "merged" in dep.detail
    assert dep.diff and "replicas" in dep.diff
    assert "merged/" in dep.diff


def test_should_skip_namespace_system_objects() -> None:
    assert _should_skip_extras_list_item(
        {"kind": "ConfigMap", "metadata": {"name": "kube-root-ca.crt"}},
    )
    assert _should_skip_extras_list_item(
        {"kind": "ServiceAccount", "metadata": {"name": "default"}},
    )


def test_should_skip_helm_release_storage_secret_from_extras_scan() -> None:
    assert _should_skip_extras_list_item(
        {"kind": "Secret", "type": "helm.sh/release.v1", "metadata": {"name": "x"}},
    )
    assert not _should_skip_extras_list_item(
        {"kind": "Secret", "type": "Opaque", "metadata": {"name": "x"}},
    )
    assert not _should_skip_extras_list_item(
        {"kind": "ConfigMap", "metadata": {"name": "x"}},
    )


def test_should_not_skip_manual_secret_with_owner_reference() -> None:
    assert not _should_skip_extras_list_item(
        {
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": "my-secret",
                "ownerReferences": [{"kind": "Something", "name": "x"}],
            },
        },
    )


def test_is_managed_by_helm_release_matches_labels() -> None:
    md = {
        "labels": {
            "meta.helm.sh/release-name": "prometheus",
            "meta.helm.sh/release-namespace": "monitoring",
        }
    }
    assert _is_managed_by_helm_release(
        md, release_name="prometheus", release_namespace="monitoring"
    )
    assert not _is_managed_by_helm_release(
        md, release_name="other", release_namespace="monitoring"
    )
    assert not _is_managed_by_helm_release(
        {"labels": {"app": "x"}}, release_name="prometheus", release_namespace="monitoring"
    )


def test_collect_extras_live_finds_non_helm_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Res:
        kind = "ConfigMap"
        api_version = "v1"
        group_version = "v1"
        namespaced = True

    listed = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "helm-cm",
                    "namespace": "monitoring",
                    "labels": {
                        "meta.helm.sh/release-name": "prometheus",
                        "meta.helm.sh/release-namespace": "monitoring",
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "manual-install",
                    "namespace": "monitoring",
                },
            },
        ],
    }

    class _Dyn:
        def get(self, resource, namespace=None, **kwargs):  # noqa: ANN001, ARG002
            return type("I", (), {"to_dict": lambda self: listed})()

    monkeypatch.setattr(
        "helmadm.drift._iter_extras_api_resources",
        lambda _dyn: [("v1", "ConfigMap", _Res())],
    )

    extras, errs = _collect_extras_live(
        _Dyn(),
        release_namespace="monitoring",
        release_name="prometheus",
        manifest_keys=set(),
    )
    assert not errs
    assert len(extras) == 1
    assert extras[0].name == "manual-install"


def test_collect_extras_skips_objects_in_release_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Res:
        kind = "Deployment"
        api_version = "apps/v1"
        group_version = "apps/v1"
        namespaced = True

    listed = {
        "apiVersion": "apps/v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "provisioner", "namespace": "openebs"},
            },
        ],
    }

    class _Dyn:
        def get(self, resource, namespace=None, **kwargs):  # noqa: ANN001, ARG002
            return type("I", (), {"to_dict": lambda self: listed})()

    monkeypatch.setattr(
        "helmadm.drift._iter_extras_api_resources",
        lambda _dyn: [("apps/v1", "Deployment", _Res())],
    )

    extras, errs = _collect_extras_live(
        _Dyn(),
        release_namespace="openebs",
        release_name="openebs",
        manifest_keys={("apps/v1", "Deployment", "openebs", "provisioner")},
    )
    assert not errs
    assert extras == []


def test_run_drift_manifest_matches_live(monkeypatch: pytest.MonkeyPatch, sample_release: dict) -> None:
    monkeypatch.setattr(
        "helmadm.drift.fetch_live_object",
        lambda dyn, obj, *, release_namespace: (copy.deepcopy(obj), None),
    )
    monkeypatch.setattr(
        "helmadm.drift.ssa_merged_object", _mock_ssa_returns_manifest
    )
    dyn = FakeDynamicClient()
    report = run_drift(
        dyn,
        sample_release,
        release_namespace="monitoring",
        release_name="prometheus",
        detect_extras=False,
    )
    assert not report.has_problem
    assert len(report.items) == 1
    assert report.items[0].severity == "ok"


def test_run_drift_detects_difference(monkeypatch: pytest.MonkeyPatch, sample_release: dict) -> None:
    def _fake_fetch(dyn: object, obj: dict, *, release_namespace: str) -> tuple:  # noqa: ARG001
        live = copy.deepcopy(obj)
        live.setdefault("data", {})["key"] = "CHANGED"
        return live, None

    monkeypatch.setattr("helmadm.drift.fetch_live_object", _fake_fetch)
    monkeypatch.setattr(
        "helmadm.drift.ssa_merged_object", _mock_ssa_returns_manifest
    )

    dyn = FakeDynamicClient()
    report = run_drift(
        dyn,
        sample_release,
        release_namespace="monitoring",
        release_name="prometheus",
        detect_extras=False,
    )
    assert report.has_problem
    assert report.items[0].severity == "drift"
    assert report.items[0].compare_method == "ssa"
    assert report.items[0].diff
    assert "merged/v1/ConfigMap/monitoring/drift-cm" in report.items[0].diff


def test_run_drift_ssa_fallback_to_legacy(
    monkeypatch: pytest.MonkeyPatch, sample_release: dict
) -> None:
    monkeypatch.setattr(
        "helmadm.drift.fetch_live_object",
        lambda dyn, obj, *, release_namespace: (copy.deepcopy(obj), None),
    )

    def _ssa_fail(*_a, **_k):  # noqa: ANN002
        raise SSAUnsupportedError("CRD does not support apply-patch")

    monkeypatch.setattr("helmadm.drift.ssa_merged_object", _ssa_fail)

    dyn = FakeDynamicClient()
    report = run_drift(
        dyn,
        sample_release,
        release_namespace="monitoring",
        release_name="prometheus",
        detect_extras=False,
        verbose=True,
    )
    assert not report.has_problem
    assert report.items[0].compare_method == "legacy"
    assert report.items[0].legacy_fallback_reason


def test_run_drift_legacy_mode_unchanged(
    monkeypatch: pytest.MonkeyPatch, sample_release: dict
) -> None:
    def _fake_fetch(dyn: object, obj: dict, *, release_namespace: str) -> tuple:  # noqa: ARG001
        live = copy.deepcopy(obj)
        live.setdefault("data", {})["key"] = "CHANGED"
        return live, None

    monkeypatch.setattr("helmadm.drift.fetch_live_object", _fake_fetch)

    dyn = FakeDynamicClient()
    report = run_drift(
        dyn,
        sample_release,
        release_namespace="monitoring",
        release_name="prometheus",
        detect_extras=False,
        compare_mode="legacy",
    )
    assert report.has_problem
    assert report.items[0].compare_method == "legacy"
    assert "manifest/v1/ConfigMap/monitoring/drift-cm" in (report.items[0].diff or "")


def test_run_drift_extras_flag(monkeypatch: pytest.MonkeyPatch, sample_release: dict) -> None:
    monkeypatch.setattr(
        "helmadm.drift.fetch_live_object",
        lambda dyn, obj, *, release_namespace: (copy.deepcopy(obj), None),
    )
    monkeypatch.setattr(
        "helmadm.drift.ssa_merged_object", _mock_ssa_returns_manifest
    )

    monkeypatch.setattr(
        "helmadm.drift._collect_extras_live",
        lambda *_a, **_k: (
            [
                ExtraObjectResult(
                    api_version="v1",
                    kind="Secret",
                    namespace="monitoring",
                    name="orphan-secret",
                )
            ],
            [],
        ),
    )

    dyn = FakeDynamicClient()
    report = run_drift(
        dyn,
        sample_release,
        release_namespace="monitoring",
        release_name="prometheus",
        detect_extras=True,
    )
    assert report.has_problem
    assert any(e.name == "orphan-secret" for e in report.extras)


def test_drift_ssa_annotation_lines() -> None:
    notes = drift_ssa_annotation_lines()
    assert any("SSA dry-run merged" in line for line in notes)
    assert any("managedFields" in line for line in notes)
    assert any("managed-by" in line for line in notes)


def test_drift_ignore_annotation_lines_service_includes_service_rules() -> None:
    svc_notes = drift_ignore_annotation_lines("Service")
    assert any("Service spec:" in line for line in svc_notes)
    assert any("clusterIP" in line for line in svc_notes)

    cm_notes = drift_ignore_annotation_lines("ConfigMap")
    assert not any("Service spec:" in line for line in cm_notes)


def test_format_report_text_optional_ignore_annotations_before_diff() -> None:
    report = DriftReport(
        release_name="r",
        namespace="ns",
        compare_mode="legacy",
        items=[
            ManifestObjectResult(
                api_version="v1",
                kind="ConfigMap",
                namespace="ns",
                name="x",
                severity="drift",
                detail="differs",
                compare_method="legacy",
                diff="--- a\n+++ b\n",
            )
        ],
    )
    plain = format_report_text(report)
    assert "# helmadm:" not in plain

    ann = format_report_text(report, ignore_annotations=True)
    assert "--ignore-annotations / -ia" in ann
    idx_header = ann.index("[drift]")
    idx_note = ann.index("# helmadm: Unified diff below")
    idx_diff = ann.index("--- a")
    assert idx_header < idx_note < idx_diff


def test_format_report_text_ssa_ignore_annotations() -> None:
    report = DriftReport(
        release_name="r",
        namespace="ns",
        compare_mode="ssa",
        items=[
            ManifestObjectResult(
                api_version="v1",
                kind="ConfigMap",
                namespace="ns",
                name="x",
                severity="drift",
                detail="differs",
                compare_method="ssa",
                diff="--- a\n+++ b\n",
            )
        ],
    )
    ann = format_report_text(report, ignore_annotations=True)
    assert "compare-mode=ssa" in ann
    assert "SSA dry-run merged" in ann


def test_format_report_text_contains_result() -> None:
    report = DriftReport(
        release_name="r",
        namespace="ns",
        items=[
            ManifestObjectResult(
                api_version="v1",
                kind="ConfigMap",
                namespace="ns",
                name="x",
                severity="ok",
            )
        ],
    )
    out = format_report_text(report)
    assert "RESULT: in sync" in out
    assert "[ok]" in out


def test_cli_drift_help() -> None:
    result = runner.invoke(app, ["drift", "--help"])
    assert result.exit_code == 0
    assert "merged" in result.stdout.lower()
    assert "--compare-mode" in result.stdout
    assert "--field-manager" in result.stdout
    assert "--ignore-annotations" in result.stdout
    assert "-ia" in result.stdout


def test_cli_drift_sync_exit_zero(
    monkeypatch: pytest.MonkeyPatch, sample_release: dict, clean_env
) -> None:
    monkeypatch.setenv(ENV_NAMESPACE, "monitoring")
    monkeypatch.delenv(ENV_RELEASE_NAME, raising=False)
    monkeypatch.setattr(
        "helmadm.drift.fetch_live_object",
        lambda dyn, obj, *, release_namespace: (copy.deepcopy(obj), None),
    )
    monkeypatch.setattr(
        "helmadm.drift.ssa_merged_object", _mock_ssa_returns_manifest
    )

    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch("helmadm.cli.check_kubernetes_accessible"),
        patch("helmadm.cli.get_release", return_value=sample_release),
        patch("helmadm.cli.load_dynamic_client", return_value=FakeDynamicClient()),
    ):
        result = runner.invoke(app, ["drift", "-n", "monitoring", "prometheus"])

    assert result.exit_code == 0
    assert "[ok]" in result.stdout
    assert "compare-mode=ssa" in result.stdout


def test_cli_drift_ignore_annotations(
    monkeypatch: pytest.MonkeyPatch, sample_release: dict, clean_env
) -> None:
    monkeypatch.setenv(ENV_NAMESPACE, "monitoring")
    monkeypatch.delenv(ENV_RELEASE_NAME, raising=False)

    def _fetch(_dyn, obj, *, release_namespace):  # noqa: ANN001
        live = copy.deepcopy(obj)
        if live.get("kind") == "ConfigMap" and isinstance(live.get("data"), dict):
            live["data"]["patched"] = "1"
        return live, None

    monkeypatch.setattr("helmadm.drift.fetch_live_object", _fetch)
    monkeypatch.setattr(
        "helmadm.drift.ssa_merged_object", _mock_ssa_returns_manifest
    )

    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch("helmadm.cli.check_kubernetes_accessible"),
        patch("helmadm.cli.get_release", return_value=sample_release),
        patch("helmadm.cli.load_dynamic_client", return_value=FakeDynamicClient()),
    ):
        result = runner.invoke(
            app,
            [
                "drift",
                "-ia",
                "-n",
                "monitoring",
                "prometheus",
            ],
        )

    assert result.exit_code == 1
    assert "compares SSA dry-run merged vs live" in result.stdout
    assert "--ignore-annotations / -ia" in result.stdout


def test_fetch_live_object_uses_dyn_get_when_discovery_returns_resource_list() -> None:
    """ResourceList defines get(body); live fetch must use DynamicClient.get instead."""
    from kubernetes.dynamic.resource import Resource, ResourceList

    manifest_obj = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "Prometheus",
        "metadata": {
            "name": "prometheus-kube-prometheus-prometheus",
            "namespace": "monitoring",
        },
    }
    from unittest.mock import MagicMock

    k8s_client = MagicMock()
    base = Resource(
        prefix="apis",
        group="monitoring.coreos.com",
        api_version="v1",
        kind="Prometheus",
        namespaced=True,
        verbs=["get"],
        name="prometheuses",
        client=k8s_client,
    )
    resource_list = ResourceList(
        k8s_client,
        group="monitoring.coreos.com",
        api_version="v1",
        base_kind="Prometheus",
        kind="PrometheusList",
    )
    k8s_client.resources.get.side_effect = lambda **kw: (
        resource_list
        if kw.get("kind") == "Prometheus"
        and kw.get("api_version") == "monitoring.coreos.com/v1"
        else base
    )

    class _Dyn:
        def get(self, resource, name=None, namespace=None, **_kw):  # noqa: ANN001
            assert resource is base
            assert name == manifest_obj["metadata"]["name"]
            assert namespace == "monitoring"
            return type("Inst", (), {"to_dict": lambda self: manifest_obj})()

        resources = k8s_client.resources

    live, err = fetch_live_object(_Dyn(), manifest_obj, release_namespace="monitoring")
    assert err is None
    assert live == manifest_obj


def test_cli_empty_manifest_reports_error(
    monkeypatch: pytest.MonkeyPatch, sample_release: dict, clean_env
) -> None:
    empty = dict(sample_release)
    empty.pop("manifest", None)
    monkeypatch.setenv(ENV_NAMESPACE, "monitoring")

    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch("helmadm.cli.check_kubernetes_accessible"),
        patch("helmadm.cli.get_release", return_value=empty),
    ):
        result = runner.invoke(app, ["drift", "-n", "monitoring", "prometheus"])

    assert result.exit_code == 1
    assert "manifest" in result.stderr.lower()
