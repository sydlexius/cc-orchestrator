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


def rollup(*contexts, review_decision="__OMIT__", head_ref_oid=DEFAULT_SHA,
           merge_state="CLEAN", base_ref="main"):
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
    # merge_state (#334): GitHub's AGGREGATE verdict, now on the GATING path. Defaults to
    # CLEAN so every pre-existing PASS fixture keeps passing; pass "__OMIT__" to drop the
    # field (unreadable -> BLOCK) or any other value to exercise the new gate.
    if merge_state != "__OMIT__":
        doc["mergeStateStatus"] = merge_state
    # base_ref (#375): FULL mode needs the PR's base to ask GitHub what that ref
    # actually requires. Defaults to "main" so every pre-existing fixture keeps its
    # behavior; pass a feature-branch name to exercise the non-default-base case, or
    # "__OMIT__" to drop the field (unreadable base -> BLOCK).
    if base_ref != "__OMIT__":
        doc["baseRefName"] = base_ref
    return json.dumps(doc)


def rules_doc(*, contexts=None, extra_types=None):
    """A `rules/branches/<ref>` response.

    contexts: required status-check contexts, emitted as a `required_status_checks`
    rule. Pass None for a ref that requires NO checks.

    extra_types: other rule types present on the ref. This exists for the F4 case in
    DESIGN-expected-check-set.md, and it is the whole reason the fallback predicate
    cannot be "does the base have rules": stillwater returns exactly one rule for a
    non-default ref -- `copilot_code_review`, from an auto-review ruleset, with ZERO
    required checks. A predicate keyed on rule PRESENCE would call that base governed,
    reconcile against an empty required set, and pass the identical false green.
    """
    out = []
    for t in (extra_types or []):
        out.append({"type": t, "ruleset_id": 1})
    if contexts is not None:
        out.append({
            "type": "required_status_checks",
            "ruleset_id": 2,
            "parameters": {"required_status_checks":
                           [{"context": c} for c in contexts]},
        })
    return json.dumps(out)


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


def checkrun(name, status, conclusion, started_at="__OMIT__", completed_at="__OMIT__"):
    """A CheckRun rollup entry.

    started_at / completed_at (#301 Part 1) drive the latest-per-name reduction. Both
    default to OMITTED so every pre-existing fixture is unchanged - and note that an
    omitted timestamp reads as NULL, which the reduction sorts as NEWEST (fail-closed:
    a QUEUED re-run with no startedAt must beat a stale SUCCESS). Pass an ISO string to
    order runs explicitly, or None to emit an explicit JSON null."""
    d = {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}
    if started_at != "__OMIT__":
        d["startedAt"] = started_at
    if completed_at != "__OMIT__":
        d["completedAt"] = completed_at
    return d


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
        threads_json="__DEFAULT__", threads_fail=False, protection=None, comments=None,
        comments_fail=False, reviews=None, reviews_fail=False,
        rules_main=None, rules_base=None, rules_fail=False, default_branch_fail=False,
        rules_main_fail=False, default_branch=None):
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
                "  repo)\n"
                "    # `gh repo view` serves TWO callers now: the repo-slug resolution that\n"
                "    # predates this stub, and (#375) the default-branch lookup the\n"
                "    # expected-set fallback needs. Discriminate on the --json field, or the\n"
                "    # slug answer is handed to a caller parsing a branch name.\n"
                "    case \"$*\" in\n"
                "      *defaultBranchRef*)\n"
                "        # DEFAULT_BRANCH_FAIL fails ONLY this route. Without a knob here the\n"
                "        # fail-open in the fallback path was untestable, and it shipped: an\n"
                "        # unreadable default branch skipped reconciliation entirely and PASSed\n"
                "        # with no output at all.\n"
                "        #\n"
                "        # THE ERROR BODY GOES TO STDOUT, not stderr. Real `gh ... --jq` writes\n"
                "        # its error payload to STDOUT and exits nonzero (the same contract the\n"
                "        # protection route models below). An earlier stub wrote this failure to\n"
                "        # STDERR -- kinder than reality in the one direction that mattered, so a\n"
                "        # caller using `|| echo \"\"` captured the payload as a BRANCH NAME and the\n"
                "        # harness could not see it. Fidelity here is what gives the case teeth.\n"
                "        if [ -n \"${DEFAULT_BRANCH_FAIL:-}\" ]; then\n"
                "          printf '%s' '{\"message\":\"Not Found\",\"status\":\"404\"}'; exit 1\n"
                "        fi\n"
                "        echo \"${FIXTURE_DEFAULT_BRANCH:-main}\"; exit 0;;\n"
                "    esac\n"
                "    echo 'owner/repo'; exit 0;;\n"
                "  *comments)\n"
                "    # `gh api repos/.../issues/<pr>/comments` (#237 @codoki-trigger detector).\n"
                "    # COMMENTS_FAIL fails ONLY this route (gh pr view still succeeds) to test the\n"
                "    # trigger detector's fail-toward-not-triggered posture in isolation (#277 CR).\n"
                "    if [ -n \"${COMMENTS_FAIL:-}\" ]; then echo 'gh: simulated comments-API failure' >&2; exit 1; fi\n"
                "    printf '%s' \"${FIXTURE_COMMENTS:-[]}\"; exit 0;;\n"
                "  *reviews)\n"
                "    # `gh api repos/.../pulls/<pr>/reviews` (#315 disambiguation + #301 Part 2\n"
                "    # coverage advisory). REVIEWS_FAIL fails ONLY this route, so the fail-OPEN\n"
                "    # posture can be proved in isolation: an unreadable reviews API must leave\n"
                "    # the exit code identical, never flip a PASS to a BLOCK.\n"
                "    if [ -n \"${REVIEWS_FAIL:-}\" ]; then echo 'gh: simulated reviews-API failure' >&2; exit 1; fi\n"
                "    printf '%s' \"${FIXTURE_REVIEWS:-[]}\"; exit 0;;\n"
                "  */rules/branches/*)\n"
                "    # `gh api repos/.../rules/branches/<ref>` (#375). THE authority for the\n"
                "    # expected check set, and unlike branches/<ref>/protection it needs no\n"
                "    # admin scope (measured: the legacy route 404s on this repo while this one\n"
                "    # answers). FIXTURE_RULES_MAIN serves the default branch; FIXTURE_RULES_BASE\n"
                "    # serves any other ref, so the stacked case (base has no required checks,\n"
                "    # main does) is expressible. RULES_FAIL fails the route to prove the\n"
                "    # unreadable path BLOCKS rather than degrading to a pass.\n"
                "    if [ -n \"${RULES_FAIL:-}\" ]; then echo 'gh: simulated rules-API failure' >&2; exit 1; fi\n"
                "    case \"$a\" in\n"
                "      *\"/rules/branches/${FIXTURE_DEFAULT_BRANCH:-main}\")\n"
                "        # RULES_MAIN_FAIL fails ONLY the DEFAULT branch's route. RULES_FAIL kills\n"
                "        # every rules read at once and so exits at the BASE read, leaving the\n"
                "        # default-branch unreadable BLOCK unreachable by any fixture -- neutering\n"
                "        # that exit into a silent skip failed ZERO tests. Per-route is what\n"
                "        # separates the four unreadable paths from each other.\n"
                "        if [ -n \"${RULES_MAIN_FAIL:-}\" ]; then echo 'gh: simulated rules-API failure (default branch)' >&2; exit 1; fi\n"
                "        printf '%s' \"${FIXTURE_RULES_MAIN:-[]}\"; exit 0;;\n"
                "      *) printf '%s' \"${FIXTURE_RULES_BASE:-[]}\"; exit 0;;\n"
                "    esac;;\n"
                "  *protection)\n"
                "    # `gh api repos/.../branches/<base>/protection`. TWO callers: --diagnose\n"
                "    # (#275, no --jq: it wants the raw document) and the #375 expected-set union\n"
                "    # (--jq: it wants extracted context names). Empty FIXTURE_PROTECTION\n"
                "    # simulates a 403 (no admin scope) so the degradation path is exercised.\n"
                "    #\n"
                "    # THE SUCCESS PATH MUST APPLY --jq. A stub that printed the fixture VERBATIM\n"
                "    # regardless of the filter made the entire LEGACY half of the union\n"
                "    # untestable: deleting either half of the filter, or pointing it at the wrong\n"
                "    # ref, failed ZERO tests, and the case that looked like coverage passed only\n"
                "    # because the context name appeared as a SUBSTRING of the dumped JSON blob.\n"
                "    # That is the same 'JSON document as a check name' defect the 404 path below\n"
                "    # models, reached through the success path. Shell out to real jq so the\n"
                "    # filter under test is the filter that runs -- and so a BROKEN filter exits\n"
                "    # nonzero here exactly as `gh --jq` does, which is what makes the\n"
                "    # unparseable-body cases constructible at all.\n"
                "    if [ -n \"${FIXTURE_PROTECTION:-}\" ]; then\n"
                "      _f=''; _next=0\n"
                "      for _a in \"$@\"; do\n"
                "        if [ \"$_next\" = 1 ]; then _f=\"$_a\"; break; fi\n"
                "        case \"$_a\" in --jq|-q) _next=1;; --jq=*) _f=\"${_a#--jq=}\"; break;; esac\n"
                "      done\n"
                "      if [ -n \"$_f\" ]; then printf '%s' \"$FIXTURE_PROTECTION\" | jq -r \"$_f\"; exit $?; fi\n"
                "      printf '%s' \"$FIXTURE_PROTECTION\"; exit 0\n"
                "    fi\n"
                "    # MODELS REAL GH, which the earlier empty-stdout stub did not: on a 404\n"
                "    # `gh api --jq` writes the ERROR BODY to STDOUT and exits 1. The stub used\n"
                "    # to emit nothing, so a caller keeping stdout past a nonzero exit looked\n"
                "    # correct here and unioned a 404 payload in as a required context name live.\n"
                "    printf '%s' '{\"message\":\"Branch not protected\",\"status\":\"404\"}'\n"
                "    exit 1;;\n"
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
        if rules_main is not None:
            env["FIXTURE_RULES_MAIN"] = rules_main
        if rules_base is not None:
            env["FIXTURE_RULES_BASE"] = rules_base
        if rules_fail:
            env["RULES_FAIL"] = "1"
        if rules_main_fail:
            env["RULES_MAIN_FAIL"] = "1"
        if default_branch_fail:
            env["DEFAULT_BRANCH_FAIL"] = "1"
        # The default branch is a FIXTURE, not a constant. Every case defaulting to
        # "main" meant hard-coding default_branch="main" and never calling gh at all
        # passed the whole suite: the value was proven to FAIL correctly and never
        # proven to be READ.
        env.pop("FIXTURE_DEFAULT_BRANCH", None)
        if default_branch is not None:
            env["FIXTURE_DEFAULT_BRANCH"] = default_branch
        env.pop("FIXTURE_COMMENTS", None)
        if comments is not None:
            env["FIXTURE_COMMENTS"] = comments
        env.pop("COMMENTS_FAIL", None)
        if comments_fail:
            env["COMMENTS_FAIL"] = "1"
        env.pop("FIXTURE_REVIEWS", None)
        if reviews is not None:
            env["FIXTURE_REVIEWS"] = reviews
        env.pop("REVIEWS_FAIL", None)
        if reviews_fail:
            env["REVIEWS_FAIL"] = "1"
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

    # #289: the BLOCK must NAME THE CLEARING ACTION. This is the half of #289 that actually
    # prevents the stillwater #2424 override: a review-body finding has no inline thread to
    # resolve and clears only by acking the review BY ID, and that channel was invisible from the
    # block message -- so the maintainer replied "fixed in <sha>" (no id), the gate never cleared,
    # and they merged OVER the oracle. A fail-closed gate that cannot tell you how to satisfy it
    # trains the lead to override it. (This message had ZERO harness coverage until now: it could
    # be reverted and every test stayed green.)
    rc, out, err, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=2)
    both = out + err
    check("#289: a review-body BLOCK names the clearing action (reply-comment.sh --review)",
          rc == 2 and "reply-comment.sh --review" in both)
    check("#289: the BLOCK says the review id is the ack token (a bare reply does NOT clear)",
          rc == 2 and "review id" in both.lower() and "does NOT clear" in both)

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

    print("== CODOKI-GATE MODE (#237, auto-review-off aware) ==")
    no_codoki = rollup(checkrun("ci", "COMPLETED", "SUCCESS"))  # no Codoki check present
    C_TRIGGER = '[{"user":{"login":"sydlexius"},"body":"@codoki review please"}]'
    C_BOTQUOTE = ('[{"user":{"login":"codoki-pr-intelligence[bot]"},'
                  '"body":"Reply with @codoki to request another review."}]')
    # THE HANG FIX: missing Codoki check + no @codoki trigger -> NOT expected -> exit 0
    # (auto-review off; pr-watch must not wait forever).
    rc, out, _, _ = run(["1", "owner/repo", "--codoki-gate"], fixture_json=no_codoki, comments="[]")
    check("--codoki-gate: missing check + no trigger -> exit 0 (not expected, no hang)",
          rc == 0 and "not expected" in out.lower())
    # Missing check but a NON-bot @codoki trigger present -> expected -> exit 2 (wait).
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-gate"], fixture_json=no_codoki, comments=C_TRIGGER)
    check("--codoki-gate: missing check + @codoki trigger -> exit 2 (expected, wait)", rc == 2)
    # @codoki appears ONLY in Codoki's own comment -> not a trigger -> exit 0.
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-gate"], fixture_json=no_codoki, comments=C_BOTQUOTE)
    check("--codoki-gate: @codoki only in Codoki's own comment -> exit 0 (bot quote excluded)", rc == 0)
    # An email/domain like foo@codoki.example.com must NOT be read as a trigger.
    C_EMAIL = '[{"user":{"login":"sydlexius"},"body":"ping me at ops@codoki.example.com about this"}]'
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-gate"], fixture_json=no_codoki, comments=C_EMAIL)
    check("--codoki-gate: @codoki.<domain> email is NOT a trigger -> exit 0", rc == 0)
    # Codoki check present + settled OK -> exit 0 (same as --codoki-only).
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-gate"],
                   fixture_json=rollup(checkrun("Codoki PR Review", "COMPLETED", "SUCCESS")))
    check("--codoki-gate: Codoki settled OK -> exit 0", rc == 0)
    # Codoki check present but IN_PROGRESS -> exit 2 (wait), even with no trigger.
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-gate"],
                   fixture_json=rollup(checkrun("Codoki PR Review", "IN_PROGRESS", None)), comments="[]")
    check("--codoki-gate: Codoki check present but IN_PROGRESS -> exit 2 (wait)", rc == 2)
    # Codoki check present + FAILURE -> exit 2.
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-gate"],
                   fixture_json=rollup(checkrun("Codoki PR Review", "COMPLETED", "FAILURE")))
    check("--codoki-gate: Codoki check FAILURE -> exit 2", rc == 2)
    # REGRESSION: --codoki-only stays STRICT (missing -> exit 2), unchanged by #237.
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"], fixture_json=no_codoki, comments=C_TRIGGER)
    check("regression: --codoki-only missing check -> exit 2 (strict, ignores trigger)", rc == 2)
    # #277 CR: an ISOLATED comments-API failure (gh pr view still OK) must classify as
    # NOT-EXPECTED (the trigger detector fails toward not-triggered - liveness), exit 0.
    rc, out, _, _ = run(["1", "owner/repo", "--codoki-gate"], fixture_json=no_codoki, comments_fail=True)
    check("#277: --codoki-gate comments-API failure -> exit 0 (fail-toward-satisfied, no hang)",
          rc == 0 and "not expected" in out.lower())
    # ...and the STRICT --codoki-only is unaffected by a comments failure (it never fetches
    # comments): a missing check still BLOCKs, so the merge-gate posture stays fail-closed.
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only"], fixture_json=no_codoki, comments_fail=True)
    check("#277: --codoki-only missing check + comments-fail -> exit 2 (unaffected, fail-closed)", rc == 2)

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

    print("== #301 Part 1: latest-per-name CheckRun reduction ==")
    # GitHub CANCELS in-flight duplicates on every push, so a superseded CANCELLED run
    # sits in the rollup beside the LATER same-name run that SUCCEEDED. Evaluating the
    # list flat blocked on the corpse - normal push behavior, not an edge case.
    #
    # This RELAXES the gate, which for a fail-closed oracle is the dangerous direction,
    # so cases (c)(d)(e) below are the mandatory false-green traps: each one must still
    # BLOCK, and each fails if the sort keys are reverted.
    T1, T2 = "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
    # (a) earlier CANCELLED + later SUCCESS, same name -> PASS (the defect being fixed).
    rc, out, _, _ = run(["1", "owner/repo"],
                        fixture_json=rollup(checkrun("ci", "COMPLETED", "CANCELLED", started_at=T1),
                                            checkrun("ci", "COMPLETED", "SUCCESS", started_at=T2)),
                        unreplied_findings=0)
    check("#301a: superseded CANCELLED + later SUCCESS (same name) -> PASS", rc == 0)
    # (b) a LONE CANCELLED with no later same-name run must still BLOCK - the reduction
    # must not swallow a genuine failure just because it dedupes.
    rc, _, _, _ = run(["1", "owner/repo"],
                      fixture_json=rollup(checkrun("ci", "COMPLETED", "CANCELLED", started_at=T1)),
                      unreplied_findings=0)
    check("#301b: lone CANCELLED (no later same-name) -> BLOCK", rc == 2)
    # (c) A newer IN_PROGRESS run (completedAt=null) must beat a stale SUCCESS.
    rc, _, _, _ = run(["1", "owner/repo"],
                      fixture_json=rollup(
                          checkrun("ci", "COMPLETED", "SUCCESS", started_at=T1, completed_at=T1),
                          checkrun("ci", "IN_PROGRESS", "", started_at=T2, completed_at=None)),
                      unreplied_findings=0)
    check("#301c: stale SUCCESS + newer IN_PROGRESS(null completedAt) -> BLOCK", rc == 2)
    # (c2) THE CASE THAT ACTUALLY PROVES THE KEY *ORDER*. Cases (c) and (d) are both
    # carried by the null-sorts-newest rule, so they pass under EITHER key ordering -
    # verified by mutation: swapping startedAt/completedAt broke nothing until this case
    # existed. Here BOTH timestamps are present and they DISAGREE: a run STARTED later
    # FINISHED earlier (a fast re-run overtaking a slow original, which is ordinary when
    # a re-run skips cached steps). startedAt-primary correctly picks the later-started
    # FAILURE; completedAt-primary picks the SUCCESS that finished last and false-greens.
    rc, _, _, _ = run(["1", "owner/repo"],
                      fixture_json=rollup(
                          checkrun("ci", "COMPLETED", "SUCCESS", started_at=T1,
                                   completed_at="2026-01-03T00:00:00Z"),
                          checkrun("ci", "COMPLETED", "FAILURE", started_at=T2,
                                   completed_at="2026-01-02T12:00:00Z")),
                      unreplied_findings=0)
    check("#301c2: later-STARTED FAILURE that finished FIRST -> BLOCK (startedAt is primary)",
          rc == 2)
    # (d) FALSE-GREEN TRAP: a QUEUED re-run has startedAt=null. Nulls must sort NEWEST,
    # not oldest, or the old SUCCESS wins and the pending re-run is invisible.
    rc, _, _, _ = run(["1", "owner/repo"],
                      fixture_json=rollup(
                          checkrun("ci", "COMPLETED", "SUCCESS", started_at=T1, completed_at=T1),
                          checkrun("ci", "QUEUED", "", started_at=None, completed_at=None)),
                      unreplied_findings=0)
    check("#301d: old SUCCESS + newer QUEUED(null startedAt) -> BLOCK (nulls sort newest)", rc == 2)
    # (e) the reduction must not be a blanket "any SUCCESS wins": a LATER FAILURE over an
    # earlier SUCCESS still blocks.
    rc, _, _, _ = run(["1", "owner/repo"],
                      fixture_json=rollup(
                          checkrun("ci", "COMPLETED", "SUCCESS", started_at=T1),
                          checkrun("ci", "COMPLETED", "FAILURE", started_at=T2)),
                      unreplied_findings=0)
    check("#301e: latest same-name is FAILURE over earlier SUCCESS -> BLOCK", rc == 2)
    # StatusContexts have no re-run semantics and must bypass the grouping untouched.
    rc, _, _, _ = run(["1", "owner/repo"],
                      fixture_json=rollup(checkrun("ci", "COMPLETED", "SUCCESS", started_at=T2),
                                          statusctx("legacy", "FAILURE")),
                      unreplied_findings=0)
    check("#301: a failing StatusContext still BLOCKS (not swallowed by the reduction)", rc == 2)
    # The unknown-__typename fail-closed path must survive the restructure.
    rc, _, _, _ = run(["1", "owner/repo"],
                      fixture_json=rollup(checkrun("ci", "COMPLETED", "SUCCESS", started_at=T2),
                                          unknowntype("mystery")),
                      unreplied_findings=0)
    check("#301: unknown __typename still BLOCKS (fail-closed path intact)", rc == 2)

    print("== #334: GitHub's aggregate verdict gates the PASS ==")
    # THE DEFECT: the oracle returned PASS on a PR sitting at mergeStateStatus=BLOCKED,
    # because mergeStateStatus was fetched ONLY in DIAGNOSE mode (an after-the-fact
    # explainer) and never on the gating path. Measured with an unsigned commit tripping a
    # required_signatures rule this oracle does not evaluate rule-by-rule. For a check whose
    # whole contract is fail-CLOSED, a false PASS is the worst defect it can have.
    ALL_GREEN_CTX = checkrun("ci", "COMPLETED", "SUCCESS")
    rc, out, err, _ = run(["1", "owner/repo"],
                          fixture_json=rollup(ALL_GREEN_CTX, merge_state="BLOCKED"),
                          unreplied_findings=0)
    check("#334: all checks green but mergeStateStatus=BLOCKED -> exit 2 (was a false PASS)",
          rc == 2 and "RESULT: PASS" not in out)
    check("#334: the BLOCK names the aggregate verdict, not a guess",
          "mergeStateStatus=BLOCKED" in err)
    check("#334: the BLOCK points at --diagnose for the specific unmet rule",
          "--diagnose" in err)
    # DIRTY (conflicts) is the other everyday blocking state.
    rc, out, _, _ = run(["1", "owner/repo"],
                        fixture_json=rollup(ALL_GREEN_CTX, merge_state="DIRTY"),
                        unreplied_findings=0)
    check("#334: mergeStateStatus=DIRTY (conflicts) -> exit 2", rc == 2 and "RESULT: PASS" not in out)
    # UNKNOWN means GitHub has not finished computing. "Not yet known" must never read as
    # "fine" in a fail-closed gate - re-running resolves it in seconds.
    rc, out, err, _ = run(["1", "owner/repo"],
                          fixture_json=rollup(ALL_GREEN_CTX, merge_state="UNKNOWN"),
                          unreplied_findings=0)
    check("#334: mergeStateStatus=UNKNOWN -> exit 2 (still computing is not 'fine')",
          rc == 2 and "re-run" in err)
    # An unreadable/absent field must BLOCK, not default to mergeable.
    rc, out, _, _ = run(["1", "owner/repo"],
                        fixture_json=rollup(ALL_GREEN_CTX, merge_state="__OMIT__"),
                        unreplied_findings=0)
    check("#334: absent mergeStateStatus -> exit 2 (fail closed, never assume mergeable)",
          rc == 2 and "RESULT: PASS" not in out)
    # THE MERGEABLE SET still passes - a gate that blocks everything is not a fix.
    for ms in ("CLEAN", "UNSTABLE", "HAS_HOOKS"):
        rc, out, _, _ = run(["1", "owner/repo"],
                            fixture_json=rollup(ALL_GREEN_CTX, merge_state=ms),
                            unreplied_findings=0)
        check(f"#334: mergeStateStatus={ms} still PASSes", rc == 0 and "RESULT: PASS" in out)
    # BEHIND passes DELIBERATELY (base-freshness is out of this oracle's scope, owned by
    # base-freshness.sh) but must be REPORTED rather than silently swallowed.
    rc, out, _, _ = run(["1", "owner/repo"],
                        fixture_json=rollup(ALL_GREEN_CTX, merge_state="BEHIND"),
                        unreplied_findings=0)
    check("#334: BEHIND still PASSes (base-freshness is out of scope)", rc == 0)
    check("#334: BEHIND is REPORTED in the PASS line, not silent",
          "BEHIND" in out and "out of this oracle's scope" in out)

    print("== #315: reviewDecision=<none> is disambiguated, not bare ==")
    # A bare `<none>` inside a RESULT: PASS line reads as "review state is fine" while
    # meaning only "not an active CHANGES_REQUESTED". reviewDecision is a branch-protection
    # artifact, so null covers three different states: approved, never-reviewed, or
    # dismissed-by-push. Measured on a real PR that had an APPROVED on HEAD.
    rc, out, _, _ = run(["1", "owner/repo"],
                        fixture_json=rollup(ALL_GREEN_CTX, review_decision=None),
                        unreplied_findings=0)
    check("#315: a null reviewDecision PASS still says <none>", rc == 0 and "reviewDecision=<none>" in out)
    check("#315: ...but is qualified, not left bare",
          "no decision required" in out)
    # REPORTING, not gating: the qualifier must never turn a PASS into a BLOCK, because
    # requiring an approval would impose a policy the repo has not set (auto-review is off
    # org-wide, so plenty of legitimately-mergeable PRs carry no approval).
    check("#315: the disambiguation NEVER changes the verdict (still exit 0)", rc == 0)

    print("== #301 Part 2: review-coverage advisory (WARN only, fail-OPEN) ==")
    # The gate measured thread state and CI state but never asked whether anyone reviewed
    # the code about to merge, so fix-round commits pushed after the last review merged
    # UNREVIEWED with every gate green.
    def review(commit, login="coderabbitai[bot]", state="APPROVED", at="2026-01-01T00:00:00Z"):
        return {"commit_id": commit, "state": state, "submitted_at": at,
                "user": {"login": login}}
    OTHER_SHA = "b" * 40
    GREEN = checkrun("ci", "COMPLETED", "SUCCESS")
    # head IS among the reviewed oids -> silent. Membership, NOT recency: a reviewer who
    # re-reviews an EARLIER commit must not trip a WARN (that is the false-WARN the AC names).
    rc, out, _, _ = run(["1", "owner/repo"], fixture_json=rollup(GREEN), unreplied_findings=0,
                        reviews=json.dumps([review(DEFAULT_SHA),
                                            review(OTHER_SHA, at="2026-01-02T00:00:00Z")]))
    check("#301p2: head among reviewed oids -> PASS with NO coverage WARN",
          rc == 0 and "WARN: no review covers" not in out)
    # head NOT reviewed -> WARN naming the range, verdict UNCHANGED.
    rc, out, _, _ = run(["1", "owner/repo"], fixture_json=rollup(GREEN), unreplied_findings=0,
                        reviews=json.dumps([review(OTHER_SHA)]))
    check("#301p2: head NOT reviewed -> WARN, exit UNCHANGED (still 0)", rc == 0)
    check("#301p2: the WARN names the unreviewed range",
          f"{OTHER_SHA[:8]}..{DEFAULT_SHA[:8]}" in out)
    check("#301p2: the WARN says the LEAD decides (it is not a policy)", "LEAD decides" in out)
    # Zero reviews -> WARN, verdict unchanged.
    rc, out, _, _ = run(["1", "owner/repo"], fixture_json=rollup(GREEN), unreplied_findings=0,
                        reviews="[]")
    check("#301p2: zero reviews -> WARN, exit UNCHANGED", rc == 0 and "entirely unreviewed" in out)
    # FAIL-OPEN, the property that makes an advisory safe: an unreadable reviews API must
    # leave the exit code IDENTICAL to a normal run. An advisory that can flip a verdict
    # is not an advisory.
    rc_fail, out_fail, _, _ = run(["1", "owner/repo"], fixture_json=rollup(GREEN),
                                  unreplied_findings=0, reviews_fail=True)
    rc_base, _, _, _ = run(["1", "owner/repo"], fixture_json=rollup(GREEN),
                           unreplied_findings=0, reviews=json.dumps([review(DEFAULT_SHA)]))
    check("#301p2: reviews-API failure -> exit IDENTICAL to a healthy run (fail-OPEN)",
          rc_fail == rc_base == 0)
    check("#301p2: ...and says so as a NOTE rather than failing silently",
          "unverifiable" in out_fail or "unreadable" in out_fail)

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

    # >100 review threads -> truncation NOTE (the unresolved count is a lower bound).
    rc, out, err, _ = run(["7", "owner/repo", "--diagnose"],
                          fixture_json=diag_fixture(mss="BLOCKED",
                                                    contexts=[checkrun("ci", "COMPLETED", "SUCCESS")]),
                          protection=prot_fixture(required_contexts=["ci"], conv_res=True),
                          threads_json=threads_doc(unresolved=1, total=150))
    check("#275: >100 threads -> truncation NOTE (lower bound), exit 2",
          rc == 2 and "lower bound" in (out + err))

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
    # #277 Copilot: the three modes are mutually exclusive.
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-only", "--codoki-gate"], fixture_json=ALL_GREEN)
    check("#277: --codoki-only + --codoki-gate -> exit 1 (mutually exclusive)", rc == 1)
    rc, _, _, _ = run(["1", "owner/repo", "--codoki-gate", "--diagnose"], fixture_json=ALL_GREEN)
    check("#277: --codoki-gate + --diagnose -> exit 1 (mutually exclusive)", rc == 1)

    print()
    print("== #375: reconcile the rollup against an EXPECTED check set ==")
    # THE CANONICAL FIXTURE, from the live false PASS on sydlexius/stillwater#3021:
    # a PR based off a non-trunk branch, mergeStateStatus=CLEAN, carrying ONE of the
    # required contexts. Build/Test/Lint/Coverage/CodeQL never ran. Nothing red,
    # nothing pending, nothing reported missing -- and GitHub's own aggregate verdict
    # agrees, because the ruleset targets ~DEFAULT_BRANCH and off that branch there is
    # no rule to violate. Silence is not consent.
    MAIN_REQUIRES = rules_doc(contexts=["Build", "Test", "Lint", "Signed Commits"])
    STACKED = rollup(
        checkrun("Signed Commits", "COMPLETED", "SUCCESS"),
        base_ref="fix/parent-branch",
    )
    rc, out, err, _ = run(["3021", "owner/repo"], fixture_json=STACKED,
                          rules_main=MAIN_REQUIRES, rules_base=rules_doc())
    blob = out + err
    check("#375: a non-default-base PR missing 3 of 4 required checks BLOCKS "
          "(the live #3021 false PASS)", rc == 2)
    check("#375: the BLOCK NAMES the missing contexts (a gate that will not say what "
          "is missing cannot be acted on)",
          "Build" in blob and "Test" in blob and "Lint" in blob)

    # F4 -- THE PREDICATE THAT MUST NOT REGRESS. stillwater returns exactly one rule for
    # a non-default ref: copilot_code_review, with ZERO required checks. If the fallback
    # keys on "has rules" instead of "has required_status_checks", this base reads as
    # governed, the expected set is empty, and the same false green passes through a
    # longer path. This case is the difference between the fix and the appearance of one.
    rc, out, err, _ = run(["3021", "owner/repo"], fixture_json=STACKED,
                          rules_main=MAIN_REQUIRES,
                          rules_base=rules_doc(extra_types=["copilot_code_review"]))
    check("#375 F4: a base carrying rules but NO required_status_checks still falls "
          "back to the default branch (predicate is on required checks, not on rules)",
          rc == 2)

    # The fallback invents policy in exactly one place, so it must announce it.
    check("#375: the fallback to the default branch is REPORTED, never silent",
          "default branch" in blob.lower() or "fallback" in blob.lower())

    # A base with its OWN required checks wins -- the release-base case that is the
    # whole reason for choosing Option C over "always measure against main".
    RELEASE_BASE = rollup(
        checkrun("Build", "COMPLETED", "SUCCESS"),
        base_ref="release/1.2",
    )
    # DISCRIMINATING SHAPE. An earlier version gave the base ["Build"] while main
    # required 4 and the PR had Build -- which passes whether the base's set wins OR no
    # reconciliation happens at all, so the assertion did not test its own name. The
    # base now requires a context main does NOT (`OnlyBase`), so a PASS is only possible
    # if the base's set genuinely won, and a BLOCK naming OnlyBase proves it was used.
    rc, out, err, _ = run(["500", "owner/repo"], fixture_json=RELEASE_BASE,
                          rules_main=MAIN_REQUIRES,
                          rules_base=rules_doc(contexts=["Build", "OnlyBase"]))
    blob500 = out + err
    check("#375: a base with its OWN required set is measured against THAT, not main "
          "(BLOCKs on the base-only context, proving the base set was the one used)",
          rc == 2 and "OnlyBase" in blob500)
    check("#375: ...and it does NOT block on main-only contexts that do not apply to "
          "that base (a protected release base is not measured against main)",
          "Test" not in blob500.split("did not run")[-1].split("\n")[0])

    RELEASE_OK = rollup(
        checkrun("Build", "COMPLETED", "SUCCESS"),
        checkrun("OnlyBase", "COMPLETED", "SUCCESS"),
        base_ref="release/1.2",
    )
    rc, out, err, _ = run(["501", "owner/repo"], fixture_json=RELEASE_OK,
                          rules_main=MAIN_REQUIRES,
                          rules_base=rules_doc(contexts=["Build", "OnlyBase"]))
    check("#375: a release base with ITS OWN set satisfied PASSES (not blocked by "
          "main's extra contexts)", rc == 0)

    # C1 -- THE FAIL-OPEN THAT SHIPPED IN THE FIRST DRAFT. When the fallback is needed
    # (base requires nothing) and `gh repo view` fails, an earlier version skipped
    # reconciliation entirely and PASSed emitting NOTHING -- byte-identical to a PR that
    # legitimately had no expected set. One transient API failure restored the exact
    # #3021 false PASS, on the oracle that arms the floor merge-auth token.
    rc, out, err, _ = run(["3021", "owner/repo"], fixture_json=STACKED,
                          rules_main=MAIN_REQUIRES, rules_base=rules_doc(),
                          default_branch_fail=True)
    check("#375 C1: an unreadable DEFAULT BRANCH blocks when the fallback is needed "
          "(never a silent PASS)", rc == 2)

    # ...but it must not block when the fallback is NOT needed: a base carrying its own
    # required set never consults the default branch, so a flaky lookup is irrelevant there.
    rc, out, err, _ = run(["502", "owner/repo"], fixture_json=RELEASE_OK,
                          rules_main=MAIN_REQUIRES,
                          rules_base=rules_doc(contexts=["Build", "OnlyBase"]),
                          default_branch_fail=True)
    check("#375 C1: an unreadable default branch does NOT block a base that carries "
          "its own required set (the guard fires only where it matters)", rc == 0)

    # M4: a skipped reconciliation and a clean one must be distinguishable in the output.
    NO_RULES = rollup(checkrun("ci", "COMPLETED", "SUCCESS"), base_ref="main")
    rc, out, err, _ = run(["800", "owner/repo"], fixture_json=NO_RULES,
                          rules_main=rules_doc(), rules_base=rules_doc())
    check("#375 M4: a repo with NO required checks says so, rather than passing "
          "silently (a PASS must be auditable)",
          rc == 0 and "reconciliation skipped" in (out + err))

    # I2: the expected set is the UNION of both authorities. They disagree on this repo
    # (rulesets 4, legacy 1) and legacy is a strict SUBSET today, so the union is a
    # no-op -- which is precisely why shipping without it would have looked correct
    # until the first legacy-only required context appeared. `protection` supplies a
    # context the rules endpoint does not; it must be enforced.
    UNION_PR = rollup(checkrun("Build", "COMPLETED", "SUCCESS"), base_ref="main")
    rc, out, err, _ = run(["900", "owner/repo"], fixture_json=UNION_PR,
                          rules_main=rules_doc(contexts=["Build"]),
                          rules_base=rules_doc(contexts=["Build"]),
                          protection=prot_fixture(required_contexts=["LegacyOnly"]))
    check("#375 I2: a LEGACY-only required context is enforced (expected set is the "
          "UNION of rulesets and branch protection, not either alone)",
          rc == 2 and "LegacyOnly" in (out + err))

    # ...and legacy being unreadable must NOT block: it needs admin scope and 404s
    # ("Branch not protected") wherever legacy protection is simply not configured, so
    # treating absence as unreadable would block every merge on a rulesets-only repo.
    rc, out, err, _ = run(["901", "owner/repo"], fixture_json=UNION_PR,
                          rules_main=rules_doc(contexts=["Build"]),
                          rules_base=rules_doc(contexts=["Build"]))
    check("#375 I2: an unreadable/absent legacy protection degrades to the rulesets "
          "set rather than blocking (asymmetric on purpose)", rc == 0)

    # NO REGRESSION on the normal path. This criterion is load-bearing, not ceremony:
    # the oracle gates orchestrate-authorize-merge.sh, so a defect that made the
    # reconciliation wrong would block every merge.
    NORMAL = rollup(
        checkrun("Build", "COMPLETED", "SUCCESS"),
        checkrun("Test", "COMPLETED", "SUCCESS"),
        checkrun("Lint", "COMPLETED", "SUCCESS"),
        checkrun("Signed Commits", "COMPLETED", "SUCCESS"),
    )
    rc, out, err, _ = run(["600", "owner/repo"], fixture_json=NORMAL,
                          rules_main=MAIN_REQUIRES, rules_base=MAIN_REQUIRES)
    check("#375 NO REGRESSION: a main-based PR with every required check green "
          "still PASSES", rc == 0)

    # UNREADABLE -> BLOCK, per the #334 UNKNOWN precedent. A gate that cannot verify
    # has not passed; "still computing" must never read as "fine".
    rc, out, err, _ = run(["700", "owner/repo"], fixture_json=NORMAL, rules_fail=True)
    check("#375: an UNREADABLE expected set BLOCKS (fail closed, per #334)", rc == 2)

    # ------------------------------------------------------------------ #375 round 2.
    # Every case below is one shape of a SINGLE defect: a status or a type that goes
    # unchecked, turning "I could not read this" into "there was nothing to read" --
    # which routes to PASS, which arms the floor merge-auth token. The first round fixed
    # that shape at three call sites and left its siblings; these pin the siblings.

    # C1: `jq -e .` proves the body is JSON, NOT that it is the array-of-rules the
    # endpoint contracts. The extraction jq then ran with its status DISCARDED (a plain
    # assignment, which pipefail never sees), so any jq runtime error yielded an empty
    # set and a PASS. Each body below is valid JSON that the extraction cannot process.
    MISSING_BUILD = rollup(checkrun("Other", "COMPLETED", "SUCCESS"), base_ref="main")
    for label, body in [
        # The shape a paginated/enveloped response would take. REST list endpoints grow
        # envelopes; `.[]` over an object exits 5.
        ("an ENVELOPED object", '{"rules":[{"type":"required_status_checks",'
                                '"parameters":{"required_status_checks":[{"context":"Build"}]}}]}'),
        # One non-object element poisons the whole extraction, silently.
        ("a poisoned array", '["a string", {"type":"required_status_checks",'
                             '"parameters":{"required_status_checks":[{"context":"Build"}]}}]'),
        # GitHub returns {"message":...} with HTTP 200 on some paths, so gh exits 0.
        ("an error object served 200", '{"message":"Not Found","status":"404"}'),
        ("a JSON scalar", '42'),
        ("a JSON string", '"required_status_checks"'),
    ]:
        rc, out, err, _ = run(["1001", "owner/repo"], fixture_json=MISSING_BUILD,
                              rules_main=body, rules_base=body)
        check(f"#375 C1: {label} is UNREADABLE, never an empty expected set "
              f"(a shape error must not read as 'requires nothing')",
              rc == 2 and "unreadable" in (out + err))

    # ...and the control: the SAME rollup with a well-formed doc must still block on the
    # real missing context, not on unreadability. Without this the cases above would
    # pass under a blanket "always BLOCK" and prove nothing.
    rc, out, err, _ = run(["1002", "owner/repo"], fixture_json=MISSING_BUILD,
                          rules_main=rules_doc(contexts=["Build"]),
                          rules_base=rules_doc(contexts=["Build"]))
    check("#375 C1 control: a WELL-FORMED doc still blocks on the genuinely missing "
          "context (the shape check did not swallow the real verdict)",
          rc == 2 and "Build" in (out + err) and "unreadable" not in (out + err))

    # C2: `gh --jq` exits 1 BOTH when the endpoint 404s and when the FILTER fails, and
    # the legacy leg keyed only on exit status. A 200 whose body is unparseable
    # therefore degraded the union to rulesets-only with NO output saying so -- exactly
    # the "legacy-only context vanishes" case the union exists to prevent.
    LEGACY_OK = rollup(checkrun("Build", "COMPLETED", "SUCCESS"), base_ref="main")
    for label, body in [("HTML", "<html>502 Bad Gateway</html>"),
                        ("truncated JSON", '{"required_status_checks": {"cont'),
                        ("null", "null")]:
        rc, out, err, _ = run(["1003", "owner/repo"], fixture_json=LEGACY_OK,
                              rules_main=rules_doc(contexts=["Build"]),
                              rules_base=rules_doc(contexts=["Build"]),
                              protection=body)
        check(f"#375 C2: an UNPARSEABLE legacy body ({label}) is SURFACED, not silently "
              f"degraded to rulesets-only", "legacy" in (out + err).lower())

    # ...while a genuine 404/403 stays silent. The asymmetry is the whole design: legacy
    # protection is simply absent on a rulesets-only repo, and announcing that on every
    # merge would train the reader to ignore the line that matters above.
    rc, out, err, _ = run(["1004", "owner/repo"], fixture_json=LEGACY_OK,
                          rules_main=rules_doc(contexts=["Build"]),
                          rules_base=rules_doc(contexts=["Build"]))
    check("#375 C2: an ABSENT legacy protection (404) stays silent -- only an "
          "unparseable PRESENT one is surfaced",
          rc == 0 and "legacy" not in (out + err).lower())

    # C3: the fix documented three comment blocks above the default-branch lookup was
    # never applied TO it. Real `gh ... --jq` writes its error body to STDOUT and exits
    # 1, so `|| echo ""` captured the PAYLOAD, the [ -z ] guard was skipped, and the
    # script fetched rules/branches/<error text>, got [], and PASSed. The C1 guard the
    # harness certified as closed was still reachable on the transport failure it names.
    rc, out, err, _ = run(["1005", "owner/repo"], fixture_json=STACKED,
                          rules_main=MAIN_REQUIRES, rules_base=rules_doc(),
                          default_branch_fail=True)
    check("#375 C3: a default-branch lookup that fails WITH A BODY ON STDOUT blocks "
          "(the error payload is not usable as a branch name)",
          rc == 2 and "unreadable" in (out + err))

    # ...and the same guard must reject a `null` branch name. `--jq .a.b` prints the
    # literal `null` when the field is absent, which is non-empty and so passed the
    # [ -z ] test, then resolved as a ref named "null" -> [] -> PASS.
    rc, out, err, _ = run(["1006", "owner/repo"], fixture_json=STACKED,
                          rules_main=MAIN_REQUIRES, rules_base=rules_doc(),
                          default_branch="null")
    check("#375 C3: a literal 'null' default-branch name is rejected, not used as a ref",
          rc == 2)

    # m1: the default branch must be READ, not assumed. Hard-coding it to "main" and
    # never calling gh passed the entire suite, because every fixture defaulted to main.
    STACKED_TRUNK = rollup(checkrun("Build", "COMPLETED", "SUCCESS"), base_ref="feature/x")
    rc, out, err, _ = run(["1007", "owner/repo"], fixture_json=STACKED_TRUNK,
                          rules_main=MAIN_REQUIRES, rules_base=rules_doc(),
                          default_branch="trunk")
    check("#375: the fallback uses the REPO'S default branch, whatever it is named "
          "(a non-'main' trunk is honored, proving the value is read not assumed)",
          rc == 2 and "trunk" in (out + err))

    # I2 (real): with the stub now applying --jq, a legacy-only context is enforced by
    # NAME rather than by appearing as a substring of a dumped JSON blob -- and the PASS
    # side, which had no case at all, is now covered.
    LEGACY_PRESENT = rollup(checkrun("Build", "COMPLETED", "SUCCESS"),
                            checkrun("LegacyOnly", "COMPLETED", "SUCCESS"), base_ref="main")
    rc, out, err, _ = run(["1008", "owner/repo"], fixture_json=LEGACY_PRESENT,
                          rules_main=rules_doc(contexts=["Build"]),
                          rules_base=rules_doc(contexts=["Build"]),
                          protection=prot_fixture(required_contexts=["LegacyOnly"]))
    check("#375 I2: a legacy-only context that IS present PASSES (the legacy leg "
          "contributes a real name, not a JSON document)",
          rc == 0 and "reconciled" in (out + err))

    # ...and the BLOCK side names exactly that context on its own, with no JSON blob.
    rc, out, err, _ = run(["1009", "owner/repo"], fixture_json=LEGACY_OK,
                          rules_main=rules_doc(contexts=["Build"]),
                          rules_base=rules_doc(contexts=["Build"]),
                          protection=prot_fixture(required_contexts=["LegacyOnly"]))
    blob_i2 = out + err
    check("#375 I2: the missing legacy context is named EXACTLY, with no JSON payload "
          "leaking into the required-check list",
          rc == 2 and "LegacyOnly" in blob_i2 and "required_status_checks" not in blob_i2)

    # I1: a context name containing a NEWLINE split into two independent requirements,
    # so two unrelated decoy checks satisfied a required check that never ran. Such a
    # name is pathological; rejecting it fail-closed is the correct handling.
    NEWLINE_DECOYS = rollup(checkrun("alpha", "COMPLETED", "SUCCESS"),
                            checkrun("beta", "COMPLETED", "SUCCESS"), base_ref="main")
    rc, out, err, _ = run(["1010", "owner/repo"], fixture_json=NEWLINE_DECOYS,
                          rules_main=rules_doc(contexts=["alpha\nbeta"]),
                          rules_base=rules_doc(contexts=["alpha\nbeta"]))
    check("#375 I1: a required context containing a NEWLINE is not satisfied by two "
          "decoy checks named after its halves", rc == 2)

    # ...AND FROM THE LEGACY LEG. The first fix guarded only the rulesets doc, so the
    # identical false PASS survived one call site over -- the same "patched at one site,
    # not swept" shape the round was opened to end. Both legs are now validated at the
    # UNION, so a per-leg omission is not expressible.
    rc, out, err, _ = run(["1014", "owner/repo"],
                          fixture_json=rollup(checkrun("gamma", "COMPLETED", "SUCCESS"),
                                              checkrun("delta", "COMPLETED", "SUCCESS"),
                                              base_ref="main"),
                          rules_main=rules_doc(), rules_base=rules_doc(),
                          protection=prot_fixture(required_contexts=["gamma\ndelta"]))
    check("#375 I1: a NEWLINE-bearing context from the LEGACY leg is rejected too "
          "(the invariant is on the union, not on one source)", rc == 2)

    # An EMPTY 200 legacy body is BENIGN, not malformed: the rules leg normalizes exactly
    # this case, and losing that on the legacy leg turned its documented best-effort
    # degradation into a SILENT BLOCK (silent because control never reached the
    # unparseable NOTE). Fail-closed, so not a false PASS -- but a leg that "contributes
    # nothing rather than failing the gate" must actually not fail the gate.
    for label, body in [("empty", ""), ("whitespace-only", "   ")]:
        rc, out, err, _ = run(["1020", "owner/repo"], fixture_json=LEGACY_OK,
                              rules_main=rules_doc(contexts=["Build"]),
                              rules_base=rules_doc(contexts=["Build"]),
                              protection=body)
        check(f"#375: a {label} 200 legacy body degrades benignly (best-effort leg, "
              f"never a silent BLOCK)", rc == 0)

    # The modern `checks[].context` shape -- what GitHub returns today -- had no case at
    # all: deleting that half of the legacy filter failed zero tests.
    rc, out, err, _ = run(["1015", "owner/repo"], fixture_json=LEGACY_OK,
                          rules_main=rules_doc(contexts=["Build"]),
                          rules_base=rules_doc(contexts=["Build"]),
                          protection=json.dumps({"required_status_checks": {
                              "strict": False, "contexts": [],
                              "checks": [{"context": "ModernOnly"}]}}))
    check("#375: the modern legacy `checks[].context` shape contributes to the expected "
          "set (not only the deprecated `contexts[]`)",
          rc == 2 and "ModernOnly" in (out + err))

    # A non-STRING context would be rendered as a name -- an object pretty-prints across
    # several LINES, fabricating multiple required checks out of one malformed entry.
    # Fail-closed, and it keeps JSON punctuation out of the operator-facing list.
    for label, ctx in [("a number", 42), ("null", None), ("an object", {"a": 1})]:
        doc = json.dumps([{"type": "required_status_checks", "ruleset_id": 2,
                           "parameters": {"required_status_checks": [{"context": ctx}]}}])
        rc, out, err, _ = run(["1016", "owner/repo"], fixture_json=LEGACY_OK,
                              rules_main=doc, rules_base=doc)
        check(f"#375: a non-string required context ({label}) is UNREADABLE, never "
              f"rendered into the expected set as a name",
              rc == 2 and "unreadable" in (out + err))

    # Each C1 defense proven INDEPENDENTLY. Mutating them only together let a partial
    # regression of either land silently, which is how a defense-in-depth pair decays
    # into one defense plus a comment.
    rc, out, err, _ = run(["1017", "owner/repo"], fixture_json=MISSING_BUILD,
                          rules_main="[null]", rules_base="[null]")
    check("#375 C1a: a null ELEMENT is caught by the array-of-objects type check "
          "(independent of the extraction status check)",
          rc == 2 and "unreadable" in (out + err))

    BAD_PARAMS = json.dumps([{"type": "required_status_checks", "parameters": "oops"}])
    rc, out, err, _ = run(["1018", "owner/repo"], fixture_json=MISSING_BUILD,
                          rules_main=BAD_PARAMS, rules_base=BAD_PARAMS)
    check("#375 C1b: a jq RUNTIME error during extraction is caught by the status check "
          "(independent of the type check, which this document passes)",
          rc == 2 and "unreadable" in (out + err))

    # C3's value REJECTION proven independently of its capture: reverting only the case
    # arm (keeping capture-then-decide) let the literal 'null' through to a false PASS.
    # The legitimate-name direction is asserted by the 'trunk' case above.
    for good in ["release/1.2", "feature/foo.bar"]:
        rc, out, err, _ = run(["1019", "owner/repo"], fixture_json=STACKED_TRUNK,
                              rules_main=MAIN_REQUIRES, rules_base=rules_doc(),
                              default_branch=good)
        check(f"#375 C3: a legitimate default-branch name ({good}) is still USED for "
              f"the fallback (the rejection is narrow)",
              rc == 2 and good in (out + err))

    # I3: the unreadable-default-branch-RULES exit was unreachable by any fixture
    # (RULES_FAIL killed the base read first and exited above it), so neutering it into
    # a silent skip failed zero tests. Per-route failure separates the two.
    rc, out, err, _ = run(["1011", "owner/repo"], fixture_json=STACKED,
                          rules_main=MAIN_REQUIRES, rules_base=rules_doc(),
                          rules_main_fail=True)
    check("#375: unreadable DEFAULT-BRANCH rules block too (all four unreadable inputs "
          "fail the same way, each proven separately)",
          rc == 2 and "unreadable" in (out + err))

    # An absent baseRefName was documented as BLOCKing but no fixture ever omitted it,
    # so replacing that whole branch with expected="" failed zero tests.
    NO_BASE = rollup(checkrun("Build", "COMPLETED", "SUCCESS"), base_ref="__OMIT__")
    rc, out, err, _ = run(["1012", "owner/repo"], fixture_json=NO_BASE,
                          rules_main=MAIN_REQUIRES, rules_base=MAIN_REQUIRES)
    check("#375: an ABSENT baseRefName blocks (the expected set is unknowable)",
          rc == 2 and "base branch" in (out + err))

    # M5c: the PASS-side reconciliation NOTE was unmutated -- deleting it failed zero
    # tests, which would make a reconciled PASS indistinguishable from a pre-#375 PASS.
    # That audit trail IS the deliverable; the skipped side already had a case.
    rc, out, err, _ = run(["1013", "owner/repo"], fixture_json=NORMAL,
                          rules_main=MAIN_REQUIRES, rules_base=MAIN_REQUIRES)
    check("#375 M5c: a CLEAN reconciliation says so on the PASS path (a reconciled "
          "PASS must be distinguishable from a pre-#375 one)",
          rc == 0 and "reconciled against" in (out + err))

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
