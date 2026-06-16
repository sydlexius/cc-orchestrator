#!/usr/bin/env python3
"""Proof harness for orchestrate-setup.py. Drives the CLI against temp fixtures
(temp settings/marker/guard/templates/artifact dirs) so the real env is never touched.
Run: python3 test-orchestrate-setup.py"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time


def _key(tmux):
    # Byte-mode, mirroring the guard's LC_ALL=C tr and setup's _session_key.
    return re.sub(rb'[^A-Za-z0-9]', b'_', tmux.encode("utf-8", "surrogateescape")).decode("ascii")


TEST_TMUX = "/tmp/tmux-test,1,0"

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "orchestrate-setup.py")
FAILS = []


def write_stub_guard(path, selftest_rc=0):
    """A fake guard mimicking the real floor for up's armed self-test:
      - `--self-test` exits selftest_rc.
      - push-main payload -> Tier-1 HARD DENY (exit 2).
      - merge-by-API payload (`gh api ... pulls/N/merge`) -> Tier-2 HARD DENY (exit 2).
      - `gh pr merge` CLI payload -> Tier-2 HARD DENY (exit 2, #105).
      - anything else -> exit 0.
    Written in Python (not bash) to parse the stdin JSON properly - avoids the shell
    case-pattern quoting traps a bash stub is prone to."""
    pr_merge = "gh pr " + "merge"   # assembled from pieces; no trigger string on this line
    with open(path, "w") as f:
        f.write(
            "#!/usr/bin/env python3\n"
            "import sys, json\n"
            f"if len(sys.argv) > 1 and sys.argv[1] == '--self-test':\n"
            f"    sys.exit({selftest_rc})\n"
            "try:\n"
            "    cmd = json.loads(sys.stdin.read())['tool_input']['command']\n"
            "except Exception:\n"
            "    sys.exit(0)\n"
            "if 'git push origin main' in cmd:\n"
            "    sys.exit(2)\n"
            "if 'pulls/' in cmd and '/merge' in cmd:\n"
            "    sys.exit(2)\n"
            f"if {pr_merge!r} in cmd:\n"
            "    sys.exit(2)\n"
            "sys.exit(0)\n")
    os.chmod(path, 0o755)


def run(args, *, env_overrides=None, tmux=True):
    """Invoke the CLI. Returns (returncode, stdout+stderr)."""
    env = dict(os.environ)
    env.pop("TOOL_INPUT", None)
    if tmux:
        # Force the fixture TMUX unconditionally (not `env.get("TMUX") or ...`): when the
        # harness itself runs INSIDE a real tmux (e.g. a teammate pane), inheriting the
        # ambient $TMUX would key the floor marker to a value other than TEST_TMUX and the
        # marker-arming checks would spuriously fail. Pin it so the harness is deterministic
        # regardless of the ambient tmux.
        env["TMUX"] = TEST_TMUX
    else:
        env.pop("TMUX", None)
    if env_overrides:
        env.update(env_overrides)
    p = subprocess.run([sys.executable, SCRIPT, *args], env=env,
                       capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout + p.stderr


def check(label, cond):
    status = "ok" if cond else "FAIL"
    if not cond:
        FAILS.append(label)
    print(f"  [{status}] {label}")


def main():
    with tempfile.TemporaryDirectory() as td:
        good = os.path.join(td, "settings.json")
        json.dump({"teammateMode": "tmux", "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}}, open(good, "w"))

        # Task 3 guard + wired settings defined early so Task 2 PASS case can use them.
        guard = os.path.join(td, "guard.sh"); write_stub_guard(guard, selftest_rc=0)
        wired = os.path.join(td, "wired.json")
        json.dump({"teammateMode": "tmux", "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
                   "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                       {"type": "command", "command": 'bash "$HOME/.claude/scripts/orchestrate-guard.sh"'}]}]}},
                  open(wired, "w"))

        # Skeleton: doctor against a fully-healthy fixture exits 0 with "no hard fail".
        # Run with explicit fixtures (NOT the bare ambient env) so it is host-independent:
        # a CI runner has no ~/.claude wiring, so an unconfigured default correctly hard-fails.
        rc, out = run(["doctor"], env_overrides={"ORCHESTRATE_SETTINGS": wired, "ORCHESTRATE_GUARD": guard}, tmux=True)
        check("doctor skeleton (healthy fixture) exits 0", rc == 0)
        check("doctor prints no-hard-fail", "no hard fail" in out)

        rc, out = run(["doctor"], env_overrides={"ORCHESTRATE_SETTINGS": wired, "ORCHESTRATE_GUARD": guard}, tmux=True)
        check("teams+tmux PASS -> no hard fail (rc0)", rc == 0 and "Agent Teams enabled" in out)
        rc, out = run(["doctor"], env_overrides={"ORCHESTRATE_SETTINGS": wired, "ORCHESTRATE_GUARD": guard}, tmux=False)
        check("not in tmux -> hard fail (rc1)", rc == 1 and "NOT inside tmux" in out)
        bad = os.path.join(td, "bad.json")
        json.dump({"teammateMode": "acceptEdits"}, open(bad, "w"))
        rc, out = run(["doctor"], env_overrides={"ORCHESTRATE_SETTINGS": bad, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "", "ORCHESTRATE_GUARD": guard}, tmux=True)
        check("teams off -> hard fail (rc1)", rc == 1 and "Agent Teams not ready" in out)

        rc, out = run(["doctor"], env_overrides={"ORCHESTRATE_SETTINGS": wired, "ORCHESTRATE_GUARD": guard})
        check("guard wired + healthy -> PASS", "guard wired" in out and "self-test passes" in out)
        rc, out = run(["doctor"], env_overrides={"ORCHESTRATE_SETTINGS": good, "ORCHESTRATE_GUARD": guard})
        check("guard not wired -> hard fail + prints JSON", rc == 1 and "guard NOT wired" in out and "hooks.PreToolUse" in out)
        badguard = os.path.join(td, "badguard.sh"); write_stub_guard(badguard, selftest_rc=1)
        rc, out = run(["doctor"], env_overrides={"ORCHESTRATE_SETTINGS": wired, "ORCHESTRATE_GUARD": badguard})
        check("guard self-test fails -> hard fail", rc == 1 and "self-test FAILED" in out)

        repo = os.path.join(td, "repo"); os.makedirs(repo)
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        tpl = os.path.join(td, "templates"); os.makedirs(tpl)
        # required-permissions fixture with a real allow-list section, a //tmp entry (double-slash
        # idiom), a trailing ## section containing a "Do NOT add" line and a NOTE: line - so we
        # can verify those negative-context entries are NOT reported missing.
        open(os.path.join(tpl, "required-permissions.md"), "w").write(
            "## Needed allow-list entries\n"
            "- `Bash(gh pr *)`\n"
            "- `Bash(zzz-missing *)`\n"
            "- `Write(//tmp/**)`\n"
            "## Guardrails\n"
            "- Do NOT add `Bash(jq *)` just for stack edits\n"
            "- NOTE: `Bash(go *)` is discussed here but not prescribed\n"
        )
        ov = {"ORCHESTRATE_SETTINGS": wired, "ORCHESTRATE_GUARD": guard, "ORCHESTRATE_TEMPLATES_DIR": tpl}
        rc, out = run(["doctor", "--repo", repo], env_overrides=ov)
        check("clean repo -> PASS with HEAD", "clean at HEAD=" in out)
        check("allow-list missing entry -> WARN (not hard fail)", "MISSING" in out and "zzz-missing" in out and rc == 0)
        # The //tmp entry should normalize to /tmp/** and match the settings allow (or just not
        # appear as a false positive if the settings has it). The Do NOT / NOTE lines in the
        # Guardrails section must NOT surface as missing entries.
        check("guardrails section entries NOT reported missing",
              "jq" not in out or "Do NOT" not in out)
        check("NOTE: line entries NOT reported missing", "go *)" not in out or "NOTE" not in out)
        open(os.path.join(repo, "dirt.txt"), "w").write("x")
        rc, out = run(["doctor", "--repo", repo], env_overrides=ov)
        check("dirty repo -> WARN not FAIL (rc0)", "DIRTY" in out and rc == 0)

        # Task 5: up scaffolds artifacts
        art = os.path.join(td, "artifacts"); os.makedirs(art)
        os.makedirs(os.path.join(tpl, "x"), exist_ok=True)
        # Use the real template token syntax: <REPO>, <STACK>, <SPACING_MIN> (no {{...}}).
        open(os.path.join(tpl, "pr-shipper-brief.md"), "w").write(
            "Stack: <STACK>\nRepo: <REPO>\nPacing: <SPACING_MIN> minutes\n")
        floor_dir = os.path.join(td, "floor.d")
        # Add a GitHub remote to the repo fixture so scaffold_artifacts can derive the slug.
        subprocess.run(["git", "-C", repo, "remote", "add", "origin",
                        "https://github.com/testowner/testrepo.git"], check=True)
        upov = dict(ov); upov.update({"ORCHESTRATE_ARTIFACT_DIR": art, "ORCHESTRATE_FLOOR_DIR": floor_dir})
        rc, out = run(["up", "--team", "demo", "--repo", repo], env_overrides=upov)
        # P3-A /tmp-namespacing: all artifacts live under ARTIFACTS/<team>/ (per-team dir),
        # stack drops its <team>- prefix (the dir carries the team identity).
        team_dir = os.path.join(art, "demo")
        stack = os.path.join(team_dir, "stack.json")
        check("up scaffolds <team>/stack.json=[]", os.path.exists(stack) and json.load(open(stack)) == [])
        check("up creates <team>/pr-triage dir", os.path.isdir(os.path.join(team_dir, "pr-triage")))
        check("up creates <team>/adv-review dir", os.path.isdir(os.path.join(team_dir, "adv-review")))
        planner_seed = os.path.join(team_dir, "planner", "proposed.json")
        check("#11 up scaffolds <team>/planner/proposed.json={\"flags\": []}",
              os.path.exists(planner_seed) and json.load(open(planner_seed)) == {"flags": []})
        brief = open(os.path.join(team_dir, "pr-shipper-brief.md")).read()
        # Verify: brief body contains the owner/name SLUG (derived from the remote), not the
        # raw path. The header comment records the path for diagnostics, so we check the
        # body lines (everything after the first line) for the slug and absence of raw path.
        brief_body = "\n".join(brief.splitlines()[1:])
        check("brief substitutions rendered",
              "testowner/testrepo" in brief_body and
              stack in brief_body and
              "<REPO>" not in brief_body and
              "<STACK>" not in brief_body and
              "<SPACING_MIN>" not in brief_body)
        check("brief body contains slug not raw path",
              "testowner/testrepo" in brief_body and repo not in brief_body)

        # P3-A: two parallel teams must NOT clobber each other (disjoint per-team dirs).
        rc, out = run(["up", "--team", "beta", "--repo", repo], env_overrides=upov)
        beta_stack = os.path.join(art, "beta", "stack.json")
        check("second team gets its own dir", os.path.exists(beta_stack))
        check("first team's stack untouched by second team's up",
              os.path.exists(stack) and json.load(open(stack)) == [])
        check("teams have disjoint triage dirs",
              os.path.isdir(os.path.join(art, "demo", "pr-triage")) and
              os.path.isdir(os.path.join(art, "beta", "pr-triage")))

        # F2(c): up captures whichever ORCHESTRATE_STILLWATER_{KEYFILE,MUSIC,DB} are set
        # in the env at up-time and persists them to <team-dir>/profile.env (0600, eval-able
        # `export K=V` lines) so allocate can read them without re-exporting every session.
        # These are PATHS (not secret material), so 0600 is hygiene, not a secrecy boundary.
        swkey = os.path.join(td, "real.key"); open(swkey, "w").write("K\n")
        swmusic = os.path.join(td, "music")
        swdb = os.path.join(td, "src.db")
        pe_ov = dict(upov)
        pe_ov.update({"ORCHESTRATE_STILLWATER_KEYFILE": swkey,
                      "ORCHESTRATE_STILLWATER_MUSIC": swmusic,
                      "ORCHESTRATE_STILLWATER_DB": swdb})
        rc, out = run(["up", "--team", "swteam", "--repo", repo], env_overrides=pe_ov)
        prof = os.path.join(art, "swteam", "profile.env")
        check("up writes <team-dir>/profile.env when stillwater env is set", rc == 0 and os.path.isfile(prof))
        prof_body = open(prof).read() if os.path.isfile(prof) else ""
        check("profile.env persists KEYFILE/MUSIC/DB as export lines",
              f"export ORCHESTRATE_STILLWATER_KEYFILE={swkey}" in prof_body and
              f"export ORCHESTRATE_STILLWATER_MUSIC={swmusic}" in prof_body and
              f"export ORCHESTRATE_STILLWATER_DB={swdb}" in prof_body)
        check("profile.env is 0600",
              os.path.isfile(prof) and (os.stat(prof).st_mode & 0o777) == 0o600)
        # Only SET keys are persisted; an unset optional key (DB) is omitted, not blank.
        marker_demo = os.path.join(floor_dir, _key(TEST_TMUX))
        if os.path.exists(marker_demo):
            os.remove(marker_demo)
        pe_ov2 = dict(upov)
        pe_ov2.update({"ORCHESTRATE_STILLWATER_KEYFILE": swkey,
                       "ORCHESTRATE_STILLWATER_MUSIC": swmusic})
        pe_ov2.pop("ORCHESTRATE_STILLWATER_DB", None)
        rc, out = run(["up", "--team", "swteam2", "--repo", repo], env_overrides=pe_ov2)
        prof2 = os.path.join(art, "swteam2", "profile.env")
        prof2_body = open(prof2).read() if os.path.isfile(prof2) else ""
        check("absent optional DB key omitted from profile.env",
              "ORCHESTRATE_STILLWATER_KEYFILE" in prof2_body and
              "ORCHESTRATE_STILLWATER_DB" not in prof2_body)
        # up with NO stillwater env still succeeds (profile.env empty or just absent keys).
        marker_demo = os.path.join(floor_dir, _key(TEST_TMUX))
        if os.path.exists(marker_demo):
            os.remove(marker_demo)
        pe_ov3 = dict(upov)
        for k in ("ORCHESTRATE_STILLWATER_KEYFILE", "ORCHESTRATE_STILLWATER_MUSIC",
                  "ORCHESTRATE_STILLWATER_DB"):
            pe_ov3.pop(k, None)
        rc, out = run(["up", "--team", "noenv", "--repo", repo], env_overrides=pe_ov3)
        check("up with no stillwater env still succeeds (rc0)", rc == 0 and "SESSION ARMED" in out)

        # #123: up emits a loud stale-guard WARNING when ORCHESTRATE_BUNDLED_GUARD differs
        # from the deployed ORCHESTRATE_GUARD, but still exits 0 (loud-non-fatal design).
        # When they match, up is quiet (no WARNING block).
        #
        # Approach: use ORCHESTRATE_BUNDLED_GUARD to point at an alternate file, and
        # ORCHESTRATE_GUARD at the existing stub guard (`guard`). A stale "bundle" (different
        # content) triggers the warning; an identical "bundle" (copy of the deployed) does not.
        stale_bundle = os.path.join(td, "stale_bundle.sh")
        open(stale_bundle, "w").write("#!/bin/sh\n# stale bundled guard (different)\nexit 0\n")
        os.chmod(stale_bundle, 0o755)
        fresh_bundle = os.path.join(td, "fresh_bundle.sh")
        import shutil as _shutil
        _shutil.copy2(guard, fresh_bundle)

        marker_demo = os.path.join(floor_dir, _key(TEST_TMUX))
        if os.path.exists(marker_demo):
            os.remove(marker_demo)
        stale_ov = dict(upov); stale_ov["ORCHESTRATE_BUNDLED_GUARD"] = stale_bundle
        rc, out = run(["up", "--team", "stale_guard_team", "--repo", repo], env_overrides=stale_ov)
        check("#123: up warns (stale bundle) - loud WARNING block in output", "WARNING: STALE FLOOR GUARD" in out)
        check("#123: up warns (stale bundle) - names the configure remedy", "configure --apply" in out)
        check("#123: up warns (stale bundle) - names restart remedy", "RESTART" in out)
        check("#123: up warns (stale bundle) - still exits 0 (non-fatal)", rc == 0)
        check("#123: up warns (stale bundle) - still arms session (SESSION ARMED)", "SESSION ARMED" in out)

        marker_demo = os.path.join(floor_dir, _key(TEST_TMUX))
        if os.path.exists(marker_demo):
            os.remove(marker_demo)
        fresh_ov = dict(upov); fresh_ov["ORCHESTRATE_BUNDLED_GUARD"] = fresh_bundle
        rc, out = run(["up", "--team", "fresh_guard_team", "--repo", repo], env_overrides=fresh_ov)
        check("#123: up quiet (matching bundle) - no WARNING block", "WARNING: STALE FLOOR GUARD" not in out)
        check("#123: up quiet (matching bundle) - exits 0", rc == 0)
        check("#123: up quiet (matching bundle) - arms session", "SESSION ARMED" in out)

        # #10: check_slack_channel() is a FORMAT-only, WARN-level, optional doctor check.
        # It reads ORCHESTRATE_SLACK_CHANNEL from env; it NEVER returns FAIL (the channel is
        # optional and must not block doctor/up). The healthy wired+guard fixture means doctor
        # overall is rc0, so the per-line status is what we assert. The slack line is identified
        # by the ORCHESTRATE_SLACK_CHANNEL token the _emit message carries.
        def slack_line(out):
            for ln in out.splitlines():
                if "ORCHESTRATE_SLACK_CHANNEL" in ln and ln.lstrip().startswith("["):
                    return ln
            return ""

        sov = {"ORCHESTRATE_SETTINGS": wired, "ORCHESTRATE_GUARD": guard}
        # Absent (empty -> treated as unset) -> WARN, terminal-only, doctor still rc0.
        rc, out = run(["doctor"], env_overrides={**sov, "ORCHESTRATE_SLACK_CHANNEL": ""})
        ln = slack_line(out)
        check("slack: absent -> WARN", "[WARN" in ln and "terminal-only" in ln)
        check("slack: absent -> doctor not hard-fail (rc0)", rc == 0)
        # F3-C-4 wiring: with the var absent, a slack WARN line MUST appear in doctor output -
        # proves check_slack_channel() is actually wired into cmd_doctor.
        check("slack: cmd_doctor wiring -> WARN line present when absent", ln != "")
        # Malformed ids (fail [A-Z][A-Z0-9]{5,}) -> WARN, never FAIL.
        for bad in ("123", "abc", "C12", "c0b8y401qr2", "C0B8Y/01QR2", "C0B8 401QR2"):
            rc, out = run(["doctor"], env_overrides={**sov, "ORCHESTRATE_SLACK_CHANNEL": bad})
            ln = slack_line(out)
            check(f"slack: malformed {bad!r} -> WARN", "[WARN" in ln and "well-formed" in ln)
            check(f"slack: malformed {bad!r} -> never FAIL", "[FAIL" not in ln and rc == 0)
        # Well-formed ids -> PASS.
        for good in ("C0B8Y401QR2", "G12345", "D0ABCDE", "W0B8Y401"):
            rc, out = run(["doctor"], env_overrides={**sov, "ORCHESTRATE_SLACK_CHANNEL": good})
            ln = slack_line(out)
            check(f"slack: well-formed {good!r} -> PASS", "[PASS" in ln)
            check(f"slack: well-formed {good!r} -> rc0", rc == 0)
        # Never FAIL regardless of value: no slack line ever emits a FAIL status.
        for val in ("", "x", "123", "C0B8Y401QR2"):
            rc, out = run(["doctor"], env_overrides={**sov, "ORCHESTRATE_SLACK_CHANNEL": val})
            check(f"slack: value {val!r} -> slack check never FAIL", "[FAIL" not in slack_line(out))

        # #89: check_slack_bot_user_id() mirrors the channel check - FORMAT-only, WARN-level,
        # optional, NEVER FAIL. Its line is identified by the ORCHESTRATE_SLACK_BOT_USER_ID token.
        def bot_line(out):
            for ln in out.splitlines():
                if "ORCHESTRATE_SLACK_BOT_USER_ID" in ln and ln.lstrip().startswith("["):
                    return ln
            return ""
        rc, out = run(["doctor"], env_overrides={**sov, "ORCHESTRATE_SLACK_BOT_USER_ID": ""})
        bln = bot_line(out)
        check("#89 bot-id: absent -> WARN (text-sentinel fallback)", "[WARN" in bln and "text sentinel" in bln)
        check("#89 bot-id: cmd_doctor wiring -> line present when absent", bln != "")
        check("#89 bot-id: absent -> doctor rc0", rc == 0)
        for bad in ("123", "abc", "U12", "u0bb8nueere", "U0BB/NUEERE", "U0BB NUEERE"):
            rc, out = run(["doctor"], env_overrides={**sov, "ORCHESTRATE_SLACK_BOT_USER_ID": bad})
            bln = bot_line(out)
            check(f"#89 bot-id: malformed {bad!r} -> WARN, never FAIL", "[WARN" in bln and "[FAIL" not in bln and rc == 0)
        for good in ("U0BB8NUEERE", "W0B8Y401", "U12345"):
            rc, out = run(["doctor"], env_overrides={**sov, "ORCHESTRATE_SLACK_BOT_USER_ID": good})
            check(f"#89 bot-id: well-formed {good!r} -> PASS", "[PASS" in bot_line(out) and rc == 0)

        # F2-B-2: ORCHESTRATE_SLACK_CHANNEL is NOT in PROFILE_ENV_KEYS, so `up` must NOT
        # write it to the team artifact dir's profile.env. Set it (and a stillwater key so
        # profile.env is actually written) then inspect the file content.
        marker_slack = os.path.join(floor_dir, _key(TEST_TMUX))
        if os.path.exists(marker_slack):
            os.remove(marker_slack)
        slack_up_ov = dict(upov)
        slack_up_ov.update({"ORCHESTRATE_STILLWATER_KEYFILE": swkey,
                            "ORCHESTRATE_SLACK_CHANNEL": "C0B8Y401QR2"})
        rc, out = run(["up", "--team", "slackteam", "--repo", repo], env_overrides=slack_up_ov)
        slack_prof = os.path.join(art, "slackteam", "profile.env")
        slack_prof_body = open(slack_prof).read() if os.path.isfile(slack_prof) else ""
        check("slack: up succeeded with channel set (rc0)", rc == 0 and os.path.isfile(slack_prof))
        check("slack: ORCHESTRATE_SLACK_CHANNEL NOT written to team profile.env (F2-B-2)",
              "ORCHESTRATE_SLACK_CHANNEL" not in slack_prof_body and "C0B8Y401QR2" not in slack_prof_body)

        # P3-A: an invalid --team (path-escape / separators) is rejected cleanly, no dir made.
        rc, out = run(["up", "--team", "a/b", "--repo", repo], env_overrides=upov)
        check("up rejects --team with a slash (no traceback)",
              rc != 0 and "Traceback" not in out and not os.path.isdir(os.path.join(art, "a")))
        rc, out = run(["up", "--team", "..", "--repo", repo], env_overrides=upov)
        check("up rejects --team '..' (no traceback)", rc != 0 and "Traceback" not in out)
        rc, out = run(["up", "--team=-rf", "--repo", repo], env_overrides=upov)
        check("up rejects leading-dash --team (no traceback)",
              rc != 0 and "Traceback" not in out and not os.path.isdir(os.path.join(art, "-rf")))

        # Task 6: up arms THIS session's keyed marker + armed self-test.
        # Reset any marker left by Task 5's `up` so this task tests arming from clean.
        marker = os.path.join(floor_dir, _key(TEST_TMUX))
        if os.path.exists(marker):
            os.remove(marker)
        rc, out = run(["up", "--team", "demo", "--repo", repo], env_overrides=upov)
        check("up arms the keyed marker on success", rc == 0 and os.path.exists(marker) and "SESSION ARMED" in out)
        check("marker has a header", "team: demo" in open(marker).read())
        check("marker records tmux", "tmux:" in open(marker).read())
        os.remove(marker)
        # up refuses to arm when $TMUX is empty: doctor hard-fails first -> non-zero exit, no marker.
        rc, out = run(["up", "--team", "demo", "--repo", repo], env_overrides=upov, tmux=False)
        check("up without $TMUX exits non-zero and arms no marker", rc != 0 and not os.path.exists(marker))
        openguard = os.path.join(td, "openguard.sh"); write_stub_guard(openguard, selftest_rc=0)
        open(openguard, "w").write("#!/usr/bin/env bash\n[ \"$1\" = \"--self-test\" ] && exit 0\nexit 0\n")
        os.chmod(openguard, 0o755)
        failov = dict(upov); failov["ORCHESTRATE_GUARD"] = openguard
        rc, out = run(["up", "--team", "demo", "--repo", repo], env_overrides=failov)
        check("fail-open guard -> up ABORTS (rc1)", rc == 1 and "failing open" in out)
        check("fail-open -> keyed marker REMOVED (no half-armed session)", not os.path.exists(marker))

        # Task 7: down removes ONLY this session's key, LEAVES foreign, GCs stale tombstones
        os.makedirs(floor_dir, exist_ok=True)
        open(marker, "w").write("x")                                   # my fresh marker
        foreign = os.path.join(floor_dir, _key("/tmp/tmux-501/other,9,9"))
        open(foreign, "w").write("y")                                  # another live session
        stale = os.path.join(floor_dir, _key("/tmp/tmux-501/dead,8,8"))
        open(stale, "w").write("z")                                    # a crashed-session tombstone
        old = time.time() - 200 * 3600                                 # > 72h
        os.utime(stale, (old, old))
        downov = {"ORCHESTRATE_FLOOR_DIR": floor_dir}
        rc, out = run(["down"], env_overrides=downov)                  # runs with TEST_TMUX
        check("down removes my keyed marker", rc == 0 and not os.path.exists(marker) and "marker removed" in out)
        check("down LEAVES a foreign live marker", os.path.exists(foreign))
        check("down GCs a stale tombstone", not os.path.exists(stale))
        rc, out = run(["down"], env_overrides=downov)
        check("down is idempotent (my marker already gone)", rc == 0 and "already disarmed" in out)
        check("down prints the teardown checklist", "shutdown_request" in out and "TeamDelete" in out)

        # TTL clamp: a non-positive ORCHESTRATE_FLOOR_TTL_HOURS must NOT make GC delete a
        # FRESH foreign marker (the cardinal P3-A cross-session-disarm sin). With the clamp,
        # TTL=0/negative falls back to 72h, so a fresh foreign marker survives `down`.
        live_foreign = os.path.join(floor_dir, _key("/tmp/tmux-501/live,3,3"))
        open(live_foreign, "w").write("live")
        rc, out = run(["down"], env_overrides={"ORCHESTRATE_FLOOR_DIR": floor_dir,
                                               "ORCHESTRATE_FLOOR_TTL_HOURS": "0"})
        check("ttl-clamp: TTL=0 down does NOT disarm a fresh foreign session", os.path.exists(live_foreign))
        rc, out = run(["down"], env_overrides={"ORCHESTRATE_FLOOR_DIR": floor_dir,
                                               "ORCHESTRATE_FLOOR_TTL_HOURS": "-5"})
        check("ttl-clamp: TTL=-5 down does NOT disarm a fresh foreign session", os.path.exists(live_foreign))

        # down releases this session's resource leases (best-effort integration)
        res_state = os.path.join(td, "resources.json")
        json.dump({"version":1,"leases":[
            {"id":"demo/impl","session":"demo","teammate":"impl","profile":"generic",
             "created":"2000-01-01T00:00:00Z","ttl_hours":72,"marker_key":"",
             "resources":{"port":{"kind":"port","value":2099},"data_dir":{"kind":"dir","value":td}},
             "env":{},"env_file":None,"meta":{}}]}, open(res_state,"w"))
        rc, out = run(["down","--team","demo"], env_overrides={"ORCHESTRATE_FLOOR_DIR": floor_dir,
                                                               "ORCHESTRATE_RESOURCES_FILE": res_state})
        remaining = json.load(open(res_state))["leases"]
        check("down released the demo session's lease", all(lease["session"]!="demo" for lease in remaining))

        # Misconfigured FLOOR_DIR (a regular file, not a dir): up and down must abort/skip
        # cleanly (no raw traceback). Sweeps the too-narrow-except class.
        floor_file = os.path.join(td, "floor-is-a-file")
        open(floor_file, "w").write("oops")
        rc, out = run(["down"], env_overrides={"ORCHESTRATE_FLOOR_DIR": floor_file})
        check("misconfig: down with file-FLOOR_DIR exits clean (no traceback)",
              rc == 0 and "Traceback" not in out and "already disarmed" in out)
        upfile_ov = dict(ov); upfile_ov.update({"ORCHESTRATE_ARTIFACT_DIR": art, "ORCHESTRATE_FLOOR_DIR": floor_file})
        rc, out = run(["up", "--team", "demo", "--repo", repo], env_overrides=upfile_ov)
        check("misconfig: up with file-FLOOR_DIR aborts clean (no traceback)",
              rc == 1 and "Traceback" not in out and "cannot arm the floor marker" in out)

        # #25: pre-teardown dirty-worktree scan (WARN-and-proceed, never refuse). A marker
        # that records a `repo:` makes `down` scan every worktree of that repo and warn on
        # uncommitted work, so the lead does not `make remove-worktree` over it. The HEAD-vs-
        # arm-SHA gate from the original issue is INTENTIONALLY absent (HEAD is meant to
        # advance; a SHA-equality gate would refuse every legitimate teardown).
        os.makedirs(floor_dir, exist_ok=True)
        # Use a DEDICATED fresh repo (NOT the shared `repo`, which an earlier test dirtied
        # with an untracked dirt.txt): the scan covers the primary worktree too, so the
        # clean-case fixture must have EVERY worktree clean.
        repo25 = os.path.join(td, "repo25"); os.makedirs(repo25)
        subprocess.run(["git", "-C", repo25, "init", "-q"], check=True)
        subprocess.run(["git", "-C", repo25, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        wt = os.path.join(td, "wt-feat25")
        subprocess.run(["git", "-C", repo25, "worktree", "add", "-q", "-b", "feat25", wt], check=True)
        repo_marker = (f"orchestrate session\nteam: demo\nrepo: {repo25}\nhead: dead123\n")
        open(marker, "w").write(repo_marker)
        rc, out = run(["down"], env_overrides={"ORCHESTRATE_FLOOR_DIR": floor_dir})
        check("#25 down: clean worktrees -> no dirty-worktree WARNING, proceeds (rc0)",
              rc == 0 and "uncommitted work in these worktrees" not in out and not os.path.exists(marker))
        # Dirty the worktree, re-arm the marker, tear down again: WARN names it, still proceeds.
        open(os.path.join(wt, "stray.txt"), "w").write("uncommitted")
        open(marker, "w").write(repo_marker)
        rc, out = run(["down"], env_overrides={"ORCHESTRATE_FLOOR_DIR": floor_dir})
        check("#25 down: dirty worktree -> WARNS naming it, still proceeds (rc0, marker removed)",
              rc == 0 and "uncommitted work in these worktrees" in out and wt in out
              and not os.path.exists(marker) and "Traceback" not in out)
        subprocess.run(["git", "-C", repo25, "worktree", "remove", "--force", wt],
                       capture_output=True)

    # P3-G: doctor scans the settings cascade for merge-gate SHADOW rules. The Tier-2
    # merge gate works by OMITTING the squash-merge command from the allow-list so CC
    # prompts the human; CC UNIONS the allow-list across the cascade, so a single blanket
    # rule anywhere silently re-grants merge. doctor must FAIL LOUDLY (rc1 + name file+rule).
    # All trigger strings live in JSON fixtures the harness writes (read from files, never
    # on a command line), per the orchestrate test-driving rule.
    MERGE_LITERAL = "gh pr " + "merge"  # avoid the literal triple on any source line scanned by tools
    with tempfile.TemporaryDirectory() as td:
        guard = os.path.join(td, "guard.sh"); write_stub_guard(guard, selftest_rc=0)
        wired = os.path.join(td, "wired.json")
        json.dump({"teammateMode": "tmux", "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
                   "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                       {"type": "command", "command": 'bash "$HOME/.claude/scripts/orchestrate-guard.sh"'}]}]}},
                  open(wired, "w"))
        base_ov = {"ORCHESTRATE_SETTINGS": wired, "ORCHESTRATE_GUARD": guard}

        def write_settings(path, allow):
            json.dump({"permissions": {"allow": allow}}, open(path, "w"))

        def doctor_with_cascade(files):
            ov = dict(base_ov)
            ov["ORCHESTRATE_SETTINGS_FILES"] = ":".join(files)
            return run(["doctor"], env_overrides=ov)

        # Clean cascade: no shadow -> doctor passes (rc0), no shadow warning.
        f_clean = os.path.join(td, "clean.json")
        write_settings(f_clean, ["Bash(gh pr view:*)", "Bash(gh pr comment:*)",
                                 "Bash(gh pr list)", "Bash(gh pr diff *)",
                                 "Bash(git push:*)", "Read(*)", "Bash(go test *)"])
        rc, out = doctor_with_cascade([f_clean])
        check("p3g: clean cascade -> doctor rc0", rc == 0)
        check("p3g: clean cascade -> no shadow reported", "SHADOW" not in out)

        # MUST-FLAG: every one of these Bash allow-rule patterns grants the merge command
        # via a BROADER grant (not merge-scoped), so each must make doctor FAIL (rc1) and
        # name both the file and the offending rule. The 'merge' literal is assembled from
        # MERGE_LITERAL pieces so no source line here carries the triple.
        # NOTE: the merge-SCOPED patterns (exact `gh pr merge`, `gh pr merge *`,
        # `gh pr merge:*`, `gh pr merge --squash`) are NOT in this list - they are the
        # sanctioned allow-list entry and belong in must_not_flag below.
        ME = "merge"  # piece for assembling boundary/partial-word patterns off the triple
        must_flag = [
            # boundary wildcards broader than merge ('gh pr:*' / 'gh pr *' grant ALL subcommands)
            ("gh pr:*", "gh pr:*"),
            ("gh pr *", "gh pr *"),
            ("gh:*", "gh:*"),
            ("gh *", "gh *"),
            # plain (no-space) prefix, including mid-word truncations of 'merge':
            # these use a plain glob that is NOT a recognised merge-scoped boundary tail,
            # so they remain shadows even though they happen to match merge invocations.
            ("gh pr merg*", "gh pr " + ME[:-1] + "*"),
            ("gh pr mer*", "gh pr " + ME[:-2] + "*"),
            ("gh pr me*", "gh pr " + ME[:-3] + "*"),
            ("gh pr m*", "gh pr " + ME[:-4] + "*"),
            ("gh pr*", "gh pr*"),
            ("gh p*", "gh p*"),
            ("gh*", "gh*"),
            ("g*", "g*"),
            # bare 'gh pr' prefix (no wildcard): grants all 'gh pr ...' subcommands, not merge-only
            ("gh pr bare", "gh pr"),
            # leading / infix glob ('*' anywhere matches any run incl. spaces)
            ("star", "*"),
            ("* merge", "* " + ME),
            ("* pr merge", "* pr " + ME),
            ("gh * merge", "gh * " + ME),
            ("gh*merge", "gh*" + ME),
        ]
        for label, pat in must_flag:
            fsh = os.path.join(td, "shadow.json")
            rule = f"Bash({pat})"
            write_settings(fsh, ["Bash(gh pr view:*)", rule, "Read(*)"])
            rc, out = doctor_with_cascade([fsh])
            check(f"p3g: shadow {label!r} -> doctor rc1", rc == 1)
            check(f"p3g: shadow {label!r} -> names the file", fsh in out)
            check(f"p3g: shadow {label!r} -> names the offending rule", rule in out)

        # Multi-file: shadow in file 2 of 3 is caught, and the RIGHT file is named.
        f1 = os.path.join(td, "casc1.json"); write_settings(f1, ["Bash(gh pr view:*)"])
        f2 = os.path.join(td, "casc2.json"); write_settings(f2, ["Bash(gh pr:*)"])
        f3 = os.path.join(td, "casc3.json"); write_settings(f3, ["Read(*)"])
        rc, out = doctor_with_cascade([f1, f2, f3])
        check("p3g: multi-file shadow in file 2 -> rc1", rc == 1)
        check("p3g: multi-file names the offending file (f2)", f2 in out)
        check("p3g: multi-file does NOT blame the clean file (f1)", f1 not in out)

        # MUST-NOT-FLAG: none of these shadow the merge gate, so doctor must PASS (rc0)
        # with NO shadow reported. This includes:
        #   (a) rules that don't grant merge at all (different subcommands, non-Bash, etc.)
        #   (b) merge-SCOPED rules: exact `gh pr merge`, `gh pr merge *`, `gh pr merge:*`,
        #       and merge-specific invocations like `gh pr merge --squash`. These are the
        #       new sanctioned allow-list entry - the floor deny backstops them in a marker
        #       session, so they are NOT shadows. Each is checked in isolation so a single
        #       false positive is pinpointed by its label.
        PR = "gh pr "
        must_not_flag = [
            # ---- merge-SCOPED: sanctioned allow-list entries (NEW #105 policy) ----
            # exact bare merge target: grants merge or merge + any args
            ("merge exact", MERGE_LITERAL),
            # exact merge with merge-own flags: grants only that specific invocation
            ("merge exact --squash", MERGE_LITERAL + " --squash"),
            ("merge exact --rebase", MERGE_LITERAL + " --rebase"),
            # boundary-star: grants merge + at least one arg (the /merge-pr working form)
            ("merge *", MERGE_LITERAL + " *"),
            # boundary-colon-star: alternate boundary form
            ("merge:*", MERGE_LITERAL + ":*"),
            # ---- non-merge subcommands: different or unrelated ----
            ("gh pr view:*", PR + "view:*"),
            ("gh pr comment:*", PR + "comment:*"),
            ("gh pr list", PR + "list"),
            ("gh pr diff *", PR + "diff *"),
            ("gh pr review:*", PR + "review:*"),
            ("gh project *", "gh project *"),
            ("git push:*", "git " + "push:*"),
            # boundary wildcard whose prefix is a different / partial word
            ("gh pr m:*", PR + ME[0] + ":*"),
            ("gh pr merge-queue:*", PR + ME + "-queue:*"),
            # non-Bash rules cannot grant a Bash command
            ("Read(*)", None),
            ("Edit(*)", None),
            # quoted / space-padded specifiers: CC matches the specifier literally against
            # a command that carries no quotes/leading-space, so these do NOT grant the
            # bare target. They MUST NOT be normalized into a flag.
            ("quoted", None),
            ("single-quoted", None),
            ("leading-space", None),
            ("trailing-space", None),
        ]
        # The quoted/padded specifiers below embed the actual merge specifier text
        # (assembled from pieces) INSIDE the Bash(...) wrapper, never on a command line.
        quoted_rules = {
            "quoted": 'Bash("' + PR + ME + '")',
            "single-quoted": "Bash('gh pr:*')",
            "leading-space": "Bash( gh pr:*)",
            "trailing-space": "Bash(gh pr:* )",
        }
        for label, pat in must_not_flag:
            if label in quoted_rules:
                rule = quoted_rules[label]
            elif pat is None:
                rule = f"{label}"  # already a full rule string (Read(*) / Edit(*))
            else:
                rule = f"Bash({pat})"
            f_one = os.path.join(td, "notflag.json")
            write_settings(f_one, ["Bash(gh pr view:*)", rule, "Read(*)"])
            rc, out = doctor_with_cascade([f_one])
            check(f"p3g: not-flag {label!r} -> doctor rc0", rc == 0)
            check(f"p3g: not-flag {label!r} -> no shadow reported", "SHADOW" not in out)

        # And all of them together in one cascade still pass (no aggregate false positive).
        f_fp = os.path.join(td, "fp.json")
        all_notflag = []
        for label, pat in must_not_flag:
            if label in quoted_rules:
                all_notflag.append(quoted_rules[label])
            elif pat is None:
                all_notflag.append(label)
            else:
                all_notflag.append(f"Bash({pat})")
        write_settings(f_fp, all_notflag + ["Write(//tmp/**)"])
        rc, out = doctor_with_cascade([f_fp])
        check("p3g: false-positive guards -> doctor rc0", rc == 0)
        check("p3g: false-positive guards -> no shadow reported", "SHADOW" not in out)

        # Empty / absent allow list -> no rules -> pass.
        f_empty = os.path.join(td, "empty.json"); write_settings(f_empty, [])
        f_noperm = os.path.join(td, "noperm.json"); json.dump({"env": {}}, open(f_noperm, "w"))
        rc, out = doctor_with_cascade([f_empty, f_noperm])
        check("p3g: empty/absent allow -> doctor rc0", rc == 0)

        # allow not a list (malformed shape) -> treated as no rules, no crash.
        f_badshape = os.path.join(td, "badshape.json")
        json.dump({"permissions": {"allow": "Bash(gh pr:*)"}}, open(f_badshape, "w"))
        rc, out = doctor_with_cascade([f_badshape])
        check("p3g: allow-not-a-list -> rc0 (no crash, no shadow)",
              rc == 0 and "Traceback" not in out)

        # permissions is null -> grants nothing, must NOT crash with AttributeError.
        f_permnull = os.path.join(td, "permnull.json")
        json.dump({"permissions": None}, open(f_permnull, "w"))
        rc, out = doctor_with_cascade([f_permnull])
        check("p3g: permissions=null -> rc0 (no crash, no shadow)",
              rc == 0 and "Traceback" not in out)

        # top-level JSON is an array (not an object) -> grants nothing, no crash.
        f_toparr = os.path.join(td, "toparr.json")
        json.dump(["Bash(gh pr:*)"], open(f_toparr, "w"))
        rc, out = doctor_with_cascade([f_toparr])
        check("p3g: top-level array -> rc0 (no crash, no shadow)",
              rc == 0 and "Traceback" not in out)

        # Fault tolerance: a malformed/odd FIRST file must NOT abort the scan and hide a
        # genuine shadow in a LATER file. file1 is malformed JSON (an error), file2 has a
        # real 'gh pr:*' shadow -> doctor must still rc1 AND name file2's shadow.
        f_bad1 = os.path.join(td, "fault1.json")
        open(f_bad1, "w").write("{not valid json")
        f_good2 = os.path.join(td, "fault2.json"); write_settings(f_good2, ["Bash(gh pr:*)"])
        rc, out = doctor_with_cascade([f_bad1, f_good2])
        check("p3g: bad file1 does not hide shadow in file2 -> rc1",
              rc == 1 and "Traceback" not in out)
        check("p3g: bad file1 + shadow file2 -> names the shadow file2", f_good2 in out)

        # Fault tolerance variant: a non-dict (array) FIRST file must not hide file2 shadow.
        f_arr1 = os.path.join(td, "fault_arr1.json"); json.dump([1, 2], open(f_arr1, "w"))
        f_good3 = os.path.join(td, "fault3.json"); write_settings(f_good3, ["Bash(gh pr:*)"])
        rc, out = doctor_with_cascade([f_arr1, f_good3])
        check("p3g: array file1 does not hide shadow in file2 -> rc1",
              rc == 1 and "Traceback" not in out and f_good3 in out)

        # Missing file in the cascade is skipped silently (not a problem).
        f_missing = os.path.join(td, "does-not-exist.json")
        rc, out = doctor_with_cascade([f_clean, f_missing])
        check("p3g: missing cascade file skipped (rc0, no traceback)",
              rc == 0 and "Traceback" not in out)

        # Malformed JSON in a cascade file -> reported as a doctor problem (rc1), no crash.
        f_malformed = os.path.join(td, "malformed.json")
        open(f_malformed, "w").write("{not valid json")
        rc, out = doctor_with_cascade([f_clean, f_malformed])
        check("p3g: malformed JSON cascade file -> rc1 (reported, no traceback)",
              rc == 1 and "Traceback" not in out and f_malformed in out)

    # Key-contract: the guard's bash sanitization must equal setup's python one, for an
    # ASCII sample AND a multibyte one, under BOTH the ambient locale and LC_ALL=C. The
    # guard uses `LC_ALL=C tr` (byte-mode), so the bash side here mirrors that exactly.
    for sample in ("/tmp/tmux-501/default,12345,0", "/tmp/tmux-café/sock,7,0"):
        for locale_env in ({}, {"LC_ALL": "C"}):
            env = dict(os.environ); env.update(locale_env)
            bash_key = subprocess.run(
                ["bash", "-c", "printf '%s' \"$1\" | LC_ALL=C tr -c 'A-Za-z0-9' '_'", "_", sample],
                capture_output=True, text=True, timeout=5, env=env).stdout
            label = f"key-contract: bash==python for {sample!r} (LC_ALL={locale_env.get('LC_ALL','ambient')})"
            check(label, bash_key == _key(sample))

    # configure: consent-based settings.json wiring (floor hook + missing allow-list entries)
    # AND the #30 guard-deploy (copy the bundled guard to the stable GUARD path).
    with tempfile.TemporaryDirectory() as td:
        ctpl = os.path.join(td, "templates"); os.makedirs(ctpl)
        open(os.path.join(ctpl, "required-permissions.md"), "w").write(
            "## Needed allow-list entries\n- `Bash(gh pr view *)`\n- `Bash(go test *)`\n"
            "## Guardrails\n- NOTE: `Bash(go *)` not prescribed\n")
        cs = os.path.join(td, "settings.json")
        json.dump({"permissions": {"allow": ["Bash(gh pr view *)"]}}, open(cs, "w"))
        # #30 guard-deploy fixtures: a fake BUNDLED guard source + an (initially absent) dest, so
        # configure's guard deploy operates on temp files, NEVER the real ~/.claude guard.
        cbundle = os.path.join(td, "bundled-guard.sh"); write_stub_guard(cbundle)
        cdest = os.path.join(td, "deployed", "orchestrate-guard.sh")
        # Pin the shadow-narrowing cascade scan to THIS fixture (cs) so configure's
        # cascade-wide narrowing never reaches the real ~/.claude cascade.
        cov = {"ORCHESTRATE_SETTINGS": cs, "ORCHESTRATE_TEMPLATES_DIR": ctpl,
               "ORCHESTRATE_SETTINGS_FILES": cs,
               "ORCHESTRATE_GUARD": cdest, "ORCHESTRATE_BUNDLED_GUARD": cbundle}
        rc, out = run(["configure"], env_overrides=cov)
        check("configure dry-run previews hook + missing entry + guard DEPLOY, writes NOTHING",
              rc == 0 and "PreToolUse" in out and "Bash(go test *)" in out and "DEPLOY the floor guard" in out
              and json.load(open(cs)).get("hooks") is None and not os.path.exists(cdest))
        rc, out = run(["configure", "--apply", "--yes"], env_overrides=cov)
        s = json.load(open(cs))
        hookok = any(b.get("matcher") == "Bash" and any("orchestrate-guard.sh" in h.get("command", "")
                     for h in b.get("hooks", [])) for b in s.get("hooks", {}).get("PreToolUse", []))
        check("configure --apply wires the floor hook + backs up", rc == 0 and hookok and os.path.exists(cs + ".bak"))
        check("configure --apply adds the missing allow entry + keeps the existing one",
              "Bash(go test *)" in s["permissions"]["allow"] and "Bash(gh pr view *)" in s["permissions"]["allow"])
        check("configure does NOT add NOTE: lines as allow entries", "Bash(go *)" not in s["permissions"]["allow"])
        # #30: the bundled guard was DEPLOYED to the stable path, byte-identical + executable.
        guard_deployed = (os.path.isfile(cdest)
                          and open(cdest, "rb").read() == open(cbundle, "rb").read()
                          and bool(os.stat(cdest).st_mode & 0o111))
        check("#30: configure --apply deploys the bundled guard to the stable path (executable)", guard_deployed)
        rc, out = run(["configure", "--apply", "--yes"], env_overrides=cov)
        check("configure is idempotent once configured (no add, no narrow, guard matches)",
              rc == 0 and "already has the floor hook" in out and "NARROW" not in out
              and "DEPLOY the floor guard" not in out and "REFRESH" not in out)
        # #30: a STALE deployed guard (differs from the bundle) is detected + REFRESHED on --apply.
        open(cdest, "w").write("#!/bin/sh\nexit 1\n# drifted\n")
        rc, out = run(["configure"], env_overrides=cov)
        check("#30: configure detects a STALE deployed guard (REFRESH preview, no write)",
              rc == 0 and "REFRESH" in out and open(cdest).read().startswith("#!/bin/sh\nexit 1"))
        rc, out = run(["configure", "--apply", "--yes"], env_overrides=cov)
        check("#30: configure --apply REFRESHES a stale deployed guard back to the bundle",
              open(cdest, "rb").read() == open(cbundle, "rb").read())
        # #30: a MISSING bundled source warns (does not crash); the rest of configure still runs.
        covms = dict(cov); covms["ORCHESTRATE_BUNDLED_GUARD"] = os.path.join(td, "does-not-exist.sh")
        rc, out = run(["configure", "--apply", "--yes"], env_overrides=covms)
        check("#30: missing bundled guard -> warns, no crash", rc in (0, 1) and "missing" in out.lower())
        # #30: doctor WARNs (never FAILs) on a stale deployed guard.
        open(cdest, "w").write("#!/bin/sh\nexit 1\n# drifted again\n")
        rc, out = run(["doctor"], env_overrides=cov, tmux=True)
        check("#30: doctor WARNs on a stale deployed guard (not a hard fail)",
              "STALE vs the bundled plugin guard" in out)
        open(cs, "w").write("{not json")
        rc, out = run(["configure", "--apply", "--yes"], env_overrides=cov)
        check("configure REFUSES to overwrite an unparseable settings.json (no clobber)",
              rc == 1 and "refusing to touch" in out and open(cs).read().startswith("{not"))

    # #71: configure NARROWS a blanket `gh pr` allow-rule that shadows the merge gate.
    # The blanket specifier text lives only INSIDE Bash(...) wrappers (never on a command
    # line), and the 'merge' literal is assembled from pieces so no source line carries the
    # trigger triple for the live PreToolUse Bash hook.
    with tempfile.TemporaryDirectory() as td:
        PR = "gh pr "
        ME = "merge"
        ntpl = os.path.join(td, "templates"); os.makedirs(ntpl)
        # A required-permissions.md whose floor hook + allow entries are ALREADY satisfied by
        # the fixture below, so configure's ADD step is a no-op and only narrowing runs.
        open(os.path.join(ntpl, "required-permissions.md"), "w").write(
            "## Needed allow-list entries\n- `Bash(" + PR + "view *)`\n## Guardrails\n- nothing\n")
        WIREDHOOK = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": 'bash "$HOME/.claude/scripts/orchestrate-guard.sh"'}]}]}}

        def narrow_fixture(allow):
            """Write a settings file pre-wired with the floor hook so configure's ADD step is
            a no-op and only the shadow-narrowing path exercises. Returns its path."""
            p = os.path.join(td, "ncfg.json")
            d = dict(WIREDHOOK); d["permissions"] = {"allow": allow}
            json.dump(d, open(p, "w"))
            return p

        def nov(path):
            # SETTINGS == the only cascade file, so doctor + configure see the same single file.
            return {"ORCHESTRATE_SETTINGS": path, "ORCHESTRATE_TEMPLATES_DIR": ntpl,
                    "ORCHESTRATE_SETTINGS_FILES": path}

        # (a) Happy path: a blanket `gh pr *` shadow is narrowed; afterward the doctor shadow
        # scan PASSES on that cascade file (the gate is restored).
        blanket = "Bash(" + PR + "*)"
        ncfg = narrow_fixture(["Bash(" + PR + "view *)", blanket, "Read(*)"])
        # doctor first CONFIRMS the shadow exists (rc1).
        rc, out = run(["doctor"], env_overrides=nov(ncfg))
        check("#71: pre-narrow doctor flags the gh-pr blanket shadow (rc1)",
              rc == 1 and "SHADOW" in out and blanket in out)
        # configure dry-run PREVIEWS the narrow, writes nothing.
        rc, out = run(["configure"], env_overrides=nov(ncfg))
        before = json.load(open(ncfg))
        check("#71: configure dry-run previews the narrow, writes nothing",
              "NARROW" in out and "remove " + blanket in out and blanket in before["permissions"]["allow"])
        # configure --apply narrows it.
        rc, out = run(["configure", "--apply", "--yes"], env_overrides=nov(ncfg))
        after = json.load(open(ncfg))["permissions"]["allow"]
        narrowed_ok = (blanket not in after
                       and ("Bash(" + PR + "view *)") in after
                       and ("Bash(" + PR + "comment *)") in after
                       and not any(ME in r for r in after))  # merge stays omitted
        check("#71: configure --apply narrows the blanket (merge omitted)", narrowed_ok)
        check("#71: configure --apply backs up the file before narrowing",
              os.path.exists(ncfg + ".bak"))
        # Atomic write (CR #72): the temp file is os.replace'd onto the target and never left
        # behind, and the target is always complete/parseable (no truncate-then-write window).
        leftover_tmp = [f for f in os.listdir(td) if f.startswith(".orch-tmp-")]
        check("#71: atomic narrow leaves no temp file behind + target stays valid JSON",
              not leftover_tmp and isinstance(json.load(open(ncfg)), dict))
        # bak preserves the pre-narrow blanket.
        check("#71: backup retains the original blanket",
              blanket in json.load(open(ncfg + ".bak"))["permissions"]["allow"])
        # The doctor shadow scan now PASSES on that cascade file.
        rc, out = run(["doctor"], env_overrides=nov(ncfg))
        check("#71: post-narrow doctor shadow scan PASSES (rc0, no shadow)",
              "SHADOW" not in out and "no settings-cascade rule shadows" in out)

        # (b) Idempotency: running configure again is a no-op (already narrowed, no shadow).
        os.remove(ncfg + ".bak")
        rc, out = run(["configure", "--apply", "--yes"], env_overrides=nov(ncfg))
        check("#71: re-running configure is a no-op once narrowed (no NARROW, no new bak)",
              rc == 0 and "NARROW" not in out and not os.path.exists(ncfg + ".bak"))

        # (b2) The `gh pr:*` colon-boundary blanket form is also narrowed.
        colon = "Bash(" + PR.rstrip() + ":*)"
        ncfg2 = os.path.join(td, "ncfg2.json")
        d = dict(WIREDHOOK); d["permissions"] = {"allow": [colon]}
        json.dump(d, open(ncfg2, "w"))
        rc, out = run(["configure", "--apply", "--yes"], env_overrides=nov(ncfg2))
        after2 = json.load(open(ncfg2))["permissions"]["allow"]
        check("#71: `gh pr:*` colon blanket is narrowed too (merge omitted)",
              colon not in after2 and ("Bash(" + PR + "view *)") in after2
              and not any(ME in r for r in after2))

        # (c) A broader-scope shadow (`gh *`) is SURFACED, NOT rewritten.
        broad = "Bash(gh *)"
        ncfg3 = os.path.join(td, "ncfg3.json")
        # Include the required allow entry so configure's ADD step is a no-op and only the
        # cascade narrow path runs (so a stray ADD-path .bak does not confuse this assertion).
        d = dict(WIREDHOOK); d["permissions"] = {"allow": ["Bash(" + PR + "view *)", broad, "Read(*)"]}
        json.dump(d, open(ncfg3, "w"))
        rc, out = run(["configure", "--apply", "--yes"], env_overrides=nov(ncfg3))
        after3 = json.load(open(ncfg3))["permissions"]["allow"]
        check("#71: broader `gh *` shadow is surfaced for human resolution, NOT rewritten",
              rc == 1 and "HUMAN resolution" in out and broad in after3
              and not os.path.exists(ncfg3 + ".bak"))

        # (d) An unparseable cascade file in the narrow path is SKIPPED, not clobbered.
        # SETTINGS itself stays parseable (so configure's own preamble does not bail);
        # a SECOND cascade file is the unparseable one carrying the shadow scan target.
        good_primary = os.path.join(td, "primary.json")
        json.dump(WIREDHOOK, open(good_primary, "w"))  # parseable, no shadow
        badcasc = os.path.join(td, "badcasc.json")
        open(badcasc, "w").write("{not valid json")
        unparse_ov = {"ORCHESTRATE_SETTINGS": good_primary, "ORCHESTRATE_TEMPLATES_DIR": ntpl,
                      "ORCHESTRATE_SETTINGS_FILES": good_primary + ":" + badcasc}
        rc, out = run(["configure", "--apply", "--yes"], env_overrides=unparse_ov)
        check("#71: unparseable cascade file is reported + skipped, never clobbered",
              rc == 1 and badcasc in out and open(badcasc).read() == "{not valid json"
              and not os.path.exists(badcasc + ".bak"))

        # (e) Aborting the y/N declines the narrow (no write, no bak).
        ncfg5 = os.path.join(td, "ncfg5.json")
        d = dict(WIREDHOOK); d["permissions"] = {"allow": [blanket]}
        json.dump(d, open(ncfg5, "w"))
        # Drive an interactive (no --yes) --apply with 'n' on stdin via a subprocess wrapper.
        env5 = dict(os.environ); env5["TMUX"] = TEST_TMUX; env5.update(nov(ncfg5))
        p5 = subprocess.run([sys.executable, SCRIPT, "configure", "--apply"], env=env5,
                            input="n\n", capture_output=True, text=True, timeout=30)
        check("#71: declining the narrow y/N leaves the file unchanged (no bak)",
              "aborted" in (p5.stdout + p5.stderr)
              and blanket in json.load(open(ncfg5))["permissions"]["allow"]
              and not os.path.exists(ncfg5 + ".bak"))

    # #68: slug derivation - _derive_repo_slug parses both SSH and HTTPS remote URL forms
    # and scaffold_artifacts renders the slug (not the raw path) into the brief's <REPO>.
    # Import the function directly from the module (no subprocess) to test the parser in
    # isolation without environment side-effects.
    import importlib.util
    _spec = importlib.util.spec_from_file_location("orchestrate_setup", SCRIPT)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

    # SSH form: git@github.com:owner/name.git -> owner/name
    with tempfile.TemporaryDirectory() as td68:
        r_ssh = os.path.join(td68, "ssh-repo"); os.makedirs(r_ssh)
        subprocess.run(["git", "-C", r_ssh, "init", "-q"], check=True)
        subprocess.run(["git", "-C", r_ssh, "remote", "add", "origin",
                        "git@github.com:acme/widget.git"], check=True)
        slug = _mod._derive_repo_slug(r_ssh)
        check("#68 slug: SSH git@github.com:owner/name.git -> owner/name",
              slug == "acme/widget")

        # SSH form without .git suffix: git@github.com:owner/name -> owner/name
        subprocess.run(["git", "-C", r_ssh, "remote", "set-url", "origin",
                        "git@github.com:acme/widget"], check=True)
        slug = _mod._derive_repo_slug(r_ssh)
        check("#68 slug: SSH git@github.com:owner/name (no .git) -> owner/name",
              slug == "acme/widget")

        # HTTPS form: https://github.com/owner/name.git -> owner/name
        subprocess.run(["git", "-C", r_ssh, "remote", "set-url", "origin",
                        "https://github.com/acme/widget.git"], check=True)
        slug = _mod._derive_repo_slug(r_ssh)
        check("#68 slug: HTTPS https://github.com/owner/name.git -> owner/name",
              slug == "acme/widget")

        # HTTPS form without .git suffix: https://github.com/owner/name -> owner/name
        subprocess.run(["git", "-C", r_ssh, "remote", "set-url", "origin",
                        "https://github.com/acme/widget"], check=True)
        slug = _mod._derive_repo_slug(r_ssh)
        check("#68 slug: HTTPS https://github.com/owner/name (no .git) -> owner/name",
              slug == "acme/widget")

        # No remote -> SystemExit with a clear error message (never silently renders a path).
        r_noremote = os.path.join(td68, "noremote"); os.makedirs(r_noremote)
        subprocess.run(["git", "-C", r_noremote, "init", "-q"], check=True)
        try:
            _mod._derive_repo_slug(r_noremote)
            check("#68 slug: no remote -> SystemExit (error not raised)", False)
        except SystemExit as e:
            msg = str(e)
            check("#68 slug: no remote -> SystemExit with clear message",
                  "owner/name" in msg or "origin" in msg or "slug" in msg)

        # scaffold_artifacts renders the slug into the brief's body, not the raw repo path.
        # Wire a minimal fixture: templates dir with a pr-shipper-brief template.
        art68 = os.path.join(td68, "art"); os.makedirs(art68)
        tpl68 = os.path.join(td68, "templates"); os.makedirs(tpl68)
        open(os.path.join(tpl68, "pr-shipper-brief.md"), "w").write(
            "Repo: <REPO>\nStack: <STACK>\nPacing: <SPACING_MIN> minutes\n")
        # Set a fresh HTTPS remote for this scaffold test.
        subprocess.run(["git", "-C", r_ssh, "remote", "set-url", "origin",
                        "https://github.com/myorg/myrepo.git"], check=True)
        orig_artifacts = _mod.ARTIFACTS
        orig_templates = _mod.TEMPLATES
        _mod.ARTIFACTS = art68
        _mod.TEMPLATES = tpl68
        try:
            stack68, _triage68, brief68_path = _mod.scaffold_artifacts("t68", r_ssh, 12)
        finally:
            _mod.ARTIFACTS = orig_artifacts
            _mod.TEMPLATES = orig_templates
        brief68 = open(brief68_path).read()
        brief68_body = "\n".join(brief68.splitlines()[1:])  # skip header comment
        check("#68 scaffold: brief body contains slug not raw path",
              "myorg/myrepo" in brief68_body and r_ssh not in brief68_body)
        check("#68 scaffold: brief body has no <REPO> placeholder remaining",
              "<REPO>" not in brief68_body)
        check("#68 scaffold: brief body has no <STACK> placeholder remaining",
              "<STACK>" not in brief68_body)

    # #107: _missing_allow_entries harvester over-harvest guard (Option A: a deliberately
    # SIMPLE parser - every backticked Bash/Write/Edit/Read(...) token on a non-NOTE line in
    # the section is a prescribed entry - plus a DISCIPLINED doc, pinned by an exact-set test).
    # Verify: (a) a de-backticked prose Perm mention is NOT harvested; (b) THE bbef7e3
    # REGRESSION - a bullet with a prose intro + colon then several comma-separated entries
    # harvests ALL of them (the gh-pr-subcommand line bbef7e3 silently dropped); (c) NOTE:
    # lines are skipped; (d) non-Perm backticked tokens (e.g. `gh pr`) are ignored; and (e)
    # the REAL required-permissions.md harvests EXACTLY the prescribed set - no phantom added
    # (esp. the security-relevant Bash(gh api *) that #24 removed), no real entry dropped.
    import importlib.util as _ilu
    _spec107 = _ilu.spec_from_file_location("orchestrate_setup", SCRIPT)
    _mod107 = _ilu.module_from_spec(_spec107)
    _spec107.loader.exec_module(_mod107)

    def _harvest(md_text):
        """Call _missing_allow_entries with an empty allow-list against a fixture md."""
        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as _td:
            _tpl = _os.path.join(_td, "templates"); _os.makedirs(_tpl)
            open(_os.path.join(_tpl, "required-permissions.md"), "w").write(md_text)
            orig = _mod107.TEMPLATES; _mod107.TEMPLATES = _tpl
            try:
                return _mod107._missing_allow_entries({}) or []
            finally:
                _mod107.TEMPLATES = orig

    # (a) A prose mention written WITHOUT a backtick-Perm wrapper (the doc-discipline fix)
    # is invisible to the harvester - this is how the security-relevant gh-api phantom is
    # kept out under Option A.
    prose_md = (
        "## Needed allow-list entries\n"
        "- gh-api access via WRAPPERS, NOT a broad gh-api allow-rule (Bash(gh api *) form) - issue #24\n"
    )
    check("#107 de-backticked prose Perm mention is NOT harvested", _harvest(prose_md) == [])

    # (b) THE bbef7e3 REGRESSION GUARD: a bullet with a prose intro + colon then several
    # comma-separated backticked entries must harvest EVERY entry (bbef7e3's list-item guard
    # required the first backtick to sit immediately after `- `, so it dropped this whole line
    # and lost the 9 non-merge gh-pr subcommands).
    enum_md = (
        "## Needed allow-list entries\n"
        "- The NON-MERGE `gh pr` subcommands, ENUMERATED (never a blanket `gh pr *`): "
        "`Bash(gh pr view *)`, `Bash(gh pr diff *)`, `Bash(gh pr merge *)`.\n"
    )
    check("#107 prose-intro enumeration bullet: ALL entries harvested (the bbef7e3 regression)",
          set(_harvest(enum_md)) == {"Bash(gh pr view *)", "Bash(gh pr diff *)", "Bash(gh pr merge *)"})

    # (c) NOTE: lines are commentary, never prescriptive - their backticked tokens are skipped.
    note_md = (
        "## Needed allow-list entries\n"
        "- the go subcommands. NOTE: settings usually has `Bash(go build *)` already\n"
    )
    check("#107 NOTE: line tokens are not harvested", _harvest(note_md) == [])

    # (d) A backticked token that is NOT a Bash/Write/Edit/Read Perm (e.g. `gh pr`) is ignored.
    nonperm_md = (
        "## Needed allow-list entries\n"
        "- `gh pr` family, the entry is `Bash(gh issue *)`\n"
    )
    check("#107 non-Perm backticked token ignored, real Perm harvested",
          _harvest(nonperm_md) == ["Bash(gh issue *)"])

    # (e) EXACT-SET pin on the REAL required-permissions.md: this is the durable guard - it
    # fails CI if any future prose mention re-introduces a phantom OR any real entry is dropped.
    EXPECTED_107 = {
        "Bash(scripts/safe-push.sh *)", "Bash(scripts/safe-push.sh)",
        "Bash(gh pr view *)", "Bash(gh pr diff *)", "Bash(gh pr checks *)",
        "Bash(gh pr create *)", "Bash(gh pr list *)", "Bash(gh pr status *)",
        "Bash(gh pr edit *)", "Bash(gh pr ready *)", "Bash(gh pr comment *)",
        "Bash(gh pr merge *)", "Bash(gh issue *)", "Bash(git *)",
        "Write(/tmp/**)", "Edit(/tmp/**)", "Read(/tmp/**)",
        "Bash(make *)", "Bash(golangci-lint run *)", "Bash(govulncheck *)",
        "Bash(~/.claude/scripts/*.sh *)",
    }
    real_req = os.path.join(os.path.dirname(os.path.abspath(SCRIPT)),
                            "..", "skills", "orchestrate", "templates", "required-permissions.md")
    if os.path.isfile(real_req):
        real_got = set(_harvest(open(real_req).read()))
        check("#107 real required-permissions.md: harvested set EXACTLY matches prescribed entries",
              real_got == EXPECTED_107)
        check("#107 real required-permissions.md: no Bash(gh api *) phantom (the #24 least-privilege regression)",
              not any("gh api" in e for e in real_got))
        check("#107 real required-permissions.md: all 9 non-merge gh-pr subcommands present",
              sum(1 for e in real_got if e.startswith("Bash(gh pr ") and "merge" not in e) == 9)
    else:
        check("#107 real required-permissions.md: file accessible for regression check", False)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("All harness checks passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
