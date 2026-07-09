<!-- CHECKPOINT HEAD (#222): paste at the TOP of the session-state doc. Hold ONLY
     non-derivable INTENT + pointers - resume reads THIS, never a 12-13K-token blob.
     RECONSTRUCT the reboot-durable derivables on demand (they live authoritatively
     elsewhere, so mirroring them here just re-bloats); MIRROR only judgment artifacts. -->

## >>> CHECKPOINT <DATE TIME PDT> (read FIRST) <<<
**Main HEAD = `<sha>`.** Team = `<team>`. Lead = orchestrator (delegates; owns push/PR/merge with explicit human go).

### STATUS (one banner line)
- <where the session is right now, in one line>

### NEXT 2-3 ACTIONS (non-derivable intent)
- <the immediate next step(s) a fresh resume should DO first>

### WAVE / PLAN (non-derivable)
- <the current wave map / decomposition / resolved design decisions a resume cannot re-derive from git or GitHub>

### JUDGMENT ARTIFACTS (mirror rule KEPT - #222 carve-out B1)
- <adversarial-review / triage FINDING SETS: mirror them here (or a durable per-PR store). They are NOT reboot-durable and NOT SHA-invalidated, so losing a mid-loop finding set on reboot is REAL DATA LOSS. Deterministic SHA-named receipts do NOT belong here - they live in /tmp/<team>/pr-<N>/ and reconstruct.>

### POINTERS (not copies)
- Design/plan docs: <paths>. Feedback inbox: `~/.claude/orchestrate-feedback/inbox/`.
- Per-PR receipt dirs: `/tmp/<team>/pr-<N>/` (deterministic receipts; self-pruning; swept at post-merge-cleanup).
- Encryption-key FORM (not the value) + any non-derivable infra note.

### CONTROL POINTS (do not forget)
- No bot merges; merge is human. Per-PR human go at stack time. Lead vets every PR-body `#N` vs `gh issue view`. Bots background pr-watch. Implementers PR-blind.
## >>> end checkpoint <<<

<!-- RECONSTRUCT ON DEMAND (do NOT mirror these - gh/git are the source of truth, so a
     copy here only goes stale and re-bloats). Delegate the raw dump to a DIGEST SUBAGENT
     (#227) so it never enters the lead's window:
       - In-flight / open PRs:  gh pr list --head <branch>   (or --author @me)
       - Worktrees:             git worktree list
       - Bot / queue / open-task state: the live team + TaskList + gh pr checks
-->
