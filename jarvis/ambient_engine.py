"""
Motor de Escuta Ambiente do Jarvis.
Captura microfone (e loopback opcional), roda VAD, transcreve na GPU RTX 2070 e salva no SQLite.
"""
import sys
import time
import queue
import signal
import threading
import base64
import importlib.util
from pathlib import Path
from datetime import datetime
from typing import Optional

import gpu_priority
from audio_archive import ContinuousRecorder
from audio_capture import AudioCaptureService, LoopbackCaptureService
from meeting_gate import MeetingAudioGate
from transcriber import DEFAULT_MODEL, LocalTranscriber
from storage import ignore_transcripts, init_db, save_transcript
from transcript_hygiene import EchoGuard

_voice_path = Path(__file__).resolve().parents[1] / "voice_identity" / "client.py"
if not _voice_path.exists():
    _voice_path = Path.home() / "AppData/Local/hermes/voice_identity/client.py"
_voice_spec = importlib.util.spec_from_file_location("ambient_voice_client", _voice_path)
_voice_module = importlib.util.module_from_spec(_voice_spec)
_voice_spec.loader.exec_module(_voice_module)


def note_meeting_participant(text: str):
    return _voice_module.client.meeting_hint(text)

class JarvisAmbientEngine:
    # Sinal de GPU ocupada para a passada offline (outro processo); o lock impede que o
    # "fila vazia" do worker sobrescreva o "ocupado" de um trecho que acabou de chegar.
    _gpu_signal_lock = threading.Lock()

    def __init__(self, model_size: str = DEFAULT_MODEL, language: str = "pt", sensitivity: float = 0.5):
        self.language = language
        self.active_session_id = "ambient"
        self.is_running = False
        
        init_db()
        print(f"[*] Carregando transcritor neural na GPU (modelo: {model_size})...")
        self.transcriber = LocalTranscriber(model_size=model_size, device="cuda", compute_type="int8_float16")
        
        self.audio_queue = queue.Queue(maxsize=20)
        # Pausa de 1,5 s fecha o trecho (1,1 s picotava as frases: 19% dos trechos tinham
        # até 3 palavras) e o trecho vai até 30 s antes de ser cortado.
        # Gravação contínua (2 h) para recuperar o que a transcrição perdeu.
        self.recorders = [ContinuousRecorder("mic"), ContinuousRecorder("loopback")]
        self.capture_service = AudioCaptureService(
            vad_probability_threshold=sensitivity,
            silence_timeout_sec=1.5,
            max_sentence_duration_sec=30.0,
            on_speech_chunk=self._on_audio_received,
            on_frame=self.recorders[0].write,
        )
        self.loopback_service = LoopbackCaptureService(
            silence_timeout_sec=1.5,
            max_sentence_duration_sec=30.0,
            on_speech_chunk=self._on_audio_received,
            on_frame=self.recorders[1].write,
        )
        # Som do PC (vídeo, chamada) chega pelo sistema e, pela caixa de som, pelo microfone.
        self.echo_guard = EchoGuard()
        # Só o som de aplicativo de reunião (Zoom, Teams, Meet…) vira reunião; YouTube e WhatsApp não.
        self.meeting_gate = MeetingAudioGate()
        self.worker_thread: Optional[threading.Thread] = None
        self._worker_stop = threading.Event()
        # Modo de destino dos trechos transcritos:
        #   "save"    -> grava no SQLite (memória de reuniões) [padrão]
        #   "discard" -> joga fora (compat com wake antigo)
        #   "capture" -> entrega via on_captured_chunk sem gravar
        #                (captura do comando dito após "Hey Jarvis")
        self.capture_mode = "save"
        self.on_captured_chunk: Optional[object] = None
        self.on_meeting_command: Optional[object] = None  # (kind, name) — Fase 3
        self._discard_pending = False

    def _on_audio_received(self, audio_data, source: str, duration: float):
        try:
            in_meeting = source != "loopback" or self.meeting_gate.active()
            with self._gpu_signal_lock:
                self.audio_queue.put_nowait((audio_data, source, duration, self.active_session_id, in_meeting))
                gpu_priority.mark_busy()
        except queue.Full:
            print("[!] Fila de transcrição cheia; trecho de áudio descartado.", file=sys.stderr)

    def _update_gpu_signal(self):
        with self._gpu_signal_lock:
            if self.audio_queue.empty():
                gpu_priority.mark_idle()
            else:
                gpu_priority.mark_busy()

    def _transcription_worker(self, stop: threading.Event):
        # Evento de parada por geração: um start() posterior não "ressuscita"
        # o worker antigo (antes, dois workers disputavam a GPU).
        while not stop.is_set() or not self.audio_queue.empty():
            try:
                audio_data, source, duration, session_id, in_meeting = self.audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                if self._discard_pending or self.capture_mode == "discard":
                    # Modo descarte: joga fora o trecho em fila (a própria
                    # fala "Hey Jarvis" transcrita como "Beijarps!").
                    continue
                text = self.transcriber.transcribe_audio_chunk(audio_data, language=self.language)
                if self._discard_pending:
                    continue  # stop(discard_pending=True) chegou durante a transcrição
                if self.capture_mode == "capture":
                    # Captura de comando pós-wake: entrega ao orquestrador,
                    # sem contaminar a memória de reuniões. O PCM original
                    # acompanha a transcrição para a biometria local.
                    if text and callable(self.on_captured_chunk):
                        self.on_captured_chunk(text, audio_data, source, duration)
                    continue
                if source == "mic" and text and self.echo_guard.mic_is_echo(text):
                    print(f"[Eco do PC descartado] {duration:.1f}s", file=sys.stderr)
                    continue  # nem biometria: a voz é do vídeo/chamada, não de alguém da sala
                if source == "loopback" and not in_meeting:
                    # Som do PC fora de aplicativo de reunião (vídeo, WhatsApp): fica fora da memória e das atas,
                    # mas ainda protege contra o eco dele no microfone quando toca na caixa de som.
                    if text:
                        ignore_transcripts(self.echo_guard.system_heard(text), "eco do áudio do PC")
                    continue
                if source in {"mic", "loopback"} and _voice_module.client.mode() != "off":
                    # Both local and remote meeting speech feed identity. Only local
                    # mic text may provide a name hint for the current voice.
                    pcm = (audio_data.clip(-1, 1) * 32767).astype("<i2").tobytes()
                    payload = {"data": base64.b64encode(pcm).decode("ascii"), "source": "ambient"}
                    if source == "mic":
                        payload["text"] = text
                    _voice_module.client.submit(payload)
                    if source == "mic":
                        note_meeting_participant(text or "")
                if not text:
                    continue
                now_str = datetime.now().strftime("%H:%M:%S")
                transcript_id = save_transcript(text=text, source=source, duration=duration, session_id=session_id)
                if source == "mic":
                    self.echo_guard.remember_mic(transcript_id, text)
                else:
                    # A cópia do microfone pode ter chegado antes da do sistema.
                    ignore_transcripts(self.echo_guard.system_heard(text), "eco do áudio do PC")
                print(f"[Memória {now_str}] {source}: {duration:.1f}s registrados")
                # Fase 3: comandos falados de reunião ("Jarvis, iniciando
                # reunião com X" / "encerrar reunião") — detectados só no modo
                # save (a fala já foi gravada como parte da reunião).
                if source == "mic" and callable(self.on_meeting_command):
                    # Só o microfone da sala: um participante remoto (loopback)
                    # não pode abrir/encerrar reunião.
                    try:
                        from meeting_tracker import detect_meeting_command
                        cmd = detect_meeting_command(text)
                        if cmd:
                            self.on_meeting_command(cmd["kind"], cmd["name"])
                    except Exception as e:
                        print(f"[!] Detecção de comando de reunião falhou: {e}", file=sys.stderr)
            except Exception as e:
                print(f"[!] Erro durante transcrição: {e}", file=sys.stderr)
            finally:
                self.audio_queue.task_done()
                self._update_gpu_signal()

    def start(self):
        if self.is_running:
            return
        if self.worker_thread and self.worker_thread.is_alive():
            # Worker da geração anterior ainda drenando (transcrição lenta).
            self.worker_thread.join(timeout=20.0)
        self.is_running = True
        self._discard_pending = False
        print("[+] Iniciando motor de escuta e transcrição...")
        self._worker_stop = threading.Event()
        self.worker_thread = threading.Thread(target=self._transcription_worker,
                                              args=(self._worker_stop,), daemon=True)
        self.worker_thread.start()
        for recorder in self.recorders:
            recorder.start()
        try:
            self.meeting_gate.start()
            self.capture_service.start()
            self.loopback_service.start()
        except Exception:
            self.is_running = False
            self._worker_stop.set()
            self.capture_service.stop()
            self.loopback_service.stop()
            for recorder in self.recorders:
                recorder.stop()
            if self.worker_thread:
                self.worker_thread.join(timeout=2.0)
            raise
        print("[+] Jarvis Ambient Engine ativo. Fale algo no ambiente para testar...")

    def stop(self, discard_pending: bool = False):
        print("\n[*] Encerrando o motor de escuta...")
        self._discard_pending = discard_pending
        self.capture_service.stop()
        self.loopback_service.stop()
        self.meeting_gate.stop()
        for recorder in self.recorders:
            recorder.stop()
        self.is_running = False
        self._worker_stop.set()
        if self.worker_thread:
            self.worker_thread.join(timeout=2.0)
        print("[+] Motor de escuta encerrado.")

    def set_session(self, session_name: str):
        self.active_session_id = session_name
        print(f"[*] Sessão/Reunião ativa alterada para: '{session_name}'")

if __name__ == "__main__":
    engine = JarvisAmbientEngine(model_size="small", language="pt")
    engine.start()

    def signal_handler(sig, frame):
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        engine.stop()
