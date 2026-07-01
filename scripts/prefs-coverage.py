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
2 = config / drift / parse error (fails closed).
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


def sh(args):
    return subprocess.run(args, capture_output=True, text=True)


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

    prefs = cfg.get("pref", [])
    exempts = cfg.get("exempt", [])
    if not prefs:
        print("prefs-coverage: .prefs.toml has no [[pref]] entries -- nothing to check.")
        return 0

    # DRIFT guard (best-effort): every [[pref]].key must exist in [source]. Only
    # enforced when [source].list_cmd emits the authoritative keys; otherwise the
    # human-read [source].file is a reviewer concern, not machine-checkable here.
    src = cfg.get("source", {})
    if src.get("list_cmd"):
        r = sh(["bash", "-c", src["list_cmd"]])
        if r.returncode == 0:
            known = {x.strip() for x in r.stdout.splitlines() if x.strip()}
            # Only drift-check prefs that HAVE a key; a keyless [[pref]] is a CONFIG
            # error caught in the per-pref loop below (not a spurious DRIFT [None]).
            drift = [p["key"] for p in prefs if p.get("key") and p["key"] not in known]
            if drift:
                print(f"prefs-coverage: DRIFT -- [[pref]] keys absent from [source]: {drift}",
                      file=sys.stderr)
                return 2

    changed = changed_files(resolve_base())
    if not changed:
        print("prefs-coverage: no changed files in range -- nothing to check.")
        return 0

    def exemption(path, key):
        for ex in exempts:
            glob = ex.get("surface", "")
            if glob and fnmatch.fnmatch(path, glob) and key in ex.get("prefs", []):
                return ex.get("reason", "(no reason given)")
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
