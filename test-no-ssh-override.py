#!/usr/bin/env python3
"""No plugin helper or command block may override git's ssh command (issue #568).

CC Safety Net (rule `git.ssh-env`) DENIES any network git call that runs under a
GIT_SSH_COMMAND / GIT_SSH / GIT_SSH_VARIANT / core.sshCommand override, and the analyzer checks
every command line statically, so a helper that sets one makes every command block running it
denied before anything executes (the gate does not run). Re-spelling the override does not help
(`-c core.sshCommand=`, inline `GIT_SSH_COMMAND=`, `GIT_CONFIG_KEY_n=core.sshCommand` are all
denied). Non-interactive behavior comes from GIT_TERMINAL_PROMPT=0 + a forced failing SSH_ASKPASS.

WHAT IS SCANNED (non-comment lines only; prose and `#` comment lines are allowed):
  - scripts/*.sh and scripts/*.py
  - inside fenced bash/sh/shell/zsh blocks only: commands/*.md and skills/orchestrate/templates/*.md
test-*.py (which stub and assert on these names) and design docs are not scanned.

WHAT COUNTS AS A VIOLATION: any occurrence of GIT_SSH_COMMAND, GIT_SSH, GIT_SSH_VARIANT or
`sshCommand` (core.sshCommand, GIT_CONFIG_KEY_<n>=core.sshCommand, `-c core.sshCommand=...`),
or a GIT_CONFIG_VALUE_<n> carrying an `ssh ...` command, that is not a pure READ. A read is
stripped before the test: `$NAME` / `${NAME...}` expansion, `unset NAME`,
Python `.get("NAME"`, `.pop("NAME"`, `"NAME" in`, and a `["NAME"]` subscript not followed by
an assignment (plain, augmented or annotated; `==` is a comparison, so a read). `env -u NAME`
is NOT a read: Safety Net denies it too. So `[ -n "${GIT_SSH_COMMAND:-}" ]` is fine; `GIT_SSH_COMMAND=x`, `export
GIT_SSH_COMMAND`, `env["GIT_SSH_COMMAND"] = x`, `setdefault("GIT_SSH_COMMAND", x)` and
`{"GIT_SSH_COMMAND": x}` are not. A trailing comment after code is treated as code (doubt is a
violation).

Usage: python3 test-no-ssh-override.py [repo-root]
Stdlib only, no network, read-only. Exit 0 clean / 1 violation or self-test failure / 2 cannot check (no scripts/, or a sanity floor
tripped with no violation: the scan cannot be trusted).
"""
import glob
import os
import re
import sys

NAMES = r"GIT_SSH_COMMAND|GIT_SSH_VARIANT|GIT_SSH"
READ_PATTERNS = [
    re.compile(r"\$\{?(?:%s)\b[^}\s]*\}?" % NAMES),                       # $NAME / ${NAME:-x}
    re.compile(r"\bunset\s+(?:-\w+\s+)*(?:%s)\b" % NAMES),               # unset NAME (env -u is DENIED)
    re.compile(r"\.(?:get|pop)\(\s*[\"'](?:%s)[\"']" % NAMES),            # .get("NAME" / .pop("NAME"
    re.compile(r"[\"'](?:%s)[\"']\s+(?:not\s+)?in\b" % NAMES),            # "NAME" in env
    re.compile(r"\[\s*[\"'](?:%s)[\"']\s*\](?!\s*(?::[^=\n]*)?(?:[-+*/%%&|^@]|//|\*\*|<<|>>)?=(?!=))" % NAMES),  # env["NAME"] read
]
BANNED = [
    re.compile(r"\b(?:%s)\b" % NAMES),
    re.compile(r"sshCommand", re.I),
    # GIT_CONFIG_VALUE_<n> carrying an ssh command: shell `=`, Python `env["..."] =` / dict `"...":`,
    # or the key as the last token on the line (value on the next line of a multi-line literal).
    re.compile(r"GIT_CONFIG_VALUE_\d+[\"']?\s*\]?\s*[=:]\s*(?:[\"']?ssh\b|$)"),
]
# CommonMark fences: a run of >= 3 backticks or tildes opens; only a run of the SAME char, at least
# as long, with nothing after it closes (so a shorter fence inside a longer one is content).
FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)([^\n]*)$")
FENCE_CLOSE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
SHELL_LANGS = {"bash", "sh", "shell", "zsh"}
MIN_SH, MIN_PY, MIN_MD, MIN_BLOCKS = 30, 5, 10, 100  # parse-sanity floors


def violations(text, fenced_md=False):
    """Return [(lineno, line)] of violating non-comment lines. fenced_md: only inside shell fences.
    Also returns the number of shell blocks seen (md mode) via the second tuple element."""
    hits, blocks = [], 0
    in_block, in_fence = False, False
    fch, flen = "", 0
    for n, line in enumerate(text.splitlines(), 1):
        if fenced_md:
            if in_fence:
                c = FENCE_CLOSE.match(line)
                if c and c.group(1)[0] == fch and len(c.group(1)) >= flen:
                    in_fence = in_block = False
                    continue
            else:
                m = FENCE_OPEN.match(line)
                # a backtick fence's info string may not contain a backtick (else it is inline code)
                if m and not (m.group(1)[0] == "`" and "`" in m.group(3)):
                    in_fence, fch, flen = True, m.group(1)[0], len(m.group(1))
                    in_block = m.group(2).lower() in SHELL_LANGS
                    blocks += in_block
                    continue
            if not in_block:
                continue
        if line.lstrip().startswith("#"):
            continue
        probe = line
        for rp in READ_PATTERNS:
            probe = rp.sub(" ", probe)
        if any(bp.search(probe) for bp in BANNED):
            hits.append((n, line.strip()))
    return hits, blocks


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def scan(root):
    files = {"sh": [], "py": [], "md": []}
    for p in sorted(glob.glob(os.path.join(root, "scripts", "*.sh"))):
        files["sh"].append(p)
    for p in sorted(glob.glob(os.path.join(root, "scripts", "*.py"))):
        if not os.path.basename(p).startswith("test-"):
            files["py"].append(p)
    for pat in (("commands", "*.md"), ("skills", "orchestrate", "templates", "*.md")):
        files["md"] += sorted(glob.glob(os.path.join(root, *pat)))
    out, blocks = [], 0
    for kind, paths in files.items():
        for p in paths:
            h, b = violations(read(p), fenced_md=(kind == "md"))
            blocks += b
            out += [(os.path.relpath(p, root), n, ln) for n, ln in h]
    return files, blocks, out


# Every banned spelling must be caught; every allowed read/comment must not be.
MUTANTS = [
    ("sh", 'GIT_SSH_COMMAND="ssh -o BatchMode=yes"'),
    ("sh", 'export GIT_SSH_COMMAND'),
    ("sh", 'GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh} -o BatchMode=yes" git fetch'),
    ("sh", 'GIT_SSH=ssh git fetch origin'),
    ("sh", 'GIT_SSH_VARIANT=ssh git fetch origin'),
    ("sh", 'git -c core.sshCommand="ssh -i k" fetch origin'),
    ("sh", 'git config core.sshCommand "ssh -i k"'),
    ("sh", 'export GIT_CONFIG_KEY_0=core.sshCommand'),
    ("sh", 'export GIT_CONFIG_VALUE_0="ssh -o BatchMode=yes"'),
    ("sh", 'env -u GIT_SSH_COMMAND git fetch origin'),
    ("py", 'env["GIT_SSH_COMMAND"] = "ssh -oBatchMode=yes"'),
    ("py", 'env.setdefault("GIT_SSH_COMMAND", "ssh")'),
    ("py", 'subprocess.run(cmd, env={"GIT_SSH_COMMAND": "ssh"})'),
    ("py", '["git", "-c", "core.sshCommand=ssh", "fetch"]'),
    ("py", 'env["GIT_SSH"] = "ssh"'),
    ("py", 'os.environ["GIT_SSH_COMMAND"] += " -oBatchMode=yes"'),
    ("py", 'env["GIT_SSH_COMMAND"]: str = "ssh"'),
    ("md", '```bash\nGIT_SSH_COMMAND="ssh -o BatchMode=yes" git ls-remote origin\n```'),
    ("md", '```sh\ngit -c core.sshCommand=ssh fetch\n```'),
    # #568 review: Python mapping assignment of the GIT_CONFIG_* injection spelling
    ("py", 'env["GIT_CONFIG_VALUE_0"] = "ssh -o BatchMode=yes"'),
    ("py", 'env["GIT_CONFIG_KEY_0"] = "core.sshCommand"'),
    ("py", 'env.update({"GIT_CONFIG_VALUE_0": "ssh -o BatchMode=yes"})'),
    # multi-line literals: key on its own line, value on the next, call spanning lines
    ("py", 'env.update({\n    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",\n})'),
    ("py", 'env.update({\n    "GIT_CONFIG_VALUE_0":\n        "ssh -o BatchMode=yes",\n})'),
    ("py", 'subprocess.run(cmd, env=dict(\n    os.environ,\n    GIT_SSH_COMMAND="ssh"))'),
    # #568 review: fence handling (tilde, 4-backtick, shorter inner fence must not close)
    ("md", '~~~bash\nGIT_SSH_COMMAND="ssh -o BatchMode=yes" git fetch\n~~~'),
    ("md", '````bash\nGIT_SSH_COMMAND="ssh -o BatchMode=yes" git fetch\n````'),
    ("md", '````bash\n```\nGIT_SSH_COMMAND=ssh git fetch\n````'),
    ("md", '```bash\ngit fetch\n``` trailing\nGIT_SSH_COMMAND=ssh git fetch\n```'),
    ("md", '~~~~sh\n~~~\ngit -c core.sshCommand=ssh fetch\n~~~~'),
]
ALLOWED = [
    ("sh", '[ -n "${GIT_SSH_COMMAND:-}" ] && echo custom-ssh'),
    ("sh", 'echo "ssh is $GIT_SSH_COMMAND"'),
    ("sh", 'unset GIT_SSH_COMMAND'),
    ("sh", '# GIT_SSH_COMMAND="ssh -o BatchMode=yes" is denied by CC Safety Net'),
    ("sh", 'export SSH_ASKPASS_REQUIRE=force'),
    ("py", 'ssh = env.get("GIT_SSH_COMMAND")'),
    ("py", 'env.pop("GIT_SSH_COMMAND", None)'),
    ("py", 'if "GIT_SSH_COMMAND" in os.environ:'),
    ("py", 'x = env["GIT_SSH_COMMAND"]'),
    ("py", 'if env["GIT_SSH_COMMAND"] == "ssh":'),
    ("py", '# env["GIT_SSH_COMMAND"] = "ssh"'),
    ("py", 'env["SSH_ASKPASS_REQUIRE"] = "force"'),
    ("md", 'Prose may say GIT_SSH_COMMAND and core.sshCommand freely.\n```bash\ngit fetch origin\n```'),
    ("md", '```text\nGIT_SSH_COMMAND=x\n```'),
    ("md", '```bash\n# GIT_SSH_COMMAND=x is denied\ngit fetch origin\n```'),
    ("py", 'x = env["GIT_CONFIG_VALUE_0"]'),
    ("py", 'env.update({\n    "GIT_TERMINAL_PROMPT": "0",\n})'),
    ("md", '~~~text\nGIT_SSH_COMMAND=x\n~~~'),
    ("md", '````text\n```bash\nGIT_SSH_COMMAND=x\n```\n````\n```bash\ngit fetch\n```'),
    ("md", '```bash\ngit fetch\n```\nGIT_SSH_COMMAND=x in prose after the block'),
]


def selftest():
    bad = []
    for kind, text in MUTANTS:
        hits, _ = violations(text, fenced_md=(kind == "md"))
        if not hits:
            bad.append("NOT caught: %r" % text)
    for kind, text in ALLOWED:
        hits, _ = violations(text, fenced_md=(kind == "md"))
        if hits:
            bad.append("WRONGLY flagged: %r" % text)
    return bad


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    if not os.path.isdir(os.path.join(root, "scripts")):
        print("cannot check: no scripts/ under %s" % root, file=sys.stderr)
        return 2
    bad = selftest()
    for b in bad:
        print("[FAIL] self-test: " + b)
    files, blocks, hits = scan(root)
    for kind, floor in (("sh", MIN_SH), ("py", MIN_PY), ("md", MIN_MD)):
        if len(files[kind]) < floor:
            print("[FAIL] sanity floor: %d %s files (need >= %d)" % (len(files[kind]), kind, floor))
            bad.append("floor")
    if blocks < MIN_BLOCKS:
        print("[FAIL] sanity floor: %d fenced shell blocks (need >= %d)" % (blocks, MIN_BLOCKS))
        bad.append("floor")
    for rel, n, ln in hits:
        print("[FAIL] %s:%d overrides the ssh command (CC Safety Net git.ssh-env): %s" % (rel, n, ln[:120]))
    if hits or any(b != "floor" for b in bad):
        return 1
    if bad:
        return 2
    print("[PASS] %d sh, %d py, %d md files, %d shell blocks; %d mutants caught, %d allowed forms clean"
          % (len(files["sh"]), len(files["py"]), len(files["md"]), blocks, len(MUTANTS), len(ALLOWED)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
