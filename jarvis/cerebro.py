"""
Ponte do Jarvis com o Segundo Cérebro: a pasta de notas Markdown compartilhada com as
outras IAs do senhor __JARVIS_USER__ (regras em <Cerebro>/AGENTS.md).

O que o Jarvis faz aqui: depois da ata, acrescenta a reunião no histórico da ficha de
cada participante CONFIRMADO pelo senhor; cria a ficha se a pessoa ainda não existe.
Só acrescenta (nunca apaga nem reescreve), com data e fonte; nome ambíguo não grava.
"""
import os
import re
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

import tomllib

from cerebro_io import atomic_write, file_lock
from storage import CONFIG_PATH

ENV_VAR = "JARVIS_CEREBRO_DIR"  # definida (mesmo vazia) prevalece sobre o config.toml; vazia desliga
AMBIGUOUS = "ambíguo"
_SUMMARY_MAX = 400
_PEOPLE_HEADING = "## Pessoas"


def vault_dir() -> Optional[Path]:
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        try:
            with open(CONFIG_PATH, "rb") as config_file:
                raw = tomllib.load(config_file).get("cerebro", {}).get("path", "")
        except (OSError, tomllib.TOMLDecodeError):
            raw = ""
    path = Path(raw) if raw else None
    return path if path and (path / "pessoas").is_dir() else None


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text).casefold()) if not unicodedata.combining(c)).strip()


def _frontmatter(text: str) -> Dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    fields: Dict[str, str] = {}
    for line in text[3:end if end > 0 else 0].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not line.startswith((" ", "\t")):
            fields[key.strip()] = value.split(" #", 1)[0].strip()
    return fields


def _names(path: Path) -> List[str]:
    meta = _frontmatter(path.read_text(encoding="utf-8"))
    names = [path.stem, meta.get("nome", "")]
    names += [a.strip().strip("\"'") for a in meta.get("apelidos", "").strip("[]").split(",")]
    return [n for n in names if n]


def find_person(vault: Path, name: str) -> Union[Path, str, None]:
    """Ficha da pessoa: nome/apelido exato; senão, primeiro nome único. Mais de uma → AMBIGUOUS."""
    key = _fold(name)
    if not key:
        return None
    notes = [p for p in sorted((vault / "pessoas").glob("*.md")) if not p.name.startswith("_")]
    names = {p: [_fold(n) for n in _names(p)] for p in notes}
    exact = [p for p, ns in names.items() if key in ns]
    if not exact and " " not in key:
        exact = [p for p, ns in names.items() if any(n.split()[0] == key for n in ns if n)]
    if len(exact) > 1:
        return AMBIGUOUS
    return exact[0] if exact else None


def _safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")[:80]


def create_person(vault: Path, name: str, day: date, context: str) -> Path:
    path = vault / "pessoas" / f"{_safe_filename(name)}.md"
    template = vault / "_modelos" / "pessoa.md"
    text = template.read_text(encoding="utf-8") if template.exists() else _FALLBACK_TEMPLATE
    iso = day.isoformat()
    text = re.sub(r"(?m)^nome:.*$", f"nome: {name}", text, count=1)
    text = re.sub(r"(?m)^(criado|ultima_interacao):.*$", lambda m: f"{m.group(1)}: {iso}", text)
    text = re.sub(r"(?m)^# .*$", f"# {name}", text, count=1)
    text = re.sub(r"(?m)^- AAAA-MM-DD — .*\n?", "", text)
    text = re.sub(r"(?ms)^(## Quem é)[^\n]*\n.*?(?=^## )", rf"\g<1>\n\n_(a completar — {context})_\n\n", text, count=1)
    text = re.sub(r"(?ms)^(## Pontos de atenção)[^\n]*\n.*?(?=^## )", r"\g<1>\n\n", text, count=1)
    with path.open("x", encoding="utf-8", newline="\n") as note:  # "x": nunca sobrescreve
        note.write(text)
    _add_to_map(vault, name, context)
    return path


def _add_to_map(vault: Path, name: str, context: str) -> None:
    with file_lock(vault / "_mapa.md"):
        _add_to_map_locked(vault, name, context)


def _add_to_map_locked(vault: Path, name: str, context: str) -> None:
    path = vault / "_mapa.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if f"[[{name}]]" in text:
        return
    line = f"- [[{name}]] — a completar ({context})\n"
    start = text.find(_PEOPLE_HEADING)
    if start < 0:
        text = text.rstrip("\n") + f"\n\n{_PEOPLE_HEADING} (`pessoas/`)\n\n{line}"
    else:
        body_start = text.find("\n", start) + 1
        nxt = text.find("\n## ", body_start)
        end = len(text) if nxt < 0 else nxt + 1
        section = text[body_start:end].replace("_(vazio)_\n", "").rstrip("\n")
        text = text[:body_start] + section + "\n" + line + ("\n" if nxt >= 0 else "") + text[end:]
    atomic_write(path, text)


def _append_to_section(text: str, heading: str, line: str) -> str:
    """Acrescenta a linha no fim da seção '## heading' (cria a seção no fim se não existir)."""
    match = re.search(rf"(?m)^## {re.escape(heading)}\s*$", text)
    if not match:
        return text.rstrip("\n") + f"\n\n## {heading}\n\n{line}\n"
    nxt = re.search(r"(?m)^## ", text[match.end():])
    end = match.end() + nxt.start() if nxt else len(text)
    gap = "\n" if text[match.end():end].strip() else "\n\n"  # seção vazia: linha em branco após o título
    return text[:end].rstrip("\n") + gap + line + "\n" + ("\n" + text[end:] if nxt else "")


def add_history(path: Path, line: str, marker: str, day: date, voice_id: str = "") -> bool:
    with file_lock(path):
        return _add_history_locked(path, line, marker, day, voice_id)


def _add_history_locked(path: Path, line: str, marker: str, day: date, voice_id: str = "") -> bool:
    """Acrescenta a linha no '## Histórico' (relendo a nota agora). Marker já presente → não repete."""
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return False
    text = _append_to_section(text, "Histórico", line)
    meta = _frontmatter(text)
    last = meta.get("ultima_interacao", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", last) or last < day.isoformat():
        text = re.sub(r"(?m)^ultima_interacao:.*$", f"ultima_interacao: {day.isoformat()}", text, count=1)
    if voice_id and not meta.get("voz_id"):
        text = re.sub(r"(?m)^voz_id:.*$", f"voz_id: {voice_id}", text, count=1)
    atomic_write(path, text)
    return True


def local_day(utc_ts: str) -> date:
    moment = datetime.strptime(str(utc_ts)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    return moment.replace(tzinfo=timezone.utc).astimezone().date()


def _tasks_for(name: str, tasks: Iterable[str]) -> List[str]:
    key = _fold(name)
    out = []
    for task in tasks:
        who, sep, what = str(task).partition(":")
        if sep and (_fold(who) == key or _fold(who).split()[:1] == key.split()[:1]):
            out.append(what.strip())
    return out


def meeting_note(vault: Path, meeting: Dict[str, Any], ata: Dict[str, Any], day: date, title: str,
                 people: List[str]) -> str:
    """Nota da reunião em reunioes/ com links para os participantes. Já existe: só acrescenta
    participantes que faltam (nunca reescreve). Retorna o nome da nota, para o link."""
    folder = vault / "reunioes"
    folder.mkdir(exist_ok=True)
    existing = next(folder.glob(f"* (reunião {meeting['id']}).md"), None)
    if existing is not None:
        text = existing.read_text(encoding="utf-8")
        for person in people:
            if f"[[{person}]]" not in text:
                text = _append_to_section(text, "Participantes", f"- [[{person}]]")
        existing.write_text(text, encoding="utf-8", newline="\n")
        return existing.stem

    def bullets(items: Iterable[str]) -> str:
        return "\n".join(f"- {i}" for i in items if str(i).strip()) or "_(nenhuma)_"

    name = f"{day.isoformat()} {_safe_filename(title)[:70]} (reunião {meeting['id']})"
    tasks = [*(f"__JARVIS_USER__: {t}" for t in ata.get("tasks_owner") or []), *(ata.get("tasks_others") or [])]
    text = (f"---\ntags: [reuniao]\ndata: {day.isoformat()}\nreuniao_id: {meeting['id']}\nfonte: ata do Jarvis\n---\n\n"
            f"# {title}\n\n{day:%d/%m/%Y}\n\n"
            f"## Participantes\n\n{bullets(f'[[{p}]]' for p in people)}\n\n"
            f"## Resumo\n\n{str(ata.get('summary') or '').strip() or '_(sem resumo)_'}\n\n"
            f"## Decisões\n\n{bullets(ata.get('key_decisions') or [])}\n\n"
            f"## Tarefas\n\n{bullets(tasks)}\n")
    (folder / f"{name}.md").write_text(text, encoding="utf-8", newline="\n")
    return name


def record_meeting(meeting: Dict[str, Any], ata: Dict[str, Any], skip_names: Iterable[str] = (),
                   voices: Optional[List[Dict[str, Any]]] = None, vault: Optional[Path] = None) -> Dict[str, List[str]]:
    vault = vault or vault_dir()
    if vault is None:
        return {"criadas": [], "atualizadas": [], "ambiguas": []}
    with file_lock(vault / "_indice" / "reunioes"):
        return _record_meeting_locked(meeting, ata, skip_names, voices, vault)


def _record_meeting_locked(meeting: dict[str, Any], ata: dict[str, Any], skip_names: Iterable[str] = (),
                           voices: list[dict[str, Any]] | None = None, vault: Path | None = None) -> dict[str, list[str]]:
    """Registra a reunião na ficha de cada participante confirmado. Retorna o que fez com cada nome."""
    vault = vault or vault_dir()
    result: Dict[str, List[str]] = {"criadas": [], "atualizadas": [], "ambiguas": []}
    if vault is None:
        return result
    skip = {_fold(n) for n in skip_names}
    voice_ids = {_fold(v["name"]): v["id"] for v in voices or [] if v.get("id") and v.get("name")}
    day = local_day(meeting["started_at"])
    title = (meeting.get("title") or ata.get("title") or "sem título").strip()
    marker = f"(fonte: reunião #{meeting['id']})"
    summary = re.sub(r"\s+", " ", str(ata.get("summary") or "")).strip()
    if len(summary) > _SUMMARY_MAX:
        summary = summary[:_SUMMARY_MAX].rsplit(" ", 1)[0] + "…"
    people = []
    for name in meeting.get("participants") or []:
        if _fold(name) in skip or _fold(name).split()[0] in skip:
            continue
        found = find_person(vault, name)
        if found == AMBIGUOUS:
            result["ambiguas"].append(name)
            continue
        if found is None:
            found = create_person(vault, name, day, f"participou da reunião “{title}” em {day:%d/%m/%Y}")
            result["criadas"].append(name)
        people.append((name, found))
    if not people:
        return result
    note = meeting_note(vault, meeting, ata, day, title, [path.stem for _, path in people])
    for name, found in people:
        line = f"- {day.isoformat()} — [[{note}|reunião “{title}”]]" + (f": {summary}" if summary else "")
        for task in _tasks_for(name, ata.get("tasks_others") or []):
            line += f" Tarefa: {task}."
        added = add_history(found, f"{line} {marker}", marker, day, voice_ids.get(_fold(name), ""))
        if added and name not in result["criadas"]:
            result["atualizadas"].append(name)
    return result


_FALLBACK_TEMPLATE = """---
nome:
apelidos: []
relacao:
cargo:
organizacao:
whatsapp:
email:
voz_id:
tags: [pessoa]
criado: AAAA-MM-DD
ultima_interacao: AAAA-MM-DD
---

# Nome Sobrenome

## Quem é

## Pontos de atenção

## Histórico

"""
