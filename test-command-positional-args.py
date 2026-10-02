#!/usr/bin/env python3
"""No command body may contain a dollar sign directly followed by a digit (issue #507).

Claude Code substitutes `$<digit>` in a command body BEFORE any shell sees it, and its
indexing is 0-based, so a bash-style positional reference binds the wrong argument (measured:
`/autofix-pr 506` rendered `pr_number="1800"`). Commands must parse `$ARGUMENTS` with `read`
and use the `$(N)` form for awk fields. The rule is deliberately file-wide, prose included:
a regex that tried to tell "shell block" from "prose" would be the fuzzy matcher this repo
avoids, and the substitution does not care where in the file the token sits.

Usage: python3 test-command-positional-args.py [commands-dir]   (default: ./commands)
Stdlib only, no network, read-only. Exit 0 clean / 1 violation / 2 cannot check.
"""
import os
import re
import sys

# `\{?` also catches the braced form (`${1}`, `${2:-x}`): whether Claude Code substitutes it is
# unverified, so it is refused on doubt. `$@`/`$#` carry no digit and are not known to be substituted.
PATTERN = re.compile(r"\$\{?[0-9]")
MIN_FILES = 10  # parse-sanity floor: an empty or wrong dir must not read as clean


def scan(cmd_dir):
    hits = []
    names = sorted(n for n in os.listdir(cmd_dir) if n.endswith(".md"))
    for name in names:
        with open(os.path.join(cmd_dir, name), encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if PATTERN.search(line):
                    hits.append((name, lineno, line.rstrip()))
    return names, hits


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    cmd_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "commands")
    if not os.path.isdir(cmd_dir):
        print("cannot check: not a directory: %s" % cmd_dir, file=sys.stderr)
        return 2
    names, hits = scan(cmd_dir)
    if len(names) < MIN_FILES:
        print("[FAIL] sanity floor: only %d command files in %s (need >= %d)"
              % (len(names), cmd_dir, MIN_FILES))
        return 1
    for name, lineno, line in hits:
        print("[FAIL] %s:%d has a dollar-digit token: %s" % (name, lineno, line.strip()[:120]))
    if hits:
        return 1
    print("[PASS] %d command files, no dollar-digit tokens" % len(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
