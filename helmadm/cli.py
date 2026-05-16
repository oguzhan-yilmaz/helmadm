from __future__ import annotations

import sys
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
    resolve_context,
    resolve_namespace,
    resolve_release_name,
    resolve_repo_url_option,
)
from helmadm.chart_values import ChartValuesFetchError, fetch_remote_chart_values
from helmadm.helm_release import (
    HelmReleaseDecodeError,
    HelmReleaseNotFoundError,
    get_release,
    list_releases,
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
    build_values_debug,
    cluster_values_from_release,
    extract_values_from_release,
    resolve_values_object,
)

logger = logging_config.get_logger("cli")

app = typer.Typer(
    name="helmadm",
    help="Generate Argo CD Application manifests from Helm releases in Kubernetes.",
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
                "Enable debug logging on stderr. "
                "Set HELM_TO_ARGOCD_TRACE_VALUES=1 for per-key values/diff logs."
            ),
            rich_help_panel=PANEL_GLOBAL,
        ),
    ] = False,
) -> None:
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


def run(options: CliOptions) -> int:
    logger.debug(
        "convert: namespace=%r release=%r repo_url=%r context=%r kubeconfig=%s",
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
        release = get_release(api, options.namespace, options.release_name)
    except HelmReleaseNotFoundError as exc:
        logger.debug("release not found: %s", exc)
        typer.echo(str(exc), err=True)
        return 1
    except HelmReleaseDecodeError as exc:
        logger.debug("release decode failed: %s", exc)
        typer.echo(str(exc), err=True)
        return 1

    logger.debug("convert pipeline: release decoded and validated")

    try:
        repo_url = resolve_repo_url(release, options.repo_url)
    except RepoURLMissingError as exc:
        logger.debug("repo URL missing: %s", exc)
        typer.echo(str(exc), err=True)
        return 1

    logger.debug("convert pipeline: repo URL resolved to %r", repo_url)

    try:
        user_values, chart_values = extract_values_from_release(release)
        cluster_values = cluster_values_from_release(release)
    except TypeError as exc:
        logger.debug("values extraction failed: %s", exc)
        typer.echo(str(exc), err=True)
        return 1

    chart_metadata = release.get("chart", {}).get("metadata", {})
    chart_name = chart_metadata.get("name")
    chart_version = chart_metadata.get("version")
    if not chart_name or not chart_version:
        typer.echo(
            "Error: release chart metadata is missing name or version",
            err=True,
        )
        return 1

    logger.debug("convert pipeline: fetching remote chart defaults from %r", repo_url)
    try:
        remote_defaults = fetch_remote_chart_values(
            repo_url, chart_name, chart_version
        )
    except ChartValuesFetchError as exc:
        logger.debug("remote chart values fetch failed: %s", exc)
        typer.echo(str(exc), err=True)
        return 1

    logger.debug(
        "convert pipeline: diffing cluster values against remote chart defaults"
    )
    values_object, values_strategy = resolve_values_object(
        cluster_values, remote_defaults
    )
    logger.debug(
        "convert pipeline: valuesObject strategy=%r with %d top-level key(s)",
        values_strategy,
        len(values_object),
    )

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
        logger.debug("convert pipeline: built .debug manifest block")

    try:
        manifest = build_application(
            release, values_object, repo_url, debug=debug_info
        )
    except ValueError as exc:
        logger.debug("manifest build failed: %s", exc)
        typer.echo(str(exc), err=True)
        return 1

    logger.debug("convert pipeline: rendering Application manifest to YAML")
    rendered = render_application(manifest)
    logger.debug("convert pipeline: rendered manifest (%d bytes)", len(rendered))
    sys.stdout.write(rendered)
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
    "convert",
    help="Generate an Argo CD Application manifest from a Helm release.",
)
def convert(
    ctx: typer.Context,
    verbose: Annotated[
        bool,
        typer.Option(
            "-v",
            "--verbose",
            help=(
                "Enable debug logging on stderr. "
                "Set HELM_TO_ARGOCD_TRACE_VALUES=1 for per-key values/diff logs."
            ),
            rich_help_panel=PANEL_GLOBAL,
        ),
    ] = False,
    release_name: Annotated[
        str | None,
        typer.Argument(
            help=f"Helm release name. [env: {ENV_RELEASE_NAME}]",
        ),
    ] = None,
    namespace: Annotated[
        str | None,
        typer.Option(
            "-n",
            "--namespace",
            help=(
                "Namespace where the Helm release is installed. "
                f"Falls back to ${ENV_NAMESPACE} or the current kubeconfig context."
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
                "Kubeconfig file path. When omitted, uses "
                f"${ENV_KUBECONFIG} or ~/.kube/config (same as kubectl)."
            ),
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            help=f"Kubeconfig context to use. [env: {ENV_CONTEXT}]",
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
    repo_url: Annotated[
        str | None,
        typer.Option(
            "--repo-url",
            help=(
                "Helm chart repository URL when chart.metadata.repoURL is missing. "
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
                "Include a .debug block in the manifest with cluster values "
                "(release.config, chart.values) and diff diagnostics. "
                "Also enables stderr debug logging."
            ),
            rich_help_panel=PANEL_GLOBAL,
        ),
    ] = False,
) -> None:
    """
    Read a Helm release from the cluster and print an Argo CD Application manifest.

    Non-helm fields use [yellow]CHANGE_ME[/yellow] placeholders.
    Overrides are written to [cyan]spec.source.helm.valuesObject[/cyan].

    Use [cyan]--debug[/cyan] to embed [cyan].debug[/cyan] in the YAML with raw values
    from the cluster and the diff result (remove before applying to Argo CD).

    \b
    Examples:

      helmadm convert -n monitoring prometheus
      helmadm convert -n monitoring prometheus > application.yaml
      helmadm convert -n monitoring prometheus \\
          --repo-url https://prometheus-community.github.io/helm-charts
      helmadm --debug convert -n keda keda
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
        "convert resolved: namespace=%r release=%r context=%r repo_url=%r debug=%s",
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
    raise typer.Exit(run(options)    )


@app.command(
    "drift",
    help=(
        "Compare the Helm release's stored rendered manifest against live cluster objects "
        "(read-only)."
    ),
)
def drift_command(
    ctx: typer.Context,
    verbose: Annotated[
        bool,
        typer.Option(
            "-v",
            "--verbose",
            help="Enable debug logging on stderr.",
            rich_help_panel=PANEL_GLOBAL,
        ),
    ] = False,
    release_name: Annotated[
        str | None,
        typer.Argument(
            help=f"Helm release name. [env: {ENV_RELEASE_NAME}]",
        ),
    ] = None,
    namespace: Annotated[
        str | None,
        typer.Option(
            "-n",
            "--namespace",
            help=(
                "Namespace where the Helm release is installed. "
                f"Falls back to ${ENV_NAMESPACE} or the current kubeconfig context."
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
                "Kubeconfig file path. When omitted, uses "
                f"${ENV_KUBECONFIG} or ~/.kube/config (same as kubectl)."
            ),
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            help=f"Kubeconfig context to use. [env: {ENV_CONTEXT}]",
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
    detect_extras: Annotated[
        bool,
        typer.Option(
            "--detect-extras",
            help=(
                "List every namespaced resource kind in the release namespace and report "
                "objects missing from the Helm manifest (includes resources without Helm labels; "
                "needs broad list RBAC)."
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
                "Prefix each unified diff with # helmadm lines describing fields stripped "
                "for compare (metadata noise, Service runtime fields, Pod defaults, …)."
            ),
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = False,
) -> None:
    """
    Read the Helm 3 manifest stored with the release, fetch matching live objects, and print a diff summary.

    This command does **not** change the cluster. Hook resources stay out of manifest and are omitted (Helm hooks are stored separately from `manifest`).
    Comparisons omit `metadata` fields populated by Kubernetes (uids, timestamps, ...) and omit `status`, so false positives remain possible—e.g. list ordering differences on `containers[*].env`, or Secrets `data` vs `stringData`.

    Helm injects ``meta.helm.sh/release-name`` and ``meta.helm.sh/release-namespace`` at install time; drift ignores those annotations when diffing against ``manifest``. Similarly ignored: ``kubectl.kubernetes.io/restartedAt``; label ``app.kubernetes.io/managed-by: Helm``; on ``Service`` objects, apiserver-filled ``clusterIP`` / ``clusterIPs``, ``ipFamilies``, ``ipFamilyPolicy``, ``internalTrafficPolicy``, per-port ``nodePort`` on the **live** side only (manifest ``nodePort`` is kept so pinned chart ports still compare), and ``sessionAffinity`` when unset or the API default ``None`` / ``"None"``.

    All ``metadata.namespace`` fields are ignored when comparing (templates often omit namespace). ``metadata.annotations`` / ``labels`` set to YAML ``null`` are treated like omitted keys.

    For ``Deployment`` (and other objects with embedded Pod templates: DaemonSet, StatefulSet, ReplicaSet, Job, CronJob, Pod), drift ignores ``deployment.kubernetes.io/revision`` on Deployments, default ``progressDeadlineSeconds`` (600), and common defaulted Pod / container fields such as ``schedulerName``, ``hostNetwork``, ``dnsPolicy``, ``terminationGracePeriodSeconds``, ``restartPolicy`` when ``Always``, ``terminationMessagePath``, ``terminationMessagePolicy``, empty ``securityContext`` and ``resources`` objects on containers, and redundant ``serviceAccount`` when ``serviceAccountName`` is present.

    Exit code ``1`` if any drift, missing manifest object, extras (with ``--detect-extras``), or fetch error occurs; ``0`` when everything matches.

    With ``--detect-extras``, every namespaced API resource type is listed in ``-n``; objects not named in the Helm ``manifest`` are reported (including manually applied resources without Helm labels). Helm release storage Secrets (type ``helm.sh/release.v1``) are omitted from that scan.

    \b
    Examples:

      helmadm drift -n monitoring prometheus
      helmadm drift --detect-extras -n monitoring prometheus
      helmadm drift --ignore-annotations -n monitoring prometheus
      helmadm drift -ia -n monitoring prometheus
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
    "ls",
    help="List Helm releases stored in the cluster.",
)
def ls(
    ctx: typer.Context,
    verbose: Annotated[
        bool,
        typer.Option(
            "-v",
            "--verbose",
            help=(
                "Enable debug logging on stderr. "
                "Set HELM_TO_ARGOCD_TRACE_VALUES=1 for per-key values/diff logs."
            ),
            rich_help_panel=PANEL_GLOBAL,
        ),
    ] = False,
    namespace: Annotated[
        str | None,
        typer.Option(
            "-n",
            "--namespace",
            help="Namespace to list. Omit to list all namespaces (default).",
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = None,
    all_namespaces: Annotated[
        bool,
        typer.Option(
            "-A",
            "--all-namespaces",
            help="List releases in all namespaces (default when -n is omitted).",
            rich_help_panel=PANEL_RELEASE,
        ),
    ] = False,
    detail: Annotated[
        bool,
        typer.Option(
            "--detail/--no-detail",
            help=(
                "Show chart name, version, repo URL, and whether --repo-url is "
                "required for convert. [default: detail on]"
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
                "Kubeconfig file path. When omitted, uses "
                f"${ENV_KUBECONFIG} or ~/.kube/config (same as kubectl)."
            ),
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            help=f"Kubeconfig context to use. [env: {ENV_CONTEXT}]",
            rich_help_panel=PANEL_KUBERNETES,
        ),
    ] = None,
) -> None:
    """
    List Helm releases from Kubernetes secrets (Helm 3 storage driver).

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
