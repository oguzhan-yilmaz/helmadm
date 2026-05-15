from helmadm.helm_release import HelmReleaseSummary
from helmadm.ls_output import format_release_list


def test_format_release_list_empty():
    assert format_release_list([]) == "No Helm releases found."


def test_format_release_list_basic():
    releases = [
        HelmReleaseSummary(
            namespace="monitoring",
            name="prometheus",
            revision=3,
            status="deployed",
        ),
    ]
    output = format_release_list(releases, detail=False)
    assert "NAMESPACE" in output
    assert "monitoring" in output
    assert "prometheus" in output
    assert "3" in output
    assert "deployed" in output


def test_format_release_list_detail_shows_needs_repo_url():
    releases = [
        HelmReleaseSummary(
            namespace="monitoring",
            name="prometheus",
            revision=1,
            chart_name="kube-prometheus-stack",
            chart_version="45.0.0",
            repo_url="https://prometheus-community.github.io/helm-charts",
            needs_repo_url=False,
        ),
        HelmReleaseSummary(
            namespace="monitoring",
            name="legacy",
            revision=2,
            chart_name="legacy-chart",
            chart_version="1.0.0",
            repo_url=None,
            needs_repo_url=True,
        ),
    ]
    output = format_release_list(releases, detail=True)
    assert "NEEDS_REPO_URL" in output
    assert "REVISION" in output
    assert "1" in output
    assert "2" in output
    assert "no" in output
    assert "yes" in output
    assert "prometheus-community" in output
