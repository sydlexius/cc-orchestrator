#!/usr/bin/env bash
# gh-comment.sh <subcommand> ...  (issue #24, P3-F phase 2)
#
# Dedicated wrapper for PR/issue COMMENT mutations only. Subcommands:
#   post     <pr> <body>                                  -> POST issues/<pr>/comments (top-level)
#   reply    <pr> <comment-id> <body>                     -> POST pulls/<pr>/comments/<id>/replies
#   inline   <pr> --file <path> --line <n> [--side S] <body>
#                                                         -> POST pulls/<pr>/comments (review comment)
#   trigger-cr <pr>                                       -> POST issues/<pr>/comments "@coderabbitai review"
#
# Construction guarantee: every endpoint is built ONLY from validated numerics (pr, comment-id,
# line) and fixed literals; the BODY (and file path) are passed as DATA via -f/-F fields and are
# NEVER interpolated into the endpoint string or the method. No caller-supplied -X/--method is
# accepted; each subcommand pins POST. So no input can reach a /merge, --admin, or arbitrary
# endpoint. Repo and (for inline) the HEAD commit sha are resolved internally.
#
# Canonical source: cc-orchestrator repo root; deployed by symlink into ~/.claude/scripts/.
set -euo pipefail

die() { echo "gh-comment: $1" >&2; exit 2; }

# Whole-string numeric check: a bash `case` glob rejects any non-digit, INCLUDING an
# embedded newline (a line-oriented `grep -Eq '^[0-9]+$'` matches per line, so a value
# like $'7\n../../pulls/1/merge' would pass on its first line). Empty also rejected.
is_num() { case "$1" in (*[!0-9]*|'') return 1 ;; (*) return 0 ;; esac; }

# Validate a repo value (owner/name) as a whole string via a bash `case` glob (no external
# tool): exactly one slash, each side a strict charset, no path-traversal/metachars/newline.
# Rejects empty, any out-of-charset char (newline included), more than one slash (*/*/*), a
# leading/trailing slash, and any '..' segment. grep-INDEPENDENT (BSD grep's -z/^...$ cannot
# be trusted to anchor the whole string). Prevents an unvalidated repo arg (e.g.
# o/r/../../../pulls/1) from retargeting the endpoint.
validate_repo() {
  case "$1" in
    (''|*[!A-Za-z0-9._/-]*|*/*/*|/*|*/|*..*)
      die "repo must be owner/name ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+); got: '${1}'"
      ;;
  esac
}

resolve_repo() {
  local r="${GITHUB_REPOSITORY:-}"
  if [ -z "$r" ]; then
    r="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
  fi
  [ -n "$r" ] || die "no repo (set GITHUB_REPOSITORY=owner/name or run in a gh-resolvable repo)"
  validate_repo "$r"
  printf '%s' "$r"
}

resolve_head_sha() {
  local sha
  sha="$(git rev-parse HEAD 2>/dev/null || true)"
  [ -n "$sha" ] || die "could not resolve HEAD commit sha (run inside the PR's checked-out worktree)"
  printf '%s' "$sha"
}

sub="${1:-}"
[ -n "$sub" ] || die "usage: gh-comment.sh <post|reply|inline|trigger-cr> ..."
shift

case "$sub" in
  post)
    pr="${1:-}"; body="${2:-}"
    is_num "$pr" || die "post: pr must be numeric (got: ${pr})"
    [ -n "$body" ] || die "post: body is required"
    repo="$(resolve_repo)"
    exec gh api -X POST "repos/${repo}/issues/${pr}/comments" -f "body=${body}"
    ;;

  reply)
    pr="${1:-}"; cid="${2:-}"; body="${3:-}"
    is_num "$pr" || die "reply: pr must be numeric (got: ${pr})"
    is_num "$cid" || die "reply: comment-id must be numeric (got: ${cid})"
    [ -n "$body" ] || die "reply: body is required"
    repo="$(resolve_repo)"
    exec gh api -X POST "repos/${repo}/pulls/${pr}/comments/${cid}/replies" -f "body=${body}"
    ;;

  trigger-cr)
    pr="${1:-}"
    is_num "$pr" || die "trigger-cr: pr must be numeric (got: ${pr})"
    repo="$(resolve_repo)"
    exec gh api -X POST "repos/${repo}/issues/${pr}/comments" -f "body=@coderabbitai review"
    ;;

  inline)
    pr="${1:-}"
    is_num "$pr" || die "inline: pr must be numeric (got: ${pr})"
    shift
    file=""; line=""; side="RIGHT"; body=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --file) file="${2:-}"; shift 2 ;;
        --file=*) file="${1#--file=}"; shift ;;
        --line) line="${2:-}"; shift 2 ;;
        --line=*) line="${1#--line=}"; shift ;;
        --side) side="${2:-}"; shift 2 ;;
        --side=*) side="${1#--side=}"; shift ;;
        --) shift; body="${1:-}"; shift || true; break ;;
        -*) die "inline: unknown flag ${1} (this wrapper accepts only --file/--line/--side)" ;;
        *) body="$1"; shift ;;
      esac
    done
    [ -n "$file" ] || die "inline: --file <path> is required"
    is_num "$line" || die "inline: --line must be numeric (got: ${line})"
    case "$side" in
      RIGHT|LEFT) ;;
      *) die "inline: --side must be RIGHT or LEFT (got: ${side})" ;;
    esac
    [ -n "$body" ] || die "inline: body is required"
    repo="$(resolve_repo)"
    sha="$(resolve_head_sha)"
    # body/path/side/line are DATA (-f/-F fields); the endpoint is fixed + numeric pr only.
    exec gh api -X POST "repos/${repo}/pulls/${pr}/comments" \
      -f "body=${body}" -f "commit_id=${sha}" -f "path=${file}" \
      -F "line=${line}" -f "side=${side}"
    ;;

  *)
    die "unknown subcommand '${sub}' (use post|reply|inline|trigger-cr)"
    ;;
esac
