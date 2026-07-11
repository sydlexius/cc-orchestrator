# Design: orchestration merge gate — readiness (deterministic) vs authority (human)

Date: 2026-07-10
Status: DRAFT (brainstorm) — direction chosen with the maintainer 2026-07-10; pre-decomposition.
NOT adversarially reviewed yet (the floor-touching slice MUST survive an independent hostile
pass + K=2 dry rounds before any implementation; see Rigor).
Issue: #262
Companion:
- `scripts/orchestrate-guard.sh` — the deterministic floor (Tier-2 marker-gated merge deny:
  `is_pr_merge` #105 + `is_merge_api`). The artifact this design relaxes.
- `scripts/ship-gate-preflight.sh` — the deterministic readiness oracle (#110; #132 CR
  outside-diff, #117 reviewDecision, #239 codecov). The artifact this design HARDENS.
- `CLAUDE.md` / `skills/orchestrate/templates/gates.toml.md` — the floor-never-reads-the-receipt
  invariant this design deliberately (and narrowly) relaxes, with the maintainer's recorded consent.
- `DESIGN-deterministic-floor.md` — the floor's threat model ("honest bot on the obvious path,
  NOT adversarial evasion") this design leans on.

## Problem

The marker-active (orchestration) merge rule bundles two separable guarantees:

- A **readiness gate** — "is this merge safe?" Already deterministic via `ship-gate-preflight.sh`
  (CI rollup + 0 actionable review-body findings incl. CR outside-diff + `reviewDecision`
  coupling + Codoki root ack).
- An **authority gate** — "must a human pull the trigger?" In a marker session the floor
  hard-denies both the `gh pr merge` CLI (`is_pr_merge`, #105) and merge-by-API
  (`gh api ... pulls/N/merge`), so the human merges from a separate plain terminal or the
  GitHub UI.

The maintainer wants to keep the readiness guarantee but drop the authority friction. The
friction is worse than "open another terminal": copying the PR number and the merge command
between windows relies on terminal copy, which the maintainer has disabled (mouse-clicks off,
to fix a broken-copy interaction). So the escape hatch — "merge from a separate plain terminal"
— costs a window switch AND a broken copy. **"Frictionless" here must mean: no window switch,
no copy-paste, minimal typing, the merge executed in-session.**

The obstacle the issue names precisely: the thing that enforces the restriction *unbypassably*
is the floor, and a standing invariant is that **the floor is NEVER taught to read the gate
receipt** (it must not trust an artifact an agent could forge). So today the floor's only
un-forgeable answer is "no agent at all" = human-only.

## Principle: the two axes are orthogonal; solve each where it belongs

> **Readiness is a deterministic computation. Authority is a human decision. Bundling them into
> one "human-only" rule over-couples them: the human is forced to re-adjudicate readiness (which
> a machine already settles) merely to exercise authority (which only a human can hold).**

Split them:

- **Readiness** → a hardened, fail-closed, deterministic oracle (`ship-gate-preflight.sh`). Its
  output is the machine's answer to "is it safe," and it must be trustworthy enough that a human
  need not re-derive it.
- **Authority** → the human's explicit "merge" instruction, realized as a short **in-session**
  action the lead consumes — not a separate-terminal round trip.

The floor's job is neither of these; it is to keep an *honest* lead from merging on its own
rationalized "looks ready" judgment (the historical failure: a lead declared "merge-ready" with
a red non-required check and rationalized it away). Once readiness is DETERMINISTIC rather than
the lead's opinion, the human's remaining role collapses to a pure "yes, go" — and, under the
floor's stated **honest-bot** threat model, a short in-session confirmation is sufficient for
that, because an honest lead does not self-authorize a merge it was not told to make.

## The pivotal constraint (why the obvious options are wrong)

The floor **cannot distinguish a human-authored command from a bot-authored one** inside a
session — both are `Bash` tool calls under the same `$TMUX`. Two consequences drove the design:

1. **There is no un-forgeable in-session human signal** (the `!` bash-mode escape *might* be one
   — a bot cannot type `!` — but whether `!` routes through the PreToolUse floor is undocumented;
   Anthropic docs do not settle it, and we chose NOT to hinge the design on it. See Rejected
   Alternatives / Open experiment).
2. **Any in-session "arm the merge" token is therefore forgeable by the lead.** Under a strict
   never-trust-artifact invariant this kills the token approach. Under the **honest-bot** threat
   model — explicitly in scope for the floor — forging is out of scope, so a token an honest lead
   writes only via the real authorization path is acceptable. **The maintainer accepted this
   residual explicitly (2026-07-10): "if I see shenanigans, I'll follow up with a request to
   close that door."** The tightening path is recorded in "Reversal / closing the door" below.

## Chosen direction

**Keep authority human (the human's explicit "merge" instruction is the trigger), execute the
merge in-session, and gate it on a hardened deterministic readiness oracle — with the floor
staying deny-only, local, and fail-open (never taught to do network I/O).**

Concretely, two independent pieces:

### Piece A — harden the readiness oracle (`ship-gate-preflight.sh`)

Independently valuable; ships first; no floor change.

1. **Add a review-thread enumeration gate.** Today the oracle reads only
   `statusCheckRollup,reviewDecision`. It must ALSO query GraphQL `reviewThreads` and BLOCK on
   any `isResolved == false`. This is a first-class, explicitly-enumerated condition — NOT the
   same as "0 unreplied bot findings" (a thread can be replied-to yet unresolved, and vice
   versa) — and it must NOT depend on GitHub branch-protection / `mergeStateStatus` being
   configured (the floor must not assume repo settings). **Fail-closed:** an unreadable or
   partial/paginated-truncated thread list BLOCKS; only a complete enumeration showing zero
   `isResolved == false` PASSES.
2. **Emit the validated head SHA.** The oracle's single `gh pr view` fetches
   `statusCheckRollup,reviewDecision,reviewThreads,headRefOid` in ONE atomic snapshot, so on
   PASS it can attest a single coherent fact — *"these checks were green AND these threads
   resolved, FOR commit X"* — by printing a parseable `headRefOid=<sha>` on its RESULT line.
   This is what binds {checks, threads, SHA} together: a downstream step that pins THIS emitted
   SHA is guaranteed to be merging exactly the commit whose gates the oracle cleared. Resolving
   the SHA in any separate read (a second `gh pr view`) reopens that binding gap and is
   rejected. The SHA is emitted only alongside `exit 0`; a BLOCK emits none.
3. **Scoped exit-path hardening review.** Because the oracle becomes load-bearing for the
   irreversible step, audit every exit path: prove no spurious `exit 0` on a lookup error,
   partial GitHub response, or adversarial PR-body / bot-comment input; confirm fail-closed on
   ambiguous/unreadable state. Re-verify the #132 (CR outside-diff), #117 (reviewDecision), and
   #239 (codecov signal) corrections hold under adversarial input.

The oracle already fails closed thoroughly (every `gh`/`jq` error → `exit 2`); this pass makes
that a *proven* property and closes the `isResolved` gap.

### Piece B — the authorization channel (floor-checked, network-free)

A new deliberate step plus a cheap local floor check. Network readiness work lives in the
deliberate step, NEVER in the floor.

1. **`orchestrate-authorize-merge.sh <pr>`** (new helper; run by the lead when the human says
   "merge"). It:
   - runs the hardened `ship-gate-preflight <pr>` (fail-closed) — the network readiness check,
     done here, outside the hot floor path;
   - ONLY on PASS **captures the `headRefOid=<sha>` the oracle emitted** (greps the RESULT line;
     does NOT do a second `gh pr view` — the emitted SHA is definitionally the one whose gates
     just passed, per Piece A(2)) and writes a session-scoped token under
     `FLOOR_DIR/merge-auth/<session-key>` containing `{pr, head_sha, expiry}`, keyed by the same
     `$TMUX` sanitization the marker uses, with a short TTL (minutes).
2. **The merge is SHA-pinned.** The relaxed merge path REQUIRES
   `gh pr merge <pr> --squash --match-head-commit <head_sha>`. `gh` itself refuses the merge if
   HEAD has moved (closes the authorize→HEAD-moves TOCTOU at the GitHub side).
3. **The floor check (`orchestrate-guard.sh`, the `is_pr_merge && marker_active` branch):**
   before denying, parse `<pr>` and the `--match-head-commit` SHA from the command (local
   regex), read the token file (local), and **ALLOW iff** the token is fresh AND `token.pr == pr`
   AND `token.head_sha == sha`. Otherwise **DENY** (today's behavior). The floor does **no
   network I/O**: it compares the command's pinned SHA against the token's SHA, both local. `gh`
   enforces that the pinned SHA is still HEAD.

Flow (what the maintainer types): to the lead, "merge 262". The lead runs
`orchestrate-authorize-merge 262` (→ hardened preflight → token) then
`gh pr merge 262 --squash --match-head-commit <sha>` (→ floor sees a matching token → ALLOW →
merged). No window switch, no copy, one short instruction. The human's "merge 262" is the
authority; the token + preflight guarantee it only lands if deterministically ready and only at
the exact reviewed SHA.

## Decisions the issue required to be settled

### Fail-open vs fail-closed

- **The readiness oracle fails CLOSED** (Piece A): any unverifiable state — gh/jq error, partial
  response, unreadable/partial thread list, non-numeric count, unrecognized enum — BLOCKS. This
  is already the oracle's posture; the hardening pass proves it and extends it to threads.
- **The floor stays fail-OPEN globally, but the token check is deny-on-doubt.** The floor is NOT
  inverted: it never does network I/O, so it never faces "GitHub is flaky → open or closed?" The
  only new local decision is "is there a fresh matching token?" — and any doubt (missing,
  unreadable, malformed, TTL-expired, SHA-mismatch) resolves to **no token → DENY**, the current
  default. The pre-existing global fail-open residual (a guard *crash* before reaching the
  merge branch exits 0 = allow) is UNCHANGED by this design — it is the same residual the floor
  has today, accepted under the honest-bot model, and NOT worsened. We deliberately do NOT make
  the merge path globally fail-closed: that is the Option-2 inversion this design exists to avoid.

### Tier-scoping (non-CR only, or all merges?)

**Decision: tier-agnostic authority.** The human's explicit "merge" instruction is the trigger
for all tiers; the *readiness* differences between tiers (a CR-required PR needs the Codoki ack,
`reviewDecision` coupling, resolved threads, all checks green) are already encoded in the
deterministic oracle. Because readiness — the only thing the tier affected — is now deterministic
and fail-closed, no separate authority carve-out per tier is needed: the CR-required "a human
authorizes" guarantee is satisfied by the human's in-session "merge" instruction, and in an
orchestration session the human at the console IS the maintainer. **Maintainer may elect at
sign-off to scope the first rollout to non-CR merges only** (leaving CR-required merges
out-of-band until the mechanism proves out); the mechanism supports either without change.

### The never-trust-artifact relaxation (the crux)

This design teaches the floor to read a token an in-session agent produced — a narrow,
deliberate relaxation of "the floor is NEVER taught to read the gate receipt." It is justified
ONLY by the honest-bot threat model and is bounded:

- the token is not a *readiness* receipt the floor trusts for safety — safety comes from the
  SHA-pinned merge + the fact the token is only writable via the real preflight-gated path;
- the token is *authority + binding* metadata (which PR, which SHA, still-fresh), all of which
  the floor verifies against the live command locally;
- forging the token (writing it without running the real preflight) is possible for a dishonest
  or buggy lead — **explicitly out of the threat model, accepted by the maintainer, reversible.**

## Reversal / closing the door

If a bot ever forges authorization ("shenanigans"), the door closes by reverting to un-forgeable
authority without touching Piece A:

- **Minimal:** drop Piece B's token relaxation → the floor's marker-gated merge deny returns to
  today's human-only, out-of-band merge. Readiness stays hardened (Piece A is independent).
- **Stronger:** require the authorization to arrive on a channel the lead cannot forge — the `!`
  bash-mode escape IF the open experiment confirms it bypasses the floor (a human-exclusive
  channel), or an out-of-band arm (separate terminal / GitHub UI) as today.

## Rejected alternatives

1. **Status quo (human-only, out-of-band).** Zero new surface, but the friction — window switch
   + broken copy — is exactly the pain. Rejected as the target state; retained as the
   close-the-door fallback.
2. **Option 2: the floor recomputes the gate itself** (shell out to `ship-gate-preflight` from
   inside the hook; allow iff exit 0). Rejected as the primary mechanism: it puts network I/O and
   a fail-CLOSED decision into a local, fail-OPEN PreToolUse hook — inverting the floor's core
   invariant on the one irreversible action. Worst case: a hook timeout that defaults to allow
   turns a slow `gh` call into "wave through an unverified merge." Piece B achieves the same
   un-forgeable-readiness goal by moving the network recompute OUT of the floor (into the
   deliberate authorize step) and having the floor verify only a cheap local SHA-pinned token.
3. **Pure session-armed token with no readiness coupling** (Option 3 alone). Rejected: arming a
   blanket "merge-allowed" token decouples authority from readiness, so an armed session could
   merge an *unready* PR. Piece B couples them: the token is only writable when the hardened
   preflight passed, and it is bound to the exact reviewed SHA.

## Open experiment (does NOT block this design)

Whether the `!` bash-mode escape routes through the PreToolUse floor is undocumented. A
copy-free, side-effect-free discriminator: with the floor wired, run `! git commit --no-verify
--dry-run -m x` in a repo — a `BLOCKED:` message means `!` routes through the floor; git
dry-run output means `!` bypasses it. If it bypasses, it unlocks a stronger close-the-door
option (a human-exclusive `!ok` authorization). This is recorded, not required.

## Acceptance criteria (issue #262) — mapping

- [x] Design-of-record comparing the options against the tradeoffs, with a chosen direction and
  rationale — this document.
- [x] Fail-open-vs-fail-closed semantics settled and justified — see "Decisions".
- [x] Tier-scoping decided (tier-agnostic authority; readiness carries the tier differences) —
  see "Decisions".
- [ ] Scoped `ship-gate-preflight` hardening review (exit-path audit; no spurious PASS on
  lookup error / partial response / adversarial input; fail-closed on unreadable state) —
  scoped here as Piece A(3); EXECUTED in the impl PR.
- [ ] Recomputed gate BLOCKS on any unresolved review thread (`isResolved == false`) as a
  first-class, branch-protection-independent, fail-closed condition — scoped here as Piece A(1);
  IMPLEMENTED in the impl PR.
- [ ] A linked implementation issue referencing this doc, gated on maintainer sign-off of the
  direction — to be filed after sign-off (see Next steps).

## Decomposition / next steps (post sign-off)

Per the decompose-before-building + fold-by-surface rules, land as up to two PRs:

1. **PR 1 — Piece A (readiness hardening):** `ship-gate-preflight.sh` gains the `isResolved`
   thread enumeration + the exit-path audit. Self-contained, independently valuable, CR-required
   (script FUNCTION), maintainer-merge tier (touches the load-bearing merge oracle). Ships first.
2. **PR 2 — Piece B (authorization channel):** `orchestrate-authorize-merge.sh` + the floor
   token check in `orchestrate-guard.sh` + SKILL.md wiring. Edits the deterministic floor and
   the operating model → maintainer-merge tier, FULL floor rigor: TDD harness cases + an
   independent adversarial (ralph-loop) pass converging at K=2 dry rounds, all gates green.

A single linked implementation issue references this doc; PR 2 is gated on maintainer sign-off of
the direction (the never-trust-artifact relaxation is the maintainer's call to ratify).

## Rigor

This edits the deterministic floor AND the operating model, so it is maintainer-merge tier and
gets full floor rigor. Threat model stays "honest bot on the obvious path," but because the merge
is the one unforgeable irreversible step, the hardening bar on Piece A (now load-bearing) and the
adversarial bar on Piece B (the floor relaxation) are higher than for an advisory. No
implementation begins until this direction is signed off.
