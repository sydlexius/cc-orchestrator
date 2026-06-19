# orchestrate skill - adversarial review findings (2026-06-05, 4 hostile critics)

> SUPERSEDED (#139): `TeamCreate`/`TeamDelete` referenced below were REMOVED by Anthropic. The team is now IMPLICIT (spawn named teammates directly via the `Agent` tool) and teardown is `shutdown_request` -> wait for each "terminated" notice (no `TeamDelete` step). The live `SKILL.md` teardown is authoritative; this historical doc is left as-is.

Dog-food: ran the orchestrate adversarial-review pattern ON orchestrate. 4 parallel
read-only critics (safety/permissions, lifecycle/concurrency, completeness, operational-realism).
Below: synthesized + deduped, prioritized. Items marked [FIXED] were corrected the same night;
the rest are a punch-list for a fresh, rested pass (do NOT trust the skill for an unattended
run until the CRITICALs are addressed).

## CRITICAL - no deterministic safety floor (the systemic hole)
The skill's own honest premise - "read-only/no-merge is enforced by the CHARTER prompt, not
permissions" - IS the hole. The top invariants have ZERO enforcement floor for auto-mode bots:
- **NO-MERGE bypassable** 3 ways, all evading the string-grep guards: `gh pr merge` (allow-listed `gh pr *`; the PostToolUse hook only *prints* after, doesn't block), `gh api -X PUT repos/{o}/{r}/pulls/{n}/merge` (allow-listed `gh api *`; hook greps only literal `gh pr merge`), and via an allow-listed `~/.claude/scripts/*.sh` wrapper. **[CLOSED by deterministic floor - see DESIGN-deterministic-floor.md + orchestrate-guard.sh; merge (gh pr merge any flags + MUTATING merge-by-API on pulls/{n}/merge) is now marker-gated-denied.]**
- **PUSH-MAIN / destructive force bypassable**: `safe-push.sh main --force` / `--no-verify` - safe-push forwards `$@` to `git push`, and the main-block PreToolUse hook matches only literal `git push main`, NOT the wrapper. [FIXED the FALSE CLAIM in required-permissions.md that safe-push is hook-gated against main.] **[CLOSED by deterministic floor - orchestrate-guard.sh now denies safe-push/git-push to main/master, bare --force/-f (non-lease), and --no-verify ALWAYS, covering the safe-push $@-forwarding gap.]**
- **PR-blind / single-pusher bypassable**: implementer holds `Bash(git *)` + `gh pr *`; the `# prep-pr-ok` override is echoed in the block message, teaching the bypass. [Partially mitigated: the floor blocks the irreversible push-main/force; PR-blindness itself stays CHARTER-level - the override echo is on the advisory feature-push gate (a sanctioned action), not on any hard floor deny, so it leaks no bypass to a hard deny.]
- FIX (the real one, for next pass): add PreToolUse DENY hooks for `gh pr merge`, `gh api ... -X (POST|PATCH|PUT|DELETE)`, `gh api .../merge`, and `safe-push.sh` targeting main/`--force`(non-lease)/`--no-verify`. Deny outranks allow and is the ONLY wall for auto bots. Prose is not a wall. **[DONE phase 1: orchestrate-guard.sh written + harness-proven (test-orchestrate-guard.py, 35 case-groups + 4 regressions green) + wired as the PreToolUse Bash hook. SCOPE NOTE: generic non-merge `gh api -X` was deliberately dropped to charter-level - the lead needs it mid-session and the floor cannot tell lead from bot; the floor denies only the IRREVERSIBLE actions (merge + push-main/force/no-verify).]**

## CRITICAL - operational mechanics that do not work as written
- **Ralph cannot nest** [FIXED in SKILL + review charter]: the installed plugin is one global `Stop` hook + one `.claude/ralph-loop.local.md` per cwd; session-level, not agent-scoped, not nestable, runs forever without `--max-iterations`/`--completion-promise`. Use plain bounded iteration in an agent's own prompt for build-until-green / loop-until-dry; reserve the plugin for at most ONE single-cwd loop.
- **background-watch waking an IDLE teammate is UNVERIFIED** [FIXED: downgraded the assertion]: `run_in_background` re-invoke is known for a live session; whether it wakes an idle tmux teammate is unproven. If it doesn't, shipper/triage sleep forever and the pipeline silently stalls. SAFER: the LEAD (live session) owns all pr-watch backgrounding; bots act only when the lead messages them. VERIFY on this machine before relying on bot-owned background watch.
- **allow-list gap will stall auto bots** [FIXED in required-permissions]: doc said `Bash(go *)` but settings has only scoped `go build/test/vet/mod/tool/generate` - a bare `go run`/`go env`/`go list`/`go clean` in the gate prompts, and an auto bot can't answer. Run the gate once in the lead session, capture every Bash command, ensure each is covered. Note `acceptEdits` (implementer) does NOT silence Bash prompts.
- **per-teammate MODE may not be settable** (like permissions): if mode is global/inherited, "auto vs acceptEdits vs read-only per bot" is charter-only too. VERIFY the Agent-tool `mode` param works per-teammate on this CC version; else the READ-ONLY rows carry the same prose-only caveat.

## HIGH - lifecycle / concurrency
- **Ref-ownership race**: implementer commit AND pr-shipper push/rebase both write `refs/heads/<branch>`. Respawn on a kept-but-stale worktree commits on the wrong base, clobbering a rebase or stalling on lease mismatch. FIX: name ONE ref-advancer (the implementer worktree; pr-shipper push-ONLY never rebase); rebases happen in a respawned implementer's worktree; respawn precondition = assert worktree exists, branch matches, and reconcile worktree-HEAD vs origin (reset to origin/<branch>) BEFORE fixing.
- **head_sha not enforced**: safe-push verifies origin == current local HEAD, NOT the stack's approved `head_sha` (which is optional in the schema). A branch can ship at a SHA the maintainer never approved. FIX: make head_sha required; shipper hard-compares before `gh pr create`; verdicts/approvals pinned to a SHA.
- **worktree keep-vs-remove inconsistent**: SKILL says "keep for fix rounds" but teardown says "leave worktrees with OPEN PRs" - a stacked-but-not-yet-opened branch's worktree can be removed, breaking respawn. FIX: "keep from first commit until the PR MERGES"; respawn asserts/recreates the worktree.
- **stack concurrent-write corruption**: lead append + shipper pop via whole-file Write = lost update. FIX: single-writer (only the lead mutates the stack; shipper signals "shipped #N").
- **infinite loops / no caps**: "relaunch watch and yield" uncapped; MACRO + POST-PR loops have no numeric cap; pr-watch exit codes conflated (0=settled/blocked, 1=timeout, 2=setup-error treated as retryable). FIX: cap relaunches + backoff; branch on exit code (2 -> STOP+escalate); numeric caps on every loop.
- **teardown loses uncommitted work**: no clean-worktree assertion before shutdown_request. FIX: assert `git status --porcelain` empty + head_sha match before teardown.
- **port leak / collision**: no allocator, no liveness check, no server-stop on teardown; respawn rebinds same port. FIX: port table + `lsof -ti:PORT -sTCP:LISTEN` precheck + scoped server-stop on teardown.

## HIGH - multi-lead / parallel-session support (NEW 2026-06-05, demonstrated live)
Surfaced while a real parallel session (`m55-warm-fronts`) ran alongside the Phase-2 build; the
single global marker was accidentally `down`'d, disarming the other session's merge-gating.
- **Per-session marker REFCOUNTING.** The Tier-2 marker `~/.claude/orchestrate-floor.active` is a
  SINGLE per-user file: no refcount, so EITHER session's `down`/teardown disarms merge-gating for
  BOTH, the TTL is shared, and merge-gating is all-or-nothing machine-wide. FIX: replace the single
  file with a directory `~/.claude/orchestrate-floor.d/<team>` (one file per active session); the
  guard treats Tier-2 as active if ANY file in the dir is fresh (<=TTL); `orchestrate-setup.py down`
  removes ONLY its own `<team>` file. orchestrate-guard.sh `marker_active()` changes from "stat one
  file" to "any fresh file in the dir." Keep the env override (`ORCHESTRATE_FLOOR_MARKER` -> a dir).
- **Team-namespace ALL /tmp artifacts.** `orchestrate-setup.py` namespaces the stack
  (`/tmp/<team>-stack.json`) but NOT the triage dir (`/tmp/pr-triage`) or the brief
  (`/tmp/pr-shipper-brief.md`) - two parallel teams clobber each other. FIX: put all artifacts under
  `/tmp/<team>/` (stack, pr-triage, pr-shipper-brief, adv-review).
- **Marker-aware `/merge-pr` (prep + hand-off).** During an active session the floor blocks the
  agent-issued `gh pr merge` that `/merge-pr` runs, so the human currently loses all of `/merge-pr`'s
  automation (CR-status check, squash, post-merge cleanup, local-main FF, branch delete, issue
  verify) and must do a bare `! gh pr merge`. FIX: make `/merge-pr` detect the marker and, when
  active, do all checks + squash-prep + post-merge cleanup but PRINT the exact `! gh pr merge <pr>
  --squash` for the human to run (the one irreversible step stays human-executed, unforgeable by a
  bot), then resume cleanup after the human confirms it landed. Outside a session, unchanged.

## HIGH/MED - completeness / missing failure modes
- **Merge-seam / hot-spot overlap**: dispatch-map has no file-overlap check; FIFO stack all `base: main`; no rebase-after-merge. Overlapping clusters must go SEQUENTIAL (repo memory). FIX: overlap check in dispatch-map; rebase remaining stack branches + re-prep after each merge.
- **CR never posts** (norabbit / Dependabot / bot down): infinite re-watch. FIX: bounded retries -> escalate; mention the norabbit path.
- **CI never green** (stuck/flaky/infra): MERGE-READY never fires, infinite watch. FIX: distinguish code-fail (->implementer) from infra (->lead re-run); bound it.
- **maintainer unavailable overnight**: define the IDLE state - triage accumulates MERGE-READY in BRIEFING.md and goes quiet; shipper drains only already-approved entries (safe); nothing auto-merges. State it.
- **break-glass skipped**: "else Opus" default overrides the repo's Sonnet default and never gates `[effort: max|ultracode]` issue hints (UNTRUSTED -> need maintainer go). FIX: cite the hint-map + break-glass rule.
- **SHIPPABLE gate content undefined**: state the min evidence (forced-dark UAT screenshots, punch-list + live URL, prototype-vs-generated).
- **dispatch-map underspecified**: most-used artifact, no template. ADD templates/dispatch-map.md {cluster, issues, worktree, branch, port, model, effort, hint-source, overlapping-files} + the hint-mapping rule.
- **stack durability**: the live stack + brief live only in /tmp; checkpoint has no stack mirror. ADD a "SHIPPER STACK (durable mirror)" section, or move the stack out of /tmp.
- **review output dir undefined** (charter says "session's review dir"): define /tmp/adv-review/.
- **labels**: schema is a comma string but brief does per-`--label`; state the split + the required-label CI gate + docs-label policy.
- **gh pr comment for the rate-limit probe**: shipper IS authorized to post `@coderabbitai rate limit` - state it as the ONE allowed comment (vs the "never decides" framing).
- **PR-blind fix-lists** should carry RATIONALE per item (intent, not literal edit) to avoid structurally-induced thrash.
- **SSH_AUTH_SOCK**: Bash tool gets an empty sock; bots must export the 1Password agent sock before safe-push/gh.
- **memories not bundled**: SKILL leans on private memory files for load-bearing patterns; inline their essence (esp. team-prompt-clobbering, task-id-vs-issue).
- **non-Stillwater assumption**: templates hardcode safe-push/prep-pr/make-worktree/govulncheck/OpenAPI; state the prerequisite or genericize.
- **when-NOT-to-use** thin: add hot-spot-overlap, CR-budget-exhausted, fewer-than-~3-PRs.

## Things that are actually fine (not padding)
- Write/Edit secret-file PreToolUse hooks are real deterministic guards (apply to teammates).
- pr-triage charter is the strongest (enumerates `gh api -X` + specific destructive `gh pr` verbs) - use it as the model to harden the others.
- Write-tool-scoped-to-/tmp (no jq+redirect) is a correct guard.
- SendMessage/idle/shutdown/TeamDelete mechanics match the tool docs.
- "Teammates share the global allow-list" is correctly + honestly stated.

## VERDICT
The architecture is sound; the ENFORCEMENT is not. Until the CRITICAL deny-hooks land and the
background-watch + ref-ownership + stack-writer issues are fixed, treat the skill as a GUIDED
playbook for a LEAD-DRIVEN session (lead owns git + watch + stack; bots advise), NOT an
unattended auto-pipeline. The dog-food validated the adversarial-review stage: a single
parallel round surfaced ~30 real findings the author missed.

**UPDATE (phase 1, 2026-06-05):** the CRITICAL deny-hook items above are CLOSED - `orchestrate-guard.sh`
is the installed PreToolUse Bash floor (merge marker-gated-denied; push-main/force/no-verify always-denied;
proven by `test-orchestrate-guard.py`). The remaining gate to "unattended" is the HIGH lifecycle/concurrency
tranche (background-watch verification, ref-ownership single-advancer, single-writer stack, loop caps,
port allocator, worktree keep-until-merge, head_sha SHA-compare enforcement) - the phase-3 work. So the
LEAD-DRIVEN-playbook verdict still holds for those, but the catastrophic-action floor (merge / push-main /
force) is now deterministic, not prose.
