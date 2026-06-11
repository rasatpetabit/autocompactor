"""Tests for install.py against a temp HOME and a stubbed crontab.

Every run goes through a subprocess with HOME pointed at tmp_path and a
fake `crontab` binary on PATH, so the real ~/.claude/settings.json and
the real user crontab are never touched.
"""

import json
import os
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(HERE, "install.py")

FAKE_CRONTAB = """#!/bin/sh
if [ "$1" = "-l" ]; then
    cat "$FAKE_CRON_FILE" 2>/dev/null || exit 1
else
    cat > "$FAKE_CRON_FILE"
fi
"""


def _env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "crontab"
    if not fake.exists():
        fake.write_text(FAKE_CRONTAB)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("AUTOCOMPACTOR_")}
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["FAKE_CRON_FILE"] = str(tmp_path / "fake_crontab")
    return env


def run_install(tmp_path, *flags):
    res = subprocess.run([sys.executable, INSTALL, *flags],
                         capture_output=True, text=True, timeout=120,
                         env=_env(tmp_path))
    return res


def read_settings(tmp_path):
    with open(tmp_path / ".claude" / "settings.json") as fh:
        return json.load(fh)


def our_groups(settings, event):
    return [g for g in settings.get("hooks", {}).get(event, [])
            if any("autocompactor" in (h.get("command") or "")
                   for h in g.get("hooks", []))]


def test_fresh_install(tmp_path):
    res = run_install(tmp_path)
    assert res.returncode == 0, res.stderr
    s = read_settings(tmp_path)
    assert len(our_groups(s, "UserPromptSubmit")) == 1
    assert len(our_groups(s, "PreCompact")) == 2
    matchers = {g["matcher"] for g in our_groups(s, "PreCompact")}
    assert matchers == {"manual", "auto"}
    # only native Claude knobs land in env on a fresh HOME — autocompactor
    # tuning lives in config.json, never seeded as env
    for key in ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW"):
        assert key in s["env"], key
    assert not any(k.startswith("AUTOCOMPACTOR_") for k in s["env"])
    assert (tmp_path / ".claude" / "autocompactor").is_dir()


def test_reinstall_is_idempotent(tmp_path):
    run_install(tmp_path)
    first = read_settings(tmp_path)
    res = run_install(tmp_path)
    assert res.returncode == 0
    assert read_settings(tmp_path) == first


def test_tuned_env_preserved(tmp_path):
    cdir = tmp_path / ".claude"
    cdir.mkdir()
    (cdir / "settings.json").write_text(json.dumps({
        "env": {
            "AUTOCOMPACTOR_SOFT_PCT": "0.5",
            "AUTOCOMPACTOR_HARD_PCT": "0.62",
            "AUTOCOMPACTOR_COOLDOWN": "20000",
            "UNRELATED_KEY": "keepme",
        },
    }))
    res = run_install(tmp_path)
    assert res.returncode == 0
    env = read_settings(tmp_path)["env"]
    # manual AUTOCOMPACTOR_* overrides survive a plain install untouched
    assert env["AUTOCOMPACTOR_SOFT_PCT"] == "0.5"
    assert env["AUTOCOMPACTOR_HARD_PCT"] == "0.62"
    assert env["AUTOCOMPACTOR_COOLDOWN"] == "20000"
    assert env["UNRELATED_KEY"] == "keepme"
    # but install never seeds new AUTOCOMPACTOR_* keys (config.json rules)
    assert "AUTOCOMPACTOR_POST_FLOOR" not in env
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "300000"


def test_force_env_resets_to_defaults(tmp_path):
    cdir = tmp_path / ".claude"
    cdir.mkdir()
    (cdir / "settings.json").write_text(json.dumps({
        "env": {"AUTOCOMPACTOR_SOFT_PCT": "0.5", "UNRELATED_KEY": "keepme",
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"},
    }))
    res = run_install(tmp_path, "--force-env")
    assert res.returncode == 0
    env = read_settings(tmp_path)["env"]
    # --force-env resets only the native keys; manual AUTOCOMPACTOR_*
    # overrides are user-owned and untouched
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "300000"
    assert env["AUTOCOMPACTOR_SOFT_PCT"] == "0.5"
    assert env["UNRELATED_KEY"] == "keepme"


def test_cron_register_idempotent(tmp_path):
    (tmp_path / "fake_crontab").write_text("0 1 * * * /bin/true # other-job\n")
    run_install(tmp_path, "--cron")
    run_install(tmp_path, "--cron")
    lines = (tmp_path / "fake_crontab").read_text().splitlines()
    ours = [ln for ln in lines if "autocompactor-nightly" in ln]
    assert len(ours) == 1
    assert "nightly_eval.py" in ours[0]
    assert any("other-job" in ln for ln in lines)


def test_remove_round_trip(tmp_path):
    cdir = tmp_path / ".claude"
    cdir.mkdir()
    other_hook = {"hooks": [{"type": "command", "command": "echo hi"}]}
    (cdir / "settings.json").write_text(json.dumps({
        "hooks": {"UserPromptSubmit": [other_hook]},
        "env": {"UNRELATED_KEY": "keepme"},
        "model": "fable",
    }))
    run_install(tmp_path, "--cron")
    res = run_install(tmp_path, "--remove")
    assert res.returncode == 0
    s = read_settings(tmp_path)
    assert our_groups(s, "UserPromptSubmit") == []
    assert our_groups(s, "PreCompact") == []
    # unrelated hook, env key, and top-level settings survive
    assert s["hooks"]["UserPromptSubmit"] == [other_hook]
    assert s["env"]["UNRELATED_KEY"] == "keepme"
    assert s["model"] == "fable"
    assert not any(k.startswith("AUTOCOMPACTOR_") for k in s["env"])
    # native key intentionally left, with a printed note
    assert s["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "300000"
    assert s["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "90"
    assert "left CLAUDE_CODE_AUTO_COMPACT_WINDOW" in res.stdout
    # cron line gone
    cron = (tmp_path / "fake_crontab").read_text()
    assert "autocompactor-nightly" not in cron


def test_status_on_fresh_install(tmp_path):
    run_install(tmp_path, "--cron")
    res = run_install(tmp_path, "--status")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "STATUS: OK" in res.stdout
    assert "cron: registered" in res.stdout


def test_status_flags_missing_install(tmp_path):
    res = run_install(tmp_path, "--status")
    assert res.returncode == 1
    assert "STATUS:" in res.stdout and "problem" in res.stdout


def test_unknown_flag_rejected(tmp_path):
    res = run_install(tmp_path, "--bogus")
    assert res.returncode == 2
    assert "unknown flag" in res.stdout
