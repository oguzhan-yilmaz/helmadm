from helmadm.helm_release import HelmReleaseSummary
from helmadm.logging_config import get_logger

logger = get_logger("ls_output")


def _column_widths(rows: list[list[str]]) -> list[int]:
    if not rows:
        return []
    column_count = max(len(row) for row in rows)
    widths = [0] * column_count
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    return widths


def _format_rows(headers: list[str], rows: list[list[str]]) -> str:
    table = [headers, *rows]
    widths = _column_widths(table)
    lines = []
    for row_index, row in enumerate(table):
        line = "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
        lines.append(line)
        if row_index == 0:
            lines.append("  ".join("-" * width for width in widths))
    return "\n".join(lines)


def format_release_list(
    releases: list[HelmReleaseSummary],
    *,
    detail: bool = False,
) -> str:
    logger.debug("formatting %d release(s) (detail=%s)", len(releases), detail)
    if not releases:
        logger.debug("no releases to display")
        return "No Helm releases found."

    if detail:
        headers = [
            "NAMESPACE",
            "RELEASE",
            "REVISION",
            "CHART",
            "VERSION",
            "REPO_URL",
            "NEEDS_REPO_URL",
        ]
        rows = [
            [
                item.namespace,
                item.name,
                str(item.revision),
                item.chart_name or "-",
                item.chart_version or "-",
                item.repo_url or "-",
                "yes" if item.needs_repo_url else "no",
            ]
            for item in releases
        ]
    else:
        headers = ["NAMESPACE", "RELEASE", "REVISION", "STATUS"]
        rows = [
            [
                item.namespace,
                item.name,
                str(item.revision),
                item.status or "-",
            ]
            for item in releases
        ]

    output = _format_rows(headers, rows)
    logger.debug("formatted table (%d bytes)", len(output))
    return output
