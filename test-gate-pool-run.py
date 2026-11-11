#!/usr/bin/env python3
"""Harness for `gate-runner.py --pool-run <name>`: named heavy commands (epic #538, unit A /
#539, PR A3). Hand-rolled, stdlib-only, no pytest. Design of record:
skills/orchestrate/design/DESIGN-gate-pool.md ("the doc"), sections 3 and 5, and the A3 row
of its test-plan table, which this file follows item by item.

WHAT IS UNDER TEST. A heavy non-gate command (a race, mutation or fuzz run) is declared in the
repo's `.gates.toml` as a `[[pool.command]]` and started BY NAME. The runner accepts one
declared name and nothing else, takes the command's declared weight from the machine budget
and its worktree like a gate (kind `named`; the class comes from the weight alone), runs it,
passes its exit code through, and ends with ONE stderr line: `POOL-RUN: <name> ran exit=<n>`
or `POOL-RUN: <name> NOT RUN reason=<slug>`. With no budget configured it runs the command
directly: no ticket, no wait, and no pool module imported.

FIXTURES ARE test-gate-pool.py's. That harness is loaded as a module (`tp`): its isolation
pins run at import, BEFORE anything here (GATEQ_HOME pinned to an empty temp directory,
GATEQ_HOLDER / GATEQ_NEST and every GIT_ variable dropped, the real pool listed), and its
runner helpers (`runner_env`, `spawn`, `hold`, `gated`, `wait_for`) are used as they are, so
the two harnesses cannot drift on how a runner is isolated. Every runner here is spawned
through them: a temp HOME, a temp pool root, and never the real `~/.claude/gate-queue`. The
checks, the cases and the mutation table are this file's own.

NO SLEEP IS A SYNCHRONIZATION, and no test process dies of a core-dumping signal: only
SIGINT and SIGTERM are ever sent, and SIGKILL only by the fixtures' cleanup.

MUTATION SELF-TEST (the test-gate-pool.py pattern). The harness ends by copying the scripts
(and the four prose files that carry the declared-name rule) into a temp directory, breaking
ONE thing in a copy, and re-running itself against that directory with `--only <case>`. The
run must exit non-zero AND print the named check's FAIL line. An unmutated copy runs first
and must pass. No working-tree file is ever opened for writing.

PARTS (the #570 pattern, as test-gate-pool.py). Run whole this harness took ~250 s inside the
gate and set the floor of one macOS shard, so it also runs in independent parts, each a
`.gates.toml` step: `--part cases` (the cases on the working sources), `--part baseline`
(every case on a clean copy), and `--part mutations K/N` (the K-th round-robin slice,
`rows[K-1::N]`, of the mutation rows, with a prerequisite scoped to the cases that slice
uses). N is MUTATION_PARTS; test-ci-gates-lockstep.py checks that the steps cover every part
exactly once. A row's stable ID is (target file, label). No argument still runs everything,
in the old order.
"""

import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True             # loading the sibling harness must leave no .pyc

# The sibling harness, for its pins and fixtures. It honors GATE_POOL_SCRIPTS (a COPY of
# scripts/, set only by a mutation re-run), so `tp.RUNNER` is always the runner under test.
_spec = importlib.util.spec_from_file_location("_gate_pool_harness",
                                               os.path.join(HERE, "test-gate-pool.py"))
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)

# The prose that carries the declared-name rule; GATE_POOL_RUN_DOCS points at a copy.
DOCS = os.environ.get("GATE_POOL_RUN_DOCS") or os.path.join(HERE, "skills", "orchestrate")
RULE_FILES = ("templates/implementer-charter.md", "templates/adversarial-review-charter.md",
              "templates/adversarial-prep-charter.md", "SKILL.md")

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


# --- fixtures -----------------------------------------------------------------------------------
def command(name, run, weight=2, extra=""):
    """One `[[pool.command]]` table. Strings go through json.dumps (a TOML basic string takes
    the same escapes), so a name or a run may hold quotes, a newline or a non-ASCII letter."""
    w = "" if weight is None else f"weight = {weight}\n"
    return f"[[pool.command]]\nname = {json.dumps(name)}\nrun = {json.dumps(run)}\n{w}{extra}\n"


def touch(path):
    return f"echo x > '{path}'"


def pool_run(root, home, *args, env=None, runner=None):
    """Run the runner to its end -> (exit code, stdout, stderr)."""
    return tp.run_runner(root, home, *args, env=env, runner=runner)


def closing(err):
    """Every `POOL-RUN:` line on stderr. There must be exactly one, and it must be the last."""
    return [ln for ln in err.splitlines() if ln.startswith("POOL-RUN:")]


def last(err):
    return (err.splitlines() or [""])[-1]


def ended(err, line):
    """True when stderr ENDS with `line` and holds no other POOL-RUN: line."""
    return last(err) == line and closing(err) == [line]


def tickets_left(home):
    """The tickets under a pool root; none when the pool was never on there."""
    return tp.listing(home, suffix=".ticket") if os.path.isdir(os.path.join(home, "waiters")) else []


def nothing_in(home):
    """The pool root as a run that takes no ticket must leave it."""
    return sorted(os.listdir(home))


# --- cases --------------------------------------------------------------------------------------
@tp.e2e
def case_names(tmp, home):
    ran = os.path.join(tmp, "ran")
    long64 = "n" + "x" * 63
    root = tp.make_repo(tmp, "names", "".join([
        command("ok", touch(ran + ".ok")), command(long64, touch(ran + ".64")),
        command("dup", touch(ran + ".dup1")), command("dup", touch(ran + ".dup2")),
        # Declared under names the flag must refuse: a runner that drops the name rule runs them.
        command("a b", touch(ran + ".bad")), command("-x", touch(ran + ".bad")),
        command(".x", touch(ran + ".bad")), command("a/b", touch(ran + ".bad")),
        command("n" * 65, touch(ran + ".bad")), command("a\nb", touch(ran + ".bad")),
        command("é", touch(ran + ".bad")), command("", touch(ran + ".bad")),
    ]))
    rc, out, err = pool_run(root, home, "--pool-run", "ok")
    same("names: a declared name runs its command and ends with the one closing line",
         (rc, out, err, os.path.exists(ran + ".ok")), (0, "", "POOL-RUN: ok ran exit=0\n", True))
    rc, out, err = pool_run(root, home, "--pool-run=" + long64)
    same("names: the `--pool-run=<name>` spelling works, and a 64-character name is valid",
         (rc, last(err), os.path.exists(ran + ".64")), (0, f"POOL-RUN: {long64} ran exit=0", True))
    rc, out, err = pool_run(root, home, "--pool-run", "nope")
    same("names: an UNKNOWN name exits 2 and runs nothing",
         (rc, out, ended(err, "POOL-RUN: nope NOT RUN reason=unknown-name")), (2, "", True))
    rc, out, err = pool_run(root, home, "--pool-run", "dup")
    same("names: a DUPLICATED name exits 2 and runs neither declaration",
         (rc, ended(err, "POOL-RUN: dup NOT RUN reason=duplicate-name"),
          os.path.exists(ran + ".dup1"), os.path.exists(ran + ".dup2")), (2, True, False, False))
    got = []
    for bad in ("a b", "-x", ".x", "a/b", "n" * 65, "a\nb", "é", "",
                "ok\nPOOL-RUN: ok ran exit=0"):
        rc, out, err = pool_run(root, home, "--pool-run", bad)
        got.append((rc, ended(err, "POOL-RUN: <invalid> NOT RUN reason=bad-name")))
    same("names: a MALFORMED name exits 2, is never echoed (no second POOL-RUN: line can be "
         "forged) and runs nothing, even when a command is declared under it",
         (got, os.path.exists(ran + ".bad")), ([(2, True)] * 9, False))
    rc, out, err = pool_run(root, home, "--pool-run")
    same("names: `--pool-run` with no name exits 2",
         (rc, ended(err, "POOL-RUN: <invalid> NOT RUN reason=usage")), (2, True))
    bare = tp.make_repo(tmp, "bare")
    rc, out, err = pool_run(bare, home, "--pool-run", "ok")
    same("names: a repo with no .gates.toml declares nothing: exit 2",
         (rc, ended(err, "POOL-RUN: ok NOT RUN reason=unknown-name")), (2, True))
    broken = tp.make_repo(tmp, "broken", "[[pool.command]\n")
    rc, out, err = pool_run(broken, home, "--pool-run", "ok")
    same("names: a .gates.toml that does not parse exits 2",
         (rc, ended(err, "POOL-RUN: ok NOT RUN reason=config")), (2, True))
    same("names: none of this created anything under the pool root", nothing_in(home), [])


@tp.e2e
def case_argv(tmp, home):
    ran, inj = os.path.join(tmp, "ran"), os.path.join(tmp, "injected")
    root = tp.make_repo(tmp, "argv", command("ok", touch(ran)))
    receipt = tp.put(os.path.join(tmp, "receipt.json"), '{"result": "pass"}\n')
    extras = [["extra"], ["--", "x"], [f"; echo x > '{inj}'"], [f"$(echo x > '{inj}')"],
              [f"`echo x > '{inj}'`"], ["--verbose"], ["--pool-run", "ok"], ["--pool-run=ok"],
              ["--class", "0"], ["--priority=0"], ["--weight", "1"],
              ["x\nPOOL-RUN: ok ran exit=0"]]
    got = []
    for extra in extras:
        for argv in (["--pool-run", "ok", *extra], [*extra, "--pool-run", "ok"]):
            rc, out, err = pool_run(root, home, *argv)
            got.append((rc, out, ended(err, "POOL-RUN: ok NOT RUN reason=usage")))
    same("argv: ANY extra argument, before or after the name, exits 2 and reaches no shell: "
         "the command did not run and nothing an argument spelled was executed",
         (got, os.path.exists(ran), os.path.exists(inj)),
         ([(2, "", True)] * (2 * len(extras)), False, False))
    for flag, argv in (("--receipt", ["--receipt", receipt]), ("--receipt", ["--receipt=" + receipt]),
                       ("--skip", ["--skip", "s"]), ("--jobs", ["--jobs", "2"]),
                       ("--memoize-dir", ["--memoize-dir", tmp]), ("--shard", ["--shard", "1/2"])):
        got = []
        for full in (["--pool-run", "ok", *argv], [*argv, "--pool-run", "ok"]):
            rc, out, err = pool_run(root, home, *full)
            got.append((rc, ended(err, "POOL-RUN: ok NOT RUN reason=usage"),
                        f"--pool-run cannot be combined with {flag}" in err))
        same(f"argv: {argv[0].split('=')[0]}{'=' if '=' in argv[0] else ''} beside --pool-run "
             "exits 2 naming the flag, and the command does not run",
             (got, os.path.exists(ran)), ([(2, True, True)] * 2, False))
    same("argv: a named command never writes or touches a receipt: the older one at the path "
         "refused beside it is still there, unchanged",
         tp.read(receipt), '{"result": "pass"}\n')
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)
    rc, out, err = pool_run(root, home, "--pool-run", "ok", "--jobs", "2")
    same("argv: with the pool ON a refused invocation takes no ticket and creates nothing "
         "under the pool root", (rc, os.path.exists(ran), nothing_in(home)),
         (2, False, ["config.toml"]))


@tp.e2e
def case_command_table(tmp, home):
    ran = os.path.join(tmp, "ran")

    def refused(tag, text, slug="bad-command", name="c"):
        root = tp.make_repo(tmp, f"t-{tag}", text)
        rc, out, err = pool_run(root, home, "--pool-run", name)
        return rc, ended(err, f"POOL-RUN: {name} NOT RUN reason={slug}"), os.path.exists(ran)

    same("command table: a MISSING weight exits 2: there is no default for a heavy command",
         refused("noweight", command("c", touch(ran), weight=None)), (2, True, False))
    same("command table: a weight that is no positive integer exits 2 (zero, a negative, a "
         "bool, a string, a float)",
         [refused(f"w{i}", command("c", touch(ran), weight=w))
          for i, w in enumerate(("0", "-1", "true", '"2"', "2.5"))], [(2, True, False)] * 5)
    same("command table: a run that is missing, no string, or empty exits 2",
         [refused("norun", '[[pool.command]]\nname = "c"\nweight = 2\n'),
          refused("run3", '[[pool.command]]\nname = "c"\nrun = 3\nweight = 2\n'),
          refused("runempty", command("c", "  "))], [(2, True, False)] * 3)
    same("command table: a timeout_s that is no positive integer exits 2",
         [refused(f"to{i}", command("c", touch(ran), extra=f"timeout_s = {t}\n"))
          for i, t in enumerate(("0", "true", '"30"'))], [(2, True, False)] * 3)
    same("command table: an unknown key in the command's table exits 2 (a misspelled "
         "timeout must not silently drop the bound)",
         refused("typo", command("c", touch(ran), extra="timeout = 30\n")), (2, True, False))
    forged = tp.make_repo(tmp, "t-forged", command(
        "c", touch(ran), extra=json.dumps("x\nPOOL-RUN: c ran exit=0") + " = 1\n"))
    rc, out, err = pool_run(forged, home, "--pool-run", "c")
    same("command table: an unknown key that spells a closing line is echoed quoted: exactly "
         "ONE line starts with `POOL-RUN:`, and it says NOT RUN",
         (rc, closing(err), os.path.exists(ran)), (2, ["POOL-RUN: c NOT RUN reason=bad-command"], False))
    same("command table: a pool.command that is no array of tables, or an entry with no "
         "string name, exits 2",
         [refused("notarray", '[pool]\ncommand = "c"\n', "config"),
          refused("notable", "[pool]\ncommand = [1]\n", "config"),
          refused("noname", command("c", touch(ran)) + "[[pool.command]]\nname = 7\n", "config"),
          refused("pooltype", "pool = 3\n", "config")], [(2, True, False)] * 4)
    # A bad declaration is that invocation's problem alone.
    other = tp.make_repo(tmp, "other-bad",
                         command("c", touch(ran)) + command("worse", "exit 9", weight=None))
    rc, out, err = pool_run(other, home, "--pool-run", "c")
    same("command table: a bad declaration under ANOTHER name does not stop this one",
         (rc, last(err), os.path.exists(ran)), (0, "POOL-RUN: c ran exit=0", True))
    gate = '[prep_pr]\ngate = "echo gate-ran"\n'
    plain = tp.make_repo(tmp, "gate-plain", gate)
    mixed = tp.make_repo(tmp, "gate-mixed", gate + command("c", "exit 9", weight=None)
                         + command("c", "exit 9") + "[[pool.command]]\nname = 7\n")
    same("command table: a bad [[pool.command]] never affects the GATE: its output and exit "
         "code equal those of the same gate with no such table",
         tp.normalized(mixed, *tp.run_runner(mixed, home)),
         tp.normalized(plain, *tp.run_runner(plain, home)))


INFO = """
import json, os, sys
with open(sys.argv[1], "w") as f:
    json.dump({"leader": os.getsid(0) == os.getpid(), "sid": os.getsid(0),
               "stdin": sys.stdin.read(), "cwd": os.getcwd(),
               "devnull": os.path.samestat(os.fstat(0), os.stat(os.devnull)),
               "holder": os.environ.get("GATEQ_HOLDER"), "nest": os.environ.get("GATEQ_NEST")}, f)
"""


@tp.e2e
def case_exit_code(tmp, home):
    for code in (0, 3, 2, 75, 124):
        root = tp.make_repo(tmp, f"exit{code}", command("c", f"echo to-out; echo to-err >&2; exit {code}"))
        same(f"exit code: the command's own exit {code} passes through, its output is passed "
             "through untouched, and the closing line says it RAN",
             pool_run(root, home, "--pool-run", "c"),
             (code, "to-out\n", f"to-err\nPOOL-RUN: c ran exit={code}\n"))
    root = tp.make_repo(tmp, "killed", command("c", "kill -TERM $$"))
    rc, out, err = pool_run(root, home, "--pool-run", "c")
    same("exit code: a command that dies of a signal reads as 128 + the signal",
         (rc, last(err)), (143, "POOL-RUN: c ran exit=143"))
    same("exit code: with no budget configured none of these runs created anything under the "
         "pool root (no ticket, no wait)", nothing_in(home), [])
    # The no-import proof: the runner copied ALONE, with no gate_pool.py beside it.
    alone = os.path.join(tmp, "alone"); os.mkdir(alone)
    runner = shutil.copy(tp.RUNNER, alone)
    info = os.path.join(tmp, "info.json")
    root = tp.make_repo(tmp, "info", command("c", tp.py_step(tmp, "info.py", INFO, info)))
    deep = os.path.join(root, "deep", "er"); os.makedirs(deep)
    typed = tp.put(os.path.join(tmp, "typed"), "typed\n")
    p = tp.spawn(deep, home, "--pool-run", "c", stdin=typed)
    rc, rec = tp.ended(p), tp._load_json(info)
    same("exit code: the command runs in the repo ROOT whatever directory the caller was in",
         (rc, rec.get("cwd")), (0, root))
    # The spawned runner leads a session of its own, so its session id is its pid.
    same("exit code: the command is the leader of a SESSION of its own, not the runner's",
         (rec.get("leader"), rec.get("sid") not in (None, p.pid)), (True, True))
    same("exit code: its stdin is /dev/null, not the runner's",
         (rec.get("stdin"), rec.get("devnull")), ("", True))
    rc, out, err = pool_run(root, home, "--pool-run", "c", runner=runner)
    same("exit code: pool OFF runs the command with NO gate_pool.py beside the runner (the "
         "module is never imported), and leaves the runner's directory alone",
         (rc, err, os.listdir(alone)), (0, "POOL-RUN: c ran exit=0\n", ["gate-runner.py"]))


@tp.e2e
def case_pool_on(tmp, home):
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)
    run, started, fifo = tp.gated(tmp, "on")
    info = os.path.join(tmp, "info.json")
    step = tp.py_step(tmp, "info.py", INFO, info)[len("exec "):]
    root = tp.make_repo(tmp, "on", command("chk", f"{step}; {run}"))
    p = tp.spawn(root, home, "--pool-run", "chk")
    tp.wait_for("the named command to start", lambda: os.path.exists(started), p)
    held = tp.listing(home, suffix=".ticket")
    same("pool on: a named command takes a TICKET: kind `named`, its name, its declared "
         "weight as cost, its worktree, and it is the running holder",
         [(t.get("kind"), t.get("name"), t.get("cost"), t.get("worktree"), t.get("state"),
           t.get("pid")) for t in tp.tickets(home)], [("named", "chk", 2, root, "running", p.pid)])
    rec = tp._load_json(info)
    same("pool on: the command is handed its runner's OWN ticket as GATEQ_HOLDER and no "
         "GATEQ_NEST", (rec.get("holder"), rec.get("nest")),
         (os.path.join(home, "waiters", held[0]) if held else None, None))
    tp.let_go(fifo)
    same("pool on: it passes, says so, and gives its ticket and slots back",
         (p.wait(timeout=60), tp.read(p.err), tp.listing(home)),
         (0, "POOL-RUN: chk ran exit=0\n", []))
    run, started, fifo = tp.gated(tmp, "big")
    p = tp.spawn(tp.make_repo(tmp, "big", command("big", run, weight=12)), home,
                 "--pool-run", "big")
    tp.wait_for("the weight-12 command to start", lambda: os.path.exists(started), p)
    same("pool on: a weight above the budget is clamped to it: the command holds the WHOLE "
         "budget", [t.get("cost") for t in tp.tickets(home)], [10])
    tp.let_go(fifo)
    same("pool on: and ends like any other", (p.wait(timeout=60), tp.listing(home)), (0, []))


# Nothing a caller can set selects the class: not these variables, and no flag (case argv).
CLASS_ENV = {v: "0" for v in ("GATEQ_CLASS", "GATEQ_CLS", "GATEQ_PRIORITY", "GATEQ_KIND",
                              "GATEQ_SMALL", "POOL_CLASS", "POOL_RUN_CLASS", "GATE_CLASS")}
CLASS_ENV.update(GATEQ_WEIGHT="1", GATEQ_COST="1", GATEQ_SMALL_CHECK_CAP="99")


def _first_served(tmp, home, tag, weight, env=None):
    """The whole budget is held; a cost-10 GATE waits, then a named check of `weight` waits
    behind it (a higher seq). The holder goes away -> (who started first, the check's ticket
    cost, both exit codes). Class 0 is served ahead of the earlier gate; class 2 behind it."""
    holder = tp.hold(home, f"/wt/{tag}-holder")
    grun, gstart, gfifo = tp.gated(tmp, tag + "-g")
    g = tp.spawn(tp.make_repo(tmp, tag + "-g", tp.one_step(grun, "weight = 10")), home, env=env)
    tp.wait_for("the gate's wait line", lambda: "waiting for 10 of 10" in tp.read(g.err), g)
    nrun, nstart, nfifo = tp.gated(tmp, tag + "-n")
    n = tp.spawn(tp.make_repo(tmp, tag + "-n", command("chk", nrun, weight)), home,
                 "--pool-run", "chk", env=env)
    # Any wait line: a check that waits at the WRONG cost fails the cost check below by name.
    tp.wait_for("the check's wait line", lambda: "gate slots" in tp.read(n.err), n)
    cost = [t.get("cost") for t in tp.tickets(home) if t.get("kind") == "named"]
    tp.drop(holder)
    tp.wait_for("one of the two to start",
                lambda: os.path.exists(gstart) or os.path.exists(nstart), g, n)
    # Whichever started holds what the other needs, so exactly one start file exists until
    # this case lets the first go.
    first = [who for who, path in (("gate", gstart), ("check", nstart)) if os.path.exists(path)]
    turns = [(n, nfifo, nstart), (g, gfifo, gstart)]
    for proc, fifo, path in turns if first == ["check"] else turns[::-1]:
        tp.wait_for("the next to start", lambda: os.path.exists(path), proc)
        tp.let_go(fifo)
    return first, cost, [g.wait(timeout=60), n.wait(timeout=60)]


def _env_names(path):
    """Every environment variable NAME a script reads or writes by literal key."""
    src = tp.read(path)
    return sorted(set(re.findall(r"""environ(?:\.get\(|\.pop\(|\.setdefault\(|\[)\s*["']([^"']+)["']""", src))
                  | set(re.findall(r"""getenv\(\s*["']([^"']+)["']""", src)))


@tp.e2e
def case_class(tmp, home):
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)
    same("class: a check declared AT the cap (weight 2, cap 2) is class 0: served ahead of a "
         "gate that was waiting first", _first_served(tmp, home, "at", 2), (["check"], [2], [0, 0]))
    same("class: one ABOVE the cap (weight 3) is class 2: it waits behind the earlier gate, "
         "and no variable a caller sets changes that or its cost",
         _first_served(tmp, home, "above", 3, env=CLASS_ENV), (["gate"], [3], [0, 0]))
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG + "small_check_cap = 3\n")
    same("class: the cap is the MACHINE's: at small_check_cap = 3 the same weight 3 is class 0",
         _first_served(tmp, home, "cap3", 3), (["check"], [3], [0, 0]))
    same("class: the runner and the pool module read no environment variable but the pool "
         "root and the holder pair, so there is none to ask for a class with",
         (_env_names(tp.RUNNER), _env_names(tp.MODULE)),
         (["GATEQ_HOME", "GATEQ_NEST"], ["GATEQ_HOLDER", "GATEQ_NEST"]))


@tp.e2e
def case_worktree(tmp, home):
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)
    grun, gstart, gfifo = tp.gated(tmp, "g1")
    nrun, nstart, nfifo = tp.gated(tmp, "n1")
    root = tp.make_repo(tmp, "wt1", tp.one_step(grun, "weight = 2") + command("chk", nrun))
    g = tp.spawn(root, home)
    tp.wait_for("the gate to start", lambda: os.path.exists(gstart), g)
    n = tp.spawn(root, home, "--pool-run", "chk")
    line = f"gate-runner: waiting for this worktree (held by gate pid {g.pid})\n"
    # Either fact ends the wait, so a check that wrongly STARTS fails the check below by
    # name instead of timing out here.
    tp.wait_for("the check to wait or start", lambda: tp.read(n.err) != "" or os.path.exists(nstart), n)
    orun, ostart, ofifo = tp.gated(tmp, "o1")
    o = tp.spawn(tp.make_repo(tmp, "wt-other", command("chk", orun)), home, "--pool-run", "chk")
    tp.wait_for("a check in another worktree to start", lambda: os.path.exists(ostart), o)
    same("worktree: a check WAITS for the gate running in its worktree (8 of 10 units are "
         "free: it is the worktree it waits for), while a check elsewhere runs beside it",
         (tp.read(n.err), os.path.exists(nstart), os.path.exists(ostart)), (line, False, True))
    tp.let_go(gfifo); tp.let_go(ofifo)
    tp.wait_for("the check to start once the gate ended", lambda: os.path.exists(nstart), n)
    tp.let_go(nfifo)
    same("worktree: the check runs once the gate has ended, and all three pass",
         ([p.wait(timeout=60) for p in (g, o, n)], last(tp.read(n.err)), tp.listing(home)),
         ([0, 0, 0], "POOL-RUN: chk ran exit=0", []))
    # The other direction: the check holds the worktree, the gate waits for it.
    grun, gstart, gfifo = tp.gated(tmp, "g2")
    nrun, nstart, nfifo = tp.gated(tmp, "n2")
    root = tp.make_repo(tmp, "wt2", tp.one_step(grun, "weight = 2") + command("chk", nrun))
    n = tp.spawn(root, home, "--pool-run", "chk")
    tp.wait_for("the check to start", lambda: os.path.exists(nstart), n)
    g = tp.spawn(root, home)
    tp.wait_for("the gate to wait or start", lambda: tp.read(g.err) != "" or os.path.exists(gstart), g)
    same("worktree: a gate WAITS for the check running in its worktree",
         (tp.read(g.err), os.path.exists(gstart)),
         (f"gate-runner: waiting for this worktree (held by named pid {n.pid})\n", False))
    tp.let_go(nfifo)
    tp.wait_for("the gate to start once the check ended", lambda: os.path.exists(gstart), g)
    tp.let_go(gfifo)
    same("worktree: the gate runs once the check has ended, and both pass",
         ([p.wait(timeout=60) for p in (n, g)], tp.listing(home)), ([0, 0], []))


NESTED_STEP = """
"$1" -B "$2" --pool-run small > "$3/small.out" 2>&1; echo $? > "$3/small.rc"
"$1" -B "$2" --pool-run big > "$3/big.out" 2>&1; echo $? > "$3/big.rc"
"""


@tp.e2e
def case_nested(tmp, home):
    # wait_timeout_s bounds the one way this can go wrong: an inner run that is NOT taken as
    # nested waits on the worktree its own gate holds, and gives up with another reason.
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG + "wait_timeout_s = 5\n")
    seen = os.path.join(tmp, "seen"); os.mkdir(seen)
    script = tp.put(os.path.join(tmp, "step.sh"), NESTED_STEP)
    inner = (f"ls \"$GATEQ_HOME/waiters\" > '{seen}/ls'; "
             f"echo \"${{GATEQ_HOLDER-unset}}|${{GATEQ_NEST-unset}}\" > '{seen}/env'")
    root = tp.make_repo(tmp, "nested", tp.one_step(
        f"sh '{script}' '{sys.executable}' '{tp.RUNNER}' '{seen}'", "weight = 4")
        + command("small", inner, weight=4) + command("big", touch(seen + "/big.ran"), weight=5))
    rc, out, err = tp.run_runner(root, home)
    ls = tp.read(os.path.join(seen, "ls")).split()
    ticket = next((n for n in ls if n.endswith(".ticket")), "?")
    # Since A4 each runner with a watchdog also holds a `.sweep` claim: the gate's own
    # (`<ticket>.sweep`) and the nested run's (`<ticket>.<its pid>.sweep`).
    own_sweep = [n for n in ls if re.fullmatch(re.escape(ticket) + r"\.[0-9]+\.sweep", n)]
    same("nested: --pool-run inside a gate step, at a weight the gate's own covers, runs "
         "UNDER the gate (GATEQ_HOLDER): no ticket of its own, one child lock, exit 0",
         (rc, tp.read(os.path.join(seen, "small.rc")), tp.read(os.path.join(seen, "small.out")),
          sorted(n for n in ls if n not in own_sweep), len(own_sweep)),
         (0, "0\n", "POOL-RUN: small ran exit=0\n",
          [ticket, ticket + ".nest", ticket + ".sweep"], 1))
    path = os.path.join(home, "waiters", ticket)
    same("nested: its command is handed the holder it runs under and the lock it holds",
         tp.read(os.path.join(seen, "env")), f"{path}|{path}.nest\n")
    same("nested: one heavier than the gate exits 75 AT ONCE (it gives up for its weight, "
         "not for the wait), labeled NOT RUN, and its command never runs",
         (tp.read(os.path.join(seen, "big.rc")), tp.read(os.path.join(seen, "big.out")),
          os.path.exists(seen + "/big.ran")),
         ("75\n", "gate-runner: NOT RUN reason=nested-over-holder holder=4 needs=5\n"
                  "POOL-RUN: big NOT RUN reason=nested-over-holder\n", False))
    same("nested: the gate itself passes and no ticket is left", (err, tickets_left(home)),
         ("", []))
    # A holder leaked from ANOTHER worktree exempts nothing: its own ticket, and a line.
    tp.hold(home, "/wt/leak", cost=1)
    leaked = os.path.join(home, "waiters", tp.listing(home, suffix=".ticket")[0])
    env_file = os.path.join(seen, "env")
    rc, out, err = pool_run(root, home, "--pool-run", "small", env={"GATEQ_HOLDER": leaked})
    own = tp.read(env_file).split("|")[0]
    same("nested: a GATEQ_HOLDER leaked from another worktree is ignored with one line: the "
         "command takes its OWN ticket",
         (rc, err, own.endswith(".ticket") and own != leaked),
         (0, "gate-runner: ignoring GATEQ_HOLDER, its holder is in another worktree (/wt/leak); "
             "taking a ticket\nPOOL-RUN: small ran exit=0\n", True))


def _overrun(tmp, home, tag, extra=""):
    """A command that outlasts its bound -> (exit code, the last two stderr lines, its
    shell gone)."""
    pidf = os.path.join(tmp, tag + ".pid")
    root = tp.make_repo(tmp, tag, command("slow", f"echo $$ > '{pidf}'; sleep 10", extra=extra))
    p = tp.spawn(root, home, "--pool-run", "slow")
    rc = tp.ended(p, timeout=40)
    pid = tp._pid_of(pidf)
    return rc, tp.read(p.err).splitlines()[-2:], bool(pid) and tp.gone(pid)


@tp.e2e
def case_timeout(tmp, home):
    def want(bound):
        return (124, [f"gate-runner: KILLED - 'slow' exceeded its timeout ({bound}s)",
                      "POOL-RUN: slow ran exit=124"], True)
    same("timeout: with the pool OFF a command that outlasts its timeout_s is killed with its "
         "group and reported as ran exit=124", _overrun(tmp, home, "off", "timeout_s = 1\n"), want(1))
    huge = tp.make_repo(tmp, "huge", command("h", "echo huge-ran", extra=f"timeout_s = 1{'0' * 400}\n"))
    same("timeout: a timeout_s too large for the clock is a bound that never comes, not a "
         "crash: the command runs and its exit code passes through",
         pool_run(huge, home, "--pool-run", "h"), (0, "huge-ran\n", "POOL-RUN: h ran exit=0\n"))
    quick = tp.make_repo(tmp, "quick", command("q", "sleep 2; echo outlived"))
    tp.put(os.path.join(home, "config.toml"), "[pool]\nprotocol = 1\njob_timeout_s = 1\n")
    same("timeout: with the pool OFF and no timeout_s there is NO run timeout (job_timeout_s "
         "is the pool's)", pool_run(quick, home, "--pool-run", "q"),
         (0, "outlived\n", "POOL-RUN: q ran exit=0\n"))
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG + "job_timeout_s = 1\n")
    same("timeout: with the pool ON and no timeout_s the bound is job_timeout_s",
         (_overrun(tmp, home, "on"), tp.listing(home)), (want(1), []))
    own = tp.make_repo(tmp, "own", command("q", "sleep 2; echo outlived", extra="timeout_s = 30\n"))
    same("timeout: a declared timeout_s wins over job_timeout_s",
         pool_run(own, home, "--pool-run", "q"), (0, "outlived\n", "POOL-RUN: q ran exit=0\n"))
    # The bound is counted from the GRANT. The wait below must outlast timeout_s, so this
    # sleep is the duration under test, not a synchronization: the wait line is already seen.
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)
    waited = tp.make_repo(tmp, "waited", command("w", "sleep 1; echo ran-after-wait",
                                                 extra="timeout_s = 4\n"))
    holder = tp.hold(home, "/wt/bound-holder")
    p = tp.spawn(waited, home, "--pool-run", "w")
    tp.wait_for("the wait line", lambda: "waiting for 2 of 10" in tp.read(p.err), p)
    time.sleep(4.5)
    tp.drop(holder)
    same("timeout: the bound is counted from the GRANT: a wait longer than timeout_s is not "
         "charged to the run, which runs to its end",
         (tp.ended(p, timeout=40), tp.read(p.out), last(tp.read(p.err)), tp.listing(home)),
         (0, "ran-after-wait\n", "POOL-RUN: w ran exit=0", []))


def _signalled(tmp, home, tag, sig):
    """One named SIG_STEP command; `sig` goes to the RUNNER alone -> (exit status, what the
    command recorded, last stderr line, tickets left)."""
    started, record = os.path.join(tmp, tag + ".started"), os.path.join(tmp, tag + ".record")
    root = tp.make_repo(tmp, tag, command("s", tp.py_step(tmp, "sig.py", tp.SIG_STEP, started, record)))
    p = tp.spawn(root, home, "--pool-run", "s")
    tp.wait_for(f"the {tag} command to start", lambda: os.path.exists(started), p)
    os.kill(p.pid, sig)                    # the runner, never its session: the command is not in it
    return tp.ended(p, timeout=20), tp.read(record), last(tp.read(p.err)), tickets_left(home)


@tp.e2e
def case_signals(tmp, home):
    same("signals: SIGTERM sent to the runner is FORWARDED to the command's session, and the "
         "runner ends 128 + the signal with a closing line that says the command ran",
         _signalled(tmp, home, "term-off", signal.SIGTERM),
         (143, "SIGTERM", "POOL-RUN: s ran exit=143", []))
    same("signals: SIGINT likewise: exit 130, never a traceback in place of the closing line",
         _signalled(tmp, home, "int-off", signal.SIGINT),
         (130, "SIGINT", "POOL-RUN: s ran exit=130", []))
    pidf, go = os.path.join(tmp, "bg.pid"), os.path.join(tmp, "bg.go")
    root = tp.make_repo(tmp, "bg", command(
        "bg", f"sleep 60 & echo $! > '{pidf}'; while [ ! -e '{go}' ]; do sleep 0.05; done"))
    # Spawned with its output in FILES: through a pipe the runner's caller would wait for the
    # background job (it holds the pipe open), and by then nothing could be told apart. The
    # watchdog (A4) is STOPPED before the command ends: its end-of-run kill of a still-listed
    # session would otherwise stand in for a skipped sweep.
    p = tp.spawn(root, home, "--pool-run", "bg")
    tp.wait_for("the bg command's job", lambda: tp._pid_of(pidf) != 0, p)
    with tp.watchdog_stopped(p.pid):
        tp.put(go, "x\n")
        rc, pid = tp.ended(p), tp._pid_of(pidf)
        swept = bool(pid) and tp.gone(pid, timeout=3.0)
    same("signals: a background job the command leaves behind is swept before the closing line",
         (rc, tp.read(p.err), swept), (0, "POOL-RUN: bg ran exit=0\n", True))
    if pid and tp.alive(pid):              # a mutant's survivor: seen alive just now
        os.kill(pid, signal.SIGTERM)
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)
    same("signals: with the pool ON a signalled run gives its ticket back before it ends",
         _signalled(tmp, home, "term-on", signal.SIGTERM),
         (143, "SIGTERM", "POOL-RUN: s ran exit=143", []))


@tp.e2e
def case_not_run(tmp, home):
    ran = os.path.join(tmp, "ran")
    root = tp.make_repo(tmp, "nr", command("c", f"{touch(ran)}; exit 75"))
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG + "wait_timeout_s = 1\n")
    holder = tp.hold(home, "/wt/elsewhere")
    same("not run: a wait that outlasts wait_timeout_s exits 75 and the closing line says NOT "
         "RUN: the command never started",
         (pool_run(root, home, "--pool-run", "c"), os.path.exists(ran),
          len(tp.listing(home, suffix=".ticket"))),
         ((75, "", "gate-runner: waiting for 2 of 10 gate slots (0 free, 0 ahead)\n"
                   "gate-runner: NOT RUN - no gate slot within 1s\n"
                   "POOL-RUN: c NOT RUN reason=wait-timeout\n"), False, 1))
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)   # the default wait: only the interrupt ends it
    p = tp.spawn(root, home, "--pool-run", "c")
    tp.wait_for("the wait line", lambda: "waiting for 2 of 10" in tp.read(p.err), p)
    os.kill(p.pid, signal.SIGINT)
    same("not run: an interrupt while WAITING exits 130, NOT RUN, with no traceback; the "
         "ticket is given up", (p.wait(timeout=60), tp.read(p.err).splitlines()[1:],
                                os.path.exists(ran), len(tp.listing(home, suffix=".ticket"))),
         (130, ["gate-runner: NOT RUN - interrupted while waiting for a gate slot",
                "POOL-RUN: c NOT RUN reason=interrupted"], False, 1))
    tp.drop(holder)
    same("not run: the SAME command's own exit 75 is told apart by the closing line: it RAN",
         (pool_run(root, home, "--pool-run", "c"), os.path.exists(ran)),
         ((75, "", "POOL-RUN: c ran exit=75\n"), True))
    os.unlink(ran)
    tp.put(os.path.join(home, "config.toml"), "[pool]\nprotocol = 1\nbudegt = 10\n")
    rc, out, err = pool_run(root, home, "--pool-run", "c")
    same("not run: a machine config error exits 2, NOT RUN: a typo never runs the command "
         "unpooled", (rc, ended(err, "POOL-RUN: c NOT RUN reason=pool-config"),
                      os.path.exists(ran)), (2, True, False))
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)
    alone = os.path.join(tmp, "alone"); os.mkdir(alone)
    rc, out, err = pool_run(root, home, "--pool-run", "c", runner=shutil.copy(tp.RUNNER, alone))
    same("not run: a budget configured and no gate_pool.py beside the runner exits 2, NOT "
         "RUN, never unpooled",
         (rc, err, os.path.exists(ran)),
         (2, "gate-runner: NOT RUN - pool configured but gate_pool.py is missing\n"
             "POOL-RUN: c NOT RUN reason=pool-module-missing\n", False))


@tp.e2e
def case_edited(tmp, home):
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)
    mark = os.path.join(tmp, "which")
    root = tp.make_repo(tmp, "ed", command("e", f"echo old > '{mark}'"))
    gates = os.path.join(root, ".gates.toml")

    def waiting():
        holder = tp.hold(home, f"/wt/holder-{next(tp._SERIAL)}")
        p = tp.spawn(root, home, "--pool-run", "e")
        tp.wait_for("the wait line", lambda: "waiting for 2 of 10" in tp.read(p.err), p)
        return holder, p

    holder, p = waiting()
    tp.put(gates, command("e", f"echo new > '{mark}'"))
    tp.drop(holder)
    same("edited: a command edited during the wait is re-read after the grant, and THAT "
         "definition runs", (p.wait(timeout=60), tp.read(mark)), (0, "new\n"))
    os.unlink(mark)
    holder, p = waiting()
    tp.put(gates, command("e", f"echo grown > '{mark}'", weight=8))
    tp.drop(holder)
    same("edited: one that costs MORE after the wait than the run holds is NOT RUN, exit 75: "
         "never wider than its slots, and the ticket is given back",
         (p.wait(timeout=60), tp.read(p.err).splitlines()[1:], os.path.exists(mark),
          tp.listing(home)),
         (75, ["gate-runner: NOT RUN - .gates.toml changed during the wait (cost 2 -> 8); "
               "run it again", "POOL-RUN: e NOT RUN reason=changed-during-wait"], False, []))
    tp.put(gates, command("e", f"echo old > '{mark}'"))
    holder, p = waiting()
    tp.put(gates, command("other", f"echo other > '{mark}'"))
    tp.drop(holder)
    same("edited: a name that is gone after the wait exits 2, NOT RUN, ticket given back",
         (p.wait(timeout=60), last(tp.read(p.err)), os.path.exists(mark), tp.listing(home)),
         (2, "POOL-RUN: e NOT RUN reason=unknown-name", False, []))


@tp.e2e
def case_launch(tmp, home):
    # Two declarations the shell can never be started with: an argument past the kernel's
    # limit (an OSError, E2BIG) and a NUL (a ValueError, raised before any fork).
    root = tp.make_repo(tmp, "launch", command("long", "echo " + "x" * (2 * 1024 * 1024))
                        + command("nul", "echo hi\x00there"))
    got = [pool_run(root, home, "--pool-run", n) for n in ("long", "nul")]
    same("launch: a command that cannot be LAUNCHED (too long for the kernel, or holding a "
         "NUL) exits 2 and the closing line says NOT RUN reason=launch-failed: no traceback",
         [(rc, out, ended(err, f"POOL-RUN: {n} NOT RUN reason=launch-failed"), "Traceback" in err)
          for n, (rc, out, err) in zip(("long", "nul"), got)], [(2, "", True, False)] * 2)
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)
    rc, out, err = pool_run(root, home, "--pool-run", "nul")
    same("launch: with the pool ON the same failure gives its ticket and slots back",
         (rc, ended(err, "POOL-RUN: nul NOT RUN reason=launch-failed"), tp.listing(home)),
         (2, True, []))


PRECHECK = "grep -q -- '--pool-run'"
_DISPATCH = '    if any(a == "--pool-run" or a.startswith("--pool-run=") for a in argv):'


def _precheck(path):
    """The caller's pre-check, run as the charters spell it."""
    return subprocess.run(["grep", "-q", "--", "--pool-run", path], check=False).returncode


@tp.e2e
def case_stale_leg(tmp, home):
    same("stale leg: the runner under test passes the callers' pre-check", _precheck(tp.RUNNER), 0)
    src = tp.read(tp.RUNNER)
    stripped = tp.put(os.path.join(tmp, "stripped.py"),
                      "".join(ln for ln in src.splitlines(True) if "--pool-run" not in ln))
    same("stale leg: a runner that does not contain the flag FAILS the pre-check, so the "
         "caller skips that leg like a missing file", _precheck(stripped), 1)
    # What the pre-check is for: a runner whose main() does not handle the flag (every copy
    # that predates it) warns, ignores it and the name, and runs the FULL GATE.
    stale = os.path.join(tmp, "stale"); os.mkdir(stale)
    same("stale leg: the dispatch this case disables occurs exactly once", src.count(_DISPATCH), 1)
    runner = tp.put(os.path.join(stale, "gate-runner.py"), src.replace(_DISPATCH, "    if False:"))
    gate_ran, cmd_ran = os.path.join(tmp, "gate.ran"), os.path.join(tmp, "cmd.ran")
    root = tp.make_repo(tmp, "stale-repo", tp.one_step(touch(gate_ran)) + command("ok", touch(cmd_ran)))
    rc, out, err = pool_run(root, home, "--pool-run", "ok", runner=runner)
    same("stale leg: a runner that does not handle the flag runs the FULL GATE, exits 0, and "
         "prints NO `POOL-RUN:` line: the absent line is the only tell",
         (rc, os.path.exists(gate_ran), os.path.exists(cmd_ran), closing(err),
          "unrecognized argument '--pool-run'" in err), (0, True, False, [], True))
    rule = {name: tp.read(os.path.join(DOCS, name)) for name in RULE_FILES}
    same("stale leg: the three charters and SKILL.md each carry the declared-name rule: the "
         "pre-check as a command, the closing line, that no such line is never a pass, and that "
         "it runs from the target worktree",
         {name: (PRECHECK in text, "POOL-RUN: <name> ran exit=<n>" in text,
                 "No such line is never a pass" in text, "the outcome is UNKNOWN" in text,
                 "target worktree as the working directory" in text, "--pool-run <name>" in text)
          for name, text in rule.items()}, {name: (True,) * 6 for name in RULE_FILES})
    same("stale leg: and each states the limit: a raw heavy command is neither budgeted nor "
         "excluded, and nothing detects it",
         {name: "is NOT budgeted and NOT excluded" in text for name, text in rule.items()},
         {name: True for name in RULE_FILES})


CASES = [
    ("names", case_names), ("argv", case_argv), ("command-table", case_command_table),
    ("exit-code", case_exit_code), ("pool-on", case_pool_on), ("class", case_class),
    ("worktree", case_worktree), ("nested", case_nested), ("timeout", case_timeout),
    ("signals", case_signals), ("not-run", case_not_run), ("launch", case_launch),
    ("edited", case_edited),
    ("stale-leg", case_stale_leg),
]

# --- mutation self-test: each assertion must be able to FAIL ------------------------------------
# (label, file, [(old, new), ...], case to run, the check whose FAIL line the run must print).
# Every `old` must occur EXACTLY ONCE in its file, or the row is stale. The first five are the
# mutations the doc's A3 test plan names.
R, M = "gate-runner.py", "gate_pool.py"
_ARGV_FAIL = ("argv: ANY extra argument, before or after the name, exits 2 and reaches no shell: "
              "the command did not run and nothing an argument spelled was executed")
_EXIT0 = ("exit code: the command's own exit 0 passes through, its output is passed through "
          "untouched, and the closing line says it RAN")
_ABOVE = ("class: one ABOVE the cap (weight 3) is class 2: it waits behind the earlier gate, and "
          "no variable a caller sets changes that or its cost")
_AT_CAP = ("class: a check declared AT the cap (weight 2, cap 2) is class 0: served ahead of a gate "
           "that was waiting first")
_CHECK_WAITS = ("worktree: a check WAITS for the gate running in its worktree (8 of 10 units are "
                "free: it is the worktree it waits for), while a check elsewhere runs beside it")
_MALFORMED = ("names: a MALFORMED name exits 2, is never echoed (no second POOL-RUN: line can be "
              "forged) and runs nothing, even when a command is declared under it")
_HANDED = ("pool on: the command is handed its runner's OWN ticket as GATEQ_HOLDER and no "
           "GATEQ_NEST")
_T_OFF = ("timeout: with the pool OFF a command that outlasts its timeout_s is killed with its "
          "group and reported as ran exit=124")
_RULE = ("stale leg: the three charters and SKILL.md each carry the declared-name rule: the "
         "pre-check as a command, the closing line, that no such line is never a pass, and that "
         "it runs from the target worktree")
_KEY = "            key = gate_pool.worktree_key(root)"
_ENTER = '_pool_enter(machine, root, cost, "named", name)'
_STATUS = "status = proc.returncode if proc.returncode >= 0 else 128 - proc.returncode"
_LIMIT0 = 'holder, env, limit = None, None, cmd.get("timeout_s")'
_LIMIT1 = 'limit = cmd.get("timeout_s", machine.get("job_timeout_s", JOB_TIMEOUT_S))'
_NOTE = ("            except OSError as e:           # the kernel drops the locks at exit anyway\n"
         '                _pool_say(f"gate-runner: note: could not release the gate slots: {e}")')
MUTATIONS = [
    # --- the five the doc names ---
    ("argv forwarded to the command", R,
     [("    if rest:                               # by name", "    if False:                              # by name"),
      ('_run_group(cmd["run"], root, session=True, env=env)',
       '_run_group(cmd["run"] + " " + " ".join(rest), root, session=True, env=env)')],
     "argv", _ARGV_FAIL),
    ("the class taken from an environment variable", M,
     [("        return 0 if e.weight <= cap else 2",
       '        return 0 if os.environ.get("GATEQ_CLASS") == "0" or e.weight <= cap else 2')],
     "class", _ABOVE),
    ("the worktree claim skipped for named commands", R,
     [(_KEY, _KEY + ' + ("" if kind == "gate" else f"#{os.getpid()}")')], "worktree", _CHECK_WAITS),
    ("a missing weight treated as 1", R,
     [('    if not _valid_jobs(cmd.get("weight")):', '    if not _valid_jobs(cmd.setdefault("weight", 1)):')],
     "command-table",
     "command table: a MISSING weight exits 2: there is no default for a heavy command"),
    ("the POOL-RUN: line omitted", R,
     [('    _pool_say(f"POOL-RUN: {shown} {what}")', "    pass")], "exit-code", _EXIT0),
    # --- the name, the arguments, the declaration ---
    ("a name the rule refuses is run anyway", R, [("    if shown != name:", "    if False:")], "names", _MALFORMED),
    ("an invalid name echoed into the closing line", R,
     [('shown = name if name is not None and re.fullmatch(POOL_RUN_NAME, name) else "<invalid>"',
       'shown = name or "<invalid>"')], "names", _MALFORMED),
    ("a duplicated name runs its first declaration", R,
     [("    if len(found) != 1:\n        if not found:", "    if not found:\n        if True:")], "names",
     "names: a DUPLICATED name exits 2 and runs neither declaration"),
    ("extra arguments ignored instead of refused", R,
     [("    if rest:                               # by name", "    if False:                              # by name")],
     "argv", _ARGV_FAIL),
    ("a gate flag beside --pool-run not named in the refusal", R,
     [('            if any(a == flag or a.startswith(flag + "=") for a in rest):', "            if False:")],
     "argv", "argv: --receipt beside --pool-run exits 2 naming the flag, and the command does not run"),
    ("a timeout_s that is no positive integer accepted", R,
     [('    if "timeout_s" in cmd and not _valid_jobs(cmd["timeout_s"]):', "    if False:")], "command-table",
     "command table: a timeout_s that is no positive integer exits 2"),
    ("an unknown key in the command's table accepted", R,
     [("    if unknown:\n        return bad(", "    if False:\n        return bad(")],
     "command-table", "command table: an unknown key in the command's table exits 2 (a misspelled "
     "timeout must not silently drop the bound)"),
    ("a run that is no non-empty string accepted", R,
     [('    if not isinstance(cmd.get("run"), str) or not cmd["run"].strip():', "    if False:")],
     "command-table", "command table: a run that is missing, no string, or empty exits 2"),
    ("an entry that is no table with a string name not refused", R,
     [('not isinstance(c, dict) or not isinstance(c.get("name"), str) for c in cmds)', "False for c in cmds)")],
     "command-table", "command table: a pool.command that is no array of tables, or an entry with no "
     "string name, exits 2"),
    # --- the run itself ---
    ("the command's exit code not passed through", R, [(_STATUS, "status = 0")], "exit-code",
     "exit code: the command's own exit 3 passes through, its output is passed through untouched, "
     "and the closing line says it RAN"),
    ("a command killed by a signal exits with the raw negative status", R,
     [(_STATUS, "status = proc.returncode")], "exit-code",
     "exit code: a command that dies of a signal reads as 128 + the signal"),
    ("the pool module imported with the pool off", R,
     [("    name, rest = _pool_run_args(argv)\n", "    name, rest = _pool_run_args(argv)\n    import gate_pool  # noqa: F401\n")],
     "exit-code", "exit code: pool OFF runs the command with NO gate_pool.py beside the runner (the "
     "module is never imported), and leaves the runner's directory alone"),
    ("the command run in the caller's directory, not the repo root", R,
     [('_run_group(cmd["run"], root, session=True, env=env)', '_run_group(cmd["run"], None, session=True, env=env)')],
     "exit-code", "exit code: the command runs in the repo ROOT whatever directory the caller was in"),
    ("the command run in a process group inside the runner's session", R,
     [('_run_group(cmd["run"], root, session=True, env=env)', '_run_group(cmd["run"], root, session=False, env=env)')],
     "exit-code", "exit code: the command is the leader of a SESSION of its own, not the runner's"),
    ("the command left on its start pipe instead of /dev/null", R,
     [("|| exit 1; exec </dev/null; exec /bin/sh", "|| exit 1; exec /bin/sh")],
     "exit-code", "exit code: its stdin is /dev/null, not the runner's"),
    ("a background job the command left behind is not swept", R,
     [("        _terminate_groups({}, [proc.pid])", "        pass")], "signals",
     "signals: a background job the command leaves behind is swept before the closing line"),
    ("a signalled run reports the command's own status", R,
     [("                    status = 128 + caught[0]", "                    status = proc.returncode")], "signals",
     "signals: SIGTERM sent to the runner is FORWARDED to the command's session, and the runner "
     "ends 128 + the signal with a closing line that says the command ran"),
    # --- the pool ---
    ("a named command's ticket says kind `gate`", R, [(_ENTER, _ENTER.replace('"named"', '"gate"'))], "pool-on",
     "pool on: a named command takes a TICKET: kind `named`, its name, its declared weight as "
     "cost, its worktree, and it is the running holder"),
    ("a named command classed as a gate (class 2 whatever its weight)", R,
     [(_ENTER, _ENTER.replace('"named"', '"gate"'))], "class", _AT_CAP),
    ("class 0 only BELOW the cap", M,
     [("        return 0 if e.weight <= cap else 2", "        return 0 if e.weight < cap else 2")], "class", _AT_CAP),
    ("an environment variable read by the pool module", M,
     [("        return 0 if e.weight <= cap else 2",
       '        return 0 if os.environ.get("GATEQ_CLASS") == "0" or e.weight <= cap else 2')],
     "class", "class: the runner and the pool module read no environment variable but the pool "
     "root and the holder pair, so there is none to ask for a class with"),
    ("the cost taken from an environment variable", R,
     [(_ENTER, _ENTER.replace("cost,", 'int(os.environ.get("GATEQ_COST", cost)),'))], "class", _ABOVE),
    ("the worktree claim skipped for named commands (the other direction)", R,
     [(_KEY, _KEY + ' + ("" if kind == "gate" else f"#{os.getpid()}")')], "worktree",
     "worktree: a gate WAITS for the check running in its worktree"),
    ("a weight above the budget not clamped", R,
     [('        cost = min(cmd["weight"], machine["budget"])', '        cost = cmd["weight"]')], "pool-on",
     "pool on: a weight above the budget is clamped to it: the command holds the WHOLE budget"),
    ("the command not handed its holder", R,
     [('                env = {**os.environ, **holder.child_env()}\n                if not env["GATEQ_NEST"]:\n'
       '                    del env["GATEQ_NEST"]\n', "                env = None\n")], "pool-on", _HANDED),
    ("an empty GATEQ_NEST handed to the command", R,
     [('                if not env["GATEQ_NEST"]:\n                    del env["GATEQ_NEST"]\n', "")],
     "pool-on", _HANDED),
    ("the ticket not given back", R,
     [("                holder.release()\n" + _NOTE + "\n    # After the release",
       "                pass\n" + _NOTE + "\n    # After the release")], "pool-on",
     "pool on: it passes, says so, and gives its ticket and slots back"),
    ("a nested run heavier than its holder runs anyway", M,
     [("        if have < need:", "        if False:")], "nested",
     "nested: one heavier than the gate exits 75 AT ONCE (it gives up for its weight, not for the "
     "wait), labeled NOT RUN, and its command never runs"),
    # --- the bound ---
    ("timeout_s ignored", R, [(_LIMIT0, _LIMIT0.replace('cmd.get("timeout_s")', "None"))], "timeout", _T_OFF),
    ("a killed command reported with another status than 124", R,
     [("                    status = EX_TIMED_OUT", "                    status = 1")], "timeout", _T_OFF),
    ("a run timeout with the pool off and no timeout_s", R,
     [(_LIMIT0, _LIMIT0.replace('cmd.get("timeout_s")', 'cmd.get("timeout_s", 1)'))], "timeout",
     "timeout: with the pool OFF and no timeout_s there is NO run timeout (job_timeout_s is the pool's)"),
    ("job_timeout_s not applied to a named command", R, [(_LIMIT1, 'limit = cmd.get("timeout_s")')],
     "timeout", "timeout: with the pool ON and no timeout_s the bound is job_timeout_s"),
    ("job_timeout_s wins over a declared timeout_s", R,
     [(_LIMIT1, 'limit = machine.get("job_timeout_s", JOB_TIMEOUT_S)')], "timeout",
     "timeout: a declared timeout_s wins over job_timeout_s"),
    ("the timeout_s overflow clamp dropped", R,
     [("time.monotonic() + min(limit, 10**9)}", "time.monotonic() + limit}")], "timeout",
     "timeout: a timeout_s too large for the clock is a bound that never comes, not a crash: the "
     "command runs and its exit code passes through"),
    ("the KILLED line omitted", R,
     [("""                    _pool_say(f"gate-runner: KILLED - {name!r} exceeded its timeout ({limit}s)")\n""", "")],
     "timeout", _T_OFF),
    ("the bound counted from before the wait, not from the grant", R,
     [(_LIMIT0 + "\n", _LIMIT0 + "\n    _t0 = time.monotonic()\n"),
      ("else time.monotonic() + min(limit, 10**9)}", "else _t0 + min(limit, 10**9)}")], "timeout",
     "timeout: the bound is counted from the GRANT: a wait longer than timeout_s is not charged to "
     "the run, which runs to its end"),
    ("a launch failure reported as ran exit=0", R,
     [("""                refused = (2, "launch-failed", f"could not launch {name!r}: {e}")""",
       "                status = 0")], "launch",
     "launch: a command that cannot be LAUNCHED (too long for the kernel, or holding a NUL) exits "
     "2 and the closing line says NOT RUN reason=launch-failed: no traceback"),
    ("a NUL in `run` is a traceback (only OSError caught at the launch)", R,
     [("            except (OSError, ValueError) as e:   # ValueError: a NUL in `run`",
       "            except OSError as e:")], "launch",
     "launch: a command that cannot be LAUNCHED (too long for the kernel, or holding a NUL) exits "
     "2 and the closing line says NOT RUN reason=launch-failed: no traceback"),
    ("an unknown key echoed raw into stderr", R,
     [("{unknown!r}", "{', '.join(unknown)}")], "command-table",
     "command table: an unknown key that spells a closing line is echoed quoted: exactly ONE line "
     "starts with `POOL-RUN:`, and it says NOT RUN"),
    # --- the wait ---
    ("the declaration costed before the wait is the one run", R,
     [("            cmd, bad = _named_command(root, name)\n            recost",
       "            _, bad = _named_command(root, name)\n            recost")], "edited",
     "edited: a command edited during the wait is re-read after the grant, and THAT definition runs"),
    ("a declaration that grew during the wait runs under the smaller hold", R,
     [("            elif recost > cost:", "            elif False:")], "edited",
     "edited: one that costs MORE after the wait than the run holds is NOT RUN, exit 75: never "
     "wider than its slots, and the ticket is given back"),
    ("a machine config error runs the command unpooled", R,
     [('    if state == "error":                   # a typo never runs', '    if False:                              # a typo never runs')],
     "not-run", "not run: a machine config error exits 2, NOT RUN: a typo never runs the command unpooled"),
    ("a refusal's closing line says the command ran", R,
     [('        _pool_run_line(shown, f"NOT RUN reason={slug}")', '        _pool_run_line(shown, f"ran exit={code}")')],
     "not-run", "not run: a wait that outlasts wait_timeout_s exits 75 and the closing line says NOT "
     "RUN: the command never started"),
    # --- the callers' pre-check and the prose ---
    ("the flag spelled another way (the pre-check finds no `--pool-run`)", R,
     [("--pool-run", "--pool_run", "all")], "stale-leg",
     "stale leg: the runner under test passes the callers' pre-check"),
    ("a charter without the pre-check", "templates/implementer-charter.md",
     [(PRECHECK, "grep -q pool-run")], "stale-leg", _RULE),
    ("SKILL.md without the closing-line rule", "SKILL.md",
     [("No such line is never a pass", "No such line is a warning")], "stale-leg", _RULE),
    ("a charter that reads a missing line as NOT RUN", "templates/adversarial-review-charter.md",
     [("the outcome is UNKNOWN", "it did NOT run")], "stale-leg", _RULE),
    ("a charter without the worktree cwd rule", "templates/adversarial-prep-charter.md",
     [("target worktree as the working directory", "runner")], "stale-leg", _RULE),
    ("a charter without the stated limit", "templates/adversarial-review-charter.md",
     [("is NOT budgeted and NOT excluded", "may not be budgeted")], "stale-leg",
     "stale leg: and each states the limit: a raw heavy command is neither budgeted nor excluded, "
     "and nothing detects it"),
]


def _rerun(d, *args):
    """Re-run this harness against the scripts and docs COPIES under `d`."""
    env = {**os.environ, "GATE_POOL_SCRIPTS": os.path.join(d, "scripts"),
           "GATE_POOL_RUN_DOCS": os.path.join(d, "docs")}
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args],
                           capture_output=True, text=True, env=env, timeout=600)
    except subprocess.TimeoutExpired:      # a mutant that hangs is a kill-less failure, not a hang
        return 124, "TIMEOUT: the re-run was killed after 600 s"
    return r.returncode, r.stdout + r.stderr


def _sources():
    """The scripts and the rule prose a re-run copies, read once from the working tree."""
    scripts = {name: tp.read(os.path.join(tp.SCRIPTS, name))
               for name in (M, R, "orchestrate_schemas.py")}
    docs = {name: tp.read(os.path.join(DOCS, name)) for name in RULE_FILES}
    return scripts, docs


def _copy_to(d, srcs, name=None, mutated=None):
    """A scripts directory and a docs directory for a re-run, with `name` replaced by
    `mutated` when given. COPIES, in a temp directory: no tracked file is ever written."""
    for sub, files in zip(("scripts", "docs"), srcs):
        for n, text in files.items():
            path = os.path.join(d, sub, n)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tp.put(path, mutated if n == name else text)
    return d


MUTATION_PARTS = 4   # how many `--part mutations K/N` slices .gates.toml runs; lockstep-checked


def mutation_rows():
    """Every mutation row, in the one declaration order the K/N slices index into. A row's
    stable ID is (target file, label); a duplicate ID is a harness bug."""
    rows = list(MUTATIONS)
    ids = [(m[1], m[0]) for m in rows]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise SystemExit(f"test-gate-pool-run.py: duplicate mutation row ID(s): {dup}")
    return rows


def part_rows(rows, k, n):
    """The K-th (1-based) of N round-robin slices of `rows`: an exact partition by construction."""
    return rows[k - 1::n]


def mutation_selftest(part=None):
    """The mutation self-test: the unmutated copy first, then each row against its own mutated
    copy. `part` = (K, N) runs only that slice of the rows, with a prerequisite scoped to the
    cases that slice uses; None runs everything (the full harness)."""
    known = {name for name, _ in CASES}
    srcs = _sources()
    all_rows = mutation_rows()
    # The lockstep checks only the STEP list, so a part_rows() edit that drops rows would keep
    # every gate green. Prove here, in every mode, that the MUTATION_PARTS slices cover each
    # row index exactly once.
    idx = list(range(len(all_rows)))
    covered = sorted(i for k in range(1, MUTATION_PARTS + 1)
                     for i in part_rows(idx, k, MUTATION_PARTS))
    check(f"mutation self-test: the {MUTATION_PARTS} part slices cover all "
          f"{len(all_rows)} rows exactly once", covered == idx)
    rows = all_rows if part is None else part_rows(all_rows, *part)
    # i is the row's index in the FULL list, so a mutant's directory name is part-independent.
    indexed = [(all_rows.index(r), r) for r in rows]
    with tempfile.TemporaryDirectory(prefix="gate-pool-run-mut-") as tmp:
        clean = _copy_to(os.path.join(tmp, "clean"), srcs)
        if part is None:
            rc, out = _rerun(clean)
            check("mutation self-test: the UNMUTATED copy passes", rc == 0)
        else:
            need = sorted({r[3] for _, r in indexed if r[3] in known})
            args = [x for c in need for x in ("--only", c)]
            rc, out = _rerun(clean, *args)
            check(f"mutation self-test: the UNMUTATED copy passes the {len(need)} case(s) "
                  f"part {part[0]}/{part[1]} uses", rc == 0)
        if rc != 0:
            print(out)
            return
        for i, (label, name, edits, case, want) in indexed:
            src = {**srcs[0], **srcs[1]}[name]
            # An edit is (old, new), `old` occurring exactly once, or (old, new, "all").
            counts = [src.count(e[0]) if len(e) == 2 else min(src.count(e[0]), 1) for e in edits]
            if counts != [1] * len(edits) or case not in known:
                check(f"mutation self-test: '{label}' is stale "
                      f"({counts} occurrences of its texts, case '{case}')", False)
                continue
            for e in edits:
                src = src.replace(e[0], e[1])
            rc, out = _rerun(_copy_to(os.path.join(tmp, f"m{i:02d}"), srcs, name, src),
                             "--only", case)
            killed = rc != 0 and f"[FAIL] {want}\n" in out
            check(f"mutation: {label} -> killed by '{case}'", killed)
            if not killed:
                print(f"         exit {rc}; wanted the FAIL line of: {want}")
    if part is not None:
        print(f"mutation part {part[0]}/{part[1]}: {len(rows)} of {len(all_rows)} rows")


USAGE = ("usage: test-gate-pool-run.py [--only <case>]... | --part cases | --part baseline | "
         "--part mutations K/N   (N = MUTATION_PARTS)")


def parse_part(words):
    """['cases'] | ['baseline'] | ['mutations', 'K/N'] -> (kind, (K, N) or None); None if
    malformed. N must equal MUTATION_PARTS, so a step list built for another N cannot run
    silently."""
    if words in (["cases"], ["baseline"]):
        return words[0], None
    if len(words) == 2 and words[0] == "mutations":
        m = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", words[1])
        if m and int(m.group(2)) == MUTATION_PARTS and 1 <= int(m.group(1)) <= MUTATION_PARTS:
            return "mutations", (int(m.group(1)), int(m.group(2)))
    return None


def baseline_selftest():
    """The full clean-copy run of every case (the unmutated prerequisite), as its own part."""
    with tempfile.TemporaryDirectory(prefix="gate-pool-run-base-") as tmp:
        rc, out = _rerun(_copy_to(os.path.join(tmp, "clean"), _sources()))
        check("baseline: the UNMUTATED copy passes every case", rc == 0)
        if rc != 0:
            print(out)


def main():
    args = sys.argv[1:]
    only, part = [], None
    while args:
        if args[0] == "--only" and len(args) >= 2 and part is None:
            only.append(args[1])
            args = args[2:]
        elif args[0] == "--part" and len(args) >= 2 and part is None and not only:
            words = args[1:3] if args[1] == "mutations" else args[1:2]
            part = parse_part(words)
            if part is None:
                print(f"test-gate-pool-run.py: bad --part {' '.join(words)!r}\n{USAGE}",
                      file=sys.stderr)
                return 2
            args = args[1 + len(words):]
        else:
            print(USAGE, file=sys.stderr)
            return 2
    if part is not None and os.environ.get("GATE_POOL_SCRIPTS"):
        print("test-gate-pool-run.py: --part is for the real tree, not a re-run against a copy",
              file=sys.stderr)
        return 2
    kind = part[0] if part else None
    selected = [(n, fn) for n, fn in CASES if (not only or n in only) and kind in (None, "cases")]
    unknown = [o for o in only if o not in {n for n, _ in CASES}]
    if unknown or (kind in (None, "cases") and not selected):
        print(f"test-gate-pool-run.py: no case named {unknown or only!r}", file=sys.stderr)
        return 2
    for name, fn in selected:
        print(f"{name}:")
        try:
            fn()
        except Exception as e:             # a crash is a FAIL line, never a lost verdict
            check(f"{name}: the case ran to its end (raised {type(e).__name__}: {e})", False)
    # Skipped inside a re-run against a copy, so the self-test never recurses.
    if kind == "baseline":
        print("baseline:")
        baseline_selftest()
    elif kind == "mutations":
        print(f"mutation self-test (part {part[1][0]}/{part[1][1]}):")
        mutation_selftest(part[1])
    elif not only and not os.environ.get("GATE_POOL_SCRIPTS") and kind is None:
        print("mutation self-test:")
        mutation_selftest()
    elif not only and kind is None:
        print("mutation self-test: skipped (GATE_POOL_SCRIPTS is set: this is a re-run "
              "against a copy)")

    print("isolation:")
    same("isolation: the pinned GATEQ_HOME is still empty", os.listdir(tp._PINNED_HOME), [])
    same("isolation: no spawned runner created anything under its (temp) HOME",
         os.listdir(tp._FAKE_HOME), [])
    same("isolation: the real pool directory is exactly as it was at the start",
         tp._real_pool_listing(), tp._REAL_POOL_BEFORE)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("All gate-pool-run harness checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
