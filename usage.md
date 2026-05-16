


```bash
uv run helmadm ls

```


```bash

uv run helmadm drift -n kube-system traefik
uv run helmadm drift -n kube-system traefik  -h


 uv run helmadm drift -n kube-system traefik --detect-extras -v | delta


 uv run helmadm drift -n monitoring blackbox-exporter
 uv run helmadm drift -n monitoring blackbox-exporter | delta
 uv run helmadm drift -n nfs-storage nfs-provisioner | delta
 delta -h
 uv run helmadm drift -n nfs-storage nfs-provisioner | delta -s
 uv run helmadm ls
 uv run helmadm drift -n nfs-storage nfs-provisioner --detect-extras | delta -s
 uv run helmadm drift -n keycloak-26 keycloak-26 --detect-extras | delta -s
 uv run helmadm drift -n kube-system metallb --detect-extras | delta -s
 uv run helmadm drift -n kube-system metallb --detect-extras | delta -s


```

```bash


uv run helmadm argocd-yaml --repo-url https://prometheus-community.github.io/helm-charts -n monitoring blackbox-exporter

uv run helmadm argocd-yaml -n keda --repo-url https://kedacore.github.io/charts keda
uv run helmadm argocd-yaml -n keda --repo-url https://kedacore.github.io/charts keda --verbose


uv run helmadm --verbose argocd-yaml -n keda --repo-url https://kedacore.github.io/charts --debug keda


uv run helmadm --verbose argocd-yaml -n keda --repo-url https://kedacore.github.io/charts --debug keda | code -
uv run helmadm --verbose argocd-yaml --repo-url https://kedacore.github.io/charts --debug -n monitoring prometheus | code -
uv run helmadm --verbose argocd-yaml --repo-url https://prometheus-community.github.io/helm-charts --debug -n monitoring prometheus | code -
uv run helmadm --verbose argocd-yaml --repo-url https://prometheus-community.github.io/helm-charts  -n monitoring prometheus | yq .
uv run helmadm --verbose argocd-yaml --repo-url https://prometheus-community.github.io/helm-charts  -n monitoring prometheus | code -
time uv run helmadm argocd-yaml --repo-url https://prometheus-community.github.io/helm-charts  -n monitoring prometheus
uv run helmadm argocd-yaml --repo-url https://prometheus-community.github.io/helm-charts -n monitoring blackbox-exporter
uv run helmadm argocd-yaml --repo-url https://prometheus-community.github.io/helm-charts -n monitoring prometheus
uv run helmadm argocd-yaml --repo-url https://traefik.github.io/charts -n kube-system traefik -v | yq .
uv run helmadm argocd-yaml --repo-url https://prometheus-community.github.io/helm-charts -n monitoring blackbox-exporter\n
uv run helmadm drift -n monitoring blackbox-exporter -ia -v | delta -s --paging never
uv run helmadm argocd-yaml -n monitoring      blackbox-exporter -v


```
