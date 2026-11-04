"""Unambiguous media shortcuts. No text becomes shell code or a chosen video."""
import re

from local_files import fold
from local_media import MediaSession

SEARCH = re.compile(r"^(?:jarvis[, ]+)?(?:procura|procure|busca|busque|pesquisa|pesquise)\s+(?:por\s+)?(.+?)\s+no\s+youtube[.!?]*$", re.IGNORECASE)
OPEN = re.compile(r"^(?:jarvis[, ]+)?(?:abra|abre)\s+(?:(?:o|esse|este)\s+)?(?:(primeiro|segundo|terceiro|[1-5])\s+)?video[.!?]*$")
COMMANDS = {"pause o spotify": "pause", "pausa o spotify": "pause", "continue o spotify": "play",
            "retome o spotify": "play", "proxima faixa no spotify": "next", "faixa anterior no spotify": "previous"}
PLAY = re.compile(r"^(?:jarvis[, ]+)?(?:toca|toque|tocar|coloca|coloque|bota|bote|ponha|p[oõ]e)\s+(.+?)\s+no\s+spotify[.!?]*$",
                  re.IGNORECASE)
STEP = re.compile(r"^(?:jarvis[, ]+)?(sobe|suba|aumenta|aumente|abaixa|abaixe|baixa|baixe|diminui|diminua)\s+"
                  r"(?:um\s+pouco\s+)?o\s+volume\s+do\s+spotify(?:\s+um\s+pouco)?$")
VOLUME = re.compile(r"^(?:jarvis[, ]+)?(?:(?:coloque|coloca|ponha|poe|deixe|deixa|ajuste|ajusta)\s+o\s+)?"
                    r"volume\s+do\s+spotify\s+(?:em|para|no|a)?\s*(\d{1,3})\s*(?:%|por\s*cento)?$")


def spotify_ref(media: MediaSession) -> str | None:
    sessions = media.dispatch("media.status", {})
    matches = [row for row in sessions.get("sessions", []) if row["app"].casefold() == "spotify.exe"
               or row["app"].casefold().startswith("spotifyab.spotifymusic_")]
    return matches[0]["ref"] if len(matches) == 1 else None


def try_command(text: str, media: MediaSession) -> str | None:
    if not isinstance(text, str) or len(text) > 220:
        return None
    normalized = fold(text.strip().rstrip(".!?"))
    search = SEARCH.fullmatch(text.strip())
    if search:
        query = search.group(1)
        if re.search(r"[,;\n]|\s(?:e|depois|entao)\s", fold(query)):
            return None
        result = media.dispatch("videos.search", {"query": query})
        lines = [f"{n}. {row['title']} — {row['channel']}" for n, row in enumerate(result["results"], 1)]
        return ("Vídeos encontrados:\n" + "\n".join(lines) + "\nPeça: abra o primeiro vídeo.") if lines else result.get("message", "Não encontrei vídeos. Use a busca no navegador.")
    play = PLAY.fullmatch(text.strip())
    if play:
        # " e " fica de fora do bloqueio: nomes de dupla ("Chitãozinho e Xororó") são comuns.
        if re.search(r"[,;\n]|\s(?:depois|entao)\s", fold(play.group(1))) or fold(text).startswith(("nao ", "jarvis nao")):
            return None
        try:
            result = media.dispatch("spotify.play", {"query": play.group(1)})
        except ValueError as exc:
            return str(exc)
        return result.get("message") or result.get("error", "O Spotify não confirmou.")
    opened = OPEN.fullmatch(normalized)
    try:
        if opened:
            ordinal = opened.group(1)
            if ordinal is None:
                return media.dispatch("videos.open", {})["message"]
            number = {"primeiro": 1, "segundo": 2, "terceiro": 3}.get(ordinal)
            return media.dispatch("videos.open", {"number": number or int(ordinal)})["message"]
        command = COMMANDS.get(normalized)
        volume = VOLUME.fullmatch(normalized)
        step = STEP.fullmatch(normalized)
        if volume and int(volume.group(1)) > 100:
            return "O volume vai de 0 a 100."
        if command or volume or step:
            ref = spotify_ref(media)
            if not ref:
                return "Não encontrei uma única sessão do Spotify. Abra o aplicativo e confira o painel Mídia."
            if step:
                up = step.group(1).startswith(("sob", "sub", "aument"))
                result = media.dispatch("media.volume", {"ref": ref, "step": 15 if up else -15})
            elif volume:
                result = media.dispatch("media.volume", {"ref": ref, "level": int(volume.group(1))})
            else:
                result = media.dispatch("media.command", {"ref": ref, "command": command})
            return result.get("message") or result.get("error", "O aplicativo não confirmou o comando.")
    except (ValueError, OSError):
        return "A referência de mídia venceu ou está indisponível. Atualize a busca ou o painel Mídia."
    return None
