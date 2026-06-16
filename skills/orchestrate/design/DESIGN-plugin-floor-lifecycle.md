# DESIGN: plugin floor lifecycle (Option A - the floor is NOT plugin-gated)

Status: DECIDED (maintainer, 2026-06-10). Implemented in #30 PR 2/2.
Related: `DESIGN-deterministic-floor.md` (the guard itself), #30 (plugin conversion), #58 (the
consent-based `configure` that wires it).

## The question

When cc-orchestrator ships as a Claude Code plugin (#30), where does the deterministic security
floor (`orchestrate-guard.sh`, the PreToolUse `Bash` deny authority) live? A plugin can declare a
hook in `hooks/hooks.json` referencing `${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-guard.sh`. Should
the floor become that plugin hook, or stay a hand-/configure-wired `settings.json` hook?

## Decision: Option A - settings.json-resident at a stable path

The floor stays a `settings.json` `PreToolUse` hook whose command is the STABLE path
`bash "$HOME/.claude/scripts/orchestrate-guard.sh"`. The plugin does NOT ship a floor hook in
`hooks/hooks.json` (it ships no plugin hook at all). The plugin bundles the guard SOURCE under
`scripts/`; `orchestrate-setup.py configure --apply` copies/refreshes it to the stable path and
wires the settings hook, with consent (shown diff + y/N, backup, never clobbers an unparseable file).

## Why not a plugin hook (the options considered)

1. **Plugin-gated floor** (CR's auto-plan default): the floor is a plugin hook; disabling the
   plugin removes it. REJECTED. Two independent failure modes:
   - **Versioned-cache path churn.** An installed plugin lives in `~/.claude/plugins/cache/{id}/{version}/`,
     a directory that CHANGES on every update. `${CLAUDE_PLUGIN_ROOT}` resolves correctly inside the
     plugin's own hook process, but the floor is a global always-on guard whose stability must not
     depend on the plugin's update/enable state. A security floor that silently vanishes or
     repath-breaks on a plugin update or a `/plugin disable` is the exact "guardrail you can't rely
     on" we are guarding against.
   - **Disable != intent to drop the floor.** A user may disable the orchestrate plugin to quiet its
     skill/commands while still wanting the push-to-main / `--force` / `--no-verify` Tier-1 denials
     active. Coupling the floor to plugin-enabled state removes it precisely when it is least expected.
2. **Settings.json-resident (CHOSEN).** Stable path, survives plugin enable/disable/update. The
   `gh pr merge` allow-list omission (the human-merge gate) is likewise an independent settings-level
   control, so both halves of the safety model live at the same durable layer.
3. **Hybrid** (plugin installs, then a one-time wire into settings): this is effectively Option A
   with the wiring automated. `configure` IS that automation, done with consent rather than silently
   at install (a plugin cannot reliably write `settings.json` on install, and silent permission
   escalation is exactly what doctor-stays-read-only avoids).

## Consequences

- The bundled guard is the SOURCE of truth; the deployed `~/.claude/scripts/orchestrate-guard.sh` is
  a copy. After a plugin update that changes the guard, the deployed copy is STALE until the user
  re-runs `configure --apply`. `doctor` WARNs (never FAILs) when the deployed guard differs from the
  bundled source, so drift is visible.
- `configure` is the single writer of both the hook entry and the stable-path guard file; `doctor`
  stays read-only. "Permissions are the user's to grant" holds - the user runs `configure` and
  approves every addition.
- The guard's `--self-test` re-invokes via `"$0"`, so it is path-agnostic and unaffected by where the
  guard is deployed. Its determinism guarantee continues to live in `test-orchestrate-guard.py`, not
  in any deployment detail.
- Cutover from the legacy symlink install: remove `~/.claude/skills/orchestrate`, the
  `~/.claude/scripts/orchestrate-*` script symlinks, and the bundled-command symlinks (the plugin now
  provides them), but KEEP the settings.json floor hook + the stable-path guard. There is no
  double-wiring of the floor to undo, because under Option A the plugin never wires one.
