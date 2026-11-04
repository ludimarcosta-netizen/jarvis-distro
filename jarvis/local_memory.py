"""Local-only memory review with scoped references and immutable confirmations."""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

from cerebro_memory import Memory
from cerebro_review import Reviews, body, digest, safe_text

TTL = 600


def profile_memory(home: str) -> Memory:
    # A profile must opt in to the vault through its own config or the explicit shared setting.
    import os

    import tomllib

    configured = os.environ.get("JARVIS_CEREBRO_DIR")
    if configured is None:
        config = Path(home) / "jarvis/config.toml"
        if not config.is_file():
            config = Path(home) / "config.toml"
        try:
            with config.open("rb") as stream:
                configured = tomllib.load(stream).get("cerebro", {}).get("path", "")
        except (OSError, tomllib.TOMLDecodeError):
            configured = ""
    if not configured:
        raise ValueError("Segundo Cérebro não configurado neste perfil.")
    return Memory(Path(configured))


class MemorySession:
    def __init__(self, factory, *, clock=time.monotonic):
        self.factory, self.clock = factory, clock
        self.refs: dict[str, tuple[float, str, str, str]] = {}
        self.tickets: dict[str, tuple[float, dict]] = {}
        self.lock = threading.Lock()

    def _selected(self, store: Reviews, ref):
        saved = self.refs.get(ref) if isinstance(ref, str) else None
        if not saved or self.clock() - saved[0] > TTL:
            raise ValueError("Referência vencida; busque a memória novamente.")
        _, target, expected, previous = saved
        _, raw = store.note(target)
        latest = store.latest()
        event = latest.get(target)
        if digest(raw) != expected or (event["id"] if event else "") != previous:
            raise ValueError("A nota mudou desde a busca. Busque novamente.")
        return target, raw, latest

    def dispatch(self, action: str, request: dict) -> dict:
        if not self.lock.acquire(blocking=False):
            raise ValueError("Aguarde a operação de memória em andamento.")
        try:
            memory = self.factory()
            store = Reviews(memory.vault)
            if action == "memory.search":
                return self.search(memory, store, request)
            if action == "memory.confirm":
                ticket = request.get("ticket")
                saved = self.tickets.pop(ticket, None) if isinstance(ticket, str) else None
                if not saved or self.clock() - saved[0] > TTL:
                    raise ValueError("Revisão vencida ou já confirmada; confira a nota novamente.")
                result = store.commit(**saved[1])
                self.refs.clear()
                self.tickets.clear()
                return {**result, "message": "Revisão salva. O original foi preservado; novas consultas usarão esta revisão."}
            target, raw, latest = self._selected(store, request.get("ref"))
            view = store.view(target, raw, latest)
            if action == "memory.select":
                text = body(view["text"] or raw)
                return {"source": safe_text(target), "text": safe_text(text)[:4000], "state": view["state"],
                        "editable": len(body(raw)) <= 4000 and safe_text(raw) == raw,
                        "truncated": len(text) > 4000,
                        "history": [{key: safe_text(event[key]) for key in ("action", "reason", "at", "source")}
                                    for event in store.events() if event["target"] == target][-10:]}
            if action == "memory.prepare":
                return self.prepare(store, target, raw, latest, request)
            raise ValueError("Ação de memória inválida.")
        finally:
            self.lock.release()

    def search(self, memory: Memory, store: Reviews, request: dict) -> dict:
        query = request.get("query")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 150:
            raise ValueError("Informe uma busca de até 150 caracteres.")
        self.refs.clear()
        self.tickets.clear()
        # Reviewing disabled notes needs the original source index, not hidden records in normal search.
        rows = memory.search(query, 10, semantic=False)
        latest = store.latest()
        partial = False
        if request.get("include_inactive") is True:
            terms = query.casefold().split()
            started, size = self.clock(), 0
            learned = (p.relative_to(memory.vault).as_posix() for p in
                       (memory.vault / "perfil/aprendido").glob("*.md"))
            # Include automatically superseded notes as well as explicit panel reviews.
            from itertools import chain
            for index, target in enumerate(chain(latest, learned)):
                if index >= 100 or size > 2_000_000 or self.clock() - started > 1:
                    partial = True
                    break
                try:
                    _, raw = store.note(target)
                except (OSError, ValueError):
                    continue
                size += len(raw)
                if store.view(target, raw, latest)["state"] == "disabled" and all(word in (target + " " + raw).casefold() for word in terms):
                    rows.append({"path": target, "title": Path(target).stem, "body": "Nota desativada nas consultas.", "section": "", "data": latest.get(target, {}).get("at", "")})
        results = []
        seen = set()
        for row in rows[:20]:
            target = row["path"]
            if target in seen:
                continue
            seen.add(target)
            try:
                _, raw = store.note(target)
            except (OSError, ValueError):
                continue
            view = store.view(target, raw, latest)
            ref = secrets.token_urlsafe(24)
            self.refs[ref] = (self.clock(), target, digest(raw), latest.get(target, {}).get("id", ""))
            results.append({"ref": ref, "title": safe_text(row["title"]), "source": safe_text(target),
                            "excerpt": safe_text(row["body"])[:1000], "date": safe_text(str(row.get("data", ""))), "state": view["state"]})
        return {"results": results, "partial": partial,
                "notice": "Busca de notas revisáveis. Revisões afetam a nota inteira nas consultas do Cerebro atualizado; o Markdown original e outras cópias permanecem."}

    def prepare(self, store: Reviews, target: str, raw: str, latest: dict, request: dict) -> dict:
        action, replacement, reason = request.get("change"), request.get("text", ""), request.get("reason")
        if not isinstance(action, str) or action not in {"correct", "disable", "restore"}:
            raise ValueError("Escolha corrigir, desativar ou restaurar.")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 300 or safe_text(reason) != reason:
            raise ValueError("Informe um motivo de até 300 caracteres, sem dados protegidos.")
        if not isinstance(replacement, str) or len(replacement) > 4000 or safe_text(replacement) != replacement:
            raise ValueError("Texto inválido ou com dados protegidos.")
        if action == "correct" and (not replacement.strip() or len(body(raw)) > 4000 or safe_text(raw) != raw):
            raise ValueError("Correção textual indisponível ou vazia. Notas longas devem usar o editor local.")
        self.tickets.clear()
        ticket = secrets.token_urlsafe(24)
        args = {"target": target, "expected": digest(raw), "previous": latest.get(target, {}).get("id", ""),
                "action": action, "replacement": replacement if action == "correct" else "", "reason": reason}
        self.tickets[ticket] = (self.clock(), args)
        return {"ticket": ticket, "source": safe_text(target), "change": action,
                "text": replacement if action == "correct" else "", "reason": reason,
                "notice": "Confirme a revisão desta nota inteira. Nenhum arquivo original será apagado ou sobrescrito."}
