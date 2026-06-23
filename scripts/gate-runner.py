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
  - Form B: `steps = [ { name, run, required?, skip_if_absent?, skip_if? } ]`
            -> run each in order with per-step skip predicates.
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

Exit codes: 0 = all gates passed / skipped / fell open; non-zero = a required
gate failed.

Run: python3 gate-runner.py   (from anywhere inside the repo)
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tomllib

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
    try:
        proc = subprocess.run(command, shell=True, cwd=cwd, check=False)
    except OSError as e:
        log(f"[FAIL] {label}: could not launch ({e})")
        return False
    ok = proc.returncode == 0
    log(f"[{'PASS' if ok else 'FAIL'}] {label} (exit {proc.returncode})")
    return ok


# --- Form A / Form B over a parsed [prep_pr] table -------------------------

def run_prep_pr(prep, root):
    """Run the [prep_pr] table. Return process exit code (0 ok, non-zero fail)."""
    has_gate = "gate" in prep
    has_steps = "steps" in prep
    if has_gate and has_steps:
        warn("[prep_pr] sets BOTH `gate` and `steps`; they are mutually "
             "exclusive. Refusing to guess.")
        return 2
    if has_gate:
        return run_form_a(prep["gate"], root)
    if has_steps:
        return run_form_b(prep["steps"], root)
    warn("[prep_pr] has neither `gate` nor `steps`; nothing to run.")
    return 0


def run_form_a(gate, root):
    log(f"gate-runner: .gates.toml Form A (delegate) -> {gate!r}")
    if not isinstance(gate, str) or not gate.strip():
        warn("[prep_pr].gate must be a non-empty string")
        return 2
    ok = _run_command("gate", gate, root)
    return 0 if ok else 1


def _skip_reason(step, root):
    """Return a skip reason string if the step should be skipped, else None."""
    tool = step.get("skip_if_absent")
    if tool and shutil.which(tool) is None:
        return f"{tool} not on PATH"
    pattern = step.get("skip_if")
    if pattern:
        matches = glob.glob(os.path.join(root, pattern), recursive=True)
        if not matches:
            return f"no files match {pattern}"
    return None


def run_form_b(steps, root):
    log(f"gate-runner: .gates.toml Form B (enumerate) -> {len(steps)} step(s)")
    if not isinstance(steps, list):
        warn("[prep_pr].steps must be an array of step tables")
        return 2
    soft_failures = 0
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            warn(f"step #{i} is not a table; skipping")
            continue
        name = step.get("name") or f"step-{i}"
        run = step.get("run")
        if not run:
            warn(f"step {name!r} has no `run`; skipping")
            continue
        required = step.get("required", True)
        reason = _skip_reason(step, root)
        if reason:
            log(f"[SKIP] {name}: {reason}")
            continue
        ok = _run_command(name, run, root)
        if not ok:
            if required:
                log(f"gate-runner: HARD failure at {name!r} -- stopping.")
                return 1
            soft_failures += 1
            log(f"[WARN] {name}: soft failure (required=false), continuing.")
    if soft_failures:
        log(f"gate-runner: all required steps passed "
            f"({soft_failures} soft failure(s) warned, not blocking).")
    else:
        log("gate-runner: all steps passed.")
    return 0


# --- Fail-open fallback chain (no .gates.toml) -----------------------------

def fallback_chain(root):
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


# --- Entry point ------------------------------------------------------------

def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    root = find_repo_root()
    config_path = os.path.join(root, CONFIG_NAME)

    if not os.path.isfile(config_path):
        return fallback_chain(root)

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        # A present-but-broken config is a real error (unlike a missing one).
        warn(f"could not parse {CONFIG_NAME}: {e}")
        return 2

    prep = data.get("prep_pr")
    if not isinstance(prep, dict):
        warn(f"{CONFIG_NAME} has no [prep_pr] table; nothing to run.")
        return 0

    log(f"gate-runner: using {config_path}")
    return run_prep_pr(prep, root)


if __name__ == "__main__":
    sys.exit(main())
