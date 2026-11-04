import importlib.util
import json
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

HOME = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HOME / "profiles" / "vigia" / "scripts"))
import vigia_entrega  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "vigia_reply_context", HOME / "jarvis" / "vigia_reply_context.py"
)
reply = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reply)

PROPOSAL = {
    "chave": "oauth_timeout",
    "titulo": "Configurar timeout no OAuth do Vertex",
    "evidencia": "7 ConnectTimeout em oauth2.googleapis.com/token.",
    "sugestao": "Adicionar timeout e retentativas no transporte do google-auth.",
    "fonte": "https://googleapis.dev/python/google-auth/latest/user-guide.html",
    "esforco": "baixo",
    "risco": "Baixo.",
    "verificar": "Renovar o token e conferir o log.",
}


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def preflight(self, message):
        pass

    def send(self, message):
        self.sent.append(message)
        return "100"


class FakeCtx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)


class VigiaReplyContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "cron").mkdir()
        self.history = root / "propostas.md"
        report = root / "2026-10-19_17-00-33.md"
        second = dict(PROPOSAL, chave="ripgrep_path", titulo="Colocar o ripgrep no PATH")
        report.write_text(
            "# Vigia\n\n## Response\n" + json.dumps({"propostas": [PROPOSAL, second]}), encoding="utf-8"
        )
        self.telegram = FakeTelegram()
        with closing(vigia_entrega.open_queue(root)) as con:
            vigia_entrega.stage_report(con, report, self.history)
            vigia_entrega.archive(con, self.history)
            vigia_entrega.deliver(con, self.telegram, now=1e12)
        self.queue = root / "cron" / "vigia.db"
        self._paths = (reply.QUEUE, reply.HISTORY)
        reply.QUEUE, reply.HISTORY = self.queue, self.history
        ctx = FakeCtx()
        reply.register(ctx)
        (self.hook,) = ctx.hooks["pre_llm_call"]

    def tearDown(self):
        reply.QUEUE, reply.HISTORY = self._paths
        self.tmp.cleanup()

    def agent_context(self, text):
        result = self.hook(session_id="tg", user_message=text, conversation_history=[])
        return result["context"] if result else None

    def test_instala_1_after_delivery_reaches_agent_with_proposal_1(self):
        self.assertEqual(len(self.telegram.sent), 1)
        context = self.agent_context("instala 1")
        self.assertIn("Proposta 1: Configurar timeout no OAuth do Vertex", context)
        self.assertIn("Adicionar timeout e retentativas", context)
        self.assertIn("Renovar o token e conferir o log", context)
        self.assertIn("2026-10-19", context)
        self.assertNotIn("ripgrep", context)

    def test_variants_and_multiple_numbers(self):
        self.assertIn("ripgrep", self.agent_context("Pode instalar a 2"))
        both = self.agent_context("aprova 1 e 2")
        self.assertIn("Proposta 1:", both)
        self.assertIn("Proposta 2:", both)
        self.assertIn("não existe nesta entrega", self.agent_context("instala 3"))

    def test_other_messages_get_no_context(self):
        for text in ("instala o Spotify", "bom dia", "instala 1 programa novo no computador da Beltrana agora"):
            self.assertIsNone(self.agent_context(text))
        self.assertIsNone(self.hook(user_message=None))

    def test_queue_is_read_only_and_untouched(self):
        before = self.queue.read_bytes()
        self.agent_context("instala 1")
        self.assertEqual(self.queue.read_bytes(), before)

    def test_no_delivery_means_no_context(self):
        reply.QUEUE = Path(self.tmp.name) / "inexistente.db"
        self.assertIsNone(self.agent_context("instala 1"))


if __name__ == "__main__":
    unittest.main()
