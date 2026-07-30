#!/usr/bin/env python3
"""Proof harness for cr-quota-watch.sh -- the READ-ONLY CodeRabbit quota surfacer.

CodeRabbit announces its own remaining quota in two different sentences, and a human
never sees either one reliably:

  1. The rate-limit reply to a status query / blocked trigger:
        "... Your next review will be available in 59 minutes."
  2. The acknowledgment appended to a review it DID perform:
        "... Your next INCLUDED review will be available in 54 minutes."
     GitHub wraps this one in a <details> block that renders COLLAPSED, so the visible
     summary is only "Action performed" -- the quota sentence is invisible until clicked.

This watcher reads those lines and prints them. It POSTS NOTHING and triggers nothing,
so it can never consume a review slot.

Every fixture body below is VERBATIM from a real CodeRabbit comment (sydlexius/stillwater
#2806/#2807/#2813, fetched 2026-07-30), not hand-written prose. The matcher is the front
half of the full requester, so proving it against real bytes is the whole point.

The harness stubs every external dependency -- `gh` is a temp 0755 script first on PATH
serving canned JSON from $COMMENTS_JSON -- so it never touches the network and never
posts. Host timezone is irrelevant: assertions are on relative durations and on a
TZ-pinned Pacific label computed the same way the script computes it.

Contract asserted:
  exit 0  no ACTIVE limit (no signal / newest signal expired / "available now")
  exit 1  LIMITED -- newest signal's deadline is still in the future; line surfaced
  exit 2  setup error (bad args, unresolvable repo, gh read failure)

Run: python3 test-cr-quota-watch.py
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "cr-quota-watch.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


# --- Fixtures ---------------------------------------------------------------
CR = "coderabbitai[bot]"

# VERBATIM, stillwater #2807 2026-07-25T23:15:08Z (the "next review" noun phrase).
RL_TEMPLATE = (
    "<!-- This is an auto-generated reply by CodeRabbit -->\n"
    "You're currently rate limited under our [Fair Usage Limits Policy]"
    "(https://docs.coderabbit.ai/management/plans#fair-usage-limits-policy). Your recent PR "
    "review activity is in the 95th percentile or higher among CodeRabbit users, so adaptive "
    "limits apply. Your next review will be available in {dur}."
)

# VERBATIM, stillwater #2806 2026-07-25T22:18:50Z -- the <details>-COLLAPSED variant with
# the DIFFERENT noun phrase ("next INCLUDED review"). A regex tuned only to the reply above
# misses this one, and it is the better signal: it reports the spent slot as the review lands.
INCLUDED_TEMPLATE = (
    "<!-- This is an auto-generated reply by CodeRabbit -->\n"
    "<!-- CodeRabbit review command invocation: 4cc1d5fe-94a8-4316-a1a3-2a9ed7bcd300 -->\n"
    "<details>\n"
    "<summary>✅ Action performed</summary>\n\n"
    "Full review finished.\n\n---\n\n"
    "Your included review limit is currently reached under our [Fair Usage Limits Policy]"
    "(https://docs.coderabbit.ai/management/plans#fair-usage-limits-policy). Your recent PR "
    "review activity is in the 95th percentile or higher among CodeRabbit users, so adaptive "
    "limits apply. This review may still proceed through usage-based billing if eligible. "
    "Your next included review will be available in {dur}.\n\n"
    "</details>"
)

# VERBATIM available-state reply, measured on cc-orchestrator #351.
AVAILABLE_BODY = (
    "<!-- This is an auto-generated reply by CodeRabbit -->\n"
    "Your [plan](https://docs.coderabbit.ai/management/plans#fair-usage-limits-policy) includes PR "
    "reviews subject to [rate limits](https://docs.coderabbit.ai/management/plans#rate-limits).\n"
    "Reviews are available now."
)

# THE TRAP: the retired Codoki service used an ABSOLUTE UTC timestamp, and transcripts are
# full of these. A parser that leaks Codoki's format into the CR matcher reads a wall-clock
# time as a relative duration.
CODOKI_BODY = (
    "<!-- CODOKI_RATE_LIMIT -->\n"
    "Please wait 10 minutes 51 seconds before requesting another review.\n"
    "Next available slot: **2026-06-22 04:50:02 UTC**"
)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**kw):
    """An ISO8601 Z timestamp that many units in the PAST."""
    return iso(datetime.now(timezone.utc) - timedelta(**kw))


def comment(body, login=CR, created=None):
    ts = created or ago(minutes=1)
    return {"user": {"login": login}, "created_at": ts, "updated_at": ts, "body": body}


def comments_json(*objs):
    return json.dumps(list(objs))


def pacific_label(dt):
    """The America/Los_Angeles label the script emits ('%H:%M %Z'), or None if
    zoneinfo is unavailable (then the strict assertion is skipped)."""
    try:
        from zoneinfo import ZoneInfo
    except Exception:
        return None
    return dt.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%H:%M %Z")


def run(args, *, comments="[]", api_fail=False, repo_fail=False):
    """Invoke the watcher with a stubbed gh. Returns (rc, stdout, stderr)."""
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "case \"${1:-}\" in\n"
                "  repo) [ -n \"${GH_REPO_FAIL:-}\" ] && exit 1; echo 'owner/repo'; exit 0;;\n"
                "  api)  [ -n \"${GH_API_FAIL:-}\" ] && exit 1; printf '%s' \"${COMMENTS_JSON:-[]}\"; exit 0;;\n"
                "esac\n"
                "exit 0\n"
            )
        os.chmod(gh, 0o755)

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["COMMENTS_JSON"] = comments
        if api_fail:
            env["GH_API_FAIL"] = "1"
        if repo_fail:
            env["GH_REPO_FAIL"] = "1"

        p = subprocess.run([SCRIPT] + args, env=env, capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout, p.stderr


def main():
    print("== arg validation ==")
    rc, out, _ = run(["--help"])
    check("--help -> exit 0, prints usage", rc == 0 and "cr-quota-watch" in out)
    rc, _, _ = run([])
    check("no args -> exit 2", rc == 2)
    rc, _, _ = run(["notanumber"])
    check("non-numeric PR -> exit 2", rc == 2)
    rc, _, _ = run(["1", "owner/repo", "extra"])
    check("too many args -> exit 2", rc == 2)
    rc, _, err = run(["1"], repo_fail=True)
    check("repo unresolvable -> exit 2", rc == 2 and "setup error" in err)

    print("== no signal at all ==")
    rc, out, _ = run(["1", "owner/repo"], comments="[]")
    check("no comments -> exit 0 (no active limit)", rc == 0)
    rc, out, _ = run(["1", "owner/repo"],
                     comments=comments_json(comment("Just a normal human comment.", login="someuser")))
    check("unrelated comment -> exit 0", rc == 0)

    print("== the DEFAULT-HIDDEN signal: <details>-collapsed 'next INCLUDED review' ==")
    body = INCLUDED_TEMPLATE.format(dur="54 minutes")
    posted = datetime.now(timezone.utc) - timedelta(minutes=4)
    rc, out, err = run(["2806", "owner/repo"], comments=comments_json(comment(body, created=iso(posted))))
    text = out + err
    check("collapsed 'included review' line -> exit 1 (LIMITED)", rc == 1)
    check("surfaces the PR it came from", "2806" in text)
    check("reports remaining time, not the raw 54 (posted 4m ago -> ~50m left)", "50m" in text)
    lbl = pacific_label(posted + timedelta(minutes=54))
    if lbl is None:
        check("zoneinfo unavailable -> skip strict Pacific assertion (informational)", True)
    else:
        check(f"deadline shown as a Pacific-labeled time ('{lbl}')", lbl in text)

    print("== the other noun phrase: 'your next review' (rate-limit reply) ==")
    rc, out, err = run(["2807", "owner/repo"],
                       comments=comments_json(comment(RL_TEMPLATE.format(dur="59 minutes"),
                                                      created=ago(minutes=2))))
    check("'next review will be available in' -> exit 1 (LIMITED)", rc == 1)
    check("reports ~57m remaining", "57m" in (out + err))

    print("== unit and plurality variants that a naive '(\\d+) minutes' breaks on ==")
    # Every one of these is a REAL observed duration string.
    for dur, expect_rc, why in [
        ("1 minute", 1, "SINGULAR minute"),
        ("4 seconds", 1, "DIFFERENT unit: seconds"),
        ("2 hours", 1, "hours (unmeasured but must not silently fail to parse)"),
        ("1 hour", 1, "SINGULAR hour"),
    ]:
        rc, out, err = run(["1", "owner/repo"],
                           comments=comments_json(comment(RL_TEMPLATE.format(dur=dur),
                                                          created=ago(seconds=1))))
        check(f"'available in {dur}.' parsed -> exit 1 ({why})", rc == 1)

    print("== expiry: a signal whose deadline has PASSED is not an active limit ==")
    rc, out, err = run(["1", "owner/repo"],
                       comments=comments_json(comment(RL_TEMPLATE.format(dur="5 minutes"),
                                                      created=ago(hours=3))))
    check("3h-old '5 minutes' signal -> exit 0 (expired, not still limited)", rc == 0)

    print("== 'Reviews are available now.' ==")
    rc, out, err = run(["1", "owner/repo"], comments=comments_json(comment(AVAILABLE_BODY)))
    check("available-state reply -> exit 0", rc == 0)
    check("says reviews are available", "available" in (out + err).lower())

    print("== newest signal wins (the countdown is NON-MONOTONIC; never count down locally) ==")
    old = comment(RL_TEMPLATE.format(dur="5 minutes"), created=ago(minutes=90))
    new = comment(RL_TEMPLATE.format(dur="59 minutes"), created=ago(minutes=2))
    rc, out, err = run(["1", "owner/repo"], comments=comments_json(old, new))
    check("older expired + newer active -> exit 1 (newest wins)", rc == 1)
    check("reports the NEWER remaining time (~57m), not the older", "57m" in (out + err))
    # ... and in the other order in the array, so ordering is by timestamp not position.
    rc, out, err = run(["1", "owner/repo"], comments=comments_json(new, old))
    check("array order reversed -> same verdict (sorted by timestamp, not position)",
          rc == 1 and "57m" in (out + err))
    # A newer AVAILABLE reply clears an older limit signal.
    rc, out, err = run(["1", "owner/repo"],
                       comments=comments_json(comment(RL_TEMPLATE.format(dur="59 minutes"),
                                                      created=ago(minutes=30)),
                                              comment(AVAILABLE_BODY, created=ago(minutes=1))))
    check("newer 'available now' overrides an older active limit -> exit 0", rc == 0)

    print("== TRAP: Codoki's ABSOLUTE-timestamp format must never match ==")
    rc, out, err = run(["1", "owner/repo"],
                       comments=comments_json(comment(CODOKI_BODY, login="codoki-pr-intelligence[bot]")))
    check("Codoki rate-limit body -> exit 0 (not read as a CR relative duration)", rc == 0)
    check("Codoki's wall-clock slot never surfaces as a CR deadline", "04:50:02" not in (out + err))

    print("== trust boundary: only coderabbitai[bot] counts ==")
    body = RL_TEMPLATE.format(dur="59 minutes")
    rc, out, err = run(["1", "owner/repo"], comments=comments_json(comment(body, login="impersonator")))
    check("spoofed quota line from a non-CR author -> exit 0 (ignored)", rc == 0)
    rc, out, err = run(["1", "owner/repo"],
                       comments=comments_json(comment(body, login="impersonator"),
                                              comment(body, created=ago(minutes=2))))
    check("genuine CR signal alongside a spoof -> exit 1", rc == 1)

    print("== gh read failure must not read as 'no limit' ==")
    rc, out, err = run(["1", "owner/repo"], comments="[]", api_fail=True)
    check("gh api read fails -> exit 2 (setup error, never a false exit 0)",
          rc == 2 and "setup error" in err)

    print("== read-only: the script never posts ==")
    src = open(SCRIPT).read()
    for forbidden in ["gh pr comment", "gh api -X POST", '--method POST', "-X POST", "reply-comment"]:
        check(f"source contains no '{forbidden}'", forbidden not in src)
    check("source contains no @coderabbitai trigger string", "@coderabbitai" not in src)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
