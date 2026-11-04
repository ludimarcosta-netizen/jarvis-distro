"""Cron real em perfil temporário, entre processos, sem modelo ou entrega externa."""
import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

CORE = Path(os.environ.get("HERMES_AGENT_DIR") or Path.home() / "AppData/Local/hermes/hermes-agent")


@lru_cache(maxsize=1)
def hermes_python():
    """Python capaz de importar o cron do Hermes (o venv do Jarvis não tem ruamel)."""
    installed = sorted(CORE.parent.glob("installs/*/environments/*/venv/Scripts/python.exe"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
    for candidate in (os.environ.get("JARVIS_HERMES_PYTHON"), sys.executable, *map(str, installed)):
        if not candidate:
            continue
        try:
            probe = subprocess.run([candidate, "-c", "import ruamel.yaml, cron.jobs, cron.scheduler"],
                                   cwd=CORE, env={**os.environ, "PYTHONPATH": str(CORE)},
                                   capture_output=True, timeout=60, stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return candidate
    pytest.skip("nenhum Python com ruamel.yaml importa o cron do Hermes; defina JARVIS_HERMES_PYTHON")

DRIVER = r'''
import json
import os
from datetime import timedelta
from pathlib import Path
import sys
from cron import jobs, scheduler
from hermes_time import now

home = Path(os.environ["HERMES_HOME"])
action = sys.argv[1]
if action == "create":
    scripts = home / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / "reminder.py"
    script.write_text("from pathlib import Path\np = Path(" + repr(str(home / "effects.txt")) +
                      ")\nwith p.open('a') as f: f.write('reminder\\n')\nprint('test reminder')\n")
    job = jobs.create_job(prompt="", schedule=(now() - timedelta(seconds=5)).isoformat(),
                          name="Jarvis integration reminder", script=str(script),
                          interpreter=sys.executable, no_agent=True, deliver="local")
    result = {"id": job["id"], "kind": job["schedule"]["kind"]}
elif action == "list":
    result = [{"id": j["id"], "state": j.get("state"),
               "completed": j.get("repeat", {}).get("completed", 0)}
              for j in jobs.list_jobs(include_disabled=True)]
elif action == "cancel":
    result = jobs.remove_job(sys.argv[2])
elif action == "tick":
    result = scheduler.tick(verbose=False, sync=True)
elif action == "claim":
    result = jobs.claim_dispatch(sys.argv[2])
else:
    raise ValueError(action)
print("JARVIS_RESULT=" + json.dumps(result))
'''


def run(home, action, *args):
    home.mkdir(parents=True, exist_ok=True)
    # Child starts with only OS necessities; never load the running app's profile/secrets.
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"}}
    env.update(HERMES_HOME=str(home), HOME=str(home), USERPROFILE=str(home),
               PYTHONPATH=str(CORE), PYTHONIOENCODING="utf-8", TZ="UTC")
    proc = subprocess.run([hermes_python(), "-c", DRIVER, action, *args], cwd=CORE,
                          env=env, capture_output=True, text=True, encoding="utf-8",
                          timeout=60, stdin=subprocess.DEVNULL, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    results = [line.removeprefix("JARVIS_RESULT=") for line in proc.stdout.splitlines()
               if line.startswith("JARVIS_RESULT=")]
    assert len(results) == 1, proc.stdout
    return json.loads(results[0])


def test_reminder_persists_fires_and_does_not_repeat_after_restart(tmp_path):
    home = tmp_path / "profile"
    created = run(home, "create")
    assert created["kind"] == "once"
    assert [job["id"] for job in run(home, "list")] == [created["id"]]
    run(home, "tick")
    effect = home / "effects.txt"
    assert effect.read_text().splitlines() == ["reminder"]
    completed = run(home, "list")
    assert len(completed) == 1
    assert completed[0]["state"] == "completed"
    assert completed[0]["completed"] == 1
    run(home, "tick")
    assert effect.read_text().splitlines() == ["reminder"]
    outputs = list((home / "cron/output" / created["id"]).glob("*.md"))
    assert outputs and any("test reminder" in p.read_text(encoding="utf-8") for p in outputs)


def test_cancelled_reminder_never_fires_and_profiles_stay_isolated(tmp_path):
    home = tmp_path / "profile"
    created = run(home, "create")
    assert run(tmp_path / "other", "list") == []
    assert run(home, "cancel", created["id"]) is True
    assert run(home, "list") == []
    run(home, "tick")
    assert not (home / "effects.txt").exists()
    assert run(home, "cancel", created["id"]) is False


def test_dispatch_reserved_before_crash_is_not_executed_again(tmp_path):
    home = tmp_path / "profile"
    created = run(home, "create")
    # Child exits after the durable reservation, before any side effect.
    assert run(home, "claim", created["id"]) is True
    run(home, "tick")
    run(home, "tick")
    assert not (home / "effects.txt").exists()
    assert all(job["state"] != "scheduled" for job in run(home, "list"))
