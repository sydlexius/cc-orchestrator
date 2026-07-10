#!/usr/bin/env bash
# cache-reclaim.sh -- report, and on EXPLICIT request reclaim, build-cache disk space.
#
# Report-first and safe by construction: the default just MEASURES and prints the exact toolchain
# command to reclaim each target; nothing is ever deleted unless you name it with --yes. Reclaim is
# ALWAYS the toolchain's own clean command (`cargo clean`, `npm cache ...`), NEVER a hand-rolled rm
# (the module/registry files are read-only; only the toolchain deletes them safely).
#
# Usage:
#   cache-reclaim.sh [--report] [--root <dir>]     Report disk + reclaimable caches (default).
#   cache-reclaim.sh --nudge                        df-ONLY: one advisory line if the home volume is
#                                                   >= RECLAIM_NUDGE_PCT (default 90) full, else silent.
#                                                   (This is what /post-merge-cleanup calls -- cheap.)
#   cache-reclaim.sh --yes <name|path[,...]> [--root <dir>]
#                                                   Reclaim ONLY the named targets:
#                                                     npm            -> npm cache verify   (light GC)
#                                                     npm=force      -> npm cache clean --force (full)
#                                                     <rust proj dir> -> cargo clean --manifest-path <dir>/Cargo.toml
#                                                     cargo-registry -> cargo cache --autoclean (only if
#                                                                       the cargo-cache plugin is installed;
#                                                                       else a skip + install hint)
#
# Targets (extensible): npm (global cache), cargo-target (per-project target/ dirs -- the real Rust
# disk hog, regenerable, LOCAL to each repo), cargo-registry (report-only behind cargo-cache). Go is
# intentionally OMITTED: it has no surgical reclaim (the build cache self-trims; `go clean -modcache`
# is a full wipe, not a trim).
#
# Safety: reads only in report/nudge mode; the sole mutation is a --yes-gated toolchain clean. Every
# path is quoted and skipped if empty/missing. Fails OPEN -- a du/tool error is caught per-target and
# the script always exits 0, so it can never break a caller (e.g. post-merge-cleanup). No gh/git/
# network mutation, no allow-list/floor change.
#
# Canonical source: cc-orchestrator repo root; reached repo-local or via ${CLAUDE_PLUGIN_ROOT}/scripts.
set -uo pipefail   # deliberately NOT -e: a du/tool failure must skip that target, never abort.

# -h/--help: print this header block as usage.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

mode="report"
root=""
yes_names=""
NUDGE_PCT="${RECLAIM_NUDGE_PCT:-90}"
case "$NUDGE_PCT" in ''|*[!0-9]*) NUDGE_PCT=90 ;; esac

while [ $# -gt 0 ]; do
  case "$1" in
    --report) mode="report"; shift ;;
    --nudge)  mode="nudge";  shift ;;
    # Require the operand explicitly: a bare trailing `--yes`/`--root` ($#==1) would make
    # `shift 2` a no-op and spin the loop forever (there is no `set -e`).
    --yes)    [ $# -ge 2 ] || { echo "cache-reclaim: --yes needs a value" >&2; exit 2; }; mode="yes"; yes_names="$2"; shift 2 ;;
    --root)   [ $# -ge 2 ] || { echo "cache-reclaim: --root needs a value" >&2; exit 2; }; root="$2"; shift 2 ;;
    *) echo "cache-reclaim: unknown arg: $1 (see --help)" >&2; exit 2 ;;
  esac
done

# Home-volume used-capacity as a bare integer percent (empty on any failure).
home_capacity() {
  df -P "$HOME" 2>/dev/null | awk 'NR==2 { gsub(/%/,"",$5); print $5+0 }'
}

# du -sh of a path -> just the size column (empty on failure).
dir_size() { du -sh "$1" 2>/dev/null | cut -f1; }

# --- report -----------------------------------------------------------------

report_npm() {
  command -v npm >/dev/null 2>&1 || return 0
  local p; p="$(npm config get cache 2>/dev/null)"
  [ -n "$p" ] && [ -d "$p" ] || return 0
  echo "  npm             $(dir_size "$p")  (global, unmanaged)  -> 'npm cache verify' (light) | 'npm cache clean --force' (full)"
}

report_cargo_targets() {
  command -v cargo >/dev/null 2>&1 || return 0
  local scan; scan="${root:-$(pwd)}"
  [ -d "$scan" ] || return 0
  # A target/ dir counts only when it has a sibling Cargo.toml (a real Rust build dir, never a stray).
  find "$scan" -type d -name target -prune 2>/dev/null | while IFS= read -r t; do
    local proj; proj="$(dirname "$t")"
    [ -f "$proj/Cargo.toml" ] || continue
    echo "  cargo-target    $(dir_size "$t")  $proj  -> cargo clean --manifest-path \"$proj/Cargo.toml\""
  done
}

report_cargo_registry() {
  command -v cargo >/dev/null 2>&1 || return 0
  local reg; reg="${CARGO_HOME:-$HOME/.cargo}/registry"
  [ -d "$reg" ] || return 0
  echo "  cargo-registry  $(dir_size "$reg")  (report-only)  -> 'cargo install cargo-cache' then 'cargo cache --autoclean'"
}

report_mode() {
  echo "=== disk (home volume) ==="
  df -h "$HOME" 2>/dev/null | sed -n '1,2p'
  echo
  echo "=== reclaimable build caches (nothing is cleaned automatically) ==="
  report_npm
  report_cargo_targets
  report_cargo_registry
  echo
  echo "To reclaim: cache-reclaim.sh --yes <name|rust-project-dir>[,...]  (e.g. --yes npm, or a project path)"
  echo "Go is omitted: its build cache self-trims and there is no surgical modcache reclaim."
}

# --- nudge (df only) --------------------------------------------------------

nudge_mode() {
  local cap; cap="$(home_capacity)"
  [ -n "$cap" ] || return 0
  if [ "$cap" -ge "$NUDGE_PCT" ] 2>/dev/null; then
    echo "Disk ${cap}% full - run /reclaim-cache to reclaim build caches (npm, Rust target/ dirs)."
  fi
  return 0
}

# --- reclaim (--yes) --------------------------------------------------------

reclaim_one() {
  local n="$1"
  case "$n" in
    npm)
      command -v npm >/dev/null 2>&1 && npm cache verify || echo "cache-reclaim: npm not found; skipped 'npm'" >&2 ;;
    npm=force)
      command -v npm >/dev/null 2>&1 && npm cache clean --force || echo "cache-reclaim: npm not found; skipped 'npm=force'" >&2 ;;
    cargo-registry)
      if command -v cargo >/dev/null 2>&1 && cargo cache --version >/dev/null 2>&1; then
        cargo cache --autoclean
      else
        echo "cache-reclaim: the cargo-cache plugin is not installed; run 'cargo install cargo-cache' to reclaim the registry. skipped 'cargo-registry'" >&2
      fi ;;
    *)
      # Otherwise treat as a Rust project directory; require a Cargo.toml so we never clean a
      # non-Rust path. Accept either the project dir itself or a path whose parent holds Cargo.toml.
      if command -v cargo >/dev/null 2>&1 && [ -d "$n" ] && [ -f "$n/Cargo.toml" ]; then
        cargo clean --manifest-path "$n/Cargo.toml"
      else
        echo "cache-reclaim: unknown reclaim target '$n' (expected: npm, npm=force, cargo-registry, or a Rust project dir containing Cargo.toml); skipped" >&2
      fi ;;
  esac
  return 0
}

yes_mode() {
  [ -n "$yes_names" ] || { echo "cache-reclaim: --yes needs a comma-separated list of targets" >&2; return 0; }
  local n
  local IFS=','
  set -f   # disable glob expansion so a token like '*' can NEVER expand to unnamed projects
  for n in $yes_names; do
    [ -n "$n" ] && reclaim_one "$n"
  done
  set +f
  return 0
}

case "$mode" in
  report) report_mode ;;
  nudge)  nudge_mode ;;
  yes)    yes_mode ;;
esac
exit 0
