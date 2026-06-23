#!/usr/bin/env python3
"""Proof harness for orchestrate-resources.py. Drives the CLI against temp fixtures.
Run: python3 test-orchestrate-resources.py"""
import datetime as _dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile


def _nowish():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "orchestrate-resources.py")
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
        floor_d = os.path.join(td, "floor.d")
        ov = {"ORCHESTRATE_RESOURCES_FILE": state, "ORCHESTRATE_RESOURCE_BASE": base,
              "ORCHESTRATE_PORT_RANGE": "1980-2080",
              "ORCHESTRATE_FLOOR_DIR": floor_d}

        # The harness uses TMUX=/tmp/tmux-test,1,0; its marker_key is "_tmp_tmux_test_1_0".
        # Create the marker file so GC doesn't reclaim leases the harness just allocated
        # (GC reclaims when port not listening AND (dir gone OR marker absent)).
        os.makedirs(floor_d, exist_ok=True)
        open(os.path.join(floor_d, "_tmp_tmux_test_1_0"), "w").close()

        # list on a fresh (absent) state file -> empty leases, exit 0, valid JSON
        rc, out, err = run(["list", "--json"], env_overrides=ov)
        check("list on absent state -> empty", rc == 0 and json.loads(out) == [])

        # corrupt state -> hard error (no silent reset)
        with open(state, "w") as f:
            f.write("{ not json")
        rc, out, err = run(["list", "--json"], env_overrides=ov)
        check("corrupt state -> hard error rc!=0", rc != 0 and "corrupt" in (out + err).lower())

        # reset state for allocate tests
        os.remove(state)

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
        # a LISTENing port in range is skipped. Bind an OS-assigned EPHEMERAL port
        # (bind to :0, read the assigned number) and drive the allocator's range to
        # exactly that port, instead of scanning a fixed range and hoping one is free.
        # Scanning a fixed range flaked under port contention (#176): a candidate port
        # could be occupied non-deterministically, so the case passed standalone but
        # failed under load. An ephemeral bind is always free at bind time (mirrors the
        # bind(("127.0.0.1", 0)) pattern used elsewhere in this file).
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0)); s.listen(1)
        bound = s.getsockname()[1]
        ov_one = dict(ov); ov_one["ORCHESTRATE_PORT_RANGE"] = f"{bound}-{bound}"
        rc, out4, err4 = run(["allocate", "--session", "B", "--teammate", "z"], env_overrides=ov_one)
        check("range-of-one that is LISTENing -> allocate fails (exhausted) with a clear msg",
              rc != 0 and "no free port" in (out4 + err4).lower())
        s.close()

        # --- #98: single-call lsof enumeration (not one lsof per port) ---
        # A fake `lsof` on PATH records each invocation and reports ONE listener (n*:TARGET).
        # allocate must (a) call lsof exactly ONCE for the whole scan (was ~101, one per port),
        # and (b) still skip the reported listening port.
        print("\n  [#98: single-call lsof scan]")
        lsofdir = os.path.join(td, "lsofbin")
        os.makedirs(lsofdir, exist_ok=True)
        counter = os.path.join(td, "lsof-calls.log")
        fake_lsof = os.path.join(lsofdir, "lsof")
        with open(fake_lsof, "w") as f:
            f.write('#!/bin/sh\n'
                    'echo x >> "$LSOF_FAKE_COUNTER"\n'
                    'printf "p1\\nn*:%s\\n" "$LSOF_FAKE_TARGET"\n')
        os.chmod(fake_lsof, 0o755)
        target = 2000
        ov98 = dict(ov)
        ov98["ORCHESTRATE_PORT_RANGE"] = f"{target}-{target + 1}"  # 2 ports; target is "listening"
        ov98["PATH"] = lsofdir + os.pathsep + os.environ.get("PATH", "")
        ov98["LSOF_FAKE_COUNTER"] = counter
        ov98["LSOF_FAKE_TARGET"] = str(target)
        rc, out98, err98 = run(["allocate", "--session", "S98", "--teammate", "t"], env_overrides=ov98)
        check("#98: allocate exits 0 with fake single-call lsof", rc == 0)
        if rc == 0:
            check("#98: the lsof-reported LISTENing port is skipped",
                  json.loads(out98)["resources"]["port"]["value"] == target + 1)
        ncalls = sum(1 for _ in open(counter)) if os.path.exists(counter) else 0
        check(f"#98: lsof invoked ONCE for the whole scan (not per-port); got {ncalls}", ncalls == 1)

        # --- #98: lsof failure (or absence) does not block allocation ---
        # A fake lsof that exits non-zero with no output -> empty snapshot -> allocate proceeds.
        faildir = os.path.join(td, "lsoffail")
        os.makedirs(faildir, exist_ok=True)
        with open(os.path.join(faildir, "lsof"), "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(faildir, "lsof"), 0o755)
        ovfail = dict(ov)
        ovfail["ORCHESTRATE_PORT_RANGE"] = "2050-2051"
        ovfail["PATH"] = faildir + os.pathsep + os.environ.get("PATH", "")
        rc, outf, errf = run(["allocate", "--session", "S98f", "--teammate", "t"], env_overrides=ovfail)
        check("#98: lsof failure does not block allocation (port still granted)",
              rc == 0 and 2050 <= json.loads(outf)["resources"]["port"]["value"] <= 2051)

        # invalid session/teammate (slash) is rejected cleanly (no path nesting / id collision)
        rc, out, err = run(["allocate", "--session", "a/b", "--teammate", "x"], env_overrides=ov)
        check("allocate rejects '/' in session", rc != 0 and "invalid" in (out + err).lower())

        # --- Task 3: env bundle emission ---
        # generic lease has env_file path set + file written on disk
        rc, out, _ = run(["allocate", "--session", "C", "--teammate", "e",
                          "--profile", "generic"], env_overrides=ov)
        lease = json.loads(out)
        check("generic lease has env_file path", bool(lease["env_file"]))
        check("env_file written on disk", os.path.isfile(lease["env_file"]))
        # generic profile sets no app env, but the file + map must still exist (possibly empty)
        body = open(lease["env_file"]).read()
        check("env_file is KEY=VALUE text (or empty)", all("=" in ln for ln in body.splitlines() if ln.strip()))
        # env_file must be owner-only (0600)
        check("env_file is 0600", (os.stat(lease["env_file"]).st_mode & 0o777) == 0o600)

        # --- Task 4: release + gc ---
        # release frees a port for re-allocation
        rc, out, _ = run(["allocate", "--session", "D", "--teammate", "r"], env_overrides=ov)
        ddir = json.loads(out)["resources"]["data_dir"]["value"]
        rc, _, _ = run(["release", "--session", "D", "--teammate", "r"], env_overrides=ov)
        check("release exit 0", rc == 0)
        check("released lease gone", all(lease["id"] != "D/r" for lease in json.loads(run(["list", "--json"], env_overrides=ov)[1])))
        check("release without --purge keeps the dir", os.path.isdir(ddir))
        # --purge removes the dir
        rc, out, _ = run(["allocate", "--session", "D", "--teammate", "p"], env_overrides=ov)
        pdir = json.loads(out)["resources"]["data_dir"]["value"]
        run(["release", "--session", "D", "--teammate", "p", "--purge"], env_overrides=ov)
        check("--purge removes the dir", not os.path.exists(pdir))
        # gc reclaims a dead lease (port not listening, marker absent) but spares a listening one
        # craft a stale lease directly in the state file:
        st = json.loads(open(state).read())
        st["leases"].append({"id": "X/dead", "session": "X", "teammate": "dead", "profile": "generic",
            "created": "2000-01-01T00:00:00Z", "ttl_hours": 72, "marker_key": "nope",
            "resources": {"port": {"kind": "port", "value": 2075}, "data_dir": {"kind": "dir", "value": os.path.join(base, "X", "dead")}},
            "env": {}, "env_file": None, "meta": {}})
        open(state, "w").write(json.dumps(st))
        rc, _, _ = run(["gc"], env_overrides=ov)
        check("gc reclaims a TTL-expired dead lease", all(lease["id"] != "X/dead" for lease in json.loads(run(["list", "--json"], env_overrides=ov)[1])))
        # gc spares a lease whose port is currently LISTENing
        import socket as _sock
        ls = _sock.socket()
        ls.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
        ls.bind(("127.0.0.1", 0))
        ls.listen(1)
        lp = ls.getsockname()[1]
        st = json.loads(open(state).read())
        st["leases"].append({"id": "L/live", "session": "L", "teammate": "live", "profile": "generic",
            "created": _nowish(), "ttl_hours": 72, "marker_key": "nope",
            "resources": {"port": {"kind": "port", "value": lp}, "data_dir": {"kind": "dir", "value": td}},
            "env": {}, "env_file": None, "meta": {}})
        open(state, "w").write(json.dumps(st))
        run(["gc"], env_overrides=ov)
        check("gc spares a LISTENing lease", any(lease["id"] == "L/live" for lease in json.loads(run(["list", "--json"], env_overrides=ov)[1])))
        ls.close()

        # I-2 (core invariant): concurrent allocate across separate PROCESSES yields NO
        # duplicate port (flock serializes the read-modify-write). 8 real subprocesses.
        import concurrent.futures
        cstate = os.path.join(td, "conc.json")
        cov = {"ORCHESTRATE_RESOURCES_FILE": cstate,
               "ORCHESTRATE_RESOURCE_BASE": os.path.join(td, "cbase"),
               "ORCHESTRATE_PORT_RANGE": "1980-2080",
               "ORCHESTRATE_FLOOR_DIR": os.path.join(td, "floor.d")}

        def _alloc(i):
            rc, out, _ = run(["allocate", "--session", "P", "--teammate", f"t{i}"], env_overrides=cov)
            return json.loads(out)["resources"]["port"]["value"] if rc == 0 else None

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            ports = list(ex.map(_alloc, range(8)))
        check("concurrent allocate: all 8 succeeded", all(p is not None for p in ports))
        check("concurrent allocate: NO duplicate ports (flock serialized)",
              len(set(ports)) == len(ports))

        # Startup grace: a fresh lease in a session with NO floor marker must survive the
        # next allocate's lazy gc (its server has not started listening yet). No marker dir.
        gstate = os.path.join(td, "grace.json")
        gov = {"ORCHESTRATE_RESOURCES_FILE": gstate,
               "ORCHESTRATE_RESOURCE_BASE": os.path.join(td, "gbase"),
               "ORCHESTRATE_PORT_RANGE": "1980-2080",
               "ORCHESTRATE_FLOOR_DIR": os.path.join(td, "no-such-floor.d")}
        rcf, _, _ = run(["allocate", "--session", "G", "--teammate", "fresh"], env_overrides=gov, tmux=None)
        rco, _, _ = run(["allocate", "--session", "G", "--teammate", "other"], env_overrides=gov, tmux=None)  # triggers lazy gc
        check("grace test: both allocates succeeded (gc actually ran)", rcf == 0 and rco == 0)
        survived = any(le["id"] == "G/fresh" for le in json.loads(run(["list", "--json"], env_overrides=gov)[1]))
        check("startup grace: fresh no-marker lease survives the next allocate's gc", survived)
        # With grace disabled, the same no-marker, not-listening lease IS reclaimable by gc.
        gov0 = dict(gov); gov0["ORCHESTRATE_LEASE_GRACE_SECONDS"] = "0"
        run(["gc"], env_overrides=gov0, tmux=None)
        gone = all(le["id"] != "G/fresh" for le in json.loads(run(["list", "--json"], env_overrides=gov0)[1]))
        check("grace=0: a no-marker, not-listening lease is reclaimed by gc", gone)

        # --- Task 5: stillwater profile + --provision ---
        print("\n  [Task 5: stillwater profile]")
        realkey = os.path.join(td, "real-encryption.key")
        open(realkey, "w").write("DEADBEEF\n")
        os.chmod(realkey, 0o600)
        sw = dict(ov)
        sw.update({"ORCHESTRATE_STILLWATER_KEYFILE": realkey, "ORCHESTRATE_STILLWATER_MUSIC": "/music"})
        rc, out, err = run(["allocate", "--session", "S", "--teammate", "a", "--profile", "stillwater"], env_overrides=sw)
        lease = json.loads(out)
        e = lease["env"]
        check("stillwater emits SW_PORT/SW_DB_PATH/SW_BACKUP_PATH/SW_MUSIC_PATH",
              all(k in e for k in ("SW_PORT", "SW_DB_PATH", "SW_BACKUP_PATH", "SW_MUSIC_PATH")))
        check("stillwater emits SW_LOG_FORMAT and SW_LOG_LEVEL",
              "SW_LOG_FORMAT" in e and "SW_LOG_LEVEL" in e)
        check("stillwater NEVER puts the key in env", "SW_ENCRYPTION_KEY" not in e)
        check("SW_DB_PATH under the data dir", e["SW_DB_PATH"].startswith(lease["resources"]["data_dir"]["value"]))
        check("SW_MUSIC_PATH matches configured value", e["SW_MUSIC_PATH"] == "/music")
        check("SW_PORT matches allocated port", e["SW_PORT"] == str(lease["resources"]["port"]["value"]))
        # env_file must exist and not contain the key
        check("stillwater env_file written", os.path.isfile(lease["env_file"]))
        env_body = open(lease["env_file"]).read()
        check("stillwater env_file never contains SW_ENCRYPTION_KEY", "SW_ENCRYPTION_KEY" not in env_body)
        # missing required config -> clear error naming the var
        rc2, out2, err2 = run(["allocate", "--session", "S", "--teammate", "b", "--profile", "stillwater"], env_overrides=ov)
        check("stillwater missing KEYFILE config -> error naming ORCHESTRATE_STILLWATER_KEYFILE",
              rc2 != 0 and "ORCHESTRATE_STILLWATER_KEYFILE" in (out2 + err2))
        # missing MUSIC var -> error naming it
        sw_nomusic = dict(ov)
        sw_nomusic["ORCHESTRATE_STILLWATER_KEYFILE"] = realkey
        rc3, out3, err3 = run(["allocate", "--session", "S", "--teammate", "c2", "--profile", "stillwater"], env_overrides=sw_nomusic)
        check("stillwater missing MUSIC config -> error naming ORCHESTRATE_STILLWATER_MUSIC",
              rc3 != 0 and "ORCHESTRATE_STILLWATER_MUSIC" in (out3 + err3))
        # KEYFILE pointing at a nonexistent path -> error (else --provision makes a dangling
        # key symlink and the binary silently auto-generates a wrong key).
        sw_badkey = dict(sw); sw_badkey["ORCHESTRATE_STILLWATER_KEYFILE"] = os.path.join(td, "nope.key")
        rc4, out4, err4 = run(["allocate", "--session", "S", "--teammate", "c3", "--profile", "stillwater"], env_overrides=sw_badkey)
        check("stillwater nonexistent KEYFILE -> error (no dangling key symlink)",
              rc4 != 0 and "does not exist" in (out4 + err4).lower())
        # both required vars missing at once -> ONE error naming BOTH (F2: validate
        # all required config up front, not one hard-fail per missing key).
        check("stillwater both vars missing -> single error names BOTH",
              rc2 != 0 and "ORCHESTRATE_STILLWATER_KEYFILE" in (out2 + err2)
              and "ORCHESTRATE_STILLWATER_MUSIC" in (out2 + err2))
        # --- F2(c): stillwater config fallback to <team-dir>/profile.env ---
        # When the ORCHESTRATE_STILLWATER_* vars are NOT in the env, allocate (stillwater)
        # reads them from <team-dir>/profile.env, where team-dir is ARTIFACTS/<session>
        # (same convention `up` uses). Real os.environ values still WIN over the file.
        print("\n  [F2(c): profile.env fallback]")
        art = os.path.join(td, "artifacts")
        pe_ov = dict(ov); pe_ov["ORCHESTRATE_ARTIFACT_DIR"] = art
        pkey = os.path.join(td, "pe.key"); open(pkey, "w").write("PK\n"); os.chmod(pkey, 0o600)
        # session name becomes the team-dir component, like `up --team <session>`.
        pe_team_dir = os.path.join(art, "PE"); os.makedirs(pe_team_dir, exist_ok=True)
        with open(os.path.join(pe_team_dir, "profile.env"), "w") as pf:
            pf.write(f"export ORCHESTRATE_STILLWATER_KEYFILE={pkey}\n")
            pf.write("export ORCHESTRATE_STILLWATER_MUSIC=/pe-music\n")
        # env UNSET for the two required vars + profile.env present -> allocate succeeds.
        rc, out, err = run(["allocate", "--session", "PE", "--teammate", "a",
                            "--profile", "stillwater"], env_overrides=pe_ov)
        check("profile.env fallback: allocate succeeds with env UNSET (rc0)", rc == 0)
        if rc == 0:
            le = json.loads(out)
            check("profile.env fallback: SW_MUSIC_PATH read from file",
                  le["env"]["SW_MUSIC_PATH"] == "/pe-music")
            check("profile.env fallback: keyfile from file recorded",
                  le["meta"].get("keyfile_src") == pkey)
        # env SET wins over the file (precedence: os.environ first, then profile.env).
        winkey = os.path.join(td, "win.key"); open(winkey, "w").write("W\n"); os.chmod(winkey, 0o600)
        pe_win = dict(pe_ov)
        pe_win["ORCHESTRATE_STILLWATER_KEYFILE"] = winkey
        pe_win["ORCHESTRATE_STILLWATER_MUSIC"] = "/env-music"
        rc, out, err = run(["allocate", "--session", "PE", "--teammate", "b",
                            "--profile", "stillwater"], env_overrides=pe_win)
        check("profile.env precedence: env-set MUSIC wins over file (rc0)", rc == 0)
        if rc == 0:
            lew = json.loads(out)
            check("profile.env precedence: SW_MUSIC_PATH is the env value, not the file's",
                  lew["env"]["SW_MUSIC_PATH"] == "/env-music")
            check("profile.env precedence: keyfile is the env value, not the file's",
                  lew["meta"].get("keyfile_src") == winkey)
        # missing in BOTH env and file -> still errors clearly naming the var.
        empty_team_dir = os.path.join(art, "EMPTY"); os.makedirs(empty_team_dir, exist_ok=True)
        open(os.path.join(empty_team_dir, "profile.env"), "w").close()  # present but empty
        rc, out, err = run(["allocate", "--session", "EMPTY", "--teammate", "a",
                            "--profile", "stillwater"], env_overrides=pe_ov)
        check("profile.env: missing in BOTH -> error names ORCHESTRATE_STILLWATER_KEYFILE",
              rc != 0 and "ORCHESTRATE_STILLWATER_KEYFILE" in (out + err))

        # --provision: places encryption.key (0600) beside DB + mkdir backups + seeds DB copy.
        # src DB is a real SQLite file (the backup-API provisioner requires a valid DB).
        srcdb = os.path.join(td, "src.db")
        _seed = sqlite3.connect(srcdb)
        _seed.execute("CREATE TABLE t (v TEXT)")
        _seed.execute("INSERT INTO t VALUES ('seed')")
        _seed.commit()
        _seed.close()
        swp = dict(sw)
        swp["ORCHESTRATE_STILLWATER_DB"] = srcdb
        rc, out, _ = run(["allocate", "--session", "S", "--teammate", "c", "--profile", "stillwater", "--provision"], env_overrides=swp)
        lease = json.loads(out)
        dd = lease["resources"]["data_dir"]["value"]
        keyf = os.path.join(dd, "encryption.key")
        check("--provision places encryption.key beside DB", os.path.exists(keyf))
        check("placed key is 0600", (os.stat(keyf).st_mode & 0o777) == 0o600)
        check("--provision creates backups dir", os.path.isdir(os.path.join(dd, "backups")))
        check("--provision seeds the DB copy", os.path.exists(os.path.join(dd, "stillwater.db")))
        check("--provision env_file never contains secret", "SW_ENCRYPTION_KEY" not in open(lease["env_file"]).read())
        # --provision without a src db: backups created, encryption.key placed, no DB copy needed
        swp2 = dict(sw)
        # no ORCHESTRATE_STILLWATER_DB
        swp2.pop("ORCHESTRATE_STILLWATER_DB", None)
        rc, out, _ = run(["allocate", "--session", "S", "--teammate", "d", "--profile", "stillwater", "--provision"], env_overrides=swp2)
        lease2 = json.loads(out)
        dd2 = lease2["resources"]["data_dir"]["value"]
        check("--provision no-srcdb: backups dir created", os.path.isdir(os.path.join(dd2, "backups")))
        check("--provision no-srcdb: encryption.key placed", os.path.exists(os.path.join(dd2, "encryption.key")))

        # --provision WAL freshness (F3): the leased copy must include commits still
        # resident in the live WAL, not just rows already folded into the main .db.
        # Build a WAL-mode source: 'in-main' is checkpointed into the .db file, then
        # autocheckpoint is disabled and 'in-wal' is committed so it stays in the WAL.
        # Keep `live` OPEN across the subprocess so close-on-exit cannot checkpoint it.
        waldb = os.path.join(td, "wal-src.db")
        live = sqlite3.connect(waldb)
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("CREATE TABLE t (v TEXT)")
        live.execute("INSERT INTO t VALUES ('in-main')")
        live.commit()
        live.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # fold 'in-main' into the .db
        live.execute("PRAGMA wal_autocheckpoint=0")        # stop auto-checkpoint
        live.execute("INSERT INTO t VALUES ('in-wal')")
        live.commit()                                       # stays in the WAL only
        swwal = dict(sw); swwal["ORCHESTRATE_STILLWATER_DB"] = waldb
        rc, out, _ = run(["allocate", "--session", "S", "--teammate", "wal",
                          "--profile", "stillwater", "--provision"], env_overrides=swwal)
        lease3 = json.loads(out)
        copydb = os.path.join(lease3["resources"]["data_dir"]["value"], "stillwater.db")
        conn = sqlite3.connect(copydb)
        rows = sorted(r[0] for r in conn.execute("SELECT v FROM t").fetchall())
        conn.close()
        live.close()
        check("--provision copy includes WAL-resident commits (F3 point-in-time)",
              rows == ["in-main", "in-wal"])

        # --- Fix 1: path traversal -- '..' in session/teammate rejected ---
        print("\n  [Fix 1: path traversal guard]")
        rc, out, err = run(["allocate", "--session", "..", "--teammate", "x"], env_overrides=ov)
        check("fix1: '..' session rejected nonzero", rc != 0)
        check("fix1: '..' session gives clear message", "invalid" in (out + err).lower())
        # Confirm no dir was created outside RESOURCE_BASE
        check("fix1: no dir escaped RESOURCE_BASE", not os.path.isdir(os.path.normpath(os.path.join(base, "..", "x"))))
        rc, out, err = run(["allocate", "--session", "valid", "--teammate", ".."], env_overrides=ov)
        check("fix1: '..' teammate rejected nonzero", rc != 0)
        check("fix1: '..' teammate gives clear message", "invalid" in (out + err).lower())
        # single dot also rejected
        rc, out, err = run(["allocate", "--session", ".", "--teammate", "x"], env_overrides=ov)
        check("fix1: '.' session rejected nonzero", rc != 0)

        # --- Fix 2: GC liveness-over-TTL -- ancient listening lease must survive ---
        print("\n  [Fix 2: liveness-over-TTL]")
        import socket as _sock2
        ls2 = _sock2.socket(_sock2.AF_INET, _sock2.SOCK_STREAM)
        ls2.setsockopt(_sock2.SOL_SOCKET, _sock2.SO_REUSEADDR, 1)
        ls2.bind(("127.0.0.1", 0))
        ls2.listen(1)
        lp2 = ls2.getsockname()[1]
        st2 = json.loads(open(state).read())
        st2["leases"].append({"id": "Z/ancient", "session": "Z", "teammate": "ancient",
            "profile": "generic", "created": "2000-01-01T00:00:00Z", "ttl_hours": 72,
            "marker_key": "nope",
            "resources": {"port": {"kind": "port", "value": lp2},
                          "data_dir": {"kind": "dir", "value": td}},
            "env": {}, "env_file": None, "meta": {}})
        open(state, "w").write(json.dumps(st2))
        run(["gc"], env_overrides=ov)
        ls2.close()
        leases_after = json.loads(run(["list", "--json"], env_overrides=ov)[1])
        check("fix2: ancient-but-listening lease survives gc", any(lease["id"] == "Z/ancient" for lease in leases_after))

        # --- Fix 3: null 'created' does not crash gc ---
        print("\n  [Fix 3: null created -> no crash]")
        st3 = json.loads(open(state).read())
        st3["leases"].append({"id": "N/nullcreated", "session": "N", "teammate": "nullcreated",
            "profile": "generic", "created": None, "ttl_hours": 72,
            "marker_key": "nope",
            "resources": {"port": {"kind": "port", "value": 2077},
                          "data_dir": {"kind": "dir", "value": os.path.join(td, "no-such-dir-99")}},
            "env": {}, "env_file": None, "meta": {}})
        open(state, "w").write(json.dumps(st3))
        rc, out, err = run(["gc"], env_overrides=ov)
        check("fix3: gc with null created exits 0 (no traceback)", rc == 0 and "traceback" not in (out + err).lower())
        leases_n = json.loads(run(["list", "--json"], env_overrides=ov)[1])
        check("fix3: null-created lease reclaimed (treated as ancient)", all(lease["id"] != "N/nullcreated" for lease in leases_n))

        # --- Fix 4: non-numeric ttl_hours does not crash gc/allocate ---
        print("\n  [Fix 4: non-numeric ttl_hours -> no crash]")
        st4 = json.loads(open(state).read())
        st4["leases"].append({"id": "T/badttl", "session": "T", "teammate": "badttl",
            "profile": "generic", "created": "2000-01-01T00:00:00Z", "ttl_hours": "abc",
            "marker_key": "nope",
            "resources": {"port": {"kind": "port", "value": 2078},
                          "data_dir": {"kind": "dir", "value": os.path.join(td, "no-such-dir-98")}},
            "env": {}, "env_file": None, "meta": {}})
        open(state, "w").write(json.dumps(st4))
        rc, out, err = run(["gc"], env_overrides=ov)
        check("fix4: gc with str ttl_hours exits 0 (no traceback)", rc == 0 and "traceback" not in (out + err).lower())
        rc2, out2, err2 = run(["allocate", "--session", "T2", "--teammate", "q"], env_overrides=ov)
        check("fix4: allocate with str ttl_hours in state exits 0", rc2 == 0)

        # --- Fix 5: leases not a list -> corrupt error ---
        print("\n  [Fix 5: leases not a list -> corrupt error]")
        with open(state, "w") as f5:
            json.dump({"version": 1, "leases": {}}, f5)
        rc, out, err = run(["list", "--json"], env_overrides=ov)
        check("fix5: leases={} -> list hard-errors nonzero", rc != 0)
        check("fix5: leases={} -> error contains 'corrupt'", "corrupt" in (out + err).lower())
        rc, out, err = run(["allocate", "--session", "F5", "--teammate", "t"], env_overrides=ov)
        check("fix5: leases={} -> allocate hard-errors nonzero", rc != 0)
        check("fix5: leases={} -> allocate error contains 'corrupt'", "corrupt" in (out + err).lower())
        # also test leases="bad"
        with open(state, "w") as f5b:
            json.dump({"version": 1, "leases": "bad"}, f5b)
        rc, out, err = run(["list", "--json"], env_overrides=ov)
        check("fix5: leases='bad' -> list hard-errors nonzero", rc != 0)
        check("fix5: leases='bad' -> error contains 'corrupt'", "corrupt" in (out + err).lower())
        os.remove(state)  # reset for subsequent tests

        # --- Fix 6: shell-safe quoting in env bundle ---
        print("\n  [Fix 6: shell-safe env quoting]")
        sw6 = dict(ov)
        realkey6 = os.path.join(td, "k6.key")
        open(realkey6, "w").write("KEY\n"); os.chmod(realkey6, 0o600)
        music_with_space = os.path.join(td, "my music library")
        os.makedirs(music_with_space, exist_ok=True)
        sw6.update({"ORCHESTRATE_STILLWATER_KEYFILE": realkey6,
                    "ORCHESTRATE_STILLWATER_MUSIC": music_with_space})
        rc, out, err = run(["allocate", "--session", "Q6", "--teammate", "q",
                            "--profile", "stillwater"], env_overrides=sw6)
        check("fix6: allocate with space in MUSIC exits 0", rc == 0)
        lease6 = json.loads(out)
        ef6 = lease6["env_file"]
        check("fix6: env_file written", os.path.isfile(ef6))
        # Source the env file in bash and read back SW_MUSIC_PATH
        bash_r = subprocess.run(
            ["bash", "-c", f". {ef6}; printf '%s' \"$SW_MUSIC_PATH\""],
            capture_output=True, text=True, timeout=10)
        check("fix6: sourced env_file yields exact music path with space",
              bash_r.returncode == 0 and bash_r.stdout == music_with_space)
        # STDOUT must be pure JSON (export block on STDERR only)
        check("fix6: stdout is pure JSON (no export lines)", out.strip().startswith("{"))

        # --- Fix 7a: inverted port range exits with clear message ---
        print("\n  [Fix 7a: inverted port range]")
        ov7a = dict(ov); ov7a["ORCHESTRATE_PORT_RANGE"] = "2080-1980"
        rc, out, err = run(["allocate", "--session", "R7", "--teammate", "r"], env_overrides=ov7a)
        check("fix7a: inverted range -> nonzero exit", rc != 0)
        check("fix7a: inverted range -> message mentions inversion",
              "invert" in (out + err).lower() or "lo > hi" in (out + err) or "2080" in (out + err))

        # --- Fix 7b: conflicting --lease and --session/--teammate selectors ---
        print("\n  [Fix 7b: conflicting release selectors]")
        # allocate a fresh lease to release
        rc7b, out7b, _ = run(["allocate", "--session", "R7b", "--teammate", "t"], env_overrides=ov)
        check("fix7b: allocate for selector test succeeded", rc7b == 0)
        json.loads(out7b)  # parse to confirm valid JSON; result not needed
        # conflicting: --lease points to one id, --session/--teammate to another
        rc, out, err = run(["release", "--lease", "other/id",
                            "--session", "R7b", "--teammate", "t"], env_overrides=ov)
        check("fix7b: conflicting selectors -> nonzero exit", rc != 0)
        check("fix7b: conflicting selectors -> error message", len(out + err) > 0)
        # matching selectors (same id) still work
        rc, out, err = run(["release", "--lease", "R7b/t",
                            "--session", "R7b", "--teammate", "t"], env_overrides=ov)
        check("fix7b: matching selectors -> exits 0", rc == 0)

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
