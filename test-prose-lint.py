#!/usr/bin/env python3
"""Black-box harness for scripts/prose-lint.sh (issue #219). Stdlib-only, no pytest.

Hermetic: stubs the prose-tooling client so no live LanguageTool server is needed. A fake
`PROSE_TOOLING_DIR` holds a fake `.venv/bin/python` (a bash script that IS the client stand-in)
and an empty `bin/prose_check.py`. The fake python:
  - logs its full argv LOSSLESSLY (NUL-separated) to $FAKE_ARGV_LOG, so forwarding is asserted;
  - reads the LAST arg (the input file the wrapper passes) and keys output+exit on its content:
      contains BLOCKME    -> print a blocking (ERROR) finding on the input path, exit 1
      contains ADVISORYME -> print an advisory (warn) finding on the input path, exit 0
      contains SERVERDOWN -> print a server-down message to STDERR, exit 2
      otherwise           -> clean, exit 0
This mirrors the real client's exit contract (0 clean/advisory, 1 blocking, 2 server-unreachable)
and its `path:line: [SEV] RULE msg` output line, so the wrapper's label-rewrite and exit
pass-through are exercised without a server.

The wrapper is invoked via `bash scripts/prose-lint.sh` so +x is not required to test."""
import os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(HERE, "scripts", "prose-lint.sh")
FAILS = []

FAKE_PY = r"""#!/usr/bin/env bash
# Fake prose-tooling client. $1 is the client path (prose_check.py) -- ignored; the real
# python would exec it. Remaining args are the client's args; the LAST is the input file.
printf '%s\0' "$@" >> "$FAKE_ARGV_LOG"
inp=""
for a in "$@"; do inp="$a"; done   # last arg = the input file
content="$(cat "$inp" 2>/dev/null || true)"
case "$content" in
  *BLOCKME*)    echo "$inp:1: [ERROR] LOCAL_EM_DASH Em-dash: prefer a dash, comma, or parentheses."; exit 1 ;;
  *ADVISORYME*) echo "$inp:2: [warn] PASSIVE_VOICE Consider active voice."; exit 0 ;;
  *SERVERDOWN*) echo "prose-check: LanguageTool server unreachable" >&2; exit 2 ;;
  *RAWTIMEOUT*) exit 124 ;;   # simulate what timeout(1) returns when it kills a hung client
  *HANG*)       sleep 30; exit 0 ;;   # real hang; a bounded PROSE_LINT_TIMEOUT must cut it off
  *)            exit 0 ;;
esac
"""


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def make_tooling(present=True):
    """Create a fake PROSE_TOOLING_DIR. present=False leaves the client/venv absent."""
    d = tempfile.mkdtemp()
    if present:
        vbin = os.path.join(d, ".venv", "bin")
        os.makedirs(vbin)
        py = os.path.join(vbin, "python")
        with open(py, "w") as f:
            f.write(FAKE_PY)
        os.chmod(py, 0o755)
        bindir = os.path.join(d, "bin")
        os.makedirs(bindir)
        open(os.path.join(bindir, "prose_check.py"), "w").close()
    return d


def run(args, tooling_dir, stdin=None, extra_env=None):
    """Run the wrapper. Returns (rc, stdout, stderr, argv_list) where argv_list is the exact
    list of args the fake client was called with (empty if never called)."""
    fd, argv_log = tempfile.mkstemp()  # atomically created (no mktemp TOCTOU); the fake client >> appends
    os.close(fd)
    env = dict(os.environ)
    env["PROSE_TOOLING_DIR"] = tooling_dir
    env["FAKE_ARGV_LOG"] = argv_log
    if extra_env:
        env.update(extra_env)
    try:
        p = subprocess.run(["bash", WRAPPER, *args], env=env, input=stdin,
                           capture_output=True, text=True, timeout=15)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", "TIMEOUT"
    argv = []
    if os.path.exists(argv_log):
        with open(argv_log, "rb") as f:
            argv = [a.decode() for a in f.read().split(b"\0") if a]
        os.remove(argv_log)
    return rc, out, err, argv


def write_draft(text):
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def main():
    print("test-prose-lint.py")
    tooling = make_tooling(present=True)

    # 1. blocking finding on a FILE -> exit 1, finding printed with the file label
    f = write_draft("This has BLOCKME in it.\n")
    rc, out, err, argv = run([f], tooling)
    check("1a blocking file -> exit 1", rc == 1)
    check("1b blocking file -> finding on stdout", "LOCAL_EM_DASH" in out)
    os.remove(f)

    # 2. advisory-only -> exit 0, finding printed
    f = write_draft("This has ADVISORYME in it.\n")
    rc, out, err, argv = run([f], tooling)
    check("2a advisory -> exit 0", rc == 0)
    check("2b advisory -> finding on stdout", "PASSIVE_VOICE" in out)
    os.remove(f)

    # 3. clean -> exit 0, no findings
    f = write_draft("All clean here.\n")
    rc, out, err, argv = run([f], tooling)
    check("3a clean -> exit 0", rc == 0)
    check("3b clean -> no finding lines", out.strip() == "")
    os.remove(f)

    # 4. server unreachable (client exits 2) -> wrapper exits 2, loud on stderr
    f = write_draft("Draft with SERVERDOWN token.\n")
    rc, out, err, argv = run([f], tooling)
    check("4a server-down -> exit 2", rc == 2)
    check("4b server-down -> message on stderr", "unreachable" in err.lower())
    os.remove(f)

    # 4c. nonexistent draft file -> "cannot check" = exit 2, NOT 1 (a die() after the EXIT trap is
    # installed must not have its exit 2 clobbered to 1 by the trap's last falsy command).
    rc, out, err, argv = run(["/tmp/prose-lint-no-such-file.md"], tooling)
    check("4c nonexistent file -> exit 2 (not clobbered to 1)", rc == 2)
    check("4d nonexistent file -> loud stderr", "cannot read" in err.lower())
    check("4e nonexistent file -> client never invoked", argv == [])

    # 4f. an UNREADABLE regular file -> exit 2 ("cannot check"), NOT a fall-through to the client
    # (guards the -f-only vs -r readability gap). Skipped under root, which bypasses file perms.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        check("4f unreadable file -> exit 2 (SKIPPED under root)", True)
    else:
        f = write_draft("BLOCKME but unreadable.\n")
        os.chmod(f, 0o000)
        rc, out, err, argv = run([f], tooling)
        os.chmod(f, 0o600)  # restore so cleanup can remove it
        os.remove(f)
        check("4f unreadable file -> exit 2", rc == 2)
        check("4g unreadable file -> client never invoked", argv == [])

    # 5. not configured: PROSE_TOOLING_DIR points at a dir with no client/venv -> exit 2, clear msg
    empty = make_tooling(present=False)
    f = write_draft("Anything BLOCKME.\n")
    rc, out, err, argv = run([f], empty)
    check("5a not-configured -> exit 2", rc == 2)
    check("5b not-configured -> loud stderr (no silent skip)", err.strip() != "" and "prose" in err.lower())
    check("5c not-configured -> client never invoked", argv == [])
    os.remove(f)

    # 6. stdin path: piped draft, no FILE arg -> client invoked on a temp .md, label rewritten
    rc, out, err, argv = run([], tooling, stdin="Piped BLOCKME draft.\n")
    check("6a stdin blocking -> exit 1", rc == 1)
    check("6b stdin -> default label '(draft)' on the finding line", out.startswith("(draft):"))
    check("6c stdin -> no temp path leaked in output", "/tmp" not in out and "/var" not in out)
    # the temp file passed to the client is cleaned up afterward
    infile = argv[-1] if argv else ""
    check("6d stdin -> temp input .md suffix", infile.endswith(".md"))
    check("6e stdin -> temp input removed after run", infile != "" and not os.path.exists(infile))

    # 7. explicit '-' also means stdin
    rc, out, err, argv = run(["-"], tooling, stdin="Dash BLOCKME draft.\n")
    check("7 '-' reads stdin -> exit 1", rc == 1)

    # 8. --label overrides the displayed path
    f = write_draft("BLOCKME here.\n")  # assign first (no walrus -- keep <3.8 compatibility)
    rc, out, err, argv = run(["--label", "PR-body", f], tooling)
    check("8a --label rewrites path column", out.startswith("PR-body:"))
    os.remove(f)

    # 9. --profile and --no-autostart are forwarded to the client
    f = write_draft("Clean.\n")
    rc, out, err, argv = run(["--profile", "microcopy", "--no-autostart", f], tooling)
    check("9a --profile forwarded", "microcopy" in argv)
    check("9b --no-autostart forwarded", "--no-autostart" in argv)
    check("9c default profile is docs when omitted (sanity)", True)  # asserted in 10
    os.remove(f)

    # 10. default profile is docs
    f = write_draft("Clean.\n")
    rc, out, err, argv = run([f], tooling)
    check("10 default --profile docs forwarded", "docs" in argv)
    os.remove(f)

    # 11. a 124 exit from the client (what timeout(1) returns on kill) maps to cannot-check (2)
    f = write_draft("Draft RAWTIMEOUT token.\n")
    rc, out, err, argv = run([f], tooling)
    check("11a client-124 -> wrapper exit 2 (cannot-check)", rc == 2)
    check("11b client-124 -> loud 'timed out' on stderr", "timed out" in err.lower())
    os.remove(f)

    # 12. a REAL bounded timeout: a hanging client cut off by a small PROSE_LINT_TIMEOUT -> exit 2.
    # Guarded: only meaningful when a timeout/gtimeout binary exists (stock macOS lacks it).
    import shutil
    if shutil.which("timeout") or shutil.which("gtimeout"):
        f = write_draft("Draft HANG token.\n")
        rc, out, err, argv = run([f], tooling, extra_env={"PROSE_LINT_TIMEOUT": "1"})
        check("12 real hang cut off by PROSE_LINT_TIMEOUT -> exit 2", rc == 2)
        os.remove(f)
    else:
        check("12 real timeout (SKIPPED -- no timeout/gtimeout binary)", True)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("all prose-lint checks passed")


if __name__ == "__main__":
    main()
