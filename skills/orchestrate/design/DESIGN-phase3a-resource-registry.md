# DESIGN - P3-A sub-item: generalized resource registry / allocator (v1)

Date: 2026-06-05 (PDT). Status: SHIPPED (implemented; was APPROVED design, pre-implementation).
Parent: `ROADMAP-phase3.md` P3-A (supersedes the narrower "cross-session port
allocator" bullet). Siblings (done): `DESIGN-phase3a-marker-refcounting.md`,
`DESIGN-phase3a-tmp-namespacing.md`. Canonical code: a NEW
`~/Developer/cc-orchestrator/orchestrate-resources.py` + harness
`test-orchestrate-resources.py`, symlinked into `~/.claude/scripts/`; integrates with
`orchestrate-setup.py` + the SKILL dispatch model.

## Problem & goals

Parallel orchestrate teammates each run a full app instance (the measuring stick: a
stillwater dev server). They collide on resources that have NO owner today - TCP ports
and data/DB dirs (worktrees + branches already have owners: `make worktree`/`worktrees.md`
and git). Two goals, both first-class:

1. **Anti-contention.** Hand out collision-free ports + data dirs across PARALLEL
   sessions, and reclaim them when a session ends or crashes.
2. **Authoritative env contract.** The lease is the orchestrator's DECLARATION of the
   exact env var/value pairs an instance needs, so (a) the lead injects them when standing
   up a teammate's tmux pane, and (b) the teammate AND deterministic scripts (e.g.
   stillwater's `dev-restart.sh`, which already loads a `.env`) CONSUME them instead of
   assuming/guessing. The bundle is therefore EMITTED TO A FILE, not just printed.

## Decisions (approved 2026-06-05)

- **D1 - Two layers.** A generic, app-agnostic REGISTRY CORE (leases, resource kinds,
  atomic JSON, GC) + a PROFILE layer that maps a lease's primitives into a concrete env
  bundle for one app. v1 ships a `stillwater` profile. New apps = new profiles; the core
  never learns app specifics.
- **D2 - v1 allocates ports + dirs, emits the env bundle, records worktree/branch.**
  (Q1 answer.) Worktree/branch are `meta` references, not owned. The env bundle (incl.
  shared values like the encryption key + music path) is derived OUTPUT.
- **D3 - Per-teammate LEASE bundle** keyed by `(session, teammate)`; one `allocate`
  returns the whole instance profile, one `release` frees all of it. (Q4 answer.)
- **D4 - Atomicity = advisory file lock.** Every mutate takes `fcntl.flock(LOCK_EX)` on
  the state file, does read-modify-write, then atomic `os.replace`. Safe for truly
  parallel leads. (Q2 answer.)
- **D5 - GC = liveness probe + TTL fallback, lazy + on `down`.** (Q3 answer.)
- **D6 - Env bundle is WRITTEN to a `.env` file** (path recorded in the lease) in
  addition to the lease JSON `env` map and an eval-able export block on STDERR - so
  deterministic scripts/tmux standup source it, never assume. (Maintainer refinement.)
  PRECEDENCE (maintainer 2026-06-05): `dev-restart.sh` prioritizes an already-set REAL env
  var over the `.env` file. So the AUTHORITATIVE delivery is the lead EXPORTING the bundle
  as real env into the teammate's tmux pane (form 2) - it wins over any stale/checked-in
  `.env`, eliminating contention. The written `.env` (form 3) is a durable RECORD + a
  fallback for vars NOT already exported; it can never override an exported value, by
  design. Do not rely on the file to CHANGE a value the pane already exported.

## State file & concurrency

- `~/.claude/orchestrate-resources.json` (shared across sessions; sibling of
  `orchestrate-floor.d`). Override: `ORCHESTRATE_RESOURCES_FILE` (tests point it at a
  tempdir). Created on first `allocate` (`{"version":1,"leases":[]}`).
- Every mutating op: open (create if absent), `fcntl.flock(fd, LOCK_EX)`, read+parse,
  modify, write a temp file in the same dir + `os.replace` over the original, then close
  (releasing the lock). Read-only `list` may take `LOCK_SH`. A corrupt/unparseable file is
  a hard error (do NOT silently reset - that could double-allocate); print the path.

## Schema (lease-centric, open-ended)

```json
{
  "version": 1,
  "leases": [
    {
      "id": "<session>/<teammate>",
      "session": "<team-or-$TMUX>",
      "teammate": "alice",
      "profile": "stillwater",
      "created": "<iso-utc>",
      "ttl_hours": 72,
      "resources": {
        "port":     { "kind": "port", "value": 1983 },
        "data_dir": { "kind": "dir",  "value": "/tmp/orchestrate/<session>/alice" }
      },
      "env": { "SW_PORT": "1983", "SW_DB_PATH": ".../stillwater.db", "...": "..." },
      "env_file": "/tmp/orchestrate/<session>/alice/instance.env",
      "meta": { "worktree": "../stillwater-...", "branch": "..." }
    }
  ]
}
```
`resources` is a map of arbitrary KINDS - adding a kind (a second port, an appdata path)
is just another entry; nothing else in the core changes. `env`/`env_file` are the
contract (D6). `meta` records already-owned things without owning them.

## Resource kinds (v1)

- **port** - allocate the lowest free port in a pool (default range `1980-2080`, base
  `1983`; overridable `ORCHESTRATE_PORT_RANGE`), skipping (a) any port already in a lease
  and (b) any port currently LISTENing (`lsof -nP -iTCP:<p> -sTCP:LISTEN`, the
  listener-scoped form - never a bare `lsof -ti` that also matches clients). v1 allocates
  ONE TCP port (stillwater dev = plain HTTP on `SW_PORT`; no TLS/HTTP3 for UAT). A profile
  may request N (`--ports N`); default 1.
- **dir** - a unique data dir under a base (default `/tmp/orchestrate/<session>/<teammate>`;
  overridable `ORCHESTRATE_RESOURCE_BASE`). The core `mkdir -p`s it and records the path.
  SEEDING its contents (copying a DB, etc.) is the profile's job, never the core's.

## Env bundle emission (D6 - the contract)

`allocate` produces the bundle in THREE consumable forms:
1. The lease JSON `env` map (machine-readable, in the registry).
2. An eval-able export block on STDERR (`export SW_PORT=1983\n...`) - STDOUT stays pure
   JSON (the lease) so it is machine-parseable without stripping. The lead reads the STDERR
   block deliberately (e.g. capture stderr) and exports it into the teammate's tmux pane at
   standup. Do NOT `eval $(orchestrate-resources.py allocate ...)` - that captures only
   stdout (the JSON), not the export block.
3. A WRITTEN `.env` file at `resources.data_dir/instance.env` (path stored as
   `lease.env_file`), `KEY=VALUE` per line - so deterministic scripts source it
   (stillwater's `dev-restart.sh` loads a `.env`; point it at this file, or symlink it to
   the worktree's `.env`). This is what stops the agent/scripts from assuming values.

## Profile: stillwater (v1)

From the recon collision analysis - UNIQUE per instance: `SW_PORT`, `SW_DB_PATH`,
`SW_BACKUP_PATH`; SHARED: the encryption key (as a FILE, see below) + `SW_MUSIC_PATH`;
auto/independent: `SW_SESSION_SECRET` (auto-generated), logging. Given an allocated
`{port, data_dir}` the profile emits this env bundle (NOTE: NO secret in it):
```
SW_PORT=<port>
SW_DB_PATH=<data_dir>/stillwater.db
SW_BACKUP_PATH=<data_dir>/backups
SW_MUSIC_PATH=<shared music library>
SW_LOG_FORMAT=text
SW_LOG_LEVEL=debug
```
- **Encryption key = a FILE beside the DB, NEVER an env value (VERIFIED 2026-06-05 against
  `cmd/stillwater/main.go:1041` `resolveEncryptionKey`).** Native priority is
  `SW_ENCRYPTION_KEY` (value) > an `encryption.key` file in `dirname(SW_DB_PATH)` >
  generate. There is NO `SW_ENCRYPTION_KEY_FILE` in the native binary (that is a
  Docker-entrypoint convention only; `reference_uat_encryption_key.md` now records both
  native forms). The profile uses the FILE mechanism: it does NOT put the key in `env`/`.env`/the
  process environment (which would leak it via `ps eww`, `/proc`, and a plaintext secret in
  `/tmp/.../instance.env`). Instead, provisioning places the real key as
  `<data_dir>/encryption.key` (0600), preferably a SYMLINK to the configured real key file,
  so the binary finds it beside the DB. Config: `ORCHESTRATE_STILLWATER_KEYFILE` (real
  `encryption.key` path), `ORCHESTRATE_STILLWATER_MUSIC` (shared library). No real path is
  guessed in code - a missing required config var makes `allocate --profile stillwater`
  error with a message naming the var.
- **Provisioning (`--provision`) is opt-in and pairs the DB + key:** copy the real DB to
  `<data_dir>/stillwater.db`, `rm -f *-wal *-shm`, `mkdir -p backups`, AND place
  `<data_dir>/encryption.key` (symlink/copy of the configured real key, 0600). WITHOUT
  `--provision`: a fresh empty DB whose key the binary auto-generates beside it - no real
  secret needed, self-consistent. Either way the secret never enters the env bundle. The
  profile prints the exact `setup_hint` commands when not auto-running them.

## CLI: `orchestrate-resources.py`

- `allocate --session S --teammate T --profile P [--ports N] [--provision]` - gc first,
  then atomically reserve free port(s) + a dir, derive the env, write the lease + the
  `.env` file, print the lease JSON on STDOUT and the eval-able `export KEY=VALUE` block on
  STDERR. Idempotent per `(S,T)`: re-allocate returns the existing lease (does not
  double-allocate).
- `release (--session S --teammate T | --lease ID) [--purge]` - drop the lease (frees
  port+dir for reuse); `--purge` also `rm -rf`s the data dir.
- `gc` - reclaim dead leases (see GC). Safe to run anytime.
- `list [--session S] [--json]` - human/JSON view.

## GC (liveness + TTL)

A lease is reclaimable ONLY when its port is NOT LISTENing, AND additionally either (its
`data_dir` is gone OR its owning `$TMUX` marker is absent from `orchestrate-floor.d`) OR it
is older than `ttl_hours` (default 72). A LISTENing port always pins the lease, even past
TTL - the allocator exists to prevent collisions, so it never reclaims a still-listening
server's lease. `gc` runs inside every `allocate` (lazy) and is called by
`orchestrate-setup.py down` for the ending session. GC never deletes a data dir unless the
lease is `--purge`d on release (a reclaimed-by-gc dir is left for post-mortem, like the
/tmp artifacts).

## Integration

- `orchestrate-setup.py down`: best-effort `release`/`gc` of the ending session's leases
  (resolve them by `session`); failures are non-fatal (mirror the marker GC).
- The LEAD calls `allocate` per teammate at spawn, reads the STDERR export block, and
  EXPORTS those vars as real env into the teammate's tmux pane (the authoritative path -
  wins over any `.env` per D6). STDOUT is the lease JSON (machine-parseable; use it to
  record the lease in the dispatch map). The written `lease.env_file` is the durable
  record/fallback. The dispatch map gains the per-teammate lease.
- `SKILL.md`: document the allocate-at-spawn step + the env-file contract; note ports/dirs
  are leased (no hand-picked fixed ports).
- Deploy: claude-kit canonical + symlink into `~/.claude/scripts/` (like safe-push.sh /
  orchestrate-guard.sh).

## Testing (`test-orchestrate-resources.py`, env-driven temp fixtures)

- distinct ports + dirs across two teammates (no overlap).
- LISTENing port is skipped (bind a socket on a port in range, assert allocate avoids it).
- flock serialization: many concurrent `allocate` calls (subprocesses) yield NO duplicate
  port (the core invariant under parallelism).
- idempotent re-allocate for the same `(session,teammate)` returns the same lease.
- gc reclaims a dead lease (port free + dir gone) but SPARES a live one (port listening).
- release frees a port for re-allocation; `--purge` removes the dir.
- TTL expiry reclaims.
- corrupt state file -> hard error (no silent reset / double-allocate).
- stillwater profile: emits the expected SW_* keys; writes `instance.env`; errors clearly
  when `ORCHESTRATE_STILLWATER_KEYFILE`/`_MUSIC` are unset; `--provision` runs the setup.
- THEN a 1-2 round adversarial critic pass (cross-session-stateful, not security-floor;
  hunt: dup-allocation races, GC reclaiming a live lease, port-range exhaustion, env-file
  contention, profile config-missing handling).

## Out of scope (v1)

Owning worktree/branch creation (meta only); multi-port-per-instance beyond `--ports`;
auto-seeding beyond `--provision`; non-stillwater profiles; the cold-start latency item
(P3-H). The port-range default assumes the macOS dev box; not a multi-user host.
