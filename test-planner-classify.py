#!/usr/bin/env python3
"""Behavioral harness for planner_classify (issue #11). Hand-rolled, stdlib-only, no pytest.
Imports the pure helper directly (underscore module name) and asserts detection FIRES -
the AC#5 disjointness behavior, not just schema shape. Also validates the proposal artifact
shape against templates/proposed.schema.json (structural, stdlib-only)."""

import json, os, sys
from planner_classify import find_overlaps, size_flags, SIZING_BUDGET

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "templates", "proposed.schema.json")
FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def main():
    # --- AC#5: overlap detection FIRES on a known-overlapping pair, stays silent on disjoint ---
    overl = find_overlaps({"a": ["x.go", "y.go"], "b": ["y.go", "z.go"]})
    check("#11 overlap: an overlapping pair emits exactly one overlap flag", len(overl) == 1)
    check("#11 overlap: flag kind is 'overlap'", overl and overl[0]["kind"] == "overlap")
    check("#11 overlap: both branches named, sorted",
          overl and overl[0]["branches"] == ["a", "b"])
    check("#11 overlap: conflicting_paths is exactly the shared file",
          overl and overl[0]["conflicting_paths"] == ["y.go"])
    check("#11 overlap: a proposal string is present", overl and bool(overl[0]["proposal"]))

    disjoint = find_overlaps({"a": ["x.go"], "b": ["y.go"], "c": ["z.go"]})
    check("#11 overlap: a fully disjoint set emits NO overlap flag (AC#5)", disjoint == [])

    # distinct branch-sets sharing different paths -> distinct flags
    multi = find_overlaps({"a": ["s1"], "b": ["s1", "s2"], "c": ["s2"]})
    # s1 shared by {a,b}; s2 shared by {b,c} -> two flags
    check("#11 overlap: two distinct sharing branch-sets -> two flags", len(multi) == 2)
    check("#11 overlap: deterministic order by branches", [f["branches"] for f in multi] == [["a", "b"], ["b", "c"]])

    # #12 widening: an open-PR entry is just another key in the same map (same mechanism)
    widened = find_overlaps({"live-branch": ["api.go"], "pr-77-branch": ["api.go"]})
    check("#11/#12 overlap: an open-PR file list participates in the same overlap check",
          len(widened) == 1 and widened[0]["conflicting_paths"] == ["api.go"])

    # empty / single-branch inputs are silent
    check("#11 overlap: empty input -> no flags", find_overlaps({}) == [])
    check("#11 overlap: single branch -> no flags", find_overlaps({"a": ["x", "y"]}) == [])

    # --- sizing: strict-> threshold on EITHER bound ---
    big_lines = size_flags({"a": {"changed_lines": 401, "files": 1}})
    check("#11 sizing: > line budget emits a sizing flag", len(big_lines) == 1 and big_lines[0]["kind"] == "sizing")
    check("#11 sizing: stats carried through", big_lines and big_lines[0]["stats"] == {"changed_lines": 401, "files": 1})
    big_files = size_flags({"a": {"changed_lines": 10, "files": 11}})
    check("#11 sizing: > file budget emits a sizing flag", len(big_files) == 1)
    # boundary: EXACTLY at budget is NOT over (strict >)
    at_budget = size_flags({"a": {"changed_lines": 400, "files": 10}})
    check("#11 sizing: exactly at budget is NOT flagged (strict >)", at_budget == [])
    under = size_flags({"a": {"changed_lines": 50, "files": 2}})
    check("#11 sizing: under budget -> no flag", under == [])
    check("#11 sizing: default budget is 400 lines / 10 files",
          SIZING_BUDGET == {"changed_lines": 400, "files": 10})
    # custom budget honored
    custom = size_flags({"a": {"changed_lines": 100, "files": 1}}, budget={"changed_lines": 50, "files": 10})
    check("#11 sizing: custom budget honored", len(custom) == 1)
    # deterministic order across branches
    order = size_flags({"z": {"changed_lines": 500, "files": 1}, "a": {"changed_lines": 500, "files": 1}})
    check("#11 sizing: deterministic order by branch", [f["branches"][0] for f in order] == ["a", "z"])

    # --- artifact shape vs templates/proposed.schema.json (structural, stdlib-only) ---
    schema = json.load(open(SCHEMA))
    allowed_kinds = set(schema["properties"]["flags"]["items"]["properties"]["kind"]["enum"])
    check("#11 schema: kinds enum is overlap/sizing/next-tranche",
          allowed_kinds == {"overlap", "sizing", "next-tranche"})
    seed = {"flags": []}
    check("#11 schema: the {\"flags\": []} seed has the required 'flags' array",
          isinstance(seed.get("flags"), list))
    # every flag the helpers emit conforms: kind in enum + a proposal string
    sample = overl + big_lines
    check("#11 schema: every emitted flag has an allowed kind + a proposal",
          all(f["kind"] in allowed_kinds and isinstance(f.get("proposal"), str) and f["proposal"] for f in sample))

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("All planner-classify harness checks passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
