#!/usr/bin/env python3
"""Proof harness for stale-branch-sweep.sh (#137 Phase 3).

The sweep is a read-only-decision / gated-delete reconciler for stale remote
heads. This harness stubs BOTH external dependencies (gh, git) so it is
deterministic and host-independent - it NEVER calls real gh/git:

  - `gh` and `git` are temp 0755 scripts placed first on PATH.
  - The stub gh serves canned PR-list / repo-view output from env vars, and
    records (or fails) the DELETE that gh-delete-branch.sh execs.
  - The stub git serves canned `git ls-remote --heads origin` output.

The script resolves gh-delete-branch.sh as a SIBLING of itself (the REAL
wrapper), whose final `exec gh api -X DELETE` lands on the stub gh. So delete
mode is exercised end-to-end without touching a live remote.

Contract asserted: exit 0 = success (dry-run or clean delete), exit 1 = a
deletion failed, exit 2 = usage / fail-closed read error.

Run: python3 test-stale-branch-sweep.py
"""
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "stale-branch-sweep.sh")
SHA = "a" * 40

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


def heads_blob(*branches):
    """Build a `git ls-remote --heads origin` style blob."""
    return "".join(f"{SHA}\trefs/heads/{b}\n" for b in branches)


def run(args, *, ls_remote="", default_branch="main", open_heads="",
        merged_heads="", closed_heads="", open_fail=False, delete_fail=False,
        merged_fail=False, closed_fail=False, default_fail=False):
    """Invoke the sweep with stubbed gh + git. Returns (rc, stdout, stderr)."""
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        del_log = os.path.join(td, "deletes.log")

        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "case \"${1:-}\" in\n"
                "  repo)\n"
                "    for a in \"$@\"; do case \"$a\" in\n"
                "      nameWithOwner) echo 'owner/repo'; exit 0;;\n"
                "      defaultBranchRef) [ -n \"${GH_DEFAULT_FAIL:-}\" ] && exit 1; printf '%s\\n' \"${GH_DEFAULT_BRANCH:-main}\"; exit 0;;\n"
                "    esac; done; exit 0;;\n"
                "  pr)\n"
                "    state=''; prev=''\n"
                "    for a in \"$@\"; do [ \"$prev\" = '--state' ] && state=\"$a\"; prev=\"$a\"; done\n"
                "    case \"$state\" in\n"
                "      open) [ -n \"${GH_OPEN_FAIL:-}\" ] && exit 1; printf '%s' \"${GH_OPEN_HEADS:-}\";;\n"
                "      merged) [ -n \"${GH_MERGED_FAIL:-}\" ] && exit 1; printf '%s' \"${GH_MERGED_HEADS:-}\";;\n"
                "      closed) [ -n \"${GH_CLOSED_FAIL:-}\" ] && exit 1; printf '%s' \"${GH_CLOSED_HEADS:-}\";;\n"
                "    esac; exit 0;;\n"
                "  api)\n"
                "    [ -n \"${GH_DELETE_FAIL:-}\" ] && exit 1\n"
                "    echo \"DELETE $*\" >> \"${GH_DELETE_LOG:-/dev/null}\"; exit 0;;\n"
                "esac\n"
                "exit 0\n"
            )
        os.chmod(gh, 0o755)

        git = os.path.join(bindir, "git")
        with open(git, "w") as f:
            f.write(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "if [ \"${1:-}\" = 'ls-remote' ]; then printf '%s' \"${GIT_LSREMOTE:-}\"; exit 0; fi\n"
                "exit 0\n"
            )
        os.chmod(git, 0o755)

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["GIT_LSREMOTE"] = ls_remote
        env["GH_DEFAULT_BRANCH"] = default_branch
        env["GH_OPEN_HEADS"] = open_heads
        env["GH_MERGED_HEADS"] = merged_heads
        env["GH_CLOSED_HEADS"] = closed_heads
        env["GH_DELETE_LOG"] = del_log
        if open_fail:
            env["GH_OPEN_FAIL"] = "1"
        if delete_fail:
            env["GH_DELETE_FAIL"] = "1"
        if merged_fail:
            env["GH_MERGED_FAIL"] = "1"
        if closed_fail:
            env["GH_CLOSED_FAIL"] = "1"
        if default_fail:
            env["GH_DEFAULT_FAIL"] = "1"

        p = subprocess.run([SCRIPT] + args, env=env, capture_output=True, text=True, timeout=20)
        deletes = ""
        if os.path.exists(del_log):
            with open(del_log) as fh:
                deletes = fh.read()
        return p.returncode, p.stdout, p.stderr, deletes


def main():
    print("== arg validation ==")
    rc, out, _, _ = run(["--help"])
    check("--help -> exit 0", rc == 0 and "stale-branch-sweep" in out)
    rc, _, _, _ = run(["--bogus"])
    check("unknown flag -> exit 2", rc == 2)
    rc, _, _, _ = run(["owner/repo", "extra"])
    check("extra positional arg -> exit 2", rc == 2)

    print("== dry-run / no-match paths ==")
    rc, out, _, _ = run(["owner/repo"], ls_remote="")
    check("no remote heads -> exit 0", rc == 0)
    # A head whose PR is still open is never a candidate.
    rc, out, _, dels = run(["owner/repo"],
                           ls_remote=heads_blob("feat/live"),
                           open_heads="feat/live\n", merged_heads="", closed_heads="")
    check("head with an OPEN PR -> not a candidate, exit 0", rc == 0 and "would delete" not in out)
    # A head with no PR history at all is left alone.
    rc, out, _, _ = run(["owner/repo"],
                        ls_remote=heads_blob("random/manual"),
                        open_heads="", merged_heads="", closed_heads="")
    check("head with NO PR history -> left alone (nothing to do), exit 0",
          rc == 0 and "would delete" not in out)
    # main / master / default never deleted even if they appear merged.
    rc, out, _, _ = run(["owner/repo"],
                        ls_remote=heads_blob("main", "master", "develop"),
                        default_branch="develop",
                        merged_heads="main\nmaster\ndevelop\n")
    check("main/master/default never candidates -> exit 0, none listed",
          rc == 0 and "would delete" not in out)

    print("== dry-run match (no deletion) ==")
    rc, out, _, dels = run(["owner/repo"],
                           ls_remote=heads_blob("feat/done", "feat/live"),
                           open_heads="feat/live\n",
                           merged_heads="feat/done\n")
    check("merged head, no open PR -> listed as 'would delete' (dry-run)",
          rc == 0 and "would delete: feat/done" in out)
    check("dry-run performs NO deletion", dels == "")

    print("== fail-closed read errors (each gh read guarded separately) ==")
    rc, _, err, _ = run(["owner/repo"],
                        ls_remote=heads_blob("feat/done"),
                        open_fail=True, merged_heads="feat/done\n")
    check("open-PR read fails -> exit 2 (fail closed)", rc == 2)
    # F2: merged and closed are read SEPARATELY; either failing must abort even if
    # the other succeeds (a grouped read would mask the merged failure).
    rc, _, err, _ = run(["owner/repo"],
                        ls_remote=heads_blob("feat/done"),
                        merged_fail=True, closed_heads="")
    check("merged-PR read fails (closed ok) -> exit 2 (fail closed)", rc == 2)
    rc, _, err, _ = run(["owner/repo"],
                        ls_remote=heads_blob("feat/done"),
                        closed_fail=True, merged_heads="feat/done\n")
    check("closed-PR read fails (merged ok) -> exit 2 (fail closed)", rc == 2)
    # F4: default-branch read is load-bearing -> fail closed.
    rc, _, err, _ = run(["owner/repo"],
                        ls_remote=heads_blob("feat/done"),
                        default_fail=True, merged_heads="feat/done\n")
    check("default-branch read fails -> exit 2 (fail closed)", rc == 2)

    print("== delete mode ==")
    rc, out, _, dels = run(["--delete", "owner/repo"],
                           ls_remote=heads_blob("feat/done", "feat/live"),
                           open_heads="feat/live\n",
                           merged_heads="feat/done\n")
    check("--delete removes the orphan -> exit 0", rc == 0)
    check("--delete routed a DELETE for feat/done", "feat%2Fdone" in dels or "feat/done" in dels)
    # closed (not merged) PR head also reaps.
    rc, out, _, dels = run(["--delete", "owner/repo"],
                           ls_remote=heads_blob("fix/closed"),
                           closed_heads="fix/closed\n")
    check("closed-PR head also reaped in --delete -> exit 0", rc == 0 and dels != "")
    # a deletion failure surfaces as exit 1.
    rc, _, _, _ = run(["--delete", "owner/repo"],
                      ls_remote=heads_blob("feat/done"),
                      merged_heads="feat/done\n", delete_fail=True)
    check("a failed deletion -> exit 1", rc == 1)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
