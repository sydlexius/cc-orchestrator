#!/usr/bin/env python3
"""The finding channel helper (#230, design/DESIGN-tl-context-minimization.md #6).

The adv-review <-> implementer loop hands findings across a schema-typed channel
(#225 schemas), SPLIT to preserve PR-BLINDNESS:
  - finding-fix-list/v1    the implementer reads/writes -- {round, findings[...]},
                           NO thread ids, NO bot-reply prose.
  - finding-reply-slice/v1 LEAD-only -- finding_id -> {thread_id, disposition,
                           reply_text}; the implementer never sees it.

This helper is the DETERMINISTIC guard over that channel. It NEVER mutates the
REMOTE and NEVER changes the target repo's working tree, index, or history; its
only network op is `git fetch`, which is read-only to the remote (it only updates
the local object DB + remote-tracking refs, not your branches or working tree).
It NEVER touches GitHub or the allow-list. Subcommands:

  validate <fix-list|reply-slice> <file.json>
      Schema-validate (via orchestrate_schemas) PLUS channel invariants a bare
      schema cannot express (round >= 1; an `addressed` finding carries a fix_sha;
      finding ids unique; a `fix` reply carries reply text).

  liveness <file> --deadline-secs N
      mtime signal so the lead distinguishes a slow writer from a dead one:
      fresh (<=N) | slow (<=2N) | stalled (<=4N) | dead (>4N) | missing. A SIGNAL,
      never a gate -- exit 0 for every present-file state, exit 1 only for missing.

  guard-reply --repo P --branch B --finding ID --sha SHA [--no-fetch]
      THE guardrail: a `fix` reply may be posted only once its fix_sha is (a) an
      ancestor of origin/<B> (PUSHED -- enforces push-first, no 404-on-unpushed)
      AND (b) bound to the finding by a `Finding-Id: <ID>` commit trailer (ancestry
      alone does not prove THIS commit fixed THIS finding). Fetches origin/<B>
      first (read-only) so a stale local ref cannot false-pass; --no-fetch skips it.

  guard-slice --repo P --branch B --fix-list F <reply-slice.json> [--no-fetch]
      Batch guard-reply over every `fix` disposition in the slice, looking up each
      finding's fix_sha in the paired fix-list. merge-safe/rebut replies need no
      SHA and are skipped. One fetch for the whole slice.

Exit codes: 0 = ok/pass, 1 = a check failed, 2 = usage / IO error.
Stdlib only (the repo carries no third-party deps).
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orchestrate_schemas  # noqa: E402

TRAILER_KEY = "Finding-Id"
_KINDS = {"fix-list": "finding-fix-list/v1", "reply-slice": "finding-reply-slice/v1"}


def _err(msg):
    sys.stderr.write(msg.rstrip("\n") + "\n")


def _load(path):
    """Load JSON. Returns (obj, None) or (None, exit_code) on IO/parse error."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except (OSError, json.JSONDecodeError) as e:
        _err(f"cannot read {path}: {e}")
        return None, 2


# --- channel invariants (beyond the bare schema) ----------------------------

def _fix_list_invariants(obj):
    errors = []
    round_ = obj.get("round")
    if isinstance(round_, int) and not isinstance(round_, bool) and round_ < 1:
        errors.append(f"round: must be >= 1, got {round_}")
    seen = set()
    for f in obj.get("findings", []):
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        if fid in seen:
            errors.append(f"findings: duplicate finding id {fid!r}")
        seen.add(fid)
        if f.get("status") == "addressed" and not f.get("fix_sha"):
            errors.append(f"findings: {fid!r} is 'addressed' but has no fix_sha")
    return errors


def _reply_slice_invariants(obj):
    errors = []
    replies = obj.get("replies", {})
    if isinstance(replies, dict):
        for fid, r in replies.items():
            if isinstance(r, dict) and r.get("disposition") == "fix" \
                    and not (r.get("reply_text") or "").strip():
                errors.append(f"replies.{fid}: 'fix' disposition needs reply_text")
    return errors


def cmd_validate(argv):
    if len(argv) != 2 or argv[0] not in _KINDS:
        _err("usage: finding_channel.py validate <fix-list|reply-slice> <file.json>")
        return 2
    kind, path = argv
    obj, ioerr = _load(path)
    if ioerr:
        return ioerr
    errors = orchestrate_schemas.validate(_KINDS[kind], obj)
    errors += (_fix_list_invariants(obj) if kind == "fix-list"
               else _reply_slice_invariants(obj))
    if errors:
        for e in errors:
            _err(e)
        return 1
    return 0


# --- liveness ---------------------------------------------------------------

def cmd_liveness(argv):
    path, deadline = None, None
    it = iter(argv)
    for tok in it:
        if tok == "--deadline-secs":
            deadline = next(it, None)
        elif path is None:
            path = tok
        else:
            _err(f"unexpected argument: {tok}")
            return 2
    if path is None or deadline is None:
        _err("usage: finding_channel.py liveness <file> --deadline-secs N")
        return 2
    try:
        n = int(deadline)
        if n <= 0:
            raise ValueError
    except ValueError:
        _err("--deadline-secs must be a positive integer")
        return 2
    try:
        age = int(_now() - os.stat(path).st_mtime)
    except OSError:
        print(f"missing age=- file={path}")
        return 1
    if age <= n:
        state = "fresh"
    elif age <= 2 * n:
        state = "slow"
    elif age <= 4 * n:
        state = "stalled"
    else:
        state = "dead"
    print(f"{state} age={age}s file={path}")
    return 0


def _now():
    # Isolated for testability; the harness sets file mtimes relative to real time.
    import time
    return time.time()


# --- git plumbing (read-only; check=False, inspect returncode) --------------

def _git(repo, *args):
    """`git -C repo <args>`, NON-INTERACTIVE. Returns (rc, stdout_stripped).

    ls-remote/fetch against an auth-required origin would otherwise block on a
    credential prompt and hang the guard indefinitely (Codoki #255). Force
    GIT_TERMINAL_PROMPT=0 (never prompt for HTTP creds) and default SSH to
    BatchMode (fail fast instead of prompting) so an unreachable/auth-required
    remote fails fast to the 'error' verdict (exit 2, safe-block) rather than
    stalling the pipeline. This preserves the read-only behavior."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    proc = subprocess.run(["git", "-C", repo, *args], env=env,
                          capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout.strip()


def _resolve_commit(repo, sha):
    """Full 40-hex sha for a real commit, or None."""
    rc, out = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    return out if rc == 0 and out else None


def _sync_origin(repo, branch):
    """Refresh origin/<branch> from the remote (read-only). Returns:
      'ok'     remote reachable, branch exists, origin/<branch> now fresh;
      'absent' remote reachable but has NO such branch (definitively unpushed);
      'error'  remote unreachable / other failure (cannot prove pushed).
    Classifying via ls-remote first means a never-pushed branch is 'absent'
    (a real not-pushed verdict, exit 1) and NOT confused with a network failure
    (exit 2) -- and a network failure never falls through to a STALE local ref."""
    rc, out = _git(repo, "ls-remote", "--heads", "origin", branch)
    if rc != 0:
        return "error"
    if not out.strip():
        return "absent"
    frc, _ = _git(repo, "fetch", "origin", branch)
    return "ok" if frc == 0 else "error"


def _is_pushed(repo, full_sha, branch):
    rc, _ = _git(repo, "merge-base", "--is-ancestor", full_sha, f"origin/{branch}")
    return rc == 0


def _bound_to_finding(repo, full_sha, finding):
    rc, out = _git(repo, "show", "-s",
                   f"--format=%(trailers:key={TRAILER_KEY},valueonly=true)", full_sha)
    if rc != 0:
        return False
    values = {line.strip() for line in out.splitlines() if line.strip()}
    return finding in values


def _guard_one(repo, branch, finding, sha, origin_present=True):
    """Core check (no fetch). Returns (ok: bool, message: str).

    origin_present=False means the remote has NO such branch (an 'absent' verdict
    from _sync_origin): the SHA cannot be pushed to a branch that does not exist,
    so this is a HARD not-pushed -- we must NOT consult the local origin/<branch>
    ref, which may be STALE (left behind by a prior fetch after the remote branch
    was deleted/renamed) and would otherwise false-pass the push check."""
    full = _resolve_commit(repo, sha)
    if full is None:
        return False, f"{finding}: {sha!r} is not a commit in {repo}"
    if not origin_present or not _is_pushed(repo, full, branch):
        return False, (f"{finding}: {full} is not on origin/{branch} "
                       "(push first, then reply)")
    if not _bound_to_finding(repo, full, finding):
        return False, (f"{finding}: {full} is not bound to the finding "
                       f"(missing '{TRAILER_KEY}: {finding}' commit trailer)")
    return True, f"OK {finding} {full} on origin/{branch}"


def _parse_flags(argv, flags, bools=()):
    """Tiny flag parser. `flags` = set of --name taking a value; `bools` = set of
    valueless --name. Returns (values_dict, positionals, error_or_None)."""
    vals, pos = {}, []
    it = iter(argv)
    for tok in it:
        if tok in flags:
            v = next(it, None)
            if v is None:
                return None, None, f"missing value for {tok}"
            vals[tok] = v
        elif tok in bools:
            vals[tok] = True
        elif tok.startswith("--"):
            return None, None, f"unknown flag {tok}"
        else:
            pos.append(tok)
    return vals, pos, None


def cmd_guard_reply(argv):
    flags = {"--repo", "--branch", "--finding", "--sha"}
    vals, pos, err = _parse_flags(argv, flags, bools={"--no-fetch"})
    if err or pos or not flags <= set(vals):
        _err("usage: finding_channel.py guard-reply --repo P --branch B "
             "--finding ID --sha SHA [--no-fetch]")
        return 2
    repo, branch = vals["--repo"], vals["--branch"]
    origin_present = True
    if not vals.get("--no-fetch"):
        status = _sync_origin(repo, branch)
        if status == "error":
            _err(f"cannot reach origin for {branch} (cannot prove the SHA is pushed)")
            return 2
        # 'absent' -> the remote has no such branch: a HARD not-pushed. We pass
        # origin_present=False so _guard_one reports it WITHOUT trusting a possibly
        # stale local origin/<branch> ref (the hostile-review false-pass).
        origin_present = status != "absent"
    ok, msg = _guard_one(repo, branch, vals["--finding"], vals["--sha"],
                         origin_present=origin_present)
    (print if ok else _err)(msg)
    return 0 if ok else 1


def cmd_guard_slice(argv):
    flags = {"--repo", "--branch", "--fix-list"}
    vals, pos, err = _parse_flags(argv, flags, bools={"--no-fetch"})
    if err or len(pos) != 1 or not flags <= set(vals):
        _err("usage: finding_channel.py guard-slice --repo P --branch B "
             "--fix-list F <reply-slice.json> [--no-fetch]")
        return 2
    repo, branch = vals["--repo"], vals["--branch"]
    slice_path = pos[0]
    # Validate both slices before trusting their contents. An IO error (exit 2 from
    # cmd_validate) must PROPAGATE as 2, not collapse to a failed check (1).
    for kind, path in (("reply-slice", slice_path), ("fix-list", vals["--fix-list"])):
        rc = cmd_validate([kind, path])
        if rc == 2:
            return 2
        if rc != 0:
            _err(f"{path}: {kind} failed validation")
            return 1
    reply_obj, e1 = _load(slice_path)
    fix_obj, e2 = _load(vals["--fix-list"])
    if e1 or e2:
        return 2
    by_id = {f.get("id"): f for f in fix_obj.get("findings", [])}
    origin_present = True
    if not vals.get("--no-fetch"):
        status = _sync_origin(repo, branch)
        if status == "error":
            _err(f"cannot reach origin for {branch} (cannot prove the SHAs are pushed)")
            return 2
        origin_present = status != "absent"
    failures = []
    for fid, r in reply_obj.get("replies", {}).items():
        if r.get("disposition") != "fix":
            continue  # merge-safe / rebut carry no fix commit
        entry = by_id.get(fid)
        if entry is None:
            failures.append(f"{fid}: 'fix' reply but no such finding in the fix-list")
            continue
        sha = entry.get("fix_sha")
        if not sha:
            failures.append(f"{fid}: 'fix' reply but fix-list entry has no fix_sha")
            continue
        ok, msg = _guard_one(repo, branch, fid, sha, origin_present=origin_present)
        if not ok:
            failures.append(msg)
    if failures:
        for f in failures:
            _err(f)
        return 1
    return 0


_COMMANDS = {
    "validate": cmd_validate,
    "liveness": cmd_liveness,
    "guard-reply": cmd_guard_reply,
    "guard-slice": cmd_guard_slice,
}


def main(argv):
    if not argv or argv[0] not in _COMMANDS:
        _err("usage: finding_channel.py <validate|liveness|guard-reply|guard-slice> ...")
        return 2
    return _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
