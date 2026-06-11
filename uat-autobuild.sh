#!/usr/bin/env bash
# uat-autobuild.sh -- Watch a git branch and auto-rebuild + restart a local UAT server.
#
# Repo-agnostic: all project-specific details (port, build command, server
# command, health URL) are passed via flags. No Stillwater-specific hardcoding.
#
# OVERVIEW
#   An implementer commits to a feature branch in a worktree. This script polls
#   that branch's HEAD; when a new commit lands it rebuilds with --build-cmd. If
#   the build is GREEN, the old UAT server on --port is stopped (safely -- see
#   LEASE-SAFETY below) and a fresh server is started. If the build is RED, the
#   old server keeps running and the failure is logged.
#
# LEASE-SAFETY
#   To stop the old server this script sends SIGTERM (then SIGKILL after a grace
#   period) to the LISTENER on --port only. It identifies the listener via:
#     lsof -nP -iTCP:<port> -sTCP:LISTEN -t
#   This is intentionally narrow: it matches ONLY the process with a LISTEN
#   socket on that port, NEVER TCP clients (e.g. a browser tab). This is
#   critical -- `lsof -ti:<port> | xargs kill` would kill clients too.
#   When the script itself started the server and still holds the PID, it
#   prefers killing that tracked PID (which is even more precise). The listen-
#   socket lookup is the fallback for orphan restarts.
#
# USAGE
#   uat-autobuild.sh [options]
#
# REQUIRED FLAGS
#   --worktree <path>     Git worktree to watch and build in.
#   --port <n>            Leased UAT port for the server.
#   --server-cmd "<cmd>"  Command that starts the server (the caller bakes in
#                         the binary path and all environment: port, DB path,
#                         encryption key, etc.). Runs backgrounded; this script
#                         tracks the PID.
#
# OPTIONAL FLAGS
#   --branch <name>       Branch to watch. Default: worktree's current branch
#                         (git -C <worktree> rev-parse --abbrev-ref HEAD).
#   --build-cmd "<cmd>"   Build command run inside the worktree.
#                         Default: make build
#                         For plain Go: --build-cmd "go build -o bin/app ./cmd/app"
#   --health-url <url>    Polled after restart to confirm the server is up.
#                         Default: http://localhost:<port>/healthz
#   --poll <secs>         Branch-HEAD poll interval in seconds. Default: 12
#   --pause-file <path>   While this file EXISTS, all rebuilds are skipped (so
#                         an in-progress UAT is not disrupted mid-click).
#                         Default: <logdir>/PAUSE
#                         To pause: touch <pause-file>
#                         To resume: rm <pause-file>
#   --logdir <path>       Directory for build.log, server.log, and state files.
#                         Default: /tmp/uat-autobuild/<sanitized-branch>
#   --once                Do a single check-and-maybe-rebuild, then exit.
#                         Useful for debugging the script itself.
#   -h, --help            Print this help and exit 0.
#
# STATE FILES (inside --logdir)
#   build.log         stdout+stderr of the last build command.
#   server.log        stdout+stderr of the current server process.
#   last-built-sha    SHA of the last commit that produced a green build.
#   server.pid        PID of the currently-running server (best-effort).
#   PAUSE             Create this file to hold all rebuilds (see --pause-file).
#
# EXAMPLES
#   # Stillwater-style (Make-based build):
#   uat-autobuild.sh \
#     --worktree ../stillwater-1900 \
#     --port 1975 \
#     --server-cmd "SW_PORT=1975 SW_DB_PATH=/tmp/uat.db bin/stillwater" \
#     --build-cmd "make build"
#
#   # Plain Go build:
#   uat-autobuild.sh \
#     --worktree ../myapp-feature \
#     --branch feature/cool-thing \
#     --port 8090 \
#     --build-cmd "go build -o bin/app ./cmd/app" \
#     --server-cmd "APP_PORT=8090 bin/app" \
#     --health-url "http://localhost:8090/health"
#
#   # Single-shot debug run:
#   uat-autobuild.sh --worktree . --port 8080 --server-cmd "bin/app" --once
#
# DEPENDENCIES
#   bash, git, lsof, curl  (all standard on macOS and most Linux distros)

set -euo pipefail

# ---------------------------------------------------------------------------
# Help: print the header comment block and exit.
# ---------------------------------------------------------------------------
case "${1:-}" in
  -h|--help)
    awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"
    exit 0
    ;;
esac

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
for dep in git lsof curl; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    echo "error: required dependency '$dep' not found in PATH" >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
OPT_WORKTREE=""
OPT_BRANCH=""
OPT_BUILD_CMD="make build"
OPT_PORT=""
OPT_SERVER_CMD=""
OPT_HEALTH_URL=""
OPT_POLL=12
OPT_PAUSE_FILE=""
OPT_LOGDIR=""
OPT_ONCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --worktree)
      [[ $# -lt 2 ]] && { echo "error: --worktree requires a value" >&2; exit 1; }
      OPT_WORKTREE="$2"; shift 2 ;;
    --branch)
      [[ $# -lt 2 ]] && { echo "error: --branch requires a value" >&2; exit 1; }
      OPT_BRANCH="$2"; shift 2 ;;
    --build-cmd)
      [[ $# -lt 2 ]] && { echo "error: --build-cmd requires a value" >&2; exit 1; }
      OPT_BUILD_CMD="$2"; shift 2 ;;
    --port)
      [[ $# -lt 2 ]] && { echo "error: --port requires a value" >&2; exit 1; }
      OPT_PORT="$2"; shift 2 ;;
    --server-cmd)
      [[ $# -lt 2 ]] && { echo "error: --server-cmd requires a value" >&2; exit 1; }
      OPT_SERVER_CMD="$2"; shift 2 ;;
    --health-url)
      [[ $# -lt 2 ]] && { echo "error: --health-url requires a value" >&2; exit 1; }
      OPT_HEALTH_URL="$2"; shift 2 ;;
    --poll)
      [[ $# -lt 2 ]] && { echo "error: --poll requires a value" >&2; exit 1; }
      OPT_POLL="$2"; shift 2 ;;
    --pause-file)
      [[ $# -lt 2 ]] && { echo "error: --pause-file requires a value" >&2; exit 1; }
      OPT_PAUSE_FILE="$2"; shift 2 ;;
    --logdir)
      [[ $# -lt 2 ]] && { echo "error: --logdir requires a value" >&2; exit 1; }
      OPT_LOGDIR="$2"; shift 2 ;;
    --once)
      OPT_ONCE=1; shift ;;
    -h|--help)
      # Already handled above, but catch it here if mixed with other flags.
      awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"
      exit 0 ;;
    *)
      echo "error: unknown flag: $1" >&2
      echo "Run with --help for usage." >&2
      exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Validate required flags
# ---------------------------------------------------------------------------
missing=()
[[ -z "$OPT_WORKTREE" ]]   && missing+=("--worktree")
[[ -z "$OPT_PORT" ]]       && missing+=("--port")
[[ -z "$OPT_SERVER_CMD" ]] && missing+=("--server-cmd")

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "error: missing required flag(s): ${missing[*]}" >&2
  echo "Run with --help for usage." >&2
  exit 1
fi

# Validate worktree path.
if [[ ! -d "$OPT_WORKTREE" ]]; then
  echo "error: --worktree path does not exist: $OPT_WORKTREE" >&2
  exit 1
fi
if ! git -C "$OPT_WORKTREE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: --worktree is not inside a git work tree: $OPT_WORKTREE" >&2
  exit 1
fi

# Validate port is a positive integer.
if ! [[ "$OPT_POLL" =~ ^[0-9]+$ ]] || [[ "$OPT_POLL" -lt 1 ]]; then
  echo "error: --poll must be a positive integer, got: $OPT_POLL" >&2
  exit 1
fi
if ! [[ "$OPT_PORT" =~ ^[0-9]+$ ]] || [[ "$OPT_PORT" -lt 1 ]] || [[ "$OPT_PORT" -gt 65535 ]]; then
  echo "error: --port must be an integer 1-65535, got: $OPT_PORT" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Resolve branch
# ---------------------------------------------------------------------------
if [[ -z "$OPT_BRANCH" ]]; then
  OPT_BRANCH=$(git -C "$OPT_WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  if [[ -z "$OPT_BRANCH" ]] || [[ "$OPT_BRANCH" == "HEAD" ]]; then
    echo "error: could not resolve branch from worktree HEAD (detached?); pass --branch explicitly" >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Defaults that depend on other resolved values
# ---------------------------------------------------------------------------
if [[ -z "$OPT_HEALTH_URL" ]]; then
  OPT_HEALTH_URL="http://localhost:${OPT_PORT}/healthz"
fi

# Sanitize branch name for use in path (replace slashes and spaces with dashes).
branch_slug="${OPT_BRANCH//[\/: ]/-}"

if [[ -z "$OPT_LOGDIR" ]]; then
  OPT_LOGDIR="/tmp/uat-autobuild/${branch_slug}"
fi

if [[ -z "$OPT_PAUSE_FILE" ]]; then
  OPT_PAUSE_FILE="${OPT_LOGDIR}/PAUSE"
fi

mkdir -p "$OPT_LOGDIR"

# Convenience shorthand for state files.
BUILD_LOG="${OPT_LOGDIR}/build.log"
SERVER_LOG="${OPT_LOGDIR}/server.log"
LAST_BUILT_SHA_FILE="${OPT_LOGDIR}/last-built-sha"
SERVER_PID_FILE="${OPT_LOGDIR}/server.pid"

# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------
echo "[uat-autobuild] starting"
echo "  worktree : $OPT_WORKTREE"
echo "  branch   : $OPT_BRANCH"
echo "  port     : $OPT_PORT"
echo "  build    : $OPT_BUILD_CMD"
echo "  server   : $OPT_SERVER_CMD"
echo "  health   : $OPT_HEALTH_URL"
echo "  poll     : ${OPT_POLL}s"
echo "  logdir   : $OPT_LOGDIR"
echo "  pause-at : $OPT_PAUSE_FILE"
[[ "$OPT_ONCE" -eq 1 ]] && echo "  mode     : --once (single iteration)"

# ---------------------------------------------------------------------------
# Signal handling for clean exit
# ---------------------------------------------------------------------------
_cleanup() {
  echo ""
  echo "[uat-autobuild] caught signal -- exiting cleanly (server left running, if any)"
  exit 0
}
trap _cleanup INT TERM

# ---------------------------------------------------------------------------
# Helper: read the tracked server PID, if any.
# Returns empty string if the file does not exist or the PID is not alive.
# ---------------------------------------------------------------------------
tracked_pid() {
  if [[ -f "$SERVER_PID_FILE" ]]; then
    local pid
    pid=$(cat "$SERVER_PID_FILE" 2>/dev/null || true)
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return
    fi
  fi
  echo ""
}

# ---------------------------------------------------------------------------
# Helper: resolve the PID of the LISTENER on $OPT_PORT (not clients).
# Returns empty string if nothing is listening.
# ---------------------------------------------------------------------------
listener_pid() {
  lsof -nP -iTCP:"${OPT_PORT}" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
}

# ---------------------------------------------------------------------------
# Helper: stop the currently-running UAT server (lease-safe).
#
# Strategy:
#   1. If we tracked a PID ourselves, kill that PID (most precise).
#   2. Fall back to the listener on --port (catches orphan restarts).
#   3. Send SIGTERM first; wait up to GRACE_SECS; send SIGKILL if still alive.
#
# NEVER uses `lsof -ti:<port>` without -sTCP:LISTEN; that form matches clients
# (e.g. a browser tab) and would kill unrelated processes.
# ---------------------------------------------------------------------------
GRACE_SECS=10

stop_server() {
  local pid=""
  pid=$(tracked_pid)

  if [[ -z "$pid" ]]; then
    pid=$(listener_pid)
  fi

  if [[ -z "$pid" ]]; then
    # Nothing listening on the port -- nothing to stop.
    return 0
  fi

  echo "[uat-autobuild] stopping server (pid $pid) on port $OPT_PORT"
  kill -TERM "$pid" 2>/dev/null || true

  # Wait for the process to exit, up to GRACE_SECS.
  local waited=0
  while kill -0 "$pid" 2>/dev/null && [[ "$waited" -lt "$GRACE_SECS" ]]; do
    sleep 1
    (( waited++ )) || true
  done

  if kill -0 "$pid" 2>/dev/null; then
    echo "[uat-autobuild] graceful TERM timed out after ${GRACE_SECS}s -- sending SIGKILL to pid $pid"
    kill -KILL "$pid" 2>/dev/null || true
    sleep 1
  fi

  rm -f "$SERVER_PID_FILE"
}

# ---------------------------------------------------------------------------
# Helper: start the server and record its PID.
# ---------------------------------------------------------------------------
start_server() {
  echo "[uat-autobuild] starting server: $OPT_SERVER_CMD"
  # Run the server command in a subshell, backgrounded, with output to server.log.
  # eval is needed to honor shell quoting in OPT_SERVER_CMD (e.g. VAR=val cmd).
  eval "$OPT_SERVER_CMD" >>"$SERVER_LOG" 2>&1 &
  local new_pid=$!
  echo "$new_pid" >"$SERVER_PID_FILE"
  echo "[uat-autobuild] server started (pid $new_pid) -- logs: $SERVER_LOG"
}

# ---------------------------------------------------------------------------
# Helper: poll the health URL until the server responds or we time out.
# Returns 0 on success, 1 on timeout.
# ---------------------------------------------------------------------------
HEALTH_TIMEOUT=30  # seconds to wait for the server to become healthy

wait_healthy() {
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  echo "[uat-autobuild] waiting for health check: $OPT_HEALTH_URL (timeout ${HEALTH_TIMEOUT}s)"
  while [[ $(date +%s) -lt "$deadline" ]]; do
    if curl -sf --max-time 3 "$OPT_HEALTH_URL" >/dev/null 2>&1; then
      echo "[uat-autobuild] server is healthy"
      return 0
    fi
    sleep 2
  done
  echo "[uat-autobuild] WARNING: health check timed out after ${HEALTH_TIMEOUT}s -- server may still be starting"
  return 1
}

# ---------------------------------------------------------------------------
# Helper: run the build inside the worktree.
# Returns 0 on success, 1 on failure.
# Tees output to BUILD_LOG.
# ---------------------------------------------------------------------------
run_build() {
  local sha="$1"
  echo "[uat-autobuild] building sha $sha -- cmd: $OPT_BUILD_CMD"
  echo "--- build $sha @ $(date) ---" >"$BUILD_LOG"
  # Run the build in the worktree directory. bash -c with cd so OPT_BUILD_CMD
  # (which may be "make build" or "go build ...") resolves paths correctly.
  if (cd "$OPT_WORKTREE" && eval "$OPT_BUILD_CMD") >>"$BUILD_LOG" 2>&1; then
    echo "[uat-autobuild] build GREEN for $sha -- logs: $BUILD_LOG"
    return 0
  else
    echo "[uat-autobuild] build RED for $sha -- keeping old server running -- logs: $BUILD_LOG"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
last_built_sha=""
if [[ -f "$LAST_BUILT_SHA_FILE" ]]; then
  last_built_sha=$(cat "$LAST_BUILT_SHA_FILE" 2>/dev/null || true)
fi

echo "[uat-autobuild] watching branch '$OPT_BRANCH' (last built: ${last_built_sha:-none})"

while true; do
  # Wrap the entire poll iteration in a subshell-style error handler so a
  # transient failure (flaky git, stale lock, etc.) logs and continues rather
  # than killing the watcher.
  iteration_ok=1

  {
    # --- Pause check ---
    if [[ -f "$OPT_PAUSE_FILE" ]]; then
      # Silent while paused; the caller is doing UAT and we must not disrupt.
      if [[ "$OPT_ONCE" -eq 1 ]]; then
        echo "[uat-autobuild] paused (pause file exists: $OPT_PAUSE_FILE) -- exiting (--once)"
        exit 0
      fi
      sleep "$OPT_POLL"
      continue
    fi

    # --- Resolve current branch HEAD ---
    current_sha=$(git -C "$OPT_WORKTREE" rev-parse "refs/heads/${OPT_BRANCH}" 2>/dev/null || true)
    if [[ -z "$current_sha" ]]; then
      # Branch ref might be under refs/remotes if worktree is on a different
      # branch. Try the remote tracking ref as a fallback.
      current_sha=$(git -C "$OPT_WORKTREE" rev-parse "refs/remotes/origin/${OPT_BRANCH}" 2>/dev/null || true)
    fi

    if [[ -z "$current_sha" ]]; then
      echo "[uat-autobuild] WARNING: could not resolve HEAD for branch '$OPT_BRANCH' -- will retry"
      iteration_ok=0
    fi
  } || iteration_ok=0

  if [[ "$iteration_ok" -eq 0 ]]; then
    if [[ "$OPT_ONCE" -eq 1 ]]; then
      echo "[uat-autobuild] iteration failed -- exiting (--once)"
      exit 1
    fi
    sleep "$OPT_POLL"
    continue
  fi

  # --- No change? Sleep and continue silently. ---
  if [[ "$current_sha" == "$last_built_sha" ]]; then
    if [[ "$OPT_ONCE" -eq 1 ]]; then
      echo "[uat-autobuild] no new commit (already at $current_sha) -- exiting (--once)"
      exit 0
    fi
    sleep "$OPT_POLL"
    continue
  fi

  # --- New commit detected ---
  echo "[uat-autobuild] new commit detected: ${last_built_sha:-none} -> ${current_sha}"

  if run_build "$current_sha"; then
    # Build succeeded -- safe to swap the server.
    stop_server
    # Brief pause to let the port fully release before the new process binds.
    sleep 1
    start_server
    wait_healthy || true   # Log the warning but do not abort -- the server may still come up.
    echo "$current_sha" >"$LAST_BUILT_SHA_FILE"
    last_built_sha="$current_sha"
  else
    # Build failed -- leave the existing server running.
    echo "[uat-autobuild] build failed at $current_sha -- keeping server on ${last_built_sha:-<none>}"
    # Do NOT update last_built_sha: we only track green builds.
  fi

  if [[ "$OPT_ONCE" -eq 1 ]]; then
    echo "[uat-autobuild] done (--once)"
    exit 0
  fi

  sleep "$OPT_POLL"
done
