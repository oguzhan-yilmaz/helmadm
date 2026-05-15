from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse

import yaml

from helmadm.argocd_manifest import normalize_repo_url
from helmadm.logging_config import get_logger

logger = get_logger("chart_values")

_DEFAULT_TIMEOUT_S = 60
_USER_AGENT = "helmadm/0.1"


class ChartValuesFetchError(Exception):
    pass


def _repo_base_url(repo_url: str) -> str:
    return normalize_repo_url(repo_url).rstrip("/")


def _fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    logger.debug("fetching %s", url)
    try:
        with urllib.request.urlopen(request, timeout=_DEFAULT_TIMEOUT_S) as response:
            return response.read()
    except urllib.error.URLError as exc:
        raise ChartValuesFetchError(f"failed to fetch {url!r}: {exc}") from exc


def _load_repo_index(repo_url: str) -> dict[str, Any]:
    base = _repo_base_url(repo_url)
    index_url = f"{base}/index.yaml"
    raw = _fetch_bytes(index_url)
    index = yaml.safe_load(raw)
    if not isinstance(index, dict):
        raise ChartValuesFetchError(f"invalid index.yaml from {index_url!r}")
    logger.debug(
        "loaded chart index from %s (%d chart name(s))",
        index_url,
        len(index.get("entries", {})),
    )
    return index


def _chart_archive_url(index: dict[str, Any], chart_name: str, version: str) -> str:
    entries = index.get("entries")
    if not isinstance(entries, dict):
        raise ChartValuesFetchError("chart index has no entries")

    versions = entries.get(chart_name)
    if not versions:
        raise ChartValuesFetchError(
            f"chart {chart_name!r} not found in repository index"
        )

    for entry in versions:
        if not isinstance(entry, dict):
            continue
        if entry.get("version") != version:
            continue
        urls = entry.get("urls")
        if isinstance(urls, list) and urls:
            return str(urls[0])

    raise ChartValuesFetchError(
        f"chart {chart_name!r} version {version!r} not found in repository index"
    )


def _resolve_chart_url(repo_url: str, archive_url: str) -> str:
    parsed = urlparse(archive_url)
    if parsed.scheme in ("http", "https"):
        return archive_url
    base = _repo_base_url(repo_url)
    return urljoin(f"{base}/", archive_url.lstrip("/"))


def _values_from_chart_archive(data: bytes, chart_name: str) -> dict[str, Any]:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            preferred = f"{chart_name}/values.yaml"
            chosen: tarfile.TarInfo | None = None
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                name = member.name.lstrip("./")
                if name == preferred:
                    chosen = member
                    break
                if name.endswith("/values.yaml") and chosen is None:
                    chosen = member
            if chosen is None:
                raise ChartValuesFetchError("values.yaml not found in chart archive")

            extracted = archive.extractfile(chosen)
            if extracted is None:
                raise ChartValuesFetchError("failed to read values.yaml from chart archive")
            parsed = yaml.safe_load(extracted.read())
    except tarfile.TarError as exc:
        raise ChartValuesFetchError("failed to read chart archive") from exc

    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ChartValuesFetchError("values.yaml is not a mapping")
    logger.debug(
        "parsed values.yaml from chart archive (%d top-level key(s))", len(parsed)
    )
    return parsed


def _fetch_via_http(repo_url: str, chart_name: str, chart_version: str) -> dict[str, Any]:
    index = _load_repo_index(repo_url)
    archive_url = _chart_archive_url(index, chart_name, chart_version)
    resolved_url = _resolve_chart_url(repo_url, archive_url)
    archive_bytes = _fetch_bytes(resolved_url)
    logger.debug("downloaded chart archive (%d bytes)", len(archive_bytes))
    return _values_from_chart_archive(archive_bytes, chart_name)


def _fetch_via_helm_cli(
    repo_url: str, chart_name: str, chart_version: str
) -> dict[str, Any]:
    helm = shutil.which("helm")
    if not helm:
        raise ChartValuesFetchError("helm binary not found on PATH")

    logger.debug(
        "running helm show values for chart=%r version=%r repo_url=%r",
        chart_name,
        chart_version,
        repo_url,
    )
    result = subprocess.run(
        [
            helm,
            "show",
            "values",
            chart_name,
            "--repo-url",
            repo_url,
            "--version",
            chart_version,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise ChartValuesFetchError(
            message or "helm show values failed"
        )

    parsed = yaml.safe_load(result.stdout)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ChartValuesFetchError("helm show values did not return a mapping")
    logger.debug(
        "helm show values returned %d top-level key(s)", len(parsed)
    )
    return parsed


def fetch_remote_chart_values(
    repo_url: str,
    chart_name: str,
    chart_version: str,
) -> dict[str, Any]:
    """Return default values.yaml for a chart version from a Helm repository."""
    logger.debug(
        "fetching remote chart values: chart=%r version=%r repo_url=%r",
        chart_name,
        chart_version,
        repo_url,
    )
    try:
        return _fetch_via_http(repo_url, chart_name, chart_version)
    except ChartValuesFetchError as http_exc:
        logger.debug("http chart values fetch failed: %s", http_exc)
        if shutil.which("helm") is None:
            raise
        logger.debug("falling back to helm show values")
        try:
            return _fetch_via_helm_cli(repo_url, chart_name, chart_version)
        except ChartValuesFetchError as helm_exc:
            raise ChartValuesFetchError(
                f"{http_exc}; helm fallback: {helm_exc}"
            ) from helm_exc
