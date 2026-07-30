#!/usr/bin/env python3
"""Proof harness for run-paths.sh (issue #303): THE single producer of a worktree's
run-artifact directory.

Contract under test:
  - SET mode (CC_RUN_WORKTREE non-empty) hashes the handed-over path string VERBATIM:
    no `git worktree list` lookup, no realpath, no on-disk existence check. This is the
    load-bearing amendment -- cleanup-worktree.sh must compute the SAME CC_RUN_DIR for a
    worktree that has already been removed AND pruned out of the worktree list, which is
    exactly when any lookup-based implementation would return nothing or diverge.
  - UNSET mode resolves the current worktree by MATCHING $PWD against the porcelain
    list and returns the RECORDED string -- deliberately not `git rev-parse
    --show-toplevel`, which realpath-resolves and hits the macOS /tmp vs /private/tmp
    divergence. Resolution may pick the record; only the recorded string is hashed.
  - SOURCING CONTRACT: never leaks `set -e`/`set -u` into the caller, never aborts the
    sourcing shell; every failure degrades to an EMPTY CC_RUN_DIR that a consumer's
    `[ -d ]` guard skips.
  - GOLANGCI_LINT_CACHE is exported under CC_RUN_DIR (the per-worktree lint cache that
    makes cross-worktree stale-path phantoms impossible), and only when CC_RUN_DIR is
    non-empty. GOCACHE is never touched.
  - CC_RUN_NO_MKDIR=1 computes the path without creating it (what cleanup-worktree.sh
    needs -- creating a dir it is about to delete would resurrect it).

This harness stubs `git` via a temp 0755 script first on PATH, so the porcelain list is
driven directly and no real repository or remote is involved.

Run: python3 test-run-paths.py
"""
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "run-paths.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


# `git worktree list --porcelain` is the ONLY git call the producer makes. The stub
# records every invocation so "the SET branch performs no lookup" is asserted from
# evidence rather than assumed.
GIT_STUB = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'printf "%s\\n" "$*" >>"$GITLOG"\n'
    'if [ "$1" = "worktree" ] && [ "$2" = "list" ]; then\n'
    # GIT_RC drives the failure axis: a git that exits non-zero (not a repo, git broken)
    # must degrade, never abort the sourcing shell. A stub that can only succeed cannot
    # prove the degradation paths at all.
    '  [ "${GIT_RC:-0}" = "0" ] || exit "$GIT_RC"\n'
    '  cat "$PORCELAIN"; exit 0\n'
    "fi\n"
    'exit "${GIT_RC:-0}"\n'
)


def porcelain(*worktrees):
    """Render a `git worktree list --porcelain` payload for the given paths (first = main)."""
    out = []
    for i, wt in enumerate(worktrees):
        out.append(f"worktree {wt}")
        out.append("HEAD " + "0" * 40)
        out.append("branch refs/heads/" + ("main" if i == 0 else f"feat/wt{i}"))
        out.append("")
    return "\n".join(out) + "\n"


def source_producer(td, *, cwd, porcelain_text, env_overrides=None, no_mkdir=True,
                    git_rc=0):
    """Source run-paths.sh in a clean bash and dump the resulting environment.

    Sources under `set -eu` on purpose: a producer that leaked a failing command or an
    unbound variable into the caller would abort here, so the sourcing contract is
    exercised by construction rather than by a separate assertion.
    Returns (rc, {name: value}, git_invocation_log).
    """
    bindir = os.path.join(td, "bin"); os.makedirs(bindir, exist_ok=True)
    gitp = os.path.join(bindir, "git")
    with open(gitp, "w") as f:
        f.write(GIT_STUB)
    os.chmod(gitp, 0o755)

    pfile = os.path.join(td, "porcelain.txt")
    with open(pfile, "w") as f:
        f.write(porcelain_text)
    glog = os.path.join(td, "git.log")
    open(glog, "w").close()

    env = dict(os.environ)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env["PORCELAIN"] = pfile
    env["GITLOG"] = glog
    env["XDG_CACHE_HOME"] = os.path.join(td, "cache")
    env["GIT_RC"] = str(git_rc)
    env.pop("CC_RUN_WORKTREE", None)
    env.pop("CC_RUN_NO_MKDIR", None)
    if no_mkdir:
        env["CC_RUN_NO_MKDIR"] = "1"
    if env_overrides:
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v

    # Source under the FULL `set -euo pipefail`, not merely `set -eu`. pipefail is the
    # strictest realistic caller (and cleanup-worktree.sh's own mode), and it is the
    # option that exposes a producer whose internal PIPELINE fails: without pipefail a
    # `git | awk` where git dies still exits 0 via awk, so a plain `set -eu` harness
    # cannot construct the abort at all.
    script = (
        "set -euo pipefail\n"
        f". {SCRIPT}\n"
        'printf "CC_RUN_ROOT=%s\\n" "${CC_RUN_ROOT:-}"\n'
        'printf "CC_RUN_DIR=%s\\n" "${CC_RUN_DIR:-}"\n'
        'printf "GOLANGCI_LINT_CACHE=%s\\n" "${GOLANGCI_LINT_CACHE:-<unset>}"\n'
        'printf "GOCACHE=%s\\n" "${GOCACHE:-<unset>}"\n'
        # Prove the producer did not leave its internals in the caller's shell.
        'printf "LEAKED=%s\\n" "$(set | grep -c "^_ccrp_" || true)"\n'
    )
    p = subprocess.run(["bash", "-c", script], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=60)
    vals = {}
    for line in p.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            vals[k] = v
    with open(glog) as f:
        log = f.read()
    return p.returncode, vals, log, p.stderr


def main():
    print("run-paths.sh producer harness (#303)")

    # ---------------------------------------------------------------------
    # CASE A (AMENDMENT 1): the SET branch hashes the string verbatim, so a live
    # invocation and an ALREADY-PRUNED invocation for the identical recorded string
    # yield the IDENTICAL CC_RUN_DIR. The pruned run's porcelain list does not contain
    # the worktree at ALL -- a lookup-based implementation returns empty or diverges.
    # ---------------------------------------------------------------------
    print("\nCASE A -- SET mode hashes the recorded string verbatim (amendment 1)")
    with tempfile.TemporaryDirectory() as td:
        main_wt = "/Users/x/Developer/myrepo"
        gone_wt = "/Users/x/Developer/myrepo-1234"

        live = porcelain(main_wt, gone_wt)     # worktree still registered
        pruned = porcelain(main_wt)            # worktree removed AND pruned

        rc_l, v_l, log_l, err_l = source_producer(
            td, cwd=td, porcelain_text=live, env_overrides={"CC_RUN_WORKTREE": gone_wt})
        rc_p, v_p, log_p, err_p = source_producer(
            td, cwd=td, porcelain_text=pruned, env_overrides={"CC_RUN_WORKTREE": gone_wt})

        check("live-worktree invocation yields a non-empty CC_RUN_DIR",
              rc_l == 0 and v_l.get("CC_RUN_DIR", "") != "")
        check("pruned-worktree invocation yields a non-empty CC_RUN_DIR",
              rc_p == 0 and v_p.get("CC_RUN_DIR", "") != "")
        check("live and pruned invocations yield the IDENTICAL CC_RUN_DIR "
              "(a porcelain lookup in the SET branch would diverge here)",
              v_l.get("CC_RUN_DIR") == v_p.get("CC_RUN_DIR") and v_p.get("CC_RUN_DIR", "") != "")
        check("CC_RUN_DIR carries the basename AND a 12-hex suffix",
              v_p.get("CC_RUN_DIR", "").startswith(
                  os.path.join(td, "cache", "myrepo-run", "myrepo-1234-"))
              and len(v_p.get("CC_RUN_DIR", "").rsplit("-", 1)[-1]) == 12
              and all(c in "0123456789abcdef" for c in v_p.get("CC_RUN_DIR", "").rsplit("-", 1)[-1]))

        # The ONLY porcelain read in SET mode is the main-worktree lookup that supplies
        # the <prefix>-run root. The identifier itself must involve no second lookup.
        check("SET mode reads the worktree list at most once (prefix only; no identifier lookup)",
              log_p.count("worktree list") <= 1)

        # A path that DOES NOT EXIST on disk is the normal case for cleanup; it must not
        # be treated as an error, and it must not realpath-resolve to something else.
        rc_n, v_n, _, _ = source_producer(
            td, cwd=td, porcelain_text=pruned,
            env_overrides={"CC_RUN_WORKTREE": "/Users/x/Developer/myrepo-never-existed"})
        check("a never-existed worktree path still produces a CC_RUN_DIR (no existence check)",
              rc_n == 0 and "myrepo-never-existed-" in v_n.get("CC_RUN_DIR", ""))

    # ---------------------------------------------------------------------
    # CASE B: a symlinked path must hash as the RECORDED string, not its resolved
    # target. This is the live macOS /tmp -> /private/tmp hazard: realpath-resolving
    # would produce a different identifier than the one git recorded, reintroducing the
    # producer/consumer divergence the whole issue is about.
    # ---------------------------------------------------------------------
    print("\nCASE B -- the recorded string is hashed, never its realpath")
    with tempfile.TemporaryDirectory() as td:
        real = os.path.join(td, "real", "myrepo-77"); os.makedirs(real)
        link_parent = os.path.join(td, "link"); os.makedirs(link_parent)
        link = os.path.join(link_parent, "myrepo-77")
        os.symlink(real, link)
        main_wt = os.path.join(td, "real", "myrepo"); os.makedirs(main_wt)

        _, v_link, _, _ = source_producer(
            td, cwd=td, porcelain_text=porcelain(main_wt, link),
            env_overrides={"CC_RUN_WORKTREE": link})
        _, v_real, _, _ = source_producer(
            td, cwd=td, porcelain_text=porcelain(main_wt, real),
            env_overrides={"CC_RUN_WORKTREE": real})
        check("a symlinked worktree path and its resolved target hash DIFFERENTLY "
              "(proves no realpath resolution of the hashed string)",
              v_link.get("CC_RUN_DIR") != v_real.get("CC_RUN_DIR")
              and v_link.get("CC_RUN_DIR", "") != "")

    # ---------------------------------------------------------------------
    # CASE C: UNSET / dev mode resolves the current worktree from the porcelain list.
    # ---------------------------------------------------------------------
    print("\nCASE C -- UNSET mode resolves the current worktree from the recorded list")
    with tempfile.TemporaryDirectory() as td:
        main_wt = os.path.join(td, "myrepo"); os.makedirs(main_wt)
        wt = os.path.join(td, "myrepo-88"); os.makedirs(os.path.join(wt, "scripts"))
        text = porcelain(main_wt, wt)

        _, v_root, _, _ = source_producer(td, cwd=wt, porcelain_text=text)
        _, v_sub, _, _ = source_producer(td, cwd=os.path.join(wt, "scripts"), porcelain_text=text)
        _, v_set, _, _ = source_producer(td, cwd=td, porcelain_text=text,
                                         env_overrides={"CC_RUN_WORKTREE": wt})

        check("dev mode from the worktree root resolves a CC_RUN_DIR",
              v_root.get("CC_RUN_DIR", "") != "")
        check("dev mode from a SUBDIRECTORY yields the same CC_RUN_DIR as the root",
              v_sub.get("CC_RUN_DIR") == v_root.get("CC_RUN_DIR"))
        check("dev mode and SET mode agree for the same worktree "
              "(the producer/consumer contract, asserted directly)",
              v_set.get("CC_RUN_DIR") == v_root.get("CC_RUN_DIR"))
        check("the longest matching record wins (nested worktree not swallowed by main)",
              os.path.basename(v_root.get("CC_RUN_DIR", "")).startswith("myrepo-88-"))

    # ---------------------------------------------------------------------
    # CASE D: sourcing contract + degradation. A producer that aborted, leaked shell
    # options, or emitted a garbage path would break every consumer that sources it.
    # ---------------------------------------------------------------------
    print("\nCASE D -- sourcing contract and empty-CC_RUN_DIR degradation")
    with tempfile.TemporaryDirectory() as td:
        main_wt = os.path.join(td, "myrepo"); os.makedirs(main_wt)

        # No worktree resolvable at all (empty porcelain, cwd outside any worktree).
        rc, vals, _, err = source_producer(td, cwd=td, porcelain_text="")
        check("unresolvable worktree does NOT abort the sourcing shell (rc 0 under set -eu)",
              rc == 0)
        check("unresolvable worktree degrades to an EMPTY CC_RUN_DIR",
              vals.get("CC_RUN_DIR") == "")
        check("degradation is LOUD on stderr, never silent",
              "run-paths.sh" in err and "CC_RUN_DIR is empty" in err)
        check("GOLANGCI_LINT_CACHE is NOT exported when CC_RUN_DIR is empty",
              vals.get("GOLANGCI_LINT_CACHE") == "<unset>")
        check("no _ccrp_* internals leak into the caller's shell",
              vals.get("LEAKED") == "0")

        # Shell OPTIONS must not leak either. The sourcing above runs under `set -eu`,
        # where a leaked `set -e` is invisible -- so assert it from a caller that has
        # errexit/nounset/pipefail OFF and check they are still off afterward. A
        # consumer that sources this and then relies on `cmd || fallback` would break
        # silently otherwise.
        wt = os.path.join(td, "myrepo-opt"); os.makedirs(wt)
        pfile2 = os.path.join(td, "porcelain-opt.txt")
        with open(pfile2, "w") as f:
            f.write(porcelain(main_wt, wt))
        env2 = dict(os.environ)
        env2["PATH"] = os.path.join(td, "bin") + os.pathsep + env2.get("PATH", "")
        env2.update({"PORCELAIN": pfile2, "GITLOG": os.path.join(td, "git-opt.log"),
                     "XDG_CACHE_HOME": os.path.join(td, "cache"),
                     "CC_RUN_WORKTREE": wt, "CC_RUN_NO_MKDIR": "1"})
        open(env2["GITLOG"], "w").close()
        p2 = subprocess.run(
            ["bash", "-c",
             "set +e +u +o pipefail\n"
             f". {SCRIPT}\n"
             'case "$-" in *e*) echo "ERREXIT_LEAKED" ;; esac\n'
             'case "$-" in *u*) echo "NOUNSET_LEAKED" ;; esac\n'
             '[ -o pipefail ] && echo "PIPEFAIL_LEAKED"\n'
             'echo "SURVIVED"\n'],
            cwd=td, env=env2, capture_output=True, text=True, timeout=60)
        check("sourcing does not leak set -e / -u / pipefail into the caller",
              "SURVIVED" in p2.stdout and "LEAKED" not in p2.stdout)

        # A FAILING git (exit 128: not a repo, git broken, git absent) must degrade, not
        # abort. This is the case the contract is really about: the producer's internal
        # `git | awk` pipeline succeeds via awk under plain `set -eu`, so ONLY a
        # pipefail caller surfaces git's status -- and the SET branch's file-scope
        # command substitution then trips the caller's errexit. Asserted in BOTH modes,
        # because the SET branch (what cleanup-worktree.sh uses) and the UNSET branch
        # reach the failing pipeline through different call sites.
        wt_fail = os.path.join(td, "myrepo-gitfail")
        for label, overrides in (("SET mode", {"CC_RUN_WORKTREE": wt_fail}),
                                 ("UNSET mode", None)):
            rc, vals, _, err = source_producer(
                td, cwd=td, porcelain_text=porcelain(main_wt, wt_fail),
                env_overrides=overrides, git_rc=128)
            check(f"a git that exits 128 does NOT abort the sourcing shell ({label}, "
                  "under set -euo pipefail)", rc == 0)
            check(f"a git that exits 128 degrades to an EMPTY CC_RUN_DIR ({label})",
                  vals.get("CC_RUN_DIR") == "")
            check(f"the git failure is reported on stderr ({label})",
                  "CC_RUN_DIR is empty" in err)

        # An unset HOME must DEGRADE, not kill the sourcing shell. This is the one expansion
        # that runs at file scope in the caller's shell, and under `set -u` a bare `$HOME`
        # there is an unbound-variable error that aborts the WHOLE shell -- a consumer's
        # `. run-paths.sh || true` does NOT rescue it, because set -u kills the shell rather
        # than just the `.` builtin. Real environments hit this: cron, systemd `User=` without
        # PAM, a CI runner, `env -i`. In cleanup-worktree.sh that would abort at the source
        # line, ABOVE all local cleanup -- the #302 pathology, relocated.
        for label, keep_xdg in (("neither HOME nor XDG_CACHE_HOME", False),
                                ("HOME unset but XDG_CACHE_HOME set", True)):
            ov = {"HOME": None, "CC_RUN_WORKTREE": os.path.join(td, "myrepo-nohome")}
            if not keep_xdg:
                ov["XDG_CACHE_HOME"] = None
            rc, vals, _, err = source_producer(
                td, cwd=td, porcelain_text=porcelain(main_wt, os.path.join(td, "myrepo-nohome")),
                env_overrides=ov)
            check(f"unset HOME does NOT abort the sourcing shell ({label})", rc == 0)
            if keep_xdg:
                # XDG_CACHE_HOME alone is sufficient; HOME is only the fallback.
                check("XDG_CACHE_HOME alone still yields a CC_RUN_DIR (HOME is just a fallback)",
                      vals.get("CC_RUN_DIR", "") != "")
            else:
                check("with neither var set, CC_RUN_DIR degrades to empty", vals.get("CC_RUN_DIR") == "")
                check("the no-cache-root degradation names the actual cause",
                      "XDG_CACHE_HOME" in err and "HOME" in err)

        # Healthy path: the lint cache lands under the run dir; GOCACHE stays untouched.
        wt = os.path.join(td, "myrepo-99"); os.makedirs(wt)
        _, vals, _, _ = source_producer(td, cwd=td, porcelain_text=porcelain(main_wt, wt),
                                        env_overrides={"CC_RUN_WORKTREE": wt})
        check("GOLANGCI_LINT_CACHE is the per-worktree dir under CC_RUN_DIR",
              vals.get("GOLANGCI_LINT_CACHE") == os.path.join(vals.get("CC_RUN_DIR", ""), "golangci"))
        check("GOCACHE is left untouched (content-addressed; shared safely)",
              vals.get("GOCACHE") == "<unset>")

        # A "/" basename would collapse the run dir to `<root>//` and merge unrelated
        # artifacts into one bucket -- degrade instead.
        _, vals, _, _ = source_producer(td, cwd=td, porcelain_text=porcelain(main_wt, "/"),
                                        env_overrides={"CC_RUN_WORKTREE": "/"})
        check("a '/' worktree path degrades to empty rather than collapsing the path",
              vals.get("CC_RUN_DIR") == "")

    # ---------------------------------------------------------------------
    # CASE E: mkdir behavior. Default creates 0700; CC_RUN_NO_MKDIR=1 does not create.
    # ---------------------------------------------------------------------
    print("\nCASE E -- directory creation (0700 by default; CC_RUN_NO_MKDIR=1 computes only)")
    with tempfile.TemporaryDirectory() as td:
        main_wt = os.path.join(td, "myrepo"); os.makedirs(main_wt)
        wt = os.path.join(td, "myrepo-mk"); os.makedirs(wt)
        text = porcelain(main_wt, wt)

        _, v_nm, _, _ = source_producer(td, cwd=td, porcelain_text=text,
                                        env_overrides={"CC_RUN_WORKTREE": wt}, no_mkdir=True)
        check("CC_RUN_NO_MKDIR=1 computes the path WITHOUT creating it "
              "(cleanup must not resurrect the dir it is deleting)",
              v_nm.get("CC_RUN_DIR", "") != "" and not os.path.isdir(v_nm["CC_RUN_DIR"]))

        _, v_mk, _, _ = source_producer(td, cwd=td, porcelain_text=text,
                                        env_overrides={"CC_RUN_WORKTREE": wt}, no_mkdir=False)
        created = v_mk.get("CC_RUN_DIR", "")
        check("default mode creates the run dir", created != "" and os.path.isdir(created))
        check("the created run dir is 0700 (artifacts may carry secrets)",
              created != "" and os.path.isdir(created)
              and (os.stat(created).st_mode & 0o777) == 0o700)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("All run-paths.sh checks passed.")


if __name__ == "__main__":
    main()
