#!/usr/bin/env python3
"""Drift-guard: assert that SKILL.md **Version X.Y.Z** and plugin.json "version"
are identical. Exits 1 with a clear message on mismatch. Run: python3 test-version-lockstep.py"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SKILL_MD = os.path.join(ROOT, "skills", "orchestrate", "SKILL.md")
PLUGIN_JSON = os.path.join(ROOT, ".claude-plugin", "plugin.json")


def skill_version():
    try:
        with open(SKILL_MD, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        sys.exit(f"FAIL: cannot read {SKILL_MD}: {e}")
    m = re.search(r'^\*\*Version\s+(\S+)\*\*', text, re.MULTILINE)
    if not m:
        sys.exit(f"FAIL: no **Version X.Y.Z** line found in {SKILL_MD}")
    return m.group(1)


def plugin_version():
    try:
        with open(PLUGIN_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        sys.exit(f"FAIL: cannot read {PLUGIN_JSON}: {e}")
    except json.JSONDecodeError as e:
        sys.exit(f"FAIL: {PLUGIN_JSON} is not valid JSON: {e}")
    v = data.get("version")
    if not v:
        sys.exit(f"FAIL: no 'version' field in {PLUGIN_JSON}")
    return v


def main():
    sv = skill_version()
    pv = plugin_version()
    if sv == pv:
        print(f"ok: SKILL.md and plugin.json both at version {sv}")
        sys.exit(0)
    sys.exit(
        f"FAIL: version lockstep drift detected\n"
        f"  SKILL.md **Version**:  {sv}\n"
        f"  plugin.json version:   {pv}\n"
        f"Bump BOTH together. SKILL.md is the source of truth; plugin.json drives "
        f"/plugin marketplace update-detection."
    )


if __name__ == "__main__":
    main()
