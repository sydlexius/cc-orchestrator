---
description: "Report reclaimable build-cache disk space (npm, Rust target/ dirs) and, on request, reclaim named targets"
argument-hint: "[--root <dir>] [reclaim: a name or rust-project path to clean]"
allowed-tools: ["Bash"]
---

# Reclaim build-cache disk space

Report disk usage and the reclaimable build caches, then - only if the user names targets -
reclaim them via each toolchain's own clean command. Report-first and safe by construction:
nothing is deleted unless explicitly named. Go is intentionally omitted (its build cache
self-trims; there is no surgical modcache reclaim).

**Arguments:** $ARGUMENTS

---

## Step 1 -- Locate the helper

```bash
PL=""
if [ -f scripts/cache-reclaim.sh ]; then PL=scripts/cache-reclaim.sh
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "${CLAUDE_PLUGIN_ROOT}/scripts/cache-reclaim.sh" ]; then PL="${CLAUDE_PLUGIN_ROOT}/scripts/cache-reclaim.sh"
else echo "cache-reclaim.sh not found (reinstall/update the plugin)"; fi
```

If `PL` is empty, stop here (the helper is not available). Every command below is guarded on it.

---

## Step 2 -- Report (always first)

First, parse an optional scan root from `$ARGUMENTS` (the user may pass `--root <dir>`, e.g.
`--root ~/Developer`, to scan a wider tree than the current repo):

```bash
root=""
case " $ARGUMENTS " in
  *" --root "*) root=$(printf '%s' "$ARGUMENTS" | awk '{for(i=1;i<NF;i++) if($i=="--root"){print $(i+1); exit}}') ;;
esac
```

Run the report. By default it scans the current repo for Rust `target/` dirs; the `--root <dir>`
above widens the scan. It measures the npm cache,
each Rust project's `target/` dir (the real Rust disk hog, regenerable), and the cargo
registry, and prints the exact toolchain command to reclaim each - it does NOT clean anything.

```bash
# Pass --root only when a scan root was requested; build the args explicitly (a
# `${root:+--root "$root"}` one-liner collapses "--root <path>" into a single arg).
if [ -n "$PL" ]; then
  if [ -n "${root:-}" ]; then bash "$PL" --report --root "$root"; else bash "$PL" --report; fi
fi
```

Present the report to the user. Point out the biggest reclaimable items and note that npm and
Rust `target/` dirs are safe to reclaim (they regenerate), while the go build cache is omitted
because it self-manages.

---

## Step 3 -- Reclaim (only when the user names targets)

Do NOT reclaim anything unless the user asked (in `$ARGUMENTS` or in reply to the report).
When they name targets, pass them to `--yes` as a comma-separated list. Each is one of:

- `npm` -> `npm cache verify` (light GC, keeps the cache working)
- `npm=force` -> `npm cache clean --force` (full wipe; only if verify didn't reclaim enough)
- a Rust project directory path (as printed in the report) -> `cargo clean` for that project
- `cargo-registry` -> **report-only**: the helper prints the `cargo cache --autoclean` command for
  you to run yourself (it never mutates the registry; install the plugin once with
  `cargo install cargo-cache`)

```bash
bash "$PL" --yes "<name-or-path>[,<name-or-path>...]"
```

The helper only ever runs the toolchain's own clean command (never a hand-rolled `rm`) and
skips anything it cannot identify. Confirm with the user before reclaiming a project's `target/`
that they are not currently building it (the rebuild will be cold).

---

## Notes

- This is on-demand disk hygiene, not a merge-time action. `/post-merge-cleanup` only prints a
  one-line advisory when the disk is nearly full ("run /reclaim-cache"); it never cleans caches.
- The helper reads only in report mode; the sole mutation is a `--yes`-gated toolchain clean. It
  makes no git/gh/network change and touches no cache outside the ones it reports.
