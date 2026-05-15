import pytest

from helmadm.helm_release import (
    HelmReleaseDecodeError,
    decode_release_data,
    validate_decoded_release,
)


def test_decode_release_data(encoded_release_data, sample_release):
    decoded = decode_release_data(encoded_release_data)
    assert decoded == sample_release


def test_decode_release_data_empty_payload():
    with pytest.raises(HelmReleaseDecodeError, match="empty"):
        decode_release_data("")


def test_validate_decoded_release_rejects_non_object():
    with pytest.raises(HelmReleaseDecodeError, match="JSON object"):
        validate_decoded_release([])


def test_validate_decoded_release_requires_chart_metadata(sample_release):
    bad = {"name": "app", "chart": {"metadata": {"name": "x"}}}
    with pytest.raises(HelmReleaseDecodeError, match="version"):
        validate_decoded_release(bad)


def test_validate_decoded_release_rejects_bad_config_type(sample_release):
    bad = {**sample_release, "config": "not-a-dict"}
    with pytest.raises(HelmReleaseDecodeError, match="config"):
        validate_decoded_release(bad)
