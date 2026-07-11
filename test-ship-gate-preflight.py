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


# A valid-looking 40-hex head SHA the oracle emits on PASS (#263 Piece A). The
# real oracle parses `headRefOid` from the SAME `gh pr view` snapshot as the
# checks, so every PASS fixture carries one by default.
DEFAULT_SHA = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"


def rollup(*contexts, review_decision="__OMIT__", head_ref_oid=DEFAULT_SHA):
    """Build a statusCheckRollup JSON document from context dicts.

    review_decision: when supplied, add a top-level `reviewDecision` field (the
    #117 gate). Use None to emit an explicit JSON null; leave at the "__OMIT__"
    sentinel to omit the field entirely (mirrors a gh response that carries only
    statusCheckRollup). Both null and absent must read as 'no active decision'.

    head_ref_oid (#263 Piece A): the `headRefOid` field the oracle emits on PASS.
    Defaults to DEFAULT_SHA so existing PASS fixtures keep passing. Pass None to
    emit an explicit JSON null, or "" to emit an empty string -- both must read as
    an unreadable SHA and BLOCK a PASS (a PASS with no pinnable SHA is useless to
    the downstream authorize-merge step)."""
    doc = {"statusCheckRollup": list(contexts)}
    if review_decision != "__OMIT__":
        doc["reviewDecision"] = review_decision
    if head_ref_oid != "__OMIT__":
        doc["headRefOid"] = head_ref_oid
    return json.dumps(doc)


def threads_doc(unresolved=0, resolved=0, total=None, raw_nodes=None):
    """Build the reviewThreads GraphQL response the oracle enumerates (#263 Piece
    A). `unresolved`/`resolved` set the node counts; `total` overrides totalCount
    (default = node count). Setting total > node count simulates a paginated-
    TRUNCATED list the oracle must treat as fail-closed.

    raw_nodes (fail-closed cases): when supplied, use this exact list as `nodes`
    verbatim -- so a caller can inject a node whose isResolved is null / missing /
    a string, or a null node element. Every such node is NOT provably resolved and
    MUST be counted as unresolved (block), never silently read as resolved."""
    if raw_nodes is not None:
        nodes = raw_nodes
    else:
        nodes = ([{"isResolved": False}] * unresolved) + ([{"isResolved": True}] * resolved)
    tc = len(nodes) if total is None else total
    return json.dumps({"data": {"repository": {"pullRequest": {
        "reviewThreads": {"totalCount": tc, "nodes": nodes}}}}})


# Default threads fixture for the existing PASS cases: zero threads, none unresolved.
DEFAULT_THREADS = threads_doc(unresolved=0, resolved=0)


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


def diag_fixture(*, state="OPEN", mss="BLOCKED", mergeable="MERGEABLE",
                 review_decision="__OMIT__", base="main", is_draft=False,
                 contexts=None):
    """PR-view JSON for --diagnose (#275). `contexts` = rollup entries
    (checkrun()/statusctx() dicts)."""
    doc = {"state": state, "mergeStateStatus": mss, "mergeable": mergeable,
           "baseRefName": base, "headRefName": "feature", "isDraft": is_draft,
           "statusCheckRollup": list(contexts or [])}
    if review_decision != "__OMIT__":
        doc["reviewDecision"] = review_decision
    return json.dumps(doc)


def prot_fixture(*, required_contexts=None, strict=False, conv_res=False,
                 linear=False, signatures=False):
    """branches/<base>/protection JSON for --diagnose (#275)."""
    return json.dumps({
        "required_status_checks": {"strict": strict,
                                   "contexts": list(required_contexts or [])},
        "required_conversation_resolution": {"enabled": conv_res},
        "required_linear_history": {"enabled": linear},
        "required_signatures": {"enabled": signatures},
    })


def run(args, *, fixture_json, gh_fail=False, unreplied_findings=0,
        unreplied_fail=False, unreplied_missing=False, unreplied_raw=None,
        unreplied_fail_until=0, codoki_ack_verdict="no-summary",
        codoki_ack_fail=False, codoki_ack_missing=False,
        threads_json="__DEFAULT__", threads_fail=False, protection=None):
    """Invoke the oracle with stubbed gh + pr-unreplied-comments.sh + gh-react.sh.
    Returns (exit_code, stdout, stderr, argv) where argv is the recorded helper
    argv content (one line per invocation, read back from the log) -- used to
    assert the oracle passes --allow-stale and to drive the retry cases.
    UNREPLIED_FAIL_UNTIL=N makes the stub exit 2 on its first N invocations
    (counted via a counter file) then succeed, exercising the bounded retry.

    The Codoki-root-ack gate (#234, FULL mode only) reads gh-react.sh, also
    stubbed under $HOME/.claude/scripts. codoki_ack_verdict drives the stub's
    'CODOKI-ACK: <verdict>' line (default 'no-summary' so pre-#234 cases still
    PASS); codoki_ack_fail=True makes it exit 2 (tool failure -> oracle BLOCKs);
    codoki_ack_missing=True omits the stub entirely (missing -> oracle BLOCKs)."""
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
                "# Route by subcommand token: `gh api graphql` (#263 thread gate),\n"
                "# `gh repo view`, else `gh pr view`.\n"
                "for a in \"$@\"; do case \"$a\" in\n"
                "  graphql)\n"
                "    if [ -n \"${THREADS_FAIL:-}\" ]; then echo 'gh api graphql: simulated failure' >&2; exit 1; fi\n"
                "    printf '%s' \"${FIXTURE_THREADS:-}\"; exit 0;;\n"
                "  repo) echo 'owner/repo'; exit 0;;\n"
                "  *protection)\n"
                "    # `gh api repos/.../branches/<base>/protection` (#275 --diagnose). Empty\n"
                "    # FIXTURE_PROTECTION simulates a 403 (no admin scope) so the degradation\n"
                "    # path is exercised; the main oracle never calls this route.\n"
                "    if [ -n \"${FIXTURE_PROTECTION:-}\" ]; then printf '%s' \"$FIXTURE_PROTECTION\"; exit 0; fi\n"
                "    echo 'gh: HTTP 403 (branch protection needs admin)' >&2; exit 1;;\n"
                "esac; done\n"
                "# `gh pr view <pr> --repo <repo> --json statusCheckRollup,...,headRefOid`\n"
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

        # Stub gh-react.sh (the #234 Codoki-root-ack reader), unless simulating it
        # missing. Emits a 'CODOKI-ACK: <verdict>' line and exits 0; CODOKI_ACK_FAIL
        # makes it exit 2 (tool failure -> oracle must BLOCK).
        if not codoki_ack_missing:
            reactor = os.path.join(helper_dir, "gh-react.sh")
            with open(reactor, "w") as f:
                f.write(
                    "#!/usr/bin/env bash\n"
                    "set -eu\n"
                    "if [ -n \"${CODOKI_ACK_FAIL:-}\" ]; then\n"
                    "  echo 'gh-react: simulated failure -- ack UNVERIFIABLE' >&2; exit 2\n"
                    "fi\n"
                    "echo \"CODOKI-ACK: ${CODOKI_ACK_VERDICT:-no-summary} -- stub\"\n"
                )
            os.chmod(reactor, 0o755)

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["HOME"] = home
        env["FIXTURE_JSON"] = fixture_json
        env["FIXTURE_THREADS"] = DEFAULT_THREADS if threads_json == "__DEFAULT__" else threads_json
        env.pop("FIXTURE_PROTECTION", None)
        if protection is not None:
            env["FIXTURE_PROTECTION"] = protection
        env.pop("THREADS_FAIL", None)
        if threads_fail:
            env["THREADS_FAIL"] = "1"
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
        env["CODOKI_ACK_VERDICT"] = codoki_ack_verdict
        # Clear any inherited CODOKI_ACK_FAIL first, then set ONLY when this case
        # asks for it -- otherwise a caller's env var would fail every case (CR #253).
        env.pop("CODOKI_ACK_FAIL", None)
        if codoki_ack_fail:
            env["CODOKI_ACK_FAIL"] = "1"

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

    print("== #234 FULL MODE: Codoki-root-ack gate ==")
    # No Codoki summary -> ack gate PASSES (never fail-closed on absence).
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN,
                      unreplied_findings=0, codoki_ack_verdict="no-summary")
    check("no Codoki summary + all green -> exit 0 (ack gate passes on absence)", rc == 0)
    # Summary present but UNACKED -> BLOCK.
    rc, _, err, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN,
                        unreplied_findings=0, codoki_ack_verdict="unacked")
    check("Codoki summary present but UNACKED -> exit 2 (block)", rc == 2)
    check("BLOCK message names the unmet Codoki ack",
          "codoki" in err.lower() and "ack" in err.lower())
    # Non-bot ack present (acked) -> PASS.
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN,
                      unreplied_findings=0, codoki_ack_verdict="acked")
    check("Codoki summary ACKED (non-bot +1/-1) -> exit 0 (pass)", rc == 0)
    # gh-react tool failure -> BLOCK (fail closed; never a silent skip).
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN,
                      unreplied_findings=0, codoki_ack_fail=True)
    check("gh-react ack-read failure -> exit 2 (fail closed)", rc == 2)
    # gh-react missing -> BLOCK (cannot verify the ack).
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN,
                      unreplied_findings=0, codoki_ack_missing=True)
    check("gh-react missing -> exit 2 (ack unverifiable, fail closed)", rc == 2)
    # --codoki-only must NOT run the ack gate: an UNACKED verdict is irrelevant there
    # (settlement mode is a pure check-rollup signal). Codoki check green -> PASS.
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"],
                      fixture_json=rollup(checkrun("Codoki PR Review", "COMPLETED", "SUCCESS")),
                      codoki_ack_verdict="unacked")
    check("--codoki-only ignores the ack gate (UNACKED verdict) -> exit 0", rc == 0)
    # --codoki-only also PASSes with gh-react missing (proves the ack gate is skipped).
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"],
                      fixture_json=rollup(checkrun("Codoki PR Review", "COMPLETED", "SUCCESS")),
                      codoki_ack_missing=True)
    check("--codoki-only skips the ack gate (gh-react missing still PASS)", rc == 0)

    print("== #263 Piece A: review-thread enumeration gate (isResolved) ==")
    # Baseline: all green, 0 unresolved threads -> PASS.
    rc, out, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN,
                        unreplied_findings=0, threads_json=threads_doc(unresolved=0, resolved=2))
    check("all green + 0 unresolved threads (2 resolved) -> exit 0", rc == 0)
    # ONE unresolved thread -> BLOCK, even with everything else green.
    rc, _, err, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN,
                        unreplied_findings=0, threads_json=threads_doc(unresolved=1, resolved=2))
    check("1 unresolved review thread -> exit 2 (block)", rc == 2)
    check("BLOCK message names the unresolved thread(s)",
          "unresolved" in err.lower() and "thread" in err.lower())
    # Many unresolved -> BLOCK.
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN,
                      unreplied_findings=0, threads_json=threads_doc(unresolved=5))
    check("5 unresolved threads -> exit 2 (block)", rc == 2)
    # FAIL CLOSED: the threads GraphQL query errors -> BLOCK.
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN,
                      unreplied_findings=0, threads_fail=True)
    check("threads GraphQL query fails -> exit 2 (fail closed)", rc == 2)
    # FAIL CLOSED: malformed threads JSON -> BLOCK.
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN,
                      unreplied_findings=0, threads_json="not json{")
    check("malformed threads JSON -> exit 2 (fail closed)", rc == 2)
    # FAIL CLOSED: paginated-TRUNCATED list (totalCount > nodes fetched) -> BLOCK,
    # even when every fetched node is resolved (the unfetched ones are unknown).
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0,
                      threads_json=threads_doc(unresolved=0, resolved=3, total=150))
    check("truncated thread list (totalCount 150 > 3 nodes) -> exit 2 (fail closed)", rc == 2)
    # FAIL CLOSED on a node that is NOT PROVABLY resolved. isResolved is Boolean! on
    # the happy path, but reviewThreads.nodes ELEMENTS are nullable, so a partial
    # GraphQL error can null a thread while gh still returns `data`. Anything other
    # than a literal isResolved==true MUST block, never read as resolved (hostile
    # review FINDING 1: the inverse `== false` match failed OPEN here).
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0,
                      threads_json=threads_doc(raw_nodes=[{"isResolved": None}]))
    check("thread isResolved=null -> exit 2 (not provably resolved, fail closed)", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0,
                      threads_json=threads_doc(raw_nodes=[{}]))
    check("thread with isResolved MISSING -> exit 2 (fail closed)", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0,
                      threads_json=threads_doc(raw_nodes=[None]))
    check("NULL node element -> exit 2 (fail closed)", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0,
                      threads_json=threads_doc(raw_nodes=[{"isResolved": "false"}]))
    check("thread isResolved as STRING 'false' -> exit 2 (fail closed)", rc == 2)
    # Positive control: provably-resolved (literal true) nodes still PASS.
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0,
                      threads_json=threads_doc(raw_nodes=[{"isResolved": True}, {"isResolved": True}]))
    check("all nodes isResolved=true -> exit 0 (provably resolved)", rc == 0)
    # #265 review (CR Critical / Copilot): totalCount must be a PRESENT non-negative
    # integer. A null/missing totalCount must NOT default to 0 and defeat the
    # truncation guard (a partial GraphQL error can null it while nodes look valid).
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0,
                      threads_json='{"data":{"repository":{"pullRequest":{"reviewThreads":'
                                   '{"totalCount":null,"nodes":[{"isResolved":true}]}}}}}')
    check("null totalCount + resolved node -> exit 2 (truncation guard not defeated)", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0,
                      threads_json='{"data":{"repository":{"pullRequest":{"reviewThreads":'
                                   '{"nodes":[{"isResolved":true}]}}}}}')
    check("missing totalCount -> exit 2 (fail closed)", rc == 2)
    # totalCount < node count is impossible in a well-formed response -> MALFORMED.
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0,
                      threads_json=threads_doc(resolved=3, total=1))
    check("totalCount(1) < nodes(3) impossible -> exit 2 (fail closed)", rc == 2)
    # A GraphQL partial-error payload (non-empty top-level .errors) alongside data
    # -> BLOCK, even if the data sub-object looks complete.
    rc, _, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0,
                      threads_json='{"errors":[{"message":"rate limited"}],"data":{"repository":'
                                   '{"pullRequest":{"reviewThreads":{"totalCount":1,'
                                   '"nodes":[{"isResolved":true}]}}}}}')
    check("GraphQL .errors present (partial error) -> exit 2 (fail closed)", rc == 2)
    # --codoki-only must NOT run the thread gate (settlement is a pure check signal):
    # unresolved threads are irrelevant there.
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"],
                      fixture_json=rollup(checkrun("Codoki PR Review", "COMPLETED", "SUCCESS")),
                      threads_json=threads_doc(unresolved=9))
    check("--codoki-only ignores the thread gate (9 unresolved) -> exit 0", rc == 0)

    print("== #263 Piece A: emit validated headRefOid on PASS ==")
    # PASS prints a parseable headRefOid=<sha> line, and it is the validated SHA.
    rc, out, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0)
    check("PASS emits a parseable headRefOid=<sha> on stdout", rc == 0 and f"headRefOid={DEFAULT_SHA}" in out)
    # A BLOCK (unresolved thread) must NOT emit a headRefOid (no attestation on a non-PASS).
    rc, out, err, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN,
                          unreplied_findings=0, threads_json=threads_doc(unresolved=1))
    check("BLOCK does NOT emit headRefOid=", rc == 2 and "headRefOid=" not in (out + err))
    # FAIL CLOSED: an otherwise-green PASS with an UNREADABLE head SHA must BLOCK
    # (a PASS with no pinnable SHA is useless downstream).
    rc, _, _, _ = run(["1", "owner/repo"],
                      fixture_json=rollup(checkrun("ci", "COMPLETED", "SUCCESS"), head_ref_oid=""),
                      unreplied_findings=0)
    check("empty headRefOid + all green -> exit 2 (cannot attest SHA, fail closed)", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"],
                      fixture_json=rollup(checkrun("ci", "COMPLETED", "SUCCESS"), head_ref_oid=None),
                      unreplied_findings=0)
    check("null headRefOid + all green -> exit 2 (fail closed)", rc == 2)
    rc, _, _, _ = run(["1", "owner/repo"],
                      fixture_json=rollup(checkrun("ci", "COMPLETED", "SUCCESS"), head_ref_oid="__OMIT__"),
                      unreplied_findings=0)
    check("absent headRefOid + all green -> exit 2 (fail closed)", rc == 2)
    # #265 review (CR Major): validate the ENTIRE SHA, not one line. grep is line-
    # oriented, so "<40hex>\nforged" would pass a line-anchored match and inject
    # extra output into the attestation. A multi-line head SHA must BLOCK.
    rc, out, _, _ = run(["1", "owner/repo"],
                        fixture_json=rollup(checkrun("ci", "COMPLETED", "SUCCESS"),
                                            head_ref_oid=DEFAULT_SHA + "\nforged"),
                        unreplied_findings=0)
    check("multi-line head_sha (40hex + newline + junk) -> exit 2 (whole-value validation)",
          rc == 2 and "forged" not in out)
    # --codoki-only does NOT require/emit headRefOid (settlement mode).
    rc, out, _, _ = run(["1", "owner/repo", "--codoki-only"],
                        fixture_json=rollup(checkrun("Codoki PR Review", "COMPLETED", "SUCCESS"),
                                            head_ref_oid="__OMIT__"))
    check("--codoki-only PASSes without headRefOid (settlement mode) -> exit 0", rc == 0)

    print("== DIAGNOSE (#275) ==")
    # AC (c): REVIEW_REQUIRED -> names the review requirement.
    rc, out, err, _ = run(["7", "owner/repo", "--diagnose"],
                          fixture_json=diag_fixture(mss="BLOCKED", review_decision="REVIEW_REQUIRED",
                                                    contexts=[checkrun("ci", "COMPLETED", "SUCCESS")]),
                          protection=prot_fixture(required_contexts=["ci"]),
                          threads_json=threads_doc(resolved=1))
    o = out + err
    check("#275(c): REVIEW_REQUIRED -> REASON names review requirement, exit 2",
          rc == 2 and "REVIEW_REQUIRED" in o and "REASON:" in o)

    # AC (a): unresolved conversation (require_conversation_resolution) -> names it + count.
    rc, out, err, _ = run(["7", "owner/repo", "--diagnose"],
                          fixture_json=diag_fixture(mss="BLOCKED",
                                                    contexts=[checkrun("ci", "COMPLETED", "SUCCESS")]),
                          protection=prot_fixture(required_contexts=["ci"], conv_res=True),
                          threads_json=threads_doc(unresolved=2, resolved=1))
    o = out + err
    check("#275(a): unresolved conversation -> REASON with count 2, exit 2",
          rc == 2 and "unresolved review conversation(s): 2" in o)

    # AC (b): a required check that never reported -> MISSING.
    rc, out, err, _ = run(["7", "owner/repo", "--diagnose"],
                          fixture_json=diag_fixture(mss="BLOCKED",
                                                    contexts=[checkrun("ci", "COMPLETED", "SUCCESS")]),
                          protection=prot_fixture(required_contexts=["ci", "required-e2e"]),
                          threads_json=threads_doc(resolved=1))
    o = out + err
    check("#275(b): missing required check -> REASON names it MISSING, exit 2",
          rc == 2 and "required-e2e" in o and "MISSING" in o)

    # A required check present but FAILING -> not-success reason.
    rc, out, err, _ = run(["7", "owner/repo", "--diagnose"],
                          fixture_json=diag_fixture(mss="BLOCKED",
                                                    contexts=[checkrun("ci", "COMPLETED", "FAILURE")]),
                          protection=prot_fixture(required_contexts=["ci"]),
                          threads_json=threads_doc(resolved=1))
    o = out + err
    check("#275: failing required check -> REASON 'ci' not-success, exit 2",
          rc == 2 and "'ci'" in o and "FAILURE" in o)

    # Behind base (strict).
    rc, out, err, _ = run(["7", "owner/repo", "--diagnose"],
                          fixture_json=diag_fixture(mss="BEHIND",
                                                    contexts=[checkrun("ci", "COMPLETED", "SUCCESS")]),
                          protection=prot_fixture(required_contexts=["ci"], strict=True),
                          threads_json=threads_doc(resolved=1))
    check("#275: behind base -> REASON behind, exit 2", rc == 2 and "BEHIND base" in (out + err))

    # Codoki root ack unmet.
    rc, out, err, _ = run(["7", "owner/repo", "--diagnose"],
                          fixture_json=diag_fixture(mss="BLOCKED",
                                                    contexts=[checkrun("ci", "COMPLETED", "SUCCESS")]),
                          protection=prot_fixture(required_contexts=["ci"]),
                          threads_json=threads_doc(resolved=1),
                          codoki_ack_verdict="unacked")
    check("#275: Codoki ack unmet -> REASON ack UNMET, exit 2",
          rc == 2 and "Codoki root-summary ack is UNMET" in (out + err))

    # Codoki ack UNVERIFIABLE (reader failed) -> honest NOTE, not a silent skip.
    rc, out, err, _ = run(["7", "owner/repo", "--diagnose"],
                          fixture_json=diag_fixture(state="OPEN", mss="CLEAN", review_decision="APPROVED",
                                                    contexts=[checkrun("ci", "COMPLETED", "SUCCESS")]),
                          protection=prot_fixture(required_contexts=["ci"]),
                          threads_json=threads_doc(resolved=1), codoki_ack_fail=True)
    check("#275: Codoki ack unverifiable -> NOTE (not silent), exit 0",
          rc == 0 and "could not be verified" in (out + err))

    # Draft.
    rc, out, err, _ = run(["7", "owner/repo", "--diagnose"],
                          fixture_json=diag_fixture(mss="DRAFT", is_draft=True,
                                                    contexts=[checkrun("ci", "COMPLETED", "SUCCESS")]),
                          protection=prot_fixture(required_contexts=["ci"]),
                          threads_json=threads_doc(resolved=1))
    check("#275: draft PR -> REASON DRAFT, exit 2", rc == 2 and "DRAFT" in (out + err))

    # Clean/mergeable -> exit 0 (no fabricated reason).
    rc, out, err, _ = run(["7", "owner/repo", "--diagnose"],
                          fixture_json=diag_fixture(state="OPEN", mss="CLEAN", review_decision="APPROVED",
                                                    contexts=[checkrun("ci", "COMPLETED", "SUCCESS")]),
                          protection=prot_fixture(required_contexts=["ci"]),
                          threads_json=threads_doc(resolved=1))
    check("#275: clean/mergeable -> exit 0, MERGEABLE", rc == 0 and "MERGEABLE" in (out + err))

    # Merged short-circuit.
    rc, out, err, _ = run(["7", "owner/repo", "--diagnose"], fixture_json=diag_fixture(state="MERGED"))
    check("#275: merged PR -> exit 0 short-circuit", rc == 0 and "MERGED" in (out + err))

    # UNKNOWN merge state with no other reason -> INDETERMINATE (not a fabricated block).
    rc, out, err, _ = run(["7", "owner/repo", "--diagnose"],
                          fixture_json=diag_fixture(mss="UNKNOWN", review_decision="APPROVED",
                                                    contexts=[checkrun("ci", "COMPLETED", "SUCCESS")]),
                          protection=prot_fixture(required_contexts=["ci"]),
                          threads_json=threads_doc(resolved=1))
    check("#275: UNKNOWN merge state -> INDETERMINATE, exit 2 (no fabricated reason)",
          rc == 2 and "INDETERMINATE" in (out + err) and "REASON:" not in (out + err))

    # Protection unreadable (403) but an unresolved thread exists -> still diagnosed.
    rc, out, err, _ = run(["7", "owner/repo", "--diagnose"],
                          fixture_json=diag_fixture(mss="BLOCKED",
                                                    contexts=[checkrun("ci", "COMPLETED", "SUCCESS")]),
                          protection=None,
                          threads_json=threads_doc(unresolved=1))
    o = out + err
    check("#275: protection 403 -> NOTE + still flags unresolved thread, exit 2",
          rc == 2 and "not readable" in o and "unresolved review conversation(s): 1" in o)

    # Core PR fetch failure -> fail-closed.
    rc, _, _, _ = run(["7", "owner/repo", "--diagnose"], fixture_json=diag_fixture(), gh_fail=True)
    check("#275: gh pr view failure -> exit 2 (fail closed)", rc == 2)

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
