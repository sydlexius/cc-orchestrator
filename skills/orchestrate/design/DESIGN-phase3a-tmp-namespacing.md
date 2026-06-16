# DESIGN - P3-A sub-item: team-namespace orchestrate /tmp artifacts

Date: 2026-06-05 (PDT). Status: SHIPPED (implemented; was APPROVED design, pre-implementation).
Parent: `ROADMAP-phase3.md` P3-A. Companions: `DESIGN-phase3a-marker-refcounting.md`
(sibling P3-A piece, done), `DESIGN-phase2-setup.md` (the setup script this extends).
Canonical code: `~/Developer/cc-orchestrator/orchestrate-setup.py` (+ harness
`test-orchestrate-setup.py`); templates/charters in
`~/.claude/skills/orchestrate/templates/`.

## Problem

`scaffold_artifacts()` namespaces only the stack (`ARTIFACTS/<team>-stack.json`). The
triage dir (`ARTIFACTS/pr-triage`) and the shipper brief (`ARTIFACTS/pr-shipper-brief.md`)
are SHARED flat paths. Two parallel orchestrate teams clobber each other's triage output
and brief. (Marker refcounting, the sibling P3-A piece, already isolates the FLOOR marker;
this isolates the working ARTIFACTS.)

## Decisions (approved 2026-06-05)

- **D1 - Per-team artifact dir.** All artifacts live under `ARTIFACTS/<team>/`:
  `stack.json`, `pr-triage/`, `pr-shipper-brief.md`, and `adv-review/`. With the default
  `ARTIFACTS=/tmp` this is `/tmp/<team>/...` (matches the roadmap). The stack filename
  drops its `<team>-` prefix (the dir now carries the team identity): `stack.json`.
- **D2 - Key = `<team>`** (NOT `$TMUX`). These artifacts are referenced by humans and bots
  BY TEAM NAME in charters/briefs; the team is the natural, readable key. (Contrast the
  marker, which the guard keys by `$TMUX` because it cannot know the team.)
- **D3 - Team-name validation.** `--team` must match `^[A-Za-z0-9._-]+$` (no `/`, no
  `..`, non-empty). `up` aborts cleanly with a clear message otherwise. Prevents a path
  escape / a broken `os.path.join` from an odd team name. (New, small.)
- **D4 - No backward-compat.** Ephemeral `/tmp`, wipe-the-deck precedent. The old flat
  paths are dropped, not aliased.
- **D5 - `down` unchanged re artifacts.** It does NOT remove the team artifact dir
  (current behavior: artifacts are left for post-mortem; `/tmp` clears on reboot). No
  artifact GC (YAGNI).

## Component 1 - `orchestrate-setup.py`

- Add a validator:
  ```python
  TEAM_RE = re.compile(r'^[A-Za-z0-9._-]+$')
  def _validate_team(team):
      if not team or not TEAM_RE.match(team) or team in (".", ".."):
          raise SystemExit(f"up: ABORT - invalid --team {team!r}; use [A-Za-z0-9._-]+ "
                           "(no '/', no '..').")
  ```
  Call it at the top of `cmd_up` (before doctor/scaffold). `SystemExit` gives a clean
  non-zero exit + message, no traceback.
- `scaffold_artifacts(team, repo, spacing)`: build `team_dir = os.path.join(ARTIFACTS,
  team)`, `os.makedirs(team_dir, exist_ok=True)`, then:
  - `stack = os.path.join(team_dir, "stack.json")`
  - `triage = os.path.join(team_dir, "pr-triage")` (`makedirs`)
  - `adv = os.path.join(team_dir, "adv-review")` (`makedirs`) - NEW (roadmap lists it)
  - `brief_out = os.path.join(team_dir, "pr-shipper-brief.md")`
  - Brief token substitution (`<STACK>` etc.) and the rendered-by header are unchanged
    except they now point at the nested paths.
  - Return `stack, triage, brief_out` (and optionally `adv`/`team_dir` if a caller needs
    them; keep the return tuple minimal - the `up` print can show `team_dir`).
- `cmd_up` SESSION-ARMED print: show `team dir: {team_dir}` (and the nested stack/triage/
  brief) so the lead/bots get the real paths.

## Component 2 - Tests (`test-orchestrate-setup.py`)

- Update existing Task-5 assertions to the nested layout: stack at
  `<art>/<team>/stack.json`, triage at `<art>/<team>/pr-triage`, brief at
  `<art>/<team>/pr-shipper-brief.md`; assert `adv-review/` is created.
- NEW: **two-team no-clobber** - run `up` for team `alpha` then team `beta` (same
  `ARTIFACTS`), assert each has its own `<art>/alpha/...` and `<art>/beta/...` and they do
  not overlap (alpha's stack still `[]` after beta's `up`).
- NEW: **bad team name rejected** - `up --team "a/b"` and `up --team ".."` abort non-zero
  with the validation message and create no `<art>/a` dir.
- Keep the brief-substitution + marker + ttl-clamp + misconfig checks green.

## Component 3 - Templates / charters / docs

- `templates/pr-triage-charter.md`: `<OUTDIR> (default /tmp/pr-triage)` ->
  `<OUTDIR> (default /tmp/<team>/pr-triage)`.
- `templates/pr-shipper-brief.md`: `<STACK> (e.g. /tmp/<team>-stack.json)` ->
  `(e.g. /tmp/<team>/stack.json)`.
- `templates/SESSION-STATE.checkpoint.md`: any `output <dir>` wording -> the team dir.
- `required-permissions.md`: `Write(//tmp/**)` already covers subdirs - NO change.
- `SKILL.md` setup-sequence step 3 mentions `/tmp/<team>-stack.json` / `mkdir -p
  /tmp/pr-triage` - update to the per-team dir.

## Rigor

Non-security plumbing (no guard/marker change): harness GREEN (full
`test-orchestrate-setup.py` + a no-cross-regression run of `test-orchestrate-guard.py`)
+ a SINGLE adversarial critic pass (not the marker work's 5-round loop). TDD inside the
build (update/extend the failing assertions first, then the code). Commit locally in
claude-kit; do NOT push.

## Out of scope

Cross-session port allocator and marker-aware `/merge-pr` (separate P3-A sub-items);
charter pre-rendering / cold-start latency (logged as candidate P3-H).
