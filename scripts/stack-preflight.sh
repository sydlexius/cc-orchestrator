#!/usr/bin/env bash
# stack-preflight.sh [--base <branch>] <slice> <slice> [<slice> ...]
#
# READ-ONLY pre-submit checker for a sanctioned dependent PR stack (/orchestrate:stack-prs).
# `gh stack link` pushes every branch by itself and so skips safe-push.sh's receipt, freshness
# and one-branch checks; this script re-asks those questions for EVERY slice immediately before
# the lead runs link. It never pushes, never calls a gh mutation, never touches a working tree or
# index. Its only network ops are read-only: `gh pr view`, and a `git fetch` (base-freshness's,
# plus one to bring a PR slice's head commit local) that may update the object DB and
# remote-tracking refs, nothing else. Non-interactive: GIT_TERMINAL_PROMPT=0 + SSH BatchMode.
#
# Slices are given BOTTOM TO TOP. Each is detected, never guessed:
#   an existing directory -> a WORKTREE slice (branch = `git -C <wt> branch --show-current`)
#   all digits            -> a PR slice (`gh pr view <n>`)
#   anything else         -> usage error (exit 2)
#
# --base <branch> names the stack TRUNK (the bottom slice's base). Default: the branch origin/HEAD
# points at. Never a hard-coded `main`; an unresolvable trunk is exit 2 (pass --base).
#
# Checks, one labeled line each (`slice <k> <label>: <check>: PASS|FAIL|WARN|INFO|UNKNOWN - ...`):
#   clean     worktree slices: no uncommitted or untracked changes.
#   receipt   worktree slices: <git-dir>/prep-pr-receipt.json is a gate-receipt/v1 from gate-runner
#             with result=pass and tree_sha == refs/heads/<branch>^{tree}. PR-only slices WARN
#             (not checked: no worktree; link can attach an already-pushed PR).
#   pr        PR slices: state OPEN (draft state reported as INFO).
#   pr-base   PR slices: slice 1's base is the trunk; slice k>1's base is slice k-1's branch, or the
#             trunk (INFO: not yet stacked, link will retarget it).
#   fresh     slice 1 only: base-freshness.sh <trunk> <tip> reads fresh. UNKNOWN is a STOP here
#             (exit 2), unlike prep-pr: link bypasses safe-push, so doubt never passes.
#   ancestry  slice k>1: slice k-1's tip is an ancestor of slice k's tip.
#
# base-freshness.sh is called from THIS script's own directory, never from the caller's cwd: a
# consumer repo's same-named scripts/ file must never substitute for the gate (the #433 rule), and
# the sibling ships in the same plugin/repo copy. The call happens inside this process, so the
# PreToolUse "Helper exec paths" rule (literal paths on the Bash command line) does not apply to it.
#
# Exit: 0 every check passed (WARN/INFO allowed); 1 a definitive failure; 2 usage error OR anything
# undeterminable (gh read failure, unreadable receipt, unknown freshness, unresolvable commit).
# 2 OUTRANKS 1: a stack with an unreadable slice is not "merely failing", it is unknown.
set -o pipefail

usage() {
  echo "usage: stack-preflight.sh [--base <branch>] <slice> <slice> [<slice> ...]" >&2
  echo "       slice = an existing worktree directory, or a PR number; bottom to top" >&2
  exit 2
}
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

export GIT_TERMINAL_PROMPT=0
if [ -n "${GIT_SSH_COMMAND:-}" ]; then GIT_SSH_COMMAND="$GIT_SSH_COMMAND -o BatchMode=yes"; else GIT_SSH_COMMAND="ssh -o BatchMode=yes"; fi
export GIT_SSH_COMMAND

SELF_DIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd) || { echo "stack-preflight: cannot resolve own directory" >&2; exit 2; }

trunk=""
n=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --base)
      [ "$#" -ge 2 ] || { echo "stack-preflight: --base needs a branch name" >&2; usage; }
      trunk="$2"; shift 2; continue ;;
    -*) echo "stack-preflight: unknown flag '$1'" >&2; usage ;;
  esac
  n=$((n + 1))
  if [ -d "$1" ]; then
    kind[n]="wt"; arg[n]=$(cd "$1" && pwd)
  else
    case "$1" in
      ''|*[!0-9]*) echo "stack-preflight: slice '$1' is neither an existing directory nor a PR number" >&2; usage ;;
    esac
    kind[n]="pr"; arg[n]="$1"
  fi
  shift
done
[ "$n" -ge 2 ] || { echo "stack-preflight: a stack needs at least two slices (got $n)" >&2; usage; }

worst=0
fail() { [ "$worst" -lt 1 ] && worst=1; }
undet() { worst=2; }
say() { echo "slice $1 $2: $3: $4"; }

# Git context for ancestry and freshness: the first worktree slice, else the caller's cwd. Every
# worktree slice must share its object store (same --git-common-dir), or ancestry is meaningless.
ctx=""
for k in $(seq 1 "$n"); do
  if [ "${kind[k]}" = wt ]; then ctx="${arg[k]}"; break; fi
done
[ -n "$ctx" ] || ctx=$(pwd)
ctx_common=$(cd "$ctx" && git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || {
  echo "stack-preflight: '$ctx' is not inside a git repository" >&2; exit 2; }

if [ -z "$trunk" ]; then
  trunk=$(git -C "$ctx" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)
  trunk="${trunk#origin/}"
  [ -n "$trunk" ] || { echo "stack-preflight: cannot resolve the trunk from origin/HEAD; pass --base <branch>" >&2; exit 2; }
fi
git check-ref-format "refs/heads/$trunk" >/dev/null 2>&1 || { echo "stack-preflight: invalid --base '$trunk'" >&2; usage; }
echo "stack-preflight: trunk=$trunk slices=$n"

# --- Resolve every slice: branch, tip commit, and (PR slices) base/state ---
for k in $(seq 1 "$n"); do
  branch[k]=""; tip[k]=""
  if [ "${kind[k]}" = wt ]; then
    wt="${arg[k]}"; label[k]="($wt)"
    c=$(cd "$wt" && git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || {
      echo "stack-preflight: '$wt' is not a git worktree" >&2; exit 2; }
    [ "$c" = "$ctx_common" ] || { echo "stack-preflight: '$wt' belongs to a different repository than '$ctx'" >&2; exit 2; }
    branch[k]=$(git -C "$wt" branch --show-current 2>/dev/null || true)
    if [ -z "${branch[k]}" ]; then say "$k" "${label[k]}" branch "FAIL - detached HEAD (no branch to link)"; fail; continue; fi
    tip[k]=$(git -C "$wt" rev-parse --verify --quiet "refs/heads/${branch[k]}^{commit}" 2>/dev/null || true)
    [ -n "${tip[k]}" ] || { say "$k" "${label[k]}" branch "UNKNOWN - cannot resolve refs/heads/${branch[k]}"; undet; continue; }
  else
    label[k]="(#${arg[k]})"
    if ! js=$(cd "$ctx" && gh pr view "${arg[k]}" --json headRefName,baseRefName,headRefOid,isDraft,state 2>/dev/null); then
      say "$k" "${label[k]}" pr "UNKNOWN - gh pr view ${arg[k]} failed (read failure is never a pass)"; undet; continue
    fi
    if ! fields=$(printf '%s' "$js" | python3 -c 'import json, re, sys
d = json.load(sys.stdin)
h, b, o, s = d["headRefName"], d["baseRefName"], d["headRefOid"], d["state"]
assert all(isinstance(x, str) and x and "\t" not in x and "\n" not in x for x in (h, b, o, s))
assert re.fullmatch("[0-9a-f]{40}", o) and isinstance(d["isDraft"], bool)
print("\t".join((h, b, o, s, str(d["isDraft"]).lower())))' 2>/dev/null); then
      say "$k" "${label[k]}" pr "UNKNOWN - gh pr view ${arg[k]} returned unparseable JSON"; undet; continue
    fi
    IFS="$(printf '\t')" read -r f_head f_base f_oid f_state f_draft <<EOF
$fields
EOF
    branch[k]="$f_head"; prbase[k]="$f_base"; tip[k]="$f_oid"; prstate[k]="$f_state"; prdraft[k]="$f_draft"
    # Bring the head commit local for ancestry (read-only fetch; a validated plain branch name).
    if ! git -C "$ctx" cat-file -e "${tip[k]}^{commit}" 2>/dev/null; then
      if git check-ref-format "refs/heads/${branch[k]}" >/dev/null 2>&1; then
        git -C "$ctx" fetch --quiet origin "${branch[k]}" >/dev/null 2>&1 || true
      fi
      if ! git -C "$ctx" cat-file -e "${tip[k]}^{commit}" 2>/dev/null; then
        say "$k" "${label[k]}" pr "UNKNOWN - head commit ${tip[k]} is not available locally (fetch failed)"; undet; tip[k]=""
      fi
    fi
  fi
done

# Duplicate branches would make link's chaining ambiguous.
for i in $(seq 1 "$n"); do for j in $(seq 1 "$n"); do
  if [ "$i" -lt "$j" ] && [ -n "${branch[i]}" ] && [ "${branch[i]}" = "${branch[j]}" ]; then
    echo "stack-preflight: slices $i and $j are the same branch '${branch[i]}'" >&2; exit 2
  fi
done; done

# --- Per-slice checks ---
for k in $(seq 1 "$n"); do
  L="${label[k]} [${branch[k]:-?}]"
  if [ "${kind[k]}" = wt ]; then
    wt="${arg[k]}"
    if ! st=$(git -C "$wt" status --porcelain --untracked-files=normal 2>/dev/null); then
      say "$k" "$L" clean "UNKNOWN - cannot read git status"; undet
    elif [ -n "$st" ]; then
      say "$k" "$L" clean "FAIL - uncommitted or untracked changes"; fail
    else
      say "$k" "$L" clean "PASS"
    fi
    if [ -n "${tip[k]}" ]; then
      gd=$(git -C "$wt" rev-parse --absolute-git-dir 2>/dev/null || true)
      rc_path="$gd/prep-pr-receipt.json"
      want_tree=$(git -C "$wt" rev-parse --verify --quiet "refs/heads/${branch[k]}^{tree}" 2>/dev/null || true)
      if [ -z "$gd" ] || [ -z "$want_tree" ]; then
        say "$k" "$L" receipt "UNKNOWN - cannot resolve the git-dir or branch tree"; undet
      elif [ ! -e "$rc_path" ]; then
        say "$k" "$L" receipt "FAIL - no gate receipt at $rc_path (run /prep-pr on this branch)"; fail
      else
        # Prints "ok <tree>" or "bad <reason>" (definitive) or exits nonzero (unreadable).
        r=$(python3 -c 'import json, re, sys
d = json.load(open(sys.argv[1]))
if not isinstance(d, dict):
    print("bad the receipt is not a JSON object"); sys.exit(0)
for key, want in (("schema", "gate-receipt/v1"), ("producer", "gate-runner"), ("result", "pass")):
    if d.get(key) != want:
        print("bad receipt %s is %s, expected %s" % (key, json.dumps(d.get(key)), want)); sys.exit(0)
t = d.get("tree_sha")
if not isinstance(t, str) or not re.fullmatch("[0-9a-fA-F]{40}", t):
    print("bad receipt tree_sha is not a 40-hex SHA"); sys.exit(0)
print("ok " + t.lower())' "$rc_path" 2>/dev/null)
        case "$r" in
          "ok $want_tree") say "$k" "$L" receipt "PASS - result=pass, tree $want_tree" ;;
          ok\ *) say "$k" "$L" receipt "FAIL - STALE: receipt gated tree ${r#ok }, branch is tree $want_tree (re-run /prep-pr)"; fail ;;
          bad\ *) say "$k" "$L" receipt "FAIL - ${r#bad }"; fail ;;
          *) say "$k" "$L" receipt "UNKNOWN - receipt at $rc_path is unreadable or not JSON"; undet ;;
        esac
      fi
    fi
  else
    say "$k" "$L" receipt "WARN - not checked (no worktree; PR slice)"
    if [ -n "${prstate[k]:-}" ]; then
      if [ "${prstate[k]}" = OPEN ]; then
        say "$k" "$L" pr "PASS - OPEN$([ "${prdraft[k]}" = true ] && echo ' (draft)')"
      else
        say "$k" "$L" pr "FAIL - state ${prstate[k]}, expected OPEN"; fail
      fi
      if [ "$k" -eq 1 ]; then want_base="$trunk"; else want_base="${branch[k-1]}"; fi
      if [ "${prbase[k]}" = "$want_base" ]; then
        say "$k" "$L" pr-base "PASS - based on $want_base"
      elif [ "$k" -gt 1 ] && [ "${prbase[k]}" = "$trunk" ]; then
        say "$k" "$L" pr-base "INFO - based on the trunk $trunk, not yet stacked (link will chain it onto ${want_base:-slice $((k - 1))})"
      else
        say "$k" "$L" pr-base "FAIL - based on ${prbase[k]}, expected ${want_base:-the branch of slice $((k - 1))}"; fail
      fi
    fi
  fi

  [ -n "${tip[k]}" ] || continue
  if [ "$k" -eq 1 ]; then
    if [ ! -f "$SELF_DIR/base-freshness.sh" ]; then
      say "$k" "$L" fresh "UNKNOWN - base-freshness.sh not found beside this script (gate did not run)"; undet; continue
    fi
    out=$(cd "$ctx" && bash "$SELF_DIR/base-freshness.sh" "$trunk" "${tip[k]}" 2>&1); frc=$?
    line=$(printf '%s\n' "$out" | grep '^freshness:' | tail -n 1)
    case "$frc:$line" in
      "0:freshness: fresh"*) say "$k" "$L" fresh "PASS - ${line#freshness: }" ;;
      "1:freshness: behind"*) say "$k" "$L" fresh "FAIL - ${line#freshness: }"; fail ;;
      *) say "$k" "$L" fresh "UNKNOWN - ${line:-base-freshness rc=$frc} (doubt is a STOP before link)"; undet ;;
    esac
  else
    prev="${tip[k-1]}"
    if [ -z "$prev" ]; then
      say "$k" "$L" ancestry "UNKNOWN - slice $((k - 1)) tip unresolved"; undet; continue
    fi
    git -C "$ctx" merge-base --is-ancestor "$prev" "${tip[k]}" 2>/dev/null; arc=$?
    case "$arc" in
      0) say "$k" "$L" ancestry "PASS - contains slice $((k - 1)) tip ${prev:0:12}" ;;
      1) say "$k" "$L" ancestry "FAIL - does not contain slice $((k - 1)) tip ${prev:0:12} (rebuild it on the lower slice before the first push)"; fail ;;
      *) say "$k" "$L" ancestry "UNKNOWN - merge-base failed (rc=$arc)"; undet ;;
    esac
  fi
done

case "$worst" in
  0) echo "stack-preflight: PASS" ;;
  1) echo "stack-preflight: FAIL" ;;
  *) echo "stack-preflight: FAIL (undeterminable; fix the UNKNOWN lines, never link on doubt)" ;;
esac
exit "$worst"
