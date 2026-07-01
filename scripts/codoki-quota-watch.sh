#!/usr/bin/env bash
# codoki-quota-watch.sh <PR#> [owner/repo]   (issue #70, DETECT-AND-NOTIFY)
#
# READ-ONLY detector for Codoki's rate-limited state. It DETECTS whether Codoki is
# rate-limited on a PR, parses the next-available-slot time, and NOTIFIES the lead.
# It NEVER posts `@codoki` or any comment: triggering a bot review is maintainer-only
# (per-instance authorization), so this script only surfaces the state + the wait so
# the lead can retrigger on the maintainer's go. That detect-only scope is the whole
# point of the re-scope from the original "auto-retrigger" design.
#
# Three states are distinguished, each with a distinct exit code:
#   SETTLED       Codoki has reviewed / its check is settled. Nothing to do.
#   RATE-LIMITED  Codoki posted its `<!-- CODOKI_RATE_LIMIT -->` marker; the next
#                 slot time is parsed and surfaced. The lead retriggers AFTER the
#                 slot, on the maintainer's go.
#   NOT-YET-RUN   No rate-limit marker and the Codoki check is not settled yet;
#                 Codoki simply has not run. Keep waiting.
#
# SETTLED vs NOT-YET-RUN is decided by REUSING the deterministic oracle
# `ship-gate-preflight.sh --codoki-only` (reads the `Codoki PR Review` status check
# off statusCheckRollup; #110). This script does NOT re-implement settlement polling
# and invents no recovery marker.
#
# Detection of RATE-LIMITED keys on the exact HTML marker `<!-- CODOKI_RATE_LIMIT -->`
# (verified live on PR #88), NOT `<!-- CODOKI_QUOTA -->`. The slot is parsed from
# `Next available slot: **YYYY-MM-DD HH:MM:SS UTC**` as an ABSOLUTE UTC timestamp
# (do NOT confuse with the RELATIVE "N minutes and N seconds" format in CodeRabbit's
# own rate-limit messages).
#
# Usage:
#   codoki-quota-watch.sh <PR#> [owner/repo]
#
# Arguments:
#   PR#         PR number (required, numeric).
#   owner/repo  Repo slug (optional; resolved via `gh repo view` if omitted).
#
# Exit codes (distinct per state; 2 is the repo-conventional setup error):
#   0  SETTLED      - Codoki check settled (reviewed). No action.
#   1  RATE-LIMITED - marker present; next-slot time surfaced for a lead retrigger.
#   2  SETUP ERROR  - bad/missing args, repo unresolvable, or a gh read failure.
#   3  NOT-YET-RUN  - not rate-limited and not settled; Codoki has not run yet.
#
# Output: a single lead-facing notification line per state. For RATE-LIMITED it
# includes the next-slot UTC time, the relative wait, and a US Pacific-labeled time
# (house style). All read-only: paginated `gh api` GETs via `jq -s 'add // []'`.
set -euo pipefail

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

# --- Argument parsing / startup validation ---
if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: codoki-quota-watch.sh <PR#> [owner/repo]" >&2
  exit 2
fi
pr="$1"
repo="${2:-}"
if ! [[ "$pr" =~ ^[0-9]+$ ]]; then
  echo "setup error: PR# must be numeric, got: $pr" >&2
  exit 2
fi
if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
fi
if [ -z "$repo" ]; then
  echo "setup error: could not resolve repo (pass owner/repo, or run inside a gh-aware repo)" >&2
  exit 2
fi

CODOKI_MARKER='<!-- CODOKI_RATE_LIMIT -->'
# Settlement oracle, resolved like pr-watch.sh does (deployed copy under $HOME).
# Overridable via CODOKI_QUOTA_ORACLE so the test harness can stub it.
ORACLE="${CODOKI_QUOTA_ORACLE:-${HOME}/.claude/scripts/ship-gate-preflight.sh}"

# --- Helpers ---

# Parse the absolute UTC slot string -> epoch seconds. macOS-safe dual-form. The
# slot is an ABSOLUTE UTC timestamp, so it MUST be interpreted as UTC: GNU `date`
# gets an explicit " UTC" suffix; BSD `date -j` uses `-u`. (Parsing it without a
# zone would wrongly read it in local time and skew the wait.) Echoes 0 on failure.
slot_to_epoch() {
  local slot="$1"
  date -u -d "$slot UTC" +%s 2>/dev/null \
    || date -j -u -f "%Y-%m-%d %H:%M:%S" "$slot" +%s 2>/dev/null \
    || echo 0
}

# Format an epoch as a US Pacific labeled time (house style). Dual-form.
fmt_pacific() {
  local epoch="$1"
  TZ="America/Los_Angeles" date -d "@$epoch" +'%Y-%m-%d %H:%M %Z' 2>/dev/null \
    || TZ="America/Los_Angeles" date -r "$epoch" +'%Y-%m-%d %H:%M %Z' 2>/dev/null \
    || echo "unknown"
}

# Humanize a positive second count as "Nh Nm Ns" (omitting zero leading units).
humanize_secs() {
  local s="$1" h m
  h=$(( s / 3600 )); m=$(( (s % 3600) / 60 )); s=$(( s % 60 ))
  if [ "$h" -gt 0 ]; then printf '%dh %dm %ds' "$h" "$m" "$s"
  elif [ "$m" -gt 0 ]; then printf '%dm %ds' "$m" "$s"
  else printf '%ds' "$s"; fi
}

# --- Read Codoki's rate-limit marker (READ-ONLY, paginated) ---
# Capture gh's exit status BEFORE the jq pipe: `gh ... 2>/dev/null | jq -s 'add
# // []'` would emit `[]` even when gh FAILS (network/auth/404), masking a read
# error as an empty comment set. A gh read failure is a setup-error (exit 2), per
# the header contract - so the gh GET is its own command, checked, then piped.
raw_comments="$(gh api --paginate "repos/$repo/issues/$pr/comments" 2>/dev/null)" || {
  echo "setup error: could not read issue comments for PR #$pr ($repo) (gh api read failed)" >&2
  exit 2
}
comments="$(printf '%s' "$raw_comments" | jq -s 'add // []' 2>/dev/null || true)"
if [ -z "$comments" ]; then
  echo "setup error: could not parse issue comments for PR #$pr ($repo)" >&2
  exit 2
fi

# Latest Codoki comment carrying the rate-limit marker (Codoki edits in place, so
# prefer updated_at; fall back to created_at). Its body, or empty if none.
# TRUST BOUNDARY: only a comment authored by Codoki itself counts - require the
# author login to be the Codoki bot AND the body to carry the marker, so a
# non-Codoki user cannot spoof `<!-- CODOKI_RATE_LIMIT -->` and force a false
# RATE-LIMITED. The literal login is what this repo keys on everywhere
# (pr-watch.sh, pr-unreplied-comments.sh).
CODOKI_LOGIN='codoki-pr-intelligence[bot]'
rl_body="$(printf '%s' "$comments" | jq -r --arg marker "$CODOKI_MARKER" --arg login "$CODOKI_LOGIN" '
  [ .[] | select((.user.login // "") == $login and ((.body // "") | contains($marker))) ]
  | sort_by(.updated_at // .created_at) | last | .body // ""' 2>/dev/null || true)"

# --- Settlement oracle: SETTLED vs NOT-YET-RUN (#110 reuse) ---
oracle_settled() {
  [ -x "$ORACLE" ] || return 2   # 2 = oracle unavailable (cannot decide)
  if "$ORACLE" --codoki-only "$pr" "$repo" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# SETTLED wins outright: if the Codoki check is green, a (possibly stale) earlier
# rate-limit marker no longer matters - recovery is confirmed by the oracle.
# Capture the rc via `|| rc=$?` so `set -e` is not tripped AND a non-taken `if`
# (which returns 0) cannot mask the real return value.
oracle_rc=0
oracle_settled || oracle_rc=$?
if [ "$oracle_rc" -eq 0 ]; then
  echo "CODOKI SETTLED: Codoki has reviewed PR #$pr ($repo); the check is settled. No action needed."
  exit 0
fi

# Not settled. If the rate-limit marker is present, we are RATE-LIMITED.
if [ -n "$rl_body" ]; then
  slot="$(printf '%s' "$rl_body" \
    | sed -nE 's/.*Next available slot:[[:space:]]*\*{0,2}([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})[[:space:]]*UTC.*/\1/p' \
    | head -1)"
  if [ -z "$slot" ]; then
    echo "CODOKI RATE-LIMITED: PR #$pr ($repo) - marker present but next-slot time could not be parsed. Retrigger on the maintainer's go after the cooldown." >&2
    exit 1
  fi
  slot_epoch="$(slot_to_epoch "$slot")"
  now_epoch="$(date -u +%s)"
  if [ "$slot_epoch" -gt 0 ]; then
    remaining=$(( slot_epoch - now_epoch ))
    pacific="$(fmt_pacific "$slot_epoch")"
    if [ "$remaining" -gt 0 ]; then
      wait_str="in $(humanize_secs "$remaining")"
    else
      wait_str="slot already passed; retrigger window OPEN"
    fi
    echo "CODOKI RATE-LIMITED: PR #$pr ($repo). Next slot: ${slot} UTC (${pacific} PT) -- ${wait_str}. Lead retriggers @codoki on the maintainer's go (this script never posts)." >&2
  else
    echo "CODOKI RATE-LIMITED: PR #$pr ($repo). Next slot: ${slot} UTC (could not convert to epoch/Pacific). Retrigger on the maintainer's go after that time." >&2
  fi
  exit 1
fi

# No marker, not settled. Either Codoki has not run yet, or the oracle is absent.
if [ "$oracle_rc" -eq 2 ]; then
  echo "CODOKI NOT-YET-RUN: PR #$pr ($repo) - no rate-limit marker; settlement oracle unavailable at $ORACLE, so settled-vs-not could not be confirmed. Treating as not-yet-run." >&2
fi
echo "CODOKI NOT-YET-RUN: Codoki has not run on PR #$pr ($repo) and is not rate-limited. Keep waiting."
exit 3
