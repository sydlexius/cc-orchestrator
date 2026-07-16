#!/usr/bin/env python3
"""Proof harness for orchestrate-steer.sh (the WARN-level steering hook, #95).

Asserts the five advisory rules, through BOTH input channels (stdin JSON, $TOOL_INPUT env):
  (1) MID-RUN CANONICAL EDIT (marker-gated): an Edit/Write of a canonical file (SKILL.md,
      templates/*, orchestrate-guard.sh, orchestrate-steer.sh) WARNs only while THIS session's
      marker is fresh; never blocks (exit 0). A non-canonical path, or a canonical path with no
      active marker, is silent.
  (2) RAW GH-API MUTATION -> WRAPPER (marker-independent): a `gh api` mutation on the command line
      WARNs; a gh-* wrapper invocation, a read-only `gh api` GET, or a non-gh command is silent.
Every case asserts exit 0 (steering NEVER blocks) and the presence/absence of the `STEER:` line.
Run: python3 test-orchestrate-steer.py
"""
import importlib.util
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


def _self_key(tmux, ccsid):
    """The key steer derives for THIS session - mirrors orchestrate-steer.sh's _session_keys()
    precedence (#312): $TMUX wins unprefixed, else 'ccsid_' + sanitized session id, else none."""
    if tmux is not None:
        return _key(tmux)
    if ccsid is not None:
        return "ccsid_" + _key(ccsid)
    return None


def run_steer(tool_input, *, channel, marker_active=False, tmux=DEFAULT_TMUX, ccsid=None,
              ttl_hours=24,
              stale_self=False, tool_name="Bash", session_id=None, read_state_dir=None):
    """Invoke the steer hook. Returns (exit_code, stderr). channel in {'stdin','env'}.
    tool_input is the dict passed as .tool_input (e.g. {'file_path': ...} or {'command': ...}).
    tool_name + session_id populate the stdin TOP-LEVEL fields (the env channel carries neither,
    mirroring the real PreToolUse payload); read_state_dir pins the read-dedup state store so a
    test controls per-session read tracking (Rule 4)."""
    with tempfile.TemporaryDirectory() as td:
        floor_dir = os.path.join(td, "orchestrate-floor.d")
        os.makedirs(floor_dir, exist_ok=True)
        self_key = _self_key(tmux, ccsid)
        if marker_active and self_key is not None:
            open(os.path.join(floor_dir, self_key), "w").close()  # fresh mtime
        if stale_self and self_key is not None:
            p = os.path.join(floor_dir, self_key)
            open(p, "w").close()
            old = time.time() - (ttl_hours + 1) * 3600
            os.utime(p, (old, old))
        env = dict(os.environ)
        env["ORCHESTRATE_FLOOR_DIR"] = floor_dir
        env["ORCHESTRATE_FLOOR_TTL_HOURS"] = str(ttl_hours)
        env["ORCHESTRATE_READ_STATE_DIR"] = read_state_dir or os.path.join(td, "read-state")
        if tmux is None:
            env.pop("TMUX", None)
        else:
            env["TMUX"] = tmux
        # #312 DETERMINISM: steer's marker now falls back to $CLAUDE_CODE_SESSION_ID when $TMUX is
        # absent, and this harness runs INSIDE a real Claude Code session that exports one. Strip it
        # by default so `tmux=None` genuinely means "no key"; pass ccsid= to exercise the fallback.
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        if ccsid is not None:
            env["CLAUDE_CODE_SESSION_ID"] = ccsid
        env.pop("TOOL_INPUT", None)
        stdin_data = ""
        if channel == "stdin":
            payload = {"tool_name": tool_name, "tool_input": tool_input}
            if session_id is not None:
                payload["session_id"] = session_id
            stdin_data = json.dumps(payload)
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

    # ---- Rule 4: read-dedup advisory WARN (marker-independent, #226) ----
    # A 2nd+ Read of a path already read THIS session with UNCHANGED mtime/size warns; the first
    # read, a read after the file changed, a read with no session_id, and a non-Read tool never do.
    with tempfile.TemporaryDirectory() as rtd:
        state_dir = os.path.join(rtd, "read-state")
        target = os.path.join(rtd, "some-file.txt")
        with open(target, "w") as fh:
            fh.write("hello\n")

        def read_call(path, *, sid="sess-A", tname="Read"):
            return run_steer({"file_path": path}, channel="stdin", tool_name=tname,
                             session_id=sid, read_state_dir=state_dir)

        rc1, e1 = read_call(target)
        check("read-dedup: 1st Read of a path -> silent (records state)", rc1 == 0 and not warned(e1))
        rc2, e2 = read_call(target)
        check("read-dedup: 2nd Read of an UNCHANGED path -> WARN, exit 0", rc2 == 0 and warned(e2))
        rc3, e3 = read_call(target)
        check("read-dedup: 3rd unchanged Read still WARNs (idempotent)", rc3 == 0 and warned(e3))

        # After the file changes (newer mtime), the re-read is legitimate -> silent, then re-arms.
        newer = time.time() + 5
        os.utime(target, (newer, newer))
        rc4, e4 = read_call(target)
        check("read-dedup: Read after mtime change -> silent (content changed, legit re-read)",
              rc4 == 0 and not warned(e4))
        rc5, e5 = read_call(target)
        check("read-dedup: next unchanged Read after the change WARNs again", rc5 == 0 and warned(e5))

        # ACCEPTED FP (F30-class, fail-safe): a same-mtime + same-SIZE change (a modification within
        # the prior read's 1-second stat granularity) is indistinguishable from an unchanged file, so
        # the re-read draws a spurious advisory WARN. Documented, not a bug (a nudge, never a deny).
        orig = os.stat(target).st_mtime
        with open(target, "w") as fh:
            fh.write("world\n")            # same length as "hello\n" -> unchanged size
        os.utime(target, (orig, orig))     # force mtime back -> same fingerprint despite new content
        rcFP, eFP = read_call(target)
        check("read-dedup: same-second same-size change -> spurious WARN (accepted F30-class FP)",
              rcFP == 0 and warned(eFP))
        newer2 = time.time() + 9           # move past the collision window for the remaining cases
        os.utime(target, (newer2, newer2))
        read_call(target)                  # re-arm the fingerprint for this session

        # A different session_id does not inherit session A's read history.
        rc6, e6 = read_call(target, sid="sess-B")
        check("read-dedup: first Read in a DIFFERENT session -> silent (per-session state)",
              rc6 == 0 and not warned(e6))

        # No session_id -> cannot track -> never warns (fail-open), even on a repeat read.
        run_steer({"file_path": target}, channel="stdin", tool_name="Read", read_state_dir=state_dir)
        rc7, e7 = run_steer({"file_path": target}, channel="stdin", tool_name="Read",
                            read_state_dir=state_dir)
        check("read-dedup: no session_id -> silent even on a repeat Read", rc7 == 0 and not warned(e7))

        # A non-Read tool carrying a file_path (env channel / no tool_name) never triggers dedup.
        run_steer({"file_path": target}, channel="stdin", tool_name="Edit",
                  session_id="sess-C", read_state_dir=state_dir)
        rcE, eE = run_steer({"file_path": target}, channel="stdin", tool_name="Edit",
                            session_id="sess-C", read_state_dir=state_dir)
        check("read-dedup: repeated Edit (not Read) -> no read-dedup WARN", rcE == 0 and not warned(eE))

        # HARDENING (CR/Codoki review-round): a pre-existing, owned-but-group/other-writable state dir
        # is forced to 700, so we never write fingerprints into a dir others can symlink/clobber in.
        gw_dir = os.path.join(rtd, "group-writable-state")
        os.makedirs(gw_dir)
        os.chmod(gw_dir, 0o770)
        run_steer({"file_path": target}, channel="stdin", tool_name="Read",
                  session_id="sess-GW", read_state_dir=gw_dir)
        check("read-dedup: pre-existing group-writable state dir is forced to 700",
              (os.stat(gw_dir).st_mode & 0o777) == 0o700)

        # A nonexistent / unstattable path cannot be fingerprinted -> silent, never warns.
        ghost = os.path.join(rtd, "does-not-exist.txt")
        run_steer({"file_path": ghost}, channel="stdin", tool_name="Read",
                  session_id="sess-D", read_state_dir=state_dir)
        rcG, eG = run_steer({"file_path": ghost}, channel="stdin", tool_name="Read",
                            session_id="sess-D", read_state_dir=state_dir)
        check("read-dedup: unstattable path -> silent on repeat (fail-open)", rcG == 0 and not warned(eG))

    # REGRESSION (#226): a Read of a CANONICAL file must NOT trip the Rule-1 canonical-edit WARN
    # (tool_name=='Read' gates Rule 1 off) - reading SKILL.md mid-run is fine; only EDITING warns.
    with tempfile.TemporaryDirectory() as rtd2:
        rcC, eC = run_steer({"file_path": "/home/u/repo/skills/orchestrate/SKILL.md"}, channel="stdin",
                            tool_name="Read", session_id="sess-R", marker_active=True,
                            read_state_dir=os.path.join(rtd2, "s"))
        check("read-dedup: Read of a canonical file + marker -> no canonical-EDIT warn (tool_name=Read)",
              rcC == 0 and "do not edit mid-run" not in eC)

    # Empty payload -> silent, exit 0 (fail-open).
    rc, err = run_steer({}, channel="stdin")
    check("empty tool_input -> silent, exit 0 (fail-open)", rc == 0 and not warned(err))

    # --- #287: THE ADVISORY INVARIANT (mechanically pinned, never assumed) ---------------------
    # /prep-pr Step 4a grants this script the CHEAP review tier (one multi-lens pass instead of the
    # full K=2 loop) on the strength of ONE property: it is ADVISORY -- it cannot block a tool call.
    # Claude Code blocks only on a nonzero exit (2) or a stdout `permissionDecision: deny`, so the
    # property is: no nonzero exit on a live path, and NO STDOUT AT ALL.
    #
    # This test IS the tier's premise. Without it, "steer is advisory" is a claim in a comment, and
    # the day a diff adds an `exit 2` is precisely the day that diff gets reviewed at the cheap tier.
    # Deny-on-doubt applied to rigor: if this ever goes red, the tier must revert to deny-authority.
    print("\n== #287: the ADVISORY INVARIANT that earns this script the cheap review tier ==")
    with open(STEER) as fh:
        src = fh.read()
    # Strip the --self-test block: its nonzero exits are inert (the hook is wired with NO args, so
    # the self-test branch is unreachable in production).
    # NB: match the block-closing `fi` at COLUMN 0 -- `ln.strip() == "fi"` also matches the NESTED
    # `fi`s inside the self-test, which would end the strip early and leave its inert `exit 1` in the
    # "live" source (a false failure; it bit this test).
    # Close the block on a `fi` at the SAME INDENT as its opening `if` -- not at column 0. Matching
    # column 0 assumes the self-test is never nested; if it is ever moved inside a function, its `fi`
    # is indented, `in_selftest` stays true for the REST OF THE FILE, and every live-path exit after
    # it drops out of the scan -- a FALSE GREEN on the very invariant this test exists to guard
    # (CodeRabbit, PR #291). Matching a bare `ln.strip() == "fi"` is the opposite failure: it stops
    # early on a NESTED fi and leaves the self-test's inert `exit 1` in the live source (a false RED,
    # which bit this test during implementation). Indent-matching avoids both.
    live, in_selftest, selftest_indent = [], False, None
    for ln in src.split("\n"):
        if "--self-test" in ln and ln.lstrip().startswith("if "):
            in_selftest = True
            selftest_indent = len(ln) - len(ln.lstrip())
        if in_selftest:
            if ln.strip() == "fi" and (len(ln) - len(ln.lstrip())) == selftest_indent:
                in_selftest = False
            continue
        live.append(ln)
    live_src = "\n".join(live)
    bad_exits = re.findall(r'(?:^|[^A-Za-z_])exit\s+([1-9]\d*)', live_src)
    check("#287 ADVISORY INVARIANT: no nonzero exit on any live path (it cannot DENY a tool call)",
          not bad_exits)

    # No stdout on ANY path: every emission must be >&2. A stdout write is how a PreToolUse hook
    # returns a permissionDecision, so stdout is the other way this script could become blocking.
    _repo = os.path.dirname(os.path.abspath(__file__))
    payloads = [
        {"tool_name": "Bash", "tool_input": {"command": "gh api -X PATCH repos/o/r/issues/1"}},
        {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(_repo, "scripts/safe-push.sh")}},
        {"tool_name": "Agent", "tool_input": {"run_in_background": False}},
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        {"tool_name": "Agent", "tool_input": {}},
    ]
    stdout_clean, rc_clean = True, True
    for pl in payloads:
        with tempfile.TemporaryDirectory() as td:
            fd = os.path.join(td, "orchestrate-floor.d"); os.makedirs(fd)
            open(os.path.join(fd, _key(DEFAULT_TMUX)), "w").close()   # marker ACTIVE (worst case)
            env = dict(os.environ, ORCHESTRATE_FLOOR_DIR=fd, TMUX=DEFAULT_TMUX,
                       ORCHESTRATE_READ_STATE_DIR=os.path.join(td, "rs"))
            env.pop("TOOL_INPUT", None)
            pr = subprocess.run([STEER], input=json.dumps(pl), env=env,
                                capture_output=True, text=True, timeout=10)
            if pr.stdout != "":
                stdout_clean = False
            if pr.returncode != 0:
                rc_clean = False
    check("#287 ADVISORY INVARIANT: writes NOTHING to stdout on any path "
          "(a stdout permissionDecision is the other way a hook can block)", stdout_clean)
    check("#287 ADVISORY INVARIANT: exits 0 on every path, marker ACTIVE, all rule surfaces",
          rc_clean)

    # --- #284: the canonical-edit rule must cover the DEPLOYED HELPERS + commands/ --------------
    # Reproduced live: a marker-active mid-run edit of safe-push.sh was SILENT, so the ONE mechanism
    # whose job is to say "log feedback, do not edit mid-run" missed the exact file that motivated
    # the rule. These are canonical-source files by the same argument as the guard.
    print("\n== #284: canonical matcher covers the deployed helpers + commands/ ==")
    repo = os.path.dirname(os.path.abspath(__file__))
    for helper in ("scripts/safe-push.sh", "scripts/pr-unreplied-comments.sh",
                   "scripts/gh-comment.sh", "scripts/gate-runner.py", "commands/prep-pr.md"):
        p = os.path.join(repo, helper)
        rc, err = run_steer({"file_path": p}, channel="stdin", tool_name="Edit", marker_active=True)
        check(f"#284: marker-active Edit of {helper} -> WARN", rc == 0 and warned(err))
        # Marker-gating must survive: no marker -> silent (a solo session is never nagged).
        rc, err = run_steer({"file_path": p}, channel="stdin", tool_name="Edit", marker_active=False)
        check(f"#284: NO marker, Edit of {helper} -> silent", rc == 0 and not warned(err))

    # LOCKSTEP (the guard against the exact bug round 1 caught): EVERY Option-A-deployed helper in
    # orchestrate-setup.py's HELPER_NAMES must be canonical to the steer matcher. The first cut of this
    # matcher drifted 4 helpers behind that set, so a mid-run `issue-watch.sh` edit stayed SILENT --
    # bug #283 verbatim, for a different file. Without this test the list re-drifts the next time a
    # helper is added.
    # IMPORT the real tuple; do NOT regex it. A regex here is how this test became THEATER once
    # already: `HELPER_NAMES\s*=\s*[\(\[](.*?)[\)\]]` is non-greedy and terminated at the first `)`,
    # which lands inside an inline comment `(#216).` -- so it captured 12 of 15 names and silently
    # dropped ship-gate-preflight.sh, issue-watch.sh and gh-react.sh, TWO of which were the very
    # helpers whose omission was the bug this test exists to catch. Mutation-proved: deleting
    # issue-watch.sh from the matcher left the harness GREEN. Import the module and pin the EXACT
    # count, so a truncated parse or a newly-added helper cannot pass unnoticed.
    spec = importlib.util.spec_from_file_location(
        "_osetup", os.path.join(repo, "scripts/orchestrate-setup.py"))
    _osetup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_osetup)
    helper_names = list(_osetup.HELPER_NAMES)
    check("#284 lockstep: HELPER_NAMES imported (exact count -- a truncated parse must not pass)",
          len(helper_names) == 15)
    for h in helper_names:
        p = os.path.join(repo, "scripts", h)
        rc, err = run_steer({"file_path": p}, channel="stdin", tool_name="Edit", marker_active=True)
        check(f"#284 lockstep: deployed helper {h} is canonical -> WARN", rc == 0 and warned(err))

    # A symlinked helper (the legacy claude-kit layout) must STILL warn: readlink -f resolves it away
    # from any scripts/ parent, so a resolved-only match would go silent on the exact layout the
    # resolution exists to handle.
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "scripts"))
        real = os.path.join(td, "kit-safe-push.sh")
        open(real, "w").close()
        link = os.path.join(td, "scripts", "safe-push.sh")
        os.symlink(real, link)
        rc, err = run_steer({"file_path": link}, channel="stdin", tool_name="Edit", marker_active=True)
        check("#284: SYMLINKED helper (resolves outside scripts/) still WARNs", rc == 0 and warned(err))

    # A Read of a canonical helper still must NOT nag (the Read carve-out is preserved).
    rc, err = run_steer({"file_path": os.path.join(repo, "scripts/safe-push.sh")},
                        channel="stdin", tool_name="Read", marker_active=True, session_id="sess-284")
    check("#284: marker-active READ of a helper -> no canonical-edit nag", rc == 0 and not warned(err))

    # A non-canonical script must stay silent (the matcher must not swallow the whole repo).
    rc, err = run_steer({"file_path": os.path.join(repo, "test-orchestrate-steer.py")},
                        channel="stdin", tool_name="Edit", marker_active=True)
    check("#284: marker-active Edit of a NON-canonical file (a test harness) -> silent",
          rc == 0 and not warned(err))

    # --- #231: foreground-Agent containment (marker-gated WARN) ---------------------------------
    # The #221 spike proved PreToolUse fires on Agent and the payload carries run_in_background.
    # CRITICAL (from the 45 captured live payloads): the field is ABSENT, not false, when the caller
    # omits it -- and agents DEFAULT TO BACKGROUND. So a naive falsy check would warn on 13/45 legal
    # background spawns. Demand the EXACT shape: warn only on an explicit `false`.
    print("\n== #231: foreground-Agent containment ==")
    rc, err = run_steer({"description": "x", "prompt": "y", "run_in_background": False},
                        channel="stdin", tool_name="Agent", marker_active=True)
    check("#231: marker-active Agent with run_in_background=false -> WARN", rc == 0 and warned(err))

    rc, err = run_steer({"description": "x", "prompt": "y"},
                        channel="stdin", tool_name="Agent", marker_active=True)
    check("#231: marker-active Agent with run_in_background ABSENT (defaults background) -> silent",
          rc == 0 and not warned(err))

    rc, err = run_steer({"description": "x", "prompt": "y", "run_in_background": True},
                        channel="stdin", tool_name="Agent", marker_active=True)
    check("#231: marker-active Agent with run_in_background=true -> silent", rc == 0 and not warned(err))

    rc, err = run_steer({"description": "x", "prompt": "y", "run_in_background": False},
                        channel="stdin", tool_name="Agent", marker_active=False)
    check("#231: NO marker, foreground Agent -> silent (solo session is never gated)",
          rc == 0 and not warned(err))

    # The WARN must name the remedy (a NAMED async teammate), not merely scold.
    _, err = run_steer({"description": "x", "prompt": "y", "run_in_background": False},
                       channel="stdin", tool_name="Agent", marker_active=True)
    check("#231: the WARN names the remedy (named async teammate)",
          "name" in err.lower() and ("async" in err.lower() or "background" in err.lower()))

    # A non-Agent tool carrying run_in_background (e.g. a Bash background call) must NOT trigger it.
    rc, err = run_steer({"command": "ls", "run_in_background": False},
                        channel="stdin", tool_name="Bash", marker_active=True)
    check("#231: Bash with run_in_background=false -> silent (Agent-only rule)",
          rc == 0 and not warned(err))

    # TYPE-EXACTNESS: only a JSON boolean false warns. The STRING "false" and 0 are NOT false -- the
    # matcher must not collapse types (the "demand the exact shape" rule the floor-matcher work paid
    # for). An earlier tostring-based form warned on the string, which is the shape a hand-built or
    # proxied payload could carry.
    rc, err = run_steer({"description": "x", "run_in_background": "false"},
                        channel="stdin", tool_name="Agent", marker_active=True)
    check('#231: run_in_background as the STRING "false" -> silent (type-exact)',
          rc == 0 and not warned(err))
    rc, err = run_steer({"description": "x", "run_in_background": 0},
                        channel="stdin", tool_name="Agent", marker_active=True)
    check("#231: run_in_background=0 -> silent (type-exact, not falsy)", rc == 0 and not warned(err))
    rc, err = run_steer({"description": "x", "run_in_background": None},
                        channel="stdin", tool_name="Agent", marker_active=True)
    check("#231: run_in_background=null -> silent", rc == 0 and not warned(err))

    # FAIL-SILENT-OPEN on the Agent path with jq unavailable: the hook must never block a spawn.
    #
    # The PATH must contain `cat` but NOT `jq`. An earlier version of this case symlinked ONLY bash --
    # which meant `cat` was missing too, so the script's `stdin_json=$(cat)` returned nothing and it
    # early-exited on the empty-payload fail-open BEFORE ever reaching jq. It passed while proving
    # NOTHING about jq-absence (caught by Copilot on PR #286). Provide cat + the other coreutils the
    # hook may touch, and withhold ONLY jq, so the jq-missing branch is the one actually exercised.
    with tempfile.TemporaryDirectory() as jqless:
        for tool in ("bash", "cat", "basename", "readlink", "mkdir", "chmod", "cksum", "cut", "printf"):
            for src in (f"/bin/{tool}", f"/usr/bin/{tool}"):
                if os.path.exists(src):
                    os.symlink(src, os.path.join(jqless, tool))
                    break
        check("#231: jq-absent probe has cat but NOT jq (else the case proves nothing)",
              os.path.exists(os.path.join(jqless, "cat"))
              and not os.path.exists(os.path.join(jqless, "jq")))
        env = dict(os.environ, PATH=jqless)
        payload = json.dumps({"tool_name": "Agent", "session_id": "s",
                              "tool_input": {"run_in_background": False}})
        p = subprocess.run(["/bin/bash", STEER], input=payload, capture_output=True, text=True,
                           timeout=10, env=env)
        # exit 0 AND genuinely silent: no STEER line and no stderr noise at all (the fail-silent-open
        # contract is silence, not merely a zero exit).
        check("#231: jq absent -> exit 0, silent (fail-open, never blocks the spawn)",
              p.returncode == 0 and not warned(p.stderr) and p.stderr.strip() == "")

    # ---- #312: the MARKER-GATED rules must fire in a NON-TMUX gated session --------------
    # steer's marker_active checked ONLY $TMUX, so once #312 let a session arm outside tmux,
    # every marker-gated rule (1 = mid-run canonical edit, 5 = foreground Agent spawn) went
    # SILENTLY DEAD for the whole newly-supported mode - the nudges simply never fired, with
    # no signal. Advisory tier, so a missing nudge is the blast radius, not a deny hole.
    CANON = "/Users/jesse/Developer/cc-orchestrator/scripts/safe-push.sh"
    rc, err = run_steer({"file_path": CANON}, channel="stdin", tool_name="Edit",
                        marker_active=True, tmux=None, ccsid="steer-ccsid-session")
    check("#312 steer: canonical edit in a ccsid-gated session -> rule 1 WARNs (was silent)",
          rc == 0 and warned(err))
    rc, err = run_steer({"run_in_background": False}, channel="stdin", tool_name="Agent",
                        marker_active=True, tmux=None, ccsid="steer-ccsid-session")
    check("#312 steer: foreground Agent in a ccsid-gated session -> rule 5 WARNs (was silent)",
          rc == 0 and warned(err))
    # Still silent where it should be: no marker, and no identifier at all.
    rc, err = run_steer({"file_path": CANON}, channel="stdin", tool_name="Edit",
                        marker_active=False, tmux=None, ccsid="steer-ccsid-session")
    check("#312 steer: ccsid session with NO marker -> silent (marker-gated, not always-on)",
          rc == 0 and not warned(err))
    rc, err = run_steer({"file_path": CANON}, channel="stdin", tool_name="Edit",
                        marker_active=True, tmux=None, ccsid=None)
    check("#312 steer: NEITHER identifier -> no key -> silent", rc == 0 and not warned(err))
    # A stale ccsid marker expires exactly like a stale tmux one.
    rc, err = run_steer({"file_path": CANON}, channel="stdin", tool_name="Edit",
                        marker_active=False, stale_self=True, tmux=None, ccsid="steer-stale")
    check("#312 steer: STALE ccsid marker -> silent (TTL honored per candidate)",
          rc == 0 and not warned(err))

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
