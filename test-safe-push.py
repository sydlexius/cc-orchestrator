#!/usr/bin/env python3
"""Proof harness for safe-push.sh: branch-arg validation (#35) + additive-vs-rewrite
classification (#148).

#35: safe-push.sh adds `-u origin` itself, so the FIRST positional must be a branch
name; a leading-dash first positional is rejected (exit 2) instead of silently
flowing onto the git-push line.

#148: before pushing, safe-push classifies the push against a FRESH `git ls-remote`
SHA (not the stale local ref): first-push / fast-forward = ADDITIVE (allowed);
remote-ahead = diverged (REFUSED, exit 1); otherwise = history REWRITE (REFUSED
unless --rewrite/--rebased, which is CONSUMED, auto-adds --force-with-lease, and
never injects a bare --force).

This harness stubs `git` via a temp 0755 script first on PATH (host-independent;
never touches a real remote). The stub is STATEFUL: `ls-remote` returns the
configurable OLD remote SHA before any push, then LOCAL_SHA once a push has been
recorded (simulating the push landing, so the post-push verification passes). It
also returns configurable `merge-base --is-ancestor` exit codes to drive each
classification branch, and records every `git push` for assertion.

Run: python3 test-safe-push.py
"""
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "safe-push.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


GIT_STUB = (
    "#!/usr/bin/env bash\n"
    "set -eu\n"
    'case "$1 ${2:-}" in\n'
    '  "rev-parse --git-dir") echo "$GITDIR"; exit 0 ;;\n'
    "esac\n"
    'if [ "$1" = "symbolic-ref" ]; then\n'
    '  if [ -n "${CUR_BRANCH:-}" ]; then echo "$CUR_BRANCH"; exit 0; else exit 1; fi\n'
    "fi\n"
    'if [ "$1" = "rev-parse" ] && [ "$2" = "--verify" ]; then\n'
    '  echo "$LOCAL_SHA"; exit 0\n'
    "fi\n"
    'if [ "$1" = "cat-file" ]; then\n'
    '  # -e <sha>^{commit}: is the remote tip present locally? Configurable.\n'
    '  exit "${CAT_FILE_RC:-0}"\n'
    "fi\n"
    'if [ "$1" = "merge-base" ] && [ "$2" = "--is-ancestor" ]; then\n'
    '  # args: merge-base --is-ancestor <A> <B>. (remote,local)=is remote an ancestor\n'
    '  # of local (fast-forward); (local,remote)=is local an ancestor of remote (diverged).\n'
    '  if [ "$3" = "${REMOTE_SHA:-}" ] && [ "$4" = "$LOCAL_SHA" ]; then exit "${MB_R_ANC_L:-1}"; fi\n'
    '  if [ "$3" = "$LOCAL_SHA" ] && [ "$4" = "${REMOTE_SHA:-}" ]; then exit "${MB_L_ANC_R:-1}"; fi\n'
    "  exit 1\n"
    "fi\n"
    'if [ "$1" = "push" ]; then\n'
    '  shift; printf "%s\\n" "$*" >>"$PUSHLOG"\n'
    '  # Emit a recognizable transcript; safe-push redirects push stdout+stderr to its log.\n'
    '  if [ -n "${PUSH_TRANSCRIPT:-}" ]; then printf "%s\\n" "$PUSH_TRANSCRIPT"; printf "%s\\n" "$PUSH_TRANSCRIPT" >&2; fi\n'
    '  exit "${PUSH_RC:-0}"\n'
    "fi\n"
    'if [ "$1" = "ls-remote" ]; then\n'
    '  # Stateful: after a push has been recorded, the remote matches local (the push\n'
    '  # landed). Before any push, return the configurable OLD remote SHA (empty = no ref).\n'
    '  if [ -s "$PUSHLOG" ]; then\n'
    '    # POST_PUSH_REMOTE overrides what the remote reports AFTER the push, so a test can\n'
    '    # exercise the two VERIFICATION failure branches: unset -> the push landed (default);\n'
    '    # "none" -> the ref is absent; anything else -> the ref exists but holds that SHA.\n'
    '    if [ "${POST_PUSH_REMOTE:-}" = "none" ]; then :\n'
    '    elif [ -n "${POST_PUSH_REMOTE:-}" ]; then printf "%s\\trefs/heads/x\\n" "$POST_PUSH_REMOTE"\n'
    '    else printf "%s\\trefs/heads/x\\n" "$LOCAL_SHA"; fi\n'
    '  elif [ -n "${REMOTE_SHA:-}" ]; then\n'
    '    printf "%s\\trefs/heads/x\\n" "$REMOTE_SHA"\n'
    "  fi\n"
    "  exit 0\n"
    "fi\n"
    "exit 0\n"
)


def run(args, *, cur_branch="feature/x", local_sha="aaaa111", remote_sha="",
        mb_r_anc_l=1, mb_l_anc_r=1, cat_file_rc=0, push_rc=0, push_transcript="",
        post_push_remote=None):
    """Invoke safe-push.sh with a stubbed git. Returns (rc, stdout, stderr, pushes, log)
    where pushes is the list of recorded `git push ...` argument strings and log is the
    content of safe-push's own log file (read before the tempdir is cleaned up)."""
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        gitdir = os.path.join(td, "gitdir"); os.makedirs(gitdir)
        pushlog = os.path.join(td, "pushlog")

        git = os.path.join(bindir, "git")
        with open(git, "w") as f:
            f.write(GIT_STUB)
        os.chmod(git, 0o755)

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["GITDIR"] = gitdir
        env["LOCAL_SHA"] = local_sha
        env["REMOTE_SHA"] = remote_sha
        env["MB_R_ANC_L"] = str(mb_r_anc_l)
        env["MB_L_ANC_R"] = str(mb_l_anc_r)
        env["CAT_FILE_RC"] = str(cat_file_rc)
        env["PUSH_RC"] = str(push_rc)
        env["PUSH_TRANSCRIPT"] = push_transcript
        if post_push_remote is not None:
            env["POST_PUSH_REMOTE"] = post_push_remote
        else:
            env.pop("POST_PUSH_REMOTE", None)
        env["PUSHLOG"] = pushlog
        if cur_branch is not None:
            env["CUR_BRANCH"] = cur_branch
        else:
            env.pop("CUR_BRANCH", None)

        p = subprocess.run(["bash", SCRIPT] + args, env=env,
                           capture_output=True, text=True, timeout=15)
        pushes = []
        if os.path.exists(pushlog):
            with open(pushlog) as fh:
                pushes = [ln.rstrip("\n") for ln in fh if ln.strip()]
        log = ""
        logpath = os.path.join(gitdir, "safe-push.log")
        if os.path.exists(logpath):
            with open(logpath) as fh:
                log = fh.read()
        return p.returncode, p.stdout, p.stderr, pushes, log


def main():
    print("== #35 leading-dash first positional -> exit 2, NO push ==")
    rc, out, err, pushes, _log = run(["-u", "origin", "main"])
    check("'-u origin main' -> exit 2", rc == 2)
    check("'-u ...' does not invoke git push", len(pushes) == 0)
    check("'-u ...' error names the branch-first usage", "branch name" in err)

    print("== first-push (no remote ref) -> ADDITIVE, proceeds ==")
    rc, out, err, pushes, _log = run(["feature/x"])  # remote_sha="" -> first-push
    check("first-push -> exit 0", rc == 0)
    check("first-push invokes exactly one git push", len(pushes) == 1)
    check("push targets origin feature/x", bool(pushes) and "origin feature/x" in pushes[0])

    print("== first-push + trailing flag -> flag forwarded intact ==")
    rc, out, err, pushes, _log = run(["feature/x", "--force-with-lease"])
    check("'--force-with-lease' -> exit 0", rc == 0)
    check("--force-with-lease forwarded", bool(pushes) and "--force-with-lease" in pushes[0])

    print("== no-arg -> current-branch fallback via symbolic-ref ==")
    rc, out, err, pushes, _log = run([], cur_branch="feature/current")
    check("no-arg -> exit 0 (current-branch fallback)", rc == 0)
    check("no-arg pushes the symbolic-ref branch", bool(pushes) and "origin feature/current" in pushes[0])

    print("== #148 fast-forward (remote is ancestor of local) -> ADDITIVE, proceeds ==")
    rc, out, err, pushes, _log = run(["feature/x"], remote_sha="oldbbb222", mb_r_anc_l=0)
    check("fast-forward -> exit 0", rc == 0)
    check("fast-forward pushes", len(pushes) == 1)

    print("== #148 diverged (remote ahead) -> REFUSED, exit 1, NO push ==")
    rc, out, err, pushes, _log = run(["feature/x"], remote_sha="oldbbb222", mb_r_anc_l=1, mb_l_anc_r=0)
    check("diverged -> exit 1", rc == 1)
    check("diverged does NOT push", len(pushes) == 0)
    check("diverged message says remote is AHEAD", "AHEAD" in err)
    check("diverged message is NOT the rewrite message", "REWRITE" not in err)

    print("== #148 rewrite WITHOUT intent -> REFUSED, exit 1, NO push ==")
    rc, out, err, pushes, _log = run(["feature/x"], remote_sha="oldbbb222", mb_r_anc_l=1, mb_l_anc_r=1)
    check("rewrite-no-intent -> exit 1", rc == 1)
    check("rewrite-no-intent does NOT push", len(pushes) == 0)
    check("rewrite-no-intent refuses a silent rewrite", "silent rewrite" in err)

    print("== #148 rewrite WITH --rewrite -> proceeds, lease auto-added, flag consumed ==")
    rc, out, err, pushes, _log = run(["feature/x", "--rewrite"], remote_sha="oldbbb222", mb_r_anc_l=1, mb_l_anc_r=1)
    check("rewrite+intent -> exit 0", rc == 0)
    check("rewrite+intent pushes", len(pushes) == 1)
    check("rewrite+intent auto-adds --force-with-lease", bool(pushes) and "--force-with-lease" in pushes[0])
    check("rewrite+intent does NOT forward --rewrite to git push", bool(pushes) and "--rewrite" not in pushes[0])
    check("rewrite+intent never injects a bare --force", bool(pushes) and " --force " not in (" " + pushes[0] + " ").replace("--force-with-lease", "x"))
    check("rewrite+intent warns about orphaned SHA", "orphaned" in err.lower())

    print("== #148 --rebased alias also unlocks the rewrite ==")
    rc, out, err, pushes, _log = run(["feature/x", "--rebased"], remote_sha="oldbbb222", mb_r_anc_l=1, mb_l_anc_r=1)
    check("--rebased alias -> exit 0", rc == 0)
    check("--rebased pushes with --force-with-lease", bool(pushes) and "--force-with-lease" in pushes[0])

    print("== #148 remote tip not in local DB (stale/shallow) -> fetch hint, exit 1, NO push ==")
    rc, out, err, pushes, _log = run(["feature/x"], remote_sha="oldbbb222", cat_file_rc=1)
    check("missing-object -> exit 1", rc == 1)
    check("missing-object does NOT push", len(pushes) == 0)
    check("missing-object suggests git fetch", "git fetch origin" in err)
    check("missing-object is NOT labeled a rewrite", "REWRITE" not in err and "silent rewrite" not in err)

    print("== #148 rewrite + caller already passed --force-with-lease -> not doubled ==")
    rc, out, err, pushes, _log = run(["feature/x", "--rewrite", "--force-with-lease"], remote_sha="oldbbb222", mb_r_anc_l=1, mb_l_anc_r=1)
    check("rewrite + explicit lease -> exit 0", rc == 0)
    check("--force-with-lease appears exactly once", bool(pushes) and pushes[0].count("--force-with-lease") == 1)

    print("== #293 SUCCESS path does NOT mirror the push transcript into stderr (log-only) ==")
    rc, out, err, pushes, log = run(["feature/x"], push_transcript="ENUMERATING_OBJECTS_MARKER")
    check("success -> exit 0", rc == 0)
    check("success emits the terse verified line", "verified origin/feature/x" in err)
    check("success does NOT mirror the transcript into stderr", "ENUMERATING_OBJECTS_MARKER" not in err)
    check("success still captures the transcript in the log", "ENUMERATING_OBJECTS_MARKER" in log)

    print("== #293 FAILURE path: set-e-safe capture + bounded tail + log path (no full mirror) ==")
    rc, out, err, pushes, log = run(["feature/x"], push_rc=1, push_transcript="REMOTE_REJECTED_MARKER")
    check("push failure -> exit 1 via safe-push's own handler (set -e did not abort)", rc == 1)
    check("failure emits a bounded-tail header", "last" in err.lower() and "lines" in err.lower())
    check("failure surfaces the transcript via the tail", "REMOTE_REJECTED_MARKER" in err)
    check("failure names the log path", "safe-push.log" in err)

    # The single-marker test above proves a tail is EMITTED, not that it is BOUNDED - it would
    # pass identically if emit_log_tail dumped the whole log. Prove the bound with a transcript
    # longer than the 30-line window: the NEWEST lines must appear and the OLDEST must not.
    # (CodeRabbit finding on PR #351: "it would still pass if emit_log_tail dumped the entire log".)
    print("== #293 FAILURE tail is BOUNDED to the last 30 lines (oldest excluded) ==")
    long_transcript = "\n".join(f"XSCRIPT_LINE_{i:03d}" for i in range(1, 61))
    rc, out, err, pushes, log = run(["feature/x"], push_rc=1, push_transcript=long_transcript)
    check("bounded: exit 1", rc == 1)
    check("bounded: NEWEST line is in the tail", "XSCRIPT_LINE_060" in err)
    check("bounded: OLDEST line is NOT in the tail (30-line bound enforced)",
          "XSCRIPT_LINE_001" not in err)
    check("bounded: an out-of-window middle line is NOT in the tail",
          "XSCRIPT_LINE_020" not in err)
    check("bounded: the FULL transcript is still in the log (both ends)",
          "XSCRIPT_LINE_001" in log and "XSCRIPT_LINE_060" in log)

    # The other TWO failure branches: git push exits 0 but verification fails. Both must exit 1
    # through safe-push's own handler with the same bounded tail + log path, or a silently-failed
    # push reads as success - the exact mode this wrapper exists to catch.
    print("== #293 VERIFICATION failures: missing ref and SHA mismatch ==")
    rc, out, err, pushes, log = run(["feature/x"], push_transcript="MISSING_REF_MARKER",
                                    post_push_remote="none")
    check("missing-ref: exit 1 (push exited 0 but origin has no ref)", rc == 1)
    # Assert the DISTINGUISHING phrase, not loose substrings. `"no" in err and "ref" in err`
    # also matched the SHA-mismatch fallback ("does NOT match" contains "no"; the log path
    # contains "ref"), so the check passed even with the missing-ref branch disabled - a test
    # with no teeth. The two branches deliberately overlap (an empty remote_sha also fails the
    # mismatch check), so only the message text tells them apart.
    check("missing-ref: reports the ABSENT-ref case specifically (not the mismatch fallback)",
          "has no" in err and "does not match" not in err)
    check("missing-ref: emits the bounded-tail header",
          "last" in err.lower() and "lines" in err.lower())
    check("missing-ref: names the log path", "safe-push.log" in err)

    rc, out, err, pushes, log = run(["feature/x"], local_sha="aaaa111",
                                    push_transcript="SHA_MISMATCH_MARKER",
                                    post_push_remote="bbbb222")
    check("sha-mismatch: exit 1 (remote ref moved to a DIFFERENT sha)", rc == 1)
    check("sha-mismatch: reports both shas", "aaaa111" in err and "bbbb222" in err)
    check("sha-mismatch: emits the bounded-tail header",
          "last" in err.lower() and "lines" in err.lower())
    check("sha-mismatch: names the log path", "safe-push.log" in err)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
