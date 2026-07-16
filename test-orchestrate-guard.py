#!/usr/bin/env python3
"""Proof harness for orchestrate-guard.sh.

Runs each case through BOTH input channels (stdin JSON, $TOOL_INPUT env) and the
specified marker state, asserting the guard's decision:
  - "block": exit 2 (hard deny, stderr reason).
  - "allow": exit 0 with NO permissionDecision on stdout (plain pass-through).

MERGE GATING (#105, 2026-06-15): BOTH `gh pr merge` CLI (is_pr_merge) AND merge-by-API
(`gh api ... pulls/N/merge` mutating) are MARKER-GATED FLOOR DENIES (exit 2 when the
session marker is active). A solo/non-marker session is never Tier-2-gated, so the
maintainer's /merge-pr just works prompt-free. `gh pr -R owner/repo merge` and other
global-flag forms between `pr` and `merge` are also matched (adversarial hardening).
The harness also asserts the guard NEVER emits a permissionDecision (the allow-list
`ask` approach was rejected after a live test proved CC ignores hook `ask` output).
Run: python3 test-orchestrate-guard.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "orchestrate-guard.sh")


# Fixed test $TMUX for the common single-session cases.
DEFAULT_TMUX = "/tmp/tmux-test,1,0"


def _key(tmux):
    """Mirror the guard's sanitization EXACTLY (byte-mode; contract; see DESIGN)."""
    return re.sub(rb'[^A-Za-z0-9]', b'_', tmux.encode("utf-8", "surrogateescape")).decode("ascii")


def _self_key(tmux, ccsid):
    """The key the guard derives for THIS session - mirrors orchestrate-guard.sh's
    _session_keys() precedence (#312): $TMUX wins unprefixed, else 'ccsid_' + sanitized
    session id, else no key at all."""
    if tmux is not None:
        return _key(tmux)
    if ccsid is not None:
        return "ccsid_" + _key(ccsid)
    return None


def run_guard(command, *, marker_active, channel, tmux=DEFAULT_TMUX, ccsid=None,
              foreign_keys=(), stale_self=False, ttl_hours=24,
              merge_token=None, token_tmux=None):
    """Invoke the guard. Returns (exit_code, stdout, stderr). channel in {'stdin','env'}.
    command=None means 'send no command at all' (empty-read case).
      - marker_active: arm THIS session's key (fresh) under FLOOR_DIR.
      - tmux: the $TMUX value the guard sees (None => unset => never gated).
      - foreign_keys: extra $TMUX values to arm (fresh) - other sessions' markers.
      - stale_self: arm THIS session's key with an OLD mtime (older than TTL).
      - merge_token (#263 Piece B): if set, write a merge-auth token under
        FLOOR_DIR/merge-auth/<key of token_tmux or tmux>. A dict is JSON-encoded; a
        str is written verbatim (to exercise the malformed-JSON deny path)."""
    with tempfile.TemporaryDirectory() as td:
        floor_dir = os.path.join(td, "orchestrate-floor.d")
        os.makedirs(floor_dir, exist_ok=True)
        self_key = _self_key(tmux, ccsid)
        if merge_token is not None:
            auth = os.path.join(floor_dir, "merge-auth")
            os.makedirs(auth, exist_ok=True)
            # token_tmux overrides the SESSION's key to exercise a foreign/mismatched token.
            tt = _key(token_tmux) if token_tmux is not None else self_key
            with open(os.path.join(auth, tt), "w") as f:
                f.write(merge_token if isinstance(merge_token, str) else json.dumps(merge_token))
        # Default 24 (not the guard's 72h default) so the stale_self case only needs a
        # ~25h-old file; passed to the guard via ORCHESTRATE_FLOOR_TTL_HOURS below.
        # Overridable per-case (e.g. ttl_hours=0/-5) to exercise the guard's TTL clamp.
        if marker_active and self_key is not None:
            open(os.path.join(floor_dir, self_key), "w").close()  # fresh mtime
        if stale_self and self_key is not None:
            p = os.path.join(floor_dir, self_key)
            open(p, "w").close()
            old = time.time() - (ttl_hours + 1) * 3600
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
        # #312 DETERMINISM: the guard now falls back to $CLAUDE_CODE_SESSION_ID when $TMUX is
        # absent, and the harness runs INSIDE a real Claude Code session that exports one. Strip
        # it by default so a `tmux=None` case genuinely means "no key at all" instead of
        # silently keying off the ambient session id; pass ccsid= to exercise the fallback.
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        if ccsid is not None:
            env["CLAUDE_CODE_SESSION_ID"] = ccsid
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


def has_decision(stdout):
    """True if stdout carries any hook permissionDecision. The guard should NEVER emit one
    now (the ask approach was rejected - CC ignored it); this catches a regression."""
    try:
        json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]
        return True
    except (ValueError, KeyError, TypeError):
        return False


# Case table: (label, command, marker_active, expected_mode in {"block","allow"})
# command=None => empty-read case (no stdin, no env).
CASES = [
    ("empty-read fails open (no stdin/env)", None, False, "allow"),
    # Tier-1 push-main: blocked ALWAYS (marker irrelevant)
    ("git push origin main", "git push origin main", False, "block"),
    ("git -C wt push origin main", "git -C ../wt push origin main", False, "block"),
    ("git push origin HEAD:main (refspec)", "git push origin HEAD:main", False, "block"),
    ("safe-push.sh main", "scripts/safe-push.sh main", False, "block"),
    ("push master", "git push origin master", False, "block"),
    # false-positive guards
    ("branch named maintenance allowed", "git push origin maintenance # prep-pr-ok", False, "allow"),
    ("branch named domain allowed", "git push origin domain # prep-pr-ok", False, "allow"),
    ("feature branch push (advisory only, has override)",
     "git push origin feat # prep-pr-ok", False, "allow"),
    # Tier-1 force / no-verify: blocked ALWAYS
    ("bare --force push", "git push --force origin feat", False, "block"),
    ("-f push", "git push -f origin feat", False, "block"),
    ("safe-push --force", "scripts/safe-push.sh feat --force", False, "block"),
    ("--no-verify push", "git push --no-verify origin feat", False, "block"),
    # Tier-1 git --no-verify on ANY git subcommand (broadened from push-only): blocked ALWAYS
    ("git commit --no-verify", "git commit --no-verify", False, "block"),
    ("git commit -m msg --no-verify", 'git commit -m msg --no-verify', False, "block"),
    ("git rebase --no-verify", "git rebase --no-verify", False, "block"),
    ("git commit -m msg (no flag) allowed", "git commit -m msg", False, "allow"),
    ("git pull --no-verify (pull forwards to merge)", "git pull --no-verify", False, "block"),
    # scoped-negative: --no-verify with NO git invocation must NOT be blocked
    ("sometool --no-verify (no git) allowed", "sometool --no-verify", False, "allow"),
    # subcommand-scoping false-positive guards (FP-round1): the `git` word + `--no-verify`
    # substring present, but NO git subcommand that accepts the flag -> must ALLOW.
    ("gh issue title bans --no-verify mentioning git allowed",
     'gh issue create --title "ban --no-verify in git workflows" --body x', False, "allow"),
    ("git config alias with --no-verify in value allowed",
     'git config alias.foo "log --no-verify-style"', False, "allow"),
    # global opts between git and the accepting subcommand still block (anchoring is loose)
    ("git -C dir commit --no-verify still blocks", "git -C ../wt commit --no-verify", False, "block"),
    # subcommand that does NOT accept --no-verify + token in an arg -> allow
    ("git log --grep with --no-verify in pattern allowed",
     'git log --grep="--no-verify"', False, "allow"),
    # Tier-1 gh pr merge --admin (admin-bypass overriding branch protection): blocked ALWAYS.
    # SUBCOMMAND-anchored: `gh pr merge` is the ONLY gh subcommand that accepts --admin.
    ("gh pr merge --admin", "gh pr merge --admin 1868", False, "block"),
    ("gh pr merge --squash --admin", "gh pr merge --squash --admin 1868", False, "block"),
    ("gh -R o/r pr merge --admin (global flags)", "gh -R o/r pr merge --admin 5", False, "block"),
    # scoped-negative: --admin with NO gh invocation must NOT be blocked
    ("sometool --admin (no gh) allowed", "sometool --admin", False, "allow"),
    ("gh pr view (no admin) allowed", "gh pr view 123", False, "allow"),
    ("gh pr list (no admin) allowed", "gh pr list", False, "allow"),
    # subcommand-scoping false-positive guards (FP-round1): --admin in a NON-merge gh
    # subcommand cannot be a real bypass (gh rejects the flag there) -> must ALLOW.
    # `gh repo edit --admin` is NOT a bypass (repo edit takes no --admin); gh would error.
    ("gh repo edit --admin (not a merge bypass) allowed", "gh repo edit --admin", False, "allow"),
    ("gh pr create --title with --admin literal allowed",
     'gh pr create --title "block gh --admin"', False, "allow"),
    ("gh issue comment body documents --admin allowed",
     'gh issue comment 5 -b "document the --admin flag"', False, "allow"),
    ("git commit msg mentions gh --admin (no pr merge) allowed",
     'git commit -m "doc the --admin gh flag"', False, "allow"),
    # gh absent -> admin deny cannot fire even if pr/merge/--admin words are in prose
    ("git commit msg 'pr merge --admin' (no gh word) allowed",
     'git commit -m "do not pr merge --admin"', False, "allow"),
    ("--force-with-lease allowed", "git push --force-with-lease origin feat # prep-pr-ok", False, "allow"),
    ("git clean --force allowed", "git clean --force", False, "allow"),
    # `gh pr merge` is MARKER-GATED DENIED (#105): marker active -> block (a bot cannot merge in a
    # team session; the human merges from a SEPARATE terminal where no marker is present); marker
    # absent (solo) -> allow, so the maintainer's /merge-pr just works prompt-free. The matcher
    # tolerates global FLAGS (and their values) between `pr` and `merge` - so `gh pr -R o/r merge`
    # and `gh pr --repo o/r merge` are caught in addition to the simple adjacent form - while NOT
    # matching a different `gh pr` subcommand (comment/create/view) whose body mentions merge.
    # `gh pr merge --admin` stays Tier-1 (is_gh_admin, always denied regardless of marker).
    ("gh pr merge (marker active) -> block (#105 floor gate)", "gh pr merge 1868", True, "block"),
    ("gh pr merge --auto (active) -> block", "gh pr merge --auto 1868", True, "block"),
    ("gh pr merge --squash (active) -> block (the /merge-pr form)", "gh pr merge --squash 101", True, "block"),
    ("gh -R o/r pr merge (active) -> block (global flags before pr)", "gh -R o/r pr merge 1868", True, "block"),
    # #105 adversarial fix: global flags BETWEEN pr and merge must also be caught
    ("gh pr -R owner/repo merge 5 (active) -> block (flag between pr+merge, the bug)", "gh pr -R owner/repo merge 5", True, "block"),
    ("gh pr --repo owner/repo merge 5 (active) -> block (long-flag between pr+merge)", "gh pr --repo owner/repo merge 5", True, "block"),
    ("gh pr --repo owner/repo merge --auto (active) -> block (flag+value then merge)", "gh pr --repo owner/repo merge --auto", True, "block"),
    ("gh pr --repo=owner/repo merge 5 (active) -> block (=-form, no-space flag assignment)", "gh pr --repo=owner/repo merge 5", True, "block"),
    # marker ABSENT (solo) -> must allow (so /merge-pr just works prompt-free)
    ("gh pr merge (marker ABSENT/solo) -> allow (so /merge-pr just works)", "gh pr merge 1868", False, "allow"),
    ("gh pr merge --squash (absent/solo) -> allow", "gh pr merge --squash 101", False, "allow"),
    ("gh pr -R owner/repo merge 5 (absent/solo) -> allow", "gh pr -R owner/repo merge 5", False, "allow"),
    ("gh pr merge --admin (active) -> block (Tier-1 admin, always)", "gh pr merge --admin 5", True, "block"),
    ("gh pr merge --admin (absent) -> block (Tier-1 admin, always)", "gh pr merge --admin 5", False, "block"),
    # #105 false-positive guards: a `gh pr <other-subcommand>` whose BODY mentions merge must NOT be blocked.
    ("gh pr comment body says merge (active) -> allow",
     'gh pr comment 5 -b "ready to merge this"', True, "allow"),
    ("gh pr comment body 'squash and merge' (active) -> allow",
     'gh pr comment 5 -b "lets squash and merge it"', True, "allow"),
    ("gh pr edit --add-label merge (active) -> allow",
     'gh pr edit 5 --add-label merge', True, "allow"),
    ("gh pr view --json mergeable (active) -> allow",
     'gh pr view 5 --json mergeable', True, "allow"),
    ("git merge main (active) -> allow (a git merge, no gh)",
     'git merge main', True, "allow"),
    ("gh pr create -t 'pr merge gate' (active) -> allow (pr not followed by flags-then-merge)",
     'gh pr create -t "pr merge gate"', True, "allow"),
    # ACCEPTED F30 false-positive (rare, documented): a body literally containing the phrase
    # "pr merge" (with optional flags between) trips the whole-string matcher (no shell-quote
    # parsing). Reword or use the human `!` escape.
    ("gh pr comment body literally 'pr merge' (active) -> block (accepted F30 FP)",
     'gh pr comment 5 -b "do the pr merge now"', True, "block"),
    # merge-by-API stays a marker-gated HARD DENY (gh api * is broadly allow-listed)
    ("merge-by-API PUT (active) -> block", "gh api -X PUT repos/o/r/pulls/1/merge", True, "block"),
    ("merge-by-API --method PUT (active) -> block", "gh api --method PUT repos/o/r/pulls/1/merge", True, "block"),
    ("merge-by-API field-implies-POST (active) -> block",
     "gh api repos/o/r/pulls/1/merge -f merge_method=squash", True, "block"),
    ("merge-by-API --method=PUT glued (active) -> block", "gh api --method=PUT repos/o/r/pulls/1/merge", True, "block"),
    ("merge-by-API -XPUT glued (active) -> block", "gh api -XPUT repos/o/r/pulls/1/merge", True, "block"),
    ("merge-by-API -X=PUT (active) -> block", "gh api -X=PUT repos/o/r/pulls/1/merge", True, "block"),
    ("merge-by-API --field= glued (active) -> block", "gh api repos/o/r/pulls/1/merge --field=merge_method=squash", True, "block"),
    ("merge-by-API -f glued (active) -> block", "gh api repos/o/r/pulls/1/merge -fmerge_method=squash", True, "block"),
    # merge-by-API with marker ABSENT -> allowed (solo session untouched)
    ("merge-by-API PUT (absent) allowed", "gh api -X PUT repos/o/r/pulls/1/merge", False, "allow"),
    # false-positives: plain-ALLOW even with marker active
    ("gh pr create title contains merge (active)",
     'gh pr create --title "merge auth refactor"', True, "allow"),
    ("gh api GET merge-status check (active)",
     "gh api repos/o/r/pulls/5/merge", True, "allow"),
    ("CodeQL dismiss -X PATCH (active) allowed",
     "gh api -X PATCH repos/o/r/code-scanning/alerts/5", True, "allow"),
    ("CodeQL dismiss --method=PATCH glued (active) allowed",
     "gh api --method=PATCH repos/o/r/code-scanning/alerts/5", True, "allow"),
    ("gh pr view (active) allowed", "gh pr view 1868", True, "allow"),
    # prep-pr-ok advisory gate
    ("feature push without override blocked", "git push origin feat", False, "block"),
    ("push main + prep-pr-ok still blocked", "git push origin main # prep-pr-ok", False, "block"),
    # non-push commands always allowed
    ("ls allowed", "ls -la", False, "allow"),
    ("git status allowed", "git status", False, "allow"),
    # per-clause hard denies (FP1 fix)
    ("compound: checkout main && push feat (FP1 allowed)",
     "git checkout main && git push origin feat # prep-pr-ok", False, "allow"),
    ("compound: clean --force && push feat (FP1 allowed)",
     "git clean --force && git push origin feat # prep-pr-ok", False, "allow"),
    ("compound: commit msg has main && push feat (FP1 allowed)",
     'git commit -m "fix main bug" && git push origin feat # prep-pr-ok', False, "allow"),
    ("compound: rm -f && push feat (FP1 allowed)",
     "rm -f x.tmp && git push origin feat # prep-pr-ok", False, "allow"),
    ("compound: push main && ls still blocks (no per-clause bypass)",
     "git push origin main && ls", False, "block"),
    # compound: a `gh pr merge` clause is now marker-gated (#105), so view && merge BLOCKS
    # per-clause when the marker is active; the same compound in a SOLO session is allowed.
    ("compound: view && gh-pr-merge (active) -> block (#105 per-clause)",
     "gh pr view 5 && gh pr merge 5", True, "block"),
    ("compound: view && gh-pr-merge (absent/solo) -> allow",
     "gh pr view 5 && gh pr merge 5", False, "allow"),
    # ...but a merge-by-API clause in a compound still hard-blocks (per-clause)
    ("compound: view && merge-by-API (active) -> block (per-clause)",
     "gh pr view 5 && gh api -X PUT repos/o/r/pulls/5/merge", True, "block"),
    # a `gh pr merge` ASK must not pre-empt a Tier-1 push-main: it is not gated anyway,
    # but the push-main in a later clause must still hard-block.
    ("compound: gh-pr-merge && push main still BLOCKS (Tier-1 wins)",
     "gh pr merge 5 && git push origin main", True, "block"),
    ("compound: gh-pr-merge && force push still BLOCKS (Tier-1 wins)",
     "gh pr merge 5 && git push --force origin feat", True, "block"),
    # G1: quoted main destination
    ("G1 quoted 'main' dest blocked", "git push origin 'main'", False, "block"),
    ('G1 quoted "main" dest blocked', 'git push origin "main"', False, "block"),
    # G2 accepted limitation
    ("G2 glued -fu force accepted-limitation (allowed w/ override)",
     "git push -fu origin feat # prep-pr-ok", False, "allow"),
    # push-subcommand anchoring
    ("commit msg says 'push to main' is NOT a push (allowed)",
     'git -C ../wt commit -m "ci: docker job runs only on push to main"', False, "allow"),
    ("git log main..HEAD read-only ref (allowed)", "git log main..HEAD", False, "allow"),
    ("git merge-base main HEAD read-only (allowed)", "git merge-base main HEAD", False, "allow"),
    ("git diff main..HEAD read-only (allowed)", "git diff main..HEAD", False, "allow"),
    ("git rev-list main..HEAD read-only (allowed)", "git rev-list main..HEAD", False, "allow"),
    ("env-prefix real push origin main still blocks",
     "GIT_PAGER=cat git push origin main", False, "block"),
    ("git -C wt push origin main still blocks", "git -C ../wt push origin main", False, "block"),
    ("commit msg mentions safe-push + main is NOT a push (allowed)",
     'git commit -m "doc: use safe-push.sh for features, never push main"', False, "allow"),
    ("bash-wrapped safe-push to main still blocks", "bash scripts/safe-push.sh main", False, "block"),
    ("env-prefix safe-push to main still blocks", "FOO=bar scripts/safe-push.sh main", False, "block"),
    ("./safe-push.sh master still blocks", "./safe-push.sh master", False, "block"),
    ("~/.claude path safe-push main still blocks", "~/.claude/scripts/safe-push.sh main", False, "block"),
    ("cd wt && safe-push main still blocks (per-clause)", "cd ../wt && safe-push.sh main", False, "block"),
    ("safe-push feature branch allowed (override)", "scripts/safe-push.sh feat # prep-pr-ok", False, "allow"),
    ("gh pr diff allowed", "gh pr diff 1868", False, "allow"),
    ("gh api PR resource GET allowed (marker active)", "gh api repos/o/r/pulls/1", True, "allow"),
    ("gh api graphql field-flag non-merge allowed (marker active, F8)", "gh api graphql -f query=...resolveReviewThread...", True, "allow"),
    # FP-round2 (2026-06-07): `git push` / `safe-push` appearing as a quoted ARGUMENT to a
    # read-only inspector, or as prose inside a heredoc/echo body, is NOT a push invocation.
    # Before this round, looks_like_git_push matched `git ... push` ANYWHERE in a clause (only
    # looks_like_safe_push was command-position anchored), so a read-only diagnostic or a log
    # entry documenting the guard was denied (the dogfood-lead report: pgrep blocked, and a
    # `cat >> log <<EOF ... git push ... EOF` blocked the very entry describing the block).
    # Fix anchors looks_like_git_push to command position (clause start) and makes the advisory
    # push-detection per-clause. Read-only inspectors + documentation must ALLOW.
    ("FP2: pgrep -f for a 'git push origin' pattern (read-only) allowed",
     "pgrep -f 'git push origin'", False, "allow"),
    ("FP2: grep -f patterns 'git push origin' (read-only, -f not a force) allowed",
     "grep -f patterns 'git push origin'", False, "allow"),
    ("FP2: echo documenting 'git push' prose allowed",
     "echo 'documenting git push origin behavior'", False, "allow"),
    ("FP2: heredoc log append whose BODY mentions git push allowed",
     "cat >> notes.md <<'EOF'\nthe git push origin command was denied\nEOF", False, "allow"),
    ("FP2: ps | grep 'git push' pipeline (read-only) allowed",
     "ps aux | grep 'git push origin'", False, "allow"),
    ("FP2: rg 'safe-push' in a log (read-only) allowed",
     "rg 'safe-push' /tmp/run.log", False, "allow"),
    # FP-round2 negatives: real pushes embedded in compounds with inspectors must STILL gate.
    ("FP2-neg: pgrep ... && real push main still BLOCKS (per-clause)",
     "pgrep -f 'git push' && git push origin main", False, "block"),
    ("FP2-neg: echo prose && real feature push still advisory-blocks",
     "echo 'doc git push' && git push origin feat", False, "block"),
    # FP2-round2 (adversarial pass): a real push after a command-position INTRODUCER, or as a
    # bare-newline-separated second command, MUST still block. The first clause-start anchor
    # regressed these vs the old "anywhere-in-clause" matcher; the _INTRO group + bare-newline
    # split restore them. push-to-main = hard block; feature = advisory block.
    ("FP2r2: subshell ( git push origin main ) blocks", "( git push origin main )", False, "block"),
    ("FP2r2: command git push origin main blocks", "command git push origin main", False, "block"),
    ("FP2r2: nohup git push origin main blocks", "nohup git push origin main", False, "block"),
    ("FP2r2: time git push origin main blocks", "time git push origin main", False, "block"),
    ("FP2r2: leading redirect git push main blocks", ">/tmp/x git push origin main", False, "block"),
    ("FP2r2: if/then git push origin main blocks", "if true; then git push origin main; fi", False, "block"),
    ("FP2r2: bare-newline second command push main blocks", "echo hi\ngit push origin main", False, "block"),
    ("FP2r2: bare-newline feature push advisory-blocks", "echo hi\ngit push origin feat", False, "block"),
    ("FP2r2: subshell feature push advisory-blocks", "( git push origin feat )", False, "block"),
    # backslash-newline CONTINUATION to main stays ONE clause and still blocks (preserved)
    ("FP2r2: backslash-newline continued push main blocks", "git push origin \\\nmain", False, "block"),
    # a heredoc/prose body line that does NOT lead with git stays allowed across a bare newline
    ("FP2r2: heredoc body bare-newline git-push prose allowed",
     "cat <<'EOF'\nfirst line\nthe git push origin step was denied\nEOF", False, "allow"),
    # --- (#186) tag-push carve-out: a PURE tag push is exempt from the prep-pr-ok
    # ADVISORY only (block 3). A tag push never goes through a PR, so /prep-pr is N/A.
    # Recognized: the `--tags` flag, or a `refs/tags/<name>` destination. No override needed.
    # POSITIVE exempt (allow WITHOUT # prep-pr-ok):
    ("tag push refs/tags/ exempt from advisory", "git push origin refs/tags/v1.10.0", False, "allow"),
    ("tag push --tags exempt from advisory", "git push --tags", False, "allow"),
    ("tag push --tags origin exempt", "git push --tags origin", False, "allow"),
    ("tag push origin --tags exempt", "git push origin --tags", False, "allow"),
    ("tag push exempt even with marker active", "git push origin refs/tags/v1.10.0", True, "allow"),
    ("tag push with redundant override still allowed", "git push origin refs/tags/v1.10.0 # prep-pr-ok", False, "allow"),
    # REGRESSION: the carve-out must NOT leak a Tier-1 deny (those fire BEFORE is_push_clause):
    ("tag+main mixed still blocks (main deny)", "git push origin refs/tags/v1 main", False, "block"),
    ("--tags + bare force still blocks (force deny)", "git push --tags --force origin feat", False, "block"),
    ("push main TO a tag ref still blocks (main: refspec)", "git push origin main:refs/tags/x", False, "block"),
    ("--tags + --no-verify still blocks (no-verify deny)", "git push --tags --no-verify origin feat", False, "block"),
    # PRESERVE advisory for real branch pushes (narrowing: refs/tags/ needs the slash):
    ("branch named refs/tags-backup is NOT a tag push (advisory)", "git push origin refs/tags-backup", False, "block"),
    ("plain feature push still advisory-blocks (unchanged)", "git push origin feat", False, "block"),
    # COMPOUND: a tag clause exempts itself but a later feature clause still gates:
    ("compound tag && feature: feature clause still advisory-blocks",
     "git push origin refs/tags/v1 && git push origin feat", False, "block"),
    # MARKER session: the carve-out never disarms the Tier-2 merge gate:
    ("compound tag && gh-pr-merge (active) still blocks merge (Tier-2)",
     "git push origin refs/tags/v1 && gh pr merge 5", True, "block"),
    # A tag token only in a TRAILING COMMENT must NOT exempt a real feature push from the
    # advisory (the matcher strips a trailing `#...` comment before testing for tag refs):
    ("feature push with refs/tags/ only in comment still advisory-blocks",
     "git push origin feat # refs/tags/v1", False, "block"),
    ("feature push with --tags only in comment still advisory-blocks",
     "git push origin feat # use --tags", False, "block"),
]

FAILS = []


def check(label, command, marker_active, expected):
    channels = ["stdin", "env"] if command is not None else ["stdin"]
    for ch in channels:
        rc, stdout, _stderr = run_guard(command, marker_active=marker_active, channel=ch)
        if expected == "block":
            ok = (rc == 2)
        elif expected == "allow":
            # plain allow: exit 0 AND no permissionDecision leaked onto stdout
            ok = (rc == 0 and not has_decision(stdout))
        else:
            ok = False
        status = "ok" if ok else "FAIL"
        if not ok:
            FAILS.append(f"{label} [{ch}]: expected {expected}, got exit {rc} "
                         f"hasDecision={has_decision(stdout)}")
        print(f"  [{status}] {label} [{ch}] -> exit {rc} (want {expected})")


def main():
    for label, command, marker_active, expected in CASES:
        check(label, command, marker_active, expected)

    # --- P3-A refcount / keying properties (merge-by-API is the only marker-gated path) ---
    MERGE_API = "gh api -X PUT repos/o/r/pulls/1/merge"
    SESS_A = "/tmp/tmux-501/default,111,0"
    SESS_B = "/tmp/tmux-501/default,222,1"

    def expect(label, rc, want):
        ok = (rc == 2) if want == "block" else (rc == 0)
        print(f"  [{'ok' if ok else 'FAIL'}] {label} -> exit {rc} (want {want})")
        if not ok:
            FAILS.append(f"{label}: expected {want}, got exit {rc}")

    rc, _o, _e = run_guard(MERGE_API, marker_active=True, channel="stdin", tmux=SESS_A)
    expect("refcount: armed self merge-by-API blocked", rc, "block")
    rc, _o, _e = run_guard(MERGE_API, marker_active=False, channel="stdin",
                           tmux=SESS_B, foreign_keys=[SESS_A])
    expect("refcount: foreign marker does NOT gate me", rc, "allow")
    rc, _o, _e = run_guard(MERGE_API, marker_active=False, channel="stdin",
                           tmux=None, foreign_keys=[SESS_A])
    expect("refcount: empty $TMUX never gated", rc, "allow")
    rc, _o, _e = run_guard(MERGE_API, marker_active=False, channel="stdin",
                           tmux=SESS_A, stale_self=True)
    expect("refcount: stale self marker not gated", rc, "allow")
    rc, _o, _e = run_guard(MERGE_API, marker_active=True, channel="stdin",
                           tmux=SESS_A, foreign_keys=[SESS_B])
    expect("refcount: two armed - A gated under A", rc, "block")
    rc, _o, _e = run_guard(MERGE_API, marker_active=True, channel="stdin",
                           tmux=SESS_B, foreign_keys=[SESS_A])
    expect("refcount: two armed - B gated under B", rc, "block")
    rc, _o, _e = run_guard("git" + " push origin main", marker_active=False,
                           channel="stdin", tmux=None)
    expect("refcount: Tier-1 push-main blocks regardless of $TMUX", rc, "block")

    # Tier-1 git --no-verify and gh --admin are marker-INDEPENDENT: no $TMUX, no marker.
    rc, _o, _e = run_guard("git commit --no-verify", marker_active=False,
                           channel="stdin", tmux=None)
    expect("marker-indep: git --no-verify blocks with no $TMUX/marker", rc, "block")
    rc, _o, _e = run_guard("gh pr merge --admin 1868", marker_active=False,
                           channel="stdin", tmux=None)
    expect("marker-indep: gh --admin blocks with no $TMUX/marker", rc, "block")

    # Post-disarm isolation (the defect P3-A fixes): with only B armed (A's marker
    # removed), A is NOT gated but B still is. foreign_keys arms B; marker_active=False
    # for A models A having been disarmed while B stays live.
    rc, _o, _e = run_guard(MERGE_API, marker_active=False, channel="stdin",
                           tmux=SESS_A, foreign_keys=[SESS_B])
    expect("refcount: after A disarmed, A not gated (B still armed)", rc, "allow")
    rc, _o, _e = run_guard(MERGE_API, marker_active=True, channel="stdin",
                           tmux=SESS_B, foreign_keys=[])
    expect("refcount: after A disarmed, B still gated", rc, "block")

    # Key-contract: a known $TMUX maps to the expected sanitized filename, AND the guard
    # actually finds a marker placed at that exact path. Guards against sanitization drift
    # (e.g. tr -cs squeeze) that the value-specific cases above would not catch.
    contract_tmux = "/tmp/tmux-501/default,111,0"
    if _key(contract_tmux) != "_tmp_tmux_501_default_111_0":
        FAILS.append(f"key-contract: _key({contract_tmux!r}) = {_key(contract_tmux)!r}, "
                     "expected '_tmp_tmux_501_default_111_0'")
        print("  [FAIL] key-contract: sanitized key mismatch")
    else:
        print("  [ok] key-contract: sanitized key == _tmp_tmux_501_default_111_0")
    rc, _o, _e = run_guard(MERGE_API, marker_active=True, channel="stdin", tmux=contract_tmux)
    expect("key-contract: guard finds marker at the known sanitized path", rc, "block")

    # TTL clamp: a non-positive ORCHESTRATE_FLOOR_TTL_HOURS must NOT silently disarm the
    # gate. The guard rejects 0/negative/non-integer and falls back to 72h, so a FRESH
    # marker still blocks. (Without the clamp, age_h < 0 would be false -> never active.)
    rc, _o, _e = run_guard(MERGE_API, marker_active=True, channel="stdin", tmux=SESS_A, ttl_hours=0)
    expect("ttl-clamp: TTL=0 still blocks a fresh marker (clamped to 72h)", rc, "block")
    rc, _o, _e = run_guard(MERGE_API, marker_active=True, channel="stdin", tmux=SESS_A, ttl_hours=-5)
    expect("ttl-clamp: TTL=-5 still blocks a fresh marker (clamped to 72h)", rc, "block")

    # Locale-independent key: a multibyte $TMUX is sanitized byte-wise (LC_ALL=C tr) on the
    # guard side, matching run_guard's byte-mode _key for placing the marker. If they agreed
    # only under a UTF-8 locale this would fail when the guard runs under LC_ALL=C.
    mb_tmux = "/tmp/tmux-café/sock,7,0"
    rc, _o, _e = run_guard(MERGE_API, marker_active=True, channel="stdin", tmux=mb_tmux)
    expect("locale: multibyte $TMUX keys identically (guard finds the marker)", rc, "block")

    # Regression: hard-deny block messages must NOT echo a re-enabling token.
    HARD_DENY = [
        ("git push origin main", False),
        ("git push --force origin feat", False),
        ("git push --no-verify origin feat", False),
        ("git commit --no-verify", False),
        ("gh pr merge --admin 1868", False),
        ("gh api -X PUT repos/o/r/pulls/1/merge", True),  # merge-by-API stays a hard deny
    ]
    for cmd, active in HARD_DENY:
        rc, _stdout, stderr = run_guard(cmd, marker_active=active, channel="stdin")
        if rc != 2:
            FAILS.append(f"regression setup: '{cmd}' expected block(2), got {rc}")
        elif "prep-pr-ok" in stderr:
            FAILS.append(f"regression: hard-deny message for '{cmd}' leaks the prep-pr-ok bypass token")
        else:
            print(f"  [ok] regression: '{cmd}' blocks without leaking a bypass token")

    # Regression: the guard must NEVER emit a hook permissionDecision anymore (the `ask`
    # approach was rejected - this CC ignores it; emitting it is dead/misleading output).
    saw_decision = False
    for cmd, active in [("gh pr merge 1868", True), ("gh api -X PUT repos/o/r/pulls/1/merge", True),
                        ("git push origin main", False)]:
        _rc, stdout, _stderr = run_guard(cmd, marker_active=active, channel="stdin")
        if has_decision(stdout):
            saw_decision = True
            FAILS.append(f"regression: guard emitted a permissionDecision for '{cmd}' (ask was removed)")
    if not saw_decision:
        print("  [ok] regression: guard emits no permissionDecision on any path (ask removed)")

    # --- #263 Piece B: merge-auth token relaxation (marker-active `gh pr merge`) ---
    # A fresh session-scoped token whose head_sha matches the pinned --match-head-commit
    # ALLOWs the merge; every doubt (absent/expired/mismatched/malformed/foreign/no-pin)
    # BLOCKs (deny on doubt). merge-by-API is NOT relaxed. Solo (no marker) is unchanged.
    VSHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"

    def _tok(sha=VSHA, pr=265, exp_delta=600):
        return {"pr": pr, "head_sha": sha, "expiry": int(time.time()) + exp_delta}

    MERGE_OK = "gh pr merge 265 --squash --match-head-commit " + VSHA
    rc, _o, _e = run_guard(MERGE_OK, marker_active=True, channel="stdin", merge_token=_tok())
    expect("piece-b: fresh token + matching --match-head-commit -> ALLOW", rc, "allow")
    rc, _o, _e = run_guard(MERGE_OK, marker_active=True, channel="stdin", merge_token=None)
    expect("piece-b: NO token -> BLOCK (deny on doubt)", rc, "block")
    rc, _o, _e = run_guard(MERGE_OK, marker_active=True, channel="stdin", merge_token=_tok(sha="b" * 40))
    expect("piece-b: token head_sha != pinned sha -> BLOCK", rc, "block")
    rc, _o, _e = run_guard(MERGE_OK, marker_active=True, channel="stdin", merge_token=_tok(exp_delta=-60))
    expect("piece-b: EXPIRED token -> BLOCK", rc, "block")
    rc, _o, _e = run_guard("gh pr merge 265 --squash", marker_active=True, channel="stdin", merge_token=_tok())
    expect("piece-b: token but NO --match-head-commit pin -> BLOCK", rc, "block")
    rc, _o, _e = run_guard(MERGE_OK, marker_active=True, channel="stdin", merge_token="not json{")
    expect("piece-b: malformed token JSON -> BLOCK", rc, "block")
    rc, _o, _e = run_guard(MERGE_OK, marker_active=True, channel="stdin",
                           merge_token={"pr": 265, "expiry": int(time.time()) + 600})
    expect("piece-b: token missing head_sha -> BLOCK", rc, "block")
    rc, _o, _e = run_guard(MERGE_OK, marker_active=True, channel="stdin",
                           merge_token={"pr": 265, "head_sha": VSHA, "expiry": "soon"})
    expect("piece-b: non-numeric expiry -> BLOCK", rc, "block")
    rc, _o, _e = run_guard(MERGE_OK.replace(VSHA, VSHA.upper()), marker_active=True,
                           channel="stdin", merge_token=_tok(sha=VSHA))
    expect("piece-b: UPPERCASE --match-head-commit matches lowercase token -> ALLOW", rc, "allow")
    rc, _o, _e = run_guard(MERGE_OK, marker_active=False, channel="stdin", merge_token=None)
    expect("piece-b: solo/no-marker gh pr merge -> ALLOW (unchanged)", rc, "allow")
    rc, _o, _e = run_guard("gh api -X PUT repos/o/r/pulls/265/merge", marker_active=True,
                           channel="stdin", merge_token=_tok())
    expect("piece-b: merge-by-API NOT relaxed by a token -> BLOCK", rc, "block")
    rc, _o, _e = run_guard(MERGE_OK, marker_active=True, channel="stdin",
                           tmux=SESS_A, merge_token=_tok(), token_tmux=SESS_B)
    expect("piece-b: another session's token does NOT authorize me -> BLOCK", rc, "block")
    # Two --match-head-commit flags are AMBIGUOUS: gh's pflag honors the LAST, the guard
    # would validate one - deny on doubt rather than risk validating a different SHA than
    # gh enforces (hostile-review MINOR; aligns with deny-on-doubt).
    rc, _o, _e = run_guard(MERGE_OK + " --match-head-commit " + ("c" * 40), marker_active=True,
                           channel="stdin", merge_token=_tok(sha=VSHA))
    expect("piece-b: two --match-head-commit flags (ambiguous) -> BLOCK", rc, "block")
    # PR binding (Copilot #268): a token for #265 must NOT authorize merging a DIFFERENT
    # PR even if the pinned SHA matches (two PRs can share a head SHA - same head branch).
    rc, _o, _e = run_guard("gh pr merge 999 --squash --match-head-commit " + VSHA, marker_active=True,
                           channel="stdin", merge_token=_tok(pr=265, sha=VSHA))
    expect("piece-b: token.pr(265) != command pr(999), SHA matches -> BLOCK (pr binding)", rc, "block")
    # pr right after `merge` (sanctioned) binds correctly -> ALLOW.
    rc, _o, _e = run_guard("gh pr merge 265 --squash --match-head-commit " + VSHA, marker_active=True,
                           channel="stdin", merge_token=_tok(pr=265, sha=VSHA))
    expect("piece-b: token.pr == command pr (sanctioned order) -> ALLOW", rc, "allow")
    # ANY flag before the positional pr -> deny-on-doubt (the sanctioned form is pr-FIRST;
    # allowing flags between merge and the pr re-opens the value-flag divergence below).
    rc, _o, _e = run_guard("gh pr merge --squash 265 --match-head-commit " + VSHA, marker_active=True,
                           channel="stdin", merge_token=_tok(pr=265, sha=VSHA))
    expect("piece-b: a flag before the pr -> BLOCK (pr must be first after merge)", rc, "block")
    # CRITICAL class (hostile-review): gh's value-taking flags (-b/--body/-t/--subject/
    # -A/--author-email/-F/--body-file) can carry a bare integer VALUE; a valueless-flag
    # regex would read that value as the pr while gh merges a DIFFERENT positional pr.
    # Requiring the pr immediately after `merge` denies the whole class.
    for vflag in ("-b", "--body", "-t", "--subject", "-A", "--author-email"):
        cmd = f"gh pr merge {vflag} 265 999 --match-head-commit {VSHA}"
        rc, _o, _e = run_guard(cmd, marker_active=True, channel="stdin", merge_token=_tok(pr=265, sha=VSHA))
        expect(f"piece-b: value-flag {vflag} 265 before real pr 999 -> BLOCK (no value scrape)", rc, "block")
    # A malformed non-token pr (gh would treat `265abc` as a branch, not PR 265) must NOT
    # extract 265 -> deny-on-doubt (the pr must be a complete numeric token).
    rc, _o, _e = run_guard("gh pr merge 265abc --squash --match-head-commit " + VSHA, marker_active=True,
                           channel="stdin", merge_token=_tok(pr=265, sha=VSHA))
    expect("piece-b: pr token '265abc' (not a clean number) -> BLOCK", rc, "block")
    # pr OMITTED / unparsable -> deny on doubt.
    rc, _o, _e = run_guard("gh pr merge --squash --match-head-commit " + VSHA, marker_active=True,
                           channel="stdin", merge_token=_tok(pr=265, sha=VSHA))
    expect("piece-b: pr omitted from merge command -> BLOCK (deny on doubt)", rc, "block")
    # A "merge <token.pr>" string inside a QUOTED arg (gh's --body) must NOT be scraped
    # as the PR while gh merges a DIFFERENT positional pr (Codoki #268 High: the extract
    # must anchor to the actual invocation, not any 'merge N' substring).
    rc, _o, _e = run_guard('gh pr merge --body "merge 265" 999 --match-head-commit ' + VSHA,
                           marker_active=True, channel="stdin", merge_token=_tok(pr=265, sha=VSHA))
    expect("piece-b: quoted 'merge 265' in --body, real pr 999 -> BLOCK (no scrape)", rc, "block")

    # ---- #312: tmux is no longer required to run a GATED session -------------------------
    # This block exists because the guard's OWN suite previously had ZERO coverage of the
    # session-key contract: reverting the derivation to tmux-only left this file PASSING, so a
    # maintainer editing the deny authority and running the deny authority's tests saw green
    # while the Tier-2 gate was off. Cross-LANGUAGE agreement is pinned in
    # test-orchestrate-setup.py; what is pinned HERE is the guard's own gating BEHAVIOR.
    MERGE_CMD = "gh " + "pr " + "merge 42 --squash"   # built here, never on a Bash line

    rc, _o, _e = run_guard(MERGE_CMD, marker_active=True, channel="stdin",
                           tmux=None, ccsid="sess-aaaa-bbbb")
    expect("#312: non-tmux session + ccsid-keyed marker -> Tier-2 BLOCK (gated without tmux)",
           rc, "block")
    rc, _o, _e = run_guard("ls -la", marker_active=True, channel="stdin",
                           tmux=None, ccsid="sess-aaaa-bbbb")
    expect("#312: non-tmux marker-active session, benign command -> allow (no over-blocking)",
           rc, "allow")
    rc, _o, _e = run_guard(MERGE_CMD, marker_active=False, channel="stdin",
                           tmux=None, ccsid="sess-aaaa-bbbb")
    expect("#312: ccsid session with NO marker -> allow (solo/non-marker is never gated)",
           rc, "allow")
    # THE ESCAPE HATCH: the maintainer's separate terminal is a DIFFERENT Claude Code session,
    # so its ccsid differs and another session's marker must never gate it.
    rc, _o, _e = run_guard(MERGE_CMD, marker_active=False, channel="stdin",
                           tmux=None, ccsid="a-different-session",
                           foreign_keys=("/tmp/tmux-other,9,9",))
    expect("#312: a DIFFERENT session's marker does NOT gate this one (escape hatch intact)",
           rc, "allow")
    # Neither identifier -> no key -> nothing to look up -> never gated (fail closed).
    rc, _o, _e = run_guard(MERGE_CMD, marker_active=False, channel="stdin",
                           tmux=None, ccsid=None)
    expect("#312: no $TMUX and no ccsid -> no key -> not gated (fail closed)", rc, "allow")
    # A stale ccsid marker must expire exactly like a stale tmux one.
    rc, _o, _e = run_guard(MERGE_CMD, marker_active=False, channel="stdin",
                           tmux=None, ccsid="sess-stale", stale_self=True)
    expect("#312: STALE ccsid marker (older than TTL) -> not gated", rc, "allow")
    # BOTH identifiers present: $TMUX wins for arming, and the tmux-keyed marker still gates.
    rc, _o, _e = run_guard(MERGE_CMD, marker_active=True, channel="stdin",
                           tmux=DEFAULT_TMUX, ccsid="sess-ignored")
    expect("#312: both identifiers -> tmux key wins and still gates", rc, "block")
    # THE ARM/CHECK ASYMMETRY (the reason marker_active scans EVERY candidate, not just the
    # first-precedence one): `up` runs as a Bash tool call, so its env is command-controllable
    # (`env -u TMUX ... up`) while the guard's never is. A session that armed under ccsid while
    # the guard holds a real $TMUX must STILL be gated - first-precedence alone would look only
    # under the tmux key, find nothing, and silently allow the merge.
    with tempfile.TemporaryDirectory() as _td312:
        _fd312 = os.path.join(_td312, "orchestrate-floor.d"); os.makedirs(_fd312)
        open(os.path.join(_fd312, "ccsid_" + _key("armed-via-ccsid")), "w").close()
        _env312 = dict(os.environ)
        _env312.update({"ORCHESTRATE_FLOOR_DIR": _fd312, "ORCHESTRATE_FLOOR_TTL_HOURS": "24",
                        "TMUX": DEFAULT_TMUX, "CLAUDE_CODE_SESSION_ID": "armed-via-ccsid"})
        _env312.pop("TOOL_INPUT", None)
        _p312 = subprocess.run(["bash", GUARD], env=_env312, capture_output=True, text=True,
                               input=json.dumps({"tool_name": "Bash",
                                                 "tool_input": {"command": MERGE_CMD}}), timeout=15)
        expect("#312: marker armed under ccsid but guard sees $TMUX -> STILL gated "
               "(arm/check asymmetry closed)", _p312.returncode, "block")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print(f"All {len(CASES)} allow/block cases + P3-A refcount/key-contract cases "
          f"+ #312 session-key cases + {len(HARD_DENY) + 1} regressions passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
