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
if "/commits/" in endpoint:
    emit(COMMIT)
emit(PULL)
'''


def run(args, *, inline="[]", reviews="[]", issue="[]", graphql=None,
        graphql_next=None, committer_date="2026-06-18T00:00:00Z", me="testuser"):
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
        p = subprocess.run(["bash", SCRIPT] + args + ["123", "owner/repo"],
                           env=env, capture_output=True, text=True, timeout=20)
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

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
