# Deterministic Floor (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `orchestrate` deterministic floor: one PreToolUse `Bash` guard that hard-denies push-to-main/force/no-verify always and merge while an orchestrate session marker is active, proven by a test harness, then wire it in and apply the SKILL/schema updates.

**Architecture:** A single bash guard script (`orchestrate-guard.sh`) reads the Bash command from the hook payload (stdin JSON, `$TOOL_INPUT` env fallback, fail-open on empty), runs Tier-1 hard denies then marker-gated Tier-2 merge denies then the existing `prep-pr-ok` advisory gate, exiting 2 to block / 0 to allow. A Python harness (`test-orchestrate-guard.py`) drives it through both input channels and both marker states. The guard is wired into `~/.claude/settings.json`, replacing the existing inline push hook.

**Tech Stack:** bash + `jq` + coreutils `stat` (guard); python3 stdlib `subprocess`/`json`/`tempfile` (harness). No new dependencies, no binaries. Design spec: `~/.claude/skills/orchestrate/DESIGN-deterministic-floor.md`.

**Threat model (bounds the work):** honest-but-misaligned bot on the obvious command path. Catch the natural command spellings robustly; do NOT chase adversarial evasion (aliases, `$(...)`, base64). See spec "Threat model" + "Known honest-path limitations."

---

## File Structure

### Created (authored in `~/Developer/claude-kit`, the gist = canonical source)
- `orchestrate-guard.sh` - the PreToolUse `Bash` deny authority. **Symlinked** into `~/.claude/scripts/orchestrate-guard.sh` (the gist file stays canonical; matches the existing `pr-watch.sh`/`safe-push.sh` symlink convention - no copy to drift). settings.json references the `~/.claude/scripts/` symlink path.
- `test-orchestrate-guard.py` - the proof harness. Stays in claude-kit ONLY (dev/test artifact, run from the gist; it resolves the guard via its own directory, and is not referenced by settings.json, so it is NOT symlinked).

### Modified
- `~/.claude/settings.json` - replace the inline `PreToolUse.Bash` push hook with a call to the guard. USER-APPROVED diff; needs one CC restart. (Not a git repo; applied, not committed.)
- `~/.claude/skills/orchestrate/templates/stack.schema.json` - make `head_sha` required.
- `~/.claude/skills/orchestrate/SKILL.md` - compose change (delegate inner loop to `subagent-driven-development`) + reference the guard/marker.
- `~/.claude/skills/orchestrate/templates/required-permissions.md` - flip "add DENY hooks (TODO)" to "the guard provides them."
- `~/.claude/skills/orchestrate/REVIEW-FINDINGS.md` - mark the CRITICAL deny-hook items "closed by deterministic floor."

### Runtime (not install artifacts)
- `~/.claude/orchestrate-floor.active` - the marker. Created/removed by hand (phase 1) or the phase-2 setup script. The guard only reads it.

### Conventions
- Commits land in `~/Developer/claude-kit` (the gist is a git repo). `~/.claude/` edits (settings.json, SKILL.md, templates) are applied but not git-committed (those dirs are not repos).
- All paths below are absolute. `CK=~/Developer/claude-kit`.

---

## Task 1: Harness scaffold + guard input-reading (fail-open)

**Files:**
- Create: `~/Developer/cc-orchestrator/orchestrate-guard.sh`
- Create: `~/Developer/cc-orchestrator/test-orchestrate-guard.py`

- [ ] **Step 1: Write the guard skeleton (input read + fail-open, allow everything else)**

Create `~/Developer/cc-orchestrator/orchestrate-guard.sh`:

```bash
#!/usr/bin/env bash
# orchestrate-guard.sh - single PreToolUse Bash deny authority for the orchestrate floor.
# Exit 2 = block (stderr reason). Exit 0 = allow. Fails OPEN on any internal error.
# Spec: ~/.claude/skills/orchestrate/DESIGN-deterministic-floor.md
# NO `set -e`: a grep no-match returns 1 and is normal control flow here.
set -u

MARKER="${ORCHESTRATE_FLOOR_MARKER:-$HOME/.claude/orchestrate-floor.active}"
TTL_HOURS="${ORCHESTRATE_FLOOR_TTL_HOURS:-24}"

# --- read the command: stdin JSON first, then $TOOL_INPUT env, else fail OPEN ---
cmd=""
stdin_json=""
if [ ! -t 0 ]; then
  stdin_json=$(cat 2>/dev/null)
fi
if [ -n "$stdin_json" ]; then
  cmd=$(printf '%s' "$stdin_json" | jq -r '.tool_input.command // empty' 2>/dev/null)
fi
if [ -z "$cmd" ] && [ -n "${TOOL_INPUT:-}" ]; then
  cmd=$(printf '%s' "$TOOL_INPUT" | jq -r '.command // empty' 2>/dev/null)
fi
# Fail OPEN on empty read - never block on no signal.
[ -z "$cmd" ] && exit 0

# (checks added in later tasks)

exit 0
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x ~/Developer/cc-orchestrator/orchestrate-guard.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Write the harness with the runner infrastructure + the empty-read cases**

Create `~/Developer/cc-orchestrator/test-orchestrate-guard.py`:

```python
#!/usr/bin/env python3
"""Proof harness for orchestrate-guard.sh.

Runs each case through BOTH input channels (stdin JSON, $TOOL_INPUT env) and the
specified marker state, asserting the guard's exit code (2=block, 0=allow).
Run: python3 test-orchestrate-guard.py
"""
import json
import os
import subprocess
import sys
import tempfile

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orchestrate-guard.sh")


def run_guard(command, *, marker_active, channel):
    """Invoke the guard. Returns exit code. channel in {'stdin','env'}.
    command=None means 'send no command at all' (empty-read case)."""
    with tempfile.TemporaryDirectory() as td:
        marker = os.path.join(td, "orchestrate-floor.active")
        if marker_active:
            open(marker, "w").close()  # fresh mtime
        env = dict(os.environ)
        env["ORCHESTRATE_FLOOR_MARKER"] = marker
        env["ORCHESTRATE_FLOOR_TTL_HOURS"] = "24"
        env.pop("TOOL_INPUT", None)
        stdin_data = ""
        if command is not None:
            payload = {"tool_name": "Bash", "tool_input": {"command": command}}
            if channel == "stdin":
                stdin_data = json.dumps(payload)
            elif channel == "env":
                env["TOOL_INPUT"] = json.dumps({"command": command})
        p = subprocess.run([GUARD], input=stdin_data, env=env,
                           capture_output=True, text=True)
        return p.returncode


# Case table: (label, command, marker_active, expected_exit)
# command=None => empty-read case (no stdin, no env).
CASES = [
    ("empty-read fails open (no stdin/env)", None, False, 0),
]

FAILS = []


def check(label, command, marker_active, expected):
    channels = ["stdin", "env"] if command is not None else ["stdin"]
    for ch in channels:
        rc = run_guard(command, marker_active=marker_active, channel=ch)
        status = "ok" if rc == expected else "FAIL"
        if rc != expected:
            FAILS.append(f"{label} [{ch}]: expected {expected}, got {rc}")
        print(f"  [{status}] {label} [{ch}] -> exit {rc} (want {expected})")


def main():
    for label, command, marker_active, expected in CASES:
        check(label, command, marker_active, expected)
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print(f"All {sum(1 for _ in CASES)} case-groups passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the harness to verify the scaffold passes**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-guard.py`
Expected: PASS - `All 1 case-groups passed.` (the empty-read case fails open = exit 0).

- [ ] **Step 5: Commit**

```bash
git -C ~/Developer/claude-kit add orchestrate-guard.sh test-orchestrate-guard.py
git -C ~/Developer/claude-kit commit -m "feat(orchestrate): guard skeleton + harness scaffold (fail-open input read)"
```

---

## Task 2: Tier-1 push-to-main deny

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-guard.sh`
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-guard.py`

- [ ] **Step 1: Add the push-main cases to the harness CASES table**

In `test-orchestrate-guard.py`, replace the `CASES = [ ... ]` list with:

```python
CASES = [
    ("empty-read fails open (no stdin/env)", None, False, 0),
    # Tier-1 push-main: blocked ALWAYS (marker irrelevant)
    ("git push origin main", "git push origin main", False, 2),
    ("git -C wt push origin main", "git -C ../wt push origin main", False, 2),
    ("git push origin HEAD:main (refspec)", "git push origin HEAD:main", False, 2),
    ("safe-push.sh main", "scripts/safe-push.sh main", False, 2),
    ("push master", "git push origin master", False, 2),
    # false-positive guards: substrings / other-branch must NOT block
    ("branch named maintenance allowed", "git push origin maintenance", False, 0),
    ("branch named domain allowed", "git push origin domain", False, 0),
    ("feature branch push (advisory only, has override)",
     "git push origin feat # prep-pr-ok", False, 0),
]
```

Note: the plain feature push WITHOUT `# prep-pr-ok` is intentionally deferred to Task 5 (the advisory gate); here we use the override so it resolves to allow.

- [ ] **Step 2: Run the harness to verify the new block cases FAIL**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-guard.py`
Expected: FAIL - the `git push origin main` etc. cases report `expected 2, got 0` (guard still allows everything).

- [ ] **Step 3: Implement Tier-1 push-main in the guard**

In `orchestrate-guard.sh`, replace the `# (checks added in later tasks)` line with:

```bash
# --- matchers (honest-path; whole-word, separator-aware) -------------------
looks_like_git_push() {
  printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_-])git([[:space:]]|$)' \
    && printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])push([[:space:]]|$)'
}
looks_like_safe_push() {
  printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_-])safe-push(\.sh)?([[:space:]]|$)'
}
is_push() { looks_like_git_push || looks_like_safe_push; }

# main/master as a push DESTINATION: whole word, boundary = start/space/colon
# (colon catches HEAD:main; slash deliberately excluded so feature/main is NOT a
# false-positive - refs/heads/main is a non-obvious form, branch-protection backstop).
has_main_dest() {
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]:])(main|master)([[:space:]]|$)'
}

# --- (1) Tier-1 hard denies (ALWAYS) --------------------------------------
if is_push && has_main_dest; then
  echo "BLOCKED: refusing to push main/master from Claude. Never allowed; if you (the human) truly intend it, run it yourself via the ! prefix or the GitHub UI." >&2
  exit 2
fi
```

(Leave the trailing `exit 0` at the end of the file.)

- [ ] **Step 4: Run the harness to verify push-main blocks and false-positives stay allowed**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-guard.py`
Expected: PASS - push-main cases exit 2; `maintenance`/`domain`/override-push exit 0.

- [ ] **Step 5: Commit**

```bash
git -C ~/Developer/claude-kit add orchestrate-guard.sh test-orchestrate-guard.py
git -C ~/Developer/claude-kit commit -m "feat(orchestrate): Tier-1 push-to-main deny (whole-word, refspec, git -C)"
```

---

## Task 3: Tier-1 force + no-verify deny

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-guard.sh`
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-guard.py`

- [ ] **Step 1: Add force/no-verify cases to CASES**

Append to the `CASES` list (before the closing `]`):

```python
    # Tier-1 force / no-verify: blocked ALWAYS
    ("bare --force push", "git push --force origin feat", False, 2),
    ("-f push", "git push -f origin feat", False, 2),
    ("safe-push --force", "scripts/safe-push.sh feat --force", False, 2),
    ("--no-verify push", "git push --no-verify origin feat", False, 2),
    # --force-with-lease must be ALLOWED (substring trap)
    ("--force-with-lease allowed", "git push --force-with-lease origin feat # prep-pr-ok", False, 0),
    # non-push --force must NOT block (gated by is_push)
    ("git clean --force allowed", "git clean --force", False, 0),
```

- [ ] **Step 2: Run the harness to verify the force/no-verify block cases FAIL**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-guard.py`
Expected: FAIL - the bare-force / `-f` / `--no-verify` cases report `expected 2, got 0`.

- [ ] **Step 3: Implement force + no-verify in the guard**

In `orchestrate-guard.sh`, add these matchers immediately after the `has_main_dest()` function:

```bash
# bare --force or -f, but NOT --force-with-lease (substring trap)
has_bare_force() {
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])(--force([[:space:]]|$)|-f([[:space:]]|$))'
}
has_no_verify() {
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])--no-verify([[:space:]]|$)'
}
```

Then, inside the `# --- (1) Tier-1 hard denies` block, AFTER the push-main `if`, add:

```bash
if is_push && has_bare_force; then
  echo "BLOCKED: refusing a non-lease force push from Claude. Use --force-with-lease, or run it yourself via ! if truly intended." >&2
  exit 2
fi
if is_push && has_no_verify; then
  echo "BLOCKED: refusing --no-verify push (skips the pre-push gate). Run the gate, or use ! if truly intended." >&2
  exit 2
fi
```

- [ ] **Step 4: Run the harness to verify force/no-verify block and lease/clean stay allowed**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-guard.py`
Expected: PASS - bare-force/`-f`/`--no-verify` exit 2; `--force-with-lease` and `git clean --force` exit 0.

- [ ] **Step 5: Commit**

```bash
git -C ~/Developer/claude-kit add orchestrate-guard.sh test-orchestrate-guard.py
git -C ~/Developer/claude-kit commit -m "feat(orchestrate): Tier-1 bare-force + no-verify deny (lease/clean allowed)"
```

---

## Task 4: Tier-2 merge deny (marker-gated)

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-guard.sh`
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-guard.py`

- [ ] **Step 1: Add merge cases (both marker states) to CASES**

Append to the `CASES` list:

```python
    # Tier-2 merge: blocked ONLY when marker active
    ("gh pr merge (marker active)", "gh pr merge 1868", True, 2),
    ("gh pr merge --auto (active)", "gh pr merge --auto 1868", True, 2),
    ("gh -R o/r pr merge (active)", "gh -R o/r pr merge 1868", True, 2),
    ("merge-by-API PUT (active)", "gh api -X PUT repos/o/r/pulls/1/merge", True, 2),
    ("merge-by-API --method PUT (active)", "gh api --method PUT repos/o/r/pulls/1/merge", True, 2),
    ("merge-by-API field-implies-POST (active)",
     "gh api repos/o/r/pulls/1/merge -f merge_method=squash", True, 2),
    # marker ABSENT -> merge allowed (solo /merge-pr untouched)
    ("gh pr merge (marker absent) allowed", "gh pr merge 1868", False, 0),
    ("merge-by-API PUT (absent) allowed", "gh api -X PUT repos/o/r/pulls/1/merge", False, 0),
    # false-positives: must be ALLOWED even with marker active
    ("gh pr create title contains merge (active)",
     'gh pr create --title "merge auth refactor"', True, 0),
    ("gh api GET merge-status check (active)",
     "gh api repos/o/r/pulls/5/merge", True, 0),
    ("CodeQL dismiss -X PATCH (active) allowed",
     "gh api -X PATCH repos/o/r/code-scanning/alerts/5", True, 0),
    ("gh pr view (active) allowed", "gh pr view 1868", True, 0),
```

- [ ] **Step 2: Run the harness to verify the merge block cases FAIL**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-guard.py`
Expected: FAIL - the marker-active merge cases report `expected 2, got 0`.

- [ ] **Step 3: Implement Tier-2 + marker freshness in the guard**

In `orchestrate-guard.sh`, add these matchers after `has_no_verify()`:

```bash
# gh pr merge: needs a 'gh' word AND a 'pr merge' consecutive subcommand
# (tolerates gh global flags like -R o/r between gh and pr; NOT triggered by the
# word 'merge' inside e.g. a PR title, which is 'pr create ... "merge ..."').
is_gh_pr_merge() {
  printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_-])gh([[:space:]]|$)' \
    && printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])pr[[:space:]]+merge([[:space:]]|$)'
}

# merge-by-API: gh + api + a pulls/<n>/merge path AND a mutating method/field.
# A bare GET (no method, no field) is a merge-STATUS check and is allowed.
is_merge_api() {
  printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_-])gh([[:space:]]|$)' || return 1
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])api([[:space:]]|$)' || return 1
  printf '%s' "$cmd" | grep -Eq 'pulls/[0-9]+/merge' || return 1
  printf '%s' "$cmd" | grep -Eq '(-X|--method)[[:space:]]+(PUT|POST)' && return 0
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])(-f|--field|-F|--input|--raw-field)([[:space:]]|$)' && return 0
  return 1
}

# marker present AND fresh (mtime within TTL hours). macOS stat -f %m.
marker_active() {
  [ -f "$MARKER" ] || return 1
  local mtime now age_h
  mtime=$(stat -f %m "$MARKER" 2>/dev/null) || return 1
  now=$(date +%s)
  age_h=$(( (now - mtime) / 3600 ))
  [ "$age_h" -lt "$TTL_HOURS" ]
}
```

Then add the Tier-2 block AFTER the Tier-1 block and BEFORE the trailing `exit 0`:

```bash
# --- (2) Tier-2 merge denies (ONLY if marker active) -----------------------
# Short-circuit: only stat the marker if a merge pattern matched (protects the
# ~5ms always-on budget - no stat on ordinary ls/grep/git-status calls).
if is_gh_pr_merge || is_merge_api; then
  if marker_active; then
    echo "BLOCKED: merge is human during an orchestrate session. Use ! gh pr merge ... (your own shell) or the GitHub UI." >&2
    exit 2
  fi
fi
```

- [ ] **Step 4: Run the harness to verify merge gating works in both marker states**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-guard.py`
Expected: PASS - merge blocks only when marker active; GET status-check, `gh pr create` title, CodeQL `-X PATCH`, and `gh pr view` all allowed.

- [ ] **Step 5: Commit**

```bash
git -C ~/Developer/claude-kit add orchestrate-guard.sh test-orchestrate-guard.py
git -C ~/Developer/claude-kit commit -m "feat(orchestrate): Tier-2 marker-gated merge deny (gh pr merge + mutating merge-by-API)"
```

---

## Task 5: prep-pr-ok advisory gate (feature pushes, LAST)

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-guard.sh`
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-guard.py`

- [ ] **Step 1: Add advisory-gate cases to CASES**

Append to the `CASES` list:

```python
    # prep-pr-ok advisory gate: feature push WITHOUT override is blocked (advisory)
    ("feature push without override blocked", "git push origin feat", False, 2),
    # the override must NEVER bypass a hard deny (evaluation order F18)
    ("push main + prep-pr-ok still blocked", "git push origin main # prep-pr-ok", False, 2),
    # non-push commands always allowed
    ("ls allowed", "ls -la", False, 0),
    ("git status allowed", "git status", False, 0),
```

- [ ] **Step 2: Run the harness to verify the bare feature push case FAILS**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-guard.py`
Expected: FAIL - `git push origin feat` reports `expected 2, got 0` (no advisory gate yet).

- [ ] **Step 3: Implement the advisory gate as the LAST check**

In `orchestrate-guard.sh`, replace the trailing `exit 0` (the final line) with:

```bash
# --- (3) prep-pr-ok advisory gate (feature pushes), LAST -------------------
# The override is checked LAST and can ONLY satisfy this advisory gate - it can
# never reach a hard deny above (so `git push main # prep-pr-ok` stays blocked).
if is_push; then
  if printf '%s' "$cmd" | grep -q 'prep-pr-ok'; then
    exit 0
  fi
  echo "BLOCKED: git push must be preceded by /prep-pr (gate + review + squash). If you have already run the gate this turn, append the literal comment # prep-pr-ok to override." >&2
  exit 2
fi

exit 0
```

- [ ] **Step 4: Run the full harness green**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-guard.py`
Expected: PASS - all case-groups pass, including `git push origin main # prep-pr-ok` still blocked (exit 2) and `ls`/`git status` allowed.

- [ ] **Step 5: Commit**

```bash
git -C ~/Developer/claude-kit add orchestrate-guard.sh test-orchestrate-guard.py
git -C ~/Developer/claude-kit commit -m "feat(orchestrate): prep-pr-ok advisory gate (last; override cannot bypass a hard deny)"
```

---

## Task 6: Self-test mode

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-guard.sh`

- [ ] **Step 1: Add the self-test mode at the top of the guard**

In `orchestrate-guard.sh`, immediately after the `TTL_HOURS=...` line (before the input-read block), insert:

```bash
# --- self-test: `orchestrate-guard.sh --self-test` feeds a known Tier-1 block
# payload and asserts exit 2; used by install/setup to catch a silently
# failing-open guard. Prints PASS/FAIL, exits 0 on pass, 1 on fail.
if [ "${1:-}" = "--self-test" ]; then
  rc=0
  printf '%s' '{"tool_name":"Bash","tool_input":{"command":"git push origin main"}}' \
    | "$0" >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 2 ]; then
    echo "orchestrate-guard self-test PASS (Tier-1 push-main blocked)"
    exit 0
  fi
  echo "orchestrate-guard self-test FAIL: expected exit 2, got $rc - guard is failing OPEN" >&2
  exit 1
fi
```

- [ ] **Step 2: Run the self-test to verify it passes**

Run: `~/Developer/cc-orchestrator/orchestrate-guard.sh --self-test`
Expected: prints `orchestrate-guard self-test PASS (Tier-1 push-main blocked)`, exit 0.

- [ ] **Step 3: Confirm the full harness is still green**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-guard.py`
Expected: PASS - all case-groups pass.

- [ ] **Step 4: Commit**

```bash
git -C ~/Developer/claude-kit add orchestrate-guard.sh
git -C ~/Developer/claude-kit commit -m "feat(orchestrate): guard --self-test mode (install-time fail-open canary)"
```

---

## Task 7: Deploy + wire into settings.json (USER-APPROVED) + post-install self-test

**Files:**
- Create/Modify: `~/.claude/scripts/orchestrate-guard.sh` (deploy copy)
- Modify: `~/.claude/settings.json`

- [ ] **Step 1: Symlink the guard into `~/.claude/scripts/`**

The gist file is canonical; `~/.claude/scripts/` holds symlinks to it (matching `pr-watch.sh`/`safe-push.sh`), so the gist stays the single source of truth with no copy to drift. The gist file is already executable from Task 1.

```bash
ln -sf ~/Developer/cc-orchestrator/orchestrate-guard.sh ~/.claude/scripts/orchestrate-guard.sh
ls -l ~/.claude/scripts/orchestrate-guard.sh   # confirm the symlink target
~/.claude/scripts/orchestrate-guard.sh --self-test
```
Expected: the `ls -l` shows `-> /Users/jesse/Developer/cc-orchestrator/orchestrate-guard.sh`; the self-test PASSes resolved through the symlink.

- [ ] **Step 2: Read the current settings.json PreToolUse.Bash hook**

Run: `python3 -c "import json; print(json.dumps(json.load(open('$HOME/.claude/settings.json'))['hooks']['PreToolUse'][0], indent=2))"`
Expected: prints the existing inline `matcher: Bash` push hook (the one this replaces).

- [ ] **Step 3: Present the exact diff and get USER approval**

Per the standing rule "never edit settings.json silently": show the user the exact before/after of the `PreToolUse` `Bash` matcher block. The new block:

```json
{
  "matcher": "Bash",
  "hooks": [
    { "type": "command", "command": "bash \"$HOME/.claude/scripts/orchestrate-guard.sh\"" }
  ]
}
```

This REPLACES the inline push-hook command only. Leave the `Write`/`Edit` secret-file PreToolUse hooks and the `PostToolUse` `gh pr merge` print UNCHANGED.

STOP here until the user approves the diff. Do not write settings.json unattended.

- [ ] **Step 4: Apply the approved edit**

Use the Edit tool on `~/.claude/settings.json` to replace ONLY the inline push-hook `command` string with `bash "$HOME/.claude/scripts/orchestrate-guard.sh"` (keep the `matcher: Bash` wrapper). Verify it still parses:

Run: `python3 -c "import json; json.load(open('$HOME/.claude/settings.json')); print('settings.json valid JSON')"`
Expected: `settings.json valid JSON`.

- [ ] **Step 5: Note the restart + run the post-install self-test**

Tell the user: the new hook loads after a CC restart; the OLD inline hook protects push-main until then. After restart, the live hook is the guard.

Run (verifies the deployed guard regardless of restart): `~/.claude/scripts/orchestrate-guard.sh --self-test`
Expected: self-test PASS. If it does NOT block, STOP and fix before relying on the floor.

(No git commit - `~/.claude/settings.json` is not in a repo.)

---

## Task 8: stack.schema.json - require head_sha

**Files:**
- Modify: `~/.claude/skills/orchestrate/templates/stack.schema.json`

- [ ] **Step 1: Read the current schema**

Run: `cat ~/.claude/skills/orchestrate/templates/stack.schema.json`
Expected: prints the JSON schema for a stack entry; note its `required` array and whether `head_sha` is a defined property.

- [ ] **Step 2: Add head_sha to required (and define it if absent)**

Edit `stack.schema.json` so the per-entry object lists `head_sha` in its `required` array, and has a property definition like:

```json
"head_sha": {
  "type": "string",
  "pattern": "^[0-9a-f]{7,40}$",
  "description": "Pinned commit SHA the maintainer approved for this branch. pr-shipper hard-compares this to the pushed HEAD before gh pr create (enforcement: phase 3)."
}
```

Keep all other properties/required entries intact. If the schema uses `$defs`/`items`, add to the correct entry object.

- [ ] **Step 3: Verify the schema is valid JSON**

Run: `python3 -c "import json; d=json.load(open('$HOME/.claude/skills/orchestrate/templates/stack.schema.json')); print('head_sha required:', 'head_sha' in str(d))"`
Expected: prints `head_sha required: True`.

(No git commit - `~/.claude/skills/` is not a repo. This is a precondition only; the SHA-compare enforcement is phase 3.)

---

## Task 9: SKILL.md compose change + template/findings updates

**Files:**
- Modify: `~/.claude/skills/orchestrate/SKILL.md`
- Modify: `~/.claude/skills/orchestrate/templates/required-permissions.md`
- Modify: `~/.claude/skills/orchestrate/REVIEW-FINDINGS.md`

- [ ] **Step 1: Compose the inner loop onto subagent-driven-development in SKILL.md**

In `SKILL.md`, in the "Convergence loops" / "Lifecycle details" area, edit the IMPLEMENT-loop and implementer/review prose to DELEGATE the single-task inner loop to `subagent-driven-development` (fresh subagent per task + spec-then-quality review), keeping only the orchestrate deltas (PR-blindness, permission charter, persistent-teammate lifecycle, PR pipeline). Add the F22 caveat verbatim in intent:

> The single-task inner loop (one implementer + spec->quality review) follows `subagent-driven-development`. Orchestrate runs these loops in PARALLEL across clusters, which that skill forbids for shared worktrees - safe here ONLY because each implementer is on a DISJOINT worktree, so the shared-file conflict premise does not hold. Do not "fix" the parallelism to comply with the sub-skill.

Do NOT reference `dispatching-parallel-agents`.

- [ ] **Step 2: Reference the guard/marker in the Hard-invariants section of SKILL.md**

Replace the "NO DETERMINISTIC FLOOR YET" hard-invariant bullet with one stating the floor now exists: `~/.claude/scripts/orchestrate-guard.sh` hard-denies push-main/force/no-verify always and marker-gated merge (`~/.claude/orchestrate-floor.active`); generic `gh api -X` stays charter-level; adversarial evasion is out of scope. Point to `DESIGN-deterministic-floor.md`.

- [ ] **Step 3: Update required-permissions.md**

In `templates/required-permissions.md`, change the "Guardrails - prose is NOT a wall; add DENY hooks" section from a TODO to: "the deterministic floor is provided by `orchestrate-guard.sh` (installed as the PreToolUse Bash hook); it hard-denies push-main/force/no-verify and marker-gated merge. Generic `gh api -X` containment remains charter-level." Keep the allow-list entries.

- [ ] **Step 4: Mark the CRITICAL items closed in REVIEW-FINDINGS.md**

In `REVIEW-FINDINGS.md`, annotate the two CRITICAL sections (no-merge / push-main / force bypassable) with: "[CLOSED by deterministic floor - see DESIGN-deterministic-floor.md + orchestrate-guard.sh; merge is now marker-gated-denied, push-main/force/no-verify always-denied]." Leave the HIGH lifecycle items (phase-3 tranche) open.

- [ ] **Step 5: Sanity-check the SKILL.md still reads coherently**

Run: `grep -n "orchestrate-guard\|disjoint worktree\|subagent-driven-development" ~/.claude/skills/orchestrate/SKILL.md`
Expected: the new references are present.

(No git commit - `~/.claude/skills/` is not a repo.)

---

## Task 10: Final verification gate

**Files:** none (verification only)

- [ ] **Step 1: Full harness green from the canonical location**

Run: `python3 ~/Developer/cc-orchestrator/test-orchestrate-guard.py`
Expected: PASS - all case-groups pass.

- [ ] **Step 2: Deployed guard self-test green**

Run: `~/.claude/scripts/orchestrate-guard.sh --self-test`
Expected: self-test PASS.

- [ ] **Step 3: Confirm the gist commits landed**

Run: `git -C ~/Developer/claude-kit log --oneline -6`
Expected: the Task 1-6 commits are present.

- [ ] **Step 4: Hand off to the hostile-critic convergence pass**

The FLOOR gate also requires the hostile-critic pass (spec Testing strategy step 2): parallel read-only critics hunt new honest-path bypass spellings + false-positives until K=2 dry rounds. The COMPOSE gate is the read-through confirming the F22 caveat is present and no sub-skill rule is violated. Report both before declaring phase 1 done. (This is a separate review step, not a code task.)

---

## Self-Review (plan vs spec)

**Spec coverage:**
- Guard (Tier-1 always-on push-main/force/no-verify; Tier-2 marker-gated merge incl. merge-by-API; prep-pr-ok advisory) -> Tasks 2-5. Covered.
- Evaluation order F18 (override cannot bypass hard deny) -> Task 5 step 4 case + ordering. Covered.
- Input channel F23 (stdin + $TOOL_INPUT fallback + empty fail-open) -> Task 1 + harness both-channels. Covered.
- Fail-open F19 + self-test -> Task 1 (empty fail-open), Task 6 (self-test), Task 7 step 5 / Task 10 step 2 (run it). Covered.
- Matching rules F6/F9/F11/F12/F13/F14 -> Tasks 2-4 matchers + harness cases. Covered.
- Whole-word/refspec F + maintenance false-positive -> Task 2 cases. Covered.
- Marker freshness/TTL F20 + global scope F21 -> Task 4 `marker_active` (stat -f %m) + harness temp marker. Covered (scope is documented behavior, not code).
- Short-circuit (no stat on ordinary calls) -> Task 4 ordering (marker_active only inside the merge branch). Covered.
- settings.json wiring + user-approval F27 + restart note -> Task 7. Covered.
- PostToolUse print kept F16 -> Task 7 step 3 ("leave ... UNCHANGED"). Covered.
- head_sha schema precondition F17 -> Task 8. Covered.
- Compose + F22 caveat -> Task 9. Covered.
- required-permissions + REVIEW-FINDINGS updates -> Task 9. Covered.
- Floor gate + compose gate F26 + hostile-critic -> Task 10. Covered.
- Deferred (phase 2 setup script, phase 3 head_sha enforcement + lifecycle) -> correctly NOT in this plan.

**Placeholder scan:** no TBD/TODO; every code step shows complete code; every run step shows the exact command + expected result.

**Type/name consistency:** matcher function names (`is_push`, `has_main_dest`, `has_bare_force`, `has_no_verify`, `is_gh_pr_merge`, `is_merge_api`, `marker_active`) are defined once and reused consistently; env vars `ORCHESTRATE_FLOOR_MARKER` / `ORCHESTRATE_FLOOR_TTL_HOURS` and the marker filename are consistent across guard, harness, and Task 7/8.

**Known accepted limitations (from spec, not bugs):** `refs/heads/main` explicit-ref form and bare-push-on-main-checked-out-worktree evade (branch-protection backstop); Bash-tool-only coverage; adversarial evasion out of scope.
