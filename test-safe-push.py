#!/usr/bin/env python3
"""Proof harness for safe-push.sh: branch-arg validation (#35, #432) + additive-vs-rewrite
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
import json
import os
import shutil
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "safe-push.sh")

FAILS = []
TREE = "ab" * 20
DROP = object()
VALID_RECEIPT = {"schema": "gate-receipt/v1", "commit_sha": "cd" * 20, "tree_sha": TREE,
                 "worktree": "/w", "result": "pass", "steps": [], "producer": "gate-runner"}


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
    '  # #318: the receipt leg asks for refs/heads/<b>^{tree}; TREE_SHA answers it (unset = fail).\n'
    '  for a in "$@"; do\n'
    '    case "$a" in *"^{tree}") if [ -n "${TREE_SHA:-}" ]; then echo "$TREE_SHA"; exit 0; fi; exit 128 ;; esac\n'
    '  done\n'
    '  # KNOWN_BRANCHES unset -> answer ANY name (the historical default). SET (space-separated)\n'
    '  # -> resolve only refs/heads/<known>, FAIL otherwise, like real git (#432): without this\n'
    '  # mode the missing-branch path was never exercised.\n'
    '  if [ -n "${KNOWN_BRANCHES+x}" ]; then\n'
    '    for b in $KNOWN_BRANCHES; do\n'
    '      if [ "$3" = "refs/heads/$b" ]; then echo "$LOCAL_SHA"; exit 0; fi\n'
    '    done\n'
    '    echo "fatal: Needed a single revision" >&2; exit 128\n'
    '  fi\n'
    '  echo "$LOCAL_SHA"; exit 0\n'
    "fi\n"
    'if [ "$1" = "remote" ] && [ "$#" -eq 1 ]; then\n'
    '  for r in ${REMOTES-origin}; do echo "$r"; done; exit 0\n'
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
    'if [ "$1" = "ls-remote" ] || [ "$1" = "fetch" ]; then echo "$1" >>"$NETLOG"; fi\n'
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
        post_push_remote=None, known_branches=None, remotes=None, receipt="valid",
        tree_sha=TREE, script=None, net=None, extra_env=None):
    """Invoke safe-push.sh with a stubbed git. Returns (rc, stdout, stderr, pushes, log)
    where pushes is the list of recorded `git push ...` argument strings and log is the
    content of safe-push's own log file (read before the tempdir is cleaned up).
    receipt: "valid" (the default, so every case exercises the leg's PASS path rather than
    bypassing it), a dict merged over the valid receipt, a raw str written verbatim, or None
    (no receipt file). net: a list that receives every ls-remote/fetch the stub saw."""
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        gitdir = os.path.join(td, "gitdir"); os.makedirs(gitdir)
        pushlog = os.path.join(td, "pushlog")
        netlog = os.path.join(td, "netlog")
        if receipt is not None:
            with open(os.path.join(gitdir, "prep-pr-receipt.json"), "w") as fh:
                if isinstance(receipt, str) and receipt != "valid":
                    fh.write(receipt)
                else:
                    body = dict(VALID_RECEIPT)
                    if isinstance(receipt, dict):
                        body.update(receipt)
                    json.dump({k: v for k, v in body.items() if v is not DROP}, fh)

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
        env["NETLOG"] = netlog
        if tree_sha is None:
            env.pop("TREE_SHA", None)
        else:
            env["TREE_SHA"] = tree_sha
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        env.update(extra_env or {})
        if known_branches is not None:
            env["KNOWN_BRANCHES"] = known_branches
        else:
            env.pop("KNOWN_BRANCHES", None)
        if remotes is not None:
            env["REMOTES"] = remotes
        else:
            env.pop("REMOTES", None)
        if cur_branch is not None:
            env["CUR_BRANCH"] = cur_branch
        else:
            env.pop("CUR_BRANCH", None)

        p = subprocess.run(["bash", script or SCRIPT] + args, env=env,
                           capture_output=True, text=True, timeout=15)
        pushes = []
        if os.path.exists(pushlog):
            with open(pushlog) as fh:
                pushes = [ln.rstrip("\n") for ln in fh if ln.strip()]
        if net is not None and os.path.exists(netlog):
            with open(netlog) as fh:
                net.extend(ln.strip() for ln in fh if ln.strip())
        log = ""
        logpath = os.path.join(gitdir, "safe-push.log")
        if os.path.exists(logpath):
            with open(logpath) as fh:
                log = fh.read()
        return p.returncode, p.stdout, p.stderr, pushes, log


def receipt_stub_cases():
    # (label, run kwargs, needle with the FULL validator present, needle from the INLINE check
    # alone). Both legs must refuse every case: exit 1, no push, no network, /prep-pr + --ungated.
    bad = [
        ("missing receipt", dict(receipt=None), "no gate receipt", "no gate receipt"),
        ("result=fail", dict(receipt={"result": "fail"}), "did not pass", "did not pass"),
        ("wrong producer", dict(receipt={"producer": "hand-rolled"}), "producer", "producer"),
        ("stale tree", dict(receipt={"tree_sha": "ef" * 20}), "STALE", "STALE"),
        ("not JSON", dict(receipt="{nope"), "not a valid gate-receipt/v1", "not readable JSON"),
        ("a JSON array, not an object", dict(receipt="[]"), "not a valid gate-receipt/v1",
         "not a JSON object"),
        ("tree_sha absent", dict(receipt={"tree_sha": DROP}), "not a valid gate-receipt/v1",
         "tree_sha is null"),
        ("tree_sha not 40-hex", dict(receipt={"tree_sha": "zz" * 20}), "not a valid gate-receipt/v1",
         "not a 40-hex SHA"),
        ("wrong schema name", dict(receipt={"schema": "gate-receipt/v2"}), "not a valid gate-receipt/v1",
         "receipt schema is"),
        ("branch tree unresolvable", dict(tree_sha=None), "cannot resolve the tree",
         "cannot resolve the tree"),
    ]
    with tempfile.TemporaryDirectory() as td:
        # A copy with NO orchestrate_schemas.py beside it and no CLAUDE_PLUGIN_ROOT (run() drops
        # it): the shape of the DEPLOYED ~/.claude/scripts copy the pr-shipper runs.
        lone = os.path.join(td, "safe-push.sh")
        shutil.copy(SCRIPT, lone)
        for leg, script, idx in (("validator present", SCRIPT, 2), ("validator ABSENT", lone, 3)):
            print(f"== #318 receipt leg, {leg}: every refusal is exit 1, no push, no network ==")
            for case in bad:
                label, kw, needle = case[0], case[1], case[idx]
                net = []
                rc, out, err, pushes, _log = run(["feature/x"], net=net, script=script, **kw)
                check(f"[{leg}] {label} -> exit 1, no push, no network, says '{needle}'",
                      rc == 1 and not pushes and not net and needle in err)
                check(f"[{leg}] {label} -> refusal points at /prep-pr and names --ungated",
                      "/prep-pr" in err and "--ungated" in err)
            rc, out, err, pushes, _log = run(["feature/x"], script=script)
            check(f"[{leg}] valid receipt -> exit 0 and says it verified the receipt",
                  rc == 0 and len(pushes) == 1 and "gate receipt verified" in err)
            rc, out, err, pushes, _log = run(["feature/x"], script=script,
                                             receipt={"tree_sha": TREE.upper()})
            check(f"[{leg}] an upper-case tree_sha (valid hex) still binds (exit 0)",
                  rc == 0 and len(pushes) == 1)
            rc, out, err, pushes, _log = run(["feature/x", "--ungated"], script=script, receipt=None)
            check(f"[{leg}] --ungated with no receipt -> exit 0 and one push", rc == 0 and len(pushes) == 1)
            check(f"[{leg}] --ungated is announced on stderr", "--ungated DECLARED" in err and "SKIPPED" in err)
            check(f"[{leg}] --ungated never reaches git push", bool(pushes) and "--ungated" not in pushes[0])
        rc, out, err, pushes, _log = run(["feature/x"], script=lone)
        check("validator ABSENT says so (load-bearing fields only), never silently",
              "load-bearing fields only" in err)
        rc, out, err, pushes, _log = run(["feature/x"])
        check("validator present does NOT claim the reduced check", "load-bearing" not in err)
        # PYTHONVERBOSE makes python write import traces to STDERR on success; only the inline
        # check's STDOUT may reach the tree bind, or a noisy interpreter reads as a STALE receipt.
        rc, out, err, pushes, _log = run(["feature/x"], script=lone, extra_env={"PYTHONVERBOSE": "1"})
        check("interpreter stderr noise does not corrupt the tree bind (exit 0)",
              rc == 0 and len(pushes) == 1)


def real_repo(td):
    """A throwaway repo whose origin is a LOCAL bare repo, so nothing leaves the temp dir.
    Returns (g, env, work, origin): g(*args, cwd=work) runs real git and returns stdout."""
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.path.join(td, "gitconfig"),
               GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    with open(env["GIT_CONFIG_GLOBAL"], "w") as fh:
        fh.write("[user]\n\tname = t\n\temail = t@example.invalid\n"
                 "[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n"
                 "[init]\n\tdefaultBranch = main\n")
    origin, work = os.path.join(td, "origin.git"), os.path.join(td, "work")

    def g(*a, cwd=work):
        return subprocess.run(["git", *a], cwd=cwd, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    g("init", "-q", "--bare", origin, cwd=td)
    g("clone", "-q", origin, work, cwd=td)
    with open(os.path.join(work, "f.txt"), "w") as fh:
        fh.write("base\n")
    g("add", "f.txt"); g("commit", "-q", "-m", "c0")
    g("push", "-q", "origin", "main")        # seed the base (inside the harness only)
    g("remote", "set-head", "origin", "main")
    return g, env, work, origin


def gate(g, cwd, ref, **over):
    """Write the receipt gate-runner --receipt would write, into cwd's git-dir, gating ref's tree."""
    body = dict(VALID_RECEIPT, commit_sha=g("rev-parse", ref, cwd=cwd),
                tree_sha=g("rev-parse", ref + "^{tree}", cwd=cwd), worktree=cwd)
    body.update(over)
    with open(os.path.join(g("rev-parse", "--absolute-git-dir", cwd=cwd), "prep-pr-receipt.json"), "w") as fh:
        json.dump(body, fh)


def safe_push(env, cwd, args):
    p = subprocess.run(["bash", SCRIPT] + args, cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout + p.stderr


def real_git_cases():
    print("== #466 REAL git: a TAG named like the branch does not break the push ==")
    with tempfile.TemporaryDirectory() as td:
        g, env, work, origin = real_repo(td)
        g("checkout", "-q", "-b", "dup"); g("commit", "-q", "--allow-empty", "-m", "c1")
        g("tag", "dup", "main")              # same name, DIFFERENT commit
        g("checkout", "-q", "main")
        gate(g, work, "dup")                 # dup is checked out nowhere: caller's git-dir
        rc, out = safe_push(env, work, ["dup"])
        remote = g("ls-remote", origin, "refs/heads/dup")
        check("branch + same-named tag -> exit 0", rc == 0)
        check("...and origin's refs/heads/dup is the BRANCH tip, not the tag",
              remote.split("\t")[0] == g("rev-parse", "refs/heads/dup"))
        check("...and no 'matches more than one' refspec error", "more than one" not in out)
        up = subprocess.run(["git", "config", "branch.dup.merge"], cwd=work, env=env,
                            capture_output=True, text=True).stdout.strip()
        check("...and -u still recorded the upstream", up == "refs/heads/dup")
        # --force-with-lease (auto-added by --rewrite) must compose with the full refspec: it
        # leases the same refs/heads/dup destination, so a real rewrite lands.
        g("checkout", "-q", "dup"); g("commit", "-q", "--amend", "--allow-empty", "-m", "c1b")
        g("checkout", "-q", "main")
        gate(g, work, "dup")
        rc, out = safe_push(env, work, ["dup", "--rewrite"])
        remote = g("ls-remote", origin, "refs/heads/dup")
        check("rewrite + auto lease over the full refspec -> exit 0 and the new tip lands",
              rc == 0 and remote.split("\t")[0] == g("rev-parse", "refs/heads/dup"))

    print("== #318 REAL git: receipt of the worktree HOLDING the branch, pushed by name from elsewhere ==")
    with tempfile.TemporaryDirectory() as td:
        g, env, work, origin = real_repo(td)
        wt = os.path.join(td, "wt")
        g("worktree", "add", "-q", "-b", "feat", wt)
        with open(os.path.join(wt, "f.txt"), "a") as fh:
            fh.write("one\n")
        g("commit", "-q", "-am", "f1", cwd=wt)
        rc, out = safe_push(env, work, ["feat"])
        check("holder has NO receipt -> refused (exit 1), nothing pushed",
              rc == 1 and "no gate receipt" in out and g("ls-remote", origin, "refs/heads/feat") == "")
        gate(g, work, "feat")                # a receipt in the WRONG (caller's) git-dir
        rc, out = safe_push(env, work, ["feat"])
        check("a receipt in the caller's git-dir does not stand in for the holder's (exit 1)", rc == 1)
        os.remove(os.path.join(g("rev-parse", "--absolute-git-dir"), "prep-pr-receipt.json"))
        gate(g, wt, "HEAD")                  # ONLY the holder has a receipt now
        rc, out = safe_push(env, work, ["feat"])
        check("holder's passing receipt found from the shared checkout -> exit 0",
              rc == 0 and "gate receipt verified" in out)
        check("...and feat landed on origin", g("ls-remote", origin, "refs/heads/feat") != "")
        # dirty holder: an UNTRACKED file, then a modified tracked file - both refuse.
        with open(os.path.join(wt, "f.txt"), "a") as fh:
            fh.write("two\n")
        g("commit", "-q", "-am", "f2", cwd=wt)
        gate(g, wt, "HEAD")
        with open(os.path.join(wt, "new.txt"), "w") as fh:
            fh.write("x\n")
        rc, out = safe_push(env, work, ["feat"])
        check("holder with an UNTRACKED file -> refused (exit 1) naming uncommitted/untracked",
              rc == 1 and "untracked" in out)
        os.remove(os.path.join(wt, "new.txt"))
        with open(os.path.join(wt, "f.txt"), "a") as fh:
            fh.write("dirty\n")
        rc, out = safe_push(env, work, ["feat"])
        check("holder with a MODIFIED tracked file -> refused (exit 1)", rc == 1)
        g("checkout", "-q", "--", "f.txt", cwd=wt)
        # squash after the gate: new commit SHA, SAME tree -> the tree bind accepts it.
        gated_commit = g("rev-parse", "HEAD", cwd=wt)
        g("reset", "-q", "--soft", "main", cwd=wt); g("commit", "-q", "-m", "squashed", cwd=wt)
        squashed = g("rev-parse", "HEAD", cwd=wt)
        rc, out = safe_push(env, work, ["feat", "--rewrite"])
        check("squashed-same-tree (commit %s.. != gated %s..) -> exit 0" % (squashed[:7], gated_commit[:7]),
              rc == 0 and squashed != gated_commit)
        # a later commit on the branch makes the receipt STALE.
        with open(os.path.join(wt, "f.txt"), "a") as fh:
            fh.write("three\n")
        g("commit", "-q", "-am", "f3", cwd=wt)
        rc, out = safe_push(env, work, ["feat"])
        check("commit after the gate (tree moved) -> refused as STALE", rc == 1 and "STALE" in out)


def fix_round_1_cases():
    print("== #318 R2: one branch per call - no second refspec, no ref-widening flag ==")
    for extra in (["refs/heads/y:refs/heads/y"], ["y"], ["--all"], ["--tags"], ["--mirror"], ["--", "y"]):
        rc, out, err, pushes, _log = run(["feature/x"] + extra)
        check(f"forwarded {extra} -> exit 2, NO push", rc == 2 and not pushes)
    rc, out, err, pushes, _log = run(["feature/x", "-o", "ci.skip"])
    check("a flag with a SEPARATE value (-o ci.skip) is still forwarded intact",
          rc == 0 and bool(pushes) and pushes[0].endswith("-o ci.skip"))
    with tempfile.TemporaryDirectory() as td:        # probe P1, real git
        g, env, work, origin = real_repo(td)
        g("branch", "a"); g("branch", "x")
        gate(g, work, "a")
        rc, out = safe_push(env, work, ["a", "refs/heads/x:refs/heads/x"])
        check("real git: 'a refs/heads/x:refs/heads/x' -> exit 2, and NEITHER ref lands",
              rc == 2 and g("ls-remote", origin, "refs/heads/x") == ""
              and g("ls-remote", origin, "refs/heads/a") == "")

    def holder_repo(td):
        g, env, work, origin = real_repo(td)
        wt = os.path.join(td, "wt")
        g("worktree", "add", "-q", "-b", "feat", wt)
        with open(os.path.join(wt, "f.txt"), "a") as fh:
            fh.write("one\n")
        g("commit", "-q", "-am", "f1", cwd=wt)
        gate(g, wt, "HEAD")
        return g, env, work, origin, wt

    print("== #318 R3: a branch held by TWO worktrees is refused ==")
    with tempfile.TemporaryDirectory() as td:
        g, env, work, origin, wt = holder_repo(td)
        wt2 = os.path.join(td, "wt2")
        g("worktree", "add", "-q", "--force", wt2, "feat")
        with open(os.path.join(wt2, "junk.txt"), "w") as fh:
            fh.write("x\n")
        rc, out = safe_push(env, work, ["feat"])
        check("second holder (dirty) -> exit 1 naming the double checkout",
              rc == 1 and "worktrees have it checked out" in out)

    print("== #318 R4: status.showUntrackedFiles=no cannot hide an untracked file ==")
    with tempfile.TemporaryDirectory() as td:
        g, env, work, origin, wt = holder_repo(td)
        g("config", "status.showUntrackedFiles", "no")
        with open(os.path.join(wt, "new.txt"), "w") as fh:
            fh.write("x\n")
        rc, out = safe_push(env, work, ["feat"])
        check("untracked file under showUntrackedFiles=no -> refused", rc == 1 and "untracked" in out)

    print("== #318 R5: a worktree MID-REBASE of the branch refuses; a plain detached one is not a holder ==")
    with tempfile.TemporaryDirectory() as td:
        g, env, work, origin, wt = holder_repo(td)
        with open(os.path.join(work, "f.txt"), "a") as fh:
            fh.write("conflict\n")
        g("commit", "-q", "-am", "main-side")
        subprocess.run(["git", "rebase", "main"], cwd=wt, env=env, capture_output=True)
        gate(g, work, "feat")                # a same-tree receipt in the CALLER's git-dir
        rc, out = safe_push(env, work, ["feat"])
        check("mid-rebase holder -> exit 1 naming the rebase", rc == 1 and "mid-rebase" in out)
    with tempfile.TemporaryDirectory() as td:
        g, env, work, origin, wt = holder_repo(td)
        g("checkout", "-q", "--detach", cwd=wt)
        gate(g, work, "feat")
        rc, out = safe_push(env, work, ["feat"])
        check("plain detached worktree is not a holder: caller's receipt pushes (exit 0)", rc == 0)

    print("== #318 R6: status-read failure, prunable holder, and exact holder match ==")
    with tempfile.TemporaryDirectory() as td:
        g, env, work, origin, wt = holder_repo(td)
        with open(os.path.join(g("rev-parse", "--absolute-git-dir", cwd=wt), "index"), "w") as fh:
            fh.write("garbage")               # git status now FAILS in the holder
        rc, out = safe_push(env, work, ["feat"])
        check("holder whose status cannot be read -> refused (fail closed)",
              rc == 1 and "cannot read the status" in out)
    with tempfile.TemporaryDirectory() as td:
        g, env, work, origin, wt = holder_repo(td)
        gate(g, work, "feat")                # a same-tree receipt the fallback WOULD accept
        shutil.rmtree(wt)                    # removed without `git worktree prune`
        rc, out = safe_push(env, work, ["feat"])
        check("prunable holder -> refused with the prune hint (no caller fallback)",
              rc == 1 and "prune" in out)
    with tempfile.TemporaryDirectory() as td:
        g, env, work, origin = real_repo(td)
        wt = os.path.join(td, "wt")
        g("worktree", "add", "-q", "-b", "x/foo", wt)
        with open(os.path.join(wt, "junk.txt"), "w") as fh:
            fh.write("x\n")                 # x/foo's holder is dirty and has no receipt
        g("branch", "y/foo")
        gate(g, work, "y/foo")
        rc, out = safe_push(env, work, ["y/foo"])
        check("y/foo is not held by x/foo's worktree (exact match): pushes (exit 0)", rc == 0)


def main():
    print("== #35 leading-dash first positional -> exit 2, NO push ==")
    rc, out, err, pushes, _log = run(["-u", "origin", "main"])
    check("'-u origin main' -> exit 2", rc == 2)
    check("'-u ...' does not invoke git push", len(pushes) == 0)
    check("'-u ...' error names the branch-first usage", "branch name" in err)

    # #432: the stub's STRICT mode (KNOWN_BRANCHES set) makes `rev-parse --verify` fail for an
    # unknown name, as real git does, so the missing-branch exit 2 is finally exercised.
    print("== #432 nonexistent local branch -> exit 2, NO push ==")
    rc, out, err, pushes, _log = run(["no-such-branch"], known_branches="feature/x")
    check("nonexistent branch -> exit 2", rc == 2)
    check("nonexistent branch does not invoke git push", len(pushes) == 0)
    check("nonexistent branch error names the missing ref", "refs/heads/no-such-branch" in err)
    rc, out, err, pushes, _log = run(["feature/x"], known_branches="feature/x")
    check("strict stub: a KNOWN branch still pushes (exit 0)", rc == 0 and len(pushes) == 1)

    print("== #432 git-push argument order (`origin <branch>`) -> exit 2 naming the form ==")
    rc, out, err, pushes, _log = run(["origin", "feature/x"], known_branches="feature/x")
    check("'origin feature/x' -> exit 2", rc == 2)
    check("'origin feature/x' does not invoke git push", len(pushes) == 0)
    check("'origin feature/x' error says the remote is implicit and names the form",
          "remote is implicit" in err and "safe-push.sh <branch>" in err)
    rc, out, err, pushes, _log = run(["upstream", "feature/x"], known_branches="feature/x",
                                     remotes="origin upstream")
    check("'upstream feature/x' (any configured remote) -> exit 2, no push",
          rc == 2 and len(pushes) == 0 and "remote is implicit" in err)
    # a word that is NOT a configured remote is a branch name as before
    rc, out, err, pushes, _log = run(["feature/x", "--force-with-lease"], known_branches="feature/x",
                                     remotes="origin upstream")
    check("'<branch> --force-with-lease' is untouched by the remote check (exit 0)", rc == 0)
    # a remote name with NO local branch of that name gets the real remedy whatever follows it:
    # a bare `origin` or `origin --flag` used to fall through to the unhelpful
    # "refs/heads/origin does not exist" (hostile review of #432, M6)
    rc, out, err, pushes, _log = run(["origin", "--force-with-lease"], known_branches="feature/x")
    check("'origin --flag' (no local branch 'origin') -> exit 2 naming the remote remedy",
          rc == 2 and len(pushes) == 0 and "remote is implicit" in err)
    rc, out, err, pushes, _log = run(["origin"], known_branches="feature/x")
    check("bare 'origin' (no local branch 'origin') -> exit 2 naming the remote remedy",
          rc == 2 and len(pushes) == 0 and "remote is implicit" in err)
    # a genuine local branch that shares a remote's name, with no second word, still pushes
    rc, out, err, pushes, _log = run(["origin"], known_branches="origin feature/x")
    check("a real local branch named 'origin' (no second word) still pushes (exit 0)",
          rc == 0 and len(pushes) == 1)

    print("== first-push (no remote ref) -> ADDITIVE, proceeds ==")
    rc, out, err, pushes, _log = run(["feature/x"])  # remote_sha="" -> first-push
    check("first-push -> exit 0", rc == 0)
    check("first-push invokes exactly one git push", len(pushes) == 1)
    # #466: the FULL refspec, never the bare name (a same-named tag makes the bare name ambiguous).
    check("push targets origin refs/heads/feature/x:refs/heads/feature/x",
          bool(pushes) and "origin refs/heads/feature/x:refs/heads/feature/x" in pushes[0])

    print("== first-push + trailing flag -> flag forwarded intact ==")
    rc, out, err, pushes, _log = run(["feature/x", "--force-with-lease"])
    check("'--force-with-lease' -> exit 0", rc == 0)
    check("--force-with-lease forwarded", bool(pushes) and "--force-with-lease" in pushes[0])

    print("== no-arg -> current-branch fallback via symbolic-ref ==")
    rc, out, err, pushes, _log = run([], cur_branch="feature/current")
    check("no-arg -> exit 0 (current-branch fallback)", rc == 0)
    check("no-arg pushes the symbolic-ref branch",
          bool(pushes) and "origin refs/heads/feature/current:refs/heads/feature/current" in pushes[0])

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

    receipt_stub_cases()
    real_git_cases()
    fix_round_1_cases()

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
