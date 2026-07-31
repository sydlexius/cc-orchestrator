#!/usr/bin/env python3
"""Proof harness for elmer-tick.sh -- THE ONE WRITER of the review requester.

WHAT THIS IS: the only component in the elmer system that posts anything. Under the
CLAUDE.md carve-out (approved 2026-07-30) it holds the maintainer's own CodeRabbit
trigger, mechanized, and every bound of that carve-out is enforced in the script
rather than by the caller's discretion. So this harness's real job is not "does it
post" -- it is "does it DECLINE to post in every case where it must".

THE CENTRAL ASSERTION, repeated across most cases below: the post log is EMPTY.
A guard that is merely reported as working, while a post slips out, is the exact
failure this file exists to catch, so the cases assert the absence of the side
effect and not just an exit code.

Every external dependency is stubbed: `gh` is a temp script on PATH that records
what it was asked to post, and cr-quota-watch.sh is a sibling stub whose exit code
is set per-case. Nothing here touches the network.

Contract asserted:
  exit 0  did its job, INCLUDING the no-op cases (empty queue, throttled, cap
          spent, lock contended). A timer-driven loop must not read those as errors.
  exit 1  refused a specific entry (stale SHA, closed PR, non-incremental form,
          a review already at this head). The entry stays queued.
  exit 2  setup error (bad args, gh read failure, quota read failure, a post that
          failed, a drain that failed after a successful post).

Run: python3 test-elmer-tick.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "scripts", "elmer-tick.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


SHA = "a" * 40
OTHER_SHA = "b" * 40

GH_STUB = r"""#!/usr/bin/env bash
[ "${GH_FAIL:-0}" = "1" ] && exit 1
if [ "$1" = "pr" ] && [ "$2" = "comment" ]; then
  [ "${GH_POST_FAIL:-0}" = "1" ] && { echo "post rejected" >&2; exit 1; }
  # CONCURRENCY INSTRUMENTATION (only when GH_INTERVAL_LOG is set, so every other
  # case is unaffected). A post takes real time in reality, so the stub occupies the
  # critical section for GH_POST_SLEEP seconds and records the START and END of that
  # occupancy as ONE line. One python3 invocation does the timing AND the sleep, so
  # the appended line is short enough to be atomic under O_APPEND from N racers.
  if [ -n "${GH_INTERVAL_LOG:-}" ]; then
    python3 -c 'import sys,time
s=time.time(); time.sleep(float(sys.argv[1])); print(s, time.time())' \
      "${GH_POST_SLEEP:-1.5}" >> "$GH_INTERVAL_LOG"
  fi
  printf '%s\n' "$*" >> "$GH_POSTLOG"
  echo "https://github.com/x/y/pull/1#issuecomment-1"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  case "$*" in
    *reviews*) printf '{"reviews":%s}\n' "${GH_REVIEWS:-[]}" ; exit 0 ;;   # shape: [{author:{login},state,commit:{oid}}]
    *) printf '{"headRefOid":"%s","state":"%s"}\n' \
         "${GH_HEAD:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}" "${GH_STATE:-OPEN}" ; exit 0 ;;
  esac
fi
exit 0
"""

QUOTA_STUB = """#!/usr/bin/env bash
exit ${GH_QUOTA:-0}
"""


class Env:
    """A disposable elmer home + stubbed gh + a sibling quota stub.

    The script under test is COPIED next to the quota stub because it resolves
    cr-quota-watch.sh as its own sibling; running the repo copy would find the real
    one and make the throttle cases untestable. That is not a harness convenience --
    an earlier ad-hoc run did exactly that and reported a passing throttle case while
    the script actually posted.
    """

    def __init__(self, tmp):
        self.tmp = tmp
        self.home = os.path.join(tmp, "elmer")
        self.inbox = os.path.join(self.home, "inbox")
        self.drained = os.path.join(self.home, "drained")
        os.makedirs(self.inbox); os.makedirs(self.drained)
        self.bin = os.path.join(tmp, "bin"); os.makedirs(self.bin)
        self.postlog = os.path.join(tmp, "postlog")
        open(self.postlog, "w").close()
        self._write(os.path.join(self.bin, "gh"), GH_STUB)
        self.sd = os.path.join(tmp, "sd"); os.makedirs(self.sd)
        self._write(os.path.join(self.sd, "cr-quota-watch.sh"), QUOTA_STUB)
        with open(SCRIPT) as f:
            body = f.read()
        self.script = os.path.join(self.sd, "elmer-tick.sh")
        self._write(self.script, body)

    @staticmethod
    def _write(path, body):
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o755)

    def queue(self, pr, repo="sydlexius/cc-orchestrator", sha=SHA,
              form="incremental", at="2026-07-30T10:00:00Z"):
        entry = {"repo": repo, "pr": pr, "commit_sha": sha, "form": form,
                 "enqueued_at": at}
        with open(os.path.join(self.inbox, f"e-{pr}.json"), "w") as f:
            json.dump(entry, f)

    def drain_record(self, name, triggered_at):
        with open(os.path.join(self.drained, name), "w") as f:
            json.dump({"triggered_at": triggered_at}, f)

    def _env(self, extra):
        e = dict(os.environ)
        e["PATH"] = self.bin + os.pathsep + e["PATH"]
        e["ELMER_HOME"] = self.home
        e["GH_POSTLOG"] = self.postlog
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def run(self, **env):
        return subprocess.run([self.script], capture_output=True, text=True,
                              env=self._env(env), timeout=120)

    def run_stdout_closed(self, **env):
        """Run the tick with fd 1 CLOSED, the way a rotated/broken log redirect leaves it.

        `>&-` cannot be expressed through subprocess's stdout parameter (every value it
        accepts is an OPEN fd), so the closing is done by an exec'd bash wrapper -- the
        same construct that reproduced the defect by hand. stderr stays captured so the
        assertion can still see what the run reported.
        """
        return subprocess.run(["bash", "-c", 'exec "$1" >&-', "sh", self.script],
                              capture_output=True, text=True, env=self._env(env),
                              timeout=120)

    def run_args_stdout_closed(self, *args, **env):
        """run_stdout_closed, but with ARGUMENTS -- for the `-h` and bad-arg paths.

        Those two exit before the queue is ever read, so they need their own runner
        rather than a flag on the standard one.
        """
        return subprocess.run(["bash", "-c", 'exec "$@" >&-', "sh", self.script, *args],
                              capture_output=True, text=True, env=self._env(env),
                              timeout=120)

    def run_args_stderr_closed(self, *args, **env):
        """The stderr twin of run_args_stdout_closed."""
        return subprocess.run(["bash", "-c", 'exec "$@" 2>&-', "sh", self.script, *args],
                              capture_output=True, text=True, env=self._env(env),
                              timeout=120)

    def run_stderr_closed(self, **env):
        """Run the tick with fd 2 CLOSED -- the STDERR twin of run_stdout_closed.

        The suite mutation-proved five fixes and still passed with an unguarded
        `report_queue_health` in the file, for one reason: there was no closed-STDERR
        case at all. Every earlier case that exercised a stderr write had a working
        fd 2, so a write that could not fail was the only write ever tested. The
        queue-health report is stderr-ONLY by contract (the contended path must stay
        silent on stdout), so a stdout-closed run never touches it.

        stdout stays CAPTURED here, deliberately: the assertions need to see that the
        no-op paths still print their normal line while the stderr report is failing.
        """
        return subprocess.run(["bash", "-c", 'exec "$1" 2>&-', "sh", self.script],
                              capture_output=True, text=True, env=self._env(env),
                              timeout=120)

    @property
    def posts(self):
        with open(self.postlog) as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]

    def ls(self, which):
        d = self.inbox if which == "inbox" else self.drained
        return sorted(os.listdir(d))


def case(label):
    def deco(fn):
        with tempfile.TemporaryDirectory() as tmp:
            print(f"\n{label}")
            fn(Env(tmp))
        return fn
    return deco


# --- No-op paths: exit 0, and nothing posted --------------------------------------

@case("empty queue")
def _(env):
    r = env.run()
    check("exit 0", r.returncode == 0)
    check("posted nothing", env.posts == [])


@case("lock contended -> quiet 0 (a second loop window is normal)")
def _(env):
    os.makedirs(os.path.join(env.home, ".tick.lock"))
    env.queue(354)
    r = env.run()
    check("exit 0", r.returncode == 0)
    check("silent on stdout", r.stdout.strip() == "")
    check("posted nothing", env.posts == [])
    check("entry left queued", env.ls("inbox") == ["e-354.json"])


@case("stale lock is broken and reclaimed")
def _(env):
    lock = os.path.join(env.home, ".tick.lock")
    os.makedirs(lock)
    os.utime(lock, (1, 1))  # 1970: far past any threshold
    env.queue(354)
    r = env.run()
    check("proceeded past the lock", "breaking a stale tick lock" in r.stderr)
    check("posted once", len(env.posts) == 1)


@case("hourly cap reached -> 0, nothing posted")
def _(env):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(4):
        env.drain_record(f"old-{i}.json", now)
    env.queue(354)
    r = env.run()
    check("exit 0", r.returncode == 0)
    check("says cap reached", "hourly cap reached" in r.stdout)
    check("posted nothing", env.posts == [])


@case("cap counts an unreadable drain record AS a post (never discounts it)")
def _(env):
    with open(os.path.join(env.drained, "bad.json"), "w") as f:
        f.write('{"broken":')
    env.queue(354)
    r = env.run(ELMER_MAX_PER_HR=1)
    check("exit 0", r.returncode == 0)
    check("cap treated as spent", "hourly cap reached" in r.stdout)
    check("posted nothing", env.posts == [])


@case("drain records outside the hour do not count")
def _(env):
    env.drain_record("old.json", "2020-01-01T00:00:00Z")
    env.queue(354)
    r = env.run(ELMER_MAX_PER_HR=1)
    check("posted once", len(env.posts) == 1)
    check("exit 0", r.returncode == 0)


@case("CR throttled -> 0, nothing posted, entry stays queued")
def _(env):
    env.queue(354)
    r = env.run(GH_QUOTA=1)
    check("exit 0", r.returncode == 0)
    check("posted nothing", env.posts == [])
    check("entry left queued", env.ls("inbox") == ["e-354.json"])


# --- Refusals: exit 1, entry stays queued, nothing posted -------------------------

@case("stale head -> refuse (the receipt described a different SHA)")
def _(env):
    env.queue(354)
    r = env.run(GH_HEAD=OTHER_SHA)
    check("exit 1", r.returncode == 1)
    check("posted nothing", env.posts == [])
    check("names both SHAs", SHA[:12] in r.stderr and OTHER_SHA[:12] in r.stderr)
    check("entry left queued", env.ls("inbox") == ["e-354.json"])


@case("closed PR -> refuse")
def _(env):
    env.queue(354)
    r = env.run(GH_STATE="MERGED")
    check("exit 1", r.returncode == 1)
    check("posted nothing", env.posts == [])


@case("form=full -> refuse at the POSTING site, not just at enqueue")
def _(env):
    env.queue(354, form="full")
    r = env.run()
    check("exit 1", r.returncode == 1)
    check("posted nothing", env.posts == [])
    check("names the form", "'full'" in r.stderr)


# The fixtures below use the REAL `gh pr view --json reviews` shape:
#   {"author": {"login": ...}, "state": ..., "commit": {"oid": ...}}
# There is NO `commit_id` key. The first cut of both the guard AND this stub used one,
# so the test agreed with the bug and the guard was dead in production while every
# assertion passed. Verified live against a real PR before rewriting these.
# If a fixture here ever needs a new field, check it against a real PR first.
def review(sha, login="coderabbitai", state="COMMENTED"):
    return {"author": {"login": login}, "state": state, "commit": {"oid": sha}}


@case("a CodeRabbit review already exists at this head -> refuse")
def _(env):
    env.queue(354)
    r = env.run(GH_REVIEWS=json.dumps([review(SHA)]))
    check("exit 1", r.returncode == 1)
    check("posted nothing", env.posts == [])


@case("a review at a DIFFERENT head does not block")
def _(env):
    env.queue(354)
    r = env.run(GH_REVIEWS=json.dumps([review(OTHER_SHA)]))
    check("posted once", len(env.posts) == 1)
    check("exit 0", r.returncode == 0)


@case("a HUMAN review at this head must NOT suppress a wanted CR pass")
def _(env):
    env.queue(354)
    r = env.run(GH_REVIEWS=json.dumps([review(SHA, login="sydlexius", state="APPROVED")]))
    check("posted once", len(env.posts) == 1)
    check("exit 0", r.returncode == 0)


@case("the bot login is matched as coderabbitai[bot] too")
def _(env):
    env.queue(354)
    r = env.run(GH_REVIEWS=json.dumps([review(SHA, login="coderabbitai[bot]")]))
    check("exit 1", r.returncode == 1)
    check("posted nothing", env.posts == [])


# --- Setup errors: exit 2, never mistaken for "nothing to do" ---------------------

@case("gh read failure -> exit 2, not a silent skip")
def _(env):
    env.queue(354)
    r = env.run(GH_FAIL=1)
    check("exit 2", r.returncode == 2)
    check("posted nothing", env.posts == [])
    check("entry left queued", env.ls("inbox") == ["e-354.json"])


@case("quota read failure -> exit 2, does NOT fall through to a post")
def _(env):
    env.queue(354)
    r = env.run(GH_QUOTA=2)
    check("exit 2", r.returncode == 2)
    check("posted nothing", env.posts == [])


@case("post fails -> exit 2, entry stays queued, next tick recovers it")
def _(env):
    env.queue(354)
    r = env.run(GH_POST_FAIL=1)
    check("exit 2", r.returncode == 2)
    check("nothing drained", env.ls("drained") == [])
    check("entry left queued", env.ls("inbox") == ["e-354.json"])
    env.run()
    check("recovery posts once", len(env.posts) == 1)
    check("recovery drains it", len(env.ls("drained")) == 1)


@case("arguments are rejected (it services the queue, it takes none)")
def _(env):
    e = dict(os.environ); e["ELMER_HOME"] = env.home
    r = subprocess.run([env.script, "354"], capture_output=True, text=True, env=e)
    check("exit 2", r.returncode == 2)


# --- The success path and its invariants ------------------------------------------

@case("success: exactly one post, drained, and never repeated")
def _(env):
    env.queue(354)
    r = env.run()
    check("exit 0", r.returncode == 0)
    check("posted exactly once", len(env.posts) == 1)
    check("posted the INCREMENTAL trigger", "@coderabbitai review" in env.posts[0])
    check("did NOT post a full review", "full review" not in env.posts[0])
    check("inbox drained", env.ls("inbox") == [])
    check("drain record written", env.ls("drained") == ["e-354.json"])
    with open(os.path.join(env.drained, "e-354.json")) as f:
        rec = json.load(f)
    check("record carries triggered_at", bool(rec.get("triggered_at")))
    check("record carries the SHA", rec.get("commit_sha") == SHA)
    r2 = env.run()
    check("re-tick posts nothing", len(env.posts) == 1)
    check("re-tick exit 0", r2.returncode == 0)


@case("only ONE entry posts per tick (the quota signal is perishable)")
def _(env):
    env.queue(354); env.queue(355); env.queue(356)
    env.run()
    check("posted exactly once", len(env.posts) == 1)
    check("two entries still queued", len(env.ls("inbox")) == 2)


@case("stillwater outranks an older entry from another repo")
def _(env):
    env.queue(100, repo="sydlexius/cc-orchestrator", at="2026-07-30T01:00:00Z")
    env.queue(102, repo="sydlexius/stillwater", at="2026-07-30T23:00:00Z")
    env.run()
    check("posted once", len(env.posts) == 1)
    check("posted the stillwater PR", "102" in env.posts[0])


@case("without stillwater, oldest wins (FIFO)")
def _(env):
    env.queue(100, at="2026-07-30T08:00:00Z")
    env.queue(101, at="2026-07-30T07:00:00Z")
    env.run()
    check("posted the older entry", "101" in env.posts[0])


@case("an unparseable queue entry is skipped, never posted for")
def _(env):
    with open(os.path.join(env.inbox, "bad.json"), "w") as f:
        f.write('{"broken":')
    r = env.run()
    check("exit 0", r.returncode == 0)
    check("posted nothing", env.posts == [])


@case("an entry ALREADY in drained/ is never posted for (idempotency read, not trust)")
def _(env):
    # The tick used to claim "anything in drained/ is never re-posted" while nothing in
    # it ever READ drained/ - the de-dup lived only in elmer-enqueue.sh, a different
    # process. This asserts the tick enforces it itself.
    env.queue(354)
    env.drain_record("e-354.json", "2020-01-01T00:00:00Z")
    r = env.run()
    check("exit 0 (nothing pickable)", r.returncode == 0)
    check("posted nothing", env.posts == [])


@case("a post whose inbox rm fails exits 2, NEVER 1, and cannot double-post")
def _(env):
    # rc=1 means "POSTED NOTHING" per the contract, and the loop runbook hands the entry
    # back to the TL on a 1. A successful post reported as 1 makes every downstream
    # decision rest on a false premise. An unwritable inbox used to do exactly that via
    # `set -e` on the `rm -f`.
    env.queue(354)
    os.chmod(env.inbox, 0o555)
    try:
        r = env.run()
        check("posted once", len(env.posts) == 1)
        check("exit 2, not 1", r.returncode == 2)
        check("says POSTED", "POSTED" in r.stderr)
        check("drain record written", env.ls("drained") == ["e-354.json"])
    finally:
        os.chmod(env.inbox, 0o755)
    # The stale inbox entry now co-exists with its drain record; the next tick must
    # NOT post again.
    env.run()
    check("next tick does NOT re-post", len(env.posts) == 1)


@case("an unreadable entry is REPORTED, not silently invisible")
def _(env):
    with open(os.path.join(env.inbox, "bad.json"), "w") as f:
        f.write('{"broken":')
    r = env.run()
    check("exit 0", r.returncode == 0)
    check("posted nothing", env.posts == [])
    check("warns about the unreadable entry", "unreadable" in r.stderr.lower())
    check("names the file", "bad.json" in r.stderr)


@case("jq missing -> exit 2, never a false 'queue empty'")
def _(env):
    # Without an explicit check, every jq read degrades behind a `|| true`, a full queue
    # reads as unreadable, and the tick prints "queue empty" at exit 0 - a silent wrong
    # answer under an environment fault.
    env.queue(354)
    broken = os.path.join(env.bin, "jq")
    Env._write(broken, "#!/usr/bin/env bash\nexit 127\n")
    r = env.run()
    check("exit 2", r.returncode == 2)
    check("posted nothing", env.posts == [])
    check("does NOT claim the queue is empty", "queue empty" not in r.stdout)


@case("dry run posts nothing but shows the exact command")
def _(env):
    env.queue(354)
    r = env.run(ELMER_DRY_RUN=1)
    check("exit 0", r.returncode == 0)
    check("posted nothing", env.posts == [])
    check("shows the trigger", "@coderabbitai review" in r.stdout)
    check("entry left queued", env.ls("inbox") == ["e-354.json"])


@case("concurrent ticks against a STALE lock never hold it simultaneously")
def _(env):
    # THE ONLY VALID EVIDENCE IS OVERLAP, NOT A POST COUNT. A tick that starts after
    # the winner RELEASED the lock and posts is succession, which is correct
    # behaviour; counting posts cannot tell it apart from a race and an earlier pass
    # nearly filed a false accusation on exactly that confusion. Simultaneity is the
    # one thing succession can never produce, so the stub brackets each post with a
    # START/END timestamp and holds the critical section for a real interval; the
    # assertion is that no two of those intervals overlap.
    #
    # This case is what separates the identity-checked break from a merely ATOMIC
    # one. `mv` succeeds for one racer at a time but does not verify WHICH directory
    # it moved, so a tick that read the old lock's age can break a FRESH lock the
    # winner already installed. Reverting the inode check (or going back to
    # rmdir+mkdir) makes overlaps appear here.
    ilog = os.path.join(env.tmp, "intervals")
    open(ilog, "w").close()

    e = dict(os.environ)
    e["PATH"] = env.bin + os.pathsep + e["PATH"]
    e["ELMER_HOME"] = env.home
    e["GH_POSTLOG"] = env.postlog
    e["GH_INTERVAL_LOG"] = ilog
    e["GH_POST_SLEEP"] = "1.5"
    # The hourly cap is a DIFFERENT bound and must not mask the race: raised so a
    # racy lock is free to post as many times as it can.
    e["ELMER_MAX_PER_HR"] = "999"

    # THREE ROUNDS, not one. The race window is small and process start-up spread is
    # widest on the first burst (cold caches), so a single burst detects the racy form
    # only about 5 times in 6 - measured, not assumed. Rounds are strictly sequential
    # (every process is waited on before the next round starts), so no interval can
    # span a round boundary and pooling them into one overlap sweep is sound.
    spans = []
    for _round in range(3):
        lock = os.path.join(env.home, ".tick.lock")
        if os.path.isdir(lock):
            os.rmdir(lock)
        os.makedirs(lock)
        os.utime(lock, (1, 1))  # 1970: far past any threshold, so every tick sees stale
        for pr in range(400, 408):
            env.queue(pr)
        procs = [subprocess.Popen([env.script], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, env=e) for _ in range(8)]
        for p in procs:
            p.wait(timeout=60)

    with open(ilog) as f:
        spans = [tuple(float(x) for x in ln.split()) for ln in f.read().split("\n") if ln.strip()]
    spans.sort()
    # Sweep with a running max end rather than comparing adjacent pairs: a fully
    # CONTAINED interval overlaps its predecessor without adjoining it in sort order.
    overlaps = 0; max_end = None
    for start, end in spans:
        if max_end is not None and start < max_end:
            overlaps += 1
        max_end = end if max_end is None else max(max_end, end)
    check(f"at least one post happened ({len(spans)} intervals)", len(spans) >= 1)
    check(f"zero overlapping post intervals (found {overlaps})", overlaps == 0)


# --- The fix-scoped cases: the same silent-wrong-answer class, other dependencies ---

@case("a stdout write failure after a SUCCESSFUL post never reports rc=1")
def _(env):
    # THE CONTRACT SAYS 1 MEANS "POSTED NOTHING", and the loop runbook hands the entry
    # back to the TL on a 1. Two bare `echo`s sat below the `rm -f` under `set -e`, so a
    # stdout write error (EBADF - a timer loop redirecting into a closed or rotated fd)
    # exited 1 AFTER the post, the drain record and the inbox removal. Reproduced with
    # `elmer-tick.sh >&-`: rc=1, posted=1. EPIPE is a different animal (141); it is this
    # EBADF class that forges a refusal out of a spent review slot.
    env.queue(354)
    r = env.run_stdout_closed()
    check("posted once", len(env.posts) == 1)
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 0", r.returncode == 0)
    check("inbox drained", env.ls("inbox") == [])
    check("drain record written", env.ls("drained") == ["e-354.json"])


@case("a broken basename does NOT turn a full queue into 'queue empty'")
def _(env):
    # The drained/ check was `[ -e "$drained/$(basename "$f")" ]`. A basename that fails
    # or yields empty collapses that to `[ -e "$drained/" ]` - TRUE for the directory
    # itself - so EVERY entry is skipped and the tick reports an empty queue over a full
    # one. That is the jq-probe failure class exactly, reintroduced through a second,
    # unprobed dependency; `${f##*/}` removes the dependency instead of probing it.
    Env._write(os.path.join(env.bin, "basename"),
               "#!/usr/bin/env bash\nexit 127\n")
    env.queue(354)
    r = env.run()
    check("does NOT claim the queue is empty", "queue empty" not in r.stdout)
    check("posted once", len(env.posts) == 1)
    check("exit 0", r.returncode == 0)
    check("inbox drained", env.ls("inbox") == [])


@case("queue health is reported on the LOCK-CONTENDED path")
def _(env):
    # Lock contention is the DESIGNED outcome of a second loop window, so a report that
    # sits below it is dead under exactly the condition a human creates routinely.
    with open(os.path.join(env.inbox, "bad.json"), "w") as f:
        f.write('{"broken":')
    os.makedirs(os.path.join(env.home, ".tick.lock"))
    r = env.run()
    check("exit 0", r.returncode == 0)
    check("still silent on stdout", r.stdout.strip() == "")
    check("warns about the unreadable entry", "unreadable" in r.stderr.lower())
    check("names the file", "bad.json" in r.stderr)
    check("posted nothing", env.posts == [])


@case("queue health is reported on the CAP-REACHED path")
def _(env):
    # The cap fires under any steady load, so this is the other routinely-taken exit
    # the report used to sit below.
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(4):
        env.drain_record(f"old-{i}.json", now)
    with open(os.path.join(env.inbox, "bad.json"), "w") as f:
        f.write('{"broken":')
    r = env.run()
    check("exit 0", r.returncode == 0)
    check("says cap reached", "hourly cap reached" in r.stdout)
    check("warns about the unreadable entry", "unreadable" in r.stderr.lower())
    check("names the file", "bad.json" in r.stderr)
    check("posted nothing", env.posts == [])


@case("an inbox entry that is ALSO in drained/ is reported as stale, never posted")
def _(env):
    # The exit-2 rm-failure path strands a file in BOTH directories. The drained/ check
    # `continue`s BEFORE the readability read, so such a file can never reach the
    # unreadable report either: it was surfaced exactly once, by the tick that stranded
    # it, and was then invisible forever while every later tick asserted "queue empty".
    # Two individually-correct fixes jointly produced a silent wrong answer.
    env.queue(354)
    env.drain_record("e-354.json", "2020-01-01T00:00:00Z")
    r = env.run()
    check("exit 0", r.returncode == 0)
    check("posted nothing", env.posts == [])
    check("warns it is stale", "stale" in r.stderr.lower())
    check("names the file", "e-354.json" in r.stderr)
    check("tells the operator to remove it", "by hand" in r.stderr)
    check("entry left in place", env.ls("inbox") == ["e-354.json"])


@case("a coderabbitai LOOKALIKE login does not suppress a wanted review")
def _(env):
    # `test("^coderabbitai")` is unanchored at the end, so `coderabbitai-impostor` and
    # `coderabbitai2` also suppressed. The check can only ever SUPPRESS, so the cost is
    # a silently skipped review the maintainer wanted - cheap to fix, invisible to hit.
    env.queue(354)
    r = env.run(GH_REVIEWS=json.dumps([review(SHA, login="coderabbitai-impostor")]))
    check("posted once", len(env.posts) == 1)
    check("exit 0", r.returncode == 0)


@case("a coderabbitai2 login does not suppress either")
def _(env):
    env.queue(354)
    r = env.run(GH_REVIEWS=json.dumps([review(SHA, login="coderabbitai2")]))
    check("posted once", len(env.posts) == 1)
    check("exit 0", r.returncode == 0)


# --- The stderr side of the same class: a report that forges a refusal --------------

@case("a stderr write failure on the CONTENDED path never reports rc=1")
def _(env):
    # THE FIX THAT REINTRODUCED THE BUG IT FIXED. `report_queue_health` was moved above
    # the lock so the report would survive the two routinely-taken early exits - and its
    # four writes were left bare. Both callers are contractually 0, so with fd 2 closed
    # and an unhealthy queue, `set -e` turned the REPORT into rc=1: a refusal for an
    # entry nobody refused, on the exact path a human creates by opening a second
    # /elmer-loop window. The runbook then sends the TL to re-queue nothing.
    # An unreadable entry is REQUIRED here: without one the report writes nothing at
    # all and the closed fd is never touched, which is precisely why the suite could
    # mutation-prove five fixes and still miss this.
    with open(os.path.join(env.inbox, "bad.json"), "w") as f:
        f.write('{"broken":')
    os.makedirs(os.path.join(env.home, ".tick.lock"))
    r = env.run_stderr_closed()
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 0", r.returncode == 0)
    check("still silent on stdout", r.stdout.strip() == "")
    check("posted nothing", env.posts == [])


@case("a stderr write failure on the CAP-REACHED path never reports rc=1")
def _(env):
    # The cap fires under any steady load, so this is the other routinely-taken exit.
    # It also asserts the STDOUT line still prints: a guard that swallowed the whole
    # function's output would pass the rc check while silently losing the report.
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(4):
        env.drain_record(f"old-{i}.json", now)
    with open(os.path.join(env.inbox, "bad.json"), "w") as f:
        f.write('{"broken":')
    r = env.run_stderr_closed()
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 0", r.returncode == 0)
    check("stdout line still printed", "hourly cap reached" in r.stdout)
    check("posted nothing", env.posts == [])


@case("a stderr write failure with a STALE entry queued never reports rc=1")
def _(env):
    # The stale branch of the report is a SEPARATE `if` with three writes of its own,
    # so a guard applied to only the unreadable branch would pass the two cases above
    # and still fail here. An entry in BOTH inbox/ and drained/ is the state the
    # exit-2 rm-failure path leaves behind, i.e. reachable in production.
    env.queue(354)
    env.drain_record("e-354.json", "2020-01-01T00:00:00Z")
    r = env.run_stderr_closed()
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 0", r.returncode == 0)
    check("posted nothing", env.posts == [])


@case("report_queue_health still PRINTS when stderr works (the guard is not a mute)")
def _(env):
    # THE OVER-HARDENING CHECK, and the reason it is a case rather than a manual pass:
    # `{ ... } >&2 || true` is one edit away from `{ ... } >/dev/null`, and both make
    # every rc assertion above pass. Converting a real report into a silent success
    # would be a worse defect than the rc=1 being fixed here, so the suite asserts the
    # output SURVIVES the guard on both branches.
    with open(os.path.join(env.inbox, "bad.json"), "w") as f:
        f.write('{"broken":')
    env.queue(355)
    env.drain_record("e-355.json", "2020-01-01T00:00:00Z")
    r = env.run()
    check("exit 0", r.returncode == 0)
    check("unreadable branch still warns", "unreadable" in r.stderr.lower())
    check("names the unreadable file", "bad.json" in r.stderr)
    check("stale branch still warns", "stale" in r.stderr.lower())
    check("names the stale file", "e-355.json" in r.stderr)
    check("still tells the operator to remove it", "by hand" in r.stderr)


# --- mktemp: a SETUP error, never a silent 1 ----------------------------------------

@case("mktemp failure -> exit 2 with a message, never a silent 1")
def _(env):
    # The two mktemps moved ABOVE the lock, so they now gate EVERY tick including pure
    # no-ops, unguarded under `set -e`. An unwritable or full TMPDIR exited 1 with no
    # output whatsoever - the runbook reads that as "an entry was refused" and sends the
    # TL to re-queue an entry nothing ever touched. An unavailable mktemp is a broken
    # dependency, the same shape as the jq probe, so it is exit 2 and it says so.
    Env._write(os.path.join(env.bin, "mktemp"), "#!/usr/bin/env bash\nexit 1\n")
    env.queue(354)
    r = env.run()
    check(f"exit 2, not 1 (got {r.returncode})", r.returncode == 2)
    check("says setup error", "setup error" in r.stderr.lower())
    check("names mktemp", "mktemp" in r.stderr)
    check("posted nothing", env.posts == [])
    check("entry left queued", env.ls("inbox") == ["e-354.json"])


@case("a mktemp that fails on the SECOND call strands no temp file")
def _(env):
    # The trap was armed only after BOTH calls, so a second failure leaked the first
    # file into TMPDIR on every tick - unbounded, since this now runs on no-ops too.
    # Arming the trap after the FIRST mktemp is what closes it; the stub hands out
    # files from a directory the case can then inspect.
    leak = os.path.join(env.tmp, "leak"); os.makedirs(leak)
    Env._write(os.path.join(env.bin, "mktemp"), f"""#!/usr/bin/env bash
c="{env.tmp}/mkcount"
n=$(cat "$c" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$c"
[ "$n" -ge 2 ] && exit 1
f="{leak}/t.$n"; : > "$f"; printf '%s\\n' "$f"
""")
    env.queue(354)
    r = env.run()
    check(f"exit 2, not 1 (got {r.returncode})", r.returncode == 2)
    check(f"nothing stranded in TMPDIR (found {os.listdir(leak)})", os.listdir(leak) == [])
    check("posted nothing", env.posts == [])


# --- Saying the true thing about a queue that is not empty --------------------------

@case("an ALL-STALE queue does not claim 'queue empty' on stdout")
def _(env):
    # Every inbox entry also in drained/ selects nothing, and the stdout line used to
    # assert "queue empty" over a directory with files in it - literally false, and read
    # by an operator as "the queue drained normally", so the stranded files are never
    # looked at. The stderr warning already named them; stdout now agrees with it.
    env.queue(354)
    env.drain_record("e-354.json", "2020-01-01T00:00:00Z")
    r = env.run()
    check("exit 0", r.returncode == 0)
    check("does NOT claim the queue is empty", "queue empty" not in r.stdout)
    check("says there is nothing postable", "no postable entries" in r.stdout)
    check("stale warning still on stderr", "stale" in r.stderr.lower())
    check("posted nothing", env.posts == [])
    check("entry left in place", env.ls("inbox") == ["e-354.json"])


@case("a GENUINELY empty queue still says 'queue empty'")
def _(env):
    # The other half of the wording fix: the new branch must not swallow the true case.
    r = env.run()
    check("exit 0", r.returncode == 0)
    check("still says queue empty", "queue empty" in r.stdout)
    check("posted nothing", env.posts == [])


# --- THE WHOLE-FILE SWEEP: no write anywhere may change the exit status -------------
#
# The three rounds before this one each guarded ONE write site and each left another
# unguarded, because the file was being fixed instance-by-instance. The cases below
# assert the CLASS instead: every contractually-0 no-op keeps its 0 with stdout
# closed, and every deliberate exit 2 keeps its 2 with stderr closed. A future write
# added without the `{ ... } || true` template fails here regardless of which path it
# sits on.
#
# WHY EACH FD PAIRS WITH ITS OWN SET: the no-op lines are STDOUT writes (the tick's
# normal report), so only a closed fd 1 can break them; the setup-error messages are
# STDERR writes, so only a closed fd 2 can. Testing a path against the wrong fd
# exercises a write that cannot fail and proves nothing - which is exactly how the
# earlier suite passed while `report_queue_health` sat unguarded.

@case("stdout closed: an EMPTY QUEUE still exits 0, never a phantom refusal")
def _(env):
    # `echo "elmer-tick: queue empty..."` was bare on a contractually-0 path, so a
    # timer loop with a rotated log turned the most common outcome in the whole system
    # into rc=1 - "an entry was refused" for a queue with nothing in it at all.
    r = env.run_stdout_closed()
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 0", r.returncode == 0)
    check("posted nothing", env.posts == [])


@case("stdout closed: the CAP-REACHED no-op still exits 0")
def _(env):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(4):
        env.drain_record(f"old-{i}.json", now)
    env.queue(354)
    r = env.run_stdout_closed()
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 0", r.returncode == 0)
    check("posted nothing", env.posts == [])
    check("entry left queued", env.ls("inbox") == ["e-354.json"])


@case("stdout closed: the THROTTLED no-op still exits 0")
def _(env):
    env.queue(354)
    r = env.run_stdout_closed(GH_QUOTA=1)
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 0", r.returncode == 0)
    check("posted nothing", env.posts == [])
    check("entry left queued", env.ls("inbox") == ["e-354.json"])


@case("stdout closed: the DRY RUN still exits 0")
def _(env):
    env.queue(354)
    r = env.run_stdout_closed(ELMER_DRY_RUN=1)
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 0", r.returncode == 0)
    check("posted nothing", env.posts == [])
    check("entry left queued", env.ls("inbox") == ["e-354.json"])


@case("stdout closed: the ALL-STALE no-op still exits 0")
def _(env):
    # The other arm of the same `if`: a guard applied to only one branch would pass
    # the empty-queue case above and still fail here.
    env.queue(354)
    env.drain_record("e-354.json", "2020-01-01T00:00:00Z")
    r = env.run_stdout_closed()
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 0", r.returncode == 0)
    check("posted nothing", env.posts == [])


@case("stdout closed: `-h` still exits 0 (help is not a setup error)")
def _(env):
    # The awk that prints the header block exits 2 on a write error, so an unguarded
    # `-h` with stdout closed reported a SETUP ERROR for a request it serviced fine.
    r = env.run_args_stdout_closed("-h")
    check(f"rc is 0 (got {r.returncode})", r.returncode == 0)


@case("stderr closed: a gh READ FAILURE still exits 2, never a downgrade to 1")
def _(env):
    # THE WORST DIRECTION IN THE WHOLE CONTRACT. 2 means "setup error, the loop is
    # broken"; 1 means "an entry was refused, hand it back to the TL". With fd 2
    # closed, `set -e` on the bare `echo` ahead of the `exit 2` made every genuine
    # setup error indistinguishable from a refusal, so the operator re-queues an entry
    # while the actual fault - an unreadable GitHub - goes unreported.
    env.queue(354)
    r = env.run_stderr_closed(GH_FAIL=1)
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 2", r.returncode == 2)
    check("posted nothing", env.posts == [])
    check("entry left queued", env.ls("inbox") == ["e-354.json"])


@case("stderr closed: a QUOTA read failure still exits 2")
def _(env):
    env.queue(354)
    r = env.run_stderr_closed(GH_QUOTA=2)
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 2", r.returncode == 2)
    check("posted nothing", env.posts == [])


@case("stderr closed: a POST failure still exits 2")
def _(env):
    env.queue(354)
    r = env.run_stderr_closed(GH_POST_FAIL=1)
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 2", r.returncode == 2)
    check("nothing drained", env.ls("drained") == [])
    check("entry left queued", env.ls("inbox") == ["e-354.json"])


@case("stderr closed: a broken jq still exits 2 (the earliest exit-2 site)")
def _(env):
    # The jq probe fires before ANYTHING else, so it is the one exit-2 site no other
    # case can reach; a sweep that missed it would be invisible to every test above.
    env.queue(354)
    Env._write(os.path.join(env.bin, "jq"), "#!/usr/bin/env bash\nexit 127\n")
    r = env.run_stderr_closed()
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 2", r.returncode == 2)
    check("posted nothing", env.posts == [])


@case("stderr closed: a failing mktemp still exits 2")
def _(env):
    Env._write(os.path.join(env.bin, "mktemp"), "#!/usr/bin/env bash\nexit 1\n")
    env.queue(354)
    r = env.run_stderr_closed()
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 2", r.returncode == 2)
    check("posted nothing", env.posts == [])


@case("stderr closed: a bad ARGUMENT still exits 2")
def _(env):
    r = env.run_args_stderr_closed("354")
    check(f"rc is NOT 1 (got {r.returncode})", r.returncode != 1)
    check("rc is 2", r.returncode == 2)


@case("stderr closed: the REFUSALS keep their 1 (guarding must not move them either)")
def _(env):
    # The sweep guards refusal sites too, and a guard that changed a 1 into something
    # else would be as much a defect as the 2-to-1 downgrade. Asserted on all three
    # stderr-reporting refusal paths.
    env.queue(354)
    r = env.run_stderr_closed(GH_HEAD=OTHER_SHA)
    check(f"stale head still rc=1 (got {r.returncode})", r.returncode == 1)
    check("posted nothing", env.posts == [])


@case("stderr closed: a CLOSED PR and a FULL form keep their 1")
def _(env):
    env.queue(354)
    r = env.run_stderr_closed(GH_STATE="MERGED")
    check(f"closed PR still rc=1 (got {r.returncode})", r.returncode == 1)
    check("posted nothing", env.posts == [])


@case("stderr closed: form=full keeps its 1")
def _(env):
    env.queue(354, form="full")
    r = env.run_stderr_closed()
    check(f"full form still rc=1 (got {r.returncode})", r.returncode == 1)
    check("posted nothing", env.posts == [])


@case("stderr closed: an already-reviewed head keeps its 1")
def _(env):
    env.queue(354)
    r = env.run_stderr_closed(GH_REVIEWS=json.dumps([review(SHA)]))
    check(f"already reviewed still rc=1 (got {r.returncode})", r.returncode == 1)
    check("posted nothing", env.posts == [])


# --- OVER-HARDENING: the guard must not be a mute -----------------------------------
#
# `{ ... } >&2 || true` is one careless edit from `{ ... } >/dev/null`, and BOTH make
# every rc assertion above pass while the second silently discards the report. Losing
# the output would be a worse defect than the rc=1 being fixed, so every guarded group
# gets a matching case asserting the message STILL LANDS on a working fd. These are
# the counterweight to the closed-fd cases and must be read as a pair with them.

@case("over-hardening: the no-op stdout lines are still PRINTED on a working fd")
def _(env):
    r = env.run()
    check("empty-queue line printed", "queue empty" in r.stdout)
    check("exit 0", r.returncode == 0)


@case("over-hardening: the CAP line is still printed")
def _(env):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(4):
        env.drain_record(f"old-{i}.json", now)
    env.queue(354)
    r = env.run()
    check("cap line printed", "hourly cap reached" in r.stdout)
    check("names the counts", "4/4" in r.stdout)
    check("exit 0", r.returncode == 0)


@case("over-hardening: the THROTTLED line is still printed")
def _(env):
    env.queue(354)
    r = env.run(GH_QUOTA=1)
    check("throttle line printed", "throttled" in r.stdout)
    check("exit 0", r.returncode == 0)


@case("over-hardening: the DRY RUN still shows the exact command")
def _(env):
    env.queue(354)
    r = env.run(ELMER_DRY_RUN=1)
    check("shows the target", "#354" in r.stdout)
    check("shows the trigger", "@coderabbitai review" in r.stdout)
    check("exit 0", r.returncode == 0)


@case("over-hardening: `-h` still prints the usage block")
def _(env):
    r = subprocess.run([env.script, "-h"], capture_output=True, text=True,
                       env=env._env({}), timeout=120)
    check("exit 0", r.returncode == 0)
    check("printed the header block", "Exit codes:" in r.stdout)


@case("over-hardening: the SETUP-ERROR messages still reach a working stderr")
def _(env):
    env.queue(354)
    r = env.run(GH_FAIL=1)
    check("exit 2", r.returncode == 2)
    check("gh read failure names the PR", "#354" in r.stderr)
    check("says setup error", "setup error" in r.stderr.lower())


@case("over-hardening: the quota and post failures still report")
def _(env):
    env.queue(354)
    r = env.run(GH_QUOTA=2)
    check("exit 2", r.returncode == 2)
    check("quota failure reports", "quota read failed" in r.stderr)


@case("over-hardening: a failed post still surfaces gh's own stderr")
def _(env):
    # The `printf '%s\n' "$post_out"` inside the guarded group is what carries gh's
    # message; a group redirected to /dev/null would lose the only clue why it failed.
    env.queue(354)
    r = env.run(GH_POST_FAIL=1)
    check("exit 2", r.returncode == 2)
    check("says the post failed", "the post failed" in r.stderr)
    check("relays gh's own message", "post rejected" in r.stderr)


@case("over-hardening: the bad-argument usage line still prints")
def _(env):
    r = subprocess.run([env.script, "354"], capture_output=True, text=True,
                       env=env._env({}), timeout=120)
    check("exit 2", r.returncode == 2)
    check("prints usage", "usage: elmer-tick.sh" in r.stderr)


@case("over-hardening: the REFUSAL messages still print in full")
def _(env):
    # The stale-head refusal is a THREE-line group; a mute would drop the two detail
    # lines the operator needs to act, while the rc assertion passed either way.
    env.queue(354)
    r = env.run(GH_HEAD=OTHER_SHA)
    check("exit 1", r.returncode == 1)
    check("line 1: says refused", "REFUSED" in r.stderr)
    check("line 2: names both SHAs", SHA[:12] in r.stderr and OTHER_SHA[:12] in r.stderr)
    check("line 3: tells them to re-run prep-pr", "/prep-pr" in r.stderr)


@case("over-hardening: the form=full refusal still names the entry file")
def _(env):
    env.queue(354, form="full")
    r = env.run()
    check("exit 1", r.returncode == 1)
    check("names the form", "'full'" in r.stderr)
    check("names the entry path", "e-354.json" in r.stderr)


@case("over-hardening: the stale-lock note still prints")
def _(env):
    lock = os.path.join(env.home, ".tick.lock")
    os.makedirs(lock)
    os.utime(lock, (1, 1))
    env.queue(354)
    r = env.run()
    check("note printed", "breaking a stale tick lock" in r.stderr)
    check("posted once", len(env.posts) == 1)


@case("over-hardening: the POST-SUCCESS lines still print")
def _(env):
    env.queue(354)
    r = env.run()
    check("exit 0", r.returncode == 0)
    check("says POSTED", "POSTED an incremental review request" in r.stdout)
    check("names the drain record", "drained: " in r.stdout)


print()
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all elmer-tick assertions passed")
