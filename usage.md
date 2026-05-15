


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


uv run helmadm convert --repo-url https://prometheus-community.github.io/helm-charts -n monitoring blackbox-exporter


```
