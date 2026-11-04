"""Turn a still (generated or supplied) into a moving B-roll clip.

Modes:
    full   the image fills the frame with a slow camera move (Ken Burns) and
           REPLACES the footage while it plays (opaque .mp4). The voice keeps
           running underneath: classic "show what I'm talking about".
    card   the image floats as a framed picture-in-picture card (rounded
           corners, accent border, shadow) and the speaker stays visible
           (transparent .mov). Good for screenshots, products, documents.

Motion (full): zoom-in, zoom-out, pan-left, pan-right, pan-up, pan-down, none.
Entrance (card): pop, slide. Both fade out at the end.

Usage:
    python helpers/broll.py edit/broll/b01.png -o edit/animations/b01.mp4 --duration 3.5 --motion zoom-in --match edit/base_preview.mp4
    python helpers/broll.py shot.png -o edit/animations/card1.mov --mode card --position right --brand edit/brand.json

EDL: {"file": "animations/b01.mp4", "start_in_output": 12.4, "duration": 3.5, "fade_in": 0.15, "fade_out": 0.15}
Moves are subpixel (PIL affine on float coordinates): no zoompan jitter.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from brand import load_brand  # noqa: E402
from text_overlay import ease_out_cubic, parse_color, parse_size, probe_size_fps  # noqa: E402

MOTIONS = ("zoom-in", "zoom-out", "pan-left", "pan-right", "pan-up", "pan-down", "none")
POSITIONS = ("right", "left", "center", "top-right", "top-left", "bottom-right", "bottom-left")
ZOOM = 1.15  # Ken Burns travel: enough to read as motion, slow enough not to distract


def ease_in_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ken_burns_params(motion: str, p: float) -> tuple[float, float, float]:
    """(zoom, center_x, center_y) at progress p; centers are 0..1 of the free travel."""
    e = ease_in_out(p)
    if motion == "zoom-in":
        return 1 + (ZOOM - 1) * e, 0.5, 0.5
    if motion == "zoom-out":
        return ZOOM - (ZOOM - 1) * e, 0.5, 0.5
    if motion == "pan-left":
        return ZOOM, 1 - e, 0.5
    if motion == "pan-right":
        return ZOOM, e, 0.5
    if motion == "pan-up":
        return ZOOM, 0.5, 1 - e
    if motion == "pan-down":
        return ZOOM, 0.5, e
    return 1.0, 0.5, 0.5


def full_frames(img: Image.Image, size: tuple[int, int], n: int, motion: str):
    W, H = size
    k = max(W / img.width, H / img.height)  # cover scale at zoom 1
    for i in range(n):
        z, fx, fy = ken_burns_params(motion, i / max(1, n - 1))
        s = k * z
        half_w, half_h = W / (2 * s), H / (2 * s)
        # fx/fy pick a center inside the travel that keeps the window on the image
        cx = half_w + fx * (img.width - 2 * half_w)
        cy = half_h + fy * (img.height - 2 * half_h)
        frame = img.transform(size, Image.AFFINE,
                              (1 / s, 0, cx - half_w, 0, 1 / s, cy - half_h),
                              resample=Image.BICUBIC)
        yield frame.tobytes()


def build_card(img: Image.Image, size: tuple[int, int], scale: float, border: tuple, radius_ratio: float = 0.04):
    W, H = size
    cw = int(W * scale)
    ch = int(img.height * cw / img.width)
    if ch > H * 0.8:  # tall images: limit by height instead
        ch = int(H * 0.8)
        cw = int(img.width * ch / img.height)
    photo = img.resize((cw, ch), Image.LANCZOS).convert("RGBA")
    bw = max(3, cw // 120)
    radius = int(min(cw, ch) * radius_ratio) + bw
    pad = max(12, cw // 18)  # room for the shadow
    card = Image.new("RGBA", (cw + 2 * bw + 2 * pad, ch + 2 * bw + 2 * pad), (0, 0, 0, 0))
    box = [pad, pad, pad + cw + 2 * bw - 1, pad + ch + 2 * bw - 1]
    shadow = Image.new("RGBA", card.size, (0, 0, 0, 0))
    off = pad // 3
    ImageDraw.Draw(shadow).rounded_rectangle([box[0] + off, box[1] + off, box[2] + off, box[3] + off],
                                             radius=radius, fill=(0, 0, 0, 150))
    card = Image.alpha_composite(card, shadow.filter(ImageFilter.GaussianBlur(pad / 2.5)))
    ImageDraw.Draw(card).rounded_rectangle(box, radius=radius, fill=border)
    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, cw - 1, ch - 1], radius=max(1, radius - bw), fill=255)
    card.paste(photo, (pad + bw, pad + bw), mask)
    return card


def card_anchor(size: tuple[int, int], card: Image.Image, position: str) -> tuple[int, int]:
    W, H = size
    mx, my = int(W * 0.04), int(H * 0.06)
    xs = {"left": mx, "center": (W - card.width) // 2, "right": W - card.width - mx}
    ys = {"top": my, "center": (H - card.height) // 2, "bottom": H - card.height - my}
    if "-" in position:
        v, h = position.split("-")
        return xs[h], ys[v]
    return xs[position], ys["center"]


def card_frames(card: Image.Image, size: tuple[int, int], n: int, fps: float, position: str,
                entrance: str, intro: float, outro: float):
    W, H = size
    x0, y0 = card_anchor(size, card, position)
    duration = n / fps
    from_right = x0 + card.width / 2 > W / 2
    for i in range(n):
        t = i / fps
        appear = ease_out_cubic(t / intro) if intro else 1.0
        fade = ease_out_cubic((duration - t) / outro) if outro else 1.0
        # slow drift keeps a held card alive: +3% over the clip
        scale = 1.0 + 0.03 * (t / duration)
        dx = 0
        opacity = min(appear, fade)
        if entrance == "pop":
            scale *= 0.85 + 0.15 * appear
        else:  # slide in from the nearest side
            dist = (W - x0) if from_right else (x0 + card.width)
            dx = int((1 - appear) * dist * (1 if from_right else -1))
            opacity = fade
        layer = card
        if abs(scale - 1.0) > 0.001:
            layer = card.resize((max(1, int(card.width * scale)), max(1, int(card.height * scale))), Image.BICUBIC)
        if opacity < 0.999:
            layer = layer.copy()
            layer.putalpha(layer.getchannel("A").point(lambda a, o=opacity: int(a * o)))
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        if opacity > 0.001:
            px = x0 + dx - (layer.width - card.width) // 2
            py = y0 - (layer.height - card.height) // 2
            frame.paste(layer, (px, py))
        yield frame.tobytes()


def encode(frames, size: tuple[int, int], fps: str, out: Path, alpha: bool) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    W, H = size
    if alpha:
        codec = ["-c:v", "qtrle", "-pix_fmt", "argb"]
        pix_in = "rgba"
    else:
        codec = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        pix_in = "rgb24"
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", pix_in, "-s", f"{W}x{H}",
           "-r", fps, "-i", "-", *codec, str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin
    try:
        for f in frames:
            proc.stdin.write(f)
    finally:
        proc.stdin.close()
    if proc.wait() != 0:
        sys.exit("ffmpeg failed to encode the B-roll clip")


def main() -> None:
    ap = argparse.ArgumentParser(description="Still image -> moving B-roll clip")
    ap.add_argument("image", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True, help=".mp4 for full, .mov for card")
    ap.add_argument("--mode", choices=("full", "card"), default="full")
    ap.add_argument("--duration", type=float, default=3.5)
    ap.add_argument("--motion", choices=MOTIONS, default="zoom-in", help="full mode camera move")
    ap.add_argument("--position", choices=POSITIONS, default="right", help="card mode")
    ap.add_argument("--entrance", choices=("pop", "slide"), default="pop", help="card mode")
    ap.add_argument("--scale", type=float, default=0.45, help="card width as a share of the frame")
    ap.add_argument("--border", type=parse_color, help="card border (default brand accent)")
    ap.add_argument("--intro", type=float, default=0.4)
    ap.add_argument("--outro", type=float, default=0.3)
    ap.add_argument("--match", type=Path, help="Take size and fps from this video (a rendered base)")
    ap.add_argument("--size", type=parse_size, help="WIDTHxHEIGHT (default 1920x1080)")
    ap.add_argument("--fps", help="Default 30, or --match's rate")
    ap.add_argument("--brand", type=Path)
    args = ap.parse_args()

    if not args.image.is_file():
        sys.exit(f"image not found: {args.image}")
    size, fps = (1920, 1080), "30"
    if args.match:
        size, fps = probe_size_fps(args.match)
    if args.size:
        size = args.size
    if args.fps:
        fps = args.fps
    n = max(1, round(args.duration * float(Fraction(fps))))
    img = Image.open(args.image).convert("RGB")

    if args.mode == "full":
        if args.output.suffix.lower() == ".mov":
            print("note: full mode is opaque; .mp4 is smaller", file=sys.stderr)
        encode(full_frames(img, size, n, args.motion), size, fps, args.output, alpha=False)
    else:
        if args.output.suffix.lower() not in (".mov",):
            sys.exit("card mode needs alpha: use a .mov output")
        border = args.border or parse_color(load_brand(args.brand)["colors"]["accent"])
        card = build_card(img, size, args.scale, border)
        encode(card_frames(card, size, n, float(Fraction(fps)), args.position, args.entrance,
                           args.intro, args.outro), size, fps, args.output, alpha=True)
    print(f"{args.mode} b-roll -> {args.output}  ({size[0]}x{size[1]} @ {fps}, {args.duration:.2f}s)")


if __name__ == "__main__":
    main()
