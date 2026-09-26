#!/usr/bin/env python3
"""Bash 3.2 construct gate (#463): fail on bash-4-only syntax in shipped shell.

WHY. macOS /bin/bash is 3.2 and shellcheck does NOT flag bash-4-only constructs (it lints
against the shebang's dialect, and `bash` means "a modern bash" to it). #460 fixed
`scripts/orchestrate-status.sh`, which used `mapfile` and so exited 127 on a stock Mac while
every gate stayed green. This is the EARLY SIGNAL; the PRIMARY guard is the macOS CI leg,
which puts a `bash -> /bin/bash` shim first on PATH so every harness actually RUNS the
scripts under 3.2 (a grep cannot see runtime-only differences such as `"${arr[@]}"` on an
empty array under `set -u`, which is an unbound-variable error before bash 4.4).

SCOPE. Every `scripts/*.sh` (glob, so a new script cannot be left out) plus the fenced
```bash / ```sh / ```shell / ```zsh (or ~~~) blocks of `commands/*.md`. Comments are ignored: whole-line comments, and a
trailing `#` comment found by a small quote-aware scan (a `#` inside quotes or `${#x}` is
not a comment). Quoted strings are still scanned: a construct inside `bash -c '...'` is code.

ALLOW MARKER. A line whose trailing COMMENT is `# bash32-ok: <reason>` is skipped (the marker
inside a quoted string is code, not a marker, so it cannot hide a construct on that line). The reason is REQUIRED
(an empty one fails), because the marker is a decision to ship a construct that breaks on a
stock Mac and a reviewer must be able to dispute it. None is in use today.

KNOWN FALSE POSITIVES. The scan is a regex over source text, so a construct's spelling inside
a regex or a sed replacement is flagged as if it were shell syntax, e.g. `grep -E 'a|&b'`
(reads as `|&`) or `sed 's/x/&>>/'` (reads as `&>>`). The remedy is the allow marker with a
reason saying so; rewording the regex is also fine.

Failure output is `path:line: <construct name>: <source line>`, one per hit.
The file ends with a self-test: every construct pattern must catch its own bad sample, a
list of bash-3.2-valid lines must stay silent, and a fixture tree with `mapfile` injected into
a copy of a real script must fail. Stdlib only. Run: python3 test-bash32-constructs.py
"""
import glob
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.environ.get("BASH32_ROOT") or os.path.dirname(os.path.abspath(__file__))

# (name, pattern, a line the pattern MUST catch). Bash 3.2 `declare` takes only -afFirtxp,
# so any other attribute letter below is a bash 4+ attribute.
CONSTRUCTS = [
    ("mapfile/readarray", r"\b(?:mapfile|readarray)\b", "mapfile -t lines < f"),
    ("declare/local/readonly -A/-n/-l/-u/-g/-c (bash 4 attribute)",
     r"\b(?:declare|typeset|local|readonly)\s+(?:-[a-zA-Z]+\s+)*-[a-zA-Z]*[Anlugc]",
     "local -n ref=$1"),
    ("bare local - (bash 4.4)", r"\blocal\s+-(?=\s|;|$)", "local -"),
    ("case modification ${x,,} ${x^^} ${x,} ${x^}",
     r"\$\{[#!]?(?:\w+|[@*])(?:\[[^]]*\])?(?:,,|\^\^|,|\^)", 'lower="${name,,}"'),
    ("${x@...} parameter transform", r"\$\{[#!]?(?:\w+|[@*])(?:\[[^]]*\])?@[QEPAaKkUuL]\}",
     'printf "%s" "${v@Q}"'),
    ("negative array index", r"\w\[\s*-\d+\s*\]", 'last="${arr[-1]}"'),
    ("wait -n/-f/-p", r"\bwait\s+-[a-z]*[nfp]", "wait -n"),
    ("read -i/-N", r"\bread\s+(?:-\w+\s+(?:[^-\s]\S*\s+)?)*-[a-zA-Z]*[iN]", "read -r -i def x"),
    ("read -t fractional timeout",
     r"\bread\s+(?:-\w+\s+(?:[^-\s]\S*\s+)?)*-[a-zA-Z]*t\s*\d*\.\d", "read -t 0.5 x"),
    ("bash 4+/5 variable (EPOCH*, BASHPID, SRANDOM, BASH_ARGV0)",
     r"\b(?:EPOCHREALTIME|EPOCHSECONDS|BASHPID|SRANDOM|BASH_ARGV0)\b", "t=$EPOCHSECONDS"),
    ("shopt bash 4 option",
     r"\bshopt\s+(?:-[a-z]+\s+)+[\w ]*\b(?:globstar|lastpipe|autocd|checkjobs|direxpand|dirspell|"
     r"globasciiranges|inherit_errexit|compat\d+)\b",
     "shopt -s globstar"),
    ("|& pipe", r"(?<!\|)\|&", "cmd |& tee log"),
    ("&>> append", r"&>>", "cmd &>> log"),
    ("case fallthrough ;;& / ;&", r"(?:;;&|(?<!;);&)(?=\s|$)", "  a) x ;;&"),
    ("coproc", r"\bcoproc\b", "coproc cat"),
    ("printf %(fmt)T", r"%\([^)]*\)T", "printf '%(%s)T' -1"),
    ("[[ -v var ]] / test -v", r"(?:\[\[?|\btest)\s+(?:!\s+)?-v\s", "[[ -v HOME ]]"),
    ("brace expansion step {a..b..n}", r"\{-?\w+\.\.-?\w+\.\.-?\d+\}", "for i in {1..9..2}; do"),
    ("named fd {var}> redirection", r"(?<![$\w])\{[A-Za-z_]\w*\}[<>]", "exec {fd}>log"),
]
COMPILED = [(n, re.compile(p), s) for n, p, s in CONSTRUCTS]

# Bash-3.2-valid lines that must NOT match (each one is a near-miss of a pattern above).
SAFE = [
    'n="${#arr[@]}"', 'x="${y:-,}"', 'x="${y:+^}"', 'x="${y/,/;}"', 'x="${y#,}"',
    'last="${arr[${#arr[@]}-1]}"', 'tail="${arr[@]: -1}"', '[ "$a" -eq -1 ]',
    "cmd 2>&1 | tee log", "a || b", "awk '/[;&|]/'", "cmd >> log 2>&1", "  a) x ;;", "local -a xs=()",
    "declare -r X=1", "local -i n=0", 'names="${!pre@}"', "wait $pid", "shopt -s nullglob",
    "for i in {1..9}; do", 'echo "${x}>"', "printf '%s\\n' x", "[ -n \"$v\" ]",
    'x="${*:-,}"', "local -a", "read -r -t 5 x", "read -rn1 c", "readonly -a X", "echo $RANDOM",
    "shopt -s -q nullglob", "mapfile -t x  # bash32-ok: fixture", "IFS= read -r -d '' x",
]

# (construct name, a line it MUST catch): extra samples for the widened patterns, and the
# comment/marker scan edge cases (an escaped quote must not flip quote state, and a marker
# inside a quoted string is not a marker).
MUST_CATCH = [
    ("readonly", "readonly -A MAP=()"), ("case modification", 'l="${@,,}"'),
    ("case modification", 'u="${*^^}"'), ("parameter transform", 'q="${@@Q}"'),
    ("read -i", "read -N 1 c"), ("read -i", "read -rN1 c"), ("read -t", "read -t0.25 x"),
    ("SRANDOM", "r=$SRANDOM"), ("BASH_ARGV0", "BASH_ARGV0=x"), ("bare local", "local -; set -f"),
    ("shopt", "shopt -s inherit_errexit"), ("shopt", "shopt -qs globstar"),
    ("shopt", "shopt -s -q lastpipe"), ("mapfile", 'echo "a\\"b #" ; mapfile -t x'),
    ("mapfile", 'echo "# bash32-ok: sneaky"; mapfile -t x'),
    ("without a reason", "mapfile -t x # bash32-ok:"),
]

MARKER = re.compile(r"#\s*bash32-ok:(.*)$")
FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})\s*(?:bash|sh|shell|zsh)(?:\s[^`]*)?$")


def strip_comment(line):
    """Drop a trailing shell comment: an unquoted `#` at the start of a word."""
    sq = dq = False
    prev = " "
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and not sq:  # the escaped character is literal: skip it too
            prev = "x"
            i += 2
            continue
        if ch == "'" and not dq:
            sq = not sq
        elif ch == '"' and not sq:
            dq = not dq
        elif ch == "#" and not sq and not dq and prev in " \t;|&(":
            return line[:i]
        prev = ch
        i += 1
    return line


def line_hits(ln):
    """Names of the constructs on one shell line ([] if clean, a comment, or allow-marked)."""
    if ln.lstrip().startswith("#"):
        return []
    code = strip_comment(ln)
    mk = MARKER.search(ln[len(code):])  # the marker counts only in the trailing comment
    if mk:
        return [] if mk.group(1).strip() else ["bash32-ok marker without a reason"]
    return [name for name, rx, _ in COMPILED if rx.search(code)]


def shell_lines(path):
    """Yield (lineno, text) of the shell code in `path`."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    if not path.endswith(".md"):
        yield from enumerate(lines, 1)
        return
    fence = None  # the opening fence's run (e.g. "```" or "~~~~"); closes on the same kind
    for n, ln in enumerate(lines, 1):
        s = ln.strip()
        if fence is None:
            m = FENCE_OPEN.match(ln)
            if m:
                fence = m.group(1)
        elif s.startswith(fence) and not s.strip(fence[0]):
            fence = None
        else:
            yield n, ln


def scan(root):
    files = sorted(glob.glob(os.path.join(root, "scripts", "*.sh"))
                   + glob.glob(os.path.join(root, "commands", "*.md")))
    hits = []
    md_lines = 0
    for path in files:
        rel = os.path.relpath(path, root)
        for n, ln in shell_lines(path):
            md_lines += path.endswith(".md")
            for name in line_hits(ln):
                hits.append(f"{rel}:{n}: {name}: {ln.strip()}")
    return files, hits, md_lines


FENCE_FIXTURE = """intro mapfile
```shell
x1
```
```python
mapfile
```
~~~bash
x2
```
x3
~~~
```sh title="demo"
x4
````
```zsh
x5
```
"""


def selftest():
    bad = [f"'{s}' not caught by {n}" for n, _, s in COMPILED if n not in line_hits(s)]
    bad += [f"'{s}' not caught by {n}" for n, s in MUST_CATCH
            if not any(n in h for h in line_hits(s))]
    fp = [f"'{s}' flagged by {h}" for s in SAFE for h in line_hits(s)]
    if strip_comment("x=1 # mapfile") != "x=1 " or "mapfile" not in strip_comment("echo '#' mapfile"):
        bad.append("strip_comment mis-handles a trailing or quoted #")
    with tempfile.TemporaryDirectory() as tmp:
        md = os.path.join(tmp, "f.md")
        with open(md, "w") as fh:
            fh.write(FENCE_FIXTURE)
        got = [ln for _, ln in shell_lines(md)]
    if got != ["x1", "x2", "```", "x3", "x4", "x5"]:
        bad.append(f"fence extraction wrong: {got}")
    if bad or fp:
        sys.exit("FAIL: pattern self-test:\n  " + "\n  ".join(bad + fp))
    print(f"  [ok  ] {len(COMPILED)} patterns + {len(MUST_CATCH)} edge lines caught; "
          f"{len(SAFE)} safe lines silent; fence extraction exact")

    # Mutation proof: a fixture copy of a real script with `mapfile` injected must fail,
    # naming the file, the line and the construct.
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "scripts"))
        src = os.path.join(ROOT, "scripts", "orchestrate-status.sh")
        with open(src, encoding="utf-8") as fh:
            body = fh.read().splitlines()
        body.insert(1, 'mapfile -t prs < <(printf "1\\n")')
        with open(os.path.join(tmp, "scripts", "orchestrate-status.sh"), "w") as fh:
            fh.write("\n".join(body) + "\n")
        r = subprocess.run([sys.executable, os.path.abspath(__file__)], capture_output=True,
                           text=True, env={**os.environ, "BASH32_ROOT": tmp})
        want = "scripts/orchestrate-status.sh:2: mapfile/readarray"
        if r.returncode == 0 or want not in r.stdout + r.stderr:
            sys.exit(f"FAIL: mutation self-test: injected mapfile not reported as '{want}' "
                     f"(rc={r.returncode}):\n{r.stdout}{r.stderr}")
    print("  [ok  ] mutation self-test: injected mapfile fails with file:line + construct")


def main():
    print("bash 3.2 construct gate (#463)")
    files, hits, md_lines = scan(ROOT)
    if not os.environ.get("BASH32_ROOT"):
        # Parse-sanity floor: a broken glob or fence extractor scans nothing and passes.
        n_sh = sum(1 for f in files if f.endswith(".sh"))
        n_md = len(files) - n_sh
        if n_sh < 10 or n_md < 5 or md_lines == 0:
            sys.exit(f"FAIL: scanned {n_sh} scripts/*.sh, {n_md} commands/*.md, {md_lines} fenced "
                     "shell lines - the glob or fence extractor broke (scanning nothing passes)")
        selftest()
    if hits:
        print("FAIL: bash-4-only constructs (macOS /bin/bash is 3.2):")
        for h in hits:
            print("  " + h)
        sys.exit(1)
    print(f"  [ok  ] no bash-4-only construct in {len(files)} files ({md_lines} fenced md lines)")
    print("\nok: shipped shell is bash 3.2 clean (by construct grep)")


if __name__ == "__main__":
    main()
