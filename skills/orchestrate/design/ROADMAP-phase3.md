# Phase 3 Roadmap (decomposition) - orchestrate skill

Date: 2026-06-05
Status: **DECOMPOSITION ONLY - implement later.** Each sub-project below is its own
spec -> plan -> build cycle (like Phase 1/2). Item details live in `REVIEW-FINDINGS.md`.
Companions: `DESIGN-deterministic-floor.md` (Phase 1), `DESIGN-phase2-setup.md` (Phase 2),
`REVIEW-FINDINGS.md` (the source findings).

## Goal / end-state (maintainer-chosen 2026-06-05): ROBUST LEAD-DRIVEN, including PARALLEL sessions

The lead stays human-in-the-loop (approves every merge + ship gate); bots build/advise. Phase 3
makes THAT safe and multi-session-clean - it is NOT aiming for a fully unattended pipeline. This
reprioritizes the original tranche:
- **Multi-session safety comes FIRST** - it is demonstrated-needed (a real parallel session,
  `m55-warm-fronts`, ran during the Phase-2 build and the single global marker was accidentally
  disarmed for it).
- **The port allocator moves UP** into the multi-session group - parallel leads collide on dev-server
  ports, so it is a cross-session concern, not just lifecycle polish.
- **Ref/stack-ownership enforcement drops to LAST** - in a lead-driven model the lead already owns
  the stack and vets the SHA at stack time, so those items are mostly documentation of the
  lead-driven model + belt-and-suspenders deterministic enforcement (they matter most only if the
  goal later shifts toward unattended).

## Build order: P3-A -> P3-B -> P3-C

P3-A and P3-C are largely independent; P3-B is mostly prose. P3-A carries the most CODE (and touches
the always-on security guard again), so it gets the most rigor.

---

## P3-A: Multi-session / parallel-lead safety (HIGHEST - do first)

**Why first:** the gap that makes parallel lead-driven sessions actually safe; demonstrated live.

> STATUS 2026-06-06: marker REFCOUNTING sub-item DONE (built + 5-round adversarial
> critic loop converged; spec `DESIGN-phase3a-marker-refcounting.md`, plan
> `PLAN-phase3a-marker-refcounting.md`; committed in claude-kit, not pushed). The marker
> is keyed by the WHOLE sanitized `$TMUX` (not `<team>` - the guard can't know the team),
> TTL 72h, empty `$TMUX` never gated. /tmp TEAM-NAMESPACING sub-item ALSO DONE 2026-06-05
> (spec `DESIGN-phase3a-tmp-namespacing.md`; artifacts now under `/tmp/<team>/`
> {stack.json, pr-triage/, adv-review/, pr-shipper-brief.md} + `--team` slug validation;
> single critic pass, committed not pushed). RESOURCE REGISTRY/ALLOCATOR sub-item ALSO
> DONE 2026-06-06 (`orchestrate-resources.py` + `test-orchestrate-resources.py`;
> `allocate/release/gc/list` CLI; stillwater profile; `orchestrate-setup.py down`
> best-effort releases the session's leases; spec `DESIGN-phase3a-resource-registry.md`).
> MARKER-AWARE `/merge-pr` ALSO DONE 2026-06-06 (`merge-pr.md` Step 3 now branches on a
> guard-mirrored `marker_active` check: active session -> prints `! gh pr merge <pr> --squash`
> for the human + verifies MERGED before cleanup; solo -> merges directly). **P3-A COMPLETE.**

- **Per-session marker REFCOUNTING.** [DONE] Replace the single `~/.claude/orchestrate-floor.active` with a
  directory `~/.claude/orchestrate-floor.d/<key>` (one file per active session, key = sanitized `$TMUX`). `orchestrate-guard.sh`
  `marker_active()` becomes "ANY fresh file (<=TTL) in the dir." `orchestrate-setup.py down` removes
  ONLY its own `<team>` file, so one session's teardown never disarms another's. Keep the env override
  (`ORCHESTRATE_FLOOR_MARKER` -> a dir, backward-compatible: a plain file still works).
  - Touches: `orchestrate-guard.sh` + `test-orchestrate-guard.py` (the always-on security hook - same
    test-driving-isolation discipline + engage-ralph-loop as Phase 1), `orchestrate-setup.py` +
    its harness.
  - Foundational: the rest of P3-A composes on it.
  - **Maintainer note (2026-06-06):** the single global `~/.claude/orchestrate-floor.active`
    currently PREVENTS parallel orchestrations (one marker = one session). Maintainer wants this
    expanded and suggests keying the per-session marker on the **`$TMUX`** session env var (they
    only use tmux-based team agents, so `$TMUX` is a reliable per-session discriminator). Design
    sketch: marker filename derived from `$TMUX` (e.g. `orchestrate-floor.d/<sanitized-$TMUX>` or
    `<team>@<tmux-id>`); `marker_active()` = ANY fresh file in the dir; `down` removes only this
    session's file. Open Qs to resolve in design: `$TMUX` format/stability across panes vs windows
    vs sessions (it encodes socket,pid,session - pick the right field); fallback when `$TMUX` is
    empty (non-tmux invocation -> fall back to `<team>` keying, never silently share one marker).
  - **HARD REQUIREMENT (maintainer 2026-06-06): standalone (unorchestrated) sessions must be IMMUNE
    to another session's tombstone.** A stale/abandoned marker from one orchestrate session must NOT
    gate an unrelated solo session. `marker_active()` must check whether THIS session is an
    orchestrate session (its OWN `$TMUX`-keyed marker exists), NOT "does ANY marker exist anywhere"
    (the current global-`active`-file semantics). With per-session keying a solo session has no
    matching key -> never gated, regardless of any tombstone. Note the blast radius already SHRANK
    after the merge rework: the ONLY marker-gated behavior left is the merge-by-API hard deny
    (`gh pr merge` is allow-list-gated -> prompts regardless of marker; Tier-1 is marker-independent),
    and the 24h TTL auto-expires a tombstone - but session-scoping (not just time-scoping) is the
    real fix and is REQUIRED.
- **Team-namespace ALL /tmp artifacts** under `/tmp/<team>/` (stack, `pr-triage`, `pr-shipper-brief`,
  `adv-review`). [DONE 2026-06-05] `orchestrate-setup.py scaffold_artifacts` now nests all four under
  `ARTIFACTS/<team>/` (stack drops its `<team>-` prefix -> `stack.json`); `adv-review/` added; new
  `_validate_team` (first char alnum, `[A-Za-z0-9._-]`) rejects path-unsafe names. Templates/charters
  (`pr-triage-charter`, `pr-shipper-brief`) + SKILL setup-sequence updated to the new paths.
- **Cross-session PORT ALLOCATOR (generalized: resource registry / allocator).** [DONE 2026-06-06]
  `orchestrate-resources.py` implements port + data-dir leasing across sessions: `allocate` (lease
  JSON on STDOUT; eval-able `export` block on STDERR) + `release` + `gc` + `list`; stillwater profile
  emits SW_PORT/SW_DB_PATH/SW_BACKUP_PATH/SW_MUSIC_PATH/SW_LOG_* with the encryption key as a 0600
  file (never in env); `--provision` seeds the DB + key symlink; `orchestrate-setup.py down` best-effort
  releases the session's leases. Spec: `DESIGN-phase3a-resource-registry.md`.
- **Marker-aware `/merge-pr` (prep + hand-off).** [DONE 2026-06-06] When the marker is active,
  `/merge-pr` does all its checks but PRINTS the exact `! gh pr merge <pr> --squash` for the human to
  run (the one irreversible step stays human-executed, unforgeable by a bot), verifies the PR is
  `MERGED`, then resumes post-merge cleanup. Outside a session (or non-tmux), unchanged. Step 3 of
  `merge-pr.md` now branches on a `marker_active` check that mirrors the guard's `marker_active()`
  byte-for-byte (`$TMUX`-keyed, `LC_ALL=C` sanitize, `$FLOOR_DIR`, `$TTL_HOURS` fresh; bad TTL never
  disarms). Detection verified across 7 cases (fresh/stale/no-marker/no-tmux/bad-TTL).

**Rigor note:** the guard change re-touches the security floor - keep Phase-1's harness green, add
refcount cases (one session armed / two armed / one tears down -> other still armed / all stale ->
inactive), run the engage-ralph-loop. Never put trigger substrings on a Bash command line.

---

## P3-B: Runtime lifecycle robustness (MEDIUM) -- [MOSTLY DONE 2026-06-06: loop caps + pr-watch exit-code branching + worktree keep-until-merge codified in SKILL.md/charters; teardown clean-worktree assertion DEFERRED (code, needs design)]

**Why second:** useful for long/parallel sessions, but the lead catches most of this in a lead-driven
model. Mostly charter/prose + one teardown check.

- **Loop caps + pr-watch exit-code branching.** Numeric cap on every loop (build-until-green,
  review-until-dry, post-PR CR); branch on `pr-watch` exit (0=settled/blocked, 1=timeout -> bounded
  relaunch, 2=setup-error -> STOP+escalate; never retry on 2). Touches: `SKILL.md` + bot charters.
- **Worktree keep-until-merge.** Keep a worktree from FIRST commit until the PR MERGES (not just
  "until stacked"); respawn asserts/recreates it. Resolves the SKILL "keep for fix rounds" vs teardown
  "leave open-PR worktrees" inconsistency. Touches: `SKILL.md` lifecycle + charters.
- **Teardown clean-worktree assertion.** Assert `git status --porcelain` empty + `head_sha` match
  before teardown (don't shut down over uncommitted work). Touches: `orchestrate-setup.py down` +
  `SKILL.md`.

---

## P3-C: Ref + stack ownership enforcement (LOWEST under lead-driven) -- [DONE 2026-06-06: single ref-advancer + single-writer stack + head_sha SHA-compare codified in SKILL.md + pr-shipper/implementer charters]

**Why last:** in a lead-driven model the lead already owns the stack + git refs + vets the SHA at
stack time, so these are largely DOCUMENTATION of the lead-driven model + belt-and-suspenders
deterministic enforcement (they earn their keep mainly if the goal later moves toward unattended).

- **Single ref-advancer.** Name ONE ref-advancer (the implementer worktree); pr-shipper is push-ONLY,
  never rebases; respawn precondition = assert worktree exists + branch matches + reconcile
  worktree-HEAD vs `origin/<branch>` BEFORE fixing. Touches: charters + `SKILL.md`.
- **Single-writer stack.** Only the lead mutates the stack; the shipper signals "shipped #N" rather
  than popping. Already the lead-driven default - mostly codify + guard against a bot write. Touches:
  charters.
- **`head_sha` SHA-compare ENFORCEMENT.** pr-shipper hard-compares the stack entry's `head_sha` to the
  pushed branch HEAD before `gh pr create`, and refuses on mismatch. Builds on Phase-1's
  `head_sha`-required schema. Touches: pr-shipper charter/behavior + a small compare check.

---

## P3-D: Context-budget delegation (NEW TODO, maintainer-requested 2026-06-06) -- [DONE 2026-06-06: "context discipline" section in SKILL.md + delegate-or-summarize line in every charter]

**Problem (observed):** the lead/orchestrator on a Medium-effort Opus model survives only
~2-4h before compaction is forced; teammates burn context too. The roster must be far more
protective of context limits.

**Goal:** make the lead AND followers delegate aggressively to short-lived sub-agents that
return CONCLUSIONS (not transcripts), so the long-lived agents keep lean windows and run
longer between compactions. Bake the delegation discipline into the charters/SKILL, not just
ad-hoc practice.

**Direction to ideate (do NOT build yet):**
- Default to PARALLEL sub-agents for independent work (matches `feedback_parallel_agents_default`).
  FOREGROUND for the most part; BACKGROUND ONLY where it is PROVABLY 0% chance of a
  permission/approval prompt or sandbox denial (pure read-only search/analysis) - the standing
  `feedback_no_background_agents` / global background ban applies verbatim.
- Lead: push context-heavy work to sub-agents that return only the decision-relevant summary -
  UAT/screenshots, RCA, log greps, rebase-conflict resolution, hostile review, doc sweeps
  (extends `feedback_lead_delegate_to_preserve_context`). The lead's window should hold
  DECISIONS + the checkpoint, not raw output.
- Followers (implementers/bots): each already delegates context-heavy work to its OWN one-shot
  sub-agents (implementer-charter:18) - generalize this into an explicit "delegate-or-summarize"
  rule with a per-task context budget, and respawn-fresh at task boundaries
  (`feedback_agent_teams_context_recycling`).
- Consider a "context budget" knob per role + a checkpoint-then-respawn cadence so a long job is
  a series of fresh lean agents rather than one bloating window.
- Open question to resolve in design: when to checkpoint+respawn the LEAD itself vs delegate
  more; how to measure remaining budget; whether a dedicated "summarizer" sub-agent role helps.

**Touches:** `SKILL.md` (lifecycle + a new "context discipline" section), all charters
(implementer/adversarial-review/adversarial-prep/pr-shipper/pr-triage), and the dispatch model.
Cross-refs memories: `feedback_lead_delegate_to_preserve_context`,
`feedback_parallel_agents_default`, `feedback_no_background_agents`,
`feedback_agent_teams_context_recycling`, `feedback_subagent_internal_error_mitigation`.

---

## P3-E: Stop human-facing prompts from clobbering the input box (NEW TODO, maintainer 2026-06-06) -- [DONE (in-skill) 2026-06-06: "lead is the sole human-facing channel" invariant in SKILL.md + all teammate charters. Research finding: NO harness serialization/input-buffer fix exists in CC 2.1.167; auto-mode + PermissionRequest hooks are COMPLEMENTS for permission dialogs (candidate follow-up); in-skill is the ONLY fix for AskUserQuestion clobbering.]

**Problem (acute, maintainer-flagged):** during active team work the FIREHOSE of teammate
approval prompts + AskUserQuestion dialogs is not FIFO'd - they clobber the input box and overwrite
whatever the maintainer is mid-typing, which makes AskUserQuestion (a convention the maintainer
LIKES) "absolutely useless." Existing memory: `feedback_team_prompt_clobbering`.

**Goal:** the maintainer can reliably read + answer ONE human-facing prompt at a time without a
later agent request stealing focus or clobbering their in-progress response.

**Direction to ideate (do NOT build yet) - direct AND orthogonal angles:**
- ORTHOGONAL (design, in-skill, cheapest): make the LEAD the SOLE human-facing channel.
  Teammates NEVER emit AskUserQuestion or a permission prompt to the human - they MESSAGE THE LEAD,
  who SERIALIZES and surfaces exactly one ask at a quiescent point. Reinforce auto-mode bots (no
  permission prompts) + the deterministic floor so teammates have nothing to prompt about. Batch
  asks; avoid UI prompts during active team work (extends `feedback_team_prompt_clobbering`).
- ORTHOGONAL (cadence): a "quiet window" / barrier where teammates queue messages and the lead
  drains them between AskUserQuestion rounds, so a dialog is never interrupted mid-answer.
- DIRECT (harness, may be out of our control): a setting to QUEUE/serialize tool-permission +
  question prompts instead of clobbering; preserve the input buffer across an incoming prompt.
  Investigate whether CC exposes prompt-queueing / focus-preservation config; if not, file upstream.
- Verify which prompts actually clobber (teammate permission approvals vs AskUserQuestion vs
  PushNotification) before designing - the fix differs per source.

**Touches:** `SKILL.md` (a "human-facing channel = lead only" invariant), all teammate charters
(forbid direct human prompts; route to lead), the dispatch/cadence model. Cross-ref memories:
`feedback_team_prompt_clobbering`, `feedback_pr_go_punchlist_or_questions`,
`feedback_no_pr_without_explicit_approval`. Pairs naturally with P3-D (both are about disciplined
lead-mediated interaction).

---

## P3-F: Replace broad `gh api *` with deterministic wrapper scripts (NEW TODO, maintainer 2026-06-06)

**Maintainer preference:** dislikes the broad `Bash(gh api *)` allow-list entry; prefers
DETERMINISTIC wrapper scripts that expose only specific, audited `gh api` operations (the existing
`reply-comment.sh` / `resolve-threads.sh` / `pr-unreplied-comments.sh` pattern), then allow-list the
WRAPPERS, not raw `gh api`.

**Why it matters / payoff:**
- Shrinks the merge attack surface: if raw `gh api` is not allow-listed and no wrapper performs a
  merge, then the guard's merge-by-API hard-deny becomes UNNECESSARY - the obscure bot merge path
  is closed by construction, not by a pattern-matching hook.
- That in turn could remove the LAST marker-gated behavior in the guard, which simplifies the
  tombstone/standalone-immunity story (P3-A): with nothing marker-gated, a solo session is immune
  to a tombstone trivially.
- More auditable + deterministic (matches the safe-push.sh philosophy).

**Direction to ideate (do NOT build yet):**
- Inventory every `gh api` call the lead/bots actually need (CodeQL dismiss, `resolveReviewThread`,
  review/comment reads, check-runs, code-scanning). Group into a small set of intent-named wrappers
  (e.g. `gh-codeql-dismiss.sh <alert>`, `gh-resolve-thread.sh <id>`, read-only `gh-api-get.sh`).
- Each wrapper validates its args and performs ONE deterministic operation; NONE perform a merge.
- Replace `Bash(gh api *)` in settings.json with `Bash(~/.claude/scripts/gh-*.sh *)` (or the repo
  `scripts/` equivalents). Then drop the guard's merge-by-API deny + revisit the marker's purpose.
- Open Qs: read-only `gh api` GETs are many/varied - a single `gh-api-get.sh` that refuses any
  `-X/--method/-f/--field` (mutation flags) may be cleaner than enumerating every GET.

**Touches:** new wrapper scripts (claude-kit), `required-permissions.md`, settings.json allow-list,
and POSSIBLY `orchestrate-guard.sh` (remove merge-by-API deny once raw `gh api` is no longer
allow-listed). Pairs with P3-A (removing the last marker-gated behavior).

**Deployment (maintainer responsibility, standing rule):** every wrapper is CREATED in
`~/Developer/claude-kit/` (canonical) and DEPLOYED by symlink into `~/.claude/scripts/` - never a
copy (drift). The maintainer of this tooling ensures both steps happen, or instructs the Lead to do
the create-in-claude-kit + symlink when a teammate authors one. See SESSION-STATE "Standing
constraints".

---

## P3-G: Doctor scans the FULL settings cascade for merge-gate shadowing (NEW, demonstrated-needed 2026-06-05) -- [DONE 2026-06-06: orchestrate-setup.py doctor scans the cascade + fails loudly; CC-faithful prefix/glob matcher hardened after an Opus critic found false-negatives (no-space/infix wildcards) + crash-on-malformed-JSON; commits 9df77fa + 58371b2]

**Problem (DEMONSTRATED this session):** the Tier-2 merge gate works by OMITTING `gh pr merge` from the
allow-list so CC prompts the human. But CC UNIONS the allow-list across the ENTIRE settings cascade
(`~/.claude/settings.json` + `~/.claude/settings.local.json` + project `.claude/settings.json` +
project `.claude/settings.local.json`). Any single blanket rule anywhere - `Bash(gh pr:*)`,
`Bash(gh pr *)`, `Bash(gh:*)`, `Bash(gh *)`, `Bash(*)` - silently re-grants merge and DEFEATS the gate
with no error. During this session's PENDING-VERIFY, the project `settings.local.json` had TWO such
rules (`gh pr:*` at line 19 AND `gh pr *` near line 1004, different syntaxes, far apart), accumulated
from past "always allow" clicks; `gh pr merge` ran with no prompt. Both were narrowed by hand, but the
hole is structural and WILL recur (every "always allow" on a `gh pr ...` command re-adds a blanket rule).

**Goal:** make the invariant ("no allow rule in the cascade matches `gh pr merge`") SELF-CHECKING.
`orchestrate-setup.py doctor` (and `arm`/`up`) parses every settings file in the cascade, simulates
each `Bash(...)` allow rule against the literal `gh pr merge ...`, and FAILS LOUDLY (non-zero, named
file + line) if any rule would grant it. Treat a found shadow as a hard doctor failure, not a warning -
the gate is silently broken until it is removed.

**Direction to ideate (do NOT build yet):**
- Reuse the cascade-scan logic prototyped this session (Python: load each file's `permissions.allow`,
  normalize `:*` and ` *` suffixes to a prefix, test against `gh pr merge ...`).
- Decide the remediation UX: doctor PRINTS the offending file+rule and the exact narrowed replacement
  (enumerate non-merge subcommands), or offers to rewrite it. Never auto-edit a user settings file
  without showing the diff + consent (standing constraint).
- Document the BEHAVIORAL rule in the human-facing charter: approve the in-session merge with
  "allow once", NEVER "always allow" (the latter is precisely how the blanket rules accumulate).
- Pairs with P3-F: if raw `gh api` is replaced by wrappers AND no allow rule grants `gh pr merge`, the
  merge path is closed by construction and the marker-gated merge-by-API deny can retire.

**Touches:** `orchestrate-setup.py` (doctor cascade-scan + harness cases: clean / `gh pr:*` /
`gh pr *` / `gh:*` / `gh *` / `*` / multi-file), `required-permissions.md` (document the invariant +
"allow once" rule), the human charter. Cross-ref: SESSION-STATE VERIFY-2026-06-05 finding.

---

## Per-sub-project cycle (when implementing later)

Each of P3-A/B/C: brainstorm its own decisions -> `writing-plans` -> subagent-driven build ->
critic/ralph convergence -> commit to the gist. P3-A has real CODE (guard + setup + merge-pr) and a
security-floor change, so it deserves full Phase-1-grade rigor; P3-B/C are mostly charter/prose with a
couple of deterministic checks. Recommended starting point: **P3-A's marker refcounting**, since it is
foundational, demonstrated-needed, and unblocks safe parallel sessions.
