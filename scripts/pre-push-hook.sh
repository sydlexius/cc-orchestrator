#!/usr/bin/env bash
# pre-push-hook.sh -- a thin git pre-push hook that runs the repo's gates via
# gate-runner.py, so the hook and the /prep-pr command gate from the SAME runner
# (one source of truth). It blocks the push when a required gate fails.
#
# INSTALL (symlink; Design Choice 2 of issue #169):
#   From the repo root:
#     ln -s ../../scripts/pre-push-hook.sh .git/hooks/pre-push
#   (A symlink keeps the hook tracking the repo's copy as it is updated, rather
#   than a stale snapshot. Copy it instead if your environment disallows hook
#   symlinks.)
#
# RESOLUTION: it finds gate-runner.py via, in order, ${CLAUDE_PLUGIN_ROOT}/scripts,
# the repo's own scripts/ dir (resolved from this script's location), then the
# deployed stable path ~/.claude/scripts. It execs whichever it finds first and
# exits with that runner's exit code (non-zero blocks the push).
#
# STANDALONE: gate-runner.py needs no orchestrate session / marker / env beyond
# PATH, so this hook works in any repo.
set -euo pipefail

# git invokes a pre-push hook with stdin (the ref lines) and two argv params
# (remote name + URL); gate-runner does not need them, so they are ignored here.

find_runner() {
  local candidates=()
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
    candidates+=("${CLAUDE_PLUGIN_ROOT}/scripts/gate-runner.py")
  fi
  # This script lives in <repo>/scripts/, so the sibling is the repo copy.
  local self_dir
  self_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  candidates+=("${self_dir}/gate-runner.py")
  candidates+=("${HOME}/.claude/scripts/gate-runner.py")
  local c
  for c in "${candidates[@]}"; do
    if [ -f "$c" ]; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  return 1
}

runner=$(find_runner) || {
  echo "pre-push-hook: gate-runner.py not found (looked in CLAUDE_PLUGIN_ROOT, the repo scripts/ dir, and ~/.claude/scripts)." >&2
  echo "pre-push-hook: install the orchestrate plugin or run 'orchestrate-setup.py configure --apply', then retry." >&2
  exit 1
}

exec python3 "$runner"
