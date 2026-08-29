#!/usr/bin/env python3
"""Every orchestrate-steer.sh nudge must stay ONE LINE (issue #406).

THE DEFECT THIS EXISTS TO PREVENT. A steer rule is ADVISORY: it exits 0, blocks nothing, and
therefore fires again on the next matching tool call. Its message is re-read by someone who
has already seen it and is not being stopped. So the message must state the FIX, not the
CASE -- the case belongs in the comment block above the rule, where a reader who needs
convincing will actually look.

MEASURED, not assumed: the #231 foreground-Agent nudge had grown to 703 characters -- a
paragraph re-printed on every foreground spawn in a marker session -- and its whole argument
was ALREADY in the comment block directly above it. The five rules totalled 1437 characters.

THE ASYMMETRY THAT MAKES THIS A REAL RULE, and the reason a length cap is not arbitrary
bikeshedding: a BLOCK can be long. It fires once, stops the command, and the author has to
decide what to do next -- they need the argument at that moment. `.claude/hookify.block-lsof.local.md`
is ~30 lines and correctly so. A WARN is the inverse in every respect. One field, two display
contexts, opposite requirements.

WHY A HARNESS AND NOT JUST SHORTER STRINGS. Trimming by hand fixes today's length and
guarantees tomorrow's regrowth: the next person to add a caveat appends it to the message,
because that is where the previous caveats are. Same shape as test-version-lockstep.py and
test-ci-gates-lockstep.py (#364) -- assert the invariant rather than re-syncing it once.

THREE CHECKS, because the obvious one passes while the invariant is broken:
  1. LENGTH CAP per nudge. The direct assertion.
  2. A PARSE-SANITY FLOOR before any verdict. An empty parse checks nothing and PASSES,
     which is precisely how a drift guard becomes decorative (the #330 lesson, and the same
     floor test-ci-gates-lockstep.py carries). If the extraction breaks, that must FAIL
     loudly rather than bless an unmeasured file.
  3. NO EMBEDDED NEWLINE. A multi-line nudge defeats the cap by construction -- three short
     lines pass a per-string length check and still print a paragraph.

Stdlib only, no network, read-only. Mirrors the house harness style (no pytest).
"""

import re
import sys
from pathlib import Path

# Per-nudge ceiling. Chosen from the measured corpus rather than picked round: after
# trimming, the longest legitimate nudge is 166 chars (the gh-* wrapper rule, which is long
# only because it ENUMERATES the six wrappers -- a list, not an argument). 200 leaves room
# for a wrapper to be added without inviting a paragraph. If a nudge genuinely cannot fit,
# that is the signal its rationale belongs in the comment block, not that the cap is wrong.
MAX_NUDGE_CHARS = 200

# The extraction must find at least this many. Five rules exist today (#95, #159, #226, #231).
# A floor below the real count catches a broken regex; setting it AT the count would make
# adding a sixth rule fail for the wrong reason.
MIN_NUDGES = 5

STEER = Path(__file__).parent / "scripts" / "orchestrate-steer.sh"

# Match `emit_warn "..."` allowing escaped quotes inside the string.
#
# ACCEPTED LIMITATION, stated so this is not mistaken for airtight enforcement: shell
# concatenates ADJACENT string literals, so `emit_warn "short""also short"` is one long
# message at runtime while this captures only the first segment. Not defended against,
# for the same reason the security floor documents its own quoted-flag false negatives --
# the threat model is an author appending a caveat to a message, not one evading a style
# check. A contributor who splits a nudge across adjacent literals to dodge the cap has
# read this comment and decided to anyway, which is a review problem, not a parser problem.
NUDGE_RE = re.compile(r'emit_warn\s+"((?:[^"\\]|\\.)*)"')

failures = []


def check(label, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")
    if not cond:
        if detail:
            print(f"         {detail}")
        failures.append(label)


def main():
    print(f"== steer nudge length ({STEER.name}) ==")

    if not STEER.is_file():
        print(f"FAIL: {STEER} not found", file=sys.stderr)
        return 1

    src = STEER.read_text()
    nudges = NUDGE_RE.findall(src)

    # CHECK 2 FIRST, deliberately. Every assertion below is vacuous if the extraction found
    # nothing, and a vacuous pass is worse than a failure because it reads as coverage.
    check(
        f"parse sanity: found >= {MIN_NUDGES} nudges",
        len(nudges) >= MIN_NUDGES,
        f"found {len(nudges)}; the emit_warn regex likely broke -- verdicts below are meaningless",
    )
    if len(nudges) < MIN_NUDGES:
        print(f"\nFAILED: {len(failures)} check(s)", file=sys.stderr)
        return 1

    # CHECK 1: the cap.
    for i, msg in enumerate(nudges, 1):
        n = len(msg)
        check(
            f"nudge {i} is <= {MAX_NUDGE_CHARS} chars (is {n})",
            n <= MAX_NUDGE_CHARS,
            f"{msg[:90]}...\n         "
            "A steer nudge states the FIX; put the argument in the comment block above the rule.",
        )

    # CHECK 3: no embedded newline (a multi-line nudge evades a per-string cap).
    for i, msg in enumerate(nudges, 1):
        check(
            f"nudge {i} is a single line",
            "\\n" not in msg and "\n" not in msg,
            "a multi-line nudge prints a paragraph regardless of each line's length",
        )

    total = sum(len(m) for m in nudges)
    print(f"\n  {len(nudges)} nudges, longest {max(len(m) for m in nudges)}, total {total} chars")

    if failures:
        print(f"\nFAILED: {len(failures)} check(s)", file=sys.stderr)
        return 1
    print("\nALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
