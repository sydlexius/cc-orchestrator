#!/usr/bin/env python3
"""Versioned schema registry + validator for orchestrate structured artifacts (#225).

One versioned source-of-truth schema per artifact type that ONE orchestrate agent
writes and ANOTHER reads, so producers and consumers never drift silently:

  - gate-receipt/v1        the deterministic gate receipt (#229 / design #4),
                           written by prep-pr as a byproduct of the real gate run.
  - finding-fix-list/v1    the PR-BLIND fix-list slice of the finding channel
                           (#230 / design #6): {round, findings[...]}. NO thread
                           ids, NO bot-reply prose -- the implementer reads/writes it.
  - finding-reply-slice/v1 the LEAD-ONLY reply/thread slice: a mapping
                           finding_id -> {thread_id, disposition, reply_text}. The
                           implementer never sees it (preserves PR-blindness).

This module is the AUTHORITATIVE machine schema; `skills/orchestrate/templates/schemas.md`
documents it for humans and must stay in sync. Producers/consumers either import
`validate()` (Python, e.g. gate-runner) or call the `--validate` CLI (shell).

Stdlib only -- a hand-rolled lightweight validator (the repo carries no jsonschema
dependency). `validate(schema_name, obj)` returns a list of human-readable error
strings; an empty list means the artifact conforms. Extra/unknown keys are ALLOWED
(forward-compat: report-by-exception, design #3, extends the receipt block).
"""
import json
import re
import sys

# Anchors are implicit under re.fullmatch below; NOT `^...$` -- with re.match a `$`
# matches before a trailing newline, so a 41-char "….\n" SHA would validate (#225 hostile-review LOW).
# Case-insensitive hex: git emits lowercase, but a valid SHA is a valid SHA either
# case; validating FORMAT (not canonicalization) avoids false-negative rejects (Codoki #250).
_SHA40 = r"[0-9a-fA-F]{40}"


def _f(type_, required=True, enum=None, const=None, pattern=None, items=None,
       values=None, nullable=False):
    """Build a field spec. `items` = element field-spec dict for a list; `values`
    = value field-spec dict for a dict/mapping; `nullable` allows an explicit None."""
    return {"type": type_, "required": required, "enum": enum, "const": const,
            "pattern": pattern, "items": items, "values": values, "nullable": nullable}


# --- The registry: schema id -> ordered field-spec map -----------------------
_FINDING = {
    "id": _f(str),
    "severity": _f(str, enum=["critical", "high", "medium", "low", "nit"]),
    "detail": _f(str),
    "status": _f(str, enum=["open", "addressed"]),
    "fix_sha": _f(str, required=False, nullable=True),
}
_STEP = {
    "name": _f(str),
    "result": _f(str, enum=["pass", "fail", "skip"]),
}
_REPLY = {
    "thread_id": _f(str),
    "disposition": _f(str, enum=["merge-safe", "rebut", "fix"]),
    "reply_text": _f(str),
}

SCHEMAS = {
    "gate-receipt/v1": {
        "schema": _f(str, const="gate-receipt/v1"),
        "commit_sha": _f(str, pattern=_SHA40),
        "tree_sha": _f(str, pattern=_SHA40),
        "worktree": _f(str),
        "result": _f(str, enum=["pass", "fail"]),
        "steps": _f(list, items=_STEP),
        "producer": _f(str),
    },
    "finding-fix-list/v1": {
        "schema": _f(str, const="finding-fix-list/v1"),
        "round": _f(int),
        "findings": _f(list, items=_FINDING),
    },
    "finding-reply-slice/v1": {
        "schema": _f(str, const="finding-reply-slice/v1"),
        "replies": _f(dict, values=_REPLY),
    },
}


def _type_ok(spec, val):
    t = spec["type"]
    if t is int:
        # bool is a subclass of int -- reject it where a real int is expected.
        return isinstance(val, int) and not isinstance(val, bool)
    return isinstance(val, t)


def _check_value(spec, val, path, errors):
    if val is None:
        if not spec["nullable"]:
            errors.append(f"{path}: null not allowed")
        return
    if not _type_ok(spec, val):
        errors.append(f"{path}: expected {spec['type'].__name__}, got {type(val).__name__}")
        return
    if spec["const"] is not None and val != spec["const"]:
        errors.append(f"{path}: must equal {spec['const']!r}, got {val!r}")
    if spec["enum"] is not None and val not in spec["enum"]:
        errors.append(f"{path}: {val!r} not in {spec['enum']}")
    if spec["pattern"] is not None and not re.fullmatch(spec["pattern"], val):
        errors.append(f"{path}: {val!r} does not match {spec['pattern']}")
    if spec["items"] is not None:  # list of objects
        for i, elem in enumerate(val):
            _check_object(spec["items"], elem, f"{path}[{i}]", errors)
    if spec["values"] is not None:  # dict/mapping of objects
        for k, v in val.items():
            _check_object(spec["values"], v, f"{path}.{k}", errors)


def _check_object(fields, obj, path, errors):
    if not isinstance(obj, dict):
        errors.append(f"{path}: expected object, got {type(obj).__name__}")
        return
    for name, spec in fields.items():
        if name not in obj:
            if spec["required"]:
                errors.append(f"{path}.{name}: required field missing")
            continue
        _check_value(spec, obj[name], f"{path}.{name}", errors)
    # Extra keys are intentionally allowed (forward-compat).


def validate(schema_name, obj):
    """Validate `obj` against the named schema. Returns a list of error strings
    (empty = conforms). An unknown schema name is itself an error."""
    fields = SCHEMAS.get(schema_name)
    if fields is None:
        return [f"unknown schema {schema_name!r}; known: {sorted(SCHEMAS)}"]
    errors = []
    _check_object(fields, obj, schema_name, errors)
    return errors


def _main(argv):
    if len(argv) != 3 or argv[0] != "--validate":
        sys.stderr.write("usage: orchestrate_schemas.py --validate <schema-name> <file.json>\n")
        return 2
    schema_name, path = argv[1], argv[2]
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"cannot read {path}: {e}\n")
        return 2
    errors = validate(schema_name, obj)
    if errors:
        for e in errors:
            sys.stderr.write(e + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
