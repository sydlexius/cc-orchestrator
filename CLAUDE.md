# cc-orchestrator - Claude Code Project Instructions

A Claude Code team-agent orchestrator: a lead-orchestrated multi-agent pipeline for shipping
several PRs in parallel, plus the deterministic security floor and cross-session resource registry
it runs on. This file is hand-maintained (no `/init`); keep it accurate and lean.

## >> ON SESSION START / RESUME: read SESSION-STATE.md FIRST <<

`SESSION-STATE.md` (in this repo root; gitignored, machine-local) is the running checkpoint -
the top banner has current status + the next actions. Read it before doing anything when the
user says "pick up where we left off", "resume orchestrator work", "continue orchestrator", or
otherwise asks to continue. It supersedes any stale detail below it in that file.

## Style and conventions

(Style basics - no emoji, no em-dashes, Pacific-labeled times, TOML over YAML - are in the
user-global CLAUDE.md; not repeated here. Repo-specific deltas only:)

- Python: stdlib only, hand-rolled subprocess-driven test harnesses (no pytest). Lint gate is
  `ruff check --select F,E741` (E702 semicolons are the intentional harness style; do NOT "fix"
  them). Shell: `shellcheck` v0.11.0 clean (CI pins this exact version via the digest-pinned
  koalaman/shellcheck image, so a clean local v0.11.0 gate predicts CI - install v0.11.0 to match).

## Architecture

The repo is structured as a Claude Code plugin (and its own single-plugin marketplace):
`.claude-plugin/{plugin.json,marketplace.json}`, the skill under `skills/orchestrate/`, the runtime
under `scripts/`. The deterministic floor is intentionally NOT a plugin hook - it stays a
`settings.json` PreToolUse hook at the stable `~/.claude/scripts/orchestrate-guard.sh` path
(Option A; see `skills/orchestrate/design/DESIGN-plugin-floor-lifecycle.md`), so it survives plugin enable/disable/update.

Runtime (`scripts/`; canonical source is this repo):

- `scripts/orchestrate-guard.sh` - the single PreToolUse Bash deny authority (deterministic floor).
  Tier-1 = general bash-safety, MARKER-INDEPENDENT (every session): push-to-main/master, bare
  `--force`/`-f` (not `--force-with-lease`), `git ... --no-verify` (any accepting subcommand),
  `gh ... --admin` (admin-bypass on `pr merge`). Tier-2 = orchestrate-MARKER-GATED merge: BOTH
  the `gh pr merge` CLI (`is_pr_merge`, #105) AND merge-by-API (`gh api ... pulls/N/merge`);
  a SOLO/non-marker session is never Tier-2-gated. Fails OPEN on any internal error. Threat model
  = honest bot on the obvious path, NOT adversarial evasion (it is a guardrail, not a sandbox).
- `scripts/orchestrate-steer.sh` - the advisory WARN-level steering hook (#95), SEPARATE from the
  deny-floor guard. Exit 0 ALWAYS (never blocks); emits a `STEER:` nudge to stderr on three rules:
  (1) a marker-gated mid-run edit of a canonical file (SKILL.md/templates/guard/steer) -> log
  feedback instead; (2) a raw `gh api` mutation not via a `gh-*` wrapper -> use the wrapper;
  (3) a raw `gh pr comment`/`gh pr create` -> canonical path (reply-comment.sh/gh-comment.sh; /prep-pr)
  (#159; canonical-steering only - `merge` and the allow-listed lifecycle subcommands are NOT flagged).
  Wired
  for Edit/Write/Bash PreToolUse by `configure` (deployed Option-A like the guard); never duplicates
  or weakens a guard deny; opt out with `configure --no-steer`. Fails SILENT-OPEN.
- `orchestrate-resources.py` - cross-session port + data-dir lease allocator (flock-atomic JSON
  state). `stillwater` profile emits SW_* env (lease JSON on STDOUT, eval-able exports on STDERR);
  the encryption key is a 0600 file beside the DB, NEVER in env/.env.
- `scripts/orchestrate-setup.py` - doctor / up / down / configure, marker lifecycle, and a
  settings-cascade scan that FAILS LOUDLY if any allow-rule shadows the merge gate. `configure
  [--apply]` is the consent-based path that wires the floor hook + missing allow-list entries into
  settings.json, DEPLOYS the bundled guard to the stable `~/.claude/scripts/` path (so a fresh
  plugin install has a working floor; idempotent, refreshes a stale copy, warns on a missing source),
  DEPLOYS the 12 bundled PR-lifecycle helpers the same Option-A way (#133; retiring any claude-kit
  symlink, backed up to `<dest>.bak`), and (unless `--no-steer`) DEPLOYS + wires the advisory
  steering hook `orchestrate-steer.sh` for Edit/Write/Bash (#95; doctor only ever WARNs about it),
  DEPLOYS this script to the stable path + wires a read-only SessionStart `init` advisory hook (#162;
  the `init` subcommand reuses the single `_scan_merge_gate_shadows` matcher to surface a `gh pr *`
  merge-gate shadow at session start - READ-ONLY, SILENT on a clean cascade, exit 0 on every path
  (fail-open); doctor stays the authoritative HARD-FAIL and only WARNs that the init hook is wired),
  AND (via two SEPARATE paths) (a) narrows any blanket `gh pr` allow-rule
  (`Bash(gh pr *)`/`Bash(gh pr:*)`) that shadows the merge gate down to the enumerated
  non-merge subcommands only (broader `gh *`/`*` shadows are surfaced for human resolution,
  never auto-rewritten), and (b) adds the explicit merge-scoped entry `Bash(gh pr merge *)`
  via the missing-allow path (parsed from required-permissions.md); doctor's `_is_merge_scoped`
  helper accepts an explicit merge-scoped rule as NOT a shadow (the floor backstops it); it
  shows the diff, writes only with --apply + a y/N, backs up, never clobbers an unparseable
  file, and reuses doctor's single shadow matcher.
  doctor stays read-only (it WARNs on a stale deployed guard) so "permissions are the user's to grant" holds.
- `uat-autobuild.sh` - repo-agnostic UAT auto-rebuild watcher: polls a branch HEAD, runs a
  parameterized `--build-cmd` on a new commit, and swaps only that port's LISTEN-pid (lease-safe)
  to the fresh binary. Keeps the leased UAT binary current (the SKILL.md "UAT EVERGREEN" mandate).
- `scripts/planner_classify.py`, `scripts/uat-autobuild.sh`, `scripts/gh-*.sh` - the planner helper,
  the UAT auto-rebuild watcher, and the least-privilege gh wrappers.
- `scripts/prefs-coverage.py` - opt-in UI-preference-coverage HARD-GATE (a repo enables it by adding a
  `.prefs.toml` + a `.gates.toml` step; schema `skills/orchestrate/templates/prefs.toml.md`). For each
  `[[pref]]` it greps the directly-changed governed surfaces for the pref's `verify` regex; a governed,
  changed surface that misses the mechanism is a hard failure unless an `[[exempt]]` block covers it.
  Executes `git` ONLY - a repo-declared `list_cmd` drift-check was dropped for RCE safety (#201).
- `test-orchestrate-{guard,resources,setup}.py`, `test-planner-classify.py`, `test-gh-wrappers.py` -
  the proof harnesses (kept at repo root; dev tooling, not shipped in the skill).

Skill definition (`skills/orchestrate/`): `SKILL.md` (lead playbook) + `templates/` (per-role
charters + schemas) + `engage-ralph-loop.md` (the adversarial-critic-loop brief; named to
disambiguate from the `/ralph-loop` skill) + `DESIGN-*`/`PLAN-*`/`ROADMAP-*` (under `skills/orchestrate/design/`).

## Gates (run locally; CI enforces them)

The AUTHORITATIVE gate definition is now `.gates.toml` at the repo root (read by
`scripts/gate-runner.py`; `/prep-pr`, `/handle-review`, `/review-stack`, and the
optional `scripts/pre-push-hook.sh` all delegate to that one runner). Run the
whole set with `python3 scripts/gate-runner.py`. The command listing below is
kept for human reference and is DERIVED from `.gates.toml` (keep the two in
sync); it is also the fallback gate-runner would run if `.gates.toml` were
removed.

```sh
shellcheck scripts/orchestrate-guard.sh scripts/orchestrate-steer.sh scripts/orchestrate-feedback.sh scripts/uat-autobuild.sh scripts/ship-gate-preflight.sh scripts/gh-api-get.sh scripts/gh-codeql-dismiss.sh scripts/gh-resolve-thread.sh scripts/gh-comment.sh scripts/gh-codeql-autofix.sh scripts/gh-delete-branch.sh scripts/stale-branch-sweep.sh scripts/codoki-quota-watch.sh scripts/pr-watch.sh scripts/pr-unreplied-comments.sh scripts/pr-read-comments.sh scripts/reply-comment.sh scripts/resolve-threads.sh scripts/cleanup-worktree.sh scripts/patch-coverage.sh scripts/pr-codeql-autofixes.sh scripts/safe-push.sh scripts/pre-push-hook.sh  # v0.11.0 (CI-pinned; install shellcheck v0.11.0 locally to match)
ruff check --select F,E741 scripts/orchestrate-*.py scripts/planner_classify.py scripts/gate-runner.py scripts/prefs-coverage.py test-orchestrate-*.py test-planner-classify.py test-gh-wrappers.py test-ship-gate-preflight.py test-pr-unreplied-comments.py test-pr-read-comments.py test-safe-push.py test-pr-watch.py test-version-lockstep.py test-stale-branch-sweep.py test-codoki-quota-watch.py test-gate-runner.py test-prefs-coverage.py
./scripts/orchestrate-guard.sh --self-test    # MUST use ./ - the self-test re-invokes "$0";
                                              # `bash scripts/orchestrate-guard.sh` makes $0 a bare name -> 127
./scripts/orchestrate-steer.sh --self-test    # advisory WARN-level steering hook (#95)
python3 test-orchestrate-guard.py
python3 test-orchestrate-steer.py
python3 test-orchestrate-resources.py
python3 test-orchestrate-setup.py
python3 test-orchestrate-feedback.py
python3 test-planner-classify.py
python3 test-gh-wrappers.py
python3 test-ship-gate-preflight.py
python3 test-pr-unreplied-comments.py
python3 test-pr-read-comments.py
python3 test-safe-push.py
python3 test-pr-watch.py
python3 test-version-lockstep.py
python3 test-stale-branch-sweep.py
python3 test-codoki-quota-watch.py
python3 test-gate-runner.py
python3 test-prefs-coverage.py
```

## Versioning

cc-orchestrator uses Semantic Versioning. The authoritative current version is the
`**Version X.Y.Z**` line near the top of `SKILL.md` (the single visible source of truth, so
`/reload-skills` surfaces it and symlink-vs-loaded drift is detectable). Bump it on any material
change to the skill, templates, or runtime: PATCH for a fix, MINOR for a new rule/feature/charter
addition, MAJOR for a breaking charter or deterministic-floor change. Tag releases `vX.Y.Z`
(annotated) at the merge that ships them - the `/push-release` skill cuts the tag + a GitHub
Release whose notes are auto-generated from the merged PRs (no maintained changelog file - git
history + the per-tag Release notes are the record, matching the GitHub-auto-gen preference).
Keep the SKILL.md version line and the git tag in lockstep.

LOCKSTEP RULE: `.claude-plugin/plugin.json` "version" MUST move in lockstep with the
SKILL.md `**Version**` line on every bump. SKILL.md is the human source of truth; plugin.json
drives `/plugin marketplace` update-detection, so they must never diverge. The CI
`test-version-lockstep.py` harness enforces this: a drift fails the gate.

## Working ON the security floor (critical rules)

- The live guard greps Bash COMMAND LINES. When editing the guard, NEVER put a trigger substring
  (`git push`, a push destination `main`/`master`, `gh pr merge`, `gh ... --admin`,
  `git ... --no-verify`, `pulls/N/merge`) on a Bash command line. Keep all such payloads INSIDE the
  harness/fixtures (fed to the guard via its normal stdin/env channel). The harness is the only way
  to exercise the guard safely (ISOLATION: the artifact inspects its own invocation environment).
- A PreToolUse hook loads at SESSION START, so editing the guard does not change the live hook
  mid-session (no self-lockout); the harness + `--self-test` validate the new behavior directly.
- Accepted false-positive limitations (documented, do not chase without shell-quote parsing): a
  flag token quoted inside an accepting (sub)command's argument, e.g. `git commit -m "...--no-verify..."`
  or a `gh pr comment` body quoting the literal merge command. Rare, recoverable by rewording or the
  human `!` shell escape. See skills/orchestrate/design/DESIGN-deterministic-floor.md (F30).
- Security-floor changes get full rigor: TDD harness cases + an independent adversarial critic pass
  (the `engage-ralph-loop.md` brief), converging at K=2 dry rounds with all gates green.

## Operating model (lead-driven; distilled from prior orchestration sessions)

- AUTONOMY TIERS (maintainer directive 2026-06-14). Outward steps are gated BY TIER, not blanket-gated:
  - NON-CR, well-shaped issue (no script-FUNCTION change - prose/doc/charter/SKILL.md/CLAUDE.md edits):
    the lead carries it the ENTIRE cycle AUTONOMOUSLY (implement -> PR -> CI -> merge -> cleanup) with NO
    per-step maintainer approval; the standing grant IS the "go".
  - CR-REQUIRED PR (changes a script's FUNCTION: `.sh`/`.py`/`.mjs` logic): needs maintainer PERMISSION
    TO OPEN the PR, and its MERGE stays human.
  - GATES + hostile pre-push review are NEVER waived - autonomy waives the maintainer's APPROVAL, not the
    rigor (build + prep + hostile review green before ANY PR; a CR-required PR still requires a CR pass
    before merge, and the MAINTAINER triggers that pass themselves - the lead/agent NEVER posts
    `@coderabbitai review` (it is the maintainer's EXCLUSIVE purview); the lead only SURFACES a
    warranted pass as a `▶ NEEDS YOU` gate, like merge, and stops - never lead-initiated, not even
    on per-instance authorization).
  - SELF-IMPOSED CARVE-OUT: a change that edits the deterministic floor / merge-policy / operating-model
    ITSELF routes for MAINTAINER MERGE even when it is "doc" (this file's operating-model + SKILL.md floor
    invariants qualify).
  - MECHANICAL: autonomous merge works only in a SOLO / non-marker session; a marker-active TEAM session
    has the floor hard-deny `gh pr merge` CLI (is_pr_merge, #105) and deny merge-by-API, so there the lead
    SURFACES the merge to the human (who merges from a SEPARATE plain terminal). "Looks good"/"LGTM" is
    NEVER a merge authorization for the human-gated tiers; commit locally freely regardless of tier.
- The LEAD owns all privileged/outward steps (push, `gh pr create`, CR replies, merge) and is the
  SOLE human-facing channel. Teammates implement + test + commit in their OWN worktree and message
  the lead rather than prompting the human (AskUserQuestion/permission prompts from teammates clobber
  the human's input box).
- The merge (the one irreversible step) stays human-executed: in a marker-active session the floor
  hard-denies the `gh pr merge` CLI (is_pr_merge, #105) and the merge-by-API path, so the human
  runs it from a SEPARATE plain terminal (no marker there) or the GitHub UI. The allow-list now
  carries an EXPLICIT merge-scoped entry (`Bash(gh pr merge *)`) - safe because the floor deny
  outranks it in a marker session, and it enables prompt-free solo merge for the maintainer.
- Default to PARALLEL subagents for independent work, but only when DISJOINT (different files / git
  index): split by the resource being mutated, not just by logical task. FOREGROUND for anything that
  writes/commits; BACKGROUND only for provably-zero-prompt read-only work (the standing background-agent
  ban). Match rigor to blast radius (full rigor on the floor; light cadence on prose).
- The lead delegates context-heavy work (UAT, RCA, log greps, hostile review) to short-lived subagents
  that return CONCLUSIONS, keeping its own window for decisions + the checkpoint; respawn fresh at task
  boundaries.
- FEEDBACK-LOG DRAIN GATE (maintainer directive 2026-06-15; BINDING). This rule lives HERE in CLAUDE.md,
  not only in SKILL.md, on purpose: the SKILL.md copy kept getting SKIPPED and does not reliably survive
  context exhaustion / compaction, whereas CLAUDE.md is injected at every session start. A
  `~/.claude/orchestrate-feedback/inbox/<entry>` entry is DRAINED (moved to `drained/` cold storage via
  `orchestrate-feedback.sh drain`, #149) ONLY after this EXACT three-step ordering - never out of order,
  never collapsed into one pass, never skipped because an entry "looks obviously right":
  1. TASK A HOSTILE REVIEWER on the entry FIRST. Dispatch an ACTUAL adversarial reviewer (a subagent, or
     the `engage-ralph-loop.md` brief) - the lead's own glance does NOT count. It REPRODUCES every empirical
     claim (run it, never static-grep), pushes back on holes / over-reach / mis-scoping, and runs a
     least-privilege check that the proposed change never weakens the deterministic floor or broadens an
     allow-list. No tasked hostile review -> no issue, no drain.
  2. THEN FILE THE ISSUE (only after step 1). A verified entry becomes a normal issue (template + agent
     hints + immediate CR steering per the SKILL.md DRAIN PROCEDURE); an unreproducible claim is still filed,
     framed as a KNOWLEDGE-GAP issue (never lose the signal); an entry the hostile review KILLS is drained
     via `drain <entry> --killed --verdict "KILLED: <reason>"` (the `--killed` flag makes `--issue`
     OPTIONAL - without it `drain` requires a numeric `--issue N` and rejects the killed form), so the
     killed-entry record lives in `drained/`.
  3. THEN DRAIN THE ENTRY (only after step 2). The entry stays in `inbox/` until its issue number (or the
     KILLED drop reason) is recorded against it via `drain`. NEVER drain before the issue exists; NEVER file before the
     hostile review. This GATES the SKILL.md "TRIAGE RIGOR / DRAIN PROCEDURE"; if the two ever disagree, this
     ordering wins.

## Deployment

Canonical source is this repo, packaged as a Claude Code plugin: install via
`/plugin marketplace add sydlexius/cc-orchestrator` + `/plugin install orchestrate@cc-orchestrator`,
or `claude --plugin-dir <repo>` for development (`/reload-plugins` to pick up edits). The skill +
commands + scripts are auto-discovered from the plugin layout; no symlinks needed. The deterministic
floor stays a `settings.json` PreToolUse hook at the stable `~/.claude/scripts/orchestrate-guard.sh`
path (Option A; never plugin-gated), and `orchestrate-setup.py configure --apply` deploys that guard
+ wires the hook with consent. The repo's `main` is the canonical history; the old claude-kit gist no
longer carries these files. (CUTOVER from a legacy symlink install: install the plugin, run
`configure --apply`, then retire the old `~/.claude/skills/orchestrate` + `~/.claude/scripts/orchestrate-*`
symlinks; the settings.json floor hook + stable-path guard stay.)

Deploying a merged floor/guard change to OTHER sessions: a merged guard change does NOT
auto-propagate to already-running sessions or other machines. Three steps: (1) update the plugin
(`/plugin marketplace update` + reinstall, or `git pull` + `/reload-plugins` for a `--plugin-dir`
dev install) to get the new bundled guard + doctor; (2) `orchestrate-setup.py configure --apply` to
RE-DEPLOY the updated guard to the stable `~/.claude/scripts/` path (the PreToolUse hook runs the
DEPLOYED copy, not the repo) and wire any sanctioned allow-list entry - this stays CONSENT-GATED and
never silently edits settings.json; (3) RESTART each open Claude Code session (the PreToolUse hook
loads the guard at SESSION START, so a running window keeps the old guard until it is relaunched -
this is a session restart, NOT an OS reboot).

## CI / security settings

GitHub Actions pinned to commit SHAs (with a `# vX.Y.Z` comment), `permissions: contents: read`,
`persist-credentials: false` on checkout, `concurrency` with cancel-in-progress. Branch protection on
`main` requiring the CI check (mirrors sydlexius/stillwater) is applied at the finish-line.
