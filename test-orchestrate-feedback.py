#!/usr/bin/env python3
"""Proof harness for orchestrate-feedback.sh (#149): the maildir feedback store.

Every run uses an isolated temp MAILDIR (via $ORCHESTRATE_FEEDBACK_DIR) and a
fixed $GITHUB_REPOSITORY, so it is host-independent and never touches the real
~/.claude store. Covers add / drain / list, input validation, the drained/
cold-storage move, and the headline property: CONCURRENT adds from multiple
"team leads" never clobber each other (one-file-per-entry + atomic rename).

Run: python3 test-orchestrate-feedback.py
"""
import os
import subprocess
import sys
import tempfile
import concurrent.futures

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "orchestrate-feedback.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


def run(maildir, args, *, stdin=None, repo="sydlexius/cc-orchestrator"):
    env = dict(os.environ)
    env["ORCHESTRATE_FEEDBACK_DIR"] = maildir
    env["GITHUB_REPOSITORY"] = repo
    p = subprocess.run(["bash", SCRIPT] + args, env=env, input=stdin,
                       capture_output=True, text=True, timeout=90)
    return p.returncode, p.stdout, p.stderr


def inbox_files(maildir):
    d = os.path.join(maildir, "inbox")
    return sorted(f for f in os.listdir(d) if f.endswith(".md")) if os.path.isdir(d) else []


def drained_files(maildir):
    d = os.path.join(maildir, "drained")
    return sorted(f for f in os.listdir(d) if f.endswith(".md")) if os.path.isdir(d) else []


def main():
    with tempfile.TemporaryDirectory() as td:
        md = os.path.join(td, "store")

        print("== add: creates an inbox entry, prints the filename, writes the body ==")
        rc, out, err = run(md, ["add", "my-slug", "the body text"])
        fname = out.strip()
        check("add -> exit 0", rc == 0)
        check("add prints a .md filename", fname.endswith(".md"))
        check("add filename carries the repo + slug", "sydlexius-cc-orchestrator" in fname and "my-slug" in fname)
        check("entry lands in inbox/", fname in inbox_files(md))
        body = open(os.path.join(md, "inbox", fname)).read()
        check("entry body written", "the body text" in body)
        check("README created on first use", os.path.isfile(os.path.join(md, "README.md")))
        readme = open(os.path.join(md, "README.md")).read()
        check("README has the cold-storage stop-sign", "DO NOT read" in readme and "gh issue list" in readme)

        print("== add: body via stdin when no positional body ==")
        rc, out, err = run(md, ["add", "stdin-slug"], stdin="piped body\nline2")
        f2 = out.strip()
        check("stdin add -> exit 0", rc == 0)
        check("stdin body written", "piped body" in open(os.path.join(md, "inbox", f2)).read())

        print("== add: bad-char slug is sanitized (no slash/space in filename) ==")
        rc, out, err = run(md, ["add", "bad slug/with:chars", "b"])
        f3 = out.strip()
        check("bad-slug add -> exit 0", rc == 0)
        check("no slash in filename", "/" not in f3)
        check("no space in filename", " " not in f3)

        print("== add: empty body refused ==")
        rc, out, err = run(md, ["add", "x"], stdin="")
        check("empty-body add -> non-zero", rc != 0)

        print("== add: missing slug refused ==")
        rc, out, err = run(md, ["add"])
        check("no-slug add -> non-zero", rc != 0)

        print("== list: inbox entries, sorted, never drained/ ==")
        rc, out, err = run(md, ["list"])
        listed = [ln for ln in out.splitlines() if ln.strip()]
        check("list -> exit 0", rc == 0)
        check("list shows all 3 inbox entries", len(listed) == 3)
        check("list is sorted", listed == sorted(listed))

        print("== drain: appends breadcrumb + moves to drained/, leaves inbox ==")
        rc, out, err = run(md, ["drain", fname, "--issue", "149", "--verdict", "VERIFIED maildir"])
        check("drain -> exit 0", rc == 0)
        check("entry left inbox/", fname not in inbox_files(md))
        check("entry now in drained/", fname in drained_files(md))
        drained_body = open(os.path.join(md, "drained", fname)).read()
        check("drained entry has the breadcrumb", "DRAINED -> #149 [VERIFIED maildir]" in drained_body)
        check("drained entry retains original body", "the body text" in drained_body)

        print("== list never includes drained entries ==")
        rc, out, err = run(md, ["list"])
        check("drained entry not in list", fname not in out)

        print("== drain validation ==")
        rc, out, err = run(md, ["drain", f2, "--issue", "notanumber", "--verdict", "x"])
        check("non-numeric --issue -> non-zero", rc != 0)
        rc, out, err = run(md, ["drain", f2, "--issue", "5"])
        check("missing --verdict -> non-zero", rc != 0)
        rc, out, err = run(md, ["drain", "../escape.md", "--issue", "5", "--verdict", "x"])
        check("path-traversal entry -> non-zero", rc != 0)
        # Symlink hardening: a planted symlink in inbox/ must be refused (else drain
        # would cat an arbitrary target into drained/).
        secret = os.path.join(td, "secret.txt")
        open(secret, "w").write("SECRET")
        link = os.path.join(md, "inbox", "evil.md")
        os.symlink(secret, link)
        rc, out, err = run(md, ["drain", "evil.md", "--issue", "5", "--verdict", "x"])
        check("symlink entry -> non-zero (rejected)", rc != 0)
        check("symlink entry NOT moved to drained/", "evil.md" not in drained_files(md))
        os.unlink(link)
        rc, out, err = run(md, ["drain", "does-not-exist.md", "--issue", "5", "--verdict", "x"])
        check("missing entry -> non-zero", rc != 0)
        rc, out, err = run(md, ["drain", fname, "--issue", "149", "--verdict", "x"])
        check("re-drain (already drained) -> non-zero", rc != 0)

        print("== unknown subcommand refused ==")
        rc, out, err = run(md, ["frobnicate"])
        check("unknown subcommand -> non-zero", rc != 0)

    print("== CONCURRENCY: N parallel adds (multiple TLs) never clobber ==")
    with tempfile.TemporaryDirectory() as td:
        md = os.path.join(td, "store")
        N = 25

        def one_add(i):
            return run(md, ["add", f"concurrent-{i}", f"body {i}"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
            results = list(ex.map(one_add, range(N)))
        rcs_ok = all(rc == 0 for rc, _o, _e in results)
        names = sorted(o.strip() for _rc, o, _e in results)
        check(f"all {N} concurrent adds exit 0", rcs_ok)
        check(f"all {N} produced DISTINCT filenames (no collision)", len(set(names)) == N)
        check(f"inbox holds exactly {N} entries (no clobber)", len(inbox_files(md)) == N)

    print("== CONCURRENCY: N parallel adds of the SAME slug never clobber (atomic ln) ==")
    with tempfile.TemporaryDirectory() as td:
        md = os.path.join(td, "store")
        N = 25

        def same_slug_add(i):
            # Identical slug + body: the only thing keeping the filenames apart is
            # the atomic ln-claim + random suffix. This is the real clobber risk.
            return run(md, ["add", "samey", "identical body"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
            results = list(ex.map(same_slug_add, range(N)))
        check(f"all {N} same-slug adds exit 0", all(rc == 0 for rc, _o, _e in results))
        check(f"same-slug: inbox holds exactly {N} entries (atomic claim, no clobber)",
              len(inbox_files(md)) == N)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
