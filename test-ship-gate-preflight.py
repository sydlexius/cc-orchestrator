#!/usr/bin/env python3
"""Proof harness for ship-gate-preflight.sh (#110).

The oracle is a read-only merge-readiness gate that must FAIL CLOSED on every
ambiguous/unknown state. This harness stubs BOTH external dependencies so it is
deterministic and host-independent - it NEVER calls real gh or the real
pr-unreplied-comments.sh:

  - `gh` is a temp 0755 script placed first on PATH. It serves the canned
    statusCheckRollup JSON from the $FIXTURE_JSON env var for `gh pr view`, and a
    fixed slug for `gh repo view`. A $GH_FAIL env var makes it exit non-zero
    (to exercise the fail-closed gh-error path).
  - pr-unreplied-comments.sh is stubbed under a temp $HOME/.claude/scripts/ (the
    oracle resolves it via $HOME). Its behavior is driven by env vars:
      UNREPLIED_FINDINGS  -> prints the "Review-body comments with actionable
                             findings: N" line when N>0 (mirrors the real script,
                             which only prints the line when N>0)
      UNREPLIED_FAIL=1     -> exit 2 (helper error -> oracle must BLOCK)
      UNREPLIED_MISSING=1  -> do not create the helper at all (oracle must BLOCK)

Decision contract asserted:
  exit 0 = PASS, exit 1 = USAGE error, exit 2 = BLOCK (fail-closed).

Run: python3 test-ship-gate-preflight.py
"""
import json
import os
import subprocess
import sys
import tempfile

ORACLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "ship-gate-preflight.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


def rollup(*contexts):
    """Build a statusCheckRollup JSON document from context dicts."""
    return json.dumps({"statusCheckRollup": list(contexts)})


def checkrun(name, status, conclusion):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def statusctx(context, state):
    return {"__typename": "StatusContext", "context": context, "state": state}


def unknowntype(name, status="COMPLETED", conclusion="SUCCESS", typename="UnknownType"):
    """A context whose __typename is NEITHER CheckRun nor StatusContext (or is
    absent, when typename=None). gh always emits __typename to discriminate the
    union, so an unknown/absent type is ambiguous -> the oracle MUST BLOCK rather
    than treat it as a CheckRun (the #110 fail-open BLOCKER)."""
    d = {"name": name, "status": status, "conclusion": conclusion}
    if typename is not None:
        d["__typename"] = typename
    return d


def run(args, *, fixture_json, gh_fail=False, unreplied_findings=0,
        unreplied_fail=False, unreplied_missing=False, unreplied_raw=None):
    """Invoke the oracle with stubbed gh + pr-unreplied-comments.sh.
    Returns (exit_code, stdout, stderr)."""
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        home = os.path.join(td, "home")
        helper_dir = os.path.join(home, ".claude", "scripts"); os.makedirs(helper_dir)

        # Stub gh.
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "if [ -n \"${GH_FAIL:-}\" ]; then echo 'gh: simulated failure' >&2; exit 1; fi\n"
                "# `gh repo view --json nameWithOwner --jq .nameWithOwner`\n"
                "for a in \"$@\"; do case \"$a\" in repo) echo 'owner/repo'; exit 0;; esac; done\n"
                "# `gh pr view <pr> --repo <repo> --json statusCheckRollup`\n"
                "printf '%s' \"${FIXTURE_JSON:-}\"\n"
            )
        os.chmod(gh, 0o755)

        # Stub pr-unreplied-comments.sh (unless we are simulating it missing).
        if not unreplied_missing:
            helper = os.path.join(helper_dir, "pr-unreplied-comments.sh")
            with open(helper, "w") as f:
                f.write(
                    "#!/usr/bin/env bash\n"
                    "set -eu\n"
                    "if [ -n \"${UNREPLIED_FAIL:-}\" ]; then echo 'helper: simulated failure' >&2; exit 2; fi\n"
                    "# UNREPLIED_RAW prints the findings line with a RAW (possibly non-numeric)\n"
                    "# count, to exercise the fail-closed-on-non-numeric path.\n"
                    "if [ -n \"${UNREPLIED_RAW:-}\" ]; then\n"
                    "  echo \"=== Review-body comments with actionable findings: ${UNREPLIED_RAW} ===\"\n"
                    "  echo 'some other output line'; exit 0\n"
                    "fi\n"
                    "n=\"${UNREPLIED_FINDINGS:-0}\"\n"
                    "# Real script prints the line ONLY when N>0.\n"
                    "if [ \"$n\" -gt 0 ]; then\n"
                    "  echo \"=== Review-body comments with actionable findings: $n ===\"\n"
                    "fi\n"
                    "echo 'some other output line'\n"
                )
            os.chmod(helper, 0o755)

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["HOME"] = home
        env["FIXTURE_JSON"] = fixture_json
        if gh_fail:
            env["GH_FAIL"] = "1"
        env["UNREPLIED_FINDINGS"] = str(unreplied_findings)
        if unreplied_fail:
            env["UNREPLIED_FAIL"] = "1"
        if unreplied_raw is not None:
            env["UNREPLIED_RAW"] = unreplied_raw

        p = subprocess.run([ORACLE] + args, env=env, capture_output=True, text=True, timeout=15)
        return p.returncode, p.stdout, p.stderr


ALL_GREEN = rollup(
    checkrun("ci", "COMPLETED", "SUCCESS"),
    statusctx("buildkite", "SUCCESS"),
)


def main():
    print("== FULL MODE: pass path ==")
    rc, out, err = run(["123", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=0)
    check("all checks green (CheckRun SUCCESS + StatusContext SUCCESS) + 0 unreplied -> exit 0",
          rc == 0)

    rc, out, err = run(["123", "owner/repo"],
                       fixture_json=rollup(checkrun("a", "COMPLETED", "NEUTRAL"),
                                           checkrun("b", "COMPLETED", "SKIPPED")),
                       unreplied_findings=0)
    check("NEUTRAL + SKIPPED conclusions still pass -> exit 0", rc == 0)

    print("== FULL MODE: CheckRun block paths ==")
    rc, _, _ = run(["1", "owner/repo"], fixture_json=rollup(checkrun("ci", "COMPLETED", "FAILURE")))
    check("CheckRun conclusion=FAILURE -> exit 2", rc == 2)
    rc, _, _ = run(["1", "owner/repo"], fixture_json=rollup(checkrun("ci", "COMPLETED", None)))
    check("CheckRun conclusion=null -> exit 2", rc == 2)
    rc, _, _ = run(["1", "owner/repo"], fixture_json=rollup(checkrun("ci", "IN_PROGRESS", None)))
    check("CheckRun status=IN_PROGRESS -> exit 2", rc == 2)
    rc, _, _ = run(["1", "owner/repo"], fixture_json=rollup(checkrun("ci", "QUEUED", None)))
    check("CheckRun status=QUEUED -> exit 2", rc == 2)
    rc, _, _ = run(["1", "owner/repo"], fixture_json=rollup(checkrun("ci", "COMPLETED", "CANCELLED")))
    check("CheckRun conclusion=CANCELLED -> exit 2", rc == 2)
    # A green CheckRun mixed with one bad one must still block.
    rc, _, _ = run(["1", "owner/repo"],
                   fixture_json=rollup(checkrun("ok", "COMPLETED", "SUCCESS"),
                                       checkrun("bad", "COMPLETED", "FAILURE")))
    check("mixed green + FAILURE -> exit 2", rc == 2)

    print("== FULL MODE: StatusContext block paths ==")
    rc, _, _ = run(["1", "owner/repo"], fixture_json=rollup(statusctx("sc", "PENDING")))
    check("StatusContext state=PENDING -> exit 2", rc == 2)
    rc, _, _ = run(["1", "owner/repo"], fixture_json=rollup(statusctx("sc", "ERROR")))
    check("StatusContext state=ERROR -> exit 2", rc == 2)
    rc, _, _ = run(["1", "owner/repo"], fixture_json=rollup(statusctx("sc", "EXPECTED")))
    check("StatusContext state=EXPECTED -> exit 2", rc == 2)
    rc, _, _ = run(["1", "owner/repo"], fixture_json=rollup(statusctx("sc", "FAILURE")))
    check("StatusContext state=FAILURE -> exit 2", rc == 2)

    print("== FULL MODE: empty rollup + review-gate paths ==")
    rc, _, _ = run(["1", "owner/repo"], fixture_json=rollup())
    check("empty rollup (no checks) -> exit 2 (no vacuous pass)", rc == 2)
    rc, _, _ = run(["1", "owner/repo"], fixture_json='{"statusCheckRollup":null}')
    check("null rollup -> exit 2", rc == 2)

    rc, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_findings=3)
    check("all green but N=3 unreplied findings -> exit 2", rc == 2)
    rc, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_fail=True)
    check("pr-unreplied-comments.sh errors -> exit 2 (fail closed)", rc == 2)
    rc, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_missing=True)
    check("pr-unreplied-comments.sh missing -> exit 2 (fail closed)", rc == 2)
    # Codoki #116: a findings line PRESENT but with a non-numeric count must BLOCK,
    # not be silently read as N=0 (the fail-open the parse comment intended to prevent).
    rc, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_raw="N/A")
    check("review-body findings line present but NON-NUMERIC count -> exit 2 (fail closed)", rc == 2)
    # Sanity: a numeric count embedded in a richer line is still parsed and blocks.
    rc, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, unreplied_raw="4 (2 major)")
    check("review-body findings line with numeric-prefixed count (4 ...) -> exit 2", rc == 2)

    print("== FAIL-CLOSED: gh / json errors ==")
    rc, _, _ = run(["1", "owner/repo"], fixture_json=ALL_GREEN, gh_fail=True)
    check("gh failure -> exit 2 (fail closed)", rc == 2)
    rc, _, _ = run(["1", "owner/repo"], fixture_json="this is not json{")
    check("malformed JSON from gh -> exit 2 (fail closed)", rc == 2)

    print("== FAIL-CLOSED: unknown / absent __typename (the #110 fail-open BLOCKER) ==")
    rc, _, _ = run(["1", "owner/repo"], fixture_json=rollup(unknowntype("ci")))
    check("full: unknown __typename with green-looking CheckRun fields -> exit 2", rc == 2)
    rc, _, _ = run(["1", "owner/repo"], fixture_json=rollup(unknowntype("ci", typename=None)))
    check("full: ABSENT __typename -> exit 2", rc == 2)
    rc, _, _ = run(["1", "owner/repo"],
                   fixture_json=rollup(checkrun("ok", "COMPLETED", "SUCCESS"), unknowntype("x")))
    check("full: green CheckRun + unknown-type check -> exit 2 (unknown blocks, no mask)", rc == 2)
    rc, _, _ = run(["1", "owner/repo", "--codoki-only"],
                   fixture_json=rollup(unknowntype("Codoki PR Review")))
    check("codoki-only: unknown __typename named like Codoki -> exit 2", rc == 2)

    print("== CODOKI-ONLY MODE ==")
    codoki_ok = rollup(checkrun("Codoki PR Review", "COMPLETED", "SUCCESS"),
                       checkrun("ci", "IN_PROGRESS", None))  # other checks irrelevant here
    rc, _, _ = run(["1", "owner/repo", "--codoki-only"], fixture_json=codoki_ok)
    check("--codoki-only: Codoki COMPLETED/SUCCESS -> exit 0 (ignores other checks)", rc == 0)

    rc, _, _ = run(["1", "owner/repo", "--codoki-only"],
                   fixture_json=rollup(checkrun("ci", "COMPLETED", "SUCCESS")))
    check("--codoki-only: Codoki check missing -> exit 2", rc == 2)

    rc, _, _ = run(["1", "owner/repo", "--codoki-only"],
                   fixture_json=rollup(checkrun("Codoki PR Review", "IN_PROGRESS", None)))
    check("--codoki-only: Codoki IN_PROGRESS -> exit 2", rc == 2)

    rc, _, _ = run(["1", "owner/repo", "--codoki-only"],
                   fixture_json=rollup(checkrun("Codoki PR Review", "COMPLETED", "FAILURE")))
    check("--codoki-only: Codoki COMPLETED/FAILURE -> exit 2", rc == 2)

    # --codoki-only must NOT consult pr-unreplied-comments.sh: a missing helper
    # must still PASS when Codoki is settled (proves the helper is skipped).
    rc, _, _ = run(["1", "owner/repo", "--codoki-only"], fixture_json=codoki_ok,
                   unreplied_missing=True)
    check("--codoki-only skips pr-unreplied-comments.sh (missing helper still PASS)", rc == 0)

    # --codoki-pattern override matches a differently-named check.
    rc, _, _ = run(["1", "owner/repo", "--codoki-only", "--codoki-pattern", "My Bot"],
                   fixture_json=rollup(checkrun("My Bot", "COMPLETED", "SUCCESS")))
    check("--codoki-pattern override matches a differently-named check -> exit 0", rc == 0)
    # ... and the default pattern then does NOT match it.
    rc, _, _ = run(["1", "owner/repo", "--codoki-only"],
                   fixture_json=rollup(checkrun("My Bot", "COMPLETED", "SUCCESS")))
    check("default pattern does not match 'My Bot' -> exit 2", rc == 2)
    # A StatusContext-shaped Codoki check is accepted via name/state mapping.
    rc, _, _ = run(["1", "owner/repo", "--codoki-only"],
                   fixture_json=rollup(statusctx("Codoki PR Review", "SUCCESS")))
    check("--codoki-only: StatusContext Codoki SUCCESS -> exit 0", rc == 0)

    print("== USAGE ==")
    rc, _, _ = run([], fixture_json=ALL_GREEN)
    check("no args -> exit 1 (usage)", rc == 1)
    rc, _, _ = run(["1", "owner/repo", "--codoki-pattern"], fixture_json=ALL_GREEN)
    check("--codoki-pattern with no value -> exit 1 (usage)", rc == 1)
    rc, _, _ = run(["1", "owner/repo", "--bogus"], fixture_json=ALL_GREEN)
    check("unknown flag -> exit 1 (usage)", rc == 1)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
