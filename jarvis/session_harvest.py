"""Alimenta o Cerebro (Obsidian) com os históricos locais das IAs compatíveis.

Lê as conversas locais (Claude Code, Codex, Antigravity, Gemini CLI e Hermes),
tira padrões de segredos, captura trechos sem rede e grava uma nota por sessão em
`<Cerebro>/sessoes/`, mais `_atividade.md` (últimos 14 dias) e `perfil/Jeito de trabalhar.md`
(preferências observadas, só acrescenta) e fatos novos no histórico das fichas de pessoas que já existem.
O serviço cerebro_memory seleciona contexto e fontes por tarefa.

Uso: python session_harvest.py run [--max N] [--tool FERRAMENTA] [--dry-run]
Só resume sessões paradas há IDLE_S segundos e alteradas desde o último resumo.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote

import cerebro
import project_notes
from cerebro_io import atomic_write, file_lock

HERE = Path(__file__).resolve().parent
HOME = Path.home()
STATE_FILE = HERE / "session_harvest_state.json"
IDLE_S = 5 * 60
MIN_USER_CHARS = 300
INPUT_CAP = 60_000
ACTIVITY_DAYS = 14
TOOL_NAMES = {"claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity",
              "gemini": "Gemini CLI", "hermes": "Jarvis Hermes"}
DEFAULT_TOOLS = ("claude", "codex", "antigravity", "hermes")


@dataclass
class Session:
    tool: str
    sid: str
    project: str
    started: datetime
    ended: datetime
    source: Path
    turns: List[Tuple[str, str]] = field(default_factory=list)  # (user|assistant, texto)
    files: List[str] = field(default_factory=list)
    activity: List[str] = field(default_factory=list)
    mtime: float = 0.0

    @property
    def user_chars(self) -> int:
        return sum(len(t) for r, t in self.turns if r == "user")


# Compatibilidade com os consumidores: conteúdo preservado por escolha do usuário.
def redact(text: str) -> str:
    """Preserva o texto integralmente, sem mascaramento."""
    return text


def memory_excerpt(text: str, limit: int) -> str:
    """Limita apenas o tamanho do contexto, sem filtrar o conteúdo."""
    return text[:limit]


# ── leitores ──────────────────────────────────────────────────────────────────────

def _iso(value: Any) -> Optional[datetime]:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc).astimezone()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError, OSError):
        return None


_NOISE_PREFIXES = ("<command-", "<local-command", "Caveat:", "[Request interrupted", "<environment_context>",
                   "# AGENTS.md", "<user_instructions>", "<permissions", "<system-reminder>", "<task-notification>")
_TAGS = re.compile(r"<(system-reminder|ide_[a-z_]+|task-notification)>.*?</\1>", re.S)


def _clean_user(text: str) -> str:
    text = _TAGS.sub(" ", text).strip()
    return "" if text.startswith(_NOISE_PREFIXES) else text


def _project_name(cwd: str, fallback: str = "") -> str:
    cwd = (cwd or "").rstrip("\\/")
    return re.split(r"[\\/]", cwd)[-1] if cwd and cwd.count("\\") + cwd.count("/") > 1 else (fallback or cwd or "geral")


def read_claude(path: Path) -> Optional[Session]:
    turns, files, stamps, cwd = [], [], [], ""
    sid = path.stem
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
            continue
        cwd = d.get("cwd") or cwd
        when = _iso(d.get("timestamp"))
        if when:
            stamps.append(when)
        content = (d.get("message") or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text"):
                text = _clean_user(b["text"]) if d["type"] == "user" else b["text"].strip()
                if text:
                    turns.append((d["type"], text))
            elif b.get("type") == "tool_use" and b.get("name") in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
                target = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path")
                if target and target not in files:
                    files.append(target)
    if not stamps:
        return None
    return Session("claude", sid, _project_name(cwd, path.parent.name), min(stamps), max(stamps), path, turns, files,
                   mtime=path.stat().st_mtime)


_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)


def read_codex(path: Path) -> Optional[Session]:
    turns, files, stamps, cwd, sid = [], [], [], "", path.stem
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        payload = d.get("payload") or {}
        when = _iso(d.get("timestamp"))
        if when:
            stamps.append(when)
        if d.get("type") == "session_meta":
            if payload.get("thread_source") == "subagent" or payload.get("parent_thread_id"):
                return None  # o trabalho do subagente já aparece na sessão principal
            cwd, sid = payload.get("cwd") or cwd, payload.get("id") or sid
        elif d.get("type") == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            text = "\n".join(c.get("text", "") for c in payload.get("content") or [] if isinstance(c, dict)).strip()
            text = _clean_user(text) if role == "user" else text
            if text:
                turns.append((role, text))
        elif d.get("type") == "response_item" and payload.get("type") == "custom_tool_call":
            for target in _PATCH_FILE.findall(str(payload.get("input") or "")):
                if target not in files:
                    files.append(target.strip())
    if not stamps:
        return None
    return Session("codex", sid, _project_name(cwd), min(stamps), max(stamps), path, turns, files, mtime=path.stat().st_mtime)


def _proto_strings(blob: bytes, depth: int = 0) -> List[str]:
    """Textos de um protobuf sem esquema: lê os campos delimitados e desce nos aninhados."""
    out, i, n = [], 0, len(blob)

    def varint() -> int:
        nonlocal i
        value = shift = 0
        while True:
            byte = blob[i]
            i += 1
            value |= (byte & 127) << shift
            shift += 7
            if byte < 128:
                return value

    try:
        while i < n:
            kind = varint() & 7
            if kind == 0:
                varint()
            elif kind == 1:
                i += 8
            elif kind == 5:
                i += 4
            elif kind == 2:
                length = varint()
                chunk = blob[i:i + length]
                i += length
                if len(chunk) != length:
                    break
                try:
                    text = chunk.decode("utf-8")
                    if text.isprintable() or "\n" in text:
                        out.append(text)
                        continue
                except UnicodeDecodeError:
                    pass
                if depth < 5:
                    out += _proto_strings(chunk, depth + 1)
            else:
                break
    except IndexError:
        pass
    return out


_AG_EDIT_KEYS = ("TargetFile", "CodeContent", "ReplacementContent")


def read_antigravity(path: Path, prompts: List[Dict[str, Any]]) -> Optional[Session]:
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        rows = conn.execute("SELECT idx, step_type, step_payload FROM steps ORDER BY idx").fetchall()
    except sqlite3.Error:
        return None
    sid = path.stem
    root = path.parent.parent
    title = ""
    try:
        summary = sqlite3.connect(f"file:{(root / 'conversation_summaries.db').as_posix()}?mode=ro", uri=True).execute(
            "SELECT title, preview, workspace_uris FROM conversation_summaries WHERE conversation_id = ?", (sid,)).fetchone()
        title = (summary[0] or summary[1] or "") if summary else ""
        workspace = (summary[2] or "") if summary else ""
    except sqlite3.Error:
        workspace = ""
    turns, files, activity = [], [], []
    if title:
        turns.append(("user", title))
    started = datetime.fromtimestamp(path.stat().st_ctime).astimezone()
    ended = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    for ask in prompts:
        when = _iso(ask.get("timestamp"))
        if when and started - timedelta(minutes=5) <= when <= ended + timedelta(minutes=5):
            turns.append(("user", str(ask.get("display") or "")))
            workspace = workspace or str(ask.get("workspace") or "")
    for _idx, step_type, payload in rows:
        if step_type == 14 or not payload:  # 14 = contexto/instruções do sistema
            continue
        for text in _proto_strings(bytes(payload)):
            if text.startswith("{"):
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                if data.get("toolSummary") and data["toolSummary"] not in activity:
                    activity.append(data["toolSummary"])
                target = data.get("TargetFile") or (data.get("AbsolutePath") if any(k in data for k in _AG_EDIT_KEYS) else None)
                if target and target not in files:
                    files.append(target)
            elif len(text) > 60 and not re.fullmatch(r"[\w\-:$ ]{30,}", text):
                turns.append(("assistant", text.strip()))
    brain = root / "brain" / sid
    if brain.is_dir():
        for artifact in sorted(brain.glob("*.md"))[:4]:
            turns.append(("assistant", f"[{artifact.stem}]\n" + artifact.read_text(encoding="utf-8", errors="replace")[:4000]))
    if not turns:
        return None
    uri = re.search(r"file:///([^\"'\]\s,]+)", workspace)
    cwd = unquote(uri.group(1)).replace("/", "\\") if uri else workspace
    return Session("antigravity", sid, _project_name(cwd),
                   started, ended, path, turns, files, activity, mtime=path.stat().st_mtime)


def read_gemini(path: Path) -> Session | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    turns, stamps = [], []
    for item in data.get("messages", []):
        role = {"user": "user", "gemini": "assistant", "model": "assistant"}.get(item.get("type"))
        content = item.get("content", "")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        if role and isinstance(content, str) and content.strip():
            turns.append((role, _clean_user(content) if role == "user" else content))
        stamp = _iso(item.get("timestamp"))
        if stamp:
            stamps.append(stamp)
    if not stamps or not turns:
        return None
    return Session("gemini", str(data.get("sessionId") or path.stem),
                   _project_name(str(data.get("cwd") or ""), path.parent.parent.name),
                   min(stamps), max(stamps), path, turns)


def read_hermes(path: Path, sid: str) -> Session | None:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT cwd,started_at FROM sessions WHERE id=? AND parent_session_id IS NULL", (sid,)).fetchone()
        messages = conn.execute("SELECT role,content,timestamp FROM messages WHERE session_id=? AND role IN ('user','assistant') AND active=1 AND _compressed_summary=0 ORDER BY id", (sid,)).fetchall()
    if not row or not messages:
        return None
    turns = [(r, _clean_user(t) if r == "user" else t) for r, t, _ in messages if isinstance(t, str) and t.strip()]
    return Session("hermes", sid, _project_name(row[0] or "", "Jarvis"),
                   datetime.fromtimestamp(row[1], timezone.utc).astimezone(),
                   datetime.fromtimestamp(max(m[2] for m in messages), timezone.utc).astimezone(), path, turns)


def discover(tools: Iterable[str], hermes_days: int = 14) -> Iterable[Tuple[str, Path, Callable[[], Optional[Session]]]]:
    if "gemini" in tools:
        for path in (HOME / ".gemini" / "tmp").glob("*/chats/session-*.json"):
            yield "gemini", path, lambda p=path: read_gemini(p)
    if "hermes" in tools:
        db = HOME / "AppData/Local/hermes/state.db"
        if db.exists():
            with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                rows = conn.execute("SELECT s.id,max(m.timestamp) FROM sessions s JOIN messages m ON m.session_id=s.id WHERE s.parent_session_id IS NULL AND m.role IN ('user','assistant') AND m.active=1 AND m._compressed_summary=0 GROUP BY s.id HAVING max(m.timestamp)>?", (datetime.now().timestamp() - hermes_days * 86400,)).fetchall()
            for sid, stamp in rows:
                reader = partial(read_hermes, db, sid)
                reader.harvest_sid, reader.harvest_mtime = sid, stamp
                yield "hermes", db, reader
    if "claude" in tools:
        for path in (HOME / ".claude" / "projects").glob("*/*.jsonl"):
            yield "claude", path, lambda p=path: read_claude(p)
    if "codex" in tools:
        for path in (HOME / ".codex" / "sessions").rglob("*.jsonl"):
            yield "codex", path, lambda p=path: read_codex(p)
    if "antigravity" in tools:
        prompts: List[Dict[str, Any]] = []
        history = HOME / ".gemini" / "antigravity-cli" / "history.jsonl"
        if history.exists():
            for line in history.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    prompts.append(json.loads(line))
                except ValueError:
                    pass
        for root in (HOME / ".gemini" / "antigravity-cli", HOME / ".gemini" / "antigravity"):
            for path in (root / "conversations").glob("*.db"):
                yield "antigravity", path, lambda p=path: read_antigravity(p, prompts if "-cli" in str(p) else [])


# ── resumo ────────────────────────────────────────────────────────────────────────

def _transcript(session: Session) -> str:
    lines = []
    for role, text in session.turns:
        text = redact(text)
        limit = 1800 if role == "user" else 700
        lines.append(f"{'SENHOR' if role == 'user' else 'IA'}: {text[:limit]}")
    body = "\n".join(lines)
    if len(body) > INPUT_CAP:  # mantém começo e fim, onde ficam o pedido e o resultado
        body = body[:INPUT_CAP // 2] + "\n[... trecho do meio omitido ...]\n" + body[-INPUT_CAP // 2:]
    extras = ""
    if session.files:
        extras += "\nARQUIVOS EDITADOS: " + "; ".join(session.files[:40])
    if session.activity:
        extras += "\nAÇÕES: " + "; ".join(session.activity[:40])
    return redact(body + extras)


PROMPT = """Você resume uma sessão de trabalho do senhor __JARVIS_USER__ com uma IA ({tool}, projeto "{project}", {date}).
O objetivo é que ele (ou o Jarvis) consiga, meses depois, pesquisar "o que eu fiz neste projeto" ou "o que editei
nesta lei/decreto/documento". Produza JSON estrito, em português do Brasil, sem inventar nada:
- "titulo": título curto e específico (sem data, sem nome da ferramenta)
- "resumo": 2 a 4 frases: o que ele queria e o resultado
- "uma_linha": uma frase de até 160 caracteres
- "pedidos": o que o senhor pediu (lista curta)
- "feito": o que foi efetivamente feito/alterado (lista; seja concreto: arquivos, funções, artigos de lei, valores)
- "documentos": leis, decretos, portarias, contratos, planilhas, projetos ou sistemas citados/alterados, com número/nome
- "decisoes": decisões tomadas, com o motivo quando houver
- "pendencias": o que ficou por fazer
- "preferencias": como o senhor gosta de trabalhar/decidir, evidenciado na conversa (correções, "prefiro", "não faça",
  critérios de decisão). Só o que a conversa mostra; lista vazia se nada.
- "prefeitura": true se o trabalho tem relação com a Prefeitura de __JARVIS_CIDADE__ (leis, decretos, portarias,
  licitações, secretarias, procuradoria, servidores, atendimento ao município); false se for projeto pessoal ou técnico
- "pessoas": pessoas reais citadas pelo nome e relevantes ao trabalho (colegas, servidores, contatos); NÃO inclua
  nomes de subagentes/IAs, o próprio senhor nem figuras públicas só mencionadas de passagem; "organizacoes": órgãos/empresas
- "fatos_pessoas": fatos novos e concretos sobre essas pessoas que a conversa afirma (cargo, função, setor, contato de
  trabalho, relação com o senhor, acordo, decisão, pendência dela), como lista de {{"pessoa": nome como citado,
  "fato": frase curta e autoexplicativa}}. Nada de suposição, CPF/RG, dados bancários ou de saúde. Lista vazia se nada.
Regras: nunca inclua senhas, chaves ou tokens; ignore ruído e comandos triviais.

<sessao>
{body}
</sessao>"""


def summarize_cloud(session: Session) -> Dict[str, Any]:
    raise PermissionError("Históricos das IAs não são enviados à nuvem; use summarize local")


def summarize(session: Session) -> dict[str, Any]:
    """Captura local extrativa: preserva pedidos e relatos, sem promover inferências a fatos."""
    users = [memory_excerpt(t, 1200) for role, t in session.turns if role == "user"]
    replies = [memory_excerpt(t, 1600) for role, t in session.turns if role == "assistant"]
    first = re.sub(r"\s+", " ", users[0] if users else session.project)
    return {"titulo": first[:65], "resumo": "Registro local extrativo; respostas da IA são relatos não verificados.",
            "uma_linha": first[:160], "pedidos": users[-4:],
            "feito": ["Relato da IA (não verificado): " + t for t in replies[-2:]],
            "preferencias": [], "fatos_pessoas": [], "decisoes": [], "pendencias": []}


# ── notas ─────────────────────────────────────────────────────────────────────────

def _slug(text: str, limit: int = 70) -> str:
    text = unicodedata.normalize("NFC", re.sub(r'[\\/:*?"<>|#^\[\]\n\r]+', " ", text or "")).strip(" .")
    return re.sub(r"\s{2,}", " ", text)[:limit].strip() or "Sessão"


def _names(items: Any) -> List[str]:
    """Itens como texto: o modelo às vezes devolve {"nome": ...} em vez de string."""
    out = []
    for item in items or []:
        if isinstance(item, dict):
            item = next((v for v in item.values() if isinstance(v, str)), "")
        if str(item).strip():
            out.append(str(item).strip())
    return out


def _bullets(items: Any) -> str:
    return "\n".join(f"- {redact(i)}" for i in _names(items)) or "- (nada registrado)"


def _link_known(names: Iterable[str], vault: Path, folder: str) -> List[str]:
    existing = {p.stem.casefold(): p.stem for p in (vault / folder).glob("*.md")}
    return [f"[[{existing[n.casefold()]}]]" if n.casefold() in existing else n for n in _names(names)]


PREFEITURA_NOTE = "Prefeitura Municipal de __JARVIS_CIDADE__"
_PREFEITURA_RE = re.compile(
    r"prefeitura|munic[ií]pio|municipal|__JARVIS_CIDADE_REGEX__|secretaria (?:municipal|de )|procuradoria|licita[çc][ãa]o|"
    r"preg[ãa]o|empenho|controle interno|lei org[âa]nica|projeto de lei|decreto|portaria|servidor(?:es)? p[úu]blic|"
    r"c[âa]mara municipal|tribunal de contas|\btcm\b|\btce\b|iptu|dispensa de licita|contrata[çc][ãa]o p[úu]blica|"
    r"administra[çc][ãa]o p[úu]blica|prefeito", re.IGNORECASE)


def is_prefeitura(data: Dict[str, Any]) -> bool:
    """Trabalho ligado à Prefeitura: o modelo disse que sim, ou o resumo fala de administração municipal."""
    if data.get("prefeitura") is True:
        return True
    fields = ("titulo", "resumo", "pedidos", "feito", "documentos", "decisoes", "pendencias", "organizacoes")
    text = " ".join(" ".join(_names(data[f])) if isinstance(data.get(f), list) else str(data.get(f) or "")
                    for f in fields)
    return len(_PREFEITURA_RE.findall(text)) >= 2


def relink_prefeitura(vault: Path, classify: Optional[Callable[[str], bool]] = None) -> int:
    """Liga à Prefeitura as notas de sessão já geradas que falam de administração municipal (sem usar o modelo)."""
    if not (vault / "organizacoes" / f"{PREFEITURA_NOTE}.md").exists():
        return 0
    org, changed = f"[[{PREFEITURA_NOTE}]]", 0
    for path in (vault / "sessoes").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        if "organizacao:" in text.split("---", 2)[1]:
            continue
        body = text.split("## Arquivos alterados")[0]  # caminhos de arquivo não contam como assunto
        if len(_PREFEITURA_RE.findall(body)) < 2 and not (classify and classify(body)):
            continue
        text = text.replace("tipo: sessao-ia\n", f'tipo: sessao-ia\norganizacao: "{org}"\ntags: [prefeitura]\n', 1)
        text = re.sub(r"(\*\*Quando:\*\* [^\n]*)", lambda m: f"{m.group(1)} · **Organização:** {org}", text, count=1)
        path.write_text(text, encoding="utf-8")
        changed += 1
    return changed


def note_name(session: Session, data: Dict[str, Any]) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", session.sid)[:8]
    return (f"{session.started:%Y-%m-%d} {TOOL_NAMES[session.tool]} — "
            f"{_slug(str(data.get('titulo') or session.project))} ({safe_id})")


def write_note(session: Session, data: Dict[str, Any], vault: Path) -> Path:
    folder = vault / "sessoes"
    folder.mkdir(exist_ok=True)
    path = folder / (note_name(session, data) + ".md")
    # Reutiliza a nota da mesma sessão mesmo quando o título mudar.
    matches = []
    for candidate in folder.glob("*.md"):
        meta = cerebro._frontmatter(candidate.read_text(encoding="utf-8"))
        if meta.get("sessao_id") == session.sid and meta.get("ferramenta") == TOOL_NAMES[session.tool]:
            matches.append(candidate)
    if matches:
        path = min(matches, key=lambda p: p.name)
    if not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError("Nota de sessão fora da pasta permitida")
    people = _link_known(data.get("pessoas") or [], vault, "pessoas")
    orgs = _link_known(data.get("organizacoes") or [], vault, "organizacoes")
    summary_text = " ".join(str(data.get(k) or "") for k in ("titulo", "resumo", "uma_linha")) + " " + " ".join(
        _names(data.get("pedidos")) + _names(data.get("feito")) + _names(data.get("documentos")))
    project = project_notes.resolve(vault, session.project, summary_text)
    project_link = f"[[{project}]]" if project else session.project
    org = (f"[[{PREFEITURA_NOTE}]]" if (is_prefeitura(data) or project_notes.project_has_org(vault, project))
           and (vault / "organizacoes" / f"{PREFEITURA_NOTE}.md").exists() else "")
    front = ("---\ntipo: sessao-ia\n" + (f'projeto_nota: "[[{project}]]"\n' if project else "")
             + (f'organizacao: "{org}"\ntags: [prefeitura]\n' if org else "") +
             f"ferramenta: {TOOL_NAMES[session.tool]}\nprojeto: \"{session.project}\"\nsessao_id: {session.sid}\n"
             f"inicio: {session.started:%Y-%m-%d %H:%M}\nfim: {session.ended:%Y-%m-%d %H:%M}\n"
             "origem: gerada automaticamente por session_harvest.py (Jarvis)\n---\n")
    files = "\n".join(f"- `{f}`" for f in session.files[:60]) or "- (nenhum)"
    body = (f"# {data.get('titulo') or session.project}\n\n{redact(str(data.get('resumo') or ''))}\n\n"
            f"**Projeto:** {project_link} · **Ferramenta:** {TOOL_NAMES[session.tool]} · "
            f"**Quando:** {session.started:%d/%m/%Y %H:%M}–{session.ended:%H:%M}"
            + (f" · **Organização:** {org}" if org else "") + "\n\n"
            f"## Pedidos\n{_bullets(data.get('pedidos'))}\n\n## O que foi feito\n{_bullets(data.get('feito'))}\n\n"
            f"## Documentos, leis e sistemas\n{_bullets(data.get('documentos'))}\n\n"
            f"## Decisões\n{_bullets(data.get('decisoes'))}\n\n## Pendências\n{_bullets(data.get('pendencias'))}\n\n"
            f"## Arquivos alterados\n{files}\n\n"
            f"## Pessoas e organizações\n{', '.join(people + orgs) or '(nenhuma)'}\n")
    with file_lock(path):
        if path.exists():
            previous = path.read_text(encoding="utf-8")
            version = hashlib.sha256(previous.encode("utf-8")).hexdigest()
            archive = vault / "_historico" / "sessoes" / (version + ".md")
            if not archive.exists():
                atomic_write(archive, previous)
        atomic_write(path, front + "\n" + body)
    return path


def append_preferences(data: Dict[str, Any], session: Session, note: str, vault: Path) -> None:
    with file_lock(vault / "perfil" / "Jeito de trabalhar.md"):
        _append_preferences_locked(data, session, note, vault)


def _append_preferences_locked(data: dict[str, Any], session: Session, note: str, vault: Path) -> None:
    prefs = [redact(p) for p in _names(data.get("preferencias"))]
    if not prefs:
        return
    path = vault / "perfil" / "Jeito de trabalhar.md"
    path.parent.mkdir(exist_ok=True)
    if not path.exists():
        path.write_text("# Jeito de trabalhar do __JARVIS_USER__\n\nPreferências e critérios de decisão observados nas sessões "
                        "com as IAs. Só acrescenta; cada linha cita a sessão de origem.\n\n## Observações\n", encoding="utf-8")
    known = path.read_text(encoding="utf-8").casefold()
    fresh = [p for p in prefs if p.casefold() not in known]
    if fresh:
        with path.open("a", encoding="utf-8") as out:
            for p in fresh:
                out.write(f"- {session.started:%Y-%m-%d} — {p} (fonte: [[{note}]])\n")


def append_person_facts(data: Dict[str, Any], session: Session, note: str, vault: Path) -> int:
    """Acrescenta no histórico das fichas JÁ existentes os fatos da sessão. Pessoa sem ficha ou nome ambíguo:
    não grava (criar ficha nova é com o senhor). Retorna quantos fatos entraram."""
    added = 0
    for item in data.get("fatos_pessoas") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("pessoa") or "").strip()
        fact = re.sub(r"\s+", " ", redact(str(item.get("fato") or ""))).strip().rstrip(".")[:300]
        found = cerebro.find_person(vault, name) if fact and "[REMOVIDO]" not in fact else None
        if not isinstance(found, Path) or cerebro._fold(found.stem).startswith("__JARVIS_USER_LOWER__"):
            continue
        line = f"- {session.started:%Y-%m-%d} — {fact} (fonte: [[{note}|sessão {TOOL_NAMES[session.tool]}]])"
        added += cerebro.add_history(found, line, line[2:], session.started.date())
    return added


def refresh_activity(vault: Path, state: Dict[str, Any]) -> None:
    cutoff = (datetime.now() - timedelta(days=ACTIVITY_DAYS)).strftime("%Y-%m-%d")
    rows = sorted((v for v in state.values() if v.get("date", "") >= cutoff), key=lambda v: v["date"], reverse=True)
    out, day = ["# Atividade recente (últimos 14 dias)\n",
                "Gerado automaticamente das sessões com as IAs. Detalhes nas notas de `sessoes/`.\n"], ""
    for v in rows:
        if v["date"] != day:
            day = v["date"]
            out.append(f"\n## {day}")
        out.append(f"- **{v['tool']} · {v['project']}** — {v['line']} [[{v['note']}]]")
    atomic_write(vault / "_atividade.md", "\n".join(out) + "\n")


def classify_prefeitura(note_text: str) -> bool:
    """Classificação conservadora local; nunca envia notas do cofre à nuvem."""
    return bool(_PREFEITURA_RE.search(note_text))


# ── execução ──────────────────────────────────────────────────────────────────────

def load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run(max_sessions: int = 25, tools=DEFAULT_TOOLS, dry_run: bool = False,
        summarizer: Callable[[Session], dict[str, Any]] = summarize, now: float | None = None,
        learner_factory: Callable[[Path], Any] | None = None) -> dict[str, int]:
    """learner_factory: aprendizado de preferências (preference_learning.Learner); None = não aprende."""
    with file_lock(STATE_FILE):
        return _run(max_sessions, tools, dry_run, summarizer, now, learner_factory)


def user_messages(session: Session) -> str:
    return "\n".join("- " + redact(re.sub(r"\s+", " ", text))[:1500] for role, text in session.turns if role == "user")


def _run(max_sessions: int = 25, tools=DEFAULT_TOOLS, dry_run: bool = False,
        summarizer: Callable[[Session], Dict[str, Any]] = summarize, now: Optional[float] = None,
        learner_factory: Optional[Callable[[Path], Any]] = None) -> Dict[str, int]:
    vault = cerebro.vault_dir()
    if vault is None:
        raise SystemExit("Cerebro não configurado (config.toml [cerebro] path).")
    learner = learner_factory(vault) if learner_factory and not dry_run else None
    if learner:
        learner.retry_queue()
    state, now = load_state(), now or datetime.now().timestamp()
    pending = []
    for tool, path, reader in discover(tools):
        key = f"{tool}:{path}"
        if hasattr(reader, "harvest_sid"):
            key += ":" + reader.harvest_sid
        try:
            stat = path.stat()
        except OSError:
            continue
        modified = getattr(reader, "harvest_mtime", stat.st_mtime)
        if now - modified < IDLE_S or state.get(key, {}).get("mtime") == modified:
            continue
        pending.append((modified, key, reader))
    pending.sort(reverse=True)  # as mais recentes primeiro; o atraso antigo escoa nas noites seguintes
    counts = {"pendentes": len(pending), "resumidas": 0, "puladas": 0, "erros": 0, "fatos_pessoas": 0,
              "preferencias_vistas": 0, "aprendidas": 0}
    for mtime, key, reader in pending:
        if counts["resumidas"] >= max_sessions:
            break
        try:
            session = reader()
            if session is None or session.user_chars < MIN_USER_CHARS:
                state[key] = {"mtime": mtime, "skipped": True}
                counts["puladas"] += 1
                continue
            if dry_run:
                print(f"[dry] {session.tool} {session.project} {session.started:%Y-%m-%d} turnos={len(session.turns)}")
                counts["resumidas"] += 1
                continue
            data = summarizer(session)
            note_path = write_note(session, data, vault)
            append_preferences(data, session, note_path.stem, vault)
            counts["fatos_pessoas"] += append_person_facts(data, session, note_path.stem, vault)
            if learner:
                counts["preferencias_vistas"] += learner.observe_session(
                    user_messages(session), note_path.stem, f"{session.started:%Y-%m-%d}")
            state[key] = {"mtime": mtime, "note": note_path.stem, "date": f"{session.started:%Y-%m-%d}",
                          "tool": TOOL_NAMES[session.tool], "project": session.project,
                          "line": redact(str(data.get("uma_linha") or data.get("titulo") or ""))[:200]}
            counts["resumidas"] += 1
        except Exception as exc:  # noqa: BLE001 — uma sessão ruim não para as outras
            counts["erros"] += 1
            print(f"[!] {key}: {type(exc).__name__}: {exc}", file=sys.stderr)
        if not dry_run:
            atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False))
    if learner:
        counts["aprendidas"] = len(learner.consolidate())
        learner.save()
    if not dry_run:
        refresh_activity(vault, state)
        project_notes.refresh_session_blocks(vault, {v["note"]: v["line"] for v in state.values() if v.get("note")})
    return counts


def learn_history(learner_factory: Callable[[Path], Any], tools=DEFAULT_TOOLS, save_every: int = 10) -> Dict[str, int]:
    """Passa pelo aprendizado as sessões resumidas ainda não aprendidas ou alteradas desde então, da mais antiga
    para a mais nova (a preferência mais recente vence as contradições). Independe de quem resumiu: o coletor de
    15 min resume sem modelo, e o job diário aprende aqui. Guarda a versão (mtime) aprendida de cada nota."""
    vault = cerebro.vault_dir()
    if vault is None:
        raise SystemExit("Cerebro não configurado (config.toml [cerebro] path).")
    state, learner = load_state(), learner_factory(vault)
    done = learner.state.get("historico", {})
    if isinstance(done, list):  # formato antigo (só notas): considera aprendida a versão atual
        notes = set(done)
        done = {v["note"]: v.get("mtime") for v in state.values() if v.get("note") in notes}
        learner.state["historico"] = done
    rows = []
    for tool, path, reader in discover(tools, hermes_days=36500):
        key = f"{tool}:{path}" + (":" + reader.harvest_sid if hasattr(reader, "harvest_sid") else "")
        item = state.get(key, {})
        if item.get("note") and done.get(item["note"], "nunca") != item.get("mtime"):
            rows.append((item.get("date", ""), item["note"], reader, item.get("mtime")))
    rows.sort(key=lambda row: row[:2])
    counts = {"sessoes": len(rows), "lidas": 0, "preferencias_vistas": 0, "erros": 0, "aprendidas": 0}
    for n, (day, note, reader, mtime) in enumerate(rows, 1):
        try:
            session = reader()
        except Exception as exc:  # noqa: BLE001 — uma sessão ilegível não para as outras
            counts["erros"] += 1
            print(f"[!] {note}: {type(exc).__name__}", file=sys.stderr)
            session = None
        if session is not None:
            counts["preferencias_vistas"] += learner.observe_session(user_messages(session), note, day)
            counts["lidas"] += 1
        done[note] = mtime
        learner.state["historico"] = done
        if n % save_every == 0:
            learner.save()
            print(f"{n}/{len(rows)} sessões", flush=True)
    counts["aprendidas"] = len(learner.consolidate())
    learner.save()
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "relink", "aprender-historico"])
    parser.add_argument("--max", type=int, default=25)
    parser.add_argument("--tool", choices=list(TOOL_NAMES))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--llm", action="store_true", help="relink: usa o modelo nas notas que as palavras-chave não pegaram")
    args = parser.parse_args()
    if args.command == "relink":
        print({"religadas": relink_prefeitura(cerebro.vault_dir(), classify_prefeitura if args.llm else None)})
    elif args.command == "aprender-historico":
        from preference_learning import Learner
        print(learn_history(Learner, (args.tool,) if args.tool else DEFAULT_TOOLS))
    else:
        from preference_learning import Learner
        tools = (args.tool,) if args.tool else DEFAULT_TOOLS
        print(run(args.max, tools, args.dry_run))
        if not args.dry_run:
            print(learn_history(Learner, tools))
