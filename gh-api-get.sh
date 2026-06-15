#!/usr/bin/env bash
# gh-api-get.sh (issue #24, P3-F): a READ-ONLY `gh api` passthrough.
#
# Refuses ANY mutation flag, then passes through to `gh api "$@"`. This replaces broad
# `Bash(gh api *)` allow-listing with a deterministic wrapper (least privilege): a bot's
# gh-api access can no longer mutate anything - including the merge-by-API surface
# (`gh api -X PUT .../pulls/N/merge`), which requires a refused flag.
#
# Note: in `gh api`, any field flag (-f/-F/--field/--raw-field/--input) WITHOUT --method
# defaults to POST (a mutation), and -X/--method sets the verb - so refusing all of them
# leaves only true GETs. A GET that needs query params passes them in the URL path
# (e.g. `gh-api-get.sh "repos/o/r/x?state=open"`), not via -f.
#
# Canonical source: cc-orchestrator repo root; deployed by symlink into ~/.claude/scripts/.
set -euo pipefail

for arg in "$@"; do
  case "$arg" in
    -X*|--method|--method=*|-f*|--raw-field|--raw-field=*|-F*|--field|--field=*|--input|--input=*)
      printf 'gh-api-get: refusing mutation flag %q (this wrapper is GET-only; use a dedicated wrapper for a mutation)\n' "$arg" >&2
      exit 2
      ;;
  esac
done

exec gh api "$@"
