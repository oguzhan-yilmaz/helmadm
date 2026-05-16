from __future__ import annotations

import io
import re
import shutil
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from helmadm.argocd_manifest import normalize_repo_url
from helmadm.yaml_render import dump_yaml
from helmadm.logging_config import get_logger

logger = get_logger("pull_bundle")

METADATA_FILENAME = "helmadm-pull-metadata.yaml"
_README_FILENAME = "README.md"


class PullBundleExistsError(Exception):
    pass


@dataclass(frozen=True)
class PullKubernetesContext:
    kubeconfig: str | None
    context: str | None
    pulled_at: str


@dataclass(frozen=True)
class PullBundle:
    namespace: str
    release_name: str
    helm_revision: int
    chart_name: str
    chart_version: str
    repo_url: str
    repo_alias: str
    chart_ref: str
    status: str | None
    all_values_filename: str
    changed_values_filename: str
    remote_all_values_filename: str
    metadata_filename: str = METADATA_FILENAME
    readme_filename: str = _README_FILENAME
    oci_chart: bool = False

    def bundle_path(self, parent_dir: Path) -> Path:
        return bundle_path(parent_dir, self.namespace, self.release_name)


def bundle_path(parent_dir: Path, namespace: str, release_name: str) -> Path:
    return parent_dir / _sanitize_path_component(namespace) / _sanitize_path_component(
        release_name
    )


def _sanitize_path_component(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned or "unknown"


def derive_repo_alias(repo_url: str) -> str:
    parsed = urlparse(normalize_repo_url(repo_url))
    host = parsed.hostname or "repo"
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] == "github" and labels[-1] == "io":
        return _sanitize_path_component(labels[0])
    if len(labels) >= 3 and labels[-2] == "gitlab" and labels[-1] == "io":
        return _sanitize_path_component(labels[0])
    return _sanitize_path_component(labels[0])


def render_values_yaml(values: dict[str, Any]) -> str:
    return dump_yaml(values)


def build_pull_bundle(
    *,
    namespace: str,
    release_name: str,
    helm_revision: int,
    chart_name: str,
    chart_version: str,
    repo_url: str,
    repo_alias: str | None = None,
    status: str | None = None,
    oci_chart: bool = False,
) -> PullBundle:
    alias = repo_alias or derive_repo_alias(repo_url)
    return PullBundle(
        namespace=namespace,
        release_name=release_name,
        helm_revision=helm_revision,
        chart_name=chart_name,
        chart_version=chart_version,
        repo_url=repo_url,
        repo_alias=alias,
        chart_ref=f"{alias}/{chart_name}",
        status=status,
        all_values_filename=f"{chart_name}.all.values.yaml",
        changed_values_filename=f"{chart_name}.changed.values.yaml",
        remote_all_values_filename=f"{chart_name}.remote-all.values.yaml",
        oci_chart=oci_chart,
    )


def build_pull_metadata(
    bundle: PullBundle,
    *,
    kubernetes: PullKubernetesContext,
    values_strategy: str | None = None,
) -> dict[str, Any]:
    return {
        "pulledAt": kubernetes.pulled_at,
        "kubernetes": {
            "kubeconfig": kubernetes.kubeconfig,
            "context": kubernetes.context,
        },
        "release": {
            "name": bundle.release_name,
            "namespace": bundle.namespace,
            "helmRevision": bundle.helm_revision,
            "status": bundle.status,
        },
        "chart": {
            "name": bundle.chart_name,
            "version": bundle.chart_version,
            "repoURL": bundle.repo_url,
            "repoAlias": bundle.repo_alias,
            "chartRef": bundle.chart_ref,
            "oci": bundle.oci_chart,
        },
        "values": {
            "strategy": values_strategy,
            "files": {
                "all": bundle.all_values_filename,
                "changed": bundle.changed_values_filename,
                "remoteAll": bundle.remote_all_values_filename,
            },
        },
        "bundle": {
            "path": f"{bundle.namespace}/{bundle.release_name}",
            "files": [
                bundle.metadata_filename,
                bundle.readme_filename,
                bundle.all_values_filename,
                bundle.changed_values_filename,
                bundle.remote_all_values_filename,
            ],
        },
    }


def render_pull_readme(bundle: PullBundle) -> str:
    alias = bundle.repo_alias
    chart_ref = bundle.chart_ref
    version = bundle.chart_version
    release = bundle.release_name
    namespace = bundle.namespace
    all_values = bundle.all_values_filename
    changed_values = bundle.changed_values_filename
    remote_all = bundle.remote_all_values_filename

    oci_note = ""
    if bundle.oci_chart:
        oci_note = (
            "\n> OCI chart: adjust `helm repo` / `helm pull` for `oci://` if needed.\n"
        )

    return f"""# Reproduce Helm release `{release}`

Namespace `{namespace}`, chart `{bundle.chart_name}` `{version}` (Helm revision {bundle.helm_revision}).

| File | Use |
|------|-----|
| `{changed_values}` | Overrides only (usual reinstall) |
| `{all_values}` | Full effective cluster values |
| `{remote_all}` | Upstream chart defaults |
| `{METADATA_FILENAME}` | Pull provenance and release info |
{oci_note}
## Chart repo

```bash
helm repo add {alias} {bundle.repo_url}
helm repo update {alias}
helm search repo {chart_ref} --versions
helm pull --untar --version {version} {chart_ref}
```

## Install / upgrade

**Overrides from cluster** (`{changed_values}`):

```bash
helm upgrade -i {release} {chart_ref} \\
  -n {namespace} --create-namespace \\
  -f {changed_values} \\
  --version {version}
```

**Full cluster values** (`{all_values}`):

```bash
helm upgrade -i {release} {chart_ref} \\
  -n {namespace} --create-namespace \\
  -f {all_values} \\
  --version {version}
```

**Chart defaults only** (`{remote_all}`):

```bash
helm upgrade -i {release} {chart_ref} \\
  -n {namespace} --create-namespace \\
  -f {remote_all} \\
  --version {version}
```

## Template (dry-run)

```bash
helm template {release} {chart_ref} -n {namespace} -f {changed_values} --version {version}
helm template {release} {chart_ref} -n {namespace} -f {all_values} --version {version}
helm template {release} {chart_ref} -n {namespace} -f {remote_all} --version {version}
```

## Uninstall

```bash
helm uninstall -n {namespace} {release}
```
"""


@dataclass(frozen=True)
class PullBundleFiles:
    all_values_yaml: str
    changed_values_yaml: str
    remote_all_values_yaml: str
    metadata_yaml: str
    readme: str


def build_pull_bundle_files(
    bundle: PullBundle,
    *,
    cluster_values: dict[str, Any],
    values_object: dict[str, Any],
    remote_defaults: dict[str, Any],
    kubernetes: PullKubernetesContext,
    values_strategy: str | None = None,
) -> PullBundleFiles:
    return PullBundleFiles(
        all_values_yaml=render_values_yaml(cluster_values),
        changed_values_yaml=render_values_yaml(values_object),
        remote_all_values_yaml=render_values_yaml(remote_defaults),
        metadata_yaml=render_values_yaml(
            build_pull_metadata(
                bundle, kubernetes=kubernetes, values_strategy=values_strategy
            )
        ),
        readme=render_pull_readme(bundle),
    )


def write_pull_bundle(
    parent_dir: Path,
    bundle: PullBundle,
    files: PullBundleFiles,
    *,
    force: bool = False,
) -> Path:
    bundle_dir = bundle.bundle_path(parent_dir)
    if bundle_dir.exists():
        if not force:
            raise PullBundleExistsError(
                f"bundle directory already exists: {bundle_dir} (use --force to overwrite)"
            )
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=False)
    logger.debug("writing pull bundle to %s", bundle_dir)

    writes: tuple[str, str] = (
        (bundle.metadata_filename, files.metadata_yaml),
        (bundle.readme_filename, files.readme),
        (bundle.all_values_filename, files.all_values_yaml),
        (bundle.changed_values_filename, files.changed_values_yaml),
        (bundle.remote_all_values_filename, files.remote_all_values_yaml),
    )
    for filename, content in writes:
        path = bundle_dir / filename
        path.write_text(content, encoding="utf-8")
        logger.debug("wrote %s (%d bytes)", path, len(content))

    return bundle_dir


def write_pull_bundle_tar(parent_dir: Path, bundle_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(parent_dir).as_posix()
            archive.add(path, arcname=rel)
    logger.debug(
        "built tarball for %s (%d bytes)", bundle_dir, buffer.tell()
    )
    return buffer.getvalue()


def kubernetes_context_for_pull(
    *,
    kubeconfig: Path | None,
    context: str | None,
) -> PullKubernetesContext:
    import os

    if kubeconfig is not None:
        kubeconfig_display: str | None = str(kubeconfig)
    elif os.environ.get("KUBECONFIG"):
        kubeconfig_display = os.environ["KUBECONFIG"]
    else:
        kubeconfig_display = str(Path.home() / ".kube" / "config")

    return PullKubernetesContext(
        kubeconfig=kubeconfig_display,
        context=context,
        pulled_at=datetime.now(UTC).isoformat(),
    )
