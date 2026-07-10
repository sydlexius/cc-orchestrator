#!/usr/bin/env python3
"""Black-box harness for scripts/resolve-threads.sh numeric-arg validation (issue #257).
Stdlib-only, no pytest. Stubs `gh` via PATH (logs its argv NUL-separated, returns canned data)
so no network/auth is needed. The core assertions: a non-numeric <pr> or <comment-db-id>
fails with a usage error + exit 1 BEFORE any gh/GraphQL call (empty gh log == no mutation),
while numeric args pass validation and reach the (stubbed) gh path unchanged.

Invoked via `bash scripts/resolve-threads.sh` so +x is not required to test."""
import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(HERE, "scripts", "resolve-threads.sh")
FAILS = []

# Stub gh: log argv losslessly; answer the two read calls resolve-threads makes on the happy
# path (repo view -> owner/name; graphql fetch -> empty reviewThreads). The resolve MUTATION
# (resolveReviewThread) must never be reached in these tests; if it is, it's logged like any call.
GH_STUB = r"""#!/usr/bin/env bash
printf '%s\0' "$@" >> "$GH_LOG"
args="$*"
case "$args" in
  *"repo view"*)   echo "owner/name" ;;
  *graphql*)       echo '{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[]}' ;;
  *)               echo '{}' ;;
esac
"""


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def run(args):
    """Run resolve-threads.sh with a stubbed gh. Returns (rc, stdout+stderr, gh_argv_list)."""
    d = tempfile.mkdtemp()
    ghstub = os.path.join(d, "gh")
    log = os.path.join(d, "ghlog")
    with open(ghstub, "w") as f:
        f.write(GH_STUB)
    os.chmod(ghstub, 0o755)
    env = dict(os.environ)
    env["PATH"] = d + os.pathsep + env["PATH"]
    env["GH_LOG"] = log
    try:
        p = subprocess.run(["bash", WRAPPER, *args], env=env, capture_output=True,
                           text=True, timeout=15)
        rc, out = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        rc, out = 124, "TIMEOUT"
    argv = []
    if os.path.exists(log):
        with open(log, "rb") as f:
            argv = [a.decode() for a in f.read().split(b"\0") if a]
    shutil.rmtree(d, ignore_errors=True)  # don't accumulate per-run temp dirs in /tmp (Codoki)
    return rc, out, argv


def main():
    print("test-resolve-threads.py")

    # 1. non-numeric <comment-db-id> (the filed repro: a repo slug where an id belongs) ->
    #    usage error + exit 1, BEFORE any gh call (empty log == no mutation).
    rc, out, argv = run(["242", "sydlexius/cc-orchestrator"])
    check("1a non-numeric db-id -> exit 1", rc == 1)
    check("1b non-numeric db-id -> 'comment-db-id must be numeric' message", "comment-db-id must be numeric" in out)
    check("1c non-numeric db-id -> usage block printed", "Usage:" in out)
    check("1d non-numeric db-id -> gh NEVER called (no mutation)", argv == [])

    # 2. non-numeric <pr> (the pr/slug swap) -> usage + exit 1 before the query.
    rc, out, argv = run(["sydlexius/cc-orchestrator", "123"])
    check("2a non-numeric pr -> exit 1", rc == 1)
    check("2b non-numeric pr -> 'pr must be numeric' message", "pr must be numeric" in out)
    check("2c non-numeric pr -> gh NEVER called", argv == [])

    # 3. a non-numeric id among otherwise-valid ids is still caught (per-arg validation).
    rc, out, argv = run(["242", "123", "notanid", "456"])
    check("3a mixed valid/invalid ids -> exit 1", rc == 1)
    check("3b mixed -> names the bad arg", "notanid" in out)
    check("3c mixed -> gh NEVER called", argv == [])

    # 3b. an embedded newline in an id is rejected (the whole-string case-glob matches per
    #     character, so a value like "12\n34" that a line-oriented grep would pass is caught).
    rc, out, argv = run(["242", "12\n34"])
    check("3d newline-bearing id -> exit 1", rc == 1)
    check("3e newline-bearing id -> gh NEVER called", argv == [])

    # 4. happy path: numeric pr + numeric id passes validation and reaches gh (stub returns
    #    empty threads -> 'Skipped ... not found'), exit 0. The resolve logic is unchanged.
    rc, out, argv = run(["242", "123456"])
    check("4a numeric args -> exit 0", rc == 0)
    check("4b numeric args -> reached gh (graphql fetch ran)", any("graphql" in a for a in argv))
    check("4c numeric args -> no resolveReviewThread mutation (empty threads)",
          not any("resolveReviewThread" in a for a in argv))

    # 5. --bot <pattern> is an arbitrary regex, NOT numeric-validated; numeric positionals
    #    after it still pass.
    rc, out, argv = run(["--bot", "foo|bar", "242", "123456"])
    check("5a --bot regex + numeric args -> exit 0 (pattern not rejected)", rc == 0)

    # 6. the pre-existing arg-COUNT guard still fires (too few args).
    rc, out, argv = run(["242"])
    check("6 too-few-args -> exit 1 (count guard intact)", rc == 1 and "Usage:" in out)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("all resolve-threads checks passed")


if __name__ == "__main__":
    main()
