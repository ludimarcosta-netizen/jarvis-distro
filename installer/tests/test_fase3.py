import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "installer"))

from instalador import doctor, etapas, sistema  # noqa: E402
from instalador.estado import AVISO, ERRO, OK  # noqa: E402
from instalador.passos import canais, cerebro, documentos, servicos  # noqa: E402
from test_nucleo import ExecutorFalso  # noqa: E402
from test_passos import CAT, Base  # noqa: E402

JARVIS_PY = os.environ.get("JARVIS_TEST_PY", "")
TOKEN_BOT ="123456:" + "A" * 35


class TestRegistro(unittest.TestCase):
    def test_toda_etapa_do_catalogo_existe(self):
        usadas = {e for r in CAT.recursos.values() for e in r.etapas}
        self.assertEqual(usadas - set(etapas.ETAPAS), set())


class TestCerebro(Base):
    def test_cria_cofre_registra_mcp_e_agenda(self):
        ex = ExecutorFalso()
        status, msg = cerebro.cerebro(self.ctx(["s"], ex=ex), CAT.recursos["cerebro"])
        self.assertEqual(status, OK, msg)
        for pasta in cerebro.PASTAS:
            self.assertTrue((self.tmp / "c" / pasta).is_dir())
        mcp = ex.achou("mcp add cerebro")
        self.assertTrue(mcp and f"JARVIS_CEREBRO_DIR={self.tmp / 'c'}" in mcp[0][0])
        self.assertEqual(len(ex.achou("schtasks /Create")), 2)

    def test_sem_agendar_e_teste_falho(self):
        ex = ExecutorFalso({"cerebro_memory.py status": sistema.Saida(1, "")})
        status, _ = cerebro.cerebro(self.ctx(["n"], ex=ex), CAT.recursos["cerebro"])
        self.assertEqual(status, ERRO)
        self.assertFalse(ex.achou("schtasks"))

    @unittest.skipUnless(JARVIS_PY and Path(JARVIS_PY).exists(), "defina JARVIS_TEST_PY com o Python de um Jarvis")
    def test_blocos_reais_no_soul_e_no_cofre(self):
        from instalador import marcadores
        marcadores.copiar(RAIZ / "jarvis", self.home / "jarvis", self.ctx().valores)
        (self.home / "SOUL.md").write_text("Persona\n", encoding="utf-8")
        cofre = self.tmp / "c"
        cofre.mkdir()
        py = JARVIS_PY
        s = sistema.Executor().rodar([py, "-c", cerebro._BLOCOS, str(self.home / "jarvis"), py, str(cofre)], timeout=120)
        self.assertTrue(s.ok, s.texto)
        self.assertIn("<!-- CEREBRO_START -->", (self.home / "SOUL.md").read_text(encoding="utf-8"))
        self.assertIn(str(cofre), (cofre / "AGENTS.md").read_text(encoding="utf-8"))


class TestTelegram(Base):
    def test_token_invalido_e_erro(self):
        ctx = self.ctx()
        self.segredos = ["nao-e-token"]
        self.assertEqual(canais.telegram(ctx, CAT.recursos["telegram"])[0], ERRO)

    def test_configura_e_instala_gateway_sem_expor_token(self):
        ex = ExecutorFalso()
        ctx = self.ctx(["123456789"], ex=ex)
        self.segredos = [TOKEN_BOT]
        status, msg = canais.telegram(ctx, CAT.recursos["telegram"])
        self.assertEqual(status, OK, msg)
        env = (self.home / ".env").read_text(encoding="utf-8")
        self.assertIn(f"TELEGRAM_BOT_TOKEN={TOKEN_BOT}", env)
        self.assertIn("TELEGRAM_ALLOWED_USERS=123456789", env)
        self.assertTrue(ex.achou("gateway install --start-now --start-on-login"))
        self.assertFalse(any(TOKEN_BOT in c[0] for c in ex.comandos))


class TestWhatsApp(Base):
    def test_sem_docker_e_erro(self):
        ex = ExecutorFalso({"docker version": sistema.Saida(1, "")})
        self.assertEqual(canais.whatsapp(self.ctx(ex=ex), CAT.recursos["whatsapp"])[0], ERRO)

    def test_pasta_e_env_com_segredos_aleatorios(self):
        env = canais.preparar_pasta(self.ctx())
        texto = env.read_text(encoding="utf-8")
        self.assertNotIn("TROQUE_POR", texto)
        self.assertEqual(len(canais.chave_api(env)), 64)
        self.assertTrue((env.parent.parent / "docker-compose.yml").is_file())
        antes = texto
        canais.preparar_pasta(self.ctx())  # rodar de novo não troca as senhas
        self.assertEqual(env.read_text(encoding="utf-8"), antes)

    def test_fluxo_pareado(self):
        estados = iter([(404, {}), (200, {"instance": {"state": "open"}})])

        def chamar(metodo, caminho, chave, corpo=None, timeout=20):
            if caminho.startswith("/instance/connectionState"):
                return next(estados)
            return (201 if metodo == "POST" else 200), {}

        with mock.patch.object(canais, "chamar", side_effect=chamar):
            status, msg = canais.whatsapp(self.ctx(), CAT.recursos["whatsapp"])
        self.assertEqual(status, OK, msg)


class TestDocumentos(Base):
    def test_prefeitura_cria_base_com_numero_escolhido(self):
        ex = ExecutorFalso()
        status, msg = documentos.prefeitura(self.ctx(["42"], ex=ex), CAT.recursos["prefeitura"])
        self.assertEqual(status, OK, msg)
        pasta = documentos.pasta_oficios(self.ctx())
        self.assertEqual(json.loads((pasta / "oficios.json").read_text(encoding="utf-8")),
                         {"next_number": 42, "records": []})
        script = (pasta / "oficios.py").read_text(encoding="utf-8")
        self.assertNotIn("__JARVIS_", script)
        self.assertIn(str(pasta.as_posix()), script)

    def test_prefeitura_numero_invalido(self):
        self.assertEqual(documentos.prefeitura(self.ctx(["zero"]), CAT.recursos["prefeitura"])[0], ERRO)

    def test_pdf_sem_certificado_fica_aviso(self):
        ex = ExecutorFalso({"certificates": sistema.Saida(0, "[]")})
        status, msg = documentos.pdf(self.ctx(ex=ex), CAT.recursos["pdf"])
        self.assertEqual(status, AVISO)
        self.assertTrue(ex.achou("--python 3.12"))
        self.assertIn("certificado", msg)


class TestServicos(Base):
    def test_google_codigo_nao_vai_para_o_log(self):
        setup = self.home / "skills" / "productivity" / "google-workspace" / "scripts" / "setup.py"
        setup.parent.mkdir(parents=True)
        setup.write_text("", encoding="utf-8")
        segredo = self.tmp / "client.json"
        segredo.write_text("{}", encoding="utf-8")
        checks = iter([sistema.Saida(1), sistema.Saida(0)])

        class Ex(ExecutorFalso):
            def rodar(s, args, **kw):
                if "--check" in args:
                    s.comandos.append((" ".join(map(str, args)), kw))
                    return next(checks)
                if "--auth-url" in args:
                    return sistema.Saida(0, "https://accounts.example/auth")
                return super().rodar(args, **kw)

        ex = Ex()
        status, msg = servicos.google(self.ctx([str(segredo), "codigo-123"], ex=ex), CAT.recursos["google"])
        self.assertEqual(status, OK, msg)
        self.assertEqual(ex.achou("--auth-code")[0][1].get("ocultar"), 1)

    def test_spotify_grava_client_id(self):
        cfg = self.home / "jarvis" / "config.toml"
        cfg.write_text('[spotify]\nclient_id = ""\n', encoding="utf-8")
        servicos.gravar_client_id(cfg, "a" * 32)
        self.assertEqual(cfg.read_text(encoding="utf-8"), f'[spotify]\nclient_id = "{"a" * 32}"\n')
        self.estado.marcar("voz", OK)
        (self.home / "jarvis" / ".venv311" / "Scripts").mkdir(parents=True)
        (self.home / "jarvis" / ".venv311" / "Scripts" / "python.exe").write_bytes(b"")
        self.assertEqual(servicos.spotify(self.ctx(["xyz"]), CAT.recursos["spotify"])[0], ERRO)

    def test_navegador(self):
        with mock.patch.object(servicos, "achar_chrome", return_value=None):
            self.assertEqual(servicos.navegador(self.ctx(), CAT.recursos["navegador"])[0], ERRO)
        ex = ExecutorFalso()
        with mock.patch.object(servicos, "achar_chrome", return_value="chrome.exe"):
            self.assertEqual(servicos.navegador(self.ctx(ex=ex), CAT.recursos["navegador"])[0], OK)
        self.assertTrue(ex.achou("config set browser.auto_launch_cdp true"))

    def test_antigravity_recusado(self):
        with mock.patch.object(servicos, "achar_agy", return_value=None):
            self.assertEqual(servicos.antigravity(self.ctx(["n"]), CAT.recursos["antigravity"])[0], AVISO)

    def test_homeassistant(self):
        ctx = self.ctx(["", ""])
        self.segredos = ["tok"]
        with mock.patch.object(servicos, "testar_hass", return_value=False):
            self.assertEqual(servicos.homeassistant(ctx, CAT.recursos["homeassistant"])[0], ERRO)
        self.assertFalse((self.home / ".env").exists())
        ctx = self.ctx([""])
        self.segredos = ["tok"]
        with mock.patch.object(servicos, "testar_hass", return_value=True):
            self.assertEqual(servicos.homeassistant(ctx, CAT.recursos["homeassistant"])[0], OK)
        self.assertIn("HASS_TOKEN=tok", (self.home / ".env").read_text(encoding="utf-8"))

    def test_agendamentos(self):
        self.assertEqual(servicos.agendamentos(self.ctx(), CAT.recursos["agendamentos"])[0], AVISO)
        self.estado.marcar("telegram", OK)
        ex = ExecutorFalso()
        self.assertEqual(servicos.agendamentos(self.ctx(["07:30"], ex=ex), CAT.recursos["agendamentos"])[0], OK)
        self.assertTrue(ex.achou("cron create 30 7 * * *"))


class TestDoctor(Base):
    def test_lista_pendencias(self):
        self.estado.escolhidos = ["telegram"]
        self.estado.marcar("base", OK)
        self.estado.marcar("ia", OK)
        self.estado.marcar("telegram", AVISO, "gateway parado")
        pendentes = doctor.rodar(self.ctx(), CAT)
        saida = "\n".join(self.roteiro.saida)
        self.assertIn("falta: TELEGRAM_BOT_TOKEN no .env", saida)
        self.assertIn("gateway parado", saida)
        self.assertGreaterEqual(pendentes, 3)


if __name__ == "__main__":
    unittest.main()
