"""Opt-in visual check-ins, driven by the existing dashboard poll, without execution tools."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path

from cerebro_io import atomic_write, file_lock

DEFAULT = {"enabled": False, "interval_minutes": 60, "start": "09:00", "end": "18:00"}


def validate(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(DEFAULT):
        raise ValueError("Informe ativação, intervalo, início e fim.")
    if type(value["enabled"]) is not bool or type(value["interval_minutes"]) is not int or not 15 <= value["interval_minutes"] <= 240:
        raise ValueError("Intervalo deve ser inteiro entre 15 e 240 minutos.")
    for key in ("start", "end"):
        if not isinstance(value[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value[key]):
            raise ValueError("Horários devem usar HH:MM.")
    if value["start"] == value["end"]:
        raise ValueError("Início e fim devem ser diferentes.")
    return dict(value)


def within_hours(config: dict, now: datetime) -> bool:
    current = now.strftime("%H:%M")
    start, end = config["start"], config["end"]
    return start <= current < end if start < end else current >= start or current < end


def meeting_active(path: Path) -> bool | None:
    """Read only the existing meeting state; unknown state suppresses check-ins."""
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)) as conn:
            return conn.execute("SELECT 1 FROM meetings WHERE status IN ('open','prompting') LIMIT 1").fetchone() is not None
    except (OSError, sqlite3.Error):
        return None


class Store:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "config": dict(DEFAULT), "next_at": 0, "notice": None}
        if not isinstance(data, dict) or data.get("version") != 1 or not {"config", "next_at", "notice"} <= data.keys():
            raise ValueError("Configuração de proatividade ilegível; avisos suspensos.")
        validate(data["config"])
        if type(data.get("next_at")) not in (int, float) or not math.isfinite(data["next_at"]):
            raise ValueError("Estado de proatividade inválido.")
        notice = data.get("notice")
        if notice is not None and (not isinstance(notice, dict) or not isinstance(notice.get("id"), str)):
            raise ValueError("Aviso de proatividade inválido.")
        return data

    def save(self, config: dict, now: datetime) -> None:
        config = validate(config)
        with file_lock(self.path):
            self._load()  # never silently discard a corrupt state
            atomic_write(self.path, json.dumps({"version": 1, "config": config,
                         "next_at": now.timestamp() + config["interval_minutes"] * 60, "notice": None}))

    def dismiss(self, notice_id: str, now: datetime) -> None:
        with file_lock(self.path):
            data = self._load()
            if not isinstance(notice_id, str) or not data["notice"] or data["notice"]["id"] != notice_id:
                raise ValueError("Aviso vencido; atualize o painel.")
            data.update(notice=None, next_at=now.timestamp() + data["config"]["interval_minutes"] * 60)
            atomic_write(self.path, json.dumps(data))

    def poll(self, now: datetime, *, blocked: bool, meeting: bool | None | Callable[[], bool | None]) -> dict:
        with file_lock(self.path):
            data = self._load()
            config = data["config"]
            if callable(meeting):
                meeting = meeting() if config["enabled"] and not blocked and within_hours(config, now) else False
            reason = ("Desativado" if not config["enabled"] else "Pausado pelos controles de voz ou tarefa" if blocked
                      else "Estado da reunião indisponível" if meeting is None else "Reunião em andamento" if meeting
                      else "Fora do horário definido" if not within_hours(config, now) else "Ativo")
            if reason == "Ativo" and not data["notice"] and now.timestamp() >= data["next_at"]:
                data.update(notice={"id": uuid.uuid4().hex}, next_at=now.timestamp() + config["interval_minutes"] * 60)
                atomic_write(self.path, json.dumps(data))
            notice = data["notice"] if reason == "Ativo" else None
            return {"config": config, "status": reason, "notice": {**notice, "text": "Quer revisar suas prioridades ou fazer uma pausa?"} if notice else None}
