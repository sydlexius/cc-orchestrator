# cc-orchestrator

A Claude Code team-agent orchestrator: a lead-orchestrated, multi-agent pipeline for shipping
several PRs in parallel, backed by a deterministic security floor and a cross-session resource
registry. Extracted from a personal tooling gist into a dedicated repo so the floor and its
harnesses are protected by CI rather than hand-run checks.

## Layout

Runtime (deployed by symlink into `~/.claude/scripts/`):

- `orchestrate-guard.sh` - single PreToolUse Bash deny authority (the deterministic floor).
  Tier-1 = general bash-safety, marker-independent (push-to-main, bare `--force`, `git --no-verify`,
  `gh --admin`). Tier-2 = orchestrate-marker-gated merge-by-API.
- `orchestrate-resources.py` - cross-session port + data-dir lease allocator, with a `stillwater`
  profile (env bundle on stdout, eval-able exports on stderr; encryption key as a 0600 file, never
  in env).
- `orchestrate-setup.py` - bootstrap / doctor / up / down, marker lifecycle, and a settings-cascade
  scan that fails loudly if any allow-rule shadows the merge gate.
- `test-orchestrate-{guard,resources,setup}.py` - subprocess-driven proof harnesses (no pytest).

Skill definition (deployed by symlink into `~/.claude/skills/orchestrate/`):

- `SKILL.md` + `templates/` (the lead playbook + the per-role charters and schemas).
- `engage-ralph-loop.md` - the reusable adversarial-critic-loop brief (point `/ralph-loop` or any
  iterate-until-done harness at it; named to disambiguate from the `/ralph-loop` skill itself).
- `DESIGN-*.md` / `PLAN-*.md` / `ROADMAP-phase3.md` - the design corpus.

## Gates (run locally; enforced in CI)

```sh
shellcheck orchestrate-guard.sh
ruff check --select F,E741 orchestrate-*.py test-orchestrate-*.py
./orchestrate-guard.sh --self-test          # invoke via ./ (the self-test re-invokes "$0")
python3 test-orchestrate-guard.py
python3 test-orchestrate-resources.py
python3 test-orchestrate-setup.py
```

## Deployment

Canonical source is this repo; runtime files are symlinked into `~/.claude/scripts/` and the skill
into `~/.claude/skills/orchestrate/` (never copied, to avoid drift). The security guard is wired as
the global PreToolUse Bash hook in `~/.claude/settings.json`.
