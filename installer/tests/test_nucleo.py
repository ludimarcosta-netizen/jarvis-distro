import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "installer"))

from instalador import __main__ as principal  # noqa: E402
from instalador import marcadores, sistema  # noqa: E402
from instalador.console import Console  # noqa: E402
from instalador.estado import AVISO, OK, Estado  # noqa: E402
from instalador.perfil import Perfil, PerfilInvalido, validar  # noqa: E402
from instalador.recursos import Catalogo  # noqa: E402

PASTAS_DISTRIBUIDAS = ["jarvis", "plugins", "desktop-plugins", "voice_identity", "skins", "skills"]


def perfil(**extra) -> Perfil:
    base = dict(nome="Ana", nome_completo="Ana Maria Souza", cidade="São João do Céu", cidade_curta="São João",
                pasta_usuario=r"C:\Users\ana", pasta_cerebro=r"D:\Notas", pasta_downloads=r"C:\Users\ana\Downloads")
    return Perfil(**(base | extra))


class Roteiro:
    """Respostas pré-gravadas para o Console."""

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.saida = []

    def ler(self, pergunta):
        self.saida.append(pergunta)
        if not self.respostas:
            raise AssertionError(f"pergunta sem resposta: {pergunta}")
        return self.respostas.pop(0)

    def console(self):
        return Console(self.ler, self.saida.append, lambda p: (_ for _ in ()).throw(AssertionError(p)))


class ExecutorFalso(sistema.Executor):
    """Registra os comandos; responde pelo primeiro trecho de `respostas` que aparecer no comando."""

    def __init__(self, respostas=None):
        super().__init__()
        self.respostas = respostas or {}
        self.comandos = []

    def rodar(self, args, **kw):
        linha = " ".join(map(str, args))
        self.comandos.append((linha, kw))
        for trecho, saida in self.respostas.items():
            if trecho in linha:
                return saida
        return sistema.Saida(0, "")

    def achou(self, trecho):
        return [c for c in self.comandos if trecho in c[0]]


class TestMarcadores(unittest.TestCase):
    def setUp(self):
        self.v = marcadores.valores(perfil(), r"C:\Users\ana\AppData\Local\hermes")

    def test_variantes(self):
        v = self.v
        self.assertEqual(v["USER_LOWER"], "ana")
        self.assertEqual(v["CIDADE_FOLD"], "sao joao do ceu")
        self.assertEqual(v["CIDADE_ASCII"], "Sao Joao do Ceu")
        self.assertEqual(v["CIDADE_UPPER"], "SÃO JOÃO DO CÉU")
        self.assertEqual(v["CIDADE_URL"], "S%C3%A3o%20Jo%C3%A3o%20do%20C%C3%A9u")
        self.assertEqual(v["CIDADE_CURTA_FOLD"], "sao joao")
        self.assertEqual(v["CEREBRO_FS"], "D:/Notas")
        self.assertEqual(v["USERPROFILE_BS2"], r"C:\\Users\\ana")

    def test_regex_aceita_com_e_sem_acento(self):
        r = re.compile(self.v["CIDADE_REGEX"], re.IGNORECASE)
        for texto in ("São João do Céu", "sao joao do ceu", "SÃO  JOÃO DO CEU"):
            self.assertTrue(r.fullmatch(texto), texto)
        self.assertFalse(r.fullmatch("Sao Jose do Ceu"))

    def test_regex_escapa_metacaracteres(self):
        self.assertEqual(marcadores.como_regex("A.b"), r"a\.b")

    def test_token_fica_intacto_e_desconhecido_e_relatado(self):
        texto, faltando = marcadores.preencher("__JARVIS_USER__ __JARVIS_TOKEN__ __JARVIS_NOVO__", self.v)
        self.assertEqual(texto, "Ana __JARVIS_TOKEN__ __JARVIS_NOVO__")
        self.assertEqual(faltando, {"NOVO"})

    def test_todos_os_marcadores_da_distribuicao_tem_valor(self):
        usados = set()
        for pasta in PASTAS_DISTRIBUIDAS:
            for f in (RAIZ / pasta).rglob("*"):
                if f.is_file():
                    usados |= set(marcadores.PADRAO.findall(f.read_bytes().decode("utf-8", "ignore")))
        self.assertTrue(usados, "nenhum marcador encontrado: a exportação está vazia?")
        self.assertEqual(usados - set(self.v) - marcadores.RESERVADOS, set())

    def test_copiar_preenche_texto_e_preserva_binario(self):
        with tempfile.TemporaryDirectory() as d:
            origem, destino = Path(d, "o"), Path(d, "d")
            (origem / "sub").mkdir(parents=True)
            (origem / "sub" / "a.py").write_text("x = '__JARVIS_CEREBRO_BS2__'\r\n", encoding="utf-8", newline="")
            (origem / "b.bin").write_bytes(b"\xff\x00__JARVIS_USER__")
            (destino).mkdir()
            (destino / "so_no_destino.txt").write_text("fica", encoding="utf-8")
            res = marcadores.copiar(origem, destino, self.v)
            self.assertEqual((res.arquivos, res.preenchidos, res.faltando), (2, 1, {}))
            self.assertEqual((destino / "sub" / "a.py").read_bytes(), b"x = 'D:\\\\Notas'\r\n")
            self.assertEqual((destino / "b.bin").read_bytes(), b"\xff\x00__JARVIS_USER__")
            self.assertTrue((destino / "so_no_destino.txt").exists())


class TestPerfil(unittest.TestCase):
    def test_valido(self):
        validar(perfil())
        validar(perfil(nome="D'Ávila", dominio="prefeitura.gov.br"))

    def test_invalidos(self):
        for extra in ({"nome": "Ana Maria"}, {"nome": "Ana1"}, {"cidade_curta": "Outra"},
                      {"pasta_cerebro": "Notas"}, {"dominio": "x"}, {"fuso": "Brasilia"}, {"nome": ""}):
            with self.assertRaises(PerfilInvalido, msg=str(extra)):
                validar(perfil(**extra))

    def test_pergunta_de_novo_ate_ficar_valido(self):
        r = Roteiro(["Ana1", "", "Vila Nova", "", r"C:\Users\ana", "", "", "", "",   # nome inválido
                     "Ana", "Ana Souza", "", "", "", "", "", "", "PREF.GOV.BR"])
        from instalador.perfil import perguntar
        p = perguntar(r.console())
        self.assertEqual((p.nome, p.nome_completo, p.cidade_curta), ("Ana", "Ana Souza", "Vila"))
        self.assertEqual(p.pasta_cerebro, str(Path(r"C:\Users\ana") / "Documents" / "Cerebro"))
        self.assertEqual(p.dominio, "pref.gov.br")
        self.assertTrue(any("Ajuste" in s for s in r.saida))


class TestEstado(unittest.TestCase):
    def test_ida_e_volta(self):
        with tempfile.TemporaryDirectory() as d:
            c = Path(d, "x", "estado.json")
            e = Estado(c, perfil=perfil(), escolhidos=["voz"])
            e.marcar("base", OK, "feito")
            e2 = Estado.carregar(c)
            self.assertEqual(e2.perfil, perfil())
            self.assertEqual(e2.escolhidos, ["voz"])
            self.assertEqual(e2.situacao("base").status, OK)
            self.assertEqual(e2.situacao("nada").status, "pendente")
            self.assertEqual(json.loads(c.read_text(encoding="utf-8"))["versao"], 1)

    def test_versao_desconhecida(self):
        with tempfile.TemporaryDirectory() as d:
            c = Path(d, "estado.json")
            c.write_text('{"versao": 99}', encoding="utf-8")
            with self.assertRaises(ValueError):
                Estado.carregar(c)


class TestCatalogo(unittest.TestCase):
    def setUp(self):
        self.cat = Catalogo.carregar(RAIZ / "installer" / "recursos.toml")

    def test_ordem_respeita_dependencias(self):
        ordem = self.cat.ordem(["reunioes", "telegram"])
        self.assertEqual(ordem[:2], ["base", "ia"])
        self.assertLess(ordem.index("voz"), ordem.index("reunioes"))
        self.assertEqual(len(ordem), len(set(ordem)))

    def test_origens_de_copia_existem(self):
        for r in self.cat.recursos.values():
            for c in r.copiar:
                self.assertTrue((RAIZ / c.de).exists(), f"{r.id}: {c.de}")

    def test_ciclo_e_destino_absoluto_sao_recusados(self):
        with tempfile.TemporaryDirectory() as d:
            for corpo in ('[[recurso]]\nid="a"\nnome="a"\ndescricao=""\netapas=[]\ndepende=["a"]\n',
                          '[[recurso]]\nid="a"\nnome="a"\ndescricao=""\netapas=[]\ncopiar=[{de="x",para="../y"}]\n'):
                p = Path(d, "r.toml")
                p.write_text(corpo, encoding="utf-8")
                with self.assertRaises(ValueError):
                    Catalogo.carregar(p)


class TestFluxo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"

    def test_instala_base_e_recurso_marcado_e_retoma(self):
        ids = list(Catalogo.carregar(RAIZ / "installer" / "recursos.toml").recursos)
        pdf = str(ids.index("pdf") + 1)
        perfil_respostas = ["Ana", "Ana Souza", "Vila Nova", "", str(self.tmp / "u"), "", "", "", ""]
        r = Roteiro(perfil_respostas + [pdf, "i", "s"])
        ex = ExecutorFalso({" -z ": sistema.Saida(0, "OK")})
        self.assertEqual(principal.rodar(r.console(), RAIZ, self.home, ex=ex), 0)

        est = Estado.carregar(self.home / principal.NOME_ESTADO)
        self.assertEqual(est.escolhidos, ["pdf"])
        self.assertEqual(est.situacao("base").status, OK)
        self.assertEqual(est.situacao("ia").status, OK)
        self.assertTrue(ex.achou(" model"))
        self.assertEqual(est.situacao("pdf").status, AVISO)
        texto = (self.home / "jarvis" / "transcript_hygiene.py").read_text(encoding="utf-8")
        self.assertNotIn("__JARVIS_", texto)
        self.assertIn('_HOTWORD_CIDADE = "Vila Nova".split()', texto)
        self.assertTrue((self.home / "skills" / "productivity" / "pdf-signing-local").is_dir())
        self.assertFalse((self.home / "skills" / "prefeitura").exists())

        # Segunda rodada: não pergunta o perfil de novo e lembra o que estava marcado.
        r2 = Roteiro(["s"])
        self.assertEqual(principal.rodar(r2.console(), RAIZ, self.home), 0)
        self.assertTrue(any("[x]" in s and "Assinatura de PDF" in s for s in r2.saida))

    def test_recusa_instancia_de_origem_e_pasta_alheia(self):
        (self.home / "jarvis").mkdir(parents=True)
        with self.assertRaises(principal.Recusado):
            principal.rodar(Roteiro([]).console(), RAIZ, self.home)
        principal.checar_destino(self.home, False, forcar=True)  # com --forcar passa
        (self.home / "jarvis" / "exportacao_regras.toml").write_text("", encoding="utf-8")
        with self.assertRaises(principal.Recusado):
            principal.checar_destino(self.home, True, forcar=True)

    def test_obrigatorio_nao_desmarca(self):
        r = Roteiro(["Ana", "Ana Souza", "Vila Nova", "", str(self.tmp / "u"), "", "", "", "", "1", "99", "s"])
        principal.rodar(r.console(), RAIZ, self.home, ex=ExecutorFalso())
        self.assertTrue(any("obrigatório." in s for s in r.saida))
        self.assertTrue(any("Opção inválida: 99" in s for s in r.saida))


if __name__ == "__main__":
    unittest.main()
