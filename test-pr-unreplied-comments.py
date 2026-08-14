#!/usr/bin/env python3
"""Proof harness for pr-unreplied-comments.sh (#132 gate-correctness + audit; #93 staleness).

Covers three additions, all host-independent (gh is a temp Python stub first on
PATH, applying any --jq via the real jq; never a real PR):

  #132 default-count fix: the gating line "Review-body comments with actionable
       findings: N" must ADD CodeRabbit's "Outside diff range comments (K)" block
       (carried in the review BODY, no inline thread) to the count. The lead's
       canonical case: a body with "Actionable comments posted: 1" + "Outside diff
       range comments (6)" must report 7, not 1. Summed across ALL CR submissions.

  #132 --audit mode: complete-coverage enumeration of every CR + Codoki comment
       (inline FINDINGS via GraphQL reviewThreads + issue-level SUMMARIES). Exit 0
       only when every finding is replied AND every thread resolved; exit 1
       otherwise. Summaries are informational (never gate). --audit is mutually
       exclusive with the gating/scripting early-exit modes.

  #93 staleness advisory: a non-fatal "STALE-ADVISORY:" line for any bot verdict
       (review or in-place-edited issue comment) predating the current HEAD push.
       Exit code UNCHANGED.

Default-mode cases pass --allow-stale so the git-backed base-freshness gate is
skipped (no real repo needed).

Run: python3 test-pr-unreplied-comments.py
"""
import json
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "pr-unreplied-comments.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


GH_STUB = r'''#!/usr/bin/env python3
import os, sys, subprocess
args = sys.argv[1:]
ME = os.environ.get("ME", "testuser")
INLINE = os.environ.get("INLINE_JSON", "[]")
REVIEWS = os.environ.get("REVIEWS_JSON", "[]")
ISSUE = os.environ.get("ISSUE_JSON", "[]")
GRAPHQL = os.environ.get("GRAPHQL_JSON", '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[]}}}}}')
GRAPHQL_NEXT = os.environ.get("GRAPHQL_NEXT", "")
HEAD_SHA = os.environ.get("HEAD_SHA", "abcdef1234567890abcdef1234567890abcdef12")
COMMITTER_DATE = os.environ.get("COMMITTER_DATE", "2026-06-18T00:00:00Z")
CHECK_RUNS = os.environ.get("CHECK_RUNS_JSON", '{"total_count":0,"check_runs":[]}')
# #316: the REAL gh writes a 4xx ERROR BODY to STDOUT and exits nonzero even with
# --jq -- the filter is never applied, so the caller receives the raw payload where
# it expected filtered JSON. A stub that always pipes through jq is KINDER THAN
# REALITY and makes the caller's type-validation untestable (a mutation deleting
# that validation survived until this knob existed). Set CHECK_RUNS_RAW=1 to emit
# the CHECK_RUNS payload verbatim, bypassing --jq, the way gh actually does.
CHECK_RUNS_RAW = os.environ.get("CHECK_RUNS_RAW", "") == "1"
PULL = '{"head":{"sha":"%s"}}' % HEAD_SHA
COMMIT = '{"commit":{"committer":{"date":"%s"}}}' % COMMITTER_DATE

def emit(data, raw=False):
    if "--jq" in args and not raw:
        expr = args[args.index("--jq") + 1]
        p = subprocess.run(["jq", "-r", expr], input=data, capture_output=True, text=True)
        sys.stdout.write(p.stdout)
        sys.exit(0)
    sys.stdout.write(data)
    # raw mode models a gh ERROR: the body goes to STDOUT with --jq unapplied AND the
    # exit status is NONZERO. Exiting 0 here would make the stub KINDER THAN REALITY in
    # the one way that matters: the caller's `|| echo '[]'` fallback would never fire,
    # so the CONCATENATION path (gh's body + the fallback text, which is what actually
    # feeds jq garbage) would go untested while looking covered.
    sys.exit(1 if raw else 0)

if args[:2] == ["api", "user"]:
    emit('{"login":"%s"}' % ME)
if args[:2] == ["api", "graphql"]:
    # Paginated GraphQL: the script passes `-F cursor=null` (or `-f cursor=null`)
    # for the first page and `-f cursor=<endCursor>` to advance. Serve GRAPHQL for
    # the first page; serve GRAPHQL_NEXT (when set) for any non-null cursor so a
    # >100-thread (hasNextPage) scenario can be exercised.
    cursor = None
    for a in args:
        if a.startswith("cursor="):
            cursor = a.split("=", 1)[1]; break
    if cursor and cursor != "null" and GRAPHQL_NEXT:
        emit(GRAPHQL_NEXT)
    emit(GRAPHQL)

endpoint = ""
for a in args:
    if "repos/" in a:
        endpoint = a; break
if endpoint.endswith("/reviews"):
    emit(REVIEWS)
if endpoint.endswith("/comments") and "/pulls/" in endpoint:
    emit(INLINE)
if endpoint.endswith("/comments") and "/issues/" in endpoint:
    # ISSUE_RAW=1 mirrors real gh on an error: the body goes to STDOUT with the --jq
    # filter UNAPPLIED. Needed to exercise the #316-sibling abort at the issue-comments
    # read (an HTML 5xx body makes jq exit 5 and, unguarded, kills the script).
    emit(ISSUE, raw=os.environ.get("ISSUE_RAW", "") == "1")
# The check-runs endpoint (repos/O/R/commits/SHA/check-runs?per_page=100) also
# contains "/commits/", so it MUST be matched before the generic committer-date
# branch. Match on substring (a ?query may follow the /check-runs path).
if "/check-runs" in endpoint:
    emit(CHECK_RUNS, raw=CHECK_RUNS_RAW)
if "/commits/" in endpoint:
    emit(COMMIT)
emit(PULL)
'''


def run(args, *, inline="[]", reviews="[]", issue="[]", graphql=None,
        graphql_next=None, committer_date="2026-06-18T00:00:00Z", me="testuser",
        check_runs=None, extra_env=None):
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(GH_STUB)
        os.chmod(gh, 0o755)
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["ME"] = me
        env["INLINE_JSON"] = inline
        env["REVIEWS_JSON"] = reviews
        env["ISSUE_JSON"] = issue
        env["COMMITTER_DATE"] = committer_date
        if graphql is not None:
            env["GRAPHQL_JSON"] = graphql
        if graphql_next is not None:
            env["GRAPHQL_NEXT"] = graphql_next
        if check_runs is not None:
            env["CHECK_RUNS_JSON"] = check_runs
        if extra_env:
            env.update(extra_env)
        p = subprocess.run(["bash", SCRIPT] + args + ["123", "owner/repo"],
                           env=env, capture_output=True, text=True, timeout=20)
        return p.returncode, p.stdout, p.stderr


def run_argv(argv, *, inline="[]", reviews="[]", issue="[]", me="testuser"):
    """Run the script with EXACTLY `argv` (no appended positional), for arg-parse tests
    (e.g. a flag placed AFTER the <pr> positional). Same gh stub as run()."""
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(GH_STUB)
        os.chmod(gh, 0o755)
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["ME"] = me
        env["INLINE_JSON"] = inline
        env["REVIEWS_JSON"] = reviews
        env["ISSUE_JSON"] = issue
        env["COMMITTER_DATE"] = "2026-06-18T00:00:00Z"
        p = subprocess.run(["bash", SCRIPT] + argv, env=env, capture_output=True, text=True, timeout=20)
        return p.returncode, p.stdout, p.stderr


def findings_count(out):
    """Extract N from the 'Review-body comments with actionable findings: N' line."""
    for ln in out.splitlines():
        if "Review-body comments with actionable findings:" in ln:
            for tok in ln.replace("=", " ").split():
                if tok.isdigit():
                    return int(tok)
    return None


# --- A review body carrying both an actionable inline count AND an outside-diff block.
CR_BODY_1_PLUS_6 = (
    '[{"id":111,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
    '"submitted_at":"2026-06-18T02:00:00Z",'
    '"body":"**Actionable comments posted: 1**\\n\\n<summary>Outside diff range comments (6)</summary>\\nfindings here"}]'
)
# Two CR submissions, outside-diff (6) and (3): the sum must aggregate across both.
CR_TWO_SUBMISSIONS = (
    '[{"id":111,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
    '"submitted_at":"2026-06-18T02:00:00Z",'
    '"body":"<summary>Outside diff range comments (6)</summary>"},'
    '{"id":222,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
    '"submitted_at":"2026-06-18T03:00:00Z",'
    '"body":"<summary>Outside diff range comments (3)</summary>"}]'
)


def main():
    print("== #132 default count: outside-diff findings are ADDED to the gate count ==")
    rc, out, err = run(["--allow-stale"], reviews=CR_BODY_1_PLUS_6)
    n = findings_count(out)
    check("Actionable posted:1 + Outside diff range comments (6) -> reports 7 (not 1)", n == 7)
    check("exit 0", rc == 0)

    rc, out, err = run(["--allow-stale"], reviews=CR_TWO_SUBMISSIONS)
    n = findings_count(out)
    # 2 surviving CR review bodies + (6 + 3) outside-diff findings, summed across BOTH
    # submissions (never latest-per-reviewer).
    check("two CR submissions aggregate outside-diff: 2 bodies + (6+3) -> reports 11", n == 11)

    rc, out, err = run(["--allow-stale"], reviews="[]")
    check("no findings -> no sentinel line, exit 0",
          findings_count(out) is None and rc == 0)

    print("== #93 staleness advisory (non-fatal; exit unchanged) ==")
    stale_review = ('[{"id":9,"user":{"login":"coderabbitai[bot]"},"state":"APPROVED",'
                    '"submitted_at":"2026-06-17T00:00:00Z","body":""}]')
    rc, out, err = run(["--allow-stale"], reviews=stale_review,
                       committer_date="2026-06-18T00:00:00Z")
    check("review predating HEAD -> STALE-ADVISORY line present", "STALE-ADVISORY:" in out)
    check("STALE-ADVISORY names the bot", "coderabbitai[bot]" in out)
    check("staleness advisory does NOT change exit code (exit 0)", rc == 0)

    fresh_review = ('[{"id":9,"user":{"login":"coderabbitai[bot]"},"state":"APPROVED",'
                    '"submitted_at":"2026-06-18T05:00:00Z","body":""}]')
    rc, out, err = run(["--allow-stale"], reviews=fresh_review,
                       committer_date="2026-06-18T00:00:00Z")
    check("review newer than HEAD -> no STALE-ADVISORY", "STALE-ADVISORY:" not in out)

    edited_stale = ('[{"id":7,"user":{"login":"codoki-pr-intelligence[bot]"},'
                    '"created_at":"2026-06-16T00:00:00Z","updated_at":"2026-06-17T00:00:00Z",'
                    '"body":"Review Status: Safe to merge"}]')
    rc, out, err = run(["--allow-stale"], issue=edited_stale,
                       committer_date="2026-06-18T00:00:00Z")
    check("in-place-edited issue comment predating HEAD -> STALE-ADVISORY (edited)",
          "STALE-ADVISORY:" in out and "(edited)" in out)

    print("== #132 --audit mode ==")
    # REALISM (#132): GraphQL author.login returns bot logins WITHOUT the "[bot]"
    # suffix (e.g. "coderabbitai") whereas REST user.login carries it. The audit
    # FINDINGS path reads GraphQL, so its fixtures MUST use suffix-less bot logins to
    # match real GitHub; an earlier "[bot]"-suffixed fixture masked the bug where
    # every bot thread was dropped (0 findings on a fully-resolved PR #129).
    # All replied + resolved -> exit 0.
    g_ok = ('{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":['
            '{"isResolved":true,"path":"a.sh","line":10,"comments":{"nodes":['
            '{"author":{"login":"coderabbitai"}},{"author":{"login":"testuser"}}]}}'
            ']}}}}}')
    rc, out, err = run(["--audit"], graphql=g_ok)
    check("audit: all findings replied + resolved -> exit 0", rc == 0)
    check("audit: table has TYPE/AUTHOR/LOCATION/REPLIED/RESOLVED columns",
          all(c in out for c in ("TYPE", "AUTHOR", "LOCATION", "REPLIED", "RESOLVED")))
    check("audit: location renders file:line", "a.sh:10" in out)
    # The core regression: a RESOLVED bot thread (suffix-less GraphQL login) is
    # ENUMERATED as a finding and counted, NOT silently dropped (PR #129 read 0).
    check("audit: resolved bot thread (suffix-less GraphQL login) IS enumerated as a finding",
          "finding" in out and "coderabbitai" in out)

    # Unreplied finding (only the bot comment, no human reply) -> exit 1. With the
    # suffix-less login this ALSO proves the root-author select normalizes: pre-fix
    # the bot thread was dropped (0 findings) and this wrongly exited 0.
    g_unreplied = ('{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":['
                   '{"isResolved":true,"path":"a.sh","line":10,"comments":{"nodes":['
                   '{"author":{"login":"coderabbitai"}}]}}'
                   ']}}}}}')
    rc, out, err = run(["--audit"], graphql=g_unreplied)
    check("audit: unreplied finding -> exit 1", rc == 1)

    # Replied but unresolved thread -> exit 1.
    g_unresolved = ('{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":['
                    '{"isResolved":false,"path":"a.sh","line":10,"comments":{"nodes":['
                    '{"author":{"login":"coderabbitai"}},{"author":{"login":"testuser"}}]}}'
                    ']}}}}}')
    rc, out, err = run(["--audit"], graphql=g_unresolved)
    check("audit: unresolved thread -> exit 1", rc == 1)

    # REPLIED normalization (#132): a bot's OWN reply also arrives suffix-less from
    # GraphQL and must NOT be mis-counted as a human reply. Two bot comments, no
    # human, resolved -> still UNREPLIED -> exit 1. Pre-fix the second bot's
    # suffix-less login failed the membership test and read as a human reply
    # (replied:true), wrongly exiting 0.
    g_bot_only_reply = ('{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":['
                        '{"isResolved":true,"path":"a.sh","line":10,"comments":{"nodes":['
                        '{"author":{"login":"coderabbitai"}},'
                        '{"author":{"login":"codoki-pr-intelligence"}}]}}'
                        ']}}}}}')
    rc, out, err = run(["--audit"], graphql=g_bot_only_reply)
    check("audit: bot's own (suffix-less) reply is NOT a human reply -> still unreplied -> exit 1",
          rc == 1)

    # Includes BOTH CodeRabbit (thread, GraphQL suffix-less) and Codoki (issue-level
    # summary, REST so "[bot]"-suffixed). Summaries read REST .user.login, which
    # carries the suffix; that path is unchanged.
    codoki_summary = '[{"user":{"login":"codoki-pr-intelligence[bot]"},"body":"Review Status: Safe","created_at":"x","updated_at":"x"}]'
    rc, out, err = run(["--audit"], graphql=g_ok, issue=codoki_summary)
    check("audit: enumerates both CodeRabbit finding and Codoki summary",
          "coderabbitai" in out and "codoki-pr-intelligence[bot]" in out)
    check("audit: Codoki issue-level summary does not flip exit (still 0)", rc == 0)

    # Mutual exclusivity with --count-only -> usage error exit 1.
    rc, out, err = run(["--audit", "--count-only"])
    check("audit + --count-only -> exit 1 (mutually exclusive)", rc == 1)

    print("== #234 --audit: Codoki summary ACKED vs UNACKED ==")
    # The Codoki issue-level summary is informational (never gates the exit code),
    # but its ack state is now surfaced from the embedded reactions counts: a +1 or
    # -1 reaction => ACKED, none => UNACKED. Non-fatal in both directions (exit 0).
    codoki_acked = ('[{"user":{"login":"codoki-pr-intelligence[bot]"},'
                    '"body":"Review Status: Safe","created_at":"x","updated_at":"x",'
                    '"reactions":{"total_count":1,"+1":1,"-1":0}}]')
    rc, out, err = run(["--audit"], graphql=g_ok, issue=codoki_acked)
    check("audit: Codoki summary with a reaction -> ACKED in the table",
          "ACKED" in out and "UNACKED" not in out)
    check("audit: Codoki ACKED does not flip the exit (still 0)", rc == 0)

    codoki_unacked = ('[{"user":{"login":"codoki-pr-intelligence[bot]"},'
                      '"body":"Review Status: Safe","created_at":"x","updated_at":"x",'
                      '"reactions":{"total_count":0,"+1":0,"-1":0}}]')
    rc, out, err = run(["--audit"], graphql=g_ok, issue=codoki_unacked)
    check("audit: Codoki summary with no reaction -> UNACKED in the table",
          "UNACKED" in out)
    check("audit: Codoki UNACKED does not flip the exit (still 0, informational)", rc == 0)

    print("== #132 --audit no-silent-caps: >100-thread pagination ==")
    # A PR with MORE than one page of review threads. The OLD code stopped at
    # reviewThreads(first:100) with no pageInfo check, so it could SILENTLY
    # TRUNCATE and still print "AUDIT: COMPLETE" / exit 0. The fix must paginate:
    # page 1 (hasNextPage:true) carries a resolved+replied finding; page 2 carries
    # a replied-but-UNRESOLVED finding. If pagination works, the script sees page 2
    # and reports INCOMPLETE / exit 1 -- it must NEVER print COMPLETE or exit 0.
    g_page1 = ('{"data":{"repository":{"pullRequest":{"reviewThreads":{'
               '"pageInfo":{"hasNextPage":true,"endCursor":"C1"},'
               '"nodes":[{"isResolved":true,"path":"a.sh","line":10,"comments":{'
               '"pageInfo":{"hasNextPage":false},"nodes":['
               '{"author":{"login":"coderabbitai"}},{"author":{"login":"testuser"}}]}}]'
               '}}}}}')
    g_page2 = ('{"data":{"repository":{"pullRequest":{"reviewThreads":{'
               '"pageInfo":{"hasNextPage":false},'
               '"nodes":[{"isResolved":false,"path":"b.sh","line":20,"comments":{'
               '"pageInfo":{"hasNextPage":false},"nodes":['
               '{"author":{"login":"coderabbitai"}},{"author":{"login":"testuser"}}]}}]'
               '}}}}}')
    rc, out, err = run(["--audit"], graphql=g_page1, graphql_next=g_page2)
    check("audit pagination: >100 threads (hasNextPage) does NOT print AUDIT: COMPLETE",
          "AUDIT: COMPLETE" not in out)
    check("audit pagination: >100 threads does NOT exit 0", rc != 0)
    check("audit pagination: page 2 thread is aggregated (b.sh:20 present, exit 1)",
          "b.sh:20" in out and rc == 1)

    # Inner-comments overflow: a single thread with MORE than 100 comments cannot
    # prove "replied" -> FAIL CLOSED (no COMPLETE, non-zero exit).
    g_comment_overflow = ('{"data":{"repository":{"pullRequest":{"reviewThreads":{'
                          '"pageInfo":{"hasNextPage":false},'
                          '"nodes":[{"isResolved":true,"path":"a.sh","line":10,"comments":{'
                          '"pageInfo":{"hasNextPage":true},"nodes":['
                          '{"author":{"login":"coderabbitai"}}]}}]'
                          '}}}}}')
    rc, out, err = run(["--audit"], graphql=g_comment_overflow)
    check("audit comment-overflow (>100 comments/thread) does NOT print AUDIT: COMPLETE",
          "AUDIT: COMPLETE" not in out)
    check("audit comment-overflow fails closed (exit non-zero)", rc != 0)

    print("== #145 --check-resolved: UNRESOLVED-ADVISORY on the default path ==")
    g_resolved = ('{"data":{"repository":{"pullRequest":{"reviewThreads":{'
                  '"pageInfo":{"hasNextPage":false},'
                  '"nodes":[{"isResolved":true,"path":"a.sh","line":10,'
                  '"comments":{"nodes":[{"author":{"login":"coderabbitai"}}]}}]'
                  '}}}}}')
    g_unresolved = ('{"data":{"repository":{"pullRequest":{"reviewThreads":{'
                    '"pageInfo":{"hasNextPage":false},'
                    '"nodes":[{"isResolved":false,"path":"a.sh","line":10,'
                    '"comments":{"nodes":[{"author":{"login":"coderabbitai"}}]}}]'
                    '}}}}}')
    g_unresolved_human = ('{"data":{"repository":{"pullRequest":{"reviewThreads":{'
                          '"pageInfo":{"hasNextPage":false},'
                          '"nodes":[{"isResolved":false,"path":"h.sh","line":5,'
                          '"comments":{"nodes":[{"author":{"login":"some-human"}}]}}]'
                          '}}}}}')
    rc, out, err = run(["--check-resolved", "--allow-stale"], graphql=g_resolved)
    check("all threads resolved -> no UNRESOLVED-ADVISORY", "UNRESOLVED-ADVISORY" not in out)
    check("all-resolved exit 0", rc == 0)

    rc, out, err = run(["--check-resolved", "--allow-stale"], graphql=g_unresolved)
    check("one unresolved bot thread -> UNRESOLVED-ADVISORY line", "UNRESOLVED-ADVISORY:" in out)
    check("advisory carries the bot author", "coderabbitai" in out)
    check("advisory carries the path:line", "a.sh:10" in out)
    check("unresolved thread + no unreplied comments -> exit 0 (advisory non-fatal)", rc == 0)

    rc, out, err = run(["--check-resolved", "--allow-stale"], graphql=g_unresolved_human)
    check("non-bot-rooted unresolved thread -> NO advisory", "UNRESOLVED-ADVISORY" not in out)

    rc, out, err = run(["--check-resolved", "--audit"])
    check("--check-resolved + --audit -> usage error", rc != 0 and "mutually exclusive" in err)

    rc, out, err = run(["--check-resolved", "--allow-stale"], graphql="not-json")
    check("graphql failure degrades to a stderr note (best-effort)", "UNRESOLVED-ADVISORY:" in err)
    check("graphql failure does NOT change exit code", rc == 0)
    check("graphql failure emits no advisory on stdout", "UNRESOLVED-ADVISORY" not in out)

    # found > 0 AND an unresolved thread: the advisory rides ALONGSIDE the normal
    # unreplied report and does not perturb the (found>0) exit code.
    rc, out, err = run(["--check-resolved", "--allow-stale"],
                       reviews=CR_BODY_1_PLUS_6, graphql=g_unresolved)
    check("found>0 + --check-resolved: unreplied total still reported", "Total unreplied" in out)
    check("found>0 + --check-resolved: advisory ALSO emitted", "UNRESOLVED-ADVISORY:" in out)
    check("found>0 + --check-resolved: exit unchanged (0)", rc == 0)

    # Multi-page reviewThreads: the advisory loop must follow hasNextPage/endCursor
    # and surface unresolved threads from EVERY page (not just the first 100).
    g_page1 = ('{"data":{"repository":{"pullRequest":{"reviewThreads":{'
               '"pageInfo":{"hasNextPage":true,"endCursor":"C1"},'
               '"nodes":[{"isResolved":false,"path":"p1.sh","line":1,'
               '"comments":{"nodes":[{"author":{"login":"coderabbitai"}}]}}]}}}}}')
    g_page2 = ('{"data":{"repository":{"pullRequest":{"reviewThreads":{'
               '"pageInfo":{"hasNextPage":false},'
               '"nodes":[{"isResolved":false,"path":"p2.sh","line":2,'
               '"comments":{"nodes":[{"author":{"login":"coderabbitai"}}]}}]}}}}}')
    rc, out, err = run(["--check-resolved", "--allow-stale"], graphql=g_page1, graphql_next=g_page2)
    check("multi-page advisory: page-1 unresolved thread surfaced", "p1.sh:1" in out)
    check("multi-page advisory: page-2 unresolved thread surfaced (pagination followed)", "p2.sh:2" in out)
    check("multi-page advisory: exit 0", rc == 0)

    print("== #239 coverage advisory: gate on the codecov/patch CHECK-RUN, not the comment glyph ==")
    # The codecov comment prints a leading :x: whenever ANY patch line is uncovered,
    # INDEPENDENT of whether the patch threshold passed. Deriving threshold_state from
    # that glyph made a passing gate read "fail" and spuriously paused /merge-pr.
    # The gating truth is the codecov/patch check-run conclusion.
    CODECOV_XMARK = (
        '[{"id":555,"user":{"login":"codecov[bot]"},"created_at":"2026-07-06T00:00:00Z",'
        '"body":":x: Patch coverage is `87.20000%` with `16 lines` in your changes missing coverage.\\n'
        'See [report](https://app.codecov.io/gh/o/r/pull/1)."}]'
    )
    CR_PATCH_PASS = '{"total_count":1,"check_runs":[{"name":"codecov/patch","status":"completed","conclusion":"success"}]}'
    CR_PATCH_FAIL = '{"total_count":1,"check_runs":[{"name":"codecov/patch","status":"completed","conclusion":"failure"}]}'
    CR_NO_CODECOV = '{"total_count":1,"check_runs":[{"name":"gates (ubuntu-latest)","status":"completed","conclusion":"success"}]}'

    # Core bug: :x: glyph in the comment BUT the codecov/patch check-run PASSED -> not fail.
    rc, out, err = run(["--coverage-only"], issue=CODECOV_XMARK, check_runs=CR_PATCH_PASS)
    adv = json.loads(out)
    check("glyph :x: but codecov/patch check-run success -> threshold_state=pass (not fail)",
          adv.get("threshold_state") == "pass")
    check("comment glyph preserved as advisory-only comment_glyph=uncovered",
          adv.get("comment_glyph") == "uncovered")
    check("patch_pct still parsed from the comment (87.2)", adv.get("patch_pct") == 87.2)
    check("coverage-only exit 0", rc == 0)

    # Genuine gating failure: the codecov/patch check-run itself failed -> fail (still gates).
    rc, out, err = run(["--coverage-only"], issue=CODECOV_XMARK, check_runs=CR_PATCH_FAIL)
    adv = json.loads(out)
    check("codecov/patch check-run failure -> threshold_state=fail", adv.get("threshold_state") == "fail")

    # No gating codecov signal: comment present but NO codecov/patch check-run -> none (advisory-only, no pause).
    rc, out, err = run(["--coverage-only"], issue=CODECOV_XMARK, check_runs=CR_NO_CODECOV)
    adv = json.loads(out)
    check("no codecov/patch check-run -> threshold_state=none (advisory-only, never a gating fail)",
          adv.get("threshold_state") == "none")

    # No codecov comment at all -> status none (unchanged contract).
    rc, out, err = run(["--coverage-only"], issue="[]", check_runs=CR_PATCH_PASS)
    adv = json.loads(out)
    check("no codecov comment -> status=none (unchanged)", adv.get("status") == "none")

    print("== #316 --coverage-only: never a silent abort, never an unmeasured number ==")
    # THE DEFECT: codecov changes its WORDING at 100% patch coverage -- it drops the
    # "Patch coverage is `NN%`" line entirely and prints only
    # ":white_check_mark: All modified and coverable lines are covered by tests."
    # The extractor is a grep|head|grep|head pipeline; a non-matching grep exits 1,
    # pipefail propagates it, and `set -e` killed the script AT THAT ASSIGNMENT --
    # before the jq -n that emits the JSON. Measured on 24 of 60 recent stillwater
    # PRs (15 of which had a codecov/patch check-run reporting a real number).
    #
    # It hid because default mode calls build_coverage_advisory inside $(...), and
    # command substitution masks errexit. Only --coverage-only calls it at top level.
    COV_FULL = (
        '[{"id":556,"user":{"login":"codecov[bot]"},"created_at":"2026-07-06T00:00:00Z",'
        '"body":"## Codecov Report\\n:white_check_mark: All modified and coverable lines '
        'are covered by tests.\\nSee [report](https://app.codecov.io/gh/o/r/pull/1)."}]'
    )

    def _cr(title, conclusion="success", status="completed"):
        return json.dumps({"total_count": 1, "check_runs": [{
            "name": "codecov/patch", "status": status, "conclusion": conclusion,
            "output": {"title": title}}]})

    CR_100 = _cr("100.00% of diff hit (target 78.00%)")

    # (1) THE REGRESSION GUARD, and it is AFFIRMATIVE on purpose: the emitted JSON must
    # carry the real number from the check-run, not merely "not crash". A fix that
    # swallowed the error and emitted null would pass an absence-only assertion.
    rc, out, err = run(["--coverage-only"], issue=COV_FULL, check_runs=CR_100)
    check("100%-wording comment: exits 0 (was: silent abort, exit 1, no output)", rc == 0)
    adv = json.loads(out) if out.strip() else {}
    check("100%-wording comment: emits parseable JSON (was: empty stdout)", adv != {})
    check("100%-wording comment: patch_pct=100.0 from the check-run", adv.get("patch_pct") == 100.0)
    check("100%-wording comment: patch_pct_source=check_run (names where the number came from)",
          adv.get("patch_pct_source") == "check_run")
    check("100%-wording comment: no reason set when a number WAS measured",
          adv.get("patch_pct") == 100.0 and adv.get("patch_pct_reason") is None)

    # (2) The comment path still wins when the comment HAS the number -- the check-run
    # is a fallback, not a replacement (the comment carries more precision: 87.20000).
    rc, out, err = run(["--coverage-only"], issue=CODECOV_XMARK, check_runs=CR_100)
    adv = json.loads(out)
    check("comment carries the pct -> comment wins over the check-run title (87.2, not 100.0)",
          adv.get("patch_pct") == 87.2)
    check("comment path names its source", adv.get("patch_pct_source") == "comment")

    # (3) NOT MEASURED must be DISTINGUISHABLE from measured -- the issue's core AC.
    # "Coverage not affected" carries no percentage anywhere.
    rc, out, err = run(["--coverage-only"], issue=COV_FULL,
                       check_runs=_cr("Coverage not affected when comparing a1b2c3d...e4f5g6h"))
    check("no percentage anywhere: exit 0", rc == 0)
    adv = json.loads(out) if out.strip() else {}
    # ANCHOR every null-assertion on status=="present". Without it an EMPTY stdout
    # satisfies "patch_pct is null" vacuously -- the assertion would read as green
    # against the very abort it exists to catch.
    check("no percentage anywhere: advisory was actually produced", adv.get("status") == "present")
    check("no percentage anywhere: patch_pct is null, NEVER 0",
          adv.get("status") == "present" and adv.get("patch_pct") is None)
    check("no percentage anywhere: patch_pct_source is null",
          adv.get("status") == "present" and adv.get("patch_pct_source") is None)
    check("no percentage anywhere: a reason NAMES why (not merely absent)",
          isinstance(adv.get("patch_pct_reason"), str) and adv.get("patch_pct_reason") != "")

    # (4) THE FALSE-NUMBER TRAP: an unanchored regex over the check-run title would
    # harvest the TARGET (78.00) and report it as the patch percentage -- a plausible
    # number nobody measured, which is exactly the failure class this issue names.
    rc, out, err = run(["--coverage-only"], issue=COV_FULL,
                       check_runs=_cr("Coverage not affected (target 78.00%)"))
    adv = json.loads(out) if out.strip() else {}
    check("title with only a TARGET pct: advisory was actually produced",
          adv.get("status") == "present")
    check("title with only a TARGET pct: must NOT report 78.0 as patch coverage",
          adv.get("status") == "present" and adv.get("patch_pct") != 78.0)
    check("title with only a TARGET pct: patch_pct is null",
          adv.get("status") == "present" and adv.get("patch_pct") is None)

    # (5) No codecov/patch check-run at all -> nothing to fall back to.
    rc, out, err = run(["--coverage-only"], issue=COV_FULL, check_runs=CR_NO_CODECOV)
    check("100%-wording comment + no codecov/patch check-run: exit 0", rc == 0)
    adv = json.loads(out) if out.strip() else {}
    check("100%-wording comment + no check-run: patch_pct null with a reason",
          adv.get("patch_pct") is None and isinstance(adv.get("patch_pct_reason"), str))

    # (6) PENDING: the check-run exists but has not concluded. threshold_state stays
    # "none" (unchanged #239 contract) and a title may not exist yet.
    rc, out, err = run(["--coverage-only"], issue=COV_FULL,
                       check_runs=_cr(None, conclusion=None, status="in_progress"))
    check("pending check-run: exit 0", rc == 0)
    adv = json.loads(out) if out.strip() else {}
    check("pending check-run: threshold_state=none (unchanged)", adv.get("threshold_state") == "none")
    check("pending check-run: patch_pct null, never a number",
          adv.get("status") == "present" and adv.get("patch_pct") is None)

    # (7) MALFORMED check-run payload -- must degrade, not abort.
    rc, out, err = run(["--coverage-only"], issue=COV_FULL, check_runs="{not json at all")
    check("malformed check-runs payload: exit 0 (degrades, never aborts)", rc == 0)
    adv = json.loads(out) if out.strip() else {}
    check("malformed check-runs payload: still emits JSON with patch_pct null",
          adv.get("status") == "present" and adv.get("patch_pct") is None)

    # (7b) THE 4xx-BODY-ON-STDOUT CASE, which (7) does NOT cover. gh does not apply
    # --jq on an error: it writes the error body to STDOUT and exits nonzero, so the
    # caller receives a JSON OBJECT that is real JSON but is NOT a check-run -- e.g.
    # {"message":"Not Found",...}. Reading .output.title off it yields empty (benign).
    # Both shapes are tested because they fail DIFFERENTLY: the error object parses
    # cleanly and simply lacks the fields, while a bare string makes jq error outright.
    # Only the second exercises the per-field `2>/dev/null || echo ""` degradation.
    ERR_BODY = '{"message":"Not Found","documentation_url":"https://docs.github.com/rest"}'
    rc, out, err = run(["--coverage-only"], issue=COV_FULL, check_runs=ERR_BODY,
                       extra_env={"CHECK_RUNS_RAW": "1"})
    check("4xx error body on stdout: exit 0", rc == 0)
    adv = json.loads(out) if out.strip() else {}
    check("4xx error body on stdout: advisory still produced", adv.get("status") == "present")
    check("4xx error body on stdout: patch_pct null, never a number from the error payload",
          adv.get("status") == "present" and adv.get("patch_pct") is None)
    check("4xx error body on stdout: threshold_state=none (never a spurious gating fail)",
          adv.get("threshold_state") == "none")

    # A NON-OBJECT raw payload. `jq -r '.conclusion'` over a bare JSON string errors
    # ("Cannot index string with ...") and, under pipefail+errexit, would abort the
    # script. What absorbs it is the `2>/dev/null || echo ""` on EACH jq read -- NOT a
    # `type == "object"` pre-check, which was written and then deliberately REMOVED as
    # dead code (see the comment at the call site). Do not reinstate that guard on the
    # strength of these assertions: they pin the BEHAVIOR, not any one mechanism.
    rc, out, err = run(["--coverage-only"], issue=COV_FULL, check_runs='"Not Found"',
                       extra_env={"CHECK_RUNS_RAW": "1"})
    check("non-object raw payload: exit 0 (the per-field jq guards absorb it)", rc == 0)
    adv = json.loads(out) if out.strip() else {}
    check("non-object raw payload: advisory still produced with patch_pct null",
          adv.get("status") == "present" and adv.get("patch_pct") is None)

    # (8) DEFAULT MODE must not regress: it rendered "?" for the missing pct (errexit
    # was masked by $(...)), and it must keep rendering a non-numeric placeholder --
    # never a fabricated 0 -- while now being able to show the check-run number.
    rc, out, err = run(["--allow-stale"], issue=COV_FULL, check_runs=CR_100)
    check("default mode with 100%-wording comment: exit 0", rc == 0)
    check("default mode surfaces the recovered 100 rather than '?'",
          "Patch coverage: 100" in out)
    check("default mode never prints a fabricated 'Patch coverage: 0%'",
          "Patch coverage:" in out and "Patch coverage: 0%" not in out)

    # (8b) THE NULL case in DEFAULT mode -- which (8) does NOT reach. (8) runs against a
    # payload whose pct is always 100, so it can never observe how a NULL renders, and a
    # hostile pass proved it: mutating the renderer's `.patch_pct // "?"` to `// 0` left
    # every assertion green. An assertion ABOUT a fabricated zero that cannot see a null
    # is decorative. Same defect class as #390's tests -- a check reading stronger than
    # it is -- so it gets an AFFIRMATIVE assertion on the placeholder, not just absence.
    rc, out, err = run(["--allow-stale"], issue=COV_FULL,
                       check_runs=_cr("Coverage not affected when comparing a1b2c3d...e4f5g6h"))
    check("default mode, unmeasured pct: exit 0", rc == 0)
    check("default mode, unmeasured pct: renders the '?' placeholder",
          "Patch coverage: ?%" in out)
    check("default mode, unmeasured pct: NEVER renders it as 0%",
          "Patch coverage: 0%" not in out)

    # (9) THE SIBLING ABORT at the ISSUE-COMMENTS read -- the same defect class as the
    # extraction pipeline, at a different call site, found by a hostile pre-push pass.
    # `gh ... || echo '[]'` CONCATENATES on error (gh writes the body to stdout AND
    # exits nonzero), so the fallback never replaces the garbage; an HTML 5xx body then
    # makes jq exit 5 and kills the script. Fixing one instance of a defect class while
    # its twin survives 20 lines away is how a class reads as closed when it is not.
    for label, payload in [
        ("HTML 5xx body", "<html><head><title>502 Bad Gateway</title></head></html>"),
        ("plain-text error", "Gateway Timeout"),
        ("4xx JSON error object", '{"message":"Not Found","documentation_url":"https://docs.github.com/rest"}'),
        ("bare JSON string", '"Not Found"'),
        ("JSON object, not an array", '{"comments":[]}'),
        # An ARRAY of non-objects passes the `type == "array"` check but makes the
        # select/sort_by pipeline exit 5 ("Cannot index number with string"). This is
        # the case that proves the two guards are COMPLEMENTARY rather than redundant:
        # mutation testing showed either guard alone passed the suite until this shape
        # was covered, which is what a redundant-looking pair looks like when the tests
        # simply never reach the half only one of them catches.
        ("array of numbers", '[1,2,3]'),
        ("array of strings", '["a","b"]'),
        ("array of arrays", '[[]]'),
    ]:
        rc, out, err = run(["--coverage-only"], issue=payload, extra_env={"ISSUE_RAW": "1"})
        check(f"issue-comments {label}: exit 0 (was: abort, empty stdout)", rc == 0)
        adv = json.loads(out) if out.strip() else {}
        check(f"issue-comments {label}: emits the no-codecov contract, never nothing",
              adv.get("status") == "none")

    # A non-numeric comment id must not abort either: it feeds jq --argjson, which
    # REJECTS a JSON string and exits 2.
    STR_ID = ('[{"id":"not-a-number","user":{"login":"codecov[bot]"},'
              '"created_at":"2026-07-06T00:00:00Z","body":":x: Patch coverage is `50.00000%` with `1 line`."}]')
    rc, out, err = run(["--coverage-only"], issue=STR_ID, check_runs=CR_100)
    check("non-numeric comment id: exit 0 (--argjson would have exited 2)", rc == 0)
    adv = json.loads(out) if out.strip() else {}
    check("non-numeric comment id: the advisory still reports the real pct",
          adv.get("status") == "present" and adv.get("patch_pct") == 50.0)

    print("== #252 --itemized: one checkable line per UNADDRESSED finding across all 3 classes ==")
    # An unreplied inline bot comment. path/line must match the GraphQL thread node
    # below so the resolved lookup keys off path AND line.
    ITEM_INLINE = (
        '[{"id":501,"user":{"login":"coderabbitai[bot]"},"in_reply_to_id":null,'
        '"path":"foo.sh","original_line":42,"commit_id":"abcdef1234567",'
        '"body":"Potential issue: fix this bug\\nmore detail"}]'
    )
    # A review-body nitpick with an actionable body and NO inline thread.
    ITEM_REVIEW = (
        '[{"id":111,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"**Actionable comments posted: 1**\\n\\nsome finding"}]'
    )
    # An issue-level actionable Codoki comment.
    ITEM_ISSUE = (
        '[{"id":701,"user":{"login":"codoki-pr-intelligence[bot]"},'
        '"created_at":"2026-06-18T01:00:00Z","updated_at":"2026-06-18T01:00:00Z",'
        '"body":"### Codoki PR Review\\nHigh: something"}]'
    )
    # Resolved is keyed on the thread ROOT comment fullDatabaseId (== the REST inline
    # comment id 501), NOT path+line (#256 CR Major: line drifts on rebase).
    G_ITEM_UNRESOLVED = (
        '{"data":{"repository":{"pullRequest":{"reviewThreads":{'
        '"pageInfo":{"hasNextPage":false},'
        '"nodes":[{"isResolved":false,"path":"foo.sh","line":42,'
        '"comments":{"nodes":[{"fullDatabaseId":"501","author":{"login":"coderabbitai"}}]}}]}}}}}'
    )
    G_ITEM_RESOLVED = (
        '{"data":{"repository":{"pullRequest":{"reviewThreads":{'
        '"pageInfo":{"hasNextPage":false},'
        '"nodes":[{"isResolved":true,"path":"foo.sh","line":42,'
        '"comments":{"nodes":[{"fullDatabaseId":"501","author":{"login":"coderabbitai"}}]}}]}}}}}'
    )

    # (a) review-body-only -> a "review-body | ... | (body) |" line with resolved:n/a.
    rc, out, err = run(["--itemized", "--allow-stale"], reviews=ITEM_REVIEW)
    check("itemized: review-body nitpick -> 'review-body |' line present",
          any(ln.startswith("review-body |") for ln in out.splitlines()))
    check("itemized: review-body line marks (body) and resolved:n/a",
          any(ln.startswith("review-body |") and "(body)" in ln and "resolved:n/a" in ln
              for ln in out.splitlines()))
    # #289: this case used to assert the NOTE said the finding clears when "the reviewer
    # re-reviews a fresh SHA (a maintainer re-trigger)" -- i.e. the TEST PINNED THE FALSE
    # CLAIM. The code never implemented that; the real clearing condition is ack-by-review-id,
    # and the false NOTE is what made the maintainer override the gate on stillwater #2424.
    # Now it asserts the note states the condition the code ACTUALLY implements.
    check("itemized: review-body NOTE states the REAL clearing condition (ack by review id)",
          "NOTE:" in out and "REFERENCES THE REVIEW ID" in out
          and "reply-comment.sh --review" in out
          and "re-review" not in out and "maintainer re-trigger" not in out)
    check("itemized: review-body exit 0", rc == 0)

    # (b) inline unreplied comment -> "inline | ... | path:line |" with resolved from GraphQL.
    rc, out, err = run(["--itemized", "--allow-stale"], inline=ITEM_INLINE,
                       graphql=G_ITEM_UNRESOLVED)
    inline_lines = [ln for ln in out.splitlines() if ln.startswith("inline |")]
    check("itemized: inline unreplied -> 'inline | ... | foo.sh:42 |' line",
          any("foo.sh:42" in ln for ln in inline_lines))
    check("itemized: inline strips the [bot] suffix from the author",
          any(ln.startswith("inline | coderabbitai |") for ln in inline_lines))
    check("itemized: matching GraphQL isResolved=false -> resolved:no",
          any("resolved:no" in ln for ln in inline_lines))
    check("itemized: inline exit 0", rc == 0)

    rc, out, err = run(["--itemized", "--allow-stale"], inline=ITEM_INLINE,
                       graphql=G_ITEM_RESOLVED)
    check("itemized: matching GraphQL isResolved=true -> resolved:yes",
          any(ln.startswith("inline |") and "resolved:yes" in ln for ln in out.splitlines()))

    # (c) issue-level actionable -> "issue-level | ... | (issue) |" line.
    rc, out, err = run(["--itemized", "--allow-stale"], issue=ITEM_ISSUE)
    check("itemized: issue-level actionable -> 'issue-level | ... | (issue) |' line",
          any(ln.startswith("issue-level |") and "(issue)" in ln for ln in out.splitlines()))
    check("itemized: issue-level exit 0", rc == 0)

    # (d) all three classes present -> all three line types emitted.
    rc, out, err = run(["--itemized", "--allow-stale"], inline=ITEM_INLINE,
                       reviews=ITEM_REVIEW, issue=ITEM_ISSUE, graphql=G_ITEM_UNRESOLVED)
    lines = out.splitlines()
    check("itemized: all three -> inline line present",
          any(ln.startswith("inline |") for ln in lines))
    check("itemized: all three -> review-body line present",
          any(ln.startswith("review-body |") for ln in lines))
    check("itemized: all three -> issue-level line present",
          any(ln.startswith("issue-level |") for ln in lines))
    check("itemized: order is inline, then review-body, then issue-level",
          ([ln.split(" |")[0] for ln in lines
            if ln.startswith(("inline |", "review-body |", "issue-level |"))]
           == ["inline", "review-body", "issue-level"]))
    check("itemized: all three exit 0", rc == 0)

    # (e) clean PR -> header with 0 findings, exit 0, no finding lines.
    rc, out, err = run(["--itemized", "--allow-stale"])
    check("itemized: clean PR header says 0 finding(s)",
          "Itemized triage checklist: 0 finding(s)" in out)
    check("itemized: clean PR emits no finding lines",
          not any(ln.startswith(("inline |", "review-body |", "issue-level |"))
                  for ln in out.splitlines()))
    check("itemized: clean PR exit 0", rc == 0)

    # (f) --itemized --count-only -> usage error, exit 1, "mutually exclusive".
    rc, out, err = run(["--itemized", "--count-only"])
    check("itemized + --count-only -> exit 1 (mutually exclusive)",
          rc == 1 and "mutually exclusive" in err)

    # (g) excerpt skips leading HTML noise (real bots lead with an HTML comment /
    # <details> marker) so the checklist line carries the actual finding, not noise.
    ITEM_ISSUE_HTML = (
        '[{"id":702,"user":{"login":"codoki-pr-intelligence[bot]"},'
        '"created_at":"2026-06-18T01:00:00Z","updated_at":"2026-06-18T01:00:00Z",'
        '"body":"<!-- CODOKI_REVIEW_COMMENT -->\\n### Codoki PR Review\\nHigh: real finding"}]'
    )
    rc, out, err = run(["--itemized", "--allow-stale"], issue=ITEM_ISSUE_HTML)
    il = next((ln for ln in out.splitlines() if ln.startswith("issue-level |")), "")
    check("itemized: issue-level excerpt skips the HTML-comment marker",
          "<!--" not in il and "Codoki PR Review" in il)
    ITEM_REVIEW_HTML = (
        '[{"id":112,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"<details>\\n<summary>Nitpick comments (1)</summary>\\n**Actionable comments posted: 2**"}]'
    )
    rc, out, err = run(["--itemized", "--allow-stale"], reviews=ITEM_REVIEW_HTML)
    rl = next((ln for ln in out.splitlines() if ln.startswith("review-body |")), "")
    excerpt = rl.split(" | ")[3] if rl.count(" | ") >= 3 else rl
    check("itemized: review-body excerpt strips HTML tags (no raw < or >)",
          "<" not in excerpt and ">" not in excerpt and "Nitpick comments" in excerpt)

    # (h) HIGH (hostile #1): a CR review body carrying "Outside diff range comments (N)"
    # contributes N+1 to the header count -- the review-body line MUST annotate that
    # subtotal so header == visible accounting (no silent omission of the N sub-findings).
    ITEM_REVIEW_OUTSIDE = (
        '[{"id":113,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"**Actionable comments posted: 1**\\n<summary>Outside diff range comments (6)</summary>"}]'
    )
    rc, out, err = run(["--itemized", "--allow-stale"], reviews=ITEM_REVIEW_OUTSIDE)
    check("itemized: header counts the 6 outside-diff sub-findings (7 total)",
          "7 finding(s)" in out)
    rl = next((ln for ln in out.splitlines() if ln.startswith("review-body |")), "")
    check("itemized: review-body line annotates its outside-diff subtotal (+6)",
          "outside-diff" in rl and "6" in rl)

    # (i) MEDIUM (hostile #2): --itemized --audit must be a usage error, not silently audit.
    rc, out, err = run(["--itemized", "--audit"])
    check("itemized + --audit -> exit 1 (mutually exclusive)",
          rc == 1 and "mutually exclusive" in err)

    # (j) LOW (hostile #3): a literal '|' in a body must not corrupt the pipe columns.
    ITEM_INLINE_PIPE = (
        '[{"id":502,"user":{"login":"coderabbitai[bot]"},"in_reply_to_id":null,'
        '"path":"a.sh","original_line":10,"commit_id":"abcdef1234567",'
        '"body":"use a | b pipe here"}]'
    )
    rc, out, err = run(["--itemized", "--allow-stale"], inline=ITEM_INLINE_PIPE,
                       graphql=G_ITEM_UNRESOLVED)
    il = next((ln for ln in out.splitlines() if ln.startswith("inline |")), "")
    check("itemized: a '|' in the body does not add columns (exactly 5 fields)",
          len(il.split(" | ")) == 5 and il.split(" | ")[4].startswith("replied:"))

    # (k) NIT (hostile #4): an HTML comment containing '>' should not leak into the excerpt.
    ITEM_ISSUE_GTCOMMENT = (
        '[{"id":703,"user":{"login":"codoki-pr-intelligence[bot]"},'
        '"created_at":"2026-06-18T01:00:00Z","updated_at":"2026-06-18T01:00:00Z",'
        '"body":"<!-- a > b noise -->\\nreal finding text"}]'
    )
    rc, out, err = run(["--itemized", "--allow-stale"], issue=ITEM_ISSUE_GTCOMMENT)
    il = next((ln for ln in out.splitlines() if ln.startswith("issue-level |")), "")
    check("itemized: HTML comment containing '>' does not leak into excerpt",
          "noise" not in il and "real finding text" in il)

    # (l) NIT (hostile #5): [bot] suffix stripped on ALL three classes' authors.
    rc, out, err = run(["--itemized", "--allow-stale"], reviews=ITEM_REVIEW, issue=ITEM_ISSUE)
    check("itemized: review-body author strips [bot]",
          any(ln.startswith("review-body | coderabbitai |") for ln in out.splitlines()))
    check("itemized: issue-level author strips [bot]",
          any(ln.startswith("issue-level | codoki-pr-intelligence |") for ln in out.splitlines()))

    # (m) MAJOR (CR #256): resolved is keyed on the comment ID, NOT path+line, so a
    # rebase that moves the thread's current `line` away from the comment's original
    # line must NOT break the match (path+line matching would drop to resolved:? here).
    G_ITEM_LINEDRIFT = (
        '{"data":{"repository":{"pullRequest":{"reviewThreads":{'
        '"pageInfo":{"hasNextPage":false},'
        '"nodes":[{"isResolved":true,"path":"foo.sh","line":999,'  # line drifted from 42
        '"comments":{"nodes":[{"fullDatabaseId":"501","author":{"login":"coderabbitai"}}]}}]}}}}}'
    )
    rc, out, err = run(["--itemized", "--allow-stale"], inline=ITEM_INLINE,
                       graphql=G_ITEM_LINEDRIFT)
    check("itemized: resolved matches by comment ID despite line drift (rebase-safe)",
          any(ln.startswith("inline |") and "resolved:yes" in ln for ln in out.splitlines()))

    # (n) Nitpick (CR #256): a GraphQL failure for --itemized renders inline resolved:?.
    rc, out, err = run(["--itemized", "--allow-stale"], inline=ITEM_INLINE,
                       graphql="not-json")
    check("itemized: GraphQL failure -> inline resolved:? (best-effort degrade)",
          any(ln.startswith("inline |") and "resolved:?" in ln for ln in out.splitlines()))
    check("itemized: GraphQL failure does not change exit (still 0)", rc == 0)

    print("== #259: flags are position-independent; a flag after <pr> is NOT swallowed as [repo] ==")
    # The bug: `<pr> --count-only` left --count-only as $2=repo -> gh api repos/--count-only/...
    # -> cryptic 404. After the fix the flag is parsed wherever it sits and the default repo
    # (or a real one) is used. --count-only is the simplest mode to assert on (prints a number).
    rc, out, err = run_argv(["123", "--count-only", "owner/repo"])
    check("#259: `<pr> --count-only <repo>` runs count mode (not repos/--count-only 404)",
          rc == 0 and "--count-only" not in err and out.strip().isdigit())
    # A flag after <pr> with the default repo omitted must also work (the natural form).
    rc, out, err = run_argv(["123", "--count-only"])
    check("#259: `<pr> --count-only` (default repo) runs count mode", rc == 0 and out.strip().isdigit())
    # Regression: the documented flags-BEFORE order still works.
    rc, out, err = run_argv(["--count-only", "123", "owner/repo"])
    check("#259: `--count-only <pr> <repo>` (flags first) still works", rc == 0 and out.strip().isdigit())
    # A genuinely unknown flag ANYWHERE fails LOUDLY (usage error), never a cryptic 404.
    rc, out, err = run_argv(["123", "--bogus", "owner/repo"])
    check("#259: `<pr> --bogus` -> usage error exit 1 (loud, not 404)",
          rc == 1 and "bogus" in (out + err).lower())
    # A bare `-x`-style token after <pr> is also a flag, not a repo -> loud usage error.
    rc, out, err = run_argv(["123", "-x"])
    check("#259: `<pr> -x` -> usage error exit 1 (a dash-token is never a repo)", rc == 1)
    # Too many positionals (a second bare token after the repo) -> usage error.
    rc, out, err = run_argv(["123", "owner/repo", "extra"])
    check("#259: extra positional after <pr> [repo] -> usage error exit 1", rc == 1)

    # --- #272: --count-only must not over-count informational bot summaries -------
    # A docs-only PR whose only issue-level bot comments are (a) an ACKED Codoki
    # review summary and (b) a CODOKI_INFO post must yield --count-only = 0 (matching
    # --audit), not 2.
    ACKED_SUMMARY = ('{"id":901,"user":{"login":"codoki-pr-intelligence[bot]"},'
                     '"body":"<!-- CODOKI_REVIEW_COMMENT -->\\n### Codoki PR Review\\nSummary",'
                     '"created_at":"2026-06-18T01:00:00Z","updated_at":"2026-06-18T01:00:00Z",'
                     '"reactions":{"total_count":1,"+1":1,"-1":0}}')
    CODOKI_INFO = ('{"id":902,"user":{"login":"codoki-pr-intelligence[bot]"},'
                   '"body":"<!-- CODOKI_INFO -->\\nHeads-up, informational only",'
                   '"created_at":"2026-06-18T01:00:00Z","updated_at":"2026-06-18T01:00:00Z",'
                   '"reactions":{"total_count":0,"+1":0,"-1":0}}')
    UNACKED_SUMMARY = ('{"id":903,"user":{"login":"codoki-pr-intelligence[bot]"},'
                       '"body":"<!-- CODOKI_REVIEW_COMMENT -->\\n### Codoki PR Review\\nSummary",'
                       '"created_at":"2026-06-18T01:00:00Z","updated_at":"2026-06-18T01:00:00Z",'
                       '"reactions":{"total_count":0,"+1":0,"-1":0}}')
    rc, out, err = run(["--count-only"], issue="[" + ACKED_SUMMARY + "," + CODOKI_INFO + "]")
    check("#272: acked Codoki summary + CODOKI_INFO -> --count-only = 0",
          rc == 0 and out.strip() == "0")
    # CODOKI_INFO alone is never a finding.
    rc, out, err = run(["--count-only"], issue="[" + CODOKI_INFO + "]")
    check("#272: CODOKI_INFO alone -> --count-only = 0", rc == 0 and out.strip() == "0")
    # An UNACKED Codoki summary INTENTIONALLY still counts (the ack is a real pending
    # action; ship-gate-preflight.sh BLOCKs on it) - do not over-correct to 0.
    rc, out, err = run(["--count-only"], issue="[" + UNACKED_SUMMARY + "]")
    check("#272: UNACKED Codoki summary -> --count-only = 1 (still actionable)",
          rc == 0 and out.strip() == "1")

    # ---- #289: the ship-gate FALSE BLOCK (stillwater #2424) ----
    # A pure outside-diff CR review clears ONLY via ack-by-REVIEW-ID (a $me comment created
    # after submitted_at whose body CONTAINS the review id). The maintainer replied "fixed in
    # <sha>" with no review id -> never acked -> BLOCK forever, while the helper's own NOTE
    # told them it could only be cleared by a maintainer re-trigger. They overrode the gate.
    print("\n== #289: review-body findings -- the ack channel must be DISCOVERABLE ==")
    OUTSIDE_ONLY = (
        '[{"id":4680966542,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"> [!CAUTION]\\n<summary>Outside diff range comments (1)</summary>"}]'
    )

    # The BLOCK/report path must NAME the exact clearing action, with the review id. Without
    # this the finding is undiscoverable-in-practice and the lead overrides the gate.
    rc, out, err = run([], reviews=OUTSIDE_ONLY)
    both = out + err
    check("#289: report names the clearing command (reply-comment.sh --review)",
          "reply-comment.sh --review" in both)
    check("#289: report names the REVIEW ID to ack", "4680966542" in both)

    # The wrong NOTE must be gone: the code does NOT implement 'the reviewer re-reviews a
    # fresh SHA / a maintainer re-trigger'. That false line is what caused the override.
    # NB: assert against --itemized, where the NOTE actually PRINTS. An earlier version of this
    # case ran default mode, which never emits the NOTE at all -- so it passed VACUOUSLY and
    # stayed GREEN even with the false NOTE restored verbatim (proved by mutation in review).
    rc_i, out_i, err_i = run(["--itemized", "--allow-stale"], reviews=OUTSIDE_ONLY)
    both_i = out_i + err_i
    check("#289: the false 'maintainer re-trigger' NOTE is gone (asserted in --itemized, "
          "where the NOTE actually prints)",
          "NOTE:" in both_i and "maintainer re-trigger" not in both_i
          and "re-reviews a fresh SHA" not in both_i)

    # An id-stamped ack CLEARS it (proving the channel works and the agent can self-serve).
    ACK = ('[{"id":9001,"user":{"login":"testuser"},"created_at":"2026-06-18T03:00:00Z",'
           '"body":"Fixed in abc1234. Acking CR review 4680966542."}]')
    rc, out, err = run(["--count-only"], reviews=OUTSIDE_ONLY, issue=ACK)
    check("#289: an ID-STAMPED ack clears the review-body finding (count 0)",
          rc == 0 and out.strip() == "0")

    # A bare 'fixed in <sha>' reply WITHOUT the review id must NOT clear it -- that is the
    # real #2424 behavior and it is CORRECT (the finding was real). It must merely be
    # DISCOVERABLE, not auto-cleared.
    NO_ID = ('[{"id":9002,"user":{"login":"testuser"},"created_at":"2026-06-18T03:00:00Z",'
             '"body":"@coderabbitai Valid finding, fixed in 16f8e332."}]')
    rc, out, err = run(["--count-only"], reviews=OUTSIDE_ONLY, issue=NO_ID)
    check("#289: a reply WITHOUT the review id does NOT clear it (finding stays real)",
          rc == 0 and out.strip() != "0")

    # REGRESSION GUARD (the DANGEROUS fix that was proposed and rejected): an APPROVED review
    # on HEAD must NEVER clear an earlier COMMENTED review's outside-diff finding. CR's
    # incremental review only examines the diff SINCE its last review, so a finding on code the
    # fix push never touched is never re-examined: APPROVED means "nothing new in the
    # increment", NOT "the old finding is fixed". Reverting this would let an ignored
    # outside-diff Major merge behind an increment-only approve (#31, #132, stillwater#1931).
    APPROVED_AFTER = (
        '[{"id":4680966542,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"<summary>Outside diff range comments (1)</summary>"},'
        '{"id":4681018833,"user":{"login":"coderabbitai[bot]"},"state":"APPROVED",'
        '"submitted_at":"2026-06-18T05:00:00Z","body":""}]'
    )
    rc, out, err = run(["--count-only"], reviews=APPROVED_AFTER)
    check("#289 REGRESSION GUARD: a later APPROVED review does NOT clear an earlier "
          "COMMENTED outside-diff finding (#31/#132/stillwater#1931)",
          rc == 0 and out.strip() != "0")

    # An issue-level bot comment carrying a DECLARED INFORMATIONAL marker is not actionable.
    # (github-actions[bot] STAYS in the bot set -- it is the generic actor for real
    # workflow-posted security/lint findings, so no login-based blanket exclusion.)
    INFO_MARKED = ('[{"id":9100,"user":{"login":"github-actions[bot]"},'
                   '"created_at":"2026-06-18T03:00:00Z",'
                   '"body":"<!-- MY_BOT_INFO -->\\n## Nightly status\\nAll green."}]')
    rc, out, err = run(["--count-only"], issue=INFO_MARKED,
                       extra_env={"PR_INFO_MARKERS": "MY_BOT_INFO"})
    check("#289: a bot comment with a DECLARED informational marker -> not actionable (0)",
          rc == 0 and out.strip() == "0")

    # CRITICAL REGRESSION (caught in review): an IDENTITY marker must NEVER be used to exclude.
    # `docs-drift-bot` was briefly a DEFAULT marker -- but the workflow stamps that same marker on
    # EVERY body so it can upsert its own comment, including "## Docs drift: MERGE BLOCKED". So the
    # exclusion silently suppressed a genuinely BLOCKING finding: the exact recall loss this gate
    # exists to prevent. Only an INFORMATIONAL-ONLY marker class qualifies. Both bodies below carry
    # the identity marker; NEITHER may be excluded by default.
    DRIFT_BLOCKED = ('[{"id":9103,"user":{"login":"github-actions[bot]"},'
                     '"created_at":"2026-06-18T03:00:00Z",'
                     '"body":"<!-- docs-drift-bot -->\\n## Docs drift: MERGE BLOCKED\\n'
                     'Merge is blocked until you update docs/."}]')
    rc, out, err = run(["--count-only"], issue=DRIFT_BLOCKED)
    check("#289 CRITICAL: a docs-drift MERGE-BLOCKED comment is STILL actionable "
          "(an identity/upsert marker must never be an informational exclusion)",
          rc == 0 and out.strip() == "1")

    # ...but a github-actions comment WITHOUT the marker is STILL actionable (a real
    # workflow-posted finding must not be lost).
    REAL_FINDING = ('[{"id":9101,"user":{"login":"github-actions[bot]"},'
                    '"created_at":"2026-06-18T03:00:00Z",'
                    '"body":"Security scan: 2 high-severity findings in pkg/auth."}]')
    rc, out, err = run(["--count-only"], issue=REAL_FINDING)
    check("#289: an UNMARKED github-actions finding is STILL actionable (no login blanket)",
          rc == 0 and out.strip() == "1")

    # HARDENING (hostile review, lens 1) -- two ways the first cut of the marker exclusion
    # SILENTLY WEAKENED a fail-closed merge gate. Both are pinned here so they cannot return.
    #
    # (1) QUOTING ATTACK / accident: a REAL finding that merely MENTIONS the marker string
    # must NOT be suppressed. The matcher must require the marker to be the body's LEADING,
    # COMPLETE HTML comment -- not a substring anywhere in the text. (CodeRabbit quotes diff
    # snippets in its walkthrough, and this repo family ships that very marker, so a real
    # finding ABOUT the marker is not hypothetical.)
    # NB: this MUST quote a marker that is actually IN the default set (CODOKI_INFO). An earlier
    # version quoted `docs-drift-bot`, which a later fix REMOVED from the defaults -- so the case
    # passed because the marker was not in the matcher, NOT because the matcher was anchored, and
    # it stayed GREEN when the anchoring was reverted. A fix had silently broken the guard.
    QUOTES_MARKER = ('[{"id":9102,"user":{"login":"coderabbitai[bot]"},'
                     '"created_at":"2026-06-18T03:00:00Z",'
                     '"body":"Potential issue: the workflow writes `<!-- CODOKI_INFO -->` '
                     'but the gate never reads it. Fix before merge."}]')
    rc, out, err = run(["--count-only"], issue=QUOTES_MARKER)
    check("#289 HARDENING: a real finding that QUOTES the marker is NOT suppressed "
          "(matcher is anchored to a leading, complete HTML comment)",
          rc == 0 and out.strip() == "1")

    # (2) REGEX INJECTION: PR_INFO_MARKERS used to be interpolated as a raw regex fragment
    # INSIDE the capture group, so an unbalanced ')' escaped it. `x)|(.*` -- or the plausible
    # TYPO `CODOKI_INFO)|(` -- produced an alternation matching EVERY comment: exit 0, count 0,
    # NO WARNING. Every issue-level finding silently suppressed on a fail-closed gate. The
    # matcher now takes marker NAMES only and REFUSES a malformed value LOUDLY.
    for bad in ("x)|(.*", "CODOKI_INFO)|(", ".*"):
        rc, out, err = run(["--count-only"], issue=REAL_FINDING,
                           extra_env={"PR_INFO_MARKERS": bad})
        check(f"#289 HARDENING: injected PR_INFO_MARKERS {bad!r} -> REFUSED loudly, "
              f"never a silent count of 0",
              rc != 0 and "invalid PR_INFO_MARKERS" in err and out.strip() != "0")

    # A well-formed custom marker list still works.
    rc, out, err = run(["--count-only"], issue=REAL_FINDING,
                       extra_env={"PR_INFO_MARKERS": "MY_BOT_INFO"})
    check("#289 HARDENING: a VALID custom marker list is accepted (real finding still counted)",
          rc == 0 and out.strip() == "1")

    # (3) GLOB: the marker split is an unquoted expansion, so without `set -f` the value also
    # undergoes PATHNAME EXPANSION and the matcher is built from FILENAMES in the cwd -- silently,
    # with the gate's behavior depending on where it was invoked from. A glob must be REFUSED.
    rc, out, err = run(["--count-only"], issue=REAL_FINDING, extra_env={"PR_INFO_MARKERS": "*"})
    check("#289 HARDENING: a GLOB in PR_INFO_MARKERS is refused (set -f; matcher never built "
          "from cwd filenames)",
          rc != 0 and "invalid PR_INFO_MARKERS" in err)

    # (4) The SIBLING marker (CODOKI_REVIEW_COMMENT) must be anchored too -- same defect class,
    # one line away, and it was left unanchored for a round.
    QUOTES_SIBLING = ('[{"id":9104,"user":{"login":"coderabbitai[bot]"},'
                      '"created_at":"2026-06-18T03:00:00Z",'
                      '"body":"Bug: we emit `<!-- CODOKI_REVIEW_COMMENT -->` but never parse it.",'
                      '"reactions":{"+1":1}}]')
    rc, out, err = run(["--count-only"], issue=QUOTES_SIBLING)
    check("#289 HARDENING: a finding QUOTING the CODOKI_REVIEW_COMMENT marker is not suppressed "
          "(sibling matcher anchored too)",
          rc == 0 and out.strip() == "1")

    # PR #290 (Copilot): anchoring ALONE is not enough -- the marker must also be TERMINATED.
    # A body merely STARTING with `<!-- CODOKI_REVIEW_COMMENT` (no `-->`) was still treated as a
    # Codoki summary: a self-suppression channel, and inconsistent with INFO_MARKERS_RE, which
    # requires a COMPLETE leading HTML comment.
    UNTERMINATED = ('[{"id":9105,"user":{"login":"coderabbitai[bot]"},'
                    '"created_at":"2026-06-18T03:00:00Z",'
                    '"body":"<!-- CODOKI_REVIEW_COMMENT this is not a closed comment\\n'
                    'High: a real finding hiding behind an unterminated marker.",'
                    '"reactions":{"+1":1}}]')
    rc, out, err = run(["--count-only"], issue=UNTERMINATED)
    check("#290: an UNTERMINATED CODOKI_REVIEW_COMMENT marker does NOT suppress the comment "
          "(marker must be a COMPLETE leading HTML comment)",
          rc == 0 and out.strip() == "1")

    # (5) The report must show the BREAKDOWN, not just a summed integer (a bare "2 findings" for
    # ONE finding is what fed the "the oracle cries wolf" read that ends in an override).
    rc, out, err = run(["--allow-stale"], reviews=CR_BODY_1_PLUS_6)
    check("#289: the report shows the total AND its breakdown (bodies + outside-diff)",
          "body/bodies" in out and "outside-diff" in out)

    print()
    print("== #374: Copilot SUPPRESSED findings must survive the review-body filter ==")
    # Copilot parks findings it declined to post inline inside a
    # "<details><summary>Suppressed comments (N)</summary>" block in the review BODY.
    # Two predicates conspired to drop every such body before that block was examined:
    # the CodeRabbit-shaped keyword allowlist (measured: 0 of 99 real Copilot bodies match
    # it) and, redundantly, a "^## Pull request overview" exclusion. Net effect: --itemized
    # reported 0 findings on a PR carrying real ones, and 0 is what /handle-review and
    # ship-gate-preflight read as "clean".
    COPILOT_SUPPRESSED_2 = (
        '[{"id":333,"user":{"login":"copilot-pull-request-reviewer[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"## Pull request overview\\n\\nThis PR does things.\\n\\n'
        '<details><summary>Suppressed comments (2)</summary>\\n\\n'
        '**internal/publish/thing_test.go:96**\\n* the wg.Wait() can hang the test process\\n'
        '**internal/publish/other.go:12**\\n* stale doc comment\\n</details>"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=COPILOT_SUPPRESSED_2)
    n = findings_count(out)
    # 1 surviving body + 2 suppressed items = 3. Before the fix this was 0.
    check("#374: a Copilot body with 'Suppressed comments (2)' is NOT dropped, and the "
          "count SUMS N rather than counting the body as 1 (expect 3)", n == 3)
    check("#374: exit 0", rc == 0)

    rc, out, err = run(["--itemized", "--allow-stale"], reviews=COPILOT_SUPPRESSED_2)
    check("#374: --itemized surfaces the suppressed body (the channel /handle-review reads)",
          "0 finding(s)" not in out)

    # The header total must RECONCILE with the visible rows (#252's accounting rule). The
    # CR path already annotates "[+N outside-diff]"; without the matching suppressed
    # annotation the row reads as a bland summary while N real findings hide behind it,
    # which is how a triager talks themselves into an override. Caught pre-push on #374.
    check("#374: --itemized ANNOTATES the suppressed subtotal, so the header count equals "
          "the visible accounting (not a bare row hiding N findings)",
          "[+2 suppressed]" in out)

    # Two Copilot submissions: the sum must aggregate across BOTH, never latest-per-reviewer
    # (same argument as the #132 outside-diff sum -- a later APPROVED review does not clear
    # an earlier submission's suppressed findings).
    COPILOT_TWO_SUBMISSIONS = (
        '[{"id":333,"user":{"login":"copilot-pull-request-reviewer[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"## Pull request overview\\n<details><summary>Suppressed comments (2)</summary>x</details>"},'
        '{"id":444,"user":{"login":"copilot-pull-request-reviewer[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T03:00:00Z",'
        '"body":"## Pull request overview\\n<details><summary>Suppressed comments (1)</summary>y</details>"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=COPILOT_TWO_SUBMISSIONS)
    n = findings_count(out)
    check("#374: suppressed counts SUM across both submissions (2 bodies + 2 + 1 = 5)", n == 5)

    # BOILERPLATE MUST STAY FILTERED. 58 of the 99 measured Copilot bodies are pure
    # boilerplate; admitting them would double the checklist with noise, which is the
    # correct DIRECTION of error but avoidable. The anchor is the literal <summary>
    # element, not the prose.
    COPILOT_BOILERPLATE = (
        '[{"id":555,"user":{"login":"copilot-pull-request-reviewer[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"## Pull request overview\\n\\nCopilot reviewed 3 files and generated no new comments."}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=COPILOT_BOILERPLATE)
    n = findings_count(out)
    check("#374: a boilerplate Copilot body (no suppressed block) stays FILTERED (expect 0)",
          n in (0, None))

    # A "(0)" block must NOT admit the body. Admitting it would count the body itself as
    # 1 finding while it holds none -- the cries-wolf direction that ends in an override.
    COPILOT_SUPPRESSED_ZERO = (
        '[{"id":666,"user":{"login":"copilot-pull-request-reviewer[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"## Pull request overview\\n<details><summary>Suppressed comments (0)</summary></details>"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=COPILOT_SUPPRESSED_ZERO)
    n = findings_count(out)
    check("#376 review: 'Suppressed comments (0)' does NOT admit the body (expect 0, not 1)",
          n in (0, None))

    # The Copilot login is NOT stable across installations: this repo's reviewer posts as
    # "Copilot" while stillwater sees "copilot-pull-request-reviewer[bot]". The matcher is
    # therefore keyed on the BODY element, not the login -- a login allowlist would silently
    # drop findings under a third spelling, which is the exact silent-zero #374 fixed.
    COPILOT_ALT_LOGIN = (
        '[{"id":777,"user":{"login":"Copilot"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"## Pull request overview\\n<details><summary>Suppressed comments (2)</summary>x</details>"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=COPILOT_ALT_LOGIN)
    n = findings_count(out)
    check("#376 review: the OTHER real Copilot login ('Copilot', as seen on this repo) is "
          "surfaced too -- the matcher keys on the body element, not an unstable login", n == 3)

    print()
    print("== #377: an absurd sub-finding count must never ZERO the gate ==")
    # Both sums land in a bash $(( )), which is 64-bit; jq emits arbitrary precision.
    # An overflowing N made the arithmetic fail and swallowed the WHOLE count -- the
    # fail-OPEN direction on a fail-CLOSED oracle. The dangerous part is not the
    # malformed body itself but that it ERASES unrelated real findings from the same
    # run, so the load-bearing case is a REAL CodeRabbit finding sitting beside it.
    HUGE = "9223372036854775806"
    CR_REAL_PLUS_HUGE_COPILOT = (
        '[{"id":111,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"<summary>Outside diff range comments (6)</summary>"},'
        '{"id":222,"user":{"login":"copilot-pull-request-reviewer[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T03:00:00Z",'
        '"body":"## Pull request overview\\n<details><summary>Suppressed comments (' + HUGE + ')</summary>x</details>"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=CR_REAL_PLUS_HUGE_COPILOT)
    n = findings_count(out)
    check("#377: a real CR finding is NOT erased by an overflowing Copilot count "
          "(count must be >0, never swallowed)", n is not None and n > 0)
    check("#377: the overflow path still BLOCKS (exit 0 with a nonzero count, never a "
          "silent 'no unreplied comments')", "No unreplied bot comments" not in out)

    # jq renders a sufficiently large integer in float form, which bash then truncates
    # into a plausible-looking but meaningless number.
    FLOATY = "99999999999999999999999999999999999"
    COPILOT_FLOATY = (
        '[{"id":333,"user":{"login":"copilot-pull-request-reviewer[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"## Pull request overview\\n<details><summary>Suppressed comments (' + FLOATY + ')</summary>x</details>"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=COPILOT_FLOATY)
    n = findings_count(out)
    check("#377: a float-rendered huge count is clamped to a sane number, not a "
          "truncation artifact", n is not None and 0 < n <= 100001)

    # Same hazard on the PRE-EXISTING CodeRabbit path -- clamping one and not the other
    # is how a half-closed hole reads as closed.
    CR_HUGE = (
        '[{"id":444,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"<summary>Outside diff range comments (' + HUGE + ')</summary>"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=CR_HUGE)
    n = findings_count(out)
    check("#377: the CodeRabbit outside-diff sum is clamped too (both paths, or the "
          "hole is only half closed)", n is not None and 0 < n <= 100001)

    print()
    print("== #378: an inline reply must not clear a review's BODY-level findings ==")
    # A review carrying BOTH inline comments and a body-level block routed down the
    # "has inline comments" branch, where acked_by_reference was never consulted. So
    # replying to the inline comment alone cleared the whole review and its body-level
    # findings vanished, uncounted and unread. The shape is real: stillwater#3014
    # review 4913081288 ("generated 1 comment" + Suppressed comments (3)).
    #
    # This is the SHARED addressed-state machine, so the CodeRabbit outside-diff case
    # has the identical hole and both are asserted here. Fixing one would leave the
    # other reading as fixed.
    MIXED_COPILOT = (
        '[{"id":888,"user":{"login":"copilot-pull-request-reviewer[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"## Pull request overview\\n<details><summary>Suppressed comments (3)</summary>x</details>"}]'
    )
    # One inline comment belonging to that review, already REPLIED (absent from unreplied).
    INLINE_REPLIED = (
        '[{"id":9501,"user":{"login":"copilot-pull-request-reviewer[bot]"},'
        '"pull_request_review_id":888,"path":"a.sh","original_line":1,'
        '"created_at":"2026-06-18T02:00:00Z","commit_id":"abcdef1234",'
        '"body":"an inline finding"},'
        '{"id":9601,"user":{"login":"testuser"},"in_reply_to_id":9501,'
        '"path":"a.sh","original_line":1,"created_at":"2026-06-18T04:00:00Z",'
        '"commit_id":"abcdef1234","body":"fixed in abc1234"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=MIXED_COPILOT, inline=INLINE_REPLIED)
    n = findings_count(out)
    check("#378: inline REPLIED but 3 suppressed body findings unacked -> still reported "
          "(the body findings do not ride out on the inline reply)",
          n == 4)  # 1 body + 3 suppressed, exact

    MIXED_CR = (
        '[{"id":889,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"<summary>Outside diff range comments (2)</summary>"}]'
    )
    INLINE_REPLIED_CR = (
        '[{"id":9502,"user":{"login":"coderabbitai[bot]"},'
        '"pull_request_review_id":889,"path":"a.sh","original_line":1,'
        '"created_at":"2026-06-18T02:00:00Z","commit_id":"abcdef1234",'
        '"body":"an inline finding"},'
        '{"id":9602,"user":{"login":"testuser"},"in_reply_to_id":9502,'
        '"path":"a.sh","original_line":1,"created_at":"2026-06-18T04:00:00Z",'
        '"commit_id":"abcdef1234","body":"fixed in abc1234"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=MIXED_CR, inline=INLINE_REPLIED_CR)
    n = findings_count(out)
    check("#378: the SHARED hole is fixed for CodeRabbit outside-diff too, not just "
          "Copilot (same branch, same defect)", n == 3)  # 1 body + 2 outside-diff, exact

    # NO WEDGE, and NO over-blocking: once the review id is acked, the body findings
    # clear even though the inline comment was replied to separately. Both channels
    # must be satisfiable, or the fix converts a recall hole into a permanent block.
    ACK_888 = ('[{"id":9601,"user":{"login":"testuser"},'
               '"created_at":"2026-06-18T05:00:00Z",'
               '"body":"Addressed the suppressed findings in review 888 - fixed in abc1234."}]')
    rc, out, err = run(["--allow-stale"], reviews=MIXED_COPILOT,
                       inline=INLINE_REPLIED, issue=ACK_888)
    n = findings_count(out)
    check("#378: inline replied AND review id acked -> fully cleared (the fix must not "
          "create an unclearable state)", n in (0, None))

    # A review whose body carries NO sub-finding block keeps the old behavior exactly:
    # an inline reply clears it. This is the regression guard on the shared machine --
    # a round summary is not a finding.
    PLAIN_REVIEW = (
        '[{"id":890,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"**Actionable comments posted: 1**\\n\\nNitpick: a plain round summary."}]'
    )
    INLINE_REPLIED_PLAIN = (
        '[{"id":9503,"user":{"login":"coderabbitai[bot]"},'
        '"pull_request_review_id":890,"path":"a.sh","original_line":1,'
        '"created_at":"2026-06-18T02:00:00Z","commit_id":"abcdef1234",'
        '"body":"an inline finding"},'
        '{"id":9603,"user":{"login":"testuser"},"in_reply_to_id":9503,'
        '"path":"a.sh","original_line":1,"created_at":"2026-06-18T04:00:00Z",'
        '"commit_id":"abcdef1234","body":"fixed in abc1234"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=PLAIN_REVIEW, inline=INLINE_REPLIED_PLAIN)
    n = findings_count(out)
    check("#378 REGRESSION GUARD: a review with NO body-level block still clears on an "
          "inline reply alone (unchanged behavior)", n in (0, None))

    # PROSE MENTIONING the phrase is not a finding. Without the <summary> element anchor
    # a body saying "will use Outside diff range comments (3) next round" would make its
    # review unclearable by an inline reply. Measured across two repos: every real
    # occurrence carries the element; zero appear as bare prose. (#377/#378 pre-push review)
    PROSE_MENTION = (
        '[{"id":891,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"**Actionable comments posted: 1**\\nWill use Outside diff range comments (3) next round."}]'
    )
    INLINE_REPLIED_PROSE = (
        '[{"id":9504,"user":{"login":"coderabbitai[bot]"},'
        '"pull_request_review_id":891,"path":"a.sh","original_line":1,'
        '"created_at":"2026-06-18T02:00:00Z","commit_id":"abcdef1234",'
        '"body":"an inline finding"},'
        '{"id":9604,"user":{"login":"testuser"},"in_reply_to_id":9504,'
        '"path":"a.sh","original_line":1,"created_at":"2026-06-18T04:00:00Z",'
        '"commit_id":"abcdef1234","body":"fixed in abc1234"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=PROSE_MENTION, inline=INLINE_REPLIED_PROSE)
    n = findings_count(out)
    check("#378: a PROSE mention of 'Outside diff range comments (N)' is not a finding "
          "(anchored on the <summary> element, so an inline reply still clears it)",
          n in (0, None))

    # A "(0)" collapsible holds no findings: blocking on it would mean gating a body
    # whose own annotation reads "0 outside-diff + 0 suppressed".
    CR_ZERO_BLOCK = (
        '[{"id":892,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"> <summary>Outside diff range comments (0)</summary><blockquote>"}]'
    )
    INLINE_REPLIED_ZERO = (
        '[{"id":9505,"user":{"login":"coderabbitai[bot]"},'
        '"pull_request_review_id":892,"path":"a.sh","original_line":1,'
        '"created_at":"2026-06-18T02:00:00Z","commit_id":"abcdef1234",'
        '"body":"an inline finding"},'
        '{"id":9605,"user":{"login":"testuser"},"in_reply_to_id":9505,'
        '"path":"a.sh","original_line":1,"created_at":"2026-06-18T04:00:00Z",'
        '"commit_id":"abcdef1234","body":"fixed in abc1234"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=CR_ZERO_BLOCK, inline=INLINE_REPLIED_ZERO)
    n = findings_count(out)
    check("#378: an outside-diff '(0)' block does not gate a replied review "
          "(positive counts only, matching the suppressed leg)", n in (0, None))

    # The REAL CodeRabbit shape carries an emoji between <summary> and the phrase
    # (`> <summary>WARNING Outside diff range comments (1)</summary><blockquote>`), so
    # the element anchor must tolerate intervening characters or it would match nothing
    # in production while passing a tidier fixture.
    CR_REAL_SHAPE = (
        '[{"id":893,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"> <summary>\\u26a0\\ufe0f Outside diff range comments (1)</summary><blockquote>x"}]'
    )
    INLINE_REPLIED_REAL = (
        '[{"id":9506,"user":{"login":"coderabbitai[bot]"},'
        '"pull_request_review_id":893,"path":"a.sh","original_line":1,'
        '"created_at":"2026-06-18T02:00:00Z","commit_id":"abcdef1234",'
        '"body":"an inline finding"},'
        '{"id":9606,"user":{"login":"testuser"},"in_reply_to_id":9506,'
        '"path":"a.sh","original_line":1,"created_at":"2026-06-18T04:00:00Z",'
        '"commit_id":"abcdef1234","body":"fixed in abc1234"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=CR_REAL_SHAPE, inline=INLINE_REPLIED_REAL)
    n = findings_count(out)
    check("#378: the REAL CR shape (emoji between <summary> and the phrase) is still "
          "detected -- the anchor tolerates intervening characters", n == 2)  # 1 body + 1, exact

    # THE SUMS AND THE CLEARING PREDICATE MUST AGREE. The predicate anchors on the
    # <summary> element with a positive count; if the SUMS stay unanchored they add
    # findings for prose mentions and (0) blocks that the predicate refuses to gate on,
    # so ship-gate-preflight parses a total larger than the accounting a maintainer can
    # clear. A count nobody can drive to zero is the wedge shape. (CodeRabbit, PR #380)
    PROSE_ONLY = (
        '[{"id":894,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"<summary>Suppressed comments (1)</summary>\\nAlso: Outside diff range comments (7) is the format we use."}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=PROSE_ONLY)
    n = findings_count(out)
    check("#380 review: a PROSE mention does not inflate outside_diff_sum "
          "(1 body + 1 suppressed = 2, not 9)", n == 2)

    CR_ZERO_SUM = (
        '[{"id":895,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
        '"submitted_at":"2026-06-18T02:00:00Z",'
        '"body":"<summary>Suppressed comments (1)</summary>\\n<summary>Outside diff range comments (0)</summary>"}]'
    )
    rc, out, err = run(["--allow-stale"], reviews=CR_ZERO_SUM)
    n = findings_count(out)
    check("#380 review: an outside-diff '(0)' adds nothing to the sum "
          "(1 body + 1 suppressed = 2)", n == 2)

    # The ack channel already exists and must still clear a suppressed body: N items share
    # ONE review id, so one reply-comment.sh --review <id> ack clears all N together.
    # Verified in production on stillwater#3018. This is why per-ITEM itemization would
    # introduce a wedge and was deliberately deferred.
    ACK = ('[{"id":9001,"user":{"login":"testuser"},'
           '"created_at":"2026-06-18T04:00:00Z",'
           '"body":"Fixed in abc1234. (Replying here because the finding arrived as a '
           'suppressed comment in review 333.)"}]')
    rc, out, err = run(["--allow-stale"], reviews=COPILOT_SUPPRESSED_2, issue=ACK)
    n = findings_count(out)
    check("#374: an ack REFERENCING the review id clears the body AND its suppressed items "
          "(no wedge: one ack clears all N)", n in (0, None))

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
