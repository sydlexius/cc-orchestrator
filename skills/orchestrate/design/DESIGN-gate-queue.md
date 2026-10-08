# Design: gate job queue, dispatcher and workers (#538, unit B / #540)

Date: 2026-10-07
Status: PROPOSAL, revised in FIX ROUND 3 after a third hostile review returned DO NOT SHIP. The round's advice was to SIMPLIFY
instead of patching, and this document does: the worker bounds everything itself, the dispatcher only admits and recovers, and one
persisted bit replaces the journal. The former single document is now three, one per unit. The maintainer decisions on #538 (every
comment through 2026-10-07) are BINDING and are designed to, not reopened. RECONCILED 2026-10-07 against the MERGED pool document (PR #543).
Issues: #538 is the EPIC; #540 is unit B, the subject of this document.
Companions: `DESIGN-gate-pool.md` (unit A; cited as "pool section N"), `DESIGN-gate-queue-push.md` (unit C: the push and PR tail),
`DESIGN-gate-queue-wiring.md` (unit D, and the deferred unit E note), `DESIGN-deterministic-floor.md` (threat model).
Depends on: the pool document merged, and pool PRs A1, A2, A3 and A4 (section 10; B3 needs A2, and the wiring document's D1 needs A3).

Scope: one new module (`scripts/gate_queue.py`), one enqueue helper (`scripts/gate-enqueue.py`, the ONLY writer of queue entries),
two internal `gate-runner.py` flags, two schemas in `scripts/orchestrate_schemas.py`, a small extension of `scripts/gate_pool.py`
(section 2), ONE read-only check in `scripts/cleanup-worktree.sh` and the matching prose in `commands/post-merge-cleanup.md`, and
ONE advisory rule in `scripts/orchestrate-steer.sh` (section 8). NO deterministic-floor change. No allow-list entry is harvested
or written by any tool; ONE entry (the helper) is printed for the maintainer to grant by hand (section 8). Stdlib Python 3.11+, no
new bash file. Unit B sends NOTHING outward: its jobs stop after the receipt check. The runner makes NO commit.

Evidence labels: RUN = executed on this machine in an earlier round or by a reviewer (Darwin 27.0.0 and 27.0.1, Python 3.14.8;
appendix), READ = read from the current source, REASONED = argued and not executed, PRIOR = a report with no persisted data. NOTHING
WAS RUN IN FIX ROUND 3; every mechanism this round adds is REASONED. Linux was NOT exercised.

TAKEN FROM THE POOL BY NAME, defined there and not here: `GATEQ_HOME` and `config.toml`; `admit.lock` and `seq`; slot files;
`schedule()` and `commit()`; a ticket and its `.sched` sidecar; the per-worktree exclusion and its key; the nested-run exemption
(`GATEQ_HOLDER`, `.nest`, `GATEQ_NEST`); the pooled step path and the in-process `job_timeout_s`; the orphan watchdog and its held
start; the `.sweep` worktree claim; exit 75; the pool protocol and the foreign-record rules; pool-capable and when the budget may be
configured.

---

## Problem

The load problem is the pool's. The queue's is that QUEUED WORK HAS NO DURABLE ORDER: the pool orders processes that are still
alive, so a lead whose session dies while it waits loses its place, two requests for one branch gate the same tree twice, and
nothing gives a fix round precedence over a first push. The push-side problems are stated in `DESIGN-gate-queue-push.md`.

## Decisions this unit is bound by

Numbered as in the epic and the pool document (3, 4, 6, 9, 11, 12 and 13 are specified there).

- Decision 1: `gate-runner.py` may open a PR for a queued job. With no job it is unchanged: run the gate, write the receipt,
  nothing outward.
- Decision 2: a job is declared DATA (worktree, branch, PR details). Never a command line.
- Decision 5: a failed job loses its position (a re-enqueue goes to the back of its class).
- Decision 6: fix rounds outrank first pushes; FIFO within each class (the pool's `schedule()`).
- Decision 7: one job per branch, replaced in place while queued. If that branch's job is running, the new one waits behind it
  and is dropped when the branch head has not moved.
- Decision 8: self-starting runner: whoever enqueues tries the runner lock; the winner starts a detached runner that drains the
  queue and exits when it is empty. No cron, no designated lead.
- Decision 9: a stale lock must not be able to break the queue: kernel `flock` only.
- Decision 10: result delivery is the designer's call: a result file.

## Shape in one page

```
 gate-enqueue.py add --> queued/<job>.json          worker (job 1): preflight, gate, receipt   [unit C adds push, PR]
      (writes DATA only)        |               +--> one process per job; bounds every call it makes; writes its own result
                                v               |
            gate-runner.py --drain-queue   (ONE detached dispatcher: admits, recovers dead workers, nothing else)
                                |  feeds job tickets to the pool's schedule()
                                v
                    the machine budget (DESIGN-gate-pool.md)
```

OFF BY DEFAULT: with no machine budget at pool protocol 2, or without `[queue] enabled = true`, `add` exits 2 and the commands take
today's inline path.

---

## 1. Queue layout on disk

Root: the pool's `GATEQ_HOME`. The queue adds these; `config.toml`, `admit.lock`, `seq`, `slots/`, `waiters/` and `tmp/` are the pool's.

```
~/.claude/gate-queue/                 0700; every file 0600
  runner.lock                         flock target: the one dispatcher. Permanent; means nothing by itself
  runner.alive                        flock target: liveness probe (never contended by an enqueuer)
  runner.beat                         dispatcher-written in place: {pid, pool_protocol}; its mtime is the heartbeat
  queued/<job>.json                   gate-job/v1, waiting. NEVER flocked, in any directory
  queued/<job>.ticket                 the job's TICKET (section 2): dispatcher-written; nothing in queued/ is ever flocked
  queued/<job>.sched                  the pool's sidecar, exactly its shape: {pool_protocol, bypass}
  queued/<job>.pushing                the ONE persisted bit (unit C). Usually absent
  running/<job>.json|.ticket|.sched|.pushing     the same files while a worker owns the job. The TICKET is flocked by the
                                      dispatcher before the worker exists and handed to it: a FREE lock means a dead worker
  running/<job>.ticket.nest*          the nest locks of nested runs under the worker (the pool's `.nest` chain); finalize, bounce
                                      and recovery unlink them by prefix, only after trying each lock
  waiters/<job>.<pid>.sweep           the worker's SWEEP CLAIM (its own pid), flocked by its watchdog (section 2). In the POOL's
                                      directory, by the pool's rule and name; so is a nested run's, in the same shape
  done/<job>.json, results/<job>.json the terminal entry; gate-job-result/v1 (THE delivery channel)
  payload/<job>.r<rev>/               env.json (the step environment snapshot); unit C adds title.txt and body.md
  logs/<job>.log, logs/runner.log     one job's transcript; the dispatcher's log, append-only
  ctl-tmp/                            TMPDIR for the runner's own calls
```

`<job>` is `<seq>-<id>`: `seq` is the pool's 10-digit counter, allocated under `admit.lock` (next = 1 + the maximum of the `seq` file
and every seq visible in `queued/`, `running/` and `waiters/`); `id` is `<UTC compact timestamp>-<6 hex>`, never an ordering key. A
re-enqueued failed job gets a NEW seq (decision 5); a replace in place keeps the NAME (decision 7). NOT IN THE FIRST DELIVERY:
pruning of `done/`, `results/`, `payload/` and `logs/`, and log rotation; they grow until the user deletes old files.

WRITES. Stage in `tmp/`, `fsync`, `os.replace`; the payload directory is written and fsynced FIRST. Every mutation of `queued/` and
`running/` happens under `admit.lock`. The pool's rule binds the queue (pool section 1): a file some process holds a `flock` on is
never replaced, unlinked and re-created, or renamed over. Here that is one file per job, the ticket, only while it is in `running/`.
`<job>.json` is never flocked, so `add` may replace it in place whenever it is in `queued/`. A running ticket is rewritten only by
its owner (the worker), in place, under `admit.lock` (the pool's owner rule, RUN F4b), and only twice: its pid at start, and a bounce
(section 5). `queued -> running` renames the same inodes, which keeps the ticket's lock (RUN F1a, the same mechanics).

### `gate-job/v1`

| Field | Type | Rule |
|---|---|---|
| `schema`, `protocol` | str, int | const `gate-job/v1`; const 1 (the JOB protocol, distinct from the pool's). Any other value is refused |
| `job_id`, `seq`, `revision` | str, int, int | `job_id` matches `[0-9]{10}-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}` and equals the file stem; `revision` is +1 per replace in place |
| `enqueued_at`, `enqueued_by` | str, object | informational only |
| `repo` | str | `owner/name`, DERIVED from `origin`'s URL |
| `worktree` | str | the RECORDED worktree string (the pool's exclusion key) of a worktree of that repo holding `branch` |
| `branch`, `head_sha` | str | DERIVED from the worktree at enqueue. `head_sha` is THE PIN: the commit the judgment steps approved |
| `after` | str or null | the `job_id` of this branch's job that was RUNNING at enqueue (decision 7), else null |
| `tail` | str | enum `gate`, `push`. `gate` stops after the receipt check: unit B's test and staging surface, enqueued by no command |
| `base`, `pr` | | unit C's fields; both null for a `gate` job |

`gate-job-result/v1`: `schema` and `producer` (const `gate-runner`); `job_id`, `revision`, `repo`, `branch`, `head_sha` copied from
the entry that ran; `state` (enum `done`, `failed`, `refused`, `superseded`, `cancelled`, `error`); `step` (enum `claim`,
`preflight`, `gate`, `receipt`, `finalize`, `recover`; unit C adds four); `reason` (a slug); `gate` (enum `ran-pass`, `ran-fail`,
`reused`, `not-run`); `pushed` (enum `yes`, `no`, `unknown`: always `no` in unit B, unit C states the rule); `receipt_path`,
`log_path`, `started_at`, `finished_at`, `attempts`. Unit C adds the push and PR fields.

The registry today cannot express a list of scalars or a nested object (READ: `_f(items=...)` means "list of objects"), so PR B1
extends it. Unknown keys stay allowed and are NEVER interpreted. `orchestrate_schemas.py` joins `HELPER_NAMES` in B1, because the
runner always runs from the deployed leg and must not validate against "validator not found" (the #284 lockstep applies).

### Every field is validated as data, twice

`add` validates before writing; the worker RE-validates before acting, because the entry sat on disk in between.

- `branch`: from `git -C <worktree> symbolic-ref --quiet --short HEAD` (a detached worktree is refused); `--branch` is only an
  assertion that must match. It must pass `git check-ref-format --branch`, not start with `-`, not be `HEAD` or start with `refs/`,
  and not be `main`, `master`, the cached default or the LIVE default (`git ls-remote --symref origin HEAD`). An unreadable live
  default REFUSES (this code may fail closed; the floor cannot).
- `worktree`: its realpath must equal that of a `worktree` line of the repo's `git worktree list --porcelain` whose `branch` line is
  exactly `refs/heads/<branch>`. The RECORDED string of that line is stored.
- The entry file and the queue root must be regular, owned by the effective uid and not group or other writable.
- Every value reaches a subprocess as ONE argv element of a list, `shell=False`. No field is interpolated into a shell string.

---

## 2. The queue and the pool

### Who owns what (the round-3 ownership finding)

| Thing | Owner | Where |
|---|---|---|
| The extension of `gate_pool.py`: evaluators read job tickets, and `POOL_PROTOCOL` becomes 2 | THIS document, PR B2 | below |
| The holder record of a running job | THIS document | "A job has a ticket", below |
| The foreign-record and foreign-waiter rules | THE POOL document, unchanged | pool section 4; the queue follows them (section 5) |
| The `.sched` sidecar and its shape `{pool_protocol, bypass}` | THE POOL document, unchanged | pool section 1 |

The earlier draft kept queue fields in a job's `.sched` and declared the foreign-waiter rule cut. Both are withdrawn: a pool
evaluator rewriting such a sidecar would have dropped the queue's fields, and the pool keeps the rule. The pool document needs a few
wording edits that say "unit B extends this"; they are listed in the section "Edits this unit needs in the pool document"
(`DESIGN-gate-queue-pool-edits.md`) and change no unit A behavior.

### A job has a ticket

A job is represented to the pool by a TICKET with the pool's own fields, so the pool reads one record shape everywhere:

```
{pid, pool_protocol, kind: "job", name: <job_id>, cost, worktree, state, open_pr,      # the pool reads these
 attempts, cost_pinned}                                                                # queue-private; the pool ignores them
```

- QUEUED: `queued/<job>.ticket`, `state = "waiting"`, written by the dispatcher once it has derived the class and the cost. It is NOT
  flocked: a queued job has no process of its own. A job with no ticket yet is not schedulable.
- RUNNING: `running/<job>.ticket`, `state = "running"`, flocked. This file IS the job's HOLDER RECORD: it carries the pool's frozen
  fields (`pool_protocol`, `worktree`, `cost`) and its flock is held by the worker. `GATEQ_HOLDER` for a worker's children is its
  path, and the pool's nest lock is `<that path>.nest`. Nothing else is a holder record.
- THE SWEEP CLAIM is the pool's, unchanged, and stays in `waiters/` (pool section 4). A worker is a runner with a watchdog, so it
  holds one: `waiters/<job>.<worker pid>.sweep` (`{pool_protocol, worktree}`; the job's name is its ticket name, and the pid keeps
  one attempt's file from ever being the one a later attempt creates), flocked by the worker's
  WATCHDOG. The dispatcher made the commit before the worker existed, so the WORKER creates it, in the `admit.lock` section that
  writes its pid into its ticket (kept until a try-lock on the file fails; the wait also ends when the watchdog child has
  exited: the worker then unlinks its own still-free `.sweep`, releases `admit.lock` and exits non-zero having started nothing,
  and recovery re-queues it, `attempts + 1`), BEFORE its first step and its first external call; its
  held ticket covers the gap. A run nested under a worker (a hook's gate inside its push, a `--pool-run` inside a step) creates its
  own by the pool's rule, `waiters/<job>.<its pid>.sweep`. Nothing is added to `running/` and no evaluator changes: `busy` already
  counts EVERY held `.sweep` in `waiters/`, and any evaluator unlinks a free one. So after a worker is SIGKILLed nothing starts in
  its worktree (the requeued job included) until the last watchdog under it is done; no time limit, as in the pool. The DISPATCHER
  creates no `.sweep`: it holds no worktree and its own calls are reads.
- An unreadable or missing ticket whose lock is free is rebuilt by the dispatcher from the entry (class and cost derived again).
- The ticket is derived outside `admit.lock` and written inside it, ONLY if the entry is still in `queued/` at the revision it was
  derived for; a ticket with no entry is unlinked by the dispatcher, so an orphan never reaches `schedule()`.
- The rename of the four files is not atomic. The order is entry first, ticket last, and recovery joins a split set; a `queued/`
  ticket found with `state = "running"` (the dispatcher died before the rename) is reset to `waiting`.

### The extension of `gate_pool.py` (PR B2)

Unit A's code reads `waiters/` only. From B2, every evaluator (a hand-run gate, the hook, a named command, the dispatcher):

1. adds to `busy` the worktree key of every `running/*.ticket` whose flock is HELD, as for a running ticket in `waiters/` (held
   `.sweep` files are counted already, and a worker's is in `waiters/`);
2. adds to the entries it passes to `schedule()` every `queued/*.ticket`, ONLY while a dispatcher is provably working: `runner.alive`
   is HELD and `runner.beat` was touched within the last 90 s. A queued job has no lock, so the dispatcher's liveness is its own;
3. records a pass against a job in the job's `.sched`, exactly as for another process's ticket;
4. never starts, rewrites or unlinks anything in `queued/` or `running/` unless it is the dispatcher. A `running/` ticket whose lock
   is free holds nothing and is left for the dispatcher's recovery;
5. honors `GATEQ_HOLDER` and `GATEQ_NEST` for a file directly inside `running/` as well as `waiters/`. The pool's rule already
   says so (pool section 5: `GATEQ_NEST` in the SAME directory as the holder's record); B2 implements the `running/` half. A
   worker's nest lock is `running/<job>.ticket.nest`; without this a level-2 run under a worker would take the holder's own `.nest`
   and wait on its ancestor until the push's bound. A `.nest` still HELD when its job leaves `running/` is left in place (the
   pool's rule) and unlinked by the dispatcher on a later pass, once free;
6. allocates a seq from the maximum visible in `queued/` and `running/` as well as `waiters/` (section 1).

`schedule()` itself does not change (pool section 2 already carries `kind == "job"`). An evaluator WITHOUT rule 1 would start a gate
in a worktree where a job is running, so B2 sets `POOL_PROTOCOL = 2` and the queue is on only at protocol 2: an A-era copy then
exits 2 against the config and touches nothing (the pool's config-mismatch rule). Deploying B2 therefore REQUIRES the user's edit of
`config.toml` to `protocol = 2` to keep the pool working, and that edit alone does not turn the queue on (`[queue] enabled`,
section 3). FLAG DAY: a cc-orchestrator worktree whose branch predates B2 gates from the repo leg at protocol 1 and exits 2 until
it merges main (for a reviewed PR that is a head move). B2 adds a doctor WARN for any visible copy of `gate_pool.py` whose
`POOL_PROTOCOL` differs from the configured one (the pool's own WARN tests `gate-runner.py` for the three parts of pool-capable,
never the number). Consequence, stated plainly: whenever no dispatcher is alive and working, queued jobs hold no reservation and
hand-run gates go ahead of them until the next `add` or `wait` starts a runner. That costs the queued jobs their turn for that
interval, nothing else.

### Class, cost, and feeding `schedule()`

CLASS. Class 1 (a FIX ROUND) when the branch has exactly one OPEN PR whose head is in `repo`, else class 2. It is DERIVED, never
requested: the dispatcher reads it once per revision (`gh pr list --repo=<repo> --head=<branch> --state=open
--json=number,isCrossRepository --limit=100`, bounded by `call_timeout_s`; cross-repository PRs are not counted) and stores `open_pr`
in the ticket. An unreadable or timed-out read leaves the job without a ticket for that pass and is retried with backoff (silence on
doubt). The cached value decides ORDER only; every ACTION in unit C uses a fresh read by the worker.

COST. `cost = min(weight, budget)`, `weight` read from the job's own `<worktree>/.gates.toml` by the pool's rule for a gate (a file
that does not parse makes the job `refused` `bad-gates-config`). A job costs 0 only when BOTH hold:

1. a PASSING receipt already binds the branch tree and the worktree is clean (the condition `safe-push.sh` accepts); AND
2. no pre-push hook can start a gate inside the job's push: `git -C <worktree> rev-parse --git-path hooks/pre-push` (which honors
   `core.hooksPath`) does not exist or is not executable. For a `gate` job this condition is vacuous.

The dispatcher computes the cost ONCE per revision, OUTSIDE `admit.lock` (local `git` and file reads), and writes it into the ticket
under the lock; a scheduling pass reads the ticket's number only. A wrong 0 is corrected by a BOUNCE (section 5), which PINS the cost
to the gate weight so the job is not re-admitted at 0 every pass. A wrong non-zero cost holds idle slots for one tail. A cost-0 job
never waits for budget; it still waits for its worktree.

FEEDING. Each pass, under `admit.lock`, the dispatcher is an ordinary evaluator: it passes every job ticket and every live waiter
ticket to `schedule()` with the pool's `free` and `busy`, and starts only the JOBS in the result, through `commit()`. A job's bypass
count lives in its `.sched` like any waiter's, so a heavy first push gets the pool's starvation protection. The dispatcher is the
BATCH committer the pool's `pend` and `prot()` exist for: it commits every job of ONE returned list in that same `admit.lock`
section, each with the `passed` list `schedule()` returned (never recomputed), so one pass cannot pass an entry beyond K. The
pool's foreign-waiter rule applies to the dispatcher unchanged: it starts no job while a LIVE foreign ticket with a lower seq waits.

---

## 3. Configuration

The budget file is the pool's. The queue reads `budget`, `protocol` and `job_timeout_s` and adds one table, which holds EVERY queue
key: an unknown key inside `[pool]` exits 2 for every gate, and unit A's reader ignores a table it does not define (pool section 3).

```toml
[queue]                        # every key optional
enabled = true                 # the queue's OWN switch; absent = off, whatever the pool does
push_timeout_s = 900           # unit C: the worker's bound on one safe-push.sh call; a pre-push hook adds job_timeout_s to it (push section 5)
call_timeout_s = 30            # every other git and gh call the runner itself makes
tool_dirs = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]   # the default; the runner's whole PATH
```

- `tool_dirs` is the runner's `PATH` and nothing more: tools are run BY NAME through it. The real-path lookup of the earlier draft
  is CUT (a `brew upgrade` moved the resolved file under a live dispatcher, which then failed silently).
- A non-integer or non-positive timeout (`bool` refused), an `enabled` that is not a boolean, a `tool_dirs` that is not a list of
  absolute paths, or an UNKNOWN KEY inside `[queue]` makes `add` exit 2 naming the key and the dispatcher admit nothing: a typo
  never silently changes a bound (the pool's unknown-key rule, applied to the queue's table by the queue's reader, never by a gate).
  An invalid `[queue]` is the queue OFF for every reader, by the same path as an unreadable budget file (next bullet), with the key
  named: `enabled` exits 1 and prints it, `wait` exits 2 `queue-config key=<k>` for a queued job, `status` prints `queue=invalid
  key=<k>`, the dispatcher admits nothing and exits once no worker is alive. Queued entries stay and resume in order when the file
  is fixed.
- ON OR OFF. On exactly when the pool is on at `protocol = 2`, the code speaks it, AND `[queue] enabled` is true (absent = off).
  So the protocol edit the pool demands after B2 keeps the pool working without turning the queue on. `gate-enqueue.py enabled`
  exits 0 or 1 and prints why. The pool's activation rule comes first (pool sections 4 and 9): a budget is written only after A1
  to A4 are released and deployed and doctor reports every visible copy pool-capable (`gate_pool.py` beside the runner, the
  import, and the `POOL_WATCHDOG = 1` line); `enabled = true` only after B1 to B4 are released and deployed likewise. VALUES ARE
  READ ONCE (the pool's rule): the dispatcher and each worker keep the values read at start; only on-or-off (which includes the
  validity of `[queue]`) is re-read each pass, and the dispatcher admits nothing while `waiters/` holds a protocol-1 ticket (a ticket already waiting at
  the flag day never re-reads the config and would start beside a job). A changed `budget`, K or cap
  therefore reaches the dispatcher only at its next start: run `stop` after editing them.
- THE BUDGET FILE REMOVED or unreadable: off from the next poll. `add` exits 2; `wait` keeps waiting for a RUNNING job and exits 2
  `queue-off` for a queued one; the dispatcher admits nothing and exits once no worker is alive; workers finish (they keep the values
  they started with); queued entries stay, `status` and `cancel` keep working, and restoring the file resumes them in order.

---

## 4. Processes

### Locks

`fcntl.flock` on open descriptors in the 0700 directory; no stale-age break, no pid probe (decision 9). The pool's three rules bind
the queue (no bare fork, close and never `LOCK_UN`, a flocked file is never replaced: pool section 4), plus three for hand-offs:

1. A LOCK MOVES ONLY BY AN EXPLICIT HAND-OFF: `pass_fds=(...)` naming exactly the descriptors the child is to own, after which the
   giver closes its copies. Two exist: `runner.lock` from starter to dispatcher, and a job's ticket and slot descriptors from
   dispatcher to worker.
2. THE RECEIVER'S FIRST ACT IS `os.set_inheritable(fd, False)` ON EVERY HANDED DESCRIPTOR. `pass_fds` leaves one inheritable in the
   child: RUN (R2, R6) a child the worker started without `close_fds` kept the lock HELD after the worker died; RUN G1, with the
   call made first the lock was FREE the moment the worker was SIGKILLed.
3. A HANDED DESCRIPTOR IS HELD UNTIL THE PROCESS EXITS. RUN (R3): a worker that closed it looked dead while alive. The one exception
   is a bounce (section 5), where the worker closes its ticket descriptor inside the `admit.lock` section that moves the job back.

| Lock | Holder | Held for |
|---|---|---|
| `runner.lock`, `runner.alive` | the dispatcher and nothing else | its whole life |
| `admit.lock` (the pool's) | anyone | milliseconds: directory work, one `Popen`, and a worker's wait for its watchdog to lock its `.sweep`; never a `git` or `gh` call, never a gate |
| `waiters/<job>.<pid>.sweep` (the pool's) | the worker's WATCHDOG, by its own open | from the worker's start until every group it registered is gone |
| `slots/NNN.lock` (the pool's), `running/<job>.ticket` | the job's worker (taken by the dispatcher, handed over) | the job |

Order: `runner.lock`, then `admit.lock`, then slot and ticket files with `LOCK_NB` only. Nothing blocks on a slot or a ticket, and a
ticket is locked or probed only under `admit.lock`, so no cycle exists and a probe can never make an admission fail.

### Start, exit, and detach

`gate-enqueue.py add`: (1) validates, takes `admit.lock`, allocates the seq, publishes the entry, releases the lock; (2) THEN tries
`runner.lock` with `LOCK_EX | LOCK_NB`; (3) on a win starts the dispatcher and HANDS IT THE LOCKED DESCRIPTOR, so the lock is never
released and re-taken between starter and runner; (4) on a loss does nothing: a live dispatcher will see the entry.

THE ONE EXIT RULE. The dispatcher exits by itself only when no worker is alive AND there is nothing it may admit or recover. It
then releases `runner.lock`, re-scans once, and if the queue is on and an entry appeared re-takes the lock non-blocking (losing
means a newer runner exists). An entry is always written BEFORE the lock attempt, so every interleaving ends with a lock holder that
has seen the entry. ONLY AN ENQUEUER OR A `wait` TOUCHES `runner.lock`: every other process that asks "is a dispatcher alive" (`status`, `stop`,
a ticket's evaluator) probes `runner.alive` with a shared non-blocking lock, so no probe can make an enqueuer lose the race.

```
p = subprocess.Popen([sys.executable, RUNNER, "--drain-queue"], stdin=PIPE, stdout=log, stderr=log,
                     start_new_session=True, close_fds=True, pass_fds=(lock_fd,), cwd=GATEQ_HOME, env=minimal)
p.stdin.write(one line: {"runner_lock_fd": lock_fd}); p.stdin.close()
```

- `start_new_session=True` is `setsid()`: no controlling terminal. RUN E2: such a child was alive after the Bash call returned,
  reparented to pid 1.
- `RUNNER` is ALWAYS the deployed `~/.claude/scripts/gate-runner.py` (home directory from the password database), never the helper's
  own leg: a dispatcher started from a cc-orchestrator worktree would run that branch's unreviewed code against other repos' jobs.
- `--drain-queue` and `--queue-worker` take NO argument and carry NO job data. Each verifies a hand-off record on stdin: the named
  descriptor is open on the expected file (same device and inode) and a fresh non-blocking lock attempt on that path FAILS. A
  hand-typed run has no record and EXITS 2 HAVING DONE NOTHING (RUN G3b). The check guards accident, not intent (section 8).

### Two environments

THE STEP ENVIRONMENT is what a job's GATE STEPS run with. At enqueue the helper snapshots an ALLOW-LIST into `env.json`: `PATH`,
`HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_*`, `TMPDIR`, `XDG_*`, `SSH_AUTH_SOCK`, `GNUPGHOME`, `GH_HOST`, `GH_CONFIG_DIR`, the
common toolchain roots (`NVM_DIR`, `GOPATH`, `GOROOT`, `GOFLAGS`, `CARGO_HOME`, `RUSTUP_HOME`, `VIRTUAL_ENV`, `PYENV_ROOT`), and any
name the repo lists in `.gates.toml` `[queue] env_passthrough` (read only by `gate-enqueue.py`). NOT under `[pool]`: in both files
that table is the pool's, and `gate-runner.py` ignores a `.gates.toml` table it does not define (READ: this repo's file has `[prep_pr]` and `[merge_pr]`; `[steer]` is a consumer opt-in).

- `env_passthrough` is BOUNDED: a name matching `GIT_*`, `GATEQ_*`, `DYLD_*`, `LD_*`, `PYTHON*`, `BASH_ENV`, `ENV`, `IFS` or `PATH` is
  refused at enqueue and stripped again at load. `.gates.toml` is agent-editable; without the bound, `GIT_CONFIG_COUNT` would carry
  `commit.gpgsign=false` into a detached process.
- No command-line-valued variable is persisted in a queue file (`GIT_SSH_COMMAND` and `GIT_*` as a whole are out: decision 2), and
  no credential (`GH_TOKEN`, `GITHUB_TOKEN`, anything matching the credential-name patterns `settings-scrub.py` uses). `GATEQ_*` is
  never read from a snapshot; the worker sets `GATEQ_HOLDER` and `GATEQ_HOME` on its children from its own state.

THE CONTROL ENVIRONMENT is what the dispatcher, every worker, and every call the runner itself makes run with. It is a property of
the RUNNER: both processes, right after the descriptor check, DISCARD the inherited environment and build this one (RUN G4: a
process started with `GIT_CONFIG_COUNT`, `GIT_EDITOR`, a token and a hostile `PATH` passed none of them to its child). ONE inherited
value is read first, `GATEQ_HOME`, which the harness needs: an absolute directory owned by the effective uid, mode 0700, else exit 2.

```
PATH                 tool_dirs joined with ":"          HOME, USER, LOGNAME   from the password database
LANG, LC_ALL         C.UTF-8                            TMPDIR                <GATEQ_HOME>/ctl-tmp
GATEQ_HOME           the validated root                 GIT_TERMINAL_PROMPT   0
GIT_SSH_COMMAND      "ssh -o BatchMode=yes" (constant)  GH_PROMPT_DISABLED    1
GIT_OPTIONAL_LOCKS   0                                  (so the dispatcher's `git status` never takes `index.lock`)
```

plus four VALUES from the job's snapshot, each validated as data and used only for that job's calls: `SSH_AUTH_SOCK` (a socket owned
by the effective uid), `GH_CONFIG_DIR` and `XDG_CONFIG_HOME` (directories owned by the effective uid), `GH_HOST` (`[A-Za-z0-9.-]+`);
an invalid one is dropped. Stdin is `/dev/null`. Nothing may prompt. This defends against `PATH` redirection of the runner's own
`git` and `gh` only: it is NOT a defense against a same-user writer (READ: `/opt/homebrew/bin` is mode 775, user-owned), and it still
honors git's configuration files. Process limits and an OS SANDBOX are inherited from the starter and no environment fixes that: a
dispatcher started from a sandboxed call fails its jobs with the OS error (REASONED); the remedy is `stop` and a fresh enqueue.

### Every call is bounded by the process that makes it, and dies with it

ONE helper in `gate_queue.py` makes every external call of the dispatcher and of a worker. It answers round 3's findings 2 and 3
together: no call depends on another process for its timeout, and no call outlives the process that made it.

WHO REGISTERS WHAT. The dispatcher and each worker start ONE watchdog of their own (the pool's death-pipe watchdog, pool section 4,
PR A4) before their first external call. Its code stays in `gate-runner.py` (the pool's placement); the helper is handed its pipe.

| Process | Registers with ITS OWN watchdog | Never registers |
|---|---|---|
| a worker | every gate step group (the pooled step path does this already), and the process group of EVERY call the helper makes: `git`, `gh`, and in unit C `base-freshness.sh` and `safe-push.sh` | - |
| the dispatcher | the process group of each of its own calls (the class read, the cost reads) | a worker: workers must survive it |
| a pre-push hook's gate inside a worker's push (unit C) | its own step groups (it is a `gate-runner.py` with its own watchdog) | - |

The helper, for one call (REASONED, not run; harness cases in B2):

1. starts `[sys.executable, "-c", LAUNCH, <start fd>, *argv]` in its OWN SESSION (`start_new_session=True`, `close_fds=True`,
   `pass_fds` = the start pipe only, the control environment, stdin `/dev/null`). `LAUNCH` reads one byte from the start pipe and
   then `exec`s the list argv with no shell; on EOF it exits without running anything. This is the pool's HELD START for a list argv;
2. writes `+<pgid>` to the watchdog, THEN the start byte, so a process killed between the two leaves nothing running;
3. reads output from FILES: stdout and stderr go to files under `ctl-tmp/`, never pipes, and the helper waits on the leader's
   exit, not on end-of-file (RUN, reviewer, round 4: k2.py: a descendant that called `setsid()` and kept a pipe held the read
   blocked 9 s after the child exited; with a file the call returned in 0.0 s). It waits at most the call's timeout. On expiry it sends SIGTERM to the process GROUP, waits 5 s, then SIGKILLs the group. Killing
   only the direct child is not enough: RUN G2a left a background grandchild alive, RUN G2b (own session, group kill) left nothing;
4. prunes in the pool's PRUNE ORDER: where `os.waitid` exists it learns of the leader's exit with `WNOWAIT`, sweeps the group,
   writes `-<pgid>` and only then reaps; elsewhere it reaps, sweeps and writes `-<pgid>` first thing (the pool's stated residual).

LIMIT: a descendant that starts its own session escapes the group sweep and the watchdog; nothing here reaches it.

So when a worker is SIGKILLed, its watchdog reads EOF and SIGKILLs every listed group at once: a gate step, an in-flight `gh` call,
an in-flight push, and keeps the worker's `.sweep` locked until every one of them is gone (section 2). RUN H2 (round-3 reviewer) showed the defect this closes: an own-session stand-in was alive after its starter was
killed. A hook's gate inside a killed push is a member of the push's group, dies with it, and its own watchdog then sweeps its steps.
RESIDUAL, stated plainly: if a worker AND its watchdog are both killed, registered groups survive and nothing claims the worktree; recovery (section 5) and unit C's
one persisted bit keep the REPORT truthful in that case, they do not stop the orphan.

| Call | Bound | On expiry |
|---|---|---|
| any `git` or `gh` call (and unit C's `base-freshness.sh`) | `call_timeout_s` | an UNREADABLE result for that call: silence on doubt in the dispatcher, the step's `error` in a worker |
| the gate | the pool's `job_timeout_s`, in-process, exactly as a hand-run gate (pool section 4, "The hand-run timeout") | `failed` `job-timeout`, with a fail receipt |
| `safe-push.sh` (unit C) | `push_timeout_s`; `job_timeout_s + push_timeout_s` when a pre-push hook can run (push section 5) | `error` `push-timeout`, `pushed=unknown` |

WHAT BOUNDS A JOB: `job_timeout_s`, plus the push's bound, plus a handful of `call_timeout_s`, all enforced by the worker with no
other process alive. Nothing outside bounds the WORKER PROCESS itself; a worker older than that sum is a bug, and `status` marks it
`overdue`. A worker handles SIGTERM by sweeping its registered groups and writing its own result (`error` `terminated`); that is how
an operator ends a running job (`kill <pid>`, the pid `status` shows). THE HANDLER ONLY RAISES in the main flow: it is deferred
while `admit.lock` is held and disabled after a bounce or finalize (RUN, reviewer, round 4: k1.py: a handler that takes
`admit.lock` while the main flow holds it blocks forever and keeps the lock HELD for every evaluator until SIGKILL). A gate that
returns 130 inside a worker (the parallel gate path turns the signal into exit 130; READ `gate-runner.py`) is `error`
`terminated`. A worker that ignores SIGTERM is ended by `stop`, then `kill -9` of the worker, then `cancel`.

### Dispatch

```
drain():
    verify the hand-off record and runner.lock; set it non-inheritable            # else exit 2
    read GATEQ_HOME; discard the environment; build the control environment; start the watchdog
    take runner.alive; write runner.beat
    loop:
        beat()                                         # also before and after every external call
        on = the pool config is readable, has a budget, is at this protocol, AND [queue] is valid with enabled = true
        recover()                                      # section 5: running/ entries whose ticket lock is FREE
        if on:
            for at most ONE job without a ticket: derive open_pr and cost   # one gh read and local reads, OUTSIDE admit.lock
            with admit.lock:
                validate each new entry (invalid -> result refused, entry -> done/)
                write the ticket derived above
                for e, passed in schedule(job tickets + live waiter tickets, free, budget, busy):
                    if e is not a job: continue                              # its own process starts it
                    replace e's ticket with state = "running"                # unflocked: nothing in queued/ is ever flocked
                    fd_t = open(it); flock(fd_t, LOCK_EX | LOCK_NB)          # BEFORE the worker exists; cannot fail here
                    slot_fds = commit(e, passed)
                    rename queued/e.{json,ticket,sched,pushing} -> running/
                    w = Popen([sys.executable, RUNNER, "--queue-worker"], stdin=PIPE, stdout=log, stderr=log,
                              start_new_session=True, close_fds=True, pass_fds=(fd_t, *slot_fds), cwd=GATEQ_HOME, env=minimal)
                    write the hand-off record {job_id, ticket_fd, slot_fds} to w.stdin; close it
                    close fd_t and slot_fds here                             # close only, never LOCK_UN
        reap exited children (waitpid WNOHANG)
        if no worker is alive and nothing to admit or recover: exit by the release-and-rescan rule
        sleep 0.5
```

- THE TICKET LOCK EXISTS BEFORE THE WORKER DOES, so `busy` never omits a worktree whose job has been admitted. If `Popen` fails, the
  dispatcher closes the descriptors (freeing them) and renames the files back under the same `admit.lock`.
- A WORKER verifies its record (the ticket descriptor is open on `running/<job_id>.ticket` and held; every slot descriptor is a held
  slot), sets them non-inheritable, discards the environment, builds the control one, starts its watchdog, then in ONE `admit.lock`
  section writes its pid into its ticket and creates its `.sweep` (section 2), then, outside the lock, re-validates the entry and runs the tail. It holds no `runner.lock`, `runner.alive` or `admit.lock`.
- WHEN THE DISPATCHER DIES, workers do not (RUN F1b: `runner.lock` and `admit.lock` were free at once; the worker kept its locks).
  They lose NOTHING: every bound is their own, and each finalizes its own job. The next dispatcher sees them as held tickets.
- THE HEARTBEAT. `runner.alive` HELD says a dispatcher exists, not that it is working. It touches `runner.beat` each pass and around
  every external call, so a healthy beat is never older than `call_timeout_s` plus the poll interval; `call_timeout_s` is therefore
  bounded at 60 (a larger value makes `add` exit 2), or a healthy dispatcher would read as hung against the 90 s window. While the
  queue is off the dispatcher still beats, so already-waiting tickets keep yielding to jobs nobody will admit until the last worker
  exits. Tickets honor queued jobs only
  while the beat is younger than 90 s. `add`, `wait` and `status` print `runner=hung pid=<n> beat=<age>s` otherwise. Nothing
  restarts a hung dispatcher automatically; someone runs `stop`. A hung dispatcher costs queued jobs their turn and nothing else.

### The tail in unit B

| # | Step | What happens | On failure |
|---|---|---|---|
| 1 | `preflight` | Re-validate the entry. The worktree exists, holds `branch`, is clean, not mid-rebase or merge; `refs/heads/<branch>` equals `head_sha`. If `after` is set, that job's result says `done`, and its `head_sha` equals this one's: `superseded` (section 5; any other predecessor state, or no readable result, drops nothing) | `refused` `no-worktree`, `dirty-worktree`, `head-moved`, `protected-branch`, `mid-rebase`; `superseded` `dropped-unchanged` |
| 2 | `gate` | A passing receipt binds `HEAD^{tree}` and the tree is clean: REUSE it. Else, holding slots: run the gate IN-PROCESS through the pool's pooled step path with the step environment, `GATEQ_HOLDER` set, and `_write_receipt` to `<git-dir>/prep-pr-receipt.json`. Else (admitted at cost 0): BOUNCE | `failed` `gate-failed` or `job-timeout` (the fail receipt is on disk) |
| 3 | `receipt` | Full `gate-receipt/v1` validation, `producer == gate-runner`, `result == pass`, `tree_sha == HEAD^{tree}` | `failed` `receipt-invalid`, `receipt-stale` |
| 4 | `finalize` | Write the result atomically; then, under `admit.lock`, move the job's files to `done/` and delete `.pushing`; exit (which frees the slots and the ticket) | - |

Unit C inserts `classify` and `freshness` after step 1 and `push` and `pr` after step 3. The runner writes nothing into the worktree
at any step. A re-enqueue that changed only PR details while the job ran is `superseded`, and the result says to use `gh pr edit`.

---

## 5. Job states and recovery

```
            add                     admit                        tail ends
 (nothing) -----> queued/<job> -------------> running/<job> ----------------> done/<job> + results/<job>.json
                    ^   |  replace in place        |                            done | failed | refused | superseded | error
                    |   +--(same name, rev+1)      |
                    |   +--cancel--> done/ (cancelled)
                    +-- worker died (attempts + 1), or BOUNCE (a cost-0 job that needs slots after all)
```

- `running -> done`: the worker writes the result FIRST, then moves the files. A crash between the two leaves a result beside a
  `running/` entry, and recovery finishes the move. A terminal entry never moves again; a re-enqueue after `failed`, `refused`,
  `cancelled` or `error` is a NEW entry (decision 5).
- BOUNCE: a worker admitted at cost 0 that finds it needs slots (the receipt no longer binds the tree, or in unit C a pre-push hook
  appeared) does this in ONE `admit.lock` section: rewrites its ticket in place (`state = "waiting"`, `cost` = the gate weight,
  `cost_pinned`), renames its files back to `queued/`, and CLOSES its ticket descriptor, so no flocked file is ever visible in
  `queued/`. Then it exits. A cost-0 job holds no slot. The name is kept, so the position is; it is not an attempt.
- REPLACE IN PLACE (decision 7), under `admit.lock`: an entry for the same `(repo, branch)` in `queued/` gets a new payload
  directory, `revision + 1`, the new `head_sha`, and an `os.replace` of the same file name. Its `.sched` bypass count is kept; its
  ticket is deleted (class and cost are derived again); a `.pushing` marker is KEPT (it is deleted only on the move to `done/`;
  unit C says what `add` prints).
- SAME BRANCH RUNNING: the new entry gets a new seq and `after = <the running job>`. It is not eligible while that job runs (same
  worktree key) and is dropped at preflight when the head has not moved. The runner never moves a head, so comparing the two pins
  is the whole test. `superseded` requires the predecessor's state to be `done`: a cancelled or failed predecessor with the same
  head does not drop its successor. A bounce or a recovery can put the earlier job back in `queued/` beside the `after` job; a
  third `add` for the branch replaces the NEWEST queued entry.

RECOVERY. On start and each pass, under `admit.lock`, a `running/` entry of this protocol whose ticket lock is FREE has a dead worker.

1. A result exists: finish the move to `done/`.
2. Otherwise `attempts + 1`. At 3 attempts the job ends `error` `worker-died-3x` (`step = recover`), so a job that kills its worker
   cannot loop forever.
3. Otherwise the files go back to `queued/` under the same name (position kept), with the ticket replaced (`state = "waiting"`, the
   new `attempts`). A `.pushing` marker travels with them.

WHAT SURVIVES A DEAD WORKER is exactly the entry, the attempt count, and that one marker. Unit B's steps are reads, a gate that is
skipped when a receipt binds the tree, and a receipt check, and the runner changes nothing in the worktree, so a recovered `gate` job
simply re-runs. What a recovered job does when the marker is present is unit C's rule (it reads origin FIRST), and the `pushed`
value of every result this section writes follows that rule: `no` when the marker is absent, `unknown` when it is present.

POOL PROTOCOL. The queue adds no protocol rule of its own. A record of another pool protocol is never scheduled, rewritten or
recovered by this dispatcher; `status` shows it as `foreign-protocol`; a foreign RUNNING ticket whose lock is held counts as busy by
the pool's record-mismatch rule; a foreign QUEUED job has no live owner and is ignored until `cancel` clears it. `gate-job/v1`
carries its own, independent JOB `protocol`.

---

## 6. The helper's verbs

`add` prints one line, `GATE-ENQUEUE: ENQUEUED|REPLACED id=<job> position=<n> runner=started|alive|hung|none`, and exits 0 (queued),
1 (REFUSED: validation said no, nothing written) or 2 (setup: queue off, the live default-branch read failed, the directory is
unwritable). `add` makes no `gh` call.

`wait <job> [--timeout <s>]` polls every 2 s and prints exactly ONE stdout line, `GATE-JOB: <STATE> id=<job> branch=<b> ...`. On
each poll, if `runner.lock` is free and its job needs a dispatcher, it starts one by `add`'s steps 2 and 3. A job needs a dispatcher
when it is QUEUED with the queue on, or is in `running/` with a FREE ticket lock (probed under `admit.lock`). A RUNNING job with a
live worker needs none. Exits: 0 `done`, 1 `failed`, 3 `refused`, 4 `superseded`, 5 `error`, 6 `cancelled` (each with a result file);
7 `no-runner` (three start attempts produced no dispatcher: the job is STILL QUEUED, nothing ran, and there is NO result file); 75
still in-progress at `--timeout`; 2 usage or setup, including `queue-off` for a queued job.

`status` is read-only (it takes `admit.lock` for the instant it probes locks) and prints the pool's line and one line per record:

```
GATE-POOL: budget=10 free=2 protocol=2 runner=alive pid=4411 beat=1s queued=3 running=2 waiters=1
running 0000000041-...  cls=1 cost=4 pid=4502 age=312s            branch=fix/x  worktree=/w/a
queued  0000000045-...  cls=2 cost=4 bypass=1 attempts=0           branch=feat/y worktree=/w/c
```

A queued line held back by a held `.sweep` in its worktree gains `blocked=sweep pid=<n>`.

A running line gains `overdue` (older than the job's bounds), `dead` (ticket lock free, awaiting recovery) or `foreign-protocol`.
`status --worktree <path> --quiet` prints nothing and answers by exit code: 0 = no record claims the worktree, 10 = claimed, 2 =
could not determine. "Claimed" means a record whose flock is HELD (a running job's ticket, a pool ticket, or a `.sweep` file) names
it, or a QUEUED job does, with the queue on OR off: a queued job is durable work that resumes when the queue is turned back on, and
`cancel` works either way. The read uses only the directory and the locks, never the config, so a malformed config cannot make it
undeterminable. 10 is used because a crashed Python exits 1 (RUN R5).

`cancel <job>` withdraws a job that no process owns. It acts by itself, under `admit.lock`, with or without a dispatcher.

| The record is | `cancel` does | Exit |
|---|---|---|
| in `queued/`, or in `running/` with its ticket lock FREE and NO result yet (a dead worker), any pool protocol | writes the result `cancelled` (`cancelled-by-operator`, or `cancelled-dead-worker`) and moves the files to `done/`. `pushed` follows section 5's rule | 0 |
| in `running/`, ticket lock HELD | nothing, ever: `GATE-ENQUEUE: RUNNING id=<job> pid=<n>` | 1 |
| in `running/`, ticket lock FREE, and a result file already exists (the worker died between writing it and moving its files) | finishes the move, prints the existing state | 1 |
| already terminal, or unknown | nothing; prints the existing state | 1 |

There is no `cancel --all` and no `cancel` of a running job: one ends by finishing, by its own timeouts, or by SIGTERM to its worker.

`stop` ends the dispatcher and starts nothing. It probes `runner.alive`: free means `not-running`, exit 0. Otherwise it reads the
pid from `runner.beat`, sends SIGTERM (the dispatcher exits at once and never signals a worker), waits up to 10 s for `runner.alive`
to come free, then SIGKILLs, and exits 0 only after it has seen the probe succeed (else 2). The pid is signalled only while the
probe shows the lock held, which narrows and does not close the pid-reuse window. WHAT `stop` COSTS: until the next `add` or `wait`,
nothing is admitted or recovered and queued jobs hold no reservation. Running jobs are unaffected. To keep the queue down, remove
`config.toml` first, then `stop`. All verbs ride the ONE grant; `status`, `cancel` and `stop` work with the queue off.

---

## 7. `cleanup-worktree.sh` and `/post-merge-cleanup`

READ (`scripts/cleanup-worktree.sh`, lines 122 to 156): the script refuses to remove the worktree holding the caller's cwd (#448,
`exit 1`), and only then captures the run directory and runs `git worktree remove`. A clean worktree with a gate running in it is not
dirty, so nothing today stops it vanishing under a running job. THE NEW CHECK SITS IMMEDIATELY AFTER THE #448 BLOCK AND BEFORE THE
RUN-DIRECTORY CAPTURE, and only when the worktree directory exists:

| Condition, tested in this order | The script |
|---|---|
| `CC_CLEANUP_IGNORE_GATE_QUEUE=1` is set (THE OVERRIDE) | skips the check, prints one stderr line saying so, PROCEEDS |
| `gate-enqueue.py` is found neither beside the script nor at `~/.claude/scripts/` | PROCEEDS: no queue is installed |
| the queue root (`${GATEQ_HOME:-$HOME/.claude/gate-queue}`) is not a directory | PROCEEDS: no queue has ever run here (tested in bash) |
| the helper's `status --worktree <path> --quiet` exits 0 | PROCEEDS |
| it exits 10 | REFUSES (`exit 1`, nothing touched), naming the record; the message says to wait or to `cancel` a queued job |
| it exits anything else (2, a crash's 1, 127: READ `/usr/bin/python3` here is 3.9) | REFUSES (`exit 1`, nothing touched) as UNDETERMINED, printing the code |

- The answer does not depend on the queue's switch: a queued job claims its worktree while the queue is off too, or a cleanup
  made then would leave a job that can only end `no-worktree` once the queue is back. No path refuses forever: a queued job is
  cancellable (with the queue off as well) and a running one ends at its own timeouts.
- THE OVERRIDE IS THE MAINTAINER'S. The refusal message does not print it; `commands/post-merge-cleanup.md` says an agent reports
  the refusal and sets the variable only on the maintainer's explicit word in that session.
- READ (`commands/post-merge-cleanup.md`, Step 3, about lines 104 to 114): that command also removes a worktree INLINE with a bare
  `git worktree remove`, which the script's check never sees. PR B4 edits the step to run the same `status --worktree --quiet` probe
  first (literal helper path, the "Helper exec paths" rule) and to read its exit code exactly as the table does.

---

## 8. Security and least privilege

The floor's threat model: an honest lead or teammate on the obvious path, NOT an adversary. Unit B sends nothing outward; the
push-side rules (the closed `safe-push.sh` template, the argv registry, what the runner enforces because the floor cannot) are in
`DESIGN-gate-queue-push.md`.

- THE QUEUE DIRECTORY AND THE DESCRIPTOR CHECKS GUARD AGAINST ACCIDENT, NOT INTENT. The directory is the user's own, mode 0700, and
  the agent runs as the user. Any permitted Bash command that writes a file can write a job entry, which the next dispatcher drains.
  A process that flocks a hand-written `running/` ticket and execs `--queue-worker` with that descriptor passes the worker's check
  (RUN R1) and also bypasses the budget, because no dispatcher counted it.
- THE OS-LEVEL POWER IS THE SAME AS AN INLINE PUSH, BUT THE PERMISSION SYSTEM AND THE FLOOR DO NOT SEE IT: once unit C exists, a
  forged entry needs no push or PR grant. A teammate that holds no `safe-push.sh` and no `gh pr create` grant, but any file-writing
  Bash call, can cause a push and a PR this way. What bounds it is what a hand-edited entry can cause (below and unit C), a
  charter rule (`DESIGN-gate-queue-wiring.md`) and one nudge (next bullet). No deny.
- THE ENQUEUE NUDGE (PR B4, ADVISORY). `orchestrate-steer.sh` gains one rule: `gate-enqueue.py` at command position (the clause
  split of its rules 2, 3 and 6) with the verb `add`, `cancel` or `stop`, in a call whose hook payload carries a non-empty
  `agent_type`, prints a `STEER:` line naming the charter rule. Per the #426 measurement (cited, not re-run; B4's UAT repeats it
  on the deployed Claude Code) the lead's main thread carries no `agent_type`, and a subagent, a fork and a teammate in either
  mode each carry one. Exit 0 on every path; an absent or unreadable field is silent. It says the rule at the point of use; it
  is NOT enforcement.
- ACCEPTED LIMIT, decided here and not deferred: a teammate that ignores the charter and the nudge, or forges an entry, can
  queue a push. A deny needs the caller's identity, which only a PreToolUse hook sees, so it is a FLOOR change; and it would
  cover the helper's command line only, while the forged entry (any file write by the same uid) stays open. Against the
  floor's own threat model (an honest actor on the obvious path) it adds nothing to the nudge; against anyone else it is one
  file write from useless. The outcome stays bounded by what a hand-edited entry can cause (below and unit C).
- No `Edit(...)` rule for the directory is in the required list and none must ever be added; that keeps the OBVIOUS path (the file
  tools) closed, and nothing more is claimed for it.

THE ONE GRANT: `Bash(python3 ~/.claude/scripts/gate-enqueue.py *)` (plus the plugin-path form), PRINTED for the maintainer to grant by
hand like the gate-runner entry (#169), never harvested and never written by a tool. The most it can do: `add` queues one job for
the checked-out NON-default branch of a worktree (in unit B that runs the repo's gate; unit C adds one additive, receipt-gated push
and at most one `gh pr create`); `wait` is read-only and may start a dispatcher; `status` and `enabled` are read-only; `cancel`
withdraws a job no process owns; `stop` ends the dispatcher the queue itself started. It cannot merge, approve, comment, edit a PR,
force, rewrite, delete a ref, push a tag or a default branch, post a review trigger, make a commit, or run a caller-supplied
command. The existing gate-runner grant gains `--drain-queue` and `--queue-worker`; neither carries data and each typed by hand
exits 2 (a forged hand-off passes, as stated). The detached runner and its children are not tool calls and need no entry.

WHAT A MALFORMED OR HAND-EDITED ENTRY CAN CAUSE IN UNIT B: the gate of the named worktree runs (its own `.gates.toml`, already
trusted config for anyone who can run shell there). An unknown `protocol`, a stem that differs from `job_id`, a failed schema or
field check, a `worktree` that is not a worktree of `repo` holding `branch`, or a `head_sha` that does not match the branch is
`refused` with nothing run. A hand-edited `env.json` can point a GATE STEP at another binary, which is inside what "can run the
repo's gate" already means; it cannot reach the runner's own calls.

THE FLOOR is NOT changed and is NOT taught to read the queue, a job or a result. It covers the enqueue and wait command lines (where
it has nothing to deny) and does NOT cover anything the detached dispatcher or its workers execute.

---

## 9. Failure modes (queue)

Pool failures are in the pool document; push and PR failures are in `DESIGN-gate-queue-push.md`.

| Failure | Resulting state | Recovery |
|---|---|---|
| Dispatcher SIGKILLed | `runner.lock` and `admit.lock` free at once (RUN F1b, F2); its in-flight call is swept by its watchdog. Workers finish their own tails | The next `add`, or a `wait` whose job needs a dispatcher, starts one |
| Dispatcher ALIVE BUT HUNG | Beat stale; tickets stop honoring queued jobs after 90 s. Running jobs are unaffected | `runner=hung` is reported; someone runs `stop`. Not automatic |
| Worker SIGKILLed mid-gate | Slots and ticket free at once. Its watchdog SIGKILLs every registered group and holds the `.sweep` claim on the worktree until they are gone | Back to `queued/` in place, `attempts + 1`; it is not eligible until the claim frees, then the gate re-runs. Nothing outward happened |
| Worker AND its watchdog both killed | Slots free; registered groups survive as orphans in the worktree | Recovery re-queues; the re-run gate's clean-before and clean-after checks are the backstop (the pool's limit for any raw run) |
| Older deployed runner started with `--drain-queue` | READ (`_parse_args`): it ignores the flag and runs a plain gate in `GATEQ_HOME`. An A-era copy exits 2 there (pool protocol 1 against the configured 2) and takes no ticket; a pre-pool copy finds no `.gates.toml` (checked: `~/.claude` is not in a git repository and has no fallback target, so this holds only under that condition), falls through its fallback chain and exits 0. No entry is touched | `wait` exits 7 `no-runner`; run `configure --apply` |
| Clock stepped | Order is by `seq`. A beat can look fresh up to 90 s too long, or stale until the next beat | None: fairness only |
| Disk full | `add`: exit 2, nothing published. Worker: the result write fails, the entry stays in `running/` | The worker retries three times, then exits; recovery re-runs when space returns |
| Lead edits the worktree or commits after enqueueing | Dirty worktree, a `dirty-after-run` receipt, or a moved head | `failed` or `refused` `head-moved`; commit and re-enqueue |

---

## 10. Decomposition: unit B (#540) as four PRs

Round 3 found the former B2 too large for one review, so the dispatcher and the worker are SEPARATE PRs unconditionally.

- B1, DATA ONLY: both schemas and the registry extension; deploy `orchestrate_schemas.py`; `gate-enqueue.py` with `add` (`--tail
  gate`), `enabled` (and the `[queue]` table's reader), `status` (with `--worktree --quiet`) and `cancel`. No runner exists, so `add` prints `runner=none`. `gates.toml.md` documents `.gates.toml` `[queue] env_passthrough`.
- B2, THE DISPATCHER AND THE POOL EXTENSION: the `gate_pool.py` job-ticket reader and `POOL_PROTOCOL = 2`; `--drain-queue`, the
  `runner.lock` hand-off, the discarded environment, the bounded-call helper with watchdog registration, class and cost, admission
  through `schedule()`, the heartbeat, recovery, `wait`, `GATEQ_HOLDER` and `GATEQ_NEST`
  honored for `running/`, and
  the doctor WARN for a visible `gate_pool.py` whose `POOL_PROTOCOL` differs from the configured one. Tested against a STUB worker.
- B3, THE WORKER: `--queue-worker`, its hand-off, non-inheritable descriptors, the step environment, the in-process gate and receipt
  check (`tail = "gate"`), its `.sweep` claim, the bounce, finalize, SIGTERM.
- B4, OPERABILITY: `stop`, the `hung`, `overdue` and `dead` reports, the `cleanup-worktree.sh` check, the
  `commands/post-merge-cleanup.md` edit, and the enqueue nudge in `orchestrate-steer.sh` (ADVISORY tier, earned by checking the
  post-diff file). A script FUNCTION change, in this unit on purpose: it is needed once a job can run.

DEPENDENCIES. B1 needs A1. B2 needs B1 and A4 (its calls register with the watchdog; A4 itself needs A2 and A3). B3 needs B2 and A2
(the pooled step path and the in-process `job_timeout_s`). B4 needs B3. Tier: CR-required (script function). Agent hints: `[mode:
plan] [model: opus] [effort: high]`. SIZE: none measured (READ: `/prep-pr` Step 1b's threshold is 800 lines or 10 files).

Acceptance criteria:

- [ ] An entry is published atomically, ordered by sequence number, replaced in place while queued; a second job for a running
      branch waits and is dropped when the head has not moved.
- [ ] Whoever enqueues starts the one detached runner or does nothing; no interleaving of enqueue and exit strands an entry; only an
      enqueuer or a `wait` touches `runner.lock`. A hand-typed `--drain-queue` or `--queue-worker` exits 2 having done nothing.
- [ ] From protocol 2 every evaluator counts a running job's worktree as busy and, while a dispatcher is alive and beating, a queued
      job as an entry; no evaluator but the dispatcher starts, rewrites or unlinks a job record; an A-era copy exits 2.
- [ ] Fix rounds outrank first pushes through the pool's `schedule()`; class and cost are derived outside `admit.lock`; a `gh` read
      failure or timeout never classifies a job; a bounce pins the cost.
- [ ] The dispatcher never signals a worker and no running job needs one: with the dispatcher SIGKILLed, a running job still ends at
      its own timeouts and writes its own result.
- [ ] Every external call is started held, in its own session, registered with its maker's watchdog and bounded by its maker:
      SIGKILL of a worker or of the dispatcher leaves none of its calls running (a descendant that starts its own session is the
      stated exception); an expired call's whole group is gone; output is read from files, never pipes.
- [ ] After a worker is SIGKILLed, nothing starts in its worktree (the requeued job, a hand-run gate, a named command) until its
      watchdog and every nested one are done; the claim is the pool's `.sweep` in `waiters/`, and `status --worktree` reports it.
- [ ] A worker holds only its ticket lock and slots, makes them non-inheritable first and holds them until exit; the dispatcher and
      every worker discard the inherited environment; no command-line-valued variable or credential reaches a queue file.
- [ ] A dead worker's job returns to its place with `attempts + 1` and ends `error` at three; `cancel` withdraws any job no process
      owns and never touches a held one; `stop` ends the dispatcher and starts nothing.
- [ ] `cleanup-worktree.sh` and the inline `/post-merge-cleanup` path proceed when the helper or the queue root is absent or the
      override is set, refuse (nothing touched) on exit 10, and refuse as undetermined on any other non-zero exit.
- [ ] An `add`, `cancel` or `stop` typed by anything but the lead's main thread draws a `STEER:` line and is never blocked.
- [ ] A malformed, mis-owned or hand-edited entry is refused before anything runs; every assertion is mutation-proven.

TEST PLANS. Every script PR follows the pool document's rules (pool section 9, "PR-level slicing"): stdlib, `ruff`, a new `test-*.py`
harness as a named `.gates.toml` step and in both lint lists, `HELPER_NAMES` with the #284 lockstep, every assertion MUTATION-PROVEN
on a copy, every harness pins `GATEQ_HOME` to a fresh temp directory, removes `GATEQ_HOLDER` from the child environment and never
runs the real gate. NO new bash file:
B4 edits two existing ones, `scripts/cleanup-worktree.sh` and `scripts/orchestrate-steer.sh`.

| PR | Test plan |
|---|---|
| B1 | `test-gate-enqueue.py`, `git` stubbed: atomic publish; replace in place keeps the name and the bypass count and drops the ticket; every field validator; `env_passthrough` refuses each denied pattern; no credential-named variable is snapshotted; ownership checks; `cancel` of a queued entry, of a held running record (refused) and of a free one (withdrawn); `status --worktree --quiet` returns 0, 10 and 2, a queued job claims with the queue on AND off (and with no readable config), and so does a held `.sweep`; an unknown `[queue]` key in `config.toml` exits 2, and `.gates.toml` `[pool] env_passthrough` is not read. Mutations: read `env_passthrough` from `[pool]`; accept a mismatched stem; let `GIT_SSH_COMMAND` into the snapshot; publish before the payload is fsynced; let `cancel` touch a held record; return 1 for "claimed"; read the config to decide whether a queued job claims |
| B2 | `test-gate-queue.py`, `gh` and `git` stubbed, a stub worker: a hand-run ticket does not start in a worktree whose job ticket is held; it counts queued jobs only while `runner.alive` is held and the beat is fresh; it never unlinks a job record; an A-era protocol exits 2; class derivation and its unreadable and timed-out paths; a cross-repository PR does not make a fix round; enqueue-versus-exit interleavings driven deterministically; a probe of `runner.alive` during an `add` strands nothing; the ticket is HELD before the worker process exists; a hand-typed `--drain-queue` exits 2 and leaves `runner.lock` free; the discarded environment; no `git` call under `admit.lock`; a bounded call's grandchild is gone after expiry; the dispatcher SIGKILLed mid-call leaves no call running; a call killed between `Popen` and the start byte runs nothing; dead-worker requeue keeps the name and the marker, and the 3-attempt cap holds; budget file removed with a worker alive; a call whose descendant starts its own session and keeps stdout returns at the leader's exit (K2); the queue is off without `[queue] enabled`; a batch of jobs committed in one pass never passes an entry beyond K; the doctor WARN. Mutations: recompute a job's `passed` list at commit; capture a call's output through a pipe; enqueue takes the lock BEFORE writing; skip the re-scan after release; treat a `gh` read failure as "no PR"; count a cross-repository PR; drop rule 1 of the pool extension; honor queued jobs with a stale beat; flock the ticket after the worker starts; send the start byte before `+pgid`; kill only the direct child; drop the timeout from one call; register a worker with the dispatcher's watchdog |
| B3 | Extends `test-gate-queue.py`: WITH A WORKER ALIVE, SIGKILL of the dispatcher leaves the worker running to its own result; a stuck gate step is ended by the worker at `job_timeout_s` with no dispatcher alive; SIGKILL of the worker leaves no step and no call running and frees its ticket even with a `close_fds=False` child alive; SIGTERM yields `error` `terminated` and is deferred while `admit.lock` is held (K1); a level-2 run under a worker does not wait on its ancestor; a hand-typed `--queue-worker` exits 2; cost-0 reuse, the bounce and its pinned cost; `after` with an unmoved head is `superseded` only when the predecessor ended `done` (a failed, cancelled or result-less one drops nothing); the worker's `.sweep` is in `waiters/` and locked by its watchdog before its first call; with a step group that ignores the first kill, SIGKILL of the worker leaves the requeued job and a same-worktree hand-run gate waiting until the watchdog is done, and the same with a nested run's watchdog. Mutations: start a call before the `.sweep` is locked; put the worker's `.sweep` where `busy` does not read; start the worker with a bare fork; pass `runner.lock` in the worker's `pass_fds`; `LOCK_UN` a handed descriptor; drop the `set_inheritable` call; have the worker close its ticket descriptor early; skip the environment discard; let a bounce keep cost 0; drop the predecessor-`done` test from `superseded` |
| B4 | `stop` with a worker alive leaves the worker running and `runner.lock` free; `stop` never touches `runner.lock`; the three status flags. `test-cleanup-worktree.py` gains: helper absent, root absent, override set (each proceeds); exit 0; exit 10 (refuses, nothing touched), including a real helper run against a QUEUED job with the queue turned off, which proceeds once the job is cancelled; exits 1, 2 and 127 (undetermined); the check runs after the cwd guard and before the run-directory capture. Mutations: let `stop` start a dispatcher; let `cleanup-worktree.sh` proceed on exit 2; refuse when the helper is absent; read exit 1 as "claimed"; print the override in the refusal. `test-orchestrate-steer.py` gains: `add`, `cancel` and `stop` with an `agent_type` each warn; the same three with none, and `status`, `enabled` and `wait` with one, are silent; a quoted or commented mention is silent; every case exits 0 with empty stdout. Mutations: warn with no `agent_type`; warn on `status`; exit non-zero on a match |

---

## Edits this unit needs in the pool document

Moved verbatim, with the review-5 additions, to `DESIGN-gate-queue-pool-edits.md` (one follow-up PR against the pool document).

---

## 11. Removed in fix round 3, and rejected

| Removed | Why it is not needed | What remains, and where |
|---|---|---|
| The dispatcher-side `job_timeout_s` enforcer | The gate is bounded in-process by the pool, like a hand-run gate; two timers on two clocks contradicted the pool | Section 4, "Every call is bounded" |
| `running/<job>.ctl` and the 15 s SIGKILL | Nothing outside a worker asks it to stop | An operator's SIGTERM to the worker (section 4) |
| The adoption clock (`granted_at`) | A worker needs no dispatcher, so nothing is adopted or re-timed | A held ticket is a live job |
| `stop`'s restart of a dispatcher; `wait`'s restart for a healthy running job | Their only purpose was to restore the outside timeout | `stop` only ends the dispatcher; `wait` starts one for a queued or dead-worker job (section 6) |
| The worker journal | Its fields were the timeout clock, the step for `.ctl`, and a holder record nobody flocked | The ticket is the holder record (section 2); ONE bit, `.pushing`, survives a dead worker (section 5, unit C) |
| Queue fields in `.sched`; the per-pass cost refresh | A pool evaluator's rewrite would drop them; a bounce corrects a wrong 0 and pins it | The job's ticket (section 2) |
| The real-path tool lookup | Pure cost once the permission checks were cut; a `brew upgrade` stranded a live dispatcher | `PATH = tool_dirs`, tools by name (section 3) |

REJECTED: a helper that chains gate, push and PR with no queue; `run <worktree> -- <cmd>` and `fix <script>` modes (one grant for them
launders every other rule); a cron job, a launchd agent or a designated lead as the runner (decision 8); `mkdir` locks with a
stale-age break; a bare `os.fork()` for workers; one environment for gate steps and the runner's own calls; a hand-typed
`--drain-queue` that takes the lock and serves; an outward call whose only bound is another process; folding the enqueue verbs into
`gate-runner.py` to avoid a grant (a second meaning for the #169 grant, which every implementer holds); ordering by timestamp; a
declared priority field; teaching the floor to read the queue; delivering results by SendMessage or Slack. CUT, not rejected
forever: `cancel` of a running job, an automatic stop of a hung dispatcher, retention pruning, log rotation.

---

## Appendix: what was run

Nothing was run in fix round 3. No gate, harness or commit at any time; Linux was not exercised. Experiments from earlier rounds and
from the reviewers that this document still relies on (Darwin, Python 3.14.8, stdlib and `sleep` only; scripts under the session's
`exp/`). The pool's experiments (F4b among them) are in the pool document.

| # | Where | Experiment | Observed |
|---|---|---|---|
| E2 | first draft | A `start_new_session=True` child of a Claude Code Bash call writes a file 6 s later | Written after the call returned: `ppid=1`, own session, no tty |
| F1a | fix 1 | A dispatcher flocks a slot and a queued file, renames the file into `running/`, starts a worker with `pass_fds`, closes its copies | `runner.lock`, the slot and the RENAMED file all HELD |
| F1b, F2 | fix 1 | SIGKILL the dispatcher (also while it holds `admit.lock`), then the worker | `runner.lock` and `admit.lock` FREE at once; slot and file HELD until the worker died |
| R1 | review 2 | An ordinary process flocks a file it wrote in `running/` and runs the worker's verification | ACCEPTED: the check guards accident only |
| R2, R6, G1 | review 2, fix 2 | A worker with a handed descriptor starts a `close_fds=False` child and dies; then the same with `set_inheritable(fd, False)` first | HELD by the child; with the fix, FREE at once |
| R3, R5 | review 2 | A live worker closes its handed descriptor; an uncaught Python error | The lock reads FREE while the worker lives; exit 1 |
| G2a, G2b | fix 2 | A stand-in `sh -c "sleep & sleep; wait"` past its bound: kill the direct child only; then own session and a group SIGTERM | The grandchild survived; with the group kill nothing was left, 1.3 s after a 1 s bound |
| G3b, G4 | fix 2 | A dispatcher stand-in started with no hand-off record; a process started with `GIT_CONFIG_*`, a token and a hostile `PATH` that clears `os.environ` and rebuilds it | Exit 2 with `runner.lock` FREE; its child saw only the rebuilt names |
| H2 | review 3 | A worker stand-in starts a call in its own session and is SIGKILLed | The call was ALIVE 1 s later: the defect the watchdog registration closes |

Read for fix round 3: `scripts/cleanup-worktree.sh` (the #448 block, the run-directory capture); `commands/post-merge-cleanup.md`
Step 3; `scripts/gate-runner.py` (`_parse_args`, `_run_gates`, the fallback chain); `required-permissions.md` (the deployed-helper
glob matches `.sh` only). Reasoned, not run: the held start for a list argv; a watchdog sweep of an outward call; the ticket as
holder record; the bounce; the protocol-2 extension of the pool's evaluators; sandbox inheritance by a detached child; a worker's `.sweep` claim in `waiters/` (added in the reconciliation).
