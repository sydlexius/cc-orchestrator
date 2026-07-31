#!/usr/bin/env python3
"""Proof harness for elmer-enqueue.sh -- the RECEIPT-GATED queue writer.

THE CENTRAL CLAIM UNDER TEST: a TL cannot file a review request for code that has
not passed /prep-pr. The gate is not honor-system -- it is a string compare:

    gate-receipt/v1 {commit_sha, result} ... AND commit_sha == the PR's CURRENT head

A TL who ran /prep-pr and then pushed two more commits holds a receipt whose
commit_sha no longer matches HEAD, so enqueue REFUSES. That is the same
"verify, do not classify" shape as #337's branch delete: check the fact, never
the claim.

Every external dependency is stubbed, so the harness never touches the network and
never writes outside a temp dir:
  - `gh` is a temp 0755 script first on PATH, serving a canned PR view from
    $PR_JSON (headRefOid / state) and a fixed repo slug.
  - The maildir is redirected via $ELMER_HOME, so the real ~/.claude/elmer is
    never touched.

Contract asserted:
  exit 0  enqueued (entry written to inbox/)
  exit 1  REFUSED  (gate failed: no/invalid/failing/stale receipt, closed PR,
                    `full` review form, or already queued/drained)
  exit 2  setup error (bad args, unresolvable repo, gh read failure)

A refusal must NEVER leave a queue entry behind, and must never be silent.

Run: python3 test-elmer-enqueue.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "scripts", "elmer-enqueue.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


# --- Fixtures ---------------------------------------------------------------
SHA_A = "a" * 40          # the gated commit
SHA_B = "b" * 40          # a later push -> receipt goes stale
TREE = "c" * 40


def receipt(commit=SHA_A, result="pass", schema="gate-receipt/v1", steps=None):
    return {
        "schema": schema,
        "commit_sha": commit,
        "tree_sha": TREE,
        "worktree": "/tmp/wt",
        "result": result,
        "steps": steps if steps is not None else [{"name": "shellcheck", "result": "pass"}],
        "producer": "gate-runner",
    }


def pr_json(head=SHA_A, state="OPEN"):
    return json.dumps({"headRefOid": head, "state": state, "number": 42})


def run(args, *, receipt_obj="__default__", pr=None, api_fail=False,
        repo_fail=False, home=None, write_receipt=True, raw_receipt=None,
        timeout=30, close_fd=None):
    """Invoke enqueue with a stubbed gh + isolated ELMER_HOME.

    Returns (rc, stdout, stderr, inbox_entries, home_dir_kept_alive)."""
    td = tempfile.mkdtemp()
    bindir = os.path.join(td, "bin"); os.makedirs(bindir)
    gh = os.path.join(bindir, "gh")
    with open(gh, "w") as f:
        f.write(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "case \"${1:-}\" in\n"
            "  repo) [ -n \"${GH_REPO_FAIL:-}\" ] && exit 1; echo 'owner/repo'; exit 0;;\n"
            "  pr)   [ -n \"${GH_API_FAIL:-}\" ] && exit 1; printf '%s' \"${PR_JSON:-{}}\"; exit 0;;\n"
            "esac\n"
            "exit 0\n"
        )
    os.chmod(gh, 0o755)

    rpath = os.path.join(td, "receipt.json")
    if raw_receipt is not None:
        with open(rpath, "w") as f:
            f.write(raw_receipt)
    elif write_receipt:
        obj = receipt() if receipt_obj == "__default__" else receipt_obj
        if obj is not None:
            with open(rpath, "w") as f:
                json.dump(obj, f)

    elmer_home = home or os.path.join(td, "elmer")

    env = dict(os.environ)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env["PR_JSON"] = pr if pr is not None else pr_json()
    env["ELMER_HOME"] = elmer_home
    if api_fail:
        env["GH_API_FAIL"] = "1"
    if repo_fail:
        env["GH_REPO_FAIL"] = "1"

    full = [SCRIPT] + [a.replace("__RECEIPT__", rpath) for a in args]
    if close_fd == 1:
        # `>&-` cannot be expressed through subprocess's stdout parameter (every value
        # it accepts is an OPEN fd), so the closing is done by an exec'd bash wrapper --
        # the same construct that reproduced the defect by hand. stderr stays captured
        # so the assertion can still see what the run reported.
        full = ["bash", "-c", 'exec "$@" >&-', "sh"] + full
    elif close_fd == 2:
        full = ["bash", "-c", 'exec "$@" 2>&-', "sh"] + full
    p = subprocess.run(full, env=env, capture_output=True, text=True, timeout=timeout)

    inbox = os.path.join(elmer_home, "inbox")
    entries = sorted(os.listdir(inbox)) if os.path.isdir(inbox) else []
    return p.returncode, p.stdout, p.stderr, entries, elmer_home


def main():
    print("== arg validation ==")
    rc, out, _, _, _ = run(["--help"])
    check("--help -> exit 0, prints usage", rc == 0 and "elmer-enqueue" in out)
    rc, _, _, _, _ = run([])
    check("no args -> exit 2", rc == 2)
    rc, _, _, _, _ = run(["notanumber", "--receipt", "__RECEIPT__"])
    check("non-numeric PR -> exit 2", rc == 2)
    rc, _, err, _, _ = run(["42"])
    check("missing --receipt -> exit 2", rc == 2 and "receipt" in err.lower())
    rc, _, err, _, _ = run(["42", "--receipt", "__RECEIPT__"], repo_fail=True)
    check("repo unresolvable -> exit 2", rc == 2 and "setup error" in err)

    # A TRAILING FLAG WITH NO VALUE MUST FAIL, NOT SPIN. With `shift 2 || true` the
    # failed shift is swallowed, `$#` never decreases, and the arg loop runs forever
    # at 100% CPU with no output - an unattended stall in a Bash call that never
    # returns. `run()` passes a subprocess timeout, so a regression FAILS this case
    # instead of hanging the harness (`timeout(1)` is not available on macOS, so the
    # bound is enforced in python).
    for args, label in (
        (["42", "owner/repo", "--receipt"], "trailing --receipt with no value"),
        (["42", "owner/repo", "--form"], "trailing --form with no value"),
        (["--receipt"], "bare --receipt alone"),
    ):
        try:
            rc, _, err, entries, _ = run(args, timeout=10)
            check(f"{label} -> exit 2 promptly", rc == 2)
            check(f"{label} -> nothing queued", entries == [])
        except subprocess.TimeoutExpired:
            check(f"{label} -> exit 2 promptly", False)
            check(f"{label} -> nothing queued", False)

    print("== the happy path ==")
    rc, out, err, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"])
    check("valid pass-receipt matching HEAD -> exit 0", rc == 0)
    check("exactly one inbox entry written", len(entries) == 1)
    check("entry name carries repo, PR, and short sha",
          entries and "42" in entries[0] and SHA_A[:12] in entries[0] and "owner" in entries[0])

    print("== THE GATE: receipt must exist, be valid, PASS, and match HEAD ==")
    rc, _, err, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                                 write_receipt=False)
    check("no receipt at path -> exit 1 REFUSED", rc == 1)
    check("no receipt -> nothing queued", entries == [])
    check("no receipt -> says why", "receipt" in err.lower())

    rc, _, err, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                                 receipt_obj=receipt(result="fail"))
    check("result=fail -> exit 1 REFUSED", rc == 1 and entries == [])

    # THE LOAD-BEARING CASE: gate passed on SHA_A, but the branch has moved to SHA_B.
    rc, _, err, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                                 receipt_obj=receipt(commit=SHA_A), pr=pr_json(head=SHA_B))
    check("STALE receipt (gated sha != PR head) -> exit 1 REFUSED", rc == 1)
    check("stale receipt -> nothing queued", entries == [])
    check("stale receipt -> names the mismatch and points at /prep-pr",
          "stale" in err.lower() and "prep-pr" in err)

    rc, _, err, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                                 raw_receipt="{not valid json")
    check("malformed receipt JSON -> exit 1 REFUSED (not a crash)", rc == 1 and entries == [])

    rc, _, err, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                                 receipt_obj=receipt(schema="something-else/v1"))
    check("wrong schema id -> exit 1 REFUSED", rc == 1 and entries == [])

    rc, _, err, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                                 raw_receipt=json.dumps({"schema": "gate-receipt/v1"}))
    check("receipt missing commit_sha/result -> exit 1 REFUSED", rc == 1 and entries == [])

    print("== a receipt from a DIFFERENT tool must not pass ==")
    rc, _, _, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                               raw_receipt=json.dumps(
                                   {"schema": "gate-receipt/v1", "commit_sha": SHA_A,
                                    "tree_sha": TREE, "worktree": "/tmp/wt",
                                    "result": "pass", "steps": [],
                                    "producer": "handmade"}))
    check("producer != gate-runner -> exit 1 REFUSED", rc == 1 and entries == [])

    print("== PR state ==")
    rc, _, err, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                                 pr=pr_json(state="MERGED"))
    check("merged PR -> exit 1 REFUSED", rc == 1 and entries == [])
    rc, _, err, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                                 pr=pr_json(state="CLOSED"))
    check("closed PR -> exit 1 REFUSED", rc == 1 and entries == [])
    rc, _, err, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                                 api_fail=True)
    check("gh read failure -> exit 2 (never a silent enqueue)", rc == 2 and entries == [])

    print("== trigger form: `full` is refused outright ==")
    rc, _, err, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__",
                                  "--form", "full"])
    check("--form full -> exit 1 REFUSED", rc == 1 and entries == [])
    check("refusal explains full is reserved", "full" in err.lower())
    rc, _, _, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__",
                                "--form", "incremental"])
    check("--form incremental -> exit 0", rc == 0 and len(entries) == 1)

    print("== idempotency: never queue the same PR+SHA twice ==")
    _, _, _, _, home = run(["42", "owner/repo", "--receipt", "__RECEIPT__"])
    rc, _, err, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"], home=home)
    check("second enqueue of same PR+SHA -> exit 1 REFUSED", rc == 1)
    check("still exactly one entry (no duplicate)", len(entries) == 1)
    # A drained record must also suppress a re-queue: that IS the audit trail.
    drained = os.path.join(home, "drained")
    os.makedirs(drained, exist_ok=True)
    for e in os.listdir(os.path.join(home, "inbox")):
        os.replace(os.path.join(home, "inbox", e), os.path.join(drained, e))
    rc, _, err, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"], home=home)
    check("already DRAINED -> exit 1 REFUSED (never re-post)", rc == 1)
    check("drained suppression leaves inbox empty", entries == [])
    # ... but a NEW sha on the same PR is a legitimately new request.
    rc, _, _, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                               receipt_obj=receipt(commit=SHA_B), pr=pr_json(head=SHA_B),
                               home=home)
    check("same PR at a NEW gated sha -> exit 0 (a real new request)",
          rc == 0 and len(entries) == 1)

    print("== the entry is machine-readable and carries the gate evidence ==")
    rc, _, _, entries, home = run(["42", "owner/repo", "--receipt", "__RECEIPT__"])
    body = json.load(open(os.path.join(home, "inbox", entries[0])))
    check("entry is valid JSON with repo/pr/sha", body.get("repo") == "owner/repo"
          and str(body.get("pr")) == "42" and body.get("commit_sha") == SHA_A)
    check("entry records the trigger form as incremental", body.get("form") == "incremental")
    check("entry cites the receipt path it was gated on", "receipt" in body)
    check("entry is timestamped", bool(body.get("enqueued_at")))

    print("== a CLOSED output fd must never change the exit code ==")
    # THE CLASS: under `set -e` a bare `echo` onto a CLOSED or BROKEN fd (EBADF/EIO -
    # an unattended loop redirecting into a rotated or closed log) FAILS, and that
    # failure becomes the script's exit status, OVERRIDING the intended code. Every
    # write site in this file was unguarded, so:
    #   - the SUCCESS path's three echoes sit BELOW the `mv` that publishes the entry,
    #     so the entry was PUBLISHED and the caller was told rc=1 (REFUSED) while the
    #     tick would go on to post it - a false premise about the one outcome that
    #     ends in a spent review slot;
    #   - every deliberate `exit 2` (setup error) downgraded to 1 (REFUSED), sending
    #     the operator to fix a gate that never complained.
    # These cases are the reason the fix is a WHOLE-FILE sweep and not a per-site patch.
    rc, _, _, entries, home = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                                  close_fd=1)
    check("SUCCESS with stdout closed -> still exit 0", rc == 0)
    check("SUCCESS with stdout closed -> the entry IS published", len(entries) == 1)

    rc, _, _, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                               write_receipt=False, close_fd=2)
    check("REFUSED with stderr closed -> still exit 1", rc == 1)
    check("REFUSED with stderr closed -> nothing queued", entries == [])

    rc, _, _, entries, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                               api_fail=True, close_fd=2)
    check("SETUP ERROR with stderr closed -> still exit 2, never a downgrade to 1",
          rc == 2)
    check("setup error with stderr closed -> nothing queued", entries == [])

    rc, _, _, _, _ = run(["notanumber", "--receipt", "__RECEIPT__"], close_fd=2)
    check("arg validation with stderr closed -> still exit 2", rc == 2)
    rc, _, _, _, _ = run([], close_fd=2)
    check("usage path with stderr closed -> still exit 2", rc == 2)
    rc, _, _, _, _ = run(["42", "owner/repo", "--receipt"], timeout=10, close_fd=2)
    check("trailing --receipt with stderr closed -> still exit 2", rc == 2)
    rc, _, _, _, _ = run(["42", "--badflag", "--receipt", "__RECEIPT__"], close_fd=2)
    check("unknown flag with stderr closed -> still exit 2", rc == 2)
    rc, _, _, _, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__",
                          "--form", "full"], close_fd=2)
    check("form=full refusal with stderr closed -> still exit 1", rc == 1)
    # No explicit owner/repo here on purpose: the resolution path is only reached when
    # the slug is NOT given as an argument.
    rc, _, _, _, _ = run(["42", "--receipt", "__RECEIPT__"], repo_fail=True, close_fd=2)
    check("unresolvable repo with stderr closed -> still exit 2", rc == 2)
    rc, _, _, _, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                         receipt_obj=receipt(commit=SHA_A), pr=pr_json(head=SHA_B),
                         close_fd=2)
    check("stale receipt with stderr closed -> still exit 1", rc == 1)
    rc, _, out_, _, _ = run(["--help"], close_fd=1)
    check("--help with stdout closed -> still exit 0 (a help request is not an error)",
          rc == 0)

    print("== OVER-HARDENING: a guard must not become a mute ==")
    # `{ ... } >&2 || true` is one careless edit from `{ ... } >/dev/null`, and BOTH
    # satisfy every rc assertion above while the second silently discards the report.
    # These assert the messages STILL LAND when the fd works, at both ends of the file.
    rc, out, err, _, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"])
    check("success line still prints on stdout", "ENQUEUED" in out and "entry:" in out)
    check("success line still names the no-post invariant",
          "posts nothing" in out)
    rc, _, err, _, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                           receipt_obj=receipt(commit=SHA_A), pr=pr_json(head=SHA_B))
    check("the STALE refusal still prints ALL FOUR lines",
          "STALE" in err and SHA_A in err and SHA_B in err and "prep-pr" in err)
    rc, _, err, _, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__",
                            "--form", "full"])
    check("the form=full refusal still prints BOTH lines",
          "not enqueueable" in err and "resolved threads" in err)
    rc, _, err, _, _ = run(["42", "owner/repo", "--receipt", "__RECEIPT__"],
                           api_fail=True)
    check("the gh-read setup error still names the PR", "#42" in err)
    rc, out, _, _, _ = run(["--help"])
    check("--help still prints the header", "elmer-enqueue" in out and "Exit codes" in out)

    print("== read-only toward GitHub: enqueue never posts ==")
    src = open(SCRIPT).read()
    for forbidden in ["gh pr comment", "gh api", "-X POST", "--method POST",
                      "coderabbitai", "gh pr merge"]:
        check(f"source contains no '{forbidden}'", forbidden not in src)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
