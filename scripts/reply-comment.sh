#!/bin/bash
# reply-comment.sh -- post or reply to a PR comment
#
# Usage:
#   reply-comment.sh <pr> <body>                                 -- top-level PR comment
#   reply-comment.sh <pr> <comment-id> <body>                    -- reply to an inline review thread
#   reply-comment.sh <pr> --file <path> --line <n> [--side RIGHT|LEFT] <body>
#                                                                -- new inline review comment on <file>:<line>
#                                                                   against the PR's HEAD commit. Use this for
#                                                                   CodeRabbit outside-diff (review-body)
#                                                                   findings that have no threadable comment ID.
#                                                                   PREFER this (anchored to the code) over a
#                                                                   top-level comment when acking findings: CR
#                                                                   engages best when chat is initiated on the
#                                                                   code changes. If GitHub 422s on a line
#                                                                   outside the PR diff, fall back to the
#                                                                   top-level "<pr> <body>" form with --review.
#
# Any form also accepts --review <id>, which stamps a reference to that
# CodeRabbit review id into the posted body. pr-unreplied-comments.sh treats a
# pure outside-diff finding as addressed only when a later comment references
# its review id, so pass --review when acking such a finding via a top-level
# comment to make the ack detectable.
set -euo pipefail

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

usage() {
  cat >&2 <<EOF
Usage:
  $0 <pr> <body>
      -- top-level PR comment
  $0 <pr> <comment-id> <body>
      -- reply to inline review thread
  $0 <pr> --file <path> --line <n> [--side RIGHT|LEFT] <body>
      -- new inline review comment on <file>:<line> against PR HEAD
         (use this for CodeRabbit outside-diff findings that cannot
         accept a threaded reply)

  Any form also accepts: --review <id>
      -- stamp a reference to CodeRabbit review <id> into the body so
         pr-unreplied-comments.sh can detect the ack of an outside-diff finding
EOF
  exit 1
}

if [ "${#}" -lt 2 ]; then
  usage
fi

if ! command -v gh &>/dev/null; then
  echo "Error: gh (GitHub CLI) is required but not installed." >&2
  exit 1
fi

repo=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null) || {
  echo "Error: could not determine repository. Run from inside a git repo with a GitHub remote." >&2
  exit 1
}
pr="$1"
shift

# Scan for --file/--line flags; keep non-flag args as positionals.
file=""
line=""
side="RIGHT"
review_id=""
positional=()
while [ "${#}" -gt 0 ]; do
  case "$1" in
    --file)
      [ "${#}" -ge 2 ] || { echo "Error: --file needs a path" >&2; usage; }
      file="$2"; shift 2;;
    --line)
      [ "${#}" -ge 2 ] || { echo "Error: --line needs a number" >&2; usage; }
      line="$2"; shift 2;;
    --side)
      [ "${#}" -ge 2 ] || { echo "Error: --side needs RIGHT or LEFT" >&2; usage; }
      side="$2"; shift 2;;
    --review)
      # Stamp a machine-detectable reference to a CodeRabbit review id so that
      # pr-unreplied-comments.sh recognizes this comment as an ack of that
      # review's outside-diff finding (it greps the id in $me's later comments).
      [ "${#}" -ge 2 ] || { echo "Error: --review needs a review id" >&2; usage; }
      review_id="$2"; shift 2;;
    --)
      shift; while [ "${#}" -gt 0 ]; do positional+=("$1"); shift; done;;
    *)
      positional+=("$1"); shift;;
  esac
done

# Append the review reference marker (when --review was given) to whatever body
# the form below posts. The id in this marker is what pr-unreplied-comments.sh
# matches to clear a pure outside-diff finding.
review_suffix=""
if [ -n "$review_id" ]; then
  # Mention @coderabbitai (possessive form) so the bot engages with the ack, but
  # avoid the bare "@coderabbitai review" adjacency, which is CR's command syntax
  # to trigger a NEW review. The review id stays in the text so
  # pr-unreplied-comments.sh detects the ack and clears the outside-diff finding.
  review_suffix=$'\n\n'"_(Addressing @coderabbitai's review ${review_id}.)_"
fi

if [ -n "$file" ] || [ -n "$line" ]; then
  # Inline-on-file-line form: both --file and --line required, plus exactly one body.
  if [ -z "$file" ] || [ -z "$line" ]; then
    echo "Error: --file and --line must be used together" >&2
    usage
  fi
  if [ "${#positional[@]}" -ne 1 ]; then
    echo "Error: inline-on-file-line form takes exactly one body argument" >&2
    usage
  fi
  body="${positional[0]}"
  case "$side" in
    RIGHT|LEFT) ;;
    *) echo "Error: --side must be RIGHT or LEFT (got: $side)" >&2; usage;;
  esac

  # Resolve PR HEAD SHA. The comments API rejects abbreviated SHAs and any
  # commit that isn't part of the PR's commit list, so use the branch tip
  # as GitHub currently sees it.
  head_sha=$(gh pr view "$pr" --json headRefOid -q .headRefOid 2>/dev/null) || {
    echo "Error: could not resolve HEAD SHA for PR #$pr" >&2
    exit 1
  }

  gh api "repos/$repo/pulls/$pr/comments" -X POST \
    -f body="${body}${review_suffix}" \
    -f commit_id="$head_sha" \
    -f path="$file" \
    -F line="$line" \
    -f side="$side" \
    --silent
  echo "Posted inline comment on $file:$line (PR #$pr, HEAD $head_sha)"
  exit 0
fi

# No --file/--line: the two-positional-only forms.
case "${#positional[@]}" in
  1)
    # Top-level PR comment
    body="${positional[0]}"
    gh api "repos/$repo/issues/$pr/comments" -f body="${body}${review_suffix}" --silent
    echo "Posted comment on PR #$pr"
    ;;
  2)
    # Reply to inline review thread
    comment_id="${positional[0]}"
    body="${positional[1]}"
    gh api "repos/$repo/pulls/$pr/comments/$comment_id/replies" -f body="${body}${review_suffix}" --silent
    echo "Replied to comment $comment_id on PR #$pr"
    ;;
  *)
    usage
    ;;
esac
