#!/usr/bin/env python3
"""Harness for the ORPHAN WATCHDOG (epic #538, unit A / #539, PR A4). Hand-rolled, stdlib-only,
no pytest. Design of record: skills/orchestrate/design/DESIGN-gate-pool.md ("the doc"),
section 4, "The orphan watchdog", and the A4 row of its section 9 test-plan table, which this
file follows item by item (the doctor half, "a copy without `POOL_WATCHDOG = 1` is not
pool-capable", lives with the rest of doctor in test-orchestrate-setup.py).

WHAT IS UNDER TEST. A step group runs in a process group of its own, so a runner killed by
SIGKILL would leave it running. gate-runner therefore starts one `python3 -c` child, the
watchdog, in a session of its own, fed `+<pgid>` / `-<pgid>` / `S<path>` over a pipe; on EOF it
SIGKILLs every group still listed and exits only once each is gone, which frees the `.sweep`
worktree claim it holds. A step starts HELD (it reads a start byte first), the byte is sent only
after `+<pgid>`, and a pruned pgid is never signalled. With the pool on, a holder's grant (and a
nested run's) takes a `.sweep` claim whose lock the WATCHDOG holds, and `busy` counts every held
`.sweep`, so a SIGKILLed holder's worktree stays claimed until its orphans are gone.

FIXTURES ARE test-gate-pool.py's (loaded as a module, `tp`): its isolation pins run at import
(GATEQ_HOME pinned to an empty temp directory, GATEQ_HOLDER / GATEQ_NEST and every GIT_
variable dropped, the real pool listed), and every runner here gets its environment from
`tp.runner_env`: a temp HOME and a temp pool root, never the real `~/.claude/gate-queue`.

TRACED COPIES. To see what the watchdog was told and what it signalled, a case runs a COPY of
the runner (in a temp directory, beside copies of gate_pool.py and orchestrate_schemas.py) whose
watchdog source has its no-op `trace()` replaced by one that appends each line to the file
GATE_WD_TRACE names, and (for the cases that need a group the first kill does not end) skips its
SIGKILLs for GATE_WD_IGNORE_KILL_S seconds after EOF. That stands in for a process the kernel
will not kill, which no test can make on purpose. No working-tree file is ever opened for
writing.

NO TEST PROCESS DIES OF A CORE-DUMPING SIGNAL: only SIGKILL and SIGTERM are ever sent. Every
step group a case starts is killed in a `finally`, and the end of the run asserts that no
`sleep 59.<this pid>` (this harness's own) is left anywhere.

MUTATION SELF-TEST (the test-gate-pool.py pattern). The harness ends by copying the scripts into
a temp directory, breaking ONE thing in a copy, and re-running itself against it with
`--only <case>`. The run must exit non-zero AND print the named check's FAIL line. An unmutated
copy runs first and must pass. The re-runs go RERUN_JOBS at a time: they mostly wait on facts
(a group's death, a waiter's start), not on the CPU.
"""

import concurrent.futures
import contextlib
import fcntl
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True             # loading the sibling harness must leave no .pyc

_spec = importlib.util.spec_from_file_location("_gate_pool_harness",
                                               os.path.join(HERE, "test-gate-pool.py"))
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)
gp = tp.gp                                  # gate_pool, from the scripts under test

FAILS = []
# Every long step here runs this, unique to THIS process (mutation re-runs run side by side, and
# each kills its own leftovers by this token alone); none may outlive the run.
SLEEP = f"sleep 59.{os.getpid()}"
_SLEEP_RE = re.escape(SLEEP) + "( |$)"


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def same(name, got, want):
    check(name, got == want)
    if got != want:
        print(f"         got  {got!r}\n         want {want!r}")


# --- fixtures -----------------------------------------------------------------------------------
_GROUPS = []                                # every step pgid a case learned, killed in its finally


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def group_gone(pgid, timeout):
    """True once group `pgid` is gone, waiting at most `timeout` seconds."""
    end = time.monotonic() + timeout
    while group_alive(pgid):
        if time.monotonic() > end:
            return False
        time.sleep(0.02)
    return True


def kill_groups():
    for g in _GROUPS:
        with contextlib.suppress(OSError):
            os.killpg(g, signal.SIGKILL)
    del _GROUPS[:]
    for pid in leftovers():                 # a mutant may leave one no case learned of
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def leftovers():
    """Every live process whose command line carries this harness's own long sleep."""
    r = subprocess.run(["pgrep", "-f", _SLEEP_RE], capture_output=True, text=True, check=False)
    return [int(x) for x in r.stdout.split()]


def case_of(fn):
    """tp.e2e, plus: every step group the case learned is killed on every way out."""
    def body(tmp, home):
        try:
            fn(tmp, home)
        finally:
            kill_groups()
    body.__name__ = fn.__name__
    return tp.e2e(body)


def pid_in(path, timeout=30.0, *procs):
    """The pid a step wrote to `path` (it is its group's id), once it is there."""
    tp.wait_for(f"{os.path.basename(path)}", lambda: tp.read(path).strip().isdigit(), *procs,
                timeout=timeout)
    pid = int(tp.read(path).strip())
    _GROUPS.append(pid)
    return pid


def long_step(pidf, token=SLEEP):
    """A step that records its group id, then runs a background AND a foreground sleep."""
    return f"echo $$ > '{pidf}'; {token} & {token}"


def steps(*runs, head=""):
    """A [prep_pr] Form B table; each run is (name, command)."""
    body = "".join(f"[[prep_pr.steps]]\nname = {json.dumps(n)}\nrun = {json.dumps(r)}\n"
                   for n, r in runs)
    return f"[prep_pr]\n{head}\n{body}"


def command(name, run, weight=2):
    return f"[[pool.command]]\nname = {json.dumps(name)}\nrun = {json.dumps(run)}\nweight = {weight}\n"


# The instrumented watchdog. TRACE_HOOK is the source's no-op, replaced in a COPY only.
TRACE_HOOK = "def trace(line):\n    pass\n"
TRACED = '''SKIP_UNTIL = 0.0
def trace(line):
    global SKIP_UNTIL
    if line == b"EOF":
        SKIP_UNTIL = time.monotonic() + float(os.environ.get("GATE_WD_IGNORE_KILL_S") or 0)
    path = os.environ.get("GATE_WD_TRACE")
    if path:
        with open(path, "ab") as f:
            f.write(line + b"\\n")
'''
KILL_HOOK = '            trace(b"K%d" % g)\n'
SKIPPED = '            if time.monotonic() < SKIP_UNTIL:\n                continue\n' + KILL_HOOK
_COPIES = ("gate_pool.py", "gate-runner.py", "orchestrate_schemas.py")


def traced(tmp, extra=()):
    """A COPY of the scripts under test with the watchdog traced -> the runner's path. `extra`
    is more (old, new) edits for the runner copy; each `old` must occur exactly once."""
    d = tempfile.mkdtemp(prefix="wd-scripts-", dir=tmp)
    for name in _COPIES:
        src = tp.read(os.path.join(tp.SCRIPTS, name))
        if name == "gate-runner.py":
            for old, new in ((TRACE_HOOK, TRACED), (KILL_HOOK, SKIPPED), *extra):
                if src.count(old) != 1:
                    raise AssertionError(f"traced copy: {old!r} occurs {src.count(old)} times")
                src = src.replace(old, new)
        tp.put(os.path.join(d, name), src)
    return os.path.join(d, "gate-runner.py")


def spawn(runner, root, home, *args, env=None):
    """A runner started in its own session (its pid is its pgid), output to files."""
    base = os.path.join(os.path.dirname(home), f"run{next(tp._SERIAL)}")
    with open(base + ".out", "wb") as out, open(base + ".err", "wb") as err:
        p = subprocess.Popen([sys.executable, "-B", runner, *args], cwd=root,
                             env=tp.runner_env(home, env), stdin=subprocess.DEVNULL,
                             stdout=out, stderr=err, start_new_session=True)
    p.out, p.err = base + ".out", base + ".err"
    tp._LIVE.append(p)
    return p


def trace_of(path):
    return tp.read(path).split()


def sigkill(p):
    """SIGKILL the runner itself (never its group: the case is about what is left behind)."""
    os.kill(p.pid, signal.SIGKILL)
    p.wait(timeout=30)


def started(path):
    return os.path.exists(path)


# --- cases --------------------------------------------------------------------------------------
def case_marker():
    src = tp.read(tp.RUNNER)
    mod = tp.load_runner()
    same("marker: the runner under test carries `POOL_WATCHDOG = 1` at column 0, once, and it "
         "is the module's value", (re.findall(r"^POOL_WATCHDOG = 1$", src, re.M),
                                   getattr(mod, "POOL_WATCHDOG", None)), (["POOL_WATCHDOG = 1"], 1))


@case_of
def case_protocol(tmp, home):
    mod = tp.load_runner()
    claim = tp.put(os.path.join(tmp, "x.sweep"), "{}\n")
    g1 = subprocess.Popen(SLEEP.split(), start_new_session=True)
    g2 = subprocess.Popen(SLEEP.split(), start_new_session=True)
    _GROUPS.extend([g1.pid, g2.pid])
    wd = subprocess.Popen([sys.executable, "-I", "-B", "-c", mod._WATCHDOG_SRC],
                          stdin=subprocess.PIPE, start_new_session=True, close_fds=True)
    tp._LIVE.append(wd)
    try:
        wd.stdin.write(b"+%d\n+%d\n-%d\nS%s\n" % (g1.pid, g2.pid, g2.pid, claim.encode()))
        wd.stdin.flush()
        tp.wait_for("the watchdog to lock the claim", lambda: not gp_free(claim), wd, timeout=10)
        same("protocol: the watchdog holds the `.sweep` file it was sent, and is still running",
             (gp_free(claim), wd.poll()), (False, None))
        wd.stdin.close()
        rc, g1_rc = tp.ended(wd, 10), tp.ended(g1, 10)   # a string when it did not end: a FAIL
        same("protocol: on EOF it kills every listed group and exits once each is gone, which "
             "frees the claim", (rc, g1_rc, gp_free(claim)), (0, -signal.SIGKILL, True))
        same("protocol: a pgid it was told is gone (`-<pgid>`) is never signalled",
             g2.poll(), None)
        empty = subprocess.Popen([sys.executable, "-I", "-B", "-c", mod._WATCHDOG_SRC],
                                 stdin=subprocess.PIPE, start_new_session=True)
        tp._LIVE.append(empty)
        t0 = time.monotonic()
        empty.stdin.close()
        same("protocol: with nothing listed it exits at once on EOF",
             (empty.wait(timeout=10), time.monotonic() - t0 < 2.0), (0, True))
    finally:
        for g in (g1, g2):
            with contextlib.suppress(OSError):
                g.kill()
            g.wait()


def gp_free(path):
    return tp.lock_free(path)


@case_of
def case_sigkill_parallel(tmp, home):
    runner, wt = traced(tmp), os.path.join(tmp, "par.trace")
    qpid, lpid = os.path.join(tmp, "q.pid"), os.path.join(tmp, "l.pid")
    root = tp.make_repo(tmp, "par", steps(("quick", f"echo $$ > '{qpid}'"),
                                          ("long", long_step(lpid)), head="jobs = 2"))
    p = spawn(runner, root, home, env={"GATE_WD_TRACE": wt})
    q, lg = pid_in(qpid, 30, p), pid_in(lpid, 30, p)
    with contextlib.suppress(AssertionError):     # a mutant that never prunes fails below, by name
        tp.wait_for("the finished step's prune", lambda: f"-{q}" in trace_of(wt), timeout=5)
    sigkill(p)
    same("sigkill parallel: SIGKILL of a pool-OFF parallel runner leaves no registered step "
         "group alive within a few seconds", group_gone(lg, 5.0), True)
    tp.wait_for("the watchdog to finish", lambda: "DONE" in trace_of(wt), timeout=10)
    t = trace_of(wt)
    same("sigkill parallel: each group was listed before it ran; the finished one was pruned",
         (f"+{q}" in t, f"+{lg}" in t, f"-{q}" in t, f"-{lg}" in t), (True, True, True, False))
    same("sigkill parallel: a pruned pgid is never signalled: the watchdog killed the listed "
         "group alone", sorted({x for x in t if x.startswith("K")}), [f"K{lg}"])


@case_of
def case_prune_serial(tmp, home):
    # A POOLED serial gate of two steps, its runner SIGKILLed during the second: the first
    # step's group was pruned by its own sweep, so the watchdog's kill set is the second's alone.
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)
    runner, wt = traced(tmp), os.path.join(tmp, "pser.trace")
    qpid, lpid = os.path.join(tmp, "pq.pid"), os.path.join(tmp, "pl.pid")
    root = tp.make_repo(tmp, "pser", steps(("quick", f"echo $$ > '{qpid}'"),
                                           ("long", long_step(lpid))))
    p = spawn(runner, root, home, env={"GATE_WD_TRACE": wt})
    q, lg = pid_in(qpid, 30, p), pid_in(lpid, 30, p)
    sigkill(p)
    same("prune serial: SIGKILL of a pooled serial runner during its second step leaves that "
         "step's group dead within a few seconds", group_gone(lg, 5.0), True)
    with contextlib.suppress(AssertionError):
        tp.wait_for("the watchdog to finish", lambda: "DONE" in trace_of(wt), timeout=10)
    t = trace_of(wt)
    same("prune serial: the first step's group was listed and then pruned by its own sweep, "
         "the second listed and never pruned", (f"+{q}" in t, f"-{q}" in t, f"+{lg}" in t,
                                                f"-{lg}" in t), (True, True, True, False))
    same("prune serial: a pruned pgid is never signalled: the watchdog killed the second step's "
         "group alone", sorted({x for x in t if x.startswith("K")}), [f"K{lg}"])


@case_of
def case_prune_parallel(tmp, home):
    # A parallel step whose shell exits at once, leaving a 1 s background job in its group (a
    # LINGERING group). Once that job is done the group is pruned; a runner SIGKILLed after
    # that has the watchdog kill the still-running step's group alone.
    runner, wt = traced(tmp), os.path.join(tmp, "ppar.trace")
    bpid, lpid = os.path.join(tmp, "pb.pid"), os.path.join(tmp, "pl.pid")
    root = tp.make_repo(tmp, "ppar", steps(("bg", f"echo $$ > '{bpid}'; sleep 1 &"),
                                           ("long", long_step(lpid)), head="jobs = 2"))
    p = spawn(runner, root, home, env={"GATE_WD_TRACE": wt})
    b, lg = pid_in(bpid, 30, p), pid_in(lpid, 30, p)
    with contextlib.suppress(AssertionError):     # a mutant that never prunes fails below, by name
        tp.wait_for("the lingering group's prune", lambda: f"-{b}" in trace_of(wt), timeout=6)
    sigkill(p)
    same("prune parallel: SIGKILL of the runner leaves the running step's group dead within a "
         "few seconds", group_gone(lg, 5.0), True)
    with contextlib.suppress(AssertionError):
        tp.wait_for("the watchdog to finish", lambda: "DONE" in trace_of(wt), timeout=10)
    t = trace_of(wt)
    same("prune parallel: a finished step's lingering group was pruned once its background job "
         "ended, and never signalled: the watchdog killed the running step's group alone",
         (f"+{b}" in t, f"-{b}" in t, sorted({x for x in t if x.startswith("K")})),
         (True, True, [f"K{lg}"]))


@case_of
def case_sigkill_serial(tmp, home):
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)
    runner, wt = traced(tmp), os.path.join(tmp, "ser.trace")
    pidf = os.path.join(tmp, "s.pid")
    root = tp.make_repo(tmp, "ser", steps(("s", long_step(pidf))))
    p = spawn(runner, root, home, env={"GATE_WD_TRACE": wt})
    g = pid_in(pidf, 30, p)
    sigkill(p)
    same("sigkill serial: SIGKILL of a POOLED serial runner leaves no registered step group alive "
         "within a few seconds", group_gone(g, 5.0), True)
    tp.wait_for("the watchdog to finish", lambda: "DONE" in trace_of(wt), timeout=10)
    t = trace_of(wt)
    claims = [x for x in t if x.startswith("S")]
    same("sigkill serial: the holder handed its watchdog ONE `.sweep` claim, `<ticket>.sweep`, "
         "before its step was listed", (len(claims), claims[0].endswith(".ticket.sweep") if claims
                                        else None, t.index(f"+{g}") > t.index(claims[0])
                                        if claims and f"+{g}" in t else None), (1, True, True))
    # The next gate in that worktree runs, and its pass clears the dead holder's records.
    tp.put(os.path.join(root, ".gates.toml"), steps(("s", "true")))
    rc, out, err = tp.run_runner(root, home, runner=runner)
    same("sigkill serial: once the sweep is done the next gate in that worktree runs, and the dead "
         "holder's ticket and free `.sweep` are cleared", (rc, tp.listing(home)), (0, []))


@case_of
def case_sigkill_pool_run(tmp, home):
    got = {}
    for label in ("off", "on"):
        h = os.path.join(tmp, f"gq-{label}"); os.mkdir(h, 0o700)
        if label == "on":
            tp.put(os.path.join(h, "config.toml"), tp.CONFIG)
        pidf = os.path.join(tmp, f"pr-{label}.pid")
        root = tp.make_repo(tmp, f"pr-{label}", command("long", long_step(pidf)))
        p = spawn(tp.RUNNER, root, h, "--pool-run", "long")
        g = pid_in(pidf, 30, p)
        sigkill(p)
        got[label] = group_gone(g, 5.0)
    same("sigkill pool-run: SIGKILL of a `--pool-run` runner, pool OFF and ON, leaves no "
         "registered command group alive within a few seconds", got, {"off": True, "on": True})


_BYTE = '            os.write(w, b"\\n")\n'
_KILL_ME = "            os.kill(os.getpid(), signal.SIGKILL)\n"
_RELEASE_DEF = "def _release(proc, w):\n"


@case_of
def case_held(tmp, home):
    mark = os.path.join(tmp, "ran")
    # (a) The runner dies between Popen and the start byte, before it even listed the group:
    # the step must never run, and nothing may be left (a step that ran unheld would be an
    # orphan no watchdog knows of).
    runner = traced(tmp, [(_RELEASE_DEF, _RELEASE_DEF + _KILL_ME[8:])])
    root = tp.make_repo(tmp, "held-a", steps(("s", f"echo x > '{mark}'; {SLEEP}"), head="jobs = 2"))
    p = spawn(runner, root, home)
    p.wait(timeout=30)
    end = time.monotonic() + 5
    while leftovers() and time.monotonic() < end:
        time.sleep(0.05)
    same("held: a runner killed between Popen and the start byte leaves no step running, and the "
         "step never ran anything", (p.returncode, started(mark), leftovers()),
         (-signal.SIGKILL, False, []))
    # (b) The runner dies the instant AFTER the start byte: the step was listed before it could
    # run, so the watchdog kills it. Pool off (parallel) and pool on (serial).
    seen = {}
    for label, head, cfg in (("parallel", "jobs = 2", None), ("serial", "", tp.CONFIG)):
        h = os.path.join(tmp, f"gq-{label}"); os.mkdir(h, 0o700)
        if cfg:
            tp.put(os.path.join(h, "config.toml"), cfg)
        runner = traced(tmp, [(_BYTE, _BYTE + _KILL_ME)])
        wt = os.path.join(tmp, f"b-{label}.trace")
        root = tp.make_repo(tmp, f"held-b-{label}", steps(("s", SLEEP), head=head))
        p = spawn(runner, root, h, env={"GATE_WD_TRACE": wt})
        p.wait(timeout=30)
        with contextlib.suppress(AssertionError):
            tp.wait_for("the watchdog to finish", lambda: "DONE" in trace_of(wt), timeout=10)
        end = time.monotonic() + 5
        while leftovers() and time.monotonic() < end:
            time.sleep(0.05)
        seen[label] = (p.returncode, leftovers())
        kill_groups()
    same("held: a runner killed right after the start byte leaves no step running: its group "
         "was listed with the watchdog BEFORE the byte let it run",
         seen, {"parallel": (-signal.SIGKILL, []), "serial": (-signal.SIGKILL, [])})


@case_of
def case_inherit(tmp, home):
    tp.put(os.path.join(home, "config.toml"), "[pool]\nprotocol = 1\nbudget = 4\n")
    runner, wt = traced(tmp), os.path.join(tmp, "inh.trace")
    pidf = os.path.join(tmp, "inh.pid")
    root = tp.make_repo(tmp, "inh", steps(("s", long_step(pidf))))
    p = spawn(runner, root, home, env={"GATE_WD_TRACE": wt, "GATE_WD_IGNORE_KILL_S": "4"})
    g = pid_in(pidf, 30, p)
    waiters = os.path.join(home, "waiters")
    ticket = next(os.path.join(waiters, n) for n in tp.listing(home, suffix=".ticket"))
    sweep = ticket + ".sweep"
    wdpid = json.loads(tp.read(sweep)).get("pid")
    sigkill(p)
    # The holder is gone and its watchdog is still sweeping (it ignores its first kills here):
    # whatever it holds now, it holds by itself.
    slots = [os.path.join(home, "slots", n) for n in tp.listing(home, "slots")]
    snap = {"watchdog alive": tp.alive(wdpid), "group alive": group_alive(g),
            "slots free": [gp_free(s) for s in slots], "ticket free": gp_free(ticket),
            "admit.lock free": gp_free(os.path.join(home, "admit.lock")),
            "sweep held": not gp_free(sweep)}
    same("inherit: with its holder SIGKILLed and its sweep still running, the watchdog holds NO "
         "slot, ticket or admit.lock descriptor, and exactly the `.sweep` lock it opened itself",
         snap, {"watchdog alive": True, "group alive": True, "slots free": [True] * 4,
                "ticket free": True, "admit.lock free": True, "sweep held": True})
    time.sleep(1.5)                        # pacing only: the facts are read below, both ways
    same("inherit: 1.5 s on, the group is still alive and the watchdog still holds the claim: "
         "it does not exit before its groups are gone",
         (group_alive(g), tp.alive(wdpid), not gp_free(sweep)), (True, True, True))
    same("inherit: and once the group is gone the watchdog exits and the claim is free",
         (group_gone(g, 15.0), tp.gone(wdpid, 10.0), gp_free(sweep)), (True, True, True))


@case_of
def case_sweep_claim(tmp, home):
    # The holder H takes the whole budget in worktree A, and its watchdog does not get its group
    # killed for 11 s after H dies (a group the first kill does not end). W1 waits in the SAME
    # worktree, W2 in another; both are blocked while H lives.
    tp.put(os.path.join(home, "config.toml"), "[pool]\nprotocol = 1\nbudget = 4\n")
    runner = traced(tmp)
    hpid, w1, w2 = (os.path.join(tmp, n) for n in ("h.pid", "w1.started", "w2.started"))
    # W1 runs in worktree A too (the same directory), so the step it runs is chosen by its
    # environment.
    a = tp.make_repo(tmp, "A", steps(("s", f'if test -n "$W1"; then echo x > "$W1"; else '
                                           f"{long_step(hpid)}; fi"), head="weight = 4"))
    b = tp.make_repo(tmp, "B", steps(("s", f"echo x > '{w2}'"), head="weight = 1"))
    h = spawn(runner, a, home, env={"GATE_WD_IGNORE_KILL_S": "11"})
    g = pid_in(hpid, 30, h)
    p1 = spawn(runner, a, home, env={"W1": w1})
    p2 = spawn(runner, b, home)
    tp.wait_for("both waiters to wait", lambda: all("waiting" in tp.read(x.err) for x in (p1, p2)),
                p1, p2, timeout=30)
    sigkill(h)
    t0 = time.monotonic()
    with contextlib.suppress(AssertionError):
        tp.wait_for("W2 to start", lambda: started(w2), timeout=6)
    same("sweep claim: after the holder is SIGKILLed a waiter in ANOTHER worktree starts at once "
         "(its slots are free), while the dead holder's group is still alive",
         (started(w2), time.monotonic() - t0 < 6, group_alive(g)), (True, True, True))
    while time.monotonic() - t0 < 10.5 and not started(w1):
        time.sleep(0.05)
    same("sweep claim: a waiter in the SAME worktree does not start while the watchdog's sweep "
         "runs: the claim is held past 10 s for as long as the registered group is alive",
         (started(w1), group_alive(g), time.monotonic() - t0 >= 10.5), (False, True, True))
    with contextlib.suppress(AssertionError):
        tp.wait_for("W1 to start", lambda: started(w1), timeout=20)
    same("sweep claim: it starts once the sweep is done, and by then the group is gone",
         (started(w1), group_alive(g)), (True, False))
    same("sweep claim: both waiters pass", (p1.wait(timeout=30), p2.wait(timeout=30)), (0, 0))


NESTED = """[prep_pr]
weight = 4
[[prep_pr.steps]]
name = "outer"
run = {run}
"""


@case_of
def case_nested(tmp, home):
    # The holder's step runs a nested `--pool-run` whose OWN watchdog does not get its command
    # group killed for 5 s. The holder's watchdog is plain: it is done as soon as the nested
    # RUNNER is dead.
    tp.put(os.path.join(home, "config.toml"), "[pool]\nprotocol = 1\nbudget = 4\n")
    runner = traced(tmp)
    ipid, w1 = os.path.join(tmp, "inner.pid"), os.path.join(tmp, "w1.started")
    inner = (f"GATE_WD_IGNORE_KILL_S=5 '{sys.executable}' -B '{runner}' --pool-run inner")
    root = tp.make_repo(tmp, "N", NESTED.format(run=json.dumps(
        f'if test -n "$W1"; then echo x > "$W1"; else {inner}; fi'))
        + command("inner", long_step(ipid), weight=2))
    h = spawn(runner, root, home)
    g = pid_in(ipid, 30, h)
    p1 = spawn(runner, root, home, env={"W1": w1})
    tp.wait_for("the waiter to wait", lambda: "waiting" in tp.read(p1.err), p1, timeout=30)
    sweeps = sorted(n for n in os.listdir(os.path.join(home, "waiters")) if n.endswith(".sweep"))
    same("nested: the holder and its nested run each hold a `.sweep` claim of their own",
         [re.sub(r"\.[0-9]+\.[0-9a-f]{8}\.sweep$", ".<pid>.<hex>.sweep",
                 re.sub(r"^.*?\.ticket", "T", n))
          for n in sweeps], ["T.<pid>.<hex>.sweep", "T.sweep"])
    sigkill(h)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 3.0 and not started(w1):
        time.sleep(0.05)
    same("nested: after the holder is SIGKILLed, a waiter in the same worktree does not start "
         "while the NESTED watchdog is still killing the nested command's group",
         (started(w1), group_alive(g)), (False, True))
    with contextlib.suppress(AssertionError):
        tp.wait_for("W1 to start", lambda: started(w1), timeout=20)
    same("nested: it starts once the nested watchdog is done, and by then that group is gone",
         (started(w1), group_alive(g), p1.wait(timeout=30)), (True, False, 0))


def case_busy():
    with tempfile.TemporaryDirectory(prefix="wd-busy-") as tmp:
        home = os.path.join(tmp, "gq")
        pool = gp.Pool(home, tp.cfg())
        waiters = os.path.join(home, "waiters")
        rec = {"pool_protocol": gp.POOL_PROTOCOL, "worktree": "/wt/a", "pid": 4242}
        held = {}
        for name in ("0000000009-x-aa.ticket.sweep", "0000000009-x-aa.ticket.777.0a1b2c3d.sweep"):
            path = tp.put(os.path.join(waiters, name), json.dumps(rec) + "\n")
            fd = os.open(path, os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
            held[name] = fd
        foreign = tp.put(os.path.join(waiters, "0000000008-x-bb.ticket.sweep"),
                         json.dumps({**rec, "pool_protocol": 999}) + "\n")
        w = pool.enter("gate", "gate", 1, "/wt/a")
        other = pool.enter("gate", "gate", 1, "/wt/b")
        same("busy: a HELD `.sweep` claims its worktree: a waiter there does not start, and says "
             "who holds it", (w.poll(), w.status()),
             (False, "gate-runner: waiting for this worktree (held by sweep pid 4242)"))
        same("busy: a waiter in another worktree starts", other.poll(), True)
        os.close(held.pop("0000000009-x-aa.ticket.sweep"))
        same("busy: a nested run's held `<ticket>.<pid>.<hex>.sweep` claims it just the same",
             w.poll(), False)
        os.close(held.pop("0000000009-x-aa.ticket.777.0a1b2c3d.sweep"))
        same("busy: once every claim is free the waiter starts, the free claims of this protocol "
             "are unlinked, and one of another protocol is left alone",
             (w.poll(), sorted(n for n in tp.listing(home) if n.endswith(".sweep"))),
             (True, [os.path.basename(foreign)]))
        w.leave(); other.leave()


def case_nest_file():
    with tempfile.TemporaryDirectory(prefix="wd-nest-") as tmp:
        home = os.path.join(tmp, "gq")
        pool = gp.Pool(home, tp.cfg())
        nest = tp.put(os.path.join(home, "waiters", "0000000003-x-cc.ticket.nest"), "")
        fd = os.open(nest, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        w = pool.enter("gate", "gate", 1, "/wt/n")
        w.poll()
        same("nest file: a dead ticket's HELD `.nest` file is not unlinked by a pass",
             os.path.exists(nest), True)
        os.close(fd)
        w.leave()
        w = pool.enter("gate", "gate", 1, "/wt/n"); w.poll(); w.leave()
        same("nest file: and once it is free the next pass unlinks it", os.path.exists(nest), False)


class _Sweeper:
    """A stand-in watchdog for the gate_pool side: records what it was asked, locks the claim
    on a descriptor of its own (as the real one does in its own process)."""

    def __init__(self, home, fail=False, pid=4343):
        self.pid, self.home, self.fail, self.fds, self.seen = pid, home, fail, [], []

    def claim(self, path):
        admit_free = gp_free(os.path.join(self.home, "admit.lock"))
        self.seen.append((os.path.basename(path), admit_free, json.loads(tp.read(path))))
        if self.fail:
            raise OSError("the orphan watchdog did not take the sweep claim")
        fd = os.open(path, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.fds.append(fd)

    def let_go(self):
        for fd in self.fds:
            os.close(fd)
        self.fds = []


def case_claim():
    with tempfile.TemporaryDirectory(prefix="wd-claim-") as tmp:
        home = os.path.join(tmp, "gq")
        pool = gp.Pool(home, tp.cfg())
        s = _Sweeper(home)
        w = pool.enter("gate", "gate", 2, "/wt/c", sweeper=s)
        name = os.path.basename(w.path) + ".sweep"
        same("claim: the grant takes the worktree's `.sweep` claim under admit.lock, recording "
             "the protocol, the worktree and the watchdog's pid",
             (w.poll(), s.seen), (True, [(name, False, {"pool_protocol": gp.POOL_PROTOCOL,
                                                       "worktree": "/wt/c", "pid": 4343})]))
        w.holder.release()
        same("claim: a release while the watchdog still holds the claim leaves it in place",
             name in tp.listing(home), True)
        s.let_go()
        x = pool.enter("gate", "gate", 1, "/wt/z"); x.poll(); x.leave()
        same("claim: a free claim is unlinked by the next pass", name in tp.listing(home), False)
        bad = _Sweeper(home, fail=True)
        w = pool.enter("gate", "gate", 2, "/wt/c", sweeper=bad)
        try:
            w.poll()
            raised = False
        except OSError:
            raised = True
        same("claim: a claim that fails fails the grant: no `.sweep` left, the ticket still "
             "waiting, every slot free",
             (raised, [n for n in tp.listing(home) if n.endswith(".sweep")],
              tp._load_json(w.path).get("state"),
              [gp_free(os.path.join(home, "slots", n)) for n in tp.listing(home, "slots")]),
             (True, [], "waiting", [True] * 10))
        w.leave()


def _locked(path, text):
    """Write `path` and hold an exclusive flock on it (an orphan watchdog's claim) -> fd."""
    tp.put(path, text)
    fd = os.open(path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def case_claim_exclusive():
    # PR #581 review: a claim is never written over an existing file. A reused pid under the
    # same holder must not replace a claim whose OLD inode an orphan watchdog still flocks.
    with tempfile.TemporaryDirectory(prefix="wd-excl-") as tmp:
        home = os.path.join(tmp, "gq")
        pool = gp.Pool(home, tp.cfg())
        h = pool.enter("gate", "gate", 4, "/wt/e", sweeper=_Sweeper(home))
        h.poll()
        hname = os.path.basename(h.path)
        waiters = os.path.join(home, "waiters")
        rec = {"pool_protocol": gp.POOL_PROTOCOL, "worktree": "/wt/e", "pid": 5151,
               "nest": hname + ".nest"}
        # A direct claim at a path that exists is REFUSED and touches nothing.
        taken = os.path.join(waiters, hname + ".1.0a0a0a0a.sweep")
        fd0 = _locked(taken, json.dumps(rec) + "\n")
        ino = os.stat(taken).st_ino
        s0 = _Sweeper(home, pid=6161)
        with pool._admit():
            try:
                pool._claim(taken, "/wt/e", s0)
                refused = False
            except FileExistsError:
                refused = True
        same("claim exclusive: a claim at a path that already exists is refused: the existing "
             "file keeps its inode, its lock and its record, and no watchdog was handed it",
             (refused, os.stat(taken).st_ino == ino, not gp_free(taken),
              tp._load_json(taken), s0.seen), (True, True, True, rec, []))
        os.close(fd0)
        # A nested run (one level down a live chain) whose OWN pid names a held claim of an
        # ancestor-depth run (a SIGKILLed ancestor's, still sweeping): it starts, and its claim
        # is a separate file beside the held one.
        parent = _locked(os.path.join(waiters, hname + ".nest"), "")
        old = os.path.join(waiters, "%s.%d.sweep" % (hname, os.getpid()))
        fd1 = _locked(old, json.dumps(rec) + "\n")
        ino = os.stat(old).st_ino
        s1 = _Sweeper(home, pid=7171)
        n = pool.nested(2, "/wt/e", {"GATEQ_HOLDER": h.path,
                                    "GATEQ_NEST": os.path.join(waiters, hname + ".nest")},
                        print, s1)
        started_ = n is not None and n.poll()
        mine = [x[0] for x in s1.seen]
        same("claim exclusive: a nested run whose pid names a HELD claim never replaces it: that "
             "claim keeps its inode, lock and record, and the run's own claim is another file",
             (started_, os.stat(old).st_ino == ino, not gp_free(old), tp._load_json(old),
              len(mine) == 1 and mine[0] != os.path.basename(old)),
             (True, True, True, rec, True))
        n.leave(); s1.let_go(); os.close(fd1); os.close(parent)
        h.holder.release()


def case_nested_sweep():
    # PR #581 review: a nested run that is done (its child lock free) while its watchdog still
    # kills its groups keeps the NEXT nested run under that holder from starting beside them.
    with tempfile.TemporaryDirectory(prefix="wd-nsweep-") as tmp:
        home = os.path.join(tmp, "gq")
        pool = gp.Pool(home, tp.cfg())
        hs = _Sweeper(home)
        h = pool.enter("gate", "gate", 4, "/wt/s", sweeper=hs)
        h.poll()
        env = {"GATEQ_HOLDER": h.path}
        s1, s2, s3 = (_Sweeper(home, pid=p) for p in (8101, 8102, 8103))
        n1 = pool.nested(2, "/wt/s", env, print, s1)
        same("nested sweep: the first nested run starts and takes its claim",
             (n1.poll(), len(s1.seen)), (True, 1))
        n1.leave()                     # done; its watchdog (s1) is still sweeping
        n2 = pool.nested(2, "/wt/s", env, print, s2)
        same("nested sweep: a second nested run under the same holder does not start while the "
             "first one's `.sweep` is still held, and says so",
             (n2.poll(), n2.status(), s2.seen),
             (False, "gate-runner: waiting for a nested sweep under this holder to finish "
                     "(sweep pid 8101)", []))
        s1.let_go()
        same("nested sweep: it starts once that sweep is done", n2.poll(), True)
        # A nested run's OWN child (one level down) is not held up by its live parent's claim.
        n3 = pool.nested(1, "/wt/s", {**env, **n2.holder.child_env()}, print, s3)
        same("nested sweep: a nested run's own child starts beside its live parent's claim "
             "(an ancestor never blocks)", n3 is not None and n3.poll(), True)
        n3.leave(); s3.let_go(); n2.leave(); s2.let_go(); h.holder.release(); hs.let_go()


@case_of
def case_claim_fail(tmp, home):
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)
    mark = os.path.join(tmp, "ran")
    # A watchdog that dies at once can take no claim: the gate is NOT RUN, never unpooled.
    runner = traced(tmp, [("_WATCHDOG_SRC = r\"\"\"\n", "_WATCHDOG_SRC = r\"\"\"\nraise SystemExit(0)\n")])
    root = tp.make_repo(tmp, "cf", steps(("s", f"echo x > '{mark}'")))
    rc, out, err = tp.run_runner(root, home, runner=runner)
    same("claim fail: with the pool on, a watchdog that cannot take the claim makes the gate NOT "
         "RUN (exit 2): the step never ran and the ticket was given back",
         (rc, started(mark), tp.listing(home), "NOT RUN" in err), (2, False, [], True))
    root = tp.make_repo(tmp, "cf-run", command("c", f"echo x > '{mark}'"))
    rc, out, err = tp.run_runner(root, home, "--pool-run", "c", runner=runner)
    same("claim fail: a `--pool-run` whose watchdog cannot take the claim is NOT RUN with "
         "reason=pool-watchdog (the watchdog's claim), not pool-error",
         (rc, started(mark), "POOL-RUN: c NOT RUN reason=pool-watchdog" in err), (2, False, True))


def case_claim_fast():
    # A `.sweep` path the WATCHDOG cannot open (relative: it runs with cwd "/", the runner does
    # not) ends the watchdog at once, after it killed what it had listed; claim() then fails
    # FAST with the claim error, instead of spinning CLAIM_WAIT_S under admit.lock.
    mod = tp.load_runner()
    with tempfile.TemporaryDirectory(prefix="wd-fast-") as tmp:
        tp.put(os.path.join(tmp, "rel.sweep"), "{}\n")
        g = subprocess.Popen(SLEEP.split(), start_new_session=True)
        _GROUPS.append(g.pid)
        here = os.getcwd()
        wd = None
        try:
            os.chdir(tmp)
            wd = mod._Watchdog(True)
            tp._LIVE.append(wd.proc)
            wd.send(b"+%d" % g.pid)
            t0 = time.monotonic()
            try:
                wd.claim("rel.sweep")
                err = None
            except OSError as e:
                err = e
            took = time.monotonic() - t0
            same("claim fast: a claim the watchdog cannot open fails within 3 s (not CLAIM_WAIT_S) "
                 "with the claim error, and the watchdog killed its listed group and exited 3",
                 (isinstance(err, mod._ClaimError), took < 3.0, tp.ended(g, 10),
                  tp.ended(wd.proc, 10)), (True, True, -signal.SIGKILL, 3))
        finally:
            os.chdir(here)
            if wd is not None:
                wd.close()
            with contextlib.suppress(OSError):
                g.kill()
            g.wait()


@case_of
def case_relative_home(tmp, home):
    # GATEQ_HOME given RELATIVE (to the runner's cwd): the pool root is made absolute where it
    # is resolved, so the watchdog (cwd "/") is sent a path it can open, and the run runs.
    tp.put(os.path.join(home, "config.toml"), tp.CONFIG)
    root = tp.make_repo(tmp, "rel", command("c", "true"))
    rel = os.path.relpath(home, root)
    t0 = time.monotonic()
    rc, out, err = tp.run_runner(root, home, "--pool-run", "c", env={"GATEQ_HOME": rel})
    same("relative home: with a RELATIVE GATEQ_HOME a pooled `--pool-run` runs at once (exit 0, "
         "its POOL-RUN line, within 5 s)",
         (rel.startswith(".."), rc, "POOL-RUN: c ran exit=0" in err, time.monotonic() - t0 < 5.0),
         (True, 0, True, True))


@case_of
def case_pool_off_serial(tmp, home):
    seen = {}
    for form, gates in (("form-a", '[prep_pr]\ngate = "true"\n'),
                        ("serial", steps(("s", "true"))), ("parallel", steps(("s", "true"),
                                                                              head="jobs = 2"))):
        wt = os.path.join(tmp, f"{form}.trace")
        root = tp.make_repo(tmp, form, gates)
        rc, out, err = tp.run_runner(root, home, runner=traced(tmp), env={"GATE_WD_TRACE": wt})
        seen[form] = (rc, os.path.exists(wt) and "DONE" in trace_of(wt))
    same("pool off: the serial paths (Form A, serial Form B) start NO watchdog; the parallel "
         "path starts one, and it is done when the run is", seen,
         {"form-a": (0, False), "serial": (0, False), "parallel": (0, True)})


CASES = [
    ("marker", case_marker), ("protocol", case_protocol), ("sigkill-parallel", case_sigkill_parallel),
    ("prune-serial", case_prune_serial), ("prune-parallel", case_prune_parallel),
    ("sigkill-serial", case_sigkill_serial), ("sigkill-pool-run", case_sigkill_pool_run),
    ("held", case_held), ("inherit", case_inherit), ("sweep-claim", case_sweep_claim),
    ("nested", case_nested), ("busy", case_busy), ("nest-file", case_nest_file),
    ("claim", case_claim), ("claim-fail", case_claim_fail), ("claim-fast", case_claim_fast),
    ("claim-exclusive", case_claim_exclusive), ("nested-sweep", case_nested_sweep),
    ("relative-home", case_relative_home), ("pool-off-serial", case_pool_off_serial),
]

# --- mutation self-test: each assertion must be able to FAIL ------------------------------------
# (label, file, [(old, new)], case, the check whose FAIL line the run must print). Every `old`
# must occur EXACTLY ONCE in its file, or the row is stale. The first seven are the mutations
# the doc's A4 test plan names.
R, M = "gate-runner.py", "gate_pool.py"
_SWEEP_LOOP = 'trace(b"EOF")\nlast = None\nwhile groups:\n'
_RELEASE = ('        _watch(proc.pid)\n        try:\n            os.write(w, b"\\n")\n'
            "        except BrokenPipeError:            # the held shell is already gone: its exit says so\n"
            "            pass\n")
_SAME_WT = ("sweep claim: a waiter in the SAME worktree does not start while the watchdog's sweep "
            "runs: the claim is held past 10 s for as long as the registered group is alive")
MUTATIONS = [
    ("the claim released on a timer", R,
     [(_SWEEP_LOOP, 'trace(b"EOF")\nlast = None\n_t0 = time.monotonic()\n'
                    "while groups and time.monotonic() - _t0 < 5.0:\n")], "sweep-claim", _SAME_WT),
    ("a nested run given no `.sweep` of its own", M,
     [("            if self.sweeper is not None:\n                sweep = os.path.join(",
       "            if False:\n                sweep = os.path.join(")], "nested",
     "nested: after the holder is SIGKILLed, a waiter in the same worktree does not start while "
     "the NESTED watchdog is still killing the nested command's group"),
    ("the `.sweep` claim left out of busy", M,
     [("                    busy.setdefault(rec[\"worktree\"], f\"sweep pid {rec.get('pid')}\")",
       "                    pass")], "busy",
     "busy: a HELD `.sweep` claims its worktree: a waiter there does not start, and says who holds "
     "it"),
    ("a held `.nest` file unlinked", M,
     [("                    and _probe(path)[0] is False:\n                _rm(path)",
       ":\n                _rm(path)")], "nest-file",
     "nest file: a dead ticket's HELD `.nest` file is not unlinked by a pass"),
    ("`-<pgid>` never sent", R,
     [('        wd.send(b"-%d" % pgid)', "        pass")], "sigkill-parallel",
     "sigkill parallel: a pruned pgid is never signalled: the watchdog killed the listed group alone"),
    ("the EOF sweep skipped", R,
     [(_SWEEP_LOOP, 'trace(b"EOF")\nlast = None\nwhile False:\n')], "sigkill-serial",
     "sigkill serial: SIGKILL of a POOLED serial runner leaves no registered step group alive "
     "within a few seconds"),
    ("the start byte sent before `+<pgid>`", R,
     [(_RELEASE, '        try:\n            os.write(w, b"\\n")\n'
                 "        except BrokenPipeError:            # the held shell is already gone: its exit says so\n"
                 "            pass\n        _watch(proc.pid)\n")], "held",
     "held: a runner killed right after the start byte leaves no step running: its group was "
     "listed with the watchdog BEFORE the byte let it run"),
    # --- beyond the doc's list: each new assertion above has teeth ---
    ("the marker missing", R, [("\nPOOL_WATCHDOG = 1\n", "\nPOOL_WATCHDOG = 0\n")], "marker",
     "marker: the runner under test carries `POOL_WATCHDOG = 1` at column 0, once, and it is the "
     "module's value"),
    ("the watchdog exits before its groups are gone", R,
     [("    groups = {g for g in groups if alive(g)}\n", "    groups = set()\n")], "inherit",
     "inherit: 1.5 s on, the group is still alive and the watchdog still holds the claim: it does "
     "not exit before its groups are gone"),
    ("the watchdog's SIGKILL never sent", R,
     [("                os.killpg(g, signal.SIGKILL)\n", "                pass\n")], "protocol",
     "protocol: on EOF it kills every listed group and exits once each is gone, which frees the "
     "claim"),
    ("the runner, not the watchdog, holds the `.sweep` lock", R,
     [('        self.send(b"S" + raw)\n',
       '        self.mine = os.open(path, os.O_RDONLY); fcntl.flock(self.mine, fcntl.LOCK_EX); return\n')],
     "inherit",
     "inherit: with its holder SIGKILLed and its sweep still running, the watchdog holds NO slot, "
     "ticket or admit.lock descriptor, and exactly the `.sweep` lock it opened itself"),
    ("a step started unheld (no start pipe read)", R,
     [("_HOLD = 'IFS= read -r __gate_runner_hold || exit 1; ", "_HOLD = '")], "held",
     "held: a runner killed between Popen and the start byte leaves no step running, and the step "
     "never ran anything"),
    ("a grant that takes no claim", M,
     [("            sweeper.claim(path)\n", "            pass\n")], "claim-fail",
     "claim fail: with the pool on, a watchdog that cannot take the claim makes the gate NOT RUN "
     "(exit 2): the step never ran and the ticket was given back"),
    ("a failed claim left on disk", M,
     [("        except BaseException:\n            _rm(path)\n            raise",
       "        except BaseException:\n            raise")], "claim",
     "claim: a claim that fails fails the grant: no `.sweep` left, the ticket still waiting, every "
     "slot free"),
    ("a release that unlinks a claim its watchdog still holds", M,
     [("        if self.sweep and _probe(self.sweep)[0] is False:", "        if self.sweep:")], "claim",
     "claim: a release while the watchdog still holds the claim leaves it in place"),
    ("the pool-OFF serial path starts a watchdog", R,
     [("    if _POOL_RUN is not None:              # pool ON: its own group, interrupts forwarded",
       "    _watchdog()\n    if _POOL_RUN is not None:              # pool ON: its own group, interrupts forwarded")],
     "pool-off-serial",
     "pool off: the serial paths (Form A, serial Form B) start NO watchdog; the parallel path "
     "starts one, and it is done when the run is"),
    # --- the hostile review's round: the prune on the pooled serial and lingering paths ---
    ("the pooled serial sweep never prunes its group", R,
     [("    _prune_watched(KILL_GRACE_S if _group_alive(proc.pid) else 0.0)\n", "    pass\n")],
     "prune-serial",
     "prune serial: a pruned pgid is never signalled: the watchdog killed the second step's "
     "group alone"),
    ("a lingering group dropped without `-<pgid>`", R,
     [("        lingering.discard(pgid)\n        _unwatch(pgid)\n",
       "        lingering.discard(pgid)\n")], "prune-parallel",
     "prune parallel: a finished step's lingering group was pruned once its background job "
     "ended, and never signalled: the watchdog killed the running step's group alone"),
    ("a claim the watchdog cannot open left to time out", R,
     [("                except OSError:\n                    failed = True\n                    break\n",
       "                except OSError:\n                    continue\n")], "claim-fast",
     "claim fast: a claim the watchdog cannot open fails within 3 s (not CLAIM_WAIT_S) with the "
     "claim error, and the watchdog killed its listed group and exited 3"),
    ("a relative GATEQ_HOME used as given", R,
     [('    return os.path.abspath(os.environ.get("GATEQ_HOME") or os.path.join(',
       '    return (os.environ.get("GATEQ_HOME") or os.path.join(')], "relative-home",
     "relative home: with a RELATIVE GATEQ_HOME a pooled `--pool-run` runs at once (exit 0, its "
     "POOL-RUN line, within 5 s)"),
    ("a claim failure reported as pool-error", R,
     [('            _pool_say(f"gate-runner: NOT RUN - {e}")\n            return None, 2, "pool-watchdog"',
       '            _pool_say(f"gate-runner: NOT RUN - {e}")\n            return None, 2, "pool-error"')],
     "claim-fail",
     "claim fail: a `--pool-run` whose watchdog cannot take the claim is NOT RUN with "
     "reason=pool-watchdog (the watchdog's claim), not pool-error"),
    # --- the bot review round (PR #581): exclusive nested claims, nested runs wait on a sweep ---
    ("a nested claim named by pid alone and written over an existing file", M,
     [('                sweep = os.path.join(self.pool.waiters, "%s.%d.%s.sweep" % (\n'
       '                    hname, os.getpid(), os.urandom(4).hex()))\n',
       '                sweep = os.path.join(self.pool.waiters, "%s.%d.sweep" % (\n'
       '                    hname, os.getpid()))\n'),
      ("            os.link(tmp, path)             # exclusive: EEXIST leaves the existing claim alone\n",
       "            os.replace(tmp, path)\n")],
     "claim-exclusive",
     "claim exclusive: a nested run whose pid names a HELD claim never replaces it: that claim "
     "keeps its inode, lock and record, and the run's own claim is another file"),
    ("a claim written over an existing file", M,
     [("            os.link(tmp, path)             # exclusive: EEXIST leaves the existing claim alone\n",
       "            os.replace(tmp, path)\n")],
     "claim-exclusive",
     "claim exclusive: a claim at a path that already exists is refused: the existing file keeps "
     "its inode, its lock and its record, and no watchdog was handed it"),
    ("a nested run that ignores a held nested `.sweep`", M,
     [("            if orphan is not None:\n", "            if False:\n")], "nested-sweep",
     "nested sweep: a second nested run under the same holder does not start while the first "
     "one's `.sweep` is still held, and says so"),
    ("a nested run blocked by its own ancestors' claims", M,
     [("            ancestors.add(cur)\n", "            pass\n")], "nested-sweep",
     "nested sweep: a nested run's own child starts beside its live parent's claim (an ancestor "
     "never blocks)"),
]


RERUN_JOBS = 3


def _rerun(d, *args):
    env = {**os.environ, "GATE_POOL_SCRIPTS": d}
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args],
                           capture_output=True, text=True, env=env, timeout=600)
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT: the re-run was killed after 600 s"
    return r.returncode, r.stdout + r.stderr


def mutation_selftest():
    known = {name for name, _ in CASES}
    srcs = {n: tp.read(os.path.join(tp.SCRIPTS, n)) for n in _COPIES}
    ids = [(m[1], m[0]) for m in MUTATIONS]
    check("mutation self-test: every row has a unique (file, label)", len(set(ids)) == len(ids))

    def copy_to(d, name=None, mutated=None):
        os.mkdir(d)
        for n, text in srcs.items():
            tp.put(os.path.join(d, n), mutated if n == name else text)
        return d
    with tempfile.TemporaryDirectory(prefix="gate-wd-mut-") as tmp:
        rc, out = _rerun(copy_to(os.path.join(tmp, "clean")))
        check("mutation self-test: the UNMUTATED copy passes", rc == 0)
        if rc != 0:
            print(out)
            return
        runs = {}
        with concurrent.futures.ThreadPoolExecutor(RERUN_JOBS) as ex:
            for i, (label, name, edits, case, want) in enumerate(MUTATIONS):
                src = srcs[name]
                counts = [src.count(old) for old, _ in edits]
                if counts != [1] * len(edits) or case not in known:
                    runs[i] = (f"{counts} occurrences of its texts, case '{case}'", None)
                    continue
                for old, new in edits:
                    src = src.replace(old, new)
                d = copy_to(os.path.join(tmp, f"m{i:02d}"), name, src)
                runs[i] = (None, ex.submit(_rerun, d, "--only", case))
        for i, (label, name, edits, case, want) in enumerate(MUTATIONS):
            stale, fut = runs[i]
            if stale:
                check(f"mutation self-test: '{label}' is stale ({stale})", False)
                continue
            rc, out = fut.result()
            killed = rc != 0 and f"[FAIL] {want}\n" in out
            check(f"mutation: {label} -> killed by '{case}'", killed)
            if not killed:
                print(f"         exit {rc}; wanted the FAIL line of: {want}")


def main():
    args, only = sys.argv[1:], []
    while args:
        if args[0] == "--only" and len(args) >= 2:
            only.append(args[1])
            args = args[2:]
        else:
            print("usage: test-gate-watchdog.py [--only <case>]...", file=sys.stderr)
            return 2
    selected = [(n, fn) for n, fn in CASES if not only or n in only]
    unknown = [o for o in only if o not in {n for n, _ in CASES}]
    if unknown or not selected:
        print(f"test-gate-watchdog.py: no case named {unknown or only!r}", file=sys.stderr)
        return 2
    for name, fn in selected:
        print(f"{name}:")
        try:
            fn()
        except Exception as e:             # a crash is a FAIL line, never a lost verdict
            check(f"{name}: the case ran to its end (raised {type(e).__name__}: {e})", False)
        finally:
            kill_groups()
    if not only and not os.environ.get("GATE_POOL_SCRIPTS"):
        print("mutation self-test:")
        mutation_selftest()
    elif not only:
        print("mutation self-test: skipped (GATE_POOL_SCRIPTS is set: this is a re-run "
              "against a copy)")

    print("isolation:")
    left = leftovers()
    same(f"isolation: no `{SLEEP}` this harness started is left running", left, [])
    kill_groups()
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
    print("All gate-watchdog harness checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
