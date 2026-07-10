#!/usr/bin/env python3
"""Proof harness for scripts/finding_channel.py (#230, the finding channel).

The finding channel is the schema-typed handoff for the adv-review <-> implementer
loop, split into a PR-BLIND fix-list slice and a LEAD-ONLY reply/thread slice
(schemas landed in #225). This helper is the DETERMINISTIC guard over that channel:
  - validate   schema + channel invariants on a slice file
  - liveness   mtime/deadline signal (fresh|slow|stalled|dead|missing)
  - guard-reply THE AC guardrail: a fix_sha must be PUSHED to origin/<branch> AND
               bound to its finding by a `Finding-Id: <id>` commit trailer
               (ancestry alone is insufficient) before its `fix` reply is posted.
  - guard-slice batch guard-reply over every `fix` disposition in a reply-slice,
               looking up each finding's fix_sha in the paired fix-list.

Isolation (mirrors test-gate-runner.py): every git case runs in its own
tempfile.TemporaryDirectory() with a BARE origin repo + a work clone, git config
forced local (GIT_CONFIG_GLOBAL/SYSTEM -> nonexistent), identity via -c. The
harness NEVER depends on host git config or network.

Exit contract asserted: 0 = ok/pass, 1 = a check failed, 2 = usage / IO error.

Run: python3 test-finding-channel.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "scripts", "finding_channel.py")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


def run(*args, cwd=None):
    """Invoke the real finding_channel.py. Returns (rc, stdout+stderr)."""
    proc = subprocess.run([sys.executable, HELPER, *args], cwd=cwd,
                          capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


# --- git isolation helpers --------------------------------------------------

def git_env(root):
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.path.join(root, ".gitconfig-none")
    env["GIT_CONFIG_SYSTEM"] = os.path.join(root, ".gitconfig-none-sys")
    return env


def git(root, *args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd or root, env=git_env(root),
                          capture_output=True, text=True, check=True)


def make_origin_and_work(root):
    """A bare origin + a work clone on branch `feature`. Returns work path."""
    origin = os.path.join(root, "origin.git")
    work = os.path.join(root, "work")
    subprocess.run(["git", "init", "-q", "--bare", origin], env=git_env(root), check=True)
    subprocess.run(["git", "clone", "-q", origin, work], env=git_env(root), check=True)
    git(root, "-c", "user.email=t@e", "-c", "user.name=t", "commit",
        "--allow-empty", "-q", "-m", "root", cwd=work)
    git(root, "checkout", "-q", "-b", "feature", cwd=work)
    return work


def commit(root, work, msg, *, empty=True, fname=None, content="x\n"):
    if not empty:
        with open(os.path.join(work, fname), "w", encoding="utf-8") as f:
            f.write(content)
        git(root, "add", "-A", cwd=work)
    args = ["-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", msg]
    if empty:
        args.insert(-2, "--allow-empty")
    git(root, *args, cwd=work)
    return git(root, "rev-parse", "HEAD", cwd=work).stdout.strip()


def push(root, work, branch="feature"):
    git(root, "push", "-q", "origin", branch, cwd=work)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return path


# --- fixtures ---------------------------------------------------------------

def good_fix_list(round_=1):
    return {"schema": "finding-fix-list/v1", "round": round_,
            "findings": [
                {"id": "F1", "severity": "high", "detail": "boom",
                 "status": "addressed", "fix_sha": "a" * 40},
                {"id": "F2", "severity": "low", "detail": "nit",
                 "status": "open", "fix_sha": None},
            ]}


def good_reply_slice():
    return {"schema": "finding-reply-slice/v1",
            "replies": {
                "F1": {"thread_id": "PRRT_1", "disposition": "fix",
                       "reply_text": "fixed"},
                "F2": {"thread_id": "PRRT_2", "disposition": "merge-safe",
                       "reply_text": "safe"},
            }}


# --- validate ---------------------------------------------------------------

def test_validate_good_fix_list():
    with tempfile.TemporaryDirectory() as root:
        p = write_json(os.path.join(root, "f.json"), good_fix_list())
        rc, out = run("validate", "fix-list", p)
        check("validate fix-list (good) -> exit 0", rc == 0)


def test_validate_good_reply_slice():
    with tempfile.TemporaryDirectory() as root:
        p = write_json(os.path.join(root, "r.json"), good_reply_slice())
        rc, out = run("validate", "reply-slice", p)
        check("validate reply-slice (good) -> exit 0", rc == 0)


def test_validate_schema_error_surfaced():
    with tempfile.TemporaryDirectory() as root:
        bad = good_fix_list(); bad["findings"][0]["severity"] = "bogus"
        p = write_json(os.path.join(root, "f.json"), bad)
        rc, out = run("validate", "fix-list", p)
        check("validate fix-list (bad enum) -> exit 1", rc == 1)
        check("validate: schema error mentions the field", "severity" in out)


def test_validate_round_lt_1():
    with tempfile.TemporaryDirectory() as root:
        bad = good_fix_list(round_=0)
        p = write_json(os.path.join(root, "f.json"), bad)
        rc, out = run("validate", "fix-list", p)
        check("validate fix-list (round<1) -> exit 1", rc == 1)
        check("validate: round invariant explained", "round" in out)


def test_validate_addressed_without_sha():
    with tempfile.TemporaryDirectory() as root:
        bad = good_fix_list()
        bad["findings"][0]["status"] = "addressed"; bad["findings"][0]["fix_sha"] = None
        p = write_json(os.path.join(root, "f.json"), bad)
        rc, out = run("validate", "fix-list", p)
        check("validate: addressed finding w/o fix_sha -> exit 1", rc == 1)
        check("validate: addressed/no-sha explained",
              "fix_sha" in out and "F1" in out)


def test_validate_duplicate_ids():
    with tempfile.TemporaryDirectory() as root:
        bad = good_fix_list()
        bad["findings"][1]["id"] = "F1"  # dup
        p = write_json(os.path.join(root, "f.json"), bad)
        rc, out = run("validate", "fix-list", p)
        check("validate: duplicate finding ids -> exit 1", rc == 1)
        check("validate: dup id explained", "duplicate" in out.lower())


def test_validate_fix_disposition_empty_reply_text():
    with tempfile.TemporaryDirectory() as root:
        bad = good_reply_slice()
        bad["replies"]["F1"]["reply_text"] = ""  # fix must carry reply text
        p = write_json(os.path.join(root, "r.json"), bad)
        rc, out = run("validate", "reply-slice", p)
        check("validate: fix disposition w/ empty reply_text -> exit 1", rc == 1)


def test_validate_fix_disposition_whitespace_reply_text():
    # Codoki suggestion: a 'fix' reply whose reply_text is whitespace-only must
    # fail the invariant (exercises the .strip() path, not just empty-string).
    with tempfile.TemporaryDirectory() as root:
        bad = good_reply_slice()
        bad["replies"]["F1"]["reply_text"] = "   \t\n "
        p = write_json(os.path.join(root, "r.json"), bad)
        rc, out = run("validate", "reply-slice", p)
        check("validate: fix disposition w/ whitespace-only reply_text -> exit 1",
              rc == 1)


def test_liveness_zero_deadline_is_usage_exit2():
    with tempfile.TemporaryDirectory() as root:
        p = write_json(os.path.join(root, "f.json"), {})
        rc, out = run("liveness", p, "--deadline-secs", "0")
        check("liveness: --deadline-secs 0 -> exit 2 (usage)", rc == 2)


def test_liveness_nonint_deadline_is_usage_exit2():
    with tempfile.TemporaryDirectory() as root:
        p = write_json(os.path.join(root, "f.json"), {})
        rc, out = run("liveness", p, "--deadline-secs", "abc")
        check("liveness: non-integer --deadline-secs -> exit 2 (usage)", rc == 2)


def test_git_calls_are_non_interactive():
    # Codoki hardening: git subprocesses must be non-interactive so ls-remote/fetch
    # cannot hang on a credential prompt. Shim `git` on PATH to record its env; the
    # first git call (rev-parse in guard-reply) must carry GIT_TERMINAL_PROMPT=0.
    with tempfile.TemporaryDirectory() as root:
        bindir = os.path.join(root, "bin")
        os.makedirs(bindir)
        envlog = os.path.join(root, "git-env.log")
        shim = os.path.join(bindir, "git")
        with open(shim, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nenv > %r\nexit 1\n" % envlog)
        os.chmod(shim, 0o755)
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        subprocess.run([sys.executable, HELPER, "guard-reply", "--repo", root,
                        "--branch", "b", "--finding", "F1", "--sha", "abc",
                        "--no-fetch"], env=env, capture_output=True, text=True)
        recorded = ""
        if os.path.exists(envlog):
            with open(envlog, encoding="utf-8") as f:
                recorded = f.read()
        check("git calls set GIT_TERMINAL_PROMPT=0 (non-interactive, no hang)",
              "GIT_TERMINAL_PROMPT=0" in recorded)


def test_validate_missing_file():
    with tempfile.TemporaryDirectory() as root:
        rc, out = run("validate", "fix-list", os.path.join(root, "nope.json"))
        check("validate: missing file -> exit 2 (IO)", rc == 2)


def test_validate_unknown_kind():
    with tempfile.TemporaryDirectory() as root:
        p = write_json(os.path.join(root, "f.json"), good_fix_list())
        rc, out = run("validate", "bogus-kind", p)
        check("validate: unknown kind -> exit 2 (usage)", rc == 2)


# --- liveness ---------------------------------------------------------------

def _set_age(path, age):
    t = time.time() - age
    os.utime(path, (t, t))


def test_liveness_missing():
    with tempfile.TemporaryDirectory() as root:
        rc, out = run("liveness", os.path.join(root, "nope.json"),
                      "--deadline-secs", "100")
        check("liveness: missing file -> exit 1", rc == 1)
        check("liveness: 'missing' state printed", "missing" in out)


def test_liveness_fresh():
    with tempfile.TemporaryDirectory() as root:
        p = write_json(os.path.join(root, "f.json"), {})
        _set_age(p, 10)
        rc, out = run("liveness", p, "--deadline-secs", "100")
        check("liveness: fresh -> exit 0", rc == 0)
        check("liveness: 'fresh' state printed", "fresh" in out)


def test_liveness_slow():
    with tempfile.TemporaryDirectory() as root:
        p = write_json(os.path.join(root, "f.json"), {})
        _set_age(p, 150)  # 100 < 150 <= 200
        rc, out = run("liveness", p, "--deadline-secs", "100")
        check("liveness: slow -> exit 0", rc == 0)
        check("liveness: 'slow' state printed", "slow" in out)


def test_liveness_stalled():
    with tempfile.TemporaryDirectory() as root:
        p = write_json(os.path.join(root, "f.json"), {})
        _set_age(p, 300)  # 200 < 300 <= 400
        rc, out = run("liveness", p, "--deadline-secs", "100")
        check("liveness: stalled -> exit 0", rc == 0)
        check("liveness: 'stalled' state printed", "stalled" in out)


def test_liveness_dead():
    with tempfile.TemporaryDirectory() as root:
        p = write_json(os.path.join(root, "f.json"), {})
        _set_age(p, 500)  # > 400
        rc, out = run("liveness", p, "--deadline-secs", "100")
        check("liveness: dead -> exit 0 (signal, not gate)", rc == 0)
        check("liveness: 'dead' state printed", "dead" in out)


# --- guard-reply ------------------------------------------------------------

def test_guard_reply_pass():
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        sha = commit(root, work, "fix F1\n\nFinding-Id: F1")
        push(root, work)
        rc, out = run("guard-reply", "--repo", work, "--branch", "feature",
                      "--finding", "F1", "--sha", sha)
        check("guard-reply: pushed + trailer-bound -> exit 0", rc == 0)
        check("guard-reply: OK line names the finding + sha",
              "F1" in out and sha in out)


def test_guard_reply_short_sha_normalized():
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        sha = commit(root, work, "fix F1\n\nFinding-Id: F1")
        push(root, work)
        rc, out = run("guard-reply", "--repo", work, "--branch", "feature",
                      "--finding", "F1", "--sha", sha[:8])
        check("guard-reply: short sha normalized -> exit 0", rc == 0)
        check("guard-reply: full sha echoed", sha in out)


def test_guard_reply_unpushed_fails():
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        sha = commit(root, work, "fix F1\n\nFinding-Id: F1")
        # NOT pushed -> origin/feature does not contain it.
        rc, out = run("guard-reply", "--repo", work, "--branch", "feature",
                      "--finding", "F1", "--sha", sha)
        check("guard-reply: unpushed sha -> exit 1", rc == 1)
        check("guard-reply: unpushed reason (push first)",
              "push" in out.lower() or "origin" in out.lower())


def test_guard_reply_no_trailer_fails():
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        sha = commit(root, work, "fix something (no trailer)")
        push(root, work)
        rc, out = run("guard-reply", "--repo", work, "--branch", "feature",
                      "--finding", "F1", "--sha", sha)
        check("guard-reply: pushed but no Finding-Id trailer -> exit 1", rc == 1)
        check("guard-reply: binding reason mentions trailer/finding",
              "trailer" in out.lower() or "bound" in out.lower())


def test_guard_reply_wrong_finding_trailer_fails():
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        sha = commit(root, work, "fix F2\n\nFinding-Id: F2")
        push(root, work)
        # Trailer is F2 but we ask about F1 -> not bound to F1.
        rc, out = run("guard-reply", "--repo", work, "--branch", "feature",
                      "--finding", "F1", "--sha", sha)
        check("guard-reply: trailer for a DIFFERENT finding -> exit 1", rc == 1)


def test_guard_reply_bad_sha_fails():
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        commit(root, work, "root2\n\nFinding-Id: F1"); push(root, work)
        rc, out = run("guard-reply", "--repo", work, "--branch", "feature",
                      "--finding", "F1", "--sha", "deadbeef" * 5)
        check("guard-reply: nonexistent sha -> exit 1", rc == 1)


def test_guard_reply_unreachable_origin_exit2():
    # A reachable local repo whose origin points to a nonexistent path -> ls-remote
    # fails -> 'cannot prove pushed' -> exit 2 (safe-block, distinct from unpushed).
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        sha = commit(root, work, "fix F1\n\nFinding-Id: F1"); push(root, work)
        git(root, "remote", "set-url", "origin",
            os.path.join(root, "does-not-exist.git"), cwd=work)
        rc, out = run("guard-reply", "--repo", work, "--branch", "feature",
                      "--finding", "F1", "--sha", sha)
        check("guard-reply: unreachable origin -> exit 2 (cannot prove pushed)",
              rc == 2)


def test_guard_reply_absent_branch_stale_ref_fails():
    # THE hostile-review false-pass (Finding 1): the remote branch is DELETED after
    # a prior fetch left a STALE local origin/<branch>. ls-remote -> absent, but the
    # stale local ref still "contains" the sha. The guard must NOT consult the stale
    # ref -- an absent remote branch is a hard not-pushed (exit 1), never a pass.
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        sha = commit(root, work, "fix F1\n\nFinding-Id: F1")
        push(root, work)
        git(root, "fetch", "-q", "origin", "feature", cwd=work)  # stale ref = sha
        # delete the branch on the bare origin -> ls-remote now empty (absent)
        subprocess.run(["git", "--git-dir", os.path.join(root, "origin.git"),
                        "update-ref", "-d", "refs/heads/feature"],
                       env=git_env(root), check=True)
        rc, out = run("guard-reply", "--repo", work, "--branch", "feature",
                      "--finding", "F1", "--sha", sha)
        check("guard-reply: absent remote branch + stale local ref -> exit 1 "
              "(no stale false-pass)", rc == 1)


def test_guard_slice_absent_branch_stale_ref_fails():
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        sha = commit(root, work, "fix F1\n\nFinding-Id: F1")
        push(root, work)
        git(root, "fetch", "-q", "origin", "feature", cwd=work)
        subprocess.run(["git", "--git-dir", os.path.join(root, "origin.git"),
                        "update-ref", "-d", "refs/heads/feature"],
                       env=git_env(root), check=True)
        fx = write_json(os.path.join(root, "fix.json"), _fix_list_for({"F1": sha}))
        rp = write_json(os.path.join(root, "rep.json"), good_reply_slice())
        rc, out = run("guard-slice", "--repo", work, "--branch", "feature",
                      "--fix-list", fx, rp)
        check("guard-slice: absent remote branch + stale local ref -> exit 1", rc == 1)


def test_guard_slice_missing_fix_list_is_io_exit2():
    # Finding 3: a missing --fix-list path is an IO error (exit 2), NOT a failed
    # check (exit 1) -- guard-slice must not collapse cmd_validate's 2 to 1.
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        rp = write_json(os.path.join(root, "rep.json"), good_reply_slice())
        rc, out = run("guard-slice", "--repo", work, "--branch", "feature",
                      "--fix-list", os.path.join(root, "nope.json"), rp)
        check("guard-slice: missing --fix-list file -> exit 2 (IO, not 1)", rc == 2)


def test_guard_reply_no_fetch():
    # --no-fetch: relies on the already-present origin/feature ref (no network).
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        sha = commit(root, work, "fix F1\n\nFinding-Id: F1")
        push(root, work)
        git(root, "fetch", "-q", "origin", "feature", cwd=work)
        rc, out = run("guard-reply", "--repo", work, "--branch", "feature",
                      "--finding", "F1", "--sha", sha, "--no-fetch")
        check("guard-reply --no-fetch: pushed + trailer -> exit 0", rc == 0)


# --- guard-slice ------------------------------------------------------------

def _fix_list_for(shas):
    """fix-list where F1 is addressed@shas['F1'], F2 is a merge-safe (open)."""
    return {"schema": "finding-fix-list/v1", "round": 1,
            "findings": [
                {"id": "F1", "severity": "high", "detail": "d",
                 "status": "addressed", "fix_sha": shas["F1"]},
                {"id": "F2", "severity": "low", "detail": "d",
                 "status": "open", "fix_sha": None},
            ]}


def test_guard_slice_pass():
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        sha = commit(root, work, "fix F1\n\nFinding-Id: F1"); push(root, work)
        fx = write_json(os.path.join(root, "fix.json"), _fix_list_for({"F1": sha}))
        rp = write_json(os.path.join(root, "rep.json"), good_reply_slice())
        rc, out = run("guard-slice", "--repo", work, "--branch", "feature",
                      "--fix-list", fx, rp)
        check("guard-slice: fix pushed+bound, merge-safe skipped -> exit 0", rc == 0)


def test_guard_slice_unpushed_fails():
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        sha = commit(root, work, "fix F1\n\nFinding-Id: F1")  # NOT pushed
        fx = write_json(os.path.join(root, "fix.json"), _fix_list_for({"F1": sha}))
        rp = write_json(os.path.join(root, "rep.json"), good_reply_slice())
        rc, out = run("guard-slice", "--repo", work, "--branch", "feature",
                      "--fix-list", fx, rp)
        check("guard-slice: a fix reply w/ unpushed sha -> exit 1", rc == 1)
        check("guard-slice: names the failing finding", "F1" in out)


def test_guard_slice_fix_missing_from_fix_list():
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        commit(root, work, "fix F1\n\nFinding-Id: F1"); push(root, work)
        # fix-list has only F2 (open); reply-slice marks F1 as `fix` -> no sha to bind.
        fl = {"schema": "finding-fix-list/v1", "round": 1,
              "findings": [{"id": "F2", "severity": "low", "detail": "d",
                            "status": "open", "fix_sha": None}]}
        rp = {"schema": "finding-reply-slice/v1",
              "replies": {"F1": {"thread_id": "T", "disposition": "fix",
                                 "reply_text": "fixed"}}}
        fx = write_json(os.path.join(root, "fix.json"), fl)
        rpp = write_json(os.path.join(root, "rep.json"), rp)
        rc, out = run("guard-slice", "--repo", work, "--branch", "feature",
                      "--fix-list", fx, rpp)
        check("guard-slice: fix reply w/ finding absent from fix-list -> exit 1",
              rc == 1)


def test_guard_slice_addressed_no_sha_fails():
    with tempfile.TemporaryDirectory() as root:
        work = make_origin_and_work(root)
        commit(root, work, "fix F1\n\nFinding-Id: F1"); push(root, work)
        # fix-list entry for F1 is `fix`-replied but has no fix_sha.
        fl = {"schema": "finding-fix-list/v1", "round": 1,
              "findings": [{"id": "F1", "severity": "high", "detail": "d",
                            "status": "open", "fix_sha": None}]}
        rp = {"schema": "finding-reply-slice/v1",
              "replies": {"F1": {"thread_id": "T", "disposition": "fix",
                                 "reply_text": "fixed"}}}
        fx = write_json(os.path.join(root, "fix.json"), fl)
        rpp = write_json(os.path.join(root, "rep.json"), rp)
        rc, out = run("guard-slice", "--repo", work, "--branch", "feature",
                      "--fix-list", fx, rpp)
        check("guard-slice: fix reply but fix-list entry has no fix_sha -> exit 1",
              rc == 1)


# --- usage ------------------------------------------------------------------

def test_no_subcommand_usage():
    rc, out = run()
    check("no subcommand -> exit 2 (usage)", rc == 2)


def test_unknown_subcommand_usage():
    rc, out = run("frobnicate")
    check("unknown subcommand -> exit 2 (usage)", rc == 2)


def main():
    print("test-finding-channel.py")
    for fn in [
        test_validate_good_fix_list, test_validate_good_reply_slice,
        test_validate_schema_error_surfaced, test_validate_round_lt_1,
        test_validate_addressed_without_sha, test_validate_duplicate_ids,
        test_validate_fix_disposition_empty_reply_text,
        test_validate_fix_disposition_whitespace_reply_text,
        test_liveness_zero_deadline_is_usage_exit2,
        test_liveness_nonint_deadline_is_usage_exit2,
        test_git_calls_are_non_interactive,
        test_validate_missing_file, test_validate_unknown_kind,
        test_liveness_missing, test_liveness_fresh, test_liveness_slow,
        test_liveness_stalled, test_liveness_dead,
        test_guard_reply_pass, test_guard_reply_short_sha_normalized,
        test_guard_reply_unpushed_fails, test_guard_reply_no_trailer_fails,
        test_guard_reply_wrong_finding_trailer_fails, test_guard_reply_bad_sha_fails,
        test_guard_reply_unreachable_origin_exit2,
        test_guard_reply_absent_branch_stale_ref_fails,
        test_guard_slice_absent_branch_stale_ref_fails,
        test_guard_slice_missing_fix_list_is_io_exit2,
        test_guard_reply_no_fetch,
        test_guard_slice_pass, test_guard_slice_unpushed_fails,
        test_guard_slice_fix_missing_from_fix_list,
        test_guard_slice_addressed_no_sha_fails,
        test_no_subcommand_usage, test_unknown_subcommand_usage,
    ]:
        print(f"- {fn.__name__}")
        fn()
    print()
    if FAILS:
        print(f"FAIL ({len(FAILS)} check(s) failed):")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("all finding-channel checks passed")


if __name__ == "__main__":
    main()
