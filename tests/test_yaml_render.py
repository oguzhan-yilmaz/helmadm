import yaml

from helmadm.yaml_render import (
    dump_yaml,
    normalize_multiline_value,
    should_use_block_scalar,
)


def test_should_use_block_scalar_real_and_escaped_newlines() -> None:
    assert should_use_block_scalar("line1\nline2")
    assert should_use_block_scalar("[OUTPUT]\\n    name loki\\n    match *\\n")
    assert not should_use_block_scalar("short")
    assert not should_use_block_scalar("no-newlines-here")


def test_normalize_multiline_value_expands_escaped_newlines() -> None:
    raw = "[OUTPUT]\\n    name loki\\n    match *\\n"
    normalized = normalize_multiline_value(raw)
    assert normalized.startswith("[OUTPUT]\n")
    assert "    name loki\n" in normalized


def test_dump_yaml_unicode_multiline_uses_block_scalar() -> None:
    """Regression: em dash and other Unicode must not force quoted scalars."""
    text = (
        "[OUTPUT]\n    name loki\n    match *\n"
        "    # See logging-stack/loki.README.md \u2014 http://loki-gateway.loki.svc\n"
    )
    rendered = dump_yaml({"config": {"outputs": text}})
    assert 'outputs: "[OUTPUT]' not in rendered
    assert "outputs: |\n" in rendered
    assert "\u2014" in rendered


def test_dump_yaml_escaped_multiline_uses_block_scalar() -> None:
    doc = {
        "config": {
            "inputs": "[INPUT]\n    Name tail\n",
            "outputs": "[OUTPUT]\\n    name loki\\n    match *\\n",
            "extraFiles": {
                "labelmap.json": '{\\n  "kubernetes": {"namespace_name": "namespace"}\\n}\\n',
            },
        }
    }
    rendered = dump_yaml(doc)

    assert 'outputs: "[OUTPUT]\\n' not in rendered
    assert "outputs: |\n" in rendered
    assert "    name loki\n" in rendered
    assert "inputs: |\n" in rendered
    assert "labelmap.json: |\n" in rendered

    roundtrip = yaml.safe_load(rendered)
    assert roundtrip["config"]["outputs"].startswith("[OUTPUT]\n")
    assert "name loki" in roundtrip["config"]["outputs"]
