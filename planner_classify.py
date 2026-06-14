#!/usr/bin/env python3
"""Pure classification logic for the planner (lookahead) role - issue #11.

The planner AGENT runs the git commands (`git diff --name-only`, `git diff --stat`,
and #12's `gh pr diff --name-only` for open PRs) per its READ-ONLY charter; THIS module
only classifies the already-collected diff data into proposal flags. Keeping the
load-bearing set/threshold logic here makes it deterministic and harness-provable -
the repo ethos that "the determinism guarantee lives in the test harness, not runtime
prose" (the same reason the guard is proven by test-orchestrate-guard.py).

Importable (underscore module name) because test-planner-classify.py calls these
functions directly. Stdlib only.

Flag shapes match templates/proposed.schema.json:
  overlap:     {"kind": "overlap",     "branches": [...], "conflicting_paths": [...], "proposal": str}
  sizing:      {"kind": "sizing",      "branches": [b],   "stats": {...},            "proposal": str}
  (next-tranche flags are produced by the agent's dependency-graph step, not here.)
"""

from collections import defaultdict

# The ONE sizing budget (issue #11 P1): a branch is oversized if it exceeds EITHER bound.
SIZING_BUDGET = {"changed_lines": 400, "files": 10}


def find_overlaps(branch_files):
    """Given {branch: [paths from `git diff --name-only`]}, return `overlap` flags for any
    path touched by >= 2 branches. EXACT (diff-confirmed), not predicted. One flag per
    distinct SET of branches that share path(s); each lists the shared `conflicting_paths`.

    The comparison set may include open-PR file lists too (#12): pass an open PR under a
    stable key (e.g. its branch name) alongside the live-worktree branches - the overlap
    mechanism is identical, so the widening is just another entry in `branch_files`.
    """
    path_to_branches = defaultdict(set)
    for branch, files in branch_files.items():
        for path in set(files):
            path_to_branches[path].add(branch)

    groups = defaultdict(list)  # frozenset(branches) -> [shared paths]
    for path, branches in path_to_branches.items():
        if len(branches) >= 2:
            groups[frozenset(branches)].append(path)

    flags = []
    for branchset, paths in groups.items():
        flags.append({
            "kind": "overlap",
            "branches": sorted(branchset),
            "conflicting_paths": sorted(paths),
            "proposal": ("serialize or re-partition these branches; they touch shared "
                         "files (overlap surfaces as a rebase/merge conflict if left parallel)"),
        })
    flags.sort(key=lambda fl: (fl["branches"], fl["conflicting_paths"]))
    return flags


def size_flags(branch_stats, budget=None):
    """Given {branch: {"changed_lines": N, "files": M}} (from `git diff --stat`), return a
    `sizing` flag for each branch exceeding EITHER bound of `budget` (default SIZING_BUDGET).
    Strict `>` per the issue ("> 400 changed lines OR > 10 files"). Deterministic order.
    """
    if budget is None:
        budget = SIZING_BUDGET
    flags = []
    for branch in sorted(branch_stats):
        st = branch_stats[branch]
        lines = st.get("changed_lines", 0)
        files = st.get("files", 0)
        if lines > budget["changed_lines"] or files > budget["files"]:
            flags.append({
                "kind": "sizing",
                "branches": [branch],
                "stats": {"changed_lines": lines, "files": files},
                "proposal": ("split into stacked sub-PRs; exceeds the sizing budget "
                             f"({budget['changed_lines']} lines OR {budget['files']} files)"),
            })
    return flags
