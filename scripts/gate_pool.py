#!/usr/bin/env python3
"""The machine-wide gate pool (epic #538, unit A / #539).

Design of record: skills/orchestrate/design/DESIGN-gate-pool.md ("the doc"). The pool lets
several worktrees gate at once without oversubscribing the machine: every gate, pre-push hook
and named heavy command takes `cost` units of one machine budget, and what does not fit waits.

THIS FILE CURRENTLY HOLDS THE POLICY ONLY: `cls()` and `schedule()`, the doc's section 2
pseudocode as a pure function. It opens no file, takes no lock and imports nothing. The pool
on disk (slot locks, tickets, `.sched` sidecars) and the gate-runner wiring are added by the
later PRs of #539; until they land nothing imports this module outside its harness.

Importable (underscore module name) because test-gate-pool.py calls these functions directly,
replaying both worked tables of section 2, and mutation-proves every rule against a copy.
Stdlib only.
"""

# BUMP when the ordering rules or the meaning of anything on disk change (doc section 4): a
# process schedules, rewrites and unlinks only records of ITS OWN protocol.
POOL_PROTOCOL = 1


class Entry:
    """One waiting entry: a ticket in unit A, also a queued job in unit B.

    `seq` orders entries within a class. `kind` is "gate" (hand-run or hook), "named" (a named
    heavy command) or "job" (a queued job, unit B). `cost` is the budget the entry asked for and
    `weight` the weight it declared; a ticket carries no separate weight, so it defaults to the
    cost (doc section 1: "for a ticket, cls() reads cost as e.weight"). `worktree` is the
    recorded worktree key, `bypass` the stored count of times the entry has been passed, and
    `open_pr` whether a job's branch has an open PR. `path` is the record the entry came from.

    Compared and hashed by IDENTITY (no __eq__): schedule() keys its per-pass counts on the
    entry objects it was handed.
    """
    __slots__ = ("seq", "kind", "weight", "cost", "worktree", "bypass", "open_pr", "path")

    def __init__(self, seq, kind, cost, worktree, *, weight=None, bypass=0, open_pr=False,
                 path=None):
        self.seq, self.kind, self.cost, self.worktree = seq, kind, cost, worktree
        self.weight = cost if weight is None else weight
        self.bypass, self.open_pr, self.path = bypass, open_pr, path


def cls(e, cap):
    """The class of an entry: 0 a small named check, 1 a fix round, 2 everything else.
    DERIVED, never requested: nothing a caller passes can select class 0, only a declared
    weight at or below `cap` (the machine's small_check_cap)."""
    if e.kind == "named":
        return 0 if e.weight <= cap else 2
    if e.kind == "job":
        return 1 if e.open_pr else 2
    return 2                               # hand-run gate, pre-push hook


def schedule(entries, free, budget, busy, *, k, cap):
    """entries: every waiting job and ticket. free: unheld budget units. busy: the worktree
    keys of everything running. k: backfill_bypass_limit. cap: small_check_cap.
    Returns [(entry, passed)] to start now, in order; `passed` lists the entries that start
    leaves waiting, whose bypass count the caller's commit raises by one.

    Pure: reads nothing but its arguments and changes none of them. A one-to-one transcription
    of the doc's section 2 pseudocode, with K and CAP as keyword arguments. Called only under
    admit.lock."""
    order = sorted(entries, key=lambda e: (cls(e, cap), e.seq))
    claimed = set(busy)
    pend = {}                              # passes this pass's own starts will record, by entry

    def prot(x):                           # PROTECTED: passed K times, counting this pass's starts
        return x.bypass + pend.get(x, 0) >= k

    def guard():                           # a protected gate (class 1 or 2) waiting on budget, not on its worktree
        return any(cls(w, cap) > 0 and prot(w) and w.worktree not in claimed for w in order)

    start, blocked = [], []                # blocked = entries ahead in order that could not start
    for e in order:
        if e.worktree in claimed:          # PER-WORKTREE EXCLUSION: one thing per worktree.
            continue                       # Not eligible: neither started nor counted as blocked.
        c = min(e.cost, budget)            # a cost above the whole budget RUNS ALONE
        if c == 0:                         # no budget taken, never a pass
            start.append((e, []))
            claimed.add(e.worktree)
            continue
        if cls(e, cap) == 0 and guard():
            continue                       # HOLD small checks while a protected gate waits
        if any(prot(b) for b in blocked):
            continue                       # HOLD: a protected entry is ahead; let the budget drain
        if c <= free:
            passed = list(blocked)         # backfill past earlier entries
            if cls(e, cap) == 0:           # priority past waiting gates
                passed += [w for w in order
                           if cls(w, cap) > 0 and w.worktree not in claimed
                           and w.worktree != e.worktree   # e is about to claim it: w waits on the worktree
                           and min(w.cost, budget) > free - c]
            for b in passed:               # so one pass cannot pass an entry beyond K
                pend[b] = pend.get(b, 0) + 1
            start.append((e, passed))
            free -= c
            claimed.add(e.worktree)
        else:
            blocked.append(e)
    return start
