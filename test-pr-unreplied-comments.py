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
PULL = '{"head":{"sha":"%s"}}' % HEAD_SHA
COMMIT = '{"commit":{"committer":{"date":"%s"}}}' % COMMITTER_DATE

def emit(data):
    if "--jq" in args:
        expr = args[args.index("--jq") + 1]
        p = subprocess.run(["jq", "-r", expr], input=data, capture_output=True, text=True)
        sys.stdout.write(p.stdout)
    else:
        sys.stdout.write(data)
    sys.exit(0)

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
    emit(ISSUE)
# The check-runs endpoint (repos/O/R/commits/SHA/check-runs?per_page=100) also
# contains "/commits/", so it MUST be matched before the generic committer-date
# branch. Match on substring (a ?query may follow the /check-runs path).
if "/check-runs" in endpoint:
    emit(CHECK_RUNS)
if "/commits/" in endpoint:
    emit(COMMIT)
emit(PULL)
'''


def run(args, *, inline="[]", reviews="[]", issue="[]", graphql=None,
        graphql_next=None, committer_date="2026-06-18T00:00:00Z", me="testuser",
        check_runs=None):
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
    check("itemized: review-body carries the NOTE about no inline thread / re-review",
          "NOTE:" in out and "re-review" in out)
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

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
