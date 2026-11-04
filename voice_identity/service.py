"""Authenticated loopback helper; bounded queues keep ML off the live audio path."""
from __future__ import annotations

import base64
import ctypes
import io
import json
import math
import queue
import secrets
import threading
import time
import wave
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from confirmation import Confirmations, requires_screen
from hints import meeting_participant_name, spoken_name
from store import Store, pcm_from_wav, protect, similarity, unit

ROOT = Path(__file__).resolve().parent
PORT = 17839
MAX_BODY = 1100000
ERROR_ALREADY_EXISTS = 183


def acquire_instance_mutex():
    """Keep exactly one helper alive even though Windows HTTPServer reuses ports."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, True, r"Local\HermesVoiceIdentity17839")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def wav_base64(pcm: bytes) -> str:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return base64.b64encode(output.getvalue()).decode("ascii")


class Service:
    def __init__(self, root: Path = ROOT, engine=None, store=None):
        self.root = root
        self.root.joinpath("data").mkdir(exist_ok=True)
        self.store = store or Store(root / "data" / "voices.sqlite3")
        self.lock = threading.RLock()
        self.confirm = Confirmations()
        self.mode = self.load_mode()
        self.engine = engine
        self.foreground_engine = engine
        self.error = ""
        self.latest: dict = {}
        self.session = ""
        self.generation = 0
        # Foreground work includes guided enrollment. Diarization can be slower
        # than real time on CPU, so keep enough headroom to avoid dropping valid
        # enrollment batches while the user is still speaking.
        self.queue = queue.Queue(maxsize=24)
        self.ambient_queue = queue.Queue(maxsize=8)
        self.dropped = 0
        self.processing_s = 0.0
        self.foreground_busy = False
        self.live_audio_at = 0.0
        self.buffers: dict[str, tuple[bytearray, float]] = {}
        self.enrollment: dict = {}
        self._review_cache: tuple[float, list[dict], list[dict]] | None = None
        # Recent recognized voices (epoch, profile id, name), memory only: meeting
        # participant suggestions read it; lost on restart by design.
        self.heard: deque[tuple[float, str, str, bool]] = deque(maxlen=4000)

    def clear_pending_audio(self):
        for work_queue in (self.queue, self.ambient_queue):
            while True:
                try:
                    work_queue.get_nowait()
                except queue.Empty:
                    break
                else:
                    work_queue.task_done()

    def load_mode(self) -> str:
        path = self.root / "settings.json"
        if not path.exists():
            return "off"
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("mode") not in ("off", "observe", "protect"):
            raise ValueError("Configuração de voz inválida")
        return "observe" if data["mode"] == "protect" else data["mode"]

    def set_mode(self, mode: str):
        if mode not in ("off", "observe", "protect"):
            raise ValueError("Modo inválido")
        mode = "observe" if mode == "protect" else mode
        self.mode = mode
        self.generation += 1
        self.confirm.cancel()
        self.buffers.clear()
        self.latest = {}
        self.enrollment = {}
        path = self.root / "settings.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"mode": mode, "schema_version": 1}), encoding="utf-8")
        tmp.replace(path)

    def status(self) -> dict:
        self.confirm.expire()
        self.expire_enrollment()
        now = time.monotonic()
        if self._review_cache is None or now - self._review_cache[0] >= 5:
            candidates = self.store.candidate_summaries()
            profiles = self.store.profiles()
            self._review_cache = (time.monotonic(), candidates, profiles)
        _, candidates, profiles = self._review_cache
        profile_names = {profile["id"]: profile["name"] for profile in profiles}
        pending = self.confirm.pending
        return {"available": True, "mode": self.mode,
                "ready": self.engine is not None and self.foreground_engine is not None,
                "error": self.error, "latest": self.latest, "dropped": self.dropped, "session": self.session,
                "processing_s": round(self.processing_s, 3),
                "live_buffer_s": round(len(self.buffers.get("live", (bytearray(), 0.0))[0]) / 32000, 3),
                "live_audio_age_s": (round(max(0.0, time.monotonic() - self.live_audio_at), 3)
                                     if self.live_audio_at else None),
                "foreground_queue": self.queue.qsize(),
                "foreground_busy": self.foreground_busy,
                "profiles": [{k: p[k] for k in ("id", "name", "owner")} for p in profiles],
                "candidates": [{**{k: c[k] for k in ("id", "created", "expires", "seconds", "source")},
                                "enrollment_ready": c.get("enrollment_ready", False),
                                "replace_id": c.get("replace_id", ""),
                                "suggested_id": c.get("suggested_id", ""),
                                "suggested_name": profile_names.get(c.get("suggested_id", ""), ""),
                                "hint_name": c.get("hint_name", ""),
                                "confidence": c.get("confidence", 0.0),
                                "chunks": c.get("chunks", 0),
                                "label_name": c.get("label_name", ""),
                                "label_owner": c.get("label_owner", False)}
                               for c in candidates],
                "enrollment": {k: self.enrollment[k] for k in ("state", "seconds", "error", "id", "hint", "batches_received")
                               if k in self.enrollment},
                "review_due": bool(candidates) and time.localtime().tm_hour >= 18,
                "pending": ({k: pending[k] for k in ("id", "digest", "request", "screen_only")}
                            | {"remaining_s": max(0, int(pending["expires"] - time.monotonic()))}
                            if pending else None)}

    def audio(self, body: dict):
        if self.mode == "off":
            return {"ok": True, "skipped": True}
        source = body.get("source")
        if source not in ("live", "ambient", "enrollment"):
            raise ValueError("Fonte inválida")
        session = body.get("session", "")
        if source in ("live", "enrollment") and (not session or session != self.session):
            return {"ok": False, "error": "stale session"}
        if source == "ambient" and self.session and time.monotonic() - self.live_audio_at < 3:
            return {"ok": True, "skipped": True}
        encoded = body.get("data", "")
        if not isinstance(encoded, str) or len(encoded) > 900000:
            raise ValueError("Áudio excedeu o limite")
        pcm = base64.b64decode(encoded, validate=True)
        if len(pcm) % 2 or len(pcm) > 640000:
            raise ValueError("PCM16 inválido")
        if source == "ambient" and len(pcm) > 16000 * 2 * 5:
            pcm = pcm[-16000 * 2 * 5:]
        hint_name, hint_id = spoken_name(body.get("text", ""), self.store.profiles()) if source == "ambient" else ("", "")
        if body.get("playback"):
            self.buffers.pop(source, None)
            return {"ok": True, "skipped": True}
        now = time.monotonic()
        if source == "live":
            self.live_audio_at = now
        captured = float(body.get("captured_at", now))
        if not math.isfinite(captured) or captured > now or now - captured > 15:
            self.buffers.pop(source, None)
            self.dropped += 1
            return {"ok": True, "skipped": True}
        buf, started = self.buffers.get(source, (bytearray(), captured))
        buf.extend(pcm)
        self.buffers[source] = (buf, started)
        # Live commands need an owner estimate before Gemini can delegate a short turn.
        # Enrollment keeps longer batches for profile quality.
        batch_seconds = 2 if source == "live" else 5
        if len(buf) >= 16000 * 2 * batch_seconds or source == "ambient":
            if len(buf) >= 16000 * 2 * 2:
                pending = self.confirm.pending
                item = (bytes(buf[:640000]), source, session, started,
                        pending["id"] if pending else "", self.mode, self.generation,
                        hint_name, hint_id)
                try:
                    work_queue = self.ambient_queue if source == "ambient" else self.queue
                    work_queue.put_nowait(item)
                except queue.Full:
                    self.dropped += 1
            self.buffers.pop(source, None)
        return {"ok": True}

    def worker(self, foreground=False):
        work_queue = self.queue if foreground else self.ambient_queue
        engine_attr = "foreground_engine" if foreground else "engine"
        try:
            if getattr(self, engine_attr) is None:
                from engine import Engine
                setattr(self, engine_attr, Engine(self.root / "models"))
        except Exception as exc:  # noqa: BLE001 -- model loaders raise provider-specific errors
            self.error = f"Falha ao carregar modelos locais ({type(exc).__name__})"
        while True:
            try:
                item = work_queue.get(timeout=5)
            except queue.Empty:
                if not foreground:
                    with self.lock:
                        self.store.prune_candidates()  # retention does not decrypt biometric samples
                        self.confirm.expire()
                        self.expire_enrollment()
                continue
            try:
                if getattr(self, engine_attr) is not None:
                    if foreground:
                        with self.lock:
                            self.foreground_busy = True
                    self.process(item)
            except Exception as exc:  # noqa: BLE001 -- failure is surfaced, never grants authorization
                self.error = f"Falha na análise local ({type(exc).__name__}); use confirmação na tela"
                self.latest = {}
            finally:
                if foreground:
                    with self.lock:
                        self.foreground_busy = False
                work_queue.task_done()

    def process(self, item):
        pcm, source, session, captured, challenge_id, mode, generation, hint_name, hint_id = item
        engine = self.foreground_engine if source in ("live", "enrollment") else self.engine
        if engine is None:
            return
        def stale():
            return (generation != self.generation or mode != self.mode or self.mode == "off"
                    or (source == "live"
                        and (session != self.session or time.monotonic() - captured > 15))
                    or (source == "enrollment" and session != self.session)
                    or (source == "ambient" and bool(self.session)
                        and time.monotonic() - self.live_audio_at < 3))
        with self.lock:
            if stale():
                return
            enrolling = self.enrollment.get("state") == "recording" and source in ("live", "enrollment")
        started = time.monotonic()
        if enrolling:
            spans = engine.segments(pcm, enrollment=True)
        elif source == "live":
            # Keep the 2s biometric minimum, but merge clean same-speaker fragments
            # split by natural pauses/diarization inside the current live batch.
            spans = engine.segments(pcm, merge_speech=True)
        else:
            spans = engine.segments(pcm)
        self.processing_s = time.monotonic() - started
        with self.lock:
            if not stale() and self.enrollment.get("state") == "recording":
                self.collect_enrollment(spans, source, session, captured)
                return
        for span in spans:
            with self.lock:
                if stale():
                    return
                legacy_vector = engine.legacy_embedding(span["samples"])
                embeddings = {engine.model_id: span["embedding"]}
                if legacy_vector is not None:
                    embeddings[engine.legacy_model_id] = legacy_vector
                match = self.store.match_models(embeddings)
                self.latest = {**match, "source": source, "session": session,
                               "at": time.time() - (time.monotonic() - captured),
                               "start": round(span["start"], 2), "end": round(span["end"], 2)}
                if source in ("ambient", "live") and match.get("id"):
                    self.heard.append((self.latest["at"], match["id"], match["name"], bool(match.get("owner"))))
                if source in ("ambient", "live"):
                    self.store.candidate(
                        span["embedding"], wav_base64(span["pcm"][:320000]),
                        min(10, span["end"] - span["start"]), source, engine.model_id,
                        suggested_id=match.get("id") or "",
                        hint_name=hint_name, confidence=match.get("score", 0.0) if match.get("id") else 0.0)
                    self._review_cache = None
                # Speaker identity is advisory; it never grants command permission.
        self.processing_s = time.monotonic() - started

    def expire_enrollment(self):
        if self.enrollment.get("state") == "recording" and time.monotonic() - self.enrollment["started"] > 180:
            self.enrollment = {"state": "error", "seconds": 0,
                               "error": "Tempo esgotado. Grave novamente em ambiente silencioso."}

    def collect_enrollment(self, spans, source, session, captured):
        self.expire_enrollment()
        job = self.enrollment
        if (job.get("state") != "recording" or source not in ("live", "enrollment") or session != self.session
                or captured < job["started"] or captured in job["batches"]):
            return
        job["batches_received"] = job.get("batches_received", 0) + 1
        job["hint"] = ""
        clean = [span for span in spans if span.get("pcm") and span.get("embedding") is not None]
        if len({span.get("speaker", 0) for span in clean}) > 1:
            self.enrollment = {"state": "error", "seconds": 0, "error": "Mais de uma voz. Grave uma pessoa por vez."}
            return
        if not clean:
            job["hint"] = "Trecho sem fala suficiente ou com vozes misturadas. Continue falando sozinho."
            return
        # Diarization can split one speaker into several short spans. Count all clean
        # speech from that speaker in this independent batch and keep one batch embedding.
        pcm_bytes = sum(len(span["pcm"]) for span in clean)
        if pcm_bytes < 32000:
            job["hint"] = "Trecho com menos de 1s de fala útil. Continue falando sem pausas longas."
            return
        dimensions = {len(span["embedding"]) for span in clean}
        if len(dimensions) != 1:
            self.enrollment = {"state": "error", "seconds": 0, "error": "Falha ao analisar a amostra. Grave novamente."}
            return
        dimension = dimensions.pop()
        vector = unit([
            sum(float(span["embedding"][i]) * len(span["pcm"]) for span in clean) / pcm_bytes
            for i in range(dimension)
        ])
        # Compare with the running centroid of accepted batches. The same voice
        # across independent 5s windows measures ~0.70-0.85 here; a hard
        # any-previous-<0.75 gate rejected the owner's own voice and stalled
        # enrollment. 0.62 vs the centroid still rejects a different person
        # (those measure well below 0.6 with this extractor).
        if job["embeddings"]:
            centroid = unit([sum(previous[i] for previous in job["embeddings"]) / len(job["embeddings"])
                             for i in range(dimension)])
            if similarity(vector, centroid) < 0.62:
                self.enrollment = {"state": "error", "seconds": 0,
                                   "error": "Voz inconsistente ou ruído. Refaça sozinho, na mesma distância."}
                return
        job["batches"].add(captured)
        job["embeddings"].append(vector)
        job["pcm"].append(b"".join(span["pcm"] for span in clean))
        job["seconds"] += pcm_bytes / 32000
        if job["seconds"] >= 20 and len(job["embeddings"]) >= 4:
            identity = self.store.prepared_candidate(job["embeddings"], wav_base64(b"".join(job["pcm"])),
                                                      job["seconds"], self.foreground_engine.model_id,
                                                      job["replace_id"])
            self.enrollment = {"state": "ready", "seconds": job["seconds"], "id": identity}

    def dispatch(self, path: str, body: dict) -> dict:
        if path == "/health":
            return {"available": True}
        with self.lock:
            if path == "/status":
                return self.status()
            if path == "/settings":
                self.set_mode(body.get("mode"))
            elif path == "/session":
                next_session = str(body.get("session") or "")[:80]
                if next_session == self.session:
                    return {"ok": True}
                self.session = next_session
                self.generation += 1
                self.clear_pending_audio()
                self.buffers.clear()
                self.live_audio_at = 0.0
                self.latest = {}
                self.confirm.cancel()
                self.enrollment = {}
            elif path == "/audio":
                return self.audio(body)
            elif path == "/enrollment":
                if (self.mode == "off" or not self.session or self.confirm.pending
                        or self.foreground_engine is None):
                    raise ValueError("Ative o reconhecimento local antes de cadastrar")
                replacement = str(body.get("replace_id") or "")
                if replacement and not any(p["id"] == replacement for p in self.store.profiles()):
                    raise ValueError("Cadastro inexistente")
                self.generation += 1
                self.clear_pending_audio()
                self.buffers.clear()
                self.enrollment = {"state": "recording", "started": time.monotonic(), "seconds": 0,
                                   "embeddings": [], "pcm": [], "batches": set(), "replace_id": replacement}
            elif path == "/enroll":
                ids = body.get("ids", [])
                if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str):
                    raise ValueError("Escolha um cartão de voz")
                selected = self.store.candidate_by_id(ids[0])
                if not selected:
                    raise ValueError("Amostra ausente")
                if selected.get("replace_id"):
                    identity = self.store.enroll(ids, str(body.get("name", "")),
                                                 body.get("owner") is True, body.get("consent") is True)
                    result = {"id": identity, "pending": False, "action": "replaced"}
                else:
                    result = self.store.save_identity(ids[0], str(body.get("name", "")),
                                                      body.get("owner") is True, body.get("consent") is True)
                if not result.get("pending"):
                    self.generation += 1
                    self.enrollment = {}
                self._review_cache = None
                return {"ok": True, **result}
            elif path == "/delete":
                self.generation += 1
                self.store.delete(str(body.get("id", "")), body.get("candidate") is True)
                self.latest = {}
                self.confirm.cancel()
                self._review_cache = None
            elif path == "/review":
                if type(body.get("confirmed")) is not bool:
                    raise ValueError("Revisão inválida")
                result = self.store.review(str(body.get("id", "")), body["confirmed"])
                self._review_cache = None
                return {"ok": True, "saved": result is not None, **(result or {})}
            elif path == "/meeting-hint":
                name = meeting_participant_name(body.get("text", ""))
                if not name:
                    raise ValueError("Nome de participante não encontrado")
                hinted = self.store.hint_recent_meeting(name)
                self._review_cache = None
                return {"ok": True, "hinted": hinted}
            elif path == "/heard":
                since, until = body.get("since"), body.get("until")
                if not all(type(v) in (int, float) and math.isfinite(v) for v in (since, until)):
                    raise ValueError("Intervalo inválido")
                counts = Counter((pid, name, owner) for at, pid, name, owner in self.heard if since <= at <= until)
                return {"ok": True, "voices": [{"id": pid, "name": name, "owner": owner, "count": n}
                                               for (pid, name, owner), n in counts.most_common()]}
            elif path == "/sample":
                c = self.store.candidate_by_id(str(body.get("id", "")))
                if c is None:
                    raise ValueError("Amostra expirada")
                pcm = pcm_from_wav(c.get("sample", ""))
                if not pcm:
                    raise ValueError("Amostra indisponível")
                duration = len(pcm) // 32000
                offset = body.get("offset", 0)
                if type(offset) is not int or offset < 0 or offset > duration:
                    raise ValueError("Trecho inválido")
                start = min(offset * 32000, max(0, len(pcm) - 32000))
                return {"ok": True, "audio": wav_base64(pcm[start:start + 12 * 32000]),
                        "offset": start // 32000, "duration": duration}
            elif path == "/challenge":
                if body.get("session") != self.session or not self.session or self.enrollment.get("state") == "recording":
                    raise ValueError("Sessão protegida indisponível")
                self.buffers.clear()  # old audio cannot answer a new challenge
                request = str(body.get("request", ""))
                if not requires_screen(request):
                    raise ValueError("Este pedido não precisa de confirmação na tela")
                p = self.confirm.create(request, self.session, screen_only=True)
                return {"ok": True, **p}
            elif path == "/decision":
                if type(body.get("allow")) is not bool:
                    raise ValueError("Decisão inválida")
                self.confirm.screen(str(body.get("id", "")), str(body.get("digest", "")), body["allow"])
            elif path == "/result":
                return self.confirm.result(str(body.get("id", "")), str(body.get("digest", "")))
            elif path == "/cancel":
                self.confirm.cancel()
            else:
                raise ValueError("Operação desconhecida")
            return {"ok": True}


def run():
    instance_mutex = acquire_instance_mutex()
    if instance_mutex is None:
        return
    service = Service()
    token_path = ROOT / "data" / "token.bin"
    if token_path.exists():
        token = protect(token_path.read_bytes(), decrypt=True).decode()
    else:
        token = secrets.token_urlsafe(32)
        token_path.write_bytes(protect(token.encode()))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # never log audio, names, pending commands or credentials

        def do_POST(self):
            self.connection.settimeout(3)
            if self.headers.get("Origin") or not secrets.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    raise ValueError("Requisição excedeu o limite")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise TypeError("Requisição inválida")
                result = service.dispatch(self.path, body)
                code = 200
            except ValueError as exc:
                message = str(exc) if self.path == "/enroll" else "Pedido inválido, expirado ou indisponível"
                result, code = {"ok": False, "error": message}, 400
            except (TypeError, KeyError):
                result, code = {"ok": False, "error": "Pedido inválido, expirado ou indisponível"}, 400
            except Exception:  # noqa: BLE001 -- HTTP boundary returns explicit failure without private data
                result, code = {"ok": False, "error": "Falha local; operação não autorizada"}, 503
            payload = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=service.worker, daemon=True, name="voice-inference-ambient").start()
    threading.Thread(target=service.worker, args=(True,), daemon=True, name="voice-inference-foreground").start()
    print("Local voice helper ready on loopback; audio never sent to a recognition API.", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run()

