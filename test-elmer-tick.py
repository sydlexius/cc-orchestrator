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
  printf '%s\n' "$*" >> "$GH_POSTLOG"
  echo "https://github.com/x/y/pull/1#issuecomment-1"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  case "$*" in
    *reviews*) printf '{"reviews":%s}\n' "${GH_REVIEWS:-[]}" ; exit 0 ;;
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

    def run(self, **env):
        e = dict(os.environ)
        e["PATH"] = self.bin + os.pathsep + e["PATH"]
        e["ELMER_HOME"] = self.home
        e["GH_POSTLOG"] = self.postlog
        e.update({k: str(v) for k, v in env.items()})
        return subprocess.run([self.script], capture_output=True, text=True, env=e)

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


@case("a review already exists at this head -> refuse")
def _(env):
    env.queue(354)
    r = env.run(GH_REVIEWS=json.dumps([{"commit_id": SHA}]))
    check("exit 1", r.returncode == 1)
    check("posted nothing", env.posts == [])


@case("a review at a DIFFERENT head does not block")
def _(env):
    env.queue(354)
    r = env.run(GH_REVIEWS=json.dumps([{"commit_id": OTHER_SHA}]))
    check("posted once", len(env.posts) == 1)
    check("exit 0", r.returncode == 0)


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


@case("dry run posts nothing but shows the exact command")
def _(env):
    env.queue(354)
    r = env.run(ELMER_DRY_RUN=1)
    check("exit 0", r.returncode == 0)
    check("posted nothing", env.posts == [])
    check("shows the trigger", "@coderabbitai review" in r.stdout)
    check("entry left queued", env.ls("inbox") == ["e-354.json"])


print()
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all elmer-tick assertions passed")
