---
description: "Preview and apply orchestrate-setup configure (floor hook, helpers, agents), then run doctor"
argument-hint: "[--apply] [--yes] [--no-steer] [--no-ctxmeter]"
allowed-tools: ["Bash"]
---

# Configure the orchestrate floor and helpers

Front for `orchestrate-setup.py configure`: it resolves the script from the right place (so a stale
plugin-cache version is never picked by hand), shows the preview, applies only on an explicit yes,
then runs `doctor`. This command never edits `settings.json` itself - only `configure` does - adds
no allow-list entry, and no argument can skip the guard (configure always wires it).

**Arguments:** $ARGUMENTS

---

## Step 1 -- Validate arguments

The ONLY accepted words in `$ARGUMENTS` are `--apply`, `--yes`, `--no-steer`, `--no-ctxmeter`. Match
each whitespace-separated word EXACTLY and case-sensitively against those four literals. Anything
else is unknown - `--apply=1`, `--APPLY`, `-y`, `--no-steer;rm`, a quoted or prefixed word, a
positional word, a `--no-guard`-style guess - so say which word was rejected, list the accepted
flags, and STOP. Do not run anything. An empty `$ARGUMENTS` is valid.

Build `FLAGS` yourself by typing ONLY the literal names `--no-steer` and/or `--no-ctxmeter` for the
ones present (empty otherwise). Never paste any user text into a command; `--apply` and `--yes`
never go into `FLAGS` (Step 4 passes them itself).

- Every run: Step 3 shows the preview, then asks ONE question; Step 4 runs only on an explicit yes.
  A typed `--apply` (with or without `--yes`) does NOT skip the question: it only says the user
  expects to apply, and the answer to the question after the preview is still the gate (#504).
- `--yes` without `--apply` is rejected: it has no meaning on its own. With `--apply` it is redundant
  and harmless.

---

## Step 2 -- Locate the script

```bash
if [ -f scripts/orchestrate-setup.py ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then echo "configure: leg=repo"
elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-setup.py' ]; then echo "configure: leg=plugin"
else echo "configure: leg=none -- orchestrate-setup.py not found (load via /orchestrate:configure, or reinstall/update the plugin)"; fi
```

If `leg=none`, stop. Every block below re-runs the same `[ -f ]` detection and then runs the ONE
matching literal path (each Bash call is a fresh shell; a helper path is never run through a
variable - the "Helper exec paths" rule in `prep-pr.md`).

There is deliberately no deployed-copy leg (`~/.claude/scripts/orchestrate-setup.py`): that copy
resolves its bundle relative to its own location, so run from there it would deploy nothing and skip
the allow-list and agent steps, while still reporting "nothing to change". A deploy must run from the
plugin (or this repo).

---

## Step 3 -- Preview (always first)

Substitute `FLAGS` for `<FLAGS>`:

```bash
if [ -f scripts/orchestrate-setup.py ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-setup.py' ]; then leg=plugin
else leg=none; fi
rc=2
[ "$leg" = repo ]   && { python3 scripts/orchestrate-setup.py configure <FLAGS>; rc=$?; }
[ "$leg" = plugin ] && { python3 '${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-setup.py' configure <FLAGS>; rc=$?; }
[ "$leg" = none ]   && echo "orchestrate-setup.py not found (load via /orchestrate:configure)"
echo "preview rc=$rc leg=$leg"
(exit "$rc")
```

Show the user the full preview output, whatever `rc` is. Then decide:

- `rc` 2 or higher, or a Python traceback, or `leg=none`: a real error. Stop; nothing to apply.
- `rc` 1: NOT fatal by itself. A dry run returns 1 when it found something that needs attention,
  most often a blanket `gh pr` allow-rule shadowing the merge gate that `--apply` would narrow
  (`_narrow_merge_gate_shadows`). If the output shows a change `--apply` would make, continue to
  the question. If it says the remaining item needs a HUMAN (a broader `gh *`/`*` shadow, or a file
  that could not be scanned), say so plainly: applying will not fix that item.
- `rc` 0 AND configure reports everything already matches (its "already has the floor hook ...
  match the bundled plugin copies" line) AND no shadow was reported: say so and stop.

Then ask exactly ONE question: "Apply these changes?" (even when `--apply` was passed). The Bash
tool has no tty, so configure's own y/N cannot be answered; the user's reply here is the only gate.
Proceed to Step 4 ONLY on an explicit yes in this session. Anything else: stop, nothing applied.

---

## Step 4 -- Apply (only after an explicit yes to the Step 3 question)

Substitute `FLAGS` for `<FLAGS>` (only `--no-steer` / `--no-ctxmeter`; `--apply --yes` is already in
the command):

```bash
if [ -f scripts/orchestrate-setup.py ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-setup.py' ]; then leg=plugin
else leg=none; fi
rc=2
[ "$leg" = repo ]   && { python3 scripts/orchestrate-setup.py configure --apply --yes <FLAGS>; rc=$?; }
[ "$leg" = plugin ] && { python3 '${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-setup.py' configure --apply --yes <FLAGS>; rc=$?; }
[ "$leg" = none ]   && echo "orchestrate-setup.py not found (load via /orchestrate:configure)"
echo "apply rc=$rc leg=$leg"
(exit "$rc")
```

If `rc` is 2 or higher, report configure's output and stop (do not run doctor as if it applied).
`rc` 1 after an apply means an item still needs a human (an unresolved broader shadow, a skipped
or unwritable file): report which, then still run Step 5 so doctor shows the resulting state.

---

## Step 5 -- Doctor (after an apply)

```bash
if [ -f scripts/orchestrate-setup.py ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-setup.py' ]; then leg=plugin
else leg=none; fi
drc=2; out=""
[ "$leg" = repo ]   && { out=$(python3 scripts/orchestrate-setup.py doctor 2>&1); drc=$?; }
[ "$leg" = plugin ] && { out=$(python3 '${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate-setup.py' doctor 2>&1); drc=$?; }
[ "$leg" = none ]   && echo "orchestrate-setup.py not found (load via /orchestrate:configure)"
if [ "$drc" -ne 0 ] && ! printf '%s\n' "$out" | grep -q '^\[FAIL\]'; then
  printf '%s\n' "$out"
else
  printf '%s\n' "$out" | grep -E '^\[(FAIL|WARN)\]'
fi
echo "doctor rc=$drc leg=$leg"
(exit "$drc")
```

Normally only the `[FAIL]` / `[WARN]` lines print. A nonzero rc with no `[FAIL]` line means doctor
itself failed, and the block then prints its FULL output instead; report that as a doctor failure.
Trust "none found" ONLY when `doctor rc=0`. Some WARNs are standing
environment notes (for example tmux or Slack), not apply failures. Do not add a separate
byte-compare: doctor's DIFFER check already covers deployed-vs-bundled drift (#292).

If the apply added or changed a HOOK in `settings.json` (the preview diff shows a `hooks` change),
tell the user to restart the Claude Code session, since that config loads at process start. A
redeployed script alone needs no restart.
