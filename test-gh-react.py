#!/usr/bin/env python3
"""Black-box proof harness for gh-react.sh (issue #234). Stdlib-only, no pytest.

gh-react.sh is the least-privilege wrapper that resolves the LATEST Codoki
issue-level review-summary comment on a PR (author login
`codoki-pr-intelligence[bot]`, preferring one carrying the
`<!-- CODOKI_REVIEW_COMMENT -->` marker with an author-login fallback) and either
READS the current ack state (for the ship-gate oracle) or POSTS a 👍/👎 reaction
(for a human actuation).

SETTLED ack rule (issue #234): ack satisfaction = ANY non-bot login's reaction
(+1 OR -1) on the latest Codoki summary. A bot login's reaction NEVER counts. A
-1 (rebut) ADDITIONALLY requires an `@codoki` reply comment to exist. No summary
=> the ack query reports no-summary / PASS (never fail-closed on absence).

`gh` is stubbed on PATH (a temp python script). For GETs it serves canned JSON
from env vars (applying any --jq via the real jq); for the reactions POST it
LOGS its argv (NUL-separated, lossless) and exits 0 so the construction guarantee
(reactions endpoint only, never /merge) is asserted without a network call.

Run: python3 test-gh-react.py
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(HERE, "scripts", "gh-react.sh")
FAILS = []


def check(label, ok):
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}")
    if not ok:
        FAILS.append(label)


# gh stub: GET -> serve JSON (apply --jq via real jq); mutation (-X POST) -> log argv.
GH_STUB = r'''#!/usr/bin/env python3
import json, os, re, sys, subprocess
args = sys.argv[1:]
ISSUE = os.environ.get("ISSUE_JSON", "[]")
REACTIONS = os.environ.get("REACTIONS_JSON", "[]")
# Per-comment-id reactions: {"<id>": [<reactions>]} -- lets a test give the REAL
# summary and a non-summary comment DIFFERENT reactions (the #234 false-pass proof).
REACTIONS_BY_ID = json.loads(os.environ.get("REACTIONS_BY_ID_JSON", "{}"))
GH_FAIL = os.environ.get("GH_FAIL", "")

def emit(data):
    if "--jq" in args:
        expr = args[args.index("--jq") + 1]
        p = subprocess.run(["jq", "-r", expr], input=data, capture_output=True, text=True)
        sys.stdout.write(p.stdout)
    else:
        sys.stdout.write(data)
    sys.exit(0)

# Mutation: gh api -X POST .../reactions -> log argv losslessly, no network.
if "-X" in args or "--method" in args:
    log = os.environ.get("GH_LOG")
    if log:
        with open(log, "ab") as f:
            for a in args:
                f.write(a.encode() + b"\0")
    sys.exit(0)

if GH_FAIL:
    sys.stderr.write("gh: simulated failure\n"); sys.exit(1)

endpoint = ""
for a in args:
    if a.startswith("repos/"):
        endpoint = a; break
if "/reactions" in endpoint:
    m = re.search(r"/comments/(\d+)/reactions", endpoint)
    if m and m.group(1) in REACTIONS_BY_ID:
        emit(json.dumps(REACTIONS_BY_ID[m.group(1)]))
    emit(REACTIONS)
if endpoint.endswith("/comments") and "/issues/" in endpoint:
    emit(ISSUE)
# Unknown GET -> empty array (keeps jq happy).
emit("[]")
'''


def run(args, *, issue="[]", reactions="[]", reactions_by_id=None, gh_fail=False, repo="owner/repo"):
    """Invoke gh-react.sh with a stubbed gh. Returns (rc, stdout, stderr, posted_argv)."""
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(GH_STUB)
        os.chmod(gh, 0o755)
        log = os.path.join(td, "ghlog")
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["ISSUE_JSON"] = issue
        env["REACTIONS_JSON"] = reactions
        if reactions_by_id is not None:
            import json as _json
            env["REACTIONS_BY_ID_JSON"] = _json.dumps(reactions_by_id)
        env["GH_LOG"] = log
        if repo is not None:
            env["GITHUB_REPOSITORY"] = repo
        if gh_fail:
            env["GH_FAIL"] = "1"
        p = subprocess.run(["bash", WRAPPER] + args, env=env,
                           capture_output=True, text=True, timeout=20)
        posted = []
        if os.path.exists(log):
            with open(log, "rb") as f:
                posted = [t.decode() for t in f.read().split(b"\0") if t]
        return p.returncode, p.stdout, p.stderr, posted


# --- Fixtures ---------------------------------------------------------------
MARKER = "<!-- CODOKI_REVIEW_COMMENT -->"

SUMMARY_MARKED = (
    '{"id":9001,"user":{"login":"codoki-pr-intelligence[bot]"},'
    '"body":"' + MARKER + '\\nReview summary: safe to merge",'
    '"created_at":"2026-07-01T00:00:00Z"}'
)
# A real summary whose MARKER dropped but whose "### Codoki PR Review" header
# remains -> resolves via the header heuristic (format-drift fallback), NOT a blind
# author-login pick.
SUMMARY_NOMARKER = (
    '{"id":9002,"user":{"login":"codoki-pr-intelligence[bot]"},'
    '"body":"### Codoki PR Review\\n**Summary:** looks good",'
    '"created_at":"2026-07-01T00:00:00Z"}'
)
# A NON-summary Codoki comment (no marker, no "Codoki PR Review" header) -- e.g. a
# progress/status note. Must NEVER be selected as the summary (the #234 false-pass).
CODOKI_NONSUMMARY = (
    '{"id":8800,"user":{"login":"codoki-pr-intelligence[bot]"},'
    '"body":"Codoki is reviewing this pull request...",'
    '"created_at":"2026-07-01T05:00:00Z"}'
)
# An OLDER Codoki summary + a NEWER one: the latest (by created_at) must win.
SUMMARY_OLD = (
    '{"id":8000,"user":{"login":"codoki-pr-intelligence[bot]"},'
    '"body":"' + MARKER + '\\nold summary","created_at":"2026-06-01T00:00:00Z"}'
)
CODOKI_REPLY = (
    '{"id":9500,"user":{"login":"sydlexius"},'
    '"body":"@codoki this is a false positive because ...",'
    '"created_at":"2026-07-01T02:00:00Z"}'
)


def issue_arr(*objs):
    return "[" + ",".join(objs) + "]"


def main():
    print("== usage / validation ==")
    rc, out, err, _ = run([])
    check("no subcommand -> nonzero, usage", rc != 0)
    rc, out, err, _ = run(["bogus", "5"])
    check("unknown subcommand -> nonzero", rc != 0)
    rc, out, err, posted = run(["codoki-ack", "abc"])
    check("non-numeric pr -> rc2, no gh POST", rc == 2 and posted == [])
    rc, out, err, posted = run(["codoki-ack", "5", "--react", "up"])
    check("invalid --react value -> rc2, no gh POST", rc == 2 and posted == [])

    print("== READ mode: ack resolution ==")
    # No summary at all -> PASS (never fail-closed on absence).
    rc, out, err, _ = run(["codoki-ack", "5"], issue="[]")
    check("no Codoki summary -> exit 0, prints no-summary",
          rc == 0 and "no-summary" in out and "CODOKI-ACK:" in out)

    # Non-bot +1 satisfies.
    rc, out, err, _ = run(["codoki-ack", "5"], issue=issue_arr(SUMMARY_MARKED),
                          reactions='[{"content":"+1","user":{"login":"sydlexius"}}]')
    check("non-bot +1 -> acked (exit 0)", rc == 0 and "acked" in out and "unacked" not in out)

    # Non-bot -1 WITHOUT an @codoki reply does NOT satisfy.
    rc, out, err, _ = run(["codoki-ack", "5"], issue=issue_arr(SUMMARY_MARKED),
                          reactions='[{"content":"-1","user":{"login":"sydlexius"}}]')
    check("non-bot -1 without @codoki reply -> unacked", rc == 0 and "unacked" in out)

    # Non-bot -1 WITH an @codoki reply DOES satisfy.
    rc, out, err, _ = run(["codoki-ack", "5"],
                          issue=issue_arr(SUMMARY_MARKED, CODOKI_REPLY),
                          reactions='[{"content":"-1","user":{"login":"sydlexius"}}]')
    check("non-bot -1 WITH @codoki reply -> acked", rc == 0 and "acked" in out and "unacked" not in out)

    # A BOT reaction NEVER counts.
    rc, out, err, _ = run(["codoki-ack", "5"], issue=issue_arr(SUMMARY_MARKED),
                          reactions='[{"content":"+1","user":{"login":"codoki-pr-intelligence[bot]"}}]')
    check("bot-only +1 -> unacked (bot reaction never satisfies)", rc == 0 and "unacked" in out)

    # No reactions at all on a present summary -> unacked.
    rc, out, err, _ = run(["codoki-ack", "5"], issue=issue_arr(SUMMARY_MARKED), reactions="[]")
    check("summary present, zero reactions -> unacked", rc == 0 and "unacked" in out)

    # Marker-less summary resolves via the header heuristic (format-drift fallback).
    rc, out, err, _ = run(["codoki-ack", "5"], issue=issue_arr(SUMMARY_NOMARKER),
                          reactions='[{"content":"+1","user":{"login":"sydlexius"}}]')
    check("marker-absent summary resolved via '### Codoki PR Review' header -> acked",
          rc == 0 and "acked" in out and "9002" in out)

    # #234 hostile-review MEDIUM (false-PASS closure): a NON-summary Codoki comment
    # is NEWER and carries a stray +1, while the REAL summary (id 9001) is unacked.
    # Reactions are keyed PER COMMENT ID (CR #253): +1 only on the non-summary 8800,
    # nothing on the real summary 9001 -> proves the resolver reads the SUMMARY's
    # reactions (empty -> unacked), never the non-summary's stray +1.
    rc, out, err, _ = run(["codoki-ack", "5"],
                          issue=issue_arr(SUMMARY_MARKED, CODOKI_NONSUMMARY),
                          reactions_by_id={"8800": [{"content": "+1", "user": {"login": "sydlexius"}}],
                                           "9001": []})
    check("non-summary Codoki comment NOT selected; real summary (9001) wins",
          rc == 0 and "summary 9001" in out and "8800" not in out)
    check("stray +1 on the non-summary is NOT counted; real summary -> unacked (false-PASS closed)",
          "unacked" in out)

    # No marker AND no header on ANY Codoki comment -> refuse to guess: no-summary
    # (PASS, never picks an arbitrary comment) + a loud format-drift WARNING.
    rc, out, err, _ = run(["codoki-ack", "5"], issue=issue_arr(CODOKI_NONSUMMARY),
                          reactions='[{"content":"+1","user":{"login":"sydlexius"}}]')
    check("no recognizable summary -> no-summary (refuses to guess)",
          rc == 0 and "no-summary" in out)
    check("no recognizable summary but Codoki commented -> loud drift WARNING",
          "format may have changed" in err.lower() or "recognized review summary" in err.lower())

    # gh failure resolving the summary -> LOUD nonzero (never silent 'not applicable').
    rc, out, err, _ = run(["codoki-ack", "5"], gh_fail=True)
    check("gh failure in read -> nonzero, loud stderr",
          rc != 0 and ("codoki" in err.lower() or "gh" in err.lower()))

    print("== POST mode: construction guarantee ==")
    # Post +1: resolves the latest summary id and POSTs to its reactions endpoint.
    rc, out, err, posted = run(["codoki-ack", "5", "--react", "+1"],
                               issue=issue_arr(SUMMARY_MARKED))
    j = " ".join(posted)
    check("post +1 -> POST issues/comments/9001/reactions with content=+1",
          rc == 0 and "-X POST" in j and "issues/comments/9001/reactions" in j and "content=+1" in j)
    check("post construction guarantee: never targets /merge", "/merge" not in j)

    # Latest-summary selection: the NEWER marked summary (9001) wins over the older (8000).
    rc, out, err, posted = run(["codoki-ack", "5", "--react", "+1"],
                               issue=issue_arr(SUMMARY_OLD, SUMMARY_MARKED))
    j = " ".join(posted)
    check("post targets the LATEST summary id (9001, not 8000)",
          rc == 0 and "issues/comments/9001/reactions" in j and "comments/8000/" not in j)

    # Post -1 posts the down-reaction.
    rc, out, err, posted = run(["codoki-ack", "5", "--react", "-1"],
                               issue=issue_arr(SUMMARY_MARKED))
    j = " ".join(posted)
    check("post -1 -> content=-1 on the reactions endpoint",
          rc == 0 and "content=-1" in j and "issues/comments/9001/reactions" in j)

    # Post when NO summary exists -> LOUD nonzero, no POST (cannot ack a nonexistent summary).
    rc, out, err, posted = run(["codoki-ack", "5", "--react", "+1"], issue="[]")
    check("post with no summary -> nonzero + no POST (loud, not silent skip)",
          rc != 0 and posted == [])

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):")
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
