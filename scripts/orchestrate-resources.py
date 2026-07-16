#!/usr/bin/env python3
"""orchestrate-resources.py - cross-session resource registry/allocator for orchestrate.

  allocate --session S --teammate T --profile P [--ports N] [--provision]
  release  (--session S --teammate T | --lease ID) [--purge]
  gc
  list [--session S] [--json]

State: $ORCHESTRATE_RESOURCES_FILE (default ~/.claude/orchestrate-resources.json),
mutated under an exclusive flock with an atomic temp+replace write.
Design: ~/.claude/skills/orchestrate/design/DESIGN-phase3a-resource-registry.md
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse

HOME = os.path.expanduser("~")
STATE = os.environ.get("ORCHESTRATE_RESOURCES_FILE", os.path.join(HOME, ".claude", "orchestrate-resources.json"))
RESOURCE_BASE = os.environ.get("ORCHESTRATE_RESOURCE_BASE", "/tmp/orchestrate")
PORT_RANGE = os.environ.get("ORCHESTRATE_PORT_RANGE", "1980-2080")
FLOOR_DIR = os.environ.get("ORCHESTRATE_FLOOR_DIR", os.path.join(HOME, ".claude", "orchestrate-floor.d"))
# F2(c): orchestrate-setup.py `up` scaffolds artifacts under ARTIFACTS/<team> and persists
# the stillwater profile config to <team>/profile.env. The session name passed to `allocate`
# IS the team name, so derive the team dir the SAME way `up` does (ORCHESTRATE_ARTIFACT_DIR,
# default /tmp) - no divergent hardcoded path.
ARTIFACTS = os.environ.get("ORCHESTRATE_ARTIFACT_DIR", "/tmp")


def _int_env(name, default, minimum=None):
    try:
        val = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    if minimum is not None and val < minimum:
        return default
    return val


TTL_HOURS = _int_env("ORCHESTRATE_FLOOR_TTL_HOURS", 72, minimum=1)
# Startup grace: a just-allocated lease is NOT liveness-reclaimable for this many seconds,
# so a lazy gc (run inside the next allocate) cannot reap a lease before its server has had
# time to start listening. Without it, a fresh lease in a session with no floor marker
# (marker_key="") is immediately reclaimable. The hard TTL backstop still applies. 0 disables.
LEASE_GRACE_SECONDS = _int_env("ORCHESTRATE_LEASE_GRACE_SECONDS", 300, minimum=0)


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _marker_key():
    """This session's floor-marker key, for lease liveness (_marker_absent -> reclaimable).

    #312: MIRRORS the derivation in the DERIVATION REGISTRY in `scripts/orchestrate-guard.sh`
    (SIX live copies - keep them in lockstep). $TMUX wins, sanitized and UNPREFIXED; else
    'ccsid_' + the sanitized $CLAUDE_CODE_SESSION_ID; else "" (no key).

    WHY THIS HAD TO CHANGE WITH #312, and why "" is no longer a safe default: pre-#312, no
    $TMUX genuinely meant no marker could exist, so "" was TRUE and a lease keyed "" was
    correctly reclaimable. #312 lets a NON-tmux session arm a marker, so a tmux-only key
    reports "" for a session that IS armed and alive -> _marker_absent() reads True ->
    the GC reclaims a LIVE teammate's lease once past the grace window -> the next allocate
    hands out a port that is still in use. Verified: identical steps under $TMUX keep the
    lease; without it the lease is GC'd. Returning the real key restores the invariant that
    "" means "genuinely unkeyed" (neither identifier - a session the floor can never gate)."""
    def _sanitize(value):
        return re.sub(rb'[^A-Za-z0-9]', b'_', value.encode("utf-8", "surrogateescape")).decode("ascii")

    tmux = os.environ.get("TMUX", "")
    if tmux:
        return _sanitize(tmux)
    ccsid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if ccsid:
        return "ccsid_" + _sanitize(ccsid)
    return ""


def _empty_state():
    return {"version": 1, "leases": []}


def _parse_state(text, path):
    if not text.strip():
        return _empty_state()
    try:
        data = json.loads(text)
    except ValueError:
        sys.exit(f"orchestrate-resources: corrupt state file {path} (unparseable JSON) - "
                 "refusing to continue (a silent reset could double-allocate). Fix or remove it.")
    if not isinstance(data, dict) or "leases" not in data:
        sys.exit(f"orchestrate-resources: corrupt state file {path} (missing 'leases').")
    if not isinstance(data["leases"], list):
        sys.exit(f"orchestrate-resources: corrupt state file {path} "
                 "('leases' is not a list - refusing to continue; a silent reset could double-allocate).")
    return data


def _read_state():
    """Read without a write lock (for `list`)."""
    try:
        with open(STATE) as f:
            return _parse_state(f.read(), STATE)
    except FileNotFoundError:
        return _empty_state()


def _with_lock(mutate):
    """Serialize a read -> mutate(state) -> atomic-write across processes.
    `mutate` returns a (new_state, result) tuple; result is returned to the caller.

    The lock is taken on a STABLE side file (STATE + ".lock") that is never renamed.
    This is essential: the write is an atomic `os.replace(tmp, STATE)`, which swaps
    STATE's inode. flock locks an INODE, so locking STATE itself would let a process
    queued on the old (now-unlinked) inode wake up, re-read STALE content, and pick a
    resource another writer already took (observed: duplicate ports under concurrency).
    Locking the never-replaced .lock inode serializes correctly; STATE is read fresh
    AFTER the lock is held."""
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    lock_fd = os.open(STATE + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = _read_state()  # fresh read under the lock (STATE may have been replaced)
        new_state, result = mutate(state)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as wf:
            json.dump(new_state, wf, indent=2)
        os.replace(tmp, STATE)
        return result
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def cmd_list(args):
    leases = _read_state()["leases"]
    if getattr(args, "session", None):
        leases = [lease for lease in leases if lease.get("session") == args.session]
    if args.json:
        print(json.dumps(leases, indent=2))
    else:
        for lease in leases:
            print(f"{lease['id']:30} {lease.get('profile','-'):12} "
                  f"port={lease['resources'].get('port',{}).get('value','-')} "
                  f"dir={lease['resources'].get('data_dir',{}).get('value','-')}")
    return 0


def _listening_ports_snapshot():
    """Set of all TCP ports with an active LISTENer, from a SINGLE lsof enumeration.

    #98: the old code fanned out one `lsof -iTCP:PORT` per port across the whole
    ORCHESTRATE_PORT_RANGE (~101 calls for the default 1980-2080). When lsof is slow
    (e.g. it crawls the macOS TimeMachine localsnapshot volumes), that fan-out makes
    `allocate` hang past the harness timeout. One `lsof -nP -iTCP -sTCP:LISTEN` enumerates
    every listener in a single crawl, so a slow lsof costs one call, not N.

    Listener-scoped via `-sTCP:LISTEN` (never a bare lsof that also matches client
    connections). `-Fn` gives field-prefixed machine output: each `n` line is a network
    name like `n*:8080` / `n127.0.0.1:2000` / `n[::1]:2000`; the port is the token after
    the last ':'. lsof unavailable / slow-failure -> empty set: do not block allocation
    (the eventual bind fails loudly later), exactly as the per-port fallback did."""
    try:
        r = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fn"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return set()  # lsof missing or timed out -> treat as "nothing known to listen"
    ports = set()
    for line in r.stdout.splitlines():
        if not line.startswith("n"):
            continue
        name = line[1:]
        if "->" in name:  # a peer address means an established conn, not a pure listener; skip
            continue
        _host, sep, port = name.rpartition(":")
        if not sep:
            continue
        try:
            ports.add(int(port))
        except ValueError:
            continue
    return ports


def _port_range():
    try:
        lo, _, hi = PORT_RANGE.partition("-")
        lo_i, hi_i = int(lo), int(hi)
    except ValueError:
        sys.exit(f"orchestrate-resources: invalid ORCHESTRATE_PORT_RANGE {PORT_RANGE!r}; "
                 "expected 'LO-HI' (e.g. 1980-2080).")
    if lo_i > hi_i:
        sys.exit(f"orchestrate-resources: ORCHESTRATE_PORT_RANGE {PORT_RANGE!r} is inverted "
                 f"(lo={lo_i} > hi={hi_i}); swap the values.")
    return lo_i, hi_i


def _leased_ports(state):
    out = set()
    for lease in state["leases"]:
        pv = lease.get("resources", {}).get("port", {}).get("value")
        if isinstance(pv, int):
            out.add(pv)
    return out


def _scan_listening(lo, hi, snapshot=None):
    """Pre-scan OS-LISTENing ports in [lo, hi], called OUTSIDE the state lock so LOCK_EX is
    never held across an lsof subprocess (the in-memory pick is the only locked work). #98:
    ONE lsof enumeration (via `_listening_ports_snapshot`) filtered to the range, not one
    lsof per port. Pass a shared `snapshot` to reuse a single enumeration across callers."""
    if snapshot is None:
        snapshot = _listening_ports_snapshot()
    return {p for p in snapshot if lo <= p <= hi}


def _free_port(state, listening):
    """Lowest port in range not already leased and not in the pre-scanned `listening` set."""
    lo, hi = _port_range()
    taken = _leased_ports(state) | listening
    for p in range(lo, hi + 1):
        if p not in taken:
            return p
    sys.exit(f"orchestrate-resources: no free port in range {PORT_RANGE} "
             f"({len(_leased_ports(state))} leased) - widen ORCHESTRATE_PORT_RANGE or run `gc`.")


def _find_lease(state, lease_id):
    for lease in state["leases"]:
        if lease["id"] == lease_id:
            return lease
    return None


def cmd_allocate(args):
    for part in (args.session, args.teammate):
        if not part or "/" in part or part in (".", ".."):
            sys.exit(f"orchestrate-resources: invalid session/teammate {part!r} "
                     "(must be non-empty and contain no '/' and not be '.' or '..').")
    lease_id = f"{args.session}/{args.teammate}"
    lo, hi = _port_range()
    # Scan liveness OUTSIDE the lock (I-1: keep LOCK_EX short - no lsof under the lock).
    # Cover the alloc range AND any already-leased port (a lease may sit outside the range
    # if the range changed), so the lazy gc below has accurate liveness for every lease.
    # #98: take ONE lsof enumeration and reuse it for both filters (no per-port fan-out,
    # no double crawl), so a slow lsof costs a single call.
    snapshot = _listening_ports_snapshot()
    listening = _scan_listening(lo, hi, snapshot) | _scan_listening_ports(_leased_ports(_read_state()), snapshot)

    def mutate(state):
        _gc_inplace(state, listening)  # reclaim dead leases before allocating
        existing = _find_lease(state, lease_id)
        if existing:
            return state, existing  # idempotent
        port = _free_port(state, listening)
        data_dir = os.path.join(RESOURCE_BASE, args.session, args.teammate)
        os.makedirs(data_dir, exist_ok=True)
        lease = {
            "id": lease_id, "session": args.session, "teammate": args.teammate,
            "profile": args.profile, "created": _now_iso(), "ttl_hours": TTL_HOURS,
            "marker_key": _marker_key(),
            "resources": {"port": {"kind": "port", "value": port},
                          "data_dir": {"kind": "dir", "value": data_dir}},
            "env": {}, "env_file": None, "meta": {},
        }
        apply_profile(args.profile, lease, args)  # defined in Task 5 (no-op for "generic")
        write_env_bundle(lease)                   # defined in Task 3
        state["leases"].append(lease)
        return state, lease

    lease = _with_lock(mutate)
    print(json.dumps(lease, indent=2))
    if lease["env"]:
        sys.stderr.write("# eval-able exports (orchestrate-resources):\n")
        for k, v in lease["env"].items():
            sys.stderr.write(f"export {k}={shlex.quote(v)}\n")
    return 0


# --- Lease lifecycle: age, liveness, GC ---

def _lease_age_hours(lease):
    try:
        created = datetime.datetime.strptime(lease["created"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except (KeyError, ValueError, TypeError):
        return float("inf")  # malformed/null timestamp -> treat as ancient (reclaimable)
    return (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds() / 3600.0


def _marker_absent(lease):
    key = lease.get("marker_key") or ""
    if not key:
        return True  # no owning marker recorded -> cannot be kept alive by one
    return not os.path.isfile(os.path.join(FLOOR_DIR, key))


def _scan_listening_ports(ports, snapshot=None):
    """Liveness-scan a set of ports OUTSIDE any lock, from a SINGLE lsof enumeration (#98:
    was one lsof per port). Pass a shared `snapshot` to reuse one enumeration across callers;
    an empty `ports` short-circuits with no lsof call at all."""
    wanted = {p for p in ports if isinstance(p, int)}
    if not wanted:
        return set()
    if snapshot is None:
        snapshot = _listening_ports_snapshot()
    return wanted & snapshot


def _reclaimable(lease, listening):
    """`listening` is a pre-scanned set of currently-LISTENing ports (scanned outside the
    lock, so this stays pure in-memory and LOCK_EX is never held across lsof).

    Liveness wins over TTL: if the port is currently LISTENing, spare the lease
    unconditionally -- even if it is ancient.  NOT-listening is a precondition for
    all reclamation paths."""
    port = lease.get("resources", {}).get("port", {}).get("value")
    if port in listening:
        return False  # live server pins the lease regardless of age or marker
    age_h = _lease_age_hours(lease)
    try:
        ttl = float(lease.get("ttl_hours", TTL_HOURS))
    except (TypeError, ValueError):
        ttl = TTL_HOURS  # non-numeric ttl_hours falls back to the global default
    if age_h >= ttl:
        return True  # hard TTL backstop (port not listening, age exceeded)
    if age_h * 3600.0 < LEASE_GRACE_SECONDS:
        return False  # startup grace: server may not be listening yet; don't liveness-reclaim
    data_dir = lease.get("resources", {}).get("data_dir", {}).get("value")
    dir_gone = not (data_dir and os.path.isdir(data_dir))
    return dir_gone or _marker_absent(lease)


def _gc_inplace(state, listening):
    state["leases"] = [lease for lease in state["leases"] if not _reclaimable(lease, listening)]
    return state


def cmd_gc(args):
    # Pre-scan leased-port liveness OUTSIDE the lock (unlocked read is fine: a lease added
    # after the scan is simply not gc'd this round - grace protects it anyway).
    listening = _scan_listening_ports(_leased_ports(_read_state()))
    _with_lock(lambda s: (_gc_inplace(s, listening), None))
    return 0


def cmd_release(args):
    def mutate(state):
        if args.lease:
            target = args.lease
            if args.session and args.teammate:
                derived = f"{args.session}/{args.teammate}"
                if derived != target:
                    sys.exit(f"release: --lease {target!r} conflicts with "
                             f"--session/--teammate (resolves to {derived!r}); "
                             "pass only one selector.")
        elif args.session and args.teammate:
            target = f"{args.session}/{args.teammate}"
        else:
            sys.exit("release: need --lease ID or both --session and --teammate")
        lease = _find_lease(state, target)
        if lease and args.purge:
            ddir = lease.get("resources", {}).get("data_dir", {}).get("value")
            if ddir and os.path.isdir(ddir):
                shutil.rmtree(ddir, ignore_errors=True)
        state["leases"] = [lease for lease in state["leases"] if lease["id"] != target]
        return state, None
    _with_lock(mutate)
    return 0


def _snapshot_sqlite(src_db, dst_db):
    """Point-in-time copy of a (possibly live) SQLite DB via the online backup API (F3).

    A plain file copy of the .db drops the live -wal side-car, so the leased copy can
    miss the most recent committed writes. The backup API reads a consistent snapshot
    (committed WAL folded in) from a READ-ONLY connection -- safe against the running
    instance's concurrent writers -- and yields a self-contained dst (no wal/shm)."""
    # mode=ro: never perturb the live source. A running instance guarantees the
    # -wal/-shm exist and the data dir is writable, so a read-only WAL open succeeds.
    # Drop any stale side-cars from a prior provision into the same (reused) lease dir;
    # a leftover dst-wal would otherwise be replayed and shadow this fresh snapshot.
    for sidecar in (dst_db + "-wal", dst_db + "-shm"):
        try:
            os.remove(sidecar)
        except OSError:
            pass
    src_uri = "file:" + urllib.parse.quote(src_db) + "?mode=ro"
    src = sqlite3.connect(src_uri, uri=True)
    try:
        dst = sqlite3.connect(dst_db)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _provision_stillwater(lease, data_dir, db_path, keyfile, src_db):
    """Materialize the data directory contents for a stillwater instance:
    - snapshot the real DB via the SQLite backup API (point-in-time, WAL folded in)
      when src_db is given and exists
    - mkdir backups/
    - place encryption.key (symlink preferred; 0600 copy fallback)
    The key is never written into env or instance.env (see _profile_stillwater)."""
    os.makedirs(os.path.join(data_dir, "backups"), exist_ok=True)
    if src_db and os.path.exists(src_db):
        _snapshot_sqlite(src_db, db_path)
    dst_key = os.path.join(data_dir, "encryption.key")
    # Prefer a symlink so the TARGET's 0600 is what os.stat follows; fall back to a
    # 0600 copy if the symlink fails (cross-device, permission, etc.).
    # Never trust cp/copy default perms -- enforce 0600 explicitly on the copy path
    # (mirrors stillwater issue #1880).
    try:
        if os.path.lexists(dst_key):
            os.remove(dst_key)
        os.symlink(keyfile, dst_key)
    except OSError:
        # Copy fallback. Remove any leftover entry first, then open with O_NOFOLLOW so a
        # stale/dangling symlink at dst_key can never be FOLLOWED (which would truncate the
        # real key file). Enforce 0600 explicitly (never trust copy defaults; issue #1880).
        try:
            os.remove(dst_key)
        except OSError:
            pass
        fd = os.open(dst_key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out_f, open(keyfile, "rb") as src_f:
            out_f.write(src_f.read())
        os.chmod(dst_key, 0o600)


def _parse_profile_env(path):
    """Parse a <team-dir>/profile.env (the eval-able `export K=V` file `up` writes) into a
    dict. Tolerant: skips blanks, `#` comments, and any line not matching `[export ]K=V`.
    Never raises on a missing/unreadable file (returns {}) - this is a best-effort fallback,
    not a hard dependency. Values are taken verbatim (`up` writes unquoted paths)."""
    out = {}
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key:
            out[key] = val
    return out


def _stillwater_config(args):
    """Resolve the stillwater profile config vars with precedence:
      1. real os.environ (an exported value ALWAYS wins - preserve existing behavior), then
      2. <ARTIFACTS>/<session>/profile.env (what `up` persisted for the session).
    Returns a dict of only the keys that resolved to a non-empty value. Validation
    (missing-required, nonexistent keyfile) stays in the caller, unchanged."""
    keys = ("ORCHESTRATE_STILLWATER_KEYFILE", "ORCHESTRATE_STILLWATER_MUSIC",
            "ORCHESTRATE_STILLWATER_DB")
    file_cfg = _parse_profile_env(os.path.join(ARTIFACTS, args.session, "profile.env"))
    resolved = {}
    for k in keys:
        v = os.environ.get(k, "") or file_cfg.get(k, "")
        if v:
            resolved[k] = v
    return resolved


def _profile_stillwater(lease, args):
    """Populate the env bundle for a stillwater instance.

    The encryption key is NOT placed in the env bundle (never in env, .env, or process
    environment -- it would leak via `ps eww`, /proc, and a plaintext secret in instance.env).
    Instead, provisioning places `<data_dir>/encryption.key` as a symlink/copy beside the DB
    so the binary resolves it via its file-beside-DB mechanism (priority: SW_ENCRYPTION_KEY
    env var > encryption.key in dirname(SW_DB_PATH) > generate).

    Required config vars (no path is guessed; missing -> hard error naming the var):
      ORCHESTRATE_STILLWATER_KEYFILE  - path to the real 0600 encryption key file
      ORCHESTRATE_STILLWATER_MUSIC    - path to the shared music library

    Optional:
      ORCHESTRATE_STILLWATER_DB       - path to a source DB to seed (used by --provision)
    """
    data_dir = lease["resources"]["data_dir"]["value"]
    port = lease["resources"]["port"]["value"]
    # F2(c): resolve config with precedence (exported env wins, then <team-dir>/profile.env)
    # so a session armed via `up` need not re-export the 3 paths every allocate.
    cfg = _stillwater_config(args)
    # Validate ALL required config up front (F2): report every missing key in a single
    # error rather than one hard-fail per key across sequential retries.
    required = ("ORCHESTRATE_STILLWATER_KEYFILE", "ORCHESTRATE_STILLWATER_MUSIC")
    missing = [n for n in required if not cfg.get(n)]
    if missing:
        sys.exit("orchestrate-resources: required config not set (no path is guessed): "
                 + ", ".join(missing) + ". Set them and retry.")
    keyfile = cfg["ORCHESTRATE_STILLWATER_KEYFILE"]
    if not os.path.isfile(keyfile):
        sys.exit(f"orchestrate-resources: ORCHESTRATE_STILLWATER_KEYFILE {keyfile!r} does not "
                 "exist or is not a file (a dangling key symlink would make the binary silently "
                 "auto-generate a wrong key).")
    music = cfg["ORCHESTRATE_STILLWATER_MUSIC"]
    db_path = os.path.join(data_dir, "stillwater.db")
    # Env bundle - the secret key is intentionally absent.
    lease["env"] = {
        "SW_PORT": str(port),
        "SW_DB_PATH": db_path,
        "SW_BACKUP_PATH": os.path.join(data_dir, "backups"),
        "SW_MUSIC_PATH": music,
        "SW_LOG_FORMAT": "text",
        "SW_LOG_LEVEL": "debug",
    }
    lease["meta"]["keyfile_src"] = keyfile
    src_db = cfg.get("ORCHESTRATE_STILLWATER_DB", "")
    # Always record setup hints (useful when --provision is not given).
    setup_hint = [
        f"mkdir -p {os.path.join(data_dir, 'backups')}",
        f"ln -sfn {keyfile} {os.path.join(data_dir, 'encryption.key')}  # real key beside DB (0600 via target)",
    ]
    if src_db:
        setup_hint = [f"sqlite3 {shlex.quote(src_db)} \".backup '{db_path}'\"  "
                      "# point-in-time snapshot incl. live WAL (cp would drop the -wal)"] + setup_hint
    lease["meta"]["setup_hint"] = setup_hint
    if args.provision:
        _provision_stillwater(lease, data_dir, db_path, keyfile, src_db)


def apply_profile(profile, lease, args):
    """Dispatch to the profile handler. Generic profile: no app env (empty bundle)."""
    if profile == "stillwater":
        _profile_stillwater(lease, args)
    # "generic" (default): no app env; the lease carries an empty env bundle.
    return lease


def write_env_bundle(lease):
    """Write lease['env'] to <data_dir>/instance.env (KEY=VALUE per line) and record the
    path on the lease. The map is also the lease's machine-readable contract; cmd_allocate
    prints the lease JSON, and the eval-able export block is emitted by cmd_allocate."""
    data_dir = lease["resources"]["data_dir"]["value"]
    env_file = os.path.join(data_dir, "instance.env")
    # Create 0600 from birth (no world-readable window between create and chmod). Values
    # are written raw `KEY=VALUE`; a profile MUST NOT put a newline in a value (the
    # stillwater profile only emits paths/ports, which are safe).
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for k, v in lease["env"].items():
            f.write(f"{k}={shlex.quote(v)}\n")
    os.chmod(env_file, 0o600)  # enforce mode even if the file pre-existed with looser perms
    lease["env_file"] = env_file
    return lease


def build_parser():
    p = argparse.ArgumentParser(prog="orchestrate-resources.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("list"); pl.add_argument("--session"); pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)
    pa = sub.add_parser("allocate")
    pa.add_argument("--session", required=True)
    pa.add_argument("--teammate", required=True)
    pa.add_argument("--profile", default="generic")
    pa.add_argument("--ports", type=int, default=1)   # v1 uses 1; reserved for future
    pa.add_argument("--provision", action="store_true")
    pa.set_defaults(func=cmd_allocate)
    pr = sub.add_parser("release")
    pr.add_argument("--session"); pr.add_argument("--teammate"); pr.add_argument("--lease")
    pr.add_argument("--purge", action="store_true"); pr.set_defaults(func=cmd_release)
    pg = sub.add_parser("gc"); pg.set_defaults(func=cmd_gc)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
