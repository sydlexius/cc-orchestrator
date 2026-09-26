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

  3. (#454) The banner in CR's SUMMARY comment, which CR EDITS IN PLACE:
        "**Next included review available in 49 minutes."
     Capital N and no "will be"; its countdown dates from `updated_at`, not
     `created_at`, so the harness also asserts the deadline, the selection rule, and
     the missing/unusable-`updated_at` fallback against that field. An EDIT never
     outranks a newer, longer limit: "available" signals date from `created_at` only,
     and among limited signals newer than the newest "available" the LARGEST deadline
     wins (454-F1).

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

#467: a compound duration ("1 hour and 5 minutes.") is summed; a recognized CR limit
phrase whose duration cannot be parsed is LIMITED ("deadline UNKNOWN") until a 1h
ceiling. #456: the scan covers the queried PR plus the 10 most recently updated PRs,
and the selection rule runs across all of them.

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

# The #454 SUMMARY-comment banner (capital N, no "will be"). Only the quota sentence is
# load-bearing here; the surrounding summary markup is abbreviated.
SUMMARY_TEMPLATE = (
    "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
    "> [!NOTE]\n"
    "> **Next included review available in {dur}.**\n"
)

# VERBATIM banner forms from the 2026-09-26 survey (canticle #1056, cc-orchestrator #397,
# canticle #918), with the count and allowance parameterized.
BANNER_A = (
    "**Included review availability:** {n} currently available. Your included PR review "
    "attempts over the past 7 days set your current allowance at {al} per hour."
)
BANNER_B = (
    "**Included review availability:** Your plan provides up to {al} included review per hour; "
    "{n} remain after this review."
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


def comment(body, login=CR, created=None, updated=None, drop_updated=False):
    ts = created or ago(minutes=1)
    c = {"user": {"login": login}, "created_at": ts, "updated_at": updated or ts, "body": body}
    if drop_updated:
        del c["updated_at"]
    return c


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


GH_LOG = []  # the endpoints the last run() asked gh for, in order


def run(args, *, comments="[]", api_fail=False, repo_fail=False, pulls=None, per_pr=None,
        pulls_fail=False, fail_pr=None):
    """Invoke the watcher with a stubbed gh. Returns (rc, stdout, stderr).

    `pulls` is the recently-updated PR list (#456); `per_pr` maps a PR number to its own
    comments JSON, and any PR without an entry is served `comments`. GH_LOG records every
    endpoint read, so a case can prove a PR beyond the scan bound was never read.
    By default the list holds just the queried PR: an EMPTY list is a read failure (Q2)."""
    if pulls is None:
        pulls = json.dumps([{"number": int(args[0])}]) if args and args[0].isdigit() else "[]"
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        cdir = os.path.join(td, "comments"); os.makedirs(cdir)
        log = os.path.join(td, "gh.log")
        for n, body in (per_pr or {}).items():
            with open(os.path.join(cdir, f"{n}.json"), "w") as f:
                f.write(body)
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "case \"${1:-}\" in\n"
                "  repo) [ -n \"${GH_REPO_FAIL:-}\" ] && exit 1; echo 'owner/repo'; exit 0;;\n"
                "  api)  [ -n \"${GH_API_FAIL:-}\" ] && exit 1\n"
                "        ep=''; for a in \"$@\"; do case \"$a\" in repos/*) ep=\"$a\";; esac; done\n"
                "        printf '%s\\n' \"$ep\" >> \"$GH_LOG\"\n"
                "        case \"$ep\" in\n"
                "          */pulls\\?*) [ -n \"${GH_PULLS_FAIL:-}\" ] && exit 1\n"
                "                     printf '%s' \"${PULLS_JSON-[]}\"; exit 0;;\n"
                "          */issues/*/comments) n=${ep#*/issues/}; n=${n%%/*}\n"
                "                     [ \"${GH_FAIL_PR:-}\" = \"$n\" ] && exit 1\n"
                "                     if [ -f \"$COMMENTS_DIR/$n.json\" ]; then cat \"$COMMENTS_DIR/$n.json\"\n"
                "                     else printf '%s' \"${COMMENTS_JSON-[]}\"; fi; exit 0;;\n"
                "        esac; exit 1;;\n"
                "esac\n"
                "exit 0\n"
            )
        os.chmod(gh, 0o755)

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["COMMENTS_JSON"] = comments
        env["PULLS_JSON"] = pulls
        env["COMMENTS_DIR"] = cdir
        env["GH_LOG"] = log
        for k in ("GH_API_FAIL", "GH_REPO_FAIL", "GH_PULLS_FAIL", "GH_FAIL_PR"):
            env.pop(k, None)
        if api_fail:
            env["GH_API_FAIL"] = "1"
        if repo_fail:
            env["GH_REPO_FAIL"] = "1"
        if pulls_fail:
            env["GH_PULLS_FAIL"] = "1"
        if fail_pr is not None:
            env["GH_FAIL_PR"] = str(fail_pr)

        p = subprocess.run([SCRIPT] + args, env=env, capture_output=True, text=True, timeout=30)
        GH_LOG[:] = open(log).read().splitlines() if os.path.exists(log) else []
        return p.returncode, p.stdout, p.stderr


def pulls_json(*numbers):
    return json.dumps([{"number": n, "updated_at": ago(minutes=1)} for n in numbers])


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

    print("== #454: the SUMMARY banner ('Next included review available in', capital N) ==")
    rc, out, err = run(["454", "owner/repo"],
                       comments=comments_json(comment(SUMMARY_TEMPLATE.format(dur="49 minutes"),
                                                      created=ago(minutes=2))))
    check("capitalized banner without 'will be' -> exit 1 (LIMITED)", rc == 1)
    check("banner reports ~47m remaining and the included-review noun",
          "47m" in (out + err) and "included review" in (out + err))

    print("== #454: an EDITED summary dates its countdown from updated_at ==")
    edited = comment(SUMMARY_TEMPLATE.format(dur="59 minutes"),
                     created=ago(minutes=34), updated=ago(minutes=1))
    rc, out, err = run(["454", "owner/repo"], comments=comments_json(edited))
    check("edited summary (updated 1m ago, created 34m ago) -> exit 1", rc == 1)
    check("deadline from updated_at (~58m), not created_at (~25m)", "58m" in (out + err))
    rc, out, err = run(["454", "owner/repo"],
                       comments=comments_json(comment(SUMMARY_TEMPLATE.format(dur="20 minutes"),
                                                      created=ago(minutes=34),
                                                      updated=ago(minutes=1))))
    check("20-minute notice edited 1m ago on a 34m-old summary -> still LIMITED (exit 1)",
          rc == 1 and "19m" in (out + err))

    print("== #454: newest-signal ordering is by updated_at ==")
    reply = comment(RL_TEMPLATE.format(dur="10 minutes"), created=ago(minutes=5))
    summ = comment(SUMMARY_TEMPLATE.format(dur="40 minutes"),
                   created=ago(minutes=60), updated=ago(minutes=1))
    for order, objs in [("summary last", (reply, summ)), ("summary first", (summ, reply))]:
        rc, out, err = run(["1", "owner/repo"], comments=comments_json(*objs))
        check(f"older-created but more-recently-edited summary wins ({order}) -> ~39m",
              rc == 1 and "39m" in (out + err))

    print("== #454: a missing updated_at falls back to created_at ==")
    rc, out, err = run(["1", "owner/repo"],
                       comments=comments_json(comment(RL_TEMPLATE.format(dur="30 minutes"),
                                                      created=ago(minutes=2),
                                                      drop_updated=True)))
    check("no updated_at key -> deadline from created_at (~28m)", rc == 1 and "28m" in (out + err))
    # 454-F3: a PRESENT but unusable updated_at (garbage string, JSON null) must take the
    # same created_at fallback, not drop the signal or date it from nothing.
    for label, bad in [("garbage-string", "garbage"), ("null", None)]:
        c = comment(RL_TEMPLATE.format(dur="30 minutes"), created=ago(minutes=2))
        c["updated_at"] = bad
        rc, out, err = run(["1", "owner/repo"], comments=comments_json(c))
        check(f"{label} updated_at -> deadline from created_at (~28m)",
              rc == 1 and "28m" in (out + err))

    print("== 454-F1: an EDIT never outranks a newer, longer limit ==")
    # An old 'available now' reply edited 1m ago: an edit does not re-assert availability,
    # so the fresh 59-minute limit (5m ago) still stands.
    fresh = comment(RL_TEMPLATE.format(dur="59 minutes"), created=ago(minutes=5))
    old_av = comment(AVAILABLE_BODY, created=ago(minutes=90), updated=ago(minutes=1))
    for order, objs in [("available last", (fresh, old_av)), ("available first", (old_av, fresh))]:
        rc, out, err = run(["1", "owner/repo"], comments=comments_json(*objs))
        check(f"edited old 'available now' vs fresh 59m limit ({order}) -> LIMITED ~54m",
              rc == 1 and "54m" in (out + err))
    # A 120m-old summary whose STALE '2 minutes' banner survived an unrelated edit 1m ago:
    # the largest live deadline wins, not the most recently touched comment.
    fresh = comment(RL_TEMPLATE.format(dur="59 minutes"), created=ago(minutes=2))
    stale = comment(SUMMARY_TEMPLATE.format(dur="2 minutes"),
                    created=ago(minutes=120), updated=ago(minutes=1))
    for order, objs in [("summary last", (fresh, stale)), ("summary first", (stale, fresh))]:
        rc, out, err = run(["1", "owner/repo"], comments=comments_json(*objs))
        check(f"stale 2m banner edited 1m ago vs fresh 59m reply ({order}) -> ~57m, not ~1m",
              rc == 1 and "57m" in (out + err))

    print("== 454-F2: an upper-case unit is still minutes, not hours ==")
    rc, out, err = run(["1", "owner/repo"],
                       comments=comments_json(comment("Next included review available in 49 MINUTES.",
                                                      created=ago(minutes=1))))
    check("'49 MINUTES.' -> ~48m (unit downcased, not the 3600 multiplier)",
          rc == 1 and "~48m" in (out + err))

    print("== 454-F5: a malformed comment neither blinds the scan nor reads as all-clear ==")
    numeric = {"user": {"login": CR}, "created_at": ago(minutes=1), "updated_at": ago(minutes=1),
               "body": 5}
    live = comment(RL_TEMPLATE.format(dur="30 minutes"), created=ago(minutes=1))
    rc, out, err = run(["1", "owner/repo"], comments=comments_json(numeric, live))
    check("numeric-body CR comment beside a live 30m limit -> still LIMITED (exit 1)",
          rc == 1 and "29m" in (out + err))
    # A non-array response (an error object) makes jq fail at evaluation; that must be a
    # setup error, never swallowed into "no quota signal" / exit 0.
    rc, out, err = run(["1", "owner/repo"], comments='{"message": "Not Found"}')
    check("non-array comments response -> exit 2 (jq failure is a setup error)",
          rc == 2 and "setup error" in err)

    # A null / false body passes `add // []` as an empty set; it must be refused, and so
    # must malformed JSON (a truncated body), never read as "no quota signal".
    for label, body in [("null", "null"), ("false", "false"), ("malformed", '[{"user":')]:
        rc, out, err = run(["1", "owner/repo"], comments=body)
        check(f"{label} comments response -> exit 2 (not a false all-clear)",
              rc == 2 and "setup error" in err)

    print("== a limit TIED with 'available' on the same second stays LIMITED ==")
    ts = ago(minutes=1)
    for order, objs in [("available last", (comment(RL_TEMPLATE.format(dur="30 minutes"), created=ts),
                                            comment(AVAILABLE_BODY, created=ts))),
                        ("available first", (comment(AVAILABLE_BODY, created=ts),
                                             comment(RL_TEMPLATE.format(dur="30 minutes"), created=ts)))]:
        rc, out, err = run(["1", "owner/repo"], comments=comments_json(*objs))
        check(f"same-second limit and available ({order}) -> LIMITED ~29m",
              rc == 1 and "29m" in (out + err))

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

    print("== #467: COMPOUND durations are summed, never read as 'no limit' ==")
    for dur, created, want, why in [
        ("1 hour and 5 minutes", ago(minutes=1), "~1h 4m", "the reported shape"),
        ("1 hour, 5 minutes, and 4 seconds", ago(minutes=1), "~1h 4m", "comma list + ', and'"),
        ("2 Hours, 1 minute", ago(minutes=1), "~2h 0m", "comma only, mixed case, plural+singular"),
        ("5 Minutes and 50 seconds", ago(minutes=1), "~5m", "minutes + seconds"),
    ]:
        rc, out, err = run(["1", "owner/repo"],
                           comments=comments_json(comment(RL_TEMPLATE.format(dur=dur), created=created)))
        check(f"'available in {dur}.' -> LIMITED {want} ({why})", rc == 1 and want in (out + err))

    print("== #467: a recognized limit phrase with an UNPARSEABLE duration is LIMITED ==")
    for dur in ["1 hour 5 minutes", "a little while"]:
        rc, out, err = run(["1", "owner/repo"],
                           comments=comments_json(comment(RL_TEMPLATE.format(dur=dur),
                                                          created=ago(minutes=5))))
        check(f"'available in {dur}.' 5m ago -> exit 1, 'deadline UNKNOWN', ~55m ceiling",
              rc == 1 and "deadline UNKNOWN" in (out + err) and "~55m" in (out + err))
    rc, out, err = run(["1", "owner/repo"],
                       comments=comments_json(comment(RL_TEMPLATE.format(dur="a little while"),
                                                      created=ago(minutes=61))))
    check("unparseable notice older than the 1h ceiling -> EXPIRED, exit 0",
          rc == 0 and "NO usable countdown" in (out + err))

    print("== the AVAILABILITY BANNER: a remaining-slot count (survey 2026-09-26) ==")
    zero = comment(BANNER_A.format(n="0 reviews are", al="1 review"),
                   created=ago(minutes=90), updated=ago(minutes=5))
    rc, out, err = run(["1", "owner/repo"], comments=comments_json(zero))
    check("current form '0 reviews are currently available' (edited 5m ago) -> LIMITED, "
          "deadline UNKNOWN, ~55m from updated_at",
          rc == 1 and "deadline UNKNOWN" in out and "~55m" in out)
    rc, out, err = run(["1", "owner/repo"],
                       comments=comments_json(comment(BANNER_B.format(al="1", n="0"),
                                                      created=ago(minutes=5))))
    check("older form '0 remain after this review' -> LIMITED, deadline UNKNOWN",
          rc == 1 and "deadline UNKNOWN" in out)
    old_zero = comment(BANNER_A.format(n="0 reviews are", al="1 review"), created=ago(minutes=10))
    rc, out, err = run(["1", "owner/repo"],
                       comments=comments_json(old_zero,
                                              comment(BANNER_A.format(n="9 reviews are", al="10 reviews"),
                                                      created=ago(minutes=2))))
    check("newer '9 reviews available' clears an older zero -> exit 0, surfaces N and allowance",
          rc == 0 and "9 included reviews available" in out and "10/hour" in out)
    rc, out, err = run(["1", "owner/repo"],
                       comments=comments_json(comment(AVAILABLE_BODY, created=ago(minutes=10)),
                                              comment(BANNER_A.format(n="0 reviews are", al="1 review"),
                                                      created=ago(minutes=2))))
    check("newer zero banner overrides an older 'available now' -> LIMITED", rc == 1)
    rc, out, err = run(["1", "owner/repo"],
                       comments=comments_json(comment(BANNER_A.format(n="0 reviews are", al="1 review"),
                                                      created=ago(minutes=61))))
    check("zero banner older than the 1h ceiling -> EXPIRED, exit 0",
          rc == 0 and "NO usable countdown" in out)
    rc, out, err = run(["1", "owner/repo"],
                       comments=comments_json(comment(BANNER_A.format(n="1 review is", al="1 review"),
                                                      created=ago(minutes=2))))
    check("singular '1 review is currently available' -> exit 0 (clear), says 1 included review",
          rc == 0 and "1 included review available" in out)

    print("== #456: account-wide scan across recently updated PRs ==")
    live = comments_json(comment(RL_TEMPLATE.format(dur="30 minutes"), created=ago(minutes=1)))
    rc, out, err = run(["1", "owner/repo"], pulls=pulls_json(2, 1), per_pr={2: live})
    check("limit on a DIFFERENT PR than the queried one -> exit 1, names it",
          rc == 1 and "PR #2" in out and "account-wide" in out and "29m" in out)
    check("PR list read with the fixed bound (state=all, sort=updated, per_page=10)",
          "repos/owner/repo/pulls?state=all&sort=updated&direction=desc&per_page=10" in GH_LOG)
    check("queried PR in the list is read ONCE (deduplicated)",
          GH_LOG.count("repos/owner/repo/issues/1/comments") == 1)
    old_limit = comments_json(comment(RL_TEMPLATE.format(dur="59 minutes"), created=ago(minutes=30)))
    fresh_av = comments_json(comment(AVAILABLE_BODY, created=ago(minutes=1)))
    rc, out, err = run(["1", "owner/repo"], pulls=pulls_json(2), per_pr={1: old_limit, 2: fresh_av})
    check("'available' on PR B newer than a limit on PR A -> exit 0", rc == 0)
    rc, out, err = run(["1", "owner/repo"], pulls=pulls_json(2), per_pr={1: live})
    check("queried PR absent from the list is still scanned -> exit 1",
          rc == 1 and "issues/1/comments" in " ".join(GH_LOG))
    rc, out, err = run(["1", "owner/repo"], pulls_fail=True)
    check("PR list read failure -> exit 2", rc == 2 and "setup error" in err)
    rc, out, err = run(["1", "owner/repo"], pulls=pulls_json(2), fail_pr=2)
    check("a scanned PR's comment read failure -> exit 2", rc == 2 and "setup error" in err)
    for label, body in [("non-array", '{"message": "Not Found"}'),
                        ("non-numeric number", '[{"number": "2"}]'),
                        ("non-object entry", '[2]')]:
        rc, out, err = run(["1", "owner/repo"], pulls=body)
        check(f"{label} PR list -> exit 2", rc == 2 and "setup error" in err)
    nums = list(range(101, 112))  # 11 PRs; the bound is 10
    rc, out, err = run(["1", "owner/repo"], pulls=pulls_json(*nums), per_pr={111: live})
    check("live limit on the 11th listed PR (beyond the bound) -> ignored, exit 0", rc == 0)
    check("the 11th listed PR is never read",
          not any("issues/111/" in e for e in GH_LOG))
    rc, out, err = run(["1", "owner/repo"], pulls=pulls_json(*nums), per_pr={110: live})
    check("live limit on the 10th listed PR (inside the bound) -> exit 1", rc == 1 and "PR #110" in out)

    print("== review round 1 (Q1-Q7) ==")
    rc, out, err = run(["1", "owner/repo"],
                       comments=comments_json(comment(RL_TEMPLATE.format(dur="10000000000000000000 seconds"),
                                                      created=ago(minutes=1))))
    check("Q1 absurd duration is capped (no int overflow to EXPIRED) -> exit 1, ~23h",
          rc == 1 and "~23h 59m" in out)
    for label, kw in [("empty PR list output", {"pulls": ""}),
                      ("'[]' PR list", {"pulls": "[]"}),
                      ("empty comments body", {"comments": ""})]:
        rc, out, err = run(["1", "owner/repo"], **kw)
        check(f"Q2 {label} -> exit 2, never a silent shrink", rc == 2 and "setup error" in err)
    rc, out, err = run(["1", "owner/repo"], pulls='[{"number": 1e3}]')
    check("Q3 exponent-form PR number -> exit 2", rc == 2 and "setup error" in err)
    rc, out, err = run(["01", "owner/repo"], pulls=pulls_json(1), per_pr={1: live})
    check("Q4 '01' normalizes to PR #1: read once, not labeled account-wide",
          rc == 1 and GH_LOG.count("repos/owner/repo/issues/1/comments") == 1
          and "account-wide" not in out)
    two_pages = (comments_json(comment("Just a normal human comment.", login="someuser"))
                 + comments_json(comment(RL_TEMPLATE.format(dur="30 minutes"), created=ago(minutes=1))))
    rc, out, err = run(["1", "owner/repo"], comments=two_pages)
    check("Q5 multi-page comments (limit on page 2) -> exit 1", rc == 1 and "29m" in out)
    for label, body in [("countdown", RL_TEMPLATE.format(dur="30 minutes")),
                        ("zero banner", BANNER_A.format(n="0 reviews are", al="1 review"))]:
        c = comment(body)
        c["created_at"] = "garbage"; c["updated_at"] = None
        rc, out, err = run(["1", "owner/repo"], comments=comments_json(c))
        check(f"Q7 {label} with no parseable timestamp -> exit 2, not dropped",
              rc == 2 and "no parseable timestamp" in err)

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
