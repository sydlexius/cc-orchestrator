#!/usr/bin/env python3
"""Proof harness for orchestrate-steer.sh (the WARN-level steering hook, #95).

Asserts all seven advisory rules. The command/file rules (1)-(3) run through BOTH input channels
(stdin JSON and $TOOL_INPUT env); rules (4) and (5) need stdin top-level fields (tool_name,
session_id), so they run through stdin only.
  (1) MID-RUN CANONICAL EDIT (marker-gated): an Edit/Write of a canonical file (SKILL.md,
      templates/*, guard/steer, the deployed helpers, commands/*.md) WARNs only while THIS
      session's marker is fresh. A non-canonical path, or no active marker, is silent.
  (2) RAW GH-API MUTATION -> WRAPPER (marker-independent): a raw `gh api` REST mutation or GraphQL
      `mutation` WARNs; a gh-* wrapper, a GET, a GraphQL read, or quoted prose is silent.
  (3) RAW GH PR create/comment/new -> CANONICAL PATH: the word sequence anywhere in the command's
      CODE (including $(...), backticks, `bash -c`/eval scripts and heredocs fed to a shell) WARNs;
      reads and prose are silent.
  (4) REDUNDANT RE-READ (per-session state) and (5) FOREGROUND AGENT (marker-gated).
  (6) PIPED SAFE-PUSH (#432): a safe-push.sh call whose clause is ended by a lone `|` WARNs;
      `||`, a comment, quoted prose, and safe-push as the LAST pipeline command are silent.
  (7) EXPENSIVE GATE PROFILE (#343): a `.gates.toml`-declared var set on a gate/upload WARNs (a
      DOUBLE SPEND with a passing receipt at HEAD); off values, prose and undeclared repos are silent.
Plus the #287 advisory invariant (no nonzero exit, no stdout), robustness on malformed input, and
scan-time bounds. Every case asserts exit 0 (steering NEVER blocks) and the `STEER:` line's
presence/absence.
Run: python3 test-orchestrate-steer.py
"""
import importlib.util
import json
import os
import pty
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
              stale_self=False, tool_name="Bash", session_id=None, read_state_dir=None,
              timeout=5):
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
        try:
            p = subprocess.run([STEER], input=stdin_data, env=env,
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "TIMEOUT (the hook hung; it must never block a tool call)"
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


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                           "-c", "commit.gpgsign=false", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _fixture_repo(root, gates_toml):
    """A real git repo (HEAD needed for the double-spend leg) with an optional .gates.toml."""
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    _git(root, "init", "-q")
    if gates_toml is not None:
        with open(os.path.join(root, ".gates.toml"), "w") as f:
            f.write(gates_toml)
    _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    return _git(root, "rev-parse", "HEAD")


RUN7_STDOUT = []


def run7(command, cwd, *, channel="stdin", path_prefix=None):
    """Rule 7 runs off the payload cwd (stdin) or the process cwd (env channel). Returns
    (rc, stdout, stderr); no marker, no TMUX key (rule 7 is marker-independent)."""
    env = dict(os.environ)
    for k in ("TOOL_INPUT", "TMUX", "CLAUDE_CODE_SESSION_ID"):
        env.pop(k, None)
    if path_prefix:
        env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
    stdin_data = ""
    if channel == "stdin":
        stdin_data = json.dumps({"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}})
    else:
        env["TOOL_INPUT"] = json.dumps({"command": command})
    p = subprocess.run([STEER], input=stdin_data, env=env, cwd=cwd,
                       capture_output=True, text=True, timeout=10)
    RUN7_STDOUT.append((command, p.stdout))   # EVERY rule-7 call feeds the stdout invariant
    return p.returncode, p.stdout, p.stderr


def rule7_cases():
    # ---- Rule 7 (#343): EXPENSIVE GATE PROFILE, opt-in via .gates.toml [steer] ----
    DECL = ('[prep_pr]\ngate = "true"\n\n[steer]\n'
            'expensive_profile_env = ["SW_GATE_FULL", "SW_RACE", "bad name"]\n')
    WARN = [
        "SW_GATE_FULL=1 python3 scripts/gate-runner.py",
        "SW_GATE_FULL=1 ./scripts/pre-push-hook.sh",
        "env SW_GATE_FULL=1 python3 scripts/gate-runner.py",
        "export SW_GATE_FULL=1; python3 scripts/gate-runner.py",
        "export SW_GATE_FULL=yes && safe-push.sh b",
        "SW_GATE_FULL=1 safe-push.sh b",
        "SW_GATE_FULL=1 git push origin b",
        "SW_GATE_FULL=1 git -C . push origin b",
        "SW_RACE=1 python3 scripts/gate-runner.py",              # a second declared var
        "OTHER=1 SW_GATE_FULL=1 python3 scripts/gate-runner.py",
        "SW_GATE_FULL=1 timeout 1800 python3 scripts/gate-runner.py",
        "cd x && SW_GATE_FULL=1 python3 scripts/gate-runner.py",
        "bash -c 'SW_GATE_FULL=1 python3 scripts/gate-runner.py'",
        "SW_GATE_FULL=1 python3 '${CLAUDE_PLUGIN_ROOT}/scripts/gate-runner.py' --receipt r",
        "SW_GATE_FULL=1 \\\n  python3 scripts/gate-runner.py",   # backslash-newline continuation
        # R343-3: a quoted ON value, a mixed word, and an expansion still count as set
        'SW_GATE_FULL="1" python3 scripts/gate-runner.py',
        'SW_GATE_FULL="$V" python3 scripts/gate-runner.py',
        'SW_GATE_FULL=0"" python3 scripts/gate-runner.py',
        'SW_GATE_FULL="00" safe-push.sh b',
        'export SW_GATE_FULL="yes"; python3 scripts/gate-runner.py',
        # an unset inside a CODE frame cannot undo the OUTER export; inside the frame it still flows
        "export SW_GATE_FULL=1; bash -c 'unset SW_GATE_FULL'; python3 scripts/gate-runner.py",
        "bash -c 'export SW_GATE_FULL=1; python3 scripts/gate-runner.py'",
        "eval 'export SW_GATE_FULL=1'; python3 scripts/gate-runner.py",  # eval shares the shell
        # #478 (a): eval opening a code frame from INSIDE $(...) was read as prose (unbounded
        # backward word-scan in codeq()); the export inside it must still flow (eval shares the
        # shell of whichever frame it runs in).
        "x=$(eval 'export SW_GATE_FULL=1'; python3 scripts/gate-runner.py)",
        # #478 (b1): a prefix assignment on the command that OPENS a bash -c frame flows into that
        # child process env even though it is never exported in the parent shell.
        "SW_GATE_FULL=1 bash -c 'python3 scripts/gate-runner.py'",
        # #478 (b2): a leading `{` (brace-group opener) before export must not blind the export scan.
        "{ export SW_GATE_FULL=1; }; python3 scripts/gate-runner.py",
        # #478 E1 (fix round 1): TWO contiguous prefix assignments both seed the child; a
        # transparent wrapper keyword (env) between assignment(s) and the shell name is still
        # skipped over, matching the same treatment CP7 already gives "env" elsewhere in this file.
        "A=1 SW_GATE_FULL=1 bash -c 'python3 scripts/gate-runner.py'",
        "SW_GATE_FULL=1 env bash -c 'python3 scripts/gate-runner.py'",
    ]
    SILENT = [
        "python3 scripts/gate-runner.py",                        # the default profile
        "safe-push.sh b",
        "SW_GATE_FULL=0 python3 scripts/gate-runner.py",
        "SW_GATE_FULL= safe-push.sh b",
        "export SW_GATE_FULL=1; SW_GATE_FULL=0 python3 scripts/gate-runner.py",  # prefix overrides
        "export SW_GATE_FULL=1; unset SW_GATE_FULL; python3 scripts/gate-runner.py",
        "export SW_GATE_FULL=0; safe-push.sh b",
        "SW_GATE_FULL=1; python3 scripts/gate-runner.py",        # not exported: never reaches the gate
        'echo "SW_GATE_FULL=1 python3 scripts/gate-runner.py"',  # quoted prose
        "git commit -m 'SW_GATE_FULL=1 safe-push.sh b'",
        "python3 scripts/gate-runner.py # SW_GATE_FULL=1",       # comment
        "OTHER=1 python3 scripts/gate-runner.py",                # an undeclared var
        "SW_GATE_FULLER=1 python3 scripts/gate-runner.py",       # a name that merely starts alike
        "SW_GATE_FULL=1 go build ./...",                         # not a gate/upload
        "SW_GATE_FULL=1 grep -n x scripts/gate-runner.py",       # gate name not at command position
        # R343-3: a QUOTED off value (empty or 0, as the whole word) is off, as bash sees it
        'SW_GATE_FULL="0" python3 scripts/gate-runner.py',
        'SW_GATE_FULL="" python3 scripts/gate-runner.py',
        "SW_GATE_FULL='' safe-push.sh b",
        "SW_GATE_FULL='0' python3 scripts/gate-runner.py",
        "SW_GATE_FULL=$'0' python3 scripts/gate-runner.py",
        'env SW_GATE_FULL="" python3 scripts/gate-runner.py',
        'export SW_GATE_FULL="0"; safe-push.sh b',
        'export SW_GATE_FULL=1; SW_GATE_FULL="" python3 scripts/gate-runner.py',
        # #478 E2 (fix round 1): the (b2) leading-`{` allowance must not also misread the OFF value.
        "{ export SW_GATE_FULL=0; }; python3 scripts/gate-runner.py",
        # an export inside a CODE frame dies with that process: it never reaches the outer gate
        "bash -c 'export SW_GATE_FULL=1' && python3 scripts/gate-runner.py",
        "x=$(export SW_GATE_FULL=1); python3 scripts/gate-runner.py",
        # #478 (b1): a prefix assignment on a bash -c command is scoped to THAT child process only
        # (the seed is snapshotted and restored on pop) - it must never leak to a LATER clause.
        "SW_GATE_FULL=1 bash -c 'true'; python3 scripts/gate-runner.py",
        # #478 (b1) off-value: a prefix assignment of 0 must not turn the gate on.
        "SW_GATE_FULL=0 bash -c 'python3 scripts/gate-runner.py'",
        # #478 E1 (fix round 1, regression): a NAME=value-shaped word that is merely an ARGUMENT to
        # an earlier, unrelated command in the same clause is not a bash prefix assignment and must
        # not seed the child. Both went silent -> WARN under the pre-fix-round-1 whole-buffer scan.
        "find . -name SW_GATE_FULL=1 -exec bash -c 'python3 scripts/gate-runner.py' \\;",
        "printf SW_GATE_FULL=1 bash -c 'python3 scripts/gate-runner.py'",
    ]
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "decl"); os.makedirs(repo)
        head = _fixture_repo(repo, DECL)
        stdout_clean = True
        for c in WARN:
            rc, out, err = run7(c, os.path.join(repo, "sub"))     # cwd BELOW the root: walk-up
            stdout_clean = stdout_clean and out == ""
            check(f"#343: declared expensive profile -> WARN, exit 0 ({c[:50]!r})",
                  rc == 0 and warned(err) and "Double spend" not in err)
        for c in SILENT:
            rc, out, err = run7(c, repo)
            stdout_clean = stdout_clean and out == ""
            check(f"#343: default / off / prose -> silent ({c[:50]!r})", rc == 0 and not warned(err))
        rc, out, err = run7(WARN[0], repo, channel="env")
        check("#343: $TOOL_INPUT channel (no payload cwd) reads the process cwd -> WARN",
              rc == 0 and warned(err))

        # NO DECLARATION -> silent everywhere (repo-agnostic, zero behavior change).
        for name, toml in (("nosteer", '[prep_pr]\ngate = "true"\n'), ("nofile", None),
                           ("broken", "[steer\nexpensive_profile_env = [\n"),
                           ("wrongtype", "[steer]\nexpensive_profile_env = 1\n")):
            r = os.path.join(td, name); os.makedirs(r); _fixture_repo(r, toml)
            quiet = all(rc == 0 and not warned(err)
                        for rc, _, err in (run7(c, r) for c in WARN[:6]))
            check(f"#343: no usable declaration ({name}) -> silent on every WARN shape", quiet)
        # R343-4: a .gates.toml that never names the key skips the python3 fork entirely (a stub
        # python3 first on PATH records each call). The declaring repo proves the stub is reached.
        stub = os.path.join(td, "stubbin"); os.makedirs(stub)
        calls = os.path.join(td, "py-calls")
        with open(os.path.join(stub, "python3"), "w") as f:
            f.write(f"#!/bin/sh\necho x >> '{calls}'\nexit 0\n")
        os.chmod(os.path.join(stub, "python3"), 0o755)
        run7(WARN[0], os.path.join(td, "nosteer"), path_prefix=stub)
        check("#343 R343-4: .gates.toml without the key -> no python3 fork", not os.path.exists(calls))
        run7(WARN[0], repo, path_prefix=stub)
        check("#343 R343-4: .gates.toml naming the key -> python3 reached (stub is live)",
              os.path.exists(calls))
        r = os.path.join(td, "strform"); os.makedirs(r)
        _fixture_repo(r, '[steer]\nexpensive_profile_env = "SW_GATE_FULL"\n')
        rc, _, err = run7(WARN[0], r)
        check("#343: a single-string declaration is accepted -> WARN", rc == 0 and warned(err))
        # The declaration is read from the nearest .git root only: a nested repo without one is silent.
        nested = os.path.join(repo, "sub", "inner"); os.makedirs(nested); _fixture_repo(nested, None)
        rc, _, err = run7(WARN[0], nested)
        check("#343: nested repo with no declaration -> silent (root is the nearest .git)",
              rc == 0 and not warned(err))

        # DOUBLE SPEND: a passing receipt for THIS HEAD names the upload's pre-push re-run.
        gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
        rpath = os.path.join(gitdir, "prep-pr-receipt.json")

        def receipt(**over):
            body = {"schema": "gate-receipt/v1", "commit_sha": head, "result": "pass",
                    "producer": "gate-runner"}
            body.update(over)
            with open(rpath, "w") as f:
                json.dump(body, f)

        receipt()
        for c in ("SW_GATE_FULL=1 safe-push.sh b", "SW_GATE_FULL=1 git push origin b",
                  # gate THEN upload in one command: the upload must still be judged (it outranks)
                  "SW_GATE_FULL=1 python3 scripts/gate-runner.py && SW_GATE_FULL=1 safe-push.sh b"):
            rc, out, err = run7(c, repo)
            stdout_clean = stdout_clean and out == ""
            check(f"#343: upload at a receipt-passed HEAD -> DOUBLE SPEND ({c[:40]!r})",
                  rc == 0 and "Double spend" in err)
        rc, _, err = run7("SW_GATE_FULL=1 python3 scripts/gate-runner.py", repo)
        check("#343: a GATE (not an upload) with a receipt -> generic nudge, not double spend",
              rc == 0 and warned(err) and "Double spend" not in err)
        rc, _, err = run7("safe-push.sh b", repo)
        check("#343: default-profile upload with a receipt -> silent", rc == 0 and not warned(err))
        for label, over in (("stale commit_sha", {"commit_sha": "0" * 40}),
                            ("result=fail", {"result": "fail"}),
                            ("wrong producer", {"producer": "hand"}),
                            ("wrong schema", {"schema": "x/v0"})):
            receipt(**over)
            rc, _, err = run7("SW_GATE_FULL=1 safe-push.sh b", repo)
            check(f"#343: receipt {label} -> generic nudge, NOT double spend",
                  rc == 0 and warned(err) and "Double spend" not in err)
        with open(rpath, "w") as f:
            f.write("{not json")
        rc, out, err = run7("SW_GATE_FULL=1 safe-push.sh b", repo)
        check("#343: unreadable receipt -> generic nudge, exit 0",
              rc == 0 and warned(err) and "Double spend" not in err and out == "")
        dirty = [c for c, out in RUN7_STDOUT if out != ""]
        check(f"#343: rule 7 never writes stdout on ANY of {len(RUN7_STDOUT)} runs "
              f"(advisory invariant){': ' + repr(dirty[:3]) if dirty else ''}",
              stdout_clean and RUN7_STDOUT and not dirty)


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
    # A command that QUOTES the literal `gh api -X ...` in an argument (e.g. `git commit -m "...gh api
    # -X PATCH..."`) is now SILENT: the scanner masks quoted prose (pinned in SCAN_SILENT below).
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

    # ---- Rule 3 per-clause invocation matching (the maintainer rejected the old "accepted" FP) ----
    # The old matcher grepped the WHOLE line for gh / pr / comment|create independently, so any gh pr
    # READ plus a stray `create`/`comment` word anywhere warned. It now requires the words to appear
    # as ONE CONTIGUOUS SEQUENCE - `gh`, optional flag groups, `pr`, optional flag groups, then
    # create|comment|new - WITHIN A SINGLE CLAUSE of a code frame. The sequence may sit anywhere in
    # that clause, NOT only at its command position: `echo next: gh pr create` still warns, and is
    # the accepted false positive documented in _steer_scan (bash cannot tell an echo argument from
    # a command word without knowing what the words are used for).
    GH_PR_INVOCATION_WARN = [
        "gh pr create --fill",
        "gh pr comment 5 -b hi",
        "gh -R o/r pr create --title t --body b",
        "gh pr --repo o/r comment 5 -b x",
        "cd x && gh pr create --title t --body b",
        "gh pr list --state open && gh pr create --fill",
        "GH_REPO=o/r gh pr create --fill",
        "/opt/homebrew/bin/gh pr comment 5 -b hi",
        "gh pr view 5\ngh pr comment 5 -b hi",           # newline-separated second command
    ]
    for c in GH_PR_INVOCATION_WARN:
        rc_ok, warned_all, _ = both_channels({"command": c}, marker_active=False)
        check(f"gh pr invocation -> WARN, exit 0 ({c[:42]!r})", rc_ok and warned_all)

    GH_PR_READ_SILENT = [
        "gh pr view 943 && echo create",
        "gh pr view 945 --comments",
        "gh pr view 7 --json body --jq .body | grep -n create",
        'gh pr list --search "create"',
        "gh pr diff 5",
        "gh pr checks 5",
        "reply-comment.sh 5 123 'x'",
        "gh pr list && echo create the changelog",       # was the documented "accepted FP"
        "gh pr view 5 --json title; echo comment",
        "gh pr view 5 --json comments || echo comment failed",
    ]
    for c in GH_PR_READ_SILENT:
        rc_ok, _, silent_all = both_channels({"command": c}, marker_active=True)
        check(f"gh pr read / non-invocation -> silent ({c[:42]!r})", rc_ok and silent_all)

    # ---- Rule 2 GraphQL: only a `mutation` operation warns; reads are silent ----
    GQL_WARN = [
        "gh api graphql -f query='mutation{resolveReviewThread(input:{threadId:\"T\"}){thread{id}}}'",
        "gh api graphql -f query='mutation R($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id}}}' -F id=T",
        "gh api graphql -f query=mutation{x}",
        'gh api graphql -f query="mutation { addReaction(input:{}) { reaction { content } } }"',
        "gh api graphql -f query='\n  mutation {\n    x\n  }\n'",   # multi-line document
        "gh api graphql --raw-field query='mutation M { x }'",
        "gh api graphql -fquery='mutation{x}'",
        # a REST mutation in ANOTHER clause of a compound with a GraphQL read still warns
        "gh api graphql -f query='{viewer{login}}' && gh api repos/o/r/issues -f title=hi",
        "gh api graphql -f query='{viewer{login}}' && gh api -X PATCH repos/o/r/issues/1",
    ]
    for c in GQL_WARN:
        rc_ok, warned_all, _ = both_channels({"command": c}, marker_active=False)
        check(f"gh api graphql mutation -> WARN, exit 0 ({c[:42]!r})", rc_ok and warned_all)

    GQL_SILENT = [
        "gh api graphql -f query='{repository(owner:\"o\",name:\"r\"){pullRequest(number:5){id}}}'",
        "gh api graphql -f query='query { viewer { login } }'",
        "gh api graphql -f query='query Threads($n:Int!){repository(owner:\"o\",name:\"r\"){pullRequest(number:$n){reviewThreads(first:50){nodes{isResolved}}}}}' -F n=5",
        "gh api graphql -f query='\n  query {\n    viewer { login }\n  }\n'",
        "gh api graphql -f query='{viewer{login}}' --jq .data.viewer.login",
        "gh api graphql -f query='{repository(owner:\"o\",name:\"r\"){mutationCount: id}}'",
        # SILENT-ON-DOUBT: the document is not on the command line, so it cannot be classified.
        "gh api graphql -F query=@threads.graphql -F n=5",
        "gh api graphql --input payload.json",
    ]
    for c in GQL_SILENT:
        rc_ok, _, silent_all = both_channels({"command": c}, marker_active=True)
        check(f"gh api graphql read -> silent ({c[:42]!r})", rc_ok and silent_all)

    # ---- hostile-review round 2: every base-era nudge on a REAL mutation is restored ----------
    # The first per-clause rewrite demanded `gh` at clause command position and split clauses
    # quote-blind; each vector below went SILENT under it (or is one branch of the scanner that
    # replaced it, pinned so a mutation of that branch goes red HERE, at its own assertion).
    SCAN_WARN = [
        # I1: rule 3 as a word sequence anywhere, not only at clause start
        "URL=$(gh pr create --fill)",
        'echo "$(gh pr create --fill)"',                       # $(...) inside "..." is code
        "env FOO=1 gh pr create --fill",
        "timeout 30 gh pr comment 5 -b hi",
        "sudo gh pr create --fill",
        "! gh pr create --fill",
        "echo 5 | xargs -I{} gh pr comment {} -b hi",
        "xargs gh pr comment 5 -b hi < f",
        "sleep 1 & gh pr create --fill",                       # lone & separates
        "bash -c 'gh pr create --fill'",                       # a -c script is code, not prose
        "eval 'gh pr comment 5 -b hi'",
        "gh pr view 5 --json x\ngh pr create --fill",          # newline-separated second command
        "(gh pr create --fill)",
        "command gh pr create --fill",
        "if x; then gh pr create --fill; fi",
        "gh pr view 5; gh pr create --fill",                   # ; separator
        "gh pr \\\n  create --fill",                           # backslash-newline join
        "gh -R 'o/r' pr comment 5 -b x",                       # quoted flag value stays one token
        # I2: rule 2 judged per REAL command (quote-aware split; newlines split outside quotes)
        "gh api repos/o/r/issues -f title=hi\ngh api graphql -f query='{viewer{login}}'",
        "gh api graphql -f query='{viewer{login}}'\ngh api repos/o/r/issues -f title=hi",
        "gh api graphql -f query='{viewer{login}}'\ngh api -X POST repos/o/r/issues",
        "gh api repos/o/r/issues/1/comments --jq '.[] | .id' -f body=x",
        "gh api graphql --jq '.a | .b' -f query='mutation{x}'",
        "gh api graphql -f query='{a}' & gh api repos/o/r/issues -f t=1",   # lone & splits rule 2
        "gh api graphql -f query='{a}'; gh api repos/o/r/issues -f t=1",    # ; splits rule 2
        "gh api 2>&1 repos/o/r/issues -f t=1",                 # >& is a redirect, not a separator
        # graphql branches
        "gh api graphql -X PATCH repos/o/r/issues/1",          # GraphQL never takes PATCH/PUT/DELETE
        "gh api --paginate graphql -f query='mutation{x}'",    # flag groups between api and graphql
        "gh api graphql -f query=$'mutation { x }'",           # M-c: ANSI-C quoted document
        "gh api graphql -f query='fragment F on X { id } mutation { x { ...F } }'",
        "gh api graphql -f query=@- <<'EOF'\nmutation {\n  x\n}\nEOF",   # heredoc body = the document
    ]
    for c in SCAN_WARN:
        rc_ok, warned_all, _ = both_channels({"command": c}, marker_active=False)
        check(f"scanner: real mutation -> WARN, exit 0 ({c[:48]!r})", rc_ok and warned_all)

    SCAN_SILENT = [
        'echo "run gh pr create later"',                       # quoted prose
        "git commit -m 'then gh pr create and gh api -X PATCH x'",
        "gh-comment.sh 5 'see && gh pr create later'",         # a separator inside quotes never splits
        "gh pr view 5 --json body --jq '.body' # then gh pr comment",   # comment is prose
        "cat > notes.md <<'EOF'\nthen gh pr create\nEOF",      # heredoc body is prose
        "gh pr view 5\necho create",
        "gh api --paginate graphql -f query='{viewer{login}}'",
        "gh api -H 'X-Github-Next-Global-ID: 1' graphql -f query='{viewer{login}}'",   # M-b
        "gh api graphql -f query='mutationFoo'",               # mutation tail: a name, not the keyword
        "gh api graphql -f query='query Mutations { viewer { login } }'",
        "gh api graphql -f query='{a}' -f body=\"x mutation Foo y\"",
        "gh pr view 5 --comments && gh pr list",
        # flag groups never span an UNQUOTED separator (a flag value glued to `|`/`;`, then the
        # coreutils `pr` command). NOT because the flag token class excludes those bytes - _FLAGS
        # is `[^[:space:]]+`, which matches `;` `&` `|` `(` `)` like any other non-space byte. The
        # scanner CUTS THE CLAUSE at an unquoted separator BEFORE it judges, so the words on either
        # side are never in the same clause for the sequence to match across.
        "gh --version -R o/r| pr create.txt",
        "gh -R o/r; pr comment.txt",
        # M-3: an unescaped newline ends a command in bash, so `gh pr` NEWLINE `create` is two
        # commands (`gh pr`, then a `create` command) - NOT a gh pr create invocation.
        "gh pr\ncreate --fill",
    ]
    for c in SCAN_SILENT:
        rc_ok, _, silent_all = both_channels({"command": c}, marker_active=True)
        check(f"scanner: read / prose -> silent ({c[:48]!r})", rc_ok and silent_all)

    # ---- hostile-review round 3: every nested-code shape is scanned as code ------------------
    # Each vector went SILENT at a7f6a9f (or is the one branch of the frame scanner that fixes it),
    # pinned so a mutation of that branch goes red HERE, at its own assertion.
    SCAN3_WARN = [
        # I-1: the prefilter joins backslash-newline continuations BEFORE matching
        "gh\\\n pr create --fill",
        "gh pr\\\n  create --fill",
        # I-2: a heredoc fed to a shell is CODE, not prose (bare, -s, <<-, quoted/unquoted delimiter)
        "bash <<'EOF'\ngh pr create --fill\nEOF",
        "bash -s <<EOF\ngh api -X DELETE repos/o/r/git/refs/heads/x\nEOF",
        "sh <<-\"EOF\"\n\tgh pr comment 5 -b hi\n\tEOF",
        "zsh <<EOF\necho hi\ngh pr create --fill\nEOF\necho done",
        "cat <<'A' && bash <<'B'\nnot code: gh pr view 1\nA\ngh pr create --fill\nB",   # prose, then code
        "echo $((1<<2))\ngh pr create --fill",                  # `<<` in arithmetic is no heredoc
        "(( x = 1 << 2 )); gh pr create --fill",
        # I-3: separators inside a -c / eval code quote split ITS clauses
        "bash -c 'gh api graphql -f query=\"{viewer{login}}\"; gh api repos/o/r/issues/1/comments -f body=hi'",
        "eval 'gh api graphql -f query=x; gh api repos/o/r/issues/1/comments -f body=y'",
        "bash -c \"gh api graphql -f query='{a}' && gh api -X DELETE repos/o/r/x\"",
        # I-4: a separator inside a nested $(...) / backtick never cuts the OUTER clause
        "gh api repos/o/r/issues/$(gh pr view --json number -q .number | head -1)/comments -f body=x",
        "gh api \"repos/$(git remote get-url origin | sed s/x/y/)/issues/1\" -X PATCH -f state=closed",
        "gh api -X DELETE repos/o/r/git/refs/heads/$(gh api graphql -f query='{viewer{login}}' --jq .data.viewer.login)",
        "gh api repos/o/r/issues/`gh api graphql -f query='{a}' --jq .n`/comments -f body=x",
        # M-5: `gh pr new` (alias of create); long-option and ANSI-C -c scripts
        "gh pr new --fill",
        "bash --login -c 'gh pr create --fill'",
        "bash -c $'gh pr create --fill'",
        "bash -O extglob -c 'gh pr create --fill'",
        # the query DOCUMENT (not a --jq filter) decides a GraphQL mutation
        "gh api graphql --jq '.a' -f query='\nmutation {\n  x\n}'",
        "gh api graphql -f query=mutation{x} --jq .",
        # a <<- body's delimiter line is TAB-indented; without the strip the body swallows what follows
        "cat <<-EOF\n\tx\n\tEOF\ngh pr create --fill",
        # ((...)) arithmetic: its `<<` is a shift, so the next line is a command, not a heredoc body
        "(( x = 1 << 2 ))\ngh pr create --fill",
        # a $(...) placeholder keeps a glued flag value a value: `-f$(cat body)` is a field
        "gh api repos/o/r/issues -f$(cat body)",
        # a shell-fed heredoc body ends at its delimiter even with an unbalanced quote inside it
        "bash <<EOF\necho 'x\nEOF\ngh pr create --title 'y'",
        # inside a code "...", a ' is literal to bash's parse of the OUTER quote: `"` still closes it
        "bash -c \"echo it's\" && gh pr create --title 'x'",
        # a shell-fed heredoc must never hang the scan (it is judged, then closed at its delimiter)
        "bash <<'EOF'\necho hi\nEOF\ngh pr create --fill",
        # a clause longer than one 256-byte buffer chunk keeps its early words (chunk join is exact)
        "gh pr create --title " + "t" * 300,
        "gh api repos/o/r/issues -f title=hi " + "x" * 600,
        # SQ_DOLLAR_WARN: a `$` immediately before a code script's closing quote is NOT a $'...' open.
        # Without the !csq[d] guard that branch ate the closing quote and opened a frame that never
        # closed, silencing every clause after it -- a regression vs base, which warned on all three.
        "bash -c 'grep x$' ; gh pr create --fill",
        "eval 'echo $' ; gh pr comment 5 -b x",
        "sh -c 'printf %s$' ; gh api repos/o/r/i -f a=b",
        # the invariant the dead-csq removal RELIES ON: the main loop closes a single-quoted code
        # script at its SQ, so a mutation AFTER a closed script is still judged. Every other -c/eval
        # vector puts the invocation INSIDE the script, where the close need not be correct.
        "bash -c 'echo hi' && gh pr create --fill",
        "eval 'ls'; gh api -X PATCH repos/o/r/issues/1",
        # `cat <<DELIM | bash` -- the whole SHALONE regex (sudo/command/exec prefixes) serves only
        # this branch and had no vector; disabling it left the harness green.
        "cat <<EOF | bash\ngh pr create --fill\nEOF",
        "cat <<'EOF' | sudo bash\ngh api -X DELETE repos/o/r/x\nEOF",
        # the prefilter ends a word on any non-word byte, not whitespace: heredoc() rewrites `<<D` to
        # a space, so the scanner sees `gh api graphql` where the raw bytes have `<` after `api`.
        "gh api<<D graphql -f query='mutation{x}'\nD",  # re_api
        "gh pr<<D create --fill\nD",  # re_pr
        "gh<<D api -X POST repos/o/r/i\nD",  # re_gh
        # #478 (a): eval opening a code frame from INSIDE another frame was read as prose, because
        # the backward word-scan in codeq() was unbounded by the frame start and collected the
        # enclosing opener glued to "eval" ($(eval / "eval) - which never equals "eval". Rules 2, 3
        # and 6, each via an eval inside $(...) AND inside a double-quoted bash -c script.
        "x=$(eval 'gh api -X PATCH repos/o/r/issues/1')",
        "bash -c \"eval 'gh api -X PATCH repos/o/r/issues/1'\"",
        "x=$(eval 'gh pr create --fill')",
        "bash -c \"eval 'gh pr create --fill'\"",
        "x=$(eval 'safe-push.sh b | tail -5')",
        "bash -c \"eval 'safe-push.sh b | tail -5'\"",
    ]
    for c in SCAN3_WARN:
        rc_ok, warned_all, _ = both_channels({"command": c}, marker_active=False)
        check(f"scanner r3: real mutation -> WARN, exit 0 ({c[:48]!r})", rc_ok and warned_all)

    SCAN3_SILENT = [
        # M-2: a double quote nested INSIDE a code quote is prose again
        "bash -c 'echo \"then gh pr create\"'",
        "bash -c \"echo \\\"then gh pr create\\\"\"",
        "bash -c 'ls # then gh pr create'",
        # M-2: a NEWLINE-led `mutation` in a --jq filter is not the query document
        "gh api graphql -f query='{viewer{login}}' --jq '\nmutation (.)'",
        "gh api graphql -f query='{viewer{login}}' --jq '.data\n| mutation'",
        # a heredoc that is NOT fed to a shell stays prose
        "cat > notes.md <<'EOF'\n... gh pr create ...\nEOF",
        "bash script.sh <<EOF\ngh pr create --fill\nEOF",       # stdin of a script file, not code
        "bash -c 'cat' <<EOF\ngh pr create --fill\nEOF",       # stdin of a -c script, not code
        "cat <<'A'\ngh api -X DELETE x\nA\necho done",
        # a command substitution's words stay inside it
        "echo \"$(gh pr view 5)\" create",
        # each nested frame starts with an EMPTY clause: a sibling's words never leak into the next
        "echo $(gh api repos/o/r/pulls) $(echo -f x)",
        # only the query= value is the document: another field's value beginning `mutation` is data
        "gh api graphql -f query='query($q:String!){search(query:$q,type:ISSUE,first:1){issueCount}}' -f q='mutation testing'",
        "gh api graphql -f query='{viewer{login}}' -f note='\nmutation x'",
        # #413: an explicit READ method makes -f/-F query parameters, not a body - silent
        "gh api -X GET repos/o/r/issues",
        "gh api --method GET search/issues -f q=x",
        "gh api -X GET search/issues -f q='repo:o/r is:pr'",
        "gh api -XGET search/issues -f q=x",
        "gh api --method=GET search/issues -F per_page=5",
        "gh api -X HEAD repos/o/r -f x=1",
        "gh api --method OPTIONS repos/o/r",
        "gh api -X GET -X GET search/issues -f q=x",           # adjacent reads both stripped
        # CR on #450: a QUOTED literal read method is the same literal to bash and gh
        "gh api -X 'GET' search/issues -f q=x",                # fast-path single quote
        'gh api -X "GET" search/issues -f q=x',                # slow-path double quote
        'gh api --method="HEAD" repos/o/r -f x=1',
        "gh api --method 'OPTIONS' repos/o/r -f x=1",
    ]
    for c in SCAN3_SILENT:
        rc_ok, _, silent_all = both_channels({"command": c}, marker_active=True)
        check(f"scanner r3: read / prose -> silent ({c[:48]!r})", rc_ok and silent_all)

    # #413: the read-method exemption is narrow. ANY explicit method that is not a literal read verb
    # keeps the warn: a mutation verb (even after a GET), a glued -XPOST, a non-literal -X "$M", a
    # lookalike (GETX), a lowercase verb, and a sibling clause's mutation.
    READ_METHOD_WARN = [
        "gh api -X POST repos/o/r/issues -f title=x",
        "gh api -X PATCH repos/o/r/issues/1",
        "gh api -X DELETE repos/o/r/git/refs/heads/x",
        "gh api --method PUT repos/o/r/x",
        "gh api -X GET repos/o/r/issues -X POST -f title=x",
        "gh api -X GET repos/o/r/issues -XPOST",
        "gh api -XPOST repos/o/r/issues -f title=x",
        "gh api -X \"$M\" repos/o/r/issues -f title=x",
        "gh api -X GET -X \"$M\" repos/o/r/issues",
        "gh api -X GETX repos/o/r/issues -f q=1",
        "gh api -X get repos/o/r/issues -f q=1",
        "gh api -X GET repos/o/r/issues && gh api repos/o/r/issues -f title=x",
        # a quoted NON-read or non-literal method still warns (only the exact verbs are exempt)
        "gh api -X 'POST' repos/o/r/issues -f title=x",
        "gh api -X 'GET' -X POST repos/o/r/issues -f title=x",
        "gh api -X 'GETX' repos/o/r/issues -f q=1",
        'gh api -X "GET $x" repos/o/r/issues -f q=1',
    ]
    for c in READ_METHOD_WARN:
        rc_ok, warned_all, _ = both_channels({"command": c}, marker_active=False)
        check(f"#413: non-read explicit method -> WARN, exit 0 ({c[:48]!r})", rc_ok and warned_all)

    # ---- Rule 6 (#432): a PIPED safe-push -> WARN (its exit code is the verdict) ----
    # Without pipefail a pipeline returns the LAST command's exit, so `safe-push.sh b 2>&1 | tail`
    # reports a refused push as 0. Judged per clause by the same frame-scoped splitter as rules 2/3:
    # the clause holding safe-push.sh at command position must be ENDED by a lone `|`.
    PIPED_PUSH_WARN = [
        "safe-push.sh b 2>&1 | tail -5",
        "safe-push.sh origin b 2>&1 | tail -5",                # the observed incident shape
        "scripts/safe-push.sh b | tail -5",
        "~/.claude/scripts/safe-push.sh b 2>&1 | tail -5",
        "\"$HOME\"/.claude/scripts/safe-push.sh b | tail",
        "bash ~/.claude/scripts/safe-push.sh b | tee push.log",
        "bash -c 'safe-push.sh b 2>&1 | tail -5'",             # a -c script is code
        "safe-push.sh b |& tail",
        "cd x && safe-push.sh b | tail",
        "FOO=1 safe-push.sh b | head",
        "if safe-push.sh b | tail; then echo ok; fi",
        "out=$(safe-push.sh b | tail -3)",
        # I1: a QUOTED script path collapses to one placeholder; the closing quote re-exposes the name
        "bash '${CLAUDE_PLUGIN_ROOT}/scripts/safe-push.sh' b 2>&1 | tail -5",   # prep-pr Step 7 shape
        "bash \"$HOME/.claude/scripts/safe-push.sh\" b | tail",
        "'safe-push.sh' b | tail",
        "bash $'/x/safe-push.sh' b | tail",
        "bash -c \"bash \\\"$HOME/x/safe-push.sh\\\" b | tail\"",
        # M1: common wrappers and their -flag / numeric arguments
        "timeout 60 safe-push.sh b | tail",
        "nice safe-push.sh b | tail",
        "nice -n 10 safe-push.sh b | tail",
        "sudo -E safe-push.sh b | tail",
        "bash -x safe-push.sh b | tail",
        "/bin/bash safe-push.sh b | tail",
        "/usr/bin/env bash scripts/safe-push.sh b | tail",
    ]
    for c in PIPED_PUSH_WARN:
        rc_ok, warned_all, _ = both_channels({"command": c}, marker_active=False)
        check(f"#432: piped safe-push -> WARN, exit 0 ({c[:48]!r})", rc_ok and warned_all)

    PIPED_PUSH_SILENT = [
        "safe-push.sh b",
        "safe-push.sh b || echo x",                            # `||` is not a pipe
        "safe-push.sh b # prep-pr-ok",
        "safe-push.sh b --force-with-lease # prep-pr-ok",
        "safe-push.sh b # then | tail",                        # a comment is prose
        "echo \"safe-push.sh b | tail\"",                      # quoted prose
        "git commit -m 'safe-push.sh b | tail'",
        "echo hi | safe-push.sh b",                            # safe-push is the LAST command
        "safe-push.sh b && echo done | tail",                  # the pipe belongs to echo's clause
        "grep -n x scripts/safe-push.sh | head",               # not at command position
        "my-safe-push.sh b | tail",                            # a different script
        "safe-push.sh.bak b | tail",                           # the name must END at .sh
        # I1 must not turn quoted PROSE into code: the name has to END the quote AND sit at command
        # position
        "git commit -m 'use safe-push.sh | tail'",
        "echo 'safe-push.sh' | tail",                          # argument slot, not command position
        "echo \"x/safe-push.sh\" | tail",
        "bash 'safe-push.sh.bak' b | tail",
        "bash 'x/my-safe-push.sh' b | tail",                   # a quoted name must follow a `/`
        "grep -n x 'scripts/safe-push.sh' | head",
        "bash '${CLAUDE_PLUGIN_ROOT}/scripts/safe-push.sh' b",
        "bash \"$HOME/.claude/scripts/safe-push.sh\" b || echo x",
    ]
    for c in PIPED_PUSH_SILENT:
        rc_ok, _, silent_all = both_channels({"command": c}, marker_active=True)
        check(f"#432: unpiped / prose safe-push -> silent ({c[:48]!r})", rc_ok and silent_all)

    rule7_cases()

    # ROBUSTNESS: malformed / unbalanced / hostile input exits 0 promptly and never blocks.
    ROBUST = [
        "bash -c '", "bash -c \"x", "gh pr create $( $( $(", "gh pr create `", "cat <<EOF\n",
        "bash <<EOF\n", "echo $((", "((", "\\", "'", "\"", "$'", "gh api \\", "<<", "<<-",
        "eval \"'\"", "bash -c \"\\\"", "x\x01y gh pr view",
    ]
    for c in ROBUST:
        try:
            rc, _ = run_steer({"command": c}, channel="stdin")
            ok = rc == 0
        except subprocess.TimeoutExpired:
            ok = False
        check(f"robustness: unbalanced input exits 0 promptly ({c[:30]!r})", ok)
    for raw in ("", "not json", "null", '{"tool_input":null}', '{"tool_input":{"command":null}}',
                '{"tool_input":{"command":"gh pr create\\u0000 --fill"}}'):
        p = subprocess.run([STEER], input=raw, capture_output=True, text=True, timeout=5)
        check(f"robustness: malformed payload exits 0, no stdout ({raw[:30]!r})",
              p.returncode == 0 and p.stdout == "")

    # #414: every field now comes out of ONE jq call as NUL-terminated values. A newline-bearing
    # command must arrive whole (a line-based split would hand the rule only the first line), and a
    # command carrying a NUL or separator-LOOKING text must not shift the fields after it: the NUL
    # is dropped (what `$(jq -r)` always did), so a LEADING NUL still leaves the rule its command.
    for c in ("cd /tmp &&\ngh pr create --fill\n",
              "cat <<'EOF'\nnot a command\nEOF\ngh pr comment 5 -b hi",
              "\u0000gh pr create --fill",
              "gh pr create --fill\u0000\u0000\u0000\u0000\u0000END\u0000",
              "echo '\\u0000END\\u0000' ; gh pr create --fill"):
        rc_ok, warned_all, _ = both_channels({"command": c})
        check(f"#414: newline / NUL / separator-like command reaches the rule whole ({c[:36]!r})",
              rc_ok and warned_all)
    for c in ("gh pr view 1\nls\n", "echo 'gh pr create'\u0000\n"):
        rc_ok, _, silent_all = both_channels({"command": c})
        check(f"#414: multi-line / NUL-bearing READ stays silent ({c[:36]!r})", rc_ok and silent_all)
    # a NUL in an EARLY field (session_id) must not shift the later ones: the re-Read still warns.
    with tempfile.TemporaryDirectory() as ntd:
        nf = os.path.join(ntd, "f"); open(nf, "w").close()
        for _ in range(2):
            rc, err = run_steer({"file_path": nf}, channel="stdin", tool_name="Read",
                                session_id="s\u0000x", read_state_dir=os.path.join(ntd, "st"))
        check("#414: NUL in session_id never shifts the later fields (2nd Read still warns)",
              rc == 0 and warned(err))
        # the per-field trailing-newline strip (as `$(...)` did) is observable: `f\n` fingerprints
        # as `f`, so the 2nd Read of a newline-suffixed path still warns.
        for _ in range(2):
            rc, err = run_steer({"file_path": nf + "\n"}, channel="stdin", tool_name="Read",
                                session_id="s-nl", read_state_dir=os.path.join(ntd, "st"))
        check("#414: trailing newline in file_path is stripped (2nd Read of 'f\\n' still warns)",
              rc == 0 and warned(err))
    # A TTY stdin must never make jq wait on the terminal: the `[ -t 0 ]` guard redirects it, and the
    # $TOOL_INPUT fallback still steers. Without the guard the hook HANGS, the one way it could block.
    tty_env = {k: v for k, v in os.environ.items()
               if k not in ("TMUX", "CLAUDE_CODE_SESSION_ID", "TOOL_INPUT")}
    tty_env["TOOL_INPUT"] = json.dumps({"command": "gh pr create --fill"})
    pty_master, pty_slave = pty.openpty()
    try:
        p = subprocess.run([STEER], stdin=pty_slave, capture_output=True, env=tty_env, timeout=10)
        tty_rc, tty_err, tty_out = p.returncode, p.stderr.decode(errors="replace"), p.stdout
    except subprocess.TimeoutExpired:
        tty_rc, tty_err, tty_out = 124, "TIMEOUT", b""
    finally:
        os.close(pty_master); os.close(pty_slave)
    check(f"#414: TTY stdin + $TOOL_INPUT -> no hang, warns, exit 0, no stdout (rc {tty_rc})",
          tty_rc == 0 and warned(tty_err) and tty_out == b"")

    # PERF (M-1): the scan is linear. 400KB of quoted words took 7.5s at a7f6a9f (quadratic tail).
    # #453: asserted as a RATIO, not a wall-clock ceiling. A fixed 3s bound flaked on macOS CI
    # (1.65s-3.14s spread for the SAME linear scan), because it measured the runner as much as the
    # code. Timing the full input against a quarter-size baseline in the same process cancels the
    # runner's speed: linear is ~4x (less, since process start is a fixed cost), quadratic ~16x.
    # Each size keeps its FASTEST of up to 3 runs (noise only ever adds time), and the 10s ceiling
    # stays as a sanity bound so a genuine hang still fails even if both sizes hang alike. The runs
    # are INTERLEAVED (q, f, q, f, ...) so a sustained slowdown lands on both sizes, not just one.
    # The ceiling is ENFORCED, not just asserted: the subprocess timeout IS the ceiling. run_steer
    # already turns a TimeoutExpired into rc 124 (a failing run, no traceback), so a hung scan fails
    # THROUGH this check after 10s instead of burning a 30s timeout per run.
    PERF_CEILING = 10.0

    def timed(c):
        t0 = time.time()
        rc, err = run_steer({"command": c}, channel="stdin", timeout=PERF_CEILING)
        return time.time() - t0, rc == 0 and not warned(err)
    for label, build, n in (
            ("400KB of 'a' words", lambda k: "gh pr view 1 " + " ".join(["'a'"] * k) + " && echo create", 100000),
            ("50k $(a) substitutions", lambda k: "gh pr view 1 " + "$(a) " * k + "# create", 50000),
            ("100k-line heredoc", lambda k: "cat > f <<'EOF'\n" + "line x\n" * k + "EOF\ngh pr view 1 # create", 100000)):
        c_base, c_full = build(n // 4), build(n)
        t_base = t_full = None; ok_base = ok_full = True
        for _ in range(3):
            dt, ok = timed(c_base); ok_base = ok_base and ok
            t_base = dt if t_base is None else min(t_base, dt)
            dt, ok = timed(c_full); ok_full = ok_full and ok
            t_full = dt if t_full is None else min(t_full, dt)
            if dt >= PERF_CEILING:
                break  # already over the ceiling: more runs only burn CI time
        ratio = t_full / max(t_base, 1e-3)
        check(f"perf: {label} scales linearly, silent (4x input -> {ratio:.1f}x time, < 8x; "
              f"{t_base:.2f}s -> {t_full:.2f}s, < {PERF_CEILING:.0f}s)",
              ok_base and ok_full and ratio < 8.0 and t_full < PERF_CEILING)

    # PERF: a long read chain never reaches awk (the prefilter), and one that does (every clause
    # carries `comment`) is scanned in ONE pass, not one fork per clause.
    # The limit matches the 3.0s the linearity block above uses: run_steer measures the WHOLE
    # subprocess (shell start, jq, the awk scan), so a loaded CI runner can blow a 1s bound while
    # the scanner itself is fine. Correctness (exit 0, silent) stays unconditional; only the timing
    # is runner-tolerant. dt is captured ONCE - measuring separately for the label and the assertion
    # let a failure print a passing-looking number.
    for label, c in (
            ("300-clause read chain", " && ".join(f"gh pr view {i} --json title" for i in range(300))),
            ("300-clause prefilter-hit chain",
             " && ".join(f"gh pr view {i} --comments" for i in range(300)))):
        t0 = time.time()
        rc, err = run_steer({"command": c}, channel="stdin")
        dt = time.time() - t0
        check(f"perf: {label} scans in < 3s, silent, exit 0 ({dt:.2f}s)",
              rc == 0 and not warned(err) and dt < 3.0)

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
    # deny-authority depth) on the strength of ONE property: it is ADVISORY -- it cannot block a tool call.
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
        # rule 6 (#432; CR on #450): the piped safe-push WARN path must not write stdout either
        {"tool_name": "Bash", "tool_input": {"command": "safe-push.sh b 2>&1 | tail -5"}},
        {"tool_name": "Bash", "tool_input": {"command": "bash '/x/scripts/safe-push.sh' b | tail"}},
        # #413 quoted read method (silent path) and a quoted mutation (warn path)
        {"tool_name": "Bash", "tool_input": {"command": "gh api -X 'GET' search/issues -f q=x"}},
        {"tool_name": "Bash", "tool_input": {"command": "gh api -X 'POST' repos/o/r -f a=b"}},
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
    # 29 = the prior 21 + the eight #402 additions: the six gh-* wrappers orchestrate-steer.sh
    # ADVERTISES as the remedy for a raw `gh api` mutation (a nudge naming a helper the install
    # does not provide is worse than no nudge), plus orchestrate-status.sh (a live stable-path
    # dependency of the DEPLOYED elmer-triage.sh - the #216 shape in the one workflow that runs
    # unattended) and orchestrate-feedback.sh (named by steer rule 1 as the canonical way to log
    # feedback). Bumping this number is DELIBERATE, not bookkeeping: pinning it is what makes a
    # helper added to the deploy set impossible to add without also making it canonical below.
    check("#284 lockstep: HELPER_NAMES imported (exact count -- a truncated parse must not pass)",
          len(helper_names) == 29)
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
    # which meant `cat` was missing too, so the script's then-`stdin_json=$(cat)` returned nothing and it
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
