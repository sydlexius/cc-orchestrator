# Design: TL context minimization (receipts, file-handoff, deterministic containment)

Date: 2026-07-05
Status: DRAFT (brainstorm) - adversarially reviewed 2026-07-05 (3 independent hostile critics:
correctness/feasibility, floor/least-privilege, prior-art/consistency). Floor-touching bets
KILLED or de-scoped; several levers reframed from "new mechanism" to "enforce/reuse existing."
Pre-decomposition.
Companion: `SKILL.md` (lead playbook - the DIRECTIVE-ID/RECEIPT/LEDGER protocol, DELEGATE-OR-
SUMMARIZE, the /tmp/<team> handoff, `stack.schema.json` attestations this REUSES),
`DESIGN-deterministic-floor.md` (the floor whose invariants constrain this),
`ship-gate-preflight.sh` (the deterministic merge oracle this reuses),
`orchestrate-feedback.sh` (the maildir this borrows *self-pruning*, not append, from).

## Problem

The orchestrate LEAD (TL) is the single human-facing channel and the scheduler for the whole
multi-agent pipeline, so its context window is the scarcest resource in the system: when it
fills, the TL compacts and re-derives, and the whole team stalls behind it. An empirical crawl
of 32 orchestrate-lead transcripts (main chain only = the TL's own window) attributes ~2.97M
tokens of tool-result text, ~93K/session, dominated not by reasoning but by **content the TL
only needed to route or verify, never to think about**:

| Source (TL window) | ~tokens | note |
| --- | --- | --- |
| `Read` results | 1,234,688 (42%) | much re-read or derivable, not net-new reasoning |
| `Bash` results | 1,116,877 (38%) | oracle re-runs + raw command dumps |
| teammate report bodies (`Agent`/`SendMessage`) | ~358,000 | the TL "opens the envelope" to relay it |
| Slack reads | ~161,000 | history pulled into the lead window |

Rigorously-isolated **waste** subsets (per-session, not guessed): redundant re-reads (2nd+
`Read` of a path already in-context) **343 events, ~201K tokens**; teammate-report bodies the
TL opened, **326 reports, ~308K tokens (~9.6K/session)**; oracle re-run output ~332K tokens
(the churn is the *repeats* - the biggest measured repeat is `ship-gate-preflight` re-runs);
`SESSION-STATE.md` bloat 12-13K tokens/read, every resume.

## Principle

Every indicator is the same violation: **the TL holds *content* when it only needs *metadata*.**

> **Generative rule:** the TL should touch content only when it must *reason*. For *routing*
> and *verifying* it touches only names, enums, pointers, and exit codes.

Two orthogonal axes generate the levers, and place any new idea: **(1) *when* content enters
the window** - only on exception, only on cache-miss, only the needed section; **(2) *who*
holds the content** - a file, a subagent that evaporates, or an authoritative source re-queried.

**Load-bearing caveat (from adversarial review).** A SHA is a proxy for the *committed tree
object only*. The state a gate actually validates is *(committed tree + index + working tree +
live GitHub)*. So an `HEAD-sha` name/key can vouch for a clean, committed tree and **nothing
else** - not a dirty worktree, not staged-but-uncommitted work, not CI/review state. Every
receipt/memoization lever below is bounded by this: it applies to *pure functions of a
committed tree*, verified against a *live clean-worktree check*, in the *artifact's own
worktree*. GitHub-reading oracles are excluded entirely.

## Prior art this REUSES (do not reinvent)

Adversarial review found most of the "new" mechanisms already ship. This design EXTENDS them:

- **DIRECTIVE-ID / RECEIPT / LEDGER protocol** (`SKILL.md`, `implementer-charter.md`): every
  teammate report already carries a `RECEIPT:` block enumerating directive IDs with state, and
  the lead already runs the **RECEIPT-DIFF CHECK** against its TaskList on every "complete."
  We ADD `receipt_path` + `verdict` fields to that block; we do NOT mint a parallel ledger.
- **`stack.schema.json` attestations**: `head_sha` (pr-shipper hard-compares to pushed HEAD),
  `prep_ok` (prep-gate-green attestation gating the `# prep-pr-ok` marker), `review_handled`
  (SHA attestation CR replies were posted+resolved). The "attestation keyed to a SHA = proof"
  model is shipped; the gate receipt (below) is a *formalization* of it, not a new format.
- **`ship-gate-preflight.sh`** (#110): already the deterministic merge oracle (exit 0=PASS /
  2=BLOCK, fail-closed, verdict chosen inside the tool), already consumed by exit code in
  `/merge-pr` and `pr-watch.sh` (which calls the oracle's `--codoki-gate`). It IS the "verify by
  exit code" contract.
- **`pr-watch.sh` / `issue-watch.sh`**: already the never-poll watchers (block, one stdout
  line). The "never-poll rule" is shipped policy, not a new lever.
- **DELEGATE-OR-SUMMARIZE** (`SKILL.md`): already mandates pushing big reads/greps/log work to
  short-lived subagents that return CONCLUSIONS. The digest-subagent lever is *enforcement of
  this*, not a new rule.
- **The `/tmp/<team>/` handoff** (`SKILL.md` role table): pr-triage/adv-review/pr-prep/
  plan-steward/planner already write work products to files the lead routes. The finding
  channel formalizes a *schema* over this existing pattern.

## Indicators and grounded levers (survivors of review)

### #1 Redundant re-reads (~201K, pure waste) -> steer WARN, not deny

A PreToolUse hook that **warns** on a re-`Read` of a path already read this session with
unchanged mtime/hash. Route through `orchestrate-steer.sh` (advisory, fail-silent-open), NOT
the guard: a hard deny would fail-*closed* against a legitimately-needed re-read (post-
compaction, hash collision). Needs stateful per-session read-tracking - a different mechanism
than the guard's stateless command-line grep. **Prereq (harness-verify): does PreToolUse fire
on `Read`, and can the hook see the file_path + prior-read state?** Unverified today.

### #2 `SESSION-STATE.md` bloat -> checkpoint HEAD + reconstruct, WITH a durable home for judgment artifacts

Root cause: the file copies state that lives authoritatively elsewhere. Split it:

- **Checkpoint HEAD** - a tiny durable file (repo-root, gitignored, survives reboot) holding
  only *non-derivable intent*: status banner, next 2-3 actions, wave/plan, pointers.
- **Reconstruct-on-demand for REBOOT-DURABLE derivables** - in-flight PRs from `gh pr list
  --head`, worktrees from `git worktree list`. Never mirrored -> never stale.
- **CORRECTION from review (B1):** the existing "mirror any /tmp artifact into the durable
  doc" rule exists because **judgment artifacts (adv-review/triage findings) are NOT reboot-
  durable and NOT SHA-invalidated** - losing a mid-loop finding set on reboot is real data
  loss. So: only *deterministic SHA-named receipts* may be reboot-ephemeral (`/tmp`); judgment
  findings keep a durable home (mirror rule retained, or a durable per-PR store). Do NOT
  blanket-reverse the mirror rule.
- **Per-PR dirs** `/tmp/<team>/pr-<N>/` hold the deterministic receipts (maildir-*shaped*,
  single-writer, self-pruning, swept at `post-merge-cleanup`).

### #3 Teammate-report bodies (~308K) -> file envelopes + report-by-exception (internal only)

Trust rationale: the TL reading a teammate's verdict confers no trust (it is not re-doing the
work, is not a deterministic oracle) - but the read *does* preserve a human-inspectable
artifact at a trust boundary. So: teammate writes its body to a file; reports a tiny header
(verdict enum + receipt path + `blocked` bool + note) by EXTENDING the existing RECEIPT block;
the TL routes the *path* unopened. **Fenced (Bet 4 constraint): report-by-exception applies to
INTERNAL, non-boundary handoffs only. At the push/merge boundary the TL still runs the live
CLAUDE.md enumeration** (`pr-unreplied-comments.sh` + thread `isResolved`) - a one-bit ACK
never crosses the boundary, and the RECEIPT-DIFF CHECK still runs (see Bet 4 for the
reconciliation).

### #4 Deterministic gate receipt (committed-tree only; a formalization of stack.json)

`gate-runner.py` gains `--receipt <path>`, writing the receipt as a byproduct of the real run
(verdict token chosen inside the tool from its own exit code; atomic `os.replace()`; never a
`tee` the caller names). Bounded by the load-bearing caveat:

- **Filename** keys on worktree + PR + gate + **full 40-char SHA** + verdict, no timestamp
  (7-char collides across worktrees). Existence is a *hint*, not proof.
- **Body** records `commit_sha` (full), `tree_sha` (`git rev-parse HEAD^{tree}`), `worktree`
  (absolute path), `result`, `steps[]`, `producer`.
- **Consumer contract (corrected):** trust iff `result==pass` AND, *run in the artifact's
  worktree*, `commit_sha == $(git rev-parse HEAD)` AND a **live** clean-worktree check passes
  (`git diff --quiet && git diff --cached --quiet`). The stale `tree_clean` body field is
  worthless for freshness. This is *two* live git queries, not "one rev-parse" - the receipt
  can vouch ONLY for a clean, committed HEAD, never for uncommitted work.
- **Reuse:** this is the `stack.json` `head_sha`/`prep_ok` attestation made a first-class
  artifact; the userland prep-pr command (which already runs the gates) writes it and appends
  `# prep-pr-ok`. The guard is NOT taught to read it (see KILLED, Bet 1).
- **Lifecycle:** `/tmp/<team>/pr-<N>/`, OS-reaped, swept at `post-merge-cleanup`.

### #5 Raw Bash output in-window -> delegate reads/greps (enforce DELEGATE-OR-SUMMARIZE)

Big reads/greps go to a subagent that returns the *hit*, not the *haystack*; heavy build/test
output tees to a file, grep the tail. This is enforcement of an existing rule that isn't
holding - pair with the budget meter (Bet 6) for instrumentation, and a steer nudge.

### #6 The review<->fix loop - the finding channel (split for PR-blindness)

The adv-review -> implementer loop is the worst relay tax because it is a *loop*. Formalize the
existing `/tmp/<team>/adv-review` handoff as a schema-typed channel - **split into two files to
preserve PR-BLINDNESS (harness-misfit D1):**

- **fix-list slice** (PR-blind, implementer reads/writes): `{round, findings:[{id, severity,
  detail, status:"open"|"addressed", fix_sha}]}` - NO thread IDs, NO bot-reply prose.
- **reply/thread slice** (lead only): `{finding_id -> {thread_id, disposition, reply_text}}` -
  the implementer never sees it, preserving "implementers never see the PR/CR."

Protocol (correctness #4 fixes):
- **Single-writer-per-round**, serialized by the TL (round = the serialization token) - no
  concurrent mutation of one array; or per-agent append-only files the lead reduces.
- **Liveness:** file mtime + a per-round deadline, so the TL can distinguish *stalled* from
  *dead* from *slow* (verdict+open-count alone cannot).
- **Freeze lifecycle (M2):** the finding file is owned by the LEAD across the implementer's
  freeze/respawn (implementers are shutdown+wait-terminated before push, respawned fresh per
  round) - a terminated writer's file is validated by the lead, not trusted blindly.
- The loop crosses NO trust boundary, but its *output* (the diff) does - so the TL's K-round
  cap + stall detection are cheap **because the boundary gate re-checks the output** (gates +
  hostile prep review + live thread enumeration), NOT because verification was deleted.

For the external bot loop, the reply/thread slice carries pre-composed replies: **triage**
authors `merge-safe`/`rebut` replies; the **implementer** authors `fix` replies citing the SHA
it committed. The TL actuates via existing helpers. **Guardrail (strengthened):** before posting
a `fix` reply, verify `fix_sha` is on the pushed remote AND bound to the finding (a commit
trailer referencing the finding id - ancestry alone does not prove *this* commit fixed *this*
finding). This also enforces push-first-then-reply (prevents the 404-on-unpushed-SHA failure).

### #7 Console-availability containment -> steer WARN, not guard deny

A marker-gated PreToolUse hook that **warns** (via `orchestrate-steer.sh`) on an explicit
`run_in_background:false` Agent/Task call. NOT a guard deny: a deny is fail-*closed* against the
lead's own legitimate synchronous patterns and can strand an approval a backgrounded agent
can't answer. **Prereq (harness-verify): does PreToolUse fire on the `Agent`/`Task` tool, and
can the hook read `run_in_background` + the marker (which is `$TMUX`-keyed, absent in some
in-process spawns)?** Unverified - must be confirmed before this is buildable, not assumed.

## Architectural bets (revised)

1. **[KILLED] Verification-in-the-floor.** Proposed the floor allow push/merge only if a
   SHA-named receipt exists. Killed by two independent defeaters: (a) the receipt filename is a
   string any teammate can `touch`, so it converts the *unconditional* marker-gated merge deny
   into "allow if a forgeable file exists" - directly reopening the #105 bot-merge hole on the
   obvious path; (b) the floor's mandatory fail-*open*-on-error means any error in the receipt
   check (`rev-parse` in a stray cwd, reaped `/tmp`) ALSO allows the action. For GitHub-reading
   oracles, existence is additionally *stale* (verdict was true at a past instant). **Receipts
   INFORM the lead's ship-gate decision; they NEVER replace the floor deny or the live thread
   enumeration.** The `prep-pr-ok` advisory push gate stays; the guard is never taught to run
   git or read `/tmp` (that would also destroy its "greps its own command line" isolation).
2. **[KEEP, reframed] Digest subagent as the reader of raw state.** Enforcement of the existing
   DELEGATE-OR-SUMMARIZE rule: a throwaway read-only agent reads SESSION-STATE / `gh pr list` /
   big diffs and returns a bounded (<500-tok) digest. Cheapest big win; read-only (background-
   agent-ban compliant).
3. **[KEEP, honest-partial] Choreography over courier.** The reachable 80%: non-boundary
   handoffs pass only a *path*; the TL stays scheduler, stops being courier. Review confirmed
   this fits the harness (adv-review is a one-shot ephemeral Agent that writes a file and
   evaporates; the lead re-spawns per round). Full peer-to-peer choreography still fights the
   spawn model - not pursued.
4. **[KEEP, fenced] Report-by-exception (internal only).** Success = a one-bit ACK on INTERNAL
   handoffs. Honest bound: it reduces the TL's per-round *token* cost even on REJECT rounds
   (detail goes to the finding file, not the window) but does NOT reduce the number of
   scheduling *turns* - it is token-relocation, not "cheaper the better it runs" for the loop.
   **Reconciliation with the RECEIPT-DIFF CHECK (M1):** you cannot one-bit-ACK on success AND
   diff the full ledger on every complete - so the ledger moves to a file the lead globs and
   diffs *on demand*; the ACK carries its path. **Per-task-class trust (correctness #3):** a
   one-bit ACK is safe ONLY where a deterministic downstream oracle re-checks before the
   irreversible step (gate-gated pushes). For JUDGMENT correctness (did the implementer make
   the *intended* fix; is a rebut *valid*) a green gate proves "tests pass," not "right change" -
   those still require adv-review/the lead to read the substance. Never collapse a judgment
   success to one bit without naming the oracle that re-checks it.
5. **[KEEP, de-scoped] Memoize PURE committed-tree oracles only.** NOT "every deterministic
   oracle." Explicit allowlist keyed on `HEAD^{tree}` + a live clean-worktree gate. EXCLUDED:
   `git diff` (worktree/index, not HEAD), `ship-gate-preflight` and `pr-unreplied-comments`
   (live GitHub - CI/review flip at constant HEAD). Memoizing those returns dangerously stale
   verdicts and defeats ship-gate's fail-closed-freshness purpose.
6. **[KEEP - the star] Instrument the context budget.** A PostToolUse meter tracking cumulative
   window growth, warning at ~70% ("delegate reads, checkpoint") and ~85% ("force checkpoint +
   digest handoff"). Genuinely new *as instrumentation* - it operationalizes the already-
   articulated-but-unmeasured "context budget" concept (P3-D, `SKILL.md` per-task budget).
   Makes every other lever's payoff visible.

## Cross-cutting infrastructure

- **Status oracle** (`orchestrate-status.sh`) - COMPOSES existing digests
  (`pr-unreplied-comments.sh --count-only`, `ship-gate-preflight` compact verdict, `pr-watch`
  one-line) into one line per in-flight PR. Read-only `gh pr list`/`view` ONLY - assert in its
  charter so it never grows a mutation and never becomes a reason to widen `gh pr`.
- **Receipt/finding schema registry** - one versioned source-of-truth schema per artifact under
  `templates/` + a validator, so producers/consumers never drift. Prerequisite for #3/#4/#6.
- **No allow-list broadening:** every actuator (reply-comment.sh, resolve-threads.sh, status
  oracle) flows through existing allow-listed wrappers. The Codoki/CR reaction ack (a `gh api
  reactions` POST) MUST route through a `gh-*` wrapper, never a raw `gh api` that invites a
  broad `Bash(gh api:*)` grant. This design broadens no allow-list rule.

Lower-effort levers (each a point in the when/who 2x2): charter-by-reference (agents read their
charter from a path); delta reports (round-N carries only the change); map-reduce fan-out (N
checkers + one reducer).

## Trust-boundary map

- **Crosses a boundary (TL required, keeps its check):** push, `gh pr create`, posting replies,
  merge. The TL actuates from pre-written artifacts AND runs its own live check - the receipt
  SHA re-verified live + `git status` clean for push; the full thread enumeration for
  merge-ready. Never a glance at a receipt's existence.
- **No boundary (pointer-only, no TL content):** implementer<->adv-review rounds, triage->
  implementer disposition, gate-run->shipper handoff. Content flows agent-to-agent through
  files; the TL schedules and watches liveness. Cheap because the boundary gate re-checks the
  output.

## Open questions / harness-verification prerequisites

- **PreToolUse coverage:** does it fire on `Read` (#1) and on the `Agent`/`Task` tool (#7), and
  can a hook read their structured input + the `$TMUX`-keyed marker? Must be verified before
  either is buildable.
- **Worktree receipt semantics:** consumers must run git queries in the *artifact's* worktree;
  the cross-worktree HEAD-mismatch needs an explicit resolution (record + verify the worktree
  path) before #4 ships.
- **Finding-channel ownership across freeze/respawn** - lead-owned, single-writer-per-round;
  confirm the reduction protocol.
- Schema-drift is closed by the registry; build it before the first producer ships.

## Sequenced roadmap (ROI order; process notes)

1. **#2 checkpoint-HEAD trim** (retain the mirror rule for judgment artifacts), **status
   oracle** (composition), **report-by-exception charter change (internal only)** - mostly doc/
   charter; autonomous-tier (status oracle is a small read-only script -> CR-required).
2. **Schema registry** + **#1 read-dedup (steer)** + **digest subagent** + **context-budget
   meter (Bet 6)** - steer/hook mechanisms (advisory), CR-required; pending the PreToolUse
   harness-verification.
3. **#4 gate receipt** (committed-tree-correct, worktree-aware; written by prep-pr, not the
   guard) + **Bet 5 memoization (pure-oracle allowlist)** - `gate-runner` FUNCTION change ->
   CR-required, maintainer merge; TDD harness.
4. **#6 finding channel** (split slices for PR-blindness; internal adv-review loop first) -
   charter + helper FUNCTION change -> CR-required.
5. **#7 foreground containment as a STEER WARN** - advisory hook in `orchestrate-steer.sh`,
   pending harness-verification; CR-required but not a floor deny.

**KILLED and not filed:** Bet 1 (verification-in-the-floor) - do not move push/merge
authorization into the guard under any form. If ever revisited it owes the full self-imposed
carve-out (maintainer merge + K=2 ralph) AND a written answer to "why is a `touch`-able file an
acceptable authorization token for the one irreversible action" - which the reviewers judged
unanswerable under the stated threat model.

Every surviving item drains per the CLAUDE.md DRAIN PROCEDURE (hostile review -> issue ->
drain). This doc gets its own tracked impl issue referencing it. The three adversarial reviews
that shaped this revision are the hostile-review record for the design itself.
