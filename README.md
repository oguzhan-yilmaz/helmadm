# helmadm

Generate an Argo CD `Application` manifest from a Helm release stored in your cluster.

Reads Helm release state from Kubernetes Secrets (no `helm` or `kubectl` CLI), diffs user values against chart defaults embedded in the release, and prints a manifest with overrides in `spec.source.helm.valuesObject`.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Access to a Kubernetes cluster (kubeconfig or in-cluster)

## Install

```bash
uv sync
```

## Usage

```bash
uv run helmadm --help
uv run helmadm convert --help
uv run helmadm ls --help
```

### Convert a release

```bash
uv run helmadm convert -n monitoring prometheus
```

If the release does not store `chart.metadata.repoURL` (older installs or local charts), pass the repository URL:

```bash
uv run helmadm convert -n monitoring prometheus \
  --repo-url https://prometheus-community.github.io/helm-charts
```

Tool-specific options can be set via environment variables (CLI flags take precedence):

| Flag | Environment variable |
|------|----------------------|
| `-n` / `--namespace` | `HELM_TO_ARGOCD_NAMESPACE` (or current kubeconfig context namespace) |
| `--context` | `HELM_TO_ARGOCD_CONTEXT` |
| `--repo-url` | `HELM_TO_ARGOCD_REPO_URL` |
| release name (positional) | `HELM_TO_ARGOCD_RELEASE_NAME` |
| (Kubernetes HTTP) | `HELM_TO_ARGOCD_K8S_CONNECT_TIMEOUT` — connect timeout in seconds (default: `5`; fails fast when the API is unreachable, e.g. VPN off) |
| (Kubernetes HTTP) | `HELM_TO_ARGOCD_K8S_READ_TIMEOUT` — read timeout in seconds (default: `120`) |

`--kubeconfig` follows kubectl: set the path with the flag, or omit it and let the client use `KUBECONFIG` (colon-separated paths on Unix) or `~/.kube/config`.

Without `-n`, the namespace is resolved in order: flag → `HELM_TO_ARGOCD_NAMESPACE` → default namespace on your current kubeconfig context.

### List releases

```bash
uv run helmadm ls                  # all namespaces, detailed output (default)
uv run helmadm ls -n monitoring    # one namespace
uv run helmadm ls --no-detail      # revision/status only (faster)
```

By default, the table includes chart name/version, stored `repoURL`, and a `NEEDS_REPO_URL` column (`yes` means you must pass `--repo-url` when running `convert`).

Output from `convert` is a single `Application` YAML on stdout. Non-helm fields (`metadata`, `project`, `destination`) use `CHANGE_ME` placeholders for you to fill in.

## Development

```bash
uv run pytest
```
