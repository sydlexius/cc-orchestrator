# ARCHIVE: the retired Codoki agent-judgment mechanisms

STATUS: DORMANT as of 2026-07-12. The Codoki subscription lapsed and Codoki has no free tier, so
no PR will ever receive a Codoki review again. Every NON-DETERMINISTIC (agent-judgment / prompt-driven)
Codoki mechanism was removed from the live instruction surfaces on that date. This file is the record
of what was removed and where it lived.

WHAT IS STILL LIVE (deliberately, do NOT "clean these up"): the DETERMINISTIC Codoki plumbing stays in
the tree. The only thing that can produce a Codoki check is an `@codoki` mention, and nothing emits one
any more, so every gate a LIVE PR PATH actually touches is INERT BY DESIGN:

- `ship-gate-preflight.sh --codoki-gate` - liveness mode (#237): no trigger + no check -> PASS. This is
  the mode `pr-watch.sh` calls, so the settle loop cannot wedge.
- `ship-gate-preflight.sh` FULL mode, Codoki root-ack gate (#234) - PASSes on no-summary.
- `gh-react.sh codoki-ack` - the ack reader/actuator; still the way to clear a LEGACY unacked summary.
- `resolve-threads.sh`'s `copilot|greptile|codoki` bot regex, and the Codoki test harnesses /
  `.gates.toml` steps (all still pass; they test the plumbing, not a live Codoki).

DEAD TOOLING - DO NOT RUN (the one path that is NOT inert):

- `ship-gate-preflight.sh --codoki-only` - the STRICT settlement mode. By contract it BLOCKs (exit 2)
  when the `Codoki PR Review` check is missing, which is now the permanent state. It does not hang (it
  is one-shot), but it always reports "not settled".
- `codoki-quota-watch.sh` - its only caller. On any post-lapse PR it loops the strict oracle and emits
  `CODOKI NOT-YET-RUN ... Keep waiting.` - a permanently wrong signal about a bot that will never run.

Both are kept only so a re-subscribe is a restore rather than a rewrite.

RE-ENABLING (if the subscription ever returns): restore the blocks below to the files named against
them. Nothing else needs to change - no script was removed, so the deterministic layer comes back to
life the moment an `@codoki` mention is posted again.

---

## 1. `skills/orchestrate/SKILL.md` - CODOKI-BEFORE-CODERABBIT

Sat immediately after TRIGGERED CR REQUIRED FOR SCRIPT FUNCTIONAL CHANGES.

> - CODOKI-BEFORE-CODERABBIT (CR-required PRs). For a PR that needs a triggered CR pass, get PAST CODOKI FIRST - all Codoki findings triaged to clean (real ones fixed) or documented false-positive declines (empirical reason + the thumbs-down reaction + the `@codoki` rebuttal per the Codoki-ack rule) - BEFORE triggering `@coderabbitai review`. This avoids duplicate punch-list items and a wasted CR slot (the budget is exhaustible; BATCH BY SURFACE + TIER under PR-OPEN OWNERSHIP is the front-end half - fewer, surface-coherent PRs spend fewer CR reviews). SCOPE (#237): Codoki auto-review is OFF, so this rule applies ONLY when a `@codoki` pass was actually triggered (see BOT-REVIEWER CALIBRATION - the lead triggers it sparingly, and most PRs get NO Codoki pass at all); when no Codoki pass exists there is nothing to clear before CR. FLOW (only if `@codoki` was triggered): the lead triages every Codoki finding vs the CURRENT code -> re-push until Codoki is clean (or its only remaining items are documented FP declines; do not let a persistent bot false-positive block indefinitely) -> ONLY THEN surface the CR-trigger as a `▶ NEEDS YOU` ask and STOP - the MAINTAINER posts `@coderabbitai review` themselves (the lead/agent never does; triggering CR is the maintainer's exclusive purview). Memory: `codoki-before-coderabbit`.

Also retired with it: the auto-memory entry `codoki-before-coderabbit` (deleted from the local memory store).

## 2. `skills/orchestrate/SKILL.md` - the CODOKI row of BOT-REVIEWER CALIBRATION

The rule was titled `BOT-REVIEWER CALIBRATION + WHEN TO MENTION @codoki` (#236); the title lost its
second half and the Copilot / CodeRabbit rows stayed. The excised row:

> - CODOKI = weak; auto-review is OFF (#237, as of 2026-07-11 - the flip is LIVE). Codoki now reviews ONLY on an explicit `@codoki` trigger, so by default there is NOTHING to triage. Measured precision ~0.61, recall ~0.43, ~32% FP, WORST on security (0.41), and INVERTED severity calibration (its High-severity findings are LESS reliable than its Medium). TRIGGERING: the LEAD posts `@codoki` when warranted - it is lead-triggerable, NOT maintainer-exclusive like CR (resolved in #237); the pr-shipper, being PUSH-ONLY, may SURFACE a recommendation to the lead but NEVER posts to Codoki itself (the lead owns every outward comment, same actuation boundary as all bot interaction). The DEFAULT is to NOT trigger: do so ONLY for a small/medium FOCUSED correctness / ordering / resource-accounting diff (its one genuine strength); otherwise do not mention it. When you DO trigger it, pr-watch stays honest via the oracle's `--codoki-gate` mode (#237): an UNtriggered PR with no Codoki check no longer wedges the settle loop (the pre-#237 hang), while a triggered one waits for the check. Once its check lands, triage per CODOKI-BEFORE-CODERABBIT and CALIBRATE: discount its High-severity flags (verify before acting), expect a trigger-happy secret-scanner + confidently-wrong syntax/version FP class, and NEVER treat a Codoki pass as review coverage.

The measured numbers survive in issue #236 (full analysis + repeatable harness) if the calibration is
ever needed again.

## 3. `skills/orchestrate/SKILL.md` - BATCH BY SURFACE + TIER (#153), Codoki-budget clause

The rule STAYS; only its Codoki-budget reasoning was cut. The excised opening clause:

> A CR pass is a scarce slot (~5/hr) and a triggered `@codoki` pass draws on Codoki's rate-limited budget (~10/hr per `codoki-quota-watch.sh`) - note Codoki auto-review is OFF (#237), so a PR no longer auto-consumes a Codoki review; only a maintainer-triggered CR pass and any lead-triggered `@codoki` pass spend budget.

Its closing sentence also named the retired rule: "This is the front-end half of the exhaustible-review
budget named in CODOKI-BEFORE-CODERABBIT".

## 4. `~/.claude/CLAUDE.md` (user-global, machine-local) - the standing Codoki ack mandate

> - Codoki ack: ALWAYS react 👍/👎 to Codoki's root review post (👍 accept; 👎 rebut a false positive + `@codoki` reply with the reason). Required "seen-and-triaged" signal.

Demoted to ORACLE-DRIVEN: the agent no longer decides to ack. It acks only when
`ship-gate-preflight.sh` BLOCKs on a LEGACY unacked Codoki summary, clearing it with
`gh-react.sh codoki-ack <pr> --react +1|-1`.

The same file's reviewer-posture sentence also carried the triggering guidance:

> Codoki = feeble (~0.61 prec/~0.43 recall/~32% FP), triggered SPARINGLY by the lead only for a small focused correctness/ordering/resource diff (default: do not trigger).

## 5. `skills/orchestrate/templates/pr-triage-charter.md` - the Codoki calibration clause

Excised from the CALIBRATE BY REVIEWER list in step 2:

> Codoki findings run ~32% FP with INVERTED severity (discount its High-severity flags, expect secret-scanner + wrong-syntax/version FPs, verify any Go "won't compile" claim vs CI)

(The Go "won't compile" clause is a COPILOT calibration and was KEPT, reattached to Copilot.)

## 6. `skills/orchestrate/templates/pr-shipper-brief.md` - the lead-decidable trigger

Excised from the bot-trigger boundary (step 28) - the shipper's "posts NOTHING to any bot" prohibition
STAYS; only the clause telling the LEAD it may trigger was cut:

> and `@codoki`-trigger is lead-decidable (#237).

Its push-order note (step 25) and the SKILL.md push-order / Stage-progression / LITE-mode lines carried
the same "Codoki auto-review is OFF (#237) ... a triggered `@codoki` pass's findings are still handled"
phrasing; all were reworded to "Codoki is NOT IN SERVICE ... a LEGACY Codoki finding is still handled".
The push-order rule itself (CR-specific, push-first) is UNCHANGED.

## 7. `commands/handle-review.md`, `commands/review-stack.md`

These asserted Codoki was the PRIMARY AUTO-REVIEWER that "reviews every push" - already stale after
#237 turned auto-review off, and false outright once the subscription lapsed. Corrected in place to:
Codoki does not review; a LEGACY Codoki thread or summary on an older PR is triaged, replied and
resolved like any other bot finding. No waiting on Codoki, no triggering it. Greptile (where installed)
remains the one bot that may post UNPROMPTED, so the `pr-watch` quiet-period wait still stands.

`commands/autofix-pr.md` was deliberately NOT edited: its only Codoki mention is a future-enhancements
bullet noting that non-CR reviewer bots do not yet gate the loop's SUCCESS condition - descriptive, not
a triggering instruction.

## 8. The auto-memory entries (local store, not in this repo)

- `codoki-before-coderabbit` - DELETED (the rule it encoded is retired, above).
- `reviewer-tank-copilot-not-codoki` - REWRITTEN as a Copilot-is-the-trusted-tank fact; Codoki noted as
  retired rather than as a reviewer to calibrate against.
- Incidental Codoki clauses were stripped from the remaining entries and the `MEMORY.md` index.
