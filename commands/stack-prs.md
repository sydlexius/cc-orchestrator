---
description: "Link gated branches or PRs into a GitHub PR stack (fallback only): preflight every slice, confirm, gh stack link, verify"
argument-hint: "<slice> [<slice> ...] [--base <branch>] [--open]"
allowed-tools: ["Bash", "Read", "Write"]
---

# Stack PRs

The sanctioned way to build a dependent PR stack. A stack is the FALLBACK, never the default: see
the SKILL.md bullet "STACKED PRS ARE TOLERATED, NOT PREFERRED". This command preflights every
slice, shows the exact `gh stack link` it will run, runs it only on an explicit yes, verifies the
result, and drafts the PR bodies. It never merges, never posts a review trigger, and never runs
`gh stack submit/sync/rebase/push/merge`.

A slice is a worktree path (an existing directory) or a PR number (all digits), listed BOTTOM TO
TOP. `--base <branch>` names the trunk the bottom PR targets (default: the branch `origin/HEAD`
points at). `--open` marks the PRs ready for review instead of leaving them drafts.

**Arguments:** $ARGUMENTS

---

## Step 0 -- Validate arguments and check policy

Split `$ARGUMENTS` on whitespace. Accept ONLY these words, matched exactly and case-sensitively:

- `--open` (at most once).
- `--base` followed by ONE branch name (at most once). The name must be a plain branch: letters,
  digits, `.`, `_`, `-`, `/` only, no leading `-`, no `..`, not ending in `/` or `.lock`.
- A slice: an all-digits word (a PR number), or a word naming an EXISTING directory. Check each
  candidate directory with the Step 0 block below, never by pasting it into another command.

Anything else (`--base=x`, `--force`, `-o`, a quoted or `;`-bearing word, a word that is neither an
existing directory nor all digits) is rejected: name the word, show the argument hint, and STOP.
Fewer than two slices: STOP (a stack of one is an ordinary PR; use `/prep-pr`).

Build `SLICES` (the validated slices, in the order given, each single-quoted), `BASEFLAG`
(`--base '<branch>'` when given, else empty) and `OPENFLAG` (`--open` when given, else empty)
yourself. Never paste unvalidated user text into a
command. Verify the directories and the base name:

```bash
for s in <SLICES>; do
  case "$s" in
    ''|*[!0-9]*) if [ -d "$s" ] && git -C "$s" rev-parse --git-dir >/dev/null 2>&1; then echo "slice ok (worktree): $s"; else echo "slice REJECTED: $s"; fi ;;
    *) echo "slice ok (PR): #$s" ;;
  esac
done
b='<BASE or empty>'; [ -z "$b" ] || { git check-ref-format "refs/heads/$b" && echo "base ok: $b" || echo "base REJECTED: $b"; }
```

Any `REJECTED`: STOP.

Then state the policy check in two lines, and STOP if either fails:

1. **Why a stack.** The work exceeds the `/prep-pr` Step 1b size gate (800 LOC / 10 files) AND
   cannot split into independent PRs (an upper slice cannot compile or test without the lower
   one), and the lead agreed (the maintainer, in a solo session). The designated #501 controlled
   test is the one exception to the size condition.
2. **Every slice already passed `/prep-pr`, stopped before its push.** Step 1 checks the receipts
   mechanically; this line is the lead's statement that the gate was the real one.

---

## Step 1 -- Preflight every slice

`stack-preflight.sh` is READ-ONLY (see its header). It is not deployed to `~/.claude/scripts/`,
so there is no deployed leg; the leg rules are the "Helper exec paths" section of `prep-pr.md`.

```bash
if [ -f scripts/stack-preflight.sh ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/stack-preflight.sh' ]; then leg=plugin
else leg=none; fi
pf_rc=3
[ "$leg" = repo ]   && { bash scripts/stack-preflight.sh <BASEFLAG> <SLICES>; pf_rc=$?; }
[ "$leg" = plugin ] && { bash '${CLAUDE_PLUGIN_ROOT}/scripts/stack-preflight.sh' <BASEFLAG> <SLICES>; pf_rc=$?; }
[ "$leg" = none ]   && echo "stack-preflight: NOT RUN -- helper not found (load via /orchestrate:stack-prs, or update the plugin)"
echo "pf_rc=$pf_rc leg=$leg"
(exit "$pf_rc")
```

Show every line. `pf_rc=0` -> continue (report each `WARN`/`INFO`). Anything else -> STOP:

- `1`: a definitive failure. Fix the named slice (commit or stash, re-run `/prep-pr`, merge the
  trunk or the lower slice in, retarget a PR), then re-run this command.
- `2`: undeterminable (a gh read failure, an unreadable receipt, freshness unknown) or a usage
  error. Never link on doubt.
- `3` or a hook-denied block: the gate did not run (see "Hook-denied gate command" in `prep-pr.md`).

---

## Step 2 -- Show the plan, ask once

Build `LINKARGS`: for each slice in order, the BRANCH name the preflight printed in brackets
(worktree slice) or the PR number (PR slice). Show the command exactly:

```text
gh stack link [--base <branch>] [--open] <LINKARGS>
```

and say, briefly:

- `gh stack link` PUSHES every branch argument itself, bypassing `safe-push.sh` (receipt,
  freshness, one-branch checks). Step 1 just re-ran those checks; the lead's yes authorizes the
  bypass for this stack.
- It reuses an open PR for a branch that has one and CREATES the missing ones with the base
  chained to the slice below. If the PRs already belong to a stack, it appends.
- **Draft state (verified from `gh stack link --help`).** link's help does NOT state whether new
  PRs are drafts (unlike `gh stack submit --help`, which says `--auto` creates drafts). So
  Step 3 READS each PR's draft state rather than assuming. `--open` "marks new AND EXISTING PRs as
  ready for review": with `--open`, a draft PR you meant to keep draft is un-drafted too.

Then ask exactly ONE question: "Run this `gh stack link`?" Proceed ONLY on an explicit yes in this
session. Anything else: STOP, nothing pushed.

---

## Step 3 -- Link, then verify

Re-run the Step 1 block first if anything moved since (a commit, a fetch, a minute of doubt);
`pf_rc` must still be 0. Then:

```bash
gh stack link <BASEFLAG> <OPENFLAG> <LINKARGS>; echo "link_rc=$?"
```

A nonzero `link_rc`: report its output and STOP. Do not retry blindly: a partial run may have
pushed some branches and created some PRs. Reconcile first (`gh pr list --head <branch>` per
slice, `git ls-remote origin <branch>`).

Verify, per slice (`<n>` = its PR number, `<branch>` its branch):

```bash
gh pr view <n> --json number,url,state,isDraft,baseRefName,headRefName,headRefOid
git ls-remote origin refs/heads/<branch>
git rev-parse refs/heads/<branch>    # worktree slices only
gh stack view --json 2>/dev/null || echo "gh stack view: no local tracking (expected after link)"
```

Required, else STOP and report the mismatch:

- every PR is OPEN; the bottom PR's `baseRefName` is the trunk; each higher PR's `baseRefName` is
  the branch of the slice below;
- the remote head (`ls-remote`) equals the local tip, and equals `headRefOid`;
- draft state is what was intended: without `--open`, any PR that came out ready is converted
  with `gh pr ready <n> --undo` (link's draft default is undocumented, so this is the handling).

Report the stack number (from link's output, or the GitHub stack UI link on any PR) and every PR
URL, bottom to top.

---

## Step 4 -- PR bodies

Every body stands on its own: neither review bot is documented to follow a "see #N" pointer.
For each PR, draft the body into a file (`/tmp/stack-prs-<n>.md`, via the Write tool, so trigger
words never appear on a Bash command line), mirroring `.github/pull_request_template.md`
(`prep-pr.md` Step 8b has the row mapping), with these additions in the Summary:

- `Stack: #<lower> <- #<this> <- #<upper>` naming the neighbor PR numbers (bottom first).
- Why a clean split was impossible (one or two sentences, the real reason).
- The cross-slice context ITSELF: what the lower slice provides that this one uses, or what the
  upper slice builds on top of this one.
- Closing keywords on the slice that completes the issue only; the others say `Part of #N`.

Run the advisory prose-lint on each file (the `prep-pr.md` Step 8b block, passing the file path
instead of piping, `--label "(pr-body #<n>)"`); it never blocks. Then apply:

```bash
gh pr edit <n> --body-file /tmp/stack-prs-<n>.md
```

If link generated the title, replace it with `gh pr edit <n> --title '<title>'` (a conventional
commit-style title, single-quoted, no trigger words; there is no title-file flag).

---

## Step 5 -- Next steps (print for the lead)

1. Watch each PR with `/pr-watch <n>`, one watch per PR, backgrounded.
2. CodeRabbit reviews the BOTTOM PR only, and only when the maintainer triggers it. No agent and no
   elmer queue entry ever posts a review trigger for any PR of a stack. (The #501 controlled test:
   the maintainer may trigger the upper PR, naming the related PR in the trigger text.)
3. Merge bottom-up, one PR at a time, through `/merge-pr` (its per-PR readiness oracle). Never
   `gh stack merge`: it merges the whole stack atomically and the floor does not gate it (#516).
4. After the lower PR merges, refresh the next PR with an ADDITIVE `gh pr update-branch <n>` (and
   confirm its base retargeted to the trunk). Never `gh stack sync`, `rebase` or `push` once any PR
   in the stack has a review: they force-push, which orphans cited fix SHAs and empties CR's
   incremental-review delta.
5. Fix rounds on a stack PR go through `/handle-review` on that PR's own worktree (safe-push path).

---

## Best practices

- **Split first.** Independent PRs off the trunk beat any stack. A stack needs both conditions in
  Step 0, and the lead's agreement.
- **Cut slices on disjoint files** where possible, so a fix on one slice rarely touches another.
- **Build the upper slice on the lower one locally before the first push.** Rebasing is free before
  any review and costly after; get the chain right while it is free.
- **Keep every slice independently green.** Each slice passes `/prep-pr` on its own branch.
- **Use `gh stack link`, not `init`/`add`/`submit`, with worktrees.** `init` checks branches out and
  fails when a worktree already holds one ("'b' is already used by worktree"), yet still records a
  half-made local stack. `link` needs no local tracking, takes branches or PR numbers, and appends
  to an existing stack when the first argument is a stack number.
- **Re-check immediately before link.** link pushes by itself; the receipt and freshness checks
  only hold for the tree they saw. Re-run Step 1 if anything moved.
- **Bodies are self-contained.** `submit --auto` and link can both leave generated titles and empty
  bodies; Step 4 replaces them, and each body names its neighbors.
- **Forbidden on a stack:** `gh stack merge` (#516); `gh stack sync/rebase/push` after any review;
  a hand-made `gh pr create --base <other-branch>` (not a GitHub stack; nothing links the PRs).
- **Record the outcome for #503** when the stack was a measured case (the #501 controlled test):
  what the review bots did with the stack context, and how many rounds each PR took.
