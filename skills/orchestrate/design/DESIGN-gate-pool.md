# Design: machine-wide weighted gate pool and named heavy commands (#538, unit A / #539)

Date: 2026-10-07
Status: DESIGN for unit A (#539), ready to build. This document is the pool half of the #538 design,
split out of `DESIGN-gate-queue.md` after two hostile review rounds found the pool sound, with only
mechanical and minor findings (all applied here). The maintainer decisions recorded on #538 (every
comment through 2026-10-07) are BINDING: this design follows them and does not reopen them. Defaults he adopted on
2026-10-07 are listed below and may be revisited by him. Nothing in this document is awaiting his
word (see "Open questions").
Issues: #538 is the EPIC; #539 is unit A, the subject of this document.
Companions: `DESIGN-gate-queue.md` (units B to D: the job queue, the dispatcher, the push and PR
tail; in progress), `DESIGN-fixround-push-gate.md` (the receipt leg in `safe-push.sh`),
`DESIGN-deterministic-floor.md` (threat model, what the floor denies).

Scope: `scripts/gate-runner.py`, one new sibling module (`scripts/gate_pool.py`), `.gates.toml`
gains one key and one table, and the pool-on prose in the commands and charters named in section 6.
NO deterministic-floor change. NO new allow-list entry. NO new schema. The pool makes NO commit of
any kind and writes nothing into any worktree.

Evidence labels used below: RUN = executed on this machine for this design (Darwin 27.0.0 and
27.0.1, Python 3.14.8; see the appendix), READ = read from the current source, REASONED = argued
from documented behavior and not executed, PRIOR = a report with no persisted query or data.
Linux was NOT exercised.

---

## Problem

Nothing budgets heavy work across worktrees and sessions on one machine.

- `scripts/gate-runner.py` takes no lock (READ: no `fcntl`/`flock` in the file), and `scripts/pre-push-hook.sh` ends in a bare `exec
  python3 "$runner"`.
- `--jobs N` (#501) bounds ONE process. `.gates.toml` sets `jobs = 4` and the hook inherits it, so N worktrees gating at once is up
  to 4N harness processes. `jobs` multiplies load, it never caps it.
- PRIOR: stillwater, 2026-10-02 around 16:25 PDT, overlapping gates on top of about ten agents drove load to roughly 150 and macOS
  OOM-killed the terminal and the SSH/signing agent. PRIOR: the same day two cc-orchestrator sessions were OOM-killed by parallel
  agents each gating. Neither has a surviving query; they motivate the work and justify no number in it.
- Heavy NON-gate commands (race, mutation and fuzz runs started by several hostile reviewers at once) recreate the same exhaustion
  outside gate-runner, so they join the pool in the same unit.

## Decisions this design is bound by

Numbered as in the epic (decisions 1, 2, 5, 7, 8 and 10 concern the queue and are in `DESIGN-gate-queue.md`).

3. Several gates run in parallel under a machine-wide WEIGHTED budget, in cost units.
4. BACKFILL: an entry whose cost fits the free budget starts now, ahead of an earlier entry that does not fit.
6. Fix rounds outrank first pushes; FIFO within each class.
9. A stale lock must not be able to break the pool.

Added by the maintainer's review of the first draft (2026-10-07), equally binding:

11. Named heavy NON-gate commands are in the FIRST delivery (this unit), run only by a name declared in `.gates.toml`. Several
    reviewers each running race or mutation checks would otherwise recreate the exhaustion outside gate-runner.
12. Small named checks form a THIRD, HIGHEST priority class. They are synchronous waiters, not persisted jobs.
13. `/prep-pr` KEEPS its inline Step 2 gate. It takes slots like any gate.

## Defaults adopted 2026-10-07 (the maintainer may revisit)

These were open questions in the first draft; this design treats them as decisions. The last two rows were adopted in the final
#538 comment of 2026-10-07.

| Default | Where it is specified |
|---|---|
| Starvation bound: a waiting entry passed K = 2 times is protected. K is the `config.toml` key `backfill_bypass_limit` | sections 2, 3 |
| A protected first push does NOT hold back fix rounds | section 2 |
| A gate that declares neither `weight` nor `jobs` runs alone (costs the whole budget) | section 3 |
| A hand-run gate and the pre-push hook wait in the first-push class and draw on the same budget | sections 2, 5 |
| The orphan watchdog ships as a PR under unit A | sections 4, 9 |
| Machine budget: 10 | section 3 |
| Top-class weight cap: `small_check_cap = 2` | sections 2, 3 |
| A protected gate of either class also holds back small (class-0) checks | section 2 |

Nothing in this document is WITHDRAWN. (The one default withdrawn during review, runner-made refresh merges, belongs to the queue.)

## Shape in one page

```
 hand-run gate / pre-push hook / --pool-run <name>
        |   computes its cost, takes a ticket, polls schedule() under admit.lock
        v
 waiters/<seq>-<id>.ticket   (flocked by its owner)  -->  slots/000..NNN.lock  (the budget: one flock per unit)
```

- THE POOL: `budget` slot files, one kernel `flock` per cost unit. Every gate on the machine holds `cost` of them for its whole run,
  whether run by hand or by the pre-push hook. So does every named heavy command (`--pool-run <name>`, a race or mutation check
  declared in `.gates.toml`), and the pool never runs two things in the same worktree at once.
- ONE POLICY FUNCTION, `schedule()`, evaluated under ONE short lock (`admit.lock`) by whoever is asking. In unit A its entries are
  TICKETS and each process starts only itself; it is defined over generic entries so that unit B's dispatcher can later feed queued
  jobs to the same function (the extension point; see `DESIGN-gate-queue.md`). There is one DEFINITION of the ordering rule; which
  COPY a process runs is not assumed (a hand-run gate may come from a repo or plugin leg), so every record carries a pool protocol
  number and a mismatch degrades to waiting (section 4).
- OFF BY DEFAULT: with no machine budget configured, `gate-runner.py` never imports the new module (`scripts/gate_pool.py`, stdlib
  Python 3.11+, deployed via `HELPER_NAMES`) and is byte-identical to today, and the commands take today's inline path.

---

## 1. Pool layout on disk

Root: `GATEQ_HOME`, default `~/.claude/gate-queue`, mode 0700, owned by the user. It sits beside `~/.claude/elmer` and
`~/.claude/orchestrate-feedback` on purpose. The directory is named for the whole #538 design; unit B adds its own subdirectories to
the same root, and unit A's code never reads or writes them.

REFINED from the proposed position ("a 0700 per-user cache directory, the `run-paths.sh` root"): `CC_RUN_ROOT` is
`<XDG_CACHE_HOME>/<repo-prefix>-run` (READ, `scripts/run-paths.sh`), which is PER REPO, so it cannot hold a MACHINE-wide pool. Never
`/tmp` (world-writable: squattable lock, symlinkable log).

```
~/.claude/gate-queue/                 0700
  config.toml                         machine budget; USER-owned, no tool ever writes it
  admit.lock                          flock target: serializes every directory mutation and admission scan
  seq                                 last allocated sequence number (read and written under admit.lock)
  slots/000.lock ... <budget-1>.lock  one per cost unit
  waiters/<seq>-<id>.ticket           a hand-run gate or named command, waiting or running (flocked).
                                      Written ONLY by its owner, in place; never replaced by anyone
  waiters/<seq>-<id>.sched            sidecar any evaluator may replace: {pool_protocol, bypass}
  tmp/                                staging for atomic writes (same filesystem, so rename is atomic)
```

Every file is 0600. A ticket holds `{pid, pool_protocol, kind, name, cost, worktree, state}` and nothing an evaluator must write.
`kind` is `gate` (hand-run or hook) or `named`; `state` is `waiting` or `running`. A HOLDER is a flocked ticket in state `running`
(a waiting ticket is flocked too); `busy` counts holders only. For a ticket, `cls()` reads `cost` as `e.weight`.

### Entry name

`<seq>-<id>`: `seq` is a 10-digit zero-padded integer allocated under `admit.lock` (next = 1 + max(the `seq` file, every seq visible
in `waiters/`)); `id` is `<UTC compact timestamp>-<6 hex>`, for humans and uniqueness, NEVER an ordering key. REFINED from "name led
by a UTC timestamp": a wall clock that steps backwards would sort a later entry ahead of an earlier one; a counter allocated under
the lock cannot. A lost or corrupt `seq` file is rebuilt from the visible maximum, and order only ever matters among entries that
coexist.

### Writes, and the one thing they must never touch

Every mutation of `waiters/` happens under `admit.lock`. An owner creates its ticket (staged in `tmp/`, fsynced, renamed in) and
flocks it inside ONE `admit.lock` critical section, so no evaluator can observe it unlocked (REASONED).

THE RULE: a file that some process holds a `flock` on is NEVER replaced, unlinked and re-created, or renamed over. RUN (reviewer's
B; appendix B): after `os.replace` over a flocked ticket a new opener saw the lock FREE, because the lock stays with the old inode,
and the next evaluator would have unlinked a live waiter as dead. So:

- a ticket is written only by its OWNER, in place (truncate and rewrite through the locked descriptor; RUN F4b: lock still HELD,
  same inode);
- anything ANOTHER process must record about a ticket (the bypass count) lives in a `.sched` sidecar, which nobody flocks and which
  is replaced atomically (RUN F4a: the ticket's lock stayed HELD).

---

## 2. Scheduling

### Classes and order

Three classes. Order = `(cls, seq)`; a lower class goes first, FIFO within a class.

| `cls` | Who | How it enters |
|---|---|---|
| 0 | a SMALL named check: `--pool-run <name>` whose declared `weight` is at or below `small_check_cap` | a synchronous ticket (section 5) |
| 1 | a fix round: a queued job whose branch has exactly one OPEN PR | a persisted job |
| 2 | a first push (a queued job with no open PR), a hand-run gate, the pre-push hook, and a named command HEAVIER than the cap (a full race suite) | a job, or a ticket |

In unit A the entries are TICKETS (hand-run gates, the pre-push hook, named commands), so class 0 and class 2 are populated and
class 1 is not: a fix round is a queued job, which unit B adds. `schedule()` is defined over generic entries and carries class 1
now, so unit B changes nothing in it; the harness exercises class 1 by feeding the pure function synthetic entries.

The class is DERIVED, never requested. For a named command the class comes from the `weight` declared in the repo's `.gates.toml`
compared with the machine's cap; there is no flag, variable or argument by which a caller can ask for class 0.

CLASS 0 IS DEFINED BY DECLARED WEIGHT ONLY, NEVER BY THE KIND OF CHECK. The scheduler does not know or ask whether a named command
is a mutation run, a race run or anything else. A single targeted race test declared at weight 2 is class 0 exactly like a mutation
check declared at weight 2; a full race suite declared at weight 8 is class 2 exactly like a full mutation sweep declared at weight
8.

`small_check_cap` is 2 (a `config.toml` key; ADOPTED 2026-10-07). The cap is the guard against the class being used to jump heavy
work: at budget 10 a class-0 check can never take more than a fifth of the machine, and a repo that declares a heavy suite at weight
2 to get priority is under-declaring its load, which is the same trust the `weight` of a gate already rests on.

### When the class matters at all

Backfill already starts a small job whenever its cost fits the free budget, whatever its class. So class 0 changes the outcome in
exactly ONE situation: the free budget is BELOW the check's cost when it arrives, and budget then frees up. At that moment the check
is served before gates that arrived earlier. In every other case backfill would have started it anyway.

Example, budget 10. Running: G1(4), G2(4), and m1, a mutation check (2), so 0 free. Waiting: F, a fix round (4, seq 15); C, a first
push (4, seq 18); r1, a single targeted race test declared at weight 2 (seq 30). G1 finishes, 4 free.

| | Who gets the 4 freed units | r1 waits for |
|---|---|---|
| Without class 0 | F (order F, C, r1); 0 left | the next finisher, possibly a whole gate |
| With class 0 | r1 takes 2 at once; F (4) no longer fits in the remaining 2 | nothing |

The price is real and stated: F is delayed until 2 more units free up (m1 or r1 finishing). It is recorded as a pass against F and
C, and the bound below limits how often that can happen. Had r1 been the repo's FULL race suite declared at weight 8, it would be
class 2 with seq 30: behind F and C, and waiting for 8 free units like any heavy gate.

### Cost before running

`cost = min(weight, budget)`. A ticket carries the cost its own process computed. THE COST IS COMPUTED OUTSIDE `admit.lock`: the
process parses `.gates.toml`, resolves `weight` and the effective `jobs`, resolves its worktree key (`git worktree list`), and only
then takes the lock to write the ticket, with the cost cached in it. A scheduling pass reads tickets and sidecars and try-locks slot
files; it runs no `git`, parses no TOML and makes no network call, so it does no work whose duration depends on the repository.
(Unit B's dispatcher derives the cost of a queued job on its own side of the lock for the same reason.) Unit A has no zero-cost
entry; `schedule()` keeps the `c == 0` branch for the queued jobs of unit B that need no budget.

### The policy function (precise pseudocode)

```
K   = backfill_bypass_limit      # 2, adopted by default 2026-10-07
CAP = small_check_cap            # 2, adopted 2026-10-07

def cls(e):
    if e.kind == "named": return 0 if e.weight <= CAP else 2
    if e.kind == "job":   return 1 if e.open_pr else 2
    return 2                               # hand-run gate, pre-push hook

def schedule(entries, free, budget, busy):
    """entries: every waiting job and ticket. busy: the worktree keys of everything running.
    Returns [(entry, passed)] to start now. Pure: reads nothing but its arguments.
    Called only under admit.lock."""
    order   = sorted(entries, key=lambda e: (cls(e), e.seq))
    claimed = set(busy)
    pend    = {}                           # passes this pass's own starts will record, by entry
    def prot(x):                           # PROTECTED: passed K times, counting this pass's starts
        return x.bypass + pend.get(x, 0) >= K
    def guard():                           # a protected gate (class 1 or 2) waiting on budget, not on its worktree
        return any(cls(w) > 0 and prot(w) and w.worktree not in claimed for w in order)
    start, blocked = [], []                # blocked = entries ahead in order that could not start
    for e in order:
        if e.worktree in claimed:          # PER-WORKTREE EXCLUSION: one thing per worktree.
            continue                       # Not eligible: neither started nor counted as blocked.
        c = min(e.cost, budget)            # a cost above the whole budget RUNS ALONE
        if c == 0:
            start.append((e, [])); claimed.add(e.worktree)   # no budget taken, never a pass
            continue
        if cls(e) == 0 and guard():
            continue                       # HOLD small checks while a protected gate waits
        if any(prot(b) for b in blocked):
            continue                       # HOLD: a protected entry is ahead; let the budget drain
        if c <= free:
            passed = list(blocked)                           # backfill past earlier entries
            if cls(e) == 0:                                  # priority past waiting gates
                passed += [w for w in order
                           if cls(w) > 0 and w.worktree not in claimed
                           and w.worktree != e.worktree     # e is about to claim it: w waits on the worktree
                           and min(w.cost, budget) > free - c]
            for b in passed: pend[b] = pend.get(b, 0) + 1    # so one pass cannot pass an entry beyond K
            start.append((e, passed)); free -= c; claimed.add(e.worktree)
        else:
            blocked.append(e)
    return start

def commit(e, passed):                     # by whoever actually starts e, still under admit.lock
    lock min(e.cost, budget) slot files (LOCK_NB; cannot fail: free was counted under this lock)
    record e.worktree and e.cost in e's own holder record (its ticket, or its running journal)
    for b in passed: b.bypass += 1         # persisted in b's .sched SIDECAR, never in a flocked file
```

PROTECTION COUNTS THE PASS'S OWN STARTS (PR #543 review). One pass can return several starts that each pass the same waiting
entry. Had protection been tested against the stored count alone, a pass over an entry at `bypass = 1` with four small entries
behind it would return all four and leave the count at 5, breaking the bound of K as soon as one evaluator commits a whole batch
(unit B's dispatcher). So `schedule()` keeps `pend`, the passes its own selections will record, and both HOLD tests use `prot()`.
In unit A an evaluator commits only itself, so this only makes it yield to starts ahead of it that have not polled yet. Both worked
tables and the example below were re-traced by hand against this version and come out the same (REASONED; `replay.py` not re-run).

Where its comments say "job" or "running journal" they mean unit B's queued jobs; in unit A the
only entries are tickets and the holder record is the ticket.

`free` is counted under `admit.lock` by try-locking every slot file below `budget` and releasing the ones not kept. `busy` is read
in the same critical section from the holder records whose flock is HELD (running tickets). The whole cost and the worktree claim
are taken ONCE, together, all or nothing, and nothing is ever acquired while holding either, so there is no hold-and-wait and no
deadlock. Releases happen at any time and only ever raise `free` or shrink `busy`.

Each evaluator acts only for what it owns: a ticket's process starts only itself. `commit` is therefore called ONLY for the
evaluator's own entry, in the SAME `admit.lock` critical section as the `schedule()` call that returned it, and it takes real slot
flocks there: that is why it cannot fail, and why no reservation is ever written to disk. Entries in `start` that belong to other
processes are NOT started and nothing is held for them; they only reduce `free` inside this one calculation, so the evaluator cannot
take budget the order gives to someone ahead. Every later pass recomputes from the live locks and tickets, so if the state changed
before an owner polled (a class-0 arrival, a new holder in its worktree) that owner simply gets the new answer. The budget and the
exclusion rest on held flocks alone, never on another pass's result (a ticket polls every 1 s with jitter). Two rules on what an
evaluator may touch:

- an evaluator schedules, rewrites and unlinks only records of ITS OWN pool protocol (section 4);
- a ticket of the evaluator's own protocol whose flock is free belongs to a dead process and is unlinked (with its sidecar) by the
  next evaluator. A ticket of ANOTHER protocol is never unlinked; section 4 says how a dead one is cleared.

Bypass counts are the only thing one process writes about another's entry, and they go to the `.sched` sidecar (section 1). Unit B's
dispatcher, when it exists, evaluates the same function over queued jobs plus live tickets; nothing in this section changes for it.


### Answers the issue asked for

- A fix round past a first push: always, by class order. That is priority, not backfill, and it is NOT counted as a pass (decision 6
  is strict; adopted default below).
- A first push past a blocked fix round: yes, by the same bounded backfill (it only uses budget the fix round cannot use), and it IS
  counted against that fix round's limit.
- A small named check past a waiting gate of either class: yes, by class order, and it IS counted.
- A job costing more than the whole budget: clamped to the budget, so it starts only when nothing is running and nothing else starts
  while it runs.
- Two or three at once: with budget 10, two `jobs = 4` gates run together (8) and a third waits; a gate declaring `weight = 3` runs
  three abreast (9); beside two cost-4 gates one weight-2 check still runs.

### Starvation bound (adopted by default 2026-10-07; the maintainer may revisit)

A per-entry BYPASS COUNT. An entry's count rises by one each time something starts that leaves it waiting: a later-ordered entry
backfilling past it, or a class-0 check taking budget while it waits. At `K = backfill_bypass_limit` (2) the entry is PROTECTED:

- nothing BEHIND it in order starts until it has started;
- a protected FIRST PUSH does NOT hold back fix rounds (they are ahead of it in order, and decision 6 keeps that strict). A stream
  of fix rounds can therefore still delay a first push; that is the accepted price of "fix rounds outrank first pushes";
- a protected gate of EITHER class DOES hold back class-0 checks (ADOPTED 2026-10-07). Class 0 is a courtesy for small work, not a
  precedence the maintainer ruled on the way he ruled on fix rounds, and it is the realistic source of a never-ending stream:
  several hostile reviewers each looping a mutation check.

Why a count and not an age: an age needs a clock and a threshold tuned to gate length, and gate length differs by an order of
magnitude between repos. A count is clock-free, is stored next to the entry, and bounds the thing that actually hurts (being
passed). No gate duration is claimed anywhere in this section: none was measured.

Worked numbers 1, backfill only. This repo's `jobs = 4`, budget 10. Running: A(4), B(4), so 2 free. Queue, all first pushes: H(8)
seq 10, C(4) seq 11, D(2) seq 12, E(4) seq 13, F(4) seq 14.

| Event | Free | Decision | bypass(H) |
|---|---|---|---|
| pass 1 | 2 | H blocked (8 > 2), C blocked (4 > 2), D(2) fits: BACKFILL | 1 |
| A finishes | 4 | H blocked, C(4) fits: BACKFILL | 2 = PROTECTED |
| B finishes | 4 | H blocked and protected: E and F are HELD although E fits | 2 |
| D, then C finish | 10 | H(8) starts | - |
| pass after | 2 | E, F wait for H or take the next free 4 | - |

Worked numbers 2, a stream of class-0 checks. Budget 10, K = 2, cap 2. Running: G1(4), G2(4), so 2 free. Waiting: H, a first-push
gate costing 8. Three reviewers, each in their own worktree, submit small checks one after another: m1 (a mutation check), r1 (a
single targeted race test), m2 (another mutation check), and so on, each declared at weight 2. The scheduler treats them
identically.

| Event | Free before | Decision | bypass(H) |
|---|---|---|---|
| m1 (mutation) arrives | 2 | m1 fits and starts; H (8) is left waiting | 1 |
| r1 (race test) arrives | 0 | r1 waits (class 0, first in order) | 1 |
| G1 finishes | 4 | r1 is served first and starts; H (8 > 2) is left waiting | 2 = PROTECTED |
| m2 (mutation) arrives | 2 | m2 FITS but is HELD: a protected gate is waiting | 2 |
| m1, r1, G2 finish | 10 | H(8) starts | - |
| next poll (under 1 s) | 2 | m2 starts beside H | - |

Without the hold, m2 and every later check keep at least 4 units in use whenever two overlap, H needs 8 free, and it never starts.
With it H waits for at most K passes plus the drain of what was running when it became protected, and a held check then runs BESIDE
the gate it waited for, because 8 + 2 fits in 10.

### Per-worktree exclusion

The pool never runs two things in the same worktree at once. A mutation harness rewrites tracked files in place; a gate running
beside it would test a mutated tree, and a receipt snapshot taken beside it would at best fail as dirty and at worst bind the wrong
thing. PRIOR: this repo has a recorded incident of a mutation being committed by a concurrent `git add`. So a named check waits for that
worktree's gate, and a gate waits for that worktree's check.

The exclusion applies to EVERY pooled command, not only to ones that rewrite files. The pool does not know which commands mutate,
and the read-only side is equally wrong: a race run reading a tree that a mutation check is rewriting tests code nobody wrote. So
two race tests in one worktree also run one after the other; reviewers who want them concurrent use separate worktrees, which is
already the one-worktree-per-agent rule.

- KEY: the RECORDED worktree path string, meaning the `worktree` line of `git worktree list --porcelain` that the process's
  directory resolves to (the `run-paths.sh` rule: resolution may PICK the record, only the recorded string is compared). A ticket
  stores that string in its `worktree` field. A directory that is not a registered worktree (a reviewer's private extract) is keyed
  by the root directory gate-runner resolved, so two different directories never exclude each other.
- WHERE IT LIVES: nowhere of its own. A holder writes its key into its ticket, and `busy` is the set of keys in holder records whose
  flock is HELD, plus the keys of held `.sweep` files (section 4, the orphan watchdog). A holder killed by SIGKILL drops its SLOTS
  with its lock at once; its WORKTREE stays claimed by the watchdog's `.sweep` lock until the orphaned step groups are gone (about
  a second), then frees. Both are kernel locks, so nothing can go stale. Before A4 lands there is no `.sweep` and the
  claim drops with the ticket lock, which is one reason the budget is not configured before A4 (section 9).
- NO DEADLOCK: slots and the worktree claim are taken in ONE critical section, all or nothing, and a process never waits while
  holding either. A waiter that cannot have its worktree holds nothing.
- NESTING: a holder sets `GATEQ_HOLDER` for its own children, so a gate step that itself calls `--pool-run`, or a pre-push hook
  fired by the holder's own push, runs under the holder's claim and slots instead of waiting on them. The exemption is EXACT: same
  worktree key, and the holder's held cost at or above the nested run's cost (section 5).
- ONE-PASS IMPRECISION, accepted: `guard` is computed from `busy` as it stands when the pass begins, and class 0 is evaluated
  first. If a protected gate's worktree is taken later in the SAME pass by an entry ahead of it (a fix round in that worktree),
  small checks were held on that pass for a gate that turned out to be worktree-blocked. The next pass sees the worktree in `busy`
  and releases them, so the cost is one poll (about a second) of delay for a small check, never a wrong start. Computing it exactly
  would need a look-ahead over classes 1 and 2 before class 0 is decided, which the plain single loop is kept free of.
- NOT STARVATION: an entry waiting on its worktree is not eligible, so it is not "blocked" and accrues no passes, and it holds
  nobody back.
- LIMIT, stated plainly: the exclusion covers only what runs THROUGH the pool. A mutation harness started as a raw command, or a
  lead editing files by hand, is invisible to it; the receipt's clean-before, clean-after and unchanged-tree checks remain the
  backstop there.
- A check that dies mid-mutation leaves the worktree dirty. The next gate in that worktree then writes a `dirty-before-run` fail
  receipt: loud, and never a pass on a mutated tree. Restoring the files is the owner's job (verify against `git show HEAD:<path>`,
  never the working file).


---

## 3. Cost and budget

### `.gates.toml`: `[prep_pr] weight`

```toml
[prep_pr]
jobs = 4
weight = 4        # optional; a positive integer. Machine cost units this gate occupies.
```

- Anything but a positive integer exits 2, like `jobs`. `bool` is refused.
- Default when absent but `jobs` is declared (Form B `jobs = N`, or `--jobs N` on a hand-run gate, which wins): the EFFECTIVE
  `jobs`. So this repo costs 4 with no edit.
- NO `weight` AND NO `jobs` (a Form A umbrella, the fallback chain, or a Form B table that declares neither): the whole budget, so
  it RUNS ALONE (adopted by default 2026-10-07). An undeclared cost is assumed heavy, and the repo opts into concurrency by
  declaring one number. This changes how many such gates run at once, never how one runs.
- `weight` is read by the runner, never by a step, and it changes nothing about how the gate itself runs. `exclusive` and `jobs`
  keep their in-process meaning.
- It is trusted repo config like `run`: a repo that under-declares only hurts the machine's load, never a verdict. Per-step tokens
  shared across gates stay ruled out (rejected alternatives).

### `.gates.toml`: `[[pool.command]]` (named heavy commands)

```toml
[[pool.command]]
name = "mutate-guard"      # [A-Za-z0-9][A-Za-z0-9_.-]{0,63}, unique in the file
run = "..."                # trusted config, like a step's `run`
weight = 2                 # REQUIRED positive integer; there is no default for a heavy command
timeout_s = 1800           # optional
```

Read only by `gate-runner.py --pool-run <name>` (section 5). A missing or invalid `weight`, a duplicated name, or a non-string `run`
exits 2 for that invocation and does not affect the gate. Neither the key nor the table is read by the floor.

How a process learns its own cost: it parses `<repo root>/.gates.toml` with `tomllib` before it takes a ticket (file read only), as
the runner already does to run the gate. A file that does not parse, or a `[prep_pr]` that is invalid, exits 2 as it does today.

### The machine budget

`~/.claude/gate-queue/config.toml`, written by the user, never by a tool:

```toml
[pool]
protocol = 1                   # the POOL protocol this directory speaks (section 4). REQUIRED
budget = 10                    # cost units. ABSENT, or the file absent = the feature is OFF.
backfill_bypass_limit = 2      # K, the starvation bound (section 2). Adopted default
small_check_cap = 2            # named commands at or below this weight are class 0. Adopted
wait_timeout_s = 3600          # a hand-run gate or named command gives up WAITING (exit 75)
job_timeout_s = 5400           # one RUN is killed after this: with the pool ON, a hand-run gate
                               # and a named command with no timeout_s
```

Unit B adds its own keys to this file (call timeouts, tool paths); unit A's reader ignores keys it does not define (ASSUMED; the
alternative, refusing unknown keys, would make a unit B config break a unit A runner). `protocol` and `budget` are the only required
keys; every other key, when absent, takes the value shown above. VALIDATION, one rule for every key this unit defines: `protocol`,
`budget`, `backfill_bypass_limit`, `small_check_cap`, `wait_timeout_s` and `job_timeout_s` must each be a POSITIVE INTEGER when
present (`bool` refused, zero refused, a string or float refused). A bad value exits 2
naming the key, exactly like a bad `budget`: `backfill_bypass_limit = 0` would protect every waiter at once and `job_timeout_s = 0`
would kill every run. These six MACHINE keys are validated in A1's config reader for every gate, including the keys A2 and A3
first use. The repo's `[[pool.command]]` table is a SEPARATE contract: its `weight` and `timeout_s` take the same positive-integer
rule, but they are validated by `--pool-run` alone (A3), and a bad entry there exits 2 for that invocation and never affects an
ordinary gate (the rule stated under the table above). A runner that finds a budget configured but no `gate_pool.py`
beside it exits 2 (`gate-runner: NOT RUN - pool configured but gate_pool.py is missing`): it must never fall back to running
unpooled.

- OFF means off: `gate-runner.py` reads only whether the file and key exist; when they do not, the pool module is never imported and
  output and exit codes are byte-identical to today. HOW THAT IS PROVEN (rewritten in fix round 1): not by "the existing harness
  passes unmodified". READ: `test-gate-runner.py` passes the caller's `HOME` and environment to the runner it spawns, so on a
  machine where the maintainer has configured the budget its fixture gates (no `weight`, no `jobs`: the whole budget) would take
  REAL tickets from the live pool, or run nested under the outer gate's holder. So the harness IS MODIFIED: every harness that
  spawns `gate-runner.py` pins `GATEQ_HOME` to a fresh empty temp directory and removes `GATEQ_HOLDER` from the child environment.
  Verified against the current tree: `test-gate-runner.py` is the only harness that executes the runner today
  (`test-ci-gates-lockstep.py` parses `ci.yml`, `test-orchestrate-steer.py` feeds command STRINGS to the steer hook,
  `test-finding-channel.py` names it in a comment), so it is the one existing file changed; the new harnesses (`test-gate-pool.py`,
  `test-gate-pool-run.py`, and those of units B and C) are born with the rule. The OFF proof itself is three dedicated cases in
  `test-gate-pool.py`: (a) with `GATEQ_HOME` empty, output and exit code of a fixture gate equal a recorded golden for Form A,
  serial Form B, parallel Form B and the fallback chain; (b) the same gates pass when `gate-runner.py` is copied ALONE into a temp
  directory with no `gate_pool.py` beside it, which is the no-import assertion; (c) no file is created under `GATEQ_HOME`. CI
  configures no budget, so CI always runs the OFF path.
- A present but malformed file, a non-positive `budget`, or (with a `budget` present) a `protocol` that is missing or differs from
  the evaluating code's exits 2 for every gate. A typo must never silently disable the bound it configures (the `elmer-tick.sh`
  `ELMER_LOCK_STALE_SECS` lesson), and code that speaks another protocol must not touch the directory at all.
- REFINED from "user-level config or environment": a FILE, not an environment variable. The budget must be one number for every
  session and for a pre-push hook that inherits one session's environment; two sessions exporting different values would disagree
  about how many slots exist. `GATEQ_HOME` (the root) is the one environment override, used by the harness exactly as `ELMER_HOME`
  is. LIMIT, stated plainly: an agent that sets `GATEQ_HOME` by hand to an empty directory runs its gate outside the pool. That is
  the same honest-actor hole as typing a heavy command raw (section 5), and closing it is the subject of the deferred enforcement phase.
- `budget = 10` is ADOPTED for this machine (2026-10-07; the maintainer may revisit). It is the issue's worked number for a 10-core,
  16 GB machine (a PRIOR from the review, not a tuned value). Recorded wall time stays tuning input only; live load average and free
  memory are never admission signals.

### When the budget file is changed or removed

Two rules, and no larger table.

- REMOVED (the file, or its `budget` key): a process STARTED after the removal runs as with the pool off. Holders keep their slots
  until they exit. A ticket already WAITING never re-reads the config: it keeps waiting against the slot files and the other live
  tickets, starts when its cost fits, or exits 75 at `wait_timeout_s`. It never converts to an unpooled run. Unpooled newcomers hold
  no slot and no worktree claim, so it cannot see them. Removing the file is the supported way to turn the pool off in a hurry; it
  never kills anything. LIMIT, stated plainly: from the removal until the last pooled holder exits, a newcomer is neither budgeted
  NOR excluded, so it can run in a worktree where a pooled gate or mutation check is still running. That is today's behavior
  (nothing excludes anything with the pool off) and the receipt's clean-before, clean-after and unchanged-tree checks are the
  backstop. Keeping claims alive through the drain was rejected: it needs newcomers to keep reading a pool the user just switched
  off. To turn the pool off WITHOUT that window, wait until `waiters/` is empty, then remove the file.
- CHANGED (`budget` raised or lowered, `K`, the cap, a timeout): the file is read at each process start. A process that is already
  running or waiting keeps the values it read, so the pool may be briefly over a lowered figure or under a raised one while older
  processes exit. That costs load or fairness, never correctness: slot flocks, not the config, are what bound the units actually
  held. A slot index at or above a RAISED budget is created by the first evaluator that reads the new figure, under `admit.lock`. A
  file made MALFORMED is the config rule above: every new gate exits 2.

---

## 4. Locks, the pool protocol, and the pooled step path

### Locks

CONFIRMED: `fcntl.flock` on open descriptors in the 0700 directory, no stale-age break, no pid probe. A stale lock is not
representable (decision 9): `admit.lock`, slot files and tickets are released by the kernel when the LAST process holding that open
file description dies.

That last clause is the point, so it is stated as rules rather than left implied:

1. NO LOCK DESCRIPTOR IS EVER INHERITED BY ACCIDENT. Every lock file is opened with Python's default non-inheritable flag, and every
   child process the pool starts is started with `subprocess.Popen` (exec, `close_fds=True`). A bare `os.fork()` copies every
   descriptor regardless of that flag (the flag acts only at exec): RUN (reviewer's A; appendix A) a forked child that never touched
   the descriptor kept a lock HELD after its parent was SIGKILLed. So there is NO bare fork in `gate_pool.py`, and a harness case
   greps for one.
2. CLOSE, NEVER `LOCK_UN`, on a descriptor a process shares or has given away. RUN (reviewer's C; appendix C): a child's `LOCK_UN`
   on an inherited descriptor freed the PARENT's lock, because the lock belongs to the open file description both share. A process
   unlocks only a lock nobody else holds a copy of.
3. A FLOCKED FILE IS NEVER REPLACED (section 1).

| Lock | Holder | Held for |
|---|---|---|
| `admit.lock` | any evaluator | one scheduling pass: reads of tickets and sidecars, try-locks of slot files, and writes of the evaluator's own ticket. Never `git`, TOML parsing, network or a gate (section 2, "Cost before running") |
| `slots/NNN.lock` | a hand-run gate or named command | one run |
| `waiters/<ticket>` | a hand-run gate or named command | wait plus run |

Lock order is fixed: `admit.lock`, then slot files and ticket files with `LOCK_NB` only. Nothing blocks on a slot or a ticket, so no
cycle exists. RUN (E1c): a second `open()` of a locked file in the SAME process conflicts (flock belongs to the open file
description), which is what lets the harness test contention in one process.

### Pool protocol

"One policy function" is one definition, not one running copy. READ: a hand-run gate comes from whichever leg its caller resolved:
`commands/prep-pr.md` prefers the REPO copy inside cc-orchestrator, and `scripts/pre-push-hook.sh` tries the plugin, then its own
directory, then the deployed copy. So a cc-orchestrator branch that edits `gate_pool.py` evaluates ITS `schedule()` against the live
machine pool, and an old plugin cache can meet a newer deployed runner.

`gate_pool.py` therefore defines `POOL_PROTOCOL = 1`, and it is written into every ticket, every `.sched` sidecar and (by the user)
`config.toml`. It covers the on-disk layout of `waiters/` and `.sched` and the meaning of `schedule()`. What is FROZEN below every
protocol, and may never change: `admit.lock`, the `seq` file, the slot files, the ticket fields `pool_protocol`, `worktree` and
`cost`, and "a held flock means a live owner". The rules:

- CONFIG MISMATCH: code whose `POOL_PROTOCOL` differs from `config.toml`'s `protocol` exits 2 for a gate (`gate-runner: NOT RUN -
  pool protocol <mine> != configured <theirs>; update the plugin and run configure --apply`) and touches nothing in the directory. A
  protocol bump therefore needs the user's own edit of `config.toml`, which is the point: a branch cannot move the machine to its
  protocol by being run.
- RECORD MISMATCH (records written before such an edit, or by a copy that is simply older): an evaluator NEVER schedules, rewrites
  or unlinks a record of another protocol, not even one whose flock is free. It treats a foreign holder's slots and worktree as
  HELD: its slots are held anyway (slot flocks are below the protocol and `free` is counted by try-lock), and its worktree key is
  read from the frozen holder fields and added to `busy` while its flock is held.
- A FOREIGN TICKET WHOSE LOCK IS FREE belongs to a dead process (a held lock is the only sign of a live owner). The evaluator still
  never touches it, so the pool needs a clearing path that adds no command: `orchestrate-setup.py doctor` WARNs naming the file (and
  its sidecar), and the user deletes it. Doctor probes under `admit.lock` (a bare try-lock can beat a new owner to its ticket), and
  foreign means differing from `config.toml`'s `protocol`. Until then it holds no slot (its flock is free) and no worktree claim, because `busy`
  counts only records whose flock is HELD. A foreign ticket whose lock is HELD is a live process: it is never touched and counts as
  held.
- FOREIGN WAITERS: an evaluator does not start while any LIVE foreign ticket (flock held) with a LOWER seq exists in `waiters/`,
  and ignores foreign tickets with a higher seq. Wait-for edges then only point at lower sequence numbers, so two protocols cannot
  deadlock on each other, and the budget is never exceeded because real slot flocks are still required. This is conservative on
  purpose (a mixed pool degrades toward one-at-a-time) and lasts until the older processes exit.
- A COPY THAT PREDATES THE POOL knows no protocol, no `GATEQ_HOME` and no `config.toml`: it runs an ordinary gate UNPOOLED and
  nothing in its output says so. No protocol number can catch code that never reads one. So ACTIVATION HAS A PRECONDITION: the
  budget is written only after `orchestrate-setup.py doctor` reports every copy it can see as pool-capable. With a budget
  configured, doctor WARNs naming each `gate-runner.py` on the deployed leg and in the plugin cache that lacks a `gate_pool.py`
  beside it or does not import it (a text check; read-only). LIMIT, stated plainly: doctor cannot see a cc-orchestrator WORKTREE
  whose branch predates A1 (the repo leg wins there). Such a gate is unbudgeted and unexcluded until the branch is refreshed, which
  is the same exposure as a raw heavy command (section 7).
- THE LIMIT, stated plainly: a number catches an honest version change, not an edit that keeps the number. A comment beside
  `POOL_PROTOCOL` says to bump it when ordering or layout semantics change; nothing enforces that. (A harness pin on a hash of the
  policy source was considered and CUT: it is test churn on every refactor, for an honest-actor limit.) A same-number edit can start
  ITS OWN ticket out of turn, miscount a bypass, or unlink a ticket it wrongly believes dead. It cannot take slots that are held, so
  the budget itself holds.

### Pooled serial step path

READ: only the PARALLEL path starts each step in its own session (`start_new_session=True` in `run_form_b_parallel`). The SERIAL
path (Form A, serial Form B, the fallback chain) is `subprocess.run(command, shell=True)` in `_run_command`: the step shares the
runner's process group and there is no pgid to register, sweep or time out. The treatment, decided:

- POOL OFF: the serial path is untouched and byte-identical. No session, no group, no sweep.
- POOL ON: a serial step is started with `Popen(..., shell=True, process_group=0)`: its OWN PROCESS GROUP, deliberately NOT a new
  session, so it keeps the runner's controlling terminal and its output still streams as today. The runner waits on it with the
  remaining `job_timeout_s` as the bound, registers the pgid with the watchdog (below), and on every exit path runs the same
  SIGTERM-then-SIGKILL sweep the parallel path has.
- CTRL-C. Today a terminal interrupt reaches the serial step because it is in the foreground process group. In its own group it
  would not, so the runner FORWARDS: on SIGINT, SIGTERM, SIGHUP or SIGQUIT it sends the same signal to the step's group, waits for
  the step, then exits exactly as the serial path does today for that signal (a harness case compares the two exit statuses and
  final lines, pool on against pool off). SIGQUIT is forwarded because today a terminal Ctrl-\ reaches the step in the foreground
  group, so leaving it out would be a behavior change. RUN F5: a forwarded SIGINT ended the step's shell and its foreground child; a
  BACKGROUND child of the step survived it (a non-interactive shell starts background jobs with SIGINT ignored, which is equally
  true today) and was gone after the SIGTERM sweep. So the pool-on serial path cleans up strictly more than today's.
- SIGTSTP IS NOT FORWARDED, a STATED DIFFERENCE. A terminal Ctrl-Z stops the runner (it is in the foreground group) and not the
  step, which keeps running and keeps its slots until the runner is continued or killed; while stopped, the runner cannot enforce
  its timeout. Forwarding it would need the runner to stop and resume its step group in step with itself (SIGCONT included), more
  machinery than the case earns: agents run the gate in the background (section 6). Nothing in the pool depends on it.
- STATED DIFFERENCE: a serial step that reads the terminal interactively is not supported with the pool on. A background process
  group that reads its controlling terminal is stopped by the kernel (SIGTTIN), so the runner gives a pooled serial step `/dev/null`
  as stdin and such a step fails at once instead of hanging while it holds slots. The parallel path already does this. THIS IS NOT
  A COMPLETE GUARD: a step that calls terminal control from its own group (`tcsetattr`, for instance) can be stopped by SIGTTOU and
  stays stopped until `job_timeout_s` ends it with the SIGKILL leg of the sweep. Stdin from `/dev/null` closes only reads of stdin;
  a step that opens `/dev/tty` (a credential or passphrase prompt) is stopped by SIGTTIN the same way. No further mitigation is
  attempted (REASONED, not run).
- DESCRIPTOR INHERITANCE, decided on purpose: slot descriptors are NON-inheritable, so the slots free the instant the holding gate
  process dies. The alternative (steps inherit the slot, so it stays held until every orphan exits) was rejected: one step that
  leaves a daemon behind would hold budget forever, which is the stale-lock wedge decision 9 forbids.

### The hand-run timeout

A HAND-RUN GATE AND A NAMED COMMAND, with the pool ON, are bounded by themselves: the gate by `job_timeout_s`, a named command by
its `timeout_s` or else `job_timeout_s`. They time their steps through the same registered groups and end with exit 1 and
`gate-runner: KILLED - exceeded job_timeout_s` plus a fail receipt (a gate), or `POOL-RUN: <name> ran exit=124` (a named command).
With the pool OFF there is no run timeout, as today. The timeout is enforced by the runner process itself, which is alive while it
waits on its step, so it does not depend on the watchdog.

### The orphan watchdog

The cost of non-inheritable slot descriptors is that a SIGKILLed gate leaves its step groups running with the budget already free.
The fix, ADOPTED as its own PR under unit A: a DEATH-PIPE WATCHDOG. The gate process starts one tiny `python3 -c` child in its own
session whose stdin is a pipe; it sends `+<pgid>` when a step group starts and `-<pgid>` when it empties; on EOF (the gate process
is gone, SIGKILL included) the watchdog SIGKILLs the groups still listed. RUN (E3): after SIGKILL of the parent, a registered `sleep
& sleep` group was gone within one second.

- LAUNCH IS HELD UNTIL REGISTERED. `Popen` creates the group before the runner can name it, so a runner killed in between would
  leave an unregistered step running. Every watched step therefore starts HELD: its shell first reads one byte from a start pipe.
  The runner writes `+<pgid>` to the watchdog and only then writes the start byte. A step whose start pipe reaches EOF (the runner
  died first) exits without running anything. The `+<pgid>` is already in the watchdog's pipe when the byte is sent, so the
  watchdog reads it before it can see EOF; no acknowledgement round trip is needed (REASONED, not run; a harness case in A4 kills
  the runner between `Popen` and the start byte).
- PRUNE ORDER. A pgid cannot be reused while its leader is an unreaped child or any member is alive. The runner sweeps the group,
  then writes `-<pgid>`, as the FIRST thing after reaping the leader. The watchdog never signals a pruned pgid. RESIDUAL, stated
  plainly: a runner SIGKILLed in the instants between that reap and that write leaves a listed pgid whose number is free; harm
  needs the kernel to hand that exact number to a new group before the watchdog reads EOF (milliseconds). The earlier text called
  this "safe against pid reuse"; it is narrow, not closed. WHERE `os.waitid` EXISTS the window is closed instead: the runner learns
  of the leader's exit with `WNOWAIT` (the leader stays an unreaped zombie, so its number cannot be reused), sweeps the group,
  writes `-<pgid>`, and only then reaps. That is every Linux, and macOS from Python 3.13 (PRIOR, from the Python docs; not run).
  On an older macOS Python the order above applies and the residual stands.
- THE WORKTREE STAYS CLAIMED UNTIL THE SWEEP IS DONE (PR #543 review). A SIGKILLed holder's ticket lock frees at once, about a
  second before the watchdog has killed its step groups, and a waiter must not start in that worktree beside an orphaned mutation
  step. So with the pool on, a holder creates `waiters/<ticket name>.sweep` (`{pool_protocol, worktree}`) in the same `admit.lock`
  section as its commit and sends the path to its watchdog over the pipe. The WATCHDOG opens that file itself and holds an
  exclusive `flock` on it (its own open, nothing inherited, so rule 1 of the locks section holds); the holder starts its first step
  only once a try-lock on the file fails. `busy` is the worktree keys of held tickets PLUS held `.sweep` files. On EOF the watchdog
  kills the listed groups, waits until EVERY one is gone (signal 0 to the group fails), re-sending SIGKILL each second, and only
  then exits, which frees the claim. There is NO time limit (PR #543 review: a limit would admit the next gate beside a live
  orphan). A group the kernel will not kill (a process stuck in an uninterruptible call) therefore keeps that ONE worktree claimed
  for as long as it lives; other worktrees are unaffected, a waiter there prints `waiting for this worktree (held by sweep pid
  <n>)`, and killing that watchdog pid by hand releases it. This is a live lock held for a live process, not a stale one. After a
  normal exit the list is empty and it exits at once. A `.sweep` whose lock is free is unlinked by the next
  evaluator with its ticket. NOT COVERED, stated plainly: SLOTS are not held through the sweep (they free with the holder), so for
  about a second the machine can run an orphan's load on top of a full budget; and a watchdog killed together with its holder
  claims nothing. REASONED, not run; A4 adds both sides (the watchdog's lock and the `busy` reader) and the test.

The watchdog is started with `Popen` and `close_fds=True` like every other child, so it INHERITS no lock descriptor. The one lock
it holds is the `.sweep` file it opened itself (above); it never holds a slot, a ticket or `admit.lock`. It covers the
parallel step path, the pooled serial step path and `--pool-run`; the pool-off serial path has no group and stays untouched. The
watchdog lives in `gate-runner.py`, not `gate_pool.py`: A4's harness runs pool-off, where the module is never imported.

---

## 5. Direct callers: hand-run gates and named heavy commands

YES: a hand-run `python3 scripts/gate-runner.py`, the pre-push hook, and every named heavy command take slots from the SAME budget
when it is configured. Anything heavy that did not count against the pool would be the hole the pool exists to close. With no budget
configured they are untouched.

### How a ticket waits

1. After the config parse (cost needs `weight` and the effective `jobs`), the process writes a ticket `waiters/<seq>-<id>.ticket`
   (`{pid, pool_protocol, kind, name, cost, worktree, state}`), flocks it, and polls every 1 s (jittered). The bypass count is NOT
   in the ticket; it lives in the ticket's `.sched` sidecar, which other evaluators replace (section 1). Each poll, under
   `admit.lock`, it runs the SAME `schedule()` over live tickets. It starts only itself, and changes its own `state` by rewriting
   its ticket in place.
2. It prints one stderr line when it starts waiting and one every 30 s: `gate-runner: waiting for 4 of 10 gate slots (2 free, 3
   ahead)`, or `gate-runner: waiting for this worktree (held by <kind> pid <n>)`.
3. The receipt snapshot (`_snapshot`) is taken AFTER the grant, immediately before the first step, so a wait never widens the window
   the receipt attests, and the per-worktree exclusion guarantees no pooled check is rewriting the tree at that moment. After the
   grant the runner re-reads `.gates.toml` and runs THAT definition (today's order: snapshot, then parse). The held cost stays the
   ticket's; a `weight` or `jobs` edited during the wait is mis-costed for that one run (load, never a verdict).
4. After `wait_timeout_s` it gives up: exit 75 (`EX_TEMPFAIL`), `gate-runner: NOT RUN - no gate slot within 3600s`. 75 is none of 0
   (pass), 1 (gate failed), 2 (config) or 130 (interrupted), so a caller can never read "did not run" as "failed" or as "passed".
   With `--receipt`, an older receipt at the path is unlinked, as for every other run that produced none (#497).
5. A hand-run gate and the pre-push hook are class 2, the first-push class (adopted by default 2026-10-07). The ticket's sidecar
   carries its bypass count, so a heavy hand-run gate gets the same starvation protection as a queued one.
6. Once running, it is bounded by `job_timeout_s` (section 4). With the pool on its serial steps run in their own process group,
   with interrupts forwarded (section 4).

The hook is `exec python3 "$runner"` and needs no edit: it inherits all of this, and a timeout blocks the push with a message that
says the gate did not run. An agent runs a waiting gate in the background; the command prose that says so ships with the pool
(section 6).

### Named heavy commands (in the first delivery)

The maintainer's concern, 2026-10-07: several hostile reviewers each running race or mutation checks recreate the original
exhaustion, because those are not gate-runner runs. So declared heavy NON-gate commands join the pool in unit A, not later. This
settles the issue's third acceptance criterion.

Declared in the repo's `.gates.toml` (schema in section 3):

```toml
[[pool.command]]
name = "mutate-guard"            # the only thing a caller ever passes
run = "<the repo's own command>" # trusted config, like a step's `run`
weight = 2                       # REQUIRED. At or below small_check_cap = class 0; above = class 2

[[pool.command]]
name = "race-one-pkg"            # a single targeted race test: also weight 2, so also class 0
run = "<the repo's own command>"
weight = 2

[[pool.command]]
name = "race-full"               # the full race suite: above the cap, so it waits in class 2
run = "<the repo's own command>"
weight = 8
```

The names and weights are illustrative; the class follows from the weight alone, never from what the command does.

Run as `python3 <leg>/gate-runner.py --pool-run <name>`:

- `<name>` must match `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}` and exactly one `[[pool.command]]` of the `.gates.toml` at the resolved repo
  root. Unknown or duplicated: exit 2, nothing run.
- NO further argument is accepted (exit 2), and none is forwarded. A variant (another target, another profile) is another declared
  name. This is what keeps it "by name, never as argv".
- Not combinable with `--receipt`, `--skip`, `--jobs` or `--memoize-dir` (exit 2). It never writes or touches a receipt: a named
  check attests nothing about a tree.
- It takes a ticket exactly as above, with `kind = "named"`, cost `min(weight, budget)`, the class derived from the weight, and the
  worktree claim. Then it runs `run` with `shell=True` in the repo root (the documented trusted-config path), in its own session,
  with the same SIGTERM-then-SIGKILL sweep and the same watchdog as a gate step.
- Exit code: the command's own. The pool's own outcomes are 75 (no slot or no worktree within `wait_timeout_s`) and 2 (usage or
  config). Because a command may itself exit 75 or 2, the runner ends with ONE stderr line that disambiguates: `POOL-RUN: <name> ran
  exit=<n>` or `POOL-RUN: <name> NOT RUN reason=<slug>`. A caller that must tell them apart reads that line.
- AN ABSENT `POOL-RUN:` LINE MEANS THE NAMED COMMAND DID NOT RUN, whatever the exit code. READ (`_parse_args`): a runner that
  predates this flag warns `unrecognized argument '--pool-run'`, ignores it and the name, and runs the repo's FULL GATE. A stale
  deployed or plugin-cache copy would therefore answer a request for a 2-unit mutation check with an unpooled full gate and exit
  0. So the charter rule and every caller treat "no `POOL-RUN:` line" as NOT RUN and report it; a pass is only ever `POOL-RUN:
     <name> ran exit=0`.
- THE LINE DETECTS A STALE RUNNER ONLY AFTER ITS FULL GATE HAS RUN, so callers also check BEFORE: a leg is used for `--pool-run`
  only if its `gate-runner.py` contains the flag (`grep -q -- '--pool-run' <the literal leg path>`, beside the usual `[ -f ]`
  test). A leg that fails is skipped like a missing file, and with no capable leg the caller reports NOT RUN and starts nothing.
  The doctor WARN of section 4 names the same stale copies.
- An optional `timeout_s` per command kills the group and reports `ran exit=124`. Absent, with the pool ON the bound is
  `job_timeout_s`; with the pool OFF there is no run timeout (as for a hand-run gate today).
- POOL OFF: `--pool-run <name>` still resolves the name and runs the command, with no ticket and no wait. So a charter can say
  "always use the name" on every machine, and turning the budget on later changes nothing an agent types.
- It is a WAITER, not a persisted job: no result record, nothing outward. If its process dies, its ticket's lock frees and the
  request is simply gone.

### Charter prose (lands with the flag, PR A3)

One rule, added to `skills/orchestrate/templates/implementer-charter.md`, `adversarial-review-charter.md`,
`adversarial-prep-charter.md` and to the machine-resource bullet at SKILL.md line 236 (the pool half; unit D extends that bullet for
the queue):

> A heavy check (a race or sanitizer run, a mutation run, a fuzz or accessibility suite, a container build: anything that runs the
> toolchain for more than a few seconds or rewrites files in place) is run ONLY through a name declared in the repo's `.gates.toml`
> (`gate-runner.py --pool-run <name>`), in the background, from a runner copy that contains `--pool-run`. It ran only if the output ends with `POOL-RUN: <name> ran exit=<n>`; no
> such line means it did NOT run, whatever the exit code. If the check you need has no declared name, report that to the lead
> instead of running it raw; declaring one is a reviewed edit to trusted config.

This does not reverse `implementer-charter.md` line 44 (the implementer still runs the FULL gate) or the adversarial-prep role; it
says how heavy work is started, not who may start it. The "at most two hostile reviewers" prose in SKILL.md stays until the pool has
run for a while; the pool makes it a backstop instead of the only control.

THE LIMIT, stated plainly: this is prose plus a mechanism for those who follow it. An UNDECLARED raw command (a reviewer typing the
race suite directly) is still unbudgeted and still unexcluded from its worktree. Nothing in the first delivery detects or blocks it.
Closing that gap mechanically is the deferred enforcement phase's subject (`DESIGN-gate-queue.md`).

### Nested runs, the one hold-and-wait trap

Every holder (a hand-run gate or a named command; unit B's workers will be holders too) sets `GATEQ_HOLDER=<path of its own ticket
or running entry>` in the environment of the children it starts. Three cases need it:

- a holder's own push ends in an upload, and an installed pre-push hook starts a SECOND gate inside the first one's slots (a pooled
  gate that pushes, or unit B's worker pushing a queued job). If it queued for its own, a gate costing more than half the budget
  would wait on itself until the timeout;
- a gate step that itself calls `--pool-run <name>` would wait forever for the worktree its own gate holds;
- a named command that runs the gate as part of its work, likewise.

THE EXEMPTION IS EXACT (fix round 1): a variable that merely names a held lock must not exempt an unrelated gate from budget and
exclusion, nor skip the accounting when the nested run costs more than its holder. A nested run skips acquisition ONLY when ALL of
these hold:

1. `GATEQ_HOLDER` names a file directly inside this pool's `waiters/` (or, from unit B, `running/`) whose flock is HELD and whose
   pool protocol is the caller's;
2. the holder's recorded WORKTREE KEY equals the caller's own key (the same string comparison the exclusion uses);
3. the holder's recorded held COST is at or above the nested run's cost (`min(cost, budget)`).

ONE NESTED RUN PER HOLDER AT A TIME. Condition 3 alone lets two children of a cost-4 holder each start a cost-4 nested run, 8 units
under 4 slots. So a nested run that passes all three conditions then takes an exclusive `flock` on `<holder ticket>.nest` (a
sidecar nobody else locks) for its whole run. A second nested run under the same holder WAITS on that lock, holding nothing, and
exits 75 at `wait_timeout_s`. The kernel frees the lock when its holder dies.

DEEPER NESTING IS A CHAIN, NEVER A TREE (PR #543 review: a blanket exemption for descendants would let one nested run fan out two
concurrent children). Every nested run owns ONE child lock of its own: the path of the lock it holds, plus `.nest`. It sets
`GATEQ_NEST=<the lock it holds>` for its children. A run nested under it does not skip locking: it takes the exclusive `flock` on
`<GATEQ_NEST>.nest`, its IMMEDIATE parent's child lock, for its whole run. So a child never waits on an ancestor (it locks one level
down, which only its siblings contend for), and two siblings at any depth run one after the other. `GATEQ_NEST` is honored only
when it names a file in the SAME directory as the holder's record (`waiters/`, or from unit B `running/`, matching condition 1
above) whose name begins with the holder's ticket name and whose flock is HELD; otherwise
it is ignored and the run takes the holder's own `.nest` like a first-level run. Every lock in the chain is released by the kernel
on death. CLEANUP NEVER UNLINKS A HELD LOCK (the rule of section 1): an evaluator that unlinks a dead ticket try-locks each of that
ticket's `.nest` files and unlinks only the free ones. One that is still held belongs to a nested run that outlived its holder for
a moment; it is left in place, and any later evaluator unlinks a `.nest` file whose lock is free and whose ticket is gone.

WHAT THIS BOUNDS, AND WHAT IT DOES NOT. Real load under one holder is at most its own steps plus ONE nested run PER NESTING LEVEL,
each no heavier than the holder; the depth is whatever the repo's own declared commands nest, normally one. A gate whose parallel steps keep running beside a nested named command therefore uses up to `cost + nested cost` while
holding `cost`. That is not charged: charging it would mean acquiring while holding, the one hold-and-wait this design forbids. A
repo that nests declares a `weight` that covers it, the same trust `weight` already rests on. The hook inside a holder's push is
not this case: the holder is blocked in the push while the hook's gate runs, so the two never overlap.

Otherwise:

| What fails | The nested run does | Why |
|---|---|---|
| condition 1 (missing, unlocked, foreign, outside the pool) | ignores the variable and takes a ticket like anyone | a stale or forged value buys nothing |
| condition 2 (another worktree) | ignores the variable, takes a ticket, and prints one stderr line naming the leak | it is an unrelated gate; it must be budgeted and excluded on its own |
| condition 3 (same worktree, holder holds LESS) | exits 75 AT ONCE: `NOT RUN reason=nested-over-holder holder=<n> needs=<m>` | it cannot wait: its own parent holds the worktree, so a wait would end only at `wait_timeout_s`. And it must not run: the difference would be unbudgeted |

How the three cases come out under that rule:

- THE HOOK INSIDE A HOLDER'S PUSH. The holder holds the gate's `weight` and the hook's gate costs the same `weight` in the same
  worktree, so it satisfies all three conditions: "the gate inside the push is paid for" and "it does not wait on itself" are both
  true. (Unit B's cost rule makes a queued push in a hook-installed repo hold that weight for the same reason.)
- `--pool-run` INSIDE A GATE STEP: the gate holds `weight`; the named command's weight must be at or below it. A repo that declares
  a step calling a HEAVIER named command gets the fast 75 and a message that says which number to raise.
- A NAMED COMMAND THAT RUNS THE GATE: its declared `weight` must be at or above the gate's cost, which is the honest declaration
  anyway (it runs the whole gate). A weight-2 command wrapping a cost-4 gate is refused at once instead of running 4 units on a
  2-unit ticket.

WHERE THE VARIABLE MAY COME FROM. Only from the holder that started this process, set on that child's environment from the holder's
own state. It is never read from a stored environment snapshot or a passthrough list (unit B refuses `GATEQ_*` in both).

LIMIT, stated plainly: an agent can still type `GATEQ_HOLDER=<a live holder in this same worktree with enough cost>` by hand. That
runs a second thing in a worktree whose holder is running, under slots that holder already paid for: no budget is exceeded, the
exclusion is defeated for that one worktree, and the receipt's clean-before and clean-after checks remain the backstop. It is the
same honest-actor limit as typing the command raw.

This is load and exclusion accounting only, never a verdict input. (The duplicate gate in the first case is today's "double spend"
from steer rule 7 and is not made worse; letting the hook reuse a binding receipt is a separate idea, out of scope.)

---

## 6. Command and charter prose when a budget is configured

The pool is live the moment the maintainer writes `config.toml`. From then on a foreground gate can sit behind the Bash tool's
10-minute ceiling while `wait_timeout_s` is 3600, and exit 75 means the gate never ran. READ (main at f157846): every place that
runs the gate in the foreground reads a non-zero `gate_rc` as a FAILED gate, so turning the budget on with the old prose would
have agents "fixing" a gate that did not run. The prose that prevents that lands in PR A2, before any queue exists. The spots
(line numbers approximate):

| File | Spot |
|---|---|
| `commands/prep-pr.md` | Step 2 (about lines 255-332): the gate block and "If `gate_rc` is non-zero: ... Fix the failing gate" |
| `commands/handle-review.md` | Step 5.5 (about lines 487-515): the gate block and its reading of `gate_rc` |
| `commands/handle-review.md` | Step 7 gated-push block (about lines 759-784): the push runs on `gate_rc = 0`; the message reads `gate FAILED` for any other value |
| `commands/review-stack.md` | Step 4d "Run verification (delegate to gate-runner)" (about lines 432-460): the gate block and "If `gate_rc` is non-zero, fix the failures" |
| `skills/orchestrate/templates/adversarial-prep-charter.md` | the "Run the GATE STEPS DIRECTLY" bullet (about line 15), step (1): `gate-runner.py --receipt ...`, which already says `gate: NOT RUN` = RED |
| `skills/orchestrate/templates/implementer-charter.md` | the "FULL-GATE SCOPE" bullet (about line 44) and the "LONG GATE CHAINS" bullet (about line 35): run the full gate, launch long chains in the background |
| `commands/push-release.md` | step 4 "Run pre-checks" (about lines 98-99): a repo's `build.pre_checks` may be the gate (this repo's `.claude/release.toml` sets it to gate-runner), and any failure stops the release |
| `commands/prep-pr.md` | Step 7 push (about lines 835-869): with a pre-push hook installed, the upload starts the hook's gate, which waits on its own ticket (the Step 2 gate has exited, so there is no holder) |
| `skills/orchestrate/templates/pr-shipper-brief.md` | step 1 push (about line 18): the same hook-fired gate |

The edits, the same in each place:

- WHEN A MACHINE BUDGET IS CONFIGURED, the gate block is run in the background. The predicate is the runner's own, never bare file
  existence: `~/.claude/gate-queue/config.toml` exists AND has a `budget` key (a file holding only other keys is the pool OFF,
  section 3). The command block tests it with one read-only text match for a `budget =` line in that file. THE TEXT MATCH IS AN APPROXIMATION
  of the runner's TOML parse, in one direction that matters: a key written in a quoted or escaped spelling parses as `budget` and
  matches no such line, so the pool would be on while callers still run the gate in the foreground. The file is written by hand by
  one maintainer, so the contract is to write the key plainly, and `doctor` (which parses the file) WARNs when the parsed state and
  the text match disagree. A command block cannot parse TOML itself without an inline interpreter script. A false match (a
  commented-out key matched by a loose pattern, a malformed file) errs toward the background, which is harmless with the pool off:
  the gate runs the same and `gate_rc` is read the same way. When it holds, the gate block is run with
  `run_in_background: true` and the session reads `gate_rc=` from the block's own output on the completion notice, never from the
  notification's exit status.
- `gate_rc = 75` IS `gate: NOT RUN` (no slot or no worktree within the wait). It is never "fix the failing gate" and never a
  pass. The session re-runs the block; a second 75 is reported to the maintainer (in the charters: to the lead). The
  adversarial-prep report treats it as RED, as it already treats a missing runner.
- In the `/handle-review` gated-push block the push still runs only on `gate_rc = 0`, and the message for 75 says NOT RUN instead
  of `gate FAILED`.
- A push block in a repo with the pre-push hook installed is run the same way. A hook line `gate-runner: NOT RUN` under a failed
  push is a gate that did not run: re-run the block, never fix.

With no budget file these blocks behave exactly as today: the new prose is conditional on the file. `test-command-positional-args.py`
and the "Helper exec paths" rules in `commands/prep-pr.md` still apply to every command file edited. `/prep-pr` keeps its inline
Step 2 gate (decision 13); this changes only how the session waits on it.

---

## 7. Security and least privilege

The threat model is the floor's: an honest lead or teammate on the obvious path, NOT an adversary. The design is judged on whether
that path can still produce an unbudgeted heavy run, a second meaning for an existing grant, or a gate that reads as passed when
it did not run.

No new allow-list entry. `Bash(python3 ~/.claude/scripts/gate-runner.py *)` is the existing grant (#169); it gains `--pool-run
<name>`, which runs only a command already declared in the repo's `.gates.toml`, the same trusted file whose `run` strings this
grant has always executed as gate steps, and forwards no argument. An agent that can edit `.gates.toml` could declare a new
command, exactly as it could add a gate step today, so no new power is created; a `.gates.toml` edit is reviewed like any other
diff. The pool cannot cause a push or a PR. The deterministic floor is NOT changed and is NOT taught to read the pool;
`/prep-pr` Step 4a's classifier will print `standard` for these files, and that tier is right.

What this unit does NOT do: it does not stop an agent from typing a heavy command raw, from setting `GATEQ_HOME` to a private
directory, or from hand-setting `GATEQ_HOLDER`. Such a run is unbudgeted or unexcluded until the deferred enforcement phase; the
control until then is the charter rule in section 5.

---

## 8. Failure modes

| Failure | Resulting state | Recovery |
|---|---|---|
| A holder (hand-run gate or named command) SIGKILLed | Its slots free at once (kernel). Its step groups are orphaned, and its worktree stays claimed by the watchdog's `.sweep` lock until they are gone | Swept by the watchdog (section 4), which then exits and frees the worktree; the next evaluator unlinks the ticket and its sidecars |
| A hand-run gate HUNG in a step | Slots and worktree held | The runner kills it at `job_timeout_s` (exit 1, `KILLED`, a fail receipt); a named command at its `timeout_s` or `job_timeout_s` (`ran exit=124`) |
| No slot or worktree within `wait_timeout_s` | Nothing ran | Exit 75, `NOT RUN`; a stale receipt at the `--receipt` path is unlinked (#497). Callers read 75 as not run (section 6) |
| Old runner given `--pool-run <name>` | It ignores the flag and runs the FULL gate, unpooled | No `POOL-RUN:` line is printed, which every caller reads as NOT RUN (section 5); update the plugin and run `configure --apply` |
| A gate-runner copy that PREDATES the pool runs an ordinary gate | It runs unpooled: unbudgeted and unexcluded, with no signal in its output | Doctor WARNs on a stale deployed or plugin copy when a budget is configured; configure the budget only after it is clean. A worktree on a pre-A1 branch is not visible to doctor (section 4) |
| Two nested runs under one parent at once (a holder, or a nested run at any depth) | The second waits on that parent's child lock, holding nothing | Starts when the first ends; exit 75 after `wait_timeout_s` (section 5) |
| The runner is SIGKILLed between `Popen` and registration | The step is still held on its start pipe, reads EOF and exits without running | None needed (section 4) |
| A gate-runner copy with another POOL PROTOCOL | Against the config: exit 2, directory untouched. Against older live records: it waits behind lower-seq foreign tickets and never schedules, rewrites or unlinks them | Clears when the older processes exit (section 4) |
| A foreign-protocol ticket whose lock is FREE | A dead process's ticket, never touched by any evaluator. It holds no slot and no claim | `orchestrate-setup.py doctor` WARNs naming the file; the user deletes it (section 4). A foreign ticket whose lock is HELD is live and counts as held |
| Budget file removed or its `budget` key removed | Holders finish; new processes run as with the pool off; a ticket already waiting starts when its cost fits or exits 75 | Restore the file to turn the pool back on (section 3) |
| Malformed `config.toml` | Every new gate exits 2 with the parse error; holders finish | Fix the file |
| Pool root not 0700 or not owned | A gate exits 2 (ASSUMED: the pre-split design states this only for the queue) | `chmod 700`; doctor WARNs on it |
| Named check SIGKILLed mid-mutation | Slots free at once and the worktree claim once the watchdog's sweep is done; the worktree is left DIRTY | The next gate there writes a `dirty-before-run` fail receipt. The owner restores the files (check `git show HEAD:<path>`) |
| A named check's worktree is busy | It waits holding nothing; other worktrees are unaffected | Starts when the holder exits; exit 75 after `wait_timeout_s`, labeled NOT RUN |
| `--pool-run` with an undeclared or duplicated name, or extra arguments | Exit 2, nothing run | Declare the name in `.gates.toml` (a reviewed edit) |
| A continuous stream of class-0 checks | A waiting gate is passed K times, then protected; later checks are held | Section 2, worked numbers 2 |
| A heavy check run raw, outside the pool | Unbudgeted and unexcluded; the pool cannot see it | Charter rule now; a mechanism only in the deferred enforcement phase |
| A pooled serial step stopped by SIGTTOU | The step stays stopped, holding slots | `job_timeout_s` ends it (SIGKILL leg of the sweep); stdin from `/dev/null` closes only reads of stdin, and a step that opens `/dev/tty` is stopped by SIGTTIN the same way (section 4) |

---

## 9. Decomposition: unit A (#539) as four PRs

#538 is the EPIC. Unit A is one issue (#539), four PRs numbered A1 to A4 here (the queue document
numbers its own PRs separately). Named heavy commands and the orphan watchdog ride in unit A rather than in issues of their own:
every filed issue spends review budget, and `--pool-run` is a thin caller of the same ticket path, whose two hard parts (the class-0
rule and the worktree exclusion) live inside `schedule()`, which A1 must get right anyway.

- WHEN THE BUDGET MAY BE CONFIGURED: only after ALL FOUR PRs are merged, released and deployed (`configure --apply`), the watchdog
  of A4 included. Before A4 a SIGKILLed holder frees its slots while its step groups keep running, so a waiter is admitted on top
  of an orphan's load, which is the oversubscription the pool exists to prevent. A2's prose is written to be safe earlier, but a
  budget written before A4 runs with that hole, and doctor's pool-capable check (section 4) also requires the watchdog. The one
  release after the batch (below) is therefore the activation point.
- Why the command prose is in this unit and not in unit D: the pool can be made live the moment someone writes `config.toml`, which is
  after A2 and long before the queue wiring. Until the prose lands, `/prep-pr` runs a waiting gate in the foreground under a
  10-minute tool ceiling and reads 75 as "fix the failing gate".
- Depends on: this document merged. Nothing else.
- Tier: CR-required (script function). The charter prose rides in A3, since it is only true once the flag exists; the command prose
  rides in A2.
- Agent hints: `[mode: plan] [model: opus] [effort: high]`
- Acceptance criteria:
  - [ ] With no budget configured, gate-runner output and exit codes are unchanged and the pool module is never imported. This is
        PROVEN by dedicated cases (golden output for all four gate forms, and the runner copied alone with no pool module beside
        it), and every harness that spawns the runner pins `GATEQ_HOME` to an empty temp directory and unsets `GATEQ_HOLDER`;
        `test-gate-runner.py` is modified to do so. `--pool-run <name>` with the pool off runs the declared command directly.
  - [ ] With budget 10, two cost-4 gates run together, a third waits, a cost above the budget runs alone, and a wait that times out
        exits 75 with no step run and no receipt left that could read as this run's.
  - [ ] `--pool-run` accepts only a name declared in `.gates.toml`, forwards no argument, and derives its class from the declared
        weight and the machine cap; no caller input can select class 0; an absent `POOL-RUN:` line is documented and chartered as
        NOT RUN.
  - [ ] Two things never run in the same worktree at once. A nested run skips acquisition ONLY under a live holder of the same pool
        protocol with the SAME worktree key and a held cost at or above its own; a leaked `GATEQ_HOLDER` from another worktree is
        ignored, and a nested run heavier than its holder exits 75 at once. A SIGKILLed holder frees its slots at once and its worktree when the watchdog's sweep is done, with
        no cleanup.
  - [ ] No child process inherits a lock descriptor (no bare fork; every child is exec with `close_fds=True`), no flocked file is
        ever replaced (bypass counts live in a sidecar), and a record or config of another pool protocol is never scheduled,
        rewritten or unlinked. A foreign-protocol ticket whose lock is free is named by a `doctor` WARN and left in place; one whose
        lock is held counts as held.
  - [ ] With the pool on, a serial step runs in its own process group with SIGINT, SIGTERM, SIGHUP and SIGQUIT forwarded and the
        same exit as today, a hand-run gate is killed at `job_timeout_s`, and with the pool off the serial path is byte-identical.
        SIGTSTP and the SIGTTOU limit are documented as stated differences.
  - [ ] A scheduling pass makes no `git` call, parses no TOML and makes no network call under `admit.lock`; a ticket's cost is
        computed before the lock is taken and read from the ticket.
  - [ ] With a budget configured, every foreground gate caller of section 6 (`/prep-pr` Step 2, `/handle-review` Step 5.5 and its
        gated-push block, `/review-stack` Step 4d, the adversarial-prep and implementer charters, `/push-release` pre-checks, and
        the `/prep-pr` Step 7 and pr-shipper pushes where a hook is installed) runs the gate in the background and reads exit 75 as
        NOT RUN, never as a failed gate; with no budget file they are unchanged.
  - [ ] Killing the gate process with SIGKILL leaves no registered step group alive within a few seconds (one second was observed),
        on the parallel path, the pooled serial path and `--pool-run`; a pgid the runner already pruned is never signalled; the
        watchdog child inherits no lock descriptor and holds only the `.sweep` lock it opened itself.
  - [ ] A gate passed K times is protected and holds back later gates and class-0 checks; every new assertion is mutation-proven;
        harnesses are `.gates.toml` steps and in both lint lists; the three charters and SKILL.md carry the declared-name rule and
        its stated limit.

### PR-level slicing and test plans

Foundation first. Every script PR: stdlib only, Python 3.11+ (the `tomllib` floor gate-runner already has), `ruff check --select
F,E741` clean, a new `test-*.py` harness added as a `.gates.toml` step with a unique explicit name (the lockstep harness's
filesystem leg then requires nothing more for CI, which derives its list), the harness added to the hand-maintained `ruff` list in
BOTH `.gates.toml` and the CLAUDE.md `## Gates` block, and each new script added to `HELPER_NAMES` with the #284 steer
canonical-list and helper-count lockstep. NO new bash. Every new assertion is MUTATION-PROVEN: the named mutation is applied to a
copy, the case must fail, and the mutation never touches a file being committed. EVERY harness that spawns `gate-runner.py`,
existing or new, pins `GATEQ_HOME` to a fresh empty directory under `tempfile` and removes `GATEQ_HOLDER` from the child
environment, and never runs the real gate. One version bump and ONE release after the batch lands, in its own release PR: this is the maintainer's standing batch-release
practice (as v0.106.0, #531), and it is safe here because nothing is active until the budget is written after that release.

No PR is claimed to fit the `/prep-pr` Step 1b size threshold (READ: 800 lines of change, 10 files). A1 is the largest and is the
one most likely to exceed it; Step 1b will say so when it does, and the pure `schedule()` with its table tests is the natural seam
to split on if so.

| PR | Scope | Tier | Test plan |
|---|---|---|---|
| A1 | `gate_pool.py` with `POOL_PROTOCOL`; the pure three-class `schedule()` and the per-worktree exclusion; slots; tickets plus `.sched` sidecars; gate-runner acquires as a hand-run gate; `weight`; `config.toml` (`protocol`, `budget`, `backfill_bypass_limit`, `small_check_cap`, `wait_timeout_s`, `job_timeout_s`); exit 75; the exact `GATEQ_HOLDER` exemption; the OFF proof; the `test-gate-runner.py` pin; doctor WARNs (root mode, malformed config, a foreign-protocol ticket with a free lock, a `budget` the TOML parse sees and the callers' text match does not); `gates.toml.md`; retire the "takes no lock" note in `orchestrate-steer.sh` | CR-required | `test-gate-pool.py`: the OFF proof (golden output for Form A, serial and parallel Form B and the fallback chain; the runner copied alone with no pool module; nothing created under `GATEQ_HOME`); two cost-4 fit in 10 and a third waits; cost above budget runs alone; no `weight` and no `jobs` runs alone; SIGKILLed holder frees slots and worktree and a waiter proceeds; arrival order within a class; `schedule()` table-tested as a pure function over all three classes, replaying BOTH worked tables of section 2 and the "when the class matters" example; a protected gate holds later gates and class-0 checks but a protected first push does not hold a fix round; same-worktree entries never start together and an entry waiting on its worktree accrues no pass; timeout exits 75 and unlinks a stale receipt; malformed config exits 2; the nested exemption table of section 5, row by row; a bypass written by another process leaves the ticket's lock HELD; a foreign-protocol record is never unlinked, a foreign ticket with a free lock yields the doctor WARN, and a config mismatch exits 2; no `os.fork` in the module; a pass makes no `git` call under the lock; budget file removed mid-wait (the waiter still starts when its cost fits; a process started afterwards takes no ticket); `.gates.toml` edited during a wait (the definition run is the one read after the grant); a runner with a budget configured and no `gate_pool.py` beside it exits 2; every config key refuses zero, a bool and a string with exit 2; a class-0 start records no pass against a gate waiting in its own worktree; two nested runs under one holder run one after the other; a run nested one level deeper does not wait on its ancestor; FAN-OUT: two concurrent children of one nested run run one after the other (each takes the parent's child lock), and a `GATEQ_NEST` naming an unlocked or foreign file is ignored; doctor WARNs on a pre-pool deployed or plugin copy when a budget is configured. one pass over an entry at `bypass = 1` with four small entries behind it returns one start, not four (the bound of K holds within a pass). Mutations: test protection against the stored count only; drop the `.nest` lock; let a run with a valid `GATEQ_NEST` skip locking; drop the same-worktree filter from the class-0 pass list; drop the `min(cost, budget)` clamp; drop either HOLD line; count a cost-0 start as a pass; stop counting a class-0 start as a pass; drop the `claimed` check; make slots inheritable; read a missing `budget` as unlimited; drop the worktree-key comparison from the nested check; drop the cost comparison from it; write the bypass into the ticket with `os.replace`; unlink a foreign-protocol ticket whose lock is free; honor `GATEQ_HOLDER` from a snapshot |
| A2 | The pooled serial step path (own process group, forwarded interrupts, `/dev/null` stdin); the hand-run `job_timeout_s`; the pool-on command and charter prose of section 6 (every file in that table) | CR-required (the prose rides along) | Extends `test-gate-pool.py`: a pooled serial step gets a forwarded interrupt (SIGINT, SIGTERM, SIGHUP, SIGQUIT) and the same exit status and final lines as pool-off, a hand-run gate is killed at `job_timeout_s` with `KILLED` and a fail receipt, SIGTSTP is not forwarded, and pool-off is byte-identical. `test-command-positional-args.py` and the helper-exec-path rules still pass for the four command files. Mutations: do not forward SIGQUIT; start the serial step in the runner's own group; skip the sweep on an exit path; give the step the terminal as stdin |
| A3 | Named heavy commands: `[[pool.command]]`, `--pool-run <name>`, the `POOL-RUN:` line, pool-off pass-through; the declared-name rule (with "no `POOL-RUN:` line means not run") in the implementer, adversarial-review and adversarial-prep charters and SKILL.md line 236; `gates.toml.md` | CR-required (the prose rides along) | `test-gate-pool-run.py`: unknown, duplicated or malformed name exits 2; any extra argument exits 2 and reaches no shell; missing `weight` exits 2; class is 0 at the cap and 2 one above it, and no flag or variable changes that; `--receipt`/`--skip`/`--jobs` beside it exit 2; the command's exit code passes through and the `POOL-RUN:` line separates a command's own 75 from NOT RUN; pool off runs directly with no ticket; a check and a gate in one worktree exclude each other in both directions; `--pool-run` inside a gate step runs under `GATEQ_HOLDER` when its weight is at or below the gate's and exits 75 at once when above; a leg whose runner lacks the flag is skipped by the caller's pre-check. Mutations: forward argv to the command; take the class from an environment variable; skip the worktree claim for named commands; treat a missing `weight` as 1; omit the `POOL-RUN:` line |
| A4 | The orphan watchdog (death-pipe sweeper) in the parallel step path, the pooled serial step path and `--pool-run` (the pool-off serial path has no group and stays untouched) | CR-required | Extends `test-gate-runner.py`: SIGKILL the runner, assert a registered group is gone within a few seconds and a pruned pgid is never signalled; the watchdog child inherits no lock descriptor (no slot, ticket or `admit.lock` descriptor is open in it) and holds exactly the `.sweep` lock it opened; a runner killed between `Popen` and the start byte leaves no step running; after a holder is SIGKILLed, a waiter in the SAME worktree does not start until the watchdog's sweep is done, and one in another worktree starts at once; a dead ticket's held `.nest` file is not unlinked; the claim is held for as long as a registered group is alive (a group that ignores the first kill keeps it past 10 s). Mutations: release the claim on a timer; leave the `.sweep` claim out of `busy`; unlink a held `.nest` file; never send `-pgid`, skip the EOF sweep, send the start byte before `+pgid` |

Dependencies: A2 and A3 each need A1 and are independent of each other. A4 needs A2 (it hooks the pooled serial path) and covers
`--pool-run`, so it is ordered after A3 as well. A2's timeout does NOT rely on the watchdog: the runner enforces it in-process while
alive, so A2 and A4 need no reordering. The watchdog is a prerequisite for unit B, whose dispatcher SIGKILLs a stuck worker and
relies on the watchdog to sweep that worker's step groups; that is why A4 must land before unit B, and it is the only dependency of
the queue on this unit's later PRs.

---

## 10. Rejected alternatives

- A lock or log directory under `/tmp`; `shlock` (wedges on an unrelated live pid, no stale-age break, not in a base Linux install);
  `mkdir` locks with a stale-age break (the elmer shape: correct only because bash has no `flock`, and several review rounds found
  races in it; PRIOR, no count persisted).
- A charter clause that agents never run gates (it reverses `implementer-charter.md` and the adversarial-prep role), and a cap on
  agent COUNT (agent count is not the cost).
- Per-step tokens shared across gates (a jobserver): `exclusive` steps plus hold-and-wait deadlock.
- Live load average or free memory as an admission signal (load lags a minute, so a burst is admitted together; macOS free-memory
  figures mislead). Recorded wall time as cost (it is not memory, and the receipt records none).
- `orchestrate-resources.py`'s JSON leases for slots: a lease outlives SIGKILL and needs a liveness sweep with no analogue for a
  gate. Its flock idiom is reused; its leases are not.
- A bare `os.fork()` in the pool: it copies every descriptor, so a forked child can pin a lock (RUN A). Closing them as the child's
  first act works (RUN F3) but must be remembered in every future fork; `Popen` makes the inheritance impossible instead of
  forbidden.
- Persisting a bypass count into another process's ticket by atomic replace: it deletes a live waiter (RUN B). Sidecars instead.
- FIFO tickets as the ONLY order (the issue body): it orders processes, not jobs, so a killed session loses its place. Kept only for
  hand-run gates, where the process IS the job.
- Ordering by timestamp alone, and a declared priority field. Strict FIFO admission (idle budget behind a heavy head) and unbounded
  backfill (starvation).
- The budget in an environment variable, or in a repo's `.gates.toml`.
- Step children inheriting the slot descriptors (accurate accounting, but one leaked daemon wedges the pool).
- A priority a caller can REQUEST. The class is derived from the declared weight against a machine cap, or it becomes the way to
  jump every queue. Class 0 open to any declared weight (a full race suite at the head of every queue is the original exhaustion
  with priority added). Class-0 checks exempt from the starvation bound (a looping reviewer would starve every gate).
- Arguments forwarded through `--pool-run`: that is `run <worktree> -- <cmd>` again by another door.
- A separate lock file per worktree for the exclusion: a second resource to acquire and a lock order to get wrong; deriving `busy`
  from live holder records needs neither.
- Forwarding SIGTSTP to a pooled serial step: it needs stop-and-resume coordination between the runner and its group for a case no
  agent flow produces (section 4).

---

## Open questions

None concern the pool. Every open question of the #538 design (the runner-made refresh merge, and the scope of the later enforcement
phase) belongs to `DESIGN-gate-queue.md`.

---

## What this document does not cover

- UNITS B TO D: the persisted job queue, `gate-enqueue.py`, the dispatcher and workers and their process model and environments, the
  heartbeat, timeouts and `cancel`/`stop`, the push and PR tail, freshness at slot grant, the `/prep-pr` and `/handle-review` queue
  path, and the `cleanup-worktree.sh` check. These are in `DESIGN-gate-queue.md` (in progress). They extend this unit through one
  seam: its dispatcher feeds queued jobs to the `schedule()` defined here (section 2).
- THE ENFORCEMENT PHASE (deferred, unit E, not filed): hooks that would make the queue the only agent push path, and that would also
  close the raw-heavy-command, `GATEQ_HOME` and `GATEQ_HOLDER` limits stated in sections 3, 5 and 7 of this document. It is described in
  `DESIGN-gate-queue.md`.

---

## Appendix: what was run

All in the session scratchpad; nothing in the repo was executed beyond reads, no gate or harness was run, and no commit was made.
Linux was not exercised. Experiments this document relies on (Darwin 27.0.0 and 27.0.1, Python 3.14.8; stdlib and `sleep` only),
labeled as in `DESIGN-gate-queue.md` so the two documents cite the same experiment by the same name:

| # | Experiment | Observed |
|---|---|---|
| E1c | Two `open()` calls on one file in one process, lock the first, try the second non-blocking | CONFLICT (flock is per open file description) |
| E3 | Gate process registers a step group (`sleep & sleep`) with a pipe-fed watchdog, then is SIGKILLed | The step group was gone 1 s later |
| A | A "dispatcher" holds a lock, `os.fork()`s a child that never touches the descriptor, and is SIGKILLed | HELD until the forked child exited: a forked child pins the lock |
| B | `os.replace` over a file whose owner holds a flock on it | A new opener sees FREE: the lock stays with the old inode |
| C | A forked child calls `LOCK_UN` on the inherited descriptor | The PARENT's lock is released |
| F3 | The fork fallback: a forked child's first act is `os.close()` on both lock descriptors (no `LOCK_UN`) | With the parent alive the lock stayed HELD (a close in the child does not unlock); after SIGKILL of the parent both were FREE while the child lived |
| F4a | A process flocks its ticket; another writes the bypass count to a `.sched` sidecar with `os.replace` | Ticket lock still HELD; sidecar readable |
| F4b | The owner rewrites its own flocked ticket in place (truncate and write through the locked descriptor) | Ticket lock still HELD; same inode |
| F4c | Control: `os.replace` over the flocked ticket | Ticket lock FREE to a new opener (the defect, reproduced) |
| F5 | A serial-style step `sh -c "sleep & sleep; wait"` started with `Popen(process_group=0)` (own group, same session); SIGINT forwarded to the group, then SIGTERM | After SIGINT the shell ended (`-2`) and its foreground child was gone; the BACKGROUND child survived SIGINT and was gone after the SIGTERM sweep |

`exp/fix1/replay.py` transcribes the `schedule()` pseudocode of section 2 one to one and replays both worked tables and the "when
the class matters" example; every row matched (`exp/fix1/replay.out`), and a round 2 reviewer's replay agreed. One filter was added
to the pseudocode AFTER that replay (PR #543 review: a class-0 start no longer counts a pass against an entry in its own worktree).
It only removes same-worktree entries, and neither table nor the example has two entries in one worktree, so the replay still
stands; it was not re-run.

Reasoned, not run: the serial path's exact exit status on an interrupt, pool on against pool off (a harness case in A2, since no
gate was run here); the behavior of a step stopped by SIGTTOU (section 4); an owner creating and flocking its ticket inside one
`admit.lock` section so that no evaluator sees it unlocked (section 1).

