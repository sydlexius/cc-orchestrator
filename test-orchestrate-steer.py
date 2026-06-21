#!/usr/bin/env python3
"""Proof harness for orchestrate-steer.sh (the WARN-level steering hook, #95).

Asserts the two advisory rules, through BOTH input channels (stdin JSON, $TOOL_INPUT env):
  (1) MID-RUN CANONICAL EDIT (marker-gated): an Edit/Write of a canonical file (SKILL.md,
      templates/*, orchestrate-guard.sh, orchestrate-steer.sh) WARNs only while THIS session's
      marker is fresh; never blocks (exit 0). A non-canonical path, or a canonical path with no
      active marker, is silent.
  (2) RAW GH-API MUTATION -> WRAPPER (marker-independent): a `gh api` mutation on the command line
      WARNs; a gh-* wrapper invocation, a read-only `gh api` GET, or a non-gh command is silent.
Every case asserts exit 0 (steering NEVER blocks) and the presence/absence of the `STEER:` line.
Run: python3 test-orchestrate-steer.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

STEER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "orchestrate-steer.sh")
DEFAULT_TMUX = "/tmp/tmux-test,1,0"
FAILS = []


def _key(tmux):
    """Mirror the steer/guard $TMUX sanitization EXACTLY (byte-mode)."""
    return re.sub(rb'[^A-Za-z0-9]', b'_', tmux.encode("utf-8", "surrogateescape")).decode("ascii")


def run_steer(tool_input, *, channel, marker_active=False, tmux=DEFAULT_TMUX, ttl_hours=24,
              stale_self=False):
    """Invoke the steer hook. Returns (exit_code, stderr). channel in {'stdin','env'}.
    tool_input is the dict passed as .tool_input (e.g. {'file_path': ...} or {'command': ...})."""
    with tempfile.TemporaryDirectory() as td:
        floor_dir = os.path.join(td, "orchestrate-floor.d")
        os.makedirs(floor_dir, exist_ok=True)
        if marker_active and tmux is not None:
            open(os.path.join(floor_dir, _key(tmux)), "w").close()  # fresh mtime
        if stale_self and tmux is not None:
            p = os.path.join(floor_dir, _key(tmux))
            open(p, "w").close()
            old = time.time() - (ttl_hours + 1) * 3600
            os.utime(p, (old, old))
        env = dict(os.environ)
        env["ORCHESTRATE_FLOOR_DIR"] = floor_dir
        env["ORCHESTRATE_FLOOR_TTL_HOURS"] = str(ttl_hours)
        if tmux is None:
            env.pop("TMUX", None)
        else:
            env["TMUX"] = tmux
        env.pop("TOOL_INPUT", None)
        stdin_data = ""
        if channel == "stdin":
            stdin_data = json.dumps({"tool_name": "Bash", "tool_input": tool_input})
        elif channel == "env":
            env["TOOL_INPUT"] = json.dumps(tool_input)
        p = subprocess.run([STEER], input=stdin_data, env=env,
                           capture_output=True, text=True, timeout=5)
        return p.returncode, p.stderr


def warned(stderr):
    return "STEER:" in stderr


def check(label, cond):
    status = "ok" if cond else "FAIL"
    if not cond:
        FAILS.append(label)
    print(f"  [{status}] {label}")


def both_channels(tool_input, **kw):
    """Run a case through stdin AND env; return (rc_ok_both, warned_both, silent_both)."""
    results = [run_steer(tool_input, channel=ch, **kw) for ch in ("stdin", "env")]
    rc_ok = all(rc == 0 for rc, _ in results)
    warned_all = all(warned(err) for _, err in results)
    silent_all = all(not warned(err) for _, err in results)
    return rc_ok, warned_all, silent_all


def main():
    print("orchestrate-steer.sh harness")

    # --self-test passes (marker-independent gh-api rule).
    p = subprocess.run([STEER, "--self-test"], capture_output=True, text=True, timeout=5)
    check("--self-test exits 0 and reports PASS", p.returncode == 0 and "PASS" in p.stdout)

    # ---- Rule 1: mid-run canonical edit (marker-gated) ----
    CANON = [
        "/home/u/repo/skills/orchestrate/SKILL.md",
        "/home/u/repo/skills/orchestrate/templates/implementer-charter.md",
        "/home/u/.claude/scripts/orchestrate-guard.sh",
        "/home/u/repo/scripts/orchestrate-steer.sh",
    ]
    for path in CANON:
        rc_ok, warned_all, _ = both_channels({"file_path": path}, marker_active=True)
        check(f"canonical edit + marker active -> WARN, exit 0 ({os.path.basename(path)})",
              rc_ok and warned_all)
        # Same path, NO active marker -> silent (the lead's own session is the only gated context).
        rc_ok, _, silent_all = both_channels({"file_path": path}, marker_active=False)
        check(f"canonical edit + NO marker -> silent, exit 0 ({os.path.basename(path)})",
              rc_ok and silent_all)

    # A stale (expired) marker is NOT active -> silent.
    rc_ok, _, silent_all = both_channels(
        {"file_path": "/home/u/repo/skills/orchestrate/SKILL.md"}, stale_self=True)
    check("canonical edit + STALE marker -> silent (expired marker is inactive)", rc_ok and silent_all)

    # No $TMUX (solo session) -> never gated, even on a canonical path.
    rc_ok, _, silent_all = both_channels(
        {"file_path": "/home/u/repo/skills/orchestrate/SKILL.md"}, marker_active=True, tmux=None)
    check("canonical edit + no $TMUX (solo) -> silent (never an orchestrate session)",
          rc_ok and silent_all)

    # Non-canonical paths are silent regardless of marker state.
    for path in ["/home/u/repo/scripts/orchestrate-resources.py",
                 "/home/u/repo/README.md",
                 "/home/u/repo/skills/orchestrate/design/DESIGN-deterministic-floor.md",
                 "/tmp/some-other-file.md"]:
        rc_ok, _, silent_all = both_channels({"file_path": path}, marker_active=True)
        check(f"non-canonical edit -> silent even with marker ({os.path.basename(path)})",
              rc_ok and silent_all)

    # ---- Rule 2: raw gh-api mutation -> wrapper (marker-independent) ----
    MUTATIONS = [
        "gh api -X PATCH repos/o/r/issues/1 -f state=closed",
        "gh api --method DELETE repos/o/r/git/refs/heads/x",
        "gh api repos/o/r/issues -f title=hi",
        "gh api repos/o/r/x -F body=@file",
        "gh api repos/o/r/x --field name=v",
        "gh api repos/o/r/x --raw-field q=v",
        "gh api repos/o/r/x --input payload.json",
        "cd /repo && gh api -X POST repos/o/r/labels -f name=bug",
        # PR #136 F6: a COMPOUND command that runs a gh-* wrapper AND a raw `gh api` mutation must
        # WARN - the old global `gh-*.sh` exemption wrongly suppressed it. The bare `gh api -X` is
        # present, so the warn must fire even though a wrapper token also appears on the line.
        "bash gh-comment.sh 5 hi && gh api -X PATCH repos/o/r/issues/1 -f state=closed",
        "scripts/gh-resolve-thread.sh T_1 && gh api --method DELETE repos/o/r/git/refs/heads/x",
    ]
    for c in MUTATIONS:
        # Marker-independent: fires both with and without a marker.
        rc_ok, warned_all, _ = both_channels({"command": c}, marker_active=False)
        check(f"raw gh-api mutation -> WARN, exit 0 ({c[:42]})", rc_ok and warned_all)

    # PR #136 F6 regression: a wrapper-ALONE invocation (no bare `gh api`) stays SILENT - dropping
    # the global exemption is safe because the bare-`gh` check needs `gh` + space/EOL, and the char
    # after `gh` in `gh-comment.sh` is `-`, not a boundary. (Also covered by SILENT_CMDS below.)
    for c in ["bash gh-comment.sh 5 hi && echo done",
              "scripts/gh-codeql-dismiss.sh 12 && scripts/gh-resolve-thread.sh T_1"]:
        rc_ok, _, silent_all = both_channels({"command": c}, marker_active=True)
        check(f"F6 regression: wrapper-alone compound stays silent ({c[:42]})", rc_ok and silent_all)

    # Silent: a gh-* wrapper invocation (the sanctioned path), a read-only GET, non-gh commands.
    SILENT_CMDS = [
        "bash ~/.claude/scripts/gh-comment.sh 5 'hi'",
        "scripts/gh-codeql-dismiss.sh 12",
        "gh-api-get.sh repos/o/r/pulls/5",
        "gh api repos/o/r/pulls/5",                       # read-only GET (no mutation flag)
        "gh pr view 5 --json state",                      # not `gh api`
        "echo hello",
    ]
    # ACCEPTED LIMITATION (mirrors the guard's F30 prose false-positives): a command that QUOTES the
    # literal `gh api -X ...` in an argument (e.g. `git commit -m "...gh api -X PATCH..."`) DOES trip
    # the whole-line grep. Harmless here - it is a WARN (advisory, exit 0), recoverable by rewording.
    for c in SILENT_CMDS:
        rc_ok, _, silent_all = both_channels({"command": c}, marker_active=True)
        check(f"non-mutation / wrapper / non-gh -> silent ({c[:42]})", rc_ok and silent_all)

    # ---- Rule 3: raw gh pr mutation -> canonical path (marker-independent, #159) ----
    # Reframed as canonical-STEERING, NOT creep prevention: every high-traffic gh pr subcommand is
    # already allow-listed (it never prompts), so the hook only nudges the two with a real canonical
    # target: `gh pr comment` -> reply-comment.sh/gh-comment.sh ; `gh pr create` -> /prep-pr.
    GH_PR_MUTATIONS = [
        'gh pr comment 5 --body "hi"',
        "gh pr comment -b x 5",
        "gh pr create --base main --title t --body b",
        "gh pr create --fill",
        "cd /repo && gh pr create --draft",
    ]
    for c in GH_PR_MUTATIONS:
        rc_ok, warned_all, _ = both_channels({"command": c}, marker_active=False)
        check(f"raw gh pr mutation -> WARN, exit 0 ({c[:42]})", rc_ok and warned_all)

    # EXCLUDED on purpose -> stay SILENT: merge (floor-denied in marker sessions, the sanctioned
    # prompt-free path in solo - a nag is wrong), edit/ready/close/review (allow-listed lifecycle or
    # no canonical redirect), and every read. Warning these would be pure noise.
    GH_PR_SILENT = [
        "gh pr merge 5 --squash",
        "gh pr edit 5 --add-label x",
        "gh pr ready 5",
        "gh pr close 5",
        "gh pr review 5 --approve",
        "gh pr view 5 --json state",
        "gh pr diff 5",
        "gh pr checks 5",
        "gh pr list --state open",
        "gh pr status",
    ]
    for c in GH_PR_SILENT:
        rc_ok, _, silent_all = both_channels({"command": c}, marker_active=True)
        check(f"excluded gh pr subcommand -> silent ({c[:42]})", rc_ok and silent_all)

    # Wrapper-alone stays silent: the word-boundary on bare `gh` excludes gh-comment.sh /
    # reply-comment.sh (the char after `gh` is `-`, and `comment`/`create` inside those names is not
    # space-delimited), even though those wrappers contain the subcommand word.
    for c in ["bash gh-comment.sh 5 hi && echo done",
              "scripts/reply-comment.sh 5 --file f --line 1 fixed",
              "safe-push.sh my-branch && scripts/gh-resolve-thread.sh T_1"]:
        rc_ok, _, silent_all = both_channels({"command": c}, marker_active=True)
        check(f"gh pr rule: wrapper-alone stays silent ({c[:42]})", rc_ok and silent_all)

    # ACCEPTED FALSE-POSITIVE (mirrors the gh-api F30 class): a gh pr READ compounded with a
    # standalone `create`/`comment` word in an arg trips the whole-line grep. Harmless - advisory
    # WARN, exit 0, reword to silence. Asserted so the behavior stays intentional, not a surprise.
    rc_ok, warned_all, _ = both_channels(
        {"command": "gh pr list && echo create the changelog"}, marker_active=False)
    check("accepted FP: gh pr read + standalone 'create' word -> WARN (documented)",
          rc_ok and warned_all)

    # Empty payload -> silent, exit 0 (fail-open).
    rc, err = run_steer({}, channel="stdin")
    check("empty tool_input -> silent, exit 0 (fail-open)", rc == 0 and not warned(err))

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
