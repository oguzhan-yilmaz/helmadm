import pytest

from helmadm.argocd_manifest import (
    DEBUG_FIELD,
    PLACEHOLDER,
    RepoURLMissingError,
    build_application,
    chart_repo_url,
    needs_repo_url,
    normalize_repo_url,
    render_application,
    resolve_repo_url,
)
from helmadm.values_diff import (
    VALUES_DIFF_IGNORE_ANNOTATIONS,
    build_values_debug,
    cluster_values_from_release,
    resolve_values_object,
)


def test_normalize_repo_url_strips_oci_prefix():
    assert normalize_repo_url("oci://registry.example.com/charts") == (
        "registry.example.com/charts"
    )


def test_resolve_repo_url_from_release(sample_release):
    assert (
        resolve_repo_url(sample_release, None)
        == "https://prometheus-community.github.io/helm-charts"
    )


def test_resolve_repo_url_override(sample_release):
    assert resolve_repo_url(sample_release, "oci://registry.example.com/charts") == (
        "registry.example.com/charts"
    )


def test_resolve_repo_url_missing():
    release = {"chart": {"metadata": {"name": "x", "version": "1.0.0"}}}
    with pytest.raises(RepoURLMissingError):
        resolve_repo_url(release, None)


def test_needs_repo_url(sample_release):
    assert needs_repo_url(sample_release) is False
    assert chart_repo_url(sample_release) == (
        "https://prometheus-community.github.io/helm-charts"
    )


def test_needs_repo_url_when_missing():
    release = {"chart": {"metadata": {"name": "x", "version": "1.0.0"}}}
    assert needs_repo_url(release) is True
    assert chart_repo_url(release) is None


def test_build_application(sample_release):
    cluster = cluster_values_from_release(sample_release)
    values_object, _strategy = resolve_values_object(
        cluster,
        sample_release["chart"]["values"],
    )
    manifest = build_application(
        sample_release,
        values_object,
        "https://prometheus-community.github.io/helm-charts",
    )

    assert manifest["metadata"]["name"] == PLACEHOLDER
    assert manifest["spec"]["destination"]["server"] == PLACEHOLDER
    assert manifest["spec"]["syncPolicy"]["syncOptions"] == ["CreateNamespace=false"]
    assert "automated" not in manifest["spec"]["syncPolicy"]
    assert manifest["spec"]["source"]["chart"] == "prometheus"
    assert manifest["spec"]["source"]["targetRevision"] == "25.0.0"
    assert manifest["spec"]["source"]["helm"]["releaseName"] == "prometheus"
    assert manifest["spec"]["source"]["helm"]["valuesObject"] == {
        "server": {"retention": "30d"},
        "ingress": {"enabled": True},
    }


def test_render_application_multiline_strings_use_block_scalar(sample_release):
    manifest = build_application(
        sample_release,
        {
            "config": {
                "inputs": "[INPUT]\n    Name tail\n    Path /var/log/containers/*.log\n",
                "outputs": (
                    "[OUTPUT]\\n    name loki\\n    match *\\n"
                    "    # See logging-stack/loki.README.md \u2014 http://loki-gateway.loki.svc\\n"
                ),
                "extraFiles": {
                    "labelmap.json": '{\\n  "kubernetes": {"namespace_name": "namespace"}\\n}\\n',
                },
            }
        },
        "https://fluent.github.io/helm-charts",
    )
    rendered = render_application(manifest)

    assert 'inputs: "[INPUT]\\n' not in rendered
    assert 'outputs: "[OUTPUT]\\n' not in rendered
    assert "inputs: |\n" in rendered
    assert "outputs: |\n" in rendered
    assert "    Name tail\n" in rendered
    assert "    name loki\n" in rendered
    assert "labelmap.json: |\n" in rendered
    assert '{\\n  "kubernetes"' not in rendered


def test_render_application_uses_values_object_mapping(sample_release):
    manifest = build_application(sample_release, {}, "https://example.com/charts")
    rendered = render_application(manifest)

    assert "valuesObject:" in rendered
    assert "values:" not in rendered.split("valuesObject:")[0]
    assert "&id" not in rendered

    assert rendered.index("destination:") < rendered.index("syncPolicy:")
    assert rendered.index("syncPolicy:") < rendered.index("helm:")


def test_build_application_with_debug_block(sample_release):
    cluster = cluster_values_from_release(sample_release)
    remote = sample_release["chart"]["values"]
    values_object, strategy = resolve_values_object(cluster, remote)
    debug_info = build_values_debug(
        sample_release,
        sample_release["config"],
        sample_release["chart"]["values"],
        cluster,
        remote,
        values_object,
        strategy=strategy,
    )
    manifest = build_application(
        sample_release,
        values_object,
        "https://prometheus-community.github.io/helm-charts",
        debug=debug_info,
    )

    assert DEBUG_FIELD in manifest
    assert manifest[DEBUG_FIELD]["valuesFromCluster"]["config"] == sample_release[
        "config"
    ]
    assert manifest[DEBUG_FIELD]["remoteDefaults"] == remote
    assert manifest[DEBUG_FIELD]["diff"]["valuesObject"] == values_object
    assert manifest[DEBUG_FIELD]["diff"]["ignoreAnnotations"] == list(
        VALUES_DIFF_IGNORE_ANNOTATIONS
    )


def test_render_application_includes_debug_yaml(sample_release):
    debug_info = build_values_debug(
        sample_release,
        {},
        {},
        {},
        {},
        {},
        strategy="empty",
    )
    manifest = build_application(
        sample_release, {}, "https://example.com/charts", debug=debug_info
    )
    rendered = render_application(manifest)

    assert ".debug:" in rendered
    assert "valuesFromCluster:" in rendered
