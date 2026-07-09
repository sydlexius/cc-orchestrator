# Orchestrate artifact schema registry (#225)

Versioned, single source-of-truth schemas for the structured artifacts one
orchestrate agent WRITES and another READS. Without a shared schema, producers
and consumers drift silently -- the schema-drift risk flagged in
`design/DESIGN-tl-context-minimization.md` (Cross-cutting infrastructure).

**Authoritative machine schema:** `scripts/orchestrate_schemas.py` (the `SCHEMAS`
registry + `validate()`). This document is the human-facing companion and MUST
stay in sync with that module. Prerequisite for the report-by-exception header
(design #3), the gate receipt (#229 / #4), and the finding channel (#230 / #6).

**How to validate.** Python producers/consumers `import orchestrate_schemas` and
call `validate(schema_name, obj) -> [errors]` (empty list = conforms). Shell
producers call the CLI:

```sh
python3 scripts/orchestrate_schemas.py --validate <schema-name> <file.json>
# exit 0 = valid; exit 1 = invalid (errors on stderr); exit 2 = usage / unreadable file
```

Extra/unknown keys are ALLOWED (forward-compat: report-by-exception extends the
receipt block). Every listed field below is required unless marked optional.

**Tradeoff of the open-world policy:** because extra keys pass, a *misspelled
optional* field (e.g. `fixsha` for `fix_sha`) validates silently rather than
erroring. This is the deliberate cost of supporting the report-by-exception
extension; required fields and every declared field's type/enum/pattern are still
strictly enforced, so a producer that drops or mistypes a REQUIRED field is caught.

---

## `gate-receipt/v1`

Written by `/prep-pr` as a byproduct of the real gate run (#229). A *hint*, never
proof: a consumer trusts it only when, run IN the artifact's worktree,
`result == "pass"` AND `commit_sha == $(git rev-parse HEAD)` AND a live
clean-worktree check passes. See design #4 for the consumer contract.

| field | type | notes |
|-------|------|-------|
| `schema` | string | const `"gate-receipt/v1"` |
| `commit_sha` | string | full 40-char hex |
| `tree_sha` | string | full 40-char hex (`git rev-parse HEAD^{tree}`) |
| `worktree` | string | absolute path of the producing worktree |
| `result` | string | `pass` \| `fail` |
| `steps` | list | each `{name: string, result: pass\|fail\|skip}` |
| `producer` | string | e.g. `prep-pr` |

```json
{ "schema": "gate-receipt/v1", "commit_sha": "…40hex…", "tree_sha": "…40hex…",
  "worktree": "/abs/path/wt", "result": "pass",
  "steps": [{"name": "shellcheck", "result": "pass"}], "producer": "prep-pr" }
```

## `finding-fix-list/v1`

The PR-BLIND slice of the finding channel (#230). The implementer reads/writes
it; it carries NO thread ids and NO bot-reply prose, preserving "implementers
never see the PR/CR."

| field | type | notes |
|-------|------|-------|
| `schema` | string | const `"finding-fix-list/v1"` |
| `round` | int | serialization token (single-writer-per-round) |
| `findings` | list | each `{id, severity, detail, status, fix_sha}` |

Finding: `id` string; `severity` `critical\|high\|medium\|low\|nit`; `detail`
string; `status` `open\|addressed`; `fix_sha` string or null (optional).

```json
{ "schema": "finding-fix-list/v1", "round": 1,
  "findings": [{"id": "F1", "severity": "high", "detail": "…",
                "status": "addressed", "fix_sha": "abc123…"}] }
```

## `finding-reply-slice/v1`

The LEAD-ONLY slice of the finding channel (#230): a mapping keyed by finding id.
The implementer never sees it.

| field | type | notes |
|-------|------|-------|
| `schema` | string | const `"finding-reply-slice/v1"` |
| `replies` | object | `finding_id -> {thread_id, disposition, reply_text}` |

Reply: `thread_id` string; `disposition` `merge-safe\|rebut\|fix`; `reply_text` string.

```json
{ "schema": "finding-reply-slice/v1",
  "replies": {"F1": {"thread_id": "PRRT_x", "disposition": "fix",
                     "reply_text": "fixed in abc123"}} }
```
