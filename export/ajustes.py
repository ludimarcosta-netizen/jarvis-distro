"""Ajustes de código aplicados na exportação, DEPOIS das trocas por marcadores.

Servem para trechos que não basta parametrizar (código que supõe o formato do nome da cidade, por
exemplo). O texto `de` já está com marcadores, então este arquivo não tem dado pessoal.
Se `de` não for encontrado (o original mudou), a exportação barra: revise o ajuste.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Ajuste:
    arquivo: str  # caminho no repositório, com /
    de: str
    para: str
    motivo: str


_HIGIENE_DE = r'''_HOTWORD_VOCAB = {"jarvis", "__JARVIS_USER_LOWER__", "__JARVIS_USER_LOWER__", "__JARVIS_CIDADE_CURTA_FOLD__", "do", "ceu"}
_HOTWORD_LEAK_RE = re.compile(
    r"(?:\bjarvis\W+)?\b__JARVIS_USER_REGEX__\W+__JARVIS_CIDADE_CURTA_REGEX__(?:\W+do(?:\W+c[ée]u)?)?(?:\W+jarvis)?\b"
    r"|(?:\b(?:do\W+)?c[ée]u\b\W*){3,}", re.IGNORECASE)


def hotword_leak(text: str) -> bool:
    """Trecho INTEIRO feito só de pedaços das hotwords ("__JARVIS_USER__ __JARVIS_CIDADE__", "do Céu", "Céu").
    "__JARVIS_USER__" ou "Jarvis" sozinhos podem ser fala real (alguém chamando) e não contam."""
    tokens = re.findall(r"\w+", _fold(text))
    if not tokens or not set(tokens) <= _HOTWORD_VOCAB:
        return False
    return bool(set(tokens) & {"__JARVIS_CIDADE_CURTA_FOLD__", "ceu"}) or (len(tokens) >= 2 and bool(set(tokens) & {"__JARVIS_USER_LOWER__", "__JARVIS_USER_LOWER__"}))'''

_HIGIENE_PARA = r'''_HOTWORD_USER = "__JARVIS_USER__"
_HOTWORD_CIDADE = "__JARVIS_CIDADE__".split()
_HOTWORD_CIDADE_CURTA = "__JARVIS_CIDADE_CURTA__".split()
_LIGACOES = {"do", "da", "de", "dos", "das", "e"}


def _palavra_re(palavra: str) -> str:
    """Palavra minúscula aceitando com e sem acento ("Céu" -> "c[ée]u")."""
    return "".join(f"[{c}{_fold(c)}]" if _fold(c) != c else re.escape(c) for c in palavra.casefold())


def _hotword_leak_pattern() -> str:
    """Nome + cidade (o resto do nome da cidade é opcional, palavra a palavra) ou a última palavra
    da cidade repetida 3+ vezes (com a ligação "do/da/de" antes, se houver)."""
    cauda = ""
    for palavra in reversed(_HOTWORD_CIDADE[len(_HOTWORD_CIDADE_CURTA):]):
        cauda = rf"(?:\W+{_palavra_re(palavra)}{cauda})?"
    curta = r"\W+".join(map(_palavra_re, _HOTWORD_CIDADE_CURTA))
    nome = rf"(?:\bjarvis\W+)?\b{_palavra_re(_HOTWORD_USER)}\W+{curta}{cauda}(?:\W+jarvis)?\b"
    ultima = _palavra_re(_HOTWORD_CIDADE[-1])
    antes = _HOTWORD_CIDADE[-2] if len(_HOTWORD_CIDADE) >= 2 and _fold(_HOTWORD_CIDADE[-2]) in _LIGACOES else ""
    ligacao = rf"(?:{_palavra_re(antes)}\W+)?" if antes else ""
    return nome + rf"|(?:\b{ligacao}{ultima}\b\W*){{3,}}"


_HOTWORD_CIDADE_TOKENS = set(re.findall(r"\w+", _fold(" ".join(_HOTWORD_CIDADE))))
_HOTWORD_VOCAB = {"jarvis", _fold(_HOTWORD_USER)} | _HOTWORD_CIDADE_TOKENS
_HOTWORD_LEAK_RE = re.compile(_hotword_leak_pattern(), re.IGNORECASE)


def hotword_leak(text: str) -> bool:
    """Trecho INTEIRO feito só de pedaços das hotwords ("__JARVIS_USER__ __JARVIS_CIDADE__", o fim do nome da cidade).
    "__JARVIS_USER__" ou "Jarvis" sozinhos podem ser fala real (alguém chamando) e não contam."""
    tokens = re.findall(r"\w+", _fold(text))
    if not tokens or not set(tokens) <= _HOTWORD_VOCAB:
        return False
    return bool(set(tokens) & (_HOTWORD_CIDADE_TOKENS - _LIGACOES)) or (len(tokens) >= 2 and _fold(_HOTWORD_USER) in tokens)'''

_TESTE_DE_1 = r'''        for text in ("__JARVIS_USER__ __JARVIS_CIDADE__", "__JARVIS_USER__ __JARVIS_CIDADE_CURTA__", "do Céu", "Céu.", "Céu, Céu, Céu",
                     "Jarvis __JARVIS_USER__ __JARVIS_CIDADE__ Jarvis", "__JARVIS_USER__ __JARVIS_CIDADE_CURTA__ do"):'''

_TESTE_PARA_1 = r'''        cidade, curta = "__JARVIS_CIDADE__", "__JARVIS_CIDADE_CURTA__"
        resto = cidade[len(curta):].strip() or cidade  # fim do nome da cidade
        ultima = cidade.split()[-1]
        for text in ("__JARVIS_USER__ __JARVIS_CIDADE__", "__JARVIS_USER__ __JARVIS_CIDADE_CURTA__", resto, f"{ultima}.",
                     f"{ultima}, {ultima}, {ultima}", "Jarvis __JARVIS_USER__ __JARVIS_CIDADE__ Jarvis",
                     f"__JARVIS_USER__ __JARVIS_CIDADE_CURTA__ {resto.split()[0]}"):'''

_TESTE_DE_2 = r'''        self.assertIsNone(hy.strip_hotword_leak("e o Céu do Céu do Céu"))'''

_TESTE_PARA_2 = r'''        ultima = "__JARVIS_CIDADE__".split()[-1]
        self.assertIsNone(hy.strip_hotword_leak(f"e o {ultima} {ultima} {ultima}"))'''

AJUSTES = (
    Ajuste("jarvis/transcript_hygiene.py", _HIGIENE_DE, _HIGIENE_PARA,
           "filtro de vazamento das hotwords supunha cidade no formato 'X do Céu'"),
    Ajuste("jarvis/tests/test_hygiene.py", _TESTE_DE_1, _TESTE_PARA_1, "testes do filtro com qualquer cidade"),
    Ajuste("jarvis/tests/test_hygiene.py", _TESTE_DE_2, _TESTE_PARA_2, "testes do filtro com qualquer cidade"),
)


def aplicar(destino: str, texto: str) -> tuple[str, list[str]]:
    """Aplica os ajustes do arquivo; devolve (texto, motivos dos ajustes que não encontraram o trecho)."""
    crlf = "\r\n" in texto
    faltando = []
    for a in AJUSTES:
        if a.arquivo != destino:
            continue
        de, para = (a.de.replace("\n", "\r\n"), a.para.replace("\n", "\r\n")) if crlf else (a.de, a.para)
        if texto.count(de) != 1:
            faltando.append(a.motivo)
            continue
        texto = texto.replace(de, para)
    return texto, faltando
