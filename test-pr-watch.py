#!/usr/bin/env python3
"""Proof harness for pr-watch.sh CR-skip + Codoki-settlement wiring (#34, #110, #173).

Three behaviors are exercised here, all host-independent (gh + the Codoki oracle
are stubbed; never a real PR):

  #34  A `norabbit`-labeled or CR-"Review skipped" PR must SETTLE without waiting
       for a CodeRabbit review that will never land.

  #173 CR-waiting is OPT-IN. With org-wide auto-review OFF, an untriggered PR posts
       NO CR check at all, so the default must be SATISFIED. The script waits for
       `cr-review` ONLY on positive evidence CR will review: an existing review
       (incl. DISMISSED), CR in requested_reviewers, or a `@coderabbitai review` /
       `@coderabbitai full review` trigger comment. A bare/`resolve`/`summary`
       mention does NOT count (guardrail). The idle-no-trigger PR settles.

  #110 Codoki posts its verdict as a `Codoki PR Review` entry in statusCheckRollup,
       invisible to the reviews API. pr-watch defers Codoki settlement to the
       oracle `ship-gate-preflight.sh --codoki-only`: exit 0 -> settled, exit 2 ->
       stays pending (`codoki-check`). The oracle is stubbed via $HOME so this test
       isolates pr-watch's wiring from the oracle's own (separately tested) logic.

`gh` is a temp Python stub first on PATH; it serves canned JSON per endpoint and
applies any `--jq` filter via the real jq. PR_WATCH_POLL_INTERVAL=0 drives the
loop without the 30s production cadence. Settle cases assert exit 0 + the
`settled` line; not-settled cases use a short timeout and assert exit 1 with the
expected token in the `pending=` list.

Run: python3 test-pr-watch.py
"""
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "pr-watch.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


HEAD_SHA = "a" * 40
COMMITTER_DATE = "2026-06-18T00:00:00Z"

# Two DISTINCT roles for pr-watch.sh's timeout arg, kept separate to avoid a flake:
#   SETTLE_TIMEOUT  -- a settle/review-blocked case reaches its terminal in ~2 poll
#     iterations and EXITS immediately, so this value is only a safety ceiling and is
#     never actually waited out; it must be generous. Each iteration forks ~11 `gh`
#     stubs (each a Python cold-start) + jq, so ~1.3s locally but multiples of that on
#     a loaded CI runner. A tight ceiling (the old shared `timeout_secs=2`) let the
#     settle work race its own deadline and time out to exit 1 -- the ubuntu-only flake.
#   PENDING_TIMEOUT -- a timeout-EXPECTED case (asserts exit 1 + a `pending=` token)
#     spins until the deadline, so here the timeout IS the wall-clock duration and must
#     stay short. (PR_WATCH_POLL_INTERVAL=0 keeps the loop fast in both roles.)
SETTLE_TIMEOUT = 15
PENDING_TIMEOUT = 2

GH_STUB = r'''#!/usr/bin/env python3
import os, sys, subprocess
args = sys.argv[1:]
HEAD_SHA = os.environ["HEAD_SHA"]
MERGEABLE = os.environ.get("MERGEABLE", "clean")
COMMITTER_DATE = os.environ.get("COMMITTER_DATE", "")
REVIEWS = os.environ.get("REVIEWS_JSON", "[]")
CHECKS = os.environ.get("CHECKS_JSON", "[]")
LABELS = os.environ.get("LABELS_JSON", "[]")
COMMENTS = os.environ.get("COMMENTS_JSON", "[]")
ISSUE = os.environ.get("ISSUE_JSON", "[]")
REQUESTED = os.environ.get("REQUESTED_REVIEWERS_JSON", '{"users":[]}')
PR_STATE = os.environ.get("PR_STATE", "open")
MERGED = os.environ.get("MERGED", "false")
THREADS = os.environ.get("THREADS_JSON", '{"data":{"repository":{"pullRequest":{"reviewThreads":{"totalCount":0,"nodes":[]}}}}}')
THREADS_RC = int(os.environ.get("THREADS_RC", "0"))
STATE_DIR = os.environ.get("STUB_STATE_DIR", "")

def bump(name):
    # Per-endpoint call counter (file-backed; each gh call is a fresh process).
    path = os.path.join(STATE_DIR, name)
    n = int(open(path).read()) if os.path.exists(path) else 0
    open(path, "w").write(str(n + 1))
    return n

# MERGEABLE_SEQ ("blocked,blocked,clean") steps mergeable_state per pulls/<n> read,
# holding the last value, so a test can drive a pending-set TRANSITION (#399).
seq = os.environ.get("MERGEABLE_SEQ", "")
if seq and args[:1] == ["api"] and any(a.endswith("/pulls/123") for a in args):
    vals = seq.split(","); MERGEABLE = vals[min(bump("pull"), len(vals) - 1)]
PULL = ('{"head":{"sha":"%s"},"mergeable_state":"%s","state":"%s","merged":%s}'
        % (HEAD_SHA, MERGEABLE, PR_STATE, MERGED))
COMMIT = '{"commit":{"committer":{"date":"%s"}}}' % COMMITTER_DATE

def emit(data):
    if "--jq" in args:
        expr = args[args.index("--jq") + 1]
        p = subprocess.run(["jq", "-r", expr], input=data, capture_output=True, text=True)
        sys.stdout.write(p.stdout)
    else:
        sys.stdout.write(data)
    sys.exit(0)

if args[:2] == ["pr", "checks"]:
    emit(CHECKS)
if args[:2] == ["pr", "view"]:
    emit('{"labels":%s}' % LABELS)
# GraphQL reviewThreads read (#441). Record the query so a test can assert it is a
# `query`, never a mutation. THREADS_RC != 0 simulates a gh/GraphQL failure.
if args[:2] == ["api", "graphql"]:
    if STATE_DIR:
        open(os.path.join(STATE_DIR, "graphql-args"), "a").write(" ".join(args) + "\n")
    if THREADS_RC:
        sys.stderr.write("graphql error\n"); sys.exit(THREADS_RC)
    # Validate the query VARIABLES the way GitHub would: each must be present with
    # the right value AND the right type flag (-f = String, -F = typed/Int). A
    # missing or mistyped variable gets GitHub's error body, which pr-watch must
    # read as UNREADABLE (#441 round 1: dropping `-F number=` used to pass).
    pairs = set(zip(args, args[1:]))
    need = {("-f", "owner=owner"), ("-f", "name=repo"), ("-F", "number=123")}
    if not need <= pairs:
        emit('{"errors":[{"message":"missing variable"}]}')
    emit(THREADS)

# gh api ... : find the endpoint token (contains "repos/").
endpoint = ""
for a in args:
    if "repos/" in a:
        endpoint = a; break
if endpoint.endswith("/reviews"):
    emit(REVIEWS)
if endpoint.endswith("/comments") and "/pulls/" in endpoint:
    # GROW_INLINE=<login>: every read returns one MORE inline comment by <login>, so
    # the quiet-period bot count never stabilizes (#441 AC f deferral case).
    grow = os.environ.get("GROW_INLINE", "")
    if grow:
        n = bump("inline") + 1
        emit("[" + ",".join('{"user":{"login":"%s"}}' % grow for _ in range(n)) + "]")
    emit(COMMENTS)
if endpoint.endswith("/comments") and "/issues/" in endpoint:
    emit(ISSUE)
if endpoint.endswith("/requested_reviewers"):
    emit(REQUESTED)
if "/commits/" in endpoint:
    emit(COMMIT)
# bare pulls/<n>
emit(PULL)
'''


def run(*, labels="[]", checks="[]", reviews="[]", codoki_rc=0,
        requested_reviewers='{"users":[]}', comments="[]", issue_comments="[]",
        timeout_secs=SETTLE_TIMEOUT, blocking_reviewers=None, mergeable="clean",
        pr_state="open", merged="false", threads=None, threads_rc=0,
        mergeable_seq="", grow_inline="", want_graphql_args=False):
    with tempfile.TemporaryDirectory() as td:
        state_dir = os.path.join(td, "state"); os.makedirs(state_dir)
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        home = os.path.join(td, "home")
        oracle_dir = os.path.join(home, ".claude", "scripts"); os.makedirs(oracle_dir)

        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(GH_STUB)
        os.chmod(gh, 0o755)

        # Stub the Codoki oracle: exit with the configured rc.
        oracle = os.path.join(oracle_dir, "ship-gate-preflight.sh")
        with open(oracle, "w") as f:
            f.write("#!/usr/bin/env bash\nexit ${CODOKI_RC:-0}\n")
        os.chmod(oracle, 0o755)

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["HOME"] = home
        env["HEAD_SHA"] = HEAD_SHA
        env["MERGEABLE"] = mergeable
        env["PR_STATE"] = pr_state
        env["MERGED"] = merged
        if threads is not None:
            env["THREADS_JSON"] = threads
        env["THREADS_RC"] = str(threads_rc)
        env["MERGEABLE_SEQ"] = mergeable_seq
        env["GROW_INLINE"] = grow_inline
        env["STUB_STATE_DIR"] = state_dir
        env["COMMITTER_DATE"] = COMMITTER_DATE
        env["LABELS_JSON"] = labels
        env["CHECKS_JSON"] = checks
        env["REVIEWS_JSON"] = reviews
        env["COMMENTS_JSON"] = comments
        env["ISSUE_JSON"] = issue_comments
        env["REQUESTED_REVIEWERS_JSON"] = requested_reviewers
        env["CODOKI_RC"] = str(codoki_rc)
        env["PR_WATCH_POLL_INTERVAL"] = "0"
        if blocking_reviewers is not None:
            env["PR_WATCH_BLOCKING_REVIEWERS"] = blocking_reviewers

        p = subprocess.run(["bash", SCRIPT, "123", "owner/repo", str(timeout_secs)],
                           env=env, capture_output=True, text=True, timeout=30)
        if want_graphql_args:
            gpath = os.path.join(state_dir, "graphql-args")
            gargs = open(gpath).read() if os.path.exists(gpath) else ""
            return p.returncode, p.stdout, p.stderr, gargs
        return p.returncode, p.stdout, p.stderr


CR_APPROVED = '[{"user":{"login":"coderabbitai[bot]"},"state":"APPROVED","submitted_at":"2026-06-18T01:00:00Z"}]'
CR_DISMISSED = '[{"user":{"login":"coderabbitai[bot]"},"state":"DISMISSED","submitted_at":"2026-06-18T01:00:00Z"}]'
SKIP_CHECK = '[{"name":"CodeRabbit","state":"SUCCESS","description":"Review skipped"}]'
GREEN_CHECK = '[{"name":"ci","state":"SUCCESS","description":"Build passed"}]'

# review-blocked fixtures (#195): the terminal must fire on ANY reviewer's latest
# HEAD review being CHANGES_REQUESTED, not just CodeRabbit. submitted_at is >= the
# stub COMMITTER_DATE so the head-date filter keeps them.
CR_CHANGES = '[{"user":{"login":"coderabbitai[bot]"},"state":"CHANGES_REQUESTED","submitted_at":"2026-06-18T01:00:00Z"}]'
GREPTILE_CHANGES = '[{"user":{"login":"greptile-apps[bot]"},"state":"CHANGES_REQUESTED","submitted_at":"2026-06-18T01:00:00Z"}]'
HUMAN_CHANGES = '[{"user":{"login":"octocat"},"state":"CHANGES_REQUESTED","submitted_at":"2026-06-18T01:00:00Z"}]'
MULTI_CHANGES = ('[{"user":{"login":"coderabbitai[bot]"},"state":"CHANGES_REQUESTED","submitted_at":"2026-06-18T01:00:00Z"},'
                 '{"user":{"login":"octocat"},"state":"CHANGES_REQUESTED","submitted_at":"2026-06-18T01:30:00Z"}]')
# A reviewer's earlier CHANGES_REQUESTED superseded by a later APPROVED on HEAD must
# NOT block (latest-per-reviewer wins).
SUPERSEDED_CHANGES = ('[{"user":{"login":"octocat"},"state":"CHANGES_REQUESTED","submitted_at":"2026-06-18T01:00:00Z"},'
                      '{"user":{"login":"octocat"},"state":"APPROVED","submitted_at":"2026-06-18T02:00:00Z"}]')
# Supersession must survive INTERLEAVING: octocat CHANGES_REQUESTED, then ANOTHER
# reviewer's event, then octocat APPROVED -- input NOT sorted by login. jq's group_by
# collates all rows of a login into one group regardless of input order (verified:
# group_by(.user.login) on this yields [[octocat,octocat],[coderabbitai]]), so
# map(sort_by(.submitted_at)|last) still picks octocat's APPROVED -> no block. This is
# the exact scenario a review flagged as a false "group_by needs pre-sort" bug (#205).
INTERLEAVED_SUPERSEDED = ('[{"user":{"login":"octocat"},"state":"CHANGES_REQUESTED","submitted_at":"2026-06-18T01:00:00Z"},'
                          '{"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED","submitted_at":"2026-06-18T01:15:00Z"},'
                          '{"user":{"login":"octocat"},"state":"APPROVED","submitted_at":"2026-06-18T02:00:00Z"}]')
# Same interleaving but octocat's LATEST is CHANGES_REQUESTED -> still blocks, and the
# collated group means by= lists octocat exactly ONCE (no duplicate login).
INTERLEAVED_STILL_BLOCKED = ('[{"user":{"login":"octocat"},"state":"CHANGES_REQUESTED","submitted_at":"2026-06-18T01:00:00Z"},'
                             '{"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED","submitted_at":"2026-06-18T01:15:00Z"},'
                             '{"user":{"login":"octocat"},"state":"CHANGES_REQUESTED","submitted_at":"2026-06-18T02:00:00Z"}]')

PENDING_CHECK = '[{"name":"ci","state":"IN_PROGRESS","description":""}]'

# #441 reviewThreads fixtures (GraphQL shape pr-watch.sh queries).
COPILOT = "copilot-pull-request-reviewer[bot]"
CR_APPROVED_COPILOT_COMMENTED = (
    '[{"user":{"login":"coderabbitai[bot]"},"state":"APPROVED","submitted_at":"2026-06-18T01:00:00Z"},'
    '{"user":{"login":"%s"},"state":"COMMENTED","submitted_at":"2026-06-18T01:10:00Z"}]' % COPILOT)


def _thread(resolved, login):
    # Live GraphQL shape (measured on a real PR): a Bot actor's login carries NO
    # `[bot]` suffix and __typename is "Bot"; pr-watch.sh re-appends the suffix.
    if login.endswith("[bot]"):
        author = {"__typename": "Bot", "login": login[:-len("[bot]")]}
    else:
        author = {"__typename": "User", "login": login}
    return {"isResolved": resolved, "comments": {"nodes": [{"author": author}]}}


def _threads(nodes, total=None):
    import json
    return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
        "totalCount": len(nodes) if total is None else total, "nodes": nodes}}}}})


THREADS_ONE_OPEN = _threads([_thread(True, "coderabbitai[bot]"), _thread(False, COPILOT)])
THREADS_DUP_AUTHORS = _threads([_thread(False, COPILOT), _thread(False, "octocat"),
                                _thread(False, COPILOT), _thread(True, "octocat")])
THREADS_ALL_RESOLVED = _threads([_thread(True, COPILOT), _thread(True, "octocat")])
THREADS_ERRORS = ('{"errors":[{"message":"Something went wrong"}],'
                  '"data":{"repository":{"pullRequest":{"reviewThreads":{"totalCount":1,"nodes":[]}}}}}')
THREADS_NULL_NODE = _threads([None, _thread(True, COPILOT)])
THREADS_NULL_RESOLVED = _threads([{"isResolved": None, "comments": {"nodes": []}}])
THREADS_TRUNC_OPEN = _threads([_thread(False, COPILOT)] + [_thread(True, COPILOT)] * 99, total=150)
THREADS_TRUNC_RESOLVED = _threads([_thread(True, COPILOT)] * 100, total=150)
# totalCount BELOW the node count is self-contradictory -> UNREADABLE, never a count.
THREADS_TOTAL_BELOW_NODES = _threads([_thread(False, COPILOT)], total=0)
# CR reviewed an OLDER head (submitted before the stub COMMITTER_DATE).
CR_APPROVED_OLD_HEAD = '[{"user":{"login":"coderabbitai[bot]"},"state":"APPROVED","submitted_at":"2026-06-17T01:00:00Z"}]'
RED_AND_GREEN_CHECKS = ('[{"name":"ci","state":"SUCCESS","description":"ok"},'
                        '{"name":"lint","state":"FAILURE","description":"Lint failed"}]')
# A checks body whose entries carry no `state`: pending AND failing are unreadable.
CHECKS_NO_STATE = '[{"name":"ci","description":"?"}]'

CR_REQUESTED = '{"users":[{"login":"coderabbitai[bot]"}]}'
TRIGGER_COMMENT = '[{"body":"please @coderabbitai review this PR"}]'
TRIGGER_FULL_COMMENT = '[{"body":"@coderabbitai full review"}]'
RESOLVE_COMMENT = '[{"body":"@coderabbitai resolve"}]'
# CR's OWN auto-generated summary/walkthrough boilerplate quotes "@coderabbitai review"
# as user instructions. It must NOT count as a trigger (the #173 live-UAT-caught bug:
# every CR-touched PR would otherwise false-positive back into a cr-review hang).
CR_BOILERPLATE_COMMENT = ('[{"user":{"login":"coderabbitai[bot]"},'
                          '"body":"<!-- summarize -->\\nTip: tag @coderabbitai review to re-run."}]')


def main():
    print("== #34: norabbit label -> CR satisfied, settles (no CR-review wait) ==")
    rc, out, err = run(labels='[{"name":"norabbit"}]', checks=GREEN_CHECK, reviews="[]")
    check("norabbit + green CI + Codoki settled -> exit 0", rc == 0)
    check("emits 'settled' line", "settled head=" in out)

    print("== #34: CR 'Review skipped' check -> CR satisfied, settles ==")
    rc, out, err = run(labels="[]", checks=SKIP_CHECK, reviews="[]")
    check("Review-skipped check + Codoki settled -> exit 0", rc == 0)
    check("emits 'settled' line", "settled head=" in out)

    print("== #173: idle CR -- no review, no norabbit, no trigger, not requested -> settles ==")
    # The exact bug: auto-review OFF means CR posts NO check at all, so the old
    # opt-out logic waited the full timeout. Opt-in: no positive evidence -> satisfied.
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews="[]", timeout_secs=SETTLE_TIMEOUT)
    check("idle CR + green CI + Codoki settled -> exit 0 (not a timeout)", rc == 0)
    check("emits 'settled' line", "settled head=" in out)
    check("pending list never names cr-review", "cr-review" not in err)

    print("== #173: @coderabbitai review trigger comment present -> waits for CR ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews="[]",
                       issue_comments=TRIGGER_COMMENT, timeout_secs=PENDING_TIMEOUT)
    check("triggered, no review yet -> exit 1 (timeout, not settled)", rc == 1)
    check("pending names cr-review", "cr-review" in err)

    print("== #173: @coderabbitai full review trigger comment present -> waits for CR ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews="[]",
                       issue_comments=TRIGGER_FULL_COMMENT, timeout_secs=PENDING_TIMEOUT)
    check("full-review triggered, no review yet -> exit 1 (timeout)", rc == 1)
    check("pending names cr-review", "cr-review" in err)

    print("== #173: CR in requested_reviewers -> waits for CR ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews="[]",
                       requested_reviewers=CR_REQUESTED, timeout_secs=PENDING_TIMEOUT)
    check("CR requested, no review yet -> exit 1 (timeout)", rc == 1)
    check("pending names cr-review", "cr-review" in err)

    print("== #173: existing DISMISSED CR review -> still expected, waits (no regression) ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=CR_DISMISSED, timeout_secs=PENDING_TIMEOUT)
    check("DISMISSED review present -> exit 1 (timeout, stays pending)", rc == 1)
    check("pending names cr-review", "cr-review" in err)

    print("== #173 (guardrail 1): @coderabbitai resolve alone does NOT count as triggered -> settles ==")
    # `@coderabbitai resolve`/`summary` engage CR WITHOUT requesting a review, so
    # they must not re-introduce the false-wait. Only review-triggering forms count.
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews="[]",
                       issue_comments=RESOLVE_COMMENT, timeout_secs=SETTLE_TIMEOUT)
    check("resolve-only comment -> exit 0 (settles, not triggered)", rc == 0)
    check("emits 'settled' line", "settled head=" in out)
    check("pending list never names cr-review", "cr-review" not in err)

    print("== #173 (live-UAT bug): CR's OWN comment quoting @coderabbitai review does NOT trigger -> settles ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews="[]",
                       issue_comments=CR_BOILERPLATE_COMMENT, timeout_secs=SETTLE_TIMEOUT)
    check("CR-authored boilerplate -> exit 0 (settles, not a trigger)", rc == 0)
    check("pending list never names cr-review", "cr-review" not in err)

    print("== #110: Codoki not settled (oracle exit 2) -> stays pending ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=CR_APPROVED,
                       codoki_rc=2, timeout_secs=PENDING_TIMEOUT)
    check("Codoki oracle exit 2 -> exit 1 (timeout, not settled)", rc == 1)
    check("pending names codoki-check", "codoki-check" in err)

    print("== happy path: CR APPROVED + Codoki settled + green CI -> settled ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=CR_APPROVED, codoki_rc=0)
    check("CR APPROVED + Codoki settled -> exit 0", rc == 0)
    check("emits 'settled' line", "settled head=" in out)

    print("== #195: CR CHANGES_REQUESTED -> review-blocked (back-compat) ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=CR_CHANGES)
    check("CR CHANGES_REQUESTED -> exit 0", rc == 0)
    check("emits 'review-blocked' line", "review-blocked head=" in out)
    check("names CR in by=", "coderabbitai[bot]" in out)

    print("== #195: non-CR bot (greptile) CHANGES_REQUESTED -> review-blocked ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=GREPTILE_CHANGES)
    check("greptile CHANGES_REQUESTED -> exit 0", rc == 0)
    check("emits 'review-blocked' line", "review-blocked head=" in out)
    check("names greptile in by=", "greptile-apps[bot]" in out)

    print("== #195: HUMAN reviewer CHANGES_REQUESTED -> review-blocked (reviewer-agnostic) ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=HUMAN_CHANGES)
    check("human CHANGES_REQUESTED -> exit 0", rc == 0)
    check("emits 'review-blocked' line", "review-blocked head=" in out)
    check("names the human reviewer in by=", "octocat" in out)

    print("== #195: MULTIPLE reviewers CHANGES_REQUESTED -> review-blocked names all ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=MULTI_CHANGES)
    check("multi CHANGES_REQUESTED -> exit 0", rc == 0)
    check("names both reviewers in by=", "coderabbitai[bot]" in out and "octocat" in out)

    print("== #195: superseded CHANGES_REQUESTED (later APPROVED on HEAD) -> settles, not blocked ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=SUPERSEDED_CHANGES)
    check("latest-per-reviewer APPROVED -> exit 0", rc == 0)
    check("emits 'settled' (not review-blocked)", "settled head=" in out and "review-blocked" not in out)

    print("== #195: PR_WATCH_BLOCKING_REVIEWERS restricts the set (excluded reviewer does NOT block) ==")
    # Restrict blocking to CR only; a human CHANGES_REQUESTED is then NOT a blocker,
    # so with green CI + clean mergeable the PR settles instead of routing to handle-review.
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=HUMAN_CHANGES,
                       blocking_reviewers="coderabbitai[bot]")
    check("restricted set excludes human -> exit 0", rc == 0)
    check("emits 'settled' (human not in blocking set)", "settled head=" in out and "review-blocked" not in out)

    print("== #195: PR_WATCH_BLOCKING_REVIEWERS restriction still fires for an IN-set reviewer ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=CR_CHANGES,
                       blocking_reviewers="coderabbitai[bot], greptile-apps[bot]")
    check("in-set CR CHANGES_REQUESTED -> exit 0", rc == 0)
    check("emits 'review-blocked' line", "review-blocked head=" in out)

    print("== #205 (Codoki rebuttal): interleaved supersession clears the block (group_by collates) ==")
    # octocat CR -> other reviewer -> octocat APPROVED, unsorted by login. group_by
    # collates octocat's rows into one group so the later APPROVED wins -> settles.
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=INTERLEAVED_SUPERSEDED)
    check("interleaved latest APPROVED -> exit 0", rc == 0)
    check("emits 'settled' (not review-blocked)", "settled head=" in out and "review-blocked" not in out)

    print("== #205 (Codoki rebuttal): interleaved still-blocked -> by= lists the login ONCE ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=INTERLEAVED_STILL_BLOCKED)
    check("interleaved latest CHANGES_REQUESTED -> exit 0", rc == 0)
    check("emits 'review-blocked'", "review-blocked head=" in out)
    check("by= lists octocat exactly once (no dup from split groups)", out.count("octocat") == 1)

    print("== #195 (hostile-review regression): separator-only env must NOT hang -> match-any fallback ==")
    # A separator-only value (",," / spaces) reduces to zero tokens. It must parse to
    # the match-any sentinel [] (a single valid JSON array), NOT double-emit "[]\n[]"
    # which broke --argjson every poll and hung the watch to timeout. With CR
    # CHANGES_REQUESTED present, the fallback means review-blocked STILL fires.
    for junk in (",,", "   ", " , , "):
        rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=CR_CHANGES,
                           blocking_reviewers=junk, timeout_secs=SETTLE_TIMEOUT)
        check(f"separator-only {junk!r} -> exit 0 (no hang)", rc == 0)
        check(f"separator-only {junk!r} -> emits 'review-blocked'", "review-blocked head=" in out)
        check(f"separator-only {junk!r} -> not a timeout", "timeout" not in err)

    # ---------------------------------------------------------------------
    # #435: a MERGED / CLOSED PR is terminal immediately.
    # ---------------------------------------------------------------------
    print("== #435: MERGED PR (mergeable_state unknown) -> 'merged' terminal, exit 0 ==")
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_APPROVED, mergeable="unknown",
                       pr_state="closed", merged="true")
    check("merged -> exit 0 (not a timeout)", rc == 0)
    check("emits 'merged head=<sha8>'", out.strip() == "merged head=" + HEAD_SHA[:8])

    print("== #435: merged beats review-blocked (checked before the rest of the loop) ==")
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_CHANGES, mergeable="unknown",
                       pr_state="closed", merged="true")
    check("merged + stale CHANGES_REQUESTED -> 'merged', not review-blocked",
          rc == 0 and out.strip() == "merged head=" + HEAD_SHA[:8])

    print("== #435: CLOSED-unmerged PR -> 'closed' terminal, exit 0 ==")
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_APPROVED, mergeable="unknown",
                       pr_state="closed", merged="false")
    check("closed -> exit 0", rc == 0)
    check("emits 'closed head=<sha8>'", out.strip() == "closed head=" + HEAD_SHA[:8])

    # ---------------------------------------------------------------------
    # #441: thread-blocked. CI green, CR APPROVED, Copilot COMMENTED, blocked.
    # ---------------------------------------------------------------------
    print("== #441 (a): blocked + 1 unresolved thread + CI green -> thread-blocked ==")
    rc, out, err, gargs = run(checks=GREEN_CHECK, reviews=CR_APPROVED_COPILOT_COMMENTED,
                              mergeable="blocked", threads=THREADS_ONE_OPEN,
                              want_graphql_args=True)
    check("(a) exit 0", rc == 0)
    check("(a) emits 'thread-blocked ... unresolved=1 failing=0 by=<login>'",
          out.strip() == "thread-blocked head=%s unresolved=1 failing=0 by=%s" % (HEAD_SHA[:8], COPILOT))
    check("(a) the GraphQL call is a `query`, never a mutation",
          "query=query(" in gargs and "mutation" not in gargs)

    print("== #441 (a'): by= de-duplicates the first-comment authors ==")
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_APPROVED, mergeable="blocked",
                       threads=THREADS_DUP_AUTHORS)
    check("(a') unresolved=3 by= lists each login once",
          out.strip() == "thread-blocked head=%s unresolved=3 failing=0 by=%s,octocat" % (HEAD_SHA[:8], COPILOT))

    print("== #441 (b): all threads resolved -> no thread-blocked (stays pending) ==")
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_APPROVED, mergeable="blocked",
                       threads=THREADS_ALL_RESOLVED, timeout_secs=PENDING_TIMEOUT)
    check("(b) exit 1 (timeout)", rc == 1)
    check("(b) no thread-blocked / settled on stdout", "thread-blocked" not in out and "settled" not in out)
    check("(b) pending is merge(blocked) only", "pending=merge(blocked)\n" in err)

    print("== #441 (c): GraphQL failure / malformed body -> neither thread-blocked nor settled ==")
    for label, kw in (("gh failure", {"threads_rc": 1}),
                      ("top-level errors", {"threads": THREADS_ERRORS}),
                      ("null data", {"threads": '{"data":null}'}),
                      ("non-JSON body", {"threads": "not json"}),
                      ("null node", {"threads": THREADS_NULL_NODE}),
                      ("non-boolean isResolved", {"threads": THREADS_NULL_RESOLVED})):
        rc, out, err = run(checks=GREEN_CHECK, reviews=CR_APPROVED, mergeable="blocked",
                           timeout_secs=PENDING_TIMEOUT, **kw)
        check(f"(c) {label}: exit 1, no terminal on stdout",
              rc == 1 and "thread-blocked" not in out and "settled" not in out)
        check(f"(c) {label}: pending names threads(unreadable)", "threads(unreadable)" in err)

    print("== #441 (d): CHANGES_REQUESTED + an open thread -> review-blocked wins ==")
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_CHANGES, mergeable="blocked",
                       threads=THREADS_ONE_OPEN)
    check("(d) exit 0 + review-blocked", rc == 0 and "review-blocked head=" in out)
    check("(d) no thread-blocked", "thread-blocked" not in out)

    print("== #441 (e): CI pending -> no terminal ==")
    rc, out, err = run(checks=PENDING_CHECK, reviews=CR_APPROVED, mergeable="blocked",
                       threads=THREADS_ONE_OPEN, timeout_secs=PENDING_TIMEOUT)
    check("(e) exit 1 (timeout)", rc == 1)
    check("(e) no thread-blocked", "thread-blocked" not in out)
    check("(e) pending names ci(1)", "ci(1)" in err)

    print("== #441 (f): quiet-gate deferral -- Copilot still posting -> thread-blocked withheld ==")
    # Copilot inline comments keep arriving, so the bot count never stabilizes. This
    # also proves copilot-pull-request-reviewer[bot] is in QUIET_AUTHORS_JQ: without
    # it the growing count is invisible and thread-blocked fires prematurely.
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_APPROVED, mergeable="blocked",
                       threads=THREADS_ONE_OPEN, grow_inline=COPILOT,
                       timeout_secs=PENDING_TIMEOUT)
    check("(f) exit 1 (timeout, deferred)", rc == 1)
    check("(f) no thread-blocked while Copilot is still posting", "thread-blocked" not in out)
    check("(f) pending names threads(quiet-confirm)", "threads(quiet-confirm)" in err)

    print("== #441 (g): totalCount > nodes is REPORTED, not silently truncated ==")
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_APPROVED, mergeable="blocked",
                       threads=THREADS_TRUNC_OPEN)
    check("(g) thread-blocked still fires on the fetched unresolved thread",
          rc == 0 and "thread-blocked head=" in out and "unresolved=1" in out)
    check("(g) stderr reports 100 of 150 fetched, a lower bound",
          "100/150" in err and "lower bound" in err)
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_APPROVED, mergeable="blocked",
                       threads=THREADS_TRUNC_RESOLVED, timeout_secs=PENDING_TIMEOUT)
    check("(g) truncated + every fetched thread resolved -> no terminal, pends threads(truncated)",
          rc == 1 and "thread-blocked" not in out and "threads(truncated)" in err)

    print("== #441 (h): a settle-path baseline does NOT confirm thread-blocked ==")
    # Poll 1 reads `clean` (settle path sets the quiet baseline and continues without
    # resetting it); poll 2 reads `blocked` with an open thread. The `thread:` tag on
    # the baseline forces a SECOND poll on which thread-blocked's own predicate holds,
    # so the terminal never fires on a single thread read. Without the tag, poll 2
    # would emit at once (never announcing threads(quiet-confirm), one graphql call).
    rc, out, err, gargs = run(checks=GREEN_CHECK, reviews=CR_APPROVED, threads=THREADS_ONE_OPEN,
                              mergeable_seq="clean,blocked", want_graphql_args=True)
    check("(h) still reaches thread-blocked", rc == 0 and "thread-blocked head=" in out)
    check("(h) passed through threads(quiet-confirm) first", "threads(quiet-confirm)" in err)
    check("(h) the thread predicate was read on >= 2 polls", gargs.count("query=query(") >= 2)

    print("== #441 (i): CR triggered, CR review older than head, blocked, open thread -> pends ==")
    # thread-blocked needs merge(blocked) to be the SOLE pending item; a CR review still
    # owed on the new head keeps cr-review pending, so the watch times out instead.
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_APPROVED_OLD_HEAD, mergeable="blocked",
                       threads=THREADS_ONE_OPEN, issue_comments=TRIGGER_COMMENT,
                       timeout_secs=PENDING_TIMEOUT)
    check("(i) exit 1 (timeout), no thread-blocked", rc == 1 and "thread-blocked" not in out)
    check("(i) pending is cr-review,merge(blocked)", "pr-watch: pending=cr-review,merge(blocked)\n" in err)

    print("== #441 (j): totalCount below the node count -> threads(unreadable) ==")
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_APPROVED, mergeable="blocked",
                       threads=THREADS_TOTAL_BELOW_NODES, timeout_secs=PENDING_TIMEOUT)
    check("(j) exit 1, no terminal", rc == 1 and "thread-blocked" not in out and "settled" not in out)
    check("(j) pending names threads(unreadable)", "threads(unreadable)" in err)

    print("== #441 (k): GraphQL variables are sent with the right types ==")
    # The stub answers a missing/mistyped variable with GitHub's error body, so an open
    # thread still reaching thread-blocked (case a) proves all three were sent. Here the
    # argument shape is asserted directly as well.
    rc, out, err, gargs = run(checks=GREEN_CHECK, reviews=CR_APPROVED, mergeable="blocked",
                              threads=THREADS_ONE_OPEN, want_graphql_args=True)
    check("(k) -f owner / -f name (String), -F number (Int)",
          "-f owner=owner" in gargs and "-f name=repo" in gargs and "-F number=123" in gargs)

    print("== #441 (l): red CI + open thread -> thread-blocked names failing=1 ==")
    rc, out, err = run(checks=RED_AND_GREEN_CHECKS, reviews=CR_APPROVED, mergeable="blocked",
                       threads=THREADS_ONE_OPEN)
    check("(l) exit 0 + failing=1 on the line",
          rc == 0 and out.strip() == "thread-blocked head=%s unresolved=1 failing=1 by=%s" % (HEAD_SHA[:8], COPILOT))

    print("== #441 (m): unreadable checks read -> no terminal (failing never a guessed zero) ==")
    for label, ck in (("entries without state", CHECKS_NO_STATE), ("non-JSON body", "not json"),
                      ("object, not array", '{"state":"SUCCESS"}')):
        rc, out, err = run(checks=ck, reviews=CR_APPROVED, mergeable="blocked",
                           threads=THREADS_ONE_OPEN, timeout_secs=PENDING_TIMEOUT)
        check(f"(m) {label}: exit 1, no thread-blocked", rc == 1 and "thread-blocked" not in out)
        check(f"(m) {label}: pending names ci(unknown)", "ci(unknown)" in err)

    print("== #399: review-blocked quiet deferral is announced on stderr ==")
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_CHANGES, grow_inline="coderabbitai[bot]",
                       timeout_secs=PENDING_TIMEOUT)
    check("#399 review-blocked deferral -> exit 1, no terminal", rc == 1 and out.strip() == "")
    check("#399 names review-blocked(quiet-confirm)",
          err.count("pr-watch: pending=review-blocked(quiet-confirm)\n") == 1)

    # ---------------------------------------------------------------------
    # #399: one stderr line per pending-set CHANGE, silence when unchanged.
    # ---------------------------------------------------------------------
    print("== #399: pending-set transition emits one stderr line per change ==")
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_APPROVED,
                       mergeable_seq="behind,behind,behind,clean")
    check("#399 settles after the transition", rc == 0 and "settled head=" in out)
    check("#399 merge(behind) announced exactly once across 3 identical polls",
          err.count("pr-watch: pending=merge(behind)\n") == 1)
    check("#399 the change to quiet-confirm is announced",
          "pr-watch: pending=quiet-confirm\n" in err)
    check("#399 stdout carries only the terminal line", out.strip().count("\n") == 0)

    print("== #399: unchanged pending set across many polls -> exactly one stderr line ==")
    rc, out, err = run(checks=GREEN_CHECK, reviews=CR_APPROVED, mergeable="dirty",
                       timeout_secs=PENDING_TIMEOUT)
    check("#399 unchanged merge(dirty) -> one transition line only",
          err.count("pr-watch: pending=") == 1 and "pr-watch: pending=merge(dirty)\n" in err)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
