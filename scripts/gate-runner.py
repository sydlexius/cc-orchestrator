#!/usr/bin/env python3
"""gate-runner: run a repo's pre-push / pre-PR gates from a declarative
`.gates.toml`, with a fail-open fallback chain for repos that have no config.

ONE runner, ONE source of truth. The PR-lifecycle commands (/prep-pr,
/handle-review, /review-stack) and the optional pre-push-hook.sh all delegate
here instead of each re-implementing gate detection in prose.

Standalone: ZERO dependency on an orchestrate session, marker files, or
ORCHESTRATE_FLOOR_DIR. Needs only a normal PATH. The pre-push hook path
exercises exactly this standalone mode.

Config (`.gates.toml` at the repo root), `[prep_pr]` table, two mutually
exclusive forms:
  - Form A: `gate = "<umbrella command>"`  -> run as one command.
  - Form B: `steps = [ { name, run, required?, skip_if_absent?, skip_if?,
            exclusive? } ]` -> run each in order with per-step skip predicates.
            Opt-in parallel: `[prep_pr] jobs = N` or `--jobs N` (CLI wins;
            1 = the serial path, byte-identical to before #501).
            Opt-in `--skip <name>` (repeatable) skips named Form B steps;
            CI uses it to leave lint to its dedicated pinned steps.
            Opt-in `--shard K/N` runs only the steps whose name hashes to
            shard K of N (CI splits the macOS leg across runners, #544).
See skills/orchestrate/templates/gates.toml.md for the full schema.

Fail-open fallback when `.gates.toml` is absent, in order:
  1. Known umbrella: `make gate` target, then `scripts/pre-push-gate.sh`.
  2. The `## Gates` block in CLAUDE.md (run its command lines in order).
  3. Language-agnostic basics inferred from a repo manifest (WARN first).
  4. Nothing detectable: WARN and exit 0 (PROCEED; never hard-block).

TRUST BOUNDARY: `.gates.toml` is trusted repo config (like a Makefile / CI yaml)
-- the commands are run by someone who can already run shell in this repo. No
`eval` of dynamic strings, no privilege escalation, no weakening of the
deterministic floor or the advisory `# prep-pr-ok` gate. A `run`/`gate` string
is handed to the shell (shell=True) ONLY as the documented trusted-config path,
exactly like a Makefile recipe; nothing else is dynamically constructed.

MACHINE-WIDE GATE POOL (#539, skills/orchestrate/design/DESIGN-gate-pool.md).
OFF unless the USER wrote a `budget` into `~/.claude/gate-queue/config.toml`
(root override: GATEQ_HOME). Off, nothing below changes and the sibling
`gate_pool` module is never imported. On, this run first takes `cost` units of
the machine budget as a hand-run gate (`[prep_pr] weight`, else Form B's
effective `jobs`, else the whole budget) and waits for them; a wait that gives
up is exit 75, NOT RUN, which is neither a pass nor a failed gate.

Exit codes: 0 = all gates passed / skipped / fell open; 75 = NOT RUN (pool on,
no slot in time); 130 = SIGINT while waiting for a slot (NOT RUN); other
non-zero = a required gate failed (1) or a config error (2).

Run: python3 gate-runner.py   (from anywhere inside the repo)
"""
import glob
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

# So the sibling `orchestrate_schemas` module imports when gate-runner is run as
# a script (its dir is not otherwise on sys.path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import tomllib
except ModuleNotFoundError:  # tomllib is stdlib only in Python 3.11+
    sys.stderr.write(
        "gate-runner: requires Python 3.11+ (stdlib tomllib); found %s. "
        "Re-run with a 3.11+ interpreter.\n" % sys.version.split()[0]
    )
    raise SystemExit(2)

CONFIG_NAME = ".gates.toml"


def log(msg):
    print(msg, flush=True)


def warn(msg):
    print(f"WARN: {msg}", file=sys.stderr, flush=True)


def find_repo_root():
    """Repo root via `git rev-parse --show-toplevel`; fall back to cwd."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        if out.returncode == 0:
            root = out.stdout.strip()
            if root:
                return root
    except OSError:
        pass
    return os.getcwd()


def _run_command(label, command, cwd):
    """Run one shell command in cwd. Return True on exit 0, else False.

    shell=True is the documented trusted-repo-config path (the command came from
    `.gates.toml` / CLAUDE.md, both trusted like a Makefile). No dynamic string
    is built here -- the command is passed through verbatim."""
    # Wall time is REPORTING only (#400): it never feeds the verdict, the exit
    # code, the memo cache, or the receipt.
    start = time.perf_counter()
    try:
        proc = subprocess.run(command, shell=True, cwd=cwd, check=False)
    except OSError as e:
        log(f"[FAIL] {label}: could not launch ({e}), "
            f"{time.perf_counter() - start:.1f}s")
        return False
    elapsed = time.perf_counter() - start
    ok = proc.returncode == 0
    log(f"[{'PASS' if ok else 'FAIL'}] {label} "
        f"(exit {proc.returncode}, {elapsed:.1f}s)")
    return ok


# --- Form A / Form B over a parsed [prep_pr] table -------------------------

def _synth_records(rc):
    """A single synthesized {name:"gates", result} record for the non-granular
    paths (Form A, fallback, config error) -- keyed off the overall exit code."""
    return [{"name": "gates", "result": "pass" if rc == 0 else "fail"}]


def _valid_jobs(value):
    """A `jobs` value is a real positive int (bool is an int subclass: refused)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _step_names(steps):
    """The names a `--skip` can target, derived exactly as both Form B paths
    derive them (`name`, else `step-<index>`)."""
    return {(s.get("name") or f"step-{i}") for i, s in enumerate(steps)
            if isinstance(s, dict)}


def _shard_of(name, n):
    """The 1-based shard (1..n) a step name belongs to: sha256 of the UTF-8 name
    mod n. A stable digest, never Python's per-process-seeded `hash()`, so the
    assignment is identical across runs, machines and interpreters. The name
    ALONE decides, so a step's shard never depends on which other steps exist
    or are skipped (adding a `--skip` cannot move a step)."""
    return 1 + int(hashlib.sha256(name.encode("utf-8")).hexdigest(), 16) % n


def _apply_shard(steps, shard):
    """Keep only the steps whose name hashes to shard (k, n), from the FULL
    declared list. Unnamed steps use their derived `step-<index>` name, with the
    index taken before filtering. Non-table entries are kept so each path still
    warns about them exactly as it does unsharded."""
    k, n = shard
    kept = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            kept.append(s)
            continue
        name = s.get("name") or f"step-{i}"
        if _shard_of(name, n) == k:
            kept.append(s if s.get("name") else {**s, "name": name})
    return kept


def run_prep_pr(prep, root, memoize_dir=None, cli_jobs=None, cli_skip=None,
                cli_shard=None):
    """Run the [prep_pr] table. Return (exit_code, records). `cli_jobs` is the
    already-validated `--jobs` value (None = not given); it overrides the
    table's `jobs`, which is validated whenever present. `cli_skip` is the
    `--skip` name set (None = not given): every name must match a Form B step,
    else exit 2, because a skip that names nothing is a stale caller that would
    silently run (or, after a rename, silently stop skipping) the wrong set.
    `cli_shard` is the validated `(k, n)` pair (None = not given): Form B only,
    names must be unique, and the step list is filtered AFTER the `--skip`
    names are validated against the full list."""
    has_gate = "gate" in prep
    has_steps = "steps" in prep
    if has_gate and has_steps:
        warn("[prep_pr] sets BOTH `gate` and `steps`; they are mutually "
             "exclusive. Refusing to guess.")
        return 2, _synth_records(2)
    if "jobs" in prep and not _valid_jobs(prep["jobs"]):
        warn("[prep_pr].jobs must be a positive integer")
        return 2, _synth_records(2)
    # `weight` (#539) is the gate's machine cost. It is read by main() before this run takes
    # its pool slots and changes nothing here, but a bad value is refused with the pool ON OR
    # OFF, like `jobs`: CI configures no budget, and must still catch it.
    if "weight" in prep and not _valid_jobs(prep["weight"]):
        warn("[prep_pr].weight must be a positive integer")
        return 2, _synth_records(2)
    jobs = cli_jobs if cli_jobs is not None else prep.get("jobs", 1)
    if cli_skip and not has_steps:
        warn("`--skip` applies only to Form B `steps`; refusing to run a gate "
             "that cannot honor it")
        return 2, _synth_records(2)
    if cli_shard and not has_steps:
        warn("`--shard` applies only to Form B `steps`; refusing to run a gate "
             "that cannot honor it")
        return 2, _synth_records(2)
    if (cli_skip or cli_shard) and isinstance(prep.get("steps"), list):
        # A NAME SELECTS STEPS ONLY UNDER --skip / --shard, so duplicates are refused only here (a
        # consumer with a reused name and no flag is unaffected). Two steps sharing a name would
        # both match one --skip, so a copied block whose `run` was changed but not its `name`
        # would leave CI silently (PR review of ci/parallel-harnesses); under --shard they share
        # a shard, so the duplicate would hide. Unnamed steps count by their
        # derived `step-<index>` name, which can collide with an explicit one.
        flag = "--skip" if cli_skip else "--shard"
        if cli_shard and any(isinstance(s, dict) and s.get("name")
                             and not isinstance(s["name"], str)
                             for s in prep["steps"]):
            # The shard is a hash of the name: a non-string one cannot be hashed.
            warn("`--shard` needs every step `name` to be a string")
            return 2, _synth_records(2)
        all_names = [(s.get("name") or f"step-{i}") for i, s in enumerate(prep["steps"])
                     if isinstance(s, dict)]
        dupes = sorted({n for n in all_names if all_names.count(n) > 1})
        if dupes:
            warn(f"`{flag}` needs unique step names; duplicated: {', '.join(dupes)}")
            return 2, _synth_records(2)
        unknown = sorted((cli_skip or frozenset()) - _step_names(prep["steps"]))
        if unknown:
            warn(f"`--skip` names no Form B step: {', '.join(unknown)}")
            return 2, _synth_records(2)
    if has_gate:
        if jobs > 1:
            warn("`jobs` applies only to Form B `steps`; Form A runs serially")
        return run_form_a(prep["gate"], root)
    if has_steps:
        steps = prep["steps"]
        if cli_shard and isinstance(steps, list):
            total = len(steps)
            steps = _apply_shard(steps, cli_shard)
            log(f"gate-runner: shard {cli_shard[0]}/{cli_shard[1]}: "
                f"{len(steps)} of {total} steps")
        if jobs > 1:
            return run_form_b_parallel(steps, root, memoize_dir, jobs, cli_skip)
        return run_form_b(steps, root, memoize_dir, cli_skip)
    warn("[prep_pr] has neither `gate` nor `steps`; nothing to run.")
    return 0, _synth_records(0)


def run_form_a(gate, root):
    log(f"gate-runner: .gates.toml Form A (delegate) -> {gate!r}")
    if not isinstance(gate, str) or not gate.strip():
        warn("[prep_pr].gate must be a non-empty string")
        return 2, _synth_records(2)
    ok = _run_command("gate", gate, root)
    rc = 0 if ok else 1
    return rc, _synth_records(rc)


def _skip_reason(step, root, name=None, cli_skip=None):
    """Return a skip reason string if the step should be skipped, else None."""
    if cli_skip and name in cli_skip:
        return "--skip"
    tool = step.get("skip_if_absent")
    if tool and shutil.which(tool) is None:
        return f"{tool} not on PATH"
    pattern = step.get("skip_if")
    if pattern:
        matches = glob.glob(os.path.join(root, pattern), recursive=True)
        if not matches:
            return f"no files match {pattern}"
    return None


def run_form_b(steps, root, memoize_dir=None, cli_skip=None):
    """Run the ordered steps. Return (exit_code, records) where records is a list
    of {name, result in pass|fail|skip} in step order (for the gate receipt)."""
    records = []
    if not isinstance(steps, list):
        warn("[prep_pr].steps must be an array of step tables")
        return 2, _synth_records(2)
    log(f"gate-runner: .gates.toml Form B (enumerate) -> {len(steps)} step(s)")
    soft_failures = 0
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            warn(f"step #{i} is not a table; skipping")
            continue
        name = step.get("name") or f"step-{i}"
        run = step.get("run")
        if not isinstance(run, str) or not run.strip():
            warn(f"step {name!r} has invalid `run` (expected a non-empty string)")
            return 2, records
        required = step.get("required", True)
        if not isinstance(required, bool):
            warn(f"step {name!r} has invalid `required` (expected a boolean)")
            return 2, records
        if not isinstance(step.get("exclusive", False), bool):
            warn(f"step {name!r} has invalid `exclusive` (expected a boolean)")
            return 2, records
        reason = _skip_reason(step, root, name, cli_skip)
        if reason:
            log(f"[SKIP] {name}: {reason}")
            records.append({"name": name, "result": "skip"})
            continue

        # Pure-oracle memoization (opt-in, conservative, PASS-only): a step is
        # memoizable ONLY if it declares `pure = true`, --memoize-dir is set, the
        # committed tree resolves, AND the worktree is LIVE-clean. Fail-open: any
        # git error / dirty tree / non-pure step => run normally, no cache. A memo
        # bug degrades to re-running, never to skipping a real gate.
        memo_file = None
        if memoize_dir and step.get("pure", False) is True:
            tree = _memoizable_tree(root)
            if tree:
                memo_file = os.path.join(memoize_dir, _memo_key(tree, name, run))
                if _memo_is_pass(memo_file):
                    log(f"[MEMO] {name}: cached pass (tree {tree[:7]})")
                    records.append({"name": name, "result": "pass"})
                    continue

        ok = _run_command(name, run, root)
        # Memoize PASS ONLY -- a failing step always re-runs (user sees output).
        if ok and memo_file is not None:
            _memo_write_pass(memo_file)
        if not ok:
            if required:
                records.append({"name": name, "result": "fail"})
                log(f"gate-runner: HARD failure at {name!r} -- stopping.")
                return 1, records
            soft_failures += 1
            records.append({"name": name, "result": "fail"})
            log(f"[WARN] {name}: soft failure (required=false), continuing.")
        else:
            records.append({"name": name, "result": "pass"})
    if soft_failures:
        log(f"gate-runner: all required steps passed "
            f"({soft_failures} soft failure(s) warned, not blocking).")
    else:
        log("gate-runner: all steps passed.")
    return 0, records


# --- Parallel Form B (#501; opt-in via `jobs` > 1) ---------------------------
#
# ONE thread owns every child: a poll loop launches steps in declaration order
# (bounded by `jobs` and the `exclusive` barrier), reaps them, and prints each
# finished step's captured output as one whole block, strictly in declaration
# order. Nothing here touches the serial path above, which stays byte-identical.

KILL_GRACE_S = 3.0   # SIGTERM -> SIGKILL grace for cancelled process groups
POLL_S = 0.02


def _may_launch(running, jobs, exclusive):
    """The launch gate. Nothing starts beside a running exclusive step; an
    exclusive step starts only when nothing is in flight; else bounded by jobs."""
    if any(e["exclusive"] for e in running.values()):
        return False
    if exclusive:
        return not running
    return len(running) < jobs


def _killpg(pid, sig):
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


_LAUNCH_SIGS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _group_alive(pgid):
    """True while process group `pgid` still has a member we can signal. EPERM
    means the pgid now belongs to someone else's processes: not ours, so gone."""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _prune_lingering(lingering):
    """Drop every finished step's group that has emptied. An empty group can
    never be re-joined, and dropping it promptly keeps the final sweep from
    signalling a pgid the kernel has since recycled for an unrelated process."""
    for pgid in [g for g in lingering if not _group_alive(g)]:
        lingering.discard(pgid)


def _reap(p, block=False):
    """Reap direct child `p` WITHOUT Popen's `_waitpid_lock`; True once it is
    gone. Cleanup runs with INT/TERM/HUP ignored, so it must not depend on that
    lock (#546): if an exception ever leaked it, poll() answers None forever
    and wait() never returns. One thread owns every child, so nothing else can
    be waiting on this pid. Setting `returncode` as Popen would keeps its later
    poll(), wait() and __del__ returning at once, never touching the lock."""
    if p.returncode is not None:
        return True
    try:
        pid, status = os.waitpid(p.pid, 0 if block else os.WNOHANG)
    except ChildProcessError:
        # Already reaped (a poll() cut short after its waitpid) or SIGCHLD is
        # ignored: the status is lost. 0 is Popen's own value for this case.
        p.returncode = 0
        return True
    if pid != p.pid:
        return False
    p.returncode = os.waitstatus_to_exitcode(status)
    return True


def _terminate_groups(running, lingering=(), grace=KILL_GRACE_S):
    """SIGTERM every in-flight step's process group AND every finished step's
    still-populated group (`lingering`: a `sleep 30 &` left behind by a step
    whose shell already exited), wait up to `grace` for all of them to empty,
    then SIGKILL every group (unconditionally: a descendant that ignored SIGTERM
    can outlive its group leader) and reap every direct child."""
    procs = [e["proc"] for e in running.values()]
    groups = [p.pid for p in procs] + list(lingering)
    for g in groups:
        _killpg(g, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and (
            any(not _reap(p) for p in procs)
            or any(_group_alive(g) for g in lingering)):
        time.sleep(POLL_S)
    for g in groups:
        _killpg(g, signal.SIGKILL)
    for p in procs:
        while not _reap(p, block=True):
            pass


def _print_block(e):
    """Print one finished step whole: its log lines, captured output, verdict."""
    for line in e["pre"]:
        log(line)
    if e.get("out"):
        sys.stdout.flush()
        with open(e["out"], "rb") as f:
            shutil.copyfileobj(f, sys.stdout.buffer)
        sys.stdout.buffer.flush()
    for line in e["post"]:
        log(line)


def _flush_ready(plan, printed):
    """Print every consecutive finished step starting at index `printed`.
    Returns the new `printed` index: step k prints only after 0..k-1."""
    while printed < len(plan) and plan[printed]["state"] == "done":
        _print_block(plan[printed])
        printed += 1
    return printed


def run_form_b_parallel(steps, root, memoize_dir, jobs, cli_skip=None):
    """Parallel Form B. Same return contract as run_form_b: (exit_code, records
    in declaration order). Every step is validated BEFORE anything launches."""
    if not isinstance(steps, list):
        warn("[prep_pr].steps must be an array of step tables")
        return 2, _synth_records(2)
    log(f"gate-runner: .gates.toml Form B (enumerate) -> {len(steps)} step(s), "
        f"jobs={jobs}")
    plan = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            warn(f"step #{i} is not a table; skipping")
            continue
        name = step.get("name") or f"step-{i}"
        run = step.get("run")
        if not isinstance(run, str) or not run.strip():
            warn(f"step {name!r} has invalid `run` (expected a non-empty string)")
            return 2, []
        for key, default in (("required", True), ("exclusive", False)):
            if not isinstance(step.get(key, default), bool):
                warn(f"step {name!r} has invalid `{key}` (expected a boolean)")
                return 2, []
        plan.append({"idx": len(plan), "step": step, "name": name, "run": run,
                     "required": step.get("required", True),
                     "exclusive": step.get("exclusive", False),
                     "state": "pending", "pre": [], "post": [], "out": None,
                     "result": None, "memo": None, "checked": False})

    capdir = tempfile.mkdtemp(prefix="gate-runner-")
    running = {}
    lingering = set()   # pgids of finished steps, swept on every exit path
    nxt = printed = 0
    hard = None
    rc = 0
    # INT/TERM/HUP handlers only RECORD the signal (#546); the loop raises
    # KeyboardInterrupt itself, at the top of an iteration. A handler that
    # raises lands on ANY bytecode boundary, including inside Popen.poll()
    # between its `_waitpid_lock.acquire(False)` and the `try` that releases
    # it: the lock then stays held by nobody and cleanup's reap blocks on it
    # forever, with all three signals already ignored. Python's default SIGINT
    # handler raises the same way, so SIGINT is replaced too.
    # SIGHUP joins SIGTERM: the groups run in their own sessions, so a hangup
    # never reaches them and an unhandled one would orphan every group.
    got = []            # signals recorded so far
    in_output = [False]

    def _note(signum, frame):
        got.append(signum)
        # THE ONE PLACE A HANDLER STILL RAISES: while the loop prints finished
        # blocks. A write to a stalled stdout blocks for as long as the reader
        # likes and is retried after a handler returns, so recording alone
        # would leave the runner unstoppable there. No Popen call is in flight
        # in that window. At most once: a second signal must never raise out
        # of the cleanup below. This covers the in-loop flush ONLY: the flush
        # inside the cleanup and the final log line run with these signals
        # ignored or restored, so a stalled stdout can still hold the runner
        # there (#548).
        if in_output[0]:
            in_output[0] = False
            raise KeyboardInterrupt
    # SIGINT inherited as SIG_IGN (a non-interactive shell's `&` job) stays
    # ignored, as Python itself leaves it.
    old = {s: signal.signal(s, _note) for s in _LAUNCH_SIGS
           if not (s == signal.SIGINT
                   and signal.getsignal(s) == signal.SIG_IGN)}
    try:
        while True:
            if got:
                raise KeyboardInterrupt
            # Dispatch in declaration order. Skip predicates and memo lookups
            # run here in the parent ONCE per step (`checked`), when it first
            # reaches the head: a head blocked on capacity never re-runs them.
            while hard is None and nxt < len(plan):
                # A signal that was pending across the launch window below was
                # recorded when the mask was restored: stop launching.
                if got:
                    raise KeyboardInterrupt
                e = plan[nxt]
                if not e["checked"]:
                    e["checked"] = True
                    reason = _skip_reason(e["step"], root, e["name"], cli_skip)
                    if reason:
                        e["pre"].append(f"[SKIP] {e['name']}: {reason}")
                        e["state"], e["result"] = "done", "skip"
                        nxt += 1
                        continue
                    if memoize_dir and e["step"].get("pure", False) is True:
                        tree = _memoizable_tree(root)
                        if tree:
                            e["memo"] = os.path.join(
                                memoize_dir, _memo_key(tree, e["name"], e["run"]))
                            if _memo_is_pass(e["memo"]):
                                e["pre"].append(f"[MEMO] {e['name']}: cached "
                                                f"pass (tree {tree[:7]})")
                                e["state"], e["result"] = "done", "pass"
                                nxt += 1
                                continue
                if not _may_launch(running, jobs, e["exclusive"]):
                    break
                # The skip predicate and the memo lookup above can take a while
                # (they shell out): a signal recorded during them must stop
                # THIS step from starting, not kill it one iteration later.
                if got:
                    raise KeyboardInterrupt
                e["out"] = os.path.join(capdir, f"{nxt}.out")
                e["start"] = time.perf_counter()
                # Block INT/TERM/HUP from Popen until the step is registered in
                # `running`: a signal in between would otherwise be acted on
                # before cleanup can see the new group, orphaning it. A pending
                # signal is delivered (recorded) when the mask is restored,
                # after registration, and raised at the top of this loop.
                # The child inherits the blocked mask through fork/exec, so
                # preexec_fn restores the caller's mask there, else the step
                # could never receive the SIGTERM leg of cleanup.
                prev = signal.pthread_sigmask(signal.SIG_BLOCK, _LAUNCH_SIGS)
                try:
                    try:
                        with open(e["out"], "wb") as out:
                            e["proc"] = subprocess.Popen(
                                e["run"], shell=True, cwd=root,
                                stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True,
                                preexec_fn=lambda: signal.pthread_sigmask(
                                    signal.SIG_SETMASK, prev))
                    except OSError as err:
                        e["post"].append(
                            f"[FAIL] {e['name']}: could not launch ({err}), "
                            f"{time.perf_counter() - e['start']:.1f}s")
                        e["state"], e["result"] = "done", "fail"
                        if e["required"]:
                            hard = e
                        else:
                            e["post"].append(f"[WARN] {e['name']}: soft failure "
                                             "(required=false), continuing.")
                        nxt += 1
                        continue
                    e["state"] = "running"
                    running[nxt] = e
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, prev)
                nxt += 1
            # Reap.
            _prune_lingering(lingering)
            for idx in list(running):
                e = running[idx]
                code = e["proc"].poll()
                if code is None:
                    continue
                del running[idx]
                # The leader is reaped, but a background descendant may still
                # hold its group; keep that group for the final sweep.
                lingering.add(e["proc"].pid)
                ok = code == 0
                e["post"].append(f"[{'PASS' if ok else 'FAIL'}] {e['name']} "
                                 f"(exit {code}, "
                                 f"{time.perf_counter() - e['start']:.1f}s)")
                e["state"], e["result"] = "done", "pass" if ok else "fail"
                if ok and e["memo"] is not None:
                    _memo_write_pass(e["memo"])
                elif not ok and e["required"]:
                    if hard is None or e["idx"] < hard["idx"]:
                        hard = e
                elif not ok:
                    e["post"].append(f"[WARN] {e['name']}: soft failure "
                                     "(required=false), continuing.")
            if hard is not None:
                break
            try:
                in_output[0] = True   # inside the try: no window leaves it set
                printed = _flush_ready(plan, printed)
            finally:
                in_output[0] = False
            if nxt >= len(plan) and not running:
                break
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        rc = 130
    finally:
        # Ignore further INT/TERM/HUP while cleaning up: a second Ctrl-C inside
        # the kill grace would otherwise abort it, orphaning the groups and
        # leaking capdir. Restored after the rmtree.
        masked = {s: signal.signal(s, signal.SIG_IGN) for s in _LAUNCH_SIGS}
        # A signal recorded after the loop's last check (in the iteration that
        # finished or failed the run) is still an interrupt, never a pass.
        if got:
            rc = 130
        # Sweep on EVERY exit path, all-pass included: a finished step's stray
        # background job is never wanted and must not outlive the run.
        _prune_lingering(lingering)
        if running or lingering:
            _terminate_groups(running, lingering)
            for e in running.values():
                e["post"].append(f"[FAIL] {e['name']} (cancelled, "
                                 f"{time.perf_counter() - e['start']:.1f}s)")
                e["out"] = None   # partial output of a killed step: discarded
                e["state"], e["result"] = "done", "fail"
                e["cancelled"] = True
            running.clear()
        if rc == 0:
            printed = _flush_ready(plan, printed)
        shutil.rmtree(capdir, ignore_errors=True)
        for s in _LAUNCH_SIGS:   # a SIGINT left ignored above goes back to that
            signal.signal(s, old.get(s, masked[s]))

    records = []
    for e in plan:
        if e["state"] != "done":
            continue   # never launched (after a stop): no record, as serial
        rec = {"name": e["name"], "result": e["result"]}
        if e.get("cancelled"):
            rec["cancelled"] = True
        records.append(rec)
    if rc == 130:
        log("gate-runner: interrupted -- all in-flight steps killed.")
        return rc, records
    if hard is not None:
        log(f"gate-runner: HARD failure at {hard['name']!r} -- stopping.")
        return 1, records
    soft = sum(1 for e in plan if e["result"] == "fail")
    if soft:
        log(f"gate-runner: all required steps passed "
            f"({soft} soft failure(s) warned, not blocking).")
    else:
        log("gate-runner: all steps passed.")
    return 0, records


# --- Fail-open fallback chain (no .gates.toml) -----------------------------

def fallback_chain(root):
    """No `.gates.toml`: run the fallback chain. Return (exit_code, records)
    with a single synthesized `gates` record (fallbacks are not per-step)."""
    rc = _fallback_chain_rc(root)
    return rc, _synth_records(rc)


def _fallback_chain_rc(root):
    """No `.gates.toml`: pick a fallback layer and run it. Always returns an
    exit code; NEVER hard-blocks a config-less repo (terminal layer exits 0)."""
    log(f"gate-runner: no {CONFIG_NAME} found; entering fail-open fallback chain.")

    # Layer 1: known umbrella.
    rc = _fallback_umbrella(root)
    if rc is not None:
        return rc

    # Layer 2: CLAUDE.md `## Gates` block.
    rc = _fallback_claude_md(root)
    if rc is not None:
        return rc

    # Layer 3: language-agnostic basics from a manifest.
    rc = _fallback_basics(root)
    if rc is not None:
        return rc

    # Layer 4: nothing detectable -- warn and proceed.
    warn("no gate definition found, proceeding without gates")
    log("gate-runner: fallback layer 4 (none) -- PROCEED, exit 0.")
    return 0


def _fallback_umbrella(root):
    """Layer 1. `make gate` target, then scripts/pre-push-gate.sh. Returns an
    exit code if this layer applies, else None."""
    if shutil.which("make"):
        try:
            probe = subprocess.run(
                ["make", "-n", "gate"], cwd=root,
                capture_output=True, text=True, check=False,
            )
        except OSError:
            probe = None
        if probe is not None and probe.returncode == 0:
            log("gate-runner: fallback layer 1 (umbrella) -> `make gate`.")
            ok = _run_command("make gate", "make gate", root)
            return 0 if ok else 1
    gate_sh = os.path.join(root, "scripts", "pre-push-gate.sh")
    if os.path.isfile(gate_sh) and os.access(gate_sh, os.X_OK):
        log("gate-runner: fallback layer 1 (umbrella) -> scripts/pre-push-gate.sh.")
        ok = _run_command("pre-push-gate.sh",
                          "bash scripts/pre-push-gate.sh", root)
        return 0 if ok else 1
    return None


def _extract_gates_block(text):
    """Pull the command lines out of a `## Gates` block in CLAUDE.md. The block
    runs from a `## Gates` heading to the next `## ` heading; its commands live
    in a fenced ```sh code block. Returns a list of command lines (in order)."""
    m = re.search(r'^##+\s+Gates\b.*?$', text, re.MULTILINE | re.IGNORECASE)
    if not m:
        return []
    start = m.end()
    nxt = re.search(r'^##+\s+\S', text[start:], re.MULTILINE)
    section = text[start:start + nxt.start()] if nxt else text[start:]
    fences = re.findall(r'```[a-zA-Z]*\n(.*?)```', section, re.DOTALL)
    cmds = []
    for fence in fences:
        for raw in fence.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Drop trailing inline comments (kept simple; trusted config).
            cmds.append(line)
    return cmds


def _fallback_claude_md(root):
    """Layer 2. Run the command lines from CLAUDE.md's `## Gates` block in
    order. Returns an exit code if a block was found, else None."""
    claude_md = os.path.join(root, "CLAUDE.md")
    if not os.path.isfile(claude_md):
        return None
    try:
        with open(claude_md, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    cmds = _extract_gates_block(text)
    if not cmds:
        return None
    log(f"gate-runner: fallback layer 2 (CLAUDE.md ## Gates) -> "
        f"{len(cmds)} command(s).")
    for cmd in cmds:
        ok = _run_command(cmd, cmd, root)
        if not ok:
            log(f"gate-runner: HARD failure at {cmd!r} -- stopping.")
            return 1
    log("gate-runner: all CLAUDE.md gate commands passed.")
    return 0


def _fallback_basics(root):
    """Layer 3. Infer a basic test runner from a repo manifest. WARN about the
    inference before running. Returns an exit code if a manifest matched, else
    None."""
    if os.path.isfile(os.path.join(root, "go.mod")):
        warn("inferring `go test ./...` from go.mod (no .gates.toml / "
             "## Gates / umbrella)")
        log("gate-runner: fallback layer 3 (basics) -> go test ./...")
        ok = _run_command("go test", "go test ./...", root)
        return 0 if ok else 1
    if os.path.isfile(os.path.join(root, "package.json")):
        warn("inferring `npm test` from package.json (no .gates.toml / "
             "## Gates / umbrella)")
        log("gate-runner: fallback layer 3 (basics) -> npm test")
        ok = _run_command("npm test", "npm test", root)
        return 0 if ok else 1
    py_harnesses = sorted(glob.glob(os.path.join(root, "test-*.py")))
    if py_harnesses:
        warn("inferring `python3 test-*.py` harnesses (no .gates.toml / "
             "## Gates / umbrella)")
        log(f"gate-runner: fallback layer 3 (basics) -> "
            f"{len(py_harnesses)} python3 test-*.py harness(es)")
        for h in py_harnesses:
            rel = os.path.relpath(h, root)
            ok = _run_command(rel, f"python3 {rel}", root)
            if not ok:
                log(f"gate-runner: HARD failure at {rel!r} -- stopping.")
                return 1
        log("gate-runner: all inferred python3 harnesses passed.")
        return 0
    return None


# --- git helpers (fail-open: return None on any error) ----------------------

def _git_out(args, root):
    """`git <args>` in root; return stripped stdout on exit 0, else None."""
    try:
        out = subprocess.run(["git"] + args, cwd=root,
                             capture_output=True, text=True, check=False)
    except OSError:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _worktree_clean(root):
    """True iff `git status --porcelain` is EMPTY -- no staged, unstaged, OR
    UNTRACKED changes. Untracked files MUST count as dirty (#229 false-pass the
    hostile review caught): a `pure` step that discovers files by glob (a linter
    or test over a directory) would otherwise memo-pass while an untracked input
    file that fails it sits on disk -- and untracked files are the normal state
    of in-progress work. Consequence: the `--memoize-dir` MUST live OUTSIDE the
    worktree or be gitignored (`--porcelain` hides gitignored files), else the
    cache dir itself shows as untracked and nothing is ever memoizable.
    Fail-open: any error => not clean."""
    try:
        p = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                          capture_output=True, text=True, check=False)
    except OSError:
        return False
    if p.returncode != 0:
        return False
    return p.stdout.strip() == ""


# --- Pure-oracle memoization helpers ---------------------------------------

def _memoizable_tree(root):
    """The committed tree sha IF it resolves AND the worktree is clean, else
    None. This is the whole eligibility gate for a memoizable step."""
    tree = _git_out(["rev-parse", "HEAD^{tree}"], root)
    if not tree:
        return None
    if not _worktree_clean(root):
        return None
    return tree


def _memo_key(tree, name, run):
    """Cache key: sha256(tree \0 name \0 run). Keyed on the committed TREE only
    (assumes a fixed toolchain within the memo window -- a shellcheck/ruff/python
    version bump at constant tree is NOT detected; documented in gates.toml.md)."""
    h = hashlib.sha256()
    h.update((tree + "\0" + name + "\0" + run).encode("utf-8"))
    return h.hexdigest()


def _memo_is_pass(memo_file):
    try:
        with open(memo_file, encoding="utf-8") as f:
            return f.read().strip() == "pass"
    except OSError:
        return False


def _memo_write_pass(memo_file):
    """Atomically record a PASS. Fail-open: a write error only warns (the gate
    already ran and passed; a missing cache entry just re-runs next time)."""
    tmp = f"{memo_file}.tmp.{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(memo_file), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("pass\n")
        os.replace(tmp, memo_file)
    except OSError as e:
        warn(f"could not write memo cache {memo_file}: {e}")
        # Mirror _atomic_write_json: don't leave a *.tmp.<pid> behind on failure.
        try:
            os.unlink(tmp)
        except OSError:
            pass


# --- Gate receipt (--receipt) ----------------------------------------------

def _snapshot(root, receipt_path):
    """Pre-run snapshot: (commit, tree, dirty). Any git error leaves commit/tree
    empty or dirty non-empty: doubt = no pass. Never raises."""
    commit = _git_out(["rev-parse", "HEAD"], root)
    tree = _git_out(["rev-parse", "HEAD^{tree}"], root)
    dirty = _tree_dirty_excluding(root, receipt_path)
    return commit, tree, dirty


def _write_receipt(path, root, rc, records, pre):
    """Write a `gate-receipt/v1` receipt (schema-validated, atomic). The receipt
    is a BYPRODUCT: any failure here WARNs and never changes the gate exit code.
    FAIL-OPEN when HEAD/tree cannot resolve (not a git repo / no commit).
    `pre` is the _snapshot taken BEFORE the gate ran."""
    pre_commit, pre_tree, pre_dirty = pre
    if not pre_commit or not pre_tree:
        # #497: no receipt for this run, so an OLDER one at the path must not
        # survive to be read as this run's verdict (git missing used to leave a
        # stale pass in place). Only the exact path is removed, matching
        # _remove_stale: a `.tmp.<pid>` leftover is never read as a receipt,
        # and globbing tmp names could unlink a concurrent writer's in-flight file.
        removed = _remove_stale(path)
        warn("cannot resolve HEAD/tree (git missing, not a git repo, or no "
             "commit); skipping gate receipt"
             + ("" if removed else " (an older receipt could NOT be removed; see above)"))
        return
    # #481 R7 + review round 1 (TOCTOU): the receipt binds HEAD^{tree} but the
    # gate tested the WORKING tree. A pass therefore needs rc==0 AND a clean
    # tree BEFORE the run AND a clean tree AFTER it AND an unchanged tree sha
    # across the run (a mid-run discard or commit would otherwise let a gate
    # that saw dirt, or a different tree, bind a clean tree). A git error
    # anywhere is doubt = no pass. Otherwise write result=fail with a `reason`,
    # which also OVERWRITES any older pass at this path. The gate exit code is
    # untouched (the receipt stays a byproduct). The receipt records the PRE-run
    # commit/tree: that is what the gate tested.
    # ACCEPTED WINDOW (#497, decided not to close): these are two point-in-time
    # reads, before the first step and after the last. A pass therefore says
    # "HEAD^{tree} was clean and unchanged at both ends", NOT "every step read
    # exactly HEAD^{tree}": content that existed only mid-run (clean -> dirty ->
    # clean while the gate runs) is tested but never bound, and still passes.
    # Closing it means running the gate in an exported checkout of HEAD^{tree};
    # not adopted, because it needs a concurrent editor (outside the
    # honest-actor model), costs a full extra checkout per run, and breaks
    # gates that read untracked config or caches from the live worktree.
    reason = ""
    if pre_dirty:
        reason = "dirty-before-run: " + pre_dirty
    else:
        post_tree = _git_out(["rev-parse", "HEAD^{tree}"], root)
        post_dirty = _tree_dirty_excluding(root, path)
        if not post_tree:
            # Redundant for the verdict (an empty post_tree would also fail the
            # != test below); kept only so the reason says "unresolvable".
            reason = "tree-changed-during-run: " + pre_tree + "..unresolvable"
        elif post_tree != pre_tree:
            reason = ("tree-changed-during-run: " + pre_tree + ".."
                      + post_tree)
        elif post_dirty:
            reason = "dirty-after-run: " + post_dirty
    receipt = {
        "schema": "gate-receipt/v1",
        "commit_sha": pre_commit,
        "tree_sha": pre_tree,
        "worktree": root,
        # verdict chosen HERE: pass needs the tool's own exit code 0 AND no
        # receipt invariant tripped (`reason` empty: clean before and after,
        # tree unchanged). Do not reduce this to the exit code alone.
        "result": "pass" if (rc == 0 and not reason) else "fail",
        "steps": records,
        "producer": "gate-runner",
    }
    if reason:
        receipt["reason"] = reason + "; gate tested the working tree, not a clean unchanged HEAD^{tree}"
        warn("gate receipt written as result=fail (" + reason + ")")
    if receipt["result"] != "pass":
        # Belt-and-braces, for EVERY non-pass (a plain gate failure on a clean
        # tree included, PR #498 review): if writing the fail receipt below
        # itself fails (schema/write error), the older pass must not survive.
        _remove_stale(path)
    try:
        import orchestrate_schemas
    except Exception as e:  # degraded but functional: write without validation.
        warn(f"could not import orchestrate_schemas ({e}); "
             "writing gate receipt without schema validation")
        _atomic_write_json(path, receipt)
        return
    errors = orchestrate_schemas.validate("gate-receipt/v1", receipt)
    if errors:  # never emit a malformed receipt.
        warn("gate receipt failed schema validation; NOT writing: "
             + "; ".join(errors))
        return
    _atomic_write_json(path, receipt)


def _remove_stale(path):
    """Best-effort removal of an older receipt so it cannot survive if the
    fail receipt below cannot be written (e.g. schema or write error).
    Returns True when no receipt remains at the path (removed, or none was
    there), False when an existing one could not be removed (warned)."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        # #497 review: a silent failure here (e.g. a read-only parent dir) let a
        # stale pass survive while the caller's warning claimed it was removed.
        warn(f"could not remove older gate receipt {path}: {e}")
        return False
    return True


def _porcelain_paths(root):
    """Run `git status --porcelain -z --untracked-files=normal`; return a list
    of paths, or None on any error. -z avoids quoting; a rename carries two
    NUL-separated paths (both are returned)."""
    try:
        p = subprocess.run(["git", "status", "--porcelain", "-z",
                            "--untracked-files=normal"],
                           cwd=root, capture_output=True, check=False)
    except OSError:
        return None
    if p.returncode != 0:
        return None
    fields = p.stdout.decode("utf-8", "surrogateescape").split("\0")
    out, i = [], 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if len(f) < 4:
            continue
        out.append(f[3:])
        if f[0] in "RC":  # rename/copy: the next field is the source path.
            if i < len(fields) and fields[i]:
                out.append(fields[i])
            i += 1
    return out


def _tree_dirty_excluding(root, receipt_path):
    """'' when the worktree is clean ignoring the receipt file itself (and its
    atomic-write tmp); else a short description. Any git error => dirty."""
    paths = _porcelain_paths(root)
    if paths is None:
        return "git status failed"
    rroot = os.path.realpath(root)
    rpath = os.path.realpath(receipt_path)

    def own(p):
        # The receipt itself, or a leftover of _atomic_write_json's temp file
        # (`<receipt>.tmp.<pid>`; an interrupted write can leave one). Without
        # the tmp leg, one interrupted write inside the worktree turned every
        # later clean gate into result=fail (PR #498 review).
        # The tmp leg matches ONLY a plain FILE with an ASCII-digit suffix:
        # git reports a whole untracked DIRECTORY as one `name/` entry, and
        # realpath strips the slash, so without the isdir test a directory
        # named `<receipt>.tmp.123/` would hide every file inside it.
        # Accepted limit: a TRACKED file with that exact name is matched by
        # path alone; only a receipt path inside the worktree is exposed, and
        # the standard path lives under the git-dir, where status shows nothing.
        if p == rpath:
            return True
        suffix = p[len(rpath) + 5:]
        return (p.startswith(rpath + ".tmp.") and suffix.isascii()
                and suffix.isdigit() and not os.path.isdir(p))

    dirty = []
    for rel in paths:
        ap = os.path.realpath(os.path.join(rroot, rel))
        if own(ap):
            continue
        if rel.endswith("/") and rpath.startswith(ap + os.sep):
            # untracked DIR holding the receipt: look inside for anything else.
            # `normal` collapses the dir; expand untracked files explicitly.
            try:
                lst = subprocess.run(
                    ["git", "ls-files", "--others", "--exclude-standard",
                     "-z", "--", rel], cwd=root, capture_output=True,
                    check=False)
            except OSError:
                return "git ls-files failed"
            if lst.returncode != 0:
                return "git ls-files failed"
            names = [n for n in lst.stdout.decode(
                "utf-8", "surrogateescape").split("\0") if n]
            others = [n for n in names
                      if not own(os.path.realpath(os.path.join(rroot, n)))]
            if others:
                dirty.append(others[0])
            continue
        dirty.append(rel)
    if dirty:
        more = f" (+{len(dirty) - 1} more)" if len(dirty) > 1 else ""
        return dirty[0] + more
    return ""


def _atomic_write_json(path, obj):
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        warn(f"could not write gate receipt to {path}: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


# --- Entry point ------------------------------------------------------------

def _parse_args(argv):
    """Parse the optional flags. Returns (receipt_path, memoize_dir, jobs,
    skip). All default None; unknown args are warned and ignored (never fatal).
    `jobs` is the RAW --jobs string, validated in _run_gates so a bad value
    exits 2. `skip` is the list of raw --skip values (None when never given),
    likewise validated in _run_gates."""
    receipt_path = None
    memoize_dir = None
    jobs = None
    skip = None
    shard = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--receipt":
            i += 1
            if i >= len(argv):
                warn("--receipt requires a path argument; ignoring")
                break
            receipt_path = argv[i]
        elif a.startswith("--receipt="):
            receipt_path = a[len("--receipt="):]
        elif a == "--memoize-dir":
            i += 1
            if i >= len(argv):
                warn("--memoize-dir requires a directory argument; ignoring")
                break
            memoize_dir = argv[i]
        elif a.startswith("--memoize-dir="):
            memoize_dir = a[len("--memoize-dir="):]
        elif a == "--jobs":
            i += 1
            jobs = argv[i] if i < len(argv) else ""
        elif a.startswith("--jobs="):
            jobs = a[len("--jobs="):]
        elif a == "--skip":
            i += 1
            skip = (skip or []) + [argv[i] if i < len(argv) else ""]
        elif a.startswith("--skip="):
            skip = (skip or []) + [a[len("--skip="):]]
        elif a == "--shard":
            i += 1
            shard = argv[i] if i < len(argv) else ""
        elif a.startswith("--shard="):
            shard = a[len("--shard="):]
        else:
            warn(f"unrecognized argument {a!r}; ignoring")
        i += 1
    return receipt_path, memoize_dir, jobs, skip, shard


def _run_gates(root, memoize_dir, jobs=None, skip=None, shard=None, raw=None):
    """Resolve config and run the gates. Return (exit_code, records). `jobs` is
    the raw --jobs string or None; `skip` the raw --skip list or None; `shard`
    the raw --shard string or None. `raw` (pool on only) is the bytes of a
    `.gates.toml` that _pool_cost already judged INVALID: they are parsed
    instead of the file, so the config that was refused a ticket is the one
    rejected here, never a version fixed in between (which would run unpooled)."""
    if shard is not None:
        m = re.fullmatch(r"([0-9]+)/([0-9]+)", shard)
        if not m or not 1 <= int(m.group(1)) <= int(m.group(2)):
            warn(f"--shard must be K/N with 1 <= K <= N (got {shard!r})")
            return 2, _synth_records(2)
        shard = (int(m.group(1)), int(m.group(2)))
    if skip is not None:
        if any(not s.strip() for s in skip):
            warn("--skip requires a step name")
            return 2, _synth_records(2)
        skip = frozenset(skip)
    if jobs is not None:
        if not re.fullmatch(r"[0-9]+", jobs) or int(jobs) < 1:
            warn(f"--jobs must be a positive integer (got {jobs!r})")
            return 2, _synth_records(2)
        jobs = int(jobs)
    config_path = os.path.join(root, CONFIG_NAME)
    if raw is None and not os.path.isfile(config_path):
        if skip:
            warn("`--skip` applies only to Form B `steps`; the fallback chain "
                 "cannot honor it")
            return 2, _synth_records(2)
        if shard:
            warn("`--shard` applies only to Form B `steps`; the fallback chain "
                 "cannot honor it")
            return 2, _synth_records(2)
        if jobs is not None and jobs > 1:
            warn("`--jobs` applies only to Form B `steps`; the fallback chain "
                 "runs serially")
        return fallback_chain(root)
    try:
        if raw is None:
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
        else:
            data = tomllib.load(io.BytesIO(raw))
    except (OSError, tomllib.TOMLDecodeError) as e:
        # A present-but-broken config is a real error (unlike a missing one).
        warn(f"could not parse {CONFIG_NAME}: {e}")
        return 2, _synth_records(2)
    prep = data.get("prep_pr")
    if not isinstance(prep, dict):
        warn(f"{CONFIG_NAME} is present but has no valid [prep_pr] table; failing closed.")
        return 2, _synth_records(2)
    log(f"gate-runner: using {config_path}")
    return run_prep_pr(prep, root, memoize_dir, jobs, skip, shard)


# --- Machine-wide gate pool (#539; OFF unless the user configured a budget) ---
#
# Design of record: skills/orchestrate/design/DESIGN-gate-pool.md. The config reader lives
# HERE, not in gate_pool.py, because it must run with the pool off (a typo such as `budegt`
# has to exit 2, never read as "no budget") while the module is never imported then.

POOL_KEYS = ("protocol", "budget", "backfill_bypass_limit", "small_check_cap",
             "wait_timeout_s", "job_timeout_s")


def _pool_home():
    """The pool root: $GATEQ_HOME (empty reads as unset), else ~/.claude/gate-queue."""
    return os.environ.get("GATEQ_HOME") or os.path.join(
        os.path.expanduser("~"), ".claude", "gate-queue")


def _pool_config():
    """The machine config -> ("off", info) | ("error", message) | ("on", cfg). Reads
    <pool root>/config.toml. Never imports gate_pool, never writes, never creates.

    OFF is exactly: no file, no `[pool]` table, or a `[pool]` table with no `budget` (info
    then says so, for doctor). Everything else that is not a valid config is an ERROR, so a
    typo can never silently disable the bound it configures: an unreadable or malformed file,
    a `[pool]` that is no table, ANY key outside POOL_KEYS, a value that is not a positive
    integer, a `budget` with no `protocol`. Tables other than `[pool]` are ignored (unit B
    adds its keys in a table of its own). `cfg` is the validated `[pool]` table."""
    path = os.path.join(_pool_home(), "config.toml")
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        for p in (path, _pool_home()):     # a dangling symlink is no "missing": it is a config
            if os.path.islink(p):          # that cannot be read, so never read as OFF
                return "error", f"{p}: is a dangling symlink"
        return "off", None
    except (OSError, ValueError) as e:     # ValueError: a TOML or a UTF-8 decode error
        return "error", f"{path}: {e}"
    for key in ("budget", "protocol"):
        if key in data:                    # outside [pool]: would read as no budget at all
            return "error", f"{path}: top-level `{key}` belongs under [pool]"
    if "pool" not in data:
        return "off", None
    pool = data["pool"]
    if not isinstance(pool, dict):
        return "error", f"{path}: [pool] must be a table"
    for key in pool:
        if key not in POOL_KEYS:
            return "error", f"{path}: unknown key [pool].{key}"
    for key in POOL_KEYS:
        if key in pool and not _valid_jobs(pool[key]):
            return "error", f"{path}: [pool].{key} must be a positive integer"
    if "budget" not in pool:
        return "off", "pool-table-without-budget"
    if "protocol" not in pool:
        return "error", f"{path}: [pool].budget needs [pool].protocol"
    return "on", dict(pool)


def _pool_cost(root, jobs_raw, budget):
    """What this invocation costs -> (cost, raw). Parses `.gates.toml` OUTSIDE any lock.

    cost: `[prep_pr] weight`, else the `jobs` Form B will actually use (`--jobs` wins), else
    the WHOLE budget (Form A, the fallback chain, a Form B table declaring neither: an
    undeclared cost is assumed heavy and runs alone), clamped to the budget.
    cost None: this invocation's config is one _run_gates rejects with exit 2 before it runs
    anything (a bad `--jobs`, an unparseable file, no `[prep_pr]` table, both `gate` and
    `steps`, a bad `jobs`, a bad `weight`); it takes no ticket. `raw` is then the bytes that
    were judged (None when no file was read), for _run_gates to reject THOSE."""
    cli = None
    if jobs_raw is not None:
        if not re.fullmatch(r"[0-9]+", jobs_raw) or int(jobs_raw) < 1:
            return None, None
        cli = int(jobs_raw)
    config_path = os.path.join(root, CONFIG_NAME)
    if not os.path.isfile(config_path):
        return budget, None                # the fallback chain
    try:
        with open(config_path, "rb") as f:
            raw = f.read()
    except OSError:
        # Unreadable right now: judged again after the grant, pooled at the whole budget.
        return budget, None
    try:
        prep = tomllib.load(io.BytesIO(raw)).get("prep_pr")
    except ValueError:
        return None, raw
    if not isinstance(prep, dict) or ("gate" in prep and "steps" in prep) \
            or ("jobs" in prep and not _valid_jobs(prep["jobs"])) \
            or ("weight" in prep and not _valid_jobs(prep["weight"])):
        return None, raw
    if "weight" in prep:
        return min(prep["weight"], budget), raw
    jobs = cli if cli is not None else prep.get("jobs")
    if "steps" in prep and jobs is not None:
        return min(jobs, budget), raw
    return budget, raw


def _pool_say(line):
    """A pool line: stderr, no `WARN:` prefix (a wait is not a warning)."""
    try:
        print(line, file=sys.stderr, flush=True)
    except OSError:                        # a closed stderr must not change the exit code
        pass


def _pool_enter(cfg, root, cost):
    """Take `cost` units as a hand-run gate (kind `gate`, class 2), waiting for them ->
    (holder, None), or (None, exit code) with the reason already printed: 2 for a pool this
    code cannot use, 75 for a wait that gave up (NOT RUN), 130 for an interrupted wait.
    A run started BY a holder in this worktree goes under that holder's slots instead
    (gate_pool decides, from GATEQ_HOLDER). The module is imported only here."""
    here = os.path.dirname(os.path.abspath(__file__))
    real = os.path.dirname(os.path.realpath(__file__))
    where = next((d for d in (here, real)
                  if os.path.isfile(os.path.join(d, "gate_pool.py"))), None)
    if where is None:
        # Never fall back to running unpooled: the user configured a bound.
        _pool_say("gate-runner: NOT RUN - pool configured but gate_pool.py is missing")
        return None, 2
    try:
        if where not in sys.path:          # a runner reached through a symlink
            sys.path.insert(0, where)
        import gate_pool
        try:
            # Pool() refuses another pool protocol and a root that is not the user's own 0700
            # directory BEFORE it touches anything there.
            pool = gate_pool.Pool(_pool_home(), cfg)
            # The worktree key runs git, so it is resolved HERE, before admit.lock is ever
            # taken, from the worktree ROOT (a subdirectory would get a different key).
            key = gate_pool.worktree_key(root)
            return pool.acquire("gate", "gate", cost, key, environ=os.environ,
                                say=_pool_say), None
        except gate_pool.NotRun as e:
            _pool_say(e.message)
            return None, e.code
        except KeyboardInterrupt:          # the ticket was given up by acquire()
            _pool_say("gate-runner: NOT RUN - interrupted while waiting for a gate slot")
            return None, 130
        except OSError as e:               # a pool directory this run cannot use: never unpooled
            _pool_say(f"gate-runner: NOT RUN - pool error: {e}")
            return None, 2
    except Exception as e:                 # a broken gate_pool.py: never a traceback, never unpooled
        _pool_say(f"gate-runner: NOT RUN - pool error: {type(e).__name__}: {e}")
        return None, 2


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    receipt_path, memoize_dir, jobs, skip, shard = _parse_args(argv)
    if receipt_path is not None and shard is not None:
        # Same argument as --skip: a shard attests to a subset of the gate.
        warn("`--shard` cannot be combined with `--receipt`: a receipt must "
             "attest the whole gate")
        return 2
    if receipt_path is not None and skip is not None:
        # A receipt attests that the gate PASSED; one written by a run that
        # skipped steps on request would attest to less than the gate. CI (the
        # one --skip caller) never writes a receipt, so refuse the pair.
        warn("`--skip` cannot be combined with `--receipt`: a receipt must "
             "attest the whole gate")
        return 2
    root = find_repo_root()
    state, pool = _pool_config()
    if state == "error":
        _pool_say(f"gate-runner: NOT RUN - {pool}")
        if receipt_path:                   # no step ran: nothing may read as this run's (#497)
            _remove_stale(receipt_path)
        return 2
    holder = raw = None
    if state == "on":
        # Cost first, outside every lock. A config this run would reject anyway takes no
        # ticket: it falls through and _run_gates rejects the bytes that were judged.
        cost, raw = _pool_cost(root, jobs, pool["budget"])
        if cost is not None:
            holder, code = _pool_enter(pool, root, cost)
            if holder is None:
                if receipt_path:
                    _remove_stale(receipt_path)
                return code
            raw = None                     # granted: re-read .gates.toml and run THAT definition
            # Children get the holder from THIS run's own state, never an inherited value.
            os.environ.update(holder.child_env())
            if not os.environ["GATEQ_NEST"]:
                del os.environ["GATEQ_NEST"]
    try:
        # The snapshot is taken AFTER the grant, so a wait never widens what a receipt attests.
        pre = _snapshot(root, receipt_path) if receipt_path else None
        rc, records = _run_gates(root, memoize_dir, jobs, skip, shard, raw=raw)
        if receipt_path:
            _write_receipt(receipt_path, root, rc, records, pre)
    finally:
        if holder is not None:
            try:
                holder.release()
            except OSError as e:           # the kernel drops the locks at exit anyway
                _pool_say(f"gate-runner: note: could not release the gate slots: {e}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
