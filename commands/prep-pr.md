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
if git rev-parse --verify -q origin/main >/dev/null 2>&1; then base_ref=origin/main; else base_ref=main; fi
base=$(git merge-base "$base_ref" HEAD)
git branch --show-current
git log "$base_ref"..HEAD --oneline
git diff "$base"..HEAD --stat
```

Report:
- Current branch name
- Number of commits ahead of main
- Files changed (summary)

If on `main`, stop immediately: "You are on main. Create a feature branch first."

---

## Step 1b -- Hand-written PR size gate

Generated and lock files (`*.sum`, `*.lock`, `*.snap`, compiled caches)
inflate `git diff --stat` and disguise PR risk. The relevant signal is
**hand-written LOC** -- what reviewers actually have to read, and what
correlates with CR round count (see `feedback_pr_size_handwritten_loc.md`).

The exclude list below is generic; add the target repo's own generated-file
globs as detected (e.g. `*_templ.go` for a templ repo, `*.pb.go` for a
protobuf repo). All such examples are illustrative of what detection would
find -- do not assume any specific stack.

Compute hand-written diff size:

```bash
if git rev-parse --verify -q origin/main >/dev/null 2>&1; then base_ref=origin/main; else base_ref=main; fi
base=$(git merge-base "$base_ref" HEAD)
git diff --stat "$base"..HEAD -- ':!*.sum' ':!*.lock' ':!*.snap' \
  ':!*.pyc' ':!__pycache__/*'
```

Capture totals (additions + deletions) and file count from the
`N files changed, A insertions(+), D deletions(-)` summary line.

**Thresholds:**
- >800 hand-written LOC (additions + deletions), OR
- >10 hand-written files

If either is exceeded, **stop** and say:

> "This PR has `<N>` hand-written LOC across `<F>` files, exceeding the
> sustainable-PR-size threshold (800 LOC / 10 files). Large PRs
> correlate with multi-round CR churn (a single oversized PR routinely
> costs several extra review rounds).
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

## Step 2 -- Tests and gates (delegate to gate-runner)

This command ships in a plugin installed into arbitrary repos, so it must run
the **target repo's own gates** rather than hardcode any one stack. Gate
detection is no longer re-implemented in this prose: it is owned by the bundled
`gate-runner.py`, which reads `.gates.toml` when present and otherwise falls
back through a fail-open detection chain (umbrella script -> the `## Gates`
block in `CLAUDE.md` -> language-agnostic basics -> warn-and-proceed). Delegate
to it:

```bash
# Prefer the repo-local copy (dev / --plugin-dir installs); else the deployed
# copy at the stable path. The runner finds the repo root itself and reads
# .gates.toml or falls back; it exits non-zero on the first required-gate
# failure, 0 when everything passed/skipped/fell open.
if [ -f scripts/gate-runner.py ]; then
  python3 scripts/gate-runner.py
else
  python3 ~/.claude/scripts/gate-runner.py
fi
gate_rc=$?
```

`gate-runner.py` reads `.gates.toml` (`[prep_pr]` with either a `gate`
delegate or an ordered `steps` list; see
`skills/orchestrate/templates/gates.toml.md` for the schema). When there is no
`.gates.toml`, it falls back automatically -- so a repo with a `## Gates` block,
a `make gate` target, a `scripts/pre-push-gate.sh`, or just a manifest still
gets the right gates run, and a config-less repo is warned but never hard-
blocked. It prints a per-step `[PASS]` / `[SKIP]` / `[FAIL]` line.

*Illustrative -- what runs in cc-orchestrator (this repo):* its `.gates.toml`
enumerates `shellcheck` on the shell scripts, `ruff check --select F,E741` on
the `.py` files, the guard and steer `--self-test`s, and the `python3
test-*.py` harnesses. Another target repo declares a different set (or relies on
the fallback chain).

If `gate_rc` is non-zero: print the runner's failure output, stop, and say:
"Fix the failing gate before proceeding. Do not push broken code."

If `gate_rc` is 0: note it and continue to Step 2b.

**If a gate run produces a coverage profile** (e.g. a `go test
-coverprofile=<file>` step in the repo's `.gates.toml`), capture that profile
path so Step 2b can reuse it. Repos with no coverage tooling produce no
profile, and Step 2b self-skips accordingly.

---

## Step 2b -- Patch coverage gate (Codecov parity)

This step is a **gate** when a coverage service is active on the repo, and a
**self-skip no-op** when none is. Detect the coverage service first:

```bash
# A coverage service is present if EITHER signal exists:
#   - a codecov.yml (or .codecov.yml) at the repo root, OR
#   - a codecov/* commit-status context on the PR head
#     (e.g. `gh pr checks` lists a "codecov/patch" or "codecov/project" context)
if [ -f codecov.yml ] || [ -f .codecov.yml ]; then
  echo "codecov.yml present -- run the patch-coverage gate below."
else
  echo "No codecov.yml -- the patch-coverage gate self-skips (no coverage service)."
fi
```

When no coverage service is detected (no `codecov.yml` and no `codecov/*`
status context), SKIP this step entirely and continue to Step 3 -- treat patch
coverage as **N/A**, not a failure (the coverage `status:none` self-skip). The
repo's `.gates.toml` can make this explicit with `[merge_pr]`
`coverage_advisory = false`: when that is set (or the runner / `patch-coverage.sh
--coverage-only` reports `{"status":"none"}`), report Coverage as N/A and
continue rather than gating.

When a coverage service IS active: Codecov's patch-coverage check measures the
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
COVER_OUT="${COVER_PROFILE:-/tmp/patch-cover.out}" \
  bash "${CLAUDE_PLUGIN_ROOT}/scripts/patch-coverage.sh"
gate_status=$?
rm -f "${COVER_PROFILE:-/tmp/patch-cover.out}"
```

Point `COVER_PROFILE` at whatever coverage profile the Step 2 gate run
produced (the detected gate block's `-coverprofile` output). If the detected
gates produced no profile -- the repo has no coverage tooling -- this gate
self-skips (see exit code `2` below).

To override for a one-off -- a repo with no `codecov.yml` patch target, or a
deliberately lower bar -- pass `PATCH_COVERAGE_THRESHOLD=<n>` (and/or
`PATCH_COVERAGE_EXCLUDE="<globs>"`), and explain the override in the PR
description. An explicit env var always wins over the `codecov.yml` value.

**Interpret the exit code:**

- `0` -- patch coverage meets the threshold, or there's nothing in scope (the
  script reports "no Go source changes in scope" when the diff has no covered
  source -- treat that as a SKIP, not a failure). Print the script's report and
  continue to Step 3.
- `1` -- patch coverage below threshold. Print the per-file breakdown the
  script emitted, then say:
  > "Patch coverage `<pct>%` is below the `<threshold>%` threshold. Codecov
  > will flag this. Add tests for the uncovered lines, then re-run prep-pr.
  > **STOP** -- do not push. (To override for a one-off, re-run with
  > `PATCH_COVERAGE_THRESHOLD=<lower>` and explain in the PR description.)"
  >
  > Do NOT proceed without the user explicitly overriding.
- `2` -- missing profile or other setup condition. The disposition depends on
  whether a coverage service was detected at the top of this step:
  - **Coverage service active** (codecov.yml or a `codecov/*` status context):
    treat exit 2 as a real configuration error -- surface the script's stderr
    and stop. Do not silently skip a gate the repo actually enforces.
  - **No coverage service detected**, or the Step 2 gates produced no coverage
    profile (the repo has no coverage tooling -- e.g. a stdlib-only shell/Python
    repo): treat exit 2 as a SKIP. Print "Patch-coverage gate skipped (no
    coverage profile / no coverage service)." and continue to Step 3.

**If the script is missing** (`${CLAUDE_PLUGIN_ROOT}/scripts/patch-coverage.sh` not found),
treat as a `2` configuration error: stop and tell the user to reinstall or update
the plugin so the bundled, versioned helper is present at that path (do not source
it from anywhere else -- an out-of-band copy risks drifting from the running
release). Do NOT fall back to the old 0%-function check -- that gate is what let
this regress in the first place.

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

## Step 3 -- OpenAPI consistency check (self-skipping)

**Self-skip when there is no OpenAPI surface.** This step applies only to repos
that declare an HTTP API with an OpenAPI spec. Detect the signal first:

```bash
if [ -d internal/api ]; then
  echo "internal/api/ present -- run the OpenAPI consistency check below."
else
  echo "No internal/api/ -- OpenAPI consistency check skipped."
fi
```

If `internal/api/` is absent, SKIP this step and continue to Step 3b. The
commands below are illustrative of the check a repo with an OpenAPI surface
would run; adapt the path to the target repo's API package as detected.

Run the AST-based consistency test first:

```bash
go test -count=1 -run TestOpenAPIConsistency -v ./internal/api/
```

If it fails, fix the reported spec drift before continuing.

Then follow the semantic checks in `.claude/commands/check-openapi.md` against the
PR-wide diff:

```bash
if git rev-parse --verify -q origin/main >/dev/null 2>&1; then base_ref=origin/main; else base_ref=main; fi
base=$(git merge-base "$base_ref" HEAD)
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
if git rev-parse --verify -q origin/main >/dev/null 2>&1; then base_ref=origin/main; else base_ref=main; fi
base=$(git merge-base "$base_ref" HEAD)
git diff "$base"..HEAD | grep -E '^-.*(func|def|class|type|var|const) ' | \
  sed 's/^-//' | grep -oE '[A-Za-z_][A-Za-z0-9_]*'
```

For each old name found, grep the full codebase for remaining references.
Scope the `--include` globs to the target repo's source file types as detected
(below uses this repo's `*.sh`/`*.py`; a Go repo would use `*.go`/`*.templ`,
etc.):

```bash
grep -rn "OldName" --include='*.sh' --include='*.py' .
```

**Flag as CRITICAL** if the old name still appears in code (excluding the diff's `-` lines
and comments). Incomplete renames cause build/compile failures (in compiled
languages) or silent behavior changes (in interpreted ones).

---

## Step 3c -- Raw error leak check (self-skipping)

**Self-skip when there are no API handler files.** This check targets HTTP
handler response paths. Detect the signal first:

```bash
if compgen -G 'internal/api/handlers_*.go' >/dev/null 2>&1; then
  echo "internal/api/handlers_*.go present -- run the raw-error-leak check below."
else
  echo "No internal/api/handlers_*.go -- raw-error-leak check skipped."
fi
```

If no `internal/api/handlers_*.go` files exist, SKIP this step and continue to
Step 3d. The check below is illustrative of a Go-handler repo; adapt the paths
and patterns to the target repo's handler layer as detected.

Check for raw internal error messages leaking to clients:

```bash
if git rev-parse --verify -q origin/main >/dev/null 2>&1; then base_ref=origin/main; else base_ref=main; fi
base=$(git merge-base "$base_ref" HEAD)
git diff "$base"..HEAD -- internal/api/handlers_*.go | grep '^+' | \
  grep -E 'err\.(Error|String)\(\)|fmt\.(Sprintf|Errorf).*err[^o]' | \
  grep -v 'slog\.\|logger\.\|log\.'
```

**Flag as IMPORTANT** if any handler response path includes raw `err.Error()` text.
Client-visible messages must be generic; the full error belongs in a server-side `slog` call.

---

## Step 3d -- Axis/state vocabulary drift check (self-skipping)

**Self-skip when there is no i18n + template surface.** This check targets
repos that pair backend state with user-facing localized copy. Detect the
signal first:

```bash
if [ -d internal/i18n ] && [ -d web/templates ]; then
  echo "internal/i18n/ + web/templates/ present -- run the vocabulary-drift check below."
else
  echo "No internal/i18n/ + web/templates/ -- vocabulary-drift check skipped."
fi
```

If either `internal/i18n/` or `web/templates/` is absent, SKIP this step and
continue to Step 3e. The grep patterns below are illustrative of a Go + templ +
i18n repo; adapt them to the target repo's localization surface as detected.

When a PR extends a multi-state system with a new axis, state, or mode, backend
code is usually updated while user-facing copy (i18n keys, inline templ
strings, templ code comments, hardcoded English in helper functions) silently
retains the pre-expansion vocabulary. CodeRabbit flags these drift sites one
or two per review round, costing multiple review cycles. See
`feedback_axis_vocabulary_sweep.md` in memory for the failure-mode background.

Detect candidate expansions in the diff:

```bash
if git rev-parse --verify -q origin/main >/dev/null 2>&1; then base_ref=origin/main; else base_ref=main; fi
base=$(git merge-base "$base_ref" HEAD)

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

## Step 3e -- Vulnerability scan (self-skipping)

**Self-skip when there is no Go module.** This step runs Go's `govulncheck`.
Detect the signal first:

```bash
if [ -f go.mod ]; then
  echo "go.mod present -- run the vulnerability scan below."
else
  echo "No go.mod -- govulncheck step skipped (run the target repo's own dependency-audit gate, if any)."
fi
```

If `go.mod` is absent, SKIP this Go-specific scan. If the target repo declares
its own dependency-audit gate (e.g. `pip-audit`, `npm audit`, `cargo audit`),
that belongs in the Step 2 detected gate block, not here. Continue to Step 4a.

When `go.mod` is present, run the vulnerability scan that mirrors the Security
CI job. This is a **gate**. `make vulncheck` wraps `go run
golang.org/x/vuln/cmd/govulncheck@v1.1.4 ./...`, the same pinned version CI
installs, so a local pass means CI's govulncheck will pass too.

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

## Step 4a -- Local hostile review

Run an adversarial pre-push review of the diff. Dispatch a hostile-reviewer
subagent following the `engage-ralph-loop.md` brief (the adversarial-critic
loop bundled with this plugin), pointed at the branch diff. The reviewer
reproduces every empirical claim (runs it, never static-greps), pushes back on
holes / over-reach / mis-scoping, and runs a least-privilege check that the
change never weakens a security floor or broadens an allow-list.

If the target repo also has a local review toolkit installed (e.g. a
`pr-review-toolkit:review-pr` skill), running it here is a useful complement;
treat its absence as a no-op, not a failure.

If any Critical findings: stop. Say "Fix all critical issues before pushing."

If Important findings: present them and ask:
"There are important (non-blocking) findings. Fix them now, or proceed anyway? (fix/proceed)"

Wait for the user's answer before continuing.

---

## Step 5 -- Generated-file / lockstep check (self-skipping)

Detect which generated-file invariant the target repo has, and check only that
one. Three branches; if no signal is present, skip the step.

**Branch A -- templ-generated code** (signal: `*.templ` files exist). Check
that touched `*.templ` files regenerated their `*_templ.go` outputs:

```bash
if git rev-parse --verify -q origin/main >/dev/null 2>&1; then base_ref=origin/main; else base_ref=main; fi
base=$(git merge-base "$base_ref" HEAD)
templ_changed=$(git diff --name-only "$base"..HEAD -- '*.templ')
generated_changed=$(git diff --name-only "$base"..HEAD -- '*_templ.go')

if [ -n "$templ_changed" ] && [ -z "$generated_changed" ]; then
  echo "ERROR: .templ files changed but *_templ.go files did not."
  echo "Run 'templ generate' and stage the generated files before pushing."
  exit 1
fi
```

**Branch B -- version lockstep** (signal: `skills/orchestrate/SKILL.md` and
`.claude-plugin/plugin.json` both exist -- the generated-file equivalent for
this repo). The plugin manifest version must move in lockstep with the SKILL.md
`**Version**` line:

```bash
if [ -f skills/orchestrate/SKILL.md ] && [ -f .claude-plugin/plugin.json ]; then
  python3 test-version-lockstep.py
fi
```

If the lockstep harness fails (the two versions diverged), stop: bump both to
the same value before pushing.

**Branch C -- no signal.** If neither `*.templ` files nor the
SKILL.md/plugin.json pair is present, the repo has no generated-file invariant
this step knows about -- print "No generated-file invariant detected -- step
skipped." and continue to Step 6. (Add the target repo's own generated-file
check here as detected, e.g. `*.pb.go` from protobuf, an OpenAPI client, etc.)

If any branch's check exits with an error, stop and show the message.

---

## Step 6 -- Squash to a single commit

Squashing to one commit at first push makes reviewers (CodeRabbit and humans) read
the full changeset at once instead of discovering issues commit-by-commit, which
costs review rounds. Squashing is therefore the recommended default -- but it is
history-shaping, not a gate, and the maintainer may intentionally keep distinct
per-logical-unit commits on a multi-part PR. So it is the maintainer's call per PR:
**prompt rather than squash unconditionally.** (Final `main` history is one commit
regardless -- `/merge-pr` squash-merges -- so keeping commits here costs nothing at
merge time; it only changes how the first push reads in review.)

Count commits ahead of main:

```bash
if git rev-parse --verify -q origin/main >/dev/null 2>&1; then base_ref=origin/main; else base_ref=main; fi
git log "$base_ref"..HEAD --oneline
```

If there is already only one commit, say: "Already a single commit -- no squash needed."
and continue to Step 7.

If there is more than one commit, ASK before squashing -- do not squash
unconditionally. Prompt: "Branch has `<N>` commits ahead of main. Squash to one for
the first push (recommended -- reviewers read the full changeset at once)? (squash /
keep)". The recommendation defaults to squash; honor "keep" to push the commits
as-is and continue to Step 7. A standing instruction for this PR ("keep N commits" /
"don't squash") is honored without re-prompting (see User override below).

On "squash", do it non-interactively (no editor). Use a soft reset to the merge-base,
then a single new commit:

```bash
if git rev-parse --verify -q origin/main >/dev/null 2>&1; then base_ref=origin/main; else base_ref=main; fi
base=$(git merge-base "$base_ref" HEAD)
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
if git rev-parse --verify -q origin/main >/dev/null 2>&1; then base_ref=origin/main; else base_ref=main; fi
git log "$base_ref"..HEAD --oneline
```

There should be exactly one line. If the squash failed or the branch is no longer
ahead of main, stop and explain -- do not retry blindly.

**When this rule does NOT apply:** subsequent pushes to the same PR after the first
push (e.g. fix-up commits during `/handle-review`). Those should be additive
commits, not force-pushes, so reviewers can see exactly what changed in response to
their feedback. Squash-on-merge happens at `/merge-pr` time.

**Maintainer control:** the squash is opt-in per PR via the prompt above. A standing
"keep N commits" / "don't squash" instruction for this specific PR is honored without
re-prompting. The recommendation defaults to squash but is never forced; the default
recommendation still applies to the next PR.

---

## Step 7 -- Push

Then push via the safe-push wrapper bundled with this plugin. It always pushes
with `-u origin <branch>` and verifies the remote ref actually moved (guarding
the pipe-swallow silent-failure mode), so the "no upstream yet" case needs no
separate command:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/safe-push.sh" "$(git branch --show-current)"
```

Report the push result. If it fails (non-fast-forward, auth error, the remote
ref did not move, etc.), stop and explain -- do not retry automatically.

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

### Step 8a -- Resolve PR labels (if the repo enforces them)

Some repos gate PRs on a required label (e.g. a "Require release label" CI
check). Detect whether the target repo has such a check before forcing labels:
inspect its label set and required checks rather than assuming a fixed
vocabulary. If the repo has no label gate, labels are optional polish.

Fetch labels from each linked issue and reuse the matching ones:

```bash
for n in <issue-numbers>; do
  gh issue view "$n" --json labels --jq '.labels[].name'
done
```

Cross-reference against the target repo's own label vocabulary (list it with
`gh label list`); do not assume any fixed set. If no linked issue carries a
relevant label, infer the best match from the change type (a bug fix -> a
`bug`-style label, a new feature -> an `enhancement`-style label, and so on,
using whatever names the repo actually defines).

Build a `--label` flag string from the matched names, e.g.
`--label "enhancement"`.

**Mechanical / docs-only / config-only PRs:** consider suggesting the
`norabbit` label to the maintainer (it is in the org CodeRabbit denylist, so it
skips an unnecessary review). Suggest only -- never apply it without consent.

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

The exact section names vary by repo; match whatever the detected template
declares. The mapping below reflects this repo's template
(`.github/pull_request_template.md`) as an illustrative example:

| Section | Action |
|---------|--------|
| `## Summary` | Bullet points from `$ARGUMENTS` if provided, else inferred from the squashed commit message. 1-3 bullets, why-focused. Note any behavior change to the guard, resource allocator, setup, or skill/charters. |
| `## Linked issue` | Replace the `Closes #` placeholder with the resolved `issue_refs`. Use `Part of #N` for an intermediate slice if explicitly told so; otherwise `Closes #N`. |
| `## Gates (run locally; CI enforces them ...)` | Pre-mark `[x]` for each gate command the detected Step 2 gate block actually ran and passed; leave `[ ]` for any the run did not cover. |
| `## Security-floor changes` | Delete this section if the diff does not touch the guard/floor. If it does, pre-mark the TDD / adversarial-critic / no-trigger-substring items per what this run validated. |
| `## Test plan` | Pre-mark `[x]` for the gate entries this run exercised; leave `[ ]` for "Reviewer follow-ups" and any manual UAT sub-bullets. |

**Gates checkbox `[x]` mapping** (this repo's template, illustrative):

| Item | Mark `[x]` if |
|------|---------------|
| Gate command (shellcheck / ruff / self-tests / `test-*.py`) | That command ran in the detected Step 2 gate block and passed |
| Security-floor TDD item | The diff touches the guard AND harness cases were added/updated before the change; else leave `[ ]` (or delete the whole section if the guard is untouched) |
| Adversarial-critic pass | Step 4a's hostile review ran and converged; else `[ ]` |
| No trigger substrings on Bash lines | Confirmed no `git push`/`main`/`gh pr merge`/etc. payloads on command lines (payloads stay in fixtures); else `[ ]` |

For a repo with a different template (UI screenshots, UAT, OpenAPI, `templ
generate` rows, etc.), map each row to the corresponding self-skipping step
above: mark `[x]` when that step passed or self-skipped as not-applicable, leave
`[ ]` when it needs user/reviewer attention (screenshots and manual UAT always
stay `[ ]` -- the agent cannot capture or perform them).

**Do NOT add** a "Generated with Claude Code" footer in the body unless the
target repo's template includes one. Humans use the same template, and an
unsolicited footer is repo-meta noise (per `feedback_use_pr_template`).

**Fallback (no template file).** If `.github/pull_request_template.md`
does not exist (older repos, other consumers of this skill), write the body to
a file and pass it with `--body-file` (a file, not a Bash heredoc -- the
security floor greps command lines, and a `--body-file` keeps any trigger words
off the command line). Fill the Gates list from the detected Step 2 gate block
rather than any hardcoded runner:

```bash
title="<conventional-commit-title>"
body_file="$(mktemp)"
{
  printf '## Summary\n'
  printf -- '- <bullets from $ARGUMENTS or inferred from the squashed commit>\n\n'
  printf '## Linked issue\n%s\n\n' "$issue_refs"
  printf '## Gates\n'
  printf -- '- [x] <each gate command the detected Step 2 block ran and passed>\n'
} > "$body_file"
gh pr create --title "$title" --label "$label" --body-file "$body_file"
rm -f "$body_file"
```

List the Gates as whatever detection found (e.g. for this repo: `shellcheck`,
`ruff check --select F,E741`, the guard/steer self-tests, the `test-*.py`
harnesses); for a Go repo it would be `go test ./...`, govulncheck, etc. The
examples are illustrative of a detected gate set, not a hardcoded assumption.

**If a PR already exists**, print its URL and say "PR already open -- bot
reviewers will review the push automatically." Do not attempt to re-create.

**Note:** The first push is the cleanest moment for review -- every issue
caught before the first push saves a review round. This is why Steps 2-5
gate so aggressively, and why the Gates checklist tells reviewers
explicitly which gates already passed.
