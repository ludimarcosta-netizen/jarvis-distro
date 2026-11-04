"""Ledger de orquestração com executores falsos no PATH (mesmo caminho .cmd dos CLIs reais)."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orquestracao
import orquestracao_mcp

FAKE = r'''
import json, sys, time
executor, tarefa = sys.argv[1], (sys.argv[sys.argv.index("-p") + 1] if sys.argv[1] == "agy" else sys.stdin.read())
if "DORMIR" in tarefa:
    time.sleep(60)
if "QUEBRAR" in tarefa:
    print("falha simulada", file=sys.stderr); sys.exit(3)
resposta = "eco: " + tarefa.strip()
if executor == "claude":
    print(json.dumps({"type": "result", "is_error": False, "result": resposta, "session_id": "s1",
        "total_cost_usd": 0.0125, "usage": {"input_tokens": 10, "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 5, "output_tokens": 7}, "modelUsage": {"claude-haiku-5-5": {}},
        "argv": sys.argv[2:]}))
elif executor == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": "nao-e-uuid"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": resposta}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 20, "output_tokens": 3}}))
else:
    print(json.dumps({"conversation_id": "c1", "status": "SUCCESS", "response": resposta,
        "usage": {"input_tokens": 40, "output_tokens": 2, "cache_read_tokens": 0}}))
'''


class OrquestracaoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "ledger.db"
        self.pasta = self.root / "projeto"
        self.pasta.mkdir()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "fake.py").write_text(FAKE, encoding="utf-8")
        for nome in ("claude", "codex", "agy"):
            (bin_dir / f"{nome}.cmd").write_text(f'@"{sys.executable}" -B "{bin_dir / "fake.py"}" {nome} %*\r\n',
                                                 encoding="utf-8")
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        env.pop(orquestracao.PROFUNDIDADE_ENV, None)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(orquestracao.PROFUNDIDADE_ENV, None)

    def iniciar(self, executor="claude", tarefa="analise o projeto", **extra):
        extra.setdefault("aguardar", True)
        return orquestracao.iniciar(executor, tarefa, str(self.pasta), origem="claude", db=self.db, **extra)

    def linha(self, execucao):
        with closing(orquestracao.connect(self.db)) as conn:
            return dict(conn.execute("SELECT * FROM execucoes WHERE id=?", (execucao,)).fetchone())

    def test_claude_metrics_and_requested_vs_observed_model(self):
        resultado = self.iniciar(modelo="haiku", esforco="low")
        self.assertEqual(resultado["status"], "concluido")
        self.assertEqual(resultado["resposta"], "eco: analise o projeto")
        self.assertEqual((resultado["modelo_pedido"], resultado["modelo_observado"]), ("haiku", "claude-haiku-5-5"))
        self.assertEqual((resultado["tokens_entrada"], resultado["tokens_cache"], resultado["tokens_saida"]), (115, 100, 7))
        self.assertEqual(resultado["custo_estimado_usd"], 0.0125)
        self.assertIsNone(resultado["validacao_ok"])

    def test_read_mode_never_grants_edits(self):
        args = orquestracao.comando("claude", "leitura", "", "", "x")
        self.assertNotIn("acceptEdits", args)
        self.assertIn("read-only", orquestracao.comando("codex", "leitura", "", "", "x"))
        self.assertIn("plan", orquestracao.comando("antigravity", "leitura", "", "", "x"))
        self.assertIn("acceptEdits", orquestracao.comando("claude", "escrita", "", "", "x"))

    def test_codex_and_antigravity_unknown_cost_stays_null(self):
        for executor in ("codex", "antigravity"):
            resultado = self.iniciar(executor)
            self.assertEqual(resultado["status"], "concluido", executor)
            self.assertEqual(resultado["resposta"], "eco: analise o projeto")
            self.assertIsNone(resultado["custo_estimado_usd"])
            self.assertIsNone(resultado["modelo_observado"])
        self.assertEqual(resultado["tokens_entrada"], 40)

    def test_shell_metacharacters_reach_executor_literally(self):
        tarefa = 'diga "oi" & echo INJETADO | more > %TEMP%\\x.txt'
        self.assertEqual(self.iniciar(tarefa=tarefa)["resposta"], "eco: " + tarefa)

    def test_validation(self):
        self.assertEqual(self.iniciar(validacao="contem", esperado="projeto")["status"], "concluido")
        falhou = self.iniciar(validacao="exato", esperado="outra coisa")
        self.assertEqual((falhou["status"], falhou["validacao_ok"]), ("validacao_falhou", 0))
        self.assertEqual(self.iniciar(validacao="json")["status"], "validacao_falhou")
        with self.assertRaises(ValueError):
            self.iniciar(validacao="contem")

    def test_executor_failure_keeps_stderr_summary(self):
        resultado = self.iniciar(tarefa="QUEBRAR")
        self.assertEqual(resultado["status"], "falhou")
        self.assertIn("falha simulada", resultado["erro"])

    def test_refusals(self):
        with self.assertRaises(ValueError):
            self.iniciar("codex", modo="escrita")  # sem .git
        with self.assertRaises(ValueError):
            orquestracao.iniciar("claude", "x", "relativo", origem="claude", db=self.db)
        with self.assertRaises(ValueError):
            self.iniciar(modelo="haiku & del")
        with mock.patch.dict(os.environ, {orquestracao.PROFUNDIDADE_ENV: "1"}), self.assertRaises(ValueError):
            self.iniciar()

    def test_detached_run_and_concurrency_limit(self):
        primeiro = self.iniciar(tarefa="DORMIR", aguardar=False)
        bloqueador = self.iniciar(tarefa="DORMIR", aguardar=False)
        with self.assertRaises(ValueError):
            self.iniciar(aguardar=False)
        time.sleep(1)  # o worker grava o próprio PID ao começar
        orquestracao.encerrar_arvore(self.linha(bloqueador["id"])["pid"])
        segundo = self.iniciar(tarefa="em segundo plano", aguardar=False)
        prazo = time.time() + 60
        while orquestracao.consultar(segundo["id"], self.db)["status"] == "rodando" and time.time() < prazo:
            time.sleep(0.5)
        self.assertEqual(orquestracao.consultar(segundo["id"], self.db)["resposta"], "eco: em segundo plano")
        self.assertEqual(self.linha(primeiro["id"])["status"], "rodando")
        orquestracao.encerrar_arvore(self.linha(primeiro["id"])["pid"])
        self.assertEqual(orquestracao.consultar(primeiro["id"], self.db)["status"], "incerto")

    def test_timeout_kills_process_tree(self):
        execucao = uuid.uuid4().hex
        with closing(orquestracao.connect(self.db)) as conn, conn:
            conn.execute("""INSERT INTO execucoes(id,criado,atualizado,origem,executor,modo,pasta,tarefa,status)
                VALUES(?,?,?,'manual','claude','leitura',?,'DORMIR','rodando')""",
                         (execucao, time.time(), time.time(), str(self.pasta)))
        inicio = time.monotonic()
        orquestracao.executar(execucao, "DORMIR", 3, self.db)
        self.assertLess(time.monotonic() - inicio, 30)
        self.assertEqual(self.linha(execucao)["status"], "tempo_esgotado")

    def test_report_marks_unknown_cost_and_reads_hermes_subagents(self):
        self.iniciar()
        self.iniciar("codex")
        hermes = self.root / "state.db"
        with closing(sqlite3.connect(hermes)) as conn, conn:
            conn.execute("""CREATE TABLE sessions(source TEXT, model TEXT, billing_provider TEXT, end_reason TEXT,
                started_at REAL, ended_at REAL, input_tokens INT, cache_read_tokens INT, output_tokens INT,
                estimated_cost_usd REAL, cost_status TEXT)""")
            conn.execute("INSERT INTO sessions VALUES('subagent','gpt-6.1-sol','openai-codex','agent_close',?,?,100,10,5,0,'included')",
                         (time.time() - 50, time.time()))
        relatorio = orquestracao.relatorio(7, self.db, hermes)
        por_executor = {g["executor"]: g for g in relatorio["ledger"]}
        self.assertEqual(por_executor["claude"]["custo_estimado_usd"], 0.0125)
        self.assertEqual(por_executor["codex"]["sem_custo"], 1)
        self.assertEqual(relatorio["subagentes_hermes"][0]["modelo"], "gpt-6.1-sol")
        self.assertEqual(orquestracao.relatorio(7, self.db, self.root / "nao-existe" / "x.db")["subagentes_hermes"],
                         "Banco do Hermes indisponível")

    def test_mcp_forces_read_mode(self):
        nomes = {t["name"] for t in orquestracao_mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
        self.assertEqual(nomes, set(orquestracao_mcp.FUNCTIONS))
        chamadas = []
        with mock.patch.object(orquestracao, "iniciar", lambda *a, **k: chamadas.append((a, k)) or {"id": "x"}):
            resposta = orquestracao_mcp.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "orquestracao_executar",
                "arguments": {"executor": "codex", "tarefa": "t", "pasta": str(self.pasta), "origem": "claude"}}})
            recusada = orquestracao_mcp.dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "orquestracao_executar", "arguments": {"executor": "codex", "tarefa": "t",
                                                              "pasta": str(self.pasta), "origem": "claude", "modo": "escrita"}}})
        self.assertFalse(resposta["result"]["isError"])
        self.assertEqual(chamadas[0][1]["modo"], "leitura")
        self.assertTrue(recusada["result"]["isError"])
        self.assertEqual(len(chamadas), 1)


class InterpretarTest(unittest.TestCase):
    def test_codex_model_from_session_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = str(uuid.uuid4())
            dia = Path(tmp) / ".codex/sessions/2026/10/24"
            dia.mkdir(parents=True)
            (dia / f"rollout-2026-10-24T10-00-00-{thread}.jsonl").write_text(
                json.dumps({"type": "session_meta"}) + "\n" +
                json.dumps({"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}}) + "\n", encoding="utf-8")
            self.assertEqual(orquestracao.modelo_codex(thread, Path(tmp)), "gpt-6.1-sol")
            self.assertIsNone(orquestracao.modelo_codex("../../etc", Path(tmp)))

    def test_claude_error_and_garbage(self):
        self.assertTrue(orquestracao.interpretar("claude", json.dumps({"is_error": True, "result": "limite"}))["erro_executor"])
        self.assertTrue(orquestracao.interpretar("claude", "texto solto")["erro_executor"])
        self.assertTrue(orquestracao.interpretar("antigravity", json.dumps({"status": "ERROR"}))["erro_executor"])


if __name__ == "__main__":
    unittest.main()
