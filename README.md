# cc-orchestrator

A Claude Code team-agent orchestrator: a lead-orchestrated, multi-agent pipeline for shipping
several PRs in parallel, backed by a deterministic security floor and a cross-session resource
registry. Extracted from a personal tooling gist into a dedicated repo so the floor and its
harnesses are protected by CI rather than hand-run checks.

## Layout

The repo is a Claude Code plugin (and its own single-plugin marketplace): `.claude-plugin/`
holds `plugin.json` + `marketplace.json`; the skill lives under `skills/orchestrate/` and the
runtime scripts under `scripts/`, both auto-discovered by the plugin system.

Runtime (`scripts/`):

- `scripts/orchestrate-guard.sh` - single PreToolUse Bash deny authority (the deterministic floor).
  Tier-1 = general bash-safety, marker-independent (push-to-main, bare `--force`, `git --no-verify`,
  `gh --admin`). Tier-2 = orchestrate-marker-gated merge-by-API.
- `scripts/orchestrate-resources.py` - cross-session port + data-dir lease allocator, with a
  `stillwater` profile (env bundle on stdout, eval-able exports on stderr; encryption key as a 0600
  file, never in env).
- `scripts/orchestrate-setup.py` - bootstrap / doctor / up / down / configure, marker lifecycle, and
  a settings-cascade scan that fails loudly if any allow-rule shadows the merge gate.
- `scripts/planner_classify.py`, `scripts/uat-autobuild.sh`, `scripts/gh-*.sh` - the planner helper,
  the UAT auto-rebuild watcher, and the least-privilege gh wrappers.
- `test-orchestrate-{guard,resources,setup}.py`, `test-planner-classify.py`, `test-gh-wrappers.py` -
  subprocess-driven proof harnesses (no pytest), kept at repo root (dev tooling, not shipped).

Skill definition (`skills/orchestrate/`):

- `SKILL.md` + `templates/` (the lead playbook + the per-role charters and schemas).
- `engage-ralph-loop.md` - the reusable adversarial-critic-loop brief (point `/ralph-loop` or any
  iterate-until-done harness at it; named to disambiguate from the `/ralph-loop` skill itself).
- `DESIGN-*.md` / `PLAN-*.md` / `ROADMAP-phase3.md` - the design corpus (repo root).

## Gates (run locally; enforced in CI)

```sh
shellcheck scripts/orchestrate-guard.sh
ruff check --select F,E741 scripts/orchestrate-*.py scripts/planner_classify.py test-*.py
./scripts/orchestrate-guard.sh --self-test   # invoke via ./ (the self-test re-invokes "$0")
python3 test-orchestrate-guard.py
python3 test-orchestrate-resources.py
python3 test-orchestrate-setup.py
python3 test-planner-classify.py
python3 test-gh-wrappers.py
```

## Deployment

Canonical source is this repo, which is structured as a Claude Code plugin (and its own
single-plugin marketplace) so it can be installed without local git/symlinks - via
`/plugin marketplace add sydlexius/cc-orchestrator` + `/plugin install orchestrate@cc-orchestrator`,
or `claude --plugin-dir <repo>` for development. The deterministic floor is intentionally NOT a
plugin hook: it stays a `settings.json` PreToolUse hook at the stable path
`~/.claude/scripts/orchestrate-guard.sh`, wired with consent by `orchestrate-setup.py configure`,
so it survives plugin enable/disable/update (see `DESIGN-plugin-floor-lifecycle.md`, Option A).

The plugin packaging lands across #30 (the `configure` guard-deploy step, the symlink-retirement
cutover, and the bundled commands follow in the same effort); until that cutover, an existing
install may still run from the legacy symlinks into `~/.claude/scripts/` + `~/.claude/skills/orchestrate/`.
