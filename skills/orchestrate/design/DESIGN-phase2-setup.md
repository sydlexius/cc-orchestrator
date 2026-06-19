# Design: `orchestrate-setup.py` (Phase 2 - bootstrap + teardown)

> SUPERSEDED (#139): `TeamCreate`/`TeamDelete` referenced below were REMOVED by Anthropic. The team is now IMPLICIT (spawn named teammates directly via the `Agent` tool) and teardown is `shutdown_request` -> wait for each "terminated" notice (no `TeamDelete` step). The live `SKILL.md` teardown is authoritative; this historical doc is left as-is.

Date: 2026-06-05
Status: SHIPPED (implemented; was APPROVED (brainstorm), pre-implementation)
Companion: `DESIGN-deterministic-floor.md` (Phase 1, the guard this script arms),
`REVIEW-FINDINGS.md` (the Phase-3 lifecycle tranche this script deliberately does NOT do),
`SKILL.md` (the setup sequence this script automates).

## Problem

Standing up an orchestrate session is currently MANUAL (SKILL.md "Setup sequence"): the lead
hand-verifies prerequisites, hand-creates the /tmp artifacts, and hand-`touch`es the Tier-2
marker - error-prone, and easy to forget the marker (which leaves merge-gating off) or to start
without Agent Teams actually working. Phase 2 replaces the deterministic parts of that sequence
with one script that (a) refuses to start a session whose prerequisites are not met, (b)
scaffolds the shared artifacts, (c) arms the marker, and (d) PROVES the floor is live before
handing control back to the lead. The non-deterministic parts (TeamCreate, the dispatch map,
spawning teammates) stay the lead's interactive job - a Python script cannot call the Agent or
TeamCreate tools.

## Decisions (brainstorm, maintainer-approved 2026-06-05)

- **Settings.json: verify-and-print, never write (A).** The guard is a once-per-machine install.
  The doctor DETECTS whether the `PreToolUse.Bash` hook points at `orchestrate-guard.sh`; if
  absent it PRINTS the exact JSON block to add and FAILS. It never mutates settings.json (honors
  the standing "never edit settings.json silently" rule). No auto-install, no flag.
- **Scaffold the /tmp artifacts (A).** `up` creates the empty stack file, the triage dir, and the
  filled briefs. Mechanical, error-prone by hand, exactly what a setup script is for.
- **No marker heartbeat (A).** `up` `touch`es the marker once. A session past the 24h TTL re-runs
  `up` (or re-`touch`es). The teardown `rm` is the real off-switch; fail-open-after-TTL is the
  deliberate safe default (merge stays human regardless). A background daemon would be a new
  orphan/leak failure surface for a window that should not occur in a lead-driven session.

## Components (each: purpose / interface / behavior)

One Python CLI, `orchestrate-setup.py`, three subcommands. Python (not bash): runs occasionally,
latency irrelevant, and it does JSON/settings inspection + file scaffolding that bash does poorly.

### `doctor` (read-only prerequisite check; reused by `up`)
- **Purpose:** answer "is this environment fit to stand up an orchestrate session?" without
  changing anything.
- **Interface:** `orchestrate-setup.py doctor [--repo <path>]`. Exit 0 if no hard FAIL, 1 if any
  hard FAIL. Prints each check as `PASS` / `WARN` / `FAIL` with a one-line reason.
- **Checks:**
  1. Agent Teams on: `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` (env or settings.json `env`) AND
     `teammateMode: "tmux"` in settings.json. FAIL if off.
  2. tmux: installed AND the lead is inside it (`$TMUX` non-empty). FAIL if not in tmux (teammates
     will not spawn).
  3. Guard wired: settings.json `hooks.PreToolUse` has a `Bash` matcher whose command references
     `orchestrate-guard.sh`. If missing, PRINT the exact JSON block to add and FAIL (verify-and-
     print; never writes).
  4. Guard healthy: `orchestrate-guard.sh --self-test` exits 0 (not failing open).
  5. Repo main (if `--repo` given): record HEAD; WARN (not FAIL) if `git -C <repo> status
     --porcelain` is non-empty (stray untracked files should not block; a dirty tree is a heads-up).
  6. Allow-list diff: compare `permissions.allow` against the entries in
     `templates/required-permissions.md`; PRINT any missing ones (WARN). Auto bots stall on an
     un-allow-listed Bash call, so this is a real pre-flight.

### `up --team <name> --repo <path> [--spacing <sec>]`
- **Purpose:** arm a session: gate on prerequisites, scaffold artifacts, arm the marker, prove the
  floor.
- **Behavior (ordered; abort on any hard FAIL before the marker is armed):**
  1. Run `doctor --repo <path>`; abort if it FAILs (do not arm a session on a broken environment).
  2. Scaffold artifacts: write `/tmp/<team>-stack.json` = `[]` (valid against
     `templates/stack.schema.json`), `mkdir -p /tmp/pr-triage`, render `templates/pr-shipper-brief.md`
     into `/tmp/pr-shipper-brief.md` substituting team / repo / spacing / stack-path.
  3. Arm the marker: write `~/.claude/orchestrate-floor.active` (path overridable via
     `ORCHESTRATE_FLOOR_MARKER`) with a small human-readable header: team, ISO start time, repo,
     recorded HEAD.
  4. Armed self-test (Finding F25): feed the guard, via subprocess on stdin (payload built inside
     Python so the live hook never sees a trigger on the command line), (a) a Tier-1 payload
     (`git push origin main`) and (b) - now that the marker exists - a Tier-2 payload
     (`gh pr merge 5`). Assert BOTH exit 2. If either does not block, the floor is failing open:
     `rm` the marker (never leave a half-armed session) and abort with a loud error.
  5. On success, print a short "session armed" summary (marker path, stack path, triage dir, brief
     path) and the reminder that merges are now human-only via `! ` until `down`.

### `down [--team <name>]`
- **Purpose:** disarm the session.
- **Behavior:** `rm` the marker (idempotent - fine if already gone). Print the team-teardown
  checklist for the lead to execute via its tools: `shutdown_request` each teammate -> wait for the
  "terminated" notice -> `TeamDelete`; leave worktrees that still have open PRs. The script does NOT
  assert clean worktrees or stop servers (Phase-3 items).

### marker file
- Same contract the guard already reads: presence + fresh mtime = Tier-2 active. The header content
  is for human debuggability; the guard checks only existence + mtime.

## Error behavior
- `doctor` FAILs are loud and abort `up`. WARNs (dirty tree, missing allow-list entries) are surfaced
  but do not block - the lead decides.
- The `up` armed self-test is the one place that can leave dangerous state (an armed-but-broken
  floor). It is fail-CLOSED for setup purposes: any non-blocking result -> remove the marker + abort.
  This is the inverse of the guard's own fail-OPEN runtime stance, and correct: at setup time we want
  to REFUSE to proceed on a broken floor; at runtime the guard must not brick all shell work.

## Testing strategy
1. `test-orchestrate-setup.py` (Python harness, gist-only like the guard harness): drive each
   subcommand against a TEMP marker path + a TEMP settings.json fixture + a TEMP repo, asserting:
   each doctor check's PASS/WARN/FAIL path; artifact creation (stack file is valid JSON `[]`, triage
   dir exists, brief rendered with substitutions); marker armed with the expected header; the armed
   self-test gating BOTH Tier-1 and Tier-2; the fail-open -> abort -> marker-removed path (inject a
   stubbed guard that exits 0 and assert `up` aborts AND the marker is gone); `down` removes the
   marker and is idempotent. Never touch the real `~/.claude/orchestrate-floor.active` or the real
   settings.json.
2. Adversarial / ralph critic convergence pass (per `engage-ralph-loop.md`): TARGET =
   orchestrate-setup.py + its harness; SCOPE = honest-operator setup mistakes (wrong/missing
   prereqs, half-armed sessions, stale artifacts), NOT adversarial evasion; GATES = the harness +
   shellcheck-equivalent (pyflakes/ruff if available) + a real `doctor` run in this environment;
   ISOLATION = drive the guard through subprocess/stdin, never trigger substrings on the command line.

## Artifact locations
Author in `~/Developer/claude-kit` (the gist = canonical). `orchestrate-setup.py` is symlinked to
`~/.claude/scripts/orchestrate-setup.py` for convenient invocation (matching pr-watch.sh /
safe-push.sh / orchestrate-guard.sh); it is NOT referenced by settings.json. The harness
(`test-orchestrate-setup.py`) stays gist-only (dev artifact, resolves the script via its own dir).
This design doc lives at `~/.claude/skills/orchestrate/DESIGN-phase2-setup.md` (not a git repo, so
not committed there; the script is versioned in the gist).

## Out of scope this pass (Phase 3 / named follow-ups)
Durable stack mirror (stack lives in /tmp); marker heartbeat; port allocator; clean-worktree
teardown assertion; server start/stop; ref-ownership single-advancer; single-writer stack;
head_sha SHA-compare enforcement. These are the REVIEW-FINDINGS HIGH lifecycle tranche and land in
Phase 3. Team spawn / dispatch-map stay the lead's interactive job (not scriptable).
