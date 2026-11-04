"""Embeddings locais opcionais; cache reconstruível separado das notas e do FTS.

O modelo atual trunca cada trecho em 128 tokens (não 512): só o começo de trechos longos pesa no vetor.
Trocar o modelo ou o tamanho de trecho muda `MODEL`; os vetores antigos são descartados pelo campo `model`.
"""
import hashlib
import sqlite3
import threading
from contextlib import closing
from functools import lru_cache
from pathlib import Path

import numpy as np

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MAX_TOKENS = 128  # limite de entrada do MODEL; texto além disso é ignorado pelo modelo

_vectors: dict[str, tuple[tuple[int, int], dict[str, np.ndarray]]] = {}
_vectors_lock = threading.Lock()


@lru_cache(maxsize=2)
def encoder(cache: str, download: bool = False):
    from fastembed import TextEmbedding
    return TextEmbedding(MODEL, cache_dir=cache, threads=2,
                         providers=["CPUExecutionProvider"], local_files_only=not download)


def release_model() -> None:
    """Descarrega o modelo (o daemon chama após um tempo ocioso; a próxima consulta recarrega)."""
    encoder.cache_clear()


def fingerprint(row) -> str:
    text = "\n".join(str(row[k]) for k in ("path", "title", "section", "body", "line"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stamp(db: Path) -> tuple[int, int]:
    stat = db.stat()
    return stat.st_mtime_ns, stat.st_size


class SemanticIndex:
    def __init__(self, db: Path, cache: Path, seed: Path | None = None):
        # seed: índice semântico anterior (mesmo modelo) para reaproveitar vetores dos trechos inalterados.
        self.db, self.cache, self.seed = db, cache, seed

    def _seed(self, conn: sqlite3.Connection) -> None:
        if not self.seed or not self.seed.is_file() or conn.execute("SELECT 1 FROM vectors LIMIT 1").fetchone():
            return
        conn.execute("ATTACH DATABASE ? AS seed", (f"{self.seed.as_uri()}?mode=ro",))
        try:
            conn.execute("INSERT OR IGNORE INTO vectors SELECT hash, model, vector FROM seed.vectors WHERE model=?", (MODEL,))
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE seed")

    def refresh(self, rows: list[dict], download: bool = False) -> dict:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db.resolve().as_uri(), timeout=30, uri=True)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS vectors(hash TEXT PRIMARY KEY, model TEXT, vector BLOB)")
            self._seed(conn)
            known = {r[0] for r in conn.execute("SELECT hash FROM vectors WHERE model=?", (MODEL,))}
            current = {fingerprint(row): row for row in rows}
            missing = [key for key in current if key not in known]
            # O modelo (centenas de MB) só é carregado quando há trecho novo para vetorizar.
            model = encoder(str(self.cache), download) if missing else None
            for start in range(0, len(missing), 8):
                keys = missing[start:start + 8]
                texts = ["\n".join(current[k][f] for f in ("title", "section", "body")) for k in keys]
                vectors = list(model.embed(texts, batch_size=8))
                if len(vectors) != len(keys):
                    raise ValueError("Modelo retornou quantidade incorreta de embeddings")
                for key, vector in zip(keys, vectors):
                    vector = np.asarray(vector, dtype=np.float32)
                    norm = np.linalg.norm(vector)
                    if not np.isfinite(vector).all() or norm == 0:
                        raise ValueError("Embedding inválido")
                    conn.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?)",
                                 (key, MODEL, (vector / norm).tobytes()))
                conn.commit()
            conn.executemany("DELETE FROM vectors WHERE hash=?", [(key,) for key in known - current.keys()])
            conn.execute("DELETE FROM vectors WHERE model<>?", (MODEL,))
        with _vectors_lock:
            _vectors.pop(str(self.db), None)
        return {"novos": len(missing), "trechos": len(current), "modelo": MODEL}

    def vectors(self) -> dict[str, np.ndarray]:
        """Vetores em memória, relidos do disco só quando o arquivo do índice muda."""
        key, stamp = str(self.db), _stamp(self.db)
        with _vectors_lock:
            cached = _vectors.get(key)
            if cached and cached[0] == stamp:
                return cached[1]
        with closing(sqlite3.connect(f"{self.db.as_uri()}?mode=ro", uri=True)) as conn:
            loaded = {h: np.frombuffer(blob, dtype=np.float32)
                      for h, blob in conn.execute("SELECT hash,vector FROM vectors WHERE model=?", (MODEL,))}
        with _vectors_lock:
            _vectors[key] = (stamp, loaded)
        return loaded

    def rank(self, query: str, rows: list[dict], limit: int) -> list[dict]:
        if not self.db.is_file():
            return []
        model = encoder(str(self.cache))
        vector = np.asarray(next(iter(model.query_embed(query))), dtype=np.float32)
        vector /= np.linalg.norm(vector)
        cached = self.vectors()
        scored = []
        for row in rows:
            stored = cached.get(row.get("hash") or fingerprint(row))
            if stored is None:
                continue
            score = float(stored @ vector)
            if score >= 0.25:
                scored.append((score, row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [row for _, row in scored[:limit]]


def fuse(lexical: list[dict], semantic: list[dict]) -> list[dict]:
    scores, items = {}, {}
    for ranking in (lexical, semantic):
        for position, row in enumerate(ranking, 1):
            key = (row["path"], row["line"])
            items[key] = row
            scores[key] = scores.get(key, 0.0) + 1 / (60 + position)
    return [items[key] for key in sorted(scores, key=scores.get, reverse=True)]
