from typing import Any

import yaml

from helmadm.logging_config import get_logger

logger = get_logger("argocd_manifest")

PLACEHOLDER = "CHANGE_ME"


class RepoURLMissingError(Exception):
    pass


def normalize_repo_url(url: str) -> str:
    normalized = url.removeprefix("oci://")
    if normalized != url:
        logger.debug("stripped oci:// prefix from repo URL")
    return normalized


def chart_repo_url(release: dict[str, Any]) -> str | None:
    repo_url = release.get("chart", {}).get("metadata", {}).get("repoURL")
    if not repo_url:
        return None
    return normalize_repo_url(repo_url)


def needs_repo_url(release: dict[str, Any]) -> bool:
    return chart_repo_url(release) is None


def resolve_repo_url(release: dict[str, Any], repo_url_override: str | None) -> str:
    if repo_url_override:
        resolved = normalize_repo_url(repo_url_override)
        logger.debug("using repo URL override: %r", resolved)
        return resolved

    metadata = release.get("chart", {}).get("metadata", {})
    repo_url = metadata.get("repoURL")
    if repo_url:
        resolved = normalize_repo_url(repo_url)
        logger.debug("using repo URL from chart metadata: %r", resolved)
        return resolved

    logger.debug("chart metadata has no repoURL and no override provided")
    raise RepoURLMissingError(
        "chart.metadata.repoURL is missing from the helm release; "
        "pass --repo-url with the chart repository URL"
    )


DEBUG_FIELD = ".debug"


def build_application(
    release: dict[str, Any],
    values_object: dict[str, Any],
    repo_url: str,
    *,
    debug: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chart = release.get("chart", {})
    metadata = chart.get("metadata", {})

    chart_name = metadata.get("name")
    chart_version = metadata.get("version")
    logger.debug(
        "building application: chart=%r version=%r repo_url=%r values_keys=%d",
        chart_name,
        chart_version,
        repo_url,
        len(values_object),
    )
    if not chart_name or not chart_version:
        raise ValueError("release chart metadata is missing name or version")

    manifest = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": PLACEHOLDER,
            "namespace": PLACEHOLDER,
        },
        "spec": {
            "project": PLACEHOLDER,
            "destination": {
                "server": PLACEHOLDER,
                "namespace": PLACEHOLDER,
            },
            "syncPolicy": {
                "syncOptions": [
                    "CreateNamespace=false",
                ],
            },
            "source": {
                "repoURL": repo_url,
                "chart": chart_name,
                "targetRevision": chart_version,
                "helm": {
                    "releaseName": release.get("name", ""),
                    "valuesObject": values_object,
                },
            },
        },
    }
    if debug is not None:
        manifest[DEBUG_FIELD] = debug
        logger.debug("attached %s block to manifest", DEBUG_FIELD)
    logger.debug("built Argo CD Application manifest for chart %r", chart_name)
    return manifest


class _NoAliasDumper(yaml.SafeDumper):
    """Avoid YAML anchors (e.g. valuesObject: &id001 {}) when dicts are reused."""

    def ignore_aliases(self, data: object) -> bool:
        return True


def render_application(manifest: dict[str, Any]) -> str:
    rendered = yaml.dump(
        manifest,
        Dumper=_NoAliasDumper,
        default_flow_style=False,
        sort_keys=False,
    )
    logger.debug("rendered manifest to YAML (%d bytes)", len(rendered))
    return rendered
