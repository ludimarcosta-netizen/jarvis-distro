import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import exportar  # noqa: E402

REGRAS = r"""
[[trocar]]
regex = '(?i)[A-Z]:\\\\Users\\\\Fulano'
troca = '__JARVIS_USERPROFILE_BS2__'

[[trocar]]
regex = '(?i)[A-Z]:\\Users\\Fulano'
troca = '__JARVIS_USERPROFILE_BS__'

[[trocar]]
literal = 'Fulano'
troca = '__JARVIS_USER__'

[proibido]
padroes = ['(?i)fulano', '[\w.+-]+@gmail\.com']

[pessoas]
catalogo = '{pessoas}'
"""

MANIFESTO = {
    "geradas": ["jarvis"],
    "excluir": ["*__pycache__*", "*.db", "*/data/*"],
    "copiar": [{"de": "jarvis", "para": "jarvis", "incluir": ["*.py", "web/**/*", "data/*"]}],
    "segredos": {"padroes": [r"AIza[0-9A-Za-z_\-]{35}"]},
}


class ExportarTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name)
        self.origem = self.raiz / "origem"
        (self.origem / "jarvis" / "web").mkdir(parents=True)
        (self.origem / "jarvis" / "data").mkdir()
        (self.origem / "jarvis" / "__pycache__").mkdir()
        pessoas = self.raiz / "pessoas"
        pessoas.mkdir()
        (pessoas / "Beltrana Souza.md").write_text("x", encoding="utf-8")
        (pessoas / "Zé.md").write_text("x", encoding="utf-8")  # curto demais: ignorado
        regras = self.raiz / "regras.toml"
        regras.write_text(REGRAS.replace("{pessoas}", str(pessoas)), encoding="utf-8")
        self.trocas, self.proibidos = exportar.carregar_regras(regras)

    def tearDown(self):
        self._tmp.cleanup()

    def escrever(self, rel, conteudo):
        p = self.origem / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(conteudo, bytes):
            p.write_bytes(conteudo)
        else:
            p.write_text(conteudo, encoding="utf-8")

    def rodar(self):
        return exportar.exportar(self.origem, MANIFESTO, self.trocas, self.proibidos)

    def test_troca_nome_e_caminhos_preservando_separador(self):
        self.escrever("jarvis/a.py", 'X = "C:\\\\Users\\\\Fulano\\\\x"\nY = r"C:\\Users\\Fulano"\nprint("Ola Fulano")\n')
        res = self.rodar()
        texto = res.arquivos["jarvis/a.py"].decode()
        self.assertIn('"__JARVIS_USERPROFILE_BS2__\\\\x"', texto)
        self.assertIn('r"__JARVIS_USERPROFILE_BS__"', texto)
        self.assertIn("Ola __JARVIS_USER__", texto)
        self.assertEqual(res.problemas, [])

    def test_so_copia_o_que_o_manifesto_permite(self):
        self.escrever("jarvis/a.py", "ok")
        self.escrever("jarvis/web/app.js", "ok")
        self.escrever("jarvis/notas.md", "fora da lista")
        self.escrever("jarvis/data/voz.npy", "dado pessoal")
        self.escrever("jarvis/__pycache__/a.cpython-311.pyc", b"\x00\x01")
        self.escrever("jarvis/base.db", "x")
        self.assertEqual(sorted(self.rodar().arquivos), ["jarvis/a.py", "jarvis/web/app.js"])

    def test_pessoa_do_catalogo_vira_ficticio_estavel_em_todas_as_formas(self):
        self.escrever("jarvis/a.py", "# reunião com Beltrana Souza; beltrana; BELTRANA; Beltrana\n# Zé não conta\n")
        res = self.rodar()
        texto = res.arquivos["jarvis/a.py"].decode()
        self.assertEqual(res.problemas, [])
        self.assertNotIn("eltrana", texto.lower())
        completo = texto.split("reunião com ")[1].split(";")[0]
        primeiro = completo.split()[0]
        self.assertIn(f"; {primeiro.lower()}; {primeiro.upper()}; {primeiro}\n", texto.replace("\r\n", "\n"))
        self.assertIn("Zé não conta", texto)

    def test_forma_da_pessoa_que_escapa_da_troca_e_barrada(self):
        self.escrever("jarvis/a.py", "x = 'BeltranaSouza'\ny = 'Beltrána'\n")
        res = self.rodar()
        # "BeltranaSouza" não é palavra inteira: passa; "Beltrána" (acento) dobra para Beltrana e é barrado.
        self.assertEqual([p.linha for p in res.problemas], [2])

    def test_barra_email_e_sobra_pessoal_que_nao_tem_regra(self):
        # "fulano" minúsculo não tem regra de troca: sobra e é barrado (uma vez por linha).
        self.escrever("jarvis/a.py", "x = 'fulano.silva@gmail.com'\n")
        res = self.rodar()
        self.assertEqual([(p.linha, p.tipo) for p in res.problemas], [(1, "pessoal")])

    def test_segredo_e_barrado_sem_mostrar_valor(self):
        chave = "AIza" + "A" * 35
        self.escrever("jarvis/a.py", f"KEY = '{chave}'\n")
        res = self.rodar()
        self.assertEqual(res.problemas[0].tipo, "segredo")
        self.assertEqual(res.problemas[0].trecho, "<oculto>")

    def test_binario_e_copiado_sem_alterar(self):
        dados = b"\x00Fulano\xff"
        self.escrever("jarvis/web/som.wav", dados)
        res = self.rodar()
        self.assertEqual(res.arquivos["jarvis/web/som.wav"], dados)

    def test_gravar_recria_so_pastas_geradas(self):
        repo = self.raiz / "repo"
        (repo / "jarvis").mkdir(parents=True)
        (repo / "jarvis" / "velho.py").write_text("removido", encoding="utf-8")
        (repo / "installer").mkdir()
        (repo / "installer" / "fica.py").write_text("fica", encoding="utf-8")
        self.escrever("jarvis/a.py", "novo")
        exportar.gravar(self.rodar(), repo, MANIFESTO["geradas"])
        self.assertFalse((repo / "jarvis" / "velho.py").exists())
        self.assertTrue((repo / "jarvis" / "a.py").exists())
        self.assertTrue((repo / "installer" / "fica.py").exists())

    def test_ajuste_aplica_com_crlf_e_barra_quando_trecho_sumiu(self):
        import ajustes
        a = ajustes.AJUSTES[0]
        texto, faltando = ajustes.aplicar(a.arquivo, "x\r\n" + a.de.replace("\n", "\r\n") + "\r\n")
        self.assertEqual((texto, faltando), ("x\r\n" + a.para.replace("\n", "\r\n") + "\r\n", []))
        self.escrever(a.arquivo, "codigo mudou")
        res = self.rodar()
        self.assertTrue(any(p.tipo == "ajuste" and p.arquivo == a.arquivo for p in res.problemas))


if __name__ == "__main__":
    unittest.main()
