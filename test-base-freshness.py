#!/usr/bin/env python3
"""Proof harness for base-freshness.sh (issue #282): the git-only, caller-supplied-base
behind-check used by the push-side (pr-shipper fix-round) and merge-side (open-PR
staleness sweep) paths.

Contract under test:
  - The base is ALWAYS caller-supplied; the script never infers or hard-codes `main`.
  - Non-interactive by construction: GIT_TERMINAL_PROMPT=0 (+ SSH BatchMode) so an
    auth-required / unreachable origin fails FAST instead of hanging a push path.
  - Exactly one labeled `freshness:` line on every path: fresh / behind / unknown.
  - EXIT CONTRACT: 0 for fresh AND unknown (best-effort degradation never blocks);
    a distinct non-zero (1) ONLY for a definitively-resolved BEHIND; 2 for a
    malformed invocation.

This harness stubs `git` via a temp 0755 script first on PATH (host-independent; never
touches a real remote). The stub records the environment of the `fetch` invocation so the
non-interactive guarantee is asserted, not assumed, and returns configurable exit codes
for fetch / ref-resolution / rev-list so each degradation branch is driven directly.

Run: python3 test-base-freshness.py
"""
import os
import re
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "base-freshness.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


def effective_batchmode(log):
    """The LAST BatchMode value ssh would honor across the recorded GIT_SSH_COMMAND (later -o wins).
    Returns 'yes' / 'no' / None. Matches both `-oBatchMode=X` and `-o BatchMode=X` spellings."""
    lines = [ln for ln in log.splitlines() if ln.startswith("GIT_SSH_COMMAND=")]
    if not lines:
        return None
    vals = re.findall(r"BatchMode=(\w+)", lines[-1])
    return vals[-1] if vals else None


GIT_STUB = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'if [ "$1" = "rev-parse" ] && [ "$2" = "--git-dir" ]; then echo "$GITDIR"; exit 0; fi\n'
    'if [ "$1" = "rev-parse" ] && [ "$2" = "--is-shallow-repository" ]; then\n'
    '  echo "${SHALLOW:-false}"; exit 0\n'
    "fi\n"
    'if [ "$1" = "fetch" ]; then\n'
    '  # Record the non-interactive env of the FETCH invocation (the one network call).\n'
    '  { echo "GIT_TERMINAL_PROMPT=${GIT_TERMINAL_PROMPT:-<unset>}"\n'
    '    echo "GIT_SSH_COMMAND=${GIT_SSH_COMMAND:-<unset>}"\n'
    '    echo "ARGS=$*"; } >>"$FETCHLOG"\n'
    '  exit "${FETCH_RC:-0}"\n'
    "fi\n"
    'if [ "$1" = "rev-parse" ]; then\n'
    '  # rev-parse --verify --quiet <rev>: last arg is the rev.\n'
    '  for a in "$@"; do rev="$a"; done\n'
    '  case "$rev" in\n'
    '    origin/*) [ "${BASE_REF_RC:-0}" = "0" ] || exit "$BASE_REF_RC"; echo "baseSHA"; exit 0 ;;\n'
    '    *)        [ "${HEAD_REF_RC:-0}" = "0" ] || exit "$HEAD_REF_RC"; echo "headSHA"; exit 0 ;;\n'
    "  esac\n"
    "fi\n"
    'if [ "$1" = "rev-list" ]; then\n'
    '  [ "${REVLIST_RC:-0}" = "0" ] || exit "$REVLIST_RC"\n'
    '  echo "${BEHIND:-0}"; exit 0\n'
    "fi\n"
    "exit 0\n"
)


def run(args, *, behind="0", fetch_rc=0, base_ref_rc=0, head_ref_rc=0,
        revlist_rc=0, shallow="false", ssh_command=None):
    """Invoke base-freshness.sh with a stubbed git. Returns (rc, stdout, stderr, fetchlog)."""
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        gitdir = os.path.join(td, "gitdir"); os.makedirs(gitdir)
        fetchlog = os.path.join(td, "fetchlog")

        git = os.path.join(bindir, "git")
        with open(git, "w") as f:
            f.write(GIT_STUB)
        os.chmod(git, 0o755)

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["GITDIR"] = gitdir
        env["FETCHLOG"] = fetchlog
        env["BEHIND"] = behind
        env["FETCH_RC"] = str(fetch_rc)
        env["BASE_REF_RC"] = str(base_ref_rc)
        env["HEAD_REF_RC"] = str(head_ref_rc)
        env["REVLIST_RC"] = str(revlist_rc)
        env["SHALLOW"] = shallow
        # The caller's env must NOT be able to make the fetch interactive.
        env.pop("GIT_TERMINAL_PROMPT", None)
        if ssh_command is not None:
            env["GIT_SSH_COMMAND"] = ssh_command
        else:
            env.pop("GIT_SSH_COMMAND", None)

        p = subprocess.run(["bash", SCRIPT] + args, env=env,
                           capture_output=True, text=True, timeout=15)
        log = ""
        if os.path.exists(fetchlog):
            with open(fetchlog) as fh:
                log = fh.read()
        return p.returncode, p.stdout, p.stderr, log


def main():
    print("== fresh (0 behind) -> exit 0, one labeled freshness line ==")
    rc, out, err, log = run(["main"], behind="0")
    check("fresh -> exit 0", rc == 0)
    check("fresh prints a 'freshness:' line", "freshness:" in out)
    check("fresh is labeled fresh", "freshness: fresh" in out)
    check("fresh names the caller-supplied base", "origin/main" in out)

    print("== behind N -> distinct non-zero, count + ADDITIVE refresh guidance (never rebase) ==")
    rc, out, err, log = run(["main"], behind="7")
    check("behind -> distinct non-zero (1)", rc == 1)
    check("behind is labeled behind", "freshness: behind" in out)
    check("behind carries the count", "7" in out)
    check("behind guidance is ADDITIVE (git merge + update-branch)",
          "git merge" in out and "update-branch" in out)
    check("behind guidance NEVER prescribes a rebase (rewrites reviewed SHAs)",
          "rebase" not in out.lower())

    print("== a NON-main base is honored verbatim (never infers main) ==")
    rc, out, err, log = run(["release/1.2"], behind="3")
    check("non-main base -> behind exit 1", rc == 1)
    check("non-main base named in the message", "origin/release/1.2" in out)
    check("the word 'main' is never substituted in", "origin/main" not in out)
    check("fetch targeted the caller-supplied base", "release/1.2" in log)

    print("== unknown: unresolvable base ref -> exit 0 (never blocks) ==")
    rc, out, err, log = run(["nosuch"], base_ref_rc=1)
    check("unresolvable base -> exit 0", rc == 0)
    check("unresolvable base is labeled unknown", "freshness: unknown" in out)

    print("== unknown: unresolvable HEAD ref -> exit 0 ==")
    # NOTE: the stub routes an `origin/*` rev to BASE_REF_RC, so drive the HEAD branch with a
    # non-origin ref (the shape the pr-shipper fix-round path uses: the local branch / HEAD).
    rc, out, err, log = run(["main", "feature/gone"], head_ref_rc=1)
    check("unresolvable head -> exit 0", rc == 0)
    check("unresolvable head is labeled unknown", "freshness: unknown" in out)

    print("== unknown: fetch failure -> exit 0, never a false 'fresh' ==")
    rc, out, err, log = run(["main"], fetch_rc=128, behind="0")
    check("fetch failure -> exit 0", rc == 0)
    check("fetch failure is labeled unknown", "freshness: unknown" in out)
    check("fetch failure is NOT reported as fresh", "freshness: fresh" not in out)
    check("fetch failure is NOT reported as behind", "freshness: behind" not in out)

    print("== unknown: shallow clone -> exit 0 ==")
    rc, out, err, log = run(["main"], shallow="true")
    check("shallow -> exit 0", rc == 0)
    check("shallow is labeled unknown", "freshness: unknown" in out)
    check("shallow does not even attempt the count", "freshness: behind" not in out)

    print("== unknown: rev-list failure -> exit 0 ==")
    rc, out, err, log = run(["main"], revlist_rc=1)
    check("rev-list failure -> exit 0", rc == 0)
    check("rev-list failure is labeled unknown", "freshness: unknown" in out)

    print("== NON-INTERACTIVE: GIT_TERMINAL_PROMPT=0 + SSH BatchMode on the fetch ==")
    rc, out, err, log = run(["main"], behind="0")
    check("fetch was invoked", "ARGS=" in log)
    check("GIT_TERMINAL_PROMPT=0 was set on the fetch", "GIT_TERMINAL_PROMPT=0" in log)
    check("SSH BatchMode was set on the fetch", "BatchMode=yes" in log)

    print("== NON-INTERACTIVE: a caller GIT_SSH_COMMAND is PRESERVED, BatchMode appended ==")
    rc, out, err, log = run(["main"], ssh_command="ssh -i /k/id -p 2222")
    check("caller ssh command preserved", "-i /k/id -p 2222" in log)
    check("BatchMode still enforced", "BatchMode=yes" in log)

    print("== NON-INTERACTIVE: a caller BatchMode=no cannot survive (effective BatchMode=yes) ==")
    rc, out, err, log = run(["main"], ssh_command="ssh -oBatchMode=no")
    check("caller ssh BatchMode=no is preserved in the command", "BatchMode=no" in log)
    check("but the EFFECTIVE BatchMode is yes (our -o BatchMode=yes appended LAST wins)",
          effective_batchmode(log) == "yes")

    print("== malformed invocation -> exit 2 ==")
    rc, out, err, log = run([])
    check("no base arg -> exit 2", rc == 2)
    rc, out, err, log = run(["--bogus"])
    check("unknown flag -> exit 2", rc == 2)
    rc, out, err, log = run(["main", "HEAD", "extra"])
    check("extra positional -> exit 2", rc == 2)

    print("== --help -> exit 0, prints the header ==")
    rc, out, err, log = run(["--help"])
    check("--help -> exit 0", rc == 0)
    check("--help prints usage", "base-freshness.sh" in out)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
