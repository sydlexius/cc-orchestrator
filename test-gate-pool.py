#!/usr/bin/env python3
"""Harness for scripts/gate_pool.py, the machine-wide gate pool (epic #538, unit A / #539).
Hand-rolled, stdlib-only, no pytest. Design of record:
skills/orchestrate/design/DESIGN-gate-pool.md ("the doc").

THIS SLICE COVERS THE PURE POLICY ONLY: `cls()` and the three-class `schedule()` of the doc's
section 2. Nothing here opens a lock or writes a pool file; the on-disk pool, the nested-run
exemption and the gate-runner wiring arrive with their own slices and extend this file.

TABLE-DRIVEN. Every case builds synthetic entries, calls `schedule()` once, and compares the
WHOLE answer: which entries start, in which order, and which entries each start passes. The
two worked tables and the "when the class matters" example of section 2 are REPLAYED row by
row, asserting the start set AND the bypass counts after each row. A replay commits every
returned start (the way unit B's dispatcher will), so it also proves the bound of K holds
when one evaluator commits a whole batch.

ISOLATION. No test may touch the real pool at ~/.claude/gate-queue. The module has no default
home, so that is structural; three belts on top of it:
  1. GATEQ_HOME is pinned to a fresh empty temp directory and GATEQ_HOLDER / GATEQ_NEST are
     removed, before anything else runs, and the directory must still be empty at the end;
  2. the real pool directory (resolved from the passwd database, never from $HOME) is listed
     at the start and at the end, and any difference fails the harness;
  3. a case asserts the module source names neither `.claude` nor `expanduser`.

MUTATION SELF-TEST (the test-ci-gates-lockstep.py pattern). An assertion that cannot fail is
decorative, so the harness ends by copying scripts/gate_pool.py into a temp directory, breaking
ONE thing in the copy, and re-running itself against that copy with `--only <case>`. The run
must exit non-zero AND print the named check's FAIL line. An unmutated copy runs first and must
pass, so a broken fixture cannot read as a full set of kills. The working tree's file is never
opened for writing, so a concurrent `git add` can never capture a mutant.
"""

import os
import pwd
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# --- isolation pins: before the module under test is imported, before any case -------------
for _var in ("GATEQ_HOLDER", "GATEQ_NEST"):
    os.environ.pop(_var, None)
_PINNED_HOME = tempfile.mkdtemp(prefix="gate-pool-test-")
os.environ["GATEQ_HOME"] = _PINNED_HOME
_REAL_POOL = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".claude", "gate-queue")


def _real_pool_listing():
    try:
        return sorted(os.listdir(_REAL_POOL))
    except FileNotFoundError:
        return None


_REAL_POOL_BEFORE = _real_pool_listing()

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


CASES = [
    ("cls", case_cls), ("order", case_order), ("clamp", case_clamp),
    ("table1", case_table1), ("table2", case_table2), ("class-matters", case_class_matters),
    ("protected", case_protected), ("worktree", case_worktree),
    ("class0-pass-list", case_class0_pass_list), ("bound-in-pass", case_bound_in_pass),
    ("cost-zero", case_cost_zero), ("pure", case_pure), ("static", case_static),
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
    ("the pass's own starts no longer recorded",
     "pend[b] = pend.get(b, 0) + 1", "pass", "bound-in-pass",
     "bound: one pass over a gate at bypass 1 with four class-0 checks returns ONE start"),
    ("the c == 0 branch dropped",
     "if c == 0:", "if False:", "cost-zero",
     "cost 0: a zero-cost entry is not held by a protected entry ahead of it"),
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
        fn()
    # Skipped inside a re-run against a copy, so the self-test never recurses.
    if only is None and not os.environ.get("GATE_POOL_SCRIPTS"):
        print("mutation self-test:")
        mutation_selftest()

    print("isolation:")
    same("isolation: the pinned GATEQ_HOME is still empty", os.listdir(_PINNED_HOME), [])
    os.rmdir(_PINNED_HOME)
    same("isolation: the real pool directory is exactly as it was found",
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
