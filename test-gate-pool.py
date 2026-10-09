#!/usr/bin/env python3
"""Harness for scripts/gate_pool.py, the machine-wide gate pool (epic #538, unit A / #539).
Hand-rolled, stdlib-only, no pytest. Design of record:
skills/orchestrate/design/DESIGN-gate-pool.md ("the doc").

TWO HALVES. The POLICY cases cover `cls()` and the three-class `schedule()` of the doc's
section 2 as a pure function. The DISK cases cover the pool of sections 1 and 4: slots,
tickets, `.sched` sidecars and the scheduling pass under admit.lock, and the nested-run
exemption of section 5 (the three conditions, the `.nest` chain). The RUNNER cases spawn the
real scripts/gate-runner.py in `git init`ed temp repos, each with its own tiny `.gates.toml`:
first the proof that a machine with no budget configured behaves exactly as before the pool
existed (output equal to goldens recorded from the pre-pool runner).

DISK CASES NEED NO THREAD AND NO SLEEP. flock belongs to the open file description, so a
second open of a locked file in the SAME process conflicts (doc section 4, RUN E1c). A case
therefore plays several processes by holding several waiters in one temp home and calling
`poll()` on them in a fixed order. A DEAD owner is played by closing its descriptors (what
the kernel does), and once for real by SIGKILLing a helper child after reading the line it
prints when it holds; `wait()` returns only after the kernel has closed its descriptors.

POLICY CASES ARE TABLE-DRIVEN. Each builds synthetic entries, calls `schedule()` once, and compares the
WHOLE answer: which entries start, in which order, and which entries each start passes. The
two worked tables and the "when the class matters" example of section 2 are REPLAYED row by
row, asserting the start set AND the bypass counts after each row. A replay commits every
returned start (the way unit B's dispatcher will), so it also proves the bound of K holds
when one evaluator commits a whole batch.

ISOLATION. No test may touch the real pool at ~/.claude/gate-queue. The module has no default
home, so that is structural for the library cases; the belts on top of it:
  1. GATEQ_HOME is pinned to a fresh empty temp directory and GATEQ_HOLDER / GATEQ_NEST are
     removed, before anything else runs, and the directory must still be empty at the end;
  2. a case asserts the module source names neither `.claude` nor `expanduser`;
  3. every runner is spawned through `runner_env(home)`, whose pool root is a REQUIRED argument
     and whose HOME is a temp directory, so even a dropped GATEQ_HOME resolves `~` to scratch;
  4. the real pool directory (under the passwd-database home, whatever HOME says) is listed
     at the start and at the end, and the two must be equal.

MUTATION SELF-TEST (the test-ci-gates-lockstep.py pattern). An assertion that cannot fail is
decorative, so the harness ends by copying scripts/gate_pool.py and scripts/gate-runner.py into
a temp directory, breaking ONE thing in one copy, and re-running itself against that directory
with `--only <case>`. The run
must exit non-zero AND print the named check's FAIL line. An unmutated copy runs first and must
pass, so a broken fixture cannot read as a full set of kills. No working-tree file is ever
opened for writing, so a concurrent `git add` can never capture a mutant.
"""

import ast
import atexit
import contextlib
import fcntl
import importlib.util
import io
import itertools
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# --- isolation pins: before the module under test is imported, before any case -------------
for _var in ("GATEQ_HOLDER", "GATEQ_NEST"):
    os.environ.pop(_var, None)
# A git hook exports GIT_DIR (and friends); every git this harness starts, the fixture's included,
# would then write into the REAL repository. No GIT_ variable survives into a case.
for _var in [v for v in os.environ if v.startswith("GIT_")]:
    os.environ.pop(_var, None)
_PINNED_HOME = tempfile.mkdtemp(prefix="gate-pool-test-")
os.environ["GATEQ_HOME"] = _PINNED_HOME
atexit.register(shutil.rmtree, _PINNED_HOME, True)   # every exit path, early returns included
# The HOME every spawned runner gets: with GATEQ_HOME dropped, `~/.claude/gate-queue` lands here.
_FAKE_HOME = tempfile.mkdtemp(prefix="gate-pool-test-home-")
atexit.register(shutil.rmtree, _FAKE_HOME, True)
_REAL_POOL = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".claude", "gate-queue")


def _real_pool_listing():
    try:
        return sorted(os.listdir(_REAL_POOL))
    except OSError:
        return None                        # absent (or unreadable): must still be so at the end


_REAL_POOL_BEFORE = _real_pool_listing()

# GATE_POOL_SCRIPTS points the harness at a COPY of the module; only the mutation self-test
# at the end of this file sets it.
SCRIPTS = os.environ.get("GATE_POOL_SCRIPTS") or os.path.join(HERE, "scripts")
MODULE = os.path.join(SCRIPTS, "gate_pool.py")
RUNNER = os.path.join(SCRIPTS, "gate-runner.py")
sys.dont_write_bytecode = True
sys.path.insert(0, SCRIPTS)
import gate_pool as gp  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def same(name, got, want):
    """check() for a whole answer; a mismatch prints both sides."""
    check(name, got == want)
    if got != want:
        print(f"         got  {got!r}\n         want {want!r}")


# --- synthetic entries ----------------------------------------------------------------------
# `path` doubles as the entry's label. Every entry gets its own worktree unless a case says
# otherwise, because sharing one is itself a scheduling fact (the per-worktree exclusion).
def ent(name, seq, kind, cost, wt=None, bypass=0, open_pr=False, weight=None):
    return gp.Entry(seq=seq, kind=kind, cost=cost, worktree=wt or "/wt/" + name,
                    weight=weight, bypass=bypass, open_pr=open_pr, path=name)


def gate(name, seq, cost, **kw):       # a hand-run gate or the pre-push hook: class 2
    return ent(name, seq, "gate", cost, **kw)


def named(name, seq, weight, **kw):    # a named command: class 0 at or below the cap, else 2
    return ent(name, seq, "named", weight, **kw)


def fix(name, seq, cost, **kw):        # a queued job with an open PR (unit B): class 1
    return ent(name, seq, "job", cost, open_pr=True, **kw)


def push(name, seq, cost, **kw):       # a queued job with no open PR (unit B): class 2
    return ent(name, seq, "job", cost, **kw)


def names(start):
    return [(e.path, [p.path for p in passed]) for e, passed in start]


def run(entries, free, busy=(), budget=10, k=2, cap=2):
    return names(gp.schedule(entries, free, budget, set(busy), k=k, cap=cap))


class Replay:
    """Replays a sequence of passes over one waiting set. Each row commits EVERY returned
    start (bypass += 1 for each entry it passed, then the started entry leaves the set)."""

    def __init__(self, title, waiting, budget=10, k=2, cap=2):
        self.title, self.waiting = title, list(waiting)
        self.budget, self.k, self.cap = budget, k, cap

    def row(self, label, free, want_start, want_bypass):
        start = gp.schedule(self.waiting, free, self.budget, set(), k=self.k, cap=self.cap)
        for e, passed in start:
            for b in passed:
                b.bypass += 1
            self.waiting.remove(e)
        same(f"{self.title}, {label}: starts", names(start), want_start)
        same(f"{self.title}, {label}: bypass counts",
             {e.path: e.bypass for e in self.waiting}, want_bypass)


# --- cases ----------------------------------------------------------------------------------
def case_cls():
    cap = 2
    same("cls: a named command AT the cap is class 0", gp.cls(named("n", 1, 2), cap), 0)
    same("cls: a named command below the cap is class 0", gp.cls(named("n", 1, 1), cap), 0)
    same("cls: a named command one above the cap is class 2", gp.cls(named("n", 1, 3), cap), 2)
    same("cls: the cap is the machine's (weight 3 is class 0 at cap 3)",
         gp.cls(named("n", 1, 3), 3), 0)
    same("cls: a job with an open PR (a fix round) is class 1", gp.cls(fix("f", 1, 4), cap), 1)
    same("cls: a job with no open PR (a first push) is class 2", gp.cls(push("c", 1, 4), cap), 2)
    same("cls: a hand-run gate is class 2", gp.cls(gate("g", 1, 4), cap), 2)
    same("cls: a small gate is still class 2 (the kind decides, not the size)",
         gp.cls(gate("g", 1, 1), cap), 2)
    same("cls: an unknown kind is class 2", gp.cls(ent("h", 1, "hook", 1), cap), 2)
    same("cls: a named command is classed by its declared weight, never its clamped cost",
         gp.cls(ent("n", 1, "named", 2, weight=8), cap), 2)
    same("cls: a ticket built without a weight reads its cost as the weight",
         gate("g", 1, 4).weight, 4)
    same("cls: an explicit weight of 0 is honored, not replaced by the cost",
         gp.cls(ent("n", 1, "named", 5, weight=0), cap), 0)


def case_order():
    a, b, c = gate("a", 1, 1), gate("b", 2, 1), gate("c", 3, 1)
    same("order: three equal-cost entries and one free unit start the lowest seq only",
         run([c, a, b], 1), [("a", [])])
    same("order: with room for all three they start in seq order",
         run([c, a, b], 3), [("a", []), ("b", []), ("c", [])])
    same("order: a fix round goes ahead of an earlier first push, and that is not a pass",
         run([push("C", 10, 4), fix("F", 20, 4)], 4), [("F", [])])
    same("order: a first push backfills past a blocked fix round, and that IS a pass",
         run([fix("F", 5, 8), push("C", 9, 2)], 2), [("C", ["F"])])
    same("order: one pass serves class 0, then class 1, then class 2",
         run([push("C", 10, 4), fix("F", 20, 4), named("n", 30, 2)], 10),
         [("n", []), ("F", []), ("C", [])])
    same("order: nothing waiting starts nothing", run([], 10), [])


def case_clamp():
    same("clamp: a cost above the whole budget starts when the whole budget is free",
         run([gate("big", 1, 12)], 10), [("big", [])])
    same("clamp: nothing starts beside a cost above the budget",
         run([gate("big", 1, 12), gate("s", 2, 1)], 10), [("big", [])])
    same("clamp: a cost above the budget waits while anything else holds a unit",
         run([gate("big", 1, 12)], 9), [])
    same("clamp: a small entry backfills past a waiting cost above the budget",
         run([gate("big", 1, 12), gate("s", 2, 1)], 9), [("s", ["big"])])
    same("clamp: a cost equal to the budget starts on an empty machine",
         run([gate("g", 1, 10)], 10), [("g", [])])


def case_table1():
    # Worked numbers 1, backfill only. Budget 10, running A(4) and B(4), all first pushes.
    t = Replay("table 1", [push("H", 10, 8), push("C", 11, 4), push("D", 12, 2),
                           push("E", 13, 4), push("F", 14, 4)])
    t.row("pass 1", 2, [("D", ["H", "C"])], {"H": 1, "C": 1, "E": 0, "F": 0})
    t.row("A finishes", 4, [("C", ["H"])], {"H": 2, "E": 0, "F": 0})
    t.row("B finishes", 4, [], {"H": 2, "E": 0, "F": 0})
    t.row("D, then C finish", 10, [("H", [])], {"E": 0, "F": 0})
    t.row("pass after", 2, [], {"E": 0, "F": 0})
    t.row("the next free 4", 4, [("E", [])], {"F": 0})


def case_table2():
    # Worked numbers 2, a stream of class-0 checks. Budget 10, running G1(4) and G2(4).
    t = Replay("table 2", [gate("H", 10, 8), named("m1", 20, 2)])
    t.row("m1 arrives", 2, [("m1", ["H"])], {"H": 1})
    t.waiting.append(named("r1", 30, 2))
    t.row("r1 arrives", 0, [], {"H": 1, "r1": 0})
    t.row("G1 finishes", 4, [("r1", ["H"])], {"H": 2})
    t.waiting.append(named("m2", 40, 2))
    t.row("m2 arrives", 2, [], {"H": 2, "m2": 0})
    t.row("m1, r1, G2 finish", 10, [("H", [])], {"m2": 0})
    t.row("next poll", 2, [("m2", [])], {})


def case_class_matters():
    # "When the class matters at all": budget 10, G1 finishes and 4 units free up.
    def waiting(r1_weight=2):
        return [fix("F", 15, 4), push("C", 18, 4), named("r1", 30, r1_weight)]
    with0 = Replay("class matters, with class 0", waiting())
    with0.row("G1 finishes", 4, [("r1", ["F", "C"])], {"F": 1, "C": 1})
    without = Replay("class matters, without class 0", waiting(), cap=0)
    without.row("G1 finishes", 4, [("F", [])], {"C": 0, "r1": 0})
    heavy = Replay("class matters, r1 as a full suite at weight 8", waiting(8))
    heavy.row("G1 finishes", 4, [("F", [])], {"C": 0, "r1": 0})


def case_protected():
    same("protected: a protected gate holds a later gate that fits",
         run([gate("H", 10, 8, bypass=2), gate("E", 13, 4)], 4), [])
    same("protected: a gate passed once holds nobody yet",
         run([gate("H", 10, 8, bypass=1), gate("E", 13, 4)], 4), [("E", ["H"])])
    same("protected: a protected gate holds a class-0 check that fits",
         run([gate("H", 10, 8, bypass=2), named("m", 40, 2)], 2), [])
    same("protected: a gate passed once does not hold a class-0 check",
         run([gate("H", 10, 8, bypass=1), named("m", 40, 2)], 2), [("m", ["H"])])
    same("protected: a protected first push does NOT hold a fix round",
         run([push("H", 10, 8, bypass=2), fix("F", 20, 4)], 4), [("F", [])])
    same("protected: a protected fix round holds first pushes and class-0 checks",
         run([fix("F", 20, 8, bypass=2), push("C", 10, 2), named("m", 30, 2)], 2), [])
    same("protected: a protected gate waiting on its worktree holds no class-0 check",
         run([gate("H", 10, 8, bypass=2, wt="/wt/X"), named("m", 40, 2)], 2, busy={"/wt/X"}),
         [("m", [])])
    same("protected: a protected gate starts once it fits, and what no longer fits waits",
         run([gate("H", 10, 8, bypass=2), gate("E", 13, 4)], 10), [("H", [])])
    same("protected: a protected class-0 check holds what is behind it",
         run([named("n1", 1, 2, bypass=2), named("n2", 2, 1), gate("g", 3, 1)], 1), [])
    same("protected: a protected class-0 check does not hold a check AHEAD of it",
         run([named("n2", 1, 1), named("n1", 2, 2, bypass=2)], 1), [("n2", [])])


def case_worktree():
    X = "/wt/X"
    same("worktree: two gates in one worktree never start together",
         run([gate("A", 1, 4, wt=X), gate("B", 2, 4, wt=X)], 10), [("A", [])])
    same("worktree: a check and a gate in one worktree never start together",
         run([named("m", 2, 2, wt=X), gate("G", 1, 4, wt=X)], 10), [("m", [])])
    same("worktree: an entry whose worktree is running does not start",
         run([gate("B", 1, 4, wt=X)], 10, busy={X}), [])
    same("worktree: an entry waiting on its worktree accrues no pass from a backfill",
         run([gate("B", 1, 8, wt=X), gate("C", 2, 2)], 2, busy={X}), [("C", [])])
    same("worktree: an entry waiting on its worktree accrues no pass from a class-0 start",
         run([gate("B", 1, 8, wt=X), named("m", 2, 2)], 2, busy={X}), [("m", [])])
    same("worktree: a protected entry waiting on its worktree holds nobody back",
         run([gate("B", 1, 8, wt=X, bypass=2), gate("C", 2, 2)], 2, busy={X}), [("C", [])])
    same("worktree: a worktree claimed earlier in the pass is not a block and not a pass",
         run([gate("A", 1, 2, wt=X), gate("B", 2, 8, wt=X), gate("C", 3, 2)], 4),
         [("A", []), ("C", [])])


def case_class0_pass_list():
    X = "/wt/X"
    same("class-0 pass list: no pass against a gate waiting in the check's own worktree",
         run([named("m", 30, 2, wt=X), gate("G", 10, 8, wt=X), gate("H", 11, 8, wt="/wt/Y")], 2),
         [("m", ["H"])])
    same("class-0 pass list: a gate the start leaves short of budget is passed",
         run([named("m", 30, 2), gate("G", 10, 4)], 5), [("m", ["G"])])
    same("class-0 pass list: a gate that still fits beside the check is not passed",
         run([named("m", 30, 2), gate("G", 10, 4)], 6), [("m", []), ("G", [])])
    same("class-0 pass list: a class-0 start records no pass against another class-0 check",
         run([named("m", 1, 2), named("n", 2, 2)], 2), [("m", [])])
    same("class-0 pass list: a gate whose worktree an earlier start claimed is not passed",
         run([named("m1", 1, 2, wt=X), named("m2", 2, 2, wt="/wt/Y"), gate("G", 3, 8, wt=X)],
             4), [("m1", []), ("m2", [])])


def case_bound_in_pass():
    def small_gates():
        return [gate(f"s{i}", 10 + i, 1) for i in (1, 2, 3, 4)]
    same("bound: one pass over a gate at bypass 1 with four small gates behind returns ONE start",
         run([gate("H", 10, 8, bypass=1)] + small_gates(), 4), [("s1", ["H"])])
    same("bound: one pass over a gate at bypass 1 with four class-0 checks returns ONE start",
         run([gate("H", 10, 8, bypass=1)] + [named(f"m{i}", 20 + i, 1) for i in (1, 2, 3, 4)], 4),
         [("m1", ["H"])])
    same("bound: one pass over a gate at bypass 0 returns exactly K starts",
         run([gate("H", 10, 8)] + small_gates(), 4), [("s1", ["H"]), ("s2", ["H"])])
    t = Replay("bound", [gate("H", 10, 8, bypass=1)] + small_gates())
    t.row("a committed batch", 4, [("s1", ["H"])], {"H": 2, "s2": 0, "s3": 0, "s4": 0})


def case_cost_zero():
    X = "/wt/X"
    same("cost 0: a zero-cost entry behind a blocked one starts and records no pass",
         run([gate("H", 1, 8), push("Z", 2, 0)], 2), [("Z", [])])
    same("cost 0: a zero-cost entry starts with nothing free",
         run([push("Z", 1, 0)], 0), [("Z", [])])
    same("cost 0: a zero-cost entry is not held by a protected entry ahead of it",
         run([gate("H", 1, 8, bypass=2), push("Z", 2, 0)], 2), [("Z", [])])
    same("cost 0: a zero-cost entry takes no budget",
         run([push("Z", 1, 0), gate("G", 2, 4)], 4), [("Z", []), ("G", [])])
    same("cost 0: a zero-cost start claims its worktree",
         run([push("Z1", 1, 0, wt=X), push("Z2", 2, 0, wt=X), gate("G", 3, 2, wt=X)], 10),
         [("Z1", [])])
    same("cost 0: a zero-cost entry waits for its worktree like any other",
         run([push("Z", 1, 0, wt=X)], 10, busy={X}), [])


def case_pure():
    H, D = gate("H", 10, 8), gate("D", 12, 2)
    entries, busy = [D, H], {"/wt/elsewhere"}
    first = gp.schedule(entries, 2, 10, busy, k=2, cap=2)
    same("pure: the pass under test does pass an entry", names(first), [("D", ["H"])])
    check("pure: schedule() leaves the entries list as it found it", entries == [D, H])
    same("pure: schedule() writes no bypass count (commit is the caller's)",
         (H.bypass, D.bypass), (0, 0))
    same("pure: schedule() leaves the busy set as it found it", busy, {"/wt/elsewhere"})
    same("pure: the same arguments give the same answer",
         names(gp.schedule(entries, 2, 10, busy, k=2, cap=2)), names(first))
    same("pure: busy may be any iterable of worktree keys",
         names(gp.schedule([gate("B", 1, 1, wt="/wt/X")], 10, 10, ["/wt/X"], k=2, cap=2)), [])


def case_static():
    with open(MODULE, encoding="utf-8") as f:
        src = f.read()
    check("static: POOL_PROTOCOL is the integer 1",
          type(gp.POOL_PROTOCOL) is int and gp.POOL_PROTOCOL == 1)
    check("static: the module names no .claude path (the home is always passed in)",
          ".claude" not in src)
    check("static: the module never expands a home directory", "expanduser" not in src)
    check("static: the module under test is the one in the selected scripts directory",
          os.path.realpath(gp.__file__) == os.path.realpath(MODULE))
    # An AST walk, so the comments that explain the rule do not trip it (doc section 4, rule 1).
    bad = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Attribute, ast.Name)):
            ident = node.attr if isinstance(node, ast.Attribute) else node.id
            if ident in ("fork", "forkpty"):
                bad.append(ident)
        elif isinstance(node, ast.keyword) and (node.arg == "pass_fds" or (
                node.arg == "close_fds" and getattr(node.value, "value", None) is False)):
            bad.append(node.arg)
    same("static: no bare fork, no pass_fds and no close_fds=False in the module", bad, [])


# --- the pool on disk -------------------------------------------------------------------------
def cfg(budget=10, **kw):
    return {"protocol": gp.POOL_PROTOCOL, "budget": budget, **kw}


def with_home(fn):
    """Run a case against its OWN pool home, a directory that does not exist yet."""
    def case():
        with tempfile.TemporaryDirectory(prefix="gate-pool-home-") as tmp:
            fn(os.path.join(tmp, "gq"))
    return case


def lock_free(path):
    """What any other process would see: a FRESH open of `path`, try-locked."""
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def die(w):
    """Play a dead owner: every descriptor closed, nothing unlinked, no release() run."""
    for fd in (w.holder.fds if w.holder else [w.fd]):
        os.close(fd)


def listing(home, sub="waiters", suffix=""):
    return sorted(n for n in os.listdir(os.path.join(home, sub)) if n.endswith(suffix))


def on_disk(w):
    with open(w.path, encoding="utf-8") as f:
        return json.load(f)


def sched(w):
    return w.path[:-len(".ticket")] + ".sched"


@with_home
def case_disk_budget(home):
    # Each waiter gets its own Pool object, as each process would.
    a, b, c = (gp.Pool(home, cfg()).enter("gate", "gate", 4, "/wt/" + n) for n in "abc")
    same("disk budget: a new ticket is on disk in state waiting, with the fields of section 1",
         on_disk(c), {"pid": os.getpid(), "pool_protocol": 1, "kind": "gate", "name": "gate",
                      "cost": 4, "worktree": "/wt/c", "state": "waiting"})
    check("disk budget: a waiting ticket is flocked from the moment it exists",
          not lock_free(c.path))
    same("disk budget: two cost-4 gates fit in a budget of 10", (a.poll(), b.poll()), (True, True))
    same("disk budget: a third cost-4 gate waits", c.poll(), False)
    same("disk budget: the wait line names the cost, the budget and what is free",
         c.status(), "gate-runner: waiting for 4 of 10 gate slots (2 free, 0 ahead)")
    same("disk budget: a holder's ticket says running", on_disk(a)["state"], "running")
    same("disk budget: a holder holds its ticket plus one slot per cost unit",
         len(a.holder.fds), 5)
    same("disk budget: a failed poll keeps no slot (2 of 10 are still free to a newcomer)",
         sum(lock_free(os.path.join(home, "slots", n)) for n in listing(home, "slots")), 2)
    modes = {os.path.relpath(os.path.join(d, n), home): os.stat(os.path.join(d, n)).st_mode & 0o777
             for d, subs, files in os.walk(home) for n in subs + files}
    same("disk budget: every pool directory is 0700 and every file 0600",
         {k: oct(v) for k, v in modes.items()
          if v != (0o700 if k in ("waiters", "slots", "tmp") else 0o600)}, {})
    same("disk budget: the root itself is 0700", os.stat(home).st_mode & 0o777, 0o700)
    a.holder.release()
    same("disk budget: a release frees the slots and the waiter starts", c.poll(), True)
    b.holder.release(); c.leave()
    same("disk budget: after every release waiters/ and tmp/ are empty",
         (listing(home), listing(home, "tmp")), ([], []))
    same("disk budget: the budget is one slot file per cost unit",
         listing(home, "slots"), [f"{i:03d}.lock" for i in range(10)])


@with_home
def case_disk_order(home):
    pool = gp.Pool(home, cfg(budget=1))
    a, b = pool.enter("gate", "gate", 1, "/wt/a"), pool.enter("gate", "gate", 1, "/wt/b")
    os.unlink(os.path.join(home, "seq"))
    c = pool.enter("gate", "gate", 1, "/wt/c")
    same("disk order: seq follows enter() order and survives a deleted seq file",
         (a.seq, b.seq, c.seq), (1, 2, 3))
    same("disk order: a ticket's name leads with its 10-digit seq",
         os.path.basename(c.path)[:11], "0000000003-")
    same("disk order: a later arrival does not start ahead of an earlier one",
         (c.poll(), b.poll()), (False, False))
    same("disk order: the wait line counts the entries ahead",
         c.status(), "gate-runner: waiting for 1 of 1 gate slots (1 free, 2 ahead)")
    same("disk order: the earliest starts", a.poll(), True)
    a.holder.release()
    same("disk order: then the next in arrival order, not the last", (c.poll(), b.poll()),
         (False, True))
    b.holder.release(); c.leave()
    d = pool.enter("gate", "gate", 1, "/wt/d")
    same("disk order: a seq is never reused once its ticket is gone (the seq file)", d.seq, 4)
    with open(os.path.join(home, "seq"), "w", encoding="utf-8") as f:
        f.write("not a number\n")
    e = pool.enter("gate", "gate", 1, "/wt/e")
    same("disk order: a corrupt seq file is rebuilt from the visible maximum", e.seq, 5)
    d.leave(); e.leave()
    same("disk order: leave() unlinks a waiting ticket", listing(home), [])


@with_home
def case_disk_worktree(home):
    pool = gp.Pool(home, cfg())
    a = pool.enter("gate", "gate", 4, "/wt/X")
    b = pool.enter("gate", "gate", 4, "/wt/X")       # same worktree; its cost fits beside a
    g = pool.enter("gate", "gate", 8, "/wt/X")       # same worktree, and too big to fit as well
    c = pool.enter("gate", "gate", 2, "/wt/Y")
    same("disk worktree: the first entry in a worktree starts", a.poll(), True)
    same("disk worktree: a second entry in the SAME worktree does not start", b.poll(), False)
    same("disk worktree: its wait line names who holds the worktree", b.status(),
         f"gate-runner: waiting for this worktree (held by gate pid {os.getpid()})")
    same("disk worktree: an entry in another worktree starts past both", c.poll(), True)
    check("disk worktree: an entry waiting on its worktree accrues no pass (no sidecar appears)",
          not os.path.exists(sched(b)) and not os.path.exists(sched(g)))
    a.holder.release(); c.holder.release()
    same("disk worktree: the next one starts once the worktree's holder is gone, and only one",
         (g.poll(), b.poll(), g.poll()), (False, True, False))
    b.leave(); g.leave()


def _is_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


@with_home
def case_release_atomic(home):
    pool = gp.Pool(home, cfg())
    a = pool.enter("gate", "gate", 4, "/wt/a"); a.poll()
    fds, admit, seen = list(a.holder.fds), pool._admit, []

    @contextlib.contextmanager
    def spy():                             # what is still held the moment admit.lock is let go
        with admit():
            yield
        seen.append((os.path.exists(a.path), [_is_open(fd) for fd in fds]))
    pool._admit = spy
    a.holder.release()
    same("release: the ticket is gone and every lock dropped in ONE admit.lock section",
         (len(fds), seen), (5, [(False, [False] * 5)]))


@with_home
def case_sidecar(home):
    pool = gp.Pool(home, cfg())
    x = pool.enter("gate", "gate", 4, "/wt/x"); x.poll()
    b = pool.enter("gate", "gate", 8, "/wt/b")       # 8 > the 6 free: blocked on budget
    inode = os.fstat(b.fd).st_ino
    c = pool.enter("gate", "gate", 2, "/wt/c")
    same("sidecar: a later entry backfills past the blocked one", (b.poll(), c.poll()),
         (False, True))
    check("sidecar: a bypass written by another evaluator leaves the ticket's lock HELD",
          not lock_free(b.path))
    same("sidecar: the passed ticket is still the inode its owner opened",
         os.stat(b.path).st_ino, inode)
    with open(sched(b), encoding="utf-8") as f:
        same("sidecar: the pass is recorded in the passed ticket's .sched sidecar",
             json.load(f), {"pool_protocol": 1, "bypass": 1})
    same("sidecar: the passed ticket's own content is untouched", on_disk(b)["state"], "waiting")
    d = pool.enter("gate", "gate", 2, "/wt/d")
    e = pool.enter("gate", "gate", 2, "/wt/e")
    same("sidecar: a second pass is read back and counted", d.poll(), True)
    same("sidecar: at K passes the entry is protected, and a later one that fits is held",
         e.poll(), False)
    die(b)                                            # b's process is gone; nothing cleaned up
    same("sidecar: a dead waiter protects nobody: the held entry starts", e.poll(), True)
    same("sidecar: the dead ticket and its sidecar were unlinked by that pass",
         (os.path.exists(b.path), os.path.exists(sched(b))), (False, False))
    with open(os.path.join(home, "tmp", "left-behind"), "w", encoding="utf-8"):
        pass
    for w in (x, c, d, e):
        w.leave()
    f = pool.enter("gate", "gate", 1, "/wt/f"); f.poll(); f.leave()
    same("sidecar: a pass clears staging left in tmp/ by a process that died mid-write",
         listing(home, "tmp"), [])


HOLD_CHILD = """
import sys
sys.path.insert(0, sys.argv[1])
import gate_pool as gp
cost, budget = (int(a) for a in (sys.argv[4:6] or (10, 10)))
w = gp.Pool(sys.argv[2], {"protocol": gp.POOL_PROTOCOL, "budget": budget}).enter(
    "gate", "gate", cost, sys.argv[3])
assert w.poll()
print("held", flush=True)
sys.stdin.read()
"""


@with_home
def case_sigkill(home):
    pool = gp.Pool(home, cfg())
    child = subprocess.Popen([sys.executable, "-B", "-c", HOLD_CHILD, SCRIPTS, home, "/wt/W"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        same("sigkill: the helper child holds the whole budget", child.stdout.readline(), "held\n")
        dead = listing(home, suffix=".ticket")
        w = pool.enter("gate", "gate", 4, "/wt/W")
        same("sigkill: a waiter in the holder's worktree does not start", w.poll(), False)
        same("sigkill: its wait line names the live holder", w.status(),
             f"gate-runner: waiting for this worktree (held by gate pid {child.pid})")
        other = pool.enter("gate", "gate", 1, "/wt/other")
        same("sigkill: nor does one elsewhere, with every slot held", other.poll(), False)
        same("sigkill: no slot is free while the holder lives", other.status(),
             "gate-runner: waiting for 1 of 10 gate slots (0 free, 1 ahead)")
        child.send_signal(signal.SIGKILL)
        child.wait()                       # the kernel closed its descriptors before this returns
        same("sigkill: a SIGKILLed holder frees its slots and its worktree at once",
             (w.poll(), other.poll()), (True, True))
        check("sigkill: the dead holder's ticket was unlinked by the next pass",
              len(dead) == 1 and dead[0] not in listing(home))
        w.leave(); other.leave()
    finally:
        child.kill(); child.wait()
        child.stdin.close(); child.stdout.close()


FD_PROBE = """
import os, sys
found = []
for fd in map(int, sys.argv[1:]):
    try:
        os.fstat(fd)
        found.append(fd)
    except OSError:
        pass
print(found)
"""


@with_home
def case_inherit(home):
    w = gp.Pool(home, cfg()).enter("gate", "gate", 3, "/wt/a")
    check("inherit: a waiting ticket's descriptor is non-inheritable",
          not os.get_inheritable(w.fd))
    w.poll()
    same("inherit: no descriptor a holder holds (ticket and slots) is inheritable",
         [os.get_inheritable(fd) for fd in w.holder.fds], [False] * 4)
    # The flag is the mechanism; this is the outcome. A REAL child, started by exec and asked
    # to keep every descriptor it can (close_fds=False), reports which of the holder's
    # descriptor numbers are open in it. The pipe end is the control: it IS inheritable, so a
    # probe that could see nothing would fail here.
    r, wr = os.pipe(); os.set_inheritable(r, True)
    try:
        seen = subprocess.run([sys.executable, "-B", "-c", FD_PROBE, *map(str, w.holder.fds + [r])],
                              close_fds=False, capture_output=True, text=True).stdout
    finally:
        os.close(r); os.close(wr)
    same("inherit: a child a holder starts by exec holds NONE of the holder's lock descriptors",
         seen, f"[{r}]\n")
    w.leave()


@with_home
def case_budget_change(home):
    a = gp.Pool(home, cfg(budget=2)).enter("gate", "gate", 1, "/wt/a"); a.poll()
    same("budget change: a budget of 2 makes two slot files",
         listing(home, "slots"), ["000.lock", "001.lock"])
    b = gp.Pool(home, cfg(budget=4)).enter("gate", "gate", 3, "/wt/b")
    same("budget change: the first evaluator with a RAISED budget creates the new slots",
         (b.poll(), listing(home, "slots")),
         (True, ["000.lock", "001.lock", "002.lock", "003.lock"]))
    b.leave()
    c = gp.Pool(home, cfg(budget=1)).enter("gate", "gate", 1, "/wt/c")
    same("budget change: an evaluator with a LOWERED budget never probes above its own figure",
         (c.poll(), c.status()),
         (False, "gate-runner: waiting for 1 of 1 gate slots (0 free, 0 ahead)"))
    big = gp.Pool(home, cfg(budget=1)).enter("gate", "gate", 12, "/wt/big")
    a.leave(); c.leave()
    same("budget change: a cost above the budget holds the whole budget and no more",
         (big.poll(), len(big.holder.fds)), (True, 2))
    big.leave()


@with_home
def case_root(home):
    os.mkdir(home, 0o755); os.chmod(home, 0o755)
    try:
        gp.Pool(home, cfg()); err = None
    except gp.NotRun as e:
        err = e
    same("root: a pool root open to group or other is refused with exit 2",
         (err.code, err.message) if err else None,
         (2, f"gate-runner: NOT RUN - pool root {home} must be owned by you with mode 0700"))
    same("root: a refused root is left untouched", os.listdir(home), [])


class _NoSubprocess:
    def __getattr__(self, name):
        raise AssertionError(f"subprocess.{name} used under admit.lock")


@with_home
def case_no_git_under_lock(home):
    real, gp.subprocess = gp.subprocess, _NoSubprocess()
    try:
        pool = gp.Pool(home, cfg(budget=1))
        a, b = pool.enter("gate", "gate", 1, "/wt/a"), pool.enter("gate", "gate", 1, "/wt/b")
        a.poll(); b.poll(); a.leave(); b.poll(); b.leave()
        ok = True
    except AssertionError as e:
        ok = False; print(f"         {e}")
    finally:
        gp.subprocess = real
    check("no git under the lock: enter, poll, commit and release start no process", ok)


def refused(fn):
    """The NotRun `fn` raises, as (code, reason, message); None when it raises none."""
    try:
        fn()
    except gp.NotRun as e:
        return e.code, e.reason, e.message
    return None


@with_home
def case_acquire(home):
    pool = gp.Pool(home, cfg(budget=4, wait_timeout_s=70))
    said, slept, now = [], [], [0.0]

    def sleep(s):
        slept.append(s); now[0] += s
        if len(slept) > 500:               # a timeout that stopped firing fails, never hangs
            raise AssertionError("acquire() kept waiting past its timeout")
    kw = dict(environ={}, say=said.append, clock=lambda: now[0], sleep=sleep)
    h = pool.acquire("gate", "gate", 4, "/wt/a", **kw)
    same("acquire: a free pool grants on the first poll, with no line and no sleep",
         (len(h.fds), said, slept), (5, [], []))
    same("acquire: a wait that outlasts wait_timeout_s is NOT RUN, exit 75",
         refused(lambda: pool.acquire("gate", "gate", 2, "/wt/b", **kw)),
         (75, "wait-timeout", "gate-runner: NOT RUN - no gate slot within 70s"))
    same("acquire: one wait line at the first failed poll, then one every 30 s",
         said, ["gate-runner: waiting for 2 of 4 gate slots (0 free, 0 ahead)"] * 3)
    check("acquire: every sleep is the 1 s poll with its jitter, and they add up to the timeout",
          all(0.8 <= s <= 1.2 for s in slept) and 70 <= sum(slept) < 71.2)
    same("acquire: a waiter that gave up leaves no ticket behind",
         listing(home), [os.path.basename(h.path)])
    mark = len(slept)

    def release_on_third(s):               # the fake clock still runs, so this cannot hang
        sleep(s)
        if len(slept) == mark + 3:
            h.release()
    h2 = pool.acquire("gate", "gate", 2, "/wt/b", **{**kw, "sleep": release_on_third})
    same("acquire: a release during the wait is taken at the next poll",
         (len(h2.fds), len(slept) - mark), (3, 3))

    def interrupt(s):
        raise KeyboardInterrupt
    try:
        pool.acquire("gate", "gate", 4, "/wt/c", **{**kw, "sleep": interrupt}); got = None
    except KeyboardInterrupt:
        got = listing(home)
    same("acquire: an interrupt while waiting passes through and leaves no ticket behind",
         got, [os.path.basename(h2.path)])
    h2.release()


def plant(home, name, content, hold=False, sub="waiters"):
    """Put a file in waiters/ that this pool did not write; with `hold`, flock it the way a
    live owner would and return the descriptor."""
    path = os.path.join(home, sub, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content if isinstance(content, str) else json.dumps(content))
    if hold:
        fd = os.open(path, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd


@with_home
def case_foreign(home):
    pool = gp.Pool(home, cfg())
    low = pool.enter("gate", "gate", 1, "/wt/low")           # seq 1
    in_f = pool.enter("gate", "gate", 1, "/wt/F")            # seq 2, the live foreign one's worktree
    alien = {"pid": 1, "pool_protocol": 999, "kind": "gate", "name": "gate", "cost": 1,
             "worktree": "/wt/F", "state": "running"}
    live = plant(home, "0000000003-live.ticket", alien, hold=True)
    plant(home, "0000000004-dead.ticket", alien)
    plant(home, "0000000004-dead.sched", {"pool_protocol": 999, "bypass": 1})
    plant(home, "0000000005-junk.ticket", "not a ticket")    # unreadable counts as foreign
    planted = ["0000000003-live.ticket", "0000000004-dead.sched", "0000000004-dead.ticket",
               "0000000005-junk.ticket"]
    high = pool.enter("gate", "gate", 1, "/wt/high")
    same("foreign: seq allocation counts the foreign tickets it can see", high.seq, 6)
    same("foreign: a waiter does not start behind a LIVE foreign ticket with a lower seq",
         [high.poll() for _ in range(10)], [False] * 10)
    same("foreign: its wait line says so", high.status(),
         "gate-runner: waiting behind a ticket of another pool protocol")
    same("foreign: a live foreign ticket's worktree is busy", (in_f.poll(), in_f.status()),
         (False, "gate-runner: waiting for this worktree (held by another pool protocol)"))
    same("foreign: a live foreign ticket with a HIGHER seq blocks nobody", low.poll(), True)
    same("foreign: no foreign or unreadable record was unlinked in all those passes",
         [n for n in listing(home) if n in planted], planted)
    with open(os.path.join(home, "waiters", planted[2]), encoding="utf-8") as f:
        same("foreign: nor rewritten", json.load(f), alien)
    os.close(live)                                           # the foreign process is gone
    same("foreign: with its lock free it blocks nobody and holds no worktree",
         (high.poll(), in_f.poll()), (True, True))
    same("foreign: and it is STILL never unlinked (doctor names it, the user deletes it)",
         [n for n in listing(home) if n in planted], planted)
    for w in (low, in_f, high):
        w.leave()
    other = home + "-other"
    same("foreign: a config of another pool protocol is refused with exit 2",
         refused(lambda: gp.Pool(other, {"protocol": 2, "budget": 10})),
         (2, "pool-protocol", "gate-runner: NOT RUN - pool protocol 1 != configured 2; "
                              "update the plugin and run configure --apply"))
    check("foreign: and the directory it names is not touched", not os.path.exists(other))


def tree(home):
    """Every entry under `home` (and `home` itself, as ".") with its inode, MODE, size and
    mtime: equal before and after means nothing was created, written, chmod-ed, replaced or
    unlinked."""
    st = os.lstat(home)
    out = [(".", st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns)]
    for root, dirs, files in os.walk(home):
        for n in dirs + files:
            st = os.lstat(os.path.join(root, n))
            out.append((os.path.relpath(os.path.join(root, n), home), st.st_ino, st.st_mode,
                        st.st_size, st.st_mtime_ns))
    return sorted(out)


@with_home
def case_doctor_tickets(home):
    same("doctor tickets: a pool with no waiters/ has none", gp.dead_foreign_tickets(home, 1), [])
    check("doctor tickets: and the pool root is not created", not os.path.exists(home))
    os.makedirs(os.path.join(home, "waiters"))
    alien = {"pid": 1, "pool_protocol": 999, "kind": "gate", "name": "gate", "cost": 1,
             "worktree": "/wt/F", "state": "running"}
    mine = dict(alien, pool_protocol=gp.POOL_PROTOCOL)
    w = os.path.join(home, "waiters")
    held = [plant(home, "0000000001-live.ticket", alien, hold=True),
            plant(home, "0000000004-mine-live.ticket", mine, hold=True)]
    plant(home, "0000000002-dead.ticket", alien)
    plant(home, "0000000002-dead.sched", {"pool_protocol": 999, "bypass": 1})
    plant(home, "0000000003-mine-dead.ticket", mine)
    plant(home, "0000000005-junk.ticket", "not a ticket")
    os.mkdir(os.path.join(w, "0000000006-dir.ticket"))
    before = tree(home)
    same("doctor tickets: a FREE ticket of another protocol is listed with its sidecar, a free "
         "unreadable one without; a HELD ticket and one of our own protocol never are",
         gp.dead_foreign_tickets(home, gp.POOL_PROTOCOL),
         [(os.path.join(w, "0000000002-dead.ticket"), os.path.join(w, "0000000002-dead.sched")),
          (os.path.join(w, "0000000005-junk.ticket"), None)])
    same("doctor tickets: nothing was created, written or unlinked (no admit.lock either)",
         tree(home), before)
    same("doctor tickets: foreign means differing from the CONFIGURED protocol",
         [os.path.basename(t) for t, _ in gp.dead_foreign_tickets(home, 999)],
         ["0000000003-mine-dead.ticket", "0000000005-junk.ticket"])
    check("doctor tickets: every trial lock was dropped, and a live owner's lock was not",
          lock_free(os.path.join(w, "0000000002-dead.ticket"))
          and not lock_free(os.path.join(w, "0000000001-live.ticket")))
    plant(home, "admit.lock", "", sub="")
    before = tree(home)
    same("doctor tickets: with an admit.lock present the answer is the same",
         len(gp.dead_foreign_tickets(home, gp.POOL_PROTOCOL)), 2)
    check("doctor tickets: and admit.lock is left free and untouched",
          lock_free(os.path.join(home, "admit.lock")) and tree(home) == before)
    # An own-protocol ticket whose NAME is not numeric, or whose record lacks a field an
    # evaluator acts on, is not one this code clears: it is listed when its lock is free.
    plant(home, "notnum.ticket", mine)
    plant(home, "0000000009-bad.ticket", dict(mine, state="done"))
    same("doctor tickets: an own-protocol ticket with a non-numeric name or a malformed record "
         "is listed, as documented",
         sorted(os.path.basename(t) for t, _ in gp.dead_foreign_tickets(home, gp.POOL_PROTOCOL)),
         ["0000000002-dead.ticket", "0000000005-junk.ticket", "0000000009-bad.ticket",
          "notnum.ticket"])
    # A HELD admit.lock (a live or stuck evaluator): the helper gives up after its bounded wait
    # and returns None ("skipped"), distinct from [] ("none found"). It must not block; the
    # alarm turns a blocking lock into a FAIL line rather than a hung harness.
    class _Hung(Exception):
        pass

    def _on_alarm(signum, frame):
        raise _Hung()
    saved_wait, gp.ADMIT_WAIT_S = gp.ADMIT_WAIT_S, 0.3
    old_handler = signal.signal(signal.SIGALRM, _on_alarm)
    hold = os.open(os.path.join(home, "admit.lock"), os.O_RDONLY)
    try:
        fcntl.flock(hold, fcntl.LOCK_EX | fcntl.LOCK_NB)
        signal.alarm(10)
        t0 = time.monotonic()
        try:
            got = gp.dead_foreign_tickets(home, gp.POOL_PROTOCOL)
        except _Hung:
            got = "HUNG: blocked on admit.lock"
        finally:
            signal.alarm(0)
        elapsed = time.monotonic() - t0
    finally:
        signal.signal(signal.SIGALRM, old_handler)
        gp.ADMIT_WAIT_S = saved_wait
        os.close(hold)
    same("doctor tickets: a held admit.lock is 'skipped' (None) after a bounded wait, never a "
         "hang and never a lockless read", got, None)
    check("doctor tickets: and it gave up within a few seconds", elapsed < 5)
    same("doctor tickets: once admit.lock is free again the list comes back",
         len(gp.dead_foreign_tickets(home, gp.POOL_PROTOCOL)), 4)
    for fd in held:
        os.close(fd)


# --- nested runs (doc section 5) ---------------------------------------------------------------
def holder_in(pool, worktree, cost=4):
    """A running ticket holder, and the environment it gives the children it starts."""
    h = pool.enter("gate", "gate", cost, worktree); h.poll()
    return h, h.holder.child_env()


def no_wait(s):
    """The sleep of an acquire() that must be granted on its first poll: a wait is a failure
    here, never a hang."""
    raise AssertionError("acquire() had to wait")


@with_home
def case_nested_rows(home):
    pool, said = gp.Pool(home, cfg()), []
    h, env = holder_in(pool, "/wt/X")
    same("nested rows: a ticket holder names its OWN ticket for its children, and blanks "
         "GATEQ_NEST", env, {"GATEQ_HOLDER": h.path, "GATEQ_NEST": ""})
    running = on_disk(h)
    waiting = pool.enter("gate", "gate", 4, "/wt/X")
    plant(home, "0000000007-dead.ticket", running)
    alien = plant(home, "0000000008-alien.ticket", {**running, "pool_protocol": 999}, hold=True)
    rows = {"unset": "", "a missing file": h.path + "x.ticket",
            "an unlocked ticket": os.path.join(home, "waiters", "0000000007-dead.ticket"),
            "another protocol": os.path.join(home, "waiters", "0000000008-alien.ticket"),
            "outside waiters/": os.path.join(home, os.path.basename(h.path)),
            "a ticket still waiting": waiting.path}
    same("nested rows: condition 1 fails -> no exemption, and no line",
         ({k: pool.nested(4, "/wt/X", {"GATEQ_HOLDER": v}, said.append) for k, v in rows.items()},
          said), (dict.fromkeys(rows), []))
    same("nested rows: condition 2 fails (another worktree) -> a ticket, and ONE line naming "
         "the leak", (pool.nested(4, "/wt/Y", env, said.append), said),
         (None, ["gate-runner: ignoring GATEQ_HOLDER, its holder is in another worktree "
                 "(/wt/X); taking a ticket"]))
    same("nested rows: condition 3 fails (heavier than its holder) -> NOT RUN, exit 75, at once",
         refused(lambda: pool.nested(5, "/wt/X", env, said.append)),
         (75, "nested-over-holder",
          "gate-runner: NOT RUN reason=nested-over-holder holder=4 needs=5"))
    os.close(alien)                        # or every ticket below would wait behind it
    tickets = listing(home, suffix=".ticket")
    n = pool.acquire("named", "small", 4, "/wt/X", environ=env, say=said.append, sleep=no_wait)
    same("nested rows: all three hold -> it runs with no ticket and no slot of its own",
         (listing(home, suffix=".ticket"), len(n.fds), os.get_inheritable(n.fds[0])),
         (tickets, 1, False))
    same("nested rows: what it holds is its holder's child lock, which it names for ITS children",
         (n.nest, n.child_env()),
         (h.path + ".nest", {"GATEQ_HOLDER": h.path, "GATEQ_NEST": h.path + ".nest"}))
    n.release()
    t = pool.acquire("gate", "gate", 1, "/wt/Y", environ=env, say=said.append, sleep=no_wait)
    same("nested rows: a run handed a holder from another worktree takes its own ticket and "
         "names THAT one for its children",
         (t.nest, t.child_env()["GATEQ_HOLDER"] == t.path != h.path, len(said)), (None, True, 2))
    t.release(); waiting.leave(); h.leave()


@with_home
def case_nested_serial(home):
    pool = gp.Pool(home, cfg())
    h, env = holder_in(pool, "/wt/X")
    n1, n2, n3 = (pool.nested(4, "/wt/X", env, print) for _ in range(3))
    same("nested serial: two nested runs under one holder do not run together",
         (n1.poll(), n2.poll()), (True, False))
    n1.leave()
    same("nested serial: the second starts when the first ends", (n2.poll(), n3.poll()),
         (True, False))
    die(h)                                 # the holder is SIGKILLed under a running nested run
    same("nested serial: a nested WAIT whose holder died is exempt no longer",
         (n3.poll(), n3.lost), (False, True))
    w = pool.enter("gate", "gate", 1, "/wt/other"); w.poll()
    same("nested serial: the pass that unlinks a dead holder's ticket leaves its HELD child lock",
         (os.path.exists(h.path), os.path.exists(h.path + ".nest")), (False, True))
    n2.leave(); w.leave()
    w = pool.enter("gate", "gate", 1, "/wt/other"); w.poll(); w.leave()
    check("nested serial: a later pass unlinks the child lock once it is free",
          not os.path.exists(h.path + ".nest"))
    # The same through acquire(): the wait turns into an ordinary ticket.
    h2, env2 = holder_in(pool, "/wt/Z")
    first = pool.nested(2, "/wt/Z", env2, print); first.poll()

    naps = []

    def holder_dies(s):
        naps.append(s)
        if len(naps) == 1:
            die(h2)
        elif len(naps) > 3:
            raise AssertionError("acquire() kept waiting under a dead holder")
    got = pool.acquire("named", "small", 2, "/wt/Z", environ=env2, say=lambda line: None,
                       sleep=holder_dies)
    same("nested serial: acquire() takes a ticket when the holder it waited under dies",
         (got.nest, listing(home, suffix=".ticket")), (None, [os.path.basename(got.path)]))
    got.release(); first.leave()


@with_home
def case_nested_chain(home):
    pool = gp.Pool(home, cfg())
    h, env = holder_in(pool, "/wt/X")
    n1 = pool.nested(4, "/wt/X", env, print); n1.poll()
    env1 = {**env, **n1.holder.child_env()}
    c1, c2 = (pool.nested(4, "/wt/X", env1, print) for _ in range(2))
    same("nested chain: a run one level deeper does not wait on its ancestor, it locks one "
         "level below", (c1.poll(), c1.holder.nest), (True, h.path + ".nest.nest"))
    same("nested chain: FAN-OUT, two children of one nested run do not run together",
         c2.poll(), False)
    c1.leave()
    same("nested chain: the second child starts when the first ends", c2.poll(), True)
    c2.leave()
    # The foreign name is as long as the holder's, so only the prefix comparison rejects it.
    foreign = "9" + os.path.basename(h.path)[1:] + ".nest"
    other = plant(home, foreign, "", hold=True)
    bad = {"unlocked": h.path + ".nest.nest", "missing": h.path + ".nest.nest.nest",
           "not this holder's": os.path.join(home, "waiters", foreign)}
    tries = {k: pool.nested(4, "/wt/X", {**env, "GATEQ_NEST": v}, print) for k, v in bad.items()}
    same("nested chain: a GATEQ_NEST that is unlocked, missing or not this holder's is ignored: "
         "the run waits on its holder's own child lock", {k: t.poll() for k, t in tries.items()},
         dict.fromkeys(bad, False))
    n1.leave()
    same("nested chain: and takes that lock once it is free",
         (tries["unlocked"].poll(), tries["unlocked"].holder.nest), (True, h.path + ".nest"))
    tries["unlocked"].leave(); os.close(other); h.leave()


def poll_or_err(w):
    try:
        return w.poll()
    except Exception as e:
        return f"raised {type(e).__name__}"


def case_unreadable_ticket():
    if os.geteuid() == 0:
        print("  [ok] unreadable ticket: skipped, uid 0 ignores file modes")
        return
    with tempfile.TemporaryDirectory(prefix="gate-pool-home-") as tmp:
        home = os.path.join(tmp, "gq")
        pool = gp.Pool(home, cfg())
        fd = plant(home, "0000000001-held.ticket", "", hold=True)
        path = os.path.join(home, "waiters", "0000000001-held.ticket")
        os.chmod(path, 0)                  # held, yet it cannot be opened: unknown content
        w = pool.enter("gate", "gate", 1, "/wt/w")
        same("unreadable ticket: a flocked ticket that cannot be opened is a live unknown one, "
             "never absent: a higher seq does not start", poll_or_err(w), False)
        check("unreadable ticket: and it is not unlinked", os.path.exists(path))
        os.chmod(path, 0o600); os.close(fd); w.leave()


def case_strays():
    def fresh(tmp, name):
        pool = gp.Pool(os.path.join(tmp, name), cfg(budget=2))
        return pool, pool.enter("gate", "gate", 2, "/wt/a")
    with tempfile.TemporaryDirectory(prefix="gate-pool-home-") as tmp:
        pool, a = fresh(tmp, "dir")
        os.mkdir(os.path.join(pool.waiters, "0000000000-dir.ticket"))
        same("strays: a directory named *.ticket in waiters/ changes nothing",
             poll_or_err(a), True)
        a.leave()
        pool, a = fresh(tmp, "tmpdir")
        os.mkdir(os.path.join(pool.tmp, "stray"))
        same("strays: a directory in tmp/ does not stop the sweep", poll_or_err(a), True)
        a.leave()
        pool, a = fresh(tmp, "link")
        link = os.path.join(pool.waiters, "0000000000-link.ticket")
        os.symlink(a.path, link)
        same("strays: a symlink to a live ticket is not a second entry, so the ticket starts",
             poll_or_err(a), True)
        check("strays: and the symlink is left alone", os.path.islink(link))
        a.leave()


def case_git_isolation():
    check("git isolation: no GIT_ variable is in the environment at case time",
          [v for v in os.environ if v.startswith("GIT_")] == [])
    # The hook shape for real: GIT_DIR exported to a re-run of the fixture case. Unscrubbed, its
    # git init writes into that directory (or the case fails); scrubbed, the directory stays empty.
    with tempfile.TemporaryDirectory(prefix="gate-pool-gitdir-") as tmp:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--only", "worktree-key"],
                           capture_output=True, text=True,
                           env={**os.environ, "GIT_DIR": tmp, "GATE_POOL_SCRIPTS": SCRIPTS})
        same("git isolation: with GIT_DIR exported the fixture case still passes and the "
             "directory GIT_DIR names stays empty", (r.returncode, os.listdir(tmp)), (0, []))


def case_worktree_key():
    # A linked worktree that was MOVED, with a symlink left at the path git recorded: the one
    # shape where the recorded string and the resolved path differ on every platform.
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    with tempfile.TemporaryDirectory(prefix="gate-pool-wt-") as tmp:
        repo, wt, moved, plain = (os.path.join(tmp, n) for n in ("repo", "wt", "moved", "plain"))

        def git(*args):
            return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                                  text=True, env=env).stdout
        os.mkdir(repo); os.mkdir(plain)
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
            "--allow-empty", "-m", "init")
        git("worktree", "add", "-q", "--detach", wt)
        os.rename(wt, moved); os.symlink(moved, wt)
        recorded = [ln[len("worktree "):] for ln in git("worktree", "list", "--porcelain")
                    .splitlines() if ln.startswith("worktree ") and ln.endswith("/wt")]
        check("worktree key: the fixture's recorded string differs from the resolved path",
              len(recorded) == 1 and recorded[0] != os.path.realpath(moved))
        same("worktree key: a path that resolves to a registered worktree gets the string git "
             "RECORDED", [gp.worktree_key(moved)], recorded)
        same("worktree key: every spelling of one worktree gets the same key",
             [gp.worktree_key(wt)], recorded)
        same("worktree key: a directory that is no worktree is keyed by its own resolved path",
             gp.worktree_key(plain), os.path.realpath(plain))
        # The hook shape: GIT_DIR exported, naming ANOTHER repository. The harness scrubs its
        # own environment at import, so only setting it here proves the call site scrubs too.
        other = os.path.join(tmp, "other"); os.mkdir(other)
        subprocess.run(["git", "-C", other, "init", "-q"], check=True, capture_output=True,
                       env=env)
        os.environ["GIT_DIR"] = os.path.join(other, ".git")
        try:
            under_hook = [gp.worktree_key(moved)]
        finally:
            del os.environ["GIT_DIR"]
        same("worktree key: with GIT_DIR naming another repository the key is still the string "
             "git RECORDED for this worktree", under_hook, recorded)


# --- the runner, end to end ---------------------------------------------------------------------
_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
_SERIAL = itertools.count()                # a fresh name for every temp repo of one case


def runner_env(home, extra=None):
    """The environment of EVERY spawned runner. `home` (the pool root) is required; HOME is a
    temp directory; an inherited holder is dropped unless the case hands one over in `extra`."""
    env = {k: v for k, v in os.environ.items() if k not in ("GATEQ_HOLDER", "GATEQ_NEST")}
    env.update(_GIT_ENV, GATEQ_HOME=home, HOME=_FAKE_HOME)
    env.update(extra or {})
    return env


def run_runner(root, home, *args, runner=None, env=None):
    """Run the runner to its end in `root` -> (exit code, stdout, stderr)."""
    r = subprocess.run([sys.executable, "-B", runner or RUNNER, *args], cwd=root,
                       env=runner_env(home, env), capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout, r.stderr


def put(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def make_repo(tmp, name, gates=None):
    """A `git init`ed temp repo, with `gates` as its .gates.toml (None: no such file)."""
    root = os.path.join(tmp, name)
    subprocess.run(["git", "init", "-q", root], check=True, capture_output=True,
                   env={**os.environ, **_GIT_ENV})
    if gates is not None:
        put(os.path.join(root, ".gates.toml"), gates)
    return root


def e2e(fn):
    """Run a runner case with its own scratch directory (resolved, as git reports paths) and
    its own EMPTY pool home inside it."""
    def case():
        with tempfile.TemporaryDirectory(prefix="gate-pool-e2e-") as tmp:
            tmp = os.path.realpath(tmp)
            home = os.path.join(tmp, "gq"); os.mkdir(home, 0o700)
            try:
                fn(tmp, home)
            finally:
                reap_all()                 # nothing a case started outlives it
    return case


# The four gate forms. A soft failure and a skip ride in the Form B fixtures so the goldens pin
# more than the happy line.
_STEPS = """
[[prep_pr.steps]]
name = "one"
run = "echo one-ran"
[[prep_pr.steps]]
name = "soft"
run = "echo soft-ran; echo soft-err >&2; exit 3"
required = false
[[prep_pr.steps]]
name = "absent"
run = "echo never"
skip_if_absent = "gate-pool-no-such-tool"
[[prep_pr.steps]]
name = "two"
run = "echo two-ran"
"""
FORMS = {
    "form-a": '[prep_pr]\ngate = "echo form-a-ran"\n',
    "form-b-serial": "[prep_pr]\n" + _STEPS,
    "form-b-parallel": "[prep_pr]\njobs = 2\n" + _STEPS,
    "fallback": None,
}
# RECORDED FROM THE PRE-POOL RUNNER (main at eaf6900) and committed before gate-runner.py was
# touched: (exit code, stdout, stderr), durations normalized to N.Ns, the repo root to <ROOT>.
_B_TAIL = ("[FAIL] soft (exit 3, N.Ns)\n[WARN] soft: soft failure (required=false), continuing.\n"
           "[SKIP] absent: gate-pool-no-such-tool not on PATH\ntwo-ran\n[PASS] two (exit 0, N.Ns)\n"
           "gate-runner: all required steps passed (1 soft failure(s) warned, not blocking).\n")
GOLDEN = {
    "form-a": (0, "gate-runner: using <ROOT>/.gates.toml\n"
                  "gate-runner: .gates.toml Form A (delegate) -> 'echo form-a-ran'\n"
                  "form-a-ran\n[PASS] gate (exit 0, N.Ns)\n", ""),
    # A serial step writes straight to the runner's stderr; a parallel step's is captured into
    # its block. The goldens pin that difference too.
    "form-b-serial": (0, "gate-runner: using <ROOT>/.gates.toml\n"
                         "gate-runner: .gates.toml Form B (enumerate) -> 4 step(s)\n"
                         "one-ran\n[PASS] one (exit 0, N.Ns)\nsoft-ran\n" + _B_TAIL, "soft-err\n"),
    "form-b-parallel": (0, "gate-runner: using <ROOT>/.gates.toml\n"
                           "gate-runner: .gates.toml Form B (enumerate) -> 4 step(s), jobs=2\n"
                           "one-ran\n[PASS] one (exit 0, N.Ns)\nsoft-ran\nsoft-err\n" + _B_TAIL, ""),
    "fallback": (0, "gate-runner: no .gates.toml found; entering fail-open fallback chain.\n"
                    "gate-runner: fallback layer 2 (CLAUDE.md ## Gates) -> 1 command(s).\n"
                    "fallback-ran\n[PASS] echo fallback-ran (exit 0, N.Ns)\n"
                    "gate-runner: all CLAUDE.md gate commands passed.\n", ""),
}


def form_repo(tmp, form, name=None, gates=None):
    root = make_repo(tmp, name or form, FORMS[form] if gates is None else gates)
    if FORMS[form] is None:                # the fallback chain: layer 2, CLAUDE.md's Gates block
        put(os.path.join(root, "CLAUDE.md"), "# x\n\n## Gates\n\n```sh\necho fallback-ran\n```\n")
    return root


def normalized(root, rc, out, err):
    def norm(text):
        return re.sub(r"\b[0-9]+\.[0-9]s", "N.Ns", text.replace(root, "<ROOT>"))
    return rc, norm(out), norm(err)


def golden_runs(label, tmp, home, runner=None):
    """Every form against the pool root `home`: output and exit code must EQUAL the golden, and
    the run must leave the pool root exactly as it found it."""
    before = sorted(os.listdir(home))
    for form in FORMS:
        root = form_repo(tmp, form, f"{form}-{next(_SERIAL)}")
        same(f"{label}: {form} output and exit code equal the pre-pool golden",
             normalized(root, *run_runner(root, home, runner=runner)), GOLDEN.get(form))
    same(f"{label}: nothing was created under the pool root", sorted(os.listdir(home)), before)


@e2e
def case_off_golden(tmp, home):
    golden_runs("off golden: no config.toml", tmp, home)


@e2e
def case_off_runner_alone(tmp, home):
    # The no-import proof: gate-runner.py copied ALONE, with no gate_pool.py beside it.
    alone = os.path.join(tmp, "alone"); os.mkdir(alone)
    runner = shutil.copy(RUNNER, alone)
    golden_runs("off runner alone: no config.toml", tmp, home, runner)
    put(os.path.join(home, "config.toml"), "[pool]\nprotocol = 1\n")
    golden_runs("off runner alone: a [pool] table with no budget", tmp, home, runner)
    same("off runner alone: the runner's directory holds the runner and nothing else",
         os.listdir(alone), ["gate-runner.py"])


# --- the runner with a budget configured --------------------------------------------------------
# NO SLEEP IS A SYNCHRONIZATION. A case waits for FACTS with a bounded deadline (`wait_for`): a
# step's started file, a wait line on stderr, a process exit. A step that must stay running
# blocks on a FIFO until the case opens it. "The third gate waits" is a STATE assertion (its
# ticket says waiting, its step never started) made while the holders are provably blocked.
# A production waiter notices a release on its next 1 s poll, so each such hand-over costs about
# a second; no test-only seam shortens it.
CONFIG = "[pool]\nprotocol = 1\nbudget = 10\n"
_LIVE = []                                 # every process a case started, for its cleanup


def load_runner():
    """The runner as a module, for the two pure-ish functions a spawn would table-test slowly."""
    spec = importlib.util.spec_from_file_location("_gate_runner", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@contextlib.contextmanager
def pool_home_env(home):
    """GATEQ_HOME for an IN-PROCESS call of the runner's config reader; the pin comes back."""
    os.environ["GATEQ_HOME"] = home
    try:
        yield
    finally:
        os.environ["GATEQ_HOME"] = _PINNED_HOME


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def wait_for(what, fact, *procs, timeout=60.0):
    """Wait for a FACT. The short sleep only paces the checks. A deadline reached, or a watched
    process that exited first, raises: the case fails by name instead of hanging."""
    end = time.monotonic() + timeout
    while not fact():
        if any(p.poll() is not None for p in procs) and not fact():
            raise AssertionError(f"a process exited before: {what}")
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for: {what}")
        time.sleep(0.02)


# NO TEST PROCESS MAY DIE OF SIGQUIT: the kernel reports that as a crash (on macOS a crash
# report and a dialog per process). The one case that sends SIGQUIT runs the runner through
# this driver: SIGQUIT has a do-nothing handler before main() starts (so a runner that fails
# to handle it survives it), and the final "die of the signal" step is replaced by its
# documented fallback status, 128 + the signal, FOR SIGQUIT ALONE. The real `_die_of` runs for
# SIGTERM and SIGHUP, whose default action is a plain termination. A case may also hand the
# driver a PATCH (python source, run with `mod` bound, before main()) to open a window no
# outside process can hit by timing: see LAUNCH_SIG_PATCH.
QUIT_SAFE_DRIVER = """
import importlib.util, os, signal, sys
signal.signal(signal.SIGQUIT, lambda signum, frame: None)
spec = importlib.util.spec_from_file_location("_gate_runner", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
_real_die = mod._die_of
mod._die_of = lambda sig: 128 + sig if sig == signal.SIGQUIT else _real_die(sig)
exec(os.environ.get("GATE_POOL_PATCH", ""))
sys.exit(mod.main(sys.argv[2:]))
"""


def spawn(root, home, *args, env=None, stdin=None, session=True, quit_safe=False, patch=None,
          ignore=()):
    """Start a runner in its own session (so its pid is its process group), its output in
    files (`.out`, `.err`). `stdin` is a file to read from; the default is /dev/null.
    `session=False` gives it its own GROUP inside this harness's session instead: a group whose
    leader's parent shares its session is not orphaned, so SIGTSTP can stop it.
    `quit_safe=True` runs it through QUIT_SAFE_DRIVER; so does `patch` (source the driver
    runs before main()). `ignore` names signals the runner INHERITS as ignored."""
    head = [sys.executable, "-B", "-c", QUIT_SAFE_DRIVER, RUNNER] if quit_safe or patch else \
        [sys.executable, "-B", RUNNER]
    if patch:
        env = {**(env or {}), "GATE_POOL_PATCH": patch}

    def pre():
        # A harness started with SIGINT ignored must not hand that to the interrupt cases.
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        for s in ignore:
            signal.signal(s, signal.SIG_IGN)
    base = os.path.join(os.path.dirname(home), f"run{next(_SERIAL)}")
    with open(base + ".out", "wb") as out, open(base + ".err", "wb") as err, \
            open(stdin or os.devnull, "rb") as inp:
        p = subprocess.Popen(
            [*head, *args], cwd=root, env=runner_env(home, env),
            stdin=inp, stdout=out, stderr=err, start_new_session=session,
            process_group=None if session else 0, preexec_fn=pre)
    p.out, p.err = base + ".out", base + ".err"
    _LIVE.append(p)
    return p


def _stop(p, sig):
    with contextlib.suppress(OSError):
        if hasattr(p, "err"):              # a runner: its whole session
            os.killpg(p.pid, sig)
        else:
            p.kill()


def reap_all():
    """Every exit path of a runner case: nothing a case started outlives it."""
    for p in _LIVE:
        if p.poll() is None:
            _stop(p, signal.SIGTERM)       # TERM first: a parallel runner kills its own steps
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _stop(p, signal.SIGKILL)
                p.wait()
        for stream in (p.stdin, p.stdout):
            if stream:
                stream.close()
    del _LIVE[:]


def hold(home, worktree, cost=10, budget=10):
    """A helper child HOLDING `cost` of the budget in `worktree`; returns once it does."""
    child = subprocess.Popen([sys.executable, "-B", "-c", HOLD_CHILD, SCRIPTS, home, worktree,
                              str(cost), str(budget)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    _LIVE.append(child)
    if child.stdout.readline() != "held\n":
        raise AssertionError("the helper child did not get its slots")
    return child


def drop(child):
    """The holder's process ends WITHOUT releasing: the kernel frees its locks, and wait()
    returns only after that."""
    child.stdin.close()
    child.wait(timeout=60)


def gated(tmp, tag):
    """A step that records it started, then blocks until the case lets it go ->
    (run string, started file, fifo)."""
    started, fifo = os.path.join(tmp, tag + ".started"), os.path.join(tmp, tag + ".fifo")
    os.mkfifo(fifo)
    return f"echo x > '{started}'; cat '{fifo}' > /dev/null", started, fifo


def _open_for_write(fifo):
    try:                                   # ENXIO until a reader has the FIFO open
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        return True
    except OSError:
        return False


def let_go(fifo):
    wait_for(f"a reader on {os.path.basename(fifo)}", lambda: _open_for_write(fifo))


def one_step(run, head=""):
    return f'[prep_pr]\n{head}\n[[prep_pr.steps]]\nname = "s"\nrun = "{run}"\n'


def tickets(home):
    return [_load_json(os.path.join(home, "waiters", n)) for n in listing(home, suffix=".ticket")]


def _load_json(path):
    try:
        return json.loads(read(path))
    except ValueError:
        return {}


def commit(root, msg):
    env = {**os.environ, **_GIT_ENV}
    subprocess.run(["git", "-C", root, "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "-m", msg], check=True, capture_output=True, env=env)
    return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


@e2e
def case_config_table(tmp, home):
    gr, path = load_runner(), os.path.join(home, "config.toml")

    def cfg_of(body):
        if body is not None:
            put(path, body)
        with pool_home_env(home):
            return gr._pool_config()

    def error(reason):
        return "error", f"{path}: {reason}"
    same("config: no config.toml is OFF", cfg_of(None), ("off", None))
    same("config: a file with no [pool] table is OFF, and an unknown TABLE is ignored",
         cfg_of("[queue]\nbudget = 3\n"), ("off", None))
    same("config: a [pool] table with no budget is OFF, and says so for doctor",
         cfg_of("[pool]\nprotocol = 1\nwait_timeout_s = 5\n"), ("off", "pool-table-without-budget"))
    full = {k: n for n, k in enumerate(gr.POOL_KEYS, 1)}
    same("config: all six keys beside another table are ON, as the validated [pool] table",
         cfg_of("[pool]\n" + "".join(f"{k} = {v}\n" for k, v in full.items()) + "[queue]\nx = 0\n"),
         ("on", full))
    wrong = {}
    for key in gr.POOL_KEYS:               # one rule for all six: a positive integer
        for value in ("0", "-1", "true", '"3"', "1.5"):
            body = "[pool]\n" + "".join(
                f"{k} = {v}\n" for k, v in {"protocol": 1, "budget": 10, key: value}.items())
            if cfg_of(body) != error(f"[pool].{key} must be a positive integer"):
                wrong[f"{key} = {value}"] = cfg_of(body)
    same("config: every key refuses zero, a negative, a bool, a string and a float, naming "
         "the key", wrong, {})
    same("config: a misspelled key (budegt) is an error naming it, never 'no budget'",
         cfg_of("[pool]\nprotocol = 1\nbudegt = 10\n"), error("unknown key [pool].budegt"))
    same("config: [pool] that is not a table is an error",
         cfg_of('pool = "on"\n'), error("[pool] must be a table"))
    same("config: a budget with no protocol is an error",
         cfg_of("[pool]\nbudget = 10\n"), error("[pool].budget needs [pool].protocol"))
    state, message = cfg_of("[pool\nbudget = 10\n")
    check("config: a file that does not parse is an error naming the file",
          state == "error" and message.startswith(path + ": "))
    os.environ["GATEQ_HOME"] = ""
    try:
        same("config: an empty GATEQ_HOME reads as unset", gr._pool_home(),
             os.path.join(os.path.expanduser("~"), ".claude", "gate-queue"))
    finally:
        os.environ["GATEQ_HOME"] = _PINNED_HOME
    same("config: reading the config created nothing", os.listdir(home), ["config.toml"])


@e2e
def case_cost_table(tmp, home):
    gr = load_runner()

    def cost(gates, jobs=None, budget=10):
        root = os.path.join(tmp, f"cost{next(_SERIAL)}"); os.mkdir(root)
        if gates is not None:
            put(os.path.join(root, ".gates.toml"), gates)
        return gr._pool_cost(root, jobs, budget)[0]
    b = "[prep_pr]\n{}\n" + _STEPS         # Form B with a head line
    same("cost: weight wins over jobs and over --jobs",
         (cost(b.format("jobs = 2\nweight = 4")), cost(b.format("weight = 4"), "8")), (4, 4))
    same("cost: a weight above the budget is clamped to it", cost(b.format("weight = 12")), 10)
    same("cost: with no weight, Form B costs its effective jobs (--jobs wins), clamped",
         (cost(b.format("jobs = 4")), cost(b.format("jobs = 4"), "2"), cost(b.format(""), "1"),
          cost(b.format("jobs = 16"))), (4, 2, 1, 10))
    same("cost: no weight and no jobs is the WHOLE budget (bare Form B, Form A, the fallback "
         "chain), and jobs buys Form A and the fallback chain nothing",
         (cost(b.format("")), cost(FORMS["form-a"]), cost(None), cost(FORMS["form-a"], "3"),
          cost('[prep_pr]\njobs = 3\ngate = "true"\n'), cost(None, "3"), cost(None, budget=7)),
         (10, 10, 10, 10, 10, 10, 7))
    rejected = {"--jobs 0": cost(b.format(""), "0"), "--jobs x": cost(b.format(""), "x"),
                "unparseable": cost("[prep_pr\n"), "no [prep_pr]": cost("[other]\nx = 1\n"),
                "gate and steps": cost('[prep_pr]\ngate = "true"\nsteps = []\n'),
                "jobs = 0": cost(b.format("jobs = 0")), "weight = 0": cost(b.format("weight = 0")),
                "weight = true": cost(b.format("weight = true")),
                'weight = "4"': cost(b.format('weight = "4"'))}
    same("cost: a config the run rejects anyway has NO cost (it takes no ticket)",
         rejected, dict.fromkeys(rejected))
    # The bytes judged invalid are the ones rejected, whatever the file says by then.
    root = form_repo(tmp, "form-a", "raw")
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        rc = gr._run_gates(root, None, raw=b"[prep_pr\n")[0]
    same("cost: the config judged while costing is the one rejected, not a later rewrite", rc, 2)


@e2e
def case_weight(tmp, home):                # `home` stays empty: the pool is OFF throughout
    got = {}
    for bad in ("0", "true", '"4"', "2.5"):
        root = make_repo(tmp, f"w{next(_SERIAL)}", f"[prep_pr]\nweight = {bad}\n" + _STEPS)
        rc, out, err = run_runner(root, home)
        got[bad] = (rc, err, "one-ran" in out)
    same("weight: with the pool OFF a weight that is no positive integer exits 2 and runs no "
         "step", got,
         dict.fromkeys(got, (2, "WARN: [prep_pr].weight must be a positive integer\n", False)))
    root = form_repo(tmp, "form-b-serial", "weight-ok", "[prep_pr]\nweight = 3\n" + _STEPS)
    same("weight: with the pool OFF a valid weight changes no output (the serial golden)",
         normalized(root, *run_runner(root, home)), GOLDEN["form-b-serial"])
    same("weight: and nothing was created under the pool root", os.listdir(home), [])


@e2e
def case_e2e_refusals(tmp, home):
    path, marker = os.path.join(home, "config.toml"), os.path.join(tmp, "ran")
    receipt = os.path.join(tmp, "receipt.json")
    root = make_repo(tmp, "r", one_step(f"echo x > '{marker}'"))

    def refused_by(body, runner=None):
        """-> (exit code, stderr, a step ran, a stale receipt survived, the pool root)."""
        put(path, body); put(receipt, '{"result": "pass"}\n')
        rc, out, err = run_runner(root, home, "--receipt", receipt, runner=runner)
        return rc, err, os.path.exists(marker), os.path.exists(receipt), sorted(os.listdir(home))

    def not_run(line):
        return 2, f"gate-runner: NOT RUN - {line}\n", False, False, ["config.toml"]
    for label, body, reason in (
            ("[pool] that is not a table", 'pool = "on"\n', "[pool] must be a table"),
            ("a misspelled key (budegt)", "[pool]\nprotocol = 1\nbudegt = 10\n",
             "unknown key [pool].budegt"),
            ("a zero budget", "[pool]\nprotocol = 1\nbudget = 0\n",
             "[pool].budget must be a positive integer"),
            ("a bool in a key a later PR first uses", CONFIG + "job_timeout_s = true\n",
             "[pool].job_timeout_s must be a positive integer"),
            ("a budget with no protocol", "[pool]\nbudget = 10\n",
             "[pool].budget needs [pool].protocol")):
        same(f"refusals: {label} exits 2 naming it: no step, no ticket, no stale receipt",
             refused_by(body), not_run(f"{path}: {reason}"))
    rc, err, *rest = refused_by("[pool\n")
    same("refusals: a config that does not parse exits 2 the same way",
         (rc, err.startswith(f"gate-runner: NOT RUN - {path}: "), err.count("\n"), rest),
         (2, True, 1, [False, False, ["config.toml"]]))
    if os.geteuid() != 0:                  # uid 0 reads through any mode
        put(path, CONFIG); os.chmod(path, 0)
        rc, out, err = run_runner(root, home)
        os.chmod(path, 0o600)
        same("refusals: a config that cannot be read exits 2, never 'no budget'",
             (rc, err.startswith(f"gate-runner: NOT RUN - {path}: "), os.path.exists(marker)),
             (2, True, False))
    same("refusals: a config of another pool protocol exits 2 and the directory is untouched",
         refused_by("[pool]\nprotocol = 2\nbudget = 10\n"),
         not_run("pool protocol 1 != configured 2; update the plugin and run configure --apply"))
    os.chmod(home, 0o750)
    try:
        same("refusals: a pool root open to the group exits 2",
             refused_by(CONFIG), not_run(f"pool root {home} must be owned by you with mode 0700"))
    finally:
        os.chmod(home, 0o700)
    alone = os.path.join(tmp, "alone"); os.mkdir(alone)
    same("refusals: a budget configured and no gate_pool.py beside the runner exits 2, never "
         "an unpooled run", refused_by(CONFIG, shutil.copy(RUNNER, alone)),
         not_run("pool configured but gate_pool.py is missing"))
    put(path, "[queue]\nbudegt = 1\n" + CONFIG)
    same("refusals: an unknown TABLE beside [pool] is ignored: the gate runs, pooled, silently",
         (run_runner(root, home)[::2], os.path.exists(marker), "waiters" in os.listdir(home),
          listing(home)), ((0, ""), True, True, []))
    os.unlink(marker)                      # the pooled run above left its marker and its dirs
    for n in os.listdir(home):
        if n != "config.toml":
            (shutil.rmtree if os.path.isdir(os.path.join(home, n)) else os.unlink)(
                os.path.join(home, n))
    same("refusals: a top-level budget (outside [pool]) exits 2 saying where it belongs",
         refused_by("budget = 10\nprotocol = 1\n"),
         not_run(f"{path}: top-level `budget` belongs under [pool]"))

    def refused_at(h):
        put(receipt, '{"result": "pass"}\n')
        rc, out, err = run_runner(root, h, "--receipt", receipt)
        return rc, err, os.path.exists(marker), os.path.exists(receipt)
    os.unlink(path); gone = os.path.join(tmp, "nowhere"); dangling = os.path.join(tmp, "dangling")
    os.symlink(gone, path); os.symlink(gone, dangling)
    same("refusals: a dangling config.toml or a dangling pool home exits 2, never 'off': no "
         "step, no stale receipt",
         (refused_at(home), refused_at(dangling)),
         ((2, f"gate-runner: NOT RUN - {path}: is a dangling symlink\n", False, False),
          (2, f"gate-runner: NOT RUN - {dangling}: is a dangling symlink\n", False, False)))
    os.unlink(path); os.unlink(dangling)
    empty, valid = os.path.join(tmp, "empty"), os.path.join(tmp, "valid")
    os.mkdir(empty, 0o700); os.symlink(empty, valid)
    same("refusals: a pool home that is a symlink to a real directory with no config is OFF: "
         "the gate runs unpooled and nothing is created there",
         (run_runner(root, valid)[::2], os.path.exists(marker), os.listdir(empty)),
         ((0, ""), True, []))
    os.unlink(marker)
    put(path, CONFIG); put(os.path.join(home, "waiters"), "x")
    rc, err, ran, kept = refused_at(home)
    os.unlink(os.path.join(home, "waiters"))
    same("refusals: a pool home whose waiters is a regular file exits 2, never unpooled",
         (rc, err.startswith("gate-runner: NOT RUN - pool error: "), ran, kept),
         (2, True, False, False))
    broken = os.path.join(tmp, "broken"); os.mkdir(broken)
    put(os.path.join(broken, "gate_pool.py"), "def (\n")
    rc, err, ran, kept, _ = refused_by(CONFIG, shutil.copy(RUNNER, broken))
    same("refusals: a gate_pool.py with a syntax error exits 2 in one line, never a traceback",
         (rc, err.startswith("gate-runner: NOT RUN - pool error: SyntaxError: "),
          err.count("\n"), ran, kept), (2, True, 1, False, False))
    linked = os.path.join(tmp, "linked"); os.mkdir(linked)
    os.symlink(RUNNER, os.path.join(linked, "gate-runner.py"))
    put(path, CONFIG)
    rc, out, err = run_runner(root, home, runner=os.path.join(linked, "gate-runner.py"))
    same("refusals: a runner reached through a symlink finds gate_pool.py beside the real file",
         (rc, "is missing" in err, os.path.exists(marker)), (0, False, True))
    if os.geteuid() != 0:                  # uid 0 reads through any mode
        os.unlink(marker); put(path, CONFIG + "wait_timeout_s = 1\n")
        holder = hold(home, "/wt/elsewhere"); gates = os.path.join(root, ".gates.toml")
        os.chmod(gates, 0)
        rc, out, err = run_runner(root, home)
        os.chmod(gates, 0o600); drop(holder)
        same("refusals: a .gates.toml that cannot be read still takes a ticket at the whole "
             "budget (waits, exits 75), never an unpooled run",
             (rc, "waiting for 10 of 10" in err, os.path.exists(marker)), (75, True, False))


@e2e
def case_e2e_rejected_costing(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    off = os.path.join(tmp, "off"); os.mkdir(off)
    for label, gates, args in (
            ("a bad --jobs", FORMS["form-b-serial"], ["--jobs", "0"]),
            ("an unparseable .gates.toml", "[prep_pr\n", []),
            ("no [prep_pr] table", "[other]\nx = 1\n", []),
            ("both gate and steps", '[prep_pr]\ngate = "echo no"\nsteps = []\n', []),
            ("a bad jobs", "[prep_pr]\njobs = 0\n" + _STEPS, []),
            ("a bad weight", "[prep_pr]\nweight = 0\n" + _STEPS, [])):
        root = make_repo(tmp, f"rej{next(_SERIAL)}", gates)
        on = normalized(root, *run_runner(root, home, *args))
        same(f"rejected while costing: {label} exits 2 with the pool-off output, no step and "
             "no ticket",
             (on[0], on == normalized(root, *run_runner(root, off, *args)), "-ran" in on[1],
              os.listdir(home)), (2, True, False, ["config.toml"]))
    fwd = os.path.join(tmp, "fwd-ran")
    good, here, sink = make_repo(tmp, "fwd", one_step(f"echo x > '{fwd}'")), os.getcwd(), io.StringIO()
    mod = load_runner(); mod._pool_cost = lambda root, jobs, budget: (None, b"[prep_pr\n")
    os.chdir(good)
    try:
        with pool_home_env(home), contextlib.redirect_stderr(sink), \
                contextlib.redirect_stdout(sink):
            rc = mod.main([])
    finally:
        os.chdir(here)
    same("rejected while costing: main() hands the bytes it costed to the run: the invalid "
         "config is rejected even though the file on disk is valid",
         (rc, os.path.exists(fwd)), (2, False))


@e2e
def case_e2e_three_gates(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    runs = []
    for tag in "abc":
        run, started, fifo = gated(tmp, tag)
        runs.append((make_repo(tmp, tag, one_step(run, "jobs = 4")), started, fifo))
    a, b = spawn(runs[0][0], home), spawn(runs[1][0], home)
    wait_for("the first two gates to start", lambda: all(os.path.exists(r[1]) for r in runs[:2]),
             a, b)
    c = spawn(runs[2][0], home)
    line = "gate-runner: waiting for 4 of 10 gate slots (2 free, 0 ahead)\n"
    wait_for("the third gate's wait line", lambda: line in read(c.err), c)
    same("three gates: two cost-4 gates run together in a budget of 10 and a third WAITS: its "
         "ticket says so, its step has not started, and it printed the one wait line",
         (sorted(t.get("state") for t in tickets(home)), os.path.exists(runs[2][1]), read(c.err)),
         (["running", "running", "waiting"], False, line))
    same("three gates: each ticket is a hand-run gate of cost 4 keyed by its own worktree",
         sorted((t.get("kind"), t.get("cost"), t.get("worktree")) for t in tickets(home)),
         sorted(("gate", 4, r[0]) for r in runs))
    let_go(runs[0][2])
    same("three gates: the first gate passes", a.wait(timeout=60), 0)
    wait_for("the third gate to start once slots are free", lambda: os.path.exists(runs[2][1]), c)
    let_go(runs[1][2]); let_go(runs[2][2])
    same("three gates: all pass, the pool is left empty, and a gate granted on its first poll "
         "printed nothing new",
         ([p.wait(timeout=60) for p in (b, c)], listing(home), read(a.err) + read(b.err)),
         ([0, 0], [], ""))


@e2e
def case_e2e_over_budget(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    run, started, fifo = gated(tmp, "big")
    repo = make_repo(tmp, "big", one_step(run, "weight = 12"))
    deep = os.path.join(repo, "deep", "er"); os.makedirs(deep)
    p = spawn(deep, home)                  # started two directories below the worktree root
    wait_for("the weight-12 gate to start", lambda: os.path.exists(started), p)
    same("over budget: a gate started from a SUBDIRECTORY is keyed by its worktree ROOT",
         [t.get("worktree") for t in tickets(home)], [repo])
    small = gp.Pool(home, cfg()).enter("gate", "gate", 1, "/wt/small")
    same("over budget: a weight above the budget runs, holding the WHOLE budget: nothing fits "
         "beside it",
         (small.poll(), small.status(), [t.get("cost") for t in tickets(home)
                                         if t.get("state") == "running"]),
         (False, "gate-runner: waiting for 1 of 10 gate slots (0 free, 0 ahead)", [10]))
    let_go(fifo)
    same("over budget: and gives the whole budget back when it ends",
         (p.wait(timeout=60), small.poll()), (0, True))
    small.leave()


@e2e
def case_e2e_undeclared(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    run, started, fifo = gated(tmp, "umbrella")
    a = spawn(make_repo(tmp, "form-a", f'[prep_pr]\ngate = "{run}"\n'), home)
    wait_for("the Form A gate to start", lambda: os.path.exists(started), a)
    b = spawn(make_repo(tmp, "bare-b", one_step("echo second-ran")), home)
    line = "gate-runner: waiting for 10 of 10 gate slots (0 free, 0 ahead)\n"
    wait_for("the second gate's wait line", lambda: line in read(b.err), b)
    same("undeclared cost: a Form A gate holds the whole budget, and a Form B gate declaring "
         "neither weight nor jobs waits for all of it",
         (read(b.err), "second-ran" in read(b.out)), (line, False))
    let_go(fifo)
    same("undeclared cost: the second runs alone once the first ends",
         (a.wait(timeout=60), b.wait(timeout=60), "second-ran" in read(b.out)), (0, 0, True))


@e2e
def case_e2e_timeout(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG + "wait_timeout_s = 1\n")
    hold(home, "/wt/elsewhere")
    marker = os.path.join(tmp, "ran")
    receipt = put(os.path.join(tmp, "receipt.json"), '{"result": "pass"}\n')
    root = make_repo(tmp, "t", one_step(f"echo x > '{marker}'", "jobs = 2"))
    same("timeout: a wait that outlasts wait_timeout_s exits 75, NOT RUN",
         run_runner(root, home, "--receipt", receipt),
         (75, "", "gate-runner: waiting for 2 of 10 gate slots (0 free, 0 ahead)\n"
                  "gate-runner: NOT RUN - no gate slot within 1s\n"))
    same("timeout: no step ran, the waiter's ticket is gone, and no older receipt is left to "
         "read as this run's",
         (os.path.exists(marker), len(listing(home, suffix=".ticket")), os.path.exists(receipt)),
         (False, 1, False))


@e2e
def case_e2e_interrupt(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    hold(home, "/wt/elsewhere")
    marker = os.path.join(tmp, "ran")
    receipt = put(os.path.join(tmp, "receipt.json"), '{"result": "pass"}\n')
    p = spawn(make_repo(tmp, "i", one_step(f"echo x > '{marker}'", "jobs = 2")), home,
              "--receipt", receipt)
    wait_for("the wait line", lambda: "waiting for 2 of 10" in read(p.err), p)
    os.kill(p.pid, signal.SIGINT)
    same("interrupt: an interrupt while WAITING exits 130 with one line; no step ran, the "
         "ticket and the stale receipt are gone",
         (p.wait(timeout=60), read(p.err).splitlines()[1:], os.path.exists(marker),
          len(listing(home, suffix=".ticket")), os.path.exists(receipt)),
         (130, ["gate-runner: NOT RUN - interrupted while waiting for a gate slot"], False, 1,
          False))


@e2e
def case_e2e_budget_removed(tmp, home):
    config = put(os.path.join(home, "config.toml"), CONFIG)
    holder = hold(home, "/wt/elsewhere")
    waited, fresh = os.path.join(tmp, "waited"), os.path.join(tmp, "fresh")
    w = spawn(make_repo(tmp, "w", one_step(f"echo x > '{waited}'", "jobs = 2")), home)
    wait_for("the wait line", lambda: "waiting for 2 of 10" in read(w.err), w)
    os.unlink(config)
    before = listing(home)
    rc, out, err = run_runner(make_repo(tmp, "n", one_step(f"echo x > '{fresh}'", "jobs = 2")),
                              home)
    same("budget removed: a gate started AFTER the removal runs at once, unpooled, and takes "
         "no ticket", (rc, err, os.path.exists(fresh), listing(home)), (0, "", True, before))
    same("budget removed: the gate already waiting is still waiting shortly after the unlink",
         (w.poll(), os.path.exists(waited)), (None, False))
    drop(holder)
    same("budget removed: and it starts when its cost fits",
         (w.wait(timeout=60), os.path.exists(waited)), (0, True))


@e2e
def case_e2e_gates_edited(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    holder = hold(home, "/wt/elsewhere")
    def1, def2 = os.path.join(tmp, "def1"), os.path.join(tmp, "def2")
    root = make_repo(tmp, "e", one_step(f"echo x > '{def1}'", "jobs = 2"))
    p = spawn(root, home)
    wait_for("the wait line", lambda: "waiting for 2 of 10" in read(p.err), p)
    put(os.path.join(root, ".gates.toml"), one_step(f"echo x > '{def2}'", "jobs = 2"))
    drop(holder)
    same("gates edited: a .gates.toml edited during the wait is re-read after the grant, and "
         "THAT definition runs",
         (p.wait(timeout=60), os.path.exists(def1), os.path.exists(def2)), (0, False, True))
    # The same edit RAISING the cost: the run holds 2 units and the new definition needs 8.
    def3, def4 = os.path.join(tmp, "def3"), os.path.join(tmp, "def4")
    wide = make_repo(tmp, "w", one_step(f"echo x > '{def3}'", "jobs = 2"))
    holder, receipt = hold(home, "/wt/elsewhere"), os.path.join(tmp, "old-receipt.json")
    put(receipt, '{"result": "pass"}\n')
    p = spawn(wide, home, "--receipt", receipt)
    wait_for("the wait line", lambda: "waiting for 2 of 10" in read(p.err), p)
    put(os.path.join(wide, ".gates.toml"), one_step(f"echo x > '{def4}'", "jobs = 8"))
    drop(holder)
    same("gates edited: a definition that costs MORE after the wait than the run holds is NOT "
         "RUN, exit 75: no step, no stale receipt, and its slots are given back",
         (p.wait(timeout=60), os.path.exists(def3), os.path.exists(def4), os.path.exists(receipt),
          read(p.err).splitlines()[-1], listing(home, suffix=".ticket")),
         (75, False, False, False, "gate-runner: NOT RUN - .gates.toml changed during the wait "
                                   "(cost 2 -> 8); run it again", []))


@e2e
def case_e2e_receipt(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    holder = hold(home, "/wt/elsewhere")
    root = make_repo(tmp, "r", one_step("true", "jobs = 2"))
    first, receipt = commit(root, "before the wait"), os.path.join(tmp, "receipt.json")
    p = spawn(root, home, "--receipt", receipt)
    wait_for("the wait line", lambda: "waiting for 2 of 10" in read(p.err), p)
    put(os.path.join(root, "new.txt"), "x\n")
    second = commit(root, "during the wait")
    drop(holder)
    rc, rec = p.wait(timeout=60), _load_json(receipt)
    same("receipt: the snapshot is taken AFTER the grant: the receipt binds the commit made "
         "during the wait, and passes",
         (rc, rec.get("commit_sha") == second != first, rec.get("result")), (0, True, "pass"))


GIT_STUB = """
import fcntl, os, sys
admit, log, real, args = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
state = "ABSENT"
try:
    fd = os.open(admit, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = "FREE"
    except BlockingIOError:
        state = "HELD"
    os.close(fd)
except FileNotFoundError:
    pass
with open(log, "a") as f:
    f.write(state + " " + " ".join(args) + "\\n")
os.execv(real, ["git"] + args)
"""


@e2e
def case_e2e_git_outside_lock(tmp, home):
    # Every git the runner starts goes through a stub that first asks: is admit.lock held?
    put(os.path.join(home, "config.toml"), CONFIG)
    bindir, log = os.path.join(tmp, "bin"), os.path.join(tmp, "git.log")
    os.mkdir(bindir)
    stub = put(os.path.join(tmp, "git-stub.py"), GIT_STUB)
    put(os.path.join(bindir, "git"),
        f"#!/bin/sh\nexec '{sys.executable}' -B '{stub}' '{os.path.join(home, 'admit.lock')}' "
        f"'{log}' '{shutil.which('git')}' \"$@\"\n")
    os.chmod(os.path.join(bindir, "git"), 0o755)
    root = make_repo(tmp, "g", one_step("true", "jobs = 2"))
    commit(root, "init")
    rc, out, err = run_runner(root, home, "--receipt", os.path.join(tmp, "receipt.json"),
                              env={"PATH": bindir + os.pathsep + os.environ["PATH"]})
    calls = read(log).splitlines()
    same("git outside the lock: the runner resolves its worktree key and takes its receipt "
         "snapshot through git, and NO git call runs while admit.lock is held",
         (rc, [c for c in calls if c.startswith("HELD")],
          any("worktree list" in c for c in calls), any(c.startswith("FREE") for c in calls)),
         (0, [], True, True))


HOLDER_STEP = """
echo "${GATEQ_HOLDER-unset}|${GATEQ_NEST-unset}" >> "$1/env"
if [ -z "$GATE_POOL_INNER" ]; then
  GATE_POOL_INNER=1 "$2" -B "$3" > "$1/inner.out" 2>&1
  echo $? > "$1/inner.rc"
else
  ls "$GATEQ_HOME/waiters" > "$1/inner.ls"
fi
"""


@e2e
def case_e2e_holder_env(tmp, home):
    # wait_timeout_s bounds the one way this case can go wrong: an inner run that waits on the
    # worktree its own outer gate holds.
    put(os.path.join(home, "config.toml"), CONFIG + "wait_timeout_s = 3\n")
    hold(home, "/wt/leak", cost=1)
    leaked = os.path.join(home, "waiters", listing(home, suffix=".ticket")[0])
    out_dir = os.path.join(tmp, "seen"); os.mkdir(out_dir)
    script = put(os.path.join(tmp, "step.sh"), HOLDER_STEP)
    root = make_repo(tmp, "h", one_step(
        f"sh '{script}' '{out_dir}' '{sys.executable}' '{RUNNER}'", "weight = 2"))
    rc, out, err = run_runner(root, home, env={"GATEQ_HOLDER": leaked, "GATEQ_NEST": "/leaked"})
    seen = read(os.path.join(out_dir, "env")).splitlines()
    own = seen[0].split("|")[0] if seen else ""
    same("holder env: a runner handed a holder from ANOTHER worktree says so once and takes "
         "its own ticket", (rc, err),
         (0, "gate-runner: ignoring GATEQ_HOLDER, its holder is in another worktree (/wt/leak); "
             "taking a ticket\n"))
    same("holder env: a holder's step sees the runner's OWN ticket in GATEQ_HOLDER, never the "
         "inherited one, and no GATEQ_NEST",
         (seen[:1], os.path.dirname(own), own.endswith(".ticket"), own != leaked),
         ([f"{own}|unset"], os.path.join(home, "waiters"), True, True))
    inner_ls = read(os.path.join(out_dir, "inner.ls")).split()
    same("holder env: an inner runner started by a step takes NO ticket: it runs under its "
         "holder, one lock below, and names both for its own steps",
         (read(os.path.join(out_dir, "inner.rc")), seen[1:],
          sum(n.endswith(".ticket") for n in inner_ls), os.path.basename(own) + ".nest" in inner_ls),
         ("0\n", [f"{own}|{own}.nest"], 2, True))


# --- the pooled serial step path (PR A2; doc section 4) -----------------------------------------
# A terminal signal goes to the FOREGROUND PROCESS GROUP. Every runner here is its own session
# and group leader (`spawn`), so `os.killpg(p.pid, sig)` is what a terminal does: with the pool
# off it reaches the runner AND its serial step (one group), with the pool on the runner alone,
# which must forward it. Every helper step ends itself after 60 s, so a mutant that loses one
# cannot leave it behind for long, and no case kills a pid it did not just see alive.
INFO_STEP = """
import json, os, sys
with open(sys.argv[1], "w") as f:
    json.dump({"pgrp": os.getpgrp(), "stdin": sys.stdin.read()}, f)
"""
SIG_STEP = """
import os, signal, sys
started, record = sys.argv[1:3]
got = []
def note(signum, frame):
    if got:                                # the first signal is the one recorded
        return
    got.append(signum)
    with open(record, "w") as f:
        f.write(signal.Signals(signum).name)
    os._exit(9)
for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
    signal.signal(s, note)
signal.alarm(60)
with open(started, "w") as f:
    f.write("x")
while True:
    signal.pause()
"""
TSTP_STEP = """
import json, os, signal, sys
started, record, fifo = sys.argv[1:4]
got = []
signal.signal(signal.SIGTSTP, lambda n, f: got.append("SIGTSTP"))
signal.alarm(60)
with open(started, "w") as f:
    f.write("x")
with open(fifo) as f:                      # blocks until the case lets the step go
    f.read()
with open(record, "w") as f:
    json.dump(got, f)
"""
# Records EVERY forwarded signal it gets (one name per line, appended) and ends itself, exit 9,
# after `n` of them. Its started file (its pid) is written AFTER the handlers are in: that is
# the handshake. With a fifo it blocks there until the case lets it go, then exits 0. SIGQUIT
# is not handled: no case sends it one.
REC_STEP = """
import os, signal, sys
started, record, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
fifo = sys.argv[4] if len(sys.argv) > 4 else None
got = []
def note(signum, frame):
    got.append(signum)
    with open(record, "a") as f:
        f.write(signal.Signals(signum).name + "\\n")
    if len(got) >= n:
        os._exit(9)
for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(s, note)
signal.alarm(60)
with open(started, "w") as f:
    f.write(str(os.getpid()))
if fifo:
    with open(fifo) as f:                  # blocks until the case lets the step go
        f.read()
    sys.exit(0)
while True:
    signal.pause()
"""
# Run by QUIT_SAFE_DRIVER inside the runner: right after the REAL Popen of a pooled step
# returns (the group exists, the forwarding handlers do not yet), the runner sends ITSELF
# SIGTERM. With the launch-window mask held the signal is pending and is delivered once the
# handlers are in; with no mask it kills the runner on the spot and the group is an orphan.
LAUNCH_SIG_PATCH = """
import subprocess as _sp
_Popen = _sp.Popen
class _SignalAtLaunch(_Popen):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        pidf = os.environ["GATE_POOL_STEP_PID"]
        if k.get("process_group") == 0 and not os.path.exists(pidf):
            with open(pidf, "w") as f:
                f.write(str(self.pid))
            os.kill(os.getpid(), signal.SIGTERM)
_sp.Popen = _SignalAtLaunch
"""
DIE_BY = ("SIGINT", "SIGTERM", "SIGHUP")   # compared pool on against pool off, to the death


def py_step(tmp, name, source, *args):
    """A step that runs a helper script, exec'd so the step's shell IS the script."""
    script = put(os.path.join(tmp, name), source)
    return "exec " + " ".join(f"'{a}'" for a in (sys.executable, script, *args))


def off_home(tmp):
    """A second, EMPTY pool root: the pool-off side of a comparison."""
    home = os.path.join(tmp, "off", "gq")
    os.makedirs(home, 0o700, exist_ok=True)
    return home


def ended(p, timeout=60):
    """The exit status, or a string when the runner did not end (a FAIL by name, not a hang)."""
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return "still running"


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                # somebody else's process now: ours is gone
        return False
    return True


def gone(pid, timeout=10.0):
    """True once `pid` is gone. A process just killed may be an unreaped zombie for an instant,
    so this waits for the fact, bounded; a survivor costs the bound and reads False."""
    end = time.monotonic() + timeout
    while alive(pid):
        if time.monotonic() > end:
            return False
        time.sleep(0.02)
    return True


def tail(p, root):
    """What a caller sees last: every stdout line (durations and the root normalized) and the
    last stderr line (an interrupt's traceback differs in its frames, never in that line)."""
    _, out, err = normalized(root, 0, read(p.out), read(p.err))
    return out, (err.splitlines() or [""])[-1]


@e2e
def case_serial_golden_on(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    for form in FORMS:
        root = form_repo(tmp, form, f"{form}-{next(_SERIAL)}")
        same(f"serial golden on: {form} with the pool ON prints and exits exactly as the "
             "pre-pool golden", normalized(root, *run_runner(root, home)), GOLDEN.get(form))
    same("serial golden on: and the pool is left empty", listing(home), [])


@e2e
def case_serial_group(tmp, home):
    typed = put(os.path.join(tmp, "typed"), "typed\n")
    seen = {}
    for label, pool_home in (("off", off_home(tmp)), ("on", home)):
        info = os.path.join(tmp, f"info-{label}.json")
        root = make_repo(tmp, f"g-{label}", one_step(py_step(tmp, "info.py", INFO_STEP, info)))
        if label == "on":
            put(os.path.join(home, "config.toml"), CONFIG)
        p = spawn(root, pool_home, stdin=typed)
        rec = (ended(p), _load_json(info))
        seen[label] = (rec[0], rec[1].get("pgrp") == p.pid, rec[1].get("stdin"))
    same("serial group: with the pool OFF a serial step shares the runner's process group and "
         "reads the runner's stdin, as before the pool", seen["off"], (0, True, "typed\n"))
    same("serial group: with the pool ON a serial step runs in a process group of its OWN",
         seen["on"][:2], (0, False))
    same("serial group: with the pool ON a serial step's stdin is /dev/null, not the runner's",
         seen["on"][2], "")


def _signalled(tmp, home, tag, sig, send, **kw):
    """One runner with one SIG_STEP step, signalled once the step runs ->
    (exit status, what the step recorded, stdout, last stderr line)."""
    started, record = os.path.join(tmp, tag + ".started"), os.path.join(tmp, tag + ".record")
    root = make_repo(tmp, tag, one_step(py_step(tmp, "sig.py", SIG_STEP, started, record)))
    p = spawn(root, home, **kw)
    wait_for(f"the {tag} step to start", lambda: os.path.exists(started), p)
    send(p.pid, sig)
    with contextlib.suppress(AssertionError):   # a lost signal is a FAIL of its check, by name
        wait_for("the step's record", lambda: read(record) != "", timeout=10)
    return (ended(p, timeout=20), read(record), *tail(p, root))


@e2e
def case_serial_signals(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    off, left = off_home(tmp), []
    for name in DIE_BY:
        sig = getattr(signal, name)
        # os.killpg on the runner's group is what a terminal does.
        seen = {label: _signalled(tmp, pool_home, f"{name}-{label}", sig, os.killpg)
                for label, pool_home in (("off", off), ("on", home))}
        same(f"serial signals: with the pool OFF {name} from the terminal reaches the step and "
             "ends the runner by that signal", seen["off"][:2], (-sig, name))
        same(f"serial signals: {name} reaches a pooled serial step: the runner forwards it to "
             "the step's group", seen["on"][1], name)
        same(f"serial signals: {name} ends the pooled runner with the pool-off exit status and "
             "final lines", (seen["on"][0], seen["on"][2:]), (seen["off"][0], seen["off"][2:]))
        left.append(listing(home))         # read now: a later runner would unlink a dead ticket
    same("serial signals: a runner ended by a forwarded signal gave its ticket back first",
         left, [[], [], []])
    # SIGQUIT: forwarding only, and to the runner's pid alone. No process here dies of it (see
    # QUIT_SAFE_DRIVER), so there is NO pool-off run and no die-by-SIGQUIT status to compare.
    rc, record, out, _ = _signalled(tmp, home, "SIGQUIT-on", signal.SIGQUIT, os.kill,
                                    quit_safe=True)
    same("serial signals: SIGQUIT reaches a pooled serial step: the runner forwards it to the "
         "step's group", record, "SIGQUIT")
    same("serial signals: after a forwarded SIGQUIT the runner prints no step verdict and goes "
         "to die of SIGQUIT (stubbed in this case: exit 128+3)",
         (rc, out), (128 + signal.SIGQUIT, seen["off"][2]))


def _pid_of(path):
    return int(read(path).strip() or 0)


@e2e
def case_serial_sweep(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    # (a) A step that ends normally and leaves a background job in its group.
    seen = {}
    for label, pool_home in (("off", off_home(tmp)), ("on", home)):
        pidf = os.path.join(tmp, f"bg-{label}.pid")
        p = spawn(make_repo(tmp, f"bg-{label}", one_step(f"sleep 60 & echo $! > '{pidf}'")),
                  pool_home)
        rc, pid = ended(p), _pid_of(pidf)
        if label == "off":
            seen[label] = (rc, alive(pid))
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)   # seen alive on the line above: ours
        else:
            seen[label] = (rc, gone(pid), "[PASS] s (exit 0" in read(p.out))
    same("serial sweep: with the pool OFF a step's background job outlives the runner, as "
         "before the pool", seen["off"], (0, True))
    same("serial sweep: with the pool ON a passing step's background job is swept before the "
         "runner ends", seen["on"], (0, True, True))
    # (b) An interrupt: the shell dies of the forwarded SIGINT, its background job ignores it
    # (a non-interactive shell starts `&` jobs that way; RUN F5) and goes with the sweep.
    pidf, started = os.path.join(tmp, "int.pid"), os.path.join(tmp, "int.started")
    p = spawn(make_repo(tmp, "int", one_step(
        f"sleep 60 & echo $! > '{pidf}'; echo x > '{started}'; wait")), home)
    wait_for("the interrupted step to start", lambda: os.path.exists(started), p)
    os.killpg(p.pid, signal.SIGINT)
    same("serial sweep: after a forwarded SIGINT the step's background job, which ignores "
         "SIGINT, is swept before the runner ends",
         (ended(p), gone(_pid_of(pidf))), (-signal.SIGINT, True))
    # (c) A step that ignores the forwarded signal AND the sweep's SIGTERM: the SIGKILL leg.
    pidf, started = os.path.join(tmp, "deaf.pid"), os.path.join(tmp, "deaf.started")
    p = spawn(make_repo(tmp, "deaf", one_step(
        f"trap '' TERM; echo $$ > '{pidf}'; echo x > '{started}'; sleep 60")), home)
    wait_for("the deaf step to start", lambda: os.path.exists(started), p)
    os.killpg(p.pid, signal.SIGTERM)
    same("serial sweep: a step that ignores the forwarded SIGTERM is killed with its group, and "
         "the runner still ends by SIGTERM",
         (ended(p), gone(_pid_of(pidf))), (-signal.SIGTERM, True))


def _kill_seen_alive(pid):
    """Cleanup after a FAILED check: a step a mutant left behind, seen alive just now."""
    if pid and alive(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def _rec(tmp, home, tag, n, tail="", fifo=None, **kw):
    """One runner with one REC_STEP step, returned once the step's handlers are in ->
    (runner, record file, the step's pid)."""
    started, record = os.path.join(tmp, tag + ".started"), os.path.join(tmp, tag + ".record")
    step = py_step(tmp, "rec.py", REC_STEP, started, record, str(n), *([fifo] if fifo else []))
    if tail:                               # NOT exec'd: the shell stays, the script is its child
        step = step[len("exec "):] + tail
    p = spawn(make_repo(tmp, tag, one_step(step)), home, **kw)
    wait_for(f"the {tag} step to start", lambda: read(started) != "", p)
    return p, record, _pid_of(started)


@e2e
def case_serial_launch_window(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    pidf = os.path.join(tmp, "launch.pid")
    p = spawn(make_repo(tmp, "launch", one_step("sleep 30")), home, patch=LAUNCH_SIG_PATCH,
              env={"GATE_POOL_STEP_PID": pidf})
    rc, pid = ended(p, timeout=30), _pid_of(pidf)
    same("serial launch window: a signal that lands between the step's Popen and the handler "
         "install is not lost: it is forwarded, the step's group is gone, the ticket is given "
         "back, and the runner ends by that signal",
         (rc, bool(pid) and gone(pid, 3.0), listing(home)), (-signal.SIGTERM, True, []))
    _kill_seen_alive(pid)


@e2e
def case_serial_two_signals(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    # (a) Two DIFFERENT signals during one step. The second is sent only once the step has
    # recorded the first, so the runner provably took them in this order.
    p, record, pid = _rec(tmp, home, "two", 2)
    os.kill(p.pid, signal.SIGTERM)
    with contextlib.suppress(AssertionError):
        wait_for("the first forwarded signal", lambda: read(record) != "", timeout=10)
    os.kill(p.pid, signal.SIGHUP)
    rc = ended(p, timeout=30)
    same("serial two signals: two different signals during one step are EACH forwarded, once",
         read(record).split(), ["SIGTERM", "SIGHUP"])
    same("serial two signals: the FIRST signal decides how the runner ends, and the ticket is "
         "given back", (rc, listing(home)), (-signal.SIGTERM, []))
    _kill_seen_alive(pid)
    # (b) A second signal DURING THE SWEEP. The step outlives every SIGTERM and records each:
    # its second record line is the sweep's own SIGTERM, the fact that the runner is in its
    # sweep (the SIGKILL comes KILL_GRACE_S later).
    p, record, pid = _rec(tmp, home, "insweep", 99)
    os.kill(p.pid, signal.SIGTERM)
    with contextlib.suppress(AssertionError):
        wait_for("the sweep's SIGTERM", lambda: len(read(record).split()) >= 2, timeout=15)
    in_sweep = read(record).split()
    os.kill(p.pid, signal.SIGHUP)
    same("serial two signals: a second signal during the sweep does not abort it: the step's "
         "group is gone when the runner ends, by the FIRST signal, its ticket given back",
         (in_sweep, ended(p, timeout=30), gone(pid, 3.0), listing(home)),
         (["SIGTERM", "SIGTERM"], -signal.SIGTERM, True, []))
    _kill_seen_alive(pid)


@e2e
def case_serial_ignored(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    fifo = os.path.join(tmp, "ign.fifo"); os.mkfifo(fifo)
    p, record, pid = _rec(tmp, home, "ign", 99, fifo=fifo, ignore=(signal.SIGHUP,))
    os.kill(p.pid, signal.SIGHUP)
    # No fact marks "the runner did NOT act": the bound gives a wrongly forwarded signal time
    # to reach the step. The exit status below does not depend on it.
    with contextlib.suppress(AssertionError):
        wait_for("a forwarded signal", lambda: read(record) != "", timeout=1.0)
    with contextlib.suppress(AssertionError):
        let_go(fifo)
    same("serial ignored: a signal the runner INHERITED as ignored stays ignored with the pool "
         "on: the step is not signalled, the gate passes, exit 0, no ticket left",
         (ended(p, timeout=30), read(record), read(p.out).splitlines()[-1:], listing(home)),
         (0, "", ["gate-runner: all steps passed."], []))
    _kill_seen_alive(pid)


@e2e
def case_serial_group_signal(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    # The step's shell is the group leader and the recording script is its CHILD.
    p, record, pid = _rec(tmp, home, "child", 1, tail="; true")
    os.kill(p.pid, signal.SIGHUP)
    rc = ended(p, timeout=30)
    same("serial group signal: a forwarded signal goes to the step's GROUP: a child the step's "
         "shell started without exec gets it too",
         (rc, read(record).split(), gone(pid, 3.0)), (-signal.SIGHUP, ["SIGHUP"], True))
    _kill_seen_alive(pid)


@e2e
def case_serial_tstp(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG)
    started, record = os.path.join(tmp, "tstp.started"), os.path.join(tmp, "tstp.record")
    fifo = os.path.join(tmp, "tstp.fifo"); os.mkfifo(fifo)
    root = make_repo(tmp, "tstp", one_step(py_step(tmp, "tstp.py", TSTP_STEP, started, record,
                                                   fifo)))
    # Not a session of its own: the kernel discards SIGTSTP's stop for an orphaned group.
    p = spawn(root, home, session=False)
    wait_for("the step to start", lambda: os.path.exists(started), p)
    os.kill(p.pid, signal.SIGTSTP)         # Ctrl-Z: the runner alone is in the foreground group

    def stopped():                         # this harness is the runner's parent, so it can ask
        pid, status = os.waitpid(p.pid, os.WUNTRACED | os.WNOHANG)
        return pid == p.pid and os.WIFSTOPPED(status)
    # THE HANDSHAKE: the runner being STOPPED is the fact that it has acted on the signal. A
    # runner that forwards it instead never stops, and the bound below is then what gives the
    # forwarded signal time to arrive: both checks fail, neither by luck.
    try:
        wait_for("the runner to stop", stopped, timeout=10)
        runner_stopped = True
    except AssertionError:
        runner_stopped = False
    # The step ends while the runner is still stopped. A step that is gone, or never records,
    # is a FAIL of the check below by name, not a crash of the case.
    with contextlib.suppress(AssertionError):
        wait_for("a reader on the fifo", lambda: _open_for_write(fifo), timeout=15)
        wait_for("the step's record", lambda: read(record) != "", timeout=15)
    seen = read(record)
    os.kill(p.pid, signal.SIGCONT)
    same("serial tstp: SIGTSTP stops the runner and is NOT forwarded: the step never sees it "
         "and runs on to its end while the runner is stopped", (runner_stopped, seen),
         (True, "[]"))
    same("serial tstp: continued, the runner ends with an ordinary pass",
         (ended(p), read(p.out).splitlines()[-1:]), (0, ["gate-runner: all steps passed."]))


# --- the hand-run timeout (PR A2; doc section 4, "The hand-run timeout") -------------------------
# The step outlasts job_timeout_s BY CONSTRUCTION (it sleeps 20 s against a 1 s bound); nothing
# here is ordered by a sleep. A mutant that never kills costs those 20 s and then fails by name.
_KILLED = "gate-runner: KILLED - exceeded job_timeout_s (1s)"


def _overrun(tmp, home, tag, head):
    """One gate whose only step outlasts the bound -> (exit status, last stderr line, the
    receipt's result, the step's shell gone, tickets left, stdout)."""
    pidf, receipt = os.path.join(tmp, tag + ".pid"), os.path.join(tmp, tag + ".receipt.json")
    root = make_repo(tmp, tag, one_step(f"echo $$ > '{pidf}'; sleep 20", head))
    commit(root, "a tree for the receipt to bind")
    put(receipt, '{"result": "pass"}\n')   # an older pass that must not survive
    p = spawn(root, home, "--receipt", receipt)
    rc = ended(p, timeout=40)
    pid = _pid_of(pidf)
    return (rc, (read(p.err).splitlines() or [""])[-1], _load_json(receipt).get("result"),
            bool(pid) and gone(pid), listing(home), read(p.out))


@e2e
def case_job_timeout(tmp, home):
    put(os.path.join(home, "config.toml"), CONFIG + "job_timeout_s = 1\n")
    rc, last, result, swept, left, out = _overrun(tmp, home, "serial", "")
    same("job timeout: a hand-run gate whose serial step outlasts job_timeout_s is KILLED: "
         "exit 1 and the KILLED line", (rc, last), (1, _KILLED))
    same("job timeout: the killed serial step's group is swept and the slots are given back",
         (swept, left), (True, []))
    same("job timeout: the killed gate leaves a FAIL receipt, never the older pass",
         result, "fail")
    same("job timeout: a killed step gets no PASS or FAIL verdict line of its own",
         [ln for ln in out.splitlines() if ln.startswith(("[PASS]", "[FAIL]"))], [])
    rc, last, result, swept, left, out = _overrun(tmp, home, "parallel", "jobs = 2")
    same("job timeout: a PARALLEL gate is bounded the same way: exit 1, the KILLED line, a "
         "fail receipt, its step group swept, its slots given back",
         (rc, last, result, swept, left), (1, _KILLED, "fail", True, []))
    # The bound is the pool's: with no budget the same key bounds nothing.
    put(os.path.join(home, "config.toml"), "[pool]\nprotocol = 1\njob_timeout_s = 1\n")
    root = make_repo(tmp, "unbounded", one_step("sleep 2; echo outlived-the-bound"))
    rc, out, err = run_runner(root, home)
    same("job timeout: with the pool OFF job_timeout_s bounds nothing: a step that outlasts it "
         "passes", (rc, "outlived-the-bound" in out, err), (0, True, ""))
    # Any positive integer is a valid bound; one no float can hold must not reach the clock.
    put(os.path.join(home, "config.toml"), CONFIG + "job_timeout_s = 1" + "0" * 400 + "\n")
    rc, out, err = run_runner(make_repo(tmp, "huge", one_step("echo huge-ran")), home)
    same("job timeout: an absurdly large job_timeout_s is clamped: the gate runs and passes, "
         "with no traceback and no ticket left",
         (rc, "[PASS] s (exit 0" in out, err, listing(home)), (0, True, "", []))


@e2e
def case_nested_timeout(tmp, home):
    # wait_timeout_s bounds the one way this can go wrong: a run that is NOT taken as nested
    # would wait on the worktree its holder holds.
    put(os.path.join(home, "config.toml"), CONFIG + "job_timeout_s = 1\nwait_timeout_s = 5\n")
    root = make_repo(tmp, "nested", one_step("sleep 8"))
    hold(home, root)
    held = listing(home, suffix=".ticket")
    env = {"GATEQ_HOLDER": os.path.join(home, "waiters", held[0])}
    p = spawn(root, home, env=env)
    same("nested timeout: a NESTED run is bounded by job_timeout_s too: KILLED, exit 1, and no "
         "ticket of its own",
         (ended(p, timeout=40), (read(p.err).splitlines() or [""])[-1],
          listing(home, suffix=".ticket")), (1, _KILLED, held))
    # The holder is by now OLDER than the bound (the run above took all of it), so a nested
    # run measured from its holder's start would be killed before its first step.
    put(os.path.join(root, ".gates.toml"), one_step("echo quick-ran"))
    rc, out, err = run_runner(root, home, env=env)
    same("nested timeout: the bound is measured from the nested run's OWN start: a quick one "
         "under a holder older than job_timeout_s passes",
         (rc, "quick-ran" in out, err), (0, True, ""))


CONTEND_CHILD = """
import fcntl, os, sys, time
sys.path.insert(0, sys.argv[1])
import gate_pool as gp
home, wt, checks, rounds = sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
pool = gp.Pool(home, {"protocol": gp.POOL_PROTOCOL, "budget": 2})

def lock(name):
    fd = os.open(os.path.join(checks, name), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)

print("ready", flush=True)
sys.stdin.readline()
end = time.monotonic() + 120
for _ in range(rounds):
    w = pool.enter("gate", "gate", 1, wt)
    while not w.poll():
        if time.monotonic() > end:
            sys.exit("deadline")
        time.sleep(0.001)                  # paces the polls; every decision is under admit.lock
    mine = lock("worktree-" + os.path.basename(wt))
    unit = lock("unit-0") or lock("unit-1")
    print(w.seq, "ok" if mine and unit else "TWO-IN-ONE-WORKTREE" if unit else "OVER-BUDGET",
          flush=True)
    for fd in (mine, unit):
        if fd is not None:
            os.close(fd)
    w.leave()
print("done", flush=True)
"""


@e2e
def case_contention(tmp, home):
    # REAL processes, because every in-process case is single-threaded and so cannot tell a
    # taken admit.lock from an absent one. Proven: with admit.lock not taken, concurrent
    # processes corrupt staging and seq allocation and this harness fails. Six race enter() and poll() on a budget of 2, two per
    # worktree. Each, while it HOLDS, proves the pool's promise with locks of its OWN, outside
    # the pool: its worktree's check lock (nobody else runs in this worktree) and one of two
    # unit locks (at most `budget` run at once). Both are taken after the grant and dropped
    # before the release, so under a correct pool a try-lock can never fail; the kernel answers
    # atomically, so there is no listing to race.
    checks, rounds = os.path.join(tmp, "checks"), 15
    os.mkdir(checks)
    kids = [subprocess.Popen([sys.executable, "-B", "-c", CONTEND_CHILD, SCRIPTS, home,
                              f"/wt/{name}", checks, str(rounds)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for name in "abc" for _ in range(2)]
    _LIVE.extend(kids)
    ready = [k.stdout.readline() for k in kids]
    for k in kids:                         # the starting gun: all six are imported and waiting
        with contextlib.suppress(OSError):
            k.stdin.write("go\n"); k.stdin.flush()
    outs = [k.communicate(timeout=300) for k in kids]
    lines = [ln.split() for out, _ in outs for ln in out.splitlines()]
    seqs = sorted(int(ln[0]) for ln in lines if len(ln) == 2)
    got = ([k.returncode for k in kids], ready.count("ready\n"), sum(ln == ["done"] for ln in lines),
           sorted({ln[1] for ln in lines if len(ln) == 2}), seqs == list(range(1, len(seqs) + 1)),
           len(seqs), listing(home))
    same("contention: six processes racing enter() and poll() on a budget of 2: every one "
         "finishes every round, no seq is handed out twice, and the pool's directory is left empty", got, ([0] * 6, 6, 6, ["ok"], True, 6 * rounds, []))
    if got[0] != [0] * 6:
        print("         " + " | ".join(err.strip().splitlines()[-1] for _, err in outs
                                        if err.strip()))


CASES = [
    ("cls", case_cls), ("order", case_order), ("clamp", case_clamp),
    ("table1", case_table1), ("table2", case_table2), ("class-matters", case_class_matters),
    ("protected", case_protected), ("worktree", case_worktree),
    ("class0-pass-list", case_class0_pass_list), ("bound-in-pass", case_bound_in_pass),
    ("cost-zero", case_cost_zero), ("pure", case_pure), ("static", case_static),
    ("disk-budget", case_disk_budget), ("disk-order", case_disk_order),
    ("disk-worktree", case_disk_worktree), ("release-atomic", case_release_atomic),
    ("sidecar", case_sidecar), ("sigkill", case_sigkill),
    ("inherit", case_inherit), ("budget-change", case_budget_change), ("root", case_root),
    ("no-git-under-lock", case_no_git_under_lock), ("worktree-key", case_worktree_key),
    ("acquire", case_acquire), ("foreign", case_foreign),
    ("doctor-tickets", case_doctor_tickets), ("nested-rows", case_nested_rows),
    ("nested-serial", case_nested_serial), ("nested-chain", case_nested_chain),
    ("unreadable-ticket", case_unreadable_ticket), ("strays", case_strays),
    ("git-isolation", case_git_isolation),
    ("off-golden", case_off_golden), ("off-runner-alone", case_off_runner_alone),
    ("config-table", case_config_table), ("cost-table", case_cost_table), ("weight", case_weight),
    ("e2e-refusals", case_e2e_refusals), ("e2e-rejected-costing", case_e2e_rejected_costing),
    ("e2e-three-gates", case_e2e_three_gates), ("e2e-over-budget", case_e2e_over_budget),
    ("e2e-undeclared", case_e2e_undeclared), ("e2e-timeout", case_e2e_timeout),
    ("e2e-interrupt", case_e2e_interrupt), ("e2e-budget-removed", case_e2e_budget_removed),
    ("e2e-gates-edited", case_e2e_gates_edited), ("e2e-receipt", case_e2e_receipt),
    ("e2e-git-outside-lock", case_e2e_git_outside_lock), ("e2e-holder-env", case_e2e_holder_env),
    ("contention", case_contention),
    ("serial-golden-on", case_serial_golden_on), ("serial-group", case_serial_group),
    ("serial-signals", case_serial_signals), ("serial-sweep", case_serial_sweep),
    ("serial-tstp", case_serial_tstp), ("job-timeout", case_job_timeout),
    ("serial-launch-window", case_serial_launch_window),
    ("serial-two-signals", case_serial_two_signals), ("serial-ignored", case_serial_ignored),
    ("serial-group-signal", case_serial_group_signal), ("nested-timeout", case_nested_timeout),
]

# --- mutation self-test: each assertion must be able to FAIL ---------------------------------
# (label, old text, new text, case to run, the check whose FAIL line the run must print).
# The first eight are the schedule() mutations the doc's A1 test plan names; the rest guard
# assertions this harness adds. `old` must occur EXACTLY ONCE in the module, or the row is stale.
MUTATIONS = [
    ("protection tested against the stored count only",
     "return x.bypass + pend.get(x, 0) >= k", "return x.bypass >= k", "bound-in-pass",
     "bound: one pass over a gate at bypass 1 with four small gates behind returns ONE start"),
    ("same-worktree filter dropped from the class-0 pass list",
     "and w.worktree != e.worktree", "and True", "class0-pass-list",
     "class-0 pass list: no pass against a gate waiting in the check's own worktree"),
    ("min(cost, budget) clamp dropped",
     "c = min(e.cost, budget)", "c = e.cost", "clamp",
     "clamp: a cost above the whole budget starts when the whole budget is free"),
    ("first HOLD line dropped (class 0 behind guard())",
     "if cls(e, cap) == 0 and guard():", "if False:", "table2",
     "table 2, m2 arrives: starts"),
    ("second HOLD line dropped (a protected entry ahead)",
     "if any(prot(b) for b in blocked):", "if False:", "table1",
     "table 1, B finishes: starts"),
    ("a cost-0 start counted as a pass",
     "start.append((e, []))", "start.append((e, list(blocked)))", "cost-zero",
     "cost 0: a zero-cost entry behind a blocked one starts and records no pass"),
    ("a class-0 start no longer counted as a pass",
     "if cls(e, cap) == 0:", "if False:", "table2",
     "table 2, m1 arrives: starts"),
    ("claimed check dropped",
     "if e.worktree in claimed:", "if False:", "worktree",
     "worktree: two gates in one worktree never start together"),
    ("class-0 boundary moved below the cap",
     "e.weight <= cap", "e.weight < cap", "cls",
     "cls: a named command AT the cap is class 0"),
    ("fix rounds no longer class 1",
     "return 1 if e.open_pr else 2", "return 2", "cls",
     "cls: a job with an open PR (a fix round) is class 1"),
    ("order ignores the class",
     "key=lambda e: (cls(e, cap), e.seq)", "key=lambda e: e.seq", "order",
     "order: a fix round goes ahead of an earlier first push, and that is not a pass"),
    ("order ignores the sequence number",
     "key=lambda e: (cls(e, cap), e.seq)", "key=lambda e: cls(e, cap)", "order",
     "order: three equal-cost entries and one free unit start the lowest seq only"),
    ("a start no longer spends free budget",
     "free -= c", "pass", "order",
     "order: three equal-cost entries and one free unit start the lowest seq only"),
    ("a start no longer claims its worktree",
     "free -= c\n            claimed.add(e.worktree)", "free -= c", "worktree",
     "worktree: a worktree claimed earlier in the pass is not a block and not a pass"),
    ("a cost-0 start no longer claims its worktree",
     "start.append((e, []))\n            claimed.add(e.worktree)", "start.append((e, []))",
     "cost-zero", "cost 0: a zero-cost start claims its worktree"),
    ("protection begins one pass late",
     "pend.get(x, 0) >= k", "pend.get(x, 0) > k", "table1",
     "table 1, B finishes: starts"),
    ("guard() counts a gate waiting on its worktree",
     "prot(w) and w.worktree not in claimed for w in order", "prot(w) for w in order",
     "protected", "protected: a protected gate waiting on its worktree holds no class-0 check"),
    ("guard() counts a protected class-0 check",
     "cls(w, cap) > 0 and prot(w)", "prot(w)", "protected",
     "protected: a protected class-0 check does not hold a check AHEAD of it"),
    ("class-0 pass list counts a gate waiting on its worktree",
     "if cls(w, cap) > 0 and w.worktree not in claimed", "if cls(w, cap) > 0", "worktree",
     "worktree: an entry waiting on its worktree accrues no pass from a class-0 start"),
    ("class-0 pass list ignores the budget the check takes",
     "> free - c]", "> free]", "class0-pass-list",
     "class-0 pass list: a gate the start leaves short of budget is passed"),
    ("class-0 pass list counts a gate that still fits",
     "> free - c]", ">= free - c]", "class0-pass-list",
     "class-0 pass list: a gate that still fits beside the check is not passed"),
    ("class-0 pass list counts a gate whose worktree this pass claimed",
     "cls(w, cap) > 0 and w.worktree not in claimed", "cls(w, cap) > 0 and w.worktree not in busy",
     "class0-pass-list",
     "class-0 pass list: a gate whose worktree an earlier start claimed is not passed"),
    ("an explicit weight of 0 replaced by the cost",
     "cost if weight is None else weight", "weight or cost", "cls",
     "cls: an explicit weight of 0 is honored, not replaced by the cost"),
    ("the pass's own starts no longer recorded",
     "pend[b] = pend.get(b, 0) + 1", "pass", "bound-in-pass",
     "bound: one pass over a gate at bypass 1 with four class-0 checks returns ONE start"),
    ("the c == 0 branch dropped",
     "if c == 0:", "if False:", "cost-zero",
     "cost 0: a zero-cost entry is not held by a protected entry ahead of it"),
    # --- the pool on disk. The first two are named by the doc's A1 test plan. ---
    ("slot and ticket descriptors made inheritable",
     "return os.open(path, flags, 0o600)",
     "fd = os.open(path, flags, 0o600); os.set_inheritable(fd, True); return fd", "inherit",
     "inherit: no descriptor a holder holds (ticket and slots) is inheritable"),
    ("the bypass written into the ticket with os.replace",
     "p._put(_sched_of(b.path), json.dumps(", "p._put(b.path, json.dumps(", "sidecar",
     "sidecar: a bypass written by another evaluator leaves the ticket's lock HELD"),
    ("a bare fork in the module",
     "import time\n", "import time\n_fork = os.fork\n", "static",
     "static: no bare fork, no pass_fds and no close_fds=False in the module"),
    ("the worktree key resolved inside admit.lock",
     "= p._scan(self.seq)", "= p._scan(self.seq); worktree_key(p.home)",
     "no-git-under-lock",
     "no git under the lock: enter, poll, commit and release start no process"),
    ("a failed poll keeps the slots it probed",
     "for fd in got[keep:]:", "for fd in got[len(got):]:", "disk-budget",
     "disk budget: a failed poll keeps no slot (2 of 10 are still free to a newcomer)"),
    ("the worktree of a running ticket not counted as busy",
     "busy[rec[\"worktree\"]] = f\"{rec['kind']} pid {rec.get('pid')}\"", "pass", "disk-worktree",
     "disk worktree: a second entry in the SAME worktree does not start"),
    ("a dead ticket left in place",
     "if ours:", "if False:", "sidecar",
     "sidecar: the dead ticket and its sidecar were unlinked by that pass"),
    ("a foreign-protocol ticket whose lock is free unlinked",
     "if ours:", "if True:", "foreign",
     "foreign: no foreign or unreadable record was unlinked in all those passes"),
    ("a live foreign ticket with a lower seq no longer blocks",
     "blocked = blocked or n is None or n < seq", "pass", "foreign",
     "foreign: a waiter does not start behind a LIVE foreign ticket with a lower seq"),
    ("a live foreign ticket with ANY seq blocks",
     "blocked = blocked or n is None or n < seq", "blocked = True", "foreign",
     "foreign: a live foreign ticket with a HIGHER seq blocks nobody"),
    ("a live foreign ticket's worktree not counted as busy",
     'busy.setdefault(rec["worktree"], "another pool protocol")', "pass", "foreign",
     "foreign: a live foreign ticket's worktree is busy"),
    ("a waiter that gave up keeps its ticket",
     "            w.leave()\n            raise", "            raise", "acquire",
     "acquire: a waiter that gave up leaves no ticket behind"),
    ("the wait line repeated on every poll",
     "if noted is None or now - noted >= WAIT_NOTE_S:", "if True:", "acquire",
     "acquire: one wait line at the first failed poll, then one every 30 s"),
    # --- nested runs. The first four are named by the doc's A1 test plan. ---
    ("the .nest lock dropped",
     "if not _try_lock(fd):", "if False:", "nested-serial",
     "nested serial: two nested runs under one holder do not run together"),
    ("a run with a valid GATEQ_NEST skips locking",
     'return "ok", os.path.join(self.waiters, parent + ".nest")',
     'return "ok", os.path.join(self.waiters, parent + ".nest" + ('
     'os.urandom(4).hex() if parent != hname else ""))', "nested-chain",
     "nested chain: FAN-OUT, two children of one nested run do not run together"),
    ("the worktree-key comparison dropped from the nested check",
     'if rec["worktree"] != worktree:', "if False:", "nested-rows",
     "nested rows: condition 2 fails (another worktree) -> a ticket, and ONE line naming "
     "the leak"),
    ("the cost comparison dropped from the nested check",
     "if have < need:", "if False:", "nested-rows",
     "nested rows: condition 3 fails (heavier than its holder) -> NOT RUN, exit 75, at once"),
    ("condition 1 no longer needs the holder's lock HELD",
     "if not held or not _ours(rec)", "if not _ours(rec)", "nested-rows",
     "nested rows: condition 1 fails -> no exemption, and no line"),
    ("a GATEQ_NEST honored without its lock HELD",
     " \\\n                and _probe(os.path.join(self.waiters, nname))[0]:", ":", "nested-chain",
     "nested chain: a GATEQ_NEST that is unlocked, missing or not this holder's is ignored: "
     "the run waits on its holder's own child lock"),
    ("a HELD child lock unlinked with its dead holder's ticket",
     " \\\n                    and _probe(path)[0] is False:", ":", "nested-serial",
     "nested serial: the pass that unlinks a dead holder's ticket leaves its HELD child lock"),
    ("a nested wait keeps its exemption after its holder died",
     "self.lost = True", "pass", "nested-serial",
     "nested serial: a nested WAIT whose holder died is exempt no longer"),
    ("a ticket holder passes an inherited GATEQ_HOLDER through",
     'return {"GATEQ_HOLDER": self.path, "GATEQ_NEST": ""}', 'return {"GATEQ_NEST": ""}',
     "nested-rows",
     "nested rows: a ticket holder names its OWN ticket for its children, and blanks GATEQ_NEST"),
    ("a nested run passes no GATEQ_HOLDER to its children",
     'return {"GATEQ_HOLDER": self.path, "GATEQ_NEST": self.nest}',
     'return {"GATEQ_NEST": self.nest}', "nested-rows",
     "nested rows: what it holds is its holder's child lock, which it names for ITS children"),
    ("a released holder's locks dropped after admit.lock is let go",
     "            self._close()                  # before admit.lock is let go, never after",
     "        self._close()", "release-atomic",
     "release: the ticket is gone and every lock dropped in ONE admit.lock section"),
    ("worktree_key runs git with the caller's GIT_ variables",
     "timeout=60, env=env).stdout", "timeout=60).stdout", "worktree-key",
     "worktree key: with GIT_DIR naming another repository the key is still the string git "
     "RECORDED for this worktree"),
    ("seq allocated from the seq file alone",
     "seq = 1 + max([0, last] + [n for n in map(_seq_of, os.listdir(self.waiters))\n"
     "                                       if n is not None])", "seq = 1 + last", "disk-order",
     "disk order: seq follows enter() order and survives a deleted seq file"),
    ("seq allocated from the visible tickets alone",
     "seq = 1 + max([0, last] +", "seq = 1 + max([0] +", "disk-order",
     "disk order: a seq is never reused once its ticket is gone (the seq file)"),
    ("the pool root's mode not checked",
     "or stat.S_IMODE(st.st_mode) & 0o077:", "and False:", "root",
     "root: a pool root open to group or other is refused with exit 2"),
    ("the recorded worktree string replaced by the resolved path",
     "            return line[9:]", "            return real", "worktree-key",
     "worktree key: a path that resolves to a registered worktree gets the string git "
     "RECORDED"),
    ("a GATEQ_NEST honored without the holder's name as its prefix",
     "if nname.startswith(hname) and tail and", "if tail and", "nested-chain",
     "nested chain: a GATEQ_NEST that is unlocked, missing or not this holder's is ignored: "
     "the run waits on its holder's own child lock"),
    ("a non-regular file in waiters/ read as a ticket",
     'if not name.endswith(".ticket") or not _is_file(path):', 'if not name.endswith(".ticket"):',
     "strays", "strays: a directory named *.ticket in waiters/ changes nothing"),
    ("a symlink in waiters/ followed as a ticket",
     'if not name.endswith(".ticket") or not _is_file(path):', 'if not name.endswith(".ticket"):',
     "strays", "strays: a symlink to a live ticket is not a second entry, so the ticket starts"),
    ("the tmp/ sweep raising on a stray entry",
     "except OSError:  # a stray entry in tmp/", "except FileNotFoundError:", "strays",
     "strays: a directory in tmp/ does not stop the sweep"),
]
MUTATIONS += [
    # --- owed by the review of the on-disk slices: real processes, and a real exec'd child ---
    ("admit.lock never taken (the pass and every change to waiters/ unserialized)",
     "            fcntl.flock(fd, fcntl.LOCK_EX)\n            yield", "            yield",
     "contention",
     "contention: six processes racing enter() and poll() on a budget of 2: every one finishes "
     "every round, no seq is handed out twice, and the pool's directory is left empty"),
    ("lock descriptors inherited by a child started by exec",
     "return os.open(path, flags, 0o600)",
     "fd = os.open(path, flags, 0o600); os.set_inheritable(fd, True); return fd", "inherit",
     "inherit: a child a holder starts by exec holds NONE of the holder's lock descriptors"),
    # --- dead_foreign_tickets(), doctor's read-only view (S5) ---
    ("doctor's list includes a ticket whose lock is HELD",
     "if held is False and not mine:", "if not mine:", "doctor-tickets",
     "doctor tickets: a FREE ticket of another protocol is listed with its sidecar, a free "
     "unreadable one without; a HELD ticket and one of our own protocol never are"),
    ("doctor's list includes tickets of the configured protocol",
     "if held is False and not mine:", "if held is False:", "doctor-tickets",
     "doctor tickets: a FREE ticket of another protocol is listed with its sidecar, a free "
     "unreadable one without; a HELD ticket and one of our own protocol never are"),
    ("doctor's probe creates admit.lock",
     "    if _is_file(admit_path):\n        try:\n            admit = _open_lock(admit_path, "
     "os.O_RDONLY | os.O_NONBLOCK)",
     "    if True:\n        try:\n            admit = _open_lock(admit_path, os.O_RDWR | os.O_CREAT)",
     "doctor-tickets",
     "doctor tickets: nothing was created, written or unlinked (no admit.lock either)"),
    ("doctor's admit.lock try-lock reverted to a BLOCKING lock",
     "while not _try_lock(admit):",
     "fcntl.flock(admit, fcntl.LOCK_EX)\n            while False:", "doctor-tickets",
     "doctor tickets: a held admit.lock is 'skipped' (None) after a bounded wait, never a hang "
     "and never a lockless read"),
    ("doctor reports a held admit.lock as 'none found' ([]) instead of skipped",
     "return None\n                time.sleep(0.05)", "return []\n                time.sleep(0.05)",
     "doctor-tickets",
     "doctor tickets: a held admit.lock is 'skipped' (None) after a bounded wait, never a hang "
     "and never a lockless read"),
    ("doctor treats an own-protocol ticket with a bad name as its own",
     "protocol != POOL_PROTOCOL or (_ours(rec) and _seq_of(name) is not None))",
     "protocol != POOL_PROTOCOL or _ours(rec))", "doctor-tickets",
     "doctor tickets: an own-protocol ticket with a non-numeric name or a malformed record "
     "is listed, as documented"),
    ("doctor's probe unlinks the dead ticket it names",
     "dead.append((path, side if _is_file(side) else None))",
     "dead.append((path, side if _is_file(side) else None)); _rm(path)", "doctor-tickets",
     "doctor tickets: nothing was created, written or unlinked (no admit.lock either)"),
]
# The same table for scripts/gate-runner.py: the wiring. `old` must occur exactly once THERE.
RUNNER_MUTATIONS = [
    ("a [pool] table with no budget read as an unlimited budget",
     '    if "budget" not in pool:\n        return "off", "pool-table-without-budget"',
     '    pool.setdefault("budget", 1 << 20)', "off-runner-alone",
     "off runner alone: a [pool] table with no budget: form-a output and exit code equal the "
     "pre-pool golden"),
    ("an unknown key inside [pool] ignored (a typo disables the bound)",
     "        if key not in POOL_KEYS:", "        if False:", "e2e-refusals",
     "refusals: a misspelled key (budegt) exits 2 naming it: no step, no ticket, no stale receipt"),
    ("the positive-integer rule dropped from the machine keys",
     "        if key in pool and not _valid_jobs(pool[key]):", "        if False:", "config-table",
     "config: every key refuses zero, a negative, a bool, a string and a float, naming the key"),
    ("the [prep_pr] weight check skipped",
     'if "weight" in prep and not _valid_jobs(prep["weight"]):', "if False:", "weight",
     "weight: with the pool OFF a weight that is no positive integer exits 2 and runs no step"),
    ("an undeclared cost read as one unit, not the whole budget",
     "        return min(jobs, budget), raw\n    return budget, raw",
     "        return min(jobs, budget), raw\n    return 1, raw", "cost-table",
     "cost: no weight and no jobs is the WHOLE budget (bare Form B, Form A, the fallback "
     "chain), and jobs buys Form A and the fallback chain nothing"),
    ("a config rejected while costing takes a ticket anyway",
     "        if cost is not None:", "        if True:", "e2e-rejected-costing",
     "rejected while costing: a bad weight exits 2 with the pool-off output, no step and no "
     "ticket"),
    ("the worktree key resolved inside admit.lock",
     "        key = gate_pool.worktree_key(root)",
     "        with pool._admit(): key = gate_pool.worktree_key(root)", "e2e-git-outside-lock",
     "git outside the lock: the runner resolves its worktree key and takes its receipt "
     "snapshot through git, and NO git call runs while admit.lock is held"),
    ("the worktree key taken from the current directory, not the worktree root",
     "        key = gate_pool.worktree_key(root)",
     "        key = gate_pool.worktree_key(os.getcwd())", "e2e-over-budget",
     "over budget: a gate started from a SUBDIRECTORY is keyed by its worktree ROOT"),
    ("the receipt snapshot taken before the grant",
     "    state, pool = _pool_config()\n",
     "    state, pool = _pool_config()\n"
     "    if receipt_path:\n"
     "        _early = _snapshot(root, receipt_path); globals()['_snapshot'] = lambda r, p: _early\n",
     "e2e-receipt",
     "receipt: the snapshot is taken AFTER the grant: the receipt binds the commit made during "
     "the wait, and passes"),
    ("the definition costed before the wait run after the grant",
     "            recost, raw = _pool_cost(root, jobs, pool[\"budget\"])",
     "            recost, _ = _pool_cost(root, jobs, pool[\"budget\"])", "e2e-gates-edited",
     "gates edited: a .gates.toml edited during the wait is re-read after the grant, and THAT "
     "definition runs"),
    ("a definition that grew during the wait run under the smaller hold",
     "            if recost is not None and recost > cost:", "            if False:",
     "e2e-gates-edited",
     "gates edited: a definition that costs MORE after the wait than the run holds is NOT RUN, "
     "exit 75: no step, no stale receipt, and its slots are given back"),
    ("an inherited GATEQ_HOLDER passed through to the steps",
     "            os.environ.update(holder.child_env())",
     '            os.environ.setdefault("GATEQ_NEST", "")', "e2e-holder-env",
     "holder env: a holder's step sees the runner's OWN ticket in GATEQ_HOLDER, never the "
     "inherited one, and no GATEQ_NEST"),
    ("a wait that gave up leaves the older receipt in place",
     "                if receipt_path:\n                    _remove_stale(receipt_path)\n"
     "                return code", "                return code", "e2e-timeout",
     "timeout: no step ran, the waiter's ticket is gone, and no older receipt is left to read "
     "as this run's"),
    ("a dangling symlink read as 'no config' (pool off)",
     "            if os.path.islink(p) and not os.path.exists(p):", "            if False:",
     "e2e-refusals",
     "refusals: a dangling config.toml or a dangling pool home exits 2, never 'off': no step, "
     "no stale receipt"),
    ("a symlinked pool home refused as dangling although its target exists",
     "            if os.path.islink(p) and not os.path.exists(p):",
     "            if os.path.islink(p):", "e2e-refusals",
     "refusals: a pool home that is a symlink to a real directory with no config is OFF: "
     "the gate runs unpooled and nothing is created there"),
    ("a broken gate_pool.py escapes as a traceback (no catch-all arm)",
     "    except Exception as e:                 # a broken gate_pool.py",
     "    except ZeroDivisionError as e:         # a broken gate_pool.py", "e2e-refusals",
     "refusals: a gate_pool.py with a syntax error exits 2 in one line, never a traceback"),
    ("an unusable pool directory lets the gate run (the OSError arm returns 0)",
     '            _pool_say(f"gate-runner: NOT RUN - pool error: {e}")\n            return None, 2',
     '            _pool_say(f"gate-runner: NOT RUN - pool error: {e}")\n            return None, 0',
     "e2e-refusals",
     "refusals: a pool home whose waiters is a regular file exits 2, never unpooled"),
    ("a top-level budget ignored",
     '        if key in data:                    # outside [pool]',
     '        if False:                          # outside [pool]', "e2e-refusals",
     "refusals: a top-level budget (outside [pool]) exits 2 saying where it belongs"),
    ("main() drops the costed bytes instead of forwarding them",
     "_run_gates(root, memoize_dir, jobs, skip, shard, raw=raw)",
     "_run_gates(root, memoize_dir, jobs, skip, shard)", "e2e-rejected-costing",
     "rejected while costing: main() hands the bytes it costed to the run: the invalid config "
     "is rejected even though the file on disk is valid"),
    ("the slots given back before the gate ran (released at the grant)",
     "            recost, raw = _pool_cost(root, jobs, pool[\"budget\"])",
     "            holder.release(); recost, raw = _pool_cost(root, jobs, pool[\"budget\"])",
     "e2e-over-budget",
     "over budget: a weight above the budget runs, holding the WHOLE budget: nothing fits "
     "beside it"),
]
# --- the pooled serial step path (PR A2) ---
_SIGS = "_FORWARD_SIGS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)"
_SWEEP_CALL = "        _sweep_step(proc)\n        for s, handler in old.items():"
_BG_SWEPT = ("serial sweep: with the pool ON a passing step's background job is swept before "
             "the runner ends")
_SAME_END = "ends the pooled runner with the pool-off exit status and final lines"
RUNNER_MUTATIONS += [
    ("SIGQUIT not forwarded to a pooled serial step",
     _SIGS, _SIGS.replace(", signal.SIGQUIT", ""), "serial-signals",
     "serial signals: SIGQUIT reaches a pooled serial step: the runner forwards it to the "
     "step's group"),
    ("SIGTSTP forwarded to a pooled serial step",
     _SIGS, _SIGS.replace(")", ", signal.SIGTSTP)"), "serial-tstp",
     "serial tstp: SIGTSTP stops the runner and is NOT forwarded: the step never sees it and "
     "runs on to its end while the runner is stopped"),
    ("a pooled serial step started in the runner's own process group",
     "                process_group=0,\n", "", "serial-group",
     "serial group: with the pool ON a serial step runs in a process group of its OWN"),
    ("main() never switches the pooled step path on",
     '            _POOL_RUN = {"deadline": time.monotonic() + limit}', "            pass",
     "serial-group",
     "serial group: with the pool ON a serial step runs in a process group of its OWN"),
    ("a pooled serial step given the runner's stdin (the terminal)",
     "command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL,\n                process_group=0,",
     "command, shell=True, cwd=cwd,\n                process_group=0,", "serial-group",
     "serial group: with the pool ON a serial step's stdin is /dev/null, not the runner's"),
    ("the pool-OFF serial path given the pooled step path (a group, a sweep)",
     "    if _POOL_RUN is not None:              # pool ON", "    if True:", "serial-group",
     "serial group: with the pool OFF a serial step shares the runner's process group and "
     "reads the runner's stdin, as before the pool"),
    ("the sweep skipped on the exit path of an interrupted step",
     _SWEEP_CALL, _SWEEP_CALL.replace("        _sweep", "        if not caught:\n            _sweep"),
     "serial-sweep",
     "serial sweep: after a forwarded SIGINT the step's background job, which ignores SIGINT, "
     "is swept before the runner ends"),
    ("the sweep skipped on the exit path of a step that ended by itself",
     _SWEEP_CALL, _SWEEP_CALL.replace("        _sweep", "        if caught:\n            _sweep"),
     "serial-sweep", _BG_SWEPT),
    ("the sweep leaves a finished step's background job alone",
     "        _terminate_groups({}, [proc.pid])", "        pass", "serial-sweep", _BG_SWEPT),
    ("the sweep leaves a step that outlived the forwarded signal running",
     '    if not _reap(proc):\n        _terminate_groups({0: {"proc": proc}})',
     "    if not _reap(proc):\n        pass", "serial-sweep",
     "serial sweep: a step that ignores the forwarded SIGTERM is killed with its group, and "
     "the runner still ends by SIGTERM"),
    ("a forwarded SIGTERM read as a failed step (exit 1, a FAIL line)",
     "        raise _PoolSignal(caught[0])", "        pass", "serial-signals",
     "serial signals: SIGTERM " + _SAME_END),
    ("a forwarded SIGINT ends the runner with no KeyboardInterrupt",
     "        if caught[0] == signal.SIGINT:     # the traceback", "        if False:              # the traceback",
     "serial-signals", "serial signals: SIGINT " + _SAME_END),
    ("the runner dies of a forwarded signal before it gives its slots back",
     "        raise _PoolSignal(caught[0])", "        _die_of(caught[0])", "serial-signals",
     "serial signals: a runner ended by a forwarded signal gave its ticket back first"),
]
# --- the hand-run timeout (PR A2) ---
_T_KILLED = ("job timeout: a hand-run gate whose serial step outlasts job_timeout_s is KILLED: "
             "exit 1 and the KILLED line")
_T_PARALLEL = ("job timeout: a PARALLEL gate is bounded the same way: exit 1, the KILLED line, a "
               "fail receipt, its step group swept, its slots given back")
RUNNER_MUTATIONS += [
    ("a serial step is never checked against job_timeout_s",
     "            if _pool_overdue():            # the sweep below is the kill",
     "            if False:", "job-timeout", _T_KILLED),
    ("a parallel run is never checked against job_timeout_s",
     "            if _pool_overdue():            # pool on only",
     "            if False:                      # pool on only", "job-timeout", _T_PARALLEL),
    ("job_timeout_s from the config ignored (the default always applies)",
     'limit = pool.get("job_timeout_s", JOB_TIMEOUT_S)', "limit = JOB_TIMEOUT_S", "job-timeout",
     _T_KILLED),
    ("a killed gate exits 0",
     "            rc, records = 1, _synth_records(1)", "            rc, records = 0, _synth_records(0)",
     "job-timeout", _T_KILLED),
    ("a killed gate prints no KILLED line",
     '            _pool_say(f"gate-runner: KILLED - exceeded job_timeout_s ({limit}s)")\n', "",
     "job-timeout", _T_KILLED),
    ("a killed gate read as an ordinary failed step (a FAIL line, no KILLED)",
     "    if overdue:\n        raise _PoolTimeout\n", "", "job-timeout",
     "job timeout: a killed step gets no PASS or FAIL verdict line of its own"),
    ("a killed gate writes no receipt (the timeout escapes main's receipt step)",
     "        except _PoolTimeout:\n", "        except ZeroDivisionError:\n", "job-timeout",
     "job timeout: the killed gate leaves a FAIL receipt, never the older pass"),
]
# --- the A2 review rows: the launch window, the sweep, ignored and repeated signals, the clamp ---
_RESTORE = "\n            signal.signal(s, handler)"
_POOL_ON = '            _POOL_RUN = {"deadline": time.monotonic() + limit}'
RUNNER_MUTATIONS += [
    ("the forwarded signals are not blocked across the step's launch",
     "    prev = signal.pthread_sigmask(signal.SIG_BLOCK, _FORWARD_SIGS)",
     "    prev = signal.pthread_sigmask(signal.SIG_BLOCK, ())", "serial-launch-window",
     "serial launch window: a signal that lands between the step's Popen and the handler "
     "install is not lost: it is forwarded, the step's group is gone, the ticket is given "
     "back, and the runner ends by that signal"),
    ("the recording handlers taken out BEFORE the sweep",
     _SWEEP_CALL + _RESTORE,
     "        for s, handler in old.items():" + _RESTORE + "\n        _sweep_step(proc)",
     "serial-two-signals",
     "serial two signals: a second signal during the sweep does not abort it: the step's "
     "group is gone when the runner ends, by the FIRST signal, its ticket given back"),
    ("a signal inherited as ignored is taken over by the pooled step path",
     "            if signal.getsignal(s) != signal.SIG_IGN:", "            if True:",
     "serial-ignored",
     "serial ignored: a signal the runner INHERITED as ignored stays ignored with the pool "
     "on: the step is not signalled, the gate passes, exit 0, no ticket left"),
    ("only the first signal of a step is forwarded",
     "            if n > sent:", "            if n > sent and not sent:", "serial-two-signals",
     "serial two signals: two different signals during one step are EACH forwarded, once"),
    ("the LAST signal decides how the runner ends",
     "        raise _PoolSignal(caught[0])", "        raise _PoolSignal(caught[-1])",
     "serial-two-signals",
     "serial two signals: the FIRST signal decides how the runner ends, and the ticket is "
     "given back"),
    ("a nested run gets no deadline",
     _POOL_ON, _POOL_ON.replace("+ limit", '+ (limit if holder.nest is None else float("inf"))'),
     "nested-timeout",
     "nested timeout: a NESTED run is bounded by job_timeout_s too: KILLED, exit 1, and no "
     "ticket of its own"),
    ("a forwarded signal sent to the step's leader, not its group",
     "                    _killpg(proc.pid, s)", "                    os.kill(proc.pid, s)",
     "serial-group-signal",
     "serial group signal: a forwarded signal goes to the step's GROUP: a child the step's "
     "shell started without exec gets it too"),
    ("job_timeout_s reaches the clock unclamped",
     "            limit = min(limit, 10**9)\n", "", "job-timeout",
     "job timeout: an absurdly large job_timeout_s is clamped: the gate runs and passes, "
     "with no traceback and no ticket left"),
]
if os.geteuid() != 0:                      # chmod does not bite for root, so that case skips
    RUNNER_MUTATIONS.append(
        ("an unreadable .gates.toml takes no ticket (runs unpooled)",
         "        # Unreadable right now: judged again after the grant, pooled at the whole budget.\n"
         "        return budget, None",
         "        return None, None", "e2e-refusals",
         "refusals: a .gates.toml that cannot be read still takes a ticket at the whole budget "
         "(waits, exits 75), never an unpooled run"))
    MUTATIONS.append(
        ("any failure to open a ticket read as 'missing'",
         'except FileNotFoundError:\n        return None, ""',
         'except OSError:\n        return None, ""', "unreadable-ticket",
         "unreadable ticket: a flocked ticket that cannot be opened is a live unknown one, "
         "never absent: a higher seq does not start"))


def _rerun(scripts_dir, *args):
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args], capture_output=True,
                           text=True, env={**os.environ, "GATE_POOL_SCRIPTS": scripts_dir},
                           timeout=600)
    except subprocess.TimeoutExpired:      # a mutant that hangs is a kill-less failure, not a hang
        return 124, "TIMEOUT: the re-run was killed after 600 s"
    return r.returncode, r.stdout + r.stderr


def mutation_selftest():
    known = {name for name, _ in CASES}
    # The module, the runner that imports it, and the schema module the runner validates a
    # receipt with: one scripts directory, as deployed.
    srcs = {name: read(os.path.join(SCRIPTS, name))
            for name in ("gate_pool.py", "gate-runner.py", "orchestrate_schemas.py")}

    def copy_to(d, name=None, mutated=None):
        """A scripts directory for a re-run, with `name` replaced by `mutated` when given.
        COPIES, in a temp directory: no tracked file is ever opened for writing."""
        os.mkdir(d)
        for n, text in srcs.items():
            put(os.path.join(d, n), mutated if n == name else text)
        return d
    rows = [("gate_pool.py", m) for m in MUTATIONS] + [("gate-runner.py", m)
                                                       for m in RUNNER_MUTATIONS]
    with tempfile.TemporaryDirectory(prefix="gate-pool-mut-") as tmp:
        clean = copy_to(os.path.join(tmp, "clean"))
        rc, out = _rerun(clean)
        check("mutation self-test: the UNMUTATED copy passes", rc == 0)
        if rc != 0:
            print(out)
            return
        for i, (name, (label, old, new, case, want)) in enumerate(rows):
            src = srcs[name]
            if src.count(old) != 1 or case not in known:
                check(f"mutation self-test: '{label}' is stale "
                      f"({src.count(old)} occurrences of its text, case '{case}')", False)
                continue
            d = copy_to(os.path.join(tmp, f"m{i:02d}"), name, src.replace(old, new))
            rc, out = _rerun(d, "--only", case)
            killed = rc != 0 and f"[FAIL] {want}\n" in out
            check(f"mutation: {label} -> killed by '{case}'", killed)
            if not killed:
                print(f"         exit {rc}; wanted the FAIL line of: {want}")


def main():
    only = None
    if len(sys.argv) == 3 and sys.argv[1] == "--only":
        only = sys.argv[2]
    elif len(sys.argv) != 1:
        print("usage: test-gate-pool.py [--only <case>]", file=sys.stderr)
        return 2
    selected = [(n, fn) for n, fn in CASES if only in (None, n)]
    if not selected:
        print(f"test-gate-pool.py: no case named {only!r}", file=sys.stderr)
        return 2
    for name, fn in selected:
        print(f"{name}:")
        try:
            fn()
        except Exception as e:             # a crash is a FAIL line, never a lost verdict
            check(f"{name}: the case ran to its end (raised {type(e).__name__}: {e})", False)
    # Skipped inside a re-run against a copy, so the self-test never recurses.
    if only is None and not os.environ.get("GATE_POOL_SCRIPTS"):
        print("mutation self-test:")
        mutation_selftest()
    elif only is None:
        print("mutation self-test: skipped (GATE_POOL_SCRIPTS is set: this is a re-run "
              "against a copy)")

    print("isolation:")
    same("isolation: the pinned GATEQ_HOME is still empty", os.listdir(_PINNED_HOME), [])
    same("isolation: no spawned runner created anything under its (temp) HOME",
         os.listdir(_FAKE_HOME), [])
    same("isolation: the real pool directory is exactly as it was at the start",
         _real_pool_listing(), _REAL_POOL_BEFORE)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("All gate-pool harness checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
