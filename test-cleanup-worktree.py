#!/usr/bin/env python3
"""Proof harness for cleanup-worktree.sh (issues #302, #303, #337, #421, #448).

#302 -- cleanup ran the LEAST on the most common path. Under `set -euo pipefail` the
remote-DELETE block tolerated ONLY a 404, so a squash-merge with auto-delete-branch
(which returns 422 "Reference does not exist") fell to `else` and `exit 1` -- ABOVE the
run-dir removal and the lint-cache clean, which therefore never ran. A second unguarded
network call, `git fetch --prune`, sat upstream of the same local cleanup, so fixing
only the 422 left the identical skip reachable. The structural fix is ORDERING: all
local cleanup runs before any network call, and the surviving network call tolerates
failure.

#337 -- the delete no longer classifies the HTTP status at all. It VERIFIES the outcome
with `git ls-remote --exit-code`: ref absent = success whatever the API said; ref
present = a real failure that surfaces the captured stderr and exits 1. This is immune
to every status-code variation and matches the house pattern (safe-push.sh verifies the
remote ref moved rather than trusting an exit code).

#421 -- a FAILED `git worktree remove` (an untracked file is enough) left the branch
checked out in the surviving worktree, so the local branch delete refused and `set -e`
aborted above the remote delete and `git fetch --prune`. Both branch deletes are now
gated on removal success; a failed removal keeps the branch, still prunes, and exits
non-zero on purpose.

#448 -- removing the worktree that holds the caller's cwd locks the session out (its
cwd vanishes). The script now refuses, before any destructive or network step, when the
physical cwd (`pwd -P`) is the worktree or under it, and never for a sibling that merely
shares a name prefix. Each case is mutation-proven: a copy of the script with the guard
broken (refusal removed, prefix match, `pwd -L`) must flip that case's verdict.

#303 -- the run dir comes SOLELY from the sourced run-paths.sh producer (via
CC_RUN_WORKTREE), captured BEFORE `git worktree remove`. No path is reconstructed here
and no sha12 is computed here; that second derivation is precisely the drift that let
the producer write `<prefix>-run/<basename>-<sha12>` while the consumer removed
`<prefix>-run/<basename>`, leaking 170+ stale dirs (922M).

This harness stubs `git`, `gh`, and `jq` via temp 0755 scripts first on PATH. It never
touches a real repository, a real remote, or the real cache dir (XDG_CACHE_HOME is
redirected into the temp tree). The real run-paths.sh IS exercised -- the producer and
consumer are proved to agree end-to-end rather than each proved against a fixture.

Run: python3 test-cleanup-worktree.py
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "scripts", "cleanup-worktree.sh")

FAILS = []
LAST = {}

# #448 mutations. Each one breaks the cwd guard a different way; a case that still
# passes under its mutant proves nothing.
MUT_NO_REFUSE = ("exit 1  # cwd-guard-refuse", ":  # cwd-guard-refuse")
MUT_PREFIX_MATCH = ('"$wt_real"/*)', '"$wt_real"*)')
MUT_NO_REALPATH = ('cwd_real=$(pwd -P 2>/dev/null)', 'cwd_real=$(pwd -L 2>/dev/null)')
MUT_UNRESOLVED_OPEN = ("exit 1  # cwd-guard-unresolved", ":  # cwd-guard-unresolved")


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


# --- stubs -----------------------------------------------------------------
#
# git: serves the porcelain worktree list, resolves the default branch, and drives the
# two failure levers this harness needs -- LS_REMOTE_RC (does the remote head still
# exist?) and FETCH_RC (does `git fetch --prune` fail?). Every invocation is logged so
# ORDERING can be asserted from evidence, which is the whole point of #302.
GIT_STUB = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'printf "%s\\n" "$*" >>"$GITLOG"\n'
    'case "$1 ${2:-}" in\n'
    '  "rev-parse --is-inside-work-tree") echo true; exit 0 ;;\n'
    '  "worktree list") cat "$PORCELAIN"; exit 0 ;;\n'
    '  "worktree remove") [ "${WT_REMOVE_RC:-0}" = "0" ] || exit "$WT_REMOVE_RC"; rm -rf "$3"; exit 0 ;;\n'
    '  "worktree prune") exit 0 ;;\n'
    '  "symbolic-ref --quiet") echo "origin/main"; exit 0 ;;\n'
    '  "show-ref --quiet") exit "${SHOW_REF_RC:-0}" ;;\n'
    '  "branch -d") exit "${BRANCH_D_RC:-0}" ;;\n'
    '  "branch -D") exit "${BRANCH_BIG_D_RC:-0}" ;;\n'
    '  "ls-remote --exit-code") exit "${LS_REMOTE_RC:-1}" ;;\n'
    '  "fetch --prune") exit "${FETCH_RC:-0}" ;;\n'
    "esac\n"
    "exit 0\n"
)

# gh: `repo view` answers the two -q queries cleanup makes; `api ... -X DELETE` replays
# a canned status/body pair on stderr and a canned exit code.
GH_STUB = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'printf "%s\\n" "$*" >>"$GHLOG"\n'
    'if [ "$1" = "repo" ]; then\n'
    '  case "$*" in\n'
    '    *nameWithOwner*) echo "acme/myrepo" ;;\n'
    '    *defaultBranchRef*) echo "main" ;;\n'
    "  esac\n"
    "  exit 0\n"
    "fi\n"
    'if [ "$1" = "api" ]; then\n'
    '  [ -n "${DELETE_STDERR:-}" ] && printf "%s\\n" "$DELETE_STDERR" >&2\n'
    '  exit "${DELETE_RC:-0}"\n'
    "fi\n"
    "exit 0\n"
)

JQ_STUB = (
    "#!/usr/bin/env bash\n"
    "# `printf '%s' \"$branch\" | jq -sRr @uri` -- URL-encode is irrelevant to these\n"
    "# assertions, so pass the branch through unchanged.\n"
    "cat\n"
)

# GitHub's real response after a squash-merge with auto-delete-branch already removed
# the head ref. This is THE payload that aborted cleanup before #302.
ERR_422_REF_GONE = 'gh: Reference does not exist (HTTP 422)'
ERR_404 = 'gh: Not Found (HTTP 404)'
ERR_422_OTHER = 'gh: Validation failed: something else entirely (HTTP 422)'
ERR_500 = 'gh: Internal Server Error (HTTP 500)'


def porcelain(main_wt, feature_wt):
    return (
        f"worktree {main_wt}\nHEAD {'0' * 40}\nbranch refs/heads/main\n\n"
        f"worktree {feature_wt}\nHEAD {'1' * 40}\nbranch refs/heads/feat/thing\n\n"
    )


def run_cleanup(td, *, suffix="1234", delete_rc=0, delete_stderr="", ls_remote_rc=1,
                fetch_rc=0, make_run_dir=True, worktree_exists=True,
                extra_env=None, golangci=True, wt_remove_rc=0, producer=True,
                cwd="main", mutate=None, wt_unreadable=False):
    """Set up a fake repo tree, create the run dir at the PRODUCER's exact path, and run
    cleanup-worktree.sh. Returns (rc, stdout, stderr, run_dir, git_log, gh_log).

    cwd selects where the script runs (#448): "main" (the main checkout), "worktree"
    (the worktree being removed), "worktree_sub" (a subdirectory of it), "worktree_link"
    (a symlink to it, so only `pwd -P` can see through), or "sibling" (a directory whose
    name shares the worktree's prefix). mutate is (old, new): run a COPY of the script
    with that one substitution, to prove an assertion has teeth. LAST records the
    feature worktree path for the caller."""
    root = tempfile.mkdtemp(dir=td)
    bindir = os.path.join(root, "bin"); os.makedirs(bindir)
    for name, body in (("git", GIT_STUB), ("gh", GH_STUB), ("jq", JQ_STUB)):
        p = os.path.join(bindir, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)
    if golangci:
        # A stub golangci-lint that records the invocation AND the GOLANGCI_LINT_CACHE it
        # actually ran under. Recording argv alone is not enough: the real binary honors
        # that variable over the default location, so `cache clean` cleans a DIFFERENT
        # cache depending on the environment, and an argv-only assertion passes
        # identically whether the global cache or the per-worktree one was hit.
        p = os.path.join(bindir, "golangci-lint")
        with open(p, "w") as f:
            f.write('#!/usr/bin/env bash\n'
                    'printf "%s CACHE=[%s]\\n" "$*" "${GOLANGCI_LINT_CACHE:-<unset>}" >>"$LINTLOG"\n'
                    'exit 0\n')
        os.chmod(p, 0o755)

    main_wt = os.path.join(root, "myrepo"); os.makedirs(main_wt)
    feature_wt = os.path.join(root, f"myrepo-{suffix}")
    if worktree_exists:
        os.makedirs(feature_wt)

    pfile = os.path.join(root, "porcelain.txt")
    with open(pfile, "w") as f:
        f.write(porcelain(main_wt, feature_wt))

    cache = os.path.join(root, "cache")
    glog = os.path.join(root, "git.log"); open(glog, "w").close()
    ghlog = os.path.join(root, "gh.log"); open(ghlog, "w").close()
    lintlog = os.path.join(root, "lint.log"); open(lintlog, "w").close()

    env = dict(os.environ)
    env.update({
        "PATH": bindir + os.pathsep + env.get("PATH", ""),
        "PORCELAIN": pfile, "GITLOG": glog, "GHLOG": ghlog, "LINTLOG": lintlog,
        "XDG_CACHE_HOME": cache,
        "DELETE_RC": str(delete_rc), "DELETE_STDERR": delete_stderr,
        "LS_REMOTE_RC": str(ls_remote_rc), "FETCH_RC": str(fetch_rc),
        "WT_REMOVE_RC": str(wt_remove_rc),
    })
    env.pop("CC_SKIP_GLOBAL_LINT_CACHE_CLEAN", None)
    if extra_env:
        env.update(extra_env)

    # Ask the REAL producer where the run dir belongs, then create it there. This is
    # what gives Case (c) its teeth: a cleanup that reverts to the old hashless path
    # cannot find this directory.
    probe_env = dict(env)
    probe_env["CC_RUN_WORKTREE"] = feature_wt
    probe_env["CC_RUN_NO_MKDIR"] = "1"
    probe = subprocess.run(
        ["bash", "-c", f'. {os.path.join(HERE, "scripts", "run-paths.sh")}; printf "%s" "$CC_RUN_DIR"'],
        cwd=main_wt, env=probe_env, capture_output=True, text=True, timeout=60)
    run_dir = probe.stdout.strip()
    if make_run_dir and run_dir:
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "sentinel.txt"), "w") as f:
            f.write("artifact\n")

    # The consumer locates the producer relative to its OWN path, then falls back to
    # ~/.claude/scripts. To exercise the producer-absent branch, run a COPY of the script
    # from a directory with no run-paths.sh beside it and a $HOME that has none either.
    script_to_run = SCRIPT
    if not producer:
        lone = os.path.join(root, "lone"); os.makedirs(lone)
        script_to_run = os.path.join(lone, "cleanup-worktree.sh")
        with open(SCRIPT) as src, open(script_to_run, "w") as dst:
            dst.write(src.read())
        os.chmod(script_to_run, 0o755)
        env["HOME"] = os.path.join(root, "nohome")
    if mutate is not None:
        old, new = mutate
        mdir = os.path.join(root, "mutant"); os.makedirs(mdir)
        script_to_run = os.path.join(mdir, "cleanup-worktree.sh")
        with open(SCRIPT) as src:
            text = src.read()
        assert old in text, f"mutation anchor not found: {old!r}"
        with open(script_to_run, "w") as dst:
            dst.write(text.replace(old, new))
        # Keep the producer beside the mutant so only the mutated line differs.
        with open(os.path.join(HERE, "scripts", "run-paths.sh")) as src, \
                open(os.path.join(mdir, "run-paths.sh"), "w") as dst:
            dst.write(src.read())

    if cwd == "main":
        run_cwd = main_wt
    elif cwd == "worktree":
        run_cwd = feature_wt
    elif cwd == "worktree_sub":
        run_cwd = os.path.join(feature_wt, "sub", "dir"); os.makedirs(run_cwd)
    elif cwd == "worktree_link":
        run_cwd = os.path.join(root, "link-to-wt"); os.symlink(feature_wt, run_cwd)
    elif cwd == "sibling":
        run_cwd = feature_wt + "2"; os.makedirs(run_cwd)
    else:
        raise ValueError(cwd)
    LAST["feature_wt"] = feature_wt
    # What a shell that `cd`-ed there would export; bash keeps an inherited PWD that names
    # the real cwd, so the symlink case presents its LOGICAL path exactly as in real use.
    env["PWD"] = run_cwd

    # wt_unreadable (Copilot on #451): a mode-000 worktree makes `cd` fail, so the guard
    # cannot resolve its physical path. It must fail CLOSED, not skip the containment check.
    if wt_unreadable:
        os.chmod(feature_wt, 0)
    try:
        p = subprocess.run(["bash", script_to_run, suffix], cwd=run_cwd, env=env,
                           capture_output=True, text=True, timeout=120)
    finally:
        if wt_unreadable and os.path.isdir(feature_wt):
            os.chmod(feature_wt, 0o755)
    with open(glog) as f:
        git_log = f.read()
    with open(ghlog) as f:
        gh_log = f.read()
    with open(lintlog) as f:
        lint_log = f.read()
    return p.returncode, p.stdout, p.stderr, run_dir, git_log, gh_log, lint_log


def main():
    print("cleanup-worktree.sh harness (#302 / #303 / #337 / #421 / #448)")

    # ---------------------------------------------------------------------
    # CASE A (#302 / #337): the 422 that aborted every squash-merge cleanup.
    # ---------------------------------------------------------------------
    print("\nCASE A -- 422 'Reference does not exist' with the ref verifiably gone")
    with tempfile.TemporaryDirectory() as td:
        rc, out, err, run_dir, _, _, lint = run_cleanup(
            td, delete_rc=1, delete_stderr=ERR_422_REF_GONE, ls_remote_rc=1)
        check("exit 0 (the 422 no longer aborts the run)", rc == 0)
        check("the already-deleted branch is taken and reported",
              "already deleted" in out.lower())
        check("the run dir was REMOVED (the 922M leak)",
              run_dir != "" and not os.path.isdir(run_dir))
        check("the lint-cache clean RAN (it used to sit downstream of the abort)",
              "cache clean" in lint)
        # The fallback exists to clear the SHARED cache. Sourcing run-paths.sh exports
        # GOLANGCI_LINT_CACHE into cleanup's own shell, so without `env -u` the clean
        # would target the per-worktree cache that was just deleted -- inert, while
        # still logging "cache clean". Assert the ENVIRONMENT, not the argv.
        check("the clean targets the GLOBAL cache, not the per-worktree one "
              "(GOLANGCI_LINT_CACHE unset for the call)",
              "CACHE=[<unset>]" in lint)
        check("the API's complaint is kept as an annotation, not swallowed",
              "422" in out or "422" in err)

    # 404 must behave identically -- the point of #337 is that the status code stopped
    # being the discriminator at all.
    print("\nCASE B -- 404 behaves identically (status code is no longer the verdict)")
    with tempfile.TemporaryDirectory() as td:
        rc, out, _, run_dir, _, _, lint = run_cleanup(
            td, delete_rc=1, delete_stderr=ERR_404, ls_remote_rc=1)
        check("exit 0", rc == 0)
        check("run dir removed", run_dir != "" and not os.path.isdir(run_dir))
        check("lint-cache clean ran", "cache clean" in lint)

    # A 422 for a DIFFERENT reason, with the ref STILL PRESENT, must still fail loudly.
    # This is the half a widened `grep -qE '404|422'` would have silently swallowed.
    print("\nCASE C -- a genuine failure (ref still present) still fails LOUDLY")
    with tempfile.TemporaryDirectory() as td:
        rc, out, err, run_dir, _, _, _ = run_cleanup(
            td, delete_rc=1, delete_stderr=ERR_422_OTHER, ls_remote_rc=0)
        check("exit 1 (loud failure preserved)", rc == 1)
        check("the captured API stderr is surfaced", "something else entirely" in err)
        check("the failure names the verified condition, not a status code",
              "still present" in err.lower())
        check("local cleanup ALREADY RAN before the failing network call (#302 ordering)",
              run_dir != "" and not os.path.isdir(run_dir))

    with tempfile.TemporaryDirectory() as td:
        rc, out, err, _, _, _, _ = run_cleanup(
            td, delete_rc=1, delete_stderr=ERR_500, ls_remote_rc=0)
        check("a 500 with the ref still present also exits 1", rc == 1)

    # The inverse trap: the API reports SUCCESS but the ref survives. Trusting the exit
    # code would report a clean delete; verification catches it.
    print("\nCASE D -- API reports success but the ref survives -> still a failure")
    with tempfile.TemporaryDirectory() as td:
        rc, out, err, _, _, _, _ = run_cleanup(td, delete_rc=0, ls_remote_rc=0)
        check("exit 1 despite a successful DELETE exit code", rc == 1)
        check("reported as a still-present ref", "still present" in err.lower())

    # ---------------------------------------------------------------------
    # CASE E (#302): the SECOND network exit. `git fetch --prune` used to sit upstream
    # of local cleanup, so fixing only the 422 left the identical skip reachable.
    # ---------------------------------------------------------------------
    print("\nCASE E -- a failing `git fetch --prune` cannot skip local cleanup")
    with tempfile.TemporaryDirectory() as td:
        rc, out, err, run_dir, git_log, _, lint = run_cleanup(
            td, delete_rc=1, delete_stderr=ERR_422_REF_GONE, ls_remote_rc=1, fetch_rc=1)
        check("exit 0 (a prune failure loses only the prune)", rc == 0)
        check("the prune failure is reported", "prune" in err.lower())
        check("run dir still removed", run_dir != "" and not os.path.isdir(run_dir))
        check("lint-cache clean still ran", "cache clean" in lint)

        # Ordering asserted from the invocation log, not from the exit code.
        fetch_at = git_log.find("fetch --prune")
        check("`git fetch --prune` is the LAST network call, after all local cleanup",
              fetch_at != -1 and fetch_at > git_log.find("worktree remove"))

    # ---------------------------------------------------------------------
    # CASE F (#303): the run dir comes from the producer, at the producer's exact path,
    # captured before the worktree is removed.
    # ---------------------------------------------------------------------
    print("\nCASE F -- the run dir matches the PRODUCER's path (one producer, #303)")
    with tempfile.TemporaryDirectory() as td:
        rc, out, _, run_dir, _, _, _ = run_cleanup(td, delete_rc=0, ls_remote_rc=1)
        check("the removed path carries the producer's -<sha12> suffix "
              "(reverting to the hashless path fails here)",
              run_dir != "" and len(os.path.basename(run_dir).rsplit("-", 1)[-1]) == 12)
        check("cleanup announced removing exactly the producer's path", run_dir in out)
        check("the run dir is gone", not os.path.isdir(run_dir))

        # No sha12 may be computed inside cleanup-worktree.sh and no run-dir path may be
        # assembled there -- that second derivation IS the drift #303 makes
        # unrepresentable. Comment lines are stripped first: the surrounding comments
        # necessarily QUOTE the very forms being banned (`<prefix>-run/<basename>`,
        # "sha12"), and an assertion that matched its own explanatory prose would fail
        # on a correct file and pass on nothing.
        with open(SCRIPT) as f:
            code = "\n".join(ln for ln in f.read().splitlines()
                             if not ln.lstrip().startswith("#"))
        check("cleanup-worktree.sh computes no hash of its own (no second derivation)",
              "shasum" not in code and "sha256sum" not in code)
        check("cleanup-worktree.sh assembles no run-dir path of its own",
              "-run/" not in code and "XDG_CACHE_HOME" not in code)

    # An already-removed worktree (the common resume/rerun case) must still find and
    # remove the same run dir -- the whole reason the producer hashes a string.
    print("\nCASE G -- an already-removed worktree still resolves the same run dir")
    with tempfile.TemporaryDirectory() as td:
        rc, out, _, run_dir, _, _, _ = run_cleanup(
            td, delete_rc=1, delete_stderr=ERR_422_REF_GONE, ls_remote_rc=1,
            worktree_exists=False)
        check("exit 0", rc == 0)
        check("run dir removed even though the worktree dir was already gone",
              run_dir != "" and not os.path.isdir(run_dir))

    # ---------------------------------------------------------------------
    # CASE H: the mid-transition lint-cache opt-out.
    # ---------------------------------------------------------------------
    print("\nCASE H -- the global lint-cache clean is opt-out, not removed")
    with tempfile.TemporaryDirectory() as td:
        rc, out, _, _, _, _, lint = run_cleanup(
            td, delete_rc=0, ls_remote_rc=1,
            extra_env={"CC_SKIP_GLOBAL_LINT_CACHE_CLEAN": "1"})
        check("exit 0", rc == 0)
        check("the global clean is SKIPPED when opted out", "cache clean" not in lint)
        check("the skip is announced, not silent", "Skipping global golangci" in out)

    with tempfile.TemporaryDirectory() as td:
        rc, out, _, run_dir, _, _, _ = run_cleanup(
            td, delete_rc=0, ls_remote_rc=1, golangci=False)
        check("cleanup succeeds with golangci-lint absent (no lint-tooling requirement)",
              rc == 0 and not os.path.isdir(run_dir))

    # ---------------------------------------------------------------------
    # CASE I2: a FAILING `git worktree remove` is the one remaining command that could
    # abort above local cleanup (it refuses on a dirty worktree -- an untracked build
    # artifact is enough). It must not resurrect the #302 leak shape. The run dir is
    # deliberately KEPT here: the worktree is still live and its run dir may hold an
    # in-flight gate's lock, so deleting it would be a concurrency wipe.
    # ---------------------------------------------------------------------
    print("\nCASE I2 -- a failing `git worktree remove` does not abort the run")
    with tempfile.TemporaryDirectory() as td:
        rc, out, err, run_dir, _, _, lint = run_cleanup(
            td, delete_rc=1, delete_stderr=ERR_422_REF_GONE, ls_remote_rc=1,
            wt_remove_rc=1)
        check("exit NON-zero on purpose (#421: the cleanup is incomplete)", rc != 0)
        check("the failure is surfaced with the force-remove remedy",
              "FAILED" in err and "--force" in err)
        check("the run dir is KEPT (the worktree is still live; may hold a gate lock)",
              run_dir != "" and os.path.isdir(run_dir))
        check("the keep is announced, not silent", "Keeping run dir" in out)
        check("the local cleanup still ran (lint cache)", "cache clean" in lint)

    # ---------------------------------------------------------------------
    # CASE K (#421): a failed removal (the untracked-file case) left the branch checked
    # out in the surviving worktree, so `git branch -d/-D` refused and `set -e` aborted
    # ABOVE the remote delete and `git fetch --prune`. The stub makes both branch deletes
    # fail exactly as real git does in that state, so a regression aborts here.
    # ---------------------------------------------------------------------
    print("\nCASE K -- failed removal (untracked file): branches KEPT, prune still runs (#421)")
    with tempfile.TemporaryDirectory() as td:
        rc, out, err, run_dir, git_log, gh_log, _ = run_cleanup(
            td, delete_rc=0, ls_remote_rc=1, wt_remove_rc=1,
            extra_env={"BRANCH_D_RC": "1", "BRANCH_BIG_D_RC": "1"})
        check("exit non-zero on purpose", rc != 0)
        check("the local branch delete was SKIPPED",
              "branch -d" not in git_log and "branch -D" not in git_log)
        check("the remote branch delete was SKIPPED (no gh api DELETE)",
              "-X DELETE" not in gh_log)
        check("`git fetch --prune` STILL RAN", "fetch --prune" in git_log)
        check("the keep is reported plainly ('kept')", "kept" in (out + err).lower()
              and "Keeping branch" in err)
        check("the untracked-file hint is still given",
              "untracked" in err.lower())

    # ---------------------------------------------------------------------
    # CASE J: the producer is not deployed. Cleanup must degrade LOUDLY and still do
    # everything else, never reconstruct the path itself.
    # ---------------------------------------------------------------------
    print("\nCASE J -- run-paths.sh absent: loud degradation, no local path reconstruction")
    with tempfile.TemporaryDirectory() as td:
        rc, out, err, run_dir, _, _, lint = run_cleanup(
            td, delete_rc=0, ls_remote_rc=1, producer=False)
        check("exit 0", rc == 0)
        check("the missing producer is reported LOUDLY with the fix",
              "run-paths.sh not found" in err and "configure --apply" in err)
        check("the run dir is left alone (never reconstructed locally)",
              run_dir != "" and os.path.isdir(run_dir))
        check("the rest of cleanup still ran", "cache clean" in lint)

    # ---------------------------------------------------------------------
    # CASE L (#448): removing the worktree that holds the caller's cwd locks the session
    # out (its cwd vanishes). Refuse BEFORE anything destructive or networked.
    # ---------------------------------------------------------------------
    def refused(label, cwd, mutate=None, wt_unreadable=False):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err, run_dir, git_log, gh_log, lint = run_cleanup(
                td, delete_rc=0, ls_remote_rc=1, cwd=cwd, mutate=mutate,
                wt_unreadable=wt_unreadable)
            return {
                "rc": rc, "err": err,
                "nothing_touched": (
                    "worktree remove" not in git_log and "worktree prune" not in git_log
                    and "branch -" not in git_log and "fetch" not in git_log
                    and gh_log == "" and lint == ""
                    and os.path.isdir(LAST["feature_wt"])
                    and run_dir != "" and os.path.isdir(run_dir)),
            }

    for cwd, what in (("worktree", "cwd == the worktree"),
                      ("worktree_sub", "cwd in a SUBDIRECTORY of the worktree"),
                      ("worktree_link", "cwd reached through a SYMLINK to the worktree")):
        print(f"\nCASE L -- {what} is REFUSED (#448)")
        r = refused(what, cwd)
        check(f"{what}: exit non-zero", r["rc"] != 0)
        check(f"{what}: nothing removed, deleted, or fetched (refused before every step)",
              r["nothing_touched"])
        check(f"{what}: says to run from the main checkout",
              "main checkout" in r["err"] and "refusing" in r["err"].lower())
        # Mutation proof: with the refusal neutralized the same run proceeds and removes
        # the worktree, so the assertions above are not passing by accident.
        m = refused(what, cwd, mutate=MUT_NO_REFUSE)
        check(f"{what}: MUTANT without the refusal proceeds (the case has teeth)",
              not m["nothing_touched"])
    m = refused("symlink", "worktree_link", mutate=MUT_NO_REALPATH)
    check("symlinked cwd: MUTANT using `pwd -L` is NOT refused (pwd -P is load-bearing)",
          not m["nothing_touched"])

    print("\nCASE L -- an UNRESOLVABLE worktree path fails CLOSED (Copilot on #451)")
    if os.geteuid() == 0:
        # root ignores mode 000, so the lever cannot fire (a uid-0 run would invent a pass).
        print("  [skip] running as root: chmod 000 does not make cd fail")
    else:
        r = refused("unresolvable", "main", wt_unreadable=True)
        check("unresolvable worktree path: exit non-zero", r["rc"] != 0)
        check("unresolvable worktree path: nothing removed, deleted, or fetched",
              r["nothing_touched"])
        check("unresolvable worktree path: says it could not resolve the path",
              "could not resolve" in r["err"])
        m = refused("unresolvable", "main", mutate=MUT_UNRESOLVED_OPEN, wt_unreadable=True)
        check("unresolvable: MUTANT that skips the check proceeds (the case has teeth)",
              not m["nothing_touched"])

    print("\nCASE L -- a SIBLING dir sharing the worktree's name prefix is NOT refused (#448)")
    with tempfile.TemporaryDirectory() as td:
        rc, out, err, run_dir, git_log, _, _ = run_cleanup(
            td, delete_rc=0, ls_remote_rc=1, cwd="sibling")
        check("sibling (/a/wt2 vs /a/wt): exit 0", rc == 0)
        check("sibling: the worktree WAS removed", "worktree remove" in git_log
              and not os.path.isdir(LAST["feature_wt"]))
        check("sibling: no refusal message", "refusing" not in err.lower())
    m = refused("sibling", "sibling", mutate=MUT_PREFIX_MATCH)
    check("sibling: MUTANT with a bare prefix match WRONGLY refuses (the case has teeth)",
          m["rc"] != 0 and m["nothing_touched"])

    print("\nCASE I -- absent run dir is a silent no-op (idempotent)")
    with tempfile.TemporaryDirectory() as td:
        rc, out, _, run_dir, _, _, _ = run_cleanup(
            td, delete_rc=0, ls_remote_rc=1, make_run_dir=False)
        check("exit 0", rc == 0)
        check("no removal is announced for a dir that does not exist",
              "Removing run dir" not in out)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("All cleanup-worktree.sh checks passed.")


if __name__ == "__main__":
    main()
