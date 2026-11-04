"""
Passada offline das reuniões (decidida com o senhor __JARVIS_USER__ em 05/10/2026).

A transcrição ao vivo corta a fala a cada 30 s e mistura quem fala; a ata ficava sem nexo.
Agora, ao encerrar a reunião, o áudio gravado (audio_archive) é montado na linha do tempo da
reunião, as vozes são separadas na gravação inteira e cada trecho é transcrito com contexto
(offline_transcribe.py, num subprocesso: isola a GPU e qualquer queda). O resultado vira
vozes ("Voz 1", "Voz 2"…) que o senhor reconhece pela página, e falas por falante que alimentam a ata.
"""
import calendar
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import wave
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import av
import numpy as np

import meeting_gate
import transcript_hygiene as hygiene
import meetings_repo as repo
from audio_archive import ARCHIVE_DIR, block_start, flush_recordings
from storage import DB_PATH

HERE = Path(__file__).resolve().parent
SAMPLES_DIR = HERE / "voice_samples"
RATE = 16000
SOURCES = ("mic", "loopback")
PAD_BEFORE_S = 60.0   # a fala é gravada no banco depois de transcrita: o áudio começa antes de started_at
PAD_AFTER_S = 15.0
BLOCK_TOLERANCE_S = 30.0
MIN_RMS = 0.003       # abaixo disso a fonte ficou muda (loopback sem som do PC)
MAX_ATTEMPTS = 3
RETRY_BASE_S = 600.0
CLI_TIMEOUT_S = 21600  # 6 h: a passada espera a fala ao vivo (gpu_priority) e pode demorar em dia de muita conversa
# Abaixo do normal no Windows (o diarizador, filho, herda): o PC e a escuta ao vivo vêm primeiro.
CLI_CREATIONFLAGS = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
ECHO_SIMILARITY = 0.6
ECHO_WINDOW_S = 4.0
_TS = "%Y-%m-%d %H:%M:%S"
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("JARVIS_OFFLINE", "1") != "0"


def _fmt(value: datetime) -> str:
    return value.strftime(_TS)


def _epoch(utc_text: str) -> float:
    return float(calendar.timegm(time.strptime(str(utc_text)[:19].replace("T", " "), _TS)))


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text).casefold()) if not unicodedata.combining(c))


def window(meeting: Dict[str, Any]) -> Tuple[float, float]:
    """(início, fim) do áudio da reunião em epoch, já com as folgas."""
    return _epoch(meeting["started_at"]) - PAD_BEFORE_S, _epoch(meeting["ended_at"]) + PAD_AFTER_S


def block_files(source: str, t0: float, t1: float, root: Path = ARCHIVE_DIR) -> List[Tuple[float, Path]]:
    """Blocos gravados que tocam [t0, t1]. O nome do arquivo é a hora local de abertura do bloco."""
    found = []
    for path in sorted((*((root / source).glob("*.ogg")), *((root / source).glob("*.flac")))):
        try:
            start = block_start(path)
        except ValueError:
            continue
        if start < t1 and start + 600 + BLOCK_TOLERANCE_S > t0:
            found.append((start, path))
    return found


def has_audio(meeting: Dict[str, Any], root: Path = ARCHIVE_DIR) -> bool:
    t0, t1 = window(meeting)
    return bool(block_files("mic", t0, t1, root))


def _decode(path: Path) -> np.ndarray:
    resampler = av.AudioResampler(format="flt", layout="mono", rate=RATE)
    parts = []
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                parts.append(out.to_ndarray().reshape(-1))
    return np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)


def assemble(source: str, t0: float, t1: float, out_wav: Path, root: Path = ARCHIVE_DIR,
             keep: Optional[List[Tuple[float, float]]] = None) -> bool:
    """Monta o áudio da fonte entre t0 e t1 (posição = relógio) em WAV 16 kHz. False se não há som."""
    files = block_files(source, t0, t1, root)
    if not files:
        return False
    timeline = np.zeros(int((t1 - t0) * RATE), dtype=np.float32)
    for start, path in files:
        try:
            samples = _decode(path)
        except Exception as exc:
            raise RuntimeError(f"Bloco de áudio {path.name} ilegível; revisão adiada") from exc
        offset = int(round((start - t0) * RATE))
        if offset < 0:
            samples, offset = samples[-offset:], 0
        samples = samples[:max(0, len(timeline) - offset)]
        timeline[offset:offset + len(samples)] = samples
    if keep is not None:  # silencia o que não é de aplicativo de reunião (vídeo, WhatsApp…)
        mask = np.zeros(len(timeline), dtype=bool)
        for start, end in keep:
            mask[max(0, int((start - t0) * RATE)):max(0, int((end - t0) * RATE))] = True
        timeline[~mask] = 0.0
    if float(np.sqrt(np.mean(timeline ** 2))) < MIN_RMS:
        return False
    pcm = (np.clip(timeline, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(out_wav), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm)
    return True


def drop_echo(mic: List[Dict[str, Any]], system: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fala do microfone que é o som do PC saindo pela caixa: igual a uma fala da chamada no mesmo instante."""
    kept = []
    for u in mic:
        folded = _fold(u["text"])
        echo = any(
            u["start"] < s["end"] + ECHO_WINDOW_S and s["start"] < u["end"] + ECHO_WINDOW_S
            and difflib.SequenceMatcher(None, folded, _fold(s["text"])).ratio() >= ECHO_SIMILARITY
            for s in system)
        if not echo:
            kept.append(u)
    return kept


def _python() -> str:
    venv = HERE / ".venv311" / "Scripts" / "python.exe"
    return str(venv) if venv.exists() else sys.executable


def run_cli(wav: Path, out_json: Path, samples_dir: Path, num_speakers: int = -1,
            db_path: str = DB_PATH) -> Dict[str, Any]:
    env = os.environ.copy()
    env["JARVIS_TRANSCRIPTS_DB"] = db_path
    subprocess.run([_python(), str(HERE / "offline_transcribe.py"), "--json", str(wav), str(out_json),
                    str(samples_dir), str(num_speakers)], check=True, capture_output=True, timeout=CLI_TIMEOUT_S, env=env,
                   creationflags=CLI_CREATIONFLAGS)
    return json.loads(out_json.read_text(encoding="utf-8"))


def _transcribe_sources(meeting: Dict[str, Any], tmp: Path, runner: Callable, root: Path,
                        db_path: str = DB_PATH) -> Dict[str, Dict[str, Any]]:
    flush_recordings()
    t0, t1 = window(meeting)
    results = {}
    for source in SOURCES:
        wav = tmp / f"{source}.wav"
        keep = meeting_gate.kept_intervals(t0, t1, db_path) if source == "loopback" else None
        if not assemble(source, t0, t1, wav, root, keep):
            continue
        samples = tmp / f"samples_{source}"
        samples.mkdir()
        data = (run_cli(wav, tmp / f"{source}.json", samples, meeting.get(f"{source}_speakers") or -1, db_path)
                if runner is run_cli else runner(wav, tmp / f"{source}.json", samples))
        results[source] = {"data": data, "samples": samples}
    return results


def _store(meeting_id: int, t0: float, results: Dict[str, Dict[str, Any]], db_path: str) -> int:
    for old in repo.clear_offline(meeting_id, db_path):
        (SAMPLES_DIR / old).unlink(missing_ok=True)
    SAMPLES_DIR.mkdir(exist_ok=True)
    loop = results.get("loopback", {}).get("data", {}).get("utterances", [])
    voices, utterances = [], []
    for source, item in results.items():
        data = item["data"]
        spoken = data["utterances"] if source == "loopback" else drop_echo(data["utterances"], loop)
        for label, info in data["speakers"].items():
            sample = None
            if info.get("sample"):
                sample = f"m{meeting_id}_{source}_{label}.wav"
                shutil.copyfile(item["samples"] / info["sample"], SAMPLES_DIR / sample)
            suggestion = info.get("suggestion") or {}
            voices.append({"source": source, "label": int(label), "seconds": info["seconds"],
                           "suggested_name": suggestion.get("name"), "suggested_score": suggestion.get("score"),
                           "sample_file": sample, "embedding": info.get("embedding"), "evidence": info.get("evidence")})
        for u in spoken:
            review = dict(u["review"]) if u.get("review") else None
            if review and review.get("sample"):
                original_sample = review.pop("sample")
                if original_sample != Path(original_sample).name:
                    raise ValueError("Nome de amostra de revisão inválido")
                sample = f"m{meeting_id}_{source}_{original_sample}"
                shutil.copyfile(item["samples"] / original_sample, SAMPLES_DIR / sample)
                review["sample_file"] = sample
            utterances.append({"source": source, "label": int(u["speaker"]), "text": u["text"],
                               "review": review,
                               "start_at": _fmt(datetime.utcfromtimestamp(t0 + u["start"])),
                               "end_at": _fmt(datetime.utcfromtimestamp(t0 + u["end"]))})
    utterances = [u for u in utterances if u.get("review") or not hygiene.hallucination_reason(u["text"])]
    used = {(u["source"], u["label"]) for u in utterances}
    voices = [v for v in voices if (v["source"], v["label"]) in used]  # voz que só falou eco não aparece
    repo.save_offline_result(meeting_id, voices, utterances, db_path)
    return len(utterances)


def process_meeting(meeting_id: int, db_path: str = DB_PATH, runner: Optional[Callable] = None,
                    root: Path = ARCHIVE_DIR, now: Optional[datetime] = None) -> bool:
    """Roda a passada de uma reunião encerrada. Falha volta para a fila com espera; esgotadas as
    tentativas, a ata sai da transcrição ao vivo. True se a passada terminou com falas."""
    meeting = repo.get_meeting(meeting_id, db_path)
    if not meeting or meeting["status"] != "closed":
        return False
    now = now or datetime.utcnow().replace(microsecond=0)
    repo.update_meeting(meeting_id, db_path=db_path, offline_status="running")
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-offline-") as tmp:
            results = _transcribe_sources(meeting, Path(tmp), runner or run_cli, root, db_path)
            stored = _store(meeting_id, window(meeting)[0], results, db_path) if results else 0
    except Exception as exc:  # noqa: BLE001 — GPU ocupada, falta de memória, modelo ausente…
        attempts = int(meeting.get("offline_attempts") or 0) + 1
        detail = getattr(exc, "stderr", b"") or b""
        print(f"[!] Passada offline da reunião {meeting_id} falhou (tentativa {attempts}): {exc} "
              f"{detail.decode('utf-8', 'replace')[-300:] if isinstance(detail, bytes) else ''}")
        if attempts >= MAX_ATTEMPTS:
            repo.update_meeting(meeting_id, db_path=db_path, offline_status="error", offline_attempts=attempts,
                                offline_next_try_at=None, ata_status="pending", ata_attempts=0, ata_next_try_at=None)
        else:
            repo.update_meeting(meeting_id, db_path=db_path, offline_status="pending", offline_attempts=attempts,
                                offline_next_try_at=_fmt(now + timedelta(seconds=RETRY_BASE_S * attempts)))
        return False
    status = "done" if stored else "none"  # sem fala/áudio: a ata continua vindo da transcrição ao vivo
    repo.update_meeting(meeting_id, db_path=db_path, offline_status=status, offline_attempts=0,
                        offline_next_try_at=None, ata_status="pending", ata_attempts=0, ata_next_try_at=None)
    print(f"[+] Passada offline da reunião {meeting_id}: {stored} falas por falante.")
    return bool(stored)


def run_pending(db_path: str = DB_PATH, runner: Optional[Callable] = None, root: Path = ARCHIVE_DIR,
                now: Optional[datetime] = None) -> int:
    """Processa as reuniões na fila, uma por vez. Retorna quantas terminaram."""
    done = 0
    with _lock:
        for meeting_id in repo.meetings_for_offline(_fmt(now or datetime.utcnow()), db_path):
            done += bool(process_meeting(meeting_id, db_path, runner, root, now))
    return done


def kick(db_path: str = DB_PATH) -> None:
    """Dispara a fila numa thread própria (a passada leva minutos); sem efeito se já está rodando."""
    if not enabled() or _lock.locked():
        return
    if not repo.meetings_for_offline(_fmt(datetime.utcnow()), db_path):
        return
    threading.Thread(target=run_pending, args=(db_path,), daemon=True, name="jarvis-offline").start()


def recover(db_path: str = DB_PATH) -> None:
    """No início do processo: passada que ficou "rodando" quando o app caiu volta para a fila."""
    repo.reset_running_offline(db_path)


def sample_path(sample_file: str) -> Path:
    """Caminho do trecho de uma voz; recusa nome que fuja da pasta de amostras."""
    path = (SAMPLES_DIR / Path(sample_file).name).resolve()
    if sample_file != Path(sample_file).name or path.parent != SAMPLES_DIR.resolve() or not path.is_file():
        raise LookupError("Amostra não encontrada.")
    return path
