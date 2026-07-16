# `.gates.toml` schema

`.gates.toml` is a declarative, language-agnostic gate definition that lives at a
repo's root. A single runner (`scripts/gate-runner.py`) reads it and runs the
gates, and the PR-lifecycle commands (`/prep-pr`, `/handle-review`,
`/review-stack`) and the optional `pre-push-hook.sh` all delegate to that one
runner instead of re-implementing gate detection in prose. One config, one
runner, one source of truth for "what the gates are" in any repo that consumes
this plugin.

`.gates.toml` is TRUSTED repo configuration, on the same footing as a `Makefile`
or a CI workflow file: its commands are run by the same person who can already
run arbitrary shell in the repo. The runner introduces no `eval` of dynamic
strings, no privilege escalation, and does NOT weaken the deterministic floor or
the advisory `# prep-pr-ok` gate (see DESIGN-deterministic-floor.md). It is not a
sandbox; it is a declarative front-end over commands the repo author wrote.

When `.gates.toml` is ABSENT, the runner falls back through a fail-open detection
chain (umbrella script, then the `## Gates` block in `CLAUDE.md`, then
language-agnostic basics, then warn-and-proceed) so a config-less repo is never
hard-blocked. That fallback is the runner's job, NOT this file's; this file
documents only the config when it is present.

---

## `[prep_pr]` section

The gates run before a push / PR-open. Exactly ONE of two mutually exclusive
forms describes them:

### Form A -- delegate (`gate`)

A single umbrella command. Use this when the repo already has one canonical gate
target (a `make gate`, a `scripts/pre-push-gate.sh`, a CI-parity wrapper) and you
want `.gates.toml` to just point at it.

- `gate` (string, required for Form A): the umbrella command. Run as one
  subprocess via the shell (trusted-repo-config semantics, like a `Makefile`
  recipe). Non-zero exit fails the gate.

`gate` and `steps` are MUTUALLY EXCLUSIVE -- a `[prep_pr]` table sets one or the
other, never both. Setting both is a config error and the runner refuses it.

### Form B -- enumerate (`steps`)

An ordered array of step tables, each its own command, run in listing order.
Use this when the repo's gates are a list of independent commands (the
cc-orchestrator shape: shellcheck, ruff, self-tests, per-harness `python3
test-*.py`) and you want per-step PASS/SKIP/FAIL reporting and per-step skip
predicates.

- `steps` (array of tables, required for Form B): the ordered step list.

Per-step keys:

| Key              | Type    | Default | Meaning |
|------------------|---------|---------|---------|
| `name`           | string  | (req.)  | Human label printed in the per-step `[PASS]` / `[SKIP]` / `[FAIL]` line. |
| `run`            | string  | (req.)  | The command, run as one subprocess via the shell (trusted-config semantics). |
| `required`       | bool    | `true`  | `true` (or omitted): a non-zero exit is a HARD failure -- the runner stops and exits non-zero. `false`: a non-zero exit is a SOFT failure -- the runner prints `[FAIL]` (warn), keeps going, and does NOT fail the overall run on this step alone. |
| `skip_if_absent` | string  | (none)  | A binary / tool name. If it is NOT found on `PATH` (`shutil.which`), the step is SKIPPED (`[SKIP] <name>: <tool> not on PATH`), not failed. For an optional linter/tool whose absence should not block. |
| `skip_if`        | string  | (none)  | A glob (evaluated recursively from the repo root). If the glob matches ZERO files, the step is SKIPPED (`[SKIP] <name>: no files match <glob>`). Absence-based: skip when there is nothing to check (e.g. skip a UI lint when `web/**` matches nothing). |
| `pure`           | bool    | `false` | Opt into pure-oracle memoization (see below). Mark `true` ONLY for a step whose result is a pure function of the COMMITTED TREE -- a static analysis / self-contained test suite over tracked files that reads nothing else. It is an explicit allowlist: a step is memoizable ONLY if it declares `pure = true`. Default (`false`/omitted) = never memoized. |

Predicate evaluation order: `skip_if_absent` and `skip_if` are both evaluated
BEFORE the command runs. If either triggers a skip, `run` is not executed.

### Pure-oracle memoization (`pure = true` + `--memoize-dir`)

`gate-runner.py --memoize-dir <dir>` (OFF by default; absent = ZERO behavior
change) memoizes the PASS of `pure = true` steps, keyed on the committed tree, so
a repeated gate run over an unchanged tree can SKIP re-running an expensive pure
oracle instead of paying for it again.

Rules (deliberately conservative -- a memo bug can only cause a re-run, never a
false pass):

- A step is memoized ONLY when `--memoize-dir` is set AND the step declares
  `pure = true` AND the committed tree resolves (`git rev-parse HEAD^{tree}`) AND
  a LIVE clean-worktree check passes (`git status --porcelain` is EMPTY -- no
  staged, unstaged, OR untracked changes).
- **Untracked files count as DIRTY** (they defeat memoization). This is a
  safety property: a `pure` step that discovers files by glob (a linter or test
  over a directory) reads untracked files as INPUTS, and untracked files are the
  normal state of in-progress work; a diff-only clean check would memo-pass while
  an untracked input that fails the step sits on disk. Because untracked files
  block memoization, **the `--memoize-dir` MUST live OUTSIDE the worktree or be
  gitignored** (`--porcelain` hides gitignored files) -- an in-repo cache dir
  would itself show as untracked and nothing would ever be memoizable.
- Cache key = `sha256(tree \0 name \0 run)`; the cache file holds the literal
  `pass`. A hit logs `[MEMO] <name>: cached pass (tree <sha7>)` and counts the
  step as passed WITHOUT running it.
- PASS ONLY is memoized. A FAILING step is never cached and always re-runs, so
  the user always sees a real failure's output.
- FAIL-OPEN everywhere: any git error, a dirty/untracked tree, a non-pure step,
  or `--memoize-dir` absent => the step runs normally with no cache read/write.

**EXCLUSIONS -- never mark these `pure = true`:** git-diff-based steps (they read
the index/worktree), `ship-gate-preflight`, and `pr-unreplied-comments` (they read
LIVE GitHub). All of these can flip at a constant HEAD, so a tree-keyed cache
would be unsound; leave them non-pure so they always re-run.

**Caveat:** memoization keys on the committed TREE only and assumes a fixed
toolchain within the memo window. A `shellcheck` / `ruff` / `python` version bump
at a constant tree is NOT detected -- clear `<dir>` (or leave the toolchain fixed)
across a memo window.

---

## `[merge_pr]` section

Optional. Tunes merge-time behavior for the lifecycle commands.

- `coverage_advisory` (bool, default `true`): when `false`, the consuming
  command treats patch coverage as ADVISORY / N/A rather than a blocking gate
  -- the explicit config equivalent of "this repo has no coverage service"
  (the coverage `status:none` self-skip). When `true` or omitted, normal
  patch-coverage gating applies if a coverage service is detected.

---

## Example -- Form A (delegate; stillwater-style)

```toml
# A repo with one canonical umbrella gate target.
[prep_pr]
# Single command; non-zero exit fails the gate. Mutually exclusive with `steps`.
gate = "make gate"

[merge_pr]
# Patch coverage IS enforced for this repo (a coverage service is active).
coverage_advisory = true
```

## Example -- Form B (enumerate; cc-orchestrator-style)

```toml
# A repo whose gates are an ordered list of independent commands.
[prep_pr]
# `steps` is mutually exclusive with `gate`. Run in listing order; the runner
# stops at the first HARD failure (a `required` step that exits non-zero).

  [[prep_pr.steps]]
  name = "shellcheck"
  run = "shellcheck scripts/foo.sh scripts/bar.sh"
  # `skip_if_absent`: if shellcheck is not installed locally, SKIP (do not fail)
  # rather than block a contributor who has not installed the optional linter.
  skip_if_absent = "shellcheck"

  [[prep_pr.steps]]
  name = "ruff"
  run = "ruff check --select F,E741 scripts/*.py test-*.py"
  skip_if_absent = "ruff"

  [[prep_pr.steps]]
  name = "guard-self-test"
  run = "./scripts/orchestrate-guard.sh --self-test"

  [[prep_pr.steps]]
  name = "harness-foo"
  run = "python3 test-foo.py"

  [[prep_pr.steps]]
  name = "ui-lint"
  run = "npm run lint:ui"
  # `skip_if`: only run when the UI surface exists; SKIP when web/** is empty.
  skip_if = "web/**"
  # `required = false`: a soft, advisory check -- a non-zero exit warns but does
  # not fail the overall gate.
  required = false

  [[prep_pr.steps]]
  name = "base-freshness"
  # OPT-IN (#282), ADVISORY: is this branch still current with its base? The base is
  # ALWAYS explicit -- base-freshness.sh never infers or hard-codes `main`, so each
  # repo (and each backport lane) names its OWN base here. Copy this step only if you
  # want the pre-push warning; it is deliberately NOT a default step.
  run = "bash scripts/base-freshness.sh main"
  # `required = false` is LOAD-BEARING: base-freshness exits 1 on a definitively-BEHIND
  # branch, and being behind base must WARN, never hard-fail the gate (the routing is the
  # lead's -- see SKILL.md BEHIND-BASE ROUTING). fresh AND unknown both exit 0.
  required = false
  # `skip_if`: nothing to check when the script is not installed in this repo.
  skip_if = "scripts/base-freshness.sh"

[merge_pr]
# This repo has no coverage service; patch coverage is advisory / N/A.
coverage_advisory = false
```
