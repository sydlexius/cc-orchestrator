#!/usr/bin/env python3
"""prefs-coverage.py -- .prefs.toml UI-preference coverage gate (Phase 2, #201).

Reads `.prefs.toml` at the repo root (schema:
skills/orchestrate/templates/prefs.toml.md). For each `[[pref]]`, greps the
DIRECTLY-CHANGED surfaces (`git diff BASE..HEAD`, BASE resolved like
patch-coverage.sh) that match the pref's `surface` glob for its `verify` regex.
A matching changed surface that does NOT reference the mechanism is a MISSING
finding -- a HARD failure UNLESS the surface+pref is covered by an `[[exempt]]`
block (printed with its reason). This is the "necessary" static check; the
adversarial-review charter's rendered Playwright pass is the "sufficient" one.

Design decisions (from the #200/#201 brainstorm): uniform hard-gate + per-surface
opt-out; narrow surface = directly-changed files; verify is necessary-not-
sufficient; absent-manifest self-skip.

Exit codes: 0 = pass, self-skip, or nothing-in-scope; 1 = un-exempted MISSING;
2 = config / parse error (fails closed).
"""
import sys
import os
import re
import subprocess
import fnmatch

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    print("prefs-coverage: requires Python 3.11+ (tomllib).", file=sys.stderr)
    sys.exit(2)


def sh(args, timeout=120):
    # Bounded so a hung git op fails fast (rc 124) instead of burning the job budget.
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"timed out after {timeout}s")


def resolve_base():
    # Mirror patch-coverage.sh's BASE fallback chain. If NONE of these refs exist
    # (fresh / unrelated history), returns None -> changed_files falls back to
    # `git diff HEAD` (working-tree only), so the gate fails OPEN rather than
    # treating the whole tree as changed. Intentional, matching patch-coverage.sh.
    for ref in ("origin/main", "main", "origin/master", "master"):
        if sh(["git", "rev-parse", "--verify", "-q", ref]).returncode == 0:
            return ref
    return None


def changed_files(base):
    if base:
        mb = sh(["git", "merge-base", base, "HEAD"]).stdout.strip()
        rng = f"{mb}..HEAD" if mb else "HEAD"
    else:
        rng = "HEAD"
    out = sh(["git", "diff", "--name-only", rng]).stdout
    return [f for f in out.splitlines() if f.strip()]


def main():
    root = sh(["git", "rev-parse", "--show-toplevel"]).stdout.strip() or os.getcwd()
    os.chdir(root)  # anchor all subsequent git ops to the repo root (robust from nested cwd)
    manifest = os.path.join(root, ".prefs.toml")
    if not os.path.isfile(manifest):
        print("prefs-coverage: no .prefs.toml -- self-skip (no UI-preference manifest).")
        return 0
    try:
        with open(manifest, "rb") as fh:
            cfg = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError) as e:
        print(f"prefs-coverage: .prefs.toml parse error: {e}", file=sys.stderr)
        return 2

    # Fail CLOSED on malformed-but-valid TOML shapes. A raw crash would exit 1 and
    # masquerade as a MISSING finding; config/shape errors must be a clean exit 2.
    prefs = cfg.get("pref", [])
    exempts = cfg.get("exempt", [])
    if not isinstance(prefs, list) or not all(isinstance(p, dict) for p in prefs):
        print("prefs-coverage: CONFIG -- [[pref]] must be an array of tables.", file=sys.stderr)
        return 2
    if not isinstance(exempts, list) or not all(isinstance(e, dict) for e in exempts):
        print("prefs-coverage: CONFIG -- [[exempt]] must be an array of tables.", file=sys.stderr)
        return 2
    if not prefs:
        print("prefs-coverage: .prefs.toml has no [[pref]] entries -- nothing to check.")
        return 0

    # NOTE: [source] is a human-read pointer only (file/docs) -- this tool does NOT
    # execute anything from it. A key-DRIFT check that ran the repo's key-emitting
    # command was deliberately dropped (#201): executing any PR-controlled command in
    # CI is an RCE vector, and the drift check is optional; it can return later behind
    # a trusted-context gate. The enumeration-from-authoritative-source discipline
    # stays a reviewer concern (the adversarial-review charter), not a shell exec here.
    changed = changed_files(resolve_base())
    if not changed:
        print("prefs-coverage: no changed files in range -- nothing to check.")
        return 0

    # Normalize/validate [[exempt]].prefs up front. TOML allows a bare string, and
    # `key in "font_family"` is SUBSTRING membership -- it would falsely exempt a pref
    # named "font". Coerce a lone string to a 1-element list; filter a list to strings;
    # reject any other type as a CONFIG error (fail closed).
    norm_exempts = []
    for ex in exempts:
        pv = ex.get("prefs", [])
        if isinstance(pv, str):
            pv = [pv]
        elif isinstance(pv, list):
            pv = [x for x in pv if isinstance(x, str)]
        else:
            print(f"prefs-coverage: CONFIG -- [[exempt]].prefs must be a string or "
                  f"array of strings: {ex}", file=sys.stderr)
            return 2
        norm_exempts.append((ex.get("surface", ""), pv, ex.get("reason", "(no reason given)")))

    def exemption(path, key):
        for glob, pv, reason in norm_exempts:
            if glob and fnmatch.fnmatch(path, glob) and key in pv:
                return reason
        return None

    missing = []
    honored = 0
    for p in prefs:
        key, surf, verify = p.get("key"), p.get("surface"), p.get("verify")
        if not (key and surf and verify):
            print(f"prefs-coverage: CONFIG -- each [[pref]] needs key + surface + verify: {p}",
                  file=sys.stderr)
            return 2
        try:
            rx = re.compile(verify)
        except re.error as e:
            print(f"prefs-coverage: CONFIG -- bad verify regex for pref {key!r}: {e}",
                  file=sys.stderr)
            return 2
        for path in changed:
            if not fnmatch.fnmatch(path, surf):
                continue
            full = os.path.join(root, path)
            if not os.path.isfile(full):
                continue  # deleted/renamed-away in the range
            with open(full, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
            if rx.search(body):
                honored += 1
                print(f"  [HONORS ] {path}  ({key})")
            else:
                reason = exemption(path, key)
                if reason is not None:
                    print(f"  [EXEMPT ] {path}  ({key}) -- {reason}")
                else:
                    missing.append((path, key))
                    print(f"  [MISSING] {path}  ({key}) -- no match for /{verify}/")

    if missing:
        print(f"\nprefs-coverage: {len(missing)} un-exempted MISSING (surface x pref). "
              "Wire the pref into the surface, or add an [[exempt]] block with a reason.",
              file=sys.stderr)
        return 1
    print(f"\nprefs-coverage: OK ({honored} honored, no un-exempted misses).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
