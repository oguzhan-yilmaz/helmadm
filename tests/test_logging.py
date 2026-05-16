import logging
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from helmadm.cli import app
from helmadm.env import ENV_TRACE_VALUES
from helmadm.logging_config import (
    AlignedFormatter,
    PACKAGE_LOGGER,
    get_logger,
    setup_logging,
    trace_values,
)
from helmadm.values_diff import diff_values
from tests.conftest import make_load_release_and_values_result

runner = CliRunner()


def _allow_caplog_to_see_package_logs(caplog) -> None:
    """``setup_logging`` sets propagate=False; pytest caplog only sees propagated records."""
    caplog.set_level(logging.DEBUG, logger=PACKAGE_LOGGER)
    logging.getLogger(PACKAGE_LOGGER).propagate = True


@pytest.fixture(autouse=True)
def reset_package_logger():
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    yield
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)

_SAMPLE_RELEASE = {
    "name": "app",
    "config": {},
    "chart": {
        "metadata": {
            "name": "app",
            "version": "1.0.0",
            "repoURL": "https://charts.example.com",
        },
        "values": {},
    },
}


def test_setup_logging_verbose_enables_debug():
    setup_logging(verbose=True)
    logger = logging.getLogger(PACKAGE_LOGGER)
    assert logger.level == logging.DEBUG
    assert logger.handlers
    assert isinstance(logger.handlers[0].formatter, AlignedFormatter)


def test_setup_logging_default_is_warning():
    setup_logging(verbose=False)
    logger = logging.getLogger(PACKAGE_LOGGER)
    assert logger.level == logging.WARNING


def test_verbose_flag_enables_debug_logging():
    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch(
            "helmadm.cli._load_release_and_values",
            return_value=make_load_release_and_values_result(_SAMPLE_RELEASE),
        ),
        patch("helmadm.cli.render_application", return_value=""),
    ):
        result = runner.invoke(
            app, ["--verbose", "argocd-yaml", "-n", "ns", "app"]
        )

    assert result.exit_code == 0
    logger = logging.getLogger(PACKAGE_LOGGER)
    assert logger.level == logging.DEBUG
    assert logger.handlers


def test_without_verbose_uses_warning_level():
    with (
        patch("helmadm.cli.load_kubernetes_client"),
        patch(
            "helmadm.cli._load_release_and_values",
            return_value=make_load_release_and_values_result(_SAMPLE_RELEASE),
        ),
        patch("helmadm.cli.render_application", return_value=""),
    ):
        result = runner.invoke(app, ["argocd-yaml", "-n", "ns", "app"])

    assert result.exit_code == 0
    logger = logging.getLogger(PACKAGE_LOGGER)
    assert logger.level == logging.WARNING


def test_root_help_lists_verbose():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--verbose" in result.stdout or "-v" in result.stdout


def test_values_diff_trace_suppressed_without_env(caplog):
    setup_logging(verbose=True)
    _allow_caplog_to_see_package_logs(caplog)
    diff_values({"a": 1}, {"a": 2})
    assert not any("diffing values" in r.message for r in caplog.records)


def test_values_diff_trace_emitted_when_env_set(monkeypatch, caplog):
    monkeypatch.setenv(ENV_TRACE_VALUES, "1")
    setup_logging(verbose=True)
    _allow_caplog_to_see_package_logs(caplog)
    diff_values({"a": 1}, {"a": 2})
    assert any("diffing values" in r.message for r in caplog.records)


def test_trace_values_helper_respects_env(monkeypatch, caplog):
    setup_logging(verbose=True)
    _allow_caplog_to_see_package_logs(caplog)
    trace_values(get_logger("test"), "trace probe")
    assert not any("trace probe" in r.message for r in caplog.records)

    monkeypatch.setenv(ENV_TRACE_VALUES, "yes")
    trace_values(get_logger("test"), "trace probe")
    assert any("trace probe" in r.message for r in caplog.records)
