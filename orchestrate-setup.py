#!/usr/bin/env python3
"""orchestrate-setup.py - bootstrap/teardown for an orchestrate session.

  doctor [--repo PATH]                          read-only prerequisite check (exit 0 ok, 1 hard-fail)
  up --team NAME --repo PATH [--spacing SEC]    arm a session (scaffold + marker + armed self-test)
  down [--team NAME]                            disarm (rm marker + print teardown checklist)

Design: ~/.claude/skills/orchestrate/DESIGN-phase2-setup.md
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
# Script-relative: templates ship ALONGSIDE this script in the repo, so resolve them relative
# to the script (realpath resolves the ~/.claude/scripts deploy symlink to the repo). The old
# default was an absolute ~/.claude/skills/orchestrate/templates path that only existed via
# the skill symlink - it broke on any host without it (e.g. CI), so up/doctor found no templates.
TEMPLATES = os.environ.get("ORCHESTRATE_TEMPLATES_DIR", os.path.join(os.path.dirname(os.path.realpath(__file__)), "templates"))
ARTIFACTS = os.environ.get("ORCHESTRATE_ARTIFACT_DIR", "/tmp")

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


def scaffold_artifacts(team, repo, spacing):
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
    brief_out = os.path.join(team_dir, "pr-shipper-brief.md")
    src = os.path.join(TEMPLATES, "pr-shipper-brief.md")
    try:
        body = open(src).read()
    except OSError:
        body = "# pr-shipper brief\n(template not found; fill manually)\n"
    # Substitute the real template tokens: <REPO>, <STACK>, <SPACING_MIN>.
    # The team is captured in the injected header comment; the template has no team token.
    body = (body.replace("<REPO>", repo)
                .replace("<STACK>", stack)
                .replace("<SPACING_MIN>", str(spacing)))
    header = f"<!-- rendered by orchestrate-setup.py: team={team} repo={repo} spacing={spacing}min stack={stack} -->\n"
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
    """With the marker armed, the guard must HARD-DENY (exit 2) BOTH:
      - Tier-1 push-main, AND
      - Tier-2 merge-by-API (`gh api ... pulls/N/merge` mutating).
    `gh pr merge` is intentionally NOT hook-gated (the allow-list + a Claude Code prompt
    gate it, because a PreToolUse hook on this CC honors a deny but ignores `ask`), so it
    is not asserted here. Payloads are built here and fed on stdin, so the live hook never
    sees a trigger on the command line (the orchestrate test-driving rule)."""
    t1 = _feed_guard("git push origin main")
    t2 = _feed_guard("gh api -X PUT repos/o/r/pulls/5/merge")
    return t1 == 2 and t2 == 2


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


def _merge_rule_shadows(pattern):
    """Does this Bash allow PATTERN grant ANY command in the merge family (the bare
    `gh pr merge` or any merge invocation with args)? If so it silently re-grants merge and
    defeats the human-in-the-loop gate, so the doctor must FAIL.

    Sound test: the rule shadows iff the language of commands it grants INTERSECTS the merge
    family. Both languages have the shape `literal-stem` + (glob / boundary tail), so the
    intersection is non-empty iff one side's regex matches a minimal concrete member of the
    other. We probe both directions:
      - the rule's regex matches the bare target, or the target with one trailing arg
        (catches every prefix/boundary/glob rule that grants merge-with-args), AND
      - the family regex matches the rule's literal stem (its specifier with globs emptied)
        (catches a rule LONGER than the target, e.g. `gh pr merge --squash`, which grants
        only a specific merge invocation that the family still covers)."""
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
                    "(any one silently re-grants merge - remove/narrow them):")
        for path, rule in shadows:
            print(f"         {rule}   in {path}")
        print(f"         Fix: narrow each to the non-merge subcommands (e.g. {MERGE_TARGET.rsplit(' ',1)[0]} "
              "view/diff/checks/create/list), never a blanket prefix that covers 'merge'.")
    return FAIL


# The non-merge `gh pr` subcommands a narrowed blanket is replaced WITH. `merge` is
# DELIBERATELY OMITTED so Claude Code keeps prompting the human (the merge gate). The
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
    existing = set(allow)
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
# DESIGN-maintainer-channel.md (doctor check, F3-B-4).
SLACK_CHANNEL_RE = re.compile(r"[A-Z][A-Z0-9]{5,}")


def check_slack_channel():
    """Doctor check (WARN-level, optional): the maintainer Slack channel is
    optional and FORMAT-only. The stdlib doctor subprocess cannot reach MCP
    tools, so reachability is OUT of scope (validated at runtime by the lead's
    first slack_send_message; degrades per D4). This NEVER returns FAIL - the
    channel is optional and must not block doctor/up. See
    DESIGN-maintainer-channel.md (check_slack_channel, F1-3 / F2-C-3)."""
    channel = os.environ.get("ORCHESTRATE_SLACK_CHANNEL", "")
    if not channel:
        return _emit(WARN, "ORCHESTRATE_SLACK_CHANNEL not set (channel optional; terminal-only mode)")
    if not SLACK_CHANNEL_RE.fullmatch(channel):
        return _emit(WARN, f"ORCHESTRATE_SLACK_CHANNEL={channel!r} is not a well-formed Slack channel id "
                           "(expected [A-Z][A-Z0-9]{5,}); terminal-only mode")
    return _emit(PASS, f"ORCHESTRATE_SLACK_CHANNEL={channel} is a well-formed Slack channel id")


def cmd_doctor(args):
    settings = _load_settings()
    repo_status, _head = check_repo_main(getattr(args, "repo", None))
    results = [check_agent_teams(settings), check_tmux(),
               check_guard_wired(settings), check_guard_healthy(),
               repo_status, check_allowlist(settings),
               check_merge_gate_shadows(), check_slack_channel()]
    hard_fail = any(s == FAIL for s in results)
    print()
    print("doctor: HARD-FAIL (fix the FAIL lines above before `up`)" if hard_fail else "doctor: ok (no hard fail)")
    return 1 if hard_fail else 0


def cmd_up(args):
    _validate_team(args.team)  # reject a path-unsafe team name before doing any work
    print("Pre-flight doctor:")
    rc = cmd_doctor(args)
    if rc != 0:
        print("\nup: ABORT - doctor reported a hard failure; fix it and re-run.", file=sys.stderr)
        return rc
    # Reuse HEAD from doctor's check_repo_main; add timeout + empty-tolerant fallback.
    _repo_status, head = check_repo_main(args.repo)
    if not head:
        head_result = subprocess.run(["git", "-C", args.repo, "rev-parse", "--short", "HEAD"],
                                     capture_output=True, text=True, timeout=15)
        head = head_result.stdout.strip() if head_result.returncode == 0 else "unknown"
    stack, triage, brief = scaffold_artifacts(args.team, args.repo, args.spacing)
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
              "hard-deny push-main and/or merge-by-API (both must exit 2). The floor is failing "
              "open. Marker REMOVED. Fix the guard before standing up a session.", file=sys.stderr)
        return 1
    print(f"\nup: SESSION ARMED."
          f"\n  marker:   {marker_path}"
          f"\n  team dir: {os.path.join(ARTIFACTS, args.team)}"
          f"\n  stack:    {stack}\n  triage:   {triage}\n  brief:    {brief}"
          f"\n  Merges are now HUMAN-ONLY via the ! prefix until `down`.")
    return 0


TEARDOWN_CHECKLIST = """down: marker removed (Tier-2 merge-gating OFF).
Team teardown is the lead's job (tool calls, not this script):
  1. SendMessage shutdown_request to EACH teammate.
  2. WAIT for each "terminated" notice.
  3. THEN TeamDelete (it refuses while a member is alive).
  4. LEAVE worktrees that still have open PRs; clean the rest with `make remove-worktree`.
     (down WARNS above about any worktree with uncommitted work - commit before removing it.)"""


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
    print(f"    ({MERGE_TARGET!r} stays OMITTED so the human is still prompted - the merge gate.)")

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
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
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

    # SETTINGS additions (hook + missing allow entries) are applied to the primary settings
    # file. Shadow NARROWING is a separate, cascade-wide remediation handled below (it may
    # touch any cascade file, not just SETTINGS), driven by the SINGLE doctor matcher.
    if not add_hook and not missing_allow:
        print(f"configure: {SETTINGS} already has the floor hook + all documented allow-list entries.")
        # Still run the cascade-wide shadow narrowing; it has its own scan + diff + consent.
        return _narrow_merge_gate_shadows(args.apply, args.yes)

    print(f"configure will ADD to {SETTINGS}:")
    if add_hook:
        print(f"  hooks.PreToolUse += {json.dumps(GUARD_HOOK_BLOCK)}")
        print("    (the always-on deterministic security floor)")
    for m in missing_allow:
        print(f"  permissions.allow += {m}")

    if not args.apply:
        print("\n(preview only; re-run with --apply to write. settings.json is backed up first, "
              "and an unparseable file is never overwritten.)")
        return _narrow_merge_gate_shadows(args.apply, args.yes)

    if not args.yes:
        try:
            resp = input("\nApply these changes to settings.json? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            print("configure: aborted; no changes written.")
            return 1

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

    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
    except OSError as e:
        print(f"configure: FAILED to write {SETTINGS}: {e}", file=sys.stderr)
        return 1

    backup_note = f" (backup: {SETTINGS}.bak)" if status == "ok" else ""
    print(f"configure: wrote {SETTINGS}{backup_note}.")
    if add_hook:
        print("RESTART the Claude Code session for the floor hook to load (PreToolUse hooks load at session start).")
    # After applying the SETTINGS additions, narrow any cascade shadow (own scan/diff/consent).
    narrow_rc = _narrow_merge_gate_shadows(args.apply, args.yes)
    return narrow_rc


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

    w = sub.add_parser("down")
    w.add_argument("--team")

    c = sub.add_parser("configure",
                       help="wire the floor hook + allow-list into settings.json (consent-based)")
    c.add_argument("--apply", action="store_true",
                   help="write the changes (default is a dry-run preview)")
    c.add_argument("--yes", action="store_true",
                   help="skip the y/N confirmation (for non-interactive setup)")

    args = p.parse_args()
    return {"doctor": cmd_doctor, "up": cmd_up, "down": cmd_down,
            "configure": cmd_configure}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
