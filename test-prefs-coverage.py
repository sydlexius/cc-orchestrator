#!/usr/bin/env python3
"""Proof harness for scripts/prefs-coverage.py (.prefs.toml coverage gate, #201).

Stdlib-only, subprocess-driven (repo convention). Builds throwaway git repos with
a base commit on `main` + a feature change, then asserts the consumer's exit code
and output: HONORS / MISSING-blocked / MISSING-exempted / DRIFT / self-skip /
CONFIG / nothing-in-scope. Uses TOML LITERAL strings for `verify` so the regex
backslashes survive intact.
"""
import os
import sys
import subprocess
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
CONSUMER = os.path.join(HERE, "scripts", "prefs-coverage.py")

_fail = 0


def check(desc, cond):
    global _fail
    print(f"  [{'ok  ' if cond else 'FAIL'}] {desc}")
    if not cond:
        _fail += 1


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def write(repo, path, content):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(content)


def setup(base_files, head_files):
    """Temp repo: commit base_files on `main`, then head_files on a feature branch."""
    td = tempfile.mkdtemp()
    git(td, "init", "-q")
    git(td, "config", "user.email", "t@t.t")
    git(td, "config", "user.name", "t")
    git(td, "checkout", "-q", "-b", "main")
    for p, c in base_files.items():
        write(td, p, c)
    git(td, "add", "-A")
    git(td, "commit", "-q", "-m", "base")
    git(td, "checkout", "-q", "-b", "feat")
    for p, c in head_files.items():
        write(td, p, c)
    git(td, "add", "-A")
    git(td, "commit", "-q", "-m", "feat")
    return td


def run(repo):
    r = subprocess.run([sys.executable, CONSUMER], cwd=repo,
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


# A one-pref manifest (density -> must consume var(--sw-density-) in web/templates/*.templ).
# verify uses a TOML LITERAL string so `\(` reaches the regex intact.
MANIFEST = (
    "[[pref]]\n"
    'key = "density"\n'
    'applies_via = "data-attr"\n'
    'mechanism = "[data-density]; var(--sw-density-*)"\n'
    'surface = "web/templates/*.templ"\n'
    "verify = 'var\\(--sw-density-'\n"
    'severity = "high"\n'
)
HONORING = "<div style=\"padding: var(--sw-density-row-py)\">logs</div>\n"
IGNORING = "<div class=\"logs\">logs</div>\n"

repos = []


def main():
    # 1. self-skip: no .prefs.toml
    r = setup({"a.txt": "x\n"}, {"a.txt": "y\n"})
    repos.append(r)
    rc, out = run(r)
    check("self-skip (no .prefs.toml) -> exit 0", rc == 0 and "self-skip" in out)

    # 2. HONORS: a changed governed surface consumes the mechanism
    r = setup({".prefs.toml": MANIFEST, "web/templates/logs.templ": "old\n"},
              {"web/templates/logs.templ": HONORING})
    repos.append(r)
    rc, out = run(r)
    check("HONORS (changed surface consumes verify) -> exit 0", rc == 0 and "HONORS" in out)

    # 3. MISSING-blocked: changed governed surface does NOT consume it, no exemption
    r = setup({".prefs.toml": MANIFEST, "web/templates/logs.templ": "old\n"},
              {"web/templates/logs.templ": IGNORING})
    repos.append(r)
    rc, out = run(r)
    check("MISSING (no verify, not exempt) -> exit 1", rc == 1 and "MISSING" in out)

    # 4. MISSING-exempted: same, but the surface+pref is in [[exempt]]
    exempt_manifest = MANIFEST + (
        "\n[[exempt]]\n"
        'surface = "web/templates/logs.templ"\n'
        'prefs = ["density"]\n'
        'reason = "logs viewer is fixed-width monospace; density N/A"\n'
    )
    r = setup({".prefs.toml": exempt_manifest, "web/templates/logs.templ": "old\n"},
              {"web/templates/logs.templ": IGNORING})
    repos.append(r)
    rc, out = run(r)
    check("MISSING-exempted -> exit 0, prints reason",
          rc == 0 and "EXEMPT" in out and "density N/A" in out)

    # 4b. HIGH regression: a STRING [[exempt]].prefs must not SUBSTRING-exempt a pref.
    #     pref "font" must NOT be exempted by prefs = "font_family" (a bare string).
    substr_manifest = (
        "[[pref]]\n"
        'key = "font"\n'
        'applies_via = "class"\n'
        'mechanism = "m"\n'
        'surface = "web/templates/*.templ"\n'
        "verify = 'FONTMARKER'\n"
        'severity = "high"\n'
        "\n[[exempt]]\n"
        'surface = "web/templates/*.templ"\n'
        'prefs = "font_family"\n'  # STRING, not a list -- must not substring-match "font"
        'reason = "unrelated"\n'
    )
    r = setup({".prefs.toml": substr_manifest, "web/templates/x.templ": "old\n"},
              {"web/templates/x.templ": "no marker here\n"})
    repos.append(r)
    rc, out = run(r)
    check("string [[exempt]].prefs does NOT substring-exempt (font vs font_family) -> exit 1",
          rc == 1 and "MISSING" in out)

    # 4c. a legit string [[exempt]].prefs (exact key) DOES exempt (normalized to a list)
    exact_str = (
        "[[pref]]\n"
        'key = "density"\n'
        'applies_via = "data-attr"\n'
        'mechanism = "m"\n'
        'surface = "web/templates/*.templ"\n'
        "verify = 'var\\(--sw-density-'\n"
        'severity = "high"\n'
        "\n[[exempt]]\n"
        'surface = "web/templates/*.templ"\n'
        'prefs = "density"\n'  # STRING, exact key -> normalized to ["density"], exempts
        'reason = "ok"\n'
    )
    r = setup({".prefs.toml": exact_str, "web/templates/x.templ": "old\n"},
              {"web/templates/x.templ": "no marker\n"})
    repos.append(r)
    rc, out = run(r)
    check("string [[exempt]].prefs with exact key -> exempts (exit 0)",
          rc == 0 and "EXEMPT" in out)

    # 5. nothing-in-scope: changed file does not match any pref.surface
    r = setup({".prefs.toml": MANIFEST, "internal/api/handler.go": "old\n"},
              {"internal/api/handler.go": "new\n"})
    repos.append(r)
    rc, out = run(r)
    check("nothing-in-scope (no surface match) -> exit 0", rc == 0)

    # 5b. single-* surface glob spans subdirectories (fnmatch: * matches across /)
    r = setup({".prefs.toml": MANIFEST, "web/templates/sub/logs.templ": "old\n"},
              {"web/templates/sub/logs.templ": HONORING})
    repos.append(r)
    rc, out = run(r)
    check("surface glob spans subdirs (single-* fnmatch) -> HONORS, exit 0",
          rc == 0 and "HONORS" in out and "sub/logs.templ" in out)

    # 6. DRIFT: a [[pref]].key absent from [source].list_cmd output
    drift_manifest = (
        "[source]\n"
        "list_cmd = \"printf 'theme\\\\nfont_size\\\\n'\"\n\n"
        + MANIFEST  # density is NOT in {theme, font_size}
    )
    r = setup({".prefs.toml": drift_manifest, "web/templates/logs.templ": "old\n"},
              {"web/templates/logs.templ": HONORING})
    repos.append(r)
    rc, out = run(r)
    check("DRIFT ([[pref]].key not in [source]) -> exit 2", rc == 2 and "DRIFT" in out)

    # 7. no drift when the key IS in [source]
    ok_src = (
        "[source]\n"
        "list_cmd = \"printf 'density\\\\ntheme\\\\n'\"\n\n"
        + MANIFEST
    )
    r = setup({".prefs.toml": ok_src, "web/templates/logs.templ": "old\n"},
              {"web/templates/logs.templ": HONORING})
    repos.append(r)
    rc, out = run(r)
    check("no-drift (key in [source]) + HONORS -> exit 0", rc == 0 and "HONORS" in out)

    # 8. CONFIG: bad verify regex -> exit 2 (fails closed)
    bad_rx = (
        "[[pref]]\n"
        'key = "x"\n'
        'applies_via = "class"\n'
        'mechanism = "m"\n'
        'surface = "web/templates/*.templ"\n'
        "verify = '('\n"  # unbalanced paren -> re.error
        'severity = "low"\n'
    )
    r = setup({".prefs.toml": bad_rx, "web/templates/logs.templ": "old\n"},
              {"web/templates/logs.templ": "new\n"})
    repos.append(r)
    rc, out = run(r)
    check("CONFIG (bad verify regex) -> exit 2", rc == 2 and "CONFIG" in out)

    # 9. parse error -> exit 2
    r = setup({".prefs.toml": "this is = = not toml\n", "web/templates/logs.templ": "o\n"},
              {"web/templates/logs.templ": "n\n"})
    repos.append(r)
    rc, out = run(r)
    check("parse error -> exit 2", rc == 2)

    for r in repos:
        shutil.rmtree(r, ignore_errors=True)

    print()
    if _fail:
        print(f"FAILED: {_fail} check(s) failed")
        return 1
    print("all prefs-coverage checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
