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
- `[release]` — `tag_prefix` (e.g. `v`)

If the file is missing, stop and tell the user to create it.

### Arguments

The user may provide:
- A version number: `/push-release 0.2.0` — use this exact version
- `--dry-run`: show everything but don't commit, tag, or push
- No args: auto-suggest the next patch version (e.g. 0.1.0 → 0.1.1)

### Steps

1. **Check working tree.** Run `git status --porcelain`. If output is non-empty, refuse:
   "Working tree is dirty. Commit or stash changes first."

2. **Read current version.** Open the `versioning.file`, find the line matching
   `versioning.pattern` (with `{version}` as a capture group), extract the current
   version string.

3. **Determine target version.** If the user provided a version arg, use it. Otherwise,
   parse current as semver and bump the patch number. Show: "Current: {current} →
   Next: {next}". Ask user to confirm or provide a different version.

4. **Run pre-checks.** Execute each command in `build.pre_checks` sequentially from
   `build.working_dir`. If any fails, stop and show the error. Do NOT skip pre-checks.

5. **Gather merged PRs.** Find the last tag: `git tag --sort=-creatordate | head -1`.
   If no tags exist, get the first commit: `git rev-list --max-parents=0 HEAD`.
   Then list merged PRs since that point, **including each PR's closing issues**:
   `gh pr list --state merged --base main --search "merged:>={date}" --limit 200 --json number,title,labels,closingIssuesReferences`
   where `{date}` is the tag's date (`git log -1 --format=%aI {tag}`). The
   `closingIssuesReferences` field is what makes step 6's issue-preferred
   linking possible without parsing PR bodies.

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
   ```
   ## New Features
   - Description of feature (#123)                     <- issue # (PR closes issue 123)
   - Description spanning two issues (#124, #125)

   ## Bug Fixes
   - Description of fix (#456)
   - Dependabot bump for X (#1438)                     <- PR # (no closing issue)
   ```

7. **Show for approval.** Display the version and release notes. Ask: "Create release
   v{version} with these notes? [y/N]". If dry-run, show everything and stop here
   with: "Dry run complete. No changes made."

8. **Bump version.** In `versioning.file`, replace the matched line using
   `versioning.pattern` with `{version}` replaced by the target version. Run each
   command in `versioning.post_bump` sequentially.

9. **Commit and tag.**
   ```bash
   git add -A
   git commit -m "release: v{version}"
   git tag -s v{version} -m "v{version}"
   ```

10. **Push.** `git push && git push --tags`

11. **Monitor.** Run `gh run list --branch v{version} --limit 1` to show the release
    workflow status. Provide the URL so the user can watch it.

### Important

- NEVER use `--admin` or `--force` flags on any git or gh command.
- Run the project's configured formatter before committing (subagent commits bypass pre-commit hooks).
- If any step fails, stop and report the error. Do not continue.
