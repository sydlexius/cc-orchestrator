#!/usr/bin/env python3
"""Lockstep harness: CI must lint and run exactly what .gates.toml gates (#364, #379).

THE BUG THIS EXISTS TO PREVENT. `.gates.toml` and `.github/workflows/ci.yml` each carried a
HAND-MAINTAINED list of what to lint and which harnesses to run, and nothing compared them.
They drifted to the point where CI shellchecked 28 of the 37 scripts the local gate covered
- including `orchestrate-authorize-merge.sh`, which arms the merge-auth token the security
floor reads - and ran only 26 of 42 gated harnesses (#379).

WHY A HARNESS AND NOT A ONE-TIME RE-SYNC. Re-syncing by hand fixes today's drift and
guarantees tomorrow's. The repo has solved this shape twice already - `test-version-lockstep.py`
for the SKILL.md/plugin.json pair, and the `#284` exact-count assertion for HELPER_NAMES.

THE HARNESS LIST IS NOW DERIVED, NOT DUPLICATED (a deliberate REVERSAL). This header used to
say "WHY NOT DERIVE CI's LIST FROM .gates.toml": deriving makes drift impossible, but CI would
then depend on parsing repo config at CI time. The maintainer accepted that trade-off: CI ran
all 46 harnesses as SERIAL Actions steps (375s on macOS, 230s on ubuntu, nearly all of it
subprocess-spawn cost), and the only way to parallelize them is to hand them to gate-runner,
which already runs .gates.toml `jobs = 4` with `exclusive` barriers locally. So CI now runs
`python3 scripts/gate-runner.py --jobs 4 --skip ...` once per OS leg. Two wins: wall time, and
a harness added to .gates.toml runs in CI with no ci.yml edit, so there is no second list to
drift. That holds only while each step has a unique, explicit name, because CI deselects steps
by name (--skip): a reused name would deselect two. Both gate-runner (under --skip) and this
harness refuse duplicate or missing names. What this harness checks for harnesses therefore changes from "the
two lists agree" to "CI cannot silently stop running the derived list":
  - each OS leg (Linux, macOS) has exactly ONE gate-runner invocation, on a one-line `run:`,
    gated by an exact `if: runner.os == '<OS>'`, with no `continue-on-error`;
  - it passes `--jobs N` (N >= 2; that is the point of the change);
  - its `--skip` set equals that leg's CI_SKIP table EXACTLY, both directions, and every
    CI_SKIP entry carries a written reason and names a real .gates.toml step;
  - no step CI runs is `required = false` or carries a `skip_if_absent` / `skip_if`
    predicate, each of which would let it pass CI without running or failing.

THE LINT LISTS STAY HAND-MAINTAINED. CI's shellcheck runs from a digest-pinned image and
ruff is installed on Linux only, so those two steps keep their dedicated CI steps (and are
`--skip`ped from gate-runner). Their lists still get the original three checks, because two
of them can pass while the invariant is broken:
  1. SET EQUALITY, BOTH DIRECTIONS. A one-way check misses a CI-only entry, which is a
     stale path that lints nothing and looks like coverage.
  2. A PARSE-SANITY FLOOR. An empty or truncated parse compares {} against {} and passes,
     which is exactly how a drift guard becomes decorative. (Learned the hard way in #330:
     a `split("]")` truncated on a `[ -x ]` inside a comment and reported a present entry
     as missing. Assert the parse before trusting the verdict.)
  3. A FILESYSTEM CROSS-CHECK. Set equality only proves the two lists agree - they can
     agree and both omit a script that exists. Every `scripts/*.sh` must be linted
     somewhere, and every `test-*.py` must be a .gates.toml step.

A MUTATION SELF-TEST at the end re-runs this file against fixture copies with ONE invariant
broken per case and requires each to fail with its check's message.

Stdlib only, no network. Run: python3 test-ci-gates-lockstep.py
"""
import glob
import os
import re
import shlex
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    sys.exit("FAIL: tomllib unavailable (needs Python 3.11+)")

# LOCKSTEP_ROOT points the checks at a fixture tree; only the mutation self-test at the end of
# this file sets it, to prove each harness-step check fails when its invariant breaks.
ROOT = os.environ.get("LOCKSTEP_ROOT") or os.path.dirname(os.path.abspath(__file__))
GATES = os.path.join(ROOT, ".gates.toml")
CI = os.path.join(ROOT, ".github", "workflows", "ci.yml")

# Scripts deliberately exempt from the filesystem cross-check, each with a stated reason.
# An entry here is a decision to leave a file unlinted, so it must not be silently editable.
FS_EXEMPT: dict[str, str] = {}

# The .gates.toml steps CI's gate-runner invocation deliberately `--skip`s, PER OS LEG, each
# with a written reason. The default is that CI runs every step; an entry here is a decision
# that a gated step goes unchecked on that leg, so it needs a reason a reviewer can dispute.
# ci.yml's --skip set must equal this table exactly (both directions), so neither side can
# change alone.
_LINT_REASON = ("run by a dedicated Linux CI step instead (shellcheck from the digest-pinned "
                "koalaman image, ruff installed Linux-only); its list is lockstepped below")
CI_SKIP: dict[str, dict[str, str]] = {
    "Linux": {
        "shellcheck": _LINT_REASON,
        "ruff": _LINT_REASON,
    },
    "macOS": {
        "shellcheck": _LINT_REASON,
        "ruff": _LINT_REASON,
        "test-orchestrate-setup": (
            "the setup/doctor harness exercises HOST-COUPLED wiring (settings cascade, tmux, "
            "guard execution, git repo state) that diverges on a fresh macOS runner - "
            "env-coupling, not a shipped-code bug - so it runs on the Linux leg only"),
    },
}

def fail(msg):
    sys.exit(f"FAIL: {msg}")


# An exemption is only as good as its written reason: an empty or whitespace-only one would
# silently drop coverage while this guard still passed, so reject it before any set math.
for _label, _exempt in [("FS_EXEMPT", FS_EXEMPT)] + [(f"CI_SKIP[{o}]", t) for o, t in CI_SKIP.items()]:
    _blank = sorted(k for k, v in _exempt.items() if not isinstance(v, str) or not v.strip())
    if _blank:
        fail(f"{_label} entries without a written reason -> {_blank}")


def expand(tokens):
    """Resolve glob tokens against the repo so both sides compare real paths."""
    out = set()
    for t in tokens:
        if not t or t.startswith("-"):
            continue
        if any(ch in t for ch in "*?["):
            out.update(os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, t)))
        else:
            out.add(t)
    return out


def gates_step_run(name):
    try:
        with open(GATES, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError) as e:
        fail(f"cannot read/parse .gates.toml: {e}")
    for step in data.get("prep_pr", {}).get("steps", []):
        if step.get("name") == name:
            run = step.get("run", "")
            if not run:
                fail(f".gates.toml step '{name}' has an empty run string")
            return run
    fail(f".gates.toml has no prep_pr step named '{name}'")


def ci_text():
    try:
        with open(CI, encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        fail(f"cannot read ci.yml: {e}")


print("CI <-> .gates.toml lint lockstep + gate-runner wiring (#364, #379)")

ci_src = ci_text()

# --- shellcheck ------------------------------------------------------------------------
gates_sc = expand(gates_step_run("shellcheck").split()[1:])
m = re.search(r"scripts=\((.*?)\)", ci_src, re.S)
if not m:
    fail("ci.yml has no `scripts=( ... )` shellcheck array (parse failed, not a drift)")
ci_sc = expand(m.group(1).split())

# --- ruff ------------------------------------------------------------------------------
gates_ruff = expand(gates_step_run("ruff").split()[1:])
m = re.search(r"run: ruff check[^\n]*", ci_src)
if not m:
    fail("ci.yml has no `ruff check` line (parse failed, not a drift)")
ci_ruff = expand(m.group(0).split()[2:])

# --- 2. parse sanity, BEFORE any verdict ------------------------------------------------
# An empty parse compares {} to {} and passes. These floors are deliberately well below the
# real counts (37 / ~40) so ordinary growth never trips them, but a collapsed parse does.
for label, s in (("gates shellcheck", gates_sc), ("ci shellcheck", ci_sc),
                 ("gates ruff", gates_ruff), ("ci ruff", ci_ruff)):
    if len(s) < 10:
        fail(f"{label} parsed only {len(s)} entries - the parse broke; fix it rather than "
             f"the lists (an empty-vs-empty comparison passes and proves nothing)")
print(f"  [ok  ] parses are non-degenerate "
      f"(shellcheck {len(gates_sc)}/{len(ci_sc)}, ruff {len(gates_ruff)}/{len(ci_ruff)})")

# --- 1. set equality, BOTH directions ---------------------------------------------------
problems = []
for label, a, b in (("shellcheck", gates_sc, ci_sc), ("ruff", gates_ruff, ci_ruff)):
    only_gates = sorted(a - b)
    only_ci = sorted(b - a)
    if only_gates:
        problems.append(f"{label}: in .gates.toml but NOT linted by CI -> {only_gates}")
    if only_ci:
        problems.append(f"{label}: in ci.yml but NOT in .gates.toml -> {only_ci} "
                        f"(a CI-only entry is often a stale path that lints nothing)")
if problems:
    fail("the CI and .gates.toml lint lists have drifted:\n  " + "\n  ".join(problems)
         + "\n\nAdd the missing entries to BOTH lists. Do not delete from .gates.toml to "
           "make this pass - that removes local coverage instead of restoring CI's.")
print("  [ok  ] shellcheck lists match in both directions")
print("  [ok  ] ruff lists match in both directions")

# --- 3. filesystem cross-check ----------------------------------------------------------
# Both lists agreeing does not mean they are complete: they can agree and both omit a file.
on_disk = {os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, "scripts", "*.sh"))}
unlinted = sorted(on_disk - gates_sc - set(FS_EXEMPT))
if unlinted:
    fail("these scripts exist but are linted by NEITHER list:\n  " + "\n  ".join(unlinted)
         + "\n\nAdd them to the shellcheck step in .gates.toml AND ci.yml, or add an entry "
           "to FS_EXEMPT in this harness with a written reason.")
print(f"  [ok  ] every scripts/*.sh is linted ({len(on_disk)} on disk)")

stale = sorted(gates_sc - on_disk)
if stale:
    fail(f"shellcheck targets that no longer exist on disk: {stale}")
print("  [ok  ] no shellcheck target is missing from disk")

# --- harness steps: .gates.toml is the ONE list (#379, then derived) -------------------
# A harness counts only when its step RUNS it: the whole run string must be exactly
# `python3 test-<name>.py`. A text match would count `echo python3 test-x.py`, which runs
# nothing; any other run string mentioning a harness is an unsupported shape and fails.
HARNESS_STEP_RE = re.compile(r"python3\s+(test-[\w.-]+\.py)")


def gates_steps():
    try:
        with open(GATES, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError) as e:
        fail(f"cannot read/parse .gates.toml: {e}")
    steps = data.get("prep_pr", {}).get("steps", [])
    if not isinstance(steps, list):
        fail(".gates.toml [prep_pr].steps is not an array")
    return [s for s in steps if isinstance(s, dict)]


steps = gates_steps()
# CI selects steps by NAME (--skip), so every step needs an EXPLICIT, UNIQUE name: a copied block
# whose `run` changed but not its `name` would share a --skip and drop out of CI silently, and an
# unnamed step's derived `step-<i>` shifts whenever a step is inserted above it.
_unnamed = [s.get("run", "?") for s in steps if not s.get("name")]
if _unnamed:
    fail(f".gates.toml steps without an explicit `name` (CI --skip selects by name): {_unnamed}")
_names = [s["name"] for s in steps]
_dupes = sorted({n for n in _names if _names.count(n) > 1})
if _dupes:
    fail(f".gates.toml step names are not unique (one --skip would match several): {_dupes}")
step_names = set(_names)
gates_h = set()
for step in steps:
    run = step.get("run", "").strip()
    m = HARNESS_STEP_RE.fullmatch(run)
    if m:
        gates_h.add(m.group(1))
    elif re.search(r"python3\s+test-", run):
        fail(f".gates.toml step '{step.get('name')}' names a harness in an unsupported shape "
             f"(only a bare `python3 test-<name>.py` counts as running it): {run!r}")

if len(gates_h) < 10 or len(steps) < 10:
    fail(f"gates parsed only {len(steps)} steps / {len(gates_h)} harnesses - the parse broke; "
         f"fix it rather than the config (an empty parse passes and proves nothing)")
print(f"  [ok  ] harness parse is non-degenerate ({len(steps)} steps, {len(gates_h)} harnesses)")

h_on_disk = {os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "test-*.py"))}
ungated = sorted(h_on_disk - gates_h)
if ungated:
    fail("these harnesses exist but .gates.toml runs NONE of them:\n  " + "\n  ".join(ungated))
missing = sorted(gates_h - h_on_disk)
if missing:
    fail(f"harness steps that no longer exist on disk: {missing}")
print(f"  [ok  ] every test-*.py is a gate step ({len(h_on_disk)} on disk)")

# --- CI runs .gates.toml through gate-runner on BOTH legs ---------------------------------
# Split ci.yml into step blocks (each starts at a `- name:` list item) and find every block
# whose run line invokes gate-runner. The shapes accepted are deliberately narrow: an
# unrecognized `if:` or a multi-line `run: |` is a FAILURE, never a guess.
OS_IF_RE = re.compile(r"^\s*if:\s*runner\.os\s*==\s*'(Linux|macOS)'\s*$")
blocks = re.split(r"^(?=\s*- name:)", ci_src, flags=re.M)
invocations: dict[str, list[list[str]]] = {"Linux": [], "macOS": []}
for block in blocks:
    # Matches any INVOCATION spelling (`python3 scripts/...`, `./scripts/...`, `python ...`),
    # not the bare filename, which the ruff step lints as a target.
    if not re.search(r"(python3?\s+|\./)scripts/gate-runner\.py", block):
        continue
    if not re.search(r"^\s*- name:", block, re.M):
        continue   # the preamble before the first step (comments only)
    body = [ln for ln in block.split("\n") if ln.strip() and not ln.strip().startswith("#")]
    name = body[0].strip()
    runs = [ln for ln in body if re.match(r"^\s*run:", ln)]
    if len(runs) != 1 or not re.match(r"^\s*run:\s*python3 scripts/gate-runner\.py(\s|$)", runs[0]):
        fail(f"ci.yml step `{name}` mentions gate-runner.py but is not a one-line "
             f"`run: python3 scripts/gate-runner.py ...` (unsupported shape)")
    if any(re.match(r"^\s*continue-on-error:", ln) for ln in body):
        fail(f"ci.yml step `{name}` sets continue-on-error, so the harnesses can fail "
             f"without failing CI")
    ifs = [ln for ln in body if re.match(r"^\s*if:", ln)]
    if len(ifs) > 1:
        fail(f"ci.yml step `{name}` has more than one `if:`")
    if ifs:
        m = OS_IF_RE.match(ifs[0])
        if not m:
            fail(f"ci.yml step `{name}` has an unrecognized condition {ifs[0].strip()!r}; "
                 f"only `if: runner.os == 'Linux'` / `'macOS'` is accepted")
        legs = [m.group(1)]
    else:
        legs = ["Linux", "macOS"]
    argv = shlex.split(runs[0].split("run:", 1)[1])[2:]
    for leg in legs:
        invocations[leg].append(argv)

if not re.search(r"^\s*os:\s*\[ubuntu-latest,\s*macos-latest\]\s*$", ci_src, re.M):
    fail("ci.yml's matrix is not exactly `os: [ubuntu-latest, macos-latest]`; the required "
         "checks `gates (ubuntu-latest)` / `gates (macos-latest)` depend on it")

problems = []
for leg, found in invocations.items():
    if len(found) != 1:
        problems.append(f"{leg}: expected exactly one gate-runner invocation, found {len(found)} "
                        f"(no gate-runner invocation for {leg} means CI stopped running the "
                        f"harnesses there)")
        continue
    argv = found[0]
    jobs, skip, i = None, set(), 0
    while i < len(argv):
        a = argv[i]
        if a in ("--jobs", "--skip") and i + 1 < len(argv):
            val = argv[i + 1]; i += 2
        elif a.startswith(("--jobs=", "--skip=")):
            a, val = a.split("=", 1); i += 1
        else:
            problems.append(f"{leg}: unexpected gate-runner argument {a!r}")
            break
        if a == "--jobs":
            jobs = val
        else:
            skip.add(val)
    if not (jobs and jobs.isdigit() and int(jobs) >= 2):
        problems.append(f"{leg}: gate-runner is not run with `--jobs N` (N >= 2); got {jobs!r}")
    want = set(CI_SKIP[leg])
    if skip - want:
        problems.append(f"{leg}: ci.yml --skips steps CI_SKIP gives no reason for -> "
                        f"{sorted(skip - want)}")
    if want - skip:
        problems.append(f"{leg}: CI_SKIP lists steps ci.yml does not --skip -> "
                        f"{sorted(want - skip)}")
    not_steps = sorted(want - step_names)
    if not_steps:
        problems.append(f"{leg}: CI_SKIP names no .gates.toml step -> {not_steps}")
    for step in steps:
        if step.get("name") in skip:
            continue
        if step.get("required", True) is not True:
            problems.append(f"{leg}: step '{step.get('name')}' is required = false, so CI "
                            f"cannot fail on it")
        for pred in ("skip_if_absent", "skip_if"):
            if step.get(pred):
                problems.append(f"{leg}: step '{step.get('name')}' has {pred}, so CI can pass "
                                f"it without running it (--skip it with a CI_SKIP reason)")
if problems:
    fail("CI's gate-runner wiring does not run the gated harnesses as required:\n  "
         + "\n  ".join(problems))
print(f"  [ok  ] both legs run gate-runner with --jobs; --skip sets match CI_SKIP "
      f"(Linux {len(CI_SKIP['Linux'])}, macOS {len(CI_SKIP['macOS'])})")


# --- mutation self-test: each check must be able to FAIL ----------------------------------
# A drift guard that cannot fail is decorative. Re-run this file against a fixture copy of the
# repo with ONE invariant broken, and require a non-zero exit carrying that check's message.
# Skipped inside a fixture run (LOCKSTEP_ROOT set) so it never recurses.
def _mutation_selftest():
    import shutil
    import subprocess
    import tempfile

    victim = sorted(gates_h)[0]
    step_line = f'run = "python3 {victim}"'
    linux_run = "run: python3 scripts/gate-runner.py --jobs 4 --skip shellcheck --skip ruff\n"
    mac_run = ("run: python3 scripts/gate-runner.py --jobs 4 --skip shellcheck --skip ruff "
               "--skip test-orchestrate-setup\n")
    gates, ci = ".gates.toml", ".github/workflows/ci.yml"
    cases = [
        # (label, file to mutate, old text, new text, message the failure must carry).
        ("gates step removed", gates, step_line, 'run = "true"', "runs NONE of them"),
        ("echo'd harness", gates, step_line, f'run = "echo python3 {victim}"', "unsupported shape"),
        ("soft harness", gates, step_line, step_line + "\n  required = false", "required = false"),
        ("predicated harness", gates, step_line, step_line + '\n  skip_if_absent = "nope"',
         "has skip_if_absent"),
        ("linux leg dropped", ci, linux_run, "run: true\n", "no gate-runner invocation for Linux"),
        ("mac leg dropped", ci, mac_run, "run: true\n", "no gate-runner invocation for macOS"),
        ("jobs dropped", ci, mac_run, mac_run.replace("--jobs 4 ", ""), "--jobs N"),
        ("extra skip", ci, linux_run, linux_run.replace("ruff", f"ruff --skip {victim[:-3]}"),
         "CI_SKIP gives no reason"),
        ("skip dropped", ci, mac_run, mac_run.replace(" --skip test-orchestrate-setup", ""),
         "CI_SKIP lists steps ci.yml does not --skip"),
        ("continue-on-error", ci, linux_run, linux_run + "        continue-on-error: true\n",
         "continue-on-error"),
        ("odd condition", ci, "if: runner.os == 'macOS'\n        run: python3 scripts/gate-runner",
         "if: false\n        run: python3 scripts/gate-runner", "unrecognized condition"),
        ("duplicate step name", gates, step_line, step_line + f'\n\n  [[prep_pr.steps]]\n  name = "ruff"\n  run = "python3 {victim}"',
         "step names are not unique"),
        ("unnamed step", gates, step_line, step_line + '\n\n  [[prep_pr.steps]]\n  run = "true"',
         "without an explicit `name`"),
        ("shellcheck drift", ci, "scripts/stack-preflight.sh\n", "\n", "NOT linted by CI"),
        ("ruff drift", ci, "test-settings-scrub.py test-stack-preflight.py",
         "test-settings-scrub.py", "NOT linted by CI"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for label, rel, old, new, want in [(None, None, None, None, None)] + cases:
            fx = os.path.join(tmp, label.replace(" ", "-").replace("'", "") if label else "clean")
            os.makedirs(os.path.join(fx, ".github", "workflows"))
            os.makedirs(os.path.join(fx, "scripts"))
            for f in (gates, ci):
                shutil.copy(os.path.join(ROOT, f), os.path.join(fx, f))
            for p in glob.glob(os.path.join(ROOT, "scripts", "*.sh")) + glob.glob(os.path.join(ROOT, "test-*.py")):
                dst = os.path.join(fx, os.path.relpath(p, ROOT))
                open(dst, "w").close()   # presence is all the filesystem checks read
            if rel:
                path = os.path.join(fx, rel)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                if text.count(old) != 1:
                    fail(f"mutation self-test: {old!r} is not unique in {rel}; update the case")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text.replace(old, new))
            r = subprocess.run([sys.executable, os.path.abspath(__file__)], capture_output=True,
                               text=True, env={**os.environ, "LOCKSTEP_ROOT": fx})
            out = r.stdout + r.stderr
            if label is None:
                if r.returncode != 0:
                    fail(f"mutation self-test: the UNMUTATED fixture failed, so the fixture is "
                         f"broken and no mutation result means anything:\n{out}")
            elif r.returncode == 0 or want not in out:
                fail(f"mutation self-test: '{label}' did not fail with '{want}' "
                     f"(rc={r.returncode}); the check it targets has no teeth:\n{out}")
    print(f"  [ok  ] mutation self-test: {len(cases)} broken fixtures each fail, clean passes")


if not os.environ.get("LOCKSTEP_ROOT"):
    _mutation_selftest()

print("\nok: CI lints in lockstep with .gates.toml and runs its harnesses via gate-runner")
