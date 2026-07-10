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

---

## Step 4 -- Write body file

Write the fully populated template to `/tmp/gh-issue-body.md`.

---

## Step 4b -- Advisory prose-lint (never blocks)

Run the drafted body through the shared prose-lint helper so the issue text gets
the same grammar/style checking as committed Markdown. This is **advisory** -- it
prints findings but never blocks issue creation.

```bash
# Locate the helper without assuming ${CLAUDE_PLUGIN_ROOT} is set (an unset var would expand
# to "/scripts/prose-lint.sh"). Capture the exit code with `|| pl_rc=$?` so a non-zero result
# can NEVER abort the caller under `set -e` -- this check is strictly advisory.
PL=""
if [ -f scripts/prose-lint.sh ]; then
  PL=scripts/prose-lint.sh
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "${CLAUDE_PLUGIN_ROOT}/scripts/prose-lint.sh" ]; then
  PL="${CLAUDE_PLUGIN_ROOT}/scripts/prose-lint.sh"
fi
pl_rc=0
if [ -n "$PL" ]; then
  bash "$PL" --profile docs --label "(issue-body)" /tmp/gh-issue-body.md || pl_rc=$?
else
  echo "prose-lint skipped (helper not found)"; pl_rc=2
fi
```

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

```bash
gh issue create --title "<title>" --body-file /tmp/gh-issue-body.md --label <label>
```

After creation, ask: "Assign to a milestone? (enter milestone title or skip)"

If yes:

```bash
gh issue edit <number> --milestone "<title>"
```

---

## Step 6 -- Cleanup

```bash
rm -f /tmp/gh-issue-body.md
```

Report the issue number and URL.
