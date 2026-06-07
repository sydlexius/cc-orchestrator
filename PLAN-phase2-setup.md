# orchestrate-setup.py (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `orchestrate-setup.py` - a Python CLI that gates orchestrate-session prerequisites (`doctor`), arms a session (`up`: scaffold artifacts + marker + prove the floor), and disarms it (`down`), proven by a harness.

**Architecture:** One Python CLI in the `~/Developer/claude-kit` gist (canonical, auto-synced), symlinked to `~/.claude/scripts/`. Three subcommands built incrementally; each capability is added with harness cases (red -> green) then committed. All real paths (settings.json, marker, guard, templates, artifact dir) are read from env vars with production defaults, so the harness drives the script against temp fixtures and NEVER touches the real environment.

**Tech Stack:** python3 stdlib only (`argparse`, `json`, `os`, `subprocess`, `shutil`, `tempfile`). No new deps. Design spec: `~/.claude/skills/orchestrate/DESIGN-phase2-setup.md`.

**Conventions:** `CK=~/Developer/claude-kit`. Commits land in the gist (a git repo; the Stop-hook auto-sync also commits, but commit explicitly for descriptive history). `~/.claude/` edits (symlink) are applied, not committed. All paths absolute. No emoji, no em-dashes.

---

## File Structure
- Create: `~/Developer/cc-orchestrator/orchestrate-setup.py` - the CLI (doctor/up/down). Symlinked to `~/.claude/scripts/orchestrate-setup.py`.
- Create: `~/Developer/cc-orchestrator/test-orchestrate-setup.py` - the proof harness (gist-only; resolves the script via its own dir).
- Reads (never writes): `~/.claude/settings.json` (via `ORCHESTRATE_SETTINGS`), the guard (via `ORCHESTRATE_GUARD`), `templates/` (via `ORCHESTRATE_TEMPLATES_DIR`).
- Writes: the marker (via `ORCHESTRATE_FLOOR_MARKER`) and artifacts under `ORCHESTRATE_ARTIFACT_DIR` (default `/tmp`).

### Env-var seams (the testability contract)
| Env var | Default | Used for |
|---|---|---|
| `ORCHESTRATE_SETTINGS` | `~/.claude/settings.json` | read-only settings inspection |
| `ORCHESTRATE_FLOOR_MARKER` | `~/.claude/orchestrate-floor.active` | the marker armed/removed (matches the guard's var) |
| `ORCHESTRATE_GUARD` | `~/.claude/scripts/orchestrate-guard.sh` | guard health + armed self-test |
| `ORCHESTRATE_TEMPLATES_DIR` | `~/.claude/skills/orchestrate/templates` | stack.schema.json, pr-shipper-brief.md, required-permissions.md |
| `ORCHESTRATE_ARTIFACT_DIR` | `/tmp` | the stack file + pr-triage dir + rendered briefs |

---

## Task 1: Skeleton (argparse + empty doctor) + harness runner

**Files:**
- Create: `~/Developer/cc-orchestrator/orchestrate-setup.py`
- Create: `~/Developer/cc-orchestrator/test-orchestrate-setup.py`

- [ ] **Step 1: Write the script skeleton**

Create `~/Developer/cc-orchestrator/orchestrate-setup.py`:

```python
#!/usr/bin/env python3
"""orchestrate-setup.py - bootstrap/teardown for an orchestrate session.

  doctor [--repo PATH]                          read-only prerequisite check (exit 0 ok, 1 hard-fail)
  up --team NAME --repo PATH [--spacing SEC]    arm a session (scaffold + marker + armed self-test)
  down [--team NAME]                            disarm (rm marker + print teardown checklist)

Design: ~/.claude/skills/orchestrate/DESIGN-phase2-setup.md
All real paths come from env vars (see below) so the harness drives temp fixtures.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
SETTINGS = os.environ.get("ORCHESTRATE_SETTINGS", os.path.join(HOME, ".claude", "settings.json"))
MARKER = os.environ.get("ORCHESTRATE_FLOOR_MARKER", os.path.join(HOME, ".claude", "orchestrate-floor.active"))
GUARD = os.environ.get("ORCHESTRATE_GUARD", os.path.join(HOME, ".claude", "scripts", "orchestrate-guard.sh"))
TEMPLATES = os.environ.get("ORCHESTRATE_TEMPLATES_DIR", os.path.join(HOME, ".claude", "skills", "orchestrate", "templates"))
ARTIFACTS = os.environ.get("ORCHESTRATE_ARTIFACT_DIR", "/tmp")

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _emit(status, msg):
    print(f"[{status:4}] {msg}")
    return status


def cmd_doctor(args):
    # checks are added in later tasks; for now, no hard fail.
    results = []
    hard_fail = any(s == FAIL for s in results)
    print()
    print("doctor: HARD-FAIL" if hard_fail else "doctor: ok (no hard fail)")
    return 1 if hard_fail else 0


def cmd_up(args):
    print("up: not implemented yet")
    return 0


def cmd_down(args):
    print("down: not implemented yet")
    return 0


def main():
    p = argparse.ArgumentParser(prog="orchestrate-setup.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor"); d.add_argument("--repo")
    u = sub.add_parser("up"); u.add_argument("--team", required=True); u.add_argument("--repo", required=True); u.add_argument("--spacing", default="90")
    w = sub.add_parser("down"); w.add_argument("--team")
    args = p.parse_args()
    return {"doctor": cmd_doctor, "up": cmd_up, "down": cmd_down}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x ~/Developer/cc-orchestrator/orchestrate-setup.py`
Expected: no output.

- [ ] **Step 3: Write the harness runner + first case**

Create `~/Developer/cc-orchestrator/test-orchestrate-setup.py`:

```python
#!/usr/bin/env python3
"""Proof harness for orchestrate-setup.py. Drives the CLI against temp fixtures
(temp settings/marker/guard/templates/artifact dirs) so the real env is never touched.
Run: python3 test-orchestrate-setup.py"""
import json
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orchestrate-setup.py")
FAILS = []


def run(args, *, env_overrides=None, tmux=True):
    """Invoke the CLI. Returns (returncode, stdout+stderr)."""
    env = dict(os.environ)
    env.pop("TOOL_INPUT", None)
    if tmux:
        env["TMUX"] = env.get("TMUX") or "/tmp/tmux-test,1,0"
    else:
        env.pop("TMUX", None)
    if env_overrides:
        env.update(env_overrides)
    p = subprocess.run([sys.executable, SCRIPT, *args], env=env,
                       capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout + p.stderr


def check(label, cond):
    status = "ok" if cond else "FAIL"
    if not cond:
        FAILS.append(label)
    print(f"  [{status}] {label}")


def main():
    # Task 1: skeleton runs and doctor exits 0 with no checks.
    rc, out = run(["doctor"])
    check("doctor skeleton exits 0", rc == 0)
    check("doctor prints no-hard-fail", "no hard fail" in out)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("All harness checks passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the harness green**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-setup.py`
Expected: `All harness checks passed.`

- [ ] **Step 5: Commit**

```bash
git -C ~/Developer/claude-kit add orchestrate-setup.py test-orchestrate-setup.py
git -C ~/Developer/claude-kit commit -m "feat(orchestrate): setup.py skeleton + harness runner (doctor/up/down stubs)"
```

---

## Task 2: doctor checks - Agent Teams + tmux

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-setup.py`
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-setup.py`

- [ ] **Step 1: Add the two checks + a settings loader**

In `orchestrate-setup.py`, add after `_emit`:

```python
def _load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def check_agent_teams(settings):
    on = os.environ.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS") == "1"
    if not on and settings:
        on = settings.get("env", {}).get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS") == "1"
    mode = (settings or {}).get("teammateMode")
    if on and mode == "tmux":
        return _emit(PASS, "Agent Teams enabled (env=1, teammateMode=tmux)")
    return _emit(FAIL, f"Agent Teams not ready (enabled={on}, teammateMode={mode!r}); set CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 + teammateMode=tmux")


def check_tmux():
    if not shutil.which("tmux"):
        return _emit(FAIL, "tmux not installed")
    if not os.environ.get("TMUX"):
        return _emit(FAIL, "lead is NOT inside tmux ($TMUX empty) - teammates will not spawn; relaunch claude inside tmux")
    return _emit(PASS, "tmux installed and lead is inside it")
```

- [ ] **Step 2: Wire them into `cmd_doctor`**

Replace `cmd_doctor`'s `results = []` line with:

```python
    settings = _load_settings()
    results = [check_agent_teams(settings), check_tmux()]
```

- [ ] **Step 3: Add harness cases**

In `test-orchestrate-setup.py`, add a helper to write a temp settings file and cases. Add before `main`'s final print block (after the Task 1 cases):

```python
    with tempfile.TemporaryDirectory() as td:
        good = os.path.join(td, "settings.json")
        json.dump({"teammateMode": "tmux", "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}}, open(good, "w"))
        rc, out = run(["doctor"], env_overrides={"ORCHESTRATE_SETTINGS": good}, tmux=True)
        check("teams+tmux PASS -> no hard fail (rc0)", rc == 0 and "Agent Teams enabled" in out)
        rc, out = run(["doctor"], env_overrides={"ORCHESTRATE_SETTINGS": good}, tmux=False)
        check("not in tmux -> hard fail (rc1)", rc == 1 and "NOT inside tmux" in out)
        bad = os.path.join(td, "bad.json")
        json.dump({"teammateMode": "acceptEdits"}, open(bad, "w"))
        rc, out = run(["doctor"], env_overrides={"ORCHESTRATE_SETTINGS": bad, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": ""}, tmux=True)
        check("teams off -> hard fail (rc1)", rc == 1 and "Agent Teams not ready" in out)
```

- [ ] **Step 4: Run harness; verify red-then-green**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-setup.py`
Expected: PASS - all checks green (teams+tmux pass; not-in-tmux and teams-off both hard-fail).

- [ ] **Step 5: Commit**

```bash
git -C ~/Developer/claude-kit add orchestrate-setup.py test-orchestrate-setup.py
git -C ~/Developer/claude-kit commit -m "feat(orchestrate): doctor checks for Agent Teams + tmux"
```

---

## Task 3: doctor checks - guard wired (verify-and-print) + guard healthy

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-setup.py`
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-setup.py`

- [ ] **Step 1: Add the two checks**

In `orchestrate-setup.py`, add after `check_tmux`:

```python
GUARD_HOOK_JSON = """  Add this to ~/.claude/settings.json under hooks.PreToolUse (verify-and-print; this
  script never writes settings.json):
    { "matcher": "Bash", "hooks": [ { "type": "command",
      "command": "bash \\"$HOME/.claude/scripts/orchestrate-guard.sh\\"" } ] }"""


def check_guard_wired(settings):
    hooks = (settings or {}).get("hooks", {}).get("PreToolUse", [])
    for block in hooks:
        if block.get("matcher") == "Bash":
            for h in block.get("hooks", []):
                if "orchestrate-guard.sh" in h.get("command", ""):
                    return _emit(PASS, "guard wired as the PreToolUse Bash hook")
    _emit(FAIL, "guard NOT wired as a PreToolUse Bash hook")
    print(GUARD_HOOK_JSON)
    return FAIL


def check_guard_healthy():
    try:
        p = subprocess.run([GUARD, "--self-test"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return _emit(FAIL, f"guard self-test could not run ({e}); guard at {GUARD}")
    if p.returncode == 0:
        return _emit(PASS, "guard --self-test passes (not failing open)")
    return _emit(FAIL, f"guard --self-test FAILED (rc={p.returncode}); guard may be failing open")
```

- [ ] **Step 2: Wire into `cmd_doctor`**

Update the `results` line in `cmd_doctor`:

```python
    results = [check_agent_teams(settings), check_tmux(),
               check_guard_wired(settings), check_guard_healthy()]
```

- [ ] **Step 3: Add a stub-guard helper + cases to the harness**

In `test-orchestrate-setup.py`, add near the top (after imports):

```python
def write_stub_guard(path, selftest_rc=0):
    """A fake guard: `--self-test` exits selftest_rc; a payload on stdin exits 2 if it
    contains a push-main/merge trigger, else 0 (mimics the real guard for up's self-test)."""
    with open(path, "w") as f:
        f.write(
            "#!/usr/bin/env bash\n"
            f'[ "$1" = "--self-test" ] && exit {selftest_rc}\n'
            'in=$(cat 2>/dev/null)\n'
            'case "$in" in *\\"git push origin main\\"*|*\\"gh pr merge*) exit 2 ;; esac\n'
            "exit 0\n")
    os.chmod(path, 0o755)
```

Add cases inside the existing `with tempfile.TemporaryDirectory() as td:` block (after the Task 2 cases), extending the `good` settings to include the guard hook:

```python
        guard = os.path.join(td, "guard.sh"); write_stub_guard(guard, selftest_rc=0)
        wired = os.path.join(td, "wired.json")
        json.dump({"teammateMode": "tmux", "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
                   "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                       {"type": "command", "command": 'bash "$HOME/.claude/scripts/orchestrate-guard.sh"'}]}]}},
                  open(wired, "w"))
        rc, out = run(["doctor"], env_overrides={"ORCHESTRATE_SETTINGS": wired, "ORCHESTRATE_GUARD": guard})
        check("guard wired + healthy -> PASS", "guard wired" in out and "self-test passes" in out)
        rc, out = run(["doctor"], env_overrides={"ORCHESTRATE_SETTINGS": good, "ORCHESTRATE_GUARD": guard})
        check("guard not wired -> hard fail + prints JSON", rc == 1 and "guard NOT wired" in out and "hooks.PreToolUse" in out)
        badguard = os.path.join(td, "badguard.sh"); write_stub_guard(badguard, selftest_rc=1)
        rc, out = run(["doctor"], env_overrides={"ORCHESTRATE_SETTINGS": wired, "ORCHESTRATE_GUARD": badguard})
        check("guard self-test fails -> hard fail", rc == 1 and "self-test FAILED" in out)
```

- [ ] **Step 4: Run harness green**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-setup.py`
Expected: PASS - guard-wired+healthy passes; not-wired prints the JSON and hard-fails; bad self-test hard-fails.

- [ ] **Step 5: Commit**

```bash
git -C ~/Developer/claude-kit add orchestrate-setup.py test-orchestrate-setup.py
git -C ~/Developer/claude-kit commit -m "feat(orchestrate): doctor guard checks (verify-and-print wiring + self-test health)"
```

---

## Task 4: doctor checks - repo main + allow-list diff (doctor complete)

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-setup.py`
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-setup.py`

- [ ] **Step 1: Add the two checks**

In `orchestrate-setup.py`, add after `check_guard_healthy`:

```python
def check_repo_main(repo):
    if not repo:
        return _emit(WARN, "no --repo given; skipping repo/HEAD check")
    try:
        head = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=15)
        dirty = subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return _emit(FAIL, f"could not inspect repo {repo}: {e}")
    if head.returncode != 0:
        return _emit(FAIL, f"{repo} is not a git repo")
    h = head.stdout.strip()
    if dirty.stdout.strip():
        return _emit(WARN, f"{repo} HEAD={h} but working tree is DIRTY (stray files won't block, just a heads-up)")
    return _emit(PASS, f"{repo} clean at HEAD={h}")


def check_allowlist(settings):
    allow = set((settings or {}).get("permissions", {}).get("allow", []))
    req_file = os.path.join(TEMPLATES, "required-permissions.md")
    try:
        text = open(req_file).read()
    except OSError:
        return _emit(WARN, f"required-permissions.md not found at {req_file}; skipping allow-list diff")
    import re
    needed = set(re.findall(r"`(Bash\([^`]+\)|Write\([^`]+\)|Edit\([^`]+\)|Read\([^`]+\))`", text))
    missing = sorted(n for n in needed if n not in allow)
    if not missing:
        return _emit(PASS, "allow-list covers the documented bot entries")
    _emit(WARN, f"{len(missing)} allow-list entries from required-permissions.md are MISSING:")
    for m in missing:
        print(f"         {m}")
    return WARN
```

- [ ] **Step 2: Wire into `cmd_doctor` (final form)**

Replace `cmd_doctor` body's results line + return so it reads:

```python
    settings = _load_settings()
    results = [check_agent_teams(settings), check_tmux(),
               check_guard_wired(settings), check_guard_healthy(),
               check_repo_main(getattr(args, "repo", None)), check_allowlist(settings)]
    hard_fail = any(s == FAIL for s in results)
    print()
    print("doctor: HARD-FAIL (fix the FAIL lines above before `up`)" if hard_fail else "doctor: ok (no hard fail)")
    return 1 if hard_fail else 0
```

- [ ] **Step 3: Add harness cases (temp git repo + required-permissions fixture)**

In `test-orchestrate-setup.py`, add inside the temp-dir block (after Task 3 cases):

```python
        repo = os.path.join(td, "repo"); os.makedirs(repo)
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        tpl = os.path.join(td, "templates"); os.makedirs(tpl)
        open(os.path.join(tpl, "required-permissions.md"), "w").write("- `Bash(gh pr *)`\n- `Bash(zzz-missing *)`\n")
        ov = {"ORCHESTRATE_SETTINGS": wired, "ORCHESTRATE_GUARD": guard, "ORCHESTRATE_TEMPLATES_DIR": tpl}
        rc, out = run(["doctor", "--repo", repo], env_overrides=ov)
        check("clean repo -> PASS with HEAD", "clean at HEAD=" in out)
        check("allow-list missing entry -> WARN (not hard fail)", "MISSING" in out and "zzz-missing" in out and rc == 0)
        open(os.path.join(repo, "dirt.txt"), "w").write("x")
        rc, out = run(["doctor", "--repo", repo], env_overrides=ov)
        check("dirty repo -> WARN not FAIL (rc0)", "DIRTY" in out and rc == 0)
```

- [ ] **Step 4: Run harness green**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-setup.py`
Expected: PASS - clean repo passes, missing allow-list and dirty tree are WARN (rc 0, not hard fail).

- [ ] **Step 5: Commit**

```bash
git -C ~/Developer/claude-kit add orchestrate-setup.py test-orchestrate-setup.py
git -C ~/Developer/claude-kit commit -m "feat(orchestrate): doctor repo-main + allow-list checks (doctor complete)"
```

---

## Task 5: `up` - scaffold artifacts

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-setup.py`
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-setup.py`

- [ ] **Step 1: Add the scaffold function**

In `orchestrate-setup.py`, add after `check_allowlist`:

```python
def scaffold_artifacts(team, repo, spacing):
    os.makedirs(ARTIFACTS, exist_ok=True)
    stack = os.path.join(ARTIFACTS, f"{team}-stack.json")
    with open(stack, "w") as f:
        json.dump([], f)
    triage = os.path.join(ARTIFACTS, "pr-triage")
    os.makedirs(triage, exist_ok=True)
    brief_out = os.path.join(ARTIFACTS, "pr-shipper-brief.md")
    src = os.path.join(TEMPLATES, "pr-shipper-brief.md")
    try:
        body = open(src).read()
    except OSError:
        body = "# pr-shipper brief\n(template not found; fill manually)\n"
    body = (body.replace("{{TEAM}}", team).replace("{{REPO}}", repo)
                .replace("{{SPACING}}", str(spacing)).replace("{{STACK_PATH}}", stack))
    header = f"<!-- rendered by orchestrate-setup.py: team={team} repo={repo} spacing={spacing}s stack={stack} -->\n"
    with open(brief_out, "w") as f:
        f.write(header + body)
    return stack, triage, brief_out
```

- [ ] **Step 2: Call it from a partial `cmd_up`**

Replace `cmd_up` with:

```python
def cmd_up(args):
    rc = cmd_doctor(args)
    if rc != 0:
        print("\nup: ABORT - doctor reported a hard failure; fix it and re-run.", file=sys.stderr)
        return rc
    stack, triage, brief = scaffold_artifacts(args.team, args.repo, args.spacing)
    print(f"\nscaffolded: stack={stack} triage={triage} brief={brief}")
    # marker + armed self-test added in Task 6.
    return 0
```

- [ ] **Step 3: Add harness cases**

In `test-orchestrate-setup.py`, add inside the temp-dir block (after Task 4 cases):

```python
        art = os.path.join(td, "artifacts"); os.makedirs(art)
        os.makedirs(os.path.join(tpl, "x"), exist_ok=True)
        open(os.path.join(tpl, "pr-shipper-brief.md"), "w").write("repo={{REPO}} team={{TEAM}} stack={{STACK_PATH}}\n")
        upov = dict(ov); upov.update({"ORCHESTRATE_ARTIFACT_DIR": art, "ORCHESTRATE_FLOOR_MARKER": os.path.join(td, "m.active")})
        rc, out = run(["up", "--team", "demo", "--repo", repo], env_overrides=upov)
        stack = os.path.join(art, "demo-stack.json")
        check("up scaffolds stack=[]", os.path.exists(stack) and json.load(open(stack)) == [])
        check("up creates triage dir", os.path.isdir(os.path.join(art, "pr-triage")))
        brief = open(os.path.join(art, "pr-shipper-brief.md")).read()
        check("brief substitutions rendered", "team=demo" in brief and f"repo={repo}" in brief and "{{" not in brief)
```

- [ ] **Step 4: Run harness green**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-setup.py`
Expected: PASS - up scaffolds a valid empty stack, the triage dir, and a fully-substituted brief.

- [ ] **Step 5: Commit**

```bash
git -C ~/Developer/claude-kit add orchestrate-setup.py test-orchestrate-setup.py
git -C ~/Developer/claude-kit commit -m "feat(orchestrate): up scaffolds stack + triage dir + rendered brief"
```

---

## Task 6: `up` - arm marker + armed self-test + abort-on-fail-open

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-setup.py`
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-setup.py`

- [ ] **Step 1: Add marker + armed-self-test functions**

In `orchestrate-setup.py`, add after `scaffold_artifacts`:

```python
def _now_iso():
    return subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                          capture_output=True, text=True).stdout.strip()


def arm_marker(team, repo, head):
    with open(MARKER, "w") as f:
        f.write(f"orchestrate session\nteam: {team}\nstarted: {_now_iso()}\nrepo: {repo}\nhead: {head}\n")


def _feed_guard(command):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    p = subprocess.run([GUARD], input=payload, capture_output=True, text=True, timeout=15)
    return p.returncode


def armed_self_test():
    """With the marker armed, BOTH a Tier-1 push-main and a Tier-2 merge must block (exit 2).
    Payloads are built here and fed on stdin, so the live hook never sees a trigger on the
    command line (the orchestrate test-driving rule)."""
    t1 = _feed_guard("git push origin main")
    t2 = _feed_guard("gh pr merge 5")
    return t1 == 2 and t2 == 2
```

- [ ] **Step 2: Complete `cmd_up`**

Replace `cmd_up` with:

```python
def cmd_up(args):
    rc = cmd_doctor(args)
    if rc != 0:
        print("\nup: ABORT - doctor reported a hard failure; fix it and re-run.", file=sys.stderr)
        return rc
    head = subprocess.run(["git", "-C", args.repo, "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    stack, triage, brief = scaffold_artifacts(args.team, args.repo, args.spacing)
    arm_marker(args.team, args.repo, head)
    if not armed_self_test():
        os.remove(MARKER)
        print("\nup: ABORT - armed self-test FAILED: the guard did not block push-main and/or "
              "merge with the marker armed. The floor is failing open. Marker REMOVED. Fix the "
              "guard before standing up a session.", file=sys.stderr)
        return 1
    print(f"\nup: SESSION ARMED."
          f"\n  marker:  {MARKER}"
          f"\n  stack:   {stack}\n  triage:  {triage}\n  brief:   {brief}"
          f"\n  Merges are now HUMAN-ONLY via the ! prefix until `down`.")
    return 0
```

- [ ] **Step 3: Add harness cases (real-guard arm path + fail-open abort path)**

In `test-orchestrate-setup.py`, add inside the temp-dir block (after Task 5 cases). The stub guard already blocks push-main/merge, so it stands in for the real guard's arm path:

```python
        marker = os.path.join(td, "m.active")
        upov2 = dict(upov); upov2["ORCHESTRATE_FLOOR_MARKER"] = marker
        rc, out = run(["up", "--team", "demo", "--repo", repo], env_overrides=upov2)
        check("up arms the marker on success", rc == 0 and os.path.exists(marker) and "SESSION ARMED" in out)
        check("marker has a header", "team: demo" in open(marker).read())
        os.remove(marker)
        openguard = os.path.join(td, "openguard.sh"); write_stub_guard(openguard, selftest_rc=0)
        # make it ALWAYS exit 0 (fails open even on a trigger) to simulate a broken floor:
        open(openguard, "w").write("#!/usr/bin/env bash\n[ \"$1\" = \"--self-test\" ] && exit 0\nexit 0\n")
        os.chmod(openguard, 0o755)
        failov = dict(upov2); failov["ORCHESTRATE_GUARD"] = openguard
        rc, out = run(["up", "--team", "demo", "--repo", repo], env_overrides=failov)
        check("fail-open guard -> up ABORTS (rc1)", rc == 1 and "failing open" in out)
        check("fail-open -> marker REMOVED (no half-armed session)", not os.path.exists(marker))
```

Note: the fail-open stub passes `--self-test` (so `doctor`'s health check passes) but exits 0 on the armed payloads, isolating the `up` armed-self-test as the thing that catches the broken floor.

- [ ] **Step 4: Run harness green**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-setup.py`
Expected: PASS - up arms the marker with a header on success; a fail-open guard makes up abort AND removes the marker.

- [ ] **Step 5: Commit**

```bash
git -C ~/Developer/claude-kit add orchestrate-setup.py test-orchestrate-setup.py
git -C ~/Developer/claude-kit commit -m "feat(orchestrate): up arms marker + armed self-test (abort+cleanup on fail-open)"
```

---

## Task 7: `down` - rm marker + print teardown checklist

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-setup.py`
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-setup.py`

- [ ] **Step 1: Implement `cmd_down`**

Replace `cmd_down` with:

```python
TEARDOWN_CHECKLIST = """down: marker removed (Tier-2 merge-gating OFF).
Team teardown is the lead's job (tool calls, not this script):
  1. SendMessage shutdown_request to EACH teammate.
  2. WAIT for each "terminated" notice.
  3. THEN TeamDelete (it refuses while a member is alive).
  4. LEAVE worktrees that still have open PRs; clean the rest with `make remove-worktree`."""


def cmd_down(args):
    existed = os.path.exists(MARKER)
    try:
        os.remove(MARKER)
    except FileNotFoundError:
        pass
    print(TEARDOWN_CHECKLIST if existed else "down: no marker present (already disarmed).")
    print(TEARDOWN_CHECKLIST.split("\n", 1)[1] if not existed else "")
    return 0
```

- [ ] **Step 2: Add harness cases**

In `test-orchestrate-setup.py`, add inside the temp-dir block (after Task 6 cases):

```python
        open(marker, "w").write("x")
        downov = {"ORCHESTRATE_FLOOR_MARKER": marker}
        rc, out = run(["down"], env_overrides=downov)
        check("down removes the marker", rc == 0 and not os.path.exists(marker) and "marker removed" in out)
        rc, out = run(["down"], env_overrides=downov)
        check("down is idempotent (no marker)", rc == 0 and "already disarmed" in out)
        check("down prints the teardown checklist", "shutdown_request" in out and "TeamDelete" in out)
```

- [ ] **Step 3: Run harness green**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-setup.py`
Expected: PASS - down removes the marker, is idempotent, and prints the shutdown -> TeamDelete checklist.

- [ ] **Step 4: Commit**

```bash
git -C ~/Developer/claude-kit add orchestrate-setup.py test-orchestrate-setup.py
git -C ~/Developer/claude-kit commit -m "feat(orchestrate): down removes marker + prints teardown checklist"
```

---

## Task 8: Deploy (symlink) + real doctor run + lint

**Files:**
- Create: `~/.claude/scripts/orchestrate-setup.py` (symlink)

- [ ] **Step 1: Symlink for convenient invocation**

```bash
ln -sf ~/Developer/cc-orchestrator/orchestrate-setup.py ~/.claude/scripts/orchestrate-setup.py
ls -l ~/.claude/scripts/orchestrate-setup.py
```
Expected: the symlink points at `/Users/jesse/Developer/cc-orchestrator/orchestrate-setup.py`.

- [ ] **Step 2: Lint (if a linter is available)**

Run: `python3 -m pyflakes ~/Developer/cc-orchestrator/orchestrate-setup.py 2>/dev/null || ruff check ~/Developer/cc-orchestrator/orchestrate-setup.py 2>/dev/null || python3 -c "import ast,sys; ast.parse(open('$HOME/Developer/cc-orchestrator/orchestrate-setup.py').read()); print('syntax ok')"`
Expected: no warnings, or `syntax ok`.

- [ ] **Step 3: Real `doctor` run in THIS environment**

Run: `~/.claude/scripts/orchestrate-setup.py doctor --repo /Users/jesse/Developer/stillwater`
Expected: PASS on Agent Teams, tmux, guard-wired, guard-healthy; a HEAD line for the repo; allow-list either PASS or a WARN list. No hard FAIL (or, if there is one, it is a real environment gap to report).

- [ ] **Step 4: Commit (script is already committed; nothing new in the gist)**

The symlink lives in `~/.claude/` (not a repo). No commit needed. Confirm the gist is clean: `git -C ~/Developer/claude-kit status --porcelain orchestrate-setup.py` (expect no output).

---

## Task 9: Final gate - full harness + critic convergence + design read-through

**Files:** none (verification only)

- [ ] **Step 1: Full harness green**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-setup.py`
Expected: `All harness checks passed.`

- [ ] **Step 2: Confirm the gist commits landed**

Run: `git -C ~/Developer/claude-kit log --oneline -8`
Expected: the Task 1-7 commits are present.

- [ ] **Step 3: Hand off to the adversarial / ralph critic convergence pass**

Per `~/Developer/cc-orchestrator/engage-ralph-loop.md`, instantiate: TARGET = orchestrate-setup.py + its harness; SCOPE = honest-operator setup mistakes (missing/half prereqs, half-armed sessions, stale artifacts, the fail-open->abort path), NOT adversarial evasion; GATES = the harness + a lint/syntax check + a real `doctor` run; ISOLATION = drive the guard via subprocess/stdin, never trigger substrings on the command line. Loop until K=2 dry rounds; fix real findings + add regression cases; document accepted limitations in `DESIGN-phase2-setup.md`.

- [ ] **Step 4: Design read-through**

Confirm against `DESIGN-phase2-setup.md`: verify-and-print settings (never writes), artifacts scaffolded, no heartbeat, teardown print-only, fail-CLOSED-at-setup self-test. Report convergence before declaring Phase 2 done.

---

## Self-Review (plan vs spec)

**Spec coverage:** doctor checks (teams+tmux T2; guard wired verify-and-print + healthy T3; repo-main + allow-list T4) -> covered. up (scaffold T5; marker + armed self-test + fail-open abort T6) -> covered. down (rm + checklist, idempotent T7) -> covered. Env-var seams (testability) -> Task 1 + used throughout. Symlink + real doctor + lint -> T8. Harness + critic pass -> T9. Out-of-scope (heartbeat, durable stack, port allocator, clean-worktree assertion, team spawn) -> correctly absent. All spec sections map to a task.

**Placeholder scan:** no TBD/TODO; every code step shows complete code; every run step shows the exact command + expected result.

**Type/name consistency:** function names (`_emit`, `_load_settings`, `check_agent_teams`, `check_tmux`, `check_guard_wired`, `check_guard_healthy`, `check_repo_main`, `check_allowlist`, `scaffold_artifacts`, `arm_marker`, `_feed_guard`, `armed_self_test`, `cmd_doctor/up/down`) are defined once and reused consistently. Env vars (`ORCHESTRATE_SETTINGS/FLOOR_MARKER/GUARD/TEMPLATES_DIR/ARTIFACT_DIR`) match the guard's `ORCHESTRATE_FLOOR_MARKER` and are consistent across script + harness. Status constants `PASS/WARN/FAIL` used uniformly; only `FAIL` is a hard fail.

**Known accepted (spec, not bugs):** marker heartbeat, durable stack, port allocator, clean-worktree teardown assertion -> Phase 3. `_now_iso` shells `date` (Date.now-free style is irrelevant here; this is a normal script). Brief substitution uses `{{TOKEN}}` placeholders the template must contain; a template without them renders unchanged (harness covers the substituted case).
