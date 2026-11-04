"""Preferência local compartilhada: escutar sem iniciar ou reproduzir voz."""
import json
import os
from pathlib import Path

STATE_PATH = Path(__file__).with_name(".listen_only.json")


def enabled(path: Path = STATE_PATH) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))["enabled"]
        return value if type(value) is bool else True
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, TypeError):
        return True  # Estado ilegível nunca permite uma fala inesperada.


def save(value: bool, path: Path = STATE_PATH) -> None:
    if type(value) is not bool:
        raise ValueError("Informe enabled como verdadeiro ou falso.")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"enabled": value}), encoding="utf-8")
    os.replace(temporary, path)
