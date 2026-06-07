# Required permissions for the auto-mode bots

The pipeline bots run in auto mode and SHARE the global allow-list in
`~/.claude/settings.json` (Agent-Teams teammates cannot have a narrower set than
the lead - so read-only is enforced by the CHARTER prompt, not by permissions).

Before launching, diff this list against `permissions.allow` in settings.json and
PRINT any missing entries for the user to approve. NEVER edit settings.json
silently. Changes may require a session restart to take effect.

## Needed allow-list entries (bare-invocation + bash forms as the repo uses)
- `Bash(scripts/safe-push.sh *)` and `Bash(scripts/safe-push.sh)`  (pr-shipper push; bare form, since the repo's `bash scripts/*.sh` rules do not cover it)
- `Bash(gh pr *)`  (pr create / view / diff / checks; also covers `gh pr comment` if ever needed - but triage charter forbids it)
- `Bash(gh api *)`  (read endpoints for triage; NOTE: this also permits `-X POST/PATCH` - keep it scoped by charter, or omit and let triage use only `gh pr view`)
- `Bash(gh issue *)`  (lead files tracking issues)
- `Bash(git *)`  (push is hook-gated against main; safe-push wraps it)
- `Write(//tmp/**)`, `Edit(//tmp/**)`, `Read(//tmp/**)` (+ `//private/tmp/**` on macOS)  (stack file, briefs, triage output)
- the `go` subcommands the gate runs. NOTE: settings typically has only SCOPED entries (`Bash(go build *)`, `go test *`, `go vet *`, `go mod *`, `go tool *`, `go generate *`), NOT `Bash(go *)`. A bare `go run`/`go env`/`go list`/`go clean` in the gate will PROMPT, and an auto bot CANNOT answer -> STALL. Run the gate once in the LEAD session first, capture every `go ...` it issues, and ensure each is covered (or add `Bash(go *)` deliberately). NOTE: `acceptEdits` mode (implementer) does NOT silence Bash prompts.
- `Bash(make *)`, `Bash(golangci-lint run *)`, `Bash(govulncheck *)`  (adversarial-prep gate, implementer tests)
- `Bash(~/.claude/scripts/*.sh *)`  (pr-watch.sh, etc.)

## Guardrails - the deterministic floor (installed) + the remaining charter-level wall
The merge / push-main / force invariants now HAVE a deterministic floor: `orchestrate-guard.sh` is installed as the PreToolUse `Bash` hook (deny outranks the shared allow-list). See `DESIGN-deterministic-floor.md`. What the floor covers vs what stays charter-level:
- DONE (merge gating, REVISED 2026-06-06): merge is gated in TWO places. (1) `gh pr merge` is NOT hook-gated - it is gated by the ALLOW-LIST: settings.json lists the non-merge `gh pr` subcommands (`view/diff/checks/create/list/status/edit/ready/comment`) but OMITS `merge`, so Claude Code PROMPTS the human for `gh pr merge` (a human approves, an auto-mode bot stalls). Do NOT add `Bash(gh pr *)` or `Bash(gh pr merge *)` back - that would auto-approve merge and remove the prompt. (2) MUTATING merge-by-API (`gh api ... pulls/{n}/merge` with `-X PUT/POST` or a field flag) IS hook-gated (marker-gated hard deny), because `gh api *` stays broadly allow-listed so the allow-list cannot gate it; a bare GET merge-status check is allowed. WHY split: a PreToolUse hook on this CC honors a hard deny but IGNORES `permissionDecision:ask` (live-tested), so the hook cannot produce a prompt - only the allow-list can. A solo session with no marker still prompts for `gh pr merge` (allow-list) and allows merge-by-API.
- DONE (floor, ALWAYS-ON): `git push`/`safe-push.sh` to `main`/`master`, bare `--force`/`-f` (non-lease), and `--no-verify` are DENIED unconditionally - this closes the safe-push `$@`-forwarding gap (the old hook matched only literal `git push main`).
- STILL CHARTER-LEVEL (deliberately NOT on the floor): generic `gh api -X (POST|PATCH|PUT|DELETE)` is not denied - the LEAD needs it mid-session (CodeQL dismiss, `resolveReviewThread`), the floor cannot tell lead from bot, and those actions are recoverable. Keep `gh api -X` + `gh pr review --approve` prohibitions in BOTH read-only charters (pr-triage's is the model - copy it). Same for `gh pr review/close/edit/ready`, `make remove-worktree/migrate`, and `pkill -f`/`kill` patterns broader than the bot's own PID (they can reap other agents' servers) - charter-forbid + monitor.
- Add explicit "never push main / never --force / never --no-verify / only the head stack entry" lines to the pr-shipper charter (belt-and-suspenders over the floor).
- The Write/Edit secret-file PreToolUse hooks ARE real deterministic guards (keep them).
- Do NOT add `Bash(jq *)` just for stack edits - jq plus a shell redirect can clobber any file; use the Write tool (scoped to /tmp) for stack mutations instead.
- Prefer scoping `gh api` to GET-only by charter; the allow-list cannot express the `-X` distinction.

## INVARIANT: no cascade rule may shadow the merge gate (doctor enforces it)
Claude Code UNIONS `permissions.allow` across the WHOLE settings cascade (`~/.claude/settings.json` + `~/.claude/settings.local.json` + project `.claude/settings.json` + project `.claude/settings.local.json`). The Tier-2 merge gate works ONLY because the squash-merge command is OMITTED from the allow-list, so CC PROMPTS the human. A single blanket rule ANYWHERE in the cascade - `Bash(gh pr:*)`, `Bash(gh pr *)`, `Bash(gh:*)`, `Bash(gh *)`, `Bash(*)`, or the merge command itself - silently re-grants merge and DEFEATS the gate with no error. `orchestrate-setup.py doctor` scans every cascade file, simulates each `Bash(...)` allow-rule against the merge command, and HARD-FAILS (non-zero, naming the file + offending rule) when any rule would grant it. A found shadow is a hard failure, not a warning - the gate is silently broken until the rule is removed/narrowed.
- BEHAVIORAL RULE (human): approve the in-session merge prompt with "allow once", NEVER "always allow". "Always allow" on a `gh pr ...` command re-adds a blanket `Bash(gh pr ...)` rule to settings.local.json, which is precisely how shadow rules accumulate and silently re-defeat the gate. Narrow each `gh pr` allow-rule to the non-merge subcommands (view/diff/checks/create/list/status/edit/ready/comment); never a blanket prefix that covers `merge`.
