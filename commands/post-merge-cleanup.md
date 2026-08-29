---
description: "Post-merge cleanup: update main, remove worktree, delete branches, prune refs, verify linked issues"
argument-hint: "<PR-number> (e.g. 32)"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Edit"]
---

# Post-Merge Cleanup

Run after a PR is merged to clean up the working environment.

**PR number:** $ARGUMENTS

If no PR number is provided, stop and ask: "Which PR number was just merged?"

```bash
pr_number="${ARGUMENTS:-}"
if [ -z "$pr_number" ]; then
  echo "No PR number provided. Usage: /post-merge-cleanup <PR-number>" >&2
  exit 1
fi
```

---

## Step 1 -- Gather PR metadata

```bash
repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)

gh pr view "$pr_number" --json \
  headRefName,mergeCommit,closingIssuesReferences,state \
  --jq '{branch: .headRefName, sha: .mergeCommit.oid, state: .state, issues: [.closingIssuesReferences[].number]}'
```

Capture:
- `branch` -- the feature branch name
- `sha` -- the merge commit SHA
- `state` -- must be MERGED; if not, stop: "PR #$pr_number is not merged yet."
- `issues` -- list of issue numbers auto-closed by this PR (may be empty)

---

## Step 2 -- Update local main

Picking the right command depends on which worktree (if any) currently
holds `main`. Reaching for `git checkout main && git pull --ff-only`
unconditionally breaks when the main worktree path is leased to a
feature branch with uncommitted work, which happens whenever this skill
runs from a sibling worktree (or, as is sometimes the case during a
long-running agent session, when the main path itself is holding the
feature branch).

```bash
current=$(git rev-parse --abbrev-ref HEAD)
if [ "$current" = "main" ]; then
  # We're on main. Standard fast-forward pull.
  git pull --ff-only
else
  # Find which worktree (if any) has main checked out.
  main_wt=$(git worktree list --porcelain | awk '
    /^worktree /{wt=$2}
    /^branch refs\/heads\/main$/{print wt; exit}
  ')
  if [ -z "$main_wt" ]; then
    # No worktree has main checked out -- safe to update the local ref
    # directly via a fetch refspec. No working-tree disturbance.
    git fetch origin main:main
  else
    # A sibling worktree owns main. Pull from there with -C so we don't
    # need to switch contexts.
    git -C "$main_wt" pull --ff-only
  fi
fi
```

If the update fails with "not possible to fast-forward", check whether
the divergence is a squash-merge artifact:

```bash
git log --oneline main...origin/main
```

If local main has exactly 1 commit that is a strict ancestor of the
squash commit (same diff, different SHA), it is safe to reset. Run the
reset from whichever worktree owns main (or via `git -C "$main_wt"
reset --hard origin/main`); do **not** reset from a worktree that
doesn't own main.

If local main has unique commits not present in the remote, stop and
explain. Do not force-pull or merge.

---

## Step 3 -- Remove worktree (if one exists for this branch)

```bash
git worktree list --porcelain \
  | awk -v b="refs/heads/$branch" '
      $1=="worktree"{wt=$2}
      $1=="branch" && $2==b{print wt}
    '
```

If a worktree path is found:
- Check for uncommitted changes: `git -C "$wt_path" status --porcelain`
- If dirty, stop and warn: "Worktree at $wt_path has uncommitted changes. Clean up manually or pass --force."
- If clean: `git worktree remove "$wt_path"`

If no worktree is found, note it and continue.

---

## Step 4 -- Delete local branch

```bash
if ! git show-ref --verify --quiet "refs/heads/$branch"; then
  echo "local branch $branch not found (already deleted)"
elif git branch -d "$branch"; then
  echo "deleted local $branch (merged)"
else
  echo "ERROR: local branch $branch is unmerged; refusing to delete." >&2
  echo "Verify the merge landed, then delete it manually with 'git branch -D $branch'." >&2
  exit 1
fi
```

Do not use `-D` (force). The three-branch check above never swallows the
unmerged error: a `git branch -d` failure means the branch is unmerged, so the
script stops and warns rather than silently leaving it (or force-deleting it).

---

## Step 5 -- Delete remote branch

VERIFY THE OUTCOME; do not infer it from the HTTP status (#337). GitHub returns
404 OR 422 "Reference does not exist" for an absent ref depending on the path
taken, so the old `grep -q '404'` test was incomplete BY CONSTRUCTION -- and a
squash-merge with auto-delete-branch (the common case here) returns exactly the
422, which this step then misreported as an error on a branch that was deleted
correctly. Widening the grep to `404|422` was rejected: 422 is generic
Unprocessable Entity, so the bare code would also swallow genuine failures, and
matching the message text pins on error PROSE rather than an API contract.

So: attempt the delete capturing stderr for DIAGNOSTICS ONLY, then let
`git ls-remote --exit-code` decide. This tests the condition actually being
asserted, is immune to every status-code variation, and is already the house
pattern -- `safe-push.sh` verifies the remote ref MOVED rather than trusting an
exit code. Use the fully-qualified ref form so it cannot match a tag or be
ambiguous.

```bash
encoded_branch=$(printf '%s' "$branch" | jq -sRr @uri)
err_file=$(mktemp)
del_rc=0
gh api "repos/$repo/git/refs/heads/$encoded_branch" -X DELETE >/dev/null 2>"$err_file" || del_rc=$?

# GIT_TERMINAL_PROMPT=0 + SSH BatchMode so an auth-required origin fails FAST
# rather than hanging cleanup on a credential prompt; a caller's own
# GIT_SSH_COMMAND is preserved. Matches scripts/cleanup-worktree.sh exactly.
if GIT_TERMINAL_PROMPT=0 \
   GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh} -o BatchMode=yes" \
   git ls-remote --exit-code origin "refs/heads/$branch" >/dev/null 2>&1; then
  # Ref STILL PRESENT: a genuine failure regardless of what the API reported.
  # This also catches the inverse trap -- a DELETE that reports success while the
  # ref survives, which trusting the exit code would have called clean.
  echo "ERROR: remote head refs/heads/$branch is STILL PRESENT after delete." >&2
  [ -s "$err_file" ] && cat "$err_file" >&2
  echo "  Investigate: git ls-remote origin \"refs/heads/$branch\"" >&2
  rm -f "$err_file"
  exit 1
elif [ "$del_rc" -eq 0 ]; then
  echo "deleted remote $branch (verified absent on origin)"
else
  # The DELETE complained but the ref is verifiably gone -- the already-deleted
  # case (404, or the 422 GitHub returns after auto-delete). Annotate, don't fail.
  echo "remote branch $branch already deleted (verified absent; DELETE reported an error)"
  [ -s "$err_file" ] && sed 's/^/  api: /' "$err_file"
fi
rm -f "$err_file"

# Confidence guard: the local branch should already be absent after Step 4.
if git show-ref --verify --quiet "refs/heads/$branch"; then
  echo "WARNING: local branch $branch still exists (expected gone after Step 4)." >&2
fi
```

`git ls-remote --exit-code` exits non-zero when the ref is absent (the expected
outcome, and the success verdict here) and zero when it still exists (the
failure verdict). Note the inversion relative to the old code: the status text
now only ANNOTATES the message, it never decides it.

Note: `scripts/cleanup-worktree.sh` carries the same verify-based delete. The two
copies previously shared this defect and had DRIFTED IN SEVERITY -- the script
`exit 1`-ed a whole cleanup run on the benign 422 (#302) while this file merely
printed a spurious error. Keep them consistent; if one changes, change both.

---

## Step 6 -- Prune stale remote refs

```bash
git fetch --prune
```

---

## Step 7 -- Verify linked issues are closed

If `issues` from Step 1 is non-empty, check each one:

```bash
for n in $issues; do
  gh issue view "$n" --json number,state,title --jq '"#\(.number) [\(.state)] \(.title)"'
done
```

For each issue:
- **CLOSED** -- note it as auto-closed by the merge. No action needed.
- **OPEN** -- warn: "Issue #N is still open. Close it manually or check if it needs a `Closes #N` in the PR body."

If `issues` is empty, note: "No closing issues referenced in this PR."

---

## Step 8 -- Cut the release (if this merge bumped the version)

A version-bumping merge ships a new version, so the matching `vX.Y.Z` tag and GitHub Release must be cut NOW, as part of this cleanup, not deferred. Deferring it is how tags drift behind shipped versions (one un-cut release silently becomes several).

Detect whether this merge changed a version-defining file. This is repo-agnostic: key off a version file changing in the merge, NOT a hardcoded path. Adapt the globs to how the target repo versions (this repo uses a `SKILL.md` `**Version**` line locked to `plugin.json`; others use `package.json`, `Cargo.toml`, `pyproject.toml`, etc.):

The name match is only a CANDIDATE filter. `(^|/)` anchors to a path segment, not to a
directory, so `SKILL.md` matches ANY `SKILL.md` anywhere in any repo -- and most of them
carry no version at all (measured on a stillwater docs-only merge, whose only `SKILL.md` is
`.claude/skills/helper-scripts/SKILL.md`). Confirm each candidate actually DEFINES a
version before treating the merge as a release:

```bash
version_changed=""
while IFS= read -r f; do
  [ -n "$f" ] || continue
  # READ THE FILE AT THE MERGE COMMIT, not from the working tree: this step runs after
  # main has already moved, and the path may not exist at HEAD (or may differ).
  if git show "$sha:$f" 2>/dev/null \
      | grep -qE '^\*\*Version[[:space:]]|"version"[[:space:]]*:|^version[[:space:]]*='; then
    version_changed="$version_changed$f"$'\n'
  fi
done <<EOF
$(git show "$sha" --format= --name-only \
  | grep -iE '(^|/)(plugin\.json|package\.json|Cargo\.toml|pyproject\.toml|SKILL\.md)$' || true)
EOF
```

Adapt the version-line pattern to how the target repo declares a version; the three
alternatives above cover the `**Version X.Y.Z**` line, JSON manifests, and TOML manifests.

If `version_changed` is empty, SKIP this step and continue to the summary. That covers BOTH
"no version file changed" and "a candidate matched by name but declares no version" -- the
second is not a failure and needs no report. Do NOT reach for a sibling-`plugin.json` test
instead: it hard-codes this repo's packaging into a step whose contract is repo-agnostic, and
it would silently MISS a repo that versions a `SKILL.md` without being a plugin. A false
negative here costs a delayed tag; catching a real release is the whole point of the step.

Otherwise, read the shipped version and compare it to the latest release tag:

```bash
# current_ver: read from whichever file the repo treats as the source of truth
# (e.g. the SKILL.md "**Version X.Y.Z**" line, or `jq -r .version plugin.json`).
latest_tag=$(git tag --list 'v*' | sort -V | tail -1)
```

- If a tag for the current version ALREADY exists, nothing to cut (already released).
- If the current version is AHEAD of `latest_tag` with no matching tag, CUT THE RELEASE NOW:
  - Prefer the `/push-release` skill for the current HEAD version - it crafts house-style notes plus the tag and GitHub Release. Do NOT duplicate its note logic here.
  - If you are backfilling an already-merged version (HEAD has moved on), tag the annotated `vX.Y.Z` at this merge commit `$sha` and `gh release create vX.Y.Z` there, with house-style notes (intro, Highlights, themed prose, issue links, Compare/Install footer) - never a bare `--generate-notes` dump.

Never end cleanup with a version-bumping merge left un-released.

---

## Step 8b -- Disk-pressure advisory (df-only; never cleans)

Cleanup is a natural moment to surface disk pressure, but a merge must NEVER trigger a cache
wipe (build caches are machine-global; wiping one mid-build in a sibling worktree/CI job corrupts
that build). So this step is a single cheap `df` check that prints at most one advisory line and
cleans nothing. Reclaiming is on-demand via `/reclaim-cache`.

```bash
if [ -f scripts/cache-reclaim.sh ]; then PL=scripts/cache-reclaim.sh
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "${CLAUDE_PLUGIN_ROOT}/scripts/cache-reclaim.sh" ]; then PL="${CLAUDE_PLUGIN_ROOT}/scripts/cache-reclaim.sh"
else PL=""; fi
[ -n "$PL" ] && bash "$PL" --nudge || true
```

`--nudge` reads `df` only (no `du` scan, no clean), prints "Disk N% full - run /reclaim-cache ..."
when the home volume is >= 90% full (override `RECLAIM_NUDGE_PCT`), and is silent otherwise. It
always exits 0, so it never affects cleanup. If the line appears, surface it to the user and
suggest `/reclaim-cache`; do not reclaim anything here.

---

## Step 8c -- Open-PR staleness sweep (advisory; never blocks)

This merge just advanced the base branch, which silently left every OTHER open PR on that base
BEHIND. Surface that now, while the merge is fresh, rather than discovering it at the next merge
gate. The sweep EXCLUDES the PR that just merged.

```bash
if [ -f scripts/open-pr-staleness-sweep.sh ]; then SW=scripts/open-pr-staleness-sweep.sh
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "${CLAUDE_PLUGIN_ROOT}/scripts/open-pr-staleness-sweep.sh" ]; then SW="${CLAUDE_PLUGIN_ROOT}/scripts/open-pr-staleness-sweep.sh"
else SW=""; fi
[ -n "$SW" ] && bash "$SW" "$pr_number" "$repo" || true
```

The sweep is ADVISORY and FAIL-OPEN by contract: it exits 0 on every operational path (including a
read failure), so it can never block cleanup. Its routing is the safety hinge:

- **behind + NO review activity yet** -> it refreshes the PR itself with a plain
  `gh pr update-branch <n>` (DEFAULT merge-commit mode, additive). It NEVER passes `--rebase`.
- **behind + REVIEWED** (any submitted review, review thread, or comment) -> SURFACED ONLY, never
  touched. A HEAD-moving commit dismisses a bot's prior approval and disturbs the incremental-review
  delta, and a rewrite would orphan every cited fix SHA. The LEAD decides when that ref moves.
- **review state indeterminate / unreadable** -> treated as REVIEWED and surfaced. It fails toward
  surfacing, never toward acting.
- **cross-repository (fork) PR** -> SKIPPED and surfaced, never measured and never mutated: its
  `headRefName` names a branch in the FORK, so `origin/<head>` is at best absent and at worst an
  unrelated same-named origin branch. An unreadable `isCrossRepository` is treated AS cross-repo.
- **head fetch failed** -> reported as undetermined, never measured against a stale local ref.
- **the open-PR list hits the `--limit` cap** -> the report says so explicitly; it never claims a
  clean "nothing to do" over a set that may be truncated.
- **`gh pr update-branch` not permitted** (the `Bash(gh pr update-branch *)` allow-list entry is the
  maintainer's to grant) -> it degrades to REPORT-ONLY and still prints the behind list.

Surface any `NEEDS THE LEAD` lines to the user; do not act on them here.

---

## Step 9 -- Summary

Report:
- PR: #$pr_number (`$sha`)
- main: updated to `$(git rev-parse --short HEAD)`
- Worktree: removed `$wt_path` / none found
- Local branch: deleted `$branch` / not found
- Remote branch: deleted / already gone
- Issues: list each with state, or "none referenced"
- Any warnings
