"""Render a text insert as a transparent overlay clip (PIL → QuickTime RLE .mov).

Ready-made inserts for the common requests — put a title, a name card, typed
text or a callout over the video — so they don't have to be rebuilt from
scratch. The clip is full-frame with alpha: everything outside the text stays
transparent, so the speaker remains visible. Point an EDL overlay at it:

    {"file": "animations/slot_1/render.mov", "start_in_output": 4.2, "duration": 4.0}

Styles:
    title        big centered text (+ optional subtext), rises and fades in
    lower_third  accent bar + name + role, slides in from the left
    typewriter   text typed letter by letter (anchored on the full string)
    callout      boxed text that pops in at a position
    badge        broadcast badge: glass card, accent pill, small label (--subtext) over a big value

Usage:
    python helpers/text_overlay.py title "Como funciona" --subtext "em 3 passos" -o out.mov
    python helpers/text_overlay.py lower_third "__JARVIS_USER_FULLNAME__" --subtext "Fundador" --match edit/base_preview.mp4 -o out.mov
    python helpers/text_overlay.py typewriter "npm run build" --font C:/Windows/Fonts/consolab.ttf -o out.mov
    python helpers/text_overlay.py callout "Clique aqui" --position 1200,300 --accent "#FF5A00" -o out.mov

Colors are #RRGGBB or #RRGGBBAA. The output size must match the rendered video
(render.py scales to 1920 wide, or 1080x1920 for portrait): use --match on a
rendered base or --size.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from brand import load_brand  # noqa: E402

STYLES = ("title", "lower_third", "typewriter", "callout", "badge")
WINDOWS_FONTS = Path("C:/Windows/Fonts")
DEFAULT_FONTS = ("segoeuib.ttf", "arialbd.ttf")  # bold, both ship with Windows
DEFAULT_SUB_FONTS = ("segoeui.ttf", "arial.ttf")

Color = tuple[int, int, int, int]


def ease_out_cubic(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def parse_color(value: str) -> Color:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?", value.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"color must be #RRGGBB or #RRGGBBAA, got {value!r}")
    rgb = m.group(1)
    alpha = int(m.group(2), 16) if m.group(2) else 255
    return (int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16), alpha)


def parse_size(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{2,5})x(\d{2,5})", value.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"size must be WIDTHxHEIGHT, got {value!r}")
    w, h = int(m.group(1)), int(m.group(2))
    if w % 2 or h % 2:
        raise argparse.ArgumentTypeError("width and height must be even")
    return w, h


def probe_size_fps(video: Path) -> tuple[tuple[int, int], str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate",
         "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(out.stdout)["streams"][0]
    fps = stream.get("avg_frame_rate") or "30"
    return (int(stream["width"]), int(stream["height"])), ("30" if fps == "0/0" else fps)


def resolve_font(path: str | None, defaults: tuple[str, ...]) -> Path:
    """An explicit font path, else a Windows default. Fails loudly: a missing
    font must never fall back silently to PIL's bitmap face."""
    if path:
        p = Path(path)
        if not p.is_file():
            sys.exit(f"font not found: {p}")
        return p
    for name in defaults:
        p = WINDOWS_FONTS / name
        if p.is_file():
            return p
    sys.exit("no default font found; pass --font /path/to/font.ttf")


def wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Greedy word wrap on measured width; explicit newlines are kept."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            trial = f"{current} {word}".strip()
            if current and font.getlength(trial) > max_width:
                lines.append(current)
                current = word
            else:
                current = trial
        lines.append(current)
    return lines


@dataclass
class Spec:
    style: str
    text: str
    subtext: str
    size: tuple[int, int]
    fps: str
    duration: float
    font: Path
    sub_font: Path
    font_size: int | None
    color: Color
    sub_color: Color
    accent: Color
    box: Color
    position: str | None
    intro: float
    outro: float
    char_s: float


# -------- Layers: each style draws its finished text block once ---------------


def _text_block(lines: list[str], font: ImageFont.FreeTypeFont, fill: Color,
                align: str = "center", limit: int | None = None,
                shadow: bool = False) -> Image.Image:
    """Lay out all `lines`; draw only the first `limit` characters (typewriter).
    The block is always sized for the full text, so partial text never moves.
    `shadow` adds a soft drop shadow, for text with no box over busy footage."""
    ascent, descent = font.getmetrics()
    line_h = int((ascent + descent) * 1.15)
    width = max(1, *(int(font.getlength(l)) for l in lines))
    offset = max(2, int(font.size) // 18) if shadow else 0
    m = 4 + 3 * offset  # margin keeps the blurred shadow inside the block
    img = Image.new("RGBA", (width + 2 * m, line_h * len(lines) + 2 * m), (0, 0, 0, 0))
    shade = Image.new("RGBA", img.size, (0, 0, 0, 0)) if shadow else None
    d = ImageDraw.Draw(img)
    left = limit
    for i, line in enumerate(lines):
        lw = font.getlength(line)
        x = m + ((width - lw) / 2 if align == "center" else 0)
        shown = line if left is None else line[:max(0, left)]
        if shown:
            d.text((x, m + i * line_h), shown, font=font, fill=fill)
            if shade:
                ImageDraw.Draw(shade).text((x + offset, m + i * line_h + offset), shown, font=font, fill=(0, 0, 0, 180))
        if left is not None:
            left -= len(line) + 1  # the wrap point counts as one typed space
    if shade:
        shade = shade.filter(ImageFilter.GaussianBlur(offset))
        shade.alpha_composite(img)
        return shade
    return img


def _stack(blocks: list[Image.Image], gap: int, align: str = "center") -> Image.Image:
    blocks = [b for b in blocks if b is not None]
    width = max(b.width for b in blocks)
    height = sum(b.height for b in blocks) + gap * (len(blocks) - 1)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    y = 0
    for b in blocks:
        x = (width - b.width) // 2 if align == "center" else 0
        img.alpha_composite(b, (x, y))
        y += b.height + gap
    return img


def _pad_box(layer: Image.Image, pad: int, fill: Color, radius: int) -> Image.Image:
    img = Image.new("RGBA", (layer.width + 2 * pad, layer.height + 2 * pad), (0, 0, 0, 0))
    if fill[3]:
        ImageDraw.Draw(img).rounded_rectangle(
            [0, 0, img.width - 1, img.height - 1], radius=radius, fill=fill)
    img.alpha_composite(layer, (pad, pad))
    return img


def _fonts(spec: Spec, main_ratio: float, sub_ratio: float):
    w, h = spec.size
    base = min(w, h)
    main_px = spec.font_size or int(base * main_ratio)
    main = ImageFont.truetype(str(spec.font), main_px)
    sub = ImageFont.truetype(str(spec.sub_font), max(10, int(main_px * sub_ratio)))
    return main, sub


def build_layer(spec: Spec, limit: int | None = None) -> Image.Image:
    """The style's block. `limit` (typewriter only) draws just the first N
    characters inside the full-size block and box."""
    w, h = spec.size
    max_w = int(w * 0.8)
    text = spec.text
    if spec.style == "title":
        main, sub = _fonts(spec, 0.09, 0.45)
        shadow = spec.box[3] == 0
        blocks = [_text_block(wrap(text, main, max_w), main, spec.color, shadow=shadow)]
        if spec.subtext:
            blocks.append(_text_block(wrap(spec.subtext, sub, max_w), sub, spec.accent, shadow=shadow))
        return _pad_box(_stack(blocks, int(main.size * 0.2)), int(main.size * 0.4), spec.box, int(main.size * 0.3))
    if spec.style == "lower_third":
        main, sub = _fonts(spec, 0.05, 0.62)
        blocks = [_text_block(wrap(text, main, max_w), main, spec.color, align="left")]
        if spec.subtext:
            blocks.append(_text_block(wrap(spec.subtext, sub, max_w), sub, spec.sub_color, align="left"))
        body = _pad_box(_stack(blocks, int(main.size * 0.1), align="left"), int(main.size * 0.35), spec.box, 4)
        bar = max(4, main.size // 7)
        img = Image.new("RGBA", (body.width + bar, body.height), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle([0, 0, bar - 1, body.height - 1], fill=spec.accent)
        img.alpha_composite(body, (bar, 0))
        return img
    if spec.style == "badge":
        # broadcast badge: glass card, accent pill, small label over a big value
        main, sub = _fonts(spec, 0.08, 0.36)
        blocks = []
        if spec.subtext:
            blocks.append(_text_block(wrap(spec.subtext, sub, max_w), sub, spec.sub_color, align="left"))
        blocks.append(_text_block(wrap(text, main, max_w), main, spec.color, align="left", shadow=True))
        body = _stack(blocks, 0, align="left")
        pad = int(main.size * 0.3)
        bar = max(6, main.size // 6)
        img = Image.new("RGBA", (body.width + bar + 3 * pad, body.height + 2 * pad), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=int(main.size * 0.28),
                            fill=spec.box, outline=(255, 255, 255, 200), width=max(2, main.size // 30))
        d.rounded_rectangle([pad, pad, pad + bar - 1, img.height - pad - 1], radius=bar // 2, fill=spec.accent)
        img.alpha_composite(body, (2 * pad + bar, pad))
        return img
    if spec.style == "typewriter":
        main, _ = _fonts(spec, 0.06, 0.5)
        return _pad_box(_text_block(wrap(text, main, max_w), main, spec.color, align="left", limit=limit),
                        int(main.size * 0.4), spec.box, int(main.size * 0.2))
    # callout
    main, _ = _fonts(spec, 0.045, 0.5)
    box = spec.box if spec.box[3] else spec.accent
    return _pad_box(_text_block(wrap(text, main, int(w * 0.5)), main, spec.color),
                    int(main.size * 0.45), box, int(main.size * 0.35))


def anchor(spec: Spec, layer: Image.Image) -> tuple[int, int]:
    """Top-left of the finished layer. Portrait frames keep bottom elements
    clear of the TikTok/Reels/Shorts UI (bottom ~30%)."""
    w, h = spec.size
    portrait = h > w
    if spec.position and "," in spec.position:
        x, y = spec.position.split(",", 1)
        return int(x), int(y)
    pos = spec.position or {"lower_third": "bottom", "badge": "top"}.get(spec.style, "center")
    margin = int(w * 0.06)
    if spec.style in ("lower_third", "badge"):
        x = margin
    else:
        x = (w - layer.width) // 2
    if pos == "top":
        y = int(h * (0.12 if portrait else 0.08))
    elif pos == "bottom":
        y = int(h * (0.68 if portrait else 0.88)) - layer.height
    elif pos == "center":
        y = (h - layer.height) // 2
    else:
        sys.exit(f"position must be top, center, bottom or X,Y, got {pos!r}")
    return x, y


def frame_count(spec: Spec) -> int:
    return max(1, round(spec.duration * float(Fraction(spec.fps))))


def render_frames(spec: Spec):
    """Yield RGBA frames (bytes). Each frame holds one layer on a transparent
    canvas, so a plain paste is exact and clips layers sliding in from outside."""
    fps = float(Fraction(spec.fps))
    w, h = spec.size
    full = build_layer(spec)
    x0, y0 = anchor(spec, full)
    n = frame_count(spec)
    cache: dict[int, Image.Image] = {}
    n_chars = len(spec.text)
    # typing ends with >= 1 s of hold before the fade-out (finished frame must be readable)
    type_s = min(n_chars * spec.char_s, max(0.1, spec.duration - spec.outro - 1.0))
    for i in range(n):
        t = i / fps
        appear = ease_out_cubic(t / spec.intro) if spec.intro else 1.0
        fade = ease_out_cubic((spec.duration - t) / spec.outro) if spec.outro else 1.0
        layer, dx, dy, opacity = full, 0, 0, min(appear, fade)
        if spec.style == "title":
            dy = int((1 - appear) * h * 0.04)
        elif spec.style in ("lower_third", "badge"):
            dx = int(-(1 - appear) * (full.width + x0))
            opacity = fade
        elif spec.style == "typewriter":
            chars = n_chars if t >= type_s else int(n_chars * t / type_s)
            if chars not in cache:
                cache[chars] = full if chars >= n_chars else build_layer(spec, limit=chars)
            layer, opacity = cache[chars], fade
        elif spec.style == "callout":
            scale = 0.85 + 0.15 * appear
            if scale < 0.999:
                sw, sh = max(1, int(full.width * scale)), max(1, int(full.height * scale))
                layer = full.resize((sw, sh), Image.LANCZOS)
                dx, dy = (full.width - sw) // 2, (full.height - sh) // 2
        frame = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if opacity > 0.001:
            if opacity < 0.999:
                layer = layer.copy()
                layer.putalpha(layer.getchannel("A").point(lambda a, o=opacity: int(a * o)))
            frame.paste(layer, (x0 + dx, y0 + dy))
        yield frame.tobytes()


def render(spec: Spec, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    w, h = spec.size
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", spec.fps, "-i", "-",
        # QuickTime RLE keeps alpha, is lossless and tiny on mostly-empty frames,
        # and ffmpeg's overlay reads it natively (no special decoder like VP9 alpha)
        "-c:v", "qtrle", "-pix_fmt", "argb",
        str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin
    try:
        for frame in render_frames(spec):
            proc.stdin.write(frame)
    finally:
        proc.stdin.close()
    if proc.wait() != 0:
        sys.exit("ffmpeg failed to encode the overlay")


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a transparent text overlay clip")
    ap.add_argument("style", choices=STYLES)
    ap.add_argument("text", help="Main text (\\n for an explicit line break)")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output .mov")
    ap.add_argument("--subtext", default="", help="Second line: subtitle (title) or role (lower_third)")
    ap.add_argument("--duration", type=float, default=4.0, help="Seconds (default 4)")
    ap.add_argument("--match", type=Path, help="Take size and fps from this video (use a rendered base)")
    ap.add_argument("--size", type=parse_size, help="WIDTHxHEIGHT (default 1920x1080)")
    ap.add_argument("--fps", default=None, help="Default 30, or --match's rate")
    ap.add_argument("--font", help="Main font file (default Segoe UI Bold)")
    ap.add_argument("--sub-font", help="Subtext font file (default Segoe UI)")
    ap.add_argument("--font-size", type=int, help="Main text px (default scales with the frame)")
    ap.add_argument("--brand", type=Path, help="brand.json: default colours and fonts")
    ap.add_argument("--color", type=parse_color, help="Main text (default brand text, #FFFFFF)")
    ap.add_argument("--sub-color", type=parse_color, help="Subtext (default brand muted, #D0D0D0)")
    ap.add_argument("--accent", type=parse_color,
                    help="Bar (lower_third/badge), subtext (title), box (callout). Default brand accent")
    ap.add_argument("--box", type=parse_color, default=None,
                    help="Background box, e.g. #000000B0. Default: none for title/callout, brand box otherwise")
    ap.add_argument("--position", help="top | center | bottom | X,Y (top-left px)")
    ap.add_argument("--intro", type=float, default=0.5, help="Entrance seconds")
    ap.add_argument("--outro", type=float, default=0.4, help="Fade-out seconds")
    ap.add_argument("--char-seconds", type=float, default=0.05, help="Typewriter seconds per character")
    args = ap.parse_args()

    if args.position and args.position not in ("top", "center", "bottom") \
            and not re.fullmatch(r"-?\d+,-?\d+", args.position):
        ap.error("--position must be top, center, bottom or X,Y")
    if args.duration <= args.intro + args.outro:
        ap.error("--duration must be longer than --intro + --outro")

    size, fps = (1920, 1080), "30"
    if args.match:
        size, fps = probe_size_fps(args.match)
    if args.size:
        size = args.size
    if args.fps:
        fps = args.fps

    # explicit flag > brand.json > built-in default
    brand = load_brand(args.brand) if args.brand else None
    colors = brand["colors"] if brand else {"text": "#FFFFFF", "muted": "#D0D0D0", "accent": "#FF5A00",
                                            "box": "#000000B0"}
    box = args.box
    if box is None:
        box = (0, 0, 0, 0) if args.style in ("title", "callout") else parse_color(colors["box"])
    font = args.font or (brand["fonts"]["heading"] if brand else None)
    sub_font = args.sub_font or (brand["fonts"]["body"] if brand else None) or args.font

    spec = Spec(
        style=args.style, text=args.text.replace("\\n", "\n"), subtext=args.subtext.replace("\\n", "\n"),
        size=size, fps=fps, duration=args.duration,
        font=resolve_font(font, DEFAULT_FONTS),
        sub_font=resolve_font(sub_font, DEFAULT_SUB_FONTS),
        font_size=args.font_size,
        color=args.color or parse_color(colors["text"]),
        sub_color=args.sub_color or parse_color(colors["muted"]),
        accent=args.accent or parse_color(colors["accent"]), box=box, position=args.position,
        intro=args.intro, outro=args.outro, char_s=args.char_seconds,
    )
    render(spec, args.output)
    print(f"{args.style} overlay -> {args.output}  ({size[0]}x{size[1]} @ {fps}, {args.duration:.2f}s, alpha)")


if __name__ == "__main__":
    main()
