"""One-use screen confirmations bound to immutable sensitive requests."""
from __future__ import annotations

import hashlib
import secrets
import time
import unicodedata
import uuid

WORDS = ("zero", "um", "dois", "tres", "quatro", "cinco", "seis", "sete", "oito", "nove")


def requires_screen(request: str) -> bool:
    """Extra friction only for deletion, money, credentials and permissions; host approvals still govern tools.

    Generic verbs (enviar, executar, publicar, terminal) are NOT here: they appear in ordinary requests
    such as reminders ("me lembra de enviar o ofício") and made them wait for a screen click.

    This is NOT a complete semantic classifier and never grants execution permission.
    Other requests proceed through the ordinary Hermes tool permissions.
    """
    words = set(normalize(request).split())
    return bool(words & {"apagar", "apague", "excluir", "exclua", "deletar", "delete", "remover",
                         "formatar", "format", "rm", "pagamento", "pagar", "pague", "pix", "transferir",
                         "transfira", "comprar", "compre", "senha", "token", "segredo", "credencial",
                         "permissao", "administrador", "payment", "password", "secret"})


def normalize(text: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    for digit, word in enumerate(WORDS):
        plain = plain.replace(str(digit), " " + word + " ")
    return " ".join("".join(c if c.isalpha() else " " for c in plain).split())


class Confirmations:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.pending: dict | None = None
        self.results: dict[str, dict] = {}

    def create(self, request: str, session: str, screen_only: bool = False) -> dict:
        self.expire()
        if self.pending:
            raise ValueError("Já existe uma confirmação pendente")
        if not request or len(request) > 12000 or not session:
            raise ValueError("Pedido inválido")
        digest = hashlib.sha256((session + "\0" + request).encode()).hexdigest()
        self.pending = {"id": uuid.uuid4().hex, "digest": digest, "request": request,
                        "session": session, "created": self.clock(), "expires": self.clock() + 90,
                        "screen_only": True}
        return dict(self.pending)

    def expire(self):
        if self.pending and self.clock() >= self.pending["expires"]:
            self._finish(False, "expired")
        self.results = {k: v for k, v in self.results.items() if self.clock() - v["at"] < 180}

    def _finish(self, allowed: bool, method: str):
        p = self.pending
        if p:
            self.results[p["id"]] = {"allowed": allowed, "method": method,
                                      "digest": p["digest"], "at": self.clock()}
        self.pending = None

    def screen(self, identity: str, digest: str, allow: bool):
        self.expire()
        p = self.pending
        if not p or p["id"] != identity or not secrets.compare_digest(p["digest"], digest):
            raise ValueError("Confirmação expirada ou diferente do pedido atual")
        self._finish(allow, "screen")

    def result(self, identity: str, digest: str) -> dict:
        self.expire()
        result = self.results.get(identity)
        if result and secrets.compare_digest(result["digest"], digest):
            # Only the delegation waiter consumes this one-use result.
            del self.results[identity]
            return {"done": True, "allowed": result["allowed"], "method": result["method"]}
        if self.pending and self.pending["id"] == identity and self.pending["digest"] == digest:
            return {"done": False}
        return {"done": True, "allowed": False, "method": "missing"}

    def cancel(self):
        self._finish(False, "cancelled")
