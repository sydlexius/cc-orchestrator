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
PULL = '{"head":{"sha":"%s"},"mergeable_state":"%s"}' % (HEAD_SHA, MERGEABLE)
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

# gh api ... : find the endpoint token (contains "repos/").
endpoint = ""
for a in args:
    if "repos/" in a:
        endpoint = a; break
if endpoint.endswith("/reviews"):
    emit(REVIEWS)
if endpoint.endswith("/comments") and "/pulls/" in endpoint:
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
        timeout_secs=2, expect_fast=True, blocking_reviewers=None):
    with tempfile.TemporaryDirectory() as td:
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
        env["MERGEABLE"] = "clean"
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
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews="[]", timeout_secs=2)
    check("idle CR + green CI + Codoki settled -> exit 0 (not a timeout)", rc == 0)
    check("emits 'settled' line", "settled head=" in out)
    check("pending list never names cr-review", "cr-review" not in err)

    print("== #173: @coderabbitai review trigger comment present -> waits for CR ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews="[]",
                       issue_comments=TRIGGER_COMMENT, timeout_secs=2)
    check("triggered, no review yet -> exit 1 (timeout, not settled)", rc == 1)
    check("pending names cr-review", "cr-review" in err)

    print("== #173: @coderabbitai full review trigger comment present -> waits for CR ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews="[]",
                       issue_comments=TRIGGER_FULL_COMMENT, timeout_secs=2)
    check("full-review triggered, no review yet -> exit 1 (timeout)", rc == 1)
    check("pending names cr-review", "cr-review" in err)

    print("== #173: CR in requested_reviewers -> waits for CR ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews="[]",
                       requested_reviewers=CR_REQUESTED, timeout_secs=2)
    check("CR requested, no review yet -> exit 1 (timeout)", rc == 1)
    check("pending names cr-review", "cr-review" in err)

    print("== #173: existing DISMISSED CR review -> still expected, waits (no regression) ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=CR_DISMISSED, timeout_secs=2)
    check("DISMISSED review present -> exit 1 (timeout, stays pending)", rc == 1)
    check("pending names cr-review", "cr-review" in err)

    print("== #173 (guardrail 1): @coderabbitai resolve alone does NOT count as triggered -> settles ==")
    # `@coderabbitai resolve`/`summary` engage CR WITHOUT requesting a review, so
    # they must not re-introduce the false-wait. Only review-triggering forms count.
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews="[]",
                       issue_comments=RESOLVE_COMMENT, timeout_secs=2)
    check("resolve-only comment -> exit 0 (settles, not triggered)", rc == 0)
    check("emits 'settled' line", "settled head=" in out)
    check("pending list never names cr-review", "cr-review" not in err)

    print("== #173 (live-UAT bug): CR's OWN comment quoting @coderabbitai review does NOT trigger -> settles ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews="[]",
                       issue_comments=CR_BOILERPLATE_COMMENT, timeout_secs=2)
    check("CR-authored boilerplate -> exit 0 (settles, not a trigger)", rc == 0)
    check("pending list never names cr-review", "cr-review" not in err)

    print("== #110: Codoki not settled (oracle exit 2) -> stays pending ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=CR_APPROVED,
                       codoki_rc=2, timeout_secs=2)
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

    print("== #195 (hostile-review regression): separator-only env must NOT hang -> match-any fallback ==")
    # A separator-only value (",," / spaces) reduces to zero tokens. It must parse to
    # the match-any sentinel [] (a single valid JSON array), NOT double-emit "[]\n[]"
    # which broke --argjson every poll and hung the watch to timeout. With CR
    # CHANGES_REQUESTED present, the fallback means review-blocked STILL fires.
    for junk in (",,", "   ", " , , "):
        rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=CR_CHANGES,
                           blocking_reviewers=junk, timeout_secs=2)
        check(f"separator-only {junk!r} -> exit 0 (no hang)", rc == 0)
        check(f"separator-only {junk!r} -> emits 'review-blocked'", "review-blocked head=" in out)
        check(f"separator-only {junk!r} -> not a timeout", "timeout" not in err)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
