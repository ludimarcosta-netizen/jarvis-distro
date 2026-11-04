"""Project visual identity: one `brand.json` that every generator reads.

Without it each insert invents its own look (the classic failure: first draft
in a style nobody asked for). Write `<edit>/brand.json` once per project,
confirm it with the user, then pass `--brand <edit>/brand.json` to
text_overlay.py, broll.py, captions.py and transition.py. Any key left out
falls back to DEFAULTS; explicit command-line flags still win.

    {
      "colors": {"text": "#FFFFFF", "accent": "#FF5A00", "muted": "#D0D0D0", "box": "#0F172AE0"},
      "fonts": {"heading": "C:/Windows/Fonts/segoeuib.ttf", "body": "C:/Windows/Fonts/segoeui.ttf",
                "caption_family": "Arial Black"},
      "logo": "C:/path/logo.png",
      "image_style": "cinematic photo, natural light, shallow depth of field"
    }

`caption_family` is a font *family name* (libass looks fonts up by name);
the other fonts are file paths (PIL needs the file).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULTS: dict = {
    "colors": {
        "text": "#FFFFFF",
        "accent": "#FF5A00",
        "muted": "#D0D0D0",
        "box": "#0F172AE0",
    },
    "fonts": {
        "heading": "C:/Windows/Fonts/segoeuib.ttf",
        "body": "C:/Windows/Fonts/segoeui.ttf",
        "caption_family": "Arial Black",
    },
    "logo": None,
    "image_style": "cinematic photo, natural light, shallow depth of field",
}


def load_brand(path: Path | str | None) -> dict:
    """DEFAULTS deep-merged with the file at `path` (if any)."""
    brand = json.loads(json.dumps(DEFAULTS))
    if not path:
        return brand
    p = Path(path)
    if not p.is_file():
        sys.exit(f"brand file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"brand file {p} is not valid JSON: {exc}")
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(brand.get(key), dict):
            brand[key].update(value)
        else:
            brand[key] = value
    return brand
