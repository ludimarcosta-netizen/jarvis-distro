"""Segunda escuta local: preserva o original e nunca aceita correções sozinha."""
import re
import wave
from pathlib import Path
from typing import Any

import numpy as np
from transcript_hygiene import hallucination_reason

RATE = 16000
CONTEXT_S = 5.0
MAX_REVIEW_S = 40.0


def suspicion(segment: Any) -> list[str]:
    reasons = []
    if segment.avg_logprob < -0.7:
        reasons.append("baixa confiança")
    if segment.compression_ratio > 2.4:
        reasons.append("texto repetitivo")
    if segment.no_speech_prob > 0.6:
        reasons.append("fala incerta")
    if hallucination_reason(segment.text):
        reasons.append("possível alucinação")
    if any(w.probability < 0.35 for w in segment.words or []):
        reasons.append("palavra incerta")
    return reasons


def normalized(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def write_clip(audio: np.ndarray, path: Path) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm)


def review_utterances(model: Any, audio: np.ndarray, utterances: list[dict],
                      samples_dir: Path, hotwords: str) -> None:
    """Uma segunda escuta por janela de áudio, compartilhada por falas próximas."""
    duration = len(audio) / RATE
    samples_dir.mkdir(parents=True, exist_ok=True)
    groups = []
    for index, utterance in enumerate(utterances):
        reasons = list(utterance.pop("review_reasons", []))
        if not reasons:
            continue
        start, end = utterance["start"], utterance["end"]
        if end - start > MAX_REVIEW_S - 2 * CONTEXT_S:
            raise ValueError("Intervalo de revisão maior que a janela permitida")
        groups.append((index, utterance, reasons))
    windows = []
    for item in sorted(groups, key=lambda item: item[1]["start"]):
        start, end = item[1]["start"], item[1]["end"]
        if (windows and start <= windows[-1]["end"] + CONTEXT_S
                and max(end, windows[-1]["end"]) - windows[-1]["start"] <= MAX_REVIEW_S - 2 * CONTEXT_S):
            windows[-1]["items"].append(item)
            windows[-1]["end"] = max(end, windows[-1]["end"])
        else:
            windows.append({"start": start, "end": end, "items": [item]})
    for window in windows:
        start, end = window["start"], window["end"]
        clip_start, clip_end = max(0.0, start - CONTEXT_S), min(duration, end + CONTEXT_S)
        clip = audio[int(clip_start * RATE):int(clip_end * RATE)]
        sample = f"review_{window['items'][0][0]}.wav"
        write_clip(clip, samples_dir / sample)
        words, error = [], None
        try:
            segments, _ = model.transcribe(
                clip, language="pt", beam_size=8, word_timestamps=True,
                condition_on_previous_text=False, temperature=0, vad_filter=False,
                hotwords=hotwords)
            for segment in segments:
                for word in segment.words or []:
                    middle = clip_start + (word.start + word.end) / 2
                    words.append((middle, word.word))
        except RuntimeError as exc:
            # O backend pode incluir dados da gravação no texto da exceção.
            words, error = [], type(exc).__name__
        for _index, utterance, reasons in window["items"]:
            alternative = "".join(word for middle, word in words
                                  if utterance["start"] <= middle <= utterance["end"]).strip()
            review = {"original_text": utterance["text"], "alternative_text": alternative, "reasons": reasons,
                      "status": "pending", "sample": sample, "clip_start": clip_start,
                      "clip_end": clip_end, "agreement": False, "error": error}
            utterance["review"] = review
            review["agreement"] = bool(alternative) and normalized(alternative) == normalized(utterance["text"])
            if error:
                review["reasons"].append("segunda escuta falhou")
            elif not alternative:
                review["reasons"].append("segunda escuta sem texto")
            elif not review["agreement"]:
                review["reasons"].append("divergência entre as escutas")
