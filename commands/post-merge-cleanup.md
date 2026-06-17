---
description: "Post-merge cleanup: update main, remove worktree, delete branches, prune refs, verify linked issues"
argument-hint: "<PR-number> (e.g. 32)"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Edit"]
---

# Post-Merge Cleanup

Run after a PR is merged to clean up the working environment.

**PR number:** $ARGUMENTS

If no PR number is provided, stop and ask: "Which PR number was just merged?"

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

### Clear stale linter caches after worktree removal

golangci-lint caches analysis results keyed by absolute file path. After a
worktree is removed, those paths no longer exist, but the cache still
references them: a later `golangci-lint run` in a sibling worktree can fail
with phantom findings (it can't read the deleted files to see their `//nolint`
directives, so it reports the raw errors). When a worktree was removed above
and this is a Go repo, clear the cache so the next gate run is clean:

```bash
command -v golangci-lint >/dev/null 2>&1 && golangci-lint cache clean || true
```

This is a no-op when golangci-lint is absent or no worktree was removed.

---

## Step 4 -- Delete local branch

```bash
git branch -d "$branch" 2>/dev/null && echo "deleted local $branch" || echo "local branch $branch not found or already deleted"
```

Do not use `-D` (force). If the branch is unmerged, stop and warn.

---

## Step 5 -- Delete remote branch

```bash
encoded_branch=$(printf '%s' "$branch" | jq -sRr @uri)
gh api "repos/$repo/git/refs/heads/$encoded_branch" -X DELETE 2>/dev/null \
  && echo "deleted remote $branch" \
  || echo "remote branch $branch already deleted (404)"
```

404 is expected if GitHub deleted it automatically on merge (`--delete-branch` flag). Note and continue.

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

## Step 8 -- Summary

Report:
- PR: #$pr_number (`$sha`)
- main: updated to `$(git rev-parse --short HEAD)`
- Worktree: removed `$wt_path` / none found
- Local branch: deleted `$branch` / not found
- Remote branch: deleted / already gone
- Issues: list each with state, or "none referenced"
- Any warnings
