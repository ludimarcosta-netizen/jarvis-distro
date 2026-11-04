"""Separação de falantes de um áudio inteiro (roda no Python do voice_identity, que tem o sherpa-onnx).

Uso: python offline_diarize.py <entrada.wav|ogg> <saida.json> [pasta_de_amostras]
Saída: {"duration": s, "turns": [{"start","end","speaker"}],
        "speakers": {"0": {"embedding": [...], "seconds": s, "suggestion": {"name","score"}|null, "sample": "speaker_0.wav"}}}
O agrupamento olha a gravação inteira, não janelas de 5 s: é isso que separa bem as vozes.
"""
import hashlib
import json
import os
import sqlite3
import sys
import wave
from contextlib import closing
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np
import sherpa_onnx as sherpa
from voice_matching import calibrate, match_voice, valid_vectors

RATE = 16000
MODELS = Path(__file__).resolve().parents[1] / "voice_identity" / "models"
CLUSTER_THRESHOLD = 0.35  # menor = mais vozes; 0.5 juntava quase todos numa só (medido em 05/10)
SAMPLE_S = 8.0       # trecho que a página toca para o senhor reconhecer a voz
VOICE_DIR = MODELS.parent
# O agrupamento do sherpa fragmenta a mesma pessoa em várias vozes (20 numa reunião de 29 min).
# Só unimos grupos longos com evidência forte; vozes curtas permanecem separadas.
# Os vetores só se distinguem depois de centralizados (sem isso toda voz parece ter similaridade > 0,8).
MERGE_SIMILARITY = 0.4
MERGE_RAW_MIN = 0.9
MIN_WINDOW_S = 3.0
WINDOW_S = 5.0


def load_audio(path: str) -> np.ndarray:
    resampler = av.AudioResampler(format="flt", layout="mono", rate=RATE)
    parts = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                parts.append(out.to_ndarray().reshape(-1))
    return np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)


def build_diarizer(num_speakers: int = -1):
    if num_speakers != -1 and not 1 <= num_speakers <= 20:
        raise ValueError("Quantidade de vozes deve estar entre 1 e 20")
    embedding = sherpa.SpeakerEmbeddingExtractorConfig(
        model=str(MODELS / "speaker-ecapa512-sherpa.onnx"), num_threads=4, provider="cpu")
    config = sherpa.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(MODELS / "segmentation.onnx")), num_threads=4, provider="cpu"),
        embedding=embedding,
        clustering=sherpa.FastClusteringConfig(num_clusters=num_speakers, threshold=CLUSTER_THRESHOLD),
        min_duration_on=0.3, min_duration_off=0.5)
    if not config.validate():
        raise RuntimeError("Modelos de voz ausentes ou inválidos")
    return sherpa.OfflineSpeakerDiarization(config), sherpa.SpeakerEmbeddingExtractor(embedding)


def speaker_embedding(extractor, clip: np.ndarray) -> list:
    stream = extractor.create_stream()
    stream.accept_waveform(sample_rate=RATE, waveform=np.ascontiguousarray(clip))
    stream.input_finished()
    return np.asarray(extractor.compute(stream)).reshape(-1).tolist()


def suggest_names(vectors: dict) -> dict:
    """Compara janelas limpas com cadastros e exemplos confirmados pelo usuário."""
    none = {k: None for k in vectors}
    try:
        sys.path.insert(0, str(VOICE_DIR))
        from store import Store  # noqa: PLC0415 — só existe no venv do voice_identity
        model_id = hashlib.sha256((MODELS / "speaker-ecapa512-sherpa.onnx").read_bytes()).hexdigest()
        profiles = {}
        for profile in Store(VOICE_DIR / "data" / "voices.sqlite3").profiles():
            saved = profile["embeddings"] if profile["model"] == model_id else profile.get("model_embeddings", {}).get(model_id, [])
            if saved:
                profiles.setdefault(profile["name"], []).extend(saved)
        db_path = Path(os.environ.get("JARVIS_TRANSCRIPTS_DB") or Path(__file__).with_name("transcripts.db"))
        confirmed = {}
        if db_path.is_file():
            with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='meeting_voice_references'").fetchone():
                    for name, raw in conn.execute("SELECT name, evidence FROM meeting_voice_references ORDER BY voice_id DESC"):
                        evidence = json.loads(raw)
                        if evidence.get("model") == model_id:
                            current = confirmed.setdefault(name, [])
                            current.extend(evidence["embeddings"][:max(0, 24 - len(current))])
        # Referências confirmadas de reuniões substituem cadastros antigos ruins
        # apenas nesta comparação; o registro de autenticação permanece intacto.
        profiles.update(confirmed)
        if not profiles:
            return none
        calibration = calibrate(profiles)
        return {key: match_voice(value, profiles, calibration)
                if isinstance(value, list) or value.get("model") == model_id else None
                for key, value in vectors.items()}
    except (sqlite3.Error, OSError, ValueError, KeyError):
        print("[!] Comparação de perfis indisponível; vozes permanecem sem nome.", file=sys.stderr)
        return none


def clean_spans(spans, all_spans) -> list:
    """Subtrai fala simultânea e une intervalos da mesma voz antes de extrair vetores."""
    pieces = []
    for span in spans:
        intervals = [(span.start, span.end)]
        for other in all_spans:
            if other.speaker == span.speaker:
                continue
            intervals = [(a, b) for start, end in intervals
                         for a, b in ((start, min(end, other.start)), (max(start, other.end), end))
                         if b > a]
        pieces.extend(intervals)
    merged = []
    for start, end in sorted(pieces):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [SimpleNamespace(start=a + 0.1, end=b - 0.1, speaker=spans[0].speaker)
            for a, b in merged if b - a > 0.4]


def voice_evidence(extractor, samples: np.ndarray, spans, all_spans) -> dict:
    vectors, seconds = [], 0.0
    # Acumula somente fala limpa do mesmo grupo. Turnos curtos e sobras de
    # janelas também contribuem, sem incluir silêncio ou outra voz no intervalo.
    parts, total = [], 0
    limit = int(12 * WINDOW_S * RATE)
    for span in sorted(clean_spans(spans, all_spans), key=lambda s: s.end - s.start, reverse=True):
        start = max(0, int(span.start * RATE))
        stop = min(len(samples), int(span.end * RATE))
        for cursor in range(start, stop, int(WINDOW_S * RATE)):
            clip = samples[cursor:min(stop, cursor + int(WINDOW_S * RATE))][:limit - total]
            if not len(clip):
                break
            if float(np.sqrt(np.mean(clip ** 2))) < 0.003 or float(np.mean(np.abs(clip) >= 0.99)) >= 0.01:
                continue
            parts.append(clip)
            total += len(clip)
        if total >= limit:
            break
    audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    # Entre 9 e 15 s, três janelas independentes continuam possíveis.
    size = min(int(WINDOW_S * RATE), total // 3) if total >= 3 * MIN_WINDOW_S * RATE else int(WINDOW_S * RATE)
    for start in range(0, total, size):
        clip = audio[start:start + size]
        if len(clip) < int(MIN_WINDOW_S * RATE) or len(vectors) >= 12:
            break
        vector = speaker_embedding(extractor, clip)
        if valid_vectors([vector]):
            vectors.append(vector)
            seconds += len(clip) / RATE
    return {"model": hashlib.sha256((MODELS / "speaker-ecapa512-sherpa.onnx").read_bytes()).hexdigest(),
            "embeddings": vectors, "clean_seconds": round(seconds, 2), "windows": len(vectors)}


def write_sample(samples: np.ndarray, spans, speakers_spans, path: Path) -> bool:
    """Até SAMPLE_S s da voz: prefere o maior turno sem outra voz por cima; completa com outros turnos."""
    ordered = sorted(clean_spans(spans, speakers_spans), key=lambda s: s.end - s.start, reverse=True)
    parts, total = [], 0.0
    for span in ordered:
        take = min(span.end - span.start, SAMPLE_S - total)
        if take < 0.4:
            continue
        parts.append(samples[int(span.start * RATE):int((span.start + take) * RATE)])
        total += take
        if total >= SAMPLE_S:
            break
    if not parts:
        return False
    pcm = (np.clip(np.concatenate(parts), -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm)
    return True


def _centered(vectors: dict) -> dict:
    keys = list(vectors)
    matrix = np.array([vectors[k] for k in keys], dtype=np.float64)
    matrix /= np.linalg.norm(matrix, axis=1)[:, None]
    if len(keys) >= 3:  # com 1 ou 2 vozes não há média confiável para centralizar
        matrix -= matrix.mean(axis=0)
        matrix /= np.maximum(np.linalg.norm(matrix, axis=1)[:, None], 1e-9)
    return dict(zip(keys, matrix))


def merge_voices(vectors: dict, seconds: dict, spans=()) -> dict:
    """Une apenas grupos longos e muito semelhantes que nunca falam simultaneamente."""
    if len(vectors) < 3:
        return {k: k for k in vectors}
    unit = _centered(vectors)
    raw = {k: valid_vectors([v])[0] for k, v in vectors.items()}
    sim = lambda a, b: float(np.dot(unit[a], unit[b]))  # noqa: E731
    groups = [[k] for k in vectors]
    while True:
        best = None
        for i, j in combinations(range(len(groups)), 2):
            if any(seconds[a] < 10 or seconds[b] < 10 or float(raw[a] @ raw[b]) < MERGE_RAW_MIN
                   or any(s.speaker == a and t.speaker == b and min(s.end, t.end) > max(s.start, t.start)
                          for s in spans for t in spans)
                   for a in groups[i] for b in groups[j]):
                continue
            score = float(np.mean([sim(a, b) for a in groups[i] for b in groups[j]]))
            if score >= MERGE_SIMILARITY and (best is None or score > best[0]):
                best = (score, i, j)
        if best is None:
            break
        groups[best[1]] += groups.pop(best[2])
    return {k: max(g, key=lambda x: seconds[x]) for g in groups for k in g}


def diarize(path: str, samples_dir: str = "", num_speakers: int = -1) -> dict:
    samples = load_audio(path)
    diarizer, extractor = build_diarizer(num_speakers)
    spans = diarizer.process(samples).sort_by_start_time() if len(samples) > RATE * 2 else []
    raw = {}
    for span in spans:
        raw.setdefault(span.speaker, []).append(span)
    evidence = {k: voice_evidence(extractor, samples, v, spans) for k, v in raw.items()}
    seconds = {k: e["clean_seconds"] for k, e in evidence.items() if e["embeddings"]}
    mapping = {}
    if seconds and num_speakers == -1:
        mapping = merge_voices({k: np.mean(valid_vectors(evidence[k]["embeddings"]), axis=0)
                                for k in seconds}, seconds, spans)
    spans = [SimpleNamespace(start=s.start, end=s.end, speaker=mapping.get(s.speaker, s.speaker)) for s in spans]
    by_speaker = {}
    for span in spans:
        by_speaker.setdefault(span.speaker, []).append(span)
    speakers = {}
    for speaker, items in by_speaker.items():
        seconds = sum(s.end - s.start for s in items)
        evidence = voice_evidence(extractor, samples, items, spans)
        vectors = valid_vectors(evidence["embeddings"])
        vector = np.mean(vectors, axis=0).tolist() if vectors else []
        info = {"seconds": round(seconds, 1), "embedding": vector, "evidence": evidence,
                "suggestion": None, "sample": None}
        if samples_dir and write_sample(samples, items, spans, Path(samples_dir) / f"speaker_{speaker}.wav"):
            info["sample"] = f"speaker_{speaker}.wav"
        speakers[str(speaker)] = info
    for key, found in suggest_names({k: v["evidence"] for k, v in speakers.items()}).items():
        speakers[key]["suggestion"] = found
    turns = [{"start": round(s.start, 2), "end": round(s.end, 2), "speaker": s.speaker} for s in spans]
    return {"duration": round(len(samples) / RATE, 1), "turns": turns, "speakers": speakers}


if __name__ == "__main__" and sys.argv[1] == "--match":
    # {voz: embedding} -> {voz: sugestão}: reaplica os perfis cadastrados a vozes de reuniões antigas
    Path(sys.argv[3]).write_text(json.dumps(suggest_names(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))),
                                 encoding="utf-8")
elif __name__ == "__main__":
    Path(sys.argv[2]).write_text(json.dumps(diarize(sys.argv[1], sys.argv[3] if len(sys.argv) > 3 else "",
                                                 int(sys.argv[4]) if len(sys.argv) > 4 else -1)),
                                 encoding="utf-8")
