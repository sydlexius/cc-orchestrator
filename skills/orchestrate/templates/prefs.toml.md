# `.prefs.toml` schema

`.prefs.toml` is a per-repo **UI-preference verification manifest** that lives at a
repo's root. For every user-configurable UI preference, it records the concrete
mechanism a rendered surface must wire into to honor that preference, plus a
deterministic `verify` check. The adversarial-review charter reads it (the
USER-PREFERENCE COVERAGE dimension: "read a prefs manifest first if the project
ships one"), and a future consumer (Phase 2) turns it into a deterministic
pref-coverage gate.

**What it IS:** a map from each pref to *how you verify a surface honors it*. It
POINTS at the repo's authoritative pref list; it does not copy it.

**What it ISN'T:** a re-declaration of prefs + values. Re-declaring would create a
second source of truth that drifts from repos that already have a pref registry
(e.g. a server-side preference registry with its own drift test). `.prefs.toml`
adds the pref -> application-mechanism map that such registries do NOT capture --
the map that today lives scattered across attribute-mapping code + CSS selectors,
and whose absence let a UI surface silently ignore the layout-density and
mono-font prefs (stillwater m55 #1338).

`.prefs.toml` is TRUSTED repo configuration, on the same footing as `.gates.toml`
or a `Makefile`: its `verify` patterns are greps the repo author wrote. It grants
no privilege and does not touch the deterministic floor.

When `.prefs.toml` is ABSENT, the consumer self-skips (like the coverage
`status:none` self-skip) -- a repo with no user prefs (e.g. a dark-only UI) needs
no manifest, and that is valid, not an error.

---

## `[source]` section

Where the AUTHORITATIVE list of pref keys lives. The reviewer/consumer reads the
canonical pref names from here -- never from memory, and never from the `[[pref]]`
blocks below (those are a coverage subset, not the source of truth). Used to
enumerate prefs the manifest omitted and to drift-check that every `[[pref]].key`
still exists.

| Key        | Type   | Meaning |
|------------|--------|---------|
| `list_cmd` | string | (optional) A command that emits the authoritative pref keys, one per line (e.g. a codegen tool with a `--keys` flag). Preferred: it can never drift. |
| `file`     | string | (optional) A file a reviewer reads for the pref definitions (a schema, a typed model/registry, a settings struct). Human-read fallback when there is no `list_cmd`. |
| `docs`     | string | (optional) A generated human reference (e.g. a `preferences.md`). |

At least one of `list_cmd` / `file` should be set for a repo that has user prefs.

---

## `[[pref]]` section

One table per USER-CONFIGURABLE pref that a UI surface must VISIBLY honor.
Non-visual prefs (page size, language, notification toggles, server-side flags)
are simply OMITTED -- they have no rendered surface to cover.

| Key           | Type   | Default   | Meaning |
|---------------|--------|-----------|---------|
| `key`         | string | (req.)    | The pref name. MUST match a key from `[source]` (drift-checked). |
| `applies_via` | string | (req.)    | The mechanism kind. One of the five below. |
| `mechanism`   | string | (req.)    | Human-readable: the concrete token/attr/class/selector a surface wires into to honor the pref. |
| `verify`      | string | (req.)    | A regex the consumer greps against the changed surface's markup/code. Presence => the surface plausibly honors the pref; absence => a finding (adjudicated per the exemption + hard-gate rules below). |
| `severity`    | string | `medium`  | `high` / `medium` / `low` -- the finding severity when a surface misses this pref. |

### `applies_via` mechanisms (one real example of each, from surveyed repos)

| `applies_via`     | How a surface honors the pref | Example repo | Example `mechanism` / `verify` |
|-------------------|-------------------------------|--------------|--------------------------------|
| `data-attr`       | An ancestor sets `[data-x]`; the surface consumes the driven CSS var | stillwater `density` | `mechanism = "[data-density]; consume var(--sw-density-*)"`, `verify = "var\\(--sw-density-"` |
| `css-var`         | The surface consumes a CSS custom property directly | stillwater `bg_opacity` | `mechanism = "var(--sw-glass-bg)"`, `verify = "var\\(--sw-glass-bg"` |
| `class`           | The surface carries a modifier class the pref toggles | stillwater `theme` (`.dark`) | `mechanism = ".dark selectors + var(--sw-content-*)"`, `verify = "var\\(--sw-content-"` |
| `tailwind-variant`| Every relevant utility has a variant counterpart | media-reaper `theme` | `mechanism = "every color utility has a dark: counterpart"`, `verify = "dark:"` |
| `store-selector`  | A component reads a store/state selector and derives output (code-grep; WEAKER static signal -- lean on the charter's rendered pass) | genogram `density` | `mechanism = "reads store.spacing; derives sizes"`, `verify = "store\\.spacing"` |

---

## `[[exempt]]` section

The escape hatch for the hard gate (below): a changed surface that legitimately
need not honor a pref (a static text error page has no spacing to compact; a
single-color glyph has no theme colors) is listed here with a REASON, instead of
being forced to wire in an irrelevant mechanism. The list is itself reviewable --
a maintainer can challenge any exemption.

| Key       | Type            | Meaning |
|-----------|-----------------|---------|
| `surface` | string          | A path glob (matched against the diff's changed-surface paths). |
| `prefs`   | array of string | The pref keys this surface need not honor (must be `[[pref]].key`s). |
| `reason`  | string  (req.)  | One line justifying the skip -- surfaced by the consumer and reviewable. |

```toml
[[exempt]]
surface = "web/templates/errors/*.templ"
prefs   = ["density", "font_family"]
reason  = "static text error pages; no spacing/typography surface to vary"
```

---

## Semantics (how the Phase-2 consumer reads this)

- **Hard gate + opt-out.** For each `[[pref]]`, the consumer greps each
  DIRECTLY-CHANGED surface for `verify`. MISSING is a HARD failure UNLESS the
  surface matches an `[[exempt]]` block covering that pref (the reason is
  printed). No silent gaps; every miss is a fix or a justified exemption.
- **Narrow surface.** Only the diff's directly-changed files are grepped
  (deterministic, language-agnostic). A pref honored via an included partial is a
  rare false positive -> resolve with an `[[exempt]]` reason. Include-graph
  resolution is a future enhancement. (Phase 2 should distinguish an
  "honored-elsewhere" annotation from a true `[[exempt]]` skip, so a
  false-positive suppression does not muddy the reviewable exemption surface.)
- **Necessary, not sufficient.** `verify` is the static (deterministic-first)
  check; it proves a surface REFERENCES the mechanism, not that it APPLIES it
  correctly. Correct application is confirmed by the charter's rendered Playwright
  pref-cycle (rendered-second). `store-selector` `verify` is a code-grep and thus
  a weaker signal -- lean harder on the rendered pass there.
- **Self-skip.** No `.prefs.toml` -> the check self-skips (not a failure).

Phase 1 (this schema) defines the format and is validated by the worked example
below; the enforcing consumer is Phase 2.

---

## Worked example -- stillwater (grounded in its real prefs)

stillwater already has an authoritative pref registry (`internal/api/preference_registry.go`),
a JS mirror validated by a drift test, and generated docs -- so `.prefs.toml`
points at that registry via `[source]` and adds ONLY the pref -> mechanism map
(from `web/static/js/preferences.js` `ATTR_MAP` + `web/static/css/design-tokens.css`).
Non-visual keys (`language`, `page_size`, `notification_enabled`,
`auto_fetch_images`, `metadata_*`, `show_platform_debug`) are omitted.

```toml
[source]
file = "internal/api/preference_registry.go"           # canonical PreferenceRegistry()
docs = "docs/site/src/reference/preferences.md"         # generated human reference

[[pref]]
key = "theme"          # data-theme + .dark
applies_via = "class"
mechanism = ".dark selectors + var(--sw-content-*/--sw-sidebar-*)"
verify = "\\bdark\\b|var\\(--sw-content-|var\\(--sw-sidebar-"
severity = "high"

[[pref]]
key = "density"        # data-density
applies_via = "data-attr"
mechanism = "[data-density]; consume var(--sw-density-row-py|gap|card-py|detail-gap)"
verify = "var\\(--sw-density-"
severity = "high"

[[pref]]
key = "font_family"    # data-font-family
applies_via = "data-attr"
mechanism = "[data-font-family]; font-family: var(--sw-font-sans/--sw-font-family)"
verify = "var\\(--sw-font-(sans|family)"
severity = "high"

[[pref]]
key = "mono_font"      # data-mono
applies_via = "data-attr"
mechanism = "[data-mono]; font-family: var(--sw-font-mono) on kbd/ids/timestamps"
verify = "var\\(--sw-font-mono"
severity = "medium"

[[pref]]
key = "content_width"  # data-width
applies_via = "data-attr"
mechanism = "[data-width] main; max-width: var(--sw-content-max-width)"
verify = "var\\(--sw-content-max-width"
severity = "medium"

[[pref]]
key = "thumbnail_size" # data-thumbnail-size
applies_via = "data-attr"
mechanism = "[data-thumbnail-size]; var(--sw-thumb-size/--sw-thumb-grid-min)"
verify = "var\\(--sw-thumb-"
severity = "low"

[[pref]]
key = "lite_mode"      # data-lite (glass/blur)
applies_via = "data-attr"
mechanism = "[data-lite]; var(--sw-glass-blur) on .sw-glass/.glass-noise"
verify = "var\\(--sw-glass-blur|sw-glass"
severity = "low"

[[pref]]
key = "bg_opacity"     # inline --sw-glass-bg
applies_via = "css-var"
mechanism = "var(--sw-glass-bg) on .sw-glass/.sw-card/.sw-sidebar"
verify = "var\\(--sw-glass-bg"
severity = "low"

[[pref]]
key = "reduced_motion" # data-motion
applies_via = "data-attr"
mechanism = "animations gated by [data-motion] / @media (prefers-reduced-motion)"
verify = "data-motion|prefers-reduced-motion"
severity = "medium"

[[exempt]]
surface = "web/templates/**/errors/*.templ"
prefs   = ["density", "thumbnail_size"]
reason  = "static error pages; no spacing or thumbnail grid to vary"
```

**Would this have caught #1338?** Yes. The next/ logs viewer added its root class
to the color-token scope group but not the density/font groups, so its markup
consumed neither `var(--sw-density-*)` nor `var(--sw-font-mono)`. Against the
`density` and `mono_font` `[[pref]]` entries above, the consumer would grep the
changed logs-viewer template, find neither `verify` pattern, and -- since the
surface is not in `[[exempt]]` -- fail with a MISSING finding for both prefs.
That is exactly the gap the maintainer caught only at UAT.

> Note: the `verify` patterns and mechanisms here are derived from a survey of
> stillwater's actual `preference_registry.go` / `preferences.js` ATTR_MAP /
> `design-tokens.css`. The real `.prefs.toml` lands + is CI-verified in the
> stillwater repo (the pilot task), not here.
