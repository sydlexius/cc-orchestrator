# cc-orchestrator - Claude Code Project Instructions

A Claude Code team-agent orchestrator: a lead-orchestrated multi-agent pipeline for shipping
several PRs in parallel, plus the deterministic security floor and cross-session resource registry
it runs on. This file is hand-maintained (no `/init`); keep it accurate and lean.

## >> ON SESSION START / RESUME: read SESSION-STATE.md FIRST <<

`SESSION-STATE.md` (in this repo root; gitignored, machine-local) is the running checkpoint -
the top banner has current status + the next actions. It holds only NON-DERIVABLE intent +
pointers (#222); reboot-durable derivables (in-flight PRs via `gh pr list`, worktrees via
`git worktree list`) are RECONSTRUCTED on demand, not mirrored, and judgment findings keep a
durable home (the mirror-rule carve-out). Read it before doing anything when the
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
  deny-floor guard. Exit 0 ALWAYS (never blocks); emits a `STEER:` nudge to stderr on four rules:
  (1) a marker-gated mid-run edit of a canonical file (SKILL.md/templates/guard/steer) -> log
  feedback instead (gated OFF for a `Read` so wiring the hook for Read never nags a reader);
  (2) a raw `gh api` mutation not via a `gh-*` wrapper -> use the wrapper;
  (3) a raw `gh pr comment`/`gh pr create` -> canonical path (reply-comment.sh/gh-comment.sh; /prep-pr)
  (#159; canonical-steering only - `merge` and the allow-listed lifecycle subcommands are NOT flagged);
  (4) a redundant re-`Read` of a path already read this session with unchanged mtime+size -> skip the
  Read (#226; stateful per-session state keyed on the stdin `session_id`, marker-independent, advisory
  so the post-compaction re-read exception stays valid).
  Wired
  for Edit/Write/Bash/Read PreToolUse by `configure` (deployed Option-A like the guard); never duplicates
  or weakens a guard deny; opt out with `configure --no-steer`. Fails SILENT-OPEN.
- `orchestrate-resources.py` - cross-session port + data-dir lease allocator (flock-atomic JSON
  state). `stillwater` profile emits SW_* env (lease JSON on STDOUT, eval-able exports on STDERR);
  the encryption key is a 0600 file beside the DB, NEVER in env/.env.
- `scripts/orchestrate-setup.py` - doctor / up / down / configure, marker lifecycle, and a
  settings-cascade scan that FAILS LOUDLY if any allow-rule shadows the merge gate. `configure
  [--apply]` is the consent-based path that wires the floor hook + missing allow-list entries into
  settings.json, DEPLOYS the bundled guard to the stable `~/.claude/scripts/` path (so a fresh
  plugin install has a working floor; idempotent, refreshes a stale copy, warns on a missing source),
  DEPLOYS the 13 bundled PR-lifecycle helpers the same Option-A way (#133; #234 added
  `gh-react.sh`; retiring any claude-kit
  symlink, backed up to `<dest>.bak`), and (unless `--no-steer`) DEPLOYS + wires the advisory
  steering hook `orchestrate-steer.sh` for Edit/Write/Bash/Read (#95/#226; doctor only ever WARNs about it),
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
- `scripts/orchestrate-status.sh` - READ-ONLY status oracle (#223, part of the #220 TL
  context-minimization epic): one compact line per in-flight PR (`#N state checks/review/merge
  unreplied`), COMPOSED from a single `gh pr view` plus `pr-unreplied-comments.sh --count-only`,
  so the lead reads ~one line per PR instead of raw `gh pr list` JSON. Charter invariant (asserted
  in its header): issues only `gh pr list`/`view`/`repo view` reads + the read-only helper, NEVER
  mutates, and is NEVER a reason to widen the `gh pr` allow-list. Fails soft per-PR, loud (exit 2)
  when it cannot determine the in-flight set.
- `scripts/orchestrate_schemas.py` - versioned schema registry + stdlib validator (#225, part of
  the #220 epic): one source-of-truth schema per structured artifact one agent writes and another
  reads (`gate-receipt/v1`, `finding-fix-list/v1`, `finding-reply-slice/v1`), so producers/consumers
  never drift. Python callers `import` it (`validate(name, obj) -> [errors]`); shell callers use the
  `--validate <schema> <file.json>` CLI (exit 0 valid / 1 invalid / 2 usage). Extra keys allowed
  (forward-compat). Human doc: `skills/orchestrate/templates/schemas.md`. Prerequisite for #229/#230.
- `scripts/finding_channel.py` - the finding-channel guard (#230, part of the #220 epic): the
  deterministic helper over the adv-review<->implementer loop's two-slice channel (the #225
  `finding-fix-list/v1` + `finding-reply-slice/v1` schemas). `validate` (schema + channel
  invariants), `liveness` (mtime signal fresh|slow|stalled|dead|missing), `guard-reply` /
  `guard-slice` (THE pre-reply guardrail: a `fix` reply's `fix_sha` must be PUSHED to
  `origin/<branch>` AND bound to its finding by a `Finding-Id:` commit trailer - ancestry alone is
  insufficient; a reachable-but-branch-absent remote is `not pushed` (exit 1), an UNREACHABLE remote
  is exit 2 (safe-block), never a stale false-pass). Reads git + JSON ONLY - no REMOTE mutation and no
  working-tree/index/history change (its one network op is `git fetch`, read-only to the remote: it may
  update the local object DB + remote-tracking refs, nothing else; git calls are non-interactive via
  GIT_TERMINAL_PROMPT=0 so an auth-required origin fails fast instead of hanging), no `gh`, no
  allow-list broadening. Exit 0/1/2.
- `scripts/planner_classify.py`, `scripts/uat-autobuild.sh`, `scripts/gh-*.sh` - the planner helper,
  the UAT auto-rebuild watcher, and the least-privilege gh wrappers. `gh-react.sh codoki-ack`
  (#234) is the canonical reader/actuator of the Codoki ROOT-SUMMARY ack (the 👍/👎 reaction on
  Codoki's issue-level review-summary comment - a surface with no `isResolved` that never appears
  in `reviewThreads`); `ship-gate-preflight.sh` FULL mode now BLOCKs when a Codoki summary exists
  with no NON-BOT ack (a 👎 rebut also needs an `@codoki` reply), PASSES on no-summary, and
  `pr-unreplied-comments.sh --audit` surfaces the summary's ACKED/UNACKED state (informational).
- `scripts/prose-lint.sh` - the outward-draft prose-lint adapter (#219). A THIN wrapper over
  `~/Developer/prose-tooling`'s `bin/prose_check.py` (reuses, does NOT reimplement, the Markdown-aware
  LanguageTool client + house-style config), so the prose cc-orchestrator EMITS (issue/PR bodies,
  comments) gets the same grammar/style checking as committed Markdown. Takes a draft file or stdin,
  rewrites the output path column to a `--label`, passes the client's exit contract through (0 clean/
  advisory, 1 blocking, 2 cannot-check = server-unreachable OR prose-tooling-not-installed - always a
  loud stderr, never a silent skip). Reads a draft + runs one fixed LOCAL command; no `gh`, no git/
  network mutation, no allow-list/floor change; stdin temp is 0600, cleaned on EXIT. Wired ADVISORY
  (never blocks) into the `/prep-pr` PR-body + `new-issue` issue-body draft flows; the advisory
  call-site soft-skips on exit 2, so a user without the machine-local tooling is never gated. Reached
  via the repo-local / `${CLAUDE_PLUGIN_ROOT}/scripts/` path (no stable-path deployment needed).
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
`scripts/gate-runner.py`; `/orchestrate:prep-pr`, `/orchestrate:handle-review`, `/orchestrate:review-stack`, and the
optional `scripts/pre-push-hook.sh` all delegate to that one runner). Run the
whole set with `python3 scripts/gate-runner.py`. The command listing below is
kept for human reference and is DERIVED from `.gates.toml` (keep the two in
sync); it is also the fallback gate-runner would run if `.gates.toml` were
removed.

Two optional gate-runner flags (both OFF by default, ZERO behavior change when
absent; #229): `--receipt <path>` writes a schema-validated `gate-receipt/v1`
receipt (from `scripts/orchestrate_schemas.py`) as a BYPRODUCT of the real gate
run - `{commit_sha, tree_sha, worktree, result (pass|fail chosen from the tool's
own exit code), steps[], producer}`, atomic `os.replace`, FAIL-OPEN (a receipt
failure never changes the gate exit code; a non-git dir just skips it). The floor
is NEVER taught to read the receipt. `--memoize-dir <dir>` opts into conservative
PURE-oracle memoization: a `.gates.toml` step marked `pure = true` (explicit
allowlist; default false) whose PASS is memoized keyed on `HEAD^{tree}` + a live
clean-worktree check, so an unchanged committed tree skips re-running it
(`[MEMO] <name>: cached pass`). PASS-only, fail-open (a dirty tree / non-pure step
/ any git error just re-runs); git-diff / `ship-gate-preflight` /
`pr-unreplied-comments` steps are excluded (they flip at constant HEAD). See
`skills/orchestrate/templates/gates.toml.md`.

```sh
shellcheck scripts/orchestrate-guard.sh scripts/orchestrate-steer.sh scripts/orchestrate-context-meter.sh scripts/orchestrate-feedback.sh scripts/orchestrate-status.sh scripts/uat-autobuild.sh scripts/ship-gate-preflight.sh scripts/gh-api-get.sh scripts/gh-codeql-dismiss.sh scripts/gh-resolve-thread.sh scripts/gh-comment.sh scripts/gh-codeql-autofix.sh scripts/gh-delete-branch.sh scripts/gh-react.sh scripts/stale-branch-sweep.sh scripts/codoki-quota-watch.sh scripts/pr-watch.sh scripts/issue-watch.sh scripts/pr-unreplied-comments.sh scripts/pr-read-comments.sh scripts/reply-comment.sh scripts/resolve-threads.sh scripts/cleanup-worktree.sh scripts/patch-coverage.sh scripts/pr-codeql-autofixes.sh scripts/safe-push.sh scripts/pre-push-hook.sh scripts/prose-lint.sh  # v0.11.0 (CI-pinned; install shellcheck v0.11.0 locally to match)
ruff check --select F,E741 scripts/orchestrate-*.py scripts/orchestrate_schemas.py scripts/finding_channel.py scripts/planner_classify.py scripts/gate-runner.py scripts/prefs-coverage.py test-orchestrate-*.py test-finding-channel.py test-planner-classify.py test-gh-wrappers.py test-gh-react.py test-ship-gate-preflight.py test-pr-unreplied-comments.py test-pr-read-comments.py test-safe-push.py test-pr-watch.py test-issue-watch.py test-version-lockstep.py test-stale-branch-sweep.py test-codoki-quota-watch.py test-gate-runner.py test-prefs-coverage.py test-prose-lint.py test-resolve-threads.py
./scripts/orchestrate-guard.sh --self-test    # MUST use ./ - the self-test re-invokes "$0";
                                              # `bash scripts/orchestrate-guard.sh` makes $0 a bare name -> 127
./scripts/orchestrate-steer.sh --self-test    # advisory WARN-level steering hook (#95)
./scripts/orchestrate-context-meter.sh --self-test  # advisory PostToolUse context-budget meter (#228)
python3 test-orchestrate-guard.py
python3 test-orchestrate-steer.py
python3 test-orchestrate-context-meter.py
python3 test-orchestrate-resources.py
python3 test-orchestrate-setup.py
python3 test-orchestrate-feedback.py
python3 test-orchestrate-status.py
python3 test-orchestrate-schemas.py
python3 test-finding-channel.py
python3 test-planner-classify.py
python3 test-gh-wrappers.py
python3 test-gh-react.py
python3 test-ship-gate-preflight.py
python3 test-pr-unreplied-comments.py
python3 test-pr-read-comments.py
python3 test-safe-push.py
python3 test-pr-watch.py
python3 test-issue-watch.py
python3 test-version-lockstep.py
python3 test-stale-branch-sweep.py
python3 test-codoki-quota-watch.py
python3 test-gate-runner.py
python3 test-prefs-coverage.py
python3 test-prose-lint.py
python3 test-resolve-threads.py
```

## Versioning

cc-orchestrator uses Semantic Versioning. The authoritative current version is the
`**Version X.Y.Z**` line near the top of `SKILL.md` (the single visible source of truth, so
`/reload-skills` surfaces it and symlink-vs-loaded drift is detectable). Bump it on any material
change to the skill, templates, or runtime: PATCH for a fix, MINOR for a new rule/feature/charter
addition, MAJOR for a breaking charter or deterministic-floor change. Tag releases `vX.Y.Z`
(annotated) at the merge that ships them - the `/orchestrate:push-release` skill cuts the tag + a GitHub
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
