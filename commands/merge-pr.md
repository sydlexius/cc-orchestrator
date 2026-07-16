---
description: "Merge a PR with CodeRabbit status check, squash, and post-merge cleanup"
argument-hint: "<PR-number> (e.g. 738)"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Edit", "Write", "Agent", "Skill"]
---

# Merge PR

Safe merge workflow: verify CodeRabbit status, check CI, squash-merge, and clean up.

**PR number:** $ARGUMENTS

If `$ARGUMENTS` is a number, use it directly. Otherwise detect from the current branch:

```bash
pr_number="$ARGUMENTS"

if [ -z "$pr_number" ]; then
  pr_number=$(gh pr view --json number --jq .number 2>/dev/null)
fi
```

If still no PR found, stop and ask: "Which PR number should I merge?"

---

## Step 1 -- Pre-flight checks

The deterministic merge-readiness gate is `ship-gate-preflight.sh` (the #110
oracle). It reads GitHub's `statusCheckRollup` (every check terminal + acceptable,
no required/non-required split, fail-closed) AND calls `pr-unreplied-comments.sh`
internally to require 0 actionable review-body findings (including CodeRabbit's
outside-diff findings, corrected in #132) AND couples GitHub's `reviewDecision`.
So it SUPERSEDES the old `mergeStateStatus`/`mergeable` read, the hand-rolled
CHANGES_REQUESTED enumeration, and the direct `pr-unreplied-comments.sh` call --
those are removed here (the oracle covers all three). It exits 0 = ready, 2 =
block (any red/incomplete check, unaddressed finding, or lookup error), 1 = usage.

Run these in parallel:

```bash
repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)

# Deterministic merge-readiness oracle (CI rollup + unreplied review-body findings
# + reviewDecision). Exit 0 = ready; exit 2 = block (stderr names the failing gate).
bash ${CLAUDE_PLUGIN_ROOT}/scripts/ship-gate-preflight.sh "$pr_number" "$repo"

# Coverage advisory (informational; outside the oracle's scope). This is only
# meaningful on repos that run a coverage service (e.g. codecov). When the repo
# has no coverage integration, the script returns {"status":"none"} -- treat that
# as SKIP / "N/A", not a failure and not a merge block (there is simply no
# coverage comment yet). Only a returned threshold-fail is worth surfacing, and
# even then it is advisory.
bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh --coverage-only $pr_number

# Code-scanning (GHAS / CodeQL) alerts -- a SEPARATE API surface the oracle and the
# comment scripts can't see. Do not merge with open, untriaged code-scanning alerts.
bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-codeql-autofixes.sh $pr_number
```

If `ship-gate-preflight.sh` exits non-zero, STOP and report its stderr message
(which names the failing gate -- a red/incomplete check, an unaddressed
review-body finding, or an unrecognized reviewDecision). For unaddressed review
findings, suggest running `/handle-review $pr_number` first. Only proceed to
Step 2 when the oracle exits 0.

If the code-scanning surfacer reports open alerts, stop: triage them first (apply
the autofix / fix, or dismiss genuine false positives via the documented
`gh api -X PATCH` route) before merging. Do not merge over untriaged alerts.

### Coverage status check (advisory, not blocking)

This step is OPTIONAL and only applies when the target repo uses a coverage
service (codecov being the common one). It is not assumed present: a repo with
no coverage integration posts no codecov signal, and this step self-skips cleanly
with nothing to report.

The `--coverage-only` readout from Step 1 already carries the one gating signal:
`threshold_state`, derived from the **`codecov/patch` check-run** conclusion on the
head SHA (NOT the codecov comment glyph, NOT the legacy `commits/<sha>/status`
endpoint -- see #239). Do NOT do a separate coverage read here; use that JSON.

`threshold_state` values and how to treat them:

- `fail` -- the `codecov/patch` check-run itself FAILED (a real patch-coverage gate
  failure). **Warn, do not block** (coverage is a policy signal, not a correctness
  gate). Print:

  ```text
  Coverage advisory: the codecov/patch gate failed on this PR (patch coverage <pct>%).
  Report: <url>

  Merging anyway is fine if the coverage regression is intentional or the
  missing lines are in generated code. Cancel and add tests if not. Proceed?
  (yes / cancel / show-report)
  ```

  Default to waiting for explicit confirmation. If the user chose to proceed in a
  prior conversation turn, do not re-prompt.

- `pass` / `none` (i.e. any state other than `fail`), or `status: none` -- **SKIP
  this prompt entirely.** Only `fail` ever pauses; `build_coverage_advisory` emits
  exactly `pass | fail | none`.
  Do NOT pause or prompt the maintainer. `none` means there is no gating codecov
  check-run (advisory-only); `pass` means the patch gate passed. A codecov comment
  that shows uncovered lines (its `comment_glyph` is `uncovered`) is NOT a gate
  failure and MUST NOT trigger a prompt -- that mixed signal was the #239 spurious
  pause. The absent-signal case is a SKIP, never a block.

Merge safety is unaffected: a genuine coverage regression that the repo treats as
required (e.g. a `Coverage Floor Check` CI check) is still enforced by the CI-green
gate inside `ship-gate-preflight.sh` (statusCheckRollup), which STOPPED the merge in
Step 1 above before this advisory ever runs. This advisory only decides whether to
prompt on a non-required coverage signal; it never widens what can merge.

---

## Step 2 -- CodeRabbit status check

First, check if CR has already approved HEAD:

```bash
head_sha=$(gh pr view $pr_number --json headRefOid --jq .headRefOid)
cr_latest=$(gh api "repos/$repo/pulls/$pr_number/reviews" --paginate \
  --jq '[.[] | select(.user.login == "coderabbitai[bot]")] | sort_by(.submitted_at) | last | {state, commit_id}')
cr_state=$(echo "$cr_latest" | jq -r .state)
cr_commit=$(echo "$cr_latest" | jq -r .commit_id)
```

**If CR's latest review is APPROVED on HEAD** (`cr_state == "APPROVED"` and
`cr_commit == head_sha`): skip the status poll entirely. Print:
"CR already approved on HEAD ($head_sha) -- skipping status check."

**Otherwise**, post a status check and poll:

```bash
baseline=$(date -u +%Y-%m-%dT%H:%M:%SZ)
gh pr comment $pr_number --body "@coderabbitai status"
```

Poll for CR's response by checking for a new issue-level comment from `coderabbitai`
that appeared AFTER the baseline timestamp.

```bash
# Poll at 10s intervals, max 6 attempts (60s total)
for i in 1 2 3 4 5 6; do
  sleep 10
  response=$(gh pr view $pr_number --comments --json comments \
    --jq "[.comments[] | select(.author.login == \"coderabbitai\" and .createdAt > \"$baseline\")] | length")
  if [ "$response" -gt 0 ]; then
    echo "CR responded"
    break
  fi
  echo "Poll $i: waiting for CR response..."
done
```

Once CR responds, fetch and parse the response:

```bash
gh pr view $pr_number --comments --json comments \
  --jq '[.comments[]
         | select(.author.login == "coderabbitai" and .createdAt > "'"$baseline"'")
        ] | sort_by(.createdAt) | last | .body'
```

### Parse the response

Look for these indicators in CR's status response:

**Safe to merge:**
- "all major/critical findings have been addressed"
- "ready to merge"
- All items show checkmarks or "Fixed"/"Resolved" status
- No items marked as "Open" with Major/Critical severity

**Not safe to merge:**
- "review in progress" or "pending review"
- Items marked "Open" with Major or Critical severity
- "rate limited" (CR hasn't reviewed yet)

If CR indicates a review is in progress, wait and re-poll (up to 60 seconds total).

If CR flags open Major/Critical items, stop and report them. Suggest running
`/handle-review $pr_number` first.

If CR doesn't respond within 60 seconds, fall back to the commit-based check:

```bash
if [ "$cr_commit" != "$head_sha" ]; then
  echo "WARNING: CR has not reviewed HEAD ($head_sha). Last reviewed: $cr_commit"
  # Ask user whether to proceed
fi
```

---

## Step 3 -- Merge (marker-aware; without local branch delete)

The pre-flight (Steps 1-2) is identical in every session. Only the irreversible
merge step itself branches on whether an **orchestrate floor marker** is active.

First detect the marker. This MUST mirror the deterministic floor's
`marker_active()` (in `orchestrate-guard.sh`) exactly, so `/merge-pr` and the guard
never disagree: keyed by the SESSION KEY (sanitized, `LC_ALL=C` so the byte length
matches the guard), under `$FLOOR_DIR`, fresh within `$TTL_HOURS`.

#312: the session key is the sanitized `$TMUX` when set, else `ccsid_` + the sanitized
`$CLAUDE_CODE_SESSION_ID` - **tmux is NOT required for a gated session**, so a non-tmux
session is NOT automatically solo. Only a session with NEITHER identifier is unkeyed and
therefore never gated. Match ANY candidate key, like the guard does. (This is one of the
SIX live copies of the derivation listed in `orchestrate-guard.sh`'s DERIVATION REGISTRY;
they must move together.)

```bash
FLOOR_DIR="${ORCHESTRATE_FLOOR_DIR:-$HOME/.claude/orchestrate-floor.d}"
TTL_HOURS="${ORCHESTRATE_FLOOR_TTL_HOURS:-72}"
case "$TTL_HOURS" in ''|*[!0-9]*) TTL_HOURS=72 ;; esac          # bad TTL must not disarm
[ "$TTL_HOURS" -ge 1 ] 2>/dev/null || TTL_HOURS=72
marker_active=0
# #312: candidate keys, first-precedence first - the sanitized $TMUX when set, AND/OR
# `ccsid_` + the sanitized $CLAUDE_CODE_SESSION_ID. Check EVERY candidate, exactly like the
# guard: checking only $TMUX reported marker_active=0 for a gated NON-tmux session, routing
# this command to the "solo -> merge directly" path while the floor then hard-denied the very
# command that path emits.
keys=""
if [ -n "${TMUX:-}" ]; then
  keys="$(printf '%s' "$TMUX" | LC_ALL=C tr -c 'A-Za-z0-9' '_')"
fi
if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
  keys="$keys
ccsid_$(printf '%s' "$CLAUDE_CODE_SESSION_ID" | LC_ALL=C tr -c 'A-Za-z0-9' '_')"
fi
for key in $keys; do
  [ -n "$key" ] || continue
  marker="$FLOOR_DIR/$key"
  [ -f "$marker" ] || continue
  mtime=$(stat -c %Y "$marker" 2>/dev/null || stat -f %m "$marker" 2>/dev/null) || mtime=0  # GNU stat -c || BSD stat -f (mirrors the guard)
  now=$(date +%s)
  age_h=$(( (now - mtime) / 3600 ))
  if [ "$mtime" -gt 0 ] && [ "$age_h" -lt "$TTL_HOURS" ]; then
    marker_active=1
    break
  fi
done
echo "marker_active=$marker_active"
```

Note (both paths): do NOT pass `--delete-branch`. When a worktree holds the branch,
`gh pr merge` fails the local-delete step (the remote delete still succeeds
server-side), leaving the workflow half-done and non-idempotent. Branch cleanup is
handled explicitly in Step 4 so the sequence is safe to re-run.

### Step 3a -- marker_active=1 (active orchestrate session): hand the merge to the human

The merge is the one irreversible, unforgeable step, so in an orchestrate session it
stays human-executed. Do NOT attempt the merge yourself: the global guard hard-DENIES
the mutating merge-by-API path (`gh api ... pulls/N/merge`) whenever the marker is
fresh, and `gh pr merge` is withheld by the allow-list (an auto-mode bot stalls; a
default-mode lead is prompted). Instead PRINT the exact command for the human to run
in a SEPARATE PLAIN TERMINAL (outside the IDE) or via the GitHub UI -- NOT as an
in-session `!` bang, which fails outright in IDE-hosted sessions -- then STOP and wait:

```text
All pre-merge checks passed for #<pr_number>.
This is an active orchestrate session, so the merge itself is yours to run
(the one irreversible step, unforgeable by a bot). Run it in a SEPARATE PLAIN
TERMINAL outside the IDE, or use the GitHub UI "Squash and merge" button:

    gh pr merge <pr_number> --squash

Do NOT run it as an in-session `! ...` bang: in an IDE-hosted session the bang
shell can fail outright and the merge silently will not happen. Then tell me it
landed (or I can verify) and I'll finish post-merge cleanup.
```

Do not proceed to Step 4 until the merge is confirmed. After the human says it
landed, VERIFY before cleaning up (cleanup on an unmerged PR is wrong):

```bash
state=$(gh pr view $pr_number --json state --jq .state)
echo "PR #$pr_number state: $state"   # expect MERGED
```

If `state` is `MERGED`, continue to Step 4. If not, report the actual state and wait
(the human may still be running it, or branch protection blocked it).

### Step 3b -- marker_active=0 (solo / non-tmux session): merge directly

```bash
gh pr merge $pr_number --squash
```

If merge fails, stop and explain. Common causes:
- Branch protection rules not met
- Merge conflicts (need rebase)
- Required checks not passing

---

## Step 4 -- Hand off to /post-merge-cleanup

Invoke `/post-merge-cleanup` with the same PR number via the `Skill`
tool (`skill=post-merge-cleanup`, `args=$pr_number`). That skill owns
the full ordered cleanup sequence:

- Worktree removal (via `${CLAUDE_PLUGIN_ROOT}/scripts/cleanup-worktree.sh`)
- Local + remote branch deletion
- Stale ref pruning
- **Local main update** (the gap this chain closes; the original
  /merge-pr stopped at branch cleanup and left main stale)
- Linked-issue verification (each `Closes #N` reference resolved to
  CLOSED, or flagged OPEN with guidance)

This handoff is mandatory -- not a "do this if you remember." It applies
REGARDLESS OF MERGE METHOD: whether the merge landed via the GitHub UI
"Squash and merge" button, a `gh pr merge` run in a separate terminal,
or any out-of-band / human-merged-without-telling-the-lead path, the
verification and cleanup handoff proceed identically. It is the
sole reason cleanup logic lives in /post-merge-cleanup and not inline
here: a single source of truth that can evolve (e.g. for leased-main
edge cases, idempotent re-runs, or new GitHub API behaviours) without
needing to be kept in sync with a duplicate in this file.

### Re-run safety

`gh pr merge` on an already-merged PR is a no-op ("pull request already
merged") and /post-merge-cleanup is fully idempotent. If a prior
/merge-pr run crashed mid-flight (for instance, between Step 3's merge
and Step 4's cleanup), re-invoking `/merge-pr $pr_number` will finish
the workflow without duplicate effort or errors.

---

## Step 5 -- Summary

The /post-merge-cleanup invocation prints its own detailed cleanup
summary. This step's job is just to confirm the merge itself, and to
acknowledge that cleanup ran:

```text
## Merged

- PR: #$pr_number
- CR status: <verified clean / fallback check / skipped>
- Coverage: <patch_pct>% <threshold_state> | N/A
- Commit: <squash merge SHA>
- Post-merge cleanup: see /post-merge-cleanup output above
```

Display "N/A" for Coverage when `--coverage-only` returned `{"status":"none"}`
(no coverage service on the repo) -- a missing coverage signal is not a failure.
When `threshold_state` is `none` (codecov commented but posts no gating
`codecov/patch` check-run), show `<patch_pct>% (advisory-only)` -- also not a failure.
