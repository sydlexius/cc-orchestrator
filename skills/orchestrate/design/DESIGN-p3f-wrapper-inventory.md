# P3-F: deterministic gh-* wrapper inventory (issue #24)

Replaces the broad `Bash(gh api *)` allow-listing with a small set of deterministic wrapper
scripts, so a bot's (and the lead's) gh-api access is least-privilege and cannot reach the
merge-by-API surface. Canonical source = this repo root (alongside `orchestrate-guard.sh`);
deployed by symlink into `~/.claude/scripts/` (already covered by the
`Bash(~/.claude/scripts/*.sh *)` allow-rule, so no new allow-list entry is needed).

## Scope boundary

This PR ships the WRAPPERS + the allow-list narrowing only. The guard's marker-gated
merge-by-API hard-deny (`is_merge_api()`) RETIREMENT is a SEPARATE security-floor follow-up
requiring full Phase-1 rigor (TDD harness + engage-ralph-loop) - per the maintainer's #24
steer it is NOT folded here. Until then the guard deny stays as a belt-and-suspenders backstop.

## `gh api` call inventory (what the lead + bots legitimately need)

| Operation | Verb | Covered by |
|---|---|---|
| review / comment reads | GET | `gh-api-get.sh` |
| check-runs read | GET | `gh-api-get.sh` |
| code-scanning reads (alerts list/get) | GET | `gh-api-get.sh` |
| merge-STATUS check (read) | GET | `gh-api-get.sh` |
| any other read endpoint | GET | `gh-api-get.sh` |
| CodeQL alert dismiss | PATCH | `gh-codeql-dismiss.sh` (dedicated) |
| resolveReviewThread | GraphQL mutation | `gh-resolve-thread.sh` (dedicated) |
| PR/issue comment (post / reply / inline / trigger-CR) | POST | `gh-comment.sh` (dedicated) |
| CodeQL alert autofix request | POST | `gh-codeql-autofix.sh` (dedicated) |
| branch ref delete (post-merge cleanup) | DELETE | `gh-delete-branch.sh` (dedicated) |

Cross-reference: `templates/pr-triage-charter.md` (GET-only endpoints: reviews, comments,
check-runs, code-scanning). pr-triage remains READ-ONLY by charter and uses only `gh-api-get.sh`.

## Wrapper contracts

- **`gh-api-get.sh <gh-api-args...>`** - refuses ANY mutation flag (`-X`/`--method`,
  `-f`/`--raw-field`, `-F`/`--field`, `--input`, incl. attached `-X...`/`-f...`/`-F...` and
  `--flag=value` forms), exits 2 with a clear message; otherwise `exec gh api "$@"`. GETs that
  need params pass them in the URL path (`"repos/o/r/x?k=v"`), since any field flag implies a
  write in `gh api`.
- **`gh-codeql-dismiss.sh <alert-number> [reason] [comment]`** - validates `alert-number` is
  numeric and `reason` is one of GitHub's enum (`false positive` | `won't fix` | `used in
  tests`; default `won't fix`); resolves the repo from `$GITHUB_REPOSITORY` or `gh repo view`;
  then `gh api -X PATCH repos/<repo>/code-scanning/alerts/<n>` with the fixed dismiss payload.
- **`gh-resolve-thread.sh <thread-id>`** - validates `thread-id` is a GitHub node id; then a
  FIXED `resolveReviewThread` GraphQL mutation.
- **`gh-comment.sh <post|reply|inline> ...`** - dedicated PR/issue COMMENT poster.
  `post <pr> <body>` -> `POST issues/<pr>/comments`; `reply <pr> <comment-id> <body>` ->
  `POST pulls/<pr>/comments/<comment-id>/replies`; `inline <pr> --file <p> --line <n> [--side
  RIGHT|LEFT] <body>` -> `POST pulls/<pr>/comments` with body+commit_id(HEAD)+path+line+side.
  (A dead `trigger-cr` subcommand that posted `@coderabbitai review` was REMOVED in #192 --
  the exclusive-purview rule forbids an agent CR-trigger; a maintainer-authorized trigger uses
  the generic `post`.) Validates pr/comment-id/line numeric, side in {RIGHT,LEFT}; the body/path are DATA (-f/-F
  fields), never interpolated into the endpoint or the (fixed POST) method; refuses any
  caller-supplied -X/--method or unknown flag. Resolves repo + HEAD sha internally.
- **`gh-codeql-autofix.sh <alert-number> [repo]`** - validates `alert-number` is numeric; then
  `gh api -X POST repos/<repo>/code-scanning/alerts/<n>/autofix` (verb fixed POST). Refuses a
  non-numeric alert.
- **`gh-delete-branch.sh <branch-name> [repo]`** - validates `branch-name` against
  `^[A-Za-z0-9._/-]+$` (rejects spaces, metachars, leading `-`, newlines, `..` segments),
  URL-encodes it, then `gh api -X DELETE repos/<repo>/git/refs/heads/<encoded-branch>` (verb
  fixed DELETE). The endpoint is literal apart from the validated/encoded branch under the
  `git/refs/heads/` prefix, so a name resembling `pulls/1/merge` still targets a heads ref.

## Construction guarantee (the security property)

No wrapper can perform a merge: `gh-api-get.sh` refuses every mutation flag (a merge-by-API
needs `-X PUT`/`-X POST`); `gh-codeql-dismiss.sh` builds a `code-scanning/alerts/<numeric>`
endpoint only; `gh-resolve-thread.sh` runs a fixed `resolveReviewThread` mutation only;
`gh-comment.sh` pins each endpoint to `issues|pulls/<numeric>/comments...` with the body as
data and a fixed POST; `gh-codeql-autofix.sh` builds a `code-scanning/alerts/<numeric>/autofix`
endpoint with a fixed POST; `gh-delete-branch.sh` builds a `git/refs/heads/<validated-branch>`
endpoint with a fixed DELETE. None accepts a caller-supplied -X/--method, so no input can
reach a `/merge`, `--admin`, or arbitrary endpoint. With raw `gh api` removed from the
allow-list, the bot has no path to merge-by-API through gh-api.

## Allow-list change (applied externally by the maintainer)

Remove `Bash(gh api *)` from `~/.claude/settings.json`'s `permissions.allow`. The six
wrappers (`gh-api-get.sh`, `gh-codeql-dismiss.sh`, `gh-resolve-thread.sh`, `gh-comment.sh`,
`gh-codeql-autofix.sh`, `gh-delete-branch.sh`) are already permitted by the existing
`Bash(~/.claude/scripts/*.sh *)` rule once symlinked. `required-permissions.md` is updated to
document this (doctor reads it; permissions stay the maintainer's to grant).

## Deployment

`ln -s` each wrapper from this repo root into `~/.claude/scripts/` alongside the other runtime
scripts (or it rides the #30 plugin bundling). The wrappers call `gh api` internally; that
inner call is a subprocess of an already-allow-listed script, not a separately-gated Bash tool
call, so no `gh api` allow-rule is needed.
