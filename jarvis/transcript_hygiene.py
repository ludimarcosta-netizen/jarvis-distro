"""
Higiene da transcrição: o que não é fala real da sala não entra nas reuniões.

- Alucinações conhecidas do Whisper (legendas de vídeo do treino, eco do antigo
  initial_prompt do Jarvis) são descartadas.
- Eco do PC: som que o computador toca (vídeo, chamada) entra pelo áudio do sistema
  (loopback) e, pela caixa de som, também pelo microfone. A cópia do microfone é
  descartada — ou marcada como ignorada, se chegou antes da do sistema.

Usado ao vivo (ambient_engine) e na limpeza do histórico (jarvis.py limpar-transcricoes).
"""
import re
import threading
import time
import unicodedata
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

# Frases que o Whisper "inventa" em ruído/silêncio, e o eco do prompt antigo do Jarvis.
_HALLUCINATIONS = [
    (r"transcri[çc][ãa]o fiel de conversa", "eco do prompt antigo"),
    (r"nomes pr[óo]prios frequentes", "eco do prompt antigo"),
    (r"^\W*(j\.?a\.?r\.?v\.?i\.?s\.?\W*,?\s*)+(__JARVIS_USER_LOWER__\W*,?\s*)?(hermes\W*,?\s*)?(gemini)?\W*$", "eco do prompt antigo"),
    (r"j\.?a\.?r\.?v\.?i\.?s\.?\W+__JARVIS_USER_LOWER__\W+hermes", "eco do prompt antigo"),
    (r"amara\.org", "alucinação do Whisper"),
    (r"^\W*(transcri[çc][ãa]o e )?legendas? (pela|por)\b", "alucinação do Whisper"),
    (r"obrigad[oa] por assistir", "alucinação do Whisper"),
    (r"inscreva-se no canal|deixe (o )?seu like|ative o sininho", "alucinação do Whisper"),
    (r"^\W*(legenda|legendas)\W*$", "alucinação do Whisper"),
]
_HALLUCINATION_RE = [(re.compile(p, re.IGNORECASE), reason) for p, reason in _HALLUCINATIONS]

ECHO_WINDOW_S = 15.0       # mesmo som pelo sistema e pelo microfone chega com poucos segundos de diferença
ECHO_MIN_WORDS = 3         # trechos curtos demais não dão para comparar com segurança
ECHO_MIN_OVERLAP = 0.5     # fração das palavras do trecho do microfone presentes no do sistema


# Falas inteiras que o Whisper devolve em ruído/silêncio e que não dizem nada à ata: despedida solta,
# agradecimento solto, risada, rótulo de legenda. Só vale quando é o trecho INTEIRO.
_FILLER_ONLY = re.compile(
    r"^(?:(?:muito\s+)?obrigad[oa]s?|tchau(?:\s+tchau)*|valeu|um\s+abra[çc]o|at[ée]\s+(?:mais|logo|a\s+pr[óo]xima)|"
    r"ok|ah|oh|hum+|hm+|u[hm]+|aham|m[úu]sica|aplausos|risos|risadas?|"
    r"(?:[ah]?[eh]){3,}h*|(?:h[ae]){2,}h*|k{3,}|(?:rs)+)$", re.IGNORECASE)


def noise_reason(text: str) -> Optional[str]:
    """Motivo, se o trecho inteiro é ruído típico do Whisper (despedida solta, risada, legenda em
    maiúsculas); None se parece fala."""
    plain = re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", text or "")).strip()
    if not plain:
        return "trecho vazio"
    if _FILLER_ONLY.match(plain):
        return "ruído do Whisper (fala solta sem conteúdo)"
    letters = [c for c in plain if c.isalpha()]
    if len(plain.split()) >= 2 and letters and not any(c.islower() for c in letters):
        return "ruído do Whisper (legenda em maiúsculas)"
    return None


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", (text or "").casefold()) if not unicodedata.combining(c))


# Vazamento das hotwords (17/10/2026): com hotwords="Jarvis __JARVIS_USER__ __JARVIS_CIDADE__" o Whisper
# devolvia o próprio texto delas (ou pedaços) no silêncio — milhares de falas em 30+ reuniões.
_HOTWORD_USER = "__JARVIS_USER__"
_HOTWORD_CIDADE = "__JARVIS_CIDADE__".split()
_HOTWORD_CIDADE_CURTA = "__JARVIS_CIDADE_CURTA__".split()
_LIGACOES = {"do", "da", "de", "dos", "das", "e"}


def _palavra_re(palavra: str) -> str:
    """Palavra minúscula aceitando com e sem acento ("Céu" -> "c[ée]u")."""
    return "".join(f"[{c}{_fold(c)}]" if _fold(c) != c else re.escape(c) for c in palavra.casefold())


def _hotword_leak_pattern() -> str:
    """Nome + cidade (o resto do nome da cidade é opcional, palavra a palavra) ou a última palavra
    da cidade repetida 3+ vezes (com a ligação "do/da/de" antes, se houver)."""
    cauda = ""
    for palavra in reversed(_HOTWORD_CIDADE[len(_HOTWORD_CIDADE_CURTA):]):
        cauda = rf"(?:\W+{_palavra_re(palavra)}{cauda})?"
    curta = r"\W+".join(map(_palavra_re, _HOTWORD_CIDADE_CURTA))
    nome = rf"(?:\bjarvis\W+)?\b{_palavra_re(_HOTWORD_USER)}\W+{curta}{cauda}(?:\W+jarvis)?\b"
    ultima = _palavra_re(_HOTWORD_CIDADE[-1])
    antes = _HOTWORD_CIDADE[-2] if len(_HOTWORD_CIDADE) >= 2 and _fold(_HOTWORD_CIDADE[-2]) in _LIGACOES else ""
    ligacao = rf"(?:{_palavra_re(antes)}\W+)?" if antes else ""
    return nome + rf"|(?:\b{ligacao}{ultima}\b\W*){{3,}}"


_HOTWORD_CIDADE_TOKENS = set(re.findall(r"\w+", _fold(" ".join(_HOTWORD_CIDADE))))
_HOTWORD_VOCAB = {"jarvis", _fold(_HOTWORD_USER)} | _HOTWORD_CIDADE_TOKENS
_HOTWORD_LEAK_RE = re.compile(_hotword_leak_pattern(), re.IGNORECASE)


def hotword_leak(text: str) -> bool:
    """Trecho INTEIRO feito só de pedaços das hotwords ("__JARVIS_USER__ __JARVIS_CIDADE__", o fim do nome da cidade).
    "__JARVIS_USER__" ou "Jarvis" sozinhos podem ser fala real (alguém chamando) e não contam."""
    tokens = re.findall(r"\w+", _fold(text))
    if not tokens or not set(tokens) <= _HOTWORD_VOCAB:
        return False
    return bool(set(tokens) & (_HOTWORD_CIDADE_TOKENS - _LIGACOES)) or (len(tokens) >= 2 and _fold(_HOTWORD_USER) in tokens)


def strip_hotword_leak(text: str) -> Optional[str]:
    """Texto sem o vazamento das hotwords; None se não sobrou fala (só o vazamento ou lixo).
    Sem vazamento, devolve o texto como veio."""
    text = (text or "").strip()
    if hotword_leak(text):
        return None
    cleaned = _HOTWORD_LEAK_RE.sub(" ", text)
    if cleaned == text:
        return text
    cleaned = re.sub(r"\s+([,.!?])", r"\1", re.sub(r"\s{2,}", " ", cleaned)).strip(" ,;")
    rest = [w for w in re.findall(r"\w+", _fold(cleaned)) if len(w) >= 3 and w not in _HOTWORD_VOCAB]
    return cleaned if len(rest) >= 2 else None


def hallucination_reason(text: str) -> Optional[str]:
    """Motivo, se o texto é uma alucinação conhecida; None se parece fala real."""
    text = (text or "").strip()
    for pattern, reason in _HALLUCINATION_RE:
        if pattern.search(text):
            return reason
    if hotword_leak(text):
        return "vazamento das hotwords"
    return noise_reason(text)


# Pedaços alucinados que às vezes vêm grudados em fala real (histórico gravado antes do filtro).
_FRAGMENTS = re.compile(
    r"transcri[çc][ãa]o fiel de conversa falada em portugu[êe]s brasileiro\.?"
    r"|nomes pr[óo]prios frequentes:?"
    r"|(?:\b[a-z]?-?j\.\s?a\.\s?r\.\s?v\.\s?i\.?\s?s?\.?,?\s*)+(?:__JARVIS_USER_LOWER__\.?,?\s*)(?:j\.a\.r\.v\.i\.s\.?,?\s*|__JARVIS_USER_LOWER__\.?,?\s*|hermes\.?,?\s*|gemini\.?,?\s*)*"
    r"|(?:transcri[çc][ãa]o e )?legendas? (?:pela|por) (?:comunidade (?:de )?)?(?:amara\.org|[a-zà-ÿ]+ [a-zà-ÿ]+)"
    r"|amara\.org|(?:muito )?obrigad[oa] por assistir\.?|inscreva-se no canal\.?"
    r"|" + _HOTWORD_LEAK_RE.pattern,
    re.IGNORECASE)
MIN_REMAINING_WORDS = 3


def strip_hallucinations(text: str):
    """(texto limpo, motivo). Texto limpo None = não sobrou fala; motivo None = nada a limpar."""
    text = (text or "").strip()
    reason = hallucination_reason(text)
    cleaned = _FRAGMENTS.sub(" ", text)
    cleaned = re.sub(r"\s+([,.!?])", r"\1", re.sub(r"\s{2,}", " ", cleaned)).strip(" ,;")
    if cleaned == text:
        return (None, reason) if reason else (text, None)
    reason = reason or "alucinação do Whisper"
    return (cleaned, reason) if len(words(cleaned)) >= MIN_REMAINING_WORDS else (None, reason)


def words(text: str) -> set:
    folded = "".join(c for c in unicodedata.normalize("NFKD", (text or "").casefold())
                     if not unicodedata.combining(c))
    return set(re.findall(r"[a-z]{3,}", folded))


def is_echo(mic_words: set, system_words: set) -> bool:
    if len(mic_words) < ECHO_MIN_WORDS or not system_words:
        return False
    return len(mic_words & system_words) / len(mic_words) >= ECHO_MIN_OVERLAP


class EchoGuard:
    """Memória curta dos últimos trechos de cada origem, para cruzar mic x sistema."""

    def __init__(self, window_s: float = ECHO_WINDOW_S, clock=time.monotonic):
        self.window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._system: Deque[Tuple[float, set]] = deque()
        self._mic: Deque[Tuple[float, set, int]] = deque()

    def _prune(self, now: float) -> None:
        for queue in (self._system, self._mic):
            while queue and now - queue[0][0] > self.window_s:
                queue.popleft()

    def mic_is_echo(self, text: str) -> bool:
        """Trecho do microfone que repete o que o sistema acabou de tocar."""
        now, w = self._clock(), words(text)
        with self._lock:
            self._prune(now)
            return any(is_echo(w, sw) for _, sw in self._system)

    def remember_mic(self, transcript_id: int, text: str) -> None:
        if transcript_id and transcript_id > 0:
            with self._lock:
                self._mic.append((self._clock(), words(text), transcript_id))

    def system_heard(self, text: str) -> List[int]:
        """Registra um trecho do sistema; devolve ids do microfone que eram cópia dele."""
        now, w = self._clock(), words(text)
        with self._lock:
            self._prune(now)
            self._system.append((now, w))
            return [tid for _, mw, tid in self._mic if is_echo(mw, w)]


# ── limpeza do histórico ─────────────────────────────────────────────────────────

def scan_history(db_path: str, apply: bool = False) -> Dict[str, int]:
    """Marca como ignorados os trechos de alucinação e de eco já gravados.
    Não apaga nada: ignorado sai da página, das atas e das buscas, e pode voltar.
    Com apply=True, as atas das reuniões afetadas ficam pendentes para serem refeitas."""
    import sqlite3
    from datetime import datetime

    from storage import _ensure_db
    _ensure_db(db_path)
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        rows = conn.execute("""SELECT id, timestamp, source, text, meeting_id FROM transcripts
                               WHERE ignored = 0 ORDER BY timestamp, id""").fetchall()
        marks: Dict[int, str] = {}
        rewrites: Dict[int, Tuple[str, str]] = {}  # id -> (texto limpo, motivo)
        for tid, _ts, _src, text, _mid in rows:
            cleaned, reason = strip_hallucinations(text)
            if cleaned is None:
                marks[tid] = reason
            elif reason:
                rewrites[tid] = (cleaned, reason)
        parse = lambda v: datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S")  # noqa: E731
        system = [(parse(ts), words(text)) for tid, ts, src, text, _ in rows if src == "loopback" and tid not in marks]
        for tid, ts, src, text, _ in rows:
            if src != "mic" or tid in marks:
                continue
            t, w = parse(ts), words(text)
            if any(abs((t - st).total_seconds()) <= ECHO_WINDOW_S and is_echo(w, sw) for st, sw in system):
                marks[tid] = "eco do áudio do PC"
        counts: Dict[str, int] = {}
        for reason in marks.values():
            counts[reason] = counts.get(reason, 0) + 1
        if rewrites:
            counts["fala real com trecho alucinado recortado"] = len(rewrites)
        if apply and (marks or rewrites):
            with conn:
                conn.executemany("UPDATE transcripts SET ignored = 1, ignored_reason = ? WHERE id = ?",
                                 [(reason, tid) for tid, reason in marks.items()])
                conn.executemany("""UPDATE transcripts SET original_text = text, text = ?, ignored_reason = ?
                                    WHERE id = ? AND original_text IS NULL""",
                                 [(clean, f"recortado: {reason}", tid) for tid, (clean, reason) in rewrites.items()])
                changed = set(marks) | set(rewrites)
                affected = {mid for tid, _ts, _src, _t, mid in rows if tid in changed and mid}
                conn.executemany("""UPDATE meetings SET ata_status = 'pending', ata_attempts = 0, ata_next_try_at = NULL
                                    WHERE id = ? AND status = 'closed'""", [(m,) for m in affected])
            counts["reuniões com ata a refazer"] = len(affected)
        return counts
    finally:
        conn.close()
