#!/usr/bin/env python3
"""orchestrate-setup.py - bootstrap/teardown for an orchestrate session.

  doctor [--repo PATH]                          read-only prerequisite check (exit 0 ok, 1 hard-fail)
  up --team NAME --repo PATH [--spacing SEC]    arm a session (scaffold + marker + armed self-test)
  down [--team NAME]                            disarm (rm marker + print teardown checklist)

Design: ~/.claude/skills/orchestrate/design/DESIGN-phase2-setup.md
All real paths come from env vars (see below) so the harness drives temp fixtures.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time


def _int_env(name, default, minimum=None):
    """Parse an int env var, falling back to default on absent-or-malformed (no
    ugly import-time traceback from a typo in an obscure override). When `minimum`
    is given, a parsed value below it is clamped UP to default (not to the minimum),
    so e.g. a TTL of 0 or negative cannot turn the floor into a footgun."""
    try:
        val = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    if minimum is not None and val < minimum:
        return default
    return val


HOME = os.path.expanduser("~")
SETTINGS = os.environ.get("ORCHESTRATE_SETTINGS", os.path.join(HOME, ".claude", "settings.json"))
FLOOR_DIR = os.environ.get("ORCHESTRATE_FLOOR_DIR", os.path.join(HOME, ".claude", "orchestrate-floor.d"))
# minimum=1: a TTL of 0/negative would make _gc_stale_tombstones treat EVERY marker
# (including a live foreign session's fresh one) as stale and delete it - the cardinal
# P3-A cross-session-disarm sin. Clamp back to the default instead.
TTL_HOURS = _int_env("ORCHESTRATE_FLOOR_TTL_HOURS", 72, minimum=1)
GUARD = os.environ.get("ORCHESTRATE_GUARD", os.path.join(HOME, ".claude", "scripts", "orchestrate-guard.sh"))
# The BUNDLED guard ships beside this script (scripts/orchestrate-guard.sh). `configure` copies it
# to the stable GUARD path so a fresh plugin install has a working floor at the path the
# settings.json hook points at (Option A - the floor is settings-resident, never plugin-gated; see
# skills/orchestrate/design/DESIGN-plugin-floor-lifecycle.md). Env-overridable so the harness can point it at a fixture.
BUNDLED_GUARD = os.environ.get("ORCHESTRATE_BUNDLED_GUARD",
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "orchestrate-guard.sh"))
# The WARN-level steering hook (#95) - a SEPARATE script from the deny-floor guard. Deployed to the
# stable STEER path the same Option-A way, and wired as advisory Edit/Write/Bash PreToolUse hooks.
# Advisory + opt-out-able (`configure --no-steer`), so doctor only ever WARNs about it, never FAILs.
STEER = os.environ.get("ORCHESTRATE_STEER", os.path.join(HOME, ".claude", "scripts", "orchestrate-steer.sh"))
BUNDLED_STEER = os.environ.get("ORCHESTRATE_BUNDLED_STEER",
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "orchestrate-steer.sh"))
# Script-relative: under the plugin layout this script lives in scripts/ and the templates ship
# beside the skill at skills/orchestrate/templates/, so resolve them relative to the script
# (realpath resolves any ~/.claude/scripts deploy symlink to the real plugin/repo location). A
# script-relative default works in the worktree, the installed plugin (${CLAUDE_PLUGIN_ROOT}),
# and CI alike - never an absolute ~/.claude path that only exists via a symlink.
TEMPLATES = os.environ.get("ORCHESTRATE_TEMPLATES_DIR", os.path.normpath(
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "skills", "orchestrate", "templates")))
ARTIFACTS = os.environ.get("ORCHESTRATE_ARTIFACT_DIR", "/tmp")

# Deployed PR-lifecycle helpers (#133). The plugin bundles these 9 helpers under scripts/ and is now
# their canonical SOURCE. `configure --apply` deploys them to the stable ~/.claude/scripts/ path
# EXACTLY as it deploys the floor guard (Option A), so leads invoke them through the existing
# Bash(~/.claude/scripts/*.sh *) allow-rule - no per-plugin-version cache drift, portable across
# installs. A deploy also RETIRES the old claude-kit symlinks: it converts a symlink at the dest
# into a real plugin copy (backing the symlink up to <dest>.bak first; never clobbers an unreadable
# regular-file dest blind). Edits are PR-only; deployed copies are derived. Env-overridable so the
# harness can point them at fixtures (mirrors GUARD / BUNDLED_GUARD).
SCRIPTS_DIR = os.environ.get("ORCHESTRATE_SCRIPTS_DIR", os.path.join(HOME, ".claude", "scripts"))
BUNDLED_SCRIPTS_DIR = os.environ.get("ORCHESTRATE_BUNDLED_SCRIPTS_DIR",
    os.path.dirname(os.path.realpath(__file__)))
# The SessionStart `init` advisory (#162) reuses THIS very script. `configure --apply` deploys this
# script to the stable SCRIPTS_DIR path (Option A, exactly like the guard/steer) so the SessionStart
# hook can call a stable-path entry point - and REFRESHES it on every run so a stale shadow-detection
# logic can never persist after a plugin update. The bundled source is this file; the dest is the
# stable path. Env-overridable so the harness can point them at fixtures (mirrors GUARD/BUNDLED_GUARD).
BUNDLED_SETUP = os.environ.get("ORCHESTRATE_BUNDLED_SETUP", os.path.realpath(__file__))
SETUP_DEST = os.environ.get("ORCHESTRATE_SETUP_DEST",
    os.path.join(SCRIPTS_DIR, "orchestrate-setup.py"))
HELPER_NAMES = (
    "pr-watch.sh", "pr-unreplied-comments.sh", "pr-read-comments.sh", "reply-comment.sh",
    "resolve-threads.sh", "cleanup-worktree.sh", "patch-coverage.sh", "pr-codeql-autofixes.sh",
    "safe-push.sh", "gate-runner.py", "pre-push-hook.sh",
)
# The _helper_deploy_action results that warrant an actual deploy write (vs. None / informational).
HELPER_DEPLOY_ACTIONS = ("deploy", "refresh", "replace-symlink", "replace-broken-symlink")

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _emit(status, msg):
    print(f"[{status:4}] {msg}")
    return status


def _load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except OSError:
        return None
    except json.JSONDecodeError:
        print(f"WARN: settings.json present but unparseable at {SETTINGS}", file=sys.stderr)
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


# The deterministic-floor hook block, wired into settings.json under hooks.PreToolUse.
# The command points at the STABLE ~/.claude/scripts path (NOT a plugin-relative path):
# the floor is an always-on safety guard, so it must survive plugin enable/disable/update
# (see DESIGN: floor stays settings.json-resident, not plugin-gated). `configure` writes
# this; doctor only verifies + prints it.
GUARD_HOOK_BLOCK = {
    "matcher": "Bash",
    "hooks": [{"type": "command",
               "command": 'bash "$HOME/.claude/scripts/orchestrate-guard.sh"'}],
}
GUARD_HOOK_JSON = ("  Add this to ~/.claude/settings.json under hooks.PreToolUse (doctor only\n"
                   "  verifies + prints; `orchestrate-setup.py configure --apply` writes it for you):\n"
                   "    " + json.dumps(GUARD_HOOK_BLOCK))

# The advisory steering hooks (#95). Three PreToolUse blocks (Edit, Write, Bash) all pointing at the
# stable steer path. SEPARATE from the guard's Bash hook (settings.json runs every matching block, so
# the guard deny and the steer WARN coexist on a Bash call). Advisory + opt-out-able.
# The command is fail-OPEN: `[ -r <steer> ] && bash <steer> || true` so a missing/unreadable deployed
# script returns 0 (never blocks) and emits no per-call "No such file" noise - matching
# orchestrate-steer.sh's "exit 0 ALWAYS" advisory contract. Shared as one constant so the present()
# detection (below) can exact-match the SAME string that gets written.
STEER_HOOK_COMMAND = ('[ -r "$HOME/.claude/scripts/orchestrate-steer.sh" ] && '
                      'bash "$HOME/.claude/scripts/orchestrate-steer.sh" || true')
STEER_HOOK_BLOCKS = [
    {"matcher": m,
     "hooks": [{"type": "command", "command": STEER_HOOK_COMMAND}]}
    for m in ("Edit", "Write", "Bash")
]

# The SessionStart advisory hook (#162). At session start it runs `orchestrate-setup.py init`, a
# READ-ONLY scan that surfaces a `gh pr *` merge-gate-shadow advisory to stdout (injected into the
# session context) and is SILENT when the cascade is clean. SessionStart blocks carry no `matcher`.
# The command is fail-OPEN: `... init 2>/dev/null || true` so any scan/parse/exec error returns 0 and
# emits no stderr noise - a SessionStart hook must NEVER block or fail a session. Shared as one
# constant so the present() idempotency check exact-matches the SAME string configure writes.
SESSION_INIT_HOOK_COMMAND = (
    'python3 "$HOME/.claude/scripts/orchestrate-setup.py" init 2>/dev/null || true')
SESSION_INIT_HOOK_BLOCK = {
    "hooks": [{"type": "command", "command": SESSION_INIT_HOOK_COMMAND}],
}


def _guard_hook_present(settings):
    """True if the deterministic-floor Bash hook is already wired in settings.json."""
    for block in (settings or {}).get("hooks", {}).get("PreToolUse", []):
        if block.get("matcher") == "Bash":
            for h in block.get("hooks", []):
                if "orchestrate-guard.sh" in h.get("command", ""):
                    return True
    return False


def check_guard_wired(settings):
    if _guard_hook_present(settings):
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


def _files_identical(a, b):
    """Byte-compare two files. False if either is missing/unreadable (so a missing dest reads as
    'not identical' -> needs deploy, and a missing source is handled by the caller)."""
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def _guard_deploy_action():
    """What `configure` must do to put the bundled floor guard at the stable GUARD path:
      'missing-source' - the bundled guard is absent (cannot deploy - dev-layout problem);
      'deploy'         - GUARD does not exist yet (fresh install);
      'refresh'        - GUARD exists but differs from the bundled guard (post-update drift);
      None             - GUARD is byte-identical to the bundled guard (nothing to do)."""
    if not os.path.isfile(BUNDLED_GUARD):
        return "missing-source"
    if not os.path.exists(GUARD):
        return "deploy"
    if not _files_identical(BUNDLED_GUARD, GUARD):
        return "refresh"
    return None


def _deploy_guard():
    """Copy the bundled guard to the stable GUARD path and ensure it is executable. The caller has
    already gated on --apply + consent. Returns (ok: bool, message: str)."""
    try:
        os.makedirs(os.path.dirname(GUARD), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(GUARD), prefix=".orch-guard-")
        os.close(fd)
        try:
            shutil.copy2(BUNDLED_GUARD, tmp)
            os.chmod(tmp, os.stat(tmp).st_mode | 0o111)  # ensure u+g+o executable
            os.replace(tmp, GUARD)  # atomic; replaces a symlink itself, never its target
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True, f"deployed the floor guard -> {GUARD}"
    except OSError as e:
        return False, f"FAILED to deploy the floor guard to {GUARD}: {e}"


def check_guard_stale():
    """Doctor check (WARN-level): the deployed guard should match the bundled plugin guard. A
    difference is the post-plugin-update drift case (the bundled source moved ahead of the deployed
    copy) - WARN, never FAIL, and point at `configure --apply`. 'deploy' (no deployed guard yet) is
    already covered by check_guard_wired/healthy; 'missing-source' is a dev-layout issue, not drift."""
    action = _guard_deploy_action()
    if action == "refresh":
        return _emit(WARN, f"deployed guard at {GUARD} is STALE vs the bundled plugin guard - run "
                           "`orchestrate-setup.py configure --apply` to refresh it")
    if action in ("deploy", "missing-source"):
        return _emit(WARN, f"could not compare the deployed guard to the bundled guard ({action})")
    return _emit(PASS, "deployed guard matches the bundled plugin guard")


def _missing_steer_hook_blocks(settings):
    """The STEER_HOOK_BLOCKS not yet wired in settings.json (matched by matcher + steer command)."""
    pre = (settings or {}).get("hooks", {}).get("PreToolUse", [])

    def present(block):
        # Exact (type+command) match against the canonical STEER_HOOK_COMMAND this block carries, so a
        # stale path or a disabled `true # orchestrate-steer.sh` line no longer counts as wired; kept
        # anchored to the same constant written by configure (block["hooks"][0]["command"]).
        expected = block["hooks"][0]["command"]
        for b in pre:
            if b.get("matcher") == block["matcher"]:
                for h in b.get("hooks", []):
                    if h.get("type") == "command" and h.get("command") == expected:
                        return True
        return False
    return [b for b in STEER_HOOK_BLOCKS if not present(b)]


def _steer_deploy_action():
    """What `configure` must do to put the bundled steer script at the stable STEER path. Mirrors
    _guard_deploy_action: 'missing-source' | 'deploy' | 'refresh' | None."""
    if not os.path.isfile(BUNDLED_STEER):
        return "missing-source"
    if not os.path.exists(STEER):
        return "deploy"
    if not _files_identical(BUNDLED_STEER, STEER):
        return "refresh"
    return None


def _deploy_steer():
    """Copy the bundled steer script to the stable STEER path, executable. Caller gated on --apply +
    consent. Atomic temp-then-replace, mirroring _deploy_guard. Returns (ok, message)."""
    try:
        os.makedirs(os.path.dirname(STEER), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STEER), prefix=".orch-steer-")
        os.close(fd)
        try:
            shutil.copy2(BUNDLED_STEER, tmp)
            os.chmod(tmp, os.stat(tmp).st_mode | 0o111)
            os.replace(tmp, STEER)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True, f"deployed the steering hook -> {STEER}"
    except OSError as e:
        return False, f"FAILED to deploy the steering hook to {STEER}: {e}"


def check_steer(settings):
    """Doctor check for the advisory steering hooks (#95). WARN (never FAIL) when the hooks are not
    wired or the deployed steer script is stale/uncomparable - steering is opt-out-able and advisory,
    so its absence is never a hard fail. PASS when all 3 hooks are wired + the deployed steer matches
    the bundled copy. Stays read-only."""
    missing = _missing_steer_hook_blocks(settings)
    action = _steer_deploy_action()
    msgs = []
    if missing:
        msgs.append(f"{len(missing)} advisory steering hook(s) not wired "
                    f"({', '.join(b['matcher'] for b in missing)})")
    if action == "deploy":
        msgs.append(f"deployed steer script missing at {STEER}")
    elif action == "refresh":
        msgs.append(f"deployed steer script at {STEER} is STALE vs the bundled copy")
    elif action == "missing-source":
        msgs.append(f"bundled steer source missing at {BUNDLED_STEER}")
    if msgs:
        return _emit(WARN, "; ".join(msgs) + " - run `orchestrate-setup.py configure --apply` "
                           "(or `--no-steer` to skip the advisory steering)")
    return _emit(PASS, "advisory steering hooks wired + deployed steer matches the bundled copy")


def _session_init_hook_present(settings):
    """True if the SessionStart advisory init hook is already wired in settings.json. Exact
    (type+command) match against the canonical SESSION_INIT_HOOK_COMMAND so a stale/disabled line
    does not count as wired - anchored to the SAME string configure writes. SessionStart blocks
    carry no matcher, so we scan every block's hooks list."""
    # `or {}` / `or []` coerce a valid-JSON-but-null value (e.g. {"hooks": null}) so a malformed
    # settings file is treated as "not wired" rather than crashing this check.
    hooks = (settings or {}).get("hooks") or {}
    for block in (hooks.get("SessionStart") or []):
        for h in ((block or {}).get("hooks") or []):
            if h.get("type") == "command" and h.get("command") == SESSION_INIT_HOOK_COMMAND:
                return True
    return False


def check_session_init_hook(settings):
    """Doctor check for the SessionStart advisory init hook (#162). WARN (never FAIL) when the hook
    is not wired - the SessionStart surfacing is advisory, and its absence does NOT compromise the
    floor. This deliberately does NOT soften or replace doctor's HARD-FAIL on detected shadows via
    check_merge_gate_shadows(): the merge gate is still hard-enforced; this only nudges to wire the
    convenience surfacing. Stays read-only."""
    if _session_init_hook_present(settings):
        return _emit(PASS, "SessionStart shadow-advisory init hook wired")
    return _emit(WARN, "SessionStart shadow-advisory init hook (#162) not wired - run "
                       "`orchestrate-setup.py configure --apply` to wire it (advisory surfacing only; "
                       "the merge-gate HARD-FAIL check is unaffected)")


def _setup_deploy_action():
    """What `configure` must do to put THIS script at the stable SETUP_DEST path so the SessionStart
    hook can call a stable-path entry point. Mirrors _guard_deploy_action, but ALWAYS refreshes when
    the dest differs (no None short-circuit on first run is needed - identical content returns None):
      'missing-source' - BUNDLED_SETUP is absent (should never happen for the running script);
      'deploy'         - SETUP_DEST does not exist yet (fresh install);
      'refresh'        - SETUP_DEST exists but differs from the bundled script (post-update drift);
      None             - SETUP_DEST is byte-identical to the bundled script (nothing to do)."""
    if not os.path.isfile(BUNDLED_SETUP):
        return "missing-source"
    if not os.path.exists(SETUP_DEST):
        return "deploy"
    if not _files_identical(BUNDLED_SETUP, SETUP_DEST):
        return "refresh"
    return None


def _deploy_setup():
    """Copy THIS script to the stable SETUP_DEST path, executable. Caller gated on --apply + consent.
    Atomic temp-then-replace, mirroring _deploy_guard. Refuses a no-op self-copy (source resolves to
    the same realpath as dest) so we never clobber the running file with itself. Returns (ok, msg)."""
    if os.path.realpath(BUNDLED_SETUP) == os.path.realpath(SETUP_DEST):
        return True, f"setup script already at the stable path ({SETUP_DEST}); no self-copy needed"
    try:
        os.makedirs(os.path.dirname(SETUP_DEST), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SETUP_DEST), prefix=".orch-setup-")
        os.close(fd)
        try:
            shutil.copy2(BUNDLED_SETUP, tmp)
            os.chmod(tmp, os.stat(tmp).st_mode | 0o111)
            os.replace(tmp, SETUP_DEST)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True, f"deployed the setup script -> {SETUP_DEST}"
    except OSError as e:
        return False, f"FAILED to deploy the setup script to {SETUP_DEST}: {e}"


def _helper_deploy_action(name):
    """What `configure` must do to put bundled helper `name` at the stable SCRIPTS_DIR path. Mirrors
    _guard_deploy_action, adding claude-kit symlink retirement (#133):
      'missing-source'         - the bundled helper source is absent (dev-layout problem);
      'deploy'                 - the dest does not exist yet (fresh install);
      'replace-broken-symlink' - dest is a symlink whose target is gone (a dead claude-kit link);
      'replace-symlink'        - dest is a symlink (the claude-kit link) - convert to a real copy;
      'unreadable'             - dest is a regular file that exists but cannot be read (never clobber blind);
      'refresh'                - dest is a regular file that differs from the bundled helper (drift);
      None                     - dest is byte-identical to the bundled helper (nothing to do).
    islink is checked BEFORE exists: os.path.exists FOLLOWS the link, so a broken symlink would
    otherwise read as 'deploy' and silently leave the dead claude-kit link in place."""
    src = os.path.join(BUNDLED_SCRIPTS_DIR, name)
    dest = os.path.join(SCRIPTS_DIR, name)
    if not os.path.isfile(src):
        return "missing-source"
    if os.path.islink(dest):
        return "replace-symlink" if os.path.exists(dest) else "replace-broken-symlink"
    if not os.path.exists(dest):
        return "deploy"
    if not os.access(dest, os.R_OK):
        return "unreadable"
    if not _files_identical(src, dest):
        return "refresh"
    return None


def _deploy_helper(name):
    """Deploy bundled helper `name` to the stable SCRIPTS_DIR path. The caller has gated on --apply +
    consent and selected only actionable helpers. Converts a claude-kit symlink into a real copy
    (backing the symlink itself up to <dest>.bak first); refuses an unreadable regular-file dest
    (never clobber blind). Atomic temp-then-replace, mirroring _deploy_guard. Returns (ok, message)."""
    src = os.path.join(BUNDLED_SCRIPTS_DIR, name)
    dest = os.path.join(SCRIPTS_DIR, name)
    action = _helper_deploy_action(name)
    if action == "missing-source":
        return False, f"helper {name}: bundled source missing at {src} - cannot deploy"
    if action == "unreadable":
        return False, f"helper {name}: dest {dest} exists but is unreadable - refusing to clobber blind"
    if action is None:
        return True, f"helper {name}: already current"
    try:
        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        # Retire a claude-kit symlink: move the link itself aside before writing the real copy.
        # os.path.islink (not exists) so a BROKEN link is backed up too, not skipped; os.replace
        # renames the symlink itself, never its target.
        if os.path.islink(dest):
            os.replace(dest, dest + ".bak")
        fd, tmp = tempfile.mkstemp(dir=SCRIPTS_DIR, prefix=".orch-helper-")
        os.close(fd)
        try:
            shutil.copy2(src, tmp)
            os.chmod(tmp, os.stat(tmp).st_mode | 0o111)  # ensure u+g+o executable
            os.replace(tmp, dest)  # atomic
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        verb = {"deploy": "deployed", "refresh": "refreshed (stale)",
                "replace-symlink": "replaced claude-kit symlink for",
                "replace-broken-symlink": "replaced broken symlink for"}.get(action, "deployed")
        return True, f"helper {name}: {verb} -> {dest}"
    except OSError as e:
        return False, f"helper {name}: FAILED to deploy to {dest}: {e}"


def check_helpers_stale():
    """Doctor check (WARN-level): the deployed PR-lifecycle helpers should match the bundled plugin
    copies. Drift (a stale copy, or a not-yet-retired claude-kit symlink) is the post-plugin-update
    case - WARN, never FAIL, and point at `configure --apply`. Mirrors check_guard_stale and stays
    read-only (only _helper_deploy_action, which never writes)."""
    stale, missing, missing_deployed, unreadable = [], [], [], []
    for name in HELPER_NAMES:
        action = _helper_deploy_action(name)
        if action in ("refresh", "replace-symlink", "replace-broken-symlink"):
            stale.append(name)
        elif action == "deploy":
            missing_deployed.append(name)
        elif action == "unreadable":
            unreadable.append(name)
        elif action == "missing-source":
            missing.append(name)
    if missing_deployed:
        return _emit(WARN, "PR-lifecycle helper(s) not yet deployed to the stable path "
                           f"({', '.join(missing_deployed)}) - run `orchestrate-setup.py configure --apply` to deploy")
    if unreadable:
        return _emit(WARN, "deployed helper script(s) exist but are unreadable "
                           f"({', '.join(unreadable)}) - cannot verify; check permissions")
    if stale:
        return _emit(WARN, "deployed helper script(s) STALE vs the bundled plugin copies "
                           f"({', '.join(stale)}) - run `orchestrate-setup.py configure --apply` to refresh")
    if missing:
        return _emit(WARN, "bundled helper source(s) missing "
                           f"({', '.join(missing)}); cannot verify the deployed copies")
    return _emit(PASS, "deployed helper scripts match the bundled plugin copies")


def _emit_helper_warnings(missing, unreadable):
    """Print stderr WARNINGs for helpers that cannot be deployed (missing bundled source, or an
    unreadable dest). Shared by configure's change-preview and its no-op early-return so neither
    path silently swallows the signal."""
    for name in missing:
        print(f"configure: WARNING - bundled helper source for {name} is missing under "
              f"{BUNDLED_SCRIPTS_DIR}; cannot deploy it (the rest still applies).", file=sys.stderr)
    for name in unreadable:
        print(f"configure: WARNING - deployed helper {os.path.join(SCRIPTS_DIR, name)} is unreadable; "
              "refusing to clobber it blind (the rest still applies).", file=sys.stderr)


def check_repo_main(repo):
    """Returns (status, head_sha_or_None)."""
    if not repo:
        return _emit(WARN, "no --repo given; skipping repo/HEAD check"), None
    try:
        head = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=15)
        dirty = subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return _emit(FAIL, f"could not inspect repo {repo}: {e}"), None
    if head.returncode != 0:
        return _emit(FAIL, f"{repo} is not a git repo"), None
    h = head.stdout.strip()
    if dirty.stdout.strip():
        return _emit(WARN, f"{repo} HEAD={h} but working tree is DIRTY (stray files won't block, just a heads-up)"), h
    return _emit(PASS, f"{repo} clean at HEAD={h}"), h


def _normalize_allow_entry(entry):
    """Collapse leading // inside tool parens to a single / (Claude-Code idiom -> real path)."""
    import re
    return re.sub(r'\(//([^)]+)\)', r'(/\1)', entry)


def _missing_allow_entries(settings):
    """Compute the allow-list entries documented in required-permissions.md that are
    ABSENT from settings.json. Returns a sorted list (possibly empty), or None if the
    required-permissions section cannot be read (file missing or no section). Shared by
    `doctor` (check_allowlist) and `configure` so both see the same diff."""
    import re
    allow = set(_normalize_allow_entry(e) for e in (settings or {}).get("permissions", {}).get("allow", []))
    req_file = os.path.join(TEMPLATES, "required-permissions.md")
    try:
        text = open(req_file).read()
    except OSError:
        return None
    # Extract only the "## Needed allow-list entries" section (stop at the next ## heading).
    section_match = re.search(r'^## Needed allow-list entries[^\n]*\n(.*?)(?=^## |\Z)',
                               text, re.MULTILINE | re.DOTALL)
    if not section_match:
        return None
    # DOC CONTRACT (issue #107): every backticked Bash/Write/Edit/Read(...) token in this
    # section that is NOT on a NOTE: line is a PRESCRIBED allow-list entry. Prose that merely
    # NAMES a rule it does NOT prescribe (e.g. "REMOVE any broad `gh api` allow-rule") MUST
    # NOT wrap that rule as a backticked Perm token, or it becomes a phantom "needed" entry
    # that configure --apply would write and doctor would WARN as "missing". The parser stays
    # deliberately simple; the doc honours the contract; test-orchestrate-setup.py pins the
    # exact harvested set so a reintroduced phantom (or a dropped real entry) fails CI.
    needed = set()
    for line in section_match.group(1).splitlines():
        # Skip lines that are commentary/notes (not prescriptive allow-list items).
        if "NOTE:" in line:
            continue
        for entry in re.findall(r"`(Bash\([^`]+\)|Write\([^`]+\)|Edit\([^`]+\)|Read\([^`]+\))`", line):
            needed.add(_normalize_allow_entry(entry))
    return sorted(n for n in needed if n not in allow)


def check_allowlist(settings):
    missing = _missing_allow_entries(settings)
    if missing is None:
        return _emit(WARN, "required-permissions.md missing or has no '## Needed allow-list entries' section; skipping allow-list diff")
    if not missing:
        return _emit(PASS, "allow-list covers the documented bot entries")
    _emit(WARN, f"{len(missing)} allow-list entries from required-permissions.md are MISSING:")
    for m in missing:
        print(f"         {m}")
    print("         (run `orchestrate-setup.py configure --apply` to add these + the floor hook with consent)")
    return WARN


def _derive_repo_slug(repo):
    """Derive the owner/name slug from the repo path's git remote origin URL.

    Handles both SSH and HTTPS GitHub remote forms:
      git@github.com:owner/name.git  ->  owner/name
      https://github.com/owner/name.git  ->  owner/name
      https://github.com/owner/name     ->  owner/name

    Raises SystemExit with a clear error if the remote cannot be resolved or
    parsed (never silently renders a filesystem path into the brief).
    """
    try:
        p = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        raise SystemExit(f"scaffold_artifacts: ABORT - cannot run git to resolve remote "
                         f"for {repo}: {e}") from e
    if p.returncode != 0:
        raise SystemExit(
            f"scaffold_artifacts: ABORT - cannot derive owner/name slug for the pr-shipper brief.\n"
            f"  repo path: {repo}\n"
            f"  `git -C {repo} remote get-url origin` failed (rc={p.returncode}).\n"
            f"  The brief's <REPO> placeholder requires an owner/name slug (e.g. 'owner/repo'), "
            f"not a filesystem path.\n"
            f"  Fix: add an 'origin' remote pointing to the GitHub repo, then re-run `up`."
        )
    url = p.stdout.strip()
    # SCP-SSH: git@github.com:owner/name.git or git@github.com:owner/name
    m = re.match(r"git@[^:]+:([^/]+/[^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    # HTTPS: https://github.com/owner/name.git or https://github.com/owner/name
    m = re.match(r"https?://[^/]+/([^/]+/[^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    # ssh:// URL form: ssh://[user@]host[:port]/owner/name[.git]
    # e.g. ssh://git@github.com/owner/name.git
    #      ssh://git@ssh.github.com:443/owner/name.git
    #      ssh://github.com/owner/name
    m = re.match(r"^ssh://[^/]+/([^/]+/[^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    raise SystemExit(
        f"scaffold_artifacts: ABORT - cannot parse owner/name slug from remote URL.\n"
        f"  repo path: {repo}\n"
        f"  remote origin URL: {url!r}\n"
        f"  Expected SCP-SSH (git@github.com:owner/name.git), HTTPS "
        f"(https://github.com/owner/name), or ssh:// "
        f"(ssh://[user@]host[:port]/owner/name) form.\n"
        f"  Fix: set origin to a GitHub SSH or HTTPS URL, then re-run `up`;\n"
        f"  or use --slug owner/name to supply the slug directly."
    )


def scaffold_artifacts(team, repo, spacing, slug=None):
    # P3-A: all artifacts live under a per-team dir so parallel teams don't clobber each
    # other (stack drops its <team>- prefix - the dir now carries the team identity).
    team_dir = os.path.join(ARTIFACTS, team)
    os.makedirs(team_dir, exist_ok=True)
    stack = os.path.join(team_dir, "stack.json")
    with open(stack, "w") as f:
        json.dump([], f)
    triage = os.path.join(team_dir, "pr-triage")
    os.makedirs(triage, exist_ok=True)
    os.makedirs(os.path.join(team_dir, "adv-review"), exist_ok=True)
    # Planner (lookahead) role artifact (#11): the read-only planner overwrites this DRAFT
    # proposal; the lead ratifies. Seed it empty so a consumer always finds valid JSON.
    planner_dir = os.path.join(team_dir, "planner")
    os.makedirs(planner_dir, exist_ok=True)
    with open(os.path.join(planner_dir, "proposed.json"), "w") as f:
        json.dump({"flags": []}, f)
    brief_out = os.path.join(team_dir, "pr-shipper-brief.md")
    src = os.path.join(TEMPLATES, "pr-shipper-brief.md")
    try:
        body = open(src).read()
    except OSError:
        body = "# pr-shipper brief\n(template not found; fill manually)\n"
    # Derive the owner/name slug from the repo's git remote - the brief's <REPO> placeholders
    # require a slug (gh + pr-watch.sh both reject a filesystem path). Fail loudly if the
    # remote cannot be resolved so the operator never gets a silently-broken brief.
    # When a slug override is provided (via --slug on up), skip remote derivation entirely.
    if slug is None:
        slug = _derive_repo_slug(repo)
    # Substitute the real template tokens: <REPO>, <STACK>, <SPACING_MIN>.
    # The team is captured in the injected header comment; the template has no team token.
    body = (body.replace("<REPO>", slug)
                .replace("<STACK>", stack)
                .replace("<SPACING_MIN>", str(spacing)))
    header = f"<!-- rendered by orchestrate-setup.py: team={team} repo={repo} slug={slug} spacing={spacing}min stack={stack} -->\n"
    with open(brief_out, "w") as f:
        f.write(header + body)
    return stack, triage, brief_out


# The stillwater profile config keys `up` captures from its env and persists for the
# session so `orchestrate-resources.py allocate` can read them without re-exporting each
# time. These are PATHS (keyfile path, music dir, source DB), NOT secret material - the
# encryption KEY itself stays a 0600 file beside the DB, never stored here; only its path is.
PROFILE_ENV_KEYS = ("ORCHESTRATE_STILLWATER_KEYFILE", "ORCHESTRATE_STILLWATER_MUSIC",
                    "ORCHESTRATE_STILLWATER_DB")


def write_profile_env(team_dir):
    """Persist whichever PROFILE_ENV_KEYS are set in this env to <team_dir>/profile.env as
    eval-able `export K=V` lines, created 0600. Only set keys are written; absent keys are
    simply omitted (no hard-fail if none are set - the generic profile needs none)."""
    set_keys = [(k, os.environ[k]) for k in PROFILE_ENV_KEYS if os.environ.get(k, "")]
    path = os.path.join(team_dir, "profile.env")
    # 0600 from birth (these are paths, not secrets, but no reason to make them world-readable).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# stillwater profile config captured at `up` time (PATHS, not secret material;\n"
                "# the encryption key stays a 0600 file beside the DB - only its path is here).\n")
        for k, v in set_keys:
            f.write(f"export {k}={v}\n")
    os.chmod(path, 0o600)  # enforce even if it pre-existed with looser perms
    return path


def _atomic_write_json(path, data):
    """Serialize `data` to `path` atomically: write a temp file in the SAME directory, fsync it,
    then os.replace (an atomic same-filesystem rename on POSIX). A partial/failed write can never
    leave `path` truncated - a reader or a crash sees either the prior complete file or the new
    complete one. Preserves the existing file mode when `path` already exists. Callers still back
    up first; this is the durability guarantee on top of that. Raises OSError on failure (the temp
    file is cleaned up first), so existing `except OSError` write-error handling still applies."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".orch-tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except OSError:
            pass  # path may not exist yet (first write); mkstemp's 0600 is acceptable then
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# First char must be alphanumeric: blocks path-escape (`.`/`..`/`/`) AND leading-dash
# names like `-rf` that would later be mis-parsed as flags by shell tools touching the dir.
TEAM_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


def _validate_team(team):
    """A team name becomes a /tmp path component, so reject anything that could escape
    the artifact dir or break os.path.join. SystemExit -> clean non-zero, no traceback."""
    if not team or not TEAM_RE.match(team):
        raise SystemExit(f"up: ABORT - invalid --team {team!r}; must start with a letter or "
                         "digit and contain only [A-Za-z0-9._-] (no '/', no leading '.'/'-').")


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_key():
    """This session's marker key, mirroring the guard's sanitization EXACTLY.
    Returns None when $TMUX is empty (a non-tmux session is never an orchestrate
    session and must never arm/own a marker)."""
    tmux = os.environ.get("TMUX", "")
    if not tmux:
        return None
    # Byte-mode to match the guard's `LC_ALL=C tr -c 'A-Za-z0-9' '_'` (each non-alnum
    # BYTE -> '_'), so the two sides key identically regardless of locale. surrogateescape
    # round-trips the original env bytes; the result is pure ASCII alnum/underscore.
    raw = tmux.encode("utf-8", "surrogateescape")
    return re.sub(rb'[^A-Za-z0-9]', b'_', raw).decode("ascii")


def _marker_path():
    key = _session_key()
    return os.path.join(FLOOR_DIR, key) if key else None


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


def _feed_guard(command):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    p = subprocess.run([GUARD], input=payload, capture_output=True, text=True, timeout=15)
    return p.returncode


def armed_self_test():
    """With the marker armed, the guard must HARD-DENY (exit 2) ALL THREE:
      - Tier-1 push-main, AND
      - Tier-2 merge-by-API (`gh api ... pulls/N/merge` mutating), AND
      - Tier-2 `gh pr merge` CLI (`is_pr_merge`, #105).
    All payloads are built here and fed on stdin, so the live hook never sees a trigger
    on the command line (the orchestrate test-driving rule). skills/orchestrate/design/DESIGN-deterministic-floor.md
    (section 'Cases (minimum)') claims this assertion for all three paths -- the code
    matches the doc."""
    t1 = _feed_guard("git push origin main")
    t2 = _feed_guard("gh api -X PUT repos/o/r/pulls/5/merge")
    t3 = _feed_guard("gh pr " + "merge 5")
    return t1 == 2 and t2 == 2 and t3 == 2


# The literal command the Tier-2 merge gate keeps OUT of the allow-list so Claude Code
# prompts the human. Assembled from pieces so this source line carries no trigger string
# for the live PreToolUse Bash hook (which inspects command lines, not file contents).
MERGE_TARGET = "gh pr " + "merge"


# Regex (string body) matching the FAMILY of commands the Tier-2 gate must keep gated:
# the bare merge command, optionally followed by an argument boundary and any args. Built
# from MERGE_TARGET so this source line carries no trigger triple.
_MERGE_FAMILY_RE = re.compile("^" + re.escape(MERGE_TARGET) + r"(\s.*)?$", re.DOTALL)


def _rule_to_regex(pattern):
    """Translate a Bash allow specifier (the inner part of `Bash(<pattern>)`) into a regex
    body describing the set of command strings it GRANTS, faithful to Claude Code permission
    semantics (https://code.claude.com/docs/en/permissions):

      - a trailing ` *` (space-star) or `:*` is a BOUNDARY wildcard: it grants the literal
        prefix followed by a word/argument boundary (whitespace) and then anything. So
        `gh pr:*` grants `gh pr <anything>` but NOT `gh project` (no boundary after `gh pr`).
      - any OTHER `*` -- leading, infix, or a trailing `*` with no preceding space -- is a
        PLAIN glob translated to `.*` (matches any run, including spaces). So `gh pr*` is a
        plain prefix that matches `gh pr merge`, and `gh*merge` matches across a space.
      - a wildcard-free specifier matches the command exactly, or that command followed by
        an argument boundary (CC prefix-matches a bare specifier against the same command
        with trailing args), modeled as `<literal>(\\s.*)?`.

    The specifier is matched LITERALLY: a quoted or space-padded specifier (e.g. `"gh pr"`
    or ` gh pr:*`) carries those characters into the regex, so it cannot match a real
    command (which carries no quotes/leading space). No trimming or unquoting is done -- that
    is intentional, so a quoted/padded rule never spuriously shadows the gate."""
    if pattern.endswith(" *") or pattern.endswith(":*"):
        prefix = re.escape(pattern[:-2]).replace(r"\*", ".*")
        # literal prefix, then an argument boundary (whitespace) + anything, or nothing
        return prefix + r"(\s.*)?"
    if "*" in pattern:
        return re.escape(pattern).replace(r"\*", ".*")
    # wildcard-free: exact command, or that command followed by an argument boundary
    return re.escape(pattern) + r"(\s.*)?"


def _is_merge_scoped(pattern):
    """True iff the specifier's language is a SUBSET of the merge family - i.e. it grants
    only `gh pr merge` and its own args/flags, nothing broader.

    The three sanctioned forms (and their wildcard-free with-args variants) are accepted:
      - exact bare:          `gh pr merge`        (grants merge or merge + args)
      - boundary-star:       `gh pr merge *`      (grants merge + at least one arg)
      - boundary-colon-star: `gh pr merge:*`      (same, colon boundary form)
      - exact with args:     `gh pr merge --flag` (grants only that one invocation)

    Any other form - a plain-glob prefix like `gh pr merg*`, a broader boundary like
    `gh pr:*`, or anything with a wildcard not in the recognised tail positions - is NOT
    merge-scoped and is left to the intersection check in _merge_rule_shadows."""
    if pattern == MERGE_TARGET:
        return True
    if pattern.endswith(" *") or pattern.endswith(":*"):
        return pattern[:-2] == MERGE_TARGET
    # wildcard-free specifier longer than the bare target: merge-specific args are fine
    if "*" not in pattern:
        return pattern.startswith(MERGE_TARGET + " ")
    # any other wildcard form (plain-glob, infix, leading) - not merge-scoped
    return False


def _merge_rule_shadows(pattern):
    """Does this Bash allow PATTERN shadow the merge gate?

    A pattern shadows iff:
      (a) its language INTERSECTS the merge family (it can grant some merge invocation), AND
      (b) its language is NOT a SUBSET of the merge family (it grants more than merge).

    Condition (b) is the new reconciliation: a merge-SCOPED rule (one that grants merge and
    nothing beyond merge's own args) is NO LONGER a shadow. Rationale: the floor deny
    outranks it while the orchestrate marker is active (bot still cannot merge), and in a
    solo/no-marker session it is the human's own /merge-pr working as intended. Broader
    rules - `gh pr *`, `gh pr:*`, `gh *`, plain-glob prefixes, etc. - still shadow because
    they over-grant every other subcommand and are the always-allow footgun.

    Intersection test (sound, both directions):
      - the rule's regex matches the bare target, or the target with one trailing arg
        (catches every prefix/boundary/glob rule that grants merge-with-args), AND
      - the family regex matches the rule's literal stem (globs emptied) - catches a
        rule LONGER than the target, e.g. `gh pr merge --squash` (more specific merge
        invocation that the family still covers)."""
    if _is_merge_scoped(pattern):
        return False
    rule_re = re.compile("^" + _rule_to_regex(pattern) + "$", re.DOTALL)
    if rule_re.match(MERGE_TARGET) or rule_re.match(MERGE_TARGET + " x"):
        return True
    # Reverse direction: is the rule itself a (more specific) merge invocation? Empty the
    # globs to get a concrete command the rule grants, and test family membership.
    rule_stem = pattern.replace("*", "")
    return bool(_MERGE_FAMILY_RE.match(rule_stem))


def _cascade_files():
    """The settings files to scan, in cascade order. An ORCHESTRATE_SETTINGS_FILES env
    override (colon-separated paths) replaces the default cascade so the harness can point
    at temp fixtures. The default cascade is: user settings (+ .local) then project
    settings (+ .local); the project root is ORCHESTRATE_PROJECT_DIR, else the git toplevel
    of CWD, else project files are skipped."""
    override = os.environ.get("ORCHESTRATE_SETTINGS_FILES")
    if override is not None:
        return [p for p in override.split(":") if p]
    files = [os.path.join(HOME, ".claude", "settings.json"),
             os.path.join(HOME, ".claude", "settings.local.json")]
    project = os.environ.get("ORCHESTRATE_PROJECT_DIR")
    if not project:
        try:
            r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=15)
            project = r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            project = None
    if project:
        files.append(os.path.join(project, ".claude", "settings.json"))
        files.append(os.path.join(project, ".claude", "settings.local.json"))
    return files


def _scan_merge_gate_shadows():
    """Scan the settings cascade for allow-rules that shadow the merge gate.
    Returns (shadows, errors):
      shadows = list of (file, rule_string) where a Bash allow-rule grants the squash-merge.
      errors  = list of (file, message) for files that exist but are unparseable.
    Missing files are skipped silently; an absent/non-list `permissions.allow` is no rules."""
    shadows, errors = [], []
    for path in _cascade_files():
        if not os.path.exists(path):
            continue
        # Fault isolation: any problem with ONE file is recorded and the scan continues to
        # the remaining files. A miss here = a shadow in a later file is silently hidden and
        # the merge gate is defeated, so a single odd file must never abort the cascade scan.
        try:
            try:
                with open(path) as f:
                    data = json.load(f)
            except OSError as e:
                errors.append((path, f"unreadable: {e}"))
                continue
            except json.JSONDecodeError as e:
                errors.append((path, f"unparseable JSON: {e}"))
                continue
            # A non-object top level (e.g. a JSON array) or a non-dict `permissions`
            # (e.g. null) grants no rules -- it is NOT an error and must NOT crash.
            if not isinstance(data, dict):
                continue
            perms = data.get("permissions")
            if not isinstance(perms, dict):
                continue
            allow = perms.get("allow")
            if not isinstance(allow, list):
                continue
            for rule in allow:
                if not isinstance(rule, str):
                    continue
                m = re.fullmatch(r"Bash\((.*)\)", rule)
                if not m:
                    continue  # non-Bash rules (Read/Write/Edit) cannot grant a Bash command
                if _merge_rule_shadows(m.group(1)):
                    shadows.append((path, rule))
        except Exception as e:  # noqa: BLE001 - last-resort guard so one odd file never aborts the cascade
            errors.append((path, f"scan error: {e}"))
            continue
    return shadows, errors


def check_merge_gate_shadows():
    """Doctor check: a single allow-rule anywhere in the cascade that matches the
    squash-merge command silently re-grants merge and DEFEATS the human-in-the-loop gate.
    Treat any shadow (or an unparseable cascade file) as a HARD doctor failure."""
    shadows, errors = _scan_merge_gate_shadows()
    if not shadows and not errors:
        return _emit(PASS, f"no settings-cascade rule shadows the {MERGE_TARGET!r} gate")
    if errors:
        for path, msg in errors:
            _emit(FAIL, f"settings file in cascade could not be scanned: {path} ({msg})")
    if shadows:
        _emit(FAIL, f"{len(shadows)} allow-rule(s) SHADOW the {MERGE_TARGET!r} gate "
                    "(any one silently re-grants merge via a BROADER grant - remove/narrow them):")
        for path, rule in shadows:
            print(f"         {rule}   in {path}")
        _GH_PR = MERGE_TARGET.rsplit(" ", 1)[0]
        print(f"         Fix: narrow each to the non-merge subcommands (e.g. {_GH_PR} "
              "view/diff/checks/create/list), never a blanket prefix that covers 'merge'.")
        print(f"         Note: an explicit merge-only entry (Bash({MERGE_TARGET}), "
              f"Bash({MERGE_TARGET} *), or Bash({MERGE_TARGET}:*)) is the SANCTIONED form "
              "and is NOT a shadow -- the floor deny backstops it in a marker session.")
    return FAIL


# The non-merge `gh pr` subcommands a narrowed blanket is replaced WITH. `merge` is
# DELIBERATELY OMITTED here because the explicit merge-scoped entry (`Bash(gh pr merge *)`)
# comes via a SEPARATE path: _missing_allow_entries parses it from required-permissions.md
# and configure adds it alongside the other missing allow-list entries (not via this narrow
# path). The `gh pr merge` CLI is gated by the FLOOR (marker-gated hard-deny, #105). The
# entry FORMS mirror required-permissions.md exactly: `Bash(gh pr <sub> *)`. The `gh pr`
# stem is assembled from MERGE_TARGET so this source line carries no trigger string for
# the live PreToolUse Bash hook. Keep this list in lockstep with required-permissions.md.
_GH_PR_STEM = MERGE_TARGET.rsplit(" ", 1)[0]  # "gh pr" (no 'merge'), built off MERGE_TARGET
_NON_MERGE_GH_PR_SUBCOMMANDS = ("view", "diff", "checks", "create", "list",
                                "status", "edit", "ready", "comment")
_NARROWED_GH_PR_ENTRIES = [f"Bash({_GH_PR_STEM} {sub} *)" for sub in _NON_MERGE_GH_PR_SUBCOMMANDS]

# The exact blanket specifiers (inner part of `Bash(<spec>)`) that scope the WHOLE `gh pr`
# space and are therefore safe to narrow to the enumerated non-merge subcommands without
# changing scope outside `gh pr`. A broader shadow (`gh *`, `gh:*`, `*`, or anything else)
# is NOT auto-narrowed - narrowing whole-`gh` to `gh pr` would silently drop unrelated
# scope, so configure SURFACES those for explicit human resolution instead.
_NARROWABLE_GH_PR_SPECS = frozenset({_GH_PR_STEM + " *", _GH_PR_STEM + ":*"})


def _shadow_is_narrowable(rule):
    """Is this `Bash(...)` shadow rule a blanket scoped exactly to the `gh pr` space
    (`Bash(gh pr *)` / `Bash(gh pr:*)`) that we may safely narrow to the enumerated
    non-merge subcommands? Any other shadow (broader `gh *`/`gh:*`/`*`, an exact merge
    rule, a glob) is NOT narrowable and must be surfaced for human resolution."""
    m = re.fullmatch(r"Bash\((.*)\)", rule)
    return bool(m) and m.group(1) in _NARROWABLE_GH_PR_SPECS


def _narrow_allow_list(allow):
    """Return a NEW allow-list with every narrowable `gh pr` blanket replaced by the
    enumerated non-merge subcommands (merge omitted), preserving the order of the
    untouched rules and de-duplicating the added entries. Returns (new_allow, changed):
    `changed` is False when nothing was narrowable (so the caller writes nothing)."""
    out, changed = [], False
    # Filter to strings before hashing into a set: a settings file may carry a non-string
    # (e.g. a dict) in permissions.allow, and set() over it raises TypeError: unhashable type.
    # Mirrors the downstream isinstance(rule, str) gate so configure safe-skips, never crashes.
    existing = {r for r in allow if isinstance(r, str)}
    added = set()
    for rule in allow:
        if isinstance(rule, str) and _shadow_is_narrowable(rule):
            changed = True
            for entry in _NARROWED_GH_PR_ENTRIES:
                # add each enumerated subcommand once, skipping any already present
                if entry not in existing and entry not in added:
                    out.append(entry); added.add(entry)
            continue  # drop the blanket itself
        out.append(rule)
    return out, changed


# Well-formed Slack channel-id format: a leading uppercase letter + 5+ uppercase
# alphanumeric chars (min 6 total). The leading-letter requirement excludes
# all-digit strings while covering all known prefixes (C, G, D, W). See
# skills/orchestrate/design/DESIGN-maintainer-channel.md (doctor check, F3-B-4).
SLACK_CHANNEL_RE = re.compile(r"[A-Z][A-Z0-9]{5,}")


def check_slack_channel():
    """Doctor check (WARN-level, optional): the maintainer Slack channel is
    optional and FORMAT-only. The stdlib doctor subprocess cannot reach MCP
    tools, so reachability is OUT of scope (validated at runtime by the lead's
    first slack_send_message; degrades per D4). This NEVER returns FAIL - the
    channel is optional and must not block doctor/up. See
    skills/orchestrate/design/DESIGN-maintainer-channel.md (check_slack_channel, F1-3 / F2-C-3)."""
    channel = os.environ.get("ORCHESTRATE_SLACK_CHANNEL", "")
    if not channel:
        return _emit(WARN, "ORCHESTRATE_SLACK_CHANNEL not set (channel optional; terminal-only mode)")
    if not SLACK_CHANNEL_RE.fullmatch(channel):
        return _emit(WARN, f"ORCHESTRATE_SLACK_CHANNEL={channel!r} is not a well-formed Slack channel id "
                           "(expected [A-Z][A-Z0-9]{5,}); terminal-only mode")
    return _emit(PASS, f"ORCHESTRATE_SLACK_CHANNEL={channel} is a well-formed Slack channel id")


def check_slack_bot_user_id():
    """Doctor check (WARN-level, optional): the Slack SERVICE-IDENTITY bot user_id (#89).
    When set, the lead's self-echo keys on author==<bot user_id> (F6-C-3, robust) instead
    of the text sentinel. Optional + FORMAT-only (a Slack user id shares the channel id
    shape [A-Z][A-Z0-9]{5,}); NEVER FAIL - unset is fine (text-sentinel fallback, F6-C-2).
    Reachability/identity is validated at runtime by a rendered read-back, not here."""
    bot = os.environ.get("ORCHESTRATE_SLACK_BOT_USER_ID", "")
    if not bot:
        return _emit(WARN, "ORCHESTRATE_SLACK_BOT_USER_ID not set (optional; self-echo uses the text sentinel)")
    if not SLACK_CHANNEL_RE.fullmatch(bot):
        return _emit(WARN, f"ORCHESTRATE_SLACK_BOT_USER_ID={bot!r} is not a well-formed Slack user id "
                           "(expected [A-Z][A-Z0-9]{5,}); self-echo falls back to the text sentinel")
    return _emit(PASS, f"ORCHESTRATE_SLACK_BOT_USER_ID={bot} is a well-formed Slack user id")


def cmd_doctor(args):
    settings = _load_settings()
    repo_status, _head = check_repo_main(getattr(args, "repo", None))
    results = [check_agent_teams(settings), check_tmux(),
               check_guard_wired(settings), check_guard_healthy(), check_guard_stale(),
               check_helpers_stale(), check_steer(settings),
               check_session_init_hook(settings),
               repo_status, check_allowlist(settings),
               check_merge_gate_shadows(), check_slack_channel(), check_slack_bot_user_id()]
    hard_fail = any(s == FAIL for s in results)
    print()
    print("doctor: HARD-FAIL (fix the FAIL lines above before `up`)" if hard_fail else "doctor: ok (no hard fail)")
    return 1 if hard_fail else 0


def _check_stale_guard_at_up():
    """Emit a prominent, visually-distinct warning block to stderr when the deployed guard
    is STALE ('refresh') or uncomparable vs the bundled guard. Reuses _guard_deploy_action()
    so the comparison logic is NOT duplicated.

    Design choice: LOUD but NON-FATAL - `up` continues; exit code is unchanged.
    Alternatives considered:
      - Non-zero exit / abort: would block legitimate solo work when the deployed guard is
        merely stale (still functional) and the operator just hasn't run configure yet.
        Rejected: too aggressive for a runtime that fails OPEN by design.
      - Require --allow-stale-guard flag to acknowledge: adds friction without adding safety
        (the guard is still functional; the stale case is a post-update drift, not a security
        gap; the floor's OPEN-fail posture means a broken deployed guard is survivable).
        Rejected: over-engineering for a loud advisory.
    Chosen: loud-non-fatal - the operator sees the remedy and can decide to run configure
    before arming, but `up` is not bricked by a stale-but-functional guard."""
    action = _guard_deploy_action()
    if action not in ("refresh", "missing-source"):
        return  # guard is current (None) or simply not yet deployed (doctor already caught 'deploy')
    if action == "refresh":
        detail = f"deployed guard at {GUARD!r} differs from the bundled plugin guard"
    else:
        detail = f"cannot compare: bundled guard source not found at {BUNDLED_GUARD!r}"
    border = "=" * 70
    print(file=sys.stderr)
    print(border, file=sys.stderr)
    print("WARNING: STALE FLOOR GUARD", file=sys.stderr)
    print(f"  {detail}.", file=sys.stderr)
    print("  The session will still arm, but the deployed guard may be out of date.", file=sys.stderr)
    print("  REMEDY (in order):", file=sys.stderr)
    print("    1. Run:    orchestrate-setup.py configure --apply", file=sys.stderr)
    print("    2. RESTART each open Claude Code session (the PreToolUse hook loads the", file=sys.stderr)
    print("               guard at session start; a restart is required, not a reboot).", file=sys.stderr)
    print(border, file=sys.stderr)
    print(file=sys.stderr)


def cmd_up(args):
    _validate_team(args.team)  # reject a path-unsafe team name before doing any work
    # Validate --slug early (before doctor) so a typo exits immediately with a clear error.
    slug_override = getattr(args, "slug", None)
    if slug_override is not None:
        if not re.match(r"^[^/]+/[^/]+$", slug_override):
            print(f"up: ABORT - --slug must be in owner/name form (e.g. owner/name), "
                  f"got: {slug_override!r}", file=sys.stderr)
            return 1
        print(f"up: using --slug override: {slug_override}")
    print("Pre-flight doctor:")
    rc = cmd_doctor(args)
    if rc != 0:
        print("\nup: ABORT - doctor reported a hard failure; fix it and re-run.", file=sys.stderr)
        return rc
    # Loud (non-fatal) warning when the deployed guard is stale vs the bundled plugin guard.
    # doctor already WARNs at check_guard_stale(); this adds a more prominent block with the
    # specific remedy so the operator cannot miss it at `up` time.
    _check_stale_guard_at_up()
    # Reuse HEAD from doctor's check_repo_main; add timeout + empty-tolerant fallback.
    _repo_status, head = check_repo_main(args.repo)
    if not head:
        head_result = subprocess.run(["git", "-C", args.repo, "rev-parse", "--short", "HEAD"],
                                     capture_output=True, text=True, timeout=15)
        head = head_result.stdout.strip() if head_result.returncode == 0 else "unknown"
    stack, triage, brief = scaffold_artifacts(args.team, args.repo, args.spacing,
                                              slug=slug_override)
    # F2(c): capture the stillwater profile config from the env now so allocate can read it
    # from the team dir for the rest of the session (no re-exporting the 3 paths each time).
    write_profile_env(os.path.join(ARTIFACTS, args.team))
    try:
        marker_path = arm_marker(args.team, args.repo, head)
    except OSError as e:
        # FLOOR_DIR unwritable or not a directory: abort cleanly (safe direction - not
        # armed) instead of a raw traceback.
        print(f"\nup: ABORT - cannot arm the floor marker under {FLOOR_DIR}: {e}", file=sys.stderr)
        return 1
    # Treat an exception from the self-test (e.g. the guard binary vanished between
    # doctor and here) the same as a failed test: never leave a half-armed orphan marker.
    try:
        self_test_ok = armed_self_test()
    except (OSError, subprocess.SubprocessError):
        # OSError = guard binary vanished/unrunnable; SubprocessError (incl. TimeoutExpired)
        # = guard hung. Either way: never leave a half-armed orphan marker.
        self_test_ok = False
    if not self_test_ok:
        try:
            os.remove(marker_path)
        except OSError:
            pass
        # The scaffolded /tmp artifacts are intentionally LEFT here: they are inert without
        # a marker file, so they cannot trigger any floor action - and they are useful for
        # post-mortem debugging of why the self-test failed.
        print("\nup: ABORT - armed self-test FAILED: with the marker armed the guard did not "
              "hard-deny ALL of: push-main, merge-by-API, and the gh pr merge CLI (all must exit 2). "
              "The floor is failing open. Marker REMOVED. Fix the guard before standing up a "
              "session.", file=sys.stderr)
        return 1
    print(f"\nup: SESSION ARMED."
          f"\n  marker:   {marker_path}"
          f"\n  team dir: {os.path.join(ARTIFACTS, args.team)}"
          f"\n  stack:    {stack}\n  triage:   {triage}\n  brief:    {brief}"
          f"\n  Merges are now HUMAN-ONLY via the ! prefix until `down`.")
    return 0


TEARDOWN_CHECKLIST = """down: marker removed (Tier-2 merge-gating OFF).
Team teardown is the lead's job (tool calls, not this script):
  1. SendMessage shutdown_request to EACH teammate ONCE - do NOT re-send; a re-send
     re-wakes an idle agent rather than killing it faster.
  2. WAIT for each "terminated" notice BEFORE removing that teammate's worktree -
     removing the cwd of a still-alive teammate wedges it on a dead working directory.
     The team is implicit: there is no TeamDelete step (Anthropic removed
     TeamCreate/TeamDelete); waiting for every "terminated" notice IS the teardown.
  3. THEN worktrees: LEAVE any that still have open PRs; clean the rest with
     `make remove-worktree`. (down WARNS above about any worktree with uncommitted work
     - commit before removing it.) An Agent-tool worker that never sends "terminated":
     dismiss it via FleetView or let it reap at session end - do not block teardown on it."""


def _gc_stale_tombstones():
    """Best-effort: remove markers in FLOOR_DIR older than TTL. Never fatal."""
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


def _release_session_resources(team):
    """Best-effort: release this session's resource leases. Never fatal."""
    if not team:
        return
    resources = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orchestrate-resources.py")
    if not os.path.exists(resources):
        return
    try:
        out = subprocess.run([sys.executable, resources, "list", "--session", team, "--json"],
                             capture_output=True, text=True, timeout=15)
        leases = json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else []
        for lease in leases:
            subprocess.run([sys.executable, resources, "release", "--lease", lease["id"]],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass  # teardown hygiene must never crash down


def _marker_repo(path):
    """Read the `repo:` path recorded in this session's marker. None on any problem
    (no marker, unreadable, or no repo line) - teardown must never crash to read it."""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("repo:"):
                    return line.split(":", 1)[1].strip() or None
    except (OSError, ValueError):
        return None
    return None


def _dirty_worktrees(repo):
    """Pre-teardown safety scan (#25): list (path, n_uncommitted) for every worktree of
    `repo` (the primary checkout AND each linked worktree) that has uncommitted work.
    Best-effort and FAIL-OPEN: returns [] on a missing repo or ANY git error, so the
    scan can never crash `down` (which keeps its 'best-effort teardown' contract - the
    maintainer chose WARN-and-proceed over a hard refuse, #25). HEAD is intentionally NOT
    compared to the marker's arm-time SHA: the team commits freely, so HEAD is designed
    to advance, and a SHA-equality gate would fire on every legitimate teardown."""
    if not repo:
        return []
    try:
        wl = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if wl.returncode != 0:
        return []
    paths = [ln[len("worktree "):].strip()
             for ln in wl.stdout.splitlines() if ln.startswith("worktree ")]
    dirty = []
    for p in paths:
        try:
            st = subprocess.run(["git", "-C", p, "status", "--porcelain"],
                                capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue  # a vanished/unreadable worktree is not our teardown's problem
        if st.returncode == 0 and st.stdout.strip():
            dirty.append((p, len(st.stdout.strip().splitlines())))
    return dirty


def _warn_dirty_worktrees(repo):
    """Emit a prominent WARN (never a refusal) listing worktrees with uncommitted work,
    so the lead does NOT `make remove-worktree` them before committing."""
    dirty = _dirty_worktrees(repo)
    if not dirty:
        return
    print("\ndown: WARNING - uncommitted work in these worktrees; commit it BEFORE "
          "`make remove-worktree` (a worktree kept for an open PR is expected - leave it):",
          file=sys.stderr)
    for p, n in dirty:
        print(f"         {p}  ({n} uncommitted path(s))", file=sys.stderr)


def cmd_down(args):
    path = _marker_path()
    existed = bool(path) and os.path.exists(path)
    # Read the repo BEFORE removing the marker; scan its worktrees so a dirty tree is
    # surfaced before the lead cleans worktrees (#25, warn-and-proceed).
    repo = _marker_repo(path) if existed else None
    if path:
        try:
            os.remove(path)
        except OSError:
            # FileNotFoundError (already disarmed) or NotADirectoryError (FLOOR_DIR is a
            # file) etc.: down is best-effort teardown, never crash on a bad marker path.
            pass
    _gc_stale_tombstones()
    _release_session_resources(getattr(args, "team", None))
    _warn_dirty_worktrees(repo)
    if existed:
        print(TEARDOWN_CHECKLIST)
    else:
        checklist_body = TEARDOWN_CHECKLIST.split("\n", 1)[1]
        print("down: no marker present (already disarmed).")
        print(checklist_body)
    return 0


def cmd_init(args):
    """SessionStart advisory (#162). STRICTLY READ-ONLY: run the SAME read-only merge-gate-shadow
    scan doctor uses (_scan_merge_gate_shadows - never forked) and, if a shadow is found, print a
    single-line advisory to stdout (surfaced into the session context). SILENT when the cascade is
    clean (zero stdout - no per-session-start noise). Exit 0 on EVERY path, including a scan/parse
    error (logged to stderr, no stdout), so a SessionStart hook can never block or fail a session.
    Writes/mutates NOTHING - doctor/configure stay the only writers; this never touches settings."""
    try:
        shadows, _errors = _scan_merge_gate_shadows()
    except Exception as e:  # noqa: BLE001 - fail-open: a SessionStart hook must never block a session
        print(f"orchestrate-setup init: shadow scan failed ({e}); skipping advisory.", file=sys.stderr)
        return 0
    # Deliberately ignore _errors here: an unparseable cascade file is doctor's HARD-FAIL concern,
    # not a SessionStart surface (init is WARN-level advisory only and must stay silent/clean unless
    # an actual shadow grant is present). doctor remains the authoritative parse-error check.
    if shadows:
        gh_pr = MERGE_TARGET.rsplit(" ", 1)[0]  # "gh pr"; assembled off MERGE_TARGET, no trigger string
        print(f"blanket `{gh_pr} *` shadows the merge gate - run "
              "`orchestrate-setup.py configure --apply` to narrow it")
    return 0


def _load_settings_for_write():
    """Read SETTINGS for a write-merge. Returns (settings_dict_or_None, status):
      'ok'          - parsed JSON dict
      'missing'     - file absent; returns {} (configure may create it)
      'unparseable' - file present but invalid JSON; returns None (NEVER clobber)
      'unreadable'  - OSError; returns None
    Distinguishing 'missing' from 'unparseable' is the safety property: configure
    creates a fresh settings.json but refuses to overwrite an unparseable one."""
    try:
        with open(SETTINGS) as f:
            return json.load(f), "ok"
    except FileNotFoundError:
        return {}, "missing"
    except json.JSONDecodeError:
        return None, "unparseable"
    except OSError:
        return None, "unreadable"


def _narrow_shadow_file(path, apply, assume_yes):
    """Narrow every narrowable `gh pr` blanket shadow in ONE cascade file, using configure's
    consent model verbatim: show the diff, write only with --apply + a y/N (skipped by --yes),
    back up the file first, and NEVER clobber an unparseable/unreadable file. Returns one of:
      'noop'        - file has no narrowable shadow (nothing to do here)
      'preview'     - a narrow was previewed (dry-run; nothing written)
      'narrowed'    - the file was rewritten with the blanket narrowed
      'aborted'     - the user declined the y/N (nothing written)
      'skipped'     - file unparseable/unreadable - refused to touch it
      'write-error' - the write failed
    This is the per-file remediation; doctor stays read-only and only configure narrows."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return "noop"  # disappeared since the scan; nothing to narrow
    except json.JSONDecodeError:
        print(f"configure: {path} is unparseable - refusing to narrow it (fix it by hand first).", file=sys.stderr)
        return "skipped"
    except OSError as e:
        print(f"configure: {path} is unreadable ({e}) - refusing to narrow it.", file=sys.stderr)
        return "skipped"
    if not isinstance(data, dict) or not isinstance(data.get("permissions"), dict):
        return "noop"
    allow = data["permissions"].get("allow")
    if not isinstance(allow, list):
        return "noop"
    new_allow, changed = _narrow_allow_list(allow)
    if not changed:
        return "noop"

    blankets = [r for r in allow if isinstance(r, str) and _shadow_is_narrowable(r)]
    print(f"configure will NARROW the {MERGE_TARGET!r}-shadowing blanket(s) in {path}:")
    for b in blankets:
        print(f"  - remove {b}")
    for entry in _NARROWED_GH_PR_ENTRIES:
        if entry not in allow:
            print(f"  + add    {entry}")
    print(f"    (least-privilege: blanket narrowed to non-merge subcommands; the explicit"
          f" {MERGE_TARGET!r} * entry comes via the missing-allow path from required-permissions.md,"
          f" and the {MERGE_TARGET!r} CLI is gated by the FLOOR (marker-gated hard-deny, #105).)")

    if not apply:
        print("(preview only; re-run with --apply to write. the file is backed up first.)")
        return "preview"
    if not assume_yes:
        try:
            resp = input(f"\nNarrow the blanket(s) in {path}? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            print(f"configure: aborted; left {path} unchanged.")
            return "aborted"
    try:
        shutil.copy2(path, path + ".bak")
    except OSError as e:
        print(f"configure: could not back up {path} ({e}); aborting before any write.", file=sys.stderr)
        return "write-error"
    data["permissions"]["allow"] = new_allow
    try:
        _atomic_write_json(path, data)
    except OSError as e:
        print(f"configure: FAILED to write {path}: {e}", file=sys.stderr)
        return "write-error"
    print(f"configure: narrowed {path} (backup: {path}.bak).")
    return "narrowed"


def _narrow_merge_gate_shadows(apply, assume_yes):
    """Configure's remediation for merge-gate shadows across the whole settings cascade.
    Reuses the SINGLE doctor matcher (`_scan_merge_gate_shadows`, the P3-G source of truth)
    - it never re-derives shadow detection. Narrowable `gh pr` blankets are narrowed per
    cascade file (consent-gated); broader-scope shadows (`gh *`/`gh:*`/`*`) are SURFACED for
    explicit human resolution, never auto-rewritten. Returns an exit-code delta: 0 normally,
    1 if a broader shadow remains unresolved or a per-file write/skip needs the human's eye."""
    shadows, errors = _scan_merge_gate_shadows()
    if not shadows and not errors:
        return 0
    rc = 0
    # Group narrowable shadows by file so each file is rewritten once.
    narrowable_files, broader = [], []
    seen = set()
    for path, rule in shadows:
        if _shadow_is_narrowable(rule):
            if path not in seen:
                narrowable_files.append(path); seen.add(path)
        else:
            broader.append((path, rule))
    for path in narrowable_files:
        outcome = _narrow_shadow_file(path, apply, assume_yes)
        if outcome in ("preview", "aborted", "skipped", "write-error"):
            rc = 1
    if broader:
        print(f"configure: {len(broader)} broader-scope shadow(s) need HUMAN resolution "
              "(NOT auto-narrowed - narrowing a whole-'gh' or '*' rule would silently drop "
              "unrelated scope):", file=sys.stderr)
        for path, rule in broader:
            print(f"  {rule}   in {path}", file=sys.stderr)
        print(f"  Fix by hand: remove the rule, or replace it with the enumerated non-merge "
              f"{_GH_PR_STEM} subcommands (view/diff/checks/create/list/status/edit/ready/comment), "
              f"never a blanket covering 'merge'.", file=sys.stderr)
        rc = 1
    if errors:
        for path, msg in errors:
            print(f"configure: cascade file could not be scanned: {path} ({msg}) - "
                  "not narrowed (fix it by hand).", file=sys.stderr)
        rc = 1
    return rc


def cmd_configure(args):
    """Consent-based settings.json wiring: ADD the deterministic-floor hook + any missing
    documented allow-list entries, and NARROW any blanket `gh pr` allow-rule that shadows the
    merge gate down to the enumerated non-merge subcommands. Shows the exact diff; writes only
    with --apply, and only after a y/N confirmation (skipped with --yes). Backs up each file
    before writing and NEVER clobbers an unparseable file. This is the only path that writes
    settings - doctor stays read-only - so 'permissions are the user's to grant' is preserved
    (the user runs configure and approves every change)."""
    settings, status = _load_settings_for_write()
    if status in ("unparseable", "unreadable"):
        print(f"configure: {SETTINGS} is {status} - refusing to touch it (fix it by hand first).", file=sys.stderr)
        return 1

    add_hook = not _guard_hook_present(settings)
    missing_allow = _missing_allow_entries(settings)
    if missing_allow is None:
        print(f"configure: cannot read the allow-list section of required-permissions.md "
              f"(at {TEMPLATES}); skipping allow-list changes.", file=sys.stderr)
        missing_allow = []

    # Two independent remediations gated by one --apply + one consent: (1) SETTINGS additions
    # (hook + missing allow entries); (2) GUARD DEPLOY - copy the bundled guard to the stable GUARD
    # path so a fresh plugin install has a working floor (Option A). Shadow NARROWING is a separate
    # cascade-wide remediation handled below (its own scan/diff/consent), driven by the single matcher.
    guard_action = _guard_deploy_action()           # 'deploy' | 'refresh' | 'missing-source' | None
    # #133: the bundled PR-lifecycle helpers ride the SAME Option-A deploy mechanism as the guard.
    helper_actions = [(n, _helper_deploy_action(n)) for n in HELPER_NAMES]
    helpers_actionable = [(n, a) for n, a in helper_actions if a in HELPER_DEPLOY_ACTIONS]
    helper_missing = [n for n, a in helper_actions if a == "missing-source"]
    helper_unreadable = [n for n, a in helper_actions if a == "unreadable"]
    # #95: advisory steering hooks. Skipped entirely with --no-steer, and gated on the bundled steer
    # source existing (never wire a hook pointing at a script we cannot deploy). steer_action drives
    # the deploy; steer_blocks_to_add drives the settings wiring.
    steer_action = _steer_deploy_action()  # 'deploy' | 'refresh' | 'missing-source' | None
    steer_on = not getattr(args, "no_steer", False) and steer_action != "missing-source"
    steer_blocks_to_add = _missing_steer_hook_blocks(settings) if steer_on else []
    steer_deploy_needed = steer_on and steer_action in ("deploy", "refresh")
    # #162: deploy THIS script to the stable path so the SessionStart `init` hook calls a stable
    # entry point, and refresh it on drift (stale shadow-detection logic must never persist). Wire
    # the SessionStart advisory hook the same idempotent way as the steer hooks.
    setup_action = _setup_deploy_action()  # 'deploy' | 'refresh' | 'missing-source' | None
    setup_deploy_needed = setup_action in ("deploy", "refresh")
    # Gate the SessionStart wiring on the setup source being deployable: wiring a hook that calls a
    # script we cannot deploy is pointless (mirrors steer_on gating on the bundled steer existing).
    session_init_ok = setup_action != "missing-source"
    add_session_init = session_init_ok and not _session_init_hook_present(settings)
    deploy_needed = (guard_action in ("deploy", "refresh") or bool(helpers_actionable)
                     or steer_deploy_needed or setup_deploy_needed)
    settings_changes = (add_hook or bool(missing_allow) or bool(steer_blocks_to_add)
                        or add_session_init)

    if not settings_changes and not deploy_needed:
        if guard_action == "missing-source":
            print(f"configure: {SETTINGS} already has the floor hook + all documented allow-list entries.")
            print(f"  (note: the bundled guard {BUNDLED_GUARD} is missing; could not verify/deploy "
                  "the floor guard.)", file=sys.stderr)
        else:
            print(f"configure: {SETTINGS} already has the floor hook + all documented allow-list entries, "
                  "and the deployed guard + helper scripts match the bundled plugin copies.")
        _emit_helper_warnings(helper_missing, helper_unreadable)
        if not getattr(args, "no_steer", False) and steer_action == "missing-source":
            print(f"configure: WARNING - the bundled steer script {BUNDLED_STEER} is missing; skipping "
                  "the advisory steering hooks (the rest still applies).", file=sys.stderr)
        return _narrow_merge_gate_shadows(args.apply, args.yes)

    if settings_changes:
        print(f"configure will ADD to {SETTINGS}:")
        if add_hook:
            print(f"  hooks.PreToolUse += {json.dumps(GUARD_HOOK_BLOCK)}")
            print("    (the always-on deterministic security floor)")
        for m in missing_allow:
            print(f"  permissions.allow += {m}")
        for b in steer_blocks_to_add:
            print(f"  hooks.PreToolUse += {json.dumps(b)}")
            print("    (advisory WARN-level steering; opt out with --no-steer)")
        if add_session_init:
            print(f"  hooks.SessionStart += {json.dumps(SESSION_INIT_HOOK_BLOCK)}")
            print("    (#162: advisory merge-gate-shadow surfacing at session start; read-only)")
    if setup_deploy_needed:
        verb = "DEPLOY" if setup_action == "deploy" else "REFRESH (stale)"
        print(f"configure will {verb} the setup script (for the SessionStart init hook):")
        print(f"  {BUNDLED_SETUP} -> {SETUP_DEST}")
    if guard_action in ("deploy", "refresh"):
        verb = "DEPLOY" if guard_action == "deploy" else "REFRESH (stale)"
        print(f"configure will {verb} the floor guard:")
        print(f"  {BUNDLED_GUARD} -> {GUARD}")
    if helpers_actionable:
        print(f"configure will DEPLOY/REFRESH {len(helpers_actionable)} PR-lifecycle helper script(s):")
        for name, action in helpers_actionable:
            label = {"deploy": "deploy", "refresh": "refresh (stale)",
                     "replace-symlink": "replace claude-kit symlink",
                     "replace-broken-symlink": "replace broken symlink"}[action]
            print(f"  [{label}] {os.path.join(BUNDLED_SCRIPTS_DIR, name)} -> {os.path.join(SCRIPTS_DIR, name)}")
    if steer_deploy_needed:
        verb = "DEPLOY" if steer_action == "deploy" else "REFRESH (stale)"
        print(f"configure will {verb} the steering hook:")
        print(f"  {BUNDLED_STEER} -> {STEER}")
    if guard_action == "missing-source":
        print(f"configure: WARNING - the bundled guard {BUNDLED_GUARD} is missing; cannot deploy "
              "the floor guard (the rest still applies).", file=sys.stderr)
    if not getattr(args, "no_steer", False) and steer_action == "missing-source":
        print(f"configure: WARNING - the bundled steer script {BUNDLED_STEER} is missing; skipping "
              "the advisory steering hooks (the rest still applies).", file=sys.stderr)
    _emit_helper_warnings(helper_missing, helper_unreadable)

    if not args.apply:
        print("\n(preview only; re-run with --apply to write. settings.json is backed up first, "
              "and an unparseable file is never overwritten.)")
        return _narrow_merge_gate_shadows(args.apply, args.yes)

    if not args.yes:
        try:
            resp = input("\nApply these changes? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            print("configure: aborted; no changes written.")
            return 1

    if settings_changes:
        if status == "ok":
            try:
                shutil.copy2(SETTINGS, SETTINGS + ".bak")
            except OSError as e:
                print(f"configure: could not back up {SETTINGS} ({e}); aborting before any write.", file=sys.stderr)
                return 1
        if add_hook:
            settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append(GUARD_HOOK_BLOCK)
        if missing_allow:
            settings.setdefault("permissions", {}).setdefault("allow", []).extend(missing_allow)
        if steer_blocks_to_add:
            settings.setdefault("hooks", {}).setdefault("PreToolUse", []).extend(steer_blocks_to_add)
        if add_session_init:
            settings.setdefault("hooks", {}).setdefault("SessionStart", []).append(SESSION_INIT_HOOK_BLOCK)
        try:
            os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
            _atomic_write_json(SETTINGS, settings)
        except OSError as e:
            print(f"configure: FAILED to write {SETTINGS}: {e}", file=sys.stderr)
            return 1
        backup_note = f" (backup: {SETTINGS}.bak)" if status == "ok" else ""
        print(f"configure: wrote {SETTINGS}{backup_note}.")
        if add_hook or steer_blocks_to_add or add_session_init:
            print("RESTART the Claude Code session for the new PreToolUse hook(s) to load "
                  "(PreToolUse hooks load at session start).")

    # Guard deploy is a filesystem copy, independent of (and after) the settings write.
    if guard_action in ("deploy", "refresh"):
        ok, msg = _deploy_guard()
        print(f"configure: {msg}")
        if not ok:
            return 1

    # #95: steer deploy - the same Option-A filesystem copy to the stable STEER path.
    if steer_deploy_needed:
        ok, msg = _deploy_steer()
        print(f"configure: {msg}")
        if not ok:
            return 1

    # #162: setup-script deploy - the same Option-A filesystem copy to the stable SETUP_DEST path so
    # the SessionStart `init` hook calls a stable entry point. Refreshes on drift; a self-copy no-op
    # is detected and skipped inside _deploy_setup.
    if setup_deploy_needed:
        ok, msg = _deploy_setup()
        print(f"configure: {msg}")
        if not ok:
            return 1

    # #133: helper deploy - the same Option-A filesystem copy to the stable SCRIPTS_DIR path.
    # Continue past an individual failure (deploy as many as possible), then report at the end.
    helper_failed = False
    for name, _action in helpers_actionable:
        ok, msg = _deploy_helper(name)
        print(f"configure: {msg}")
        if not ok:
            helper_failed = True
    if helper_failed:
        print("configure: one or more helper scripts failed to deploy (see above).", file=sys.stderr)
        return 1

    # After applying, narrow any cascade shadow (own scan/diff/consent).
    return _narrow_merge_gate_shadows(args.apply, args.yes)


def main():
    p = argparse.ArgumentParser(prog="orchestrate-setup.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor")
    d.add_argument("--repo")

    u = sub.add_parser("up")
    u.add_argument("--team", required=True)
    u.add_argument("--repo", required=True)
    u.add_argument("--spacing", default="12",
                   help="approx minutes between pr-shipper pushes (default: 12)")
    u.add_argument("--slug",
                   help="owner/name slug override (belt-and-suspenders for unparseable remotes); "
                        "bypasses remote URL derivation, no security check skipped")

    w = sub.add_parser("down")
    w.add_argument("--team")

    # #162: read-only SessionStart advisory; no required args, no git-repo assumption.
    sub.add_parser("init",
                   help="read-only SessionStart advisory: surface a gh-pr merge-gate shadow "
                        "(silent when clean; always exits 0)")

    c = sub.add_parser("configure",
                       help="wire the floor hook + allow-list into settings.json (consent-based)")
    c.add_argument("--apply", action="store_true",
                   help="write the changes (default is a dry-run preview)")
    c.add_argument("--yes", action="store_true",
                   help="skip the y/N confirmation (for non-interactive setup)")
    c.add_argument("--no-steer", action="store_true",
                   help="skip the advisory WARN-level steering hooks (#95); the deny-floor guard is "
                        "always wired regardless")

    args = p.parse_args()
    return {"doctor": cmd_doctor, "up": cmd_up, "down": cmd_down,
            "init": cmd_init, "configure": cmd_configure}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
