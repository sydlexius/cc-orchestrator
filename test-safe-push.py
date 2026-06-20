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
    '  shift; printf "%s\\n" "$*" >>"$PUSHLOG"; exit 0\n'
    "fi\n"
    'if [ "$1" = "ls-remote" ]; then\n'
    '  # Stateful: after a push has been recorded, the remote matches local (the push\n'
    '  # landed). Before any push, return the configurable OLD remote SHA (empty = no ref).\n'
    '  if [ -s "$PUSHLOG" ]; then\n'
    '    printf "%s\\trefs/heads/x\\n" "$LOCAL_SHA"\n'
    '  elif [ -n "${REMOTE_SHA:-}" ]; then\n'
    '    printf "%s\\trefs/heads/x\\n" "$REMOTE_SHA"\n'
    "  fi\n"
    "  exit 0\n"
    "fi\n"
    "exit 0\n"
)


def run(args, *, cur_branch="feature/x", local_sha="aaaa111", remote_sha="",
        mb_r_anc_l=1, mb_l_anc_r=1, cat_file_rc=0):
    """Invoke safe-push.sh with a stubbed git. Returns (rc, stdout, stderr, pushes)
    where pushes is the list of recorded `git push ...` argument strings."""
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
        return p.returncode, p.stdout, p.stderr, pushes


def main():
    print("== #35 leading-dash first positional -> exit 2, NO push ==")
    rc, out, err, pushes = run(["-u", "origin", "main"])
    check("'-u origin main' -> exit 2", rc == 2)
    check("'-u ...' does not invoke git push", len(pushes) == 0)
    check("'-u ...' error names the branch-first usage", "branch name" in err)

    print("== first-push (no remote ref) -> ADDITIVE, proceeds ==")
    rc, out, err, pushes = run(["feature/x"])  # remote_sha="" -> first-push
    check("first-push -> exit 0", rc == 0)
    check("first-push invokes exactly one git push", len(pushes) == 1)
    check("push targets origin feature/x", bool(pushes) and "origin feature/x" in pushes[0])

    print("== first-push + trailing flag -> flag forwarded intact ==")
    rc, out, err, pushes = run(["feature/x", "--force-with-lease"])
    check("'--force-with-lease' -> exit 0", rc == 0)
    check("--force-with-lease forwarded", bool(pushes) and "--force-with-lease" in pushes[0])

    print("== no-arg -> current-branch fallback via symbolic-ref ==")
    rc, out, err, pushes = run([], cur_branch="feature/current")
    check("no-arg -> exit 0 (current-branch fallback)", rc == 0)
    check("no-arg pushes the symbolic-ref branch", bool(pushes) and "origin feature/current" in pushes[0])

    print("== #148 fast-forward (remote is ancestor of local) -> ADDITIVE, proceeds ==")
    rc, out, err, pushes = run(["feature/x"], remote_sha="oldbbb222", mb_r_anc_l=0)
    check("fast-forward -> exit 0", rc == 0)
    check("fast-forward pushes", len(pushes) == 1)

    print("== #148 diverged (remote ahead) -> REFUSED, exit 1, NO push ==")
    rc, out, err, pushes = run(["feature/x"], remote_sha="oldbbb222", mb_r_anc_l=1, mb_l_anc_r=0)
    check("diverged -> exit 1", rc == 1)
    check("diverged does NOT push", len(pushes) == 0)
    check("diverged message says remote is AHEAD", "AHEAD" in err)
    check("diverged message is NOT the rewrite message", "REWRITE" not in err)

    print("== #148 rewrite WITHOUT intent -> REFUSED, exit 1, NO push ==")
    rc, out, err, pushes = run(["feature/x"], remote_sha="oldbbb222", mb_r_anc_l=1, mb_l_anc_r=1)
    check("rewrite-no-intent -> exit 1", rc == 1)
    check("rewrite-no-intent does NOT push", len(pushes) == 0)
    check("rewrite-no-intent refuses a silent rewrite", "silent rewrite" in err)

    print("== #148 rewrite WITH --rewrite -> proceeds, lease auto-added, flag consumed ==")
    rc, out, err, pushes = run(["feature/x", "--rewrite"], remote_sha="oldbbb222", mb_r_anc_l=1, mb_l_anc_r=1)
    check("rewrite+intent -> exit 0", rc == 0)
    check("rewrite+intent pushes", len(pushes) == 1)
    check("rewrite+intent auto-adds --force-with-lease", bool(pushes) and "--force-with-lease" in pushes[0])
    check("rewrite+intent does NOT forward --rewrite to git push", bool(pushes) and "--rewrite" not in pushes[0])
    check("rewrite+intent never injects a bare --force", bool(pushes) and " --force " not in (" " + pushes[0] + " ").replace("--force-with-lease", "x"))
    check("rewrite+intent warns about orphaned SHA", "orphaned" in err.lower())

    print("== #148 --rebased alias also unlocks the rewrite ==")
    rc, out, err, pushes = run(["feature/x", "--rebased"], remote_sha="oldbbb222", mb_r_anc_l=1, mb_l_anc_r=1)
    check("--rebased alias -> exit 0", rc == 0)
    check("--rebased pushes with --force-with-lease", bool(pushes) and "--force-with-lease" in pushes[0])

    print("== #148 remote tip not in local DB (stale/shallow) -> fetch hint, exit 1, NO push ==")
    rc, out, err, pushes = run(["feature/x"], remote_sha="oldbbb222", cat_file_rc=1)
    check("missing-object -> exit 1", rc == 1)
    check("missing-object does NOT push", len(pushes) == 0)
    check("missing-object suggests git fetch", "git fetch origin" in err)
    check("missing-object is NOT labeled a rewrite", "REWRITE" not in err and "silent rewrite" not in err)

    print("== #148 rewrite + caller already passed --force-with-lease -> not doubled ==")
    rc, out, err, pushes = run(["feature/x", "--rewrite", "--force-with-lease"], remote_sha="oldbbb222", mb_r_anc_l=1, mb_l_anc_r=1)
    check("rewrite + explicit lease -> exit 0", rc == 0)
    check("--force-with-lease appears exactly once", bool(pushes) and pushes[0].count("--force-with-lease") == 1)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
