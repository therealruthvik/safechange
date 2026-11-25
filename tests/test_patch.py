import copy
import random

import pytest

from safechange import patch as jp
from safechange.backends.fake import sample_deployment


def test_apply_basic_ops():
    doc = {"a": {"b": [1, 2, 3]}, "x/y": 1, "t~": 2}
    out = jp.apply_patch(doc, [
        {"op": "replace", "path": "/a/b/0", "value": 9},
        {"op": "add", "path": "/a/b/-", "value": 4},
        {"op": "add", "path": "/a/b/1", "value": 7},
        {"op": "remove", "path": "/x~1y"},
        {"op": "replace", "path": "/t~0", "value": 3},
        {"op": "test", "path": "/a/b/0", "value": 9},
    ])
    assert out == {"a": {"b": [9, 7, 2, 3, 4]}, "t~": 3}
    assert doc == {"a": {"b": [1, 2, 3]}, "x/y": 1, "t~": 2}, "input must not be mutated"


def test_apply_is_atomic_on_failure():
    doc = {"a": 1}
    with pytest.raises(jp.PatchError):
        jp.apply_patch(doc, [{"op": "replace", "path": "/a", "value": 2},
                             {"op": "remove", "path": "/missing"}])
    assert doc == {"a": 1}


def test_test_op_failure_is_distinct():
    with pytest.raises(jp.PatchTestFailed):
        jp.apply_patch({"metadata": {"resourceVersion": "5"}},
                       [{"op": "test", "path": "/metadata/resourceVersion", "value": "4"}])


@pytest.mark.parametrize("ops", [
    "not-a-list",
    [{"op": "explode", "path": "/a"}],
    [{"op": "add", "path": "a", "value": 1}],
    [{"op": "add", "path": "/a"}],
    [{"op": "move", "path": "/a"}],
    [{"op": "replace", "path": "", "value": {}}],
    [{"op": "replace", "path": "/l/01", "value": 1}],
])
def test_malformed_patches_rejected(ops):
    with pytest.raises(jp.PatchError):
        jp.apply_patch({"a": 1, "l": [0, 1]}, ops)


def test_validate_restricts_op_kinds():
    with pytest.raises(jp.PatchError):
        jp.validate_patch([{"op": "copy", "from": "/a", "path": "/b"}], jp.PROPOSABLE_OPS)


def _mutate(rng, obj):
    o = copy.deepcopy(obj)
    c = o["spec"]["template"]["spec"]["containers"][0]
    choice = rng.randrange(6)
    if choice == 0:
        o["spec"]["replicas"] = rng.randrange(1, 10)
    elif choice == 1:
        c["image"] = f"ghcr.io/org/web:{rng.randrange(100)}"
    elif choice == 2:
        c.setdefault("env", []).append({"name": f"K{rng.randrange(9)}", "value": "v"})
    elif choice == 3:
        o["metadata"]["labels"][f"l{rng.randrange(5)}"] = "x"
    elif choice == 4:
        c["resources"].pop("requests", None)
    else:
        o["spec"]["template"]["spec"]["containers"].append({"name": "side", "image": "busybox:1"})
    return o


def test_diff_apply_round_trip_randomized():
    rng = random.Random(42)
    base = jp.normalize(sample_deployment())
    for _ in range(300):
        a = _mutate(rng, base)
        b = _mutate(rng, _mutate(rng, base))
        fwd, back = jp.diff(a, b), jp.diff(b, a)
        assert jp.apply_patch(a, fwd) == b
        assert jp.apply_patch(b, back) == a


def test_diff_scopes_ops_to_changed_paths():
    a = jp.normalize(sample_deployment())
    b = copy.deepcopy(a)
    b["spec"]["replicas"] = 4
    assert jp.diff(a, b) == [{"op": "replace", "path": "/spec/replicas", "value": 4}]
    assert jp.diff(a, a) == []


def test_normalize_strips_volatile_fields_only():
    obj = sample_deployment()
    obj["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "3", "team": "x"}
    obj["metadata"]["managedFields"] = [{}]
    n = jp.normalize(obj)
    assert "status" not in n and "resourceVersion" not in n["metadata"]
    assert "managedFields" not in n["metadata"] and "generation" not in n["metadata"]
    assert n["metadata"]["annotations"] == {"team": "x"}
    assert n["spec"] == obj["spec"]


def test_hash_is_key_order_independent():
    assert jp.sha256_json({"a": 1, "b": [1, 2]}) == jp.sha256_json({"b": [1, 2], "a": 1})
    assert jp.sha256_json({"a": 1}) != jp.sha256_json({"a": 2})
