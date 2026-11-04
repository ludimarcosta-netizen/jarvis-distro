"""Local ONNX inference. No microphone, network API, or automatic model downloads."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import sherpa_onnx as sherpa

RATE = 16000

# Same-voice threshold used to collapse diarization false splits inside one
# 5-second enrollment window. Same speaker across windows measures ~0.70-0.85
# with this extractor; different voices measure well below this.
SAME_VOICE_SIMILARITY = 0.65


class Engine:
    def __init__(self, models: Path):
        active_model = models / "speaker-ecapa512-sherpa.onnx"
        legacy_model = models / "speaker.onnx"
        embedding = sherpa.SpeakerEmbeddingExtractorConfig(
            model=str(active_model), num_threads=2, provider="cpu")
        config = sherpa.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(models / "segmentation.onnx")), num_threads=2, provider="cpu"),
            embedding=embedding,
            clustering=sherpa.FastClusteringConfig(num_clusters=-1, threshold=0.5),
            min_duration_on=0.3, min_duration_off=0.3)
        if not config.validate():
            raise RuntimeError("Modelos locais ausentes ou inválidos")
        self.diarizer = sherpa.OfflineSpeakerDiarization(config)
        self.extractor = sherpa.SpeakerEmbeddingExtractor(embedding)
        self.model_id = hashlib.sha256(active_model.read_bytes()).hexdigest()
        legacy_config = sherpa.SpeakerEmbeddingExtractorConfig(
            model=str(legacy_model), num_threads=2, provider="cpu")
        self.legacy_extractor = sherpa.SpeakerEmbeddingExtractor(legacy_config)
        self.legacy_model_id = hashlib.sha256(legacy_model.read_bytes()).hexdigest()
        self._whisper = None

    def legacy_embedding(self, samples) -> list[float] | None:
        stream = self.legacy_extractor.create_stream()
        stream.accept_waveform(sample_rate=RATE, waveform=np.ascontiguousarray(samples))
        stream.input_finished()
        if not self.legacy_extractor.is_ready(stream):
            return None
        return np.asarray(self.legacy_extractor.compute(stream)).reshape(-1).tolist()

    def _clip_embedding(self, samples, ranges):
        """Embedding for a list of (first, last) sample ranges; (None, clip) when too short."""
        clip = np.concatenate([samples[first:last] for first, last in ranges])
        if len(clip) < RATE:
            return None, clip
        stream = self.extractor.create_stream()
        stream.accept_waveform(sample_rate=RATE, waveform=clip)
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            return None, clip
        return np.asarray(self.extractor.compute(stream)).reshape(-1), clip

    @staticmethod
    def _cosine(a, b) -> float:
        norm = float(np.linalg.norm(a)) * float(np.linalg.norm(b))
        return float(np.dot(a, b) / norm) if norm > 1e-9 else 0.0

    def _merge_false_split(self, samples, groups):
        """Collapse diarization false splits inside one enrollment window.

        Short single-speaker windows are frequently clustered into 2+
        "speakers". Clusters holding >= 1s of speech are verified by
        embedding: if they are all the same voice the window is one speaker.
        A genuinely different voice keeps the window rejected (return None).
        Unverifiable splinters (< 1s) may only ride along with ONE verified
        voice that dominates the window's speech.
        """
        speech = {speaker: sum(last - first for first, last in ranges)
                  for speaker, ranges in groups.items()}
        verifiable = [s for s in groups if speech[s] >= RATE]
        embeddings = {}
        for speaker in verifiable:
            vector, _ = self._clip_embedding(samples, groups[speaker])
            if vector is None:
                return None  # long cluster that cannot be embedded: stay rejected
            embeddings[speaker] = vector
        if len(embeddings) > 1:
            names = list(embeddings)
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    if self._cosine(embeddings[a], embeddings[b]) < SAME_VOICE_SIMILARITY:
                        return None  # two real voices in the window
        if not embeddings:
            return None  # only splinters; nothing can be verified
        if len(embeddings) == 1:
            verified = next(iter(embeddings))
            total = sum(speech.values())
            if speech[verified] < 0.6 * total:
                return None  # an unverifiable cluster rivals the verified voice
        merged = []
        for speaker, ranges in groups.items():
            if speaker in embeddings or speech[speaker] < RATE:
                merged.extend(ranges)
        if not merged:
            return None
        return {0: sorted(merged)}

    def segments(self, pcm: bytes, *, enrollment: bool = False, merge_speech: bool = False) -> list[dict]:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        if len(samples) < RATE * 2 or np.sqrt(np.mean(samples ** 2)) < 0.004:
            return []
        spans = self.diarizer.process(samples).sort_by_start_time()
        if enrollment or merge_speech:
            # Natural pauses must not discard all sub-two-second phrases.
            # Join only non-overlapping speech belonging to the same speaker.
            groups = {}
            for span in spans:
                if any(other.speaker != span.speaker and min(span.end, other.end) > max(span.start, other.start)
                       for other in spans):
                    return []
                first = max(0, int(span.start * RATE))
                last = min(len(samples), int(span.end * RATE))
                if last > first:
                    groups.setdefault(span.speaker, []).append((first, last))
            if len(groups) > 1:
                # One speaker in a short window often clusters into 2+ speakers.
                # Verify by embedding: same voice -> merge; a real second voice
                # keeps this window out of the enrollment.
                groups = self._merge_false_split(samples, groups)
                if not groups:
                    return []  # never combine different speakers into an enrollment
            result = []
            for speaker, ranges in groups.items():
                clip = np.concatenate([samples[first:last] for first, last in ranges])
                minimum_speech = RATE if merge_speech and not enrollment else RATE * 2
                if len(clip) < minimum_speech or np.mean(np.abs(clip) > 0.98) > 0.02:
                    continue
                stream = self.extractor.create_stream()
                stream.accept_waveform(sample_rate=RATE, waveform=clip)
                stream.input_finished()
                if not self.extractor.is_ready(stream):
                    continue
                vector = np.asarray(self.extractor.compute(stream)).reshape(-1).tolist()
                result.append({"start": ranges[0][0] / RATE, "end": ranges[-1][1] / RATE,
                               "speaker": speaker, "embedding": vector, "samples": clip,
                               "pcm": b"".join(pcm[first * 2:last * 2] for first, last in ranges)})
            return result
        result = []
        for span in spans:
            if span.end - span.start < 2:
                continue
            # Reject overlapping speakers instead of learning a blended voice.
            if any(other.speaker != span.speaker and min(span.end, other.end) > max(span.start, other.start)
                   for other in spans):
                continue
            first, last = int(span.start * RATE), min(len(samples), int(span.end * RATE))
            clip = np.ascontiguousarray(samples[first:last])
            if np.mean(np.abs(clip) > 0.98) > 0.02:
                continue
            stream = self.extractor.create_stream()
            stream.accept_waveform(sample_rate=RATE, waveform=clip)
            stream.input_finished()
            if not self.extractor.is_ready(stream):
                continue
            vector = np.asarray(self.extractor.compute(stream)).reshape(-1).tolist()
            result.append({"start": span.start, "end": span.end, "speaker": span.speaker, "embedding": vector,
                           "pcm": pcm[first * 2:last * 2], "samples": clip})
        return result

    def transcript(self, samples) -> str:
        if self._whisper is None:
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel("base", device="cpu", compute_type="int8",
                                         cpu_threads=2, local_files_only=True)
        segments, _ = self._whisper.transcribe(samples, language="pt", beam_size=3,
                                               condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments)
