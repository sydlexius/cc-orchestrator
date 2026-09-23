---
description: Tag a release — bump version, generate notes, push tag, create GitHub Release
---

## Release Workflow

Read the project release config from `.claude/release.toml` in the repo root. Parse
it as TOML with these sections:

- `[versioning]` — `file` (path to version file), `pattern` (string with `{version}`
  placeholder), `post_bump` (array of shell commands to run after version edit)
- `[build]` — `working_dir`, `pre_checks` (array of shell commands)
- `[release_notes]` — `group_labels` (map of GitHub label → section heading),
  `default_group` (heading for PRs without a matching label)
- `[release]` — `tag_prefix` (e.g. `v`). REQUIRED: if the key is absent, STOP and ask for it
  to be set (an explicit `tag_prefix = ""` is allowed and means unprefixed tags). Steps 5 and 9
  both use it, and a missing key must not silently turn `v1.2.0`-style tags into `1.2.0`.

If the file is missing, stop and ask the user to create it.

### Arguments

The user may provide:
- A version number: `/push-release 0.2.0` — use this exact version
- `--dry-run`: show everything but don't commit, tag, or push
- No args: auto-suggest the next patch version (e.g. 0.1.0 → 0.1.1)

### Steps

1. **Check working tree.** Run `git status --porcelain`. If output is non-empty, refuse:
   "Working tree is dirty. Commit or stash changes first."

1b. **Check the checkout IS `origin/main`.** Before any version bump or range work. A tag is
    irreversible once pushed (some consumers treat a published release tag as immutable, so a
    wrong tag cannot be reissued under the same name), and Step 9 tags local `HEAD` while Step 5
    builds the notes from `origin/main`. Both must be the same commit, so the checkout must be ON
    `main`, not BEHIND `origin/main`, and not AHEAD of it (unpushed local commits would ship in
    the tag without appearing in the notes).

    Locate `base-freshness.sh` with the same repo/plugin/stable/none leg pattern
    `commands/prep-pr.md` Step 1c uses (see "Helper exec paths" at the top of that file). The
    helper fetches `origin/main` itself, so no separate fetch is needed, and the ahead-count
    below is read after that fetch:

    ```bash
    # Literal helper path in every leg -- see "Helper exec paths" in commands/prep-pr.md.
    if [ -f scripts/base-freshness.sh ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
    elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/base-freshness.sh' ]; then leg=plugin
    elif [ -f ~/.claude/scripts/base-freshness.sh ]; then leg=stable
    else leg=none; fi
    out=""
    # `|| true`: the helper exits 1 on behind. Under `set -e` a bare assignment would abort
    # here, before the lines below print, and lose the release-specific STOP message.
    [ "$leg" = repo ]   && out=$(bash scripts/base-freshness.sh main HEAD || true)
    [ "$leg" = plugin ] && out=$(bash '${CLAUDE_PLUGIN_ROOT}/scripts/base-freshness.sh' main HEAD || true)
    [ "$leg" = stable ] && out=$(bash ~/.claude/scripts/base-freshness.sh main HEAD || true)
    [ "$leg" = none ]   && out="freshness: NOT RUN -- base-freshness.sh not found on any leg (repo/plugin/deployed)"
    branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
    ahead=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo "?")
    echo "$out"
    echo "leg=$leg branch=$branch ahead=$ahead head=$(git rev-parse --short HEAD)"
    ```

    **Decide from the printed lines, never the exit code.** The helper exits 0 for both `fresh`
    and `unknown`, a contract tuned for an advisory caller like `/prep-pr`; an irreversible tag
    must not proceed on doubt. Check in this order, and STOP at the first that applies:

    - `branch` is not `main` -> **STOP**: "Releases are cut from `main`; this checkout is on
      `<branch>`. Switch to `main` and re-run `/push-release`."
    - `leg=none` (`freshness: NOT RUN`) -> **STOP**: "base-freshness.sh was not found on any leg
      (repo/plugin/deployed). Update the plugin or re-run `orchestrate-setup.py configure --apply`
      before cutting a release." A check that could not run is never a pass, and without the
      helper's fetch the `ahead` count below is read against a stale `origin/main`, so this rule
      comes first.
    - `ahead` is not `0` (including `?`) -> **STOP**: "`main` has `<ahead>` local commit(s) not on
      `origin/main` (diverged if the freshness line also says behind). They would ship in the tag
      without appearing in the notes. Reconcile by hand -- push them through a PR, or drop them if
      unwanted -- then re-run." Do not suggest a fast-forward here: it fails on a diverged branch.
    - Line starts `freshness: unknown` -> **STOP**, quoting the helper's own reason (unreachable
      origin, shallow clone, unresolvable ref). This deliberately diverges from `/prep-pr`'s
      "never block on unknown": for an irreversible tag, doubt blocks.
    - Line starts `freshness: behind` (with `ahead` = 0, so strictly behind) -> **STOP**:
      > "This checkout is `<N>` commits behind `origin/main`. Cutting the release now would tag
      > `<head>`, not the latest `main`. Fast-forward first (`git pull --ff-only`), then re-run
      > `/push-release`."
      Never echo the helper's `git merge` / `gh pr update-branch` remedy (that phrasing is for an
      open PR, not a release checkout), and never fast-forward automatically.
    - Line starts `freshness: fresh` -> proceed to Step 2.

2. **Read current version.** Open the `versioning.file`, find the line matching
   `versioning.pattern` (with `{version}` as a capture group), extract the current
   version string.

3. **Determine target version.** If the user provided a version arg, use it. Otherwise,
   parse current as semver and bump the patch number. Show: "Current: {current} →
   Next: {next}". Ask user to confirm or provide a different version.

4. **Run pre-checks.** Execute each command in `build.pre_checks` sequentially from
   `build.working_dir`. If any fails, stop and show the error. Do NOT skip pre-checks.

5. **Gather merged PRs.** Derive the set from the COMMIT RANGE, not from a date.

   ```sh
   # The range starts at the previous RELEASE tag, found in three filters (#419):
   # 1. --merged origin/main: a tag NOT reachable from the release branch (an experimental or
   #    backport tag, which sorts by date like any other) is never a range start.
   # 2. tag_prefix: only this project's own release tags. Without it a nightly/CI tag on main
   #    outranks the last release (measured on stillwater: the range started at a same-day
   #    nightly tag and held 6 commits where the true range held 36).
   # 3. The part AFTER the prefix must start with a digit, so a non-release tag that merely
   #    shares the prefix (`verified`, `vendor-sync` under "v") never wins. For a STABLE
   #    target it must also carry no '-', so stable notes range from the last STABLE release
   #    rather than the last rc (stillwater carries v1.6.0-rc1..rc13, which all match "v").
   # The prefix is compared and stripped by LENGTH in awk, never as a pattern, so a prefix
   # holding '-' ("release-") or a glob character cannot distort the test.
   # Fill both in as SINGLE-QUOTED literals (config is data, never shell source: inside double
   # quotes a `$(...)` in the prefix would execute). A value containing `'` -> STOP.
   tag_prefix='{tag_prefix}'      # from [release] in .claude/release.toml; may be empty
   target_version='{version}'     # the version determined in Step 3
   case "$tag_prefix" in *[!A-Za-z0-9._/+-]*) echo "STOP: tag_prefix '$tag_prefix' has characters outside [A-Za-z0-9._/+-]"; exit 1 ;; esac
   case "$target_version" in *-*) stable=0 ;; *) stable=1 ;; esac
   last_tag=$(git tag --list "${tag_prefix}*" --sort=-creatordate --merged origin/main \
     | awk -v p="$tag_prefix" -v stable="$stable" '
         substr($0, 1, length(p)) != p { next }
         { s = substr($0, length(p) + 1) }
         s !~ /^[0-9]/ { next }
         stable && s ~ /-/ { next }
         { print; exit }')
   # empty last_tag -> first release of this prefix/stability class -> whole history
   range="${last_tag:+$last_tag..}origin/main"
   # Print the range start and size NOW, in this same shell: a later fenced block runs in a
   # fresh shell where $last_tag and $range no longer exist.
   if [ -n "$last_tag" ]; then
     echo "range: $range (starts at $last_tag, tagged $(git log -1 --format=%ci "$last_tag"))"
   else
     echo "range: $range (no prior release tag: whole history)"
   fi
   echo "commits in range: $(git log "$range" --oneline | wc -l | tr -d ' ')"
   # The PR number is the "(#N)" trailer GitHub appends on squash-merge.
   # `|| true`: grep exits 1 on NO MATCH, and an empty PR set is a VALID outcome (a
   # first release, direct pushes, non-squash history). Without it a caller running
   # `set -o pipefail` aborts on exactly the case this line is meant to handle --
   # verified: the assignment fails rc=1 under pipefail and never reaches the next line.
   prs=$(git log "$range" --oneline | grep -oE '\(#[0-9]+\)$' | tr -d '(#)' || true)
   # One PR per line, read line by line: `for n in $prs` does NOT word-split in zsh (the shell
   # these blocks run in), so it would pass every number to one `gh pr view` call.
   printf '%s\n' "$prs" | while IFS= read -r n; do
     [ -n "$n" ] && gh pr view "$n" --json number,title,labels,closingIssuesReferences
   done
   ```

   **Why not a date search.** The obvious form -- `gh pr list --search "merged:>={tag date}"`
   -- OVER-COLLECTS, and it does so silently. `merged:>=` takes a bare `YYYY-MM-DD`, so it
   returns everything merged on the tag date INCLUDING what the previous tag already
   shipped. Measured on v0.94.2: the date search returned 2 PRs where the range contained
   1, because the other had shipped in v0.94.1 earlier the same day. An earlier run of the
   same bug returned 15 PRs for a 4-PR release. A tag is a COMMIT, so ask git for the
   commits; only the range knows where the last release actually stopped.

   **Sanity-check the result** before writing notes, from the `range:` and `commits in range:`
   lines the block printed: the commit count should be within one or two of the PR count
   (merge commits and direct pushes explain any gap). A PR count far above the commit count
   means the range is wrong -- stop and re-derive rather than shipping notes that credit
   another release's work. This check only catches OVER-collection: a range that starts too
   late (the #419 nightly-tag case, where 6/6 agreed and the true range held 36 commits)
   passes it silently, so also confirm the printed start tag is the previous RELEASE (compare
   it with `gh release list --limit 3`), not merely the most recent tag of any kind. If the
   block printed `no prior release tag` but `gh release list` shows earlier releases, **STOP**:
   `tag_prefix` does not match this project's tags (e.g. unset while the tags are `v1.2.0`),
   and continuing would credit the whole history to this release.

   The `closingIssuesReferences` field is what makes step 6's issue-preferred linking
   possible without parsing PR bodies.

6. **Generate release notes.** Group PRs by label using `release_notes.group_labels`.
   PRs without a matching label go under `release_notes.default_group`. Rewrite each
   PR title into plain, user-friendly language (drop prefixes like "feat:", "fix:",
   etc.).

   **Prefer issue numbers over PR numbers in the trailing reference.** Issues
   document the WHY (motivation, user report, design discussion, screenshots);
   PRs document the WHAT (the diff, code review conversation, fix-up commits).
   For a reader following a release-notes link, the issue is the better landing
   page — and the issue links back to its closing PR anyway. The PR's
   `closingIssuesReferences[].number` (already in the JSON from step 5) is the
   authoritative source — do not parse `Closes #N` from PR bodies.

   Fallback rules:

   - PR closes exactly one issue → use `(#<issue>)`
   - PR closes multiple issues → list them all: `(#<issue-a>, #<issue-b>)`
   - PR closes no issue (Dependabot bumps, direct CR fixes, trivial chores
     where filing an issue would be ceremony) → use the PR number itself:
     `(#<pr>)`. These cases are legitimate — not every change needs an
     issue, but every change does need a discoverable anchor.

   Style: lead with the *user-visible change*, not the implementation noun.
   "Native HTTPS without a reverse proxy" beats "Add TLS listener helper."
   Group related items into a short intro paragraph per section when the
   bullets share a theme; the previous release's notes are the reference for
   tone and structure (`gh release view <prev-tag>`).

   Format as markdown:

   ```markdown
   ## New Features
   - Description of feature (#123)                     <- issue # (PR closes issue 123)
   - Description spanning two issues (#124, #125)

   ## Bug Fixes
   - Description of fix (#456)
   - Dependabot bump for X (#1438)                     <- PR # (no closing issue)
   ```

7. **Show for approval.** Display the version and release notes. Ask: "Create release
   {tag_prefix}{version} with these notes? [y/N]". If dry-run, show everything and stop here
   with: "Dry run complete. No changes made."

8. **Bump version.** In `versioning.file`, replace the matched line using
   `versioning.pattern` with `{version}` replaced by the target version. Run each
   command in `versioning.post_bump` sequentially.

9. **Commit and tag.** The tag name is `{tag_prefix}{version}`, the SAME prefix Step 5
   searches for. A hardcoded `v` here would make a non-`v` project's next release find no
   prior tag and range over the whole history (#419).

   `tag_prefix` comes from repository config, so the tag name is built ONCE as data, validated,
   and then passed as a single quoted variable -- never pasted into the command text, where a
   `;` or `$(...)` in the prefix would run as shell. Fill in both values as SINGLE-QUOTED
   literals; if either contains a `'`, STOP (it cannot be a valid tag anyway):

   ```bash
   tag_prefix='{tag_prefix}'; version='{version}'
   tag="${tag_prefix}${version}"
   case "$tag" in
     *[!A-Za-z0-9._/+-]*|'') echo "STOP: tag name '$tag' has characters outside [A-Za-z0-9._/+-]"; exit 1 ;;
   esac
   git check-ref-format "refs/tags/$tag" || { echo "STOP: '$tag' is not a valid git tag name"; exit 1; }
   git add -A
   git commit -m "release: $tag"
   git tag -s "$tag" -m "$tag"
   echo "tag=$tag"
   ```

10. **Push** the release commit, then ONLY this release's tag (never `--tags`, which would
    also publish any stray local tag such as a nightly or test tag). Re-set `tag` the same
    way; this block runs in a fresh shell:

    ```bash
    tag='<the tag= value Step 9 printed>'
    git push && git push origin "refs/tags/$tag"
    ```

11. **Monitor.** Show the release workflow status and give the user the URL to watch:

    ```bash
    tag='<the tag= value Step 9 printed>'
    gh run list --branch "$tag" --limit 1
    ```

### Important

- NEVER use `--admin` or `--force` flags on any git or gh command.
- Run the project's configured formatter before committing (subagent commits bypass pre-commit hooks).
- If any step fails, stop and report the error. Do not continue.
