#!/usr/bin/env bash
# orchestrate-feedback.sh <subcommand> ...   (issue #149)
#
# Maildir-backed orchestrate feedback store: ONE FILE PER ENTRY under inbox/,
# moved to drained/ when it is filed as a GitHub issue. One-file-per-entry makes
# concurrent writes from MULTIPLE active team leads race-free WITHOUT locks or a
# database: each `add` writes a uniquely-named file via mktemp + atomic rename, so
# there is no shared file to clobber (the failure mode of the legacy flat
# ~/.claude/orchestrate-session-feedback.md when two leads append at once).
#
# Subcommands:
#   add <slug> [body]      Create inbox/<ts>-<repo>-<slug>-<rand>.md. Body comes
#                          from the trailing positional, or from STDIN if omitted.
#                          Prints the created filename to stdout.
#   drain <entry> --issue N --verdict <text>
#                          Append a `DRAINED -> #N [verdict]` breadcrumb to the
#                          entry, then move it inbox/ -> drained/ (cold storage).
#                          Use ONLY after the 3-step gate: hostile review -> file
#                          the issue -> drain (so #N already exists). Refuses if the
#                          entry is missing or already drained.
#   list                   List inbox/ entries (chronological via the ts prefix),
#                          one per line. NEVER lists drained/.
#
# MAILDIR defaults to ~/.claude/orchestrate-feedback; override with
# $ORCHESTRATE_FEEDBACK_DIR (used by the test harness for isolation).
#
# drained/ is COLD STORAGE: do NOT read it during a drain pass. Dedup a new
# entry against `gh issue list`, NEVER against the drained corpus (the GitHub
# issue is the durable record; drained/ is an audit trail only).
#
# Canonical source: cc-orchestrator repo scripts/. Invoke as
# ${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-feedback.sh.
set -euo pipefail

MAILDIR="${ORCHESTRATE_FEEDBACK_DIR:-$HOME/.claude/orchestrate-feedback}"
INBOX="$MAILDIR/inbox"
DRAINED="$MAILDIR/drained"

die() { echo "orchestrate-feedback: $*" >&2; exit 1; }

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

ensure_dirs() {
  mkdir -p "$INBOX" "$DRAINED"
  if [ ! -f "$MAILDIR/README.md" ]; then
    cat >"$MAILDIR/README.md" <<'README'
# orchestrate feedback (maildir)

One file per feedback entry. Managed by `orchestrate-feedback.sh` (cc-orchestrator).

- `inbox/`   - undrained entries awaiting the 3-step drain gate
               (hostile review -> file GitHub issue -> drain). Filename:
               `YYYYMMDDTHHMMSSZ-<repo>-<slug>-<rand>.md` (the ts prefix sorts
               chronologically; the rand suffix makes concurrent adds collision-proof).
- `drained/` - COLD STORAGE for entries already filed as issues. Each carries a
               `DRAINED -> #N [verdict]` breadcrumb. DO NOT read this during a
               drain pass: dedup a candidate against `gh issue list`, never
               against this corpus (the GitHub issue is the durable record).

Concurrency: one-file-per-entry + atomic rename = race-free for multiple active
team leads, no locks. Never hand-edit; use the helper's add/drain/list.
README
  fi
}

sub="${1:-}"
[ -n "$sub" ] || die "usage: orchestrate-feedback.sh <add|drain|list> ... (see --help)"
shift

case "$sub" in
  add)
    slug="${1:-}"
    [ -n "$slug" ] || die "add: usage: add <slug> [body]   (body from stdin if omitted)"
    shift
    # Sanitize the slug to filename-safe chars (no newline/slash/space can reach
    # the filename); collapse runs and trim to a short prefix.
    slug=$(printf '%s' "$slug" | tr -c 'A-Za-z0-9_-' '-' | tr -s '-' | sed 's/^-//; s/-$//')
    [ -n "$slug" ] || die "add: slug reduced to empty after sanitizing"
    slug=$(printf '%s' "$slug" | cut -c1-40)
    # Body: trailing positional if present, else STDIN.
    if [ "$#" -gt 0 ]; then
      body="$*"
    else
      body=$(cat)
    fi
    [ -n "$body" ] || die "add: empty body (pass a body arg or pipe it on stdin)"
    repo="${GITHUB_REPOSITORY:-}"
    if [ -z "$repo" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)
    fi
    repo=$(printf '%s' "${repo:-unknown}" | tr -c 'A-Za-z0-9_-' '-' | tr -s '-' | sed 's/^-//; s/-$//')
    [ -n "$repo" ] || repo="unknown"
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    ensure_dirs
    tmp=$(mktemp "$INBOX/.tmp.XXXXXX")
    printf '%s\n' "$body" >"$tmp"
    # Atomically CLAIM a unique final name: `ln` (hardlink, no -f) FAILS if the
    # name is already taken, so it is a race-free test-and-set even with many
    # leads adding at once (plain `mv` would silently clobber). Regenerate the
    # random suffix and retry on the rare collision; remove the tmp once linked
    # into place (or on give-up).
    fname=""
    attempts=0
    while : ; do
      rand=$(printf '%04x%02x' "$((RANDOM))" "$((RANDOM % 256))")
      candidate="${ts}-${repo}-${slug}-${rand}.md"
      if ln "$tmp" "$INBOX/$candidate" 2>/dev/null; then
        fname="$candidate"
        rm -f "$tmp"
        break
      fi
      attempts=$((attempts + 1))
      if [ "$attempts" -ge 8 ]; then
        rm -f "$tmp"
        die "add: could not allocate a unique entry name after $attempts attempts"
      fi
    done
    echo "$fname"
    ;;

  drain)
    entry="${1:-}"
    [ -n "$entry" ] || die "drain: usage: drain <entry> --issue N --verdict <text>"
    shift
    issue=""
    verdict=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --issue) issue="${2:-}"; shift 2 ;;
        --verdict) verdict="${2:-}"; shift 2 ;;
        *) die "drain: unknown arg '$1' (expected --issue N --verdict <text>)" ;;
      esac
    done
    case "$issue" in
      ''|*[!0-9]*) die "drain: --issue must be a numeric issue number" ;;
    esac
    [ -n "$verdict" ] || die "drain: --verdict <text> is required"
    # Reject path traversal: <entry> must be a bare inbox filename.
    case "$entry" in
      */*|*..*) die "drain: <entry> must be a bare inbox filename, not a path" ;;
    esac
    ensure_dirs
    src="$INBOX/$entry"
    # CLAIM-THEN-VALIDATE (closes a TOCTOU symlink race). A check on inbox/<entry>
    # followed by a later read is racy: a writer to inbox/ could swap the file for a
    # symlink between the check and the read, leaking an arbitrary target into
    # drained/. Instead, atomically RENAME the entry to a PRIVATE claim path FIRST
    # (mv -n refuses to clobber); after that the path is private and cannot be
    # swapped, so validating + reading the CLAIMED path is race-free. A symlink
    # entry is renamed AS the link (rename never follows it) and rejected here.
    claim="$INBOX/.claim.$$.$RANDOM"
    mv -n -- "$src" "$claim" 2>/dev/null \
      || die "drain: '$entry' not found in inbox (already drained, or wrong name?)"
    if [ ! -f "$claim" ] || [ -L "$claim" ]; then
      rm -f -- "$claim"
      die "drain: '$entry' is not a regular (non-symlink) file in inbox"
    fi
    # The claim is private now: append the breadcrumb, then move to cold storage.
    printf '\nDRAINED -> #%s [%s]\n' "$issue" "$verdict" >>"$claim"
    mv -- "$claim" "$DRAINED/$entry"
    echo "drained $entry -> #$issue"
    ;;

  list)
    ensure_dirs
    # One filename per line, chronological via the ts prefix. Empty output (exit 0)
    # when the inbox is empty. Never lists drained/. The literal-glob case (no *.md)
    # is skipped by the -e guard, so an empty inbox prints nothing.
    for f in "$INBOX"/*.md; do
      [ -e "$f" ] || continue
      basename "$f"
    done | sort
    ;;

  *)
    die "unknown subcommand '$sub' (expected add|drain|list)"
    ;;
esac
