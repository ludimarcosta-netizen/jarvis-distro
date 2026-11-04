"""Small local registry. Windows DPAPI encrypts names, embeddings and samples."""
from __future__ import annotations

import ctypes
import base64
import binascii
import io
import json
import math
import os
import sqlite3
import time
import uuid
import wave
from contextlib import closing
from pathlib import Path

PERSISTENT_EXPIRY = 253402300799  # local previews remain until the user removes them
MIN_ENROLLMENT_SECONDS = 20
MIN_ENROLLMENT_CHUNKS = 4
MAX_PREVIEW_SECONDS = 30
MAX_PREVIEW_BYTES = MAX_PREVIEW_SECONDS * 16000 * 2
MAX_PREVIEW_EMBEDDINGS = 12


def pcm_from_wav(encoded: str) -> bytes:
    try:
        with wave.open(io.BytesIO(base64.b64decode(encoded)), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
                return b""
            return audio.readframes(audio.getnframes())
    except (ValueError, binascii.Error, wave.Error, EOFError):
        return b""


def wav_from_pcm(pcm: bytes) -> str:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(pcm)
    return base64.b64encode(output.getvalue()).decode("ascii")


def protect(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Este armazenamento exige Windows DPAPI.")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    fn.restype = ctypes.c_int
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel = ctypes.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(target.data)


def unit(vector: list[float]) -> list[float]:
    if not vector or not all(math.isfinite(v) for v in vector):
        raise ValueError("Embedding inválido")
    norm = math.sqrt(sum(v * v for v in vector))
    if norm < 1e-8:
        raise ValueError("Embedding vazio")
    return [v / norm for v in vector]


def similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return -1.0
    return sum(x * y for x, y in zip(a, b))


def candidate_similarity(vector: list[float], candidate: dict) -> float:
    return max((similarity(vector, saved) for saved in candidate.get("embeddings", [candidate["embedding"]])),
               default=-1.0)


class Store:
    def __init__(self, path: Path, codec=protect):
        self.path, self.codec = path, codec
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY, secret BLOB NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY, secret BLOB NOT NULL, expires REAL NOT NULL,
                    created REAL, seconds REAL, source TEXT,
                    enrollment_ready INTEGER NOT NULL DEFAULT 0, replace_id TEXT NOT NULL DEFAULT '',
                    suggested_id TEXT NOT NULL DEFAULT '', hint_name TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    chunks INTEGER NOT NULL DEFAULT 0);
                PRAGMA user_version=4;
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(candidates)")}
            for name, definition in (
                ("created", "REAL"), ("seconds", "REAL"), ("source", "TEXT"),
                ("enrollment_ready", "INTEGER NOT NULL DEFAULT 0"),
                ("replace_id", "TEXT NOT NULL DEFAULT ''"),
                ("suggested_id", "TEXT NOT NULL DEFAULT ''"),
                ("hint_name", "TEXT NOT NULL DEFAULT ''"),
                ("confidence", "REAL NOT NULL DEFAULT 0"),
                ("chunks", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE candidates ADD COLUMN {name} {definition}")
            db.execute("UPDATE candidates SET expires=? WHERE expires<?", (PERSISTENT_EXPIRY, PERSISTENT_EXPIRY))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA secure_delete=ON")
        return db

    def pack(self, data: dict) -> bytes:
        return self.codec(json.dumps(data, ensure_ascii=False).encode())

    def unpack(self, data: bytes) -> dict:
        return json.loads(self.codec(data, decrypt=True))

    def profiles(self) -> list[dict]:
        with closing(self.connect()) as db:
            return [{"id": r[0], **self.unpack(r[1])} for r in db.execute("SELECT id,secret FROM profiles")]

    def prune_candidates(self) -> int:
        with closing(self.connect()) as db, db:
            cursor = db.execute("DELETE FROM candidates WHERE expires <= ?", (time.time(),))
            return max(0, cursor.rowcount)

    def candidate_count(self) -> int:
        self.prune_candidates()
        with closing(self.connect()) as db:
            return int(db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])

    def candidate_summaries(self) -> list[dict]:
        self.prune_candidates()
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT id,expires,created,seconds,source,enrollment_ready,replace_id,"
                "suggested_id,hint_name,confidence,chunks,secret FROM candidates ORDER BY created DESC"
            )
            result = []
            for r in rows:
                saved = self.unpack(r[11])
                result.append({"id": r[0], "expires": r[1], "created": r[2] or saved.get("created", 0),
                               "seconds": r[3] or saved.get("seconds", 0), "source": r[4] or saved.get("source", ""),
                               "enrollment_ready": bool(r[5] or saved.get("enrollment_ready")),
                               "replace_id": r[6] or saved.get("replace_id", ""),
                               "suggested_id": r[7] or "", "hint_name": r[8] or "",
                               "confidence": round(r[9] or 0.0, 3),
                               "chunks": r[10] or len(saved.get("embeddings", [])) or 1,
                               "label_name": saved.get("label_name", ""),
                               "label_owner": bool(saved.get("label_owner", False))})
            return result

    def candidates(self) -> list[dict]:
        self.prune_candidates()
        with closing(self.connect()) as db:
            return [{"id": r[0], "expires": r[2], **self.unpack(r[1])}
                    for r in db.execute("SELECT id,secret,expires FROM candidates ORDER BY expires")]

    def candidate_by_id(self, identity: str) -> dict | None:
        self.prune_candidates()
        with closing(self.connect()) as db:
            row = db.execute("SELECT id,secret,expires FROM candidates WHERE id=?", (identity,)).fetchone()
            return ({"id": row[0], "expires": row[2], **self.unpack(row[1])} if row else None)

    def candidate(self, vector: list[float], sample: str, seconds: float, source: str, model: str,
                  suggested_id: str = "", hint_name: str = "", confidence: float = 0.0) -> str:
        vector = unit(vector)
        candidates = self.candidates()
        comparable = [c for c in candidates if c["model"] == model
                      and (not c.get("enrollment_ready") or c.get("ambient_collection"))]
        best = max(comparable, key=lambda c: candidate_similarity(vector, c), default=None)
        if (best and candidate_similarity(vector, best) >= 0.72
                and (not best.get("suggested_id") or not suggested_id or best["suggested_id"] == suggested_id)):
            # Only same-model, high-similarity speech joins this speaker's saved snippets.
            if not suggested_id or not best.get("suggested_id") or best["suggested_id"] == suggested_id:
                old_pcm = pcm_from_wav(best.get("sample", ""))
                new_pcm = pcm_from_wav(sample)
                if new_pcm and (len(old_pcm) < MAX_PREVIEW_BYTES or
                                len(best.get("embeddings", [best["embedding"]])) < MAX_PREVIEW_EMBEDDINGS):
                    combined = old_pcm + new_pcm[:max(0, MAX_PREVIEW_BYTES - len(old_pcm))]
                    vectors = [*best.get("embeddings", [best["embedding"]])[:MAX_PREVIEW_EMBEDDINGS - 1], vector]
                    best["embeddings"] = vectors
                    best["embedding"] = unit([sum(v[i] for v in vectors) / len(vectors)
                                              for i in range(len(vector))])
                    best["sample"] = wav_from_pcm(combined)
                    best["seconds"] = len(combined) / (16000 * 2)
                    best["ambient_collection"] = True
                    best["enrollment_ready"] = (best["seconds"] >= MIN_ENROLLMENT_SECONDS
                                                and len(vectors) >= MIN_ENROLLMENT_CHUNKS)
            best["suggested_id"] = "" if best.get("label_name") else suggested_id or best.get("suggested_id", "")
            best["hint_name"] = hint_name or best.get("hint_name", "")
            best["confidence"] = max(confidence, best.get("confidence", 0.0))
            with closing(self.connect()) as db, db:
                db.execute("UPDATE candidates SET secret=?,seconds=?,enrollment_ready=?,"
                           "suggested_id=?,hint_name=?,confidence=?,chunks=? WHERE id=?",
                           (self.pack({k: v for k, v in best.items() if k not in ("id", "expires")}),
                            best.get("seconds", 0), int(best.get("enrollment_ready", False)),
                            best["suggested_id"], best["hint_name"], best["confidence"],
                            len(best.get("embeddings", [])), best["id"]))
            if best.get("label_name") and best.get("enrollment_ready"):
                self.save_identity(best["id"], best["label_name"], best.get("label_owner", False), True)
            return best["id"]
        identity = uuid.uuid4().hex
        value = {"embedding": vector, "sample": sample, "seconds": seconds, "source": source,
                 "model": model, "created": time.time(), "suggested_id": suggested_id,
                 "hint_name": hint_name, "confidence": confidence,
                 "embeddings": [vector], "ambient_collection": True}
        expires = PERSISTENT_EXPIRY
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO candidates "
                "(id,secret,expires,created,seconds,source,enrollment_ready,replace_id,"
                "suggested_id,hint_name,confidence,chunks) VALUES (?,?,?,?,?,?,0,'',?,?,?,1)",
                (identity, self.pack(value), expires, value["created"], seconds, source,
                 suggested_id, hint_name, confidence),
            )
        return identity

    def save_identity(self, identity: str, name: str, owner: bool, consent: bool) -> dict:
        """Persist a reviewed name now; combine all snippets the user labels alike."""
        name = name.strip()
        if not consent:
            raise ValueError("Confirme que a pessoa concorda com o cadastro vocal")
        if not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name):
            raise ValueError("Nome inválido")
        selected = self.candidate_by_id(identity)
        if not selected or (selected.get("suggested_id") and not selected.get("label_name")):
            raise ValueError("Amostra ausente ou já identificada")
        profiles = self.profiles()
        existing_owner = next((p for p in profiles if p["owner"]), None)
        existing_profile = next((p for p in profiles if p["name"].casefold() == name.casefold()), None)
        if owner and existing_owner and name.casefold() != existing_owner["name"].casefold():
            raise ValueError("Use o nome do proprietário já cadastrado para identificar sua voz")
        peers = [c for c in self.candidates()
                 if c["id"] != identity and c.get("model") == selected.get("model")
                 and c.get("label_name", "").casefold() == name.casefold()
                 and not c.get("suggested_id")]
        selected_pcm = pcm_from_wav(selected.get("sample", ""))
        parts = [selected, *(c for c in peers if pcm_from_wav(c.get("sample", "")))]
        pcm = b"".join(pcm_from_wav(c.get("sample", "")) for c in parts)[:MAX_PREVIEW_BYTES]
        vectors = [unit(v) for c in parts for v in c.get("embeddings", [c["embedding"]])
                   if len(v) == len(selected["embedding"])][:MAX_PREVIEW_EMBEDDINGS]
        if not vectors or not selected_pcm:
            selected.update({"label_name": name, "label_owner": bool(owner)})
            with closing(self.connect()) as db, db:
                db.execute("UPDATE candidates SET secret=? WHERE id=?",
                           (self.pack({k: v for k, v in selected.items() if k not in ("id", "expires")}), identity))
            return {"id": identity, "pending": True, "action": "pending",
                    "seconds": selected.get("seconds", 0), "chunks": 0}
        selected.update({"embedding": unit([sum(v[i] for v in vectors) / len(vectors)
                                            for i in range(len(vectors[0]))]),
                         "embeddings": vectors, "sample": wav_from_pcm(pcm),
                         "seconds": len(pcm) / (16000 * 2), "label_name": name,
                         "label_owner": bool(owner), "ambient_collection": True,
                         "enrollment_ready": len(pcm) >= MIN_ENROLLMENT_SECONDS * 16000 * 2
                                             and len(vectors) >= MIN_ENROLLMENT_CHUNKS})
        with closing(self.connect()) as db, db:
            db.execute("UPDATE candidates SET secret=?,seconds=?,enrollment_ready=?,chunks=? WHERE id=?",
                       (self.pack({k: v for k, v in selected.items() if k not in ("id", "expires")}),
                        selected["seconds"], int(selected["enrollment_ready"]), len(vectors), identity))
            db.executemany("DELETE FROM candidates WHERE id=?", [(c["id"],) for c in parts[1:]])
        if selected["enrollment_ready"]:
            if existing_profile:
                model = selected["model"]
                if existing_profile["model"] == model:
                    previous = existing_profile["embeddings"]
                    existing_profile["embeddings"] = [*previous[:8], *vectors[:4]][:MAX_PREVIEW_EMBEDDINGS]
                else:
                    by_model = existing_profile.setdefault("model_embeddings", {})
                    previous = by_model.get(model, [])
                    by_model[model] = [*previous[:8], *vectors[:4]][:MAX_PREVIEW_EMBEDDINGS]
                with closing(self.connect()) as db, db:
                    db.execute("UPDATE profiles SET secret=? WHERE id=?",
                               (self.pack({k: v for k, v in existing_profile.items() if k != "id"}),
                                existing_profile["id"]))
                    db.execute("DELETE FROM candidates WHERE id=?", (identity,))
                return {"id": existing_profile["id"], "pending": False, "action": "linked",
                        "seconds": selected["seconds"], "chunks": len(vectors)}
            return {"id": self.enroll([identity], name, owner, True), "pending": False, "action": "created",
                    "seconds": selected["seconds"], "chunks": len(vectors)}
        return {"id": identity, "pending": True, "action": "pending", "seconds": selected["seconds"],
                "chunks": len(vectors)}

    def enroll(self, ids: list[str], name: str, owner: bool, consent: bool) -> str:
        if not consent:
            raise ValueError("Confirme que a pessoa concorda com o cadastro vocal")
        name = name.strip()
        if not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name):
            raise ValueError("Nome inválido")
        candidates = {c["id"]: c for c in self.candidates()}
        if not ids or len(ids) != len(set(ids)) or any(i not in candidates for i in ids):
            raise ValueError("Amostra ausente ou expirada")
        selected = [candidates[i] for i in ids]
        if len(selected) != 1 or not selected[0].get("enrollment_ready"):
            raise ValueError("Aguarde 20 segundos de fala limpa em pelo menos quatro trechos")
        recording = selected[0]
        if recording["seconds"] < 20 or len(recording.get("embeddings", [])) < 4:
            raise ValueError("Cadastro vocal insuficiente")
        replacement = recording.get("replace_id", "")
        profiles = self.profiles()
        if replacement and not any(p["id"] == replacement for p in profiles):
            raise ValueError("O cadastro a substituir não existe mais")
        if owner and any(p["owner"] and p["id"] != replacement for p in profiles):
            raise ValueError("Já existe um proprietário. Exclua o cadastro anterior primeiro.")
        profile = {"name": name, "owner": bool(owner), "model": selected[0]["model"],
                   "embeddings": recording["embeddings"], "enrollment_version": 2,
                   "enrollment_seconds": recording["seconds"]}
        identity = replacement or uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?)", (identity, self.pack(profile), time.time()))
            db.executemany("DELETE FROM candidates WHERE id=?", [(i,) for i in ids])
        return identity

    def prepared_candidate(self, embeddings: list[list[float]], sample: str, seconds: float,
                           model: str, replace_id: str = "") -> str:
        if seconds < 20 or len(embeddings) < 4:
            raise ValueError("Cadastro vocal insuficiente")
        identity = uuid.uuid4().hex
        value = {"embedding": unit(embeddings[0]), "embeddings": [unit(v) for v in embeddings],
                 "sample": sample, "seconds": seconds, "source": "live", "model": model,
                 "created": time.time(), "enrollment_ready": True, "replace_id": replace_id}
        expires = PERSISTENT_EXPIRY
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO candidates "
                "(id,secret,expires,created,seconds,source,enrollment_ready,replace_id) "
                "VALUES (?,?,?,?,?,?,1,?)",
                (identity, self.pack(value), expires, value["created"], seconds, "live", replace_id),
            )
        return identity

    @staticmethod
    def voice_eligible(profile: dict) -> bool:
        return (profile.get("enrollment_version") == 2 and profile.get("enrollment_seconds", 0) >= 20
                and len(profile.get("embeddings", [])) >= 4)

    def delete(self, identity: str, candidate: bool = False):
        table = "candidates" if candidate else "profiles"
        with closing(self.connect()) as db, db:
            db.execute(f"DELETE FROM {table} WHERE id=?", (identity,))

    def review(self, identity: str, confirmed: bool) -> dict | None:
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT suggested_id FROM candidates WHERE id=? AND expires>?",
                             (identity, time.time())).fetchone()
            if not row or not row[0]:
                raise ValueError("Sugestão inexistente ou expirada")
            if not confirmed:
                candidate = self.candidate_by_id(identity)
                candidate["suggested_id"] = ""
                candidate["confidence"] = 0.0
                db.execute("UPDATE candidates SET secret=?,suggested_id='',confidence=0 WHERE id=?",
                           (self.pack({k: v for k, v in candidate.items() if k not in ("id", "expires")}), identity))
                return None
            profile = next((p for p in self.profiles() if p["id"] == row[0]), None)
            if not profile:
                raise ValueError("Perfil sugerido não existe mais")
            candidate = self.candidate_by_id(identity)
            candidate["suggested_id"] = ""
            candidate["confidence"] = 0.0
            db.execute("UPDATE candidates SET secret=?,suggested_id='',confidence=0 WHERE id=?",
                       (self.pack({k: v for k, v in candidate.items() if k not in ("id", "expires")}), identity))
        return self.save_identity(identity, profile["name"], profile["owner"], True)

    def hint_recent_meeting(self, name: str, now: float | None = None) -> int:
        """Label recent unknown previews as meeting context, never as verified identity."""
        now = time.time() if now is None else now
        with closing(self.connect()) as db, db:
            rows = list(db.execute(
                "SELECT id,created FROM candidates WHERE created BETWEEN ? AND ? "
                "AND enrollment_ready=0 AND suggested_id='' AND source IN ('ambient','live') "
                "ORDER BY created DESC", (now - 3600, now)))
            selected = []
            previous = now
            for identity, created in rows:
                if previous - created > 300:
                    break
                selected.append((name, identity))
                previous = created
            db.executemany("UPDATE candidates SET hint_name=? WHERE id=?", selected)
            return len(selected)

    def match(self, embedding: list[float], model: str, threshold: float = 0.72) -> dict:
        vector = unit(embedding)
        scores = [(max(similarity(vector, unit(v)) for v in vectors), p)
                  for p in self.profiles()
                  if (vectors := (p["embeddings"] if p["model"] == model
                                  else p.get("model_embeddings", {}).get(model, [])))]
        scores.sort(key=lambda item: item[0], reverse=True)
        if not scores:
            return {"id": None, "name": "Desconhecido", "owner": False, "score": 0.0}
        score, profile = scores[0]
        margin = score - (scores[1][0] if len(scores) > 1 else 0.0)
        if score < threshold or margin < 0.12:
            return {"id": None, "name": "Incerto", "owner": False, "score": round(score, 3)}
        return {"id": profile["id"], "name": profile["name"], "owner": profile["owner"],
                "voice_eligible": self.voice_eligible(profile),
                "score": round(score, 3), "margin": round(margin, 3)}

    def match_models(self, embeddings: dict[str, list[float]]) -> dict:
        """Accept one unambiguous model-specific match; never compare cross-model scores."""
        results = [self.match(vector, model) for model, vector in embeddings.items()]
        known = {item["id"]: item for item in results if item.get("id")}
        if len(known) == 1:
            return next(iter(known.values()))
        if len(known) > 1:
            return {"id": None, "name": "Incerto", "owner": False, "score": 0.0}
        return max(results, key=lambda item: item.get("score", 0.0),
                   default={"id": None, "name": "Desconhecido", "owner": False, "score": 0.0})
