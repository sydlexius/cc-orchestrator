# P3-A Marker Refcounting Implementation Plan

> SUPERSEDED (#139): `TeamCreate`/`TeamDelete` referenced below were REMOVED by Anthropic. The team is now IMPLICIT (spawn named teammates directly via the `Agent` tool) and teardown is `shutdown_request` -> wait for each "terminated" notice (no `TeamDelete` step). The live `SKILL.md` teardown is authoritative; this historical doc is left as-is.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the orchestrate floor's single global marker file with a `$TMUX`-keyed per-session marker directory, so parallel leads never disarm each other and solo sessions are immune to stale tombstones.

**Architecture:** The guard (`orchestrate-guard.sh`) and setup (`orchestrate-setup.py`) both derive a marker filename from `$TMUX` (sanitized) under a shared directory (`$ORCHESTRATE_FLOOR_DIR`, default `~/.claude/orchestrate-floor.d`). `marker_active()` checks only THIS session's keyed file (empty `$TMUX` => never gated). `up` arms its own keyed file; `down` removes only its own file and GCs stale tombstones. TTL raised to 72h.

**Tech Stack:** Bash (guard, fail-open, exit-2 deny), Python 3 (setup CLI + both proof harnesses). No external deps beyond `jq`, `git`, `tmux`, coreutils.

**Spec:** `~/.claude/skills/orchestrate/DESIGN-phase3a-marker-refcounting.md`

**Canonical code dir:** `~/Developer/claude-kit/` (deployed to `~/.claude/scripts/` by symlink). All commands below assume `cd ~/Developer/claude-kit`.

**Sanitization contract (used verbatim in multiple tasks):**
- bash:   `printf '%s' "$TMUX" | tr -c 'A-Za-z0-9' '_'`
- python: `re.sub(r'[^A-Za-z0-9]', '_', tmux)`
- Example: `$TMUX="/tmp/tmux-501/default,12345,0"` => key `_tmp_tmux_501_default_12345_0`

**Test-driving isolation rule (NON-NEGOTIABLE):** never put a trigger substring (`git push`, ` main`, `gh api ... pulls/N/merge`) on a live Bash command line - the live PreToolUse guard inspects it. All trigger payloads are built INSIDE the harness files and fed to the guard on stdin (the existing pattern). Run harnesses with `python3 test-*.py`.

---

## Task 1: Guard harness - convert `run_guard` to the keyed model (failing tests first)

**Files:**
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-guard.py:27-47` (`run_guard`)

This rewrites the harness's marker plumbing to the keyed-dir model. After this task the existing CASES table still drives (its `marker_active` boolean now means "arm THIS test session's key"), but the harness will FAIL against the current guard (which still reads `ORCHESTRATE_FLOOR_MARKER`). That failure is expected and proves the test drives the change.

- [ ] **Step 1: Replace `run_guard` with the keyed version**

Replace the whole function body (lines 27-47) with:

```python
import re  # add near the top imports if not already present

# Fixed test $TMUX for the common single-session cases.
DEFAULT_TMUX = "/tmp/tmux-test,1,0"


def _key(tmux):
    """Mirror the guard's sanitization EXACTLY (contract; see DESIGN)."""
    return re.sub(r'[^A-Za-z0-9]', '_', tmux)


def run_guard(command, *, marker_active, channel, tmux=DEFAULT_TMUX,
              foreign_keys=(), stale_self=False):
    """Invoke the guard. Returns (exit_code, stdout, stderr). channel in {'stdin','env'}.
    command=None means 'send no command at all' (empty-read case).
      - marker_active: arm THIS session's key (fresh) under FLOOR_DIR.
      - tmux: the $TMUX value the guard sees (None => unset => never gated).
      - foreign_keys: extra $TMUX values to arm (fresh) - other sessions' markers.
      - stale_self: arm THIS session's key with an OLD mtime (older than TTL)."""
    with tempfile.TemporaryDirectory() as td:
        floor_dir = os.path.join(td, "orchestrate-floor.d")
        os.makedirs(floor_dir, exist_ok=True)
        ttl_hours = 24
        if marker_active and tmux is not None:
            open(os.path.join(floor_dir, _key(tmux)), "w").close()  # fresh mtime
        if stale_self and tmux is not None:
            p = os.path.join(floor_dir, _key(tmux))
            open(p, "w").close()
            old = __import__("time").time() - (ttl_hours + 1) * 3600
            os.utime(p, (old, old))
        for fk in foreign_keys:
            open(os.path.join(floor_dir, _key(fk)), "w").close()
        env = dict(os.environ)
        env["ORCHESTRATE_FLOOR_DIR"] = floor_dir
        env["ORCHESTRATE_FLOOR_TTL_HOURS"] = str(ttl_hours)
        if tmux is None:
            env.pop("TMUX", None)
        else:
            env["TMUX"] = tmux
        env.pop("TOOL_INPUT", None)
        env.pop("ORCHESTRATE_FLOOR_MARKER", None)  # legacy var is gone
        stdin_data = ""
        if command is not None:
            payload = {"tool_name": "Bash", "tool_input": {"command": command}}
            if channel == "stdin":
                stdin_data = json.dumps(payload)
            elif channel == "env":
                env["TOOL_INPUT"] = json.dumps({"command": command})
        p = subprocess.run([GUARD], input=stdin_data, env=env,
                           capture_output=True, text=True, timeout=5)
        return p.returncode, p.stdout, p.stderr
```

- [ ] **Step 2: Run the harness to verify it now FAILS**

Run: `cd ~/Developer/claude-kit && python3 test-orchestrate-guard.py`
Expected: FAIL - the merge-by-API "(active) -> block" cases now exit 0 (allow) because the current guard reads `ORCHESTRATE_FLOOR_MARKER` (no longer set), so `marker_active()` returns false and the Tier-2 deny never fires. This proves the test drives the guard change.

- [ ] **Step 3: Commit the failing harness**

```bash
cd ~/Developer/claude-kit
git add test-orchestrate-guard.py
git commit -m "test(orchestrate): drive guard marker to \$TMUX-keyed dir model (P3-A, failing)"
```

---

## Task 2: Guard harness - add the refcount/keying cases

**Files:**
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-guard.py` (add a dedicated test block in `main()` before the final print; and a key-contract assertion)

These cover the new properties the CASES boolean table cannot express (foreign markers, empty `$TMUX`, staleness, two-session isolation, key contract).

- [ ] **Step 1: Add the refcount test block**

In `main()`, immediately AFTER the existing `for label, command, marker_active, expected in CASES:` loop and BEFORE the `HARD_DENY` regression block, insert:

```python
    # --- P3-A refcount / keying properties (merge-by-API is the only marker-gated path) ---
    MERGE_API = "gh api -X PUT repos/o/r/pulls/1/merge"
    SESS_A = "/tmp/tmux-501/default,111,0"
    SESS_B = "/tmp/tmux-501/default,222,1"

    def expect(label, rc, want):
        ok = (rc == 2) if want == "block" else (rc == 0)
        print(f"  [{'ok' if ok else 'FAIL'}] {label} -> exit {rc} (want {want})")
        if not ok:
            FAILS.append(f"{label}: expected {want}, got exit {rc}")

    # armed self -> blocked
    rc, _o, _e = run_guard(MERGE_API, marker_active=True, channel="stdin", tmux=SESS_A)
    expect("refcount: armed self merge-by-API blocked", rc, "block")
    # a FOREIGN session's marker does NOT gate me (core refcount property)
    rc, _o, _e = run_guard(MERGE_API, marker_active=False, channel="stdin",
                           tmux=SESS_B, foreign_keys=[SESS_A])
    expect("refcount: foreign marker does NOT gate me", rc, "allow")
    # empty $TMUX is never gated even with a marker file present in the dir
    rc, _o, _e = run_guard(MERGE_API, marker_active=False, channel="stdin",
                           tmux=None, foreign_keys=[SESS_A])
    expect("refcount: empty \$TMUX never gated", rc, "allow")
    # stale self marker (older than TTL) -> not gated
    rc, _o, _e = run_guard(MERGE_API, marker_active=False, channel="stdin",
                           tmux=SESS_A, stale_self=True)
    expect("refcount: stale self marker not gated", rc, "allow")
    # two sessions armed: each gated under its OWN $TMUX
    rc, _o, _e = run_guard(MERGE_API, marker_active=True, channel="stdin",
                           tmux=SESS_A, foreign_keys=[SESS_B])
    expect("refcount: two armed - A gated under A", rc, "block")
    rc, _o, _e = run_guard(MERGE_API, marker_active=True, channel="stdin",
                           tmux=SESS_B, foreign_keys=[SESS_A])
    expect("refcount: two armed - B gated under B", rc, "block")
    # Tier-1 is marker-independent: push-main blocks even with empty $TMUX
    rc, _o, _e = run_guard("git push origin main", marker_active=False,
                           channel="stdin", tmux=None)
    expect("refcount: Tier-1 push-main blocks regardless of \$TMUX", rc, "block")
```

- [ ] **Step 2: Run the harness to verify the new cases drive the change**

Run: `cd ~/Developer/claude-kit && python3 test-orchestrate-guard.py`
Expected: FAIL - the "armed self ... blocked" and "two armed" cases exit 0 against the current guard (still reads the old var); the "allow" cases happen to pass. Confirms the refcount cases are wired and failing for the right reason.

- [ ] **Step 3: Commit**

```bash
cd ~/Developer/claude-kit
git add test-orchestrate-guard.py
git commit -m "test(orchestrate): add \$TMUX-keyed refcount cases for the guard (P3-A, failing)"
```

---

## Task 3: Guard - rewrite `marker_active()` to the keyed-dir model

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-guard.sh:8-9` (vars), `:104-111` (`marker_active`)

- [ ] **Step 1: Replace the marker vars (lines 8-9)**

Replace:
```sh
MARKER="${ORCHESTRATE_FLOOR_MARKER:-$HOME/.claude/orchestrate-floor.active}"
TTL_HOURS="${ORCHESTRATE_FLOOR_TTL_HOURS:-24}"
```
with:
```sh
# P3-A: per-session marker is a $TMUX-keyed file under FLOOR_DIR (refcounting).
# No $TMUX => not an orchestrate session => never gated (see marker_active).
FLOOR_DIR="${ORCHESTRATE_FLOOR_DIR:-$HOME/.claude/orchestrate-floor.d}"
TTL_HOURS="${ORCHESTRATE_FLOOR_TTL_HOURS:-72}"
```

- [ ] **Step 2: Replace `marker_active()` (lines 104-111)**

Replace the whole function with:
```sh
# THIS session's marker present AND fresh. Keyed by $TMUX (sanitized) so one
# session's marker never gates another, and a non-tmux (solo) session - which can
# never be an orchestrate session - is never gated. macOS stat -f %m.
marker_active() {
  [ -n "${TMUX:-}" ] || return 1
  local key marker mtime now age_h
  key=$(printf '%s' "$TMUX" | tr -c 'A-Za-z0-9' '_')
  marker="$FLOOR_DIR/$key"
  [ -f "$marker" ] || return 1
  mtime=$(stat -f %m "$marker" 2>/dev/null) || return 1
  now=$(date +%s)
  age_h=$(( (now - mtime) / 3600 ))
  [ "$age_h" -lt "$TTL_HOURS" ]
}
```

- [ ] **Step 3: Verify nothing else references the old `MARKER` var**

Run: `cd ~/Developer/claude-kit && grep -n 'MARKER\|ORCHESTRATE_FLOOR_MARKER' orchestrate-guard.sh`
Expected: NO output (the var is fully removed; only `FLOOR_DIR` / `marker` local remain).

- [ ] **Step 4: Shellcheck the guard**

Run: `cd ~/Developer/claude-kit && shellcheck orchestrate-guard.sh`
Expected: clean (no new warnings).

---

## Task 4: Guard - run both new + existing harness cases green

**Files:** none (verification)

- [ ] **Step 1: Run the guard harness**

Run: `cd ~/Developer/claude-kit && python3 test-orchestrate-guard.py`
Expected: PASS - all existing allow/block cases AND the P3-A refcount cases pass; final line "All N allow/block cases + M regressions passed."

- [ ] **Step 2: Run the guard self-test (Tier-1 still hard-denies)**

Run: `cd ~/Developer/claude-kit && bash orchestrate-guard.sh --self-test`
Expected: "orchestrate-guard self-test PASS (Tier-1 push-main blocked)" exit 0.

- [ ] **Step 3: Commit the guard**

```bash
cd ~/Developer/claude-kit
git add orchestrate-guard.sh
git commit -m "feat(orchestrate): \$TMUX-keyed per-session floor marker + 72h TTL (P3-A)"
```

---

## Task 5: Setup harness - convert to the keyed model + add arm/down/GC/foreign cases (failing first)

**Files:**
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-setup.py:41-53` (`run`), `:135,150-172` (up/down marker tests)

- [ ] **Step 1: Teach `run()` and helpers about the keyed dir**

At the top of `test-orchestrate-setup.py`, after the imports, add:
```python
import re

def _key(tmux):
    return re.sub(r'[^A-Za-z0-9]', '_', tmux)

TEST_TMUX = "/tmp/tmux-test,1,0"
```
The existing `run(...)` already sets `env["TMUX"] = "/tmp/tmux-test,1,0"` when `tmux=True` (line 46) - leave that; it equals `TEST_TMUX`.

- [ ] **Step 2: Replace the Task-5 up scaffolding override (line 135)**

Replace:
```python
        upov = dict(ov); upov.update({"ORCHESTRATE_ARTIFACT_DIR": art, "ORCHESTRATE_FLOOR_MARKER": os.path.join(td, "m.active")})
```
with:
```python
        floor_dir = os.path.join(td, "floor.d")
        upov = dict(ov); upov.update({"ORCHESTRATE_ARTIFACT_DIR": art, "ORCHESTRATE_FLOOR_DIR": floor_dir})
```

- [ ] **Step 3: Replace the Task-6 arm + Task-7 down blocks (lines 149-172)**

Replace from the `# Task 6: up arms marker + armed self-test` comment through the end of the Task-7 down checks with:
```python
        # Task 6: up arms THIS session's keyed marker + armed self-test
        marker = os.path.join(floor_dir, _key(TEST_TMUX))
        upov2 = dict(upov)
        rc, out = run(["up", "--team", "demo", "--repo", repo], env_overrides=upov2)
        check("up arms the keyed marker on success", rc == 0 and os.path.exists(marker) and "SESSION ARMED" in out)
        check("marker has a header", "team: demo" in open(marker).read())
        check("marker records tmux", "tmux:" in open(marker).read())
        os.remove(marker)
        # up refuses to arm when $TMUX is empty (cannot key a keyless session)
        rc, out = run(["up", "--team", "demo", "--repo", repo], env_overrides=upov2, tmux=False)
        # doctor already hard-fails without tmux; assert it never armed a marker.
        check("up without \$TMUX never arms a marker", not os.path.exists(marker))
        openguard = os.path.join(td, "openguard.sh"); write_stub_guard(openguard, selftest_rc=0)
        open(openguard, "w").write("#!/usr/bin/env bash\n[ \"$1\" = \"--self-test\" ] && exit 0\nexit 0\n")
        os.chmod(openguard, 0o755)
        failov = dict(upov2); failov["ORCHESTRATE_GUARD"] = openguard
        rc, out = run(["up", "--team", "demo", "--repo", repo], env_overrides=failov)
        check("fail-open guard -> up ABORTS (rc1)", rc == 1 and "failing open" in out)
        check("fail-open -> keyed marker REMOVED (no half-armed session)", not os.path.exists(marker))

        # Task 7: down removes ONLY this session's key, LEAVES foreign, GCs stale tombstones
        os.makedirs(floor_dir, exist_ok=True)
        open(marker, "w").write("x")                                   # my fresh marker
        foreign = os.path.join(floor_dir, _key("/tmp/tmux-501/other,9,9"))
        open(foreign, "w").write("y")                                  # another live session
        stale = os.path.join(floor_dir, _key("/tmp/tmux-501/dead,8,8"))
        open(stale, "w").write("z")                                    # a crashed-session tombstone
        old = __import__("time").time() - 200 * 3600                   # > 72h
        os.utime(stale, (old, old))
        downov = {"ORCHESTRATE_FLOOR_DIR": floor_dir}
        rc, out = run(["down"], env_overrides=downov)                  # runs with TEST_TMUX
        check("down removes my keyed marker", rc == 0 and not os.path.exists(marker) and "marker removed" in out)
        check("down LEAVES a foreign live marker", os.path.exists(foreign))
        check("down GCs a stale tombstone", not os.path.exists(stale))
        rc, out = run(["down"], env_overrides=downov)
        check("down is idempotent (my marker already gone)", rc == 0 and "already disarmed" in out)
        check("down prints the teardown checklist", "shutdown_request" in out and "TeamDelete" in out)
```

- [ ] **Step 4: Run the setup harness to verify it FAILS**

Run: `cd ~/Developer/claude-kit && python3 test-orchestrate-setup.py`
Expected: FAIL - the current setup uses `ORCHESTRATE_FLOOR_MARKER` (now unset) so it arms `~/.claude/orchestrate-floor.active` (not the keyed path), and `down` doesn't GC. Proves the tests drive the setup change. (If `up`'s doctor blocks earlier on a missing piece, fix the test fixture, not the assertion.)

- [ ] **Step 5: Commit the failing setup harness**

```bash
cd ~/Developer/claude-kit
git add test-orchestrate-setup.py
git commit -m "test(orchestrate): drive setup to \$TMUX-keyed marker + down GC (P3-A, failing)"
```

---

## Task 6: Setup - keyed `arm_marker`, env, and `down` GC

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-setup.py:21` (var), `:176-179` (`arm_marker`), `:226-228` (`cmd_up` arm/remove), `:251-263` (`cmd_down`)

- [ ] **Step 1: Replace the MARKER var (line 21)**

Replace:
```python
MARKER = os.environ.get("ORCHESTRATE_FLOOR_MARKER", os.path.join(HOME, ".claude", "orchestrate-floor.active"))
```
with:
```python
FLOOR_DIR = os.environ.get("ORCHESTRATE_FLOOR_DIR", os.path.join(HOME, ".claude", "orchestrate-floor.d"))
TTL_HOURS = int(os.environ.get("ORCHESTRATE_FLOOR_TTL_HOURS", "72"))


def _session_key():
    """This session's marker key, mirroring the guard's sanitization EXACTLY.
    Returns None when $TMUX is empty (a non-tmux session is never an orchestrate
    session and must never arm/own a marker)."""
    import re
    tmux = os.environ.get("TMUX", "")
    if not tmux:
        return None
    return re.sub(r'[^A-Za-z0-9]', '_', tmux)


def _marker_path():
    key = _session_key()
    return os.path.join(FLOOR_DIR, key) if key else None
```
Add `import re` at module top if you prefer it there instead of inside `_session_key`.

- [ ] **Step 2: Rewrite `arm_marker` (lines 176-179)**

Replace:
```python
def arm_marker(team, repo, head):
    os.makedirs(os.path.dirname(MARKER) or ".", exist_ok=True)
    with open(MARKER, "w") as f:
        f.write(f"orchestrate session\nteam: {team}\nstarted: {_now_iso()}\nrepo: {repo}\nhead: {head}\n")
```
with:
```python
def arm_marker(team, repo, head):
    """Arm THIS session's keyed marker. Refuses without $TMUX (cannot key it)."""
    path = _marker_path()
    if not path:
        raise RuntimeError("cannot arm the floor without $TMUX (no session key)")
    os.makedirs(FLOOR_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write(f"orchestrate session\nteam: {team}\nstarted: {_now_iso()}\n"
                f"repo: {repo}\nhead: {head}\ntmux: {os.environ.get('TMUX', '')}\n")
    return path
```

- [ ] **Step 3: Update `cmd_up` arm + fail-path removal (lines 226-228)**

Replace:
```python
    arm_marker(args.team, args.repo, head)
    if not armed_self_test():
        os.remove(MARKER)
```
with:
```python
    marker_path = arm_marker(args.team, args.repo, head)
    if not armed_self_test():
        os.remove(marker_path)
```
Also update the `print(f"\nup: SESSION ARMED." ...)` block that referenced `{MARKER}` (line ~237) to `{marker_path}`.

- [ ] **Step 4: Rewrite `cmd_down` with GC (lines 251-263)**

Replace:
```python
def cmd_down(args):
    existed = os.path.exists(MARKER)
    try:
        os.remove(MARKER)
    except FileNotFoundError:
        pass
    if existed:
        print(TEARDOWN_CHECKLIST)
    else:
        checklist_body = TEARDOWN_CHECKLIST.split("\n", 1)[1]
        print("down: no marker present (already disarmed).")
        print(checklist_body)
    return 0
```
with:
```python
def _gc_stale_tombstones():
    """Best-effort: remove markers in FLOOR_DIR older than TTL. Never fatal."""
    import time
    cutoff = time.time() - TTL_HOURS * 3600
    try:
        entries = os.listdir(FLOOR_DIR)
    except OSError:
        return
    for name in entries:
        p = os.path.join(FLOOR_DIR, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def cmd_down(args):
    path = _marker_path()
    existed = bool(path) and os.path.exists(path)
    if path:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    _gc_stale_tombstones()
    if existed:
        print(TEARDOWN_CHECKLIST)
    else:
        checklist_body = TEARDOWN_CHECKLIST.split("\n", 1)[1]
        print("down: no marker present (already disarmed).")
        print(checklist_body)
    return 0
```

- [ ] **Step 5: Verify no stragglers reference the old MARKER var**

Run: `cd ~/Developer/claude-kit && grep -n 'MARKER\|ORCHESTRATE_FLOOR_MARKER' orchestrate-setup.py`
Expected: NO output (fully migrated to FLOOR_DIR / `_marker_path`).

---

## Task 7: Setup - run harness green + full regression

**Files:** none (verification)

- [ ] **Step 1: Run the setup harness**

Run: `cd ~/Developer/claude-kit && python3 test-orchestrate-setup.py`
Expected: PASS - "All harness checks passed." including the new arm/down/GC/foreign/empty-$TMUX checks.

- [ ] **Step 2: Run the guard harness again (no cross-regression)**

Run: `cd ~/Developer/claude-kit && python3 test-orchestrate-guard.py`
Expected: PASS (still green from Task 4).

- [ ] **Step 3: Run setup's own armed self-test path end to end**

Run: `cd ~/Developer/claude-kit && shellcheck orchestrate-guard.sh && python3 -c "import ast; ast.parse(open('orchestrate-setup.py').read()); print('setup parses')"`
Expected: shellcheck clean + "setup parses".

- [ ] **Step 4: Commit the setup change**

```bash
cd ~/Developer/claude-kit
git add orchestrate-setup.py test-orchestrate-setup.py
git commit -m "feat(orchestrate): \$TMUX-keyed marker arm/down + stale-tombstone GC (P3-A)"
```

---

## Task 8: Key-contract parity check (guard bash == setup python)

**Files:**
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-setup.py` (add a parity assertion in `main()` before the final print)

Guards against silent sanitization drift between the two implementations.

- [ ] **Step 1: Add the parity check**

In `main()` (outside the `with tempfile...` block is fine, or at its end), add:
```python
    # Key-contract: the guard's bash sanitization must equal setup's python one.
    sample = "/tmp/tmux-501/default,12345,0"
    bash_key = subprocess.run(
        ["bash", "-c", "printf '%s' \"$1\" | tr -c 'A-Za-z0-9' '_'", "_", sample],
        capture_output=True, text=True, timeout=5).stdout
    check("key-contract: bash tr == python re.sub", bash_key == _key(sample))
```

- [ ] **Step 2: Run the setup harness**

Run: `cd ~/Developer/claude-kit && python3 test-orchestrate-setup.py`
Expected: PASS including "key-contract: bash tr == python re.sub".

- [ ] **Step 3: Commit**

```bash
cd ~/Developer/claude-kit
git add test-orchestrate-setup.py
git commit -m "test(orchestrate): assert guard/setup key sanitization parity (P3-A)"
```

---

## Task 9: Adversarial convergence (engage-ralph-loop) - the end gate

**Files:** whatever the critics surface (guard / setup / harness)

- [ ] **Step 1: Run the engage-ralph-loop**

Follow `~/Developer/cc-orchestrator/engage-ralph-loop.md`: dispatch parallel READ-ONLY hostile critics against `orchestrate-guard.sh` + `orchestrate-setup.py` + both harnesses, hunting (a) honest-path bypass spellings of the keyed gate, (b) false-positives (a solo session wrongly gated), (c) sanitization-drift / key-collision edge cases, (d) GC removing a fresh foreign marker. Loop until K=2 consecutive dry rounds (no new finding). Apply fixes as their own TDD cycle (failing case -> fix -> green) and re-run BOTH harnesses after each.

- [ ] **Step 2: Final green gate**

Run: `cd ~/Developer/claude-kit && python3 test-orchestrate-guard.py && python3 test-orchestrate-setup.py && bash orchestrate-guard.sh --self-test && shellcheck orchestrate-guard.sh`
Expected: both harnesses pass, self-test PASS, shellcheck clean.

- [ ] **Step 3: Update the docs + checkpoint**

- Mark P3-A done in `~/.claude/skills/orchestrate/ROADMAP-phase3.md` and the `SESSION-STATE.md` banner (note: marker is now `$TMUX`-keyed dir + 72h TTL; single-file `*.active` retired).
- Update `~/.claude/skills/orchestrate/SKILL.md` + `DESIGN-deterministic-floor.md` references to the marker path/semantics (single `*.active` file -> keyed `orchestrate-floor.d/<key>`).
- Commit docs in claude-kit where they live there; the skill-dir docs are plain files (no git).

- [ ] **Step 4: Stop and report to the maintainer**

Do NOT push the gist or open anything. Report: harnesses green, critic dry, files touched, and that deploy is by the existing symlink (no action needed). Await the maintainer's go for any push.

---

## Notes for the implementer

- `~/Developer/claude-kit` IS a git repo; commit there freely (local). The Stop-hook auto-syncs claude-kit to the gist - do NOT manually push.
- The guard is symlinked into `~/.claude/scripts/orchestrate-guard.sh`, so edits to the claude-kit copy are LIVE immediately for the running session's hook on next load. Be aware the live hook inspects your Bash command lines - keep trigger substrings out of them (use the harness's stdin-fed payloads).
- macOS `stat -f %m` and `tr -c` are assumed (this is a darwin-only tool).
