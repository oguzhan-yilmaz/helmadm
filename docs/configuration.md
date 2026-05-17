# Configuration

CLI flags take precedence over environment variables.

## Environment variables

| Flag | Environment variable | Notes |
|------|----------------------|-------|
| `-n` / `--namespace` | `HELMADM_NAMESPACE` | Or current kubeconfig context default namespace |
| `--context` | `HELMADM_CONTEXT` | Kubeconfig context |
| `--repo-url` | `HELMADM_REPO_URL` | Chart repository URL |
| release name (positional) | `HELMADM_RELEASE_NAME` | |
| (values trace, `argocd-yaml` with `-v`) | `HELMADM_TRACE_VALUES` | Per-key diff trace on stderr |
| (Kubernetes HTTP) | `HELMADM_K8S_CONNECT_TIMEOUT` | Connect timeout in seconds (default: `5`) |
| (Kubernetes HTTP) | `HELMADM_K8S_READ_TIMEOUT` | Read timeout in seconds (default: `60`) |

## Kubeconfig

`--kubeconfig` follows kubectl behavior: use the flag, or `KUBECONFIG`, or `~/.kube/config`.

## Logging

- `-v` / `--verbose` on any subcommand enables debug logging on stderr.
- `argocd-yaml --debug` also enables stderr debug logging and embeds a `.debug` block in the YAML output.
