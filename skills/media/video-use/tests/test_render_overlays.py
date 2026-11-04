import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


HELPERS = Path(__file__).parents[1] / "helpers"


def load(name):
    spec = importlib.util.spec_from_file_location(f"video_use_{name}", HELPERS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


render = load("render")
text_overlay = load("text_overlay")
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def pixel(video: Path, t: float, x: int, y: int) -> list[int]:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(video), "-frames:v", "1",
         "-f", "image2pipe", "-c:v", "png", "-pix_fmt", "rgba", "-"],
        capture_output=True, check=True,
    )
    return list(text_overlay.Image.open(io.BytesIO(out.stdout)).convert("RGBA").getpixel((x, y)))


class OverlayFilterTests(unittest.TestCase):
    def test_default_is_top_left_pts_shifted(self):
        parts = render.overlay_filters(1, {"start_in_output": 2.5, "duration": 3}, "[0:v]", "[v1]")
        self.assertEqual(parts[0], "[1:v]setpts=PTS-STARTPTS+2.5/TB[a1]")
        self.assertEqual(parts[1], "[0:v][a1]overlay=x='0':y='0':enable='between(t,2.500,5.500)'[v1]")

    def test_position_width_and_fades(self):
        ov = {"start_in_output": 1, "duration": 4, "x": "(W-w)/2", "y": "H-h-80",
              "width": 640, "fade_in": 0.3, "fade_out": 0.5}
        prep, comp = render.overlay_filters(2, ov, "[v1]", "[v2]")
        self.assertEqual(
            prep,
            "[2:v]scale=640:-2,format=yuva420p,fade=t=in:st=0:d=0.300:alpha=1,"
            "fade=t=out:st=3.500:d=0.500:alpha=1,setpts=PTS-STARTPTS+1.0/TB[a2]",
        )
        self.assertIn("overlay=x='(W-w)/2':y='H-h-80'", comp)

    def test_rejects_filtergraph_injection_in_position(self):
        for bad in ["0,drawtext=text=x", "0';", "main_w"]:
            with self.assertRaises(SystemExit):
                render.overlay_filters(1, {"start_in_output": 0, "duration": 1, "x": bad}, "[0:v]", "[v1]")

    def test_still_image_loops_for_duration(self):
        args = render.overlay_input_args({"duration": 2.5}, Path("logo.png"))
        self.assertEqual(args, ["-loop", "1", "-t", "2.500", "-i", "logo.png"])

    def test_vp9_webm_uses_libvpx_decoder(self):
        with patch.object(render, "_probe_video_codec", return_value="vp9"):
            args = render.overlay_input_args({"duration": 1}, Path("anim.webm"))
        self.assertEqual(args, ["-c:v", "libvpx-vp9", "-i", "anim.webm"])

    def test_plain_video_input(self):
        self.assertEqual(render.overlay_input_args({"duration": 1}, Path("a.mov")), ["-i", "a.mov"])


class TextOverlayUnitTests(unittest.TestCase):
    def test_parse_color(self):
        self.assertEqual(text_overlay.parse_color("#FF5A00"), (255, 90, 0, 255))
        self.assertEqual(text_overlay.parse_color("000000B0"), (0, 0, 0, 176))

    def test_typewriter_partial_block_keeps_full_size(self):
        font = text_overlay.ImageFont.truetype(str(text_overlay.resolve_font(None, text_overlay.DEFAULT_FONTS)), 40)
        full = text_overlay._text_block(["hello world"], font, (255, 255, 255, 255), align="left")
        part = text_overlay._text_block(["hello world"], font, (255, 255, 255, 255), align="left", limit=3)
        self.assertEqual(full.size, part.size)
        self.assertLess(part.getchannel("A").getbbox()[2], full.getchannel("A").getbbox()[2])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not on PATH")
class OverlayCompositeTests(unittest.TestCase):
    """End to end: a transparent overlay must leave the base visible around it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _base(self):
        base = self.tmp / "base.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=3",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(base)],
            check=True,
        )
        return base

    def _composite(self, base, overlay):
        out = self.tmp / "out.mp4"
        render.build_final_composite(base, [overlay], None, out, self.tmp)
        return out

    def test_vp9_alpha_webm_keeps_base_visible(self):
        frame = self.tmp / "f.png"
        img = text_overlay.Image.new("RGBA", (320, 180), (0, 0, 0, 0))
        text_overlay.ImageDraw.Draw(img).rectangle([140, 70, 180, 110], fill=(255, 0, 0, 255))
        img.save(frame)
        webm = self.tmp / "ov.webm"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", "30", "-i", str(frame), "-t", "2",
             "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", str(webm)],
            check=True,
        )
        out = self._composite(self._base(), {"file": str(webm), "start_in_output": 0.5, "duration": 2})
        corner, center = pixel(out, 1.0, 10, 10), pixel(out, 1.0, 160, 90)
        self.assertGreater(corner[2], 200, f"base hidden behind overlay: {corner}")
        self.assertGreater(center[0], 200, f"overlay missing: {center}")

    def test_text_overlay_styles_render_with_alpha(self):
        base = self._base()
        for style in text_overlay.STYLES:
            mov = self.tmp / f"{style}.mov"
            subprocess.run(
                [sys.executable, str(HELPERS / "text_overlay.py"), style, "Olá mundo",
                 "--subtext", "teste", "--size", "320x180", "--duration", "2", "-o", str(mov)],
                check=True, capture_output=True,
            )
            probe = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=pix_fmt,width,height",
                 "-of", "json", str(mov)], capture_output=True, text=True, check=True).stdout)["streams"][0]
            self.assertEqual((probe["pix_fmt"], probe["width"], probe["height"]), ("argb", 320, 180), style)
            self.assertEqual(pixel(mov, 1.2, 2, 2)[3], 0, f"{style}: corner not transparent")
            out = self._composite(base, {"file": str(mov), "start_in_output": 0, "duration": 2})
            self.assertGreater(pixel(out, 1.2, 2, 2)[2], 200, f"{style}: base hidden")


if __name__ == "__main__":
    unittest.main()
