"""Mostra o QR code da instância e espera o celular conectar.

Uso:  python sistema/conectar.py [instância]   (padrão: jarvis)
Abre qr.html no navegador; a página se atualiza sozinha até a conexão abrir.
WhatsApp no celular > Aparelhos conectados > Conectar um aparelho > ler o QR.
"""
import base64
import json
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8090"
OUT = ROOT / "qr"


def api_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AUTHENTICATION_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("AUTHENTICATION_API_KEY ausente em sistema/.env")


def get(path: str, key: str) -> dict:
    req = urllib.request.Request(URL + path, headers={"apikey": key})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def page(body: str) -> None:
    (OUT / "qr.html").write_text(
        "<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5>"
        "<title>Conectar WhatsApp</title><body style='font-family:sans-serif;text-align:center;padding:24px'>"
        f"{body}</body>", encoding="utf-8")


def main() -> None:
    instance = sys.argv[1] if len(sys.argv) > 1 else "jarvis"
    key = api_key()
    OUT.mkdir(exist_ok=True)
    opened = False
    deadline = time.time() + 600
    while time.time() < deadline:
        state = get(f"/instance/connectionState/{instance}", key).get("instance", {}).get("state")
        if state == "open":
            page("<h2>Conectado ✔</h2><p>Pode fechar esta página.</p>")
            (OUT / "qr.png").unlink(missing_ok=True)
            print("Conectado.")
            return
        qr = get(f"/instance/connect/{instance}", key).get("base64", "")
        if qr:
            (OUT / "qr.png").write_bytes(base64.b64decode(qr.split(",", 1)[-1]))
            page(f"<h2>Leia com o WhatsApp do celular</h2><p>Aparelhos conectados → Conectar um aparelho</p>"
                 f"<img src='qr.png?{int(time.time())}' width=320><p>Estado: {state}</p>")
        if not opened:
            webbrowser.open((OUT / "qr.html").as_uri())
            opened = True
        time.sleep(20)
    page("<h2>Tempo esgotado</h2><p>Rode o script de novo.</p>")
    raise SystemExit("Tempo esgotado sem conexão.")


if __name__ == "__main__":
    main()
