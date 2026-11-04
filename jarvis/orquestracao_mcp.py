"""Servidor MCP stdio do ledger de orquestração. Pelas IAs, delega somente em modo leitura."""
import json
import logging
import sqlite3
import sys
from typing import Any

import orquestracao
from cerebro_mcp import STRING, tool

LOG = logging.getLogger(__name__)

TOOLS = [
    tool("orquestracao_executar",
         "Delega uma tarefa de leitura/análise a outro CLI (claude, codex ou antigravity) e registra no ledger. "
         "Roda em segundo plano: acompanhe com orquestracao_status. Não escreve arquivos; não substitui sua validação.",
         {"executor": {"type": "string", "enum": sorted(orquestracao.EXECUTORES)}, "tarefa": STRING,
          "pasta": STRING, "origem": {"type": "string", "enum": sorted(orquestracao.ORIGENS - {"manual"})},
          "modelo": STRING, "esforco": {"type": "string", "enum": sorted(orquestracao.ESFORCOS - {""})},
          "validacao": {"type": "string", "enum": ["contem", "exato", "json"]}, "esperado": STRING,
          "timeout_s": {"type": "integer", "minimum": 30, "maximum": 7200}},
         ["executor", "tarefa", "pasta", "origem"], read_only=False),
    tool("orquestracao_status", "Status, resposta e métricas de uma execução delegada.", {"id": STRING}, ["id"]),
    tool("orquestracao_listar", "Execuções recentes do ledger.",
         {"limite": {"type": "integer", "minimum": 1, "maximum": 50}}, []),
    tool("orquestracao_relatorio",
         "Métricas por origem, executor e modelo (inclui subagentes do Jarvis). Custos são estimativas; nulo = desconhecido.",
         {"dias": {"type": "integer", "minimum": 1, "maximum": 365}}, []),
]


def executar(executor: str, tarefa: str, pasta: str, origem: str, modelo: str = "", esforco: str = "",
             validacao: str = "", esperado: str = "", timeout_s: int = 1800) -> dict[str, Any]:
    return orquestracao.iniciar(executor, tarefa, pasta, origem=origem, modo="leitura", modelo=modelo,
                                esforco=esforco, validacao=validacao, esperado=esperado, timeout_s=timeout_s)


FUNCTIONS = {"orquestracao_executar": executar, "orquestracao_status": orquestracao.consultar,
             "orquestracao_listar": orquestracao.listar, "orquestracao_relatorio": orquestracao.relatorio}


def dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    if "id" not in message:
        return None
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
    method, params = message.get("method"), message.get("params", {})
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"} else "2025-06-18"
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "orquestracao-local", "version": "1.0.0"},
                  "instructions": "Ledger local de delegação entre IAs. Execução pelas IAs só em modo leitura."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            name, arguments = params.get("name"), params.get("arguments", {})
            if name == "orquestracao_status" and isinstance(arguments, dict):
                arguments = {"execucao": arguments.get("id")}
            if name not in FUNCTIONS or not isinstance(arguments, dict):
                raise ValueError("Ferramenta inválida")
            data = FUNCTIONS[name](**arguments)
            result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": False}
        except ValueError as exc:
            # Mensagens de validação são próprias e não contêm a tarefa; ajudam a IA a corrigir a chamada.
            result = {"content": [{"type": "text", "text": f"Entrada recusada: {exc}"}], "isError": True}
        except (TypeError, OSError, sqlite3.Error):
            LOG.warning("orquestracao_tool_failed")
            result = {"content": [{"type": "text", "text": "Entrada inválida ou ledger indisponível."}], "isError": True}
    else:
        response["error"] = {"code": -32601, "message": "Method not found"}
        return response
    response["result"] = result
    return response


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            if len(line) > 200_000:
                raise ValueError("Message too large")
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("Invalid request")
            response = dispatch(message)
        except (ValueError, TypeError, AttributeError):
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Invalid JSON-RPC message"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
