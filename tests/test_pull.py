import base64
import gzip
import json
import tarfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from helmadm.cli import PullCliOptions, app, run_pull
from helmadm.helm_release import (
    HELM_RELEASE_DATA_KEY,
    HelmReleaseNotFoundError,
    find_release_secret,
)
from helmadm.pull_bundle import (
    METADATA_FILENAME,
    PullBundleExistsError,
    PullKubernetesContext,
    build_pull_bundle,
    build_pull_bundle_files,
    build_pull_metadata,
    bundle_path,
    derive_repo_alias,
    write_pull_bundle,
    write_pull_bundle_tar,
)
from typer.testing import CliRunner

runner = CliRunner()

SAMPLE_RELEASE = {
    "name": "fluentbit",
    "namespace": "loki",
    "config": {"kind": "DaemonSet"},
    "chart": {
        "metadata": {
            "name": "fluent-bit",
            "version": "0.57.5",
            "repoURL": "https://fluent.github.io/helm-charts",
        },
        "values": {},
    },
}

K8S_CTX = PullKubernetesContext(
    kubeconfig="/path/to/kubeconfig",
    context="my-cluster",
    pulled_at="2026-05-16T12:00:00+00:00",
)


def _encode_release(release: dict) -> str:
    payload = gzip.compress(json.dumps(release).encode())
    return base64.b64encode(base64.b64encode(payload)).decode()


def _make_secret(revision: int, release: dict | None = None) -> MagicMock:
    payload = _encode_release(release or SAMPLE_RELEASE)
    secret = MagicMock()
    secret.metadata.name = f"sh.helm.release.v1.fluentbit.v{revision}"
    secret.metadata.labels = {
        "owner": "helm",
        "name": "fluentbit",
        "version": str(revision),
        "status": "deployed",
    }
    secret.type = "helm.sh/release.v1"
    secret.data = {HELM_RELEASE_DATA_KEY: payload}
    return secret


def test_derive_repo_alias() -> None:
    assert (
        derive_repo_alias("https://fluent.github.io/helm-charts") == "fluent"
    )
    assert (
        derive_repo_alias(
            "https://prometheus-community.github.io/helm-charts"
        )
        == "prometheus-community"
    )
    assert derive_repo_alias("https://charts.example.com/stable") == "charts"


def test_bundle_path_sanitizes() -> None:
    assert bundle_path(Path("/out"), "loki", "fluentbit") == Path(
        "/out/loki/fluentbit"
    )
    assert bundle_path(Path("/out"), "ns/prod", "my release") == Path(
        "/out/ns-prod/my-release"
    )


def test_build_pull_metadata() -> None:
    bundle = build_pull_bundle(
        namespace="loki",
        release_name="fluentbit",
        helm_revision=3,
        chart_name="fluent-bit",
        chart_version="0.57.5",
        repo_url="https://fluent.github.io/helm-charts",
    )
    meta = build_pull_metadata(
        bundle, kubernetes=K8S_CTX, values_strategy="remote.diff"
    )
    assert meta["pulledAt"] == K8S_CTX.pulled_at
    assert meta["kubernetes"]["kubeconfig"] == "/path/to/kubeconfig"
    assert meta["kubernetes"]["context"] == "my-cluster"
    assert meta["release"]["helmRevision"] == 3
    assert meta["chart"]["name"] == "fluent-bit"
    assert meta["values"]["strategy"] == "remote.diff"


def test_build_pull_bundle_files_diff() -> None:
    bundle = build_pull_bundle(
        namespace="loki",
        release_name="fluentbit",
        helm_revision=3,
        chart_name="fluent-bit",
        chart_version="0.57.5",
        repo_url="https://fluent.github.io/helm-charts",
    )
    files = build_pull_bundle_files(
        bundle,
        cluster_values={"kind": "DaemonSet", "image": {"tag": "2.0"}},
        values_object={"kind": "DaemonSet"},
        remote_defaults={"kind": "Deployment", "image": {"tag": "1.0"}},
        kubernetes=K8S_CTX,
        values_strategy="remote.diff",
    )
    all_values = yaml.safe_load(files.all_values_yaml)
    changed = yaml.safe_load(files.changed_values_yaml)
    remote = yaml.safe_load(files.remote_all_values_yaml)
    metadata = yaml.safe_load(files.metadata_yaml)
    assert all_values["kind"] == "DaemonSet"
    assert changed == {"kind": "DaemonSet"}
    assert remote["kind"] == "Deployment"
    assert metadata["release"]["name"] == "fluentbit"
    assert "helm upgrade -i fluentbit" in files.readme
    assert "fluent-bit.changed.values.yaml" in files.readme
    assert "fluent-bit.all.values.yaml" in files.readme
    assert "fluent-bit.remote-all.values.yaml" in files.readme
    assert "-n loki" in files.readme
    assert "--version 0.57.5" in files.readme


def test_write_pull_bundle_creates_files(tmp_path: Path) -> None:
    bundle = build_pull_bundle(
        namespace="loki",
        release_name="fluentbit",
        helm_revision=3,
        chart_name="fluent-bit",
        chart_version="0.57.5",
        repo_url="https://fluent.github.io/helm-charts",
    )
    files = build_pull_bundle_files(
        bundle,
        cluster_values={},
        values_object={},
        remote_defaults={},
        kubernetes=K8S_CTX,
    )
    bundle_dir = write_pull_bundle(tmp_path, bundle, files)
    assert bundle_dir == tmp_path / "loki" / "fluentbit"
    assert (bundle_dir / "fluent-bit.all.values.yaml").is_file()
    assert (bundle_dir / "fluent-bit.changed.values.yaml").is_file()
    assert (bundle_dir / "fluent-bit.remote-all.values.yaml").is_file()
    assert (bundle_dir / METADATA_FILENAME).is_file()
    assert (bundle_dir / "README.md").is_file()
    assert not (bundle_dir / "release.yaml").exists()


def test_write_pull_bundle_refuses_existing(tmp_path: Path) -> None:
    bundle = build_pull_bundle(
        namespace="loki",
        release_name="fluentbit",
        helm_revision=1,
        chart_name="fluent-bit",
        chart_version="0.57.5",
        repo_url="https://fluent.github.io/helm-charts",
    )
    files = build_pull_bundle_files(
        bundle,
        cluster_values={},
        values_object={},
        remote_defaults={},
        kubernetes=K8S_CTX,
    )
    write_pull_bundle(tmp_path, bundle, files)
    with pytest.raises(PullBundleExistsError):
        write_pull_bundle(tmp_path, bundle, files, force=False)


def test_write_pull_bundle_force_overwrites(tmp_path: Path) -> None:
    bundle = build_pull_bundle(
        namespace="loki",
        release_name="fluentbit",
        helm_revision=1,
        chart_name="fluent-bit",
        chart_version="0.57.5",
        repo_url="https://fluent.github.io/helm-charts",
    )
    files = build_pull_bundle_files(
        bundle,
        cluster_values={"x": 1},
        values_object={},
        remote_defaults={},
        kubernetes=K8S_CTX,
    )
    write_pull_bundle(tmp_path, bundle, files)
    files2 = build_pull_bundle_files(
        bundle,
        cluster_values={"x": 2},
        values_object={},
        remote_defaults={},
        kubernetes=K8S_CTX,
    )
    write_pull_bundle(tmp_path, bundle, files2, force=True)
    content = yaml.safe_load(
        (tmp_path / "loki" / "fluentbit" / "fluent-bit.all.values.yaml").read_text()
    )
    assert content == {"x": 2}


def test_write_pull_bundle_tar(tmp_path: Path) -> None:
    bundle = build_pull_bundle(
        namespace="loki",
        release_name="fluentbit",
        helm_revision=2,
        chart_name="fluent-bit",
        chart_version="0.57.5",
        repo_url="https://fluent.github.io/helm-charts",
    )
    files = build_pull_bundle_files(
        bundle,
        cluster_values={},
        values_object={},
        remote_defaults={},
        kubernetes=K8S_CTX,
    )
    bundle_dir = write_pull_bundle(tmp_path, bundle, files)
    tarball = write_pull_bundle_tar(tmp_path, bundle_dir)
    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as archive:
        names = archive.getnames()
    assert "loki/fluentbit/README.md" in names
    assert "loki/fluentbit/fluent-bit.changed.values.yaml" in names
    assert f"loki/fluentbit/{METADATA_FILENAME}" in names


def test_find_release_secret_revision() -> None:
    api = MagicMock()
    api.list_namespaced_secret.return_value.items = [
        _make_secret(1),
        _make_secret(2),
        _make_secret(3),
    ]
    secret = find_release_secret(api, "loki", "fluentbit", revision=2)
    assert secret.metadata.labels["version"] == "2"

    latest = find_release_secret(api, "loki", "fluentbit")
    assert latest.metadata.labels["version"] == "3"


def test_find_release_secret_missing_revision() -> None:
    api = MagicMock()
    api.list_namespaced_secret.return_value.items = [_make_secret(1)]
    with pytest.raises(HelmReleaseNotFoundError, match="available revisions"):
        find_release_secret(api, "loki", "fluentbit", revision=9)


def test_run_pull_writes_bundle(tmp_path: Path, clean_env, monkeypatch) -> None:
    monkeypatch.setenv("HELM_TO_ARGOCD_NAMESPACE", "loki")
    options = PullCliOptions(
        namespace="loki",
        release_name="fluentbit",
        output_parent=tmp_path,
        context="ctx-a",
    )
    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch(
            "helmadm.cli._load_release_and_values",
            return_value=(
                SAMPLE_RELEASE,
                "https://fluent.github.io/helm-charts",
                {"kind": "DaemonSet"},
                {"kind": "Deployment"},
                {"kind": "DaemonSet"},
                "remote.diff",
                3,
                "deployed",
            ),
        ),
    ):
        code = run_pull(options)
    assert code == 0
    bundle_dir = tmp_path / "loki" / "fluentbit"
    assert bundle_dir.is_dir()
    metadata = yaml.safe_load((bundle_dir / METADATA_FILENAME).read_text())
    assert metadata["kubernetes"]["context"] == "ctx-a"
    readme = (bundle_dir / "README.md").read_text()
    assert "helm upgrade -i fluentbit" in readme
    assert "changed.values.yaml" in readme


def test_run_pull_tar_stdout(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("HELM_TO_ARGOCD_NAMESPACE", "loki")
    options = PullCliOptions(
        namespace="loki",
        release_name="fluentbit",
        output_parent=Path("/unused"),
        tar=True,
    )
    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch(
            "helmadm.cli._load_release_and_values",
            return_value=(
                SAMPLE_RELEASE,
                "https://fluent.github.io/helm-charts",
                {},
                {},
                {},
                "empty",
                1,
                "deployed",
            ),
        ),
    ):
        code = run_pull(options)
    assert code == 0


def test_pull_cli_requires_output(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("HELM_TO_ARGOCD_NAMESPACE", "loki")
    monkeypatch.setenv("HELM_TO_ARGOCD_RELEASE_NAME", "fluentbit")
    result = runner.invoke(app, ["pull", "-n", "loki", "fluentbit"])
    assert result.exit_code == 2
    assert "--output" in result.stderr or "output" in result.stderr.lower()


def test_pull_cli_help_lists_options(clean_env) -> None:
    result = runner.invoke(app, ["pull", "--help"])
    assert result.exit_code == 0
    assert "--output" in result.stdout
    assert "--revision" in result.stdout
    assert "--tar" in result.stdout
    assert "{namespace}/{release}" in result.stdout


def test_root_help_lists_pull() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "pull" in result.stdout
