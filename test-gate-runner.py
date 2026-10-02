#!/usr/bin/env python3
"""Proof harness for scripts/gate-runner.py (#169).

The gate-runner reads a repo's `.gates.toml` (or falls back through a fail-open
detection chain) and runs the gates. This harness exercises it END-TO-END in
isolated temp dirs: it builds a throwaway repo per case, writes a `.gates.toml`
(or omits it to drive the fallback chain), invokes the REAL gate-runner.py as a
subprocess, and asserts on its exit code + the per-step PASS/SKIP/FAIL lines.

Isolation: every case runs in its own tempfile.TemporaryDirectory(). To make
`git rev-parse --show-toplevel` resolve the temp dir as the repo root (the
runner finds the root that way), each temp repo is `git init`-ed. PATH is
controlled per-case (a temp bin dir prepended, or a tool removed) to drive the
skip predicates and the umbrella/basics fallbacks deterministically -- the
harness NEVER depends on what is installed on the host.

Contract asserted: exit 0 = all gates passed / skipped / fell open; non-zero =
a required gate failed (1) or a config error (2). Form A, Form B, both skip
predicates, required=false soft-fail, every fallback layer, and the terminal
fail-open (no config + nothing detectable -> exit 0).

Run: python3 test-gate-runner.py
"""
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "scripts", "gate-runner.py")

# Import the shared schema validator the same way gate-runner does, to assert the
# receipt actually conforms (not just that we spelled the fields the same way).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import orchestrate_schemas  # noqa: E402

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


def git_env(root):
    """Env that isolates git from the host's global/system config."""
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.path.join(root, ".gitconfig-none")
    env["GIT_CONFIG_SYSTEM"] = os.path.join(root, ".gitconfig-none-sys")
    return env


def git_init(root):
    """Init a quiet temp git repo so --show-toplevel resolves to root."""
    subprocess.run(["git", "init", "-q"], cwd=root, env=git_env(root), check=True)


def git_commit(root, msg="init"):
    """Stage everything and commit, so `git rev-parse HEAD` resolves.

    Identity is passed via -c (the isolated env has no user.name/email)."""
    env = git_env(root)
    subprocess.run(["git", "add", "-A"], cwd=root, env=env, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "-m", msg],
        cwd=root, env=env, check=True,
    )


def git_head(root):
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=git_env(root),
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def write(root, relpath, content, *, executable=False):
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(relpath) else None
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if executable:
        os.chmod(path, 0o755)
    return path


def run_runner(root, *, extra_path=None, drop_tools=(), args=()):
    """Invoke the real gate-runner.py inside `root`. Returns (rc, stdout+stderr).

    extra_path: a dir prepended to PATH (so a fake tool resolves).
    drop_tools: tool names to make absent -- we build a SANITIZED PATH of
    symlinks to the real binaries EXCEPT the dropped ones, so shutil.which()
    returns None for them regardless of the host.
    args: extra CLI args appended after the runner path (e.g. --receipt)."""
    env = dict(os.environ)
    parts = []
    if extra_path:
        parts.append(extra_path)
    if drop_tools:
        sandbox_bin = os.path.join(root, ".sandbox-bin")
        os.makedirs(sandbox_bin, exist_ok=True)
        # Mirror every binary on the current PATH except the dropped ones.
        seen = set()
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if not d or not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                if name in drop_tools or name in seen:
                    continue
                src = os.path.join(d, name)
                if os.path.isfile(src) and os.access(src, os.X_OK):
                    link = os.path.join(sandbox_bin, name)
                    if not os.path.lexists(link):
                        try:
                            os.symlink(src, link)
                            seen.add(name)
                        except OSError:
                            pass
        parts.append(sandbox_bin)
        env["PATH"] = os.pathsep.join(parts)
    elif parts:
        env["PATH"] = os.pathsep.join(parts + [os.environ.get("PATH", "")])
    proc = subprocess.run(
        [sys.executable, RUNNER, *args], cwd=root, env=env,
        capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


# --- Form A (delegate) ------------------------------------------------------

def test_form_a_pass():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "ok.sh", "#!/bin/sh\nexit 0\n", executable=True)
        write(root, ".gates.toml", '[prep_pr]\ngate = "sh ok.sh"\n')
        rc, out = run_runner(root)
        check("Form A: pass -> exit 0", rc == 0)
        check("Form A: announces delegate form", "Form A" in out)
        check("Form A: PASS line printed", "[PASS] gate" in out)


def test_form_a_fail():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, ".gates.toml", '[prep_pr]\ngate = "sh -c \'exit 3\'"\n')
        rc, out = run_runner(root)
        check("Form A: fail -> exit 1", rc == 1)
        check("Form A: FAIL line printed", "[FAIL] gate" in out)


# --- Form B (enumerate) -----------------------------------------------------

def test_form_b_order_and_pass():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        marker = os.path.join(root, "order.txt")
        cfg = f"""\
[prep_pr]
  [[prep_pr.steps]]
  name = "first"
  run = "echo 1 >> {marker}"
  [[prep_pr.steps]]
  name = "second"
  run = "echo 2 >> {marker}"
"""
        write(root, ".gates.toml", cfg)
        rc, out = run_runner(root)
        check("Form B: all pass -> exit 0", rc == 0)
        check("Form B: announces enumerate form", "Form B" in out)
        order = []
        if os.path.exists(marker):
            with open(marker, encoding="utf-8") as f:
                order = f.read().split()
        check("Form B: steps run IN ORDER", order == ["1", "2"])
        check("Form B: per-step PASS lines", "[PASS] first" in out and "[PASS] second" in out)


def test_form_b_hard_fail_stops():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        marker = os.path.join(root, "ran.txt")
        cfg = f"""\
[prep_pr]
  [[prep_pr.steps]]
  name = "boom"
  run = "sh -c 'exit 1'"
  [[prep_pr.steps]]
  name = "after"
  run = "echo reached >> {marker}"
"""
        write(root, ".gates.toml", cfg)
        rc, out = run_runner(root)
        check("Form B: required fail -> exit 1", rc == 1)
        check("Form B: FAIL line for failing step", "[FAIL] boom" in out)
        check("Form B: stops at first hard fail (later step NOT run)",
              not os.path.exists(marker))


def test_step_lines_carry_duration():
    """#400: the PASS and FAIL lines report per-step wall time after the exit
    code, e.g. `[PASS] fast (exit 0, 0.0s)`. Reporting only; the verdict and
    exit code are unchanged."""
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        cfg = """\
[prep_pr]
  [[prep_pr.steps]]
  name = "fast"
  run = "true"
  [[prep_pr.steps]]
  name = "slow-soft"
  run = "sleep 0.3; exit 4"
  required = false
"""
        write(root, ".gates.toml", cfg)
        rc, out = run_runner(root)
        check("#400: soft fail still exits 0", rc == 0)
        check("#400: PASS line carries a duration",
              re.search(r"^\[PASS\] fast \(exit 0, \d+\.\ds\)$", out, re.M) is not None)
        m = re.search(r"^\[FAIL\] slow-soft \(exit 4, (\d+\.\d)s\)$", out, re.M)
        check("#400: FAIL line carries a duration", m is not None)
        check("#400: duration reflects real wall time (>= 0.3s)",
              m is not None and float(m.group(1)) >= 0.3)


def test_launch_failure_carries_duration():
    """#400 (Copilot on #451): a step that cannot LAUNCH (OSError, e.g. a vanished cwd)
    reports its wall time too, so every FAIL line has the same shape. Driven through
    _run_command directly: every caller passes the repo root, so no config reaches it."""
    import importlib.util, io, contextlib
    spec = importlib.util.spec_from_file_location("gate_runner_mod", RUNNER)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        ok = mod._run_command("ghost", "true", "/nonexistent/gate-runner-cwd")
    out = buf.getvalue()
    check("#400: a launch failure is a FAIL (returns False)", ok is False)
    check("#400: the launch-failure FAIL line carries a duration",
          re.search(r"^\[FAIL\] ghost: could not launch \(.*\), \d+\.\ds$", out, re.M) is not None)


def test_form_b_soft_fail_continues():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        marker = os.path.join(root, "ran.txt")
        cfg = f"""\
[prep_pr]
  [[prep_pr.steps]]
  name = "soft"
  run = "sh -c 'exit 1'"
  required = false
  [[prep_pr.steps]]
  name = "after"
  run = "echo reached >> {marker}"
"""
        write(root, ".gates.toml", cfg)
        rc, out = run_runner(root)
        check("Form B: required=false soft fail -> exit 0", rc == 0)
        check("Form B: later step still runs after soft fail",
              os.path.exists(marker))
        check("Form B: soft failure announced", "soft failure" in out)


def test_form_b_skip_if_absent_skips():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        cfg = """\
[prep_pr]
  [[prep_pr.steps]]
  name = "needs-tool"
  run = "sh -c 'exit 1'"
  skip_if_absent = "definitely-not-a-real-binary-xyz"
"""
        write(root, ".gates.toml", cfg)
        rc, out = run_runner(root)
        check("skip_if_absent (missing) -> SKIP, exit 0", rc == 0)
        check("skip_if_absent (missing): SKIP line", "[SKIP] needs-tool" in out)
        check("skip_if_absent (missing): run NOT executed (no FAIL)",
              "[FAIL] needs-tool" not in out)


def test_form_b_skip_if_absent_present_runs():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        # `sh` is certainly present -> step must RUN (and pass here).
        cfg = """\
[prep_pr]
  [[prep_pr.steps]]
  name = "have-sh"
  run = "sh -c 'exit 0'"
  skip_if_absent = "sh"
"""
        write(root, ".gates.toml", cfg)
        rc, out = run_runner(root)
        check("skip_if_absent (present) -> step RUNS, exit 0", rc == 0)
        check("skip_if_absent (present): PASS not SKIP",
              "[PASS] have-sh" in out and "[SKIP] have-sh" not in out)


def test_form_b_skip_if_no_match_skips():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        cfg = """\
[prep_pr]
  [[prep_pr.steps]]
  name = "ui-lint"
  run = "sh -c 'exit 1'"
  skip_if = "web/**/*.ts"
"""
        write(root, ".gates.toml", cfg)
        rc, out = run_runner(root)
        check("skip_if (no match) -> SKIP, exit 0", rc == 0)
        check("skip_if (no match): SKIP line w/ reason",
              "[SKIP] ui-lint" in out and "no files match" in out)


def test_form_b_skip_if_match_runs():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "web/app.ts", "// ui\n")
        cfg = """\
[prep_pr]
  [[prep_pr.steps]]
  name = "ui-lint"
  run = "sh -c 'exit 0'"
  skip_if = "web/**/*.ts"
"""
        write(root, ".gates.toml", cfg)
        rc, out = run_runner(root)
        check("skip_if (match) -> step RUNS, exit 0", rc == 0)
        check("skip_if (match): PASS not SKIP",
              "[PASS] ui-lint" in out and "[SKIP] ui-lint" not in out)


def test_mutually_exclusive():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        cfg = """\
[prep_pr]
gate = "true"
  [[prep_pr.steps]]
  name = "x"
  run = "true"
"""
        write(root, ".gates.toml", cfg)
        rc, out = run_runner(root)
        check("gate + steps both set -> exit 2 (config error)", rc == 2)
        check("mutually exclusive: explained", "mutually exclusive" in out)


def test_broken_toml():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, ".gates.toml", "this is = = not valid toml [[[\n")
        rc, out = run_runner(root)
        check("present-but-broken .gates.toml -> exit 2", rc == 2)
        check("broken toml: parse error surfaced", "could not parse" in out)


def test_malformed_prep_pr_fails_closed():
    # A present config whose [prep_pr] is missing or mis-typed must FAIL CLOSED
    # (exit 2), not silently skip every gate (a typo must never disable gating).
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, ".gates.toml", "prep_pr = \"oops\"\n")  # not a table
        rc, out = run_runner(root)
        check("prep_pr not a table -> exit 2 (fail closed)", rc == 2)
        check("prep_pr not a table: failing-closed message", "failing closed" in out)
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, ".gates.toml", "[merge_pr]\ncoverage_advisory = false\n")  # no [prep_pr]
        rc, _ = run_runner(root)
        check("config present but no [prep_pr] -> exit 2 (fail closed)", rc == 2)
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, ".gates.toml", "[prep_pr]\nsteps = [{ name = \"x\", run = 5 }]\n")
        rc, out = run_runner(root)
        check("step `run` not a string -> exit 2 (fail closed)", rc == 2)
        check("invalid run: explained", "invalid `run`" in out)
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, ".gates.toml",
              "[prep_pr]\nsteps = [{ name = \"x\", run = \"true\", required = \"yes\" }]\n")
        rc, out = run_runner(root)
        check("step `required` not a bool -> exit 2 (fail closed)", rc == 2)
        check("invalid required: explained", "invalid `required`" in out)


# --- Fallback chain ---------------------------------------------------------

def test_fallback_umbrella_makefile():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        # A real `make gate` target; requires `make` on PATH.
        if subprocess.run(["sh", "-c", "command -v make"],
                          capture_output=True).returncode != 0:
            check("fallback L1 make gate (make unavailable -- skipped)", True)
            return
        write(root, "Makefile", "gate:\n\t@echo made-gate\n")
        rc, out = run_runner(root)
        check("fallback L1: `make gate` target -> exit 0", rc == 0)
        check("fallback L1: announces layer 1 umbrella (make gate)",
              "layer 1" in out and "make gate" in out)


def test_fallback_umbrella_prepush_script():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        # No make gate target; ensure `make` cannot find one either.
        write(root, "scripts/pre-push-gate.sh",
              "#!/bin/sh\necho umbrella-ran\nexit 0\n", executable=True)
        rc, out = run_runner(root, drop_tools=("make",))
        check("fallback L1: scripts/pre-push-gate.sh -> exit 0", rc == 0)
        check("fallback L1: announces pre-push-gate.sh",
              "pre-push-gate.sh" in out)


def test_fallback_claude_md():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        marker = os.path.join(root, "claude-ran.txt")
        claude = f"""\
# Repo

## Gates (run locally; CI enforces them)

```sh
echo gate-a >> {marker}
echo gate-b >> {marker}
```

## Versioning
nothing
"""
        write(root, "CLAUDE.md", claude)
        # No umbrella: drop make, no pre-push-gate.sh.
        rc, out = run_runner(root, drop_tools=("make",))
        check("fallback L2: CLAUDE.md ## Gates -> exit 0", rc == 0)
        check("fallback L2: announces layer 2", "layer 2" in out)
        ran = []
        if os.path.exists(marker):
            with open(marker, encoding="utf-8") as f:
                ran = f.read().split()
        check("fallback L2: gate commands run in order",
              ran == ["gate-a", "gate-b"])


def test_fallback_claude_md_hard_fail():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        claude = """\
## Gates

```sh
sh -c 'exit 5'
echo should-not-run
```
"""
        write(root, "CLAUDE.md", claude)
        rc, out = run_runner(root, drop_tools=("make",))
        check("fallback L2: a failing gate command -> exit 1", rc == 1)


def test_fallback_basics_python():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        # No .gates.toml, no make gate, no pre-push-gate.sh, no CLAUDE.md
        # Gates block -> manifest inference from test-*.py harnesses.
        write(root, "test-thing.py", "import sys; sys.exit(0)\n")
        rc, out = run_runner(root, drop_tools=("make",))
        check("fallback L3: python3 test-*.py basics -> exit 0", rc == 0)
        check("fallback L3: announces layer 3 basics", "layer 3" in out)
        check("fallback L3: WARNs about inference", "inferring" in out)


def test_fallback_basics_python_fail():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "test-thing.py", "import sys; sys.exit(2)\n")
        rc, out = run_runner(root, drop_tools=("make",))
        check("fallback L3: a failing harness -> exit 1", rc == 1)


def test_fallback_terminal_fail_open():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        # Nothing detectable at all -> warn and PROCEED (exit 0).
        rc, out = run_runner(root, drop_tools=("make",))
        check("fallback L4: nothing detectable -> exit 0 (fail-open)", rc == 0)
        check("fallback L4: warns 'proceeding without gates'",
              "proceeding without gates" in out)


def test_no_claude_gates_block_falls_through():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        # CLAUDE.md WITHOUT a ## Gates block -> layer 2 must NOT match;
        # with no manifest, lands on the terminal fail-open.
        write(root, "CLAUDE.md", "# Repo\n\nNo gates here.\n")
        rc, out = run_runner(root, drop_tools=("make",))
        check("CLAUDE.md w/o ## Gates -> terminal fail-open exit 0", rc == 0)
        check("CLAUDE.md w/o ## Gates: not treated as layer 2",
              "layer 2" not in out)


# --- Part A: gate receipt (--receipt) --------------------------------------

def _load_receipt(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_receipt_schema_valid_on_pass():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "ok.sh", "#!/bin/sh\nexit 0\n", executable=True)
        write(root, ".gates.toml", '[prep_pr]\ngate = "sh ok.sh"\n')
        git_commit(root)
        head = git_head(root)
        rpath = os.path.join(root, "receipt.json")
        rc, out = run_runner(root, args=("--receipt", rpath))
        check("receipt(pass): gate exit unchanged (0)", rc == 0)
        check("receipt(pass): file written", os.path.isfile(rpath))
        r = _load_receipt(rpath)
        check("receipt(pass): schema field", r.get("schema") == "gate-receipt/v1")
        check("receipt(pass): commit_sha == HEAD (full 40)",
              r.get("commit_sha") == head and len(head) == 40)
        check("receipt(pass): tree_sha present 40-hex",
              isinstance(r.get("tree_sha"), str) and len(r["tree_sha"]) == 40)
        check("receipt(pass): worktree is repo root abs path",
              os.path.realpath(r.get("worktree", "")) == os.path.realpath(root))
        check("receipt(pass): result == pass", r.get("result") == "pass")
        check("receipt(pass): producer == gate-runner",
              r.get("producer") == "gate-runner")
        check("receipt(pass): validates against gate-receipt/v1 schema",
              orchestrate_schemas.validate("gate-receipt/v1", r) == [])


def test_receipt_result_fail_still_written():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        cfg = """\
[prep_pr]
  [[prep_pr.steps]]
  name = "boom"
  run = "sh -c 'exit 1'"
"""
        write(root, ".gates.toml", cfg)
        git_commit(root)
        head = git_head(root)
        rpath = os.path.join(root, "receipt.json")
        rc, out = run_runner(root, args=("--receipt", rpath))
        check("receipt(fail): gate exit unchanged (1)", rc == 1)
        check("receipt(fail): file STILL written on failure", os.path.isfile(rpath))
        r = _load_receipt(rpath)
        check("receipt(fail): result == fail", r.get("result") == "fail")
        check("receipt(fail): real commit_sha (== HEAD)",
              r.get("commit_sha") == head)
        check("receipt(fail): validates against schema",
              orchestrate_schemas.validate("gate-receipt/v1", r) == [])


def test_receipt_dirty_tree_never_passes():
    # #481 R7: a gate that ran on a DIRTY worktree must not leave a pass receipt
    # (the receipt binds HEAD^{tree}, which the gate did not test).
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "ok.sh", "#!/bin/sh\nexit 0\n", executable=True)
        write(root, ".gates.toml", '[prep_pr]\ngate = "sh ok.sh"\n')
        git_commit(root)
        rpath = os.path.join(root, "receipt.json")
        # Clean first: a real pass receipt exists (and is untracked: must not
        # count as dirt itself).
        rc, out = run_runner(root, args=("--receipt", rpath))
        check("receipt(dirty): clean run passes", rc == 0)
        check("receipt(dirty): clean run, untracked receipt ignored -> pass",
              _load_receipt(rpath).get("result") == "pass")
        # Re-run with the previous (untracked) receipt present: still a pass.
        rc, out = run_runner(root, args=("--receipt", rpath))
        check("receipt(dirty): re-run beside its own old receipt -> pass",
              _load_receipt(rpath).get("result") == "pass")
        # Now dirty the tree: untracked file.
        write(root, "stray.txt", "x\n")
        rc, out = run_runner(root, args=("--receipt", rpath))
        check("receipt(dirty): gate exit unchanged (0)", rc == 0)
        r = _load_receipt(rpath) if os.path.isfile(rpath) else {}
        check("receipt(dirty): stale pass receipt does not survive",
              r.get("result") != "pass")
        check("receipt(dirty): result == fail with a stated reason",
              r.get("result") == "fail" and bool(r.get("reason")))
        check("receipt(dirty): still schema-valid",
              orchestrate_schemas.validate("gate-receipt/v1", r) == [])
        os.unlink(os.path.join(root, "stray.txt"))
        # Tracked modification.
        write(root, "ok.sh", "#!/bin/sh\nexit 0\n# edit\n", executable=True)
        rc, out = run_runner(root, args=("--receipt", rpath))
        r = _load_receipt(rpath)
        check("receipt(dirty): tracked edit -> not pass", r.get("result") == "fail")


def test_receipt_path_in_untracked_dir_not_dirty():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "ok.sh", "#!/bin/sh\nexit 0\n", executable=True)
        write(root, ".gates.toml", '[prep_pr]\ngate = "sh ok.sh"\n')
        git_commit(root)
        rpath = os.path.join(root, "out", "receipt.json")
        os.makedirs(os.path.dirname(rpath))
        rc, out = run_runner(root, args=("--receipt", rpath))
        check("receipt(untracked dir): rc 0", rc == 0)
        check("receipt(untracked dir): receipt alone is not dirt -> pass",
              os.path.isfile(rpath) and _load_receipt(rpath).get("result") == "pass")
        rc, out = run_runner(root, args=("--receipt", rpath))
        check("receipt(untracked dir): re-run beside its old receipt -> pass",
              _load_receipt(rpath).get("result") == "pass")
        write(root, "out/other.txt", "x\n")
        rc, out = run_runner(root, args=("--receipt", rpath))
        check("receipt(untracked dir): sibling untracked file IS dirt",
              _load_receipt(rpath).get("result") == "fail")


def test_receipt_status_error_is_dirty():
    # Doubt = dirty: if `status` cannot run (corrupt index) there is no pass.
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "ok.sh", "#!/bin/sh\nexit 0\n", executable=True)
        write(root, ".gates.toml", '[prep_pr]\ngate = "sh ok.sh"\n')
        git_commit(root)
        rpath = os.path.join(root, "receipt.json")
        with open(os.path.join(root, ".git", "index"), "wb") as f:
            f.write(b"garbage-not-an-index")
        rc, out = run_runner(root, args=("--receipt", rpath))
        check("receipt(status error): gate exit unchanged (0)", rc == 0)
        check("receipt(status error): receipt written (HEAD resolves)",
              os.path.isfile(rpath))
        r = _load_receipt(rpath) if os.path.isfile(rpath) else {}
        check("receipt(status error): never a pass", r.get("result") == "fail")


def _load_runner_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gate_runner_mod", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_receipt_review_round_498():
    # PR #498 review (CodeRabbit): a PLAIN gate failure on a clean tree (no
    # `reason`) must also unlink an older pass, so if writing the fail receipt
    # then fails, the stale pass cannot survive. Exercised directly: the write
    # is stubbed to a no-op, which models "schema/write error after unlink".
    mod = _load_runner_module()
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "a.txt", "x\n")
        git_commit(root)
        rpath = os.path.join(root, ".git", "r.json")
        with open(rpath, "w") as f:
            json.dump({"result": "pass"}, f)
        pre = mod._snapshot(root, rpath)
        mod._atomic_write_json = lambda path, obj: None
        mod._write_receipt(rpath, root, 1, [], pre)
        check("receipt(498): clean-tree gate failure removes an older pass",
              not os.path.exists(rpath))
    # PR #498 review (Copilot): a leftover `<receipt>.tmp.<pid>` from an
    # interrupted atomic write inside the worktree is not dirt.
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "ok.sh", "#!/bin/sh\nexit 0\n", executable=True)
        write(root, ".gates.toml", '[prep_pr]\ngate = "sh ok.sh"\n')
        write(root, ".gitignore", "")
        git_commit(root)
        rpath = os.path.join(root, "receipt.json")
        write(root, "receipt.json.tmp.12345", "{}")
        rc, out = run_runner(root, args=("--receipt", rpath))
        r = _load_receipt(rpath) if os.path.isfile(rpath) else {}
        check("receipt(498): leftover .tmp.<pid> is not dirt (pass)",
              r.get("result") == "pass")
        # ...but a look-alike that is NOT the tmp pattern still counts.
        write(root, "receipt.json.tmp.x", "{}")
        rc, out = run_runner(root, args=("--receipt", rpath))
        r = _load_receipt(rpath) if os.path.isfile(rpath) else {}
        check("receipt(498): a non-pid .tmp look-alike is still dirt (fail)",
              r.get("result") == "fail")
    # Round-2 review: a DIRECTORY named like the tmp file must not hide its
    # contents (git reports it as one `name/` entry; realpath strips the slash).
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "ok.sh", "#!/bin/sh\nexit 0\n", executable=True)
        write(root, ".gates.toml", '[prep_pr]\ngate = "sh ok.sh"\n')
        git_commit(root)
        rpath = os.path.join(root, "receipt.json")
        write(root, "receipt.json.tmp.123/payload.sh", "echo evil\n")
        rc, out = run_runner(root, args=("--receipt", rpath))
        r = _load_receipt(rpath) if os.path.isfile(rpath) else {}
        check("receipt(498): a DIRECTORY named <receipt>.tmp.<n>/ is dirt (fail)",
              r.get("result") == "fail")
    # Round-2 review (M1): the untracked-DIR leg also ignores the tmp leftover.
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "ok.sh", "#!/bin/sh\nexit 0\n", executable=True)
        write(root, ".gates.toml", '[prep_pr]\ngate = "sh ok.sh"\n')
        git_commit(root)
        rpath = os.path.join(root, "out", "receipt.json")
        write(root, "out/receipt.json.tmp.123", "{}")
        rc, out = run_runner(root, args=("--receipt", rpath))
        r = _load_receipt(rpath) if os.path.isfile(rpath) else {}
        check("receipt(498): tmp leftover beside a receipt in an untracked dir (pass)",
              r.get("result") == "pass")


def test_receipt_unresolvable_head_removes_stale():
    # #497: when the pre-run HEAD/tree cannot resolve (git missing, not a repo,
    # no commit), no receipt is written -- and an OLDER pass at the path must
    # not survive either, or a consumer reads it as this run's verdict.
    # (a) direct: the unresolvable-snapshot branch of _write_receipt.
    mod = _load_runner_module()
    with tempfile.TemporaryDirectory() as root:
        rpath = os.path.join(root, "r.json")
        with open(rpath, "w") as f:
            json.dump({"result": "pass"}, f)
        mod._write_receipt(rpath, root, 0, [], (None, None, "git status failed"))
        check("receipt(497 a): unresolvable HEAD removes an older pass",
              not os.path.exists(rpath))
    # (b) end-to-end: git absent from PATH, a pass receipt from an earlier run
    # sits at the path. The gate still runs and its exit code is unchanged.
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "ok.sh", "#!/bin/sh\nexit 0\n", executable=True)
        write(root, ".gates.toml", '[prep_pr]\ngate = "sh ok.sh"\n')
        git_commit(root)
        rpath = os.path.join(root, ".git", "receipt.json")
        rc, out = run_runner(root, args=("--receipt", rpath))
        check("receipt(497 b): seeded a real pass receipt",
              os.path.isfile(rpath) and _load_receipt(rpath).get("result") == "pass")
        rc, out = run_runner(root, drop_tools=("git",), args=("--receipt", rpath))
        check("receipt(497 b): git missing -> gate exit unchanged (0)", rc == 0)
        check("receipt(497 b): git missing -> warns about skipping receipt",
              "skipping gate receipt" in out)
        check("receipt(497 b): git missing -> older pass receipt does not survive",
              not os.path.exists(rpath))
    # (c) the unlink itself FAILS (#510 review): it must warn, say so in the
    # skip message, and never claim the stale pass is gone. os.unlink is forced
    # to raise rather than chmod-ing a dir, which a root uid would ignore.
    import io, contextlib
    mod = _load_runner_module()
    with tempfile.TemporaryDirectory() as root:
        rpath = os.path.join(root, "r.json")
        with open(rpath, "w") as f:
            json.dump({"result": "pass"}, f)
        real_unlink = mod.os.unlink
        def _deny(p):
            raise PermissionError(13, "Permission denied", p)
        buf = io.StringIO()
        mod.os.unlink = _deny
        try:
            with contextlib.redirect_stderr(buf):
                mod._write_receipt(rpath, root, 0, [], (None, None, "git status failed"))
        finally:
            mod.os.unlink = real_unlink
        err = buf.getvalue()
        check("receipt(497 c): failed unlink warns with the path",
              "could not remove older gate receipt" in err and rpath in err)
        check("receipt(497 c): skip message admits the receipt could not be removed",
              "could NOT be removed" in err)
        check("receipt(497 c): failed unlink leaves the file (no false claim)",
              os.path.exists(rpath))
        mod.os.unlink = _deny
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                failed = mod._remove_stale(rpath)
        finally:
            mod.os.unlink = real_unlink
        check("receipt(497 c): _remove_stale returns False when the unlink fails",
              failed is False)
        check("receipt(497 c): _remove_stale returns True when nothing is there",
              mod._remove_stale(os.path.join(root, "nope.json")) is True)


def test_receipt_snapshot_before_run_toctou():
    # #481 review round 1: HEAD/tree/dirtiness are sampled BEFORE the run too.
    # (a) a dirty tracked edit the gate itself discards mid-run must not pass.
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "a.txt", "clean\n")
        write(root, ".gates.toml",
              '[prep_pr]\ngate = "git checkout -- a.txt"\n')
        git_commit(root)
        write(root, "a.txt", "BAD\n")
        rpath = os.path.join(root, "receipt.json")
        rc, out = run_runner(root, args=("--receipt", rpath))
        r = _load_receipt(rpath) if os.path.isfile(rpath) else {}
        check("receipt(toctou a): gate exit unchanged (0)", rc == 0)
        check("receipt(toctou a): dirty-then-reverted is not a pass",
              r.get("result") == "fail")
        check("receipt(toctou a): reason names dirty-before-run",
              "dirty-before-run" in r.get("reason", ""))
    # (b) a commit made mid-run: the tree the gate started on is not the one
    # a post-run read would bind.
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "a.txt", "clean\n")
        step = ("sh -c 'echo n > n.txt && git add n.txt && git -c "
                "user.email=t@example.com -c user.name=t commit -q -m mid'")
        write(root, ".gates.toml", f'[prep_pr]\ngate = "{step}"\n')
        git_commit(root)
        pre_head = git_head(root)
        rpath = os.path.join(root, "receipt.json")
        rc, out = run_runner(root, args=("--receipt", rpath))
        r = _load_receipt(rpath) if os.path.isfile(rpath) else {}
        check("receipt(toctou b): records the PRE-run commit",
              r.get("commit_sha") == pre_head)
        check("receipt(toctou b): gate exit unchanged (0)", rc == 0)
        check("receipt(toctou b): mid-run commit is not a pass",
              r.get("result") == "fail")
        check("receipt(toctou b): reason names tree-changed-during-run",
              "tree-changed-during-run" in r.get("reason", ""))
        check("receipt(toctou b): still schema-valid",
              orchestrate_schemas.validate("gate-receipt/v1", r) == [])
    # (c) clean and unchanged: pass, bound to the PRE-run tree.
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "ok.sh", "#!/bin/sh\nexit 0\n", executable=True)
        write(root, ".gates.toml", '[prep_pr]\ngate = "sh ok.sh"\n')
        git_commit(root)
        pre_tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=root, env=git_env(root),
            capture_output=True, text=True, check=True).stdout.strip()
        rpath = os.path.join(root, "receipt.json")
        rc, out = run_runner(root, args=("--receipt", rpath))
        r = _load_receipt(rpath)
        check("receipt(toctou c): clean unchanged -> pass",
              r.get("result") == "pass")
        check("receipt(toctou c): tree_sha is the pre-run tree",
              r.get("tree_sha") == pre_tree)
        check("receipt(toctou c): commit_sha is the pre-run HEAD",
              r.get("commit_sha") == git_head(root))


    # (d) the gate itself dirties the tree: dirty-after-run, not a pass.
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        write(root, "a.txt", "clean\n")
        write(root, ".gates.toml", '[prep_pr]\ngate = "touch stray.txt"\n')
        git_commit(root)
        rpath = os.path.join(root, "receipt.json")
        rc, out = run_runner(root, args=("--receipt", rpath))
        r = _load_receipt(rpath) if os.path.isfile(rpath) else {}
        check("receipt(toctou d): dirtied-by-gate is not a pass",
              r.get("result") == "fail"
              and "dirty-after-run" in r.get("reason", ""))


def test_receipt_steps_records_match():
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        cfg = """\
[prep_pr]
  [[prep_pr.steps]]
  name = "alpha"
  run = "sh -c 'exit 0'"
  [[prep_pr.steps]]
  name = "beta"
  run = "sh -c 'exit 0'"
  skip_if = "web/**/*.ts"
"""
        write(root, ".gates.toml", cfg)
        git_commit(root)
        rpath = os.path.join(root, "receipt.json")
        rc, out = run_runner(root, args=("--receipt", rpath))
        check("receipt(steps): exit 0", rc == 0)
        r = _load_receipt(rpath)
        steps = r.get("steps", [])
        by_name = {s["name"]: s["result"] for s in steps}
        check("receipt(steps): alpha recorded pass", by_name.get("alpha") == "pass")
        check("receipt(steps): beta recorded skip (no glob match)",
              by_name.get("beta") == "skip")
        check("receipt(steps): granular records validate",
              orchestrate_schemas.validate("gate-receipt/v1", r) == [])


def test_receipt_malformed_form_b_config_error():
    # CR #251 nitpick: exercise --receipt on a MALFORMED Form B step (a config
    # error -> rc=2, distinct from a gate pass/fail). The receipt is STILL written
    # (result=fail, since rc != 0), steps[] holds whatever ran BEFORE the bad step,
    # the malformed step is not recorded, and the receipt stays schema-valid.
    with tempfile.TemporaryDirectory() as root:
        git_init(root)
        cfg = """\
[prep_pr]
  [[prep_pr.steps]]
  name = "ran-first"
  run = "sh -c 'exit 0'"
  [[prep_pr.steps]]
  name = "bad-required"
  run = "sh -c 'exit 0'"
  required = "not-a-bool"
"""
        write(root, ".gates.toml", cfg)
        git_commit(root)
        head = git_head(root)
        rpath = os.path.join(root, "receipt.json")
        rc, out = run_runner(root, args=("--receipt", rpath))
        check("receipt(config-error): gate exits 2 (config error)", rc == 2)
        check("receipt(config-error): receipt STILL written", os.path.isfile(rpath))
        r = _load_receipt(rpath)
        check("receipt(config-error): result == fail (rc != 0)", r.get("result") == "fail")
        check("receipt(config-error): real commit_sha (== HEAD)", r.get("commit_sha") == head)
        by_name = {s["name"]: s["result"] for s in r.get("steps", [])}
        check("receipt(config-error): steps[] holds the step run before the error",
              by_name.get("ran-first") == "pass")
        check("receipt(config-error): the malformed step is NOT recorded",
              "bad-required" not in by_name)
        check("receipt(config-error): still schema-valid",
              orchestrate_schemas.validate("gate-receipt/v1", r) == [])


def test_receipt_non_git_fail_open():
    # A dir that is NOT a git repo: git rev-parse HEAD fails -> no receipt, but
    # the gate's own exit code is UNCHANGED (receipt is a byproduct).
    with tempfile.TemporaryDirectory() as root:
        # deliberately NO git_init
        write(root, "ok.sh", "#!/bin/sh\nexit 0\n", executable=True)
        write(root, ".gates.toml", '[prep_pr]\ngate = "sh ok.sh"\n')
        rpath = os.path.join(root, "receipt.json")
        rc, out = run_runner(root, args=("--receipt", rpath))
        check("receipt(non-git): gate exit unchanged (0)", rc == 0)
        check("receipt(non-git): NO receipt written (fail-open)",
              not os.path.exists(rpath))
        check("receipt(non-git): warns about skipping receipt",
              "receipt" in out.lower())


# --- Part B: pure-oracle memoization (--memoize-dir) -----------------------
# The clean-worktree gate is `git status --porcelain` (untracked counts as
# dirty, #229), so the run-detection marker AND the memoize-dir MUST live OUTSIDE
# the worktree (`ext`) -- an in-repo marker/cache dir would itself show as
# untracked and defeat memoization (which is exactly the safety property).

def test_memoize_pure_step_skipped_second_run():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ext:
        git_init(root)
        marker = os.path.join(ext, "ran.marker")  # outside the worktree
        memo = os.path.join(ext, "memo")
        cfg = f"""\
[prep_pr]
  [[prep_pr.steps]]
  name = "pure-step"
  run = "touch {marker}"
  pure = true
"""
        write(root, ".gates.toml", cfg)
        git_commit(root)
        # Run 1: clean committed tree -> step runs, marker created, cache written.
        rc1, out1 = run_runner(root, args=("--memoize-dir", memo))
        check("memo: run1 exit 0", rc1 == 0)
        check("memo: run1 executed the step (marker created)",
              os.path.exists(marker))
        check("memo: run1 did NOT report a cache hit", "[MEMO]" not in out1)
        os.remove(marker)
        # Run 2: clean tree, same committed tree -> cache hit -> SKIP.
        rc2, out2 = run_runner(root, args=("--memoize-dir", memo))
        check("memo: run2 exit 0", rc2 == 0)
        check("memo: run2 reports [MEMO] cached pass", "[MEMO] pure-step" in out2)
        check("memo: run2 did NOT re-run the step (marker NOT recreated)",
              not os.path.exists(marker))


def test_memoize_dirty_worktree_reruns():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ext:
        git_init(root)
        write(root, "tracked.txt", "v1\n")
        marker = os.path.join(ext, "ran.marker")
        memo = os.path.join(ext, "memo")
        cfg = f"""\
[prep_pr]
  [[prep_pr.steps]]
  name = "pure-step"
  run = "touch {marker}"
  pure = true
"""
        write(root, ".gates.toml", cfg)
        git_commit(root)
        os.makedirs(memo, exist_ok=True)
        # Dirty the TRACKED file -> porcelain non-empty -> not memoizable.
        with open(os.path.join(root, "tracked.txt"), "a", encoding="utf-8") as f:
            f.write("dirty\n")
        rc, out = run_runner(root, args=("--memoize-dir", memo))
        check("memo(dirty): exit 0", rc == 0)
        check("memo(dirty): step RAN (marker created)", os.path.exists(marker))
        check("memo(dirty): no [MEMO] line", "[MEMO]" not in out)
        check("memo(dirty): nothing cached", os.listdir(memo) == [])


def test_memoize_untracked_input_reruns():
    # THE #229 false-pass guard: a `pure` step that FAILS when an untracked input
    # file is present must RE-RUN (not memo-skip) once that file appears, because
    # `git status --porcelain` flags the untracked file as dirty. Untracked inputs
    # (the normal state of in-progress work) can never produce a memo false-pass.
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ext:
        git_init(root)
        memo = os.path.join(ext, "memo")
        cfg = """\
[prep_pr]
  [[prep_pr.steps]]
  name = "glob-lint"
  run = "sh -c '! test -e bad.attack'"
  pure = true
"""
        write(root, ".gates.toml", cfg)
        git_commit(root)
        # Run 1: no bad.attack -> passes, cached.
        rc1, out1 = run_runner(root, args=("--memoize-dir", memo))
        check("memo(attack): run1 passes + caches", rc1 == 0 and "[MEMO]" not in out1)
        # Introduce an UNTRACKED input that would fail the step.
        write(root, "bad.attack", "x\n")
        # Run 2: porcelain sees the untracked file -> NOT memoizable -> re-run -> FAIL.
        rc2, out2 = run_runner(root, args=("--memoize-dir", memo))
        check("memo(attack): untracked input -> step RE-RAN (not memo-skipped)",
              "[MEMO]" not in out2)
        check("memo(attack): the real failure surfaces -> exit 1 (no false-pass)",
              rc2 == 1)


def test_memoize_impure_step_never_cached():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ext:
        git_init(root)
        marker = os.path.join(ext, "ran.marker")
        memo = os.path.join(ext, "memo")
        # No `pure = true` -> NOT on the allowlist, never memoized.
        cfg = f"""\
[prep_pr]
  [[prep_pr.steps]]
  name = "impure-step"
  run = "touch {marker}"
"""
        write(root, ".gates.toml", cfg)
        git_commit(root)
        os.makedirs(memo, exist_ok=True)
        rc1, out1 = run_runner(root, args=("--memoize-dir", memo))
        check("memo(impure): run1 exit 0", rc1 == 0)
        check("memo(impure): nothing cached (pure absent)", os.listdir(memo) == [])
        os.remove(marker)
        rc2, out2 = run_runner(root, args=("--memoize-dir", memo))
        check("memo(impure): run2 re-runs the step (marker recreated)",
              os.path.exists(marker))
        check("memo(impure): no [MEMO] line", "[MEMO]" not in out2)


def test_memoize_failing_pure_not_cached():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ext:
        git_init(root)
        memo = os.path.join(ext, "memo")
        cfg = """\
[prep_pr]
  [[prep_pr.steps]]
  name = "pure-fail"
  run = "sh -c 'exit 1'"
  pure = true
"""
        write(root, ".gates.toml", cfg)
        git_commit(root)
        os.makedirs(memo, exist_ok=True)
        rc, out = run_runner(root, args=("--memoize-dir", memo))
        check("memo(fail): required pure fail -> exit 1", rc == 1)
        check("memo(fail): a FAILING step is NOT cached (memoize pass-only)",
              os.listdir(memo) == [])


def test_memoize_off_by_default():
    # No --memoize-dir -> zero behavior change (step always runs).
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ext:
        git_init(root)
        marker = os.path.join(ext, "ran.marker")
        cfg = f"""\
[prep_pr]
  [[prep_pr.steps]]
  name = "pure-step"
  run = "touch {marker}"
  pure = true
"""
        write(root, ".gates.toml", cfg)
        git_commit(root)
        rc1, _ = run_runner(root)
        os.remove(marker)
        rc2, out2 = run_runner(root)
        check("memo(off): both runs execute the step", os.path.exists(marker))
        check("memo(off): no [MEMO] line without --memoize-dir",
              "[MEMO]" not in out2 and rc1 == 0 and rc2 == 0)


# --- Part C: opt-in parallel Form B (`jobs`, `exclusive`; #501) -------------
#
# Every instrumentation file (barriers, timestamps, pid files) lives in a
# SEPARATE temp dir, never the worktree, so a receipt can still pass.

_EMIT = """\
import os, sys, time
tag, me, other = sys.argv[1], sys.argv[2], sys.argv[3]
open(me, "w").close()
end = time.time() + 10
while not os.path.exists(other) and time.time() < end:
    time.sleep(0.005)
for i in range(1000):
    (sys.stdout if i % 2 else sys.stderr).write(f"{tag}-line {i}\\n")
    sys.stdout.flush(); sys.stderr.flush()
    if i % 50 == 0:
        time.sleep(0.002)
"""

_SPAN = """\
import sys, time
path, dur = sys.argv[1], float(sys.argv[2])
t0 = time.time(); time.sleep(dur); t1 = time.time()
open(path, "w").write(f"{t0} {t1}\\n")
"""


def _steps_cfg(steps, jobs=None):
    """Build a .gates.toml from (name, run, extra-toml) tuples."""
    head = "[prep_pr]\n" + (f"jobs = {jobs}\n" if jobs is not None else "")
    body = ""
    for name, run, extra in steps:
        body += (f"  [[prep_pr.steps]]\n  name = \"{name}\"\n"
                 f"  run = '''{run}'''\n" + (f"  {extra}\n" if extra else ""))
    return head + body


def _gone(pid, *, group):
    """True once the pid (or process group) no longer exists, waiting up to 3s
    for the kernel/launchd to finish reaping."""
    end = time.time() + 3
    while time.time() < end:
        try:
            (os.killpg if group else os.kill)(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def _wait_file(path, timeout=10):
    end = time.time() + timeout
    while not os.path.exists(path) and time.time() < end:
        time.sleep(0.01)
    return os.path.exists(path)


def _read_pid(path):
    return int(open(path).read().strip()) if _wait_file(path, 5) else None


def _norm(out):
    return re.sub(r"\d+\.\ds\)", "N.Ns)", out)


def test_parallel_serial_byte_identical():
    """jobs absent, jobs = 1, and --jobs 1 overriding jobs = 4 all take the
    serial path: identical output (durations normalized) and exit codes."""
    steps = [("a", "echo out-a; echo err-a >&2", ""),
             ("b", "exit 3", "required = false"),
             ("c", "echo out-c", "")]
    outs = []
    for jobs, args in ((None, ()), (1, ()), (4, ("--jobs", "1"))):
        with tempfile.TemporaryDirectory() as root:
            git_init(root)
            write(root, ".gates.toml", _steps_cfg(steps, jobs))
            rc, out = run_runner(root, args=args)
            outs.append((rc, _norm(out).replace(root, "<root>")))
    check("#501: jobs absent / jobs=1 / --jobs 1 give identical serial output",
          outs[0] == outs[1] == outs[2])
    check("#501: serial path never announces jobs=", "jobs=" not in outs[0][1])


def test_parallel_validation():
    """Bad `jobs` (toml or CLI) or a non-bool `exclusive` exits 2, and the
    parallel path launches NOTHING on a bad later step."""
    with tempfile.TemporaryDirectory() as aux:
        marker = os.path.join(aux, "ran")
        for bad in ("0", "-1", "true", '"4"', "1.5"):
            with tempfile.TemporaryDirectory() as root:
                git_init(root)
                write(root, ".gates.toml", _steps_cfg(
                    [("a", f"touch {marker}", "")], jobs=bad))
                rc, _ = run_runner(root)
                check(f"#501: [prep_pr] jobs = {bad} -> exit 2", rc == 2)
        for bad in (("--jobs", "0"), ("--jobs", "abc"), ("--jobs=-2",), ("--jobs",)):
            with tempfile.TemporaryDirectory() as root:
                git_init(root)
                write(root, ".gates.toml", _steps_cfg([("a", "true", "")]))
                rc, _ = run_runner(root, args=bad)
                check(f"#501: {' '.join(bad)} -> exit 2", rc == 2)
        for jobs in (4, None):   # parallel first: serial validates incrementally
            with tempfile.TemporaryDirectory() as root:
                git_init(root)
                write(root, ".gates.toml", _steps_cfg(
                    [("a", f"touch {marker}", ""),
                     ("b", "true", "exclusive = 1")], jobs=jobs))
                rc, out = run_runner(root)
                check(f"#501: non-bool exclusive -> exit 2 (jobs={jobs})", rc == 2)
                if jobs == 4:
                    check("#501: parallel path launched nothing before "
                          "rejecting the config", not os.path.exists(marker))


def test_parallel_output_contiguous():
    """Two steps run CONCURRENTLY (each waits for the other to start), each
    writing 1000 lines alternating stdout/stderr. Each block prints whole, in
    declaration order, followed by its own verdict line."""
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as aux:
        git_init(root)
        emit = write(aux, "emit.py", _EMIT)
        sa, sb = os.path.join(aux, "a.started"), os.path.join(aux, "b.started")
        write(root, ".gates.toml", _steps_cfg(
            [("A", f"python3 {emit} A {sa} {sb}", ""),
             ("B", f"python3 {emit} B {sb} {sa}", "")], jobs=2))
        rc, out = run_runner(root)
        lines = out.splitlines()
        tags = [ln[0] for ln in lines if re.match(r"^[AB]-line \d+$", ln)]
        check("#501: interleave: rc 0", rc == 0)
        check("#501: interleave: all 2000 lines captured", len(tags) == 2000)
        check("#501: interleave: each step's output is one contiguous block, "
              "in declaration order", tags == ["A"] * 1000 + ["B"] * 1000)
        ia = max(i for i, ln in enumerate(lines) if ln.startswith("A-line"))
        ib = max(i for i, ln in enumerate(lines) if ln.startswith("B-line"))
        check("#501: interleave: each block is followed by its own verdict",
              lines[ia + 1].startswith("[PASS] A ")
              and lines[ib + 1].startswith("[PASS] B "))


def test_parallel_declaration_order():
    """A later fast step that FINISHES first still prints after the earlier
    slow step (the slow step does not end until the fast one has ended)."""
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as aux:
        git_init(root)
        done = os.path.join(aux, "fast.done")
        write(root, ".gates.toml", _steps_cfg(
            [("slow", f"while [ ! -e {done} ]; do sleep 0.02; done; echo SLOW-OUT", ""),
             ("fast", f"echo FAST-OUT; touch {done}", "")], jobs=2))
        rc, out = run_runner(root)
        pos = [out.find(s) for s in ("SLOW-OUT", "[PASS] slow", "FAST-OUT", "[PASS] fast")]
        check("#501: order: rc 0 (fast finished first, else slow would hang)", rc == 0)
        check("#501: order: slow block + verdict print before the fast block",
              -1 not in pos and pos == sorted(pos))


def test_parallel_exclusive_overlaps_nothing():
    """jobs = 4 over A, B, X(exclusive), C, D: X's run interval overlaps no
    other step's, while the non-exclusive neighbours do overlap (control)."""
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as aux:
        git_init(root)
        span = write(aux, "span.py", _SPAN)
        names = ["A", "B", "X", "C", "D"]
        steps = [(n, f"python3 {span} {os.path.join(aux, n)} 0.4",
                  "exclusive = true" if n == "X" else "") for n in names]
        write(root, ".gates.toml", _steps_cfg(steps, jobs=4))
        rc, out = run_runner(root)
        iv = {}
        for n in names:
            p = os.path.join(aux, n)
            if os.path.exists(p):
                iv[n] = tuple(float(x) for x in open(p).read().split())

        def overlap(a, b):
            return iv[a][0] < iv[b][1] and iv[b][0] < iv[a][1]
        check("#501: exclusive: rc 0 and every step ran", rc == 0 and len(iv) == 5)
        check("#501: exclusive: X overlaps no other step",
              len(iv) == 5 and not any(overlap("X", n) for n in "ABCD"))
        check("#501: exclusive: control - A/B and C/D did run concurrently",
              len(iv) == 5 and overlap("A", "B") and overlap("C", "D"))


def test_parallel_fail_fast_kills_groups():
    """A required failure while a `sleep 30` sibling runs: exit 1 well inside
    the sleep, the sibling's whole process group (incl. a backgrounded
    descendant) is gone, the failing output prints in full, nothing later
    launches, and the receipt records the cancelled sibling as a failure."""
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as aux:
        git_init(root)
        pg, desc, later = (os.path.join(aux, n) for n in ("pgid", "desc", "later"))
        sib = f"echo $$ > {pg}; sleep 30 & echo $! > {desc}; sleep 30"
        boom = (f"while [ ! -s {desc} ]; do sleep 0.02; done; "
                "i=0; while [ $i -lt 300 ]; do echo BOOM-$i; i=$((i+1)); done; exit 7")
        write(root, ".gates.toml", _steps_cfg(
            [("sib", sib, ""), ("boom", boom, ""), ("later", f"touch {later}", "")],
            jobs=2))
        git_commit(root)
        rpath = os.path.join(root, ".git", "receipt.json")
        t0 = time.time()
        rc, out = run_runner(root, args=("--receipt", rpath))
        took = time.time() - t0
        pgid, dpid = _read_pid(pg), _read_pid(desc)
        check("#501: fail-fast: exit 1", rc == 1)
        check(f"#501: fail-fast: returns within the kill grace, not the 30s sleep "
              f"({took:.1f}s)", took < 10)
        check("#501: fail-fast: sibling process group is gone",
              pgid is not None and _gone(pgid, group=True))
        check("#501: fail-fast: backgrounded descendant is gone",
              dpid is not None and _gone(dpid, group=False))
        check("#501: fail-fast: failing step's output printed in full",
              all(f"BOOM-{i}\n" in out for i in range(300)))
        check("#501: fail-fast: HARD failure line names the failing step",
              "gate-runner: HARD failure at 'boom' -- stopping." in out)
        check("#501: fail-fast: nothing launched after the failure",
              not os.path.exists(later))
        r = _load_receipt(rpath) if os.path.isfile(rpath) else {}
        check("#501: fail-fast: receipt result=fail, schema-valid",
              r.get("result") == "fail"
              and orchestrate_schemas.validate("gate-receipt/v1", r) == [])
        check("#501: fail-fast: cancelled sibling recorded as a failure",
              {"name": "sib", "result": "fail", "cancelled": True} in r.get("steps", []))
        for pid in (pgid, dpid):   # never leak a sleeper if the kill was broken
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass


def test_parallel_interrupt_kills_groups():
    for sig in (signal.SIGINT, signal.SIGHUP):
        _interrupt_kills_groups(sig)


def _interrupt_kills_groups(sig):
    """SIGINT (or SIGHUP) to the runner while a step holds: every process group
    is killed and no pass receipt is written."""
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as aux:
        git_init(root)
        pg = os.path.join(aux, "pgid")
        write(root, ".gates.toml", _steps_cfg(
            [("hold", f"echo $$ > {pg}; sleep 30", ""), ("ok", "true", "")], jobs=2))
        git_commit(root)
        rpath = os.path.join(root, ".git", "receipt.json")
        proc = subprocess.Popen([sys.executable, RUNNER, "--receipt", rpath],
                                cwd=root, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        pgid = _read_pid(pg)
        proc.send_signal(sig)
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill(); rc = None
        check(f"#501: {sig.name}: runner exits non-zero promptly", rc not in (None, 0))
        check(f"#501: {sig.name}: the held step's process group is gone",
              pgid is not None and _gone(pgid, group=True))
        r = _load_receipt(rpath) if os.path.isfile(rpath) else {}
        check(f"#501: {sig.name}: no pass receipt", r.get("result") != "pass")
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass


def test_parallel_receipt():
    """jobs = 4, clean tree, all pass: result=pass with steps[] in declaration
    order; and NO receipt exists while a child is still running."""
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as aux:
        git_init(root)
        go, held, f3 = (os.path.join(aux, n) for n in ("go", "held", "f3"))
        write(root, ".gates.toml", _steps_cfg(
            [("slow", f"touch {held}; while [ ! -e {go} ]; do sleep 0.02; done", ""),
             ("f2", "true", ""), ("f3", f"touch {f3}", ""), ("f4", "true", "")],
            jobs=4))
        git_commit(root)
        rpath = os.path.join(root, ".git", "receipt.json")
        proc = subprocess.Popen([sys.executable, RUNNER, "--receipt", rpath],
                                cwd=root, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        ready = _wait_file(held) and _wait_file(f3)
        time.sleep(0.3)   # let the fast siblings be reaped
        early = os.path.exists(rpath)
        open(go, "w").close()
        try:
            rc = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill(); rc = None
        check("#501: receipt: no receipt while a child still runs",
              ready and not early)
        r = _load_receipt(rpath) if os.path.isfile(rpath) else {}
        check("#501: receipt: jobs=4 all-pass clean tree -> result=pass",
              rc == 0 and r.get("result") == "pass")
        check("#501: receipt: steps[] in declaration order",
              [s.get("name") for s in r.get("steps", [])] == ["slow", "f2", "f3", "f4"])


def test_parallel_double_interrupt_term_ignoring():
    """A step that IGNORES SIGTERM, then SIGINT and a second SIGINT inside the
    kill grace: cleanup is not cut short, the SIGKILL leg reaps the group, and
    no `gate-runner-*` capture dir leaks."""
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as aux:
        git_init(root)
        pg, tmp = os.path.join(aux, "pgid"), os.path.join(aux, "tmp")
        os.makedirs(tmp)
        write(root, ".gates.toml", _steps_cfg(
            [("deaf", f"trap '' TERM; echo $$ > {pg}; sleep 60", ""),
             ("ok", "true", "")], jobs=2))
        proc = subprocess.Popen([sys.executable, RUNNER], cwd=root,
                                env=dict(os.environ, TMPDIR=tmp),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        pgid = _read_pid(pg)
        proc.send_signal(signal.SIGINT); time.sleep(1)
        proc.send_signal(signal.SIGINT)
        try:
            _, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill(); err = b""; proc.wait()
        check("#501: double SIGINT: runner exits 130, no traceback",
              proc.returncode == 130 and b"Traceback" not in err)
        check("#501: double SIGINT: the TERM-ignoring group is gone (SIGKILL leg)",
              pgid is not None and _gone(pgid, group=True))
        check("#501: double SIGINT: no gate-runner-* temp dir leaked",
              not [n for n in os.listdir(tmp) if n.startswith("gate-runner-")])
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass


def test_parallel_soft_skip_memo():
    """The parallel path honors `required = false`, both skip predicates, and
    pure-step memoization (lookup, and a write on PASS only)."""
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as aux:
        git_init(root)
        later, sk1, sk2, pure = (os.path.join(aux, n)
                                 for n in ("later", "sk1", "sk2", "pure"))
        memo = os.path.join(aux, "memo")
        write(root, ".gates.toml", _steps_cfg(
            [("soft", "exit 3", "required = false\n  pure = true"),
             ("absent", f"touch {sk1}", 'skip_if_absent = "no-such-tool-501"'),
             ("nomatch", f"touch {sk2}", 'skip_if = "no-such-dir-501/**"'),
             ("pure", f"touch {pure}", "pure = true"),
             ("later", f"touch {later}", "")], jobs=2))
        git_commit(root)
        rc, out = run_runner(root, args=("--memoize-dir", memo))
        check("#501: soft: rc 0 and later steps still run",
              rc == 0 and os.path.exists(later))
        check("#501: soft: [WARN] soft failure line",
              "[WARN] soft: soft failure (required=false)" in out)
        check("#501: skip: skip_if_absent and skip_if skip in parallel mode",
              "[SKIP] absent:" in out and "[SKIP] nomatch:" in out
              and not os.path.exists(sk1) and not os.path.exists(sk2))
        check("#501: memo: exactly one entry written (PASS only, not the soft fail)",
              os.path.isdir(memo) and len(os.listdir(memo)) == 1)
        os.remove(pure)
        rc2, out2 = run_runner(root, args=("--memoize-dir", memo))
        check("#501: memo: second run is a [MEMO] hit and does not re-run",
              rc2 == 0 and "[MEMO] pure:" in out2 and not os.path.exists(pure))


class _FakeProc:
    """Stands in for Popen: poll() returns None `holds` times, then `code`."""
    def __init__(self, code, holds=0):
        self.pid, self.code, self.holds = 999999, code, holds

    def poll(self):
        if self.holds:
            self.holds -= 1; return None
        return self.code

    def wait(self):
        return self.code


def _inproc_parallel(steps, popen, jobs=2, patch=None):
    """Run run_form_b_parallel in-process with Popen replaced. Returns
    (rc, records, output)."""
    import importlib.util, io, contextlib, types
    spec = importlib.util.spec_from_file_location("gate_runner_par", RUNNER)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mod.subprocess = types.SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL,
                                           STDOUT=subprocess.STDOUT)
    for k, v in (patch or {}).items():
        setattr(mod, k, v(getattr(mod, k)))
    buf = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(buf), \
            contextlib.redirect_stderr(buf):
        rc, records = mod.run_form_b_parallel(steps, root, "/nonexistent-memo", jobs)
        buf.flush()
    return rc, records, buf.buffer.getvalue().decode()


def test_parallel_launch_error_and_tiebreak():
    def boom(*a, **k):
        raise OSError("simulated launch failure")
    rc, recs, out = _inproc_parallel(
        [{"name": "ghost", "run": "x"}, {"name": "next", "run": "y"}], boom, jobs=1)
    check("#501: launch OSError on a required step is a HARD failure",
          rc == 1 and "HARD failure at 'ghost'" in out
          and recs == [{"name": "ghost", "result": "fail"}])
    rc, recs, out = _inproc_parallel(
        [{"name": "a", "run": "x"}, {"name": "b", "run": "y"}],
        lambda *a, **k: _FakeProc(1))
    check("#501: simultaneous required failures name the earlier-declared step",
          rc == 1 and "HARD failure at 'a'" in out)


def test_parallel_head_checked_once():
    """A head blocked on capacity for many polls runs its skip predicate and
    memo lookup exactly once."""
    calls = []

    def count(fn):
        def wrapped(step, *a):
            calls.append((fn.__name__, step if isinstance(step, str) else step["name"]))
            return fn(step, *a) if fn.__name__ == "_skip_reason" else None
        return wrapped
    steps = [{"name": n, "run": "x", "pure": True} for n in ("h1", "h2", "blocked")]
    rc, _, _ = _inproc_parallel(steps, lambda *a, **k: _FakeProc(0, holds=10),
                                patch={"_skip_reason": count, "_memoizable_tree": count})
    skips = [c for c in calls if c == ("_skip_reason", "blocked")]
    trees = [c for c in calls if c[0] == "_memoizable_tree"]
    check("#501: blocked head: skip predicate ran once",
          rc == 0 and len(skips) == 1)
    check("#501: blocked head: memo lookup ran once per step", len(trees) == 3)


def main():
    print("test-gate-runner.py")
    for fn in [
        test_form_a_pass, test_form_a_fail,
        test_form_b_order_and_pass, test_form_b_hard_fail_stops,
        test_step_lines_carry_duration,
        test_launch_failure_carries_duration,
        test_form_b_soft_fail_continues,
        test_form_b_skip_if_absent_skips, test_form_b_skip_if_absent_present_runs,
        test_form_b_skip_if_no_match_skips, test_form_b_skip_if_match_runs,
        test_mutually_exclusive, test_broken_toml,
        test_malformed_prep_pr_fails_closed,
        test_fallback_umbrella_makefile, test_fallback_umbrella_prepush_script,
        test_fallback_claude_md, test_fallback_claude_md_hard_fail,
        test_fallback_basics_python, test_fallback_basics_python_fail,
        test_fallback_terminal_fail_open, test_no_claude_gates_block_falls_through,
        test_receipt_schema_valid_on_pass, test_receipt_result_fail_still_written,
        test_receipt_steps_records_match, test_receipt_malformed_form_b_config_error,
        test_receipt_non_git_fail_open,
        test_receipt_dirty_tree_never_passes,
        test_receipt_path_in_untracked_dir_not_dirty,
        test_receipt_status_error_is_dirty,
        test_receipt_snapshot_before_run_toctou,
        test_receipt_review_round_498,
        test_receipt_unresolvable_head_removes_stale,
        test_memoize_pure_step_skipped_second_run, test_memoize_dirty_worktree_reruns,
        test_memoize_untracked_input_reruns,
        test_memoize_impure_step_never_cached, test_memoize_failing_pure_not_cached,
        test_memoize_off_by_default,
        test_parallel_serial_byte_identical, test_parallel_validation,
        test_parallel_output_contiguous, test_parallel_declaration_order,
        test_parallel_exclusive_overlaps_nothing,
        test_parallel_fail_fast_kills_groups, test_parallel_interrupt_kills_groups,
        test_parallel_receipt, test_parallel_double_interrupt_term_ignoring,
        test_parallel_soft_skip_memo, test_parallel_launch_error_and_tiebreak,
        test_parallel_head_checked_once,
    ]:
        print(f"- {fn.__name__}")
        fn()
    print()
    if FAILS:
        print(f"FAIL ({len(FAILS)} check(s) failed):")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("all gate-runner checks passed")


if __name__ == "__main__":
    main()
