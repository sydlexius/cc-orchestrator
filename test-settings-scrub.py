#!/usr/bin/env python3
"""Harness for scripts/settings-scrub.py (#393). Stdlib only, subprocess-driven.

Every run points ORCHESTRATE_SETTINGS_FILES (or ORCHESTRATE_PROJECT_DIR + HOME) at temp fixtures,
so the real ~/.claude cascade is never read. Every secret is SYNTHETIC, generated per run by the
`secrets` module - no real credential is ever in the repo. Carrier shapes mirror the census in
DESIGN-settings-credential-scrub.md (env prefixes 10 of 14, Authorization Bearer 2, ldapsearch -w 2)
plus the two census FAILURES of #325's matcher: quoted values, and a second NAME= on one line.
"""
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SCRUB = os.path.join(ROOT, "scripts", "settings-scrub.py")
SETUP = os.path.join(ROOT, "scripts", "orchestrate-setup.py")
TMP = tempfile.mkdtemp(prefix="scrub-harness-")
FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + ("" if cond else f"  -- {detail}"))
    if not cond:
        FAILS.append(name)


def tok(n=32):
    # Re-draw the rare random value that happens to contain a placeholder word ("test", "mock"...),
    # which would make a synthetic secret legitimately unflaggable and the suite flaky.
    while True:
        v = secrets.token_urlsafe(n)[:n]
        if not any(w in v.lower() for w in ("test", "dummy", "fake", "example", "placeholder",
                                             "changeme", "change-me", "redacted", "sample", "mock",
                                             "xxxx", "your-", "your_")):
            return v


def write(name, obj, raw=None):
    p = os.path.join(TMP, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(raw if raw is not None else json.dumps(obj).encode())
    return p


def rules(*allow, deny=(), ask=()):
    return {"permissions": {"allow": list(allow), "deny": list(deny), "ask": list(ask)}}


def run(files, *args, script=SCRUB, extra_env=None, timeout=60):
    env = dict(os.environ, HOME=TMP, ORCHESTRATE_SETTINGS_FILES=":".join(files))
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, script, *args], capture_output=True, text=True, env=env,
                       cwd=TMP, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def sha12(v):
    return hashlib.sha256(v.encode()).hexdigest()[:12]


# ---- 1. each carrier: caught, labeled, value never printed, hash correct ----------------------
print("carriers (census shapes, synthetic values)")
S = {k: tok() for k in ("env", "dq", "sq", "second", "envw", "amp", "bearer", "basic", "xkey", "ldap",
                         "sshpass", "mysql", "redis", "curl", "ltok", "lapi", "lpw", "url", "deny", "ask")}
GHP = "ghp_" + secrets.token_hex(18)
CASES = [
    ("env", f"Bash(ANTHROPIC_API_KEY={S['env']} claude -p hi)", "env-assignment"),
    ("dq", f'Bash(GH_TOKEN="{S["dq"]}" gh api user)', "env-assignment"),
    ("sq", f"Bash(PGPASSWORD='{S['sq']}' psql -h db)", "env-assignment"),
    ("second", f"Bash(GITHUB_REPOSITORY=o/r SW_ENCRYPTION_KEY={S['second']} ./run.sh)", "env-assignment"),
    ("envw", f"Bash(env NPM_TOKEN={S['envw']} npm publish)", "env-assignment"),
    ("amp", f"Bash(cd x && DB_PASS={S['amp']} make migrate)", "env-assignment"),
    ("bearer", f'Bash(curl -H "Authorization: Bearer {S["bearer"]}" https://api.x/y)', "auth-header"),
    ("basic", f"Bash(curl -H 'Authorization: Basic {S['basic']}' https://api.x/y)", "auth-header"),
    ("xkey", f'Bash(curl -H "X-Api-Key: {S["xkey"]}" https://api.x/y)', "auth-header"),
    ("ldap", f"Bash(ldapsearch -x -H ldap://h -D cn=admin -w {S['ldap']} -b dc=x)", "short-flag"),
    ("sshpass", f"Bash(sshpass -p '{S['sshpass']}' ssh host)", "short-flag"),
    ("mysql", f"Bash(mysql -u root -p{S['mysql']} db)", "short-flag"),
    ("redis", f"Bash(redis-cli -h h -a {S['redis']} ping)", "short-flag"),
    ("curl", f"Bash(curl -u admin:{S['curl']} https://h/)", "short-flag"),
    ("ltok", f"Bash(tool login --token={S['ltok']})", "long-flag"),
    ("lapi", f"Bash(tool --api-key {S['lapi']} list)", "long-flag"),
    ("lpw", f'Bash(tool --db-password="{S["lpw"]}")', "long-flag"),
    ("url", f"Bash(git clone https://bot:{S['url']}@example.com/r.git)", "url-credential"),
    ("ghp", f"Bash(FOO={GHP} ./x.sh)", "known-token-prefix"),
]
S["ghp"] = GHP
S["slash"] = "/" + tok(20) + "/" + tok(19)  # standard-base64 shape that starts with "/"
S["glob"] = tok()
CASES += [("slash", f"Bash(AWS_SECRET_ACCESS_KEY={S['slash']} aws s3 ls)", "env-assignment"),
          ("glob", f"Bash(API_KEY={S['glob']}*)", "env-assignment")]  # hash excludes the trailing glob


def ent(v):
    return -sum(c / len(v) * math.log2(c / len(v)) for c in (v.count(x) for x in set(v)))


WORDS = "Snowy Dagger Plaza Harbor Violet Copper Mango Tundra Falcon Quartz Ember Nimbus".split()


def midtok(n):  # 2-class value with entropy in [3.25, 3.75): exercises the second threshold arm
    while True:
        v = "".join(secrets.choice("abcdef012345") for _ in range(n))
        if 3.25 <= ent(v) < 3.75 and re.search("[a-f]", v) and re.search("[0-9]", v):
            return v


for k in ("dockere", "envq", "buildarg", "literal", "redisurl", "curlatt", "mysqlq", "query",
          "urltok", "hdrend", "legacy", "proxy"):
    S[k] = tok()
S.update(sqdollar="$" + tok(), sklive="sk_live_" + secrets.token_hex(16), skant="sk-ant-" + tok(40),
         mid14=midtok(14), mid20=midtok(20), hf="hf_" + secrets.token_hex(17), aiza="AIza" + tok(35),
         pw=next(v for v in iter(lambda: "-".join(secrets.choice(WORDS) for _ in range(3)) + "7", 0) if ent(v) >= 3.25))
CASES += [  # review round 1: S4/S7 carrier shapes, S8 wrapper-free hashing, S9 threshold boundaries
    ("dockere", f'Bash(docker run -e "API_KEY={S["dockere"]}" img)', "env-assignment"),
    ("envq", f"Bash(docker run --env=API_KEY={S['envq']} img)", "env-assignment"),
    ("buildarg", f"Bash(docker build --build-arg=NPM_TOKEN={S['buildarg']} .)", "env-assignment"),
    ("literal", f"Bash(kubectl create secret generic x --from-literal=password={S['literal']})", "env-assignment"),
    ("sqdollar", f"Bash(DB_PASSWORD='{S['sqdollar']}' psql)", "env-assignment"),
    ("query", f'Bash(curl "https://api.h/v1?token={S["query"]}")', "env-assignment"),
    ("redisurl", f"Bash(redis-cli -u redis://:{S['redisurl']}@h:6379 ping)", "url-credential"),
    ("urltok", f"Bash(git clone https://{S['urltok']}@github.com/o/r.git)", "url-credential"),
    ("curlatt", f"Bash(curl -uadmin:{S['curlatt']} https://h/)", "short-flag"),
    ("mysqlq", f"Bash(mysql -u root -p'{S['mysqlq']}' db)", "short-flag"),
    ("sklive", f"Bash(x {S['sklive']})", "known-token-prefix"),
    ("skant", f"Bash(FOO={S['skant']} ./x)", "known-token-prefix"),
    ("hdrend", f"Bash(http GET https://h Authorization:Bearer {S['hdrend']})", "auth-header"),
    ("legacy", f"Bash(npm publish --token {S['legacy']}:*)", "long-flag"),
    ("proxy", f'Bash(curl -H "Proxy-Authorization: Basic {S["proxy"]}" h)', "auth-header"),
    ("mid14", f"Bash(API_KEY={S['mid14']} x)", "env-assignment"),
    ("mid20", f"Bash(API_KEY={S['mid20']} x)", "env-assignment"),
    ("hf", f"Bash(x {S['hf']})", "known-token-prefix"), ("aiza", f"Bash(x {S['aiza']})", "known-token-prefix"),
    ("pw", f"Bash(mysql -u root -p'{S['pw']}' db)", "short-flag"),  # S2-1: passphrase on a password carrier
]
for key, rule, carrier in CASES:
    f = write(f"c-{key}.json", rules(rule))
    rc, out, err = run([f])
    v = S[key]
    check(f"{key}: exit 1", rc == 1, f"rc={rc} out={out[-300:]} err={err}")
    check(f"{key}: carrier={carrier}", f"carrier={carrier}" in out, out[-400:])
    check(f"{key}: sha256 prefix", f"sha256={sha12(v)}" in out)
    check(f"{key}: value NEVER printed", v not in out and v not in err)
check("proxy: labeled Proxy-Authorization", "label=Proxy-Authorization" in run([os.path.join(TMP, "c-proxy.json")])[1])
f = write("c-denyask.json", rules(deny=[f"Bash(API_TOKEN={S['deny']} x)"], ask=[f"Bash(X_SECRET={S['ask']} y)"]))
rc, out, _ = run([f])
check("deny and ask lists scanned", rc == 1 and "rule=deny[0]" in out and "rule=ask[0]" in out, out)
# PR #487 (CR 4109973997): userinfo cannot contain '?' or '#', so a user:pass@ shape inside a
# query string or fragment is NOT a url-credential.
for sep in ("?x=abc", "#frag"):
    qv = tok()
    f = write(f"c-urlq{abs(hash(sep))}.json", rules(f"Bash(curl https://h{sep}:{qv}@y)"))
    rc, out, _ = run([f])
    check(f"url in {sep[0]}: no url-credential taken from the query/fragment",
          "carrier=url-credential" not in out, out[-300:])

# ---- 2. realistic benign corpus: a clean machine reports clean ---------------------------------
print("clean corpus")
CLEAN = [
    "Bash(git status)", "Bash(gh pr view *)", "Read(//tmp/**)", "WebFetch(domain:github.com)",
    "Bash(GITHUB_REPOSITORY=o/r bash gh-comment.sh 12 body)",
    "Bash(GIT_SHA=" + secrets.token_hex(20) + " ./x.sh)",  # a SHA under a non-credential name
    "Bash(./scripts/reply-comment.sh 138 3440336301 \"Fixed in e616f3b: a read failure now aborts\")",
    'Bash(git commit -m "rename API_KEY=old to API_KEY=new2")', 'Bash(git commit -m "add --token support")',
    "Bash(GH_TOKEN=$GH_TOKEN gh api user)", "Bash(TOKEN=${OP_TOKEN} x)", 'Bash(API_KEY="$(op read op://v/i/f)" x)',
    "Bash(OP_TOKEN=op://Private/GitHub/token x)", "Bash(op read op://Private/GitHub/Section/field)",
    "Bash(op read op://*)", 'Bash(git commit -m "support op:// references")', "Bash(API_KEY=test-key-1234567890abcdef x)", "Bash(TOKEN=your-token-here x)",
    "Bash(PASSWORD=changeme123456 x)", "Bash(SECRET=<redacted-value-here> x)", "Bash(API_KEY=xxxxxxxxxxxxxxxx x)",
    "Bash(SSH_KEY_PATH=/home/user/.ssh/id_ed25519 ssh h)", "Bash(AUTH_TOKEN=abc x)",
    'Bash(curl -H "Authorization: Bearer $TOKEN" https://x)', "Bash(docker login --password-stdin)",
    "Bash(ldapsearch -W -b dc=x)", "Bash(git clone https://example.com/r.git)",
    # the DOCUMENTED miss: a low-entropy human passphrase is not caught by construction
    "Bash(DB_PASSWORD=correcthorsebattery x)",
    # review round 1 (S5/S6/S9): valid quoted op refs, prose, slugs, URLs, relative paths, ARNs
    'Bash(op read "op://Private/GitHub Token/credential")',
    'Bash(op read "op://Private/deploy key/private key?ssh-format=openssh")',
    "Bash(op read op://Private/deploy-key/private-key?ssh-format=openssh)",
    'Bash(./scripts/reply-comment.sh 1 2 "the Authorization: Bearer header-construction-path is gone")',
    'Bash(./scripts/reply-comment.sh 1 2 "the --password argument-parsing rejects empty")',
    'Bash(git commit -m "docs: explain --api-key precedence-ordering")',
    'Bash(git commit -m "fix: SESSION_KEY=derived-from-environment is not cached")',
    'Bash(git commit -m "fix: SESSION_KEY=derived-from-environment, not cached")',  # S2-5
    'Bash(gh issue create --body "never pass --token plaintext-on-argv")',
    "Bash(AUTH_URL=https://auth.mycompany.io/oauth2/callback ./run)",
    "Bash(TOKEN_ENDPOINT=https://login.microsoftonline.com/common/oauth2/v2.0/token ./x)",
    "Bash(AWS_SECRET_NAME=prod/payments/db-credentials aws x)", "Bash(API_KEY_FILE=secrets/stripe_api.key ./run)",
    "Bash(PRIVATE_KEY_PATH=deploy/keys/id_ed25519 ./x)", "Bash(CACHE_KEY=node-modules-linux-x64-v18 ./x)",
    "Bash(SESSION_NAME=orchestrate-main-window tmux)", "Bash(KEY=value-of-some-mixedCase-Identifier ./x)",
    "Bash(TF_VAR_db_password_secret_arn=arn:aws:secretsmanager:us-east-1 x)", "Bash(git log --format=%H)",
    "Bash(API_KEY=******** x)", "Bash(API_KEY=aaaaaaaaaaaaaaaa x)",
]
fc = write("clean.json", rules(*CLEAN))
absent = os.path.join(TMP, "does-not-exist.json")
rc, out, err = run([fc, absent])
check("clean corpus exits 0", rc == 0, out + err)
check("clean wording is NOT a proof-of-absence claim",
      "no findings from the configured detectors" in out and "NOT proof of absence" in out, out)
check("COVERAGE names the low-entropy miss", "low-entropy real password" in out)
check("COVERAGE names the probabilistic short-value miss (S10)", "PROBABILISTICALLY" in out)
check("absent file is listed, not an error", f"absent   {absent}" in out)
check("no permissions block is clean", run([write("noperm.json", {"model": "x"})])[0] == 0)

# ---- 3. malformed op:// references (#349: op run fails OPEN on them) ---------------------------
print("op:// references")
for i, ref in enumerate(["op://vault/item", "op://vault//field", "op://v/i/s/f/extra", "op://vault"]):
    rc, out, _ = run([write(f"op{i}.json", rules(f"Bash(op run --env X={ref} -- cmd)"))])
    check(f"malformed {ref} flagged", rc == 1 and "carrier=malformed-op-ref" in out and ref not in out, out)

# ---- 4. could-not-determine: loud, exit 2, never clean ------------------------------------------
print("fault isolation")
BAD = {"badjson": b"{not json", "nondict": b"[1,2]", "permstr": b'{"permissions": "x"}',
       "allowdict": b'{"permissions": {"allow": {}}}', "nonstr": b'{"permissions": {"allow": [{"a": 1}]}}',
       "utf8": b'{"permissions": {"allow": ["\xff\xfe"]}}', "deep": b'{"a":' + b"[" * 200000 + b"]" * 200000 + b"}",
       "bigint": b'{"a": ' + b"9" * 5000 + b"}"}  # RecursionError / int-digit ValueError (S3)
for name, raw in BAD.items():
    p = write(f"bad-{name}.json", None, raw)
    rc, out, err = run([p])
    check(f"{name}: exit 2", rc == 2, f"rc={rc}")
    check(f"{name}: loud SKIPPED warning", "SKIPPED" in err and p in err, err)
d = os.path.join(TMP, "adir.json"); os.makedirs(d, exist_ok=True)
check("unreadable (directory) path: exit 2", run([d])[0] == 2)
dang = os.path.join(TMP, "dangling.json"); os.symlink(os.path.join(TMP, "nowhere.json"), dang)
check("dangling symlink is NOT absent: exit 2", run([dang])[0] == 2)
fs = write("mixed-secret.json", rules(f"Bash(API_KEY={S['env']} x)"))
rc, out, _ = run([fs, os.path.join(TMP, "bad-badjson.json")])
check("mixed finding + malformed sibling: exit 2 OUTRANKS 1", rc == 2, f"rc={rc}")
check("mixed: the finding is still listed in full", f"sha256={sha12(S['env'])}" in out, out)
sur = tok()
rc, out, err = run([write("surrogate.json", None, ('{"permissions": {"allow": ["Bash(API_KEY=%s\\ud800 x)"]}}' % sur).encode())])
check("lone surrogate: hashed, exit 1, no traceback (S3)", rc == 1 and "Traceback" not in err, err[-300:])
check("ENOTDIR (a file used as a dir) is absent, exit 0", run([fs + "/child"])[0] == 0)
GUARD = """import importlib.util, os, sys
s = importlib.util.spec_from_file_location("m", sys.argv[1]); m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
def boom(*a): raise PermissionError(13, "denied")
class P:
    lstat = staticmethod(boom)
    def __getattr__(self, n): return getattr(os, n)
if sys.argv[2] == "lstat": m.os = P()
else: m._load = lambda p: 1 / 0
sys.exit(m._guarded([]))
"""
for mode, want in (("lstat", "cannot stat (PermissionError)"), ("crash", "internal error (ZeroDivisionError)")):
    p = subprocess.run([sys.executable, "-c", GUARD, SCRUB, mode], capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, HOME=TMP, ORCHESTRATE_SETTINGS_FILES=fs))
    check(f"{mode}: exit 2 (could not determine), not absent/findings", p.returncode == 2, f"rc={p.returncode}")
    check(f"{mode}: says '{want}', no traceback, no value", want in p.stdout + p.stderr
          and "Traceback" not in p.stderr and S["env"] not in p.stdout + p.stderr, p.stdout[-200:] + p.stderr[-300:])

# ---- 5. dedup by resolved identity ---------------------------------------------------------------
print("dedup")
link = os.path.join(TMP, "link.json")
os.symlink(fs, link)
rc, out, _ = run([fs, fs, link, "", fs.replace(TMP, TMP + "/./")])
check("repeat + symlink + second spelling: ONE finding", out.count("FINDING  file=") == 1, out)
check("empty override scans nothing, exit 0", run([])[0] == 0)

# ---- 6. parity with orchestrate-setup.py _cascade_files() (raw discovery, dups included) --------
print("cascade parity")
PARITY = """import importlib.util, json, sys
def load(p, n):
    s = importlib.util.spec_from_file_location(n, p); m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m); return m
print(json.dumps([load(sys.argv[1], "a")._cascade_files(), load(sys.argv[2], "b")._cascade_files()]))
"""
nogit = os.path.join(TMP, "nogit"); os.makedirs(nogit, exist_ok=True)
proj = os.path.join(TMP, "proj")
for label, env in (("override w/ dups+empties", {"ORCHESTRATE_SETTINGS_FILES": "a::b:a:"}),
                   ("empty override", {"ORCHESTRATE_SETTINGS_FILES": ""}),
                   ("project dir", {"ORCHESTRATE_PROJECT_DIR": proj}),
                   ("project == HOME (dup)", {"ORCHESTRATE_PROJECT_DIR": TMP}),
                   ("no project, not a git dir", {})):
    e = {k: v for k, v in os.environ.items() if k not in ("ORCHESTRATE_SETTINGS_FILES", "ORCHESTRATE_PROJECT_DIR")}
    e.update(env, HOME=TMP, GIT_CEILING_DIRECTORIES=TMP)
    p = subprocess.run([sys.executable, "-c", PARITY, SETUP, SCRUB], capture_output=True, text=True,
                       env=e, cwd=nogit, timeout=60)
    a, b = json.loads(p.stdout) if p.returncode == 0 else (None, 1)
    check(f"parity: {label}", a == b and a is not None, f"setup={a} scrub={b} err={p.stderr[-300:]}")

# ---- 7. detail file: 0600 from birth, symlink refused --------------------------------------------
print("detail file")
det = os.path.join(TMP, "detail.json")
rc, out, _ = run([fs], "--detail-file", det)
check("detail: exit 1 and created", rc == 1 and os.path.isfile(det))
check("detail: mode 0600", stat.S_IMODE(os.stat(det).st_mode) == 0o600, oct(os.stat(det).st_mode))
check("detail: carries the unredacted value", [r.get("value") for r in json.load(open(det))] == [S["env"]])
check("detail: stdout still never prints it", S["env"] not in out)
det_before = open(det, "rb").read()
rc, _, err = run([fs], "--detail-file", det)
check("detail: an existing path is refused (exit 2), bytes untouched (S1)", rc == 2 and open(det, "rb").read() == det_before, err)
fs_before = open(fs, "rb").read()
rc, _, _ = run([fs], "--detail-file", fs)
check("detail: pointed at the scanned settings file -> exit 2, byte-identical", rc == 2 and open(fs, "rb").read() == fs_before)
det2 = os.path.join(TMP, "detail-umask.json")
subprocess.run([sys.executable, SCRUB, "--detail-file", det2], env=dict(os.environ, HOME=TMP, ORCHESTRATE_SETTINGS_FILES=fs),
               capture_output=True, timeout=60, preexec_fn=lambda: os.umask(0o277))
check("detail: 0600 even under a 0277 umask (fchmod on the fd)", stat.S_IMODE(os.stat(det2).st_mode) == 0o600)
fifo = os.path.join(TMP, "fifo"); os.mkfifo(fifo)
try:
    rc = run([fs], "--detail-file", fifo, timeout=10)[0]
except subprocess.TimeoutExpired:
    rc = "HANG"
check("detail: a FIFO path is refused, never hangs", rc == 2, f"rc={rc}")
for sp in (os.path.join(TMP, "absent-cascade.json"), os.path.join(TMP, "settings.local.json")):  # S2-2
    rc = run([fs, os.path.join(TMP, "absent-cascade.json")], "--detail-file", sp)[0]
    check(f"detail: a settings path ({os.path.basename(sp)}) is refused, never created", rc == 2 and not os.path.lexists(sp))
p = subprocess.run([sys.executable, SCRUB], capture_output=True, timeout=60, env=dict(  # S2-3: the real entry point
    os.environ, HOME=TMP, PYTHONIOENCODING="utf-8:strict", ORCHESTRATE_SETTINGS_FILES=TMP.encode() + b"/caf\xe9.json"))
check("entry point is _guarded: internal error -> exit 2, no traceback", p.returncode == 2 and
      b"internal error (UnicodeEncodeError)" in p.stderr and b"Traceback" not in p.stderr, p.stderr[-300:])
# PR #487 (Copilot 4109974410): a FIFO in the CASCADE is skipped (exit 2), never a hang.
cfifo = os.path.join(TMP, "cascade-fifo.json"); os.mkfifo(cfifo)
try:
    rc, _, err = run([fs, cfifo], timeout=10)
except subprocess.TimeoutExpired:
    rc, err = "HANG", ""
check("cascade: a FIFO settings path is SKIPPED (exit 2), never hangs",
      rc == 2 and "SKIPPED" in err and "not a regular file" in err, f"rc={rc}")
tgt = write("victim.txt", None, b"keep")
dl = os.path.join(TMP, "detail-link.json"); os.symlink(tgt, dl)
rc, _, err = run([fs], "--detail-file", dl)
check("detail: symlinked path refused (exit 2), target untouched", rc == 2 and open(tgt).read() == "keep", err)

# ---- 8. report-only: no actuation path, files byte-identical ------------------------------------
print("report-only")
before = open(fs, "rb").read()
for args in ([], ["--yes", fs], ["--redact"], ["--detail-file"],
             ["--detail-file", os.path.join(TMP, "d1"), "--detail-file", os.path.join(TMP, "d2")]):
    rc, _, _ = run([fs], *args)
    if args:
        check(f"{' '.join(args)}: usage error exit 2", rc == 2, f"rc={rc}")
check("scanned settings file byte-identical afterward", open(fs, "rb").read() == before)
check("--help exits 0", run([], "--help")[0] == 0)

# ---- 9. MUTATION PROOF: with the matcher disabled the fixture secret is no longer caught ----------
print("mutation proof")
mut = os.path.join(TMP, "scrub-mutant.py")
src = open(SCRUB).read()
anchor = "    found = []\n    for m in _ENV.finditer(text):"
open(mut, "w").write(src.replace(anchor, "    return []\n" + anchor, 1))
check("mutation landed (matcher disabled)", open(mut).read() != src)
rc, out, _ = run([fs], script=mut)
check("mutant MISSES the fixture secret (exit 0) - the suite depends on the matcher", rc == 0, f"rc={rc}")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'PASS' if not FAILS else 'FAIL'}: {len(FAILS)} failure(s)" + (f": {FAILS}" if FAILS else ""))
sys.exit(1 if FAILS else 0)
