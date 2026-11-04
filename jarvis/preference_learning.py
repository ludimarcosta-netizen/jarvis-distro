"""Aprendizado de preferências do __JARVIS_USER__ a partir das sessões com as IAs, sem aprovação manual.

Tudo local: o modelo roda no Ollama da máquina e a similaridade usa os embeddings locais do Cerebro;
nenhuma mensagem sai do PC. Regras (combinadas com o __JARVIS_USER__ em 16/10/2026):
- declarada por ele como regra geral ("prefiro", "sempre", "de agora em diante") entra na hora;
- deduzida pela IA vira preferência só quando reaparece em MIN_SESSIONS sessões em MIN_DAYS dias diferentes;
- uma preferência nova que contradiz uma aprendida a substitui; a antiga fica no histórico, marcada;
- observação que não se repete em EXPIRE_DAYS dias é esquecida;
- saúde, dinheiro, documentos e outras pessoas nunca entram sozinhos.
Cada preferência aprendida é uma nota própria em `perfil/aprendido/`, com as sessões de origem, e pode ser
desativada no painel Memória. Aprender uma preferência nunca autoriza uma ação.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from cerebro_io import atomic_write, file_lock

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "gemma4:e4b"  # comparado com hermes3:8b em 16/10: separa melhor pedido pontual e tema sensível
STATE_FILE = Path(__file__).resolve().parent / "preference_learning_state.json"
MIN_SESSIONS = 3
MIN_DAYS = 2
EXPIRE_DAYS = 60
CANDIDATE = 0.25     # cosseno mínimo para pedir ao modelo comparar (calibrado em 16/10: paráfrases 0,42-0,74,
MAX_CANDIDATES = 3   # opostas de 0,33 a 0,75; o embedding só pré-seleciona e o modelo decide)
MAX_TEXT = 12000     # mensagens do usuário por sessão enviadas ao modelo local
MAX_QUEUE = 100
DIGEST_LINES = 60
SENSITIVE = re.compile(r"sa[uú]de|m[eé]dic|doen[cç]|rem[eé]dio|exame|press[aã]o arterial|banc[oá]|conta corrente|"
                       r"cart[aã]o|senha|sal[aá]rio|dinheiro|financ|d[ií]vida|cpf|\brg\b|passaporte|documento pessoal",
                       re.IGNORECASE)

RULE = re.compile(r"\bprefiro\b|\bsempre\b|\bnunca\b|de agora em diante|daqui (?:pra|para) frente|n[aã]o quero que|"
                  r"toda vez que|\bevite\b|\bjamais\b", re.IGNORECASE)  # "por padrão" pegava requisito de sistema

EXTRACT = """Abaixo estão SOMENTE as mensagens que o senhor __JARVIS_USER__ escreveu numa sessão de trabalho com uma IA.
Extraia preferências DURADOURAS sobre COMO ele quer que as IAs trabalhem com ele: estilo, formato, idioma,
comunicação, autonomia, processo, ferramentas, critérios de decisão. Elas valem para qualquer projeto.
NÃO são preferências (ignore):
- pedidos de uma tarefa só ("ajusta o artigo 3", "manda o arquivo");
- requisitos ou funcionalidades de um sistema que está sendo construído ("quero que o sistema mostre os últimos
  6 jogos", "o Jarvis deve abrir no modo face", "controlar a numeração dos ofícios", "ler sem marcar como lida");
- configurações do computador ou de um aplicativo; fatos sobre o mundo; conteúdo dos documentos.
Teste: se a frase só faz sentido dentro de um projeto específico, não é preferência.

Para cada preferência, escreva uma frase curta, geral e autoexplicativa em português, na terceira pessoa
("Prefere ...", "Quer ...", "Não quer ..."). Marque:
- "explicita": true só se ele a declarou como regra geral ("prefiro", "sempre", "nunca", "de agora em diante",
  "não quero que"); false se você deduziu de uma correção ou de um padrão.
- "sensivel": true se envolve saúde, dinheiro, documentos pessoais ou outras pessoas.
- "trecho": a frase dele, copiada exatamente, de onde a preferência saiu.

Responda só JSON: {"preferencias": [{"texto": "...", "explicita": true, "sensivel": false, "trecho": "..."}]}.
Lista vazia se não houver nenhuma. Não invente.

<mensagens>
{texto}
</mensagens>"""

RELATION = """Duas preferências do senhor __JARVIS_USER__ sobre como as IAs devem trabalhar:
ANTIGA: {old}
NOVA: {new}
Classifique a relação: "mesma" (dizem a mesma coisa com outras palavras), "contradiz" (seguir a NOVA impede
seguir a ANTIGA; uma pede e a outra recusa a mesma coisa) ou "diferente" (assuntos distintos ou compatíveis).
Exemplos:
- "Prefere ver o diff antes de salvar" / "Quer que mostrem as mudanças antes de gravar" -> mesma
- "Prefere ver o diff antes de salvar" / "Não quer que mostrem o diff; pode salvar direto" -> contradiz
- "Prefere relatórios longos e detalhados" / "Prefere relatórios curtos" -> contradiz
- "Prefere respostas em português" / "Prefere relatórios curtos" -> diferente
Responda só JSON: {{"relacao": "mesma"}}, {{"relacao": "contradiz"}} ou {{"relacao": "diferente"}}."""


def ollama(prompt: str) -> dict:
    """Uma chamada ao modelo local; a fala ao vivo tem prioridade na GPU."""
    import gpu_priority
    gpu_priority.wait_for_live_idle()
    body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False, "format": "json",
                       "options": {"temperature": 0}, "keep_alive": "2m"}).encode()
    request = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(json.load(response)["response"])


def local_embed(texts: list[str]) -> list[list[float]]:
    from cerebro_semantic import encoder
    model = encoder(str(Path(__file__).resolve().parent / "models" / "cerebro"))
    return [list(map(float, v)) for v in model.embed(texts)]


def _cos(a: list[float], b: list[float]) -> float:
    x, y = np.asarray(a), np.asarray(b)
    return float(x @ y / ((np.linalg.norm(x) * np.linalg.norm(y)) or 1.0))


def _slug(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9]+", " ", folded).split()
    return " ".join(words[:8]).capitalize() or "Preferencia"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def extract(user_text: str, ask: Callable[[str], dict] = ollama) -> list[dict]:
    """Explícita só com citação real contendo marcador de regra geral; o modelo sozinho é generoso demais."""
    reply = ask(EXTRACT.replace("{texto}", user_text[-MAX_TEXT:]))
    spoken = _norm(user_text)
    items = reply.get("preferencias") if isinstance(reply, dict) else None
    found = []
    for item in items if isinstance(items, list) else []:
        text = re.sub(r"\s+", " ", str(item.get("texto") or "")).strip() if isinstance(item, dict) else ""
        if 8 <= len(text) <= 300:
            quote = _norm(str(item.get("trecho") or ""))
            stated = item.get("explicita") is True and len(quote) >= 8 and quote in spoken and bool(RULE.search(quote))
            found.append({"texto": text, "explicita": stated,
                          "sensivel": item.get("sensivel") is not False or bool(SENSITIVE.search(text))})
    return found


class Learner:
    def __init__(self, vault: Path, state_file: Path = STATE_FILE, *, ask: Callable[[str], dict] = ollama,
                 embed: Callable[[list[str]], list[list[float]]] = local_embed, today: Optional[date] = None):
        self.vault, self.state_file, self.ask, self.embed = vault, state_file, ask, embed
        self.today = today or date.today()
        try:
            self.state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("observacoes", [])
        self.state.setdefault("fila", [])
        self.learned: list[str] = []

    # ── coleta ───────────────────────────────────────────────────────────────
    def observe_session(self, user_text: str, note: str, day: str) -> int:
        """Extrai e acumula; se o modelo local estiver fora, guarda a sessão para a próxima noite."""
        try:
            found = self.observe(extract(user_text, self.ask), note, day)
        except Exception:  # noqa: BLE001 — Ollama/embeddings fora: a sessão espera a próxima noite, nada se perde
            queue = [q for q in self.state["fila"] if q["note"] != note]
            self.state["fila"] = (queue + [{"note": note, "day": day, "text": user_text[-MAX_TEXT:]}])[-MAX_QUEUE:]
            return 0
        self.state["fila"] = [q for q in self.state["fila"] if q["note"] != note]
        return found

    def retry_queue(self) -> None:
        for item in list(self.state["fila"]):
            self.observe_session(item["text"], item["note"], item["day"])

    def observe(self, prefs: list[dict], note: str, day: str) -> int:
        prefs = [p for p in prefs if not p["sensivel"]]
        if not prefs:
            return 0
        vectors = self.embed([p["texto"] for p in prefs])
        relations = [self._relate(pref, vector) for pref, vector in zip(prefs, vectors)]  # tudo antes de mudar estado
        for pref, vector, (match, contradicted) in zip(prefs, vectors, relations):
            if match is None:
                match = {"id": hashlib.sha256(pref["texto"].encode()).hexdigest()[:16], "texto": pref["texto"],
                         "vetor": vector, "evidencias": [], "explicita": False, "estado": "observada",
                         "contradiz": contradicted}
                self.state["observacoes"].append(match)
            if all(e["note"] != note for e in match["evidencias"]):
                match["evidencias"].append({"note": note, "day": day})
            match["explicita"] = match["explicita"] or pref["explicita"]
            if pref["explicita"] and match["estado"] == "observada":
                match["texto"] = pref["texto"]  # a forma dita por ele vale mais que a deduzida
        return len(prefs)

    def _relate(self, pref: dict, vector: list[float]) -> tuple[Optional[dict], list[str]]:
        """A observação que é a mesma preferência (ou None) e os ids das que a nova contradiz."""
        live = [o for o in self.state["observacoes"] if o["estado"] != "substituida"]
        near = sorted((o for o in live if _cos(o["vetor"], vector) >= CANDIDATE),
                      key=lambda o: _cos(o["vetor"], vector), reverse=True)[:MAX_CANDIDATES]
        contradicted = []
        for obs in near:
            reply = self.ask(RELATION.format(old=obs["texto"], new=pref["texto"]))
            relation = reply.get("relacao") if isinstance(reply, dict) else None
            if relation == "mesma":
                return obs, contradicted
            if relation == "contradiz":
                contradicted.append(obs["id"])
        return None, contradicted

    # ── promoção, contradição e esquecimento ─────────────────────────────────
    def ready(self, obs: dict) -> bool:
        days = {e["day"] for e in obs["evidencias"]}
        return obs["explicita"] or (len(obs["evidencias"]) >= MIN_SESSIONS and len(days) >= MIN_DAYS)

    def consolidate(self) -> list[str]:
        """Promove o que tem evidência, substitui o contradito, esquece o velho. Devolve as linhas do resumo."""
        lines = []
        for obs in self.state["observacoes"]:
            if obs["estado"] != "observada" or not self.ready(obs):
                continue
            for old in self.state["observacoes"]:
                if old["estado"] == "aprendida" and old["id"] in obs.get("contradiz", []):
                    old["estado"] = "substituida"
                    self._mark_replaced(old, obs)
                    lines.append(f"substituí: {old['texto']} → {obs['texto']}")
            obs["estado"] = "aprendida"
            obs["nota"] = self._write_note(obs)
            why = "dito pelo senhor" if obs["explicita"] else f"visto em {len(obs['evidencias'])} sessões"
            lines.append(f"aprendi: {obs['texto']} ({why})")
        cutoff = (self.today - timedelta(days=EXPIRE_DAYS)).isoformat()
        self.state["observacoes"] = [o for o in self.state["observacoes"] if o["estado"] != "observada"
                                     or max(e["day"] for e in o["evidencias"]) >= cutoff]
        self.learned = lines
        if lines:
            self._digest(lines)
        return lines

    def _note_path(self, obs: dict) -> Path:
        return self.vault / "perfil" / "aprendido" / f"{_slug(obs['texto'])} {obs['id'][:6]}.md"

    def _write_note(self, obs: dict) -> str:
        path = self._note_path(obs)
        days = sorted({e["day"] for e in obs["evidencias"]})
        sources = ", ".join(f"[[{e['note']}]]" for e in obs["evidencias"][-10:])
        why = "Declarada pelo senhor como regra geral." if obs["explicita"] else (
            f"Deduzida de {len(obs['evidencias'])} sessões em {len(days)} dias diferentes.")
        meta = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in (
            ("tipo", "preferencia"), ("origem", "aprendizado automático"), ("estado", "ativa"),
            ("aprendido_em", self.today.isoformat()), ("evidencias", len(obs["evidencias"]))))
        with file_lock(path):
            if not path.exists():  # nunca sobrescreve uma nota que o senhor ou outra IA já revisou
                atomic_write(path, f"---\n{meta}\n---\n\n# Preferência aprendida\n\n{obs['texto']}\n\n"
                                   f"{why} Fontes: {sources}.\n\nSe estiver errada, desative esta nota no painel Memória.\n")
        return path.relative_to(self.vault).as_posix()

    def _mark_replaced(self, old: dict, new: dict) -> None:
        path = self.vault / old.get("nota", "")
        if not old.get("nota") or not path.is_file():
            return
        with file_lock(path):
            text = path.read_text(encoding="utf-8").replace('estado: "ativa"', 'estado: "substituida"', 1)
            note = Path(self._note_path(new)).stem
            atomic_write(path, text.rstrip() + f"\n\nSubstituída em {self.today.isoformat()} por [[{note}]].\n")

    def _digest(self, lines: list[str]) -> None:
        path = self.vault / "perfil" / "Aprendizado recente.md"
        with file_lock(path):
            old = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            rows = [f"- {self.today.isoformat()} — {line}" for line in lines] + [r for r in old if r.startswith("- ")]
            atomic_write(path, "# Aprendizado recente\n\nO que o Jarvis aprendeu sozinho nas sessões com as IAs. "
                               "Para desfazer, desative a nota da preferência no painel Memória.\n\n"
                               + "\n".join(rows[:DIGEST_LINES]) + "\n")

    def save(self) -> None:
        with file_lock(self.state_file):
            atomic_write(self.state_file, json.dumps(self.state, ensure_ascii=False))
