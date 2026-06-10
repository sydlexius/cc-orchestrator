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

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orchestrate-setup.py")
FAILS = []


def write_stub_guard(path, selftest_rc=0):
    """A fake guard mimicking the real floor for up's armed self-test:
      - `--self-test` exits selftest_rc.
      - push-main payload -> Tier-1 HARD DENY (exit 2).
      - merge-by-API payload (`gh api ... pulls/N/merge`) -> Tier-2 HARD DENY (exit 2).
      - anything else (incl. `gh pr merge`, which the allow-list gates) -> exit 0.
    Written in Python (not bash) to parse the stdin JSON properly - avoids the shell
    case-pattern quoting traps a bash stub is prone to."""
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
        upov = dict(ov); upov.update({"ORCHESTRATE_ARTIFACT_DIR": art, "ORCHESTRATE_FLOOR_DIR": floor_dir})
        rc, out = run(["up", "--team", "demo", "--repo", repo], env_overrides=upov)
        # P3-A /tmp-namespacing: all artifacts live under ARTIFACTS/<team>/ (per-team dir),
        # stack drops its <team>- prefix (the dir carries the team identity).
        team_dir = os.path.join(art, "demo")
        stack = os.path.join(team_dir, "stack.json")
        check("up scaffolds <team>/stack.json=[]", os.path.exists(stack) and json.load(open(stack)) == [])
        check("up creates <team>/pr-triage dir", os.path.isdir(os.path.join(team_dir, "pr-triage")))
        check("up creates <team>/adv-review dir", os.path.isdir(os.path.join(team_dir, "adv-review")))
        brief = open(os.path.join(team_dir, "pr-shipper-brief.md")).read()
        # Verify the real values were substituted AND none of the literal tokens remain.
        check("brief substitutions rendered",
              repo in brief and
              stack in brief and
              "<REPO>" not in brief and
              "<STACK>" not in brief and
              "<SPACING_MIN>" not in brief)

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

        # MUST-FLAG: every one of these Bash allow-rule patterns DOES grant the merge
        # command per Claude Code permission semantics, so each must make doctor FAIL
        # (rc1) and name both the file and the offending rule. The 'merge' literal is
        # assembled from MERGE_LITERAL pieces so no source line here carries the triple.
        ME = "merge"  # piece for assembling boundary/partial-word patterns off the triple
        must_flag = [
            # exact
            ("exact", MERGE_LITERAL),
            ("exact --squash", MERGE_LITERAL + " --squash"),
            # boundary wildcards (':*' and ' *' enforce a word/arg boundary)
            ("merge:*", MERGE_LITERAL + ":*"),
            ("merge *", MERGE_LITERAL + " *"),
            ("gh pr:*", "gh pr:*"),
            ("gh pr *", "gh pr *"),
            ("gh:*", "gh:*"),
            ("gh *", "gh *"),
            # plain (no-space) prefix, including mid-word truncations
            ("gh pr merg*", "gh pr " + ME[:-1] + "*"),
            ("gh pr mer*", "gh pr " + ME[:-2] + "*"),
            ("gh pr me*", "gh pr " + ME[:-3] + "*"),
            ("gh pr m*", "gh pr " + ME[:-4] + "*"),
            ("gh pr*", "gh pr*"),
            ("gh p*", "gh p*"),
            ("gh*", "gh*"),
            ("g*", "g*"),
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

        # MUST-NOT-FLAG: none of these grant the merge command, so doctor must PASS (rc0)
        # with NO shadow reported. These guard against false positives that would cripple
        # legitimate allow-rules. Each is checked in isolation so a single false positive
        # is pinpointed by its label.
        PR = "gh pr "
        must_not_flag = [
            # different subcommand
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
        # The four quoted/padded specifiers below embed the actual merge specifier text
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
