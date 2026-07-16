#!/usr/bin/env python3
"""Proof harness for open-pr-staleness-sweep.sh (issue #282): the merge-side sweep that
notices the OTHER open PRs a just-landed merge left behind their base.

THE SAFETY HINGE under test is the REVIEWED predicate and its routing:
  reviewed = (reviews[] non-empty) OR (review-thread/comment count > 0)
  behind + NOT reviewed   -> refresh with a PLAIN `gh pr update-branch <n>` (DEFAULT
                             merge-commit mode; ADDITIVE)
  behind + REVIEWED       -> SURFACE only, never mutate (a HEAD-moving commit dismisses a
                             bot's prior approval and disturbs the incremental-review delta)
  predicate INDETERMINATE -> treat as REVIEWED -> SURFACE (fail toward surface, never toward
                             acting)
  update-branch denied / errors -> degrade to REPORT-ONLY, exit 0
  CROSS-REPOSITORY (fork) PR -> SKIP + SURFACE, never measured, never mutated (its headRefName
                             names a branch in the FORK, so origin/<head> is an unrelated ref)
  head fetch FAILS        -> UNKNOWN (surface), never measured against a stale local ref
`--rebase` MUST NEVER appear in ANY gh invocation on ANY path (it rewrites every commit SHA
and orphans the fix SHAs cited in review replies). Asserted globally, per case.

FAIL-OPEN contract: exit 0 on EVERY operational path (including a read failure) so the sweep
can never block /post-merge-cleanup. exit 2 ONLY on a malformed invocation.

The harness stubs `gh` and `git` via temp 0755 scripts first on PATH (the real
scripts/base-freshness.sh runs underneath, against the stubbed git). Every gh invocation is
recorded for assertion.

Run: python3 test-open-pr-staleness-sweep.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "scripts", "open-pr-staleness-sweep.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


GH_STUB = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'printf "%s\\n" "$*" >>"$GHLOG"\n'
    'if [ "$1" = "repo" ]; then echo "owner/name"; exit 0; fi\n'
    'if [ "$1" = "pr" ] && [ "$2" = "list" ]; then\n'
    '  [ "${LIST_RC:-0}" = "0" ] || exit "$LIST_RC"\n'
    '  for n in ${OPEN_PRS:-}; do echo "$n"; done\n'
    "  exit 0\n"
    "fi\n"
    'if [ "$1" = "pr" ] && [ "$2" = "view" ]; then\n'
    '  n="$3"\n'
    '  eval "rc=\\${VIEW_RC_$n:-0}"\n'
    '  [ "$rc" = "0" ] || exit "$rc"\n'
    '  # Distinguish the SNAPSHOT read (asks for baseRefName) from the TOCTOU RE-CHECK read\n'
    '  # (reviews,comments only). The recheck returns VIEW_RECHECK_$n if set, else mirrors the\n'
    '  # snapshot reviews (field 3) + comments (field 4) so existing cases are unchanged.\n'
    '  case "$*" in\n'
    '    *baseRefName*) eval "line=\\${VIEW_$n:-}"; printf "%s\\n" "$line"; exit 0 ;;\n'
    '    *) eval "rline=\\${VIEW_RECHECK_$n:-}"\n'
    '       if [ -n "$rline" ]; then printf "%s\\n" "$rline"\n'
    '       else eval "line=\\${VIEW_$n:-}"; printf "%s\\n" "$line" | cut -f3,4; fi\n'
    '       exit 0 ;;\n'
    '  esac\n'
    "fi\n"
    'if [ "$1" = "pr" ] && [ "$2" = "update-branch" ]; then\n'
    '  exit "${UPDATE_RC:-0}"\n'
    "fi\n"
    "exit 0\n"
)

# git stub: enough for base-freshness.sh (and the sweep's head fetch) to run. FETCH_FAIL_REF
# fails the fetch of ONE named ref (so a HEAD-fetch failure can be tested apart from the base).
GIT_STUB = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'if [ "$1" = "rev-parse" ] && [ "$2" = "--git-dir" ]; then echo "$GITDIR"; exit 0; fi\n'
    'if [ "$1" = "rev-parse" ] && [ "$2" = "--is-shallow-repository" ]; then echo false; exit 0; fi\n'
    'if [ "$1" = "fetch" ]; then\n'
    '  # Record the non-interactive env of EACH fetch (the head fetch here, the base fetch inside\n'
    '  # base-freshness) so the sweep\'s OWN head-fetch env can be asserted.\n'
    '  { echo "ARGS=$*"\n'
    '    echo "GIT_TERMINAL_PROMPT=${GIT_TERMINAL_PROMPT:-<unset>}"\n'
    '    echo "GIT_SSH_COMMAND=${GIT_SSH_COMMAND:-<unset>}"; } >>"${FETCHLOG:-/dev/null}"\n'
    '  [ -n "${FETCH_FAIL_REF:-}" ] && [ "$3" = "$FETCH_FAIL_REF" ] && exit 1\n'
    '  exit "${FETCH_RC:-0}"\n'
    "fi\n"
    'if [ "$1" = "rev-parse" ]; then echo "someSHA"; exit 0; fi\n'
    'if [ "$1" = "rev-list" ]; then echo "${BEHIND:-0}"; exit 0; fi\n'
    "exit 0\n"
)

TAB = "\t"


def view(base="main", head="feat/x", reviews="0", comments="0", cross="false"):
    """A stubbed `gh pr view --jq` TSV snapshot line: base, head, reviews-count, thread-count,
    isCrossRepository. A '?' in any position is the INDETERMINATE (unreadable/malformed) shape."""
    return TAB.join([base, head, reviews, comments, cross])


def view_recheck(reviews="0", comments="0"):
    """A stubbed TOCTOU RE-CHECK line: just reviews-count + thread/comment-count (2 fields)."""
    return TAB.join([reviews, comments])


def run(args, *, open_prs="", views=None, views_recheck=None, view_rcs=None, behind="0",
        list_rc=0, update_rc=0, fetch_fail_ref="", ssh_command=None, freshness_stub=None):
    """Invoke open-pr-staleness-sweep.sh with stubbed gh + git.
    Returns (rc, stdout, stderr, gh_invocations, fetchlog)."""
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        gitdir = os.path.join(td, "gitdir"); os.makedirs(gitdir)
        ghlog = os.path.join(td, "ghlog")
        fetchlog = os.path.join(td, "fetchlog")

        for name, body in (("gh", GH_STUB), ("git", GIT_STUB)):
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)

        # When a freshness_stub is given, run a COPY of the sweep next to a STUB base-freshness.sh so
        # the sweep's OWN result-gating is exercised independently of the real helper (e.g. a helper
        # that exits non-1 or emits a non-numeric count must NOT reach update-branch).
        script_to_run = SCRIPT
        if freshness_stub is not None:
            scriptsdir = os.path.join(td, "scripts"); os.makedirs(scriptsdir)
            script_to_run = os.path.join(scriptsdir, "open-pr-staleness-sweep.sh")
            shutil.copy(SCRIPT, script_to_run)
            fp = os.path.join(scriptsdir, "base-freshness.sh")
            with open(fp, "w") as f:
                f.write(freshness_stub)
            os.chmod(fp, 0o755)

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["GITDIR"] = gitdir
        env["GHLOG"] = ghlog
        env["FETCHLOG"] = fetchlog
        env["OPEN_PRS"] = open_prs
        env["BEHIND"] = behind
        env["LIST_RC"] = str(list_rc)
        env["UPDATE_RC"] = str(update_rc)
        env["FETCH_FAIL_REF"] = fetch_fail_ref
        env.pop("GIT_TERMINAL_PROMPT", None)
        if ssh_command is not None:
            env["GIT_SSH_COMMAND"] = ssh_command
        else:
            env.pop("GIT_SSH_COMMAND", None)
        for n, line in (views or {}).items():
            env[f"VIEW_{n}"] = line
        for n, line in (views_recheck or {}).items():
            env[f"VIEW_RECHECK_{n}"] = line
        for n, rc in (view_rcs or {}).items():
            env[f"VIEW_RC_{n}"] = str(rc)

        p = subprocess.run(["bash", script_to_run] + args, env=env,
                           capture_output=True, text=True, timeout=30)
        calls = []
        if os.path.exists(ghlog):
            with open(ghlog) as fh:
                calls = [ln.rstrip("\n") for ln in fh if ln.strip()]
        flog = ""
        if os.path.exists(fetchlog):
            with open(fetchlog) as fh:
                flog = fh.read()
        return p.returncode, p.stdout, p.stderr, calls, flog


def no_rebase(calls):
    return not any("--rebase" in c for c in calls)


def updates_for(calls):
    return [c for c in calls if "update-branch" in c]


def fetch_block(fetchlog, ref):
    """Return the (ARGS, TERMINAL, SSH) 3-line block of the fetch whose ARGS names <ref>, or None."""
    lines = fetchlog.splitlines()
    for i in range(0, len(lines) - 2, 3):
        if lines[i].startswith("ARGS=") and f" {ref} " in (lines[i] + " "):
            return "\n".join(lines[i:i + 3])
    return None


def effective_batchmode(block):
    """The LAST BatchMode value ssh honors in a fetch block's GIT_SSH_COMMAND (later -o wins)."""
    lines = [ln for ln in block.splitlines() if ln.startswith("GIT_SSH_COMMAND=")]
    if not lines:
        return None
    vals = re.findall(r"BatchMode=(\w+)", lines[-1])
    return vals[-1] if vals else None


def main():
    print("== excludes the just-merged PR ==")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 102", behind="3",
        views={101: view(head="feat/merged"), 102: view(head="feat/other")})
    check("exit 0", rc == 0)
    check("never reads the merged PR (#101)", not any("view 101" in c for c in calls))
    check("reads the other open PR (#102)", any("view 102" in c for c in calls))
    check("never update-branches the merged PR", not any("update-branch 101" in c for c in calls))
    check("no --rebase anywhere", no_rebase(calls))

    print("== behind + NOT reviewed -> update-branch in DEFAULT (merge-commit) mode ==")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 102", behind="4",
        views={102: view(reviews="0", comments="0")})
    check("exit 0", rc == 0)
    check("update-branch invoked for #102", any("update-branch 102" in c for c in calls))
    check("update-branch carries NO --rebase (default merge-commit mode)", no_rebase(calls))
    check("reports the refresh", "102" in out and "refresh" in out.lower())

    print("== behind + REVIEWED (reviews[] non-empty) -> SURFACE only, NO mutation ==")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 102", behind="4",
        views={102: view(reviews="2", comments="0")})
    check("exit 0", rc == 0)
    check("NO update-branch call", updates_for(calls) == [])
    check("surfaced for the lead", "102" in out)
    check("no --rebase anywhere", no_rebase(calls))

    print("== behind + REVIEWED (review threads/comments > 0, no submitted decision) -> SURFACE ==")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 103", behind="4",
        views={103: view(reviews="0", comments="5")})
    check("exit 0", rc == 0)
    check("NO update-branch call (comment-only review still counts as reviewed)",
          updates_for(calls) == [])
    check("surfaced for the lead", "103" in out)

    print("== predicate INDETERMINATE (malformed field) -> treated as REVIEWED -> NO mutation ==")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 104", behind="4",
        views={104: view(reviews="?", comments="?")})
    check("exit 0", rc == 0)
    check("NO update-branch call (fail toward surface)", updates_for(calls) == [])
    check("surfaced as indeterminate", "104" in out)

    print("== predicate HALF-indeterminate -> still REVIEWED -> NO mutation ==")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 105", behind="4",
        views={105: view(reviews="0", comments="?")})
    check("exit 0", rc == 0)
    check("NO update-branch call", updates_for(calls) == [])

    print("== per-PR read FAILS -> treated as reviewed -> NO mutation, exit 0 ==")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 106", behind="4",
        views={106: view()}, view_rcs={106: 1})
    check("exit 0 (fail-open)", rc == 0)
    check("NO update-branch call on an unreadable PR", updates_for(calls) == [])
    check("surfaced", "106" in out)

    print("== update-branch DENIED / errors -> degrade to REPORT-ONLY, exit 0 ==")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 107", behind="4",
        views={107: view(reviews="0", comments="0")}, update_rc=1)
    check("exit 0 despite the failed update-branch", rc == 0)
    check("update-branch was attempted", any("update-branch 107" in c for c in calls))
    check("the behind PR is still REPORTED", "107" in out)
    check("no --rebase retry", no_rebase(calls))

    print("== open-PR LIST read failure -> exit 0 (fail-open, never blocks cleanup) ==")
    rc, out, err, calls, flog = run(["101", "owner/name"], list_rc=1)
    check("exit 0 (fail-open)", rc == 0)
    check("no update-branch attempted", updates_for(calls) == [])

    print("== none behind -> clean no-op ==")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 108", behind="0",
        views={108: view(reviews="0", comments="0")})
    check("exit 0", rc == 0)
    check("no update-branch on a fresh PR", updates_for(calls) == [])
    check("reports nothing to do", "no open PR" in out or "nothing" in out.lower())

    print("== no OTHER open PRs -> clean no-op ==")
    rc, out, err, calls, flog = run(["101", "owner/name"], open_prs="101")
    check("exit 0", rc == 0)
    check("no update-branch", updates_for(calls) == [])

    print("== CROSS-REPOSITORY (fork) PR -> never measured, never mutated, SURFACED ==")
    # A fork PR's headRefName names a branch in the FORK. If origin happens to carry a
    # same-named branch (dev / patch-1 / fix), measuring against origin/<head> compares an
    # UNRELATED ref - and a >0 count on an unreviewed PR would then MUTATE it.
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 109", behind="7",
        views={109: view(head="patch-1", reviews="0", comments="0", cross="true")})
    check("exit 0", rc == 0)
    check("NO update-branch call on a fork PR", updates_for(calls) == [])
    check("the fork PR is SURFACED", "109" in out)
    check("says cross-repository", "cross-repo" in out.lower())

    print("== isCrossRepository UNREADABLE -> treated as cross-repo -> skipped + surfaced ==")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 110", behind="7",
        views={110: view(reviews="0", comments="0", cross="?")})
    check("exit 0", rc == 0)
    check("NO update-branch call (fail toward not-acting)", updates_for(calls) == [])
    check("surfaced", "110" in out)

    print("== HEAD fetch FAILS -> UNKNOWN (never measured against a stale local ref) ==")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 111", behind="9",
        views={111: view(head="feat/x", reviews="0", comments="0")},
        fetch_fail_ref="feat/x")
    check("exit 0", rc == 0)
    check("NO update-branch call on an unfetchable head", updates_for(calls) == [])
    check("surfaced as undetermined", "111" in out)

    print("== merged-PR exclusion is NUMERIC (a zero-padded argument still matches) ==")
    rc, out, err, calls, flog = run(
        ["007", "owner/name"], open_prs="7 102", behind="4",
        views={7: view(head="feat/merged"), 102: view(head="feat/other")})
    check("exit 0", rc == 0)
    check("the merged PR (#7, passed as 007) is NOT read", not any("view 7 " in c for c in calls))
    check("the merged PR is NOT update-branched", not any("update-branch 7 " in c for c in calls))
    check("the other PR is still swept", any("update-branch 102" in c for c in calls))

    print("== gh pr list CAP hit -> the truncation is stated, never a clean 'nothing to do' ==")
    many = " ".join(str(i) for i in range(1, 101))   # exactly the --limit 100 cap
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs=many, behind="0",
        views={i: view(reviews="0", comments="0") for i in range(1, 101)})
    check("exit 0", rc == 0)
    check("the cap is surfaced", "100" in out and ("truncat" in out.lower() or "cap" in out.lower()))
    check("no unqualified 'nothing to do' claim", "nothing to do" not in out)

    print("== NON-INTERACTIVE: the sweep's OWN head fetch is GIT_TERMINAL_PROMPT=0 + BatchMode=yes ==")
    # A caller BatchMode=no must NOT survive as the effective setting on the head fetch.
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 120", behind="4",
        views={120: view(head="feat/hf", reviews="0", comments="0")},
        ssh_command="ssh -o BatchMode=no")
    head_blk = fetch_block(flog, "feat/hf")
    check("the head fetch was recorded", head_blk is not None)
    check("GIT_TERMINAL_PROMPT=0 on the head fetch", "GIT_TERMINAL_PROMPT=0" in (head_blk or ""))
    check("caller BatchMode=no preserved on the head fetch", "BatchMode=no" in (head_blk or ""))
    check("but the EFFECTIVE head-fetch BatchMode is yes (our -o appended LAST wins)",
          effective_batchmode(head_blk or "") == "yes")

    print("== TOCTOU: not-reviewed at snapshot but REVIEWED at re-check -> NOT refreshed ==")
    # The snapshot shows 0 reviews / 0 comments (would refresh), but a review lands before the
    # mutation: the pre-update re-read shows reviews>0, so the sweep must SURFACE, not update-branch.
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 121", behind="5",
        views={121: view(reviews="0", comments="0")},
        views_recheck={121: view_recheck(reviews="3", comments="0")})
    check("exit 0", rc == 0)
    check("NO update-branch (became reviewed between snapshot and mutation)", updates_for(calls) == [])
    check("surfaced for the lead", "121" in out)
    check("no --rebase anywhere", no_rebase(calls))

    print("== TOCTOU: re-check UNREADABLE -> treated as reviewed -> NOT refreshed ==")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 122", behind="5",
        views={122: view(reviews="0", comments="0")},
        views_recheck={122: view_recheck(reviews="?", comments="?")})
    check("exit 0", rc == 0)
    check("NO update-branch on an indeterminate re-check", updates_for(calls) == [])
    check("surfaced", "122" in out)

    print("== HARDENED GATE: helper exits NON-1 with a behind label -> NOT refreshed ==")
    stub_badrc = (
        "#!/usr/bin/env bash\n"
        'echo "freshness: behind - origin/head is 5 commit(s) behind origin/main"\n'
        "exit 3\n")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 123", behind="5",
        views={123: view(reviews="0", comments="0")}, freshness_stub=stub_badrc)
    check("exit 0", rc == 0)
    check("a non-1 helper exit NEVER reaches update-branch", updates_for(calls) == [])
    check("routed to surface (undetermined)", "123" in out)

    print("== HARDENED GATE: helper exits 1 but count is NON-NUMERIC -> NOT refreshed ==")
    stub_nonnum = (
        "#!/usr/bin/env bash\n"
        'echo "freshness: behind - origin/head is many commits behind origin/main"\n'
        "exit 1\n")
    rc, out, err, calls, flog = run(
        ["101", "owner/name"], open_prs="101 124", behind="5",
        views={124: view(reviews="0", comments="0")}, freshness_stub=stub_nonnum)
    check("exit 0", rc == 0)
    check("an unparseable count NEVER reaches update-branch (no '?' fallback)", updates_for(calls) == [])
    check("routed to surface (undetermined)", "124" in out)

    print("== malformed invocation -> exit 2 (the ONLY non-zero exit) ==")
    rc, out, err, calls, flog = run([])
    check("no PR number -> exit 2", rc == 2)
    rc, out, err, calls, flog = run(["abc"])
    check("non-numeric PR -> exit 2", rc == 2)
    rc, out, err, calls, flog = run(["--bogus", "101"])
    check("unknown flag -> exit 2", rc == 2)
    rc, out, err, calls, flog = run(["101", "owner/name", "extra"])
    check("extra positional -> exit 2", rc == 2)

    print("== --help -> exit 0, prints the header ==")
    rc, out, err, calls, flog = run(["--help"])
    check("--help -> exit 0", rc == 0)
    check("--help prints usage", "open-pr-staleness-sweep.sh" in out)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
