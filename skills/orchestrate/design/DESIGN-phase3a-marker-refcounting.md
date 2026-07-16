# DESIGN - Phase 3A: per-session marker refcounting (orchestrate floor)

Date: 2026-06-05 (PDT). Status: SHIPPED (implemented; was APPROVED design, pre-implementation).
Companions (read for style/context): `DESIGN-deterministic-floor.md` (Phase 1),
`DESIGN-phase2-setup.md` (Phase 2), `ROADMAP-phase3.md` (P3-A item),
`REVIEW-FINDINGS.md`. Canonical code lives in `~/Developer/claude-kit/`
(`orchestrate-guard.sh`, `orchestrate-setup.py`, `test-orchestrate-guard.py`,
`test-orchestrate-setup.py`); deployed into `~/.claude/scripts/` by symlink.

## Problem

The deterministic floor uses a SINGLE global marker file
`~/.claude/orchestrate-floor.active` to signal "an orchestrate session is active"
(it gates the merge-by-API hard-deny). Two defects under parallel / multi-session use:

1. **Cross-session disarm.** `orchestrate-setup.py down` removes the one global file,
   so one session's teardown disarms EVERY other live session. Demonstrated live: a
   parallel `m55-warm-fronts` session had the single marker disarmed out from under it
   during the Phase-2 build.
2. **Tombstone gating of solo sessions.** A crashed session leaves the global marker
   behind; until its 24h TTL expires, an unrelated SOLO session that happens to attempt
   merge-by-API is gated by that foreign tombstone. The current semantics are "does ANY
   marker exist anywhere," which is the wrong question.

**Hard requirement (maintainer):** a standalone (unorchestrated) session MUST be immune
to another session's tombstone. `marker_active()` must answer "is THIS session an armed
orchestrate session?", not "does any marker exist?".

## Decisions (settled via brainstorming 2026-06-05)

- **D1 - Key = whole `$TMUX`, sanitized.** `$TMUX` = `socket_path,server_pid,session_id`.
  It is IDENTICAL across all panes/windows of one tmux session (lead + every teammate
  share it -> all gated, which is exactly the set we want) and DIFFERS across separate
  tmux sessions. A restarted tmux server gets a new pid, so a stale tombstone can never
  match a future session's key -> tombstone immunity falls out of the keying for free.
  The team name is NOT in the key (the guard never knows it); it is stored as metadata
  INSIDE the marker file. The key must be derivable from the guard's environment ALONE.
- **D2 - Empty `$TMUX` => INACTIVE (never gated).** A non-tmux session cannot be an
  orchestrate session (`up`'s doctor REQUIRES `$TMUX`), so it is always solo. With no
  `$TMUX` there is no key, so no marker can ever match -> airtight, trivial immunity.
  > **SUPERSEDED by #312.** Both premises are now false: doctor's tmux checks are
  > advisory WARNs (#294) and `up` arms outside tmux, because the session key falls back
  > to `ccsid_` + the sanitized `$CLAUDE_CODE_SESSION_ID`. A non-tmux session IS a gated
  > orchestrate session, keyed and merge-gated exactly like a tmux one. Only a session
  > with NEITHER identifier is unkeyed -> never gated (the immunity D2 describes now
  > attaches to that case, not to "no tmux"). The guard matches ANY candidate key; see
  > the DERIVATION REGISTRY in `scripts/orchestrate-guard.sh`.
  >
  > The trade-off recorded immediately below is therefore VOID, and its premise was wrong
  > even before #312: it accepted that "a (never-actually-used) non-tmux orchestrate session
  > would not get the merge-by-API hook deny" on the grounds that non-tmux was "an
  > unsupported config". Non-tmux is now the maintainer's PREFERRED config (the iTerm2 /
  > in-process backend), and it IS gated. The fallback the trade-off leaned on ("the
  > allow-list + auto-mode still block an autonomous bot merge") was never load-bearing
  > either: the allow-list now carries an EXPLICIT `Bash(gh pr merge *)` entry (#105),
  > precisely because the FLOOR deny - not the allow-list - is what stops a bot merge.
  ~~Trade-off accepted: a (never-actually-used) non-tmux orchestrate session would not get
  the merge-by-API hook deny - acceptable, it is an unsupported config and the
  allow-list + auto-mode still block an autonomous bot merge.~~ (VOID - see above.)
- **D3 - Wipe the legacy single-file config (maintainer: "wipe the deck").** Drop
  `ORCHESTRATE_FLOOR_MARKER` entirely. New config: `ORCHESTRATE_FLOOR_DIR` (default
  `~/.claude/orchestrate-floor.d`) + the `$TMUX` key. Tests are rewritten to set
  `ORCHESTRATE_FLOOR_DIR` + `TMUX`. No legacy single-file escape hatch is retained.
- **D4 - TTL = 72h, no hot-path write, GC on `down`.** Marker written once at `up`; TTL
  raised 24h -> 72h so realistic multi-day sessions do not silently lose the gate
  mid-run. The guard stays a PURE READ on the always-on path (no heartbeat/touch -> no
  new fail-open surface). `down` removes its own key file AND opportunistically prunes
  any file in `FLOOR_DIR` older than TTL (cheap tombstone GC).

## Sanitization (must be byte-identical on both sides)

Replace every character NOT in `[A-Za-z0-9]` with `_`, 1:1 (no squeeze, no truncation):
- bash:   `printf '%s' "$TMUX" | tr -c 'A-Za-z0-9' '_'`
- python: `re.sub(r'[^A-Za-z0-9]', '_', tmux)`

`$TMUX` has no newlines (`socket,pid,session`); `printf '%s'` adds none. The two
transforms produce the same string for the same input. They are a CONTRACT: if one
changes, the other must change in lockstep, or the guard and setup will key differently
and the gate silently breaks. A test asserts a known `$TMUX` maps to the expected key in
both implementations.

## Component 1 - `orchestrate-guard.sh`

- Remove `MARKER="${ORCHESTRATE_FLOOR_MARKER:-...}"`. Add
  `FLOOR_DIR="${ORCHESTRATE_FLOOR_DIR:-$HOME/.claude/orchestrate-floor.d}"`.
- `TTL_HOURS="${ORCHESTRATE_FLOOR_TTL_HOURS:-72}"` (was 24).
- Rewrite `marker_active()`:
  ```sh
  marker_active() {
    [ -n "${TMUX:-}" ] || return 1          # D2: no tmux => never gated
    local key marker mtime now age_h
    key=$(printf '%s' "$TMUX" | tr -c 'A-Za-z0-9' '_')
    marker="$FLOOR_DIR/$key"
    [ -f "$marker" ] || return 1
    mtime=$(stat -f %m "$marker" 2>/dev/null) || return 1
    now=$(date +%s)
    age_h=$(( (now - mtime) / 3600 ))
    [ "$age_h" -lt "$TTL_HOURS" ]
  }
  ```
- Still called ONLY inside the merge-by-API branch (line ~155); the outer
  `push|gh` perf short-circuit is unchanged, so ordinary pipelines stay O(1).
- `--self-test` (Tier-1 push-main) is marker-independent and unchanged.
- Fail-OPEN behavior preserved on every error path.

## Component 2 - `orchestrate-setup.py`

- Remove `MARKER`; add `FLOOR_DIR = os.environ.get("ORCHESTRATE_FLOOR_DIR",
  os.path.join(HOME, ".claude", "orchestrate-floor.d"))`.
- Add `session_key()` mirroring the guard's transform EXACTLY; raise/return None if
  `$TMUX` is empty.
- `arm_marker(team, repo, head)`: if `$TMUX` empty -> hard error (cannot arm a keyless
  session; `up` already requires tmux via doctor, this is defense in depth).
  Else `os.makedirs(FLOOR_DIR, exist_ok=True)` and write `FLOOR_DIR/<key>` with
  metadata (`team`, `started`, `repo`, `head`, `tmux`).
- `armed_self_test()`: unchanged in intent. The guard subprocess inherits the parent's
  `$TMUX` and `ORCHESTRATE_FLOOR_DIR`, so it computes the same key and finds the marker
  `arm_marker` just wrote. Still asserts BOTH Tier-1 push-main and Tier-2 merge-by-API
  exit 2 with the marker armed.
- `cmd_up`: on `armed_self_test` failure, remove THIS session's key file (not the dir).
- `cmd_down`: compute own key; remove `FLOOR_DIR/<key>` (tolerate missing); THEN GC -
  iterate `FLOOR_DIR`, `os.remove` any entry whose mtime is older than TTL hours; print
  the teardown checklist. GC errors are swallowed (best-effort hygiene, never fatal).
- `doctor`: no required change for P3-A. (The P3-G cascade-scan is a separate item.)

## Component 3 - Tests (rebuild the marker cases)

`test-orchestrate-guard.py` - set `ORCHESTRATE_FLOOR_DIR` (temp dir) + `TMUX` per case:
- armed self: `TMUX=X`, fresh `FLOOR_DIR/<key(X)>`, merge-by-API -> exit 2.
- FOREIGN marker does NOT gate me: `TMUX=Y`, only `<key(X)>` exists -> merge-by-API
  allowed (exit 0). (Core refcount property.)
- empty `$TMUX` never gated: unset `TMUX`, a file present in `FLOOR_DIR` -> exit 0.
- stale marker: `<key(X)>` mtime older than TTL -> not gated (exit 0).
- two sessions armed: `<key(X)>` and `<key(Y)>` both fresh -> each gated under its own
  `$TMUX`; remove `<key(X)>` -> `Y` still gated, `X` not.
- key-contract: a known `$TMUX` value maps to the expected sanitized filename.
- Regression: Tier-1 denies (push-main / force / no-verify) still fire regardless of
  marker (marker-independent), and merge-STATUS GET is still allowed.

`test-orchestrate-setup.py` - set `ORCHESTRATE_FLOOR_DIR` + `TMUX`:
- arm creates `FLOOR_DIR/<key>` with metadata.
- down removes ONLY own key, LEAVES a foreign session's file in place.
- down GCs a stale tombstone (old mtime) while keeping a fresh foreign marker.
- arm refuses when `$TMUX` is empty.
- armed_self_test passes (Tier-1 + Tier-2 both exit 2 with marker armed).
- key-contract parity with the guard's expected filename for a known `$TMUX`.

## Component 4 - Rigor / process

- Phase-1-grade because this re-touches the always-on security guard: keep the FULL
  existing harness green, add the cases above, then run the engage-ralph-loop
  (`~/Developer/cc-orchestrator/engage-ralph-loop.md`) - parallel READ-ONLY hostile critics
  hunt new honest-path bypass spellings AND false-positives until K=2 consecutive dry
  rounds.
- Test-driving isolation (the orchestrate rule): NEVER put a trigger substring
  (`git push` / `main` / merge-by-API) on a live Bash command line - build payloads
  inside the test files and feed them to the guard on stdin (the existing pattern).
- Deploy = symlink only (already in place for the guard); the gist Stop-hook auto-syncs
  claude-kit. NO push / NO PR without the maintainer's explicit go.

## Risks / edge cases

- **Sanitization drift** between bash and python silently breaks the gate. Mitigated by
  the key-contract test on both sides (Component 3).
- **Concurrent arm** from two sessions: different keys -> different files -> no collision;
  `mkdir -p` is idempotent.
- **>72h session** still eventually loses the gate; documented, accepted (rare; the
  allow-list + auto-mode bot-merge block remain).
- **In-flight upgrade**: a session armed with the OLD global `*.active` file is not read
  after this change. The marker is transient; no live session is armed at design time.
- **Guard perf**: adds a `tr` + `stat` only inside the already-rare merge-by-API branch;
  negligible against the ~5ms always-on budget.

## Out of scope (tracked elsewhere in P3 / ROADMAP)

`/tmp` team-namespacing, the cross-session port allocator, marker-aware `/merge-pr`, and
the P3-G doctor settings-cascade scan are each their own piece. This spec is ONLY the
`$TMUX`-keyed marker refcounting in the guard + setup + their harnesses.
