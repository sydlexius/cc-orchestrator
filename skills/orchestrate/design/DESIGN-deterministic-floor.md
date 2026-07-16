# Design: deterministic floor for `orchestrate` + composed inner loop

> SUPERSEDED (marker only) by P3-A: the single global marker file
> `~/.claude/orchestrate-floor.active` described below is now a per-session
> SESSION-KEYED file `~/.claude/orchestrate-floor.d/<session-key>` with a 72h TTL.
> **#312 update:** the session key is the sanitized `$TMUX` when set, else `ccsid_` +
> the sanitized `$CLAUDE_CODE_SESSION_ID` - so **tmux is NOT required to run a GATED
> session** (the iTerm2 / in-process backend is gated identically), and only a session
> with NEITHER identifier is unkeyed and therefore never gated. The guard matches ANY
> candidate key (`_session_keys()`), so an arm-side/check-side scheme disagreement
> cannot silently disarm the gate. This SUPERSEDES the "empty `$TMUX` is never gated"
> rule stated below and in D2 of `DESIGN-phase3a-marker-refcounting.md`. See
> `DESIGN-phase3a-marker-refcounting.md`. (#105 update: `gh pr merge` is NOW also
> marker-gated on the floor - see the "Tier-2: #105 floor-gate supersedes
> allow-list-omission" section below; it supersedes the "allow-list-gated" record in
> this header.)
>
> **READ EVERYTHING BELOW AS THE PHASE-1 DESIGN RECORD, NOT AS CURRENT GUIDANCE.**
> Naming the dimensions explicitly, because a reader who hits one of them mid-document
> has no way to tell it is stale - EVERY later statement about the marker's:
>   - **PATH** (a single global `~/.claude/orchestrate-floor.active`),
>   - **TTL** (24h),
>   - **SCOPE** (per-user / global rather than per-session), and
>   - **non-tmux behavior** ("never gated")
>
> is SUPERSEDED and must not be implemented from. The AUTHORITY for the marker contract
> is, in order: the DERIVATION REGISTRY + `marker_active()` in
> `scripts/orchestrate-guard.sh` (the code), then
> `DESIGN-phase3a-marker-refcounting.md` (the design of record). This document remains
> authoritative ONLY for the Tier-1/Tier-2 rationale and the threat model.

Date: 2026-06-05
Status: APPROVED (brainstorm), pre-implementation
Companion: `REVIEW-FINDINGS.md` (the adversarial review this addresses), `SKILL.md`

## Problem

`orchestrate` stands up an unattended multi-agent PR pipeline. Its top safety
invariants - NO bot merges, NO push to main, PR-blind implementers - are
PROSE-ONLY: Agent-Teams teammates SHARE the lead's global allow-list, so a
teammate cannot be given a narrower permission set than the lead. The charter
prompt is the only "wall," and `REVIEW-FINDINGS.md` showed three ways an auto
bot evades it:

- `gh pr merge` is allow-listed (`gh pr *`); the PostToolUse hook only PRINTS
  after the fact, it does not block.
- `gh api -X PUT repos/{o}/{r}/pulls/{n}/merge` is a merge that the `git push`
  string-grep never inspects.
- `safe-push.sh main --force` / `--no-verify` forwards `$@` to `git push`, so
  the literal `git push main` matcher misses it.

This spec adds a DETERMINISTIC floor (PreToolUse deny that outranks allow) for
the obvious command forms, plus a test harness that proves it, and composes the
skill's re-derived inner loop onto the existing `subagent-driven-development`
skill.

## Threat model (the scope boundary)

Defend against an **honest-but-misaligned bot on the obvious path**: a
well-intentioned auto teammate that, misreading its charter, issues the NATURAL
command form (`gh pr merge 1868`, `safe-push.sh main`, `git push --force`). Catch
those common spellings robustly. Do NOT attempt to contain an actively-evading
adversarial agent (aliases, `$(...)`, base64, wrapper scripts) - that is
unachievable with string hooks and is explicitly out of scope. A string-matching
hook is a guardrail against confusion, not a sandbox against malice.

## Decision: two-tier floor

The deny-hooks live in global `~/.claude/settings.json` and therefore apply to
the lead, every teammate, AND every ordinary solo session. The partition is by
the test "does the lead ever legitimately do this in a solo session?"

| Verb | Legit solo? | Tier |
|---|---|---|
| `git push` / `safe-push.sh` to `main`/`master` | never (branch protection) | 1 - always-on |
| bare `--force` / `-f` (non-lease) push | never (lead uses `--force-with-lease`) | 1 - always-on |
| `--no-verify` push (skips pre-push gate) | never | 1 - always-on |
| `gh pr merge` (incl. `--auto`; `--admin` is also Tier-1) | yes (`/merge-pr`, solo) | 2 - session-gated (#105: now floor deny, not allow-list-omission) |
| `gh api ... pulls/{n}/merge` MUTATING (`-X PUT/POST` or field flag; bare GET = status-check, allowed) | no, but it IS merge | 2 - session-gated |
| generic `gh api -X POST\|PUT\|PATCH\|DELETE` (non-merge) | yes, MID-SESSION (CodeQL dismiss, `resolveReviewThread`) | NOT in the floor - charter-level (Finding F8) |
| `--force-with-lease` | yes (rebased re-push) | allowed in both tiers |

- **Tier 1 (always-on, unconditional):** never legitimate from Claude, so zero
  cost to deny always. Preserves the push-main floor solo sessions already have
  today. Net-new solo friction is effectively zero.
- **Tier 2 (session-gated) = MERGE ONLY:** the catastrophic, irreversible action.
  Fires ONLY when the marker file is present (see Mechanism). Covers `gh pr merge`
  in any flag form AND MUTATING merge-by-API (`gh api` whose path contains
  `pulls/{n}/merge` AND has a mutating method or field flag - `gh api .../merge
  -f merge_method=squash` implies POST without `-X`; a bare GET status-check is
  allowed, F14). A solo `/merge-pr` is untouched (marker absent).
- **Why generic `gh api -X` is NOT in the floor (Finding F8):** the floor cannot
  distinguish lead from bot, and the LEAD legitimately runs `gh api graphql`
  (`resolveReviewThread`) and `gh api -X PATCH .../code-scanning/alerts` (CodeQL
  dismiss) DURING an active pipeline as part of `/handle-review`. A marker-gated
  deny on generic `-X` would block the lead's own review handling. Those actions
  are also recoverable, not irreversible. So generic `gh api -X` containment stays
  CHARTER-level (the read-only bots' charters forbid it; pr-triage's is the model)
  and is revisited in the phase-3 lifecycle tranche. The deterministic floor
  guards only what is irreversible: merge and push-main/force/no-verify.

## Mechanism: marker-gated, install-once

settings.json hooks load at session start (a freshly-added hook can need a
restart), and mutating global settings on/off per session is racy. Decouple
INSTALLATION from ACTIVATION:

- ONE PreToolUse `Bash` guard, installed in settings.json ONCE
  (`orchestrate-guard.sh`). It ABSORBS the existing `git push main` +
  `prep-pr-ok` hook, so there is a single Bash-deny authority, not two
  overlapping hooks with divergent regex.
- Tier-1 checks fire unconditionally. Tier-2 checks fire ONLY when the marker
  file `~/.claude/orchestrate-floor.active` exists AND is fresh.
- Marker freshness: the guard treats the marker as active only if its mtime is
  within `ORCHESTRATE_FLOOR_TTL_HOURS` (default 24h). The teardown (`rm` marker)
  is the REAL off-switch; the TTL is purely a self-heal BACKSTOP so a missed
  teardown does not silently break solo merges forever. A session expected to run
  past the TTL must re-`touch` the marker (the setup script can refresh it on a
  heartbeat); the floor failing OPEN after the TTL is the deliberate, safe
  default for the honest-path threat model (a forgotten marker is more likely
  than a >24h unattended honest pipeline, and merge stays human regardless).
- Activation is a runtime file `stat`, but ONLY on calls that already matched a
  Tier-2 merge pattern - the guard short-circuits (cheap Tier-1 string checks
  first; no marker `stat` on ordinary `ls`/`grep`/`git status` calls), protecting
  the ~5ms always-on budget. Setup `touch`es the marker (instant, no restart),
  teardown `rm`s it. No per-session settings mutation, no restart after the
  one-time install.

Rejected alternatives: per-session settings.json rewrite (restart + race +
partial-write risk); project-level `.claude/settings.json` (applies to the lead
too - lead and teammates both live in the repo/worktrees, so it cannot
discriminate).

## Block-message rules

Two DISTINCT gate classes with different message rules (Finding F10 - do not
conflate them):

1. **Tier-1 hard floor denies** (push-main / bare-force / no-verify). These have
   NO override token at all, so the no-echoed-bypass rule is satisfied trivially.
   Messages explain WHY and point the HUMAN out-of-band: a Tier-1 deny can be done
   by the human via the `! ` prefix (their own shell) or the GitHub UI if genuinely
   needed - that path is for the human, not the agent. Tier 1 -> "never allowed
   from Claude; human runs it via `! ` if intended."
1b. **Tier-2 merge: both `gh pr merge` CLI and merge-by-API are now MARKER-GATED FLOOR DENIES**
   (#105, supersedes the 2026-06-06 allow-list-omission design). History: (i) originally a
   hook hard-deny on all merge - but the `!` bang command IS hooked on this CC, so it
   blocked the human's OWN `! gh pr merge` too, and the hook payload has no field to tell
   a human `!` from an agent Bash call. (ii) Tried emitting `permissionDecision:"ask"` to
   PROMPT instead - a LIVE TEST proved this CC IGNORES a hook `ask` (it ran the command),
   while still honoring a hook exit-2 deny. So a hook cannot prompt here. (iii) 2026-06-06
   interim: split the gate - `gh pr merge` omitted from the allow-list (CC prompts human;
   bot stalls); merge-by-API (`gh api ... pulls/N/merge`) stays a hook hard-deny. Problem:
   the "allow once" -> "always allow" click re-granted a blanket `gh pr *` rule in
   settings.local.json, re-opening the bot-merge hole (recurring doctor shadow FAIL). (iv)
   FINAL (#105): `gh pr merge` CLI is now MARKER-GATED on the FLOOR (`is_pr_merge`, exit
   2 when marker active), mirroring `is_merge_api`. The floor deny OUTRANKS the allow-list,
   so a blanket shadow can no longer defeat it. Simultaneously, `gh pr merge *` (and its
   `:*`/bare forms) IS NOW the SANCTIONED allow-list entry - so a SOLO/non-marker session
   (the maintainer's own `/merge-pr`) runs prompt-free. A marker-active team session's floor
   blocks bot merge regardless of the allow-list. See "Tier-2: #105 floor-gate supersedes
   allow-list-omission" below. Doctor reconciliation (`_is_merge_scoped`): an explicit
   merge-scoped allow-rule (`Bash(gh pr merge *)`, `Bash(gh pr merge:*)`, `Bash(gh pr merge)`)
   is ACCEPTED (not a shadow) because the floor backstops it in a marker session. A broader
   rule (`Bash(gh pr *)`, `Bash(gh pr:*)`, etc.) still hard-fails doctor.
2. **The `prep-pr-ok` advisory gate** (ordinary feature-branch pushes). This is
   NOT a floor deny and is NOT what we are securing - pr-shipper is MEANT to push
   feature branches. It is the existing lead-ergonomics gate ("run /prep-pr
   first"), preserved VERBATIM including its `# prep-pr-ok` override echo. Keeping
   that echo is fine precisely because feature pushes are sanctioned; the
   `REVIEW-FINDINGS` no-echo lesson applies to the FLOOR denies (class 1), which
   is why those have no token to leak.

### Tier-2: ask rejected, allow-list adopted (live-test record, 2026-06-06)
The `permissionDecision:"ask"` approach was TESTED LIVE and FAILED on this machine:
with the marker armed, an agent-issued `gh pr merge 999999999` RAN with no prompt
(gh just errored on the remote) - Claude Code ignored the hook's valid `ask` JSON.
The guard itself was correct (a direct feed confirmed it emitted exit 0 + valid ask
JSON); the gap is CC-side: **this CC honors a hook exit-2 DENY but ignores a hook
`ask`/structured `permissionDecision`.** So a PreToolUse hook here can only allow or
deny - it cannot PROMPT.

This record explains the 2026-06-06 interim design (allow-list-omission); see the
next section for the #105 superseding design.

### Tier-2: #105 floor-gate supersedes allow-list-omission (2026-06-15)

The 2026-06-06 allow-list-omission gate for `gh pr merge` had a structural weakness:
a single "always allow" click regenerated a blanket `Bash(gh pr *)` rule in
settings.local.json, silently re-opening the bot-merge hole. Doctor detected it on
every `orchestrate up`, but the root fix needed to be at the floor, not in user
discipline.

Adopted design (#105 - what ships):
- **`gh pr merge` CLI -> MARKER-GATED FLOOR DENY (`is_pr_merge`).** The guard now
  matches `gh pr merge` (and forms with global flags between `pr` and `merge`, e.g. `gh
  pr -R owner/repo merge 5`, `gh pr --repo owner/repo merge 5`) and exits 2 (block) when
  THIS session's marker is active - mirroring `is_merge_api` and the tolerant `pr ...
  merge` ordering that `is_gh_admin` already uses. A floor deny OUTRANKS the allow-list,
  so a blanket shadow (`gh pr *`) can never defeat it even if "always allow" re-grants it.
- **`gh pr merge` CLI + EXPLICIT ALLOW-LIST ENTRY (the sanctioned form).** Because the
  floor is MARKER-GATED (solo/non-marker sessions are never denied), `Bash(gh pr merge *)`,
  `Bash(gh pr merge:*)`, or `Bash(gh pr merge)` can now be ADDED to the allow-list as the
  SANCTIONED entry. This means the maintainer's own `/merge-pr` in a solo session runs
  prompt-free (no more per-merge approval prompt), while a marker-active team session's
  floor blocks any bot attempt regardless of the allow-list.
- **merge-by-API -> marker-gated hook HARD DENY (unchanged).** `gh api ... pulls/N/merge`
  mutating stays an exit-2 deny in the guard.
- **Doctor reconciliation (`_is_merge_scoped`).** `orchestrate-setup.py`'s
  `_merge_rule_shadows` now calls `_is_merge_scoped` first: a merge-SCOPED allow-rule
  (one whose language is a subset of the merge family - `Bash(gh pr merge)`,
  `Bash(gh pr merge *)`, `Bash(gh pr merge:*)`, or `Bash(gh pr merge --flag)`) is ACCEPTED
  (not a shadow) because the floor backstops it in a marker session. Any broader rule
  (`Bash(gh pr *)`, `Bash(gh pr:*)`, `Bash(gh *)`, etc.) still hard-fails doctor.
- **Backstop (unchanged):** server-side branch protection (required review + status checks)
  gates the real merge regardless, so even a settings regression cannot land un-reviewed
  code on `main`.

## Components (each unit: purpose / interface / deps)

### `orchestrate-guard.sh` (bash; PreToolUse `Bash` hook)
- **Purpose:** the single Bash-tool deny authority. Extracts the command string,
  applies Tier-1 (always) and Tier-2 (marker-gated) checks, exits 2 (block) with a
  stderr reason or 0 (allow).
- **Interface:** Exit 0 = allow, exit 2 = block (the proven contract - the
  existing push hook uses exit 2). Reads env `ORCHESTRATE_FLOOR_MARKER` (default
  `$HOME/.claude/orchestrate-floor.active`) and `ORCHESTRATE_FLOOR_TTL_HOURS`
  (default 24).
- **Command-input channel (Finding F23 - getting this wrong nullifies the floor):**
  the EXISTING working hooks read the command from the `$TOOL_INPUT` env var
  (`echo "$TOOL_INPUT" | jq -r '.command'`); the hookify plugin reads the
  `tool_input.command` field from a stdin JSON payload. CC populates both on this
  version, but the guard MUST be robust: read stdin JSON first, FALL BACK to
  `$TOOL_INPUT`, and if BOTH yield an empty command, FAIL OPEN (exit 0) - never
  block on an empty read. The install self-test (see Error behavior) verifies the
  chosen channel is actually populated before the guard is trusted.
- **Error behavior (Finding F19 - fail OPEN):** on ANY internal error (missing
  `jq`, malformed payload, `stat` failure, empty command read), the guard exits 0
  (allow). Rationale: this guard runs on EVERY Bash call across ALL sessions;
  fail-closed would brick all shell work if the guard breaks. The compensating
  controls are (a) `test-orchestrate-guard.py` proving correctness before deploy,
  and (b) an install-time self-test (feed the guard one known-block payload and
  assert exit 2) so a guard that is silently failing-open is caught at install,
  not in production. The deterministic guarantee lives in the harness, not in
  fail-closed runtime behavior.
- **Deps:** bash, `jq` (already used by existing hooks), coreutils `stat`.
  Bash (not python) because it runs on EVERY Bash tool call across all sessions;
  per-call latency must stay ~5ms, not ~50ms python startup.
- **Evaluation order (Finding F18):** the override must never bypass a hard deny. The
  guard evaluates PER CLAUSE in a SINGLE first-match loop, returning exit 2 on the first
  hard deny: (1) Tier-1 (push to main/master, bare force, no-verify, `--admin`) - ALWAYS;
  (2) Tier-2 merge-by-API (`gh api ... pulls/N/merge` mutating) - IF the marker is active;
  (3) Tier-2 `gh pr merge` CLI (`is_pr_merge`) - IF the marker is active (#105); (4) ONLY
  THEN the `prep-pr-ok` advisory gate for remaining feature-branch pushes. A single loop
  is correct because every floor decision is now an exit-2 deny (the exit-0 `ask` branch
  was REMOVED - see "Tier-2: ask rejected, allow-list adopted" - so no decision can be
  pre-empted by an earlier clause). The `# prep-pr-ok` override is checked LAST and can
  ONLY satisfy the advisory gate - it can NEVER reach a hard deny (so
  `git push main # prep-pr-ok` stays blocked by step 1).
- **Behavior detail:** preserves the existing `git push` -> require-prep-or-
  `# prep-pr-ok` gate; adds safe-push-to-main coverage, bare-force, no-verify
  (Tier 1); adds MUTATING merge-by-API (`pulls/{n}/merge` with a mutating
  method/field) as Tier 2 (marker-gated); adds `gh pr merge` CLI as Tier 2
  (marker-gated, `is_pr_merge`, #105 - see "Tier-2: #105 floor-gate supersedes
  allow-list-omission"). Generic `gh api -X` is NOT matched (Finding F8).
  `gh pr merge --admin` is Tier-1 (is_gh_admin), denied even in a solo session.
  Matching rules the implementer MUST honor:
  - `--force` / `-f` is matched as a WHOLE WORD and ONLY when NOT immediately
    followed by `-with-lease` (Finding F9: `--force-with-lease` contains the
    substring `--force`; a naive match wrongly blocks the allowed form).
  - whitespace between tokens is tolerated (`gh  pr   merge`).
  - **git global options between `git` and `push` are tolerated** (Finding F11):
    `git -C <dir> push`, `git -c k=v push` must still be recognized as a push.
    The existing adjacency regex (`git[[:space:]]+push`) MISSES these; the lead
    uses `git -C <worktree>` routinely.
  - **gh global flags between `gh` and the subcommand are tolerated** (Finding
    F12): `gh -R owner/repo pr merge`, `gh --repo ... api ...` must still match.
  - **gh global flags between `pr` and `merge` are also tolerated** (the #105
    adversarial finding): `gh pr -R owner/repo merge 5` and `gh pr --repo
    owner/repo merge 5` are valid gh CLI spellings and must be caught alongside
    the simple adjacent form. The `is_pr_merge` clause 2 regex allows zero or more
    flag groups (each starting with `-`) between `pr` and `merge`, mirroring the
    tolerant `pr` ... `merge` ordering already used in `is_gh_admin`.
  - merge is matched on the `pr` ... `merge` SUBCOMMAND token sequence (with
    optional flags), NOT the word "merge" as a substring (Finding F13: else
    `gh pr create --title 'merge auth'` false-positives; the regex requires
    `merge` as a whole word immediately following only flag-groups after `pr`).
  - merge-by-API is `gh api` whose path contains `pulls/<digits>/merge` AND that
    is MUTATING - i.e. has `-X|--method PUT|POST` OR a field flag
    (`-f|--field|-F|--input|--raw-field`) which implies POST (Finding F6). A bare
    `gh api .../pulls/<n>/merge` with no method and no fields is a GET merge-STATUS
    check and MUST be allowed (Finding F14).
  - main/master is matched as a WHOLE WORD anywhere in the command, so a refspec
    destination (`git push origin HEAD:main`, `feat:main`) is caught, while a
    branch named `maintenance` / `domain` is NOT (the word boundary excludes
    substrings). Implement as a boundary regex, not a positional-arg equality test
    (which would miss `HEAD:main`).
  - the marker path default expands via `$HOME` (NOT a literal `~`, which does not
    expand inside a quoted variable default) (Finding F15).

### marker file `$HOME/.claude/orchestrate-floor.active`
- **Purpose:** the activation switch for Tier-2 (merge) checks.
- **Interface:** presence + fresh mtime = Tier-2 active. Content = a small
  human-readable header (team name, ISO start time) for debuggability; the guard
  only checks existence + mtime, not content. mtime read via macOS `stat -f %m`
  (the dev machine is darwin); branch on `uname` if cross-platform use is ever
  needed (Finding F20).
- **Scope (Finding F21 - global, documented):** the marker is per-USER, not
  per-session or per-repo. While ANY orchestrate session holds it, EVERY session -
  including unrelated solo work in other repos - has Tier-2 (both `gh pr merge` CLI and
  merge-by-API) gated until teardown. Accepted for the honest-path, single-operator
  model: during that window the human merges from a SEPARATE plain terminal (no marker
  there) or via the GitHub UI. Per-repo scoping (guard compares cwd to a repo recorded
  in the marker) is deliberately NOT done - the lead and its teammates span multiple
  worktrees of the same repo, so cwd is not a clean discriminator (the same reason
  per-teammate scoping was rejected).
- **Deps:** none. Created/removed by the setup/teardown script (phase 2) or by
  hand (`touch` / `rm`) for testing.

### `test-orchestrate-guard.py` (harness; the deterministic proof)
- **Purpose:** prove the guard blocks the bypass vectors and does not block
  legitimate commands, in BOTH marker states.
- **Interface:** invokes `orchestrate-guard.sh` with crafted hook-input payloads
  via a pointed marker path/TTL (no mutation of the real marker), asserts exit
  codes. Exit 0 = all rows pass.
- **Both input channels (Finding F24):** because F23 makes the guard read stdin
  JSON with a `$TOOL_INPUT` env fallback, the harness MUST run each case through
  BOTH channels (once feeding the payload on stdin, once via `$TOOL_INPUT` with
  empty stdin) and assert identical verdicts. Add a both-empty case (no stdin, no
  `$TOOL_INPUT`) asserting fail-OPEN (exit 0). Without this the fallback path and
  the fail-open behavior ship untested.
- **Cases (minimum):**
  - blocks always: `git push origin main`, `git -C ../wt push origin main`
    (F11), `git push origin HEAD:main` (refspec destination), `scripts/safe-push.sh main`,
    `git push --force` (bare), `safe-push.sh feat --force`, `git push --no-verify`
  - BLOCKS when marker active (merge-by-API AND `gh pr merge` CLI -> exit 2) (#105):
    `gh api -X PUT repos/o/r/pulls/1/merge`, `gh api --method PUT .../pulls/1/merge`,
    `gh api repos/o/r/pulls/1/merge -f merge_method=squash` (POST via field, no `-X`);
    `gh pr merge 1868`, `gh pr merge --auto 1868`, `gh -R o/r pr merge 1868` (F12).
  - The harness also asserts the guard emits NO `permissionDecision` on any path (the
    `ask` approach was removed after the live test).
  - allows ALWAYS (proves no false-positive on legit lead work, incl. mid-session):
    `gh pr view 1868`, `gh pr create --title "merge auth refactor"` (F13 - not a
    merge), `safe-push.sh feat`, `git push --force-with-lease origin feat`,
    `git push origin feat # prep-pr-ok`, `gh pr diff 1868`,
    `git push origin maintenance` (branch name contains "main" substring - must
    NOT block), `gh api repos/o/r/pulls/1` (GET), `gh api repos/o/r/pulls/5/merge` (F14 - GET
    merge-STATUS check, no method/fields), `gh api -X PATCH
    repos/o/r/code-scanning/alerts/5` (CodeQL dismiss - F8),
    `gh api graphql -f query=...resolveReviewThread...` (thread resolve - F8)
  - NOTE: a PUSH entry in "allows ALWAYS" means NOT hard-denied (Tier-1/Tier-2);
    bare, it still hits the `prep-pr-ok` advisory gate, so the harness appends
    `# prep-pr-ok` to the push cases (`safe-push.sh feat`, `--force-with-lease`,
    `maintenance`/`domain`) to assert exit 0 once the override is present. Non-push
    (`gh ...`) entries exit 0 directly.
  - allows when marker ABSENT: `gh pr merge 1868` (solo/non-marker -> floor not active),
    `gh api -X PUT .../pulls/1/merge` (solo/non-marker -> floor not active)
  - regression: no block message contains a string that would re-enable the
    blocked action (no taught bypass).
- **Deps:** python3 (run occasionally, latency irrelevant), the guard script.

### settings.json wiring
- Replace the current inline `PreToolUse.Bash` push hook with a single
  `command` invoking `~/.claude/scripts/orchestrate-guard.sh`. Keep the existing
  Write/Edit secret-file hooks (real deterministic guards - unchanged). KEEP the
  PostToolUse `gh pr merge` print (Finding F16): since `gh pr merge` is now
  FLOOR-GATED (marker-gated deny in a team session), this print is only reached in a
  SOLO/non-marker session where the human or solo lead ran a merge - the
  post-merge-cleanup reminder is still wanted there.
- **Sanctioned `gh pr merge` allow-list entry (#105):** add `Bash(gh pr merge *)`
  (or `Bash(gh pr merge:*)`) to settings.json's `permissions.allow`. This replaces the
  old "omit merge from the allow-list" posture. The floor deny makes the explicit entry
  safe: in a marker-active team session the floor blocks bot merge regardless; in a solo
  session the entry lets the maintainer's `/merge-pr` run prompt-free. Do NOT use a
  blanket `Bash(gh pr *)` - that still hard-fails doctor because it grants more than the
  merge family. The exact sanctioned forms doctor accepts: `Bash(gh pr merge)`,
  `Bash(gh pr merge *)`, `Bash(gh pr merge:*)`, or a specific-flag form.
- **User-approved edit (Finding F27):** editing `~/.claude/settings.json` is a
  user-approved step per the standing rule "never edit settings.json silently."
  Present the exact diff for approval; do not write it unattended. Note the
  one-time install needs a CC restart to load the new hook; the OLD push hook
  stays active (so push-main protection is unbroken) until the restart swaps it.
- **Post-install self-test (Finding F25):** immediately after the wiring +
  restart, run the guard's self-test - feed it a known Tier-1 block payload
  (`git push origin main`) via BOTH channels and assert exit 2. If it does NOT
  block, the guard is silently failing open: STOP and fix before relying on it.
  This is the runtime half of F19's compensating control (the harness is the
  build-time half).

### `orchestrate-setup.py` + teardown (PHASE 2)
- **Purpose:** bootstrap an orchestrate session and own the marker lifecycle.
- **Interface (planned):** `orchestrate-setup.py --team <name>` -> prereq doctor
  (teams env, tmux, clean main, allow-list diff), idempotent guard install into
  settings.json (only if absent), `touch` marker with header, then run the guard
  SELF-TEST (Finding F25): assert a Tier-1 push-main payload blocks (exit 2) AND - now
  that the marker exists - a Tier-2 merge-by-API payload blocks (exit 2) AND a Tier-2
  `gh pr merge` CLI payload blocks (exit 2) too (#105); abort setup if any fails open.
  Teardown -> `rm` marker + the team-teardown checklist. Subjected to
  its own review pass.
- **Doctor shadow check / merge-gate reconciliation (#105):** `check_merge_gate_shadows`
  calls `_merge_rule_shadows` for each allow-rule; `_merge_rule_shadows` now calls
  `_is_merge_scoped` first. A merge-SCOPED allow-rule (`Bash(gh pr merge *)`,
  `Bash(gh pr merge:*)`, `Bash(gh pr merge)`, or a specific-flag form like
  `Bash(gh pr merge --squash)`) is ACCEPTED - it no longer triggers a shadow FAIL,
  because the floor deny backstops it in a marker session and allows it in a solo
  session as intended. Any broader rule (`Bash(gh pr *)`, `Bash(gh pr:*)`, `Bash(gh *)`,
  `Bash(*)`) still hard-fails doctor unchanged.

## Compose change (separate, smaller)

Refactor `SKILL.md`'s IMPLEMENT loop and the implementer/review charters to
DELEGATE to `subagent-driven-development` as the inner primitive (fresh
subagent per task + spec-then-quality review), keeping ONLY the orchestrate
deltas: PR-blindness, the permission charter, the persistent-teammate lifecycle,
and the outward PR pipeline. Delete the re-derived prose. Do NOT reference
`dispatching-parallel-agents` (out of scope per the maintainer). Update the
relevant `REVIEW-FINDINGS.md` items to "closed by deterministic floor."

**Caveat the compose MUST state (Finding F22):** `subagent-driven-development`
explicitly forbids dispatching multiple implementation subagents in parallel
("conflicts = Never") because it assumes they share a worktree. Orchestrate's
OUTER model runs many implementers concurrently - that is its whole point. The
delegation therefore scopes to the SINGLE-TASK inner loop (one implementer +
spec->quality review for one cluster); orchestrate's cross-cluster parallelism is
safe ONLY because each implementer is on a DISJOINT worktree, so the shared-file
conflict premise that motivates that skill's no-parallel rule does not hold. The
SKILL.md prose must make this boundary explicit so a reader does not "fix" the
parallelism to comply with the sub-skill.

## Testing strategy

1. `test-orchestrate-guard.py` table green in both marker states (the
   deterministic proof).
2. Hostile-critic convergence pass (the same dog-food that produced
   `REVIEW-FINDINGS.md`): parallel read-only critics hunt (a) new honest-path
   bypass spellings and (b) false-positives - legitimate lead commands wrongly
   blocked - looping until DRY (K=2 clean rounds). This is the "passes the ralph
   loop / critical eye" gate before declaring phase 1 done. The reusable,
   target-agnostic protocol for this kind of pass lives at
   `~/Developer/cc-orchestrator/engage-ralph-loop.md` (instantiate it with this floor
   as the TARGET, the honest-path threat model as the SCOPE, the three gates
   below, and the Python-driver ISOLATION method).
3. **Test-driving method (load-bearing).** NEVER place trigger substrings (`git
   push`, `main`, `gh pr merge`, `pulls/N/merge`) on the Bash tool's own command
   line when exercising the guard - the LIVE guard inspects the caller's command
   and blocks the test invocation itself. Construct payloads INSIDE a Python
   driver (as `test-orchestrate-guard.py` does) or a script file, so the command
   line the hook sees is inert (`python3 driver.py`). This repeatedly bit the
   guard's own author and every critic; it is the single biggest footgun when
   working ON the guard.

## Phases / sequence (maintainer-set)

- **Phase 1:** `orchestrate-guard.sh` + marker + `test-orchestrate-guard.py` +
  settings.json wiring + the SKILL.md compose change. Also make `head_sha`
  REQUIRED in `stack.schema.json` as a PRECONDITION (a branch cannot be stacked
  without a pinned SHA). NOTE (Finding F17): the schema `required` is necessary
  but NOT itself a deterministic gate - the enforcing check (pr-shipper
  hard-compares `head_sha` to the pushed branch HEAD before `gh pr create`, and
  refuses on mismatch) is pr-shipper behavior and lands with the phase-3 lifecycle
  tranche; phase 1 only adds the schema field. GATE (Finding F26 - two parts):
  (a) the FLOOR gate = `test-orchestrate-guard.py` green (both channels, both
  marker states) AND the post-install self-test blocks AND the hostile-critic pass
  is dry; (b) the COMPOSE gate is separate (the prose has no harness) = a
  read-through confirming the F22 disjoint-worktree caveat is present and no
  delegated sub-skill rule is silently violated. Both must pass.
- **Phase 2:** `orchestrate-setup.py` + teardown in claude-kit; subjected to its
  own harness + critic pass.

## Out of scope this pass (named follow-ups)

The remaining `REVIEW-FINDINGS.md` HIGH lifecycle/concurrency items are a
DISTINCT hardening tranche (mostly charter/prose/lifecycle, not deterministic
gates): ref-ownership race (single ref-advancer), single-writer stack, loop
caps + pr-watch exit-code branching, port allocator, worktree keep-until-merge,
teardown clean-worktree assertion. Track as a phase-3 tranche. Adversarial-
evasion containment (a true allowlist-only/sandbox model) is explicitly not
attempted.

## Known honest-path limitations (documented + accepted)

These are NOT closed, by design - they fall outside the "honest-but-misaligned
bot on the obvious path" threat model or are backstopped elsewhere. Listed so an
implementer does not mistake them for bugs:

- **F4 - Bash-tool-only coverage.** The guard inspects the `Bash` tool's command
  string. A merge issued through a different tool (e.g. a GitHub MCP server's
  merge tool, were one connected) bypasses it. No GitHub MCP is connected today
  (only context7 + playwright). IF one is ever added, it needs its own
  PreToolUse matcher on that tool name. Documented, not built.
- **F5 - bare push of a main-checked-out worktree.** `safe-push.sh` / `git push`
  with no explicit target, run from a worktree whose current branch IS main,
  pushes main with no "main" token in the command string, so the token match
  cannot catch it. Mitigation: server-side branch protection rejects it, and
  teammates never work on main-tracking worktrees (always feature branches). The
  floor matches the explicit-target form; branch protection is the backstop for
  the implicit form.
- **F5b - "git push" phrase inside a commit message.** `looks_like_git_push`
  anchors the `push` SUBCOMMAND (after `git` + global opts), so `git commit -m
  "...push to main..."` is correctly NOT treated as a push (the reported live
  false-positive, fixed). The residual: the literal adjacent phrase `git push`
  appearing in commit-message prose, plus a `main` token, still matches. Far
  narrower than the reported case; honest-path prose, not closed.
- **G2 - glued short-flag force.** `git push -fu origin feat` (the `-f` force
  flag glued into a short-flag cluster) is not caught by the whole-word `-f`
  matcher. Rare honest spelling; the damage (force-push a FEATURE branch) is
  recoverable; the lead's default is `--force-with-lease`. Maintainer-chosen to
  document rather than chase with a broader cluster regex.
- **Bare-push target resolution DECLINED.** A live bug report proposed resolving
  a bare `git push`'s target by reading the current branch of the operated-on
  worktree (honoring `cd` / `git -C` / a worktree path). That is the F5 case and
  is deliberately NOT built: it requires git-state resolution inside a string
  hook (the "parse the shell" path this design rejects), and branch protection
  already backstops a bare push to main. The floor matches the explicit-target
  form only. The SAME report's other claims (`git log/diff/merge-base main..HEAD`
  wrongly blocked) were FALSE on verification - those have no `push` subcommand,
  so they were never matched; only the commit-message case was a real bug.
- **F5c - quoted global-option arg containing a space.** The anchored push
  matcher tolerates `git -C <dir> push` / `-c <kv> push` by consuming the option's
  arg up to the next space. A QUOTED arg that contains a space (`git -C "dir with
  space" push origin main`, `git -c user.name="A B" push origin main`) hides the
  following `push` token, so the push is not recognized. Closing it robustly needs
  shell-quote parsing (the rejected "parse the shell" path); a partial regex would
  fix `-C "path"` but not the mixed `-c name="A B"` form - an inconsistent
  half-measure. Backstopped: it is push-to-main (branch protection rejects it) and
  worktree paths in this workflow contain no spaces, so it is a near-nonexistent
  honest spelling. Documented, not chased. (Right-gate lesson: the string hook
  deliberately does NOT parse shell quoting - branch protection is the backstop
  for any push-to-main spelling the token match misses.)
- **Compound false-positive (FIXED via per-clause).** The hard denies evaluate
  per shell clause (split on `&& || ; |`, existing newlines collapsed first so a
  backslash-continued push to main stays one clause), so a `main`/`--force` token
  in a non-push clause (`git checkout main && git push feat`) no longer trips a
  deny meant for the push clause. A perf pre-filter skips the split entirely when
  neither `push` nor a `gh` word is present (keeps ordinary pipelines O(1)).
- **F30 - `--no-verify` / `--admin` token inside a commit message or quoted arg of
  a flag-accepting (sub)command.** The two newer Tier-1 denies (`git <sub> --no-verify`
  skip-hooks, `gh pr merge --admin` protection-bypass) are now SUBCOMMAND-anchored
  (mirroring F13): the no-verify deny requires one of the git subcommands that actually
  accept the flag (commit/push/merge/rebase/cherry-pick/am/revert/pull), and the admin
  deny requires the `pr ... merge` subcommand (the only gh subcommand that accepts
  `--admin`, verified against `gh <sub> --help`). That anchoring removed the broad
  prose/quoted-arg false-positive class - `gh pr create --title "... --admin"`,
  `gh issue comment -b "document the --admin flag"`, `git commit -m "... gh ... --admin"`,
  and `gh issue create --title "ban --no-verify in git workflows"` all carry the
  trigger substrings but no real bypass subcommand, so none match now. The IRREDUCIBLE
  residue: `git commit -m "...--no-verify..."` (the flag-word appears inside the commit
  MESSAGE of a subcommand that genuinely accepts `--no-verify`) still hard-blocks, since a
  string hook cannot tell a real flag from the same token quoted in the `-m` value without
  parsing shell quoting (the rejected "parse the shell" path). Rare honest spelling; the
  fix is to reword the message (avoid the literal `--no-verify` token) or, if truly needed,
  run the commit yourself via the `!` prefix. Maintainer-chosen to DOCUMENT rather than
  chase with quote-parsing. NEEDS LEAD RATIFICATION. (The symmetric admin residue is
  narrower but NOT nonexistent: the admin deny needs the `gh` WORD plus the `pr` and
  `merge` subcommand words plus a whole-word `--admin` all in one clause, so a `git commit
  -m "...pr merge --admin..."` that lacks the literal `gh` word ALLOWS. But a PR comment /
  issue body that QUOTES a full bad command - `gh pr comment -b "never run gh pr merge
  --admin on prod"`, plausible since the maintainer documents this very guard - DOES
  residually block, UNLESS the `--admin` token happens to be glued to the closing quote
  (`...--admin"`), which escapes the whole-word right boundary. That quote-boundary luck is
  incidental, not a designed escape; the honest characterization is: quoting the literal
  `gh pr merge --admin <more>` phrase in a flag value still blocks, same accepted residue
  family as the commit-message `--no-verify` case. Reword (drop the trailing word so the
  token ends the quote, or avoid the literal phrase) or run via `!`. Same DOCUMENT-not-
  chase call; closing it needs shell-quote parsing. Verified across Iter-7 rounds 2-3.)
- **Adversarial evasion** (aliases, `$(...)`, base64, wrapper scripts) is out of
  scope per the threat model - a string hook is not a sandbox.

## Iteration log (adversarial hardening)

- **Iter 1** (F4-F9): narrowed Tier-2 to MERGE-only, dropping generic `gh api -X`
  to charter-level - it would have blocked the lead's own `/handle-review`
  (resolveReviewThread, CodeQL dismiss) mid-session, the floor cannot tell lead
  from bot, and those are recoverable not irreversible (F8). Made merge-by-API
  match on PATH not method (F6, fields imply POST). Word-boundary `--force` with a
  `-with-lease` negative-lookahead (F9, substring trap). Documented Bash-only (F4)
  and bare-push-on-main (F5) as accepted honest-path limitations. Clarified the
  marker TTL as a self-heal backstop, teardown as the real off-switch (fail-open
  after TTL is deliberate).
- **Iter 2** (F10-F17): separated the no-echoed-bypass rule (hard floor denies,
  no override) from the preserved `prep-pr-ok` advisory gate which keeps its echo,
  resolving an apparent contradiction (F10). Added matching rules for git global
  opts `git -C/-c ... push` (F11) and gh global flags `gh -R ... pr merge` (F12),
  both of which the naive adjacency regex misses and the lead uses routinely.
  Required `pr merge` SUBCOMMAND anchoring not "merge" substring, else
  `gh pr create --title 'merge ...'` false-positives (F13). Qualified merge-by-API
  to MUTATING calls only so a bare `gh api .../pulls/N/merge` GET status-check is
  allowed (F14). Marker default must `$HOME`-expand not literal `~` (F15). Keep
  the PostToolUse merge print - only reached on allowed solo merges now (F16).
  Scoped `head_sha`: schema `required` is a phase-1 precondition, the enforcing
  SHA-compare is a phase-3 pr-shipper item (F17).
- **Iter 3** (F18-F23): pinned the command-input channel (stdin JSON, fall back to
  `$TOOL_INPUT`, fail-open on empty) - reading the wrong channel would have
  silently nullified the floor (F23). Specified evaluation order so the
  `# prep-pr-ok` override is checked LAST and can never bypass a hard deny (F18).
  Defined fail-OPEN error behavior with harness + install self-test as the
  compensating control (F19). Documented the macOS `stat -f %m` mtime read (F20)
  and the global per-user marker scope - any active session gates merge everywhere
  (F21). Added the compose caveat: orchestrate's parallel implementers contradict
  `subagent-driven-development`'s no-parallel rule, safe only via disjoint
  worktrees (F22).
- **Iter 4** (F24-F27): second-order enforcement gaps. Harness must run each case
  through BOTH input channels + a both-empty fail-open case, else F23's fallback
  ships untested (F24). Wired F19's install self-test into the actual install
  steps (phase-1 wiring + phase-2 setup), feeding a known block payload and
  aborting on fail-open - otherwise the compensating control was inert (F25).
  Split the phase-1 GATE: the floor gate (harness + self-test + critic-dry) is
  distinct from the compose gate (prose read-through for the F22 caveat + sub-skill
  integrity) (F26). Marked settings.json wiring a user-approved diff per "never
  edit settings.json silently," with the old hook protecting until the restart
  (F27).
- **Iter 5** (no new substantive finding - design converged): polish only on
  already-decided rules. Clarified the whole-word main/master rule catches a
  refspec destination (`HEAD:main`) but not a `maintenance`-style substring, and
  required a boundary regex over a positional-equality test; added both as harness
  regression cases. Made "runtime stat on every relevant call" precise: the guard
  short-circuits and only stats the marker when a merge pattern matched (protects
  the ~5ms budget). No design change; no new failure class found.
- **Iter 6** (no new substantive finding - CONVERGED): full re-read confirmed
  internal consistency (Tier-2 merge-only throughout; F4-F27 coherent; harness
  covers both input channels + whole-word false-positive cases). Second
  consecutive clean pass; spec converged.
- **Iter 7 - FP sweep of the two newer Tier-1 denies (F30):** ran the bounded
  engage-ralph-loop scoped to FALSE POSITIVES on `gh --admin` and `git --no-verify`.
  Everyday git verbs (Class A), gh non-admin subcommands (Class B), and substring
  boundaries (Class D: `--administrator`/`--no-verifyx`) were already clean. The
  high-value quoted/arg-position class (Class C) found two real false-positives, both
  from the denies using a bare `gh`/`git` word + flag SUBSTRING instead of the F13
  subcommand anchoring used everywhere else: (1) `gh issue comment -b "document the
  --admin flag"` and `git commit -m "...gh...--admin..."` wrongly blocked; (2)
  `gh issue create --title "ban --no-verify in git workflows"` wrongly blocked. FIX:
  subcommand-anchor both - `--admin` only on `gh pr merge` (the sole accepting
  subcommand), `--no-verify` only on commit/push/merge/rebase/cherry-pick/am/revert/pull.
  Removed the whole prose/quoted-arg class WITHOUT weakening the real bypasses
  (`gh pr merge --admin`, `git <sub> --no-verify`, push-main, bare-force all still block;
  verified by re-running the full harness both directions). Updated the obsolete
  `gh repo edit --admin -> block` harness case (repo edit takes no `--admin`; gh would
  error, so it is not a real bypass) to `-> allow`, and added regression cases for the
  now-allowed legit forms AND the still-blocked bypasses. Residue documented as F30.

## Artifact locations

Author in `~/Developer/claude-kit` (the gist = CANONICAL source). `~/.claude/scripts/*.sh`
are SYMLINKS into the gist (e.g. existing `pr-watch.sh`/`safe-push.sh`), so the
GUARD is `ln -s`'d to `~/.claude/scripts/orchestrate-guard.sh` (the path settings.json
+ the `Bash(~/.claude/scripts/*.sh *)` allow-list reference) - no copy to drift, gist
edits are live. The HARNESS (`test-orchestrate-guard.py`) stays in the gist ONLY (a
dev/test artifact run from there; resolves the guard via its own dir; not referenced by
settings.json, so NOT symlinked). (Phase 2) `orchestrate-setup.py` likewise lives in the
gist; if it needs a `~/.claude/scripts/` entry it is symlinked the same way. This design
doc lives at
`~/.claude/skills/orchestrate/DESIGN-deterministic-floor.md` (next to
`REVIEW-FINDINGS.md`; that dir is not a git repo, so it is not committed there -
the scripts are versioned in the gist).
