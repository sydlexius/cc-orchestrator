#!/usr/bin/env python3
"""Black-box harness for the P3-F gh-* wrappers (issue #24). Stdlib-only, no pytest.
Stubs `gh` via PATH (records its args, exits 0) so no network/auth is needed; asserts the
refuse-vs-passthrough behavior AND the construction guarantee (no wrapper can call a merge
endpoint). Wrappers are invoked via `bash <wrapper>` so +x is not required to test."""
import os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def run_wrapper(wrapper, args, extra_env=None, grep_shim=False):
    """Run a wrapper with a stubbed `gh` on PATH. Returns (rc, gh_argv_list).

    The stub logs its argv LOSSLESSLY with a NUL separator, so the returned `invoked`
    is the exact LIST of args `gh` was called with (no `$*` flattening that would mangle
    args containing spaces). An empty list means `gh` was never called.

    grep_shim=True ALSO prepends a `grep` that ALWAYS exits 0 (matches everything),
    proving the wrappers' whole-string validators do NOT depend on grep behavior: under
    GNU grep on the dev box `grep -Ezq '^...$'` rejects a newline-bearing value, but BSD
    grep on the deployment target MATCHES it - so any validator that still shelled out to
    grep would REGRESS the newline/traversal bypass. A pure-bash `case` guard is immune."""
    grepstub = None
    d = tempfile.mkdtemp()
    ghstub = os.path.join(d, "gh")
    log = os.path.join(d, "ghlog")
    with open(ghstub, "w") as f:
        f.write('#!/usr/bin/env bash\nprintf "%s\\0" "$@" >> "$GH_LOG"\nexit 0\n')
    os.chmod(ghstub, 0o755)
    if grep_shim:
        grepstub = os.path.join(d, "grep")
        with open(grepstub, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(grepstub, 0o755)
    env = dict(os.environ)
    env["PATH"] = d + os.pathsep + env["PATH"]
    env["GH_LOG"] = log
    if extra_env:
        env.update(extra_env)
    try:
        p = subprocess.run(["bash", os.path.join(HERE, "scripts", wrapper), *args],
                           env=env, capture_output=True, text=True, timeout=15)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        # Treat a hang as a failure of this case, not a harness crash.
        rc = 124
    if os.path.exists(log):
        with open(log, "rb") as f:
            raw = f.read()
        invoked = [tok.decode() for tok in raw.split(b"\0") if tok]
    else:
        invoked = []
    try:
        os.remove(ghstub)
        if grepstub:
            os.remove(grepstub)
        if os.path.exists(log):
            os.remove(log)
        os.rmdir(d)
    except OSError:
        pass
    return rc, invoked


def main():
    # --- gh-api-get.sh: refuse every mutation flag (incl. attached / =value forms), no gh call ---
    for bad in (["-X", "PUT", "repos/o/r/pulls/1/merge"], ["-X", "GET", "x"], ["-f", "a=b", "x"],
                ["-F", "a=b", "x"], ["--method", "PATCH", "x"], ["--method=PATCH", "x"],
                ["--field", "a=b", "x"], ["--raw-field", "a=b", "x"], ["--input", "body.json", "x"],
                ["-XPUT", "x"], ["-fa=b", "x"], ["--field=a=b", "x"]):
        rc, invoked = run_wrapper("gh-api-get.sh", bad)
        check(f"gh-api-get refuses {bad[0]!r} (rc2, no gh call)", rc == 2 and invoked == [])
    # passthrough GETs (merge-STATUS read is allowed; only mutation flags are refused)
    rc, invoked = run_wrapper("gh-api-get.sh", ["repos/o/r/pulls/1/merge"])
    check("gh-api-get passes a GET through to `gh api`", rc == 0 and invoked == ["api", "repos/o/r/pulls/1/merge"])
    rc, invoked = run_wrapper("gh-api-get.sh", ["repos/o/r/x?state=open", "-i"])
    check("gh-api-get passes a GET with -i + query-string path", rc == 0 and invoked == ["api", "repos/o/r/x?state=open", "-i"])

    # --- gh-codeql-dismiss.sh: numeric validation + construction guarantee + reason enum ---
    env = {"GITHUB_REPOSITORY": "o/r"}
    for bad in (["abc"], ["5/../pulls/1/merge"], ["1 2"], [""], ["5x"], ["-7"]):
        rc, invoked = run_wrapper("gh-codeql-dismiss.sh", bad, env)
        check(f"gh-codeql-dismiss refuses non-numeric alert {bad!r} (rc2, no gh call)", rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-codeql-dismiss.sh", ["7"], env)
    check("gh-codeql-dismiss valid -> PATCH the code-scanning alerts endpoint",
          rc == 0 and "api -X PATCH repos/o/r/code-scanning/alerts/7" in " ".join(invoked))
    check("gh-codeql-dismiss endpoint is alerts-only (construction guarantee: no /merge)", "/merge" not in " ".join(invoked))
    rc, invoked = run_wrapper("gh-codeql-dismiss.sh", ["7", "bogus reason"], env)
    check("gh-codeql-dismiss refuses a non-enum reason (rc2)", rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-codeql-dismiss.sh", ["7", "false positive"], env)
    check("gh-codeql-dismiss accepts an enum reason", rc == 0 and "dismissed_reason=false positive" in " ".join(invoked))
    # no repo resolvable -> refuse (stub gh repo view returns empty)
    rc, invoked = run_wrapper("gh-codeql-dismiss.sh", ["7"], {"GITHUB_REPOSITORY": ""})
    check("gh-codeql-dismiss refuses when no repo resolvable", rc == 2)
    # HOSTILE: a newline-bearing numeric must be REJECTED whole (a line-oriented validator
    # would let the benign first line '7' pass, then interpolate the smuggled second line).
    rc, invoked = run_wrapper("gh-codeql-dismiss.sh", ["7\n../../pulls/1/merge"], env)
    check("gh-codeql-dismiss rejects newline-bearing alert (rc2, no gh call)", rc == 2 and invoked == [])

    # --- gh-resolve-thread.sh: node-id validation + fixed mutation, never a merge ---
    for bad in (["bad id"], ["a;b"], [""], ["x/../y"], ["a$b"]):
        rc, invoked = run_wrapper("gh-resolve-thread.sh", bad)
        check(f"gh-resolve-thread refuses bad id {bad!r} (rc2, no gh call)", rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-resolve-thread.sh", ["PRRT_kwDO123-_="])
    check("gh-resolve-thread valid -> fixed resolveReviewThread graphql mutation",
          rc == 0 and "graphql" in " ".join(invoked) and "resolveReviewThread" in " ".join(invoked))
    check("gh-resolve-thread cannot merge (construction guarantee: no pulls/merge)",
          "/merge" not in " ".join(invoked) and "pulls" not in " ".join(invoked))

    # --- gh-comment.sh: subcommand routing, numeric validation, body-as-data guarantee ---
    env = {"GITHUB_REPOSITORY": "o/r"}
    # no/unknown subcommand
    rc, invoked = run_wrapper("gh-comment.sh", [], env)
    check("gh-comment refuses no subcommand (rc2, no gh call)", rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-comment.sh", ["bogus", "1"], env)
    check("gh-comment refuses unknown subcommand (rc2, no gh call)", rc == 2 and invoked == [])
    # post
    for bad in (["post", "abc", "hi"], ["post", "1/../pulls/1/merge", "hi"], ["post", "1"]):
        rc, invoked = run_wrapper("gh-comment.sh", bad, env)
        check(f"gh-comment post refuses {bad!r} (rc2, no gh call)", rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-comment.sh", ["post", "12", "hello world"], env)
    check("gh-comment post valid -> POST issues/<pr>/comments",
          rc == 0 and "api -X POST repos/o/r/issues/12/comments" in " ".join(invoked) and "body=hello world" in " ".join(invoked))
    check("gh-comment post construction guarantee (no /merge)", "/merge" not in " ".join(invoked))
    # body containing a merge/method string is DATA: it rides in `body=...` but the endpoint
    # + method stay the fixed POST issues/<pr>/comments. Verify the structure (the body text
    # naturally appears in the stub log via -f body=, but never as a path or a -X verb token).
    rc, invoked = run_wrapper("gh-comment.sh", ["post", "12", "pulls/1/merge -X PUT --admin"], env)
    check("gh-comment post body 'pulls/1/merge -X PUT --admin' is data only (endpoint+method fixed)",
          rc == 0 and " ".join(invoked).startswith("api -X POST repos/o/r/issues/12/comments ")
          and "body=pulls/1/merge -X PUT --admin" in " ".join(invoked))  # body rides as data, not the path/verb
    # HOSTILE: a newline-bearing pr must be REJECTED whole (line-oriented validator bypass).
    rc, invoked = run_wrapper("gh-comment.sh", ["post", "12\nzzz", "hi"], env)
    check("gh-comment post rejects newline-bearing pr (rc2, no gh call)", rc == 2 and invoked == [])
    # reply
    for bad in (["reply", "abc", "5", "hi"], ["reply", "1", "abc", "hi"], ["reply", "1", "5"]):
        rc, invoked = run_wrapper("gh-comment.sh", bad, env)
        check(f"gh-comment reply refuses {bad!r} (rc2, no gh call)", rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-comment.sh", ["reply", "12", "987", "thanks"], env)
    check("gh-comment reply valid -> POST pulls/<pr>/comments/<id>/replies",
          rc == 0 and "api -X POST repos/o/r/pulls/12/comments/987/replies" in " ".join(invoked))
    check("gh-comment reply construction guarantee (no /merge)", "/merge" not in " ".join(invoked))
    # HOSTILE: a newline-bearing comment-id must be REJECTED whole (line-oriented bypass).
    rc, invoked = run_wrapper("gh-comment.sh", ["reply", "12", "5\n../../../repos/x/y/issues/1", "hi"], env)
    check("gh-comment reply rejects newline-bearing comment-id (rc2, no gh call)", rc == 2 and invoked == [])
    # trigger-cr REMOVED (#192): the dead CR-trigger subcommand is now an unknown subcommand
    # (exclusive-purview rule -- no agent-accessible CR trigger). It must make no gh call.
    rc, invoked = run_wrapper("gh-comment.sh", ["trigger-cr", "12"], env)
    check("gh-comment trigger-cr removed -> rejected, no gh call", rc != 0 and invoked == [])
    # inline (needs a HEAD sha; run inside this repo so git rev-parse HEAD works)
    for bad in (["inline", "abc", "--file", "a.py", "--line", "3", "hi"],
                ["inline", "12", "--line", "3", "hi"],                       # no --file
                ["inline", "12", "--file", "a.py", "--line", "abc", "hi"],   # bad line
                ["inline", "12", "--file", "a.py", "--line", "3", "--side", "UP", "hi"],  # bad side
                ["inline", "12", "--file", "a.py", "--line", "3"],           # no body
                ["inline", "12", "--file", "a.py", "--line", "3", "-X", "PUT", "hi"]):    # injected flag
        rc, invoked = run_wrapper("gh-comment.sh", bad, env)
        check(f"gh-comment inline refuses {bad!r} (rc2, no gh call)", rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-comment.sh", ["inline", "12", "--file", "src/a.py", "--line", "42", "looks off"], env)
    check("gh-comment inline valid -> POST pulls/<pr>/comments with body+commit_id+path+line+side",
          rc == 0 and "api -X POST repos/o/r/pulls/12/comments" in " ".join(invoked)
          and "body=looks off" in " ".join(invoked) and "commit_id=" in " ".join(invoked)
          and "path=src/a.py" in " ".join(invoked) and "line=42" in " ".join(invoked) and "side=RIGHT" in " ".join(invoked))
    check("gh-comment inline construction guarantee (no /merge, no --admin)",
          "/merge" not in " ".join(invoked) and "--admin" not in " ".join(invoked))
    rc, invoked = run_wrapper("gh-comment.sh", ["inline", "12", "--file", "a.py", "--line", "5", "--side", "LEFT", "x"], env)
    check("gh-comment inline honors --side LEFT", rc == 0 and "side=LEFT" in " ".join(invoked))
    # #100: harden inline arg parsing - a missing operand exits via die (rc2, clean) NOT a bare
    # `set -e` shift failure (which would be rc1), and extra/duplicate body tokens are rejected
    # rather than silently overwriting the body. rc==2 proves the clean die path specifically.
    for bad in (["inline", "12", "--line", "3", "--file"],                            # --file missing value
                ["inline", "12", "--file", "a.py", "--line"],                         # --line missing value
                ["inline", "12", "--file", "a.py", "--line", "3", "--side"],          # --side missing value
                ["inline", "12", "--file", "a.py", "--line", "3", "b1", "b2"],        # duplicate positional body
                ["inline", "12", "--file", "a.py", "--line", "3", "--", "b1", "b2"],  # extra token after -- body
                ["inline", "12", "--file", "a.py", "--line", "3", "body", "--", "x"]):  # body then -- duplicate
        rc, invoked = run_wrapper("gh-comment.sh", bad, env)
        check(f"#100: gh-comment inline rejects malformed {bad!r} via die (rc2, no gh call)",
              rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-comment.sh", ["inline", "12", "--file", "a.py", "--line", "7", "--", "body via dashdash"], env)
    check("#100: inline `-- <body>` posts the single body correctly",
          rc == 0 and "body=body via dashdash" in " ".join(invoked) and "line=7" in " ".join(invoked))

    # --- gh-codeql-autofix.sh: numeric validation + construction guarantee ---
    env = {"GITHUB_REPOSITORY": "o/r"}
    for bad in (["abc"], ["5/../pulls/1/merge"], ["1 2"], [""], ["5x"], ["-7"]):
        rc, invoked = run_wrapper("gh-codeql-autofix.sh", bad, env)
        check(f"gh-codeql-autofix refuses non-numeric alert {bad!r} (rc2, no gh call)", rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-codeql-autofix.sh", ["7"], env)
    check("gh-codeql-autofix valid -> POST code-scanning alerts autofix endpoint",
          rc == 0 and "api -X POST repos/o/r/code-scanning/alerts/7/autofix" in " ".join(invoked))
    check("gh-codeql-autofix endpoint is alerts-only (construction guarantee: no /merge)", "/merge" not in " ".join(invoked))
    # repo as positional arg
    rc, invoked = run_wrapper("gh-codeql-autofix.sh", ["7", "owner2/repo2"], {"GITHUB_REPOSITORY": ""})
    check("gh-codeql-autofix accepts [repo] positional arg",
          rc == 0 and "repos/owner2/repo2/code-scanning/alerts/7/autofix" in " ".join(invoked))
    # HOSTILE: a newline-bearing alert must be REJECTED whole (line-oriented bypass).
    rc, invoked = run_wrapper("gh-codeql-autofix.sh", ["7\n../../pulls/1/merge"], env)
    check("gh-codeql-autofix rejects newline-bearing alert (rc2, no gh call)", rc == 2 and invoked == [])
    # HOSTILE: a traversal repo arg must be REJECTED before interpolation.
    rc, invoked = run_wrapper("gh-codeql-autofix.sh", ["7", "o/r/../../../pulls/1"], {"GITHUB_REPOSITORY": ""})
    check("gh-codeql-autofix rejects traversal repo arg (rc2, no gh call)", rc == 2 and invoked == [])

    # --- gh-delete-branch.sh: charset validation + url-encode + construction guarantee ---
    env = {"GITHUB_REPOSITORY": "o/r"}
    for bad in (["bad branch"], ["a;b"], [""], ["x/../y"], ["a$b"], ["-X"],
                ["pulls/1/merge\nx"], ["br`whoami`"]):
        rc, invoked = run_wrapper("gh-delete-branch.sh", bad, env)
        check(f"gh-delete-branch refuses bad name {bad!r} (rc2, no gh call)", rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-delete-branch.sh", ["feat/24-gh-api-wrappers"], env)
    check("gh-delete-branch valid -> DELETE git/refs/heads/<branch>",
          rc == 0 and "api -X DELETE repos/o/r/git/refs/heads/feat/24-gh-api-wrappers" in " ".join(invoked))
    check("gh-delete-branch construction guarantee (no /merge)", "/merge" not in " ".join(invoked))
    # a name that LOOKS like a merge path is still data within the validated charset -> heads/ prefix
    rc, invoked = run_wrapper("gh-delete-branch.sh", ["pulls/1/merge"], env)
    check("gh-delete-branch 'pulls/1/merge' stays under git/refs/heads/ (cannot target the merge endpoint)",
          rc == 0 and invoked == ["api", "-X", "DELETE", "repos/o/r/git/refs/heads/pulls/1/merge"])
    # repo as positional arg
    rc, invoked = run_wrapper("gh-delete-branch.sh", ["topic", "owner2/repo2"], {"GITHUB_REPOSITORY": ""})
    check("gh-delete-branch accepts [repo] positional arg",
          rc == 0 and "repos/owner2/repo2/git/refs/heads/topic" in " ".join(invoked))
    # HOSTILE: a traversal repo arg must be REJECTED before interpolation.
    rc, invoked = run_wrapper("gh-delete-branch.sh", ["topic", "o/r/../../../pulls/1/merge"], {"GITHUB_REPOSITORY": ""})
    check("gh-delete-branch rejects traversal repo arg (rc2, no gh call)", rc == 2 and invoked == [])

    # --- PORTABILITY PROOF: re-run the key hostile inputs with an ALWAYS-TRUE `grep` shim
    # first on PATH. If any validator still shelled out to grep for its whole-string check,
    # the shim (which matches everything) would let the smuggled value through and produce a
    # gh call. A pure-bash `case` guard ignores the shim entirely, so these must STILL be
    # rc==2 with no gh call. This is the BSD-grep regression proof the inherited-PATH cases
    # (GNU grep on the dev box) cannot give. ---
    env = {"GITHUB_REPOSITORY": "o/r"}
    rc, invoked = run_wrapper("gh-codeql-autofix.sh", ["7\n../../pulls/1/merge"], env, grep_shim=True)
    check("PORTABLE: gh-codeql-autofix rejects newline alert under always-true grep (rc2, no gh call)",
          rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-codeql-dismiss.sh", ["7\n../../pulls/1/merge"], env, grep_shim=True)
    check("PORTABLE: gh-codeql-dismiss rejects newline alert under always-true grep (rc2, no gh call)",
          rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-comment.sh", ["post", "12\nzzz", "hi"], env, grep_shim=True)
    check("PORTABLE: gh-comment rejects newline pr under always-true grep (rc2, no gh call)",
          rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-comment.sh", ["reply", "12", "5\n../../../repos/x/y/issues/1", "hi"],
                              env, grep_shim=True)
    check("PORTABLE: gh-comment rejects newline comment-id under always-true grep (rc2, no gh call)",
          rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-codeql-autofix.sh", ["7", "o/r/../../../pulls/1"],
                              {"GITHUB_REPOSITORY": ""}, grep_shim=True)
    check("PORTABLE: gh-codeql-autofix rejects traversal repo under always-true grep (rc2, no gh call)",
          rc == 2 and invoked == [])
    rc, invoked = run_wrapper("gh-delete-branch.sh", ["topic", "o/r/../../../pulls/1/merge"],
                              {"GITHUB_REPOSITORY": ""}, grep_shim=True)
    check("PORTABLE: gh-delete-branch rejects traversal repo under always-true grep (rc2, no gh call)",
          rc == 2 and invoked == [])
    # node id with newline must also be rejected portably
    rc, invoked = run_wrapper("gh-resolve-thread.sh", ["PRRT_ok\n../../pulls/1/merge"], grep_shim=True)
    check("PORTABLE: gh-resolve-thread rejects newline node id under always-true grep (rc2, no gh call)",
          rc == 2 and invoked == [])

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("All gh-wrapper harness checks passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
