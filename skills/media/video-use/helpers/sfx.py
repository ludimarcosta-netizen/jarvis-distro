"""Synthesize simple sound effects (no library, no download, no licence issue).

    whoosh   filtered-noise sweep, for transitions and B-roll entrances
    pop      short pitched blip, for a card/text landing
    click    UI click, for screen recordings and checklist ticks
    impact   low boom + burst, for a reveal or a hard cut on a punchline
    riser    rising tension into a reveal (ends on its peak)
    ding     bright bell, for "correct"/a number landing

Each run prints the effect's `attack` (seconds from file start to its
transient). Start the effect `attack` seconds BEFORE the visible event:
    EDL sfx: {"file": "sfx/whoosh.wav", "at": <event_time - attack>, "gain_db": -8}

Usage:
    python helpers/sfx.py whoosh -o edit/sfx/whoosh.wav [--duration 0.6] [--seed 3]
    python helpers/sfx.py all --out-dir edit/sfx           # one of each

User-supplied effects (.wav/.mp3) work the same way in the EDL; prefer them
when the user has a sound library.
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

SR = 48000
DEFAULT_DURATION = {"whoosh": 0.6, "pop": 0.12, "click": 0.05, "impact": 1.2, "riser": 2.0, "ding": 1.0}


def _env(n: int, attack: float, release: float) -> np.ndarray:
    """Attack/release envelope over n samples (fractions of n)."""
    t = np.linspace(0, 1, n, endpoint=False)
    a = max(attack, 1e-4)
    up = np.clip(t / a, 0, 1)
    down = np.clip((1 - t) / max(release, 1e-4), 0, 1)
    return np.minimum(up, down) ** 1.5


def _sweep_filter(noise: np.ndarray, f_start: float, f_end: float, q: float = 1.2) -> np.ndarray:
    """Time-varying band-pass (state-variable filter) sweeping f_start -> f_end."""
    n = len(noise)
    freqs = np.geomspace(f_start, f_end, n)
    out = np.zeros(n)
    low = band = 0.0
    damp = 1.0 / q
    for i in range(n):
        f = 2 * np.sin(np.pi * freqs[i] / SR)
        high = noise[i] - low - damp * band
        band += f * high
        low += f * band
        out[i] = band
    return out


def whoosh(d: float, rng: np.random.Generator) -> np.ndarray:
    n = int(SR * d)
    sig = _sweep_filter(rng.standard_normal(n), 300, 4500) * _env(n, 0.55, 0.45)
    # a touch of stereo width: right channel is a slightly shifted copy
    return np.stack([sig, np.roll(sig, int(SR * 0.004))], axis=1)


def pop(d: float, rng: np.random.Generator) -> np.ndarray:
    n = int(SR * d)
    t = np.arange(n) / SR
    freq = 900 * np.exp(-t * 18) + 300
    sig = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * 30)
    return np.stack([sig, sig], axis=1)


def click(d: float, rng: np.random.Generator) -> np.ndarray:
    n = int(SR * d)
    t = np.arange(n) / SR
    sig = (np.sin(2 * np.pi * 2500 * t) * 0.6 + rng.standard_normal(n) * 0.4) * np.exp(-t * 250)
    return np.stack([sig, sig], axis=1)


def impact(d: float, rng: np.random.Generator) -> np.ndarray:
    n = int(SR * d)
    t = np.arange(n) / SR
    boom = np.sin(2 * np.pi * np.cumsum(70 * np.exp(-t * 3) + 35) / SR) * np.exp(-t * 3.5)
    burst = _sweep_filter(rng.standard_normal(n), 2500, 400) * np.exp(-t * 14) * 0.6
    sig = boom + burst
    return np.stack([sig, sig], axis=1)


def riser(d: float, rng: np.random.Generator) -> np.ndarray:
    n = int(SR * d)
    t = np.arange(n) / SR
    p = t / d
    noise = _sweep_filter(rng.standard_normal(n), 200, 6000) * p ** 2
    tone = np.sin(2 * np.pi * np.cumsum(200 + 600 * p ** 2) / SR) * 0.35 * p ** 1.5
    sig = (noise + tone) * np.clip((1 - p) / 0.02, 0, 1)  # ends on the peak, 20 ms cut-off
    return np.stack([sig, np.roll(sig, int(SR * 0.006))], axis=1)


def ding(d: float, rng: np.random.Generator) -> np.ndarray:
    n = int(SR * d)
    t = np.arange(n) / SR
    sig = sum(a * np.sin(2 * np.pi * f * t) for f, a in ((1320, 1.0), (2640, 0.4), (3960, 0.15)))
    sig = sig * np.exp(-t * 4) * np.clip(t / 0.003, 0, 1)
    return np.stack([sig, sig], axis=1)


EFFECTS = {"whoosh": whoosh, "pop": pop, "click": click, "impact": impact, "riser": riser, "ding": ding}


def normalize(sig: np.ndarray, peak_db: float = -3.0) -> np.ndarray:
    peak = np.max(np.abs(sig)) or 1.0
    return sig / peak * (10 ** (peak_db / 20))


def attack_seconds(sig: np.ndarray) -> float:
    """Time of the loudest sample: the moment the ear reads as "the hit"
    (transient for pop/click/impact, swell peak for whoosh, end for riser)."""
    mono = np.abs(sig).max(axis=1)
    peak_i = int(np.argmax(mono))
    return round(peak_i / SR, 3)


def write_wav(path: Path, sig: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(sig, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def make(effect: str, out: Path, duration: float | None, seed: int) -> float:
    rng = np.random.default_rng(seed)
    sig = normalize(EFFECTS[effect](duration or DEFAULT_DURATION[effect], rng))
    write_wav(out, sig)
    return attack_seconds(sig)


def main() -> None:
    ap = argparse.ArgumentParser(description="Synthesize sound effects")
    ap.add_argument("effect", choices=[*EFFECTS, "all"])
    ap.add_argument("-o", "--output", type=Path, help="Output .wav (single effect)")
    ap.add_argument("--out-dir", type=Path, help="Directory for `all`")
    ap.add_argument("--duration", type=float)
    ap.add_argument("--seed", type=int, default=3)
    args = ap.parse_args()

    if args.effect == "all":
        if not args.out_dir:
            ap.error("all needs --out-dir")
        for name in EFFECTS:
            out = args.out_dir / f"{name}.wav"
            print(f"{name:7s} -> {out}  attack={make(name, out, None, args.seed):.3f}s")
        return
    if not args.output:
        ap.error("-o is required")
    print(f"{args.effect} -> {args.output}  attack={make(args.effect, args.output, args.duration, args.seed):.3f}s")


if __name__ == "__main__":
    sys.exit(main())
