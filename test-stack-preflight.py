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
assertion (receipt tree bind, ancestry, gh-failure fail-closed) must turn red when the matching
line in the copy is broken, or the assertion is decorative.

Run: python3 test-stack-preflight.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(REPO, "scripts", "stack-preflight.sh")
FRESH = os.path.join(REPO, "scripts", "base-freshness.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


GH_STUB = r"""#!/usr/bin/env bash
# gh pr view <n> --json ... -> $GH_DIR/<n>.json ; GH_FAIL=1 -> exit 1
[ "${GH_FAIL:-0}" = "1" ] && { echo "gh: HTTP 502" >&2; exit 1; }
if [ "$1" = "pr" ] && [ "$2" = "view" ] && [ -f "$GH_DIR/$3.json" ]; then cat "$GH_DIR/$3.json"; exit 0; fi
echo "gh stub: unexpected $*" >&2; exit 1
"""


def git(cwd, *args):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
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
        subprocess.run(["git", "init", "-q", "--bare", "-b", "trunk", self.origin], check=True)
        subprocess.run(["git", "clone", "-q", self.origin, self.clone], check=True, capture_output=True)
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

    def run(self, *args, script=SCRIPT, cwd=None, gh_fail=False):
        env = dict(os.environ, PATH=self.bindir + os.pathsep + os.environ["PATH"],
                   GH_DIR=self.ghdir, GH_FAIL="1" if gh_fail else "0")
        r = subprocess.run(["bash", script, *args], cwd=cwd or self.clone, env=env,
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

    print("read-only: no slice is modified")
    def readonly(fx):
        before = [git(fx.clone, "rev-parse", r) for r in ("s1", "s2", "origin/trunk")]
        fx.run(fx.wt1, fx.wt2)
        after = [git(fx.clone, "rev-parse", r) for r in ("s1", "s2", "origin/trunk")]
        remote = git(fx.clone, "ls-remote", "origin")
        return before == after and "refs/heads/s1" not in remote
    check("refs unchanged and nothing pushed", scenario(readonly))

    print("mutation proofs (temp copy; the repo script is never edited)")
    mutations = [
        ("receipt tree bind removed", '"ok $want_tree")', 'ok\\ *)', case_receipt_stale, 1),
        ("ancestry check inverted", '--is-ancestor "$prev" "${tip[k]}" 2>/dev/null; arc=$?',
         '--is-ancestor "$prev" "${tip[k]}" 2>/dev/null; arc=0', case_ancestry_broken, 1),
        ("gh failure falls through", 'failed (read failure is never a pass)"; undet; continue',
         'failed (read failure is never a pass)"; continue', case_gh_fail, 2),
    ]
    with tempfile.TemporaryDirectory() as md:
        for name, old, new, case, expected_rc in mutations:
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
            rc, _ = scenario(lambda fx: case(fx, script=mut))
            check(f"{name}: mutant no longer exits {expected_rc} (got {rc})", rc != expected_rc)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("all stack-preflight checks passed")


if __name__ == "__main__":
    main()
