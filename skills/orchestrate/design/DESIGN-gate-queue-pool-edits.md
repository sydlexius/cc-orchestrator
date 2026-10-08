# Design: edits unit B needs in the pool document (#538, unit B / #540)

Date: 2026-10-07
Status: PROPOSAL. Moved out of `DESIGN-gate-queue.md` (which sits at its line limit) with the review-5 additions (items 2, 4, 9
and 10). One follow-up PR against `DESIGN-gate-pool.md`; none of these changes unit A behavior.
Companion: `DESIGN-gate-queue.md` (unit B; the section "Edits this unit needs in the pool document" points here).

---

## Edits this unit needs in the pool document

Re-checked against the MERGED `DESIGN-gate-pool.md` (main at 7d0d087, PR #543), passages quoted by text; one follow-up PR. None
changes unit A behavior. (The former item 1, `GATEQ_NEST` for `running/`, is SATISFIED by the merged chain rule.)

1. Section 2, `commit()`: "record e.worktree and e.cost in e's own holder record (its ticket, or its running journal)" -> "...
   (its ticket)". The note under the pseudocode: "Where its comments say "job" or "running journal" they mean unit B's queued jobs;
   in unit A the only entries are tickets and the holder record is the ticket." -> "Where its comments say "job" they mean unit B's
   jobs, whose holder record is also a ticket (`running/<job>.ticket`); in unit A the only entries are tickets." Section 5, nested
   runs: "`GATEQ_HOLDER=<path of its own ticket or running entry>`" -> "`GATEQ_HOLDER=<path of its own ticket>`". No journal exists.
2. Section 4, pool protocol, after "`gate_pool.py` therefore defines `POOL_PROTOCOL = 1`, and it is written into every ticket, every
   `.sched` sidecar and (by the user) `config.toml`." ADD: "Unit B's PR B2 moves it to 2: evaluators then also read job tickets in
   `queued/` and `running/`, and a protocol-1 copy exits 2 against a protocol-2 config. Deploying B2 needs the user's edit of
   `protocol` in `config.toml`; a branch that predates B2 exits 2 until it merges main. A `.sweep`'s two fields are frozen like a ticket's: a held one of any
   protocol counts in `busy`, and a free one of any protocol is unlinked."
3. Section 4, "A COPY THAT PREDATES THE POOL", after "doctor WARNs naming each `gate-runner.py` on the deployed leg and in the plugin
   cache that fails any part, and says which." ADD: "From unit B's PR B2 doctor also WARNs on a visible `gate_pool.py` whose
   `POOL_PROTOCOL` differs from the configured `protocol`; pool-capable itself never tests the number."
4. Section 4, "THE WORKTREE STAYS CLAIMED UNTIL THE SWEEP IS DONE", after "sends the path to its watchdog over the pipe." ADD:
   "`<ticket name>` is the stem `<seq>-<id>`, as for `.sched`. Every `.sweep` lives in `waiters/`, whatever directory its holder's
   record is in, so `busy` reads them from one place. A unit B worker is committed by the dispatcher before it exists, so it
   creates `waiters/<job>.<its pid>.sweep` itself (the pid keeps one attempt's file from being the one a later attempt creates),
   in the `admit.lock` section that writes its pid, before its first step or external call;
   its ticket lock, held since the commit, covers the gap." And under "EVERY RUNNER WITH A WATCHDOG HOLDS A CLAIM", ADD: "Unit
   B's dispatcher is the exception: its watchdog covers only its own read calls and it holds no worktree, so it creates none."
5. Section 4, end of the watchdog: "It covers the parallel step path, the pooled serial step path and `--pool-run`" -> "... and
   `--pool-run`, and from unit B the process group of every external call a worker or the dispatcher makes".
6. Section 9, dependencies: "The watchdog is a prerequisite for unit B, whose dispatcher SIGKILLs a stuck worker and relies on the
   watchdog to sweep that worker's step groups; that is why A4 must land before unit B, and it is the only dependency of the queue
   on this unit's later PRs." -> "The watchdog is a prerequisite for unit B, whose dispatcher and workers each run their own (the
   dispatcher never signals a worker) and whose workers hold a `.sweep` claim; A4 must land before B2. The queue also needs A2 (B3:
   the pooled step path and `job_timeout_s`) and A3 (D1: the SKILL.md machine-resource bullet)."
7. Section 1, entry name: "every seq visible in `waiters/`" -> "every seq visible in `waiters/` (from unit B also `queued/` and
   `running/`)". Section 3, under the `[[pool.command]]` table, ADD: "`[[pool.command]]` is all this unit reads under `.gates.toml`
   `[pool]`. Unit B's repo key (`env_passthrough`) lives in `.gates.toml` `[queue]`, as its machine keys live in `config.toml`
   `[queue]`."
8. Pointers, now that the queue design is three documents. Header: "`DESIGN-gate-queue.md` (units B to D: the job queue, the
   dispatcher, the push and PR tail; in progress)" -> "`DESIGN-gate-queue.md` (unit B: the job queue, dispatcher and workers),
   `DESIGN-gate-queue-push.md` (unit C: the push and PR tail), `DESIGN-gate-queue-wiring.md` (unit D, and the deferred unit E)".
   "What this document does not cover": "These are in `DESIGN-gate-queue.md` (in progress)." -> "These are in those three
   documents."; "It is described in `DESIGN-gate-queue.md`." -> "... in `DESIGN-gate-queue-wiring.md`, section 8." Section 5:
   "the deferred enforcement phase's subject (`DESIGN-gate-queue.md`)" -> "(`DESIGN-gate-queue-wiring.md`, section 8)". Defaults:
   "runner-made refresh merges, belongs to the queue" -> "... belongs to `DESIGN-gate-queue-push.md`". Open questions: "belongs to
   `DESIGN-gate-queue.md`" -> "belongs to `DESIGN-gate-queue-push.md` (questions 1, 2) and `DESIGN-gate-queue-wiring.md` (3, 4)".
9. Section 4, "NO UNLOCKED WINDOW" (the sentence ending "its watchdog has it; milliseconds, and the only thing ever waited for
   under that lock"), ADD: "The wait also ends when the watchdog child has exited: the runner then unlinks its own still-free
   `.sweep`, releases `admit.lock` and exits non-zero having started nothing." It is a liveness test, not a timer, and it is
   before the first step, so no orphan can exist; PR A4 implements the loop.
10. Section 4, the `admit.lock` row of the lock table ("writes of the evaluator's own ticket") and the sentence of item 9: ADD
    "from unit B also: job tickets, the renames between `queued/` and `running/`, and one `Popen` per admitted job". The pool
    table describes unit A only until this lands.

