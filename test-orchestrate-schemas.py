#!/usr/bin/env python3
"""Proof harness for orchestrate_schemas.py (#225).

The schema registry is the single versioned source of truth for the structured
artifacts one orchestrate agent writes and another reads -- the gate receipt
(#229/#4) and the finding-channel fix-list + reply/thread slices (#230/#6). This
harness proves the validator ACCEPTS well-formed artifacts and REJECTS every
drift class (missing required field, wrong type, bad enum/const, malformed SHA,
malformed nested list item / mapping value), and that the CLI exit codes match.

Stdlib only; imports the module directly and also drives its `--validate` CLI.

Run: python3 test-orchestrate-schemas.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(HERE, "scripts", "orchestrate_schemas.py")

spec = importlib.util.spec_from_file_location("orchestrate_schemas", MODULE_PATH)
if spec is None or spec.loader is None:
    sys.exit(f"cannot load orchestrate_schemas from {MODULE_PATH}")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


FULLSHA = "a" * 40


def gate_receipt(**over):
    d = {
        "schema": "gate-receipt/v1",
        "commit_sha": FULLSHA,
        "tree_sha": "b" * 40,
        "worktree": "/abs/path/wt",
        "result": "pass",
        "steps": [{"name": "shellcheck", "result": "pass"}],
        "producer": "prep-pr",
    }
    d.update(over)
    return d


def fix_list(**over):
    d = {
        "schema": "finding-fix-list/v1",
        "round": 1,
        "findings": [{"id": "F1", "severity": "high", "detail": "boom",
                      "status": "open", "fix_sha": None}],
    }
    d.update(over)
    return d


def reply_slice(**over):
    d = {
        "schema": "finding-reply-slice/v1",
        "replies": {"F1": {"thread_id": "PRRT_x", "disposition": "fix",
                           "reply_text": "fixed in abc123"}},
    }
    d.update(over)
    return d


def cli(schema, obj):
    """Drive the --validate CLI; return (exit_code, stdout+stderr)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(obj, f); path = f.name
    try:
        p = subprocess.run([sys.executable, MODULE_PATH, "--validate", schema, path],
                           capture_output=True, text=True, timeout=20)
        return p.returncode, p.stdout + p.stderr
    finally:
        os.unlink(path)


def main():
    print("== #225 schema registry: valid artifacts pass ==")
    check("registry exposes all three schema versions",
          {"gate-receipt/v1", "finding-fix-list/v1", "finding-reply-slice/v1"} <= set(S.SCHEMAS))
    check("valid gate-receipt -> no errors", S.validate("gate-receipt/v1", gate_receipt()) == [])
    check("valid fix-list -> no errors", S.validate("finding-fix-list/v1", fix_list()) == [])
    check("valid reply-slice -> no errors", S.validate("finding-reply-slice/v1", reply_slice()) == [])

    print("== drift classes are REJECTED ==")
    check("unknown schema name -> error", S.validate("nope/v9", {}) != [])
    check("non-dict artifact -> error", S.validate("gate-receipt/v1", ["not", "an", "object"]) != [])

    # Missing required field.
    r = gate_receipt(); del r["commit_sha"]
    errs = S.validate("gate-receipt/v1", r)
    check("missing required field -> error naming it", any("commit_sha" in e for e in errs))

    # Wrong type.
    check("wrong scalar type -> error", S.validate("gate-receipt/v1", gate_receipt(worktree=123)) != [])
    # bool is not int for round (guard against bool-is-int).
    check("bool where int expected -> error", S.validate("finding-fix-list/v1", fix_list(round=True)) != [])

    # const mismatch (schema field must equal the schema id).
    check("schema-const mismatch -> error",
          S.validate("gate-receipt/v1", gate_receipt(schema="gate-receipt/v2")) != [])

    # enum violation.
    check("bad result enum -> error", S.validate("gate-receipt/v1", gate_receipt(result="maybe")) != [])
    check("bad finding status enum -> error",
          S.validate("finding-fix-list/v1", fix_list(findings=[{"id": "F1", "severity": "high",
                     "detail": "x", "status": "kinda", "fix_sha": None}])) != [])

    # pattern (SHA must be 40 hex).
    check("short commit_sha -> error", S.validate("gate-receipt/v1", gate_receipt(commit_sha="abc123")) != [])
    # A trailing newline must NOT sneak past the SHA pattern (re.match+$ would allow it).
    check("40-hex + trailing newline -> error (fullmatch, not match)",
          S.validate("gate-receipt/v1", gate_receipt(commit_sha="a" * 40 + "\n")) != [])

    # null in a non-nullable field, and a float where int is expected.
    check("null in non-nullable field -> error", S.validate("gate-receipt/v1", gate_receipt(worktree=None)) != [])
    check("float where int expected -> error", S.validate("finding-fix-list/v1", fix_list(round=1.5)) != [])
    # A nullable+optional field explicitly set to null is fine.
    check("nullable fix_sha=null -> ok",
          S.validate("finding-fix-list/v1", fix_list(findings=[{"id": "F1", "severity": "low",
                     "detail": "x", "status": "open", "fix_sha": None}])) == [])

    # nested list item malformed (a finding missing 'id').
    check("list item missing required subfield -> error",
          S.validate("finding-fix-list/v1", fix_list(findings=[{"severity": "high", "detail": "x",
                     "status": "open", "fix_sha": None}])) != [])
    check("steps must be a list -> error", S.validate("gate-receipt/v1", gate_receipt(steps="nope")) != [])

    # mapping value malformed (a reply missing thread_id).
    check("mapping value missing required subfield -> error",
          S.validate("finding-reply-slice/v1", reply_slice(replies={"F1": {"disposition": "fix",
                     "reply_text": "x"}})) != [])
    check("bad disposition enum -> error",
          S.validate("finding-reply-slice/v1", reply_slice(replies={"F1": {"thread_id": "t",
                     "disposition": "ignore", "reply_text": "x"}})) != [])

    # Forward-compat: an EXTRA key (report-by-exception extends the receipt) is allowed.
    check("extra/unknown field is allowed (forward-compat)",
          S.validate("gate-receipt/v1", gate_receipt(blocked=False, note="extended")) == [])

    print("== CLI (--validate <schema> <file.json>) exit codes ==")
    rc, out = cli("gate-receipt/v1", gate_receipt())
    check("CLI valid -> exit 0", rc == 0)
    rc, out = cli("gate-receipt/v1", gate_receipt(result="maybe"))
    check("CLI invalid -> exit 1", rc == 1)
    check("CLI invalid -> prints the error", "result" in out)
    rc, out = cli("bogus/v1", {})
    check("CLI unknown schema -> exit 1 (a validation failure, not usage)", rc == 1)
    # exit 2 = usage / unreadable file (a distinct documented code shell callers branch on).
    p = subprocess.run([sys.executable, MODULE_PATH, "--validate", "gate-receipt/v1",
                        os.path.join(HERE, "does-not-exist-xyz.json")], capture_output=True, text=True)
    check("CLI missing file -> exit 2", p.returncode == 2)
    p = subprocess.run([sys.executable, MODULE_PATH, "--validate", "gate-receipt/v1", HERE],
                       capture_output=True, text=True)
    check("CLI directory path -> exit 2", p.returncode == 2)
    p = subprocess.run([sys.executable, MODULE_PATH, "--bogus-flag"], capture_output=True, text=True)
    check("CLI bad args -> exit 2", p.returncode == 2)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
