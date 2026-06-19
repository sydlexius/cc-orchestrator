#!/usr/bin/env python3
"""Proof harness for codoki-quota-watch.sh (#70, DETECT-AND-NOTIFY).

The watcher is a READ-ONLY detector with three states (SETTLED / RATE-LIMITED /
NOT-YET-RUN) and a distinct exit code each. This harness stubs every external
dependency so it is deterministic and host-independent - it NEVER calls real gh
and never posts anything:

  - `gh` is a temp 0755 script first on PATH. It serves a canned issue-comments
    JSON array (the marker fixture) from $COMMENTS_JSON, and a fixed repo slug.
  - The settlement oracle (ship-gate-preflight.sh --codoki-only) is stubbed via
    the CODOKI_QUOTA_ORACLE env override: a temp script that exits $ORACLE_RC
    (0 = settled, 2 = not settled), or a path that does not exist (oracle absent).

Contract asserted: exit 0 = SETTLED, exit 1 = RATE-LIMITED, exit 2 = setup error,
exit 3 = NOT-YET-RUN. The absolute-UTC slot parse is verified by checking the
Pacific-labeled output (TZ-pinned to America/Los_Angeles inside the script, so the
assertion is independent of the host timezone).

Run: python3 test-codoki-quota-watch.py
"""
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "codoki-quota-watch.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


# --- Fixtures ---------------------------------------------------------------
CODOKI = "codoki-pr-intelligence[bot]"


def comment(body, login=CODOKI, ts="2026-06-12T10:00:00Z"):
    return {"user": {"login": login}, "created_at": ts, "updated_at": ts, "body": body}


def comments_json(*objs):
    import json
    return json.dumps(list(objs))


RL_SLOT_UTC = "2099-06-15 12:00:00"
RL_BODY = ("<!-- CODOKI_RATE_LIMIT -->\n"
           "Used 5 / 5 reviews this hour.\n"
           f"Next available slot: **{RL_SLOT_UTC} UTC**")
RL_BODY_PAST = ("<!-- CODOKI_RATE_LIMIT -->\n"
                "Used 5 / 5 reviews this hour.\n"
                "Next available slot: **2000-01-01 00:00:00 UTC**")
RL_BODY_NOSLOT = "<!-- CODOKI_RATE_LIMIT -->\nUsed 5 / 5 reviews this hour. Try later."


def expected_pacific(utc_str):
    """Compute the expected America/Los_Angeles label for an absolute-UTC slot,
    matching the script's `+'%Y-%m-%d %H:%M %Z'`. Returns None if zoneinfo is
    unavailable (then the strict assertion is skipped)."""
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
    except Exception:
        return None
    dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M %Z")


def run(args, *, comments="[]", oracle_rc=2, oracle_absent=False, api_fail=False):
    """Invoke the watcher with stubbed gh + oracle. Returns (rc, stdout, stderr)."""
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)

        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "case \"${1:-}\" in\n"
                "  repo) echo 'owner/repo'; exit 0;;\n"
                "  api) [ -n \"${GH_API_FAIL:-}\" ] && exit 1; printf '%s' \"${COMMENTS_JSON:-[]}\"; exit 0;;\n"
                "esac\n"
                "exit 0\n"
            )
        os.chmod(gh, 0o755)

        oracle = os.path.join(td, "ship-gate-preflight.sh")
        if not oracle_absent:
            with open(oracle, "w") as f:
                f.write("#!/usr/bin/env bash\nexit ${ORACLE_RC:-2}\n")
            os.chmod(oracle, 0o755)
        else:
            oracle = os.path.join(td, "does-not-exist.sh")

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["COMMENTS_JSON"] = comments
        env["ORACLE_RC"] = str(oracle_rc)
        env["CODOKI_QUOTA_ORACLE"] = oracle
        if api_fail:
            env["GH_API_FAIL"] = "1"

        p = subprocess.run([SCRIPT] + args, env=env, capture_output=True, text=True, timeout=20)
        return p.returncode, p.stdout, p.stderr


def main():
    print("== arg validation ==")
    rc, out, _ = run(["--help"])
    check("--help -> exit 0", rc == 0 and "codoki-quota-watch" in out)
    rc, _, _ = run([])
    check("no args -> exit 2", rc == 2)
    rc, _, _ = run(["notanumber", "owner/repo"])
    check("non-numeric PR -> exit 2", rc == 2)
    rc, _, _ = run(["1", "owner/repo", "extra"])
    check("too many args -> exit 2", rc == 2)

    print("== SETTLED (oracle exit 0 wins) ==")
    rc, out, _ = run(["1", "owner/repo"], comments="[]", oracle_rc=0)
    check("oracle settled, no marker -> exit 0 (SETTLED)", rc == 0 and "SETTLED" in out)
    # Settled wins even if a stale rate-limit marker is still present.
    rc, out, _ = run(["1", "owner/repo"], comments=comments_json(comment(RL_BODY)), oracle_rc=0)
    check("oracle settled despite stale marker -> exit 0 (recovery confirmed)",
          rc == 0 and "SETTLED" in out)

    print("== RATE-LIMITED (marker present, not settled) ==")
    rc, out, err = run(["1", "owner/repo"], comments=comments_json(comment(RL_BODY)), oracle_rc=2)
    text = out + err
    check("marker + not settled -> exit 1 (RATE-LIMITED)", rc == 1 and "RATE-LIMITED" in text)
    check("surfaces the next-slot UTC time", RL_SLOT_UTC in text)
    check("notification only -> states it never posts", "never posts" in text)

    print("== absolute-UTC slot parse (Pacific-labeled, TZ-independent) ==")
    exp = expected_pacific(RL_SLOT_UTC)
    if exp is None:
        check("zoneinfo unavailable -> skip strict Pacific assertion (informational)", True)
    else:
        check(f"slot parsed as ABSOLUTE UTC -> Pacific label '{exp}'", exp in text)

    print("== RATE-LIMITED edge: marker but unparseable slot ==")
    rc, _, err = run(["1", "owner/repo"], comments=comments_json(comment(RL_BODY_NOSLOT)), oracle_rc=2)
    check("marker present but no slot line -> still exit 1 (RATE-LIMITED)", rc == 1 and "RATE-LIMITED" in err)

    print("== RATE-LIMITED edge: slot already passed ==")
    rc, _, err = run(["1", "owner/repo"], comments=comments_json(comment(RL_BODY_PAST)), oracle_rc=2)
    check("past slot -> exit 1, 'window OPEN' surfaced", rc == 1 and "window OPEN" in err)

    print("== gh api read failure -> setup error (not masked as NOT-YET-RUN) ==")
    rc, _, err = run(["1", "owner/repo"], comments="[]", oracle_rc=2, api_fail=True)
    check("gh api read fails -> exit 2 (setup error, not exit 3)", rc == 2 and "setup error" in err)

    print("== NOT-YET-RUN ==")
    rc, out, _ = run(["1", "owner/repo"], comments="[]", oracle_rc=2)
    check("no marker, not settled -> exit 3 (NOT-YET-RUN)", rc == 3 and "NOT-YET-RUN" in out)
    # A non-Codoki comment without the marker must not be mistaken for a marker.
    rc, out, _ = run(["1", "owner/repo"],
                     comments=comments_json(comment("just a normal comment", login="someuser")),
                     oracle_rc=2)
    check("unrelated comment, not settled -> exit 3 (NOT-YET-RUN)", rc == 3 and "NOT-YET-RUN" in out)

    print("== oracle absent (degraded settled-vs-not) ==")
    rc, out, err = run(["1", "owner/repo"], comments="[]", oracle_absent=True)
    check("oracle absent, no marker -> exit 3 (treated NOT-YET-RUN)", rc == 3 and "NOT-YET-RUN" in (out + err))
    # Rate-limit detection still works without the oracle.
    rc, _, err = run(["1", "owner/repo"], comments=comments_json(comment(RL_BODY)), oracle_absent=True)
    check("oracle absent but marker present -> exit 1 (RATE-LIMITED still detected)",
          rc == 1 and "RATE-LIMITED" in err)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
