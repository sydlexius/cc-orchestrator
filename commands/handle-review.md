---
description: "Triage open bot PR review comments, fix everything in one pass, reply in batch, push once"
argument-hint: "[PR number -- defaults to current branch's PR]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Edit", "Write", "Agent", "Task"]
---

# Handle PR Review

Resolve all open bot review comments in a single pass. The invariant: **one push,
after all fixes are complete**. Never push per-comment.

This command targets bot reviewer comments (logins ending with `[bot]`). For human
reviewer comments, apply the same triage and fix discipline manually.

**Which reviewers to expect.** **Codoki**
(`codoki-pr-intelligence[bot]`) is **NOT IN SERVICE** (subscription lapsed
2026-07-12) -- it reviews nothing and is never triggered; never post `@codoki`.
With Codoki gone and CR auto-review OFF, the only bot that may still post
UNPROMPTED is **Greptile** (where installed) -- keep waiting for it (see below).
A Codoki thread on an older PR is a LEGACY finding: triage, reply and resolve it
like any other bot's. **CodeRabbit** is **opt-in / maintainer-allocated**:
org-wide CR auto-review is OFF, so a CR review exists only when the maintainer has
already triggered one. This command NEVER triggers a CR pass (see Step 1.25); a
missing CR review is a normal state, not a problem to fix.

**Codecov** (`codecov[bot]`) is **not** treated as a reviewer bot by this command.
This applies only when codecov is active on the target repo; a repo with no
coverage service posts no such comments and the whole coverage path is a no-op.
When present, codecov posts coverage summaries as issue-level comments that have
no threaded-reply surface and require no reply. The helper script surfaces them
as an informational "Coverage advisory" -- see Step 2.

**Greptile** (`greptile-apps[bot]`) IS treated as a reviewer bot here. It posts a
single COMMENTED review ~20 min AFTER CodeRabbit APPROVES (well after CR has
gone quiet) and its inline findings carry actionable P1/P2 badges that need the
same triage + reply discipline as CR findings. The wait loop in Step 1.5 polls
for Greptile via the same `pr-unreplied-comments.sh` script (its BOT_LOGIN_FILTER
includes greptile-apps[bot]); the `pr-watch.sh` quiet-period gate also covers
Greptile so /pr-watch never settles before Greptile's review lands.

**Codoki** (`codoki-pr-intelligence[bot]`) is NOT IN SERVICE, so it posts nothing
new; the handling below applies ONLY to a LEGACY Codoki review on an older PR. It
posts COMMENTED reviews whose review *body is empty* -- every finding lives in
inline comments plus a single `### Codoki PR Review` issue-comment summary
(severity-tagged Medium/High table). Because the review body is blank, an
APPROVED/COMMENTED state tells you nothing; you MUST read its inline + issue
comments. It is covered by the same `pr-unreplied-comments.sh` BOT_LOGIN_FILTER
and the `pr-watch.sh` quiet-period gate. Its inline threads resolve via GraphQL
(see the resolve section below), not a slash command.

**PR number (optional):** "$ARGUMENTS"

---

## Step 1 -- Identify the PR

Resolve `pr_number` and `repo`:

```bash
repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
me=$(gh api user --jq .login)
```

If `$ARGUMENTS` is a number, use it directly:

```bash
pr_number="$ARGUMENTS"
```

Otherwise detect from the current branch:

```bash
pr_number=$(gh pr view --json number --jq .number)
```

Print the PR URL for confirmation:

```bash
gh pr view "$pr_number" --json url --jq .url
```

If no PR found, stop: "No open PR found for this branch."

---

## Step 1.25 -- Determine whether CodeRabbit is expected (NEVER trigger it)

**Triggering a CodeRabbit review is the maintainer's exclusive purview.** This
command MUST NOT post `@coderabbitai review` / `@coderabbitai full review` (or
any other bot-review trigger) under any circumstance -- not when a review is
missing, not "to be safe", never. CR auto-review is OFF org-wide, so CR is
**opt-in**: it reviews only when the maintainer has already allocated a pass.
A missing CR review is a normal state, not a problem to fix.

Detect whether a CR review is *expected* -- so Step 1.5 knows whether to wait
for one -- **without triggering anything**:

```bash
# CR is EXPECTED if ANY of: (a) it already reviewed this PR, (b) it is a
# requested reviewer, or (c) a maintainer-posted `@coderabbitai review` trigger
# comment already exists. This is read-only detection -- it posts nothing.
#
# All three use the REST API and match the `[bot]`-suffixed login, exactly like
# the canonical scripts/pr-watch.sh (#173). Do NOT use `gh pr view --json` here:
# its GraphQL logins drop the `[bot]` suffix (`coderabbitai`, not
# `coderabbitai[bot]`), so a `[bot]`-suffixed match would silently never fire.
cr_reviews=$(gh api "repos/$repo/pulls/$pr_number/reviews" \
  --jq '[.[] | select(.user.login == "coderabbitai[bot]")] | length')            # (a)
cr_requested=$(gh api "repos/$repo/pulls/$pr_number/requested_reviewers" \
  --jq '[.users[].login] | index("coderabbitai[bot]") // empty')                 # (b)
cr_triggered=$(gh api "repos/$repo/issues/$pr_number/comments" \
  --jq '[.[] | select(.body | test("@coderabbitai\\s+(full\\s+)?review\\b"; "i"))] | length')  # (c)
```

If none of those signals is present (`cr_reviews == 0` AND `cr_requested` empty
AND `cr_triggered == 0`), CR is **not expected**: do NOT wait for it
in Step 1.5 and do NOT trigger it. Triage whatever reviews already exist (CR only
if it already reviewed). If a CR pass would help, you may
note "a CR pass is available -- the maintainer can allocate one" and stop; never
post the trigger yourself.

This mirrors `pr-watch.sh`'s opt-in CR logic (#173): waiting for -- or
manufacturing -- a review that will never land is exactly the idle-hang that fix
removed.

---

## Step 1.5 -- Wait for bot reviews to complete

Bot reviewers can post inline comments in waves -- the review status may show
"complete" before all comments have landed. Starting triage too early means
re-triaging when late comments arrive.

**Readiness check:** A PR is ready for triage when BOTH conditions hold across two
consecutive polls:
1. No pending review requests for bot users **that are expected** -- i.e. only
   wait on CodeRabbit when Step 1.25 found it *expected* (already reviewed /
   requested / maintainer-triggered). When CR is not expected, do not count it as
   pending; any already-present bot review is what you settle on.
2. Unreplied bot comment count is stable (same count on two consecutive checks)

**Geometric cooldown:** Reviews are never triggered by this command (Step 1.25);
this loop only *waits* for reviews already in flight. Poll with increasing
intervals: **15s → 30s → 60s → 120s**. At each interval:

```bash
pending=$(bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh --pending-only "$pr_number")
unreplied=$(bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh --count-only "$pr_number")
```

If `pending == 0` AND `unreplied` count matches the previous check → ready.
If not stable after 4 polls (~3.75 minutes), tell the user which bots are still
pending and ask whether to proceed or keep waiting.

**Skip the cooldown** if the user explicitly says reviews are done or asks to proceed
immediately.

---

## Step 2 -- Fetch all review comments

**Enumerate EVERY finding first (MANDATORY).** Before any per-class reading, run
the itemized checklist -- it prints ONE checkable line per UNADDRESSED finding
across ALL THREE classes (inline threads, review-BODY findings, issue-level
actionable comments), so no class can be dropped by an inline-only glance. Triage
against THIS complete list, never against a glance at inline comments alone:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh --itemized "$pr_number"
```

Each line is `<class> | <user> | <loc> | <excerpt> | replied:<..> resolved:<..>`.
A **review-body** finding has NO inline thread to resolve: it clears when you ACK
THE REVIEW BY ID -- a comment of yours whose body REFERENCES the review id:

```sh
reply-comment.sh --review <review-id> <pr> "<why it is addressed / the fix SHA>"
```

The review id is the ack token. A reply WITHOUT it does NOT clear the finding --
not a `--file/--line` inline reply, not an `@coderabbitai resolve`, not a bare
"fixed in <sha>". (An earlier version of this doc claimed such a finding "clears
only when the reviewer re-reviews a fresh SHA / a maintainer re-trigger". THAT WAS
FALSE -- the code never implemented it. Following that advice is what left a ready
PR blocked and forced a merge override on stillwater #2424: the lead fixed the
finding, replied without the id, and the gate correctly never cleared.) Work every line to closure before claiming the
review is triaged.

Then pull the full bodies per class for the actual fixes:

```bash
# Unreplied inline comments (truncated first line -- good for triage overview).
# The default output also includes a "Coverage advisory" section when
# codecov[bot] has posted a coverage summary on the PR. That advisory is
# informational only -- codecov comments are coverage reports, not review
# threads, and require no reply.
bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh "$pr_number"

# Full bodies of all unreplied inline comments
bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-read-comments.sh "$pr_number"

# Full bodies of review-body comments (actionable findings in review summaries)
bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-read-comments.sh --reviews "$pr_number"

# Full bodies of issue-level bot comments (e.g. github-actions docs-drift
# advisories that workflows post on the PR conversation tab, not on the diff).
# These have no threaded-reply surface but often carry actionable signals.
bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-read-comments.sh --issue "$pr_number"

# Full bodies of specific comment IDs only
bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-read-comments.sh "$pr_number" 123456 789012
```

### Code-scanning (GHAS / CodeQL) alerts -- separate API surface

The comment scripts above read the review/issue-comments API and **cannot see
code-scanning alerts or their Copilot Autofix suggestions** -- those live in a
different API surface (`repos/.../code-scanning/alerts[/{n}/autofix]`). A CodeQL
alert, and a committable autofix, otherwise sails right past this flow. Always
surface them:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-codeql-autofixes.sh "$pr_number"
```

For each open alert it prints the location, message, alert URL, and whether a
Copilot Autofix is available. Triage each like any finding: real -> fix (or
apply the autofix); false positive -> dismiss via
`gh api -X PATCH repos/<repo>/code-scanning/alerts/{n} -f state=dismissed
-f dismissed_reason='false positive' -f dismissed_comment='...'` (comment max
280 chars). Do NOT merge/approve while open code-scanning alerts remain
untriaged.

### Issue-level advisory handling

If `--issue` surfaces a `github-actions[bot]` **docs-drift advisory**
(workflow-posted, body begins with `<!-- docs-drift-bot -->`), treat it as a
labelling decision rather than a code-fix finding:

- If the PR's user-visible behavior really did change but no `docs/` touch
  is in the diff, add the docs touch in this round (the bot's intent).
- If the PR is test-only, refactor, or otherwise has no user-visible change,
  swap the `needs-docs-review` label to `docs: not-required` on this push.
  The advisory clears when the label flips on the next push.

The advisory has no threaded-reply surface and no `@github-actions resolve`
command -- it resolves itself when the next push satisfies the bot's rule.
Do not categorise it under the bug/false-positive/etc. taxonomy.

For other `github-actions[bot]` issue comments (release recaps, label-gate
status, deploy markers), surface them to the user only if they look
actionable; otherwise note them silently and move on.

### Coverage advisory handling

This whole subsection is conditional on the repo having a coverage service. If
the coverage probe returns `{"status":"none"}` (no codecov integration, the case
on repos like cc-orchestrator itself), skip all coverage advisory handling
entirely -- there is no signal to act on, and that is not a failure.

If the script emits a "Coverage advisory" section with `threshold_state: fail`,
surface it to the user alongside (not as part of) the triage table. A failing
patch-coverage threshold is not a review comment and must not be categorised
with the `bug` / `false-positive` taxonomy -- it is a separate policy signal.

Default response:

- **Patch coverage below threshold**: note the % and the codecov report URL
  in the triage summary. Do not auto-add a "test-gap" item unless the user
  asks -- raising coverage is often out of scope for a review round.
- **Patch coverage above threshold or no advisory**: skip mentioning it.

Never post a reply to codecov comments. They have no threaded-reply surface
and codecov has no `@codecov resolve` command; the advisory resolves itself
when a follow-up push changes coverage.

---

## Step 3 -- Identify open (unreplied) comments

A comment is **open** if:
1. It is a top-level comment (`reply_to` is null) from a reviewer bot (login ends
   with `[bot]`, case-insensitive)
2. AND there is no subsequent comment in the same thread from `$me` (the current user)

Build the open list:
- Collect all comment IDs that have a reply from `$me` (where `reply_to` matches
  the reviewer comment's `id`)
- Subtract from the full list of top-level bot/reviewer comments

Print a numbered list of open comments, grouped by type:

```text
Open review comments (N total):

Inline (can receive threaded replies):
1. [id: 123456] [inline] path/to/file.go -- "First line of comment body..."
2. [id: 789012] [inline] internal/api/handlers.go -- "First line..."

Review body (outside-diff findings, no inline thread):
3. [id: 4093297841] [review-body] -- "Actionable comments posted: 1 ..."
...
```

**Important:** Review body comments are review objects (IDs from the reviews API), NOT
inline comment IDs. They cannot receive threaded replies via `pulls/comments/{id}/replies`.
Findings embedded in review bodies are "outside diff range" items that CodeRabbit could not
post inline. They are triaged and fixed normally in Steps 4-5, but handled differently in
Steps 7 and 8.5.

If there are no open comments, say: "No open review comments. Nothing to do." and stop.

---

## Step 4 -- Read and categorize each comment

For each open comment:
1. Read the full comment body
2. Read the referenced file and line range in the current codebase
3. Assign one of these categories:

| Category | Meaning |
|----------|---------|
| `bug` | Real code defect -- must fix |
| `spec-drift` | Docs, schema, or interface contract no longer matches the implementation (e.g. a help text, a config schema, an API spec) -- must fix |
| `test-gap` | Missing test coverage for a real gap -- should fix |
| `false-positive` | Established pattern, known behavior, or intentional design |
| `already-fixed` | Was corrected in a later commit; reply needed but no code change |
| `wont-fix` | Valid suggestion but out of scope for this PR |

### Propagation sweep

Before printing the triage table, check whether any comment is a symptom of a
broader pattern that could recur elsewhere in the codebase. This prevents the
whack-a-mole cycle of fixing one instance per review round.

For each comment about:
- A stale variable or parameter name (e.g. `resized` where `converted` is expected)
- A renamed or replaced function still referenced by the old name
- A stale comment or log message referencing the old behavior
- Any other pattern that is likely copy-pasted across multiple call sites

Run a targeted search before writing any fixes:

```bash
# Example: find all occurrences of the stale name. Scope --include to the target
# repo's source types -- e.g. --include="*.sh" --include="*.py" for this repo, or
# the relevant language extensions in another.
grep -rn "old_name\|oldNameErr" . --include="*.sh" --include="*.py" | grep -v "test-"
```

Add every additional occurrence found to the fix scope for that comment. Note them
in the triage table's Summary column so the scope is visible.

Print the full triage table before making any changes:

```text
## Triage

| # | ID     | Category       | File                 | Summary |
|---|--------|----------------|----------------------|---------|
| 1 | 123456 | bug            | reply-comment.sh     | error path returns empty result, swallows failure |
| 2 | 789012 | false-positive | orchestrate-guard.sh | intentional fail-open on internal error |
| 3 | 345678 | already-fixed  | SKILL.md             | stale description, fixed in abc1234 |
| 4 | 456789 | bug            | pr-watch.sh (+3)     | stale `old_name` -- also in safe-push.sh, ship-gate-preflight.sh (x2) |
...
```

Ask: "Does this triage look right? (yes / adjust N to <category>)"

Wait for confirmation before proceeding to fixes.

After confirmation, create a task for each comment that requires a code change (`bug`,
`spec-drift`, `test-gap`) using `TaskCreate` with a short subject describing the fix.
Record the task ID alongside the comment ID -- you will mark it complete in Step 5.

---

## Step 5 -- Implement all fixes

For every comment categorized as `bug`, `spec-drift`, or `test-gap`:

- Read the relevant code
- Make the minimal correct fix
- Do NOT push yet
- Mark the corresponding task as `completed` using `TaskUpdate`
- Note the fix briefly for use in the reply

Apply fixes for all comments before moving to the next step. Fix order matters --
address `bug` fixes before `spec-drift` since spec updates should reflect final behavior.

After all edits are complete, run the project's test suite. Determine the correct test
command from CLAUDE.md, Makefile, package.json, or project conventions. Common examples:
- Go: `go test ./...`
- Node/TS: `npm test` or `pnpm test`
- Python: `pytest`
- Rust: `cargo test`

If tests fail: stop. Fix the test failures before continuing. Do not reply to comments
or push with broken tests.

---

## Step 5.5 -- Local code review (conditional)

Before committing, decide whether to run the `pr-review-toolkit` agents. Deterministic
validation comes first; agents are only warranted when the diff is substantive enough
for their cost (~50-100K tokens, ~60s wall time) to yield real signal.

**Always run the deterministic gate first** -- this catches lint, type, test,
schema-drift, coverage, and generated-file regressions at zero agent cost.
Delegate gate detection + execution to the bundled `gate-runner.py` (it reads
the repo's `.gates.toml`, or falls back through the umbrella -> CLAUDE.md
`## Gates` -> language-basics -> warn-and-proceed chain), rather than
re-implementing per-stack test commands here:

```bash
git diff --name-only   # identify changed files
if [ -f scripts/gate-runner.py ]; then
  python3 scripts/gate-runner.py
else
  python3 ~/.claude/scripts/gate-runner.py
fi
gate_rc=$?
```

The runner prints a per-step `[PASS]` / `[SKIP]` / `[FAIL]` line and exits
non-zero on the first required-gate failure. *Illustrative -- what runs in
cc-orchestrator:* its `.gates.toml` enumerates `shellcheck` on the shell
scripts, `ruff check --select F,E741` on the `.py` files, the
`orchestrate-guard.sh`/`orchestrate-steer.sh` `--self-test` runs, and the
`python3 test-*.py` harnesses. Another target repo declares a different set (or
relies on the runner's fallback chain).

Only escalate to agents if the deterministic gate passes AND the diff meets the
"agents merited" bar below. When the gate fails, fix the failures first -- agents
cannot tell you something the compiler or linter already told you.

### Estimating patch coverage (do not hand-roll)

This block applies only when the target repo runs a coverage service and a round
touches a failing `codecov/patch` status. On a repo with no coverage integration
it is a no-op -- skip it. When it does apply, **do not write an ad-hoc coverage
script.** Use the maintained estimator, which mirrors Codecov's projection
(all-hit rule + trailing-brace correction). The profile-generation step below is
illustrative (a Go example); substitute the target repo's coverage-profile
command:

```bash
go test -count=1 -coverprofile=/tmp/cover.out ./...
COVER_OUT=/tmp/cover.out PATCH_COVERAGE_THRESHOLD=<repo target> \
  PATCH_COVERAGE_EXCLUDE="<codecov.yml ignore globs>" \
  bash ${CLAUDE_PLUGIN_ROOT}/scripts/patch-coverage.sh   # or repo-local scripts/patch-coverage.sh
```

Self-skip on the absent-signal cases: if `patch-coverage.sh` exits 0 reporting
"no Go source changes in scope" (the diff touches nothing the estimator
measures), or exits 2 (missing coverage profile -- no coverage tooling on the
repo), treat coverage as SKIP and move on. Neither is a failure. Likewise when
`pr-unreplied-comments.sh --coverage-only` returns `{"status":"none"}`, or the repo's
`.gates.toml` sets `[merge_pr]` `coverage_advisory = false`, report Coverage as
**N/A** (a self-skip, not a failure).

Interpret the result as a **conservative lower bound**: it counts a line covered
only if every coverage block touching it was hit (mixed-hit lines count as
missed, matching Codecov's partial accounting), and Codecov's block-to-line
projection typically reads a few points *higher* than this script. So:

- **Local estimate >= threshold** -> Codecov will pass. Stop adding tests.
- **Local estimate within ~5 points below threshold** -> Codecov may still
  pass. Add coverage for the cheapest genuinely-reachable branches to clear the
  *local* number, then push and let Codecov be the source of truth. Do **not**
  grind the local number far past the threshold -- that over-tests defensive
  error paths Codecov never counted, the exact "nickel-and-diming" failure mode
  this guidance exists to prevent.
- **Local estimate well below threshold** -> there is a real gap; close it.

Target the *local* metric to just clear the threshold, not to maximize it.

### When agents are merited

Run both `pr-review-toolkit:code-reviewer` and `pr-review-toolkit:silent-failure-hunter`
in parallel when **any** of the following applies:

- Concurrency primitives or shared state touched (goroutines, mutexes, channels,
  atomics, shared maps)
- Error-handling paths changed (new error branches, new fallbacks, retries,
  timeouts, or any `catch`/`recover`/error-swallowing construct)
- Security-sensitive code (auth, encryption, CSRF, input validation, redaction,
  permissions, file paths accepting external input)
- API contract changes (status codes, response shapes, OpenAPI spec edits)
- Diff introduces new abstractions, interfaces, or non-trivial logic not directly
  dictated by a CR diff (i.e. Claude had to design something)
- Diff spans >~100 lines of new logic (test files excluded from the line budget)

### When to skip agents

Skip with a one-line note like "Skipping review agents: N mechanical CR-directed
fixes, deterministic gate clean" when **all** of the following hold:

- Every fix is a mechanical implementation of a CR suggestion that
  supplied an explicit diff
- Total new logic is small (≲100 lines, not counting tests)
- No concurrency / error-handling / security / API-contract surface touched
- `pre-push-gate` (or project equivalent) passed clean

The user has explicitly flagged over-running agents as a cost problem. Default
toward skipping when the above criteria hold; default toward running when in
doubt about the substantive axes above.

### Acting on agent findings

If agents run and flag **critical** issues: fix them before committing. Do not push
code that will generate a new bot complaint on the next round.

If agents flag **important** (non-blocking) issues: present them briefly and ask
the user whether to fix them now or proceed. Do not block on suggestions.

Style-only nitpicks (e.g. hyphenation preferences in error strings, comment
wording): report and move on. Do not extend the fix scope for these.

---

## Step 6 -- Compose replies

For each open comment, draft a reply:

**bug / spec-drift / test-gap (fixed):**

```text
Fixed in <sha>. <one-sentence description of what changed>.
```

Get the sha after the fixes are committed (step 7 happens before replies are posted --
see below).

**false-positive:**

```text
<Explanation of why this is correct. Reference the specific CLAUDE.md pattern or
architectural decision if applicable. Keep it brief -- one or two sentences.>
```

**already-fixed:**

```text
Fixed in <earlier-sha>.
```

**wont-fix:**

```text
Acknowledged. This is out of scope for this PR -- tracking separately as #<issue> or
leaving for a follow-up.
```

---

## Step 7 -- Commit, get SHA, then post replies

Commit all fixes in a single commit:

```bash
git add -p  # or git add <specific files>
git commit -m "fix: address PR review findings

<bullet list of what was fixed>

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

Get the short SHA:

```bash
git rev-parse --short HEAD
```

Now substitute the real SHA into all "Fixed in <sha>" reply drafts from step 6.

### Inline comments -- threaded replies

Post threaded replies for **inline** comments only (do not wait between them):

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/reply-comment.sh "$pr_number" {COMMENT_ID} '<reply text>'
```

Run one call per inline comment. Log each one as it completes.

### Review body comments -- inline reply on the fixed line

Review body IDs are review objects, NOT inline comment IDs. The
`pulls/comments/{id}/replies` endpoint returns 404 for them, so the 3-arg form
of `reply-comment.sh` does not work. The old workaround -- a consolidated
top-level PR comment -- is blocked by the no-toplevel-summaries hook and is
noise in the Conversation tab anyway.

**Preferred path:** post a new inline review comment anchored to the line your
fix touched (or a nearby in-diff line) using the 4th form of the helper:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/reply-comment.sh "$pr_number" \
  --file path/to/file.go --line 663 \
  "Fixed in <sha>. <one-line rationale>."
```

The helper auto-resolves PR HEAD, defaults `--side RIGHT`, and errors cleanly
if `--file` / `--line` aren't paired. GitHub's `pulls/{n}/comments` endpoint
requires the line to be in the PR's diff, so anchor to a line the fix
actually changed (bug/spec-drift/test-gap) or to the nearest in-diff line
in the same file (false-positive rebuttal, wont-fix tracking link,
already-fixed pointer to an earlier SHA).

**Per-category pattern for review body findings:**

- **bug / spec-drift / test-gap**: anchor to the line your fix changed.

  ```bash
  bash ${CLAUDE_PLUGIN_ROOT}/scripts/reply-comment.sh "$pr_number" \
    --file internal/foo/bar.go --line 42 \
    "Fixed in <sha>. <one-line>."
  ```

- **false-positive**: anchor to the referenced line (or nearest in-diff line
  in the same file) with the rebuttal:

  ```bash
  bash ${CLAUDE_PLUGIN_ROOT}/scripts/reply-comment.sh "$pr_number" \
    --file internal/foo/bar.go --line 42 \
    "<evidence-based rebuttal, one or two sentences>."
  ```

- **already-fixed**: anchor to the line carrying the fix, citing the
  earlier SHA:

  ```bash
  bash ${CLAUDE_PLUGIN_ROOT}/scripts/reply-comment.sh "$pr_number" \
    --file internal/foo/bar.go --line 42 \
    "Fixed in <earlier-sha>."
  ```

- **wont-fix**: anchor to the referenced line with the tracking issue:

  ```bash
  bash ${CLAUDE_PLUGIN_ROOT}/scripts/reply-comment.sh "$pr_number" \
    --file internal/foo/bar.go --line 42 \
    "Tracking in #<issue>. <one-line justification>."
  ```

**Skip the inline reply entirely when all review body findings are
`bug`/`spec-drift`/`test-gap` addressed by the commit.** The commit message
carries the record, `@coderabbitai resolve` sweeps the threads, and an
extra inline comment adds no signal. Only post replies that carry information
the commit doesn't: rebuttals, wont-fix tracking, or already-fixed pointers
to earlier SHAs.

**Fallback:** if the review body finding references a file that is not in
the PR's diff at all (rare -- usually CR anchors to something nearby), the
API will reject the inline form. In that case, do not post a top-level
summary -- the no-toplevel-summaries hook blocks it. Rely on the commit
message + `@coderabbitai resolve` and note the unreplied finding in the
Step 9 summary so the reader knows it was handled but not threaded.

---

## Step 8 -- Resolve review threads (BEFORE push)

After replies are posted in Step 7 and **before** running `git push`, resolve the
threads that were replied to in this round. Resolving before push is mandatory:
pushing first races CodeRabbit's automatic re-review, which can auto-resolve fresh
threads from the new round and lock them out of inline replies. The resolve step
acts on already-replied threads and does not require the new commit to be visible
on origin.

### CodeRabbit threads -- `@coderabbitai resolve`

Post a single PR-level comment to resolve all addressed CR threads at once:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/reply-comment.sh "$pr_number" '@coderabbitai resolve'
```

This tells CodeRabbit to mark all of its threads that have been replied to as
resolved. CR resolve also covers review body findings -- CodeRabbit tracks its
own outside-diff items and will mark them resolved when the underlying code
changes appear on the next push.

Report that CR resolve was requested.

### Copilot + Greptile + Codoki threads -- GraphQL resolve

Copilot (`copilot-pull-request-reviewer[bot]`), Greptile (`greptile-apps[bot]`),
and Codoki (`codoki-pr-intelligence[bot]`) threads must all be resolved via
GraphQL -- none of them has a `@<bot> resolve` slash command. The default
`--bot` regex on `resolve-threads.sh` is `copilot|greptile|codoki`, so a single
call covers all three bots when the IDs span them:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/resolve-threads.sh "$pr_number" <comment_id> [<comment_id>...]
```

If you need to scope to a single bot (e.g. you triaged only Greptile in this
round), pass `--bot`:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/resolve-threads.sh --bot greptile "$pr_number" <id...>
bash ${CLAUDE_PLUGIN_ROOT}/scripts/resolve-threads.sh --bot copilot  "$pr_number" <id...>
```

The script fetches all review threads, matches each ID to a thread whose first
comment's author.login matches the regex, and calls the `resolveReviewThread`
GraphQL mutation. It skips threads that are already resolved or whose author
doesn't match, and prints one line per thread.

**Only run this step when there were Copilot, Greptile, or Codoki inline comments in
the current round.** If none of those bots' comments were triaged, skip it silently.
A typical Greptile round is small (single COMMENTED review, often 1-3 inline
findings) -- don't skip just because the count is low.

Report the output (resolved / skipped lines) so the summary in Step 9 can
reflect thread resolution status across both bots.

---

## Step 8.5 -- Push

Only after all resolves above have been requested (CR) and applied (Copilot +
Greptile via GraphQL), push.

Then push:

```bash
git push origin $(git branch --show-current) 2>&1
```

Report the result. If the push fails, explain why -- do not retry automatically.
Do not invert this order: resolving after push races CR's automatic re-review and
has caused fresh threads to be auto-resolved before they could be replied to.

---

## Step 9 -- Summary

**Build the summary deterministically from tracked variables.** Do not write it
freehand. Compute each field from the data already collected in earlier steps:

| Field | Source |
|-------|--------|
| PR link | `$repo` (Step 1) + `$pr_number` (Step 1) |
| Fixed count | `count(triage where category in {bug, spec-drift, test-gap})` |
| Dismissed count | `count(triage where category in {false-positive, wont-fix})` |
| Noted count | `count(triage where category == already-fixed)` |
| Replied count | total open comments from Step 3 |
| SHA | `git rev-parse --short HEAD` from Step 7 |
| Branch | `git branch --show-current` |
| Resolved | whether CR resolve was posted + Copilot/Greptile threads resolved |

Assemble and print:

```text
## Done -- PR [#$pr_number](https://github.com/$repo/pull/$pr_number)

- Fixed: $fixed_count comments (bug/spec/test)
- Dismissed: $dismissed_count comments (false-positive/wont-fix)
- Noted: $noted_count comments (already-fixed)
- Replied: $replied_count total
- Pushed: $sha to $branch
- Resolved: CR resolve $cr_status; GraphQL threads (Copilot + Greptile + Codoki) $graphql_resolve_status
```

Codoki is not in service, so it does not review the push; Greptile (where
installed) still may. CodeRabbit reviews only if the maintainer has allocated a
pass -- this command never triggers one.
