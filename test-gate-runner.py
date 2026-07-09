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
import subprocess
import sys
import tempfile

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


def main():
    print("test-gate-runner.py")
    for fn in [
        test_form_a_pass, test_form_a_fail,
        test_form_b_order_and_pass, test_form_b_hard_fail_stops,
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
        test_memoize_pure_step_skipped_second_run, test_memoize_dirty_worktree_reruns,
        test_memoize_untracked_input_reruns,
        test_memoize_impure_step_never_cached, test_memoize_failing_pure_not_cached,
        test_memoize_off_by_default,
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
