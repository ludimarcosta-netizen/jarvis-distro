"""
Módulo de armazenamento SQLite para o Jarvis Ambient Memory.
Persiste transcrições brutas (microfone e loopback) e atas/resumos de reuniões.
"""
import sqlite3
import json
import os
import tomllib
from contextlib import closing
from datetime import datetime
from typing import List, Dict, Any, Optional

DB_PATH = os.environ.get("JARVIS_TRANSCRIPTS_DB") or os.path.join(os.path.dirname(__file__), "transcripts.db")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.toml")

def _retention_days() -> int:
    try:
        with open(CONFIG_PATH, "rb") as config_file:
            value = tomllib.load(config_file).get("privacy", {}).get("transcript_retention_days", 0)
        return max(0, int(value))
    except (OSError, ValueError, TypeError):
        return 0

_initialized_dbs: set = set()


def _ensure_db(db_path: str = DB_PATH) -> None:
    """Schema garantido uma vez por processo e banco. Antes, toda leitura rodava
    o DDL e o DELETE de retenção, pegando lock de escrita a cada consulta."""
    if os.path.abspath(db_path) not in _initialized_dbs:
        init_db(db_path)


def _migrate_legacy_atas(cursor: sqlite3.Cursor) -> None:
    """Atas anteriores à tabela meetings viram reuniões encerradas; as falas entram
    pela janela de horário da ata. Idempotente: só atas ainda sem meeting_id."""
    legacy = cursor.execute("""
        SELECT id, title, start_time, end_time, participants FROM meeting_summaries
        WHERE meeting_id IS NULL AND start_time IS NOT NULL AND end_time IS NOT NULL
        ORDER BY start_time
    """).fetchall()
    for ata_id, title, start, end, participants in legacy:
        cursor.execute("""
            INSERT INTO meetings (title, started_at, ended_at, status, participants, closed_by, ata_status)
            VALUES (?, ?, ?, 'closed', '[]', 'legacy', 'ok')
        """, (title, start, end))
        meeting_id = cursor.lastrowid
        cursor.execute("UPDATE meeting_summaries SET meeting_id = ? WHERE id = ?", (meeting_id, ata_id))
        cursor.execute("""
            UPDATE transcripts SET meeting_id = ?
            WHERE meeting_id IS NULL AND timestamp BETWEEN ? AND ?
        """, (meeting_id, start, end))


def init_db(db_path: str = DB_PATH) -> None:
    """Cria/migra o schema e aplica a retenção configurada (chamado na inicialização)."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cursor = conn.cursor()
        # WAL: leitores (Hermes, cron, CLI) não bloqueiam a gravação contínua do
        # sentinel. A configuração é persistente no arquivo do banco.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                source TEXT,
                duration_seconds REAL,
                text TEXT,
                processed_for_summary INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS meeting_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                start_time DATETIME,
                end_time DATETIME,
                title TEXT,
                summary TEXT,
                action_items TEXT,
                key_decisions TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS meeting_markers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                kind TEXT,
                name TEXT
            )
        """)
        # Pendências em cobrança: tarefas/compromissos das reuniões com status próprio
        # (a memória permanente do Hermes guarda só fatos sobre o usuário).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pendencias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                descricao TEXT NOT NULL,
                responsavel TEXT,
                prazo TEXT,
                status TEXT NOT NULL DEFAULT 'aberta',
                ata_id INTEGER REFERENCES meeting_summaries(id),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                concluida_em DATETIME
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pendencias_status ON pendencias(status)")
        # Reunião como entidade: as falas (transcripts.meeting_id) e a ata
        # (meeting_summaries.meeting_id) apontam para ela, o que permite dividir e
        # juntar reuniões pela página local. Horários em UTC, como o resto do banco.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                started_at DATETIME,
                ended_at DATETIME,
                status TEXT NOT NULL DEFAULT 'open',
                participants TEXT NOT NULL DEFAULT '[]',
                prompt_at DATETIME,
                prompt_dismissed_at DATETIME,
                closed_by TEXT,
                ata_status TEXT NOT NULL DEFAULT 'pending',
                ata_attempts INTEGER NOT NULL DEFAULT 0,
                ata_next_try_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_meetings_started ON meetings(started_at)")
        # Colunas da Fase 3 (idempotente: ignora se já existem).
        for table, column in (("meeting_summaries", "topics TEXT"), ("meeting_summaries", "participants TEXT"),
                              ("meeting_summaries", "meeting_id INTEGER"), ("transcripts", "meeting_id INTEGER"),
                              ("transcripts", "ignored INTEGER NOT NULL DEFAULT 0"), ("transcripts", "ignored_reason TEXT"),
                              ("transcripts", "original_text TEXT")):
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # coluna já existe
        # Passada offline (05/10/2026): a reunião gravada é separada por voz e transcrita inteira.
        for column in ("offline_status TEXT NOT NULL DEFAULT 'none'", "offline_attempts INTEGER NOT NULL DEFAULT 0",
                       "offline_next_try_at DATETIME"):
            try:
                cursor.execute(f"ALTER TABLE meetings ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS meeting_voices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                label INTEGER NOT NULL,
                seconds REAL,
                name TEXT,
                suggested_name TEXT,
                suggested_score REAL,
                merged_into INTEGER,
                discarded INTEGER NOT NULL DEFAULT 0,
                sample_file TEXT,
                embedding TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS meeting_utterances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                voice_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                start_at DATETIME,
                end_at DATETIME,
                text TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_voices_meeting ON meeting_voices(meeting_id)")
        for table, additions in (("meeting_voices", ("evidence TEXT",)),
                                 ("meetings", ("mic_speakers INTEGER", "loopback_speakers INTEGER"))):
            columns = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
            for column in additions:
                if column.split()[0] not in columns:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        cursor.execute("""CREATE TABLE IF NOT EXISTS meeting_voice_references (
            voice_id INTEGER PRIMARY KEY, meeting_id INTEGER NOT NULL, name TEXT NOT NULL,
            evidence TEXT NOT NULL, updated_at REAL NOT NULL)""")
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(meeting_utterances)")}
        if "review" not in columns:
            cursor.execute("ALTER TABLE meeting_utterances ADD COLUMN review TEXT")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_utterances_meeting ON meeting_utterances(meeting_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_meeting ON transcripts(meeting_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_summaries_meeting ON meeting_summaries(meeting_id)")
        _migrate_legacy_atas(cursor)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_timestamp ON transcripts(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_session ON transcripts(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_unprocessed ON transcripts(processed_for_summary)")
        retention_days = _retention_days()
        if retention_days:
            cursor.execute("DELETE FROM transcripts WHERE datetime(timestamp) < datetime('now', ?)",
                           (f"-{retention_days} days",))
        conn.commit()
    _initialized_dbs.add(os.path.abspath(db_path))

def save_transcript(text: str, source: str = "mic", duration: float = 0.0, session_id: Optional[str] = None, db_path: str = DB_PATH) -> int:
    text = text.strip()
    if not text:
        return -1
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO transcripts (session_id, source, duration_seconds, text)
            VALUES (?, ?, ?, ?)
        """, (session_id or "ambient", source, duration, text))
        conn.commit()
        return cursor.lastrowid

def get_recent_transcripts(limit: int = 50, minutes: int = 60, db_path: str = DB_PATH,
                           session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    _ensure_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        session_filter = "AND session_id = ?" if session_id else ""
        params = (f"-{minutes} minutes", session_id, limit) if session_id else (f"-{minutes} minutes", limit)
        cursor.execute(f"""
            SELECT id, session_id, timestamp, source, duration_seconds, text
            FROM transcripts
            WHERE datetime(timestamp) >= datetime('now', ?) AND ignored = 0
            {session_filter}
            ORDER BY id DESC
            LIMIT ?
        """, params)
        return [dict(row) for row in reversed(cursor.fetchall())]

def get_unprocessed_transcripts(limit: int = 100, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    _ensure_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, session_id, timestamp, source, duration_seconds, text
            FROM transcripts
            WHERE processed_for_summary = 0 AND ignored = 0
            ORDER BY id ASC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]

def ignore_transcripts(ids: List[int], reason: str, db_path: str = DB_PATH) -> None:
    """Trecho que não é fala da sala (eco do PC, alucinação): sai das reuniões, sem apagar."""
    ids = [i for i in ids if i and i > 0]
    if not ids:
        return
    with closing(sqlite3.connect(db_path)) as conn, conn:
        marks = ",".join("?" for _ in ids)
        conn.execute(f"UPDATE transcripts SET ignored = 1, ignored_reason = ? WHERE id IN ({marks})", (reason, *ids))


def mark_transcripts_processed(ids: List[int], db_path: str = DB_PATH) -> None:
    if not ids:
        return
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in ids)
        cursor.execute(f"UPDATE transcripts SET processed_for_summary = 1 WHERE id IN ({placeholders})", ids)
        conn.commit()

def save_meeting_summary(session_id: str, title: str, summary: str, action_items: str, key_decisions: str,
                         start_time: Optional[str] = None, end_time: Optional[str] = None, db_path: str = DB_PATH) -> int:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO meeting_summaries (session_id, start_time, end_time, title, summary, action_items, key_decisions)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (session_id, start_time or datetime.now().isoformat(), end_time or datetime.now().isoformat(),
              title, summary, action_items, key_decisions))
        conn.commit()
        return cursor.lastrowid


def save_meeting_summary_v2(session_id: str, title: str, summary: str, action_items: str, key_decisions: str,
                            topics: Optional[List[str]] = None, participants: Optional[List[str]] = None,
                            start_time: Optional[str] = None, end_time: Optional[str] = None,
                            db_path: str = DB_PATH) -> int:
    """Fase 3: ata estruturada completa (tópicos e participantes além das colunas originais)."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO meeting_summaries
                (session_id, start_time, end_time, title, summary, action_items, key_decisions, topics, participants)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (session_id, start_time, end_time, title, summary, action_items, key_decisions,
              json.dumps(topics or [], ensure_ascii=False), json.dumps(participants or [], ensure_ascii=False)))
        conn.commit()
        return cursor.lastrowid


def insert_marker(kind: str, name: str = "", timestamp: Optional[str] = None, db_path: str = DB_PATH) -> int:
    """Marcador explícito de reunião ('start' com nome / 'end').
    `timestamp` opcional no formato SQLite UTC ('YYYY-MM-DD HH:MM:SS')."""
    _ensure_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cursor = conn.cursor()
        if timestamp:
            cursor.execute("INSERT INTO meeting_markers (timestamp, kind, name) VALUES (?, ?, ?)",
                           (timestamp, kind, name))
        else:
            cursor.execute("INSERT INTO meeting_markers (kind, name) VALUES (?, ?)", (kind, name))
        conn.commit()
        return cursor.lastrowid


def get_markers(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    _ensure_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, timestamp, kind, name FROM meeting_markers ORDER BY id ASC")
        return [dict(row) for row in cursor.fetchall()]


def search_transcripts(query: str, days: int = 7, limit: int = 60, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    _ensure_db(db_path)
    like = f"%{query.strip()}%"
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, session_id, timestamp, source, duration_seconds, text
            FROM transcripts
            WHERE datetime(timestamp) >= datetime('now', ?) AND text LIKE ? AND ignored = 0
            ORDER BY id DESC LIMIT ?
        """, (f"-{days} days", like, limit))
        return [dict(row) for row in reversed(cursor.fetchall())]


def search_summaries(query: str, days: int = 30, limit: int = 20, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    _ensure_db(db_path)
    like = f"%{query.strip()}%"
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, session_id, start_time, end_time, title, summary, action_items, key_decisions, topics, participants, created_at
            FROM meeting_summaries
            WHERE datetime(created_at) >= datetime('now', ?)
              AND (title LIKE ? OR summary LIKE ? OR action_items LIKE ? OR key_decisions LIKE ?)
            ORDER BY id DESC LIMIT ?
        """, (f"-{days} days", like, like, like, like, limit))
        return [dict(row) for row in reversed(cursor.fetchall())]


def list_summaries(days: int = 30, limit: int = 50, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    _ensure_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, session_id, start_time, end_time, title, summary, action_items, key_decisions, topics, participants, created_at
            FROM meeting_summaries
            WHERE datetime(created_at) >= datetime('now', ?)
            ORDER BY id DESC LIMIT ?
        """, (f"-{days} days", limit))
        return [dict(row) for row in reversed(cursor.fetchall())]


def add_pendencia(descricao: str, responsavel: str = "", prazo: str = "", ata_id: Optional[int] = None,
                  db_path: str = DB_PATH) -> int:
    """Registra uma pendência aberta. Retorna o id; se já existir uma aberta com a mesma
    descrição, retorna o id existente (idempotente). -1 para descrição vazia."""
    descricao = (descricao or "").strip()
    if not descricao:
        return -1
    _ensure_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM pendencias WHERE status = 'aberta' AND lower(descricao) = lower(?)",
                       (descricao,))
        existing = cursor.fetchone()
        if existing:
            return existing[0]
        cursor.execute("""
            INSERT INTO pendencias (descricao, responsavel, prazo, ata_id) VALUES (?, ?, ?, ?)
        """, (descricao, responsavel.strip() or None, prazo.strip() or None, ata_id))
        conn.commit()
        return cursor.lastrowid


def list_pendencias(incluir_concluidas: bool = False, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    _ensure_db(db_path)
    status_filter = "" if incluir_concluidas else "WHERE p.status = 'aberta'"
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT p.id, p.descricao, p.responsavel, p.prazo, p.status, p.ata_id, p.created_at,
                   p.concluida_em, m.title AS ata_titulo
            FROM pendencias p LEFT JOIN meeting_summaries m ON m.id = p.ata_id
            {status_filter}
            ORDER BY p.id ASC
        """)
        return [dict(row) for row in cursor.fetchall()]


def concluir_pendencia(pendencia_id: int, db_path: str = DB_PATH) -> bool:
    """Marca como concluída. False se o id não existe ou já estava concluída."""
    _ensure_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE pendencias SET status = 'concluida', concluida_em = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'aberta'
        """, (pendencia_id,))
        conn.commit()
        return cursor.rowcount > 0

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully at:", DB_PATH)
