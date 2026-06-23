#!/usr/bin/env python3
"""Proof harness for ship-gate-preflight.sh (#110).

The oracle is a read-only merge-readiness gate that must FAIL CLOSED on every
ambiguous/unknown state. This harness stubs BOTH external dependencies so it is
deterministic and host-independent - it NEVER calls real gh or the real
pr-unreplied-comments.sh:

  - `gh` is a temp 0755 script placed first on PATH. It serves the canned
    statusCheckRollup JSON from the $FIXTURE_JSON env var for `gh pr view`, and a
    fixed slug for `gh repo view`. A $GH_FAIL env var makes it exit non-zero
    (to exercise the fail-closed gh-error path).
  - pr-unreplied-comments.sh is stubbed under a temp $HOME/.claude/scripts/ (the
    oracle resolves it via $HOME). Its behavior is driven by env vars:
      UNREPLIED_FINDINGS  -> prints the "Review-body comments with actionable
                             findings: N" line when N>0 (mirrors the real script,
                             which only prints the line when N>0)
      UNREPLIED_FAIL=1     -> exit 2 (helper error -> oracle must BLOCK)
      UNREPLIED_MISSING=1  -> do not create the helper at all (oracle must BLOCK)

Decision contract asserted:
  exit 0 = PASS, exit 1 = USAGE error, exit 2 = BLOCK (fail-closed).

Run: python3 test-ship-gate-preflight.py
"""
import json
import os
import subprocess
import sys
import tempfile

ORACLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "ship-gate-preflight.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


def rollup(*contexts, review_decision="__OMIT__"):
    """Build a statusCheckRollup JSON document from context dicts.

    review_decision: when supplied, add a top-level `reviewDecision` field (the
    #117 gate). Use None to emit an explicit JSON null; leave at the "__OMIT__"
    sentinel to omit the field entirely (mirrors a gh response that carries only
    statusCheckRollup). Both null and absent must read as 'no active decision'."""
    doc = {"statusCheckRollup": list(contexts)}
    if review_decision != "__OMIT__":
        doc["reviewDecision"] = review_decision
    return json.dumps(doc)


def checkrun(name, status, conclusion):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def statusctx(context, state):
    return {"__typename": "StatusContext", "context": context, "state": state}


def unknowntype(name, status="COMPLETED", conclusion="SUCCESS", typename="UnknownType"):
    """A context whose __typename is NEITHER CheckRun nor StatusContext (or is
    absent, when typename=None). gh always emits __typename to discriminate the
    union, so an unknown/absent type is ambiguous -> the oracle MUST BLOCK rather
    than treat it as a CheckRun (the #110 fail-open BLOCKER)."""
    d = {"name": name, "status": status, "conclusion": conclusion}
    if typename is not None:
        d["__typename"] = typename
    return d


def run(args, *, fixture_json, gh_fail=False, unreplied_findings=0,
        unreplied_fail=False, unreplied_missing=False, unreplied_raw=None,
        unreplied_fail_until=0):
    """Invoke the oracle with stubbed gh + pr-unreplied-comments.sh.
    Returns (exit_code, stdout, stderr, argfile) where argfile is the path the
    helper records its received argv into (one line per invocation) -- used to
    assert the oracle passes --allow-stale and to drive the retry cases.
    UNREPLIED_FAIL_UNTIL=N makes the stub exit 2 on its first N invocations
    (counted via a counter file) then succeed, exercising the bounded retry."""
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        home = os.path.join(td, "home")
        helper_dir = os.path.join(home, ".claude", "scripts"); os.makedirs(helper_dir)
        argfile = os.path.join(td, "helper-argv.log")
        counter = os.path.join(td, "helper-calls.log")

        # Stub gh.
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "if [ -n \"${GH_FAIL:-}\" ]; then echo 'gh: simulated failure' >&2; exit 1; fi\n"
                "# `gh repo view --json nameWithOwner --jq .nameWithOwner`\n"
                "for a in \"$@\"; do case \"$a\" in repo) echo 'owner/repo'; exit 0;; esac; done\n"
                "# `gh pr view <pr> --repo <repo> --json statusCheckRollup`\n"
                "printf '%s' \"${FIXTURE_JSON:-}\"\n"
            )
        os.chmod(gh, 0o755)

        # Stub pr-unreplied-comments.sh (unless we are simulating it missing).
        if not unreplied_missing:
            helper = os.path.join(helper_dir, "pr-unreplied-comments.sh")
            with open(helper, "w") as f:
                f.write(
                    "#!/usr/bin/env bash\n"
                    "set -eu\n"
                    "# Record the received argv (one line per invocation) so the test can\n"
                    "# assert --allow-stale is passed and count retry attempts.\n"
                    "printf '%s\\n' \"$*\" >> \"$HELPER_ARGV_LOG\"\n"
                    "# Count invocations for the bounded-retry cases.\n"
                    "echo x >> \"$HELPER_CALLS_LOG\"\n"
                    "calls=$(wc -l < \"$HELPER_CALLS_LOG\" | tr -d '[:space:]')\n"
                    "# UNREPLIED_FAIL_UNTIL=N: exit 2 on the first N calls, then succeed.\n"
                    "fu=\"${UNREPLIED_FAIL_UNTIL:-0}\"\n"
                    "if [ \"$calls\" -le \"$fu\" ]; then\n"
                    "  echo 'helper: simulated TRANSIENT failure' >&2; exit 2\n"
                    "fi\n"
                    "if [ -n \"${UNREPLIED_FAIL:-}\" ]; then\n"
                    "  echo 'STOP: head branch is behind base. Rebase before starting triage:'\n"
                    "  echo 'helper: simulated PERSISTENT failure line'\n"
                    "  exit 2\n"
                    "fi\n"
                    "# UNREPLIED_RAW prints the findings line with a RAW (possibly non-numeric)\n"
                    "# count, to exercise the fail-closed-on-non-numeric path.\n"
                    "if [ -n \"${UNREPLIED_RAW:-}\" ]; then\n"
                    "  echo \"=== Review-body comments with actionable findings: ${UNREPLIED_RAW} ===\"\n"
                    "  echo 'some other output line'; exit 0\n"
                    "fi\n"
                    "n=\"${UNREPLIED_FINDINGS:-0}\"\n"
                    "# Real script prints the line ONLY when N>0.\n"
                    "if [ \"$n\" -gt 0 ]; then\n"
                    "  echo \"=== Review-body comments with actionable findings: $n ===\"\n"
                    "fi\n"
                    "echo 'some other output line'\n"
                )
            os.chmod(helper, 0o755)

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["HOME"] = home
        env["FIXTURE_JSON"] = fixture_json
        if gh_fail:
            env["GH_FAIL"] = "1"
        env["UNREPLIED_FINDINGS"] = str(unreplied_findings)
        if unreplied_fail:
            env["UNREPLIED_FAIL"] = "1"
        if unreplied_raw is not None:
            env["UNREPLIED_RAW"] = unreplied_raw
        env["UNREPLIED_FAIL_UNTIL"] = str(unreplied_fail_until)
        env["HELPER_ARGV_LOG"] = argfile
        env["HELPER_CALLS_LOG"] = counter

        p = subprocess.run([ORACLE] + args, env=env, capture_output=True, text=True, timeout=30)
        try:
            argv_log = open(argfile).read()
        except OSError:
            argv_log = ""
        return p.returncode, p.stdout, p.stderr, argv_log


ALL_GREEN = rollup(
    checkrun("ci", "COMPLETED", "SUCCESS"),
    statusctx("buildkite", "SUCCESS"),
)


def main():
    print("== FULL MODE: pass path ==")
    rc, out, err, _ = run(["123", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0)
    check("all checks green (CheckRun SUCCESS + StatusContext SUCCESS) + 0 unreplied -> exit 0",
          rc == 0)

    rc, out, err, _ = run(["123", "owner/repo"],
                       fixture_json=rollup(checkrun("a", "COMPLETED", "NEUTRAL"),
                                           checkrun("b", "COMPLETED", "SKIPPED")),
                       unreplied_findings=0)
    check("NEUTRAL + SKIPPED conclusions still pass -> exit 0", rc == 0)

    print("== FULL MODE: CheckRun block paths ==")
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=rollup(checkrun("ci", "COMPLETED", "FAILURE")))
    check("CheckRun conclusion=FAILURE -> exit 2", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=rollup(checkrun("ci", "COMPLETED", None)))
    check("CheckRun conclusion=null -> exit 2", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=rollup(checkrun("ci", "IN_PROGRESS", None)))
    check("CheckRun status=IN_PROGRESS -> exit 2", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=rollup(checkrun("ci", "QUEUED", None)))
    check("CheckRun status=QUEUED -> exit 2", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=rollup(checkrun("ci", "COMPLETED", "CANCELLED")))
    check("CheckRun conclusion=CANCELLED -> exit 2", rc == 2)
    # A green CheckRun mixed with one bad one must still block.
    rc, _, _, _ = run(["1", "owner/repo"],
                   fixture_json=rollup(checkrun("ok", "COMPLETED", "SUCCESS"),
                                       checkrun("bad", "COMPLETED", "FAILURE")))
    check("mixed green + FAILURE -> exit 2", rc == 2)

    print("== FULL MODE: StatusContext block paths ==")
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=rollup(statusctx("sc", "PENDING")))
    check("StatusContext state=PENDING -> exit 2", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=rollup(statusctx("sc", "ERROR")))
    check("StatusContext state=ERROR -> exit 2", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=rollup(statusctx("sc", "EXPECTED")))
    check("StatusContext state=EXPECTED -> exit 2", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=rollup(statusctx("sc", "FAILURE")))
    check("StatusContext state=FAILURE -> exit 2", rc == 2)

    print("== FULL MODE: empty rollup + review-gate paths ==")
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=rollup())
    check("empty rollup (no checks) -> exit 2 (no vacuous pass)", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json='{"statusCheckRollup":null}')
    check("null rollup -> exit 2", rc == 2)

    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=3)
    check("all green but N=3 unreplied findings -> exit 2", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_fail=True)
    check("pr-unreplied-comments.sh errors -> exit 2 (fail closed)", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_missing=True)
    check("pr-unreplied-comments.sh missing -> exit 2 (fail closed)", rc == 2)
    # Codoki #116: a findings line PRESENT but with a non-numeric count must BLOCK,
    # not be silently read as N=0 (the fail-open the parse comment intended to prevent).
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_raw="N/A")
    check("review-body findings line present but NON-NUMERIC count -> exit 2 (fail closed)", rc == 2)
    # Sanity: a numeric count embedded in a richer line is still parsed and blocks.
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_raw="4 (2 major)")
    check("review-body findings line with numeric-prefixed count (4 ...) -> exit 2", rc == 2)

    print("== #178: --allow-stale invocation + retry + surfaced failure ==")
    # The oracle MUST invoke the helper with --allow-stale so a behind-base PR (the
    # helper's deterministic exit-2 STOP) is not masked as a generic "helper failed".
    rc, out, err, argv = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0)
    check("#178: pass path still exits 0", rc == 0)
    check("#178: helper invoked WITH --allow-stale", "--allow-stale" in argv)
    # A persistent non-zero (behind-base STOP line included) still BLOCKs (fail closed),
    # and the BLOCK message SURFACES the helper exit code + an output tail for diagnosis.
    rc, out, err, argv = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_fail=True)
    check("#178: persistent helper failure -> exit 2 (fail closed)", rc == 2)
    check("#178: BLOCK surfaces the helper exit code (exit 2)", "exit 2" in err)
    check("#178: BLOCK surfaces a tail of the helper output (STOP line)",
          "head branch is behind base" in err or "PERSISTENT failure" in err)
    # A genuinely TRANSIENT failure (fails the first 2 attempts, succeeds on the 3rd)
    # is absorbed by the bounded retry and PASSES -- no spurious BLOCK.
    rc, out, err, argv = run(["1", "owner/repo"], fixture_json=ALL_GREEN,
                             unreplied_findings=0, unreplied_fail_until=2)
    check("#178: helper succeeds on retry (3rd attempt) -> exit 0 (PASS)", rc == 0)
    # Failing all 3 attempts (fail_until >= 3) still BLOCKs.
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_fail_until=3)
    check("#178: helper fails all 3 attempts -> exit 2 (BLOCK)", rc == 2)

    print("== FAIL-CLOSED: gh / json errors ==")
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, gh_fail=True)
    check("gh failure -> exit 2 (fail closed)", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json="this is not json{")
    check("malformed JSON from gh -> exit 2 (fail closed)", rc == 2)

    print("== FAIL-CLOSED: unknown / absent __typename (the #110 fail-open BLOCKER) ==")
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=rollup(unknowntype("ci")))
    check("full: unknown __typename with green-looking CheckRun fields -> exit 2", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=rollup(unknowntype("ci", typename=None)))
    check("full: ABSENT __typename -> exit 2", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"],
                   fixture_json=rollup(checkrun("ok", "COMPLETED", "SUCCESS"), unknowntype("x")))
    check("full: green CheckRun + unknown-type check -> exit 2 (unknown blocks, no mask)", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"],
                   fixture_json=rollup(unknowntype("Codoki PR Review")))
    check("codoki-only: unknown __typename named like Codoki -> exit 2", rc == 2)

    print("== CODOKI-ONLY MODE ==")
    codoki_ok = rollup(checkrun("Codoki PR Review", "COMPLETED", "SUCCESS"),
                       checkrun("ci", "IN_PROGRESS", None))  # other checks irrelevant here
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"], fixture_json=codoki_ok)
    check("--codoki-only: Codoki COMPLETED/SUCCESS -> exit 0 (ignores other checks)", rc == 0)

    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"],
                   fixture_json=rollup(checkrun("ci", "COMPLETED", "SUCCESS")))
    check("--codoki-only: Codoki check missing -> exit 2", rc == 2)

    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"],
                   fixture_json=rollup(checkrun("Codoki PR Review", "IN_PROGRESS", None)))
    check("--codoki-only: Codoki IN_PROGRESS -> exit 2", rc == 2)

    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"],
                   fixture_json=rollup(checkrun("Codoki PR Review", "COMPLETED", "FAILURE")))
    check("--codoki-only: Codoki COMPLETED/FAILURE -> exit 2", rc == 2)

    # --codoki-only must NOT consult pr-unreplied-comments.sh: a missing helper
    # must still PASS when Codoki is settled (proves the helper is skipped).
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"], fixture_json=codoki_ok,
                   unreplied_missing=True)
    check("--codoki-only skips pr-unreplied-comments.sh (missing helper still PASS)", rc == 0)

    # --codoki-pattern override matches a differently-named check.
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only", "--codoki-pattern", "My Bot"],
                   fixture_json=rollup(checkrun("My Bot", "COMPLETED", "SUCCESS")))
    check("--codoki-pattern override matches a differently-named check -> exit 0", rc == 0)
    # ... and the default pattern then does NOT match it.
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"],
                   fixture_json=rollup(checkrun("My Bot", "COMPLETED", "SUCCESS")))
    check("default pattern does not match 'My Bot' -> exit 2", rc == 2)
    # A StatusContext-shaped Codoki check is accepted via name/state mapping.
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"],
                   fixture_json=rollup(statusctx("Codoki PR Review", "SUCCESS")))
    check("--codoki-only: StatusContext Codoki SUCCESS -> exit 0", rc == 0)

    print("== FULL MODE: reviewDecision gate (#117, coupled with findings) ==")
    green_ctx = (checkrun("ci", "COMPLETED", "SUCCESS"), statusctx("buildkite", "SUCCESS"))
    # 2a. ACTIVE CHANGES_REQUESTED (reviewDecision set AND actionable findings>0)
    #     -> BLOCK. The findings>0 gate catches it (the coupling by ordering).
    rc, _, _, _ = run(["1", "owner/repo"],
                   fixture_json=rollup(*green_ctx, review_decision="CHANGES_REQUESTED"),
                   unreplied_findings=3)
    check("CHANGES_REQUESTED + 3 actionable findings (active) -> exit 2 (block)", rc == 2)
    # 2b. APPROVED with 0 findings -> PASS.
    rc, _, _, _ = run(["1", "owner/repo"],
                   fixture_json=rollup(*green_ctx, review_decision="APPROVED"),
                   unreplied_findings=0)
    check("reviewDecision=APPROVED + 0 findings -> exit 0 (pass)", rc == 0)
    # 2c. REVIEW_REQUIRED and explicit null with 0 findings -> PASS (no active decision).
    rc, _, _, _ = run(["1", "owner/repo"],
                   fixture_json=rollup(*green_ctx, review_decision="REVIEW_REQUIRED"),
                   unreplied_findings=0)
    check("reviewDecision=REVIEW_REQUIRED + 0 findings -> exit 0 (pass)", rc == 0)
    rc, _, _, _ = run(["1", "owner/repo"],
                   fixture_json=rollup(*green_ctx, review_decision=None),
                   unreplied_findings=0)
    check("reviewDecision=null + 0 findings -> exit 0 (pass)", rc == 0)
    rc, _, _, _ = run(["1", "owner/repo"],
                   fixture_json=rollup(*green_ctx),  # field omitted entirely
                   unreplied_findings=0)
    check("reviewDecision absent + 0 findings -> exit 0 (pass)", rc == 0)
    # 2d. SUPERSEDED CHANGES_REQUESTED: reviewDecision still CHANGES_REQUESTED but
    #     the fix landed so 0 actionable findings remain -> STAYS PASS. This is the
    #     key regression: the gate must NEVER block a stale/superseded review.
    rc, _, _, _ = run(["1", "owner/repo"],
                   fixture_json=rollup(*green_ctx, review_decision="CHANGES_REQUESTED"),
                   unreplied_findings=0)
    check("SUPERSEDED CHANGES_REQUESTED (0 findings) -> exit 0 (pass, no regression)", rc == 0)
    # 2e. UNRECOGNIZED reviewDecision value -> FAIL CLOSED (exit 2), mirroring the
    #     unknown-__typename posture (even with all checks green and 0 findings).
    rc, _, _, _ = run(["1", "owner/repo"],
                   fixture_json=rollup(*green_ctx, review_decision="WEIRD_STATE"),
                   unreplied_findings=0)
    check("unrecognized reviewDecision 'WEIRD_STATE' -> exit 2 (fail closed)", rc == 2)
    # ... and case-insensitively normalized: a lowercased known value still passes.
    rc, _, _, _ = run(["1", "owner/repo"],
                   fixture_json=rollup(*green_ctx, review_decision="approved"),
                   unreplied_findings=0)
    check("reviewDecision='approved' (lowercase) normalizes -> exit 0 (pass)", rc == 0)
    # codoki-only must NOT consult reviewDecision: a CHANGES_REQUESTED settles fine
    # so long as the Codoki check is green (the gate is full-mode only).
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"],
                   fixture_json=rollup(checkrun("Codoki PR Review", "COMPLETED", "SUCCESS"),
                                       review_decision="CHANGES_REQUESTED"))
    check("--codoki-only ignores reviewDecision=CHANGES_REQUESTED -> exit 0", rc == 0)

    print("== USAGE ==")
    rc, _, _, _ = run([], fixture_json=ALL_GREEN)
    check("no args -> exit 1 (usage)", rc == 1)
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-pattern"], fixture_json=ALL_GREEN)
    check("--codoki-pattern with no value -> exit 1 (usage)", rc == 1)
    rc, _, _, _ = run(["1", "owner/repo", "--bogus"], fixture_json=ALL_GREEN)
    check("unknown flag -> exit 1 (usage)", rc == 1)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
