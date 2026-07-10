#!/usr/bin/env bash
# prose-lint.sh -- lint an outward draft (issue body, PR body, comment) through the shared
# ~/Developer/prose-tooling LanguageTool config before it is posted (issue #219).
#
# A THIN adapter over prose-tooling's bin/prose_check.py -- it reuses, does NOT reimplement,
# the Markdown-aware client and its house-style config. This closes the gap where committed
# Markdown is grammar-checked by the git hooks but the prose cc-orchestrator EMITS (issue/PR
# bodies, review/PR comments) is not.
#
# Usage:
#   prose-lint.sh [--profile docs|microcopy] [--label TEXT] [--no-autostart] [FILE]
#
#   FILE            draft to lint; omit (or pass '-') to read the draft from STDIN.
#   --profile P     forwarded to the client (default: docs). docs = PR/issue bodies;
#                   microcopy = short comment text.
#   --label TEXT    replaces the path column in the output so a stdin draft reads
#                   "(draft):12: [ERROR] ..." and never leaks a temp path. Default: the
#                   FILE path when a file is given, else "(draft)".
#   --no-autostart  forwarded to the client (skip the ~15s LanguageTool container autostart).
#
# Exit codes (pass-through of the client's contract -- this primitive is STRICT):
#   0  clean or advisory-only findings.
#   1  a blocking finding.
#   2  cannot check: LanguageTool server unreachable OR prose-tooling not installed /
#      its .venv python or client is missing. Always a loud stderr message -- never a
#      silent skip. (An advisory caller may CHOOSE to soft-skip on exit 2; the primitive
#      itself does not.)
#
# Least-privilege: reads a draft, runs one fixed LOCAL command, prints findings. No gh, no
# git mutation, no network mutation (the LanguageTool server is local), no allow-list change,
# no floor change. The stdin temp file is 0600 and cleaned on EXIT.
#
# Canonical source: cc-orchestrator repo root. Reached by the prep-pr / new-issue command
# flows via the repo-local path (dev) or ${CLAUDE_PLUGIN_ROOT}/scripts/ (plugin install), so
# it needs no stable-path (~/.claude/scripts) deployment -- unlike the live gh-*/ship-gate
# helpers that other scripts invoke by absolute ~/.claude/scripts path.
set -euo pipefail

die() { echo "prose-lint: $1" >&2; exit 2; }

# -h / --help: print this header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

profile="docs"
label=""
no_autostart=""
file=""

while [ $# -gt 0 ]; do
  case "$1" in
    --profile) [ $# -ge 2 ] || die "--profile needs a value"; profile="$2"; shift 2 ;;
    --label)   [ $# -ge 2 ] || die "--label needs a value";   label="$2";   shift 2 ;;
    --no-autostart) no_autostart="--no-autostart"; shift ;;
    --) shift; file="${1:-}"; break ;;
    -) file="-"; shift; break ;;
    -*) die "unknown option: $1" ;;
    *) file="$1"; shift; break ;;
  esac
done

case "$profile" in
  docs|microcopy) ;;
  *) die "--profile must be docs or microcopy; got: $profile" ;;
esac

# Locate the prose-tooling client + its venv python (target repos stay dependency-free by
# design, so the client runs from prose-tooling's own .venv).
tooling_dir="${PROSE_TOOLING_DIR:-$HOME/Developer/prose-tooling}"
venv_py="$tooling_dir/.venv/bin/python"
client="$tooling_dir/bin/prose_check.py"
if [ ! -x "$venv_py" ] || [ ! -f "$client" ]; then
  die "prose-tooling not usable at $tooling_dir (need .venv/bin/python + bin/prose_check.py); install it or set PROSE_TOOLING_DIR"
fi

# Resolve the input file. Empty or '-' means stdin: capture it to a 0600 temp .md (the .md
# suffix engages the client's Markdown parser), cleaned on EXIT.
tmp_in=""
tmp_out=""
tmp_err=""
# shellcheck disable=SC2329  # invoked indirectly via `trap cleanup EXIT` below
# `return 0` is load-bearing: as the EXIT trap this runs on every exit, and a trailing falsy
# command (e.g. an unset `tmp_err` making `[ -n "" ]` false) would OVERWRITE the script's
# intended exit status -- clobbering a `die`/`exit 2` to 1 on paths that fire before the temps
# are assigned. Keep the trap status-neutral.
cleanup() { [ -n "$tmp_in" ] && rm -f "$tmp_in"; [ -n "$tmp_out" ] && rm -f "$tmp_out"; [ -n "$tmp_err" ] && rm -f "$tmp_err"; return 0; }
trap cleanup EXIT

if [ -z "$file" ] || [ "$file" = "-" ]; then
  tmp_in="$(mktemp -t prose-lint.XXXXXX)" || die "cannot create temp file"
  mv "$tmp_in" "$tmp_in.md"; tmp_in="$tmp_in.md"
  ( umask 077; cat > "$tmp_in" )
  input="$tmp_in"
  [ -n "$label" ] || label="(draft)"
else
  { [ -f "$file" ] && [ -r "$file" ]; } || die "cannot read draft: $file"
  input="$file"
  [ -n "$label" ] || label="$file"
fi

# Run the client, capturing stdout (findings) and stderr (e.g. server-down) separately so the
# label rewrite touches only the finding lines. Preserve the client's exit code.
tmp_out="$(mktemp -t prose-lint-out.XXXXXX)"
tmp_err="$(mktemp -t prose-lint-err.XXXXXX)"

# Build the client argv as an ARRAY so no token is subject to word-splitting/globbing (the
# earlier unquoted ${no_autostart:+...} form was fragile).
client_args=(--profile "$profile")
[ -n "$no_autostart" ] && client_args+=(--no-autostart)
client_args+=("$input")

# Bound the client run so a hung/slow LanguageTool server cannot stall this advisory pre-post
# check indefinitely. `timeout` is OPTIONAL (stock macOS lacks it; coreutils ships `gtimeout`);
# when neither is present we run unbounded -- no regression vs. before. A timeout maps to the
# "cannot check" exit 2 (same class as server-unreachable). Override the bound via
# PROSE_LINT_TIMEOUT (seconds).
timeout_bin=""
if command -v timeout >/dev/null 2>&1; then timeout_bin="timeout"
elif command -v gtimeout >/dev/null 2>&1; then timeout_bin="gtimeout"; fi
tmo="${PROSE_LINT_TIMEOUT:-60}"

set +e
if [ -n "$timeout_bin" ]; then
  "$timeout_bin" "$tmo" "$venv_py" "$client" "${client_args[@]}" >"$tmp_out" 2>"$tmp_err"
  rc=$?
else
  "$venv_py" "$client" "${client_args[@]}" >"$tmp_out" 2>"$tmp_err"
  rc=$?
fi
set -e

# timeout(1) exits 124 when it kills the command -> report as cannot-check (2), loudly. The
# client's own exit codes are only 0/1/2, so 124 unambiguously means the bound fired.
if [ "$rc" = 124 ]; then
  echo "prose-lint: LanguageTool client timed out after ${tmo}s (set PROSE_LINT_TIMEOUT to adjust)" >&2
  rc=2
fi

# Rewrite the leading "input:" path column to the label on each finding line (literal, not
# regex -- awk index()/substr() treat `input` as a plain string).
awk -v inp="$input" -v lab="$label" '
  index($0, inp":") == 1 { print lab substr($0, length(inp) + 1); next }
  { print }
' "$tmp_out"
cat "$tmp_err" >&2

exit "$rc"
