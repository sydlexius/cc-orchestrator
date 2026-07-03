#!/usr/bin/env python3
"""Proof harness for issue-watch.sh (#217).

issue-watch.sh polls a lightweight snapshot {comment_count, state, labels,
assignees} of a GitHub issue and fires on the FIRST change after watch-start.
This harness is host-independent: `gh` is a temp Python stub first on PATH that
serves a SCRIPTED SEQUENCE of snapshots across polls, so no real issue is touched.

The stub serves parallel sequences STATES[] and COMMENTS[] indexed by poll. Within
one loop iteration issue-watch calls `gh issue view` (state) then `gh api .../comments`
(comments); the stub returns index k for both, then advances k on the comments call
(the iteration's last gh call). Indices clamp to the last element so the sequence
stays stable once exhausted (drives timeout + stabilization cases).

ISSUE_WATCH_POLL_INTERVAL=0 drives the loop without the 30s production cadence.
Fire cases assert exit 0 + the terminal line (and body where applicable); the
timeout case uses a short timeout and asserts exit 1.

Run: python3 test-issue-watch.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "issue-watch.sh")
REPO = "owner/repo"
ISSUE = "42"

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"
    print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


GH_STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
STATES = json.loads(os.environ.get("ISSUE_STATES_JSON", "[]"))
COMMENTS = json.loads(os.environ.get("ISSUE_COMMENTS_JSON", "[]"))
counter_file = os.environ["STUB_COUNTER"]
try:
    idx = int(open(counter_file).read().strip() or "0")
except Exception:
    idx = 0

def clamp(seq):
    if not seq:
        return None
    return seq[min(idx, len(seq) - 1)]

def emit(data):
    if "--jq" in args:
        expr = args[args.index("--jq") + 1]
        try:
            p = subprocess.run(["jq", "-r", expr], input=data, capture_output=True, text=True)
        except FileNotFoundError:
            sys.stderr.write("jq not found\n")
            sys.exit(127)
        # Propagate jq failure (missing binary / bad expr) instead of masking it as
        # empty output + exit 0, which would produce false baselines (Codoki #218).
        if p.returncode != 0:
            sys.stderr.write(p.stderr)
            sys.exit(p.returncode)
        sys.stdout.write(p.stdout)
    else:
        sys.stdout.write(data)
    sys.exit(0)

if args[:2] == ["repo", "view"]:
    emit(json.dumps({"nameWithOwner": os.environ.get("STUB_REPO", "owner/repo")}))

if args[:2] == ["issue", "view"]:
    st = clamp(STATES) or {"state": "OPEN", "labels": [], "assignees": []}
    emit(json.dumps(st))

# gh api ... /comments : return the comment array for this poll, THEN advance.
# FAIL_POLLS (comma list of poll indices) makes the comments call exit non-zero
# with NO output, simulating a transient gh error (rate-limit / auth / network).
if args[:1] == ["api"] and any("/comments" in a for a in args):
    fail = set(int(x) for x in os.environ.get("FAIL_POLLS", "").split(",") if x.strip())
    open(counter_file, "w").write(str(idx + 1))
    if idx in fail:
        sys.exit(1)  # gh failure: nothing on stdout, non-zero exit
    emit(json.dumps(clamp(COMMENTS) or []))

# Unknown endpoint -> empty (fail-open on the watcher side).
sys.stdout.write("")
sys.exit(0)
'''


def run(states, comments, extra_args, timeout_secs="5", fail_polls=""):
    """Run issue-watch.sh with a stubbed gh over the given scripted sequences.

    fail_polls: comma-separated poll indices at which the comments fetch exits
    non-zero (simulating a transient gh error) -- used to prove the fail-open retry.
    """
    with tempfile.TemporaryDirectory() as td:
        bind = os.path.join(td, "bin")
        os.makedirs(bind)
        ghp = os.path.join(bind, "gh")
        open(ghp, "w").write(GH_STUB)
        os.chmod(ghp, 0o755)
        counter = os.path.join(td, "counter")
        open(counter, "w").write("0")
        env = dict(os.environ)
        env["PATH"] = bind + os.pathsep + env["PATH"]
        env["ISSUE_STATES_JSON"] = json.dumps(states)
        env["ISSUE_COMMENTS_JSON"] = json.dumps(comments)
        env["STUB_COUNTER"] = counter
        env["STUB_REPO"] = REPO
        env["FAIL_POLLS"] = fail_polls
        env["ISSUE_WATCH_POLL_INTERVAL"] = "0"
        cmd = ["bash", SCRIPT] + extra_args + [ISSUE, REPO, timeout_secs]
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
        return p.returncode, p.stdout, p.stderr


OPEN0 = {"state": "OPEN", "labels": [], "assignees": []}


def comment(cid, login, body):
    return {"id": cid, "user": {"login": login}, "body": body}


print("issue-watch.sh proof harness (#217)")

# --- new-comment (default, any author) ---
rc, out, err = run(
    [OPEN0, OPEN0],
    [[], [comment(1, "alice", "first!")]],
    [],
)
check("new-comment: fires exit 0 with author+id", rc == 0 and "new-comment issue=42 author=alice id=1" in out)
check("new-comment: prints the comment body", "first!" in out)

# --- new-comment ignores comments present at baseline ---
rc, out, err = run(
    [OPEN0, OPEN0],
    [[comment(1, "alice", "pre-existing")], [comment(1, "alice", "pre-existing")]],
    [],
    timeout_secs="1",
)
check("baseline comment does NOT trip new-comment (times out)", rc == 1 and "timeout" in err)

# --- REGRESSION (hostile-review #1): a gh failure on the BASELINE comments fetch
# must NOT be masked as an empty baseline (which would false-fire on the real,
# pre-existing comments). The comments call fails at poll 0; the real comment (id 1)
# is present the whole time; a false new-comment would fire if the baseline captured
# an empty id-set. Correct behavior: no baseline until a clean poll -> no fire.
_c1 = [comment(1, "alice", "pre-existing")]
rc, out, err = run(
    [OPEN0, OPEN0, OPEN0, OPEN0],
    [_c1, _c1, _c1, _c1],
    [],
    timeout_secs="1",
    fail_polls="0",
)
check("gh failure on baseline comments fetch does NOT false-fire (times out)",
      rc == 1 and "timeout" in err and "new-comment" not in out)

# --- REGRESSION (hostile-review #2): --author picks the NEWEST new comment, so a
# self-editing plan (posted AFTER an ancillary comment from the same author) wins
# rather than latching on the older ancillary comment and masking the plan.
CR = "coderabbitai"
rc, out, err = run(
    [OPEN0, OPEN0, OPEN0, OPEN0],
    [
        [],                                                          # baseline
        [comment(8, CR, "ack: reviewing")],                          # ancillary (older) appears
        [comment(8, CR, "ack: reviewing"), comment(9, CR, "PLAN")],  # plan (newer) appears -> track it
        [comment(8, CR, "ack: reviewing"), comment(9, CR, "PLAN")],  # plan unchanged -> stabilized
    ],
    ["--author", CR],
)
check("--author fires plan-ready on the NEWEST author comment (id 9), not the older ack (id 8)",
      rc == 0 and "plan-ready issue=42 author=coderabbitai id=9" in out and "PLAN" in out)

# --- plan-ready (--author, stabilization across 2 polls) ---
rc, out, err = run(
    [OPEN0, OPEN0, OPEN0, OPEN0],
    [
        [],                                            # baseline: no plan yet
        [comment(9, CR, "Plan (writing...)")],         # plan appears -> hold
        [comment(9, CR, "Plan FINAL")],                # body changed -> keep holding
        [comment(9, CR, "Plan FINAL")],                # unchanged vs prev -> stabilized
    ],
    ["--author", CR],
)
check("plan-ready: fires only after the author comment stabilizes", rc == 0 and "plan-ready issue=42 author=coderabbitai id=9" in out)
check("plan-ready: prints the stabilized body", "Plan FINAL" in out and "writing..." not in out)

# --- REGRESSION (Copilot #218): --author <bare> matches the `<bare>[bot]` REST login ---
rc, out, err = run(
    [OPEN0, OPEN0, OPEN0],
    [
        [],
        [comment(11, "coderabbitai[bot]", "Coding Plan")],
        [comment(11, "coderabbitai[bot]", "Coding Plan")],
    ],
    ["--author", "coderabbitai"],  # bare name must match the [bot]-suffixed login
)
check("--author bare name matches the [bot]-suffixed App login",
      rc == 0 and "plan-ready issue=42 author=coderabbitai[bot] id=11" in out)

# --- --author ignores a non-target author's comment ---
rc, out, err = run(
    [OPEN0, OPEN0, OPEN0],
    [[], [comment(5, "bob", "unrelated")], [comment(5, "bob", "unrelated")]],
    ["--author", CR],
    timeout_secs="1",
)
check("--author: a non-target author's comment does NOT fire (times out)", rc == 1 and "timeout" in err)

# --- closed fires immediately, even in --author mid-stabilization ---
CLOSED = {"state": "CLOSED", "labels": [], "assignees": []}
rc, out, err = run(
    [OPEN0, CLOSED],
    [[], [comment(9, CR, "half-written plan")]],
    ["--author", CR],
)
check("closed: fires immediately regardless of --author", rc == 0 and out.strip() == "closed issue=42")

# --- labeled (added) ---
rc, out, err = run(
    [OPEN0, {"state": "OPEN", "labels": [{"name": "bug"}], "assignees": []}],
    [[], []],
    [],
)
check("labeled: reports the added label", rc == 0 and "labeled issue=42" in out and "+bug" in out)

# --- assigned (added) ---
rc, out, err = run(
    [OPEN0, {"state": "OPEN", "labels": [], "assignees": [{"login": "alice"}]}],
    [[], []],
    [],
)
check("assigned: reports the added assignee", rc == 0 and "assigned issue=42" in out and "+alice" in out)

# --- reopened ---
rc, out, err = run(
    [CLOSED, OPEN0],
    [[], []],
    [],
)
check("reopened: reports CLOSED->OPEN", rc == 0 and out.strip() == "reopened issue=42")

# --- timeout: no change ever ---
rc, out, err = run(
    [OPEN0, OPEN0],
    [[], []],
    [],
    timeout_secs="1",
)
check("timeout: exit 1 with 'timeout: waited' on stderr", rc == 1 and "timeout: waited" in err)

# --- setup errors ---
def run_raw(extra):
    p = subprocess.run(["bash", SCRIPT] + extra, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr

rc, out, err = run_raw(["--author"])
check("setup: --author with no login exits 2", rc == 2 and "setup error" in err)
rc, out, err = run_raw(["notanumber", REPO])
check("setup: non-numeric issue exits 2", rc == 2 and "setup error" in err)
rc, out, err = run_raw([])
check("setup: no args exits 2 (usage)", rc == 2)

# --- setup: jq missing -> fail fast (Codoki #218), not a silent timeout ---
with tempfile.TemporaryDirectory() as td:
    bind = os.path.join(td, "bin")
    os.makedirs(bind)
    # A gh stub is present so the gh check passes; jq is absent from this PATH so
    # the jq check must trip. PATH is bind-only (no jq); bash is invoked by its
    # absolute path so the restricted PATH doesn't hide the interpreter itself.
    open(os.path.join(bind, "gh"), "w").write("#!/bin/sh\nexit 0\n")
    os.chmod(os.path.join(bind, "gh"), 0o755)
    env = dict(os.environ)
    env["PATH"] = bind
    p = subprocess.run([shutil.which("bash"), SCRIPT, ISSUE, REPO], capture_output=True, text=True, env=env, timeout=30)
    check("setup: jq missing exits 2 with a clear error (not a silent timeout)",
          p.returncode == 2 and "jq" in p.stderr)

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
    sys.exit(1)
print("all issue-watch.sh checks passed")
