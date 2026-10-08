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
| `exclusive`      | bool    | `false` | Parallel runs only (see `jobs` below): the step starts only when no other step is in flight, and nothing else starts until it finishes. For a timing-sensitive or contention-sensitive step. A non-bool value exits 2. |
| `pure`           | bool    | `false` | Opt into pure-oracle memoization (see below). Mark `true` ONLY for a step whose result is a pure function of the COMMITTED TREE -- a static analysis / self-contained test suite over tracked files that reads nothing else. It is an explicit allowlist: a step is memoizable ONLY if it declares `pure = true`. Default (`false`/omitted) = never memoized. |

Predicate evaluation order: `skip_if_absent` and `skip_if` are both evaluated
BEFORE the command runs. If either triggers a skip, `run` is not executed.

### Parallel steps (`jobs`, #501)

`[prep_pr] jobs = <int>` (default 1) or `gate-runner.py --jobs N` (the CLI wins;
`--jobs 1` forces serial) runs up to N Form B steps at once. Absent or 1 keeps
the serial path, byte-identical to a runner without this feature. Anything but a
positive integer exits 2. Form A and the fallback chain ignore it and stay
serial.

**Before enabling `jobs`, AUDIT EVERY STEP FOR SHARED STATE.** Steps that were
written to run alone can collide when run together: a shared `HOME` or config
dir, fixed temp paths, a fixed port, a lock file, or anything that writes to the
real repo or the real `~/.claude`. Fix the collision or mark the step
`exclusive = true`. That is why `jobs` is opt-in.

With `jobs` > 1:

- All steps are validated before anything launches.
- Steps launch in declaration order. A step blocked on capacity or on an
  `exclusive` barrier holds back every later step (no reordering).
- Skip predicates and memo lookups run in the runner before launch, once per
  step (a step held back at the head is not re-checked); a memo
  entry is written on PASS only.
- Each step gets `stdin` from `/dev/null`, and its stdout and stderr go, merged,
  to a per-step temp file. A finished step prints as one block (its output, then
  its usual `[PASS]`/`[FAIL]` line), strictly in declaration order: step k
  prints only after steps 0..k-1.
- The first REQUIRED failure stops new launches, sends SIGTERM to every in-flight
  step's process group, then SIGKILL after a 3s grace. Cancelled steps print a
  `[FAIL] <name> (cancelled, ...)` line, their partial output is discarded, and
  they are recorded as failures (`"cancelled": true`). Then the runner logs
  `HARD failure at <name> -- stopping.` and exits 1. A `required = false` failure
  still warns and continues. Ctrl-C (or SIGTERM or SIGHUP to the runner) kills
  every group; further INT/TERM/HUP are ignored while that cleanup runs, so a
  second Ctrl-C cannot cut it short. Steps run in their own sessions, so SIGKILL
  to the runner (which cannot be handled) orphans every in-flight group.
- The runner sweeps (SIGTERM, then SIGKILL) the process group of every step it
  launched on every exit path, all-pass included, so a step's stray background
  job (`sleep 30 &`) never outlives the run.
- The receipt is written only after every child has exited. `steps[]` stays in
  declaration order; a fail receipt may include steps declared after the
  failing one.

### Skipping named steps (`--skip <name>`)

`gate-runner.py --skip <name>` (repeatable; OFF by default, absent = ZERO behavior
change) skips the named Form B steps on both the serial and parallel paths, each
printing `[SKIP] <name>: --skip`. It exists so a CI workflow can run the gate's
steps through the runner while leaving a step to a dedicated CI step (this repo's
CI skips `shellcheck` and `ruff`, which it runs from a digest-pinned image). It
fails closed on doubt, exiting 2 before running anything when a name matches no
step (a stale caller after a rename), when a name is empty, when two steps share
a name (including a derived `step-<i>` that collides with an explicit name, since
one `--skip` would then match both), under Form A or the
fallback chain (nothing to skip by name), and beside `--receipt` (a receipt must
attest the whole gate, never a subset).

### Sharding the step list (`--shard K/N`)

`gate-runner.py --shard K/N` (OFF by default; absent = ZERO behavior change) runs
only the Form B steps in shard K of N, so a CI workflow can spread one step list
over N runners (this repo's macOS leg runs `--shard 1/3`, `2/3`, `3/3`). A step
belongs to shard `1 + sha256(name) mod N`: a stable digest of the step NAME, not
Python's per-process `hash()`, so the N shards partition the list and the
assignment is identical across runs and machines. It combines with `--jobs`
(each shard runs its steps in parallel) and with `--skip`: shard membership is
computed from the FULL declared list by name alone, so adding a `--skip` never
moves another step between shards, and `--skip` is still validated against the
full list (a skipped step prints `[SKIP] <name>: --skip` in its own shard only).
It exits 2 before running anything on a malformed value (anything but `K/N`
with `1 <= K <= N`), when two steps share a name, under Form A or the fallback
chain, and beside `--receipt`. The assignment is by name, not by cost, so
shards can differ in step count and wall time.

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

### Gate receipt (`--receipt <path>`)

`gate-runner.py --receipt <path>` (OFF by default) writes a `gate-receipt/v1`
(shape in `schemas.md`) as a BYPRODUCT of the run; a receipt problem never
changes the gate exit code.

- `result = "pass"` needs ALL of: exit code 0, a clean worktree BEFORE the first
  step and AFTER the last, and an unchanged `HEAD^{tree}` across the run (the
  receipt file and its `.tmp.<pid>` leftover are not dirt; a git error is doubt,
  so no pass). Anything else is `result = "fail"` with a `reason`, and an older
  receipt at the path is unlinked first (#481).
- When HEAD/tree cannot resolve (git missing, not a repo, no commit) no receipt
  is written AND the runner attempts to unlink any older receipt at the path, so
  a stale pass does not read as this run's verdict (#497). The unlink is
  best-effort: if it fails (for example a read-only directory) the runner WARNs
  and says so, the older receipt remains, and the gate exit code is unchanged.
- **What a pass does NOT guarantee (accepted window, #497):** the clean/unchanged
  checks are point-in-time reads at the two ends of the run. Content that existed
  only MID-run (a file edited and then reverted while the gate ran) is tested by
  the steps but never bound by the receipt, and the run still passes. Closing
  this means running the gate in an isolated export of `HEAD^{tree}`; that was
  declined because it needs a concurrent editor (outside the honest-actor threat
  model), costs a full extra checkout per run, and breaks gates that read
  untracked config or caches from the live worktree.

---

## `[merge_pr]` section

Optional. Tunes merge-time behavior for the lifecycle commands.

- `coverage_advisory` (bool, default `true`): when `false`, the consuming
  command treats patch coverage as ADVISORY / N/A rather than a blocking gate
  -- the explicit config equivalent of "this repo has no coverage service"
  (the coverage `status:none` self-skip). When `true` or omitted, normal
  patch-coverage gating applies if a coverage service is detected.

---

## `[steer]` section

Optional. Read ONLY by the advisory steering hook (`orchestrate-steer.sh`),
never by `gate-runner.py`; it cannot change a gate's verdict.

- `expensive_profile_env` (array of strings, or one string; default none): the
  env var name(s) that select this repo's EXPENSIVE gate profile (a race / full
  / integration run). When declared, steer rule 7 (#343) nudges on a command
  that sets one of them to a value other than empty or `0` (bare, or as the
  whole quoted word: `VAR="0"` and `VAR=''` are off) (`VAR=1 cmd`,
  `env VAR=1 cmd`, or an earlier `export VAR=1`) together with a recognized
  gate or upload at command position: `gate-runner.py`, `pre-push-hook.sh`,
  `safe-push.sh`, `git push`. The nudge points at the repo's fast compile/vet
  step first; an upload at a HEAD whose `/prep-pr` receipt already passed is
  named as a double spend (the pre-push hook would re-run the same gate).
  Undeclared (the default) means the rule is silent. Names that are not shell
  identifiers are ignored. Read from the `.gates.toml` at the nearest ancestor
  holding `.git`, on each matching command (nothing is cached). Reading it
  needs python3 >= 3.11 (`tomllib`, the same floor as `gate-runner.py`);
  without it the rule is silent.

```toml
[steer]
expensive_profile_env = ["SW_GATE_FULL"]
```

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
  # repo (and each backport lane) names its OWN base here. The `<BASE>` below is a
  # PLACEHOLDER: you MUST substitute it with this repo/lane's own base branch before
  # use -- copying it verbatim checks a ref named "<BASE>" and reports unknown. Copy
  # this step only if you want the pre-push warning; it is deliberately NOT a default step.
  run = "bash scripts/base-freshness.sh <BASE>"
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
