"""Tests for manifest vs live Helm drift comparisons."""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from helmadm.cli import app
from helmadm.drift import (
    DriftReport,
    ManifestObjectResult,
    _should_skip_extras_list_item,
    _unified_yaml_diff,
    drift_ignore_annotation_lines,
    format_report_text,
    normalize_for_compare,
    parse_release_manifest,
    run_drift,
)
from helmadm.env import ENV_NAMESPACE, ENV_RELEASE_NAME

runner = CliRunner()


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


def test_should_skip_helm_release_storage_secret_from_extras_scan() -> None:
    assert _should_skip_extras_list_item({"kind": "Secret", "type": "helm.sh/release.v1"})
    assert not _should_skip_extras_list_item({"kind": "Secret", "type": "Opaque"})
    assert not _should_skip_extras_list_item({"kind": "ConfigMap"})


def test_run_drift_manifest_matches_live(monkeypatch: pytest.MonkeyPatch, sample_release: dict) -> None:
    monkeypatch.setattr(
        "helmadm.drift.fetch_live_object",
        lambda dyn, obj, *, release_namespace: (copy.deepcopy(obj), None),
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
    assert report.items[0].diff
    assert "manifest/v1/ConfigMap/monitoring/drift-cm" in report.items[0].diff


def test_run_drift_extras_flag(monkeypatch: pytest.MonkeyPatch, sample_release: dict) -> None:
    monkeypatch.setattr(
        "helmadm.drift.fetch_live_object",
        lambda dyn, obj, *, release_namespace: (copy.deepcopy(obj), None),
    )

    monkeypatch.setattr(
        "helmadm.drift._collect_extras_live",
        lambda *_a, **_k: ([("v1", "Pod", "monitoring", "orphan-pod")], []),
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
    assert ("v1", "Pod", "monitoring", "orphan-pod") in report.extras


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
        items=[
            ManifestObjectResult(
                api_version="v1",
                kind="ConfigMap",
                namespace="ns",
                name="x",
                severity="drift",
                detail="differs",
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
    assert "manifest" in result.stdout.lower()
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

    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch("helmadm.cli.check_kubernetes_accessible"),
        patch("helmadm.cli.get_release", return_value=sample_release),
        patch("helmadm.cli.load_dynamic_client", return_value=FakeDynamicClient()),
    ):
        result = runner.invoke(app, ["drift", "-n", "monitoring", "prometheus"])

    assert result.exit_code == 0
    assert "[ok]" in result.stdout


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
    assert "# helmadm: Unified diff below" in result.stdout
    assert "--ignore-annotations / -ia" in result.stdout


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
