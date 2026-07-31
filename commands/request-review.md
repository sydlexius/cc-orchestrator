---
description: "Queue a CodeRabbit review request for a gated PR - the loop posts it later; this NEVER posts"
argument-hint: "<PR#> [owner/repo]"
allowed-tools: ["Bash"]
---

# Request a CodeRabbit review (queue it; never post it)

File a review request for a PR that has PASSED `/orchestrate:prep-pr`. The request goes into the
elmer queue, and a separate single writer (`elmer-tick.sh`, running in its own `/elmer-loop`
window) posts it when CodeRabbit's quota window allows.

**This command posts NOTHING.** That is its defining property, not a limitation. Triggering a CR
review is the maintainer's exclusive purview, and the one mechanized carve-out lives in
`elmer-tick.sh` alone. This surface has no posting verb at all, so a TL cannot trigger a review by
accident, by misreading a prompt, or by an agent going off-script - the capability is not present
to misuse.

**Arguments:** $ARGUMENTS

---

## Step 1 -- The receipt is the gate (run prep-pr FIRST)

Enqueue REFUSES unless a `gate-receipt/v1` exists, validates, says `result=pass`, names
`gate-runner` as its producer, AND carries a `commit_sha` equal to the PR's CURRENT head.

That last check is the one that makes this mechanical rather than honor-system: a TL who gated and
then pushed twice holds a receipt whose commit no longer matches HEAD, so the request is refused
with a pointer back to `/prep-pr`. There is no way to queue a review for un-gated code short of
forging a receipt, which is a different threat model (this is a guardrail against an honest TL on
the obvious path, exactly like the deterministic floor - not a sandbox).

`/orchestrate:prep-pr` writes the receipt as a byproduct of the real gate run; it never changes the
gate's verdict. If you have not run prep-pr on the current head, run it before this command.

The receipt lives under the worktree's git dir. Resolve that with `git rev-parse --git-dir`, NEVER
a literal `.git/` - in a worktree `.git` is a FILE pointing into `.git/worktrees/<name>`, so a
literal path is unwritable exactly where this workflow normally runs.

---

## Step 2 -- Enqueue

Resolve the helper in the SAME Bash call that uses it - each tool call is a fresh shell, so the
variable does not survive across calls. Substitute the real PR number:

```bash
EQ=""
if [ -f scripts/elmer-enqueue.sh ]; then EQ=scripts/elmer-enqueue.sh
elif [ -f "$HOME/.claude/scripts/elmer-enqueue.sh" ]; then EQ="$HOME/.claude/scripts/elmer-enqueue.sh"
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "${CLAUDE_PLUGIN_ROOT}/scripts/elmer-enqueue.sh" ]; then
  EQ="${CLAUDE_PLUGIN_ROOT}/scripts/elmer-enqueue.sh"
fi
[ -n "$EQ" ] || { echo "elmer-enqueue.sh not found (repo-local, deployed, or plugin)" >&2; exit 2; }

bash "$EQ" <PR#> --receipt "$(git rev-parse --git-dir)/orchestrate/gate-receipt.json"
```

The deployed `~/.claude/scripts/` path is checked before the plugin path deliberately: that stable
location is what keeps the whole loop inside the existing
`Bash(bash ~/.claude/scripts/*.sh *)` wrapper grant, so nothing here needs a broad `gh` grant or
raises a permission prompt.

### Reading the exit code

| Exit | Meaning | What to do |
|---|---|---|
| 0 | ENQUEUED | Done. The loop posts it when the quota window opens. |
| 1 | REFUSED | The gate said no. The message names which check failed - fix that, do not retry blind. |
| 2 | SETUP ERROR | Bad args, unresolvable repo, or a `gh` read failure. Never treated as "fine, enqueue it". |

A refusal never leaves a partial entry behind and is never silent.

**The common refusal is a stale receipt** ("receipt commit X does not match PR head Y"): you gated,
then pushed. Re-run `/orchestrate:prep-pr` on the new head and enqueue again. Do NOT hand-edit the
receipt or the queue entry - both are checked, and editing them is the one path that would put
un-gated code in front of a reviewer.

**Already queued / already triggered** is also a refusal, by design: a PR+SHA present in `inbox/`
or `drained/` is never queued twice. Asking GitHub "has a review happened yet" would be racy - CR
takes minutes to post - so the drain record is the authority.

---

## Step 3 -- Confirm what is queued (read-only)

```bash
ls -1 "${ELMER_HOME:-$HOME/.claude/elmer}/inbox" 2>/dev/null || echo "(queue empty)"
```

Entries are named `<repo-slug>--<pr>--<sha12>.json`. Nothing here posts or mutates.

---

## Notes

**`full review` is refused, always.** Both this surface and the tick reject it regardless of what a
caller asks: a full review re-surfaces resolved threads and owes a human decision. Only the
incremental form is ever queued or posted.

**Only the maintainer's own gated pipeline feeds the queue.** The tick never invents a target - it
posts only for entries this command admitted.

**Scope.** Reads a PR via `gh` and writes ONE local queue entry. No posting, no `gh` mutation, no
git mutation, no allow-list broadening, no floor change. The one thing it can do to the outside
world is read.

**Where the posting happens.** `/elmer-loop`, in its own window, running `elmer-tick.sh` under the
CLAUDE.md carve-out. If no loop window is running, requests simply accumulate in `inbox/` until one
is - which is a safe failure, not a lost request.
