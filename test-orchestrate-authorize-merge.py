#!/usr/bin/env python3
"""Proof harness for orchestrate-authorize-merge.sh (#263 Piece B).

The helper runs the hardened ship-gate-preflight and, ONLY on a PASS that emits a
`headRefOid=<sha>`, writes a short-TTL, session-scoped merge-auth token the floor
checks. This harness stubs the preflight (never runs the real one) and asserts the
token is written exactly when it should be, with the right contents/mode, and NEVER
on a BLOCK or a missing SHA.

Run: python3 test-orchestrate-authorize-merge.py
"""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "orchestrate-authorize-merge.sh")
SHA = "abcdef0123456789abcdef0123456789abcdef01"
FAILS = []


def check(label, ok):
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}")
    if not ok:
        FAILS.append(label)


def _key(tmux):
    return re.sub(rb'[^A-Za-z0-9]', b'_', tmux.encode("utf-8", "surrogateescape")).decode("ascii")


def run(args, *, tmux="/tmp/tmux-x,1,0", preflight_mode="pass", ttl_min=None):
    """Run the helper with a stubbed ship-gate-preflight on PATH-independent lookup.

    preflight_mode: 'pass' (exit 0, prints a RESULT line with headRefOid=SHA),
    'pass_no_sha' (exit 0 but no headRefOid), 'block' (exit 2). The helper resolves
    the preflight via ${CLAUDE_PLUGIN_ROOT}/scripts or $HOME/.claude/scripts; we stub
    it under a temp $HOME/.claude/scripts and point HOME there.
    Returns (rc, stdout, stderr, floor_dir, home)."""
    td = tempfile.mkdtemp()
    floor_dir = os.path.join(td, "floor.d")
    home = os.path.join(td, "home")
    scripts = os.path.join(home, ".claude", "scripts")
    os.makedirs(scripts)
    pf = os.path.join(scripts, "ship-gate-preflight.sh")
    if preflight_mode == "pass":
        body = (f'echo "RESULT: PASS -- all green, headRefOid={SHA}. [#$1]"\n' "exit 0\n")
    elif preflight_mode == "pass_no_sha":
        body = 'echo "RESULT: PASS -- all green (no sha emitted). [#$1]"\nexit 0\n'
    else:  # block
        body = 'echo "BLOCK: something unresolved on #$1" >&2\nexit 2\n'
    with open(pf, "w") as f:
        f.write("#!/usr/bin/env bash\n" + body)
    os.chmod(pf, 0o755)

    env = dict(os.environ)
    env["HOME"] = home
    env.pop("CLAUDE_PLUGIN_ROOT", None)  # force the $HOME/.claude/scripts fallback
    env["ORCHESTRATE_FLOOR_DIR"] = floor_dir
    if ttl_min is not None:
        env["ORCHESTRATE_MERGE_AUTH_TTL_MIN"] = str(ttl_min)
    if tmux is None:
        env.pop("TMUX", None)
    else:
        env["TMUX"] = tmux
    p = subprocess.run(["bash", HELPER] + args, env=env, capture_output=True, text=True, timeout=15)
    return p.returncode, p.stdout, p.stderr, floor_dir, home


def token_path(floor_dir, tmux="/tmp/tmux-x,1,0"):
    return os.path.join(floor_dir, "merge-auth", _key(tmux))


def main():
    print("== PASS path: token armed ==")
    rc, out, err, fd, _ = run(["265"], ttl_min=10)
    tok = token_path(fd)
    check("preflight PASS -> exit 0", rc == 0)
    check("token file written", os.path.exists(tok))
    if os.path.exists(tok):
        doc = json.load(open(tok))
        check("token.pr == 265", doc.get("pr") == 265)
        check("token.head_sha == emitted SHA", doc.get("head_sha") == SHA)
        now = int(time.time())
        check("token.expiry ~ now + 10m", isinstance(doc.get("expiry"), int) and now + 500 <= doc["expiry"] <= now + 700)
        mode = stat.S_IMODE(os.stat(tok).st_mode)
        check("token mode is 0600", mode == 0o600)
    check("stdout prints the --match-head-commit merge command", "--match-head-commit" in out and SHA in out)

    print("== BLOCK path: NO token ==")
    rc, out, err, fd, _ = run(["265"], preflight_mode="block")
    check("preflight BLOCK -> exit 2", rc == 2)
    check("no token written on BLOCK", not os.path.exists(token_path(fd)))

    print("== PASS but no headRefOid emitted: NO token ==")
    rc, out, err, fd, _ = run(["265"], preflight_mode="pass_no_sha")
    check("PASS without headRefOid -> exit 2", rc == 2)
    check("no token written when SHA missing", not os.path.exists(token_path(fd)))

    print("== usage / guards ==")
    rc, _, _, fd, _ = run([], preflight_mode="pass")
    check("no <pr> arg -> exit 1 (usage)", rc == 1)
    rc, _, _, fd, _ = run(["abc"], preflight_mode="pass")
    check("non-numeric <pr> -> exit 1 (usage)", rc == 1)
    rc, _, _, fd, _ = run(["265"], tmux=None, preflight_mode="pass")
    check("no $TMUX (not an orchestrate session) -> exit 1, no token", rc == 1 and not os.path.exists(token_path(fd)))

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
