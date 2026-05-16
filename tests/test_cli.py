from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from tests.conftest import make_load_release_and_values_result

from helmadm.cli import CliOptions, _kubernetes_client_kwargs, app, run
from helmadm.env import (
    ENV_NAMESPACE,
    ENV_RELEASE_NAME,
)
from helmadm.helm_release import HelmReleaseSummary
from helmadm.k8s import KubernetesApiError

runner = CliRunner()


def test_root_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "argocd-yaml" in result.stdout
    assert "pull" in result.stdout
    assert "ls" in result.stdout
    assert "drift" in result.stdout
    assert "--verbose" in result.stdout


def test_no_args_shows_root_help(clean_env):
    result = runner.invoke(app, [])
    # Click exits 2 when no subcommand is given (same as --help-style usage output).
    assert result.exit_code in (0, 2)
    assert "Usage:" in result.stdout
    assert "argocd-yaml" in result.stdout
    assert "pull" in result.stdout
    assert "ls" in result.stdout
    assert "drift" in result.stdout


def test_argocd_yaml_help_shows_examples():
    result = runner.invoke(app, ["argocd-yaml", "--help"])
    assert result.exit_code == 0
    assert "helmadm argocd-yaml" in result.stdout
    assert "RELEASE_NAME" in result.stdout or "release" in result.stdout.lower()
    assert "--debug" in result.stdout
    assert "--verbose" in result.stdout or "-v" in result.stdout


def test_argocd_yaml_accepts_verbose_after_arguments(clean_env, monkeypatch):
    import logging

    from helmadm.logging_config import PACKAGE_LOGGER

    monkeypatch.setenv(ENV_NAMESPACE, "kube-system")
    monkeypatch.setenv(ENV_RELEASE_NAME, "traefik")
    release = {
        "name": "traefik",
        "config": {},
        "chart": {
            "metadata": {
                "name": "traefik",
                "version": "1.0.0",
                "repoURL": "https://traefik.github.io/charts",
            },
            "values": {},
        },
    }
    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch(
            "helmadm.cli._load_release_and_values",
            return_value=make_load_release_and_values_result(release),
        ),
        patch("helmadm.cli.render_application", return_value=""),
    ):
        result = runner.invoke(
            app,
            [
                "argocd-yaml",
                "--repo-url",
                "https://traefik.github.io/charts",
                "-n",
                "kube-system",
                "traefik",
                "--verbose",
            ],
        )

    assert result.exit_code == 0
    assert logging.getLogger(PACKAGE_LOGGER).level == logging.DEBUG


def test_argocd_yaml_debug_includes_debug_block(clean_env, monkeypatch):
    release = {
        "name": "app",
        "config": {"foo": "bar"},
        "chart": {
            "metadata": {
                "name": "app",
                "version": "1.0.0",
                "repoURL": "https://charts.example.com",
            },
            "values": {"foo": "default"},
        },
    }

    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch(
            "helmadm.cli._load_release_and_values",
            return_value=make_load_release_and_values_result(
                release, remote_defaults={"foo": "default"}
            ),
        ),
    ):
        result = runner.invoke(app, ["argocd-yaml", "--debug", "-n", "ns", "app"])

    assert result.exit_code == 0
    assert ".debug:" in result.stdout
    assert "valuesFromCluster:" in result.stdout
    assert "config:" in result.stdout
    assert "foo: bar" in result.stdout
    assert "chartValues:" in result.stdout


def test_argocd_yaml_no_args_shows_help(clean_env):
    result = runner.invoke(app, ["argocd-yaml"])
    assert result.exit_code == 0
    assert "argocd-yaml" in result.stdout.lower()
    assert "Error:" not in result.stderr


def test_ls_help_shows_detail_flag():
    result = runner.invoke(app, ["ls", "--help"])
    assert result.exit_code == 0
    assert "--detail" in result.stdout
    assert "repo-url" in result.stdout.lower()


def test_parse_from_environment(clean_env, monkeypatch):
    monkeypatch.setenv(ENV_NAMESPACE, "monitoring")
    monkeypatch.setenv(ENV_RELEASE_NAME, "prometheus")

    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch("helmadm.cli._load_release_and_values") as load_values,
        patch("helmadm.cli.render_application", return_value=""),
    ):
        release = {
            "name": "prometheus",
            "config": {},
            "chart": {
                "metadata": {
                    "name": "prometheus",
                    "version": "1.0.0",
                    "repoURL": "https://charts.example.com",
                },
                "values": {},
            },
        }
        load_values.return_value = make_load_release_and_values_result(release)
        result = runner.invoke(app, ["argocd-yaml", "prometheus"])

    assert result.exit_code == 0


def test_kubeconfig_not_set_from_kubeconfig_env(clean_env, monkeypatch, tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    monkeypatch.setenv(ENV_NAMESPACE, "ns")
    monkeypatch.setenv(ENV_RELEASE_NAME, "app")

    with (
        patch("helmadm.cli.load_kubernetes_client") as load_client,
        patch("helmadm.cli._load_release_and_values") as load_values,
        patch("helmadm.cli.render_application", return_value=""),
    ):
        release = {
            "name": "app",
            "config": {},
            "chart": {
                "metadata": {
                    "name": "app",
                    "version": "1.0.0",
                    "repoURL": "https://charts.example.com",
                },
                "values": {},
            },
        }
        load_values.return_value = make_load_release_and_values_result(release)
        result = runner.invoke(app, ["argocd-yaml", "app"])

    assert result.exit_code == 0
    load_client.assert_called_once_with()


def test_explicit_kubeconfig_flag(clean_env, monkeypatch, tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setenv(ENV_NAMESPACE, "ns")
    monkeypatch.setenv(ENV_RELEASE_NAME, "app")

    with (
        patch("helmadm.cli.load_kubernetes_client") as load_client,
        patch("helmadm.cli._load_release_and_values") as load_values,
        patch("helmadm.cli.render_application", return_value=""),
    ):
        release = {
            "name": "app",
            "config": {},
            "chart": {
                "metadata": {
                    "name": "app",
                    "version": "1.0.0",
                    "repoURL": "https://charts.example.com",
                },
                "values": {},
            },
        }
        load_values.return_value = make_load_release_and_values_result(release)
        result = runner.invoke(
            app, ["argocd-yaml", "--kubeconfig", str(kubeconfig), "app"]
        )

    assert result.exit_code == 0
    load_client.assert_called_once_with(kubeconfig=str(kubeconfig))


def test_kubernetes_client_kwargs_omits_kubeconfig_without_flag():
    assert _kubernetes_client_kwargs(kubeconfig=None, context=None) == {}


def test_kubernetes_client_kwargs_includes_explicit_kubeconfig(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    assert _kubernetes_client_kwargs(kubeconfig=kubeconfig, context="prod") == {
        "kubeconfig": str(kubeconfig),
        "context": "prod",
    }


def test_cli_overrides_environment(clean_env, monkeypatch):
    monkeypatch.setenv(ENV_NAMESPACE, "from-env")
    monkeypatch.setenv(ENV_RELEASE_NAME, "from-env")

    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch("helmadm.cli._load_release_and_values") as load_values,
        patch("helmadm.cli.render_application", return_value=""),
    ):
        release = {
            "name": "from-cli-release",
            "config": {},
            "chart": {
                "metadata": {
                    "name": "app",
                    "version": "1.0.0",
                    "repoURL": "https://charts.example.com",
                },
                "values": {},
            },
        }
        load_values.return_value = make_load_release_and_values_result(release)
        result = runner.invoke(
            app, ["argocd-yaml", "-n", "from-cli", "from-cli-release"]
        )

    assert result.exit_code == 0
    load_values.assert_called_once()
    assert load_values.call_args[0][1:3] == ("from-cli", "from-cli-release")


def test_missing_namespace_exits_with_error(clean_env, monkeypatch):
    monkeypatch.setattr(
        "helmadm.cli.resolve_namespace",
        lambda *_args, **_kwargs: None,
    )
    result = runner.invoke(app, ["argocd-yaml", "prometheus"])
    assert result.exit_code == 2
    assert ENV_NAMESPACE in result.stderr


def test_missing_release_name_exits_with_error(clean_env, monkeypatch):
    monkeypatch.setenv(ENV_NAMESPACE, "monitoring")
    result = runner.invoke(app, ["argocd-yaml", "-n", "monitoring"])
    assert result.exit_code == 2
    assert ENV_RELEASE_NAME in result.stderr


def test_run_passes_no_kubeconfig_to_client(clean_env, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/from-env/kubeconfig")

    options = CliOptions(namespace="ns", release_name="app")
    with (
        patch("helmadm.cli.load_kubernetes_client") as load_client,
        patch("helmadm.cli._load_release_and_values") as load_values,
        patch("helmadm.cli.render_application", return_value=""),
    ):
        release = {
            "name": "app",
            "config": {},
            "chart": {
                "metadata": {
                    "name": "app",
                    "version": "1.0.0",
                    "repoURL": "https://charts.example.com",
                },
                "values": {},
            },
        }
        load_values.return_value = make_load_release_and_values_result(release)
        assert run(options) == 0

    load_client.assert_called_once_with()


def test_ls_command_basic(clean_env, monkeypatch):
    releases = [
        HelmReleaseSummary(
            namespace="monitoring",
            name="prometheus",
            revision=2,
            status="deployed",
            chart_name="prometheus",
            chart_version="1.0.0",
            repo_url="https://charts.example.com",
            needs_repo_url=False,
        ),
    ]
    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch("helmadm.cli.list_releases", return_value=releases) as list_mock,
    ):
        result = runner.invoke(app, ["ls", "-n", "monitoring"])

    assert result.exit_code == 0
    assert "prometheus" in result.stdout
    assert "NEEDS_REPO_URL" in result.stdout
    assert list_mock.call_args.kwargs["detail"] is True
    assert list_mock.call_args.kwargs["all_namespaces"] is False
    assert list_mock.call_args.kwargs["namespace"] == "monitoring"


def test_ls_no_detail_flag(clean_env, monkeypatch):
    releases = [
        HelmReleaseSummary(
            namespace="monitoring",
            name="prometheus",
            revision=2,
            status="deployed",
        ),
    ]
    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch("helmadm.cli.list_releases", return_value=releases) as list_mock,
    ):
        result = runner.invoke(app, ["ls", "-n", "monitoring", "--no-detail"])

    assert result.exit_code == 0
    assert "NEEDS_REPO_URL" not in result.stdout
    assert list_mock.call_args.kwargs["detail"] is False


def test_ls_defaults_to_all_namespaces(clean_env, monkeypatch):
    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch("helmadm.cli.list_releases", return_value=[]) as list_mock,
    ):
        result = runner.invoke(app, ["ls"])

    assert result.exit_code == 0
    list_mock.assert_called_once()
    assert list_mock.call_args.kwargs["all_namespaces"] is True
    assert list_mock.call_args.kwargs["namespace"] is None
    assert list_mock.call_args.kwargs["detail"] is True


def test_ls_detail_by_default(clean_env, monkeypatch):
    releases = [
        HelmReleaseSummary(
            namespace="monitoring",
            name="legacy",
            revision=1,
            chart_name="legacy-chart",
            chart_version="1.0.0",
            repo_url=None,
            needs_repo_url=True,
        ),
    ]
    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch("helmadm.cli.list_releases", return_value=releases) as list_mock,
    ):
        result = runner.invoke(app, ["ls", "-n", "monitoring"])

    assert result.exit_code == 0
    assert "NEEDS_REPO_URL" in result.stdout
    assert "yes" in result.stdout
    list_mock.assert_called_once()
    assert list_mock.call_args.kwargs["detail"] is True


def test_ls_all_namespaces(clean_env):
    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch("helmadm.cli.list_releases", return_value=[]) as list_mock,
    ):
        result = runner.invoke(app, ["ls", "-A"])

    assert result.exit_code == 0
    list_mock.assert_called_once()
    assert list_mock.call_args.kwargs["all_namespaces"] is True


@pytest.mark.no_stub_k8s_access
def test_argocd_yaml_errors_when_kubernetes_unreachable(clean_env, monkeypatch):
    monkeypatch.setenv(ENV_NAMESPACE, "ns")
    monkeypatch.setenv(ENV_RELEASE_NAME, "app")

    def fail_access(_api):
        raise KubernetesApiError("simulated cluster unreachable")

    monkeypatch.setattr("helmadm.cli.check_kubernetes_accessible", fail_access)

    with patch("helmadm.cli.load_kubernetes_client"):
        result = runner.invoke(app, ["argocd-yaml", "app"])

    assert result.exit_code == 1
    assert "Error:" in result.stderr
    assert "simulated cluster unreachable" in result.stderr
