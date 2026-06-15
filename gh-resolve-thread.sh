#!/usr/bin/env bash
# gh-resolve-thread.sh <thread-id>  (issue #24, P3-F)
#
# Dedicated wrapper for the ONE legitimate review mutation: resolveReviewThread.
# Construction guarantee: the GraphQL mutation is FIXED to resolveReviewThread and the
# thread id is validated as a GitHub node id, so this wrapper cannot perform a merge or
# any other mutation regardless of its argument.
#
# Canonical source: cc-orchestrator repo root; deployed by symlink into ~/.claude/scripts/.
set -euo pipefail

thread="${1:-}"
# Whole-string node-id check via a bash `case` glob (no external tool): GitHub node ids are
# [A-Za-z0-9_=-]. Rejects empty and any out-of-charset char, INCLUDING an embedded newline,
# so a value like $'PRRT_ok\n<smuggled>' cannot pass on its benign first line. This is
# grep-INDEPENDENT - a line-oriented match (or BSD grep's `-z`/`^...$`) cannot be trusted to
# anchor the whole string here.
case "$thread" in
  (''|*[!A-Za-z0-9_=-]*)
    echo "gh-resolve-thread: thread id must be a GitHub node id ([A-Za-z0-9_=-]+); got: '${thread}'" >&2
    exit 2
    ;;
esac

# The GraphQL query is a FIXED literal; $id is a GraphQL variable (bound by -F id=...),
# NOT a shell variable - it must stay single-quoted (SC2016 is expected here).
# shellcheck disable=SC2016
exec gh api graphql \
  -f query='mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { id isResolved } } }' \
  -F "id=${thread}"
