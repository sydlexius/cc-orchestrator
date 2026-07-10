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

### Finding channel helper (`scripts/finding_channel.py`, #230)

The channel's two slices are managed by `finding_channel.py`, the deterministic
guard over the review<->fix loop (design #6). It never mutates the REMOTE and
never changes the repo's working tree, index, or history; its only network op is
`git fetch`, read-only to the remote (it may update the local object DB +
remote-tracking refs, nothing else). It never touches the allow-list. Subcommands:

- `validate <fix-list|reply-slice> <file.json>` -- schema validate PLUS channel
  invariants a bare schema cannot express: `round >= 1`; an `addressed` finding
  carries a `fix_sha`; finding ids are unique; a `fix` reply carries `reply_text`.
- `liveness <file> --deadline-secs N` -- an mtime SIGNAL (`fresh|slow|stalled|dead|missing`)
  so the lead tells a slow writer from a dead one; exit 0 for any present file,
  exit 1 only for `missing`.
- `guard-reply --repo P --branch B --finding ID --sha SHA [--no-fetch]` -- THE
  pre-reply guardrail: exit 0 only if `SHA` is an ancestor of `origin/B` (PUSHED)
  AND bound to the finding by a `Finding-Id: ID` commit trailer (ancestry alone is
  insufficient). A reachable-but-branch-absent remote is `not pushed` (exit 1); an
  UNREACHABLE remote is `cannot prove pushed` (exit 2, safe-block) -- never a stale
  false-pass.
- `guard-slice --repo P --branch B --fix-list F <reply-slice.json> [--no-fetch]` --
  batch guard-reply over every `fix` disposition, looking up each finding's `fix_sha`
  in the paired fix-list. The LEAD runs this before actuating a reply-slice.

The `Finding-Id:` trailer is authored by the PR-blind implementer (it has the ids
from the fix-list, learns nothing about the PR). Exit convention: 0 ok/pass, 1 a
check failed, 2 usage / IO error.
