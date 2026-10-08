#!/usr/bin/env python3
"""Harness for scripts/gate_pool.py, the machine-wide gate pool (epic #538, unit A / #539).
Hand-rolled, stdlib-only, no pytest. Design of record:
skills/orchestrate/design/DESIGN-gate-pool.md ("the doc").

TWO HALVES. The POLICY cases cover `cls()` and the three-class `schedule()` of the doc's
section 2 as a pure function. The DISK cases cover the pool of sections 1 and 4: slots,
tickets, `.sched` sidecars and the scheduling pass under admit.lock, and the nested-run
exemption of section 5 (the three conditions, the `.nest` chain). The gate-runner wiring
arrives with its own slice and extends this file.

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
home, so that is structural; two belts on top of it:
  1. GATEQ_HOME is pinned to a fresh empty temp directory and GATEQ_HOLDER / GATEQ_NEST are
     removed, before anything else runs, and the directory must still be empty at the end;
  2. a case asserts the module source names neither `.claude` nor `expanduser`.

MUTATION SELF-TEST (the test-ci-gates-lockstep.py pattern). An assertion that cannot fail is
decorative, so the harness ends by copying scripts/gate_pool.py into a temp directory, breaking
ONE thing in the copy, and re-running itself against that copy with `--only <case>`. The run
must exit non-zero AND print the named check's FAIL line. An unmutated copy runs first and must
pass, so a broken fixture cannot read as a full set of kills. The working tree's file is never
opened for writing, so a concurrent `git add` can never capture a mutant.
"""

import ast
import atexit
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# --- isolation pins: before the module under test is imported, before any case -------------
for _var in ("GATEQ_HOLDER", "GATEQ_NEST"):
    os.environ.pop(_var, None)
_PINNED_HOME = tempfile.mkdtemp(prefix="gate-pool-test-")
os.environ["GATEQ_HOME"] = _PINNED_HOME
atexit.register(shutil.rmtree, _PINNED_HOME, True)   # every exit path, early returns included

# GATE_POOL_SCRIPTS points the harness at a COPY of the module; only the mutation self-test
# at the end of this file sets it.
SCRIPTS = os.environ.get("GATE_POOL_SCRIPTS") or os.path.join(HERE, "scripts")
MODULE = os.path.join(SCRIPTS, "gate_pool.py")
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
w = gp.Pool(sys.argv[2], {"protocol": gp.POOL_PROTOCOL, "budget": 10}).enter(
    "gate", "gate", 10, sys.argv[3])
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


@with_home
def case_inherit(home):
    w = gp.Pool(home, cfg()).enter("gate", "gate", 3, "/wt/a")
    check("inherit: a waiting ticket's descriptor is non-inheritable",
          not os.get_inheritable(w.fd))
    w.poll()
    same("inherit: no descriptor a holder holds (ticket and slots) is inheritable",
         [os.get_inheritable(fd) for fd in w.holder.fds], [False] * 4)
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
    plant(home, "0000000009-out.ticket", running, hold=True, sub="")
    rows = {"unset": "", "a missing file": h.path + "x.ticket",
            "an unlocked ticket": os.path.join(home, "waiters", "0000000007-dead.ticket"),
            "another protocol": os.path.join(home, "waiters", "0000000008-alien.ticket"),
            "outside waiters/": os.path.join(home, "0000000009-out.ticket"),
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
         (n.nest, n.child_env()), (h.path + ".nest", {"GATEQ_NEST": h.path + ".nest"}))
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
    other = plant(home, "0000000042-other.ticket.nest", "", hold=True)
    bad = {"unlocked": h.path + ".nest.nest", "missing": h.path + ".nest.nest.nest",
           "not this holder's": os.path.join(home, "waiters", "0000000042-other.ticket.nest")}
    tries = {k: pool.nested(4, "/wt/X", {**env, "GATEQ_NEST": v}, print) for k, v in bad.items()}
    same("nested chain: a GATEQ_NEST that is unlocked, missing or not this holder's is ignored: "
         "the run waits on its holder's own child lock", {k: t.poll() for k, t in tries.items()},
         dict.fromkeys(bad, False))
    n1.leave()
    same("nested chain: and takes that lock once it is free",
         (tries["unlocked"].poll(), tries["unlocked"].holder.nest), (True, h.path + ".nest"))
    tries["unlocked"].leave(); os.close(other); h.leave()


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


CASES = [
    ("cls", case_cls), ("order", case_order), ("clamp", case_clamp),
    ("table1", case_table1), ("table2", case_table2), ("class-matters", case_class_matters),
    ("protected", case_protected), ("worktree", case_worktree),
    ("class0-pass-list", case_class0_pass_list), ("bound-in-pass", case_bound_in_pass),
    ("cost-zero", case_cost_zero), ("pure", case_pure), ("static", case_static),
    ("disk-budget", case_disk_budget), ("disk-order", case_disk_order),
    ("disk-worktree", case_disk_worktree), ("sidecar", case_sidecar), ("sigkill", case_sigkill),
    ("inherit", case_inherit), ("budget-change", case_budget_change), ("root", case_root),
    ("no-git-under-lock", case_no_git_under_lock), ("worktree-key", case_worktree_key),
    ("acquire", case_acquire), ("foreign", case_foreign), ("nested-rows", case_nested_rows),
    ("nested-serial", case_nested_serial), ("nested-chain", case_nested_chain),
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
]


def _rerun(scripts_dir, *args):
    r = subprocess.run([sys.executable, os.path.abspath(__file__), *args], capture_output=True,
                       text=True, env={**os.environ, "GATE_POOL_SCRIPTS": scripts_dir})
    return r.returncode, r.stdout + r.stderr


def mutation_selftest():
    with open(MODULE, encoding="utf-8") as f:
        src = f.read()
    known = {name for name, _ in CASES}
    with tempfile.TemporaryDirectory(prefix="gate-pool-mut-") as tmp:
        clean = os.path.join(tmp, "clean"); os.mkdir(clean)
        with open(os.path.join(clean, "gate_pool.py"), "w", encoding="utf-8") as f:
            f.write(src)
        rc, out = _rerun(clean)
        check("mutation self-test: the UNMUTATED copy passes", rc == 0)
        if rc != 0:
            print(out)
            return
        for i, (label, old, new, case, want) in enumerate(MUTATIONS):
            if src.count(old) != 1 or case not in known:
                check(f"mutation self-test: '{label}' is stale "
                      f"({src.count(old)} occurrences of its text, case '{case}')", False)
                continue
            d = os.path.join(tmp, f"m{i:02d}"); os.mkdir(d)
            with open(os.path.join(d, "gate_pool.py"), "w", encoding="utf-8") as f:
                f.write(src.replace(old, new))
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
