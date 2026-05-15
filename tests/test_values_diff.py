import pytest

from helmadm.values_diff import (
    build_values_debug,
    cluster_values_from_release,
    coalesce_values,
    diff_values,
    extract_values_from_release,
    resolve_values_object,
    values_equal,
)


def test_diff_returns_only_changed_keys():
    user = {
        "server": {"retention": "30d", "replicas": 1},
        "ingress": {"enabled": True},
    }
    defaults = {
        "server": {"retention": "15d", "replicas": 1},
        "ingress": {"enabled": False},
    }

    assert diff_values(user, defaults) == {
        "server": {"retention": "30d"},
        "ingress": {"enabled": True},
    }


def test_diff_empty_config():
    assert diff_values({}, {"foo": "bar"}) == {}
    assert diff_values(None, {"foo": "bar"}) == {}


def test_diff_list_inequality():
    user = {"tags": ["a", "b"]}
    defaults = {"tags": ["a"]}
    assert diff_values(user, defaults) == {"tags": ["a", "b"]}


def test_diff_list_equality_omitted():
    user = {"tags": ["a", "b"]}
    defaults = {"tags": ["a", "b"]}
    assert diff_values(user, defaults) == {}


def test_diff_omits_empty_dict_when_default_missing():
    assert diff_values({"foo": {}}, {}) == {}
    assert diff_values({"resources": {"limits": {}}}, {}) == {}


def test_diff_omits_equal_nested_defaults_and_empty_annotations():
    cluster = {
        "server": {"retention": "30d", "replicas": 1, "resources": {}},
        "ingress": {"enabled": True},
        "annotations": {},
    }
    remote = {
        "server": {"retention": "15d", "replicas": 1},
        "ingress": {"enabled": False},
    }
    assert diff_values(cluster, remote) == {
        "server": {"retention": "30d"},
        "ingress": {"enabled": True},
    }


def test_diff_omits_null_when_default_missing():
    assert diff_values({"x": None}, {}) == {}


def test_diff_omits_empty_chart_noise_when_remote_omits_keys():
    """Cluster coalesced state often lists [], ''; remote values.yaml may omit those keys."""
    cluster = {
        "pod": {
            "enableServiceLinks": True,
            "envFromConfigMaps": [],
            "envFromSecret": "",
            "extraVolumes": [],
            "image": {"tag": "2.0", "pullPolicy": ""},
        }
    }
    remote = {
        "pod": {
            "enableServiceLinks": True,
            "image": {"tag": "1.0"},
        }
    }
    assert diff_values(cluster, remote) == {"pod": {"image": {"tag": "2.0"}}}


def test_values_equal_treats_omit_and_empty_collections_as_same():
    assert values_equal({"a": []}, {"a": None})
    assert values_equal({"a": ""}, {})
    assert values_equal({"x": {"y": []}}, {"x": {}})


def test_extract_values_from_release(sample_release):
    user, defaults = extract_values_from_release(sample_release)
    assert user == sample_release["config"]
    assert defaults == sample_release["chart"]["values"]


def test_extract_values_from_release_missing_config(sample_release):
    release = {**sample_release, "config": None}
    user, defaults = extract_values_from_release(release)
    assert user == {}
    assert defaults == sample_release["chart"]["values"]


def test_diff_values_rejects_non_dict_user():
    with pytest.raises(TypeError, match="user values"):
        diff_values("bad", {})


def test_coalesce_values_merges_nested_tables():
    base = {"server": {"retention": "15d", "replicas": 1}}
    overrides = {"server": {"retention": "30d"}}
    assert coalesce_values(base, overrides) == {
        "server": {"retention": "30d", "replicas": 1},
    }


def test_resolve_values_object_empty_when_cluster_matches_remote():
    cluster = {"replicas": 1, "image": {"tag": "1.0.0"}}
    remote = {"replicas": 1, "image": {"tag": "1.0.0"}}
    values_object, strategy = resolve_values_object(cluster, remote)

    assert strategy == "empty"
    assert values_object == {}


def test_resolve_values_object_remote_diff_for_overrides():
    cluster = {"replicas": 3, "image": {"tag": "2.0.0"}}
    remote = {"replicas": 1, "image": {"tag": "1.0.0"}}
    values_object, strategy = resolve_values_object(cluster, remote)

    assert strategy == "remote.diff"
    assert values_object == {"replicas": 3, "image": {"tag": "2.0.0"}}


def test_cluster_values_from_release_coalesces_config(sample_release):
    cluster = cluster_values_from_release(sample_release)
    assert cluster["server"]["retention"] == "30d"
    assert cluster["ingress"]["enabled"] is True


def test_build_values_debug_includes_cluster_values(sample_release):
    cluster = cluster_values_from_release(sample_release)
    remote = sample_release["chart"]["values"]
    values_object, strategy = resolve_values_object(cluster, remote)
    debug = build_values_debug(
        sample_release,
        sample_release["config"],
        sample_release["chart"]["values"],
        cluster,
        remote,
        values_object,
        strategy=strategy,
    )

    assert debug["valuesFromCluster"]["config"] == sample_release["config"]
    assert debug["valuesFromCluster"]["coalesced"] == cluster
    assert debug["remoteDefaults"] == remote
    assert debug["diff"]["valuesObject"] == values_object
    assert "server" in debug["diff"]["topLevelKeys"]["inValuesObject"]
