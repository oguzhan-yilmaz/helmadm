# helmadm

CLI tools for Helm 3 releases stored in Kubernetes: generate Argo CD `Application` YAML, export reproducible Helm install bundles, list releases, and compare a release manifest to live cluster objects. No `helm` or `kubectl` binary required for helmadm itself.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Access to a Kubernetes cluster (kubeconfig or in-cluster)

## Install

```bash
uv sync
```

## Commands

| Command | Purpose |
|---------|---------|
| `argocd-yaml` | Print an Argo CD `Application` manifest for a release (stdout) |
| `pull` | Export a reproducible Helm install bundle (values files + README) |
| `ls` | List Helm releases in the cluster |
| `drift` | Compare the release's stored manifest to live objects (read-only) |

```bash
uv run helmadm --help
uv run helmadm argocd-yaml --help
uv run helmadm pull --help
uv run helmadm ls --help
uv run helmadm drift --help
```

### `argocd-yaml` — Application manifest

Reads the release from cluster storage, diffs coalesced chart values + user config against `helm show values` for the chart version (fetched from the chart repo), and writes overrides to `spec.source.helm.valuesObject`. Non-helm fields use `CHANGE_ME` placeholders.

```bash
uv run helmadm ls -n monitoring
uv run helmadm argocd-yaml -n monitoring prometheus
```

If the release has no `chart.metadata.repoURL`, pass the repository URL (see `NEEDS_REPO_URL` in `ls`):

```bash
uv run helmadm argocd-yaml -n monitoring prometheus \
  --repo-url https://prometheus-community.github.io/helm-charts
```

`--debug` adds a `.debug` block to the YAML (raw values, diff metadata, `ignoreAnnotations`). Remove it before applying to Argo CD.

### `pull` — reproducible install bundle

Reads a release from cluster storage and writes a directory you can use with plain `helm install` / `helm upgrade` (no helmadm or Argo required for reinstall).

```bash
uv run helmadm pull -n loki -o ./bundles fluentbit
```

Creates `./bundles/{namespace}/{release}/` containing:

- `helmadm-pull-metadata.yaml` — when pulled, kubeconfig/context, release and chart info
- `{chart}.all.values.yaml` — effective values from the cluster (coalesced)
- `{chart}.changed.values.yaml` — overrides only (diff vs remote chart defaults)
- `{chart}.remote-all.values.yaml` — chart defaults from the repository
- `README.md` — helm install/template commands for each values file

Options:

- `--revision N` — pull a specific Helm release revision (default: latest)
- `--repo-url` — when the release has no `chart.metadata.repoURL` (see `NEEDS_REPO_URL` in `ls`)
- `--repo-name` — helm repo alias used in the README (default: derived from the repo URL host)
- `--tar` — write a gzip tarball of the bundle to stdout instead of leaving files on disk
- `--force` — overwrite an existing bundle directory

```bash
uv run helmadm pull -n monitoring prometheus --revision 3 -o ./bundles
uv run helmadm pull -n loki -o ./bundles fluentbit --tar > fluentbit-bundle.tar.gz
```

### `ls` — list releases

```bash
uv run helmadm ls                  # all namespaces, detailed (default)
uv run helmadm ls -n monitoring    # one namespace
uv run helmadm ls --no-detail      # name / revision / status only
```

### `drift` — manifest vs live

```bash
uv run helmadm drift -n monitoring prometheus
uv run helmadm drift --detect-extras -n monitoring prometheus
uv run helmadm drift -ia -n kube-system traefik   # print normalization notes before each diff
```

Exit `1` on drift, missing objects, fetch errors, or extras (with `--detect-extras`).

### Environment variables

CLI flags take precedence over env vars.

| Flag | Environment variable |
|------|----------------------|
| `-n` / `--namespace` | `HELMADM_NAMESPACE` (or current kubeconfig context namespace) |
| `--context` | `HELMADM_CONTEXT` |
| `--repo-url` | `HELMADM_REPO_URL` |
| release name (positional) | `HELMADM_RELEASE_NAME` |
| (values trace, with `-v`) | `HELMADM_TRACE_VALUES` — per-key logs during `argocd-yaml` |
| (Kubernetes HTTP) | `HELMADM_K8S_CONNECT_TIMEOUT` — connect timeout in seconds (default: `5`) |
| (Kubernetes HTTP) | `HELMADM_K8S_READ_TIMEOUT` — read timeout in seconds (default: `60`) |

`--kubeconfig` follows kubectl: use the flag, or `KUBECONFIG` / `~/.kube/config`.

## Development

```bash
uv run pytest
```
