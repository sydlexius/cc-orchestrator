#!/bin/bash
# resolve-threads.sh -- Resolve bot review threads on a PR via GraphQL
#
# Usage:
#   resolve-threads.sh [--bot <pattern>] <pr> <comment-db-id...>
#
# Resolves the review threads whose first comment matches one of the given
# comment database IDs. The optional --bot flag restricts resolution to
# threads whose first comment's author.login matches the given case-insensitive
# regex (default: "copilot|greptile|codoki" -- the bots that lack a slash-command
# resolve). CodeRabbit threads are NOT in the default pattern because they
# resolve via "@coderabbitai resolve", not GraphQL; pass --bot coderabbit if
# you specifically need to force-resolve a CR thread via this path.
#
# Examples:
#   # Default: copilot OR greptile OR codoki (the GraphQL-resolve bots):
#   bash resolve-threads.sh 1695 9876543 8765432
#
#   # Copilot only:
#   bash resolve-threads.sh --bot copilot 851 1234567 2345678
#
#   # Any bot (escape hatch):
#   bash resolve-threads.sh --bot 'bot' 1695 1111 2222
#
# Prints "Resolved <thread-id> (comment <db-id>)" per thread, or
# "Skipped <db-id> (already resolved, not found, or author mismatch)"
# if nothing matched.
set -euo pipefail

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

bot_pattern='copilot|greptile|codoki'
if [ "${1:-}" = "--bot" ]; then
  if [ -z "${2:-}" ]; then
    echo "Error: --bot requires a regex pattern argument."
    exit 1
  fi
  bot_pattern="$2"
  shift 2
fi

if [ "${#}" -lt 2 ]; then
  echo "Usage: $0 [--bot <pattern>] <pr> <comment-db-id...>"
  exit 1
fi

if ! command -v gh &>/dev/null; then
  echo "Error: gh (GitHub CLI) is required but not installed."
  exit 1
fi

pr="$1"
shift
repo=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null) || {
  echo "Error: could not determine repository. Run from inside a git repo with a GitHub remote."
  exit 1
}
owner="${repo%%/*}"
name="${repo##*/}"

# Fetch all review threads with first-comment metadata
threads=$(gh api graphql -f query="
{
  repository(owner: \"$owner\", name: \"$name\") {
    pullRequest(number: $pr) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 1) {
            nodes {
              databaseId
              author { login }
            }
          }
        }
      }
    }
  }
}" --jq '.data.repository.pullRequest.reviewThreads.nodes')

for db_id in "$@"; do
  thread=$(echo "$threads" | jq --argjson id "$db_id" --arg pat "$bot_pattern" \
    '[.[] | select(
      .comments.nodes[0].databaseId == $id and
      (.comments.nodes[0].author.login | test($pat; "i"))
    )] | first // empty')

  if [ -z "$thread" ]; then
    echo "Skipped $db_id (not found or author does not match /$bot_pattern/i)"
    continue
  fi

  is_resolved=$(echo "$thread" | jq -r '.isResolved')
  if [ "$is_resolved" = "true" ]; then
    echo "Skipped $db_id (already resolved)"
    continue
  fi

  thread_id=$(echo "$thread" | jq -r '.id')
  gh api graphql -f query="
mutation {
  resolveReviewThread(input: { threadId: \"$thread_id\" }) {
    thread { isResolved }
  }
}" --jq '.data.resolveReviewThread.thread.isResolved' > /dev/null

  echo "Resolved thread $thread_id (comment $db_id)"
done
