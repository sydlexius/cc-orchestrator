#!/usr/bin/env python3
"""Assert no apostrophe sits inside a single-quoted jq program.

WHY THIS IS A GATE AND NOT A COMMENT. A jq program embedded in a shell script is a
SINGLE-QUOTED string, and POSIX shells give no escape for an apostrophe inside one.
So an apostrophe in jq prose - `CR's`, `Copilot's`, `the reviewer's login` - is not a
style problem, it is a parse event:

  ONE apostrophe  -> the quote closes early and the shell dies loudly at the next
                     paren. Annoying, but self-announcing.
  TWO apostrophes -> they PAIR. The shell closes at the first, treats everything
                     between as UNQUOTED (subject to expansion and globbing), and
                     reopens at the second. `bash -n` sees nothing wrong. The damage
                     surfaces at runtime as a jq compile error, or - worse - as a
                     valid but DIFFERENT program.

That second case is why a comment could never carry this rule: the existing syntax
gate cannot see it. Measured during #376:

    x=$(echo '{"a":1}' | jq '
      # CR's comment and Copilot's comment
      .a')
    bash -n : PASSES
    runtime : jq: 1 compile error

This bit twice in a single pull request - the second time inside the comment that
warned about it, which is the whole argument for making it mechanical. A rule a human
must remember while writing prose is a rule that fails exactly when they are thinking
about prose.

DETECTION. Track shell quote state to find single-quoted regions opened by a `jq`
invocation, then flag any apostrophe inside one. A legitimate apostrophe there does
not exist: by construction it would have ended the region. Anything reported is a real
defect, not a style opinion - zero false positives across all 37 scripts.

KNOWN GAPS, stated because a gate that implies completeness is worse than one whose
limits are written down. This catches the observed shape; it is not a proof.

  MISSED  a possessive PLURAL pair (`the users' logins ... the bots' names`). The
          detector keys on the mid-word signature - a closing quote followed by a word
          character, as in `CR's` - and a plural possessive ends the word, so the next
          character is a space. A wider "next char must be a shell terminator" rule was
          TRIED AND REVERTED: it was reported as costing 0 false positives, but measured
          10 on this repo (a quote closing before ordinary punctuation reads as mid-prose
          under it) and still missed this case. Widening the net is not free here.
  MISSED  a paired apostrophe in a SINGLE-LINE jq program (the newline requirement).
  MISSED  a jq program supplied via `-f file` or composed in a heredoc: the program is
          not a single-quoted argument at all, so this scanner never sees it.
  MISSED  `commands/*.md` - scope is `scripts/*.sh`, yet 7 command files carry
          single-quoted jq programs that agents run verbatim.
  N/A     an apostrophe inside a DOUBLE-quoted jq program or an `--argjson` value is
          ordinary text; ignoring it is correct, not a gap.
  N/A     `JQ=jq; $JQ '...'` - `bash -n` already fails that one loudly.

FIX, when this fires: reword. "CR's vocabulary" -> "the CR vocabulary"; "Copilot's
block" -> "the Copilot block". Never reach for a backslash - there is no escape for
an apostrophe inside a single-quoted shell string, and `'\''` inside a jq program
would corrupt the program itself.

Stdlib only, no network. Exit 0 clean / 1 violations found / 2 usage error.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
FAILS = []


def scan(text):
    """Yield (line_no, col, context) for each apostrophe inside a jq single-quoted region.

    A hand-rolled scanner rather than a regex: quote state is a property of the whole
    preceding file, and a regex cannot carry that state. Double-quoted regions are
    tracked too, because an apostrophe inside one is ordinary text ("don't" in an echo
    is fine) and flagging it would be a false positive that trains people to ignore
    this gate.
    """
    i, line_no, col = 0, 1, 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            line_no += 1
            col = 1
            i += 1
            continue
        # SKIP SHELL COMMENTS -- but ONLY those outside a quoted region, which is what
        # this branch is: the scanner reaches here only when no quote is open (an open
        # single-quoted region is consumed whole below).
        #
        # THE TRADE THIS ENCODES, because getting it backwards makes the gate useless
        # in one direction or noise in the other:
        #   - A `#` line in ORDINARY SHELL is prose. `pr-watch.sh:70` explains a jq
        #     pitfall in English ("jq's `//` operator") in a file `bash -n` calls
        #     clean. Flagging it is a false positive, and a gate that cries wolf on
        #     valid code gets ignored -- worse than absent, because it reads as coverage.
        #   - A `#` line INSIDE a jq program is NOT skipped, and must not be: that is
        #     exactly where the real #376 defect lived. The shell has no idea it is a
        #     comment; it sees an apostrophe and closes the quote. An early version of
        #     this scanner skipped both and PASSED the real mutation -- a decorative
        #     gate that proved nothing while looking thorough.
        if ch == "#" and (i == 0 or text[i - 1] in " \t\n;|&("):
            while i < n and text[i] != "\n":
                i += 1
            continue
        # A backslash escapes the next char OUTSIDE quotes and inside double quotes.
        if ch == "\\":
            i += 2
            col += 2
            continue
        if ch == '"':
            # Skip the whole double-quoted region; an apostrophe in there is literal.
            i += 1
            col += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                    col += 1
                if text[i] == "\n":
                    line_no += 1
                    col = 0
                i += 1
                col += 1
            i += 1
            col += 1
            continue
        if ch == "'":
            # Opening a single-quoted region. Decide whether it is a jq program by
            # looking at the command word immediately before it on this logical line:
            # only a jq invocation matters here, so a shell string like 'don't...' in
            # unrelated code is out of scope (it would be its own bug, not this one).
            # Walk BACK over backslash-continued lines to find the command word. A jq
            # invocation routinely opens its program six lines below the `jq` token:
            #
            #     jq -n -r \
            #       --argjson inline "$all_comments" \
            #       ...
            #       --arg ok "$itemized_resolved_ok" '
            #
            # An earlier version inspected only the CURRENT line, so it never saw the
            # `jq` and classified the largest program in the repo as ordinary text --
            # passing the exact mutation this gate exists to catch. Verified against a
            # real mutant, not reasoned about.
            line_start = text.rfind("\n", 0, i) + 1
            scan_start = line_start
            while scan_start > 0:
                prev_end = text.rfind("\n", 0, scan_start - 1) + 1
                prev = text[prev_end:scan_start - 1]
                if not prev.rstrip().endswith("\\"):
                    break
                scan_start = prev_end
            prefix = text[scan_start:i]
            is_jq = re.search(r"(^|[|(;\s])jq\b[^']*$", prefix, re.S) is not None
            start_line = line_no
            i += 1
            col += 1
            # Consume to the closing quote (no escapes exist inside single quotes).
            body_start = i
            while i < n and text[i] != "'":
                if text[i] == "\n":
                    line_no += 1
                    col = 0
                i += 1
                col += 1
            body = text[body_start:i]
            i += 1
            col += 1
            if is_jq and "\n" in body:
                # A multi-line jq program. If the character FOLLOWING this closing quote
                # is a word character, the quote did not really end the program -- it
                # landed mid-word, i.e. inside prose like CR's. That is the paired
                # -apostrophe signature, and it is the case bash -n cannot see.
                #
                # A WIDER rule was tried and REVERTED. The pre-push review reported that
                # testing for a legitimate shell terminator instead would cost 0 false
                # positives and additionally catch the plural-possessive and single-line
                # gaps. Measured here, it produced 10 FALSE POSITIVES on this repo -- a
                # quote closing before ordinary punctuation reads as mid-prose under that
                # rule. The narrower signature is the one with evidence behind it, so the
                # documented gaps stay documented rather than traded for noise.
                nxt = text[i] if i < n else ""
                if nxt.isalnum() or nxt == "_":
                    tail = body.rsplit("\n", 1)[-1][-60:]
                    yield (line_no, start_line, tail)
            continue
        i += 1
        col += 1


def check(label, ok):
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}")
    if not ok:
        FAILS.append(label)


def main():
    if len(sys.argv) > 1 and sys.argv[1] not in ("--help", "-h"):
        print("usage: test-jq-quoting.py [--help]", file=sys.stderr)
        sys.exit(2)
    if len(sys.argv) > 1:
        print(__doc__)
        sys.exit(0)

    print("== jq single-quote integrity: no apostrophe inside a jq program ==")

    # SELF-TEST FIRST. A scanner that silently matches nothing would pass this gate on
    # every file and read exactly like a clean repo - the #330 lesson, and the reason
    # #324 shipped an unreachable deny twice. Prove it can FAIL before trusting a PASS.
    # The fixture puts the apostrophes on a jq EXPRESSION line, not in a jq `#` comment.
    # A comment line would now be skipped by the comment rule above, and the self-test
    # would pass while proving nothing -- a vacuous fixture is how a gate becomes
    # decorative. The real #376 defect was in a jq comment, but the shell damage is
    # identical either way (the quote closes at the apostrophe regardless), so testing
    # the detectable form keeps the check honest.
    broken_two = "x=$(echo '{\"a\":1}' | jq '\n  .users | map(CR's) | map(Copilot's)\n  ')\n"
    check("self-test: the PAIRED-apostrophe case (which bash -n passes) is DETECTED",
          len(list(scan(broken_two))) > 0)

    clean = "x=$(echo '{\"a\":1}' | jq '\n  .users | map(select(.ok))\n  ')\n"
    check("self-test: an apostrophe-free jq program is CLEAN (no false positive)",
          len(list(scan(clean))) == 0)

    dq = 'echo "do not worry, it is fine"\nx=$(jq \'\n  .a\n  \')\n'
    check("self-test: an apostrophe in a DOUBLE-quoted string is ignored",
          len(list(scan(dq))) == 0)

    # THE FALSE POSITIVE THAT ALMOST SHIPPED. scripts/pr-watch.sh:70 explains a jq
    # pitfall in an English file-header comment. bash -n calls that file clean; the
    # first version of this scanner called it a defect. Pinned so the comment-skip
    # cannot silently regress.
    prose = ("# jq's `// alternative` operator only falls back on null/false, so a\n"
             "# hand-rolled `(.conclusion // \"\")` misreports GitHub's mid-flight state.\n"
             "x=$(jq '\n  .a\n  ')\n")
    check("self-test: apostrophes in a SHELL COMMENT are ignored (the pr-watch.sh:70 "
          "false positive that would have made this gate noise)",
          len(list(scan(prose))) == 0)

    # The real scan, over every shell script the repo ships.
    scripts = sorted(REPO.glob("scripts/*.sh"))
    check(f"found shell scripts to scan (got {len(scripts)})", len(scripts) > 0)

    violations = []
    for path in scripts:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:                      # unreadable file is a real failure,
            violations.append((path, 0, str(exc)))  # never a silent skip
            continue
        for end_line, start_line, tail in scan(text):
            violations.append((path, start_line, tail))

    for path, line, tail in violations:
        print(f"    {path.relative_to(REPO)}:{line}: apostrophe inside a jq program near: ...{tail}")
    check(f"no apostrophe inside any jq program (found {len(violations)})",
          len(violations) == 0)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):")
        for f in FAILS:
            print("  - " + f)
        print("\nFIX: reword the prose. \"CR's\" -> \"the CR\"; \"Copilot's\" -> \"the Copilot\".")
        print("There is NO escape for an apostrophe inside a single-quoted shell string.")
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
