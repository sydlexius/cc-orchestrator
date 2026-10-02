#!/usr/bin/env python3
"""Proof harness for stack-preflight.sh: the read-only pre-link checker behind /orchestrate:stack-prs.

WHAT IS UNDER TEST. `gh stack link` pushes every branch of a stack by itself, so it skips
safe-push.sh's receipt, freshness and one-branch checks. stack-preflight re-asks those questions
for EVERY slice immediately before the lead runs link: the worktree is clean, its gate receipt
passed AND binds the branch's current tree, the bottom slice is fresh against the trunk, each
upper slice contains the lower slice's tip, and a PR slice is OPEN and based on the slice below.

THE EXIT CONTRACT is the part a caller acts on: 0 all pass (WARN/INFO allowed), 1 a definitive
failure, 2 a usage error or anything undeterminable. A gh read failure or an unreadable receipt
is 2, never 0: link has no second gate behind it.

FIXTURES ARE REAL GIT. A bare origin, a clone whose trunk is deliberately NOT `main` (proves the
trunk comes from origin/HEAD, never a hard-coded name), and real worktrees per slice. `gh` is a
stub first on PATH serving canned `pr view` JSON per number, or failing on demand.

MUTATION PROOFS run against a temp COPY of the script (the repo file is never edited): each key
assertion (receipt tree bind, ancestry, gh-failure fail-closed, the trunk-slice refusal, the
live and cached default-branch refusals, the assert-free PR JSON validator, the pre-PASS head
re-read, the refs/pull/<n>/head fetch, and every fail-closed UNKNOWN path) must turn red when the matching line
in the copy is broken, or the assertion is decorative.

Run: python3 test-stack-preflight.py
     STACK_PREFLIGHT_BASH=/bin/bash python3 test-stack-preflight.py   (macOS bash 3.2)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
# The interpreter the script runs under; STACK_PREFLIGHT_BASH=/bin/bash proves macOS bash 3.2.
BASH = os.environ.get("STACK_PREFLIGHT_BASH", "bash")
SCRIPT = os.path.join(REPO, "scripts", "stack-preflight.sh")
FRESH = os.path.join(REPO, "scripts", "base-freshness.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


GH_STUB = r"""#!/usr/bin/env bash
# gh pr view <n> --json ... -> $GH_DIR/<n>.json ; GH_FAIL=1 -> exit 1
# gh pr view <n> --json headRefOid --jq .headRefOid (the pre-PASS re-read) -> $GH_DIR/<n>.head2
#   when that file exists (a head that MOVED, or garbage), else the JSON's own headRefOid.
[ "${GH_FAIL:-0}" = "1" ] && { echo "gh: HTTP 502" >&2; exit 1; }
if [ "$1" = "pr" ] && [ "$2" = "view" ] && [ -f "$GH_DIR/$3.json" ]; then
  case " $* " in
    *" --jq "*)
      if [ -f "$GH_DIR/$3.head2" ]; then cat "$GH_DIR/$3.head2"; exit 0; fi
      python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["headRefOid"])' "$GH_DIR/$3.json"; exit 0 ;;
  esac
  cat "$GH_DIR/$3.json"; exit 0
fi
echo "gh stub: unexpected $*" >&2; exit 1
"""


def git(cwd, *args):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {args} failed: {r.stderr}")
    return r.stdout.strip()


class Fixture:
    """origin.git (bare) + clone/ (trunk=trunk) + wt1 (s1 off trunk) + wt2 (s2 off s1)."""

    def __init__(self, td):
        self.td = td
        self.origin = os.path.join(td, "origin.git")
        self.clone = os.path.join(td, "clone")
        self.wt1 = os.path.join(td, "wt1")
        self.wt2 = os.path.join(td, "wt2")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "trunk", self.origin], check=True, timeout=60)
        subprocess.run(["git", "clone", "-q", self.origin, self.clone], check=True, capture_output=True,
                       timeout=60)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            git(self.clone, "config", k, v)
        self.commit(self.clone, "base.txt", "base")
        git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/trunk")
        git(self.clone, "remote", "set-head", "origin", "trunk")
        git(self.clone, "worktree", "add", "-q", "-b", "s1", self.wt1, "origin/trunk")
        self.commit(self.wt1, "one.txt", "one")
        git(self.clone, "worktree", "add", "-q", "-b", "s2", self.wt2, "s1")
        self.commit(self.wt2, "two.txt", "two")
        self.receipt(self.wt1)
        self.receipt(self.wt2)
        self.ghdir = os.path.join(td, "gh"); os.makedirs(self.ghdir)
        self.bindir = os.path.join(td, "bin"); os.makedirs(self.bindir)
        p = os.path.join(self.bindir, "gh")
        with open(p, "w") as f:
            f.write(GH_STUB)
        os.chmod(p, 0o755)

    def commit(self, wt, name, body):
        with open(os.path.join(wt, name), "w") as f:
            f.write(body + "\n")
        git(wt, "add", name)
        git(wt, "commit", "-q", "-m", name)

    def receipt(self, wt, *, result="pass", tree=None, raw=None):
        gd = git(wt, "rev-parse", "--absolute-git-dir")
        path = os.path.join(gd, "prep-pr-receipt.json")
        if raw is not None:
            body = raw
        else:
            br = git(wt, "branch", "--show-current")
            t = tree or git(wt, "rev-parse", f"refs/heads/{br}^{{tree}}")
            body = json.dumps({"schema": "gate-receipt/v1", "commit_sha": "a" * 40, "tree_sha": t,
                               "worktree": wt, "result": result, "steps": [], "producer": "gate-runner"})
        with open(path, "w") as f:
            f.write(body)
        return path

    def pr(self, n, head, base, *, state="OPEN", draft=True):
        oid = git(self.clone, "rev-parse", head)
        with open(os.path.join(self.ghdir, f"{n}.json"), "w") as f:
            json.dump({"headRefName": head, "baseRefName": base, "headRefOid": oid,
                       "isDraft": draft, "state": state}, f)

    def run(self, *args, script=SCRIPT, cwd=None, gh_fail=False, env_extra=None):
        env = dict(os.environ, PATH=self.bindir + os.pathsep + os.environ["PATH"],
                   GH_DIR=self.ghdir, GH_FAIL="1" if gh_fail else "0", **(env_extra or {}))
        r = subprocess.run([BASH, script, *args], cwd=cwd or self.clone, env=env,
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr


def scenario(fn):
    with tempfile.TemporaryDirectory() as td:
        return fn(Fixture(td))


def line(out, slice_no, check_name):
    for ln in out.splitlines():
        if ln.startswith(f"slice {slice_no} ") and f": {check_name}: " in ln:
            return ln
    return ""


# ---------------------------------------------------------------- cases
def case_all_pass(fx, script=SCRIPT):
    return fx.run(fx.wt1, fx.wt2, script=script)


def case_receipt_stale(fx, script=SCRIPT):
    fx.commit(fx.wt2, "late.txt", "edited after the gate")   # branch tree moves, receipt does not
    return fx.run(fx.wt1, fx.wt2, script=script)


def case_ancestry_broken(fx, script=SCRIPT):
    git(fx.wt2, "reset", "-q", "--hard", "origin/trunk")      # s2 no longer contains s1
    fx.commit(fx.wt2, "two.txt", "two")
    fx.receipt(fx.wt2)
    return fx.run(fx.wt1, fx.wt2, script=script)


def case_gh_fail(fx, script=SCRIPT):
    return fx.run(fx.wt1, "41", script=script, gh_fail=True)


def case_trunk_slice(fx, script=SCRIPT):
    """The reviewer's repro: an UNPUSHED trunk commit, gated, passed as the bottom slice."""
    fx.commit(fx.clone, "local.txt", "unpushed trunk commit")
    fx.receipt(fx.clone)
    wt3 = os.path.join(fx.td, "wt3")
    git(fx.clone, "worktree", "add", "-q", "-b", "s3", wt3, "trunk")
    fx.commit(wt3, "three.txt", "three"); fx.receipt(wt3)
    return fx.run(fx.clone, wt3, script=script)


def case_default_bottom(fx, script=SCRIPT):
    """--base release with the DEFAULT branch (origin/HEAD -> trunk) as the bottom slice."""
    git(fx.clone, "push", "-q", "origin", "origin/trunk:refs/heads/release")
    fx.receipt(fx.clone)
    return fx.run("--base", "release", fx.clone, fx.wt1, script=script)


def case_default_upper(fx, script=SCRIPT):
    """--base release with the DEFAULT branch as an UPPER slice."""
    git(fx.clone, "push", "-q", "origin", "origin/trunk:refs/heads/release")
    fx.receipt(fx.clone)
    return fx.run("--base", "release", fx.wt1, fx.clone, script=script)


def case_trunk_upper(fx, script=SCRIPT):
    """A worktree slice on the trunk in an UPPER position, normal base (origin/HEAD)."""
    fx.receipt(fx.clone)
    return fx.run(fx.wt1, fx.clone, script=script)


def case_main_fallback(fx, script=SCRIPT):
    """origin/HEAD unresolvable: a slice on `main` is still refused (--base trunk given)."""
    git(fx.clone, "remote", "set-head", "origin", "--delete")
    wtm = os.path.join(fx.td, "wtm")
    git(fx.clone, "worktree", "add", "-q", "-b", "main", wtm, "s2")
    fx.receipt(wtm)
    return fx.run("--base", "trunk", fx.wt1, wtm, script=script)


def case_base_head(fx, script=SCRIPT):
    return fx.run("--base", "HEAD", fx.wt1, fx.wt2, script=script)


def case_base_refs(fx, script=SCRIPT):
    return fx.run("--base", "refs/heads/x", fx.wt1, fx.wt2, script=script)


def case_base_dash(fx, script=SCRIPT):
    return fx.run("--base", "-x", fx.wt1, fx.wt2, script=script)


def case_upload_pack(fx, script=SCRIPT):
    """A PR headRefName shaped as a git option must never reach git as one."""
    marker = os.path.join(fx.td, "PWNED")
    up = os.path.join(fx.td, "up.sh")
    with open(up, "w") as f:
        f.write(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    os.chmod(up, 0o755)
    with open(os.path.join(fx.ghdir, "42.json"), "w") as f:
        json.dump({"headRefName": f"--upload-pack={up}", "baseRefName": "s1", "headRefOid": "c" * 40,
                   "isDraft": True, "state": "OPEN"}, f)
    rc, out = fx.run(fx.wt1, "42", script=script)
    return rc, out + ("\nMARKER-EXISTS" if os.path.exists(marker) else "")


def case_unreachable_origin(fx, script=SCRIPT):
    git(fx.clone, "remote", "set-url", "origin", os.path.join(fx.td, "no-such-origin.git"))
    return fx.run(fx.wt1, fx.wt2, script=script)


def case_head_fetch_fail(fx, script=SCRIPT):
    """PR head commit not local and not fetchable: slice 2 (top) so nothing above masks it."""
    with open(os.path.join(fx.ghdir, "42.json"), "w") as f:
        json.dump({"headRefName": "s2", "baseRefName": "s1", "headRefOid": "b" * 40,
                   "isDraft": True, "state": "OPEN"}, f)
    return fx.run(fx.wt1, "42", script=script)


def case_foreign_repo(fx, script=SCRIPT):
    """A worktree of a DIFFERENT clone carrying the very same commits: only the repo check stops it."""
    other = os.path.join(fx.td, "other")
    subprocess.run(["git", "clone", "-q", fx.clone, other], check=True, capture_output=True, timeout=60)
    git(other, "checkout", "-q", "-b", "s2", "origin/s2")
    fx.receipt(other)
    return fx.run(fx.wt1, other, script=script)


def case_detached(fx, script=SCRIPT):
    git(fx.wt2, "checkout", "-q", "--detach")
    return fx.run(fx.wt1, fx.wt2, script=script)


def case_no_sibling(fx, script=SCRIPT):
    lone = os.path.join(fx.td, "lone"); os.makedirs(lone)
    copy = os.path.join(lone, "stack-preflight.sh"); shutil.copy(script, copy)
    return fx.run(fx.wt1, fx.wt2, script=copy)


def case_status_unreadable(fx, script=SCRIPT):
    """Corrupt slice 2's index (not slice 1's: slice 1 is the git context the freshness fetch runs
    in, and a corrupt index there also fails that fetch, which would mask this check)."""
    with open(os.path.join(git(fx.wt2, "rev-parse", "--absolute-git-dir"), "index"), "wb") as f:
        f.write(b"DIRC-corrupted-index")
    return fx.run(fx.wt1, fx.wt2, script=script)


def case_numeric_dir(fx, script=SCRIPT):
    """A directory named 41 in the cwd: the bare number is still the PR (command's Step 0 order)."""
    os.makedirs(os.path.join(fx.clone, "41"))
    fx.pr(41, "s1", "trunk"); fx.pr(42, "s2", "s1")
    return fx.run("41", "42", script=script)


def case_head_literal_optimized(fx, script=SCRIPT):
    """headRefOid "HEAD" under PYTHONOPTIMIZE=1: an assert-based validator is stripped and lets it
    through (cat-file resolves HEAD); explicit checks must still call the JSON unparseable."""
    with open(os.path.join(fx.ghdir, "42.json"), "w") as f:
        json.dump({"headRefName": "s2", "baseRefName": "s1", "headRefOid": "HEAD",
                   "isDraft": True, "state": "OPEN"}, f)
    return fx.run(fx.wt1, "42", script=script, env_extra={"PYTHONOPTIMIZE": "1"})


def stale_default(fx):
    """Remote default renamed: the bare origin's HEAD -> `new`, the clone's cached origin/HEAD ->
    `old`. Both branches sit on the trunk commit."""
    for b in ("old", "new"):
        git(fx.clone, "push", "-q", "origin", f"origin/trunk:refs/heads/{b}")
    git(fx.origin, "symbolic-ref", "HEAD", "refs/heads/new")
    git(fx.clone, "fetch", "-q", "origin")
    git(fx.clone, "remote", "set-head", "origin", "old")


def case_live_default_slice(fx, script=SCRIPT):
    """A slice on the LIVE default branch (`new`) while the cached origin/HEAD still says `old`."""
    stale_default(fx)
    wtn = os.path.join(fx.td, "wtn")
    git(fx.clone, "worktree", "add", "-q", "-b", "new", wtn, "s2")
    fx.commit(wtn, "n.txt", "n"); fx.receipt(wtn)
    return fx.run("--base", "trunk", fx.wt1, wtn, script=script)


def case_cached_default_slice(fx, script=SCRIPT):
    """A slice on the CACHED default (`old`) while the live default is `new`: still refused."""
    stale_default(fx)
    wto = os.path.join(fx.td, "wto")
    git(fx.clone, "worktree", "add", "-q", "-b", "old", wto, "s2")
    fx.commit(wto, "o.txt", "o"); fx.receipt(wto)
    return fx.run("--base", "trunk", fx.wt1, wto, script=script)


def case_live_default_trunk(fx, script=SCRIPT):
    """No --base: the trunk is the LIVE default (`new`), not the stale cached `old`."""
    stale_default(fx)
    return fx.run(fx.wt1, fx.wt2, script=script)


def case_head_moved(fx, script=SCRIPT):
    """The pre-PASS re-read of #42's head returns a different (valid) oid."""
    fx.pr(42, "s2", "s1")
    with open(os.path.join(fx.ghdir, "42.head2"), "w") as f:
        f.write(git(fx.clone, "rev-parse", "s1") + "\n")
    return fx.run(fx.wt1, "42", script=script)


def case_head_reread_garbage(fx, script=SCRIPT):
    fx.pr(42, "s2", "s1")
    with open(os.path.join(fx.ghdir, "42.head2"), "w") as f:
        f.write("HEAD\n")
    return fx.run(fx.wt1, "42", script=script)


def case_fork_pr(fx, script=SCRIPT):
    """A FORK PR: its head commit exists on origin ONLY as refs/pull/43/head (no refs/heads/<name>),
    and not in the local clone. Fetching refs/pull/<n>/head is the only way to get it."""
    other = os.path.join(fx.td, "forkclone")
    subprocess.run(["git", "clone", "-q", fx.origin, other], check=True, capture_output=True, timeout=60)
    for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
        git(other, "config", k, v)
    git(other, "fetch", "-q", fx.wt1, "s1")
    git(other, "checkout", "-q", "-b", "forkbr", "FETCH_HEAD")
    fx.commit(other, "fork.txt", "fork")
    oid = git(other, "rev-parse", "HEAD")
    git(other, "push", "-q", "origin", "HEAD:refs/pull/43/head")
    with open(os.path.join(fx.ghdir, "43.json"), "w") as f:
        json.dump({"headRefName": "forkbr", "baseRefName": "s1", "headRefOid": oid,
                   "isDraft": True, "state": "OPEN"}, f)
    rc, out = fx.run(fx.wt1, "43", script=script)
    return rc, out


def main():
    print("all-pass worktree stack")
    rc, out = scenario(case_all_pass)
    check("exit 0", rc == 0)
    check("final PASS line", out.rstrip().endswith("stack-preflight: PASS"))
    check("trunk resolved from origin/HEAD, not hard-coded main", "trunk=trunk" in out)
    check("slice 1 fresh PASS", "PASS" in line(out, 1, "fresh"))
    check("slice 2 ancestry PASS", "PASS" in line(out, 2, "ancestry"))
    check("both receipts PASS", "PASS" in line(out, 1, "receipt") and "PASS" in line(out, 2, "receipt"))

    print("dirty worktree")
    def dirty(fx):
        with open(os.path.join(fx.wt1, "scratch.txt"), "w") as f:
            f.write("x")
        return fx.run(fx.wt1, fx.wt2)
    rc, out = scenario(dirty)
    check("exit 1", rc == 1); check("clean FAIL on slice 1", "FAIL" in line(out, 1, "clean"))

    print("receipt: missing / result=fail / stale tree / unreadable")
    def missing(fx):
        os.unlink(os.path.join(git(fx.wt1, "rev-parse", "--absolute-git-dir"), "prep-pr-receipt.json"))
        return fx.run(fx.wt1, fx.wt2)
    rc, out = scenario(missing)
    check("missing -> exit 1", rc == 1); check("missing named", "no gate receipt" in line(out, 1, "receipt"))
    rc, out = scenario(lambda fx: (fx.receipt(fx.wt1, result="fail"), fx.run(fx.wt1, fx.wt2))[1])
    check("result=fail -> exit 1", rc == 1); check("result named", "result" in line(out, 1, "receipt"))
    rc, out = scenario(case_receipt_stale)
    check("stale tree -> exit 1", rc == 1); check("STALE named", "STALE" in line(out, 2, "receipt"))
    rc, out = scenario(lambda fx: (fx.receipt(fx.wt1, raw="{not json"), fx.run(fx.wt1, fx.wt2))[1])
    check("unreadable receipt -> exit 2 (never a pass)", rc == 2)
    check("unreadable named UNKNOWN", "UNKNOWN" in line(out, 1, "receipt"))

    print("broken ancestry")
    rc, out = scenario(case_ancestry_broken)
    check("exit 1", rc == 1); check("ancestry FAIL", "FAIL" in line(out, 2, "ancestry"))

    print("bottom slice behind the trunk")
    def behind(fx):
        fx.commit(fx.clone, "moved.txt", "trunk moved")
        git(fx.clone, "push", "-q", "origin", "HEAD:refs/heads/trunk")
        return fx.run(fx.wt1, fx.wt2)
    rc, out = scenario(behind)
    check("exit 1", rc == 1); check("fresh FAIL behind", "behind" in line(out, 1, "fresh"))

    print("explicit --base overrides origin/HEAD")
    def other_base(fx):
        git(fx.clone, "push", "-q", "origin", "origin/trunk:refs/heads/release")
        return fx.run("--base", "release", fx.wt1, fx.wt2)
    rc, out = scenario(other_base)
    check("exit 0 against release", rc == 0); check("trunk=release", "trunk=release" in out)

    print("PR slices")
    def pr_ok(fx):
        fx.pr(41, "s1", "trunk"); fx.pr(42, "s2", "s1", draft=False)
        return fx.run("41", "42")
    rc, out = scenario(pr_ok)
    check("open, chained PRs -> exit 0", rc == 0)
    check("PR slice receipt is a WARN", "WARN" in line(out, 1, "receipt"))
    check("draft reported", "(draft)" in line(out, 1, "pr"))

    def pr_unstacked(fx):
        fx.pr(41, "s1", "trunk"); fx.pr(42, "s2", "trunk")
        return fx.run("41", "42")
    rc, out = scenario(pr_unstacked)
    check("upper PR on trunk -> INFO, exit 0", rc == 0 and "INFO" in line(out, 2, "pr-base"))

    def pr_wrong_base(fx):
        git(fx.clone, "push", "-q", "origin", "origin/trunk:refs/heads/elsewhere")
        fx.pr(41, "s1", "trunk"); fx.pr(42, "s2", "elsewhere")
        return fx.run("41", "42")
    rc, out = scenario(pr_wrong_base)
    check("wrong base -> exit 1", rc == 1); check("pr-base FAIL", "FAIL" in line(out, 2, "pr-base"))

    def pr_closed(fx):
        fx.pr(41, "s1", "trunk", state="CLOSED"); fx.pr(42, "s2", "s1")
        return fx.run("41", "42")
    rc, out = scenario(pr_closed)
    check("closed PR -> exit 1", rc == 1); check("pr FAIL CLOSED", "CLOSED" in line(out, 1, "pr"))

    rc, out = scenario(case_gh_fail)
    check("gh read failure -> exit 2", rc == 2); check("gh failure UNKNOWN", "UNKNOWN" in line(out, 2, "pr"))

    def pr_garbage(fx):
        with open(os.path.join(fx.ghdir, "41.json"), "w") as f:
            f.write('{"headRefName": "s1"}')
        fx.pr(42, "s2", "s1")
        return fx.run("41", "42")
    rc, out = scenario(pr_garbage)
    check("unparseable gh JSON -> exit 2", rc == 2)

    print("mixed worktree + PR")
    def mixed(fx):
        fx.pr(42, "s2", "s1")
        return fx.run(fx.wt1, "42")
    rc, out = scenario(mixed)
    check("exit 0", rc == 0)
    check("worktree receipt PASS + PR receipt WARN",
          "PASS" in line(out, 1, "receipt") and "WARN" in line(out, 2, "receipt"))

    print("usage errors -> exit 2")
    def usage(fx):
        res = {}
        res["one slice"] = fx.run(fx.wt1)[0]
        res["bogus slice"] = fx.run(fx.wt1, "not-a-dir")[0]
        res["unknown flag"] = fx.run("--force", fx.wt1, fx.wt2)[0]
        res["--base without value"] = fx.run(fx.wt1, fx.wt2, "--base")[0]
        res["refspec base"] = fx.run("--base", "a:b", fx.wt1, fx.wt2)[0]
        res["duplicate branch"] = fx.run(fx.wt1, fx.wt1)[0]
        res["mixed digits"] = fx.run(fx.wt1, "12a")[0]
        return res
    for k, v in scenario(usage).items():
        check(f"{k} -> 2", v == 2)

    print("dash-led / HEAD / refs/* --base are refused by their OWN guards")
    rc, out = scenario(case_base_dash)
    check("dash-led base -> exit 2", rc == 2); check("dash guard message", "(leading '-')" in out)
    rc, out = scenario(case_base_head)
    check("--base HEAD -> exit 2", rc == 2); check("HEAD guard message", "HEAD and refs/* are refused" in out)
    rc, out = scenario(case_base_refs)
    check("--base refs/heads/x -> exit 2", rc == 2); check("refs guard message", "HEAD and refs/* are refused" in out)

    print("a slice that IS a protected branch is refused (link would push it past the floor)")
    rc, out = scenario(case_trunk_slice)
    check("trunk slice -> exit 2", rc == 2); check("trunk named", "is the trunk branch 'trunk'" in out)
    rc, out = scenario(case_default_bottom)
    check("--base release, default branch at bottom -> exit 2", rc == 2)
    check("default named (bottom)", "slice 1 " in out and "is the default branch 'trunk'" in out)
    rc, out = scenario(case_default_upper)
    check("--base release, default branch as upper slice -> exit 2", rc == 2)
    check("default named (upper)", "slice 2 " in out and "is the default branch 'trunk'" in out)
    rc, out = scenario(case_trunk_upper)
    check("trunk worktree slice in upper position -> exit 2", rc == 2)
    check("trunk named (upper)", "slice 2 " in out and "is the trunk branch 'trunk'" in out)
    rc, out = scenario(case_main_fallback)
    check("origin/HEAD unresolvable, slice on main -> exit 2", rc == 2)
    check("main named as protected", "is a protected branch name 'main'" in out)
    def pr_trunk(fx):
        fx.pr(41, "trunk", "trunk"); fx.pr(42, "s1", "trunk")
        return fx.run("41", "42")
    rc, out = scenario(pr_trunk)
    check("PR slice whose head is the trunk -> exit 2", rc == 2)

    print("default branch: LIVE remote HEAD and the stale cached origin/HEAD are both refused")
    rc, out = scenario(case_live_default_slice)
    check("slice on the live default (cache stale) -> exit 2", rc == 2)
    check("live default named", "is the default branch 'new'" in out)
    rc, out = scenario(case_cached_default_slice)
    check("slice on the cached default (live moved) -> exit 2", rc == 2)
    check("cached default named", "is the default branch 'old'" in out)
    rc, out = scenario(case_live_default_trunk)
    check("no --base: trunk is the LIVE default, not the stale cache", rc == 0 and "trunk=new" in out)

    print("PR JSON validation survives python -O (no asserts)")
    rc, out = scenario(case_head_literal_optimized)
    check("headRefOid HEAD under PYTHONOPTIMIZE=1 -> exit 2", rc == 2)
    check("named unparseable", "unparseable" in line(out, 2, "pr"))

    print("PR heads are re-read just before PASS")
    rc, out = scenario(case_head_moved)
    check("head moved during the checks -> exit 2", rc == 2)
    check("moved named", "head moved" in line(out, 2, "head"))
    rc, out = scenario(case_head_reread_garbage)
    check("malformed re-read -> exit 2", rc == 2)
    check("malformed re-read named", "failed or was malformed" in line(out, 2, "head"))
    rc, out = scenario(pr_ok)
    check("unchanged heads -> head PASS on every PR slice",
          "PASS" in line(out, 1, "head") and "PASS" in line(out, 2, "head"))

    print("a fork PR's head is fetched via refs/pull/<n>/head")
    rc, out = scenario(case_fork_pr)
    check("fork PR (head only at refs/pull/43/head) -> exit 0", rc == 0)
    check("fork PR ancestry PASS", "PASS" in line(out, 2, "ancestry"))

    print("an option-shaped PR headRefName never reaches git as an option")
    rc, out = scenario(case_upload_pack)
    check("upload-pack headRefName -> exit 2", rc == 2)
    check("upload-pack command NOT executed", "MARKER-EXISTS" not in out)

    print("fail-closed paths")
    rc, out = scenario(case_unreachable_origin)
    check("unreachable origin -> exit 2", rc == 2); check("fresh UNKNOWN", "UNKNOWN" in line(out, 1, "fresh"))
    rc, out = scenario(case_head_fetch_fail)
    check("head fetch failure -> exit 2", rc == 2); check("head UNKNOWN", "not available locally" in line(out, 2, "pr"))
    rc, out = scenario(case_foreign_repo)
    check("worktree of another repo -> exit 2", rc == 2); check("different repository named", "different repository" in out)
    rc, out = scenario(case_detached)
    check("detached HEAD -> exit 1", rc == 1); check("detached named", "detached HEAD" in line(out, 2, "branch"))
    rc, out = scenario(case_no_sibling)
    check("no sibling base-freshness.sh -> exit 2", rc == 2); check("missing sibling named", "not found beside" in line(out, 1, "fresh"))
    rc, out = scenario(case_status_unreadable)
    check("unreadable git status -> exit 2", rc == 2); check("clean UNKNOWN", "UNKNOWN" in line(out, 2, "clean"))

    print("slice classification: all digits is a PR first")
    rc, out = scenario(case_numeric_dir)
    check("bare 41 with a dir named 41 -> PR slice, exit 0", rc == 0 and "(#41)" in out)
    def dot_numeric(fx):
        wt = os.path.join(fx.clone, "..", "77")
        git(fx.clone, "worktree", "add", "-q", "-b", "s7", wt, "s2")
        fx.commit(wt, "seven.txt", "seven"); fx.receipt(wt)
        return fx.run(fx.wt1, fx.wt2, "../77")
    rc, out = scenario(dot_numeric)
    check("../77 (a path, not a number) -> worktree slice, exit 0", rc == 0 and "77) [s7]" in out)

    print("an unenterable directory is a usage error")
    def unenterable(fx):
        d = os.path.join(fx.td, "locked"); os.makedirs(d); os.chmod(d, 0o600)
        try:
            return fx.run(fx.wt1, d)
        finally:
            os.chmod(d, 0o700)
    rc, out = scenario(unenterable)
    check("unenterable dir -> exit 2", rc == 2); check("clear message", "cannot be entered" in out)

    print("read-only: no slice is modified")
    def readonly(fx):
        before = [git(fx.clone, "rev-parse", r) for r in ("s1", "s2", "origin/trunk")]
        fx.run(fx.wt1, fx.wt2)
        after = [git(fx.clone, "rev-parse", r) for r in ("s1", "s2", "origin/trunk")]
        remote = git(fx.clone, "ls-remote", "origin")
        return before == after and "refs/heads/s1" not in remote
    check("refs unchanged and nothing pushed", scenario(readonly))

    print("mutation proofs (temp copy; the repo script is never edited)")
    def rc_is(n):
        return lambda rc, out: rc == n

    def stops_before_reread(rc, out):
        # The FIRST read must fail closed by itself; the pre-PASS head re-read is a second net that
        # would also exit 2, so a bare rc check cannot tell the two apart. No `head` line = the
        # re-read never ran because the slice was already UNKNOWN.
        return rc == 2 and ": head: " not in out
    mutations = [
        ("receipt tree bind removed", '"ok $want_tree")', 'ok\\ *)', case_receipt_stale, rc_is(1)),
        ("ancestry check inverted", '--is-ancestor "$prev" "${tip[k]}" 2>/dev/null; arc=$?',
         '--is-ancestor "$prev" "${tip[k]}" 2>/dev/null; arc=0', case_ancestry_broken, rc_is(1)),
        ("gh failure falls through", 'failed (read failure is never a pass)"; undet; continue',
         'failed (read failure is never a pass)"; continue', case_gh_fail, stops_before_reread),
        ("trunk-slice refusal removed", "(link would push it past the floor)\" >&2; exit 2",
         "(link would push it past the floor)\" >&2", case_trunk_slice, rc_is(2)),
        ("protected-slice check narrowed to $trunk only (bottom)",
         'elif [ -n "$live_default" ] && [ "$b" = "$live_default" ]; then why="the default branch"\n'
         '  elif [ -n "$default_branch" ] && [ "$b" = "$default_branch" ]; then',
         'elif false; then why=x\n  elif false; then', case_default_bottom, rc_is(2)),
        ("protected-slice check narrowed to $trunk only (upper)",
         'elif [ -n "$live_default" ] && [ "$b" = "$live_default" ]; then why="the default branch"\n'
         '  elif [ -n "$default_branch" ] && [ "$b" = "$default_branch" ]; then',
         'elif false; then why=x\n  elif false; then', case_default_upper, rc_is(2)),
        ("live default-branch refusal removed",
         'elif [ -n "$live_default" ] && [ "$b" = "$live_default" ]; then', 'elif false; then',
         case_live_default_slice, rc_is(2)),
        ("cached default-branch refusal removed",
         'elif [ -n "$default_branch" ] && [ "$b" = "$default_branch" ]; then', 'elif false; then',
         case_cached_default_slice, rc_is(2)),
        ("trunk from the stale cache, not the live default",
         'trunk="${live_default:-$default_branch}"', 'trunk="$default_branch"',
         case_live_default_trunk, lambda rc, out: rc == 0 and "trunk=new" in out),
        ("headRefOid SHA check removed (python -O)",
         'if not re.fullmatch("[0-9a-f]{40}", o) or not isinstance(dr, bool): sys.exit(1)',
         'if not isinstance(dr, bool): sys.exit(1)', case_head_literal_optimized,
         lambda rc, out: rc == 2 and "unparseable" in line(out, 2, "pr")),
        ("validator back on assert (stripped under -O)",
         'if not re.fullmatch("[0-9a-f]{40}", o) or not isinstance(dr, bool): sys.exit(1)',
         'assert re.fullmatch("[0-9a-f]{40}", o) and isinstance(dr, bool)', case_head_literal_optimized,
         lambda rc, out: rc == 2 and "unparseable" in line(out, 2, "pr")),
        ("moved head passes", 'during the checks (re-run)"; undet', 'during the checks (re-run)"',
         case_head_moved, rc_is(2)),
        ("re-read failure passes", 'or was malformed (read failure is never a pass)"; undet',
         'or was malformed (read failure is never a pass)"', case_head_reread_garbage, rc_is(2)),
        ("PR head fetched by branch name, not refs/pull (fork PR)",
         'fetch --quiet origin "refs/pull/${arg[k]}/head"', 'fetch --quiet origin "refs/heads/${branch[k]}"',
         case_fork_pr, rc_is(0)),
        ("fixed-name refusal removed", "main|master|HEAD) why=", "__none__) why=",
         case_main_fallback, rc_is(2)),
        ("protected-slice check on slice 1 only (trunk upper)",
         '  b="${branch[k]}"\n  [ -n "$b" ] || continue',
         '  [ "$k" -eq 1 ] || continue\n  b="${branch[k]}"\n  [ -n "$b" ] || continue',
         case_trunk_upper, rc_is(2)),
        ("protected-slice check on slice 1 only (default upper)",
         '  b="${branch[k]}"\n  [ -n "$b" ] || continue',
         '  [ "$k" -eq 1 ] || continue\n  b="${branch[k]}"\n  [ -n "$b" ] || continue',
         case_default_upper, rc_is(2)),
        ("dash-led base guard removed",
         "case \"$trunk\" in -*) echo \"stack-preflight: invalid --base '$trunk' (leading '-')\" >&2; usage ;; esac\n",
         "", case_base_dash, lambda rc, out: "(leading '-')" in out),
        ("HEAD/refs base guard removed", "in HEAD|refs/*) echo", "in __none__) echo",
         case_base_head, lambda rc, out: "HEAD and refs/* are refused" in out),
        ("fetch by the raw headRefName (the original line)", 'fetch --quiet origin "refs/pull/${arg[k]}/head"',
         'fetch --quiet origin "${branch[k]}"', case_upload_pack,
         lambda rc, out: "MARKER-EXISTS" not in out),
        ("unknown freshness passes", '(doubt is a STOP before link)"; undet ;;',
         '(doubt is a STOP before link)"; ;;', case_unreachable_origin, rc_is(2)),
        ("head-fetch failure passes", '(fetch failed)"; undet; tip[k]=""',
         '(fetch failed)"; tip[k]=""', case_head_fetch_fail, stops_before_reread),
        ("different-repo check removed", "than '$ctx'\" >&2; exit 2; }",
         "than '$ctx'\" >&2; }", case_foreign_repo, rc_is(2)),
        ("detached HEAD passes", 'no branch to link)"; fail; continue', 'no branch to link)"; continue',
         case_detached, rc_is(1)),
        ("missing sibling passes", '(gate did not run)"; undet; continue', '(gate did not run)"; continue',
         case_no_sibling, rc_is(2)),
        ("status read failure passes", 'cannot read git status"; undet', 'cannot read git status"',
         case_status_unreadable, rc_is(2)),
        ("directory checked before digits", "    ''|*[!0-9]*)\n      [ -d",
         "    *)\n      [ -d", case_numeric_dir, lambda rc, out: rc == 0 and "(#41)" in out),
    ]
    with tempfile.TemporaryDirectory() as md:
        for name, old, new, case, holds in mutations:
            src = open(SCRIPT).read()
            ok_anchor = src.count(old) == 1
            check(f"{name}: anchor found exactly once", ok_anchor)
            if not ok_anchor:
                continue
            mdir = os.path.join(md, name.replace(" ", "_")); os.makedirs(mdir)
            mut = os.path.join(mdir, "stack-preflight.sh")
            with open(mut, "w") as f:
                f.write(src.replace(old, new))
            shutil.copy(FRESH, os.path.join(mdir, "base-freshness.sh"))
            rc, out = scenario(lambda fx: case(fx, script=mut))
            check(f"{name}: mutant breaks the assertion (got rc={rc})", not holds(rc, out))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("all stack-preflight checks passed")


if __name__ == "__main__":
    main()
