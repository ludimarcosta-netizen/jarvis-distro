"""Abre na hora um programa instalado neste PC, sem IA no meio.

Uso:
  abrir.py "spotify"                 abre (ou lista opções se for ambíguo)
  abrir.py --procurar "adobe"        só lista o que combina
  abrir.py --ensinar fone "Vincular ao Celular"   cria/atualiza atalho falado
  abrir.py --buscar youtube "receita de pão"      abre a busca (youtube|google|maps|spotify)
  abrir.py --atualizar               refaz a lista de programas (≈15 s)

Base: lista de programas do menu Iniciar (Get-StartApps) em cache + atalhos.json.
Abre por shell:AppsFolder, que funciona para programas comuns e da Microsoft Store.
Usado também pela voz do Jarvis (plugin gemini-live-bridge, função abrir_programa).
"""
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "apps_cache.json"
ATALHOS = ROOT / "atalhos.json"
CACHE_MAX_AGE_S = 12 * 3600
_PS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
_NOT_APPS = re.compile(r"(?i)\b(desinstalar|uninstall|leia-me|readme|manual|ajuda|help|release notes|license)\b")
_FILLER = {"o", "a", "os", "as", "programa", "aplicativo", "app", "do", "da", "de", "meu", "minha", "pra", "para", "ai", "aí"}
_lock = threading.Lock()
_refreshing = threading.Event()


def fold(text: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFKD", str(text).casefold()) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


def _scan() -> list:
    """Get-StartApps é lento (≈15 s): só aqui, nunca no caminho do pedido."""
    out = subprocess.run([str(_PS), "-NoProfile", "-NonInteractive", "-Command",
                          "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"],
                         capture_output=True, timeout=120, creationflags=0x08000000)
    data = json.loads(out.stdout.decode("utf-8", "replace") or "[]")
    apps = [{"name": a["Name"], "id": a["AppID"]} for a in (data if isinstance(data, list) else [data])
            if a.get("Name") and a.get("AppID") and not _NOT_APPS.search(a["Name"])]
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"at": time.time(), "apps": apps}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CACHE)
    return apps


def apps(refresh: bool = False) -> list:
    with _lock:
        if refresh or not CACHE.exists():
            return _scan()
        data = json.loads(CACHE.read_text(encoding="utf-8"))
    if time.time() - data.get("at", 0) > CACHE_MAX_AGE_S:
        refresh_in_background()
    return data["apps"]


def refresh_in_background() -> None:
    if _refreshing.is_set():
        return
    _refreshing.set()

    def run():
        try:
            with _lock:
                _scan()
        except Exception as exc:  # noqa: BLE001 — lista antiga continua valendo
            print(f"[abrir] lista de programas não atualizada: {exc}", file=sys.stderr)
        finally:
            _refreshing.clear()
    threading.Thread(target=run, daemon=True, name="abrir-refresh").start()


def atalhos() -> dict:
    try:
        return {fold(k): v for k, v in json.loads(ATALHOS.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError):
        return {}


def ensinar(apelido: str, destino: str) -> str:
    data = json.loads(ATALHOS.read_text(encoding="utf-8")) if ATALHOS.exists() else {}
    data[apelido.strip()] = destino.strip()
    ATALHOS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"Atalho salvo: “{apelido}” abre {destino}."


def _words(text: str) -> list:
    return [w for w in fold(text).split() if w not in _FILLER]


def find(query: str, catalog: list) -> list:
    """Melhores candidatos, do mais confiável ao menos: nome exato > todas as palavras >
    começo de palavra > parecido. Devolve só o primeiro nível que achar algo."""
    words = _words(query)
    if not words:
        return []
    q = " ".join(words)
    named = [(fold(a["name"]), a) for a in catalog]
    for test in (lambda n: n == q,
                 lambda n: all(w in n.split() for w in words),
                 lambda n: all(any(t.startswith(w) for t in n.split()) for w in words)):
        hits = [a for n, a in named if test(n)]
        if hits:
            return hits
    close = difflib.get_close_matches(q, [n for n, _ in named], n=5, cutoff=0.8)
    if close:
        return [a for n, a in named if n in close]

    def score(n: str) -> float:  # erro de transcrição: "fotoshop" ≈ "photoshop"
        best = [max((difflib.SequenceMatcher(None, w, t).ratio() for t in n.split()), default=0) for w in words]
        return min(best)
    scored = sorted(((score(n), a) for n, a in named), key=lambda s: -s[0])
    top = scored[0][0] if scored else 0
    return [a for s, a in scored if s >= 0.75 and s >= top - 0.03][:6]


def _launch(target: str, is_app_id: bool) -> None:
    if is_app_id:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"], creationflags=0x08000000)
    else:
        os.startfile(target)  # endereço (ms-phone:, https:) ou caminho de pasta/arquivo


def open_app(query: str) -> dict:
    """{'ok': bool, 'mensagem': str, 'opcoes': [...]} — mensagem pronta para ser falada."""
    query = str(query or "").strip()
    alias = atalhos().get(" ".join(_words(query))) or atalhos().get(fold(query))
    if alias and (re.match(r"^[a-z][a-z0-9+.-]*:", alias, re.I) or "\\" in alias or "/" in alias
                  or alias.casefold().endswith(".exe")):
        _launch(alias, False)
        return {"ok": True, "mensagem": f"Abri {query}.", "opcoes": []}
    hits = find(alias or query, apps())
    if not hits:
        refresh_in_background()  # pode ser programa recém-instalado; nunca faz o senhor esperar a lista
        return {"ok": False, "mensagem": f"Não encontrei nenhum programa chamado “{query}” neste computador. "
                "Se acabou de instalar, peça de novo em meio minuto.", "opcoes": []}
    if len(hits) > 1:
        names = [a["name"] for a in hits[:6]]
        return {"ok": False, "mensagem": "Encontrei mais de um: " + "; ".join(names) + ". Qual deles?",
                "opcoes": names}
    _launch(hits[0]["id"], True)
    return {"ok": True, "mensagem": f"Abri {hits[0]['name']}.", "opcoes": []}


SEARCH_URLS = {
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "spotify": "spotify:search:{q}",  # abre a busca dentro do app do Spotify
}
_SEARCH_NAMES = {"youtube": "YouTube", "google": "Google", "maps": "Google Maps", "spotify": "Spotify"}


def play_spotify(query: str) -> dict:
    """Open the requested search; selecting a track requires the user or an API."""
    result = search("spotify", query)
    if result["ok"]:
        result["mensagem"] += " Escolha a faixa no Spotify; a reprodução ainda não foi confirmada."
    return result


def search(where: str, query: str) -> dict:
    """Abre a página de resultados (não escolhe resultado: isso é trabalho do Hermes)."""
    from urllib.parse import quote, quote_plus
    key = fold(where).replace(" ", "")
    key = {"googlemaps": "maps", "mapa": "maps", "mapas": "maps"}.get(key, key)
    query = str(query or "").strip()
    if key not in SEARCH_URLS or not query:
        return {"ok": False, "mensagem": "Busca rápida só no YouTube, Google, Maps ou Spotify, com o que procurar.",
                "opcoes": []}
    encoded = quote(query) if key in ("maps", "spotify") else quote_plus(query)
    _launch(SEARCH_URLS[key].format(q=encoded), False)
    return {"ok": True, "mensagem": f"Abri a busca por “{query}” no {_SEARCH_NAMES[key]}.", "opcoes": []}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--atualizar":
        print(f"{len(apps(refresh=True))} programas na lista.")
        return 0
    if args[0] == "--ensinar" and len(args) == 3:
        print(ensinar(args[1], args[2]))
        return 0
    if args[0] == "--buscar" and len(args) >= 3:
        result = search(args[1], " ".join(args[2:]))
        print(result["mensagem"])
        return 0 if result["ok"] else 2
    if args[0] == "--tocar" and len(args) >= 3:
        if args[1].lower() == "spotify":
            result = play_spotify(" ".join(args[2:]))
            print(result["mensagem"])
            return 0 if result["ok"] else 2
        else:
            result = search(args[1], " ".join(args[2:]))
            print(result["mensagem"])
            return 0 if result["ok"] else 2
    if args[0] == "--procurar" and len(args) == 2:
        print("\n".join(a["name"] for a in find(args[1], apps())) or "Nada encontrado.")
        return 0
    result = open_app(" ".join(args))
    print(result["mensagem"])
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
