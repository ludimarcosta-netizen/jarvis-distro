"""Small, preloaded PCM bank. Playback never invokes an AI or an external player."""
from __future__ import annotations

import json
import re
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "voice_phrases"


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


@dataclass(frozen=True)
class Clip:
    text: str
    pcm: bytes
    bridge: bool


class Bank:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.clips: list[Clip] = []
        self.cursor = 0

    def load(self):
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        rows = manifest["clips"]
        if manifest.get("version") != 1 or not isinstance(rows, list) or len(rows) > 20:
            raise ValueError("Catálogo de voz inválido")
        clips = []
        for row in rows:
            path = (self.root / row["file"]).resolve()
            text = row["text"]
            if not path.is_relative_to(self.root) or not isinstance(text, str) or not 0 < len(text) <= 200:
                raise ValueError("Frase de voz inválida")
            with wave.open(str(path), "rb") as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 24000):
                    raise ValueError("Frase exige PCM16 mono de 24 kHz")
                if not 0 < audio.getnframes() <= 8 * 24000:
                    raise ValueError("Frase de voz longa demais")
                pcm = audio.readframes(audio.getnframes())
                if len(pcm) != audio.getnframes() * 2:
                    raise ValueError("Frase de voz incompleta")
            clips.append(Clip(text, pcm, row.get("bridge") is True))
        self.clips = clips

    def acknowledgement(self) -> Clip | None:
        choices = [clip for clip in self.clips if clip.bridge]
        if not choices:
            return None
        clip = choices[self.cursor % len(choices)]
        self.cursor += 1
        return clip

    def match(self, text: str) -> Clip | None:
        key = normalized(text)
        return next((clip for clip in self.clips if normalized(clip.text) == key), None)
