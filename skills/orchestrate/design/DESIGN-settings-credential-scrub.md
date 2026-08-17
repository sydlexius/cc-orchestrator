# Design: never persist a credential-shaped value in settings.local.json (#326)

Status: PROPOSED 2026-08-16 — **scrubber now, hook deferred**; the CLAUDE.md tension
(see that section below) needs MAINTAINER RATIFICATION before the actuating half ships.

Scope: a new `scripts/settings-scrub.py` reading and rewriting settings JSON on the LOCAL
FILESYSTEM. No floor change, no guard change, no allow-list broadening, no `gh`, no git or
network mutation. The CLAUDE.md instruction-layer wording fix is tracked on the sibling
policy issue, not here.

**"Local" means local-disk, NOT `settings.local.json` only.** The cascade this reads
(section: Implementation shape) is `~/.claude/settings.json`, `~/.claude/
settings.local.json`, and the project's `.claude/settings{,.local}.json`. Two of those are
frequently GIT-TRACKED, so an actuating write can dirty a tracked file and land in a
commit. That is a materially wider blast radius than the title suggests, and it is part of
what the ratification section below asks the maintainer to approve. **Default the ACTUATING half to
`settings.local.json` files only**; touching a tracked settings file requires a separate
explicit opt-in flag. Detection still reports across the whole cascade — finding a secret
in a tracked file is exactly the case a human most needs told about.

---

## Problem

A credential passed as a command-line argument is a rotation emergency, and the reason is
not the argv exposure. Argv is process-table visible, which is local-user and transient.
What makes it durable is that **`settings.local.json` writes the approved command line
down verbatim and keeps it forever.**

That reframes the problem: it is a PERSISTENCE problem, not a command-matching problem,
and the enforcement point belongs at the boundary where the value gets STORED rather than
the boundary where the command is about to run.

A redacted census across 21 settings files (values correlated by SHA-256 prefix so nothing
had to be printed) found real secrets sitting plaintext on disk at scan time. **Use the
RECONCILED figures below — not the ones in #326's body.**

| | first pass | reconciled (independent re-run) |
| :-- | ---: | ---: |
| allow-rules scanned | 5,355 | **5,358** |
| real (high-entropy) secrets | 15 | **14** |
| env-assignment prefixes | 11 | **10** |
| `Authorization: Bearer` | 2 | 2 |
| `ldapsearch -w` | 2 | 2 |
| long credential flags | 0 | 0 |
| placeholder / `test`-shaped stubs | 3 | **4** (of 18 total matches) |

The re-run is the number to trust. The first pass was **off by one on three counts, each
in the direction that FLATTERED the argument** — a bias worth naming in a doc whose whole
purpose is to argue from that census. #326's body still carries the first-pass numbers;
they are superseded here. Re-run the scan rather than quoting either column from memory.

One repo alone held 1,362 rules, 1,133 of them verbatim full command lines rather than
glob patterns. (That pair is from the same first pass and was NOT independently re-run;
treat it as indicative of the shape, not as a measurement.)

### The accumulation mechanism, observed

This repo's own `.claude/settings.local.json` demonstrated the storage behavior on
2026-08-16, independently of any secret. It opened the session at **508 allow rules**;
three dead grants were removed individually, and a classifier pass over the remaining
**505 removed 409 one-and-done verbatim command lines**, leaving 96. The five largest of
25 categories:

```
170  one-shot diagnostic echo      echo "rc=$?", echo "GATE_RC=$?", ...
 45  one-off mutation/probe        sed -i version bumps, perl -0pi mutations
 35  pinned to a PR/comment id     reply-comment.sh 138 3440206801 "..."
 29  harness env-injection         GITHUB_REPOSITORY=o/r bash gh-comment.sh ...
 12  dead session scratchpad path  /private/tmp/.../<old-session-uuid>/...
```

DISCLOSURE, because this evidence was produced by exactly the kind of act this doc asks
the maintainer to ratify: that prune was an automated REMOVAL of 409 grants, a larger step than
the value-redaction proposed below. It was maintainer-directed and consent-gated per
removal, backed up and diff-verified. It is cited here as evidence of the accumulation
mechanism, not as precedent for unattended editing.

Every rule is born MAXIMALLY SPECIFIC, because "always allow" records whatever incidental
string was in flight — an exit code, a PR number, a session UUID. Nothing expires them. A
credential in that position is not an anomaly in the format; it is the format working as
designed.

### Why a PreToolUse deny cannot be the answer

It must decide "is this argument a secret" from a regex over an open set of third-party
CLI spellings. Two measured failures, both from #325's attempts:

- **It cannot separate discussion from usage.** `git commit -m "add --token support"`
  blocked while `--token to it` passed, because `[A-Za-z0-9+/_.-]{6,}` matches any English
  word of six or more characters. That blocked the repo's own mandated close-out
  actuations (commit, `gh pr comment`, `gh issue create`) — which is how a security control
  gets disabled by the next person who hits it.
- **The replacement failed its own census on first write**, catching 5 of 14: the regex
  captured nothing on quoted values and stopped at the first `NAME=` on the line. An
  author-written vector table had claimed 37/37 clean.

> HISTORICAL NOTE, not a measurement: an earlier emergency deny is often described as
> having caught "2 of 15". That figure is **not reproducible by anyone** — the deny existed
> only in a deployed copy, never in repo history, and has since been overwritten. Do not
> cite it as evidence.

A storage-side check faces a strictly easier problem, but the advantage is a MATTER OF
DEGREE, not a structural immunity:

- **Smaller, self-authored corpus.** It inspects only stored permission rules, not every
  command line, commit message, issue body, or PR reply as they are written.
- **Cheaper failure.** A false positive redacts an already-granted rule rather than
  blocking a command mid-work.

An earlier draft of this doc claimed a storage-side check "cannot false-positive on
prose". **That is false, and this repo's own settings file falsifies it.** A permission
rule stores the command line VERBATIM, quoted prose arguments included. Measured against
the 508 pre-prune rules on 2026-08-16: **42 stored rules carry long quoted English**,
including full PR-reply bodies —

```
Bash(./scripts/reply-comment.sh 138 3440336301 "Fixed in e616f3b: git ls-remote no
  longer uses || true; a read failure now aborts with exit 2 (fail closed)...")
```

So prose DOES reach the detector, and `git commit -m "rename API_KEY=old to
API_KEY=new2"` becomes a stored rule the moment it is granted. The scrubber must be built
for that, not assumed immune to it: the placeholder and entropy filters below are what
carry the load, and they are not optional garnish.

---

## The enforcement-point finding (VERIFIED, and it is not what the plan assumed)

The open question was whether a `PermissionRequest` hook can MODIFY the rule that gets
persisted, or only allow/deny. Verified against Claude Code **2.1.233** from the live
docs plus the shipped binary's zod schemas.

**It can write an arbitrary rule.** The hook receives the proposed rule text on stdin as
`permission_suggestions[].rules[].ruleContent`, and may emit its own
`decision.updatedPermissions` array of the same entry shape with a persisting
`destination`. The docs state the equivalence outright:

> A hook can echo one of the `permission_suggestions` it received as its own
> `updatedPermissions` output, which is equivalent to the user selecting that
> "always allow" option in the dialog.

Echoing a MODIFIED entry is therefore a redacted always-allow grant. `ruleContent` is an
unconstrained string in the schema — nothing binds it to the command that triggered the
prompt:

```js
tli = be({toolName:F(), ruleContent:F().optional()})
MDt = W0("type",[ be({type:kt("addRules"), rules:ht(tli()), behavior:eli(), destination:JBr()}), ... ])
```

Two facts constrain how far this goes:

1. **`updatedPermissions` is the ONLY field that reaches the rule store.** `updatedInput`
   rewrites this invocation's arguments and never persists. Redacting what is STORED and
   redacting what RUNS are separate mechanisms requiring separate fields.
2. **The hook fires BEFORE the dialog, not after a click.** There is no event that
   intercepts an actual "always allow" click. So the mechanism is PRE-EMPTION: the hook
   must return `behavior: "allow"` plus a redacted `updatedPermissions`, which suppresses
   the dialog entirely. If the hook declines to decide, the dialog appears and the user's
   click persists the ORIGINAL unredacted suggestion, which the hook can no longer amend.

### Two hook strategies, with opposite failure directions

Point 2 constrains the REDACT strategy specifically, and it is worth separating the two
uses rather than condemning the event:

**REDACT (`behavior: "allow"` + a rewritten `updatedPermissions`).** Pre-emption means the
hook must decide to ALLOW — silently, on the user's behalf — in order to redact. A detector
with any false-positive rate would be auto-granting permissions the user never saw a
prompt for. That is a materially worse failure than a missed cleanup, and it inverts the
consent property the whole design rests on.

**REFUSE (`behavior: "deny"` + `message`).** This needs NO pre-emptive allow. The hook
declines the grant and tells the user to re-run with the secret out of argv — which is
exactly what #326's body proposed ("it can still refuse the grant and tell the user to
re-run with the secret out of argv, which is a worse UX but a sound outcome"). Its failure
direction is the opposite one: a false positive BLOCKS LOUDLY rather than granting
silently, and the user sees it happen.

So the deferral below is about the REDACT strategy, not about the event being unsafe. The
refuse strategy is a live option and is deferred only because it shares the redact
strategy's unsolved half — the detector. A refuse hook is exactly as good as its
false-positive rate, and #325 measured that rate as bad enough to disable a control
(blocking `git commit -m "add --token support"`). Building the detector against the real
corpus is the prerequisite for EITHER hook mode, and the scrubber is how that detector
earns its confidence on data that already exists, at a failure cost of a missed cleanup
rather than a blocked commit.

> Caveat: that `behavior: "deny"` persists nothing was established from the output schema
> and the decompiled trace, not from an observed run. Verify it empirically before a
> refuse hook depends on it.

### What was NOT verified

Carried forward verbatim rather than dropped, because each one is load-bearing for a
decision above:

- **Downstream normalization of `ruleContent`.** Not traced past `wPe -> JEd(...)`. The zod
  schema does not constrain it, but a normalizer that rejects a rule not matching the
  triggering command is not ruled out. **Test this before any hook depends on it.**
- **No live payload was captured.** Grepping `~/.claude/projects/*/*.jsonl` for
  `"hook_event_name":"PermissionRequest"` returned nothing — hook stdin is not written to
  transcripts. The input schema rests on the doc example plus the binary's input builder.
- **Nothing was executed.** No hook was run and no resulting settings write was observed;
  the persistence claim rests on doc text plus the decompiled path, not a file diff.
- **"There is no click-interception event" is ABSENCE OF EVIDENCE.** No such event appears
  in the documented event list or the binary's event enum, which is not proof none exists.
  This one matters most: it is the entire premise of the pre-emption constraint, and
  therefore of the redact strategy's deferral.
- **Multi-hook precedence is undocumented** for `updatedPermissions` when several
  `PermissionRequest` hooks each return one.

---

## Decision

**Ship the scrubber (report-first, consent-gated). Defer the hook**, with the finding
above recorded so the deferral is a decision rather than an omission.

Rationale, in the order that mattered:

- The scrubber is the only option that cleans the **15 measured historical leaks**. They
  are on disk now; a preventive hook does nothing about them.
- The scrubber's failure mode is a MISSED CLEANUP. The hook's failure mode is an
  AUTO-GRANTED PERMISSION the user never saw. Those are not comparable, and the cheap
  direction goes first.
- It maps onto established repo precedent (`cache-reclaim.sh`) instead of inventing a
  shape.
- It ships without depending on the one capability still marked untested above.

The two are separable deliverables; this is not "scrubber instead of hook", it is
"scrubber first, hook when its unverified premise is measured".

**The ACTUATING half of the scrubber is BLOCKED on maintainer ratification** (see "The
CLAUDE.md tension" below). The detection-and-report half is unblocked and can ship first;
it delivers most of the value on its own, since the backlog becoming visible and
correlatable is the thing nobody can do today.

---

## The CLAUDE.md tension — NEEDS RATIFICATION

CLAUDE.md is unambiguous:

> NEVER hand-edit or monkey-patch permissions in `settings.json` / `settings.local.json`
> (adding OR narrowing a grant) - the user owns every grant. [...] Removing such a grant
> is the USER'S call - surface it, never hand-edit it out.

A scrubber that rewrites a rule is mechanically an automated edit of a grant. This design
does NOT step over that. The proposed reading, for the maintainer to accept or reject:

**Redact the VALUE, preserve the RULE.** Rewriting a secret to a fixed placeholder inside
a rule does not narrow the grant in spirit — a rule carrying a live secret was never going
to match a future invocation with a rotated secret anyway. The grant's shape, tool, and
scope are untouched.

**A leaked credential is a security incident, not a permission preference.** The
no-hand-edit rule exists to stop an agent quietly widening its own privileges. Redacting a
plaintext secret widens nothing.

**Actuation stays consent-gated regardless.** Report-only by default; `--yes <target>`
required to write; backup first; never clobber an unparseable file.

If the maintainer rejects this reading, the fallback is REPORT-ONLY FOREVER: the tool
prints what it found and the human edits. That still delivers most of the value (the
backlog becomes visible and correlatable) and costs only convenience. **The tool must not
ship its actuating half until this is ratified.**

---

## Detection contract

Two census lessons are load-bearing and both must hold:

- A scan matching only `KEY=VALUE` and not flag-style args **passes on nothing**. A scan
  matching only flag-style args misses **11 of 15**. It must do BOTH. (The current
  CLAUDE.md wording names only flag-style and asserts the inverse; that fix is tracked on
  the sibling policy issue.)
- Placeholders must be excluded or the report is noise. The census cleanly separated 3
  placeholders (`test`, `testkey`, `dummy`) from 15 real values.

Carrier set:

| Carrier | Example shape | Census count |
| :-- | :-- | ---: |
| env-assignment prefix | `KEY=<value> cmd ...` | 11 |
| `Authorization:` / `Bearer` header | `-H "Authorization: Bearer <v>"` | 2 |
| short flag, tool-scoped | `ldapsearch -w <v>` | 2 |
| long credential flag | `--token=`, `--api-key=`, `--password=` | 0 |
| inline URL credential | `://user:pw@host` | 0 |

The last two scored zero in the census and are included anyway: absence in one sample is
not absence in the population, and the marginal cost of a matcher is a line.

A value is flagged only if it is **both non-placeholder AND high-entropy** (Shannon, with
a reported bucket). The bar favors precision so that a clean machine reports clean.

Two honest limits on that conjunction:

- **A low-entropy real password is missed by construction.** A human-chosen
  `correcthorsebattery` scores like prose. The conjunction is a deliberate
  precision-for-recall trade, not a complete detector.
- **A miss is not merely "no cleanup".** It is a FALSE ALL-CLEAR: the report says clean and
  a human reasonably concludes there is nothing to rotate. That is worse than silence, and
  it is why the report must state what it CANNOT detect rather than presenting a clean
  scan as proof of absence.

Detection operates ONLY over rule text being stored as a permission — never over arbitrary
prose. That is the structural property, not a tuning choice.

---

## Reporting discipline

**No output path may print a secret value.** Per finding, emit: file, carrier type, value
SHAPE (length, character classes, entropy bucket), and a short SHA-256 prefix. The prefix
is what makes a single rotation's blast radius visible across repos without revealing
anything.

Unredacted detail goes ONLY to a 0600 local file, created 0600-from-birth with the
established idiom (`orchestrate-setup.py:956`):

```python
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
...
os.chmod(path, 0o600)   # enforce even if it pre-existed with looser perms
```

The `chmod` after the fact is not redundant: `O_CREAT` mode applies only when the file is
newly created, so a pre-existing looser file would otherwise keep its mode.

---

## Implementation shape

Python, because detection needs JSON parsing, SHA-256, entropy scoring, and atomic JSON
writes — and because the only existing cascade parser is Python.

- **Cascade discovery** reuses the `_cascade_files()` approach from
  `orchestrate-setup.py:1352`, including an `ORCHESTRATE_SETTINGS_FILES`-style
  colon-separated override so the harness can point at temp fixtures.
  `orchestrate-setup.py` is hyphenated and not importable, so the small helper is
  replicated rather than imported.
- **Fault isolation per file**: a malformed or non-dict settings file is skipped with a
  loud warning and **never treated as clean**. This is the #334/#375 rule — "I could not
  read this" must never render as "there was nothing to read".
- **CLI shape from `cache-reclaim.sh`** (default report-only; `--yes <target>` to actuate;
  `-h/--help`), but the **EXIT CONTRACT from `cr-quota-watch.sh`**, which is the right
  precedent because this tool ANSWERS A QUESTION rather than performing a chore:

  | exit | meaning |
  | ---: | :-- |
  | 0 | scanned successfully, nothing found |
  | 1 | scanned successfully, findings present |
  | 2 | could not determine (unreadable file, malformed invocation) |

  A blanket "every non-malformed path exits 0" would make *12 credentials found*, *clean*,
  and *3 files unreadable* indistinguishable to any caller — which is precisely the
  #334/#375 defect this doc invokes two paragraphs above, where "I could not read this"
  renders as "there was nothing to read". A fail-open exit belongs on a chore that must
  never abort a pipeline (`cache-reclaim --nudge`); it does not belong on an oracle.
- **Write discipline**: back up before any write, verify the backup EXISTS rather than
  inferring it from a call that returned (the #292/#327 lesson — `shutil.copy2` onto a
  directory does not raise, it copies INTO it), refuse the write if the backup cannot be
  made, then atomic `os.replace`.
- **Harness** in the repo's stdlib-only subprocess style; `ruff check --select F,E741`
  clean; full gate green.
- **Gate registration is part of the deliverable, not follow-up.** A new script plus its
  `test-settings-scrub.py` must be added to BOTH lint enumerations — `.gates.toml` and the
  `## Gates` block in `CLAUDE.md` — and to the CI list. These are hand-maintained and drift
  silently: `test-ci-gates-lockstep.py` exists because they once diverged until CI
  shellchecked 28 of the 37 scripts the local gate covered, `orchestrate-authorize-merge.sh`
  among the nine CI never linted. That harness will FAIL the gate on an unregistered
  addition, which is the intended behavior, not an obstacle to route around.

### Invocation model

`SessionStart` and `SessionEnd` were both floated on #326. The recommendation is
**neither, initially: an on-demand command** (`/scrub-settings`, or a direct invocation),
for one reason — a scan that runs automatically at session boundaries is a scan whose
output nobody reads, and this tool's entire value is a human acting on its report. The
canonical failure here is a hook that prints on every session until it becomes wallpaper,
which is precisely the class this session fixed elsewhere (a status hook printing on 9 of
10 events, unread for four weeks).

If it later earns automatic invocation, `SessionStart` is the correct end: findings are
actionable at the start of work, and `SessionEnd` output is written to a terminal the user
has typically stopped reading. Any automatic mode must be REPORT-ONLY and must stay silent
on a clean scan.

### Mutation-proving the detector

Per the standing rule that a new gate must be proved able to FAIL: the harness must
include a fixture whose secret the detector is expected to catch, and the test suite must
be shown failing when the matcher is disabled. A detector that reports clean because it
never looked is indistinguishable from a clean machine — which is this repo's most
frequently re-grown defect, and the exact shape #324's coverage assert exists to prevent.

---

## Out of scope

No floor or guard change. No allow-list broadening. No network, `git`, or `gh` mutation.
No CLAUDE.md wording fix (sibling policy issue). The `PermissionRequest` hook itself,
per the deferral above.

## Follow-ups this design creates

1. **An implementation issue** for `settings-scrub.py` referencing this doc (standing rule:
   a design doc needing implementation gets its own tracked issue).
2. **An empirical test** of whether a hook-supplied `ruleContent` survives downstream
   normalization — the one unverified premise the deferred hook would depend on.
3. **Maintainer ratification** of the CLAUDE.md-tension section before the
   actuating half ships.
