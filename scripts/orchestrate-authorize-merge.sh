#!/usr/bin/env bash
# orchestrate-authorize-merge.sh <pr> [owner/repo] - #263 Piece B.
#
# Arms a short-TTL, session-scoped merge-auth TOKEN that the deterministic floor
# (orchestrate-guard.sh) checks to ALLOW a `gh pr merge <pr> --match-head-commit
# <sha>` inside an orchestrate (marker-active) session, WITHOUT teaching the floor
# to do network I/O. The network readiness check lives HERE (a deliberate step run
# on the human's "merge" instruction), not in the hot floor path:
#
#   1. Run the hardened ship-gate-preflight (the #110/#263-Piece-A oracle). It reads
#      CI + review-body findings + reviewDecision + review threads + the Codoki ack,
#      fails CLOSED on any doubt, and on PASS emits the validated `headRefOid=<sha>`
#      from the same snapshot as the checks.
#   2. ONLY on PASS with an emitted SHA, write the token {pr, head_sha, expiry}. The
#      merge must then pin `--match-head-commit <sha>`; the floor allows it iff a
#      fresh token's head_sha matches that pinned SHA. gh itself refuses the merge
#      unless the SHA is the PR's current head, so the token cannot authorize a
#      different PR or a moved HEAD.
#
# This is the lead's helper, NOT the floor. It performs no gh/git REMOTE mutation
# (its only write is the local token file); the readiness read is delegated to the
# read-only preflight. Exit 0 = armed, 1 = usage / not-an-orchestrate-session,
# 2 = readiness BLOCKED (no token armed).
#
# Reversal (close-the-door): the floor stops honoring the token the moment
# merge_authorized() is removed or the TTL is set to 0; this helper and Piece A are
# untouched by that. See DESIGN-merge-gate-readiness-vs-authority.md.
set -euo pipefail

pr="${1:-}"
repo="${2:-}"
if [ -z "$pr" ]; then
  echo "usage: orchestrate-authorize-merge.sh <pr> [owner/repo]" >&2
  exit 1
fi
case "$pr" in
  ''|*[!0-9]*) echo "usage: orchestrate-authorize-merge.sh: <pr> must be numeric (got '$pr')" >&2; exit 1 ;;
esac

# A merge-auth token is session-scoped. No $TMUX => not an orchestrate session =>
# the floor never gates a merge here, so there is nothing to arm.
if [ -z "${TMUX:-}" ]; then
  echo "authorize-merge: not in an orchestrate session (no \$TMUX); the floor does not gate merges here, nothing to arm." >&2
  exit 1
fi
# Session key: MUST mirror the floor's _session_key() exactly (LC_ALL=C byte-mode).
key=$(printf '%s' "$TMUX" | LC_ALL=C tr -c 'A-Za-z0-9' '_')

FLOOR_DIR="${ORCHESTRATE_FLOOR_DIR:-$HOME/.claude/orchestrate-floor.d}"
TTL_MIN="${ORCHESTRATE_MERGE_AUTH_TTL_MIN:-10}"
# Empty / non-numeric / negative -> the 10m default (a bad value must not disarm the
# gate). TTL_MIN=0 IS allowed and meaningful: it arms an already-expired token so the
# guard always denies - the documented env kill-switch (set TTL 0 to stop honoring
# tokens without a code change; see the reversal note above and the design doc).
case "$TTL_MIN" in ''|*[!0-9]*) TTL_MIN=10 ;; esac

# Locate the hardened readiness oracle (plugin root first, then the stable path).
preflight="${CLAUDE_PLUGIN_ROOT:-}/scripts/ship-gate-preflight.sh"
if [ ! -x "$preflight" ]; then
  preflight="$HOME/.claude/scripts/ship-gate-preflight.sh"
fi
if [ ! -x "$preflight" ]; then
  echo "authorize-merge: ship-gate-preflight.sh not found/executable; cannot verify readiness." >&2
  exit 2
fi

# Run the readiness gate. FAIL CLOSED: any non-zero (BLOCK / usage / lookup error)
# means NOT ready -> do not arm.
out=""
rc=0
if [ -n "$repo" ]; then
  out=$("$preflight" "$pr" "$repo" 2>&1) || rc=$?
else
  out=$("$preflight" "$pr" 2>&1) || rc=$?
fi
if [ "$rc" -ne 0 ]; then
  echo "authorize-merge: readiness gate did NOT pass for #$pr (exit $rc) - not arming a merge token." >&2
  printf '%s\n' "$out" | tail -3 >&2
  exit 2
fi

# Extract the validated head SHA the oracle emitted (Piece A). No SHA => cannot pin
# the merge => do not arm.
# `|| true`: a no-match grep exits 1 and would trip `set -e`/`pipefail` before the
# explicit empty-SHA check below; soft-fail so that check owns the exit-2 decision.
sha=$(printf '%s\n' "$out" | grep -oE 'headRefOid=[0-9a-f]{40,}' | head -1 | sed 's/^headRefOid=//' || true)
if [ -z "$sha" ]; then
  echo "authorize-merge: readiness PASSed but the oracle emitted no headRefOid; refusing to arm without a pinnable SHA." >&2
  exit 2
fi

# Write the token atomically, owner-only. {pr, head_sha, expiry_epoch}.
now=$(date +%s)
expiry=$(( now + TTL_MIN * 60 ))
auth_dir="$FLOOR_DIR/merge-auth"
( umask 077; mkdir -p "$auth_dir" )
tok="$auth_dir/$key"
tmp="$tok.tmp.$$"
( umask 077; printf '{"pr":%s,"head_sha":"%s","expiry":%s}\n' "$pr" "$sha" "$expiry" > "$tmp" )
mv -f "$tmp" "$tok"
chmod 600 "$tok" 2>/dev/null || true

echo "authorize-merge: armed merge for #$pr @ $sha (expires in ${TTL_MIN}m)."
echo "  merge with:  gh pr merge $pr --squash --match-head-commit $sha"
exit 0
