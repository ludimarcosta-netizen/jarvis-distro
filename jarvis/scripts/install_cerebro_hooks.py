"""Callbacks locais de fim de tarefa; preserva hooks, permissões e notify existentes."""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import tomllib
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.install_cerebro import save


def command(runtime: Path, python: Path, source: str) -> str:
    return f'"{python.as_posix()}" -B "{(runtime / "cerebro_capture.py").as_posix()}" --source {source}'


def load_json(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    result = json.loads(raw) if raw.strip() else {}
    if not isinstance(result, dict):
        raise TypeError("Configuração deve ser objeto")
    return result


def install(home: Path, runtime: Path, python: Path, backup: Path) -> dict:
    backup.mkdir(parents=True, exist_ok=True)
    changed = []
    path = home / ".claude/settings.json"
    data = load_json(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError("Hooks do Claude em formato incompatível; preservados")
    group = {"hooks": [{"type": "command", "command": command(runtime, python, "claude"), "timeout": 5}]}
    stop = hooks.setdefault("Stop", [])
    if group not in stop:
        stop.append(group)
    if save(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", backup):
        changed.append(str(path))
    path = home / ".gemini/config/hooks.json"
    data = load_json(path)
    entry = {"enabled": True, "Stop": [{"type": "command", "command": command(runtime, python, "antigravity"), "timeout": 5}]}
    if "cerebro-capture" in data and data["cerebro-capture"] != entry:
        raise ValueError("Hook cerebro-capture já existe com outra configuração")
    data["cerebro-capture"] = entry
    if save(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", backup):
        changed.append(str(path))
    path = home / ".codex/config.toml"
    text = path.read_text(encoding="utf-8-sig")
    config = tomllib.loads(text)
    replacement = [str(python), "-B", str(runtime / "cerebro_capture.py"), "--source", "codex", "--notify"]
    original = config.get("notify")
    if original != replacement:
        stored = runtime / ".notify_original.json"
        if stored.exists():
            raise ValueError("Callback original já preservado; conferir alteração externa antes de instalar")
        if original:
            save(stored, json.dumps(original, ensure_ascii=False), backup)
        line = "notify = " + json.dumps(replacement)
        if original is None:
            new = line + "\n" + text
        else:
            new, count = re.subn(r"(?ms)^notify\s*=\s*\[.*?\]", lambda _: line, text, count=1)
            if count != 1:
                raise ValueError("Não foi possível localizar notify")
        actual = tomllib.loads(new)
        if actual.get("notify") != replacement or {k:v for k,v in actual.items() if k != "notify"} != {k:v for k,v in config.items() if k != "notify"}:
            raise ValueError("Configuração TOML não preservada")
        if save(path, new, backup):
            changed.append(str(path))
    path = runtime.parent / "config.yaml"
    text = path.read_text(encoding="utf-8-sig")
    config = yaml.safe_load(text)
    hooks = config.get("hooks") or {}
    if not isinstance(hooks, dict):
        raise TypeError("Hooks do Hermes incompatíveis; preservados")
    hermes_command = command(runtime, python, "hermes")
    entry = {"command": hermes_command, "timeout": 5, "fail_closed": False}
    entries = hooks.setdefault("on_session_end", [])
    if entry not in entries:
        entries.append(entry)
    fragment = yaml.safe_dump({"hooks": hooks}, allow_unicode=True, sort_keys=False)
    if "hooks" not in config:
        new = text.rstrip() + "\n\n" + fragment
    else:
        new, count = re.subn(r"(?m)^hooks:[^\n]*(?:\n(?:[ \t].*|[ \t]*|#.*))*", lambda _: fragment.rstrip(), text, count=1)
        if count != 1:
            raise ValueError("Bloco hooks do Hermes não encontrado")
    actual = yaml.safe_load(new)
    if {k:v for k,v in actual.items() if k != "hooks"} != {k:v for k,v in config.items() if k != "hooks"}:
        raise ValueError("Configuração do Hermes não preservada")
    if save(path, new, backup):
        changed.append(str(path))
    return {"alterados": changed, "hermes_command": hermes_command, "backup": str(backup)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", required=True, action="store_true")
    parser.parse_args()
    runtime = Path.home() / "AppData/Local/hermes/jarvis"
    backup = runtime / "backups" / ("captura-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    print(json.dumps(install(Path.home(), runtime, runtime / ".venv311/Scripts/python.exe", backup), ensure_ascii=False))
