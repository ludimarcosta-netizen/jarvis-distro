import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "installer"))

from instalador import marcadores, sistema  # noqa: E402
from instalador.estado import AVISO, ERRO, OK, Estado  # noqa: E402
from instalador.etapas import Contexto  # noqa: E402
from instalador.passos import face, ia, reunioes, voz  # noqa: E402
from instalador.perfil import Perfil  # noqa: E402
from instalador.recursos import Catalogo  # noqa: E402
from test_nucleo import ExecutorFalso, Roteiro  # noqa: E402

CAT = Catalogo.carregar(RAIZ / "installer" / "recursos.toml")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        (self.home / "jarvis").mkdir(parents=True)
        self.perfil = Perfil(nome="Ana", nome_completo="Ana Souza", cidade="Vila Nova", cidade_curta="Vila",
                             pasta_usuario=str(self.tmp / "u"), pasta_cerebro=str(self.tmp / "c"),
                             pasta_downloads=str(self.tmp / "d"))
        self.estado = Estado(self.home / "estado.json", perfil=self.perfil)

    def ctx(self, respostas=None, ex=None, origem=RAIZ):
        self.roteiro = Roteiro(respostas or [])
        self.segredos = []
        con = self.roteiro.console()
        con._segredo = lambda p: self.segredos.pop(0) if self.segredos else ""
        return Contexto(origem, self.home, marcadores.valores(self.perfil, str(self.home)), con, self.estado,
                        ex or ExecutorFalso())


class TestIA(Base):
    def test_primeira_vez_escolhe_modelo_testa_e_instala_persona(self):
        ex = ExecutorFalso({" -z ": sistema.Saida(0, "carregando\nOK\n")})
        status, msg = ia.ia(self.ctx(ex=ex), CAT.recursos["ia"])
        self.assertEqual(status, OK, msg)
        soul = (self.home / "SOUL.md").read_text(encoding="utf-8")
        self.assertIn("Ana", soul)
        self.assertNotIn("__JARVIS_", soul)
        self.assertNotIn("CEREBRO_START", soul)
        self.assertTrue(ex.achou("config set display.skin jarvis"))
        self.assertTrue(ex.achou("config set timezone America/Sao_Paulo"))
        modelo = ex.achou(" model")
        self.assertTrue(modelo and modelo[0][1].get("interativo"))
        self.assertIn("teste respondeu: OK", msg)

    def test_ia_que_nao_responde_e_erro(self):
        ex = ExecutorFalso({" -z ": sistema.Saida(1, "")})
        self.assertEqual(ia.ia(self.ctx(ex=ex), CAT.recursos["ia"])[0], ERRO)

    def test_persona_preserva_bloco_do_cerebro_e_guarda_copia(self):
        alvo = self.home / "SOUL.md"
        alvo.write_text("Persona antiga\n<!-- CEREBRO_START -->\nmeu cofre\n<!-- CEREBRO_END -->\n", encoding="utf-8")
        ia.gravar_persona(self.ctx(["s"]))
        novo = alvo.read_text(encoding="utf-8")
        self.assertIn("J.A.R.V.I.S.", novo)
        self.assertTrue(novo.rstrip().endswith("meu cofre\n<!-- CEREBRO_END -->"))
        self.assertIn("Persona antiga", (self.home / "SOUL.md.bak-instalador").read_text(encoding="utf-8"))

    def test_persona_alheia_fica_se_o_usuario_recusar(self):
        alvo = self.home / "SOUL.md"
        alvo.write_text("Minha persona\n", encoding="utf-8")
        self.assertEqual(ia.gravar_persona(self.ctx(["n"])), "persona atual mantida")
        self.assertEqual(alvo.read_text(encoding="utf-8"), "Minha persona\n")

    def test_segunda_vez_so_troca_modelo_se_pedir(self):
        self.estado.marcar("ia", OK)
        ex = ExecutorFalso({" -z ": sistema.Saida(0, "OK")})
        ia.ia(self.ctx(["n"], ex=ex), CAT.recursos["ia"])
        self.assertFalse(ex.achou(" model"))


class TestFace(Base):
    def _repo(self, origem):
        repo = self.home / "hermes-agent"
        repo.mkdir()
        g = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "a.txt").write_text("um\n", encoding="utf-8")
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-qm", "base"], check=True)
        subprocess.run(g + ["tag", "v0.21.6"], check=True)
        (repo / "a.txt").write_text("um\nfeito por __JARVIS_USER__\n", encoding="utf-8")
        patch = subprocess.run(g + ["diff", "--binary"], check=True, capture_output=True).stdout
        subprocess.run(g + ["checkout", "-q", "--", "a.txt"], check=True)
        (origem / "hermes-patches" / "novos" / "apps").mkdir(parents=True)
        (origem / "hermes-patches" / "BASE.txt").write_text("v0.21.6\n", encoding="utf-8")
        (origem / "hermes-patches" / "jarvis.patch").write_bytes(patch)
        (origem / "hermes-patches" / "novos" / "apps" / "novo.ts").write_text("// __JARVIS_USER__\n", encoding="utf-8")
        return repo

    @unittest.skipUnless(shutil.which("git"), "git ausente")
    def test_patch_vira_commit_no_ramo_jarvis_com_marcadores_preenchidos(self):
        origem = self.tmp / "origem"
        repo = self._repo(origem)
        ctx = self.ctx(ex=sistema.Executor(), origem=origem)
        self.assertEqual(face.aplicar_patch(ctx)[0], OK)
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "um\nfeito por Ana\n")
        self.assertEqual((repo / "apps" / "novo.ts").read_text(encoding="utf-8"), "// Ana\n")
        ramo = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
        self.assertEqual(ramo.stdout.strip(), "jarvis")
        limpo = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
        self.assertEqual(limpo.stdout.strip(), "")
        self.assertEqual(face.aplicar_patch(ctx), (OK, "patch do Jarvis já aplicado"))

    @unittest.skipUnless(shutil.which("git"), "git ausente")
    def test_patch_recusa_arvore_com_alteracoes(self):
        origem = self.tmp / "origem"
        repo = self._repo(origem)
        (repo / "a.txt").write_text("mexido\n", encoding="utf-8")
        status, msg = face.aplicar_patch(self.ctx(ex=sistema.Executor(), origem=origem))
        self.assertEqual(status, ERRO)
        self.assertIn("alterações locais", msg)

    def test_sem_voz_desliga_sentinela_e_pergunta_autoinicio(self):
        ex = ExecutorFalso()
        ctx = self.ctx(["s"], ex=ex)
        exe = self.home / "hermes-agent" / face.EXE
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        status, msg = face.face(ctx, CAT.recursos["face"])
        self.assertEqual(status, OK, msg)
        self.assertTrue((self.home / "jarvis" / ".sentinel-disabled").exists())
        self.assertTrue(ex.achou("-Stage desktop"))
        self.assertTrue(ex.achou("plugins enable gemini-live-bridge"))
        self.assertTrue(ex.achou(r"CurrentVersion\Run /v Hermes"))


class TestVoz(Base):
    def test_gpu_usa_kokoro_cuda_e_grava_chave_sem_mostrar(self):
        ex = ExecutorFalso({"nvidia-smi": sistema.Saida(0, "GPU 0: NVIDIA RTX")})
        ctx = self.ctx(ex=ex)
        self.segredos = ["chave-secreta"]
        (self.home / "jarvis" / ".sentinel-disabled").write_text("x", encoding="utf-8")
        status, msg = voz.voz(ctx, CAT.recursos["voz"])
        self.assertEqual(status, OK, msg)
        self.assertTrue(ex.achou("requirements-kokoro-gpu.txt --extra-index-url https://download.pytorch.org/whl/cu124"))
        modo = json.loads((self.home / "jarvis" / "voice_mode.json").read_text(encoding="utf-8"))
        self.assertEqual((modo["stt_device"], modo["kokoro_device"], modo["tts_engine"]), ("cuda", "cuda", "kokoro"))
        self.assertIn("GOOGLE_API_KEY=chave-secreta", (self.home / ".env").read_text(encoding="utf-8"))
        self.assertFalse(any("chave-secreta" in s for s in self.roteiro.saida))
        self.assertFalse(any("chave-secreta" in c[0] for c in ex.comandos))
        self.assertFalse((self.home / "jarvis" / ".sentinel-disabled").exists())

    def test_sem_gpu_cpu_e_sem_chave_fica_aviso(self):
        ex = ExecutorFalso({"nvidia-smi": sistema.Saida(127, "")})
        status, msg = voz.voz(self.ctx(ex=ex), CAT.recursos["voz"])
        self.assertEqual(status, AVISO)
        self.assertTrue(ex.achou("requirements-kokoro.txt"))
        self.assertFalse(ex.achou("cu124"))
        self.assertIn("voz configurada para CPU", msg)

    def test_voice_mode_existente_e_mantido(self):
        arq = self.home / "jarvis" / "voice_mode.json"
        arq.write_text('{"engine": "off"}', encoding="utf-8")
        self.assertEqual(voz.gravar_modo(self.ctx(), True), "voice_mode.json mantido")
        self.assertEqual(arq.read_text(encoding="utf-8"), '{"engine": "off"}')

    def test_falha_no_ambiente_e_erro(self):
        ex = ExecutorFalso({"pip install": sistema.Saida(1, "")})
        self.assertEqual(voz.voz(self.ctx(ex=ex), CAT.recursos["voz"])[0], ERRO)


class TestReunioes(Base):
    def setUp(self):
        super().setUp()
        self.modelos = self.home / "voice_identity" / "models"
        self.modelos.mkdir(parents=True)
        (self.modelos / "speaker.onnx").write_bytes(b"padrao")
        (self.home / "voice_identity" / "settings.example.json").write_text('{"mode":"off"}', encoding="utf-8")

    def test_sem_ecapa_usa_padrao_com_aviso_e_cria_settings(self):
        status, msg = reunioes.reunioes(self.ctx([""]), CAT.recursos["reunioes"])
        self.assertEqual(status, AVISO, msg)
        self.assertEqual((self.modelos / reunioes.MODELO_FALANTE).read_bytes(), b"padrao")
        self.assertTrue((self.home / "voice_identity" / "settings.json").exists())

    def test_ecapa_informado_e_copiado(self):
        arq = self.tmp / "ecapa.onnx"
        arq.write_bytes(b"ecapa")
        self.assertEqual(reunioes.modelo_falante(self.ctx([f'"{arq}"']), self.modelos)[0], OK)
        self.assertEqual((self.modelos / reunioes.MODELO_FALANTE).read_bytes(), b"ecapa")

    def test_motor_que_nao_abre_e_erro(self):
        ex = ExecutorFalso({"from engine import Engine": sistema.Saida(1, "")})
        self.assertEqual(reunioes.reunioes(self.ctx([""], ex=ex), CAT.recursos["reunioes"])[0], ERRO)


class TestEnv(unittest.TestCase):
    def test_gravar_preserva_outras_e_substitui(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / ".env").write_text("A=1\nB=2\n", encoding="utf-8")
            sistema.gravar_env(home, "B", "3")
            sistema.gravar_env(home, "C", "4")
            self.assertEqual((home / ".env").read_text(encoding="utf-8"), "A=1\nB=3\nC=4\n")
            self.assertTrue(sistema.ler_env(home, "C"))
            self.assertFalse(sistema.ler_env(home, "D"))
            for chave, valor in (("b", "1"), ("X", "a\nY=2"), ("X", "")):
                with self.assertRaises(ValueError):
                    sistema.gravar_env(home, chave, valor)


if __name__ == "__main__":
    unittest.main()
