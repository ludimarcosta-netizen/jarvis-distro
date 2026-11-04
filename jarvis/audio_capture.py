"""
Módulo de captura de áudio com VAD (Voice Activity Detection) e buffer pré-rolagem.
Trabalha a 16kHz mono nativo para o Whisper.
"""
import time
import queue
import threading
import numpy as np
import pyaudiowpatch as pyaudio
import sounddevice as sd
from openwakeword.vad import VAD
from scipy.signal import resample_poly
from math import gcd
from typing import Callable, Optional

class AudioCaptureService:
    def __init__(
        self,
        sample_rate: int = 16000,
        chunk_duration_ms: int = 30, # 30ms por frame
        vad_probability_threshold: float = 0.5,
        silence_timeout_sec: float = 1.2, # tempo de silêncio para fechar a sentença
        max_sentence_duration_sec: float = 25.0, # duração máxima contínua antes de quebrar
        pre_roll_sec: float = 0.5, # buffer que guarda o áudio logo antes de começar a falar
        on_speech_chunk: Optional[Callable[[np.ndarray, str, float], None]] = None,
        source: str = "mic",
        on_frame: Optional[Callable[[np.ndarray], None]] = None, # todo quadro, com ou sem fala (gravação contínua)
    ):
        self.sample_rate = sample_rate
        self._mic_rate = sample_rate  # cache da taxa do mic; nunca consultar PortAudio dentro do callback
        self.chunk_size = int(sample_rate * (chunk_duration_ms / 1000.0))
        self.vad_threshold = vad_probability_threshold
        self.silence_timeout = silence_timeout_sec
        self.max_duration = max_sentence_duration_sec
        self.pre_roll_chunks = int(pre_roll_sec * 1000 / chunk_duration_ms)
        self.on_speech_chunk = on_speech_chunk
        self.source = source
        self.on_frame = on_frame
        
        self.is_running = False
        self._thread: Optional[threading.Thread] = None
        self._pre_buffer = []
        self._ready = threading.Event()
        self._startup_error = None
        self._last_frame_at = 0.0

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status):
        if status:
            pass # Ignora eventuais xruns sem travar o áudio
        # Extrai canal mono em float32
        audio_frame = indata[:, 0].copy()
        self._audio_queue.put(audio_frame)

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._audio_queue = queue.Queue()
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            self.is_running = False
            raise TimeoutError(f"Captura {self.source} não iniciou")
        if self._startup_error:
            self.is_running = False
            raise RuntimeError(f"Captura {self.source} falhou: {self._startup_error}") from self._startup_error

    def _run(self):
        while self.is_running:
            try:
                self._process_loop()
            except Exception as exc:
                if not self._ready.is_set():
                    self._startup_error = exc
                    self.is_running = False
                    self._ready.set()
                    return
                print(f"[!] Captura {self.source} interrompida: {exc}; reconectando.", flush=True)
                self._pre_buffer.clear()
                while not self._audio_queue.empty():
                    try:
                        self._audio_queue.get_nowait()
                    except queue.Empty:
                        break
                time.sleep(1)

    def stop(self):
        self.is_running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _process_loop(self):
        self._capture_microphone()

    def _capture_microphone(self):
        """Capture the Windows default microphone through sounddevice/PortAudio."""
        device_index = sd.default.device[0]
        if device_index is None or device_index < 0:
            raise RuntimeError("Microfone padrão do Windows não encontrado")
        device = sd.query_devices(device_index)
        rate = int(device["default_samplerate"])
        block = max(1, int(rate * self.chunk_size / self.sample_rate))
        channels = 1
        self._mic_rate = rate  # cache usado pelo callback (evita query_devices por frame)

        with sd.InputStream(
            device=device_index,
            samplerate=rate,
            channels=channels,
            dtype="float32",
            blocksize=block,
            callback=self._microphone_callback,
        ):
            self._last_frame_at = time.monotonic()
            self._ready.set()
            self._segment_loop()

    def _microphone_callback(self, indata, frames, time_info, status):
        if not self.is_running:
            return
        self._last_frame_at = time.monotonic()
        samples = indata[:, 0].copy()
        rate = self._mic_rate
        if rate != self.sample_rate:
            factor = gcd(rate, self.sample_rate)
            samples = resample_poly(
                samples, self.sample_rate // factor, rate // factor
            ).astype(np.float32)
        self._audio_queue.put(samples)

    def _segment_loop(self):
            vad = VAD()
            active_speech = []
            is_speaking = False
            silence_start_time = None
            speech_start_time = None

            while self.is_running:
                try:
                    frame = self._audio_queue.get(timeout=0.2)
                except queue.Empty:
                    # A healthy microphone delivers frames even during silence.
                    # WASAPI loopback may legitimately stop delivering while idle.
                    if self.source == "mic" and time.monotonic() - self._last_frame_at > 5:
                        raise RuntimeError("Microfone deixou de entregar áudio")
                    continue

                if self.on_frame:
                    self.on_frame(frame)
                probability = vad.predict((np.clip(frame, -1, 1) * 32767).astype(np.int16))
                now = time.time()

                if probability >= self.vad_threshold:
                    if not is_speaking:
                        # Começou a falar: resgata pre-roll para não perder o início da sílaba
                        is_speaking = True
                        speech_start_time = now
                        active_speech = list(self._pre_buffer)
                    active_speech.append(frame)
                    silence_start_time = None
                else:
                    if is_speaking:
                        active_speech.append(frame)
                        if silence_start_time is None:
                            silence_start_time = now
                        elif (now - silence_start_time) >= self.silence_timeout:
                            # Terminou de falar: empacota e envia
                            self._emit_chunk(active_speech, now - speech_start_time)
                            active_speech = []
                            is_speaking = False
                            silence_start_time = None
                    else:
                        # Mantém anel de pre-roll enquanto silêncio
                        self._pre_buffer.append(frame)
                        if len(self._pre_buffer) > self.pre_roll_chunks:
                            self._pre_buffer.pop(0)

                # Força quebra de fala muito longa para transcrever sem atraso excessivo
                if is_speaking and speech_start_time and (now - speech_start_time) >= self.max_duration:
                    self._emit_chunk(active_speech, now - speech_start_time)
                    active_speech = []
                    is_speaking = False
                    silence_start_time = None

            if active_speech and speech_start_time:
                self._emit_chunk(active_speech, time.time() - speech_start_time)

    def _emit_chunk(self, frames, duration):
        if not frames:
            return
        audio_data = np.concatenate(frames)
        # Filtra se for muito curto (menos de 0.4s)
        if len(audio_data) < int(self.sample_rate * 0.4):
            return
        if self.on_speech_chunk:
            self.on_speech_chunk(audio_data, self.source, duration)

    def _capture_wasapi(self, loopback: bool):
        """Captura WASAPI (loopback da saída padrão) em modo callback.

        Importante: a thread de captura pertence ao PortAudio e o encerramento
        (stop_stream/close/terminate) acontece somente na thread dona do stream.
        A versão anterior usava uma thread Python de leitura bloqueante
        (``stream.read``) que, quando o endpoint loopback não entregava pacotes,
        ficava presa dentro da chamada; o ``join`` com timeout expirava e o
        stream era fechado/terminado sob a leitura em andamento, causando
        access violation dentro do AUDIOSES.DLL e a morte súbita do processo.
        """
        if not loopback:
            return self._capture_microphone()
        pa = pyaudio.PyAudio()
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            index = pa.get_default_wasapi_loopback()["index"] if loopback else wasapi["defaultInputDevice"]
            if index < 0:
                raise RuntimeError("Dispositivo WASAPI padrão não encontrado")
            device = pa.get_device_info_by_index(index)
            rate = int(device["defaultSampleRate"])
            channels = min(2, int(device["maxInputChannels"])) if loopback else 1
            block = max(1, int(rate * self.chunk_size / self.sample_rate))
            factor = gcd(rate, self.sample_rate)

            def loopback_callback(in_data, frame_count, time_info, status):
                # Nunca chamar PortAudio/PyAudio aqui dentro (regra do modo callback).
                if not self.is_running:
                    return (None, pyaudio.paComplete)
                samples = np.frombuffer(in_data, dtype=np.float32).reshape(-1, channels).mean(axis=1)
                resampled = resample_poly(samples, self.sample_rate // factor,
                                          rate // factor).astype(np.float32)
                self._audio_queue.put(resampled)
                return (None, pyaudio.paContinue)

            # Com stream_callback o PortAudio inicia o stream automaticamente.
            stream = pa.open(format=pyaudio.paFloat32, channels=channels, rate=rate,
                             input=True, input_device_index=index, frames_per_buffer=block,
                             stream_callback=loopback_callback)
            self._ready.set()
            try:
                self._segment_loop()
            finally:
                # Encerramento exclusivo da thread dona: seguro por construção.
                try:
                    stream.stop_stream()
                except Exception:
                    pass  # stream pode já ter sido concluído via paComplete
                stream.close()
        finally:
            pa.terminate()


class LoopbackCaptureService(AudioCaptureService):
    """Capture the default Windows speaker mix through WASAPI loopback."""

    def __init__(self, **kwargs):
        super().__init__(source="loopback", **kwargs)

    def _process_loop(self):
        self._capture_wasapi(loopback=True)

if __name__ == "__main__":
    def print_chunk(audio, source, dur):
        print(f"[!] Bloco capturado de {source}: {dur:.1f}s | amostras: {len(audio)}")

    print("[*] Iniciando teste de captura. Fale algo no microfone...")
    service = AudioCaptureService(on_speech_chunk=print_chunk)
    service.start()
    try:
        time.sleep(6)
    finally:
        service.stop()
        print("[+] Teste finalizado.")
