#!/usr/bin/env python3
"""Black-box harness for scripts/cache-reclaim.sh (the /reclaim-cache helper). Stdlib-only, no
pytest. Stubs `df`/`du`/`npm`/`cargo` via PATH (each logs its argv NUL-separated to $STUB_LOG and
returns canned data); `find` is REAL so the cargo-target scan is genuinely exercised over a temp
search root. Core guarantees asserted: report mode NEVER invokes a toolchain CLEAN command (no
`cargo clean` / `npm cache clean|verify`), only the toolchain's own clean runs under --yes (never a
hand-rolled rm), a `target/` is listed only WITH a sibling Cargo.toml, --nudge is df-only, and every
path fails open (exit 0).

Invoked via `bash scripts/cache-reclaim.sh` so +x is not required to test."""
import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(HERE, "scripts", "cache-reclaim.sh")
FAILS = []

# df stub: print a df -P-shaped table; capacity (field 5) comes from $DF_CAP so --nudge is testable.
DF_STUB = r"""#!/usr/bin/env bash
printf 'df\0' >> "$STUB_LOG"; printf '%s\0' "$@" >> "$STUB_LOG"
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/disk1 100 90 10 ${DF_CAP:-38%} /"
"""
# du stub: sizes are display-only (no thresholds), so echo a canned "SIZE\tpath" and log the call.
DU_STUB = r"""#!/usr/bin/env bash
printf 'du\0' >> "$STUB_LOG"; printf '%s\0' "$@" >> "$STUB_LOG"
# last arg is the measured path
p=""; for a in "$@"; do p="$a"; done
printf '%s\t%s\n' "${DU_SIZE:-1.0G}" "$p"
"""
# npm stub: log argv; answer `config get cache` with a path; clean/verify just log.
NPM_STUB = r"""#!/usr/bin/env bash
printf 'npm\0' >> "$STUB_LOG"; printf '%s\0' "$@" >> "$STUB_LOG"
case "$*" in
  *"config get cache"*) echo "${NPM_CACHE_DIR:-/tmp/fake-npm-cache}" ;;
esac
"""
# cargo stub: log argv; `cache --version` exits nonzero unless CARGO_CACHE_PRESENT=1 (simulate the
# cargo-cache plugin being absent); clean / cache --autoclean just log.
CARGO_STUB = r"""#!/usr/bin/env bash
printf 'cargo\0' >> "$STUB_LOG"; printf '%s\0' "$@" >> "$STUB_LOG"
case "$1 ${2:-}" in
  "cache --version"|"cache -V") [ "${CARGO_CACHE_PRESENT:-0}" = 1 ] && echo "cargo-cache 0.8" || { echo "error: no such command: cache" >&2; exit 101; } ;;
esac
"""


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def make_stubdir():
    d = tempfile.mkdtemp()
    for name, body in (("df", DF_STUB), ("du", DU_STUB), ("npm", NPM_STUB), ("cargo", CARGO_STUB)):
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)
    return d


def run(args, extra_env=None, cwd=None):
    """Run the helper with stubbed df/du/npm/cargo on PATH. Returns (rc, out, calls) where `calls`
    is a list of [tool, *argv] lists parsed from the NUL-separated stub log."""
    stubdir = make_stubdir()
    fd, log = tempfile.mkstemp()  # atomically created (no mktemp TOCTOU); stubs >> append to it
    os.close(fd)
    env = dict(os.environ)
    # Keep real `find`/`bash` reachable: prepend stubdir but retain the system PATH.
    env["PATH"] = stubdir + os.pathsep + env["PATH"]
    env["STUB_LOG"] = log
    if extra_env:
        env.update(extra_env)
    try:
        p = subprocess.run(["bash", WRAPPER, *args], env=env, capture_output=True,
                           text=True, timeout=20, cwd=cwd)
        rc, out = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        rc, out = 124, "TIMEOUT"
    calls = []
    if os.path.exists(log):
        with open(log, "rb") as f:
            toks = [t.decode() for t in f.read().split(b"\0")]
        # Reconstruct calls: each is a toolname token followed by its args until the next toolname.
        toolnames = {"df", "du", "npm", "cargo"}
        i = 0
        flat = [t for t in toks if t != ""]
        while i < len(flat):
            if flat[i] in toolnames:
                j = i + 1
                while j < len(flat) and flat[j] not in toolnames:
                    j += 1
                calls.append(flat[i:j])
                i = j
            else:
                i += 1
    os.path.exists(log) and os.remove(log)
    shutil.rmtree(stubdir, ignore_errors=True)  # don't leak the per-run stub dir (Codoki/Copilot)
    return rc, out, calls


def called(calls, tool, *needles):
    """True if some `tool` call's joined argv contains all needles."""
    for c in calls:
        if c and c[0] == tool:
            joined = " ".join(c[1:])
            if all(n in joined for n in needles):
                return True
    return False


def make_root_with_cargo():
    """A temp search root: proj/ (target/ + Cargo.toml) and stray/ (target/, NO Cargo.toml)."""
    root = tempfile.mkdtemp()
    proj = os.path.join(root, "proj")
    os.makedirs(os.path.join(proj, "target"))
    open(os.path.join(proj, "Cargo.toml"), "w").close()
    stray = os.path.join(root, "stray")
    os.makedirs(os.path.join(stray, "target"))  # no Cargo.toml sibling
    return root, proj, stray


def main():
    print("test-cache-reclaim.py")
    root, proj, stray = make_root_with_cargo()

    # 1. report mode: prints df + targets, and invokes NO clean command (report is read-only).
    rc, out, calls = run(["--report", "--root", root], extra_env={"NPM_CACHE_DIR": "/tmp/fake-npm"})
    check("1a report -> exit 0", rc == 0)
    check("1b report -> ran df", called(calls, "df"))
    check("1c report -> NO npm clean/verify", not called(calls, "npm", "clean") and not called(calls, "npm", "verify"))
    check("1d report -> NO cargo clean", not called(calls, "cargo", "clean"))

    # 2. cargo-target scan: proj (has Cargo.toml) listed; stray (no Cargo.toml) NOT listed.
    check("2a report lists the rust project target", proj in out)
    check("2b report does NOT list the stray target (no Cargo.toml)", stray not in out)

    # 3. go is omitted entirely -- no `go clean -cache/-modcache` offered, no go-build/go-mod target,
    #    and the report says so. (NB: avoid the bare "go clean" substring -- it is inside "cargo clean".)
    low = out.lower()
    check("3a report offers no go clean -cache/-modcache/-testcache",
          not any(f"go clean -{f}" in low for f in ("cache", "modcache", "testcache")))
    check("3b report offers no go-build/go-mod target", "go-build" not in low and "go-mod" not in low)
    check("3c report states go is omitted", "go is omitted" in low)

    # 4. --nudge over bound -> one advisory line, df-only (no du/clean); under bound -> silent.
    rc, out, calls = run(["--nudge"], extra_env={"DF_CAP": "93%"})
    check("4a nudge over-bound -> exit 0", rc == 0)
    check("4b nudge over-bound -> prints advisory", "reclaim-cache" in out.lower() and "93" in out)
    check("4c nudge -> df only, no du", called(calls, "df") and not any(c and c[0] == "du" for c in calls))
    rc, out, calls = run(["--nudge"], extra_env={"DF_CAP": "40%"})
    check("4d nudge under-bound -> silent", out.strip() == "" and rc == 0)

    # 5. --yes <cargo target path> -> runs `cargo clean --manifest-path <proj>/Cargo.toml`, NEVER rm.
    rc, out, calls = run(["--yes", proj, "--root", root])
    check("5a --yes target -> exit 0", rc == 0)
    check("5b --yes target -> cargo clean --manifest-path proj", called(calls, "cargo", "clean", "--manifest-path", proj))
    check("5c --yes target -> no rm in output/behavior", "rm -rf" not in out)

    # 5d. --yes <proj>/target (a path whose PARENT holds Cargo.toml) resolves to the project manifest
    #     and cleans it (the report prints <proj>, but a <proj>/target path must also work).
    rc, out, calls = run(["--yes", os.path.join(proj, "target")])
    check("5d --yes <proj>/target -> cargo clean on the project manifest",
          called(calls, "cargo", "clean", "--manifest-path", os.path.join(proj, "Cargo.toml")))

    # 6. --yes npm -> light `npm cache verify`; --yes npm=force -> `npm cache clean --force`.
    rc, out, calls = run(["--yes", "npm"], extra_env={"NPM_CACHE_DIR": "/tmp/fake-npm"})
    check("6a --yes npm -> npm cache verify (light)", called(calls, "npm", "cache", "verify"))
    check("6b --yes npm -> NOT clean --force", not called(calls, "npm", "clean", "--force"))
    rc, out, calls = run(["--yes", "npm=force"], extra_env={"NPM_CACHE_DIR": "/tmp/fake-npm"})
    check("6c --yes npm=force -> npm cache clean --force", called(calls, "npm", "clean", "--force"))

    # 7. --yes cargo-registry with cargo-cache ABSENT -> warn+skip+hint, NO autoclean, NO rm.
    rc, out, calls = run(["--yes", "cargo-registry"])
    check("7a cargo-registry absent tool -> exit 0 (fail-soft)", rc == 0)
    check("7b cargo-registry absent -> NO autoclean run", not called(calls, "cargo", "autoclean"))
    check("7c cargo-registry absent -> prints install hint", "cargo-cache" in out)

    # 8. unknown --yes name -> warned, skipped, no clean.
    rc, out, calls = run(["--yes", "bogus"])
    check("8a unknown name -> exit 0", rc == 0)
    check("8b unknown name -> warned", "bogus" in out)
    check("8c unknown name -> no cargo/npm clean", not called(calls, "cargo", "clean") and not called(calls, "npm", "clean"))

    # 9. -h/--help works.
    rc, out, calls = run(["--help"])
    check("9 --help -> exit 0 + usage", rc == 0 and "reclaim" in out.lower())

    # 10. a bare trailing --yes / --root (no operand) must fail fast, NOT hang (was an infinite loop).
    rc, out, calls = run(["--yes"])
    check("10a bare --yes -> exit 2, no hang", rc == 2)
    rc, out, calls = run(["--root"])
    check("10b bare --root -> exit 2, no hang", rc == 2)

    # 11. --yes '*' must NOT glob-expand into unnamed projects (globbing disabled around the split).
    #     cwd is a dir holding two rust projects; '*' would expand to both without the fix.
    groot = tempfile.mkdtemp()
    for nm in ("projA", "projB"):
        os.makedirs(os.path.join(groot, nm, "target"))
        open(os.path.join(groot, nm, "Cargo.toml"), "w").close()
    rc, out, calls = run(["--yes", "*", "--root", groot], cwd=groot)
    check("11a --yes '*' -> exit 0", rc == 0)
    check("11b --yes '*' -> NO cargo clean (glob not expanded to projects)", not called(calls, "cargo", "clean"))

    # 12. cargo-registry is REPORT-ONLY: --yes cargo-registry NEVER mutates (no autoclean) even with
    #     the cargo-cache plugin present; it prints the command for the user to run instead.
    rc, out, calls = run(["--yes", "cargo-registry"], extra_env={"CARGO_CACHE_PRESENT": "1"})
    check("12a cargo-registry present -> still NO autoclean (report-only)", not called(calls, "cargo", "autoclean"))
    check("12b cargo-registry -> prints the 'cargo cache --autoclean' command", "cargo cache --autoclean" in out)

    # 13. --root pointing at a nonexistent dir -> report still exits 0 (fail-open), no crash.
    rc, out, calls = run(["--report", "--root", "/no/such/dir/xyz"])
    check("13 report with nonexistent --root -> exit 0", rc == 0)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("all cache-reclaim checks passed")


if __name__ == "__main__":
    main()
