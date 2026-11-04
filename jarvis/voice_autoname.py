"""Nomeia sozinho as vozes das reuniões a partir dos perfis vocais cadastrados.

Roda quando o cadastro de vozes muda (o senhor identificou alguém) ou quando surge voz nova sem
nome: reaplica os perfis a todas as vozes sem nome, nomeia as de alta confiança e, por reunião,
atualiza participantes, ata e Cerebro (meetings_service.set_voices). Vozes que o senhor já
nomeou, uniu ou descartou nunca são tocadas.
"""

import json
import os
import subprocess
import tempfile
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

import meetings_offline as offline
from offline_transcribe import VOICE_PYTHON
import meetings_repo as repo
import meetings_service as service
from storage import DB_PATH
from voice_matching import MIN_SCORE, VERSION

HERE = Path(__file__).resolve().parent
VOICES_DB = HERE.parent / "voice_identity" / "data" / "voices.sqlite3"
STATE = HERE / "voice_autoname_state.json"
AUTO_MIN = MIN_SCORE
TIMEOUT_S = 300
_lock = threading.Lock()


def _fingerprint(unnamed: List[Dict[str, Any]], db_path: str = DB_PATH) -> str:
    try:
        stat = VOICES_DB.stat()
        profiles = f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        profiles = "none"
    return f"{VERSION}|{profiles}|{repo.reference_fingerprint(db_path)}|{max((v['id'] for v in unnamed), default=0)}|{len(unnamed)}"


def _match(vectors: Dict[str, list | dict], db_path: str = DB_PATH) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="jarvis-autoname-") as tmp:
        src, out = Path(tmp) / "in.json", Path(tmp) / "out.json"
        src.write_text(json.dumps(vectors), encoding="utf-8")
        env = os.environ.copy()
        env["JARVIS_TRANSCRIPTS_DB"] = db_path
        subprocess.run([str(VOICE_PYTHON), str(HERE / "offline_diarize.py"), "--match", str(src), str(out)],
                       check=True, capture_output=True, timeout=TIMEOUT_S, env=env)
        return json.loads(out.read_text(encoding="utf-8"))


def run(db_path: str = DB_PATH, matcher=_match, force: bool = False) -> int:
    """Retorna quantas vozes foram nomeadas."""
    with _lock:
        unnamed = repo.unnamed_voices(db_path)
        stamp = _fingerprint(unnamed, db_path)
        try:
            last = json.loads(STATE.read_text(encoding="utf-8")).get("stamp")
        except (OSError, ValueError):
            last = None
        if not unnamed or (stamp == last and not force):
            return 0
        by_meeting: Dict[int, Dict[str, list | dict]] = defaultdict(dict)
        for v in unnamed:
            by_meeting[v["meeting_id"]][str(v["id"])] = json.loads(v["evidence"] or v["embedding"])
        named = 0
        for meeting_id, vectors in by_meeting.items():
            found = _match(vectors, db_path) if matcher is _match else matcher(vectors)
            items = []
            for key, hit in found.items():
                repo.set_voice_suggestion(int(key), (hit or {}).get("name"), (hit or {}).get("score"), db_path)
                if hit and hit.get("automatic") is True and hit["score"] >= AUTO_MIN:
                    items.append({"id": int(key), "name": hit["name"]})
            if items:
                service.set_voices(meeting_id, items, db_path, confirmed=False)
                named += len(items)
                print(f"[+] Reunião {meeting_id}: {len(items)} voz(es) nomeada(s) pelos perfis cadastrados.")
        STATE.write_text(json.dumps({"stamp": _fingerprint(repo.unnamed_voices(db_path), db_path)}), encoding="utf-8")
        return named


def kick(db_path: str = DB_PATH) -> None:
    """Dispara numa thread própria; sem efeito se já está rodando ou se nada mudou."""
    if not offline.enabled() or _lock.locked():
        return
    unnamed = repo.unnamed_voices(db_path)
    if not unnamed:
        return
    try:
        if json.loads(STATE.read_text(encoding="utf-8")).get("stamp") == _fingerprint(unnamed, db_path):
            return
    except (OSError, ValueError):
        pass

    def work() -> None:
        try:
            run(db_path)
        except Exception as exc:  # noqa: BLE001 — a nomeação automática nunca derruba o app
            print(f"[!] Nomeação automática de vozes falhou: {exc}")
    threading.Thread(target=work, daemon=True, name="jarvis-autoname").start()
