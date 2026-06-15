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

- No emoji in code, commits, comments, or docs. No em-dashes in any user-facing output (use
  regular dashes, commas, parentheses). User-facing times in US Pacific, labeled.
- Python: stdlib only, hand-rolled subprocess-driven test harnesses (no pytest). Lint gate is
  `ruff check --select F,E741` (E702 semicolons are the intentional harness style; do NOT "fix"
  them). Shell: `shellcheck` clean.
- Config: prefer TOML over YAML where there is a choice.

## Architecture

Runtime (symlinked into `~/.claude/scripts/`; canonical source is this repo):

- `orchestrate-guard.sh` - the single PreToolUse Bash deny authority (deterministic floor).
  Tier-1 = general bash-safety, MARKER-INDEPENDENT (every session): push-to-main/master, bare
  `--force`/`-f` (not `--force-with-lease`), `git ... --no-verify` (any accepting subcommand),
  `gh ... --admin` (admin-bypass on `pr merge`). Tier-2 = orchestrate-MARKER-GATED merge-by-API
  (`gh api ... pulls/N/merge`). Fails OPEN on any internal error. Threat model = honest bot on the
  obvious path, NOT adversarial evasion (it is a guardrail, not a sandbox).
- `orchestrate-resources.py` - cross-session port + data-dir lease allocator (flock-atomic JSON
  state). `stillwater` profile emits SW_* env (lease JSON on STDOUT, eval-able exports on STDERR);
  the encryption key is a 0600 file beside the DB, NEVER in env/.env.
- `orchestrate-setup.py` - doctor / up / down / configure, marker lifecycle, and a settings-cascade
  scan that FAILS LOUDLY if any allow-rule shadows the merge gate. `configure [--apply]` is the
  consent-based path that wires the floor hook + missing allow-list entries into settings.json
  AND narrows any blanket `gh pr` allow-rule (`Bash(gh pr *)`/`Bash(gh pr:*)`) that shadows the
  merge gate down to the enumerated non-merge subcommands (broader `gh *`/`*` shadows are surfaced
  for human resolution, never auto-rewritten); it shows the diff, writes only with --apply + a y/N,
  backs up, never clobbers an unparseable file, and reuses doctor's single shadow matcher.
  doctor stays read-only so "permissions are the user's to grant" holds.
- `uat-autobuild.sh` - repo-agnostic UAT auto-rebuild watcher: polls a branch HEAD, runs a
  parameterized `--build-cmd` on a new commit, and swaps only that port's LISTEN-pid (lease-safe)
  to the fresh binary. Keeps the leased UAT binary current (the SKILL.md "UAT EVERGREEN" mandate).
- `test-orchestrate-{guard,resources,setup}.py` - the proof harnesses.

Skill definition (symlinked into `~/.claude/skills/orchestrate/`): `SKILL.md` (lead playbook) +
`templates/` (per-role charters + schemas) + `engage-ralph-loop.md` (the adversarial-critic-loop
brief; named to disambiguate from the `/ralph-loop` skill) + `DESIGN-*`/`PLAN-*`/`ROADMAP-*`.

## Gates (run locally; CI enforces them)

```sh
shellcheck orchestrate-guard.sh gh-api-get.sh gh-codeql-dismiss.sh gh-resolve-thread.sh gh-comment.sh gh-codeql-autofix.sh gh-delete-branch.sh
ruff check --select F,E741 orchestrate-*.py planner_classify.py test-orchestrate-*.py test-planner-classify.py test-gh-wrappers.py
./orchestrate-guard.sh --self-test            # MUST use ./ - the self-test re-invokes "$0";
                                              # `bash orchestrate-guard.sh` makes $0 a bare name -> 127
python3 test-orchestrate-guard.py
python3 test-orchestrate-resources.py
python3 test-orchestrate-setup.py
python3 test-planner-classify.py
python3 test-gh-wrappers.py
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
  human `!` shell escape. See DESIGN-deterministic-floor.md (F30).
- Security-floor changes get full rigor: TDD harness cases + an independent adversarial critic pass
  (the `engage-ralph-loop.md` brief), converging at K=2 dry rounds with all gates green.

## Operating model (lead-driven; distilled from prior orchestration sessions)

- NO push / PR / merge without the maintainer's EXPLICIT "go". "Looks good"/"LGTM" is not merge
  authorization. The outward publish step is always gated; commit locally freely.
- The LEAD owns all privileged/outward steps (push, `gh pr create`, CR replies, merge) and is the
  SOLE human-facing channel. Teammates implement + test + commit in their OWN worktree and message
  the lead rather than prompting the human (AskUserQuestion/permission prompts from teammates clobber
  the human's input box).
- The merge (the one irreversible step) stays human-executed: in a marker-active session the floor
  denies the merge-by-API path and withholds `gh pr merge` from the allow-list, so the human runs it.
- Default to PARALLEL subagents for independent work, but only when DISJOINT (different files / git
  index): split by the resource being mutated, not just by logical task. FOREGROUND for anything that
  writes/commits; BACKGROUND only for provably-zero-prompt read-only work (the standing background-agent
  ban). Match rigor to blast radius (full rigor on the floor; light cadence on prose).
- The lead delegates context-heavy work (UAT, RCA, log greps, hostile review) to short-lived subagents
  that return CONCLUSIONS, keeping its own window for decisions + the checkpoint; respawn fresh at task
  boundaries.

## Deployment

Canonical source is this repo. Runtime files deploy by SYMLINK into `~/.claude/scripts/` and the skill
into `~/.claude/skills/orchestrate/` (never a copy, to avoid drift). The guard is wired as the global
PreToolUse Bash hook in `~/.claude/settings.json`. The repo's `main` is the canonical history; the old
claude-kit gist no longer carries these files.

## CI / security settings

GitHub Actions pinned to commit SHAs (with a `# vX.Y.Z` comment), `permissions: contents: read`,
`persist-credentials: false` on checkout, `concurrency` with cancel-in-progress. Branch protection on
`main` requiring the CI check (mirrors sydlexius/stillwater) is applied at the finish-line.
