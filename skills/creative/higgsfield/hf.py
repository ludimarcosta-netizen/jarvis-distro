"""Gera imagem e vídeo na Higgsfield API (paga, por crédito) e baixa o resultado.

Uso:
  hf.py imagem --prompt "..." [--proporcao 16:9] [--resolucao 2K|4K] [--quantidade 1]
  hf.py video  --prompt "..." [--imagem URL_PUBLICA] [--duracao 5|10] [--modelo kling|hailuo]
  hf.py status REQUEST_ID

Credencial: HF_KEY="id:segredo" no ambiente ou no .env do Hermes. Resultado em
%USERPROFILE%/Pictures/Higgsfield (ou HF_OUT). Só biblioteca padrão.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API = "https://api.higgsfield.ai"
HERMES_ENV = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / ".env"
OUT = Path(os.environ.get("HF_OUT", Path.home() / "Pictures" / "Higgsfield"))
VIDEO_MODELS = {
    ("kling", False): "/kling-video/v2.5-turbo/pro/text-to-video",
    ("kling", True): "/kling-video/v2.5-turbo/pro/image-to-video",
    ("hailuo", False): "/minimax/hailuo-2.3/standard/text-to-video",
    ("hailuo", True): "/minimax/hailuo-2.3/standard/image-to-video",
}
WAIT_S = 900


class HiggsfieldError(RuntimeError):
    pass


def credential() -> str:
    value = os.environ.get("HF_KEY", "")
    if not value and HERMES_ENV.exists():
        for line in HERMES_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("HF_KEY="):
                value = line.split("=", 1)[1].strip().strip("'\"")
    if ":" not in value:
        raise HiggsfieldError("HF_KEY ausente. O senhor precisa criar a chave em console.higgsfield.ai e "
                              "salvar HF_KEY=id:segredo no .env do Hermes.")
    return value


def call(url: str, body=None) -> dict:
    req = urllib.request.Request(url if url.startswith("http") else API + url,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Authorization": f"Key {credential()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise HiggsfieldError(f"Higgsfield respondeu {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from exc
    except urllib.error.URLError as exc:
        raise HiggsfieldError(f"Sem conexão com a Higgsfield: {exc.reason}") from exc


def wait(request: dict) -> dict:
    deadline = time.time() + WAIT_S
    status_url = request.get("status_url") or f"/requests/{request['request_id']}/status"
    while time.time() < deadline:
        result = call(status_url)
        if result["status"] in ("completed", "failed", "nsfw", "canceled"):
            return result
        time.sleep(5)
    raise HiggsfieldError(f"Ainda processando após {WAIT_S // 60} min. Consulte depois: hf.py status {request['request_id']}")


def download(result: dict) -> list:
    urls = [m["url"] for m in result.get("images") or [] if m.get("url")]
    if (result.get("video") or {}).get("url"):
        urls.append(result["video"]["url"])
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = []
    for i, url in enumerate(urls, 1):
        ext = Path(url.split("?")[0]).suffix or ".bin"
        path = OUT / f"{stamp}-{result['request_id'][:8]}-{i}{ext}"
        with urllib.request.urlopen(url, timeout=120) as resp:
            path.write_bytes(resp.read())
        saved.append(str(path))
    return saved


def finish(result: dict) -> None:
    if result["status"] != "completed":
        reason = {"nsfw": "conteúdo bloqueado pelo filtro", "canceled": "cancelado"}.get(result["status"], "")
        raise HiggsfieldError(f"Geração {result['status']} {reason} {result.get('error') or ''}".strip())
    for path in download(result):
        print(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Higgsfield: imagem e vídeo")
    sub = parser.add_subparsers(dest="cmd", required=True)
    img = sub.add_parser("imagem")
    img.add_argument("--prompt", required=True)
    img.add_argument("--proporcao", default="4:3",
                     choices=["1:1", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "16:9", "9:16", "21:9"])
    img.add_argument("--resolucao", default="2K", choices=["2K", "4K"])
    img.add_argument("--quantidade", type=int, default=1, choices=range(1, 5))
    vid = sub.add_parser("video")
    vid.add_argument("--prompt", required=True)
    vid.add_argument("--imagem", help="URL pública da imagem inicial (opcional)")
    vid.add_argument("--duracao", type=int, choices=[5, 6, 10], help="kling: 5 ou 10; hailuo: 6 ou 10")
    vid.add_argument("--modelo", default="kling", choices=["kling", "hailuo"])
    st = sub.add_parser("status")
    st.add_argument("request_id")
    args = parser.parse_args()
    try:
        if args.cmd == "imagem":
            request = call("/higgsfield-ai/soul/standard", {"prompt": args.prompt, "aspect_ratio": args.proporcao,
                                                            "resolution": args.resolucao,
                                                            "num_images": args.quantidade})
        elif args.cmd == "video":
            durations = {"kling": (5, 10), "hailuo": (6, 10)}[args.modelo]
            duration = args.duracao or durations[0]
            if duration not in durations:
                raise HiggsfieldError(f"Duração {duration}s não existe no modelo {args.modelo}: use {durations}.")
            body = {"prompt": args.prompt, "duration": duration}
            if args.imagem:
                body["image_url"] = args.imagem
            request = call(VIDEO_MODELS[(args.modelo, bool(args.imagem))], body)
        else:
            request = {"request_id": args.request_id}
        print(f"Pedido {request['request_id']} enviado; aguardando...", file=sys.stderr)
        finish(wait(request))
    except HiggsfieldError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
