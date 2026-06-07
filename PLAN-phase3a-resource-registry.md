# Resource Registry v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A generic, JSON-backed, cross-session resource registry/allocator for orchestrate (`orchestrate-resources.py`) that leases collision-free ports + data dirs per teammate and emits an authoritative env bundle, with a `stillwater` profile.

**Architecture:** App-agnostic core (flock-atomic JSON state, per-teammate leases, `port`/`dir` kinds, liveness+TTL GC) + a profile layer that maps a lease's primitives into a concrete env bundle. The encryption key is supplied as a 0600 file beside the DB, never in env.

**Tech Stack:** Python 3 stdlib only (`argparse`, `fcntl`, `json`, `os`, `re`, `subprocess`, `time`, `datetime`). Hand-rolled test harness (subprocess-driven, no pytest), mirroring `test-orchestrate-setup.py`.

**Spec:** `/Users/jesse/.claude/skills/orchestrate/DESIGN-phase3a-resource-registry.md` (read it; this plan implements it).

**Canonical dir:** `~/Developer/claude-kit/`. All commands assume `cd ~/Developer/claude-kit`.

**Conventions (match the existing claude-kit harnesses):**
- Tests drive the CLI as a subprocess with env overrides into a `tempfile.TemporaryDirectory()`; assert on exit code + stdout JSON + on-disk file state. Use a `check(label, cond)` helper + a `FAILS` list; exit 1 if any fail. NO pytest.
- Env overrides the code MUST honor (for test isolation): `ORCHESTRATE_RESOURCES_FILE` (state file), `ORCHESTRATE_RESOURCE_BASE` (dir base), `ORCHESTRATE_PORT_RANGE` (e.g. `1980-2080`), `ORCHESTRATE_FLOOR_DIR` (marker dir, for liveness), `ORCHESTRATE_FLOOR_TTL_HOURS`, `ORCHESTRATE_STILLWATER_KEYFILE`, `ORCHESTRATE_STILLWATER_MUSIC`, `TMUX`.
- Commit locally in claude-kit; NEVER push. No trigger substrings (`git push`, push-destination `main`, `gh api ... pulls/N/merge`) on any Bash command line (the live guard inspects them) -- not expected here, but keep test payloads in files.

---

## Task 1: Core - state file, flock atomic RMW, `list`

**Files:**
- Create: `~/Developer/cc-orchestrator/orchestrate-resources.py`
- Create: `~/Developer/cc-orchestrator/test-orchestrate-resources.py`

- [ ] **Step 1: Write the failing harness (core cases)**

Create `test-orchestrate-resources.py`:
```python
#!/usr/bin/env python3
"""Proof harness for orchestrate-resources.py. Drives the CLI against temp fixtures.
Run: python3 test-orchestrate-resources.py"""
import json
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orchestrate-resources.py")
FAILS = []


def run(args, *, env_overrides=None, tmux="/tmp/tmux-test,1,0"):
    env = dict(os.environ)
    env.pop("TOOL_INPUT", None)
    if tmux is None:
        env.pop("TMUX", None)
    else:
        env["TMUX"] = tmux
    if env_overrides:
        env.update(env_overrides)
    p = subprocess.run([sys.executable, SCRIPT, *args], env=env,
                       capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILS.append(label)


def main():
    with tempfile.TemporaryDirectory() as td:
        state = os.path.join(td, "resources.json")
        base = os.path.join(td, "rbase")
        ov = {"ORCHESTRATE_RESOURCES_FILE": state, "ORCHESTRATE_RESOURCE_BASE": base,
              "ORCHESTRATE_PORT_RANGE": "1980-2080",
              "ORCHESTRATE_FLOOR_DIR": os.path.join(td, "floor.d")}

        # list on a fresh (absent) state file -> empty leases, exit 0, valid JSON
        rc, out, err = run(["list", "--json"], env_overrides=ov)
        check("list on absent state -> empty", rc == 0 and json.loads(out) == [])

        # corrupt state -> hard error (no silent reset)
        with open(state, "w") as f:
            f.write("{ not json")
        rc, out, err = run(["list", "--json"], env_overrides=ov)
        check("corrupt state -> hard error rc!=0", rc != 0 and "corrupt" in (out + err).lower())

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("All harness checks passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run -> expect failure (script absent)**

Run: `cd ~/Developer/claude-kit && python3 test-orchestrate-resources.py`
Expected: FAIL / error (the script does not exist yet, or `list` not implemented).

- [ ] **Step 3: Implement the core module**

Create `orchestrate-resources.py`:
```python
#!/usr/bin/env python3
"""orchestrate-resources.py - cross-session resource registry/allocator for orchestrate.

  allocate --session S --teammate T --profile P [--ports N] [--provision]
  release  (--session S --teammate T | --lease ID) [--purge]
  gc
  list [--session S] [--json]

State: $ORCHESTRATE_RESOURCES_FILE (default ~/.claude/orchestrate-resources.json),
mutated under an exclusive flock with an atomic temp+replace write.
Design: ~/.claude/skills/orchestrate/DESIGN-phase3a-resource-registry.md
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
STATE = os.environ.get("ORCHESTRATE_RESOURCES_FILE", os.path.join(HOME, ".claude", "orchestrate-resources.json"))
RESOURCE_BASE = os.environ.get("ORCHESTRATE_RESOURCE_BASE", "/tmp/orchestrate")
PORT_RANGE = os.environ.get("ORCHESTRATE_PORT_RANGE", "1980-2080")
FLOOR_DIR = os.environ.get("ORCHESTRATE_FLOOR_DIR", os.path.join(HOME, ".claude", "orchestrate-floor.d"))


def _int_env(name, default, minimum=None):
    try:
        val = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    if minimum is not None and val < minimum:
        return default
    return val


TTL_HOURS = _int_env("ORCHESTRATE_FLOOR_TTL_HOURS", 72, minimum=1)


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _marker_key():
    tmux = os.environ.get("TMUX", "")
    if not tmux:
        return ""
    return re.sub(rb'[^A-Za-z0-9]', b'_', tmux.encode("utf-8", "surrogateescape")).decode("ascii")


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
    return data


def _read_state():
    """Read without a write lock (for `list`)."""
    try:
        with open(STATE) as f:
            return _parse_state(f.read(), STATE)
    except FileNotFoundError:
        return _empty_state()


def _with_lock(mutate):
    """Open (create) STATE, take an exclusive flock, read -> mutate(state) -> atomic write.
    `mutate` returns a (new_state, result) tuple; result is returned to the caller."""
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    fd = os.open(STATE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with os.fdopen(os.dup(fd), "r") as rf:
            state = _parse_state(rf.read(), STATE)
        new_state, result = mutate(state)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as wf:
            json.dump(new_state, wf, indent=2)
        os.replace(tmp, STATE)
        return result
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def cmd_list(args):
    leases = _read_state()["leases"]
    if getattr(args, "session", None):
        leases = [l for l in leases if l.get("session") == args.session]
    if args.json:
        print(json.dumps(leases, indent=2))
    else:
        for l in leases:
            print(f"{l['id']:30} {l.get('profile','-'):12} "
                  f"port={l['resources'].get('port',{}).get('value','-')} "
                  f"dir={l['resources'].get('data_dir',{}).get('value','-')}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="orchestrate-resources.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("list"); pl.add_argument("--session"); pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```
Make it executable: `chmod +x orchestrate-resources.py`.

- [ ] **Step 4: Run -> expect pass**

Run: `cd ~/Developer/claude-kit && python3 test-orchestrate-resources.py`
Expected: `All harness checks passed.` (list-empty + corrupt-hard-error).
Also: `ruff check --select F orchestrate-resources.py test-orchestrate-resources.py` -> clean.

- [ ] **Step 5: Commit**

```bash
cd ~/Developer/claude-kit
git add orchestrate-resources.py test-orchestrate-resources.py
git commit -m "feat(orchestrate): resource-registry core - flock-atomic JSON state + list (P3-A)"
```

---

## Task 2: `port` + `dir` kinds + `allocate` (generic)

**Files:**
- Modify: `~/Developer/cc-orchestrator/orchestrate-resources.py`
- Modify: `~/Developer/cc-orchestrator/test-orchestrate-resources.py`

- [ ] **Step 1: Add the failing allocate tests**

Add inside `main()`'s `with td` block in the harness, after the corrupt-state check:
```python
        # allocate gives a port in range + a data dir; idempotent per (session,teammate)
        rc, out, err = run(["allocate", "--session", "A", "--teammate", "x"], env_overrides=ov)
        lease = json.loads(out)
        check("allocate exit 0 + JSON lease", rc == 0 and lease["id"] == "A/x")
        port_x = lease["resources"]["port"]["value"]
        check("allocated port in range", 1980 <= port_x <= 2080)
        check("data_dir created", os.path.isdir(lease["resources"]["data_dir"]["value"]))
        rc, out2, _ = run(["allocate", "--session", "A", "--teammate", "x"], env_overrides=ov)
        check("re-allocate same (session,teammate) is idempotent",
              json.loads(out2)["resources"]["port"]["value"] == port_x)
        # a second teammate gets a DIFFERENT port
        rc, out3, _ = run(["allocate", "--session", "A", "--teammate", "y"], env_overrides=ov)
        check("second teammate gets a distinct port",
              json.loads(out3)["resources"]["port"]["value"] != port_x)
        # a LISTENing port in range is skipped
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bound = None
        for cand in range(1980, 2081):
            try:
                s.bind(("127.0.0.1", cand)); s.listen(1); bound = cand; break
            except OSError:
                continue
        ov_one = dict(ov); ov_one["ORCHESTRATE_PORT_RANGE"] = f"{bound}-{bound}"
        rc, out4, err4 = run(["allocate", "--session", "B", "--teammate", "z"], env_overrides=ov_one)
        check("range-of-one that is LISTENing -> allocate fails (exhausted)", rc != 0)
        s.close()
```

- [ ] **Step 2: Run -> expect failure** (`allocate` subcommand missing).

Run: `cd ~/Developer/claude-kit && python3 test-orchestrate-resources.py` -> FAIL.

- [ ] **Step 3: Implement port/dir allocation**

Add to `orchestrate-resources.py` (before `build_parser`):
```python
def _port_listening(port):
    """True if something is LISTENing on the TCP port. Listener-scoped (never a bare
    lsof -ti that also matches client connections)."""
    try:
        r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False  # if lsof is unavailable, do not block allocation (the bind will fail loudly later)
    return r.returncode == 0 and bool(r.stdout.strip())


def _port_range():
    lo, _, hi = PORT_RANGE.partition("-")
    return int(lo), int(hi)


def _leased_ports(state):
    out = set()
    for l in state["leases"]:
        pv = l.get("resources", {}).get("port", {}).get("value")
        if isinstance(pv, int):
            out.add(pv)
    return out


def _free_port(state):
    lo, hi = _port_range()
    taken = _leased_ports(state)
    for p in range(lo, hi + 1):
        if p in taken:
            continue
        if _port_listening(p):
            continue
        return p
    sys.exit(f"orchestrate-resources: no free port in range {PORT_RANGE} "
             f"({len(taken)} leased) - widen ORCHESTRATE_PORT_RANGE or run `gc`.")


def _find_lease(state, lease_id):
    for l in state["leases"]:
        if l["id"] == lease_id:
            return l
    return None


def cmd_allocate(args):
    lease_id = f"{args.session}/{args.teammate}"

    def mutate(state):
        _gc_inplace(state)  # defined in Task 4; reclaim dead leases before allocating
        existing = _find_lease(state, lease_id)
        if existing:
            return state, existing  # idempotent
        port = _free_port(state)
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
    return 0
```
Add a temporary stub so Task 2 runs before Tasks 3-5 land (REMOVE these stubs when those tasks implement the real versions):
```python
def _gc_inplace(state):  # real impl in Task 4
    return state


def apply_profile(profile, lease, args):  # real impl in Task 5
    return lease


def write_env_bundle(lease):  # real impl in Task 3
    return lease
```
Wire the subcommand in `build_parser`:
```python
    pa = sub.add_parser("allocate")
    pa.add_argument("--session", required=True)
    pa.add_argument("--teammate", required=True)
    pa.add_argument("--profile", default="generic")
    pa.add_argument("--ports", type=int, default=1)   # v1 uses 1; reserved for future
    pa.add_argument("--provision", action="store_true")
    pa.set_defaults(func=cmd_allocate)
```

- [ ] **Step 4: Run -> expect pass.** `python3 test-orchestrate-resources.py` green; `ruff check --select F` clean.

- [ ] **Step 5: Commit**
```bash
cd ~/Developer/claude-kit
git add orchestrate-resources.py test-orchestrate-resources.py
git commit -m "feat(orchestrate): resource-registry allocate - port (skip leased+LISTENing) + dir + idempotent lease (P3-A)"
```

---

## Task 3: Env bundle emission (`.env` file + stdout map)

**Files:** Modify `orchestrate-resources.py` + harness.

- [ ] **Step 1: Failing test** -- add to harness after the allocate tests:
```python
        # env bundle: lease.env map populated, env_file written with KEY=VALUE lines
        rc, out, _ = run(["allocate", "--session", "C", "--teammate", "e",
                          "--profile", "generic"], env_overrides=ov)
        lease = json.loads(out)
        check("generic lease has env_file path", bool(lease["env_file"]))
        check("env_file written on disk", os.path.isfile(lease["env_file"]))
        # generic profile sets no app env, but the file + map must still exist (possibly empty)
        body = open(lease["env_file"]).read()
        check("env_file is KEY=VALUE text", all("=" in ln for ln in body.splitlines() if ln.strip()))
```

- [ ] **Step 2: Run -> FAIL** (env_file is None for generic).

- [ ] **Step 3: Implement** -- replace the `write_env_bundle` stub:
```python
def write_env_bundle(lease):
    """Write lease['env'] to <data_dir>/instance.env (KEY=VALUE per line) and record the
    path on the lease. The map is also the lease's machine-readable contract; cmd_allocate
    prints the lease JSON, and the eval-able export block is emitted by _print_exports."""
    data_dir = lease["resources"]["data_dir"]["value"]
    env_file = os.path.join(data_dir, "instance.env")
    with open(env_file, "w") as f:
        for k, v in lease["env"].items():
            f.write(f"{k}={v}\n")
    os.chmod(env_file, 0o600)  # may carry non-secret config; keep it owner-only on principle
    lease["env_file"] = env_file
    return lease
```
And add an eval-able export block to `cmd_allocate` right before `return 0` (after the JSON print):
```python
    if lease["env"]:
        sys.stderr.write("# eval-able exports (orchestrate-resources):\n")
        for k, v in lease["env"].items():
            sys.stderr.write(f"export {k}={v}\n")
```
(Exports go to STDERR so STDOUT stays pure JSON for machine parsing; the lead copies them into the tmux pane.)

- [ ] **Step 4: Run -> pass.** `ruff check --select F` clean.

- [ ] **Step 5: Commit**
```bash
cd ~/Developer/claude-kit
git add orchestrate-resources.py test-orchestrate-resources.py
git commit -m "feat(orchestrate): resource-registry env bundle - instance.env (0600) + export block (P3-A)"
```

---

## Task 4: `release` + `gc` (liveness + TTL)

**Files:** Modify `orchestrate-resources.py` + harness.

- [ ] **Step 1: Failing tests** -- add to harness:
```python
        # release frees a port for re-allocation
        rc, out, _ = run(["allocate", "--session", "D", "--teammate", "r"], env_overrides=ov)
        pr = json.loads(out)["resources"]["port"]["value"]
        ddir = json.loads(out)["resources"]["data_dir"]["value"]
        rc, _, _ = run(["release", "--session", "D", "--teammate", "r"], env_overrides=ov)
        check("release exit 0", rc == 0)
        check("released lease gone", all(l["id"] != "D/r" for l in json.loads(run(["list","--json"],env_overrides=ov)[1])))
        check("release without --purge keeps the dir", os.path.isdir(ddir))
        # --purge removes the dir
        rc, out, _ = run(["allocate", "--session", "D", "--teammate", "p"], env_overrides=ov)
        pdir = json.loads(out)["resources"]["data_dir"]["value"]
        run(["release", "--session", "D", "--teammate", "p", "--purge"], env_overrides=ov)
        check("--purge removes the dir", not os.path.exists(pdir))
        # gc reclaims a dead lease (port not listening, marker absent) but spares a listening one
        # craft a stale lease directly in the state file:
        st = json.loads(open(state).read())
        st["leases"].append({"id":"X/dead","session":"X","teammate":"dead","profile":"generic",
            "created":"2000-01-01T00:00:00Z","ttl_hours":72,"marker_key":"nope",
            "resources":{"port":{"kind":"port","value":2075},"data_dir":{"kind":"dir","value":os.path.join(base,"X","dead")}},
            "env":{},"env_file":None,"meta":{}})
        open(state,"w").write(json.dumps(st))
        rc,_,_ = run(["gc"], env_overrides=ov)
        check("gc reclaims a TTL-expired dead lease", all(l["id"]!="X/dead" for l in json.loads(run(["list","--json"],env_overrides=ov)[1])))
```

- [ ] **Step 2: Run -> FAIL** (release/gc missing).

- [ ] **Step 3: Implement.** Replace the `_gc_inplace` stub and add `cmd_release`/`cmd_gc`:
```python
def _lease_age_hours(lease):
    try:
        created = datetime.datetime.strptime(lease["created"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except (KeyError, ValueError):
        return float("inf")  # malformed timestamp -> treat as ancient (reclaimable)
    return (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds() / 3600.0


def _marker_absent(lease):
    key = lease.get("marker_key") or ""
    if not key:
        return True  # no owning marker recorded -> cannot be kept alive by one
    return not os.path.isfile(os.path.join(FLOOR_DIR, key))


def _reclaimable(lease):
    ttl = lease.get("ttl_hours", TTL_HOURS)
    if _lease_age_hours(lease) >= ttl:
        return True
    port = lease.get("resources", {}).get("port", {}).get("value")
    listening = isinstance(port, int) and _port_listening(port)
    if listening:
        return False  # a live server pins the lease regardless of marker
    data_dir = lease.get("resources", {}).get("data_dir", {}).get("value")
    dir_gone = not (data_dir and os.path.isdir(data_dir))
    return dir_gone or _marker_absent(lease)


def _gc_inplace(state):
    state["leases"] = [l for l in state["leases"] if not _reclaimable(l)]
    return state


def cmd_gc(args):
    _with_lock(lambda s: (_gc_inplace(s), None))
    return 0


def cmd_release(args):
    def mutate(state):
        if args.lease:
            target = args.lease
        elif args.session and args.teammate:
            target = f"{args.session}/{args.teammate}"
        else:
            sys.exit("release: need --lease ID or both --session and --teammate")
        lease = _find_lease(state, target)
        if lease and args.purge:
            ddir = lease.get("resources", {}).get("data_dir", {}).get("value")
            if ddir and os.path.isdir(ddir):
                import shutil
                shutil.rmtree(ddir, ignore_errors=True)
        state["leases"] = [l for l in state["leases"] if l["id"] != target]
        return state, None
    _with_lock(mutate)
    return 0
```
Wire subcommands in `build_parser`:
```python
    pr = sub.add_parser("release")
    pr.add_argument("--session"); pr.add_argument("--teammate"); pr.add_argument("--lease")
    pr.add_argument("--purge", action="store_true"); pr.set_defaults(func=cmd_release)
    pg = sub.add_parser("gc"); pg.set_defaults(func=cmd_gc)
```

- [ ] **Step 4: Run -> pass.** `ruff check --select F` clean. Also add+verify a LIVE-spare case:
```python
        # gc spares a lease whose port is currently LISTENing
        import socket as _sock
        ls = _sock.socket(); ls.setsockopt(_sock.SOL_SOCKET,_sock.SO_REUSEADDR,1)
        ls.bind(("127.0.0.1",0)); ls.listen(1); lp = ls.getsockname()[1]
        st = json.loads(open(state).read())
        st["leases"].append({"id":"L/live","session":"L","teammate":"live","profile":"generic",
            "created":_nowish(),"ttl_hours":72,"marker_key":"nope",
            "resources":{"port":{"kind":"port","value":lp},"data_dir":{"kind":"dir","value":td}},
            "env":{},"env_file":None,"meta":{}})
        open(state,"w").write(json.dumps(st))
        run(["gc"], env_overrides=ov)
        check("gc spares a LISTENing lease", any(l["id"]=="L/live" for l in json.loads(run(["list","--json"],env_overrides=ov)[1])))
        ls.close()
```
Add a `_nowish()` helper at the top of the harness: `import datetime as _dt` then `def _nowish(): return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")`.

- [ ] **Step 5: Commit**
```bash
cd ~/Developer/claude-kit
git add orchestrate-resources.py test-orchestrate-resources.py
git commit -m "feat(orchestrate): resource-registry release/--purge + gc (liveness probe + TTL) (P3-A)"
```

---

## Task 5: `stillwater` profile + `--provision` (key file beside DB, 0600, never in env)

**Files:** Modify `orchestrate-resources.py` + harness.

- [ ] **Step 1: Failing tests** -- add to harness:
```python
        # stillwater profile: emits SW_* env (NO secret), writes instance.env, errors on missing config
        realkey = os.path.join(td, "real-encryption.key"); open(realkey,"w").write("DEADBEEF\n"); os.chmod(realkey,0o600)
        sw = dict(ov); sw.update({"ORCHESTRATE_STILLWATER_KEYFILE": realkey, "ORCHESTRATE_STILLWATER_MUSIC": "/music"})
        rc, out, err = run(["allocate","--session","S","--teammate","a","--profile","stillwater"], env_overrides=sw)
        lease = json.loads(out)
        e = lease["env"]
        check("stillwater emits SW_PORT/SW_DB_PATH/SW_BACKUP_PATH/SW_MUSIC_PATH",
              all(k in e for k in ("SW_PORT","SW_DB_PATH","SW_BACKUP_PATH","SW_MUSIC_PATH")))
        check("stillwater NEVER puts the key in env", "SW_ENCRYPTION_KEY" not in e)
        check("SW_DB_PATH under the data dir", e["SW_DB_PATH"].startswith(lease["resources"]["data_dir"]["value"]))
        # missing required config -> clear error
        rc2, _, err2 = run(["allocate","--session","S","--teammate","b","--profile","stillwater"], env_overrides=ov)
        check("stillwater missing KEYFILE config -> error naming the var",
              rc2 != 0 and "ORCHESTRATE_STILLWATER_KEYFILE" in err2)
        # --provision: places encryption.key (0600) beside DB + mkdir backups (no real DB to copy here, so seed one)
        srcdb = os.path.join(td,"src.db"); open(srcdb,"wb").write(b"sqlite-stub")
        swp = dict(sw); swp["ORCHESTRATE_STILLWATER_DB"] = srcdb
        rc, out, _ = run(["allocate","--session","S","--teammate","c","--profile","stillwater","--provision"], env_overrides=swp)
        lease = json.loads(out); dd = lease["resources"]["data_dir"]["value"]
        keyf = os.path.join(dd,"encryption.key")
        check("--provision places encryption.key beside DB", os.path.exists(keyf))
        check("placed key is 0600", (os.stat(keyf).st_mode & 0o777) == 0o600)
        check("--provision creates backups dir", os.path.isdir(os.path.join(dd,"backups")))
        check("--provision seeds the DB copy", os.path.exists(os.path.join(dd,"stillwater.db")))
```

- [ ] **Step 2: Run -> FAIL** (profile is a no-op stub).

- [ ] **Step 3: Implement.** Replace the `apply_profile` stub:
```python
def _require_cfg(name):
    val = os.environ.get(name, "")
    if not val:
        sys.exit(f"orchestrate-resources: --profile stillwater requires {name} to be set "
                 "(no real path is guessed). Set it and retry.")
    return val


def _profile_stillwater(lease, args):
    data_dir = lease["resources"]["data_dir"]["value"]
    port = lease["resources"]["port"]["value"]
    keyfile = _require_cfg("ORCHESTRATE_STILLWATER_KEYFILE")
    music = _require_cfg("ORCHESTRATE_STILLWATER_MUSIC")
    db_path = os.path.join(data_dir, "stillwater.db")
    # Env bundle - NO secret. The key is supplied as a file beside the DB (see provisioning).
    lease["env"] = {
        "SW_PORT": str(port),
        "SW_DB_PATH": db_path,
        "SW_BACKUP_PATH": os.path.join(data_dir, "backups"),
        "SW_MUSIC_PATH": music,
        "SW_LOG_FORMAT": "text",
        "SW_LOG_LEVEL": "debug",
    }
    lease["meta"]["keyfile_src"] = keyfile
    setup_hint = [
        f"mkdir -p {os.path.join(data_dir, 'backups')}",
        f"ln -sfn {keyfile} {os.path.join(data_dir, 'encryption.key')}  # real key, beside DB (0600 via target)",
    ]
    src_db = os.environ.get("ORCHESTRATE_STILLWATER_DB", "")
    if src_db:
        setup_hint = [f"cp {src_db} {db_path}",
                      f"rm -f {db_path}-wal {db_path}-shm"] + setup_hint
    lease["meta"]["setup_hint"] = setup_hint
    if args.provision:
        _provision_stillwater(lease, data_dir, db_path, keyfile, src_db)


def _provision_stillwater(lease, data_dir, db_path, keyfile, src_db):
    os.makedirs(os.path.join(data_dir, "backups"), exist_ok=True)
    if src_db and os.path.exists(src_db):
        import shutil
        shutil.copyfile(src_db, db_path)
        for sidecar in (db_path + "-wal", db_path + "-shm"):
            try:
                os.remove(sidecar)
            except OSError:
                pass
    dst_key = os.path.join(data_dir, "encryption.key")
    # Prefer a symlink (target keeps its 0600); fall back to a 0600 copy if symlink fails.
    try:
        if os.path.lexists(dst_key):
            os.remove(dst_key)
        os.symlink(keyfile, dst_key)
    except OSError:
        import shutil
        # copy then enforce 0600 explicitly (never trust cp/copy default perms - see issue #1880)
        fd = os.open(dst_key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out, open(keyfile, "rb") as src:
            out.write(src.read())
        os.chmod(dst_key, 0o600)


def apply_profile(profile, lease, args):
    if profile == "stillwater":
        _profile_stillwater(lease, args)
    # "generic" (default): no app env; the lease still carries an empty env bundle.
    return lease
```
NOTE on the 0600 check in the test: a SYMLINK's `os.stat` follows to the target (the real key is 0600), so the assertion holds for the symlink path. The copy path enforces 0600 explicitly.

- [ ] **Step 4: Run -> pass.** `ruff check --select F` clean.

- [ ] **Step 5: VERIFY the encryption-key mechanism against the live source (trust-but-verify, do NOT skip).**
Confirm `cmd/stillwater/main.go` `resolveEncryptionKey` still resolves the key from an `encryption.key` file in `dirname(SW_DB_PATH)` (priority after `SW_ENCRYPTION_KEY`). Run:
`grep -n 'encryption.key\|filepath.Dir(cfg.Database.Path)\|SW_ENCRYPTION_KEY' ~/Developer/stillwater/cmd/stillwater/main.go`
Expected: the file-beside-DB path is still derived from the DB dir. If the mechanism changed, STOP and revise the profile before continuing.

- [ ] **Step 6: Commit**
```bash
cd ~/Developer/claude-kit
git add orchestrate-resources.py test-orchestrate-resources.py
git commit -m "feat(orchestrate): stillwater profile - env bundle (no secret) + --provision key-file-beside-DB 0600 (P3-A)"
```

---

## Task 6: Integrate `orchestrate-setup.py down` -> release/gc the session's leases

**Files:** Modify `orchestrate-setup.py` + `test-orchestrate-setup.py`.

- [ ] **Step 1: Failing test** -- in `test-orchestrate-setup.py`, in the Task-7 down area, add a resources state file with a lease for THIS session's team and assert `down` releases it. Add near the other down checks:
```python
        # down releases this session's resource leases (best-effort integration)
        res_state = os.path.join(td, "resources.json")
        json.dump({"version":1,"leases":[
            {"id":"demo/impl","session":"demo","teammate":"impl","profile":"generic",
             "created":"2000-01-01T00:00:00Z","ttl_hours":72,"marker_key":"",
             "resources":{"port":{"kind":"port","value":2099},"data_dir":{"kind":"dir","value":td}},
             "env":{},"env_file":None,"meta":{}}]}, open(res_state,"w"))
        rc, out = run(["down","--team","demo"], env_overrides={"ORCHESTRATE_FLOOR_DIR": floor_dir,
                                                               "ORCHESTRATE_RESOURCES_FILE": res_state})
        remaining = json.load(open(res_state))["leases"]
        check("down released the demo session's lease", all(l["session"]!="demo" for l in remaining))
```
(Requires `down` to accept/forward `--team` as the session key. If `down` has no `--team`, add it; default to releasing by the team passed.)

- [ ] **Step 2: Run -> FAIL** (`test-orchestrate-setup.py`).

- [ ] **Step 3: Implement** -- in `orchestrate-setup.py` `cmd_down`, after the marker removal + `_gc_stale_tombstones()`, add a best-effort call to the resources CLI for the session's leases:
```python
def _release_session_resources(team):
    """Best-effort: release this session's resource leases. Never fatal."""
    if not team:
        return
    resources = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orchestrate-resources.py")
    state = os.environ.get("ORCHESTRATE_RESOURCES_FILE")
    if not os.path.exists(resources):
        return
    try:
        out = subprocess.run([sys.executable, resources, "list", "--session", team, "--json"],
                             capture_output=True, text=True, timeout=15)
        leases = json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else []
        for l in leases:
            subprocess.run([sys.executable, resources, "release", "--lease", l["id"]],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass  # teardown hygiene must never crash down
```
Call it in `cmd_down` (pass `args.team` -- add a `--team` arg to the down subparser if absent). The subprocess inherits `ORCHESTRATE_RESOURCES_FILE` from the env, so the harness fixture is honored.

- [ ] **Step 4: Run -> pass** BOTH harnesses: `python3 test-orchestrate-setup.py` and `python3 test-orchestrate-resources.py`. `ruff check --select F` clean on both files.

- [ ] **Step 5: Commit**
```bash
cd ~/Developer/claude-kit
git add orchestrate-setup.py test-orchestrate-setup.py
git commit -m "feat(orchestrate): down releases the session's resource leases (best-effort) (P3-A)"
```

---

## Task 7: Deploy symlink + docs + adversarial critic gate

**Files:** symlink; `SKILL.md`; `ROADMAP-phase3.md`; `SESSION-STATE.md`.

- [ ] **Step 1: Deploy by symlink** (claude-kit canonical -> ~/.claude/scripts):
```bash
ln -sfn ~/Developer/cc-orchestrator/orchestrate-resources.py ~/.claude/scripts/orchestrate-resources.py
ls -l ~/.claude/scripts/orchestrate-resources.py
```

- [ ] **Step 2: Docs** -- in `SKILL.md` setup sequence + dispatch section, document: the lead calls `orchestrate-resources.py allocate --session <team> --teammate <name> --profile stillwater [--provision]` per teammate, EXPORTS the printed env into the teammate's tmux pane (authoritative; wins over `.env` per the spec), and `down` auto-releases. Note ports/dirs are leased (no hand-picked fixed ports). Mark the P3-A "resource registry" sub-item done in `ROADMAP-phase3.md` and update the `SESSION-STATE.md` banner.

- [ ] **Step 3: Adversarial critic pass (1-2 rounds, read-only).** Dispatch hostile critics against `orchestrate-resources.py` + harness + the `down` integration. Hunt: duplicate-port under concurrent allocate (flock correctness), GC reclaiming a LISTENing/live lease, port-range exhaustion handling, `instance.env`/secret leakage (assert the key is NEVER in env/env_file), provision perms (key must be 0600 even on the copy fallback), corrupt-state safety, release/idempotency races. Verify findings by reproduction through the harness. Fix real findings as their own TDD cycle; re-run BOTH harnesses + `ruff` green. Converge at 1-2 dry rounds (non-security-floor scope).

- [ ] **Step 4: Final gate.**
```bash
cd ~/Developer/claude-kit && python3 test-orchestrate-resources.py && python3 test-orchestrate-setup.py && ruff check --select F orchestrate-resources.py test-orchestrate-resources.py orchestrate-setup.py test-orchestrate-setup.py
```
Expected: both harnesses pass, ruff clean.

- [ ] **Step 5: Commit + report (NO push).**
```bash
cd ~/Developer/claude-kit
git add orchestrate-resources.py test-orchestrate-resources.py orchestrate-setup.py test-orchestrate-setup.py
git commit -m "docs(orchestrate): wire resource-registry into SKILL + deploy symlink (P3-A)"
```
Report: files, commits, harness/critic results, the symlink, and that nothing was pushed.

---

## Notes for the implementer

- `~/Developer/claude-kit` IS a git repo; commit locally freely. A Stop-hook auto-syncs it to the gist -- do NOT manually push.
- The cross-task stubs in Task 2 (`_gc_inplace`, `apply_profile`, `write_env_bundle`) are replaced by the real implementations in Tasks 3-5; if you implement out of order, keep the signatures identical.
- macOS/darwin tool: `lsof -nP -iTCP:<p> -sTCP:LISTEN` is the listener-scoped probe (never bare `lsof -ti:PORT`, which also matches client connections and has reaped the user's browser before).
- Keep STDOUT pure JSON for `allocate`/`list --json` (machine-parseable); human/eval text goes to STDERR.
