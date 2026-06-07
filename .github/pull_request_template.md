## Summary

- What changed and why (focus on the why; 1-3 bullets).
- Note any behavior change to the guard, resource allocator, setup, or skill/charters.
- Call out anything reviewers should look at first.

## Linked issue

Closes #

(Use `Part of #N` for a slice of a larger issue that is not yet fully resolved.)

## Gates (run locally; CI enforces them on ubuntu + macOS)

- [ ] `shellcheck orchestrate-guard.sh`
- [ ] `ruff check --select F,E741 orchestrate-*.py test-orchestrate-*.py`
- [ ] `./orchestrate-guard.sh --self-test` (must use `./` so the self-test's `$0` re-invocation resolves)
- [ ] `python3 test-orchestrate-guard.py`
- [ ] `python3 test-orchestrate-resources.py`
- [ ] `python3 test-orchestrate-setup.py`

## Security-floor changes (delete this section if the guard is untouched)

- [ ] New behavior is TDD-driven: harness cases added/updated before the change.
- [ ] Independent adversarial-critic pass run (the `engage-ralph-loop.md` brief), converged at K=2 dry rounds.
- [ ] No trigger substrings (`git push`, `main`/`master`, `gh pr merge`, `gh ... --admin`, `... --no-verify`, `pulls/N/merge`) on any Bash command line - payloads stay inside the harness/fixtures.

## Test plan

- [ ] Describe what you exercised beyond the harnesses (e.g. live `up`/`down`, doctor, a real lease).
- [ ] Reviewer follow-ups (anything you want a second pair of eyes on):
  - [ ]

<!-- Reminders (not checkboxes):
  - Commit/PR prose with trigger words goes through a file (`git commit -F <file>`, `--body-file`), never a Bash heredoc - the live guard greps command lines.
  - Push via `~/.claude/scripts/safe-push.sh`; the prep-pr advisory gate wants `# prep-pr-ok` once gates genuinely pass.
  - Do NOT edit the symlinked canonical skill/guard files as part of unrelated work (no-mid-run-canonical-edits).
  - Mechanical/docs-only/config-only PRs: consider suggesting the `norabbit` label to the maintainer.
-->
