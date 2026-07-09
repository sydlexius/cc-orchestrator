#!/usr/bin/env python3
"""Proof harness for orchestrate-context-meter.sh (the PostToolUse context-budget meter, #228).

The meter is ADVISORY + FAIL-OPEN: it accumulates a per-session PROXY of the lead's context growth
(compact tool_input JSON bytes + tool_response JSON bytes, /4 chars-per-token) and emits a one-line
`CTX-METER:` WARN to stderr the FIRST time the cumulative total crosses ~70% and ~85% of a
configurable budget. It NEVER blocks a tool: every path exits 0.

Asserts, via synthetic PostToolUse stdin JSON fed to the hook:
  - accumulation below the warn threshold -> no WARN, exit 0.
  - crossing 70% -> the 70% WARN fires EXACTLY once; a later call still above 70% (below 85%) does
    NOT re-warn 70%.
  - crossing 85% -> the 85% WARN fires once (and does not re-fire).
  - malformed JSON / empty session_id / missing session_id / missing tools -> exit 0, no output.
  - session isolation: two session_ids accumulate independently.
  - a bad ORCHESTRATE_CONTEXT_BUDGET_TOKENS / WARN_PCT falls back to the default (never disarms).
  - a single large tool_response pushes pct across a threshold in one call (proxy-math sanity).
Every case asserts exit 0 (the meter NEVER blocks).
Run: python3 test-orchestrate-context-meter.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

METER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "scripts", "orchestrate-context-meter.sh")
FAILS = []


def run_meter(session_id="s", tool_input=None, tool_response=None, *, ctxmeter_dir,
              budget=None, warn_pct=None, hard_pct=None, raw_stdin=None, path=None):
    """Invoke the meter hook once. Returns (exit_code, stderr, stdout)."""
    env = dict(os.environ)
    env["ORCHESTRATE_CTXMETER_DIR"] = ctxmeter_dir
    if budget is not None:
        env["ORCHESTRATE_CONTEXT_BUDGET_TOKENS"] = str(budget)
    else:
        env.pop("ORCHESTRATE_CONTEXT_BUDGET_TOKENS", None)
    if warn_pct is not None:
        env["ORCHESTRATE_CTXMETER_WARN_PCT"] = str(warn_pct)
    else:
        env.pop("ORCHESTRATE_CTXMETER_WARN_PCT", None)
    if hard_pct is not None:
        env["ORCHESTRATE_CTXMETER_HARD_PCT"] = str(hard_pct)
    else:
        env.pop("ORCHESTRATE_CTXMETER_HARD_PCT", None)
    if path is not None:
        env["PATH"] = path
    if raw_stdin is not None:
        stdin_data = raw_stdin
    else:
        stdin_data = json.dumps({
            "session_id": session_id,
            "tool_name": "Bash",
            "hook_event_name": "PostToolUse",
            "tool_input": tool_input if tool_input is not None else {},
            "tool_response": tool_response if tool_response is not None else "",
        })
    p = subprocess.run([METER], input=stdin_data, env=env,
                       capture_output=True, text=True, timeout=10)
    return p.returncode, p.stderr, p.stdout


def metered(stderr):
    return "CTX-METER" in stderr


def check(label, cond):
    status = "ok" if cond else "FAIL"
    if not cond:
        FAILS.append(label)
    print(f"  [{status}] {label}")


# A response body of N bytes -> jq -c compacts it to a quoted string of ~N+2 bytes -> ~N/4 tokens.
def body(nbytes):
    return "x" * nbytes


def main():
    print("orchestrate-context-meter.sh harness")

    # --self-test passes.
    p = subprocess.run([METER, "--self-test"], capture_output=True, text=True, timeout=10)
    check("--self-test exits 0 and reports PASS", p.returncode == 0 and "PASS" in p.stdout)

    # ---- accumulation below the warn threshold -> silent ----
    with tempfile.TemporaryDirectory() as d:
        rc, err, _ = run_meter("below", tool_response=body(400), ctxmeter_dir=d, budget=100000)
        check("below 70% -> no WARN, exit 0", rc == 0 and not metered(err))

    # ---- crossing 70% -> warn once; a second call above 70% but below 85% does NOT re-warn ----
    with tempfile.TemporaryDirectory() as d:
        # budget 1000 tokens; a ~3000-byte response ~= 750 tokens ~= 75% -> crosses 70%.
        rc, err, _ = run_meter("w70", tool_response=body(3000), ctxmeter_dir=d, budget=1000)
        check("crossing 70% -> WARN fires, exit 0", rc == 0 and metered(err))
        check("70% WARN text mentions 70%", "70%" in err and "85%" not in err)
        # A second, tiny call: total climbs to ~77%, still below 85% -> no re-warn.
        rc2, err2, _ = run_meter("w70", tool_response=body(80), ctxmeter_dir=d, budget=1000)
        check("second call still 70-85% -> NO re-warn (fires at most once)",
              rc2 == 0 and not metered(err2))

    # ---- crossing 85% -> warn once; hard fires once, no repeats ----
    with tempfile.TemporaryDirectory() as d:
        # First call ~75% -> warn (70%) fires.
        rc, err, _ = run_meter("h85", tool_response=body(3000), ctxmeter_dir=d, budget=1000)
        check("h85 setup: first call fires 70% warn", rc == 0 and "70%" in err)
        # Second call pushes to ~90% -> the 85% hard warn fires; the 70% warn does NOT re-fire.
        rc2, err2, _ = run_meter("h85", tool_response=body(700), ctxmeter_dir=d, budget=1000)
        check("crossing 85% -> hard WARN (85%) fires, exit 0", rc2 == 0 and "85%" in err2)
        check("crossing 85% does NOT re-fire the 70% warn", "70%" not in err2)
        # Third call still above 85% -> nothing re-fires.
        rc3, err3, _ = run_meter("h85", tool_response=body(700), ctxmeter_dir=d, budget=1000)
        check("above 85% again -> NO further WARN (both fired once)",
              rc3 == 0 and not metered(err3))

    # ---- fail-open inputs -> exit 0, no output ----
    with tempfile.TemporaryDirectory() as d:
        rc, err, out = run_meter(ctxmeter_dir=d, raw_stdin="{ this is not json")
        check("malformed JSON -> exit 0, silent", rc == 0 and not err and not out)
        rc, err, out = run_meter(ctxmeter_dir=d,
                                 raw_stdin=json.dumps({"session_id": "", "tool_input": {}}))
        check("empty session_id -> exit 0, silent", rc == 0 and not err and not out)
        rc, err, out = run_meter(ctxmeter_dir=d,
                                 raw_stdin=json.dumps({"tool_input": {}, "tool_response": "x"}))
        check("missing session_id -> exit 0, silent", rc == 0 and not err and not out)
        rc, err, out = run_meter(ctxmeter_dir=d, raw_stdin="")
        check("empty stdin -> exit 0, silent", rc == 0 and not err and not out)
        # Tools unavailable (only bash reachable; jq/cat/etc NOT on PATH) -> fail-open, exit 0,
        # silent. A bin dir with just a `bash` symlink keeps the `env bash` interpreter lookup
        # working while every external tool the body needs (cat/jq/...) is absent.
        bindir = os.path.join(d, "bin")
        os.makedirs(bindir, exist_ok=True)
        bash_path = shutil.which("bash") or "/bin/bash"
        os.symlink(bash_path, os.path.join(bindir, "bash"))
        rc, err, out = run_meter("notools", tool_response=body(3000), ctxmeter_dir=d,
                                 budget=1000, path=bindir)
        check("missing tools (only bash on PATH) -> exit 0, silent (fail-open)",
              rc == 0 and not metered(err) and not out)

    # ---- session isolation: two session_ids accumulate independently ----
    with tempfile.TemporaryDirectory() as d:
        rc_a, err_a, _ = run_meter("iso-A", tool_response=body(3000), ctxmeter_dir=d, budget=1000)
        check("session A crosses 70% -> WARN", rc_a == 0 and metered(err_a))
        rc_b, err_b, _ = run_meter("iso-B", tool_response=body(400), ctxmeter_dir=d, budget=1000)
        check("session B (small) stays silent -> independent accumulation",
              rc_b == 0 and not metered(err_b))

    # ---- bad env values fall back to the default (never disarm / crash) ----
    with tempfile.TemporaryDirectory() as d:
        # Bad budget -> falls back to 200000; a 750-token payload is ~0.4% -> no warn (proves the
        # default budget applied, not a disarm or a crash).
        rc, err, _ = run_meter("badbudget", tool_response=body(3000), ctxmeter_dir=d,
                               budget="not-a-number")
        check("bad BUDGET -> default 200000 applied, no warn, exit 0", rc == 0 and not metered(err))
    with tempfile.TemporaryDirectory() as d:
        # Bad WARN_PCT -> falls back to 70; a ~75% payload must still warn at 70%.
        rc, err, _ = run_meter("badwarn", tool_response=body(3000), ctxmeter_dir=d,
                               budget=1000, warn_pct="banana")
        check("bad WARN_PCT -> default 70 applied, still warns", rc == 0 and "70%" in err)
    with tempfile.TemporaryDirectory() as d:
        # Bad HARD_PCT -> falls back to 85; a ~90% payload must still hard-warn at 85%.
        rc, err, _ = run_meter("badhard", tool_response=body(3600), ctxmeter_dir=d,
                               budget=1000, hard_pct="0xdead")
        check("bad HARD_PCT -> default 85 applied, still hard-warns", rc == 0 and "85%" in err)

    # ---- a single large tool_response crosses a threshold in ONE call (proxy-math sanity) ----
    with tempfile.TemporaryDirectory() as d:
        # ~3400-byte response ~= 850 tokens ~= 85% of a 1000-token budget -> hard fires in one call.
        rc, err, _ = run_meter("big1", tool_response=body(3400), ctxmeter_dir=d, budget=1000)
        check("single large tool_response -> crosses 85% in one call", rc == 0 and "85%" in err)

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
