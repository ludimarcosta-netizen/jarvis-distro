import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HELPERS = Path(__file__).parents[1] / "helpers"
sys.path.insert(0, str(HELPERS))  # helpers import each other (brand, render, text_overlay)


def load(name):
    spec = importlib.util.spec_from_file_location(f"video_use_{name}", HELPERS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


render = load("render")
captions = load("captions")
gen_image = load("gen_image")
broll = load("broll")
sfx = load("sfx")
brand = load("brand")
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe_wh(path: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    w, h = out.split(",")
    return int(w), int(h)


class BrandTests(unittest.TestCase):
    def test_partial_file_keeps_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "brand.json"
            p.write_text(json.dumps({"colors": {"accent": "#00FF00"}}), encoding="utf-8")
            b = brand.load_brand(p)
        self.assertEqual(b["colors"]["accent"], "#00FF00")
        self.assertEqual(b["colors"]["text"], brand.DEFAULTS["colors"]["text"])
        self.assertEqual(b["fonts"], brand.DEFAULTS["fonts"])


class TimelineAndSfxTests(unittest.TestCase):
    EDL = {"ranges": [{"source": "a", "start": 10, "end": 12.5}, {"source": "a", "start": 20, "end": 21}]}

    def test_output_timeline_is_contiguous(self):
        rows = render.output_timeline(self.EDL)
        self.assertEqual([(r["output_start"], r["output_end"]) for r in rows], [(0.0, 2.5), (2.5, 3.5)])

    def test_sfx_filters_delay_and_mix_under_dialogue(self):
        parts = render.sfx_filters([{"file": "w.wav", "at": 2.35, "gain_db": -8}, {"file": "p.wav", "at": 0}], 3)
        self.assertIn("[3:a]", parts[0])
        self.assertIn("adelay=2350:all=1,volume=-8.0dB[sfx0]", parts[0])
        self.assertIn("volume=-6.0dB[sfx1]", parts[1])
        self.assertEqual(parts[-1], "[0:a][sfx0][sfx1]amix=inputs=3:duration=first:normalize=0[aout]")

    def test_negative_sfx_time_rejected(self):
        with self.assertRaises(SystemExit):
            render.sfx_filters([{"file": "w.wav", "at": -0.1}], 1)

    def test_zoom_out_of_range_rejected(self):
        for z in (1.0, 0.9, 2.5):
            with self.assertRaises(ValueError):
                render.punch_in_filter(z)


class CaptionTests(unittest.TestCase):
    CHUNKS = [[("ação", 0.0, 0.4), ("nova,", 0.5, 0.9)], [("hoje", 1.1, 1.5)]]

    def test_one_event_per_word_with_active_highlight(self):
        events = captions.build_events(self.CHUNKS, "&H0000C4FF", upper=True, pop=False)
        self.assertEqual(len(events), 3)
        self.assertIn("{\\c&H0000C4FF\\fscx112\\fscy112}AÇÃO{\\r} NOVA", events[0])
        self.assertIn("AÇÃO {\\c&H0000C4FF\\fscx112\\fscy112}NOVA{\\r}", events[1])  # trailing comma stripped

    def test_events_are_back_to_back_and_hold_short_pauses(self):
        events = captions.build_events(self.CHUNKS, "&H00FFFFFF", upper=False, pop=False)
        times = [e.split(",")[1:3] for e in events]
        self.assertEqual(times[0][1], times[1][0])          # no gap between words of a chunk
        self.assertEqual(times[1][1], "0:00:01.10")         # held until the next chunk (0.2 s pause)
        self.assertEqual(times[2][1], "0:00:02.00")         # last chunk holds 0.5 s

    def test_ass_color_is_bgr_with_inverted_alpha(self):
        self.assertEqual(captions.ass_color((255, 196, 0, 255)), "&H0000C4FF")
        self.assertEqual(captions.ass_color((0, 0, 0, 0)), "&HFF000000")


class GenImageTests(unittest.TestCase):
    def test_fit_returns_exact_aspect_size(self):
        img = gen_image.Image.new("RGB", (1024, 980))
        self.assertEqual(gen_image.fit(img, "16:9").size, (1920, 1080))
        self.assertEqual(gen_image.fit(img, "9:16").size, (1080, 1920))

    def test_cache_key_changes_with_any_input(self):
        base = gen_image.cache_key("pollinations", "flux", "p", "16:9", 7)
        self.assertNotEqual(base, gen_image.cache_key("vertex", "flux", "p", "16:9", 7))
        self.assertNotEqual(base, gen_image.cache_key("pollinations", "flux", "p", "16:9", 8))

    def test_prompt_forbids_text(self):
        self.assertIn("no text", gen_image.full_prompt("a bus", "warm light"))


class BrollTests(unittest.TestCase):
    def test_ken_burns_stays_on_image(self):
        img = broll.Image.new("RGB", (1344, 768), (200, 10, 10))
        for motion in broll.MOTIONS:
            frames = list(broll.full_frames(img, (320, 180), 5, motion))
            for f in frames:
                px = broll.Image.frombytes("RGB", (320, 180), f).getpixel((0, 0))
                self.assertEqual(px, (200, 10, 10), f"{motion}: window left the image")


class SfxTests(unittest.TestCase):
    def test_every_effect_is_normalized_stereo(self):
        rng = sfx.np.random.default_rng(1)
        for name, fn in sfx.EFFECTS.items():
            sig = sfx.normalize(fn(sfx.DEFAULT_DURATION[name], rng))
            self.assertEqual(sig.shape[1], 2, name)
            self.assertAlmostEqual(float(abs(sig).max()), 10 ** (-3 / 20), places=4, msg=name)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not on PATH")
class PunchInConcatTests(unittest.TestCase):
    """A zoomed segment must be exactly the size of an unzoomed one (lossless concat)."""

    def test_zoomed_segment_matches_size(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for w, h in ((1280, 720), (1080, 1920), (1918, 1078)):
                src = d / f"s{w}.mp4"
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r=30:d=1",
                                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1",
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
                plain, zoomed = d / f"p{w}.mp4", d / f"z{w}.mp4"
                render.extract_segment(src, 0, 0.5, "", plain, draft=True, rate="30")
                for z in (1.1, 1.15, 1.333):
                    render.extract_segment(src, 0, 0.5, "", zoomed, draft=True, rate="30",
                                           zoom_filter=render.punch_in_filter(z))
                    self.assertEqual(probe_wh(zoomed), probe_wh(plain), f"{w}x{h} zoom {z}")

    def test_sfx_only_composite_copies_video(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base = d / "base.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=2",
                            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(base)], check=True)
            sfx.make("pop", d / "pop.wav", None, 1)
            out = d / "out.mp4"
            render.build_final_composite(base, [], None, out, d, [{"file": "pop.wav", "at": 1.0, "gain_db": 0}])
            vol = subprocess.run(["ffmpeg", "-ss", "0.98", "-t", "0.1", "-i", str(out), "-af", "volumedetect",
                                  "-f", "null", "-"], capture_output=True, text=True).stderr
            self.assertNotIn("max_volume: -91", vol)  # silence base: the pop must be there
            self.assertIn("max_volume", vol)


if __name__ == "__main__":
    unittest.main()
