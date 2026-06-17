---
description: "Run all pre-push checks then squash and push -- the full pre-PR gate"
argument-hint: "[optional: short description of what this PR does]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Agent", "Task", "Skill"]
---

# PR Preparation Gate

Run every pre-push check in order. Gate on failures. Squash and push only when clean.

**Optional context:** "$ARGUMENTS"

---

## Step 1 -- Orient

```bash
base=$(git merge-base main HEAD)
git branch --show-current
git log main..HEAD --oneline
git diff "$base"..HEAD --stat
```

Report:
- Current branch name
- Number of commits ahead of main
- Files changed (summary)

If on `main`, stop immediately: "You are on main. Create a feature branch first."

---

## Step 1b -- Hand-written PR size gate

Generated files (`_templ.go`, `*.sum`, `*.lock`) inflate `git diff --stat`
and disguise PR risk. The relevant signal is **hand-written LOC** — what
reviewers actually have to read, and what correlates with CR round count
in this repo (see `feedback_pr_size_handwritten_loc.md`).

Compute hand-written diff size:

```bash
base=$(git merge-base main HEAD)
git diff --stat "$base"..HEAD -- ':!*_templ.go' ':!*.sum' \
  ':!*.lock' ':!go.work.sum' ':!*.snap'
```

Capture totals (additions + deletions) and file count from the
`N files changed, A insertions(+), D deletions(-)` summary line.

**Thresholds:**
- >800 hand-written LOC (additions + deletions), OR
- >10 hand-written files

If either is exceeded, **stop** and say:

> "This PR has `<N>` hand-written LOC across `<F>` files, exceeding the
> sustainable-PR-size threshold (800 LOC / 10 files). Large PRs in this
> repo correlate with multi-round CR churn (#1484 ran 12 rounds; #1497
> ran 6 rounds).
>
> Options:
> 1. **Split into multiple PRs by issue/concern (preferred).** Open
>    follow-up issues for the unbundled work, scope this PR to one
>    concern, drop the rest from the branch.
> 2. **Override and proceed** (only if the size is truly cohesive — e.g.
>    a single protocol implementation that can't be partitioned without
>    breaking atomicity). Provide a rationale; it will be captured in
>    the PR body's Summary section.
>
> How do you want to proceed? (split / override <rationale>)"

Wait for the answer.
- If "split": stop. Do not run further gates. Help the user split if
  asked, but the default is to let them drive.
- If "override <rationale>": record the rationale verbatim, include it
  as a line in the Step 8b Summary section ("Size override: <rationale>"),
  and continue to Step 2.

If both thresholds are within bounds: print the hand-written totals
("N LOC across F files — within threshold") and continue.

---

## Step 2 -- Tests

```bash
go test -count=1 -coverprofile=/tmp/stillwater-cover.out ./... 2>&1
```

If any test fails: print the failures, stop, and say:
"Fix failing tests before proceeding. Do not push broken code."

If tests pass: note it and continue to Step 2b.

---

## Step 2b -- Patch coverage gate (Codecov parity)

This is a **gate**, not a warning. Codecov's patch-coverage check measures the
percentage of *changed lines* that are exercised by tests, and PRs that fall
below the project's threshold get flagged. The earlier "0%-function" check
silently passed PRs whose changed lines were partially covered (e.g. a function
that's 10% line-covered shows up as non-zero per-function and slips through).
Replicate Codecov's line-level patch projection locally so failures surface
before the first push, not after CI runs.

The implementation lives in `${CLAUDE_PLUGIN_ROOT}/scripts/patch-coverage.sh`. It mirrors
Codecov's projection of coverage blocks onto the diff's added lines, using the
all-hit rule (a line counts as covered only if every block touching it was hit;
mixed-hit lines count as missed, matching Codecov's partial accounting). It runs
as a **conservative lower bound**: if the local check passes, Codecov passes
too. The residual difference is line-projection -- Codecov collapses some
multi-line statements (e.g. a `fmt.Errorf(...)` error-return spanning two lines)
that Go's profile counts as two, so Codecov typically reads a few points
*higher* than this script. A narrow local miss (within ~5 points of threshold)
may therefore still pass Codecov; gate on the local number but don't pile on
tests to push it far past the threshold.

**Run the gate** against the profile produced in Step 2. `patch-coverage.sh`
resolves both the threshold and the file excludes from the repo's own
`codecov.yml` (the patch `target` and the `ignore:` list), so the local
benchmark matches exactly what Codecov enforces -- no threshold parsing or
hard-coded excludes needed here:

```bash
COVER_OUT=/tmp/stillwater-cover.out \
  bash ${CLAUDE_PLUGIN_ROOT}/scripts/patch-coverage.sh
gate_status=$?
rm -f /tmp/stillwater-cover.out
```

To override for a one-off -- a repo with no `codecov.yml` patch target, or a
deliberately lower bar -- pass `PATCH_COVERAGE_THRESHOLD=<n>` (and/or
`PATCH_COVERAGE_EXCLUDE="<globs>"`), and explain the override in the PR
description. An explicit env var always wins over the `codecov.yml` value.

**Interpret the exit code:**

- `0` -- patch coverage meets the threshold, or there's nothing in scope. Print
  the script's report and continue to Step 3.
- `1` -- patch coverage below threshold. Print the per-file breakdown the
  script emitted, then say:
  > "Patch coverage `<pct>%` is below the `<threshold>%` threshold. Codecov
  > will flag this. Add tests for the uncovered lines, then re-run prep-pr.
  > **STOP** -- do not push. (To override for a one-off, re-run with
  > `PATCH_COVERAGE_THRESHOLD=<lower>` and explain in the PR description.)"
  >
  > Do NOT proceed without the user explicitly overriding.
- `2` -- script configuration error (missing profile, unreadable go.mod, no
  `main` branch, etc.). Treat this as a setup issue, surface the script's
  stderr, and stop. Do not silently skip.

**If the script is missing** (`${CLAUDE_PLUGIN_ROOT}/scripts/patch-coverage.sh` not found),
treat as a `2` configuration error: stop and tell the user to install it from
the gist (`ff73bb5142ea2be2acfc6ae025576c15`). Do NOT fall back to the old
0%-function check -- that gate is what let this regress in the first place.

**Excluding files from patch coverage:** the script already reads the repo's
`codecov.yml` `ignore:` list, so generated files and pure-CLI entry points that
Codecov ignores are excluded locally too -- no action needed when they are
listed there. Add them to `codecov.yml` `ignore:` (the single source of truth),
or for a one-off pass `PATCH_COVERAGE_EXCLUDE` (space-separated git pathspecs)
and document it in the PR description.

**Recommended repo setup:** add a `codecov.yml` with an explicit patch target
and an `ignore:` list. The script reads both, so the local gate and Codecov use
the same number and the same scope deterministically:

```yaml
coverage:
  status:
    patch:
      default:
        target: 70%
        threshold: 0%
ignore:
  - "cmd/<app>/main.go"     # entrypoint wiring, not unit-testable
  - "**/*_templ.go"         # generated code
```

Without `codecov.yml`, Codecov uses `target: auto` (match project coverage),
which makes the effective threshold drift as project coverage moves -- the
local gate then falls back to its default and can't perfectly match.

---

## Step 3 -- OpenAPI consistency check

Run the AST-based consistency test first:

```bash
go test -count=1 -run TestOpenAPIConsistency -v ./internal/api/
```

If it fails, fix the reported spec drift before continuing.

Then follow the semantic checks in `.claude/commands/check-openapi.md` against the
PR-wide diff:

```bash
base=$(git merge-base main HEAD)
git diff "$base"..HEAD --name-only
```

Do not use `git diff main` directly -- that can include unrelated commits that landed
on main after this branch was cut.

Report findings using the same CRITICAL / IMPORTANT / OK format defined in that file.

If any CRITICAL finding: stop. List what must be fixed.

---

## Step 3b -- Rename completeness check

If the diff contains any renamed functions, variables, types, or constants:

```bash
base=$(git merge-base main HEAD)
git diff "$base"..HEAD | grep '^-.*func \|^-.*type \|^-.*var \|^-.*const ' | \
  sed 's/^-//' | grep -oE '[A-Z][a-zA-Z0-9]*'
```

For each old name found, grep the full codebase for remaining references:

```bash
grep -rn "OldName" --include='*.go' --include='*.templ' .
```

**Flag as CRITICAL** if the old name still appears in code (excluding the diff's `-` lines
and comments). Incomplete renames cause compilation errors or silent behavior changes.

---

## Step 3c -- Raw error leak check

Check for raw internal error messages leaking to clients:

```bash
base=$(git merge-base main HEAD)
git diff "$base"..HEAD -- internal/api/handlers_*.go | grep '^+' | \
  grep -E 'err\.(Error|String)\(\)|fmt\.(Sprintf|Errorf).*err[^o]' | \
  grep -v 'slog\.\|logger\.\|log\.'
```

**Flag as IMPORTANT** if any handler response path includes raw `err.Error()` text.
Client-visible messages must be generic; the full error belongs in a server-side `slog` call.

---

## Step 3d -- Axis/state vocabulary drift check

When a PR extends a multi-state system with a new axis, state, or mode, backend
code is usually updated while user-facing copy (i18n keys, inline templ
strings, templ code comments, hardcoded English in helper functions) silently
retains the pre-expansion vocabulary. CodeRabbit flags these drift sites one
or two per review round, costing multiple review cycles. See
`feedback_axis_vocabulary_sweep.md` in memory for the failure-mode background.

Detect candidate expansions in the diff:

```bash
base=$(git merge-base main HEAD)

# New enum/const values added in internal/ code
new_consts=$(git diff "$base"..HEAD -- 'internal/**/*.go' | \
  grep -E '^\+\s+[A-Z][A-Za-z]+\s+[A-Z][A-Za-z]+\s*=\s*"' | head -5)

# New case labels in switch statements (both Go and templ)
new_cases=$(git diff "$base"..HEAD -- 'internal/**/*.go' 'web/templates/**/*.templ' | \
  grep -E '^\+\s+case\s+["A-Z]' | head -5)
```

If either variable is non-empty, the diff likely introduces a new state or
axis. Report what was found and ask:

"This diff appears to add a new state, axis, or enum value. Before pushing,
sweep these surfaces for stale mono-axis vocabulary (the phrasings that were
accurate before this PR added the new axis):

  - `internal/i18n/locales/*.json` -- every key in the affected subsystem
  - `web/templates/**/*.templ` -- both runtime strings AND code comments
  - `web/templates/helpers.go` (and similar) -- hardcoded English in helper
    functions that bypass `t(ctx, ...)`
  - `aria-label`, `role`, and `alt` attributes embedded in .templ files

Specifically grep for the pre-expansion terms, not the new ones -- the old
vocabulary is what will read as incorrect after the axis lands. Proceed, or
pause to sweep now? (sweep/proceed)"

Wait for the answer. If "sweep": pause here. If "proceed": continue to Step 4.

If both variables are empty: print "No new states/axes detected -- vocabulary
drift check skipped." and continue.

---

## Step 3e -- Vulnerability scan (govulncheck)

Run the vulnerability scan that mirrors the Security CI job. This is a **gate**.
`make vulncheck` wraps `go run golang.org/x/vuln/cmd/govulncheck@v1.1.4 ./...`,
the same pinned version CI installs, so a local pass means CI's govulncheck
will pass too.

```bash
make vulncheck
```

- **Clean** (exit 0, "No vulnerabilities found.") -- continue.
- **Findings** (non-zero exit) -- stop. govulncheck prints each advisory with a
  `Fixed in:` version. For a Go **stdlib** advisory, bump the `go` directive in
  `go.mod` to the fixed patch (every workflow reads `go-version-file: go.mod`,
  and `GOTOOLCHAIN=auto` fetches it) and update the patch pin in
  `docs/dev-setup.md`. For a **module** advisory, bump that dependency to the
  fixed version. Re-run until clean before pushing.

This is the one live-DB check intentionally kept OUT of the pre-push hook -- a
fresh advisory can flip an unrelated push red with no code change, so it runs
here, at the deliberate pre-PR gate, where that is acceptable.

---

## Step 4a -- Local code review (review-toolkit agents)

Run the full review using the Skill tool:

```
skill: "pr-review-toolkit:review-pr"
```

This runs all applicable review agents (code-reviewer, silent-failure-hunter, and conditionally
pr-test-analyzer, type-design-analyzer, comment-analyzer) and consolidates findings.

If any Critical findings: stop. Say "Fix all critical issues before pushing."

If Important findings: present them and ask:
"There are important (non-blocking) findings. Fix them now, or proceed anyway? (fix/proceed)"

Wait for the user's answer before continuing.

---

## Step 4b -- Local CodeRabbit review (CR CLI)

After the review-toolkit's findings have been triaged and any fixes applied,
run the CodeRabbit CLI as a final pre-push pass. The CLI shares an engine
with cloud-side CR, so findings here predict the first cloud review-round
with high accuracy -- catching them now collapses entire cloud review rounds.

**Cost:** ~3-4 min wall-clock on a medium diff. **Pay-off:** every cloud
round prevented is ~2 min of post-push waiting avoided. Net positive after
the first prevented round.

**Why this slot, not earlier:** running local CR before review-toolkit
triage means surfacing findings on code the toolkit will rewrite, which
wastes the pass. Running it after UAT (when applicable) keeps the diff
in final-state-ish shape so findings correspond to what will be pushed.

### Prerequisites

Skip if already verified earlier in this session, else:

```bash
coderabbit --version 2>/dev/null && coderabbit auth status 2>&1 | head -3
```

- **Missing CLI:** instruct the user to install via
  `curl -fsSL https://cli.coderabbit.ai/install.sh | sh` and stop.
- **Not logged in:** instruct `coderabbit auth login` and stop.

### Run the review

```bash
coderabbit review --plain --base origin/main 2>&1 | tail -300
```

Scope is `--base origin/main` (not local `main`) so the diff matches what
will be pushed even when local `main` is stale.

### Parse and triage

The CLI emits findings grouped by severity: `major`, `minor`, `trivial`.
Build a triage table covering **all** findings (not just Major) -- per
`feedback_present_all_review_findings.md`, never auto-defer Suggestions:
those are exactly the findings cloud-CR will rediscover and round-count
on next push. Same fix/defer schema as `/handle-review`:

| Category | Meaning |
|----------|---------|
| `bug` | Real code defect -- fix |
| `spec-drift` | OpenAPI / docs / generator out of sync with implementation -- fix |
| `test-gap` | Missing or weak test coverage -- fix |
| `false-positive` | Established pattern, intentional design -- rebut |
| `wont-fix` | Out of scope; **only valid for findings outside this PR's diff**, per `feedback_no_defer_in_scope_trivial.md` (severity isn't a deferral excuse if the surface is already open) |
| `defer` | Real but non-trivial fix requiring out-of-scope changes -- file a tracking issue, defer with rationale |

Print the full triage table with proposed categorization, then ask:

"Local CR found `<N>` findings (`<M_major>` major, `<m_minor>` minor,
`<t_trivial>` trivial). Triage as shown? (yes / adjust N to <category>)"

Wait for confirmation before applying any fixes.

### Apply fixes

For every finding categorized as `bug`, `spec-drift`, or `test-gap`:
- Read the relevant code; make the minimal correct fix.
- Mirror the deterministic-validation discipline from `/handle-review`:
  re-run only the affected test packages (not the full suite -- Step 2
  already ran the full suite, and the patch coverage gate from Step 2b
  is the floor).
- For `defer` findings, file a tracking issue with the rationale and
  record the issue number in the PR body's Pre-flight checklist (under
  a "Local CR follow-ups" line).

### When CR is missing entirely (rare)

If the CLI returns `No findings`, print "Local CR clean -- proceeding."
and continue to Step 5. If the CLI errors out non-zero with no findings
parsed, surface the stderr verbatim and stop -- do NOT silently
continue. A broken local CR pass is not the same as a clean local CR
pass.

### Bypass

If the user explicitly says "skip local CR for this PR" (e.g. tiny
mechanical change, docs-only, ralph-loop style of work), record the
bypass reason and continue to Step 5. Default is to run.

---

## Step 5 -- Generated file check

```bash
base=$(git merge-base main HEAD)
templ_changed=$(git diff --name-only "$base"..HEAD -- '*.templ')
generated_changed=$(git diff --name-only "$base"..HEAD -- '*_templ.go')

if [ -n "$templ_changed" ] && [ -z "$generated_changed" ]; then
  echo "ERROR: .templ files changed but *_templ.go files did not."
  echo "Run 'templ generate' and stage the generated files before pushing."
  exit 1
fi
```

If the check exits with an error, stop and show the message.

---

## Step 6 -- Squash to a single commit

PR branches must reach the first push as **exactly one commit**. Reviewers (CodeRabbit
and humans) read the full changeset at once instead of discovering issues
commit-by-commit, which costs review rounds. Do not ask whether to squash -- squash.

Count commits ahead of main:

```bash
git log main..HEAD --oneline
```

If there is already only one commit, say: "Already a single commit -- no squash needed."
and continue to Step 7.

If there is more than one commit, squash non-interactively (no editor, no user
prompt). Use a soft reset to the merge-base, then a single new commit:

```bash
base=$(git merge-base main HEAD)
git reset --soft "$base"
git commit -m "<conventional-commit-title>" -m "<optional body>"
```

Choose the title:
- If `$ARGUMENTS` was provided, use it (prefixed with the right conventional-commit
  type: `fix:`, `feat:`, `chore:`, etc.) and keep it under 70 chars.
- Otherwise pick the strongest of the existing per-commit titles on the branch and
  reuse it. If multiple commits cover distinct concerns (e.g. a fix plus a
  feature), prefer the linked issue's title with the right type prefix.

For the body, summarize what changed in 1-3 sentences focused on the *why*. If the
linked GitHub issue's body already says it well, paraphrase from there.

After the commit, verify the branch is still ahead of main:

```bash
git log main..HEAD --oneline
```

There should be exactly one line. If the squash failed or the branch is no longer
ahead of main, stop and explain -- do not retry blindly.

**When this rule does NOT apply:** subsequent pushes to the same PR after the first
push (e.g. fix-up commits during `/handle-review`). Those should be additive
commits, not force-pushes, so reviewers can see exactly what changed in response to
their feedback. Squash-on-merge happens at `/merge-pr` time.

**User override:** if the user has explicitly said "keep N commits" or "don't
squash" for this specific PR, honor that. The default is still squash for the next
PR.

---

## Step 7 -- Push

Then push:

```bash
git push origin $(git branch --show-current) 2>&1
```

If the branch has no upstream yet, use `-u`:

```bash
git push -u origin $(git branch --show-current) 2>&1
```

Report the push result. If it fails (non-fast-forward, auth error, etc.), stop and
explain -- do not retry automatically.

---

## Step 8 -- PR creation offer

After a successful push, check if a PR already exists:

```bash
gh pr view 2>&1
```

If no PR exists, offer to create one:
"Push succeeded. Create the PR now? (yes/no)"

If yes, determine which issue(s) this branch closes:

1. Parse the branch name for an issue number (e.g. `fix/123-some-desc` or `feat/456-thing`).
2. Scan commit messages for `#N` references.
3. Check `$ARGUMENTS` for explicit issue numbers.

Collect all discovered issue numbers into a list.

### Step 8a -- Resolve PR labels (REQUIRED -- CI check will fail without labels)

There is a required CI check ("Label gate / Require release label") that fails PRs
missing a valid release label. You MUST apply labels at PR creation time.

Fetch labels from each linked issue:

```bash
for n in <issue-numbers>; do
  gh issue view "$n" --json labels --jq '.labels[].name'
done
```

Cross-reference against the valid release labels in `.claude/release.toml`:

```
enhancement, bug, ux, database, documentation, technical-debt, testing,
automation, providers, scanner, rules, images, webhooks, emby, jellyfin, reports
```

Collect all matching labels. If no linked issue has a valid label, infer the best
match from the change type:
- Bug fix -> `bug`
- New feature -> `enhancement`
- UI/template changes -> `ux`
- Provider changes -> `providers`
- Rule engine changes -> `rules`
- Test-only changes -> `testing`
- Dashboard/report changes -> `reports`

Build a `--label` flag string: `--label "enhancement" --label "providers"` etc.

### Step 8b -- Create the PR

The PR body must match the project's PR template so reviewers (CR + humans)
see the expected shape. The template lives at
`.github/pull_request_template.md`. **Always pass `--body`** -- the
interactive prompt that loads the template automatically doesn't work from
a non-tty session, so the agent must generate a body that mirrors the
template's sections.

**Resolve issue numbers first.**
1. Parse the branch name (`fix/123-some-desc` -> `123`).
2. Scan the squashed commit message for `#N` references.
3. Check `$ARGUMENTS` for explicit issue numbers.

Collect into `issue_refs` (e.g. `Closes #123` or `Closes #12\nCloses #34`
if multiple). If none can be inferred, ask the user: "Which issue(s) does
this PR close? (e.g. #123)" -- do not guess.

**Template-aware body assembly.**

If `.github/pull_request_template.md` exists in the repo, read it and
construct `body` by mirroring the template's section structure. For each
section, fill in or pre-mark as follows:

| Section | Action |
|---------|--------|
| `## Summary` | Bullet points from `$ARGUMENTS` if provided, else inferred from the squashed commit message. 1-3 bullets, why-focused, per project convention. |
| `## Linked issue` | Replace the `Closes #` placeholder with the resolved `issue_refs`. Use `Part of #N` for an intermediate slice if explicitly told so; otherwise `Closes #N`. |
| `## Pre-flight checklist` | Pre-mark `[x]` for items this `/prep-pr` run just validated; leave `[ ]` for items requiring user/reviewer attention. See mapping below. |
| `## Test plan` | Pre-mark `[x]` for the canonical gate entries (`go test -race ./...`, `scripts/pre-push-gate.sh`); leave `[ ]` for "Manual UAT steps" and "Reviewer follow-ups" sub-bullets. |

**Pre-flight checklist `[x]` mapping** (current template as of 2026-05-19):

| Item | Mark `[x]` if |
|------|---------------|
| Pre-push gate green locally | Steps 2 + 2b passed (always true here) |
| Code review pass complete | Step 4 ran without unresolved Critical findings |
| Commits squashed | Step 6 produced a single commit (already-one or just squashed) |
| Label(s) set | Step 8a produced a non-empty label list (the `gh pr create` call applies them) |
| Docs label decision made | Step 8a applied either `needs-docs-review` or `docs: not-required` (mark `[x]`); else `[ ]` and let the user pick |
| Screenshot for UI change | Leave `[ ]` -- the agent cannot capture screenshots; user/reviewer fills |
| UAT performed for user-visible changes | Leave `[ ]` -- the agent cannot run UAT; user fills (or N/A for non-UI changes) |
| OpenAPI spec updated if shapes changed | Mark `[x]` if Step 3 reported no spec drift OR the agent applied fixes; else `[ ]` |
| `templ generate` re-run | Mark `[x]` if Step 5 passed OR no `.templ` files in the diff; else `[ ]` |

**Do NOT add** a "Generated with Claude Code" footer in the body. The
project's template intentionally doesn't include it -- humans use the same
template, and the footer is repo-meta noise (per `feedback_use_pr_template`).

**Fallback (no template file).** If `.github/pull_request_template.md`
does not exist (older repos, non-stillwater consumers of this skill), use
the legacy heredoc form below:

```bash
gh pr create --title "<branch-description>" \
  --label "<label1>" --label "<label2>" \
  --body "$(cat <<'EOF'
## Summary
<bullet points from $ARGUMENTS or inferred from commit message>

## Linked issue
<issue_refs from the resolver above>

## Test plan
- [ ] `go test -race ./...` passes
- [ ] OpenAPI spec verified against implementation
- [ ] Local review toolkit passed
EOF
)"
```

**If a PR already exists**, print its URL and say "PR already open -- bot
reviewers will review the push automatically." Do not attempt to re-create.

**Note:** The first push is the cleanest moment for review -- every issue
caught before the first push saves a review round. This is why Steps 2-5
gate so aggressively, and why the Pre-flight checklist tells reviewers
explicitly which gates already passed.
