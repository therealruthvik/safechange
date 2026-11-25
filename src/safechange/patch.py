"""RFC 6902 JSON Patch: apply, diff, normalization and canonical hashing.

Only what safechange needs, implemented without third-party dependencies so the
semantics are fully under test. All functions are pure: inputs are never mutated.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

JsonPatch = list[dict[str, Any]]

PROPOSABLE_OPS = frozenset({"add", "remove", "replace"})
ALL_OPS = frozenset({"add", "remove", "replace", "test", "move", "copy"})


class PatchError(Exception):
    """Raised when a patch is malformed or cannot be applied."""


class PatchTestFailed(PatchError):
    """A `test` operation did not match (optimistic-concurrency conflict)."""


# --------------------------------------------------------------------------- pointers
def escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def parse_pointer(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise PatchError(f"invalid JSON pointer {path!r}: must start with '/'")
    return [unescape(t) for t in path[1:].split("/")]


def join_pointer(tokens: list[str]) -> str:
    return "".join("/" + escape(t) for t in tokens)


def _list_index(container: list[Any], token: str, *, allow_end: bool) -> int:
    if token == "-" and allow_end:
        return len(container)
    if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
        raise PatchError(f"invalid array index {token!r}")
    idx = int(token)
    limit = len(container) + (1 if allow_end else 0)
    if idx >= limit:
        raise PatchError(f"array index {idx} out of range (len {len(container)})")
    return idx


def resolve(doc: Any, path: str) -> Any:
    """Return the value at `path`; raise PatchError if it does not exist."""
    cur = doc
    for tok in parse_pointer(path):
        if isinstance(cur, dict):
            if tok not in cur:
                raise PatchError(f"path {path!r} does not exist")
            cur = cur[tok]
        elif isinstance(cur, list):
            cur = cur[_list_index(cur, tok, allow_end=False)]
        else:
            raise PatchError(f"path {path!r} traverses a scalar")
    return cur


def exists(doc: Any, path: str) -> bool:
    try:
        resolve(doc, path)
        return True
    except PatchError:
        return False


def _parent(doc: Any, tokens: list[str], path: str) -> Any:
    if not tokens:
        raise PatchError("operations on the document root are not allowed")
    return resolve(doc, join_pointer(tokens[:-1]))


# --------------------------------------------------------------------------- apply
def validate_patch(ops: Any, allowed_ops: frozenset[str] = ALL_OPS) -> JsonPatch:
    """Structural validation. Returns the ops list or raises PatchError."""
    if not isinstance(ops, list):
        raise PatchError("patch must be a JSON array")
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise PatchError(f"op #{i} is not an object")
        kind = op.get("op")
        if kind not in ALL_OPS:
            raise PatchError(f"op #{i}: unknown op {kind!r}")
        if kind not in allowed_ops:
            raise PatchError(f"op #{i}: op {kind!r} is not permitted here")
        if not isinstance(op.get("path"), str):
            raise PatchError(f"op #{i}: missing string 'path'")
        parse_pointer(op["path"])
        if kind in {"add", "replace", "test"} and "value" not in op:
            raise PatchError(f"op #{i}: {kind!r} requires 'value'")
        if kind in {"move", "copy"}:
            if not isinstance(op.get("from"), str):
                raise PatchError(f"op #{i}: {kind!r} requires 'from'")
            parse_pointer(op["from"])
    return ops


def _apply_one(doc: Any, op: dict[str, Any]) -> Any:
    kind, path = op["op"], op["path"]
    tokens = parse_pointer(path)
    if kind == "test":
        if not exists(doc, path) or resolve(doc, path) != op["value"]:
            raise PatchTestFailed(f"test failed at {path!r}")
        return doc
    if kind in {"move", "copy"}:
        value = copy.deepcopy(resolve(doc, op["from"]))
        if kind == "move":
            doc = _apply_one(doc, {"op": "remove", "path": op["from"]})
        return _apply_one(doc, {"op": "add", "path": path, "value": value})

    parent = _parent(doc, tokens, path)
    last = tokens[-1]
    value = copy.deepcopy(op.get("value"))
    if isinstance(parent, dict):
        if kind == "add":
            parent[last] = value
        elif last not in parent:
            raise PatchError(f"{kind}: path {path!r} does not exist")
        elif kind == "remove":
            del parent[last]
        else:
            parent[last] = value
    elif isinstance(parent, list):
        if kind == "add":
            parent.insert(_list_index(parent, last, allow_end=True), value)
        else:
            idx = _list_index(parent, last, allow_end=False)
            if kind == "remove":
                parent.pop(idx)
            else:
                parent[idx] = value
    else:
        raise PatchError(f"{kind}: parent of {path!r} is a scalar")
    return doc


def apply_patch(doc: Any, ops: JsonPatch) -> Any:
    """Apply atomically: returns a new document, or raises without side effects."""
    validate_patch(ops)
    out = copy.deepcopy(doc)
    for op in ops:
        out = _apply_one(out, op)
    return out


# --------------------------------------------------------------------------- diff
def diff(src: Any, dst: Any, _prefix: str = "") -> JsonPatch:
    """Produce a patch P such that apply_patch(src, P) == dst.

    Dicts are diffed key-by-key; equal-length lists element-wise; anything else is
    replaced whole. This favours exactness and small, path-scoped ops over minimality.
    """
    if src == dst and type(src) is type(dst):
        return []
    if isinstance(src, dict) and isinstance(dst, dict):
        ops: JsonPatch = []
        for key in sorted(src.keys() - dst.keys()):
            ops.append({"op": "remove", "path": f"{_prefix}/{escape(key)}"})
        for key in sorted(dst.keys()):
            child = f"{_prefix}/{escape(key)}"
            if key not in src:
                ops.append({"op": "add", "path": child, "value": copy.deepcopy(dst[key])})
            else:
                ops.extend(diff(src[key], dst[key], child))
        return ops
    if isinstance(src, list) and isinstance(dst, list) and len(src) == len(dst):
        ops = []
        for i, (a, b) in enumerate(zip(src, dst)):
            ops.extend(diff(a, b, f"{_prefix}/{i}"))
        return ops
    if _prefix == "":
        raise PatchError("refusing to produce a whole-document replace")
    return [{"op": "replace", "path": _prefix, "value": copy.deepcopy(dst)}]


# --------------------------------------------------------------------------- normalize / hash
VOLATILE_METADATA = ("resourceVersion", "uid", "generation", "creationTimestamp",
                     "managedFields", "selfLink")
VOLATILE_ANNOTATIONS = ("deployment.kubernetes.io/revision",
                        "kubectl.kubernetes.io/last-applied-configuration")


def normalize(obj: dict[str, Any]) -> dict[str, Any]:
    """Strip server-managed and status fields so two reads of an unchanged object
    compare equal. Everything the agent may change survives normalization."""
    out = copy.deepcopy(obj)
    out.pop("status", None)
    meta = out.get("metadata", {})
    for key in VOLATILE_METADATA:
        meta.pop(key, None)
    ann = meta.get("annotations")
    if isinstance(ann, dict):
        for key in VOLATILE_ANNOTATIONS:
            ann.pop(key, None)
        if not ann:
            meta.pop("annotations", None)
    return out


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
