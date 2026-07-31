#!/usr/bin/env python3
"""Proof harness for safe-push.sh's base-freshness gate (issue #330).

WHAT IS UNDER TEST. safe-push classified an upload only against the branch's OWN remote
tracking ref (additive / diverged / rewrite) and never asked whether HEAD was behind the
BASE. So the first additive upload of a stale-base branch passed clean and opened a PR on
a stale base. `scripts/base-freshness.sh` (#282) already answers that question; this wires
it in. The capability existed and was unwired, which is indistinguishable from absent.

THE CONTRACT, and why each clause is shaped the way it is:

  GIT-ONLY, DELIBERATELY. safe-push calls no `gh` and this gate does not change that. The
  base is resolved from `origin/HEAD` (a local symbolic ref) or from an explicit --base.
  A `gh` lookup would put a NETWORK dependency on the single most-used path in the repo,
  where a rate-limited or unauthenticated `gh` would delay or break every push. Reviewed
  state is therefore NOT consulted here; the caller DECLARES intent with --stale-ok
  instead (the same shape as the existing --rewrite intent flag).

  BLOCK ON A DEFINITIVE BEHIND ONLY. base-freshness.sh exits 1 only when it has actually
  resolved a behind-count; unknown (unreachable origin, shallow clone, unresolvable ref)
  is exit 0 BY DESIGN so best-effort degradation never blocks a caller. This gate must
  preserve that: a degraded answer is REPORTED, never enforced.

  --base IS THE REMEDY FOR A NON-DEFAULT BASE, NOT --stale-ok. A backport branch off
  release/1.2 measured against origin/HEAD would produce a FALSE behind-count. If the
  only way out were the override, every backport author would learn to reach for
  --stale-ok, which trains the override to mean "dismiss the guard" rather than "the gate
  genuinely passed" - the exact corrosion #345 documents. So a wrong base has a CORRECT
  fix that is not an override.

  THE ORDER MATTERS. Freshness is checked BEFORE the network push but AFTER the cheap
  local validations, so a malformed invocation still fails fast with its own error rather
  than a freshness complaint.

This harness stubs `git` (and asserts NO `gh` is ever invoked) via temp 0755 scripts first
on PATH, so it is host-independent and never touches a real remote.

Run: python3 test-safe-push-freshness.py
"""
import ast
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(REPO, "scripts", "safe-push.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"
    print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


# A `git` stub driven entirely by env vars, so each branch is reachable directly.
#   ORIGIN_HEAD  -- what `symbolic-ref refs/remotes/origin/HEAD` returns ('' = unset)
#   BEHIND_N     -- commits HEAD is behind the base (0 = fresh)
#   FETCH_RC     -- exit code of `git fetch` (non-zero drives the unknown path)
#   PUSHLOG      -- every `git push` invocation is recorded here
GIT_STUB = r"""#!/usr/bin/env bash
set -u
case "$1 ${2:-}" in
  "rev-parse --git-dir") echo "$GITDIR"; exit 0 ;;
  "rev-parse --is-shallow-repository") echo "false"; exit 0 ;;
esac
if [ "$1" = "symbolic-ref" ]; then
  # --short HEAD (current branch) vs the origin/HEAD default-branch probe.
  for a in "$@"; do
    if [ "$a" = "refs/remotes/origin/HEAD" ]; then
      [ -n "${ORIGIN_HEAD:-}" ] || exit 1
      echo "origin/${ORIGIN_HEAD}"; exit 0
    fi
  done
  echo "feature-branch"; exit 0
fi
if [ "$1" = "rev-parse" ]; then echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"; exit 0; fi
if [ "$1" = "fetch" ]; then exit "${FETCH_RC:-0}"; fi
if [ "$1" = "ls-remote" ]; then
  # Empty BEFORE the push => first-push (additive), keeping the rewrite classifier out of
  # the way; the branch's SHA AFTER it, so safe-push's own post-push ref verification (the
  # entire point of the wrapper) sees the ref land. A stub that returns empty on both sides
  # fails that verification and masks the behavior under test.
  if [ -s "${PUSHLOG:-/dev/null}" ]; then
    printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\trefs/heads/feature-branch\n'
  fi
  exit 0
fi
if [ "$1" = "rev-list" ]; then echo "${BEHIND_N:-0}"; exit 0; fi
if [ "$1" = "show-ref" ] || [ "$1" = "cat-file" ] || [ "$1" = "merge-base" ]; then exit 0; fi
if [ "$1" = "push" ]; then echo "push $*" >>"${PUSHLOG:-/dev/null}"; exit 0; fi
exit 0
"""

# A `gh` stub that FAILS LOUDLY. The git-only property is asserted, not assumed: if the
# implementation ever reaches for gh, these tests break rather than silently passing.
GH_STUB = r"""#!/usr/bin/env bash
echo "gh was invoked: $*" >>"${GHLOG:-/dev/null}"
exit 1
"""


def run(args, *, origin_head="main", behind=0, fetch_rc=0):
    """Run safe-push.sh with a stubbed PATH. Returns (rc, stdout+stderr, ghlog)."""
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin")
        os.makedirs(bindir)
        for name, body in (("git", GIT_STUB), ("gh", GH_STUB)):
            p = os.path.join(bindir, name)
            with open(p, "w") as fh:
                fh.write(body)
            os.chmod(p, 0o755)
        gitdir = os.path.join(td, "gitdir")
        os.makedirs(gitdir)
        ghlog = os.path.join(td, "gh.log")
        env = dict(
            os.environ,
            PATH=bindir + os.pathsep + os.environ["PATH"],
            GITDIR=gitdir,
            ORIGIN_HEAD=origin_head,
            BEHIND_N=str(behind),
            FETCH_RC=str(fetch_rc),
            PUSHLOG=os.path.join(td, "push.log"),
            GHLOG=ghlog,
        )
        r = subprocess.run(
            ["bash", SCRIPT, *args], capture_output=True, text=True, env=env, cwd=td, timeout=30
        )
        gh_calls = ""
        if os.path.exists(ghlog):
            with open(ghlog) as fh:
                gh_calls = fh.read()
        # The RECORDED git-push argv. Asserting against the combined output instead would be
        # vacuous: safe-push's own messages contain the word "push", so a substring test over
        # stdout+stderr can inspect prose rather than the command line it claims to check.
        pushed = ""
        plog = env["PUSHLOG"]
        if os.path.exists(plog):
            with open(plog) as fh:
                pushed = fh.read()
        return r.returncode, (r.stdout + r.stderr), gh_calls, pushed


print("safe-push base-freshness gate (#330)")

print("\n== BLOCKS a definitively-behind push ==")
rc, out, _, pushed = run(["feature-branch"], behind=3)
check("behind -> non-zero exit (the push is refused)", rc != 0)
check("the message names the behind count", "3" in out)
check("the message names the override flag", "--stale-ok" in out)
check("the message points at an ADDITIVE remedy", "merge" in out.lower())
check("the message NEVER suggests --rebase (it orphans cited fix SHAs)", "--rebase" not in out)

print("\n== --stale-ok DECLARES intent and proceeds ==")
rc, out, _, pushed = run(["feature-branch", "--stale-ok"], behind=3)
check("behind + --stale-ok -> push proceeds (exit 0)", rc == 0)
check("intent is still REPORTED, not silent", "stale" in out.lower() or "behind" in out.lower())

print("\n== --stale-ok is NOT forwarded to git push ==")
check("--stale-ok never reaches the git push line", "--stale-ok" not in pushed)
check("...and the push actually happened (the assertion is not vacuous)", pushed.strip() != "")

print("\n== a FRESH branch is untouched ==")
rc, out, _, pushed = run(["feature-branch"], behind=0)
check("fresh -> exit 0", rc == 0)
check("fresh does not emit a behind complaint", "behind" not in out.lower())

print("\n== --base is the remedy for a NON-DEFAULT base (never the override) ==")
rc, out, _, pushed = run(["feature-branch", "--base", "release/1.2"], behind=0)
check("--base <name> is accepted and measured against that base", rc == 0)
check("--base never reaches the git push line", "--base" not in pushed)
check("...and the push actually happened (the assertion is not vacuous)", pushed.strip() != "")
rc, out, _, pushed = run(["feature-branch", "--base"], behind=0)
check("--base with no value -> usage error (exit 2), never a guess", rc == 2)

print("\n== UNKNOWN degrades: reported, never enforced ==")
rc, out, _, pushed = run(["feature-branch"], fetch_rc=1, behind=0)
check("unreachable origin (fetch fails) -> push still proceeds", rc == 0)
rc, out, _, pushed = run(["feature-branch"], origin_head="", behind=3)
check("origin/HEAD unset -> cannot resolve a base -> does NOT block", rc == 0)
check("...and says why rather than failing silently", "freshness" in out.lower() or "base" in out.lower())

print("\n== GIT-ONLY: the push path gains no network dependency on gh ==")
rc, out, gh_calls, pushed = run(["feature-branch"], behind=2)
check("no `gh` invocation on the behind path", gh_calls == "")
rc, out, gh_calls, pushed = run(["feature-branch"], behind=0)
check("no `gh` invocation on the fresh path", gh_calls == "")

print("\n== ORDERING: a malformed invocation still fails with ITS OWN error ==")
rc, out, _, pushed = run(["-u", "origin", "feature-branch"], behind=5)
check("leading-dash positional -> usage error (exit 2), not a freshness complaint", rc == 2)
check("the usage error is the one reported", "branch name" in out)

print("\n== DEPLOYMENT: the helper must exist at the stable path ==")
# The deployed safe-push.sh resolves base-freshness.sh via `dirname "$0"`, which IS
# ~/.claude/scripts. If the helper is not in HELPER_NAMES it is never deployed there, so
# the gate degrades to its "not found" branch and SILENTLY never fires for a plugin user -
# while every test in this repo still passes, because the repo-local copy is always found.
# That is the #216/#217 failure (a live stable-path dependency omitted from the deploy
# list); this asserts the lesson rather than re-learning it.
#
# PARSE THE LIST, DO NOT SPLIT ON THE FIRST `]`: the comments INSIDE HELPER_NAMES contain
# `[ -x ]`, so a naive split truncates the block mid-list and reports a present entry as
# missing - a wrong pattern that reads exactly like a real defect. Evaluate the literal.
setup_src = open(os.path.join(REPO, "scripts", "orchestrate-setup.py"), encoding="utf-8").read()
# It is a TUPLE, not a list, and the closing paren is at column 0. Both details were got
# wrong on the way here, each time producing a "missing entry" verdict on an entry that was
# present - which is why the parse asserts its OWN sanity below before judging anything.
_m = re.search(r"^HELPER_NAMES\s*=\s*(\(.*?^\))", setup_src, re.S | re.M)
helper_names = list(ast.literal_eval(_m.group(1))) if _m else []
check("HELPER_NAMES parses as a real list (the assertion is not vacuous)",
      len(helper_names) > 10)
# Membership on the PARSED list. Searching repr() for a DOUBLE-quoted name never matches
# (repr emits single quotes), which is a third way this one check found to fail on correct
# code - hence the parse-sanity assertion above.
check("base-freshness.sh is in HELPER_NAMES (deployed to the stable path)",
      "base-freshness.sh" in helper_names)
check("...and safe-push.sh itself is too (the dependent script)",
      "safe-push.sh" in helper_names)

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all safe-push freshness assertions passed")
