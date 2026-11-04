"""A single brain: local STT -> existing guarded Hermes turn -> local F5 speech."""
from __future__ import annotations

import asyncio
import base64
import difflib
import re
import time
import uuid
import wave

from local_voice import Segmenter, decode_pcm, settings, speech_parts, stt, tts
from local_voice_phrases import Bank, normalized


class Session:
    def __init__(self, bridge):
        self.bridge = bridge
        self.segmenter = Segmenter()
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=3)
        self.task = None
        self.seed_task = None
        self.generation = 0
        self.closed = False
        self.last_spoken = ""
        self.playback = False
        self.segment_playback = False
        self.busy = 0
        self.phrases = Bank()
        self.last_ack_at = float("-inf")
        self.last_ack_text = ""
        self.preview_text = ""
        self.preview_task = None
        self.preview_epoch = 0
        self.preview_at = 0.0
        self.speech_cues = []
        self.speech_lock = asyncio.Lock()
        self.acknowledgements_enabled = settings().get("acknowledgements", False) is True

    def activity(self, change: int):
        self.busy = max(0, self.busy + change)
        if not self.closed:
            self.bridge._state["interaction"] = "IN_PROGRESS" if self.busy else ""

    async def prepare(self):
        try:
            await asyncio.to_thread(self.phrases.load)
        except (OSError, ValueError, KeyError, TypeError, wave.Error) as exc:
            self.bridge._live_log(f"frases indisponíveis: {type(exc).__name__}; voz dinâmica mantida")
        # Load sequentially to avoid simultaneous peak RAM allocations.
        await asyncio.to_thread(stt.call, {"op": "prepare"})
        await asyncio.to_thread(tts.call, {"op": "prepare"})

    def start(self):
        self.task = asyncio.create_task(self.consume())
        self.seed_task = asyncio.create_task(self.seed_wake())

    async def close(self):
        self.closed = True
        self.bridge._state["interaction"] = ""
        self.interrupt()
        for task in (self.task, self.seed_task, self.preview_task):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.task, self.seed_task, self.preview_task) if t), return_exceptions=True)
        self.segmenter.reset()

    def interrupt(self):
        self.generation += 1
        self.bridge._audio_clear()
        self.bridge._state["interrupt_seq"] = int(self.bridge._state.get("interrupt_seq") or 0) + 1

    def feed(self, data: str, playback: bool):
        pcm = decode_pcm(data)
        self.playback = playback
        segment = self.segmenter.feed(pcm, playback)
        if self.segmenter.frames or segment is not None:
            self.segment_playback = self.segment_playback or playback
        if segment is not None:
            self.preview_epoch += 1
            self.preview_text = ""
            if self.queue.full():
                self.bridge._state["error"] = "Áudio na fila; aguarde antes de repetir o pedido."
            else:
                self.queue.put_nowait((segment, self.segment_playback))
            self.segment_playback = False
        elif (not playback and not self.segment_playback and self.segmenter.voiced >= 19200
                and time.monotonic() - self.preview_at >= 1.2
                and (self.preview_task is None or self.preview_task.done())):
            self.preview_at = time.monotonic()
            self.preview_task = asyncio.create_task(self.preview(
                b"".join(self.segmenter.frames), self.preview_epoch))

    async def preview(self, pcm: bytes, epoch: int):
        """Display-only hypothesis; only endpoint STT can submit a request."""
        try:
            result = await asyncio.to_thread(stt.call, {"pcm": base64.b64encode(pcm).decode("ascii")})
            if (not self.closed and epoch == self.preview_epoch
                    and not self.bridge._listen_only() and not self.bridge._capture_paused):
                self.preview_text = result["text"].strip()
        except (OSError, ValueError, RuntimeError) as exc:
            self.bridge._live_log(f"local STT preview: {type(exc).__name__}")

    async def consume(self):
        while not self.closed:
            pcm, playback = await self.queue.get()
            try:
                self.activity(1)
                started = time.perf_counter()
                result = await asyncio.to_thread(stt.call, {"pcm": base64.b64encode(pcm).decode("ascii")})
                self.bridge._live_log(f"voice timing: STT {time.perf_counter() - started:.3f}s")
                text = result["text"].strip()
                plain, spoken = normalized(text), normalized(self.last_spoken)
                ack_echo = time.monotonic() - self.last_ack_at < 10 and plain == normalized(self.last_ack_text)
                if not plain or (playback and spoken and (
                        ack_echo or plain in spoken or difflib.SequenceMatcher(None, plain, spoken).ratio() > .75)):
                    continue
                await self.submit(text)
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                self.bridge._live_log(f"local STT: {type(exc).__name__}")
                self.bridge._state["error"] = "Falha na transcrição local. Consulte local-stt.log."
            finally:
                self.activity(-1)
                self.queue.task_done()

    async def submit(self, text: str):
        if self.closed or self.bridge._listen_only() or self.bridge._capture_paused:
            return
        self.interrupt()
        self.bridge._state["input"].extend([text, "—"])
        core = normalized(text)
        core = re.sub(r"^(?:ei |hey )?jarvis ", "", core)
        if core in {"pare", "parar", "silencio", "pare de falar", "fique quieto"}:
            return
        if core in {"cancele", "cancelar", "cancele a tarefa", "cancela a tarefa"}:
            await self.bridge._interrupt_delegation()
            return
        # Only acknowledge receipt. The guarded Hermes turn still decides permissions/results.
        if (self.acknowledgements_enabled
                and core not in {"oi", "ola", "bom dia", "boa tarde", "boa noite", "obrigado", "obrigada", "tchau"}
                and time.monotonic() - self.last_ack_at >= 6):
            clip = self.phrases.acknowledgement()
            if clip is not None:
                self.last_ack_at = time.monotonic()
                self.last_ack_text = clip.text
                self.last_spoken = clip.text
                self.bridge._state["output"].append(clip.text)
                self.publish_speech(clip.text, clip.pcm, True)
        # Journal/permissions/queue/result delivery are the same as all existing voice tasks.
        self.activity(1)
        task = asyncio.create_task(self.bridge._run_delegation({
            "id": "local-" + uuid.uuid4().hex, "args": {"request": text}}))
        task.add_done_callback(self.completed)

    def completed(self, task):
        self.activity(-1)
        if not task.cancelled() and task.exception() is not None:
            self.bridge._state["error"] = "Não consegui processar a fala. Confira o histórico de tarefas."
            self.bridge._live_log(f"local turn: {type(task.exception()).__name__}")

    def publish_pcm(self, pcm: bytes):
        for offset in range(0, len(pcm), 4800):
            self.bridge._audio_publish(base64.b64encode(pcm[offset:offset + 4800]).decode("ascii"))

    def publish_speech(self, text: str, pcm: bytes, first: bool):
        self.speech_cues.append({"offset": self.bridge._audio_total(), "text": text,
                                 "duration": len(pcm) / 48000, "first": first})
        self.speech_cues = self.speech_cues[-512:]
        self.publish_pcm(pcm)

    async def speak(self, text: str):
        # The worker serializes individual calls, not complete multi-chunk replies.
        # Capture the epoch before waiting so interruption also invalidates queued speech.
        generation = self.generation
        queued = time.perf_counter()
        async with self.speech_lock:
            if generation != self.generation:
                return
            self.bridge._live_log(f"voice timing: speech_lock wait {time.perf_counter() - queued:.3f}s")
            await self._speak(text)

    async def _speak(self, text: str):
        if self.closed or self.bridge._listen_only() or self.bridge._capture_paused:
            return
        self.bridge._state["output"].append(text)
        self.last_spoken = text
        generation = self.generation
        started = time.perf_counter()
        try:
            self.activity(1)
            clip = self.phrases.match(text)
            if clip is not None:
                self.publish_speech(clip.text, clip.pcm, True)
                return
            parts = speech_parts(text)
            self.last_spoken = " ".join(parts)
            for index, part in enumerate(parts):
                if self.closed or generation != self.generation or self.bridge._listen_only() or self.bridge._capture_paused:
                    break
                result = await asyncio.to_thread(tts.call, {"text": part})
                t = result.get("timing", {})
                self.bridge._live_log(
                    f"voice timing: part {index} chars={len(part)} since_start={time.perf_counter() - started:.3f}s "
                    f"lock_wait={t.get('lock_wait_s')}s round_trip={t.get('round_trip_s')}s synth={t.get('synth_s')}s")
                if self.closed or generation != self.generation or self.bridge._listen_only() or self.bridge._capture_paused:
                    break
                # Preserve existing 24 kHz PCM transport and mouth animation.
                pcm = base64.b64decode(result["pcm"], validate=True)
                self.publish_speech(part, pcm, index == 0)
                if index == 0:
                    self.bridge._live_log(f"voice timing: first TTS {time.perf_counter() - started:.3f}s")
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            self.bridge._live_log(f"local TTS: {type(exc).__name__}")
            self.bridge._state["error"] = "Resposta disponível na tela; a síntese F5 falhou. Consulte local-tts.log."
        finally:
            self.bridge._live_log(f"voice timing: speech generation {time.perf_counter() - started:.3f}s")
            self.activity(-1)

    async def seed_wake(self):
        state = self.bridge._state
        deadline = time.time() + (self.bridge.WAKE_COMMAND_WAIT_S if
                    time.time() - float(state.get("wake_at") or 0) < self.bridge.WAKE_COMMAND_WAIT_S else 0)
        while not self.closed:
            text = str(state.get("wake_text") or "")
            fresh = time.time() - float(state.get("wake_text_at") or 0) < self.bridge.WAKE_TEXT_MAX_AGE_S
            if text and fresh:
                state.update(wake_text="", wake_text_at=0.0, wake_audio_pcm16="", wake_audio_at=0.0)
                await self.submit(text)
                return
            if time.time() >= deadline:
                return
            await asyncio.sleep(.25)
