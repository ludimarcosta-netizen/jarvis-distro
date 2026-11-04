"""Download public model weights only; never runs downloaded code."""
import hashlib
import json
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "models"
BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
FILES = {
    "segmentation.tar.bz2": BASE + "/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
    "speaker.onnx": BASE + "/speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx",
    "test.wav": BASE + "/speaker-segmentation-models/0-four-speakers-zh.wav",
}

if __name__ == "__main__":
    ROOT.mkdir(exist_ok=True)
    manifest = {}
    for name, url in FILES.items():
        path = ROOT / name
        if not path.exists():
            print(f"Downloading {name}", flush=True)
            with urllib.request.urlopen(url, timeout=40) as response, path.with_suffix(".part").open("wb") as out:
                while block := response.read(1024 * 1024):
                    out.write(block)
            path.with_suffix(".part").replace(path)
        manifest[name] = {"url": url, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    with tarfile.open(ROOT / "segmentation.tar.bz2") as archive:
        member = next(m for m in archive.getmembers() if m.name.endswith("/model.onnx") and m.isfile())
        with archive.extractfile(member) as source:
            (ROOT / "segmentation.onnx").write_bytes(source.read())
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Models downloaded; hashes recorded locally.")
