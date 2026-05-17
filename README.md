# helmadm

Inspect Helm 3 releases in Kubernetes: list releases, detect manifest drift, generate Argo CD `Application` YAML, and export reproducible install bundles. Uses the Kubernetes API only — no `helm` or `kubectl` binary required.

**Requirements:** Python 3.12+, kubeconfig (or in-cluster) access to the cluster.

## Install

**Recommended — [uv](https://docs.astral.sh/uv/) + `uvx` (no global install):**

```bash
uvx helmadm ls
```

Add a shell alias so `helmadm` always runs the latest from PyPI:

```bash
alias helmadm='uvx helmadm'
```

**Global tool with uv:**

```bash
uv tool install helmadm
helmadm --help
```

**pip / pipx:**

```bash
pip install helmadm
# or: pipx install helmadm
```

**From source (development):**

```bash
# in a clone of this repository
uv sync
uv run helmadm --help
```

See [docs/develop/dev-uv.md](docs/develop/dev-uv.md) for building and publishing.

## Commands

| Command | Purpose |
|---------|---------|
| `ls` | List Helm releases (Helm 3 secret storage) |
| `drift` | Compare release manifest to live objects (read-only) |
| `argocd-yaml` | Print an Argo CD `Application` manifest |
| `pull` | Export a reproducible Helm install bundle |

```bash
helmadm --help
helmadm ls --help
```

### `ls`

```bash
helmadm ls
helmadm ls -n monitoring
helmadm ls --no-detail
```

Detailed output includes chart, version, and `NEEDS_REPO_URL` when `argocd-yaml` / `pull` need `--repo-url`.

### `drift`

```bash
helmadm drift -n monitoring prometheus
helmadm drift -n monitoring prometheus --detect-extras   # objects not managed by the release
helmadm drift -ia -n kube-system traefik   # show compare notes
helmadm drift --compare-mode legacy -n monitoring prometheus
```

Default compare uses server-side apply dry-run (merged vs live; like `kubectl diff --server-side`). Legacy normalization is used automatically when SSA is unavailable, or with `--compare-mode legacy`.

Exit `1` on drift, missing objects, fetch errors, or extras (with `--detect-extras`).

**Nicer diffs:** pipe stdout through [delta](https://github.com/dandavison/delta) (`-s` for side-by-side; `--paging never` when piping so delta does not open its own pager):

```bash
helmadm drift -n monitoring prometheus --detect-extras | delta -s --paging never
uv run helmadm drift -n monitoring prometheus --detect-extras | delta -s --paging never
```

### `argocd-yaml`

```bash
helmadm ls -n monitoring
helmadm argocd-yaml -n monitoring prometheus
helmadm argocd-yaml -n monitoring prometheus \
  --repo-url https://prometheus-community.github.io/helm-charts
```

Writes overrides to `spec.source.helm.valuesObject`. Fields you must set (destination, project, …) use `CHANGE_ME`. `--debug` adds a `.debug` block — remove before applying to Argo CD.

### `pull`

```bash
helmadm pull -n loki -o ./bundles fluentbit
helmadm pull -n monitoring prometheus --revision 3 -o ./bundles
```

Creates `{namespace}/{release}/` with values files, `helmadm-pull-metadata.yaml`, and a `README.md` with plain `helm install` commands. `--tar` writes a gzip bundle to stdout; `--force` overwrites an existing directory.

## More documentation

- [Command reference](docs/commands.md) — flags, bundle layout, examples
- [Configuration](docs/configuration.md) — environment variables and kubeconfig
- [Developer guide](docs/develop/dev-uv.md) — uv, build, PyPI publish
