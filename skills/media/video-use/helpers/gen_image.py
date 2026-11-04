"""Generate B-roll stills from text prompts.

Providers:
    pollinations  free, no key (FLUX). Medium quality, public service with no
                  uptime guarantee. It stamps a small logo bottom-right; the
                  image is requested taller and that strip is cropped off.
    vertex        Gemini image on Vertex AI (default gemini-2.5-flash-image).
                  High quality, ~8 s/image, **billed to the GCP project in the
                  service-account file** (Jarvis: the prefeitura's project).
                  Use only when the user asked for it.

Images are cached: the same provider/model/prompt/aspect/seed is never paid
for twice. Each image gets a `<name>.json` sidecar recording how it was made.

Usage:
    python helpers/gen_image.py "a farmer checking soybean leaves at dawn" -o edit/broll/b01.png
    python helpers/gen_image.py --plan edit/broll_plan.json            # many, in parallel
    python helpers/gen_image.py "..." -o x.png --provider vertex --aspect 9:16

Plan file (a list; `aspect`, `provider`, `seed` optional per item):
    [{"out": "broll/b01.png", "prompt": "..."}, {"out": "broll/b02.png", "prompt": "...", "aspect": "1:1"}]
Relative `out` paths resolve against the plan file's directory.

Write prompts as photographs/scenes, never with words in the image (generators
garble text — put text in with text_overlay.py instead). `--brand` appends the
project's `image_style` so every still shares one look.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from brand import load_brand  # noqa: E402

ASPECTS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080), "4:3": (1440, 1080), "3:4": (1080, 1440)}
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
POLLINATIONS_LOGO_STRIP = 0.07  # share of height covered by the stamped logo
VERTEX_DEFAULT_MODEL = "gemini-2.5-flash-image"
JARVIS_PYTHON = os.environ.get(
    "VIDEO_USE_JARVIS_PYTHON",
    os.environ.get("VIDEO_USE_WHISPER_PYTHON",
                   str(Path.home() / "AppData/Local/hermes/jarvis/.venv311/Scripts/python.exe")),
)
HERMES_ENV = Path.home() / "AppData/Local/hermes/.env"
# never mention watermarks/logos: Vertex blocks those prompts as "requesting to remove watermarks"
NEGATIVE = "no text, no letters, no captions"


def cache_key(provider: str, model: str, prompt: str, aspect: str, seed: int) -> str:
    raw = json.dumps([provider, model, prompt, aspect, seed], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def full_prompt(prompt: str, style: str | None) -> str:
    parts = [prompt.strip().rstrip(".")]
    if style:
        parts.append(style.strip().rstrip("."))
    parts.append(NEGATIVE)
    return ". ".join(parts)


# -------- Pollinations --------------------------------------------------------


_pollinations_lock = threading.Lock()  # anonymous tier: one request at a time, parallel ones get HTTP 402


def gen_pollinations(prompt: str, aspect: str, seed: int, model: str) -> Image.Image:
    w, h = ASPECTS[aspect]
    # ask for a taller image, then crop the logo strip off the bottom
    req_h = int(h / (1 - POLLINATIONS_LOGO_STRIP))
    url = POLLINATIONS_URL.format(prompt=urllib.parse.quote(prompt, safe=""))
    params = {"width": w, "height": req_h, "seed": seed, "model": model, "nologo": "true"}
    last_error = ""
    for attempt in range(5):
        try:
            with _pollinations_lock:
                resp = requests.get(url, params=params, timeout=120)
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
                img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                strip = int(img.height * POLLINATIONS_LOGO_STRIP)
                return img.crop((0, 0, img.width, img.height - strip))
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"pollinations failed after retries: {last_error}")


# -------- Vertex (Gemini image) -----------------------------------------------


def _vertex_credentials_path() -> Path:
    value = os.environ.get("VERTEX_CREDENTIALS_PATH", "")
    if not value and HERMES_ENV.is_file():
        for line in HERMES_ENV.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("VERTEX_CREDENTIALS_PATH="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not value or not Path(value).is_file():
        sys.exit("vertex: VERTEX_CREDENTIALS_PATH not set (env or hermes .env) or file missing")
    return Path(value)


_TOKEN_SCRIPT = (
    "import json,sys\n"
    "from google.oauth2 import service_account\n"
    "import google.auth.transport.requests as tr\n"
    "c=service_account.Credentials.from_service_account_file(sys.argv[1],"
    "scopes=['https://www.googleapis.com/auth/cloud-platform'])\n"
    "c.refresh(tr.Request())\n"
    "print(json.dumps({'token':c.token,'project':c.project_id}))\n"
)
_vertex_auth: dict | None = None


def vertex_auth() -> dict:
    """Access token minted by the Jarvis venv (it has google-auth; this venv doesn't)."""
    global _vertex_auth
    if _vertex_auth is None:
        proc = subprocess.run([JARVIS_PYTHON, "-c", _TOKEN_SCRIPT, str(_vertex_credentials_path())],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            sys.exit(f"vertex: could not get an access token: {proc.stderr.strip()[-400:]}")
        _vertex_auth = json.loads(proc.stdout)
    return _vertex_auth


def gen_vertex(prompt: str, aspect: str, seed: int, model: str) -> Image.Image:
    auth = vertex_auth()
    url = (f"https://aiplatform.googleapis.com/v1/projects/{auth['project']}/locations/global/"
           f"publishers/google/models/{model}:generateContent")
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"], "seed": seed,
                             "imageConfig": {"aspectRatio": aspect}},
    }
    last_error = ""
    for attempt in range(3):
        resp = requests.post(url, json=body, timeout=180,
                             headers={"Authorization": f"Bearer {auth['token']}"})
        if resp.status_code == 200:
            data = resp.json()
            for cand in data.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    if "inlineData" in part:
                        return Image.open(io.BytesIO(base64.b64decode(part["inlineData"]["data"]))).convert("RGB")
            # blocked by safety filters or text-only answer: retrying won't help
            raise RuntimeError(
                f"vertex returned no image (finish: {[c.get('finishReason') for c in data.get('candidates', [])]}, "
                f"feedback: {data.get('promptFeedback')}). Rephrase the prompt (safety filters also refuse "
                f"real people and some minors scenes) or use pollinations")
        last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
        if resp.status_code not in (429, 500, 503):
            break
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"vertex failed: {last_error}")


PROVIDERS = {
    "pollinations": (gen_pollinations, "flux"),
    "vertex": (gen_vertex, VERTEX_DEFAULT_MODEL),
}


def fit(img: Image.Image, aspect: str) -> Image.Image:
    """Cover-crop to the exact aspect, then scale to the target size."""
    w, h = ASPECTS[aspect]
    scale = max(w / img.width, h / img.height)
    rw, rh = round(img.width * scale), round(img.height * scale)
    img = img.resize((rw, rh), Image.LANCZOS)
    left, top = (rw - w) // 2, (rh - h) // 2
    return img.crop((left, top, left + w, top + h))


def generate(prompt: str, out: Path, provider: str, aspect: str, seed: int,
             model: str | None, style: str | None) -> str:
    gen, default_model = PROVIDERS[provider]
    model = model or default_model
    text = full_prompt(prompt, style)
    key = cache_key(provider, model, text, aspect, seed)
    sidecar = out.with_suffix(".json")
    if out.exists() and sidecar.exists():
        try:
            if json.loads(sidecar.read_text(encoding="utf-8")).get("cache_key") == key:
                return f"cached  {out}"
        except json.JSONDecodeError:
            pass
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    img = fit(gen(text, aspect, seed, model), aspect)
    img.save(out)
    sidecar.write_text(json.dumps({
        "prompt": prompt, "full_prompt": text, "provider": provider, "model": model,
        "aspect": aspect, "seed": seed, "cache_key": key,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"{provider:12s} {out}  ({time.time() - t0:.1f}s)"


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate B-roll stills from prompts")
    ap.add_argument("prompt", nargs="?", help="Scene description (omit with --plan)")
    ap.add_argument("-o", "--output", type=Path, help="Output .png (single prompt)")
    ap.add_argument("--plan", type=Path, help="JSON list of {out, prompt[, aspect, provider, seed]}")
    ap.add_argument("--provider", choices=sorted(PROVIDERS), default="pollinations")
    ap.add_argument("--model", help="Provider model (default: flux / gemini-2.5-flash-image)")
    ap.add_argument("--aspect", choices=sorted(ASPECTS), default="16:9")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--brand", type=Path, help="brand.json: appends its image_style to every prompt")
    ap.add_argument("--no-style", action="store_true", help="Don't append the brand image_style")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    style = None if args.no_style else (load_brand(args.brand)["image_style"] if args.brand else None)

    if args.plan:
        items = json.loads(args.plan.read_text(encoding="utf-8"))
        base = args.plan.resolve().parent
        jobs = []
        for it in items:
            out = Path(it["out"])
            jobs.append((it["prompt"], out if out.is_absolute() else base / out,
                         it.get("provider", args.provider), it.get("aspect", args.aspect),
                         int(it.get("seed", args.seed)), it.get("model", args.model)))
    elif args.prompt and args.output:
        jobs = [(args.prompt, args.output, args.provider, args.aspect, args.seed, args.model)]
    else:
        ap.error("give a prompt and -o, or --plan")

    for j in jobs:
        if j[2] not in PROVIDERS:
            ap.error(f"unknown provider {j[2]!r}")
        if j[3] not in ASPECTS:
            ap.error(f"unknown aspect {j[3]!r}")

    failures = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [(j, pool.submit(generate, j[0], j[1], j[2], j[3], j[4], j[5], style)) for j in jobs]
        for j, fut in futures:
            try:
                print(fut.result(), flush=True)
            except Exception as exc:  # report every failed image, keep the rest
                failures += 1
                print(f"FAILED  {j[1]}: {exc}", file=sys.stderr, flush=True)
    if failures:
        sys.exit(f"{failures} of {len(jobs)} image(s) failed")


if __name__ == "__main__":
    main()
