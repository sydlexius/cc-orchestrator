#!/usr/bin/env python3
"""Proof harness for pr-watch.sh CR-skip + Codoki-settlement wiring (#34, #110).

Two behaviors are exercised here, both host-independent (gh + the Codoki oracle
are stubbed; never a real PR):

  #34  A `norabbit`-labeled or CR-"Review skipped" PR must SETTLE without waiting
       for a CodeRabbit review that will never land. A CR-ENABLED PR (no label,
       no skip check) must still WAIT for the real review (preserved behavior).

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
if "/commits/" in endpoint:
    emit(COMMIT)
# bare pulls/<n>
emit(PULL)
'''


def run(*, labels="[]", checks="[]", reviews="[]", codoki_rc=0,
        timeout_secs=2, expect_fast=True):
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
        env["COMMENTS_JSON"] = "[]"
        env["ISSUE_JSON"] = "[]"
        env["CODOKI_RC"] = str(codoki_rc)
        env["PR_WATCH_POLL_INTERVAL"] = "0"

        p = subprocess.run(["bash", SCRIPT, "123", "owner/repo", str(timeout_secs)],
                           env=env, capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout, p.stderr


CR_APPROVED = '[{"user":{"login":"coderabbitai[bot]"},"state":"APPROVED","submitted_at":"2026-06-18T01:00:00Z"}]'
SKIP_CHECK = '[{"name":"CodeRabbit","state":"SUCCESS","description":"Review skipped"}]'
GREEN_CHECK = '[{"name":"ci","state":"SUCCESS","description":"Build passed"}]'


def main():
    print("== #34: norabbit label -> CR satisfied, settles (no CR-review wait) ==")
    rc, out, err = run(labels='[{"name":"norabbit"}]', checks=GREEN_CHECK, reviews="[]")
    check("norabbit + green CI + Codoki settled -> exit 0", rc == 0)
    check("emits 'settled' line", "settled head=" in out)

    print("== #34: CR 'Review skipped' check -> CR satisfied, settles ==")
    rc, out, err = run(labels="[]", checks=SKIP_CHECK, reviews="[]")
    check("Review-skipped check + Codoki settled -> exit 0", rc == 0)
    check("emits 'settled' line", "settled head=" in out)

    print("== #34: CR enabled (no label, no skip), no CR review -> stays pending ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews="[]", timeout_secs=2)
    check("no CR review + CR enabled -> exit 1 (timeout, not settled)", rc == 1)
    check("pending names cr-review", "cr-review" in err)

    print("== #110: Codoki not settled (oracle exit 2) -> stays pending ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=CR_APPROVED,
                       codoki_rc=2, timeout_secs=2)
    check("Codoki oracle exit 2 -> exit 1 (timeout, not settled)", rc == 1)
    check("pending names codoki-check", "codoki-check" in err)

    print("== happy path: CR APPROVED + Codoki settled + green CI -> settled ==")
    rc, out, err = run(labels="[]", checks=GREEN_CHECK, reviews=CR_APPROVED, codoki_rc=0)
    check("CR APPROVED + Codoki settled -> exit 0", rc == 0)
    check("emits 'settled' line", "settled head=" in out)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
