#!/usr/bin/env python3
"""settings-scrub.py - REPORT-ONLY scan of the Claude Code settings cascade for credential-shaped
values stored inside permission rules (#393, design of record:
skills/orchestrate/design/DESIGN-settings-credential-scrub.md, #326).

"Always allow" stores the approved command line VERBATIM and forever, so a secret passed on a
command line becomes a plaintext secret on disk. This tool makes that backlog visible without
printing any of it. It NEVER writes a settings file: the consented redact actuation is a separate
follow-up (#326), and no --yes/redact path exists here, guarded or otherwise.

Usage: settings-scrub.py [--detail-file PATH] [-h|--help]

EXIT CONTRACT (the cr-quota-watch.sh shape; this ANSWERS A QUESTION, so it is never a blanket 0):
  0  every in-scope file scanned; no findings FROM THE CONFIGURED DETECTORS (not proof of absence)
  1  scanned successfully, findings present
  2  could not determine: a present file was unreadable/malformed/skipped, or a malformed
     invocation. 2 OUTRANKS 1: a partial scan must never read as the complete finding set.

NO OUTPUT PATH PRINTS A SECRET VALUE. Per finding: file, rule index, carrier, an identifier label
(a variable/flag/header NAME, never a value), value SHAPE (length, character classes, entropy
bucket) and a SHA-256 prefix, which correlates one rotation's blast radius across files. The
unredacted detail goes ONLY to --detail-file, created 0600 from birth.
"""
import hashlib
import json
import math
import os
import re
import subprocess
import sys

HOME = os.path.expanduser("~")


def _cascade_files():
    """REPLICATED from orchestrate-setup.py _cascade_files() (hyphenated, not importable); the
    harness asserts parity. Order: user settings, user .local, project settings, project .local.
    ORCHESTRATE_SETTINGS_FILES (colon-separated) REPLACES the cascade; empty entries dropped; an
    empty override scans nothing. Project root: ORCHESTRATE_PROJECT_DIR, else the git toplevel of
    CWD, else project files are skipped (never guessed from $PWD). No dedup here, by contract."""
    override = os.environ.get("ORCHESTRATE_SETTINGS_FILES")
    if override is not None:
        return [p for p in override.split(":") if p]
    files = [os.path.join(HOME, ".claude", "settings.json"),
             os.path.join(HOME, ".claude", "settings.local.json")]
    project = os.environ.get("ORCHESTRATE_PROJECT_DIR")
    if not project:
        try:
            r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=15)
            project = r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            project = None
    if project:
        files.append(os.path.join(project, ".claude", "settings.json"))
        files.append(os.path.join(project, ".claude", "settings.local.json"))
    return files


def _dedup(paths):
    """Scrubber-specific stable dedup by RESOLVED identity (first occurrence wins), so a repeat,
    a symlink, or a second spelling of one file never yields two findings for one secret."""
    seen, out = set(), []
    for p in paths:
        key = os.path.realpath(os.path.expanduser(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


# ---- carriers ---------------------------------------------------------------------------------
# A VALUE token: double-quoted, single-quoted, or a bare run up to whitespace/shell punctuation.
_VAL = r"""(?:"([^"]*)"|'([^']*)'|([^\s"'`;&|()<>]+))"""
_CRED_NAME = re.compile(  # a short word must be a whole _-component; a long one may be a suffix
    r"(?:^|_)(?:PW|PWD|PASS|AUTH|CRED|CREDS|PRIVATE|SESSION|COOKIE|BEARER|SIGNATURE)(?:$|_)|"
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIALS?)(?:$|_)", re.I)
# The prefix class admits a quote or "=" so `-e "K=V"`, `--env=K=V`, `--build-arg=K=V` and
# `--from-literal=password=V` match, and "?" so a first `?token=V` query parameter does (S4/S7).
_ENV = re.compile(r"(?:^|[\s;&|(`\"'=?])([A-Za-z_][A-Za-z0-9_]*)=" + _VAL)
_AUTH = re.compile(r"\b(Authorization|Proxy-Authorization)\s*:\s*(?:(?:Bearer|Basic|Token|token)\s+)?"
                   r"([^\s\"'`;&|()<>]+)", re.I)
_KEYHDR = re.compile(r"\b(X-Api-Key|Api-Key|X-Auth-Token|Private-Token|X-Access-Token)\s*:\s*"
                     r"([^\s\"'`;&|()<>]+)", re.I)
_SHORT = [  # tool-scoped: the flag only counts after the tool word in the same clause
    re.compile(r"\b(ldap\w*)\b[^;&|\n]*?\s-w\s*" + _VAL),
    re.compile(r"\b(sshpass)\b[^;&|\n]*?\s-p\s*" + _VAL),
    re.compile(r"\b(redis-cli)\b[^;&|\n]*?\s-a\s+" + _VAL),
    re.compile(r"\b(mysql\w*)\b[^;&|\n]*?\s-p" + _VAL),
    re.compile(r"\b(curl)\b[^;&|\n]*?\s(?:-u|--user)[\s=]*[\"']?[^\s:\"']+:([^\s\"'`;&|()@]+)"),
]
_LONG = re.compile(r"(?:^|\s)--((?:[a-z]+-)*(?:token|api-?key|password|passwd|secret|"
                   r"client-secret|private-key|access-key|secret-key|auth))(?:=|\s+)" + _VAL, re.I)
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://(?:[^:/@\s\"'`]*:)?([^:@\s/\"'`]+)@", re.I)
_PREFIX = re.compile(r"\b((?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                     r"sk-(?:ant-)?[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[A-Z0-9]{16}|"
                     r"glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{30,}|[sr]k_live_[A-Za-z0-9]{16,}|"
                     r"hf_[A-Za-z0-9]{30,}|AIza[A-Za-z0-9_-]{35})")
_OPREF = re.compile(r"\"(op://[^\"]*)\"|'(op://[^']*)'|(op://[^\s\"'`;&|()<>]+)")  # quoted refs WHOLE
_OP_OK = re.compile(r"op://[^/?]+/[^/?]+/[^/?]+(?:/[^/?]+)?"
                    r"(?:\?[A-Za-z_-]+=[A-Za-z0-9_-]+(?:&[A-Za-z_-]+=[A-Za-z0-9_-]+)*)?")
# S6: a value made only of word-like parts is prose, a slug, a relative path or an ARN, not a
# secret: "header-construction-path", "prod/payments/db-credentials", "node-modules-linux-x64-v18".
_WORD = re.compile(r"[A-Za-z][a-z]+(?:[A-Z][a-z]+)*\d{0,5}|[a-z]\d{0,5}|\d{1,4}")
_PLACEHOLDER_WORDS = ("test", "dummy", "fake", "example", "placeholder", "changeme", "change-me",
                      "redacted", "sample", "mock", "xxxx", "your-", "your_", "<", ">", "...")


def _first(m, start):
    """(value, literal): literal is True when the SINGLE-quoted alternative matched, where a
    leading "$" is a literal character rather than a shell reference (S7)."""
    for i, g in enumerate(m.groups()[start:]):
        if g is not None:
            return g, i == 1 and m.re.pattern.endswith(_VAL)
    return "", False


def _match_carriers(text):
    """Every (carrier, label, value) credential-carrier candidate in one rule. Labels are
    identifier-shaped (regex-constrained) so printing them can never print a value."""
    found = []
    for m in _ENV.finditer(text):
        if _CRED_NAME.search(m.group(1)):
            found.append(("env-assignment", m.group(1), *_first(m, 1)))
    for m in _AUTH.finditer(text):
        found.append(("auth-header", m.group(1), m.group(2), False))
    for m in _KEYHDR.finditer(text):
        found.append(("auth-header", m.group(1), m.group(2), False))
    for rx in _SHORT:
        for m in rx.finditer(text):
            found.append(("short-flag", m.group(1), *_first(m, 1)))
    for m in _LONG.finditer(text):
        found.append(("long-flag", "--" + m.group(1), *_first(m, 1)))
    for m in _URL.finditer(text):
        found.append(("url-credential", "url", m.group(1), False))
    for m in _PREFIX.finditer(text):
        found.append(("known-token-prefix", "token", m.group(1), False))
    return found


def _classes(v):
    c = []
    if re.search(r"[a-z]", v):
        c.append("lower")
    if re.search(r"[A-Z]", v):
        c.append("upper")
    if re.search(r"[0-9]", v):
        c.append("digit")
    if re.search(r"[^A-Za-z0-9]", v):
        c.append("symbol")
    return c


def _entropy(v):
    if not v:
        return 0.0
    n = len(v)
    return -sum((k / n) * math.log2(k / n) for k in (v.count(ch) for ch in set(v)))


def _bucket(h):
    return "low" if h < 2.5 else "medium" if h < 3.25 else "high" if h < 4.5 else "very-high"


def _is_placeholder(v, literal=False, prose_ok=True):
    s, low = v, v.lower()
    if not s or (not literal and (s.startswith("$") or "${" in s or "$(" in s)):
        return True  # empty, glob-only, or a shell reference: not a literal
    if _OP_OK.fullmatch(s):
        return True  # a well-formed secret REFERENCE is the safe pattern
    if any(w in low for w in _PLACEHOLDER_WORDS):
        return True
    if re.match(r"(?:~|\.{1,2})/|/(?:home|Users|tmp|var|etc|opt|usr|private|root|mnt|srv|Volumes|run)/", s):
        return True  # path-shaped (a bare leading "/" is NOT enough: standard base64 can start with one)
    parts = [p for p in re.split(r"[-_./:]+", re.sub(r"^[a-z][a-z0-9+.-]*://", "", s.rstrip(",.;!?"))) if p]
    return prose_ok and len(parts) >= 2 and all(_WORD.fullmatch(p) for p in parts)


def _is_secret(v, literal=False, prose_ok=True):
    """THE DISCRIMINATOR: flag iff NOT a placeholder (a shell reference, a well-formed op://
    reference, a stub word, an absolute path, or a value of >= 2 WORD-LIKE parts split on -_./: -
    prose, slugs, relative paths, ARNs, and URLs with no userinfo, their scheme stripped) AND
    len >= 12 AND Shannon entropy >= 3.75 bits/char, or >= 3.25 with >= 2 character classes.
    A word-like part is a lowercase or camelCase word with <= 5 trailing digits, one letter plus
    digits, or <= 4 digits, so a UUID or random token (mixed case/digits mid-part) never passes.
    The word-like exclusion is OFF (prose_ok=False) on password carriers (short flags, URL userinfo),
    where prose cannot occur. Precision-for-recall trade: a low-entropy password, a word-based
    passphrase (Word-Word-Word7) under an env/header/long-flag carrier, and probabilistically a
    short random value are MISSED by construction."""
    if _is_placeholder(v, literal, prose_ok) or len(v) < 12:
        return False
    h = _entropy(v)
    return h >= 3.75 or (h >= 3.25 and len(_classes(v)) >= 2)


def _malformed_op_refs(text):
    out = []
    for m in _OPREF.finditer(text):
        ref = next(g for g in m.groups() if g is not None)
        if "*" in ref or "$" in ref:
            continue  # a permission glob or a shell variable, not a literal reference
        if not _OP_OK.fullmatch(ref):
            out.append(("malformed-op-ref", "op", ref))
    return out


def scan_rule(text):
    seen, out = set(), []
    for carrier, label, value, literal in _match_carriers(text):
        value = re.sub(r":?\*+$", "", value)  # a trailing permission glob (`*` or legacy `:*`)
        if _is_secret(value, literal, carrier not in ("short-flag", "url-credential")) and value not in seen:
            seen.add(value)
            out.append((carrier, label, value))
    for c in _malformed_op_refs(text):
        if c[2] not in seen:
            seen.add(c[2])
            out.append(c)
    return out


def _load(path):
    """-> (rules, None) or (None, reason). Any doubt about the file is a reason, never 'clean'."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return None, f"unreadable ({type(e).__name__})"
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None, "not valid UTF-8"
    except json.JSONDecodeError as e:
        return None, f"unparseable JSON (line {e.lineno} col {e.colno})"
    except (ValueError, RecursionError) as e:  # >4300-digit int, pathological nesting (S3)
        return None, f"unparseable JSON ({type(e).__name__})"
    if not isinstance(data, dict):
        return None, f"top level is {type(data).__name__}, not an object"
    perms = data.get("permissions")
    if perms is None:
        return [], None
    if not isinstance(perms, dict):
        return None, "permissions is not an object"
    rules = []
    for key in ("allow", "deny", "ask"):
        lst = perms.get(key)
        if lst is None:
            continue
        if not isinstance(lst, list):
            return None, f"permissions.{key} is not a list"
        for i, r in enumerate(lst):
            if not isinstance(r, str):
                return None, f"permissions.{key}[{i}] is not a string"
            rules.append((f"{key}[{i}]", r))
    return rules, None


COVERAGE = """COVERAGE (what this scan CANNOT find - a clean result is NOT proof of absence):
  - a low-entropy real password (English words, short, or human-chosen) is missed by construction;
  - a secret under a variable name that is not credential-shaped, or behind a flag/tool spelling
    not in the carrier set (e.g. a JSON/form request body field), unless it carries a known prefix;
  - a short random value (under ~20 chars, especially hex) is missed PROBABILISTICALLY by the
    entropy bar; a value made only of word-like parts (prose, slugs, AND a word-based passphrase
    such as Word-Word-Word7 under PGPASSWORD= or --password) is treated as prose, and prose with
    acronym parts (values-via-CLI) can be a false positive;
  - anything outside permissions.allow/deny/ask (the env block, hooks, other files, transcripts);
  - a secret already rotated or copied elsewhere; this reads only the files listed above."""


def _write_detail(path, rows):
    """Unredacted detail, CREATE-ONLY and 0600 from birth (S1). O_EXCL refuses ANY existing path -
    a settings file, a FIFO (no hang), a symlink - so this tool can never clobber a file; fchmod
    pins the mode on the fd itself against a umask, never re-resolving the path."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rows, f, indent=2)
        f.write("\n")


def main(argv):
    detail = None
    args = list(argv)
    while args:
        a = args.pop(0)
        if a in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        if a == "--detail-file" and args and detail is None:
            detail = args.pop(0)
            continue
        print(f"settings-scrub: unknown or incomplete argument: {a} (report-only; no actuation "
              "path exists, see #326)", file=sys.stderr)
        return 2
    if detail is not None and (re.fullmatch(r"settings.*\.json", os.path.basename(detail)) or  # S2-2
                               os.path.realpath(detail) in {os.path.realpath(p) for p in _cascade_files()}):
        print(f"settings-scrub: refusing --detail-file {detail}: it names a settings file", file=sys.stderr)
        return 2
    findings, skipped, detail_rows, scanned, nrules = [], [], [], [], 0
    for path in _dedup(_cascade_files()):
        try:
            os.lstat(path)
            rules, reason = _load(path)
        except (FileNotFoundError, NotADirectoryError):
            print(f"absent   {path}")
            continue
        except OSError as e:  # EACCES on a parent is "cannot see", never "absent" (S2)
            rules, reason = None, f"cannot stat ({type(e).__name__})"
        if reason:
            skipped.append((path, reason))
            print(f"WARNING: settings-scrub: SKIPPED {path}: {reason} - NOT scanned, NOT clean",
                  file=sys.stderr)
            continue
        scanned.append(path)
        nrules += len(rules)
        for where, rule in rules:
            for carrier, label, value in scan_rule(rule):
                h = _entropy(value)
                digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]
                findings.append(f"FINDING  file={path} rule={where} carrier={carrier} label={label} "
                                f"len={len(value)} classes={','.join(_classes(value))} "
                                f"entropy={_bucket(h)} sha256={digest}")
                detail_rows.append({"file": path, "rule": where, "carrier": carrier, "label": label,
                                    "sha256": digest, "value": value, "rule_text": rule})
    for p in scanned:
        print(f"scanned  {p}")
    for p, r in skipped:
        print(f"SKIPPED  {p}: {r}")
    for f in findings:
        print(f)
    if detail is not None:
        try:
            _write_detail(detail, detail_rows)
            print(f"detail   unredacted findings written 0600 to {detail}")
        except OSError as e:
            print(f"settings-scrub: cannot create --detail-file {detail} ({type(e).__name__}); an "
                  "existing path is never overwritten - remove it or choose a new path", file=sys.stderr)
            return 2
    print(COVERAGE)
    print(f"summary: {len(scanned)} file(s) scanned, {nrules} rule(s), {len(findings)} finding(s), "
          f"{len(skipped)} skipped")
    if skipped:
        print("RESULT: COULD NOT DETERMINE - at least one present file was not scanned (exit 2)")
        return 2
    if findings:
        print("RESULT: FINDINGS PRESENT - rotate each credential, then remove it from the rule (exit 1)")
        return 1
    print("RESULT: no findings from the configured detectors (exit 0; see COVERAGE)")
    return 0


def _guarded(argv):
    """Any unexpected error is "could not determine" (exit 2), never exit 1's "scanned, findings
    present". The type name only: str(e) could quote a value (S3)."""
    try:
        return main(argv)
    except Exception as e:  # noqa: BLE001 - last-resort guard
        print(f"settings-scrub: internal error ({type(e).__name__}) - could not determine",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(_guarded(sys.argv[1:]))
