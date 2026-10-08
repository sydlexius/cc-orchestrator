# Design: the queued push and PR tail (#538, unit C / #541)

Date: 2026-10-07
Status: PROPOSAL, revised in FIX ROUND 3 (the third hostile review returned DO NOT SHIP; see `DESIGN-gate-queue.md` for the round's
simplification). The maintainer decisions on #538 and the two later ones (the runner makes no commit; commits need not be squashed)
are BINDING and are designed to, not reopened. RECONCILED 2026-10-07 against the MERGED pool document (PR #543).
Issues: #538 is the EPIC; #541 is unit C, the subject of this document.
Companions: `DESIGN-gate-queue.md` (unit B: job records, dispatcher, workers, recovery; cited as "queue section N"),
`DESIGN-gate-pool.md` (unit A; "pool section N"), `DESIGN-gate-queue-wiring.md` (unit D), `DESIGN-fixround-push-gate.md` (the receipt
leg in `safe-push.sh`), `DESIGN-expected-check-set.md` ("could not read" is never "nothing to read").
Depends on: unit B complete (PRs B1 to B4). Everything about processes, locks, environments, the bounded-call helper, the watchdog
and recovery is defined there and only USED here.

Scope: the worker's tail gains four steps (`classify`, `freshness`, `push`, `pr`), `gate-job/v1` gains `base` and `pr`, the result
gains the push and PR fields, and `gate-enqueue.py add` gains `--tail push` and the PR flags. `safe-push.sh` and `base-freshness.sh`
are CALLED, not changed. NO deterministic-floor change, no allow-list entry beyond unit B's one. The runner makes NO commit.

Evidence labels as in the companions: RUN, READ, REASONED, PRIOR. Nothing was run in fix round 3. No live push was ever made for this
design; both transports are UAT items (section 11).

---

## Problem

- EACH LEAD HAND-CHAINS GATE, PUSH AND PR CREATION. Three outward steps, each a place to stop half-way, and no single record of what
  was pushed and which PR it became.
- THE PUSH SIDE HAS AN ORDERING DEFECT. `/prep-pr` checks freshness at Step 1c, then the gate, the hostile review and any wait pass
  before `safe-push.sh` enforces freshness at Step 7. A merge to the base in that window refuses the first push, and because the
  receipt binds the TREE the refresh forces a full re-gate. PRIOR: one stillwater branch was gated three times this way.

WHAT THIS UNIT DOES ABOUT THE SECOND, AT ITS TRUE SIZE: the queue checks freshness again at slot grant. For a job that gates in the
queue this refuses a stale branch before the gate. For the common `/prep-pr` job, whose gate already ran inline, it saves nothing:
the window is Step 1c to slot grant, including the queue wait, and a refusal costs a second gate. THE RE-GATE CHURN IS NOT REMOVED,
and nothing bounds a repeat. What the queue does buy there: the second gate is one re-enqueue under slots instead of a hand-run
chain, the commit pushed is pinned, and a crash around the push is reported truthfully and resumes. The one route that could end
the churn is open question 2, outside the first delivery.

## Decisions this unit is bound by

- Decision 1: `gate-runner.py` may open a PR for a queued job; opening one is opt-in PER JOB (the `pr` object). Its default stays
  "write the receipt".
- Decision 2: a job is DATA: a worktree, a branch, a body file, labels. Never a command line.
- Decision 13: `/prep-pr` KEEPS its inline Step 2 gate; the queued tail reuses its receipt.
- Decision 14: THE RUNNER MAKES NO COMMIT. A first push that is definitively behind its base is refused `stale-base`; the lead
  refreshes in its own session.
- Decision 15: commits need not be squashed before a push.

(14 and 15 are the two later decisions; the earlier draft numbered them 13 and 14 and the inline-gate decision 12, one off from the
pool document.)

ADOPTED BY DEFAULT 2026-10-07 (the maintainer may revisit): a restarted runner may open a missing PR after a list-first check.
WITHDRAWN and still withdrawn: "the runner may itself merge the base into a behind first-push branch". READ from this machine's git
config: `commit.gpgsign=true`, `gpg.format=ssh`, and the signing program is an approval-prompting desktop signer. A merge made by a
DETACHED process waits for an approval that `GIT_TERMINAL_PROMPT=0` and SSH BatchMode do not govern (REASONED, not run: a test would
prompt the maintainer), and where signing is not forced it would be UNSIGNED and surface only at a `required_signatures` merge gate
(#333). It is open question 1.

---

## 1. What a push job adds to the records

`gate-job/v1` (queue section 1) gains:

| Field | Type | Rule |
|---|---|---|
| `base` | str or null | declared base for a first push; null = the repo default branch |
| `pr` | object or null | first-push PR details. null says "a PR exists, this is a fix round" (section 3 says when that is refused) |
| `pr.title_file`, `pr.title_sha256` | str | const `title.txt` inside this job's payload directory; 64 hex. Content: 1..256 chars, one line, no control characters |
| `pr.body_file`, `pr.body_sha256` | str | const `body.md` inside the payload directory, never a caller path; 64 hex |
| `pr.labels` | list of str | at most 20; each 1..50 chars, no control characters, no comma |
| `pr.milestone`, `pr.draft` | str or null, bool | same character rule as a label; open as draft |

`gate-job-result/v1` gains `kind` (`first`, `fix`: derived at `classify`), `pushed_sha` (what origin was verified to hold; when set
it EQUALS `head_sha`, because the runner never adds a commit), `behind_base` (non-zero only for a reviewed PR pushed with
`--stale-ok`), `safe_push_verdict` (the verbatim line), `pr_number`, `pr_url`, `pr_created` (false with a number = an existing PR).

Validation, at `add` and again in the worker:

- `base`: `git check-ref-format refs/heads/<base>` must succeed (exit status only), and the value is not dash-led, not `HEAD` and
  not `refs/*`. This is the check `base-freshness.sh` makes (READ: its line 62), so the two cannot disagree. NOT the `--branch`
  form: git documents that form as EXPANDING the previous-checkout shorthand `@{-N}`, so it can accept a value that is not a
  branch name, which `base-freshness.sh` then rejects with exit 2 and `safe-push.sh` reads as unknown and proceeds on (READ:
  its `*)` arm). The stored value is the validated string itself; nothing is expanded or normalized.
- `pr.body`: the caller passes `--body-file <path>`. The helper reads it (cap 65536 characters, valid UTF-8, no NUL), COPIES it to
  `payload/<job>.r<rev>/body.md` and records the sha256. A later edit or a deleted temp file cannot change the job.
- `pr.title`: `--title-file <path>` is the ONLY form. The floor greps command LINES, and a title that quotes a trigger phrase would
  get the enqueue call itself denied.
- Labels and the milestone are checked for SHAPE only. One that does not exist fails the `pr` step AFTER the push (`error`
  `pr-create-failed`, `pushed=yes`); the lead opens the PR by hand or re-enqueues with corrected details.
- Every value reaches `gh` as `--flag=value`. The ONE exception is `safe-push.sh --base`, which takes its value as a SEPARATE word:
  READ (`scripts/safe-push.sh`, the `--base)` case) there is no `--base=<name>` form, and that spelling would be forwarded to the
  upload as a flag. The name is validated as not dash-led, so it can never be read as one.

---

## 2. The tail, step by step

Run by the worker in the job's worktree. Gate steps run with the STEP environment; every other call goes through unit B's
bounded-call helper with the CONTROL environment (queue section 4). "Refused" always means THIS run sent nothing. THE RUNNER CHANGES
NOTHING IN THE WORKTREE: what it pushes is exactly the commit that was enqueued. Steps 1, 4, 5 and 8 are unit B's.

| # | Step | What happens | On failure |
|---|---|---|---|
| 0 | (recovered jobs only) | The `.pushing` marker is present: read origin FIRST (section 6) | section 6 |
| 1 | `preflight` | queue section 4 | `refused` |
| 2 | `classify` | ONE fresh read of the branch's PRs in ALL states (section 3). Decides fix round or first push BEFORE anything is pushed, and resolves `base`: the open PR's base, else the job's `base`, else the default branch | `error` `gh-unreadable` after 3 tries (2 s, 5 s, 10 s); `refused` `ambiguous-pr`, `pr-merged`, `pr-closed`, `pr-head-unknown`, `no-pr-details`, `shallow-clone`, `base-mismatch` |
| 3 | `freshness` | ONLY when no PR exists, or the open PR shows no review activity (or the activity is unreadable). `base-freshness.sh <base> refs/heads/<branch>`, the same read-only call `safe-push.sh` makes. Exit 1 (definitively BEHIND): STOP before any gate step. Exit 0 (fresh or unknown) proceeds. Any other exit (2: the helper rejected its arguments) is doubt and STOPS | `refused` `stale-base`, `gate=not-run`, the behind count in the log; `error` `freshness-error`, nothing sent |
| 4 | `gate` | queue section 4: reuse a binding receipt, else run in-process under slots, else bounce | `failed` |
| 5 | `receipt` | queue section 4 | `failed` |
| 6 | `push` | The worker re-tests the pre-push hook condition: a cost-0 worker BOUNCES if a hook is now present, and the answer picks the call's bound (section 5). Then the marker is written, then `safe-push.sh` runs under that bound | section 5 |
| 7 | `pr` | Section 7: a fix round creates nothing and verifies the PR head; a first push lists again, then creates | `error`, always with `pushed=yes` |
| 8 | `finalize` | queue section 4 | - |

---

## 3. Step 2: what the PR read decides

THE READ: `gh pr list --repo=<repo> --head=<branch> --state=all --limit=100` with number, state, base, head oid,
`isCrossRepository` and review activity. Two rules about it, both from round 3:

- ONLY PRs WHOSE HEAD REPOSITORY IS `repo` ARE COUNTED (`isCrossRepository` false). RUN H3 (round-3 reviewer): `gh pr list --head`
  cannot take an owner ("`<owner>:<branch>` syntax not supported"), so an outsider's open fork PR whose branch is named like a local
  one would otherwise read as this branch's PR, make the job a fix round, and be recorded as its result.
- A read that returns exactly the limit may be truncated and is treated as UNREADABLE.

"Review activity" is the predicate `open-pr-staleness-sweep.sh` uses (a review or a review comment exists), never `reviewDecision`.

A merged or closed PR must not read as "no PR": a fix-round job whose PR merged while it sat in the queue (routine here) would be
treated as a first push, re-create the auto-deleted remote branch and possibly open a second PR for merged work. So the rule starts
from what the job DECLARED: `pr = null` says "a PR exists"; `pr` details say "open a new PR". Every refusal has nothing pushed.

| # | What the read shows for this head branch (same-repository PRs only) | `pr` in the job | Outcome |
|---|---|---|---|
| 1 | more than one OPEN PR | any | `refused` `ambiguous-pr` |
| 2 | exactly one OPEN PR | present or null | FIX ROUND. Any `pr` details are ignored (`pr_created=false`). A declared `base` that differs from the PR's: `refused` `base-mismatch` |
| 3 | no OPEN PR; at least one MERGED | null | `refused` `pr-merged` |
| 4 | no OPEN PR; none merged; at least one CLOSED | null | `refused` `pr-closed` |
| 5 | no PR in any state | null | `refused` `no-pr-details` |
| 6 | no PR in any state | present | FIRST PUSH: push, then create the PR |
| 7 | no OPEN PR; merged or closed PRs exist | present | the per-PR test below; FIRST PUSH only if every one of them is shown UNRELATED |

The per-PR test of row 7, for each merged or closed PR with head commit `H`, all local reads:

- `git cat-file -e H^{commit}` fails (this repository does not have `H`): `refused` `pr-head-unknown`. A missing object is doubt,
  and doubt refuses. The remedy is in the result: fetch `refs/pull/<n>/head` and re-enqueue, or push inline.
- `H` is an ancestor of `head_sha` AND `H` is NOT reachable from `refs/remotes/origin/<base>`: the branch still carries that PR's own
  commits. `refused` `pr-merged` or `pr-closed`.
- otherwise (`H` is not in this job's history, or is already in the base): UNRELATED. Go on to the next.

| Case | What the read and the repository show | Fix-round job (`pr = null`) | First-push job (`pr` present) |
|---|---|---|---|
| (a) update-branch, then merge: the PR head is GitHub's merge commit, never fetched | merged, `H` not present locally | row 3: `pr-merged` | `pr-head-unknown` |
| (b) merge-commit repo; the old PR merged; the name is recut from the base | merged, `H` an ancestor of `head_sha`, reachable from the base | row 3: `pr-merged` | UNRELATED: first push |
| (c) squash-merge repo; the branch is CONTINUED after its PR merged | merged, `H` an ancestor, NOT reachable from the base | row 3: `pr-merged` | `pr-merged`, on purpose: a new PR would carry the merged commits again. Recut from the base, or push inline |
| (d) closed unmerged; the name is reused for new work cut from the base | closed, `H` present, NOT an ancestor | row 4: `pr-closed` | UNRELATED: first push (`pr-head-unknown` had the old head been collected) |
| (e) closed unmerged and CONTINUED on the same branch | closed, `H` an ancestor, NOT reachable from the base | row 4: `pr-closed` | `pr-closed`. Reopen the PR (the job is then a fix round), or push inline |

- The reachable-from-base test reads the LOCAL remote-tracking ref. If it is stale, case (b) reads as (c) and is refused; fetch the
  base and re-enqueue. Wrong only toward refusing.
- In a SHALLOW clone ancestry cannot be trusted: any merged or closed PR for the name is `refused` `shallow-clone`.
- A first push with no details is refused, not pushed without a PR: `/handle-review` enqueues with no details because it expects
  the PR to exist. The price: `/prep-pr`'s "push, no PR" choice takes the inline path.
- RESIDUAL, stated and not closed: a PR that merges or closes AFTER this read and before the push. `safe-push.sh` then re-creates a
  deleted branch name; step 7 creates nothing (section 7) and the lead deletes the remote branch.

---

## 4. Freshness, and what a `stale-base` refusal really costs

FRESHNESS IS CHECKED AT SLOT GRANT, BEFORE THE GATE (the issue's fourth acceptance criterion). The runner-made refresh merge is
WITHDRAWN; the in-`safe-push.sh` file-disjoint policy is NOT adopted; a caller-inferred `--stale-ok` stays REJECTED.

| Job | Refused at | Gates already spent | What the refusal costs next |
|---|---|---|---|
| `/prep-pr` first push (gate ran inline, receipt reused) | step 3 | one, inline | a second gate, from the back of the class |
| the same | step 6, by `safe-push.sh` (the base moved between step 3 and the push: seconds) | one, inline | the same |
| a job that gates in the queue (a re-enqueue after a refresh; `/handle-review` on an unreviewed PR) | step 3 | none in the queue | one gate after the refresh |
| the same | step 6 (the base moved during the queue's gate) | one, in the queue | another |

NOTHING BOUNDS A REPEAT: the base can move again during the second gate. On a busy base the window per attempt is the queue wait
plus one gate. The flow a refusal starts: the lead refreshes IN ITS OWN SESSION, where its signer can ask for approval (a plain merge
of the base, never a rebase of a pushed branch), then re-enqueues. That is a new entry at the BACK of its class (decision 5),
charged the gate weight because no receipt binds the new tree. The merge commit STAYS in the branch.

---

## 5. The `safe-push.sh` call, exactly

`bash <sibling>/safe-push.sh <branch> --base <base> [--stale-ok]`: the script beside the deployed runner, the branch, the TWO words
`--base` and the validated name, then at most `--stale-ok`. Nothing else is ever appended: no `--rewrite`, no `--rebased`, no
`--ungated`, no forwarded flag. The argv is a closed template and the harness asserts no other word can reach it. `safe-push.sh`
stays the only thing that pushes; its receipt leg, freshness leg and verdict line are unchanged and remain the authority. The call
runs with `GATEQ_HOLDER` set to the worker's ticket (`running/<job>.ticket`), so a pre-push hook's gate runs under the worker's
slots (pool section 5).

THE HOOK'S GATE IS A NESTED RUN, by the pool's rules and nothing added: it takes no ticket ONLY under the exact exemption (the
worker's ticket is held, same pool protocol, same worktree key, and the worker's held cost is at or above the gate's), holds the
worker's nest lock `running/<job>.ticket.nest` for its run, and has its own watchdog and its own `.sweep` claim in `waiters/`
(queue section 2). When the exemption fails on cost it prints `gate-runner: NOT RUN` and exits 75 at once
(`nested-over-holder`); a pool config or protocol error exits 2. Either fails the push with nothing sent: `safe-push.sh` prints
`FAILED`, the job ends `failed` `push-failed`, and the line is in `log_path`. That is a gate that DID NOT RUN, never a failed
gate (pool section 6), so the gate's steps are not what to fix; it is also never transient inside a queued push. Read the NOT RUN
line: `nested-over-holder` means the worker's held cost is below the gate's (run `stop` after a `budget` change, or correct
`weight`); exit 2 names a config or protocol error (on a branch that predates B2, merge main). Correct that, re-enqueue ONCE; a
second identical result goes to the maintainer. Queue section 2's cost rule (a job in a hook-installed repo holds
the gate weight, and a bounce pins it) is what keeps the exemption true on the ordinary path.

- `--base <base>` is always passed, for the default branch too. It is a FACT (the PR's own base, or the declared base of a new
  branch), and naming it spares `safe-push.sh` its fallback to a cached `origin/HEAD` that may be unset (freshness then reads
  unknown and the push proceeds; READ its freshness leg). The TYPED contracts differ on purpose and are not changed: `/prep-pr`
  Step 7 and the pr-shipper OMIT `--base` for the default base, because the floor's destination matcher reads a bare `main` or
  `master` word anywhere in a `safe-push.sh` COMMAND LINE as a push to it (READ `scripts/orchestrate-guard.sh`, `has_main_dest`:
  a whole-word match over the clause, whatever flag precedes it). That deny is real and does not reach this call: the worker's
  argv is a list handed to `Popen` by a detached process, never a Bash tool call, so no PreToolUse hook sees it (section 9).
  The consequence is for people and harnesses: this argv is never pasted onto an agent's command line (section 11's fixtures).
- THE BOUND on the call is `push_timeout_s` when no pre-push hook can run (the cost rule's test, queue section 2, made by EVERY
  worker immediately before the marker) and `job_timeout_s + push_timeout_s` when one can. The hook's gate carries the pool's
  `job_timeout_s` (5400 by default, against 900), so with `push_timeout_s` alone the worker's bound would end a healthy gate and
  report `pushed=unknown`. `push_timeout_s` thus always means the push itself, and no config can put it below the gate. A hook
  that appears after the test gets the short bound and, if its gate outlasts it, `error` `push-timeout`.
- `--stale-ok` is passed IFF step 2's read showed an open PR WITH review activity. There is ONE read, at step 2; nothing is re-read
  at push time (the earlier text said both). Unreadable activity = flag absent = `safe-push.sh` refuses a behind branch.
- A first push refused `stale-base` here (the base moved after step 3) ends `refused` `stale-base`. There is no retry.

Its ONE stdout line decides (READ, `scripts/safe-push.sh`: the EXIT trap prints exactly one of six statuses):

| Verdict line | The job | `pushed` (section 6 has the full rule) |
|---|---|---|
| `OK ... sha=<sha>` with `<sha>` equal to `head_sha` | goes on to `pr` | `yes` |
| `OK` with any other sha | `error` `pushed-sha-mismatch`, the tail STOPS | `unknown` |
| `REFUSED reason=<slug>` | `refused` with safe-push's slug | `no` |
| `FAILED` (the push ran; origin was read and does not hold the SHA) | `failed` `push-failed` | `no` |
| `ERROR` (an abort before the push ran) | `error` `safe-push-error` | `no` |
| `USAGE` (a defect in the closed template) | `error` `safe-push-usage` | `no` |
| `UNVERIFIED` (exit 3), no line at all, or the call was ended at its bound | `error` (`push-unverified`, `push-timeout`), the tail STOPS: no PR is opened on an unknown | `unknown` |

---

## 6. One persisted bit, and origin first

Round 3's finding 1: recovery re-ran the refusing steps after a push had landed, and the result then read "nothing sent". READ
(`scripts/safe-push.sh`): the freshness leg (about lines 454 to 499) runs BEFORE the remote read (line 535), so a re-push of a commit
origin already holds prints `REFUSED stale-base pushed=no` whenever the base moved meanwhile. "`safe-push.sh` answers it from origin"
was therefore wrong, and the journal that was deleted on requeue is replaced by one bit that is not.

THE BIT. `running/<job>.pushing` is an empty marker file. The worker creates it if absent (a recovered job that runs the tail from
the top finds it already there; `fsync` of the file and of the directory) immediately BEFORE it starts `safe-push.sh`. It travels with the job's files on a requeue and is deleted only when they
move to `done/`. While a worker is alive it means nothing: the worker knows what happened and says so in its own result. It speaks
only when the worker cannot.

ORIGIN FIRST. A worker that finds the marker at start (the job was recovered after a death at or after the push) does this before
any other step:

1. `git ls-remote origin refs/heads/<branch>`, bounded. UNREADABLE: `error` `origin-unreadable`, `pushed=unknown`, the tail STOPS.
   A failed read is never read as "the branch is not there".
2. Origin holds `head_sha`: the push LANDED. `pushed=yes`, `pushed_sha = head_sha`, and the worker goes STRAIGHT to `pr`. It does
   not re-run preflight's refusals, freshness, the gate or the push, so a base that moved or a receipt that vanished after the push
   cannot turn a landed push into a refusal.
3. Otherwise the tail runs from the top. If `classify` then shows a same-repository PR whose head is `head_sha`, the push landed
   too: a MERGED one ends the job `done` with reason `merged-after-push` and `pr_created=false` (the PR merged, and its branch was
   deleted, while the job was down); an OPEN one is the fix-round or resumed first-push case and the tail continues.

THE `pushed` RULE, one rule for every writer of a result (a worker, the dispatcher's 3-attempt cap, `cancel`):

- `yes` ONLY when origin was read and holds `head_sha` (a `safe-push.sh` `OK`, or the read above), or a same-repository PR's head is
  `head_sha`;
- `no` ONLY when no marker exists for this job, or this attempt's verdict line says nothing was sent AND the job did not start
  with a marker from an earlier attempt;
- `unknown` otherwise. So a recovered job that is then refused, cancelled or capped reports `unknown`, never "nothing sent".

WHY `unknown` AND NOT A SECOND READ: when a worker is SIGKILLed its watchdog kills the in-flight push at once (queue section 4), but
a push whose pack the server had already accepted can still land after the client is gone, and after any read. No local mechanism
closes that, so the result does not claim to. Replacing a job in place (a new `add` for the branch) KEEPS the marker; it is deleted
only on the move to `done/`. Origin-first then answers correctly for an unchanged head, and for a changed head a refusal reports
`unknown`, the conservative side. `add` still prints `prior-push=unknown sha=<old head>` on its line so the lead knows an earlier
commit may be on origin.

---

## 7. `gh pr create`, and the `pr` step

No existing helper creates a PR, so the worker calls `gh` through the bounded-call helper with a list argv:

```
gh pr create --repo=<repo> --head=<branch> --base=<base> --title=<title>
             --body-file=<GATEQ_HOME>/payload/<job>.r<rev>/body.md  [--label=<l>]... [--milestone=<m>] [--draft]
```

The `pr` step always starts with the read of section 3 again (all states, same repository):

| The re-read shows | The step |
|---|---|
| exactly one OPEN PR | creates nothing. ITS HEAD MUST EQUAL `pushed_sha`, else `error` `pr-head-mismatch` (round 3: a PR that is not this branch's must never be recorded as the result). Records number and URL, `pr_created=false`. This is a fix round, and also a first push whose worker died after the create |
| no OPEN PR; the job is a first push; nothing merged or closed that step 2 did not already clear | re-hashes title and body against the entry (`error` `body-mismatch` if either differs: nothing created), creates, reads back the number |
| a merged or closed PR that step 2 did not see, or a fix round whose PR is no longer open | creates nothing: `error` `pr-merged-during-job`. The lead deletes the re-created remote branch (the residual of section 3) |
| unreadable, three times | `error` `pr-unknown` |

ON THE ORIGIN-FIRST PATH (section 6), where `classify` never ran in this worker: (a) `kind` is `fix` when exactly one same-repository
PR is open, else `first` when `pr` details are present; (b) "that step 2 did not already clear" means "passes the per-PR test of
section 3 now"; (c) a merged same-repository PR whose head is `head_sha` ends `done` `merged-after-push` here too, whether or not
the branch was auto-deleted. `base` for a create is the job's `base`, else the default. The entry's re-validation as data still
runs before the origin read.

A failed create is followed by three re-reads: found means `done`, not found means `error` `pr-create-failed`. Every outcome of this
step carries `pushed=yes`. `gh pr create` is the one step that is NOT idempotent, and is therefore guarded (list first, in all
states) rather than retried; GitHub itself also rejects a second open PR for the same head and base (REASONED from `gh`'s documented
behavior; a UAT item). Not supported in v1: a PR from a fork to an upstream repository (`--repo` is always `origin`'s).

---

## 8. What the control environment costs a push

The control environment (queue section 4) removes what a SESSION could inject. Its costs for the push, stated plainly:

- (a) A push that works only through a caller-exported `GIT_SSH_COMMAND` fails with ssh's own error. The remedy is `~/.ssh/config`:
  the runner's constant `GIT_SSH_COMMAND` OVERRIDES `core.sshCommand` (RUN H1, round-3 reviewer: with the variable set, the
  configured command was not used), so that setting is no remedy.
- (b) An HTTPS remote authenticates through git's credential helper. READ: this repo's `origin` is HTTPS and the user's git config
  names `gh auth git-credential` for it, so the push needs `gh`'s own store to answer with no token variable. REASONED to work
  detached, NOT RUN. A setup that relies ONLY on a token variable fails the push over HTTPS, or gets a pushed branch and `error` at
  `pr` over SSH.
- (c) A pre-push hook fired by the runner's push runs under the CONTROL environment; one whose gate needs directories outside
  `tool_dirs` fails loudly (`failed`, nothing pushed) until the user adds them. Its gate may outlast `push_timeout_s` (the pool's
  `job_timeout_s` is 5400 by default, against 900), so with a hook present the worker bounds the push by their SUM (section 5);
  the price is that a push stuck in a hook-installed repo holds its slots and worktree that much longer.
- (d) PROXY AND CA VARIABLES ARE NOT CARRIED (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`, `GIT_SSL_CAINFO` and the
  like). A machine that reaches GitHub only through a proxy set in the environment cannot use the queue in v1.

GIT CONFIGURATION THE PUSH STILL OBEYS. The repo-level config is writable by anyone with shell access to the worktree: a
`git config` there can set `core.hooksPath`, `credential.helper` or a push URL (`remote.origin.pushurl`, `url.<x>.pushInsteadOf`).
The runner's `safe-push.sh` call then EXECUTES that hook or helper as the user, holding the user's agent socket and `gh`
authentication, or pushes to that URL. That is the same power as today's inline push from that worktree. It is said here so that
"fixed control environment" is not read as "the push is hermetic". The user's global config is honored on purpose.

---

## 9. Security: what the runner enforces because the floor cannot

The threat model, the forgeable entry and worker, and the one hand-granted entry are in queue section 8. The floor does not see a
detached process, so these are the runner's own:

| Floor rule (PreToolUse, command-line grep) | In the detached runner |
|---|---|
| no push to `main` / `master` / default | refused at enqueue AND at preflight, with a LIVE default-branch read that fails CLOSED |
| no bare `--force` / `-f` | unrepresentable: the push argv is a closed template; a non-fast-forward is `safe-push.sh`'s own `REFUSED rewrite` |
| no `--no-verify` | unrepresentable: no upload command is built here, and `safe-push.sh` receives no extra flag |
| no `--no-gpg-sign`, no `-c commit.gpgsign=...` | unrepresentable: the runner makes NO commit, builds no `-c` argument, and its environment carries no `GIT_CONFIG_*` |
| merge gating (Tier 2) | the runner has NO merge, approve or review code path at all |

THE CLOSED LIST of external commands the runner may execute, each by name through `tool_dirs`, with the control environment and a
timeout: `git` (`rev-parse`, `symbolic-ref`, `status`, `worktree list`, `check-ref-format`, `remote get-url`, `ls-remote`,
`cat-file -e`, `merge-base --is-ancestor`), `base-freshness.sh`, `safe-push.sh`, `gh` (`pr list`, `pr view`, `pr create`,
`repo view`), and, with the STEP environment, the gate's own steps. No `git merge`, no `git commit`, no other history-writing verb.
It is a registry in `gate_queue.py`, and a harness case fails if any call site builds an argv outside it.

WHAT A MALFORMED OR HAND-EDITED PUSH ENTRY CAN CAUSE, at most: the named worktree's gate runs; a non-default branch the user can
already push is pushed through `safe-push.sh`; one PR is opened with attacker-chosen text. It CANNOT name a command, a script, a
remote other than `origin`, a refspec, a push flag, a destination branch other than the checked-out one, a commit other than the
branch's own head, or a title or body path outside the queue. A title or body whose hash differs from the entry's creates no PR.

Rigor: `/prep-pr` Step 4a's classifier will print `standard` for these files. C1 and C2 should nevertheless be reviewed at
DENY-AUTHORITY depth, because a defect there can permit a bad push with no hook behind it.

WHAT STAYS THE LEAD'S PRIVILEGED WORK: every judgment step, REFRESHING a behind branch, composing the title, body, labels and the
draft-or-open choice, every bot reply, resolve and ack, `gh pr update-branch`, any rewrite or `--ungated` push, a first push with no
PR, stack linking, the merge, cleanup. The runner never commits, comments, edits a PR, approves, merges or posts a review trigger.

---

## 10. Failure modes (push and PR)

| Failure | Resulting state | Recovery |
|---|---|---|
| The push hangs (a prompting key agent, a stuck hook, a dead network) | Slots and worktree held | The WORKER kills the push's group at the call's bound (section 5): `error` `push-timeout`, `pushed=unknown`. Re-enqueueing is safe |
| Worker SIGKILLed mid-push | Its watchdog kills the push at once and keeps the worktree claimed (its `.sweep`, and the hook gate's own) until every group is gone. The marker exists | Recovery re-queues; the job is not eligible until the claims free; the next worker reads origin FIRST (section 6) |
| A pre-push hook's gate prints `gate-runner: NOT RUN` (exit 75 `nested-over-holder`, or a pool config error) | The push fails; nothing sent | `failed` `push-failed`, `pushed=no`; the line is in the log. Not a failed gate, and not transient: correct the cause the line names, re-enqueue ONCE (section 5) |
| Killed after the push, before PR create | Origin holds the SHA; no PR | Origin first: straight to `pr`, which lists and creates |
| Killed after PR create, before the result | The PR exists | Origin first: straight to `pr`, which finds ONE open PR with the pushed head and records it |
| Killed after the push; the base then moved, or the receipt is gone | Origin holds the SHA | Origin first skips freshness and the gate: `done`, never `refused stale-base` |
| Killed after a fix-round push; the PR then merged and its branch was deleted | Origin no longer has the branch | The tail runs; `classify` finds the merged PR with head `head_sha`: `done` `merged-after-push` |
| A recovered job is cancelled, or dies three times | - | `cancelled` or `error`, `pushed=unknown` |
| The PR MERGED or CLOSED while the job was queued | No open PR | `refused` at step 2 (`pr-merged`, `pr-closed`, `pr-head-unknown`). Nothing pushed, no branch re-created |
| The PR merged between step 2 and the push | The push re-creates the remote branch | `error` `pr-merged-during-job`, `pushed=yes`; the lead deletes the branch. Stated, not closed |
| An outsider's fork PR uses the same branch name | - | Not counted (section 3) |
| Origin unreachable before the push | `base-freshness.sh` says unknown; `safe-push.sh` prints `REFUSED remote-unreadable` | `refused`, nothing sent |
| Origin unreadable after the push | `UNVERIFIED` | `error`, `pushed=unknown`, the tail STOPS. A re-enqueue reconciles |
| `gh` rate limit, outage or hang at `classify` | each call ended at `call_timeout_s`; three tries | `error` `gh-unreadable`. Never read as "no PR exists" |
| `gh` failure at create | three re-reads | found: `done`; not found: `error` `pr-create-failed`, `pushed=yes` |
| Behind its base at slot grant, or the base moved before the push | step 3, or `safe-push.sh` | `refused` `stale-base`; a second gate for a `/prep-pr` job; nothing bounds a repeat (section 4) |
| The push needs an interactive approval, or a credential the helper cannot produce unattended | BatchMode, `GIT_TERMINAL_PROMPT=0`, stdin `/dev/null`: REASONED to fail fast, NOT RUN | `refused` or `failed`, else `push-timeout`. Both transports are UAT items |
| A pre-push hook appears after a cost-0 admission | The worker re-tests before the push | BOUNCE, cost pinned to the gate weight (queue section 5) |

---

## 11. Decomposition: unit C (#541) as two PRs

- C1, THE PUSH LEG: `--tail push`, `base`, the all-states same-repository PR read and its table, freshness before the gate, the
  cost-0 hook re-test, the marker and origin-first recovery, the closed-template `safe-push.sh` call under its bound, the
  verdict table, the `pushed` rule in every writer, the fix-round half of the `pr` step, the argv registry.
- C2, THE PR-CREATE LEG: the `pr` object, the title and body copies, list-first `gh pr create`, crash-resume.

DEPENDENCIES. C1 needs B3 (the worker) and is ordered after B4 (both edit `gate_queue.py`). C2 needs C1.

- Tier: CR-required (script function); each PR reviewed at deny-authority depth and given LIVE UAT on a sandbox repository, with the
  maintainer's go, before it opens. UAT for C1 includes a push from the DETACHED runner on this machine over BOTH transports: SSH
  through the key agent (which may ask for approval) and HTTPS through the `gh` credential helper.
  - UAT, NOT VERIFIED (reviewer, round 4: no push was made): whether a PR's head read back immediately after a push can lag the
    pushed commit. If it can, `pr-head-mismatch` needs the same 2 s, 5 s, 10 s re-read before it fires.
- Agent hints: `[mode: plan] [model: opus] [effort: high]`
- Acceptance criteria:
  - [ ] The runner refuses a default, `main` or `master` branch (an unreadable live default refuses), a moved head, and a dirty or
        missing worktree; nothing is sent on any refusal.
  - [ ] No job field and no code path can add a word to the `safe-push.sh` call beyond `--base <name>` and a derived `--stale-ok`;
        `safe-push.sh` is unchanged; every one of its six verdicts maps to a stated result; the call is ended by the worker at
        its bound (`push_timeout_s`, plus `job_timeout_s` when a pre-push hook can run) with its whole process group.
  - [ ] The runner makes no commit. A first-push branch definitively behind at slot grant is refused `stale-base` BEFORE any gate
        step, with nothing sent; unknown freshness does not block; a reviewed PR is pushed with `--stale-ok`; what is pushed is
        exactly the enqueued commit. (The stale-base re-gate is NOT ended by this unit.)
  - [ ] PR lookups read all states and count only same-repository PRs. With no open PR, a job with no details is refused before
        anything is pushed; a job with details is a first push only when every merged or closed PR for the name is shown UNRELATED;
        a missing head refuses. A fix round records a PR only when its head equals the pushed commit.
  - [ ] A LANDED PUSH IS NEVER REPORTED AS "NOTHING SENT": killing the worker at every point from the marker onward and recovering
        yields `pushed=yes` or `unknown`, never `no`, whatever the base, the receipt or the PR did meanwhile; and never a second PR.
  - [ ] An `UNVERIFIED` or timed-out push never leads to a PR. A SIGKILLed worker leaves no push process running.
  - [ ] The PR title and body are the queue's hash-checked copies; a later edit or deletion of the caller's files changes nothing.

| PR | Test plan |
|---|---|
| C1 | A harness against a local bare `origin` built by FETCH (the guard greps sandbox commands too; trigger text stays inside fixtures): protected-branch refusal including an unreadable live default; no extra word can reach `safe-push.sh`, `--base` is two words and is passed for the default base too; a `base` of `@{-1}` (with a previous checkout in the fixture), `HEAD`, a `refs/` name or a refspec is refused at `add` and again by the worker; a `base-freshness.sh` stub exiting 2 ends `error` `freshness-error` with no gate step and no push; `--stale-ok` only with review activity; a behind first push is refused with no gate step started; unknown freshness proceeds; every row of section 3's table and all five cases for both job kinds; a fork PR with the same branch name is ignored; a read at the limit is unreadable; each of the six verdicts, with a stub printing it; a stand-in push that never returns is ended at `push_timeout_s` with its group and yields no `pr` step; with a pre-push hook present, a stand-in hook gate that outlasts `push_timeout_s` is NOT ended and the push completes, and one that outlasts `job_timeout_s + push_timeout_s` is ended `push-timeout`; the worker killed after the marker with origin holding the pin, then with the base moved, then with the receipt deleted, then with the PR merged and the branch gone (each ends `pushed=yes`); killed with origin not holding it and then refused (`unknown`); origin unreadable at recovery (`error`, `unknown`); `cancel` and the 3-attempt cap on a marked job say `unknown`; a replace in place keeps the marker; a hook's gate inside the push takes no ticket, holds the worker's `.nest` and its own `.sweep`, and one heavier than the worker's held cost exits 75 at once and yields `failed` `push-failed` with the NOT RUN line in the log; an origin-first first push on a reused branch name (case b or d) does not end `pr-merged-during-job`; a fix-round PR whose head differs is `pr-head-mismatch`; the registry holds no history-writing verb. Mutations: forward one extra argv word; write `--base=<name>`; pass `--stale-ok` on an unreadable read; read open PRs only; count cross-repository PRs; treat a missing PR head as unrelated; drop the reachable-from-base condition; run the gate before the freshness check; write the marker AFTER starting the push; skip the origin-first read; report `no` for a marked job; read a failed `ls-remote` as "absent"; drop `push_timeout_s`; bound a hook-present push by `push_timeout_s` alone; validate `base` with `check-ref-format --branch`; proceed on a `base-freshness.sh` exit 2; add `git merge` to the registry. Then LIVE UAT over SSH and HTTPS |
| C2 | Stubbed `gh`: title and body copied and hash-checked; a deleted source file changes nothing; argv is `--flag=value` only; list-first finds an existing PR and creates nothing; a PR that merged during the job creates nothing; a missing label fails after the push as `pr-create-failed`; the worker killed before and after the create never yields a second PR. Mutations: create before listing; read the body from the caller's path; drop the hash check; drop the head-equals-pushed check. Then LIVE UAT |

---

## 12. Rejected alternatives

- A caller that infers `--stale-ok` from "the base moved only in files this branch does not touch" (a DECLARED flag made inferred,
  #345; unsound on renames); the in-`safe-push.sh` disjoint policy, for now (nothing measured).
- The runner making the refresh merge (withdrawn; open question 1); a retry after `stale-base`.
- "The journal need not survive, `safe-push.sh` answers from origin" (round 3, finding 1: its freshness leg refuses first).
- Recovery that waits for an orphaned push instead of killing it: the watchdog registration kills it, and the marker covers what a
  kill cannot (a pack already accepted).
- Reading only OPEN PRs to decide "is there a PR"; matching PRs by branch name across forks; deciding "related or unrelated" by
  ancestry alone, which treats a missing object as unrelated.
- PR details, or any "also push" switch, as `gate-runner.py` flags; the runner pushing with a raw upload command, or being given
  `--rewrite` or `--ungated` by a job; an inline `--title`; existence reads for labels at enqueue.

## Open questions for the maintainer (neither blocks units B to D)

1. Should the runner ever make the refresh merge itself (merge the base into a behind first-push branch at slot grant, then gate
   and push)? If yes, it would be added LATER and only with both of: a timeout on the merge, and a check that the new merge commit's
   signature state matches the branch tip's before anything is gated or pushed. (yes, later, with both conditions / no)
2. Should a LATER delivery end the stale-base re-gate by having GitHub make the merge? The route: push a behind first push and open
   its PR as usual, then, before any review exists, have GitHub merge the base in server-side (`gh pr update-branch`, plain merge
   mode). No local commit and no local signing. What it costs: the merged tree is checked only by CI, never by a local gate; the PR
   head then differs from the receipt's commit and from `head_sha`, so the receipt-gated review request (`elmer-enqueue.sh`) refuses
   until the lead pulls and gates again; the runner would gain one PR-mutating call and would pass `--stale-ok` for an UNREVIEWED
   branch, which changes what that flag declares. (yes, design it as a later unit / no)

---

## Appendix: evidence

Nothing was run for this document in fix round 3, and no push was ever made for this design.

| # | Where | Experiment | Observed |
|---|---|---|---|
| H1 | review 3, `exp/review3/h.py` | A push with `core.sshCommand` configured, with and without a constant `GIT_SSH_COMMAND` (ssh stubbed) | Without the variable the configured command ran; with it, only the variable's command ran |
| H2 | review 3 | A worker stand-in starts a "push" in its own session and is SIGKILLed | The stand-in was ALIVE 1 s later |
| H3 | review 3 | `gh pr list --help` | `--head`: "`<owner>:<branch>` syntax not supported"; default `--limit` 30; `isCrossRepository` is a JSON field |
| G2b | fix 2 | A stand-in that never returns, own session, group SIGTERM at the bound | Nothing left 1.3 s after a 1 s bound |

Read for fix round 3: `scripts/safe-push.sh` (the six verdicts and the EXIT trap, lines 97 to 163; the freshness leg at 454 to 499
BEFORE the remote read at 535; an additive re-push is classified fast-forward at 552; no `--base=<name>` form);
`commands/handle-review.md` (the Step 7 gated-push block writes the receipt and today always passes `--stale-ok`).

Reasoned, not run: GitHub rejecting a second `gh pr create` for an open head and base; a detached merge waiting on an
approval-prompting signer (not run on purpose); a detached push failing fast under BatchMode over SSH and authenticating through the
`gh` credential helper over HTTPS (UAT items for C1); the marker and the origin-first read; a server landing a push after its client
was killed.
