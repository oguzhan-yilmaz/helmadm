import copy
from typing import Any, Literal

from helmadm.logging_config import get_logger, trace_values

logger = get_logger("values_diff")

ValuesStrategy = Literal["remote.diff", "empty"]

VALUES_DIFF_IGNORE_ANNOTATIONS: tuple[str, ...] = (
    "valuesObject is cluster chart.values+config coalesced, diffed against helm show values "
    "(remote chart defaults).",
    "YAML null, empty string, empty list, and empty dict are treated as equal to a missing key "
    "(Helm omit vs explicit empty).",
    "Keys and nested branches equal after that normalization are omitted; unchanged subtrees "
    "are pruned from the diff.",
)


def _is_emptyish(value: Any) -> bool:
    """Values often omitted from values.yaml but present as empty in merged cluster state."""
    if value is None:
        return True
    if value == "":
        return True
    if value == []:
        return True
    if value == {}:
        return True
    return False


def values_equal(a: Any, b: Any) -> bool:
    """
    Whether two Helm value trees match for diff purposes (YAML omit vs explicit empty).
    """
    if a is b:
        return True
    if isinstance(a, dict) and b is None:
        return values_equal(a, {})
    if isinstance(b, dict) and a is None:
        return values_equal({}, b)
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a) | set(b)
        for key in keys:
            if not values_equal(a.get(key), b.get(key)):
                return False
        return True
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(values_equal(x, y) for x, y in zip(a, b, strict=True))
    if a == b:
        return True
    if _is_emptyish(a) and _is_emptyish(b):
        return True
    return False


def extract_values_from_release(release: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pull user and default value dicts from a decoded Helm release."""
    config = release.get("config")
    if config is None:
        trace_values(logger, "release has no 'config' key; using empty user values")
        user_values: dict[str, Any] = {}
    elif not isinstance(config, dict):
        raise TypeError(
            f"release.config must be a dict (got {type(config).__name__})"
        )
    else:
        user_values = config
        trace_values(logger, "release.config: %d top-level key(s)", len(user_values))

    chart = release.get("chart")
    if not isinstance(chart, dict):
        raise TypeError("release.chart must be a dict")

    chart_values = chart.get("values")
    if chart_values is None:
        trace_values(
            logger, "release.chart has no 'values' key; using empty defaults"
        )
        default_values: dict[str, Any] = {}
    elif not isinstance(chart_values, dict):
        raise TypeError(
            f"release.chart.values must be a dict (got {type(chart_values).__name__})"
        )
    else:
        default_values = chart_values
        trace_values(
            logger,
            "release.chart.values: %d top-level key(s)",
            len(default_values),
        )

    return user_values, default_values


def coalesce_values(
    base: dict[str, Any] | None,
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    """Deep-merge overrides onto base the way Helm coalesces values tables."""
    base_dict = copy.deepcopy(base or {})
    for key, override in (overrides or {}).items():
        base_value = base_dict.get(key)
        if isinstance(base_value, dict) and isinstance(override, dict):
            base_dict[key] = coalesce_values(base_value, override)
        else:
            base_dict[key] = copy.deepcopy(override)
    return base_dict


def cluster_values_from_release(release: dict[str, Any]) -> dict[str, Any]:
    """Effective values stored in the cluster release (chart.values + config)."""
    user_values, chart_values = extract_values_from_release(release)
    coalesced = coalesce_values(chart_values, user_values)
    trace_values(
        logger,
        "cluster values: coalesced chart.values + config (%d top-level key(s))",
        len(coalesced),
    )
    return coalesced


def diff_values(
    user: dict[str, Any] | None,
    defaults: dict[str, Any] | None,
) -> dict[str, Any]:
    if user is not None and not isinstance(user, dict):
        raise TypeError(f"user values must be a dict (got {type(user).__name__})")
    if defaults is not None and not isinstance(defaults, dict):
        raise TypeError(
            f"default values must be a dict (got {type(defaults).__name__})"
        )

    if user is None:
        trace_values(
            logger,
            "no user values (%d default key(s)); returning empty diff",
            len(defaults or {}),
        )
        return {}

    user_map = user
    defaults_map = defaults or {}
    if not user_map:
        return {}

    trace_values(
        logger,
        "diffing values: %d user key(s), %d default key(s)",
        len(user_map),
        len(defaults_map),
    )

    result: dict[str, Any] = {}
    for key, user_value in user_map.items():
        default_value = defaults_map.get(key)
        if isinstance(user_value, dict):
            if isinstance(default_value, dict) and values_equal(user_value, default_value):
                continue
            default_dict = default_value if isinstance(default_value, dict) else {}
            nested = diff_values(user_value, default_dict)
            if nested:
                result[key] = nested
                trace_values(logger, "nested diff at %r: %d key(s)", key, len(nested))
        elif not values_equal(user_value, default_value):
            result[key] = user_value
            trace_values(logger, "override at %r (differs from default)", key)
    trace_values(logger, "values diff result: %d top-level key(s)", len(result))
    return result


def resolve_values_object(
    cluster_values: dict[str, Any] | None,
    remote_defaults: dict[str, Any] | None,
) -> tuple[dict[str, Any], ValuesStrategy]:
    """Diff cluster values against remote chart defaults (helm show values)."""
    cluster = cluster_values or {}
    remote = remote_defaults or {}
    trace_values(
        logger,
        "diffing cluster values (%d keys) against remote defaults (%d keys)",
        len(cluster),
        len(remote),
    )
    diff = diff_values(cluster, remote)
    if diff:
        trace_values(
            logger,
            "valuesObject strategy=remote.diff (%d top-level key(s))",
            len(diff),
        )
        return diff, "remote.diff"
    trace_values(
        logger,
        "valuesObject strategy=empty (cluster matches remote chart defaults)",
    )
    return {}, "empty"


def _top_level_key_sets(
    left: dict[str, Any],
    right: dict[str, Any],
    diff: dict[str, Any],
) -> dict[str, list[str]]:
    left_keys = set(left)
    right_keys = set(right)
    return {
        "onlyInCluster": sorted(left_keys - right_keys),
        "onlyInRemoteDefaults": sorted(right_keys - left_keys),
        "inBoth": sorted(left_keys & right_keys),
        "inValuesObject": sorted(diff),
    }


def build_values_debug(
    release: dict[str, Any],
    user_values: dict[str, Any],
    chart_values: dict[str, Any],
    cluster_values: dict[str, Any],
    remote_defaults: dict[str, Any],
    values_object: dict[str, Any],
    *,
    strategy: ValuesStrategy,
) -> dict[str, Any]:
    """Build the values/diff section for manifest .debug output."""
    chart = release.get("chart") if isinstance(release.get("chart"), dict) else {}
    metadata = chart.get("metadata") if isinstance(chart.get("metadata"), dict) else {}

    return {
        "helmRelease": {
            "name": release.get("name"),
            "namespace": release.get("namespace"),
            "version": release.get("version"),
        },
        "chart": {
            "name": metadata.get("name"),
            "version": metadata.get("version"),
            "repoURL": metadata.get("repoURL"),
        },
        "valuesFromCluster": {
            "config": user_values,
            "chartValues": chart_values,
            "coalesced": cluster_values,
        },
        "remoteDefaults": remote_defaults,
        "configField": {
            "present": "config" in release,
            "isNone": release.get("config") is None,
            "topLevelKeyCount": len(user_values),
        },
        "diff": {
            "strategy": strategy,
            "valuesObject": copy.deepcopy(values_object),
            "topLevelKeys": _top_level_key_sets(
                cluster_values, remote_defaults, values_object
            ),
            "ignoreAnnotations": list(VALUES_DIFF_IGNORE_ANNOTATIONS),
        },
    }
