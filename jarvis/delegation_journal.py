"""Profile-local delegation outcomes. A restart never authorizes replaying work."""
from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from cerebro_review import safe_text

TERMINAL = {"done", "error", "cancelled", "uncertain"}


class Journal:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise ValueError("Versão desconhecida do registro de tarefas")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
                    request TEXT NOT NULL, status TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                PRAGMA user_version=1;
            """)
            # Recovery only records uncertainty; it never calls an executor or retains approvals.
            conn.execute("""UPDATE tasks SET status='uncertain', updated=?,
                reply='A execução foi interrompida pelo reinício. O resultado é incerto. Confira a conversa Jarvis Voice antes de repetir; ações já executadas não foram desfeitas.'
                WHERE status='running'""", (time.time(),))
            conn.execute("""UPDATE tasks SET status='cancelled', updated=?,
                reply='Pedido não executado: o aplicativo reiniciou antes do envio ao Hermes. Se ainda quiser, faça um novo pedido e confirme novamente quando necessário.'
                WHERE status IN ('queued','confirming')""", (time.time(),))

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def accept(self, session: str, call_id: str, request: str) -> tuple[str, bool]:
        key = hashlib.sha256(f"{session}\n{call_id}".encode()).hexdigest() if call_id else uuid.uuid4().hex
        task_id, now = uuid.uuid4().hex, time.time()
        with closing(self.connect()) as conn, conn:
            inserted = conn.execute("""INSERT OR IGNORE INTO tasks
                (id,request_key,request,status,created,updated) VALUES(?,?,?,'queued',?,?)""",
                (task_id, key, safe_text(request)[:2000], now, now)).rowcount
            row = conn.execute("SELECT id FROM tasks WHERE request_key=?", (key,)).fetchone()
        return row["id"], bool(inserted)

    def phase(self, task_id: str, status: str) -> None:
        if status not in {"confirming", "running"}:
            raise ValueError("Estado de execução inválido")
        previous = "queued" if status == "confirming" else "confirming"
        with closing(self.connect()) as conn, conn:
            if conn.execute("UPDATE tasks SET status=?,updated=? WHERE id=? AND status=?",
                            (status, time.time(), task_id, previous)).rowcount != 1:
                raise ValueError("A tarefa já mudou de estado")

    def finish(self, task_id: str, reply: str, status: str) -> None:
        if status not in TERMINAL:
            raise ValueError("Resultado de execução inválido")
        with closing(self.connect()) as conn, conn:
            if conn.execute("""UPDATE tasks SET status=?,reply=?,updated=?
                WHERE id=? AND status IN ('queued','confirming','running')""",
                (status, safe_text(reply)[:4000], time.time(), task_id)).rowcount != 1:
                raise ValueError("A tarefa já terminou ou não existe")

    def history(self, limit: int = 20) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Limite de histórico inválido")
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT id,request,status,reply,created,updated FROM tasks ORDER BY updated DESC,id DESC LIMIT ?",
                                (limit,)).fetchall()
        return [dict(row) for row in rows]

