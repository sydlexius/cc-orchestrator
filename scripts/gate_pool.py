#!/usr/bin/env python3
"""The machine-wide gate pool (epic #538, unit A / #539).

Design of record: skills/orchestrate/design/DESIGN-gate-pool.md ("the doc"). The pool lets
several worktrees gate at once without oversubscribing the machine: every gate, pre-push hook
and named heavy command takes `cost` units of one machine budget, and what does not fit waits.

TWO LAYERS. The POLICY is `cls()` and `schedule()`, the doc's section 2 pseudocode as a pure
function. The POOL ON DISK (`Pool`, `Waiter`, `Holder`) is sections 1 and 4: one kernel flock
per budget unit under slots/, one flocked ticket per waiting or running process under
waiters/, and ONE short lock, admit.lock, around every scheduling pass and every change to
waiters/. No lock here can go stale: the kernel drops each one when its process dies.

THE HOME IS ALWAYS PASSED IN. This module resolves no home directory and reads no config
file; its caller hands it the pool root and an already validated config. Nothing imports it
yet outside its harness: the gate-runner wiring is a later PR of #539.

THREE RULES EVERY LINE BELOW KEEPS (doc section 4):
  1. no lock descriptor is ever inherited: each comes from `_open_lock` (non-inheritable),
     and the only child this module starts is `git`, by exec with every descriptor closed;
  2. a lock is dropped by CLOSING its descriptor, never by LOCK_UN;
  3. a file some process holds a flock on is never replaced or renamed over. A ticket is
     rewritten in place by its owner; what another process must record about it (its bypass
     count) goes to a `.sched` sidecar nobody flocks.

Importable (underscore module name) because test-gate-pool.py calls these directly and
mutation-proves every rule against a copy. Stdlib only.
"""

import contextlib
import fcntl
import json
import os
import random
import stat
import subprocess
import time

# BUMP when the ordering rules or the meaning of anything on disk change (doc section 4): a
# process schedules, rewrites and unlinks only records of ITS OWN protocol.
POOL_PROTOCOL = 1


class Entry:
    """One waiting entry: a ticket in unit A, also a queued job in unit B.

    `seq` orders entries within a class. `kind` is "gate" (hand-run or hook), "named" (a named
    heavy command) or "job" (a queued job, unit B). `cost` is the budget the entry asked for and
    `weight` the weight it declared; a ticket carries no separate weight, so it defaults to the
    cost (doc section 1: "for a ticket, cls() reads cost as e.weight"). `worktree` is the
    recorded worktree key, `bypass` the stored count of times the entry has been passed, and
    `open_pr` whether a job's branch has an open PR. `path` is the record the entry came from.

    Compared and hashed by IDENTITY (no __eq__): schedule() keys its per-pass counts on the
    entry objects it was handed.
    """
    __slots__ = ("seq", "kind", "weight", "cost", "worktree", "bypass", "open_pr", "path")

    def __init__(self, seq, kind, cost, worktree, *, weight=None, bypass=0, open_pr=False,
                 path=None):
        self.seq, self.kind, self.cost, self.worktree = seq, kind, cost, worktree
        self.weight = cost if weight is None else weight
        self.bypass, self.open_pr, self.path = bypass, open_pr, path


def cls(e, cap):
    """The class of an entry: 0 a small named check, 1 a fix round, 2 everything else.
    DERIVED, never requested: nothing a caller passes can select class 0, only a declared
    weight at or below `cap` (the machine's small_check_cap)."""
    if e.kind == "named":
        return 0 if e.weight <= cap else 2
    if e.kind == "job":
        return 1 if e.open_pr else 2
    return 2                               # hand-run gate, pre-push hook


def schedule(entries, free, budget, busy, *, k, cap):
    """entries: every waiting job and ticket. free: unheld budget units. busy: the worktree
    keys of everything running. k: backfill_bypass_limit. cap: small_check_cap.
    Returns [(entry, passed)] to start now, in order; `passed` lists the entries that start
    leaves waiting, whose bypass count the caller's commit raises by one.

    Caller guarantees a unique `seq` per entry, `cost >= 0`, `budget >= 1` and
    `0 <= free <= budget`.

    Pure: reads nothing but its arguments and changes none of them. A one-to-one transcription
    of the doc's section 2 pseudocode, with K and CAP as keyword arguments. Called only under
    admit.lock."""
    order = sorted(entries, key=lambda e: (cls(e, cap), e.seq))
    claimed = set(busy)
    pend = {}                              # passes this pass's own starts will record, by entry

    def prot(x):                           # PROTECTED: passed K times, counting this pass's starts
        return x.bypass + pend.get(x, 0) >= k

    def guard():                           # a protected gate (class 1 or 2) waiting on budget, not on its worktree
        return any(cls(w, cap) > 0 and prot(w) and w.worktree not in claimed for w in order)

    start, blocked = [], []                # blocked = entries ahead in order that could not start
    for e in order:
        if e.worktree in claimed:          # PER-WORKTREE EXCLUSION: one thing per worktree.
            continue                       # Not eligible: neither started nor counted as blocked.
        c = min(e.cost, budget)            # a cost above the whole budget RUNS ALONE
        if c == 0:                         # no budget taken, never a pass
            start.append((e, []))
            claimed.add(e.worktree)
            continue
        if cls(e, cap) == 0 and guard():
            continue                       # HOLD small checks while a protected gate waits
        if any(prot(b) for b in blocked):
            continue                       # HOLD: a protected entry is ahead; let the budget drain
        if c <= free:
            passed = list(blocked)         # backfill past earlier entries
            if cls(e, cap) == 0:           # priority past waiting gates
                passed += [w for w in order
                           if cls(w, cap) > 0 and w.worktree not in claimed
                           and w.worktree != e.worktree   # e is about to claim it: w waits on the worktree
                           and min(w.cost, budget) > free - c]
            for b in passed:               # so one pass cannot pass an entry beyond K
                pend[b] = pend.get(b, 0) + 1
            start.append((e, passed))
            free -= c
            claimed.add(e.worktree)
        else:
            blocked.append(e)
    return start


# --- the pool on disk (doc sections 1 and 4) -------------------------------------------------
EX_TEMPFAIL = 75                           # NOT RUN: none of 0 (pass), 1 (failed), 2 (config)
# The optional machine keys and the values the doc gives them when absent (section 3).
DEFAULTS = {"backfill_bypass_limit": 2, "small_check_cap": 2, "wait_timeout_s": 3600}
POLL_S = 1.0                               # one pass per second, jittered 0.8x to 1.2x
WAIT_NOTE_S = 30.0                         # a wait line at the first failed poll, then this often


class NotRun(Exception):
    """The pool's own refusal: nothing ran. `code` is the exit status the caller owes (75 for
    a wait that gave up, 2 for config), `reason` a slug, `message` the whole stderr line."""

    def __init__(self, code, reason, message):
        super().__init__(message)
        self.code, self.reason, self.message = code, reason, message


def _open_lock(path, flags=os.O_RDWR | os.O_CREAT):
    """EVERY lock descriptor is opened here. os.open returns a NON-inheritable descriptor, so
    no child started by exec can keep a slot or a ticket held after its parent dies (rule 1)."""
    return os.open(path, flags, 0o600)


def _try_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _probe(path):
    """Try-lock an EXISTING file and read it -> (held, text); held is None when it is missing.
    The trial lock is dropped by closing (rule 2). Called only under admit.lock, where owners
    create and lock their files, so a probe can never beat a new owner to its own file."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None, ""
    try:
        held = not _try_lock(fd)
        return held, os.read(fd, 65536).decode("utf-8", "replace")
    finally:
        os.close(fd)


def _rm(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _load(text):
    try:
        rec = json.loads(text)
    except ValueError:
        return {}
    return rec if isinstance(rec, dict) else {}


def _ours(rec):
    """True for a ticket this code may schedule, rewrite or unlink: its own protocol and every
    field it acts on well-typed. Anything else is FOREIGN, an unreadable ticket included: doubt
    about a record never reads as "dead, unlink it"."""
    return (rec.get("pool_protocol") == POOL_PROTOCOL and rec.get("state") in ("waiting", "running")
            and isinstance(rec.get("kind"), str) and isinstance(rec.get("worktree"), str)
            and type(rec.get("cost")) is int and rec["cost"] > 0)


def _seq_of(name):
    head = name.split("-", 1)[0]
    return int(head) if head.isascii() and head.isdigit() else None


def _sched_of(ticket):
    return ticket[:-len(".ticket")] + ".sched"


def _read_sched(path):
    """The bypass count in a `.sched` sidecar, or None when it is missing, unreadable or of
    another protocol."""
    try:
        with open(path, encoding="utf-8") as f:
            rec = _load(f.read())
    except OSError:
        return None
    n = rec.get("bypass")
    return n if rec.get("pool_protocol") == POOL_PROTOCOL and type(n) is int and n >= 0 else None


def worktree_key(root):
    """The per-worktree exclusion key for `root` (doc section 2): the RECORDED path string of
    the `git worktree list --porcelain` record that `root` resolves to. Resolution only PICKS
    the record; the string git recorded is what is stored and compared. A directory that is no
    registered worktree is keyed by its own resolved path, so two different directories never
    exclude each other. Runs git, so it is called BEFORE admit.lock is taken, never under it."""
    real = os.path.realpath(root)
    try:
        out = subprocess.run(["git", "-C", root, "worktree", "list", "--porcelain"],
                             capture_output=True, text=True, check=False, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        if line.startswith("worktree ") and os.path.realpath(line[9:]) == real:
            return line[9:]
    return real


class Pool:
    """One process's view of the pool rooted at `home`. `cfg` is the validated machine config:
    `protocol`, `budget`, and optionally the keys of DEFAULTS. The values are read ONCE, here:
    a process that is waiting or running keeps the figures it started with (doc section 3).

    A config of ANOTHER pool protocol is refused before anything in the directory is touched:
    a protocol bump needs the user's own edit of the config, so a branch cannot move the
    machine to its protocol by being run (doc section 4)."""

    def __init__(self, home, cfg):
        if cfg.get("protocol") != POOL_PROTOCOL:
            raise NotRun(2, "pool-protocol",
                         f"gate-runner: NOT RUN - pool protocol {POOL_PROTOCOL} != configured "
                         f"{cfg.get('protocol')}; update the plugin and run configure --apply")
        self.home, self.budget = home, cfg["budget"]
        self.k, self.cap, self.wait_timeout_s = (cfg.get(key, DEFAULTS[key]) for key in DEFAULTS)
        self.waiters, self.slots, self.tmp = (os.path.join(home, d)
                                              for d in ("waiters", "slots", "tmp"))
        os.makedirs(home, mode=0o700, exist_ok=True)
        st = os.stat(home)
        if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise NotRun(2, "pool-root",
                         f"gate-runner: NOT RUN - pool root {home} must be owned by you with mode 0700")
        for d in (self.waiters, self.slots, self.tmp):
            os.makedirs(d, mode=0o700, exist_ok=True)

    @contextlib.contextmanager
    def _admit(self):
        """admit.lock: held for one scheduling pass or one change to waiters/, never across
        git, TOML parsing, a sleep or a gate. Blocking, and the only lock anything blocks on."""
        fd = _open_lock(os.path.join(self.home, "admit.lock"))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _put(self, path, text):
        """Atomic write of a file NOBODY flocks (seq, a `.sched` sidecar): staged in tmp/,
        fsynced, renamed over. Never used on a ticket (rule 3). Under admit.lock only."""
        tmp = os.path.join(self.tmp, os.path.basename(path))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def enter(self, kind, name, cost, worktree):
        """Take a ticket: allocate the next seq, write the ticket and flock it, all in ONE
        admit.lock section, so no evaluator can ever see it unlocked. `cost` and `worktree`
        were computed by the caller BEFORE this call (doc section 2, "Cost before running")."""
        if kind not in ("gate", "named") or type(cost) is not int or cost < 1 \
                or not isinstance(worktree, str) or not worktree:
            raise ValueError(f"bad ticket: kind={kind!r} cost={cost!r} worktree={worktree!r}")
        seq_file = os.path.join(self.home, "seq")
        with self._admit():
            try:
                with open(seq_file, encoding="utf-8") as f:
                    last = int(f.read().strip())
            except (OSError, ValueError):
                last = 0                   # lost or corrupt: rebuilt from what is visible
            seq = 1 + max([0, last] + [n for n in map(_seq_of, os.listdir(self.waiters))
                                       if n is not None])
            self._put(seq_file, f"{seq}\n")
            base = (f"{seq:010d}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
                    f"-{os.urandom(3).hex()}.ticket")
            staged = os.path.join(self.tmp, base)
            fd = _open_lock(staged, os.O_RDWR | os.O_CREAT | os.O_EXCL)
            try:
                w = Waiter(self, os.path.join(self.waiters, base), fd, seq, kind, name, cost,
                           worktree)
                w._write("waiting")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.rename(staged, w.path)  # a NEW name: locked before it is visible
            except BaseException:
                os.close(fd)
                raise
        return w

    def acquire(self, kind, name, cost, worktree, *, say, clock=time.monotonic,
                sleep=time.sleep):
        """Take a ticket and poll until it is granted -> Holder. Raises NotRun(75) when no
        slot or no worktree came within wait_timeout_s: the caller did NOT run, which is
        neither a pass nor a failed gate. On any way out but a grant (the timeout, an
        interrupt) the ticket is given up, so nothing is left waiting. `say` gets one wait line
        at the first failed poll and one every WAIT_NOTE_S; a grant on the first poll says
        nothing. The sleep only paces the polls: every decision is made under admit.lock."""
        w = self.enter(kind, name, cost, worktree)
        deadline, noted = clock() + self.wait_timeout_s, None
        try:
            while not w.poll():
                now = clock()
                if now >= deadline:
                    raise NotRun(EX_TEMPFAIL, "wait-timeout", "gate-runner: NOT RUN - no gate "
                                 f"slot within {self.wait_timeout_s}s")
                if noted is None or now - noted >= WAIT_NOTE_S:
                    say(w.status())
                    noted = now
                sleep(POLL_S * random.uniform(0.8, 1.2))
        except BaseException:
            w.leave()
            raise
        return w.holder

    def _free_slots(self):
        """Try-lock every slot below THIS process's budget; the descriptors that locked are
        the free units. Creates a missing slot file, which is how a raised budget appears. The
        caller keeps what it commits and closes the rest before it leaves admit.lock."""
        got = []
        for i in range(self.budget):
            fd = _open_lock(os.path.join(self.slots, f"{i:03d}.lock"))
            if _try_lock(fd):
                got.append(fd)
            else:
                os.close(fd)
        return got

    def _scan(self, seq):
        """Read waiters/ under admit.lock, for the ticket numbered `seq` -> (entries, busy,
        blocked). entries: the live waiting tickets of this protocol. busy: {worktree key: who
        holds it}. blocked: a LIVE foreign ticket with a lower seq exists.

        A ticket of this protocol whose lock is FREE belongs to a dead process and is unlinked.
        A FOREIGN ticket (another protocol, or unreadable) is never scheduled, rewritten or
        unlinked, not even with its lock free: clearing a dead one is doctor's WARN and the
        user's delete. While its lock is HELD it is a live process, so its worktree counts as
        busy (its slots are held anyway: free is counted by try-lock) and a waiter with a
        HIGHER seq does not start. Wait-for edges then only point at lower numbers, so two
        protocols cannot deadlock on each other (doc section 4)."""
        for name in os.listdir(self.tmp):  # staging never outlives one admit.lock section
            _rm(os.path.join(self.tmp, name))
        entries, busy, blocked = [], {}, False
        for name in sorted(os.listdir(self.waiters)):
            if not name.endswith(".ticket"):
                continue
            path = os.path.join(self.waiters, name)
            held, text = _probe(path)
            rec, n = _load(text), _seq_of(name)
            ours = _ours(rec) and n is not None
            if not held:
                if ours:
                    _rm(path)
            elif not ours:
                if isinstance(rec.get("worktree"), str):
                    busy.setdefault(rec["worktree"], "another pool protocol")
                blocked = blocked or n is None or n < seq
            elif rec["state"] == "waiting":
                entries.append(Entry(n, rec["kind"], rec["cost"], rec["worktree"],
                                     bypass=_read_sched(_sched_of(path)) or 0, path=path))
            else:
                busy[rec["worktree"]] = f"{rec['kind']} pid {rec.get('pid')}"
        for name in os.listdir(self.waiters):   # a sidecar whose ticket is gone
            path = os.path.join(self.waiters, name)
            if name.endswith(".sched") and not os.path.exists(path[:-6] + ".ticket") \
                    and _read_sched(path) is not None:
                _rm(path)
        return entries, busy, blocked


class Waiter:
    """A ticket and its owner's side of the wait. `poll()` is ONE scheduling pass; everything
    that waits is a loop around it. `holder` is set once the pass granted this ticket."""

    def __init__(self, pool, path, fd, seq, kind, name, cost, worktree):
        self.pool, self.path, self.fd, self.seq = pool, path, fd, seq
        self.kind, self.name, self.cost, self.worktree = kind, name, cost, worktree
        self.holder, self._why = None, ""

    def _write(self, state):
        """Write the ticket IN PLACE through its own (locked) descriptor: same inode, lock
        still held (rule 3). One positioned write, then the length: never an empty ticket."""
        data = json.dumps({"pid": os.getpid(), "pool_protocol": POOL_PROTOCOL, "kind": self.kind,
                           "name": self.name, "cost": self.cost, "worktree": self.worktree,
                           "state": state}).encode("utf-8") + b"\n"
        os.pwrite(self.fd, data, 0)
        os.ftruncate(self.fd, len(data))
        os.fsync(self.fd)

    def poll(self):
        """One pass under admit.lock: clear the dead, count the free slots, ask schedule(), and
        if it starts THIS ticket, commit in the same critical section (keep the slots, mark the
        ticket running, record a pass against each entry left waiting). True once granted."""
        if self.holder is not None:
            return True
        p, c = self.pool, min(self.cost, self.pool.budget)
        with p._admit():
            entries, busy, blocked = p._scan(self.seq)
            got, keep = p._free_slots(), 0
            try:
                me = next((e for e in entries if e.path == self.path), None)
                start = schedule(entries, len(got), p.budget, set(busy), k=p.k, cap=p.cap)
                passed = next((ps for e, ps in start if e is me), None)
                if passed is None or blocked:
                    ahead = 0 if me is None else sum(
                        (cls(e, p.cap), e.seq) < (cls(me, p.cap), me.seq) for e in entries)
                    self._why = (
                        f"gate-runner: waiting for this worktree (held by {busy[self.worktree]})"
                        if self.worktree in busy else
                        "gate-runner: waiting behind a ticket of another pool protocol"
                        if blocked and passed is not None else
                        f"gate-runner: waiting for {c} of {p.budget} gate slots "
                        f"({len(got)} free, {ahead} ahead)")
                    return False
                self._write("running")
                for b in passed:           # the ONLY thing written about another's entry
                    p._put(_sched_of(b.path), json.dumps(
                        {"pool_protocol": POOL_PROTOCOL, "bypass": b.bypass + 1}) + "\n")
                self.holder = Holder(p, self.path, self.cost, self.worktree, [self.fd] + got[:c])
                keep = c
            finally:
                for fd in got[keep:]:      # close, never LOCK_UN (rule 2)
                    os.close(fd)
        return True

    def status(self):
        """The wait line for the last failed poll."""
        return self._why

    def leave(self):
        """Give the ticket up: unlink it and its sidecar under admit.lock, then close."""
        if self.holder is not None:
            return self.holder.release()
        if self.fd is not None:
            with self.pool._admit():
                _rm(self.path)
                _rm(_sched_of(self.path))
            os.close(self.fd)
            self.fd = None


class Holder:
    """A granted ticket: the ticket's lock plus `min(cost, budget)` slot locks, all taken in
    one critical section and held for the whole run. `fds` is every descriptor it holds."""

    def __init__(self, pool, path, cost, worktree, fds):
        self.pool, self.path, self.cost, self.worktree, self.fds = pool, path, cost, worktree, fds

    def release(self):
        """Unlink the ticket and its sidecar under admit.lock, then drop every lock by closing
        its descriptor. Skipped by a process that dies: the kernel frees the locks at once and
        the next evaluator unlinks the ticket."""
        if self.fds:
            with self.pool._admit():
                _rm(self.path)
                _rm(_sched_of(self.path))
            for fd in self.fds:
                os.close(fd)
            self.fds = []
