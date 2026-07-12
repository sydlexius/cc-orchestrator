<!--
  CHORE / DOCS / CONFIG PR template (lighter than the default).
  Use ONLY for mechanical PRs with no script FUNCTION change and no guard change:
  docs/prose, charter/SKILL.md wording, version bumps, CI/config, dependency pins.
  Opt in via `gh pr create --template chore.md` or `?template=chore.md&expand=1`.
  If you touched a script's behavior (.sh/.py/.mjs logic) or the deterministic floor,
  STOP and use the default template instead - it carries the gate + floor-rigor checklist.
-->

## Summary

- What changed and why (focus on the why; 1-3 bullets).
- Confirm this is mechanical: no script FUNCTION change, guard untouched.

## Linked issue

Closes #

(Use `Part of #N` for a slice of a larger issue that is not yet fully resolved.)

## Gates (run the ones relevant to what you touched)

- [ ] `shellcheck ...` clean for any `.sh` edited.
- [ ] `ruff check --select F,E741 ...` clean for any `.py` edited.
- [ ] Affected harness(es) green, or N/A (note which).
- [ ] em-dash / emoji scan clean (ASCII house style).

## Version lockstep (delete if no version bump)

- [ ] `SKILL.md` `**Version**` line and `.claude-plugin/plugin.json` "version" moved together (CI `test-version-lockstep.py` enforces this).

<!-- Reminders (not checkboxes):
  - Commit/PR prose with trigger words goes through a file (`git commit -F <file>`, `--body-file`), never a Bash heredoc - the live guard greps command lines.
  - Push via `~/.claude/scripts/safe-push.sh`.
  - Do NOT edit the symlinked canonical skill/guard files as part of unrelated work (no-mid-run-canonical-edits).
  - Do NOT suggest the `norabbit` label: CR auto-review is OFF org-wide, so it suppresses nothing (a maintainer-triggered review ignores it).
-->
