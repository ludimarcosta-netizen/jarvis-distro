"""Local PCM framing and isolated speech workers; no ML imports in Hermes."""
from __future__ import annotations

import atexit
import base64
import json
import math
import os
import queue
import re
import struct
import subprocess
import threading
import time
from collections import deque
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "voice_mode.json"


def settings() -> dict:
    value = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    if not isinstance(value, dict):
        raise TypeError("Configuração de voz inválida")
    for field in ("checkpoint", "vocab", "vocoder", "reference_audio", "piper_model", "kokoro_model_dir"):
        if value.get(field):
            path = Path(value[field]).expanduser()
            value[field] = str(path if path.is_absolute() else ROOT / path)
    return value


def enabled() -> bool:
    return settings().get("engine") == "local"


def recognition_device(config: dict) -> tuple[str, str]:
    device = config.get("stt_device", "cpu")
    if device not in {"cpu", "cuda"}:
        raise ValueError("Dispositivo de reconhecimento inválido")
    return device, "int8_float16" if device == "cuda" else "int8"


def synthesis_steps(config: dict, request: dict) -> int:
    steps = request.get("steps", config.get("tts_steps", 32))
    if type(steps) is not int or steps not in {16, 24, 32}:
        raise ValueError("Passos de síntese inválidos")
    return steps


def decode_pcm(data: str) -> bytes:
    if not isinstance(data, str) or len(data) > 128000:
        raise ValueError("Bloco de áudio inválido")
    pcm = base64.b64decode(data, validate=True)
    if len(pcm) % 2:
        raise ValueError("PCM16 inválido")
    return pcm


class Segmenter:
    """Bounded 16 kHz PCM VAD with pre-roll and a 640 ms endpoint."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.frames: list[bytes] = []
        self.pre: deque[bytes] = deque(maxlen=2)
        self.silence = self.samples = self.voiced = 0

    def feed(self, pcm: bytes, playback: bool = False) -> bytes | None:
        if not pcm:
            return None
        values = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        rms = math.sqrt(sum(v * v for v in values) / len(values)) / 32768
        speech = rms >= (0.035 if playback else 0.012)
        if not self.frames and not speech:
            self.pre.append(pcm)
            return None
        if not self.frames:
            self.frames.extend(self.pre)
            self.pre.clear()
        self.frames.append(pcm)
        self.samples += len(values)
        self.voiced += len(values) if speech else 0
        self.silence = 0 if speech else self.silence + len(values)
        if self.silence < 10240 and self.samples < 320000:
            return None
        result = b"".join(self.frames) if self.voiced >= 4800 else None
        self.reset()
        return result


_UNITS = ("zero", "um", "dois", "três", "quatro", "cinco", "seis", "sete", "oito", "nove",
          "dez", "onze", "doze", "treze", "catorze", "quinze", "dezesseis", "dezessete", "dezoito", "dezenove")
_TENS = ("", "", "vinte", "trinta", "quarenta", "cinquenta", "sessenta", "setenta", "oitenta", "noventa")
_HUNDREDS = ("", "cento", "duzentos", "trezentos", "quatrocentos", "quinhentos",
             "seiscentos", "setecentos", "oitocentos", "novecentos")
_MONTHS = ("", "janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
           "agosto", "setembro", "outubro", "novembro", "dezembro")
_SPOKEN_TOKEN = re.compile(
    r"https?://\S+|(?<![\w/])\d{1,2}/\d{2}(?:/\d{2,4})?(?![\w/])|"
    r"\b\d{1,2}(?:h\d{0,2}|:\d{2})(?!\w)|"
    r"\b[A-Za-zÀ-ÿ_][\w.-]*\d[\w.-]*|"
    r"(?<![\w/])(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?%?(?![\w/])"
)


def _integer_words(value: int) -> str:
    if value < 20:
        return _UNITS[value]
    if value < 100:
        tens, units = divmod(value, 10)
        return _TENS[tens] + (f" e {_UNITS[units]}" if units else "")
    if value < 1000:
        if value == 100:
            return "cem"
        hundreds, remainder = divmod(value, 100)
        return _HUNDREDS[hundreds] + (f" e {_integer_words(remainder)}" if remainder else "")
    for scale, singular, plural in ((1_000_000_000, "bilhão", "bilhões"),
                                     (1_000_000, "milhão", "milhões"),
                                     (1000, "mil", "mil")):
        if value >= scale:
            count, remainder = divmod(value, scale)
            prefix = "" if scale == 1000 and count == 1 else _integer_words(count) + " "
            result = prefix + (singular if count == 1 else plural)
            if remainder:
                joiner = " e " if remainder < 100 or remainder % 100 == 0 else " "
                result += joiner + _integer_words(remainder)
            return result
    raise ValueError("Número fora do intervalo de síntese")


def _spoken_numbers(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group()
        if token.startswith(("http://", "https://")) or token[0].isalpha() or token[0] == "_":
            return token
        if "/" in token:
            fields = token.split("/")
            day, month = int(fields[0]), int(fields[1])
            year = int(fields[2]) if len(fields) == 3 else 2000
            try:
                date(year + 2000 if len(fields) == 3 and len(fields[2]) == 2 else year, month, day)
            except ValueError:
                return token
            spoken_day = "primeiro" if day == 1 else _integer_words(day)
            spoken = f"{spoken_day} de {_MONTHS[month]}"
            return spoken + (f" de {_integer_words(year)}" if len(fields) == 3 else "")
        if "h" in token or ":" in token:
            hour, _, minute = token.partition("h" if "h" in token else ":")
            hours = "uma hora" if int(hour) == 1 else f"{_integer_words(int(hour))} horas"
            if minute:
                minutes = "um minuto" if int(minute) == 1 else f"{_integer_words(int(minute))} minutos"
                return hours + " e " + minutes
            return hours
        percent = token.endswith("%")
        number = token.removesuffix("%")
        whole, separator, fraction = number.partition(",")
        digits = whole.replace(".", "")
        spoken = " ".join(_UNITS[int(digit)] for digit in digits) if len(digits) > 1 and digits[0] == "0" else _integer_words(int(digits))
        if separator:
            fractional = (" ".join(_UNITS[int(digit)] for digit in fraction)
                          if len(fraction) > 1 and fraction[0] == "0" else _integer_words(int(fraction)))
            spoken += " vírgula " + fractional
        return spoken + (" por cento" if percent else "")

    return _SPOKEN_TOKEN.sub(replace, text)


def speech_parts(text: str) -> list[str]:
    # Cards belong to the screen, not the spoken answer. Preserve factual text.
    text = re.sub(r"::[\w-]+\{[^\n]*?\}", "", text)
    text = re.sub(r"```.*?```", " Trecho de código disponível na tela. ", text, flags=re.DOTALL)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*#`|]", " ", text)
    text = " ".join(text.split())
    text = _spoken_numbers(text)
    if len(text) <= 180:
        return [text] if text else []
    parts, current = [], ""
    for word in text.split():
        # A smaller first phrase reduces time to meaningful speech; later blocks stay efficient.
        limit = 110 if not parts else 180
        if current and len(current) + 1 + len(word) > limit:
            parts.append(current)
            current = ""
        current = f"{current} {word}".strip()
        if not parts and len(current) >= 70 and word.rstrip('\"”’').endswith((".", "!", "?")):
            parts.append(current)
            current = ""
    if current:
        parts.append(current)
    # Don't trade initial latency for an unusably short final F5 utterance.
    if len(parts) > 1 and len(parts[-1]) < 70:
        if len(parts[-2]) + 1 + len(parts[-1]) <= 180:
            parts[-2:] = [parts[-2] + " " + parts[-1]]
        else:
            while len(parts[-1]) < 70 and " " in parts[-2]:
                previous, word = parts[-2].rsplit(" ", 1)
                if len(previous) < 70:
                    break
                parts[-2], parts[-1] = previous, word + " " + parts[-1]
    return parts


class Worker:
    """Private JSONL subprocess, serialized requests, bounded wait and explicit errors."""

    def __init__(self, kind: str):
        self.kind = kind
        self.process = None
        self.lock = threading.Lock()
        self.responses: queue.Queue = queue.Queue()
        atexit.register(self.close)

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                if os.name == "nt":
                    # Windows venv python.exe is a launcher. Killing only it leaves ML children alive.
                    subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"),
                                    "/PID", str(self.process.pid), "/T", "/F"],
                                   capture_output=True, check=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    self.process.kill()
            self.process.wait(timeout=10)
            for pipe in (self.process.stdin, self.process.stdout):
                if pipe is not None:
                    pipe.close()
            self.process = None

    def _start(self):
        tts_engine = settings().get("tts_engine", "f5") if self.kind == "tts" else "f5"
        if tts_engine not in {"f5", "piper", "kokoro"}:
            raise ValueError("Motor de síntese inválido")
        piper = tts_engine == "piper"
        kokoro = tts_engine == "kokoro"
        kokoro_device = settings().get("kokoro_device", "cpu") if kokoro else "cpu"
        if kokoro_device not in {"cpu", "cuda"}:
            raise ValueError("Dispositivo Kokoro inválido")
        if kokoro:
            runtime = ".venv-kokoro-gpu" if kokoro_device == "cuda" else ".venv-kokoro"
        else:
            runtime = ".venv-piper" if piper else ".venv-voice" if self.kind == "tts" else ".venv311"
        python = ROOT / runtime / "Scripts" / "python.exe"
        if not python.is_file():
            raise RuntimeError("Ambiente de voz local não instalado")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2"}
        for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
            env.pop(name, None)
        if kokoro:
            env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        self.responses = queue.Queue()
        log_dir = ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        with (log_dir / f"local-{self.kind}.log").open("ab") as log:
            self.process = subprocess.Popen(
                [str(python), "-I", "-X", "utf8",
                 str(ROOT / ("local_kokoro_worker.py" if kokoro else "local_piper_worker.py" if piper else "local_voice_worker.py")), self.kind],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True,
                encoding="utf-8", env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        process, responses = self.process, self.responses

        def receive():
            try:
                for line in process.stdout:
                    responses.put(json.loads(line))
            except (ValueError, OSError):
                pass
            finally:
                responses.put({"ok": False, "error": "Processo de voz local encerrou"})

        threading.Thread(target=receive, daemon=True, name=f"voice-{self.kind}").start()

    def call(self, body: dict, timeout: float = 180) -> dict:
        asked = time.perf_counter()
        with self.lock:
            waited = time.perf_counter() - asked
            sent = time.perf_counter()
            if self.process is None or self.process.poll() is not None:
                self._start()
            try:
                self.process.stdin.write(json.dumps(body, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
                result = self.responses.get(timeout=timeout)
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "Falha na voz local")
                result["timing"] = {"lock_wait_s": round(waited, 3), "round_trip_s": round(time.perf_counter() - sent, 3),
                                    "synth_s": result.pop("synth_s", None)}
                return result
            except (queue.Empty, BrokenPipeError, OSError) as exc:
                self.close()
                raise RuntimeError("A voz local não respondeu dentro do prazo") from exc


stt = Worker("stt")
tts = Worker("tts")


def warmup() -> None:
    """Load and exercise resident workers sequentially, without opening a conversation."""
    for worker in (stt, tts):
        worker.call({"op": "warmup"})
