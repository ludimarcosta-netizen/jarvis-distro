"""
Acesso a dados das reuniões (tabelas meetings, transcripts, meeting_summaries, pendencias).
Só SQL; as regras (silêncio, divisão, junção, ata) ficam em meetings_service.
Horários no formato SQLite UTC 'YYYY-MM-DD HH:MM:SS'.
"""
import json
import sqlite3
import time
from contextlib import closing
from typing import Any, Dict, Iterable, List, Optional

from storage import DB_PATH, _ensure_db

_MEETING_COLUMNS = ("id, title, started_at, ended_at, status, participants, prompt_at, prompt_dismissed_at, "
                    "closed_by, ata_status, ata_attempts, ata_next_try_at, created_at, "
                    "offline_status, offline_attempts, offline_next_try_at, mic_speakers, loopback_speakers")


def _connect(db_path: str) -> sqlite3.Connection:
    _ensure_db(db_path)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _meeting(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    item = dict(row)
    try:
        item["participants"] = json.loads(item.get("participants") or "[]")
    except ValueError:
        item["participants"] = []
    return item


def get_meeting(meeting_id: int, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    with closing(_connect(db_path)) as conn:
        return _meeting(conn.execute(f"SELECT {_MEETING_COLUMNS} FROM meetings WHERE id = ?",
                                     (meeting_id,)).fetchone())


def get_active_meeting(db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Reunião em andamento ('open' ou 'prompting'); no máximo uma por vez."""
    with closing(_connect(db_path)) as conn:
        return _meeting(conn.execute(f"""
            SELECT {_MEETING_COLUMNS} FROM meetings WHERE status IN ('open', 'prompting')
            ORDER BY id DESC LIMIT 1""").fetchone())


def list_meetings(since: Optional[str] = None, until: Optional[str] = None, limit: int = 500,
                  db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    clauses, params = [], []
    if since:
        clauses.append("m.started_at >= ?")
        params.append(since)
    if until:
        clauses.append("m.started_at < ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(f"""
            SELECT m.id, m.title, m.started_at, m.ended_at, m.status, m.participants, m.prompt_at,
                   m.closed_by, m.ata_status, m.offline_status, s.title AS ata_title,
                   (SELECT COUNT(*) FROM transcripts t WHERE t.meeting_id = m.id AND t.ignored = 0) AS chunks
            FROM meetings m LEFT JOIN meeting_summaries s ON s.meeting_id = m.id
            {where} ORDER BY m.started_at DESC LIMIT ?""", (*params, limit)).fetchall()
    return [_meeting(r) for r in rows]


def create_meeting(started_at: str, ended_at: Optional[str] = None, title: str = "", status: str = "open",
                   participants: Optional[List[str]] = None, closed_by: Optional[str] = None,
                   db_path: str = DB_PATH) -> int:
    with closing(_connect(db_path)) as conn, conn:
        cur = conn.execute("""
            INSERT INTO meetings (title, started_at, ended_at, status, participants, closed_by)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (title, started_at, ended_at or started_at, status,
             json.dumps(participants or [], ensure_ascii=False), closed_by))
        return cur.lastrowid


_UPDATABLE = {"title", "started_at", "ended_at", "status", "participants", "prompt_at", "prompt_dismissed_at",
              "closed_by", "ata_status", "ata_attempts", "ata_next_try_at", "offline_status",
              "offline_attempts", "offline_next_try_at", "mic_speakers", "loopback_speakers"}


def update_meeting(meeting_id: int, db_path: str = DB_PATH, **fields: Any) -> None:
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"campos desconhecidos: {sorted(unknown)}")
    if "participants" in fields:
        fields["participants"] = json.dumps(list(fields["participants"] or []), ensure_ascii=False)
    sets = ", ".join(f"{name} = ?" for name in fields)
    with closing(_connect(db_path)) as conn, conn:
        conn.execute(f"UPDATE meetings SET {sets} WHERE id = ?", (*fields.values(), meeting_id))


def delete_meeting(meeting_id: int, db_path: str = DB_PATH) -> None:
    """Remove a reunião e a ata; as pendências da ata perdem o vínculo (não são apagadas)."""
    with closing(_connect(db_path)) as conn, conn:
        ata_ids = [r[0] for r in conn.execute("SELECT id FROM meeting_summaries WHERE meeting_id = ?",
                                              (meeting_id,)).fetchall()]
        for ata_id in ata_ids:
            conn.execute("UPDATE pendencias SET ata_id = NULL WHERE ata_id = ?", (ata_id,))
        conn.execute("DELETE FROM meeting_summaries WHERE meeting_id = ?", (meeting_id,))
        conn.execute("UPDATE transcripts SET meeting_id = NULL WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meeting_utterances WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meeting_voices WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meeting_voice_references WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))


def unassigned_transcripts(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Falas novas ainda sem reunião (ordem de chegada)."""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT id, timestamp, source, duration_seconds, text FROM transcripts
            WHERE meeting_id IS NULL AND processed_for_summary = 0 AND ignored = 0 ORDER BY id""").fetchall()
    return [dict(r) for r in rows]


def meeting_transcripts(meeting_id: int, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT id, timestamp, source, duration_seconds, text FROM transcripts
            WHERE meeting_id = ? AND ignored = 0 ORDER BY timestamp, id""", (meeting_id,)).fetchall()
    return [dict(r) for r in rows]


def ignored_count(meeting_id: int, db_path: str = DB_PATH) -> int:
    with closing(_connect(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM transcripts WHERE meeting_id = ? AND ignored = 1",
                            (meeting_id,)).fetchone()[0]


def assign_transcripts(ids: Iterable[int], meeting_id: Optional[int], processed: Optional[bool] = None,
                       db_path: str = DB_PATH) -> None:
    ids = list(ids)
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    extra = "" if processed is None else ", processed_for_summary = " + ("1" if processed else "0")
    with closing(_connect(db_path)) as conn, conn:
        conn.execute(f"UPDATE transcripts SET meeting_id = ?{extra} WHERE id IN ({marks})", (meeting_id, *ids))


def mark_meeting_processed(meeting_id: int, db_path: str = DB_PATH) -> None:
    with closing(_connect(db_path)) as conn, conn:
        conn.execute("UPDATE transcripts SET processed_for_summary = 1 WHERE meeting_id = ?", (meeting_id,))


def bounds(meeting_id: int, db_path: str = DB_PATH) -> Optional[tuple]:
    """(primeira fala, última fala) da reunião, ou None se ficou vazia."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM transcripts WHERE meeting_id = ? AND ignored = 0",
                           (meeting_id,)).fetchone()
    return (row[0], row[1]) if row and row[0] else None


def get_summary(meeting_id: int, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("""
            SELECT id, title, summary, action_items, key_decisions, topics, participants, created_at
            FROM meeting_summaries WHERE meeting_id = ? ORDER BY id DESC LIMIT 1""", (meeting_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    for key in ("topics", "participants"):
        try:
            item[key] = json.loads(item.get(key) or "[]")
        except ValueError:
            item[key] = []
    return item


def upsert_summary(meeting_id: int, ata: Dict[str, Any], start: str, end: str, db_path: str = DB_PATH) -> int:
    """Uma ata por reunião: atualiza a existente (o id fica estável para as pendências)."""
    values = (
        ata.get("title") or "Conversa registrada",
        ata.get("summary") or "",
        "\n".join([*(ata.get("tasks_owner") or []), *(ata.get("tasks_others") or [])]),
        "\n".join(ata.get("key_decisions") or []),
        json.dumps(ata.get("topics") or [], ensure_ascii=False),
        json.dumps(ata.get("participants") or [], ensure_ascii=False),
        start, end,
    )
    with closing(_connect(db_path)) as conn, conn:
        row = conn.execute("SELECT id FROM meeting_summaries WHERE meeting_id = ? ORDER BY id DESC LIMIT 1",
                           (meeting_id,)).fetchone()
        if row:
            conn.execute("""
                UPDATE meeting_summaries SET title = ?, summary = ?, action_items = ?, key_decisions = ?,
                    topics = ?, participants = ?, start_time = ?, end_time = ? WHERE id = ?""",
                (*values, row[0]))
            return row[0]
        cur = conn.execute("""
            INSERT INTO meeting_summaries (title, summary, action_items, key_decisions, topics, participants,
                start_time, end_time, session_id, meeting_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*values, f"meeting:{meeting_id}", meeting_id))
        return cur.lastrowid


def delete_summary(meeting_id: int, db_path: str = DB_PATH) -> None:
    """Ata de reunião que ficou sem fala válida; as pendências dela ficam, sem vínculo."""
    with closing(_connect(db_path)) as conn, conn:
        conn.execute("UPDATE pendencias SET ata_id = NULL WHERE ata_id IN "
                     "(SELECT id FROM meeting_summaries WHERE meeting_id = ?)", (meeting_id,))
        conn.execute("DELETE FROM meeting_summaries WHERE meeting_id = ?", (meeting_id,))


def repoint_pendencias(from_meeting: int, to_ata_id: Optional[int], db_path: str = DB_PATH) -> None:
    """Pendências da ata de uma reunião que vai sumir passam para outra ata."""
    with closing(_connect(db_path)) as conn, conn:
        conn.execute("""
            UPDATE pendencias SET ata_id = ?
            WHERE ata_id IN (SELECT id FROM meeting_summaries WHERE meeting_id = ?)""", (to_ata_id, from_meeting))


def confirmed_participants(db_path: str = DB_PATH) -> List[str]:
    """Nomes que o senhor já confirmou em alguma reunião (base de sugestões)."""
    names: List[str] = []
    with closing(_connect(db_path)) as conn:
        for (raw,) in conn.execute("SELECT participants FROM meetings WHERE participants != '[]'"):
            try:
                names.extend(str(n) for n in json.loads(raw) if str(n).strip())
            except ValueError:
                continue
    return names


def meetings_to_retry_ata(now: str, db_path: str = DB_PATH) -> List[int]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT id FROM meetings WHERE status = 'closed' AND ata_status IN ('pending', 'error')
              AND (ata_next_try_at IS NULL OR ata_next_try_at <= ?) ORDER BY id""", (now,)).fetchall()
    return [r[0] for r in rows]


# ── passada offline: vozes e falas por falante ──────────────────────────────────

def meetings_for_offline(now: str, db_path: str = DB_PATH) -> List[int]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT id FROM meetings WHERE status = 'closed' AND offline_status = 'pending'
              AND (offline_next_try_at IS NULL OR offline_next_try_at <= ?) ORDER BY id""", (now,)).fetchall()
    return [r[0] for r in rows]


def reset_running_offline(db_path: str = DB_PATH) -> None:
    """Processo que caiu no meio da passada: volta para a fila."""
    with closing(_connect(db_path)) as conn, conn:
        conn.execute("UPDATE meetings SET offline_status = 'pending' WHERE offline_status = 'running'")


def clear_offline(meeting_id: int, db_path: str = DB_PATH) -> List[str]:
    """Apaga vozes e falas por falante da reunião. Devolve os arquivos de amostra para o chamador remover."""
    with closing(_connect(db_path)) as conn, conn:
        files = [r[0] for r in conn.execute("SELECT sample_file FROM meeting_voices WHERE meeting_id = ? "
                                            "AND sample_file IS NOT NULL", (meeting_id,))]
        for (raw,) in conn.execute("SELECT review FROM meeting_utterances WHERE meeting_id = ? AND review IS NOT NULL",
                                   (meeting_id,)):
            sample = json.loads(raw).get("sample_file")
            if sample:
                files.append(sample)
        conn.execute("DELETE FROM meeting_utterances WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meeting_voices WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meeting_voice_references WHERE meeting_id = ?", (meeting_id,))
    return files


def save_offline_result(meeting_id: int, voices: List[Dict[str, Any]], utterances: List[Dict[str, Any]],
                        db_path: str = DB_PATH) -> None:
    """Substitui as vozes/falas da reunião. voices: source,label,seconds,suggested_name,suggested_score,
    sample_file,embedding. utterances: source,label,start_at,end_at,text."""
    with closing(_connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM meeting_utterances WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meeting_voices WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meeting_voice_references WHERE meeting_id = ?", (meeting_id,))
        ids: Dict[tuple, int] = {}
        for v in voices:
            cur = conn.execute("""
                INSERT INTO meeting_voices (meeting_id, source, label, seconds, suggested_name, suggested_score,
                    sample_file, embedding, evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (meeting_id, v["source"], v["label"], v.get("seconds"), v.get("suggested_name"),
                 v.get("suggested_score"), v.get("sample_file"), json.dumps(v.get("embedding") or []),
                 json.dumps(v["evidence"]) if v.get("evidence") else None))
            ids[(v["source"], v["label"])] = cur.lastrowid
        for u in utterances:
            voice_id = ids.get((u["source"], u["label"]))
            if voice_id is None:
                continue
            conn.execute("""
                INSERT INTO meeting_utterances (meeting_id, voice_id, source, start_at, end_at, text, review)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (meeting_id, voice_id, u["source"], u["start_at"], u["end_at"], u["text"],
                 json.dumps(u["review"], ensure_ascii=False) if u.get("review") else None))


def meeting_voices(meeting_id: int, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT id, source, label, seconds, name, suggested_name, suggested_score, merged_into, discarded,
                   sample_file, evidence FROM meeting_voices WHERE meeting_id = ? ORDER BY source = 'mic' DESC, seconds DESC""",
                            (meeting_id,)).fetchall()
    return [dict(r) for r in rows]


def unnamed_voices(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Vozes sem nome, soltas (não unidas nem descartadas) e com vetor, de reuniões com a passada offline pronta."""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT v.id, v.meeting_id, v.embedding, v.evidence FROM meeting_voices v
            JOIN meetings m ON m.id = v.meeting_id
            WHERE m.offline_status = 'done' AND COALESCE(v.name, '') = '' AND v.merged_into IS NULL
              AND v.discarded = 0 AND v.embedding IS NOT NULL AND v.embedding != '[]'""").fetchall()
    return [dict(r) for r in rows]


def set_voice_suggestion(voice_id: int, name: Optional[str], score: Optional[float], db_path: str = DB_PATH) -> None:
    with closing(_connect(db_path)) as conn, conn:
        conn.execute("UPDATE meeting_voices SET suggested_name = ?, suggested_score = ? WHERE id = ?",
                     (name, score, voice_id))


def confirm_voice_reference(voice_id: int, name: str, db_path: str = DB_PATH) -> None:
    """Aprende só por confirmação humana; não modifica o cadastro de autenticação."""
    with closing(_connect(db_path)) as conn, conn:
        row = conn.execute("SELECT meeting_id, evidence, discarded, merged_into FROM meeting_voices WHERE id = ?",
                           (voice_id,)).fetchone()
        conn.execute("DELETE FROM meeting_voice_references WHERE voice_id = ?", (voice_id,))
        if not row or not name or not row[1] or row[2] or row[3] is not None:
            return
        evidence = json.loads(row[1])
        if evidence.get("clean_seconds", 0) < 9 or len(evidence.get("embeddings", [])) < 3:
            return
        conn.execute("INSERT INTO meeting_voice_references VALUES (?, ?, ?, ?, ?)",
                     (voice_id, row[0], name, row[1], time.time()))


def reference_fingerprint(db_path: str = DB_PATH) -> str:
    with closing(_connect(db_path)) as conn:
        count, latest = conn.execute("SELECT COUNT(*), MAX(updated_at) FROM meeting_voice_references").fetchone()
    return f"{count}:{latest}"


def get_voice(voice_id: int, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT id, meeting_id, sample_file FROM meeting_voices WHERE id = ?",
                           (voice_id,)).fetchone()
    return dict(row) if row else None


def update_voice(voice_id: int, name: str, merged_into: Optional[int], discarded: bool,
                 db_path: str = DB_PATH) -> None:
    with closing(_connect(db_path)) as conn, conn:
        conn.execute("UPDATE meeting_voices SET name = ?, merged_into = ?, discarded = ? WHERE id = ?",
                     (name, merged_into, 1 if discarded else 0, voice_id))


def meeting_utterances(meeting_id: int, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT id, voice_id, source, start_at, end_at, text, review FROM meeting_utterances
            WHERE meeting_id = ? ORDER BY start_at, id""", (meeting_id,)).fetchall()
    result = [dict(r) for r in rows]
    for row in result:
        row["review"] = json.loads(row["review"]) if row["review"] else None
    return result


def get_review_sample(utterance_id: int, db_path: str = DB_PATH) -> str | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT review FROM meeting_utterances WHERE id = ?", (utterance_id,)).fetchone()
    return json.loads(row[0]).get("sample_file") if row and row[0] else None


def resolve_review(meeting_id: int, utterance_id: int, choice: str, text: str,
                   db_path: str = DB_PATH) -> None:
    if choice not in {"original", "alternative", "custom"}:
        raise ValueError("Escolha de revisão inválida.")
    if not isinstance(text, str) or len(text) > 10000:
        raise ValueError("Texto de revisão inválido.")
    with closing(_connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        meeting = conn.execute("SELECT offline_status FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if not meeting or meeting[0] in {"pending", "running"}:
            raise ValueError("Aguarde o processamento do áudio terminar.")
        row = conn.execute("SELECT review FROM meeting_utterances WHERE id = ? AND meeting_id = ?",
                           (utterance_id, meeting_id)).fetchone()
        if not row or not row[0]:
            raise ValueError("Trecho inválido para esta reunião.")
        review = json.loads(row[0])
        chosen = text.strip() if choice == "custom" else review[f"{choice}_text"]
        if choice == "alternative" and not chosen:
            raise ValueError("A segunda escuta não produziu texto.")
        review["status"] = "resolved"
        review["choice"] = choice
        conn.execute("UPDATE meeting_utterances SET text = ?, review = ? WHERE id = ?",
                     (chosen, json.dumps(review, ensure_ascii=False), utterance_id))
        conn.execute("UPDATE meetings SET ata_status = 'pending', ata_attempts = 0, ata_next_try_at = NULL WHERE id = ?",
                     (meeting_id,))
