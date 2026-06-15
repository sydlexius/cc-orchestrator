<!-- Paste at the TOP of the session-state/plan doc. Keep it CURRENT as PRs ship and
     decisions land. Mirror any /tmp-only artifact (triage findings) here - /tmp clears on reboot. -->

## >>> CHECKPOINT <DATE TIME PDT> (read FIRST) <<<
**Main HEAD = `<sha>`.** Team = `<team>`. Lead = orchestrator (delegates; owns push/PR/merge with explicit human go).

### SHIPPED
- <PR #, branch, squash sha, what it closed, state (merged/open), CR/Greptile state>

### IN FLIGHT / OPEN PRS
- <PR #, branch@sha, CR state (approved / CHANGES_REQUESTED), the 2-3 findings inline (durable - do not rely on /tmp), NEXT action: handle-review? merge after glance?>

### DEFERRED / CARRYOVER (next session)
- <implementer cluster: branch@sha, worktree path (intact?), DONE vs REMAINING items scoped, resolved design decisions, then rebase+prep+review+PR>

### FOLLOW-UP ISSUES filed
- <#n: one-line each, milestone>

### BOTS
- pr-shipper: <up/down>, stack <path> = <state>, brief <path>
- pr-triage: <up/down>, charter, output <dir>
- implementers/adversarial: <up/down per cluster>

### OPEN PENDING TASKS (team list)
- <ids + one-liners>

### INFRA
- ports, encryption-key form (VALUE), go cache state, settings/permissions notes.

### CONTROL POINTS (do not forget)
- No bot merges; merge is human. Per-PR human go at stack time. Lead vets every PR-body `#N` vs `gh issue view`. Bots background pr-watch. Implementers PR-blind.
## >>> end checkpoint <<<
