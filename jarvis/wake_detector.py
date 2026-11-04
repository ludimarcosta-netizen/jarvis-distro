"""
Detector de Wake Word ("Hey Jarvis") ultraleve via openWakeWord.
Consome menos de 1% de CPU em segundo plano.
"""
import time
import queue
import threading
import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly
from math import gcd
from typing import Callable, Optional
from openwakeword.model import Model

class WakeWordDetector:
    def __init__(
        self,
        wake_word: str = "hey_jarvis",
        threshold: float = 0.5,
        on_detected: Optional[Callable[[], None]] = None
    ):
        self.wake_word = wake_word
        self.threshold = threshold
        self.on_detected = on_detected
        self.is_running = False
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._startup_error = None

        print(f"[*] Carregando modelo neural ONNX de Wake Word: '{wake_word}'...")
        self.oww_model = Model(wakeword_models=[wake_word], inference_framework="onnx")
        print("[+] openWakeWord pronto.")

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status):
        # Converte para int16 (formato esperado pelo openWakeWord)
        audio_int16 = (indata[:, 0] * 32767).astype(np.int16)
        self._queue.put(audio_int16)

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._queue = queue.Queue()
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            self.is_running = False
            raise TimeoutError("Sentinela não iniciou")
        if self._startup_error:
            self.is_running = False
            raise RuntimeError(f"Sentinela falhou: {self._startup_error}") from self._startup_error

    def _run(self):
        try:
            self._listen_loop()
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
            print(f"[!] Sentinela interrompido: {exc}")
        finally:
            # Detecção (break), erro ou stop: o stream fechou. Sem isto, um
            # wake ignorado deixava is_running=True e start() virava no-op
            # para sempre; o orquestrador reinicia pelo health check.
            self.is_running = False

    def stop(self):
        self.is_running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _listen_loop(self):
        # Modelo exige frames de 1280 amostras a 16 kHz.
        device_index = sd.default.device[0]
        if device_index is None or device_index < 0:
            raise RuntimeError("Microfone padrão do Windows não encontrado")
        rate = int(sd.query_devices(device_index)["default_samplerate"])
        block = int(rate * 0.08)
        factor = gcd(rate, 16000)

        def callback(indata, frames, time_info, status):
            samples = indata[:, 0].copy()
            frame = resample_poly(samples, 16000 // factor, rate // factor)
            self._queue.put((np.clip(frame, -1, 1) * 32767).astype(np.int16))

        with sd.InputStream(
            device=device_index,
            samplerate=rate,
            channels=1,
            dtype="float32",
            blocksize=block,
            callback=callback,
        ):
            self._ready.set()
            print("[+] Sentinela ativo. Diga 'Hey Jarvis' a qualquer momento.")
            while self.is_running:
                try:
                    frame = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                prediction = self.oww_model.predict(frame)
                score = prediction.get(self.wake_word, 0.0)

                if score >= self.threshold:
                    print(f"\n[!] WAKE WORD DETECTADA: '{self.wake_word}' (Score: {score:.2f})")
                    self.oww_model.reset()
                    if self.on_detected:
                        self.on_detected()
                    # Encerra o sentinela imediatamente: garante que o stream de
                    # áudio seja fechado de forma determinística antes da sessão
                    # Live. O orquestrador reinicia o detector depois da sessão.
                    break

if __name__ == "__main__":
    def on_wake():
        print(">>> JARVIS ACORDOU! <<<")

    detector = WakeWordDetector(on_detected=on_wake)
    detector.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        detector.stop()
