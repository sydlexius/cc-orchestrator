#!/usr/bin/env python3
"""Proof harness for pr-read-comments.sh (#144 optional [owner/repo] positional).

Host-independent: gh is a temp Python stub first on PATH (applying any --jq / -q
via the real jq); never a real PR. The stub also appends every invocation to a
call-log so a test can assert WHICH repo the script targeted (the provided slug
vs the gh-repo-view fallback).

#144: the script documents `<pr> [owner/repo] [comment-id...]` but historically
derived the repo from `gh repo view` unconditionally and fed every trailing
positional to `jq -R 'tonumber'`, so `pr-read-comments.sh 123 owner/repo` crashed
with a jq numeric-literal error. The fix disambiguates by pattern: an argument
containing '/' is a repo slug; otherwise the trailing positionals are numeric
comment IDs (legacy form preserved).

Run: python3 test-pr-read-comments.py
"""
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "pr-read-comments.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


GH_STUB = r'''#!/usr/bin/env python3
import os, sys, subprocess
args = sys.argv[1:]
ME = os.environ.get("ME", "testuser")
INLINE = os.environ.get("INLINE_JSON", "[]")
REVIEWS = os.environ.get("REVIEWS_JSON", "[]")
ISSUE = os.environ.get("ISSUE_JSON", "[]")
REPO_VIEW = os.environ.get("REPO_VIEW", "fallback-owner/fallback-repo")
CALLLOG = os.environ.get("GH_CALLLOG", "")

if CALLLOG:
    with open(CALLLOG, "a") as f:
        f.write(" ".join(args) + "\n")

def emit(data):
    # Honor both --jq <expr> and -q <expr> (gh repo view uses -q).
    flag = "--jq" if "--jq" in args else ("-q" if "-q" in args else None)
    if flag:
        expr = args[args.index(flag) + 1]
        p = subprocess.run(["jq", "-r", expr], input=data, capture_output=True, text=True)
        sys.stdout.write(p.stdout)
    else:
        sys.stdout.write(data)
    sys.exit(0)

if args[:2] == ["repo", "view"]:
    emit('{"nameWithOwner":"%s"}' % REPO_VIEW)
if args[:2] == ["api", "user"]:
    emit('{"login":"%s"}' % ME)

endpoint = ""
for a in args:
    if "repos/" in a:
        endpoint = a; break
if endpoint.endswith("/reviews"):
    emit(REVIEWS)
if endpoint.endswith("/comments") and "/pulls/" in endpoint:
    emit(INLINE)
if endpoint.endswith("/comments") and "/issues/" in endpoint:
    emit(ISSUE)
emit("[]")
'''


def run(args, *, inline="[]", reviews="[]", issue="[]", me="testuser",
        repo_view="fallback-owner/fallback-repo"):
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(GH_STUB)
        os.chmod(gh, 0o755)
        calllog = os.path.join(td, "calls.log")
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["ME"] = me
        env["INLINE_JSON"] = inline
        env["REVIEWS_JSON"] = reviews
        env["ISSUE_JSON"] = issue
        env["REPO_VIEW"] = repo_view
        env["GH_CALLLOG"] = calllog
        p = subprocess.run(["bash", SCRIPT] + args, env=env,
                           capture_output=True, text=True, timeout=20)
        calls = ""
        try:
            with open(calllog) as f:
                calls = f.read()
        except FileNotFoundError:
            pass
        return p.returncode, p.stdout, p.stderr, calls


INLINE_ONE = ('[{"id":456789,"user":{"login":"coderabbitai[bot]"},'
              '"in_reply_to_id":null,"path":"a.go","original_line":10,"body":"finding"}]')
REVIEW_BODY = ('[{"id":555,"user":{"login":"coderabbitai[bot]"},"state":"COMMENTED",'
               '"body":"Potential issue: something is wrong"}]')
ISSUE_BODY = ('[{"id":777,"user":{"login":"coderabbitai[bot]"},'
              '"body":"a general conversation comment"}]')


def main():
    print("== #144 disambiguation: <pr> <owner/repo> is a slug, not a comment ID ==")
    rc, out, err, calls = run(["123", "owner/repo"], inline=INLINE_ONE)
    check("2-arg <pr> <owner/repo>: exit 0 (the slug-as-id bug is fixed)", rc == 0)
    check("2-arg form: no jq numeric-literal error in stderr",
          "tonumber" not in err and "number" not in err.lower())
    check("2-arg form targets the PROVIDED repo", "repos/owner/repo/pulls/123/comments" in calls)
    check("2-arg form does NOT call gh repo view (slug provided)", "repo view" not in calls)

    print("== 1-arg form falls back to gh repo view ==")
    rc, out, err, calls = run(["123"], inline=INLINE_ONE)
    check("1-arg form: exit 0", rc == 0)
    check("1-arg form falls back to gh repo view", "repo view" in calls)
    check("1-arg form targets the derived repo",
          "repos/fallback-owner/fallback-repo/pulls/123/comments" in calls)

    print("== numeric trailing positional is still a comment ID (legacy form) ==")
    rc, out, err, calls = run(["123", "456789"], inline=INLINE_ONE)
    check("1-arg + numeric ID: exit 0", rc == 0)
    check("numeric ID -> repo still derived via gh repo view", "repo view" in calls)
    check("numeric ID fast path prints the matching comment", "ID:   456789" in out)

    print("== 2-arg + comment ID: repo=slug, filter by ID ==")
    rc, out, err, calls = run(["123", "owner/repo", "456789"], inline=INLINE_ONE)
    check("2-arg + ID: exit 0", rc == 0)
    check("2-arg + ID targets the provided repo", "repos/owner/repo/pulls/123/comments" in calls)
    check("2-arg + ID does NOT call gh repo view", "repo view" not in calls)
    check("2-arg + ID prints the matching comment", "ID:   456789" in out)

    print("== missing PR -> usage error ==")
    rc, out, err, calls = run([])
    check("no args -> nonzero exit", rc != 0)
    check("no args -> usage message", "Usage" in out or "Usage" in err)

    print("== mode flags compose with the repo positional ==")
    rc, out, err, calls = run(["--reviews", "123", "owner/repo"], reviews=REVIEW_BODY)
    check("--reviews + slug: exit 0", rc == 0)
    check("--reviews targets the provided repo", "repos/owner/repo/pulls/123/reviews" in calls)
    rc, out, err, calls = run(["--issue", "123", "owner/repo"], issue=ISSUE_BODY)
    check("--issue + slug: exit 0", rc == 0)
    check("--issue targets the provided repo", "repos/owner/repo/issues/123/comments" in calls)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
