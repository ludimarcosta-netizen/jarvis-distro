"""
Regras das reuniões do Jarvis.

Ciclo de uma reunião (decidido com o senhor __JARVIS_USER__ em 30/09/2026):
- falas novas entram na reunião em andamento ('open'); sem reunião, abre-se uma;
- 2 min de silêncio numa reunião com fala do microfone: status 'prompting' e o app
  pergunta se encerra; o senhor confirma (encerra), pede para continuar, ou, sem
  resposta em 5 min, ela encerra sozinha;
- fala que volta durante a pergunta cancela a pergunta (a reunião continua);
- trecho só de ruído é descartado; só áudio do sistema (vídeo) encerra sem perguntar;
- ao encerrar, a ata é gerada (Vertex) e as tarefas viram pendências;
- dividir/juntar pela página refaz as atas afetadas;
- ao encerrar, o áudio gravado passa pela passada offline (meetings_offline): vozes separadas,
  texto com contexto e falantes reconhecidos pelo senhor; a ata espera por ela (ou, se falhar,
  sai da transcrição ao vivo).
"""
import calendar
import importlib.util
import json
import os
import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import meetings_offline as offline
import transcript_hygiene as hygiene
import meetings_repo as repo
from storage import DB_PATH, ignore_transcripts

PROMPT_AFTER_S = 120.0        # silêncio que dispara a pergunta "encerrar a reunião?"
AUTO_CLOSE_AFTER_S = 300.0    # sem resposta à pergunta, encerra sozinha
SPLIT_GAP_S = PROMPT_AFTER_S + AUTO_CLOSE_AFTER_S  # fala depois disso já é outra reunião
MIN_CHUNKS = 3
MIN_SPEECH_SECONDS = 45.0
ATA_RETRY_BASE_S = 120.0
ATA_RETRY_MAX_S = 3600.0
LIKELY_MEETING_SPEECH_S = 300.0  # sem voz cadastrada, só pergunta com 5+ min de fala na sala
MEETINGS_URL = f"http://127.0.0.1:{os.environ.get('JARVIS_WEB_PORT', '17850')}"
_BOT_NAMES = {"jarvis", "j.a.r.v.i.s.", "hermes", "gemini", "assistente"}
_OWNER_NAMES = {"__JARVIS_USER_LOWER__"}  # o senhor está em todas: não ocupa vaga de sugestão
_TS = "%Y-%m-%d %H:%M:%S"


def _now() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _parse(value: str) -> datetime:
    return datetime.strptime(str(value)[:19].replace("T", " "), _TS)


def _fmt(value: datetime) -> str:
    return value.strftime(_TS)


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text).casefold()) if not unicodedata.combining(c))


# ── ciclo automático (chamado pelo rastreador a cada ~15 s) ──────────────────────

def tick(now: Optional[datetime] = None, db_path: str = DB_PATH,
         extract: Optional[Callable] = None, register: Optional[Callable] = None) -> None:
    now = now or _now()
    active = repo.get_active_meeting(db_path)
    for row in repo.unassigned_transcripts(db_path):
        ts = _parse(row["timestamp"])
        if active and (ts - _parse(active["ended_at"])).total_seconds() >= SPLIT_GAP_S:
            close_meeting(active["id"], closed_by="auto", db_path=db_path, extract=extract, register=register, now=now)
            active = None
        if active is None:
            mid = repo.create_meeting(started_at=row["timestamp"], db_path=db_path)
            active = repo.get_meeting(mid, db_path)
        repo.assign_transcripts([row["id"]], active["id"], db_path=db_path)
        changes: Dict[str, Any] = {"ended_at": row["timestamp"]}
        if active["status"] == "prompting" and active["prompt_at"] and ts > _parse(active["prompt_at"]):
            changes.update(status="open", prompt_at=None)  # a conversa voltou: não encerra
        repo.update_meeting(active["id"], db_path=db_path, **changes)
        active = repo.get_meeting(active["id"], db_path)

    if active:
        _evaluate_silence(active, now, db_path, extract, register)
    for mid in repo.meetings_to_retry_ata(_fmt(now), db_path):
        generate_ata(mid, db_path=db_path, extract=extract, register=register, now=now)
    offline.kick(db_path)


def _stats(meeting_id: int, db_path: str) -> Dict[str, Any]:
    rows = repo.meeting_transcripts(meeting_id, db_path)
    return {
        "chunks": len(rows),
        "speech": sum(float(r.get("duration_seconds") or 0) for r in rows),
        "mic": sum(1 for r in rows if r.get("source") == "mic"),
    }


def _evaluate_silence(meeting: Dict[str, Any], now: datetime, db_path: str, extract, register) -> None:
    idle = (now - _parse(meeting["ended_at"])).total_seconds()
    if idle < PROMPT_AFTER_S:
        return
    stats = _stats(meeting["id"], db_path)
    substantial = stats["chunks"] >= MIN_CHUNKS or stats["speech"] >= MIN_SPEECH_SECONDS
    if not substantial:
        discard_meeting(meeting["id"], db_path=db_path)
    elif stats["mic"] == 0:
        # Só áudio do sistema (vídeo, live): ninguém da sala falou, não há a quem perguntar.
        close_meeting(meeting["id"], closed_by="auto", db_path=db_path, extract=extract, register=register, now=now)
    elif meeting["status"] == "open":
        dismissed = meeting.get("prompt_dismissed_at")
        if not dismissed or (now - _parse(dismissed)).total_seconds() >= PROMPT_AFTER_S:
            repo.update_meeting(meeting["id"], db_path=db_path, status="prompting", prompt_at=_fmt(now))
    elif meeting["status"] == "prompting" and meeting.get("prompt_at"):
        if (now - _parse(meeting["prompt_at"])).total_seconds() >= AUTO_CLOSE_AFTER_S:
            close_meeting(meeting["id"], closed_by="auto", db_path=db_path, extract=extract, register=register,
                          now=now)


# ── ações do senhor (página ou app) ─────────────────────────────────────────────

def pending_prompt(db_path: str = DB_PATH, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Pergunta "encerrar a reunião?" em aberto, com sugestões de participantes."""
    active = repo.get_active_meeting(db_path)
    if not active or active["status"] != "prompting":
        return None
    now = now or _now()
    remaining = AUTO_CLOSE_AFTER_S - (now - _parse(active["prompt_at"])).total_seconds()
    return {"meeting": active, "auto_close_in_s": max(0, int(remaining)),
            "suggestions": suggest_participants(active["id"], db_path=db_path)}


def continue_meeting(meeting_id: int, db_path: str = DB_PATH, now: Optional[datetime] = None) -> None:
    meeting = _require(meeting_id, db_path)
    if meeting["status"] == "closed":
        raise ValueError("A reunião já foi encerrada.")
    repo.update_meeting(meeting_id, db_path=db_path, status="open", prompt_at=None,
                        prompt_dismissed_at=_fmt(now or _now()))


def close_meeting(meeting_id: int, closed_by: str = "user", participants: Optional[List[str]] = None,
                  db_path: str = DB_PATH, extract=None, register=None, now: Optional[datetime] = None,
                  defer_ata: bool = False) -> Dict[str, Any]:
    """Encerra e gera a ata. defer_ata=True só marca a ata como pendente (a página
    responde na hora e a ata é feita em segundo plano)."""
    meeting = _require(meeting_id, db_path)
    fields: Dict[str, Any] = {"status": "closed", "prompt_at": None, "closed_by": closed_by}
    if participants is not None:
        fields["participants"] = _clean_names(participants)
    span = repo.bounds(meeting_id, db_path)
    if span:
        fields.update(started_at=span[0], ended_at=span[1])
    repo.update_meeting(meeting_id, db_path=db_path, **fields)
    repo.mark_meeting_processed(meeting_id, db_path)
    if meeting["status"] != "closed":
        _queue_offline(repo.get_meeting(meeting_id, db_path), db_path)
    if meeting["status"] != "closed" or participants is not None:
        if defer_ata:
            repo.update_meeting(meeting_id, db_path=db_path, ata_status="pending", ata_attempts=0, ata_next_try_at=None)
        else:
            generate_ata(meeting_id, db_path=db_path, extract=extract, register=register, now=now)
    return repo.get_meeting(meeting_id, db_path)


def discard_meeting(meeting_id: int, db_path: str = DB_PATH) -> None:
    """Ruído isolado: as falas ficam no banco (processadas), sem reunião nem ata."""
    ids = [r["id"] for r in repo.meeting_transcripts(meeting_id, db_path)]
    repo.assign_transcripts(ids, None, processed=True, db_path=db_path)
    repo.delete_meeting(meeting_id, db_path)


def set_participants(meeting_id: int, names: Iterable[str], db_path: str = DB_PATH) -> Dict[str, Any]:
    _require(meeting_id, db_path)
    repo.update_meeting(meeting_id, db_path=db_path, participants=_clean_names(names))
    _record_in_cerebro(meeting_id, db_path)
    return repo.get_meeting(meeting_id, db_path)


def set_title(meeting_id: int, title: str, db_path: str = DB_PATH) -> Dict[str, Any]:
    _require(meeting_id, db_path)
    repo.update_meeting(meeting_id, db_path=db_path, title=str(title or "").strip()[:200])
    return repo.get_meeting(meeting_id, db_path)


def move_transcripts(source_id: int, transcript_ids: Iterable[int], target_id: Optional[int] = None,
                     new_title: str = "", db_path: str = DB_PATH, extract=None, register=None,
                     defer_ata: bool = False) -> Dict[str, Any]:
    """Envia falas selecionadas para outra reunião existente ou para uma nova."""
    source = _require(source_id, db_path)
    owned = {r["id"]: r for r in repo.meeting_transcripts(source_id, db_path)}
    ids = [int(i) for i in transcript_ids]
    if not ids or any(i not in owned for i in ids):
        raise ValueError("Selecione falas desta reunião.")
    if target_id is not None:
        target = _require(int(target_id), db_path)
        if target["id"] == source_id:
            raise ValueError("Escolha uma reunião diferente.")
    else:
        moved = sorted(owned[i]["timestamp"] for i in ids)
        new_id = repo.create_meeting(started_at=moved[0], ended_at=moved[-1], title=str(new_title or "").strip(),
                                     status="closed", closed_by="user", db_path=db_path)
        target = repo.get_meeting(new_id, db_path)
    repo.assign_transcripts(ids, target["id"], processed=target["status"] == "closed", db_path=db_path)
    _after_change(source, db_path, extract, register, fallback_ata_owner=target["id"], defer_ata=defer_ata)
    _after_change(target, db_path, extract, register, defer_ata=defer_ata)
    return {"source": repo.get_meeting(source_id, db_path), "target": repo.get_meeting(target["id"], db_path)}


def delete_meeting(meeting_id: int, db_path: str = DB_PATH) -> None:
    """O senhor excluiu uma reunião sem nada importante: some com a ata, as vozes e as falas da lista.
    As falas ficam no banco como ignoradas (não voltam a formar outra reunião) e as pendências
    continuam, só perdem o vínculo com a ata."""
    meeting = _require(meeting_id, db_path)
    if meeting["status"] in ("open", "prompting"):
        raise ValueError("Encerre a reunião antes de excluir.")
    _drop_offline(meeting_id, db_path)
    ignore_transcripts([r["id"] for r in repo.meeting_transcripts(meeting_id, db_path)],
                       "reunião excluída pelo senhor", db_path)
    repo.delete_meeting(meeting_id, db_path)


def merge_meetings(target_id: int, other_id: int, db_path: str = DB_PATH,
                   extract=None, register=None, defer_ata: bool = False) -> Dict[str, Any]:
    """Junta 'other' em 'target': falas e participantes; 'other' deixa de existir."""
    target, other = _require(target_id, db_path), _require(other_id, db_path)
    if target_id == other_id:
        raise ValueError("Escolha uma reunião diferente.")
    ids = [r["id"] for r in repo.meeting_transcripts(other_id, db_path)]
    repo.assign_transcripts(ids, target_id, processed=target["status"] == "closed", db_path=db_path)
    repo.update_meeting(target_id, db_path=db_path,
                        participants=_clean_names([*target["participants"], *other["participants"]]))
    _after_change(other, db_path, extract, register, fallback_ata_owner=target_id, defer_ata=defer_ata)
    _after_change(target, db_path, extract, register, defer_ata=defer_ata)
    return repo.get_meeting(target_id, db_path)


def _after_change(meeting: Dict[str, Any], db_path: str, extract, register,
                  fallback_ata_owner: Optional[int] = None, defer_ata: bool = False) -> None:
    _drop_offline(meeting["id"], db_path)  # as falas mudaram de reunião: o áudio já não bate com elas
    span = repo.bounds(meeting["id"], db_path)
    if span is None:  # ficou vazia: some, e as pendências vão para a ata de quem recebeu as falas
        if fallback_ata_owner is not None:
            owner_ata = repo.get_summary(fallback_ata_owner, db_path)
            repo.repoint_pendencias(meeting["id"], owner_ata["id"] if owner_ata else None, db_path)
        repo.delete_meeting(meeting["id"], db_path)
        return
    repo.update_meeting(meeting["id"], db_path=db_path, started_at=span[0], ended_at=span[1])
    if meeting["status"] == "closed":
        repo.update_meeting(meeting["id"], db_path=db_path, ata_status="pending", ata_attempts=0,
                            ata_next_try_at=None)
        if not defer_ata:
            generate_ata(meeting["id"], db_path=db_path, extract=extract, register=register)


def generate_ata(meeting_id: int, db_path: str = DB_PATH, extract=None, register=None,
                 now: Optional[datetime] = None) -> bool:
    """Extrai a ata (Vertex) e registra pendências. Falha volta com espera crescente."""
    if extract is None:
        from context_extractor import extract_structured as extract
    if register is None:
        from meeting_tracker import register_pendencias as register
    meeting = _require(meeting_id, db_path)
    if meeting.get("offline_status") in ("pending", "running"):
        return False  # a passada offline termina e libera a ata; não conta como falha
    rows = _ata_rows(meeting, db_path)
    if not rows:  # só sobrou ruído/eco ignorado: não há o que resumir (e não fica sendo tentada)
        repo.delete_summary(meeting_id, db_path)
        repo.update_meeting(meeting_id, db_path=db_path, ata_status="empty", ata_next_try_at=None)
        return False
    first_ata = repo.get_summary(meeting_id, db_path) is None
    try:
        ata = extract(rows)
    except Exception as exc:  # noqa: BLE001 — rede/cota: a reunião continua válida, a ata espera
        attempts = int(meeting.get("ata_attempts") or 0) + 1
        delay = min(ATA_RETRY_BASE_S * 2 ** (attempts - 1), ATA_RETRY_MAX_S)
        repo.update_meeting(meeting_id, db_path=db_path, ata_status="error", ata_attempts=attempts,
                            ata_next_try_at=_fmt((now or _now()) + timedelta(seconds=delay)))
        print(f"[!] Ata da reunião {meeting_id} falhou (tentativa {attempts}; nova em {delay / 60:.0f} min): {exc}")
        return False
    if meeting.get("participants"):
        ata["participants"] = list(meeting["participants"])  # confirmados pelo senhor prevalecem
    ata_id = repo.upsert_summary(meeting_id, ata, rows[0]["timestamp"], rows[-1]["timestamp"], db_path)
    changes: Dict[str, Any] = {"ata_status": "ok", "ata_attempts": 0, "ata_next_try_at": None}
    if not meeting.get("title"):
        changes["title"] = ata.get("title") or ""
    repo.update_meeting(meeting_id, db_path=db_path, **changes)
    _record_in_cerebro(meeting_id, db_path, ata)
    if first_ata:  # refazer a ata (dividir/juntar/limpeza) não cria pendência nova de reunião antiga
        try:
            register(ata, ata_id=ata_id, db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Pendências da reunião {meeting_id} não registradas: {exc}")
        _ask_participants(meeting_id, db_path, rows, ata)
    return True


def generate_pending_atas(db_path: str = DB_PATH, extract=None, register=None) -> int:
    """Gera agora as atas pendentes (sem esperar o rastreador). Retorna quantas saíram."""
    done = 0
    for mid in repo.meetings_to_retry_ata(_fmt(_now()), db_path):
        done += bool(generate_ata(mid, db_path=db_path, extract=extract, register=register))
    return done


def handle_spoken_command(kind: str, name: str = "", db_path: str = DB_PATH, extract=None, register=None) -> None:
    """'Jarvis, encerrar reunião' / 'Jarvis, iniciando reunião com X'."""
    tick(db_path=db_path, extract=extract, register=register)  # a frase do comando entra na reunião atual
    active = repo.get_active_meeting(db_path)
    if active:
        close_meeting(active["id"], closed_by="user", db_path=db_path, extract=extract, register=register)
    if kind == "start":
        label = str(name or "").strip()
        repo.create_meeting(started_at=_fmt(_now()), title=label,
                            participants=_clean_names([label]) if label else [], db_path=db_path)


# ── passada offline: vozes, falas por falante e ata ──────────────────────────────

def _queue_offline(meeting: Dict[str, Any], db_path: str) -> None:
    """Reunião recém-encerrada com áudio gravado entra na fila da passada offline (a ata espera por ela)."""
    if offline.enabled() and offline.has_audio(meeting):
        repo.update_meeting(meeting["id"], db_path=db_path, offline_status="pending", offline_attempts=0,
                            offline_next_try_at=None)


def _drop_offline(meeting_id: int, db_path: str) -> None:
    for old in repo.clear_offline(meeting_id, db_path):
        (offline.SAMPLES_DIR / old).unlink(missing_ok=True)
    repo.update_meeting(meeting_id, db_path=db_path, offline_status="none", offline_attempts=0,
                        offline_next_try_at=None)


def reprocess_offline(meeting_id: int, db_path: str = DB_PATH) -> Dict[str, Any]:
    meeting = _require(meeting_id, db_path)
    if meeting["status"] != "closed":
        raise ValueError("Encerre a reunião antes de reprocessar o áudio.")
    if not offline.has_audio(meeting):
        raise ValueError("O áudio desta reunião já foi apagado (ele é guardado por 72 h).")
    repo.update_meeting(meeting_id, db_path=db_path, offline_status="pending", offline_attempts=0,
                        offline_next_try_at=None)
    return repo.get_meeting(meeting_id, db_path)


def _effective(voices: Dict[int, Dict[str, Any]], voice_id: int) -> Dict[str, Any]:
    """Voz final depois de seguir as junções ("é a mesma pessoa que…"). Ciclo = para onde parou."""
    seen = set()
    voice = voices[voice_id]
    while voice.get("merged_into") in voices and voice["id"] not in seen:
        seen.add(voice["id"])
        voice = voices[voice["merged_into"]]
    return voice


def voice_view(meeting_id: int, db_path: str = DB_PATH) -> Dict[str, Any]:
    """Vozes com nome de exibição ("Voz 2"/"Chamada 1" enquanto o senhor não nomeia) e as falas por falante."""
    rows = repo.meeting_voices(meeting_id, db_path)
    by_id = {v["id"]: v for v in rows}
    counters: Counter = Counter()
    for v in rows:
        evidence = json.loads(v.pop("evidence") or "null")
        v["quality"] = ({"clean_seconds": evidence["clean_seconds"], "windows": evidence["windows"]}
                        if evidence else None)
        counters[v["source"]] += 1
        v["fallback"] = ("Falante desconhecido" if v["label"] == -1 else
                         f"{'Voz' if v['source'] == 'mic' else 'Chamada'} {counters[v['source']]}")
    for v in rows:
        final = _effective(by_id, v["id"])
        v["display_name"] = (final.get("name") or "").strip() or final["fallback"]
        v["effective_id"] = final["id"]
        v["is_discarded"] = bool(final.get("discarded"))
        v["has_sample"] = bool(v.get("sample_file"))
    utterances = [{"id": u["id"], "voice_id": u["voice_id"], "start_at": u["start_at"], "end_at": u["end_at"],
                   "text": u["text"], "review": u.get("review"), "speaker": by_id[u["voice_id"]]["display_name"],
                   "discarded": by_id[u["voice_id"]]["is_discarded"]}
                  for u in repo.meeting_utterances(meeting_id, db_path) if u["voice_id"] in by_id]
    return {"voices": rows, "utterances": utterances}


def _ata_rows(meeting: Dict[str, Any], db_path: str) -> List[Dict[str, Any]]:
    """Falas que alimentam a ata: por falante quando a passada offline terminou; senão, as ao vivo."""
    if meeting.get("offline_status") != "done":
        return [r for r in repo.meeting_transcripts(meeting["id"], db_path)
                if not hygiene.hallucination_reason(r.get("text"))]
    rows: List[Dict[str, Any]] = []
    for u in voice_view(meeting["id"], db_path)["utterances"]:
        if u["discarded"] or not u["text"] or hygiene.hallucination_reason(u["text"]):
            continue
        if (u.get("review") or {}).get("status") == "pending":
            u["text"] = "[Trecho duvidoso, ainda não conferido] " + u["text"]
        if rows and rows[-1]["speaker"] == u["speaker"]:
            rows[-1]["text"] += " " + u["text"]
        else:
            rows.append({"timestamp": u["start_at"], "source": u["speaker"], "speaker": u["speaker"],
                         "text": u["text"]})
    return rows


def set_voices(meeting_id: int, items: Iterable[Dict[str, Any]], db_path: str = DB_PATH,
               *, confirmed: bool = True) -> Dict[str, Any]:
    """O senhor nomeou, juntou ou descartou vozes: salva, atualiza os participantes e refaz a ata."""
    meeting = _require(meeting_id, db_path)
    owned = {v["id"] for v in repo.meeting_voices(meeting_id, db_path)}
    updates = []
    for item in items:
        voice_id = int(item["id"])
        merge = item.get("merge_into")
        merge = int(merge) if merge not in (None, "", 0) else None
        if voice_id not in owned or (merge is not None and (merge not in owned or merge == voice_id)):
            raise ValueError("Voz inválida para esta reunião.")
        updates.append((voice_id, str(item.get("name") or "").strip()[:80], merge, bool(item.get("discarded"))))
    for voice_id, name, merge, discarded in updates:
        repo.update_voice(voice_id, name, merge, discarded, db_path)
        if confirmed:
            repo.confirm_voice_reference(voice_id, name, db_path)
    view = voice_view(meeting_id, db_path)
    named = [v["display_name"] for v in view["voices"]
             if v["id"] == v["effective_id"] and not v["is_discarded"] and v["display_name"] != v["fallback"]
             and _fold(v["display_name"]) not in _OWNER_NAMES]
    repo.update_meeting(meeting_id, db_path=db_path, participants=_clean_names([*meeting["participants"], *named]),
                        ata_status="pending", ata_attempts=0, ata_next_try_at=None)
    _record_in_cerebro(meeting_id, db_path)
    return repo.get_meeting(meeting_id, db_path)


def set_speaker_counts(meeting_id: int, body: dict, db_path: str = DB_PATH) -> dict:
    meeting = _require(meeting_id, db_path)
    if meeting["offline_status"] in {"pending", "running"}:
        raise ValueError("Aguarde o processamento do áudio terminar.")
    if not body or set(body) - {"mic_speakers", "loopback_speakers"}:
        raise ValueError("Configuração de vozes inválida.")
    values = {}
    for source in ("mic", "loopback"):
        if f"{source}_speakers" not in body:
            continue
        value = body.get(f"{source}_speakers")
        if value is not None and (type(value) is not int or not 1 <= value <= 20):
            raise ValueError("Informe de 1 a 20 vozes, ou deixe automático.")
        values[f"{source}_speakers"] = value
    repo.update_meeting(meeting_id, db_path=db_path, **values)
    return repo.get_meeting(meeting_id, db_path)


# ── consulta e sugestões ────────────────────────────────────────────────────────

def meeting_detail(meeting_id: int, db_path: str = DB_PATH) -> Dict[str, Any]:
    meeting = _require(meeting_id, db_path)
    voices = voice_view(meeting_id, db_path)
    return {"meeting": meeting, "transcripts": repo.meeting_transcripts(meeting_id, db_path),
            "ignored": repo.ignored_count(meeting_id, db_path),
            "ata": repo.get_summary(meeting_id, db_path),
            "voices": voices["voices"], "utterances": voices["utterances"],
            "suggestions": suggest_participants(meeting_id, db_path=db_path)}


def suggest_participants(meeting_id: int, db_path: str = DB_PATH,
                         known_people: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Sugestões, da fonte mais confiável para a menos: "a reunião foi com X" (dito pelo
    senhor), participantes da ata, pessoas conhecidas citadas e nomes próprios detectados.
    Dentro de cada fonte, por número de menções. Exclui confirmados, o senhor e o assistente."""
    meeting = _require(meeting_id, db_path)
    rows = repo.meeting_transcripts(meeting_id, db_path)
    text = _fold(" ".join(r.get("text") or "" for r in rows))
    chosen = {_fold(n) for n in meeting["participants"]}
    scores: Counter = Counter()
    reasons: Dict[str, str] = {}
    display: Dict[str, str] = {}

    def add(name: str, reason: str, weight: int = 1) -> None:
        name = str(name or "").strip()
        key = _fold(name)
        if len(key) < 2 or key in _BOT_NAMES or key in _OWNER_NAMES or key in chosen:
            return
        scores[key] += weight
        reasons.setdefault(key, reason)
        display.setdefault(key, name)

    ata = repo.get_summary(meeting_id, db_path)
    for name in (ata or {}).get("participants") or []:
        add(name, "identificado na ata", 2000)
    for name, hits in mentioned_names(r.get("text") or "" for r in rows).items():
        add(name, "citado na conversa", min(hits, 99))
    people = known_people if known_people is not None else _known_people(db_path)
    for name in people:
        hits = len(re.findall(r"(?<!\w)" + re.escape(_fold(name)) + r"(?!\w)", text))
        if hits:
            add(name, "citado na conversa", 1000 + min(hits, 99))
    for voice in _heard_voices(meeting):
        if voice.get("count", 0) >= 2:  # um trecho só pode ser engano do reconhecimento
            add(voice["name"], "voz reconhecida", 2500 + min(int(voice["count"]), 99))
    hint = _participant_hint()
    if hint:
        for r in rows:
            if r.get("source") == "mic":
                found = hint(r.get("text") or "")
                if found:
                    add(found, "dito: “a reunião foi com…”", 3000)
    return [{"name": display[k], "reason": reasons[k], "score": scores[k]} for k, _ in scores.most_common(8)]


_WORD = r"[A-ZÀ-Ý][a-zà-ÿ]{2,}"
_VOCATIVE_RE = re.compile(rf"(?:^|[.!?]\s+)({_WORD}),")                  # "Celina, você manda…"
_MIDSENTENCE_RE = re.compile(rf"(?<=[a-zà-ÿ,] )({_WORD})(?=[\s,.!?]|$)")  # "…e o Maxwell responde"
_NOT_NAMES = {_fold(w) for w in (
    "Jarvis Hermes Gemini Senhor Senhora Doutor Doutora Deus Brasil __JARVIS_CIDADE_CURTA__ Céu Goiás Prefeitura Secretaria "
    "Câmara Estado Município Governo Então Sim Não Bom Boa Olha Gente Pessoal Isso Aquilo Mas Agora Hoje Ontem "
    "Amanhã Obrigado Obrigada Tudo Nada Claro Certo Beleza Vamos Pode Também Aqui Ali Depois Antes Porque Quando "
    "Onde Como Qual Quem Ainda Muito Pouco Vou Vai Tem Está Estou Era Foi Ser Estar Fazer WhatsApp Google YouTube "
    "Janeiro Fevereiro Março Abril Maio Junho Julho Agosto Setembro Outubro Novembro Dezembro Segunda Terça Quarta "
    "Quinta Sexta Sábado Domingo Edital Ofício Contrato Lei Decreto Portaria Reunião Projeto Sistema "
    "Saúde Educação Obras Finanças Fazenda Administração Assistência Social Cultura Esporte Turismo Planejamento "
    "Agricultura Meio Ambiente Transporte Habitação Segurança Conselho Comissão Distrito Industrial "
    # interjeições e vocativos comuns na fala, e alucinações conhecidas do Whisper
    "Cara Nossa Rapaz Fala Falei Falou Aham Uhum Hum Tchau Assim Mãe Pai Amigo Amiga Irmão Irmã Moço Moça Menino "
    "Menina Velho Bicho Uai Opa Olá Oi Mestre Chefe Legendas Amara Obrigado Valeu Constitucional").split()}
_COMPOUND_TAIL_RE = re.compile(r"[A-ZÀ-Ý][\wÀ-ÿ]* d[aeo]s? $")  # "Secretaria de " + Saúde


def mentioned_names(texts: Iterable[str]) -> Dict[str, int]:
    """Nomes próprios citados: vocativo no início da frase ou maiúscula no meio dela.
    Heurística de sugestão (o senhor confirma); nunca vira participante sozinha."""
    counts: Counter = Counter()
    shown: Dict[str, str] = {}
    for text in texts:
        for match in [*_VOCATIVE_RE.finditer(text or ""), *_MIDSENTENCE_RE.finditer(text or "")]:
            word = match.group(1)
            key = _fold(word)
            if key in _NOT_NAMES or key in _BOT_NAMES:
                continue
            if _COMPOUND_TAIL_RE.search((text or "")[:match.start(1)]):
                continue  # parte de nome composto de lugar/órgão
            counts[key] += 1
            shown.setdefault(key, word)
    return {shown[k]: n for k, n in counts.items()}


def _known_people(db_path: str) -> List[str]:
    names = list(dict.fromkeys(repo.confirmed_participants(db_path)))
    user_md = Path(__file__).resolve().parents[1] / "memories" / "USER.md"
    if user_md.exists():
        names.extend(n for n in people_from_user_md(user_md.read_text(encoding="utf-8")) if n not in names)
    return names


_NAME_RE = re.compile(r"\b[A-ZÀ-Ý][a-zà-ÿ]+(?:\s+(?:d[aeo]s?\s+)?[A-ZÀ-Ý][a-zà-ÿ]+){0,3}")
_NOT_PEOPLE = {"esposa", "filhos", "filha", "filho", "pai", "mae", "irma", "irmao", "familia", "pessoas",
               "prefeito", "__JARVIS_CIDADE_FOLD__", "__JARVIS_CIDADE_CURTA_FOLD__", "ceu", "dr", "in memoriam", "amor", "whatsapp"}


def people_from_user_md(text: str) -> List[str]:
    """Nomes das linhas de família e pessoas recorrentes do USER.md da memória do Hermes."""
    names: List[str] = []
    for entry in text.split("§"):
        if not re.search(r"(?i)\b(pessoas|fam[ií]lia|contatos)\b", entry):
            continue
        body = re.sub(r"\([^)]*\)", " ", entry.split(":", 1)[-1])
        for match in _NAME_RE.finditer(body):
            name = match.group(0).strip()
            first = _fold(name.split()[0])
            if _fold(name) in _NOT_PEOPLE or first in _NOT_PEOPLE:
                parts = name.split()[1:]
                name = " ".join(parts)
            if name and _fold(name) not in _NOT_PEOPLE and name not in names:
                names.append(name)
    return names


def _participant_hint() -> Optional[Callable[[str], str]]:
    path = Path(__file__).resolve().parents[1] / "voice_identity" / "hints.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("jarvis_voice_hints", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, "meeting_participant_name", None)


_voice_client = None


def _heard_voices(meeting: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Vozes cadastradas reconhecidas durante a reunião (estimativa; o dono fica de fora)."""
    global _voice_client
    if _voice_client is None:
        path = Path(__file__).resolve().parents[1] / "voice_identity" / "client.py"
        if not path.exists():
            return []
        spec = importlib.util.spec_from_file_location("jarvis_voice_client", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _voice_client = module.client
    since = calendar.timegm(_parse(meeting["started_at"]).timetuple()) - 60
    until = calendar.timegm(_parse(meeting["ended_at"] or meeting["started_at"]).timetuple()) + 60
    try:
        return [v for v in _voice_client.heard(since, until) if not v.get("owner")]
    except Exception:  # noqa: BLE001 — serviço de voz é opcional: sem ele, sem sugestão por voz
        return []


def _record_in_cerebro(meeting_id: int, db_path: str, ata: Optional[Dict[str, Any]] = None) -> None:
    """Acrescenta a reunião nas fichas dos participantes confirmados (Segundo Cérebro).
    Falha aqui nunca derruba a reunião nem a ata."""
    try:
        import cerebro
        meeting = repo.get_meeting(meeting_id, db_path)
        ata = ata or repo.get_summary(meeting_id, db_path)
        if not meeting or not ata or not meeting["participants"] or cerebro.vault_dir() is None:
            return
        done = cerebro.record_meeting(meeting, ata, skip_names=_OWNER_NAMES | _BOT_NAMES,
                                      voices=_heard_voices(meeting))
        if any(done.values()):
            print(f"[Cérebro] Reunião {meeting_id}: criadas {done['criadas']}, atualizadas {done['atualizadas']}, "
                  f"ambíguas (não gravadas) {done['ambiguas']}")
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Cérebro: reunião {meeting_id} não registrada nas fichas: {exc}")


def likely_meeting(rows: List[Dict[str, Any]], ata: Dict[str, Any], voices: List[Dict[str, Any]]) -> bool:
    """Reunião de verdade (vale perguntar com quem foi): outra voz cadastrada ouvida em 2+ trechos,
    ou 5+ min de fala na sala com alguém citado. Monólogo, conversa curta e vídeo ficam de fora."""
    if any(v.get("count", 0) >= 2 for v in voices):
        return True
    mic = [r for r in rows if r.get("source") == "mic"]
    if sum(float(r.get("duration_seconds") or 0) for r in mic) < LIKELY_MEETING_SPEECH_S:
        return False
    named = [n for n in ata.get("participants") or [] if _fold(n) not in _BOT_NAMES | _OWNER_NAMES
             and _fold(n).split()[:1] != ["__JARVIS_USER_LOWER__"]]
    return bool(named or mentioned_names(r.get("text") or "" for r in mic))


def _notify(title: str, body: str, url: str, button: str) -> bool:
    if os.environ.get("JARVIS_NOTIFICACOES", "1") == "0":  # testes desligam
        return False
    from notifier import notify
    return notify(title, body, url, button)


def _ask_participants(meeting_id: int, db_path: str, rows: List[Dict[str, Any]], ata: Dict[str, Any]) -> None:
    """Reunião provável encerrada sem participantes: notificação do Windows que abre a reunião na
    página para o senhor confirmar com quem foi (as sugestões já aparecem lá)."""
    try:
        meeting = repo.get_meeting(meeting_id, db_path)
        if not meeting or meeting["participants"] or not likely_meeting(rows, ata, _heard_voices(meeting)):
            return
        names = [s["name"] for s in suggest_participants(meeting_id, db_path=db_path)][:4]
        start, end = (_parse(meeting[k]).replace(tzinfo=timezone.utc).astimezone() for k in ("started_at", "ended_at"))
        body = f"{start:%d/%m %H:%M}–{end:%H:%M}. " + (f"Sugestões: {', '.join(names)}." if names else "Sem sugestões.")
        _notify(f"Com quem foi a reunião “{meeting.get('title') or 'sem título'}”?", body,
                f"{MEETINGS_URL}/#{meeting_id}", "Informar participantes")
    except Exception as exc:  # noqa: BLE001 — aviso é conveniência: nunca derruba a ata
        print(f"[!] Aviso de participantes da reunião {meeting_id} falhou: {exc}")


def _clean_names(names: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for name in names:
        name = re.sub(r"\s+", " ", str(name or "")).strip()[:80]
        if name and _fold(name) not in seen:
            seen.add(_fold(name))
            out.append(name)
    return out


def _require(meeting_id: int, db_path: str) -> Dict[str, Any]:
    meeting = repo.get_meeting(int(meeting_id), db_path)
    if meeting is None:
        raise LookupError("Reunião não encontrada.")
    return meeting


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
