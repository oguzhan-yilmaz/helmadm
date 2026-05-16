# helmadm

CLI tools for Helm 3 releases stored in Kubernetes: generate Argo CD `Application` YAML, list releases, and compare a release manifest to live cluster objects. No `helm` or `kubectl` binary required.

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
| `ls` | List Helm releases in the cluster |
| `drift` | Compare the release's stored manifest to live objects (read-only) |

```bash
uv run helmadm --help
uv run helmadm argocd-yaml --help
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
| `-n` / `--namespace` | `HELM_TO_ARGOCD_NAMESPACE` (or current kubeconfig context namespace) |
| `--context` | `HELM_TO_ARGOCD_CONTEXT` |
| `--repo-url` | `HELM_TO_ARGOCD_REPO_URL` |
| release name (positional) | `HELM_TO_ARGOCD_RELEASE_NAME` |
| (values trace, with `-v`) | `HELM_TO_ARGOCD_TRACE_VALUES` — per-key logs during `argocd-yaml` |
| (Kubernetes HTTP) | `HELM_TO_ARGOCD_K8S_CONNECT_TIMEOUT` — connect timeout in seconds (default: `5`) |
| (Kubernetes HTTP) | `HELM_TO_ARGOCD_K8S_READ_TIMEOUT` — read timeout in seconds (default: `60`) |

`--kubeconfig` follows kubectl: use the flag, or `KUBECONFIG` / `~/.kube/config`.

## Development

```bash
uv run pytest
```
