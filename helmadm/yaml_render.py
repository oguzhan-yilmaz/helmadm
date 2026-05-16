"""YAML rendering helpers (block scalars for multiline strings)."""

from __future__ import annotations

from typing import Any

import yaml


def looks_like_escaped_multiline(value: str) -> bool:
    """True when the string uses backslash escapes instead of real line breaks."""
    return "\\n" in value or "\\r\\n" in value


def should_use_block_scalar(value: str) -> bool:
    """Whether a string should be written as a YAML literal block scalar (|)."""
    if "\n" in value or "\r" in value:
        return True
    return looks_like_escaped_multiline(value)


def normalize_multiline_value(value: str) -> str:
    """
    Normalize line endings and expand common Helm-style \\n / \\t escapes.

    Leaves strings that already contain real newlines unchanged (aside from \\r\\n).
    """
    if "\n" in value or "\r" in value:
        return value.replace("\r\n", "\n").replace("\r", "\n")
    if looks_like_escaped_multiline(value):
        return (
            value.replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
        )
    return value


class NoAliasDumper(yaml.SafeDumper):
    """Avoid YAML anchors when the same dict is referenced more than once."""

    def ignore_aliases(self, data: object) -> bool:
        return True


def _represent_str(dumper: yaml.Dumper, data: str) -> yaml.nodes.ScalarNode:
    if should_use_block_scalar(data):
        data = normalize_multiline_value(data)
        style = "|"
    else:
        style = None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


NoAliasDumper.add_representer(str, _represent_str)


def dump_yaml(data: Any) -> str:
    """Dump a structure to YAML with readable multiline string formatting."""
    return yaml.dump(
        data,
        Dumper=NoAliasDumper,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
        # Required so PyYAML keeps literal (|) style for multiline strings that
        # contain Unicode (e.g. em dash in fluent-bit config comments). Without
        # this, analyze_scalar marks them as "special" and forces quoted style.
        allow_unicode=True,
    )
