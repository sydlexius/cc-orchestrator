---
description: "Capture a deferred process/tooling idea to the feedback maildir - do NOT implement it now (add | list)"
argument-hint: "add <slug>  |  list"
allowed-tools: ["Bash", "Write"]
---

# Capture a deferred idea (feedback maildir)

File a deferred process / tooling / infra idea instead of implementing it inline. If an idea
surfaces mid-run that is NOT the issue you are currently working, it gets CAPTURED here and folded
into the real fix later, through the normal PR/triage process. Recording it IS the deliverable.

This wraps `orchestrate-feedback.sh`. It exposes exactly two subcommands: `add` (write an entry) and
`list` (read-only). `drain` is deliberately NOT wrapped - see Notes.

**Arguments:** $ARGUMENTS

---

## Step 1 -- `add <slug>` (the primary path)

FIRST write the entry body to a temp file with the `Write` tool (what the friction was, what you
would change, and where - per SKILL.md's HOW TO WRITE THE LOG). THEN pipe it in via a STDIN REDIRECT.
`Write` is in `allowed-tools` for exactly this reason: the body MUST reach the helper via a file, so
a command that could only run Bash could not perform its own primary step.
Substitute your own slug and body path:

```bash
if [ -f scripts/orchestrate-feedback.sh ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-feedback.sh' ]; then leg=plugin
elif [ -f ~/.claude/scripts/orchestrate-feedback.sh ]; then leg=stable
else leg=none; fi
fb_rc=2
[ "$leg" = repo ]   && { bash scripts/orchestrate-feedback.sh add my-slug < /tmp/fb-body.md; fb_rc=$?; }
[ "$leg" = plugin ] && { bash '${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-feedback.sh' add my-slug < /tmp/fb-body.md; fb_rc=$?; }
[ "$leg" = stable ] && { bash ~/.claude/scripts/orchestrate-feedback.sh add my-slug < /tmp/fb-body.md; fb_rc=$?; }
[ "$leg" = none ]   && echo "orchestrate-feedback.sh not found (repo-local, plugin, or ~/.claude/scripts/)" >&2
echo "fb_rc=$fb_rc leg=$leg"
(exit "$fb_rc")
```

Detect and run in the SAME Bash call - each tool call is a fresh shell. The helper path is
LITERAL in every leg, never a variable (the "Helper exec paths" rule in `prep-pr.md`: a
PreToolUse safety hook denies a variable exec path, and `${CLAUDE_PLUGIN_ROOT}` is left
unsubstituted when this command loads through a `~/.claude/commands` symlink).

The STDIN redirect is load-bearing, not style. NEVER pass the body as a positional argument, and
NEVER use a `cat >> ... <<EOF` heredoc. The Bash guard hook inspects COMMAND LINES, so an entry whose
prose mentions a push or a merge trips the guard when it rides on the command line (it once blocked
the very entry documenting that block). A `< file` redirect keeps the prose off the command line
entirely; the helper reads its body from stdin precisely for this reason.

The helper prints the created filename.

---

## Step 2 -- `list` (read-only)

```bash
if [ -f scripts/orchestrate-feedback.sh ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-feedback.sh' ]; then leg=plugin
elif [ -f ~/.claude/scripts/orchestrate-feedback.sh ]; then leg=stable
else leg=none; fi
fb_rc=2
[ "$leg" = repo ]   && { bash scripts/orchestrate-feedback.sh list; fb_rc=$?; }
[ "$leg" = plugin ] && { bash '${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-feedback.sh' list; fb_rc=$?; }
[ "$leg" = stable ] && { bash ~/.claude/scripts/orchestrate-feedback.sh list; fb_rc=$?; }
[ "$leg" = none ]   && echo "orchestrate-feedback.sh not found (repo-local, plugin, or ~/.claude/scripts/)" >&2
echo "fb_rc=$fb_rc leg=$leg"
(exit "$fb_rc")
```

Shows the undrained entries in the inbox. Read-only; changes nothing.

---

## Notes

**`drain` is intentionally NOT wrapped, and that is a security property, not an omission.**

Draining is gated by the binding three-step ordering in CLAUDE.md: (1) task a hostile reviewer on the
entry, (2) THEN file the issue, (3) THEN drain the entry against that issue number. The helper script
does not ENFORCE that ordering - its "use only after the 3-step gate" note is a comment, and it will
happily drain against an `--issue N` that does not exist, with no review performed. Exposing a
one-call `/orchestrate:feedback drain` would therefore hand an agent a frictionless bypass of the
gate: privilege escalation dressed as convenience.

To drain, follow the SKILL.md DRAIN PROCEDURE and the CLAUDE.md FEEDBACK-LOG DRAIN GATE, invoking the
script directly once the issue exists. There is deliberately no shortcut here.

**Who writes the log.** The LEAD (and the planner, per its charter). Other teammates surface the
friction TO the lead, who records it - they do not write the log directly.

**Scope.** `add` writes only to the machine-local maildir; `list` reads it. One read-only
`gh repo view` runs to label the entry with its repo, and it fails soft to `unknown`, so an offline
or unauthenticated `gh` degrades rather than breaks. No network MUTATION, no git mutation, no
allow-list broadening, no floor change.
