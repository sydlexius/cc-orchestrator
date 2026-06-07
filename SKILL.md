---
name: orchestrate
description: Use when scaffolding and running a lead-orchestrated multi-agent session to ship several PRs in parallel. Sets up the team, dispatches PR-blind implementers, an adversarial prep/review gate, and the pr-shipper / pr-triage bot pipeline, each with a fixed model/mode/permission/charter spec, plus a checkpoint/resume protocol. Invoke when the user asks to "orchestrate", run a multi-agent PR push, stand up the pr-shipper/pr-triage pipeline, or scaffold a session "like that one".
---

# Orchestrate: lead-run multi-agent PR pipeline

You are the LEAD (orchestrator). You delegate building and the mechanical PR
lifecycle to single-purpose teammates, and you keep for yourself the decisions
and every privileged outward step. This skill is the playbook + templates for
standing that up and running it to completion.

## When to use
- The user wants to ship MULTIPLE PRs (a milestone push, a cluster of issues) with parallel agents.
- The user asks to stand up the pr-shipper / pr-triage pipeline or to "orchestrate" a session.

## When NOT to use
- A single PR or a quick fix. Use one `Agent` dispatch, or just do it inline.
- Anything where a human is not available to approve merges (the pipeline deliberately stops at merge).

## Prerequisites (verify before launching anything)
1. Agent Teams enabled: `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` + `teammateMode: "tmux"` in settings.json, tmux running.
2. Permissions: the auto-mode bots need specific allow-list entries. Read `templates/required-permissions.md`, diff it against `~/.claude/settings.json`, and PRINT any missing entries for the user to approve. NEVER silently edit settings.json - permissions are the user's to grant. Settings changes may need a session restart to take effect.
3. A clean main and known HEAD. Record it.

## Roles and the per-bot capability matrix (the guardrail core)
Every bot has a FIXED {model, mode, permissions, charter}. Auto mode requires
Sonnet+ (Haiku cannot auto). Read-only guarantees are enforced by the PROMPT
charter, because Agent-Teams teammates SHARE the global allow-list - you cannot
give a teammate a narrower permission set than the lead, so the charter is the
wall. Spawn each from its template charter.

| Bot | Model / Mode | CAN do | CANNOT (charter-enforced) | Charter template |
|---|---|---|---|---|
| implementer (1 per cluster) | issue hints, else Opus / medium; acceptEdits | edit OWN worktree, commit, run local tests, act on fix-instructions | push, any `gh`, see/know the PR or CR (PR-BLIND), merge, touch other worktrees | implementer-charter.md |
| adversarial-prep | Sonnet / auto | run `/prep-pr` (tests, gate, generated-file + coverage), report pass/fail | push, edit code, reply, merge | adversarial-prep-charter.md |
| adversarial-review | Sonnet or Opus / auto, READ-ONLY | run `/pr-review-toolkit:review-pr` in HOSTILE mode, draft findings | any mutation | adversarial-review-charter.md |
| pr-shipper | Sonnet / auto | safe-push ANY stacked branch, `gh pr create`, background `pr-watch.sh`, rate-limit probe | MERGE, post-merge-cleanup, edit code | pr-shipper-brief.md |
| pr-triage | Sonnet / auto, READ-ONLY | `/handle-review` MINUS mutations, draft to /tmp/<team>/pr-triage/, MERGE-READY verdict | apply/reply/resolve/push/MERGE | pr-triage-charter.md |

## Hard invariants (never violate)
- NO bot ever merges. Merge + post-merge-cleanup are the maintainer's/lead's only.
- Per-PR human go at STACK time: a branch is appended to the shipper stack only after the maintainer deems it shippable (UAT punch-list or AskUserQuestion + live URL).
- Lead vets EVERY PR body/title `#N` ref against `gh issue view N` before stacking (small TaskList IDs collide with old issues).
- Implementers are PR-BLIND: they never push, never see the PR/CR. pr-shipper moves all git; pr-triage thinks about reviews; implementers only build/fix.
- Bots background `pr-watch.sh` (Bash run_in_background) and yield - never foreground-block a timeout, never gh-poll in a loop. The pr-shipper and pr-triage DO background pr-watch themselves (verified pattern): each bot runs pr-watch.sh with run_in_background, yields, and is re-invoked on completion. Cap relaunches (~3) + escalate; branch on exit code (pr-watch: 0=settled/blocked, 1=timeout->bounded relaunch, 2=setup-error->STOP+escalate, never retry).
- Read-only bots run unattended safely ONLY because their charter forbids every mutation. Keep the charter ironclad.
- LEAD IS THE SOLE HUMAN-FACING CHANNEL. Teammates NEVER emit an AskUserQuestion or trigger a human permission prompt - they MESSAGE THE LEAD instead. The lead SERIALIZES those and surfaces exactly ONE ask to the maintainer at a quiescent point (not mid-stream of team activity). This exists because concurrent teammate prompts are not FIFO'd: they clobber the input box and overwrite whatever the maintainer is mid-typing. Auto-mode bots plus the deterministic floor are configured precisely so teammates have NOTHING to prompt about (no permission decisions reach them). Batch asks, prefer plain-text asks over UI dialogs during active team work, and never let a teammate steal the human's focus.
- One server instance per agent on a LEASED port (from `orchestrate-resources.py`; never hand-picked); never dev-restart (it pkills all). The encryption key is provisioned as a 0600 file beside the DB, never as an env value (see stillwater profile docs).
- DETERMINISTIC FLOOR (phase 1, INSTALLED): `~/.claude/scripts/orchestrate-guard.sh` is the PreToolUse `Bash` deny authority (deny outranks the shared allow-list). It hard-denies push-to-main/master, bare `--force`/`-f` (non-lease), and `--no-verify` ALWAYS, and hard-denies the MUTATING merge-by-API path (`gh api ... pulls/{n}/merge`) WHILE THIS session's `$TMUX`-keyed marker `~/.claude/orchestrate-floor.d/<sanitized-$TMUX>` is present and fresh (<=72h) (P3-A refcounting: per-session keying so parallel leads never disarm each other and a solo/non-tmux session is never gated; see `DESIGN-phase3a-marker-refcounting.md`). `gh pr merge` itself is NOT hook-gated: it is gated by the ALLOW-LIST (settings.json lists the non-merge `gh pr` subcommands but omits `merge`), so Claude Code PROMPTS the human for it - a human approves their own merge, an auto-mode bot stalls. (A PreToolUse hook on this CC honors a hard deny but IGNORES `permissionDecision:ask`, so the hook cannot prompt - the allow-list does. Verified by live test 2026-06-06; the earlier `ask` approach was rejected.) Tier-1 (push-main/force/no-verify) stays a hard deny. It fails OPEN on any internal error - the determinism guarantee lives in `test-orchestrate-guard.py`, not in fail-closed runtime. STILL CHARTER-LEVEL (NOT on the floor): generic `gh api -X` (the lead needs it mid-session for CodeQL dismiss / `resolveReviewThread`) and PR-blindness. Adversarial evasion (aliases, `$(...)`, wrapper scripts) is explicitly out of scope - the floor catches the honest-but-misaligned bot on the obvious command path, not an actively-evading one. The HIGH lifecycle items below (ref-ownership, single-writer stack, `head_sha` SHA-compare) are codified as charter invariants under the LEAD-DRIVEN model (lead owns git + watch + stack). See `DESIGN-deterministic-floor.md` + `REVIEW-FINDINGS.md`.
- SINGLE REF-ADVANCER. Exactly ONE agent advances a branch's ref: the implementer worktree (commits + any rebase happen THERE). The pr-shipper is PUSH-ONLY - it pushes the branch by name and NEVER rebases, amends, or otherwise rewrites history. Before any fix round, the respawned implementer asserts the worktree exists, its branch matches, and reconciles worktree-HEAD vs `origin/<branch>` (fast-forward or rebase locally in the worktree) BEFORE editing - so the one ref-advancer is always reconciled with the remote it is about to re-push.
- SINGLE-WRITER STACK. Only the LEAD mutates the shipper stack (`/tmp/<team>/stack.json`): the lead appends entries and the lead removes them. The pr-shipper NEVER pops or rewrites the stack - it SIGNALS "shipped #N" (PR number + URL) back to the lead, and the lead does the removal. This keeps a single writer on the stack file so two agents never race it.
- head_sha SHA-COMPARE (pr-shipper hard gate). Before `gh pr create`, the pr-shipper hard-compares the stack entry's `head_sha` to the actual pushed branch HEAD (`git rev-parse origin/<branch>`); on ANY mismatch it REFUSES to open the PR and messages the lead. This catches a stale or wrong stack entry before it becomes a PR.

## Pipeline flow (per issue/cluster)
```
dispatch-map entry
  -> implementer builds (own worktree+port, issue hints) + commits, PR-blind
  -> adversarial-prep gate (/prep-pr) -> fail loops back to implementer
  -> adversarial-review (hostile /pr-review-toolkit:review-pr) -> findings loop back
  -> lead gates SHIPPABLE (maintainer UAT: punch-list or AskUserQuestion + live URL)
  -> lead vets body refs, appends to shipper stack, checkpoints the implementer + tears down the agent (worktree kept until PR merges)
  -> pr-shipper: safe-push branch -> gh pr create -> background pr-watch -> rate-limit probe -> signal "shipped #N" to lead (lead removes the entry)
  -> pr-triage: background pr-watch -> on CR/Greptile, triage (/handle-review minus mutations) -> NOTIFY LEAD with one of two outcomes:
       * MERGE-READY (clean+mergeable) -> lead takes it straight to the maintainer to merge. SHORT-CIRCUIT: no re-review, no implementer respawn.
       * FINDINGS -> lead respawns a FRESH PR-blind implementer on the intact worktree with the fix-list -> commit -> teardown -> pr-shipper re-pushes -> re-watch -> re-triage (loop until MERGE-READY)
  -> maintainer merges (human only)
```

## Convergence loops
Drive each stage to convergence with PLAIN BOUNDED ITERATION in the owning agent's own prompt ("repeat up to N rounds; stop when X"). Every loop MUST have an objective exit and a NUMERIC round cap - this applies to ALL of them: build-until-green, review-until-dry, and the post-PR CR settle.

**pr-watch exit-code branching (every loop that waits on a watch).** Any agent that runs `pr-watch.sh` branches on its exit code, never on stdout heuristics: `0` = settled/blocked (proceed, or handle the block) -> act; `1` = timeout -> a BOUNDED relaunch of the watch, but only while still under the loop's numeric cap; `2` = setup-error (bad args, missing PR, auth) -> STOP and escalate to the lead, NEVER retry on `2` (retrying a setup error just burns the cap). This holds for the lead's own watches and for any watch a bot runs.

**Inner loop delegates to `subagent-driven-development`.** The single-task inner loop - one implementer building a cluster, then the spec-then-quality review pass on its output - follows the `subagent-driven-development` skill (fresh subagent per task; spec review first, then code-quality review). Orchestrate keeps only its own DELTAS on top: PR-blindness, the per-bot permission charter, the persistent-teammate lifecycle, and the outward PR pipeline. F22 CAVEAT (do NOT "fix" this to comply with the sub-skill): orchestrate runs these inner loops in PARALLEL across clusters, which `subagent-driven-development` forbids for shared worktrees - safe here ONLY because each implementer is on a DISJOINT worktree, so the shared-file conflict premise that motivates that skill's no-parallel rule does not hold.
- The `ralph-loop` plugin is a SINGLE session-level loop: one global `Stop` hook + one `.claude/ralph-loop.local.md` per cwd. It CANNOT nest and is NOT agent-scoped. Use it for at most ONE loop in one cwd, ONLY with `--max-iterations N` + a completion-promise (else it runs forever). Do NOT model the pipeline as "nested Ralph loops" - that was an error; use per-agent bounded iteration instead.

- IMPLEMENT loop (implementer, mutating): the `subagent-driven-development` inner loop - `build -> test -> fix` until the prep gate is GREEN (build-until-green). Exit: gate passes. Cap: ~5 rounds.
- REVIEW loop (adversarial-review, READ-ONLY): hostile pass until DRY = K consecutive rounds (default 2) with nothing new. Exit: K clean rounds. Cap total rounds.
- MACRO cluster loop (lead-driven): `implement -> adversarial-prep -> adversarial-review -> (findings? respawn PR-blind implementer with fix-list -> repeat)` until prep GREEN AND review DRY, THEN take to maintainer for the ship gate. Exit: both gates clean.
- POST-PR CR loop: `autofix-pr` (loop `/pr-watch -> /handle-review` until CR settles) - MUTATES, so it is the LEAD's tool, never a read-only bot's.

GUARDRAILS (naive Ralph bites here):
- BOUNDED: every loop has a round cap; on hitting it, STOP and escalate to the lead - do not loop forever (runaway token cost).
- OBJECTIVE EXIT only: green gate, K-clean-rounds, CR-settled. Never "looks done."
- ANTI-THRASH: if the loop oscillates (fix A re-breaks B), stop and surface to the lead.
- HUMAN GATE STAYS: a loop converges a branch to SHIPPABLE; it NEVER auto-ships or auto-merges. The maintainer still gives the PR-go and the merge.

## Context discipline (protect every long-lived window)
A Medium-effort Opus lead survives only a few hours before forced compaction, and teammates burn context too. Treat context as a budgeted resource, not free.
- DELEGATE-OR-SUMMARIZE is the default. Any agent (lead OR teammate) pushes context-heavy work to short-lived SUB-AGENTS that return CONCLUSIONS, not transcripts. Context-heavy work = Playwright UAT/screenshots, RCA, big file/log reads + greps, rebase-conflict resolution, hostile review passes, doc sweeps. The long-lived window should hold DECISIONS + the checkpoint, not raw output. This trigger is judgment-based; as a rule of thumb, delegate any task whose raw output would exceed a few hundred lines, or any multi-file read/grep, RCA, UAT, or hostile-review pass.
- PARALLEL by default for INDEPENDENT work: dispatch multiple sub-agents at once when their tasks share no state (matches the parallel-agents-default practice). Run sub-agents FOREGROUND as a rule; BACKGROUND is allowed ONLY for work that is PROVABLY 0% chance of hitting a permission/approval prompt or sandbox denial (pure read-only search/analysis). The standing global background-agent ban applies verbatim here - anything that writes, commits, pushes, or runs `gh`/`git` mutations is foreground.
- RESPAWN FRESH at task boundaries. Followers (implementers, bots) get a per-task CONTEXT BUDGET and are torn down + respawned lean between tasks rather than accreting one bloating window (this is why the implementer is PR-blind and self-contained per fix-list - a fresh copy loses nothing). Checkpoint-then-respawn turns a long job into a series of fresh lean agents.
- The LEAD applies the same rule to itself: keep the lead window for decisions + the living checkpoint; offload investigation to sub-agents that report the decision-relevant summary. (Cross-refs: the lead-delegate-to-preserve-context, parallel-agents-default, no-background-agents, agent-teams-context-recycling, and subagent-internal-error-mitigation memories.)

## Lifecycle details
- Implementer spawn: one per dispatch-map entry, disjoint worktree (`make worktree`), leased port + data dir (from `orchestrate-resources.py allocate`), model+effort from the issue's `[mode:][model:][effort:]` hints (else Opus/medium). Give it ONLY a build task + its charter - never PR/CR context.
- Implementer teardown: checkpoint (commit to its branch) + tear down the AGENT when its branch is stacked. The worktree is KEPT FROM FIRST COMMIT UNTIL ITS PR MERGES (not just "until stacked") - it must survive every fix round. Removing it earlier is the bug this resolves: the SKILL's old "keep for fix rounds" and the teardown step's "leave open-PR worktrees" are the SAME rule stated twice - keep-until-merge.
- Fix round (respawn-on-demand): when pr-triage drafts a fix-list for an open PR, respawn a FRESH PR-blind implementer pointed at the intact worktree, hand it ONLY the explicit fix-list (no PR/CR context needed - that is the point of PR-blindness), let it commit, tear it down, then pr-shipper re-pushes. Lean per context-recycling. RESPAWN PRECONDITION (the implementer asserts before fixing): the worktree exists, its branch matches the expected `<BRANCH>`, and worktree-HEAD reconciles with `origin/<branch>` (see "Single ref-advancer" under Hard invariants). If the worktree is gone (reboot, manual cleanup), recreate it via `make worktree` at the recorded branch BEFORE spawning the implementer.
- Bots (shipper/triage): one each, persist across the session; shut down when their queue is empty AND the user says wrap. cr-planner-style helpers optional.

## Setup sequence
1. Verify prerequisites (above). Print missing permissions.
2. `TeamCreate` the team (e.g. `<milestone>-impl`).
3. Instantiate per-team artifacts under `/tmp/<team>/` (P3-A namespacing, so parallel teams don't clobber): `orchestrate-setup.py up` scaffolds `/tmp/<team>/{stack.json (=[]),pr-triage/,adv-review/,pr-shipper-brief.md}`. (Manual fallback: `mkdir -p /tmp/<team>/pr-triage /tmp/<team>/adv-review`; stack = `/tmp/<team>/stack.json`.)
4. The shipper brief is rendered into `/tmp/<team>/pr-shipper-brief.md` by `up` (from `templates/pr-shipper-brief.md`, repo/spacing/stack filled). `--team` must be a filesystem-safe slug `[A-Za-z0-9._-]+`.
5. Build a DISPATCH MAP (issue/cluster -> {worktree, branch, model, effort}). Scout inline first if the work-list is unknown. Do NOT hand-pick ports or data dirs - those are leased in step 6.
6. For each implementer in the map, the LEAD runs:
   ```
   orchestrate-resources.py allocate --session <team> --teammate <name> --profile stillwater [--provision]
   ```
   This prints the lease JSON on STDOUT (machine-readable) and the eval-able `export KEY=VALUE` block on STDERR (by design - stdout stays pure JSON). The LEAD reads the STDERR block and exports those env vars into the teammate's tmux pane as REAL environment. This is the AUTHORITATIVE delivery: it wins over any `.env` file (per D6 precedence - dev-restart.sh already prioritizes exported env over .env). Ports and data dirs are collision-free leases; never hand-pick fixed values. The written `lease.env_file` is a durable fallback record only.
   Then spawn implementers from the map (charter + build task only). Spawn pr-shipper + pr-triage from their charters when the first branch nears shippable.
7. Run the pipeline. Maintain the checkpoint block (`templates/SESSION-STATE.checkpoint.md`) continuously.

## Checkpoint / resume / teardown
- Keep a living checkpoint at the TOP of the session-state/plan doc using `templates/SESSION-STATE.checkpoint.md`. Update it as PRs ship and decisions land. Mirror any /tmp-only artifact (triage findings) into the durable doc - /tmp clears on reboot.
- Teardown: `shutdown_request` each teammate -> WAIT for the "terminated" notice -> only then `TeamDelete` (it refuses while a member is alive). Keep every worktree until its PR MERGES (the keep-until-merge rule above) - never remove a worktree whose PR is still open. Then run `orchestrate-setup.py down --team <team>` to best-effort release the session's resource leases (non-fatal on failure).
- Resume: read the checkpoint block FIRST, then re-spawn only what is needed.

## Floor-friction feedback log (standing rule)
When a deterministic-floor gate (or any hard gate) BLOCKS something that was legitimate, OR you
spot a convenience that would NOT sacrifice security, the LEAD logs it to
`~/Developer/cc-orchestrator/orchestrate-session-feedback.md` (local, gitignored running log; see its
header for the entry format). Log BOTH orchestration-specific frictions and general ones.
Teammates that hit a blocked gate surface it to the lead, who records it (teammates do not write
the log directly). This is the triage queue: entries get folded into guard/skill/charter fixes -
the Tier-2 merge `ask` circuit-breaker came from exactly such a logged block (the floor was
denying the human's own `! gh pr merge`). The security bar for any "convenience" entry: it must
preserve the floor's guarantees (human-authorized merge, NO autonomous bot merge, and the
always-on push-main/force/no-verify denies). Append, never rewrite; triage separately.

## References
- Templates live in `templates/` next to this file.
- Companion memories (this machine): the dedicated-pr-pipeline-bots pattern, pr-bots-background-pr-watch, team-prompt-clobbering, pr-body-task-id-vs-issue-collision, agent-teams context recycling.
