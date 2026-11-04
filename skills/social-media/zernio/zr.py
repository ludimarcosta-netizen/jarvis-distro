"""Zernio API (redes sociais) sem OAuth/MCP.

Uso:
  zr.py contas
  zr.py perfis
  zr.py post --texto "..." --conta ID [--conta ID2] [--agendar 2026-10-06T09:00] [--midia URL ...] [--rascunho]
  zr.py status POST_ID
  zr.py api METODO /caminho [--json '{...}']   # chamada crua

Credencial: ZERNIO_KEY (sk_...) no ambiente ou no .env do Hermes. Sem --agendar nem --rascunho, publica agora.
Só biblioteca padrão.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://zernio.com/api/v1"
HERMES_ENV = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / ".env"
TIMEZONE = "America/Sao_Paulo"


class ZernioError(RuntimeError):
    pass


def credential() -> str:
    value = os.environ.get("ZERNIO_KEY", "")
    if not value and HERMES_ENV.exists():
        for line in HERMES_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("ZERNIO_KEY="):
                value = line.split("=", 1)[1].strip().strip("'\"")
    if not value:
        raise ZernioError("ZERNIO_KEY ausente. Salvar ZERNIO_KEY=sk_... no .env do Hermes.")
    return value


def call(method: str, path: str, body=None) -> dict:
    req = urllib.request.Request(
        API + path, method=method.upper(),
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {credential()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise ZernioError(f"Zernio respondeu {exc.code}: {exc.read().decode('utf-8', 'replace')[:400]}") from exc
    except urllib.error.URLError as exc:
        raise ZernioError(f"Sem conexão com a Zernio: {exc.reason}") from exc


def build_post(args) -> dict:
    accounts = [{"accountId": a} for a in args.conta]
    body = {"content": args.texto, "platforms": accounts}
    if args.midia:
        body["mediaItems"] = [{"url": u} for u in args.midia]
    if args.rascunho:
        body["isDraft"] = True
    elif args.agendar:
        body["scheduledFor"] = args.agendar
        body["timezone"] = TIMEZONE
    else:
        body["publishNow"] = True
    return body


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("contas")
    sub.add_parser("perfis")
    post = sub.add_parser("post")
    post.add_argument("--texto", required=True)
    post.add_argument("--conta", action="append", required=True)
    post.add_argument("--agendar")
    post.add_argument("--midia", action="append")
    post.add_argument("--rascunho", action="store_true")
    st = sub.add_parser("status")
    st.add_argument("post_id")
    raw = sub.add_parser("api")
    raw.add_argument("metodo")
    raw.add_argument("caminho")
    raw.add_argument("--json")
    args = p.parse_args()
    try:
        if args.cmd == "contas":
            out = call("GET", "/accounts")
        elif args.cmd == "perfis":
            out = call("GET", "/profiles")
        elif args.cmd == "post":
            out = call("POST", "/posts", build_post(args))
        elif args.cmd == "status":
            out = call("GET", f"/posts/{args.post_id}")
        else:
            out = call(args.metodo, args.caminho, json.loads(args.json) if args.json else None)
    except ZernioError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
