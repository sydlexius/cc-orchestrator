#!/usr/bin/env bash
# orchestrate-context-meter.sh - PostToolUse "context-budget meter" (#228, DESIGN Bet 6). ADVISORY
# and FAIL-OPEN: it NEVER blocks a tool - every path exits 0 (a `trap 'exit 0' EXIT` backstops even
# an unexpected `set -e` abort). On each PostToolUse call it adds a PROXY estimate of the tokens this
# tool call put into the lead's window to a per-session running total, and emits a one-line WARN to
# stderr the FIRST time the cumulative total crosses ~70% and ~85% of a configurable budget.
#
# The TOKEN PROXY is deliberately rough and DOCUMENTED as such: (byte length of the compact
# tool_input JSON + byte length of the tool_response JSON) / 4 (chars-per-token). It measures ONLY
# this call's tool I/O, NOT the full context window - but tool results dominate the lead's window
# (~42% raw-state reads per the context crawl), so cumulative tool I/O is a usable growth proxy. It
# is NOT exact; the meter is an ADVISORY nudge to delegate reads / checkpoint (#227), not a gauge.
#
# INPUT: a PostToolUse hook receives stdin JSON with session_id, tool_name, tool_input, tool_response
# (also cwd, hook_event_name). Parsed with jq. Missing jq / malformed JSON / empty session_id -> exit
# 0 silently (fail-open). STATE lives under ${ORCHESTRATE_CTXMETER_DIR:-${TMPDIR:-/tmp}/orchestrate-ctxmeter}/<session>
# as one line "<total_tokens> <fired_warn> <fired_hard>", a plain read-modify-write. RACE NOTE: within
# one session tool calls are sequential, so overlapping writes are not expected; a plain RMW is
# acceptable for an advisory meter (a lost update at worst delays a WARN one call, never blocks).
set -euo pipefail

emit_warn() { printf 'CTX-METER: %s\n' "$1" >&2; }

# --- validated env knobs (a bad value falls back to the default; a knob NEVER disarms the meter) ---
# Budget in tokens (default 200000). Non-integer / non-positive -> default.
read_budget() {
  local v="${ORCHESTRATE_CONTEXT_BUDGET_TOKENS:-200000}"
  case "$v" in ''|*[!0-9]*) v=200000 ;; esac
  [ "$v" -ge 1 ] 2>/dev/null || v=200000
  printf '%s' "$v"
}
# A percent knob in [1,100] with a caller-supplied default. Non-integer / out-of-range -> default.
read_pct() {
  local v="$1" def="$2"
  case "$v" in ''|*[!0-9]*) v="$def" ;; esac
  { [ "$v" -ge 1 ] && [ "$v" -le 100 ]; } 2>/dev/null || v="$def"
  printf '%s' "$v"
}

# --- the hook body: read stdin JSON, accumulate, warn-once. Fail-open on any error (exit 0). -------
run_meter() {
  local input session_id safe_id dir state_file
  input=$(cat 2>/dev/null || true)
  [ -n "$input" ] || return 0
  command -v jq >/dev/null 2>&1 || return 0

  session_id=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)
  [ -n "$session_id" ] || return 0
  # Sanitize the session id for a filesystem path (no traversal / separators).
  safe_id=$(printf '%s' "$session_id" | LC_ALL=C tr -c 'A-Za-z0-9._-' '_')
  [ -n "$safe_id" ] || return 0

  # PROXY: compact tool_input + tool_response, sum their byte lengths, /4 for a token estimate.
  local ti_json tr_json ti_len tr_len added
  ti_json=$(printf '%s' "$input" | jq -c '.tool_input // {}' 2>/dev/null || printf '{}')
  tr_json=$(printf '%s' "$input" | jq -c '.tool_response // {}' 2>/dev/null || printf '{}')
  ti_len=$(printf '%s' "$ti_json" | LC_ALL=C wc -c 2>/dev/null | tr -cd '0-9')
  tr_len=$(printf '%s' "$tr_json" | LC_ALL=C wc -c 2>/dev/null | tr -cd '0-9')
  case "$ti_len" in ''|*[!0-9]*) ti_len=0 ;; esac
  case "$tr_len" in ''|*[!0-9]*) tr_len=0 ;; esac
  added=$(( (ti_len + tr_len) / 4 ))

  local budget warn_pct hard_pct
  budget=$(read_budget)
  warn_pct=$(read_pct "${ORCHESTRATE_CTXMETER_WARN_PCT:-70}" 70)
  hard_pct=$(read_pct "${ORCHESTRATE_CTXMETER_HARD_PCT:-85}" 85)

  dir="${ORCHESTRATE_CTXMETER_DIR:-${TMPDIR:-/tmp}/orchestrate-ctxmeter}"
  mkdir -p "$dir" 2>/dev/null || return 0
  state_file="$dir/$safe_id"

  # Read prior state (total + which thresholds already fired); validate every field.
  local prev_total=0 fired_warn=0 fired_hard=0
  if [ -f "$state_file" ]; then
    local f_total f_warn f_hard
    read -r f_total f_warn f_hard < "$state_file" 2>/dev/null || true
    case "$f_total" in ''|*[!0-9]*) f_total=0 ;; esac
    case "$f_warn" in 1) f_warn=1 ;; *) f_warn=0 ;; esac
    case "$f_hard" in 1) f_hard=1 ;; *) f_hard=0 ;; esac
    prev_total=$f_total; fired_warn=$f_warn; fired_hard=$f_hard
  fi

  local total pct
  total=$(( prev_total + added ))
  pct=$(( 100 * total / budget ))

  # Fire each threshold AT MOST ONCE per session. Warn first, then hard (a single big jump may cross
  # both). The message quotes the configured percent so a custom knob reads truthfully.
  if [ "$fired_warn" -eq 0 ] && [ "$pct" -ge "$warn_pct" ]; then
    fired_warn=1
    emit_warn "~${warn_pct}% of context budget used - delegate raw-state reads to a digest subagent (#227) and checkpoint now."
  fi
  if [ "$fired_hard" -eq 0 ] && [ "$pct" -ge "$hard_pct" ]; then
    fired_hard=1
    emit_warn "~${hard_pct}% of context budget used - force a checkpoint + hand raw-state reads to the digest subagent; wrap up this window."
  fi

  printf '%s %s %s\n' "$total" "$fired_warn" "$fired_hard" > "$state_file" 2>/dev/null || true
  return 0
}

# --- self-test: feed synthetic payloads through a throwaway state dir and assert threshold-once. ---
# Run by the gate runner / CI (.gates.toml `ctxmeter-self-test` step) to catch a silently broken
# meter, and available to run by hand; doctor only checks that the hook is WIRED + the deployed copy
# is current, it does NOT run this self-test. Prints PASS/FAIL. Runs BEFORE the fail-open EXIT trap
# is installed so a genuine assertion failure can exit non-zero.
if [ "${1:-}" = "--self-test" ]; then
  set +e
  st_dir=$(mktemp -d 2>/dev/null) || { echo "orchestrate-context-meter self-test FAIL: mktemp" >&2; exit 1; }
  st_fail=""
  st_big=$(head -c 3000 /dev/zero 2>/dev/null | tr '\0' 'x')   # ~3000 bytes -> ~750 tokens
  st_small=$(head -c 80 /dev/zero 2>/dev/null | tr '\0' 'x')   # ~80 bytes -> ~20 tokens
  st_huge=$(head -c 3600 /dev/zero 2>/dev/null | tr '\0' 'x')  # ~3600 bytes -> ~900 tokens

  # (1) below threshold (huge budget) -> no warn, exit 0.
  st_out=$(printf '{"session_id":"below","tool_input":{"a":"b"},"tool_response":"%s"}' "$st_small" \
    | ORCHESTRATE_CTXMETER_DIR="$st_dir" ORCHESTRATE_CONTEXT_BUDGET_TOKENS=1000000 "$0" 2>&1); st_rc=$?
  { [ "$st_rc" -eq 0 ] && ! printf '%s' "$st_out" | grep -q 'CTX-METER'; } \
    || st_fail="below-threshold (rc=$st_rc out=$st_out)"

  # (2a) crossing 70% -> warn fires once.
  if [ -z "$st_fail" ]; then
    st_out=$(printf '{"session_id":"w","tool_input":{},"tool_response":"%s"}' "$st_big" \
      | ORCHESTRATE_CTXMETER_DIR="$st_dir" ORCHESTRATE_CONTEXT_BUDGET_TOKENS=1000 "$0" 2>&1); st_rc=$?
    { [ "$st_rc" -eq 0 ] && printf '%s' "$st_out" | grep -q '70%'; } \
      || st_fail="crossing-70 (rc=$st_rc out=$st_out)"
  fi
  # (2b) second small call, still 70-85% -> NO re-warn (fires at most once).
  if [ -z "$st_fail" ]; then
    st_out=$(printf '{"session_id":"w","tool_input":{},"tool_response":"%s"}' "$st_small" \
      | ORCHESTRATE_CTXMETER_DIR="$st_dir" ORCHESTRATE_CONTEXT_BUDGET_TOKENS=1000 "$0" 2>&1); st_rc=$?
    { [ "$st_rc" -eq 0 ] && ! printf '%s' "$st_out" | grep -q 'CTX-METER'; } \
      || st_fail="no-rewarn (rc=$st_rc out=$st_out)"
  fi
  # (3) crossing 85% in one call -> hard warn fires.
  if [ -z "$st_fail" ]; then
    st_out=$(printf '{"session_id":"h","tool_input":{},"tool_response":"%s"}' "$st_huge" \
      | ORCHESTRATE_CTXMETER_DIR="$st_dir" ORCHESTRATE_CONTEXT_BUDGET_TOKENS=1000 "$0" 2>&1); st_rc=$?
    { [ "$st_rc" -eq 0 ] && printf '%s' "$st_out" | grep -q '85%'; } \
      || st_fail="crossing-85 (rc=$st_rc out=$st_out)"
  fi
  # (4) malformed JSON -> exit 0, silent (fail-open).
  if [ -z "$st_fail" ]; then
    st_out=$(printf '%s' '{ not json' \
      | ORCHESTRATE_CTXMETER_DIR="$st_dir" "$0" 2>&1); st_rc=$?
    { [ "$st_rc" -eq 0 ] && [ -z "$st_out" ]; } || st_fail="malformed-json (rc=$st_rc out=$st_out)"
  fi

  rm -rf "$st_dir" 2>/dev/null || true
  if [ -z "$st_fail" ]; then
    echo "orchestrate-context-meter self-test PASS (warn-once at 70%/85%, fail-open, exit 0)"
    exit 0
  fi
  echo "orchestrate-context-meter self-test FAIL: $st_fail" >&2
  exit 1
fi

# From here on the meter is FAIL-OPEN: any exit (including a set -e abort) resolves to exit 0 so a
# broken meter can never block a tool call.
trap 'exit 0' EXIT
run_meter
exit 0
