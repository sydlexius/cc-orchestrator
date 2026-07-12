---
description: "Capture a deferred process/tooling idea to the feedback maildir - do NOT implement it now (add | list)"
argument-hint: "add <slug>  |  list"
allowed-tools: ["Bash"]
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

FIRST write the entry body to a temp file with the FILE-EDIT tool (what the friction was, what you
would change, and where - per SKILL.md's HOW TO WRITE THE LOG). THEN pipe it in via a STDIN REDIRECT.
Substitute your own slug and body path:

```bash
FB=""
if [ -f scripts/orchestrate-feedback.sh ]; then FB=scripts/orchestrate-feedback.sh
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-feedback.sh" ]; then
  FB="${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-feedback.sh"
fi
[ -n "$FB" ] || { echo "orchestrate-feedback.sh not found (repo-local or plugin)" >&2; exit 2; }

bash "$FB" add my-slug < /tmp/fb-body.md
```

Resolve the helper in the SAME Bash call that uses it - each tool call is a fresh shell, so `$FB`
does not survive across calls.

The STDIN redirect is load-bearing, not style. NEVER pass the body as a positional argument, and
NEVER use a `cat >> ... <<EOF` heredoc. The Bash guard hook inspects COMMAND LINES, so an entry whose
prose mentions a push or a merge trips the guard when it rides on the command line (it once blocked
the very entry documenting that block). A `< file` redirect keeps the prose off the command line
entirely; the helper reads its body from stdin precisely for this reason.

The helper prints the created filename.

---

## Step 2 -- `list` (read-only)

```bash
FB=""
if [ -f scripts/orchestrate-feedback.sh ]; then FB=scripts/orchestrate-feedback.sh
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-feedback.sh" ]; then
  FB="${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-feedback.sh"
fi
[ -n "$FB" ] || { echo "orchestrate-feedback.sh not found (repo-local or plugin)" >&2; exit 2; }

bash "$FB" list
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
