"""Word-highlight captions (.ass): the spoken word lights up in the accent colour.

The retention-edit caption style: short phrase-aware chunks (same chunking as
render.py's SRT), each shown while it is spoken, with the CURRENT word in the
brand accent colour and slightly bigger. Times come from the word-level
transcript on the output timeline (Rule 5), so the highlight follows the voice.

Usage:
    python helpers/captions.py edit/edl.json -o edit/captions.ass --match edit/base_preview.mp4 --brand edit/brand.json
Then point the EDL at it: "subtitles": "captions.ass" (render.py applies it LAST,
using the styles inside the file).

Options: --case upper|natural, --font-size, --margin-v, --no-pop.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from brand import load_brand  # noqa: E402
from render import iter_output_chunks  # noqa: E402
from text_overlay import parse_color, parse_size, probe_size_fps  # noqa: E402


def ass_color(rgba: tuple[int, int, int, int]) -> str:
    """ASS colour &HAABBGGRR (alpha inverted: 00 = opaque)."""
    r, g, b, a = rgba
    return f"&H{255 - a:02X}{b:02X}{g:02X}{r:02X}"


def ass_time(seconds: float) -> str:
    cs = int(round(max(0.0, seconds) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def clean_word(word: str, upper: bool) -> str:
    word = re.sub(r"\s+", " ", word).strip()
    return word.upper() if upper else word


def build_events(chunks, accent: str, upper: bool, pop: bool) -> list[str]:
    """One Dialogue per word: the whole chunk, with the active word highlighted.
    Events are back-to-back inside a chunk (no flicker between words)."""
    events: list[str] = []
    cleaned = [[(clean_word(w, upper), s, e) for w, s, e in chunk if w.strip()] for chunk in chunks]
    cleaned = [c for c in cleaned if c]
    for ci, words in enumerate(cleaned):
        # the last word's trailing comma/semicolon reads as noise at caption size
        last = words[-1]
        words[-1] = (last[0].rstrip(",;:"), last[1], last[2])
        # hold the chunk across a short pause instead of blinking off (max +0.5 s)
        hold = last[2] + 0.5
        if ci + 1 < len(cleaned):
            hold = min(hold, cleaned[ci + 1][0][1])
        for j, (_, start, _) in enumerate(words):
            end = words[j + 1][1] if j + 1 < len(words) else max(last[2], hold, start + 0.25)
            if end <= start:
                continue
            parts = []
            for k, (w, _, _) in enumerate(words):
                w = ass_escape(w)
                parts.append(f"{{\\c{accent}\\fscx112\\fscy112}}{w}{{\\r}}" if k == j else w)
            text = " ".join(parts)
            if pop and j == 0:
                # the chunk lands with a quick scale-up
                text = "{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}" + text
            events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Word,,0,0,0,,{text}")
    return events


def build_ass(chunks, size: tuple[int, int], font: str, font_size: int, margin_v: int,
              text_color: str, accent: str, upper: bool, pop: bool) -> str:
    W, H = size
    outline = max(2, font_size // 14)
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {H}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Word,{font},{font_size},{text_color},{text_color},&H00000000,&H90000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},{max(1, outline // 2)},2,{W // 12},{W // 12},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    return "\n".join(header + build_events(chunks, accent, upper, pop)) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Word-highlight .ass captions from the EDL + transcripts")
    ap.add_argument("edl", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--match", type=Path, help="Rendered base: caption canvas = its size")
    ap.add_argument("--size", type=parse_size, help="WIDTHxHEIGHT (default 1920x1080)")
    ap.add_argument("--brand", type=Path)
    ap.add_argument("--font", help="Font FAMILY name (default brand caption_family)")
    ap.add_argument("--font-size", type=int)
    ap.add_argument("--margin-v", type=int, help="px from the bottom (default: 12%% landscape, 30%% portrait)")
    ap.add_argument("--case", choices=("upper", "natural"), default="upper")
    ap.add_argument("--no-pop", action="store_true")
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    size = (1920, 1080)
    if args.match:
        size, _ = probe_size_fps(args.match)
    if args.size:
        size = args.size
    W, H = size
    portrait = H > W
    brand = load_brand(args.brand)
    font_size = args.font_size or int(min(W, H) * 0.07)
    # portrait keeps captions above the TikTok/Reels/Shorts UI band (bottom ~30%)
    margin_v = args.margin_v if args.margin_v is not None else int(H * (0.30 if portrait else 0.12))
    chunks = list(iter_output_chunks(edl, edl_path.parent))
    ass = build_ass(chunks, size, args.font or brand["fonts"]["caption_family"], font_size, margin_v,
                    ass_color(parse_color(brand["colors"]["text"])),
                    ass_color(parse_color(brand["colors"]["accent"])),
                    args.case == "upper", not args.no_pop)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(ass, encoding="utf-8")
    print(f"captions -> {args.output}  ({len(chunks)} chunks, {W}x{H}, font {font_size}px)")


if __name__ == "__main__":
    main()
