"""Transcrição offline de uma reunião gravada: separa os falantes e transcreve com contexto.

Fluxo: diarização da gravação inteira (subprocesso no Python do voice_identity) -> agrupa a fala
em blocos contínuos (cortados em pausas reais, nunca no meio da frase) -> faster-whisper large-v3
com tempo por palavra -> cada palavra recebe o falante ativo naquele instante -> falas por falante.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import transcriber  # noqa: F401 — expõe as DLLs CUDA do venv antes do faster_whisper
from audio_review import review_utterances, suspicion
from faster_whisper import WhisperModel, decode_audio
from gpu_priority import pace_offline
from transcriber import HOTWORDS

HERE = Path(__file__).resolve().parent
VOICE_PYTHON = HERE.parent / "voice_identity" / ".venv" / "Scripts" / "python.exe"
MODEL = "large-v3"
RATE = 16000
BLOCK_GAP_S = 2.0      # pausa que separa dois blocos de fala
BLOCK_MAX_S = 28.0     # bloco maior que isso é cortado na maior pausa interna
PAD_S = 0.3
MERGE_GAP_S = 1.2      # mesma voz com pausa menor que isso continua a mesma fala
MIN_RUN_S = 1.5        # troca de falante mais curta que isso é ruído da separação, não outra pessoa
_LOOP_RE = re.compile(r"\b(\w+(?:\s+\w+){0,3}?)(?:[\s,.!?]+\1\b){3,}", re.IGNORECASE)


def collapse_loops(text: str) -> str:
    """'e aí e aí e aí e aí' -> 'e aí'. O Whisper repete trechos quando perde o fio."""
    return _LOOP_RE.sub(r"\1", text).strip()


def run_diarization(audio: Path, samples_dir: str = "", num_speakers: int = -1) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "dia.json"
        cmd = [str(VOICE_PYTHON), str(HERE / "offline_diarize.py"), str(audio), str(out)]
        cmd.extend([samples_dir, str(num_speakers)])
        subprocess.run(cmd, check=True, capture_output=True)
        return json.loads(out.read_text(encoding="utf-8"))


def speech_blocks(turns: List[Dict[str, Any]], duration: float) -> List[tuple]:
    """Junta os turnos em blocos contínuos de fala; corta bloco longo na maior pausa."""
    spans = sorted((t["start"], t["end"]) for t in turns)
    blocks: List[List[float]] = []
    for start, end in spans:
        if blocks and start - blocks[-1][1] <= BLOCK_GAP_S and end - blocks[-1][0] <= BLOCK_MAX_S:
            blocks[-1][1] = max(blocks[-1][1], end)
        else:
            blocks.append([start, end])
    return [(max(0.0, s - PAD_S), min(duration, e + PAD_S)) for s, e in blocks]


def speaker_at(turns: List[Dict[str, Any]], start: float, end: float) -> int:
    """Falante com mais fala no intervalo; sem sobreposição, o turno mais próximo."""
    best, best_overlap = None, 0.0
    for turn in turns:
        overlap = min(end, turn["end"]) - max(start, turn["start"])
        if overlap > best_overlap:
            best, best_overlap = turn["speaker"], overlap
    if best is not None:
        return best
    return -1  # sem voz detectada: preserva a fala como desconhecida


def transcribe_blocks(model, audio, blocks) -> List[Dict[str, Any]]:
    words = []
    for start, end in blocks:
        clip = audio[int(start * RATE):int(end * RATE)]
        segments, _ = model.transcribe(
            clip, language="pt", beam_size=5, word_timestamps=True, condition_on_previous_text=False,
            temperature=[0, 0.2, 0.4], compression_ratio_threshold=2.0, no_speech_threshold=0.5,
            vad_filter=True, vad_parameters=dict(min_silence_duration_ms=300, speech_pad_ms=200))
        for seg in segments:
            if seg.avg_logprob < -1.0 or seg.compression_ratio > 2.4:
                continue
            for w in seg.words or []:
                words.append({"start": start + w.start, "end": start + w.end, "word": w.word})
    return words


def smooth_speakers(words: List[Dict[str, Any]]) -> None:
    """Absorve trocas de falante curtas demais (palavra solta de outra voz) no vizinho mais longo."""
    while True:
        runs: List[List[int]] = []  # [primeiro, último] índices de palavras com o mesmo falante
        for i, w in enumerate(words):
            if runs and words[runs[-1][0]]["speaker"] == w["speaker"]:
                runs[-1][1] = i
            else:
                runs.append([i, i])
        short = [k for k, (a, b) in enumerate(runs)
                 if len(runs) > 1 and words[a]["speaker"] != -1
                 and words[b]["end"] - words[a]["start"] < MIN_RUN_S]
        if not short:
            return
        k = min(short, key=lambda j: words[runs[j][1]]["end"] - words[runs[j][0]]["start"])
        neighbours = [runs[j] for j in (k - 1, k + 1)
                      if 0 <= j < len(runs) and words[runs[j][0]]["speaker"] != -1]
        if not neighbours:
            return
        target = max(neighbours, key=lambda r: words[r[1]]["end"] - words[r[0]]["start"])
        for i in range(runs[k][0], runs[k][1] + 1):
            words[i]["speaker"] = words[target[0]]["speaker"]


def build_utterances(words: List[Dict[str, Any]], turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    utterances: List[Dict[str, Any]] = []
    for w in words:
        w["speaker"] = speaker_at(turns, w["start"], w["end"])
    # Uma resposta curta também pode ser de outra pessoa; preserve os turnos detectados.
    for w in words:
        speaker = w["speaker"]
        last = utterances[-1] if utterances else None
        if (last and last["speaker"] == speaker and w["start"] - last["end"] <= MERGE_GAP_S
                and last.get("segment") == w.get("segment") and w["end"] - last["start"] <= 28):
            last["text"] += w["word"]
            last["end"] = w["end"]
        else:
            utterances.append({"speaker": speaker, "start": w["start"], "end": w["end"], "text": w["word"],
                               "segment": w.get("segment"), "review_reasons": list(w.get("review_reasons", []))})
    for u in utterances:
        u["text"] = u["text"].strip()  # repetições ficam disponíveis para a segunda escuta
    return [u for u in utterances if u["text"]]


def fold_minor_speakers(utterances: List[Dict[str, Any]], known: set) -> None:
    """Fala de voz sem perfil próprio (pouca fala, sem embedding) passa para o falante vizinho."""
    for i, u in enumerate(utterances):
        if u["speaker"] in known:
            continue
        before = next((utterances[j]["speaker"] for j in range(i - 1, -1, -1) if utterances[j]["speaker"] in known), None)
        after = next((x["speaker"] for x in utterances[i + 1:] if x["speaker"] in known), None)
        u["speaker"] = before if before is not None else after if after is not None else u["speaker"]


def transcribe_meeting(audio_path: Path, samples_dir: str = "", num_speakers: int = -1) -> Dict[str, Any]:
    audio = decode_audio(str(audio_path), sampling_rate=RATE)
    # Cada janela (inclusive a segunda escuta, que usa o mesmo modelo) espera a fala ao vivo passar.
    model = pace_offline(WhisperModel(MODEL, device="cuda", compute_type="int8_float16", local_files_only=True))
    # A identificação de vozes não decide qual áudio chega ao transcritor.
    segments, _ = model.transcribe(
        audio, language="pt", beam_size=5, word_timestamps=True, hotwords=HOTWORDS,
        condition_on_previous_text=False, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 400},
        temperature=[0, 0.2, 0.4], compression_ratio_threshold=2.4, chunk_length=28)
    words = []
    for index, segment in enumerate(segments):
        reasons = suspicion(segment)
        for word in segment.words or []:
            words.append({"start": word.start, "end": word.end, "word": word.word,
                          "segment": index, "review_reasons": reasons})
    try:
        diarization = run_diarization(audio_path, samples_dir, num_speakers)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError):
        print("[!] Identificação de vozes indisponível; preservando falas sem identificação.", file=sys.stderr)
        diarization = {"turns": [], "speakers": {}}
    utterances = build_utterances(words, diarization["turns"])
    utterances.extend(missing_speech(diarization["turns"], words))
    utterances.sort(key=lambda u: u["start"])
    if samples_dir:
        review_utterances(model, audio, utterances, Path(samples_dir), HOTWORDS)
    speakers = diarization["speakers"]
    for utterance in utterances:
        key = str(utterance["speaker"])
        if key not in speakers:
            speakers[key] = {"seconds": 0, "embedding": [], "suggestion": None, "sample": None}
            speakers[key]["seconds"] = sum(u["end"] - u["start"] for u in utterances
                                           if str(u["speaker"]) == key)
    return {"utterances": utterances, "speakers": speakers, "duration": len(audio) / RATE}


def missing_speech(turns: list[dict[str, Any]], words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detecção de voz sem palavras: oferece áudio e segunda escuta, sem inventar texto."""
    missing = []
    spans = sorted((w["start"], w["end"]) for w in words)
    # Une turnos sobrepostos para não revisar o mesmo silêncio duas vezes.
    turns_spans: list[list[float]] = []
    for turn in sorted(turns, key=lambda t: t["start"]):
        if turns_spans and turn["start"] <= turns_spans[-1][1]:
            turns_spans[-1][1] = max(turns_spans[-1][1], turn["end"])
        else:
            turns_spans.append([turn["start"], turn["end"]])
    for start, end in turns_spans:
        cursor = start
        gaps = []
        for a, b in spans:
            if b <= cursor:
                continue
            if a >= end:
                break
            if a - cursor >= 1.5:
                gaps.append((cursor, min(a, end)))
            cursor = max(cursor, b)
        if end - cursor >= 1.5:
            gaps.append((cursor, end))
        for a, b in gaps:
            while b - a >= 1.5:
                stop = min(b, a + 28)
                missing.append({"speaker": speaker_at(turns, a, stop), "start": a, "end": stop,
                                "text": "", "review_reasons": ["voz detectada sem transcrição"]})
                a = stop
    return missing


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--json":  # --json <audio> <saida.json> [pasta_de_amostras]
        data = transcribe_meeting(Path(sys.argv[2]), sys.argv[4] if len(sys.argv) > 4 else "",
                                  int(sys.argv[5]) if len(sys.argv) > 5 else -1)
        Path(sys.argv[3]).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        sys.exit(0)
    result = transcribe_meeting(Path(sys.argv[1]))
    for u in result["utterances"]:
        print(f"[{int(u['start'] // 60):02d}:{u['start'] % 60:04.1f}] Voz {u['speaker']}: {u['text']}")
    print({k: v["seconds"] for k, v in result["speakers"].items()})
