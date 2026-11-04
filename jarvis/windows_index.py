"""Busca por nome de arquivo no índice do Windows Search (o mesmo do Explorer e do menu Iniciar).

A varredura em ``local_files`` parava no teto de 10.000 itens e via só parte das pastas; o índice
responde os discos inteiros em ~0,5 s. O ambiente do Hermes não tem pywin32/comtypes, então o ADO
roda num PowerShell curto e a consulta segue por variável de ambiente, nunca montada no comando.

CONTAINS usa o índice de palavras: ignora acentos ("relatorio" acha "Relatório") e casa o começo
das palavras ("contrato" acha "CONTRATOS"). LIKE '%x%' sem filtro de extensão levava ~15 s.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

TIMEOUT = 5.0
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$c = New-Object -ComObject ADODB.Connection
$c.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
$r = $c.Execute($env:JARVIS_INDEX_SQL)
while (-not $r.EOF) { [Console]::Out.WriteLine($r.Fields.Item(0).Value); $r.MoveNext() }
"""


def _terms(tokens: list[str]) -> list[str]:
    """Palavras ASCII dos termos já sem acento ("040-2017" vira "040" e "2017"): aspas, curingas e
    operadores do usuário nunca chegam à consulta."""
    return [word for token in tokens for word in re.findall(r"[a-z0-9]+", token)]


def find(roots: Iterable[Path], tokens: list[str], *, limit: int = 1000) -> list[Path] | None:
    """Arquivos indexados sob ``roots`` com palavras começando por cada termo, mais recentes primeiro.
    Quem chama confere o nome de verdade. None quando o índice não responde (fora do Windows,
    serviço parado, erro)."""
    roots, terms = list(roots), _terms(tokens)
    if os.name != "nt" or not roots or not terms:
        return None
    scopes = " OR ".join("SCOPE='file:" + str(root).replace("'", "''") + "'" for root in roots)
    words = " AND ".join(f'"{term}*"' for term in terms)
    sql = (f"SELECT TOP {int(limit)} System.ItemPathDisplay FROM SYSTEMINDEX WHERE ({scopes}) "
           f"AND System.ItemType <> 'Directory' AND CONTAINS(System.FileName, '{words}') "
           "ORDER BY System.DateModified DESC")
    try:
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
                              capture_output=True, timeout=TIMEOUT, env={**os.environ, "JARVIS_INDEX_SQL": sql},
                              creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return [Path(line) for line in done.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
