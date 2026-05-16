from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from click.core import ParameterSource

from helmadm import logging_config
from helmadm.argocd_manifest import (
    RepoURLMissingError,
    build_application,
    render_application,
    resolve_repo_url,
)
from helmadm.env import (
    ENV_CONTEXT,
    ENV_KUBECONFIG,
    ENV_NAMESPACE,
    ENV_RELEASE_NAME,
    ENV_REPO_URL,
    ENV_TRACE_VALUES,
    resolve_context,
    resolve_namespace,
    resolve_release_name,
    resolve_repo_url_option,
)
from helmadm.chart_values import ChartValuesFetchError, fetch_remote_chart_values
from helmadm.helm_release import (
    HELM_RELEASE_DATA_KEY,
    HelmReleaseDecodeError,
    HelmReleaseNotFoundError,
    _release_status,
    decode_release_data,
    find_release_secret,
    get_release,
    helm_revision_from_secret,
    list_releases,
)
from helmadm.pull_bundle import (
    PullBundleExistsError,
    build_pull_bundle,
    build_pull_bundle_files,
    kubernetes_context_for_pull,
    write_pull_bundle,
    write_pull_bundle_tar,
)
from helmadm.drift import format_report_text, parse_release_manifest, run_drift
from helmadm.k8s import (
    KubernetesApiError,
    check_kubernetes_accessible,
    load_dynamic_client,
    load_kubernetes_client,
)
from helmadm.ls_output import format_release_list
from helmadm.values_diff import (
    ValuesStrategy,
    build_values_debug,
    cluster_values_from_release,
    extract_values_from_release,
    resolve_values_object,
)

logger = logging_config.get_logger("cli")

app = typer.Typer(
    name="helmadm",
    help=(
        "Work with Helm 3 releases stored in Kubernetes: build Argo CD Application YAML, "
        "list releases, and compare a release manifest to live objects."
    ),
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

PANEL_RELEASE = "Release"
PANEL_KUBERNETES = "Kubernetes client"
PANEL_CHART = "Chart source"
PANEL_GLOBAL = "Global"


def _apply_command_verbose(verbose: bool) -> None:
    """Re-apply logging when --verbose is set on a subcommand (global flags only parse before it)."""
    if verbose:
        logging_config.setup_logging(verbose=True)


@app.callback()
def main(
    verbose: Annotated[
        bool,
        typer.Option(
            "-v",
            "--verbose",
            help=(
                "Debug logging on stderr for all subcommands. "
                f"During argocd-yaml, set {ENV_TRACE_VALUES} for per-key "
                "values/diff trace lines (requires --verbose)."
            ),
            rich_help_panel=PANEL_GLOBAL,
        ),
    ] = False,
) -> None:
    """
    Inspect Helm releases in the cluster without the helm or kubectl CLI.

    \b
    Commands:

      [cyan]argocd-yaml[/cyan]  Build an Argo CD Application manifest from a release
      [cyan]pull[/cyan]         Export a reproducible Helm install bundle (values + README)
      [cyan]ls[/cyan]           List Helm releases (Helm 3 secret storage)
      [cyan]drift[/cyan]        Compare the release's stored manifest to live objects (read-only)
    """
    logging_config.setup_logging(verbose=verbose)
    logger.debug("logging configured (verbose=%s)", verbose)


def _kubernetes_client_kwargs(
    *,
    kubeconfig: Path | None,
    context: str | None,
) -> dict[str, str]:
    kwargs: dict[str, str] = {}
    if kubeconfig is not None:
        kwargs["kubeconfig"] = str(kubeconfig)
    if context is not None:
        kwargs["context"] = context
    return kwargs


@dataclass(frozen=True)
class CliOptions:
    namespace: str
    release_name: str
    repo_url: str | None = None
    context: str | None = None
    kubeconfig: Path | None = None
    debug: bool = False


@dataclass(frozen=True)
class PullCliOptions:
    namespace: str
    release_name: str
    output_parent: Path
    repo_url: str | None = None
    repo_name: str | None = None
    revision: int | None = None
    context: str | None = None
    kubeconfig: Path | None = None
    force: bool = False
    tar: bool = False


def _load_release_and_values(
    api: Any,
    namespace: str,
    release_name: str,
    repo_url_override: str | None,
    *,
    revision: int | None = None,
) -> tuple[
    dict[str, Any],
    str,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    ValuesStrategy,
    int,
    str | None,
]:
    secret = find_release_secret(
        api, namespace, release_name, revision=revision
    )
    helm_revision = helm_revision_from_secret(secret)
    status = _release_status(secret)
    encoded = secret.data[HELM_RELEASE_DATA_KEY]
    release = decode_release_data(encoded, expected_name=release_name)

    repo_url = resolve_repo_url(release, repo_url_override)

    chart_metadata = release.get("chart", {}).get("metadata", {})
    chart_name = chart_metadata.get("name")
    chart_version = chart_metadata.get("version")
    if not chart_name or not chart_version:
        raise ValueError("release chart metadata is missing name or version")

    cluster_values = cluster_values_from_release(release)
    remote_defaults = fetch_remote_chart_values(
        repo_url, chart_name, chart_version
    )
    values_object, values_strategy = resolve_values_object(
        cluster_values, remote_defaults
    )
    return (
        release,
        repo_url,
        cluster_values,
        remote_defaults,
        values_object,
        values_strategy,
        helm_revision,
        status,
    )


def run(options: CliOptions) -> int:
    logger.debug(
        "argocd-yaml: namespace=%r release=%r repo_url=%r context=%r kubeconfig=%s",
        options.namespace,
        options.release_name,
        options.repo_url,
        options.context,
        options.kubeconfig,
    )
    client_kwargs = _kubernetes_client_kwargs(
        kubeconfig=options.kubeconfig,
        context=options.context,
    )
    logger.debug("kubernetes client kwargs: %s", client_kwargs)
    try:
        api = load_kubernetes_client(**client_kwargs)
        check_kubernetes_accessible(api)
    except KubernetesApiError as exc:
        logger.debug("kubernetes unavailable: %s", exc)
        typer.echo(f"Error: {exc}", err=True)
        return 1

    try:
        (
            release,
            repo_url,
            cluster_values,
            remote_defaults,
            values_object,
            values_strategy,
            _helm_revision,
            _status,
        ) = _load_release_and_values(
            api,
            options.namespace,
            options.release_name,
            options.repo_url,
        )
    except HelmReleaseNotFoundError as exc:
        logger.debug("release not found: %s", exc)
        typer.echo(str(exc), err=True)
        return 1
    except HelmReleaseDecodeError as exc:
        logger.debug("release decode failed: %s", exc)
        typer.echo(str(exc), err=True)
        return 1
    except RepoURLMissingError as exc:
        logger.debug("repo URL missing: %s", exc)
        typer.echo(str(exc), err=True)
        return 1
    except (TypeError, ValueError) as exc:
        logger.debug("values load failed: %s", exc)
        typer.echo(f"Error: {exc}", err=True)
        return 1
    except ChartValuesFetchError as exc:
        logger.debug("remote chart values fetch failed: %s", exc)
        typer.echo(str(exc), err=True)
        return 1

    logger.debug("argocd-yaml pipeline: release decoded and values loaded")
    logger.debug(
        "argocd-yaml pipeline: valuesObject strategy=%r with %d top-level key(s)",
        values_strategy,
        len(values_object),
    )

    try:
        user_values, chart_values = extract_values_from_release(release)
    except TypeError as exc:
        logger.debug("values extraction failed: %s", exc)
        typer.echo(str(exc), err=True)
        return 1

    debug_info = None
    if options.debug:
        debug_info = build_values_debug(
            release,
            user_values,
            chart_values,
            cluster_values,
            remote_defaults,
            values_object,
            strategy=values_strategy,
        )
        logger.debug("argocd-yaml pipeline: built .debug manifest block")

    try:
        manifest = build_application(
            release, values_object, repo_url, debug=debug_info
        )
    except ValueError as exc:
        logger.debug("manifest build failed: %s", exc)
        typer.echo(str(exc), err=True)
        return 1

    logger.debug("argocd-yaml pipeline: rendering Application manifest to YAML")
    rendered = render_application(manifest)
    logger.debug("argocd-yaml pipeline: rendered manifest (%d bytes)", len(rendered))
    sys.stdout.write(rendered)
    return 0


def run_pull(options: PullCliOptions) -> int:
    logger.debug(
        "pull: namespace=%r release=%r output=%s revision=%s tar=%s force=%s",
        options.namespace,
        options.release_name,
        options.output_parent,
        options.revision,
        options.tar,
        options.force,
    )
    client_kwargs = _kubernetes_client_kwargs(
        kubeconfig=options.kubeconfig,
        context=options.context,
    )
    try:
        api = load_kubernetes_client(**client_kwargs)
        check_kubernetes_accessible(api)
    except KubernetesApiError as exc:
        logger.debug("kubernetes unavailable: %s", exc)
        typer.echo(f"Error: {exc}", err=True)
        return 1

    try:
        (
            release,
            repo_url,
            cluster_values,
            remote_defaults,
            values_object,
            values_strategy,
            helm_revision,
            status,
        ) = _load_release_and_values(
            api,
            options.namespace,
            options.release_name,
            options.repo_url,
            revision=options.revision,
        )
    except HelmReleaseNotFoundError as exc:
        typer.echo(str(exc), err=True)
        return 1
    except HelmReleaseDecodeError as exc:
        typer.echo(str(exc), err=True)
        return 1
    except RepoURLMissingError as exc:
        typer.echo(str(exc), err=True)
        return 1
    except (TypeError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        return 1
    except ChartValuesFetchError as exc:
        typer.echo(str(exc), err=True)
        return 1

    chart_metadata = release.get("chart", {}).get("metadata", {})
    chart_name = chart_metadata.get("name")
    chart_version = chart_metadata.get("version")
    raw_repo_url = chart_metadata.get("repoURL") or ""
    oci_chart = isinstance(raw_repo_url, str) and raw_repo_url.startswith("oci://")

    bundle = build_pull_bundle(
        namespace=options.namespace,
        release_name=options.release_name,
        helm_revision=helm_revision,
        chart_name=chart_name,
        chart_version=chart_version,
        repo_url=repo_url,
        repo_alias=options.repo_name,
        status=status,
        oci_chart=oci_chart,
    )
    k8s_ctx = kubernetes_context_for_pull(
        kubeconfig=options.kubeconfig,
        context=options.context,
    )
    files = build_pull_bundle_files(
        bundle,
        cluster_values=cluster_values,
        values_object=values_object,
        remote_defaults=remote_defaults,
        kubernetes=k8s_ctx,
        values_strategy=values_strategy,
    )

    if options.tar:
        with tempfile.TemporaryDirectory() as temp_parent:
            temp_root = Path(temp_parent)
            bundle_dir = write_pull_bundle(
                temp_root,
                bundle,
                files,
                force=True,
            )
            tarball = write_pull_bundle_tar(temp_root, bundle_dir)
        sys.stdout.buffer.write(tarball)
        return 0

    try:
        bundle_dir = write_pull_bundle(
            options.output_parent,
            bundle,
            files,
            force=options.force,
        )
    except PullBundleExistsError as exc:
        typer.echo(f"Error: {exc}", err=True)
        return 1

    typer.echo(bundle_dir)
    return 0


def run_ls(
    *,
    namespace: str | None,
    all_namespaces: bool,
    detail: bool,
    kubeconfig: Path | None,
    context: str | None,
) -> int:
    logger.debug(
        "ls: namespace=%r all_namespaces=%s detail=%s context=%r kubeconfig=%s",
        namespace,
        all_namespaces,
        detail,
        context,
        kubeconfig,
    )
    client_kwargs = _kubernetes_client_kwargs(
        kubeconfig=kubeconfig, context=context
    )
    logger.debug("kubernetes client kwargs: %s", client_kwargs)
    try:
        api = load_kubernetes_client(**client_kwargs)
        check_kubernetes_accessible(api)
    except KubernetesApiError as exc:
        logger.debug("kubernetes unavailable: %s", exc)
        typer.echo(f"Error: {exc}", err=True)
        return 1

    try:
        releases = list_releases(
            api,
            namespace=namespace,
            all_namespaces=all_namespaces,
            detail=detail,
        )
    except HelmReleaseDecodeError as exc:
        logger.debug("release list decode failed: %s", exc)
        typer.echo(str(exc), err=True)
        return 1

    logger.debug("listing %d release(s)", len(releases))
    typer.echo(format_release_list(releases, detail=detail))
    return 0


def run_drift_command(
    *,
    namespace: str,
    release_name: str,
    kubeconfig: Path | None,
    context: str | None,
    detect_extras: bool,
    ignore_annotations: bool = False,
) -> int:
    client_kwargs = _kubernetes_client_kwargs(
        kubeconfig=kubeconfig,
        context=context,
    )
    logger.debug(
        "drift: namespace=%r release=%r detect_extras=%s context=%r kubeconfig=%s",
        namespace,
        release_name,
        detect_extras,
        context,
        kubeconfig,
    )
    try:
        api = load_kubernetes_client(**client_kwargs)
        check_kubernetes_accessible(api)
    except KubernetesApiError as exc:
        logger.debug("kubernetes unavailable for drift: %s", exc)
        typer.echo(f"Error: {exc}", err=True)
        return 1

    try:
        release = get_release(api, namespace, release_name)
    except HelmReleaseNotFoundError as exc:
        logger.debug("release not found: %s", exc)
        typer.echo(str(exc), err=True)
        return 1
    except HelmReleaseDecodeError as exc:
        logger.debug("release decode failed: %s", exc)
        typer.echo(str(exc), err=True)
        return 1

    try:
        manifest_docs = parse_release_manifest(release)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        return 1
    if not manifest_docs:
        typer.echo(
            "Error: release has no Helm `manifest` (or it is empty); nothing to compare.",
            err=True,
        )
        return 1

    try:
        dyn_client = load_dynamic_client(**client_kwargs)
    except KubernetesApiError as exc:
        logger.debug("kubernetes dynamic client load failed: %s", exc)
        typer.echo(f"Error: {exc}", err=True)
        return 1

    report = run_drift(
        dyn_client,
        release,
        release_namespace=namespace,
        release_name=release_name,
        detect_extras=detect_extras,
    )
    typer.echo(
        format_report_text(report, ignore_annotations=ignore_annotations)
    )
    return 1 if report.has_problem else 0


@app.command(
    "argocd-yaml",
    help="Print an Argo CD Application manifest for a Helm release (stdout).",
)
def argocd_yaml(
    ctx: typer.Context,
    verbose: Annotated[
        bool,
        typer.Option(
            "-v",
            "--verbose",
            help=(
                "Debug logging on stderr. "
                f"Set {ENV_TRACE_VALUES} for per-key values/diff trace during this command."
            ),
            rich_help_panel=PANEL_GLOBAL,
        ),
    ] = False,
    release_name: Annotated[
        str | None,
        typer.Argument(
            help=f"Helm release name (positional). [env: {ENV_RELEASE_NAME}]",
        ),
    ] = None,
    namespace: Annotated[
        str | None,
        typer.Option(
            "-n",
            "--namespace",
            help=(
                "Namespace of the Helm release. "
                f"Default: ${ENV_NAMESPACE}, else the current kubeconfig context namespace."
            ),
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = None,
    kubeconfig: Annotated[
        Path | None,
        typer.Option(
            "--kubeconfig",
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help=(
                "Kubeconfig file. If omitted, uses "
                f"${ENV_KUBECONFIG} or ~/.kube/config (kubectl behavior)."
            ),
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            help=f"Kubeconfig context. [env: {ENV_CONTEXT}]",
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
    repo_url: Annotated[
        str | None,
        typer.Option(
            "--repo-url",
            help=(
                "Chart repository URL when the release has no chart.metadata.repoURL "
                "(see NEEDS_REPO_URL in helmadm ls). "
                f"[env: {ENV_REPO_URL}]"
            ),
            rich_help_panel=PANEL_CHART,
        ),
    ] = None,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help=(
                "Add a .debug section to the YAML (cluster values, remote chart defaults, "
                "diff strategy, ignoreAnnotations). Enables stderr debug logging. "
                "Remove .debug before applying to Argo CD."
            ),
            rich_help_panel=PANEL_GLOBAL,
        ),
    ] = False,
) -> None:
    """
    Read a Helm 3 release from cluster storage and print one Argo CD [cyan]Application[/cyan] manifest.

    Values: coalesced [cyan]chart.values[/cyan] + [cyan]release.config[/cyan] from the release are
    compared to [cyan]helm show values[/cyan] for the chart version (fetched from [cyan]--repo-url[/cyan]
    or chart.metadata.repoURL). Only differences become [cyan]spec.source.helm.valuesObject[/cyan].

    Argo CD fields you must set yourself ([cyan]metadata.name[/cyan], [cyan]project[/cyan],
    [cyan]destination[/cyan], …) use [yellow]CHANGE_ME[/yellow] placeholders.

    [cyan]--debug[/cyan] embeds a [cyan].debug[/cyan] block with the raw inputs and diff metadata
    (including [cyan]ignoreAnnotations[/cyan] describing normalization). Strip it before commit/apply.

    \b
    Examples:

      helmadm ls -n monitoring
      helmadm argocd-yaml -n monitoring prometheus
      helmadm argocd-yaml -n monitoring prometheus > application.yaml
      helmadm argocd-yaml -n monitoring prometheus \\
          --repo-url https://prometheus-community.github.io/helm-charts
      helmadm argocd-yaml --debug -n keda keda
    """
    _apply_command_verbose(verbose)
    resolved_namespace = resolve_namespace(namespace, kubeconfig)
    resolved_release_name = resolve_release_name(release_name)
    resolved_context = resolve_context(context)
    resolved_repo_url = resolve_repo_url_option(repo_url)
    if debug:
        logging_config.setup_logging(verbose=True)
        logger.debug("stderr debug logging enabled via --debug")
    logger.debug(
        "argocd-yaml resolved: namespace=%r release=%r context=%r repo_url=%r debug=%s",
        resolved_namespace,
        resolved_release_name,
        resolved_context,
        resolved_repo_url,
        debug,
    )

    if not resolved_namespace and not resolved_release_name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    if not resolved_namespace:
        typer.echo(
            "Error: namespace is required. Use -n/--namespace, "
            f"{ENV_NAMESPACE}, or set a default namespace in your kubeconfig context.",
            err=True,
        )
        raise typer.Exit(code=2)
    if not resolved_release_name:
        typer.echo(
            f"Error: release name is required. Pass it as an argument or set "
            f"{ENV_RELEASE_NAME}.",
            err=True,
        )
        raise typer.Exit(code=2)

    options = CliOptions(
        namespace=resolved_namespace,
        release_name=resolved_release_name,
        repo_url=resolved_repo_url,
        context=resolved_context,
        kubeconfig=kubeconfig,
        debug=debug,
    )
    raise typer.Exit(run(options))


@app.command(
    "drift",
    help="Compare the release manifest to live objects (read-only; unified diffs on drift).",
)
def drift_command(
    ctx: typer.Context,
    verbose: Annotated[
        bool,
        typer.Option(
            "-v",
            "--verbose",
            help="Debug logging on stderr (API fetch paths, detect-extras scans).",
            rich_help_panel=PANEL_GLOBAL,
        ),
    ] = False,
    release_name: Annotated[
        str | None,
        typer.Argument(
            help=f"Helm release name (positional). [env: {ENV_RELEASE_NAME}]",
        ),
    ] = None,
    namespace: Annotated[
        str | None,
        typer.Option(
            "-n",
            "--namespace",
            help=(
                "Namespace of the Helm release. "
                f"Default: ${ENV_NAMESPACE}, else the current kubeconfig context namespace."
            ),
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = None,
    kubeconfig: Annotated[
        Path | None,
        typer.Option(
            "--kubeconfig",
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help=(
                "Kubeconfig file. If omitted, uses "
                f"${ENV_KUBECONFIG} or ~/.kube/config (kubectl behavior)."
            ),
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            help=f"Kubeconfig context. [env: {ENV_CONTEXT}]",
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
    detect_extras: Annotated[
        bool,
        typer.Option(
            "--detect-extras",
            help=(
                "LIST every namespaced API kind in -n and flag objects not in the release "
                "manifest (includes unlabeled resources; needs broad list RBAC). "
                "Helm release storage secrets (helm.sh/release.v1) are skipped."
            ),
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = False,
    ignore_annotations: Annotated[
        bool,
        typer.Option(
            "--ignore-annotations",
            "-ia",
            help=(
                "Before each unified diff, print # helmadm lines listing normalization rules "
                "(metadata noise, Helm/kubectl annotations, Service clusterIP/nodePort, "
                "Pod template defaults, …)."
            ),
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = False,
) -> None:
    """
    Compare each object in the Helm release [cyan]manifest[/cyan] to the live API object (read-only).

    Does **not** run helm upgrade or kubectl apply. Helm hook manifests are not in [cyan]manifest[/cyan]
    and are not checked.

    Before compare, both sides are normalized (drop [cyan]status[/cyan], server metadata, Helm/kubectl
    install-time annotations, common Service and Pod defaults, etc.). Drifting objects get a unified
    diff ([cyan]manifest/...[/cyan] vs [cyan]live/...[/cyan]). False positives are still possible
    (e.g. env list order, Secret [cyan]data[/cyan] vs [cyan]stringData[/cyan]).

    Use [cyan]--ignore-annotations[/cyan] / [cyan]-ia[/cyan] to print the full normalization checklist
    above each diff. Without it, only the diff is shown.

    [cyan]--detect-extras[/cyan] reports namespaced objects in [cyan]-n[/cyan] that are absent from
    the manifest (manual installs, other controllers).

    Exit [cyan]0[/cyan] when every manifest object matches; [cyan]1[/cyan] on drift, missing object,
    fetch error, or any extra (with [cyan]--detect-extras[/cyan]).

    \b
    Examples:

      helmadm drift -n monitoring prometheus
      helmadm drift -n monitoring prometheus | delta -s
      helmadm drift --detect-extras -n monitoring prometheus
      helmadm drift -ia -n kube-system traefik
    """
    _apply_command_verbose(verbose)
    resolved_namespace = resolve_namespace(namespace, kubeconfig)
    resolved_release_name = resolve_release_name(release_name)
    resolved_context = resolve_context(context)
    logger.debug(
        "drift resolved: namespace=%r release=%r context=%r detect_extras=%s",
        resolved_namespace,
        resolved_release_name,
        resolved_context,
        detect_extras,
    )

    if not resolved_namespace and not resolved_release_name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    if not resolved_namespace:
        typer.echo(
            "Error: namespace is required. Use -n/--namespace, "
            f"{ENV_NAMESPACE}, or set a default namespace in your kubeconfig context.",
            err=True,
        )
        raise typer.Exit(code=2)
    if not resolved_release_name:
        typer.echo(
            f"Error: release name is required. Pass it as an argument or set "
            f"{ENV_RELEASE_NAME}.",
            err=True,
        )
        raise typer.Exit(code=2)

    raise typer.Exit(
        run_drift_command(
            namespace=resolved_namespace,
            release_name=resolved_release_name,
            kubeconfig=kubeconfig,
            context=resolved_context,
            detect_extras=detect_extras,
            ignore_annotations=ignore_annotations,
        )
    )


@app.command(
    "pull",
    help="Write a reproducible Helm install bundle (values files + README) from a release.",
)
def pull(
    ctx: typer.Context,
    verbose: Annotated[
        bool,
        typer.Option(
            "-v",
            "--verbose",
            help="Debug logging on stderr.",
            rich_help_panel=PANEL_GLOBAL,
        ),
    ] = False,
    release_name: Annotated[
        str | None,
        typer.Argument(
            help=f"Helm release name (positional). [env: {ENV_RELEASE_NAME}]",
        ),
    ] = None,
    namespace: Annotated[
        str | None,
        typer.Option(
            "-n",
            "--namespace",
            help=(
                "Namespace of the Helm release. "
                f"Default: ${ENV_NAMESPACE}, else the current kubeconfig context namespace."
            ),
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "-o",
            "--output",
            file_okay=False,
            dir_okay=True,
            writable=True,
            resolve_path=True,
            help=(
                "Parent directory for the bundle. Creates "
                "{namespace}/{release}/ inside it."
            ),
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = None,
    revision: Annotated[
        int | None,
        typer.Option(
            "--revision",
            min=1,
            help="Helm release revision to pull (default: latest).",
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = None,
    kubeconfig: Annotated[
        Path | None,
        typer.Option(
            "--kubeconfig",
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help=(
                "Kubeconfig file. If omitted, uses "
                f"${ENV_KUBECONFIG} or ~/.kube/config (kubectl behavior)."
            ),
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            help=f"Kubeconfig context. [env: {ENV_CONTEXT}]",
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
    repo_url: Annotated[
        str | None,
        typer.Option(
            "--repo-url",
            help=(
                "Chart repository URL when the release has no chart.metadata.repoURL "
                "(see NEEDS_REPO_URL in helmadm ls). "
                f"[env: {ENV_REPO_URL}]"
            ),
            rich_help_panel=PANEL_CHART,
        ),
    ] = None,
    repo_name: Annotated[
        str | None,
        typer.Option(
            "--repo-name",
            help="Helm repo alias for README commands (default: derived from repo URL host).",
            rich_help_panel=PANEL_CHART,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing bundle directory.",
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = False,
    tar: Annotated[
        bool,
        typer.Option(
            "--tar",
            help="Write a gzip tarball of the bundle to stdout (no files left on disk).",
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = False,
) -> None:
    """
    Export a **local bundle** from a Helm 3 release so it can be reinstalled with plain helm.

    Writes [cyan]{namespace}/{release}/[/cyan] under [cyan]-o[/cyan] / [cyan]--output[/cyan]:

    - [cyan]helmadm-pull-metadata.yaml[/cyan] — when pulled, kubeconfig/context, release and chart info
    - [cyan]{chart}.all.values.yaml[/cyan] — effective cluster values (coalesced)
    - [cyan]{chart}.changed.values.yaml[/cyan] — overrides only (diff vs remote chart defaults)
    - [cyan]{chart}.remote-all.values.yaml[/cyan] — chart defaults from the repository
    - [cyan]README.md[/cyan] — helm install/template commands for each values file

    \b
    Examples:

      helmadm pull -n loki -o ./bundles fluentbit
      helmadm pull -n monitoring prometheus --revision 3 -o ./bundles
      helmadm pull -n loki -o ./bundles fluentbit --tar > fluentbit-bundle.tar.gz
    """
    _apply_command_verbose(verbose)
    resolved_namespace = resolve_namespace(namespace, kubeconfig)
    resolved_release_name = resolve_release_name(release_name)
    resolved_context = resolve_context(context)
    resolved_repo_url = resolve_repo_url_option(repo_url)

    if not resolved_namespace and not resolved_release_name and output is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    if not resolved_namespace:
        typer.echo(
            "Error: namespace is required. Use -n/--namespace, "
            f"{ENV_NAMESPACE}, or set a default namespace in your kubeconfig context.",
            err=True,
        )
        raise typer.Exit(code=2)
    if not resolved_release_name:
        typer.echo(
            f"Error: release name is required. Pass it as an argument or set "
            f"{ENV_RELEASE_NAME}.",
            err=True,
        )
        raise typer.Exit(code=2)
    if output is None and not tar:
        typer.echo(
            "Error: --output / -o is required (unless using --tar).",
            err=True,
        )
        raise typer.Exit(code=2)
    if output is None and tar:
        output_parent = Path(tempfile.gettempdir())
    else:
        output_parent = output

    options = PullCliOptions(
        namespace=resolved_namespace,
        release_name=resolved_release_name,
        output_parent=output_parent,
        repo_url=resolved_repo_url,
        repo_name=repo_name,
        revision=revision,
        context=resolved_context,
        kubeconfig=kubeconfig,
        force=force,
        tar=tar,
    )
    raise typer.Exit(run_pull(options))


@app.command(
    "ls",
    help="List Helm 3 releases from cluster secret storage.",
)
def ls(
    ctx: typer.Context,
    verbose: Annotated[
        bool,
        typer.Option(
            "-v",
            "--verbose",
            help="Debug logging on stderr.",
            rich_help_panel=PANEL_GLOBAL,
        ),
    ] = False,
    namespace: Annotated[
        str | None,
        typer.Option(
            "-n",
            "--namespace",
            help="Limit to one namespace. If omitted, lists all namespaces.",
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = None,
    all_namespaces: Annotated[
        bool,
        typer.Option(
            "-A",
            "--all-namespaces",
            help="Same as omitting -n: list releases in every namespace.",
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = False,
    detail: Annotated[
        bool,
        typer.Option(
            "--detail/--no-detail",
            help=(
                "With detail (default): chart, version, stored repoURL, and NEEDS_REPO_URL "
                "(yes = pass --repo-url to argocd-yaml). Without: name/revision/status only."
            ),
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = True,
    kubeconfig: Annotated[
        Path | None,
        typer.Option(
            "--kubeconfig",
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help=(
                "Kubeconfig file. If omitted, uses "
                f"${ENV_KUBECONFIG} or ~/.kube/config (kubectl behavior)."
            ),
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            help=f"Kubeconfig context. [env: {ENV_CONTEXT}]",
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
) -> None:
    """
    Tabular list of Helm releases decoded from Kubernetes (Helm 3 secret driver).

    Default: all namespaces, detailed columns. Use [cyan]-n[/cyan] for one namespace.
    [cyan]NEEDS_REPO_URL=yes[/cyan] means [cyan]argocd-yaml[/cyan] needs [cyan]--repo-url[/cyan]
    because the release lacks [cyan]chart.metadata.repoURL[/cyan].

    \b
    Examples:

      helmadm ls
      helmadm ls -n monitoring
      helmadm ls --no-detail
    """
    _apply_command_verbose(verbose)
    namespace_from_cli = (
        ctx.get_parameter_source("namespace") == ParameterSource.COMMANDLINE
    )
    if namespace_from_cli:
        list_all = False
        resolved_namespace = namespace
    else:
        list_all = True
        resolved_namespace = None

    resolved_context = resolve_context(context)
    logger.debug(
        "ls resolved: namespace=%r all_namespaces=%s detail=%s context=%r",
        resolved_namespace,
        list_all,
        detail,
        resolved_context,
    )
    raise typer.Exit(
        run_ls(
            namespace=resolved_namespace,
            all_namespaces=list_all,
            detail=detail,
            kubeconfig=kubeconfig,
            context=resolved_context,
        )
    )


def cli_entry() -> None:
    app()
