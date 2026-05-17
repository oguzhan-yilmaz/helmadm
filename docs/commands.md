# Commands

All subcommands support `-h` / `--help` and global `-v` / `--verbose` (debug logging on stderr).

## `ls`

List Helm 3 releases decoded from Kubernetes secret storage.

| Option | Description |
|--------|-------------|
| `-n` / `--namespace` | Limit to one namespace (default: all namespaces) |
| `-A` / `--all-namespaces` | Same as omitting `-n` |
| `--detail` / `--no-detail` | Detailed columns (default: `--detail`) |
| `--kubeconfig` | Kubeconfig file (default: `KUBECONFIG` or `~/.kube/config`) |
| `--context` | Kubeconfig context |

**Detailed output** includes chart name, version, stored `repoURL`, and `NEEDS_REPO_URL=yes` when the release lacks `chart.metadata.repoURL` — pass `--repo-url` to `argocd-yaml` and `pull`.

```bash
helmadm ls
helmadm ls -n monitoring
helmadm ls --no-detail
```

## `drift`

Compare each object in the Helm release `manifest` (from the latest revision secret) to a live API `GET`. Read-only: no `helm upgrade` or `kubectl apply`.

**Default compare (`--compare-mode ssa`):** for each manifest object, the API server runs a server-side apply dry-run (field manager `helm` by default) and returns the merged object. That result is compared to live after stripping only `status` and `metadata.managedFields`. Drifting objects get a unified diff (`merged/...` vs `live/...`). This matches `kubectl diff --server-side` semantics without requiring kubectl.

When SSA is unavailable for a resource (some CRDs), helmadm automatically falls back to legacy client-side normalization (`manifest/...` vs `live/...`). Use `-v` to see fallback reasons. Force legacy for all objects with `--compare-mode legacy`.

| Option | Description |
|--------|-------------|
| `-n` / `--namespace` | Release namespace (required unless set via env / kubeconfig default) |
| `--compare-mode` | `ssa` (default) or `legacy` |
| `--field-manager` | Field manager for SSA dry-run (default: `helm`) |
| `--detect-extras` | List namespaced objects in `-n` not in the manifest (needs broad list RBAC) |
| `--ignore-annotations` / `-ia` | Print compare notes before each diff |
| `-v` / `--verbose` | Debug logging; SSA fallback reasons in report |
| `--kubeconfig`, `--context` | Kubernetes client |

**Exit codes:** `0` when every manifest object matches; `1` on drift, missing object, fetch error, or extras (with `--detect-extras`).

Helm hook manifests are not in `manifest` and are not checked. `kubectl scale` / replica edits on workloads in the manifest should show as drift unless something reconciles them first.

```bash
helmadm drift -n monitoring prometheus
helmadm drift -n monitoring prometheus | delta -s
helmadm drift --detect-extras -n monitoring prometheus
helmadm drift -ia -n kube-system traefik
helmadm drift --compare-mode legacy -n monitoring prometheus
helmadm drift -n monitoring blackbox-exporter -ia -v | delta -s --paging never
```

## `argocd-yaml`

Read a Helm 3 release from cluster storage and print one Argo CD `Application` manifest to stdout.

**Values:** coalesced `chart.values` + `release.config` from the release are compared to `helm show values` for the chart version (fetched from `--repo-url` or `chart.metadata.repoURL`). Only differences become `spec.source.helm.valuesObject`.

Argo CD fields you must set yourself (`metadata.name`, `project`, `destination`, …) use `CHANGE_ME` placeholders.

| Option | Description |
|--------|-------------|
| `RELEASE` | Helm release name (positional) |
| `-n` / `--namespace` | Release namespace |
| `--repo-url` | Chart repo when release has no `chart.metadata.repoURL` |
| `--debug` | Add `.debug` block (raw values, diff metadata, `ignoreAnnotations`) — strip before apply |
| `--kubeconfig`, `--context` | Kubernetes client |

With `-v` / `--verbose`, set `HELMADM_TRACE_VALUES` for per-key values/diff trace lines during this command.

```bash
helmadm ls -n monitoring
helmadm argocd-yaml -n monitoring prometheus
helmadm argocd-yaml -n monitoring prometheus > application.yaml
helmadm argocd-yaml -n monitoring prometheus \
  --repo-url https://prometheus-community.github.io/helm-charts
helmadm --verbose argocd-yaml --debug -n keda keda \
  --repo-url https://kedacore.github.io/charts
helmadm argocd-yaml --repo-url https://traefik.github.io/charts -n kube-system traefik -v | yq .
```

## `pull`

Export a local bundle from a Helm 3 release so it can be reinstalled with plain `helm install` / `helm upgrade` (no helmadm or Argo required).

Writes `{namespace}/{release}/` under `-o` / `--output`:

| File | Description |
|------|-------------|
| `helmadm-pull-metadata.yaml` | Pull time, kubeconfig/context, release and chart info |
| `{chart}.all.values.yaml` | Effective values from the cluster (coalesced) |
| `{chart}.changed.values.yaml` | Overrides only (diff vs remote chart defaults) |
| `{chart}.remote-all.values.yaml` | Chart defaults from the repository |
| `README.md` | `helm install` / `helm template` commands for each values file |

| Option | Description |
|--------|-------------|
| `RELEASE` | Helm release name (positional) |
| `-o` / `--output` | Parent directory (required unless `--tar`) |
| `-n` / `--namespace` | Release namespace |
| `--revision N` | Specific Helm release revision (default: latest) |
| `--repo-url` | When release has no `chart.metadata.repoURL` |
| `--repo-name` | Helm repo alias in README (default: derived from repo URL host) |
| `--tar` | Gzip tarball to stdout (no files on disk) |
| `--force` | Overwrite existing bundle directory |
| `--kubeconfig`, `--context` | Kubernetes client |

```bash
helmadm pull -n loki -o ./bundles fluentbit
helmadm pull -n monitoring prometheus --revision 3 -o ./bundles
helmadm pull -n loki -o ./bundles fluentbit --tar > fluentbit-bundle.tar.gz
helmadm pull -o ./bundles --repo-url https://prometheus-community.github.io/helm-charts \
  -n monitoring blackbox-exporter
```
