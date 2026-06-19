#!/usr/bin/env python3
"""Proof harness for safe-push.sh branch-argument validation (#35).

safe-push.sh adds `-u origin` itself, so the FIRST positional must be a branch
name. A leading-dash first positional (e.g. a caller copying the misuse pattern
`safe-push.sh -u origin <branch>`) previously was silently discarded and the
unconsumed flags flowed onto the `git push` line, producing a confusing
`fatal: refs/remotes/origin/HEAD cannot be resolved to branch` error. The fix
rejects a leading-dash first positional with a clear usage error + exit 2, while
PRESERVING the zero-arg current-branch fallback and legitimate TRAILING flags
(e.g. `<branch> --force-with-lease`).

This harness stubs `git` via a temp 0755 script first on PATH (host-independent;
never touches a real remote). The stub records every `git push` invocation to a
log file so the test can assert pushes happened / did not happen and that
trailing flags were forwarded intact.

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


def run(args, *, cur_branch="feature/x", local_sha="abc123def456"):
    """Invoke safe-push.sh with a stubbed git. Returns (rc, stdout, stderr, pushes)
    where pushes is the list of recorded `git push ...` argument strings."""
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        gitdir = os.path.join(td, "gitdir"); os.makedirs(gitdir)
        pushlog = os.path.join(td, "pushlog")

        git = os.path.join(bindir, "git")
        with open(git, "w") as f:
            f.write(
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
                'if [ "$1" = "push" ]; then\n'
                '  shift; printf "%s\\n" "$*" >>"$PUSHLOG"; exit 0\n'
                "fi\n"
                'if [ "$1" = "ls-remote" ]; then\n'
                '  printf "%s\\trefs/heads/x\\n" "$LOCAL_SHA"; exit 0\n'
                "fi\n"
                "exit 0\n"
            )
        os.chmod(git, 0o755)

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["GITDIR"] = gitdir
        env["LOCAL_SHA"] = local_sha
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
    print("== leading-dash first positional -> exit 2, NO push ==")
    rc, out, err, pushes = run(["-u", "origin", "main"])
    check("'-u origin main' -> exit 2", rc == 2)
    check("'-u ...' does not invoke git push", len(pushes) == 0)
    check("'-u ...' error names the branch-first usage", "branch name" in err)

    print("== normal branch arg -> proceeds to push ==")
    rc, out, err, pushes = run(["feature/x"])
    check("'feature/x' -> exit 0", rc == 0)
    check("'feature/x' invokes exactly one git push", len(pushes) == 1)
    check("push targets origin feature/x", pushes and "origin feature/x" in pushes[0])

    print("== branch + trailing flag -> flag forwarded intact ==")
    rc, out, err, pushes = run(["feature/x", "--force-with-lease"])
    check("'feature/x --force-with-lease' -> exit 0", rc == 0)
    check("--force-with-lease forwarded to git push",
          bool(pushes) and "--force-with-lease" in pushes[0])

    print("== no-arg -> current-branch fallback via symbolic-ref ==")
    rc, out, err, pushes = run([], cur_branch="feature/current")
    check("no-arg -> exit 0 (current-branch fallback)", rc == 0)
    check("no-arg pushes the symbolic-ref branch", bool(pushes) and "origin feature/current" in pushes[0])

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
