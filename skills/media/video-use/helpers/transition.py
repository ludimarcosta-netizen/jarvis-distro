"""Transition clips that sit OVER a cut (transparent .mov overlays).

Real crossfades would re-encode across segment boundaries and break the
lossless concat (Hard Rule 2). Instead the transition is an overlay whose
midpoint covers the frame exactly at the cut:

    flash   a quick white (or brand) flash, peaks at the cut
    swipe   a slanted accent band wipes across, fully covering the frame at the cut

Place it at `cut_time - duration/2` (render.py writes cut times to
`<edit>/timeline.json`). Pair a swipe with a whoosh for the classic feel.

Usage:
    python helpers/transition.py swipe -o edit/animations/t1.mov --match edit/base_preview.mp4 --brand edit/brand.json
    python helpers/transition.py flash -o edit/animations/f1.mov --duration 0.3
"""

from __future__ import annotations

import argparse
import sys
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from brand import load_brand  # noqa: E402
from broll import ease_in_out, encode  # noqa: E402
from text_overlay import parse_color, parse_size, probe_size_fps  # noqa: E402

STYLES = ("flash", "swipe")
DEFAULT_DURATION = {"flash": 0.3, "swipe": 0.5}


def flash_frames(size, n, color):
    for i in range(n):
        p = i / max(1, n - 1)
        # triangle peaking at the middle frame (the cut), max 85% so it reads as light, not a blank frame
        alpha = int(255 * 0.85 * (1 - abs(2 * p - 1)) ** 0.7)
        yield Image.new("RGBA", size, color[:3] + (alpha,)).tobytes()


def swipe_frames(size, n, color):
    W, H = size
    skew = int(H * 0.35)
    margin = int(W * 0.05)
    band = W + 2 * skew + margin
    for i in range(n):
        e = ease_in_out(i / max(1, n - 1))
        left = -band + (W + band) * e
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(frame).polygon(
            [(left + skew, 0), (left + band + skew, 0), (left + band - skew, H), (left - skew, H)],
            fill=color[:3] + (255,),
        )
        yield frame.tobytes()


def main() -> None:
    ap = argparse.ArgumentParser(description="Transition overlay clip (place centered on a cut)")
    ap.add_argument("style", choices=STYLES)
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output .mov (alpha)")
    ap.add_argument("--duration", type=float)
    ap.add_argument("--color", type=parse_color, help="flash: white; swipe: brand accent")
    ap.add_argument("--match", type=Path)
    ap.add_argument("--size", type=parse_size)
    ap.add_argument("--fps")
    ap.add_argument("--brand", type=Path)
    args = ap.parse_args()

    if args.output.suffix.lower() != ".mov":
        sys.exit("transitions need alpha: use a .mov output")
    size, fps = (1920, 1080), "30"
    if args.match:
        size, fps = probe_size_fps(args.match)
    if args.size:
        size = args.size
    if args.fps:
        fps = args.fps
    duration = args.duration or DEFAULT_DURATION[args.style]
    n = max(3, round(duration * float(Fraction(fps))))
    color = args.color or (parse_color("#FFFFFF") if args.style == "flash"
                           else parse_color(load_brand(args.brand)["colors"]["accent"]))
    frames = (flash_frames if args.style == "flash" else swipe_frames)(size, n, color)
    encode(frames, size, fps, args.output, alpha=True)
    print(f"{args.style} -> {args.output}  ({duration:.2f}s; start it at cut_time - {duration / 2:.3f})")


if __name__ == "__main__":
    main()
