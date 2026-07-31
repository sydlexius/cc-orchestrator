#!/usr/bin/env bash
# elmer-triage.sh <PR#> [<PR#>...] [owner/repo]
#
# The MECHANICAL triage drop for the unattended review requester ("elmer"): one
# maildir entry per PR, so a TL wakes to a digest instead of a raw comment dump.
#
# NO MODEL IS INVOLVED. Every field is an existing read-only helper's output or a
# `gh pr view` read - `orchestrate-status.sh` for state/checks/review/merge/unreplied
# and `pr-read-comments.sh` for the finding bodies. That is deliberate and is what
# keeps the loop a DUMB PIPE: the parts of "triage" that are deterministic cost
# nothing, never go stale, and cannot stall on a permission prompt.
#
# THE ONE NON-OBVIOUS FIELD: `triaged_sha`. A report about a PR is only valid for
# the code it was computed against, so the entry records the head SHA it saw, on its
# own line, machine-greppable. A consumer compares it to HEAD at read time: equal
# means the report is LIVE, different means STALE -> re-derive. That check is what
# would later make an OPTIONAL model-driven triage subagent safe to layer on: it
# makes staleness DETECTABLE rather than assumed. (Staleness needs the code to
# CHANGE; nothing pushes overnight, so in practice these match - the check is cheap
# insurance, not an expectation of failure.)
#
# FAIL-SOFT BY CONTRACT. Every composed read is allowed to fail, and a failure is
# RECORDED IN THE ENTRY rather than silently omitted or fatal. Two reasons: an entry
# that is quietly missing its findings section reads as "no findings", which is the
# dangerous direction; and losing four good reports because the fifth PR 404s is the
# exact failure this exists to prevent. A per-PR failure is reported and skipped; the
# sweep continues.
#
# Usage:
#   elmer-triage.sh <PR#> [<PR#>...] [owner/repo]
#
# Exit codes:
#   0  swept (entries written; a per-PR failure is reported, not fatal)
#   2  SETUP ERROR - bad/missing args or an unresolvable repo
#
# Env: ELMER_HOME overrides the maildir root (default ~/.claude/elmer).
#      ELMER_STATUS_ORACLE / ELMER_COMMENT_READER override the composed helper
#      paths (default: the stable ~/.claude/scripts/ copies). Used by the harness.
#
# READ-ONLY toward GitHub: composes read-only helpers and one `gh pr view`; it never
# posts, never mutates, and is never a reason to widen the `gh pr` allow-list.
set -euo pipefail

case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

prs=(); repo=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -*) echo "setup error: unknown flag: $1" >&2; exit 2 ;;
    */*) repo="$1"; shift ;;
    *) prs+=("$1"); shift ;;
  esac
done

if [ "${#prs[@]}" -eq 0 ]; then
  echo "usage: elmer-triage.sh <PR#> [<PR#>...] [owner/repo]" >&2
  exit 2
fi
for p in "${prs[@]}"; do
  if ! [[ "$p" =~ ^[0-9]+$ ]]; then
    echo "setup error: PR# must be numeric, got: $p" >&2
    exit 2
  fi
done

if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
fi
if [ -z "$repo" ]; then
  echo "setup error: could not resolve repo (pass owner/repo, or run inside a gh-aware repo)" >&2
  exit 2
fi

STATUS_ORACLE="${ELMER_STATUS_ORACLE:-${HOME}/.claude/scripts/orchestrate-status.sh}"
COMMENT_READER="${ELMER_COMMENT_READER:-${HOME}/.claude/scripts/pr-read-comments.sh}"

ELMER_HOME="${ELMER_HOME:-$HOME/.claude/elmer}"
triage_dir="$ELMER_HOME/triage"
mkdir -p "$triage_dir"

slug="$(printf '%s' "$repo" | tr '/' '-')"
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
wrote=0

for pr in "${prs[@]}"; do
  # --- head SHA + title (the pin, and the human label) ---
  # A read failure here is NOT fatal: the entry is still worth writing, it just
  # cannot be SHA-pinned, and it says so rather than implying a pin it lacks.
  head_sha=""; title=""
  if pr_raw="$(gh pr view "$pr" --repo "$repo" --json headRefOid,title,state 2>/dev/null)"; then
    head_sha="$(printf '%s' "$pr_raw" | jq -r '.headRefOid // ""' 2>/dev/null || true)"
    title="$(printf '%s' "$pr_raw" | jq -r '.title // ""' 2>/dev/null || true)"
  fi

  # --- composed helper: one compact state line ---
  status_line=""
  if [ -x "$STATUS_ORACLE" ]; then
    status_line="$("$STATUS_ORACLE" "$pr" "$repo" 2>/dev/null || true)"
  fi
  if [ -z "$status_line" ]; then
    status_line="(status UNAVAILABLE: ${STATUS_ORACLE##*/} missing or failed -- re-run by hand)"
  fi

  # --- composed helper: the finding bodies ---
  findings=""
  if [ -x "$COMMENT_READER" ]; then
    findings="$("$COMMENT_READER" "$pr" "$repo" 2>/dev/null || true)"
  fi
  if [ -z "$findings" ]; then
    findings="(findings UNAVAILABLE: ${COMMENT_READER##*/} missing or failed -- do NOT read this as \"no findings\"; re-run by hand)"
  fi

  entry="$triage_dir/${slug}--${pr}.md"
  tmp="$triage_dir/.${slug}--${pr}.tmp.$$"

  # `triaged_sha:` is on its OWN LINE and unadorned so a consumer can grep it
  # without parsing prose. An empty value is written explicitly rather than
  # omitting the line, so "unknown" is distinguishable from "not recorded".
  {
    printf '# triage: %s #%s\n\n' "$repo" "$pr"
    printf 'triaged_sha: %s\n' "${head_sha:-unknown}"
    printf 'triaged_at: %s\n' "$now"
    printf 'repo: %s\n' "$repo"
    printf 'pr: %s\n' "$pr"
    [ -n "$title" ] && printf 'title: %s\n' "$title"
    printf '\n'
    printf 'This report is MECHANICAL: every field below is a read-only helper'\''s\n'
    printf 'output. No model judged anything here.\n\n'
    printf 'CHECK BEFORE ACTING: compare triaged_sha above with the PR'\''s current\n'
    printf 'head. Equal -> this report is live. Different -> the branch moved, so\n'
    printf 're-derive rather than trusting it.\n\n'
    printf '## State\n\n%s\n\n' "$status_line"
    printf '## Findings\n\n%s\n' "$findings"
  } > "$tmp" 2>/dev/null || { rm -f "$tmp"; echo "WARN: could not write triage entry for #$pr" >&2; continue; }

  # A triage entry is a CURRENT-STATE report, not an append-only log: a later run
  # REPLACES it, so a TL never faces two rival reports for one PR.
  mv -f "$tmp" "$entry"
  wrote=$((wrote + 1))
  echo "triaged: $repo #$pr -> $entry"
done

echo "elmer-triage: wrote $wrote entr$( [ "$wrote" -eq 1 ] && echo y || echo ies ) to $triage_dir"
exit 0
