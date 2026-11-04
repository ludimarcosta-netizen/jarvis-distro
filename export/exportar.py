"""Exporta o código do Jarvis de uma instância pessoal para o repositório de distribuição.

Só copia o que o manifesto permite, troca valores pessoais por marcadores (__JARVIS_*__)
e barra a exportação se sobrar dado pessoal ou segredo.

Uso:
    python export/exportar.py --origem %LOCALAPPDATA%/hermes --regras <arquivo local> [--verificar]

--verificar: não escreve nada, só lista o que seria barrado.
Requer Python 3.11+ (tomllib). Sem dependências externas.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import ajustes

REPO = Path(__file__).resolve().parent.parent
MANIFESTO_PADRAO = REPO / "export" / "manifesto.toml"
REGRAS_PADRAO = Path(os.path.expandvars(r"%LOCALAPPDATA%\hermes\jarvis\exportacao_regras.toml"))


@dataclass(frozen=True)
class Regra:
    padrao: re.Pattern[str]
    troca: str


@dataclass(frozen=True)
class Problema:
    arquivo: str
    linha: int
    tipo: str  # "pessoal" | "segredo" | "ajuste"
    trecho: str


@dataclass
class Resultado:
    arquivos: dict[str, bytes] = field(default_factory=dict)  # caminho no repo -> conteúdo
    problemas: list[Problema] = field(default_factory=list)


# ---------- regras ----------

FICTICIOS = (
    "Aurora Prado", "Bento Lacerda", "Celina Marques", "Davi Teixeira", "Elisa Rangel", "Fabio Quintela",
    "Gisele Amaral", "Heitor Barcelos", "Iara Vilela", "Joaquim Seixas", "Kelly Andrade", "Lauro Fontes",
    "Marina Coutinho", "Nelson Brandao", "Olivia Pacheco", "Paulo Sampaio", "Quiteria Leal", "Raul Medeiros",
    "Sabrina Toledo", "Tadeu Pimenta", "Ursula Neves", "Valter Guimaraes", "Wanda Moreira", "Xavier Rocha",
    "Yara Campos", "Zuleica Antunes", "Abel Vasconcelos", "Bruna Saldanha", "Caio Figueira", "Debora Lins",
    "Edson Paiva", "Flavia Matos", "Gustavo Arruda", "Helena Bastos", "Igor Peixoto", "Janaina Ribas",
    "Kleber Dantas", "Larissa Godoy", "Murilo Esteves", "Nadia Furtado", "Otavio Sales", "Priscila Valente",
    "Rogerio Bueno", "Simone Calado", "Tiago Lobato", "Vanessa Prates", "Wesley Cardim", "Adriana Mota",
    "Breno Falcao", "Cintia Morais", "Danilo Rezende", "Erica Simoes", "Fausto Leme", "Giovana Abreu",
    "Hugo Tavares", "Ingrid Couto", "Jonas Viana", "Karina Lopes", "Leandro Faria", "Monica Reis",
    "Norberto Gama", "Odete Pinheiro", "Pedro Albuquerque", "Rita Coelho", "Samuel Duarte", "Tereza Galvao",
    "Vinicius Aragao", "Wilma Freitas", "Alice Monteiro", "Bernardo Siqueira", "Clara Bittencourt",
    "Diego Nogueira", "Estela Fonseca", "Felipe Carvalho", "Gabriela Tenorio", "Henrique Lustosa",
    "Isadora Vieira", "Julio Macedo", "Lorena Cabral", "Mateus Ventura", "Natalia Serra", "Orlando Brito",
    "Patricia Gusmao", "Renan Barreto", "Sofia Mourao", "Tomas Feitosa", "Viviane Rios", "Walter Assis",
)


def dobrar(texto: str) -> str:
    """Remove acentos: 'Mirélla' -> 'Mirella'."""
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def nomes_do_catalogo(pasta: Path) -> list[str]:
    """Nomes completos (arquivo `Nome.md`) e o primeiro nome de cada um, com 4+ letras."""
    if not pasta.is_dir():
        raise SystemExit(f"Pasta do catálogo de pessoas não encontrada: {pasta}")
    nomes: set[str] = set()
    for p in pasta.glob("*.md"):
        completo = p.stem.strip()
        for n in (completo, completo.split()[0] if completo else ""):
            if len(n) >= 4:
                nomes.add(n)
    return sorted(nomes, key=lambda n: (-len(n), n))  # mais longos primeiro


def _variantes(real: str, ficticio: str) -> list[tuple[str, str]]:
    pares = [(real, ficticio), (real.lower(), ficticio.lower()), (real.upper(), ficticio.upper())]
    pares += [(dobrar(a), dobrar(b)) for a, b in pares]
    vistos: dict[str, str] = {}
    for a, b in pares:
        vistos.setdefault(a, b)
    return list(vistos.items())


def trocas_de_pessoas(nomes: list[str]) -> list[Regra]:
    """Cada nome real vira um nome fictício estável.

    O mesmo primeiro nome real vira sempre o mesmo primeiro nome fictício, então "Fulana" e
    "Fulana Souza" continuam reconhecíveis como a mesma pessoa depois da troca.
    """
    def chave(s: str) -> str:
        return dobrar(s).lower()

    reais = {chave(w) for n in nomes for w in n.split()}
    primeiros = [f for f in dict.fromkeys(x.split()[0] for x in FICTICIOS) if chave(f) not in reais]
    sobrenomes = [s for s in dict.fromkeys(x.split()[1] for x in FICTICIOS) if chave(s) not in reais]
    mapa_primeiro: dict[str, str] = {}
    mapa: dict[str, str] = {}
    for n in nomes:
        if chave(n) in mapa:
            continue
        p = chave(n.split()[0])
        if p not in mapa_primeiro:
            if len(mapa_primeiro) >= len(primeiros):
                raise SystemExit("Faltam nomes fictícios: aumente FICTICIOS.")
            mapa_primeiro[p] = primeiros[len(mapa_primeiro)]
        if " " in n:
            sufixos = len(n.split()) - 1
            usados = sum(1 for v in mapa.values() if " " in v)
            if usados + sufixos > len(sobrenomes) * 4:
                raise SystemExit("Faltam sobrenomes fictícios: aumente FICTICIOS.")
            resto = [sobrenomes[(usados + k) % len(sobrenomes)] for k in range(sufixos)]
            mapa[chave(n)] = " ".join([mapa_primeiro[p], *resto])
        else:
            mapa[chave(n)] = mapa_primeiro[p]
    regras: list[Regra] = []
    for n in nomes:
        for a, b in _variantes(n, mapa[chave(n)]):
            regras.append(Regra(re.compile(rf"(?<!\w){re.escape(a)}(?!\w)"), b))
    return regras


def carregar_ignorados(caminho: Path) -> tuple[str, ...]:
    dados = tomllib.loads(caminho.read_text(encoding="utf-8"))
    return tuple(dados.get("proibido", {}).get("ignorar", []))


def carregar_regras(caminho: Path) -> tuple[list[Regra], list[re.Pattern[str]]]:
    """Lê o arquivo local de regras: substituições (em ordem) e padrões proibidos.

    Com `[pessoas] catalogo`, os nomes do catálogo viram nomes fictícios e continuam proibidos
    (qualquer forma que escape da troca barra a exportação).
    """
    dados = tomllib.loads(caminho.read_text(encoding="utf-8"))
    trocas = [
        Regra(re.compile(r["regex"] if "regex" in r else re.escape(r["literal"])), r["troca"])
        for r in dados.get("trocar", [])
    ]
    proibidos = [re.compile(p) for p in dados.get("proibido", {}).get("padroes", [])]
    catalogo = dados.get("pessoas", {}).get("catalogo")
    if catalogo:
        nomes = nomes_do_catalogo(Path(catalogo))
        trocas += trocas_de_pessoas(nomes)
        proibidos += [re.compile(rf"(?i)(?<!\w){re.escape(dobrar(n))}(?!\w)") for n in nomes]
    return trocas, proibidos


def aplicar_trocas(texto: str, trocas: list[Regra]) -> str:
    for r in trocas:
        texto = r.padrao.sub(lambda _m, t=r.troca: t, texto)
    return texto


def verificar(rel: str, texto: str, proibidos: list[re.Pattern[str]], segredos: list[re.Pattern[str]],
              ignorar: tuple[str, ...] = ()) -> list[Problema]:
    """Lista linhas com segredo ou dado pessoal. `ignorar`: trechos falsos conhecidos (ex.: CPF de exemplo)."""
    achados: list[Problema] = []
    for n, linha in enumerate(texto.splitlines(), 1):
        limpa = linha
        for t in ignorar:
            limpa = limpa.replace(t, "")
        for tipo, padroes in (("segredo", segredos), ("pessoal", proibidos)):
            alvos = (linha,) if tipo == "segredo" else (limpa, dobrar(limpa))
            for p in padroes:
                if any(p.search(a) for a in alvos):
                    trecho = "<oculto>" if tipo == "segredo" else linha.strip()[:160]
                    achados.append(Problema(rel, n, tipo, trecho))
                    break
    return achados


# ---------- seleção de arquivos ----------

def excluido(rel: str, excluir: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch("/" + rel, p) for p in excluir)


def selecionar(origem: Path, bloco: dict, excluir: list[str]) -> dict[str, Path]:
    """Retorna {caminho no repo: arquivo de origem} para um bloco [[copiar]]."""
    # `de` pode ser uma pasta fora da instância, com variável de ambiente (ex.: %USERPROFILE%/Projects/x).
    base = origem / os.path.expandvars(bloco["de"])
    escolhidos: dict[str, Path] = {}
    for padrao in bloco["incluir"]:
        for arq in base.glob(padrao):
            if not arq.is_file():
                continue
            raiz = origem if arq.is_relative_to(origem) else base
            rel_origem = arq.relative_to(raiz).as_posix()
            if excluido(rel_origem, excluir):
                continue
            destino = f'{bloco["para"]}/{arq.relative_to(base).as_posix()}'
            escolhidos[destino] = arq
    return escolhidos


def eh_texto(dados: bytes) -> bool:
    if b"\x00" in dados[:8192]:
        return False
    try:
        dados.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


# ---------- hermes-agent ----------

def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
        check=True, capture_output=True,
    ).stdout


def coletar_hermes(origem: Path, cfg: dict, excluir: list[str]) -> dict[str, bytes]:
    repo = origem / cfg["repo"]
    base = cfg["base"]
    para = cfg["para"]
    descricao = git(repo, "describe", "--tags", "--always", base).decode().strip()
    saida = {
        f"{para}/BASE.txt": f"{descricao}\n".encode(),
        f"{para}/jarvis.patch": git(repo, "diff", "--binary", "--no-color", base),
    }
    novos = git(repo, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
    for rel in filter(None, novos):
        if excluido(rel, excluir) or any(s in rel for s in ("/dist/", "/out/", "/release/")):
            continue
        saida[f"{para}/novos/{rel}"] = (repo / rel).read_bytes()
    return saida


# ---------- exportação ----------

def exportar(origem: Path, manifesto: dict, trocas: list[Regra], proibidos: list[re.Pattern[str]],
             ignorar: tuple[str, ...] = ()) -> Resultado:
    excluir = manifesto.get("excluir", [])
    cfg_segredos = manifesto.get("segredos", {})
    segredos = [re.compile(p) for p in cfg_segredos.get("padroes", [])]
    # Segredos falsos conhecidos (fixtures de teste), no formato "arquivo:linha".
    falsos = set(cfg_segredos.get("falsos_positivos", []))
    brutos: dict[str, bytes] = {}
    for bloco in manifesto.get("copiar", []):
        for destino, arq in selecionar(origem, bloco, excluir).items():
            dados = arq.read_bytes()
            if "cortar_em" in bloco:  # leva só o trecho antes do marcador (ex.: SOUL.md sem o bloco do Cerebro)
                corte = bloco["cortar_em"].encode("utf-8")
                dados = dados.split(corte, 1)[0].rstrip() + b"\n"
            brutos[destino] = dados
    if "hermes" in manifesto:
        brutos.update(coletar_hermes(origem, manifesto["hermes"], excluir))

    res = Resultado()
    for destino, dados in sorted(brutos.items()):
        if eh_texto(dados):
            texto = aplicar_trocas(dados.decode("utf-8"), trocas)
            texto, faltando = ajustes.aplicar(destino, texto)
            res.problemas += [Problema(destino, 0, "ajuste", f"trecho não encontrado: {m}") for m in faltando]
            res.problemas += [
                p for p in verificar(destino, texto, proibidos, segredos, ignorar)
                if not (p.tipo == "segredo" and f"{p.arquivo}:{p.linha}" in falsos)
            ]
            dados = texto.encode("utf-8")
        res.arquivos[destino] = dados
    return res


def gravar(res: Resultado, repo: Path, geradas: list[str]) -> None:
    for pasta in geradas:
        alvo = repo / pasta
        if alvo.exists():
            shutil.rmtree(alvo)
    for destino, dados in res.arquivos.items():
        caminho = repo / destino
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_bytes(dados)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--origem", type=Path, default=Path(os.path.expandvars(r"%LOCALAPPDATA%\hermes")))
    ap.add_argument("--regras", type=Path, default=REGRAS_PADRAO)
    ap.add_argument("--manifesto", type=Path, default=MANIFESTO_PADRAO)
    ap.add_argument("--repo", type=Path, default=REPO)
    ap.add_argument("--verificar", action="store_true", help="só lista problemas, não grava")
    a = ap.parse_args(argv)

    if not a.regras.is_file():
        print(f"Arquivo de regras não encontrado: {a.regras}", file=sys.stderr)
        return 2
    manifesto = tomllib.loads(a.manifesto.read_text(encoding="utf-8"))
    trocas, proibidos = carregar_regras(a.regras)
    res = exportar(a.origem, manifesto, trocas, proibidos, carregar_ignorados(a.regras))

    for p in res.problemas:
        print(f"[{p.tipo}] {p.arquivo}:{p.linha}: {p.trecho}")
    print(f"{len(res.arquivos)} arquivos selecionados, {len(res.problemas)} problemas.")
    if res.problemas:
        print("Exportação barrada: ajuste as regras ou o manifesto.", file=sys.stderr)
        return 1
    if not a.verificar:
        gravar(res, a.repo, manifesto.get("geradas", []))
        print(f"Gravado em {a.repo}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
