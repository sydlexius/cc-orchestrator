---
description: "Create a GitHub issue from the correct template with all required sections filled"
argument-hint: "<type> <title> (type: feature | bug | task)"
allowed-tools: ["Bash", "Read", "Write"]
---

# Create GitHub Issue

Create a new GitHub issue using the project's issue templates, following the
CLAUDE.md protocol for issue creation.

**Arguments:** $ARGUMENTS

---

## Step 1 -- Parse arguments

Extract the issue type (first word) and title (remainder) from $ARGUMENTS.

Valid types: `feature`, `bug`, `task`

If the type is missing or invalid, ask: "What type of issue? (feature / bug / task)"
If the title is missing, ask: "What is the issue title?"

---

## Step 2 -- Read template

Read the corresponding template:
- feature: `.github/ISSUE_TEMPLATE/feature.md`
- bug: `.github/ISSUE_TEMPLATE/bug.md`
- task: `.github/ISSUE_TEMPLATE/task.md`

---

## Step 3 -- Fill sections interactively

Present the agent hint defaults for the issue type and ask if they are OK:
- feature: `[mode: plan] [model: sonnet] [effort: medium]`
- bug: `[mode: direct] [model: sonnet] [effort: medium]`
- task: `[mode: direct] [model: haiku] [effort: low]`

Then for each content section in the template, ask the user to provide input.
If the user gives a brief phrase, expand it into a well-structured section.

Also ask here: **"Assign to a milestone? (enter milestone title or skip)"**. It is one more
field, and asking BEFORE creation is load-bearing, not tidiness (#344): a repo may REQUIRE a
milestone at creation time (stillwater enforces exactly this with a local hookify rule that
blocks any issue-creation command not already carrying `--milestone`). Asking afterward means
the create is DENIED and the skill's own prescribed command is the thing that gets rejected -
hit twice while filing a real issue on 2026-07-27. Carry the answer into Step 5's `--milestone`
flag; on "skip", omit the flag entirely.

List the available milestones rather than making the user recall a title:

```bash
gh api "repos/{owner}/{repo}/milestones" --jq '.[].title' 2>/dev/null || true
```

The `|| true` matters: a repo with milestones DISABLED, or a token without the scope, must
degrade to "no milestones offered" and continue, never abort the issue-filing flow.

---

## Step 4 -- Write body file

Write the fully populated template to `/tmp/gh-issue-body.md`.

---

## Step 4b -- Advisory prose-lint (never blocks)

Run the drafted body through the shared prose-lint helper so the issue text gets
the same grammar/style checking as committed Markdown. This is **advisory** -- it
prints findings but never blocks issue creation.

```bash
# Literal helper path in every leg (the "Helper exec paths" rule in prep-pr.md). prose-lint.sh
# is NOT deployed to ~/.claude/scripts/, so there is no stable leg. Capture the exit code with
# `|| pl_rc=$?` so a non-zero result can NEVER abort the caller under `set -e` -- advisory only.
if [ -f scripts/prose-lint.sh ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/prose-lint.sh' ]; then leg=plugin
else leg=none; fi
pl_rc=0
[ "$leg" = repo ]   && { bash scripts/prose-lint.sh --profile docs --label "(issue-body)" /tmp/gh-issue-body.md || pl_rc=$?; }
[ "$leg" = plugin ] && { bash '${CLAUDE_PLUGIN_ROOT}/scripts/prose-lint.sh' --profile docs --label "(issue-body)" /tmp/gh-issue-body.md || pl_rc=$?; }
[ "$leg" = none ]   && { echo "prose-lint skipped (helper not found; load via /orchestrate:new-issue)"; pl_rc=2; }
echo "pl_rc=$pl_rc"
```

A hook DENYING this block means prose-lint did not run: report it skipped and continue (the
**Hook-denied gate command** rule in `prep-pr.md`; this step is advisory).

Interpret `pl_rc`:
- `0` -- clean or advisory-only. Continue.
- `1` -- a blocking finding was printed. Surface it to the user and offer to fix
  the wording, but do **not** gate: "prose-lint flagged the above; want me to
  revise the body before creating? (revise / create as-is)". Honor either answer.
- `2` -- prose-tooling is not installed or its server is down. Print
  "prose-lint skipped (not configured / server unreachable)" and continue. The
  check is a best-effort nicety, never a hard dependency of `new-issue`.

---

## Step 5 -- Create the issue

Map the type to its label:
- feature: `enhancement`
- bug: `bug`
- task: `chore`

Set `issue_label` to that mapped label first (an unquoted `<label>` is a shell redirection).
Carry the milestone answer from Step 3 into the create itself - do NOT create first and edit
after (#344), which is denied outright in a repo that requires a milestone at creation time:

```bash
# With a milestone (the answer from Step 3):
gh issue create --title "<title>" --body-file /tmp/gh-issue-body.md --label "$issue_label" \
  --milestone "<title-from-step-3>"

# On "skip", omit the flag entirely - do NOT pass an empty --milestone "":
gh issue create --title "<title>" --body-file /tmp/gh-issue-body.md --label "$issue_label"
```

If the create is REJECTED for an unknown milestone (a title typo, or one closed since Step 3
listed it), re-list and re-ask rather than dropping the flag - silently creating the issue
without the milestone is what a required-milestone repo is trying to prevent.

---

## Step 6 -- Cleanup

```bash
rm -f /tmp/gh-issue-body.md
```

Report the issue number and URL.
